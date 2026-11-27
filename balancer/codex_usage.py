"""Codex CLI usage windows, read from Codex's own session files.

`codex exec --json` does not report rate limits, but every Codex session (CLI or
desktop app) writes `token_count` events with a `rate_limits` object to
~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl (base dir: CODEX_HOME):

    "rate_limits": {"primary":   {"used_percent": 10.0, "window_minutes": 300,   "resets_at": 1795345510},
                    "secondary": {"used_percent": 2.0,  "window_minutes": 10080, "resets_at": 1795932310}}

The newest session file holds the current figures. Read-only; nothing is sent anywhere.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

_TAIL_BYTES = 512 * 1024
_cache: dict = {"key": None, "value": None, "checked": 0.0}


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def _newest_session_files(limit: int = 5) -> list[Path]:
    """Newest rollout files, walking the date folders from the latest day backwards."""
    root = codex_home() / "sessions"
    if not root.is_dir():
        return []
    found: list[Path] = []
    for year in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        for month in sorted((p for p in year.iterdir() if p.is_dir()), reverse=True):
            for day in sorted((p for p in month.iterdir() if p.is_dir()), reverse=True):
                files = sorted(day.glob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
                found.extend(files)
                if len(found) >= limit:
                    return found[:limit]
    return found


def _last_rate_limits(path: Path) -> dict | None:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _TAIL_BYTES))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if '"rate_limits"' not in line:
            continue
        try:
            payload = json.loads(line).get("payload") or {}
        except ValueError:
            continue
        limits = payload.get("rate_limits")
        if isinstance(limits, dict) and (limits.get("primary") or limits.get("secondary")):
            return limits
    return None


def _window(name: str, data: dict | None, now: float) -> dict | None:
    if not isinstance(data, dict) or data.get("used_percent") is None:
        return None
    minutes = int(data.get("window_minutes") or 0)
    resets_at = float(data.get("resets_at") or 0)
    pct = float(data["used_percent"])
    if resets_at and resets_at < now:
        pct = 0.0  # the window already rolled over since Codex last reported
    label = "5h" if minutes == 300 else ("7 dni" if minutes == 10080 else f"{minutes} min")
    return {"agent": "codex" if minutes == 300 else f"codex ({label})", "window": label,
            "pct_used": round(min(100.0, max(0.0, pct)), 1), "resets_at": resets_at or None,
            "window_minutes": minutes, "source": name}


def codex_windows(max_age_s: float = 5.0) -> list[dict]:
    """Current Codex usage windows (5h first), cached for a few seconds."""
    now = time.time()
    if now - _cache["checked"] < max_age_s and _cache["value"] is not None:
        return _cache["value"]
    windows: list[dict] = []
    for path in _newest_session_files():
        limits = _last_rate_limits(path)
        if limits:
            for name in ("primary", "secondary"):
                w = _window(name, limits.get(name), now)
                if w:
                    windows.append(w)
            break
    windows.sort(key=lambda w: w["window_minutes"])
    _cache.update(value=windows, checked=now)
    return windows
