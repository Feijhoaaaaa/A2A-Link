"""Durable ownership and process identity for persistent CLI issue turns."""
from __future__ import annotations

import ctypes
import datetime as dt
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"
CREATE_SUSPENDED = 0x00000004


class DurableJournal:
    def __init__(self, path: str | Path, owner_epoch: int = 1):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.owner_epoch = owner_epoch
        self._lock = threading.Lock()

    def append(self, event: str, **data: Any) -> dict[str, Any]:
        try:
            self.owner_epoch = int((self.path.parent / "owner-epoch").read_text(encoding="ascii"))
        except (OSError, ValueError):
            pass
        row = {"event": event, "owner_epoch": self.owner_epoch,
               "at": dt.datetime.now(dt.timezone.utc).isoformat(), **data}
        encoded = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock, self.path.open("ab") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        return row

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"corrupt supervisor journal line {number}") from exc
            if not isinstance(row, dict) or not isinstance(row.get("event"), str):
                raise ValueError(f"invalid supervisor journal line {number}")
            rows.append(row)
        return rows


class SupervisorLock:
    """A non-blocking cross-process lock held for one supervisor action loop."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = None
        self._locked = False
        self._owner_thread: int | None = None
        self._depth = 0
        self._state_lock = threading.Lock()

    def acquire(self) -> bool:
        owner = threading.get_ident()
        with self._state_lock:
            if self._locked:
                if self._owner_thread != owner:
                    return False
                self._depth += 1
                return True
            stream = self.path.open("a+b")
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                if IS_WINDOWS:
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError):
                stream.close()
                return False
            self._stream = stream
            self._locked = True
            self._owner_thread = owner
            self._depth = 1
            return True

    def release(self) -> None:
        owner = threading.get_ident()
        with self._state_lock:
            if not self._locked or self._stream is None:
                return
            if self._owner_thread != owner:
                raise RuntimeError("supervisor lock must be released by its owning thread")
            if self._depth > 1:
                self._depth -= 1
                return
            stream = self._stream
            try:
                stream.seek(0)
                if IS_WINDOWS:
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()
                self._stream = None
                self._locked = False
                self._owner_thread = None
                self._depth = 0

    @property
    def held_by_current_thread(self) -> bool:
        with self._state_lock:
            return self._locked and self._owner_thread == threading.get_ident()

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("another A2A_Link supervisor owns this install")
        return self

    def __exit__(self, *_exc):
        self.release()


def install_id(directory: str | Path) -> str:
    """Return a stable random install id stored beside the process journal."""
    path = Path(directory) / "install-id"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        value = uuid.uuid4().hex
        try:
            with path.open("x", encoding="ascii") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            return path.read_text(encoding="ascii").strip()
        return value


class SupervisorRuntime:
    """Owns one local install's durable attempt journal and process scopes."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.install_id = install_id(self.directory)
        self.owner = f"supervisor:{socket.gethostname()}:{self.install_id}"
        self.journal = DurableJournal(self.directory / "attempts.jsonl")
        self.lock = SupervisorLock(self.directory / "supervisor.lock")
        self._epoch_path = self.directory / "owner-epoch"

    def acquire(self) -> bool:
        if not self.lock.acquire():
            return False
        try:
            try:
                epoch = int(self._epoch_path.read_text(encoding="ascii"))
            except FileNotFoundError:
                epoch = 0
            epoch += 1
            temp = self._epoch_path.with_suffix(".tmp")
            with temp.open("w", encoding="ascii") as stream:
                stream.write(str(epoch))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self._epoch_path)
            self.journal.owner_epoch = epoch
            self.journal.append("owner_acquired", owner=self.owner)
            return True
        except Exception:
            self.lock.release()
            raise

    def release(self) -> None:
        self.lock.release()

    def spawn(self, attempt_id: str, message_id: str, argv: list[str], cwd: str,
              env: dict[str, str] | None = None, metadata: dict[str, Any] | None = None,
              *, memory_limit_mb: int | None = None
              ) -> tuple[subprocess.Popen, dict[str, Any], WindowsJob | None, JobKeeper | None]:
        if not self.lock.held_by_current_thread:
            raise RuntimeError("supervisor lock must be held before spawning")
        if memory_limit_mb is not None:
            if type(memory_limit_mb) is not int or memory_limit_mb < 1:
                raise ValueError("memory_limit_mb must be a positive integer")
            if not IS_WINDOWS:
                raise RuntimeError("whole-tree memory limits are unsupported on this POSIX runner")
        attempt_dir = self.directory / "attempts" / attempt_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        stdout_file = (attempt_dir / "stdout.jsonl").open("ab", buffering=0)
        stderr_file = (attempt_dir / "stderr.log").open("ab", buffering=0)
        self.journal.append("spawn_intent", attempt_id=attempt_id, message_id=message_id, cwd=cwd,
                            argv_sha256=hashlib.sha256(json.dumps(argv).encode("utf-8")).hexdigest(),
                            **(metadata or {}))
        try:
            if IS_WINDOWS:
                job_name = f"Local\\a2a-link-{self.install_id}-{attempt_id}"
                job = WindowsJob.create(job_name)
                keeper = None
                try:
                    if memory_limit_mb is not None:
                        job.set_memory_limit_mb(memory_limit_mb)
                    keeper = start_job_keeper(job_name, self.directory)
                    proc, identity = spawn_suspended(argv, cwd, env or os.environ.copy(), job,
                                                     stdout=stdout_file, stderr=stderr_file)
                except Exception as exc:
                    self.journal.append("spawn_failed", attempt_id=attempt_id, error=str(exc))
                    job.close()
                    if keeper is not None:
                        keeper.release()
                    raise
            else:
                job = keeper = None
                try:
                    proc = subprocess.Popen(argv, cwd=cwd, env=env, shell=False, stdin=subprocess.DEVNULL,
                                            stdout=stdout_file, stderr=stderr_file, start_new_session=True,
                                            close_fds=True)
                except OSError as exc:
                    self.journal.append("spawn_failed", attempt_id=attempt_id, error=str(exc))
                    raise
                identity = process_identity(proc.pid, str(proc.pid))
        finally:
            stdout_file.close()
            stderr_file.close()
        self.journal.append("spawned", attempt_id=attempt_id, process_identity=identity)
        return proc, identity, job, keeper

    def recover(self, attempt_id: str, identity: dict[str, Any], grace_s: float = 0,
                termination_callback=None) -> str:
        if not self.lock.held_by_current_thread:
            raise RuntimeError("supervisor lock must be held before recovery")
        self.journal.append("reattach_started", attempt_id=attempt_id, process_identity=identity)
        return recover_termination(identity, grace_s=grace_s, journal=self.journal,
                                   attempt_id=attempt_id, termination_callback=termination_callback)

    def capture_session(self, attempt_id: str, adapter_type: str, generation: int,
                         cwd: str, requested_profile: dict[str, Any] | None = None,
                         offset: int = 0) -> dict[str, Any] | None:
        """Recover the first CLI session event from the durable stdout file and journal it."""
        from mcp_server.issue_adapters import ClaudeAdapter, CodexAdapter

        adapters = {"claude": ClaudeAdapter, "codex": CodexAdapter}
        try:
            adapter = adapters[adapter_type]()
        except KeyError as exc:
            raise ValueError(f"unsupported adapter: {adapter_type}") from exc
        path = self.directory / "attempts" / attempt_id / "stdout.jsonl"
        if not path.exists():
            return None
        with path.open("rb") as stream:
            stream.seek(offset)
            while line := stream.readline():
                current_offset = stream.tell()
                try:
                    event = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                session_id = adapter.session_id_from_event(event)
                if session_id:
                    data = {"attempt_id": attempt_id, "adapter_type": adapter_type,
                            "session_id": session_id, "cwd": cwd, "generation": generation,
                            "requested_profile": requested_profile, "output_offset": current_offset}
                    self.journal.append("session_started", **data)
                    return data
        return None

    def handle_saved(self, attempt_id: str) -> None:
        self.journal.append("handle_saved", attempt_id=attempt_id)


