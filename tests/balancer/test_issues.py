"""Balancer acceptance tests for issue collaboration.

The balancer only stores state. These tests play the roles of coordinator, adapter and
supervisor with plain HTTP calls; nothing here runs a CLI or git.
"""
import multiprocessing
import os
import queue as queue_module
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from balancer.app import create_app
from balancer.db import get_connection, init_schema

BASE = "a" * 40
CAND = "d" * 40
SHAS = {"claude": "b" * 40, "codex": "c" * 40}
ARGV = ["python", "-m", "pytest", "-q"]
OWNER = "supervisor:host1:install-1"
OWNER2 = "supervisor:host2:install-2"
IDENT = {"host": "host1", "platform": "posix", "pid": 42,
         "start_time": "2026-11-24T10:00:01.120Z", "group": "42"}
TTL = 100


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 24, 10, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "issues.db")


def make_client(db, clock):
    return TestClient(create_app(db, clock=clock, attempt_ttl_s=TTL))


@pytest.fixture
def client(db, clock):
    return make_client(db, clock)


# --- helpers ---------------------------------------------------------------------

def open_issue(client, request_id="req-1", **over):
    body = {"request_id": request_id, "title": "Add feature", "goal": "Ship it",
            "acceptance": ["tests pass"], "cwd": "C:/repo", "base_sha": BASE,
            "opened_from": "claude", **over}
    r = client.post("/issues", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()


def snap(client, iid):
    r = client.get(f"/issues/{iid}")
    assert r.status_code == 200, r.text
    return r.json()


def ver(client, iid):
    return snap(client, iid)["issue"]["version"]


def cur_round(client, iid):
    """Round ids are opaque and globally unique; always read the current one back."""
    return snap(client, iid)["round"]["id"]


def post_msg(client, iid, key, message_id, rnd=None, gen=1, kind="continue", owner=OWNER,
             prompt="do it", profile=None):
    body = {"message_id": message_id, "kind": kind, "round_id": rnd or cur_round(client, iid),
            "generation": gen,
            "owner": owner, "prompt": prompt}
    if profile is not None:
        body["requested_profile"] = profile
    return client.post(f"/issues/{iid}/participants/{key}/messages", json=body)


def result(client, iid, attempt, owner=OWNER, **over):
    body = {"owner": owner, "status": "done", "text": "ok", "exit_code": 0, **over}
    return client.post(f"/issues/{iid}/attempts/{attempt}/result", json=body)


def start_participant(client, iid, key, session=None, rnd=None):
    """start message, handle save, done result: the generation-1 start sequence."""
    # message ids are caller chosen and globally unique, so scope them to the issue
    r = post_msg(client, iid, key, f"start-{key}-{iid}", rnd=rnd, kind="start")
    assert r.status_code == 201, r.text
    attempt = r.json()["attempt"]["attempt_id"]
    h = client.put(f"/issues/{iid}/participants/{key}/handle", json={
        "generation": 1, "attempt_id": attempt,
        "handle": {"adapter_type": key, "session_id": session or f"sess-{iid}-{key}",
                   "cwd": f"C:/wt/{key}"}})
    assert h.status_code == 200, h.text
    assert result(client, iid, attempt).status_code == 200
    return attempt


def ready(client, iid, key, attempt, sha, rnd=None, gen=1, base=BASE, expected_version=None):
    return client.post(f"/issues/{iid}/participants/{key}/ready", json={
        "round_id": rnd or cur_round(client, iid), "generation": gen, "attempt_id": attempt, "source_commit_sha": sha,
        "expected_base_sha": base,
        "expected_version": ver(client, iid) if expected_version is None else expected_version})


def propose(client, iid, rnd=None, text="plan"):
    r = client.post(f"/issues/{iid}/proposals", json={
        "round_id": rnd or cur_round(client, iid), "by": "claude", "text": text, "expected_version": ver(client, iid)})
    assert r.status_code == 201, r.text
    return r.json()["proposal_id"]


def decide(client, iid, pid, kind="accept", rnd=None, proposal_version=1, expected_version=None):
    return client.post(f"/issues/{iid}/decisions", json={
        "round_id": rnd or cur_round(client, iid), "proposal_id": pid, "proposal_version": proposal_version, "kind": kind,
        "by": "coordinator",
        "expected_version": ver(client, iid) if expected_version is None else expected_version})


def accepted_issue(client):
    iid = open_issue(client)["issue"]["id"]
    attempts = {k: start_participant(client, iid, k) for k in ("claude", "codex")}
    for k in ("claude", "codex"):
        assert ready(client, iid, k, attempts[k], SHAS[k]).status_code == 201
    pid = propose(client, iid)
    assert decide(client, iid, pid).status_code == 201
    return iid, pid


def start_int(client, iid, pid, argv=ARGV, req="ir-1", owner=OWNER, shas=None, base=BASE):
    return client.post(f"/issues/{iid}/integrations", json={
        "integration_request_id": req, "round_id": cur_round(client, iid), "proposal_id": pid,
        "proposal_version": 1,
        "source_shas": SHAS if shas is None else shas, "expected_base_sha": base,
        "verification_argv": list(argv), "owner": owner, "expected_version": ver(client, iid)})


def candidate(client, iid, gid, owner=OWNER, sha=CAND, status="merged", **over):
    body = {"owner": owner, "status": status,
            "output": {"exit_code": 0, "stdout": "merged", "stderr": "", "truncated": False}, **over}
    if status == "merged":
        body["candidate_sha"] = sha
    else:
        body.setdefault("detail", "CONFLICT in a.py")
    return client.post(f"/issues/{iid}/integrations/{gid}/candidate", json=body)


def _post_tests(client, iid, gid, owner=OWNER, argv=ARGV, status="pass", exit_code=0, tested=CAND):
    return client.post(f"/issues/{iid}/integrations/{gid}/tests", json={
        "owner": owner, "tested_sha": tested, "argv": list(argv), "status": status,
        "exit_code": exit_code, "stdout": "42 passed", "stderr": "", "truncated": False,
        "duration_s": 1.5})


def advance(client, iid, gid, owner=OWNER):
    return client.post(f"/issues/{iid}/integrations/{gid}/advance",
                       json={"owner": owner, "expected_version": ver(client, iid)})


def int_result(client, iid, gid, owner=OWNER, status="advanced", **over):
    body = {"owner": owner, "status": status,
            "ref_update_output": {"exit_code": 0, "stdout": "", "stderr": ""}, **over}
    if status == "advanced":
        body.setdefault("new_base_sha", CAND)
    else:
        body.setdefault("error", "ref update failed")
    return client.post(f"/issues/{iid}/integrations/{gid}/result", json=body)


def active_message(client, iid, key="claude", message_id="m1"):
    start_participant(client, iid, key)
    r = post_msg(client, iid, key, message_id)
    assert r.status_code == 201, r.text
    return r.json()["attempt"]["attempt_id"]


def confirm(client, iid, attempt, owner=OWNER, method="terminated", identity=IDENT):
    return client.post(f"/issues/{iid}/attempts/{attempt}/termination_confirmed",
                       json={"owner": owner, "process_identity": identity, "method": method})


# --- 1. creation, idempotency, validation, coordinator-only mode --------------------------

def test_create_issue_replay_and_conflict(client):
    first = client.post("/issues", json={
        "request_id": "req-1", "title": "T", "goal": "G", "acceptance": ["a"], "cwd": "C:/repo",
        "base_sha": BASE, "opened_from": "codex"})
    assert first.status_code == 201
    again = client.post("/issues", json={
        "request_id": "req-1", "title": "T", "goal": "G", "acceptance": ["a"], "cwd": "C:/repo",
        "base_sha": BASE, "opened_from": "codex"})
    assert again.status_code == 200 and again.json() == first.json()
    other = client.post("/issues", json={
        "request_id": "req-1", "title": "DIFFERENT", "goal": "G", "acceptance": ["a"], "cwd": "C:/repo",
        "base_sha": BASE, "opened_from": "codex"})
    assert other.status_code == 409 and other.json()["error"] == "request_id_conflict"


def test_two_issues_with_the_same_title_get_distinct_issue_and_round_ids(client):
    """Round ids are primary keys across all issues, so they cannot be issue-local examples."""
    a = open_issue(client, request_id="req-a", title="Same title")
    b = open_issue(client, request_id="req-b", title="Same title")
    assert a["issue"]["id"] != b["issue"]["id"]
    assert a["round"]["id"] != b["round"]["id"]
    assert a["round"]["number"] == 1 and b["round"]["number"] == 1
    # the id returned at creation is the one the snapshot and every route use
    for issue in (a, b):
        iid = issue["issue"]["id"]
        assert snap(client, iid)["round"]["id"] == issue["round"]["id"]
        start_participant(client, iid, "claude", rnd=issue["round"]["id"])
        assert post_msg(client, iid, "claude", f"m1-{iid}",
                        rnd=issue["round"]["id"]).status_code == 201
        assert propose(client, iid, rnd=issue["round"]["id"])


def test_create_issue_validation_and_no_attach_fields(client):
    base = {"request_id": "r", "title": "T", "goal": "G", "cwd": "C:/repo", "base_sha": BASE,
            "opened_from": "claude"}
    assert client.post("/issues", json={**base, "opened_from": "gemini"}).status_code == 422
    assert client.post("/issues", json={**base, "base_sha": "abc"}).status_code == 422
    for extra in ("caller_handle", "handle", "source", "session_id"):
        r = client.post("/issues", json={**base, extra: "x"})
        assert r.status_code == 422 and r.json()["error"] == "validation_error", extra
    assert client.post("/issues", json={**base, "participants": ["a", "a"]}).status_code == 422


def test_create_issue_returns_deterministic_paths_without_touching_disk(client):
    resp = open_issue(client)
    iid = resp["issue"]["id"]
    assert resp["issue"]["opened_from"] == "claude"
    assert set(resp["participants"]) == {"claude", "codex"}
    root = Path(os.environ["A2A_LINK_HOME"]).as_posix()
    for key in ("claude", "codex"):
        wt = resp["paths"][key]["worktree"]
        assert wt == f"{root}/worktrees/{iid}-{key}"
        assert resp["paths"][key]["branch"] == f"a2a/issue-{iid}/{key}"
        assert not Path(wt).exists()  # the balancer never creates directories


def test_snapshot_has_goal_acceptance_round_and_both_participants(client):
    iid = open_issue(client)["issue"]["id"]
    s = snap(client, iid)
    assert s["issue"]["goal"] == "Ship it" and s["issue"]["acceptance"] == ["tests pass"]
    assert s["issue"]["opened_from"] == "claude" and s["issue"]["base_sha"] == BASE
    assert s["round"] == {"id": s["round"]["id"], "number": 1, "state": "open", "base_sha": BASE}
    assert s["round"]["id"].startswith("r_") and s["round"]["id"] != "r_1"
    assert set(s["participants"]) == {"claude", "codex"}
    for p in s["participants"].values():
        assert p["generation"] == 1 and p["handle"] is None and p["blocked"] is None
    assert client.get("/issues/i_missing").status_code == 404


def test_restart_on_same_database_keeps_state(db, clock):
    c1 = make_client(db, clock)
    iid = open_issue(c1)["issue"]["id"]
    attempt = active_message(c1, iid)
    before = snap(c1, iid)
    c2 = make_client(db, clock)  # a second balancer process on the same file
    after = snap(c2, iid)
    assert after == before
    assert after["participants"]["claude"]["handle"]["session_id"] == f"sess-{iid}-claude"
    assert [a["attempt_id"] for a in after["attempts"]][-1] == attempt
    assert open_issue(c2)["issue"]["id"] == iid  # idempotency survives the restart


# --- 2. messages, attempts, active turn ------------------------------------------------------

def test_message_replay_never_starts_a_run(client):
    iid = open_issue(client)["issue"]["id"]
    start_participant(client, iid, "claude")
    first = post_msg(client, iid, "claude", "m1")
    assert first.status_code == 201 and first.json()["execute"] is True
    replay = post_msg(client, iid, "claude", "m1")
    assert replay.status_code == 200 and replay.json()["execute"] is False
    assert replay.json()["attempt"]["attempt_id"] == first.json()["attempt"]["attempt_id"]
    s = snap(client, iid)
    assert len([a for a in s["attempts"] if a["message_id"] == "m1"]) == 1
    different = post_msg(client, iid, "claude", "m1", prompt="another prompt")
    assert different.status_code == 409 and different.json()["error"] == "message_conflict"


def test_second_message_on_active_thread_is_rejected(client):
    iid = open_issue(client)["issue"]["id"]
    attempt = active_message(client, iid)
    r = post_msg(client, iid, "claude", "m2")
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "active_turn" and body["active_message_id"] == "m1"
    assert body["attempt_id"] == attempt
    # the other participant's thread is independent
    start_participant(client, iid, "codex")
    assert post_msg(client, iid, "codex", "c1").status_code == 201


def test_concurrent_clients_get_exactly_one_active_turn(db, clock):
    seed = make_client(db, clock)
    iid = open_issue(seed)["issue"]["id"]
    start_participant(seed, iid, "claude")
    clients = [make_client(db, clock) for _ in range(2)]  # two balancer processes
    barrier = threading.Barrier(8)
    codes, lock = [], threading.Lock()

    def worker(n):
        barrier.wait()
        r = post_msg(clients[n % 2], iid, "claude", f"race-{n}")
        with lock:
            codes.append((r.status_code, r.json().get("error")))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(c for c, _ in codes).count(201) == 1
    assert all(e == "active_turn" for c, e in codes if c == 409) and len(codes) == 8
    active = [m for m in snap(seed, iid)["messages"] if m["state"] == "active"]
    assert len(active) == 1


# --- separate OS processes (the contract says "multiple balancer processes") ---------------

def _race_worker(db_path, issue_id, round_id, spec, barrier, results):
    """Runs in a spawned process: its own interpreter, app, and SQLite connections.

    Module level so the "spawn" start method (the only one on Windows) can import it.
    """
    try:
        client = TestClient(create_app(db_path))
        barrier.wait(timeout=60)  # release every process at the same moment
        r = client.post(f"/issues/{issue_id}/participants/claude/messages", json={
            "message_id": spec["message_id"], "kind": "continue", "round_id": round_id,
            "generation": 1, "owner": spec["owner"], "prompt": spec["prompt"]})
        results.put((r.status_code, r.json()))
    except BaseException as e:  # report instead of dying silently and leaving the parent waiting
        results.put(("error", repr(e)))


def _race_in_processes(db_path, issue_id, round_id, specs, timeout_s=90):
    """Start one spawned process per spec, release them together, collect their replies.

    Bounded everywhere: results are awaited with a deadline, joins have timeouts, and any
    process still alive afterwards is terminated, so a hung worker cannot hang the suite.
    """
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(len(specs))
    results = ctx.Queue()
    procs = [ctx.Process(target=_race_worker, args=(db_path, issue_id, round_id, spec, barrier, results),
                         daemon=True) for spec in specs]
    replies = []
    try:
        for p in procs:
            p.start()
        deadline = time.monotonic() + timeout_s
        while len(replies) < len(procs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pytest.fail(f"worker processes did not answer within {timeout_s}s: {replies}")
            try:
                replies.append(results.get(timeout=min(remaining, 2)))
            except queue_module.Empty:
                if not any(p.is_alive() for p in procs):
                    try:
                        replies.append(results.get(timeout=2))  # a reply may still be in flight
                    except queue_module.Empty:
                        pytest.fail(f"workers exited without answering: {replies}")
        for p in procs:
            p.join(timeout=15)
    finally:
        for p in procs:
            if p.is_alive():
                p.terminate()
                p.join(timeout=5)
        results.close()
    assert all(r[0] != "error" for r in replies), replies
    assert all(p.exitcode == 0 for p in procs), [p.exitcode for p in procs]
    return replies


def _seed_started_participant(db, clock):
    seed = make_client(db, clock)
    iid = open_issue(seed)["issue"]["id"]
    start_participant(seed, iid, "claude")
    return seed, iid, cur_round(seed, iid)


def test_two_processes_racing_distinct_messages_get_exactly_one_active_turn(db, clock):
    seed, iid, rnd = _seed_started_participant(db, clock)
    replies = _race_in_processes(db, iid, rnd, [
        {"message_id": "proc-a", "owner": "supervisor:procA:install-1", "prompt": "from process A"},
        {"message_id": "proc-b", "owner": "supervisor:procB:install-2", "prompt": "from process B"}])
    won = [body for status, body in replies if status == 201]
    lost = [body for status, body in replies if status == 409]
    assert len(won) == 1 and len(lost) == 1, replies
    assert won[0]["execute"] is True and won[0]["state"] == "active"
    assert lost[0]["error"] == "active_turn"
    assert lost[0]["active_message_id"] == won[0]["message_id"]
    s = snap(seed, iid)
    active = [m for m in s["messages"] if m["state"] == "active"]
    assert [m["message_id"] for m in active] == [won[0]["message_id"]]
    # the loser left nothing behind: one continue message, one attempt for it
    assert [m["message_id"] for m in s["messages"] if m["kind"] == "continue"] == [won[0]["message_id"]]
    assert len([a for a in s["attempts"] if a["message_id"] == won[0]["message_id"]]) == 1
    assert s["participants"]["claude"]["active_message_id"] == won[0]["message_id"]


def test_two_processes_replaying_the_same_message_reserve_one_attempt(db, clock):
    seed, iid, rnd = _seed_started_participant(db, clock)
    same = {"message_id": "proc-same", "owner": "supervisor:procA:install-1", "prompt": "identical"}
    replies = _race_in_processes(db, iid, rnd, [same, same])
    codes = sorted(status for status, _ in replies)
    assert codes == [200, 201], replies
    first = [body for status, body in replies if status == 201][0]
    replay = [body for status, body in replies if status == 200][0]
    assert first["execute"] is True and replay["execute"] is False  # a replay never starts a run
    assert first["attempt"]["attempt_id"] == replay["attempt"]["attempt_id"]
    s = snap(seed, iid)
    attempts = [a for a in s["attempts"] if a["message_id"] == "proc-same"]
    assert len(attempts) == 1 and attempts[0]["attempt_id"] == first["attempt"]["attempt_id"]
    assert [m["message_id"] for m in s["messages"] if m["kind"] == "continue"] == ["proc-same"]


def test_database_enforces_one_active_turn_without_the_app(db, client):
    open_issue(client)
    c = get_connection(db)
    sql = ("INSERT INTO messages (message_id, issue_id, pkey, thread_key, generation, kind, state,"
           " payload_hash) VALUES (?, 'i', 'claude', 'i:claude', 1, 'continue', 'active', 'h')")
    c.execute(sql, ("x1",))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(sql, ("x2",))
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_messages_active_turn", "idx_messages_one_start", "idx_attempts_unresolved",
            "idx_ready_current", "idx_integrations_one_active"} <= names


def test_stale_generation_round_and_version_are_409(client):
    iid = open_issue(client)["issue"]["id"]
    attempts = {k: start_participant(client, iid, k) for k in ("claude", "codex")}
    assert post_msg(client, iid, "claude", "g2", gen=2).json()["error"] == "stale_generation"
    assert post_msg(client, iid, "claude", "r9", rnd="r_9").json()["error"] == "stale_round"
    r = ready(client, iid, "claude", attempts["claude"], SHAS["claude"], expected_version=1)
    assert r.status_code == 409 and r.json()["error"] == "stale_version"
    assert r.json()["current_version"] == ver(client, iid)
    r = ready(client, iid, "claude", attempts["claude"], SHAS["claude"], base="9" * 40)
    assert r.status_code == 409 and r.json()["error"] == "stale_base"
    r = ready(client, iid, "claude", attempts["claude"], SHAS["claude"], gen=2)
    assert r.json()["error"] == "stale_generation"
    pid = propose(client, iid)
    assert decide(client, iid, pid, proposal_version=7).json()["error"] == "stale_proposal"
    assert decide(client, iid, pid, expected_version=1).json()["error"] == "stale_version"


def test_unconfirmed_profile_fields_stay_null(client):
    iid = open_issue(client)["issue"]["id"]
    start_participant(client, iid, "claude")
    a = post_msg(client, iid, "claude", "m1", profile={"model": "sonnet", "effort": "low"}) \
        .json()["attempt"]["attempt_id"]
    r = result(client, iid, a)  # no confirmed_profile, no context, no limits
    assert r.status_code == 200 and r.json()["profile_mismatch"] is False
    m = client.get(f"/issues/{iid}/participants/claude/messages").json()[-1]
    assert m["requested_profile"] == {"model": "sonnet", "effort": "low"}
    assert m["confirmed_profile"] == {"model": None, "effort": None}
    assert m["result"]["context"] is None and m["result"]["limits"] is None


def test_profile_mismatch_is_recorded_and_visible_but_never_blocks(client):
    iid, pid = accepted_issue(client)
    a = post_msg(client, iid, "claude", "m1", profile={"model": "opus", "effort": "high"})
    assert a.status_code == 201  # the thread is free after its start turn
    r = result(client, iid, a.json()["attempt"]["attempt_id"],
               confirmed_profile={"model": "sonnet", "effort": None})
    assert r.status_code == 200 and r.json()["profile_mismatch"] is True
    m = [m for m in snap(client, iid)["messages"] if m["message_id"] == "m1"][0]
    assert m["profile_mismatch"] is True
    assert m["requested_profile"]["model"] == "opus" and m["confirmed_profile"]["model"] == "sonnet"
    assert m["state"] == "done"
    assert post_msg(client, iid, "claude", "m2").status_code == 201  # next turn is not denied


# --- 3. handles, generations, start messages ---------------------------------------------------

def test_handles_are_fresh_per_participant_and_session_ids_unique(client):
    iid = open_issue(client)["issue"]["id"]
    start_participant(client, iid, "claude", session="same-session")
    r = post_msg(client, iid, "codex", "start-codex", kind="start")
    attempt = r.json()["attempt"]["attempt_id"]
    wrong_adapter = client.put(f"/issues/{iid}/participants/codex/handle", json={
        "generation": 1, "attempt_id": attempt,
        "handle": {"adapter_type": "claude", "session_id": "x", "cwd": "C:/wt/codex"}})
    assert wrong_adapter.status_code == 422
    ok = client.put(f"/issues/{iid}/participants/codex/handle", json={
        "generation": 1, "attempt_id": attempt,
        "handle": {"adapter_type": "codex", "session_id": "thread-1", "cwd": "C:/wt/codex"}})
    assert ok.status_code == 200
    s = snap(client, iid)["participants"]
    assert s["claude"]["handle"]["session_id"] != s["codex"]["handle"]["session_id"]
    other = open_issue(client, request_id="req-2")["issue"]["id"]
    r2 = post_msg(client, other, "claude", "start-claude", kind="start").json()["attempt"]["attempt_id"]
    reuse = client.put(f"/issues/{other}/participants/claude/handle", json={
        "generation": 1, "attempt_id": r2,
        "handle": {"adapter_type": "claude", "session_id": "same-session", "cwd": "C:/wt/x"}})
    assert reuse.status_code == 409 and reuse.json()["error"] == "session_in_use"


def test_no_attach_or_release_endpoints_exist(client):
    iid = open_issue(client)["issue"]["id"]
    for path in ("release", "attach"):
        r = client.post(f"/issues/{iid}/participants/claude/{path}", json={})
        assert r.status_code in (404, 405)
    s = snap(client, iid)
    for p in s["participants"].values():
        assert "driver" not in (p["handle"] or {}) and "interactive" not in (p["handle"] or {})


def test_handle_save_is_idempotent_and_fixed(client):
    iid = open_issue(client)["issue"]["id"]
    r = post_msg(client, iid, "claude", "start-claude", kind="start")
    attempt = r.json()["attempt"]["attempt_id"]
    body = {"generation": 1, "attempt_id": attempt,
            "handle": {"adapter_type": "claude", "session_id": "s-1", "cwd": "C:/wt/claude"}}
    url = f"/issues/{iid}/participants/claude/handle"
    assert client.put(url, json=body).status_code == 200
    v = ver(client, iid)
    assert client.put(url, json=body).status_code == 200  # replay after a supervisor restart
    assert ver(client, iid) == v
    bad = {**body, "handle": {**body["handle"], "session_id": "s-2"}}
    assert client.put(url, json=bad).json()["error"] == "handle_conflict"
    cwd = {**body, "handle": {**body["handle"], "cwd": "C:/elsewhere"}}
    assert client.put(url, json=cwd).json()["error"] == "handle_conflict"
    assert client.put(url, json={**body, "generation": 5}).json()["error"] == "stale_generation"


def test_handle_save_needs_the_start_attempt(client):
    iid = open_issue(client)["issue"]["id"]
    r = post_msg(client, iid, "claude", "start-claude", kind="start")
    h = {"adapter_type": "claude", "session_id": "s-1", "cwd": "C:/wt/claude"}
    url = f"/issues/{iid}/participants/claude/handle"
    assert client.put(url, json={"generation": 1, "handle": h}).status_code == 422
    wrong = client.put(url, json={"generation": 1, "attempt_id": "a_nope", "handle": h})
    assert wrong.status_code == 409 and wrong.json()["error"] == "stale_attempt"
    assert r.status_code == 201


def test_one_start_per_generation_and_start_lost_needs_explicit_bump(client):
    iid = open_issue(client)["issue"]["id"]
    first = post_msg(client, iid, "claude", "s1", kind="start")
    a1 = first.json()["attempt"]["attempt_id"]
    assert first.json()["handle"] is None
    # done without a handle is refused
    done = result(client, iid, a1)
    assert done.status_code == 409 and done.json()["error"] == "handle_missing"
    failed = result(client, iid, a1, status="failed", error="session_not_captured")
    assert failed.status_code == 200
    again = post_msg(client, iid, "claude", "s2", kind="start")
    assert again.status_code == 409 and again.json()["error"] == "start_exists"
    assert post_msg(client, iid, "claude", "c1").json()["error"] == "no_handle"
    blocked = snap(client, iid)["participants"]["claude"]["blocked"]
    assert blocked["reason"] == "start_lost"
    late = client.put(f"/issues/{iid}/participants/claude/handle", json={
        "generation": 1, "attempt_id": a1,
        "handle": {"adapter_type": "claude", "session_id": "late", "cwd": "C:/wt/claude"}})
    assert late.status_code == 409 and late.json()["error"] == "stale_attempt"
    bump = client.post(f"/issues/{iid}/participants/claude/generation", json={
        "expected_generation": 1, "reason": "start_lost", "expected_version": ver(client, iid)})
    assert bump.status_code == 200 and bump.json()["generation"] == 2 and bump.json()["handle"] is None
    new = post_msg(client, iid, "claude", "s3", gen=2, kind="start")
    assert new.status_code == 201


def test_generation_bump_checks(client):
    iid = open_issue(client)["issue"]["id"]
    start_participant(client, iid, "claude")
    body = {"expected_generation": 1, "reason": "manual", "expected_version": ver(client, iid)}
    url = f"/issues/{iid}/participants/claude/generation"
    assert client.post(url, json={**body, "expected_version": 1}).json()["error"] == "stale_version"
    assert client.post(url, json={**body, "expected_generation": 3}).json()["error"] == "stale_generation"
    assert client.post(url, json={**body, "reason": "nonsense"}).status_code == 422
    post_msg(client, iid, "claude", "m1")
    busy = client.post(url, json={**body, "expected_version": ver(client, iid)})
    assert busy.status_code == 409 and busy.json()["error"] == "active_turn"


def test_generation_replacement_persists_structured_handoff_and_preserves_partner(client):
    iid = open_issue(client)["issue"]["id"]
    claude_attempt = start_participant(client, iid, "claude", session="claude-old")
    codex_attempt = start_participant(client, iid, "codex", session="codex-stays")
    assert ready(client, iid, "claude", claude_attempt, SHAS["claude"]).status_code == 201
    assert ready(client, iid, "codex", codex_attempt, SHAS["codex"]).status_code == 201
    proposal_id = propose(client, iid, text="accepted proposal")
    assert decide(client, iid, proposal_id, kind="accept").status_code == 201
    before = snap(client, iid)
    handoff = {"claim": "the fix is correct", "evidence": "pytest: 4 passed",
               "correction": "replace float rounding", "decision": "use integer cents"}

    response = client.post(f"/issues/{iid}/participants/claude/generation", json={
        "expected_generation": 1, "reason": "repeated_error", "expected_version": ver(client, iid),
        "handoff": handoff,
    })

    assert response.status_code == 200, response.text
    after = snap(client, iid)
    assert after["participants"]["claude"]["generation"] == 2
    assert after["participants"]["claude"]["handle"] is None
    assert after["participants"]["claude"]["ready"] is None
    assert after["participants"]["codex"] == before["participants"]["codex"]
    assert after["generation_history"][-1] == {
        "participant": "claude", "from_generation": 1, "to_generation": 2,
        "reason": "repeated_error", "handoff": handoff,
    }
    assert after["ready_history"][-2]["state"] == "stale"
    assert after["ready_history"][-1]["state"] == "current"
    assert after["ready_history"][-1]["participant"] == "codex"
    assert after["proposals"][0]["state"] == "open"
    assert len(after["decisions"]) == len(before["decisions"]) == 1


def test_generation_replacement_requires_structured_handoff_for_issue_reasons(client):
    iid = open_issue(client)["issue"]["id"]
    url = f"/issues/{iid}/participants/claude/generation"
    body = {"expected_generation": 1, "reason": "context_threshold", "expected_version": ver(client, iid)}
    assert client.post(url, json=body).status_code == 422
    assert client.post(url, json={**body, "handoff": {"claim": "only claim"}}).status_code == 422


def test_generation_log_handoff_column_migrates_existing_database(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE generation_log (id INTEGER PRIMARY KEY, issue_id TEXT, pkey TEXT, "
                 "from_generation INTEGER, to_generation INTEGER, reason TEXT, created_at TEXT)")
    conn.commit()
    init_schema(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(generation_log)")}
    conn.close()
    assert "handoff" in columns


# --- 4. readiness, proposals, decisions -----------------------------------------------------------

def test_ready_accepts_distinct_shas_and_rejects_stale_attempts(client):
    iid = open_issue(client)["issue"]["id"]
    attempts = {k: start_participant(client, iid, k) for k in ("claude", "codex")}
    assert ready(client, iid, "claude", attempts["claude"], SHAS["claude"]).status_code == 201
    assert ready(client, iid, "codex", attempts["codex"], SHAS["codex"]).status_code == 201
    s = snap(client, iid)["participants"]
    assert s["claude"]["ready"]["source_commit_sha"] != s["codex"]["ready"]["source_commit_sha"]
    stale = ready(client, iid, "claude", "a_nope", SHAS["claude"])
    assert stale.status_code == 409 and stale.json()["error"] == "stale_attempt"
    cross = ready(client, iid, "claude", attempts["codex"], SHAS["claude"])
    assert cross.json()["error"] == "stale_attempt"
    null = client.post(f"/issues/{iid}/participants/claude/ready", json={
        "round_id": cur_round(client, iid), "generation": 1, "attempt_id": None,
        "source_commit_sha": SHAS["claude"],
        "expected_base_sha": BASE, "expected_version": ver(client, iid)})
    assert null.status_code == 422
    assert ready(client, iid, "claude", "a_x", "xyz").status_code == 422  # not a 40-hex sha


def test_ready_refused_while_attempt_active_or_not_latest(client):
    iid = open_issue(client)["issue"]["id"]
    first = start_participant(client, iid, "claude")
    busy = post_msg(client, iid, "claude", "m1").json()["attempt"]["attempt_id"]
    assert ready(client, iid, "claude", first, SHAS["claude"]).json()["error"] == "active_turn"
    assert result(client, iid, busy).status_code == 200
    assert ready(client, iid, "claude", first, SHAS["claude"]).json()["error"] == "stale_attempt"
    assert ready(client, iid, "claude", busy, SHAS["claude"]).status_code == 201


def test_accept_needs_every_participant_ready_and_clears_on_change(client):
    iid = open_issue(client)["issue"]["id"]
    attempts = {k: start_participant(client, iid, k) for k in ("claude", "codex")}
    assert ready(client, iid, "claude", attempts["claude"], SHAS["claude"]).status_code == 201
    pid = propose(client, iid)
    no = decide(client, iid, pid)
    assert no.status_code == 409 and no.json()["error"] == "not_ready"
    assert no.json()["missing"] == ["codex"]
    assert ready(client, iid, "codex", attempts["codex"], SHAS["codex"]).status_code == 201
    assert decide(client, iid, pid).status_code == 201
    state = lambda: [p for p in snap(client, iid)["proposals"] if p["proposal_id"] == pid][0]["state"]
    assert state() == "accepted"
    # a new ready supersedes the old one and clears the acceptance
    assert ready(client, iid, "codex", attempts["codex"], "e" * 40).status_code == 201
    assert state() == "open"
    assert decide(client, iid, pid).status_code == 201 and state() == "accepted"
    # a proposal revision clears it too, and the old version is stale
    rev = client.post(f"/issues/{iid}/proposals/{pid}/revisions", json={
        "text": "plan v2", "expected_proposal_version": 1, "expected_version": ver(client, iid)})
    assert rev.status_code == 200 and rev.json()["proposal_version"] == 2
    assert state() == "open"
    assert decide(client, iid, pid, proposal_version=1).json()["error"] == "stale_proposal"
    stale = client.post(f"/issues/{iid}/proposals/{pid}/revisions", json={
        "text": "x", "expected_proposal_version": 1, "expected_version": ver(client, iid)})
    assert stale.json()["error"] == "stale_proposal"


def test_new_round_opens_next_round_with_same_base(client):
    iid, pid = accepted_issue(client)
    first_round = cur_round(client, iid)
    r = decide(client, iid, pid, kind="new_round")
    assert r.status_code == 201
    s = snap(client, iid)
    assert s["round"]["number"] == 2 and s["round"]["state"] == "open"
    assert s["round"]["base_sha"] == BASE and s["round"]["id"] != first_round
    # the returned id is accepted by every route
    assert post_msg(client, iid, "claude", "after-round", rnd=s["round"]["id"]).status_code == 201
    assert s["participants"]["claude"]["ready"] is None  # readiness is per round


# --- 5. integration (two-phase records; third candidate sha) ----------------------------------------

def test_integration_start_validates_the_accepted_ready_set(client):
    iid, pid = accepted_issue(client)
    wrong = start_int(client, iid, pid, shas={"claude": SHAS["claude"], "codex": "e" * 40})
    assert wrong.status_code == 409 and wrong.json()["error"] == "ready_changed"
    assert wrong.json()["expected"] == SHAS
    missing = start_int(client, iid, pid, shas={"claude": SHAS["claude"]})
    assert missing.json()["error"] == "ready_changed"
    stale = start_int(client, iid, pid, base="9" * 40)
    assert stale.status_code == 409 and stale.json()["error"] == "stale_base"
    unaccepted = client.post(f"/issues/{iid}/integrations", json={
        "integration_request_id": "ir-x", "round_id": cur_round(client, iid), "proposal_id": "p_nope",
        "proposal_version": 1, "source_shas": SHAS, "expected_base_sha": BASE,
        "verification_argv": ARGV, "owner": OWNER, "expected_version": ver(client, iid)})
    assert unaccepted.status_code == 409 and unaccepted.json()["error"] == "not_accepted"
    ok = start_int(client, iid, pid)
    assert ok.status_code == 201
    body = ok.json()
    assert body["state"] == "merging" and body["verification_argv"] == ARGV
    assert body["source_shas"] == SHAS and body["lease"]["state"] == "running"
    again = client.post(f"/issues/{iid}/integrations", json={
        "integration_request_id": "ir-1", "round_id": cur_round(client, iid), "proposal_id": pid,
        "proposal_version": 1, "source_shas": SHAS, "expected_base_sha": BASE,
        "verification_argv": ARGV, "owner": OWNER, "expected_version": 1})
    assert again.status_code == 200 and again.json()["integration_id"] == body["integration_id"]
    second = start_int(client, iid, pid, req="ir-2")
    assert second.status_code == 409 and second.json()["error"] == "integration_active"


def test_full_integration_advances_base_and_opens_next_round(client):
    iid, pid = accepted_issue(client)
    first_round = cur_round(client, iid)
    gid = start_int(client, iid, pid).json()["integration_id"]
    c = candidate(client, iid, gid)
    assert c.status_code == 200 and c.json()["state"] == "testing"
    assert _post_tests(client, iid, gid).json()["state"] == "tested"
    adv = advance(client, iid, gid)
    assert adv.status_code == 200 and adv.json()["state"] == "advancing"
    assert snap(client, iid)["issue"]["base_sha"] == BASE  # intent only: the base has not moved yet
    res = int_result(client, iid, gid)
    assert res.status_code == 200 and res.json()["state"] == "advanced"
    s = snap(client, iid)
    assert s["issue"]["base_sha"] == CAND and s["issue"]["state"] == "open"
    assert s["round"]["number"] == 2 and s["round"]["state"] == "open"
    assert s["round"]["base_sha"] == CAND and s["round"]["id"] != first_round
    g = s["integrations"][0]
    # the candidate is a third commit, distinct from both participants' source commits
    assert g["candidate_sha"] == CAND and CAND not in g["source_shas"].values()
    assert g["source_shas"] == SHAS and g["lease"]["state"] == "released"
    assert g["merge_output"]["stdout"] == "merged" and g["ref_update_output"]["exit_code"] == 0
    assert g["tests"]["stdout"] == "42 passed" and g["tests"]["exit_code"] == 0
    assert g["tests"]["duration_s"] == 1.5 and g["verification_argv"] == ARGV
    closed = client.post(f"/issues/{iid}/close", json={"reason": "done", "expected_version": ver(client, iid)})
    assert closed.status_code == 200 and snap(client, iid)["issue"]["state"] == "closed"
    assert post_msg(client, iid, "claude", "late").json()["error"] == "issue_closed"


def test_integration_steps_are_idempotent(client):
    iid, pid = accepted_issue(client)
    gid = start_int(client, iid, pid).json()["integration_id"]
    assert candidate(client, iid, gid).status_code == 200
    assert candidate(client, iid, gid).status_code == 200  # same content: replay
    assert candidate(client, iid, gid, sha="f" * 40).json()["error"] == "result_conflict"
    assert _post_tests(client, iid, gid).status_code == 200
    v = ver(client, iid)
    assert _post_tests(client, iid, gid).status_code == 200 and ver(client, iid) == v
    assert _post_tests(client, iid, gid, exit_code=0, status="fail").json()["error"] == "result_conflict"
    assert advance(client, iid, gid).status_code == 200
    assert advance(client, iid, gid).status_code == 200  # replay while advancing
    assert int_result(client, iid, gid).status_code == 200
    v = ver(client, iid)
    assert int_result(client, iid, gid).status_code == 200 and ver(client, iid) == v
    assert int_result(client, iid, gid, status="failed").json()["error"] == "result_conflict"


def test_verification_argv_is_exact_and_outputs_persist(client):
    iid, pid = accepted_issue(client)
    gid = start_int(client, iid, pid).json()["integration_id"]
    assert _post_tests(client, iid, gid).json()["error"] == "bad_stage"  # not merged yet
    assert candidate(client, iid, gid).status_code == 200
    assert _post_tests(client, iid, gid, argv=["python", "-m", "pytest"]).json()["error"] == "argv_mismatch"
    assert _post_tests(client, iid, gid, tested="9" * 40).json()["error"] == "tested_sha_mismatch"
    assert _post_tests(client, iid, gid, status="pass", exit_code=1).status_code == 422
    assert advance(client, iid, gid).json()["error"] == "bad_stage"  # not tested yet
    assert _post_tests(client, iid, gid).status_code == 200
    g = snap(client, iid)["integrations"][0]
    assert g["tests"]["argv"] == ARGV and g["tests"]["tested_sha"] == CAND


def test_empty_verification_argv_blocks_success_but_not_feedback(client):
    iid, pid = accepted_issue(client)
    gid = start_int(client, iid, pid, argv=[]).json()["integration_id"]
    assert snap(client, iid)["integrations"][0]["verification_argv"] == []
    assert candidate(client, iid, gid).status_code == 200
    blocked = _post_tests(client, iid, gid, argv=[])
    assert blocked.status_code == 409 and blocked.json()["error"] == "verification_empty"
    assert advance(client, iid, gid).status_code == 409
    fail = _post_tests(client, iid, gid, argv=[], status="fail", exit_code=2)
    assert fail.status_code == 200 and fail.json()["state"] == "failed"
    assert snap(client, iid)["issue"]["base_sha"] == BASE


def test_conflict_and_failed_tests_record_results_and_never_move_base(client):
    iid, pid = accepted_issue(client)
    gid = start_int(client, iid, pid).json()["integration_id"]
    bad = candidate(client, iid, gid, status="conflict", detail="CONFLICT in a.py")
    assert bad.status_code == 200 and bad.json()["state"] == "conflict"
    s = snap(client, iid)
    assert s["issue"]["base_sha"] == BASE and s["round"]["number"] == 1
    assert s["integrations"][0]["error"] == "CONFLICT in a.py"
    assert s["integrations"][0]["merge_output"]["stdout"] == "merged"
    # the conflict ended the integration, so another may start; this one fails its tests
    gid2 = start_int(client, iid, pid, req="ir-2").json()["integration_id"]
    assert candidate(client, iid, gid2).status_code == 200
    failed = _post_tests(client, iid, gid2, status="fail", exit_code=1)
    assert failed.json()["state"] == "failed"
    s = snap(client, iid)
    assert s["issue"]["base_sha"] == BASE and s["round"]["number"] == 1
    g2 = [g for g in s["integrations"] if g["integration_id"] == gid2][0]
    assert g2["candidate_sha"] == CAND and g2["tests"]["status"] == "fail"
    assert advance(client, iid, gid2).status_code == 409


def test_diverged_base_is_recorded_and_not_moved(db, client):
    iid, pid = accepted_issue(client)
    gid = start_int(client, iid, pid).json()["integration_id"]
    candidate(client, iid, gid)
    _post_tests(client, iid, gid)
    assert advance(client, iid, gid).status_code == 200
    c = get_connection(db)
    c.execute("UPDATE issues SET base_sha=? WHERE id=?", ("9" * 40, iid))  # someone else moved it
    res = int_result(client, iid, gid)
    assert res.status_code == 409 and res.json()["error"] == "stale_base"
    s = snap(client, iid)
    assert s["issue"]["base_sha"] == "9" * 40
    assert s["integrations"][0]["state"] == "failed" and s["integrations"][0]["error"] == "diverged"
    assert s["round"]["number"] == 1


def test_close_is_separate_and_blocked_by_active_integration(client):
    iid, pid = accepted_issue(client)
    gid = start_int(client, iid, pid).json()["integration_id"]
    r = client.post(f"/issues/{iid}/close", json={"reason": "x", "expected_version": ver(client, iid)})
    assert r.status_code == 409 and r.json()["error"] == "integration_active"
    candidate(client, iid, gid, status="conflict", detail="c")
    r = client.post(f"/issues/{iid}/close", json={"reason": "x", "expected_version": 1})
    assert r.json()["error"] == "stale_version"


# --- 6. leases, fencing, recovery --------------------------------------------------------------------

def test_expired_lease_is_suspected_not_terminal_and_keeps_the_slot(client, clock):
    iid = open_issue(client)["issue"]["id"]
    attempt = active_message(client, iid)
    clock.advance(TTL + 1)
    s = snap(client, iid)
    a = [a for a in s["attempts"] if a["attempt_id"] == attempt][0]
    assert a["state"] == "owner_lost_suspected"
    assert s["participants"]["claude"]["blocked"]["reason"] == "owner_lost_suspected"
    r = client.post(f"/issues/{iid}/messages/m1/retry", json={"expected_attempt": attempt, "owner": OWNER2})
    assert r.status_code == 409 and r.json()["error"] == "termination_unconfirmed"
    r = client.post(f"/issues/{iid}/messages/m1/abandon", json={"expected_attempt": attempt})
    assert r.status_code == 409 and r.json()["error"] == "termination_unconfirmed"
    r = post_msg(client, iid, "claude", "m2")
    assert r.status_code == 409 and r.json()["error"] == "active_turn"
    assert r.json()["attempt_state"] == "owner_lost_suspected"
    assert ready(client, iid, "claude", attempt, SHAS["claude"]).json()["error"] == "active_turn"
    bump = client.post(f"/issues/{iid}/participants/claude/generation", json={
        "expected_generation": 1, "reason": "manual", "expected_version": ver(client, iid)})
    assert bump.json()["error"] == "active_turn"
    # nothing time driven ever freed it, even much later
    clock.advance(100 * TTL)
    assert post_msg(client, iid, "claude", "m3").json()["error"] == "active_turn"
    # the other participant is unaffected
    start_participant(client, iid, "codex")
    assert post_msg(client, iid, "codex", "c1").status_code == 201


def test_balancer_restart_keeps_attempt_and_heartbeat_renews(db, clock):
    c1 = make_client(db, clock)
    iid = open_issue(c1)["issue"]["id"]
    attempt = active_message(c1, iid)
    handle = snap(c1, iid)["participants"]["claude"]["handle"]
    clock.advance(TTL // 2)
    c2 = make_client(db, clock)  # restart while the MCP owner and the CLI child live
    s = snap(c2, iid)
    assert [a["state"] for a in s["attempts"] if a["attempt_id"] == attempt] == ["running"]
    assert s["participants"]["claude"]["handle"] == handle
    hb = c2.post(f"/issues/{iid}/attempts/{attempt}/heartbeat", json={"owner": OWNER})
    assert hb.status_code == 200 and hb.json()["state"] == "running"
    assert result(c2, iid, attempt).status_code == 200


def test_heartbeat_after_downtime_revives_a_suspected_attempt(db, clock):
    c1 = make_client(db, clock)
    iid = open_issue(c1)["issue"]["id"]
    attempt = active_message(c1, iid)
    clock.advance(5 * TTL)  # the balancer was down longer than the lease
    c2 = make_client(db, clock)
    assert [a["state"] for a in snap(c2, iid)["attempts"] if a["attempt_id"] == attempt] == \
        ["owner_lost_suspected"]
    hb = c2.post(f"/issues/{iid}/attempts/{attempt}/heartbeat", json={"owner": OWNER})
    assert hb.status_code == 200 and hb.json()["state"] == "running"
    assert c2.post(f"/issues/{iid}/attempts/{attempt}/heartbeat", json={"owner": OWNER2}) \
        .json()["error"] == "not_owner"
    assert result(c2, iid, attempt).status_code == 200


def test_process_identity_is_reported_once(client):
    iid = open_issue(client)["issue"]["id"]
    attempt = active_message(client, iid)
    url = f"/issues/{iid}/attempts/{attempt}/process"
    assert client.post(url, json={"owner": OWNER, "process_identity": IDENT}).status_code == 200
    assert client.post(url, json={"owner": OWNER, "process_identity": IDENT}).status_code == 200
    other = {**IDENT, "pid": 43}
    assert client.post(url, json={"owner": OWNER, "process_identity": other}).json()["error"] == "identity_conflict"
    assert client.post(url, json={"owner": OWNER2, "process_identity": IDENT}).json()["error"] == "not_owner"
    bad = {k: v for k, v in IDENT.items() if k != "start_time"}
    assert client.post(url, json={"owner": OWNER, "process_identity": bad}).status_code == 422
    a = [a for a in snap(client, iid)["attempts"] if a["attempt_id"] == attempt][0]
    assert a["process_identity"] == IDENT


def test_termination_confirmed_rules(client, clock):
    iid = open_issue(client)["issue"]["id"]
    attempt = active_message(client, iid)
    client.post(f"/issues/{iid}/attempts/{attempt}/process", json={"owner": OWNER, "process_identity": IDENT})
    clock.advance(TTL + 1)
    assert confirm(client, iid, attempt, owner=OWNER2).json()["error"] == "not_owner"
    r = confirm(client, iid, attempt, method="never_started", identity=None)
    assert r.status_code == 409 and r.json()["error"] == "identity_recorded"
    assert confirm(client, iid, attempt, identity={**IDENT, "pid": 43}).json()["error"] == "identity_mismatch"
    ok = confirm(client, iid, attempt)
    assert ok.status_code == 200 and ok.json()["state"] == "terminated"
    again = confirm(client, iid, attempt)
    assert again.status_code == 200 and again.json()["termination_confirmed_at"] == ok.json()["termination_confirmed_at"]
    assert confirm(client, iid, attempt, method="exited").json()["error"] == "confirmation_conflict"
    hb = client.post(f"/issues/{iid}/attempts/{attempt}/heartbeat", json={"owner": OWNER})
    assert hb.status_code == 409 and hb.json()["error"] == "terminated"
    # the slot stays held until a retry or abandon
    assert post_msg(client, iid, "claude", "m2").json()["error"] == "active_turn"


def test_never_started_confirmation_needs_no_identity(client, clock):
    iid = open_issue(client)["issue"]["id"]
    attempt = active_message(client, iid)
    assert confirm(client, iid, attempt, method="terminated", identity=None).status_code == 422
    r = confirm(client, iid, attempt, method="never_started", identity=None)
    assert r.status_code == 200 and r.json()["state"] == "terminated"


def test_retry_after_confirmed_termination_supersedes_and_late_result_is_409(client, clock):
    iid = open_issue(client)["issue"]["id"]
    a1 = active_message(client, iid)
    live = client.post(f"/issues/{iid}/messages/m1/retry", json={"expected_attempt": a1, "owner": OWNER2})
    assert live.status_code == 409 and live.json()["error"] == "attempt_live"
    clock.advance(TTL + 1)
    assert confirm(client, iid, a1).status_code == 200
    wrong = client.post(f"/issues/{iid}/messages/m1/retry", json={"expected_attempt": "a_zzz", "owner": OWNER2})
    assert wrong.status_code == 409 and wrong.json()["error"] == "stale_attempt"
    r = client.post(f"/issues/{iid}/messages/m1/retry", json={"expected_attempt": a1, "owner": OWNER2})
    assert r.status_code == 201
    body = r.json()
    a2 = body["attempt"]["attempt_id"]
    assert body["execute"] is True and a2 != a1 and body["attempt"]["number"] == 2
    assert body["attempt"]["owner"] == OWNER2 and body["handle"]["session_id"].startswith("sess-")
    replay = client.post(f"/issues/{iid}/messages/m1/retry", json={"expected_attempt": a1, "owner": OWNER2})
    assert replay.status_code == 200 and replay.json()["execute"] is False
    assert replay.json()["attempt"]["attempt_id"] == a2
    late = result(client, iid, a1)
    assert late.status_code == 409 and late.json()["error"] == "stale_attempt"
    assert late.json()["current_attempt_id"] == a2
    assert result(client, iid, a2, owner=OWNER2).status_code == 200
    states = {a["attempt_id"]: a["state"] for a in snap(client, iid)["attempts"]}
    assert states[a1] == "superseded" and states[a2] == "done"


def test_retry_after_a_failed_result_reopens_the_message(client):
    iid = open_issue(client)["issue"]["id"]
    start_participant(client, iid, "codex")
    c1 = post_msg(client, iid, "codex", "c1").json()["attempt"]["attempt_id"]
    assert result(client, iid, c1, status="failed", error="boom").status_code == 200
    again = client.post(f"/issues/{iid}/messages/c1/retry", json={"expected_attempt": c1, "owner": OWNER})
    assert again.status_code == 201 and again.json()["execute"] is True
    assert again.json()["state"] == "active" and again.json()["attempt"]["number"] == 2
    a1 = active_message(client, iid)
    assert result(client, iid, a1, status="failed", error="boom").status_code == 200
    assert post_msg(client, iid, "claude", "m2").status_code == 201  # slot was free: now held by m2
    busy = client.post(f"/issues/{iid}/messages/m1/retry", json={"expected_attempt": a1, "owner": OWNER})
    assert busy.status_code == 409 and busy.json()["error"] == "active_turn"
    snapshot = snap(client, iid)
    # the failed superseding step rolled back completely
    assert [a["state"] for a in snapshot["attempts"] if a["attempt_id"] == a1] == ["failed"]


def test_abandon_is_terminal_for_message_and_attempt(client, clock):
    iid = open_issue(client)["issue"]["id"]
    a1 = active_message(client, iid)
    live = client.post(f"/issues/{iid}/messages/m1/abandon", json={"expected_attempt": a1})
    assert live.status_code == 409 and live.json()["error"] == "attempt_live"
    clock.advance(TTL + 1)
    assert client.post(f"/issues/{iid}/messages/m1/abandon", json={"expected_attempt": a1}) \
        .json()["error"] == "termination_unconfirmed"
    assert confirm(client, iid, a1).status_code == 200
    assert client.post(f"/issues/{iid}/messages/m1/abandon", json={"expected_attempt": "a_zzz"}) \
        .json()["error"] == "stale_attempt"
    ab = client.post(f"/issues/{iid}/messages/m1/abandon", json={"expected_attempt": a1})
    assert ab.status_code == 200 and ab.json()["state"] == "abandoned"
    assert ab.json()["attempt_state"] == "abandoned"
    s = snap(client, iid)
    assert [m["state"] for m in s["messages"] if m["message_id"] == "m1"] == ["abandoned"]
    assert [a["state"] for a in s["attempts"] if a["attempt_id"] == a1] == ["abandoned"]
    late = result(client, iid, a1)  # right owner, content that would have been accepted
    assert late.status_code == 409 and late.json()["error"] == "abandoned"
    assert result(client, iid, a1, owner=OWNER2).json()["error"] == "abandoned"
    assert result(client, iid, a1, status="failed", error="x").json()["error"] == "abandoned"
    retry = client.post(f"/issues/{iid}/messages/m1/retry", json={"expected_attempt": a1, "owner": OWNER})
    assert retry.status_code == 409 and retry.json()["error"] == "abandoned"
    assert client.post(f"/issues/{iid}/messages/m1/abandon", json={"expected_attempt": a1}).status_code == 200
    assert post_msg(client, iid, "claude", "m2").status_code == 201  # the thread is free again


def test_close_lists_attempts_without_confirmed_termination(client, clock):
    iid = open_issue(client)["issue"]["id"]
    attempt = active_message(client, iid)
    clock.advance(TTL + 1)
    r = client.post(f"/issues/{iid}/close", json={"reason": "giving up", "expected_version": ver(client, iid)})
    assert r.status_code == 200 and r.json()["unconfirmed_attempts"] == [attempt]


# --- 7. integration worker fencing --------------------------------------------------------------------

def takeover(client, iid, gid, expected_owner=OWNER, owner=OWNER2, stage="merging"):
    return client.post(f"/issues/{iid}/integrations/{gid}/takeover", json={
        "expected_owner": expected_owner, "owner": owner, "expected_stage": stage})


def int_confirm(client, iid, gid, owner=OWNER, method="terminated", identity=IDENT):
    return client.post(f"/issues/{iid}/integrations/{gid}/termination_confirmed",
                       json={"owner": owner, "process_identity": identity, "method": method})


def test_integration_takeover_waits_for_confirmed_termination(client, clock):
    iid, pid = accepted_issue(client)
    gid = start_int(client, iid, pid).json()["integration_id"]
    client.post(f"/issues/{iid}/integrations/{gid}/process", json={"owner": OWNER, "process_identity": IDENT})
    live = takeover(client, iid, gid)
    assert live.status_code == 409 and live.json()["error"] == "lease_live"
    clock.advance(TTL + 1)
    g = snap(client, iid)["integrations"][0]
    assert g["lease"]["state"] == "owner_lost_suspected" and g["state"] == "merging"
    assert takeover(client, iid, gid).json()["error"] == "termination_unconfirmed"
    assert start_int(client, iid, pid, req="ir-2").json()["error"] == "integration_active"
    assert int_confirm(client, iid, gid, owner=OWNER2).json()["error"] == "not_owner"
    assert int_confirm(client, iid, gid, identity={**IDENT, "pid": 7}).json()["error"] == "identity_mismatch"
    assert int_confirm(client, iid, gid).json()["lease_state"] == "terminated"
    assert int_confirm(client, iid, gid).status_code == 200  # idempotent
    hb = client.post(f"/issues/{iid}/integrations/{gid}/heartbeat", json={"owner": OWNER})
    assert hb.json()["error"] == "terminated"
    assert takeover(client, iid, gid, expected_owner="someone").json()["error"] == "owner_mismatch"
    assert takeover(client, iid, gid, stage="testing").json()["error"] == "stage_mismatch"
    ok = takeover(client, iid, gid)
    assert ok.status_code == 200 and ok.json()["lease"]["owner"] == OWNER2
    assert ok.json()["lease"]["state"] == "running"
    assert takeover(client, iid, gid).status_code == 200  # replay
    assert snap(client, iid)["integrations"][0]["state"] == "merging"


def test_integration_stage_recovery_keeps_candidate_and_never_reruns(client, clock):
    iid, pid = accepted_issue(client)
    gid = start_int(client, iid, pid).json()["integration_id"]
    assert candidate(client, iid, gid).status_code == 200  # merge done, worker then dies
    clock.advance(TTL + 1)
    assert int_confirm(client, iid, gid).status_code == 200
    assert takeover(client, iid, gid, stage="testing").status_code == 200
    g = snap(client, iid)["integrations"][0]
    assert g["state"] == "testing" and g["candidate_sha"] == CAND
    assert g["merge_output"]["stdout"] == "merged" and g["verification_argv"] == ARGV
    assert _post_tests(client, iid, gid, owner=OWNER).json()["error"] == "not_owner"  # the fenced worker
    # the merge is never redone: a replay of the stored candidate is a no-op, another sha is refused
    assert candidate(client, iid, gid, owner=OWNER2).status_code == 200
    assert candidate(client, iid, gid, owner=OWNER2, sha="f" * 40).json()["error"] == "result_conflict"
    assert snap(client, iid)["integrations"][0]["candidate_sha"] == CAND
    assert _post_tests(client, iid, gid, owner=OWNER2).status_code == 200
    clock.advance(TTL + 1)  # worker 2 dies after the tests, before advance
    assert int_confirm(client, iid, gid, owner=OWNER2, identity={**IDENT, "pid": 99}).status_code == 200
    assert takeover(client, iid, gid, expected_owner=OWNER2, owner="supervisor:host3:i3",
                    stage="tested").status_code == 200
    g = snap(client, iid)["integrations"][0]
    assert g["state"] == "tested" and g["tests"]["status"] == "pass"  # completed tests are kept
    assert _post_tests(client, iid, gid, owner="supervisor:host3:i3").status_code == 200  # replay only
    assert advance(client, iid, gid, owner="supervisor:host3:i3").status_code == 200
    assert int_result(client, iid, gid, owner="supervisor:host3:i3").status_code == 200
    assert snap(client, iid)["issue"]["base_sha"] == CAND


def test_integration_left_advancing_reconciles_with_an_idempotent_result(client, clock):
    iid, pid = accepted_issue(client)
    gid = start_int(client, iid, pid).json()["integration_id"]
    candidate(client, iid, gid)
    _post_tests(client, iid, gid)
    assert advance(client, iid, gid).status_code == 200
    # worker dies after the git ref moved and before the result was posted
    clock.advance(TTL + 1)
    g = snap(client, iid)["integrations"][0]
    assert g["state"] == "advancing" and g["lease"]["state"] == "owner_lost_suspected"
    assert snap(client, iid)["issue"]["base_sha"] == BASE
    assert int_confirm(client, iid, gid).status_code == 200
    assert takeover(client, iid, gid, stage="advancing").status_code == 200
    # the successor read the ref (== candidate) and posts the result; replays are harmless
    assert int_result(client, iid, gid, owner=OWNER2).status_code == 200
    assert int_result(client, iid, gid, owner=OWNER2).status_code == 200
    s = snap(client, iid)
    assert s["issue"]["base_sha"] == CAND and s["integrations"][0]["state"] == "advanced"
    assert s["round"]["number"] == 2


# --- 8. compatibility, schema, dashboard ------------------------------------------------------------------

def test_init_schema_is_idempotent_and_adds_issue_tables(tmp_path):
    path = str(tmp_path / "m.db")
    conn = get_connection(path)
    init_schema(conn)
    init_schema(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tasks", "usage", "capabilities", "incidents", "issues", "rounds", "participants",
            "handles", "messages", "attempts", "proposals", "ready", "decisions",
            "integrations"} <= names


def test_events_stream_includes_issues_and_keeps_v1_keys(client):
    open_issue(client, title="Visible in dashboard")
    client.post("/tasks", json={"description": "legacy task"})
    with client.stream("GET", "/events", params={"once": "true"}) as r:
        text = "".join(r.iter_text())
    assert text.startswith("data: ")
    import json
    data = json.loads(text[len("data: "):].strip())
    assert {"tasks", "usage", "incidents", "issues"} <= set(data)
    assert data["issues"][0]["issue"]["title"] == "Visible in dashboard"
    assert data["tasks"][0]["description"] == "legacy task"


def test_dashboard_shows_issue_sections(client):
    html = client.get("/dashboard").text
    for needle in ("renderIssues", "Acceptance criteria", "Participants", "Proposals", "Decisions",
                   "Integrations", "profile_mismatch", "BLOCKED", "source_commit_sha",
                   "candidate_sha", "tests", "opened_from"):
        assert needle in html, needle


def test_legacy_tasks_still_work_next_to_issues(client):
    open_issue(client)
    t = client.post("/tasks", json={"description": "x", "model_hint": "claude"}).json()
    assert client.post(f"/tasks/{t['id']}/claim", json={"agent": "claude"}).json() == {"claimed": True}
    assert client.post(f"/tasks/{t['id']}/complete", json={"result": "done"}).status_code == 200
    assert client.get(f"/tasks/{t['id']}").json()["result"] == "done"
    assert client.post("/tasks", json={"description": ""}).status_code == 422  # v1 error shape kept
