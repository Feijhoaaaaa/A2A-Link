import os
import hashlib
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from mcp_server.supervisor import (DurableJournal, SupervisorLock, WindowsJob, process_identity,
                                   recover_termination, spawn_suspended, start_job_keeper,
                                   SupervisorRuntime)


def test_journal_is_append_only_and_fsynced_records_have_epoch(tmp_path):
    journal = DurableJournal(tmp_path / "journal.jsonl", owner_epoch=3)
    journal.append("spawn_intent", attempt_id="a1", message_id="m1")
    journal.append("spawned", attempt_id="a1", process_identity={"pid": 42})

    records = journal.read()
    assert [row["event"] for row in records] == ["spawn_intent", "spawned"]
    assert all(row["owner_epoch"] == 3 for row in records)
    assert records[0]["attempt_id"] == "a1"


def test_supervisor_lock_excludes_another_lock_instance(tmp_path):
    first = SupervisorLock(tmp_path / "supervisor.lock")
    second = SupervisorLock(tmp_path / "supervisor.lock")

    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_runtime_journals_spawn_captures_handle_and_recovers_tree(tmp_path):
    runtime = SupervisorRuntime(tmp_path / "supervisor")
    assert runtime.acquire()
    assert runtime.owner.startswith("supervisor:")
    code = "import json,time; print(json.dumps({'type':'thread.started','thread_id':'t-1'}),flush=True); time.sleep(30)"
    proc, identity, job, keeper = runtime.spawn("a1", "m1", [sys.executable, "-c", code], str(tmp_path))
    try:
        capture = None
        deadline = time.monotonic() + 5
        while capture is None and time.monotonic() < deadline:
            capture = runtime.capture_session("a1", "codex", 1, str(tmp_path), {"model": "gpt-6-luna"})
            time.sleep(0.02)
        assert capture and capture["session_id"] == "t-1"
        assert runtime.journal.read()[-1]["event"] == "session_started"
        runtime.handle_saved("a1")
        assert runtime.recover("a1", identity, grace_s=0) == "terminated"
        assert runtime.journal.read()[-1]["event"] == "termination_done"
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            if job is not None:
                job.terminate()
                job.wait_empty(5)
            else:
                proc.kill()
                proc.wait(timeout=5)
        if job is not None:
            job.close()
        if keeper is not None:
            keeper.release()
        runtime.release()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object specific")
