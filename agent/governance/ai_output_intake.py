"""Durable intake for structured AI outputs.

This module is intentionally small: it gives MF/observer flows one DB-backed
place to submit structured AI output envelopes without coupling the write path
to semantic apply, feedback review, or chain auto-completion.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .errors import ValidationError


INTAKE_SCHEMA_VERSION = "ai_output_intake.v1"

SUPPORTED_TASK_TYPES = {
    "semantic_node",
    "semantic_edge",
    "graph_structure_ops",
    "graph_enrich_config_ops",
    "feedback_review",
    "global_review",
    "cluster_report",
    "graph_event_refine",
    "mf_sub_result",
    "chain_stage_result",
}

RESERVED_TASK_TYPES = {"chain_stage_result"}
ROUTE_STATUSES = {
    "queued",
    "reserved",
    "review_pending",
    "gate_failed",
    "completed",
    "rejected",
    "failed",
}
OPEN_QUEUE_STATUS = "queued"

SCHEMA_SQL = """
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
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the AI output intake tables and indexes if missing."""
    conn.executescript(SCHEMA_SQL)


def submit_ai_output(
    conn: sqlite3.Connection,
    project_id: str,
    request: dict[str, Any],
    *,
    actor: str = "observer",
    request_id: str = "",
    idempotency_key: str = "",
) -> dict[str, Any]:
    """Validate and store one structured AI output envelope.

    The caller owns transaction commit/rollback. The write itself is serialized
    with the governance process-local SQLite write lock.
    """
    if not isinstance(request, dict):
        raise ValidationError("request body must be an object")
    project_id = _required_str(project_id, "project_id")
    task_type = _required_str(request.get("task_type"), "task_type")
    if task_type not in SUPPORTED_TASK_TYPES:
        raise ValidationError(
            f"unsupported task_type {task_type!r}",
            {"supported_task_types": sorted(SUPPORTED_TASK_TYPES)},
        )
    payload = request.get("payload")
    if not isinstance(payload, dict):
        raise ValidationError("payload must be an object")

    ensure_schema(conn)

    now = _utc_now()
    snapshot_id = _optional_str(request.get("snapshot_id"))
    base_commit = _optional_str(request.get("base_commit"))
    target_type = _optional_str(request.get("target_type"))
    target_id = _optional_str(request.get("target_id"))
    producer = _optional_str(request.get("producer")) or _optional_str(actor) or "observer"
    source_run_id = _optional_str(request.get("source_run_id"))
    provider = _optional_str(request.get("provider"))
    model = _optional_str(request.get("model"))
    prompt_hash = _optional_str(request.get("prompt_hash"))
    explicit_idem_key = _optional_str(idempotency_key) or _optional_str(request.get("idempotency_key"))
    metadata = _object_field(request, "metadata")
    self_precheck = _object_field(request, "self_precheck")
    trace_ids = _trace_ids_field(request)
    priority = _int_field(request, "priority", 0)
    max_attempts = max(1, _int_field(request, "max_attempts", 3))

    payload_json = _json_dumps(payload)
    payload_hash = _payload_hash(payload_json)
    _validate_payload_hash(request.get("payload_hash"), payload_hash)
    dedupe_key = explicit_idem_key or _dedupe_key(
        project_id=project_id,
        snapshot_id=snapshot_id,
        task_type=task_type,
        target_type=target_type,
        target_id=target_id,
        producer=producer,
        source_run_id=source_run_id,
        payload_hash=payload_hash,
    )
    output_id = _output_id(project_id, dedupe_key)
    default_route_status = "reserved" if task_type in RESERVED_TASK_TYPES else "queued"
    route_status = _route_status_field(request, default_route_status)
    if task_type in RESERVED_TASK_TYPES and route_status != "reserved":
        raise ValidationError("reserved task_type must use route_status='reserved'")

    row = {
        "output_id": output_id,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "base_commit": base_commit,
        "task_type": task_type,
        "target_type": target_type,
        "target_id": target_id,
        "producer": producer,
        "source_run_id": source_run_id,
        "provider": provider,
        "model": model,
        "prompt_hash": prompt_hash,
        "payload_hash": payload_hash,
        "dedupe_key": dedupe_key,
        "idempotency_key": explicit_idem_key,
        "status": "submitted",
        "route_status": route_status,
        "payload_json": payload_json,
        "self_precheck_json": _json_dumps(self_precheck),
        "graph_query_trace_ids_json": _json_dumps(trace_ids),
        "metadata_json": _json_dumps(metadata),
        "created_by": _optional_str(actor),
        "created_at": now,
        "updated_at": now,
    }

    from .db import sqlite_write_lock

    with sqlite_write_lock():
        existing = _fetch_output_by_dedupe(conn, project_id, dedupe_key)
        if existing:
            payload = output_row_to_dict(existing)
            payload["ok"] = True
            payload["idempotent"] = True
            return payload

        try:
            conn.execute(
                """
                INSERT INTO ai_outputs (
                    output_id, project_id, snapshot_id, base_commit, task_type,
                    target_type, target_id, producer, source_run_id, provider,
                    model, prompt_hash, payload_hash, dedupe_key, idempotency_key,
                    status, route_status, payload_json, self_precheck_json,
                    graph_query_trace_ids_json, metadata_json, created_by,
                    created_at, updated_at
                ) VALUES (
                    :output_id, :project_id, :snapshot_id, :base_commit, :task_type,
                    :target_type, :target_id, :producer, :source_run_id, :provider,
                    :model, :prompt_hash, :payload_hash, :dedupe_key, :idempotency_key,
                    :status, :route_status, :payload_json, :self_precheck_json,
                    :graph_query_trace_ids_json, :metadata_json, :created_by,
                    :created_at, :updated_at
                )
                """,
                row,
            )
        except sqlite3.IntegrityError:
            existing = _fetch_output_by_dedupe(conn, project_id, dedupe_key)
            if not existing:
                raise
            payload = output_row_to_dict(existing)
            payload["ok"] = True
            payload["idempotent"] = True
            return payload

        conn.execute(
            """
            INSERT INTO ai_output_events (
                output_id, project_id, event_type, actor, request_id,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                output_id,
                project_id,
                "submitted",
                _optional_str(actor),
                _optional_str(request_id),
                _json_dumps({"route_status": route_status, "schema": INTAKE_SCHEMA_VERSION}),
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO ai_output_queue (
                output_id, project_id, task_type, target_type, target_id,
                status, priority, max_attempts, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                output_id,
                project_id,
                task_type,
                target_type,
                target_id,
                route_status,
                priority,
                max_attempts,
                now,
                now,
            ),
        )

    payload = output_row_to_dict(row)
    payload["ok"] = True
    payload["idempotent"] = False
    return payload


