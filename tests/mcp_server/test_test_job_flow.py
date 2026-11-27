from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import sys
import tarfile
from contextlib import closing
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from balancer.app import create_app
from mcp_server import test_runner as runner_module


ADMIN = "flow-admin"
RUNNER_ID = "flow-runner"


def _tar_bytes() -> bytes:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        info = tarfile.TarInfo("README.md")
        info.size = len(b"flow fixture\n")
        tar.addfile(info, io.BytesIO(b"flow fixture\n"))
    return archive.getvalue()


class _BytesStream(httpx.SyncByteStream):
    def __init__(self, data: bytes):
        self.data = data

    def __iter__(self):
        yield self.data


def _dispatch(api: TestClient, request: httpx.Request) -> httpx.Response:
    headers = {key: value for key, value in request.headers.items()
               if key.lower() not in ("host", "content-length", "connection")}
    response = api.request(request.method, request.url.raw_path.decode("ascii"),
                           headers=headers, content=request.read())
    return httpx.Response(response.status_code, headers=dict(response.headers),
                          stream=_BytesStream(response.content), request=request)


def _create_request(api: TestClient, request_id: str, archive: bytes, argv: list[str]) -> None:
    sha = hashlib.sha256(archive).hexdigest()
    upload = api.put(f"/test-snapshots/{sha}", content=archive)
    assert upload.status_code in (200, 201), upload.text
    created = api.post("/test-requests", json={
        "request_id": request_id,
        "snapshot_sha256": sha,
        "profile": "flow-test",
        "targets": ["linux"],
        "session_ref": None,
    })
    assert created.status_code == 201, created.text


def _profile_catalog(tmp_path: Path, argv: list[str]) -> runner_module.TestProfileCatalog:
    path = tmp_path / "test-profiles.json"
    path.write_text(json.dumps({"profiles": {
        "flow-test": {"argv": argv, "timeout_s": 20},
    }}), encoding="utf-8")
    return runner_module.TestProfileCatalog.load(path)


def _registered_runner(api: TestClient, token_file: str | None = None):
    response = api.post("/runners/register", headers={"Authorization": f"Bearer {ADMIN}"},
                        json={"runner_id": RUNNER_ID, "labels": ["linux"],
                              "platform": "linux"})
    assert response.status_code == 201, response.text
    return response.json()["token"]


def _runner_client(api: TestClient, token: str) -> runner_module.RunnerClient:
    transport = httpx.MockTransport(lambda request: _dispatch(api, request))
    client = httpx.Client(transport=transport)
    return runner_module.RunnerClient("http://balancer.test", RUNNER_ID, token,
                                      http_client=client, lease_s=30)


@pytest.mark.parametrize(("exit_code", "status"), [(0, "succeeded"), (7, "failed")])
def test_real_runner_and_balancer_flow_records_process_result_and_log(
        tmp_path, monkeypatch, exit_code, status):
    monkeypatch.setenv("A2A_LINK_RUNNER_REGISTRATION_TOKEN", ADMIN)
    monkeypatch.setenv("A2A_LINK_HOME", str(tmp_path / "data"))
    db_path = str(tmp_path / "balancer.sqlite")
    archive = _tar_bytes()
    app = create_app(db_path)
    with TestClient(app, base_url="http://127.0.0.1") as api:
        token = _registered_runner(api)
        _create_request(api, "flow-request", archive,
                        [sys.executable, "-c", f"print('flow-output'); raise SystemExit({exit_code})"])
        client = _runner_client(api, token)
        try:
            runner = runner_module.Runner(
                client, tmp_path / "runner", cpu_count=2,
                profile_catalog=_profile_catalog(tmp_path, [
                    sys.executable, "-c", f"print('flow-output'); raise SystemExit({exit_code})"]),
                settings=runner_module.RunnerSettings(poll_interval_s=0.01,
                                                      heartbeat_interval_s=0.1,
                                                      keep_job_dirs=True),
                base_env={"PATH": os.environ.get("PATH", "")},
            )
            assert runner.run(max_jobs=1) == runner_module.EXIT_OK
        finally:
            client.close()

        request = api.get("/test-requests/flow-request").json()
        job = request["jobs"][0]
        assert request["state"] == status
        assert job["state"] == status
        assert job["result"]["exit_code"] == exit_code
        assert job["result"]["resources"]["memory_limit_enforced"] is None
        log = api.get(f"/test-requests/flow-request/log?job_id={job['job_id']}").json()
        assert "flow-output" in log["text"]


def test_disconnected_runner_restart_keeps_lost_slot_and_rejects_late_result(
        tmp_path, monkeypatch):
    monkeypatch.setenv("A2A_LINK_RUNNER_REGISTRATION_TOKEN", ADMIN)
    monkeypatch.setenv("A2A_LINK_HOME", str(tmp_path / "data"))
    db_path = str(tmp_path / "balancer.sqlite")
    archive = _tar_bytes()
    app = create_app(db_path)
    with TestClient(app, base_url="http://127.0.0.1") as api:
        token = _registered_runner(api)
        _create_request(api, "disconnected", archive, [sys.executable, "-c", "print('never-run')"])
        client = _runner_client(api, token)
        job = client.claim()
        assert job and job["state"] == "preparing"
        assert client.heartbeat(runner_module.TestJob.from_claim(job), "running")["state"] == "running"
        job_id = job["job_id"]
        client.close()

    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("UPDATE test_jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE job_id=?",
                     (job_id,))
        conn.commit()

    restarted = create_app(db_path)
    with TestClient(restarted, base_url="http://127.0.0.1") as api:
        detail = api.get("/test-requests/disconnected").json()
        assert detail["jobs"][0]["state"] == "lost"
        client = _runner_client(api, token)
        try:
            assert client.claim() is None
            with pytest.raises(runner_module.LeaseLost):
                client.post_result(runner_module.TestJob.from_claim(job),
                                   {"lease_token": job["lease_token"], "status": "succeeded",
                                    "exit_code": 0, "timed_out": False,
                                    "stdout_tail": "", "stderr_tail": ""})
        finally:
            client.close()
