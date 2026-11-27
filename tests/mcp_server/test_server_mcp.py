"""End-to-end through the real MCP protocol layer (in-process client <-> server)."""
import json

import anyio
import pytest
import mcp_types as types
from mcp import Client
from mcp.shared.exceptions import MCPError

from mcp_server.server import build_server
from tests.mcp_server.test_terminal import setup  # noqa: F401  (fixture)


# "auto" negotiates the 2026-07-28 protocol (InputRequiredResult round-trip);
# "legacy" uses the initialize handshake + mid-call elicitation/create, as older stdio clients do.
MODES = pytest.mark.parametrize("mode", ["auto", "legacy"])


def _call(server, tool, args, elicitation_callback=None, mode="auto"):
    async def go():
        async with Client(server, elicitation_callback=elicitation_callback, mode=mode) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            res = await c.call_tool(tool, args)
            return names, res
    try:
        return anyio.run(go)
    except BaseExceptionGroup as eg:  # anyio task groups wrap the client-side MCPError
        leaf = eg
        while isinstance(leaf, BaseExceptionGroup) and len(leaf.exceptions) == 1:
            leaf = leaf.exceptions[0]
        raise leaf from eg


def _payload(res) -> dict:
    if res.structured_content is not None:
        return res.structured_content
    return json.loads(res.content[0].text)


def test_tools_are_registered(setup):
    _, path, _ = setup
    names, _ = _call(build_server(str(path)), "describe_terminal_policy", {})
    assert {"delegate_to_claude", "delegate_to_codex", "run_command", "describe_terminal_policy"} <= names
    assert "replace_issue_generation" in names
    assert "update_a2a_link" in names


def test_update_a2a_link_tool_calls_shared_updater(setup, monkeypatch):
    _, path, _ = setup
    import mcp_server.server as server_module

    expected = {"status": "updated", "from_version": "2.0.0", "to_version": "2.1.0",
                "restart_required": True}
    calls = []

    def fake_update():
        calls.append(True)
        return expected

    monkeypatch.setattr(server_module, "update_installation", fake_update)
    _, response = _call(build_server(str(path)), "update_a2a_link", {})

    assert _payload(response) == expected
    assert calls == [True]


def test_replace_issue_generation_tool_forwards_handoff(setup, monkeypatch):
    _, path, _ = setup
    import mcp_server.server as server_module

    class FakeIssues:
        call = None

        def bump_generation(self, issue_id, participant, **body):
            self.call = (issue_id, participant, body)
            return {"generation": 2, "handle": None}

    fake = FakeIssues()
    monkeypatch.setattr(server_module, "ISSUES", fake)
    handoff = {"claim": "claim", "evidence": "test", "correction": "fix", "decision": "accept"}
    _, response = _call(build_server(str(path)), "replace_issue_generation", {
        "issue_id": "i-1", "participant": "claude", "reason": "repeated_error",
        "expected_generation": 1, "expected_version": 8, "handoff": handoff,
    })

    assert _payload(response)["generation"] == 2
    assert fake.call == ("i-1", "claude", {"expected_generation": 1, "expected_version": 8,
                                              "reason": "repeated_error", "handoff": handoff})


@MODES
def test_run_command_safe_over_mcp(setup, mode):
    work, path, _ = setup
    _, res = _call(build_server(str(path)), "run_command",
                   {"argv": ["python", "hello.py", "x"], "cwd": str(work)}, mode=mode)
    data = _payload(res)
    assert data["status"] == "ok" and data["returncode"] == 0 and "hello from" in data["stdout"]


@MODES
def test_run_command_confirm_accepted_via_elicitation(setup, mode):
    work, path, _ = setup
    prompts = []

    async def approve(context, params):
        prompts.append(params.message)
        return types.ElicitResult(action="accept", content={"approve": True})

    _, res = _call(build_server(str(path)), "run_command",
                   {"argv": ["python", "danger.py"], "cwd": str(work)}, approve, mode)
    assert _payload(res)["status"] == "ok" and (work / "touched.txt").exists()
    assert "danger.py" in prompts[0]


@MODES
def test_run_command_confirm_declined_via_elicitation(setup, mode):
    work, path, _ = setup

    async def decline(context, params):
        return types.ElicitResult(action="decline")

    _, res = _call(build_server(str(path)), "run_command",
                   {"argv": ["python", "danger.py"], "cwd": str(work)}, decline, mode)
    assert _payload(res)["status"] == "declined" and not (work / "touched.txt").exists()


@MODES
def test_run_command_confirm_refused_when_client_lacks_elicitation(setup, mode):
    work, path, _ = setup
    # Fail closed: the server rejects the call with MISSING_REQUIRED_CLIENT_CAPABILITY.
    with pytest.raises(MCPError, match="elicitation capability"):
        _call(build_server(str(path)), "run_command",
              {"argv": ["python", "danger.py"], "cwd": str(work)}, mode=mode)
    assert not (work / "touched.txt").exists()


def test_executor_disabled_without_policy(tmp_path):
    _, res = _call(build_server(None), "run_command", {"argv": ["git", "status"], "cwd": str(tmp_path)})
    data = _payload(res)
    assert data["status"] == "denied" and "A2A_LINK_POLICY" in data["reason"]


def test_unwritable_audit_log_disables_executor_with_clear_reason(tmp_path):
    root = tmp_path / "r"; root.mkdir()
    blocker = tmp_path / "not_a_dir"; blocker.write_text("x")
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"allowed_roots": [str(root)], "audit_log": str(blocker / "a.jsonl"),
                             "commands": {"git": {"default": "safe"}}}))
    _, res = _call(build_server(str(p)), "run_command", {"argv": ["git", "status"], "cwd": str(root)})
    data = _payload(res)
    assert not res.is_error and data["status"] == "denied" and "not writable" in data["reason"]


def test_confirmation_schema_has_only_spec_top_level_keys():
    # Codex rejects unknown top-level keys (e.g. pydantic's "title") and cancels the prompt.
    from mcp.server.elicitation import render_elicitation_schema
    from mcp_server.server import ConfirmCommand

    schema = render_elicitation_schema(ConfirmCommand)
    assert set(schema) <= {"$schema", "type", "properties", "required"}
    assert schema["properties"]["approve"]["type"] == "boolean"
