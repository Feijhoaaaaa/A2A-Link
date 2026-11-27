"""Background test runner: claims test jobs from the balancer and executes them.

Contracts:
- the balancer's authenticated test-job API
- runner-local process and profile handling

Job flow: claim -> prepare (verified snapshot, strict profile) -> acknowledged
``phase=running`` heartbeat -> spawn -> bounded log heartbeats -> proven-empty
process scope -> complete logs -> one idempotent result.

Process supervision lives in ``mcp_server.test_process`` (owned separately); this
module only talks to it through the ``TestProcess`` protocol below. A runner that
loses its lease or cannot prove its process scope empty stops claiming work: the
balancer's ``lost`` state is authoritative and keeps the runner's slot.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import sys
import tarfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Literal, Protocol

import httpx

log = logging.getLogger("a2a_link.runner")

KiB = 1024
MiB = 1024 * KiB
GiB = 1024 * MiB

TOKEN_ENV = "A2A_LINK_RUNNER_TOKEN"
HOME_ENV = "A2A_LINK_RUNNER_HOME"
PROFILE_CONFIG_ENV = "A2A_LINK_TEST_PROFILE_CONFIG"
DATA_HOME_ENV = "A2A_LINK_HOME"
# Matches the API's limits (balancer/test_jobs_api.py).
API_LOG_CHUNK_MAX_BYTES = 256 * KiB
RESULT_TAIL_MAX_BYTES = 64 * KiB
RESULT_ERROR_MAX_BYTES = 4 * KiB
DEFAULT_SNAPSHOT_MAX_BYTES = GiB
DEFAULT_TIMEOUT_S = 3600.0
MAX_TIMEOUT_S = 7 * 24 * 3600.0
MAX_ARCHIVE_MEMBERS = 500_000
MAX_MEMBER_NAME_BYTES = 4096

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_HALTED = 3

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
RUNNER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$", re.IGNORECASE)

# HTTP statuses retried with the identical request (every runner call is idempotent).
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
# 409 codes meaning this runner no longer owns an active lease for the job.
LEASE_LOST_CODES = frozenset({"lease_lost", "lease_expired", "lease_inactive", "owner_mismatch"})

# Only these variables reach the test process; everything else (tokens, cloud
# credentials, agent settings) is dropped.
PASSTHROUGH_ENV = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
                   "LANG", "LC_ALL", "LC_CTYPE", "TZ", "NUMBER_OF_PROCESSORS",
                   "PROCESSOR_ARCHITECTURE", "OS")
RESERVED_ENV_PREFIX = "A2A_LINK_RUNNER"

PROFILE_KEYS = frozenset({"argv", "timeout_s", "env", "cwd", "parallelism", "parallelism_args",
                          "memory_limit_mb", "snapshot", "name", "description"})
MAX_ARGV = 1024
MAX_ARG_BYTES = 32 * KiB
MAX_MEMORY_LIMIT_MB = 16 * 1024 * 1024  # 16 TiB
# The only placeholder parallelism_args may use; any other {name} is a typo, not a literal.
JOBS_PLACEHOLDER = "{jobs}"
PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
# Exact key set of the result `resources` object.
RESOURCE_KEYS = ("parallelism", "parallel_args_applied", "memory_limit_mb",
                 "memory_limit_enforced")
# Generic build/runtime parallelism knobs; framework flags come only from parallelism_args.
PARALLEL_ENV = ("CMAKE_BUILD_PARALLEL_LEVEL", "MAKEFLAGS", "CARGO_BUILD_JOBS", "OMP_NUM_THREADS")


# --- errors -----------------------------------------------------------------------

class RunnerError(Exception):
    """Base class for runner failures."""


class TransientError(RunnerError):
    """The balancer could not be reached (or answered 5xx) within the retry window."""


class ApiRejected(RunnerError):
    def __init__(self, status: int, code: str, detail: str = ""):
        super().__init__(f"HTTP {status} {code}: {detail}".rstrip(": "))
        self.status, self.code, self.detail = status, code, detail


class AuthError(ApiRejected):
    """Runner credentials were refused; retrying cannot help."""


class LeaseLost(ApiRejected):
    """The balancer says this runner no longer holds the job's lease."""


class ProtocolError(RunnerError):
    """The balancer's answer contradicts the agreed protocol."""


class SnapshotError(RunnerError):
    """The snapshot failed verification or safe extraction."""


class ProfileError(RunnerError):
    """The resolved test profile is invalid or asks for something unsupported."""


class RunnerHalted(RunnerError):
    """The runner must stop claiming work (lost lease or unproven termination)."""


# --- typed job / result -------------------------------------------------------------

@dataclass(frozen=True)
class TestJob:
    __test__ = False  # not a pytest class

    job_id: str
    request_id: str
    target: Any
    profile: Any
    snapshot_sha256: str
    lease_token: str = field(repr=False)
    lease_expires_at: str | None = None

    @classmethod
    def from_claim(cls, data: Any) -> "TestJob":
        if not isinstance(data, dict):
            raise ProtocolError("claim returned a non-object job")
        job_id, sha = data.get("job_id"), data.get("snapshot_sha256")
        token = data.get("lease_token")
        if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
            raise ProtocolError("claim returned an invalid job_id")
        if not isinstance(sha, str) or not SHA256_RE.match(sha):
            raise ProtocolError("claim returned an invalid snapshot_sha256")
        if not isinstance(token, str) or not token or len(token) > 256:
            raise ProtocolError("claim returned an invalid lease_token")
        if data.get("state") not in (None, "preparing"):
            raise ProtocolError("claim returned a job that is not preparing")
        return cls(job_id=job_id, request_id=str(data.get("request_id", "")),
                   target=data.get("target"), profile=data.get("profile"),
                   snapshot_sha256=sha, lease_token=token,
                   lease_expires_at=data.get("lease_expires_at"))


