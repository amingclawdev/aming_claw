"""Commit-bound asset impact event log and pending reminder projection.

The event table is the source for operator decisions.  The reminder table is a
derived projection: unresolved impact events are grouped by asset/node pair,
and explicit resolution events provide the stop condition.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "asset_impact.v1"
EVENT_IMPACT_DETECTED = "impact_detected"
EVENT_RESOLUTION_RECORDED = "resolution_recorded"
STATUS_PENDING = "pending"
STATUS_RECORDED = "recorded"
RESOLUTION_KINDS = {"updated", "keep_unchanged", "waived"}
ACTION_CATALOG = {
    "primary_actions": ["updated", "keep_unchanged", "waived"],
    "resolution_kinds": {
        "updated": {
            "label": "Updated",
            "description": "The impacted asset was updated to match the node change.",
        },
        "keep_unchanged": {
            "label": "Keep unchanged",
            "description": "The impacted asset was reviewed and still matches the node.",
        },
        "waived": {
            "label": "Waived",
            "description": "The reminder is intentionally dismissed with a recorded note.",
        },
    },
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_asset_impact_events (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id                 TEXT NOT NULL,
    event_type                 TEXT NOT NULL,
    asset_kind                 TEXT NOT NULL DEFAULT '',
    asset_path                 TEXT NOT NULL DEFAULT '',
    node_id                    TEXT NOT NULL DEFAULT '',
    node_title                 TEXT NOT NULL DEFAULT '',
    commit_sha                 TEXT NOT NULL DEFAULT '',
    snapshot_id                TEXT NOT NULL DEFAULT '',
    run_id                     TEXT NOT NULL DEFAULT '',
    actor                      TEXT NOT NULL DEFAULT '',
    status                     TEXT NOT NULL DEFAULT '',
    impact_key                 TEXT NOT NULL DEFAULT '',
    covers_event_ids_json      TEXT NOT NULL DEFAULT '[]',
    evidence_json              TEXT NOT NULL DEFAULT '{}',
    created_at                 TEXT NOT NULL,
    UNIQUE(project_id, event_type, asset_kind, asset_path, node_id, commit_sha, snapshot_id, impact_key)
);
CREATE INDEX IF NOT EXISTS idx_graph_asset_impact_events_group
    ON graph_asset_impact_events(project_id, asset_kind, asset_path, node_id, id);
CREATE INDEX IF NOT EXISTS idx_graph_asset_impact_events_snapshot
    ON graph_asset_impact_events(project_id, snapshot_id, event_type);
CREATE INDEX IF NOT EXISTS idx_graph_asset_impact_events_commit
    ON graph_asset_impact_events(project_id, commit_sha, event_type);

CREATE TABLE IF NOT EXISTS graph_asset_impact_reminders (
    project_id                 TEXT NOT NULL,
    reminder_id                TEXT NOT NULL,
    impact_key                 TEXT NOT NULL DEFAULT '',
    asset_kind                 TEXT NOT NULL DEFAULT '',
    asset_path                 TEXT NOT NULL DEFAULT '',
    node_id                    TEXT NOT NULL DEFAULT '',
    node_title                 TEXT NOT NULL DEFAULT '',
    status                     TEXT NOT NULL DEFAULT 'pending',
    first_commit_sha           TEXT NOT NULL DEFAULT '',
    latest_commit_sha          TEXT NOT NULL DEFAULT '',
    first_event_id             INTEGER NOT NULL DEFAULT 0,
    latest_event_id            INTEGER NOT NULL DEFAULT 0,
    impact_count               INTEGER NOT NULL DEFAULT 0,
    open_event_ids_json        TEXT NOT NULL DEFAULT '[]',
    evidence_json              TEXT NOT NULL DEFAULT '{}',
    created_at                 TEXT NOT NULL,
    updated_at                 TEXT NOT NULL,
    PRIMARY KEY(project_id, reminder_id)
);
CREATE INDEX IF NOT EXISTS idx_graph_asset_impact_reminders_status
    ON graph_asset_impact_reminders(project_id, status, asset_kind, node_id);
CREATE INDEX IF NOT EXISTS idx_graph_asset_impact_reminders_asset
    ON graph_asset_impact_reminders(project_id, asset_kind, asset_path);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _text(value: Any) -> str:
    return str(value or "")


def _norm_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip("/")


def _json(value: Any, default: Any) -> str:
    if value is None:
        value = default
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return json.dumps({"unserializable": repr(value)}, ensure_ascii=False, sort_keys=True)


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return default


def _path_list(raw: Any) -> list[str]:
    if raw is None:
        values: list[Any] = []
    elif isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, Iterable):
        values = list(raw)
    else:
        values = [raw]
    out = {_norm_path(item) for item in values if _norm_path(item)}
    return sorted(out)


def _impact_key(project_id: str, asset_kind: str, asset_path: str, node_id: str) -> str:
    payload = "\0".join([project_id, asset_kind, asset_path, node_id])
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"asset-impact:{digest[:24]}"


def _reminder_id(impact_key: str) -> str:
    digest = hashlib.sha256(impact_key.encode("utf-8")).hexdigest()
    return f"air-{digest[:16]}"


def _event_row(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["covers_event_ids"] = [
        int(item)
        for item in _loads(out.pop("covers_event_ids_json", "[]"), [])
        if str(item).isdigit()
    ]
    out["evidence"] = _loads(out.pop("evidence_json", "{}"), {})
    return out


def _reminder_row(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["open_event_ids"] = [
        int(item)
        for item in _loads(out.pop("open_event_ids_json", "[]"), [])
        if str(item).isdigit()
    ]
    out["evidence"] = _loads(out.pop("evidence_json", "{}"), {})
    return out


def _asset_impact_action_catalog(asset_kinds: Iterable[str] = ()) -> dict[str, Any]:
    return {
        **ACTION_CATALOG,
        "filters": {
            "status": [STATUS_PENDING],
            "asset_kind": sorted({str(kind or "") for kind in asset_kinds if str(kind or "")}),
        },
    }


def _asset_impact_reminder_summary(reminders: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    by_asset_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_node_id: dict[str, int] = {}
    open_event_count = 0
    rows = [dict(reminder) for reminder in reminders]
    for reminder in rows:
        asset_kind = str(reminder.get("asset_kind") or "")
        status = str(reminder.get("status") or "")
        node_id = str(reminder.get("node_id") or "")
        if asset_kind:
            by_asset_kind[asset_kind] = by_asset_kind.get(asset_kind, 0) + 1
        if status:
            by_status[status] = by_status.get(status, 0) + 1
        if node_id:
            by_node_id[node_id] = by_node_id.get(node_id, 0) + 1
        open_event_count += len(reminder.get("open_event_ids") or [])
    return {
        "total_count": len(rows),
        "pending_count": by_status.get(STATUS_PENDING, 0),
        "open_event_count": open_event_count,
        "by_asset_kind": dict(sorted(by_asset_kind.items())),
        "by_status": dict(sorted(by_status.items())),
        "by_node_id": dict(sorted(by_node_id.items())),
    }


def _fetch_reminder_by_id(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    reminder_id: str,
) -> dict[str, Any]:
    row = conn.execute(
        """SELECT * FROM graph_asset_impact_reminders
           WHERE project_id = ? AND reminder_id = ?
           LIMIT 1""",
        (_text(project_id), _text(reminder_id)),
    ).fetchone()
    return _reminder_row(row) if row else {}


def _impact_key_for_reminder_id(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    reminder_id: str,
) -> tuple[str, dict[str, Any]]:
    reminder = _fetch_reminder_by_id(conn, project_id=project_id, reminder_id=reminder_id)
    if reminder:
        return str(reminder.get("impact_key") or ""), reminder
    rows = conn.execute(
        """SELECT DISTINCT impact_key FROM graph_asset_impact_events
           WHERE project_id = ? AND impact_key != ''
           ORDER BY impact_key""",
        (_text(project_id),),
    ).fetchall()
    for row in rows:
        impact_key = str(row["impact_key"] or "")
        if _reminder_id(impact_key) == reminder_id:
            return impact_key, {}
    return "", {}


def _synthesize_reminder_from_events(
    *,
    project_id: str,
    reminder_id: str,
    impact_key: str,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    impact_events = [event for event in events if event.get("event_type") == EVENT_IMPACT_DETECTED]
    covered = {
        int(event_id)
        for event in events
        if event.get("event_type") == EVENT_RESOLUTION_RECORDED
        for event_id in (event.get("covers_event_ids") or [])
        if str(event_id).isdigit()
    }
    open_events = [
        event for event in impact_events
        if int(event.get("id") or 0) not in covered
    ]
    source = (open_events or impact_events or events or [{}])[0]
    latest = (open_events or impact_events or events or [{}])[-1]
    open_event_ids = [int(event.get("id") or 0) for event in open_events if int(event.get("id") or 0) > 0]
    return {
        "project_id": _text(project_id),
        "reminder_id": _text(reminder_id),
        "impact_key": _text(impact_key),
        "asset_kind": str(source.get("asset_kind") or ""),
        "asset_path": str(source.get("asset_path") or ""),
        "node_id": str(source.get("node_id") or ""),
        "node_title": str(latest.get("node_title") or source.get("node_title") or ""),
        "status": STATUS_PENDING if open_event_ids else STATUS_RECORDED,
        "first_commit_sha": str((impact_events[0] if impact_events else source).get("commit_sha") or ""),
        "latest_commit_sha": str((impact_events[-1] if impact_events else latest).get("commit_sha") or ""),
        "first_event_id": int((impact_events[0] if impact_events else source).get("id") or 0),
        "latest_event_id": int((impact_events[-1] if impact_events else latest).get("id") or 0),
        "impact_count": len(open_event_ids),
        "total_impact_count": len(impact_events),
        "open_event_ids": open_event_ids,
        "evidence": {
            "schema_version": SCHEMA_VERSION,
            "source": "graph_asset_impact_events",
            "projection_rule": "history_synthesized_by_reminder_id",
            "event_ids": [int(event.get("id") or 0) for event in events if int(event.get("id") or 0) > 0],
            "covered_event_ids": sorted(covered),
        },
        "created_at": str(source.get("created_at") or ""),
        "updated_at": str(latest.get("created_at") or ""),
    }


def _fetch_event_by_unique(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    event_type: str,
    asset_kind: str,
    asset_path: str,
    node_id: str,
    commit_sha: str,
    snapshot_id: str,
    impact_key: str,
) -> dict[str, Any]:
    row = conn.execute(
        """SELECT * FROM graph_asset_impact_events
           WHERE project_id = ? AND event_type = ? AND asset_kind = ?
             AND asset_path = ? AND node_id = ? AND commit_sha = ?
             AND snapshot_id = ? AND impact_key = ?
           ORDER BY id DESC LIMIT 1""",
        (
            project_id,
            event_type,
            asset_kind,
            asset_path,
            node_id,
            commit_sha,
            snapshot_id,
            impact_key,
        ),
    ).fetchone()
    return _event_row(row) if row else {}


def _node_code_or_config_paths(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    snapshot_id: str,
    node_ids: set[str],
) -> dict[str, set[str]]:
    if not node_ids:
        return {}
    placeholders = ",".join("?" for _ in sorted(node_ids))
    rows = conn.execute(
        f"""SELECT node_id, primary_files_json, metadata_json
            FROM graph_nodes_index
            WHERE project_id = ? AND snapshot_id = ? AND node_id IN ({placeholders})""",
        [project_id, snapshot_id, *sorted(node_ids)],
    ).fetchall()
    out: dict[str, set[str]] = {}
    for row in rows:
        metadata = _loads(row["metadata_json"], {})
        paths = set(_path_list(_loads(row["primary_files_json"], [])))
        if isinstance(metadata, Mapping):
            paths.update(_path_list(metadata.get("config_files")))
        out[str(row["node_id"] or "")] = paths
    return out


def _rebuild_pending_projection(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    ensure_schema(conn)
    pid = _text(project_id)
    rows = [
        _event_row(row)
        for row in conn.execute(
            """SELECT * FROM graph_asset_impact_events
               WHERE project_id = ?
               ORDER BY id""",
            (pid,),
        ).fetchall()
    ]
    covered: set[int] = set()
    for event in rows:
        if event["event_type"] == EVENT_RESOLUTION_RECORDED:
            covered.update(event.get("covers_event_ids") or [])

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in rows:
        if event["event_type"] != EVENT_IMPACT_DETECTED:
            continue
        event_id = int(event["id"])
        if event_id in covered:
            continue
        groups[str(event["impact_key"] or "")].append(event)

    now = _utc_iso()
    conn.execute(
        "DELETE FROM graph_asset_impact_reminders WHERE project_id = ?",
        (pid,),
    )
    for impact_key, events in sorted(groups.items()):
        events.sort(key=lambda item: int(item["id"]))
        first = events[0]
        latest = events[-1]
        open_event_ids = [int(item["id"]) for item in events]
        evidence = {
            "schema_version": SCHEMA_VERSION,
            "source": "graph_asset_impact_events",
            "projection_rule": "unresolved_impact_events_grouped_by_asset_node",
            "event_ids": open_event_ids,
            "commits": sorted({str(item.get("commit_sha") or "") for item in events if item.get("commit_sha")}),
            "snapshots": sorted({str(item.get("snapshot_id") or "") for item in events if item.get("snapshot_id")}),
        }
        conn.execute(
            """INSERT INTO graph_asset_impact_reminders
               (project_id, reminder_id, impact_key, asset_kind, asset_path, node_id,
                node_title, status, first_commit_sha, latest_commit_sha,
                first_event_id, latest_event_id, impact_count, open_event_ids_json,
                evidence_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pid,
                _reminder_id(impact_key),
                impact_key,
                str(first.get("asset_kind") or ""),
                str(first.get("asset_path") or ""),
                str(first.get("node_id") or ""),
                str(latest.get("node_title") or first.get("node_title") or ""),
                STATUS_PENDING,
                str(first.get("commit_sha") or ""),
                str(latest.get("commit_sha") or ""),
                int(first.get("id") or 0),
                int(latest.get("id") or 0),
                len(events),
                _json(open_event_ids, []),
                _json(evidence, {}),
                str(first.get("created_at") or now),
                now,
            ),
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": pid,
        "pending_reminder_count": len(groups),
        "open_event_count": sum(len(events) for events in groups.values()),
    }


