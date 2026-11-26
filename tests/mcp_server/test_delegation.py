import contextlib
import threading

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import mcp_server.delegate as d
from balancer.app import create_app
from balancer.db import get_connection, init_schema


@pytest.fixture
def balancer(tmp_path, monkeypatch):
    """Real balancer app served in-process; httpx.Client in delegate.py is pointed at it."""
    client = TestClient(create_app(str(tmp_path / "b.db")))
    monkeypatch.setattr(d.DelegationManager, "_client", lambda self: contextlib.nullcontext(client))
    monkeypatch.delenv(d.DEPTH_ENV, raising=False)
    return client


def _fake_cli(monkeypatch, gate=None, code=0, out="done!", err=""):
    seen = {}

    def fake(command, cwd, timeout_s, env, **progress):
        seen.update(command=command, cwd=cwd, env=env)
        if gate is not None:
            gate.wait(10)
        return code, out, err

    monkeypatch.setattr(d, "run_cli_task", fake)
    return seen


def test_fast_task_completes_and_result_is_stored(balancer, monkeypatch, tmp_path):
    _fake_cli(monkeypatch)
    mgr = d.DelegationManager("http://x")
    started = mgr.start("codex", "do it", str(tmp_path))
    res = mgr.wait(started["task_id"], 10)
    assert res["status"] == "done" and res["result"] == "done!"
    stored = balancer.get(f"/tasks/{started['task_id']}").json()
    assert stored["status"] == "done" and stored["result"] == "done!"


def test_slow_task_returns_in_progress_then_result(balancer, monkeypatch, tmp_path):
    gate = threading.Event()
    _fake_cli(monkeypatch, gate=gate)
    mgr = d.DelegationManager("http://x")
    started = mgr.start("claude", "long job", str(tmp_path))
    first = mgr.wait(started["task_id"], 0.2)
    assert first["status"] == "in_progress" and "get_delegation_result" in first["hint"]
    gate.set()
    assert mgr.wait(started["task_id"], 10)["status"] == "done"


def test_failure_is_reported_and_recorded(balancer, monkeypatch, tmp_path):
    _fake_cli(monkeypatch, code=2, out="", err="boom")
    mgr = d.DelegationManager("http://x")
    tid = mgr.start("codex", "x", str(tmp_path))["task_id"]
    res = mgr.wait(tid, 10)
    assert res["status"] == "failed" and "boom" in res["error"]
    assert balancer.get(f"/tasks/{tid}").json()["error"] == "boom"


def test_result_from_another_process_is_read_from_balancer(balancer, monkeypatch, tmp_path):
    _fake_cli(monkeypatch)
    tid = d.DelegationManager("http://x").start("codex", "x", str(tmp_path))["task_id"]
    d.DelegationManager("http://x").jobs  # fresh manager has no in-memory job
    first = d.DelegationManager("http://x")
    import time
    for _ in range(50):
        res = first.wait(tid, 0)
        if res["status"] == "done":
            break
        time.sleep(0.05)
    assert res["status"] == "done" and res["result"] == "done!"


def test_child_gets_incremented_depth_and_cannot_delegate_again(balancer, monkeypatch, tmp_path):
    seen = _fake_cli(monkeypatch)
    mgr = d.DelegationManager("http://x")
    mgr.wait(mgr.start("codex", "x", str(tmp_path))["task_id"], 10)
    assert seen["env"][d.DEPTH_ENV] == "1"
    monkeypatch.setenv(d.DEPTH_ENV, "1")  # we are now inside a delegated agent
    refused = d.DelegationManager("http://x").start("claude", "x", str(tmp_path))
    assert refused["status"] == "refused" and "depth" in refused["error"]


def test_child_commands_disable_mcp_and_limit_permissions(monkeypatch):
    for k in ("A2A_LINK_CLAUDE_PERMISSION_MODE", "A2A_LINK_CLAUDE_ALLOWED_TOOLS", "A2A_LINK_CODEX_SANDBOX"):
        monkeypatch.delenv(k, raising=False)
    claude = d.claude_command("p")
    assert "--strict-mcp-config" in claude and claude[claude.index("--permission-mode") + 1] == "acceptEdits"
    allowed = claude[claude.index("--allowedTools") + 1:]
    assert "Bash(python -m pytest:*)" in allowed and "Bash(git status:*)" in allowed
    assert not any(t.startswith("Bash(git push") or t == "Bash" for t in allowed)
    codex = d.codex_command("p")
    assert codex[codex.index("-s") + 1] == "workspace-write"
    assert "mcp_servers.a2a-link.enabled=false" in codex and codex[-1] == "p"
    monkeypatch.setenv("A2A_LINK_CLAUDE_ALLOWED_TOOLS", "Bash(git status:*), Bash(pytest:*)")
    claude = d.claude_command("p")
    assert claude[claude.index("--allowedTools") + 1:] == ["Bash(git status:*)", "Bash(pytest:*)"]