def _filetime_iso(value) -> str:
    ticks = (value.dwHighDateTime << 32) | value.dwLowDateTime
    unix_seconds = ticks / 10_000_000 - 11_644_473_600
    return dt.datetime.fromtimestamp(unix_seconds, dt.timezone.utc).isoformat(timespec="milliseconds")


def _windows_process_start_time(handle: int) -> str:
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.POINTER(FILETIME),
                                         ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
                                         ctypes.POINTER(FILETIME)]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
    if not kernel32.GetProcessTimes(wintypes.HANDLE(handle), ctypes.byref(created), ctypes.byref(exited),
                                    ctypes.byref(kernel), ctypes.byref(user)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _filetime_iso(created)


def _posix_process_start_time(pid: int) -> str:
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        raw = stat_path.read_text(encoding="ascii")
        rest = raw[raw.rfind(")") + 2:].split()
        start_ticks = int(rest[19])  # field 22, after pid and comm (fields 1-2)
        boot_time = None
        for line in Path("/proc/stat").read_text(encoding="ascii").splitlines():
            if line.startswith("btime "):
                boot_time = int(line.split()[1])
                break
        if boot_time is None:
            raise OSError("could not read OS boot time")
        return dt.datetime.fromtimestamp(boot_time + start_ticks / os.sysconf("SC_CLK_TCK"),
                                         dt.timezone.utc).isoformat(timespec="milliseconds")
    result = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                            text=True, check=True).stdout.strip()
    if not result:
        raise ProcessLookupError(pid)
    parsed = dt.datetime.strptime(result, "%a %b %d %H:%M:%S %Y").replace(tzinfo=dt.timezone.utc)
    return parsed.isoformat(timespec="milliseconds")


