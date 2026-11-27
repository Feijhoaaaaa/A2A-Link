"""HTTP layer for background test jobs.

Contract: docs/superpowers/specs/2026-11-27-background-test-jobs-api.md.

State transitions, idempotency and lease ownership live in balancer.repository;
this module owns the trust boundary (loopback vs tailnet peers), bearer
authentication, request/response schemas, and the snapshot/log files kept under
the A2A_Link home directory.

Wire formats chosen here (the contract leaves them open):

- Runner bearer tokens have the form ``<runner_id>.<secret>``. The runner ID is
  taken from the token and must match the registered record whose stored
  SHA-256 hash equals ``sha256(token)``.
- Runner-route snapshot downloads carry the lease token in ``X-Lease-Token``.
- Heartbeat log chunks are base64 (``log_chunk``) at an explicit byte
  ``log_offset``; log reads return the bytes as UTF-8 text cut at a character
  boundary, with byte offsets.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import inspect
import ipaddress
import os
import re
import secrets
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import (AliasChoices, BaseModel, ConfigDict, Field, JsonValue, StringConstraints,
                      ValidationError, field_validator, model_validator)

from balancer import repository as repo
from balancer import tasklog
from balancer.db import get_connection, write_tx

GiB = 1024 ** 3
MiB = 1024 ** 2
KiB = 1024
DEFAULT_SNAPSHOT_MAX_BYTES = GiB
DEFAULT_LOG_MAX_BYTES = 8 * MiB
LOG_CHUNK_MAX_BYTES = 256 * KiB
LOG_READ_MAX_BYTES = 1 * MiB
RESULT_TAIL_MAX_BYTES = 64 * KiB
DEFAULT_LEASE_S, MIN_LEASE_S, MAX_LEASE_S = 60, 15, 600
DELIVERY_MAX_WAIT_S = 55
DELIVERY_POLL_INTERVAL_S = 0.5
REGISTRATION_TOKEN_ENV = "A2A_LINK_RUNNER_REGISTRATION_TOKEN"
CHILD_STATES = ("queued", "preparing", "running", "succeeded", "failed", "cancelled", "lost")

# --- trust boundary -----------------------------------------------------------

TAILNET_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"),
                    ipaddress.ip_network("fd7a:115c:a1e0::/48"))
# Headers added by reverse proxies (including `tailscale serve`). A loopback peer
# carrying any of them is a proxied remote client, not a local one.
FORWARDING_HEADERS = frozenset(h.encode() for h in (
    "forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip",
    "tailscale-user-login", "tailscale-user-name", "tailscale-user-profile-pic",
))
# Starlette's in-process TestClient reports this peer; it never comes from a socket.
IN_PROCESS_PEERS = frozenset({"testclient"})
RUNNER_ROUTES = re.compile(r"^/(runners/claim|runners/[^/]+/heartbeat|test-jobs/[^/]+/(snapshot|result))/?$")


def classify_peer(host: str | None) -> Literal["loopback", "tailnet", "other"]:
    if host in IN_PROCESS_PEERS:
        return "loopback"
    try:
        ip = ipaddress.ip_address((host or "").split("%", 1)[0])
    except ValueError:
        return "other"
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "loopback"
    if any(ip in net for net in TAILNET_NETWORKS):
        return "tailnet"
    return "other"


def is_runner_route(path: str) -> bool:
    return RUNNER_ROUTES.match(path) is not None


def _parse_local_authority(authority: str) -> tuple[str, int | None] | None:
    if not authority or any(char in authority for char in "\r\n\t /\\?#,@"):
        return None
    try:
        parsed = urlsplit("//" + authority)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (hostname not in ("127.0.0.1", "localhost") or parsed.username is not None
            or parsed.password is not None or parsed.path or parsed.query or parsed.fragment
            or authority.endswith(":")):
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    return hostname, port


def _request_host_allowed(scope, headers: list[tuple[bytes, bytes]]) -> bool:
    peer = (scope.get("client") or (None,))[0]
    host_values = [value.decode("latin1") for name, value in headers if name.lower() == b"host"]
    if peer == "testclient" and (not host_values or host_values == ["testserver"]):
        return True
    if len(host_values) != 1:
        return False
    return _parse_local_authority(host_values[0]) is not None


def _origin_matches(scope, headers: list[tuple[bytes, bytes]]) -> bool:
    origins = [value.decode("latin1") for name, value in headers if name.lower() == b"origin"]
    if not origins:
        return True
    if len(origins) != 1:
        return False
    try:
        origin = urlsplit(origins[0])
        origin_port = origin.port
    except ValueError:
        return False
    if (origin.scheme.lower() != scope.get("scheme", "http").lower()
            or not origin.netloc or origin.username is not None or origin.password is not None
            or origin.path or origin.query or origin.fragment):
        return False
    origin_host = _parse_local_authority(origin.netloc)
    host_values = [value.decode("latin1") for name, value in headers if name.lower() == b"host"]
    if len(host_values) != 1:
        # Starlette's in-process test transport may omit Host; allow only its fixed host.
        return (scope.get("client", (None,))[0] == "testclient"
                and origin.netloc.lower() == "testserver")
    if (scope.get("client", (None,))[0] == "testclient"
            and host_values == ["testserver"] and origin.netloc.lower() == "testserver"):
        return True
    request_host = _parse_local_authority(host_values[0])
    if origin_host is None or request_host is None or origin_host[0] != request_host[0]:
        return False
    default_port = 443 if origin.scheme.lower() == "https" else 80
    return (origin_port or default_port) == (request_host[1] or default_port)


class TrustBoundaryMiddleware:
    """Runner routes: loopback or tailnet peers. Everything else: direct loopback only."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        peer = classify_peer((scope.get("client") or (None,))[0])
        if is_runner_route(scope.get("path", "")):
            allowed = peer in ("loopback", "tailnet")
        else:
            headers = scope.get("headers", [])
            proxied = any(name.lower() in FORWARDING_HEADERS for name, _ in headers)
            allowed = (peer == "loopback" and not proxied
                       and _request_host_allowed(scope, headers))
            if allowed and scope["type"] == "http" and scope.get("method", "GET").upper() in {
                "POST", "PUT", "PATCH", "DELETE"
            }:
                allowed = _origin_matches(scope, headers)
        if allowed:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        response = JSONResponse({"error": "forbidden_origin",
                                 "detail": "this route is not reachable from this peer"},
                                status_code=403)
        await response(scope, receive, send)


