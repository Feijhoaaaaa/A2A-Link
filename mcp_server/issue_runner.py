"""Persistent CLI issue turns; messages resume the participant's saved session."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_server.issue_adapters import (ClaudeAdapter, CodexAdapter, SessionHandle,
                                       parse_claude_events, parse_codex_events)
from mcp_server.issue_handoff import compose_generation_handoff
from mcp_server.issue_service import IssueAPIError, IssueService
from mcp_server.cli_executor import build_argv
from mcp_server.supervisor import (SupervisorRuntime, process_scope_empty,
                                   wait_for_process_scope)
from mcp_server.worktree import worktree_base


@dataclass
class TurnJob:
    attempt_id: str
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None


class IssueRunner:
    def __init__(self, service: IssueService, runtime: SupervisorRuntime):
        self.service = service
        self.runtime = runtime
        self.jobs: dict[str, TurnJob] = {}
        self._jobs_lock = threading.Lock()
        self.adapters = {"claude": ClaudeAdapter(), "codex": CodexAdapter()}

    def send(self, issue_id: str, participant: str, prompt: str, task_type: str,
             user_override: dict[str, Any] | None = None, message_id: str | None = None,
             wait_s: float = 0) -> dict[str, Any]:
        self.recover_after_restart()
        if participant not in self.adapters:
            raise ValueError("participant must be claude or codex")
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        snapshot = self.service.snapshot(issue_id)
        person = snapshot["participants"][participant]
        generation = person["generation"]
        handle_data = person.get("handle")
        kind = "continue" if handle_data else "start"
        effective_prompt = (compose_generation_handoff(snapshot, participant, generation, prompt)
                            if kind == "start" else prompt)
        cwd = handle_data["cwd"] if handle_data else str(worktree_base() / f"{issue_id}-{participant}")
        if not os.path.isdir(cwd):
            raise FileNotFoundError(f"managed participant worktree does not exist: {cwd}")
        profile = self.service.resolve_profile(participant, task_type, user_override)
        msg_id = message_id or str(uuid.uuid4())
        round_id = snapshot["round"]["id"]
        if not self.runtime.acquire():
            raise RuntimeError("another supervisor action is in progress for this install")
        try:
            reservation = self.service.reserve_message(
                issue_id, participant, message_id=msg_id, kind=kind, round_id=round_id,
                generation=generation, requested_profile=profile["requested_profile"],
                profile_source=profile["source"], owner=self.runtime.owner, prompt=effective_prompt,
            )
            attempt = reservation["attempt"]
            if not reservation.get("execute"):
                return {"status": reservation.get("state", "active"), "execute": False,
                        "message_id": msg_id, "attempt_id": attempt["attempt_id"]}
            adapter = self.adapters[participant]
            if kind == "start":
                argv = adapter.start_argv(effective_prompt, profile["requested_profile"])
            else:
                handle = SessionHandle(adapter_type=handle_data["adapter_type"],
                                       session_id=handle_data["session_id"], cwd=handle_data["cwd"],
                                       generation=handle_data["generation"])
                argv = adapter.continue_argv(handle, effective_prompt, profile["requested_profile"])
            resolved_argv, cli_error = build_argv(argv)
            if resolved_argv is None:
                self.runtime.journal.append("spawn_failed", attempt_id=attempt["attempt_id"], error=cli_error)
                self._never_started(issue_id, attempt["attempt_id"])
                return {"status": "terminated", "message_id": msg_id,
                        "attempt_id": attempt["attempt_id"], "error": cli_error}
            try:
                proc, identity, job, keeper = self.runtime.spawn(
                    attempt["attempt_id"], msg_id, resolved_argv, cwd,
                    metadata={"issue_id": issue_id, "participant": participant,
                              "kind": kind, "generation": generation,
                              "requested_profile": profile["requested_profile"]},
                )
            except Exception as exc:
                records = self.runtime.journal.read()
                attempt_rows = [row for row in records if row.get("attempt_id") == attempt["attempt_id"]]
                if any(row["event"] == "spawn_intent" for row in attempt_rows) and not any(
                        row["event"] == "spawn_failed" for row in attempt_rows):
                    self.runtime.journal.append("unresolved", attempt_id=attempt["attempt_id"],
                                                reason=f"spawn state cannot be proven: {exc}")
                    return {"status": "unresolved", "message_id": msg_id,
                            "attempt_id": attempt["attempt_id"], "error": str(exc)}
                self._never_started(issue_id, attempt["attempt_id"])
                return {"status": "terminated", "message_id": msg_id,
                        "attempt_id": attempt["attempt_id"], "error": str(exc)}
            self._retry_api(lambda: self.service.report_process(
                issue_id, attempt["attempt_id"], owner=self.runtime.owner,
                process_identity=identity,
            ))
            self.runtime.journal.append("process_reported", attempt_id=attempt["attempt_id"])
        finally:
            self.runtime.release()

        job_state = TurnJob(attempt["attempt_id"])
        with self._jobs_lock:
            self.jobs[job_state.attempt_id] = job_state
        worker = threading.Thread(
            target=self._finish_turn,
            args=(issue_id, participant, kind, generation, attempt["attempt_id"], msg_id,
                  profile, cwd, proc, identity, job, keeper, job_state),
            name=f"a2a-issue-{attempt['attempt_id']}", daemon=True,
        )
        worker.start()
        if wait_s > 0 and job_state.done.wait(min(wait_s, 55)):
            return job_state.result or {"status": "failed", "attempt_id": attempt["attempt_id"]}
        return {"status": "in_progress", "execute": True, "message_id": msg_id,
                "attempt_id": attempt["attempt_id"]}

    def recover_after_restart(self) -> list[dict[str, Any]]:
        """Reattach journaled attempts after MCP restart and fail closed on uncertain scopes."""
        records = self.runtime.journal.read()
        by_attempt: dict[str, list[dict[str, Any]]] = {}
        for row in records:
            if row.get("attempt_id"):
                by_attempt.setdefault(row["attempt_id"], []).append(row)
        recovered = []
        for attempt_id, rows in by_attempt.items():
            intent = next((row for row in rows if row["event"] == "spawn_intent"), None)
            if not intent or any(row["event"] in ("result_posted", "termination_posted",
                                                   "integration_result_posted", "integration_terminal")
                                 for row in rows):
                continue
            with self._jobs_lock:
                local_job = self.jobs.get(attempt_id)
            if local_job and not local_job.done.is_set():
                continue
            issue_id, participant = intent.get("issue_id"), intent.get("participant")
            if intent.get("kind") == "integration":
                try:
                    recovered_integration = self._recover_integration(attempt_id, rows, intent)
                    if recovered_integration:
                        recovered.append(recovered_integration)
                except Exception as exc:
                    self.runtime.journal.append("unresolved", attempt_id=attempt_id,
                                                integration_id=intent.get("integration_id"),
                                                reason=f"integration recovery failed: {type(exc).__name__}: {exc}")
                continue
            if not issue_id or participant not in self.adapters:
                self.runtime.journal.append("unresolved", attempt_id=attempt_id,
                                            reason="missing issue or participant metadata")
                continue
            if not self.runtime.acquire():
                continue
            try:
                snapshot = self.service.snapshot(issue_id)
                attempt = next((item for item in snapshot.get("attempts", [])
                                if item.get("attempt_id") == attempt_id), None)
                if not attempt or attempt.get("state") in ("done", "failed", "superseded", "abandoned"):
                    continue
                spawned = next((row for row in reversed(rows) if row["event"] == "spawned"), None)
                spawn_failed = any(row["event"] == "spawn_failed" for row in rows)
                if not spawned and not spawn_failed:
                    self.runtime.journal.append("unresolved", attempt_id=attempt_id,
                                                reason="crash after spawn intent; child identity is unknown")
                    continue
                identity = spawned.get("process_identity") if spawned else None
                session = next((row for row in reversed(rows) if row["event"] == "session_started"), None)
                handle_saved = any(row["event"] == "handle_saved" for row in rows)
                if intent.get("kind") == "start" and not session:
                    session = self.runtime.capture_session(
                        attempt_id, participant, int(intent["generation"]), intent["cwd"],
                        intent.get("requested_profile"),
                    )
                if identity:
                    method = self.runtime.recover(
                        attempt_id, identity,
                        grace_s=float(os.environ.get("A2A_LINK_REATTACH_GRACE_S", "30")),
                    )
                else:
                    method = "never_started"
                if intent.get("kind") == "start" and not handle_saved and not session:
                    session = self.runtime.capture_session(
                        attempt_id, participant, int(intent["generation"]), intent["cwd"],
                        intent.get("requested_profile"),
                    )
                if session and not handle_saved:
                    self._retry_api(lambda: self.service.save_handle(
                        issue_id, participant, generation=int(session["generation"]),
                        attempt_id=attempt_id,
                        handle={"adapter_type": participant, "session_id": session["session_id"],
                                "cwd": session["cwd"]},
                    ))
                    self.runtime.handle_saved(attempt_id)
                self._retry_api(lambda: self.service.termination_confirmed(
                    issue_id, attempt_id, owner=self.runtime.owner,
                    process_identity=identity, method=method,
                    observed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                ))
                self.runtime.journal.append("termination_posted", attempt_id=attempt_id,
                                            method=method)
                if intent.get("kind") == "start" and not session:
                    self._retry_api(lambda: self.service.result(
                        issue_id, attempt_id, owner=self.runtime.owner, status="failed",
                        text="", exit_code=1, confirmed_profile={"model": None, "effort": None},
                        context=None, limits=None, error="session_not_captured",
                    ))
                    self.runtime.journal.append("result_posted", attempt_id=attempt_id)
                recovered.append({"attempt_id": attempt_id, "method": method,
                                  "session_recovered": bool(session)})
            except Exception as exc:
                self.runtime.journal.append("unresolved", attempt_id=attempt_id,
                                            reason=f"recovery failed: {type(exc).__name__}: {exc}")
            finally:
                self.runtime.release()
        return recovered

    def _recover_integration(self, integration_id: str, rows: list[dict[str, Any]],
                             intent: dict[str, Any]) -> dict[str, Any] | None:
        issue_id = intent.get("issue_id")
        config_path = self.runtime.directory / "integrations" / f"{integration_id}.json"
        if not issue_id or not config_path.is_file():
            self.runtime.journal.append("unresolved", attempt_id=integration_id,
                                        integration_id=integration_id,
                                        reason="integration recovery config is missing")
            return None
        if not self.runtime.acquire():
            return None
        try:
            snapshot = self.service.snapshot(issue_id)
            integration = next((row for row in snapshot.get("integrations", [])
                                if row.get("integration_id") == integration_id), None)
            if not integration:
                return None
            if integration.get("state") in ("advanced", "failed", "conflict", "diverged"):
                self.runtime.journal.append("integration_terminal", integration_id=integration_id,
                                            state=integration["state"])
                return None
            spawned = next((row for row in reversed(rows) if row["event"] == "spawned"), None)
            spawn_failed = any(row["event"] == "spawn_failed" for row in rows)
            if not spawned and not spawn_failed:
                self.runtime.journal.append("unresolved", attempt_id=integration_id,
                                            integration_id=integration_id,
                                            reason="integration worker spawn status is unknown")
                return None
            identity = spawned.get("process_identity") if spawned else None
            old_owner = integration.get("lease", {}).get("owner") or intent.get("owner") or self.runtime.owner
            if identity:
                method = self.runtime.recover(
                    integration_id, identity,
                    grace_s=float(os.environ.get("A2A_LINK_REATTACH_GRACE_S", "30")),
                )
            else:
                method = "never_started"
            self._retry_api(lambda: self.service.integration_termination_confirmed(
                issue_id, integration_id, owner=old_owner, process_identity=identity,
                method=method, observed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            ))
            self.runtime.journal.append("integration_termination_posted", integration_id=integration_id,
                                        method=method)
            stage = integration["state"]
            self._retry_api(lambda: self.service.takeover_integration(
                issue_id, integration_id, expected_owner=old_owner, owner=self.runtime.owner,
                expected_stage=stage,
            ))
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["owner"] = self.runtime.owner
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            self._launch_integration_locked(issue_id, integration_id, config["integration_request_id"],
                                            config, stage)
            return {"integration_id": integration_id, "method": method,
                    "stage": stage, "taken_over": True}
        finally:
            self.runtime.release()

    def _launch_integration_locked(self, issue_id: str, integration_id: str,
                                   integration_request_id: str, config: dict[str, Any],
                                   stage: str) -> None:
        source_root = str(Path(__file__).resolve().parent.parent)
        child_env = {**os.environ, "PYTHONPATH": source_root + os.pathsep + os.environ.get("PYTHONPATH", "")}
        proc, identity, job, keeper = self.runtime.spawn(
            integration_id, integration_request_id,
            [sys.executable, "-m", "mcp_server.integration_worker",
             str(self.runtime.directory / "integrations" / f"{integration_id}.json")],
            config["repo"], env=child_env,
            metadata={"kind": "integration", "issue_id": issue_id,
                      "integration_id": integration_id,
                      "integration_request_id": integration_request_id,
                      "owner": self.runtime.owner, "stage": stage},
        )
        self._retry_api(lambda: self.service.integration_process(
            issue_id, integration_id, owner=self.runtime.owner, process_identity=identity,
        ))
        self.runtime.journal.append("integration_process_reported", integration_id=integration_id)
        state = TurnJob(integration_id)
        with self._jobs_lock:
            self.jobs[integration_id] = state

        def watch():
            tree_empty = False
            try:
                proc.wait()
                tree_empty = self._drain_process_scope(proc, identity, job)
                if not tree_empty:
                    raise RuntimeError("integration worker exited with active descendants")
                self.runtime.journal.append("integration_worker_exit", integration_id=integration_id,
                                            exit_code=proc.returncode)
                state.result = {"status": "worker_exited", "integration_id": integration_id,
                                "exit_code": proc.returncode}
            except Exception as exc:
                state.result = {"status": "unresolved", "integration_id": integration_id,
                                "error": f"{type(exc).__name__}: {exc}"}
            finally:
                try:
                    tree_empty = tree_empty or process_scope_empty(identity, job)
                except Exception:
                    tree_empty = False
                if keeper is not None and tree_empty:
                    try:
                        keeper.release()
                    except Exception:
                        pass
                if job is not None:
                    job.close()
                state.done.set()

        threading.Thread(target=watch, name=f"a2a-integration-{integration_id}", daemon=True).start()

    def wait(self, attempt_id: str, wait_s: float = 0) -> dict[str, Any]:
        with self._jobs_lock:
            job = self.jobs.get(attempt_id)
        if job is None:
            return {"status": "unknown", "attempt_id": attempt_id,
                    "detail": "attempt is not owned by this MCP process; inspect issue snapshot"}
        if wait_s > 0:
            job.done.wait(min(wait_s, 55))
        return job.result or {"status": "in_progress", "attempt_id": attempt_id}

    def abandon_message(self, issue_id: str, message_id: str, expected_attempt: str) -> dict[str, Any]:
        """Release a failed/terminated message slot so a fresh prompt can continue its saved handle."""
        return self.service.abandon(issue_id, message_id, expected_attempt=expected_attempt)

    def integrate(self, issue_id: str, integration_request_id: str, round_id: str,
                  proposal_id: str, proposal_version: int, source_shas: dict[str, str],
                  expected_base_sha: str, verification_argv: list[str],
                  expected_version: int) -> dict[str, Any]:
        self.recover_after_restart()
        snapshot = self.service.snapshot(issue_id)
        repo = snapshot["issue"]["cwd"]
        if not self.runtime.acquire():
            raise RuntimeError("another supervisor action is in progress for this install")
        try:
            self.runtime.journal.append("integration_request_intent", issue_id=issue_id,
                                        integration_request_id=integration_request_id)
            started = self.service.start_integration(
                issue_id, integration_request_id=integration_request_id, round_id=round_id,
                proposal_id=proposal_id, proposal_version=proposal_version,
                source_shas=source_shas, expected_base_sha=expected_base_sha,
                verification_argv=verification_argv, owner=self.runtime.owner,
                expected_version=expected_version,
            )
            integration_id = started["integration_id"]
            if started.get("_http_status", 201) == 200:
                return {"status": "in_progress", "integration_id": integration_id,
                        "execute": False, "detail": "idempotent replay; existing integration was not started twice"}
            rows = [row for row in self.runtime.journal.read()
                    if row.get("integration_id") == integration_id]
            if any(row["event"] == "integration_result_posted" for row in rows):
                return {"status": "done", "integration_id": integration_id, "execute": False}
            if any(row["event"] == "spawn_intent" and row.get("kind") == "integration" for row in rows):
                return {"status": "in_progress", "integration_id": integration_id, "execute": False}
            config_dir = self.runtime.directory / "integrations"
            config_dir.mkdir(parents=True, exist_ok=True)
            config_path = config_dir / f"{integration_id}.json"
            config = {"balancer_url": self.service.base_url,
                      "supervisor_dir": str(self.runtime.directory),
                      "a2a_link_home": str(self.runtime.directory.parent), "repo": repo,
                      "integration_id": integration_id, "issue_id": issue_id,
                      "integration_request_id": integration_request_id,
                      "owner": self.runtime.owner, "expected_base_sha": expected_base_sha,
                      "source_shas": source_shas, "verification_argv": verification_argv}
            if config_path.exists():
                previous = json.loads(config_path.read_text(encoding="utf-8"))
                if previous != config:
                    raise RuntimeError("integration id already has a different local worker config")
            else:
                config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
            source_root = str(Path(__file__).resolve().parent.parent)
            child_env = {**os.environ, "PYTHONPATH": source_root + os.pathsep + os.environ.get("PYTHONPATH", "")}
            proc, identity, job, keeper = self.runtime.spawn(
                integration_id, integration_request_id,
                [sys.executable, "-m", "mcp_server.integration_worker", str(config_path)],
                repo, env=child_env,
                metadata={"kind": "integration", "issue_id": issue_id,
                          "integration_id": integration_id, "integration_request_id": integration_request_id,
                          "owner": self.runtime.owner, "stage": started.get("state", "merging")},
            )
            self._retry_api(lambda: self.service.integration_process(
                issue_id, integration_id, owner=self.runtime.owner, process_identity=identity,
            ))
            self.runtime.journal.append("integration_process_reported", integration_id=integration_id)
        finally:
            self.runtime.release()
        state = TurnJob(integration_id)
        with self._jobs_lock:
            self.jobs[integration_id] = state

        def watch():
            tree_empty = False
            try:
                proc.wait()
                tree_empty = self._drain_process_scope(proc, identity, job)
                if not tree_empty:
                    raise RuntimeError("integration worker exited with active descendants")
                self.runtime.journal.append("integration_worker_exit", integration_id=integration_id,
                                            exit_code=proc.returncode)
                state.result = {"status": "worker_exited", "integration_id": integration_id,
                                "exit_code": proc.returncode}
            except Exception as exc:
                state.result = {"status": "unresolved", "integration_id": integration_id,
                                "error": f"{type(exc).__name__}: {exc}"}
            finally:
                try:
                    tree_empty = tree_empty or process_scope_empty(identity, job)
                except Exception:
                    tree_empty = False
                if keeper is not None and tree_empty:
                    try:
                        keeper.release()
                    except Exception:
                        pass
                if job is not None:
                    job.close()
                state.done.set()

        threading.Thread(target=watch, name=f"a2a-integration-{integration_id}", daemon=True).start()
        return {"status": "in_progress", "integration_id": integration_id, "execute": True}

    def _finish_turn(self, issue_id: str, participant: str, kind: str, generation: int,
                     attempt_id: str, message_id: str, profile: dict[str, Any], cwd: str, proc,
                     identity: dict[str, Any], job, keeper, state: TurnJob) -> None:
        deadline = time.monotonic() + int(os.environ.get("A2A_LINK_ISSUE_TIMEOUT_S", "3600"))
        heartbeat_deadline = 0.0
        handle_saved = kind != "start"
        session_id = None
        timed_out = False
        tree_empty = False
        try:
            while proc.poll() is None or not process_scope_empty(identity, job):
                if not handle_saved:
                    if self.runtime.acquire():
                        try:
                            captured = self.runtime.capture_session(
                                attempt_id, participant, generation,
                                cwd,
                                profile["requested_profile"],
                            )
                            if captured:
                                session_id = captured["session_id"]
                                self._retry_api(lambda: self.service.save_handle(
                                    issue_id, participant, generation=generation, attempt_id=attempt_id,
                                    handle={"adapter_type": participant, "session_id": session_id,
                                            "cwd": captured["cwd"]},
                                ))
                                self.runtime.handle_saved(attempt_id)
                                handle_saved = True
                        finally:
                            self.runtime.release()
                if time.monotonic() >= deadline:
                    timed_out = True
                    self.runtime.acquire()
                    try:
                        self.runtime.recover(attempt_id, identity, grace_s=0)
                    finally:
                        self.runtime.release()
                    break
                now = time.monotonic()
                if now >= heartbeat_deadline:
                    try:
                        self.service.heartbeat(issue_id, attempt_id, owner=self.runtime.owner)
                    except Exception:
                        # Keep the process and lease owner alive across transient balancer restarts.
                        pass
                    heartbeat_deadline = now + float(os.environ.get("A2A_LINK_ISSUE_HEARTBEAT_S", "30"))
                time.sleep(2)
            proc.wait(timeout=2)
            tree_empty = process_scope_empty(identity, job)
            if not tree_empty:
                raise RuntimeError("unresolved: CLI leader exited but its supervised process scope is still active")
            if not handle_saved and kind == "start":
                if self.runtime.acquire():
                    try:
                        captured = self.runtime.capture_session(
                            attempt_id, participant, generation,
                            cwd,
                            profile["requested_profile"],
                        )
                        if captured:
                            session_id = captured["session_id"]
                            self._retry_api(lambda: self.service.save_handle(
                                issue_id, participant, generation=generation, attempt_id=attempt_id,
                                handle={"adapter_type": participant, "session_id": session_id,
                                        "cwd": captured["cwd"]},
                            ))
                            self.runtime.handle_saved(attempt_id)
                            handle_saved = True
                    finally:
                        self.runtime.release()
            output_path = self.runtime.directory / "attempts" / attempt_id / "stdout.jsonl"
            events = []
            if output_path.exists():
                for line in output_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            if participant == "codex":
                codex_id = CodexAdapter.session_id_from_event(next((e for e in events if e.get("type") == "thread.started"), {}))
                parsed = parse_codex_events(events, self._load_codex_transcript(codex_id) if codex_id else None)
                confirmed = parsed["confirmed_profile"]
                context = parsed["context"]
                error = parsed["error"]
                text = self._extract_text(events)
            else:
                parsed = parse_claude_events(events)
                confirmed = parsed["confirmed_profile"]
                context = parsed["context"]
                error = self._extract_error(events)
                text = self._extract_text(events)
            failed = timed_out or proc.returncode != 0 or (kind == "start" and not handle_saved)
            if kind == "start" and not handle_saved:
                error = error or "session_not_captured"
            if timed_out:
                error = error or "supervised process scope exceeded the turn timeout"
            if failed and not error:
                error = f"CLI exited with code {proc.returncode}"
            result = {"owner": self.runtime.owner, "status": "failed" if failed else "done",
                      "text": text, "exit_code": proc.returncode,
                      "confirmed_profile": confirmed, "context": context, "limits": None,
                      "error": error if failed else None}
            if self.runtime.acquire():
                try:
                    self._retry_api(lambda: self.service.result(issue_id, attempt_id, **result))
                    self.runtime.journal.append("result_posted", attempt_id=attempt_id)
                finally:
                    self.runtime.release()
            state.result = {"status": result["status"], "attempt_id": attempt_id,
                            "text": text, "error": result["error"]}
        except Exception as exc:
            state.result = {"status": "failed", "attempt_id": attempt_id,
                            "error": f"{type(exc).__name__}: {exc}"}
            self.runtime.journal.append("unresolved", attempt_id=attempt_id, reason=state.result["error"])
        finally:
            try:
                tree_empty = tree_empty or process_scope_empty(identity, job)
            except Exception:
                tree_empty = False
            if keeper is not None and tree_empty:
                try:
                    keeper.release()
                except Exception:
                    pass
            if job is not None:
                job.close()
            state.done.set()

    @staticmethod
    def _retry_api(call, timeout_s: float = 300):
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                return call()
            except IssueAPIError as exc:
                if exc.status_code < 500 or time.monotonic() >= deadline:
                    raise
            except Exception:
                if time.monotonic() >= deadline:
                    raise
            time.sleep(1)

    def _never_started(self, issue_id: str, attempt_id: str) -> None:
        self._retry_api(lambda: self.service.termination_confirmed(
            issue_id, attempt_id, owner=self.runtime.owner, process_identity=None,
            method="never_started", observed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ))

    @staticmethod
    def _drain_process_scope(proc, identity: dict[str, Any], job) -> bool:
        timeout = float(os.environ.get("A2A_LINK_PROCESS_DRAIN_TIMEOUT_S", "60"))
        if wait_for_process_scope(proc, identity, job, timeout_s=timeout):
            return True
        if job is not None:
            try:
                job.terminate()
                return job.wait_empty(10)
            except OSError:
                return False
        return False

    @staticmethod
    def _extract_text(events: list[dict[str, Any]]) -> str:
        pieces = []
        for event in events:
            if event.get("type") == "result" and isinstance(event.get("result"), str):
                pieces.append(event["result"])
            elif event.get("type") == "item.completed":
                item = event.get("item") or {}
                if item.get("type") in ("agent_message", "assistant_message") and isinstance(item.get("text"), str):
                    pieces.append(item["text"])
        return "\n".join(pieces)

    @classmethod
    def _extract_error(cls, events: list[dict[str, Any]]) -> str | None:
        for event in events:
            if event.get("type") in ("error", "turn.failed"):
                value = event.get("error") or event.get("message")
                if isinstance(value, dict):
                    value = value.get("message")
                if value:
                    return str(value)
        return None

    @staticmethod
    def _load_codex_transcript(session_id: str) -> list[dict[str, Any]] | None:
        root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
        if not root.exists():
            return None
        for path in sorted(root.rglob("*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                with path.open("r", encoding="utf-8", errors="replace") as stream:
                    first = stream.readline()
                    if session_id not in first:
                        continue
                    rows = [json.loads(first)]
                    for line in stream:
                        try:
                            rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
                    return rows
            except OSError:
                continue
        return None
