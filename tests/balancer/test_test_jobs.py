from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from balancer import repository as repo
from balancer.db import get_connection, init_schema, write_tx


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "jobs.sqlite")
    with closing(get_connection(path)) as conn:
        init_schema(conn)
    return path


@pytest.fixture
def conn(db_path):
    with closing(get_connection(db_path)) as connection:
        yield connection


def create_request(conn, targets=("linux",), request_id="request-1"):
    repo.record_test_snapshot(conn, "a" * 64, 42, f"test-snapshots/{'a' * 64}.tar")
    return repo.create_test_request(
        conn, request_id, "a" * 64, {"argv": ["pytest", "-q"]}, list(targets),
        None,
    )


def finish_job(conn, job, status, **details):
    return repo.finish_test_job(
        conn, job["job_id"], job["runner_id"], job["lease_token"],
        {"status": status, **details},
    )


def job_events(conn, job_id):
    return [row["state"] for row in conn.execute(
        "SELECT state FROM test_job_events WHERE job_id=? ORDER BY id", (job_id,)
    )]


def test_matrix_creates_one_durable_job_per_target(conn, db_path):
    targets = ["linux", {"labels": ["windows", "gpu"]}]
    request = create_request(conn, targets)
    assert request["state"] == "queued"
    assert [job["target"] for job in request["jobs"]] == targets
    assert [job["state"] for job in request["jobs"]] == ["queued", "queued"]
    assert len({job["job_id"] for job in request["jobs"]}) == 2
    assert all(job_events(conn, job["job_id"]) == ["queued"] for job in request["jobs"])
    with closing(get_connection(db_path)) as reopened:
        init_schema(reopened)
        assert repo.get_test_request(reopened, "request-1") == request
        assert repo.get_test_request(reopened, "missing") is None


def test_request_replay_keeps_original_jobs_and_conflict_preserves_state(conn):
    request = create_request(conn)
    assert create_request(conn) == request
    with pytest.raises(repo.ApiError) as error:
        create_request(conn, ["windows"])
    assert error.value.code == "request_id_conflict"
    assert repo.get_test_request(conn, "request-1") == request
    assert job_events(conn, request["jobs"][0]["job_id"]) == ["queued"]


def test_claim_matches_all_labels_and_omits_session_reference(conn):
    create_request(conn, [{"labels": ["linux", "gpu"]}])
    assert repo.claim_test_job(conn, "runner", ["linux"], 60) is None
    job = repo.claim_test_job(conn, "runner", ["gpu", "linux", "extra"], 60)
    assert job["state"] == "preparing"
    assert job["snapshot_sha256"] == "a" * 64
    assert job["profile"] == {"argv": ["pytest", "-q"]}
    assert "session_ref" not in job
    assert "lease_token" not in repo.get_test_request(conn, "request-1")["jobs"][0]


def test_only_one_runner_can_claim_a_job(conn, db_path):
    request = create_request(conn)
    barrier = Barrier(2)

    def claim(runner_id):
        with closing(get_connection(db_path)) as connection:
            barrier.wait(timeout=5)
            return repo.claim_test_job(connection, runner_id, ["linux"], 60)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = [job for job in pool.map(claim, ["runner-a", "runner-b"]) if job]
    assert [job["job_id"] for job in claimed] == [request["jobs"][0]["job_id"]]
    assert job_events(conn, claimed[0]["job_id"]) == ["queued", "preparing"]


def test_runner_holds_one_slot_until_finished(conn):
    create_request(conn)
    create_request(conn, request_id="request-2")
    job = repo.claim_test_job(conn, "runner", ["linux"], 60)
    replay = repo.claim_test_job(conn, "runner", ["linux"], 60)
    assert replay["job_id"] == job["job_id"]
    assert replay["lease_token"] == job["lease_token"]
    assert finish_job(conn, job, "succeeded") == "succeeded"
    assert repo.claim_test_job(conn, "runner", ["linux"], 60)["request_id"] == "request-2"


def move_clock(monkeypatch, now):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(repo, "datetime", Clock)


