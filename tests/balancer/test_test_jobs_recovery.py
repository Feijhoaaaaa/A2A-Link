from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from balancer import repository as repo
from balancer.app import create_app
from balancer.db import get_connection, init_schema, write_tx


NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
SNAPSHOT = "a" * 64


def _create(conn, request_id):
    return repo.create_test_request(
        conn, request_id, SNAPSHOT, {"argv": ["pytest", "-q"]}, ["linux"],
        None,
    )["jobs"][0]


def test_balancer_restart_marks_only_expired_active_jobs_lost_and_keeps_slot(tmp_path):
    db_path = str(tmp_path / "recovery.sqlite")
    with closing(get_connection(db_path)) as conn:
        init_schema(conn)
        repo.record_test_snapshot(conn, SNAPSHOT, 12, f"test-snapshots/{SNAPSHOT}.tar")

        expired_preparing = _create(conn, "expired-preparing")
        expired_running = _create(conn, "expired-running")
        unexpired = _create(conn, "unexpired")
        terminal = _create(conn, "terminal")
        already_lost = _create(conn, "already-lost")
        queued = _create(conn, "queued")
        claimed = {
            "expired-preparing": repo.claim_test_job(conn, "runner-1", ["linux"], 60),
            "expired-running": repo.claim_test_job(conn, "runner-2", ["linux"], 60),
            "unexpired": repo.claim_test_job(conn, "runner-3", ["linux"], 60),
            "terminal": repo.claim_test_job(conn, "runner-4", ["linux"], 60),
            "already-lost": repo.claim_test_job(conn, "runner-5", ["linux"], 60),
        }
        # The first three are enough to establish the setup; each runner owns one slot.
        for request_id in ("expired-running", "unexpired"):
            job = claimed[request_id]
            repo.heartbeat_test_job(conn, job["job_id"], job["runner_id"], job["lease_token"],
                                    phase="running", log_offset=0, log_chunk=b"log evidence")
        repo.finish_test_job(conn, claimed["terminal"]["job_id"], "runner-4",
                             claimed["terminal"]["lease_token"], {"status": "succeeded"})
        conn.execute("UPDATE test_jobs SET state='lost' WHERE job_id=?", (claimed["already-lost"]["job_id"],))
        for request_id in ("expired-preparing", "expired-running", "unexpired", "already-lost"):
            expiry = NOW - timedelta(seconds=1) if request_id.startswith("expired") else NOW + timedelta(seconds=60)
            conn.execute("UPDATE test_jobs SET lease_expires_at=?, log_stored_bytes=12,"
                         " log_received_bytes=12, log_truncated=1 WHERE job_id=?",
                         (repo.ts(expiry), claimed[request_id]["job_id"]))

        old_rows = {row["job_id"]: dict(row) for row in conn.execute("SELECT * FROM test_jobs")}

    app = create_app(db_path, clock=lambda: NOW)
    with TestClient(app, base_url="http://127.0.0.1"):
        pass  # Entering and leaving the client exercises balancer startup/shutdown.

    with closing(get_connection(db_path)) as conn:
        rows = {row["job_id"]: dict(row) for row in conn.execute("SELECT * FROM test_jobs")}
        assert rows[claimed["expired-preparing"]["job_id"]]["state"] == "lost"
        assert rows[claimed["expired-running"]["job_id"]]["state"] == "lost"
        for request_id in ("expired-preparing", "expired-running"):
            before = old_rows[claimed[request_id]["job_id"]]
            after = rows[claimed[request_id]["job_id"]]
            for field in ("runner_id", "lease_token", "lease_expires_at", "log_stored_bytes",
                          "log_received_bytes", "log_truncated", "result"):
                assert after[field] == before[field]
            assert after["result"] is None
        assert rows[unexpired["job_id"]]["state"] == "running"
        assert rows[queued["job_id"]]["state"] == "queued"
        assert rows[terminal["job_id"]]["state"] == "succeeded"
        assert rows[already_lost["job_id"]]["state"] == "lost"

        assert repo.reconcile_expired_test_job_leases(conn, repo.ts(NOW)) == []
        assert repo.claim_test_job(conn, "runner-1", ["linux"], 60) is None
        assert repo.claim_test_job(conn, "runner-new", ["linux"], 60)["state"] == "preparing"
        for request_id in ("expired-preparing", "expired-running"):
            job = claimed[request_id]
            with pytest.raises(repo.ApiError) as error:
                repo.finish_test_job(conn, job["job_id"], job["runner_id"], job["lease_token"],
                                     {"status": "succeeded"})
            assert error.value.code == "lease_expired"



def test_request_listing_reconciles_expiry_and_returns_ordered_runner_metadata(tmp_path):
    db_path = str(tmp_path / "list.sqlite")
    with closing(get_connection(db_path)) as conn:
        init_schema(conn)
        repo.record_test_snapshot(conn, SNAPSHOT, 12, f"test-snapshots/{SNAPSHOT}.tar")
        expired = _create(conn, "request-b")
        older = _create(conn, "request-a")
        newer = _create(conn, "request-c")
        token_sha = "b" * 64
        repo.register_test_runner(conn, "runner-linux", ["linux"], "linux", token_sha)
        claimed = repo.claim_test_job(conn, "runner-linux", ["linux"], 60)
        conn.execute("UPDATE test_jobs SET lease_expires_at=?, log_stored_bytes=5, log_truncated=1"
                     " WHERE job_id=?", (repo.ts(NOW - timedelta(seconds=1)), claimed["job_id"]))
        # Make update times distinct without relying on wall-clock scheduling.
        conn.execute("UPDATE test_jobs SET updated_at=? WHERE job_id=?", ("2026-11-27T10:00:00Z", older["job_id"]))
        conn.execute("UPDATE test_jobs SET updated_at=? WHERE job_id=?", ("2026-11-27T11:00:00Z", claimed["job_id"]))
        conn.execute("UPDATE test_jobs SET updated_at=? WHERE job_id=?", ("2026-11-27T10:00:00Z", newer["job_id"]))

    app = create_app(db_path, clock=lambda: NOW)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/test-requests?limit=2")
        assert response.status_code == 200, response.text
        requests = response.json()["requests"]
        assert [request["request_id"] for request in requests] == ["request-b", "request-a"]
        assert requests[0]["jobs"][0]["state"] == "lost"
        assert requests[0]["jobs"][0]["runner_platform"] == "linux"
        assert requests[0]["jobs"][0]["log_stored_bytes"] == 5
        assert requests[0]["jobs"][0]["log_truncated"] is True
        assert "lease_token" not in requests[0]["jobs"][0]
        assert all("session_ref" not in request for request in requests)
        assert client.get("/test-requests?limit=0").status_code == 422
        assert client.get("/test-requests?limit=51").status_code == 422


def test_request_list_runner_platform_is_null_before_claim(tmp_path):
    db_path = str(tmp_path / "unclaimed.sqlite")
    with closing(get_connection(db_path)) as conn:
        init_schema(conn)
        repo.record_test_snapshot(conn, SNAPSHOT, 12, f"test-snapshots/{SNAPSHOT}.tar")
        _create(conn, "request-unclaimed")
    app = create_app(db_path, clock=lambda: NOW)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/test-requests")
        assert response.status_code == 200, response.text
        assert response.json()["requests"][0]["jobs"][0]["runner_platform"] is None
