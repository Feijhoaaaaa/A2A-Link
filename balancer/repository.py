import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- tasks -----------------------------------------------------------------

def add_task(conn: sqlite3.Connection, id: str, description: str, model_hint: str,
             status: str = "pending", error: str | None = None) -> None:
    now = _now()
    conn.execute(
        "INSERT INTO tasks (id, description, status, agent, model_hint, error, retry_count,"
        " created_at, updated_at) VALUES (?, ?, ?, NULL, ?, ?, 0, ?, ?)",
        (id, description, status, model_hint, error, now, now),
    )


def claim_task(conn: sqlite3.Connection, task_id: str, agent: str) -> bool:
    cur = conn.execute(
        "UPDATE tasks SET status='in_progress', agent=?, updated_at=?"
        " WHERE id=? AND status='pending'",
        (agent, _now(), task_id),
    )
    return cur.rowcount == 1


def complete_task(conn: sqlite3.Connection, task_id: str, result: str | None = None) -> None:
    conn.execute("UPDATE tasks SET status='done', error=NULL, result=?, updated_at=? WHERE id=?",
                 (result, _now(), task_id))


def fail_task(conn: sqlite3.Connection, task_id: str, error: str, max_retries: int = 2) -> str:
    """Record a failure; returns the new status ('pending' or 'needs_human')."""
    conn.execute(
        "UPDATE tasks SET retry_count = retry_count + 1, error=?,"
        " status = CASE WHEN retry_count + 1 > ? THEN 'needs_human' ELSE 'pending' END,"
        " updated_at=? WHERE id=?",
        (error, max_retries, _now(), task_id),
    )
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    return row["status"] if row else "unknown"


