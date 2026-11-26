import json
import os
import sys
from pathlib import Path

import anyio
import pytest

from mcp_server.terminal.executor import TerminalExecutor
from mcp_server.terminal.policy import Level, PolicyError, evaluate, load_policy

PY_DIR = str(Path(sys.executable).parent)

SCRIPTS = {
    "hello.py": "import sys; print('hello from', sys.argv[1:]); print('warn', file=sys.stderr)",
    "fail.py": "import sys; print('bad', file=sys.stderr); sys.exit(7)",
    "slow.py": "import time; time.sleep(30)",
    "danger.py": "open('touched.txt', 'w').write('x'); print('danger ran')",
    "env.py": "import os; print('SECRET=' + str(os.environ.get('A2A_TEST_SECRET')))",
}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    for name, code in SCRIPTS.items():
        (work / name).write_text(code, encoding="utf-8")
    (work / "sub").mkdir()
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("PATH", PY_DIR + os.pathsep + os.environ.get("PATH", ""))
    policy = {
        "allowed_roots": [str(work)],
        "audit_log": str(cfg / "audit.jsonl"),
        "default_timeout_s": 20,
        "max_timeout_s": 2,
        "commands": {
            "python": {
                "default": "deny",
                "deny_args": ["-c"],
                "confirm_args": ["--force"],
                "rules": [
                    {"prefix": ["hello.py"], "level": "safe"},
                    {"prefix": ["fail.py"], "level": "safe"},
                    {"prefix": ["slow.py"], "level": "safe"},
                    {"prefix": ["env.py"], "level": "safe"},
                    {"prefix": ["danger.py"], "level": "confirm"},
                ],
            }
        },
    }
    path = cfg / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    return work, path, cfg / "audit.jsonl"


def _run(executor, argv, cwd, confirm=None, timeout_s=None):
    return anyio.run(executor.run, argv, str(cwd), timeout_s, confirm)


def _audit(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]


# --- policy evaluation -------------------------------------------------------

@pytest.mark.parametrize("argv,why", [
    (["notepad"], "not in the allowlist"),
    (["cmd", "/c", "dir"], "shell/system tool"),
    (["powershell", "-Command", "ls"], "shell/system tool"),
    (["bash", "-c", "ls"], "shell/system tool"),
    (["C:/Windows/System32/cmd.exe"], "bare command name"),
    (["python", "-c", "print(1)"], "denied"),
    (["python", "other.py"], "default level"),
    (["python", "hello.py", "../../outside.txt"], "escapes allowed roots"),
    (["python", "hello.py", "C:/Windows/win.ini"], "escapes allowed roots"),
    (["python", "hello.py", "--out=../x"], "escapes allowed roots"),
    ([], "non-empty"),
    (["python", "hello.py\x00"], "NUL"),
])
def test_evaluate_denies(setup, argv, why):
    work, path, _ = setup
    d = evaluate(load_policy(path), argv, str(work))
    assert d.level is Level.DENY and why in d.reason


def test_evaluate_denies_cwd_outside_roots(setup, tmp_path):
    work, path, _ = setup
    d = evaluate(load_policy(path), ["python", "hello.py"], str(tmp_path))
    assert d.level is Level.DENY and "outside allowed roots" in d.reason


def test_evaluate_allows_subdir_and_inner_paths(setup):
    work, path, _ = setup
    d = evaluate(load_policy(path), ["python", "hello.py", "sub/../sub"], str(work / "sub"))
    assert d.level is Level.SAFE


def test_confirm_args_escalate_safe_to_confirm(setup):
    work, path, _ = setup
    d = evaluate(load_policy(path), ["python", "hello.py", "--force"], str(work))
    assert d.level is Level.CONFIRM


def test_policy_cannot_allowlist_shell(tmp_path):
    root = tmp_path / "r"; root.mkdir()
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"allowed_roots": [str(root)], "commands": {"PowerShell.exe": {"default": "safe"}}}))
    with pytest.raises(PolicyError, match="never be allowlisted"):
        load_policy(p)


def test_policy_file_must_be_outside_roots(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"allowed_roots": [str(tmp_path)], "commands": {}}))
    with pytest.raises(PolicyError, match="outside allowed root"):
        load_policy(p)


def test_example_policy_parses(tmp_path):
    example = json.loads((Path(__file__).parents[2] / "config" / "terminal-policy.example.json").read_text())
    root = tmp_path / "r"; root.mkdir()
    example["allowed_roots"] = [str(root)]
    example["audit_log"] = str(tmp_path / "audit.jsonl")
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(example))
    pol = load_policy(p)
    assert pol.commands["git"].level_for(["status"])[0] is Level.SAFE
    assert pol.commands["git"].level_for(["push"])[0] is Level.DENY


# --- execution ---------------------------------------------------------------

