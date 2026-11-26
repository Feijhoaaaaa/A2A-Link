"""HTTP client for durable issue orchestration and executor reports."""
from __future__ import annotations

import os
import subprocess
from typing import Any
from pathlib import Path

import httpx


class IssueAPIError(RuntimeError):
    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self.payload = payload
        super().__init__(payload.get("detail") or payload.get("error") or f"HTTP {status_code}")


class IssueService:
    def __init__(self, base_url: str | None = None, client: httpx.Client | None = None):
        self.base_url = (base_url or os.environ.get("A2A_LINK_BALANCER_URL", "http://127.0.0.1:8000")).rstrip("/")
        self._client = client

    def _request(self, method: str, path: str, _include_status: bool = False, **kwargs) -> dict[str, Any]:
        if self._client is not None:
            response = self._client.request(method, self.base_url + path, **kwargs)
        else:
            with httpx.Client(timeout=20) as client:
                response = client.request(method, self.base_url + path, **kwargs)
        try:
            payload = response.json()
        except ValueError:
            payload = {"detail": response.text[:1000]}
        if response.is_error:
            raise IssueAPIError(response.status_code, payload if isinstance(payload, dict) else {})
        if not isinstance(payload, dict):
            raise IssueAPIError(response.status_code, {"detail": "balancer returned a non-object response"})
        if _include_status:
            payload["_http_status"] = response.status_code
        return payload

    def resolve_profile(self, agent: str, task_type: str,
                        user_override: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("POST", "/profiles/resolve", json={
            "agent": agent, "task_type": task_type, "user_override": user_override,
        })

    def open_issue(self, **body: Any) -> dict[str, Any]:
        return self._request("POST", "/issues", json=body)

    def open_managed_issue(self, title: str, goal: str, acceptance: list[str], cwd: str,
                           request_id: str, opened_from: str,
                           participants: list[str] | None = None) -> dict[str, Any]:
        """Create an issue on a clean commit and materialize its deterministic worktrees."""
        source = Path(cwd).resolve()
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=source,
                             capture_output=True, text=True, check=True).stdout.strip()
        root = Path(top).resolve()
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root,
                                capture_output=True, text=True, check=True).stdout
        if status:
            raise ValueError("issue base checkout has uncommitted or untracked changes")
        base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                                  capture_output=True, text=True, check=True).stdout.strip()
        body = {"request_id": request_id, "title": title, "goal": goal, "acceptance": acceptance,
                "cwd": str(root), "base_sha": base_sha, "opened_from": opened_from}
        if participants is not None:
            body["participants"] = participants
        result = self.open_issue(**body)
        from mcp_server.worktree import prepare_issue_worktrees

        result["prepared_worktrees"] = prepare_issue_worktrees(str(root), result["paths"], base_sha)
        return result

    def snapshot(self, issue_id: str) -> dict[str, Any]:
        return self._request("GET", f"/issues/{issue_id}")

    def reserve_message(self, issue_id: str, participant: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/participants/{participant}/messages", json=body)

    def save_handle(self, issue_id: str, participant: str, **body: Any) -> dict[str, Any]:
        return self._request("PUT", f"/issues/{issue_id}/participants/{participant}/handle", json=body)

    def bump_generation(self, issue_id: str, participant: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/participants/{participant}/generation", json=body)

    def report_process(self, issue_id: str, attempt_id: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/attempts/{attempt_id}/process", json=body)

    def heartbeat(self, issue_id: str, attempt_id: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/attempts/{attempt_id}/heartbeat", json=body)

    def termination_confirmed(self, issue_id: str, attempt_id: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/attempts/{attempt_id}/termination_confirmed", json=body)

    def result(self, issue_id: str, attempt_id: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/attempts/{attempt_id}/result", json=body)

    def retry(self, issue_id: str, message_id: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/messages/{message_id}/retry", json=body)

    def abandon(self, issue_id: str, message_id: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/messages/{message_id}/abandon", json=body)

    def ready(self, issue_id: str, participant: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/participants/{participant}/ready", json=body)

    def proposal(self, issue_id: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/proposals", json=body)

    def decide(self, issue_id: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/decisions", json=body)

    def start_integration(self, issue_id: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/integrations", _include_status=True, json=body)

    def integration_candidate(self, issue_id: str, integration_id: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/integrations/{integration_id}/candidate", json=body)

    def integration_tests(self, issue_id: str, integration_id: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/integrations/{integration_id}/tests", json=body)

    def advance_integration(self, issue_id: str, integration_id: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/integrations/{integration_id}/advance", json=body)

    def integration_result(self, issue_id: str, integration_id: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/integrations/{integration_id}/result", json=body)

    def integration_heartbeat(self, issue_id: str, integration_id: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/integrations/{integration_id}/heartbeat", json=body)

    def integration_process(self, issue_id: str, integration_id: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/integrations/{integration_id}/process", json=body)

    def integration_termination_confirmed(self, issue_id: str, integration_id: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/integrations/{integration_id}/termination_confirmed", json=body)

    def takeover_integration(self, issue_id: str, integration_id: str, **body: Any) -> dict[str, Any]:
        return self._request("POST", f"/issues/{issue_id}/integrations/{integration_id}/takeover", json=body)