def get_task(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()


def list_tasks(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    if status is None:
        return conn.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
    return conn.execute("SELECT * FROM tasks WHERE status=? ORDER BY created_at",
                        (status,)).fetchall()


# --- usage / capabilities ----------------------------------------------------

def report_usage(conn: sqlite3.Connection, agent: str, pct_used: float,
                 window_started_at: str) -> None:
    conn.execute(
        "INSERT INTO usage (agent, window_started_at, pct_used) VALUES (?, ?, ?)"
        " ON CONFLICT(agent) DO UPDATE SET pct_used=excluded.pct_used,"
        " window_started_at=excluded.window_started_at",
        (agent, window_started_at, pct_used),
    )


def get_usage(conn: sqlite3.Connection, agent: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM usage WHERE agent=?", (agent,)).fetchone()


def list_usage(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM usage ORDER BY agent").fetchall()


def set_capabilities(conn: sqlite3.Connection, agent: str, capabilities: list[str]) -> None:
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM capabilities WHERE agent=?", (agent,))
        conn.executemany("INSERT INTO capabilities (agent, capability) VALUES (?, ?)",
                         [(agent, c) for c in capabilities])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def has_capability(conn: sqlite3.Connection, agent: str, capability: str) -> bool:
    return conn.execute("SELECT 1 FROM capabilities WHERE agent=? AND capability=?",
                        (agent, capability)).fetchone() is not None


# --- incidents -------------------------------------------------------------

def add_incident(conn: sqlite3.Connection, agent: str, kind: str, detail: str) -> int:
    cur = conn.execute(
        "INSERT INTO incidents (agent, kind, detail, created_at) VALUES (?, ?, ?, ?)",
        (agent, kind, detail, _now()),
    )
    return cur.lastrowid


def list_incidents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM incidents ORDER BY id").fetchall()


# --- issue MVP (v2) ----------------------------------------------------------
# Every function below runs inside one write_tx() transaction (BEGIN IMMEDIATE),
# receives `c` (that connection) and `now` (a clock string), and returns
# (http_status, body) or raises ApiError. The balancer only stores state: it never
# runs a CLI, runs git or touches the filesystem.

import hashlib
import json
import uuid
from pathlib import Path

LIVE_ATTEMPT_STATES = ("running", "owner_lost_suspected", "terminated")


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str = "", commit: bool = False, **ctx):
        super().__init__(f"{code}: {detail}")
        self.status, self.code, self.detail, self.commit, self.ctx = status, code, detail, commit, ctx

    def body(self) -> dict:
        return {"error": self.code, "detail": self.detail, **self.ctx}


def ts(dt: datetime) -> str:
    """Fixed-width UTC timestamp; strings of this form compare in time order."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _j(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _l(text):
    return None if text is None else json.loads(text)


def digest(value) -> str:
    return hashlib.sha256(_j(value).encode("utf-8")).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def expire_leases(c: sqlite3.Connection, now: str) -> None:
    """Lazy lease expiry: a lapsed lease only becomes `owner_lost_suspected`.

    Nothing else is time driven: the thread slot stays held and retry/abandon/takeover
    stay refused until a supervisor posts termination_confirmed.
    """
    c.execute("UPDATE attempts SET state='owner_lost_suspected'"
              " WHERE state='running' AND lease_expires_at < ?", (now,))
    c.execute("UPDATE integrations SET lease_state='owner_lost_suspected'"
              " WHERE lease_state='running' AND lease_expires_at < ?", (now,))


def _get_issue(c, issue_id):
    row = c.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", f"issue {issue_id!r} not found")
    return row


def _open_issue(c, issue_id):
    row = _get_issue(c, issue_id)
    if row["state"] == "closed":
        raise ApiError(409, "issue_closed", "the issue is closed")
    return row


def _bump(c, issue_id: str, now: str) -> int:
    c.execute("UPDATE issues SET version=version+1, updated_at=? WHERE id=?", (now, issue_id))
    return c.execute("SELECT version FROM issues WHERE id=?", (issue_id,)).fetchone()[0]


def _version(c, issue_id: str) -> int:
    return c.execute("SELECT version FROM issues WHERE id=?", (issue_id,)).fetchone()[0]


def _check_version(issue, expected: int) -> None:
    if issue["version"] != expected:
        raise ApiError(409, "stale_version", "issue version changed",
                       current_version=issue["version"])


def _participant(c, issue_id, key):
    row = c.execute("SELECT * FROM participants WHERE issue_id=? AND pkey=?",
                    (issue_id, key)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", f"participant {key!r} not found")
    return row


def _latest_round(c, issue_id):
    return c.execute("SELECT * FROM rounds WHERE issue_id=? ORDER BY number DESC LIMIT 1",
                     (issue_id,)).fetchone()


def _open_round(c, issue_id):
    return c.execute("SELECT * FROM rounds WHERE issue_id=? AND state='open'"
                     " ORDER BY number DESC LIMIT 1", (issue_id,)).fetchone()


def _handle_row(c, issue_id, key, generation):
    return c.execute("SELECT * FROM handles WHERE issue_id=? AND pkey=? AND generation=?",
                     (issue_id, key, generation)).fetchone()


def _handle_view(row):
    if row is None:
        return None
    return {"adapter_type": row["adapter_type"], "session_id": row["session_id"],
            "cwd": row["cwd"], "generation": row["generation"]}


def _attempt_row(c, issue_id, attempt_id):
    row = c.execute("SELECT * FROM attempts WHERE attempt_id=? AND issue_id=?",
                    (attempt_id, issue_id)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", f"attempt {attempt_id!r} not found")
    return row


def _attempt_view(row) -> dict:
    return {"attempt_id": row["attempt_id"], "number": row["number"], "state": row["state"],
            "owner": row["owner"], "process_identity": _l(row["process_identity"]),
            "lease_expires_at": row["lease_expires_at"],
            "termination_confirmed_at": row["termination_confirmed_at"]}


def _message_row(c, issue_id, message_id):
    row = c.execute("SELECT * FROM messages WHERE message_id=? AND issue_id=?",
                    (message_id, issue_id)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", f"message {message_id!r} not found")
    return row


# --- issues ------------------------------------------------------------------

def create_issue(c, now: str, root: str, b: dict):
    request_hash = digest(b)
    existing = c.execute("SELECT * FROM issues WHERE request_id=?", (b["request_id"],)).fetchone()
    if existing is not None:
        if existing["request_hash"] != request_hash:
            raise ApiError(409, "request_id_conflict", "request_id was used with a different body")
        return 200, json.loads(existing["response"])
    keys = b["participants"]
    if len(set(keys)) != len(keys):
        raise ApiError(422, "duplicate_participant", "participants must be unique")
    iid = new_id("i")
    rid = new_id("r")  # opaque and globally unique (rounds.id is a primary key); number stays 1
    c.execute("INSERT INTO issues (id, request_id, request_hash, title, goal, acceptance, cwd,"
              " opened_from, base_sha, state, version, created_at, updated_at)"
              " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 1, ?, ?)",
              (iid, b["request_id"], request_hash, b["title"], b["goal"], _j(b["acceptance"]),
               b["cwd"], b["opened_from"], b["base_sha"], now, now))
    c.execute("INSERT INTO rounds (id, issue_id, number, state, base_sha, created_at)"
              " VALUES (?, ?, 1, 'open', ?, ?)", (rid, iid, b["base_sha"], now))
    participants, paths = {}, {}
    for k in keys:
        c.execute("INSERT INTO participants (issue_id, pkey, generation) VALUES (?, ?, 1)", (iid, k))
        participants[k] = {"participant_id": f"{iid}:{k}", "generation": 1, "handle": None}
        paths[k] = {"worktree": (Path(root) / "worktrees" / f"{iid}-{k}").as_posix(),
                    "branch": f"a2a/issue-{iid}/{k}"}
    response = {
        "issue": {"id": iid, "title": b["title"], "state": "open", "version": 1,
                  "cwd": b["cwd"], "opened_from": b["opened_from"]},
        "round": {"id": rid, "number": 1, "state": "open", "base_sha": b["base_sha"]},
        "participants": participants, "paths": paths,
    }
    c.execute("UPDATE issues SET response=? WHERE id=?", (_j(response), iid))
    return 201, response


def list_issue_ids(c, limit: int = 20) -> list[str]:
    return [r[0] for r in c.execute("SELECT id FROM issues ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                    (limit,))]


def _integration_view(r) -> dict:
    return {
        "integration_id": r["integration_id"], "round_id": r["round_id"], "state": r["state"],
        "proposal_id": r["proposal_id"], "proposal_version": r["proposal_version"],
        "expected_base_sha": r["expected_base_sha"], "source_shas": _l(r["source_shas"]),
        "candidate_sha": r["candidate_sha"], "tested_sha": r["tested_sha"],
        "verification_argv": _l(r["verification_argv"]),
        "merge_output": _l(r["merge_output"]), "tests": _l(r["tests"]),
        "ref_update_output": _l(r["ref_update_output"]), "error": r["error"],
        "lease": {"owner": r["lease_owner"], "state": r["lease_state"],
                  "lease_expires_at": r["lease_expires_at"],
                  "process_identity": _l(r["process_identity"]),
                  "termination_confirmed_at": r["termination_confirmed_at"]},
    }


def issue_snapshot(c, issue_id: str) -> dict:
    issue = _get_issue(c, issue_id)
    rnd = _latest_round(c, issue_id)
    participants = {}
    for p in c.execute("SELECT * FROM participants WHERE issue_id=? ORDER BY rowid", (issue_id,)):
        key, gen = p["pkey"], p["generation"]
        thread = f"{issue_id}:{key}"
        handle = _handle_row(c, issue_id, key, gen)
        ready = c.execute("SELECT * FROM ready WHERE issue_id=? AND pkey=? AND round_id=?"
                          " AND state='current'", (issue_id, key, rnd["id"])).fetchone()
        active = c.execute("SELECT message_id FROM messages WHERE thread_key=? AND state='active'",
                           (thread,)).fetchone()
        blocked = None
        stuck = c.execute(
            "SELECT a.attempt_id, a.state, a.message_id FROM attempts a JOIN messages m"
            " ON m.message_id=a.message_id WHERE m.thread_key=?"
            " AND a.state IN ('owner_lost_suspected','terminated') LIMIT 1", (thread,)).fetchone()
        if stuck is not None:
            blocked = {"reason": stuck["state"], "attempt_id": stuck["attempt_id"],
                       "message_id": stuck["message_id"]}
        elif handle is None and c.execute(
                "SELECT 1 FROM messages WHERE thread_key=? AND generation=? AND kind='start'"
                " AND state IN ('failed','abandoned')", (thread, gen)).fetchone():
            blocked = {"reason": "start_lost"}
        participants[key] = {
            "participant_id": thread, "generation": gen, "handle": _handle_view(handle),
            "ready": None if ready is None else {
                "ready_id": ready["ready_id"], "round_id": ready["round_id"],
                "generation": ready["generation"], "attempt_id": ready["attempt_id"],
                "source_commit_sha": ready["source_commit_sha"],
                "expected_base_sha": ready["expected_base_sha"], "state": ready["state"]},
            "active_message_id": None if active is None else active["message_id"],
            "blocked": blocked,
        }
    proposals = [{"proposal_id": r["proposal_id"], "proposal_version": r["proposal_version"],
                  "round_id": r["round_id"], "by": r["by_who"], "text": r["body"],
                  "state": r["state"]}
                 for r in c.execute("SELECT * FROM proposals WHERE issue_id=? ORDER BY rowid", (issue_id,))]
    decisions = [{"id": r["id"], "round_id": r["round_id"], "proposal_id": r["proposal_id"],
                  "proposal_version": r["proposal_version"], "kind": r["kind"], "by": r["by_who"],
                  "note": r["note"], "at": r["created_at"]}
                 for r in c.execute("SELECT * FROM decisions WHERE issue_id=? ORDER BY rowid", (issue_id,))]
    messages = [_message_list_view(r)
                for r in c.execute("SELECT * FROM messages WHERE issue_id=? ORDER BY rowid", (issue_id,))]
    attempts = [{**_attempt_view(r), "message_id": r["message_id"], "started_at": r["started_at"],
                 "ended_at": r["ended_at"]}
                for r in c.execute("SELECT * FROM attempts WHERE issue_id=? ORDER BY rowid", (issue_id,))]
    integrations = [_integration_view(r) for r in
                    c.execute("SELECT * FROM integrations WHERE issue_id=? ORDER BY rowid", (issue_id,))]
    generation_history = [{"participant": r["pkey"], "from_generation": r["from_generation"],
                           "to_generation": r["to_generation"], "reason": r["reason"],
                           "handoff": _l(r["handoff"])}
                          for r in c.execute("SELECT * FROM generation_log WHERE issue_id=? ORDER BY id",
                                             (issue_id,))]
    ready_history = [{"participant": r["pkey"], "round_id": r["round_id"],
                      "generation": r["generation"], "attempt_id": r["attempt_id"],
                      "source_commit_sha": r["source_commit_sha"],
                      "expected_base_sha": r["expected_base_sha"], "state": r["state"]}
                     for r in c.execute("SELECT * FROM ready WHERE issue_id=? ORDER BY rowid", (issue_id,))]
    return {
        "issue": {"id": issue_id, "state": issue["state"], "version": issue["version"],
                  "opened_from": issue["opened_from"], "title": issue["title"],
                  "goal": issue["goal"], "acceptance": _l(issue["acceptance"]),
                  "cwd": issue["cwd"], "base_sha": issue["base_sha"],
                  "closed_reason": issue["closed_reason"]},
        "round": {"id": rnd["id"], "number": rnd["number"], "state": rnd["state"],
                  "base_sha": rnd["base_sha"]},
        "participants": participants, "proposals": proposals, "decisions": decisions,
        "messages": messages, "attempts": attempts, "integrations": integrations,
        "generation_history": generation_history, "ready_history": ready_history,
    }


# --- handles and generations ---------------------------------------------------

def save_handle(c, now: str, issue_id: str, key: str, b: dict):
    _open_issue(c, issue_id)
    part = _participant(c, issue_id, key)
    if b["generation"] != part["generation"]:
        raise ApiError(409, "stale_generation", "generation is not current",
                       current_generation=part["generation"])
    h = b["handle"]
    if key in ("claude", "codex") and h["adapter_type"] != key:
        raise ApiError(422, "adapter_mismatch", f"participant {key!r} uses adapter {key!r}")
    existing = _handle_row(c, issue_id, key, b["generation"])
    if existing is not None:
        if (existing["adapter_type"], existing["session_id"], existing["cwd"]) == \
                (h["adapter_type"], h["session_id"], h["cwd"]):
            return 200, {"participant_id": f"{issue_id}:{key}", "generation": b["generation"],
                         "handle": _handle_view(existing), "version": _version(c, issue_id)}
        raise ApiError(409, "handle_conflict", "this generation already has a different handle")
    if b.get("attempt_id") is None:
        raise ApiError(422, "attempt_id_required", "the first handle of a generation needs attempt_id")
    thread = f"{issue_id}:{key}"
    start = c.execute("SELECT * FROM messages WHERE thread_key=? AND generation=? AND kind='start'",
                      (thread, b["generation"])).fetchone()
    attempt = c.execute("SELECT * FROM attempts WHERE attempt_id=?", (b["attempt_id"],)).fetchone()
    if (start is None or attempt is None or start["current_attempt_id"] != b["attempt_id"]
            or attempt["state"] not in ("running", "owner_lost_suspected", "terminated", "done")):
        raise ApiError(409, "stale_attempt", "attempt_id is not this generation's start attempt",
                       current_attempt_id=None if start is None else start["current_attempt_id"])
    if c.execute("SELECT 1 FROM handles WHERE adapter_type=? AND session_id=?",
                 (h["adapter_type"], h["session_id"])).fetchone():
        raise ApiError(409, "session_in_use", "this session id already belongs to a participant")
    c.execute("INSERT INTO handles (issue_id, pkey, generation, adapter_type, session_id, cwd,"
              " start_attempt_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
              (issue_id, key, b["generation"], h["adapter_type"], h["session_id"], h["cwd"],
               b["attempt_id"], now))
    version = _bump(c, issue_id, now)
    return 200, {"participant_id": thread, "generation": b["generation"],
                 "handle": _handle_view(_handle_row(c, issue_id, key, b["generation"])),
                 "version": version}


def bump_generation(c, now: str, issue_id: str, key: str, b: dict):
    issue = _open_issue(c, issue_id)
    part = _participant(c, issue_id, key)
    _check_version(issue, b["expected_version"])
    if b["expected_generation"] != part["generation"]:
        raise ApiError(409, "stale_generation", "generation is not current",
                       current_generation=part["generation"])
    thread = f"{issue_id}:{key}"
    active = c.execute("SELECT message_id FROM messages WHERE thread_key=? AND state='active'",
                       (thread,)).fetchone()
    if active is not None:
        raise ApiError(409, "active_turn", "the thread has an active turn",
                       active_message_id=active["message_id"])
    new = part["generation"] + 1
    c.execute("UPDATE participants SET generation=? WHERE issue_id=? AND pkey=?", (new, issue_id, key))
    c.execute("INSERT INTO generation_log (issue_id, pkey, from_generation, to_generation, reason,"
              " handoff, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (issue_id, key, part["generation"], new, b["reason"], _j(b.get("handoff")), now))
    c.execute("UPDATE ready SET state='stale' WHERE issue_id=? AND pkey=? AND state='current'",
              (issue_id, key))
    current_round = _latest_round(c, issue_id)
    c.execute("UPDATE proposals SET state='open', accepted_ready_ids=NULL "
              "WHERE issue_id=? AND round_id=? AND state='accepted'",
              (issue_id, current_round["id"]))
    version = _bump(c, issue_id, now)
    return 200, {"participant_id": thread, "generation": new, "handle": None, "version": version}


# --- messages and attempts -------------------------------------------------------

def _message_list_view(m) -> dict:
    return {"message_id": m["message_id"], "participant": m["pkey"], "round_id": m["round_id"],
            "generation": m["generation"], "kind": m["kind"], "state": m["state"],
            "current_attempt_id": m["current_attempt_id"], "prompt": m["prompt"],
            "requested_profile": _l(m["requested_profile"]),
            "profile_source": m["profile_source"],
            "confirmed_profile": _l(m["confirmed_profile"]),
            "profile_mismatch": bool(m["profile_mismatch"]), "result": _l(m["result"]),
            "context": (_l(m["result"]) or {}).get("context"),
            "error": m["error"]}


def _message_response(c, m, execute: bool, status_version: int | None = None) -> dict:
    attempt = c.execute("SELECT * FROM attempts WHERE attempt_id=?", (m["current_attempt_id"],)).fetchone()
    return {
        "message_id": m["message_id"], "state": m["state"], "round_id": m["round_id"],
        "generation": m["generation"], "attempt": _attempt_view(attempt),
        "handle": _handle_view(_handle_row(c, m["issue_id"], m["pkey"], m["generation"])),
        "execute": execute, "version": _version(c, m["issue_id"]),
    }


def reserve_message(c, now: str, lease_until: str, issue_id: str, key: str, b: dict):
    issue = _get_issue(c, issue_id)
    part = _participant(c, issue_id, key)
    payload_hash = digest({k: b[k] for k in ("kind", "round_id", "generation",
                                              "requested_profile", "profile_source", "prompt")})
    m = c.execute("SELECT * FROM messages WHERE message_id=?", (b["message_id"],)).fetchone()
    if m is not None:
        if m["issue_id"] != issue_id or m["pkey"] != key or m["payload_hash"] != payload_hash:
            raise ApiError(409, "message_conflict", "message_id was used with a different payload")
        return 200, _message_response(c, m, execute=False)  # a replay never starts a run
    if issue["state"] == "closed":
        raise ApiError(409, "issue_closed", "the issue is closed")
    rnd = _open_round(c, issue_id)
    if rnd is None or rnd["id"] != b["round_id"]:
        raise ApiError(409, "stale_round", "round_id is not the open round",
                       current_round_id=None if rnd is None else rnd["id"])
    if b["generation"] != part["generation"]:
        raise ApiError(409, "stale_generation", "generation is not current",
                       current_generation=part["generation"])
    thread = f"{issue_id}:{key}"
    handle = _handle_row(c, issue_id, key, b["generation"])
    if b["kind"] == "continue":
        if handle is None:
            raise ApiError(409, "no_handle", "this generation has no handle yet; send a start message")
    elif handle is not None or c.execute(
            "SELECT 1 FROM messages WHERE thread_key=? AND generation=? AND kind='start'",
            (thread, b["generation"])).fetchone():
        raise ApiError(409, "start_exists", "this generation already has a start message")
    active = c.execute("SELECT * FROM messages WHERE thread_key=? AND state='active'", (thread,)).fetchone()
    if active is not None:
        a = c.execute("SELECT state FROM attempts WHERE attempt_id=?",
                      (active["current_attempt_id"],)).fetchone()
        raise ApiError(409, "active_turn", "the thread already has an active turn",
                       active_message_id=active["message_id"],
                       attempt_id=active["current_attempt_id"],
                       attempt_state=None if a is None else a["state"])
    attempt_id = new_id("a")
    try:
        c.execute("INSERT INTO messages (message_id, issue_id, pkey, thread_key, round_id, generation,"
                  " kind, state, payload_hash, requested_profile, profile_source, prompt,"
                  " current_attempt_id, created_at, updated_at)"
                  " VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)",
                  (b["message_id"], issue_id, key, thread, b["round_id"], b["generation"], b["kind"],
                   payload_hash, _j(b["requested_profile"]), b["profile_source"], b["prompt"],
                   attempt_id, now, now))
        c.execute("INSERT INTO attempts (attempt_id, issue_id, message_id, number, state, owner,"
                  " lease_expires_at, started_at) VALUES (?, ?, ?, 1, 'running', ?, ?, ?)",
                  (attempt_id, issue_id, b["message_id"], b["owner"], lease_until, now))
    except sqlite3.IntegrityError as e:
        raise ApiError(409, "active_turn", f"constraint: {e}") from None
    _bump(c, issue_id, now)
    m = c.execute("SELECT * FROM messages WHERE message_id=?", (b["message_id"],)).fetchone()
    return 201, _message_response(c, m, execute=True)


def list_messages(c, issue_id: str, key: str):
    _get_issue(c, issue_id)
    _participant(c, issue_id, key)
    out = []
    for m in c.execute("SELECT * FROM messages WHERE issue_id=? AND pkey=? ORDER BY rowid",
                       (issue_id, key)):
        view = _message_list_view(m)
        view["attempts"] = [_attempt_view(a) for a in c.execute(
            "SELECT * FROM attempts WHERE message_id=? ORDER BY number", (m["message_id"],))]
        out.append(view)
    return 200, out


def _require_owner(row_owner: str, owner: str) -> None:
    if row_owner != owner:
        raise ApiError(409, "not_owner", "owner does not match", owner=row_owner)


def _attempt_gate(a) -> None:
    """Common refusals for process/heartbeat/confirmation on a finished attempt."""
    if a["state"] == "abandoned":
        raise ApiError(409, "abandoned", "the attempt was abandoned")
    if a["state"] == "superseded":
        raise ApiError(409, "stale_attempt", "a newer attempt exists")
    if a["state"] in ("done", "failed"):
        raise ApiError(409, "attempt_finished", f"the attempt is already {a['state']}")


def attempt_process(c, now: str, issue_id: str, attempt_id: str, b: dict):
    a = _attempt_row(c, issue_id, attempt_id)
    _require_owner(a["owner"], b["owner"])
    _attempt_gate(a)
    if a["state"] == "terminated":
        raise ApiError(409, "terminated", "termination was already confirmed")
    ident = b["process_identity"]
    if a["process_identity"] is not None:
        if _l(a["process_identity"]) == ident:
            return 200, {"attempt_id": attempt_id, "process_identity": ident,
                         "version": _version(c, issue_id)}
        raise ApiError(409, "identity_conflict", "a different process identity is recorded")
    c.execute("UPDATE attempts SET process_identity=? WHERE attempt_id=?", (_j(ident), attempt_id))
    return 200, {"attempt_id": attempt_id, "process_identity": ident,
                 "version": _bump(c, issue_id, now)}


def attempt_heartbeat(c, now: str, lease_until: str, issue_id: str, attempt_id: str, b: dict):
    a = _attempt_row(c, issue_id, attempt_id)
    _require_owner(a["owner"], b["owner"])
    _attempt_gate(a)
    if a["state"] == "terminated":
        raise ApiError(409, "terminated", "termination was already confirmed")
    revived = a["state"] == "owner_lost_suspected"
    c.execute("UPDATE attempts SET state='running', lease_expires_at=? WHERE attempt_id=?",
              (lease_until, attempt_id))
    # A plain renewal changes no issue state, so it does not bump the version; a revival does.
    version = _bump(c, issue_id, now) if revived else _version(c, issue_id)
    return 200, {"attempt_id": attempt_id, "state": "running", "lease_expires_at": lease_until,
                 "version": version}


def _confirm_fields(b: dict, recorded) -> tuple[str, dict | None]:
    method, ident = b["method"], b.get("process_identity")
    if method == "never_started":
        if recorded is not None:
            raise ApiError(409, "identity_recorded", "a process identity is already recorded")
        if ident is not None:
            raise ApiError(422, "identity_not_allowed", "never_started carries no process_identity")
        return method, None
    if recorded is not None:
        if ident is None or ident != recorded:
            raise ApiError(409, "identity_mismatch", "process_identity differs from the recorded one")
        return method, recorded
    if ident is None:
        raise ApiError(422, "identity_required", "process_identity is required")
    return method, ident


def attempt_termination(c, now: str, issue_id: str, attempt_id: str, b: dict):
    a = _attempt_row(c, issue_id, attempt_id)
    _require_owner(a["owner"], b["owner"])
    if a["state"] == "terminated":
        same = (a["termination_method"] == b["method"]
                and (b.get("process_identity") is None or _l(a["process_identity"]) == b["process_identity"]))
        if same:
            return 200, {"attempt_id": attempt_id, "state": "terminated",
                         "termination_confirmed_at": a["termination_confirmed_at"],
                         "version": _version(c, issue_id)}
        raise ApiError(409, "confirmation_conflict", "a different confirmation is recorded")
    _attempt_gate(a)
    method, ident = _confirm_fields(b, _l(a["process_identity"]))
    c.execute("UPDATE attempts SET state='terminated', process_identity=?, termination_method=?,"
              " termination_confirmed_at=? WHERE attempt_id=?",
              (None if ident is None else _j(ident), method, now, attempt_id))
    return 200, {"attempt_id": attempt_id, "state": "terminated", "termination_confirmed_at": now,
                 "version": _bump(c, issue_id, now)}


def attempt_result(c, now: str, issue_id: str, attempt_id: str, b: dict):
    a = _attempt_row(c, issue_id, attempt_id)
    m = c.execute("SELECT * FROM messages WHERE message_id=?", (a["message_id"],)).fetchone()
    if a["state"] == "abandoned":
        raise ApiError(409, "abandoned", "the attempt was abandoned")
    if a["state"] == "superseded":
        raise ApiError(409, "stale_attempt", "a newer attempt exists",
                       current_attempt_id=m["current_attempt_id"])
    _require_owner(a["owner"], b["owner"])
    content = {k: b.get(k) for k in ("status", "text", "exit_code", "confirmed_profile",
                                      "context", "limits", "error")}
    if a["state"] in ("done", "failed"):
        if _l(a["result"]) == content:
            return 200, {"attempt_id": attempt_id, "state": a["state"], "message_state": m["state"],
                         "profile_mismatch": bool(m["profile_mismatch"]),
                         "version": _version(c, issue_id)}
        raise ApiError(409, "result_conflict", "a different result is recorded")
    if b["status"] == "failed" and not b.get("error"):
        raise ApiError(422, "error_required", "a failed result requires error")
    if m["kind"] == "start" and b["status"] == "done" and \
            _handle_row(c, issue_id, m["pkey"], m["generation"]) is None:
        raise ApiError(409, "handle_missing", "a start message needs a saved handle before it is done")
    confirmed = b.get("confirmed_profile") or {"model": None, "effort": None}
    requested = _l(m["requested_profile"]) or {}
    mismatch = int(requested.get("model") is not None and confirmed.get("model") is not None
                   and requested["model"] != confirmed["model"])
    c.execute("UPDATE attempts SET state=?, result=?, ended_at=? WHERE attempt_id=?",
              (b["status"], _j(content), now, attempt_id))
    c.execute("UPDATE messages SET state=?, confirmed_profile=?, profile_mismatch=?, result=?,"
              " error=?, updated_at=? WHERE message_id=?",
              (b["status"], _j(confirmed), mismatch,
               _j({"text": b.get("text"), "exit_code": b.get("exit_code"),
                   "context": b.get("context"), "limits": b.get("limits")}),
               b.get("error"), now, m["message_id"]))
    return 200, {"attempt_id": attempt_id, "state": b["status"], "message_state": b["status"],
                 "profile_mismatch": bool(mismatch), "version": _bump(c, issue_id, now)}


def retry_message(c, now: str, lease_until: str, issue_id: str, message_id: str, b: dict):
    _open_issue(c, issue_id)
    m = _message_row(c, issue_id, message_id)
    cur = c.execute("SELECT * FROM attempts WHERE attempt_id=?", (m["current_attempt_id"],)).fetchone()
    if b["expected_attempt"] != cur["attempt_id"]:
        if cur["retry_of"] == b["expected_attempt"] and cur["owner"] == b["owner"]:
            return 200, _message_response(c, m, execute=False)  # replay of a finished retry
        raise ApiError(409, "stale_attempt", "expected_attempt is not the current attempt",
                       current_attempt_id=cur["attempt_id"])
    if cur["state"] == "running":
        raise ApiError(409, "attempt_live", "the lease is live", lease_expires_at=cur["lease_expires_at"])
    if cur["state"] == "owner_lost_suspected":
        raise ApiError(409, "termination_unconfirmed",
                       "the lease expired but the child is not confirmed dead")
    if cur["state"] == "abandoned":
        raise ApiError(409, "abandoned", "the attempt was abandoned")
    if cur["state"] == "done":
        raise ApiError(409, "message_done", "the message is done")
    new_attempt = new_id("a")
    c.execute("UPDATE attempts SET state='superseded' WHERE attempt_id=?", (cur["attempt_id"],))
    other = c.execute("SELECT message_id FROM messages WHERE thread_key=? AND state='active'"
                      " AND message_id<>?", (m["thread_key"], message_id)).fetchone()
    if other is not None:
        raise ApiError(409, "active_turn", "another message holds the thread",
                       active_message_id=other["message_id"])
    c.execute("INSERT INTO attempts (attempt_id, issue_id, message_id, number, state, owner,"
              " lease_expires_at, retry_of, started_at) VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?)",
              (new_attempt, issue_id, message_id, cur["number"] + 1, b["owner"], lease_until,
               cur["attempt_id"], now))
    c.execute("UPDATE messages SET state='active', current_attempt_id=?, error=NULL, updated_at=?"
              " WHERE message_id=?", (new_attempt, now, message_id))
    _bump(c, issue_id, now)
    m = c.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
    return 201, _message_response(c, m, execute=True)


def abandon_message(c, now: str, issue_id: str, message_id: str, b: dict):
    m = _message_row(c, issue_id, message_id)
    cur = c.execute("SELECT * FROM attempts WHERE attempt_id=?", (m["current_attempt_id"],)).fetchone()
    if b["expected_attempt"] != cur["attempt_id"]:
        raise ApiError(409, "stale_attempt", "expected_attempt is not the current attempt",
                       current_attempt_id=cur["attempt_id"])
    if cur["state"] == "abandoned":
        return 200, {"message_id": message_id, "state": "abandoned", "attempt_id": cur["attempt_id"],
                     "attempt_state": "abandoned", "version": _version(c, issue_id)}
    if cur["state"] == "running":
        raise ApiError(409, "attempt_live", "the lease is live", lease_expires_at=cur["lease_expires_at"])
    if cur["state"] == "owner_lost_suspected":
        raise ApiError(409, "termination_unconfirmed",
                       "the lease expired but the child is not confirmed dead")
    if cur["state"] == "done":
        raise ApiError(409, "message_done", "the message is done")
    # failed or terminated: terminal for both records, in this one transaction
    c.execute("UPDATE attempts SET state='abandoned', ended_at=? WHERE attempt_id=?", (now, cur["attempt_id"]))
    c.execute("UPDATE messages SET state='abandoned', updated_at=? WHERE message_id=?", (now, message_id))
    return 200, {"message_id": message_id, "state": "abandoned", "attempt_id": cur["attempt_id"],
                 "attempt_state": "abandoned", "version": _bump(c, issue_id, now)}


# --- proposals, ready, decisions ----------------------------------------------------

def create_proposal(c, now: str, issue_id: str, b: dict):
    issue = _open_issue(c, issue_id)
    _check_version(issue, b["expected_version"])
    rnd = _open_round(c, issue_id)
    if rnd is None or rnd["id"] != b["round_id"]:
        raise ApiError(409, "stale_round", "round_id is not the open round")
    pid = new_id("p")
    c.execute("INSERT INTO proposals (proposal_id, issue_id, round_id, by_who, body, proposal_version,"
              " state, created_at) VALUES (?, ?, ?, ?, ?, 1, 'open', ?)",
              (pid, issue_id, b["round_id"], b["by"], b["text"], now))
    c.execute("INSERT INTO proposal_revisions (proposal_id, proposal_version, body, created_at)"
              " VALUES (?, 1, ?, ?)", (pid, b["text"], now))
    return 201, {"proposal_id": pid, "proposal_version": 1, "version": _bump(c, issue_id, now)}


def revise_proposal(c, now: str, issue_id: str, proposal_id: str, b: dict):
    issue = _open_issue(c, issue_id)
    p = c.execute("SELECT * FROM proposals WHERE proposal_id=? AND issue_id=?",
                  (proposal_id, issue_id)).fetchone()
    if p is None:
        raise ApiError(404, "not_found", f"proposal {proposal_id!r} not found")
    _check_version(issue, b["expected_version"])
    if p["proposal_version"] != b["expected_proposal_version"]:
        raise ApiError(409, "stale_proposal", "proposal version changed",
                       current_proposal_version=p["proposal_version"])
    new = p["proposal_version"] + 1
    c.execute("UPDATE proposals SET body=?, proposal_version=?, state='open', accepted_ready_ids=NULL"
              " WHERE proposal_id=?", (b["text"], new, proposal_id))
    c.execute("INSERT INTO proposal_revisions (proposal_id, proposal_version, body, created_at)"
              " VALUES (?, ?, ?, ?)", (proposal_id, new, b["text"], now))
    return 200, {"proposal_id": proposal_id, "proposal_version": new, "version": _bump(c, issue_id, now)}


def _clear_acceptance(c, issue_id: str, round_id: str) -> None:
    c.execute("UPDATE proposals SET state='open', accepted_ready_ids=NULL"
              " WHERE issue_id=? AND round_id=? AND state='accepted'", (issue_id, round_id))


def mark_ready(c, now: str, issue_id: str, key: str, b: dict):
    issue = _open_issue(c, issue_id)
    part = _participant(c, issue_id, key)
    _check_version(issue, b["expected_version"])
    rnd = _open_round(c, issue_id)
    if rnd is None or rnd["id"] != b["round_id"]:
        raise ApiError(409, "stale_round", "round_id is not the open round",
                       current_round_id=None if rnd is None else rnd["id"])
    if b["generation"] != part["generation"]:
        raise ApiError(409, "stale_generation", "generation is not current",
                       current_generation=part["generation"])
    thread = f"{issue_id}:{key}"
    active = c.execute("SELECT message_id FROM messages WHERE thread_key=? AND state='active'",
                       (thread,)).fetchone()
    if active is not None:
        raise ApiError(409, "active_turn", "the participant has an active turn",
                       active_message_id=active["message_id"])
    latest = c.execute(
        "SELECT a.* FROM attempts a JOIN messages m ON m.message_id=a.message_id"
        " WHERE m.thread_key=? AND m.round_id=? ORDER BY a.rowid DESC LIMIT 1",
        (thread, b["round_id"])).fetchone()
    if latest is None or latest["attempt_id"] != b["attempt_id"] or latest["state"] != "done":
        raise ApiError(409, "stale_attempt", "attempt_id is not the latest done attempt of this round",
                       current_attempt_id=None if latest is None else latest["attempt_id"])
    if b["expected_base_sha"] != rnd["base_sha"]:
        raise ApiError(409, "stale_base", "expected_base_sha is not the round base",
                       round_base_sha=rnd["base_sha"])
    c.execute("UPDATE ready SET state='superseded' WHERE issue_id=? AND pkey=? AND round_id=?"
              " AND state='current'", (issue_id, key, b["round_id"]))
    rid = new_id("rd")
    c.execute("INSERT INTO ready (ready_id, issue_id, pkey, round_id, generation, attempt_id,"
              " source_commit_sha, expected_base_sha, state, created_at)"
              " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'current', ?)",
              (rid, issue_id, key, b["round_id"], b["generation"], b["attempt_id"],
               b["source_commit_sha"], b["expected_base_sha"], now))
    _clear_acceptance(c, issue_id, b["round_id"])
    return 201, {"ready_id": rid, "round_id": b["round_id"], "source_commit_sha": b["source_commit_sha"],
                 "version": _bump(c, issue_id, now)}


def decide(c, now: str, issue_id: str, b: dict):
    issue = _open_issue(c, issue_id)
    _check_version(issue, b["expected_version"])
    rnd = _open_round(c, issue_id)
    if rnd is None or rnd["id"] != b["round_id"]:
        raise ApiError(409, "stale_round", "round_id is not the open round")
    p = c.execute("SELECT * FROM proposals WHERE proposal_id=? AND issue_id=? AND round_id=?",
                  (b["proposal_id"], issue_id, b["round_id"])).fetchone()
    if p is None:
        raise ApiError(404, "not_found", f"proposal {b['proposal_id']!r} not found in this round")
    if p["proposal_version"] != b["proposal_version"]:
        raise ApiError(409, "stale_proposal", "proposal version changed",
                       current_proposal_version=p["proposal_version"])
    ready_ids: list[str] = []
    kind = b["kind"]
    if kind == "accept":
        missing = []
        for part in c.execute("SELECT pkey FROM participants WHERE issue_id=? ORDER BY rowid", (issue_id,)):
            r = c.execute("SELECT ready_id FROM ready WHERE issue_id=? AND pkey=? AND round_id=?"
                          " AND state='current'", (issue_id, part["pkey"], b["round_id"])).fetchone()
            if r is None:
                missing.append(part["pkey"])
            else:
                ready_ids.append(r["ready_id"])
        if missing:
            raise ApiError(409, "not_ready", "every participant needs a current ready", missing=missing)
        _clear_acceptance(c, issue_id, b["round_id"])
        c.execute("UPDATE proposals SET state='accepted', accepted_ready_ids=? WHERE proposal_id=?",
                  (_j(ready_ids), b["proposal_id"]))
    elif kind == "reject":
        c.execute("UPDATE proposals SET state='rejected', accepted_ready_ids=NULL WHERE proposal_id=?",
                  (b["proposal_id"],))
    elif kind == "request_changes":
        c.execute("UPDATE proposals SET state='changes_requested', accepted_ready_ids=NULL"
                  " WHERE proposal_id=?", (b["proposal_id"],))
    else:  # new_round
        _clear_acceptance(c, issue_id, b["round_id"])
        c.execute("UPDATE rounds SET state='closed' WHERE id=?", (b["round_id"],))
        c.execute("INSERT INTO rounds (id, issue_id, number, state, base_sha, created_at)"
                  " VALUES (?, ?, ?, 'open', ?, ?)",
                  (new_id("r"), issue_id, rnd["number"] + 1, rnd["base_sha"], now))
    did = new_id("d")
    c.execute("INSERT INTO decisions (id, issue_id, round_id, proposal_id, proposal_version, kind,"
              " by_who, note, ready_ids, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
              (did, issue_id, b["round_id"], b["proposal_id"], b["proposal_version"], kind,
               b["by"], b.get("note"), _j(ready_ids), now))
    return 201, {"id": did, "kind": kind, "version": _bump(c, issue_id, now)}


# --- integration (two-phase records; the balancer never runs git) -----------------------

def _integration_row(c, issue_id, integration_id):
    row = c.execute("SELECT * FROM integrations WHERE integration_id=? AND issue_id=?",
                    (integration_id, issue_id)).fetchone()
    if row is None:
        raise ApiError(404, "not_found", f"integration {integration_id!r} not found")
    return row


def start_integration(c, now: str, lease_until: str, issue_id: str, b: dict):
    issue = _get_issue(c, issue_id)
    request_hash = digest({k: b[k] for k in b if k != "expected_version"})
    existing = c.execute("SELECT * FROM integrations WHERE integration_request_id=?",
                         (b["integration_request_id"],)).fetchone()
    if existing is not None:
        if existing["issue_id"] != issue_id or existing["request_hash"] != request_hash:
            raise ApiError(409, "request_id_conflict", "integration_request_id used with a different body")
        return 200, _integration_start_view(existing, _version(c, issue_id))
    if issue["state"] == "closed":
        raise ApiError(409, "issue_closed", "the issue is closed")
    _check_version(issue, b["expected_version"])
    rnd = _open_round(c, issue_id)
    if rnd is None or rnd["id"] != b["round_id"]:
        raise ApiError(409, "stale_round", "round_id is not the open round")
    p = c.execute("SELECT * FROM proposals WHERE proposal_id=? AND issue_id=? AND round_id=?",
                  (b["proposal_id"], issue_id, b["round_id"])).fetchone()
    if (p is None or p["state"] != "accepted" or p["proposal_version"] != b["proposal_version"]
            or p["accepted_ready_ids"] is None):
        raise ApiError(409, "not_accepted", "the round has no accept for this proposal version")
    expected = {}
    for rid in _l(p["accepted_ready_ids"]):
        r = c.execute("SELECT * FROM ready WHERE ready_id=?", (rid,)).fetchone()
        if r is None or r["state"] != "current":
            raise ApiError(409, "ready_changed", "an accepted ready is no longer current")
        expected[r["pkey"]] = r["source_commit_sha"]
    if b["source_shas"] != expected:
        raise ApiError(409, "ready_changed", "source_shas differs from the accepted ready set",
                       expected=expected)
    if b["expected_base_sha"] != issue["base_sha"] or b["expected_base_sha"] != rnd["base_sha"]:
        raise ApiError(409, "stale_base", "expected_base_sha is not the current base",
                       base_sha=issue["base_sha"])
    if c.execute("SELECT 1 FROM integrations WHERE issue_id=? AND state IN"
                 " ('merging','testing','tested','advancing')", (issue_id,)).fetchone():
        raise ApiError(409, "integration_active", "another integration is not finished")
    iid = new_id("g")
    try:
        c.execute("INSERT INTO integrations (integration_id, issue_id, integration_request_id,"
                  " request_hash, round_id, proposal_id, proposal_version, source_shas,"
                  " expected_base_sha, verification_argv, state, lease_owner, lease_state,"
                  " lease_expires_at, created_at, updated_at)"
                  " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'merging', ?, 'running', ?, ?, ?)",
                  (iid, issue_id, b["integration_request_id"], request_hash, b["round_id"],
                   b["proposal_id"], b["proposal_version"], _j(b["source_shas"]),
                   b["expected_base_sha"], _j(b["verification_argv"]), b["owner"], lease_until,
                   now, now))
    except sqlite3.IntegrityError as e:
        raise ApiError(409, "integration_active", f"constraint: {e}") from None
    _bump(c, issue_id, now)
    row = _integration_row(c, issue_id, iid)
    return 201, _integration_start_view(row, _version(c, issue_id))


def _integration_start_view(r, version: int) -> dict:
    return {"integration_id": r["integration_id"], "state": r["state"],
            "source_shas": _l(r["source_shas"]), "expected_base_sha": r["expected_base_sha"],
            "verification_argv": _l(r["verification_argv"]),
            "lease": {"owner": r["lease_owner"], "state": r["lease_state"],
                      "lease_expires_at": r["lease_expires_at"]},
            "version": version}


def _int_owner(r, owner: str) -> None:
    _require_owner(r["lease_owner"], owner)


def _int_lease_gate(r) -> None:
    if r["lease_state"] == "released":
        raise ApiError(409, "integration_finished", "the integration is finished")


def integration_process(c, now: str, issue_id: str, integration_id: str, b: dict):
    r = _integration_row(c, issue_id, integration_id)
    _int_owner(r, b["owner"])
    _int_lease_gate(r)
    if r["lease_state"] == "terminated":
        raise ApiError(409, "terminated", "termination was already confirmed")
    ident = b["process_identity"]
    if r["process_identity"] is not None:
        if _l(r["process_identity"]) == ident:
            return 200, {"integration_id": integration_id, "process_identity": ident,
                         "version": _version(c, issue_id)}
        raise ApiError(409, "identity_conflict", "a different process identity is recorded")
    c.execute("UPDATE integrations SET process_identity=?, updated_at=? WHERE integration_id=?",
              (_j(ident), now, integration_id))
    return 200, {"integration_id": integration_id, "process_identity": ident,
                 "version": _bump(c, issue_id, now)}


def integration_heartbeat(c, now: str, lease_until: str, issue_id: str, integration_id: str, b: dict):
    r = _integration_row(c, issue_id, integration_id)
    _int_owner(r, b["owner"])
    _int_lease_gate(r)
    if r["lease_state"] == "terminated":
        raise ApiError(409, "terminated", "termination was already confirmed")
    revived = r["lease_state"] == "owner_lost_suspected"
    c.execute("UPDATE integrations SET lease_state='running', lease_expires_at=?, updated_at=?"
              " WHERE integration_id=?", (lease_until, now, integration_id))
    version = _bump(c, issue_id, now) if revived else _version(c, issue_id)
    return 200, {"integration_id": integration_id, "lease_state": "running",
                 "lease_expires_at": lease_until, "version": version}


def integration_termination(c, now: str, issue_id: str, integration_id: str, b: dict):
    r = _integration_row(c, issue_id, integration_id)
    _int_owner(r, b["owner"])
    if r["lease_state"] == "terminated":
        same = (r["termination_method"] == b["method"]
                and (b.get("process_identity") is None or _l(r["process_identity"]) == b["process_identity"]))
        if same:
            return 200, {"integration_id": integration_id, "lease_state": "terminated",
                         "termination_confirmed_at": r["termination_confirmed_at"],
                         "version": _version(c, issue_id)}
        raise ApiError(409, "confirmation_conflict", "a different confirmation is recorded")
    _int_lease_gate(r)
    method, ident = _confirm_fields(b, _l(r["process_identity"]))
    c.execute("UPDATE integrations SET lease_state='terminated', process_identity=?,"
              " termination_method=?, termination_confirmed_at=?, updated_at=? WHERE integration_id=?",
              (None if ident is None else _j(ident), method, now, now, integration_id))
    return 200, {"integration_id": integration_id, "lease_state": "terminated",
                 "termination_confirmed_at": now, "version": _bump(c, issue_id, now)}


def integration_takeover(c, now: str, lease_until: str, issue_id: str, integration_id: str, b: dict):
    r = _integration_row(c, issue_id, integration_id)
    if r["state"] not in ("merging", "testing", "tested", "advancing"):
        raise ApiError(409, "integration_finished", "the integration is finished")
    if r["lease_owner"] == b["owner"] and r["takeover_from"] == b["expected_owner"] \
            and r["lease_state"] == "running" and r["state"] == b["expected_stage"]:
        return 200, _integration_start_view(r, _version(c, issue_id))  # replay
    if r["lease_owner"] != b["expected_owner"]:
        raise ApiError(409, "owner_mismatch", "expected_owner is not the lease owner",
                       owner=r["lease_owner"])
    if r["state"] != b["expected_stage"]:
        raise ApiError(409, "stage_mismatch", "expected_stage is not the current stage", stage=r["state"])
    if r["lease_state"] == "running":
        raise ApiError(409, "lease_live", "the lease is live", lease_expires_at=r["lease_expires_at"])
    if r["lease_state"] == "owner_lost_suspected":
        raise ApiError(409, "termination_unconfirmed",
                       "the lease expired but the worker is not confirmed dead")
    c.execute("UPDATE integrations SET lease_owner=?, lease_state='running', lease_expires_at=?,"
              " process_identity=NULL, termination_confirmed_at=NULL, termination_method=NULL,"
              " takeover_from=?, updated_at=? WHERE integration_id=?",
              (b["owner"], lease_until, b["expected_owner"], now, integration_id))
    _bump(c, issue_id, now)
    return 200, _integration_start_view(_integration_row(c, issue_id, integration_id),
                                        _version(c, issue_id))


def _int_alive(c, r) -> None:
    """An owner that reports is alive; refuse only a confirmed-dead or finished lease."""
    _int_lease_gate(r)
    if r["lease_state"] == "terminated":
        raise ApiError(409, "terminated", "termination was already confirmed; take over first")


def integration_candidate(c, now: str, issue_id: str, integration_id: str, b: dict):
    r = _integration_row(c, issue_id, integration_id)
    _int_owner(r, b["owner"])
    if r["state"] != "merging":
        same = (r["candidate_sha"] is not None and b["status"] == "merged"
                and b.get("candidate_sha") == r["candidate_sha"])
        if same:
            return 200, {"integration_id": integration_id, "state": r["state"],
                         "candidate_sha": r["candidate_sha"], "version": _version(c, issue_id)}
        raise ApiError(409, "result_conflict", f"the integration is {r['state']}, not merging")
    _int_alive(c, r)
    if b["status"] == "merged" and not b.get("candidate_sha"):
        raise ApiError(422, "candidate_required", "merged requires candidate_sha")
    if b["status"] == "conflict" and not b.get("detail"):
        raise ApiError(422, "detail_required", "conflict requires detail")
    state = "testing" if b["status"] == "merged" else "conflict"
    c.execute("UPDATE integrations SET state=?, candidate_sha=?, merge_output=?, error=?,"
              " lease_state=?, lease_expires_at=CASE WHEN ?='testing' THEN lease_expires_at END,"
              " updated_at=? WHERE integration_id=?",
              (state, b.get("candidate_sha"), _j(b.get("output")), b.get("detail"),
               r["lease_state"] if state == "testing" else "released", state, now, integration_id))
    return 200, {"integration_id": integration_id, "state": state,
                 "candidate_sha": b.get("candidate_sha"), "version": _bump(c, issue_id, now)}


def integration_tests(c, now: str, issue_id: str, integration_id: str, b: dict):
    r = _integration_row(c, issue_id, integration_id)
    _int_owner(r, b["owner"])
    if r["tests"] is not None:
        recorded = _l(r["tests"])
        if recorded == _tests_record(b):
            return 200, {"integration_id": integration_id, "state": r["state"],
                         "version": _version(c, issue_id)}
        raise ApiError(409, "result_conflict", "a different test result is recorded")
    if r["state"] != "testing":
        raise ApiError(409, "bad_stage", f"the integration is {r['state']}, not testing")
    _int_alive(c, r)
    if b["argv"] != _l(r["verification_argv"]):
        raise ApiError(409, "argv_mismatch", "argv differs from the stored verification_argv",
                       expected=_l(r["verification_argv"]))
    if b["tested_sha"] != r["candidate_sha"]:
        raise ApiError(409, "tested_sha_mismatch", "tested_sha differs from candidate_sha",
                       candidate_sha=r["candidate_sha"])
    if b["status"] == "pass":
        if not _l(r["verification_argv"]):
            raise ApiError(409, "verification_empty", "an empty verification_argv cannot pass")
        if b["exit_code"] != 0:
            raise ApiError(422, "exit_code_mismatch", "pass requires exit_code 0")
    state = "tested" if b["status"] == "pass" else "failed"
    c.execute("UPDATE integrations SET state=?, tested_sha=?, tests=?, lease_state=?, updated_at=?"
              " WHERE integration_id=?",
              (state, b["tested_sha"], _j(_tests_record(b)),
               r["lease_state"] if state == "tested" else "released", now, integration_id))
    return 200, {"integration_id": integration_id, "state": state, "version": _bump(c, issue_id, now)}


def _tests_record(b: dict) -> dict:
    return {"status": b["status"], "exit_code": b["exit_code"], "stdout": b.get("stdout", ""),
            "stderr": b.get("stderr", ""), "truncated": b.get("truncated", False),
            "duration_s": b.get("duration_s"), "argv": b["argv"], "tested_sha": b["tested_sha"]}


def integration_advance(c, now: str, issue_id: str, integration_id: str, b: dict):
    issue = _get_issue(c, issue_id)
    r = _integration_row(c, issue_id, integration_id)
    _int_owner(r, b["owner"])
    if r["state"] == "advancing":
        return 200, {"integration_id": integration_id, "state": "advancing",
                     "expected_base_sha": r["expected_base_sha"], "candidate_sha": r["candidate_sha"],
                     "version": _version(c, issue_id)}
    if r["state"] != "tested":
        raise ApiError(409, "bad_stage", f"the integration is {r['state']}, not tested")
    _check_version(issue, b["expected_version"])
    _int_alive(c, r)
    tests = _l(r["tests"]) or {}
    if not _l(r["verification_argv"]):
        raise ApiError(409, "verification_empty", "an empty verification_argv blocks advance")
    if tests.get("status") != "pass" or r["tested_sha"] != r["candidate_sha"]:
        raise ApiError(409, "tested_sha_mismatch", "the candidate was not tested and passed")
    if issue["base_sha"] != r["expected_base_sha"]:
        raise ApiError(409, "stale_base", "the base moved", base_sha=issue["base_sha"])
    c.execute("UPDATE integrations SET state='advancing', updated_at=? WHERE integration_id=?",
              (now, integration_id))
    return 200, {"integration_id": integration_id, "state": "advancing",
                 "expected_base_sha": r["expected_base_sha"], "candidate_sha": r["candidate_sha"],
                 "version": _bump(c, issue_id, now)}


def integration_result(c, now: str, issue_id: str, integration_id: str, b: dict):
    issue = _get_issue(c, issue_id)
    r = _integration_row(c, issue_id, integration_id)
    _int_owner(r, b["owner"])
    if r["state"] in ("advanced", "failed") and r["state"] == _result_state(b):
        recorded_ok = (b["status"] == "advanced" and b.get("new_base_sha") == r["candidate_sha"]) \
            or (b["status"] == "failed" and b.get("error") == r["error"])
        if recorded_ok:
            return 200, {"integration_id": integration_id, "state": r["state"],
                         "version": _version(c, issue_id)}
    if r["state"] != "advancing":
        raise ApiError(409, "result_conflict", f"the integration is {r['state']}, not advancing")
    _int_alive(c, r)
    if b["status"] == "failed":
        if not b.get("error"):
            raise ApiError(422, "error_required", "a failed result requires error")
        c.execute("UPDATE integrations SET state='failed', error=?, ref_update_output=?,"
                  " lease_state='released', updated_at=? WHERE integration_id=?",
                  (b["error"], _j(b.get("ref_update_output")), now, integration_id))
        return 200, {"integration_id": integration_id, "state": "failed", "version": _bump(c, issue_id, now)}
    if b.get("new_base_sha") != r["candidate_sha"]:
        raise ApiError(422, "new_base_mismatch", "new_base_sha must equal candidate_sha")
    cur = c.execute("UPDATE issues SET base_sha=?, version=version+1, updated_at=?"
                    " WHERE id=? AND base_sha=?",
                    (r["candidate_sha"], now, issue_id, r["expected_base_sha"]))
    if cur.rowcount != 1:
        c.execute("UPDATE integrations SET state='failed', error='diverged', ref_update_output=?,"
                  " lease_state='released', updated_at=? WHERE integration_id=?",
                  (_j(b.get("ref_update_output")), now, integration_id))
        _bump(c, issue_id, now)
        raise ApiError(409, "stale_base", "the base moved; recorded as diverged", commit=True,
                       base_sha=issue["base_sha"])
    c.execute("UPDATE integrations SET state='advanced', ref_update_output=?, lease_state='released',"
              " updated_at=? WHERE integration_id=?",
              (_j(b.get("ref_update_output")), now, integration_id))
    rnd = c.execute("SELECT * FROM rounds WHERE id=?", (r["round_id"],)).fetchone()
    c.execute("UPDATE rounds SET state='closed' WHERE id=?", (rnd["id"],))
    next_round = new_id("r")
    c.execute("INSERT INTO rounds (id, issue_id, number, state, base_sha, created_at)"
              " VALUES (?, ?, ?, 'open', ?, ?)",
              (next_round, issue_id, rnd["number"] + 1, r["candidate_sha"], now))
    return 200, {"integration_id": integration_id, "state": "advanced",
                 "base_sha": r["candidate_sha"], "round": {"id": next_round,
                 "number": rnd["number"] + 1, "state": "open", "base_sha": r["candidate_sha"]},
                 "version": _version(c, issue_id)}


def _result_state(b: dict) -> str:
    return "advanced" if b["status"] == "advanced" else "failed"


def close_issue(c, now: str, issue_id: str, b: dict):
    issue = _open_issue(c, issue_id)
    _check_version(issue, b["expected_version"])
    if c.execute("SELECT 1 FROM integrations WHERE issue_id=? AND state IN"
                 " ('merging','testing','tested','advancing')", (issue_id,)).fetchone():
        raise ApiError(409, "integration_active", "an integration is not finished")
    c.execute("UPDATE issues SET state='closed', closed_reason=? WHERE id=?", (b["reason"], issue_id))
    unconfirmed = [r["attempt_id"] for r in c.execute(
        "SELECT attempt_id FROM attempts WHERE issue_id=? AND state IN ('running','owner_lost_suspected')"
        " ORDER BY rowid", (issue_id,))]
    return 200, {"state": "closed", "version": _bump(c, issue_id, now),
                 "unconfirmed_attempts": unconfirmed}