# --- request / response schemas ---------------------------------------------------

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Ident = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
RequestId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,128}$")]
RunnerId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Label = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{1,64}$")]
Token = Annotated[str, StringConstraints(min_length=1, max_length=256)]
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)  # no "60" -> 60 coercion


class _Out(BaseModel):
    # Unknown repository fields are dropped, so nothing (e.g. a lease token) leaks by accident.
    model_config = ConfigDict(extra="ignore")


def _utf8_bounded(value: str | None, limit: int) -> str | None:
    if value is not None and len(value.encode("utf-8")) > limit:
        raise ValueError(f"must be at most {limit} UTF-8 bytes")
    return value


class TargetLabelsIn(_Strict):
    labels: list[Label] = Field(min_length=1, max_length=16)


class TestRequestIn(_Strict):
    __test__ = False  # not a pytest class
    request_id: RequestId
    snapshot_sha256: Sha256
    profile: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    targets: list[Label | TargetLabelsIn] = Field(min_length=1, max_length=32)
    session_ref: JsonValue = Field(...)


class RegisterIn(_Strict):
    runner_id: RunnerId
    labels: list[Label] = Field(min_length=1, max_length=32)
    platform: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    rotate: bool = False


class RunnerClaimIn(_Strict):
    lease_s: int = Field(DEFAULT_LEASE_S, ge=MIN_LEASE_S, le=MAX_LEASE_S)


