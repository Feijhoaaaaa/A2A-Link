"""Request-level test-result delivery outbox and aggregate terminal gating.

Contract: docs/superpowers/specs/2026-11-27-background-test-jobs-api.md ("Result delivery").
Issue turns are driven through the HTTP issue routes; nothing here runs a CLI.
"""
import hashlib
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from balancer import repository as repo
from balancer.app import create_app
from balancer.db import get_connection

ADMIN = "admin-secret"
BASE = "a" * 40
OWNER = "supervisor:host:install"
PROVIDER_SESSION = "provider-session-SECRET-0123"
HANDLE_CWD = "C:/secret-worktree/claude"
PROFILE = {"model": "model-x", "effort": "high"}
TAR = b"snapshot-bytes"
TAR_SHA = hashlib.sha256(TAR).hexdigest()
ARGV = ["python", "-m", "pytest", "-q"]


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    monkeypatch.setenv("A2A_LINK_RUNNER_REGISTRATION_TOKEN", ADMIN)
    return str(tmp_path / "delivery.db")


@pytest.fixture
def client(db_path):
    return TestClient(create_app(db_path))


@pytest.fixture
def conn(db_path, client):
    with closing(get_connection(db_path)) as connection:
        yield connection


# --- helpers ------------------------------------------------------------------------

def snapshot(client, issue_id):
    r = client.get(f"/issues/{issue_id}")
    assert r.status_code == 200, r.text
    return r.json()


def message_body(client, issue_id, message_id, *, kind="continue", generation=1, prompt="do it"):
    return {"message_id": message_id, "kind": kind,
            "round_id": snapshot(client, issue_id)["round"]["id"], "generation": generation,
            "requested_profile": PROFILE, "profile_source": "task_type", "owner": OWNER,
            "prompt": prompt}


def send(client, issue_id, body, participant="claude"):
    return client.post(f"/issues/{issue_id}/participants/{participant}/messages", json=body)


def post_result(client, issue_id, attempt_id, status="done"):
    body = {"owner": OWNER, "status": status, "text": "ok", "exit_code": 0}
    if status == "failed":
        body |= {"exit_code": 1, "error": f"resume returned another id than {PROVIDER_SESSION}"}
    r = client.post(f"/issues/{issue_id}/attempts/{attempt_id}/result", json=body)
    assert r.status_code == 200, r.text


def managed_turn(client, request_id="issue-1", *, save_handle=True):
    """A Link-managed Claude start turn that is still running: the one calling start_tests."""
    opened = client.post("/issues", json={
        "request_id": request_id, "title": "t", "goal": "g", "acceptance": [], "cwd": "C:/repo",
        "base_sha": BASE, "opened_from": "claude"})
    assert opened.status_code == 201, opened.text
    issue_id = opened.json()["issue"]["id"]
    started = send(client, issue_id,
                   message_body(client, issue_id, f"start-{issue_id}", kind="start"))
    assert started.status_code == 201, started.text
    attempt_id = started.json()["attempt"]["attempt_id"]
    if save_handle:
        saved = client.put(f"/issues/{issue_id}/participants/claude/handle", json={
            "generation": 1, "attempt_id": attempt_id,
            "handle": {"adapter_type": "claude", "session_id": f"{PROVIDER_SESSION}-{issue_id}",
                       "cwd": HANDLE_CWD}})
        assert saved.status_code == 200, saved.text
    return {"issue_id": issue_id, "participant": "claude", "generation": 1,
            "origin_attempt_id": attempt_id}


def end_origin_turn(client, ref):
    post_result(client, ref["issue_id"], ref["origin_attempt_id"])


def request_body(ref, request_id="req-1", targets=("linux",)):
    return {"request_id": request_id, "snapshot_sha256": TAR_SHA, "profile": "python-pytest",
            "targets": list(targets), "session_ref": ref}


def create_request(client, ref, request_id="req-1", targets=("linux",)):
    assert client.put(f"/test-snapshots/{TAR_SHA}", content=TAR).status_code in (200, 201)
    return client.post("/test-requests", json=request_body(ref, request_id, targets))


def created(client, ref, request_id="req-1", targets=("linux",)):
    r = create_request(client, ref, request_id, targets)
    assert r.status_code == 201, r.text
    return r.json()


