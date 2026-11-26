"""Policy-gated terminal executor: evaluate -> (confirm) -> run -> audit."""
from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

import anyio

from mcp_server.process import run_process
from mcp_server.terminal.policy import Level, Policy, build_env, evaluate

log = logging.getLogger("a2a_link.terminal")

# Asked with (argv, cwd, reason); returns True only if a human approved.
Confirmer = Callable[[list[str], str, str], Awaitable[bool]]


@dataclass
class CommandOutcome:
    request_id: str
    status: str            # ok | failed | timeout | denied | declined | error
    level: str
    reason: str
    argv: list[str]
    cwd: str
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_s: float = 0.0
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class AuditLog:
    """Append-only JSONL. Records metadata and output sizes, not output (may hold secrets)."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()

    def write(self, event: str, outcome: CommandOutcome, **extra) -> None:
        rec = {"ts": datetime.now(timezone.utc).isoformat(), "event": event,
               "request_id": outcome.request_id, "status": outcome.status,
               "level": outcome.level, "reason": outcome.reason, "argv": outcome.argv,
               "cwd": outcome.cwd, "returncode": outcome.returncode,
               "timed_out": outcome.timed_out, "duration_s": outcome.duration_s,
               "stdout_bytes": len(outcome.stdout.encode()),
               "stderr_bytes": len(outcome.stderr.encode()), **extra}
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        log.info("%s %s %s", event, outcome.status, outcome.argv)


class TerminalExecutor:
    def __init__(self, policy: Policy):
        self.policy = policy
        self.audit = AuditLog(policy.audit_log)

    def clamp_timeout(self, timeout_s: float | None) -> float:
        t = self.policy.default_timeout_s if timeout_s is None else timeout_s
        return max(1, min(float(t), float(self.policy.max_timeout_s)))

    async def run(self, argv: list[str], cwd: str, timeout_s: float | None,
                  confirm: Confirmer | None) -> CommandOutcome:
        rid = uuid.uuid4().hex[:12]
        decision = evaluate(self.policy, argv, cwd)
        outcome = CommandOutcome(rid, "denied", decision.level.value, decision.reason,
                                 list(argv) if isinstance(argv, list) else [], str(cwd))
        if decision.level is Level.DENY:
            self.audit.write("denied", outcome)
            return outcome

        if decision.level is Level.CONFIRM:
            approved = False
            if confirm is not None:
                try:
                    approved = await confirm(argv, decision.cwd, decision.reason)
                except Exception as e:  # client without elicitation support, transport error...
                    outcome.reason = f"{decision.reason}; confirmation unavailable: {e}"
            else:
                outcome.reason = f"{decision.reason}; no confirmation channel"
            if not approved:
                outcome.status = "declined"
                self.audit.write("declined", outcome)
                return outcome
            outcome.status = "approved"
            self.audit.write("confirmed", outcome)

        timeout = self.clamp_timeout(timeout_s)
        outcome.cwd = decision.cwd
        try:
            r = await anyio.to_thread.run_sync(
                lambda: run_process([decision.executable, *argv[1:]], cwd=decision.cwd,
                                    timeout_s=timeout, env=build_env(self.policy),
                                    max_output_bytes=self.policy.max_output_bytes))
        except OSError as e:
            outcome.status, outcome.stderr = "error", f"failed to start: {e}"
            self.audit.write("error", outcome)
            return outcome

        outcome.returncode, outcome.stdout, outcome.stderr = r.returncode, r.stdout, r.stderr
        outcome.timed_out, outcome.duration_s = r.timed_out, r.duration_s
        outcome.stdout_truncated, outcome.stderr_truncated = r.stdout_truncated, r.stderr_truncated
        outcome.status = "timeout" if r.timed_out else ("ok" if r.returncode == 0 else "failed")
        self.audit.write("executed", outcome, timeout_s=timeout)
        return outcome

    def describe(self) -> dict:
        p = self.policy
        return {
            "allowed_roots": [str(r) for r in p.allowed_roots],
            "default_timeout_s": p.default_timeout_s,
            "max_timeout_s": p.max_timeout_s,
            "commands": {
                name: {"default": c.default.value, "path": c.path,
                       "rules": [{"prefix": list(r.prefix), "level": r.level.value} for r in c.rules],
                       "deny_args": list(c.deny_args), "confirm_args": list(c.confirm_args)}
                for name, c in p.commands.items()
            },
        }