def process_identity(pid: int, group: str) -> dict[str, Any]:
    if IS_WINDOWS:
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            start_time = _windows_process_start_time(int(handle))
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        platform = "windows"
    else:
        start_time = _posix_process_start_time(pid)
        platform = "posix"
    return {"host": socket.gethostname(), "platform": platform, "pid": pid,
            "start_time": start_time, "group": group}


def identity_matches(identity: dict[str, Any]) -> bool:
    """Return whether pid still names the recorded process, without signalling it."""
    if identity.get("host") != socket.gethostname() or identity.get("platform") != ("windows" if IS_WINDOWS else "posix"):
        return False
    try:
        current = process_identity(int(identity["pid"]), str(identity["group"]))
    except (OSError, ValueError, KeyError, ProcessLookupError):
        return False
    return current["start_time"] == identity.get("start_time")


def process_group_empty(identity: dict[str, Any]) -> bool:
    """Check the termination scope. Failure to inspect is an error, never empty."""
    if identity.get("host") != socket.gethostname():
        raise OSError("process belongs to another host")
    if identity.get("platform") == "windows":
        job = WindowsJob.open(str(identity["group"]))
        try:
            return job.active_processes() == 0
        finally:
            job.close()
    if identity.get("platform") != "posix":
        raise ValueError("unsupported process platform")
    try:
        os.killpg(int(identity["group"]), 0)
    except ProcessLookupError:
        return True
    except PermissionError as exc:
        raise OSError("permission denied inspecting process group") from exc
    return False


def process_scope_empty(identity: dict[str, Any], job: "WindowsJob | None" = None) -> bool:
    """Prove the whole supervised scope is gone, even when its leader has exited."""
    if identity.get("platform") == "windows":
        if job is None:
            return process_group_empty(identity)
        return job.active_processes() == 0
    return process_group_empty(identity)


def wait_for_process_scope(proc: subprocess.Popen, identity: dict[str, Any],
                          job: "WindowsJob | None" = None, timeout_s: float = 0,
                          poll_s: float = 0.05) -> bool:
    """Wait for both the CLI leader and every process in its Job/group to exit."""
    deadline = time.monotonic() + timeout_s
    while True:
        leader_exited = proc.poll() is not None
        scope_empty = process_scope_empty(identity, job)
        if leader_exited and scope_empty:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(poll_s, max(0.001, deadline - time.monotonic())))


