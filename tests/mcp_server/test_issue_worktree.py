import subprocess

import pytest

from mcp_server.worktree import prepare_issue_worktrees


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True,
                          capture_output=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "seed.txt").write_text("base\n")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-q", "-m", "base")
    return root


def test_issue_worktrees_use_exact_base_and_are_resumable(repo, tmp_path, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("A2A_LINK_WORKTREE_DIR", str(tmp_path / "issues"))
    paths = {
        "claude": {"worktree": str(tmp_path / "issues" / "i1-claude"), "branch": "a2a/issue-i1/claude"},
        "codex": {"worktree": str(tmp_path / "issues" / "i1-codex"), "branch": "a2a/issue-i1/codex"},
    }

    created = prepare_issue_worktrees(str(repo), paths, base)
    resumed = prepare_issue_worktrees(str(repo), paths, base)

    assert created == resumed
    for key, entry in paths.items():
        assert _git(entry["worktree"], "rev-parse", "HEAD") == base
        assert _git(entry["worktree"], "branch", "--show-current") == entry["branch"]
        assert created[key]["cwd"] == entry["worktree"]


def test_issue_worktree_start_refuses_dirty_source_without_touching_it(repo, tmp_path, monkeypatch):
    (repo / "seed.txt").write_text("uncommitted user change\n")
    monkeypatch.setenv("A2A_LINK_WORKTREE_DIR", str(tmp_path))
    paths = {"claude": {"worktree": str(tmp_path / "i-claude"), "branch": "a2a/issue-i/claude"},
             "codex": {"worktree": str(tmp_path / "i-codex"), "branch": "a2a/issue-i/codex"}}

    with pytest.raises(RuntimeError, match="uncommitted changes"):
        prepare_issue_worktrees(str(repo), paths, _git(repo, "rev-parse", "HEAD"))

    assert (repo / "seed.txt").read_text() == "uncommitted user change\n"
    assert not (tmp_path / "i-claude").exists()
