import json
import os
import time

from fastapi.testclient import TestClient

from balancer import codex_usage
from balancer.app import create_app


def _session(home, day, name, limits, mtime=None):
    d = home / "codex" / "sessions" / "2026" / "11" / day
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"rollout-{name}.jsonl"
    lines = [json.dumps({"type": "session_meta", "payload": {}})]
    if limits is not None:
        lines.append(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": limits}}))
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime:
        os.utime(f, (mtime, mtime))
    return f


def _limits(five_h, week, resets_in=3600):
    now = time.time()
    return {"primary": {"used_percent": five_h, "window_minutes": 300, "resets_at": now + resets_in},
            "secondary": {"used_percent": week, "window_minutes": 10080, "resets_at": now + 86400}}


def test_newest_session_wins(_isolated_a2a_home):
    home = _isolated_a2a_home
    _session(home, "21", "old", _limits(80, 40), mtime=time.time() - 86400)
    _session(home, "22", "new", _limits(10, 2))
    w = codex_usage.codex_windows(max_age_s=0)
    assert [(x["agent"], x["pct_used"]) for x in w] == [("codex", 10.0), ("codex (7 dni)", 2.0)]


def test_falls_back_to_older_file_without_limits_in_newest(_isolated_a2a_home):
    home = _isolated_a2a_home
    _session(home, "22", "a", _limits(33, 5), mtime=time.time() - 60)
    _session(home, "22", "b", None)  # newest session has no rate_limits yet
    assert codex_usage.codex_windows(max_age_s=0)[0]["pct_used"] == 33.0


def test_expired_window_reads_as_zero(_isolated_a2a_home):
    _session(_isolated_a2a_home, "22", "x", _limits(95, 50, resets_in=-10))
    assert codex_usage.codex_windows(max_age_s=0)[0]["pct_used"] == 0.0


def test_no_codex_home_means_no_windows():
    assert codex_usage.codex_windows(max_age_s=0) == []


def test_dashboard_usage_includes_codex_and_feeds_the_router(_isolated_a2a_home, tmp_path):
    _session(_isolated_a2a_home, "22", "x", _limits(91, 3))
    client = TestClient(create_app(str(tmp_path / "b.db")))
    usage = {u["agent"]: u for u in client.get("/usage").json()}
    assert usage["codex"]["pct_used"] == 91.0 and usage["codex"]["resets_at"]
    assert usage["codex (7 dni)"]["pct_used"] == 3.0
    # 91% is over the 90% ceiling, so new tasks are suggested for claude, not codex
    assert client.post("/tasks", json={"description": "x"}).json()["agent"] == "claude"
