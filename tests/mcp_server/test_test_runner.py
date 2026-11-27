"""Tests for the background test runner (mcp_server/test_runner.py).

The balancer is faked with httpx.MockTransport (mirroring balancer/repository.py
heartbeat/log semantics); the process adapter is faked so ordering is observable.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
import threading
from pathlib import Path

import httpx
import pytest

from mcp_server import test_runner as tr

RUNNER = "r1"
TOKEN = "r1.secret-token-value"
LEASE = "lease-abc"


# --- helpers --------------------------------------------------------------------------

def make_tar(entries: list[tuple]) -> bytes:
    """entries: (name, bytes) for files, (name, None) for dirs, (name, tarinfo-type, linkname)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for entry in entries:
            name, data = entry[0], entry[1]
            info = tarfile.TarInfo(name)
            if len(entry) == 3:  # tarfile type constants are bytes too
                info.type = data
                info.linkname = entry[2]
                tar.addfile(info)
            elif data is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            else:
                info.size = len(data)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


DEFAULT_TAR = make_tar([("pkg", None), ("pkg/test_a.py", b"def test(): pass\n"),
                        ("README.md", b"hi\n")])


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.lock = threading.Lock()

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        with self.lock:
            self.now += max(s, 0.001)


class FakeBalancer:
    def __init__(self, *, tar: bytes = DEFAULT_TAR, profile=None, log_limit: int = 10 * tr.MiB,
                 jobs: int = 1, sha: str | None = None):
        self.tar = tar
        self.sha = sha or hashlib.sha256(tar).hexdigest()
        self.profile = profile if profile is not None else {"argv": ["python", "-m", "pytest"],
                                                            "timeout_s": 600}
        self.log_limit = log_limit
        self.queue = [f"job{i}" for i in range(jobs)]
        self.events: list[tuple] = []
        self.requests: list[httpx.Request] = []
        self.state: dict[str, str] = {}
        self.received = 0
        self.stored = bytearray()
        self.truncated = False
        self.last_chunk: tuple[int, int, str] | None = None
        self.cancel = False
        self.result_bodies: list[dict] = []
        # hooks: callables(request) -> httpx.Response | None, consumed in order per route
        self.fail: dict[str, list] = {"claim": [], "hb": [], "snapshot": [], "result": []}
        self.on_heartbeat = None  # callable(body) -> None, may mutate state
        self.lock = threading.Lock()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self, **kw) -> tr.RunnerClient:
        kw.setdefault("retry", tr.RetryPolicy(window_s=1.0, initial_delay_s=0.01,
                                              max_delay_s=0.01))
        if "sleep" not in kw:
            clock = FakeClock()
            kw["sleep"], kw["clock"] = clock.sleep, clock
        return tr.RunnerClient("http://bal", RUNNER, TOKEN, httpx.Client(transport=self.transport()),
                               **kw)

    def _hook(self, route: str, request: httpx.Request):
        if self.fail[route]:
            hook = self.fail[route].pop(0)
            return hook(request) if callable(hook) else hook
        return None

    @staticmethod
    def err(status: int, code: str) -> httpx.Response:
        return httpx.Response(status, json={"error": code, "detail": code})

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self.lock:
            self.requests.append(request)
            assert request.headers["authorization"] == f"Bearer {TOKEN}"
            path = request.url.path
            if path == "/runners/claim":
                return self._claim(request)
            if path == f"/runners/{RUNNER}/heartbeat":
                return self._heartbeat(request)
            if path.endswith("/snapshot"):
                return self._snapshot(request)
            if path.endswith("/result"):
                return self._result(request)
            raise AssertionError(f"unexpected {request.method} {path}")

    def _claim(self, request):
        assert request.method == "POST"
        assert json.loads(request.content) == {"lease_s": 60}
        self.events.append(("claim",))
        hooked = self._hook("claim", request)
        if hooked is not None:
            return hooked
        if not self.queue:
            return httpx.Response(200, json={"job": None})
        job_id = self.queue.pop(0)
        self.state[job_id] = "preparing"
        self.received, self.stored, self.truncated, self.last_chunk = 0, bytearray(), False, None
        return httpx.Response(200, json={"job": {
            "job_id": job_id, "request_id": "req1", "target": "local", "state": "preparing",
            "profile": self.profile, "snapshot_sha256": self.sha, "lease_token": LEASE,
            "lease_expires_at": "2026-11-27T00:01:00Z"}})

    def _heartbeat(self, request):
        body = json.loads(request.content)
        assert set(body) <= {"job_id", "lease_token", "phase", "log_offset", "log_chunk"}
        assert body["lease_token"] == LEASE
        chunk = base64.b64decode(body["log_chunk"]) if "log_chunk" in body else None
        self.events.append(("hb", body["phase"], body.get("log_offset"),
                            None if chunk is None else chunk))
        hooked = self._hook("hb", request)
        if hooked is not None:
            return hooked
        if self.on_heartbeat:
            self.on_heartbeat(body)
        job_id = body["job_id"]
        if self.state.get(job_id) == "lost":
            return self.err(409, "lease_lost")
        if self.state.get(job_id) == "running" and body["phase"] == "preparing":
            return self.err(422, "invalid_phase")
        if chunk:
            off = body["log_offset"]
            sha = hashlib.sha256(chunk).hexdigest()
            if off == self.received:
                room = max(0, self.log_limit - len(self.stored))
                self.stored += chunk[:room]
                self.received += len(chunk)
                self.truncated = self.truncated or room < len(chunk)
                self.last_chunk = (off, len(chunk), sha)
            elif self.last_chunk == (off, len(chunk), sha):
                pass  # idempotent retry
            elif off > self.received:
                return self.err(409, "log_offset_gap")
            else:
                return self.err(409, "log_offset_conflict")
        self.state[job_id] = body["phase"]
        return httpx.Response(200, json={
            "job_id": job_id, "state": body["phase"], "lease_expires_at": "x",
            "next_log_offset": self.received, "log_truncated": self.truncated,
            "cancel_requested": self.cancel})

    def _snapshot(self, request):
        assert request.method == "GET"
        assert request.headers["x-lease-token"] == LEASE
        self.events.append(("snapshot",))
        hooked = self._hook("snapshot", request)
        if hooked is not None:
            return hooked
        assert request.headers["accept-encoding"] == "identity"
        # an iterator body stays unread, like a real network stream
        return httpx.Response(200, content=iter([self.tar]), headers={
            "X-Snapshot-SHA256": self.sha, "Content-Length": str(len(self.tar))})

    def _result(self, request):
        body = json.loads(request.content)
        self.result_bodies.append(body)
        self.events.append(("result", body["status"]))
        hooked = self._hook("result", request)
        if hooked is not None:
            return hooked
        job_id = request.url.path.split("/")[2]
        self.state[job_id] = body["status"]
        return httpx.Response(200, json={"job_id": job_id, "state": body["status"]})

    def kinds(self) -> list[str]:
        out = []
        for e in self.events:
            out.append(e[0] if e[0] != "hb" else f"hb:{e[1]}")
        return out

    def shipped(self) -> bytes:
        return bytes(self.stored)


