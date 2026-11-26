"""Deterministic, bounded reconstruction for a replacement participant session."""
from __future__ import annotations

import json
from typing import Any


_MAX_CONTEXT_CHARS = 10_000


def _text(value: Any, limit: int = 2_000) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)[:limit]


def compose_generation_handoff(snapshot: dict[str, Any], participant: str,
                               generation: int, caller_prompt: str) -> str:
    """Compose a stable issue card only for a generation with a recorded replacement."""
    replacement = next((row for row in reversed(snapshot.get("generation_history", []))
                        if row.get("participant") == participant
                        and row.get("to_generation") == generation), None)
    if replacement is None:
        return caller_prompt

    issue = snapshot.get("issue") or {}
    current_round = snapshot.get("round") or {}
    rid = current_round.get("id")
    proposals = [p for p in snapshot.get("proposals", [])
                 if p.get("round_id") == rid and p.get("state") == "accepted"]
    decisions = [d for d in snapshot.get("decisions", [])
                 if d.get("round_id") == rid and d.get("kind") == "accept"]
    accepted = next((p for p in reversed(proposals)
                     if any(d.get("proposal_id") == p.get("proposal_id")
                            and d.get("proposal_version") == p.get("proposal_version")
                            for d in decisions)), None)
    decision = next((d for d in reversed(decisions)
                     if accepted and d.get("proposal_id") == accepted.get("proposal_id")
                     and d.get("proposal_version") == accepted.get("proposal_version")), None)
    passing = next((g for g in reversed(snapshot.get("integrations", []))
                    if g.get("state") == "advanced" and g.get("candidate_sha")
                    and g.get("tested_sha") == g.get("candidate_sha")
                    and (g.get("tests") or {}).get("status") == "pass"), None)
    previous_generation = replacement.get("from_generation")
    prior_message = next((m for m in reversed(snapshot.get("messages", []))
                          if m.get("participant") == participant
                          and m.get("generation") == previous_generation
                          and m.get("state") in ("done", "failed")), None)
    prior_ready = next((r for r in reversed(snapshot.get("ready_history", []))
                        if r.get("participant") == participant
                        and r.get("generation") == previous_generation), None)
    handoff = replacement.get("handoff") or {}

    sections = [
        "## Reconstructed issue handoff",
        f"Issue: {_text(issue.get('title'))}",
        f"Goal: {_text(issue.get('goal'))}",
        f"Acceptance: {_text(issue.get('acceptance'))}",
        f"Current round/base: {_text(rid)} / {_text(current_round.get('base_sha'))}",
        "Accepted proposal: " + (_text(accepted.get("text")) if accepted else "none recorded"),
        "Accepted decision: " + (_text({"kind": decision.get("kind"), "note": decision.get("note"),
                                         "proposal_version": decision.get("proposal_version")})
                                 if decision else "none recorded"),
        "Latest passing integration: " + (_text({"candidate_sha": passing.get("candidate_sha"),
                                                  "tested_sha": passing.get("tested_sha"),
                                                  "tests": passing.get("tests")})
                                          if passing else "none recorded"),
        "Previous generation latest completed/failed message: " +
        (_text({"state": prior_message.get("state"),
                "summary": (prior_message.get("result") or {}).get("text"),
                "error": prior_message.get("error") or (prior_message.get("result") or {}).get("error")})
         if prior_message else "none recorded"),
        "Previous generation ready/source: " +
        (_text({"source_commit_sha": prior_ready.get("source_commit_sha"),
                "expected_base_sha": prior_ready.get("expected_base_sha"),
                "state": prior_ready.get("state")}) if prior_ready else "none recorded"),
        f"Previous generation replacement reason: {_text(replacement.get('reason'))}",
        "Previous generation handoff (claims and evidence are coordinator input): " +
        _text({key: handoff.get(key) for key in ("claim", "evidence", "correction", "decision")}),
    ]
    context = "\n".join(sections)
    if len(context) > _MAX_CONTEXT_CHARS:
        context = context[:_MAX_CONTEXT_CHARS] + "\n[context truncated]"
    return f"{context}\n\n## Current request\n{caller_prompt}"
