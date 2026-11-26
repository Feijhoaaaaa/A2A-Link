import re
import sys

import pytest

from mcp_server.cli_executor import run_cli_task
from mcp_server.delegate import delegate
from mcp_server.process import run_process

PY = sys.executable


def test_run_process_captures_stdout_stderr_and_exit_code(tmp_path):
    r = run_process([PY, "-c", "import sys; print('hello'); print('oops', file=sys.stderr); sys.exit(3)"],
                    cwd=str(tmp_path), timeout_s=30)
    assert r.returncode == 3 and "hello" in r.stdout and "oops" in r.stderr and not r.timed_out


def test_run_process_times_out_and_kills(tmp_path):
    r = run_process([PY, "-c", "import time; time.sleep(30)"], cwd=str(tmp_path), timeout_s=1)
    assert r.timed_out and r.returncode == 124 and "timed out" in r.stderr
    assert r.duration_s < 15


def test_run_process_truncates_large_output(tmp_path):
    r = run_process([PY, "-c", "print('x' * 100000)"], cwd=str(tmp_path), timeout_s=30,
                    max_output_bytes=1000)
    assert len(r.stdout) == 1000 and r.stdout_truncated and r.returncode == 0


def test_run_process_does_not_use_a_shell(tmp_path):
    # With a shell, "&&" would chain a second command; here it is just an argument.
    r = run_process([PY, "-c", "import sys; print(sys.argv[1:])", "a", "&&", "echo", "pwned"],
                    cwd=str(tmp_path), timeout_s=30)
    assert "['a', '&&', 'echo', 'pwned']" in r.stdout


def test_run_cli_task_missing_binary():
    code, _, err = run_cli_task(["definitely-not-a-real-binary-xyz"], cwd=".")
    assert code == 127 and "not found" in err


def test_run_cli_task_timeout_returns_124(tmp_path):
    code, _, err = run_cli_task([PY, "-c", "import time; time.sleep(30)"], cwd=str(tmp_path), timeout_s=1)
    assert code == 124 and "timed out" in err


def test_delegate_to_claude_success(monkeypatch, httpx_mock):
    monkeypatch.setattr("mcp_server.delegate.run_cli_task", lambda *a, **k: (0, "done", ""))
    httpx_mock.add_response(method="GET", url=re.compile(r".*/openapi.json$"),
                            json={"info": {"title": "A2A_Link balancer"}})
    httpx_mock.add_response(method="POST", url=re.compile(r".*/tasks$"), json={"id": "t1"})
    httpx_mock.add_response(method="POST", url=re.compile(r".*/claim$"), json={"claimed": True})
    httpx_mock.add_response(method="POST", url=re.compile(r".*/complete$"), json={})
    assert delegate("claude", "say hi", "http://127.0.0.1:8000") == "done"


def test_delegate_to_unknown_agent_raises():
    with pytest.raises(ValueError):
        delegate("gemini", "say hi", "http://127.0.0.1:8000")


def test_delegate_reports_cli_failure_as_error_string(monkeypatch, httpx_mock):
    monkeypatch.setattr("mcp_server.delegate.run_cli_task", lambda *a, **k: (1, "", "bad prompt"))
    httpx_mock.add_response(method="GET", url=re.compile(r".*/openapi.json$"),
                            json={"info": {"title": "A2A_Link balancer"}})
    httpx_mock.add_response(method="POST", url=re.compile(r".*/tasks$"), json={"id": "t1"})
    httpx_mock.add_response(method="POST", url=re.compile(r".*/claim$"), json={"claimed": True})
    httpx_mock.add_response(method="POST", url=re.compile(r".*/fail$"), json={"status": "pending"})
    result = delegate("codex", "say hi", "http://127.0.0.1:8000")
    assert result.startswith("ERROR: ") and "bad prompt" in result


def test_delegate_tool_returns_result_inline_when_fast(monkeypatch):
    import mcp_server.server as srv

    class FakeMgr:
        def start(self, agent, prompt, cwd, isolation="auto"):
            return {"task_id": "t1", "agent": agent, "status": "in_progress"}

        def wait(self, task_id, wait_s):
            return {"task_id": task_id, "agent": "claude", "status": "done", "result": "ok"}

    monkeypatch.setattr(srv, "DELEGATIONS", FakeMgr())
    assert srv.delegate_to_claude("hello")["result"] == "ok"


NPM_SHIM = r"""@ECHO off
SETLOCAL
endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\fake\bin\cli.js" %*
"""


@pytest.mark.skipif(sys.platform != "win32", reason="npm .cmd shims are Windows-only")
def test_npm_shim_is_unwrapped_to_node_script(tmp_path, monkeypatch):
    from mcp_server.cli_executor import build_argv

    (tmp_path / "node_modules" / "fake" / "bin").mkdir(parents=True)
    (tmp_path / "node_modules" / "fake" / "bin" / "cli.js").write_text("// js")
    (tmp_path / "fakeagent.cmd").write_text(NPM_SHIM)
    (tmp_path / "node.exe").write_bytes(b"")
    monkeypatch.setenv("A2A_LINK_FAKEAGENT_BIN", str(tmp_path / "fakeagent.cmd"))
    prompt = 'say "hi" & del %USERPROFILE% | calc'
    argv, err = build_argv(["fakeagent", "exec", prompt])
    assert err == ""
    assert argv[0] == str(tmp_path / "node.exe")
    assert argv[1].endswith("cli.js") and argv[2:] == ["exec", prompt]  # prompt passed verbatim, no cmd.exe


@pytest.mark.skipif(sys.platform != "win32", reason="batch files are Windows-only")
def test_unknown_batch_file_refuses_metacharacters(tmp_path, monkeypatch):
    from mcp_server.cli_executor import build_argv

    (tmp_path / "tool.cmd").write_text("@echo %*")
    monkeypatch.setenv("A2A_LINK_TOOL_BIN", str(tmp_path / "tool.cmd"))
    argv, err = build_argv(["tool", "a & calc"])
    assert argv is None and "metacharacters" in err


def test_delegate_rejects_missing_cwd():
    assert delegate("codex", "hi", "http://127.0.0.1:8000", cwd="Z:/definitely/missing").startswith("ERROR: cwd")