@dataclass(frozen=True)
class TestResult:
    __test__ = False

    status: Literal["succeeded", "failed", "cancelled"]
    exit_code: int | None = None
    timed_out: bool = False
    duration_s: float | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    error: str | None = None
    # Effective resource policy, see resource_report(). Omitted when the profile was
    # never resolved (invalid profile, cancelled before start, snapshot failure).
    resources: dict[str, Any] | None = None

    def payload(self, lease_token: str) -> dict:
        """The exact JSON body; built once per job so retries are byte-identical."""
        body: dict[str, Any] = {"lease_token": lease_token, "status": self.status,
                                "timed_out": self.timed_out,
                                "stdout_tail": self.stdout_tail, "stderr_tail": self.stderr_tail}
        if self.exit_code is not None:
            body["exit_code"] = int(self.exit_code)
        if self.duration_s is not None:
            body["duration_s"] = float(round(self.duration_s, 3))
        if self.error is not None:
            body["error"] = self.error
        if self.resources is not None:
            # Strict key set; null values are part of the contract and are kept.
            body["resources"] = {key: self.resources[key] for key in RESOURCE_KEYS}
            body["resources"]["parallel_args_applied"] = list(
                self.resources["parallel_args_applied"])
        return body


@dataclass(frozen=True)
class TestProfile:
    __test__ = False

    argv: tuple[str, ...]
    timeout_s: float = DEFAULT_TIMEOUT_S
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    parallelism: str | int = "auto"
    parallelism_args: tuple[str, ...] = ()
    memory_limit_mb: int | None = None  # None: uncapped

    @classmethod
    def parse(cls, raw: Any) -> "TestProfile":
        if not isinstance(raw, dict):
            raise ProfileError("the job profile must be a resolved profile object, not a name")
        unknown = sorted(set(raw) - PROFILE_KEYS)
        if unknown:
            raise ProfileError(f"unsupported profile fields: {', '.join(unknown)}")
        argv = raw.get("argv")
        if (not isinstance(argv, list) or not 1 <= len(argv) <= MAX_ARGV
                or not all(_valid_arg(a) for a in argv)
                or not argv[0].strip()):
            raise ProfileError("profile argv must be a non-empty list of strings")
        timeout = raw.get("timeout_s", DEFAULT_TIMEOUT_S)
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not 0 < timeout <= MAX_TIMEOUT_S):
            raise ProfileError(f"profile timeout_s must be in (0, {int(MAX_TIMEOUT_S)}]")
        env = raw.get("env", {})
        if not isinstance(env, dict):
            raise ProfileError("profile env must be an object of strings")
        for name, value in env.items():
            if not isinstance(name, str) or not ENV_NAME_RE.match(name):
                raise ProfileError(f"invalid profile env name {name!r}")
            if name.upper().startswith(RESERVED_ENV_PREFIX):
                raise ProfileError(f"profile env may not set {name}")
            if not isinstance(value, str) or "\x00" in value:
                raise ProfileError(f"profile env {name} must be a string")
        cwd = raw.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str):
                raise ProfileError("profile cwd must be a relative path")
            _safe_relative_parts(cwd, ProfileError)
        parallelism = raw.get("parallelism", "auto")
        if parallelism != "auto" and (isinstance(parallelism, bool)
                                      or not isinstance(parallelism, int) or parallelism < 1):
            raise ProfileError('profile parallelism must be "auto" or a positive integer')
        parallelism_args = raw.get("parallelism_args", [])
        if (not isinstance(parallelism_args, list)
                or len(argv) + len(parallelism_args) > MAX_ARGV
                or not all(_valid_arg(a) for a in parallelism_args)):
            raise ProfileError("profile parallelism_args must be a list of strings")
        for arg in parallelism_args:
            unknown_placeholders = set(PLACEHOLDER_RE.findall(arg)) - {JOBS_PLACEHOLDER}
            if unknown_placeholders:
                raise ProfileError(f"profile parallelism_args supports only {JOBS_PLACEHOLDER}, "
                                   f"not {', '.join(sorted(unknown_placeholders))}")
        memory = raw.get("memory_limit_mb")
        if memory is not None and (isinstance(memory, bool) or not isinstance(memory, int)
                                   or not 1 <= memory <= MAX_MEMORY_LIMIT_MB):
            raise ProfileError(f"profile memory_limit_mb must be null or an integer in "
                               f"[1, {MAX_MEMORY_LIMIT_MB}]")
        return cls(argv=tuple(argv), timeout_s=float(timeout), env=dict(env), cwd=cwd,
                   parallelism=parallelism, parallelism_args=tuple(parallelism_args),
                   memory_limit_mb=memory)


@dataclass(frozen=True)
class TestProfileCatalog:
    """Runner-local allowlist mapping request profile names to validated commands."""

    profiles: dict[str, TestProfile]

    @classmethod
    def load(cls, path: str | Path) -> "TestProfileCatalog":
        source = Path(path)
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProfileError(f"cannot load test profile catalog {source}: {type(exc).__name__}") from None
        if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), dict):
            raise ProfileError("test profile catalog must contain a profiles object")
        unknown = set(raw) - {"profiles", "_comment", "_resources"}
        if unknown:
            raise ProfileError(f"unsupported test profile catalog fields: {', '.join(sorted(unknown))}")
        entries = raw["profiles"]
        if not entries:
            raise ProfileError("test profile catalog must define at least one profile")
        parsed: dict[str, TestProfile] = {}
        for name, value in entries.items():
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", name)):
                raise ProfileError(f"invalid test profile name {name!r}")
            if not isinstance(value, dict):
                raise ProfileError(f"test profile {name!r} must be an object")
            try:
                parsed[name] = TestProfile.parse(value)
            except ProfileError as exc:
                raise ProfileError(f"invalid test profile {name!r}: {exc}") from None
        return cls(parsed)

    def resolve(self, name: Any) -> TestProfile:
        if not isinstance(name, str) or not name.strip():
            raise ProfileError("job profile must be an allowlisted profile name")
        profile = self.profiles.get(name)
        if profile is None:
            raise ProfileError(f"unknown test profile {name!r}")
        return profile


def default_profile_catalog_path(environ: dict[str, str] | None = None,
                                 explicit_path: str | None = None) -> Path:
    """Resolve CLI override, env override, user config, then the checkout example."""
    environ = os.environ if environ is None else environ
    if explicit_path:
        return Path(explicit_path)
    if environ.get(PROFILE_CONFIG_ENV):
        return Path(environ[PROFILE_CONFIG_ENV])
    data_home = Path(environ.get(DATA_HOME_ENV, str(Path.home() / ".a2a_link")))
    user_catalog = data_home / "test-profiles.json"
    if user_catalog.is_file():
        return user_catalog
    return Path(__file__).resolve().parent.parent / "config" / "test-profiles.example.json"


def _valid_arg(arg: Any) -> bool:
    return isinstance(arg, str) and "\x00" not in arg and len(arg) <= MAX_ARG_BYTES