def claim(conn, label):
    job = repo.claim_test_job(conn, f"runner-{label}", [label], 60)
    assert job is not None
    return job


def finish(conn, job, status="succeeded", **details):
    return repo.finish_test_job(conn, job["job_id"], job["runner_id"], job["lease_token"],
                                {"status": status, **details})


def expire(conn, job):
    conn.execute("UPDATE test_jobs SET lease_expires_at='2000-01-01T00:00:00.000000Z'"
                 " WHERE job_id=?", (job["job_id"],))


def rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM test_deliveries ORDER BY rowid")]


def detail(client, request_id="req-1"):
    r = client.get(f"/test-requests/{request_id}")
    assert r.status_code == 200, r.text
    return r.json()


def ready(client, **params):
    r = client.get("/test-deliveries", params=params)
    assert r.status_code == 200, r.text
    return r.json()["deliveries"]


def completed_delivery(client, conn, ref, request_id="req-1"):
    """A single passing job whose origin turn has ended: one dispatchable delivery."""
    created(client, ref, request_id)
    finish(conn, claim(conn, "linux"), exit_code=0)
    end_origin_turn(client, ref)
    [item] = ready(client)
    return item


def reserve(client, item):
    return send(client, item["issue_id"], {**item["reservation"], "owner": OWNER},
                item["participant"])


# --- aggregate terminal gating ------------------------------------------------------

@pytest.mark.parametrize("sibling_state", ["queued", "preparing", "running"])
def test_lost_child_with_an_active_sibling_is_nonterminal_and_enqueues_nothing(
        client, conn, sibling_state):
    created(client, managed_turn(client), targets=["linux", "windows"])
    lost = claim(conn, "linux")
    if sibling_state != "queued":
        sibling = claim(conn, "windows")
        if sibling_state == "running":
            repo.heartbeat_test_job(conn, sibling["job_id"], sibling["runner_id"],
                                    sibling["lease_token"], phase="running")
    expire(conn, lost)

    view = detail(client)

    assert [job["state"] for job in view["jobs"]] == ["lost", sibling_state]
    assert view["state"] == "running"
    assert view["delivery"] is None
    assert rows(conn) == []


def test_last_final_child_enqueues_exactly_one_delivery(client, conn):
    created(client, managed_turn(client), targets=["linux", "windows"])
    lost, other = claim(conn, "linux"), claim(conn, "windows")
    expire(conn, lost)
    assert detail(client)["state"] == "running"

    finish(conn, other, exit_code=0)

    [row] = rows(conn)
    assert row["request_state"] == "lost"
    assert [job["state"] for job in json.loads(row["summary"])["jobs"]] == ["lost", "succeeded"]
    view = detail(client)
    assert view["state"] == "lost"
    assert view["delivery"]["state"] == "pending"
    assert view["delivery"]["message_id"] == row["message_id"]


@pytest.mark.parametrize("outcomes, aggregate", [
    ([("succeeded", {})], "succeeded"),
    ([("failed", {"exit_code": 1})], "failed"),
    ([("succeeded", {}), ("failed", {"exit_code": 1})], "partial"),
    ([("failed", {"timed_out": True})], "partial"),
    ([("cancelled", {})], "cancelled"),
])
def test_terminal_aggregate_is_preserved_in_the_delivery(client, conn, outcomes, aggregate):
    labels = ["linux", "windows"][:len(outcomes)]
    created(client, managed_turn(client), targets=labels)
    jobs = [claim(conn, label) for label in labels]
    for index, (job, (status, extra)) in enumerate(zip(jobs, outcomes)):
        assert len(rows(conn)) == 0, f"enqueued before child {index} finished"
        finish(conn, job, status, **extra)

    [row] = rows(conn)
    assert row["request_state"] == aggregate
    assert json.loads(row["summary"])["state"] == aggregate
    assert detail(client)["state"] == aggregate


def test_queued_cancel_enqueues_once_and_replayed_cancel_adds_nothing(client, conn):
    created(client, managed_turn(client), targets=["linux", "windows"])

    for _ in range(2):
        assert client.post("/test-requests/req-1/cancel").status_code == 200

    [row] = rows(conn)
    assert row["request_state"] == "cancelled"