def record_asset_impact_detected(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    asset_kind: str,
    asset_path: str,
    node_id: str,
    node_title: str = "",
    commit_sha: str,
    snapshot_id: str,
    run_id: str = "",
    actor: str = "",
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one idempotent impact event and refresh the pending projection."""

    pid = _text(project_id)
    kind = _text(asset_kind)
    path = _norm_path(asset_path)
    node = _text(node_id)
    commit = _text(commit_sha)
    snapshot = _text(snapshot_id)
    if not pid or not kind or not path or not node or not commit or not snapshot:
        raise ValueError("project_id, asset_kind, asset_path, node_id, commit_sha, and snapshot_id are required")
    impact_key = _impact_key(pid, kind, path, node)
    created_at = _utc_iso()
    ensure_schema(conn)
    from .db import sqlite_write_lock

    with sqlite_write_lock():
        conn.execute(
            """INSERT OR IGNORE INTO graph_asset_impact_events
               (project_id, event_type, asset_kind, asset_path, node_id,
                node_title, commit_sha, snapshot_id, run_id, actor, status,
                impact_key, covers_event_ids_json, evidence_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pid,
                EVENT_IMPACT_DETECTED,
                kind,
                path,
                node,
                _text(node_title),
                commit,
                snapshot,
                _text(run_id),
                _text(actor),
                STATUS_PENDING,
                impact_key,
                "[]",
                _json(dict(evidence or {}), {}),
                created_at,
            ),
        )
        event = _fetch_event_by_unique(
            conn,
            project_id=pid,
            event_type=EVENT_IMPACT_DETECTED,
            asset_kind=kind,
            asset_path=path,
            node_id=node,
            commit_sha=commit,
            snapshot_id=snapshot,
            impact_key=impact_key,
        )
        projection = _rebuild_pending_projection(conn, pid)
    return {"event": event, "projection": projection}


