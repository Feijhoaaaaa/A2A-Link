import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from balancer.app import create_app
from balancer.db import get_connection, init_schema
from balancer.repository import (add_task, claim_task, fail_task, get_usage, has_capability,
                                 list_tasks, report_usage, set_capabilities)
from balancer.router import choose_agent


@pytest.fixture
def conn(tmp_path):
    c = get_connection(str(tmp_path / "t.db"))
    init_schema(c)
    return c


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "app.db")))


def test_init_schema_creates_all_tables(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tasks", "usage", "capabilities", "incidents"} <= names


def test_init_schema_adds_weekly_usage_columns_to_existing_database(tmp_path):
    conn = get_connection(str(tmp_path / "legacy.db"))
    conn.execute("CREATE TABLE usage (agent TEXT PRIMARY KEY, window_started_at TEXT, pct_used REAL)")
    conn.execute("INSERT INTO usage VALUES ('claude', '2026-11-22T08:00:00', 41.0)")

    init_schema(conn)

    row = get_usage(conn, "claude")
    assert row["pct_used"] == 41.0
    assert row["weekly_pct_used"] is None and row["weekly_resets_at"] is None


def test_claim_task_is_atomic_only_one_wins(conn):
    add_task(conn, "t1", "do thing", "claude/high")
    assert claim_task(conn, "t1", "claude") is True
    assert claim_task(conn, "t1", "codex") is False


def test_fail_task_escalates_after_max_retries(conn):
    add_task(conn, "t1", "do thing", "claude/high")
    claim_task(conn, "t1", "claude")
    assert fail_task(conn, "t1", "boom", max_retries=2) == "pending"
    fail_task(conn, "t1", "boom again", max_retries=2)
    assert fail_task(conn, "t1", "boom thrice", max_retries=2) == "needs_human"
    row = list_tasks(conn)[0]
    assert row["status"] == "needs_human" and row["retry_count"] == 3


def test_report_usage_upserts(conn):
    report_usage(conn, "claude", 90.0, "2026-11-22T08:00:00")
    report_usage(conn, "claude", 95.0, "2026-11-22T08:00:00")
    assert get_usage(conn, "claude")["pct_used"] == 95.0


def test_report_usage_keeps_weekly_window_when_updating_five_hour_window(conn):
    report_usage(conn, "claude", 62.0, "five-hour-start", weekly_pct_used=38.0,
                 weekly_resets_at=1795932310.0)
    report_usage(conn, "claude", 66.0, "next-five-hour-start")

    row = get_usage(conn, "claude")
    assert row["pct_used"] == 66.0
    assert row["weekly_pct_used"] == 38.0
    assert row["weekly_resets_at"] == 1795932310.0


def test_capability_round_trip(conn):
    set_capabilities(conn, "codex", ["mcp:filesystem", "skill:pdf"])
    assert has_capability(conn, "codex", "skill:pdf") is True
    assert has_capability(conn, "codex", "skill:docx") is False


def test_choose_agent_picks_lowest_usage(conn):
    report_usage(conn, "claude", 80.0, "x"); report_usage(conn, "codex", 20.0, "x")
    assert choose_agent(conn, ["claude", "codex"], None) == "codex"


def test_choose_agent_excludes_over_ceiling(conn):
    report_usage(conn, "claude", 95.0, "x"); report_usage(conn, "codex", 95.0, "x")
    assert choose_agent(conn, ["claude", "codex"], None) is None


def test_choose_agent_requires_capability(conn):
    set_capabilities(conn, "codex", ["skill:pdf"])
    assert choose_agent(conn, ["claude", "codex"], "skill:pdf") == "codex"


def test_post_task_missing_description_is_422(client):
    assert client.post("/tasks", json={"model_hint": "claude/high"}).status_code == 422


def test_create_then_claim_task_flow(client):
    task_id = client.post("/tasks", json={"description": "x", "model_hint": "h"}).json()["id"]
    assert client.post(f"/tasks/{task_id}/claim", json={"agent": "claude"}).json()["claimed"] is True
    assert client.post(f"/tasks/{task_id}/claim", json={"agent": "codex"}).json()["claimed"] is False


def test_task_with_unknown_capability_is_blocked(client):
    r = client.post("/tasks", json={"description": "x", "required_capability": "skill:nope"})
    assert r.status_code == 201
    assert r.json()["status"] == "blocked" and r.json()["error"] == "missing-capability"


def test_claim_unknown_task_is_404(client):
    assert client.post("/tasks/nope/claim", json={"agent": "claude"}).status_code == 404


def test_dashboard_page_loads(client):
    r = client.get("/dashboard")
    assert r.status_code == 200 and b"<html" in r.content.lower()


def test_events_stream_is_event_stream(client):
    with client.stream("GET", "/events", params={"once": "true"}) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        body = b"".join(r.iter_bytes())
    assert body.startswith(b"data: {")


def test_cli_add_task_then_status_shows_it(client, monkeypatch):
    import balancer.cli as cli

    monkeypatch.setattr(cli, "_client", lambda: client)
    runner = CliRunner()
    r1 = runner.invoke(cli.main, ["add-task", "--description", "napisz testy", "--model-hint", "h"])
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(cli.main, ["status"])
    assert "napisz testy" in r2.output