def recover_termination(identity: dict[str, Any], grace_s: float = 0, kill_timeout_s: float = 10,
                        journal: DurableJournal | None = None, attempt_id: str | None = None,
                        termination_callback=None) -> str:
    """Prove an attempt process tree exited; never fall back from an unknown identity to a pid kill."""
    if identity.get("host") != socket.gethostname():
        raise RuntimeError("unresolved: process host does not match this supervisor")
    platform = identity.get("platform")
    if platform == "windows":
        try:
            job = WindowsJob.open(str(identity["group"]))
        except OSError as exc:
            raise RuntimeError(f"unresolved: cannot reopen Job Object: {exc}") from exc
        try:
            if grace_s:
                job.wait_empty(grace_s)
            if job.active_processes() == 0:
                method = "exited"
            else:
                job.terminate()
                if not job.wait_empty(kill_timeout_s):
                    raise RuntimeError("unresolved: Job Object remained active after termination")
                method = "terminated"
        finally:
            job.close()
    elif platform == "posix":
        if not identity_matches(identity):
            try:
                if process_group_empty(identity):
                    method = "exited"
                else:
                    raise RuntimeError("unresolved: process leader identity changed while group is active")
            except OSError as exc:
                raise RuntimeError(f"unresolved: cannot inspect process group: {exc}") from exc
        else:
            deadline = time.monotonic() + grace_s
            while time.monotonic() < deadline and not process_group_empty(identity):
                time.sleep(min(0.05, deadline - time.monotonic()))
            if process_group_empty(identity):
                method = "exited"
            else:
                if not identity_matches(identity):
                    raise RuntimeError("unresolved: process identity changed before termination")
                os.killpg(int(identity["group"]), 15)
                deadline = time.monotonic() + min(10, kill_timeout_s)
                while time.monotonic() < deadline and not process_group_empty(identity):
                    time.sleep(0.05)
                if not process_group_empty(identity):
                    os.killpg(int(identity["group"]), 9)
                    deadline = time.monotonic() + kill_timeout_s
                    while time.monotonic() < deadline and not process_group_empty(identity):
                        time.sleep(0.05)
                    if not process_group_empty(identity):
                        raise RuntimeError("unresolved: process group remained active after SIGKILL")
                method = "terminated"
    else:
        raise RuntimeError("unresolved: unsupported process identity platform")
    if journal:
        journal.append("termination_done", attempt_id=attempt_id, method=method, process_identity=identity)
    if termination_callback:
        termination_callback(identity, method)
    return method


if IS_WINDOWS:
    from ctypes import wintypes

    class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                    ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                    ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                    ("IoInfo", _IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]