# --- process adapter protocol ------------------------------------------------------------

class TestProcess(Protocol):
    """Agreed adapter surface (implemented by mcp_server.test_process.TestProcess)."""

    def poll(self) -> int | None: ...

    def read_output(self, stdout_offset: int, stderr_offset: int,
                    limit: int) -> tuple[bytes, bytes, int, int]: ...

    def terminate_and_prove(self, grace_s: float,
                            kill_timeout_s: float) -> Literal["exited", "terminated"]: ...

    def close(self) -> None: ...


# spawn(argv, cwd, env, state_dir, job_id, *, memory_limit_mb=None) -> TestProcess.
# memory_limit_mb is passed only when the profile sets a cap, so an uncapped spawn is
# the unchanged Task 4 call.
ProcessFactory = Callable[..., TestProcess]


def default_process_factory() -> ProcessFactory:
    from mcp_server.test_process import TestProcess as Adapter  # owned by the Codex executor
    return Adapter.spawn


def memory_limit_support(factory: ProcessFactory) -> tuple[bool, str]:
    """Ask the factory's adapter whether it can cap the whole process tree.

    The adapter publishes ``memory_limit_support()`` next to ``spawn`` (on the factory
    itself or on the class/instance a bound ``spawn`` belongs to). An adapter that does
    not report support cannot enforce a cap, so it is treated as unsupported.
    """
    probe = getattr(factory, "memory_limit_support", None)
    if probe is None:
        probe = getattr(getattr(factory, "__self__", None), "memory_limit_support", None)
    if probe is None:
        return False, "the process adapter does not report memory limit support"
    try:
        supported, reason = probe()
    except Exception as e:
        return False, f"memory limit support probe failed: {type(e).__name__}: {e}"
    return bool(supported), str(reason)


# --- resource policy ----------------------------------------------------------------------

def resource_environment(cpu_count: int, profile: TestProfile | dict
                         ) -> tuple[dict[str, str], list[str]]:
    """Resolve the profile's parallelism into child environment values and argv suffix.

    ``"auto"`` means ``cpu_count`` (the runner's affinity-aware logical CPU count);
    explicit values are used as given. The suffix comes only from the profile's
    explicit ``parallelism_args`` with ``{jobs}`` substituted; nothing is inferred from
    the command, so a profile without args leaves argv unchanged.
    """
    if isinstance(cpu_count, bool) or not isinstance(cpu_count, int) or cpu_count < 1:
        raise ValueError("cpu_count must be a positive integer")
    if not isinstance(profile, TestProfile):
        profile = TestProfile.parse(profile)
    jobs = cpu_count if profile.parallelism == "auto" else int(profile.parallelism)
    n = str(jobs)
    env = {name: n for name in PARALLEL_ENV}
    env["MAKEFLAGS"] = f"-j{n}"
    suffix = [arg.replace(JOBS_PLACEHOLDER, n) for arg in profile.parallelism_args]
    return env, suffix


def resource_report(profile: TestProfile, resource_env: dict[str, str], suffix: list[str],
                    *, cap_installed: bool) -> dict[str, Any]:
    """The result ``resources`` object for a resolved profile.

    ``memory_limit_enforced`` is null when no cap was requested, true only when the
    requested cap was installed for the whole process tree, and false when it was
    requested but refused (unsupported, probe failure, or the adapter could not start
    with it). Whether a capped run actually reached its limit is not reported: peak
    usage alone does not establish that.
    """
    capped = profile.memory_limit_mb is not None
    return {"parallelism": int(resource_env["OMP_NUM_THREADS"]),
            "parallel_args_applied": list(suffix),
            "memory_limit_mb": profile.memory_limit_mb,
            "memory_limit_enforced": cap_installed if capped else None}


# --- HTTP client ----------------------------------------------------------------------------

class _Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__


@dataclass(frozen=True)
class RetryPolicy:
    window_s: float = 30.0
    initial_delay_s: float = 0.5
    max_delay_s: float = 5.0


def _error_body(response: httpx.Response) -> tuple[str, str]:
    try:
        body = response.json()
    except ValueError:
        return "http_error", ""
    if isinstance(body, dict):
        code = body.get("error")
        detail = body.get("detail")
        return (code if isinstance(code, str) else "http_error",
                detail if isinstance(detail, str) else "")
    return "http_error", ""