class FakeProcess:
    """Scripted process: emits output over polls, exits at `exit_after` polls."""

    def __init__(self, balancer: FakeBalancer | None = None, *, stdout: bytes = b"",
                 stderr: bytes = b"", exit_code: int | None = 0, exit_after: int = 1,
                 terminate: str | Exception = "exited", emit_per_poll: int | None = None):
        self.balancer = balancer
        self.stdout, self.stderr = stdout, stderr
        self.exit_code, self.exit_after = exit_code, exit_after
        self.terminate = terminate
        self.emit_per_poll = emit_per_poll
        self.polls = 0
        self.spawned: dict | None = None
        self.terminated = False
        self.closed = False
        self.reads: list[tuple[int, int, int]] = []
        self.events_at_spawn: list[str] | None = None

    def factory(self, argv, cwd, env, state_dir, job_id):
        self.spawned = {"argv": argv, "cwd": Path(cwd), "env": env, "state_dir": Path(state_dir),
                        "job_id": job_id, "cwd_files": sorted(p.name for p in Path(cwd).iterdir())}
        if self.balancer is not None:
            self.events_at_spawn = self.balancer.kinds()
            self.balancer.events.append(("spawn",))
        return self

    def _visible(self, data: bytes) -> bytes:
        if self.emit_per_poll is None or self.terminated or self.done():
            return data
        return data[:self.emit_per_poll * self.polls]

    def done(self) -> bool:
        return self.exit_code is not None and self.polls >= self.exit_after

    def poll(self):
        self.polls += 1
        if self.terminated:
            return self.exit_code if self.exit_code is not None else -9
        return self.exit_code if self.done() else None

    def read_output(self, so, se, limit):
        self.reads.append((so, se, limit))
        out = self._visible(self.stdout)[so:so + limit]
        err = self._visible(self.stderr)[se:se + limit]
        return out, err, so + len(out), se + len(err)

    def terminate_and_prove(self, grace_s, kill_timeout_s):
        if self.balancer is not None:
            self.balancer.events.append(("terminate",))
        if isinstance(self.terminate, Exception):
            raise self.terminate
        self.terminated = True
        return self.terminate

    def close(self):
        self.closed = True


