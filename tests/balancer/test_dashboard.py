"""Dashboard (balancer/static/dashboard.html): English UI, preserved v1/v2 sections,
and the test-request list refreshed every 5 seconds from the list API.

Static checks always run. Behaviour checks execute the page script under Node with a
stubbed DOM/fetch/timers and are skipped when `node` is not installed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from balancer.app import create_app
from balancer.test_jobs_api import DeliveryStatusOut

PAGE = Path(__file__).resolve().parents[2] / "balancer" / "static" / "dashboard.html"


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "app.db")))


@pytest.fixture
def html(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    return r.text


def script_of(page: str) -> str:
    scripts = re.findall(r"<script>(.*?)</script>", page, flags=re.S)
    assert len(scripts) == 1, "the dashboard keeps one inline script"
    return scripts[0]


# --- static -----------------------------------------------------------------------

def test_dashboard_is_english(html):
    assert '<html lang="en">' in html
    assert not re.search(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]", html), "Polish characters left in the page"
    for polish in ("Zadania", "Zgłoszenia", "Incydenty", "Okna 5h", "brak danych", "nie potwierdzono",
                   "Kryteria akceptacji", "Uczestnicy", "Propozycje", "Decyzje", "Integracje",
                   "ZABLOKOWANY", "żądany", "potwierdzony", "Przebieg pracy", "Wynik", "Zamknij"):
        assert polish not in html, polish


def test_dashboard_preserves_existing_sections(html):
    for element_id in ('id="usage"', 'id="tasks"', 'id="details"', 'id="log"', 'id="issues"',
                       'id="incidents"', 'id="test-requests"', 'id="request-detail"'):
        assert element_id in html, element_id
    for needle in ('new EventSource("/events")', "/tasks/", "/log?lines=400", "renderIssues",
                   "Acceptance criteria", "Participants", "Proposals", "Decisions", "Integrations",
                   "profile_mismatch", "profile_source", "requested", "confirmed", "BLOCKED",
                   "source_commit_sha", "candidate_sha", "opened_from", "generation_history",
                   "owner_lost_suspected", "not confirmed"):
        assert needle in html, needle


def test_dashboard_polls_the_request_list_every_five_seconds(html):
    js = script_of(html)
    assert "/test-requests?limit=50" in js
    assert re.search(r"setInterval\(\s*refreshRequests\s*,\s*5000\s*\)", js)


def test_dashboard_is_self_contained(html):
    assert not re.search(r"<script[^>]+src=", html)
    assert "<link" not in html
    assert not re.search(r"https?://", html), "no external URLs, CDNs or fonts"
    assert "@import" not in html


def test_dashboard_states_the_operator_contract(html):
    assert "adapter not implemented" not in html
    assert "session_ref" not in html
    assert "runner slot stays held" in html
    assert "manual resolution" in html
    assert "/cancel" in html and "/log?job_id=" in html
    assert "color-scheme: dark" in html
    assert 'name="viewport"' in html


def test_dashboard_offers_copyable_session_ids_and_resume_commands(html):
    for text in ("Copy session ID", "Copy resume command", "claude --resume", "codex resume --all",
                 "navigator.clipboard", 'execCommand("copy")'):
        assert text in html


# --- behaviour (Node) --------------------------------------------------------------

HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const [pagePath, scenarioPath] = process.argv.slice(2);
const page = fs.readFileSync(pagePath, "utf8");
const scenario = JSON.parse(fs.readFileSync(scenarioPath, "utf8"));
const js = page.match(/<script>([\s\S]*?)<\/script>/)[1];

const elements = {};
function el(id) {
  return elements[id] ??= {id, innerHTML: "", textContent: "", value: "", hidden: false, open: false,
    disabled: false, scrollTop: 0, scrollHeight: 0, clientHeight: 0, style: {}, dataset: {},
    classList: {add() {}, remove() {}, toggle() {}},
    listeners: {}, addEventListener(type, fn) { (this.listeners[type] ??= []).push(fn); },
    querySelector() { return null; }, querySelectorAll() { return []; },
    set onclick(fn) { this.listeners.click = [fn]; }};
}
const intervals = [], fetches = [];
let copied = null, fallbackCopied = null, lastTextarea = null;
const responses = scenario.responses;
const sandbox = {
  console, JSON, Math, Date, String, Number, Object, Array, Promise, encodeURIComponent, isNaN,
  document: {getElementById: el, querySelector: () => null, querySelectorAll: () => [],
             addEventListener() {}, hidden: false,
             createElement: () => (lastTextarea = {value: "", style: {}, setAttribute() {}, select() {}, remove() {}}),
             body: {appendChild() {}},
             execCommand: command => { fallbackCopied = lastTextarea?.value; return command === "copy"; }},
  navigator: {clipboard: {writeText: async value => { copied = value; }}},
  setInterval: (fn, ms) => { intervals.push({fn, ms}); return intervals.length; },
  clearInterval() {}, setTimeout: (fn) => 0, clearTimeout() {},
  EventSource: class { constructor(url) { this.url = url; } },
  fetch: async (url, opts = {}) => {
    fetches.push({url, method: opts.method || "GET"});
    const key = Object.keys(responses).find(k => url.startsWith(k));
    const r = key ? responses[key] : {status: 404, body: {error: "not_found"}};
    return {ok: r.status < 400, status: r.status, json: async () => r.body,
            text: async () => JSON.stringify(r.body)};
  },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(js, sandbox);
(async () => {
  const D = sandbox.A2ADashboard;
  const out = {intervals: intervals.map(i => ({ms: i.ms, name: i.fn.name})), fetchesAtLoad: fetches.map(f => f.url)};
  out.issueHtml = D.renderIssue(scenario.issue);
  const copyButton = {dataset: {copy: "saved-session-token"}, textContent: "Copy session ID"};
  await el("issues").listeners.click[0]({target: {closest: () => copyButton}});
  out.copied = copied;
  out.copyFeedback = copyButton.textContent;
  sandbox.navigator.clipboard = null;
  const fallbackButton = {dataset: {copy: "fallback-resume-command"}, textContent: "Copy resume command"};
  await el("issues").listeners.click[0]({target: {closest: () => fallbackButton}});
  out.fallbackCopied = fallbackCopied;
  out.fallbackFeedback = fallbackButton.textContent;
  await D.refreshRequests();
  out.listHtml = el("test-requests").innerHTML;
  out.views = {};
  for (const req of scenario.requests) {
    out.views[req.request_id] = {detail: D.renderRequestDetail(req), canCancel: D.canCancel(req)};
  }
  out.fetchesAfterRender = fetches.map(f => f.url);
  await D.loadJobLog(scenario.requests[0].request_id, scenario.requests[0].jobs[0].job_id);
  out.logHtml = el("log-" + scenario.requests[0].jobs[0].job_id).innerHTML;
  out.allFetches = fetches;
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""

MARKUP = '<img src=x onerror="alert(1)">'
SHA = "a" * 64
REQUESTED = {"argv": ["python", "-m", "pytest"], "parallelism": 4, "parallelism_args": ["-n", "{jobs}"],
             "memory_limit_mb": 2048, "timeout_s": 600}


def job(job_id, state, *, target="windows", runner="win-1", platform="windows", result=None, **extra):
    return {"job_id": job_id, "request_id": "r", "target": target, "state": state, "runner_id": runner,
            "runner_platform": platform, "lease_expires_at": None, "cancel_requested": False,
            "result": result, "log_stored_bytes": 12, "log_truncated": False,
            "created_at": "2026-11-27T10:00:00+00:00", "updated_at": "2026-11-27T10:01:00+00:00", **extra}


def delivery(state, reason=None):
    return {"state": state, "reason": reason, "message_id": "testdelivery_" + "b" * 32,
            "created_at": "2026-11-27T10:01:00+00:00"}


def request(request_id, jobs, *, state, delivery=None, cancel_requested=False):
    for j in jobs:
        j["request_id"] = request_id
    return {"request_id": request_id, "snapshot_sha256": SHA, "profile": REQUESTED,
            "delivery": delivery, "targets": [j["target"] for j in jobs], "jobs": jobs,
            "state": state, "cancel_requested": cancel_requested,
            "created_at": "2026-11-27T10:00:00+00:00", "updated_at": "2026-11-27T10:01:00+00:00"}


RESULT = {"status": "succeeded", "exit_code": 0, "timed_out": False, "duration_s": 3.5,
          "stdout_tail": "3 passed " + MARKUP, "stderr_tail": "", "error": None,
          "resources": {"parallelism": 8, "parallel_args_applied": ["-n", "8"],
                        "memory_limit_mb": 2048, "memory_limit_enforced": False}}


# (state, reason) pairs the API can report, besides the pending one on req-done.
DELIVERY_CASES = [("active", None), ("delivered", None), ("failed", "abandoned"),
                  ("undeliverable", "stale_generation " + MARKUP)]


def scenario():
    running = request("req-running" + MARKUP.replace(" ", ""),
                      [job("j-run", "running"), job("j-queued", "queued", target="linux", runner=None,
                                                     platform=None)],
                      state="running")
    done = request("req-done", [job("j-done", "succeeded", result=RESULT)], state="succeeded",
                   delivery=delivery("pending"))
    lost = request("req-lost", [job("j-lost", "lost", runner="linux-1", platform="linux")], state="lost")
    others = [request(f"req-{name}", [job(f"j-{name}", "succeeded", result=RESULT)], state="succeeded",
                      delivery=delivery(name, reason))
              for name, reason in DELIVERY_CASES]
    return {"requests": [running, done, lost, *others], "issue": {
        "issue": {"title": "Resume this", "id": "i-copy", "state": "active", "version": 1,
                  "opened_from": "claude", "goal": "continue", "cwd": "C:/repo"},
        "round": {"number": 1, "state": "active", "base_sha": SHA},
        "participants": {
            "claude": {"handle": {"adapter_type": "claude", "session_id": "claude-id",
                                    "cwd": "C:/worktree/O'Brien", "generation": 2}},
            "codex": {"handle": {"adapter_type": "codex", "session_id": "codex-id",
                                   "cwd": "C:/worktree", "generation": 1}},
        },
        "messages": [], "proposals": [], "decisions": [], "generation_history": [], "integrations": [],
    }, "responses": {
        "/test-requests?limit=50": {"status": 200, "body": {"requests": [running, done, lost]}},
        "/test-requests/": {"status": 200, "body": {
            "request_id": running["request_id"], "job_id": "j-run", "state": "running", "offset": 0,
            "next_offset": 12, "stored_bytes": 12, "eof": True, "truncated": False,
            "limit_bytes": 8388608, "text": "log " + MARKUP}},
    }}


@pytest.fixture(scope="module")
def run():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    def go(tmp_path):
        harness, data = tmp_path / "harness.js", tmp_path / "scenario.json"
        harness.write_text(HARNESS, encoding="utf-8")
        data.write_text(json.dumps(scenario()), encoding="utf-8")
        p = subprocess.run([node, str(harness), str(PAGE), str(data)], capture_output=True, text=True,
                           encoding="utf-8", timeout=60)
        assert p.returncode == 0, p.stderr
        return json.loads(p.stdout)
    return go


def test_request_list_refreshes_every_five_seconds_from_the_list_api(run, tmp_path):
    out = run(tmp_path)
    assert {"ms": 5000, "name": "refreshRequests"} in out["intervals"]
    assert "/test-requests?limit=50" in out["fetchesAtLoad"]
    assert "req-done" in out["listHtml"] and "req-lost" in out["listHtml"]


def test_untrusted_text_is_escaped_everywhere(run, tmp_path):
    out = run(tmp_path)
    for chunk in [out["listHtml"], out["logHtml"], *(v["detail"] for v in out["views"].values())]:
        assert "<img" not in chunk
    assert "&lt;img" in out["listHtml"] and "&lt;img" in out["logHtml"]


def test_resume_commands_use_the_saved_cli_handle_and_worktree(run, tmp_path):
    html = run(tmp_path)["issueHtml"]
    assert "claude --resume &#39;claude-id&#39;" in html
    assert "codex resume --all &#39;codex-id&#39;" in html
    assert "Set-Location -LiteralPath &#39;C:/worktree/O&#39;&#39;Brien&#39;" in html
    assert "Copy session ID" in html and "Copy resume command" in html


def test_copy_session_button_uses_clipboard_and_confirms_result(run, tmp_path):
    out = run(tmp_path)
    assert out["copied"] == "saved-session-token"
    assert out["copyFeedback"] == "Copied"
    assert out["fallbackCopied"] == "fallback-resume-command"
    assert out["fallbackFeedback"] == "Copied"
    assert "&lt;img" in out["views"]["req-done"]["detail"]


def test_running_job_shows_requested_resources_but_no_effective_ones(run, tmp_path):
    out = run(tmp_path)
    detail = next(v["detail"] for k, v in out["views"].items() if k.startswith("req-running"))
    assert "Requested resources" in detail and "2048" in detail
    assert "Effective resources" not in detail
    assert "win-1" in detail and "windows" in detail and SHA in detail
    assert "Session delivery" not in detail  # not terminal yet
    assert out["views"][next(k for k in out["views"] if k.startswith("req-running"))]["canCancel"] is True


def test_finished_job_shows_reported_resources_tails_and_pending_delivery(run, tmp_path):
    out = run(tmp_path)
    view = out["views"]["req-done"]
    detail = view["detail"]
    assert "Effective resources" in detail and "Requested resources" not in detail
    assert "memory_limit_enforced" in detail and "false" in detail
    assert "3 passed" in detail
    assert delivery_label("pending") in detail
    assert "Reason:" not in detail
    assert view["canCancel"] is False


def test_lost_job_explains_the_held_slot(run, tmp_path):
    out = run(tmp_path)
    view = out["views"]["req-lost"]
    assert "runner slot stays held" in view["detail"] and "manual resolution" in view["detail"]
    assert "linux-1" in view["detail"]
    assert view["canCancel"] is False
    assert "Session delivery" not in view["detail"]  # delivery is null


def delivery_label(state):
    return f'Session delivery: <span class="badge {state}">{state}</span>'


def test_delivery_fixtures_match_the_api_shape():
    fixtures = [r["delivery"] for r in scenario()["requests"] if r["delivery"] is not None]
    assert sorted(f["state"] for f in fixtures) == ["active", "delivered", "failed", "pending",
                                                    "undeliverable"]
    for fixture in fixtures:
        assert DeliveryStatusOut.model_validate(fixture).model_dump() == fixture


@pytest.mark.parametrize("state", [state for state, _ in DELIVERY_CASES])
def test_each_delivery_state_is_rendered(run, tmp_path, state):
    detail = run(tmp_path)["views"][f"req-{state}"]["detail"]
    assert delivery_label(state) in detail
    assert detail.count("Session delivery") == 1


def test_delivery_reason_is_shown_only_when_present_and_is_escaped(run, tmp_path):
    views = run(tmp_path)["views"]
    assert "Reason: <code>abandoned</code>" in views["req-failed"]["detail"]
    undeliverable = views["req-undeliverable"]["detail"]
    assert "Reason: <code>stale_generation &lt;img" in undeliverable and "<img" not in undeliverable
    for name in ("req-done", "req-active", "req-delivered"):
        assert "Reason:" not in views[name]["detail"], name


def test_delivery_note_exposes_no_ids_and_offers_no_retry(run, tmp_path):
    for view in run(tmp_path)["views"].values():
        assert "testdelivery_" not in view["detail"]
        assert "session_ref" not in view["detail"]
        assert "retry" not in view["detail"].lower().replace("never retried", "")


def test_logs_are_read_only_when_expanded(run, tmp_path):
    out = run(tmp_path)
    assert not any("/log?" in url for url in out["fetchesAfterRender"])
    log_calls = [f["url"] for f in out["allFetches"] if "/log?" in f["url"]]
    assert len(log_calls) == 1 and "job_id=j-run" in log_calls[0] and "limit=" in log_calls[0]
    assert not any(f["method"] == "POST" for f in out["allFetches"])
