"""Per-task live progress log, shared by the MCP server (writer) and the balancer (reader).

One plain-text file per delegated task: ~/.a2a_link/tasks/<task_id>.log
(override the base directory with A2A_LINK_HOME). Every line is prefixed with
the time elapsed since the task started, e.g. "[01:05] -> PowerShell: python -m pytest".
"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def home() -> Path:
    return Path(os.environ.get("A2A_LINK_HOME", Path.home() / ".a2a_link"))


def log_path(task_id: str) -> Path:
    if not _SAFE_ID.match(task_id):  # task ids reach this from HTTP paths: no traversal
        raise ValueError(f"invalid task id: {task_id!r}")
    return home() / "tasks" / f"{task_id}.log"


def fmt_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class TaskLog:
    def __init__(self, task_id: str, max_line: int = 2000):
        self.path = log_path(task_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.start = time.monotonic()
        self.max_line = max_line
        self._lock = threading.Lock()

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def write(self, line: str) -> None:
        line = line.replace("\r", "")
        if len(line) > self.max_line:
            line = line[: self.max_line] + " …"
        stamp = f"[{fmt_elapsed(self.elapsed())}] "
        text = "".join(stamp + part + "\n" for part in (line.split("\n") or [""]))
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(text)


def read_tail(task_id: str, lines: int = 200) -> str | None:
    """Last `lines` lines of a task log, or None when the task has no log."""
    path = log_path(task_id)
    if not path.is_file():
        return None
    lines = max(1, min(int(lines), 5000))
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        chunk = min(size, 256 * 1024)  # enough for the requested tail of normal logs
        f.seek(size - chunk)
        data = f.read().decode("utf-8", errors="replace")
    return "\n".join(data.splitlines()[-lines:])