def test_named_job_reopens_and_proves_entire_process_tree(tmp_path):
    name = f"Local\\a2a-link-test-{uuid.uuid4().hex}"
    heartbeat = tmp_path / "alive.log"
    state = tmp_path / "state.json"
    grandchild = tmp_path / "grandchild.py"
    child = tmp_path / "child.py"
    parent = tmp_path / "parent.py"
    grandchild.write_text(
        "import time\nfrom pathlib import Path\np = Path(" + repr(str(heartbeat)) + ")\n"
        "while True:\n    p.open('a').write('x\\n')\n    time.sleep(0.1)\n"
    )
    child.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, " + repr(str(grandchild)) + "])\n"
        "time.sleep(0.2)\n"
    )
    parent.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, " + repr(str(child)) + "])\n"
    )
    owner = tmp_path / "owner.py"
    owner.write_text(
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "from mcp_server.supervisor import WindowsJob, spawn_suspended, start_job_keeper\n"
        f"name = {name!r}; root = Path({str(tmp_path)!r})\n"
        "job = WindowsJob.create(name)\n"
        "keeper = start_job_keeper(name, root)\n"
        f"proc, identity = spawn_suspended([sys.executable, {str(parent)!r}], str(root), os.environ.copy(), job)\n"
        "proc.wait(timeout=5)\n"
        f"Path({str(state)!r}).write_text(json.dumps(dict(identity=identity, leader_exit_code=proc.returncode)), encoding='utf-8')\n"
        "while not (root / 'owner.stop').exists(): time.sleep(0.05)\n"
        "job.close(); keeper.release()\n"
    )
    owner_proc = subprocess.Popen([sys.executable, str(owner)], cwd=tmp_path,
                                  env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2]) + os.pathsep + os.environ.get("PYTHONPATH", "")},
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while not state.exists() and time.monotonic() < deadline and owner_proc.poll() is None:
            time.sleep(0.02)
        assert state.exists(), "supervisor did not record the spawned job"
        import json
        recorded = json.loads(state.read_text(encoding="utf-8"))
        identity = recorded["identity"]
        assert recorded["leader_exit_code"] == 0  # CLI leader is gone; grandchild still owns the Job.
        assert identity["group"] == name
        job = WindowsJob.open(name)
        deadline = time.monotonic() + 5
        while job.active_processes() < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert job.active_processes() >= 1
        deadline = time.monotonic() + 5
        while not heartbeat.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert heartbeat.exists()

        owner_proc.kill()
        owner_proc.wait(timeout=5)
        # MCP supervisor is dead; keeper still owns the named Job Object handle.
        time.sleep(0.3)
        assert heartbeat.stat().st_size > 0
        job.close()
        reopened = WindowsJob.open(name)
        try:
            assert reopened.active_processes() >= 3
            reopened.terminate()
            assert reopened.wait_empty(10)
        finally:
            reopened.close()
        size = heartbeat.stat().st_size
        time.sleep(0.3)
        assert heartbeat.stat().st_size == size
    finally:
        owner_proc.kill() if owner_proc.poll() is None else None
        owner_proc.wait(timeout=5)
        try:
            cleanup_job = WindowsJob.open(name)
            cleanup_job.terminate()
            cleanup_job.wait_empty(5)
            cleanup_job.close()
        except OSError:
            pass
        (tmp_path / "supervisor-keepers" / (hashlib.sha256(name.encode("utf-8")).hexdigest() + ".release")).write_text("release\n", encoding="ascii")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object specific")
def test_keeper_refuses_release_until_every_job_process_exits(tmp_path):
    name = f"Local\\a2a-link-release-{uuid.uuid4().hex}"
    job = WindowsJob.create(name)
    keeper = start_job_keeper(name, tmp_path)
    proc = None
    try:
        proc, _ = spawn_suspended([sys.executable, "-c", "import time; time.sleep(30)"],
                                  str(tmp_path), os.environ.copy(), job)
        with pytest.raises(RuntimeError, match="active process"):
            keeper.release()
        assert not keeper.release_path.exists()
        job.terminate()
        assert job.wait_empty(5)
        keeper.release()
        proc.wait(timeout=5)
        assert keeper.process.poll() is not None
    finally:
        if job.active_processes():
            job.terminate()
            job.wait_empty(5)
        job.close()
        if keeper.process.poll() is None:
            keeper.release()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows identities use a named Job Object")
def test_posix_process_identity_contains_group_and_os_start_time(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"], cwd=tmp_path,
                            start_new_session=True)
    try:
        identity = process_identity(proc.pid, str(proc.pid))
        assert identity["pid"] == proc.pid
        assert identity["group"] == str(proc.pid)
        assert identity["start_time"]
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process group specific")
def test_recovery_terminates_and_proves_posix_process_group_empty(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); time.sleep(30)"],
                            cwd=tmp_path, start_new_session=True)
    identity = process_identity(proc.pid, str(proc.pid))
    journal = DurableJournal(tmp_path / "journal.jsonl")
    method = recover_termination(identity, grace_s=0, kill_timeout_s=5, journal=journal,
                                 attempt_id="attempt-1")
    assert method == "terminated"
    assert journal.read()[-1]["event"] == "termination_done"
    assert journal.read()[-1]["method"] == "terminated"
    assert subprocess.run(["kill", "-0", f"-{identity['group']}"], check=False).returncode != 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process identity specific")
def test_recovery_never_signals_reused_pid_with_live_other_group(tmp_path, monkeypatch):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path,
                            start_new_session=True)
    identity = process_identity(proc.pid, str(proc.pid))
    identity["start_time"] = "1900-01-01T00:00:00+00:00"
    with pytest.raises(RuntimeError, match="identity changed"):
        recover_termination(identity)
    assert proc.poll() is None
    proc.terminate()
    proc.wait(timeout=5)