def record_asset_impact_resolution(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    covers_event_ids: Iterable[int],
    resolution_kind: str,
    actor: str = "",
    commit_sha: str = "",
    snapshot_id: str = "",
    asset_kind: str = "",
    asset_path: str = "",
    node_id: str = "",
    node_title: str = "",
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Record an operator decision that settles one or more impact events."""

    pid = _text(project_id)
    covered = sorted({int(item) for item in covers_event_ids if int(item) > 0})
    kind = _text(resolution_kind)
    if kind not in RESOLUTION_KINDS:
        raise ValueError(f"resolution_kind must be one of {sorted(RESOLUTION_KINDS)}")
    if not pid or not covered:
        raise ValueError("project_id and covers_event_ids are required")
    ensure_schema(conn)
    first = conn.execute(
        """SELECT * FROM graph_asset_impact_events
           WHERE project_id = ? AND id = ? AND event_type = ?
           LIMIT 1""",
        (pid, covered[0], EVENT_IMPACT_DETECTED),
    ).fetchone()
    if not first:
        raise KeyError(f"impact event not found: {pid}/{covered[0]}")
    first_event = _event_row(first)
    asset_kind = _text(asset_kind or first_event.get("asset_kind"))
    asset_path = _norm_path(asset_path or first_event.get("asset_path"))
    node_id = _text(node_id or first_event.get("node_id"))
    node_title = _text(node_title or first_event.get("node_title"))
    commit_sha = _text(commit_sha)
    snapshot_id = _text(snapshot_id)
    impact_key = _impact_key(pid, asset_kind, asset_path, node_id)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "resolution_kind": kind,
        "covers_event_ids": covered,
        **dict(evidence or {}),
    }
    from .db import sqlite_write_lock

    with sqlite_write_lock():
        conn.execute(
            """INSERT OR IGNORE INTO graph_asset_impact_events
               (project_id, event_type, asset_kind, asset_path, node_id,
                node_title, commit_sha, snapshot_id, run_id, actor, status,
                impact_key, covers_event_ids_json, evidence_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pid,
                EVENT_RESOLUTION_RECORDED,
                asset_kind,
                asset_path,
                node_id,
                node_title,
                commit_sha,
                snapshot_id,
                "",
                _text(actor),
                STATUS_RECORDED,
                impact_key,
                _json(covered, []),
                _json(payload, {}),
                _utc_iso(),
            ),
        )
        projection = _rebuild_pending_projection(conn, pid)
    return {"covers_event_ids": covered, "projection": projection}


