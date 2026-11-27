"""Offline checks of the probe's parsing and anonymized report; no CLI is started."""
import json

import pytest

from tests.manual.session_resume_probe import probe
from tests.manual.session_resume_probe.probe import ProbeResult, Turn

SID = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"
NONCE = "probedeadbeef"


def _result(provider, resume_ids, reply=NONCE, unknown=None):
    turns = [Turn("start", 0, start_ids=[SID], completed=True, reply="OK")]
    for name, ids in zip(("completion_resume", "newer_message_resume"), resume_ids):
        turns.append(Turn(name, 0, start_ids=ids, completed=True, reply=reply))
    return ProbeResult(provider, "1.2.3", turns, NONCE, 1.0, unknown)


def test_codex_start_event_parsing():
    out = "\n".join([json.dumps({"type": "thread.started", "thread_id": SID}),
                     "not json",
                     json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}}),
                     json.dumps({"type": "turn.completed"})])
    events = probe.parse_jsonl(out)
    assert probe.start_event_ids("codex", events) == [SID]
    assert probe.turn_completed("codex", events)
    assert probe.reply_text("codex", events) == "hi"


def test_claude_start_and_result_ids():
    events = [{"type": "system", "subtype": "init", "session_id": SID},
              {"type": "result", "session_id": SID, "result": "ok", "is_error": False}]
    assert probe.start_event_ids("claude", events) == [SID]
    assert probe.result_session_ids("claude", events) == [SID]
    assert probe.turn_completed("claude", events)


def test_same_id_passes_and_report_is_anonymous():
    report = _result("codex", [[SID], [SID]]).report()
    assert report["passed"] is True
    assert report["start_id_shape"] == "uuid"
    text = json.dumps(report)
    assert SID not in text and NONCE not in text and "OK" not in text


@pytest.mark.parametrize("ids", [[[OTHER], [SID]], [[SID], []], [[SID, OTHER], [SID]]])
def test_new_missing_or_extra_id_fails(ids):
    report = _result("codex", ids).report()
    assert report["passed"] is False


def test_lost_context_fails():
    assert _result("claude", [[SID], [SID]], reply="unknown").report()["passed"] is False


def test_failed_start_has_no_resume_checks_and_fails():
    result = ProbeResult("claude", None, [Turn("start", 1)], NONCE, 1.0)
    report = result.report()
    assert report["passed"] is False
    assert report["start_id_shape"] == "missing"


def test_unknown_id_substitution_is_reported_without_id():
    unknown = Turn("unknown_id_resume", 0, start_ids=[OTHER], completed=True)
    report = _result("codex", [[SID], [SID]], unknown=unknown).report()
    assert report["unknown_id"] == {"returncode": 0, "rejected": False, "substituted_new_session": True}
    assert OTHER not in json.dumps(report)


def test_main_refuses_without_run():
    with pytest.raises(SystemExit):
        probe.main(["--provider", "codex"])


def test_cli_version_uses_link_executable_fallback(monkeypatch):
    seen = []
    monkeypatch.setattr(probe, "resolve_executable", lambda name: f"C:/fallback/{name}.exe")

    class Completed:
        stdout = "Claude Code 2.1.330"

    def fake_run(argv, **kwargs):
        seen.append(argv)
        return Completed()

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    assert probe.cli_version("claude") == "2.1.330"
    assert seen == [["C:/fallback/claude.exe", "--version"]]
