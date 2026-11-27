"""HTTP layer for background test jobs (docs/superpowers/specs/2026-11-27-background-test-jobs-api.md).

Most tests drive the routes against FakeStore, an in-memory implementation of the
repository surface agreed with the native Codex executor, so they exercise only
the API layer: trust boundary, auth, schemas, streaming files and idempotency.
`test_end_to_end_with_real_repository` runs the same flow against
balancer.repository once that surface exists.
"""

import base64
import hashlib
import hmac
import inspect
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from balancer import repository as repo
from balancer.app import create_app
from balancer.test_jobs_api import classify_peer, is_runner_route

ADMIN = "admin-secret"
TAILNET_PEER = "100.101.102.103"


def ts(dt):
    return repo.ts(dt)


def now():
    return datetime.now(timezone.utc)


class FakeStore:
    """In-memory stand-in for the agreed repository functions (state survives app recreation)."""

    def __init__(self):
        self.runners, self.snapshots, self.requests, self.jobs = {}, {}, {}, {}
        self.heartbeat_calls = []

    @staticmethod
    def _log(conn, job_id):
        conn.execute("CREATE TABLE IF NOT EXISTS fake_logs (job_id TEXT PRIMARY KEY, received INT,"
                     " stored INT, truncated INT, last_offset INT, last_chunk BLOB)")
        row = conn.execute("SELECT received, stored, truncated, last_offset, last_chunk"
                           " FROM fake_logs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return 0, 0, False, None, None
        return row[0], row[1], bool(row[2]), row[3], row[4]

    def _log_fields(self, conn, job_id):
        received, stored, truncated, _, _ = self._log(conn, job_id)
        return {"log_received_bytes": received, "log_stored_bytes": stored,
                "log_truncated": truncated}

    # runners
    def register_test_runner(self, conn, runner_id, labels, platform, token_sha256, rotate=False):
        if runner_id in self.runners and not rotate:
            raise repo.ApiError(409, "runner_exists", "pass rotate:true to replace its token")
        self.runners[runner_id] = {"runner_id": runner_id, "labels": labels, "platform": platform,
                                   "token_sha256": token_sha256}
        return {k: v for k, v in self.runners[runner_id].items() if k != "token_sha256"}

    def authenticate_test_runner(self, conn, runner_id, token_sha256):
        runner = self.runners.get(runner_id)
        if runner and hmac.compare_digest(runner["token_sha256"], token_sha256):
            return {k: v for k, v in runner.items() if k != "token_sha256"}
        return None

    # snapshots
    def record_test_snapshot(self, conn, sha256, byte_count, storage_key):
        self.snapshots[sha256] = {"sha256": sha256, "byte_count": byte_count,
                                  "storage_key": storage_key}
        return self.snapshots[sha256]

    def get_test_snapshot(self, conn, sha256):
        return self.snapshots.get(sha256)

    # requests
    def _expire(self):
        for job in self.jobs.values():
            if job["state"] in ("preparing", "running") and job["lease_expires_at"] <= ts(now()):
                job["state"] = "lost"

    def _view(self, conn, request_id):
        req = self.requests[request_id]
        jobs = [{**self.jobs[j], **self._log_fields(conn, j),
                 "runner_platform": (self.runners.get(self.jobs[j]["runner_id"] or "") or {}).get("platform")}
                for j in req["job_ids"]]
        states = {j["state"] for j in jobs}
        if "lost" in states:
            state = "lost"
        elif states == {"queued"}:
            state = "queued"
        elif states & {"queued", "preparing", "running"}:
            state = "running"
        elif len(states) > 1:
            state = "partial"
        else:
            state = next(iter(states))
        return {"request_id": request_id, "snapshot_sha256": req["snapshot_sha256"],
                "profile": req["profile"], "session_ref": req["session_ref"],
                "targets": [j["target"] for j in jobs], "state": state,
                "cancel_requested_at": req["cancel_requested_at"],  # as the repository reports it
                "jobs": [{k: v for k, v in j.items() if k != "lease_token"} for j in jobs],
                "created_at": req["created_at"], "updated_at": req["created_at"]}

    def create_test_request(self, conn, request_id, snapshot_sha256, profile, targets, session_ref):
        if snapshot_sha256 not in self.snapshots:
            raise repo.ApiError(409, "snapshot_missing", "missing snapshot")
        key = repr((snapshot_sha256, profile, targets, session_ref))
        if request_id in self.requests:
            if self.requests[request_id]["hash"] != key:
                raise repo.ApiError(409, "request_id_conflict", "different body")
            return self._view(conn, request_id)
        created = ts(now())
        job_ids = []
        for target in targets:
            job_id = f"tj_{uuid.uuid4().hex}"
            job_ids.append(job_id)
            self.jobs[job_id] = {"job_id": job_id, "request_id": request_id, "target": target,
                                 "state": "queued", "runner_id": None, "lease_token": None,
                                 "lease_expires_at": None, "cancel_requested": False,
                                 "result": None, "created_at": created,
                                 "updated_at": created}
        self.requests[request_id] = {"hash": key, "snapshot_sha256": snapshot_sha256,
                                     "profile": profile, "session_ref": session_ref,
                                     "job_ids": job_ids, "cancel_requested_at": None,
                                     "created_at": created}
        return self._view(conn, request_id)

    def get_test_request(self, conn, request_id):
        self._expire()
        return self._view(conn, request_id) if request_id in self.requests else None

    def list_test_requests(self, conn, limit, now=None):
        self._expire()
        ids = sorted(self.requests, key=lambda key: self._view(conn, key)["updated_at"], reverse=True)
        return [self._view(conn, request_id) for request_id in ids[:limit]]

    def cancel_test_request(self, conn, request_id):
        req = self.requests[request_id]
        req["cancel_requested_at"] = req["cancel_requested_at"] or ts(now())
        for job_id in self.requests[request_id]["job_ids"]:
            job = self.jobs[job_id]
            if job["state"] == "queued":
                job["state"] = "cancelled"
            elif job["state"] in ("preparing", "running"):
                job["cancel_requested"] = True
        return self._view(conn, request_id)

    # runner side
    def claim_test_job(self, conn, runner_id, labels, lease_s):
        self._expire()
        mine = [j for j in self.jobs.values() if j["runner_id"] == runner_id
                and j["state"] in ("preparing", "running", "lost")]
        if mine:
            job = mine[0]
            return self._claimed(job) if job["state"] == "preparing" else None
        for job in self.jobs.values():
            want = [job["target"]] if isinstance(job["target"], str) else job["target"]["labels"]
            if job["state"] == "queued" and set(want) <= set(labels):
                job.update(state="preparing", runner_id=runner_id, lease_token=uuid.uuid4().hex,
                           lease_expires_at=ts(now() + timedelta(seconds=lease_s)))
                return self._claimed(job)
        return None

    def _claimed(self, job):
        req = self.requests[job["request_id"]]
        return {**job, "snapshot_sha256": req["snapshot_sha256"], "profile": req["profile"],
                "session_ref": req["session_ref"]}

    def heartbeat_test_job(self, conn, job_id, runner_id, lease_token, *, phase, log_offset,
                           log_chunk, log_limit):
        """Mirrors balancer.repository: offsets in the DB, file write plan in the reply."""
        assert isinstance(log_chunk, bytes) and type(log_offset) is int
        self.heartbeat_calls.append((phase, log_offset, log_chunk))
        self._expire()
        job = self.jobs.get(job_id)
        if not job or job["runner_id"] != runner_id or job["lease_token"] != lease_token \
                or job["state"] not in ("preparing", "running"):
            return False
        received, stored, truncated, last_offset, last_chunk = self._log(conn, job_id)
        truncate_to, append = stored, b""
        if log_chunk:
            if log_offset == received:
                append = log_chunk[:max(0, log_limit - stored)]
                received, stored = received + len(log_chunk), stored + len(append)
                truncated = truncated or len(append) < len(log_chunk)
                last_offset, last_chunk = log_offset, log_chunk
            elif (log_offset, log_chunk) != (last_offset, last_chunk):
                code = "log_offset_gap" if log_offset > received else "log_offset_conflict"
                raise repo.ApiError(409, code, "bad log offset")
        elif log_offset != received:
            raise repo.ApiError(409, "log_offset_conflict", "empty heartbeat at a stale offset")
        # Log counters live in SQLite so they commit or roll back with the API's transaction.
        conn.execute("INSERT OR REPLACE INTO fake_logs VALUES (?, ?, ?, ?, ?, ?)",
                     (job_id, received, stored, int(truncated), last_offset, last_chunk))
        job["state"] = phase
        job["lease_expires_at"] = ts(now() + timedelta(seconds=60))
        return {"state": phase, "lease_expires_at": job["lease_expires_at"],
                "next_log_offset": received, "log_stored_bytes": stored,
                "log_truncated": truncated, "cancel_requested": job["cancel_requested"],
                "truncate_to": truncate_to, "append_bytes": append}

    def finish_test_job(self, conn, job_id, runner_id, lease_token, result):
        job = self.jobs.get(job_id)
        if job is None:
            raise repo.ApiError(404, "not_found", "no job")
        if job["runner_id"] != runner_id or job["lease_token"] != lease_token:
            raise repo.ApiError(409, "owner_mismatch", "not the lease owner")
        if job["result"] == result:
            return job["state"]
        if job["state"] not in ("preparing", "running"):
            raise repo.ApiError(409, "result_conflict", "already finished")
        job.update(state=result["status"], result=result)
        return job["state"]

    def get_test_job_lease(self, conn, job_id):
        job = self.jobs.get(job_id)
        if job is None:
            return None
        return {**{k: job[k] for k in ("job_id", "request_id", "runner_id", "lease_token", "state",
                                       "lease_expires_at")},
                "snapshot_sha256": self.requests[job["request_id"]]["snapshot_sha256"],
                **self._log_fields(conn, job_id)}


LOG_LIMIT = 64


def with_peer(app, host):
    async def wrapped(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "client": (host, 50000)}
        await app(scope, receive, send)
    return wrapped


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("A2A_LINK_RUNNER_REGISTRATION_TOKEN", ADMIN)
    monkeypatch.setenv("A2A_LINK_SNAPSHOT_MAX_BYTES", "4096")
    monkeypatch.setenv("A2A_LINK_TEST_LOG_MAX_BYTES", str(LOG_LIMIT))


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def make_app(tmp_path, env, store):
    def make(peer="127.0.0.1", **kw):
        app = create_app(str(tmp_path / "api.db"), test_job_store=kw.pop("store", store), **kw)
        return TestClient(with_peer(app, peer), base_url="http://127.0.0.1")
    return make


@pytest.fixture
def local(make_app):
    return make_app()


@pytest.fixture
def remote(make_app):
    return make_app(TAILNET_PEER)


def register(client, runner_id="runner-1", labels=("linux",), rotate=False):
    r = client.post("/runners/register", headers={"Authorization": f"Bearer {ADMIN}"},
                    json={"runner_id": runner_id, "labels": list(labels), "platform": "linux",
                          "rotate": rotate})
    assert r.status_code in (200, 201), r.text
    return r.json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}


