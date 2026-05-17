"""Commit-indexed graph snapshot state store.

This module is intentionally state-only: it stores graph snapshots, indexes,
drift rows, and pending scope-reconcile rows. It does not modify source,
documentation, or test files.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


GRAPH_SNAPSHOT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_snapshots (
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  parent_snapshot_id TEXT NOT NULL DEFAULT '',
  snapshot_kind TEXT NOT NULL,
  ref_name TEXT NOT NULL DEFAULT '',
  branch_ref TEXT NOT NULL DEFAULT '',
  graph_sha256 TEXT NOT NULL DEFAULT '',
  inventory_sha256 TEXT NOT NULL DEFAULT '',
  drift_sha256 TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(project_id, snapshot_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_snapshots_commit
  ON graph_snapshots(project_id, commit_sha);

CREATE INDEX IF NOT EXISTS idx_graph_snapshots_status
  ON graph_snapshots(project_id, status, commit_sha);

CREATE TABLE IF NOT EXISTS graph_snapshot_refs (
  project_id TEXT NOT NULL,
  ref_name TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(project_id, ref_name)
);

CREATE TABLE IF NOT EXISTS graph_ref_events (
  project_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  ref_name TEXT NOT NULL,
  branch_ref TEXT NOT NULL DEFAULT '',
  batch_id TEXT NOT NULL DEFAULT '',
  merge_queue_id TEXT NOT NULL DEFAULT '',
  operation_type TEXT NOT NULL,
  old_snapshot_id TEXT NOT NULL DEFAULT '',
  new_snapshot_id TEXT NOT NULL DEFAULT '',
  old_commit TEXT NOT NULL DEFAULT '',
  new_commit TEXT NOT NULL DEFAULT '',
  old_projection_id TEXT NOT NULL DEFAULT '',
  new_projection_id TEXT NOT NULL DEFAULT '',
  merge_epoch TEXT NOT NULL DEFAULT '',
  rollback_epoch TEXT NOT NULL DEFAULT '',
  replay_epoch TEXT NOT NULL DEFAULT '',
  source_event_id TEXT NOT NULL DEFAULT '',
  actor TEXT NOT NULL DEFAULT '',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  PRIMARY KEY(project_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_ref_events_ref
  ON graph_ref_events(project_id, ref_name, created_at);

CREATE INDEX IF NOT EXISTS idx_graph_ref_events_operation
  ON graph_ref_events(project_id, operation_type, created_at);

CREATE TABLE IF NOT EXISTS graph_nodes_index (
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  layer TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT '',
  primary_files_json TEXT NOT NULL DEFAULT '[]',
  secondary_files_json TEXT NOT NULL DEFAULT '[]',
  test_files_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(project_id, snapshot_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_nodes_primary
  ON graph_nodes_index(project_id, snapshot_id, node_id);

CREATE TABLE IF NOT EXISTS graph_edges_index (
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  edge_type TEXT NOT NULL,
  direction TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(project_id, snapshot_id, src, dst, edge_type, direction)
);

CREATE INDEX IF NOT EXISTS idx_graph_edges_dst
  ON graph_edges_index(project_id, snapshot_id, dst);

CREATE TABLE IF NOT EXISTS graph_drift_ledger (
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL,
  path TEXT NOT NULL,
  node_id TEXT NOT NULL DEFAULT '',
  target_symbol TEXT NOT NULL DEFAULT '',
  drift_type TEXT NOT NULL,
  status TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL,
  PRIMARY KEY(project_id, snapshot_id, path, drift_type, target_symbol)
);

CREATE INDEX IF NOT EXISTS idx_graph_drift_status
  ON graph_drift_ledger(project_id, status, drift_type);

CREATE TABLE IF NOT EXISTS pending_scope_reconcile (
  project_id TEXT NOT NULL,
  ref_name TEXT NOT NULL DEFAULT 'active',
  branch_ref TEXT NOT NULL DEFAULT '',
  worktree_id TEXT NOT NULL DEFAULT '',
  worktree_path TEXT NOT NULL DEFAULT '',
  commit_sha TEXT NOT NULL,
  parent_commit_sha TEXT NOT NULL DEFAULT '',
  queued_at TEXT NOT NULL,
  status TEXT NOT NULL,
  retry_count INTEGER NOT NULL DEFAULT 0,
  snapshot_id TEXT NOT NULL DEFAULT '',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(project_id, ref_name, worktree_id, commit_sha)
);

CREATE TABLE IF NOT EXISTS reconcile_run_metrics (
  project_id TEXT NOT NULL,
  run_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  commit_sha TEXT NOT NULL DEFAULT '',
  parent_commit_sha TEXT NOT NULL DEFAULT '',
  snapshot_kind TEXT NOT NULL DEFAULT '',
  strategy TEXT NOT NULL DEFAULT '',
  graph_delta_mode TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  changed_file_count INTEGER NOT NULL DEFAULT 0,
  impacted_file_count INTEGER NOT NULL DEFAULT 0,
  event_count INTEGER NOT NULL DEFAULT 0,
  node_count INTEGER NOT NULL DEFAULT 0,
  edge_count INTEGER NOT NULL DEFAULT 0,
  elapsed_ms INTEGER NOT NULL DEFAULT 0,
  trace_summary_path TEXT NOT NULL DEFAULT '',
  fallback_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  evidence_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(project_id, run_id, snapshot_id)
);

CREATE INDEX IF NOT EXISTS idx_reconcile_run_metrics_project_created
  ON reconcile_run_metrics(project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_reconcile_run_metrics_strategy
  ON reconcile_run_metrics(project_id, strategy, graph_delta_mode);
"""

SNAPSHOT_STATUS_CANDIDATE = "candidate"
SNAPSHOT_STATUS_FINALIZING = "finalizing"
SNAPSHOT_STATUS_ACTIVE = "active"
SNAPSHOT_STATUS_SUPERSEDED = "superseded"
SNAPSHOT_STATUS_ABANDONED = "abandoned"

ALLOWED_SNAPSHOT_STATUSES = {
    SNAPSHOT_STATUS_CANDIDATE,
    SNAPSHOT_STATUS_FINALIZING,
    SNAPSHOT_STATUS_ACTIVE,
    SNAPSHOT_STATUS_SUPERSEDED,
    SNAPSHOT_STATUS_ABANDONED,
}

PENDING_STATUS_QUEUED = "queued"
PENDING_STATUS_RUNNING = "running"
PENDING_STATUS_MATERIALIZED = "materialized"
PENDING_STATUS_FAILED = "failed"
PENDING_STATUS_WAIVED = "waived"

ALLOWED_PENDING_STATUSES = {
    PENDING_STATUS_QUEUED,
    PENDING_STATUS_RUNNING,
    PENDING_STATUS_MATERIALIZED,
    PENDING_STATUS_FAILED,
    PENDING_STATUS_WAIVED,
}
GRAPH_REF_OPERATION_TYPES = {
    "activate",
    "merge",
    "rollback",
    "revert",
    "replay",
    "backfill_escape",
}


class GraphSnapshotConflictError(RuntimeError):
    """Raised when snapshot activation loses its compare-and-swap race."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(GRAPH_SNAPSHOT_SCHEMA_SQL)
    _ensure_graph_snapshot_ref_columns(conn)
    _migrate_pending_scope_reconcile_branch_identity(conn)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {str(row["name"] if hasattr(row, "keys") else row[1]) for row in rows}


def _table_pk_columns(conn: sqlite3.Connection, table_name: str) -> list[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    except sqlite3.OperationalError:
        return []
    items: list[tuple[int, str]] = []
    for row in rows:
        pk = int(row["pk"] if hasattr(row, "keys") else row[5])
        if pk:
            items.append((pk, str(row["name"] if hasattr(row, "keys") else row[1])))
    return [name for _pk, name in sorted(items)]


def _ensure_graph_snapshot_ref_columns(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "graph_snapshots"):
        return
    columns = _table_columns(conn, "graph_snapshots")
    if "ref_name" not in columns:
        conn.execute("ALTER TABLE graph_snapshots ADD COLUMN ref_name TEXT NOT NULL DEFAULT ''")
    if "branch_ref" not in columns:
        conn.execute("ALTER TABLE graph_snapshots ADD COLUMN branch_ref TEXT NOT NULL DEFAULT ''")


def _migrate_pending_scope_reconcile_branch_identity(conn: sqlite3.Connection) -> None:
    """Upgrade pending scope rows from commit-only identity to ref/worktree identity."""
    if not _table_exists(conn, "pending_scope_reconcile"):
        return
    columns = _table_columns(conn, "pending_scope_reconcile")
    expected_columns = {"ref_name", "branch_ref", "worktree_id", "worktree_path"}
    expected_pk = ["project_id", "ref_name", "worktree_id", "commit_sha"]
    if expected_columns.issubset(columns) and _table_pk_columns(conn, "pending_scope_reconcile") == expected_pk:
        _ensure_pending_scope_reconcile_indexes(conn)
        return

    legacy_name = "pending_scope_reconcile_legacy_branch_identity"
    conn.execute("DROP TABLE IF EXISTS pending_scope_reconcile_migrated")
    conn.execute(f"DROP TABLE IF EXISTS {legacy_name}")
    conn.execute("DROP INDEX IF EXISTS idx_pending_scope_status")
    conn.execute("DROP INDEX IF EXISTS idx_pending_scope_branch")
    conn.execute(f"ALTER TABLE pending_scope_reconcile RENAME TO {legacy_name}")
    conn.execute(
        """
        CREATE TABLE pending_scope_reconcile (
          project_id TEXT NOT NULL,
          ref_name TEXT NOT NULL DEFAULT 'active',
          branch_ref TEXT NOT NULL DEFAULT '',
          worktree_id TEXT NOT NULL DEFAULT '',
          worktree_path TEXT NOT NULL DEFAULT '',
          commit_sha TEXT NOT NULL,
          parent_commit_sha TEXT NOT NULL DEFAULT '',
          queued_at TEXT NOT NULL,
          status TEXT NOT NULL,
          retry_count INTEGER NOT NULL DEFAULT 0,
          snapshot_id TEXT NOT NULL DEFAULT '',
          evidence_json TEXT NOT NULL DEFAULT '{}',
          PRIMARY KEY(project_id, ref_name, worktree_id, commit_sha)
        )
        """
    )
    legacy_columns = _table_columns(conn, legacy_name)

    def expr(column: str, default: str) -> str:
        if column in legacy_columns:
            return f"COALESCE({column}, {default})"
        return default

    conn.execute(
        f"""
        INSERT OR REPLACE INTO pending_scope_reconcile
          (project_id, ref_name, branch_ref, worktree_id, worktree_path,
           commit_sha, parent_commit_sha, queued_at, status, retry_count,
           snapshot_id, evidence_json)
        SELECT
          project_id,
          {expr('ref_name', "'active'")},
          {expr('branch_ref', "''")},
          {expr('worktree_id', "''")},
          {expr('worktree_path', "''")},
          commit_sha,
          parent_commit_sha,
          queued_at,
          status,
          retry_count,
          snapshot_id,
          evidence_json
        FROM {legacy_name}
        """
    )
    conn.execute(f"DROP TABLE {legacy_name}")
    _ensure_pending_scope_reconcile_indexes(conn)


def _ensure_pending_scope_reconcile_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_scope_status "
        "ON pending_scope_reconcile(project_id, status, ref_name, queued_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_scope_branch "
        "ON pending_scope_reconcile(project_id, branch_ref, worktree_id, commit_sha)"
    )


def normalize_pending_scope_identity(
    *,
    ref_name: str = "",
    branch_ref: str = "",
    worktree_id: str = "",
    worktree_path: str = "",
) -> dict[str, str]:
    branch = str(branch_ref or "").strip()
    raw_path = str(worktree_path or "").strip()
    normalized_path = ""
    if raw_path:
        try:
            normalized_path = str(Path(raw_path).expanduser().resolve()).replace("\\", "/")
        except Exception:
            normalized_path = str(Path(raw_path).expanduser()).replace("\\", "/")
    wid = str(worktree_id or "").strip()
    if not wid and normalized_path:
        digest = hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()[:12]
        wid = f"worktree:{digest}"
    ref = str(ref_name or "").strip()
    if not ref:
        ref = branch or wid or "active"
    return {
        "ref_name": ref,
        "branch_ref": branch,
        "worktree_id": wid,
        "worktree_path": normalized_path,
    }


def _json(data: Any) -> str:
    return json.dumps(data if data is not None else {}, sort_keys=True, ensure_ascii=False)


def _latest_projection_id(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    ref_name: str = "",
    branch_ref: str = "",
) -> str:
    if not snapshot_id:
        return ""
    try:
        from . import graph_events

        projection = graph_events.get_semantic_projection(
            conn,
            project_id,
            snapshot_id,
            ref_name=ref_name,
            branch_ref=branch_ref,
        )
    except Exception:
        return ""
    return str((projection or {}).get("projection_id") or "")


def record_graph_ref_event(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    ref_name: str,
    operation_type: str,
    old_snapshot_id: str = "",
    new_snapshot_id: str = "",
    old_commit: str = "",
    new_commit: str = "",
    old_projection_id: str = "",
    new_projection_id: str = "",
    branch_ref: str = "",
    batch_id: str = "",
    merge_queue_id: str = "",
    merge_epoch: str = "",
    rollback_epoch: str = "",
    replay_epoch: str = "",
    source_event_id: str = "",
    actor: str = "",
    evidence: dict[str, Any] | None = None,
    event_id: str = "",
    created_at: str = "",
) -> dict[str, Any]:
    ensure_schema(conn)
    op = str(operation_type or "").strip()
    if op not in GRAPH_REF_OPERATION_TYPES:
        raise ValueError(f"invalid graph ref operation_type: {operation_type}")
    event = event_id or f"gref-{uuid.uuid4().hex[:16]}"
    now = created_at or utc_now()
    row = {
        "project_id": project_id,
        "event_id": event,
        "ref_name": str(ref_name or "active"),
        "branch_ref": str(branch_ref or ""),
        "batch_id": str(batch_id or ""),
        "merge_queue_id": str(merge_queue_id or ""),
        "operation_type": op,
        "old_snapshot_id": str(old_snapshot_id or ""),
        "new_snapshot_id": str(new_snapshot_id or ""),
        "old_commit": str(old_commit or ""),
        "new_commit": str(new_commit or ""),
        "old_projection_id": str(old_projection_id or ""),
        "new_projection_id": str(new_projection_id or ""),
        "merge_epoch": str(merge_epoch or ""),
        "rollback_epoch": str(rollback_epoch or ""),
        "replay_epoch": str(replay_epoch or ""),
        "source_event_id": str(source_event_id or ""),
        "actor": str(actor or ""),
        "evidence_json": _json(evidence or {}),
        "created_at": now,
    }
    conn.execute(
        """
        INSERT INTO graph_ref_events (
          project_id, event_id, ref_name, branch_ref, batch_id, merge_queue_id,
          operation_type, old_snapshot_id, new_snapshot_id, old_commit, new_commit,
          old_projection_id, new_projection_id, merge_epoch, rollback_epoch,
          replay_epoch, source_event_id, actor, evidence_json, created_at
        )
        VALUES (
          :project_id, :event_id, :ref_name, :branch_ref, :batch_id, :merge_queue_id,
          :operation_type, :old_snapshot_id, :new_snapshot_id, :old_commit, :new_commit,
          :old_projection_id, :new_projection_id, :merge_epoch, :rollback_epoch,
          :replay_epoch, :source_event_id, :actor, :evidence_json, :created_at
        )
        """,
        row,
    )
    out = dict(row)
    out["evidence"] = evidence or {}
    return out


def list_graph_ref_events(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    ref_name: str = "",
    operation_type: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id]
    sql = "SELECT * FROM graph_ref_events WHERE project_id = ?"
    if ref_name:
        sql += " AND ref_name = ?"
        params.append(ref_name)
    if operation_type:
        sql += " AND operation_type = ?"
        params.append(operation_type)
    sql += " ORDER BY created_at DESC, event_id DESC LIMIT ?"
    params.append(max(1, min(500, int(limit or 50))))
    rows = conn.execute(sql, params).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["evidence"] = _decode_json(item.get("evidence_json"), {})
        out.append(item)
    return out


def _json_list(data: Any) -> str:
    if data is None:
        return "[]"
    if isinstance(data, list):
        return json.dumps(data, sort_keys=True, ensure_ascii=False)
    return json.dumps([data], sort_keys=True, ensure_ascii=False)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_id_for(snapshot_kind: str, commit_sha: str, suffix: str | None = None) -> str:
    clean_kind = (snapshot_kind or "snapshot").strip().replace("_", "-").lower()
    short = (commit_sha or "unknown").strip()[:7] or "unknown"
    tail = suffix or uuid.uuid4().hex[:4]
    return f"{clean_kind}-{short}-{tail}"


def _snapshot_root(project_id: str, snapshot_id: str) -> Path:
    from .db import _governance_root

    return _governance_root() / project_id / "graph-snapshots" / snapshot_id


def snapshot_companion_dir(project_id: str, snapshot_id: str) -> Path:
    return _snapshot_root(project_id, snapshot_id)


def snapshot_graph_path(project_id: str, snapshot_id: str) -> Path:
    return snapshot_companion_dir(project_id, snapshot_id) / "graph.json"


def write_companion_files(
    project_id: str,
    snapshot_id: str,
    *,
    graph_json: dict[str, Any] | None = None,
    file_inventory: list[dict[str, Any]] | None = None,
    drift_ledger: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    base_dir = _snapshot_root(project_id, snapshot_id)
    base_dir.mkdir(parents=True, exist_ok=True)

    graph_bytes = _json(graph_json or {}).encode("utf-8")
    inventory_bytes = _json(file_inventory or []).encode("utf-8")
    drift_bytes = _json(drift_ledger or []).encode("utf-8")

    graph_sha = _sha256_bytes(graph_bytes)
    inventory_sha = _sha256_bytes(inventory_bytes)
    drift_sha = _sha256_bytes(drift_bytes)

    (base_dir / "graph.json").write_bytes(graph_bytes)
    (base_dir / "file_inventory.json").write_bytes(inventory_bytes)
    (base_dir / "drift_ledger.json").write_bytes(drift_bytes)

    manifest = {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "graph_sha256": graph_sha,
        "inventory_sha256": inventory_sha,
        "drift_sha256": drift_sha,
        "created_at": utc_now(),
    }
    (base_dir / "manifest.json").write_text(_json(manifest), encoding="utf-8")
    return {
        "graph_sha256": graph_sha,
        "inventory_sha256": inventory_sha,
        "drift_sha256": drift_sha,
        "path": str(base_dir),
    }


def _graph_nodes(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    deps = graph_json.get("deps_graph") if isinstance(graph_json, dict) else {}
    if isinstance(deps, dict) and isinstance(deps.get("nodes"), list):
        return [n for n in deps.get("nodes", []) if isinstance(n, dict)]
    nodes = graph_json.get("nodes") if isinstance(graph_json, dict) else []
    if isinstance(nodes, list):
        return [n for n in nodes if isinstance(n, dict)]
    if isinstance(nodes, dict):
        result = []
        for node_id, node in nodes.items():
            item = dict(node) if isinstance(node, dict) else {}
            item.setdefault("id", str(node_id))
            result.append(item)
        return result
    return []


def graph_payload_edges(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Return normalized hierarchy/evidence/dependency edges from a graph payload."""
    if not isinstance(graph_json, dict):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    sections = [
        ("hierarchy_graph", "hierarchy"),
        ("evidence_graph", "evidence"),
        ("deps_graph", "dependency"),
        ("gates_graph", "gate"),
    ]
    for section_name, default_direction in sections:
        section = graph_json.get(section_name)
        if not isinstance(section, dict):
            continue
        raw_edges = section.get("edges") if "edges" in section else section.get("links")
        for edge in raw_edges or []:
            if not isinstance(edge, dict):
                continue
            src = str(edge.get("src") or edge.get("source") or "")
            dst = str(edge.get("dst") or edge.get("target") or "")
            edge_type = str(edge.get("edge_type") or edge.get("type") or "depends_on")
            direction = str(edge.get("direction") or default_direction)
            if not src or not dst:
                continue
            key = (src, dst, edge_type, direction)
            if key in seen:
                continue
            item = dict(edge)
            item["src"] = src
            item["dst"] = dst
            item["edge_type"] = edge_type
            item["direction"] = direction
            evidence = item.get("evidence")
            metadata = item.get("metadata")
            if metadata and "evidence" not in item:
                item["evidence"] = metadata
            elif evidence and "metadata" not in item:
                item.setdefault("metadata", {"evidence": evidence})
            item.setdefault("section", section_name)
            result.append(item)
            seen.add(key)
    if result:
        return result
    edges = graph_json.get("edges") if isinstance(graph_json, dict) else []
    if isinstance(edges, list):
        return [e for e in edges if isinstance(e, dict)]
    return []


