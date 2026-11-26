from mcp_server.issue_handoff import compose_generation_handoff


def test_generation_handoff_reconstructs_durable_issue_decision_and_passing_integration():
    snapshot = {
        "issue": {"title": "Pricing", "goal": "Exact rounding", "acceptance": ["-2.5 -> -3"]},
        "round": {"id": "r2", "base_sha": "b" * 40},
        "participants": {"claude": {"generation": 2}},
        "generation_history": [{"participant": "claude", "from_generation": 1,
            "to_generation": 2, "reason": "repeated_error", "handoff": {
                "claim": "rounding is fixed", "evidence": "4 tests passed",
                "correction": "float behavior was wrong", "decision": "use Fraction",
            }}],
        "messages": [{"participant": "claude", "generation": 1, "state": "failed",
                      "result": {"text": "previous turn hit the incorrect tie rule",
                                 "error": "assertion failed"}}],
        "ready_history": [{"participant": "claude", "generation": 1,
                           "source_commit_sha": "d" * 40, "expected_base_sha": "b" * 40,
                           "state": "stale"}],
        "proposals": [{"proposal_id": "p1", "proposal_version": 2, "round_id": "r2",
                       "text": "Use exact arithmetic", "state": "accepted"}],
        "decisions": [{"id": "d1", "round_id": "r2", "proposal_id": "p1",
                       "proposal_version": 2, "kind": "accept", "note": "approved"}],
        "integrations": [{"integration_id": "g1", "round_id": "r1", "state": "advanced",
                          "candidate_sha": "c" * 40, "tested_sha": "c" * 40,
                          "tests": {"status": "pass", "exit_code": 0}}],
    }

    first = compose_generation_handoff(snapshot, "claude", 2, "Implement the next request")
    second = compose_generation_handoff(snapshot, "claude", 2, "Implement the next request")

    assert first == second
    for required in ("Pricing", "Exact rounding", "-2.5 -> -3", "r2", "b" * 40,
                     "Use exact arithmetic", "approved", "c" * 40, "4 tests passed",
                     "incorrect tie rule", "assertion failed", "d" * 40,
                     "float behavior was wrong", "Implement the next request"):
        assert required in first
    assert "claude-old-session" not in first and "session_id" not in first


def test_generation_handoff_is_bounded_and_preserves_caller_prompt_last():
    snapshot = {"issue": {"title": "T" * 10000, "goal": "G" * 10000,
                          "acceptance": ["A" * 10000]},
                "round": {"id": "r1", "base_sha": "a" * 40},
                "participants": {"codex": {"generation": 2}},
                "generation_history": [{"participant": "codex", "from_generation": 1,
                    "to_generation": 2, "reason": "manual", "handoff": {
                        "claim": "c" * 10000, "evidence": "e" * 10000,
                        "correction": "x" * 10000, "decision": "d" * 10000}}],
                "proposals": [], "decisions": [], "integrations": []}

    prompt = compose_generation_handoff(snapshot, "codex", 2, "CALLER PROMPT LAST")

    assert len(prompt) <= 12000
    assert prompt.endswith("CALLER PROMPT LAST")