TAR = b"snapshot-bytes" * 10
TAR_SHA = hashlib.sha256(TAR).hexdigest()


def upload(client, data=TAR, sha=None):
    return client.put(f"/test-snapshots/{sha or hashlib.sha256(data).hexdigest()}", content=data)


def create(client, request_id="req-1", targets=("linux",), **extra):
    upload(client)
    body = {"request_id": request_id, "snapshot_sha256": TAR_SHA,
            "profile": "python-pytest", "targets": list(targets),
            "session_ref": {"adapter": "claude", "session": "s-1"}, **extra}
    return client.post("/test-requests", json=body)


def claim(client, token, **body):
    r = client.post("/runners/claim", headers=auth(token), json=body or None)
    assert r.status_code == 200, r.text
    return r.json()["job"]


def heartbeat(client, token, job, phase="running", runner_id="runner-1", offset=None, chunk=None):
    body = {"job_id": job["job_id"], "lease_token": job["lease_token"], "phase": phase}
    if chunk is not None:
        body |= {"log_offset": offset, "log_chunk": base64.b64encode(chunk).decode()}
    return client.post(f"/runners/{runner_id}/heartbeat", headers=auth(token), json=body)


def started(local, remote, targets=("linux",), labels=("linux",)):
    token = register(local, labels=labels)
    assert create(local, targets=targets).status_code == 201
    job = claim(remote, token)
    assert heartbeat(remote, token, job).status_code == 200
    return token, job


