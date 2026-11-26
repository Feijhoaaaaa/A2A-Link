import pytest


@pytest.fixture(autouse=True)
def _isolated_a2a_home(tmp_path_factory, monkeypatch):
    """Task logs and worktrees go to a temp dir, never to the real ~/.a2a_link."""
    home = tmp_path_factory.mktemp("a2a_home")
    monkeypatch.setenv("A2A_LINK_HOME", str(home))
    monkeypatch.setenv("A2A_LINK_WORKTREE_DIR", str(home / "worktrees"))
    monkeypatch.setenv("CODEX_HOME", str(home / "codex"))  # never read the real ~/.codex in tests
    from balancer import codex_usage
    codex_usage._cache.update(key=None, value=None, checked=0.0)
    return home