def test_cancel_waits_for_the_active_child_before_enqueueing(client, conn):
    created(client, managed_turn(client), targets=["linux", "windows"])
    active = claim(conn, "linux")
    assert client.post("/test-requests/req-1/cancel").status_code == 200
    assert rows(conn) == []

    finish(conn, active, "cancelled")

    assert [row["request_state"] for row in rows(conn)] == ["cancelled"]


def test_delivery_poll_reconciles_an_expired_lease_without_other_traffic(client, conn):
    ref = managed_turn(client)
    created(client, ref)
    expire(conn, claim(conn, "linux"))
    end_origin_turn(client, ref)

    [item] = ready(client)

    assert item["summary"]["state"] == "lost"
    assert len(rows(conn)) == 1


def test_startup_reconciliation_enqueues_the_lost_result(client, conn, db_path):
    created(client, managed_turn(client))
    expire(conn, claim(conn, "linux"))

    with TestClient(create_app(db_path)):
        pass

    assert [row["request_state"] for row in rows(conn)] == ["lost"]


def test_duplicate_terminal_replay_keeps_one_unchanged_row(client, conn):
    created(client, managed_turn(client))
    job = claim(conn, "linux")
    finish(conn, job, exit_code=0)
    before = rows(conn)

    finish(conn, job, exit_code=0)
    with pytest.raises(repo.ApiError):
        finish(conn, job, "failed", exit_code=1)
    client.post("/test-requests/req-1/cancel")
    repo.reconcile_expired_test_job_leases(conn, "2999-01-01T00:00:00.000000Z")

    assert len(before) == 1
    assert rows(conn) == before


def test_enqueue_is_atomic_with_the_final_transition(client, conn, monkeypatch):
    created(client, managed_turn(client))
    job = claim(conn, "linux")

    def explode(*args, **kwargs):
        raise RuntimeError("crash during enqueue")

    monkeypatch.setattr(repo, "_delivery_prompt", explode)
    with pytest.raises(RuntimeError):
        finish(conn, job)

    assert conn.execute("SELECT state FROM test_jobs").fetchone()[0] == "preparing"
    assert rows(conn) == []


# --- session_ref validation ---------------------------------------------------------

@pytest.mark.parametrize("change", [
    lambda ref: "opaque-session",
    lambda ref: {},
    lambda ref: {k: v for k, v in ref.items() if k != "origin_attempt_id"},
    lambda ref: {**ref, "session_id": PROVIDER_SESSION},
    lambda ref: {**ref, "cwd": HANDLE_CWD},
    lambda ref: {**ref, "generation": "1"},
    lambda ref: {**ref, "generation": True},
    lambda ref: {**ref, "issue_id": ""},
])
def test_malformed_session_ref_is_rejected_and_creates_nothing(client, conn, change):
    ref = managed_turn(client)

    r = create_request(client, change(ref))

    assert (r.status_code, r.json()["error"]) == (422, "invalid_session_ref"), r.text
    assert PROVIDER_SESSION not in r.text and HANDLE_CWD not in r.text
    assert conn.execute("SELECT COUNT(*) FROM test_requests").fetchone()[0] == 0


@pytest.mark.parametrize("change, reason", [
    ({"issue_id": "i_missing"}, "unknown_issue"),
    ({"participant": "gemini"}, "unknown_participant"),
    ({"participant": "codex"}, "no_handle"),
    ({"generation": 2}, "stale_generation"),
    ({"origin_attempt_id": "a_missing"}, "stale_origin_attempt"),
])
def test_stale_session_ref_is_rejected_and_creates_nothing(client, conn, change, reason):
    ref = {**managed_turn(client), **change}

    r = create_request(client, ref)

    assert (r.status_code, r.json()["error"]) == (409, "stale_session_ref"), r.text
    assert r.json()["reason"] == reason
    assert conn.execute("SELECT COUNT(*) FROM test_requests").fetchone()[0] == 0


def test_session_ref_without_a_saved_handle_is_rejected(client):
    ref = managed_turn(client, save_handle=False)

    r = create_request(client, ref)

    assert (r.status_code, r.json()["reason"]) == (409, "no_handle"), r.text


