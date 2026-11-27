import asyncio
import json
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Callable, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from balancer import repository as repo
from balancer.codex_usage import codex_windows
from balancer.tasklog import read_tail
from balancer.db import get_connection, init_schema, write_tx
from balancer.router import ProfileError, choose_agent, resolve_profile
from balancer.test_jobs_api import TrustBoundaryMiddleware, register_test_job_routes

DEFAULT_AGENTS = ["claude", "codex"]
STATIC_DIR = Path(__file__).parent / "static"


class TaskIn(BaseModel):
    description: str = Field(min_length=1)
    model_hint: str = ""
    required_capability: str | None = None


class ClaimIn(BaseModel):
    agent: str = Field(min_length=1)


class FailIn(BaseModel):
    error: str


class CompleteIn(BaseModel):
    result: str | None = None


class IncidentIn(BaseModel):
    agent: str
    kind: str
    detail: str


class UsageIn(BaseModel):
    agent: str
    pct_used: float | None = Field(default=None, ge=0, le=100)
    window_started_at: str | None = None
    weekly_pct_used: float | None = Field(default=None, ge=0, le=100)
    weekly_resets_at: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def has_usage_window(self):
        if self.pct_used is None and self.weekly_pct_used is None:
            raise ValueError("at least one usage window is required")
        if (self.pct_used is None) != (self.window_started_at is None):
            raise ValueError("pct_used and window_started_at must be provided together")
        return self


class CapabilitiesIn(BaseModel):
    capabilities: list[str]


# --- issue MVP request bodies (unknown fields are rejected with 422) -----------------

