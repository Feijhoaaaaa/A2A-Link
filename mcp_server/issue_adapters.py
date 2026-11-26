"""CLI session adapters used by persistent issue participants."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SessionHandle:
    adapter_type: str
    session_id: str
    cwd: str
    generation: int


class CodexAdapter:
    adapter_type = "codex"

    def start_argv(self, prompt: str, profile: dict[str, Any] | None = None) -> list[str]:
        return [
            "codex", "exec", "--json", "-s", "workspace-write",
            "-c", "mcp_servers.a2a-link.enabled=false",
            *self._profile_args(profile), prompt,
        ]

    def continue_argv(self, handle: SessionHandle, prompt: str,
                      profile: dict[str, Any] | None = None) -> list[str]:
        if handle.adapter_type != self.adapter_type:
            raise ValueError(f"cannot resume {handle.adapter_type!r} with codex adapter")
        if not handle.session_id.strip():
            raise ValueError("Codex session id is required to resume")
        return [
            "codex", "exec", "resume", handle.session_id, "--json",
            "-c", 'sandbox_mode="workspace-write"',
            "-c", "mcp_servers.a2a-link.enabled=false",
            *self._profile_args(profile), prompt,
        ]

    @staticmethod
    def _profile_args(profile: dict[str, Any] | None) -> list[str]:
        if not profile:
            return []
        args = []
        model = profile.get("model")
        effort = profile.get("effort")
        if model:
            args.extend(("--model", str(model)))
        if effort:
            args.extend(("-c", f'model_reasoning_effort="{effort}"'))
        return args

    @staticmethod
    def session_id_from_event(event: dict[str, Any]) -> str | None:
        if event.get("type") != "thread.started":
            return None
        session_id = event.get("thread_id")
        return session_id.strip() if isinstance(session_id, str) and session_id.strip() else None


class ClaudeAdapter:
    adapter_type = "claude"

    def start_argv(self, prompt: str, profile: dict[str, Any] | None = None) -> list[str]:
        return ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
                "--permission-mode", "acceptEdits", "--strict-mcp-config",
                *self._profile_args(profile)]

    def continue_argv(self, handle: SessionHandle, prompt: str,
                      profile: dict[str, Any] | None = None) -> list[str]:
        if handle.adapter_type != self.adapter_type:
            raise ValueError(f"cannot resume {handle.adapter_type!r} with Claude adapter")
        if not handle.session_id.strip():
            raise ValueError("Claude session id is required to resume")
        return ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
                "--permission-mode", "acceptEdits", "--strict-mcp-config",
                "--resume", handle.session_id, *self._profile_args(profile)]

    @staticmethod
    def _profile_args(profile: dict[str, Any] | None) -> list[str]:
        if not profile:
            return []
        args = []
        model = profile.get("model")
        effort = profile.get("effort")
        if model:
            args.extend(("--model", str(model)))
        if effort:
            args.extend(("--effort", str(effort)))
        return args

    @staticmethod
    def session_id_from_event(event: dict[str, Any]) -> str | None:
        if event.get("type") != "system" or event.get("subtype") != "init":
            return None
        session_id = event.get("session_id")
        return session_id.strip() if isinstance(session_id, str) and session_id.strip() else None


def parse_codex_events(events: list[dict[str, Any]],
                       transcript: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Extract observed Codex state; never echo a requested profile as confirmed."""
    session_id = None
    error = None
    usage = None
    for event in events:
        session_id = CodexAdapter.session_id_from_event(event) or session_id
        kind = event.get("type")
        if kind in ("turn.failed", "error"):
            detail = event.get("error") or event.get("message")
            if isinstance(detail, dict):
                detail = detail.get("message")
            if detail:
                error = str(detail)
        if kind == "turn.completed" and isinstance(event.get("usage"), dict):
            usage = dict(event["usage"])

    model = effort = None
    context_window = None
    for row in transcript or []:
        if row.get("type") == "turn_context":
            data = row.get("payload") or {}
            if isinstance(data.get("model"), str):
                model = data["model"]
            if isinstance(data.get("effort"), str):
                effort = data["effort"]
        if row.get("type") == "event_msg":
            event = row.get("payload") or {}
            if event.get("type") == "thread_settings_applied":
                settings = event.get("thread_settings") or {}
                if isinstance(settings.get("model"), str):
                    model = settings["model"]
                if isinstance(settings.get("reasoning_effort"), str):
                    effort = settings["reasoning_effort"]
            if event.get("type") == "token_count":
                info = event.get("info") or {}
                if isinstance(info.get("model_context_window"), int):
                    context_window = info["model_context_window"]

    return {
        "session_id": session_id,
        "confirmed_profile": {"model": model, "effort": effort},
        "context": {"window": context_window, "usage": usage},
        "usage": usage,
        "error": error,
    }


def parse_claude_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract confirmed Claude model and context usage from result events.

    Rate-limit utilization is deliberately ignored: it describes a quota window,
    not conversation context consumption.
    """
    session_id = None
    model = None
    usage_row: dict[str, Any] | None = None
    for event in events:
        session_id = ClaudeAdapter.session_id_from_event(event) or session_id
        if event.get("type") == "system" and event.get("subtype") == "init":
            if isinstance(event.get("model"), str):
                model = event["model"]
        if event.get("type") != "result":
            continue
        rows = event.get("model_usage_since_last_result")
        if not isinstance(rows, dict):
            continue
        if all(name in rows for name in ("contextWindow", "inputTokens", "cacheReadInputTokens",
                                         "cacheCreationInputTokens", "outputTokens")):
            usage_row = rows
        elif model and isinstance(rows.get(model), dict):
            usage_row = rows[model]
        elif len(rows) == 1 and isinstance(next(iter(rows.values())), dict):
            usage_row = next(iter(rows.values()))

    def observed_int(name: str) -> int | None:
        value = (usage_row or {}).get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    window = observed_int("contextWindow")
    components = {
        "input_tokens": observed_int("inputTokens"),
        "cache_read_input_tokens": observed_int("cacheReadInputTokens"),
        "cache_creation_input_tokens": observed_int("cacheCreationInputTokens"),
        "output_tokens": observed_int("outputTokens"),
    }
    used_tokens = (sum(components.values())
                   if all(value is not None for value in components.values()) else None)
    threshold = 0.9
    threshold_reached = (used_tokens / window >= threshold
                         if used_tokens is not None and window is not None and window > 0 else None)
    return {
        "session_id": session_id,
        "confirmed_profile": {"model": model, "effort": None},
        "context": {"window": window, **components, "used_tokens": used_tokens,
                    "threshold": threshold, "threshold_reached": threshold_reached},
    }