def test_origin_attempt_of_a_finished_turn_is_rejected(client):
    ref = managed_turn(client)
    end_origin_turn(client, ref)

    r = create_request(client, ref)

    assert (r.status_code, r.json()["reason"]) == (409, "stale_origin_attempt"), r.text


def test_origin_attempt_of_another_issue_is_rejected(client):
    first = managed_turn(client, "issue-1")
    second = managed_turn(client, "issue-2")

    r = create_request(client, {**first, "origin_attempt_id": second["origin_attempt_id"]})

    assert (r.status_code, r.json()["reason"]) == (409, "stale_origin_attempt"), r.text


def test_session_ref_of_a_closed_issue_is_rejected(client):
    ref = managed_turn(client)
    closed = client.post(f"/issues/{ref['issue_id']}/close", json={
        "reason": "done", "expected_version": snapshot(client, ref["issue_id"])["issue"]["version"]})
    assert closed.status_code == 200, closed.text

    r = create_request(client, ref)

    assert (r.status_code, r.json()["reason"]) == (409, "issue_closed"), r.text


def test_request_replay_after_the_origin_turn_ended_returns_the_stored_request(client):
    ref = managed_turn(client)
    first = created(client, ref)
    end_origin_turn(client, ref)

    replay = create_request(client, ref)

    assert (replay.status_code, replay.json()) == (200, first)


def test_null_session_ref_never_creates_a_delivery(client, conn):
    created(client, None)
    finish(conn, claim(conn, "linux"))

    assert rows(conn) == []
    assert detail(client)["delivery"] is None


def test_legacy_opaque_session_ref_finishes_without_a_delivery(client, conn):
    created(client, None)
    conn.execute("UPDATE test_requests SET session_ref=?", (json.dumps("legacy-opaque"),))

    finish(conn, claim(conn, "linux"))

    assert rows(conn) == []
    assert detail(client)["state"] == "succeeded"


# --- nothing leaks --------------------------------------------------------------------

def test_operator_and_runner_responses_never_leak_session_data(client, conn):
    ref = managed_turn(client)
    token = client.post("/runners/register", headers={"Authorization": f"Bearer {ADMIN}"},
                        json={"runner_id": "r1", "labels": ["linux"],
                              "platform": "linux"}).json()["token"]
    auth = {"Authorization": f"Bearer {token}"}
    texts = [create_request(client, ref).text, create_request(client, ref).text]
    claimed = client.post("/runners/claim", headers=auth)
    job = claimed.json()["job"]
    texts.append(claimed.text)
    texts.append(client.post("/runners/r1/heartbeat", headers=auth, json={
        "job_id": job["job_id"], "lease_token": job["lease_token"], "phase": "running"}).text)
    finished = client.post(f"/test-jobs/{job['job_id']}/result", headers=auth, json={
        "lease_token": job["lease_token"], "status": "succeeded", "exit_code": 0})
    assert finished.status_code == 200, finished.text
    texts += [finished.text, client.get("/test-requests").text,
              client.get("/test-requests/req-1").text,
              client.post("/test-requests/req-1/cancel").text,
              client.get("/test-requests/req-1/log").text]

    for text in texts:
        for secret in ("session_ref", "origin_attempt_id", ref["origin_attempt_id"],
                       ref["issue_id"], PROVIDER_SESSION, HANDLE_CWD):
            assert secret not in text, (secret, text)


def test_delivery_feed_and_prompt_carry_no_provider_session_or_cwd(client, conn):
    ref = managed_turn(client)
    completed_delivery(client, conn, ref)

    text = client.get("/test-deliveries").text

    assert PROVIDER_SESSION not in text and HANDLE_CWD not in text
    assert ref["origin_attempt_id"] not in text


def test_failed_turn_error_text_is_not_exposed_on_the_request(client, conn):
    item = completed_delivery(client, conn, managed_turn(client))
    attempt = reserve(client, item).json()["attempt"]["attempt_id"]
    post_result(client, item["issue_id"], attempt, "failed")

    assert PROVIDER_SESSION not in client.get("/test-requests/req-1").text


# --- frozen body and prompt -----------------------------------------------------------