class RunnerClient:
    """Authenticated runner calls with idempotent retry of the identical request."""

    def __init__(self, base_url: str, runner_id: str, token: str,
                 http_client: httpx.Client | None = None, *, lease_s: int = 60,
                 retry: RetryPolicy = RetryPolicy(), timeout_s: float = 30.0,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        if not RUNNER_ID_RE.match(runner_id or ""):
            raise ValueError("invalid runner id")
        prefix, dot, secret = (token or "").partition(".")
        if not dot or not secret or prefix != runner_id:
            raise ValueError("the runner token does not belong to this runner id")
        self.base_url = base_url.rstrip("/")
        self.runner_id = runner_id
        self.lease_s = lease_s
        self.retry = retry
        self._token = _Secret(token)
        self._http = http_client or httpx.Client(timeout=timeout_s, follow_redirects=False)
        self._owns_http = http_client is None
        self._sleep, self._clock = sleep, clock
        self._heartbeat_lock = threading.Lock()  # heartbeats are serialized

    def __repr__(self) -> str:
        return f"RunnerClient({self.base_url!r}, runner_id={self.runner_id!r})"

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token.reveal()}", **(extra or {})}

    @staticmethod
    def _raise_for(response: httpx.Response) -> None:
        if response.is_success:
            return
        code, detail = _error_body(response)
        status = response.status_code
        if status in (401, 403):
            raise AuthError(status, code, detail)
        if status == 409 and code in LEASE_LOST_CODES:
            raise LeaseLost(status, code, detail)
        raise ApiRejected(status, code, detail)

    def _retrying(self, what: str, attempt: Callable[[], Any], window_s: float | None) -> Any:
        """Run ``attempt`` until it returns, raising TransientError after the window."""
        deadline = self._clock() + (self.retry.window_s if window_s is None else window_s)
        delay = self.retry.initial_delay_s
        while True:
            try:
                return attempt()
            except _Retry as e:
                reason = e.reason
            except httpx.TransportError as e:
                reason = type(e).__name__  # never the request (it carries the token header)
            if self._clock() + delay > deadline:
                raise TransientError(f"{what}: {reason}")
            log.debug("retrying %s after %s", what, reason)
            self._sleep(delay)
            delay = min(delay * 2, self.retry.max_delay_s)

    def _call(self, method: str, path: str, *, json: Any = None,
              window_s: float | None = None) -> Any:
        def attempt():
            response = self._http.request(method, self.base_url + path, json=json,
                                          headers=self._headers())
            if response.status_code in RETRYABLE_STATUS:
                raise _Retry(f"HTTP {response.status_code}")
            self._raise_for(response)
            try:
                return response.json()
            except ValueError:
                raise ProtocolError(f"{method} {path} returned non-JSON") from None
        return self._retrying(f"{method} {path}", attempt, window_s)

    def claim(self) -> dict | None:
        out = self._call("POST", "/runners/claim", json={"lease_s": self.lease_s})
        if not isinstance(out, dict) or "job" not in out:
            raise ProtocolError("claim returned an unexpected body")
        job = out["job"]
        if job is not None and not isinstance(job, dict):
            raise ProtocolError("claim returned a non-object job")
        return job

    def heartbeat(self, job: TestJob, phase: Literal["preparing", "running"],
                  log_offset: int | None = None, chunk: bytes | None = None, *,
                  window_s: float | None = None) -> dict:
        if (log_offset is None) != (chunk is None):
            raise ValueError("log_offset and chunk go together")
        body: dict[str, Any] = {"job_id": job.job_id, "lease_token": job.lease_token,
                                "phase": phase}
        if chunk is not None:
            if not 0 < len(chunk) <= API_LOG_CHUNK_MAX_BYTES:
                raise ValueError("log chunk size is outside the API bounds")
            body["log_offset"] = log_offset
            body["log_chunk"] = base64.b64encode(chunk).decode("ascii")
        with self._heartbeat_lock:
            out = self._call("POST", f"/runners/{self.runner_id}/heartbeat", json=body,
                             window_s=window_s)
        if (not isinstance(out, dict) or out.get("job_id") != job.job_id
                or type(out.get("next_log_offset")) is not int):
            raise ProtocolError("heartbeat returned an unexpected body")
        return out

    def download_snapshot(self, job: TestJob, destination: Path, *,
                          max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES) -> None:
        """Stream the job's snapshot to ``destination`` and verify its SHA-256."""
        destination = Path(destination)
        partial = destination.with_name(destination.name + ".part")
        path = f"/test-jobs/{job.job_id}/snapshot"

        def attempt():
            digest, written = hashlib.sha256(), 0
            with self._http.stream("GET", self.base_url + path,
                                   # raw bytes must be the stored tar for the digest
                                   headers=self._headers({"X-Lease-Token": job.lease_token,
                                                          "Accept-Encoding": "identity"})
                                   ) as response:
                if response.status_code in RETRYABLE_STATUS:
                    raise _Retry(f"HTTP {response.status_code}")
                if not response.is_success:
                    response.read()
                    self._raise_for(response)
                declared_sha = response.headers.get("x-snapshot-sha256", "")
                if declared_sha != job.snapshot_sha256:
                    raise SnapshotError("the served snapshot is not the claimed snapshot")
                declared = response.headers.get("content-length")
                if declared is not None and (not declared.isdigit() or int(declared) > max_bytes):
                    raise SnapshotError(f"snapshot exceeds {max_bytes} bytes")
                with open(partial, "wb") as f:
                    for block in response.iter_raw(1 * MiB):
                        written += len(block)
                        if written > max_bytes:
                            raise SnapshotError(f"snapshot exceeds {max_bytes} bytes")
                        digest.update(block)
                        f.write(block)
                    f.flush()
                    os.fsync(f.fileno())
            if declared is not None and int(declared) != written:
                raise _Retry("short snapshot body")
            if digest.hexdigest() != job.snapshot_sha256:
                raise SnapshotError("snapshot digest does not match the claimed SHA-256")
            os.replace(partial, destination)

        try:
            self._retrying(f"GET {path}", attempt, None)
        finally:
            partial.unlink(missing_ok=True)

    def post_result(self, job: TestJob, result: TestResult | dict, *,
                    window_s: float | None = None) -> dict:
        body = result.payload(job.lease_token) if isinstance(result, TestResult) else result
        out = self._call("POST", f"/test-jobs/{job.job_id}/result", json=body, window_s=window_s)
        if not isinstance(out, dict) or out.get("state") not in ("succeeded", "failed",
                                                                 "cancelled"):
            raise ProtocolError("result returned an unexpected body")
        return out