def get_ai_output(conn: sqlite3.Connection, project_id: str, output_id: str) -> dict[str, Any] | None:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM ai_outputs WHERE project_id = ? AND output_id = ?",
        (project_id, output_id),
    ).fetchone()
    return output_row_to_dict(row) if row else None


def mark_ai_output_route_status(
    conn: sqlite3.Connection,
    project_id: str,
    output_id: str,
    route_status: str,
    *,
    actor: str = "observer",
    request_id: str = "",
    last_error: str = "",
) -> dict[str, Any]:
    """Update downstream route lifecycle while preserving the submitted output."""
    ensure_schema(conn)
    project_id = _required_str(project_id, "project_id")
    output_id = _required_str(output_id, "output_id")
    route_status = _route_status_value(route_status)
    now = _utc_now()

    from .db import sqlite_write_lock

    with sqlite_write_lock():
        row = conn.execute(
            "SELECT * FROM ai_outputs WHERE project_id = ? AND output_id = ?",
            (project_id, output_id),
        ).fetchone()
        if not row:
            return {
                "ok": False,
                "error": "ai_output_not_found",
                "output_id": output_id,
                "route_status": route_status,
            }
        conn.execute(
            """
            UPDATE ai_outputs
            SET route_status = ?, updated_at = ?
            WHERE project_id = ? AND output_id = ?
            """,
            (route_status, now, project_id, output_id),
        )
        conn.execute(
            """
            UPDATE ai_output_queue
            SET status = ?, last_error = ?, updated_at = ?
            WHERE project_id = ? AND output_id = ?
            """,
            (route_status, _optional_str(last_error), now, project_id, output_id),
        )
        conn.execute(
            """
            INSERT INTO ai_output_events (
                output_id, project_id, event_type, actor, request_id,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                output_id,
                project_id,
                "route_status_updated",
                _optional_str(actor),
                _optional_str(request_id),
                _json_dumps({"route_status": route_status, "schema": INTAKE_SCHEMA_VERSION}),
                now,
            ),
        )
    return {
        "ok": True,
        "output_id": output_id,
        "route_status": route_status,
        "output": get_ai_output(conn, project_id, output_id),
    }


def list_ai_outputs(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    task_type: str = "",
    status: str = "",
    producer: str = "",
    target_id: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    where = ["project_id = ?"]
    params: list[Any] = [project_id]
    for column, value in [
        ("task_type", task_type),
        ("status", status),
        ("producer", producer),
        ("target_id", target_id),
    ]:
        clean = _optional_str(value)
        if clean:
            where.append(f"{column} = ?")
            params.append(clean)
    params.extend([_limit(limit), max(0, int(offset or 0))])
    rows = conn.execute(
        f"""
        SELECT * FROM ai_outputs
        WHERE {' AND '.join(where)}
        ORDER BY created_at DESC, output_id DESC
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()
    return [output_row_to_dict(row) for row in rows]


def list_ai_output_queue(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    task_type: str = "",
    status: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    where = ["project_id = ?"]
    params: list[Any] = [project_id]
    for column, value in [("task_type", task_type)]:
        clean = _optional_str(value)
        if clean:
            where.append(f"{column} = ?")
            params.append(clean)
    status_clean = _optional_str(status)
    if status_clean and status_clean.lower() not in {"*", "all"}:
        where.append("status = ?")
        params.append(status_clean)
    elif not status_clean:
        where.append("status = ?")
        params.append(OPEN_QUEUE_STATUS)
    params.extend([_limit(limit), max(0, int(offset or 0))])
    rows = conn.execute(
        f"""
        SELECT * FROM ai_output_queue
        WHERE {' AND '.join(where)}
        ORDER BY priority DESC, created_at ASC, output_id ASC
        LIMIT ? OFFSET ?
        """,
        params,
    ).fetchall()
    return [queue_row_to_dict(row) for row in rows]


def output_row_to_dict(row: Any) -> dict[str, Any]:
    data = _row_dict(row)
    return {
        **data,
        "payload": _json_load(data.pop("payload_json", "{}"), {}),
        "self_precheck": _json_load(data.pop("self_precheck_json", "{}"), {}),
        "graph_query_trace_ids": _json_load(data.pop("graph_query_trace_ids_json", "[]"), []),
        "metadata": _json_load(data.pop("metadata_json", "{}"), {}),
    }


def queue_row_to_dict(row: Any) -> dict[str, Any]:
    return _row_dict(row)


def _fetch_output_by_dedupe(conn: sqlite3.Connection, project_id: str, dedupe_key: str) -> Any:
    return conn.execute(
        "SELECT * FROM ai_outputs WHERE project_id = ? AND dedupe_key = ?",
        (project_id, dedupe_key),
    ).fetchone()


def _row_dict(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def _required_str(value: Any, key: str) -> str:
    clean = _optional_str(value)
    if not clean:
        raise ValidationError(f"{key} is required")
    return clean


def _optional_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _object_field(request: dict[str, Any], key: str) -> dict[str, Any]:
    value = request.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError(f"{key} must be an object")
    return value


def _trace_ids_field(request: dict[str, Any]) -> list[str]:
    value = request.get("graph_query_trace_ids")
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValidationError("graph_query_trace_ids must be a list")
    return [_optional_str(item) for item in value if _optional_str(item)]


def _int_field(request: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(request.get(key, default))
    except (TypeError, ValueError):
        raise ValidationError(f"{key} must be an integer")


def _limit(value: int) -> int:
    try:
        return max(1, min(200, int(value)))
    except (TypeError, ValueError):
        return 50


def _route_status_field(request: dict[str, Any], default: str) -> str:
    return _route_status_value(request.get("route_status") or default)


def _route_status_value(value: Any) -> str:
    clean = _optional_str(value)
    if clean not in ROUTE_STATUSES:
        raise ValidationError(
            f"unsupported route_status {clean!r}",
            {"supported_route_statuses": sorted(ROUTE_STATUSES)},
        )
    return clean


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_load(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _payload_hash(payload_json: str) -> str:
    return "sha256:" + hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _validate_payload_hash(explicit: Any, computed: str) -> None:
    clean = _optional_str(explicit)
    if not clean:
        return
    normalized = clean if clean.startswith("sha256:") else f"sha256:{clean}"
    if normalized != computed:
        raise ValidationError(
            "payload_hash does not match payload",
            {"expected": computed, "actual": normalized},
        )


def _dedupe_key(**parts: str) -> str:
    raw = _json_dumps(parts)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _output_id(project_id: str, dedupe_key: str) -> str:
    raw = f"{project_id}:{dedupe_key}"
    return "aio-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "INTAKE_SCHEMA_VERSION",
    "SUPPORTED_TASK_TYPES",
    "RESERVED_TASK_TYPES",
    "ensure_schema",
    "submit_ai_output",
    "get_ai_output",
    "list_ai_outputs",
    "list_ai_output_queue",
    "mark_ai_output_route_status",
]