# --- trust boundary ---------------------------------------------------------------

@pytest.mark.parametrize("host, kind", [
    ("127.0.0.1", "loopback"), ("127.8.9.1", "loopback"), ("::1", "loopback"),
    ("::ffff:127.0.0.1", "loopback"), ("100.64.0.1", "tailnet"), ("100.127.255.254", "tailnet"),
    ("fd7a:115c:a1e0::1", "tailnet"), ("100.128.0.1", "other"), ("192.168.1.5", "other"),
    ("8.8.8.8", "other"), (None, "other"), ("localhost", "other"), ("testclient", "loopback"),
])
def test_classify_peer(host, kind):
    assert classify_peer(host) == kind


@pytest.mark.parametrize("path, runner", [
    ("/runners/claim", True), ("/runners/r1/heartbeat", True), ("/test-jobs/tj_1/snapshot", True),
    ("/test-jobs/tj_1/result", True), ("/runners/register", False), ("/test-requests", False),
    ("/test-snapshots/abc", False), ("/tasks", False), ("/runners/a/b/heartbeat", False),
])
def test_runner_route_patterns(path, runner):
    assert is_runner_route(path) is runner


@pytest.mark.parametrize("method, path", [
    ("get", "/tasks"), ("get", "/issues/x"), ("get", "/dashboard"), ("get", "/openapi.json"),
    ("post", "/test-requests"), ("get", "/test-requests/req-1"),
    ("post", "/runners/register"), ("put", f"/test-snapshots/{TAR_SHA}"),
])
def test_local_routes_reject_tailnet_and_public_peers(make_app, method, path):
    for peer in (TAILNET_PEER, "203.0.113.9"):
        r = getattr(make_app(peer), method)(path)
        assert r.status_code == 403 and r.json()["error"] == "forbidden_origin", (peer, path)


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Real-IP",
                                    "Tailscale-User-Login"])
def test_local_routes_reject_proxied_loopback_requests(local, header):
    r = local.get("/tasks", headers={header: "100.64.0.9"})
    assert r.status_code == 403


def test_runner_routes_reachable_from_tailnet_but_need_a_token(make_app):
    tailnet, public = make_app(TAILNET_PEER), make_app("203.0.113.9")
    assert tailnet.post("/runners/claim").status_code == 401
    assert tailnet.post("/runners/claim", headers=auth("runner-1.bogus")).status_code == 401
    assert public.post("/runners/claim").status_code == 403
    proxied = make_app().post("/runners/claim", headers={"Tailscale-User-Login": "x"})
    assert proxied.status_code == 401  # tailscale serve → loopback: allowed, still authenticated


def test_existing_routes_still_work_locally(local):
    assert local.post("/tasks", json={"description": "x"}).status_code == 201


@pytest.mark.parametrize("host", ["attacker.example", "localhost.attacker.example",
                                   "127.0.0.1.attacker.example", "127.0.0.1.evil",
                                   "localhost.", "user@localhost", "localhost:99999"])
def test_local_non_runner_routes_reject_untrusted_host(local, host):
    response = local.get("/test-requests?limit=1", headers={"Host": host})
    assert response.status_code == 403
    assert response.json()["error"] == "forbidden_origin"


@pytest.mark.parametrize("origin", ["https://127.0.0.1", "http://localhost:9000",
                                    "http://attacker.example", "null", "not-an-origin"])
def test_local_state_changes_reject_cross_origin_or_invalid_origin(local, origin):
    response = local.post("/tasks", headers={"Host": "localhost:8000", "Origin": origin},
                          json={"description": "x"})
    assert response.status_code == 403
    assert response.json()["error"] == "forbidden_origin"


def test_local_state_changes_allow_absent_or_same_origin(local):
    absent = local.post("/tasks", headers={"Host": "127.0.0.1:8000"},
                        json={"description": "cli"})
    same = local.post("/tasks", headers={"Host": "localhost:8000",
                                         "Origin": "http://localhost:8000"},
                      json={"description": "browser"})
    assert absent.status_code == 201
    assert same.status_code == 201


def test_testclient_peer_alone_allows_testserver_host(make_app):
    response = make_app("testclient").get("/test-requests?limit=1", headers={"Host": "testserver"})
    assert response.status_code == 200


def test_testclient_same_origin_write_is_allowed(make_app):
    client = make_app("testclient")
    response = client.post("/tasks", headers={"Host": "testserver", "Origin": "http://testserver"},
                           json={"description": "testclient"})
    assert response.status_code == 201


def test_request_list_is_bounded_and_contains_platform_and_log_metadata(local, remote):
    token = register(local)
    assert create(local, request_id="list-a").status_code == 201
    assert create(local, request_id="list-b").status_code == 201
    job = claim(remote, token)
    assert heartbeat(remote, token, job, offset=0, chunk=b"sample").status_code == 200

    response = local.get("/test-requests?limit=1")
    assert response.status_code == 200, response.text
    assert set(response.json()) == {"requests"}
    request = response.json()["requests"][0]
    assert request["request_id"] == "list-b"
    assert request["jobs"][0]["runner_platform"] is None
    assert request["jobs"][0]["log_stored_bytes"] == 0
    assert request["jobs"][0]["log_truncated"] is False
    assert all("lease_token" not in child for child in request["jobs"])

    detail = local.get("/test-requests/list-a").json()
    assert detail["jobs"][0]["runner_platform"] == "linux"
    assert detail["jobs"][0]["log_stored_bytes"] == len(b"sample")


# --- registration --------------------------------------------------------------------

def test_registration_fails_closed_without_admin_token(local, monkeypatch):
    monkeypatch.delenv("A2A_LINK_RUNNER_REGISTRATION_TOKEN")
    r = local.post("/runners/register", headers={"Authorization": "Bearer "},
                   json={"runner_id": "r", "labels": ["linux"], "platform": "linux"})
    assert (r.status_code, r.json()["error"]) == (503, "registration_disabled")


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"},
                                     {"Authorization": f"Basic {ADMIN}"}])