def test_delivery_freezes_the_message_id_profile_and_result_prompt(client, conn):
    ref = managed_turn(client)
    created(client, ref)
    job = claim(conn, "linux")
    finish(conn, job, "failed", exit_code=3, duration_s=12.5, stdout_tail="1 failed",
           stderr_tail="boom")
    end_origin_turn(client, ref)

    [item] = ready(client)

    body = item["reservation"]
    assert body["message_id"] == repo.delivery_message_id("req-1")
    assert (body["kind"], body["generation"]) == ("continue", 1)
    assert (body["requested_profile"], body["profile_source"]) == (PROFILE, "task_type")
    assert body["round_id"] == snapshot(client, ref["issue_id"])["round"]["id"]
    assert (item["request_id"], item["issue_id"], item["participant"]) == (
        "req-1", ref["issue_id"], "claude")
    for expected in ("test job completed", "failed", TAR_SHA, "pytest", "exit_code=3",
                     "duration_s=12.5", "runner=runner-linux", "linux", "1 failed", "boom",
                     f"/test-requests/req-1/log?job_id={job['job_id']}", "untrusted"):
        assert expected in body["prompt"], expected
    assert ready(client) == [item]


def test_prompt_is_bounded_and_strips_control_characters_for_a_large_matrix(client, conn):
    ref = managed_turn(client)
    labels = [f"label-{index:02d}" for index in range(32)]
    created(client, ref, targets=labels)
    noisy = "x\x00\x1b[31m" * 10000
    for index, label in enumerate(labels):
        failed = index == 31
        finish(conn, claim(conn, label), "failed" if failed else "succeeded",
               stdout_tail=noisy + ("FAILING-ASSERTION" if failed else ""), stderr_tail=noisy)
    end_origin_turn(client, ref)

    [item] = ready(client)

    prompt = item["reservation"]["prompt"]
    assert len(prompt) <= repo.DELIVERY_PROMPT_MAX_CHARS
    assert "\x00" not in prompt and "\x1b" not in prompt
    assert "label-31" in prompt, "failed targets are listed first so a cut never hides them"
    assert "FAILING-ASSERTION" in prompt, "failing output gets the tail budget first"


# --- delivery lifecycle ---------------------------------------------------------------

def test_busy_thread_keeps_the_delivery_pending_until_idle(client, conn):
    ref = managed_turn(client)
    created(client, ref)
    finish(conn, claim(conn, "linux"))

    assert ready(client) == []
    assert detail(client)["delivery"]["state"] == "pending"

    end_origin_turn(client, ref)

    assert [item["request_id"] for item in ready(client)] == ["req-1"]


def test_delivered_only_after_the_attempt_result_is_committed(client, conn):
    item = completed_delivery(client, conn, managed_turn(client))

    reserved = reserve(client, item)

    assert (reserved.status_code, reserved.json()["execute"]) == (201, True), reserved.text
    assert detail(client)["delivery"]["state"] == "active"
    assert ready(client) == []
    replay = reserve(client, item)
    assert (replay.status_code, replay.json()["execute"]) == (200, False)

    post_result(client, item["issue_id"], reserved.json()["attempt"]["attempt_id"])

    assert detail(client)["delivery"]["state"] == "delivered"
    assert ready(client) == []
    assert reserve(client, item).json()["execute"] is False


def test_user_continuation_that_wins_is_not_displaced(client, conn):
    item = completed_delivery(client, conn, managed_turn(client))
    user = send(client, item["issue_id"], message_body(client, item["issue_id"], "user-1"))
    assert user.status_code == 201, user.text

    lost_race = reserve(client, item)

    assert (lost_race.status_code, lost_race.json()["error"]) == (409, "active_turn")
    assert detail(client)["delivery"]["state"] == "pending"
    assert ready(client) == []
    post_result(client, item["issue_id"], user.json()["attempt"]["attempt_id"])
    assert ready(client) == [item]
    assert detail(client)["delivery"]["state"] == "pending"


def test_failed_turn_is_reported_failed_and_not_offered_again(client, conn):
    item = completed_delivery(client, conn, managed_turn(client))
    attempt = reserve(client, item).json()["attempt"]["attempt_id"]

    post_result(client, item["issue_id"], attempt, "failed")

    assert detail(client)["delivery"]["state"] == "failed"
    assert ready(client) == []