def record_scope_asset_impacts(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    snapshot_id: str,
    commit_sha: str,
    scope_graph_delta: Mapping[str, Any],
    asset_kind: str = "doc",
    actor: str = "scope-reconcile",
) -> dict[str, Any]:
    """Create impact reminders from accepted asset bindings and node changes."""

    ensure_schema(conn)
    pid = _text(project_id)
    sid = _text(snapshot_id)
    commit = _text(commit_sha)
    kind = _text(asset_kind)
    updated_nodes = set(_path_list(scope_graph_delta.get("updated_nodes")))
    file_delta = scope_graph_delta.get("file_inventory_delta")
    file_delta = file_delta if isinstance(file_delta, Mapping) else {}
    changed_paths = set(_path_list(
        list(file_delta.get("hash_changed_files") or [])
        + list(file_delta.get("status_changed_files") or [])
        + list(file_delta.get("added_files") or [])
        + list(file_delta.get("removed_files") or [])
        + list(file_delta.get("impacted_files") or [])
    ))
    if not pid or not sid or not commit or not kind or not updated_nodes:
        return {
            "schema_version": SCHEMA_VERSION,
            "project_id": pid,
            "snapshot_id": sid,
            "commit_sha": commit,
            "asset_kind": kind,
            "event_count": 0,
            "skipped": "missing_required_scope",
        }

    placeholders = ",".join("?" for _ in sorted(updated_nodes))
    rows = conn.execute(
        f"""SELECT asset_kind, asset_path, node_id, title, source, evidence_json
            FROM graph_asset_bindings
            WHERE project_id = ?
              AND snapshot_id = ?
              AND binding_status = 'accepted'
              AND asset_kind = ?
              AND node_id IN ({placeholders})
            ORDER BY asset_path, node_id""",
        [pid, sid, kind, *sorted(updated_nodes)],
    ).fetchall()
    node_code_paths = _node_code_or_config_paths(
        conn,
        project_id=pid,
        snapshot_id=sid,
        node_ids=updated_nodes,
    )
    emitted: list[dict[str, Any]] = []
    skipped_changed_asset = 0
    skipped_non_code_node_change = 0
    for row in rows:
        asset_path = _norm_path(row["asset_path"])
        node_id = _text(row["node_id"])
        if asset_path in changed_paths:
            skipped_changed_asset += 1
            continue
        code_paths = node_code_paths.get(node_id)
        if code_paths is not None and not changed_paths.intersection(code_paths):
            skipped_non_code_node_change += 1
            continue
        result = record_asset_impact_detected(
            conn,
            project_id=pid,
            asset_kind=kind,
            asset_path=asset_path,
            node_id=node_id,
            node_title=_text(row["title"]),
            commit_sha=commit,
            snapshot_id=sid,
            actor=actor,
            evidence={
                "schema_version": SCHEMA_VERSION,
                "source": "scope_graph_delta",
                "changed_paths": sorted(changed_paths),
                "updated_nodes": sorted(updated_nodes),
                "binding_source": _text(row["source"]),
                "binding_evidence": _loads(row["evidence_json"], {}),
            },
        )
        if result.get("event"):
            emitted.append(result["event"])
    projection = _rebuild_pending_projection(conn, pid)
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": pid,
        "snapshot_id": sid,
        "commit_sha": commit,
        "asset_kind": kind,
        "event_count": len(emitted),
        "event_ids": [int(event["id"]) for event in emitted],
        "pending_reminder_count": projection["pending_reminder_count"],
        "skipped_changed_asset": skipped_changed_asset,
        "skipped_non_code_node_change": skipped_non_code_node_change,
    }


