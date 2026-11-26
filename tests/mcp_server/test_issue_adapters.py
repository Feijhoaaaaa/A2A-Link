from mcp_server.issue_adapters import (ClaudeAdapter, CodexAdapter, SessionHandle,
                                       parse_claude_events, parse_codex_events)


def test_codex_start_requests_profile_and_emits_thread_id():
    adapter = CodexAdapter()
    argv = adapter.start_argv("Edit the probe", {"model": "gpt-6-luna", "effort": "low"})

    assert argv[:3] == ["codex", "exec", "--json"]
    assert argv[argv.index("--model") + 1] == "gpt-6-luna"
    assert 'model_reasoning_effort="low"' in argv
    assert adapter.session_id_from_event({"type": "thread.started", "thread_id": "thread-1"}) == "thread-1"


def test_codex_continue_resumes_saved_session_in_its_worktree():
    adapter = CodexAdapter()
    handle = SessionHandle("codex", "thread-1", r"C:\issues\i1\codex", 2)

    argv = adapter.continue_argv(handle, "Second turn", {"model": "gpt-6.2-sol", "effort": "high"})

    assert argv[:4] == ["codex", "exec", "resume", "thread-1"]
    assert argv[argv.index("--model") + 1] == "gpt-6.2-sol"
    assert 'model_reasoning_effort="high"' in argv
    assert handle.cwd == r"C:\issues\i1\codex"


def test_codex_events_report_thread_model_effort_and_context():
    events = [
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
    ]
    transcript = [
        {"type": "turn_context", "payload": {"model": "gpt-6-luna", "effort": "low"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "model_context_window": 258400, "last_usage_includes_reasoning": True,
        }}},
    ]

    result = parse_codex_events(events, transcript)

    assert result["session_id"] == "thread-1"
    assert result["confirmed_profile"] == {"model": "gpt-6-luna", "effort": "low"}
    assert result["context"]["window"] == 258400
    assert result["usage"]["input_tokens"] == 10


def test_failed_codex_start_keeps_requested_profile_unconfirmed():
    result = parse_codex_events([
        {"type": "thread.started", "thread_id": "thread-1"},
        {"type": "turn.failed", "error": {"message": "unsupported model"}},
    ])

    assert result["session_id"] == "thread-1"
    assert result["confirmed_profile"] == {"model": None, "effort": None}
    assert "unsupported model" in result["error"]


def test_claude_start_and_continue_use_link_managed_session_and_requested_profile():
    adapter = ClaudeAdapter()
    start = adapter.start_argv("First turn", {"model": "sonnet", "effort": "low"})
    assert start[:3] == ["claude", "-p", "First turn"]
    assert "--strict-mcp-config" in start
    assert start[start.index("--model") + 1] == "sonnet"
    assert start[start.index("--effort") + 1] == "low"
    handle = SessionHandle("claude", "session-1", r"C:\issues\issue1\claude", 1)
    followup = adapter.continue_argv(handle, "Second turn", {"model": "opus"})
    assert followup[followup.index("--resume") + 1] == "session-1"
    assert handle.cwd == r"C:\issues\issue1\claude"
    assert adapter.session_id_from_event({"type": "system", "subtype": "init", "session_id": "session-1"}) == "session-1"


def test_claude_result_extracts_observed_context_usage_only():
    parsed = parse_claude_events([
        {"type": "system", "subtype": "init", "session_id": "session-1",
         "model": "claude-sonnet-5-5"},
        {"type": "result", "model_usage_since_last_result": {
            "claude-sonnet-5-5": {
                "contextWindow": 200000, "inputTokens": 100,
                "cacheReadInputTokens": 20, "cacheCreationInputTokens": 10,
                "outputTokens": 5,
            }}},
        {"type": "rate_limit_event", "rate_limit_info": {
            "rateLimitType": "five_hour", "utilization": 0.99}},
    ])

    assert parsed["session_id"] == "session-1"
    assert parsed["confirmed_profile"] == {"model": "claude-sonnet-5-5", "effort": None}
    assert parsed["context"] == {
        "window": 200000, "input_tokens": 100, "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 10, "output_tokens": 5,
        "used_tokens": 135, "threshold": 0.9, "threshold_reached": False,
    }


def test_claude_context_unknown_fields_remain_null_and_rate_limit_is_not_context():
    parsed = parse_claude_events([
        {"type": "result", "model_usage_since_last_result": {
            "claude-sonnet-5-5": {"inputTokens": 90}}},
        {"type": "rate_limit_event", "rate_limit_info": {
            "rateLimitType": "five_hour", "utilization": 1.0}},
    ])

    assert parsed["context"] == {
        "window": None, "input_tokens": 90, "cache_read_input_tokens": None,
        "cache_creation_input_tokens": None, "output_tokens": None,
        "used_tokens": None, "threshold": 0.9, "threshold_reached": None,
    }


def test_claude_context_threshold_is_advisory_at_ninety_percent():
    parsed = parse_claude_events([{"type": "result", "model_usage_since_last_result": {
        "claude-sonnet-5-5": {"contextWindow": 200, "inputTokens": 170,
            "cacheReadInputTokens": 5, "cacheCreationInputTokens": 3, "outputTokens": 2}}}])
    assert parsed["context"]["used_tokens"] == 180
    assert parsed["context"]["threshold_reached"] is True
    assert parsed["context"]["threshold"] == 0.9