def settings(**kw) -> tr.RunnerSettings:
    base = dict(lease_s=60, poll_interval_s=0.1, idle_poll_s=1.0, min_chunk_interval_s=0.0,
                log_chunk_bytes=16, log_buffer_bytes=64, read_limit_bytes=16,
                terminate_grace_s=0.1, kill_timeout_s=0.1)
    base.update(kw)
    return tr.RunnerSettings(**base)


def run_one(bal: FakeBalancer, proc: FakeProcess, tmp_path: Path, clock: FakeClock | None = None,
            **kw):
    clock = clock or FakeClock()
    client = bal.client()
    data = client.claim()
    job = tr.TestJob.from_claim(data)
    return tr.run_test_job(job, tmp_path / "state", 4, client=client,
                           process_factory=proc.factory, settings=settings(**kw), clock=clock,
                           sleep=clock.sleep, base_env={"PATH": "/bin", "SECRET": "x",
                                                        tr.TOKEN_ENV: TOKEN})


# --- HTTP API shape ------------------------------------------------------------------

def test_client_rejects_token_for_other_runner():
    with pytest.raises(ValueError):
        tr.RunnerClient("http://bal", "r1", "r2.secret")
    with pytest.raises(ValueError):
        tr.RunnerClient("http://bal", "r1", "nodot")


def test_heartbeat_payload_shape_and_base64_chunk():
    bal = FakeBalancer()
    client = bal.client()
    job = tr.TestJob.from_claim(client.claim())
    client.heartbeat(job, "preparing")
    client.heartbeat(job, "running", 0, b"\x00\xffbinary")
    bodies = [json.loads(r.content) for r in bal.requests if r.url.path.endswith("/heartbeat")]
    assert bodies[0] == {"job_id": "job0", "lease_token": LEASE, "phase": "preparing"}
    assert bodies[1] == {"job_id": "job0", "lease_token": LEASE, "phase": "running",
                         "log_offset": 0,
                         "log_chunk": base64.b64encode(b"\x00\xffbinary").decode()}
    with pytest.raises(ValueError):
        client.heartbeat(job, "running", 0, b"x" * (tr.API_LOG_CHUNK_MAX_BYTES + 1))
    with pytest.raises(ValueError):
        client.heartbeat(job, "running", 0, None)


def test_retry_resends_identical_request_and_classifies_errors():
    bal = FakeBalancer()
    client = bal.client()
    job = tr.TestJob.from_claim(client.claim())

    def drop(request):
        raise httpx.ConnectError("boom")
    bal.fail["hb"] = [drop, FakeBalancer.err(503, "busy")]
    client.heartbeat(job, "running", 0, b"abc")
    sent = [r.content for r in bal.requests if r.url.path.endswith("/heartbeat")]
    assert len(sent) == 3 and len(set(sent)) == 1

    bal.fail["hb"] = [FakeBalancer.err(409, "lease_lost")]
    with pytest.raises(tr.LeaseLost):
        client.heartbeat(job, "running")
    bal.fail["hb"] = [FakeBalancer.err(401, "unauthorized")]
    with pytest.raises(tr.AuthError):
        client.heartbeat(job, "running")
    bal.fail["hb"] = [FakeBalancer.err(409, "log_offset_conflict")]
    with pytest.raises(tr.ApiRejected) as info:
        client.heartbeat(job, "running")
    assert not isinstance(info.value, tr.LeaseLost)
    bal.fail["hb"] = [FakeBalancer.err(503, "busy")] * 1000
    with pytest.raises(tr.TransientError):
        client.heartbeat(job, "running", window_s=0.05)


def test_transport_error_message_does_not_leak_token():
    bal = FakeBalancer()
    client = bal.client()

    def drop(request):
        raise httpx.ConnectError(f"failed {request.headers['authorization']}")
    bal.fail["claim"] = [drop] * 1000
    with pytest.raises(tr.TransientError) as info:
        client.claim()
    assert "secret" not in str(info.value)
    assert "secret" not in repr(client)


def test_claim_validation():
    good = {"job_id": "j1", "snapshot_sha256": "a" * 64, "lease_token": "t", "state": "preparing"}
    assert tr.TestJob.from_claim(good).job_id == "j1"
    for bad in ({**good, "job_id": "../x"}, {**good, "snapshot_sha256": "XYZ"},
                {**good, "lease_token": ""}, {**good, "state": "running"}, "job"):
        with pytest.raises(tr.ProtocolError):
            tr.TestJob.from_claim(bad)
    assert "tok-secret" not in repr(tr.TestJob.from_claim({**good, "lease_token": "tok-secret"}))


