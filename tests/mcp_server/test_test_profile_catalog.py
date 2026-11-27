import json
import sys

import pytest

from mcp_server import test_runner as tr


def write_catalog(path, profiles):
    path.write_text(json.dumps({"profiles": profiles}), encoding="utf-8")
    return path


def test_profile_catalog_resolves_configured_name_to_validated_profile(tmp_path):
    path = write_catalog(tmp_path / "profiles.json", {
        "python-pytest": {"argv": ["python", "-m", "pytest", "-q"],
                          "parallelism": 3, "parallelism_args": ["-n", "{jobs}"]},
    })

    catalog = tr.TestProfileCatalog.load(path)
    resolved = catalog.resolve("python-pytest")

    assert resolved.argv == ("python", "-m", "pytest", "-q")
    assert resolved.parallelism == 3
    assert resolved.parallelism_args == ("-n", "{jobs}")


def test_each_runner_resolves_the_same_name_from_its_local_catalog(tmp_path):
    windows = tr.TestProfileCatalog.load(write_catalog(tmp_path / "windows.json", {
        "python-pytest": {"argv": ["py", "-3", "-m", "pytest"]},
    }))
    linux = tr.TestProfileCatalog.load(write_catalog(tmp_path / "linux.json", {
        "python-pytest": {"argv": ["python3", "-m", "pytest"]},
    }))

    assert windows.resolve("python-pytest").argv == ("py", "-3", "-m", "pytest")
    assert linux.resolve("python-pytest").argv == ("python3", "-m", "pytest")


def test_catalog_rejects_unknown_names_and_profile_objects(tmp_path):
    catalog = tr.TestProfileCatalog.load(write_catalog(tmp_path / "profiles.json", {
        "known": {"argv": ["python", "-m", "pytest"]},
    }))

    with pytest.raises(tr.ProfileError, match="unknown test profile"):
        catalog.resolve("unknown")
    with pytest.raises(tr.ProfileError, match="profile name"):
        catalog.resolve({"argv": ["arbitrary", "command"]})


def test_unknown_profile_is_reported_without_download_or_process_spawn(tmp_path):
    catalog = tr.TestProfileCatalog.load(write_catalog(tmp_path / "profiles.json", {
        "known": {"argv": [sys.executable, "-c", "pass"]},
    }))

    class Client:
        def __init__(self):
            self.results = []
            self.downloads = 0

        def claim(self):
            return {"job_id": "job1", "request_id": "req1", "target": "linux",
                    "state": "preparing", "profile": "unknown",
                    "snapshot_sha256": "a" * 64, "lease_token": "runner.token"}

        def heartbeat(self, *_args, **_kwargs):
            return {"cancel_requested": False}

        def download_snapshot(self, *_args, **_kwargs):
            self.downloads += 1
            raise AssertionError("unknown profiles must be rejected before download")

        def post_result(self, _job, result, **_kwargs):
            self.results.append(result)

    client = Client()
    runner = tr.Runner(client, tmp_path / "state", profile_catalog=catalog,
                       process_factory=lambda *_args, **_kwargs: pytest.fail("must not spawn"),
                       settings=tr.RunnerSettings(poll_interval_s=0))

    assert runner.run(max_jobs=1) == tr.EXIT_OK

    assert len(client.results) == 1
    assert client.results[0]["status"] == "failed"
    assert "unknown test profile" in client.results[0]["error"]
    assert client.downloads == 0
