import json
import hashlib
import sys
import threading
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient

from balancer import repository as repo
from balancer.app import create_app
from balancer.db import get_connection
from mcp_server.issue_runner import IssueRunner
from mcp_server.issue_service import IssueAPIError, IssueService
from mcp_server.supervisor import SupervisorRuntime


RESERVATION = {
    "message_id": "testdelivery_req-1",
    "kind": "continue",
    "round_id": "round-frozen",
    "generation": 4,
    "requested_profile": {"model": "gpt-6.2-sol", "effort": "high"},
    "profile_source": "task_type",
    "prompt": "Frozen test result and log reference",
}


class FakeJournal:
    def read(self):
        return []


class NoSpawnRuntime:
    owner = "dispatcher-owner"
    journal = FakeJournal()

    def acquire(self):
        return True

    def release(self):
        pass

    def spawn(self, *_args, **_kwargs):
        raise AssertionError("execute=false must never spawn a CLI")


class DeliveryService:
    base_url = "http://127.0.0.1:8000"

    def __init__(self, execute=False):
        self.execute = execute
        self.reserved = None
        self.delivery_rows = []
        self.result_body = None
        self.process_body = None

    def snapshot(self, _issue_id):
        return {"participants": {"claude": {"generation": 4, "handle": {
            "adapter_type": "claude", "session_id": "saved-session",
            "cwd": self.cwd, "generation": 4}}}}

    def reserve_message(self, issue_id, participant, **body):
        self.reserved = (issue_id, participant, body)
        return {"execute": self.execute, "state": "active",
                "attempt": {"attempt_id": "attempt-delivery"}}

    def test_deliveries(self, *, wait_s=0, limit=10):
        self.poll_args = (wait_s, limit)
        return {"deliveries": list(self.delivery_rows)}

    def report_process(self, _issue_id, _attempt_id, **body):
        self.process_body = body
        return {}

    def heartbeat(self, *_args, **_kwargs):
        return {}

    def termination_confirmed(self, *_args, **_kwargs):
        return {}

    def result(self, _issue_id, _attempt_id, **body):
        self.result_body = body
        return {}


def delivery():
    return {"request_id": "req-1", "issue_id": "i_de6fda66d9",
            "participant": "claude", "generation": 4,
            "summary": {"state": "succeeded"}, "reservation": dict(RESERVATION)}


def test_issue_service_long_polls_the_exact_delivery_endpoint_and_shape():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"deliveries": [delivery()]})

    service = IssueService("http://127.0.0.1:8000", httpx.Client(transport=httpx.MockTransport(handler)))

    response = service.test_deliveries(wait_s=20, limit=3)

    assert response["deliveries"][0]["reservation"] == RESERVATION
    assert "wait_s=20" in seen["url"] and "limit=3" in seen["url"]
    service._client.close()


def test_send_reserved_passes_frozen_body_exactly_and_never_spawns_on_replay(tmp_path):
    service = DeliveryService(execute=False)
    service.cwd = str(tmp_path)
    runner = IssueRunner(service, NoSpawnRuntime())

    response = runner.send_reserved("i_de6fda66d9", "claude", dict(RESERVATION))

    assert response["execute"] is False
    assert service.reserved == ("i_de6fda66d9", "claude", {
        **RESERVATION, "owner": "dispatcher-owner"})


def test_send_reserved_without_current_handle_does_not_reserve_or_spawn(tmp_path):
    service = DeliveryService(execute=True)
    service.cwd = str(tmp_path)
    service.snapshot = lambda _issue_id: {"participants": {
        "claude": {"generation": 4, "handle": None}}}
    runner = IssueRunner(service, NoSpawnRuntime())

    result = runner.send_reserved("i_de6fda66d9", "claude", dict(RESERVATION))

    assert result == {"status": "undeliverable", "execute": False,
                      "message_id": RESERVATION["message_id"]}
    assert service.reserved is None


