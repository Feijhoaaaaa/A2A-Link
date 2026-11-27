"""Manual probe: does a Link-managed CLI keep its session ID across resume?

Run from the repository root (never collected by pytest; it starts real CLIs):

    python -m tests.manual.session_resume_probe.probe --provider codex --run
    python -m tests.manual.session_resume_probe.probe --provider claude --run

The probe builds argv with the same adapters Link uses (`mcp_server.issue_adapters`),
runs each turn in a fresh throwaway git repo under the system temp directory, and
prints an anonymized JSON report. Session IDs, prompts, replies, stderr and paths
stay in memory and are never printed or written; only booleans, return codes,
ID shape and CLI versions are reported.
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from mcp_server.cli_executor import resolve_executable
from mcp_server.issue_adapters import ClaudeAdapter, CodexAdapter, SessionHandle

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?")
ADAPTERS = {"codex": CodexAdapter(), "claude": ClaudeAdapter()}


def parse_jsonl(text: str) -> list[dict[str, Any]]:
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def start_event_ids(provider: str, events: Iterable[dict[str, Any]]) -> list[str]:
    """Every session ID announced by the provider's start event, in order."""
    extract = ADAPTERS[provider].session_id_from_event
    return [sid for sid in (extract(e) for e in events) if sid]


def result_session_ids(provider: str, events: Iterable[dict[str, Any]]) -> list[str]:
    """Claude also repeats the session ID on later events; Codex only on thread.started."""
    if provider != "claude":
        return []
    return [e["session_id"] for e in events
            if e.get("type") == "result" and isinstance(e.get("session_id"), str)]


def reply_text(provider: str, events: Iterable[dict[str, Any]]) -> str:
    parts = []
    for e in events:
        if provider == "codex" and e.get("type") == "item.completed":
            item = e.get("item") or {}
            if item.get("type") in ("agent_message", "assistant_message"):
                parts.append(str(item.get("text", "")))
        elif provider == "claude" and e.get("type") == "result":
            parts.append(str(e.get("result", "")))
    return "\n".join(parts)


def turn_completed(provider: str, events: Iterable[dict[str, Any]]) -> bool:
    if provider == "codex":
        return any(e.get("type") == "turn.completed" for e in events)
    return any(e.get("type") == "result" and not e.get("is_error") for e in events)


def id_shape(session_id: str | None) -> str:
    if not session_id:
        return "missing"
    return "uuid" if UUID_RE.match(session_id) else "opaque"


@dataclass
class Turn:
    name: str
    returncode: int | None
    start_ids: list[str] = field(default_factory=list)
    result_ids: list[str] = field(default_factory=list)
    completed: bool = False
    reply: str = ""
    timed_out: bool = False


@dataclass
class ProbeResult:
    provider: str
    cli_version: str | None
    turns: list[Turn]
    nonce: str
    delay_seconds: float
    unknown_id_turn: Turn | None = None

    def report(self) -> dict[str, Any]:
        """Anonymized: no IDs, prompts, replies, paths or stderr."""
        start = self.turns[0] if self.turns else None
        sid = start.start_ids[0] if start and start.start_ids else None
        checks: dict[str, bool] = {
            "session_registration_from_start_event": bool(start and len(start.start_ids) == 1),
            "assistant_turn_end_observed": bool(start and start.completed and start.returncode == 0),
        }
        for turn in self.turns[1:]:
            ids = set(turn.start_ids) | set(turn.result_ids)
            checks[f"{turn.name}_same_id"] = bool(
                sid and turn.returncode == 0 and turn.completed and ids == {sid})
        if len(self.turns) > 1:
            checks["completion_turn_has_prior_context"] = self.nonce.lower() in self.turns[1].reply.lower()
        if len(self.turns) > 2:
            checks["newer_message_turn_has_prior_context"] = self.nonce.lower() in self.turns[2].reply.lower()
        report: dict[str, Any] = {
            "provider": self.provider,
            "cli_version": self.cli_version,
            "os": f"{platform.system()} {platform.release()}",
            "start_id_shape": id_shape(sid),
            "completion_delay_seconds": self.delay_seconds,
            "turns": [{"name": t.name, "returncode": t.returncode, "completed": t.completed,
                       "timed_out": t.timed_out, "start_event_count": len(t.start_ids)}
                      for t in self.turns],
            "checks": checks,
        }
        if self.unknown_id_turn is not None:
            u = self.unknown_id_turn
            report["unknown_id"] = {
                "returncode": u.returncode,
                "rejected": u.returncode != 0 and not u.completed,
                "substituted_new_session": bool(u.start_ids or u.result_ids),
            }
        report["passed"] = all(checks.values())
        return report