def test_expired_unspawned_delivery_reservation_is_reoffered_with_stable_message_id(
        client, conn):
    item = completed_delivery(client, conn, managed_turn(client))

    first = reserve(client, item)
    assert first.status_code == 201, first.text
    first_attempt = first.json()["attempt"]["attempt_id"]
    assert ready(client) == []  # an unexpired reservation is never reclaimed
    replay = reserve(client, item)
    assert replay.status_code == 200 and replay.json()["execute"] is False

    expired = repo.ts(datetime.now(timezone.utc) - timedelta(seconds=1))
    conn.execute("UPDATE attempts SET lease_expires_at=? WHERE attempt_id=?",
                 (expired, first_attempt))

    [available] = ready(client)
    assert available["request_id"] == item["request_id"]
    assert available["reservation"] == item["reservation"]
    assert detail(client)["delivery"]["state"] == "pending"
    second = reserve(client, available)

    assert second.status_code == 201, second.text
    assert second.json()["execute"] is True
    assert second.json()["message_id"] == item["reservation"]["message_id"]
    second_attempt = second.json()["attempt"]["attempt_id"]
    assert second_attempt != first_attempt
    attempts = conn.execute("SELECT state, process_identity FROM attempts WHERE message_id=?"
                            " ORDER BY number", (item["reservation"]["message_id"],)).fetchall()
    assert [(row["state"], row["process_identity"]) for row in attempts] == [
        ("superseded", None), ("running", None)]
    duplicate = reserve(client, available)
    assert duplicate.status_code == 200 and duplicate.json()["execute"] is False


def test_expired_delivery_attempt_with_process_identity_is_never_reclaimed(client, conn):
    item = completed_delivery(client, conn, managed_turn(client))
    first = reserve(client, item)
    attempt_id = first.json()["attempt"]["attempt_id"]
    identity = {"host": "host", "pid": 12345, "start_time": "started"}
    reported = client.post(f"/issues/{item['issue_id']}/attempts/{attempt_id}/process",
                           json={"owner": OWNER, "process_identity": identity})
    assert reported.status_code == 200, reported.text
    expired = repo.ts(datetime.now(timezone.utc) - timedelta(seconds=1))
    conn.execute("UPDATE attempts SET lease_expires_at=? WHERE attempt_id=?",
                 (expired, attempt_id))

    assert ready(client) == []
    replay = reserve(client, item)
    assert replay.status_code == 200 and replay.json()["execute"] is False
    assert detail(client)["delivery"]["state"] == "active"
    row = conn.execute("SELECT state, process_identity FROM attempts WHERE attempt_id=?",
                       (attempt_id,)).fetchone()
    assert row["state"] == "owner_lost_suspected"
    recorded = json.loads(row["process_identity"])
    assert {key: recorded[key] for key in identity} == identity
    assert recorded["group"] is None and recorded["platform"] is None


def test_user_message_winning_after_delivery_poll_keeps_expired_delivery_pending(client, conn):
    ref = managed_turn(client)
    created(client, ref)
    finish(conn, claim(conn, "linux"), exit_code=0)
    end_origin_turn(client, ref)
    [polled] = ready(client)

    user = send(client, ref["issue_id"], message_body(client, ref["issue_id"], "user-after-poll"))
    assert user.status_code == 201, user.text
    lost_race = reserve(client, polled)

    assert lost_race.status_code == 409
    assert lost_race.json()["error"] == "active_turn"
    assert detail(client)["delivery"]["state"] == "pending"


@pytest.mark.parametrize("change", [
    {"prompt": "something else"}, {"profile_source": "user_override"},
    {"requested_profile": {"model": "other", "effort": None}}, {"kind": "start"},
])
def test_delivery_message_id_only_accepts_the_frozen_body(client, conn, change):
    item = completed_delivery(client, conn, managed_turn(client))

    r = send(client, item["issue_id"], {**item["reservation"], "owner": OWNER, **change})

    assert r.status_code == 409, r.text
    assert detail(client)["delivery"]["state"] == "pending"
    assert reserve(client, item).status_code == 201


def test_delivery_message_id_namespace_cannot_be_reserved_in_advance(client):
    ref = managed_turn(client)
    end_origin_turn(client, ref)

    r = send(client, ref["issue_id"], message_body(
        client, ref["issue_id"], repo.delivery_message_id("req-1")))

    assert (r.status_code, r.json()["error"]) == (409, "delivery_mismatch"), r.text