@pytest.mark.parametrize("exit_code,expected_status", [(0, "done"), (1, "failed")])
def test_delivery_result_reuses_saved_handle_and_frozen_prompt_and_profile(tmp_path,
                                                                           monkeypatch,
                                                                           exit_code,
                                                                           expected_status):
    monkeypatch.setenv("A2A_LINK_WORKTREE_DIR", str(tmp_path / "worktrees"))
    cwd = tmp_path / "worktrees" / "i_de6fda66d9-claude"
    cwd.mkdir(parents=True)
    service = DeliveryService(execute=True)
    service.cwd = str(cwd)
    runtime = SupervisorRuntime(tmp_path / "link-home" / "supervisor")
    runner = IssueRunner(service, runtime)
    captured = {}

    class ResumeAdapter:
        def continue_argv(self, handle, prompt, profile, participant_context=None):
            captured.update(handle=handle, prompt=prompt, profile=profile,
                            context=participant_context)
            event = {"type": "system", "subtype": "init", "session_id": "saved-session"}
            code = ("import json,sys; print(" + repr(json.dumps(event)) +
                    ",flush=True); sys.exit(" + str(exit_code) + ")")
            return [sys.executable, "-c", code]

        def start_argv(self, *_args):
            raise AssertionError("delivery must continue the saved CLI session")

    runner.adapters["claude"] = ResumeAdapter()

    response = runner.send_reserved("i_de6fda66d9", "claude", dict(RESERVATION), wait_s=10)

    assert response["status"] == expected_status
    assert captured["handle"].session_id == "saved-session"
    assert captured["prompt"] == RESERVATION["prompt"]
    assert captured["profile"] == RESERVATION["requested_profile"]
    assert captured["context"]["origin_attempt_id"] == "attempt-delivery"
    assert service.reserved[2] == {**RESERVATION, "owner": runtime.owner}
    assert service.result_body["status"] == expected_status


def test_transient_process_report_failure_keeps_supervised_delivery_watcher(tmp_path, monkeypatch):
    monkeypatch.setenv("A2A_LINK_WORKTREE_DIR", str(tmp_path / "worktrees"))
    cwd = tmp_path / "worktrees" / "i_de6fda66d9-claude"
    cwd.mkdir(parents=True)
    service = DeliveryService(execute=True)
    service.cwd = str(cwd)

    def unavailable(*_args, **_kwargs):
        raise OSError("balancer restarted during identity report")

    service.report_process = unavailable
    runtime = SupervisorRuntime(tmp_path / "link-home" / "supervisor")
    runner = IssueRunner(service, runtime)
    runner._retry_api = lambda call: call()
    event = {"type": "system", "subtype": "init", "session_id": "saved-session"}

    class ResumeAdapter:
        def continue_argv(self, *_args, **_kwargs):
            code = "import json; print(" + repr(json.dumps(event)) + ",flush=True)"
            return [sys.executable, "-c", code]

        def start_argv(self, *_args):
            raise AssertionError("delivery must resume the saved session")

    runner.adapters["claude"] = ResumeAdapter()

    result = runner.send_reserved("i_de6fda66d9", "claude", dict(RESERVATION), wait_s=10)

    assert result["status"] == "done"
    assert service.result_body["status"] == "done"
    journal = runtime.journal.read()
    assert any(row["event"] == "process_report_pending" for row in journal)


def test_expired_reserved_delivery_restarts_once_with_same_message_and_saved_session(
        tmp_path, monkeypatch):
    monkeypatch.setenv("A2A_LINK_WORKTREE_DIR", str(tmp_path / "worktrees"))
    cwd = tmp_path / "worktrees" / "i_de6fda66d9-claude"
    cwd.mkdir(parents=True)
    service = DeliveryService(execute=True)
    service.cwd = str(cwd)
    attempts = 0
    reserved_bodies = []

    def rearm(_issue_id, _participant, **body):
        nonlocal attempts
        reserved_bodies.append(body)
        attempts += 1
        return {"execute": attempts == 1, "state": "active",
                "attempt": {"attempt_id": "attempt-after-expiry" if attempts == 1
                            else "attempt-after-expiry"}}

    service.reserve_message = rearm
    service.delivery_rows = [delivery()]
    runtime = SupervisorRuntime(tmp_path / "link-home" / "supervisor")
    assert runtime.journal.read() == []  # crash happened before supervisor.spawn intent
    runner = IssueRunner(service, runtime)
    invocations = []

    class ResumeAdapter:
        def continue_argv(self, handle, prompt, profile, participant_context=None):
            invocations.append((handle.session_id, prompt, profile))
            event = {"type": "system", "subtype": "init", "session_id": "saved-session"}
            return [sys.executable, "-c", "import json; print(" + repr(json.dumps(event)) + ",flush=True)"]

        def start_argv(self, *_args):
            raise AssertionError("recovery must resume the stored session")

    runner.adapters["claude"] = ResumeAdapter()

    assert runner.dispatch_once() == 1
    assert runner.jobs["attempt-after-expiry"].done.wait(10)
    assert runner.jobs["attempt-after-expiry"].result["status"] == "done"
    assert runner.dispatch_once() == 1  # idempotent duplicate reservation returns execute=false

    assert attempts == 2
    assert len(invocations) == 1
    assert invocations[0] == ("saved-session", RESERVATION["prompt"],
                              RESERVATION["requested_profile"])
    assert all(body["message_id"] == RESERVATION["message_id"] for body in reserved_bodies)
    assert all(body["owner"] == runtime.owner for body in reserved_bodies)


