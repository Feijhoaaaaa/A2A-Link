"""Low-level process runner: argv only (never a shell), bounded output, hard timeout.

Shared by the CLI delegation helper and the policy-gated terminal executor.
"""
from __future__ import annotations

import codecs
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

TIMEOUT_RETURNCODE = 124
IS_WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float
    stdout_truncated: bool = False
    stderr_truncated: bool = False


LineCallback = Callable[[str, str], None]  # (stream name, line without newline)


class _BoundedReader(threading.Thread):
    """Drains a pipe fully (so the child never blocks) but keeps at most `limit` bytes.

    Optionally reports each complete line as soon as it arrives (live progress).
    """

    def __init__(self, pipe, limit: int, name: str = "", on_line: LineCallback | None = None):
        super().__init__(daemon=True)
        self.pipe, self.limit, self.stream, self.on_line = pipe, limit, name, on_line
        self.buf = bytearray()
        self.truncated = False
        self.last_activity = time.monotonic()

    def _emit(self, text: str) -> None:
        try:
            self.on_line(self.stream, text)
        except Exception:
            pass  # a broken progress sink must never break the child process

    def run(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        # read1 returns whatever is available now; read(n) would wait for n bytes or EOF.
        read = getattr(self.pipe, "read1", self.pipe.read)
        try:
            for chunk in iter(lambda: read(65536), b""):
                self.last_activity = time.monotonic()
                room = self.limit - len(self.buf)
                if room > 0:
                    self.buf += chunk[:room]
                if len(chunk) > room:
                    self.truncated = True
                if self.on_line:
                    pending += decoder.decode(chunk)
                    *lines, pending = pending.split("\n")
                    for line in lines:
                        self._emit(line.rstrip("\r"))
            if self.on_line and (pending := pending + decoder.decode(b"", final=True)):
                self._emit(pending.rstrip("\r"))
        finally:
            self.pipe.close()

    def text(self) -> str:
        return self.buf.decode("utf-8", errors="replace")


def _kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if IS_WINDOWS:
        # taskkill /T kills the whole child tree; needs no admin rights for own processes.
        taskkill = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe")
        subprocess.run([taskkill, "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_process(argv: list[str], cwd: str, timeout_s: float,
                env: dict[str, str] | None = None,
                max_output_bytes: int = 1_000_000,
                on_line: LineCallback | None = None,
                heartbeat_s: float | None = None,
                on_heartbeat: Callable[[float], None] | None = None) -> ProcessResult:
    """on_line gets each output line live; on_heartbeat(elapsed) fires after heartbeat_s of silence."""
    if not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("argv must be a non-empty list of strings")
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True  # own process group -> killable as a unit

    start = time.monotonic()
    proc = subprocess.Popen(argv, cwd=cwd, env=env, shell=False,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, **kwargs)
    out = _BoundedReader(proc.stdout, max_output_bytes, "stdout", on_line)
    err = _BoundedReader(proc.stderr, max_output_bytes, "stderr", on_line)
    out.start(); err.start()
    timed_out = False
    deadline = start + timeout_s
    last_beat = start
    while True:
        step = min(1.0, max(0.0, deadline - time.monotonic()))
        try:
            proc.wait(timeout=step)
            break
        except subprocess.TimeoutExpired:
            pass
        now = time.monotonic()
        if now >= deadline:
            timed_out = True
            _kill_tree(proc)
            break
        if heartbeat_s and on_heartbeat:
            quiet_since = max(out.last_activity, err.last_activity, last_beat)
            if now - quiet_since >= heartbeat_s:
                last_beat = now
                try:
                    on_heartbeat(now - start)
                except Exception:
                    pass
    out.join(5); err.join(5)
    duration = time.monotonic() - start

    stderr = err.text()
    if timed_out:
        note = f"timed out after {timeout_s}s"
        stderr = f"{stderr}\n{note}" if stderr else note
    return ProcessResult(
        returncode=TIMEOUT_RETURNCODE if timed_out else proc.returncode,
        stdout=out.text(), stderr=stderr, timed_out=timed_out,
        duration_s=round(duration, 3),
        stdout_truncated=out.truncated, stderr_truncated=err.truncated,
    )
