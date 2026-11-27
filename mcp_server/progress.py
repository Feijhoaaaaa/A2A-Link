"""Turn agent CLI output into readable live-progress lines.

Codex prints human-readable progress on stderr, so its lines are logged as they are.
Claude is run with `--output-format stream-json --verbose`: one JSON event per line,
summarised here as "-> tool: input", "<- result", "say: text".
"""
from __future__ import annotations

import json
import re

_MAX = 300


def _short(text, limit: int = _MAX) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + " …"


def _tool_input(name: str, data: dict) -> str:
    for key in ("command", "file_path", "path", "pattern", "url", "description", "prompt"):
        if key in data:
            return _short(data[key], 200)
    return _short(json.dumps(data, ensure_ascii=False), 200)


def parse_event(line: str) -> dict | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def format_claude_event(event: dict) -> list[str]:
    """Readable lines for one stream-json event; empty for noise (hooks, rate limits...)."""
    etype = event.get("type")
    if etype == "system":
        sub = event.get("subtype")
        if sub == "init":
            return [f"claude session started (model {event.get('model', '?')})"]
        if sub == "task_summary" and event.get("detail"):
            return [f"... {_short(event['detail'])}"]
        return []
    if etype == "assistant":
        out = []
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "tool_use":
                out.append(f"-> {block.get('name')}: {_tool_input(block.get('name', ''), block.get('input') or {})}")
            elif block.get("type") == "text" and block.get("text", "").strip():
                out.append(f"say: {_short(block['text'])}")
        return out
    if etype == "user":
        out = []
        for block in (event.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                mark = "<- ERROR" if block.get("is_error") else "<-"
                out.append(f"{mark} {_short(content or '(no output)', 200)}")
        return out
    if is_result_event(event):
        secs = (event.get("duration_ms") or 0) / 1000
        return [f"claude finished ({event.get('subtype') or event.get('stop_reason') or 'done'}, {secs:.0f}s)"]
    return []


_PWSH_WRAPPER = re.compile(r"""^\S*(?:pwsh|powershell)(?:\.exe)?"?\s+(?:-\w+\s+)*-Command\s+(['"])(.*)\1\s*$""",
                           re.IGNORECASE | re.DOTALL)


def _strip_shell_wrapper(command: str) -> str:
    """Codex on Windows reports `"...pwsh.exe" -Command 'git --version'`; show just the inner command."""
    m = _PWSH_WRAPPER.match(command.strip())
    return m.group(2) if m else command


def format_codex_event(event: dict) -> list[str]:
    """Readable lines for one `codex exec --json` event."""
    etype = event.get("type", "")
    item = event.get("item") or {}
    itype = item.get("type")
    if etype == "thread.started":
        return ["codex session started"]
    if etype in ("turn.failed", "error"):
        err = event.get("error") or {}
        return [f"ERROR: {_short(err.get('message') if isinstance(err, dict) else err or event.get('message'))}"]
    if etype == "turn.completed":
        u = event.get("usage") or {}
        return [f"codex turn finished (tokens in {u.get('input_tokens', '?')}, out {u.get('output_tokens', '?')})"]
    if not etype.startswith("item."):
        return []
    if itype == "agent_message" and etype == "item.completed":
        return [f"say: {_short(item.get('text', ''))}"] if item.get("text", "").strip() else []
    if itype == "reasoning" and etype == "item.completed" and item.get("text", "").strip():
        return [f"thinking: {_short(item['text'], 200)}"]
    if itype == "command_execution":
        cmd = _short(_strip_shell_wrapper(item.get("command", "")), 200)
        if etype == "item.started":
            return [f"-> shell: {cmd}"]
        if etype == "item.completed":
            out = (item.get("aggregated_output") or "").strip().splitlines()
            first = _short(out[0], 160) if out else "(no output)"
            more = f" (+{len(out) - 1} lines)" if len(out) > 1 else ""
            mark = "<-" if item.get("exit_code") == 0 else f"<- exit {item.get('exit_code')}:"
            return [f"{mark} {first}{more}"]
        return []
    if itype == "file_change" and etype == "item.completed":
        paths = [c.get("path", "?") for c in item.get("changes") or [] if isinstance(c, dict)]
        return [f"-> edit: {', '.join(paths) or 'files'}"]
    if itype == "mcp_tool_call" and etype == "item.started":
        return [f"-> {item.get('server', 'mcp')}/{item.get('tool', '?')}"]
    if itype == "web_search" and etype == "item.started":
        return [f"-> web search: {_short(item.get('query', ''), 160)}"]
    if itype == "todo_list" and etype in ("item.started", "item.updated"):
        todo = [t for t in item.get("items") or [] if isinstance(t, dict)]
        done = sum(1 for t in todo if t.get("completed"))
        return [f"plan: {done}/{len(todo)} done"] if todo else []
    if itype == "error":
        return [f"ERROR: {_short(item.get('message', ''))}"]
    return []


def codex_last_message(stdout: str) -> str | None:
    """Last agent message from a `codex exec --json` transcript (fallback when -o is empty)."""
    text = None
    for line in stdout.splitlines():
        event = parse_event(line)
        item = (event or {}).get("item") or {}
        if event and event.get("type") == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text") or text
    return text


def repair_mojibake(line: str) -> str:
    """Undo UTF-8 text that was decoded as cp1252 somewhere upstream ("â€”" -> "—")."""
    if "â€" not in line and "Ã" not in line:
        return line
    try:
        return line.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return line


def is_result_event(event: dict) -> bool:
    return event.get("type") == "result" or ("duration_api_ms" in event and "result" in event)


def claude_result(stdout: str) -> str | None:
    """Final answer text from a stream-json transcript (the last result event)."""
    result = None
    for line in stdout.splitlines():
        event = parse_event(line)
        if event and is_result_event(event) and isinstance(event.get("result"), str):
            result = event["result"]
    return result


def claude_usage(event: dict) -> tuple[float, float] | None:
    """(pct_used, window_resets_at_epoch) of Claude's five-hour window, if the event carries it."""
    if event.get("type") != "rate_limit_event":
        return None
    info = event.get("rate_limit_info") or {}
    window = ((info.get("unifiedWindows") or {}).get("five_hour")) or {}
    util = window.get("utilization")
    if util is None and info.get("rateLimitType") == "five_hour":
        util = info.get("utilization")
    if util is None:
        return None
    return round(float(util) * 100, 1), float(window.get("resetsAt") or info.get("resetsAt") or 0)
