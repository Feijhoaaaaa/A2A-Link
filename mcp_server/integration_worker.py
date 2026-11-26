"""Isolated integration worker; its process tree is owned by the supervisor Job/group."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from mcp_server.git_integration import GitIntegrator
from mcp_server.issue_service import IssueAPIError, IssueService
from mcp_server.supervisor import SupervisorRuntime


def _retry(call, timeout_s: float = 300):
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


def run(config: dict[str, Any]) -> int:
    service = IssueService(config["balancer_url"])
    runtime = SupervisorRuntime(config["supervisor_dir"])
    deadline = time.monotonic() + 10
    while not runtime.acquire():
        if time.monotonic() >= deadline:
            return 3
        time.sleep(0.05)
    journal = runtime.journal
    runtime.release()
    integration_id = config["integration_id"]
    issue_id = config["issue_id"]
    owner = config["owner"]
    stop = threading.Event()

    def heartbeat_loop():
        while not stop.wait(float(os.environ.get("A2A_LINK_ISSUE_HEARTBEAT_S", "30"))):
            try:
                _retry(lambda: service.integration_heartbeat(issue_id, integration_id, owner=owner))
            except Exception:
                continue

    heartbeat = threading.Thread(target=heartbeat_loop, name="issue-integration-heartbeat", daemon=True)
    heartbeat.start()
    integrator = GitIntegrator(config["a2a_link_home"], runtime)
    try:
        stages = [row for row in journal.read() if row.get("integration_id") == integration_id]
        merge_done = next((row for row in reversed(stages) if row["event"] == "merge_done"), None)
        tests_done = next((row for row in reversed(stages) if row["event"] == "tests_done"), None)
        ref_intent = next((row for row in reversed(stages) if row["event"] == "ref_update_intent"), None)
        ref_done = next((row for row in reversed(stages) if row["event"] == "ref_update_done"), None)

        if merge_done:
            merged = {"status": merge_done["status"], "candidate_sha": merge_done.get("candidate_sha"),
                      "output": merge_done["output"], "worktree": merge_done.get("worktree"),
                      "ref": merge_done.get("ref")}
        else:
            merged = integrator.merge_candidate(config["repo"], issue_id, integration_id,
                                                config["expected_base_sha"], config["source_shas"])
            journal.append("merge_done", integration_id=integration_id,
                           candidate_sha=merged.get("candidate_sha"), output=merged["output"],
                           status=merged["status"], worktree=merged.get("worktree"),
                           ref=merged.get("ref"))
        _retry(lambda: service.integration_candidate(issue_id, integration_id, owner=owner,
                                                      status=merged["status"],
                                                      candidate_sha=merged.get("candidate_sha"),
                                                      output=merged["output"], detail=merged.get("detail")))
        if merged["status"] != "merged":
            journal.append("integration_terminal", integration_id=integration_id, state="conflict")
            return 0

        candidate_sha = merged["candidate_sha"]
        if tests_done:
            tests = {key: value for key, value in tests_done.items()
                     if key in {"status", "exit_code", "stdout", "stderr", "duration_s", "truncated"}}
            tests["argv"] = tests_done.get("argv", config["verification_argv"])
            tested_sha = tests_done.get("candidate_sha", candidate_sha)
        else:
            tests = integrator.verify(merged["worktree"], config["verification_argv"],
                                      timeout_s=float(os.environ.get("A2A_LINK_ISSUE_TEST_TIMEOUT_S", "1800")))
            tested_sha = candidate_sha
            journal.append("tests_done", integration_id=integration_id, candidate_sha=candidate_sha,
                           argv=config["verification_argv"], **tests)
        _retry(lambda: service.integration_tests(issue_id, integration_id, owner=owner,
                                                  tested_sha=tested_sha,
                                                  argv=tests.get("argv", config["verification_argv"]),
                                                  **{key: value for key, value in tests.items() if key != "argv"}))
        if tests["status"] != "pass":
            journal.append("integration_terminal", integration_id=integration_id, state="failed")
            return 0

        candidate_sha = merged["candidate_sha"]
        if not ref_intent and not ref_done:
            try:
                integrator.preflight_participant_worktrees(config["repo"], issue_id,
                                                           config["source_shas"], candidate_sha)
            except Exception as exc:
                error = f"participant worktree preflight failed: {exc}"
                journal.append("worktree_sync_blocked", integration_id=integration_id, error=error)
                _retry(lambda: service.integration_result(issue_id, integration_id, owner=owner,
                                                           status="failed", error=error,
                                                           ref_update_output={"exit_code": 1, "stdout": "",
                                                                              "stderr": error}))
                journal.append("integration_terminal", integration_id=integration_id, state="failed")
                return 1

        if ref_done:
            ref_output = ref_done["output"]
        else:
            if ref_intent:
                ref = ref_intent["ref"]
            else:
                ref = merged["ref"]
                snapshot = _retry(lambda: service.snapshot(issue_id))
                _retry(lambda: service.advance_integration(issue_id, integration_id, owner=owner,
                                                            expected_version=snapshot["issue"]["version"]))
                journal.append("ref_update_intent", integration_id=integration_id,
                               expected_base_sha=config["expected_base_sha"], candidate_sha=candidate_sha,
                               ref=ref)
            current_ref = integrator.ref_value(config["repo"], ref)
            if current_ref == candidate_sha:
                # The compare-and-swap completed before the previous worker died.
                ref_output = {"exit_code": 0, "stdout": "ref already points to candidate",
                              "stderr": "", "truncated": False}
            elif current_ref == config["expected_base_sha"]:
                try:
                    integrator.preflight_participant_worktrees(config["repo"], issue_id,
                                                               config["source_shas"], candidate_sha)
                except Exception as exc:
                    error = f"participant worktree preflight failed before ref CAS: {exc}"
                    _retry(lambda: service.integration_result(issue_id, integration_id, owner=owner,
                                                               status="failed", error=error,
                                                               ref_update_output={"exit_code": 1, "stdout": "",
                                                                                  "stderr": error}))
                    journal.append("integration_terminal", integration_id=integration_id, state="failed")
                    return 1
                try:
                    ref_output = integrator.advance_ref(config["repo"], ref, candidate_sha,
                                                        config["expected_base_sha"]).as_dict()
                except Exception as exc:
                    _retry(lambda: service.integration_result(issue_id, integration_id, owner=owner,
                                                               status="failed", error=str(exc),
                                                               ref_update_output={"exit_code": 1, "stdout": "",
                                                                                  "stderr": str(exc)}))
                    journal.append("integration_terminal", integration_id=integration_id, state="failed")
                    return 1
            else:
                error = f"diverged ref {ref}: found {current_ref}, expected {config['expected_base_sha']} or {candidate_sha}"
                _retry(lambda: service.integration_result(issue_id, integration_id, owner=owner,
                                                           status="failed", error=error,
                                                           ref_update_output={"exit_code": 1, "stdout": "",
                                                                              "stderr": error}))
                journal.append("integration_terminal", integration_id=integration_id, state="diverged")
                return 1
            journal.append("ref_update_done", integration_id=integration_id,
                           candidate_sha=candidate_sha, ref=ref, output=ref_output)
        if integrator.ref_value(config["repo"], ref if not ref_done else ref_done["ref"]) != candidate_sha:
            raise RuntimeError("base ref no longer points to the tested integration candidate")
        participant_sync = integrator.sync_participant_worktrees(
            config["repo"], issue_id, integration_id, config["source_shas"], candidate_sha, journal,
        )
        journal.append("worktrees_synced", integration_id=integration_id,
                       candidate_sha=candidate_sha, participants=participant_sync)
        _retry(lambda: service.integration_result(issue_id, integration_id, owner=owner,
                                                   status="advanced", new_base_sha=candidate_sha,
                                                   ref_update_output=ref_output))
        journal.append("integration_result_posted", integration_id=integration_id, status="advanced")
        return 0
    except Exception as exc:
        journal.append("integration_unresolved", integration_id=integration_id,
                       error=f"{type(exc).__name__}: {exc}")
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        stop.set()
        heartbeat.join(timeout=2)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        return 2
    try:
        config = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 2
    return run(config)


if __name__ == "__main__":
    raise SystemExit(main())
