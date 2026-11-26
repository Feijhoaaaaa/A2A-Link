import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    description TEXT,
    status TEXT,
    agent TEXT,
    model_hint TEXT,
    error TEXT,
    retry_count INTEGER DEFAULT 0,
    result TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS usage (
    agent TEXT PRIMARY KEY,
    window_started_at TEXT,
    pct_used REAL
);
CREATE TABLE IF NOT EXISTS capabilities (
    agent TEXT,
    capability TEXT,
    PRIMARY KEY (agent, capability)
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT,
    kind TEXT,
    detail TEXT,
    created_at TEXT
);
"""

# Issue MVP (v2). New tables only; the v1 tables above are untouched.
ISSUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    title TEXT,
    goal TEXT,
    acceptance TEXT,
    cwd TEXT,
    opened_from TEXT,
    base_sha TEXT NOT NULL,
    state TEXT NOT NULL,
    version INTEGER NOT NULL,
    response TEXT,
    closed_reason TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS rounds (
    id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL,
    number INTEGER NOT NULL,
    state TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    created_at TEXT,
    UNIQUE (issue_id, number)
);
CREATE TABLE IF NOT EXISTS participants (
    issue_id TEXT NOT NULL,
    pkey TEXT NOT NULL,
    generation INTEGER NOT NULL,
    PRIMARY KEY (issue_id, pkey)
);
CREATE TABLE IF NOT EXISTS generation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id TEXT NOT NULL,
    pkey TEXT NOT NULL,
    from_generation INTEGER,
    to_generation INTEGER,
    reason TEXT,
    handoff TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS handles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id TEXT NOT NULL,
    pkey TEXT NOT NULL,
    generation INTEGER NOT NULL,
    adapter_type TEXT NOT NULL,
    session_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    start_attempt_id TEXT,
    created_at TEXT,
    UNIQUE (issue_id, pkey, generation),
    UNIQUE (adapter_type, session_id)
);
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL,
    pkey TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    round_id TEXT,
    generation INTEGER NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    requested_profile TEXT,
    profile_source TEXT,
    confirmed_profile TEXT,
    prompt TEXT,
    profile_mismatch INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT,
    current_attempt_id TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_active_turn
    ON messages (thread_key) WHERE state = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_one_start
    ON messages (thread_key, generation) WHERE kind = 'start';
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    number INTEGER NOT NULL,
    state TEXT NOT NULL,
    owner TEXT NOT NULL,
    process_identity TEXT,
    lease_expires_at TEXT,
    termination_confirmed_at TEXT,
    termination_method TEXT,
    retry_of TEXT,
    result TEXT,
    started_at TEXT,
    ended_at TEXT,
    UNIQUE (message_id, number)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_unresolved
    ON attempts (message_id) WHERE state IN ('running', 'owner_lost_suspected', 'terminated');
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    by_who TEXT NOT NULL,
    body TEXT NOT NULL,
    proposal_version INTEGER NOT NULL,
    state TEXT NOT NULL,
    accepted_ready_ids TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS proposal_revisions (
    proposal_id TEXT NOT NULL,
    proposal_version INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT,
    PRIMARY KEY (proposal_id, proposal_version)
);
CREATE TABLE IF NOT EXISTS ready (
    ready_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL,
    pkey TEXT NOT NULL,
    round_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    attempt_id TEXT NOT NULL,
    source_commit_sha TEXT NOT NULL,
    expected_base_sha TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ready_current
    ON ready (issue_id, pkey, round_id) WHERE state = 'current';
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    proposal_version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    by_who TEXT NOT NULL,
    note TEXT,
    ready_ids TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS integrations (
    integration_id TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL,
    integration_request_id TEXT NOT NULL UNIQUE,
    request_hash TEXT NOT NULL,
    round_id TEXT NOT NULL,
    proposal_id TEXT NOT NULL,
    proposal_version INTEGER NOT NULL,
    source_shas TEXT NOT NULL,
    expected_base_sha TEXT NOT NULL,
    verification_argv TEXT NOT NULL,
    state TEXT NOT NULL,
    candidate_sha TEXT,
    tested_sha TEXT,
    merge_output TEXT,
    tests TEXT,
    ref_update_output TEXT,
    error TEXT,
    lease_owner TEXT,
    lease_state TEXT,
    lease_expires_at TEXT,
    process_identity TEXT,
    termination_confirmed_at TEXT,
    termination_method TEXT,
    takeover_from TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_integrations_one_active
    ON integrations (issue_id) WHERE state IN ('merging', 'testing', 'tested', 'advancing');
"""


def get_connection(path: str) -> sqlite3.Connection:
    # isolation_level=None: autocommit; each statement is its own transaction,
    # which is what makes the single-statement claim in repository.py atomic.
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    try:  # WAL lets several balancer processes read while one writes
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    conn.executescript(SCHEMA)
    # Migrate databases created before the `result` column existed.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    if "result" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN result TEXT")
    conn.executescript(ISSUE_SCHEMA)
    # Databases created before profile resolution have no profile_source column.
    msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
    if "profile_source" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN profile_source TEXT")
    generation_cols = {r[1] for r in conn.execute("PRAGMA table_info(generation_log)")}
    if "handoff" not in generation_cols:
        conn.execute("ALTER TABLE generation_log ADD COLUMN handoff TEXT")


@contextmanager
def write_tx(path: str):
    """One request = one connection = one BEGIN IMMEDIATE transaction.

    BEGIN IMMEDIATE takes the write lock up front, so writers in any number of
    balancer processes serialise and every check-then-write below is race free.
    An exception rolls back, except one carrying `commit = True` (used to record
    a divergence and still answer 409).
    """
    conn = get_connection(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException as e:
            if getattr(e, "commit", False):
                conn.execute("COMMIT")
            else:
                conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.close()