def test_real_outbox_reoffers_expired_pre_spawn_reservation_and_dispatches_once(
        tmp_path, monkeypatch):
    """Exercise the durable feed, API rearm, and a fresh MCP process end to end."""
    monkeypatch.setenv("A2A_LINK_HOME", str(tmp_path / "link-home"))
    worktree_root = tmp_path / "worktrees"
    monkeypatch.setenv("A2A_LINK_WORKTREE_DIR", str(worktree_root))
    monkeypatch.setenv("A2A_LINK_RUNNER_REGISTRATION_TOKEN", "admin")
    db_path = str(tmp_path / "balancer.db")
    app = create_app(db_path)

    with TestClient(app) as api:
        def forward(request):
            response = api.request(request.method, request.url.path +
                                   ("?" + request.url.query.decode() if request.url.query else ""),
                                   content=request.read(),
                                   headers={key: value for key, value in request.headers.items()
                                            if key.lower() not in {"host", "content-length"}})
            return httpx.Response(response.status_code, content=response.content,
                                  headers={"content-type": response.headers.get("content-type", "application/json")},
                                  request=request)

        service = IssueService("http://testserver", httpx.Client(transport=httpx.MockTransport(forward)))
        opened = service.open_issue(request_id="restart-delivery", title="restart delivery",
                                    goal="deliver a test result", acceptance=[], cwd="C:/repo",
                                    base_sha="a" * 40, opened_from="claude")
        issue_id = opened["issue"]["id"]
        cwd = worktree_root / f"{issue_id}-claude"
        cwd.mkdir(parents=True)
        initial = service.snapshot(issue_id)
        origin = service.reserve_message(issue_id, "claude", message_id="origin-start", kind="start",
                                         round_id=initial["round"]["id"], generation=1,
                                         requested_profile=RESERVATION["requested_profile"],
                                         profile_source="task_type", owner="origin-owner", prompt="run tests")
        origin_attempt = origin["attempt"]["attempt_id"]
        service.save_handle(issue_id, "claude", generation=1, attempt_id=origin_attempt,
                            handle={"adapter_type": "claude", "session_id": "saved-session",
                                    "cwd": str(cwd)})

        snapshot = b"test snapshot for restart recovery"
        digest = hashlib.sha256(snapshot).hexdigest()
        service._request("PUT", f"/test-snapshots/{digest}", content=snapshot)
        service.create_test_request(request_id="req-restart", snapshot_sha256=digest,
                                    profile="python-pytest",
                                    targets=["linux"],
                                    session_ref={"issue_id": issue_id, "participant": "claude",
                                                 "generation": 1, "origin_attempt_id": origin_attempt})
        assert service.get_test_request("req-restart")["profile"] == "python-pytest"

        with closing(get_connection(db_path)) as conn:
            claimed = repo.claim_test_job(conn, "runner-linux", ["linux"], 60)
            assert claimed is not None
            repo.finish_test_job(conn, claimed["job_id"], claimed["runner_id"],
                                 claimed["lease_token"], {"status": "succeeded", "exit_code": 0})
        service.result(issue_id, origin_attempt, owner="origin-owner", status="done", text="ok",
                       exit_code=0, confirmed_profile=RESERVATION["requested_profile"],
                       context=None, limits=None)

        [item] = service.test_deliveries(wait_s=0)["deliveries"]
        assert item["reservation"]["message_id"].startswith("testdelivery_")
        runtime_directory = tmp_path / "link-home" / "supervisor"
        before_crash_runtime = SupervisorRuntime(runtime_directory)
        first = service.reserve_message(issue_id, "claude", **item["reservation"],
                                        owner=before_crash_runtime.owner)
        assert first["execute"] is True  # crash immediately after reservation, before spawn_intent
        attempt_id = first["attempt"]["attempt_id"]
        with closing(get_connection(db_path)) as conn:
            conn.execute("UPDATE attempts SET lease_expires_at='2000-01-01T00:00:00.000000Z'"
                         " WHERE attempt_id=?", (attempt_id,))

        invocations = []
        after_restart_runtime = SupervisorRuntime(runtime_directory)
        assert after_restart_runtime.owner == before_crash_runtime.owner
        runner = IssueRunner(service, after_restart_runtime)

        class ResumeAdapter:
            def continue_argv(self, handle, prompt, profile, participant_context=None):
                invocations.append((handle.session_id, prompt, profile,
                                    participant_context["origin_message_id"]))
                event = {"type": "system", "subtype": "init", "session_id": "saved-session"}
                script = "import json; print(" + repr(json.dumps(event)) + ",flush=True)"
                return [sys.executable, "-c", script]

            def start_argv(self, *_args):
                raise AssertionError("recovery must continue the saved session")

        runner.adapters["claude"] = ResumeAdapter()
        assert runner.dispatch_once() == 1
        [rearmed] = [job for job in runner.jobs.values()]
        assert rearmed.done.wait(10)
        assert rearmed.result["status"] == "done"
        assert runner.dispatch_once() == 0

        assert invocations == [("saved-session", item["reservation"]["prompt"],
                                item["reservation"]["requested_profile"],
                                item["reservation"]["message_id"])]
        view = service.get_test_request("req-restart")
        assert view["delivery"]["state"] == "delivered"
        assert view["delivery"]["message_id"] == item["reservation"]["message_id"]
        assert any(message["message_id"] == item["reservation"]["message_id"]
                   for message in service.snapshot(issue_id)["messages"])
        service._client.close()


