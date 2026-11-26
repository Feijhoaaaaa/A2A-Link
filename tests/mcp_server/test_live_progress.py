import contextlib
import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

import mcp_server.delegate as d
from balancer.app import create_app
from balancer.tasklog import TaskLog, log_path, read_tail
from mcp_server.process import run_process
from mcp_server.progress import claude_result, claude_usage, format_claude_event

PY = sys.executable


# --- process: live lines, heartbeat -------------------------------------------

def test_lines_arrive_while_the_process_is_still_running(tmp_path):
    """read1 matters: with read(64 KiB) nothing would arrive until the process exits."""
    seen = []
    code = "import sys, time\nprint('first', flush=True)\ntime.sleep(3)\nprint('second', flush=True)"
    t0 = time.monotonic()
    r = run_process([PY, "-c", code], cwd=str(tmp_path), timeout_s=30,
                    on_line=lambda stream, line: seen.append((round(time.monotonic() - t0, 1), stream, line)))
    assert r.returncode == 0 and "first" in r.stdout and "second" in r.stdout
    first = next(t for t, _, line in seen if line == "first")
    second = next(t for t, _, line in seen if line == "second")
    assert first < 2.5 <= second  # "first" was reported before the 3 s sleep ended


def test_stderr_lines_and_partial_last_line(tmp_path):
    seen = []
    code = "import sys\nsys.stderr.write('warn 1\\nwarn 2')"
    run_process([PY, "-c", code], cwd=str(tmp_path), timeout_s=30, on_line=lambda s, l: seen.append((s, l)))
    assert ("stderr", "warn 1") in seen and ("stderr", "warn 2") in seen


def test_heartbeat_fires_only_when_quiet(tmp_path):
    beats = []
    run_process([PY, "-c", "import time; time.sleep(3.5)"], cwd=str(tmp_path), timeout_s=30,
                heartbeat_s=1, on_heartbeat=beats.append)
    assert len(beats) >= 2 and all(b >= 1 for b in beats)


def test_broken_line_sink_does_not_break_the_process(tmp_path):
    def boom(stream, line):
        raise RuntimeError("sink failed")

    r = run_process([PY, "-c", "print('x')"], cwd=str(tmp_path), timeout_s=30, on_line=boom)
    assert r.returncode == 0 and "x" in r.stdout


# --- task log -----------------------------------------------------------------

def test_task_log_lines_are_time_stamped_and_tailable():
    log = TaskLog("abc123")
    for i in range(5):
        log.write(f"line {i}")
    tail = read_tail("abc123", 2)
    assert tail.splitlines() == ["[00:00] line 3", "[00:00] line 4"]


@pytest.mark.parametrize("bad", ["../x", "a/b", "..", "x" * 65, ""])
def test_task_log_rejects_path_traversal(bad):
    with pytest.raises(ValueError):
        log_path(bad)


# --- claude stream-json -------------------------------------------------------

TOOL_USE = {"type": "assistant", "message": {"content": [
    {"type": "tool_use", "name": "PowerShell", "input": {"command": "python -m pytest -q"}}]}}
TOOL_RESULT = {"type": "user", "message": {"content": [
    {"type": "tool_result", "content": "3 passed", "is_error": False}]}}
SAY = {"type": "assistant", "message": {"content": [{"type": "text", "text": "All tests pass."}]}}
RESULT = {"duration_api_ms": 4404, "duration_ms": 6100, "result": "Fixed calc.py; tests pass.",
          "stop_reason": "end_turn", "type": "result", "subtype": "success"}
RATE = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour",
        "unifiedWindows": {"five_hour": {"utilization": 0.62, "resetsAt": 1795345800}}}}


def test_claude_events_become_readable_lines():
    assert format_claude_event(TOOL_USE) == ["-> PowerShell: python -m pytest -q"]
    assert format_claude_event(TOOL_RESULT) == ["<- 3 passed"]
    assert format_claude_event(SAY) == ["say: All tests pass."]
    assert format_claude_event(RESULT)[0].startswith("claude finished")
    assert format_claude_event({"type": "system", "subtype": "hook_started"}) == []


CODEX_EVENTS = [
    {"type": "thread.started", "thread_id": "t"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "I’ll run the tests."}},
    {"type": "item.started", "item": {"type": "command_execution",
     "command": "\"C:\\\\Users\\\\x\\\\pwsh.exe\" -Command 'python -m unittest'", "status": "in_progress"}},
    {"type": "item.completed", "item": {"type": "command_execution", "command": "x",
     "aggregated_output": "F.\nFAIL: test_add\n...", "exit_code": 1}},
    {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "calc.py", "kind": "update"}]}},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "Fixed both bugs."}},
    {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 7}},
]


