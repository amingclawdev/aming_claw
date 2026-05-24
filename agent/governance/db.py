"""SQLite database layer for governance runtime state.

Manages:
  - Connection lifecycle (per-project databases)
  - Schema creation and migration
  - WAL mode for concurrent read/write
"""

import os
import sys
import sqlite3
import threading
from pathlib import Path

_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from utils import tasks_root


SCHEMA_VERSION = 43

_SQLITE_WRITE_LOCK = threading.RLock()


def sqlite_write_lock() -> threading.RLock:
    """Process-local write serialization for governance SQLite mutations.

    SQLite WAL permits concurrent readers but still has a single writer.  The
    governance HTTP server is threaded, so short task/queue writes can collide
    inside one process before SQLite's busy timeout has a chance to smooth the
    flow.  Callers should hold this lock only around direct DB mutation +
    commit blocks, never around model calls or slow external work.
    """
    return _SQLITE_WRITE_LOCK

SCHEMA_SQL = """
-- Node runtime state
CREATE TABLE IF NOT EXISTS node_state (
    project_id    TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    verify_status TEXT NOT NULL DEFAULT 'pending',
    build_status  TEXT NOT NULL DEFAULT 'impl:missing',
    evidence_json TEXT,
    updated_by    TEXT,
    updated_at    TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (project_id, node_id)
);

-- Node state history (event sourcing auxiliary)
CREATE TABLE IF NOT EXISTS node_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    from_status   TEXT,
    to_status     TEXT NOT NULL,
    role          TEXT NOT NULL,
    evidence_json TEXT,
    session_id    TEXT,
    ts            TEXT NOT NULL,
    version       INTEGER NOT NULL
);

-- Session management
CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    principal_id  TEXT NOT NULL,
    project_id    TEXT NOT NULL,
    role          TEXT NOT NULL,
    scope_json    TEXT,
    token_hash    TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL DEFAULT 'active',
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    last_heartbeat TEXT,
    metadata_json TEXT
);

-- Task registry (v4: upgraded from file-based)
CREATE TABLE IF NOT EXISTS tasks (
    task_id       TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'created',
    type          TEXT NOT NULL DEFAULT 'task',
    prompt        TEXT,
    related_nodes TEXT,
    assigned_to   TEXT,
    created_by    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    started_at    TEXT,
    completed_at  TEXT,
    result_json   TEXT,
    error_message TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    priority      INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT,
    retry_round   INTEGER NOT NULL DEFAULT 0,
    parent_task_id TEXT
);
-- idx_tasks_status and idx_tasks_assigned created in migration v2

-- Task attempts (retry tracking)
CREATE TABLE IF NOT EXISTS task_attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT NOT NULL REFERENCES tasks(task_id),
    attempt_num   INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'running',
    started_at    TEXT NOT NULL,
    completed_at  TEXT,
    result_json   TEXT,
    error_message TEXT
);

-- Append-only task implementation timeline. Backlog rows describe the work;
-- timeline rows describe what agents/executors/gates actually did.
CREATE TABLE IF NOT EXISTS task_timeline_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id           TEXT NOT NULL,
    backlog_id           TEXT NOT NULL DEFAULT '',
    mf_id                TEXT NOT NULL DEFAULT '',
    task_id              TEXT NOT NULL DEFAULT '',
    attempt_num          INTEGER NOT NULL DEFAULT 0,
    event_type           TEXT NOT NULL,
    phase                TEXT NOT NULL DEFAULT '',
    event_kind           TEXT NOT NULL DEFAULT '',
    scenario_id          TEXT NOT NULL DEFAULT '',
    parent_event_id      INTEGER NOT NULL DEFAULT 0,
    correlation_id       TEXT NOT NULL DEFAULT '',
    severity             TEXT NOT NULL DEFAULT '',
    decision             TEXT NOT NULL DEFAULT '',
    schema_version       INTEGER NOT NULL DEFAULT 2,
    actor                TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL DEFAULT '',
    payload_json         TEXT NOT NULL DEFAULT '{}',
    verification_json    TEXT NOT NULL DEFAULT '{}',
    artifact_refs_json   TEXT NOT NULL DEFAULT '{}',
    trace_id             TEXT NOT NULL DEFAULT '',
    commit_sha           TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_timeline_task
    ON task_timeline_events(project_id, task_id, attempt_num, id);
CREATE INDEX IF NOT EXISTS idx_task_timeline_backlog
    ON task_timeline_events(project_id, backlog_id, id);
CREATE INDEX IF NOT EXISTS idx_task_timeline_trace
    ON task_timeline_events(project_id, trace_id, id);

-- Idempotency keys
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idem_key      TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);

-- Audit index (raw events in JSONL, this is the query index)
CREATE TABLE IF NOT EXISTS audit_index (
    event_id      TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    event         TEXT NOT NULL,
    actor         TEXT,
    ok            INTEGER NOT NULL DEFAULT 1,
    ts            TEXT NOT NULL,
    node_ids      TEXT
);

-- Version snapshots (for rollback)
CREATE TABLE IF NOT EXISTS snapshots (
    project_id    TEXT NOT NULL,
    version       INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    created_by    TEXT,
    PRIMARY KEY (project_id, version)
);

-- Event outbox (transactional outbox pattern)
CREATE TABLE IF NOT EXISTS event_outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    project_id    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    delivered_at  TEXT,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    dead_letter   INTEGER NOT NULL DEFAULT 0,
    trace_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON event_outbox(delivered_at) WHERE delivered_at IS NULL AND dead_letter = 0;
CREATE INDEX IF NOT EXISTS idx_outbox_dead ON event_outbox(dead_letter) WHERE dead_letter = 1;

-- Per-project chain version (auto-chain integrity seal)
CREATE TABLE IF NOT EXISTS project_version (
    project_id    TEXT PRIMARY KEY,
    chain_version TEXT NOT NULL,     -- git short hash from last auto-merge
    updated_at    TEXT NOT NULL,     -- ISO 8601
    updated_by    TEXT NOT NULL,     -- "auto-chain" | "init" | "register"
    git_head      TEXT DEFAULT '',   -- current git HEAD (synced by executor)
    dirty_files   TEXT DEFAULT '[]', -- JSON array of uncommitted files
    git_synced_at TEXT DEFAULT ''    -- when executor last synced git status
);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_session_principal ON sessions(principal_id, project_id);
CREATE INDEX IF NOT EXISTS idx_session_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_session_token ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_audit_project_ts ON audit_index(project_id, ts);
CREATE INDEX IF NOT EXISTS idx_audit_ok ON audit_index(ok);
CREATE INDEX IF NOT EXISTS idx_idem_expires ON idempotency_keys(expires_at);
CREATE INDEX IF NOT EXISTS idx_node_history_project ON node_history(project_id, node_id, ts);

-- Chain context events (event-sourced, append-only)
CREATE TABLE IF NOT EXISTS chain_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    root_task_id  TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    ts            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chain_events_root ON chain_events(root_task_id, ts);
CREATE INDEX IF NOT EXISTS idx_chain_events_task ON chain_events(task_id, event_type, ts);

-- Durable intake for structured AI output envelopes.
CREATE TABLE IF NOT EXISTS ai_outputs (
    output_id                    TEXT PRIMARY KEY,
    project_id                   TEXT NOT NULL,
    snapshot_id                  TEXT NOT NULL DEFAULT '',
    base_commit                  TEXT NOT NULL DEFAULT '',
    task_type                    TEXT NOT NULL,
    target_type                  TEXT NOT NULL DEFAULT '',
    target_id                    TEXT NOT NULL DEFAULT '',
    producer                     TEXT NOT NULL DEFAULT '',
    source_run_id                TEXT NOT NULL DEFAULT '',
    provider                     TEXT NOT NULL DEFAULT '',
    model                        TEXT NOT NULL DEFAULT '',
    prompt_hash                  TEXT NOT NULL DEFAULT '',
    payload_hash                 TEXT NOT NULL DEFAULT '',
    dedupe_key                   TEXT NOT NULL,
    idempotency_key              TEXT NOT NULL DEFAULT '',
    status                       TEXT NOT NULL DEFAULT 'submitted',
    route_status                 TEXT NOT NULL DEFAULT 'queued',
    payload_json                 TEXT NOT NULL DEFAULT '{}',
    self_precheck_json           TEXT NOT NULL DEFAULT '{}',
    graph_query_trace_ids_json   TEXT NOT NULL DEFAULT '[]',
    metadata_json                TEXT NOT NULL DEFAULT '{}',
    created_by                   TEXT NOT NULL DEFAULT '',
    created_at                   TEXT NOT NULL,
    updated_at                   TEXT NOT NULL,
    UNIQUE(project_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_ai_outputs_project_created
    ON ai_outputs(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ai_outputs_project_type_status
    ON ai_outputs(project_id, task_type, status);
CREATE INDEX IF NOT EXISTS idx_ai_outputs_target
    ON ai_outputs(project_id, target_type, target_id);

CREATE TABLE IF NOT EXISTS ai_output_events (
    id                           INTEGER PRIMARY KEY AUTOINCREMENT,
    output_id                    TEXT NOT NULL,
    project_id                   TEXT NOT NULL,
    event_type                   TEXT NOT NULL,
    actor                        TEXT NOT NULL DEFAULT '',
    request_id                   TEXT NOT NULL DEFAULT '',
    payload_json                 TEXT NOT NULL DEFAULT '{}',
    created_at                   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_output_events_output
    ON ai_output_events(output_id, id);
CREATE INDEX IF NOT EXISTS idx_ai_output_events_project
    ON ai_output_events(project_id, created_at);

CREATE TABLE IF NOT EXISTS ai_output_queue (
    output_id                    TEXT PRIMARY KEY,
    project_id                   TEXT NOT NULL,
    task_type                    TEXT NOT NULL,
    target_type                  TEXT NOT NULL DEFAULT '',
    target_id                    TEXT NOT NULL DEFAULT '',
    status                       TEXT NOT NULL DEFAULT 'queued',
    priority                     INTEGER NOT NULL DEFAULT 0,
    attempt_count                INTEGER NOT NULL DEFAULT 0,
    max_attempts                 INTEGER NOT NULL DEFAULT 3,
    lease_token                  TEXT NOT NULL DEFAULT '',
    claimed_by                   TEXT NOT NULL DEFAULT '',
    claimed_at                   TEXT NOT NULL DEFAULT '',
    lease_expires_at             TEXT NOT NULL DEFAULT '',
    last_error                   TEXT NOT NULL DEFAULT '',
    created_at                   TEXT NOT NULL,
    updated_at                   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_output_queue_project_status
    ON ai_output_queue(project_id, status, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_ai_output_queue_project_type
    ON ai_output_queue(project_id, task_type, status);

-- Gate events audit trail (queryable gate history per task)
CREATE TABLE IF NOT EXISTS gate_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    gate_name     TEXT NOT NULL,
    passed        INTEGER NOT NULL,
    reason        TEXT,
    trace_id      TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gate_events_project_task ON gate_events(project_id, task_id);

-- Pending nodes: inferred doc associations awaiting human review (P4)
CREATE TABLE IF NOT EXISTS pending_nodes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    TEXT NOT NULL,
    node_id       TEXT NOT NULL,
    doc_path      TEXT NOT NULL,
    confidence    REAL NOT NULL DEFAULT 0.0,
    reason        TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    created_at    TEXT NOT NULL,
    reviewed_at   TEXT,
    reviewed_by   TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_nodes_project ON pending_nodes(project_id, status);
CREATE INDEX IF NOT EXISTS idx_pending_nodes_node ON pending_nodes(project_id, node_id);

-- Backlog bugs (DB-first backlog storage, OPT-DB-BACKLOG)
CREATE TABLE IF NOT EXISTS backlog_bugs (
    bug_id              TEXT PRIMARY KEY,
    title               TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'OPEN',
    priority            TEXT NOT NULL DEFAULT 'P3',
    target_files        TEXT NOT NULL DEFAULT '[]',
    test_files          TEXT NOT NULL DEFAULT '[]',
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    chain_task_id       TEXT NOT NULL DEFAULT '',
    "commit"            TEXT NOT NULL DEFAULT '',
    discovered_at       TEXT NOT NULL DEFAULT '',
    fixed_at            TEXT NOT NULL DEFAULT '',
    details_md          TEXT NOT NULL DEFAULT '',
    chain_trigger_json  TEXT NOT NULL DEFAULT '{}',
    required_docs       TEXT NOT NULL DEFAULT '[]',
    provenance_paths    TEXT NOT NULL DEFAULT '[]',
    chain_stage         TEXT NOT NULL DEFAULT '',
    last_failure_reason TEXT NOT NULL DEFAULT '',
    stage_updated_at    TEXT NOT NULL DEFAULT '',
    runtime_state       TEXT NOT NULL DEFAULT '',
    current_task_id     TEXT NOT NULL DEFAULT '',
    root_task_id        TEXT NOT NULL DEFAULT '',
    worktree_path       TEXT NOT NULL DEFAULT '',
    worktree_branch     TEXT NOT NULL DEFAULT '',
    bypass_policy_json  TEXT NOT NULL DEFAULT '{}',
    mf_type             TEXT NOT NULL DEFAULT '',
    takeover_json       TEXT NOT NULL DEFAULT '{}',
    runtime_updated_at  TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_backlog_bugs_status ON backlog_bugs(status);
CREATE INDEX IF NOT EXISTS idx_backlog_bugs_priority ON backlog_bugs(priority);

-- Reconcile sessions (CR0a: one-active-per-project invariant + state machine)
CREATE TABLE IF NOT EXISTS reconcile_sessions (
    project_id              TEXT NOT NULL,
    session_id              TEXT NOT NULL,
    run_id                  TEXT,
    status                  TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active','finalizing','finalize_failed','finalized','rolled_back')),
    started_at              TEXT NOT NULL,
    finalized_at            TEXT,
    cluster_count_total     INTEGER NOT NULL DEFAULT 0,
    cluster_count_resolved  INTEGER NOT NULL DEFAULT 0,
    cluster_count_failed    INTEGER NOT NULL DEFAULT 0,
    bypass_gates_json       TEXT NOT NULL DEFAULT '[]',
    started_by              TEXT,
    snapshot_path           TEXT,
    snapshot_head_sha       TEXT,
    base_commit_sha         TEXT NOT NULL DEFAULT '',
    target_branch           TEXT NOT NULL DEFAULT '',
    target_head_sha         TEXT NOT NULL DEFAULT '',
    finalize_error_json     TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (project_id, session_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reconcile_sessions_one_active
    ON reconcile_sessions (project_id)
    WHERE status IN ('active','finalizing','finalize_failed');

-- Reconcile batch memory (PM semantic merge context for cluster batches)
CREATE TABLE IF NOT EXISTS reconcile_batch_memory (
    project_id       TEXT NOT NULL,
    batch_id         TEXT NOT NULL,
    session_id       TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'active',
    memory_json      TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    created_by       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project_id, batch_id)
);
CREATE INDEX IF NOT EXISTS idx_reconcile_batch_memory_session
    ON reconcile_batch_memory (project_id, session_id);
CREATE INDEX IF NOT EXISTS idx_reconcile_batch_memory_status
    ON reconcile_batch_memory (project_id, status);

-- Reconcile file inventory (full-project coverage ledger before graph rebase finalize)
CREATE TABLE IF NOT EXISTS reconcile_file_inventory (
    project_id        TEXT NOT NULL,
    run_id            TEXT NOT NULL,
    path              TEXT NOT NULL,
    file_kind         TEXT NOT NULL DEFAULT '',
    language          TEXT NOT NULL DEFAULT '',
    sha256            TEXT NOT NULL DEFAULT '',
    file_hash         TEXT NOT NULL DEFAULT '',
    size_bytes        INTEGER NOT NULL DEFAULT 0,
    last_scanned_commit TEXT NOT NULL DEFAULT '',
    graph_status      TEXT NOT NULL DEFAULT '',
    mapped_node_ids   TEXT NOT NULL DEFAULT '[]',
    attached_node_ids TEXT NOT NULL DEFAULT '[]',
    attachment_role   TEXT NOT NULL DEFAULT '',
    attachment_source TEXT NOT NULL DEFAULT '',
    scan_status       TEXT NOT NULL DEFAULT '',
    cluster_id        TEXT NOT NULL DEFAULT '',
    candidate_node_id TEXT NOT NULL DEFAULT '',
    attached_to       TEXT NOT NULL DEFAULT '',
    reason            TEXT NOT NULL DEFAULT '',
    decision          TEXT NOT NULL DEFAULT 'pending',
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (project_id, run_id, path)
);
CREATE INDEX IF NOT EXISTS idx_reconcile_file_inventory_status
    ON reconcile_file_inventory (project_id, run_id, scan_status);
CREATE INDEX IF NOT EXISTS idx_reconcile_file_inventory_kind
    ON reconcile_file_inventory (project_id, run_id, file_kind);

-- Commit-bound graph asset projection. JSON artifacts remain replay/debug
-- exports; this table is the runtime projection for doc/test/config assets.
CREATE TABLE IF NOT EXISTS graph_asset_projection (
    project_id              TEXT NOT NULL,
    snapshot_id             TEXT NOT NULL DEFAULT '',
    run_id                  TEXT NOT NULL DEFAULT '',
    commit_sha              TEXT NOT NULL DEFAULT '',
    asset_kind              TEXT NOT NULL DEFAULT '',
    asset_path              TEXT NOT NULL DEFAULT '',
    file_kind               TEXT NOT NULL DEFAULT '',
    sha256                  TEXT NOT NULL DEFAULT '',
    file_hash               TEXT NOT NULL DEFAULT '',
    size_bytes              INTEGER NOT NULL DEFAULT 0,
    scan_status             TEXT NOT NULL DEFAULT '',
    graph_status            TEXT NOT NULL DEFAULT '',
    binding_status          TEXT NOT NULL DEFAULT '',
    impact_scope_policy     TEXT NOT NULL DEFAULT '',
    accepted_bindings_json  TEXT NOT NULL DEFAULT '[]',
    binding_candidates_json TEXT NOT NULL DEFAULT '[]',
    metadata_json           TEXT NOT NULL DEFAULT '{}',
    source_projection       TEXT NOT NULL DEFAULT '',
    updated_at              TEXT NOT NULL,
    PRIMARY KEY (project_id, snapshot_id, commit_sha, asset_kind, asset_path)
);
CREATE INDEX IF NOT EXISTS idx_graph_asset_projection_snapshot
    ON graph_asset_projection (project_id, snapshot_id, asset_kind, binding_status);
CREATE INDEX IF NOT EXISTS idx_graph_asset_projection_path
    ON graph_asset_projection (project_id, asset_kind, asset_path);

CREATE TABLE IF NOT EXISTS graph_asset_bindings (
    project_id          TEXT NOT NULL,
    snapshot_id         TEXT NOT NULL DEFAULT '',
    commit_sha          TEXT NOT NULL DEFAULT '',
    asset_kind          TEXT NOT NULL DEFAULT '',
    asset_path          TEXT NOT NULL DEFAULT '',
    binding_status      TEXT NOT NULL DEFAULT '',
    node_id             TEXT NOT NULL DEFAULT '',
    title               TEXT NOT NULL DEFAULT '',
    role                TEXT NOT NULL DEFAULT '',
    source              TEXT NOT NULL DEFAULT '',
    binding_key         TEXT NOT NULL DEFAULT '',
    evidence_json       TEXT NOT NULL DEFAULT '{}',
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (project_id, snapshot_id, commit_sha, asset_kind, asset_path, binding_status, node_id, binding_key)
);
CREATE INDEX IF NOT EXISTS idx_graph_asset_bindings_node
    ON graph_asset_bindings (project_id, snapshot_id, node_id, asset_kind, binding_status);
CREATE INDEX IF NOT EXISTS idx_graph_asset_bindings_path
    ON graph_asset_bindings (project_id, asset_kind, asset_path);
"""