# --- ordering ------------------------------------------------------------------------

def test_happy_path_ordering_and_result(tmp_path):
    bal = FakeBalancer()
    proc = FakeProcess(bal, stdout=b"collected 1 item\n", stderr=b"warn\n", exit_code=0)
    result = run_one(bal, proc, tmp_path)
    assert result.status == "succeeded" and result.exit_code == 0
    kinds = bal.kinds()
    assert kinds[:4] == ["claim", "hb:preparing", "snapshot", "hb:running"]
    assert kinds.index("hb:running") < kinds.index("spawn") < kinds.index("terminate")
    assert kinds.index("terminate") < kinds.index("result")
    assert kinds[-1] == "result" and kinds.count("result") == 1
    assert proc.events_at_spawn[-1] == "hb:running"
    # logs shipped before the result, via offset-addressed running heartbeats
    # stdout and stderr interleave in the combined log at read granularity
    assert sorted(bal.shipped()) == sorted(b"collected 1 item\nwarn\n")
    body = bal.result_bodies[0]
    assert body["lease_token"] == LEASE and body["status"] == "succeeded"
    assert body["stdout_tail"] == "collected 1 item\n" and body["stderr_tail"] == "warn\n"
    # run_one detects 4 CPUs; the default profile has no cap and no explicit args
    assert body["resources"] == {"parallelism": 4, "parallel_args_applied": [],
                                 "memory_limit_mb": None, "memory_limit_enforced": None}
    assert proc.closed
    assert not (tmp_path / "state" / "jobs" / "job0").exists()


def test_spawn_receives_snapshot_cwd_and_scrubbed_env(tmp_path):
    bal = FakeBalancer(profile={"argv": ["pytest", "-q"], "cwd": "pkg", "env": {"FOO": "1"}})
    proc = FakeProcess(bal)
    run_one(bal, proc, tmp_path)
    sp = proc.spawned
    assert sp["argv"] == ["pytest", "-q"] and sp["job_id"] == "job0"
    assert sp["cwd"].name == "pkg" and sp["cwd_files"] == ["test_a.py"]
    env = sp["env"]
    assert env["FOO"] == "1" and env["PATH"] == "/bin"
    assert "SECRET" not in env and tr.TOKEN_ENV not in env
    assert env["A2A_LINK_TEST_JOB_ID"] == "job0"
    assert env["OMP_NUM_THREADS"] == "4" and env["MAKEFLAGS"] == "-j4"
    assert Path(env["TMP"]).is_relative_to(tmp_path) and Path(env["HOME"]).is_relative_to(tmp_path)


def test_no_spawn_when_running_ack_fails(tmp_path):
    bal = FakeBalancer()
    proc = FakeProcess(bal)
    calls = {"n": 0}

    def fail_running(body):
        if body["phase"] == "running":
            calls["n"] += 1
            raise httpx.ConnectError("down")
    bal.on_heartbeat = fail_running
    with pytest.raises(tr.RunnerHalted):
        run_one(bal, proc, tmp_path, presumed_lost_after_s=0.05)
    assert proc.spawned is None and calls["n"] >= 1
    assert "result" not in bal.kinds()


def test_no_spawn_when_running_ack_reports_lease_lost(tmp_path):
    bal = FakeBalancer()
    proc = FakeProcess(bal)

    def lose(body):
        if body["phase"] == "running":
            bal.state["job0"] = "lost"
    bal.on_heartbeat = lose
    with pytest.raises(tr.RunnerHalted):
        run_one(bal, proc, tmp_path)
    assert proc.spawned is None and "result" not in bal.kinds()


def test_cancel_before_start_posts_cancelled_without_spawn(tmp_path):
    bal = FakeBalancer()
    bal.cancel = True
    proc = FakeProcess(bal)
    result = run_one(bal, proc, tmp_path)
    assert result.status == "cancelled" and proc.spawned is None
    assert "snapshot" not in bal.kinds()


def test_cancel_at_running_ack_posts_cancelled_without_spawn(tmp_path):
    bal = FakeBalancer()
    proc = FakeProcess(bal)
    bal.on_heartbeat = lambda b: setattr(bal, "cancel", b["phase"] == "running")
    result = run_one(bal, proc, tmp_path)
    assert result.status == "cancelled" and proc.spawned is None


