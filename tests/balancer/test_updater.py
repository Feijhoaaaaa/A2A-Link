from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from balancer.updater import UpdateError, update_installation


def _result(args: list[str], stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args, returncode, stdout, "git failure" if returncode else "")


def _checkout(tmp_path: Path, version: str = "2.0.0") -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "a2a-link"\nversion = "{version}"\n', encoding="utf-8"
    )
    return root


def test_update_fast_forwards_official_clean_checkout_and_reinstalls(tmp_path):
    root = _checkout(tmp_path)
    calls: list[tuple[list[str], Path]] = []

    def run(args, *, cwd, **kwargs):
        calls.append((args, cwd))
        if args == ["git", "rev-parse", "--show-toplevel"]:
            return _result(args, str(root))
        if args == ["git", "branch", "--show-current"]:
            return _result(args, "main\n")
        if args == ["git", "remote", "get-url", "origin"]:
            return _result(args, "git@github.com:Feijhoaaaaa/A2A-Link.git\n")
        if args == ["git", "status", "--porcelain", "--untracked-files=all"]:
            return _result(args)
        if args == ["git", "rev-parse", "HEAD"]:
            return _result(args, "old-sha\n" if sum(c[0] == args for c in calls) == 1 else "new-sha\n")
        if args == ["git", "merge", "--ff-only", "origin/main"]:
            (root / "pyproject.toml").write_text(
                '[project]\nname = "a2a-link"\nversion = "2.1.0"\n', encoding="utf-8"
            )
            return _result(args)
        return _result(args)

    result = update_installation(root, run=run, python_executable="python-test")

    assert result == {
        "status": "updated", "from_version": "2.0.0", "to_version": "2.1.0",
        "restart_required": True,
    }
    assert [args for args, _ in calls] == [
        ["git", "rev-parse", "--show-toplevel"],
        ["git", "branch", "--show-current"],
        ["git", "remote", "get-url", "origin"],
        ["git", "status", "--porcelain", "--untracked-files=all"],
        ["git", "rev-parse", "HEAD"],
        ["git", "fetch", "origin", "main:refs/remotes/origin/main"],
        ["git", "merge", "--ff-only", "origin/main"],
        ["git", "rev-parse", "HEAD"],
        ["python-test", "-m", "pip", "install", "-e", "."],
    ]
    assert all(cwd == root for _, cwd in calls)


def test_update_refuses_dirty_checkout_before_fetch(tmp_path):
    root = _checkout(tmp_path)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args == ["git", "rev-parse", "--show-toplevel"]:
            return _result(args, str(root))
        if args == ["git", "branch", "--show-current"]:
            return _result(args, "main")
        if args == ["git", "remote", "get-url", "origin"]:
            return _result(args, "https://github.com/Feijhoaaaaa/A2A-Link.git")
        if args == ["git", "status", "--porcelain", "--untracked-files=all"]:
            return _result(args, " M README.md\n")
        raise AssertionError(f"unexpected command: {args}")

    with pytest.raises(UpdateError, match="clean"):
        update_installation(root, run=run)

    assert not any(args[:2] == ["git", "fetch"] for args in calls)


@pytest.mark.parametrize(
    ("branch", "remote", "expected_error"),
    [
        ("feature", "https://github.com/Feijhoaaaaa/A2A-Link.git", "main branch"),
        ("main", "https://github.com/someone/A2A-Link.git", "official"),
    ],
)
def test_update_refuses_wrong_branch_or_remote(tmp_path, branch, remote, expected_error):
    root = _checkout(tmp_path)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args == ["git", "rev-parse", "--show-toplevel"]:
            return _result(args, str(root))
        if args == ["git", "branch", "--show-current"]:
            return _result(args, branch)
        if args == ["git", "remote", "get-url", "origin"]:
            return _result(args, remote)
        raise AssertionError(f"unexpected command: {args}")

    with pytest.raises(UpdateError, match=expected_error):
        update_installation(root, run=run)

    assert not any(args[:2] == ["git", "fetch"] for args in calls)


def test_up_to_date_checkout_reinstalls_editable_source_without_restart(tmp_path):
    root = _checkout(tmp_path)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args == ["git", "rev-parse", "--show-toplevel"]:
            return _result(args, str(root))
        if args == ["git", "branch", "--show-current"]:
            return _result(args, "main")
        if args == ["git", "remote", "get-url", "origin"]:
            return _result(args, "https://github.com/Feijhoaaaaa/A2A-Link")
        if args == ["git", "status", "--porcelain", "--untracked-files=all"]:
            return _result(args)
        if args == ["git", "rev-parse", "HEAD"]:
            return _result(args, "same-sha\n")
        return _result(args)

    result = update_installation(root, run=run)

    assert result["status"] == "up_to_date"
    assert result["from_version"] == result["to_version"] == "2.0.0"
    assert result["restart_required"] is False
    assert [sys.executable, "-m", "pip", "install", "-e", "."] in calls


def test_agentctl_update_cli_reports_restart_requirement(monkeypatch):
    import balancer.cli as cli

    monkeypatch.setattr(cli, "update_installation", lambda: {
        "status": "updated", "from_version": "2.0.0", "to_version": "2.1.0",
        "restart_required": True,
    })
    result = CliRunner().invoke(cli.main, ["update"])

    assert result.exit_code == 0
    assert "Updated A2A-Link 2.0.0 → 2.1.0." in result.output
    assert "Restart the balancer, runner, and MCP clients" in result.output