def _governance_root() -> Path:
    """Root directory for governance data."""
    return Path(tasks_root()) / "state" / "governance"


def _normalize_id(pid: str) -> str:
    """Normalize project ID inline (avoid circular import with project_service)."""
    import re
    s = pid.strip()
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1-\2', s)
    s = re.sub(r'[\s_]+', '-', s)
    s = re.sub(r'-+', '-', s)
    return s.lower().strip('-')


def _resolve_project_dir(project_id: str) -> Path:
    """Resolve the actual project directory, handling normalize mismatch.

    Tries normalized ID first, then raw ID as fallback. This handles the case
    where data was created with the raw ID (e.g., 'amingClaw') before normalize
    was enforced (P0-1), so the directory on disk doesn't match the normalized
    form ('aming-claw').
    """
    root = _governance_root()
    normalized = _normalize_id(project_id) if project_id else project_id
    normalized_dir = root / normalized
    if normalized_dir.exists():
        return normalized_dir
    # Fallback: try raw project_id (handles pre-normalize data)
    raw_dir = root / project_id
    if raw_dir.exists():
        return raw_dir
    # Neither exists — use normalized (will be created)
    return normalized_dir


def _project_db_path(project_id: str) -> Path:
    """Path to the SQLite database for a specific project."""
    project_dir = _resolve_project_dir(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir / "governance.db"


def _configure_connection(conn: sqlite3.Connection, busy_timeout: int) -> None:
    """Apply portable SQLite connection settings.

    Some shared-volume mounts reject WAL creation even when the DB file exists.
    In that case, fall back to DELETE journal mode so the governance service
    stays available instead of failing every request.
    """
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute(f"PRAGMA busy_timeout={busy_timeout}")
    except sqlite3.OperationalError:
        pass


def get_connection(project_id: str) -> sqlite3.Connection:
    """Get a SQLite connection for a project, creating/migrating schema if needed.

    Returns:
        sqlite3.Connection: An open, fully-configured connection to the
        per-project governance database.

    Connection configuration applied on every call:

    WAL mode (PRAGMA journal_mode=WAL):
        Enables Write-Ahead Logging, which allows concurrent readers to proceed
        without being blocked by an active writer.  This is important because
        multiple agents may query the database simultaneously while a write
        transaction is in progress.

    Foreign-key enforcement (PRAGMA foreign_keys=ON):
        SQLite does not enforce foreign-key constraints by default; this PRAGMA
        activates referential-integrity checks for the lifetime of the
        connection (e.g. task_attempts.task_id → tasks.task_id).

    Busy timeout (PRAGMA busy_timeout=5000):
        Instructs SQLite to wait up to 5 000 ms before raising
        ``OperationalError: database is locked`` when another connection holds
        an exclusive lock.  This prevents spurious failures under brief write
        contention.

    Row factory (sqlite3.Row):
        Sets ``conn.row_factory = sqlite3.Row`` so that every fetched row
        supports both index-based and column-name-based access
        (``row["column_name"]`` as well as ``row[0]``).

    Auto-schema migration (_ensure_schema):
        ``_ensure_schema(conn)`` is called on every new connection.  It runs
        the full ``SCHEMA_SQL`` block (``CREATE TABLE IF NOT EXISTS …``) to
        create tables on first use, then checks the stored ``schema_version``
        against ``SCHEMA_VERSION`` (currently {version}) and runs any
        outstanding incremental migration functions up to that target version.
        This means callers never need to manage schema lifecycle manually.
    """.format(version=SCHEMA_VERSION)
    db_path = _project_db_path(project_id)

    # On Docker restart, stale WAL locks may block new connections.
    # SQLite automatically recovers WAL state on first connect, but only
    # if the -shm file is accessible. Increase timeout to handle this.
    conn = sqlite3.connect(str(db_path), timeout=30)
    _configure_connection(conn, busy_timeout=10000)
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection):
    """Create all required tables if they do not already exist, then run any pending migrations.

    On first use, executes the full ``SCHEMA_SQL`` block (``CREATE TABLE IF NOT
    EXISTS …``) to initialise every table and index in the governance database.
    Subsequent calls are safe because every statement uses ``IF NOT EXISTS``.

    After the baseline schema is applied, the stored ``schema_version`` value is
    read from the ``schema_meta`` table and compared against the module-level
    ``SCHEMA_VERSION`` constant.  For each version step between the current and
    target version, the corresponding incremental migration function is executed
    in order to bring the schema up to date (e.g. adding new columns, creating
    new indexes, or back-filling data).  When all pending migrations have run,
    the stored version is updated to reflect the new baseline.
    """
    conn.executescript(SCHEMA_SQL)

    # Check and set schema version
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        current_version = int(row["value"]) if row else 0
    except sqlite3.OperationalError:
        current_version = 0

    if current_version < SCHEMA_VERSION:
        _run_migrations(conn, current_version, SCHEMA_VERSION)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        conn.commit()