@pytest.mark.parametrize("profile", ["default", {"argv": []}, {"argv": ["x"], "shell": True},
                                     {"argv": ["x"], "memory_limit_mb": 0},
                                     {"argv": ["x"], "parallelism": 0},
                                     {"argv": ["x"], "parallelism_args": ["-n", "{job}"]},
                                     {"argv": ["x"], "cwd": "../up"},
                                     {"argv": ["x"], "env": {"A2A_LINK_RUNNER_TOKEN": "x"}},
                                     {"argv": ["x"], "timeout_s": 0}])
def test_invalid_profile_fails_without_download_or_spawn(tmp_path, profile):
    bal = FakeBalancer(profile=profile)
    proc = FakeProcess(bal)
    result = run_one(bal, proc, tmp_path)
    assert result.status == "failed" and "profile" in result.error
    assert proc.spawned is None and "snapshot" not in bal.kinds()


def test_missing_profile_cwd_fails(tmp_path):
    bal = FakeBalancer(profile={"argv": ["x"], "cwd": "nope"})
    proc = FakeProcess(bal)
    result = run_one(bal, proc, tmp_path)
    assert result.status == "failed" and proc.spawned is None


def test_spawn_failure_posts_failed(tmp_path):
    bal = FakeBalancer()

    def factory(*a):
        raise FileNotFoundError("no such exe")
    client = bal.client()
    job = tr.TestJob.from_claim(client.claim())
    clock = FakeClock()
    result = tr.run_test_job(job, tmp_path, 1, client=client, process_factory=factory,
                             settings=settings(), clock=clock, sleep=clock.sleep)
    assert result.status == "failed" and "could not start" in result.error


# --- snapshot safety -------------------------------------------------------------------

def test_digest_mismatch_fails_without_spawn(tmp_path):
    bal = FakeBalancer(sha="b" * 64)  # served header matches the claim, body does not
    proc = FakeProcess(bal)
    result = run_one(bal, proc, tmp_path)
    assert result.status == "failed" and "digest" in result.error
    assert proc.spawned is None


def test_served_sha_header_mismatch_rejected(tmp_path):
    bal = FakeBalancer()
    bal.fail["snapshot"] = [httpx.Response(200, content=iter([bal.tar]),
                                           headers={"X-Snapshot-SHA256": "c" * 64})]
    proc = FakeProcess(bal)
    result = run_one(bal, proc, tmp_path)
    assert result.status == "failed" and proc.spawned is None


def test_snapshot_size_cap(tmp_path):
    bal = FakeBalancer()
    proc = FakeProcess(bal)
    result = run_one(bal, proc, tmp_path, snapshot_max_bytes=100)
    assert result.status == "failed" and "exceeds" in result.error


def test_snapshot_download_retries_on_transient(tmp_path):
    bal = FakeBalancer()
    bal.fail["snapshot"] = [FakeBalancer.err(503, "busy")]
    proc = FakeProcess(bal)
    assert run_one(bal, proc, tmp_path).status == "succeeded"
    assert bal.kinds().count("snapshot") == 2


@pytest.mark.parametrize("entries", [
    [("../evil.txt", b"x")],
    [("/abs.txt", b"x")],
    [("a/../../evil.txt", b"x")],
    [("a\\b.txt", b"x")],
    [("link", tarfile.SYMTYPE, "/etc/passwd")],
    [("hard", tarfile.LNKTYPE, "README.md")],
    [("fifo", tarfile.FIFOTYPE, "")],
    [("dev", tarfile.CHRTYPE, "")],
    [("a.txt", b"1"), ("a.txt", b"2")],  # duplicate member would overwrite
])
def test_extract_rejects_unsafe_members(tmp_path, entries):
    archive = tmp_path / "s.tar"
    archive.write_bytes(make_tar(entries))
    dest = tmp_path / "out"
    with pytest.raises(tr.SnapshotError):
        tr.extract_snapshot(archive, dest)
    assert not (tmp_path / "evil.txt").exists()


def test_extract_writes_regular_files_and_dirs(tmp_path):
    archive = tmp_path / "s.tar"
    archive.write_bytes(DEFAULT_TAR)
    assert tr.extract_snapshot(archive, tmp_path / "out") == 2
    assert (tmp_path / "out" / "pkg" / "test_a.py").read_bytes() == b"def test(): pass\n"