def test_prompt_preamble_carries_context(monkeypatch, tmp_path):
    monkeypatch.delenv("A2A_LINK_DELEGATE_PREAMBLE", raising=False)
    wrapped = d.wrap_prompt("fix bug", str(tmp_path))
    assert str(tmp_path) in wrapped and wrapped.endswith("fix bug") and "summary" in wrapped
    monkeypatch.setenv("A2A_LINK_DELEGATE_PREAMBLE", "0")
    assert d.wrap_prompt("fix bug", str(tmp_path)) == "fix bug"


def test_old_database_gets_result_column(tmp_path):
    path = str(tmp_path / "old.db")
    conn = get_connection(path)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, description TEXT, status TEXT, agent TEXT,"
                 " model_hint TEXT, error TEXT, retry_count INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT)")
    init_schema(conn)
    assert "result" in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}


def test_codex_last_message_file_is_preferred_over_stdout(balancer, monkeypatch, tmp_path):
    def fake(command, cwd, timeout_s, env, **progress):
        Path(command[command.index("-o") + 1]).write_text("FINAL MESSAGE", encoding="utf-8")
        return 0, "", ""

    monkeypatch.setattr(d, "run_cli_task", fake)
    mgr = d.DelegationManager("http://x")
    res = mgr.wait(mgr.start("codex", "x", str(tmp_path))["task_id"], 10)
    assert res["result"] == "FINAL MESSAGE"



# --- git worktree isolation --------------------------------------------------

import subprocess


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.name", "t"); _git(r, "config", "user.email", "t@t")
    (r / "app.py").write_text("print('v1')\n")
    _git(r, "add", "."); _git(r, "commit", "-q", "-m", "init")
    monkeypatch.setenv("A2A_LINK_WORKTREE_DIR", str(tmp_path / "wts"))
    return r


def _editing_cli(monkeypatch):
    seen = {}

    def fake(command, cwd, timeout_s, env, **progress):
        seen["cwd"], seen["prompt"] = cwd, command[2] if command[0] == "claude" else command[-1]
        Path(cwd, "app.py").write_text("print('v2')\n")
        Path(cwd, "new.txt").write_text("added\n")
        return 0, "edited app.py", ""

    monkeypatch.setattr(d, "run_cli_task", fake)
    return seen


def test_worktree_isolates_child_and_commits_on_its_branch(balancer, monkeypatch, repo, tmp_path):
    seen = _editing_cli(monkeypatch)
    mgr = d.DelegationManager("http://x")
    res = mgr.wait(mgr.start("claude", "edit", str(repo))["task_id"], 10)
    ws = res["workspace"]
    assert res["status"] == "done" and ws["mode"] == "worktree"
    assert Path(seen["cwd"]).resolve() == Path(ws["path"]).resolve() and str(tmp_path / "wts") in ws["path"]
    assert ws["branch"] in seen["prompt"]  # child is told it is in a worktree
    assert (repo / "app.py").read_text() == "print('v1')\n"  # caller's checkout untouched
    assert not (repo / "new.txt").exists()
    assert "app.py" in ws["changes"] and "new.txt" in ws["changes"] and ws["commit"]
    assert _git(repo, "show", f"{ws['branch']}:app.py") == "print('v2')"
    _git(repo, "merge", "-q", ws["branch"])  # the suggested merge works
    assert (repo / "new.txt").exists()


def test_worktree_warns_about_uncommitted_caller_changes(balancer, monkeypatch, repo):
    _editing_cli(monkeypatch)
    (repo / "wip.txt").write_text("not committed")
    mgr = d.DelegationManager("http://x")
    res = mgr.wait(mgr.start("codex", "edit", str(repo))["task_id"], 10)
    assert any("uncommitted" in w for w in res["workspace"]["warnings"])


def test_isolation_none_runs_in_shared_checkout(balancer, monkeypatch, repo):
    seen = _editing_cli(monkeypatch)
    mgr = d.DelegationManager("http://x")
    res = mgr.wait(mgr.start("codex", "edit", str(repo), isolation="none")["task_id"], 10)
    assert res["workspace"]["mode"] == "none" and Path(seen["cwd"]) == repo
    assert (repo / "new.txt").exists()


def test_non_repo_falls_back_with_warning_or_fails_when_forced(balancer, monkeypatch, tmp_path):
    _editing_cli(monkeypatch)
    plain = tmp_path / "plain"; plain.mkdir()
    mgr = d.DelegationManager("http://x")
    res = mgr.wait(mgr.start("codex", "edit", str(plain))["task_id"], 10)
    assert res["workspace"]["mode"] == "none" and "not a git repository" in res["workspace"]["warnings"][0]
    forced = mgr.start("codex", "edit", str(plain), isolation="worktree")
    assert forced["status"] == "failed" and "cannot isolate" in forced["error"]


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_balancer_failure_requests_help_without_running_cli(monkeypatch, tmp_path, agent):
    def denied(client, address):
        raise PermissionError("port unavailable")

    monkeypatch.setattr(d, "ensure_balancer", denied)
    seen = _fake_cli(monkeypatch)
    result = d.DelegationManager("http://localhost:8000").start(agent, "task", str(tmp_path))
    assert result["status"] == "needs_human" and "port unavailable" in result["error"]
    assert "Ask the user" in result["hint"] and not seen