def list_asset_impact_events(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    asset_kind: str = "",
    node_id: str = "",
    asset_path: str = "",
    event_type: str = "",
    limit: int = 500,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    clauses = ["project_id = ?"]
    params: list[Any] = [_text(project_id)]
    for column, value in (
        ("asset_kind", _text(asset_kind)),
        ("node_id", _text(node_id)),
        ("asset_path", _norm_path(asset_path) if asset_path else ""),
        ("event_type", _text(event_type)),
    ):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    params.append(max(1, min(int(limit or 500), 5000)))
    rows = conn.execute(
        f"""SELECT * FROM graph_asset_impact_events
            WHERE {' AND '.join(clauses)}
            ORDER BY id
            LIMIT ?""",
        params,
    ).fetchall()
    return [_event_row(row) for row in rows]


def list_pending_asset_impact_reminders(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    asset_kind: str = "",
    node_id: str = "",
    limit: int = 500,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    clauses = ["project_id = ?", "status = ?"]
    params: list[Any] = [_text(project_id), STATUS_PENDING]
    if asset_kind:
        clauses.append("asset_kind = ?")
        params.append(_text(asset_kind))
    if node_id:
        clauses.append("node_id = ?")
        params.append(_text(node_id))
    params.append(max(1, min(int(limit or 500), 5000)))
    rows = conn.execute(
        f"""SELECT * FROM graph_asset_impact_reminders
            WHERE {' AND '.join(clauses)}
            ORDER BY latest_event_id DESC
            LIMIT ?""",
        params,
    ).fetchall()
    return [_reminder_row(row) for row in rows]


def build_asset_impact_reminder_projection(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    asset_kind: str = "",
    node_id: str = "",
    status: str = STATUS_PENDING,
    limit: int = 500,
) -> dict[str, Any]:
    ensure_schema(conn)
    pid = _text(project_id)
    wanted_status = _text(status or STATUS_PENDING)
    if wanted_status != STATUS_PENDING:
        raise ValueError("status must be pending")
    reminders = list_pending_asset_impact_reminders(
        conn,
        pid,
        asset_kind=asset_kind,
        node_id=node_id,
        limit=limit,
    )
    summary = _asset_impact_reminder_summary(reminders)
    asset_kinds = summary.get("by_asset_kind", {}).keys()
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": pid,
        "status": wanted_status,
        "asset_kind": _text(asset_kind),
        "node_id": _text(node_id),
        "reminders": reminders,
        "count": len(reminders),
        "summary": summary,
        "action_catalog": _asset_impact_action_catalog(asset_kinds),
    }


def get_asset_impact_reminder_events(
    conn: sqlite3.Connection,
    project_id: str,
    reminder_id: str,
    *,
    limit: int = 500,
) -> dict[str, Any]:
    ensure_schema(conn)
    pid = _text(project_id)
    rid = _text(reminder_id)
    impact_key, reminder = _impact_key_for_reminder_id(
        conn,
        project_id=pid,
        reminder_id=rid,
    )
    if not impact_key:
        return {
            "schema_version": SCHEMA_VERSION,
            "project_id": pid,
            "reminder_id": rid,
            "reminder": {},
            "events": [],
            "count": 0,
            "summary": _asset_impact_reminder_summary([]),
            "action_catalog": _asset_impact_action_catalog(),
        }
    max_rows = max(1, min(int(limit or 500), 5000))
    events = [
        _event_row(row)
        for row in conn.execute(
            """SELECT * FROM graph_asset_impact_events
               WHERE project_id = ? AND impact_key = ?
               ORDER BY id
               LIMIT ?""",
            (pid, impact_key, max_rows),
        ).fetchall()
    ]
    if reminder:
        reminder = {
            **reminder,
            "total_impact_count": sum(
                1 for event in events if event.get("event_type") == EVENT_IMPACT_DETECTED
            ),
        }
    else:
        reminder = _synthesize_reminder_from_events(
            project_id=pid,
            reminder_id=rid,
            impact_key=impact_key,
            events=events,
        )
    summary = _asset_impact_reminder_summary([reminder] if reminder else [])
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": pid,
        "reminder_id": rid,
        "reminder": reminder,
        "events": events,
        "count": len(events),
        "summary": summary,
        "action_catalog": _asset_impact_action_catalog([reminder.get("asset_kind", "")] if reminder else []),
    }