def test_safe_command_runs_and_captures_everything(setup):
    work, path, audit = setup
    out = _run(TerminalExecutor(load_policy(path)), ["python", "hello.py", "a b"], work)
    assert out.status == "ok" and out.returncode == 0
    assert "hello from ['a b']" in out.stdout and "warn" in out.stderr
    rec = _audit(audit)[-1]
    assert rec["event"] == "executed" and rec["argv"] == ["python", "hello.py", "a b"]
    assert "stdout" not in rec and rec["stdout_bytes"] > 0


def test_nonzero_exit_is_reported(setup):
    work, path, _ = setup
    out = _run(TerminalExecutor(load_policy(path)), ["python", "fail.py"], work)
    assert out.status == "failed" and out.returncode == 7 and "bad" in out.stderr


def test_timeout_is_clamped_and_enforced(setup):
    work, path, _ = setup
    out = _run(TerminalExecutor(load_policy(path)), ["python", "slow.py"], work, timeout_s=999)
    assert out.status == "timeout" and out.timed_out and out.returncode == 124
    assert out.duration_s < 15  # clamped to max_timeout_s=2, not 999


def test_environment_is_scrubbed(setup, monkeypatch):
    work, path, _ = setup
    monkeypatch.setenv("A2A_TEST_SECRET", "hunter2")
    out = _run(TerminalExecutor(load_policy(path)), ["python", "env.py"], work)
    assert "SECRET=None" in out.stdout


def test_denied_command_is_audited_and_not_run(setup):
    work, path, audit = setup
    out = _run(TerminalExecutor(load_policy(path)), ["cmd", "/c", "echo hi"], work)
    assert out.status == "denied" and out.returncode is None
    assert _audit(audit)[-1]["event"] == "denied"


def test_confirm_command_declined_does_not_run(setup):
    work, path, audit = setup

    async def no(*_):
        return False

    out = _run(TerminalExecutor(load_policy(path)), ["python", "danger.py"], work, confirm=no)
    assert out.status == "declined" and not (work / "touched.txt").exists()
    assert _audit(audit)[-1]["event"] == "declined"


def test_confirm_command_without_channel_is_refused(setup):
    work, path, _ = setup
    out = _run(TerminalExecutor(load_policy(path)), ["python", "danger.py"], work, confirm=None)
    assert out.status == "declined" and not (work / "touched.txt").exists()


def test_confirm_command_when_confirmer_errors_is_refused(setup):
    work, path, _ = setup

    async def boom(*_):
        raise RuntimeError("Elicitation not supported")

    out = _run(TerminalExecutor(load_policy(path)), ["python", "danger.py"], work, confirm=boom)
    assert out.status == "declined" and "Elicitation not supported" in out.reason


def test_confirm_command_approved_runs(setup):
    work, path, audit = setup
    seen = {}

    async def yes(argv, cwd, reason):
        seen["argv"], seen["cwd"] = argv, cwd
        return True

    out = _run(TerminalExecutor(load_policy(path)), ["python", "danger.py"], work, confirm=yes)
    assert out.status == "ok" and (work / "touched.txt").exists()
    assert seen["argv"] == ["python", "danger.py"]
    assert [r["event"] for r in _audit(audit)][-2:] == ["confirmed", "executed"]


def test_unwritable_audit_log_is_rejected_at_load(tmp_path):
    root = tmp_path / "r"; root.mkdir()
    blocker = tmp_path / "not_a_dir"; blocker.write_text("x")
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"allowed_roots": [str(root)], "audit_log": str(blocker / "audit.jsonl"),
                             "commands": {}}))
    with pytest.raises(PolicyError, match="audit_log .* is not writable"):
        load_policy(p)


def test_pinned_executable_path_is_used(setup):
    work, path, _ = setup
    data = json.loads(path.read_text())
    data["commands"]["python"]["path"] = sys.executable
    path.write_text(json.dumps(data))
    d = evaluate(load_policy(path), ["python", "hello.py"], str(work))
    assert d.level is Level.SAFE and d.executable == sys.executable


def test_pinned_path_must_exist(setup):
    work, path, _ = setup
    data = json.loads(path.read_text())
    data["commands"]["python"]["path"] = "C:/nope/python.exe"
    path.write_text(json.dumps(data))
    with pytest.raises(PolicyError, match="absolute path to an existing file"):
        load_policy(path)


def test_pinned_path_to_shell_is_still_denied(setup, tmp_path):
    work, path, _ = setup
    fake_shell = tmp_path / "cfg" / "powershell.exe"
    fake_shell.write_bytes(b"")
    data = json.loads(path.read_text())
    data["commands"]["python"]["path"] = str(fake_shell)
    path.write_text(json.dumps(data))
    d = evaluate(load_policy(path), ["python", "hello.py"], str(work))
    assert d.level is Level.DENY and "shell/system tool" in d.reason