def test_extract_rejects_compressed_and_expansion(tmp_path):
    import gzip
    archive = tmp_path / "s.tar"
    archive.write_bytes(gzip.compress(DEFAULT_TAR))
    with pytest.raises(tr.SnapshotError):
        tr.extract_snapshot(archive, tmp_path / "o1")
    archive.write_bytes(make_tar([("big", b"x" * 1000)]))
    with pytest.raises(tr.SnapshotError):
        tr.extract_snapshot(archive, tmp_path / "o2", max_bytes=999)
    archive.write_bytes(make_tar([(f"f{i}", b"") for i in range(5)]))
    with pytest.raises(tr.SnapshotError):
        tr.extract_snapshot(archive, tmp_path / "o3", max_members=4)


def test_unsafe_snapshot_job_fails_without_spawn(tmp_path):
    bal = FakeBalancer(tar=make_tar([("ok.txt", b"1"), ("../../escape", b"x")]))
    proc = FakeProcess(bal)
    result = run_one(bal, proc, tmp_path)
    assert result.status == "failed" and "snapshot rejected" in result.error
    assert proc.spawned is None and not (tmp_path / "state" / "jobs" / "escape").exists()


# --- logs ------------------------------------------------------------------------------

def test_logs_are_sequential_single_chunks_with_contiguous_offsets(tmp_path):
    out = bytes(range(256)) * 2  # binary, multiple chunks
    bal = FakeBalancer()
    proc = FakeProcess(bal, stdout=out, exit_after=20, emit_per_poll=30)
    run_one(bal, proc, tmp_path)
    chunks = [(e[2], e[3]) for e in bal.events if e[0] == "hb" and e[3] is not None]
    assert all(0 < len(c) <= 16 for _, c in chunks)
    offset = 0
    for off, c in chunks:
        assert off == offset
        offset += len(c)
    assert bal.shipped() == out
    assert bal.kinds().index("result") > max(i for i, e in enumerate(bal.events)
                                             if e[0] == "hb" and e[3] is not None)


def test_lost_heartbeat_response_resends_identical_chunk(tmp_path):
    bal = FakeBalancer()
    state = {"dropped": False}
    real = bal._heartbeat

    def drop_after_commit(request):
        response = real(request)  # balancer commits the chunk...
        raise httpx.ReadTimeout("response lost")  # ...but the runner never hears back
    def maybe(request):
        body = json.loads(request.content)
        if "log_chunk" in body and not state["dropped"]:
            state["dropped"] = True
            return drop_after_commit(request)
        return None
    bal.fail["hb"] = [maybe] * 50
    proc = FakeProcess(bal, stdout=b"0123456789abcdefXYZ", exit_after=3)
    assert run_one(bal, proc, tmp_path).status == "succeeded"
    assert bal.shipped() == b"0123456789abcdefXYZ"
    chunk_events = [(e[2], e[3]) for e in bal.events if e[0] == "hb" and e[3] is not None]
    assert chunk_events[0] == chunk_events[1]  # identical retry


def test_log_truncation_stops_shipping_but_keeps_tails(tmp_path):
    out = b"A" * 100 + b"END"
    bal = FakeBalancer(log_limit=20)
    proc = FakeProcess(bal, stdout=out, exit_after=2)
    result = run_one(bal, proc, tmp_path)
    assert result.status == "succeeded"
    assert bal.shipped() == out[:20]
    chunks = [e for e in bal.events if e[0] == "hb" and e[3] is not None]
    assert sum(len(e[3]) for e in chunks) <= 32  # stopped after the truncation ack
    assert result.stdout_tail.endswith("END")


def test_output_buffer_backpressure_bounds_reads(tmp_path):
    out = b"x" * 1000
    bal = FakeBalancer()
    proc = FakeProcess(bal, stdout=out, exit_after=1)
    run_one(bal, proc, tmp_path)
    assert all(limit <= 16 for _, _, limit in proc.reads)
    assert bal.shipped() == out


def test_result_tails_are_capped_and_valid_utf8():
    data = ("é" * (tr.RESULT_TAIL_MAX_BYTES)).encode()
    text = tr.bounded_text(b"\xff" + data)
    assert len(text.encode()) <= tr.RESULT_TAIL_MAX_BYTES
    tail = tr._Tail(limit=10)
    for _ in range(100):
        tail.add(b"0123456789")
    assert len(tail.data) <= 20 and tail.text() == "0123456789"


def test_large_output_result_tail_capped(tmp_path):
    out = b"y" * (tr.RESULT_TAIL_MAX_BYTES + 500) + b"\xe2\x82"  # trailing partial UTF-8
    bal = FakeBalancer(log_limit=0)
    proc = FakeProcess(bal, stdout=out, exit_after=1)
    result = run_one(bal, proc, tmp_path, read_limit_bytes=64 * 1024, log_buffer_bytes=256 * 1024,
                     log_chunk_bytes=64 * 1024)
    assert len(result.stdout_tail.encode()) <= tr.RESULT_TAIL_MAX_BYTES
    assert len(json.dumps(bal.result_bodies[0])) < 3 * tr.RESULT_TAIL_MAX_BYTES