def test_app_recreation_retains_the_pending_delivery(client, conn, db_path):
    item = completed_delivery(client, conn, managed_turn(client))

    with TestClient(create_app(db_path)) as restarted:
        assert ready(restarted) == [item]
        assert detail(restarted)["delivery"]["state"] == "pending"


def test_new_round_updates_only_the_round_id_of_a_pending_delivery(client, conn):
    ref = managed_turn(client)
    item = completed_delivery(client, conn, ref)
    issue_id = ref["issue_id"]
    version = lambda: snapshot(client, issue_id)["issue"]["version"]
    round_id = item["reservation"]["round_id"]
    proposal = client.post(f"/issues/{issue_id}/proposals", json={
        "round_id": round_id, "by": "claude", "text": "p", "expected_version": version()}).json()
    moved = client.post(f"/issues/{issue_id}/decisions", json={
        "round_id": round_id, "proposal_id": proposal["proposal_id"], "proposal_version": 1,
        "kind": "new_round", "by": "coordinator", "expected_version": version()})
    assert moved.status_code == 201, moved.text

    [after] = ready(client)

    new_round = snapshot(client, issue_id)["round"]["id"]
    assert new_round != round_id
    assert after["reservation"] == {**item["reservation"], "round_id": new_round}
    assert reserve(client, after).status_code == 201


@pytest.mark.parametrize("how, reason", [("generation", "stale_generation"),
                                         ("close", "issue_closed")])
def test_stale_or_closed_target_is_visibly_undeliverable(client, conn, how, reason):
    ref = managed_turn(client)
    completed_delivery(client, conn, ref)
    issue_id = ref["issue_id"]
    version = snapshot(client, issue_id)["issue"]["version"]
    if how == "generation":
        r = client.post(f"/issues/{issue_id}/participants/claude/generation", json={
            "expected_generation": 1, "reason": "manual", "expected_version": version})
    else:
        r = client.post(f"/issues/{issue_id}/close",
                        json={"reason": "done", "expected_version": version})
    assert r.status_code == 200, r.text

    delivery = detail(client)["delivery"]

    assert (delivery["state"], delivery["reason"]) == ("undeliverable", reason)
    assert ready(client) == []


# --- long-poll ------------------------------------------------------------------------

def test_long_poll_wakes_when_the_last_job_finishes(client, conn, db_path):
    ref = managed_turn(client)
    created(client, ref)
    job = claim(conn, "linux")
    end_origin_turn(client, ref)

    def finish_later():
        with closing(get_connection(db_path)) as other:
            finish(other, job)

    timer = threading.Timer(0.3, finish_later)
    timer.start()
    started = time.monotonic()
    try:
        items = ready(client, wait_s=20)
    finally:
        timer.join()

    assert [item["request_id"] for item in items] == ["req-1"]
    assert time.monotonic() - started < 10


def test_long_poll_returns_empty_when_its_wait_expires(client):
    started = time.monotonic()

    assert ready(client, wait_s=0.2) == []

    assert 0.2 <= time.monotonic() - started < 10


@pytest.mark.parametrize("query", ["wait_s=56", "wait_s=-1", "wait_s=nan", "limit=0", "limit=51"])
def test_long_poll_bounds_are_enforced(client, query):
    r = client.get(f"/test-deliveries?{query}")

    assert (r.status_code, r.json()["error"]) == (422, "validation_error"), r.text


@pytest.mark.parametrize("peer, headers", [
    ("100.101.102.103", {}), ("203.0.113.9", {}), ("127.0.0.1", {"X-Forwarded-For": "100.64.0.9"}),
])
def test_delivery_feed_is_local_only(db_path, peer, headers):
    app = create_app(db_path)

    async def from_peer(scope, receive, send_message):
        if scope["type"] == "http":
            scope = {**scope, "client": (peer, 50000)}
        await app(scope, receive, send_message)

    r = TestClient(from_peer, base_url="http://127.0.0.1").get("/test-deliveries", headers=headers)

    assert (r.status_code, r.json()["error"]) == (403, "forbidden_origin")
