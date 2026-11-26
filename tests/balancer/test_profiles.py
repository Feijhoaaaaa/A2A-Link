"""Profile resolution: user override > task type profile > executor default (M5).

POST /profiles/resolve returns the *requested* profile and where it came from. What a
CLI later confirms is stored separately on the message and may stay null.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from balancer.app import create_app
from tests.balancer.test_issues import (Clock, TTL, open_issue, post_msg, result, snap,
                                        start_participant)

CONFIG = {
    "agents": {
        "claude": {
            "models": {"claude-sonnet-5-5": ["low", "medium"], "claude-opus-5-5": ["high"]},
            "default": {"model": "claude-sonnet-5-5", "effort": "low"},
            "task_types": {
                "feature": {"model": "claude-sonnet-5-5", "effort": "medium"},
                "design": {"model": "claude-opus-5-5", "effort": "high"},
            },
        },
        "codex": {
            "models": {"gpt-6-luna": ["low"], "gpt-6.2-sol": ["medium", "high"]},
            "default": {"model": "gpt-6.2-sol", "effort": "medium"},
            "task_types": {"simple": {"model": "gpt-6-luna", "effort": "low"}},
        },
    }
}


@pytest.fixture
def profile_config(tmp_path, monkeypatch):
    def write(config):
        path = tmp_path / "agent-profiles.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        monkeypatch.setenv("A2A_LINK_PROFILE_CONFIG", str(path))
        return path
    write(CONFIG)
    return write


@pytest.fixture
def client(tmp_path, profile_config):
    return TestClient(create_app(str(tmp_path / "p.db"), clock=Clock(), attempt_ttl_s=TTL))


def resolve(client, agent, task_type, override=None):
    body = {"agent": agent, "task_type": task_type}
    if override is not None:
        body["user_override"] = override
    return client.post("/profiles/resolve", json=body)


def test_task_type_profile_beats_executor_default(client):
    r = resolve(client, "claude", "feature")
    assert r.status_code == 200, r.text
    assert r.json() == {"requested_profile": {"model": "claude-sonnet-5-5", "effort": "medium"},
                        "source": "task_type"}
    r = resolve(client, "claude", "design")
    assert r.json() == {"requested_profile": {"model": "claude-opus-5-5", "effort": "high"},
                        "source": "task_type"}
    assert resolve(client, "codex", "simple").json() == {
        "requested_profile": {"model": "gpt-6-luna", "effort": "low"}, "source": "task_type"}


def test_executor_default_is_last(client):
    r = resolve(client, "claude", "simple")  # no `simple` profile for claude in CONFIG
    assert r.status_code == 200
    assert r.json() == {"requested_profile": {"model": "claude-sonnet-5-5", "effort": "low"},
                        "source": "executor_default"}
    r = resolve(client, "codex", "design")
    assert r.json() == {"requested_profile": {"model": "gpt-6.2-sol", "effort": "medium"},
                        "source": "executor_default"}


def test_user_override_wins_over_everything_and_passes_through(client):
    override = {"model": "claude-opus-5-5", "effort": "high"}
    r = resolve(client, "claude", "simple", override)  # beats the executor default
    assert r.json() == {"requested_profile": override, "source": "user_override"}
    r = resolve(client, "claude", "feature", override)  # beats the task type profile
    assert r.json() == {"requested_profile": override, "source": "user_override"}
    # transparent: not validated against the configured models and not merged with defaults
    odd = {"model": "some-model-the-user-wants", "effort": None}
    r = resolve(client, "codex", "simple", odd)
    assert r.status_code == 200
    assert r.json() == {"requested_profile": odd, "source": "user_override"}
    partial = resolve(client, "claude", "feature", {"effort": "high"})
    assert partial.json()["requested_profile"] == {"model": None, "effort": "high"}
    assert partial.json()["source"] == "user_override"


def test_empty_override_counts_as_not_supplied(client):
    r = resolve(client, "claude", "feature", {})
    assert r.json()["source"] == "task_type"
    r = resolve(client, "claude", "feature", {"model": None, "effort": None})
    assert r.json()["source"] == "task_type"


def test_all_three_precedence_levels_in_order(client):
    levels = [resolve(client, "claude", "feature", {"model": "u", "effort": "x"}).json(),
              resolve(client, "claude", "feature").json(),
              resolve(client, "claude", "simple").json()]
    assert [level["source"] for level in levels] == ["user_override", "task_type", "executor_default"]
    assert len({json.dumps(level["requested_profile"], sort_keys=True) for level in levels}) == 3


def test_unknown_configured_model_is_a_clear_422_and_never_falls_back(client, profile_config):
    bad = json.loads(json.dumps(CONFIG))
    bad["agents"]["claude"]["task_types"]["feature"] = {"model": "claude-sonnet-9", "effort": "low"}
    profile_config(bad)
    r = resolve(client, "claude", "feature")
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "profile_unavailable"
    assert "claude-sonnet-9" in body["detail"] and "feature" in body["detail"]
    assert "requested_profile" not in body  # no silent fallback to the executor default
    # the executor default is still fine for task types that do not use the bad entry
    assert resolve(client, "claude", "design").status_code == 200
    # a user override still passes through even when the configured entry is broken
    assert resolve(client, "claude", "feature", {"model": "x", "effort": "y"}).status_code == 200


def test_unsupported_effort_for_a_known_model_is_a_422(client, profile_config):
    bad = json.loads(json.dumps(CONFIG))
    bad["agents"]["claude"]["default"] = {"model": "claude-opus-5-5", "effort": "low"}  # opus: high only
    profile_config(bad)
    r = resolve(client, "claude", "simple")
    assert r.status_code == 422 and r.json()["error"] == "profile_unavailable"
    assert "low" in r.json()["detail"]


def test_unknown_agent_and_task_type_are_422(client):
    assert resolve(client, "gemini", "feature").status_code == 422
    r = resolve(client, "claude", "epic")
    assert r.status_code == 422 and r.json()["error"] == "validation_error"
    assert client.post("/profiles/resolve", json={"agent": "claude"}).status_code == 422
    extra = client.post("/profiles/resolve", json={"agent": "claude", "task_type": "feature",
                                                   "surprise": 1})
    assert extra.status_code == 422


def test_missing_or_broken_config_is_a_clear_422(client, monkeypatch, tmp_path):
    monkeypatch.delenv("A2A_LINK_PROFILE_CONFIG")
    r = resolve(client, "claude", "feature")
    assert r.status_code == 422 and r.json()["error"] == "profile_config_missing"
    # an explicit user override needs no configuration at all
    assert resolve(client, "claude", "feature", {"model": "m", "effort": "e"}).status_code == 200
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("A2A_LINK_PROFILE_CONFIG", str(broken))
    r = resolve(client, "claude", "feature")
    assert r.status_code == 422 and r.json()["error"] == "profile_config_invalid"
    monkeypatch.setenv("A2A_LINK_PROFILE_CONFIG", str(tmp_path / "nope.json"))
    r = resolve(client, "claude", "feature")
    assert r.status_code == 422 and r.json()["error"] == "profile_config_invalid"


def test_config_is_reread_on_every_request(client, profile_config):
    assert resolve(client, "claude", "feature").json()["requested_profile"]["effort"] == "medium"
    changed = json.loads(json.dumps(CONFIG))
    changed["agents"]["claude"]["task_types"]["feature"] = {"model": "claude-sonnet-5-5", "effort": "low"}
    profile_config(changed)
    assert resolve(client, "claude", "feature").json()["requested_profile"]["effort"] == "low"


def test_example_config_contains_only_observed_supported_profiles(client, monkeypatch):
    example = Path(__file__).resolve().parents[2] / "config" / "agent-profiles.example.json"
    monkeypatch.setenv("A2A_LINK_PROFILE_CONFIG", str(example))
    models = json.loads(example.read_text(encoding="utf-8"))["agents"]
    assert models["claude"]["models"] == {"claude-sonnet-5-5": ["low", "medium"],
                                          "claude-opus-5-5": ["high"]}
    assert models["codex"]["models"] == {"gpt-6-luna": ["low"], "gpt-6.2-sol": ["medium", "high"]}
    for agent in ("claude", "codex"):
        for task_type in ("simple", "feature", "design"):
            r = resolve(client, agent, task_type)
            assert r.status_code == 200, (agent, task_type, r.text)
            p = r.json()["requested_profile"]
            assert p["effort"] in models[agent]["models"][p["model"]]


def test_message_records_profile_source_and_keeps_confirmed_separate(client):
    iid = open_issue(client)["issue"]["id"]
    start_participant(client, iid, "claude")
    requested = resolve(client, "claude", "feature").json()
    r = client.post(f"/issues/{iid}/participants/claude/messages", json={
        "message_id": "m-profile", "round_id": snap(client, iid)["round"]["id"], "generation": 1,
        "owner": "supervisor:host1:install-1", "prompt": "work",
        "requested_profile": requested["requested_profile"], "profile_source": requested["source"]})
    assert r.status_code == 201, r.text
    attempt = r.json()["attempt"]["attempt_id"]
    # Claude reports the model but not the effort: confirmed effort stays null
    done = result(client, iid, attempt, confirmed_profile={"model": "claude-sonnet-5-5", "effort": None})
    assert done.status_code == 200 and done.json()["profile_mismatch"] is False
    m = [m for m in snap(client, iid)["messages"] if m["message_id"] == "m-profile"][0]
    assert m["profile_source"] == "task_type"
    assert m["requested_profile"] == {"model": "claude-sonnet-5-5", "effort": "medium"}
    assert m["confirmed_profile"] == {"model": "claude-sonnet-5-5", "effort": None}
    listed = client.get(f"/issues/{iid}/participants/claude/messages").json()[-1]
    assert listed["profile_source"] == "task_type"


def test_dashboard_shows_the_profile_source(client):
    html = client.get("/dashboard").text
    assert "profile_source" in html and "żądany" in html and "potwierdzony" in html


def test_profile_source_is_validated_and_part_of_the_message_identity(client):
    iid = open_issue(client)["issue"]["id"]
    start_participant(client, iid, "claude")
    body = {"message_id": "m-src", "round_id": snap(client, iid)["round"]["id"], "generation": 1,
            "owner": "supervisor:host1:install-1", "prompt": "work",
            "requested_profile": {"model": "claude-sonnet-5-5", "effort": "low"}}
    url = f"/issues/{iid}/participants/claude/messages"
    assert client.post(url, json={**body, "profile_source": "guess"}).status_code == 422
    first = client.post(url, json={**body, "profile_source": "executor_default"})
    assert first.status_code == 201
    assert client.post(url, json={**body, "profile_source": "executor_default"}).status_code == 200
    changed = client.post(url, json={**body, "profile_source": "user_override"})
    assert changed.status_code == 409 and changed.json()["error"] == "message_conflict"
    m = [m for m in snap(client, iid)["messages"] if m["message_id"] == "m-src"][0]
    assert m["profile_source"] == "executor_default"