class HeartbeatIn(_Strict):
    job_id: Ident
    lease_token: Token
    phase: Literal["preparing", "running"]
    log_offset: int | None = Field(None, ge=0)
    log_chunk: str | None = Field(None, max_length=(LOG_CHUNK_MAX_BYTES * 4) // 3 + 4)

    @model_validator(mode="after")
    def chunk_and_offset_together(self):
        if (self.log_offset is None) != (self.log_chunk is None):
            raise ValueError("log_offset and log_chunk must be sent together")
        if self.log_chunk is not None:
            try:
                data = base64.b64decode(self.log_chunk, validate=True)
            except (binascii.Error, ValueError):
                raise ValueError("log_chunk must be base64") from None
            if not data or len(data) > LOG_CHUNK_MAX_BYTES:
                raise ValueError(f"log_chunk must decode to 1..{LOG_CHUNK_MAX_BYTES} bytes")
        return self

    def chunk_bytes(self) -> bytes | None:
        return None if self.log_chunk is None else base64.b64decode(self.log_chunk, validate=True)


class JobResultResourcesIn(_Strict):
    parallelism: int = Field(ge=1)
    parallel_args_applied: list[Annotated[str, StringConstraints(max_length=4096)]] = Field(
        max_length=128)
    memory_limit_mb: int | None = Field(None, ge=1)
    memory_limit_enforced: bool | None

    @model_validator(mode="after")
    def memory_report_matches_limit(self):
        if self.memory_limit_mb is None and self.memory_limit_enforced is not None:
            raise ValueError("memory_limit_enforced must be null when no memory limit was requested")
        if self.memory_limit_mb is not None and self.memory_limit_enforced is None:
            raise ValueError("memory_limit_enforced must be a boolean when a memory limit was requested")
        return self


class JobResultIn(_Strict):
    lease_token: Token
    status: Literal["succeeded", "failed", "cancelled"]  # `lost` is the balancer's verdict only
    exit_code: int | None = None
    timed_out: bool = False
    duration_s: float | None = Field(None, ge=0)
    stdout_tail: str = ""
    stderr_tail: str = ""
    error: str | None = None
    resources: JobResultResourcesIn | None = None

    @field_validator("stdout_tail", "stderr_tail")
    @classmethod
    def tails_bounded(cls, value: str) -> str:
        return _utf8_bounded(value, RESULT_TAIL_MAX_BYTES)

    @field_validator("error")
    @classmethod
    def error_bounded(cls, value: str | None) -> str | None:
        return _utf8_bounded(value, 4 * KiB)

    @model_validator(mode="after")
    def timeout_only_on_failure(self):
        if self.timed_out and self.status != "failed":
            raise ValueError("timed_out can only be true for a failed result")
        return self


class JobOut(_Out):
    job_id: str
    request_id: str
    target: str | dict[str, JsonValue]
    state: Literal[CHILD_STATES]  # type: ignore[valid-type]
    runner_id: str | None = None
    runner_platform: Literal["windows", "linux"] | None = None
    lease_expires_at: str | None = None
    cancel_requested: bool = False
    log_stored_bytes: int = Field(0, ge=0)
    log_truncated: bool = False
    result: dict[str, JsonValue] | None = None
    created_at: str
    updated_at: str


class DeliveryStatusOut(_Out):
    state: Literal["pending", "active", "delivered", "failed", "undeliverable"]
    reason: str | None = None
    message_id: str
    created_at: str


class RequestOut(_Out):
    # No session_ref field: _Out drops it even if a store returns one.
    request_id: str
    snapshot_sha256: str
    profile: JsonValue
    delivery: DeliveryStatusOut | None = None
    targets: list[JsonValue]
    jobs: list[JobOut] = Field(min_length=1)
    state: str
    cancel_requested: bool = False
    created_at: str
    updated_at: str

    @model_validator(mode="before")
    @classmethod
    def cancel_from_timestamp(cls, data: Any) -> Any:
        # The repository records cancellation as `cancel_requested_at`.
        if isinstance(data, dict) and "cancel_requested" not in data:
            data = {**data, "cancel_requested": data.get("cancel_requested_at") is not None}
        return data


class TestRequestListOut(_Out):
    requests: list[RequestOut]


class ReservationOut(_Out):
    # With an `owner` added, this is the body of the issue "reserve message" route.
    message_id: str
    kind: Literal["continue"]
    round_id: str
    generation: int
    requested_profile: dict[str, JsonValue]
    profile_source: str | None
    prompt: str


class ReadyDeliveryOut(_Out):
    # For the local dispatcher only. No provider session ID or cwd: it reads the saved handle.
    request_id: str
    issue_id: str
    participant: str
    generation: int
    summary: dict[str, JsonValue]
    reservation: ReservationOut


class DeliveryListOut(_Out):
    deliveries: list[ReadyDeliveryOut]


class RunnerOut(_Out):
    runner_id: str
    labels: list[str]
    platform: str


class ClaimOut(_Out):
    # Only what a runner needs: never the session reference or other tokens.
    job_id: str
    request_id: str
    target: str | dict[str, JsonValue]
    state: Literal["preparing"]
    profile: JsonValue
    snapshot_sha256: str
    lease_token: str
    lease_expires_at: str


class HeartbeatOut(_Out):
    job_id: str
    state: Literal[CHILD_STATES]  # type: ignore[valid-type]
    lease_expires_at: str | None
    next_log_offset: int = Field(validation_alias=AliasChoices("next_log_offset", "next_offset",
                                                               "log_offset"))
    log_truncated: bool = Field(False, validation_alias=AliasChoices("log_truncated", "truncated"))
    cancel_requested: bool = False


def _out(model: type[BaseModel], data: Any) -> dict:
    try:
        return model.model_validate(data).model_dump(mode="json")
    except ValidationError as e:
        raise repo.ApiError(500, "invalid_response",
                            f"repository returned an invalid {model.__name__}",
                            errors=len(e.errors())) from None


# --- storage ----------------------------------------------------------------------

class RepositoryStore:
    """Default store: the repository's documented test-job surface plus a lease lookup.

    Attribute access is resolved at call time so the repository can grow the
    agreed functions independently of this module.
    """

    def __getattr__(self, name):
        return getattr(repo, name)

    def get_test_job_lease(self, conn, job_id: str) -> dict | None:
        # j.* carries the acknowledged log counters (log_received_bytes, log_stored_bytes).
        row = conn.execute(
            "SELECT j.*, r.snapshot_sha256 FROM test_jobs AS j"
            " JOIN test_requests AS r USING (request_id) WHERE j.job_id=?", (job_id,)).fetchone()
        return None if row is None else dict(row)


def _now() -> str:
    return repo.ts(datetime.now(timezone.utc))


def _fsync_close(f) -> None:
    f.flush()
    os.fsync(f.fileno())
    f.close()


def _read_range(path: Path, offset: int, limit: int) -> bytes:
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(limit)
    except FileNotFoundError:
        return b""


def _log_corrupt(committed: int, on_disk: int | None) -> "repo.ApiError":
    found = "missing" if on_disk is None else f"{on_disk} bytes"
    return repo.ApiError(500, "log_corrupt",
                         f"the stored log file is shorter than the committed {committed} bytes"
                         f" ({found})", committed_bytes=committed, on_disk_bytes=on_disk)


def _apply_log(path: Path, truncate_to: int, append: bytes) -> None:
    """Cut the file back to the committed length, then append the acknowledged bytes durably.

    The file is never extended: a missing or short file means committed bytes were
    lost, so this fails (rolling the heartbeat back) instead of padding with NULs.
    """
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | (os.O_CREAT if truncate_to == 0 else 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileNotFoundError:
        raise _log_corrupt(truncate_to, None) from None
    with os.fdopen(fd, "r+b") as f:
        size = os.fstat(f.fileno()).st_size
        if size < truncate_to:
            raise _log_corrupt(truncate_to, size)
        if size > truncate_to:
            f.truncate(truncate_to)
        f.seek(truncate_to)
        f.write(append)
        f.flush()
        os.fsync(f.fileno())


def _truncate_log(path: Path, size: int) -> None:
    try:
        with open(path, "r+b") as f:
            if os.fstat(f.fileno()).st_size > size:
                f.truncate(size)
                f.flush()
                os.fsync(f.fileno())
    except FileNotFoundError:
        pass


def _utf8_prefix(data: bytes, at_eof: bool) -> bytes:
    """Drop a trailing partial UTF-8 sequence so the next read starts on a character."""
    if at_eof:
        return data
    for cut in range(0, min(3, len(data)) + 1):
        candidate = data[:len(data) - cut]
        try:
            candidate.decode("utf-8")
            return candidate
        except UnicodeDecodeError as e:
            if e.start < len(candidate) - 3:  # invalid bytes mid-chunk: not a boundary issue
                return data
    return data


# --- routes -------------------------------------------------------------------------

def register_test_job_routes(app: FastAPI, db_path: str, *, store=None,
                             snapshot_max_bytes: int | None = None,
                             log_max_bytes: int | None = None) -> None:
    store = store or RepositoryStore()
    if snapshot_max_bytes is None:
        snapshot_max_bytes = int(os.environ.get("A2A_LINK_SNAPSHOT_MAX_BYTES", DEFAULT_SNAPSHOT_MAX_BYTES))
    if log_max_bytes is None:
        log_max_bytes = int(os.environ.get("A2A_LINK_TEST_LOG_MAX_BYTES", DEFAULT_LOG_MAX_BYTES))
    log_lock = threading.Lock()

    def snapshot_dir() -> Path:
        path = tasklog.home() / "test-snapshots"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def log_path(job_id: str) -> Path:
        path = tasklog.home() / "test-logs"
        path.mkdir(parents=True, exist_ok=True)
        return path / f"{job_id}.log"

    def db():
        return closing(get_connection(db_path))

    def check(pattern: re.Pattern, value: str, what: str) -> None:
        if not pattern.match(value):
            raise repo.ApiError(422, "validation_error", f"invalid {what}")

    def bearer(request: Request) -> str | None:
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        value = value.strip()
        return value if scheme.lower() == "bearer" and value else None

    def unauthorized(detail: str):
        return repo.ApiError(401, "unauthorized", detail)

    def authenticate(request: Request, conn) -> dict:
        token = bearer(request)
        runner_id, dot, secret = (token or "").partition(".")
        if not token or not dot or not secret or not re.match(r"^[A-Za-z0-9_-]{1,64}$", runner_id):
            raise unauthorized("a runner bearer token is required")
        runner = store.authenticate_test_runner(conn, runner_id,
                                                hashlib.sha256(token.encode()).hexdigest())
        if not runner or runner.get("runner_id") != runner_id:
            raise unauthorized("unknown runner or invalid token")
        return runner

    def owned_lease(conn, job_id: str, runner_id: str, lease_token: str) -> dict:
        job = store.get_test_job_lease(conn, job_id)
        if job is None:
            raise repo.ApiError(404, "not_found", f"test job {job_id!r} not found")
        if job["runner_id"] != runner_id or not hmac.compare_digest(
                (job["lease_token"] or "").encode(), lease_token.encode()):
            raise repo.ApiError(409, "owner_mismatch", "the runner or lease token does not match")
        if job["state"] not in ("preparing", "running") or (job["lease_expires_at"] or "") <= _now():
            raise repo.ApiError(409, "lease_inactive", "the job lease is not active")
        return job

    # --- agent / operator routes (loopback only) --------------------------------

    @app.get("/test-requests")
    def test_request_list(limit: Annotated[int, Query(ge=1, le=50)] = 50):
        with db() as conn:
            requests = store.list_test_requests(conn, limit)
        return _out(TestRequestListOut, {"requests": requests})

    def ready_deliveries(limit: int) -> list[dict]:
        with db() as conn:
            return store.list_ready_test_deliveries(conn, limit)

    @app.get("/test-deliveries")
    async def test_delivery_poll(
            request: Request,
            # NaN would never reach the deadline, so it is refused explicitly.
            wait_s: Annotated[float, Query(ge=0, le=DELIVERY_MAX_WAIT_S,
                                           allow_inf_nan=False)] = 0,
            limit: Annotated[int, Query(ge=1, le=50)] = 10):
        # SQLite is the wakeup source, so this survives restarts and sees writes from other
        # balancer processes. Sleeping on the event loop holds no worker thread.
        deadline = time.monotonic() + wait_s
        while True:
            deliveries = await run_in_threadpool(ready_deliveries, limit)
            remaining = deadline - time.monotonic()
            if deliveries or not remaining > 0 or await request.is_disconnected():
                break
            await asyncio.sleep(min(DELIVERY_POLL_INTERVAL_S, remaining))
        return _out(DeliveryListOut, {"deliveries": deliveries})

    @app.put("/test-snapshots/{sha256}")
    async def snapshot_upload(sha256: str, request: Request):
        check(SHA256_RE, sha256, "sha256")
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > snapshot_max_bytes:
            raise repo.ApiError(413, "snapshot_too_large", f"limit is {snapshot_max_bytes} bytes")
        directory = snapshot_dir()
        fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".upload-", suffix=".part")
        tmp = Path(tmp_name)
        digest, size = hashlib.sha256(), 0
        try:
            f = os.fdopen(fd, "wb")
            try:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > snapshot_max_bytes:
                        raise repo.ApiError(413, "snapshot_too_large",
                                            f"limit is {snapshot_max_bytes} bytes")
                    digest.update(chunk)
                    await run_in_threadpool(f.write, chunk)
                await run_in_threadpool(_fsync_close, f)
            finally:
                f.close()
            if digest.hexdigest() != sha256:
                raise repo.ApiError(422, "digest_mismatch", "uploaded bytes do not match the sha256")
            status = await run_in_threadpool(publish_snapshot, sha256, tmp, size, directory)
        finally:
            tmp.unlink(missing_ok=True)
        return JSONResponse({"sha256": sha256, "byte_count": size, "created": status == 201},
                            status_code=status)

    def publish_snapshot(sha256: str, tmp: Path, size: int, directory: Path) -> int:
        final = directory / f"{sha256}.tar"
        try:
            os.replace(tmp, final)  # content-addressed: replacing identical bytes is harmless
        except PermissionError:  # Windows: destination open by a reader
            if not final.exists() or final.stat().st_size != size:
                raise
        with db() as conn:
            if store.get_test_snapshot(conn, sha256) is not None:
                return 200
            try:
                store.record_test_snapshot(conn, sha256, size, f"test-snapshots/{sha256}.tar")
            except repo.ApiError:
                if store.get_test_snapshot(conn, sha256) is None:  # not a concurrent publish
                    raise
                return 200
        return 201

    def require_snapshot(conn, sha256: str) -> None:
        """A request may only reference a registered snapshot whose stored file is intact."""
        meta = store.get_test_snapshot(conn, sha256)
        path = snapshot_dir() / f"{sha256}.tar"
        try:
            intact = meta is not None and path.stat().st_size == meta["byte_count"]
        except OSError:
            intact = False
        if not intact:
            raise repo.ApiError(409, "snapshot_missing",
                                "upload the snapshot first; it is not registered and stored")

    @app.post("/test-requests")
    def test_request_create(body: TestRequestIn):
        data = body.model_dump(mode="json")
        with db() as conn:
            existed = store.get_test_request(conn, data["request_id"]) is not None
            if not existed:
                require_snapshot(conn, data["snapshot_sha256"])
            view = store.create_test_request(conn, data["request_id"], data["snapshot_sha256"],
                                             data["profile"], data["targets"], data["session_ref"])
        return JSONResponse(_out(RequestOut, view), status_code=200 if existed else 201)

    def load_request(conn, request_id: str) -> dict:
        check(REQUEST_ID_RE, request_id, "request_id")
        view = store.get_test_request(conn, request_id)
        if view is None:
            raise repo.ApiError(404, "not_found", f"test request {request_id!r} not found")
        return view

    @app.get("/test-requests/{request_id}")
    def test_request_get(request_id: str):
        with db() as conn:
            return _out(RequestOut, load_request(conn, request_id))

    @app.post("/test-requests/{request_id}/cancel")
    def test_request_cancel(request_id: str):
        with db() as conn:
            load_request(conn, request_id)
            return _out(RequestOut, store.cancel_test_request(conn, request_id))

    @app.get("/test-requests/{request_id}/log")
    def test_request_log(request_id: str, job_id: str | None = None,
                         offset: Annotated[int, Query(ge=0)] = 0,
                         limit: Annotated[int, Query(ge=1, le=LOG_READ_MAX_BYTES)] = 64 * KiB):
        with db() as conn:
            raw = load_request(conn, request_id)
            view = _out(RequestOut, raw)
        jobs = {job["job_id"]: job for job in view["jobs"]}
        raw_jobs = {job.get("job_id"): job for job in raw["jobs"]}
        if job_id is None:
            if len(jobs) != 1:
                raise repo.ApiError(422, "job_id_required", "this request has several jobs")
            job_id = next(iter(jobs))
        if job_id not in jobs:
            raise repo.ApiError(404, "not_found", f"job {job_id!r} is not part of this request")
        check(IDENT_RE, job_id, "job_id")
        # The committed DB count is authoritative: bytes past it belong to a heartbeat
        # that never committed (or is committing right now) and are never served.
        stored = committed_log_bytes(raw_jobs[job_id])
        path = log_path(job_id)
        on_disk = log_size(path)
        if on_disk is not None and on_disk > stored:
            stored = recover_log(job_id, path)
            on_disk = log_size(path)
        if (on_disk or 0) < stored:  # committed bytes are gone: never serve a short EOF
            raise _log_corrupt(stored, on_disk)
        data = _read_range(path, offset, max(0, min(limit, stored - offset)))
        eof = offset + len(data) >= stored
        data = _utf8_prefix(data, eof)
        job = raw_jobs[job_id]
        truncated = job.get("log_truncated")
        return {"request_id": request_id, "job_id": job_id, "state": jobs[job_id]["state"],
                "offset": offset, "next_offset": offset + len(data), "stored_bytes": stored,
                "eof": offset + len(data) >= stored,
                "truncated": bool(stored >= log_max_bytes if truncated is None else truncated),
                "limit_bytes": log_max_bytes,
                "text": data.decode("utf-8", errors="replace")}

    def log_size(path: Path) -> int | None:
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return None

    def committed_log_bytes(job: dict | None) -> int:
        value = (job or {}).get("log_stored_bytes", 0)
        if type(value) is not int or value < 0:
            raise repo.ApiError(500, "invalid_response", "repository returned an invalid log count")
        return value

    def recover_log(job_id: str, path: Path) -> int:
        """Cut a log file back to its committed length while holding the DB write lock.

        Heartbeats write the file inside their write transaction, so holding the lock
        here guarantees no uncommitted append is in flight while we truncate.
        """
        with write_tx(db_path) as conn:
            stored = committed_log_bytes(store.get_test_job_lease(conn, job_id))
            with log_lock:
                _truncate_log(path, stored)
        return stored

    @app.post("/runners/register")
    def runner_register(body: RegisterIn, request: Request):
        admin = os.environ.get(REGISTRATION_TOKEN_ENV, "")
        if not admin:
            raise repo.ApiError(503, "registration_disabled", f"{REGISTRATION_TOKEN_ENV} is not set")
        supplied = bearer(request)
        if supplied is None or not hmac.compare_digest(supplied.encode(), admin.encode()):
            raise unauthorized("the registration bearer token is required")
        token = f"{body.runner_id}.{secrets.token_urlsafe(32)}"
        with db() as conn:
            runner = store.register_test_runner(conn, body.runner_id, list(body.labels),
                                                body.platform,
                                                hashlib.sha256(token.encode()).hexdigest(),
                                                rotate=body.rotate)
        return JSONResponse({"runner": _out(RunnerOut, runner), "token": token},
                            status_code=200 if body.rotate else 201)

    # --- runner routes (loopback or tailnet, bearer authenticated) ---------------

    @app.post("/runners/claim")
    def runner_claim(request: Request, body: RunnerClaimIn | None = None):
        lease_s = (body or RunnerClaimIn()).lease_s
        with db() as conn:
            runner = authenticate(request, conn)
            job = store.claim_test_job(conn, runner["runner_id"], list(runner["labels"]), lease_s)
        return {"job": None if not job else _out(ClaimOut, job)}

    @app.post("/runners/{runner_id}/heartbeat")
    def runner_heartbeat(runner_id: str, body: HeartbeatIn, request: Request):
        chunk = body.chunk_bytes()
        path = log_path(body.job_id)
        # One write transaction covers the repository transition and the log file: the
        # file is truncated to the committed length, appended and fsynced before COMMIT,
        # so any failure rolls the offsets back and an exact retry starts cleanly.
        with write_tx(db_path) as conn:
            runner = authenticate(request, conn)
            if runner["runner_id"] != runner_id:
                raise repo.ApiError(403, "runner_mismatch", "the token belongs to another runner")
            # No lease precheck here: the repository validates owner, token and expiry
            # itself, and must be able to commit a discovered expiry (lease_lost).
            if chunk is None:  # empty heartbeat: restate the acknowledged offset
                job = store.get_test_job_lease(conn, body.job_id) or {}
                offset, chunk = job.get("log_received_bytes", 0), b""
                if type(offset) is not int or offset < 0:
                    raise repo.ApiError(500, "invalid_response",
                                        "repository returned an invalid log offset")
            else:
                offset = body.log_offset
            heartbeat = store.heartbeat_test_job
            extra = ({"log_limit": log_max_bytes}
                     if "log_limit" in inspect.signature(heartbeat).parameters else {})
            out = heartbeat(conn, body.job_id, runner_id, body.lease_token,
                            phase=body.phase, log_offset=offset, log_chunk=chunk, **extra)
            if not out:
                # Keep any lease expiry the repository discovered.
                raise repo.ApiError(409, "owner_mismatch",
                                    "the runner or lease token does not match an active lease",
                                    commit=True)
            if not isinstance(out, dict):
                raise repo.ApiError(500, "invalid_response", "repository heartbeat returned no state")
            truncate_to, append = out.get("truncate_to"), out.get("append_bytes", b"")
            if type(truncate_to) is not int or truncate_to < 0 or not isinstance(append, bytes):
                raise repo.ApiError(500, "invalid_response",
                                    "repository heartbeat returned no log write")
            with log_lock:
                _apply_log(path, truncate_to, append)
        return _out(HeartbeatOut, {"job_id": body.job_id, **out})

    @app.get("/test-jobs/{job_id}/snapshot")
    def runner_snapshot(job_id: str, request: Request):
        check(IDENT_RE, job_id, "job_id")
        lease_token = request.headers.get("x-lease-token", "")
        if not lease_token:
            raise repo.ApiError(409, "owner_mismatch", "X-Lease-Token is required")
        with db() as conn:
            runner = authenticate(request, conn)
            job = owned_lease(conn, job_id, runner["runner_id"], lease_token)
            meta = store.get_test_snapshot(conn, job["snapshot_sha256"])
        path = snapshot_dir() / f"{job['snapshot_sha256']}.tar"
        if meta is None or not path.is_file():
            raise repo.ApiError(404, "snapshot_missing", "the job's snapshot is not stored")
        if path.stat().st_size != meta["byte_count"]:
            raise repo.ApiError(500, "snapshot_corrupt", "stored snapshot size does not match")
        return FileResponse(path, media_type="application/x-tar",
                            headers={"X-Snapshot-SHA256": job["snapshot_sha256"],
                                     "Cache-Control": "no-store"})

    @app.post("/test-jobs/{job_id}/result")
    def runner_result(job_id: str, body: JobResultIn, request: Request):
        check(IDENT_RE, job_id, "job_id")
        result = body.model_dump(mode="json", exclude={"lease_token"})
        # Exclude absent top-level result fields but preserve explicit `null`s in the
        # strict resources object (e.g. no requested memory cap).
        result = {key: value for key, value in result.items() if value is not None}
        with db() as conn:
            runner = authenticate(request, conn)
            state = store.finish_test_job(conn, job_id, runner["runner_id"], body.lease_token,
                                          result)
        return {"job_id": job_id, "state": state}