def test_registration_requires_admin_bearer(local, headers):
    r = local.post("/runners/register", headers=headers,
                   json={"runner_id": "r", "labels": ["linux"], "platform": "linux"})
    assert r.status_code == 401


def test_registration_returns_token_once_and_stores_only_its_hash(local, store):
    token = register(local)
    assert token.startswith("runner-1.")
    stored = store.runners["runner-1"]
    assert stored["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in repr(stored)
    r = local.post("/runners/register", headers=auth(ADMIN),
                   json={"runner_id": "runner-1", "labels": ["linux"], "platform": "linux"})
    assert (r.status_code, r.json()["error"]) == (409, "runner_exists")
    assert "token" not in r.json()


def test_rotation_invalidates_old_token(local, remote):
    old = register(local)
    new = register(local, rotate=True)
    assert new != old
    assert remote.post("/runners/claim", headers=auth(old)).status_code == 401
    assert remote.post("/runners/claim", headers=auth(new)).status_code == 200


@pytest.mark.parametrize("body", [
    {"runner_id": "a.b", "labels": ["linux"], "platform": "linux"},
    {"runner_id": "r", "labels": [], "platform": "linux"},
    {"runner_id": "r", "labels": ["linux"], "platform": "linux", "admin": True},
])
def test_registration_schema_is_strict(local, body):
    r = local.post("/runners/register", headers=auth(ADMIN), json=body)
    assert (r.status_code, r.json()["error"]) == (422, "validation_error")


# --- snapshot upload ------------------------------------------------------------------

def test_upload_publishes_once_and_replays(local, store, env):
    from balancer import tasklog
    first, again = upload(local), upload(local)
    assert (first.status_code, first.json()["created"]) == (201, True)
    assert (again.status_code, again.json()["created"]) == (200, False)
    path = tasklog.home() / "test-snapshots" / f"{TAR_SHA}.tar"
    assert path.read_bytes() == TAR
    assert store.snapshots[TAR_SHA]["byte_count"] == len(TAR)
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_upload_digest_mismatch_and_oversize_leave_nothing(local, store):
    from balancer import tasklog
    r = upload(local, TAR, sha="0" * 64)
    assert (r.status_code, r.json()["error"]) == (422, "digest_mismatch")
    big = b"x" * 5000

    def chunks():
        for i in range(0, len(big), 1000):
            yield big[i:i + 1000]

    r = local.put(f"/test-snapshots/{hashlib.sha256(big).hexdigest()}", content=chunks())
    assert (r.status_code, r.json()["error"]) == (413, "snapshot_too_large")
    r = upload(local, big)  # declared Content-Length is rejected before streaming
    assert r.status_code == 413
    assert store.snapshots == {}
    assert list((tasklog.home() / "test-snapshots").iterdir()) == []


@pytest.mark.parametrize("sha", ["ABC", "g" * 64, TAR_SHA.upper()])
def test_upload_rejects_bad_digest_path(local, sha):
    assert upload(local, sha=sha).status_code == 422


# --- requests ----------------------------------------------------------------------

def test_request_create_replay_and_conflict(local):
    first = create(local)
    assert first.status_code == 201
    assert first.json()["state"] == "queued"
    replay = create(local)
    assert (replay.status_code, replay.json()) == (200, first.json())
    conflict = create(local, targets=["windows"])
    assert (conflict.status_code, conflict.json()["error"]) == (409, "request_id_conflict")


def test_request_requires_existing_snapshot(local):
    r = local.post("/test-requests", json={"request_id": "r", "snapshot_sha256": "1" * 64,
                                           "profile": "default", "targets": ["linux"],
                                           "session_ref": None})
    assert (r.status_code, r.json()["error"]) == (409, "snapshot_missing")


@pytest.mark.parametrize("damage", ["delete", "resize"])
def test_request_requires_intact_stored_snapshot(local, store, damage):
    from balancer import tasklog
    assert upload(local).status_code == 201
    path = tasklog.home() / "test-snapshots" / f"{TAR_SHA}.tar"
    if damage == "delete":
        path.unlink()
    else:
        path.write_bytes(TAR + b"x")
    r = local.post("/test-requests", json={"request_id": "r", "snapshot_sha256": TAR_SHA,
                                           "profile": "default", "targets": ["linux"],
                                           "session_ref": None})
    assert (r.status_code, r.json()["error"]) == (409, "snapshot_missing")
    assert store.requests == {}


def test_request_requires_registered_snapshot_even_if_file_exists(local, store):
    assert upload(local).status_code == 201
    del store.snapshots[TAR_SHA]  # file on disk, but no metadata row
    r = local.post("/test-requests", json={"request_id": "r2", "snapshot_sha256": TAR_SHA,
                                           "profile": "default", "targets": ["linux"],
                                           "session_ref": None})
    assert (r.status_code, r.json()["error"]) == (409, "snapshot_missing")


@pytest.mark.parametrize("change", [
    {"extra": 1}, {"targets": []}, {"targets": [{"labels": []}]}, {"targets": [{"lbls": ["a"]}]},
    {"snapshot_sha256": "abc"}, {"request_id": "has space"}, {"profile": ""},
])
def test_request_schema_is_strict(local, change):
    upload(local)
    body = {"request_id": "r", "snapshot_sha256": TAR_SHA, "profile": "default",
            "targets": ["linux"], "session_ref": None, **change}
    r = local.post("/test-requests", json=body)
    assert (r.status_code, r.json()["error"]) == (422, "validation_error")


def test_request_rejects_inline_profile_object(local):
    upload(local)
    r = local.post("/test-requests", json={
        "request_id": "inline-profile", "snapshot_sha256": TAR_SHA,
        "profile": {"argv": ["python", "-c", "arbitrary model command"]},
        "targets": ["linux"], "session_ref": None,
    })

    assert r.status_code == 422, r.text


def test_session_ref_is_required(local):
    upload(local)
    r = local.post("/test-requests", json={"request_id": "r", "snapshot_sha256": TAR_SHA,
                                           "profile": "default", "targets": ["linux"]})
    assert r.status_code == 422


def test_get_missing_request_is_404(local):
    assert local.get("/test-requests/nope").status_code == 404
    assert local.post("/test-requests/nope/cancel").status_code == 404


def test_invalid_repository_view_is_not_returned(local, store):
    create(local)
    store.requests["req-1"]["profile"] = object()  # not JSON
    r = local.get("/test-requests/req-1")
    assert (r.status_code, r.json()["error"]) == (500, "invalid_response")


# --- claim ---------------------------------------------------------------------------

def test_claim_uses_registered_labels_and_omits_session_ref(local, remote):
    token = register(local, labels=["linux"])
    create(local, targets=["windows"])
    assert claim(remote, token) is None
    r = remote.post("/runners/claim", headers=auth(token), json={"labels": ["windows"]})
    assert r.status_code == 422  # labels are never client claims
    create(local, request_id="req-2", targets=[{"labels": ["linux"]}])
    job = claim(remote, token)
    assert job["request_id"] == "req-2" and job["state"] == "preparing"
    assert set(job) == {"job_id", "request_id", "target", "state", "profile", "snapshot_sha256",
                        "lease_token", "lease_expires_at"}


def test_lost_claim_response_redelivers_same_lease(local, remote):
    token = register(local)
    create(local, targets=["linux", "linux"])
    job = claim(remote, token)
    assert claim(remote, token) == job
    assert heartbeat(remote, token, job).status_code == 200
    assert claim(remote, token) is None  # running blocks further claims


@pytest.mark.parametrize("lease_s", [14, 601, "60"])
def test_claim_lease_range(local, remote, lease_s):
    token = register(local)
    r = remote.post("/runners/claim", headers=auth(token), json={"lease_s": lease_s})
    assert r.status_code == 422


# --- heartbeat and logs ---------------------------------------------------------------

def test_heartbeat_token_must_belong_to_path_runner(local, remote):
    token, job = started(local, remote)
    register(local, runner_id="runner-2")
    r = heartbeat(remote, token, job, runner_id="runner-2")
    assert (r.status_code, r.json()["error"]) == (403, "runner_mismatch")


def test_heartbeat_wrong_lease_is_rejected(local, remote):
    token, job = started(local, remote)
    r = heartbeat(remote, token, {**job, "lease_token": "stale"})
    assert (r.status_code, r.json()["error"]) == (409, "owner_mismatch")


def test_log_chunks_are_offset_idempotent(local, remote, store):
    token, job = started(local, remote)
    r = heartbeat(remote, token, job, offset=0, chunk=b"hello ")
    assert r.json()["next_log_offset"] == 6
    assert heartbeat(remote, token, job, offset=0, chunk=b"hello ").json()["next_log_offset"] == 6
    conflict = heartbeat(remote, token, job, offset=0, chunk=b"HELLO ")
    assert (conflict.status_code, conflict.json()["error"]) == (409, "log_offset_conflict")
    gap = heartbeat(remote, token, job, offset=10, chunk=b"x")
    assert (gap.status_code, gap.json()["error"]) == (409, "log_offset_gap")
    assert heartbeat(remote, token, job, offset=6, chunk="wörld".encode()).status_code == 200
    log = local.get("/test-requests/req-1/log").json()
    assert log["text"] == "hello wörld"
    assert (log["stored_bytes"], log["eof"], log["truncated"], log["state"]) == \
        (12, True, False, "running")
    part = local.get("/test-requests/req-1/log", params={"offset": 6, "limit": 2}).json()
    assert (part["text"], part["next_offset"], part["eof"]) == ("w", 7, False)  # whole chars only


@pytest.mark.parametrize("body", [
    {"log_offset": 0}, {"log_chunk": "aGk="}, {"log_offset": 0, "log_chunk": "not base64!"},
    {"log_offset": -1, "log_chunk": "aGk="}, {"log_offset": 0, "log_chunk": ""},
    {"phase": "succeeded"}, {"extra": True},
])
def test_heartbeat_schema_is_strict(local, remote, body):
    token, job = started(local, remote)
    payload = {"job_id": job["job_id"], "lease_token": job["lease_token"], "phase": "running",
               **body}
    r = remote.post("/runners/runner-1/heartbeat", headers=auth(token), json=payload)
    assert (r.status_code, r.json()["error"]) == (422, "validation_error")


def test_log_keeps_beginning_when_truncated(local, remote):
    token, job = started(local, remote)
    assert heartbeat(remote, token, job, offset=0, chunk=b"a" * 60).status_code == 200
    r = heartbeat(remote, token, job, offset=60, chunk=b"b" * 10).json()
    assert r["log_truncated"] is True
    log = local.get("/test-requests/req-1/log").json()
    assert log["text"] == "a" * 60 + "b" * 4
    assert (log["stored_bytes"], log["truncated"]) == (LOG_LIMIT, True)


def test_multi_job_log_requires_job_id(local, remote):
    token, job = started(local, remote, targets=["linux", "windows"])
    r = local.get("/test-requests/req-1/log")
    assert (r.status_code, r.json()["error"]) == (422, "job_id_required")
    r = local.get("/test-requests/req-1/log", params={"job_id": job["job_id"]})
    assert r.status_code == 200 and r.json()["stored_bytes"] == 0
    assert local.get("/test-requests/req-1/log", params={"job_id": "tj_other"}).status_code == 404
    assert local.get("/test-requests/req-1/log",
                     params={"job_id": job["job_id"], "limit": 0}).status_code == 422


def test_cancel_is_reported_to_runner(local, remote):
    token, job = started(local, remote, targets=["linux", "windows"])
    view = local.post("/test-requests/req-1/cancel").json()
    assert [j["state"] for j in view["jobs"]] == ["running", "cancelled"]
    assert local.post("/test-requests/req-1/cancel").status_code == 200
    assert heartbeat(remote, token, job).json()["cancel_requested"] is True


def test_cancel_requested_at_maps_to_cancel_requested_and_state_is_kept(local, remote):
    token, job = started(local, remote)
    assert local.get("/test-requests/req-1").json()["cancel_requested"] is False
    cancelled = local.post("/test-requests/req-1/cancel").json()
    view = local.get("/test-requests/req-1").json()
    assert cancelled["cancel_requested"] is True and view["cancel_requested"] is True
    assert "cancel_requested_at" not in view
    assert (view["state"], view["jobs"][0]["state"]) == ("running", "running")


def test_heartbeat_calls_repository_with_keyword_bytes(local, remote, store):
    token, job = started(local, remote)
    heartbeat(remote, token, job, offset=0, chunk=b"abc")
    assert heartbeat(remote, token, job).status_code == 200  # empty: acknowledged offset, b""
    assert store.heartbeat_calls == [("running", 0, b""), ("running", 0, b"abc"),
                                     ("running", 3, b"")]


def test_heartbeat_file_failure_rolls_back_and_retry_succeeds(local, remote, store, monkeypatch):
    from balancer import test_jobs_api
    token, job = started(local, remote)
    assert heartbeat(remote, token, job, offset=0, chunk=b"one ").status_code == 200
    real = test_jobs_api._apply_log

    def half_written(path, truncate_to, append):
        real(path, truncate_to, append[:2])  # partial bytes reach disk, then the disk fails
        raise OSError("disk full")

    monkeypatch.setattr(test_jobs_api, "_apply_log", half_written)
    with pytest.raises(OSError):
        heartbeat(remote, token, job, offset=4, chunk=b"two ")
    # The offset was not committed, and the partial bytes are never served.
    log = local.get("/test-requests/req-1/log").json()
    assert (log["text"], log["stored_bytes"]) == ("one ", 4)
    monkeypatch.setattr(test_jobs_api, "_apply_log", real)
    retry = heartbeat(remote, token, job, offset=4, chunk=b"two ")
    assert (retry.status_code, retry.json()["next_log_offset"]) == (200, 8)
    assert local.get("/test-requests/req-1/log").json()["text"] == "one two "


def test_log_read_clamps_to_committed_count_and_recovers_file(local, remote):
    from balancer import tasklog
    token, job = started(local, remote)
    heartbeat(remote, token, job, offset=0, chunk=b"kept")
    path = tasklog.home() / "test-logs" / f"{job['job_id']}.log"
    with open(path, "ab") as f:
        f.write(b"uncommitted")
    log = local.get("/test-requests/req-1/log").json()
    assert (log["text"], log["stored_bytes"], log["eof"]) == ("kept", 4, True)
    assert path.read_bytes() == b"kept"
    assert heartbeat(remote, token, job, offset=4, chunk=b"!").status_code == 200
    assert local.get("/test-requests/req-1/log").json()["text"] == "kept!"


def _log_file(job):
    from balancer import tasklog
    return tasklog.home() / "test-logs" / f"{job['job_id']}.log"


@pytest.mark.parametrize("damage", ["missing", "short"])
def test_heartbeat_fails_on_lost_log_bytes_without_advancing(local, remote, damage):
    token, job = started(local, remote)
    assert heartbeat(remote, token, job, offset=0, chunk=b"committed").status_code == 200
    path = _log_file(job)
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"comm")
    r = heartbeat(remote, token, job, offset=9, chunk=b"more")
    assert (r.status_code, r.json()["error"]) == (500, "log_corrupt")
    assert not path.exists() if damage == "missing" else path.read_bytes() == b"comm"  # no NULs
    retry = heartbeat(remote, token, job, offset=9, chunk=b"more")  # SQLite did not advance
    assert (retry.status_code, retry.json()["error"]) == (500, "log_corrupt")
    assert heartbeat(remote, token, job, offset=13, chunk=b"x").json()["error"] == "log_offset_gap"


