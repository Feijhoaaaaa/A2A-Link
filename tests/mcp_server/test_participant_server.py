import json
from pathlib import Path

import pytest

from mcp_server.participant_server import (ParticipantScope, ParticipantTools,
                                          build_participant_server)
from mcp_server.test_snapshot import Snapshot


class FakeService:
    def __init__(self, cwd):
        self.cwd = str(cwd)
        self.request_body = None
        self.uploaded = None
        self.requests = {}

    def snapshot(self, issue_id):
        return {
            "issue": {"cwd": str(Path(self.cwd).parents[1]), "state": "open"},
            "paths": {"codex": {"cwd": self.cwd, "worktree": self.cwd}},
            "participants": {"codex": {"generation": 2, "handle": {
                "adapter_type": "codex", "session_id": "saved-id", "cwd": self.cwd,
                "generation": 2}}},
            "attempts": [{"attempt_id": "attempt-9", "message_id": "message-8",
                          "state": "running", "generation": 2, "participant": "codex"}],
            "messages": [{"message_id": "message-8", "participant": "codex",
                          "generation": 2, "state": "active",
                          "current_attempt_id": "attempt-9"}],
        }

    def upload_test_snapshot(self, path, sha256):
        self.uploaded = (Path(path).read_bytes(), sha256)

    def create_test_request(self, **body):
        self.request_body = body
        response = {"request_id": body["request_id"], "state": "queued",
                    "session_ref": body["session_ref"]}
        self.requests[body["request_id"]] = response
        return response

    def get_test_request(self, request_id):
        return self.requests.get(request_id)


@pytest.fixture
def valid_scope(tmp_path, monkeypatch):
    issue_root = tmp_path / "issue"
    cwd = issue_root / "worktrees" / "i_de6fda66d9-codex"
    cwd.mkdir(parents=True)
    monkeypatch.setenv("A2A_LINK_WORKTREE_DIR", str(cwd.parent))
    service = FakeService(cwd)
    scope = ParticipantScope(
        issue_id="i_de6fda66d9", participant="codex", generation=2,
        origin_attempt_id="attempt-9", origin_message_id="message-8", cwd=str(cwd),
        requested_profile={"model": "gpt-6.2-sol", "effort": "high"},
    )
    return cwd, service, scope


def test_scope_requires_complete_trusted_context(monkeypatch, valid_scope):
    _, service, scope = valid_scope
    monkeypatch.delenv("A2A_LINK_PARTICIPANT_CONTEXT", raising=False)
    with pytest.raises(ValueError, match="trusted participant context"):
        ParticipantScope.from_environment({}, service=service)

    monkeypatch.setenv("A2A_LINK_PARTICIPANT_CONTEXT", json.dumps({
        "issue_id": scope.issue_id, "participant": scope.participant,
        "generation": scope.generation, "origin_attempt_id": scope.origin_attempt_id,
        "origin_message_id": scope.origin_message_id, "cwd": scope.cwd,
        "requested_profile": scope.requested_profile,
    }))
    assert ParticipantScope.from_environment(os_env(), service=service,
                                             actual_cwd=scope.cwd) == scope


def os_env():
    import os
    return dict(os.environ)


@pytest.mark.parametrize("update", [
    {"generation": 1}, {"origin_attempt_id": "old-attempt"},
    {"origin_message_id": "old-message"}, {"cwd": None},
])
def test_scope_rejects_stale_attempt_generation_or_out_of_worktree(valid_scope, update):
    cwd, service, scope = valid_scope
    data = {"issue_id": scope.issue_id, "participant": scope.participant,
            "generation": scope.generation, "origin_attempt_id": scope.origin_attempt_id,
            "origin_message_id": scope.origin_message_id, "cwd": scope.cwd,
            "requested_profile": scope.requested_profile}
    data.update(update)
    with pytest.raises(ValueError):
        ParticipantScope.from_payload(data, service=service, actual_cwd=str(cwd))


def test_start_tests_uploads_snapshot_queues_and_returns_without_polling(tmp_path, valid_scope,
                                                                        monkeypatch):
    cwd, service, scope = valid_scope
    (cwd / "source.py").write_text("value = 7\n", encoding="utf-8")
    monkeypatch.setenv("A2A_LINK_HOME", str(tmp_path / "link-home"))
    archive = tmp_path / "snapshot.tar"
    archive.write_bytes(b"immutable-snapshot")
    monkeypatch.setattr("mcp_server.participant_server.create_snapshot", lambda *_args:
                        Snapshot("a" * 64, archive, 1, archive.stat().st_size))
    tools = ParticipantTools(scope, service, tmp_path / "link-home")
    result = tools.start_tests("unit", targets=None)

    assert result["status"] == "queued"
    assert result["request_id"]
    assert service.uploaded is not None
    assert service.request_body["profile"] == "unit"
    assert service.request_body["targets"] == ["windows" if os_name() == "nt" else "linux"]
    assert service.request_body["session_ref"] == scope.session_ref
    assert "session_id" not in service.request_body


def os_name():
    import os
    return os.name


def test_start_tests_failure_is_reported_without_fabricating_request_id(tmp_path, valid_scope,
                                                                        monkeypatch):
    _, service, scope = valid_scope
    service.upload_test_snapshot = lambda *_args: (_ for _ in ()).throw(RuntimeError("offline"))
    archive = tmp_path / "snapshot.tar"
    archive.write_bytes(b"snapshot")
    monkeypatch.setattr("mcp_server.participant_server.create_snapshot", lambda *_args:
                        Snapshot("a" * 64, archive, 1, archive.stat().st_size))
    tools = ParticipantTools(scope, service, tmp_path / "link-home")

    result = tools.start_tests("unit")

    assert result["status"] == "error"
    assert "request_id" not in result
    assert "offline" in result["error"]


def test_get_test_request_is_read_only_and_scoped_to_origin(valid_scope):
    _, service, scope = valid_scope
    service.requests["request-1"] = {"request_id": "request-1", "state": "running",
                                     "session_ref": scope.session_ref}
    tools = ParticipantTools(scope, service, Path.cwd())

    assert tools.get_test_request("request-1")["state"] == "running"
    assert tools.get_test_request("somebody-elses-request")["status"] == "error"


def test_participant_server_registers_only_start_tests_and_get_test_request(valid_scope):
    _, service, scope = valid_scope
    build_participant_server(scope, service=service, actual_cwd=scope.cwd)
    public_methods = {name for name in vars(ParticipantTools)
                      if not name.startswith("_") and callable(getattr(ParticipantTools, name))}
    assert public_methods == {"start_tests", "get_test_request"}