# --- cancellation, timeout, termination ------------------------------------------------

def test_cancel_while_running_terminates_then_posts_cancelled(tmp_path):
    bal = FakeBalancer()
    proc = FakeProcess(bal, stdout=b"partial", exit_code=None)
    seen = {"n": 0}

    def cancel_later(body):
        if body["phase"] == "running":
            seen["n"] += 1
            bal.cancel = seen["n"] >= 2  # not at the start ack
    bal.on_heartbeat = cancel_later
    result = run_one(bal, proc, tmp_path)
    assert result.status == "cancelled"
    kinds = bal.kinds()
    assert kinds.index("spawn") < kinds.index("terminate") < kinds.index("result")
    assert bal.shipped() == b"partial"


def test_timeout_terminates_and_reports_timed_out(tmp_path):
    bal = FakeBalancer(profile={"argv": ["x"], "timeout_s": 2})
    proc = FakeProcess(bal, exit_code=None)
    result = run_one(bal, proc, tmp_path)
    assert result.status == "failed" and result.timed_out and "timed out" in result.error
    assert bal.result_bodies[0]["timed_out"] is True
    assert bal.kinds().index("terminate") < bal.kinds().index("result")


def test_unproven_termination_halts_without_result(tmp_path):
    bal = FakeBalancer(profile={"argv": ["x"], "timeout_s": 1})
    proc = FakeProcess(bal, exit_code=None, terminate=RuntimeError("scope not empty"))
    with pytest.raises(tr.RunnerHalted):
        run_one(bal, proc, tmp_path)
    assert "result" not in bal.kinds()


def test_unproven_termination_after_normal_exit_halts(tmp_path):
    bal = FakeBalancer()
    proc = FakeProcess(bal, exit_code=0, terminate=RuntimeError("grandchild alive"))
    with pytest.raises(tr.RunnerHalted):
        run_one(bal, proc, tmp_path)
    assert "result" not in bal.kinds()


def test_lease_lost_while_running_terminates_and_halts(tmp_path):
    bal = FakeBalancer()
    proc = FakeProcess(bal, exit_code=None)
    seen = {"n": 0}

    def lose(body):
        if body["phase"] == "running":
            seen["n"] += 1
            if seen["n"] >= 2:
                bal.state["job0"] = "lost"
    bal.on_heartbeat = lose
    with pytest.raises(tr.RunnerHalted):
        run_one(bal, proc, tmp_path)
    assert "terminate" in bal.kinds() and "result" not in bal.kinds()


def test_unreachable_balancer_terminates_within_lease(tmp_path):
    bal = FakeBalancer()
    proc = FakeProcess(bal, exit_code=None)
    seen = {"n": 0}

    def go_down(body):
        if body["phase"] == "running":
            seen["n"] += 1
            if seen["n"] >= 2:
                raise httpx.ConnectError("down")
    bal.on_heartbeat = go_down
    clock = FakeClock()
    result = run_one(bal, proc, tmp_path, clock=clock, lease_s=20)
    assert proc.terminated and result.status == "failed" and "unreachable" in result.error
    assert bal.kinds().index("terminate") < bal.kinds().index("result")
    assert clock.now - 1000.0 < 20 + 5  # gave up about one lease after the last ack

    # A balancer that expired the lease refuses the result: the runner halts.
    bal2 = FakeBalancer()
    seen["n"] = 0
    bal2.on_heartbeat = go_down
    bal2.fail["result"] = [FakeBalancer.err(409, "lease_lost")]
    with pytest.raises(tr.RunnerHalted):
        run_one(bal2, FakeProcess(bal2, exit_code=None), tmp_path / "b", lease_s=20)


def test_result_retried_identically_after_lost_response(tmp_path):
    bal = FakeBalancer()
    real = bal._result

    def commit_then_drop(request):
        real(request)
        raise httpx.ReadTimeout("lost")
    bal.fail["result"] = [commit_then_drop]
    proc = FakeProcess(bal, stdout=b"ok")
    assert run_one(bal, proc, tmp_path).status == "succeeded"
    assert len(bal.result_bodies) >= 2
    assert all(b == bal.result_bodies[0] for b in bal.result_bodies)