def test_first_heartbeat_creates_missing_log(local, remote):
    token, job = started(local, remote)
    _log_file(job).unlink()  # nothing committed yet, so a missing file is legitimate
    assert heartbeat(remote, token, job, offset=0, chunk=b"hi").status_code == 200
    assert _log_file(job).read_bytes() == b"hi"


@pytest.mark.parametrize("damage", ["missing", "short"])
def test_log_read_reports_lost_committed_bytes(local, remote, damage):
    token, job = started(local, remote)
    assert local.get("/test-requests/req-1/log").json()["stored_bytes"] == 0  # no file yet: fine
    heartbeat(remote, token, job, offset=0, chunk=b"committed")
    path = _log_file(job)
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"comm")
    r = local.get("/test-requests/req-1/log")
    assert (r.status_code, r.json()["error"]) == (500, "log_corrupt")


@pytest.mark.parametrize("call", [
    lambda c: c.post("/runners/claim"),
    lambda c: c.post("/runners/claim", headers=auth("runner-1.bogus")),
    lambda c: c.post("/runners/register", json={"runner_id": "r", "labels": ["linux"],
                                                "platform": "linux"}),
])
def test_unauthorized_responses_name_bearer_scheme(local, call):
    r = call(local)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


# --- snapshot download ------------------------------------------------------------------