def _run_migrations(conn: sqlite3.Connection, from_version: int, to_version: int):
    """Run incremental migrations between versions.

    Add migration functions as the schema evolves:
        MIGRATIONS = {
            1: _migrate_v0_to_v1,
            2: _migrate_v1_to_v2,
        }
    """
    def _migrate_v1_to_v2(c):
        """Add new columns to tasks table + event_outbox + task_attempts."""
        # Add missing columns to tasks (ALTER TABLE ADD is safe for existing data)
        for col, typedef in [
            ("type", "TEXT NOT NULL DEFAULT 'task'"),
            ("prompt", "TEXT"),
            ("assigned_to", "TEXT"),
            ("started_at", "TEXT"),
            ("completed_at", "TEXT"),
            ("result_json", "TEXT"),
            ("error_message", "TEXT"),
            ("attempt_count", "INTEGER NOT NULL DEFAULT 0"),
            ("max_attempts", "INTEGER NOT NULL DEFAULT 3"),
            ("priority", "INTEGER NOT NULL DEFAULT 0"),
            ("metadata_json", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Create task_attempts table if not exists
        c.execute("""CREATE TABLE IF NOT EXISTS task_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            attempt_num INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            started_at TEXT NOT NULL,
            completed_at TEXT,
            result_json TEXT,
            error_message TEXT
        )""")

        # Create event_outbox if not exists (may already be from schema)
        c.execute("""CREATE TABLE IF NOT EXISTS event_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            project_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            delivered_at TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT,
            dead_letter INTEGER NOT NULL DEFAULT 0,
            trace_id TEXT
        )""")

        # Create indexes
        try:
            c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(project_id, status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_assigned ON tasks(assigned_to, status)")
        except sqlite3.OperationalError:
            pass

    def _migrate_v2_to_v3(c):
        """Add dual-field status model to tasks."""
        for col, typedef in [
            ("execution_status", "TEXT NOT NULL DEFAULT 'queued'"),
            ("notification_status", "TEXT NOT NULL DEFAULT 'none'"),
            ("notified_at", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass
        # Sync execution_status from status for existing rows
        try:
            c.execute("UPDATE tasks SET execution_status = status WHERE execution_status = 'queued' AND status != 'queued'")
        except sqlite3.OperationalError:
            pass

    def _migrate_v3_to_v4(c):
        """Add retry_round and parent_task_id fields to tasks for QA→Dev escalation."""
        for col, typedef in [
            ("retry_round", "INTEGER NOT NULL DEFAULT 0"),
            ("parent_task_id", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists

    def _migrate_v4_to_v5(c):
        """Add project_version table for chain integrity seal."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS project_version (
                project_id    TEXT PRIMARY KEY,
                chain_version TEXT NOT NULL,
                updated_at    TEXT NOT NULL,
                updated_by    TEXT NOT NULL
            )
        """)

    def _migrate_v5_to_v6(c):
        """Add git sync columns to project_version (executor writes git status)."""
        for col, typedef in [
            ("git_head", "TEXT DEFAULT ''"),
            ("dirty_files", "TEXT DEFAULT '[]'"),
            ("git_synced_at", "TEXT DEFAULT ''"),
        ]:
            try:
                c.execute(f"ALTER TABLE project_version ADD COLUMN {col} {typedef}")
            except Exception:
                pass  # column already exists

    def _migrate_v6_to_v7(c):
        """Add memories table with FTS5 full-text search for Phase 2 memory backend."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                memory_id   TEXT PRIMARY KEY,
                project_id  TEXT NOT NULL,
                ref_id      TEXT NOT NULL DEFAULT '',
                kind        TEXT NOT NULL DEFAULT 'knowledge',
                module_id   TEXT NOT NULL DEFAULT '',
                scope       TEXT NOT NULL DEFAULT 'project',
                content     TEXT NOT NULL DEFAULT '',
                summary     TEXT NOT NULL DEFAULT '',
                metadata_json TEXT,
                tags        TEXT NOT NULL DEFAULT '',
                version     INTEGER NOT NULL DEFAULT 1,
                status      TEXT NOT NULL DEFAULT 'active',
                superseded_by_memory_id TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_memories_project_ref ON memories(project_id, ref_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_memories_project_status ON memories(project_id, status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_memories_module ON memories(project_id, module_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(project_id, kind)")

        # FTS5 virtual table for full-text search
        c.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                content, summary, module_id, kind,
                content='memories',
                content_rowid='rowid'
            )
        """)

        # FTS5 sync triggers: keep FTS index in sync with memories table
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_fts_insert AFTER INSERT ON memories BEGIN
                INSERT INTO memories_fts(rowid, content, summary, module_id, kind)
                VALUES (new.rowid, new.content, new.summary, new.module_id, new.kind);
            END
        """)
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_fts_delete AFTER DELETE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, summary, module_id, kind)
                VALUES ('delete', old.rowid, old.content, old.summary, old.module_id, old.kind);
            END
        """)
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS memories_fts_update AFTER UPDATE ON memories BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content, summary, module_id, kind)
                VALUES ('delete', old.rowid, old.content, old.summary, old.module_id, old.kind);
                INSERT INTO memories_fts(rowid, content, summary, module_id, kind)
                VALUES (new.rowid, new.content, new.summary, new.module_id, new.kind);
            END
        """)

        # Memory relations table
        c.execute("""
            CREATE TABLE IF NOT EXISTS memory_relations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                from_ref_id TEXT NOT NULL,
                relation    TEXT NOT NULL,
                to_ref_id   TEXT NOT NULL,
                project_id  TEXT NOT NULL,
                metadata_json TEXT,
                created_at  TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_memrel_from ON memory_relations(project_id, from_ref_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_memrel_to ON memory_relations(project_id, to_ref_id)")

        # Memory events table (audit trail for memory lifecycle)
        c.execute("""
            CREATE TABLE IF NOT EXISTS memory_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ref_id      TEXT NOT NULL,
                project_id  TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                actor_id    TEXT NOT NULL DEFAULT '',
                detail      TEXT NOT NULL DEFAULT '',
                metadata_json TEXT,
                created_at  TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_memevt_ref ON memory_events(project_id, ref_id)")

    def _migrate_v7_to_v8(c):
        """Phase 3: Add entity_id column for ref_id↔entity mapping."""
        try:
            c.execute("ALTER TABLE memories ADD COLUMN entity_id TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # Column already exists
        c.execute("CREATE INDEX IF NOT EXISTS idx_memories_entity ON memories(project_id, entity_id)")

    def _migrate_v8_to_v9(c):
        """Add observer_mode flag to project_version for observer takeover support."""
        try:
            c.execute("ALTER TABLE project_version ADD COLUMN observer_mode INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Column already exists

    def _migrate_v9_to_v10(c):
        """Add session_context table for coordinator session-level logging."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS session_context (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                task_id TEXT,
                entry_type TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata_json TEXT DEFAULT '{}',
                created_at TEXT NOT NULL,
                created_by TEXT DEFAULT ''
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS idx_session_context_project
            ON session_context(project_id, created_at)
        """)

    def _migrate_v10_to_v11(c):
        """Add trace_id and chain_id columns to tasks table for end-to-end chain tracing."""
        try:
            c.execute("ALTER TABLE tasks ADD COLUMN trace_id TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            c.execute("ALTER TABLE tasks ADD COLUMN chain_id TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
        c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_trace ON tasks(project_id, trace_id)")

    def _migrate_v11_to_v12(c):
        """Add gate_events table for queryable gate audit trail."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS gate_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id    TEXT NOT NULL,
                task_id       TEXT NOT NULL,
                gate_name     TEXT NOT NULL,
                passed        INTEGER NOT NULL,
                reason        TEXT,
                trace_id      TEXT,
                created_at    TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_gate_events_project_task ON gate_events(project_id, task_id)")

    def _migrate_v12_to_v13(c):
        """Add subtask_groups table, subtask columns to tasks, max_subtasks to project_version."""
        # subtask_groups table
        c.execute("""
            CREATE TABLE IF NOT EXISTS subtask_groups (
                group_id       TEXT PRIMARY KEY,
                project_id     TEXT NOT NULL,
                pm_task_id     TEXT NOT NULL,
                total_count    INTEGER NOT NULL,
                completed_count INTEGER NOT NULL DEFAULT 0,
                status         TEXT NOT NULL DEFAULT 'active',
                created_at     TEXT NOT NULL,
                completed_at   TEXT,
                trace_id       TEXT,
                chain_id       TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_subtask_groups_project ON subtask_groups(project_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_subtask_groups_pm ON subtask_groups(pm_task_id)")

        # Add subtask columns to tasks table
        for col, typedef in [
            ("subtask_group_id", "TEXT"),
            ("subtask_local_id", "TEXT"),
            ("subtask_depends_on", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Add max_subtasks to project_version
        try:
            c.execute("ALTER TABLE project_version ADD COLUMN max_subtasks INTEGER NOT NULL DEFAULT 5")
        except sqlite3.OperationalError:
            pass  # Column already exists

    def _migrate_v13_to_v14(c):
        """Add pending_nodes table for inferred doc associations (P4)."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS pending_nodes (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id    TEXT NOT NULL,
                node_id       TEXT NOT NULL,
                doc_path      TEXT NOT NULL,
                confidence    REAL NOT NULL DEFAULT 0.0,
                reason        TEXT,
                status        TEXT NOT NULL DEFAULT 'pending',
                created_at    TEXT NOT NULL,
                reviewed_at   TEXT,
                reviewed_by   TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_pending_nodes_project ON pending_nodes(project_id, status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pending_nodes_node ON pending_nodes(project_id, node_id)")

    def _migrate_v14_to_v15(c):
        """Add backlog_bugs table for DB-first backlog storage (OPT-DB-BACKLOG)."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS backlog_bugs (
                bug_id              TEXT PRIMARY KEY,
                title               TEXT NOT NULL DEFAULT '',
                status              TEXT NOT NULL DEFAULT 'OPEN',
                priority            TEXT NOT NULL DEFAULT 'P3',
                target_files        TEXT NOT NULL DEFAULT '[]',
                test_files          TEXT NOT NULL DEFAULT '[]',
                acceptance_criteria TEXT NOT NULL DEFAULT '[]',
                chain_task_id       TEXT NOT NULL DEFAULT '',
                "commit"            TEXT NOT NULL DEFAULT '',
                discovered_at       TEXT NOT NULL DEFAULT '',
                fixed_at            TEXT NOT NULL DEFAULT '',
                details_md          TEXT NOT NULL DEFAULT '',
                chain_trigger_json  TEXT NOT NULL DEFAULT '{}',
                required_docs       TEXT NOT NULL DEFAULT '[]',
                provenance_paths    TEXT NOT NULL DEFAULT '[]',
                chain_stage         TEXT NOT NULL DEFAULT '',
                last_failure_reason TEXT NOT NULL DEFAULT '',
                stage_updated_at    TEXT NOT NULL DEFAULT '',
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_backlog_bugs_status ON backlog_bugs(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_backlog_bugs_priority ON backlog_bugs(priority)")

    def _migrate_v15_to_v16(c):
        """R1: Add resolution_commit and resolution_summary columns to memories table."""
        for col, typedef in [
            ("resolution_commit", "TEXT DEFAULT ''"),
            ("resolution_summary", "TEXT DEFAULT ''"),
        ]:
            try:
                c.execute(f"ALTER TABLE memories ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists

    def _migrate_v16_to_v17(c):
        """Add required_docs column to backlog_bugs table for structured doc references."""
        try:
            c.execute("ALTER TABLE backlog_bugs ADD COLUMN required_docs TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass  # Column already exists

    def _migrate_v17_to_v18(c):
        """Add provenance_paths column to backlog_bugs for tracking source doc paths."""
        try:
            c.execute("ALTER TABLE backlog_bugs ADD COLUMN provenance_paths TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass  # Column already exists

    def _migrate_v18_to_v19(c):
        """Add version_baselines table for Phase I baseline storage."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS version_baselines (
                project_id        TEXT NOT NULL,
                baseline_id       INTEGER NOT NULL,
                chain_version     TEXT NOT NULL,
                graph_sha         TEXT NOT NULL DEFAULT '',
                code_doc_map_sha  TEXT NOT NULL DEFAULT '',
                node_state_snap   TEXT NOT NULL DEFAULT '{}',
                chain_event_max   INTEGER NOT NULL DEFAULT 0,
                trigger           TEXT NOT NULL DEFAULT '',
                triggered_by      TEXT NOT NULL DEFAULT '',
                reconstructed     INTEGER NOT NULL DEFAULT 0,
                created_at        TEXT NOT NULL,
                notes             TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (project_id, baseline_id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_baselines_chain_version ON version_baselines(project_id, chain_version)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_baselines_created_at ON version_baselines(project_id, created_at)")

    def _migrate_v19_to_v20(c):
        """Add phase_h_processed_symbols table for Phase H content delta detection."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS phase_h_processed_symbols (
                fingerprint       TEXT PRIMARY KEY,
                project_id        TEXT NOT NULL,
                commit_sha        TEXT NOT NULL,
                symbol_kind       TEXT NOT NULL,
                symbol_qname      TEXT NOT NULL,
                expected_doc      TEXT NOT NULL,
                spawned_task_id   TEXT NOT NULL DEFAULT '',
                spawn_status      TEXT NOT NULL DEFAULT 'pending',
                last_chain_event  TEXT NOT NULL DEFAULT '',
                updated_at        TEXT NOT NULL,
                processed_at      TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_phase_h_processed_status ON phase_h_processed_symbols(project_id, spawn_status)")

    def _migrate_v20_to_v21(c):
        """Add phase_k_processed_contracts table for Phase K autospawn dedup."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS phase_k_processed_contracts (
                fingerprint       TEXT PRIMARY KEY,
                contract_kind     TEXT NOT NULL,
                contract_id       TEXT NOT NULL,
                discrepancy_type  TEXT NOT NULL,
                target_doc        TEXT NOT NULL DEFAULT '',
                target_test       TEXT NOT NULL DEFAULT '',
                spawned_task_id   TEXT NOT NULL DEFAULT '',
                spawn_status      TEXT NOT NULL DEFAULT 'pending',
                last_chain_event  TEXT NOT NULL DEFAULT '',
                updated_at        TEXT NOT NULL,
                processed_at      TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_phase_k_processed_status ON phase_k_processed_contracts(spawn_status)")

    def _migrate_v21_to_v22(c):
        """Add slice-baseline columns to version_baselines and create baseline_mutations table."""
        # R1: Extend version_baselines with 7 new nullable columns (idempotent)
        for col, typedef in [
            ("scope_id", "TEXT"),
            ("parent_baseline_id", "INTEGER"),
            ("scope_kind", "TEXT"),
            ("scope_value", "TEXT"),
            ("merged_into", "INTEGER"),
            ("merge_status", "TEXT"),
            ("merge_evidence_json", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE version_baselines ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists

        # R2: Create baseline_mutations table
        c.execute("""
            CREATE TABLE IF NOT EXISTS baseline_mutations (
                project_id      TEXT NOT NULL,
                baseline_id     INTEGER NOT NULL,
                mutation_id     TEXT NOT NULL,
                mutation_type   TEXT NOT NULL DEFAULT '',
                affected_file   TEXT NOT NULL DEFAULT '',
                affected_node   TEXT NOT NULL DEFAULT '',
                before_sha256   TEXT NOT NULL DEFAULT '',
                after_sha256    TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (project_id, baseline_id, mutation_id),
                FOREIGN KEY (project_id, baseline_id) REFERENCES version_baselines(project_id, baseline_id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_bm_project ON baseline_mutations(project_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_bm_baseline ON baseline_mutations(project_id, baseline_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_bm_file ON baseline_mutations(affected_file)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_bm_node ON baseline_mutations(affected_node)")

    def _migrate_v22_to_v23(c):
        """Add chain_stage, last_failure_reason, stage_updated_at columns to backlog_bugs."""
        for col, typedef in [
            ("chain_stage", "TEXT NOT NULL DEFAULT ''"),
            ("last_failure_reason", "TEXT NOT NULL DEFAULT ''"),
            ("stage_updated_at", "TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                c.execute(f"ALTER TABLE backlog_bugs ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists

    def _migrate_v23_to_v24(c):
        """Add mutations_sha256 column to version_baselines for per-baseline mutation fingerprints."""
        try:
            c.execute("ALTER TABLE version_baselines ADD COLUMN mutations_sha256 TEXT NOT NULL DEFAULT '{}'")
        except sqlite3.OperationalError:
            pass  # Column already exists

    def _migrate_v24_to_v25(c):
        """Add migrations table for Phase Z v2 migration state machine."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS migrations (
                project_id        TEXT PRIMARY KEY,
                started_at        TEXT,
                deadline_at       TEXT,
                owner             TEXT,
                state             TEXT,
                current_extension INTEGER DEFAULT 0,
                abort_reason      TEXT
            )
        """)

    def _migrate_v25_to_v26(c):
        """Add reconcile_sessions table + partial UNIQUE INDEX (CR0a).

        Mirrors SCHEMA_SQL: one-active-per-project invariant enforced via the
        ``idx_reconcile_sessions_one_active`` partial unique index over
        (project_id) WHERE status IN ('active','finalizing').
        """
        c.execute("""
            CREATE TABLE IF NOT EXISTS reconcile_sessions (
                project_id              TEXT NOT NULL,
                session_id              TEXT NOT NULL,
                run_id                  TEXT,
                status                  TEXT NOT NULL DEFAULT 'active'
                                          CHECK (status IN ('active','finalizing','finalized','rolled_back')),
                started_at              TEXT NOT NULL,
                finalized_at            TEXT,
                cluster_count_total     INTEGER NOT NULL DEFAULT 0,
                cluster_count_resolved  INTEGER NOT NULL DEFAULT 0,
                cluster_count_failed    INTEGER NOT NULL DEFAULT 0,
                bypass_gates_json       TEXT NOT NULL DEFAULT '[]',
                started_by              TEXT,
                snapshot_path           TEXT,
                snapshot_head_sha       TEXT,
                PRIMARY KEY (project_id, session_id)
            )
        """)
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_reconcile_sessions_one_active "
            "ON reconcile_sessions (project_id) "
            "WHERE status IN ('active','finalizing')"
        )

    def _migrate_v26_to_v27(c):
        """Backlog-owned chain/MF runtime state mirror."""
        for col, typedef in [
            ("runtime_state", "TEXT NOT NULL DEFAULT ''"),
            ("current_task_id", "TEXT NOT NULL DEFAULT ''"),
            ("root_task_id", "TEXT NOT NULL DEFAULT ''"),
            ("worktree_path", "TEXT NOT NULL DEFAULT ''"),
            ("worktree_branch", "TEXT NOT NULL DEFAULT ''"),
            ("bypass_policy_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("runtime_updated_at", "TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                c.execute(f"ALTER TABLE backlog_bugs ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists

    def _migrate_v27_to_v28(c):
        """MF profile and chain takeover audit fields."""
        for col, typedef in [
            ("mf_type", "TEXT NOT NULL DEFAULT ''"),
            ("takeover_json", "TEXT NOT NULL DEFAULT '{}'"),
        ]:
            try:
                c.execute(f"ALTER TABLE backlog_bugs ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists

    def _migrate_v28_to_v29(c):
        """Add reconcile batch memory for PM semantic merge context."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS reconcile_batch_memory (
                project_id       TEXT NOT NULL,
                batch_id         TEXT NOT NULL,
                session_id       TEXT NOT NULL DEFAULT '',
                status           TEXT NOT NULL DEFAULT 'active',
                memory_json      TEXT NOT NULL DEFAULT '{}',
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL,
                created_by       TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (project_id, batch_id)
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_reconcile_batch_memory_session "
            "ON reconcile_batch_memory (project_id, session_id)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_reconcile_batch_memory_status "
            "ON reconcile_batch_memory (project_id, status)"
        )

    def _migrate_v29_to_v30(c):
        """Add reconcile file inventory coverage ledger."""
        c.execute("""
            CREATE TABLE IF NOT EXISTS reconcile_file_inventory (
                project_id        TEXT NOT NULL,
                run_id            TEXT NOT NULL,
                path              TEXT NOT NULL,
                file_kind         TEXT NOT NULL DEFAULT '',
                language          TEXT NOT NULL DEFAULT '',
                sha256            TEXT NOT NULL DEFAULT '',
                file_hash         TEXT NOT NULL DEFAULT '',
                size_bytes        INTEGER NOT NULL DEFAULT 0,
                last_scanned_commit TEXT NOT NULL DEFAULT '',
                graph_status      TEXT NOT NULL DEFAULT '',
                mapped_node_ids   TEXT NOT NULL DEFAULT '[]',
                attached_node_ids TEXT NOT NULL DEFAULT '[]',
                attachment_role   TEXT NOT NULL DEFAULT '',
                attachment_source TEXT NOT NULL DEFAULT '',
                scan_status       TEXT NOT NULL DEFAULT '',
                cluster_id        TEXT NOT NULL DEFAULT '',
                candidate_node_id TEXT NOT NULL DEFAULT '',
                attached_to       TEXT NOT NULL DEFAULT '',
                reason            TEXT NOT NULL DEFAULT '',
                decision          TEXT NOT NULL DEFAULT 'pending',
                updated_at        TEXT NOT NULL,
                PRIMARY KEY (project_id, run_id, path)
            )
        """)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_reconcile_file_inventory_status "
            "ON reconcile_file_inventory (project_id, run_id, scan_status)"
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_reconcile_file_inventory_kind "
            "ON reconcile_file_inventory (project_id, run_id, file_kind)"
        )

    def _migrate_v30_to_v31(c):
        """Add reconcile commit baseline and retryable finalize failure state."""
        c.execute("DROP INDEX IF EXISTS idx_reconcile_sessions_one_active")
        c.execute("""
            CREATE TABLE IF NOT EXISTS reconcile_sessions_v31 (
                project_id              TEXT NOT NULL,
                session_id              TEXT NOT NULL,
                run_id                  TEXT,
                status                  TEXT NOT NULL DEFAULT 'active'
                                          CHECK (status IN ('active','finalizing','finalize_failed','finalized','rolled_back')),
                started_at              TEXT NOT NULL,
                finalized_at            TEXT,
                cluster_count_total     INTEGER NOT NULL DEFAULT 0,
                cluster_count_resolved  INTEGER NOT NULL DEFAULT 0,
                cluster_count_failed    INTEGER NOT NULL DEFAULT 0,
                bypass_gates_json       TEXT NOT NULL DEFAULT '[]',
                started_by              TEXT,
                snapshot_path           TEXT,
                snapshot_head_sha       TEXT,
                base_commit_sha         TEXT NOT NULL DEFAULT '',
                finalize_error_json     TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (project_id, session_id)
            )
        """)
        columns = {
            row[1] for row in c.execute("PRAGMA table_info(reconcile_sessions)").fetchall()
        }
        base_expr = "base_commit_sha" if "base_commit_sha" in columns else "COALESCE(snapshot_head_sha, '')"
        err_expr = "finalize_error_json" if "finalize_error_json" in columns else "'{}'"
        c.execute(f"""
            INSERT OR REPLACE INTO reconcile_sessions_v31 (
                project_id, session_id, run_id, status, started_at, finalized_at,
                cluster_count_total, cluster_count_resolved, cluster_count_failed,
                bypass_gates_json, started_by, snapshot_path, snapshot_head_sha,
                base_commit_sha, finalize_error_json
            )
            SELECT
                project_id, session_id, run_id, status, started_at, finalized_at,
                cluster_count_total, cluster_count_resolved, cluster_count_failed,
                bypass_gates_json, started_by, snapshot_path, snapshot_head_sha,
                {base_expr}, {err_expr}
            FROM reconcile_sessions
        """)
        c.execute("DROP TABLE reconcile_sessions")
        c.execute("ALTER TABLE reconcile_sessions_v31 RENAME TO reconcile_sessions")
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_reconcile_sessions_one_active "
            "ON reconcile_sessions (project_id) "
            "WHERE status IN ('active','finalizing','finalize_failed')"
        )

    def _migrate_v31_to_v32(c):
        """Add branch-isolated reconcile target provenance."""
        for col, typedef in [
            ("target_branch", "TEXT NOT NULL DEFAULT ''"),
            ("target_head_sha", "TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                c.execute(f"ALTER TABLE reconcile_sessions ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass

    def _migrate_v32_to_v33(c):
        """Add explicit file hash and graph mapping drift fields."""
        for col, typedef in [
            ("file_hash", "TEXT NOT NULL DEFAULT ''"),
            ("size_bytes", "INTEGER NOT NULL DEFAULT 0"),
            ("last_scanned_commit", "TEXT NOT NULL DEFAULT ''"),
            ("graph_status", "TEXT NOT NULL DEFAULT ''"),
            ("mapped_node_ids", "TEXT NOT NULL DEFAULT '[]'"),
        ]:
            try:
                c.execute(f"ALTER TABLE reconcile_file_inventory ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass
        try:
            c.execute(
                "UPDATE reconcile_file_inventory "
                "SET file_hash = CASE WHEN sha256 != '' THEN 'sha256:' || sha256 ELSE '' END "
                "WHERE file_hash = ''"
            )
        except sqlite3.OperationalError:
            pass

    def _migrate_v33_to_v34(c):
        """Add commit-indexed graph snapshot state tables."""
        from .graph_snapshot_store import GRAPH_SNAPSHOT_SCHEMA_SQL

        c.executescript(GRAPH_SNAPSHOT_SCHEMA_SQL)

    def _migrate_v34_to_v35(c):
        """Add explicit graph attachment metadata to file inventory rows."""
        for col, typedef in [
            ("attached_node_ids", "TEXT NOT NULL DEFAULT '[]'"),
            ("attachment_role", "TEXT NOT NULL DEFAULT ''"),
            ("attachment_source", "TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                c.execute(f"ALTER TABLE reconcile_file_inventory ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass

    def _migrate_v35_to_v36(c):
        """Add durable parallel branch runtime context state."""
        from .parallel_branch_runtime import ensure_branch_runtime_schema

        ensure_branch_runtime_schema(c)

    def _migrate_v36_to_v37(c):
        """Add durable parallel branch merge queue item state."""
        from .parallel_branch_runtime import ensure_branch_runtime_schema

        ensure_branch_runtime_schema(c)

    def _migrate_v37_to_v38(c):
        """Add durable parallel branch batch rollback state."""
        from .parallel_branch_runtime import ensure_branch_runtime_schema

        ensure_branch_runtime_schema(c)

    def _migrate_v38_to_v39(c):
        """Add managed ref runtime for existing long-lived branches."""
        from .managed_ref_runtime import ensure_managed_ref_schema

        ensure_managed_ref_schema(c)

    def _migrate_v39_to_v40(c):
        """Add durable structured AI output intake tables."""
        from .ai_output_intake import ensure_schema as ensure_ai_output_intake_schema

        ensure_ai_output_intake_schema(c)

    def _migrate_v40_to_v41(c):
        """Add append-only task timeline evidence table."""
        from .task_timeline import ensure_schema as ensure_task_timeline_schema

        ensure_task_timeline_schema(c)

    def _migrate_v41_to_v42(c):
        """Add MF scenario/gate evidence fields to task timeline."""
        from .task_timeline import ensure_schema as ensure_task_timeline_schema

        ensure_task_timeline_schema(c)

    def _migrate_v42_to_v43(c):
        """Add unified graph asset projection tables for doc/test/config state."""
        from .asset_projection import ensure_schema as ensure_asset_projection_schema

        ensure_asset_projection_schema(c)

    MIGRATIONS = {2: _migrate_v1_to_v2, 3: _migrate_v2_to_v3, 4: _migrate_v3_to_v4, 5: _migrate_v4_to_v5, 6: _migrate_v5_to_v6, 7: _migrate_v6_to_v7, 8: _migrate_v7_to_v8, 9: _migrate_v8_to_v9, 10: _migrate_v9_to_v10, 11: _migrate_v10_to_v11, 12: _migrate_v11_to_v12, 13: _migrate_v12_to_v13, 14: _migrate_v13_to_v14, 15: _migrate_v14_to_v15, 16: _migrate_v15_to_v16, 17: _migrate_v16_to_v17, 18: _migrate_v17_to_v18, 19: _migrate_v18_to_v19, 20: _migrate_v19_to_v20, 21: _migrate_v20_to_v21, 22: _migrate_v21_to_v22, 23: _migrate_v22_to_v23, 24: _migrate_v23_to_v24, 25: _migrate_v24_to_v25, 26: _migrate_v25_to_v26, 27: _migrate_v26_to_v27, 28: _migrate_v27_to_v28, 29: _migrate_v28_to_v29, 30: _migrate_v29_to_v30, 31: _migrate_v30_to_v31, 32: _migrate_v31_to_v32, 33: _migrate_v32_to_v33, 34: _migrate_v33_to_v34, 35: _migrate_v34_to_v35, 36: _migrate_v35_to_v36, 37: _migrate_v36_to_v37, 38: _migrate_v37_to_v38, 39: _migrate_v38_to_v39, 40: _migrate_v39_to_v40, 41: _migrate_v40_to_v41, 42: _migrate_v41_to_v42, 43: _migrate_v42_to_v43}
    for version in range(from_version + 1, to_version + 1):
        if version in MIGRATIONS:
            MIGRATIONS[version](conn)


def independent_connection(project_id: str, busy_timeout: int = 5000) -> sqlite3.Connection:
    """Open a *fresh* SQLite connection that bypasses any shared-connection pool.

    This is the preferred helper for write-heavy, latency-sensitive paths such
    as ``handle_version_update`` and ``handle_version_sync`` where a long-lived
    shared connection may already hold a WAL read-lock that causes the incoming
    write to block indefinitely.

    Key differences from ``get_connection``:
    * ``busy_timeout`` defaults to **5 000 ms** (vs 10 000 ms for the shared
      connection).  The tighter budget prevents a stalled write from blocking
      the HTTP worker thread for too long; callers are expected to wrap the call
      with the :func:`retry_on_busy` helper.
    * ``_ensure_schema`` is **not** called — the database is assumed to be
      fully migrated already.  This makes the helper cheap: no schema introspection,
      no migration logic, just open → configure → return.

    Args:
        project_id: Governance project identifier (used to locate the DB file).
        busy_timeout: SQLite busy_timeout in milliseconds (default 5000).

    Returns:
        An open, fully-configured ``sqlite3.Connection`` with WAL mode,
        foreign-key enforcement, the given busy_timeout, and ``Row`` factory.
    """
    db_path = _project_db_path(project_id)
    conn = sqlite3.connect(str(db_path), timeout=busy_timeout / 1000.0)
    _configure_connection(conn, busy_timeout=busy_timeout)
    return conn


def close_connection(conn: sqlite3.Connection):
    """Close a database connection."""
    if conn:
        conn.close()


class DBContext:
    """Context manager for database connections with automatic commit/rollback."""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.conn = None

    def __enter__(self) -> sqlite3.Connection:
        self.conn = get_connection(self.project_id)
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
            close_connection(self.conn)
        return False  # Don't suppress exceptions