def test_codex_json_events_become_readable_lines():
    from mcp_server.progress import codex_last_message, format_codex_event

    lines = [line for e in CODEX_EVENTS for line in format_codex_event(e)]
    assert lines == [
        "codex session started",
        "say: I’ll run the tests.",
        "-> shell: python -m unittest",
        "<- exit 1: F. (+2 lines)",
        "-> edit: calc.py",
        "say: Fixed both bugs.",
        "codex turn finished (tokens in 100, out 7)",
    ]
    assert codex_last_message("\n".join(json.dumps(e) for e in CODEX_EVENTS)) == "Fixed both bugs."


def test_codex_command_uses_json_events():
    assert "--json" in d.codex_command("p")


def test_mojibake_is_repaired_and_clean_text_untouched():
    from mcp_server.progress import repair_mojibake

    assert repair_mojibake("first pass â€” then") == "first pass — then"
    assert repair_mojibake("zażółć — ok") == "zażółć — ok"


def test_claude_final_result_and_usage_are_extracted():
    stream = "\n".join(json.dumps(e) for e in (TOOL_USE, TOOL_RESULT, SAY, RESULT))
    assert claude_result(stream) == "Fixed calc.py; tests pass."
    assert claude_usage(RATE) == (62.0, 1795345800.0)
    assert claude_usage(SAY) is None


# --- delegation end to end (fake CLI, real balancer app) ----------------------

@pytest.fixture
def balancer(tmp_path, monkeypatch):
    client = TestClient(create_app(str(tmp_path / "b.db")))
    monkeypatch.setattr(d.DelegationManager, "_client", lambda self: contextlib.nullcontext(client))
    monkeypatch.delenv(d.DEPTH_ENV, raising=False)
    return client


def test_claude_delegation_logs_steps_reports_usage_and_returns_final_text(balancer, monkeypatch, tmp_path):
    def fake(command, cwd, timeout_s, env, on_line, heartbeat_s, on_heartbeat):
        assert "stream-json" in command
        for event in (RATE, TOOL_USE, TOOL_RESULT, SAY, RESULT):
            on_line("stdout", json.dumps(event))
        on_heartbeat(20.0)
        return 0, "\n".join(json.dumps(e) for e in (TOOL_USE, RESULT)), ""

    monkeypatch.setattr(d, "run_cli_task", fake)
    mgr = d.DelegationManager("http://x")
    tid = mgr.start("claude", "fix it", str(tmp_path), isolation="none")["task_id"]
    res = mgr.wait(tid, 10)
    assert res["status"] == "done" and res["result"] == "Fixed calc.py; tests pass."
    assert "elapsed" in res

    log = mgr.log(tid)["log"]
    for expected in ("delegated to claude", "-> PowerShell: python -m pytest -q", "<- 3 passed",
                     "say: All tests pass.", "still working", "finished: done"):
        assert expected in log, expected
    assert balancer.get(f"/tasks/{tid}/log").text == read_tail(tid, 200)
    usage = balancer.get("/events", params={"once": "true"}).text
    assert '"agent": "claude"' in usage and "62.0" in usage


def test_codex_output_lines_are_logged_verbatim(balancer, monkeypatch, tmp_path):
    def fake(command, cwd, timeout_s, env, on_line, heartbeat_s, on_heartbeat):
        on_line("stderr", "mcp: shell started")
        on_line("stderr", "exec python -m unittest")
        return 0, "ok", ""

    monkeypatch.setattr(d, "run_cli_task", fake)
    mgr = d.DelegationManager("http://x")
    tid = mgr.start("codex", "x", str(tmp_path), isolation="none")["task_id"]
    mgr.wait(tid, 10)
    log = mgr.log(tid)["log"]
    assert "mcp: shell started" in log and "exec python -m unittest" in log


def test_list_delegations_shows_tasks_from_the_balancer_and_local_untracked(balancer, monkeypatch, tmp_path):
    monkeypatch.setattr(d, "run_cli_task", lambda *a, **k: (0, "ok", ""))
    mgr = d.DelegationManager("http://x")
    tid = mgr.start("codex", "write docs", str(tmp_path), isolation="none")["task_id"]
    mgr.wait(tid, 10)
    other_session = d.DelegationManager("http://x")  # sees it only through the balancer
    rows = other_session.recent()["delegations"]
    assert any(r["task_id"] == tid and r["task"] == "write docs" and r["status"] == "done" for r in rows)


def test_balancer_log_endpoint_validates_ids(balancer):
    assert balancer.get("/tasks/..%2F..%2Fetc/log").status_code in (400, 404)
    assert balancer.get("/tasks/unknownid/log").status_code == 404