class _Retry(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --- snapshot extraction --------------------------------------------------------------------

def _safe_relative_parts(name: str, error: type[Exception] = SnapshotError) -> tuple[str, ...]:
    """Validate a portable project-relative path and return its components."""
    if not name or "\x00" in name or "\\" in name:
        raise error(f"unsafe path {name!r}")
    if os.name == "nt" and ":" in name:  # drive letters and NTFS alternate data streams
        raise error(f"unsafe path {name!r}")
    if len(name.encode("utf-8", "surrogateescape")) > MAX_MEMBER_NAME_BYTES:
        raise error("path is too long")
    if name.startswith("/"):
        raise error(f"absolute path {name!r}")
    parts = PurePosixPath(name).parts
    if any(p in ("", ".", "..") for p in name.rstrip("/").split("/")):
        raise error(f"path traversal in {name!r}")
    if os.name == "nt":
        for part in parts:
            if WINDOWS_RESERVED.match(part) or part.endswith((" ", ".")):
                raise error(f"path {name!r} is not portable to Windows")
    return parts


def _within(root: Path, target: Path) -> bool:
    try:
        return os.path.commonpath([str(root), str(target.resolve())]) == str(root)
    except ValueError:  # different drives
        return False


def extract_snapshot(archive: Path, destination: Path, *,
                     max_members: int = MAX_ARCHIVE_MEMBERS,
                     max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES) -> int:
    """Extract only regular files and directories under ``destination``.

    Links, devices, absolute paths, traversal, and anything that would land outside
    the destination fail the whole snapshot. Returns the number of files written.
    """
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise SnapshotError("the extraction directory is not empty")
    root = destination.resolve()
    files = total = 0
    try:
        with tarfile.open(archive, mode="r:") as tar:  # uncompressed tar only
            for count, member in enumerate(tar, 1):
                if count > max_members:
                    raise SnapshotError(f"snapshot has more than {max_members} members")
                parts = _safe_relative_parts(member.name)
                target = root.joinpath(*parts)
                if not _within(root, target):
                    raise SnapshotError(f"path {member.name!r} escapes the job directory")
                if member.type == tarfile.DIRTYPE:
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
                    raise SnapshotError(f"unsupported member type for {member.name!r}")
                total += member.size
                if total > max_bytes:
                    raise SnapshotError(f"snapshot expands beyond {max_bytes} bytes")
                target.parent.mkdir(parents=True, exist_ok=True)
                if not _within(root, target.parent):
                    raise SnapshotError(f"path {member.name!r} escapes the job directory")
                source = tar.extractfile(member)
                assert source is not None  # regular file
                with source, open(target, "xb") as out:
                    shutil.copyfileobj(source, out, 1 * MiB)
                if os.name != "nt":
                    os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
                files += 1
    except SnapshotError:
        raise
    except (tarfile.TarError, OSError) as e:
        raise SnapshotError(f"snapshot extraction failed: {type(e).__name__}: {e}") from None
    return files


# --- environment ---------------------------------------------------------------------------

def build_environment(profile: TestProfile, *, tmp_dir: Path, home_dir: Path, job_id: str,
                      base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Scrubbed child environment: an allowlist plus per-job temp/home and profile env."""
    source = os.environ if base_env is None else base_env
    upper = {k.upper(): v for k, v in source.items()}
    env = {name: upper[name] for name in PASSTHROUGH_ENV if name in upper}
    env.update({"TMP": str(tmp_dir), "TEMP": str(tmp_dir), "TMPDIR": str(tmp_dir),
                "HOME": str(home_dir), "USERPROFILE": str(home_dir),
                "A2A_LINK_TEST_JOB_ID": job_id})
    env.update(profile.env)
    return env


# --- output handling -------------------------------------------------------------------------

def bounded_text(data: bytes, limit: int = RESULT_TAIL_MAX_BYTES) -> str:
    """Decode an output tail to text whose UTF-8 encoding fits in ``limit`` bytes."""
    encoded = data[-limit:].decode("utf-8", "replace").encode("utf-8")
    if len(encoded) > limit:
        encoded = encoded[-limit:]
    return encoded.decode("utf-8", "ignore")


class _Tail:
    def __init__(self, limit: int = RESULT_TAIL_MAX_BYTES):
        self.limit, self.data = limit, bytearray()

    def add(self, chunk: bytes) -> None:
        self.data += chunk
        if len(self.data) > 2 * self.limit:
            del self.data[:-self.limit]

    def text(self) -> str:
        return bounded_text(bytes(self.data[-self.limit:]), self.limit)


class LogShipper:
    """Ships raw output as offset-addressed chunks with at most one chunk in flight.

    An unacknowledged chunk is resent with identical bytes and offset until the
    balancer acknowledges it, so retries after a lost response are idempotent.
    """

    def __init__(self, client: RunnerClient, job: TestJob, *, chunk_bytes: int,
                 buffer_bytes: int):
        if not 0 < chunk_bytes <= API_LOG_CHUNK_MAX_BYTES or buffer_bytes < chunk_bytes:
            raise ValueError("invalid log chunk/buffer sizes")
        self.client, self.job = client, job
        self.chunk_bytes, self.buffer_bytes = chunk_bytes, buffer_bytes
        self.offset = 0  # acknowledged by the balancer
        self.pending = bytearray()
        self.inflight: bytes | None = None
        self.truncated = False  # balancer stopped storing; stop shipping

    def capacity(self) -> int:
        if self.truncated:
            return self.buffer_bytes  # output is discarded, never buffered
        return max(0, self.buffer_bytes - len(self.pending))

    def feed(self, data: bytes) -> None:
        if data and not self.truncated:
            self.pending += data

    def has_full_chunk(self) -> bool:
        return self.inflight is not None or len(self.pending) >= self.chunk_bytes

    def drained(self) -> bool:
        return self.inflight is None and not self.pending

    def heartbeat(self, *, window_s: float | None = None) -> dict:
        if self.inflight is None and self.pending:
            self.inflight = bytes(self.pending[:self.chunk_bytes])
            del self.pending[:len(self.inflight)]
        if self.inflight is not None:
            out = self.client.heartbeat(self.job, "running", self.offset, self.inflight,
                                        window_s=window_s)
            expected = self.offset + len(self.inflight)
        else:
            out = self.client.heartbeat(self.job, "running", window_s=window_s)
            expected = self.offset
        if out.get("state") != "running":
            raise ProtocolError(f"heartbeat reported state {out.get('state')!r}")
        if out["next_log_offset"] != expected:
            raise ProtocolError("heartbeat acknowledged an unexpected log offset")
        self.offset, self.inflight = expected, None
        if out.get("log_truncated"):
            self.truncated = True
            self.pending.clear()
        return out


# --- orchestration ------------------------------------------------------------------------------

@dataclass(frozen=True)
class RunnerSettings:
    lease_s: int = 60
    heartbeat_interval_s: float | None = None  # default: lease_s / 4
    # No acknowledgement for this long means the balancer has expired the lease.
    presumed_lost_after_s: float | None = None  # default: lease_s
    poll_interval_s: float = 0.1
    idle_poll_s: float = 5.0
    min_chunk_interval_s: float = 0.25
    log_chunk_bytes: int = 64 * KiB
    log_buffer_bytes: int = 1 * MiB
    read_limit_bytes: int = 64 * KiB
    snapshot_max_bytes: int = DEFAULT_SNAPSHOT_MAX_BYTES
    terminate_grace_s: float = 5.0
    kill_timeout_s: float = 10.0
    keep_job_dirs: bool = False

    @property
    def heartbeat_every(self) -> float:
        return self.heartbeat_interval_s or self.lease_s / 4

    @property
    def lost_after(self) -> float:
        return self.presumed_lost_after_s or float(self.lease_s)


class _PrepKeepalive:
    """Renews the preparing lease while the snapshot downloads and extracts."""

    def __init__(self, client: RunnerClient, job: TestJob, interval_s: float):
        self.client, self.job, self.interval_s = client, job, interval_s
        self.stop_event = threading.Event()
        self.cancel_requested = False
        self.lease_lost: LeaseLost | None = None
        self.thread = threading.Thread(target=self._run, name=f"keepalive-{job.job_id}",
                                       daemon=True)

    def _run(self) -> None:
        while not self.stop_event.wait(self.interval_s):
            try:
                out = self.client.heartbeat(self.job, "preparing", window_s=self.interval_s)
                self.cancel_requested |= bool(out.get("cancel_requested"))
            except LeaseLost as e:
                self.lease_lost = e
                return
            except RunnerError as e:
                log.warning("preparing heartbeat failed: %s", e)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop_event.set()
        self.thread.join()


def _job_dirs(root: Path, job_id: str) -> dict[str, Path]:
    base = Path(root) / "jobs" / job_id
    return {"base": base, "src": base / "src", "state": base / "state", "tmp": base / "tmp",
            "home": base / "home", "archive": base / "snapshot.tar"}


def _rmtree(path: Path) -> None:
    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if path.exists():
        shutil.rmtree(path, onerror=onerror)


def _bounded_error(message: str) -> str:
    return bounded_text(message.encode("utf-8", "replace"), RESULT_ERROR_MAX_BYTES)


class _JobRun:
    def __init__(self, job: TestJob, root: Path, cpu_count: int, client: RunnerClient,
                 process_factory: ProcessFactory, settings: RunnerSettings,
                 clock: Callable[[], float], sleep: Callable[[float], None],
                 base_env: dict[str, str] | None,
                 profile_catalog: TestProfileCatalog | None):
        self.job, self.root, self.cpu_count = job, Path(root), cpu_count
        self.client, self.factory, self.s = client, process_factory, settings
        self.clock, self.sleep, self.base_env = clock, sleep, base_env
        self.profile_catalog = profile_catalog
        self.dirs = _job_dirs(self.root, job.job_id)
        self.last_ack = clock()

    # -- helpers --

    def _ack(self) -> None:
        self.last_ack = self.clock()

    def _presumed_lost(self) -> bool:
        return self.clock() - self.last_ack >= self.s.lost_after

    def _finish(self, result: TestResult) -> TestResult:
        """Post the one result, retrying the identical payload until acknowledged."""
        payload = result.payload(self.job.lease_token)
        while True:
            try:
                self.client.post_result(self.job, payload, window_s=self.s.heartbeat_every)
                return result
            except LeaseLost as e:
                raise RunnerHalted(f"lease lost before the result was recorded: {e}") from None
            except TransientError as e:
                if self._presumed_lost():
                    raise RunnerHalted(f"result undeliverable within the lease: {e}") from None
                log.warning("result post failed, retrying: %s", e)
                self.sleep(self.s.poll_interval_s)
            except ApiRejected as e:
                raise RunnerHalted(f"balancer refused the result: {e}") from None

    # -- phases --

    def prepare(self) -> tuple[TestProfile, Path] | TestResult:
        """Return the validated profile and cwd, or a no-process result to post."""
        d = self.dirs
        _rmtree(d["base"])  # a re-delivered preparing claim never started a process
        for key in ("state", "tmp", "home"):
            d[key].mkdir(parents=True, exist_ok=True)
        try:
            profile = (self.profile_catalog.resolve(self.job.profile)
                       if self.profile_catalog is not None
                       else TestProfile.parse(self.job.profile))
        except ProfileError as e:
            return TestResult("failed", error=_bounded_error(f"invalid profile: {e}"))
        if profile.memory_limit_mb is not None:
            # A cap that cannot be enforced for the whole tree is refused before any
            # download or spawn rather than silently running uncapped.
            supported, reason = memory_limit_support(self.factory)
            if not supported:
                resource_env, suffix = resource_environment(self.cpu_count, profile)
                return TestResult("failed", error=_bounded_error(
                    f"memory_limit_mb={profile.memory_limit_mb} cannot be enforced by this "
                    f"runner: {reason}"), resources=resource_report(
                        profile, resource_env, suffix, cap_installed=False))
        out = self.client.heartbeat(self.job, "preparing", window_s=self.s.lost_after)
        self._ack()
        if out.get("cancel_requested"):
            return TestResult("cancelled", error="cancelled before start")
        with _PrepKeepalive(self.client, self.job, self.s.heartbeat_every) as keepalive:
            try:
                self.client.download_snapshot(self.job, d["archive"],
                                              max_bytes=self.s.snapshot_max_bytes)
                extract_snapshot(d["archive"], d["src"], max_bytes=self.s.snapshot_max_bytes)
            except SnapshotError as e:
                failure: TestResult | None = TestResult(
                    "failed", error=_bounded_error(f"snapshot rejected: {e}"))
            else:
                failure = None
            finally:
                d["archive"].unlink(missing_ok=True)
        if keepalive.lease_lost is not None:
            raise keepalive.lease_lost
        if keepalive.cancel_requested:
            return TestResult("cancelled", error="cancelled before start")
        if failure is not None:
            return failure
        cwd = d["src"].joinpath(*_safe_relative_parts(profile.cwd)) if profile.cwd else d["src"]
        if not cwd.is_dir() or not _within(d["src"].resolve(), cwd):
            return TestResult("failed", error="profile cwd is not a directory in the snapshot")
        return profile, cwd

    def acknowledge_running(self) -> dict:
        try:
            out = self.client.heartbeat(self.job, "running", window_s=self.s.lost_after)
        except TransientError as e:
            raise RunnerHalted(f"running heartbeat was never acknowledged: {e}") from None
        if out.get("state") != "running":
            raise ProtocolError(f"running heartbeat reported state {out.get('state')!r}")
        self._ack()
        return out

    def execute(self, profile: TestProfile, cwd: Path) -> TestResult:
        s, d = self.s, self.dirs
        env = build_environment(profile, tmp_dir=d["tmp"], home_dir=d["home"],
                                job_id=self.job.job_id, base_env=self.base_env)
        resource_env, suffix = resource_environment(self.cpu_count, profile)
        env.update(resource_env)  # last, so the reported parallelism is the effective one
        argv = list(profile.argv) + suffix
        spawn_kwargs: dict[str, Any] = {}
        if profile.memory_limit_mb is not None:
            spawn_kwargs["memory_limit_mb"] = profile.memory_limit_mb
        shipper = LogShipper(self.client, self.job, chunk_bytes=s.log_chunk_bytes,
                             buffer_bytes=s.log_buffer_bytes)
        tails = {"stdout": _Tail(), "stderr": _Tail()}
        offsets = [0, 0]

        def read(limit: int) -> bool:
            out, err, offsets[0], offsets[1] = proc.read_output(offsets[0], offsets[1], limit)
            tails["stdout"].add(out)
            tails["stderr"].add(err)
            shipper.feed(out + err)
            return bool(out or err)

        try:
            proc = self.factory(argv, cwd, env, d["state"], self.job.job_id, **spawn_kwargs)
        except Exception as e:  # the adapter raises before any process exists
            log.warning("spawn failed: %s", e)
            return self._finish(TestResult("failed", error=_bounded_error(
                f"could not start the test command: {type(e).__name__}: {e}"),
                resources=resource_report(profile, resource_env, suffix,
                                          cap_installed=False)))
        # The adapter installs a requested cap on the whole tree before the process
        # runs, or raises above; a returned process is therefore capped.
        resources = resource_report(profile, resource_env, suffix, cap_installed=True)
        started = self.clock()
        try:
            outcome, exit_code = self._supervise(proc, profile, shipper, read, started)
        except BaseException as e:
            # Never leave a live scope behind an unexpected failure.
            try:
                proc.terminate_and_prove(s.terminate_grace_s, s.kill_timeout_s)
            except Exception:
                raise RunnerHalted("process termination could not be proven after "
                                   f"{type(e).__name__}") from None
            proc.close()
            if not isinstance(e, Exception):
                raise
            raise RunnerHalted(f"runner failed while the test ran: {type(e).__name__}: {e}"
                               ) from None

        try:
            proc.terminate_and_prove(s.terminate_grace_s, s.kill_timeout_s)
        except Exception as e:
            # The scope may still hold live processes: no result, no further claims.
            raise RunnerHalted(f"process termination could not be proven: {e}") from None
        duration = self.clock() - started
        if exit_code is None:
            exit_code = proc.poll()

        # The scope is empty, so the output is final: read it all, shipping as we go.
        try:
            while True:
                per_stream = min(s.read_limit_bytes, shipper.capacity() // 2)
                more = read(per_stream) if per_stream > 0 else True
                if not more and shipper.drained():
                    break
                if outcome in ("lease_lost", "unreachable"):
                    if not more:
                        break
                    shipper.pending.clear()  # keep the tails; nothing can be shipped
                    continue
                if shipper.drained():
                    continue
                try:
                    shipper.heartbeat(window_s=s.heartbeat_every)
                    self._ack()
                except TransientError as e:
                    if self._presumed_lost():
                        raise RunnerHalted(f"logs undeliverable within the lease: {e}") from None
                    self.sleep(s.poll_interval_s)
                except LeaseLost as e:
                    raise RunnerHalted(f"lease lost while flushing logs: {e}") from None
        finally:
            proc.close()

        if outcome == "lease_lost":
            raise RunnerHalted("the balancer reported the lease lost; test terminated")
        tail_fields = {"stdout_tail": tails["stdout"].text(),
                       "stderr_tail": tails["stderr"].text(), "duration_s": duration,
                       "resources": resources}
        if outcome == "exited":
            result = TestResult("succeeded" if exit_code == 0 else "failed",
                                exit_code=exit_code, **tail_fields)
        elif outcome == "cancelled":
            result = TestResult("cancelled", exit_code=exit_code, **tail_fields)
        elif outcome == "timeout":
            result = TestResult("failed", exit_code=exit_code, timed_out=True,
                                error=f"timed out after {profile.timeout_s:g}s", **tail_fields)
        else:  # unreachable: terminated because the lease could not be renewed
            result = TestResult("failed", exit_code=exit_code,
                                error="balancer unreachable for the lease period; test terminated",
                                **tail_fields)
        return self._finish(result)

    def _supervise(self, proc: TestProcess, profile: TestProfile, shipper: LogShipper,
                   read: Callable[[int], bool], started: float) -> tuple[str, int | None]:
        """Watch the live process; return why it must stop and its exit code if known."""
        s = self.s
        next_heartbeat = started + s.heartbeat_every
        last_chunk_sent = started
        while True:
            exit_code = proc.poll()
            per_stream = min(s.read_limit_bytes, shipper.capacity() // 2)
            if per_stream > 0:
                read(per_stream)
            if exit_code is not None:
                outcome = "exited"
                break
            now = self.clock()
            if now - started >= profile.timeout_s:
                outcome = "timeout"
                break
            due = now >= next_heartbeat or (shipper.has_full_chunk()
                                            and now - last_chunk_sent >= s.min_chunk_interval_s)
            if due:
                try:
                    hb = shipper.heartbeat(window_s=min(s.heartbeat_every, s.lost_after))
                except LeaseLost as e:
                    log.error("lease lost while running: %s", e)
                    outcome = "lease_lost"
                    break
                except TransientError as e:
                    log.warning("heartbeat failed: %s", e)
                    if self._presumed_lost():
                        outcome = "unreachable"
                        break
                    next_heartbeat = self.clock() + min(s.heartbeat_every, 1.0)
                else:
                    self._ack()
                    last_chunk_sent = self.last_ack
                    next_heartbeat = self.last_ack + s.heartbeat_every
                    if hb.get("cancel_requested"):
                        outcome = "cancelled"
                        break
            self.sleep(s.poll_interval_s)
        return outcome, exit_code

    def run(self) -> TestResult:
        try:
            prepared = self.prepare()
        except LeaseLost as e:
            raise RunnerHalted(f"lease lost while preparing: {e}") from None
        except TransientError as e:
            raise RunnerHalted(f"balancer unreachable while preparing: {e}") from None
        if isinstance(prepared, TestResult):
            return self._finish(prepared)
        profile, cwd = prepared
        try:
            ack = self.acknowledge_running()
        except LeaseLost as e:
            raise RunnerHalted(f"lease lost before start: {e}") from None
        if ack.get("cancel_requested"):
            return self._finish(TestResult("cancelled", error="cancelled before start"))
        return self.execute(profile, cwd)


def run_test_job(job: TestJob, root: Path, cpu_count: int, *, client: RunnerClient,
                 process_factory: ProcessFactory | None = None,
                 profile_catalog: TestProfileCatalog | None = None,
                 settings: RunnerSettings | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 base_env: dict[str, str] | None = None) -> TestResult:
    """Run one claimed job to a recorded result.

    Returns the result the balancer acknowledged. Raises RunnerHalted when the
    runner must stop claiming: the lease was lost or termination was unproven.
    ``cpu_count`` is the detected logical CPU count (consumed by resource policy).
    """
    settings = settings or RunnerSettings()
    factory = process_factory or default_process_factory()
    run = _JobRun(job, root, cpu_count, client, factory, settings, clock, sleep, base_env,
                  profile_catalog)
    result = run.run()
    if not settings.keep_job_dirs:
        try:
            _rmtree(run.dirs["base"])
        except OSError as e:
            log.warning("could not remove job directory: %s", e)
    return result


class Runner:
    """Claim loop with one job slot."""

    def __init__(self, client: RunnerClient, root: Path, *, cpu_count: int | None = None,
                 process_factory: ProcessFactory | None = None,
                 profile_catalog: TestProfileCatalog | None = None,
                 settings: RunnerSettings | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 base_env: dict[str, str] | None = None):
        self.client, self.root = client, Path(root)
        self.profile_catalog = (profile_catalog if profile_catalog is not None else
                                TestProfileCatalog.load(default_profile_catalog_path()))
        self.cpu_count = cpu_count or detect_cpu_count()
        self.factory = process_factory
        self.settings = settings or RunnerSettings()
        self.sleep, self.clock, self.base_env = sleep, clock, base_env
        self.halted: str | None = None

    def run(self, *, max_jobs: int | None = None, stop_when_idle: bool = False) -> int:
        done = 0
        delay = self.settings.idle_poll_s
        while max_jobs is None or done < max_jobs:
            try:
                data = self.client.claim()
            except AuthError as e:
                log.error("runner credentials refused: %s", e)
                return EXIT_USAGE
            except (TransientError, ApiRejected, ProtocolError) as e:
                log.warning("claim failed, will reconnect: %s", e)
                self.sleep(delay)
                continue
            if data is None:
                if stop_when_idle:
                    return EXIT_OK
                self.sleep(delay)
                continue
            try:
                job = TestJob.from_claim(data)
                log.info("claimed job %s", job.job_id)
                result = run_test_job(job, self.root, self.cpu_count, client=self.client,
                                      process_factory=self.factory or default_process_factory(),
                                      profile_catalog=self.profile_catalog,
                                      settings=self.settings, clock=self.clock,
                                      sleep=self.sleep, base_env=self.base_env)
                log.info("job %s finished: %s", job.job_id, result.status)
            except RunnerError as e:
                self.halted = str(e)
                log.error("runner halted; resolve the job on the balancer before restarting: %s",
                          e)
                return EXIT_HALTED
            done += 1
        return EXIT_OK


def detect_cpu_count() -> int:
    counter = getattr(os, "process_cpu_count", None) or os.cpu_count
    return max(1, counter() or 1)


# --- CLI ----------------------------------------------------------------------------------------

def default_state_dir(environ: dict[str, str] | None = None) -> Path:
    environ = os.environ if environ is None else environ
    if environ.get(HOME_ENV):
        return Path(environ[HOME_ENV])
    return Path.home() / ".a2a_link" / "runner"


def read_token(token_file: str | None, environ: dict[str, str]) -> str:
    """Token from --token-file, else A2A_LINK_RUNNER_TOKEN. Never from argv."""
    if token_file:
        path = Path(token_file)
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise ValueError(f"cannot read token file {path}: {type(e).__name__}") from None
        if os.name != "nt":
            try:
                if path.stat().st_mode & 0o077:
                    log.warning("token file %s is readable by other users", path)
            except OSError:
                pass
    else:
        token = (environ.get(TOKEN_ENV) or "").strip()
    if not token:
        raise ValueError(f"provide the runner token with --token-file or {TOKEN_ENV}")
    return token


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="a2a-link-runner", allow_abbrev=False,
        description="Run A2A_Link background test jobs claimed from a balancer.")
    p.add_argument("--balancer-url", required=True)
    p.add_argument("--runner-id", required=True)
    p.add_argument("--token-file", help=f"file holding the runner token (or set {TOKEN_ENV})")
    p.add_argument("--label", action="append", default=[],
                   help="informational; the balancer uses the labels registered for the runner")
    p.add_argument("--state-dir", help=f"per-job work directory root (default ${HOME_ENV} "
                                       "or ~/.a2a_link/runner)")
    p.add_argument("--profiles-file", help=f"runner-local profile catalog (or set {PROFILE_CONFIG_ENV}; "
                                            "default: $A2A_LINK_HOME/test-profiles.json, then the checkout example)")
    p.add_argument("--lease-s", type=int, default=60)
    p.add_argument("--cpu-count", type=int, default=None)
    p.add_argument("--once", action="store_true", help="run at most one job, exit when idle")
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: list[str] | None = None, *, environ: dict[str, str] | None = None,
         process_factory: ProcessFactory | None = None,
         http_client: httpx.Client | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    environ = os.environ if environ is None else environ
    if any(a == "--token" or a.startswith("--token=") for a in argv):
        print(f"a2a-link-runner: --token is not accepted; use --token-file or {TOKEN_ENV}",
              file=sys.stderr)
        return EXIT_USAGE
    args = _parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        token = read_token(args.token_file, environ)
        if not 15 <= args.lease_s <= 600:
            raise ValueError("--lease-s must be between 15 and 600")
        profile_catalog = TestProfileCatalog.load(
            default_profile_catalog_path(environ, args.profiles_file))
        client = RunnerClient(args.balancer_url, args.runner_id, token, http_client,
                              lease_s=args.lease_s)
    except (ValueError, ProfileError) as e:
        print(f"a2a-link-runner: {e}", file=sys.stderr)
        return EXIT_USAGE
    environ.pop(TOKEN_ENV, None)  # keep the secret out of anything spawned later
    state_dir = Path(args.state_dir) if args.state_dir else default_state_dir(environ)
    state_dir.mkdir(parents=True, exist_ok=True)
    if process_factory is None:
        try:
            process_factory = default_process_factory()
        except ImportError as e:
            print(f"a2a-link-runner: process adapter unavailable: {e}", file=sys.stderr)
            return EXIT_USAGE
    runner = Runner(client, state_dir, cpu_count=args.cpu_count,
                    process_factory=process_factory,
                    profile_catalog=profile_catalog,
                    settings=RunnerSettings(lease_s=args.lease_s))
    log.info("runner %s polling %s (labels: %s)", args.runner_id, client.base_url,
             ", ".join(args.label) or "as registered")
    try:
        if args.once:
            return runner.run(max_jobs=1, stop_when_idle=True)
        return runner.run()
    except KeyboardInterrupt:
        return EXIT_OK
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
