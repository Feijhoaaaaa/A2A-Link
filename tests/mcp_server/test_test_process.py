from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from mcp_server import supervisor, test_process


def _wait_for(path: Path, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), f"child process did not create {path}"


def _wait_exit(process, timeout: float = 5) -> int:
    deadline = time.monotonic() + timeout
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    exit_code = process.poll()
    assert exit_code is not None, "process did not exit"
    return exit_code


def _descendant_command(tmp_path: Path) -> list[str]:
    heartbeat = tmp_path / "grandchild-alive"
    grandchild = ("import time\nfrom pathlib import Path\n"
                  f"p=Path({str(heartbeat)!r})\n"
                  "while True:\n    p.open('a').write('x')\n    time.sleep(.03)\n")
    child = ("import subprocess,sys,time\n"
             "subprocess.Popen([sys.executable, '-c', " + repr(grandchild) + "])\n"
             "time.sleep(30)\n")
    parent = ("import subprocess,sys,time\n"
              "subprocess.Popen([sys.executable, '-c', " + repr(child) + "])\n"
              "time.sleep(30)\n")
    return [sys.executable, "-c", parent]


def test_terminates_and_proves_child_and_grandchild(tmp_path):
    process = test_process.TestProcess.spawn(
        _descendant_command(tmp_path), tmp_path, dict(os.environ), tmp_path / "runner-state", "job-1"
    )
    try:
        heartbeat = tmp_path / "grandchild-alive"
        _wait_for(heartbeat)
        method = process.terminate_and_prove(grace_s=0.1, kill_timeout_s=5)
        assert method in ("exited", "terminated")
        size = heartbeat.stat().st_size
        time.sleep(0.15)
        assert heartbeat.stat().st_size == size
        process.close()
    finally:
        if not process.closed:
            process.terminate_and_prove(grace_s=0, kill_timeout_s=5)
            process.close()


def test_read_output_preserves_raw_bytes_and_offsets(tmp_path):
    command = [sys.executable, "-c",
               "import os; os.write(1, b'out\\xff'); os.write(2, b'err\\xfe')"]
    process = test_process.TestProcess.spawn(
        command, tmp_path, dict(os.environ), tmp_path / "runner-state", "job-2"
    )
    assert _wait_exit(process) == 0
    stdout, stderr, stdout_offset, stderr_offset = process.read_output(0, 0, 16)
    assert stdout == b"out\xff"
    assert stderr == b"err\xfe"
    assert (stdout_offset, stderr_offset) == (4, 4)
    process.terminate_and_prove(grace_s=0, kill_timeout_s=5)
    process.close()


def test_uncertain_termination_fails_closed_without_releasing_runner_lock(tmp_path, monkeypatch):
    process = test_process.TestProcess.spawn(
        _descendant_command(tmp_path), tmp_path, dict(os.environ), tmp_path / "runner-state", "job-3"
    )
    _wait_for(tmp_path / "grandchild-alive")
    monkeypatch.setattr(test_process, "recover_termination",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("cannot inspect scope")))
    with pytest.raises(RuntimeError, match="cannot inspect scope"):
        process.terminate_and_prove(grace_s=0, kill_timeout_s=0.1)
    with pytest.raises(RuntimeError, match="scope is not proven empty"):
        process.close()
    assert not process.closed
    monkeypatch.undo()
    process.terminate_and_prove(grace_s=0, kill_timeout_s=5)
    process.close()


@pytest.mark.parametrize("job_id", ["../escape", "", "x" * 129])
def test_rejects_job_ids_that_could_escape_state_directory(tmp_path, job_id):
    with pytest.raises(ValueError, match="job_id"):
        test_process.TestProcess.spawn([sys.executable, "-c", "pass"], tmp_path,
                                       dict(os.environ), tmp_path / "runner-state", job_id)


def test_rejects_shell_string_instead_of_argv(tmp_path):
    with pytest.raises(ValueError, match="argv"):
        test_process.TestProcess.spawn("python -c pass", tmp_path, dict(os.environ),
                                       tmp_path / "runner-state", "job-4")


