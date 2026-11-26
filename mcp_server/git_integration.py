"""Git candidate construction, exact verification and compare-and-swap base updates."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp_server.process import run_process
from mcp_server.supervisor import SupervisorRuntime
from mcp_server.worktree import worktree_base


@dataclass
class GitOutput:
    exit_code: int
    stdout: str
    stderr: str

    def as_dict(self) -> dict[str, Any]:
        max_chars = 200_000
        stdout = self.stdout[:max_chars]
        stderr = self.stderr[:max_chars]
        return {"exit_code": self.exit_code, "stdout": stdout, "stderr": stderr,
                "truncated": len(stdout) < len(self.stdout) or len(stderr) < len(self.stderr)}


class GitIntegrator:
    def __init__(self, home: str | Path, runtime: SupervisorRuntime):
        self.home = Path(home).resolve()
        self.runtime = runtime

    @staticmethod
    def _git(argv: list[str], cwd: str | Path) -> GitOutput:
        result = subprocess.run(["git", *argv], cwd=cwd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", check=False,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        return GitOutput(result.returncode, result.stdout, result.stderr)

    def prepare(self, repo: str, issue_id: str, integration_id: str,
                base_sha: str, reset_existing: bool = False) -> tuple[Path, str]:
        repo_path = Path(repo).resolve()
        head = self._git(["rev-parse", "HEAD"], repo_path)
        if head.exit_code or head.stdout.strip() != base_sha:
            raise RuntimeError("integration checkout HEAD does not match expected base SHA")
        ref = self._git(["symbolic-ref", "--quiet", "HEAD"], repo_path)
        if ref.exit_code:
            raise RuntimeError("issue base checkout is detached; cannot determine its update ref")
        worktree = self.home / "integrations" / f"{issue_id}-{integration_id}"
        branch = f"a2a/integration-{issue_id}-{integration_id}"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        if worktree.exists():
            top = self._git(["rev-parse", "--show-toplevel"], worktree)
            if top.exit_code or Path(top.stdout.strip()).resolve() != worktree:
                raise RuntimeError(f"integration path is not the recorded Git worktree: {worktree}")
            current_branch = self._git(["branch", "--show-current"], worktree)
            if current_branch.exit_code or current_branch.stdout.strip() != branch:
                raise RuntimeError("existing integration worktree is on an unexpected branch")
            if reset_existing:
                reset = self._git(["reset", "--hard", base_sha], worktree)
                clean = self._git(["clean", "-fd"], worktree)
                if reset.exit_code or clean.exit_code:
                    raise RuntimeError(reset.stderr or clean.stderr or "could not reset incomplete integration worktree")
        else:
            result = self._git(["worktree", "add", "-b", branch, str(worktree), base_sha], repo_path)
            if result.exit_code:
                raise RuntimeError(result.stderr or "could not create integration worktree")
        return worktree, ref.stdout.strip()

    def merge_candidate(self, repo: str, issue_id: str, integration_id: str,
                        base_sha: str, source_shas: dict[str, str]) -> dict[str, Any]:
        worktree, ref = self.prepare(repo, issue_id, integration_id, base_sha, reset_existing=True)
        outputs = []
        for participant in sorted(source_shas):
            output = self._git(["-c", "user.name=A2A_Link", "-c", "user.email=a2a-link@localhost",
                                "merge", "--no-ff", "--no-edit", source_shas[participant]], worktree)
            outputs.append(output)
            if output.exit_code:
                self._git(["merge", "--abort"], worktree)
                return {"status": "conflict", "candidate_sha": None,
                        "output": self._combine(outputs).as_dict(),
                        "detail": f"merge conflict while applying {participant} source commit"}
        candidate = self._git(["rev-parse", "HEAD"], worktree)
        if candidate.exit_code:
            raise RuntimeError(candidate.stderr or "could not read candidate commit")
        return {"status": "merged", "candidate_sha": candidate.stdout.strip(),
                "output": self._combine(outputs).as_dict(), "worktree": str(worktree), "ref": ref}

    @staticmethod
    def _combine(outputs: list[GitOutput]) -> GitOutput:
        return GitOutput(outputs[-1].exit_code if outputs else 0,
                         "\n".join(item.stdout for item in outputs),
                         "\n".join(item.stderr for item in outputs))

    @staticmethod
    def verify(candidate_worktree: str | Path, argv: list[str], timeout_s: float = 1800) -> dict[str, Any]:
        if not argv or not all(isinstance(arg, str) for arg in argv):
            return {"status": "fail", "exit_code": 1, "stdout": "", "stderr": "verification argv is empty or invalid",
                    "duration_s": 0.0, "truncated": False}
        result = run_process(argv, cwd=str(candidate_worktree), timeout_s=timeout_s,
                             env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        return {"status": "pass" if result.returncode == 0 else "fail",
                "exit_code": result.returncode, "stdout": result.stdout,
                "stderr": result.stderr, "duration_s": result.duration_s,
                "truncated": result.stdout_truncated or result.stderr_truncated}

    def advance_ref(self, repo: str, ref: str, candidate_sha: str,
                    expected_base_sha: str) -> GitOutput:
        result = self._git(["update-ref", ref, candidate_sha, expected_base_sha], repo)
        if result.exit_code:
            raise RuntimeError(f"Git ref compare-and-swap failed: {result.stderr or result.stdout}")
        return result

    def ref_value(self, repo: str, ref: str) -> str | None:
        result = self._git(["rev-parse", "--verify", ref], repo)
        return result.stdout.strip() if result.exit_code == 0 else None

    def participant_worktree(self, issue_id: str, participant: str) -> tuple[Path, str]:
        path = (worktree_base() / f"{issue_id}-{participant}").resolve()
        branch = f"a2a/issue-{issue_id}/{participant}"
        return path, branch

    def _participant_state(self, repo: str, issue_id: str, participant: str,
                           source_sha: str, candidate_sha: str) -> dict[str, Any]:
        path, branch = self.participant_worktree(issue_id, participant)
        if not path.is_dir():
            raise RuntimeError(f"participant worktree is missing: {participant} ({path})")
        top = self._git(["rev-parse", "--show-toplevel"], path)
        common = self._git(["rev-parse", "--git-common-dir"], path)
        repo_common = self._git(["rev-parse", "--git-common-dir"], repo)
        if top.exit_code or Path(top.stdout.strip()).resolve() != path:
            raise RuntimeError(f"participant worktree path does not match the recorded path: {participant}")
        if common.exit_code or repo_common.exit_code or (path / common.stdout.strip()).resolve() != (Path(repo) / repo_common.stdout.strip()).resolve():
            raise RuntimeError(f"participant worktree belongs to a different Git repository: {participant}")
        current_branch = self._git(["branch", "--show-current"], path)
        if current_branch.exit_code or current_branch.stdout.strip() != branch:
            raise RuntimeError(f"participant worktree is on an unexpected branch: {participant}")
        head = self._git(["rev-parse", "HEAD"], path)
        ref_name = f"refs/heads/{branch}"
        ref = self._git(["rev-parse", "--verify", ref_name], repo)
        status = self._git(["status", "--porcelain", "--untracked-files=all"], path)
        if head.exit_code or ref.exit_code or status.exit_code:
            raise RuntimeError(f"could not inspect participant worktree: {participant}")
        head_sha, ref_sha = head.stdout.strip(), ref.stdout.strip()
        if head_sha not in (source_sha, candidate_sha):
            raise RuntimeError(f"participant worktree HEAD changed unexpectedly: {participant} ({head_sha})")
        if ref_sha not in (source_sha, candidate_sha):
            raise RuntimeError(f"participant branch changed unexpectedly: {participant} ({ref_sha})")
        pending_reset = False
        if ref_sha == source_sha:
            if head_sha != source_sha or status.stdout.strip():
                raise RuntimeError(f"participant worktree is dirty or changed from its accepted commit: {participant}")
        elif head_sha != candidate_sha:
            raise RuntimeError(f"participant HEAD does not follow its candidate branch ref: {participant}")
        elif status.stdout.strip():
            # update-ref changes attached HEAD before reset updates the index and checkout.
            # Accept only the exact old source tree/index with no untracked files, proving
            # this is the journaled crash window and not user work.
            worktree_diff = self._git(["diff", "--quiet", source_sha, "--"], path)
            index_diff = self._git(["diff", "--cached", "--quiet", source_sha, "--"], path)
            untracked = self._git(["ls-files", "--others", "--exclude-standard"], path)
            if worktree_diff.exit_code != 0 or index_diff.exit_code != 0 or untracked.stdout.strip():
                raise RuntimeError(f"participant worktree is dirty: {participant}")
            pending_reset = True
        return {"participant": participant, "path": path, "branch": branch,
                "ref": ref_name, "head_sha": head_sha, "ref_sha": ref_sha,
                "source_sha": source_sha, "candidate_sha": candidate_sha,
                "pending_reset": pending_reset}

    def preflight_participant_worktrees(self, repo: str, issue_id: str,
                                        source_shas: dict[str, str], candidate_sha: str) -> None:
        """Require every accepted worktree to be clean and unchanged before base advancement."""
        for participant, source_sha in source_shas.items():
            ancestor = self._git(["merge-base", "--is-ancestor", source_sha, candidate_sha], repo)
            if ancestor.exit_code:
                raise RuntimeError(f"integration candidate does not contain {participant}'s accepted commit")
            state = self._participant_state(repo, issue_id, participant, source_sha, candidate_sha)
            if state["head_sha"] != source_sha or state["ref_sha"] != source_sha or state["pending_reset"]:
                raise RuntimeError(f"participant worktree is not at its accepted source commit: {participant}")

    def sync_participant_worktrees(self, repo: str, issue_id: str, integration_id: str,
                                   source_shas: dict[str, str], candidate_sha: str,
                                   journal) -> list[dict[str, str]]:
        """CAS each accepted participant branch to candidate, then reset its clean checkout."""
        completed: list[dict[str, str]] = []
        records = journal.read()
        for participant, source_sha in source_shas.items():
            ancestor = self._git(["merge-base", "--is-ancestor", source_sha, candidate_sha], repo)
            if ancestor.exit_code:
                raise RuntimeError(f"integration candidate does not contain accepted source for {participant}")
            previous_done = any(row.get("event") == "worktree_sync_done"
                                and row.get("integration_id") == integration_id
                                and row.get("participant") == participant for row in records)
            state = self._participant_state(repo, issue_id, participant, source_sha, candidate_sha)
            if previous_done:
                if state["head_sha"] != candidate_sha or state["ref_sha"] != candidate_sha or state["pending_reset"]:
                    raise RuntimeError(f"completed participant sync no longer matches candidate: {participant}")
                completed.append({"participant": participant, "worktree": str(state["path"]),
                                  "branch": state["branch"], "head_sha": candidate_sha})
                continue
            journal.append("worktree_sync_intent", integration_id=integration_id,
                           participant=participant, worktree=str(state["path"]),
                           branch=state["branch"], expected_source_sha=source_sha,
                           candidate_sha=candidate_sha)
            if state["ref_sha"] == source_sha:
                ref_update = self._git(["update-ref", state["ref"], candidate_sha, source_sha], repo)
                if ref_update.exit_code:
                    raise RuntimeError(f"participant branch CAS failed for {participant}: {ref_update.stderr}")
            elif state["ref_sha"] != candidate_sha:
                raise RuntimeError(f"participant branch moved during sync: {participant}")
            # A prior worker may have updated the ref and crashed before resetting this clean checkout.
            if state["pending_reset"] or state["head_sha"] == source_sha:
                reset = self._git(["reset", "--hard", candidate_sha], state["path"])
                if reset.exit_code:
                    raise RuntimeError(f"could not reset participant worktree {participant}: {reset.stderr}")
            final = self._participant_state(repo, issue_id, participant, source_sha, candidate_sha)
            if final["head_sha"] != candidate_sha or final["ref_sha"] != candidate_sha:
                raise RuntimeError(f"participant worktree did not reach integration candidate: {participant}")
            journal.append("worktree_sync_done", integration_id=integration_id,
                           participant=participant, worktree=str(final["path"]),
                           branch=final["branch"], candidate_sha=candidate_sha)
            records = journal.read()
            completed.append({"participant": participant, "worktree": str(final["path"]),
                              "branch": final["branch"], "head_sha": candidate_sha})
        return completed