def cli_version(provider: str) -> str | None:
    exe = resolve_executable(provider)
    if not exe:
        return None
    out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60)
    match = VERSION_RE.search(out.stdout or "")
    return match.group(0) if match else None


def run_turn(name: str, provider: str, argv: list[str], cwd: str, timeout: float) -> Turn:
    exe = resolve_executable(argv[0])
    if not exe:
        return Turn(name=name, returncode=None)
    try:
        proc = subprocess.run([exe, *argv[1:]], cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return Turn(name=name, returncode=None, timed_out=True)
    events = parse_jsonl(proc.stdout or "")
    return Turn(name=name, returncode=proc.returncode,
                start_ids=start_event_ids(provider, events),
                result_ids=result_session_ids(provider, events),
                completed=turn_completed(provider, events),
                reply=reply_text(provider, events))


def run_probe(provider: str, delay: float, timeout: float, unknown_id_check: bool) -> ProbeResult:
    adapter = ADAPTERS[provider]
    nonce = "probe" + secrets.token_hex(4)
    with tempfile.TemporaryDirectory(prefix="a2a-resume-probe-") as cwd:
        subprocess.run(["git", "init", "-q"], cwd=cwd, check=True)
        turns = [run_turn("start", provider, adapter.start_argv(
            f"Remember the code word {nonce}. Do not run tools or edit files. Reply only: OK"),
            cwd, timeout)]
        sid = turns[0].start_ids[0] if turns[0].start_ids else None
        if sid and turns[0].returncode == 0:
            time.sleep(delay)  # the first process has exited; simulate a long test job
            handle = SessionHandle(adapter_type=provider, session_id=sid, cwd=cwd, generation=1)
            ask = "Do not run tools or edit files. Reply with only the code word I asked you to remember."
            turns.append(run_turn("completion_resume", provider,
                                  adapter.continue_argv(handle, "Test job completed. " + ask), cwd, timeout))
            turns.append(run_turn("newer_message_resume", provider,
                                  adapter.continue_argv(handle, "New user message. " + ask), cwd, timeout))
        unknown = None
        if unknown_id_check:
            bogus = SessionHandle(adapter_type=provider, session_id=str(uuid.uuid4()),
                                  cwd=cwd, generation=1)
            unknown = run_turn("unknown_id_resume", provider,
                               adapter.continue_argv(bogus, "Reply only: OK"), cwd, timeout)
    return ProbeResult(provider=provider, cli_version=cli_version(provider), turns=turns,
                       nonce=nonce, delay_seconds=delay, unknown_id_turn=unknown)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--provider", choices=sorted(ADAPTERS), required=True)
    parser.add_argument("--run", action="store_true", help="actually start the real CLI")
    parser.add_argument("--delay", type=float, default=30.0, help="seconds between turn end and resume")
    parser.add_argument("--timeout", type=float, default=600.0, help="per-turn timeout in seconds")
    parser.add_argument("--unknown-id-check", action="store_true",
                        help="also resume a random unknown ID and report whether the CLI rejects it")
    args = parser.parse_args(argv)
    if not args.run:
        parser.error("refusing to start a real CLI without --run")
    report = run_probe(args.provider, args.delay, args.timeout, args.unknown_id_check).report()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
