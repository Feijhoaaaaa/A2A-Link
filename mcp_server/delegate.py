"""Agent-agnostic delegation: log a task in the balancer, run the agent CLI, report back.

Delegation runs in a background thread so a long task never hits the MCP client's
tool timeout: the tool waits up to `wait_s`, then hands back a task id that the
caller polls with `get_delegation_result`. While it runs, the agent's output is
written live to a per-task log (see balancer/tasklog.py), with a heartbeat line
whenever the agent is quiet.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from balancer.tasklog import TaskLog, fmt_elapsed, read_tail
from mcp_server.balancer_startup import ensure_balancer
from mcp_server.cli_executor import run_cli_task
from mcp_server.progress import (claude_result, claude_usage, codex_last_message, format_claude_event,
                                 format_codex_event, parse_event, repair_mojibake)
from mcp_server.worktree import Workspace, finalize, prepare

DEPTH_ENV = "A2A_LINK_DELEGATION_DEPTH"
MAX_WAIT_S = 55  # stay below the common 60 s MCP client tool timeout

PREAMBLE = (
    "[A2A_Link] This task was delegated to you by another AI coding agent; no human is "
    "watching this session. Project directory: {cwd}\n"
    "Work autonomously and do not ask questions. You cannot delegate further. "
    "Finish with a short summary: files changed, commands run and their results, "
    "and anything left undone.\n\nTask:\n{prompt}"
)


def _split(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


# Delegated Claude may run tests and read git state; everything else needing approval is denied.
# On Windows Claude runs commands with its PowerShell tool, so each rule exists for both tools.
# Set A2A_LINK_CLAUDE_ALLOWED_TOOLS="" to allow file edits only.
_ALLOWED_COMMANDS = [
    "python -m pytest", "pytest", "python -m unittest",
    ".venv/Scripts/python -m pytest", ".venv/Scripts/python.exe -m pytest", ".venv/bin/python -m pytest",
    "git status", "git diff", "git log",
]
DEFAULT_CLAUDE_ALLOWED_TOOLS = ",".join(f"{tool}({cmd}:*)" for tool in ("Bash", "PowerShell")
                                        for cmd in _ALLOWED_COMMANDS)


def claude_command(prompt: str, last_message_file: str | None = None) -> list[str]:
    cmd = ["claude", "-p", prompt,
           # One JSON event per line, so progress can be shown while it works.
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", os.environ.get("A2A_LINK_CLAUDE_PERMISSION_MODE", "acceptEdits"),
           # No MCP servers in the child, so it cannot call delegate_to_* again.
           "--strict-mcp-config"]
    allowed = _split(os.environ.get("A2A_LINK_CLAUDE_ALLOWED_TOOLS", DEFAULT_CLAUDE_ALLOWED_TOOLS))
    if allowed:
        cmd += ["--allowedTools", *allowed]
    return cmd


def codex_command(prompt: str, last_message_file: str | None = None) -> list[str]:
    cmd = ["codex", "exec", "--skip-git-repo-check",
           # JSONL events: clean live progress instead of echoed prompts and full command output.
           "--json",
           "-s", os.environ.get("A2A_LINK_CODEX_SANDBOX", "workspace-write"),
           # Disable this server in the child, so it cannot call delegate_to_* again.
           "-c", "mcp_servers.a2a-link.enabled=false"]
    if last_message_file:
        # stdout can be empty after a long run; the final agent message is reliable.
        cmd += ["-o", last_message_file]
    return cmd + [prompt]


AGENT_COMMANDS = {"claude": claude_command, "codex": codex_command}


def default_timeout_s() -> int:
    return int(os.environ.get("A2A_LINK_DELEGATE_TIMEOUT_S", "600"))


def heartbeat_s() -> float:
    return float(os.environ.get("A2A_LINK_HEARTBEAT_S", "15"))


def current_depth() -> int:
    try:
        return int(os.environ.get(DEPTH_ENV, "0"))
    except ValueError:
        return 0


def max_depth() -> int:
    return int(os.environ.get("A2A_LINK_MAX_DELEGATION_DEPTH", "1"))


WORKTREE_NOTE = ("You are in an isolated git worktree on branch {branch}. Your changes are committed "
                 "automatically when you finish; do not commit, push or switch branches yourself.\n")


def wrap_prompt(prompt: str, cwd: str, branch: str | None = None) -> str:
    if os.environ.get("A2A_LINK_DELEGATE_PREAMBLE", "1") == "0":
        return prompt
    note = WORKTREE_NOTE.format(branch=branch) if branch else ""
    return PREAMBLE.format(cwd=cwd, prompt=note + prompt)


@dataclass
class Job:
    task_id: str
    agent: str
    description: str = ""
    done: threading.Event = field(default_factory=threading.Event)
    status: str = "in_progress"
    result: str | None = None
    error: str | None = None
    workspace: Workspace | None = None
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def elapsed(self) -> float:
        return (self.finished or time.monotonic()) - self.started

    def view(self) -> dict:
        out = {"task_id": self.task_id, "agent": self.agent, "status": self.status,
               "elapsed_s": round(self.elapsed(), 1), "elapsed": fmt_elapsed(self.elapsed())}
        if self.workspace is not None:
            out["workspace"] = self.workspace.view()
        if self.status == "done":
            out["result"] = self.result
        elif self.status == "failed":
            out["error"] = self.error
        else:
            out["hint"] = ("Still running. Call get_delegation_result with this task_id; "
                           "get_delegation_log shows live progress.")
        return out


class DelegationManager:
    def __init__(self, balancer_url: str):
        self.balancer_url = balancer_url
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.balancer_url, timeout=10)

    def start(self, agent: str, prompt: str, cwd: str | None = None,
              timeout_s: int | None = None, isolation: str = "auto") -> dict:
        if agent not in AGENT_COMMANDS:
            raise ValueError(f"unknown agent {agent!r}; known: {sorted(AGENT_COMMANDS)}")
        if current_depth() >= max_depth():
            return {"status": "refused",
                    "error": f"delegation depth limit reached ({current_depth()}/{max_depth()}); "
                             "a delegated agent may not delegate again"}
        if not prompt.strip():
            return {"status": "refused", "error": "empty prompt"}
        cwd = cwd or os.getcwd()
        if not os.path.isdir(cwd):
            return {"status": "refused", "error": f"cwd is not an existing directory: {cwd}"}
        if isolation not in ("auto", "worktree", "none"):
            return {"status": "refused", "error": "isolation must be auto, worktree or none"}
        try:
            with self._client() as c:
                ensure_balancer(c, self.balancer_url)
                task_id = c.post("/tasks", json={"description": prompt[:500], "model_hint": agent}
                                 ).raise_for_status().json()["id"]
                if not c.post(f"/tasks/{task_id}/claim", json={"agent": agent}
                              ).raise_for_status().json().get("claimed"):
                    return {"status": "refused", "error": f"task {task_id} could not be claimed"}
        except (httpx.HTTPError, sqlite3.Error, OSError, RuntimeError, ValueError, KeyError, TypeError, AttributeError) as exc:
            return {"status": "needs_human", "agent": agent,
                    "error": f"A2A Link at {self.balancer_url}: {exc}",
                    "hint": "Ask the user to check the balancer address and port, start the "
                            "balancer from the A2A_Link project, then retry delegation."}

        log = TaskLog(task_id)
        log.write(f"delegated to {agent} (task {task_id})")
        log.write(f"task: {' '.join(prompt.split())[:300]}")
        try:
            workspace = prepare(cwd, task_id, isolation)
        except (RuntimeError, OSError) as e:
            log.write(f"failed before start: {e}")
            self._report(task_id, "failed", str(e))
            return {"task_id": task_id, "agent": agent, "status": "failed", "error": str(e)}
        cwd = workspace.cwd
        log.write(f"working directory: {cwd}" + (f" (worktree, branch {workspace.branch})"
                                                  if workspace.mode == "worktree" else ""))
        for warning in workspace.warnings:
            log.write(f"warning: {warning}")

        job = Job(task_id, agent, description=prompt[:500], workspace=workspace)
        with self._lock:
            self.jobs[task_id] = job
        env = {**os.environ, DEPTH_ENV: str(current_depth() + 1)}
        fd, last_msg = tempfile.mkstemp(prefix=f"a2a_{task_id}_", suffix=".txt")
        os.close(fd)
        command = AGENT_COMMANDS[agent](wrap_prompt(prompt, cwd, workspace.branch), last_msg)
        threading.Thread(target=self._run,
                         args=(job, log, command, cwd, timeout_s or default_timeout_s(), env, last_msg),
                         daemon=True, name=f"delegate-{task_id}").start()
        return job.view()

    def _line_handler(self, job: Job, log: TaskLog):
        formatter = {"claude": format_claude_event, "codex": format_codex_event}[job.agent]

        def on_line(stream: str, line: str) -> None:
            if not line.strip():
                return
            event = parse_event(line) if stream == "stdout" else None
            if event is None:
                log.write(repair_mojibake(line))
                return
            usage = claude_usage(event)
            if usage:
                self._report_usage(job.agent, *usage)
            for text in formatter(event):
                log.write(text)
        return on_line

    def _run(self, job: Job, log: TaskLog, command: list[str], cwd: str, timeout_s: int, env: dict,
             last_msg: str | None = None) -> None:
        def beat(elapsed: float) -> None:
            log.write(f"... still working (no new output for {heartbeat_s():.0f}s, "
                      f"elapsed {fmt_elapsed(elapsed)})")

        try:
            log.write(f"started {job.agent}, timeout {fmt_elapsed(timeout_s)}")
            code, out, err = run_cli_task(command, cwd=cwd, timeout_s=timeout_s, env=env,
                                          on_line=self._line_handler(job, log),
                                          heartbeat_s=heartbeat_s(), on_heartbeat=beat)
            if job.agent == "claude":
                out = claude_result(out) or out
            elif job.agent == "codex":
                out = codex_last_message(out) or out
            if last_msg:
                try:
                    out = Path(last_msg).read_text(encoding="utf-8", errors="replace").strip() or out
                except OSError:
                    pass
            if code == 0:
                job.status, job.result = "done", out
            else:
                job.status = "failed"
                job.error = (err or out or "").strip()[-2000:] or f"exit code {code}"
        except Exception as e:  # never leave a job hanging
            job.status, job.error = "failed", f"{type(e).__name__}: {e}"
        finally:
            if last_msg:
                Path(last_msg).unlink(missing_ok=True)
        try:
            if job.workspace is not None:
                finalize(job.workspace, job.result or job.error or "")
                if job.workspace.mode == "worktree":
                    log.write(f"changes on {job.workspace.branch}: "
                              f"{(job.workspace.diffstat or 'no changes').splitlines()[-1]}")
        except Exception as e:
            job.workspace.warnings.append(f"could not finalize worktree: {e}")
        job.finished = time.monotonic()
        log.write(f"finished: {job.status} after {fmt_elapsed(job.elapsed())}"
                  + (f" ({job.error.splitlines()[-1][:200]})" if job.status == "failed" and job.error else ""))
        try:
            text = job.result if job.status == "done" else job.error
            if job.workspace is not None and job.workspace.mode == "worktree":
                text = f"{text}\n\n[workspace] {job.workspace.view()}"
            self._report(job.task_id, job.status, text)
        finally:
            job.done.set()

    def _report(self, task_id: str, status: str, text: str | None) -> None:
        try:
            with self._client() as c:
                if status == "done":
                    c.post(f"/tasks/{task_id}/complete", json={"result": (text or "")[-20000:]})
                else:
                    c.post(f"/tasks/{task_id}/fail", json={"error": (text or "")[-4000:]})
        except httpx.HTTPError:
            pass  # the in-memory job still carries the outcome

    def _report_usage(self, agent: str, pct: float, resets_at: float) -> None:
        started = (datetime.fromtimestamp(resets_at - 5 * 3600, timezone.utc).isoformat()
                   if resets_at else datetime.now(timezone.utc).isoformat())
        try:
            with self._client() as c:
                c.post("/usage", json={"agent": agent, "pct_used": min(100.0, max(0.0, pct)),
                                       "window_started_at": started})
        except httpx.HTTPError:
            pass

    def wait(self, task_id: str, wait_s: float) -> dict:
        wait_s = max(0.0, min(float(wait_s), MAX_WAIT_S))
        job = self.jobs.get(task_id)
        if job is not None:
            job.done.wait(wait_s)
            return job.view()
        # Not started by this server process (e.g. after a restart): ask the balancer.
        try:
            with self._client() as c:
                r = c.get(f"/tasks/{task_id}")
                if r.status_code == 404:
                    return {"task_id": task_id, "status": "unknown", "error": "no such task"}
                t = r.raise_for_status().json()
        except httpx.HTTPError as e:
            return {"task_id": task_id, "status": "unknown", "error": f"balancer unreachable: {e}"}
        status = {"done": "done", "in_progress": "in_progress"}.get(t["status"], "failed")
        out = {"task_id": task_id, "agent": t["agent"], "status": status}
        if status == "done":
            out["result"] = t.get("result")
        elif status == "failed":
            out["error"] = t.get("error") or t["status"]
        else:
            out["hint"] = ("Running in another server process; get_delegation_log shows its "
                           "progress. If that process exited, the task will not finish.")
        return out

    def log(self, task_id: str, lines: int = 80) -> dict:
        try:
            text = read_tail(task_id, lines)
        except ValueError as e:
            return {"task_id": task_id, "error": str(e)}
        job = self.jobs.get(task_id)
        out = {"task_id": task_id, "log": text if text is not None else "(no log for this task)"}
        if job is not None:
            out.update(status=job.status, elapsed=fmt_elapsed(job.elapsed()))
        return out

    def recent(self, limit: int = 20) -> dict:
        """Delegations from every agent (via the balancer) plus untracked ones from this process."""
        limit = max(1, min(int(limit), 200))
        rows: list[dict] = []
        note = None
        try:
            with self._client() as c:
                for t in c.get("/tasks").raise_for_status().json():
                    rows.append({"task_id": t["id"], "agent": t["agent"], "status": t["status"],
                                 "task": (t["description"] or "")[:160], "created_at": t["created_at"],
                                 "updated_at": t["updated_at"]})
        except httpx.HTTPError:
            note = "balancer unreachable: only delegations started by this session are listed"
        known = {r["task_id"] for r in rows}
        for job in self.jobs.values():
            if job.task_id not in known:
                rows.append({"task_id": job.task_id, "agent": job.agent, "status": job.status,
                             "task": job.description[:160], "created_at": job.created_at,
                             "untracked": True})
        for row in rows:
            job = self.jobs.get(row["task_id"])
            if job is not None:
                row["elapsed"] = fmt_elapsed(job.elapsed())
        rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        out: dict = {"delegations": rows[:limit]}
        if note:
            out["note"] = note
        return out


def delegate(agent: str, prompt: str, balancer_url: str, cwd: str | None = None,
             timeout_s: int | None = None, isolation: str = "none") -> str:
    """Blocking helper: delegate and wait for the final text (used by scripts and tests)."""
    mgr = DelegationManager(balancer_url)
    started = mgr.start(agent, prompt, cwd, timeout_s, isolation)
    if "task_id" not in started:  # refused or balancer unreachable; a fast job may already be done
        return f"ERROR: {started.get('error')} {started.get('hint', '')}".rstrip()
    job = mgr.jobs[started["task_id"]]
    job.done.wait()
    return job.result if job.status == "done" else f"ERROR: {job.error}"
