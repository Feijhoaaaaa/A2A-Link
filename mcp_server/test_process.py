"""Supervised process scope used by the cross-platform test runner.

This adapter deliberately delegates process creation, identity, durable journaling,
and tree termination to the existing ``SupervisorRuntime`` implementation.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Literal

from . import supervisor
from .supervisor import SupervisorRuntime, process_scope_empty, recover_termination

_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class TestProcess:
    """One test command and its supervised process tree.

    The runner state lock remains held until ``close`` proves the process scope is
    empty and releases the supervisor's platform handles.
    """

    __test__ = False  # This is a production type, not a pytest test class.

    def __init__(self, runtime: SupervisorRuntime, job_id: str, process: subprocess.Popen,
                 identity: dict, job, keeper, memory_limit_mb: int | None):
        self._runtime = runtime
        self._job_id = job_id
        self._process = process
        self._identity = identity
        self._job = job
        self._keeper = keeper
        self._memory_limit_mb = memory_limit_mb
        # SupervisorRuntime.spawn returns only after any requested Job Object cap
        # has been installed. Keep this configuration fact after closing its handle.
        self._memory_limit_enforced = memory_limit_mb is not None
        self._closed = False
        self._termination_method: Literal["exited", "terminated"] | None = None

    @classmethod
    def spawn(cls, argv: list[str], cwd: Path, env: dict[str, str], state_dir: Path,
              job_id: str, *, memory_limit_mb: int | None = None) -> "TestProcess":
        if not isinstance(argv, list) or not argv or any(
                not isinstance(arg, str) or "\0" in arg for arg in argv):
            raise ValueError("argv must be a non-empty list of strings without NUL characters")
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise ValueError("job_id must be a path-safe identifier")
        if not isinstance(env, dict) or any(
                not isinstance(key, str) or not isinstance(value, str) or "\0" in key + value
                for key, value in env.items()):
            raise ValueError("env must map strings to NUL-free strings")
        if memory_limit_mb is not None:
            if type(memory_limit_mb) is not int or memory_limit_mb < 1:
                raise ValueError("memory_limit_mb must be a positive integer")
            supported, reason = cls.memory_limit_support()
            if not supported:
                raise RuntimeError(reason)

        workdir = Path(cwd).resolve()
        if not workdir.is_dir():
            raise ValueError("cwd must be an existing directory")
        runtime = SupervisorRuntime(Path(state_dir).resolve())
        if not runtime.acquire():
            raise RuntimeError("runner state is already owned by another process")
        try:
            process, identity, job, keeper = runtime.spawn(
                attempt_id=job_id,
                message_id=job_id,
                argv=argv,
                cwd=str(workdir),
                env=dict(env),
                metadata={"kind": "test_job", "job_id": job_id},
                memory_limit_mb=memory_limit_mb,
            )
            return cls(runtime, job_id, process, identity, job, keeper, memory_limit_mb)
        except BaseException:
            runtime.release()
            raise

    @property
    def closed(self) -> bool:
        return self._closed

    @staticmethod
    def memory_limit_support() -> tuple[bool, str]:
        """Probe whether this runner can enforce a hard whole-tree memory limit."""
        if not supervisor.IS_WINDOWS:
            return False, "whole-tree memory limits require Windows Job Objects on this runner"
        job = None
        try:
            job = supervisor.WindowsJob.create(f"Local\\a2a-link-memory-probe-{os.getpid()}")
            job.set_memory_limit_mb(1)
            if job.memory_limit_mb() != 1:
                return False, "Windows Job Object did not confirm the requested memory limit"
            return True, "Windows Job Object job-wide committed-memory limit is available"
        except (OSError, RuntimeError, ValueError) as exc:
            return False, f"Windows Job Object memory limit unavailable: {exc}"
        finally:
            if job is not None:
                job.close()

    def poll(self) -> int | None:
        if self._closed:
            raise RuntimeError("process is closed")
        return self._process.poll()

    def memory_limit_enforced(self) -> bool | None:
        """Report whether the requested whole-tree cap is installed, not whether it was reached."""
        if self._memory_limit_mb is None:
            return None
        return self._memory_limit_enforced

    def read_output(self, stdout_offset: int, stderr_offset: int,
                    limit: int) -> tuple[bytes, bytes, int, int]:
        """Read up to ``limit`` new raw bytes from each supervisor output file."""
        if self._closed:
            raise RuntimeError("process is closed")
        if type(stdout_offset) is not int or stdout_offset < 0:
            raise ValueError("stdout_offset must be a non-negative integer")
        if type(stderr_offset) is not int or stderr_offset < 0:
            raise ValueError("stderr_offset must be a non-negative integer")
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")

        directory = self._runtime.directory / "attempts" / self._job_id
        stdout, next_stdout = self._read_file(directory / "stdout.jsonl", stdout_offset, limit)
        stderr, next_stderr = self._read_file(directory / "stderr.log", stderr_offset, limit)
        return stdout, stderr, next_stdout, next_stderr

    @staticmethod
    def _read_file(path: Path, offset: int, limit: int) -> tuple[bytes, int]:
        try:
            with path.open("rb") as stream:
                size = os.fstat(stream.fileno()).st_size
                if offset > size:
                    raise ValueError(f"output offset {offset} is past the stored length {size}")
                stream.seek(offset)
                data = stream.read(limit)
        except FileNotFoundError:
            if offset:
                raise ValueError(f"output file {path.name} is missing at offset {offset}") from None
            return b"", 0
        return data, offset + len(data)

    def terminate_and_prove(self, grace_s: float,
                            kill_timeout_s: float) -> Literal["exited", "terminated"]:
        if self._closed:
            raise RuntimeError("process is closed")
        if grace_s < 0 or kill_timeout_s < 0:
            raise ValueError("termination timeouts must be non-negative")
        if self._termination_method is not None:
            return self._termination_method

        # SupervisorRuntime.recover currently fixes the kill timeout at 10 seconds;
        # call its existing recovery primitive so this adapter can honor the runner's
        # configured bound while still using the supervisor's identity/journal.
        self._runtime.journal.append("reattach_started", attempt_id=self._job_id,
                                     process_identity=self._identity)
        method = recover_termination(
            identity=self._identity,
            grace_s=grace_s,
            kill_timeout_s=kill_timeout_s,
            journal=self._runtime.journal,
            attempt_id=self._job_id,
        )
        if method not in ("exited", "terminated"):
            raise RuntimeError("supervisor returned without proving process termination")
        try:
            self._process.wait(timeout=max(kill_timeout_s, 0.1))
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("process leader did not exit after scope termination") from exc
        if not process_scope_empty(self._identity, self._job):
            raise RuntimeError("supervised process scope is not proven empty")
        self._termination_method = method
        return method

    def close(self) -> None:
        if self._closed:
            return
        # Never release the local one-job lock or Windows keeper while a descendant
        # might still be alive. The caller can retry termination or surface unresolved.
        if self._process.poll() is None or not process_scope_empty(self._identity, self._job):
            raise RuntimeError("supervised process scope is not proven empty")
        if self._keeper is not None:
            self._keeper.release()
        if self._job is not None:
            self._job.close()
        self._runtime.release()
        self._closed = True