def test_owner_downloads_snapshot_with_digest_header(local, remote):
    token, job = started(local, remote)
    url = f"/test-jobs/{job['job_id']}/snapshot"
    r = remote.get(url, headers={**auth(token), "X-Lease-Token": job["lease_token"]})
    assert r.status_code == 200
    assert r.content == TAR
    assert r.headers["x-snapshot-sha256"] == TAR_SHA
    assert int(r.headers["content-length"]) == len(TAR)
    assert remote.get(url, headers={**auth(token), "X-Lease-Token": "stale"}).status_code == 409
    assert remote.get(url, headers=auth(token)).status_code == 409
    other = register(local, runner_id="runner-2")
    assert remote.get(url, headers={**auth(other),
                                    "X-Lease-Token": job["lease_token"]}).status_code == 409
    assert remote.get(url, headers={"X-Lease-Token": job["lease_token"]}).status_code == 401


def test_expired_lease_cannot_download(local, remote, store):
    token, job = started(local, remote)
    store.jobs[job["job_id"]]["lease_expires_at"] = ts(now() - timedelta(seconds=1))
    r = remote.get(f"/test-jobs/{job['job_id']}/snapshot",
                   headers={**auth(token), "X-Lease-Token": job["lease_token"]})
    assert (r.status_code, r.json()["error"]) == (409, "lease_inactive")


# --- results --------------------------------------------------------------------------