def test_result_rejected_halts(tmp_path):
    bal = FakeBalancer()
    bal.fail["result"] = [FakeBalancer.err(409, "owner_mismatch")]
    with pytest.raises(tr.RunnerHalted):
        run_one(bal, FakeProcess(bal), tmp_path)


# --- claim loop / reconnect ------------------------------------------------------------

def test_runner_reconnects_after_transient_claim_failures(tmp_path):
    bal = FakeBalancer(jobs=2, profile="python-pytest")
    bal.fail["claim"] = [FakeBalancer.err(503, "busy")] * 3 + [
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("x"))]
    clock = FakeClock()
    procs = []

    def factory(*a):
        p = FakeProcess(bal)
        procs.append(p)
        return p.factory(*a)
    runner = tr.Runner(bal.client(retry=tr.RetryPolicy(window_s=0.0, initial_delay_s=0.01)),
                       tmp_path, cpu_count=2, process_factory=factory, settings=settings(),
                       sleep=clock.sleep, clock=clock)
    assert runner.run(stop_when_idle=True) == tr.EXIT_OK
    assert len(procs) == 2 and bal.kinds().count("result") == 2


def test_runner_halts_and_stops_claiming_after_lost_lease(tmp_path):
    bal = FakeBalancer(jobs=2, profile="python-pytest")
    bal.on_heartbeat = lambda b: bal.state.__setitem__(b["job_id"], "lost") \
        if b["phase"] == "running" else None
    clock = FakeClock()
    runner = tr.Runner(bal.client(), tmp_path, cpu_count=1,
                       process_factory=FakeProcess(bal).factory, settings=settings(),
                       sleep=clock.sleep, clock=clock)
    assert runner.run(stop_when_idle=True) == tr.EXIT_HALTED
    assert bal.kinds().count("claim") == 1 and runner.halted


def test_runner_exits_on_auth_failure(tmp_path):
    bal = FakeBalancer()
    bal.fail["claim"] = [FakeBalancer.err(401, "unauthorized")]
    runner = tr.Runner(bal.client(), tmp_path, cpu_count=1,
                       process_factory=FakeProcess().factory, settings=settings())
    assert runner.run() == tr.EXIT_USAGE


def test_redelivered_preparing_claim_starts_clean(tmp_path):
    stale = tmp_path / "state" / "jobs" / "job0" / "src"
    stale.mkdir(parents=True)
    (stale / "leftover").write_text("x")
    bal = FakeBalancer()
    proc = FakeProcess(bal)
    assert run_one(bal, proc, tmp_path).status == "succeeded"
    assert "leftover" not in proc.spawned["cwd_files"]


# --- CLI -----------------------------------------------------------------------------

def test_cli_refuses_token_argument(capsys):
    assert tr.main(["--balancer-url", "http://b", "--runner-id", "r1", "--token", "r1.x"],
                   environ={}) == tr.EXIT_USAGE
    assert "--token is not accepted" in capsys.readouterr().err


def test_cli_requires_token_source(tmp_path, capsys):
    rc = tr.main(["--balancer-url", "http://b", "--runner-id", "r1",
                  "--state-dir", str(tmp_path)], environ={})
    assert rc == tr.EXIT_USAGE and tr.TOKEN_ENV in capsys.readouterr().err


def test_cli_once_with_token_file_runs_job_and_scrubs_env(tmp_path):
    bal = FakeBalancer()
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    proc = FakeProcess(bal)
    environ = {tr.TOKEN_ENV: "ignored"}
    rc = tr.main(["--balancer-url", "http://bal", "--runner-id", RUNNER, "--token-file",
                  str(token_file), "--state-dir", str(tmp_path / "st"), "--once"],
                 environ=environ, process_factory=proc.factory,
                 http_client=httpx.Client(transport=bal.transport()))
    assert rc == tr.EXIT_OK and bal.kinds()[-1] == "result"
    assert tr.TOKEN_ENV not in environ


def test_cli_token_from_env(tmp_path):
    bal = FakeBalancer(jobs=0)
    rc = tr.main(["--balancer-url", "http://bal", "--runner-id", RUNNER, "--state-dir",
                  str(tmp_path), "--once"], environ={tr.TOKEN_ENV: TOKEN},
                 process_factory=FakeProcess().factory,
                 http_client=httpx.Client(transport=bal.transport()))
    assert rc == tr.EXIT_OK and bal.kinds() == ["claim"]


def test_default_state_dir_and_cpu_count(tmp_path):
    assert tr.default_state_dir({tr.HOME_ENV: str(tmp_path)}) == tmp_path
    assert tr.detect_cpu_count() >= 1
