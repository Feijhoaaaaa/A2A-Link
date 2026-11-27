"""Restricted MCP server for a single Link-managed issue participant."""
from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from mcp_server.issue_service import IssueService
from mcp_server.test_snapshot import create_snapshot


@dataclass(frozen=True)
class ParticipantScope:
    issue_id: str
    participant: str
    generation: int
    origin_attempt_id: str
    origin_message_id: str
    cwd: str
    requested_profile: dict[str, Any]

    @property
    def session_ref(self) -> dict[str, Any]:
        return {"issue_id": self.issue_id, "participant": self.participant,
                "generation": self.generation, "origin_attempt_id": self.origin_attempt_id}

    @classmethod
    def from_environment(cls, environ: dict[str, str] | None = None, *,
                         service: IssueService | None = None,
                         actual_cwd: str | None = None) -> "ParticipantScope":
        env = os.environ if environ is None else environ
        raw = env.get("A2A_LINK_PARTICIPANT_CONTEXT")
        if not raw:
            raise ValueError("trusted participant context is required")
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raise ValueError("trusted participant context is invalid JSON") from None
        scope = cls.from_payload(payload, service=service or IssueService(),
                                 actual_cwd=actual_cwd or os.getcwd())
        return scope

    @classmethod
    def from_payload(cls, payload: dict[str, Any], *, service: IssueService,
                     actual_cwd: str | None = None) -> "ParticipantScope":
        required = {"issue_id", "participant", "generation", "origin_attempt_id",
                    "origin_message_id", "cwd", "requested_profile"}
        if (not isinstance(payload, dict) or not required.issubset(payload)
                or set(payload) - (required | {"balancer_url", "link_home", "worktree_root"})):
            raise ValueError("trusted participant context is incomplete")
        issue_id, participant = payload["issue_id"], payload["participant"]
        generation, attempt_id = payload["generation"], payload["origin_attempt_id"]
        message_id, cwd = payload["origin_message_id"], payload["cwd"]
        profile = payload["requested_profile"]
        if (not isinstance(issue_id, str) or not issue_id or participant not in ("claude", "codex")
                or type(generation) is not int or generation < 1
                or not isinstance(attempt_id, str) or not attempt_id
                or not isinstance(message_id, str) or not message_id
                or not isinstance(cwd, str) or not cwd or not isinstance(profile, dict)):
            raise ValueError("trusted participant context has invalid fields")
        actual = Path(actual_cwd or os.getcwd()).resolve()
        root = Path(cwd).resolve()
        if actual != root or not root.is_dir():
            raise ValueError("participant worktree cwd does not match the trusted scope")

        snapshot = service.snapshot(issue_id)
        if (snapshot.get("issue") or {}).get("state") != "open":
            raise ValueError("issue is no longer open")
        link_home = Path(os.environ.get("A2A_LINK_HOME", str(Path.home() / ".a2a_link")))
        worktree_root = Path(os.environ.get("A2A_LINK_WORKTREE_DIR", payload.get(
            "worktree_root", str(link_home / "worktrees"))))
        expected = (worktree_root / f"{issue_id}-{participant}").resolve()
        # Test and embedding stores may provide the prepared paths explicitly.
        path_row = (snapshot.get("paths") or {}).get(participant) or {}
        explicit = path_row.get("cwd") or path_row.get("worktree")
        if explicit and Path(explicit).resolve() != expected:
            raise ValueError("prepared participant path does not match the configured worktree")
        if root != expected:
            raise ValueError("participant cwd is outside the issue worktree")

        part = (snapshot.get("participants") or {}).get(participant) or {}
        if part.get("generation") != generation:
            raise ValueError("participant generation is stale")
        handle = part.get("handle")
        if handle is not None and (handle.get("generation", generation) != generation
                                   or Path(handle.get("cwd", "")).resolve() != root):
            raise ValueError("participant handle scope is stale")
        message = next((row for row in snapshot.get("messages", [])
                        if row.get("message_id") == message_id), None)
        attempt = next((row for row in snapshot.get("attempts", [])
                        if row.get("attempt_id") == attempt_id), None)
        if (message is None or attempt is None or message.get("participant") != participant
                or message.get("generation") != generation
                or message.get("state") != "active"
                or message.get("current_attempt_id") != attempt_id
                or attempt.get("message_id") != message_id or attempt.get("state") != "running"):
            raise ValueError("originating participant attempt is stale or inactive")
        return cls(issue_id, participant, generation, attempt_id, message_id,
                   str(root), dict(profile))


class ParticipantTools:
    """Only the two safe operations allowed to a managed participant."""

    def __init__(self, scope: ParticipantScope, service: IssueService, link_home: Path):
        self.scope, self.service, self.link_home = scope, service, link_home

    def start_tests(self, profile: str, targets: list[str] | None = None) -> dict[str, Any]:
        """Capture this worktree and enqueue background tests; return without waiting."""
        if not isinstance(profile, str) or not profile.strip() or len(profile) > 128:
            return {"status": "error", "error": "profile must be a non-empty test profile name"}
        if targets is not None and (not isinstance(targets, list) or not targets
                                    or not all(isinstance(x, str) and x for x in targets)):
            return {"status": "error", "error": "targets must be a non-empty list of runner labels"}
        selected = targets or ["windows" if os.name == "nt" else "linux"]
        snapshot_dir = self.link_home / "participant-snapshots"
        try:
            artifact = create_snapshot(Path(self.scope.cwd), {}, snapshot_dir)
            self.service.upload_test_snapshot(artifact.archive_path, artifact.sha256)
            request_id = str(uuid.uuid4())
            request = self.service.create_test_request(
                request_id=request_id, snapshot_sha256=artifact.sha256, profile=profile,
                targets=selected, session_ref=self.scope.session_ref,
            )
            return {"status": request.get("state", "queued"),
                    "request_id": request.get("request_id", request_id),
                    "snapshot_sha256": artifact.sha256, "targets": selected}
        except Exception as exc:
            return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

    def get_test_request(self, request_id: str) -> dict[str, Any]:
        """Read one request belonging to this participant scope."""
        try:
            row = self.service.get_test_request(request_id)
        except Exception as exc:
            return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(row, dict) or row.get("session_ref") != self.scope.session_ref:
            return {"status": "error", "error": "test request is outside this participant scope"}
        return row


def build_participant_server(scope: ParticipantScope | None = None, *,
                             service: IssueService | None = None,
                             actual_cwd: str | None = None,
                             environ: dict[str, str] | None = None) -> MCPServer:
    env = os.environ if environ is None else environ
    service = service or IssueService(env.get("A2A_LINK_BALANCER_URL"))
    scope = scope or ParticipantScope.from_environment(env, service=service, actual_cwd=actual_cwd)
    link_home = Path(env.get("A2A_LINK_HOME", str(Path.home() / ".a2a_link")))
    tools = ParticipantTools(scope, service, link_home)
    server = MCPServer("a2a-link-participant")
    server.tool()(tools.start_tests)
    server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))(
        tools.get_test_request)
    return server


def main() -> None:
    import logging
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s a2a_link.participant %(levelname)s %(message)s")
    build_participant_server().run("stdio")


if __name__ == "__main__":
    main()