def post_result(client, token, job, **body):
    return client.post(f"/test-jobs/{job['job_id']}/result", headers=auth(token),
                       json={"lease_token": job["lease_token"], **body})


def test_result_is_idempotent_and_conflicts_rejected(local, remote):
    token, job = started(local, remote)
    ok = post_result(remote, token, job, status="succeeded", exit_code=0, stdout_tail="done")
    assert ok.json() == {"job_id": job["job_id"], "state": "succeeded"}
    again = post_result(remote, token, job, status="succeeded", exit_code=0, stdout_tail="done")
    assert again.status_code == 200
    conflict = post_result(remote, token, job, status="failed", exit_code=1)
    assert (conflict.status_code, conflict.json()["error"]) == (409, "result_conflict")
    view = local.get("/test-requests/req-1").json()
    assert view["state"] == "succeeded"
    assert view["jobs"][0]["result"] == {"status": "succeeded", "exit_code": 0, "timed_out": False,
                                         "stdout_tail": "done", "stderr_tail": ""}


def test_result_preserves_strict_resource_report(local, remote):
    token, job = started(local, remote)
    resources = {"parallelism": 8, "parallel_args_applied": ["-n", "8"],
                 "memory_limit_mb": 1024, "memory_limit_enforced": True}
    r = post_result(remote, token, job, status="succeeded", exit_code=0, resources=resources)
    assert r.status_code == 200, r.text
    view = local.get("/test-requests/req-1").json()
    assert view["jobs"][0]["result"]["resources"] == resources


def test_result_preserves_nested_null_resource_values(local, remote):
    token, job = started(local, remote)
    resources = {"parallelism": 2, "parallel_args_applied": [],
                 "memory_limit_mb": None, "memory_limit_enforced": None}
    r = post_result(remote, token, job, status="succeeded", exit_code=0, resources=resources)
    assert r.status_code == 200, r.text
    view = local.get("/test-requests/req-1").json()
    assert view["jobs"][0]["result"]["resources"] == resources


def test_result_can_report_requested_but_unavailable_memory_cap(local, remote):
    token, job = started(local, remote)
    resources = {"parallelism": 2, "parallel_args_applied": [],
                 "memory_limit_mb": 512, "memory_limit_enforced": False}
    r = post_result(remote, token, job, status="failed", exit_code=1, resources=resources)
    assert r.status_code == 200, r.text
    view = local.get("/test-requests/req-1").json()
    assert view["jobs"][0]["result"]["resources"] == resources


@pytest.mark.parametrize("body", [
    {"status": "lost"}, {"status": "succeeded", "timed_out": True},
    {"status": "failed", "stdout_tail": "x" * (64 * 1024 + 1)}, {"status": "failed", "bonus": 1},
    {"status": "failed", "duration_s": -1},
])
def test_result_schema_is_strict(local, remote, body):
    token, job = started(local, remote)
    r = post_result(remote, token, job, **body)
    assert (r.status_code, r.json()["error"]) == (422, "validation_error")


@pytest.mark.parametrize("resources", [
    {"parallelism": 0, "parallel_args_applied": [], "memory_limit_mb": None,
     "memory_limit_enforced": None},
    {"parallelism": True, "parallel_args_applied": [], "memory_limit_mb": None,
     "memory_limit_enforced": None},
    {"parallelism": 2, "parallel_args_applied": [], "memory_limit_mb": None,
     "memory_limit_enforced": False},
    {"parallelism": 2, "parallel_args_applied": [], "memory_limit_mb": 256,
     "memory_limit_enforced": None},
    {"parallelism": 2, "parallel_args_applied": [], "memory_limit_mb": 256,
     "memory_limit_enforced": 0},
    {"parallelism": 2, "parallel_args_applied": [], "memory_limit_mb": 0,
     "memory_limit_enforced": False},
    {"parallelism": 2, "parallel_args_applied": [], "memory_limit_mb": None,
     "memory_limit_enforced": None, "unexpected": "no"},
    {"parallelism": 2, "parallel_args_applied": "-n 2", "memory_limit_mb": None,
     "memory_limit_enforced": None},
    {"parallelism": 2, "parallel_args_applied": [], "memory_limit_mb": None,
     "memory_limit_enforced": None, "memory_limit_hit": None},
])
def test_resource_report_schema_is_strict(local, remote, resources):
    token, job = started(local, remote)
    r = post_result(remote, token, job, status="succeeded", resources=resources)
    assert (r.status_code, r.json()["error"]) == (422, "validation_error")


def test_matrix_aggregate_and_app_recreation_preserve_state(local, remote, make_app, store):
    token = register(local, labels=["linux", "windows"])
    create(local, targets=["linux", "windows"])
    first = claim(remote, token)
    heartbeat(remote, token, first, offset=0, chunk=b"log")
    post_result(remote, token, first, status="succeeded")
    second = claim(remote, token)
    post_result(remote, token, second, status="failed", exit_code=1)
    assert local.get("/test-requests/req-1").json()["state"] == "partial"

    local2, remote2 = make_app(), make_app(TAILNET_PEER)  # same db, home and store
    assert local2.get("/test-requests/req-1").json()["state"] == "partial"
    assert local2.get("/test-requests/req-1/log",
                      params={"job_id": first["job_id"]}).json()["text"] == "log"
    assert upload(local2).status_code == 200
    assert remote2.post("/runners/claim", headers=auth(token)).status_code == 200


# --- real repository ----------------------------------------------------------------------

REAL_SURFACE = ("register_test_runner", "authenticate_test_runner", "record_test_snapshot",
                "get_test_snapshot", "cancel_test_request")


def _real_repository_ready():
    if not all(hasattr(repo, name) for name in REAL_SURFACE):
        return False
    params = inspect.signature(repo.heartbeat_test_job).parameters
    return all(name in params and params[name].kind is inspect.Parameter.KEYWORD_ONLY
               for name in ("phase", "log_offset", "log_chunk"))


real_repository = pytest.mark.skipif(
    not _real_repository_ready(),
    reason="repository test-job surface from the Codex executor not merged yet")


@real_repository
def test_end_to_end_with_real_repository(tmp_path, env, monkeypatch):
    _end_to_end_real(tmp_path, monkeypatch)


