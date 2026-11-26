import json
import sys
import time
from pathlib import Path

from mcp_server.issue_runner import IssueRunner, TurnJob
from mcp_server.supervisor import SupervisorRuntime


class FakeIssueService:
    def __init__(self, cwd):
        self.cwd = cwd
        self.saved_handle = None
        self.reported_result = None
        self.message_body = None

    def snapshot(self, issue_id):
        return {"round": {"id": "r1"}, "participants": {"codex": {
            "generation": 1, "handle": None,
        }}}

    def resolve_profile(self, agent, task_type, user_override):
        return {"requested_profile": {"model": "gpt-6-luna", "effort": "low"}, "source": "task_type"}

    def reserve_message(self, issue_id, participant, **body):
        self.message_body = body
        return {"execute": True, "state": "active", "attempt": {"attempt_id": "a1"}}

    def report_process(self, issue_id, attempt_id, **body):
        return {"attempt_id": attempt_id}

    def save_handle(self, issue_id, participant, **body):
        self.saved_handle = body
        return {"handle": body["handle"]}

    def heartbeat(self, issue_id, attempt_id, **body):
        return {"attempt_id": attempt_id}

    def result(self, issue_id, attempt_id, **body):
        self.reported_result = body
        return {"attempt_id": attempt_id}

    def termination_confirmed(self, issue_id, attempt_id, **body):
        self.confirmation = body
        return {"attempt_id": attempt_id, "state": "terminated"}


class FakeCodexAdapter:
    def start_argv(self, prompt, profile):
        script = ("import json; print(json.dumps({'type':'thread.started','thread_id':'thread-1'}),flush=True); "
                  "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'finished'}}),flush=True)")
        return [sys.executable, "-c", script]

    def continue_argv(self, handle, prompt, profile):
        raise AssertionError("start should be used without a saved handle")


def test_runner_stores_reconstructed_handoff_in_durable_start_prompt(tmp_path, monkeypatch):
    worktree_root = tmp_path / "worktrees"
    cwd = worktree_root / "i-handoff-claude"
    cwd.mkdir(parents=True)
    service = FakeIssueService(cwd)
    service.snapshot = lambda _issue_id: {
        "issue": {"title": "Pricing", "goal": "Exact rounding", "acceptance": ["-2.5 -> -3"]},
        "round": {"id": "r2", "base_sha": "b" * 40},
        "participants": {"claude": {"generation": 2, "handle": None}},
        "generation_history": [{"participant": "claude", "from_generation": 1,
            "to_generation": 2, "reason": "repeated_error", "handoff": {
                "claim": "fixed", "evidence": "4 tests passed", "correction": "float was wrong",
                "decision": "use Fraction"}}],
        "proposals": [{"proposal_id": "p1", "proposal_version": 1, "round_id": "r2",
                       "text": "Use exact arithmetic", "state": "accepted"}],
        "decisions": [{"proposal_id": "p1", "proposal_version": 1, "round_id": "r2",
                       "kind": "accept", "note": "approved"}],
        "integrations": [{"state": "advanced", "candidate_sha": "c" * 40,
                          "tested_sha": "c" * 40, "tests": {"status": "pass"}}],
    }
    monkeypatch.setenv("A2A_LINK_WORKTREE_DIR", str(worktree_root))
    runner = IssueRunner(service, SupervisorRuntime(tmp_path / "supervisor"))
    captured = {}

    class Adapter:
        def start_argv(self, prompt, profile):
            captured["prompt"] = prompt
            script = ("import json; print(json.dumps({'type':'system','subtype':'init',"
                      "'session_id':'claude-handoff-1','model':'claude-sonnet-5-5'}),flush=True); "
                      "print(json.dumps({'type':'result','result':'finished'}),flush=True)")
            return [sys.executable, "-c", script]

        def continue_argv(self, *args):
            raise AssertionError("new generation must start")

    runner.adapters["claude"] = Adapter()
    result = runner.send("i-handoff", "claude", "Implement the next request", "feature", wait_s=10)

    assert result["status"] == "done"
    assert service.message_body["kind"] == "start"
    assert service.message_body["prompt"] == captured["prompt"]
    assert service.message_body["prompt"].endswith("Implement the next request")
    assert "Use exact arithmetic" in service.message_body["prompt"]
    assert "approved" in service.message_body["prompt"] and "c" * 40 in service.message_body["prompt"]
    assert "session_id" not in service.message_body["prompt"]


