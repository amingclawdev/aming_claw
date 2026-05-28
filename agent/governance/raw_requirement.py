"""Raw requirement inbox storage.

First slice of Project Inbox: persist exact raw requirement text captured from
an operator before any AI decomposition or backlog promotion happens.  Capture
Mode records the raw text; Confirm Mode foundations let an operator move a row
toward a backlog draft through explicit action only.

Design notes
------------
* Status lifecycle is intentionally minimal: ``raw_inbox`` → ``needs_confirmation``
  → ``promoted`` (terminal once a backlog row is linked) or ``dismissed``.
* Promotion DOES NOT auto-create a backlog row.  It simply records the operator
  decision and the chosen backlog id; the actual backlog upsert remains the
  responsibility of the existing backlog tools.  This keeps Capture Mode strict
  about "no implementation backlog rows created automatically".
* Rows are immutable for the ``raw_text`` field — operators capture as-is and
  amend via ``note`` instead, preserving the original requirement statement.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable


STATUS_RAW_INBOX = "raw_inbox"
STATUS_NEEDS_CONFIRMATION = "needs_confirmation"
STATUS_PROMOTED = "promoted"
STATUS_DISMISSED = "dismissed"

VALID_STATUSES = {
    STATUS_RAW_INBOX,
    STATUS_NEEDS_CONFIRMATION,
    STATUS_PROMOTED,
    STATUS_DISMISSED,
}


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS raw_requirements (
    raw_id            TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL,
    raw_text          TEXT NOT NULL DEFAULT '',
    source            TEXT NOT NULL DEFAULT '',
    session_id        TEXT NOT NULL DEFAULT '',
    captured_by       TEXT NOT NULL DEFAULT '',
    status            TEXT NOT NULL DEFAULT 'raw_inbox',
    note              TEXT NOT NULL DEFAULT '',
    promoted_bug_id   TEXT NOT NULL DEFAULT '',
    metadata_json     TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_req_project_status
    ON raw_requirements(project_id, status);
CREATE INDEX IF NOT EXISTS idx_raw_req_project_created
    ON raw_requirements(project_id, created_at);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def _row_to_dict(row: sqlite3.Row | tuple | None) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        data = {k: row[k] for k in row.keys()}
    else:
        # Fallback when row_factory is not Row; caller should have set it.
        data = dict(row)  # type: ignore[arg-type]
    metadata_raw = data.get("metadata_json") or "{}"
    try:
        parsed = json.loads(metadata_raw)
        data["metadata"] = parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        data["metadata"] = {}
    return data


def create_raw_requirement(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    raw_text: str,
    source: str = "",
    session_id: str = "",
    captured_by: str = "",
    metadata: dict[str, Any] | None = None,
    raw_id: str | None = None,
) -> dict[str, Any]:
    """Persist a raw requirement exactly as captured.

    Raises ``ValueError`` for empty text/project_id; callers map that to 400.
    """
    pid = (project_id or "").strip()
    text = (raw_text or "").strip()
    if not pid:
        raise ValueError("project_id is required")
    if not text:
        raise ValueError("raw_text is required")

    rid = raw_id or f"raw-{uuid.uuid4().hex[:12]}"
    now = _utc_now()
    meta_json = json.dumps(metadata or {}, ensure_ascii=False)

    conn.execute(
        """
        INSERT INTO raw_requirements (
            raw_id, project_id, raw_text, source, session_id, captured_by,
            status, note, promoted_bug_id, metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rid,
            pid,
            text,
            (source or "").strip(),
            (session_id or "").strip(),
            (captured_by or "").strip(),
            STATUS_RAW_INBOX,
            "",
            "",
            meta_json,
            now,
            now,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM raw_requirements WHERE raw_id = ?", (rid,)
    ).fetchone()
    return _row_to_dict(row)  # type: ignore[return-value]


def list_raw_requirements(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    status: str | Iterable[str] | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    pid = (project_id or "").strip()
    if not pid:
        return []
    params: list[Any] = [pid]
    where = ["project_id = ?"]
    if status:
        if isinstance(status, str):
            statuses = [status]
        else:
            statuses = [s for s in status if s]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where.append(f"status IN ({placeholders})")
            params.extend(statuses)
    sql = (
        "SELECT * FROM raw_requirements WHERE "
        + " AND ".join(where)
        + " ORDER BY created_at DESC LIMIT ?"
    )
    params.append(max(1, min(int(limit or 200), 1000)))
    rows = conn.execute(sql, params).fetchall()
    return [r for r in (_row_to_dict(row) for row in rows) if r is not None]


def get_raw_requirement(
    conn: sqlite3.Connection, *, project_id: str, raw_id: str
) -> dict[str, Any] | None:
    pid = (project_id or "").strip()
    rid = (raw_id or "").strip()
    if not pid or not rid:
        return None
    row = conn.execute(
        "SELECT * FROM raw_requirements WHERE project_id = ? AND raw_id = ?",
        (pid, rid),
    ).fetchone()
    return _row_to_dict(row)


def update_status(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    raw_id: str,
    new_status: str,
    note: str | None = None,
    promoted_bug_id: str | None = None,
) -> dict[str, Any]:
    """Apply a status transition.

    Promotion to ``promoted`` requires ``promoted_bug_id`` so the audit trail
    points back at a backlog row the operator (or a follow-up tool) created.
    """
    pid = (project_id or "").strip()
    rid = (raw_id or "").strip()
    status = (new_status or "").strip()
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status: {status!r}")
    existing = get_raw_requirement(conn, project_id=pid, raw_id=rid)
    if not existing:
        raise LookupError(f"raw requirement not found: {rid}")
    if status == STATUS_PROMOTED and not (promoted_bug_id or "").strip():
        raise ValueError("promoted_bug_id is required when promoting")

    now = _utc_now()
    next_note = existing["note"] if note is None else (note or "").strip()
    next_bug = (
        (promoted_bug_id or "").strip()
        if promoted_bug_id is not None
        else existing["promoted_bug_id"]
    )
    conn.execute(
        """UPDATE raw_requirements
              SET status = ?, note = ?, promoted_bug_id = ?, updated_at = ?
            WHERE project_id = ? AND raw_id = ?""",
        (status, next_note, next_bug, now, pid, rid),
    )
    conn.commit()
    return get_raw_requirement(conn, project_id=pid, raw_id=rid)  # type: ignore[return-value]


def lane_counts(
    conn: sqlite3.Connection, *, project_id: str
) -> dict[str, int]:
    """Return per-status counts for the dashboard lanes."""
    pid = (project_id or "").strip()
    if not pid:
        return {s: 0 for s in VALID_STATUSES}
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM raw_requirements "
        "WHERE project_id = ? GROUP BY status",
        (pid,),
    ).fetchall()
    counts = {s: 0 for s in VALID_STATUSES}
    for row in rows:
        status = row["status"] if isinstance(row, sqlite3.Row) else row[0]
        n = row["n"] if isinstance(row, sqlite3.Row) else row[1]
        if status in counts:
            counts[status] = int(n)
    return counts
