import httpx
import pytest

from mcp_server.issue_service import IssueAPIError, IssueService


def test_profile_resolution_sends_explicit_override_and_returns_source():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={
            "requested_profile": {"model": "gpt-6-luna", "effort": "low"},
            "source": "user_override",
        })

    client = httpx.Client(transport=httpx.MockTransport(handler))
    service = IssueService("http://test", client)
    result = service.resolve_profile("codex", "simple", {"model": "gpt-6-luna", "effort": "low"})

    assert result["source"] == "user_override"
    assert calls[0].url.path == "/profiles/resolve"
    assert calls[0].read() == b'{"agent":"codex","task_type":"simple","user_override":{"model":"gpt-6-luna","effort":"low"}}'


def test_message_reservation_passes_resolved_profile_source_unchanged():
    def handler(request):
        assert request.url.path == "/issues/i-1/participants/codex/messages"
        assert request.read() == b'{"message_id":"m-1","kind":"start","round_id":"r-1","generation":1,"requested_profile":{"model":"gpt-6-luna","effort":"low"},"profile_source":"user_override","owner":"supervisor:h:i","prompt":"start"}'
        return httpx.Response(201, json={"execute": True, "message_id": "m-1"})

    service = IssueService("http://test", httpx.Client(transport=httpx.MockTransport(handler)))
    result = service.reserve_message("i-1", "codex", message_id="m-1", kind="start",
                                     round_id="r-1", generation=1,
                                     requested_profile={"model": "gpt-6-luna", "effort": "low"},
                                     profile_source="user_override", owner="supervisor:h:i", prompt="start")
    assert result["execute"] is True


def test_api_error_preserves_conflict_context():
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(409, json={"error": "active_turn", "detail": "busy", "attempt_id": "a1"})
    ))
    service = IssueService("http://test", client)
    with pytest.raises(IssueAPIError) as caught:
        service.snapshot("i-1")
    assert caught.value.status_code == 409
    assert caught.value.payload["attempt_id"] == "a1"


def test_generation_replacement_sends_reason_and_structured_handoff():
    handoff = {"claim": "bug fixed", "evidence": "test passed",
               "correction": "previous formula was wrong", "decision": "use integer cents"}

    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/issues/i-1/participants/claude/generation"
        assert request.read() == b'{"expected_generation":1,"reason":"repeated_error","handoff":{"claim":"bug fixed","evidence":"test passed","correction":"previous formula was wrong","decision":"use integer cents"},"expected_version":8}'
        return httpx.Response(200, json={"generation": 2, "handle": None})

    service = IssueService("http://test", httpx.Client(transport=httpx.MockTransport(handler)))
    result = service.bump_generation("i-1", "claude", expected_generation=1,
                                    reason="repeated_error", handoff=handoff, expected_version=8)
    assert result["generation"] == 2