def _graph_edges(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    return graph_payload_edges(graph_json)


def graph_payload_stats(graph_json: dict[str, Any]) -> dict[str, int]:
    return {"nodes": len(_graph_nodes(graph_json)), "edges": len(_graph_edges(graph_json))}


def _decode_json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (list, dict)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return default
    return default


def _row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    if key not in row.keys():
        return default
    return row[key]


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _compact_pending_scope_row(row: dict[str, Any]) -> dict[str, Any]:
    evidence = _decode_json(row.get("evidence_json"), {})
    return {
        "ref_name": str(row.get("ref_name") or ""),
        "branch_ref": str(row.get("branch_ref") or ""),
        "worktree_id": str(row.get("worktree_id") or ""),
        "commit_sha": str(row.get("commit_sha") or ""),
        "status": str(row.get("status") or ""),
        "snapshot_id": str(row.get("snapshot_id") or ""),
        "evidence": evidence,
    }


def _source_matches_ref_event(source: str, event: dict[str, Any]) -> bool:
    source_value = str(source or "").strip()
    if not source_value:
        return False
    return source_value in {
        str(event.get("event_id") or ""),
        str(event.get("merge_epoch") or ""),
        str(event.get("new_snapshot_id") or ""),
        str(event.get("new_commit") or ""),
    }


def _rollback_source_values(event: dict[str, Any]) -> set[str]:
    evidence = event.get("evidence") if isinstance(event.get("evidence"), dict) else {}
    sources = {str(event.get("source_event_id") or "").strip()}
    for key in ("abandoned_event_ids", "abandoned_merge_epochs", "abandoned_snapshot_ids"):
        values = evidence.get(key)
        if isinstance(values, list):
            sources.update(str(value or "").strip() for value in values)
        elif values:
            sources.add(str(values).strip())
    return {source for source in sources if source}


def _projection_rows_by_id(
    conn: sqlite3.Connection,
    project_id: str,
    projection_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    ids = sorted({str(pid or "").strip() for pid in projection_ids if str(pid or "").strip()})
    if not ids or not _table_exists(conn, "graph_semantic_projections"):
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM graph_semantic_projections
        WHERE project_id = ? AND projection_id IN ({placeholders})
        """,
        (project_id, *ids),
    ).fetchall()
    return {str(row["projection_id"]): dict(row) for row in rows}


def _semantic_job_rows_for_snapshots(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_ids: set[str],
) -> list[dict[str, Any]]:
    if not snapshot_ids or not _table_exists(conn, "graph_semantic_jobs"):
        return []
    ids = sorted(snapshot_ids)
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id IN ({placeholders})
        ORDER BY snapshot_id, node_id
        """,
        (project_id, *ids),
    ).fetchall()
    return [dict(row) for row in rows]


def _semantic_job_rollback_disposition(
    row: dict[str, Any],
    *,
    active_snapshot_id: str,
    abandoned_snapshot_ids: set[str],
    branch_candidate_snapshot_ids: set[str],
) -> str:
    snapshot_id = str(row.get("snapshot_id") or "")
    status = str(row.get("status") or "").strip().lower()
    if snapshot_id in abandoned_snapshot_ids:
        return "cancelled_by_rollback" if status in {"cancelled", "canceled"} else "abandoned"
    if snapshot_id == active_snapshot_id:
        return "current"
    if snapshot_id in branch_candidate_snapshot_ids or row.get("branch_ref"):
        return "candidate"
    return "historical"


def build_graph_rollback_epoch_state(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    ref_name: str = "active",
    rollback_epoch: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """Return compact PB-005/PB-011 graph rollback state for operators/tests."""
    ensure_schema(conn)
    bounded_limit = max(1, min(500, int(limit or 50)))
    ref = normalize_pending_scope_identity(ref_name=ref_name)["ref_name"]
    ref_row = conn.execute(
        """
        SELECT snapshot_id, commit_sha
        FROM graph_snapshot_refs
        WHERE project_id = ? AND ref_name = ?
        """,
        (project_id, ref),
    ).fetchone()
    all_events = list_graph_ref_events(conn, project_id, limit=bounded_limit)
    active_snapshot_id = str(ref_row["snapshot_id"] if ref_row else "")
    target_events = [event for event in all_events if str(event.get("ref_name") or "") == ref]
    rollback_events = [
        event for event in target_events
        if event.get("operation_type") == "rollback"
        and (not rollback_epoch or event.get("rollback_epoch") == rollback_epoch)
    ]
    active_event = next(
        (
            event for event in target_events
            if str(event.get("new_snapshot_id") or "") == active_snapshot_id
        ),
        target_events[0] if target_events else {},
    )
    rollback_event = rollback_events[0] if rollback_events else {}
    abandoned_sources = _rollback_source_values(rollback_event)
    abandoned_merge_events: list[dict[str, Any]] = []
    for event in target_events:
        if event.get("operation_type") != "merge":
            continue
        explicit_match = any(_source_matches_ref_event(source, event) for source in abandoned_sources)
        same_batch = (
            rollback_event
            and event.get("batch_id")
            and event.get("batch_id") == rollback_event.get("batch_id")
        )
        if explicit_match or (not abandoned_sources and same_batch):
            abandoned_merge_events.append(event)

    abandoned_event_ids = {str(event.get("event_id") or "") for event in abandoned_merge_events}
    abandoned_merge_epochs = {
        str(event.get("merge_epoch") or "")
        for event in abandoned_merge_events
        if event.get("merge_epoch")
    }
    branch_candidate_events = [
        event for event in all_events
        if str(event.get("ref_name") or "") != ref
    ]
    abandoned_snapshot_ids = {
        str(event.get("new_snapshot_id") or "")
        for event in abandoned_merge_events
        if event.get("new_snapshot_id")
    }
    branch_candidate_snapshot_ids = {
        str(event.get("new_snapshot_id") or "")
        for event in branch_candidate_events
        if event.get("new_snapshot_id")
    }
    projection_ids: set[str] = set()
    event_snapshot_ids: set[str] = {active_snapshot_id} if active_snapshot_id else set()
    for event in all_events:
        snapshot_id = str(event.get("new_snapshot_id") or "").strip()
        if snapshot_id:
            event_snapshot_ids.add(snapshot_id)
        for key in ("old_projection_id", "new_projection_id"):
            value = str(event.get(key) or "").strip()
            if value:
                projection_ids.add(value)
    projection_rows = _projection_rows_by_id(conn, project_id, projection_ids)
    projection_states: list[dict[str, Any]] = []
    seen_projection_ids: set[str] = set()
    for event in all_events:
        projection_id = str(event.get("new_projection_id") or "").strip()
        if not projection_id or projection_id in seen_projection_ids:
            continue
        seen_projection_ids.add(projection_id)
        projection_row = projection_rows.get(projection_id, {})
        event_id = str(event.get("event_id") or "")
        event_ref = str(event.get("ref_name") or "")
        event_branch = str(event.get("branch_ref") or "")
        if event_id in abandoned_event_ids or str(event.get("merge_epoch") or "") in abandoned_merge_epochs:
            status = "abandoned"
        elif event_id == str(active_event.get("event_id") or "") and event_ref == ref:
            status = "current"
        elif event_ref != ref or event_branch:
            status = "candidate"
        else:
            status = "historical"
        projection_states.append({
            "projection_id": projection_id,
            "snapshot_id": str(event.get("new_snapshot_id") or ""),
            "ref_name": event_ref,
            "branch_ref": event_branch,
            "operation_type": str(event.get("operation_type") or ""),
            "event_id": event_id,
            "merge_epoch": str(event.get("merge_epoch") or ""),
            "rollback_epoch": str(event.get("rollback_epoch") or ""),
            "status": status,
            "base_commit": str(projection_row.get("base_commit") or event.get("new_commit") or ""),
        })

    all_pending_scope_rows = list_pending_scope_reconcile(conn, project_id)
    pending_rows = [
        _compact_pending_scope_row(row)
        for row in all_pending_scope_rows
    ][:bounded_limit]
    semantic_jobs = []
    for row in _semantic_job_rows_for_snapshots(conn, project_id, event_snapshot_ids):
        semantic_jobs.append({
            "snapshot_id": str(row.get("snapshot_id") or ""),
            "node_id": str(row.get("node_id") or ""),
            "status": str(row.get("status") or ""),
            "branch_ref": str(row.get("branch_ref") or ""),
            "operation_type": str(row.get("operation_type") or ""),
            "feature_hash": str(row.get("feature_hash") or ""),
            "attempt_count": int(row.get("attempt_count") or 0),
            "worker_id": str(row.get("worker_id") or ""),
            "claim_id": str(row.get("claim_id") or ""),
            "lease_expires_at": str(row.get("lease_expires_at") or ""),
            "last_error": str(row.get("last_error") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "rollback_disposition": _semantic_job_rollback_disposition(
                row,
                active_snapshot_id=active_snapshot_id,
                abandoned_snapshot_ids=abandoned_snapshot_ids,
                branch_candidate_snapshot_ids=branch_candidate_snapshot_ids,
            ),
        })
    return {
        "ok": True,
        "project_id": project_id,
        "ref_name": ref,
        "rollback_epoch": rollback_epoch or str(rollback_event.get("rollback_epoch") or ""),
        "active": {
            "snapshot_id": active_snapshot_id,
            "commit_sha": str(ref_row["commit_sha"] if ref_row else ""),
            "event_id": str(active_event.get("event_id") or ""),
            "operation_type": str(active_event.get("operation_type") or ""),
            "projection_id": str(active_event.get("new_projection_id") or ""),
        },
        "rollback_event": rollback_event,
        "abandoned_merge_epochs": sorted(abandoned_merge_epochs),
        "abandoned_merge_events": abandoned_merge_events[:bounded_limit],
        "branch_candidates": branch_candidate_events[:bounded_limit],
        "projection_states": projection_states[:bounded_limit],
        "pending_scope": pending_rows,
        "semantic_jobs": semantic_jobs[:bounded_limit],
        "total_counts": {
            "ref_events": len(all_events),
            "abandoned_merge_events": len(abandoned_merge_events),
            "branch_candidates": len(branch_candidate_events),
            "projection_states": len(projection_states),
            "pending_scope": len(all_pending_scope_rows),
            "semantic_jobs": len(semantic_jobs),
        },
        "truncated": {
            "ref_events": len(all_events) >= bounded_limit,
            "abandoned_merge_events": len(abandoned_merge_events) > bounded_limit,
            "branch_candidates": len(branch_candidate_events) > bounded_limit,
            "projection_states": len(projection_states) > bounded_limit,
            "pending_scope": len(all_pending_scope_rows) > bounded_limit,
            "semantic_jobs": len(semantic_jobs) > bounded_limit,
        },
    }


def invalidate_semantic_jobs_for_rollback_epoch(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    ref_name: str = "active",
    rollback_epoch: str,
    actor: str = "observer",
    now: str = "",
) -> dict[str, Any]:
    """Cancel open semantic jobs tied to merge snapshots abandoned by rollback."""
    ensure_schema(conn)
    if not _table_exists(conn, "graph_semantic_jobs"):
        return {
            "ok": True,
            "project_id": project_id,
            "rollback_epoch": rollback_epoch,
            "matched_count": 0,
            "invalidated_count": 0,
            "semantic_jobs": [],
        }
    state = build_graph_rollback_epoch_state(
        conn,
        project_id,
        ref_name=ref_name,
        rollback_epoch=rollback_epoch,
        limit=500,
    )
    abandoned_snapshot_ids = {
        str(event.get("new_snapshot_id") or "")
        for event in state.get("abandoned_merge_events", [])
        if event.get("new_snapshot_id")
    }
    if not abandoned_snapshot_ids:
        return {
            "ok": True,
            "project_id": project_id,
            "rollback_epoch": rollback_epoch,
            "matched_count": 0,
            "invalidated_count": 0,
            "semantic_jobs": [],
        }
    rows = _semantic_job_rows_for_snapshots(conn, project_id, abandoned_snapshot_ids)
    terminal = {
        "cancelled",
        "canceled",
        "failed",
        "ai_complete",
        "complete",
        "rule_complete",
    }
    open_rows = [
        row for row in rows
        if str(row.get("status") or "").strip().lower() not in terminal
    ]
    stamp = now or utc_now()
    if open_rows:
        placeholders = ",".join("(?, ?, ?)" for _ in open_rows)
        params: list[Any] = []
        for row in open_rows:
            params.extend([project_id, row["snapshot_id"], row["node_id"]])
        reason = f"invalidated by rollback_epoch {rollback_epoch} ({actor})"
        conn.execute(
            f"""
            UPDATE graph_semantic_jobs
            SET status = 'cancelled',
                operation_type = 'rollback_invalidated',
                worker_id = '',
                claim_id = '',
                claimed_at = '',
                lease_expires_at = '',
                claimed_by = '',
                last_error = ?,
                updated_at = ?
            WHERE (project_id, snapshot_id, node_id) IN ({placeholders})
            """,
            (reason, stamp, *params),
        )
    return {
        "ok": True,
        "project_id": project_id,
        "rollback_epoch": rollback_epoch,
        "matched_count": len(rows),
        "invalidated_count": len(open_rows),
        "semantic_jobs": _semantic_job_rows_for_snapshots(conn, project_id, abandoned_snapshot_ids),
    }


def _semantic_hash_state(status: str, feature_hash: str, payload: dict[str, Any]) -> str:
    status_norm = str(status or "").strip().lower()
    validation = payload.get("semantic_state_validation")
    if isinstance(validation, dict):
        validation_status = str(validation.get("status") or "").lower()
        if validation_status in {"stale_hash_mismatch", "hash_mismatch", "stale"}:
            return "stale"
        if validation.get("valid") is True:
            return "current"
        if validation.get("valid") is False:
            return "stale"

    flags = payload.get("quality_flags")
    if isinstance(flags, list):
        flag_set = {str(flag or "").strip().lower() for flag in flags}
        if flag_set.intersection({"semantic_hash_mismatch", "source_hash_changed", "semantic_stale"}):
            return "stale"

    if status_norm in {"pending_review", "review_pending"}:
        return "pending"
    if status_norm in {"ai_complete", "semantic_graph_state", "reviewed"} and feature_hash:
        return "current"
    if status_norm in {"pending_ai", "ai_pending", "running", "ai_running"}:
        return "pending"
    if status_norm in {"ai_failed", "failed"}:
        return "failed"
    return "unknown"


def _semantic_overlay_from_node_row(row: sqlite3.Row) -> dict[str, Any]:
    payload = _decode_json(_row_value(row, "semantic_json", ""), {})
    if not isinstance(payload, dict):
        payload = {}
    file_hashes = _decode_json(_row_value(row, "semantic_file_hashes_json", ""), {})
    if not isinstance(file_hashes, dict):
        file_hashes = {}

    node_status = str(_row_value(row, "semantic_status", "") or "")
    job_status = str(_row_value(row, "semantic_job_status", "") or "")
    job_status_norm = job_status.lower()
    payload_status = str(payload.get("status") or "")
    status = node_status or payload_status or "structure_only"
    if not node_status and not payload_status and job_status_norm in {
        "pending_ai",
        "ai_pending",
        "running",
        "ai_running",
        "ai_failed",
        "failed",
        "cancelled",
        "canceled",
        "rejected",
    }:
        status = job_status
    api_status = "review_pending" if status == "pending_review" else status
    feature_hash = str(
        _row_value(row, "semantic_feature_hash", "")
        or payload.get("feature_hash")
        or ""
    )
    updated_at = str(
        _row_value(row, "semantic_updated_at", "")
        or _row_value(row, "semantic_job_updated_at", "")
        or payload.get("updated_at")
        or ""
    )

    overlay = dict(payload)
    overlay.update({
        "status": api_status,
        "node_status": node_status,
        "job_status": job_status,
        "feature_hash": feature_hash,
        "file_hashes": file_hashes,
        "feedback_round": _row_value(row, "semantic_feedback_round", payload.get("feedback_round", 0)) or 0,
        "batch_index": _row_value(row, "semantic_batch_index", payload.get("batch_index")),
        "updated_at": updated_at,
        "hash_state": _semantic_hash_state(status, feature_hash, payload),
        "has_semantic_payload": bool(node_status and payload),
    })

    if job_status:
        overlay["job"] = {
            "status": job_status,
            "feature_hash": str(_row_value(row, "semantic_job_feature_hash", "") or ""),
            "attempt_count": int(_row_value(row, "semantic_job_attempt_count", 0) or 0),
            "last_error": str(_row_value(row, "semantic_job_last_error", "") or ""),
            "worker_id": str(_row_value(row, "semantic_job_worker_id", "") or ""),
            "claim_id": str(_row_value(row, "semantic_job_claim_id", "") or ""),
            "claimed_at": str(_row_value(row, "semantic_job_claimed_at", "") or ""),
            "lease_expires_at": str(_row_value(row, "semantic_job_lease_expires_at", "") or ""),
            "claimed_by": str(_row_value(row, "semantic_job_claimed_by", "") or ""),
            "updated_at": str(_row_value(row, "semantic_job_updated_at", "") or ""),
        }
    return overlay


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_json_artifact(path: Path, default: Any) -> Any:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return payload if payload is not None else default


def _current_graph_path(project_id: str) -> Path:
    from .db import _governance_root

    return _governance_root() / project_id / "graph.json"


def _baseline_graph_path(project_id: str, baseline_id: int) -> Path:
    from .db import _governance_root

    return _governance_root() / project_id / "baselines" / str(baseline_id) / "graph.json"


def _resolve_import_commit(conn: sqlite3.Connection, project_id: str, explicit: str = "") -> str:
    if explicit:
        return explicit
    try:
        row = conn.execute(
            "SELECT chain_version, git_head FROM project_version WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row:
        # chain_version is the last governed/service version; git_head may include
        # advisory MF commits that the graph has not materialized yet.
        if hasattr(row, "keys"):
            chain_version = row["chain_version"] if "chain_version" in row.keys() else ""
            git_head = row["git_head"] if "git_head" in row.keys() else ""
        else:
            chain_version = row[0] if len(row) > 0 else ""
            git_head = row[1] if len(row) > 1 else ""
        return chain_version or git_head or "unknown"
    return "unknown"


def select_existing_graph_source(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    extra_graph_paths: Iterable[str | Path] | None = None,
) -> dict[str, Any] | None:
    """Select the best existing graph payload to import.

    Empty baseline companion graphs are skipped because older scan-only
    baselines often wrote `{}` while the active graph still lived at the
    shared-volume graph path.
    """
    ensure_schema(conn)
    try:
        rows = conn.execute(
            """
            SELECT baseline_id FROM version_baselines
            WHERE project_id = ?
            ORDER BY baseline_id DESC
            """,
            (project_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    if rows:
        from .baseline_service import read_companion_file

        for row in rows:
            baseline_id = row["baseline_id"] if hasattr(row, "keys") else row[0]
            try:
                graph_json = read_companion_file(project_id, int(baseline_id), "graph.json")
            except Exception:
                continue
            stats = graph_payload_stats(graph_json)
            if stats["nodes"] > 0:
                path = _baseline_graph_path(project_id, int(baseline_id))
                return {
                    "source_kind": "baseline_companion",
                    "source_path": str(path),
                    "source_ref": str(baseline_id),
                    "graph_json": graph_json,
                    "stats": stats,
                }

    candidates: list[tuple[str, Path]] = [("shared_volume_current", _current_graph_path(project_id))]
    for path in extra_graph_paths or []:
        candidates.append(("explicit_path", Path(path)))

    for source_kind, path in candidates:
        if not path.exists():
            continue
        graph_json = _read_json_file(path)
        stats = graph_payload_stats(graph_json)
        if stats["nodes"] > 0:
            return {
                "source_kind": source_kind,
                "source_path": str(path),
                "source_ref": "",
                "graph_json": graph_json,
                "stats": stats,
            }
    return None


def create_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    commit_sha: str,
    snapshot_kind: str,
    snapshot_id: str | None = None,
    parent_snapshot_id: str = "",
    ref_name: str = "",
    branch_ref: str = "",
    graph_json: dict[str, Any] | None = None,
    file_inventory: list[dict[str, Any]] | None = None,
    drift_ledger: list[dict[str, Any]] | None = None,
    status: str = SNAPSHOT_STATUS_CANDIDATE,
    created_by: str = "",
    notes: str = "",
) -> dict[str, Any]:
    ensure_schema(conn)
    if status not in ALLOWED_SNAPSHOT_STATUSES:
        raise ValueError(f"invalid graph snapshot status: {status}")
    sid = snapshot_id or snapshot_id_for(snapshot_kind, commit_sha)
    ref_value = str(ref_name or "").strip()
    branch_value = str(branch_ref or "").strip()
    if not ref_value and branch_value:
        ref_value = branch_value
    if ref_value == "active" and not branch_value:
        ref_value = ""
    shas = write_companion_files(
        project_id,
        sid,
        graph_json=graph_json,
        file_inventory=file_inventory,
        drift_ledger=drift_ledger,
    )
    now = utc_now()
    conn.execute(
        """
        INSERT INTO graph_snapshots
          (project_id, snapshot_id, commit_sha, parent_snapshot_id, snapshot_kind,
           ref_name, branch_ref, graph_sha256, inventory_sha256, drift_sha256, status, created_at,
           created_by, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            sid,
            commit_sha,
            parent_snapshot_id,
            snapshot_kind,
            ref_value,
            branch_value,
            shas["graph_sha256"],
            shas["inventory_sha256"],
            shas["drift_sha256"],
            status,
            now,
            created_by,
            notes,
        ),
    )
    return {
        "project_id": project_id,
        "snapshot_id": sid,
        "commit_sha": commit_sha,
        "snapshot_kind": snapshot_kind,
        "ref_name": ref_value,
        "branch_ref": branch_value,
        "status": status,
        "path": shas["path"],
        "graph_sha256": shas["graph_sha256"],
        "inventory_sha256": shas["inventory_sha256"],
        "drift_sha256": shas["drift_sha256"],
    }


def index_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    nodes: Iterable[dict[str, Any]] | None = None,
    edges: Iterable[dict[str, Any]] | None = None,
) -> dict[str, int]:
    ensure_schema(conn)
    node_count = 0
    for node in nodes or []:
        node_id = str(node.get("id") or node.get("node_id") or "")
        if not node_id:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO graph_nodes_index
              (project_id, snapshot_id, node_id, layer, title, kind,
               primary_files_json, secondary_files_json, test_files_json, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                snapshot_id,
                node_id,
                str(node.get("layer") or ""),
                str(node.get("title") or ""),
                str(node.get("kind") or node.get("metadata", {}).get("kind") or ""),
                _json_list(node.get("primary") or node.get("primary_files")),
                _json_list(node.get("secondary") or node.get("secondary_files")),
                _json_list(node.get("test") or node.get("test_files")),
                _json(node.get("metadata") or {}),
            ),
        )
        node_count += 1

    edge_count = 0
    for edge in edges or []:
        src = str(edge.get("src") or edge.get("source") or "")
        dst = str(edge.get("dst") or edge.get("target") or "")
        edge_type = str(edge.get("edge_type") or edge.get("type") or "depends_on")
        direction = str(edge.get("direction") or "dependency")
        if not src or not dst:
            continue
        conn.execute(
            """
            INSERT OR REPLACE INTO graph_edges_index
              (project_id, snapshot_id, src, dst, edge_type, direction, evidence_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project_id,
                snapshot_id,
                src,
                dst,
                edge_type,
                direction,
                _json(edge.get("evidence") or edge.get("evidence_json") or {}),
            ),
        )
        edge_count += 1
    return {"nodes": node_count, "edges": edge_count}


def activate_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    expected_old_snapshot_id: str | None = None,
    ref_name: str = "active",
    operation_type: str = "activate",
    branch_ref: str = "",
    batch_id: str = "",
    merge_queue_id: str = "",
    merge_epoch: str = "",
    rollback_epoch: str = "",
    replay_epoch: str = "",
    source_event_id: str = "",
    evidence: dict[str, Any] | None = None,
    actor: str = "activate_hook",
    auto_rebuild_projection: bool = True,
) -> dict[str, Any]:
    ensure_schema(conn)
    ref_name = normalize_pending_scope_identity(ref_name=ref_name)["ref_name"]
    op = str(operation_type or "").strip()
    if op not in GRAPH_REF_OPERATION_TYPES:
        raise ValueError(f"invalid graph ref operation_type: {operation_type}")
    activation_branch_ref = str(branch_ref or "").strip()
    if not activation_branch_ref and ref_name != "active":
        activation_branch_ref = ref_name
    row = conn.execute(
        "SELECT * FROM graph_snapshots WHERE project_id = ? AND snapshot_id = ?",
        (project_id, snapshot_id),
    ).fetchone()
    if not row:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    snapshot = dict(row)
    target_ref_activation = ref_name == "active"
    snapshot_ref_identity = str(snapshot.get("ref_name") or "").strip()
    snapshot_branch_identity = str(snapshot.get("branch_ref") or "").strip()
    snapshot_is_branch_candidate = bool(
        snapshot_branch_identity or (snapshot_ref_identity and snapshot_ref_identity != "active")
    )
    if target_ref_activation and snapshot_is_branch_candidate:
        raise ValueError(
            "branch graph candidate cannot be activated as active target graph truth; "
            "merge to the target ref and run target-ref scope reconcile first"
        )
    old = conn.execute(
        "SELECT snapshot_id, commit_sha FROM graph_snapshot_refs WHERE project_id = ? AND ref_name = ?",
        (project_id, ref_name),
    ).fetchone()
    old_id = old["snapshot_id"] if old else ""
    old_commit = old["commit_sha"] if old else ""
    if expected_old_snapshot_id is not None and old_id != expected_old_snapshot_id:
        raise GraphSnapshotConflictError(
            f"active snapshot changed: expected {expected_old_snapshot_id!r}, got {old_id!r}"
        )
    old_projection_id = _latest_projection_id(
        conn,
        project_id,
        old_id,
        ref_name=ref_name,
        branch_ref=activation_branch_ref,
    )

    now = utc_now()
    conn.execute(
        """
        INSERT INTO graph_snapshot_refs(project_id, ref_name, snapshot_id, commit_sha, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(project_id, ref_name) DO UPDATE SET
          snapshot_id = excluded.snapshot_id,
          commit_sha = excluded.commit_sha,
          updated_at = excluded.updated_at
        """,
        (project_id, ref_name, snapshot_id, snapshot["commit_sha"], now),
    )
    if ref_name != "active" or activation_branch_ref:
        conn.execute(
            """
            UPDATE graph_snapshots
            SET ref_name = CASE WHEN ref_name = '' THEN ? ELSE ref_name END,
                branch_ref = CASE WHEN branch_ref = '' THEN ? ELSE branch_ref END
            WHERE project_id = ? AND snapshot_id = ?
            """,
            (
                "" if ref_name == "active" and not activation_branch_ref else ref_name,
                activation_branch_ref,
                project_id,
                snapshot_id,
            ),
        )
    if target_ref_activation:
        conn.execute(
            "UPDATE graph_snapshots SET status = ? WHERE project_id = ? AND snapshot_id = ?",
            (SNAPSHOT_STATUS_ACTIVE, project_id, snapshot_id),
        )
    if target_ref_activation and old_id and old_id != snapshot_id:
        conn.execute(
            "UPDATE graph_snapshots SET status = ? WHERE project_id = ? AND snapshot_id = ?",
            (SNAPSHOT_STATUS_SUPERSEDED, project_id, old_id),
        )
    result: dict[str, Any] = {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "commit_sha": snapshot["commit_sha"],
        "previous_snapshot_id": old_id,
        "ref_name": ref_name,
        "candidate_ref_update": not target_ref_activation,
    }

    # MF-2026-05-10-012: dashboard derives feature counters from
    # graph_semantic_projections (per-snapshot cache), not from raw events.
    # Reconcile and admin recovery can both leave a freshly created snapshot
    # without a projection, which manifests as "Node semantic 0/0" the moment
    # it becomes active. Auto-rebuild on activate is idempotent — if the
    # target snapshot already has a projection, skip. If projection rebuild
    # fails (advisory only), we still report the activation as successful.
    projection_status = "skipped"
    if auto_rebuild_projection and ref_name == "active":
        try:
            from . import graph_events  # local import to avoid module cycle

            existing = graph_events.get_semantic_projection(
                conn,
                project_id,
                snapshot_id,
                ref_name=ref_name,
                branch_ref=activation_branch_ref,
            )
            if not existing or existing.get("status") in (None, "", "missing"):
                graph_events.materialize_events(conn, project_id, snapshot_id, actor=actor)
                graph_events.build_semantic_projection(
                    conn,
                    project_id,
                    snapshot_id,
                    actor=actor,
                    ref_name=ref_name,
                    branch_ref=activation_branch_ref,
                )
                projection_status = "rebuilt"
            else:
                projection_status = "already_present"
        except Exception as exc:  # noqa: BLE001 - advisory; activation already committed
            projection_status = f"rebuild_failed: {exc}"
    result["projection_status"] = projection_status
    new_projection_id = _latest_projection_id(
        conn,
        project_id,
        snapshot_id,
        ref_name=ref_name,
        branch_ref=activation_branch_ref,
    )
    try:
        ref_event = record_graph_ref_event(
            conn,
            project_id,
            ref_name=ref_name,
            operation_type=op,
            old_snapshot_id=old_id,
            new_snapshot_id=snapshot_id,
            old_commit=old_commit,
            new_commit=str(snapshot["commit_sha"] or ""),
            old_projection_id=old_projection_id,
            new_projection_id=new_projection_id,
            branch_ref=activation_branch_ref,
            batch_id=batch_id,
            merge_queue_id=merge_queue_id,
            merge_epoch=merge_epoch,
            rollback_epoch=rollback_epoch,
            replay_epoch=replay_epoch,
            source_event_id=source_event_id,
            actor=actor,
            evidence={
                "source": "activate_graph_snapshot",
                "projection_status": projection_status,
                **(evidence or {}),
            },
        )
        result["graph_ref_event_id"] = ref_event["event_id"]
        result["old_projection_id"] = old_projection_id
        result["new_projection_id"] = new_projection_id
    except ValueError:
        raise
    # MF 2026-05-11: snapshot activation is an in-process hook (no HTTP),
    # so _emit_dashboard_changed never fires for it. Publish here so the
    # dashboard's SSE subscribers refetch when a new snapshot becomes
    # active (reconcile / pending-scope materialize, etc.).
    if target_ref_activation:
        try:
            from . import event_bus
            event_bus.publish("snapshot.activated", {
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "previous_snapshot_id": old_id,
                "commit_sha": snapshot["commit_sha"],
                "ref_name": ref_name,
                "projection_status": projection_status,
                "graph_ref_event_id": result.get("graph_ref_event_id", ""),
                "source": "activate_graph_snapshot",
            })
            event_bus.publish("dashboard.changed", {
                "project_id": project_id,
                "path": "/internal/snapshot/activate",
                "method": "WORKER",
                "source": "activate_graph_snapshot",
            })
        except Exception:  # noqa: BLE001 - advisory
            pass
    return result


def finalize_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    target_commit_sha: str = "",
    expected_old_snapshot_id: str | None = None,
    ref_name: str = "active",
    branch_ref: str = "",
    worktree_id: str = "",
    worktree_path: str = "",
    actor: str = "observer",
    materialize_pending: bool = True,
    covered_commit_shas: Iterable[str] | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Activate a candidate graph snapshot and settle matching pending scope rows.

    This is the explicit signoff bridge from a state-only reconcile candidate to
    the active graph ref. It performs the same compare-and-swap check as
    ``activate_graph_snapshot`` and only marks pending rows for the exact
    snapshot commit as materialized.
    """
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM graph_snapshots WHERE project_id = ? AND snapshot_id = ?",
        (project_id, snapshot_id),
    ).fetchone()
    if not row:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    snapshot = dict(row)
    status = str(snapshot.get("status") or "")
    if status not in {
        SNAPSHOT_STATUS_CANDIDATE,
        SNAPSHOT_STATUS_FINALIZING,
        SNAPSHOT_STATUS_ACTIVE,
    }:
        raise ValueError(f"cannot finalize graph snapshot in status {status!r}")
    commit_sha = str(snapshot.get("commit_sha") or "")
    if target_commit_sha and commit_sha != target_commit_sha:
        raise ValueError(
            f"snapshot commit mismatch: expected {target_commit_sha}, got {commit_sha}"
        )

    activation = activate_graph_snapshot(
        conn,
        project_id,
        snapshot_id,
        expected_old_snapshot_id=expected_old_snapshot_id,
        ref_name=ref_name,
    )
    materialized_count = 0
    if materialize_pending:
        identity = normalize_pending_scope_identity(
            ref_name=ref_name,
            branch_ref=branch_ref,
            worktree_id=worktree_id,
            worktree_path=worktree_path,
        )
        commit_targets = sorted({
            str(item or "").strip()
            for item in (covered_commit_shas or [commit_sha])
            if str(item or "").strip()
        })
        if not commit_targets:
            commit_targets = [commit_sha]
        pending_evidence = {
            "source": "graph_snapshot_finalizer",
            "actor": actor,
            "snapshot_id": snapshot_id,
            "ref_name": identity["ref_name"],
            "branch_ref": identity["branch_ref"],
            "worktree_id": identity["worktree_id"],
            "worktree_path": identity["worktree_path"],
            "covered_commit_shas": commit_targets,
            **(evidence or {}),
        }
        placeholders = ",".join("?" for _ in commit_targets)
        cur = conn.execute(
            f"""
            UPDATE pending_scope_reconcile
            SET status = ?,
                snapshot_id = ?,
                evidence_json = ?
            WHERE project_id = ?
              AND ref_name = ?
              AND worktree_id = ?
              AND commit_sha IN ({placeholders})
              AND status IN (?, ?, ?)
            """,
            (
                PENDING_STATUS_MATERIALIZED,
                snapshot_id,
                _json(pending_evidence),
                project_id,
                identity["ref_name"],
                identity["worktree_id"],
                *commit_targets,
                PENDING_STATUS_QUEUED,
                PENDING_STATUS_RUNNING,
                PENDING_STATUS_FAILED,
            ),
        )
        materialized_count = int(cur.rowcount or 0)
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "commit_sha": commit_sha,
        "activation": activation,
        "pending_materialized_count": materialized_count,
        "ref_name": ref_name,
    }


def get_active_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    ref_name: str = "active",
) -> dict[str, Any] | None:
    ensure_schema(conn)
    row = conn.execute(
        """
        SELECT s.*
        FROM graph_snapshot_refs r
        JOIN graph_snapshots s
          ON s.project_id = r.project_id AND s.snapshot_id = r.snapshot_id
        WHERE r.project_id = ? AND r.ref_name = ?
        """,
        (project_id, ref_name),
    ).fetchone()
    return dict(row) if row else None


def get_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> dict[str, Any] | None:
    ensure_schema(conn)
    row = conn.execute(
        "SELECT * FROM graph_snapshots WHERE project_id = ? AND snapshot_id = ?",
        (project_id, snapshot_id),
    ).fetchone()
    return dict(row) if row else None


def list_graph_snapshots(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    statuses: Iterable[str] | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id]
    sql = "SELECT * FROM graph_snapshots WHERE project_id = ?"
    status_values = [str(s) for s in statuses or [] if s]
    if status_values:
        placeholders = ",".join("?" for _ in status_values)
        sql += f" AND status IN ({placeholders})"
        params.extend(status_values)
    sql += " ORDER BY created_at DESC, snapshot_id DESC LIMIT ?"
    params.append(max(1, min(int(limit or 50), 500)))
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def list_graph_snapshots_for_commit(
    conn: sqlite3.Connection,
    project_id: str,
    commit_sha: str,
    *,
    statuses: Iterable[str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id, commit_sha]
    sql = "SELECT * FROM graph_snapshots WHERE project_id = ? AND commit_sha = ?"
    status_values = [str(s) for s in statuses or [] if s]
    if status_values:
        placeholders = ",".join("?" for _ in status_values)
        sql += f" AND status IN ({placeholders})"
        params.extend(status_values)
    sql += """
        ORDER BY
          CASE status
            WHEN 'active' THEN 0
            WHEN 'superseded' THEN 1
            WHEN 'candidate' THEN 2
            WHEN 'finalizing' THEN 3
            ELSE 4
          END,
          created_at DESC,
          snapshot_id DESC
        LIMIT ?
    """
    params.append(max(1, min(int(limit or 20), 100)))
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def get_graph_snapshot_for_commit(
    conn: sqlite3.Connection,
    project_id: str,
    commit_sha: str,
) -> dict[str, Any] | None:
    rows = list_graph_snapshots_for_commit(
        conn,
        project_id,
        commit_sha,
        statuses=[
            SNAPSHOT_STATUS_ACTIVE,
            SNAPSHOT_STATUS_SUPERSEDED,
            SNAPSHOT_STATUS_CANDIDATE,
            SNAPSHOT_STATUS_FINALIZING,
        ],
        limit=1,
    )
    return rows[0] if rows else None


def list_graph_snapshot_nodes(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    limit: int = 200,
    offset: int = 0,
    layer: str = "",
    kind: str = "",
    include_semantic: bool = True,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id, snapshot_id]
    semantic_join = include_semantic and _table_exists(conn, "graph_semantic_nodes")
    semantic_job_join = include_semantic and _table_exists(conn, "graph_semantic_jobs")
    select_columns = """
        n.node_id, n.layer, n.title, n.kind, n.primary_files_json,
        n.secondary_files_json, n.test_files_json, n.metadata_json
    """
    joins = ""
    if semantic_join:
        select_columns += """,
        s.status AS semantic_status,
        s.feature_hash AS semantic_feature_hash,
        s.file_hashes_json AS semantic_file_hashes_json,
        s.semantic_json AS semantic_json,
        s.feedback_round AS semantic_feedback_round,
        s.batch_index AS semantic_batch_index,
        s.updated_at AS semantic_updated_at
        """
        joins += """
        LEFT JOIN graph_semantic_nodes s
          ON s.project_id = n.project_id
         AND s.snapshot_id = n.snapshot_id
         AND s.node_id = n.node_id
        """
    if semantic_job_join:
        select_columns += """,
        j.status AS semantic_job_status,
        j.feature_hash AS semantic_job_feature_hash,
        j.attempt_count AS semantic_job_attempt_count,
        j.worker_id AS semantic_job_worker_id,
        j.claim_id AS semantic_job_claim_id,
        j.claimed_at AS semantic_job_claimed_at,
        j.lease_expires_at AS semantic_job_lease_expires_at,
        j.claimed_by AS semantic_job_claimed_by,
        j.last_error AS semantic_job_last_error,
        j.updated_at AS semantic_job_updated_at
        """
        joins += """
        LEFT JOIN graph_semantic_jobs j
          ON j.project_id = n.project_id
         AND j.snapshot_id = n.snapshot_id
         AND j.node_id = n.node_id
        """
    sql = f"""
        SELECT {select_columns}
        FROM graph_nodes_index n
        {joins}
        WHERE n.project_id = ? AND n.snapshot_id = ?
    """
    if layer:
        sql += " AND n.layer = ?"
        params.append(layer)
    if kind:
        sql += " AND n.kind = ?"
        params.append(kind)
    sql += " ORDER BY n.node_id LIMIT ? OFFSET ?"
    params.extend([max(1, min(int(limit or 200), 1000)), max(0, int(offset or 0))])
    rows = conn.execute(sql, params).fetchall()
    nodes: list[dict[str, Any]] = []
    for row in rows:
        node = {
            "node_id": row["node_id"],
            "layer": row["layer"],
            "title": row["title"],
            "kind": row["kind"],
            "primary_files": _decode_json(row["primary_files_json"], []),
            "secondary_files": _decode_json(row["secondary_files_json"], []),
            "test_files": _decode_json(row["test_files_json"], []),
            "metadata": _decode_json(row["metadata_json"], {}),
        }
        if include_semantic:
            node["semantic"] = _semantic_overlay_from_node_row(row)
        nodes.append(node)
    return nodes


def list_graph_snapshot_edges(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    limit: int = 500,
    offset: int = 0,
    edge_type: str = "",
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id, snapshot_id]
    sql = """
        SELECT src, dst, edge_type, direction, evidence_json
        FROM graph_edges_index
        WHERE project_id = ? AND snapshot_id = ?
    """
    if edge_type:
        sql += " AND edge_type = ?"
        params.append(edge_type)
    sql += " ORDER BY src, dst, edge_type LIMIT ? OFFSET ?"
    params.extend([max(1, min(int(limit or 500), 2000)), max(0, int(offset or 0))])
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "src": row["src"],
            "dst": row["dst"],
            "edge_type": row["edge_type"],
            "direction": row["direction"],
            "evidence": _decode_json(row["evidence_json"], {}),
        }
        for row in rows
    ]


def summarize_file_inventory_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Compact dashboard summary for snapshot file inventory rows."""
    row_list = list(rows)
    by_kind: dict[str, int] = {}
    by_scan_status: dict[str, int] = {}
    by_graph_status: dict[str, int] = {}
    by_decision: dict[str, int] = {}
    pending: list[str] = []
    for row in row_list:
        kind = str(row.get("file_kind") or "")
        scan = str(row.get("scan_status") or "")
        graph = str(row.get("graph_status") or "")
        decision = str(row.get("decision") or "")
        if kind:
            by_kind[kind] = by_kind.get(kind, 0) + 1
        if scan:
            by_scan_status[scan] = by_scan_status.get(scan, 0) + 1
        if graph:
            by_graph_status[graph] = by_graph_status.get(graph, 0) + 1
        if decision:
            by_decision[decision] = by_decision.get(decision, 0) + 1
        if scan in {"orphan", "pending_decision", "error"} or graph in {"unmapped", "error"}:
            path = str(row.get("path") or "")
            if path:
                pending.append(path)
    return {
        "total": len(row_list),
        "by_kind": dict(sorted(by_kind.items())),
        "by_scan_status": dict(sorted(by_scan_status.items())),
        "by_graph_status": dict(sorted(by_graph_status.items())),
        "by_decision": dict(sorted(by_decision.items())),
        "pending_count": len(pending),
        "pending_sample": pending[:25],
    }


def list_graph_snapshot_files(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    limit: int = 200,
    offset: int = 0,
    file_kind: str = "",
    scan_status: str = "",
    graph_status: str = "",
    decision: str = "",
    path_contains: str = "",
    sort: str = "",
) -> dict[str, Any]:
    """List file inventory rows stored with a snapshot companion artifact."""
    ensure_schema(conn)
    snapshot = get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    raw = _read_json_artifact(snapshot_companion_dir(project_id, snapshot_id) / "file_inventory.json", [])
    rows = [dict(row) for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []

    def _matches(row: dict[str, Any]) -> bool:
        if file_kind and str(row.get("file_kind") or "") != file_kind:
            return False
        if scan_status:
            row_scan_status = str(row.get("scan_status") or "")
            if row_scan_status != scan_status:
                return False
            if scan_status == "orphan" and (row.get("attached_node_ids") or row.get("attached_to")):
                return False
        if graph_status and str(row.get("graph_status") or "") != graph_status:
            return False
        if decision and str(row.get("decision") or "") != decision:
            return False
        if path_contains and path_contains not in str(row.get("path") or ""):
            return False
        return True

    filtered = [row for row in rows if _matches(row)]
    normalized_sort = str(sort or "").strip().lower().replace("-", "_")
    if normalized_sort:
        if normalized_sort in {"path", "path_asc"}:
            filtered = sorted(filtered, key=lambda row: str(row.get("path") or ""))
        elif normalized_sort == "size_desc":
            filtered = sorted(
                filtered,
                key=lambda row: (-int(row.get("size_bytes") or 0), str(row.get("path") or "")),
            )
        elif normalized_sort == "size_asc":
            filtered = sorted(
                filtered,
                key=lambda row: (int(row.get("size_bytes") or 0), str(row.get("path") or "")),
            )
        else:
            raise ValueError(f"unsupported file inventory sort: {sort}")
    start = max(0, int(offset or 0))
    end = start + max(1, min(int(limit or 200), 1000))
    return {
        "snapshot": snapshot,
        "summary": summarize_file_inventory_rows(rows),
        "total_count": len(rows),
        "filtered_count": len(filtered),
        "sort": normalized_sort,
        "files": filtered[start:end],
    }


def _count_rows(
    conn: sqlite3.Connection,
    table: str,
    project_id: str,
    snapshot_id: str,
) -> int:
    if not _table_exists(conn, table):
        return 0
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM {table} WHERE project_id = ? AND snapshot_id = ?",
        (project_id, snapshot_id),
    ).fetchone()
    return int(row["count"] if row else 0)


def _group_counts(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    project_id: str,
    snapshot_id: str,
) -> dict[str, int]:
    if not _table_exists(conn, table):
        return {}
    rows = conn.execute(
        f"""
        SELECT {column} AS key, COUNT(*) AS count
        FROM {table}
        WHERE project_id = ? AND snapshot_id = ?
        GROUP BY {column}
        ORDER BY {column}
        """,
        (project_id, snapshot_id),
    ).fetchall()
    return {str(row["key"] or ""): int(row["count"]) for row in rows if str(row["key"] or "")}


def _snapshot_notes(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    if not snapshot:
        return {}
    notes = _decode_json(snapshot.get("notes"), {})
    return notes if isinstance(notes, dict) else {}


def snapshot_materialization_provenance(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """Return checkout provenance and warnings recorded for a snapshot."""
    notes = _snapshot_notes(snapshot)
    provenance = notes.get("checkout_provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    raw_warnings = provenance.get("warnings") if isinstance(provenance, dict) else []
    warnings = [dict(item) for item in raw_warnings or [] if isinstance(item, dict)]
    return {
        "execution_root": provenance.get("execution_root", ""),
        "execution_root_role": provenance.get("execution_root_role", ""),
        "execution_root_is_ephemeral": bool(provenance.get("execution_root_is_ephemeral")),
        "canonical_project_identity": provenance.get("canonical_project_identity") or {},
        "git": provenance.get("git") or {},
        "warnings": warnings,
        "warning_count": len(warnings),
    }


def _latest_global_review_from_notes(notes: dict[str, Any]) -> dict[str, Any]:
    review_meta = notes.get("global_semantic_review")
    if not isinstance(review_meta, dict):
        return {}
    path = str(review_meta.get("latest_full_review_path") or "").strip()
    if not path:
        return {}
    payload = _read_json_artifact(Path(path), {})
    return payload if isinstance(payload, dict) else {}


def _as_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _node_metadata(node: dict[str, Any]) -> dict[str, Any]:
    metadata = node.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, Iterable):
        values = list(raw)
    else:
        values = [raw]
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip().replace("\\", "/")
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def _is_governed_feature_node(node: dict[str, Any]) -> bool:
    if str(node.get("layer") or "").upper() != "L7":
        return False
    metadata = _node_metadata(node)
    if metadata.get("exclude_as_feature") is True:
        return False
    file_role = str(metadata.get("file_role") or node.get("kind") or "").strip().lower()
    if file_role in {"package_marker", "type_contract", "entrypoint_support"}:
        return False
    return True


def _feature_coverage_picture(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    raw_features = [
        node for node in nodes
        if str(node.get("layer") or "").upper() == "L7"
    ]
    governed_features = [node for node in raw_features if _is_governed_feature_node(node)]
    doc_bound = sum(1 for node in governed_features if _string_list(node.get("secondary_files")))
    test_bound = sum(1 for node in governed_features if _string_list(node.get("test_files")))
    config_bound = sum(
        1 for node in governed_features
        if _string_list(_node_metadata(node).get("config_files"))
    )
    return {
        "raw_feature_count": len(raw_features),
        "governed_feature_count": len(governed_features),
        "excluded_feature_count": max(0, len(raw_features) - len(governed_features)),
        "doc_bound_count": doc_bound,
        "doc_coverage_ratio": _ratio(doc_bound, len(governed_features)),
        "test_bound_count": test_bound,
        "test_coverage_ratio": _ratio(test_bound, len(governed_features)),
        "config_bound_count": config_bound,
        "config_coverage_ratio": _ratio(config_bound, len(governed_features)),
    }


def _l4_asset_picture(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    l4_nodes = [
        node for node in nodes
        if str(node.get("layer") or "").upper() == "L4"
    ]
    by_kind: dict[str, int] = {}
    by_file_role: dict[str, int] = {}
    aggregate_count = 0
    no_primary_count = 0
    for node in l4_nodes:
        metadata = _node_metadata(node)
        kind = str(node.get("kind") or metadata.get("kind") or "asset").strip() or "asset"
        role = str(metadata.get("file_role") or "asset").strip() or "asset"
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_file_role[role] = by_file_role.get(role, 0) + 1
        if metadata.get("aggregate_asset") is True:
            aggregate_count += 1
        if not _string_list(node.get("primary_files")):
            no_primary_count += 1
    return {
        "score_version": "l4_asset_contract_v1_role_aware",
        "score": 100.0,
        "asset_count": len(l4_nodes),
        "aggregate_asset_count": aggregate_count,
        "no_primary_asset_count": no_primary_count,
        "by_kind": dict(sorted(by_kind.items())),
        "by_file_role": dict(sorted(by_file_role.items())),
        "policy": "L4 nodes are state/contract/asset nodes; direct files may be intentionally empty and are not scored as L7 feature coverage gaps.",
    }


def _structure_health_picture(
    *,
    nodes: list[dict[str, Any]],
    counts: dict[str, Any],
    file_summary: dict[str, Any],
    graph_corrections: dict[str, Any],
) -> dict[str, Any]:
    coverage = _feature_coverage_picture(nodes)
    feature_count = int(coverage["governed_feature_count"])
    missing_docs = max(0, feature_count - int(coverage["doc_bound_count"]))
    missing_tests = max(0, feature_count - int(coverage["test_bound_count"]))
    file_total = max(1, int(counts.get("files") or 0))
    orphan_files = int(counts.get("orphan_files") or 0)
    pending_files = int(counts.get("pending_decision_files") or 0)
    cleanup_candidates = int(counts.get("cleanup_candidates") or 0)
    proposed_patches = int(graph_corrections.get("proposed_count") or 0)
    high_risk_patches = int(graph_corrections.get("high_risk_proposed_count") or 0)

    coverage_penalty = 0.0
    if feature_count:
        coverage_penalty = ((missing_docs * 6.0) + (missing_tests * 8.0)) / feature_count
    file_penalty = min(
        12.0,
        ((orphan_files * 4.0) + (pending_files * 0.5) + (cleanup_candidates * 0.5)) / file_total * 100.0,
    )
    correction_penalty = min(8.0, proposed_patches * 1.5 + high_risk_patches * 3.0)
    score = round(max(0.0, 100.0 - coverage_penalty - file_penalty - correction_penalty), 2)
    return {
        "score_version": "structure_health_v1_algorithmic_coverage_inventory",
        "score": score,
        "status": "current",
        **coverage,
        "file_hygiene": {
            "total_files": counts.get("files", 0),
            "orphan_files": orphan_files,
            "pending_decision_files": pending_files,
            "cleanup_candidates": cleanup_candidates,
            "summary": file_summary,
        },
        "graph_correction_patches": {
            "proposed_count": proposed_patches,
            "high_risk_proposed_count": high_risk_patches,
        },
        "penalties": {
            "coverage": round(coverage_penalty, 2),
            "file_hygiene": round(file_penalty, 2),
            "graph_corrections": round(correction_penalty, 2),
        },
        "l4_asset_health": _l4_asset_picture(nodes),
    }


def _latest_projection_health(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> dict[str, Any]:
    if not _table_exists(conn, "graph_semantic_projections"):
        return {}
    try:
        row = conn.execute(
            """
            SELECT projection_id, health_json, created_at
            FROM graph_semantic_projections
            WHERE project_id = ? AND snapshot_id = ?
            ORDER BY event_watermark DESC, created_at DESC
            LIMIT 1
            """,
            (project_id, snapshot_id),
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    health = _decode_json(row["health_json"], {})
    if not isinstance(health, dict):
        health = {}
    return {
        **health,
        "projection_id": row["projection_id"],
        "projection_created_at": row["created_at"],
    }


def _semantic_health_picture(
    *,
    projection_health: dict[str, Any],
    legacy_health: dict[str, Any],
    review_meta: dict[str, Any],
) -> dict[str, Any]:
    if projection_health:
        score = _as_float(projection_health.get("project_health_score"), None)
        return {
            "score_version": projection_health.get("score_version") or "semantic_projection",
            "score": score,
            "status": "current",
            "source": "semantic_projection",
            "projection_id": projection_health.get("projection_id", ""),
            "feature_count": projection_health.get("feature_count"),
            "semantic_current_count": projection_health.get("semantic_current_count"),
            "semantic_missing_count": projection_health.get("semantic_missing_count"),
            "semantic_stale_count": projection_health.get("semantic_stale_count"),
            "semantic_unverified_hash_count": projection_health.get("semantic_unverified_hash_count"),
            "semantic_current_ratio": projection_health.get("semantic_current_ratio"),
            "semantic_trusted_count": projection_health.get("semantic_trusted_count"),
            "semantic_trusted_ratio": projection_health.get("semantic_trusted_ratio"),
            "semantic_review_debt_count": projection_health.get("semantic_review_debt_count"),
            "semantic_review_debt_ratio": projection_health.get("semantic_review_debt_ratio"),
            "doc_coverage_ratio": projection_health.get("doc_coverage_ratio"),
            "test_coverage_ratio": projection_health.get("test_coverage_ratio"),
            "semantic_debt_penalty": projection_health.get("semantic_debt_penalty"),
            "binding_context_penalty": projection_health.get("binding_context_penalty"),
            "open_issue_penalty": projection_health.get("open_issue_penalty"),
            "semantic_open_issue_count": projection_health.get("semantic_open_issue_count"),
            "low_health_count": projection_health.get("low_health_count"),
            "edge_semantic_eligible_count": projection_health.get("edge_semantic_eligible_count"),
            "edge_semantic_requested_count": projection_health.get("edge_semantic_requested_count"),
            "edge_semantic_current_count": projection_health.get("edge_semantic_current_count"),
            "edge_semantic_rule_count": projection_health.get("edge_semantic_rule_count"),
            "edge_semantic_missing_count": projection_health.get("edge_semantic_missing_count"),
            "edge_semantic_unqueued_count": projection_health.get("edge_semantic_unqueued_count"),
            "edge_semantic_needs_ai_count": projection_health.get("edge_semantic_needs_ai_count"),
            "edge_semantic_payload_current_count": projection_health.get("edge_semantic_payload_current_count"),
            "edge_semantic_coverage_ratio": projection_health.get("edge_semantic_coverage_ratio"),
            "edge_semantic_payload_coverage_ratio": projection_health.get("edge_semantic_payload_coverage_ratio"),
        }
    coverage = legacy_health.get("semantic_coverage_ratio")
    if coverage is None:
        coverage = review_meta.get("latest_full_semantic_coverage_ratio")
    if coverage is not None:
        return {
            "score_version": "semantic_metadata_fallback_v1",
            "score": _as_float(legacy_health.get("governance_observability_score"), None),
            "status": "metadata_only",
            "source": "snapshot_notes",
            "semantic_current_ratio": coverage,
            "semantic_coverage_ratio": coverage,
        }
    return {
        "score_version": "semantic_health_v1",
        "score": None,
        "status": "pending",
        "source": "none",
    }


def _project_insight_health_picture(
    *,
    latest_review: dict[str, Any],
    review_meta: dict[str, Any],
) -> dict[str, Any]:
    health = latest_review.get("health_picture") if isinstance(latest_review, dict) else {}
    if isinstance(health, dict) and health:
        file_hygiene = health.get("file_hygiene") if isinstance(health.get("file_hygiene"), dict) else {}
        return {
            "score_version": "project_insight_health_v1_global_review",
            "score": _as_float(health.get("project_health_score"), None),
            "status": "reviewed",
            "source": "global_semantic_review",
            "latest_run_id": review_meta.get("latest_full_run_id", ""),
            "latest_status": review_meta.get("latest_full_status", ""),
            "low_health_count": health.get("low_health_count"),
            "issue_counts": health.get("project_health_issue_counts", {}),
            "file_hygiene_score": health.get("file_hygiene_score"),
            "file_hygiene": {
                "available": bool(file_hygiene.get("available")),
                "run_id": file_hygiene.get("run_id", ""),
                "total_files": file_hygiene.get("total_files"),
                "review_required_count": file_hygiene.get("review_required_count"),
                "orphan_count": file_hygiene.get("orphan_count"),
                "pending_decision_count": file_hygiene.get("pending_decision_count"),
                "error_count": file_hygiene.get("error_count"),
                "cleanup_candidate_count": file_hygiene.get("cleanup_candidate_count"),
                "cleanup_candidate_bytes": file_hygiene.get("cleanup_candidate_bytes"),
                "cleanup_candidate_mb": file_hygiene.get("cleanup_candidate_mb"),
                "by_kind": file_hygiene.get("by_kind", {}),
                "by_scan_status": file_hygiene.get("by_scan_status", {}),
                "by_graph_status": file_hygiene.get("by_graph_status", {}),
                "review_required_sample": file_hygiene.get("review_required_sample", []),
                "cleanup_candidate_sample": file_hygiene.get("cleanup_candidate_sample", []),
            },
        }
    if review_meta:
        return {
            "score_version": "project_insight_health_v1_global_review",
            "score": None,
            "status": "metadata_only",
            "source": "snapshot_notes",
            "latest_run_id": review_meta.get("latest_full_run_id", ""),
            "latest_status": review_meta.get("latest_full_status", ""),
        }
    return {
        "score_version": "project_insight_health_v1_global_review",
        "score": None,
        "status": "pending",
        "source": "none",
    }


def _legacy_health_from_review(
    latest_review: dict[str, Any],
    review_meta: dict[str, Any],
) -> dict[str, Any]:
    health = latest_review.get("health_picture") if isinstance(latest_review, dict) else {}
    if not isinstance(health, dict):
        health = {}
    return {
        "project_health_score": health.get("project_health_score"),
        "raw_project_health_score": health.get("raw_project_health_score"),
        "file_hygiene_score": health.get("file_hygiene_score"),
        "artifact_binding_score": health.get("artifact_binding_score"),
        "governance_observability_score": health.get("governance_observability_score"),
        "doc_coverage_ratio": health.get("doc_coverage_ratio"),
        "test_coverage_ratio": health.get("test_coverage_ratio"),
        "semantic_coverage_ratio": (
            health.get("semantic_coverage_ratio")
            if health.get("semantic_coverage_ratio") is not None
            else review_meta.get("latest_full_semantic_coverage_ratio")
        ),
    }


def _health_from_snapshot_notes(notes: dict[str, Any]) -> dict[str, Any]:
    latest_review = _latest_global_review_from_notes(notes)
    review_meta = notes.get("global_semantic_review") if isinstance(notes.get("global_semantic_review"), dict) else {}
    return _legacy_health_from_review(latest_review, review_meta)


def _dashboard_health(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    nodes: list[dict[str, Any]],
    counts: dict[str, Any],
    file_summary: dict[str, Any],
    graph_corrections: dict[str, Any],
    notes: dict[str, Any],
) -> dict[str, Any]:
    latest_review = _latest_global_review_from_notes(notes)
    review_meta = notes.get("global_semantic_review") if isinstance(notes.get("global_semantic_review"), dict) else {}
    legacy = _legacy_health_from_review(latest_review, review_meta)
    structure = _structure_health_picture(
        nodes=nodes,
        counts=counts,
        file_summary=file_summary,
        graph_corrections=graph_corrections,
    )
    projection = _latest_projection_health(conn, project_id, snapshot_id)
    semantic = _semantic_health_picture(
        projection_health=projection,
        legacy_health=legacy,
        review_meta=review_meta,
    )
    insight = _project_insight_health_picture(
        latest_review=latest_review,
        review_meta=review_meta,
    )
    project_score = (
        legacy.get("project_health_score")
        if legacy.get("project_health_score") is not None
        else structure.get("score")
        if structure.get("score") is not None
        else semantic.get("score")
    )
    return {
        **legacy,
        "project_health_score": project_score,
        "structure_health_score": structure.get("score"),
        "semantic_health_score": semantic.get("score"),
        "project_insight_health_score": insight.get("score"),
        "structure_health": structure,
        "semantic_health": semantic,
        "project_insight_health": insight,
    }


def _semantic_counts(conn: sqlite3.Connection, project_id: str, snapshot_id: str) -> dict[str, Any]:
    return {
        "nodes_by_status": _group_counts(conn, "graph_semantic_nodes", "status", project_id, snapshot_id),
        "jobs_by_status": _group_counts(conn, "graph_semantic_jobs", "status", project_id, snapshot_id),
        "semantic_node_count": _count_rows(conn, "graph_semantic_nodes", project_id, snapshot_id),
        "semantic_job_count": _count_rows(conn, "graph_semantic_jobs", project_id, snapshot_id),
    }


def summarize_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> dict[str, Any]:
    """Return a compact dashboard-safe summary for one graph snapshot."""
    ensure_schema(conn)
    snapshot = get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")

    nodes_by_layer = _group_counts(conn, "graph_nodes_index", "layer", project_id, snapshot_id)
    edges_by_type = _group_counts(conn, "graph_edges_index", "edge_type", project_id, snapshot_id)
    semantic = _semantic_counts(conn, project_id, snapshot_id)
    try:
        from .graph_correction_patches import correction_patch_summary

        graph_corrections = correction_patch_summary(conn, project_id)
    except Exception:
        graph_corrections = {
            "total": 0,
            "by_status": {},
            "by_type": {},
            "by_risk": {},
            "last_apply_status": {},
            "proposed_count": 0,
            "accepted_count": 0,
            "rejected_count": 0,
            "stale_count": 0,
            "replayable_count": 0,
            "high_risk_proposed_count": 0,
        }
    try:
        files = list_graph_snapshot_files(conn, project_id, snapshot_id, limit=1)
        file_summary = files["summary"]
        file_total = int(files["total_count"])
    except Exception:
        file_summary = {}
        file_total = 0
    try:
        summary_nodes = list_graph_snapshot_nodes(
            conn,
            project_id,
            snapshot_id,
            limit=100000,
            include_semantic=False,
        )
    except Exception:
        summary_nodes = []

    notes = _snapshot_notes(snapshot)
    semantic_state = {}
    semantic_enrichment = notes.get("semantic_enrichment")
    if isinstance(semantic_enrichment, dict):
        semantic_state = semantic_enrichment.get("semantic_graph_state") or {}
        if not isinstance(semantic_state, dict):
            semantic_state = {}

    by_scan = file_summary.get("by_scan_status", {}) if isinstance(file_summary, dict) else {}
    by_kind = file_summary.get("by_kind", {}) if isinstance(file_summary, dict) else {}
    counts = {
        "nodes": _count_rows(conn, "graph_nodes_index", project_id, snapshot_id),
        "nodes_by_layer": nodes_by_layer,
        "edges": _count_rows(conn, "graph_edges_index", project_id, snapshot_id),
        "edges_by_type": edges_by_type,
        "features": int(nodes_by_layer.get("L7", 0)),
        "files": file_total,
        "orphan_files": int(by_scan.get("orphan", 0)),
        "pending_decision_files": int(by_scan.get("pending_decision", 0)),
        "cleanup_candidates": int(by_kind.get("generated", 0)),
        "ai_review_feedback": int(semantic_state.get("open_issue_count") or 0),
    }
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "commit_sha": snapshot["commit_sha"],
        "snapshot_kind": snapshot["snapshot_kind"],
        "snapshot_status": snapshot["status"],
        "created_at": snapshot["created_at"],
        "created_by": snapshot.get("created_by", ""),
        "graph_sha256": snapshot.get("graph_sha256", ""),
        "inventory_sha256": snapshot.get("inventory_sha256", ""),
        "drift_sha256": snapshot.get("drift_sha256", ""),
        "counts": counts,
        "health": _dashboard_health(
            conn,
            project_id,
            snapshot_id,
            nodes=summary_nodes,
            counts=counts,
            file_summary=file_summary,
            graph_corrections=graph_corrections,
            notes=notes,
        ),
        "semantic": semantic,
        "graph_correction_patches": graph_corrections,
        "file_inventory_summary": file_summary,
    }


def _backlog_counts_for_commits(
    conn: sqlite3.Connection,
    commits: Iterable[str],
) -> dict[str, dict[str, int]]:
    selected = [str(commit or "").strip() for commit in commits if str(commit or "").strip()]
    if not selected or not _table_exists(conn, "backlog_bugs"):
        return {}
    placeholders = ",".join("?" for _ in selected)
    try:
        rows = conn.execute(
            f"""
            SELECT "commit" AS commit_sha, status, mf_type, COUNT(*) AS count
            FROM backlog_bugs
            WHERE "commit" IN ({placeholders})
            GROUP BY "commit", status, mf_type
            """,
            selected,
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, dict[str, int]] = {
        commit: {"total": 0, "open": 0, "fixed": 0, "manual_fix": 0, "chain": 0}
        for commit in selected
    }
    for row in rows:
        commit = str(row["commit_sha"] or "")
        status = str(row["status"] or "").lower()
        mf_type = str(row["mf_type"] or "")
        count = int(row["count"] or 0)
        bucket = out.setdefault(commit, {"total": 0, "open": 0, "fixed": 0, "manual_fix": 0, "chain": 0})
        bucket["total"] += count
        if status == "open":
            bucket["open"] += count
        if status == "fixed":
            bucket["fixed"] += count
        if mf_type:
            bucket["manual_fix"] += count
        else:
            bucket["chain"] += count
    return out


def _pending_by_commit(conn: sqlite3.Connection, project_id: str) -> dict[str, dict[str, Any]]:
    pending = list_pending_scope_reconcile(
        conn,
        project_id,
        statuses=[PENDING_STATUS_QUEUED, PENDING_STATUS_RUNNING, PENDING_STATUS_FAILED],
        ref_name="active",
    )
    return {str(row.get("commit_sha") or ""): row for row in pending}


def list_commit_timeline(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    limit: int = 50,
    include_backlog: bool = True,
) -> list[dict[str, Any]]:
    """Return latest snapshot-backed commits for commit-anchored dashboard navigation."""
    snapshots = list_graph_snapshots(conn, project_id, limit=limit)
    active = get_active_graph_snapshot(conn, project_id)
    active_snapshot_id = str(active.get("snapshot_id") or "") if active else ""
    pending = _pending_by_commit(conn, project_id)
    by_commit: dict[str, dict[str, Any]] = {}
    for snapshot in snapshots:
        commit = str(snapshot.get("commit_sha") or "")
        if not commit:
            continue
        existing = by_commit.get(commit)
        if existing and existing.get("snapshot_status") == SNAPSHOT_STATUS_ACTIVE:
            existing["snapshot_count"] += 1
            continue
        if existing and snapshot.get("status") != SNAPSHOT_STATUS_ACTIVE:
            existing["snapshot_count"] += 1
            continue
        summary = summarize_graph_snapshot(conn, project_id, snapshot["snapshot_id"])
        by_commit[commit] = {
            "commit_sha": commit,
            "short_sha": commit[:7],
            "subject": "",
            "created_at": snapshot.get("created_at", ""),
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_kind": snapshot["snapshot_kind"],
            "snapshot_status": snapshot["status"],
            "snapshot_count": int((existing or {}).get("snapshot_count") or 0) + 1,
            "graph_resolution": "exact",
            "is_active": snapshot["snapshot_id"] == active_snapshot_id,
            "pending_scope_reconcile": commit in pending,
            "pending_scope_status": pending.get(commit, {}).get("status", ""),
            "counts": summary["counts"],
            "health": summary["health"],
        }
    if include_backlog:
        backlog = _backlog_counts_for_commits(conn, by_commit.keys())
        for commit, row in by_commit.items():
            row["backlog"] = backlog.get(commit, {"total": 0, "open": 0, "fixed": 0, "manual_fix": 0, "chain": 0})
    return list(by_commit.values())[: max(1, min(int(limit or 50), 500))]


def resolve_commit_graph_state(
    conn: sqlite3.Connection,
    project_id: str,
    commit_sha: str,
) -> dict[str, Any]:
    """Resolve a commit to the graph snapshot dashboard should display."""
    ensure_schema(conn)
    commit_sha = str(commit_sha or "").strip()
    if not commit_sha:
        raise ValueError("commit_sha is required")

    active = get_active_graph_snapshot(conn, project_id)
    active_snapshot_id = str(active.get("snapshot_id") or "") if active else ""
    exact = get_graph_snapshot_for_commit(conn, project_id, commit_sha)
    pending_rows = list_pending_scope_reconcile(conn, project_id, commit_shas=[commit_sha], ref_name="active")
    pending_active = [
        row for row in pending_rows
        if row.get("status") in {PENDING_STATUS_QUEUED, PENDING_STATUS_RUNNING, PENDING_STATUS_FAILED}
    ]
    if exact:
        return {
            "project_id": project_id,
            "commit_sha": commit_sha,
            "resolved_snapshot_id": exact["snapshot_id"],
            "resolution": "exact",
            "snapshot_status": exact["status"],
            "snapshot_kind": exact["snapshot_kind"],
            "has_graph": True,
            "has_semantic_review": bool(_snapshot_notes(exact).get("global_semantic_review")),
            "pending_scope_reconcile": bool(pending_active),
            "pending_scope_status": pending_active[0]["status"] if pending_active else "",
            "is_active": exact["snapshot_id"] == active_snapshot_id,
            "warnings": [],
        }
    if pending_active:
        return {
            "project_id": project_id,
            "commit_sha": commit_sha,
            "resolved_snapshot_id": "",
            "resolution": "pending",
            "snapshot_status": "",
            "snapshot_kind": "",
            "has_graph": False,
            "has_semantic_review": False,
            "pending_scope_reconcile": True,
            "pending_scope_status": pending_active[0]["status"],
            "is_active": False,
            "warnings": ["scope reconcile is pending for this commit"],
        }
    if active:
        return {
            "project_id": project_id,
            "commit_sha": commit_sha,
            "resolved_snapshot_id": active["snapshot_id"],
            "resolution": "advisory_latest",
            "snapshot_status": active["status"],
            "snapshot_kind": active["snapshot_kind"],
            "has_graph": True,
            "has_semantic_review": bool(_snapshot_notes(active).get("global_semantic_review")),
            "pending_scope_reconcile": False,
            "pending_scope_status": "",
            "is_active": True,
            "warnings": ["no exact graph snapshot for commit; showing latest active graph as advisory context"],
        }
    return {
        "project_id": project_id,
        "commit_sha": commit_sha,
        "resolved_snapshot_id": "",
        "resolution": "missing",
        "snapshot_status": "",
        "snapshot_kind": "",
        "has_graph": False,
        "has_semantic_review": False,
        "pending_scope_reconcile": False,
        "pending_scope_status": "",
        "is_active": False,
        "warnings": ["no graph snapshot is available"],
    }


def export_graph_snapshot_cache(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    project_root: str | Path,
    cache_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Export a non-authoritative graph cache into a project's .aming-claw/cache."""
    ensure_schema(conn)
    snapshot = get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    graph_path = snapshot_graph_path(project_id, snapshot_id)
    graph_json = _read_json_artifact(graph_path, {})
    if not isinstance(graph_json, dict) or not graph_json:
        raise ValueError(f"snapshot graph companion is empty or unreadable: {graph_path}")

    root = Path(project_root).resolve()
    base = Path(cache_dir).resolve() if cache_dir else root / ".aming-claw" / "cache"
    base.mkdir(parents=True, exist_ok=True)
    out_graph = base / "graph.current.json"
    out_manifest = base / "graph.current.manifest.json"
    graph_bytes = (
        json.dumps(graph_json, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n"
    ).encode("utf-8")
    graph_sha = _sha256_bytes(graph_bytes)
    out_graph.write_bytes(graph_bytes)
    manifest = {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "commit_sha": snapshot["commit_sha"],
        "snapshot_kind": snapshot["snapshot_kind"],
        "exported_at": utc_now(),
        "non_authoritative": True,
        "source_graph_sha256": snapshot["graph_sha256"],
        "export_graph_sha256": graph_sha,
        "graph_path": str(out_graph),
    }
    out_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "commit_sha": snapshot["commit_sha"],
        "cache_dir": str(base),
        "graph_path": str(out_graph),
        "manifest_path": str(out_manifest),
        "manifest": manifest,
    }


def abandon_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    actor: str = "observer",
    reason: str = "",
) -> dict[str, Any]:
    ensure_schema(conn)
    row = get_graph_snapshot(conn, project_id, snapshot_id)
    if not row:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    if row["status"] == SNAPSHOT_STATUS_ACTIVE:
        raise ValueError("active graph snapshot cannot be abandoned")
    if row["status"] == SNAPSHOT_STATUS_SUPERSEDED:
        raise ValueError("superseded graph snapshot cannot be abandoned")
    notes = _decode_json(row.get("notes"), {})
    if not isinstance(notes, dict):
        notes = {"previous_notes": row.get("notes") or ""}
    notes["abandoned"] = {
        "actor": actor,
        "reason": reason,
        "ts": utc_now(),
    }
    conn.execute(
        "UPDATE graph_snapshots SET status = ?, notes = ? WHERE project_id = ? AND snapshot_id = ?",
        (SNAPSHOT_STATUS_ABANDONED, _json(notes), project_id, snapshot_id),
    )
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "previous_status": row["status"],
        "status": SNAPSHOT_STATUS_ABANDONED,
    }


def get_latest_scan_baseline(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    try:
        row = conn.execute(
            """
            SELECT baseline_id, chain_version, scope_value, created_at
            FROM version_baselines
            WHERE project_id = ? AND scope_kind = 'commit_sweep'
            ORDER BY baseline_id DESC LIMIT 1
            """,
            (project_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def list_pending_scope_reconcile(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    statuses: Iterable[str] | None = None,
    commit_shas: Iterable[str] | None = None,
    ref_name: str | None = None,
    branch_ref: str | None = None,
    worktree_id: str | None = None,
    worktree_path: str | None = None,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id]
    sql = "SELECT * FROM pending_scope_reconcile WHERE project_id = ?"
    status_values = [str(s) for s in statuses or [] if s]
    if status_values:
        placeholders = ",".join("?" for _ in status_values)
        sql += f" AND status IN ({placeholders})"
        params.extend(status_values)
    commit_values = [str(s) for s in commit_shas or [] if s]
    if commit_values:
        placeholders = ",".join("?" for _ in commit_values)
        sql += f" AND commit_sha IN ({placeholders})"
        params.extend(commit_values)
    if any(value is not None for value in (ref_name, branch_ref, worktree_id, worktree_path)):
        identity = normalize_pending_scope_identity(
            ref_name=str(ref_name or ""),
            branch_ref=str(branch_ref or ""),
            worktree_id=str(worktree_id or ""),
            worktree_path=str(worktree_path or ""),
        )
        sql += " AND ref_name = ? AND worktree_id = ?"
        params.extend([identity["ref_name"], identity["worktree_id"]])
        if branch_ref is not None:
            sql += " AND branch_ref = ?"
            params.append(identity["branch_ref"])
    sql += " ORDER BY queued_at, ref_name, worktree_id, commit_sha"
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def graph_governance_status(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    active = get_active_graph_snapshot(conn, project_id)
    materialization = snapshot_materialization_provenance(active)
    scan = get_latest_scan_baseline(conn, project_id)
    pending = list_pending_scope_reconcile(
        conn,
        project_id,
        statuses=[
            PENDING_STATUS_QUEUED,
            PENDING_STATUS_RUNNING,
            PENDING_STATUS_FAILED,
        ],
    )
    return {
        "project_id": project_id,
        "active_snapshot_id": active.get("snapshot_id") if active else "",
        "graph_snapshot_commit": active.get("commit_sha") if active else "",
        "materialized_graph_baseline_commit": active.get("commit_sha") if active else "",
        "active_snapshot_materialization": materialization,
        "active_snapshot_warnings": materialization.get("warnings") or [],
        "scan_baseline_commit": scan.get("chain_version") if scan else "",
        "scan_baseline_id": scan.get("baseline_id") if scan else None,
        "pending_scope_reconcile_count": len(pending),
        "pending_scope_reconcile": pending,
    }


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def record_reconcile_run_metric(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    run_id: str,
    snapshot_id: str,
    commit_sha: str = "",
    parent_commit_sha: str = "",
    snapshot_kind: str = "",
    strategy: str = "",
    graph_delta_mode: str = "",
    status: str = "",
    changed_file_count: int = 0,
    impacted_file_count: int = 0,
    event_count: int = 0,
    node_count: int = 0,
    edge_count: int = 0,
    elapsed_ms: int = 0,
    trace_summary_path: str = "",
    fallback_reason: str = "",
    evidence: dict[str, Any] | None = None,
    created_at: str = "",
) -> dict[str, Any]:
    """Persist one reconcile timing row.

    The primary key is run_id + snapshot_id so fallback metadata can be
    upserted after graph events are emitted.
    """
    ensure_schema(conn)
    rid = str(run_id or snapshot_id or commit_sha or "").strip()
    sid = str(snapshot_id or "").strip()
    if not rid or not sid:
        raise ValueError("reconcile metric requires run_id and snapshot_id")
    now = created_at or utc_now()
    payload = _json(evidence or {})
    conn.execute(
        """
        INSERT INTO reconcile_run_metrics
          (project_id, run_id, snapshot_id, commit_sha, parent_commit_sha,
           snapshot_kind, strategy, graph_delta_mode, status,
           changed_file_count, impacted_file_count, event_count,
           node_count, edge_count, elapsed_ms, trace_summary_path,
           fallback_reason, created_at, evidence_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id, run_id, snapshot_id) DO UPDATE SET
          commit_sha = excluded.commit_sha,
          parent_commit_sha = excluded.parent_commit_sha,
          snapshot_kind = excluded.snapshot_kind,
          strategy = excluded.strategy,
          graph_delta_mode = excluded.graph_delta_mode,
          status = excluded.status,
          changed_file_count = excluded.changed_file_count,
          impacted_file_count = excluded.impacted_file_count,
          event_count = excluded.event_count,
          node_count = excluded.node_count,
          edge_count = excluded.edge_count,
          elapsed_ms = excluded.elapsed_ms,
          trace_summary_path = excluded.trace_summary_path,
          fallback_reason = excluded.fallback_reason,
          evidence_json = excluded.evidence_json
        """,
        (
            project_id,
            rid,
            sid,
            str(commit_sha or ""),
            str(parent_commit_sha or ""),
            str(snapshot_kind or ""),
            str(strategy or ""),
            str(graph_delta_mode or ""),
            str(status or ""),
            int(changed_file_count or 0),
            int(impacted_file_count or 0),
            int(event_count or 0),
            int(node_count or 0),
            int(edge_count or 0),
            int(elapsed_ms or 0),
            str(trace_summary_path or ""),
            str(fallback_reason or ""),
            now,
            payload,
        ),
    )
    row = conn.execute(
        """
        SELECT * FROM reconcile_run_metrics
        WHERE project_id=? AND run_id=? AND snapshot_id=?
        """,
        (project_id, rid, sid),
    ).fetchone()
    return dict(row)


def _metric_from_snapshot_row(row: sqlite3.Row) -> dict[str, Any] | None:
    snapshot = dict(row)
    notes = _decode_json(snapshot.get("notes"), {})
    if not isinstance(notes, dict):
        return None
    scope_delta = notes.get("scope_file_delta")
    if not isinstance(scope_delta, dict):
        pending = notes.get("pending_scope_reconcile")
        scope_delta = pending.get("scope_file_delta") if isinstance(pending, dict) else {}
    if not isinstance(scope_delta, dict):
        scope_delta = {}
    pending_notes = notes.get("pending_scope_reconcile") if isinstance(notes.get("pending_scope_reconcile"), dict) else {}
    event_summary = pending_notes.get("scope_graph_events") if isinstance(pending_notes, dict) else {}
    if not isinstance(event_summary, dict):
        event_summary = {}
    graph_stats: dict[str, Any] = {}
    graph_path = snapshot_graph_path(str(snapshot.get("project_id") or ""), str(snapshot.get("snapshot_id") or ""))
    if graph_path.exists():
        try:
            graph_stats = graph_payload_stats(json.loads(graph_path.read_text(encoding="utf-8")))
        except Exception:
            graph_stats = {}
    trace_ref = notes.get("trace") if isinstance(notes.get("trace"), dict) else {}
    trace_summary_path = str(trace_ref.get("summary_path") or "")
    trace_summary: dict[str, Any] = {}
    if trace_summary_path:
        try:
            trace_summary = json.loads(Path(trace_summary_path).read_text(encoding="utf-8"))
        except Exception:
            trace_summary = {}
    strategy = str(
        notes.get("scope_reconcile_strategy")
        or scope_delta.get("strategy")
        or ("legacy_full_like" if snapshot.get("snapshot_kind") == "scope" else snapshot.get("snapshot_kind") or "")
    )
    mode = str(
        notes.get("scope_graph_delta_mode")
        or scope_delta.get("graph_delta_mode")
        or ("full_rebuild" if strategy == "legacy_full_like" else "")
    )
    fallback_reason = str(scope_delta.get("fallback_reason") or "")
    return {
        "run_id": str(notes.get("run_id") or snapshot.get("snapshot_id") or ""),
        "snapshot_id": str(snapshot.get("snapshot_id") or ""),
        "commit_sha": str(snapshot.get("commit_sha") or ""),
        "parent_commit_sha": str(pending_notes.get("active_graph_commit") or ""),
        "snapshot_kind": str(snapshot.get("snapshot_kind") or ""),
        "strategy": strategy,
        "graph_delta_mode": mode,
        "status": str(trace_summary.get("status") or snapshot.get("status") or ""),
        "changed_file_count": _int_value(scope_delta.get("changed_file_count")),
        "impacted_file_count": _int_value(scope_delta.get("impacted_file_count")),
        "event_count": _int_value(event_summary.get("event_count")),
        "node_count": _int_value(graph_stats.get("nodes")),
        "edge_count": _int_value(graph_stats.get("edges")),
        "elapsed_ms": _int_value(trace_summary.get("elapsed_ms")),
        "trace_summary_path": trace_summary_path,
        "fallback_reason": fallback_reason,
        "created_at": str(snapshot.get("created_at") or ""),
        "evidence": {
            "source": "graph_snapshot_notes_backfill",
            "snapshot_status": snapshot.get("status") or "",
        },
    }


def backfill_reconcile_run_metrics_from_snapshots(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    """Best-effort import of historical trace timings into metrics table."""
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT * FROM graph_snapshots
        WHERE project_id=? AND snapshot_kind IN ('scope', 'full')
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (project_id, int(limit or 100)),
    ).fetchall()
    imported = 0
    for row in rows:
        metric = _metric_from_snapshot_row(row)
        if not metric:
            continue
        try:
            record_reconcile_run_metric(
                conn,
                project_id,
                run_id=metric["run_id"],
                snapshot_id=metric["snapshot_id"],
                commit_sha=metric["commit_sha"],
                parent_commit_sha=metric["parent_commit_sha"],
                snapshot_kind=metric["snapshot_kind"],
                strategy=metric["strategy"],
                graph_delta_mode=metric["graph_delta_mode"],
                status=metric["status"],
                changed_file_count=metric["changed_file_count"],
                impacted_file_count=metric["impacted_file_count"],
                event_count=metric["event_count"],
                node_count=metric["node_count"],
                edge_count=metric["edge_count"],
                elapsed_ms=metric["elapsed_ms"],
                trace_summary_path=metric["trace_summary_path"],
                fallback_reason=metric["fallback_reason"],
                evidence=metric["evidence"],
                created_at=metric["created_at"],
            )
            imported += 1
        except Exception:
            continue
    return {"project_id": project_id, "scanned": len(rows), "imported": imported}


def list_reconcile_run_metrics(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    limit: int = 50,
    strategy: str = "",
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id]
    sql = "SELECT * FROM reconcile_run_metrics WHERE project_id=?"
    if strategy:
        sql += " AND strategy=?"
        params.append(strategy)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit or 50))
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def summarize_reconcile_run_metrics(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    rows = list_reconcile_run_metrics(conn, project_id, limit=limit)
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        strategy = str(row.get("strategy") or "unknown")
        bucket = buckets.setdefault(
            strategy,
            {"count": 0, "total_elapsed_ms": 0, "min_elapsed_ms": 0, "max_elapsed_ms": 0},
        )
        elapsed = int(row.get("elapsed_ms") or 0)
        bucket["count"] += 1
        bucket["total_elapsed_ms"] += elapsed
        bucket["min_elapsed_ms"] = elapsed if not bucket["min_elapsed_ms"] else min(bucket["min_elapsed_ms"], elapsed)
        bucket["max_elapsed_ms"] = max(bucket["max_elapsed_ms"], elapsed)
    for bucket in buckets.values():
        count = int(bucket.get("count") or 0)
        bucket["avg_elapsed_ms"] = round(float(bucket["total_elapsed_ms"]) / count, 2) if count else 0

    incremental = buckets.get("incremental_graph_delta") or {}
    full_candidates = [
        bucket for name, bucket in buckets.items()
        if name in {"full_rebuild_fallback", "legacy_full_like", "full"}
    ]
    full_count = sum(int(bucket.get("count") or 0) for bucket in full_candidates)
    full_total = sum(int(bucket.get("total_elapsed_ms") or 0) for bucket in full_candidates)
    incremental_avg = float(incremental.get("avg_elapsed_ms") or 0)
    full_avg = (float(full_total) / full_count) if full_count else 0.0
    speedup = round(full_avg / incremental_avg, 2) if full_avg and incremental_avg else 0
    reduction_pct = round((1 - (incremental_avg / full_avg)) * 100, 1) if full_avg and incremental_avg else 0
    return {
        "project_id": project_id,
        "sample_count": len(rows),
        "by_strategy": buckets,
        "speedup": {
            "incremental_avg_ms": round(incremental_avg, 2),
            "full_avg_ms": round(full_avg, 2),
            "speedup_x": speedup,
            "elapsed_reduction_pct": reduction_pct,
            "full_sample_count": full_count,
            "incremental_sample_count": int(incremental.get("count") or 0),
        },
    }


def strict_graph_ready(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    target_commit: str,
) -> dict[str, Any]:
    status = graph_governance_status(conn, project_id)
    graph_commit = status.get("materialized_graph_baseline_commit") or ""
    ok = bool(target_commit and graph_commit == target_commit)
    reason = ""
    if not graph_commit:
        reason = "no_active_graph_snapshot"
    elif not target_commit:
        reason = "missing_target_commit"
    elif graph_commit != target_commit:
        reason = "graph_snapshot_commit_mismatch"
    return {
        "ok": ok,
        "reason": reason,
        "target_commit": target_commit,
        **status,
    }


def import_existing_graph_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    commit_sha: str = "",
    snapshot_id: str | None = None,
    created_by: str = "observer",
    activate: bool = False,
    expected_old_snapshot_id: str | None = None,
    extra_graph_paths: Iterable[str | Path] | None = None,
) -> dict[str, Any]:
    source = select_existing_graph_source(
        conn,
        project_id,
        extra_graph_paths=extra_graph_paths,
    )
    if not source:
        raise FileNotFoundError(f"no non-empty graph source found for project {project_id}")

    selected_commit = _resolve_import_commit(conn, project_id, commit_sha)
    sid = snapshot_id or snapshot_id_for("imported", selected_commit)
    source_notes = {
        "source_kind": source["source_kind"],
        "source_path": source["source_path"],
        "source_ref": source.get("source_ref", ""),
        "source_stats": source["stats"],
        "selected_commit": selected_commit,
    }
    snapshot = create_graph_snapshot(
        conn,
        project_id,
        snapshot_id=sid,
        commit_sha=selected_commit,
        snapshot_kind="imported",
        graph_json=source["graph_json"],
        file_inventory=[],
        drift_ledger=[],
        created_by=created_by,
        notes=_json(source_notes),
    )
    counts = index_graph_snapshot(
        conn,
        project_id,
        sid,
        nodes=_graph_nodes(source["graph_json"]),
        edges=_graph_edges(source["graph_json"]),
    )
    result = {
        **snapshot,
        "source": {k: v for k, v in source.items() if k != "graph_json"},
        "index_counts": counts,
        "activation": None,
    }
    if activate:
        result["activation"] = activate_graph_snapshot(
            conn,
            project_id,
            sid,
            expected_old_snapshot_id=expected_old_snapshot_id,
        )
    return result


def record_drift(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    snapshot_id: str,
    commit_sha: str,
    path: str,
    drift_type: str,
    target_symbol: str = "",
    node_id: str = "",
    status: str = "open",
    evidence: dict[str, Any] | None = None,
) -> None:
    ensure_schema(conn)
    conn.execute(
        """
        INSERT OR REPLACE INTO graph_drift_ledger
          (project_id, snapshot_id, commit_sha, path, node_id, target_symbol,
           drift_type, status, evidence_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            snapshot_id,
            commit_sha,
            path,
            node_id,
            target_symbol,
            drift_type,
            status,
            _json(evidence or {}),
            utc_now(),
        ),
    )


def list_graph_drift(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    snapshot_id: str = "",
    status: str = "",
    drift_type: str = "",
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id]
    sql = """
        SELECT project_id, snapshot_id, commit_sha, path, node_id,
               target_symbol, drift_type, status, evidence_json, updated_at
        FROM graph_drift_ledger
        WHERE project_id = ?
    """
    if snapshot_id:
        sql += " AND snapshot_id = ?"
        params.append(snapshot_id)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if drift_type:
        sql += " AND drift_type = ?"
        params.append(drift_type)
    sql += " ORDER BY updated_at DESC, path, drift_type, target_symbol LIMIT ? OFFSET ?"
    params.extend([max(1, min(int(limit or 200), 1000)), max(0, int(offset or 0))])
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "project_id": row["project_id"],
            "snapshot_id": row["snapshot_id"],
            "commit_sha": row["commit_sha"],
            "path": row["path"],
            "node_id": row["node_id"],
            "target_symbol": row["target_symbol"],
            "drift_type": row["drift_type"],
            "status": row["status"],
            "evidence": _decode_json(row["evidence_json"], {}),
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def get_graph_drift(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    snapshot_id: str,
    path: str,
    drift_type: str,
    target_symbol: str | None = None,
) -> dict[str, Any]:
    """Fetch one drift row. If target_symbol is omitted, the match must be unique."""
    ensure_schema(conn)
    params: list[Any] = [project_id, snapshot_id, path, drift_type]
    sql = """
        SELECT project_id, snapshot_id, commit_sha, path, node_id,
               target_symbol, drift_type, status, evidence_json, updated_at
        FROM graph_drift_ledger
        WHERE project_id = ?
          AND snapshot_id = ?
          AND path = ?
          AND drift_type = ?
    """
    if target_symbol is not None:
        sql += " AND target_symbol = ?"
        params.append(target_symbol)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        raise KeyError(f"graph drift row not found: {snapshot_id}/{path}/{drift_type}")
    if target_symbol is None and len(rows) > 1:
        raise ValueError("multiple drift rows match; target_symbol is required")
    row = rows[0]
    return {
        "project_id": row["project_id"],
        "snapshot_id": row["snapshot_id"],
        "commit_sha": row["commit_sha"],
        "path": row["path"],
        "node_id": row["node_id"],
        "target_symbol": row["target_symbol"],
        "drift_type": row["drift_type"],
        "status": row["status"],
        "evidence": _decode_json(row["evidence_json"], {}),
        "updated_at": row["updated_at"],
    }


def update_graph_drift_status(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    snapshot_id: str,
    path: str,
    drift_type: str,
    target_symbol: str = "",
    status: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Update one drift row status while preserving/augmenting its evidence."""
    row = get_graph_drift(
        conn,
        project_id,
        snapshot_id=snapshot_id,
        path=path,
        drift_type=drift_type,
        target_symbol=target_symbol,
    )
    merged_evidence = dict(row.get("evidence") or {})
    merged_evidence.update(evidence or {})
    now = utc_now()
    conn.execute(
        """
        UPDATE graph_drift_ledger
        SET status = ?,
            evidence_json = ?,
            updated_at = ?
        WHERE project_id = ?
          AND snapshot_id = ?
          AND path = ?
          AND drift_type = ?
          AND target_symbol = ?
        """,
        (
            status,
            _json(merged_evidence),
            now,
            project_id,
            snapshot_id,
            path,
            drift_type,
            target_symbol,
        ),
    )
    row["status"] = status
    row["evidence"] = merged_evidence
    row["updated_at"] = now
    return row


def queue_pending_scope_reconcile(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    commit_sha: str,
    parent_commit_sha: str = "",
    ref_name: str = "",
    branch_ref: str = "",
    worktree_id: str = "",
    worktree_path: str = "",
    status: str = PENDING_STATUS_QUEUED,
    snapshot_id: str = "",
    evidence: dict[str, Any] | None = None,
    force_requeue: bool = False,
) -> dict[str, Any]:
    ensure_schema(conn)
    if status not in ALLOWED_PENDING_STATUSES:
        raise ValueError(f"invalid pending scope reconcile status: {status}")
    now = utc_now()
    force_flag = 1 if force_requeue else 0
    identity = normalize_pending_scope_identity(
        ref_name=ref_name,
        branch_ref=branch_ref,
        worktree_id=worktree_id,
        worktree_path=worktree_path,
    )
    conn.execute(
        """
        INSERT INTO pending_scope_reconcile
          (project_id, ref_name, branch_ref, worktree_id, worktree_path,
           commit_sha, parent_commit_sha, queued_at, status, retry_count,
           snapshot_id, evidence_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
        ON CONFLICT(project_id, ref_name, worktree_id, commit_sha) DO UPDATE SET
          queued_at = CASE
            WHEN ? = 1 THEN excluded.queued_at
            ELSE pending_scope_reconcile.queued_at
          END,
          branch_ref = excluded.branch_ref,
          worktree_path = excluded.worktree_path,
          parent_commit_sha = CASE
            WHEN pending_scope_reconcile.parent_commit_sha = '' THEN excluded.parent_commit_sha
            ELSE pending_scope_reconcile.parent_commit_sha
          END,
          status = CASE
            WHEN ? = 1 THEN excluded.status
            WHEN pending_scope_reconcile.status IN ('materialized', 'waived')
            THEN pending_scope_reconcile.status
            ELSE excluded.status
          END,
          retry_count = CASE
            WHEN ? = 1 THEN pending_scope_reconcile.retry_count + 1
            ELSE pending_scope_reconcile.retry_count
          END,
          snapshot_id = CASE
            WHEN ? = 1 THEN excluded.snapshot_id
            WHEN excluded.snapshot_id != '' THEN excluded.snapshot_id
            ELSE pending_scope_reconcile.snapshot_id
          END,
          evidence_json = excluded.evidence_json
        """,
        (
            project_id,
            identity["ref_name"],
            identity["branch_ref"],
            identity["worktree_id"],
            identity["worktree_path"],
            commit_sha,
            parent_commit_sha,
            now,
            status,
            snapshot_id,
            _json({
                "ref_name": identity["ref_name"],
                "branch_ref": identity["branch_ref"],
                "worktree_id": identity["worktree_id"],
                "worktree_path": identity["worktree_path"],
                **(evidence or {}),
                **({"force_requeue": True, "forced_at": now} if force_requeue else {}),
            }),
            force_flag,
            force_flag,
            force_flag,
            force_flag,
        ),
    )
    row = conn.execute(
        """
        SELECT * FROM pending_scope_reconcile
        WHERE project_id = ? AND ref_name = ? AND worktree_id = ? AND commit_sha = ?
        """,
        (project_id, identity["ref_name"], identity["worktree_id"], commit_sha),
    ).fetchone()
    return dict(row)


def waive_pending_scope_reconcile(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    commit_shas: Iterable[str] | None = None,
    ref_name: str | None = None,
    branch_ref: str | None = None,
    worktree_id: str | None = None,
    worktree_path: str | None = None,
    snapshot_id: str = "",
    actor: str = "observer",
    reason: str = "",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mark retryable pending scope rows as waived with explicit evidence."""
    ensure_schema(conn)
    selected = [
        str(commit or "").strip()
        for commit in (commit_shas or [])
        if str(commit or "").strip()
    ]
    params: list[Any] = [project_id]
    sql = """
        SELECT commit_sha FROM pending_scope_reconcile
        WHERE project_id = ?
          AND status IN (?, ?, ?)
    """
    params.extend([PENDING_STATUS_QUEUED, PENDING_STATUS_RUNNING, PENDING_STATUS_FAILED])
    if selected:
        placeholders = ",".join("?" for _ in selected)
        sql += f" AND commit_sha IN ({placeholders})"
        params.extend(selected)
    identity: dict[str, str] | None = None
    if any(value is not None for value in (ref_name, branch_ref, worktree_id, worktree_path)):
        identity = normalize_pending_scope_identity(
            ref_name=str(ref_name or ""),
            branch_ref=str(branch_ref or ""),
            worktree_id=str(worktree_id or ""),
            worktree_path=str(worktree_path or ""),
        )
        sql += " AND ref_name = ? AND worktree_id = ?"
        params.extend([identity["ref_name"], identity["worktree_id"]])
        if branch_ref is not None:
            sql += " AND branch_ref = ?"
            params.append(identity["branch_ref"])
    sql += " ORDER BY queued_at, commit_sha"
    rows = conn.execute(sql, params).fetchall()
    targets = [row["commit_sha"] for row in rows]
    if not targets:
        return {
            "project_id": project_id,
            "waived_count": 0,
            "commit_shas": [],
            "snapshot_id": snapshot_id,
        }

    waiver_evidence = {
        "source": "pending_scope_waiver",
        "actor": actor,
        "reason": reason,
        "snapshot_id": snapshot_id,
        "commit_shas": targets,
        **(identity or {}),
        **(evidence or {}),
    }
    placeholders = ",".join("?" for _ in targets)
    update_filters = ""
    update_filter_values: list[Any] = []
    if identity is not None:
        update_filters += " AND ref_name = ? AND worktree_id = ?"
        update_filter_values.extend([identity["ref_name"], identity["worktree_id"]])
    cur = conn.execute(
        f"""
        UPDATE pending_scope_reconcile
        SET status = ?,
            snapshot_id = CASE WHEN ? != '' THEN ? ELSE snapshot_id END,
            evidence_json = ?
        WHERE project_id = ?
          AND commit_sha IN ({placeholders})
          {update_filters}
          AND status IN (?, ?, ?)
        """,
        (
            PENDING_STATUS_WAIVED,
            snapshot_id,
            snapshot_id,
            _json(waiver_evidence),
            project_id,
            *targets,
            *update_filter_values,
            PENDING_STATUS_QUEUED,
            PENDING_STATUS_RUNNING,
            PENDING_STATUS_FAILED,
        ),
    )
    return {
        "project_id": project_id,
        "waived_count": int(cur.rowcount or 0),
        "commit_shas": targets,
        "snapshot_id": snapshot_id,
        **(identity or {}),
    }


def mark_pending_scope_reconcile_failed(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    commit_sha: str,
    ref_name: str | None = None,
    branch_ref: str | None = None,
    worktree_id: str | None = None,
    worktree_path: str | None = None,
    actor: str = "observer",
    reason: str = "",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Move a queued/running pending-scope row to failed with recovery evidence."""
    ensure_schema(conn)
    commit = str(commit_sha or "").strip()
    if not commit:
        return {"project_id": project_id, "updated_count": 0, "commit_sha": ""}
    identity: dict[str, str] | None = None
    select_sql = "SELECT * FROM pending_scope_reconcile WHERE project_id=? AND commit_sha=?"
    select_params: list[Any] = [project_id, commit]
    if any(value is not None for value in (ref_name, branch_ref, worktree_id, worktree_path)):
        identity = normalize_pending_scope_identity(
            ref_name=str(ref_name or ""),
            branch_ref=str(branch_ref or ""),
            worktree_id=str(worktree_id or ""),
            worktree_path=str(worktree_path or ""),
        )
        select_sql += " AND ref_name=? AND worktree_id=?"
        select_params.extend([identity["ref_name"], identity["worktree_id"]])
        if branch_ref is not None:
            select_sql += " AND branch_ref=?"
            select_params.append(identity["branch_ref"])
    row = conn.execute(select_sql, select_params).fetchone()
    previous = dict(row) if row else {}
    failure_evidence = {
        "source": "pending_scope_failure",
        "actor": actor,
        "reason": reason,
        "commit_sha": commit,
        **(identity or {}),
        "previous_status": previous.get("status", ""),
        "previous_evidence": _decode_json(previous.get("evidence_json"), {}),
        "recoverable": True,
        "recovery_action": "force_requeue_pending_scope",
        **(evidence or {}),
    }
    update_filters = ""
    update_filter_values: list[Any] = []
    if identity is not None:
        update_filters += " AND ref_name=? AND worktree_id=?"
        update_filter_values.extend([identity["ref_name"], identity["worktree_id"]])
    cur = conn.execute(
        f"""
        UPDATE pending_scope_reconcile
        SET status=?, evidence_json=?
        WHERE project_id=? AND commit_sha=? {update_filters} AND status IN (?, ?, ?)
        """,
        (
            PENDING_STATUS_FAILED,
            _json(failure_evidence),
            project_id,
            commit,
            *update_filter_values,
            PENDING_STATUS_QUEUED,
            PENDING_STATUS_RUNNING,
            PENDING_STATUS_FAILED,
        ),
    )
    return {
        "project_id": project_id,
        "updated_count": int(cur.rowcount or 0),
        "commit_sha": commit,
        "status": PENDING_STATUS_FAILED,
        "evidence": failure_evidence,
        **(identity or {}),
    }


def recover_stale_pending_scope_reconcile(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    max_running_seconds: int = 1800,
    actor: str = "observer",
) -> dict[str, Any]:
    """Fail old running rows so dashboard Update Graph can requeue them."""
    ensure_schema(conn)
    cutoff_seconds = max(0, int(max_running_seconds or 0))
    now_text = utc_now()
    rows = conn.execute(
        """
        SELECT * FROM pending_scope_reconcile
        WHERE project_id=? AND status=?
        ORDER BY queued_at, ref_name, worktree_id, commit_sha
        """,
        (project_id, PENDING_STATUS_RUNNING),
    ).fetchall()
    recovered: list[str] = []
    recovered_rows: list[dict[str, str]] = []
    now_dt = datetime.now(timezone.utc)
    for row in rows:
        queued_at = str(row["queued_at"] or "")
        try:
            queued_dt = datetime.strptime(queued_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age_seconds = int((now_dt - queued_dt).total_seconds())
        except Exception:
            age_seconds = cutoff_seconds + 1
        if age_seconds < cutoff_seconds:
            continue
        commit = str(row["commit_sha"] or "")
        mark_pending_scope_reconcile_failed(
            conn,
            project_id,
            commit_sha=commit,
            ref_name=str(row["ref_name"] or ""),
            branch_ref=str(row["branch_ref"] or ""),
            worktree_id=str(row["worktree_id"] or ""),
            worktree_path=str(row["worktree_path"] or ""),
            actor=actor,
            reason="stale running pending-scope row exceeded recovery threshold",
            evidence={
                "source": "pending_scope_stale_running_recovery",
                "queued_at": queued_at,
                "recovered_at": now_text,
                "age_seconds": age_seconds,
                "max_running_seconds": cutoff_seconds,
            },
        )
        recovered.append(commit)
        recovered_rows.append({
            "commit_sha": commit,
            "ref_name": str(row["ref_name"] or ""),
            "branch_ref": str(row["branch_ref"] or ""),
            "worktree_id": str(row["worktree_id"] or ""),
            "worktree_path": str(row["worktree_path"] or ""),
        })
    return {
        "project_id": project_id,
        "recovered_count": len(recovered),
        "commit_shas": recovered,
        "recovered_rows": recovered_rows,
        "max_running_seconds": cutoff_seconds,
    }


__all__ = [
    "ALLOWED_PENDING_STATUSES",
    "ALLOWED_SNAPSHOT_STATUSES",
    "GRAPH_SNAPSHOT_SCHEMA_SQL",
    "GraphSnapshotConflictError",
    "activate_graph_snapshot",
    "backfill_reconcile_run_metrics_from_snapshots",
    "build_graph_rollback_epoch_state",
    "create_graph_snapshot",
    "ensure_schema",
    "export_graph_snapshot_cache",
    "finalize_graph_snapshot",
    "get_active_graph_snapshot",
    "get_graph_drift",
    "get_graph_snapshot",
    "get_latest_scan_baseline",
    "graph_governance_status",
    "graph_payload_edges",
    "index_graph_snapshot",
    "list_reconcile_run_metrics",
    "list_graph_snapshot_edges",
    "list_graph_snapshot_files",
    "list_graph_snapshot_nodes",
    "list_graph_snapshots",
    "list_graph_ref_events",
    "list_graph_drift",
    "normalize_pending_scope_identity",
    "graph_payload_stats",
    "import_existing_graph_snapshot",
    "invalidate_semantic_jobs_for_rollback_epoch",
    "abandon_graph_snapshot",
    "list_pending_scope_reconcile",
    "mark_pending_scope_reconcile_failed",
    "queue_pending_scope_reconcile",
    "record_reconcile_run_metric",
    "record_graph_ref_event",
    "recover_stale_pending_scope_reconcile",
    "record_drift",
    "select_existing_graph_source",
    "snapshot_materialization_provenance",
    "snapshot_companion_dir",
    "snapshot_graph_path",
    "snapshot_id_for",
    "strict_graph_ready",
    "summarize_reconcile_run_metrics",
    "summarize_file_inventory_rows",
    "update_graph_drift_status",
    "waive_pending_scope_reconcile",
    "write_companion_files",
]