def test_heartbeat_renews_lease_and_marks_preparation_running(conn, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    move_clock(monkeypatch, now)
    create_request(conn)
    job = repo.claim_test_job(conn, "runner", ["linux"], 60)
    move_clock(monkeypatch, now + timedelta(seconds=30))
    assert repo.heartbeat_test_job(conn, job["job_id"], "runner", job["lease_token"])
    request = repo.get_test_request(conn, "request-1")
    assert request["state"] == "running"
    assert request["jobs"][0]["state"] == "running"
    assert request["jobs"][0]["lease_expires_at"] == "2026-01-01T00:01:30.000000Z"
    assert job_events(conn, job["job_id"]) == ["queued", "preparing", "running"]


@pytest.mark.parametrize("runner, stale_token", [("another-runner", False), ("runner", True)])
def test_wrong_runner_or_token_cannot_update_job(conn, runner, stale_token):
    create_request(conn)
    job = repo.claim_test_job(conn, "runner", ["linux"], 60)
    token = "stale-token" if stale_token else job["lease_token"]
    assert not repo.heartbeat_test_job(conn, job["job_id"], runner, token)
    with pytest.raises(repo.ApiError) as error:
        repo.finish_test_job(conn, job["job_id"], runner, token, {"status": "succeeded"})
    assert error.value.code == "owner_mismatch"
    assert repo.get_test_request(conn, "request-1")["jobs"][0]["state"] == "preparing"
    assert job_events(conn, job["job_id"]) == ["queued", "preparing"]


def test_identical_finish_replays_without_another_event(conn):
    create_request(conn)
    job = repo.claim_test_job(conn, "runner", ["linux"], 60)
    assert finish_job(conn, job, "succeeded", exit_code=0) == "succeeded"
    assert finish_job(conn, job, "succeeded", exit_code=0) == "succeeded"
    with pytest.raises(repo.ApiError) as error:
        finish_job(conn, job, "failed", exit_code=1)
    assert error.value.code == "result_conflict"
    assert job_events(conn, job["job_id"]) == ["queued", "preparing", "succeeded"]
    assert repo.get_test_request(conn, "request-1")["jobs"][0]["result"] == {
        "status": "succeeded", "exit_code": 0
    }


@pytest.mark.parametrize("outcomes, aggregate", [
    (("succeeded", "succeeded"), "succeeded"),
    (("failed", "failed"), "failed"),
    (("succeeded", "failed"), "partial"),
    (("cancelled", "cancelled"), "cancelled"),
])
def test_request_state_derives_from_children(conn, outcomes, aggregate):
    create_request(conn, ["linux", "windows"])
    for label, status in zip(["linux", "windows"], outcomes):
        job = repo.claim_test_job(conn, "runner", [label], 60)
        assert repo.get_test_request(conn, "request-1")["state"] == "running"
        assert finish_job(conn, job, status) == status
    assert repo.get_test_request(conn, "request-1")["state"] == aggregate


@pytest.mark.parametrize("timed_out, expected_state", [(True, "partial"), (False, "failed")])
def test_timeout_result_state(conn, timed_out, expected_state):
    create_request(conn)
    job = repo.claim_test_job(conn, "runner", ["linux"], 60)
    assert finish_job(conn, job, "failed", timed_out=timed_out) == "failed"
    assert repo.get_test_request(conn, "request-1")["state"] == expected_state


@pytest.mark.parametrize("status, timed_out", [
    ("failed", "false"),
    ("failed", None),
    ("failed", 1),
    ("succeeded", True),
    ("cancelled", True),
    ("lost", True),
])
def test_invalid_timeout_result_is_rejected_before_persisting(conn, status, timed_out):
    create_request(conn)
    job = repo.claim_test_job(conn, "runner", ["linux"], 60)
    with pytest.raises(repo.ApiError) as error:
        finish_job(conn, job, status, timed_out=timed_out)
    assert (error.value.status, error.value.code) == (422, "invalid_result")
    recorded = repo.get_test_request(conn, "request-1")["jobs"][0]
    assert recorded["state"] == "preparing"
    assert recorded["result"] is None
    assert job_events(conn, job["job_id"]) == ["queued", "preparing"]


def test_expired_lease_stays_unresolved_across_reopen(conn, db_path, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    move_clock(monkeypatch, now)
    create_request(conn)
    job = repo.claim_test_job(conn, "runner", ["linux"], 60)
    move_clock(monkeypatch, now + timedelta(seconds=60))
    with closing(get_connection(db_path)) as reopened:
        with pytest.raises(repo.ApiError) as error:
            finish_job(reopened, job, "succeeded")
        assert error.value.code == "lease_expired"
        request = repo.get_test_request(reopened, "request-1")
        assert request["state"] == "lost"
        assert request["jobs"][0]["state"] == "lost"
        assert request["jobs"][0]["runner_id"] == "runner"
        assert request["jobs"][0]["result"] is None
        with pytest.raises(repo.ApiError) as heartbeat_error:
            repo.heartbeat_test_job(reopened, job["job_id"], "runner", job["lease_token"])
        assert (heartbeat_error.value.status, heartbeat_error.value.code) == (409, "lease_lost")
        assert repo.claim_test_job(reopened, "another-runner", ["linux"], 60) is None
        create_request(reopened, request_id="request-2")
        assert repo.claim_test_job(reopened, "runner", ["linux"], 60) is None
        assert job_events(reopened, job["job_id"]) == ["queued", "preparing", "lost"]


def test_create_request_requires_registered_snapshot(conn):
    with pytest.raises(repo.ApiError) as error:
        repo.create_test_request(conn, "missing-snapshot", "b" * 64, {}, ["linux"], None)
    assert (error.value.status, error.value.code) == (409, "snapshot_missing")
    snapshot = repo.record_test_snapshot(conn, "b" * 64, 123, "test-snapshots/b.tar")
    assert snapshot == {
        "sha256": "b" * 64, "byte_count": 123, "storage_key": "test-snapshots/b.tar"
    }
    assert repo.get_test_snapshot(conn, "b" * 64) == snapshot
    assert repo.record_test_snapshot(conn, "b" * 64, 123, "test-snapshots/b.tar") == snapshot
    assert repo.create_test_request(conn, "present-snapshot", "b" * 64, {}, ["linux"], None)["state"] == "queued"


def test_runner_registration_stores_hash_only_and_rotation_invalidates_old_hash(conn):
    import hashlib

    token = "a2r_first_secret"
    first = repo.register_test_runner(
        conn, "win-host", ["windows", "gpu"], "windows",
        hashlib.sha256(token.encode()).hexdigest(),
    )
    assert first["runner_id"] == "win-host"
    assert first["labels"] == ["windows", "gpu"]
    assert first["platform"] == "windows"
    assert "token" not in first and token not in repr(first)
    stored = conn.execute("SELECT token_sha256 FROM test_runners WHERE runner_id='win-host'").fetchone()[0]
    assert stored == hashlib.sha256(token.encode()).hexdigest()
    assert repo.authenticate_test_runner(conn, "win-host", stored)["runner_id"] == "win-host"
    assert repo.authenticate_test_runner(conn, "win-host", hashlib.sha256(b"wrong").hexdigest()) is None
    with pytest.raises(repo.ApiError) as error:
        repo.register_test_runner(
            conn, "win-host", ["windows"], "windows", hashlib.sha256(b"second").hexdigest()
        )
    assert error.value.code == "runner_exists"
    second = repo.register_test_runner(
        conn, "win-host", ["windows"], "windows", hashlib.sha256(b"second").hexdigest(),
        rotate=True,
    )
    assert second["labels"] == ["windows"]
    assert repo.authenticate_test_runner(conn, "win-host", stored) is None
    assert repo.authenticate_test_runner(conn, "win-host", hashlib.sha256(b"second").hexdigest())["labels"] == ["windows"]


def test_claim_replays_only_the_same_preparing_lease(conn, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    move_clock(monkeypatch, now)
    request = create_request(conn)
    job = repo.claim_test_job(conn, "runner", ["linux"], 60)
    replay = repo.claim_test_job(conn, "runner", ["linux"], 60)
    assert replay["job_id"] == job["job_id"]
    assert replay["lease_token"] == job["lease_token"]
    assert replay["lease_expires_at"] == job["lease_expires_at"]
    assert job_events(conn, job["job_id"]) == ["queued", "preparing"]
    assert repo.heartbeat_test_job(conn, job["job_id"], "runner", job["lease_token"], phase="running")
    assert repo.claim_test_job(conn, "runner", ["linux"], 60) is None


def test_phased_heartbeat_tracks_log_offsets_and_rejects_gaps_and_conflicts(conn):
    create_request(conn)
    job = repo.claim_test_job(conn, "runner", ["linux"], 60)
    first = repo.heartbeat_test_job(
        conn, job["job_id"], "runner", job["lease_token"],
        phase="preparing", log_offset=0, log_chunk="hello", log_limit=3,
    )
    assert first["state"] == "preparing"
    assert first["next_log_offset"] == 5
    assert first["log_stored_bytes"] == 3
    assert first["truncate_to"] == 0
    assert first["append_bytes"] == b"hel"
    assert first["log_truncated"] is True
    assert first["cancel_requested"] is False
    replay = repo.heartbeat_test_job(
        conn, job["job_id"], "runner", job["lease_token"],
        phase="preparing", log_offset=0, log_chunk="hello", log_limit=3,
    )
    assert replay["next_log_offset"] == 5
    assert replay["append_bytes"] == b""
    with pytest.raises(repo.ApiError) as conflict:
        repo.heartbeat_test_job(
            conn, job["job_id"], "runner", job["lease_token"],
            phase="preparing", log_offset=0, log_chunk="hullo", log_limit=3,
        )
    assert conflict.value.code == "log_offset_conflict"
    with pytest.raises(repo.ApiError) as gap:
        repo.heartbeat_test_job(
            conn, job["job_id"], "runner", job["lease_token"],
            phase="preparing", log_offset=8, log_chunk="!", log_limit=3,
        )
    assert gap.value.code == "log_offset_gap"
    assert repo.heartbeat_test_job(
        conn, job["job_id"], "runner", job["lease_token"],
        phase="running", log_offset=5, log_chunk="world", log_limit=3,
    )["next_log_offset"] == 10
    assert repo.heartbeat_test_job(
        conn, job["job_id"], "runner", job["lease_token"],
        phase="running", log_offset=5, log_chunk="world", log_limit=3,
    )["append_bytes"] == b""
    assert job_events(conn, job["job_id"]) == ["queued", "preparing", "running"]


def test_heartbeat_preserves_binary_log_chunks_across_utf8_boundaries(conn):
    create_request(conn)
    job = repo.claim_test_job(conn, "runner", ["linux"], 60)
    leading = b"\xe2\x82"
    first = repo.heartbeat_test_job(
        conn, job["job_id"], "runner", job["lease_token"],
        phase="preparing", log_offset=0, log_chunk=leading,
    )
    assert first["append_bytes"] == leading
    assert first["next_log_offset"] == len(leading)
    final = b"\xac"
    second = repo.heartbeat_test_job(
        conn, job["job_id"], "runner", job["lease_token"],
        phase="preparing", log_offset=len(leading), log_chunk=final,
    )
    assert second["append_bytes"] == final
    assert second["next_log_offset"] == 3
    empty = repo.heartbeat_test_job(
        conn, job["job_id"], "runner", job["lease_token"],
        phase="preparing", log_offset=second["next_log_offset"], log_chunk=b"",
    )
    assert empty["append_bytes"] == b""
    assert empty["next_log_offset"] == 3


def test_expired_heartbeat_commits_lost_transition_when_caller_owns_transaction(conn, db_path, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    move_clock(monkeypatch, now)
    create_request(conn)
    job = repo.claim_test_job(conn, "runner", ["linux"], 10)
    move_clock(monkeypatch, now + timedelta(seconds=10))
    with pytest.raises(repo.ApiError) as error:
        with write_tx(db_path) as transaction:
            repo.heartbeat_test_job(
                transaction, job["job_id"], "runner", job["lease_token"], phase="running",
            )
    assert (error.value.status, error.value.code, error.value.commit) == (409, "lease_lost", True)
    with closing(get_connection(db_path)) as reopened:
        assert repo.get_test_request(reopened, "request-1")["jobs"][0]["state"] == "lost"


def test_heartbeat_reports_cancel_and_rejects_running_to_preparing(conn):
    create_request(conn)
    job = repo.claim_test_job(conn, "runner", ["linux"], 60)
    assert repo.heartbeat_test_job(conn, job["job_id"], "runner", job["lease_token"], phase="running")
    with pytest.raises(repo.ApiError) as error:
        repo.heartbeat_test_job(conn, job["job_id"], "runner", job["lease_token"], phase="preparing")
    assert error.value.code == "invalid_phase"
    view = repo.cancel_test_request(conn, "request-1")
    assert view["cancel_requested_at"] is not None
    assert view["jobs"][0]["cancel_requested"] is True
    pulse = repo.heartbeat_test_job(conn, job["job_id"], "runner", job["lease_token"], phase="running")
    assert pulse["cancel_requested"] is True


def test_cancel_request_terminates_queued_children_and_is_idempotent(conn):
    request = create_request(conn, ["linux", "windows"])
    first = request["jobs"][0]
    cancelled = repo.cancel_test_request(conn, "request-1")
    assert cancelled["state"] == "cancelled"
    assert [j["state"] for j in cancelled["jobs"]] == ["cancelled", "cancelled"]
    assert all(j["result"] == {"reason": "cancelled_before_start", "status": "cancelled"}
               for j in cancelled["jobs"])
    assert repo.cancel_test_request(conn, "request-1") == cancelled
    assert job_events(conn, first["job_id"]) == ["queued", "cancelled"]
    with pytest.raises(repo.ApiError) as missing:
        repo.cancel_test_request(conn, "missing")
    assert (missing.value.status, missing.value.code) == (404, "not_found")


def test_repeated_cancel_does_not_change_active_job_view(conn, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    move_clock(monkeypatch, now)
    create_request(conn)
    job = repo.claim_test_job(conn, "runner", ["linux"], 60)
    repo.heartbeat_test_job(conn, job["job_id"], "runner", job["lease_token"], phase="running")
    first = repo.cancel_test_request(conn, "request-1")
    move_clock(monkeypatch, now + timedelta(seconds=1))
    replay = repo.cancel_test_request(conn, "request-1")
    assert replay == first


def test_finish_rejects_runner_claimed_lost_state(conn):
    create_request(conn)
    job = repo.claim_test_job(conn, "runner", ["linux"], 60)
    with pytest.raises(repo.ApiError) as error:
        finish_job(conn, job, "lost")
    assert (error.value.status, error.value.code) == (422, "invalid_result")


def test_task3_schema_migrates_existing_task1_job_tables(conn):
    for column in (
        "cancel_requested", "log_received_bytes", "log_stored_bytes", "log_truncated",
        "log_last_chunk_offset", "log_last_chunk_length", "log_last_chunk_sha256",
    ):
        conn.execute(f"ALTER TABLE test_jobs DROP COLUMN {column}")
    conn.execute("ALTER TABLE test_requests DROP COLUMN cancel_requested_at")
    init_schema(conn)
    job_columns = {row[1] for row in conn.execute("PRAGMA table_info(test_jobs)")}
    request_columns = {row[1] for row in conn.execute("PRAGMA table_info(test_requests)")}
    assert {"cancel_requested", "log_received_bytes", "log_stored_bytes", "log_truncated",
            "log_last_chunk_offset", "log_last_chunk_length", "log_last_chunk_sha256"} <= job_columns
    assert "cancel_requested_at" in request_columns
