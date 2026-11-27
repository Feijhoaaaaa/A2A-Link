"""Task 5: all-core parallelism and memory policy for the background test runner.

Contract tests for test-runner MCP resources ("Task 5 resource
interface"). The process adapter is faked; only the runner side is exercised here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_server import test_runner as tr
from tests.mcp_server.test_test_runner import FakeBalancer, FakeClock, FakeProcess, settings

PARALLEL_VARS = ("CMAKE_BUILD_PARALLEL_LEVEL", "CARGO_BUILD_JOBS", "OMP_NUM_THREADS")
REPO = Path(__file__).resolve().parents[2]


RESOURCE_KEYS = {"parallelism", "parallel_args_applied", "memory_limit_mb",
                 "memory_limit_enforced"}


class ResourceProcess(FakeProcess):
    """Fake adapter that also implements the Task 5 memory-limit surface."""

    def __init__(self, balancer=None, *, support=(True, "job object"), spawn_error=None, **kw):
        super().__init__(balancer, **kw)
        self.support = support
        self.spawn_error = spawn_error
        self.support_calls = 0
        self.spawn_kwargs: dict | None = None

    def memory_limit_support(self):
        self.support_calls += 1
        if self.balancer is not None:
            self.balancer.events.append(("memory_probe",))
        return self.support

    def factory(self, argv, cwd, env, state_dir, job_id, **kw):
        self.spawn_kwargs = dict(kw)
        if self.spawn_error is not None:
            raise self.spawn_error
        return super().factory(argv, cwd, env, state_dir, job_id)


def wire(body: dict) -> dict:
    """The resources object exactly as it crosses the wire (JSON round trip)."""
    resources = json.loads(json.dumps(body))["resources"]
    assert set(resources) == RESOURCE_KEYS  # strict: no extra or missing keys
    return resources


def run_job(bal, proc, tmp_path, *, cpu_count=8, base_env=None, **kw):
    clock = FakeClock()
    client = bal.client()
    job = tr.TestJob.from_claim(client.claim())
    return tr.run_test_job(job, tmp_path / "state", cpu_count, client=client,
                           process_factory=proc.factory, settings=settings(**kw), clock=clock,
                           sleep=clock.sleep,
                           base_env=base_env if base_env is not None else {"PATH": "/bin"})


def profile(**fields) -> dict:
    return {"argv": ["python", "-m", "pytest"], **fields}


# --- resource_environment -------------------------------------------------------------

def test_auto_resolves_to_detected_logical_cpus():
    env, suffix = tr.resource_environment(8, profile())
    assert env == {"CMAKE_BUILD_PARALLEL_LEVEL": "8", "MAKEFLAGS": "-j8",
                   "CARGO_BUILD_JOBS": "8", "OMP_NUM_THREADS": "8"}
    assert suffix == []
    env, _ = tr.resource_environment(8, profile(parallelism="auto"))
    assert all(env[name] == "8" for name in PARALLEL_VARS) and env["MAKEFLAGS"] == "-j8"


def test_explicit_parallelism_is_used_as_given():
    env, suffix = tr.resource_environment(8, profile(parallelism=3))
    assert all(env[name] == "3" for name in PARALLEL_VARS) and env["MAKEFLAGS"] == "-j3"
    assert suffix == []
    # explicit values are not clamped to the CPU count
    env, _ = tr.resource_environment(2, profile(parallelism=16))
    assert env["OMP_NUM_THREADS"] == "16"


def test_explicit_suffix_substitution_is_exact():
    _, suffix = tr.resource_environment(8, profile(parallelism_args=["-n", "{jobs}"]))
    assert suffix == ["-n", "8"]
    _, suffix = tr.resource_environment(8, profile(parallelism=3,
                                                   parallelism_args=["--jobs={jobs}", "-v"]))
    assert suffix == ["--jobs=3", "-v"]
    _, suffix = tr.resource_environment(4, profile(parallelism_args=["{jobs}{jobs}"]))
    assert suffix == ["44"]


@pytest.mark.parametrize("argv", [["python", "-m", "pytest"], ["cargo", "test"],
                                  ["cmake", "--build", "build"], ["make", "check"],
                                  ["ctest"], ["./run-tests.sh"]])
def test_no_flags_are_guessed_from_the_command(argv):
    _, suffix = tr.resource_environment(8, {"argv": argv})
    assert suffix == []


def test_resource_environment_accepts_a_parsed_profile():
    parsed = tr.TestProfile.parse(profile(parallelism=5, parallelism_args=["-n", "{jobs}"]))
    env, suffix = tr.resource_environment(12, parsed)
    assert env["CARGO_BUILD_JOBS"] == "5" and suffix == ["-n", "5"]


@pytest.mark.parametrize("cpu_count", [0, -1, True, 2.5, None])
def test_resource_environment_rejects_invalid_cpu_count(cpu_count):
    with pytest.raises(ValueError):
        tr.resource_environment(cpu_count, profile())


def test_resource_environment_rejects_invalid_profile():
    with pytest.raises(tr.ProfileError):
        tr.resource_environment(8, profile(parallelism=0))


# --- profile validation ------------------------------------------------------------------

def test_profile_defaults_are_auto_no_args_and_no_memory_cap():
    parsed = tr.TestProfile.parse(profile())
    assert parsed.parallelism == "auto"
    assert parsed.parallelism_args == ()
    assert parsed.memory_limit_mb is None


def test_profile_accepts_resource_fields():
    parsed = tr.TestProfile.parse(profile(parallelism=2, parallelism_args=["-n", "{jobs}"],
                                          memory_limit_mb=2048))
    assert parsed.parallelism == 2
    assert parsed.parallelism_args == ("-n", "{jobs}")
    assert parsed.memory_limit_mb == 2048


@pytest.mark.parametrize("fields", [
    {"parallelism": 0}, {"parallelism": -2}, {"parallelism": True}, {"parallelism": 1.5},
    {"parallelism": "8"}, {"parallelism": "max"}, {"parallelism": None},
    {"parallelism_args": "-n {jobs}"}, {"parallelism_args": ["-n", 4]},
    {"parallelism_args": ["a\x00b"]}, {"parallelism_args": ["-n", "{job}"]},
    {"parallelism_args": ["--workers={cpus}"]}, {"parallelism_args": ["x"] * 1024},
    {"memory_limit_mb": 0}, {"memory_limit_mb": -1}, {"memory_limit_mb": True},
    {"memory_limit_mb": 1.5}, {"memory_limit_mb": "512"},
])
def test_profile_rejects_invalid_resource_fields(fields):
    with pytest.raises(tr.ProfileError):
        tr.TestProfile.parse(profile(**fields))


def test_explicit_null_memory_limit_means_uncapped():
    assert tr.TestProfile.parse(profile(memory_limit_mb=None)).memory_limit_mb is None


# --- effective spawn and reported result ---------------------------------------------------

def test_spawn_gets_resource_env_and_explicit_suffix(tmp_path):
    bal = FakeBalancer(profile=profile(parallelism_args=["-n", "{jobs}"]))
    proc = ResourceProcess(bal)
    result = run_job(bal, proc, tmp_path, cpu_count=8)
    assert result.status == "succeeded"
    assert proc.spawned["argv"] == ["python", "-m", "pytest", "-n", "8"]
    env = proc.spawned["env"]
    assert all(env[name] == "8" for name in PARALLEL_VARS) and env["MAKEFLAGS"] == "-j8"


def test_unknown_command_argv_is_unchanged(tmp_path):
    bal = FakeBalancer(profile={"argv": ["./run-tests.sh", "--all"], "parallelism": 3})
    proc = ResourceProcess(bal)
    run_job(bal, proc, tmp_path)
    assert proc.spawned["argv"] == ["./run-tests.sh", "--all"]
    assert proc.spawned["env"]["CMAKE_BUILD_PARALLEL_LEVEL"] == "3"


def test_resource_values_override_inherited_and_profile_env(tmp_path):
    bal = FakeBalancer(profile=profile(parallelism=3, env={"OMP_NUM_THREADS": "1",
                                                           "MAKEFLAGS": "-j99", "FOO": "bar"}))
    proc = ResourceProcess(bal)
    result = run_job(bal, proc, tmp_path,
                     base_env={"PATH": "/bin", "CARGO_BUILD_JOBS": "64", "MAKEFLAGS": "-j64"})
    env = proc.spawned["env"]
    assert env["OMP_NUM_THREADS"] == "3" and env["MAKEFLAGS"] == "-j3"
    assert env["CARGO_BUILD_JOBS"] == "3" and env["FOO"] == "bar"
    assert bal.result_bodies[0]["resources"]["parallelism"] == 3
    assert result.status == "succeeded"


def test_result_reports_effective_resources_without_cap(tmp_path):
    bal = FakeBalancer(profile=profile(parallelism_args=["-n", "{jobs}"]))
    proc = ResourceProcess(bal)
    result = run_job(bal, proc, tmp_path, cpu_count=6)
    expected = {"parallelism": 6, "parallel_args_applied": ["-n", "6"],
                "memory_limit_mb": None, "memory_limit_enforced": None}
    assert bal.result_bodies[0]["resources"] == expected
    assert result.payload("t")["resources"] == expected
    # Nested nulls are kept, not dropped, when serialized.
    assert wire(bal.result_bodies[0]) == expected


def test_parallel_args_applied_are_the_exact_substituted_tokens(tmp_path):
    bal = FakeBalancer(profile=profile(parallelism=3,
                                       parallelism_args=["--jobs={jobs}", "-v", "{jobs}{jobs}"]))
    proc = ResourceProcess(bal)
    run_job(bal, proc, tmp_path)
    applied = wire(bal.result_bodies[0])["parallel_args_applied"]
    assert applied == ["--jobs=3", "-v", "33"]
    assert proc.spawned["argv"][-3:] == applied
    assert all(isinstance(token, str) for token in applied)


def test_no_memory_cap_by_default(tmp_path):
    bal = FakeBalancer(profile=profile())
    proc = ResourceProcess(bal, support=(False, "unsupported here"))
    result = run_job(bal, proc, tmp_path)
    assert result.status == "succeeded"
    assert "memory_limit_mb" not in proc.spawn_kwargs  # adapter default stays unlimited
    assert proc.support_calls == 0  # no probe is needed without a cap
    resources = wire(bal.result_bodies[0])
    assert resources["memory_limit_mb"] is None
    assert resources["memory_limit_enforced"] is None  # null: no cap was requested


def test_uncapped_profile_works_with_a_task4_adapter(tmp_path):
    # An adapter without the memory surface still runs uncapped jobs unchanged.
    bal = FakeBalancer(profile=profile())
    proc = FakeProcess(bal)
    assert run_job(bal, proc, tmp_path).status == "succeeded"
    assert wire(bal.result_bodies[0])["memory_limit_enforced"] is None


def test_supported_cap_is_passed_to_spawn_and_reported_enforced(tmp_path):
    bal = FakeBalancer(profile=profile(memory_limit_mb=1024))
    proc = ResourceProcess(bal)
    result = run_job(bal, proc, tmp_path)
    assert result.status == "succeeded"
    assert proc.spawn_kwargs == {"memory_limit_mb": 1024}
    kinds = bal.kinds()
    assert kinds.index("memory_probe") < kinds.index("snapshot") < kinds.index("spawn")
    assert wire(bal.result_bodies[0]) == {
        "parallelism": 8, "parallel_args_applied": [], "memory_limit_mb": 1024,
        "memory_limit_enforced": True}


def test_unsupported_cap_is_rejected_before_download_and_reported_unenforced(tmp_path):
    bal = FakeBalancer(profile=profile(memory_limit_mb=512, parallelism_args=["-n", "{jobs}"]))
    proc = ResourceProcess(bal, support=(False, "Linux whole-tree memory caps need cgroups"))
    result = run_job(bal, proc, tmp_path)
    assert result.status == "failed"
    assert "memory_limit_mb" in result.error and "cgroups" in result.error
    assert proc.spawned is None and "snapshot" not in bal.kinds()
    assert bal.kinds().count("result") == 1
    assert wire(bal.result_bodies[0]) == {
        "parallelism": 8, "parallel_args_applied": ["-n", "8"], "memory_limit_mb": 512,
        "memory_limit_enforced": False}


def test_cap_is_rejected_when_adapter_cannot_report_support(tmp_path):
    bal = FakeBalancer(profile=profile(memory_limit_mb=512))
    proc = FakeProcess(bal)  # Task 4 adapter: no memory_limit_support
    result = run_job(bal, proc, tmp_path)
    assert result.status == "failed" and "memory_limit_mb" in result.error
    assert proc.spawned is None and "snapshot" not in bal.kinds()
    assert wire(bal.result_bodies[0])["memory_limit_enforced"] is False


def test_memory_probe_failure_is_rejected_before_download(tmp_path):
    bal = FakeBalancer(profile=profile(memory_limit_mb=512))
    proc = ResourceProcess(bal)

    def broken():
        raise OSError("probe failed")
    proc.memory_limit_support = broken
    result = run_job(bal, proc, tmp_path)
    assert result.status == "failed" and "memory_limit_mb" in result.error
    assert proc.spawned is None and "snapshot" not in bal.kinds()
    assert wire(bal.result_bodies[0])["memory_limit_enforced"] is False


def test_cap_rejected_at_spawn_is_reported_unenforced(tmp_path):
    # The adapter refuses to start when it cannot install the cap on the whole tree.
    bal = FakeBalancer(profile=profile(memory_limit_mb=512))
    proc = ResourceProcess(bal, spawn_error=OSError("AssignProcessToJobObject failed"))
    result = run_job(bal, proc, tmp_path)
    assert result.status == "failed" and "could not start" in result.error
    assert wire(bal.result_bodies[0]) == {
        "parallelism": 8, "parallel_args_applied": [], "memory_limit_mb": 512,
        "memory_limit_enforced": False}


def test_uncapped_spawn_failure_reports_null_enforcement(tmp_path):
    bal = FakeBalancer(profile=profile())
    proc = ResourceProcess(bal, spawn_error=FileNotFoundError("no such exe"))
    result = run_job(bal, proc, tmp_path)
    assert result.status == "failed"
    assert wire(bal.result_bodies[0])["memory_limit_enforced"] is None


@pytest.mark.parametrize("exit_code,status", [(0, "succeeded"), (1, "failed")])
def test_capped_run_never_reports_or_infers_a_limit_hit(tmp_path, exit_code, status):
    # Peak usage does not prove the cap was hit: the exit code alone decides the status.
    bal = FakeBalancer(profile=profile(memory_limit_mb=256))
    proc = ResourceProcess(bal, exit_code=exit_code)
    proc.memory_limit_hit = lambda: True  # a legacy adapter probe must be ignored
    result = run_job(bal, proc, tmp_path)
    assert result.status == status and result.error is None
    body = bal.result_bodies[0]
    assert "memory_limit_hit" not in body["resources"]
    assert wire(body)["memory_limit_enforced"] is True


def test_capped_timeout_still_reports_enforcement(tmp_path):
    bal = FakeBalancer(profile=profile(memory_limit_mb=256, timeout_s=2))
    proc = ResourceProcess(bal, exit_code=None)
    result = run_job(bal, proc, tmp_path)
    assert result.status == "failed" and result.timed_out
    assert wire(bal.result_bodies[0])["memory_limit_enforced"] is True


def test_results_before_resource_resolution_omit_resources(tmp_path):
    bal = FakeBalancer(profile=profile(parallelism=0))  # invalid: never resolved
    run_job(bal, ResourceProcess(bal), tmp_path)
    assert "resources" not in bal.result_bodies[0]
    assert tr.TestResult("failed", error="x").payload("t").keys() == {
        "lease_token", "status", "timed_out", "stdout_tail", "stderr_tail", "error"}


# --- example configuration ----------------------------------------------------------------

def test_example_profiles_parse_as_runner_profiles():
    data = json.loads((REPO / "config" / "test-profiles.example.json").read_text("utf-8"))
    profiles = data["profiles"]
    assert profiles
    for name, raw in profiles.items():
        parsed = tr.TestProfile.parse(raw)
        env, suffix = tr.resource_environment(8, parsed)
        assert env["OMP_NUM_THREADS"] in ("8", str(parsed.parallelism)), name
        assert all("{" not in arg for arg in suffix), name
    assert any(raw.get("parallelism_args") for raw in profiles.values())
    assert all(raw.get("memory_limit_mb") is None for raw in profiles.values())


def test_example_pytest_profiles_match_installed_test_extra():
    import tomllib

    profiles = json.loads(
        (REPO / "config" / "test-profiles.example.json").read_text("utf-8"))["profiles"]
    serial = profiles["python-pytest"]
    xdist = profiles["python-pytest-xdist"]
    assert serial["argv"] == xdist["argv"] == ["python", "-m", "pytest", "-q"]
    # Plain pytest stays serial: no suffix is inferred from a pytest argv.
    assert "parallelism_args" not in serial
    assert tr.resource_environment(8, serial)[1] == []
    assert tr.resource_environment(8, xdist)[1] == ["-n", "8"]
    # `-n` needs pytest-xdist, so the documented `[test]` install must provide it.
    extra = tomllib.loads((REPO / "pyproject.toml").read_text("utf-8"))[
        "project"]["optional-dependencies"]["test"]
    assert "pytest" in extra and "pytest-xdist" in extra