def test_dispatch_once_preserves_busy_delivery_for_next_poll(tmp_path):
    service = DeliveryService(execute=False)
    service.delivery_rows = [delivery()]

    def active_turn(*_args, **_kwargs):
        raise IssueAPIError(409, {"error": "active_turn"})

    service.reserve_message = active_turn
    runner = IssueRunner(service, NoSpawnRuntime())

    count = runner.dispatch_once()

    assert count == 1
    assert service.delivery_rows == [delivery()]
    assert service.poll_args == (0, 10)


def test_dispatcher_start_and_stop_are_idempotent(tmp_path):
    service = DeliveryService()
    returned = threading.Event()

    def poll(*, wait_s=0, limit=10):
        returned.set()
        return {"deliveries": []}

    service.test_deliveries = poll
    runner = IssueRunner(service, NoSpawnRuntime())

    assert runner.start_dispatcher(wait_s=0.1) is True
    first = runner._dispatcher_thread
    assert runner.start_dispatcher(wait_s=0.1) is False
    assert runner._dispatcher_thread is first
    assert returned.wait(2)
    assert runner.stop_dispatcher(timeout=2) is True
    assert runner.stop_dispatcher(timeout=2) is False


def test_stdio_runtime_entry_starts_dispatcher_and_stops_it_on_exit(monkeypatch):
    from mcp_server import __main__ as entrypoint

    events = []

    class FakeRunner:
        def start_dispatcher(self):
            events.append("start")

        def stop_dispatcher(self, *, timeout):
            events.append(("stop", timeout))

    class FakeServer:
        def run(self, transport):
            events.append(("run", transport))

    monkeypatch.setattr(entrypoint, "ISSUE_RUNNER", FakeRunner())
    monkeypatch.setattr(entrypoint, "build_server", lambda _policy: FakeServer())

    entrypoint.main()

    assert events == ["start", ("run", "stdio"), ("stop", 35)]


def test_dispatcher_reconnects_after_temporary_balancer_failure(tmp_path):
    service = DeliveryService()
    calls = 0
    recovered = threading.Event()

    def poll(*, wait_s=0, limit=10):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("balancer restarted")
        recovered.set()
        return {"deliveries": []}

    service.test_deliveries = poll
    runner = IssueRunner(service, NoSpawnRuntime())
    assert runner.start_dispatcher(wait_s=0.1)
    assert recovered.wait(3)
    assert runner.stop_dispatcher(timeout=2)