def test_runner_persists_profile_handle_and_result_for_started_thread(tmp_path, monkeypatch):
    worktree_root = tmp_path / "worktrees"
    cwd = worktree_root / "i1-codex"
    cwd.mkdir(parents=True)
    supervisor_dir = tmp_path / "supervisor"
    service = FakeIssueService(cwd)
    runner = IssueRunner(service, SupervisorRuntime(supervisor_dir))
    runner.adapters["codex"] = FakeCodexAdapter()
    monkeypatch.setenv("A2A_LINK_WORKTREE_DIR", str(worktree_root))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    result = runner.send("i1", "codex", "start work", "simple", wait_s=10)

    assert result["status"] == "done"
    assert service.message_body["profile_source"] == "task_type"
    assert service.saved_handle["attempt_id"] == "a1"
    assert service.saved_handle["handle"] == {
        "adapter_type": "codex", "session_id": "thread-1", "cwd": str(cwd),
    }
    assert service.reported_result["status"] == "done"
    assert service.reported_result["text"] == "finished"
    rows = [json.loads(line) for line in (supervisor_dir / "attempts.jsonl").read_text().splitlines()]
    assert [row["event"] for row in rows if row["event"] in ("spawn_intent", "spawned", "session_started", "handle_saved", "result_posted")] == [
        "spawn_intent", "spawned", "session_started", "handle_saved", "result_posted",
    ]


def test_startup_recovery_saves_session_then_terminates_with_named_job(tmp_path, monkeypatch):
    import os
    import pytest
    from mcp_server.supervisor import IS_WINDOWS

    if not IS_WINDOWS:
        pytest.skip("Windows named Job restart recovery")
    monkeypatch.setenv("A2A_LINK_REATTACH_GRACE_S", "0")
    worktree = tmp_path / "i1-codex"
    worktree.mkdir()
    supervisor_dir = tmp_path / "supervisor"
    first_runtime = SupervisorRuntime(supervisor_dir)
    assert first_runtime.acquire()
    code = "import json,time; print(json.dumps({'type':'thread.started','thread_id':'recovered-1'}),flush=True); time.sleep(30)"
    proc, identity, job, keeper = first_runtime.spawn(
        "a2", "m2", [sys.executable, "-c", code], str(worktree),
        metadata={"issue_id": "i1", "participant": "codex", "kind": "start",
                  "generation": 1, "requested_profile": {"model": "gpt-6-luna", "effort": "low"}},
    )
    output_path = supervisor_dir / "attempts" / "a2" / "stdout.jsonl"
    deadline = time.monotonic() + 5
    while output_path.stat().st_size == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert output_path.stat().st_size > 0
    first_runtime.release()
    assert job is not None and keeper is not None
    job.close()  # simulated MCP shutdown; only the keeper and CLI tree remain

    class RestartService(FakeIssueService):
        def snapshot(self, issue_id):
            return {"attempts": [{"attempt_id": "a2", "state": "running"}]}

    service = RestartService(worktree)
    restarted = IssueRunner(service, SupervisorRuntime(supervisor_dir))
    recovered = restarted.recover_after_restart()

    assert recovered == [{"attempt_id": "a2", "method": "terminated", "session_recovered": True}]
    assert service.saved_handle["handle"]["session_id"] == "recovered-1"
    assert service.confirmation["method"] == "terminated"
    assert proc.wait(timeout=5) is not None
    keeper.release()


def test_turn_does_not_release_keeper_when_cli_leader_exits_before_descendant(tmp_path, monkeypatch):
    import time
    import pytest
    from mcp_server.supervisor import IS_WINDOWS

    if not IS_WINDOWS:
        pytest.skip("Windows Job Object lifetime")
    monkeypatch.setenv("A2A_LINK_ISSUE_TIMEOUT_S", "1")
    monkeypatch.setenv("A2A_LINK_PROCESS_DRAIN_TIMEOUT_S", "0.1")
    runtime = SupervisorRuntime(tmp_path / "supervisor")
    assert runtime.acquire()
    heartbeat = tmp_path / "descendant-heartbeat.log"
    child_code = ("import time\nfrom pathlib import Path\np=Path(" + repr(str(heartbeat)) + ")\n"
                  "while True:\n p.open('a').write('alive\\n')\n time.sleep(.05)\n")
    parent_code = ("import json,subprocess,sys; subprocess.Popen([sys.executable,'-c'," + repr(child_code) + "]); "
                   "print(json.dumps({'type':'thread.started','thread_id':'long-lived'}),flush=True)")
    proc, identity, job, keeper = runtime.spawn(
        "a3", "m3", [sys.executable, "-c", parent_code], str(tmp_path),
        metadata={"issue_id": "i1", "participant": "codex", "kind": "start",
                  "generation": 1, "requested_profile": {"model": "gpt-6-luna", "effort": "low"}},
    )
    runtime.release()

    class Service:
        saved = None
        reported = None

        def save_handle(self, issue_id, participant, **body):
            self.saved = body
            return {}

        def heartbeat(self, *_args, **_kwargs):
            return {}

        def result(self, *_args, **body):
            assert job.active_processes() == 0, "result was posted before the descendant tree exited"
            self.reported = body
            return {}

    service = Service()
    runner = IssueRunner(service, runtime)
    state = TurnJob("a3")
    runner._finish_turn("i1", "codex", "start", 1, "a3", "m3",
                        {"requested_profile": {"model": "gpt-6-luna", "effort": "low"}},
                        str(tmp_path), proc, identity, job, keeper, state)

    assert service.saved["handle"]["session_id"] == "long-lived"
    assert service.reported["status"] == "failed"
    assert keeper.process.poll() is not None