CODEX_REF = "codex/task3-storage"


def _load_codex_modules():
    """Load balancer.db/repository from the Codex branch without touching the checkout."""
    import subprocess
    import sys
    import types
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    modules = {}
    saved = sys.modules.get("balancer.db")
    try:
        for name in ("db", "repository"):
            shown = subprocess.run(["git", "-C", str(root), "show", f"{CODEX_REF}:balancer/{name}.py"],
                                   capture_output=True, timeout=30)
            if shown.returncode != 0:
                return None
            module = types.ModuleType(f"balancer.{name}")
            module.__file__ = f"{CODEX_REF}:balancer/{name}.py"
            if name == "repository":
                sys.modules["balancer.db"] = modules["db"]  # its `from balancer.db import ...`
            exec(compile(shown.stdout, module.__file__, "exec"), module.__dict__)
            modules[name] = module
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        sys.modules["balancer.db"] = saved
    return modules


def test_end_to_end_with_codex_repository_surface(tmp_path, env, monkeypatch):
    """Same flow against the Codex executor's repository before it is merged here."""
    _use_codex_repository(monkeypatch)
    _end_to_end_real(tmp_path, monkeypatch)


def test_expired_heartbeat_commits_lost_with_codex_repository(tmp_path, env, monkeypatch):
    """The repository, not an API precheck, sees the expiry and commits the job as lost."""
    import sqlite3
    _use_codex_repository(monkeypatch)
    db = str(tmp_path / "real.db")
    local = TestClient(with_peer(create_app(db), "127.0.0.1"), base_url="http://127.0.0.1")
    remote = TestClient(with_peer(create_app(db), TAILNET_PEER), base_url="http://127.0.0.1")
    token = register(local)
    upload(local)
    assert create(local).status_code == 201
    job = claim(remote, token)
    assert heartbeat(remote, token, job, offset=0, chunk=b"ok").status_code == 200
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE test_jobs SET lease_expires_at=? WHERE job_id=?",
                     (ts(now() - timedelta(seconds=1)), job["job_id"]))
    for body in ({}, {"offset": 2, "chunk": b"late"}):  # empty and with a chunk
        r = heartbeat(remote, token, job, **body)
        assert (r.status_code, r.json()["error"]) == (409, "lease_lost"), r.text
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT state FROM test_jobs WHERE job_id=?",
                            (job["job_id"],)).fetchone()[0] == "lost"
    stale = heartbeat(remote, token, {**job, "lease_token": "stale"})
    assert (stale.status_code, stale.json()["error"]) == (409, "owner_mismatch")


def _use_codex_repository(monkeypatch):
    from balancer import app as app_module
    from balancer import test_jobs_api
    modules = _load_codex_modules()
    if modules is None:
        pytest.skip(f"{CODEX_REF} is not available in this clone")
    codex_db, codex_repo = modules["db"], modules["repository"]
    params = inspect.signature(codex_repo.heartbeat_test_job).parameters
    if not all(name in params and params[name].kind is inspect.Parameter.KEYWORD_ONLY
               for name in ("phase", "log_offset", "log_chunk")):
        pytest.skip(f"{CODEX_REF} does not expose the documented heartbeat surface yet")
    for module in (app_module, test_jobs_api):
        monkeypatch.setattr(module, "repo", codex_repo)
        monkeypatch.setattr(module, "get_connection", codex_db.get_connection)
        monkeypatch.setattr(module, "write_tx", codex_db.write_tx)
    monkeypatch.setattr(app_module, "init_schema", codex_db.init_schema)
    monkeypatch.setitem(globals(), "repo", codex_repo)  # register()/ts() helpers in this file


def _end_to_end_real(tmp_path, monkeypatch):
    from balancer import test_jobs_api
    db = str(tmp_path / "real.db")
    local = TestClient(with_peer(create_app(db), "127.0.0.1"), base_url="http://127.0.0.1")
    remote = TestClient(with_peer(create_app(db), TAILNET_PEER), base_url="http://127.0.0.1")
    token = register(local)
    assert upload(local).status_code == 201
    assert create(local, session_ref=None).status_code == 201  # the real repository validates it
    job = claim(remote, token)
    assert claim(remote, token)["lease_token"] == job["lease_token"]
    r = remote.get(f"/test-jobs/{job['job_id']}/snapshot",
                   headers={**auth(token), "X-Lease-Token": job["lease_token"]})
    assert r.content == TAR
    assert heartbeat(remote, token, job, phase="preparing").status_code == 200
    assert heartbeat(remote, token, job, offset=0, chunk=b"ok\n").status_code == 200
    assert heartbeat(remote, token, job, offset=0, chunk=b"ok\n").status_code == 200
    empty = heartbeat(remote, token, job)  # no log fields: the acknowledged offset is used
    assert (empty.status_code, empty.json()["next_log_offset"]) == (200, 3), empty.text

    def broken(path, truncate_to, append):
        raise OSError("disk full")

    real_apply = test_jobs_api._apply_log
    monkeypatch.setattr(test_jobs_api, "_apply_log", broken)
    with pytest.raises(OSError):
        heartbeat(remote, token, job, offset=3, chunk=b"more\n")
    monkeypatch.setattr(test_jobs_api, "_apply_log", real_apply)
    assert heartbeat(remote, token, job, offset=3, chunk=b"more\n").json()["next_log_offset"] == 8

    cancelled = local.post("/test-requests/req-1/cancel").json()
    assert cancelled["cancel_requested"] is True and cancelled["state"] == "running"
    assert local.get("/test-requests/req-1").json()["cancel_requested"] is True
    assert heartbeat(remote, token, job).json()["cancel_requested"] is True
    assert post_result(remote, token, job, status="cancelled").status_code == 200
    assert post_result(remote, token, job, status="cancelled").status_code == 200

    reopened = TestClient(with_peer(create_app(db), "127.0.0.1"), base_url="http://127.0.0.1")
    view = reopened.get("/test-requests/req-1").json()
    assert (view["state"], view["cancel_requested"]) == ("cancelled", True)
    assert reopened.get("/test-requests/req-1/log").json()["text"] == "ok\nmore\n"
    assert remote.post("/runners/claim", headers=auth(token)).json() == {"job": None}