def resolve_asset_impact_reminder(
    conn: sqlite3.Connection,
    project_id: str,
    reminder_id: str,
    *,
    resolution_kind: str,
    note: str = "",
    actor: str = "",
) -> dict[str, Any]:
    ensure_schema(conn)
    pid = _text(project_id)
    rid = _text(reminder_id)
    history = get_asset_impact_reminder_events(conn, pid, rid)
    reminder = history.get("reminder") if isinstance(history.get("reminder"), dict) else {}
    if not reminder:
        raise KeyError(f"asset impact reminder not found: {pid}/{rid}")
    open_event_ids = [
        int(event_id)
        for event_id in (reminder.get("open_event_ids") or [])
        if int(event_id) > 0
    ]
    if not open_event_ids:
        raise ValueError("asset impact reminder has no pending events")
    resolution = record_asset_impact_resolution(
        conn,
        project_id=pid,
        covers_event_ids=open_event_ids,
        resolution_kind=resolution_kind,
        actor=_text(actor or "observer"),
        asset_kind=str(reminder.get("asset_kind") or ""),
        asset_path=str(reminder.get("asset_path") or ""),
        node_id=str(reminder.get("node_id") or ""),
        node_title=str(reminder.get("node_title") or ""),
        evidence={
            "source": "asset_impact_reminder_api",
            "reminder_id": rid,
            "note": _text(note),
        },
    )
    updated_history = get_asset_impact_reminder_events(conn, pid, rid)
    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": pid,
        "reminder_id": rid,
        "resolution": {
            "resolution_kind": _text(resolution_kind),
            "actor": _text(actor or "observer"),
            "note": _text(note),
            "covers_event_ids": open_event_ids,
        },
        "projection": resolution.get("projection", {}),
        "reminder": updated_history.get("reminder", {}),
        "events": updated_history.get("events", []),
        "summary": updated_history.get("summary", {}),
        "action_catalog": updated_history.get("action_catalog", _asset_impact_action_catalog()),
    }


__all__ = [
    "ACTION_CATALOG",
    "EVENT_IMPACT_DETECTED",
    "EVENT_RESOLUTION_RECORDED",
    "RESOLUTION_KINDS",
    "SCHEMA_VERSION",
    "STATUS_PENDING",
    "STATUS_RECORDED",
    "build_asset_impact_reminder_projection",
    "ensure_schema",
    "get_asset_impact_reminder_events",
    "list_asset_impact_events",
    "list_pending_asset_impact_reminders",
    "record_asset_impact_detected",
    "record_asset_impact_resolution",
    "record_scope_asset_impacts",
    "resolve_asset_impact_reminder",
]
