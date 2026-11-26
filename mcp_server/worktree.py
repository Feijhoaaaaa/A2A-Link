"""Git worktree isolation for delegated agents.

A delegated agent runs asynchronously, so the caller may keep editing the same
checkout. To avoid the two overwriting each other, the child works in its own
worktree on branch `a2a/<task_id>`; its changes are committed there and the
caller merges them when ready.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from mcp_server.process import run_process


def worktree_base() -> Path:
    return Path(os.environ.get("A2A_LINK_WORKTREE_DIR", Path.home() / ".a2a_link" / "worktrees"))


def git(args: list[str], cwd: str, timeout_s: float = 120) -> tuple[int, str, str]:
    exe = shutil.which("git")
    if exe is None:
        return 127, "", "git not found on PATH"
    r = run_process([exe, *args], cwd=cwd, timeout_s=timeout_s,
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    return r.returncode, r.stdout.strip(), r.stderr.strip()


@dataclass
class Workspace:
    mode: str                      # "worktree" or "none"
    cwd: str                       # where the child agent runs
    repo: str | None = None
    path: str | None = None
    branch: str | None = None
    base: str | None = None
    warnings: list[str] = field(default_factory=list)
    commit: str | None = None
    diffstat: str | None = None

    def view(self) -> dict:
        out: dict = {"mode": self.mode, "cwd": self.cwd}
        if self.mode == "worktree":
            out.update(branch=self.branch, path=self.path, base=self.base, commit=self.commit,
                       changes=self.diffstat or "no changes",
                       merge=f'git -C "{self.repo}" merge {self.branch}',
                       cleanup=(f'git -C "{self.repo}" worktree remove --force "{self.path}" && '
                                f'git -C "{self.repo}" branch -D {self.branch}'))
        if self.warnings:
            out["warnings"] = self.warnings
        return out


def prepare(cwd: str, task_id: str, isolation: str) -> Workspace:
    """isolation: "auto" (worktree when cwd is in a git repo with commits), "worktree", "none"."""
    if isolation == "none":
        return Workspace("none", cwd)
    rc, top, _ = git(["rev-parse", "--show-toplevel"], cwd)
    rc_head, base, _ = git(["rev-parse", "HEAD"], cwd) if rc == 0 else (1, "", "")
    if rc != 0 or rc_head != 0:
        reason = "not a git repository" if rc != 0 else "repository has no commits yet"
        if isolation == "worktree":
            raise RuntimeError(f"cannot isolate in a worktree: {reason}")
        return Workspace("none", cwd, warnings=[f"running in the shared checkout ({reason}); "
                                                "avoid editing the same files meanwhile"])
    repo = str(Path(top).resolve())
    path = worktree_base() / f"{Path(repo).name}-{task_id}"
    branch = f"a2a/{task_id}"
    path.parent.mkdir(parents=True, exist_ok=True)
    rc, _, err = git(["worktree", "add", "-b", branch, str(path), base], repo)
    if rc != 0:
        raise RuntimeError(f"git worktree add failed: {err}")
    ws = Workspace("worktree", str(path / os.path.relpath(Path(cwd).resolve(), repo)),
                   repo=repo, path=str(path), branch=branch, base=base)
    _, dirty, _ = git(["status", "--porcelain"], repo)
    if dirty:
        ws.warnings.append("the caller's checkout has uncommitted changes; they are NOT in the "
                           "worktree (it starts from the last commit)")
    return ws


def prepare_issue_worktrees(cwd: str, paths: dict[str, dict[str, str]], base_sha: str) -> dict[str, dict[str, str]]:
    """Create or recover the managed participant worktrees for one issue.

    The operation is idempotent per path and branch. It refuses a dirty source checkout
    so local-only user work cannot be silently omitted from the issue base.
    """
    rc, repo, err = git(["rev-parse", "--show-toplevel"], cwd)
    if rc:
        raise RuntimeError(f"issue worktrees require a Git repository: {err}")
    repo = str(Path(repo).resolve())
    rc, status, err = git(["status", "--porcelain", "--untracked-files=all"], repo)
    if rc:
        raise RuntimeError(f"could not inspect source checkout: {err}")
    if status:
        raise RuntimeError("source checkout has uncommitted changes; agree on a base before opening the issue")
    if not base_sha or any(c not in "0123456789abcdef" for c in base_sha) or len(base_sha) != 40:
        raise ValueError("base_sha must be a full lowercase 40-character commit SHA")
    rc, _, err = git(["cat-file", "-e", f"{base_sha}^{{commit}}"], repo)
    if rc:
        raise RuntimeError(f"base commit is unavailable in the source repository: {err or base_sha}")

    base_dir = worktree_base().resolve()
    prepared: dict[str, dict[str, str]] = {}
    for key, entry in paths.items():
        path = Path(entry["worktree"]).resolve()
        branch = entry["branch"]
        try:
            path.relative_to(base_dir)
        except ValueError:
            raise ValueError(f"worktree path is outside the configured A2A_Link worktree root: {path}") from None
        if git(["check-ref-format", "--branch", branch], repo)[0] != 0:
            raise ValueError(f"invalid worktree branch name: {branch!r}")

        if path.exists():
            rc, top, _ = git(["rev-parse", "--show-toplevel"], str(path))
            rc_branch, current_branch, _ = git(["branch", "--show-current"], str(path))
            if rc or rc_branch or Path(top).resolve() != path or current_branch != branch:
                raise RuntimeError(f"existing issue worktree does not match its recorded path and branch: {path}")
        else:
            if git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], repo)[0] == 0:
                args = ["worktree", "add", str(path), branch]
            else:
                args = ["worktree", "add", "-b", branch, str(path), base_sha]
            rc, _, err = git(args, repo)
            if rc:
                raise RuntimeError(f"could not prepare {key} worktree: {err}")
            rc, top, _ = git(["rev-parse", "--show-toplevel"], str(path))
            rc_branch, current_branch, _ = git(["branch", "--show-current"], str(path))
            if rc or rc_branch or Path(top).resolve() != path or current_branch != branch:
                raise RuntimeError(f"created issue worktree failed verification: {path}")
        prepared[key] = {"cwd": str(path), "worktree": str(path), "branch": branch}
    return prepared


def finalize(ws: Workspace, summary: str) -> None:
    """Commit whatever the child changed onto its branch and record a diffstat."""
    if ws.mode != "worktree":
        return
    git(["add", "-A"], ws.path)
    rc, _, _ = git(["diff", "--cached", "--quiet"], ws.path)
    if rc == 1:  # staged changes exist
        ident = []
        _, name, _ = git(["config", "user.name"], ws.path)
        _, email, _ = git(["config", "user.email"], ws.path)
        if not name or not email:
            ident = ["-c", "user.name=A2A_Link", "-c", "user.email=a2a-link@localhost"]
        msg = f"a2a: delegated task {ws.branch}\n\n{summary[:2000]}"
        rc, _, err = git([*ident, "commit", "-q", "-m", msg], ws.path)
        if rc != 0:
            ws.warnings.append(f"could not commit the delegated changes: {err[-300:]}")
    _, ws.commit, _ = git(["rev-parse", "HEAD"], ws.path)
    if ws.commit == ws.base:
        ws.commit = None
    _, ws.diffstat, _ = git(["diff", "--stat", f"{ws.base}..HEAD"], ws.path)