Sha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Key = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]{1,32}$")]
Text = Annotated[str, StringConstraints(min_length=1)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileIn(_Strict):
    model: str | None = None
    effort: str | None = None


class IssueIn(_Strict):
    request_id: Text
    title: Text
    goal: Text
    acceptance: list[str] = Field(default_factory=list)
    cwd: Text
    base_sha: Sha
    opened_from: Literal["claude", "codex"]
    participants: list[Key] = Field(default_factory=lambda: ["claude", "codex"], min_length=1)


class HandleIn(_Strict):
    adapter_type: Literal["claude", "codex"]
    session_id: Text
    cwd: Text


class HandlePutIn(_Strict):
    generation: int
    attempt_id: str | None = None
    handle: HandleIn


class GenerationHandoffIn(_Strict):
    claim: Text
    evidence: Text
    correction: Text
    decision: Text


class GenerationIn(_Strict):
    expected_generation: int
    reason: Literal["resume_failed", "cwd_changed", "start_lost", "manual",
                    "context_threshold", "repeated_error"]
    expected_version: int
    handoff: GenerationHandoffIn | None = None

    @model_validator(mode="after")
    def issue_replacements_need_handoff(self):
        if self.reason in ("context_threshold", "repeated_error") and self.handoff is None:
            raise ValueError(f"{self.reason} requires a structured handoff")
        return self


class MessageIn(_Strict):
    message_id: Text
    kind: Literal["start", "continue"] = "continue"
    round_id: Text
    generation: int
    requested_profile: ProfileIn = Field(default_factory=ProfileIn)
    # where requested_profile came from (POST /profiles/resolve); null when it was not resolved
    profile_source: Literal["user_override", "task_type", "executor_default"] | None = None
    owner: Text
    prompt: Text


class ProfileResolveIn(_Strict):
    agent: Literal["claude", "codex"]
    task_type: Literal["simple", "feature", "design"]
    user_override: ProfileIn | None = None


class ProcessIdentityIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    host: Text
    pid: int
    start_time: Text
    platform: str | None = None
    group: str | None = None


class ProcessIn(_Strict):
    owner: Text
    process_identity: ProcessIdentityIn


class OwnerIn(_Strict):
    owner: Text


class TerminationIn(_Strict):
    owner: Text
    process_identity: ProcessIdentityIn | None = None
    method: Literal["exited", "terminated", "never_started"]
    observed_at: str | None = None


class ResultIn(_Strict):
    owner: Text
    status: Literal["done", "failed"]
    text: str | None = None
    exit_code: int | None = None
    confirmed_profile: ProfileIn | None = None
    context: dict | None = None
    limits: dict | None = None
    error: str | None = None


class RetryIn(_Strict):
    expected_attempt: Text
    owner: Text


class AbandonIn(_Strict):
    expected_attempt: Text


class ProposalIn(_Strict):
    round_id: Text
    by: Literal["claude", "codex", "coordinator"]
    text: Text
    expected_version: int


class RevisionIn(_Strict):
    text: Text
    expected_proposal_version: int
    expected_version: int


class ReadyIn(_Strict):
    round_id: Text
    generation: int
    attempt_id: Text
    source_commit_sha: Sha
    expected_base_sha: Sha
    expected_version: int


class DecisionIn(_Strict):
    round_id: Text
    proposal_id: Text
    proposal_version: int
    kind: Literal["accept", "reject", "request_changes", "new_round"]
    by: Literal["claude", "codex", "coordinator"]
    note: str | None = None
    expected_version: int


class IntegrationIn(_Strict):
    integration_request_id: Text
    round_id: Text
    proposal_id: Text
    proposal_version: int
    source_shas: dict[Key, Sha]
    expected_base_sha: Sha
    verification_argv: list[str]
    owner: Text
    expected_version: int


class TakeoverIn(_Strict):
    expected_owner: Text
    owner: Text
    expected_stage: Text


class CandidateIn(_Strict):
    owner: Text
    status: Literal["merged", "conflict"]
    candidate_sha: Sha | None = None
    output: dict | None = None
    detail: str | None = None


class TestsIn(_Strict):
    owner: Text
    tested_sha: Sha
    argv: list[str]
    status: Literal["pass", "fail"]
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    duration_s: float | None = None


class AdvanceIn(_Strict):
    owner: Text
    expected_version: int


class IntegrationResultIn(_Strict):
    owner: Text
    status: Literal["advanced", "failed"]
    new_base_sha: Sha | None = None
    error: str | None = None
    ref_update_output: dict | None = None


class CloseIn(_Strict):
    reason: Text
    expected_version: int


def _task_dict(row) -> dict:
    return {k: row[k] for k in ("id", "description", "status", "agent", "model_hint",
                                "error", "retry_count", "created_at", "updated_at")}


def _task_full(row) -> dict:
    return {**_task_dict(row), "result": row["result"]}


def create_app(db_path: str, agents: list[str] | None = None, *,
               clock: Callable[[], datetime] | None = None,
               attempt_ttl_s: int | None = None, test_job_store=None) -> FastAPI:
    read_codex_usage = os.environ.get("A2A_LINK_READ_CODEX_USAGE", "1") != "0"
    agents = agents or DEFAULT_AGENTS
    clock = clock or (lambda: datetime.now(timezone.utc))
    if attempt_ttl_s is None:
        attempt_ttl_s = int(os.environ.get("A2A_LINK_ATTEMPT_TTL_S", "900"))
    conn = get_connection(db_path)
    init_schema(conn)
    lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_app):
        with write_tx(db_path) as recovery_conn:
            repo.reconcile_expired_test_job_leases(recovery_conn, repo.ts(clock()))
        yield

    app = FastAPI(title="A2A_Link balancer", lifespan=lifespan)
    # Only runner routes may be reached from the tailnet; everything else is direct loopback.
    app.add_middleware(TrustBoundaryMiddleware)

    @app.exception_handler(repo.ApiError)
    async def _api_error(request: Request, exc: repo.ApiError):
        # RFC 9110: a 401 names the scheme the client must use (runner/admin bearer tokens).
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(exc.body(), status_code=exc.status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        if request.url.path.startswith(("/issues", "/profiles", "/test-snapshots", "/test-requests",
                                        "/test-jobs", "/test-deliveries", "/runners")):
            return JSONResponse({"error": "validation_error", "detail": "invalid request body",
                                 "errors": json.loads(json.dumps(exc.errors(), default=str))},
                                status_code=422)
        return await request_validation_exception_handler(request, exc)

    def run(fn) -> JSONResponse:
        """One request = one BEGIN IMMEDIATE transaction on its own connection.

        Correctness comes from the database (transaction, compare-and-swap, unique
        indexes), not from the in-process lock, so any number of balancer processes
        can share one SQLite file.
        """
        now_dt = clock()
        now = repo.ts(now_dt)
        lease_until = repo.ts(now_dt + timedelta(seconds=attempt_ttl_s))
        with write_tx(db_path) as c:
            repo.expire_leases(c, now)
            status, body = fn(c, now, lease_until)
        return JSONResponse(body, status_code=status)

    def usage_view() -> list[dict]:
        """Usage windows: Codex from session files, Claude from rate-limit events."""
        codex = codex_windows() if read_codex_usage else []
        with lock:
            for w in codex:
                if w["window"] == "5h":  # keep the router's view current too
                    started = datetime.fromtimestamp((w["resets_at"] or time.time()) - 5 * 3600,
                                                     timezone.utc).isoformat()
                    repo.report_usage(conn, "codex", w["pct_used"], started)
            rows = []
            for r in repo.list_usage(conn):
                if r["pct_used"] is not None:
                    row = {"agent": r["agent"], "window": "5h", "pct_used": r["pct_used"],
                           "resets_at": None}
                    try:
                        row["resets_at"] = datetime.fromisoformat(r["window_started_at"]).timestamp() + 5 * 3600
                    except (TypeError, ValueError):
                        pass
                    rows.append(row)
                if r["weekly_pct_used"] is not None:
                    weekly_pct = r["weekly_pct_used"]
                    if r["weekly_resets_at"] and r["weekly_resets_at"] < time.time():
                        weekly_pct = 0.0
                    rows.append({"agent": f'{r["agent"]} (7d)', "window": "7d",
                                 "pct_used": weekly_pct, "resets_at": r["weekly_resets_at"]})
        by_agent = {r["agent"]: r for r in rows}
        for w in codex:
            by_agent[w["agent"]] = {k: w[k] for k in ("agent", "window", "pct_used", "resets_at")}
        return sorted(by_agent.values(), key=lambda r: r["agent"])

    def issues_view() -> list[dict]:
        try:
            with write_tx(db_path) as c:
                repo.expire_leases(c, repo.ts(clock()))
                return [repo.issue_snapshot(c, i) for i in repo.list_issue_ids(c)]
        except Exception:  # the v1 dashboard data must never fail because of v2 data
            return []

    def snapshot() -> dict:
        usage = usage_view()
        with lock:
            data = {
                "tasks": [_task_dict(r) for r in repo.list_tasks(conn)],
                "usage": usage,
                "incidents": [dict(r) for r in repo.list_incidents(conn)],
            }
        data["issues"] = issues_view()
        return data

    @app.get("/usage")
    def get_usage():
        return usage_view()

    @app.post("/tasks", status_code=201)
    def create_task(body: TaskIn):
        task_id = uuid.uuid4().hex[:12]
        with lock:
            suggested = choose_agent(conn, agents, body.required_capability)
            if body.required_capability and not any(
                repo.has_capability(conn, a, body.required_capability) for a in agents
            ):
                repo.add_task(conn, task_id, body.description, body.model_hint,
                              status="blocked", error="missing-capability")
                return {"id": task_id, "status": "blocked", "agent": None,
                        "error": "missing-capability"}
            repo.add_task(conn, task_id, body.description, body.model_hint)
        return {"id": task_id, "status": "pending", "agent": suggested}

    @app.get("/tasks")
    def get_tasks(status: str | None = None):
        with lock:
            return [_task_dict(r) for r in repo.list_tasks(conn, status)]

    def _require(task_id: str):
        if repo.get_task(conn, task_id) is None:
            raise HTTPException(404, f"task {task_id!r} not found")

    @app.post("/tasks/{task_id}/claim")
    def claim(task_id: str, body: ClaimIn):
        with lock:
            _require(task_id)
            return {"claimed": repo.claim_task(conn, task_id, body.agent)}

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str):
        with lock:
            _require(task_id)
            return _task_full(repo.get_task(conn, task_id))

    @app.get("/tasks/{task_id}/log", response_class=PlainTextResponse)
    def task_log(task_id: str, lines: int = 200):
        try:
            text = read_tail(task_id, lines)
        except ValueError:
            raise HTTPException(400, "invalid task id") from None
        if text is None:
            raise HTTPException(404, "no log for this task")
        return text

    @app.post("/tasks/{task_id}/complete")
    def complete(task_id: str, body: CompleteIn | None = None):
        with lock:
            _require(task_id)
            repo.complete_task(conn, task_id, body.result if body else None)
        return {"status": "done"}

    @app.post("/tasks/{task_id}/fail")
    def fail(task_id: str, body: FailIn):
        with lock:
            _require(task_id)
            return {"status": repo.fail_task(conn, task_id, body.error)}

    @app.post("/incidents", status_code=201)
    def incident(body: IncidentIn):
        with lock:
            return {"id": repo.add_incident(conn, body.agent, body.kind, body.detail)}

    @app.get("/incidents")
    def incidents():
        with lock:
            return [dict(r) for r in repo.list_incidents(conn)]

    @app.post("/usage")
    def usage(body: UsageIn):
        with lock:
            repo.report_usage(conn, body.agent, body.pct_used, body.window_started_at,
                              body.weekly_pct_used, body.weekly_resets_at)
        return {"ok": True}

    @app.put("/agents/{agent}/capabilities")
    def capabilities(agent: str, body: CapabilitiesIn):
        with lock:
            repo.set_capabilities(conn, agent, body.capabilities)
        return {"ok": True}

    # --- issue coordination API --------------------------------------------------------

    def _root() -> str:
        return os.environ.get("A2A_LINK_HOME") or str(Path.home() / ".a2a_link")

    @app.post("/profiles/resolve")
    def profiles_resolve(body: ProfileResolveIn):
        """user override > task type profile > executor default; 422 instead of a silent fallback."""
        override = body.user_override.model_dump() if body.user_override else None
        try:
            profile, source = resolve_profile(body.agent, body.task_type, override)
        except ProfileError as e:
            return JSONResponse({"error": e.code, "detail": e.detail}, status_code=422)
        return {"requested_profile": profile, "source": source}

    @app.post("/issues")
    def issues_create(body: IssueIn):
        return run(lambda c, now, lease: repo.create_issue(c, now, _root(), body.model_dump()))

    @app.get("/issues/{issue_id}")
    def issues_get(issue_id: str):
        return run(lambda c, now, lease: (200, repo.issue_snapshot(c, issue_id)))

    @app.put("/issues/{issue_id}/participants/{key}/handle")
    def issues_handle(issue_id: str, key: str, body: HandlePutIn):
        return run(lambda c, now, lease: repo.save_handle(c, now, issue_id, key, body.model_dump()))

    @app.post("/issues/{issue_id}/participants/{key}/generation")
    def issues_generation(issue_id: str, key: str, body: GenerationIn):
        return run(lambda c, now, lease: repo.bump_generation(c, now, issue_id, key, body.model_dump()))

    @app.post("/issues/{issue_id}/participants/{key}/messages")
    def issues_message(issue_id: str, key: str, body: MessageIn):
        return run(lambda c, now, lease: repo.reserve_message(c, now, lease, issue_id, key,
                                                              body.model_dump()))

    @app.get("/issues/{issue_id}/participants/{key}/messages")
    def issues_messages(issue_id: str, key: str):
        return run(lambda c, now, lease: repo.list_messages(c, issue_id, key))

    @app.post("/issues/{issue_id}/participants/{key}/ready")
    def issues_ready(issue_id: str, key: str, body: ReadyIn):
        return run(lambda c, now, lease: repo.mark_ready(c, now, issue_id, key, body.model_dump()))

    @app.post("/issues/{issue_id}/attempts/{attempt_id}/process")
    def attempts_process(issue_id: str, attempt_id: str, body: ProcessIn):
        return run(lambda c, now, lease: repo.attempt_process(c, now, issue_id, attempt_id,
                                                              body.model_dump()))

    @app.post("/issues/{issue_id}/attempts/{attempt_id}/heartbeat")
    def attempts_heartbeat(issue_id: str, attempt_id: str, body: OwnerIn):
        return run(lambda c, now, lease: repo.attempt_heartbeat(c, now, lease, issue_id, attempt_id,
                                                                body.model_dump()))

    @app.post("/issues/{issue_id}/attempts/{attempt_id}/termination_confirmed")
    def attempts_termination(issue_id: str, attempt_id: str, body: TerminationIn):
        return run(lambda c, now, lease: repo.attempt_termination(c, now, issue_id, attempt_id,
                                                                  body.model_dump()))

    @app.post("/issues/{issue_id}/attempts/{attempt_id}/result")
    def attempts_result(issue_id: str, attempt_id: str, body: ResultIn):
        return run(lambda c, now, lease: repo.attempt_result(c, now, issue_id, attempt_id,
                                                             body.model_dump()))

    @app.post("/issues/{issue_id}/messages/{message_id}/retry")
    def messages_retry(issue_id: str, message_id: str, body: RetryIn):
        return run(lambda c, now, lease: repo.retry_message(c, now, lease, issue_id, message_id,
                                                            body.model_dump()))

    @app.post("/issues/{issue_id}/messages/{message_id}/abandon")
    def messages_abandon(issue_id: str, message_id: str, body: AbandonIn):
        return run(lambda c, now, lease: repo.abandon_message(c, now, issue_id, message_id,
                                                              body.model_dump()))

    @app.post("/issues/{issue_id}/proposals")
    def proposals_create(issue_id: str, body: ProposalIn):
        return run(lambda c, now, lease: repo.create_proposal(c, now, issue_id, body.model_dump()))

    @app.post("/issues/{issue_id}/proposals/{proposal_id}/revisions")
    def proposals_revise(issue_id: str, proposal_id: str, body: RevisionIn):
        return run(lambda c, now, lease: repo.revise_proposal(c, now, issue_id, proposal_id,
                                                              body.model_dump()))

    @app.post("/issues/{issue_id}/decisions")
    def decisions_create(issue_id: str, body: DecisionIn):
        return run(lambda c, now, lease: repo.decide(c, now, issue_id, body.model_dump()))

    @app.post("/issues/{issue_id}/integrations")
    def integrations_start(issue_id: str, body: IntegrationIn):
        return run(lambda c, now, lease: repo.start_integration(c, now, lease, issue_id,
                                                                body.model_dump()))

    @app.post("/issues/{issue_id}/integrations/{integration_id}/process")
    def integrations_process(issue_id: str, integration_id: str, body: ProcessIn):
        return run(lambda c, now, lease: repo.integration_process(c, now, issue_id, integration_id,
                                                                  body.model_dump()))

    @app.post("/issues/{issue_id}/integrations/{integration_id}/heartbeat")
    def integrations_heartbeat(issue_id: str, integration_id: str, body: OwnerIn):
        return run(lambda c, now, lease: repo.integration_heartbeat(c, now, lease, issue_id,
                                                                    integration_id, body.model_dump()))

    @app.post("/issues/{issue_id}/integrations/{integration_id}/termination_confirmed")
    def integrations_termination(issue_id: str, integration_id: str, body: TerminationIn):
        return run(lambda c, now, lease: repo.integration_termination(c, now, issue_id,
                                                                      integration_id, body.model_dump()))

    @app.post("/issues/{issue_id}/integrations/{integration_id}/takeover")
    def integrations_takeover(issue_id: str, integration_id: str, body: TakeoverIn):
        return run(lambda c, now, lease: repo.integration_takeover(c, now, lease, issue_id,
                                                                   integration_id, body.model_dump()))

    @app.post("/issues/{issue_id}/integrations/{integration_id}/candidate")
    def integrations_candidate(issue_id: str, integration_id: str, body: CandidateIn):
        return run(lambda c, now, lease: repo.integration_candidate(c, now, issue_id, integration_id,
                                                                    body.model_dump()))

    @app.post("/issues/{issue_id}/integrations/{integration_id}/tests")
    def integrations_tests(issue_id: str, integration_id: str, body: TestsIn):
        return run(lambda c, now, lease: repo.integration_tests(c, now, issue_id, integration_id,
                                                                body.model_dump()))

    @app.post("/issues/{issue_id}/integrations/{integration_id}/advance")
    def integrations_advance(issue_id: str, integration_id: str, body: AdvanceIn):
        return run(lambda c, now, lease: repo.integration_advance(c, now, issue_id, integration_id,
                                                                  body.model_dump()))

    @app.post("/issues/{issue_id}/integrations/{integration_id}/result")
    def integrations_result(issue_id: str, integration_id: str, body: IntegrationResultIn):
        return run(lambda c, now, lease: repo.integration_result(c, now, issue_id, integration_id,
                                                                 body.model_dump()))

    @app.post("/issues/{issue_id}/close")
    def issues_close(issue_id: str, body: CloseIn):
        return run(lambda c, now, lease: repo.close_issue(c, now, issue_id, body.model_dump()))

    # --- background test jobs ----------------------------------------------------------
    register_test_job_routes(app, db_path, store=test_job_store)

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        return (STATIC_DIR / "dashboard.html").read_text(encoding="utf-8")

    @app.get("/events")
    async def events(once: bool = False):
        async def gen():
            while True:
                yield f"data: {json.dumps(snapshot())}\n\n"
                if once:
                    return
                await asyncio.sleep(2)

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def _default_app() -> FastAPI:
    return create_app(os.environ.get("A2A_LINK_DB", "a2a_link.db"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(_default_app(), host="127.0.0.1", port=8000)