def test_uncapped_process_reports_no_memory_limit_enforcement(tmp_path):
    process = test_process.TestProcess.spawn(
        [sys.executable, "-c", "pass"], tmp_path, dict(os.environ), tmp_path / "runner-state", "job-5"
    )
    try:
        assert _wait_exit(process) == 0
        process.terminate_and_prove(grace_s=0, kill_timeout_s=5)
        assert process.memory_limit_enforced() is None
    finally:
        if not process.closed:
            process.terminate_and_prove(grace_s=0, kill_timeout_s=5)
            process.close()


def test_memory_cap_is_rejected_before_spawn_when_platform_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(test_process.TestProcess, "memory_limit_support",
                        staticmethod(lambda: (False, "whole-tree memory enforcement unavailable")))
    with pytest.raises(RuntimeError, match="whole-tree memory enforcement unavailable"):
        test_process.TestProcess.spawn(
            [sys.executable, "-c", "pass"], tmp_path, dict(os.environ),
            tmp_path / "runner-state", "job-6", memory_limit_mb=256
        )
    assert not (tmp_path / "runner-state" / "attempts" / "job-6").exists()


def test_posix_memory_cap_is_rejected_before_creating_runner_state(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor, "IS_WINDOWS", False)
    supported, reason = test_process.TestProcess.memory_limit_support()
    assert not supported and "require Windows Job Objects" in reason
    state_dir = tmp_path / "runner-state"
    with pytest.raises(RuntimeError, match="require Windows Job Objects"):
        test_process.TestProcess.spawn(
            [sys.executable, "-c", "pass"], tmp_path, dict(os.environ), state_dir,
            "job-9", memory_limit_mb=256
        )
    assert not state_dir.exists()


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_memory_cap_is_rejected_before_spawn(tmp_path, limit):
    with pytest.raises(ValueError, match="memory_limit_mb"):
        test_process.TestProcess.spawn(
            [sys.executable, "-c", "pass"], tmp_path, dict(os.environ),
            tmp_path / "runner-state", "job-10", memory_limit_mb=limit
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object memory limits")
def test_windows_memory_cap_is_installed_before_suspended_process_resumes(tmp_path, monkeypatch):
    original = supervisor.spawn_suspended
    observed = []

    def verify_limit_before_resume(argv, cwd, env, job, **kwargs):
        observed.append(job.memory_limit_mb())
        return original(argv, cwd, env, job, **kwargs)

    monkeypatch.setattr(supervisor, "spawn_suspended", verify_limit_before_resume)
    process = test_process.TestProcess.spawn(
        [sys.executable, "-c", "pass"], tmp_path, dict(os.environ),
        tmp_path / "runner-state", "job-7", memory_limit_mb=96
    )
    try:
        assert observed == [96]
        assert test_process.TestProcess.memory_limit_support()[0]
        assert process.memory_limit_enforced() is True
        assert _wait_exit(process) == 0
        process.terminate_and_prove(grace_s=0, kill_timeout_s=5)
        assert process.memory_limit_enforced() is True
        process.close()
        assert process.memory_limit_enforced() is True
    finally:
        if not process.closed:
            process.terminate_and_prove(grace_s=0, kill_timeout_s=5)
            process.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object memory limits")
def test_windows_enforced_cap_reports_configuration_not_peak_usage(tmp_path):
    process = test_process.TestProcess.spawn(
        [sys.executable, "-c", "pass"], tmp_path, dict(os.environ),
        tmp_path / "runner-state", "job-8", memory_limit_mb=96
    )
    try:
        assert process.memory_limit_enforced() is True
        assert _wait_exit(process) == 0
        process.terminate_and_prove(grace_s=0, kill_timeout_s=5)
        assert process.memory_limit_enforced() is True
        process.close()
        assert process.memory_limit_enforced() is True
    finally:
        if not process.closed:
            process.terminate_and_prove(grace_s=0, kill_timeout_s=5)
            process.close()