class WindowsJob:
    """Named Windows Job Object; no KILL_ON_JOB_CLOSE, so a new supervisor can reopen it."""

    JOB_OBJECT_QUERY = 0x0004
    JOB_OBJECT_TERMINATE = 0x0008
    JOB_OBJECT_ASSIGN_PROCESS = 0x0001
    JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200

    def __init__(self, name: str, handle: int):
        self.name, self.handle = name, handle

    @staticmethod
    def _kernel32():
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return kernel32

    @classmethod
    def create(cls, name: str) -> "WindowsJob":
        if not IS_WINDOWS:
            raise OSError("Windows Job Objects are only available on Windows")
        kernel32 = cls._kernel32()
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        handle = kernel32.CreateJobObjectW(None, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return cls(name, int(handle))

    @classmethod
    def open(cls, name: str) -> "WindowsJob":
        if not IS_WINDOWS:
            raise OSError("Windows Job Objects are only available on Windows")
        kernel32 = cls._kernel32()
        kernel32.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.OpenJobObjectW.restype = wintypes.HANDLE
        handle = kernel32.OpenJobObjectW(cls.JOB_OBJECT_QUERY | cls.JOB_OBJECT_TERMINATE |
                                         cls.JOB_OBJECT_ASSIGN_PROCESS, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        return cls(name, int(handle))

    def assign(self, process_handle: int) -> None:
        kernel32 = self._kernel32()
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        if not kernel32.AssignProcessToJobObject(wintypes.HANDLE(self.handle), wintypes.HANDLE(process_handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def _extended_limit_information(self):
        kernel32 = self._kernel32()
        kernel32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                        wintypes.DWORD, ctypes.c_void_p]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if not kernel32.QueryInformationJobObject(
                wintypes.HANDLE(self.handle), self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return info

    def set_memory_limit_mb(self, memory_limit_mb: int) -> None:
        """Apply a hard committed-memory limit to the sum of all job processes."""
        if type(memory_limit_mb) is not int or memory_limit_mb < 1:
            raise ValueError("memory_limit_mb must be a positive integer")
        limit = memory_limit_mb * 1024 * 1024
        if limit > (1 << (ctypes.sizeof(ctypes.c_size_t) * 8)) - 1:
            raise ValueError("memory_limit_mb is too large for this platform")
        info = self._extended_limit_information()
        info.BasicLimitInformation.LimitFlags |= self.JOB_OBJECT_LIMIT_JOB_MEMORY
        info.JobMemoryLimit = limit
        kernel32 = self._kernel32()
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                      wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        if not kernel32.SetInformationJobObject(
                wintypes.HANDLE(self.handle), self.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())

    def memory_limit_mb(self) -> int | None:
        info = self._extended_limit_information()
        if not info.BasicLimitInformation.LimitFlags & self.JOB_OBJECT_LIMIT_JOB_MEMORY:
            return None
        limit = int(info.JobMemoryLimit)
        mib = 1024 * 1024
        if limit % mib:
            raise RuntimeError("Job Object memory limit is not an exact MiB value")
        return limit // mib

    def active_processes(self) -> int:
        kernel32 = self._kernel32()
        kernel32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                        wintypes.DWORD, ctypes.c_void_p]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        info = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if not kernel32.QueryInformationJobObject(wintypes.HANDLE(self.handle),
                self.JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(info.ActiveProcesses)

    def terminate(self, exit_code: int = 1) -> None:
        if self.active_processes() == 0:
            return
        kernel32 = self._kernel32()
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        if not kernel32.TerminateJobObject(wintypes.HANDLE(self.handle), exit_code):
            raise ctypes.WinError(ctypes.get_last_error())

    def wait_empty(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.active_processes() == 0:
                return True
            time.sleep(0.05)
        return self.active_processes() == 0

    def close(self) -> None:
        if self.handle:
            self._kernel32().CloseHandle(wintypes.HANDLE(self.handle))
            self.handle = 0


class JobKeeper:
    def __init__(self, process: subprocess.Popen, job_name: str, release_path: Path):
        self.process, self.job_name = process, job_name
        self.release_path = release_path

    def release(self, timeout_s: float = 5) -> None:
        job = WindowsJob.open(self.job_name)
        try:
            active = job.active_processes()
        finally:
            job.close()
        if active != 0:
            raise RuntimeError(f"cannot release keeper while Job Object has {active} active process(es)")
        self.release_path.parent.mkdir(parents=True, exist_ok=True)
        self.release_path.write_text("release\n", encoding="ascii")
        try:
            self.process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Job Object keeper did not release its handle") from exc


def start_job_keeper(job_name: str, directory: str | Path, timeout_s: float = 5) -> JobKeeper:
    """Start a small process outside the Job that keeps its named handle reopenable."""
    if not IS_WINDOWS:
        raise OSError("Job Object keepers are only available on Windows")
    root = Path(directory).resolve() / "supervisor-keepers"
    root.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(job_name.encode("utf-8")).hexdigest()
    ready = root / f"{token}.ready"
    release = root / f"{token}.release"
    ready.unlink(missing_ok=True)
    release.unlink(missing_ok=True)
    source_root = str(Path(__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": source_root + os.pathsep + os.environ.get("PYTHONPATH", "")}
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | 0x01000000
    process = subprocess.Popen([sys.executable, "-m", "mcp_server.job_watchdog", job_name,
                                str(ready), str(release)], cwd=source_root, env=env, shell=False,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=flags)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if ready.exists():
            return JobKeeper(process, job_name, release)
        if process.poll() is not None:
            raise RuntimeError(f"Job Object keeper exited before opening {job_name}")
        time.sleep(0.02)
    process.terminate()
    process.wait(timeout=2)
    raise TimeoutError(f"Job Object keeper did not open {job_name}")


def _open_suspended_process_thread(pid: int) -> int:
    class THREADENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                    ("tpBasePri", ctypes.c_long), ("tpDeltaPri", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD)]

    kernel32 = WindowsJob._kernel32()
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    if snapshot == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    entry = THREADENTRY32()
    entry.dwSize = ctypes.sizeof(entry)
    found = 0
    try:
        ok = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                found = kernel32.OpenThread(0x0002, False, entry.th32ThreadID)  # THREAD_SUSPEND_RESUME
                if found:
                    break
            entry.dwSize = ctypes.sizeof(entry)
            ok = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if not found:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(found)


def spawn_suspended(argv: list[str], cwd: str, env: dict[str, str],
                    job: WindowsJob, stdout=None, stderr=None) -> tuple[subprocess.Popen, dict[str, Any]]:
    """Start a suspended Windows child, assign it to a Job Object, then resume it."""
    if not IS_WINDOWS:
        raise OSError("spawn_suspended is Windows-only")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | CREATE_SUSPENDED
    process = subprocess.Popen(argv, cwd=cwd, env=env, shell=False, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE if stdout is None else stdout,
                               stderr=subprocess.PIPE if stderr is None else stderr,
                               creationflags=flags)
    try:
        job.assign(process._handle)
        identity = process_identity(process.pid, job.name)
        kernel32 = WindowsJob._kernel32()
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        thread = _open_suspended_process_thread(process.pid)
        try:
            if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel32.CloseHandle(thread)
        return process, identity
    except Exception:
        job.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        raise
