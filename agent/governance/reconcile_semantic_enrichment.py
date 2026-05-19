"""State-only semantic enrichment for graph reconcile snapshots.

The structural graph remains the source of truth.  Semantic enrichment is a
retryable companion artifact attached to a snapshot, so full and scope
reconcile can reuse the same review/feedback loop without mutating project
source files or graph topology.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .reconcile_trace import write_json
from .graph_snapshot_store import (
    ensure_schema,
    get_graph_snapshot,
    snapshot_companion_dir,
    snapshot_graph_path,
    utc_now,
)
from .reconcile_semantic_config import load_semantic_enrichment_config


SEMANTIC_ENRICHMENT_SCHEMA_VERSION = 1
SEMANTIC_HEALTH_ISSUE_SCHEMA_VERSION = 1
SEMANTIC_ARTIFACT_DIR = "semantic-enrichment"
SEMANTIC_INDEX_NAME = "semantic-index.json"
SEMANTIC_REVIEW_REPORT_NAME = "semantic-review-report.json"
SEMANTIC_GRAPH_STATE_NAME = "semantic-graph-state.json"
SEMANTIC_GRAPH_NAME = "semantic-graph.json"
REVIEW_FEEDBACK_NAME = "review-feedback.jsonl"

FeedbackAiCall = Callable[[str, dict[str, Any]], Any]

SEMANTIC_JOB_TERMINAL_STATUSES = {
    "ai_complete",
    "complete",
    "cancelled",
    "failed",
    "ai_failed",
    "rejected",
    "rule_complete",
}

SEMANTIC_STATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_semantic_nodes (
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '',
  feature_hash TEXT NOT NULL DEFAULT '',
  file_hashes_json TEXT NOT NULL DEFAULT '{}',
  semantic_json TEXT NOT NULL DEFAULT '{}',
  branch_ref TEXT NOT NULL DEFAULT '',
  operation_type TEXT NOT NULL DEFAULT '',
  source_branch_ref TEXT NOT NULL DEFAULT '',
  source_snapshot_id TEXT NOT NULL DEFAULT '',
  source_event_id TEXT NOT NULL DEFAULT '',
  payload_hash TEXT NOT NULL DEFAULT '',
  feedback_round INTEGER NOT NULL DEFAULT 0,
  batch_index INTEGER,
  updated_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(project_id, snapshot_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_semantic_nodes_status
  ON graph_semantic_nodes(project_id, snapshot_id, status);

-- MF 2026-05-11 (observer-hotfix): edge semantic persistent-state parity.
-- Mirrors graph_semantic_nodes minus file_hashes (edges have no files of
-- their own). `edge_signature_hash` plays the role `feature_hash` plays
-- for nodes — it's the drift detector that carry-forward compares.
CREATE TABLE IF NOT EXISTS graph_semantic_edges (
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  edge_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '',
  edge_signature_hash TEXT NOT NULL DEFAULT '',
  semantic_json TEXT NOT NULL DEFAULT '{}',
  branch_ref TEXT NOT NULL DEFAULT '',
  operation_type TEXT NOT NULL DEFAULT '',
  source_branch_ref TEXT NOT NULL DEFAULT '',
  source_snapshot_id TEXT NOT NULL DEFAULT '',
  source_event_id TEXT NOT NULL DEFAULT '',
  payload_hash TEXT NOT NULL DEFAULT '',
  feedback_round INTEGER NOT NULL DEFAULT 0,
  batch_index INTEGER,
  updated_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(project_id, snapshot_id, edge_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_semantic_edges_status
  ON graph_semantic_edges(project_id, snapshot_id, status);

CREATE TABLE IF NOT EXISTS graph_semantic_jobs (
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  node_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending_ai',
  feature_hash TEXT NOT NULL DEFAULT '',
  file_hashes_json TEXT NOT NULL DEFAULT '{}',
  branch_ref TEXT NOT NULL DEFAULT '',
  operation_type TEXT NOT NULL DEFAULT '',
  feedback_round INTEGER NOT NULL DEFAULT 0,
  batch_index INTEGER,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  worker_id TEXT NOT NULL DEFAULT '',
  claim_id TEXT NOT NULL DEFAULT '',
  claimed_at TEXT NOT NULL DEFAULT '',
  lease_expires_at TEXT NOT NULL DEFAULT '',
  claimed_by TEXT NOT NULL DEFAULT '',
  last_error TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(project_id, snapshot_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_semantic_jobs_status
  ON graph_semantic_jobs(project_id, snapshot_id, status, updated_at);
"""


def _ensure_semantic_state_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SEMANTIC_STATE_SCHEMA_SQL)
    _ensure_semantic_timeline_columns(conn)
    _ensure_semantic_jobs_claim_columns(conn)


def _semantic_write_lock():
    try:
        from .db import sqlite_write_lock

        return sqlite_write_lock()
    except Exception:
        return nullcontext()


def _commit_semantic_write(conn: sqlite3.Connection) -> None:
    try:
        conn.commit()
    except sqlite3.ProgrammingError:
        raise
    except Exception:
        # Some unit tests use in-memory connections with explicit transaction
        # ownership.  The write itself is still protected by the process lock.
        pass


def _ensure_semantic_jobs_claim_columns(conn: sqlite3.Connection) -> None:
    try:
        rows = conn.execute("PRAGMA table_info(graph_semantic_jobs)").fetchall()
    except sqlite3.OperationalError:
        return
    existing = {
        (row["name"] if hasattr(row, "keys") else row[1])
        for row in rows
    }
    for name in ("worker_id", "claim_id", "claimed_at", "lease_expires_at", "claimed_by"):
        if name not in existing:
            conn.execute(
                f"ALTER TABLE graph_semantic_jobs ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
            )


def _ensure_semantic_timeline_columns(conn: sqlite3.Connection) -> None:
    column_groups = {
        "graph_semantic_nodes": {
            "branch_ref": "TEXT NOT NULL DEFAULT ''",
            "operation_type": "TEXT NOT NULL DEFAULT ''",
            "source_branch_ref": "TEXT NOT NULL DEFAULT ''",
            "source_snapshot_id": "TEXT NOT NULL DEFAULT ''",
            "source_event_id": "TEXT NOT NULL DEFAULT ''",
            "payload_hash": "TEXT NOT NULL DEFAULT ''",
        },
        "graph_semantic_edges": {
            "branch_ref": "TEXT NOT NULL DEFAULT ''",
            "operation_type": "TEXT NOT NULL DEFAULT ''",
            "source_branch_ref": "TEXT NOT NULL DEFAULT ''",
            "source_snapshot_id": "TEXT NOT NULL DEFAULT ''",
            "source_event_id": "TEXT NOT NULL DEFAULT ''",
            "payload_hash": "TEXT NOT NULL DEFAULT ''",
        },
        "graph_semantic_jobs": {
            "branch_ref": "TEXT NOT NULL DEFAULT ''",
            "operation_type": "TEXT NOT NULL DEFAULT ''",
        },
    }
    for table, columns in column_groups.items():
        try:
            rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.OperationalError:
            continue
        existing = {
            (row["name"] if hasattr(row, "keys") else row[1])
            for row in rows
        }
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        values = [item.strip() for item in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = [raw]
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        value = str(item or "").strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _read_json(path: Path, default: Any) -> Any:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return payload if payload is not None else default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json(payload), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(_json(row) + "\n")


def _decode_notes(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}


def _update_snapshot_notes(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    patch: dict[str, Any],
) -> dict[str, Any]:
    snapshot = get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    notes = _decode_notes(snapshot.get("notes"))
    notes.update(patch)
    with _semantic_write_lock():
        conn.execute(
            """
            UPDATE graph_snapshots
            SET notes = ?
            WHERE project_id = ? AND snapshot_id = ?
            """,
            (_json(notes), project_id, snapshot_id),
        )
        _commit_semantic_write(conn)
    return notes


def _semantic_base_dir(project_id: str, snapshot_id: str) -> Path:
    return snapshot_companion_dir(project_id, snapshot_id) / SEMANTIC_ARTIFACT_DIR


def _feedback_path(project_id: str, snapshot_id: str) -> Path:
    return _semantic_base_dir(project_id, snapshot_id) / REVIEW_FEEDBACK_NAME


def _round_dir(project_id: str, snapshot_id: str, feedback_round: int) -> Path:
    return _semantic_base_dir(project_id, snapshot_id) / "rounds" / f"round-{feedback_round:03d}"


def _path_list(raw: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in _string_list(raw):
        path = str(item or "").replace("\\", "/").strip("/")
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


NODE_SEMANTIC_SELF_CHECK_RULES = [
    "required_fields_present",
    "source_payload_only",
    "no_project_mutation",
    "review_feedback_accounted_for",
    "graph_suggestions_contract_checked",
]


def _safe_int(raw: Any, default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _node_semantic_self_check(
    ai_response: dict[str, Any],
    *,
    required: bool,
) -> dict[str, Any]:
    if not required:
        return {
            "required": False,
            "valid": True,
            "status": "not_required",
            "precheck_status": "not_required",
            "checked_rules": [],
            "checked_rules_count": 0,
            "repair_attempts": 0,
            "max_repair_attempts": 0,
            "known_risks": [],
            "source": "system",
        }
    raw = ai_response.get("self_check") or ai_response.get("precheck")
    if not isinstance(raw, dict):
        return {
            "required": True,
            "valid": False,
            "status": "missing",
            "precheck_status": "missing",
            "checked_rules": [],
            "checked_rules_count": 0,
            "repair_attempts": 0,
            "max_repair_attempts": 1,
            "known_risks": ["missing_ai_self_check"],
            "source": "missing_ai_self_check",
        }

    checked_rules = _path_list(
        raw.get("checked_rules")
        or raw.get("rules_checked")
        or raw.get("rules")
    )
    checked_rules_count = _safe_int(raw.get("checked_rules_count"), len(checked_rules))
    status = str(raw.get("status") or raw.get("precheck_status") or "").strip().lower()
    valid_raw = raw.get("valid")
    valid = bool(valid_raw is True or status in {"passed", "pass", "ok", "valid"})
    if not status:
        status = "passed" if valid else "failed"
    known_risks = _path_list(raw.get("known_risks") or raw.get("risks"))
    missing_rules = [
        rule for rule in NODE_SEMANTIC_SELF_CHECK_RULES
        if checked_rules and rule not in checked_rules
    ]
    if not checked_rules and checked_rules_count <= 0:
        known_risks.append("missing_self_check_rules")
        valid = False
        status = "failed"
    if missing_rules:
        known_risks.extend(f"missing_self_check_rule:{rule}" for rule in missing_rules)
        valid = False
        status = "failed"
    return {
        "required": True,
        "valid": valid,
        "status": status,
        "precheck_status": str(raw.get("precheck_status") or status),
        "checked_rules": checked_rules,
        "checked_rules_count": checked_rules_count,
        "repair_attempts": _safe_int(raw.get("repair_attempts"), 0),
        "max_repair_attempts": _safe_int(raw.get("max_repair_attempts"), 1),
        "known_risks": known_risks,
        "source": str(raw.get("source") or "ai_self_check"),
    }


def _graph_nodes(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    deps = graph_json.get("deps_graph") if isinstance(graph_json, dict) else {}
    nodes = deps.get("nodes") if isinstance(deps, dict) else None
    return [dict(node) for node in nodes or [] if isinstance(node, dict)]


def _graph_edges(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    deps = graph_json.get("deps_graph") if isinstance(graph_json, dict) else {}
    edges = deps.get("edges") if isinstance(deps, dict) else None
    return [dict(edge) for edge in edges or [] if isinstance(edge, dict)]


def _build_edge_carry_index(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Index current snapshot's edges by edge_id with edge_signature_hash
    precomputed. Used by `_carry_forward_semantic_graph_state` to decide
    whether a base-snapshot edge_semantic entry can be reused in the new
    snapshot. Same shape as the node `feature_index`.
    """
    from . import graph_events
    node_by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        nid = str(node.get("id") or node.get("node_id") or "").strip()
        if nid:
            node_by_id[nid] = node
    out: dict[str, dict[str, Any]] = {}
    for edge in edges:
        edge_type = str(edge.get("edge_type") or edge.get("type") or "").strip()
        if edge_type == "contains":
            # contains edges aren't semantically enriched
            continue
        src = str(edge.get("src") or edge.get("source") or "").strip()
        dst = str(edge.get("dst") or edge.get("target") or "").strip()
        if not src or not dst or not edge_type:
            continue
        edge_id = f"{src}->{dst}:{edge_type}"
        src_node = node_by_id.get(src)
        dst_node = node_by_id.get(dst)
        out[edge_id] = {
            "edge_id": edge_id,
            "edge": edge,
            "src_node": src_node,
            "dst_node": dst_node,
            "edge_signature_hash": graph_events.edge_signature_hash_for_edge(
                edge, src_node, dst_node
            ),
            "stable_edge_key": graph_events.stable_edge_key_for_edge(
                edge, src_node, dst_node
            ),
        }
    return out


def _load_feature_index(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    notes = _decode_notes(snapshot.get("notes"))
    path = (
        notes.get("governance_index", {})
        .get("artifacts", {})
        .get("feature_index_path", "")
    )
    if not path:
        return {}
    payload = _read_json(Path(path), {})
    features = payload.get("features") if isinstance(payload, dict) else None
    out: dict[str, dict[str, Any]] = {}
    for feature in features or []:
        if not isinstance(feature, dict):
            continue
        node_id = str(feature.get("node_id") or "")
        if node_id:
            out[node_id] = dict(feature)
    return out


def _hash_payload(payload: Any) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _semantic_job_terminal(status: Any) -> bool:
    return str(status or "").strip().lower().replace("-", "_") in SEMANTIC_JOB_TERMINAL_STATUSES


def _semantic_job_existing_terminal_wins(existing: sqlite3.Row | None, incoming: dict[str, Any]) -> bool:
    if not existing:
        return False
    existing_status = str(existing["status"] or "")
    incoming_status = str(incoming.get("status") or "pending_ai")
    if not _semantic_job_terminal(existing_status) or _semantic_job_terminal(incoming_status):
        return False
    existing_updated_at = str(existing["updated_at"] or "")
    incoming_updated_at = str(incoming.get("updated_at") or "")
    return bool(existing_updated_at and incoming_updated_at and existing_updated_at >= incoming_updated_at)


def _node_id(node: dict[str, Any]) -> str:
    return str(node.get("id") or node.get("node_id") or "")


def _semantic_selector(raw: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(raw or {})
    scope = str(payload.get("scope") or payload.get("semantic_ai_scope") or "all").strip().lower() or "all"
    if scope in {"full", "*"}:
        scope = "all"
    if scope in {"off", "disabled"}:
        scope = "none"
    node_ids = _string_list(payload.get("node_ids") or payload.get("semantic_node_ids"))
    layers = [item.upper() for item in _string_list(payload.get("layers") or payload.get("semantic_layers"))]
    quality_flags = _string_list(payload.get("quality_flags") or payload.get("semantic_quality_flags"))
    missing = [
        item.lower().replace("-", "_")
        for item in _string_list(payload.get("missing") or payload.get("semantic_missing"))
    ]
    changed_paths = _path_list(payload.get("changed_paths") or payload.get("semantic_changed_paths"))
    path_prefixes = _path_list(payload.get("path_prefixes") or payload.get("semantic_path_prefixes"))
    match_mode = str(payload.get("match_mode") or payload.get("semantic_selector_match") or "all").strip().lower()
    if match_mode not in {"all", "any", "primary"}:
        match_mode = "all"
    include_structural = bool(payload.get("include_structural") or payload.get("semantic_include_structural"))
    if layers and any(layer in {"L1", "L2", "L3", "L4", "L5", "L6"} for layer in layers):
        include_structural = True
    if node_ids and any(str(item).upper().startswith(("L1.", "L2.", "L3.", "L4.", "L5.", "L6.")) for item in node_ids):
        include_structural = True
    return {
        "scope": scope,
        "node_ids": node_ids,
        "layers": layers,
        "quality_flags": quality_flags,
        "missing": missing,
        "changed_paths": changed_paths,
        "path_prefixes": path_prefixes,
        "match_mode": match_mode,
        "include_structural": include_structural,
    }


def _selector_from_kwargs(
    *,
    semantic_ai_scope: str | None = None,
    semantic_node_ids: Any = None,
    semantic_layers: Any = None,
    semantic_quality_flags: Any = None,
    semantic_missing: Any = None,
    semantic_changed_paths: Any = None,
    semantic_path_prefixes: Any = None,
    semantic_selector_match: str | None = None,
    semantic_include_structural: bool = False,
) -> dict[str, Any]:
    return _semantic_selector({
        "semantic_ai_scope": semantic_ai_scope,
        "semantic_node_ids": semantic_node_ids,
        "semantic_layers": semantic_layers,
        "semantic_quality_flags": semantic_quality_flags,
        "semantic_missing": semantic_missing,
        "semantic_changed_paths": semantic_changed_paths,
        "semantic_path_prefixes": semantic_path_prefixes,
        "semantic_selector_match": semantic_selector_match,
        "semantic_include_structural": semantic_include_structural,
    })


def _node_has_primary(node: dict[str, Any]) -> bool:
    return bool(_path_list(node.get("primary") or node.get("primary_files")))


def _node_excluded_from_default_semantics(node: dict[str, Any]) -> bool:
    metadata = dict(node.get("metadata") or {})
    if metadata.get("exclude_as_feature") is True:
        return True
    file_role = str(metadata.get("file_role") or "").strip().lower()
    if file_role in {
        "package_marker",
        "namespace_marker",
        "init_marker",
        "module_marker",
        "type_contract",
        "entrypoint_support",
    }:
        return True
    quality_flags = {
        str(item or "").strip().lower()
        for item in (metadata.get("quality_flags") or [])
    }
    return "coverage_noise_candidate" in quality_flags and file_role.endswith("marker")


def _semantic_candidate_nodes(graph_json: dict[str, Any], selector: dict[str, Any]) -> list[dict[str, Any]]:
    include_structural = bool(selector.get("include_structural"))
    explicit_node_ids = {str(item) for item in (selector.get("node_ids") or [])}
    out: list[dict[str, Any]] = []
    for node in _graph_nodes(graph_json):
        node_id = _node_id(node)
        if not node_id:
            continue
        if (
            not include_structural
            and node_id not in explicit_node_ids
            and _node_excluded_from_default_semantics(node)
        ):
            continue
        if _node_has_primary(node) or include_structural:
            out.append(node)
    return out


def _feature_context_from_node(
    node: dict[str, Any],
    *,
    feature_index: dict[str, dict[str, Any]],
    project_root: Path | None,
    max_excerpt_chars: int,
) -> dict[str, Any]:
    node_id = _node_id(node)
    metadata = dict(node.get("metadata") or {})
    primary = _path_list(node.get("primary") or node.get("primary_files"))
    secondary = _path_list(node.get("secondary") or node.get("secondary_files"))
    tests = _path_list(node.get("test") or node.get("test_files"))
    config = _path_list(node.get("config") or node.get("config_files") or metadata.get("config_files"))
    indexed = feature_index.get(node_id, {})
    source_excerpt: dict[str, str] = {}
    if project_root is not None and max_excerpt_chars > 0:
        budget = max_excerpt_chars
        for rel in primary + tests + secondary + config:
            if budget <= 0:
                break
            path = project_root / rel
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            excerpt = text[: min(len(text), budget)]
            source_excerpt[rel] = excerpt
            budget -= len(excerpt)
    fallback_hash_payload = {
        "node_id": node_id,
        "title": node.get("title") or "",
        "primary": primary,
        "secondary": secondary,
        "test": tests,
        "config": config,
        "functions": metadata.get("functions") or [],
    }
    return {
        "node_id": node_id,
        "title": str(node.get("title") or node_id),
        "layer": str(node.get("layer") or ""),
        "kind": str(node.get("kind") or metadata.get("kind") or ""),
        "primary": primary,
        "secondary": secondary,
        "test": tests,
        "config": config,
        "metadata": metadata,
        "file_hashes": indexed.get("file_hashes") or {},
        "feature_hash": indexed.get("feature_hash") or _hash_payload(fallback_hash_payload),
        "symbol_refs": indexed.get("symbol_refs") or [],
        "doc_refs": indexed.get("doc_refs") or [],
        "config_refs": indexed.get("config_refs") or [
            {"path": path, "kind": "config"} for path in config
        ],
        "source_excerpt": source_excerpt,
    }


def normalize_feedback_item(
    item: dict[str, Any],
    *,
    created_by: str = "observer",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Normalize one append-only review feedback item."""
    if not isinstance(item, dict):
        raise ValueError("feedback item must be an object")
    target_type = str(item.get("target_type") or "snapshot").strip() or "snapshot"
    if target_type not in {"snapshot", "node", "path", "edge"}:
        raise ValueError(f"invalid feedback target_type: {target_type}")
    priority = str(item.get("priority") or "P2").upper()
    if priority not in {"P0", "P1", "P2", "P3"}:
        priority = "P2"
    issue = str(item.get("issue") or item.get("comment") or "").strip()
    expected_change = str(item.get("expected_change") or item.get("suggestion") or "").strip()
    if not issue and not expected_change:
        raise ValueError("feedback item requires issue or expected_change")
    now = created_at or utc_now()
    raw_identity = {
        "target_type": target_type,
        "target_id": item.get("target_id") or item.get("node_id") or item.get("path") or "",
        "issue": issue,
        "expected_change": expected_change,
        "created_at": now,
    }
    feedback_id = str(item.get("feedback_id") or item.get("id") or "")
    if not feedback_id:
        feedback_id = f"fb-{uuid.uuid4().hex[:8]}"
    return {
        "feedback_id": feedback_id,
        "target_type": target_type,
        "target_id": str(
            item.get("target_id")
            or item.get("node_id")
            or item.get("path")
            or item.get("edge_id")
            or ""
        ),
        "node_id": str(item.get("node_id") or ""),
        "path": str(item.get("path") or ""),
        "edge": item.get("edge") if isinstance(item.get("edge"), dict) else {},
        "priority": priority,
        "issue": issue,
        "expected_change": expected_change,
        "status": str(item.get("status") or "open"),
        "created_by": str(item.get("created_by") or created_by or "observer"),
        "created_at": now,
        "evidence": item.get("evidence") if isinstance(item.get("evidence"), dict) else {},
        "fingerprint": _hash_payload(raw_identity)[:16],
    }


def append_review_feedback(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    feedback_items: list[dict[str, Any]] | dict[str, Any],
    *,
    created_by: str = "observer",
) -> dict[str, Any]:
    """Append review feedback to a snapshot companion JSONL artifact."""
    ensure_schema(conn)
    snapshot = get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    raw_items = feedback_items if isinstance(feedback_items, list) else [feedback_items]
    normalized = [
        normalize_feedback_item(item, created_by=created_by)
        for item in raw_items
        if isinstance(item, dict)
    ]
    path = _feedback_path(project_id, snapshot_id)
    _append_jsonl(path, normalized)
    all_feedback = _read_jsonl(path)
    _update_snapshot_notes(
        conn,
        project_id,
        snapshot_id,
        {
            "semantic_feedback": {
                "schema_version": SEMANTIC_ENRICHMENT_SCHEMA_VERSION,
                "feedback_path": str(path),
                "feedback_count": len(all_feedback),
                "latest_feedback_at": normalized[-1]["created_at"] if normalized else "",
            }
        },
    )
    return {
        "ok": True,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "feedback_path": str(path),
        "added_count": len(normalized),
        "feedback_count": len(all_feedback),
        "feedback": normalized,
    }


def load_review_feedback(project_id: str, snapshot_id: str) -> list[dict[str, Any]]:
    return _read_jsonl(_feedback_path(project_id, snapshot_id))


def _feedback_matches_feature(feedback: dict[str, Any], feature: dict[str, Any]) -> bool:
    target_type = str(feedback.get("target_type") or "")
    if target_type == "snapshot":
        return True
    node_id = str(feature.get("node_id") or "")
    if target_type == "node":
        return str(feedback.get("target_id") or feedback.get("node_id") or "") == node_id
    paths = set(feature.get("primary") or [])
    paths.update(feature.get("secondary") or [])
    paths.update(feature.get("test") or [])
    paths.update(feature.get("config") or [])
    if target_type == "path":
        target = str(feedback.get("target_id") or feedback.get("path") or "").replace("\\", "/").strip("/")
        return target in paths
    if target_type == "edge":
        edge = feedback.get("edge") if isinstance(feedback.get("edge"), dict) else {}
        return str(edge.get("src") or edge.get("source") or "") == node_id or str(
            edge.get("dst") or edge.get("target") or ""
        ) == node_id
    return False


def _quality_flags(feature: dict[str, Any], feedback: list[dict[str, Any]]) -> list[str]:
    flags: list[str] = []
    layer = str(feature.get("layer") or "")
    if not feature.get("secondary"):
        flags.append("missing_doc_binding")
    if layer == "L7" and not feature.get("test"):
        flags.append("missing_test_binding")
    if feedback:
        flags.append("has_review_feedback")
    if layer == "L7" and not feature.get("symbol_refs"):
        flags.append("missing_symbol_refs")
    return flags


def _feature_paths(feature: dict[str, Any], *, primary_only: bool = False) -> list[str]:
    paths: list[str] = []
    keys = ("primary",) if primary_only else ("primary", "secondary", "test", "config")
    for key in keys:
        paths.extend(_path_list(feature.get(key)))
    return paths


def _missing_matches(feature: dict[str, Any], missing: list[str]) -> bool:
    if not missing:
        return True
    checks = {
        "doc": not feature.get("secondary"),
        "docs": not feature.get("secondary"),
        "document": not feature.get("secondary"),
        "test": not feature.get("test"),
        "tests": not feature.get("test"),
        "config": not feature.get("config"),
        "symbol": not feature.get("symbol_refs"),
        "symbols": not feature.get("symbol_refs"),
    }
    return any(checks.get(item, False) for item in missing)


def _path_matches(
    feature: dict[str, Any],
    paths: list[str],
    prefixes: list[str],
    *,
    primary_only: bool = False,
) -> bool:
    feature_paths = _feature_paths(feature, primary_only=primary_only)
    if paths:
        requested = {path.replace("\\", "/").strip("/") for path in paths}
        if not requested.intersection(feature_paths):
            return False
    if prefixes:
        if not any(
            path == prefix or path.startswith(prefix.rstrip("/") + "/")
            for path in feature_paths
            for prefix in prefixes
        ):
            return False
    return True


def _selector_decision(
    feature: dict[str, Any],
    flags: list[str],
    selector: dict[str, Any],
) -> tuple[bool, list[str]]:
    scope = str(selector.get("scope") or "all").lower()
    if scope == "none":
        return False, ["scope_none"]
    node_ids = set(selector.get("node_ids") or [])
    layers = set(selector.get("layers") or [])
    quality_flags = set(selector.get("quality_flags") or [])
    missing = list(selector.get("missing") or [])
    changed_paths = list(selector.get("changed_paths") or [])
    path_prefixes = list(selector.get("path_prefixes") or [])
    has_filters = bool(node_ids or layers or quality_flags or missing or changed_paths or path_prefixes)
    if scope == "all" and not has_filters:
        return True, ["scope_all"]
    if scope in {"selected", "partial", "issues", "changed"} and not has_filters:
        return False, [f"scope_{scope}_requires_filter"]

    checks: list[tuple[str, bool]] = []
    if node_ids:
        checks.append(("node_id", str(feature.get("node_id") or "") in node_ids))
    if layers:
        checks.append(("layer", str(feature.get("layer") or "").upper() in layers))
    if quality_flags:
        checks.append(("quality_flags", bool(set(flags).intersection(quality_flags))))
    if missing:
        checks.append(("missing", _missing_matches(feature, missing)))
    match_mode = str(selector.get("match_mode") or "all")
    if changed_paths or path_prefixes:
        checks.append((
            "paths",
            _path_matches(
                feature,
                changed_paths,
                path_prefixes,
                primary_only=match_mode == "primary",
            ),
        ))
    if not checks:
        return scope == "all", ["scope_all"] if scope == "all" else ["not_selected"]

    matched = any(ok for _, ok in checks) if match_mode == "any" else all(ok for _, ok in checks)
    reasons = [name for name, ok in checks if ok] or ["selector_no_match"]
    return matched, reasons


def _heuristic_semantic_entry(
    feature: dict[str, Any],
    feedback: list[dict[str, Any]],
    *,
    enrichment_status: str,
    ai_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ai_response = ai_response or {}
    feedback_ids = [str(item.get("feedback_id") or "") for item in feedback if item.get("feedback_id")]
    applied = _path_list(ai_response.get("applied_feedback_ids"))
    rejected = _path_list(ai_response.get("rejected_feedback_ids"))
    unresolved = [
        feedback_id
        for feedback_id in feedback_ids
        if feedback_id and feedback_id not in set(applied) and feedback_id not in set(rejected)
    ]
    summary = str(ai_response.get("semantic_summary") or ai_response.get("purpose") or "")
    if not summary:
        primary = ", ".join(feature.get("primary") or []) or "no primary files"
        if feature.get("primary"):
            summary = f"{feature.get('title') or feature.get('node_id')} covers {primary}."
        else:
            summary = (
                f"{feature.get('title') or feature.get('node_id')} is a "
                f"{feature.get('layer') or 'graph'} structural governance node."
            )
    self_check = _node_semantic_self_check(
        ai_response,
        required=bool(ai_response) and enrichment_status == "ai_complete",
    )
    return {
        "node_id": feature.get("node_id") or "",
        "source_title": feature.get("title") or "",
        "feature_name": str(ai_response.get("feature_name") or feature.get("title") or feature.get("node_id") or ""),
        "semantic_summary": summary,
        "intent": str(ai_response.get("intent") or ai_response.get("purpose") or ""),
        "domain_label": str(ai_response.get("domain_label") or ""),
        "purpose": str(ai_response.get("purpose") or summary),
        "merge_suggestions": ai_response.get("merge_suggestions") or [],
        "split_suggestions": ai_response.get("split_suggestions") or [],
        "dependency_patch_suggestions": ai_response.get("dependency_patch_suggestions") or [],
        "graph_structure_suggestions": ai_response.get("graph_structure_suggestions") or [],
        "graph_structure_candidates": ai_response.get("graph_structure_candidates") or [],
        "graph_structure_ops": ai_response.get("graph_structure_ops") or {},
        "graph_enrich_config_suggestions": ai_response.get("graph_enrich_config_suggestions") or [],
        "graph_enrich_config_candidates": ai_response.get("graph_enrich_config_candidates") or [],
        "graph_enrich_config_ops": ai_response.get("graph_enrich_config_ops") or {},
        "doc_coverage_review": ai_response.get("doc_coverage_review") or {
            "bound": bool(feature.get("secondary")),
            "files": feature.get("secondary") or [],
        },
        "test_coverage_review": ai_response.get("test_coverage_review") or {
            "bound": bool(feature.get("test")),
            "files": feature.get("test") or [],
        },
        "config_coverage_review": ai_response.get("config_coverage_review") or {
            "bound": bool(feature.get("config")),
            "files": feature.get("config") or [],
        },
        "dead_code_candidates": ai_response.get("dead_code_candidates") or [],
        "quality_flags": _quality_flags(feature, feedback),
        "health_issues": ai_response.get("health_issues") or [],
        "self_check": self_check,
        "semantic_ai_self_check": self_check,
        "applied_feedback_ids": applied,
        "rejected_feedback_ids": rejected,
        "unresolved_feedback_ids": unresolved,
        "feedback_count": len(feedback),
        "feature_hash": feature.get("feature_hash") or "",
        "file_hashes": feature.get("file_hashes") or {},
        "primary": feature.get("primary") or [],
        "secondary": feature.get("secondary") or [],
        "test": feature.get("test") or [],
        "config": feature.get("config") or [],
        "symbol_refs": feature.get("symbol_refs") or [],
        "doc_refs": feature.get("doc_refs") or [],
        "config_refs": feature.get("config_refs") or [],
        "enrichment_status": enrichment_status,
        "layer": feature.get("layer") or "",
        "kind": feature.get("kind") or "",
    }


def _call_ai(
    ai_call: FeedbackAiCall | None,
    *,
    stage: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if ai_call is None:
        return None
    try:
        response = ai_call(stage, payload)
    except Exception as exc:  # noqa: BLE001 - caller records unavailable AI evidence
        return {"_ai_error": str(exc)}
    return response if isinstance(response, dict) else None


def _normal_batch_size(raw: int | None) -> int:
    if raw is None:
        return 1
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, value)


def _normal_ai_input_mode(raw: str | None, *, default: str = "feature") -> str:
    mode = str(raw or default or "feature").strip().lower().replace("-", "_")
    if mode in {"feature", "single", "single_feature", "per_feature", "dynamic_feature"}:
        return "feature"
    if mode in {"batch", "batched", "batch_features"}:
        return "batch"
    return "feature"


def _batch_key(feature: dict[str, Any], batch_by: str) -> str:
    mode = (batch_by or "subsystem").strip().lower()
    metadata = feature.get("metadata") if isinstance(feature.get("metadata"), dict) else {}
    if mode in {"none", "order", "flat"}:
        return "all"
    if mode in {"layer", "layers"}:
        return str(feature.get("layer") or "unknown")
    if mode in {"kind", "role"}:
        return str(feature.get("kind") or metadata.get("kind") or "unknown")
    if mode in {"subsystem", "feature", "feature_group", "group"}:
        return str(
            metadata.get("subsystem")
            or metadata.get("hierarchy_parent")
            or metadata.get("parent")
            or metadata.get("cluster_parent")
            or metadata.get("feature_cluster")
            or metadata.get("cluster")
            or feature.get("kind")
            or feature.get("layer")
            or "unknown"
        )
    return str(metadata.get(mode) or "unknown")


def _batch_records(
    records: list[dict[str, Any]],
    *,
    batch_size: int,
    batch_by: str,
) -> list[list[dict[str, Any]]]:
    if batch_size <= 1:
        return [[record] for record in records]
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for record in records:
        key = _batch_key(record["feature"], batch_by)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(record)
    batches: list[list[dict[str, Any]]] = []
    for key in order:
        group = groups[key]
        for idx in range(0, len(group), batch_size):
            batches.append(group[idx: idx + batch_size])
    return batches


def _extract_batch_ai_responses(
    response: dict[str, Any] | None,
    records: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    node_ids = [str(record["feature"].get("node_id") or "") for record in records]
    if not response:
        return {}
    if response.get("_ai_error"):
        return {node_id: {"_ai_error": response.get("_ai_error")} for node_id in node_ids}

    raw_items: Any = (
        response.get("features")
        or response.get("semantic_features")
        or response.get("nodes")
        or response.get("results")
    )
    if isinstance(raw_items, dict):
        items = []
        for node_id, payload in raw_items.items():
            if isinstance(payload, dict):
                item = dict(payload)
                item.setdefault("node_id", str(node_id))
                items.append(item)
        raw_items = items
    if not isinstance(raw_items, list):
        if len(node_ids) == 1:
            return {node_ids[0]: dict(response)}
        return {
            node_id: {"_ai_error": "semantic AI batch returned no features array"}
            for node_id in node_ids
        }

    route = response.get("_ai_route")
    elapsed = response.get("_ai_elapsed_ms")
    top_level_self_check = (
        response.get("self_check") if isinstance(response.get("self_check"), dict) else None
    )
    out: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("node_id") or item.get("id") or "")
        if not node_id:
            continue
        payload = dict(item)
        if route and not payload.get("_ai_route"):
            payload["_ai_route"] = route
        if elapsed is not None and payload.get("_ai_elapsed_ms") is None:
            payload["_ai_elapsed_ms"] = elapsed
        if top_level_self_check and not isinstance(payload.get("self_check"), dict):
            payload["self_check"] = dict(top_level_self_check)
        out[node_id] = payload
    for node_id in node_ids:
        out.setdefault(node_id, {"_ai_error": "semantic AI batch omitted node_id"})
    return out


def _safe_node_filename(node_id: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in node_id)
    return safe or "feature"


def _semantic_batch_memory_id(snapshot_id: str, round_number: int, explicit: str | None = None) -> str:
    if explicit:
        return _safe_node_filename(str(explicit))
    return f"semantic-{_safe_node_filename(snapshot_id)}-round-{int(round_number):03d}"


def _create_semantic_batch_memory(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    round_number: int,
    *,
    created_by: str,
    batch_id: str | None = None,
) -> tuple[dict[str, Any], str]:
    try:
        from . import reconcile_batch_memory as bm

        bid = _semantic_batch_memory_id(snapshot_id, round_number, batch_id)
        batch = bm.create_or_get_batch(
            conn,
            project_id,
            session_id=snapshot_id,
            batch_id=bid,
            created_by=created_by,
            initial_memory={
                "semantic_enrichment": {
                    "snapshot_id": snapshot_id,
                    "round": round_number,
                    "created_by": created_by,
                }
            },
        )
        return batch, ""
    except Exception as exc:  # noqa: BLE001 - semantic memory is advisory
        return {}, str(exc)


def _refresh_semantic_batch_memory(
    conn: sqlite3.Connection,
    project_id: str,
    batch_id: str,
) -> dict[str, Any]:
    if not batch_id:
        return {}
    try:
        from . import reconcile_batch_memory as bm

        return bm.get_batch(conn, project_id, batch_id)
    except Exception:  # noqa: BLE001 - keep semantic enrichment retryable
        return {}


def _shorten_text(value: Any, limit: int = 300) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _compact_memory_conflict(conflict: Any) -> dict[str, Any]:
    if not isinstance(conflict, dict):
        return {"reason": _shorten_text(conflict, 160)}
    out = {
        "reason": _shorten_text(conflict.get("reason"), 160),
        "cluster_fingerprint": _shorten_text(conflict.get("cluster_fingerprint"), 120),
        "file": _shorten_text(conflict.get("file"), 200),
        "owner_feature": _shorten_text(conflict.get("owner_feature"), 160),
        "claimant_feature": _shorten_text(conflict.get("claimant_feature"), 160),
    }
    items = conflict.get("items")
    if isinstance(items, list) and items:
        compact_items: list[dict[str, Any]] = []
        for item in items[:5]:
            if isinstance(item, dict):
                compact_items.append({
                    "type": _shorten_text(item.get("type") or item.get("kind"), 120),
                    "target": _shorten_text(item.get("target") or item.get("suggested_target"), 200),
                    "reason": _shorten_text(item.get("reason") or item.get("rationale"), 300),
                    "proposed_action": _shorten_text(item.get("proposed_action"), 300),
                })
            else:
                compact_items.append({"reason": _shorten_text(item, 300)})
        out["items"] = compact_items
        if len(items) > len(compact_items):
            out["omitted_item_count"] = len(items) - len(compact_items)
    return {
        key: value
        for key, value in out.items()
        if value != "" and value != [] and value != {}
    }


def _compact_health_issue(issue: Any) -> dict[str, Any]:
    if not isinstance(issue, dict):
        return {"summary": _shorten_text(issue, 240)}
    return {
        key: value
        for key, value in {
            "issue_id": str(issue.get("issue_id") or ""),
            "node_id": str(issue.get("node_id") or ""),
            "category": str(issue.get("category") or ""),
            "severity": str(issue.get("severity") or ""),
            "confidence": issue.get("confidence"),
            "source": str(issue.get("source") or ""),
            "summary": _shorten_text(issue.get("summary"), 300),
            "suggested_action": _shorten_text(issue.get("suggested_action"), 300),
            "affected_node_ids": _path_list(issue.get("affected_node_ids"))[:10],
        }.items()
        if value not in ("", [], {}, None)
    }


def _semantic_batch_memory_summary(batch: dict[str, Any]) -> dict[str, Any]:
    memory = batch.get("memory") if isinstance(batch, dict) else {}
    memory = memory if isinstance(memory, dict) else {}
    accepted = memory.get("accepted_features") if isinstance(memory.get("accepted_features"), dict) else {}
    features: list[dict[str, Any]] = []
    for name in sorted(accepted):
        item = accepted.get(name) if isinstance(accepted.get(name), dict) else {}
        features.append({
            "feature_name": name,
            "purpose": _shorten_text(item.get("purpose"), 360),
            "clusters": _path_list(item.get("clusters")),
            "owned_files": _path_list(item.get("owned_files"))[:20],
            "shared_files": _path_list(item.get("shared_files"))[:20],
            "candidate_tests": _path_list(item.get("candidate_tests"))[:20],
            "candidate_docs": _path_list(item.get("candidate_docs"))[:20],
        })
    conflicts = memory.get("open_conflicts") if isinstance(memory.get("open_conflicts"), list) else []
    return {
        "schema_version": memory.get("schema_version") or 1,
        "batch_id": batch.get("batch_id") or memory.get("batch_id") or "",
        "session_id": batch.get("session_id") or memory.get("session_id") or "",
        "accepted_feature_count": len(features),
        "file_ownership_count": len(memory.get("file_ownership") or {}),
        "open_conflict_count": len(conflicts),
        "reserved_names": _path_list(memory.get("reserved_names"))[:200],
        "accepted_features": features,
        "open_conflicts": [_compact_memory_conflict(item) for item in conflicts[-20:]],
    }


def _empty_semantic_graph_state(
    project_id: str,
    snapshot_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": SEMANTIC_ENRICHMENT_SCHEMA_VERSION,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "snapshot_kind": snapshot.get("snapshot_kind") or "",
        "commit_sha": snapshot.get("commit_sha") or "",
        "node_semantics": {},
        "accepted_features": {},
        "file_ownership": {},
        "open_issues": [],
        "health_issues": [],
        "completed_node_ids": [],
        "semantic_jobs": {},
        "semantic_job_counts": {},
        "updated_at": "",
    }


def _load_semantic_graph_state(
    project_id: str,
    snapshot_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    path = _semantic_base_dir(project_id, snapshot_id) / SEMANTIC_GRAPH_STATE_NAME
    payload = _read_json(path, {})
    if not isinstance(payload, dict):
        payload = {}
    state = _empty_semantic_graph_state(project_id, snapshot_id, snapshot)
    state.update(payload)
    if not isinstance(state.get("node_semantics"), dict):
        state["node_semantics"] = {}
    if not isinstance(state.get("semantic_jobs"), dict):
        state["semantic_jobs"] = {}
    _rebuild_semantic_graph_state_indexes(state)
    return state


def _load_semantic_graph_state_from_db(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    _ensure_semantic_state_schema(conn)
    state = _empty_semantic_graph_state(project_id, snapshot_id, snapshot)
    node_rows = conn.execute(
        """
        SELECT node_id, status, feature_hash, file_hashes_json, semantic_json,
               branch_ref, operation_type, source_branch_ref, source_snapshot_id,
               source_event_id, payload_hash
        FROM graph_semantic_nodes
        WHERE project_id = ? AND snapshot_id = ?
        ORDER BY node_id
        """,
        (project_id, snapshot_id),
    ).fetchall()
    node_semantics: dict[str, dict[str, Any]] = {}
    for row in node_rows:
        try:
            payload = json.loads(row["semantic_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            payload["feature_hash"] = str(row["feature_hash"] or payload.get("feature_hash") or "")
            try:
                file_hashes = json.loads(row["file_hashes_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                file_hashes = {}
            payload["file_hashes"] = file_hashes if isinstance(file_hashes, dict) else {}
            payload["status"] = str(row["status"] or payload.get("status") or "")
            payload["branch_ref"] = str(row["branch_ref"] or payload.get("branch_ref") or "")
            payload["operation_type"] = str(row["operation_type"] or payload.get("operation_type") or "")
            payload["source_branch_ref"] = str(row["source_branch_ref"] or payload.get("source_branch_ref") or "")
            payload["source_snapshot_id"] = str(row["source_snapshot_id"] or payload.get("source_snapshot_id") or "")
            payload["source_event_id"] = str(row["source_event_id"] or payload.get("source_event_id") or "")
            payload["payload_hash"] = str(row["payload_hash"] or payload.get("payload_hash") or "")
            node_semantics[str(row["node_id"])] = payload
    edge_rows = conn.execute(
        """
        SELECT edge_id, status, edge_signature_hash, semantic_json,
               branch_ref, operation_type, source_branch_ref, source_snapshot_id,
               source_event_id, payload_hash
        FROM graph_semantic_edges
        WHERE project_id = ? AND snapshot_id = ?
        ORDER BY edge_id
        """,
        (project_id, snapshot_id),
    ).fetchall()
    edge_semantics: dict[str, dict[str, Any]] = {}
    for row in edge_rows:
        try:
            payload = json.loads(row["semantic_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            payload["edge_signature_hash"] = str(
                row["edge_signature_hash"] or payload.get("edge_signature_hash") or ""
            )
            payload["status"] = str(row["status"] or payload.get("status") or "")
            payload["branch_ref"] = str(row["branch_ref"] or payload.get("branch_ref") or "")
            payload["operation_type"] = str(row["operation_type"] or payload.get("operation_type") or "")
            payload["source_branch_ref"] = str(row["source_branch_ref"] or payload.get("source_branch_ref") or "")
            payload["source_snapshot_id"] = str(row["source_snapshot_id"] or payload.get("source_snapshot_id") or "")
            payload["source_event_id"] = str(row["source_event_id"] or payload.get("source_event_id") or "")
            payload["payload_hash"] = str(row["payload_hash"] or payload.get("payload_hash") or "")
            edge_semantics[str(row["edge_id"])] = payload
    job_rows = conn.execute(
        """
        SELECT node_id, status, feature_hash, file_hashes_json, branch_ref,
               operation_type, feedback_round,
               batch_index, attempt_count, worker_id, claim_id, claimed_at,
               lease_expires_at, claimed_by, last_error, updated_at, created_at
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ?
        ORDER BY node_id
        """,
        (project_id, snapshot_id),
    ).fetchall()
    semantic_jobs: dict[str, dict[str, Any]] = {}
    for row in job_rows:
        try:
            file_hashes = json.loads(row["file_hashes_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            file_hashes = {}
        semantic_jobs[str(row["node_id"])] = {
            "node_id": str(row["node_id"]),
            "status": str(row["status"] or ""),
            "feature_hash": str(row["feature_hash"] or ""),
            "file_hashes": file_hashes if isinstance(file_hashes, dict) else {},
            "branch_ref": str(row["branch_ref"] or ""),
            "operation_type": str(row["operation_type"] or ""),
            "feedback_round": int(row["feedback_round"] or 0),
            "batch_index": row["batch_index"],
            "attempt_count": int(row["attempt_count"] or 0),
            "worker_id": str(row["worker_id"] or ""),
            "claim_id": str(row["claim_id"] or ""),
            "claimed_at": str(row["claimed_at"] or ""),
            "lease_expires_at": str(row["lease_expires_at"] or ""),
            "claimed_by": str(row["claimed_by"] or ""),
            "last_error": str(row["last_error"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "created_at": str(row["created_at"] or ""),
        }
    state["node_semantics"] = node_semantics
    state["edge_semantics"] = edge_semantics
    state["semantic_jobs"] = semantic_jobs
    _rebuild_semantic_graph_state_indexes(state)
    return state


def _persist_semantic_state_to_db(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    state: dict[str, Any],
    *,
    submit_for_review: bool = False,
    review_node_ids: set[str] | None = None,
    review_edge_ids: set[str] | None = None,
) -> None:
    """Persist node_semantics + semantic_jobs into the per-snapshot tables.

    MF-2026-05-10-016: when `submit_for_review` is True, force freshly
    enriched graph_semantic_nodes rows to `pending_review` regardless of the
    record's own status. `review_node_ids`/`review_edge_ids` scope that
    override for selected-node worker runs so accepted rows loaded from the
    existing semantic state are not downgraded by a later batch. Passing None
    preserves the legacy direct-call behavior: all non-carried-forward rows
    are submitted for review.
    """
    _ensure_semantic_state_schema(conn)
    node_semantics = state.get("node_semantics") if isinstance(state.get("node_semantics"), dict) else {}
    for node_id, raw_entry in node_semantics.items():
        if not isinstance(raw_entry, dict):
            continue
        # MF-2026-05-10-016 scoping: submit_for_review only affects rows that
        # were freshly enriched in this run. Carried-forward rows (those with
        # `carried_forward_from_snapshot_id` set by _carry_forward_semantic_graph_state)
        # keep their original status — they were already accepted in a prior
        # snapshot and don't need re-review just because the worker happened
        # to call run_semantic_enrichment.
        is_carried_forward = bool(raw_entry.get("carried_forward_from_snapshot_id"))
        should_submit_node = (
            submit_for_review
            and not is_carried_forward
            and (review_node_ids is None or str(node_id) in review_node_ids)
        )
        if should_submit_node:
            row_status = "pending_review"
        else:
            row_status = str(raw_entry.get("status") or "")
        row_operation = str(raw_entry.get("operation_type") or "").strip()
        if not row_operation:
            row_operation = "carry_forward" if is_carried_forward else "ai_enrich"
        row_payload_hash = str(raw_entry.get("payload_hash") or "").strip() or _hash_payload(raw_entry)
        conn.execute(
            """
            INSERT INTO graph_semantic_nodes
              (project_id, snapshot_id, node_id, status, feature_hash,
               file_hashes_json, semantic_json, branch_ref, operation_type,
               source_branch_ref, source_snapshot_id, source_event_id, payload_hash,
               feedback_round, batch_index, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, snapshot_id, node_id) DO UPDATE SET
              status = excluded.status,
              feature_hash = excluded.feature_hash,
              file_hashes_json = excluded.file_hashes_json,
              semantic_json = excluded.semantic_json,
              branch_ref = excluded.branch_ref,
              operation_type = excluded.operation_type,
              source_branch_ref = excluded.source_branch_ref,
              source_snapshot_id = excluded.source_snapshot_id,
              source_event_id = excluded.source_event_id,
              payload_hash = excluded.payload_hash,
              feedback_round = excluded.feedback_round,
              batch_index = excluded.batch_index,
              updated_at = excluded.updated_at
            """,
            (
                project_id,
                snapshot_id,
                str(node_id),
                row_status,
                str(raw_entry.get("feature_hash") or ""),
                _json(raw_entry.get("file_hashes") or {}),
                _json(raw_entry),
                str(raw_entry.get("branch_ref") or state.get("branch_ref") or ""),
                row_operation,
                str(raw_entry.get("source_branch_ref") or raw_entry.get("source_branch") or ""),
                str(raw_entry.get("source_snapshot_id") or raw_entry.get("carried_forward_from_snapshot_id") or ""),
                str(raw_entry.get("source_event_id") or ""),
                row_payload_hash,
                int(raw_entry.get("feedback_round") or 0),
                raw_entry.get("batch_index"),
                str(raw_entry.get("updated_at") or state.get("updated_at") or ""),
            ),
        )
    # MF 2026-05-11: edge_semantics mirror — same UPSERT shape as nodes,
    # keyed by edge_id, drift detector is edge_signature_hash (computed
    # by the adapter, stashed in entry['edge_signature_hash']).
    edge_semantics = state.get("edge_semantics") if isinstance(state.get("edge_semantics"), dict) else {}
    for edge_id, raw_entry in edge_semantics.items():
        if not isinstance(raw_entry, dict):
            continue
        is_carried_forward = bool(raw_entry.get("carried_forward_from_snapshot_id"))
        should_submit_edge = (
            submit_for_review
            and not is_carried_forward
            and (review_edge_ids is None or str(edge_id) in review_edge_ids)
        )
        if should_submit_edge:
            edge_row_status = "pending_review"
        else:
            edge_row_status = str(raw_entry.get("status") or "")
        edge_row_operation = str(raw_entry.get("operation_type") or "").strip()
        if not edge_row_operation:
            edge_row_operation = "carry_forward" if is_carried_forward else "ai_enrich"
        edge_payload_hash = str(raw_entry.get("payload_hash") or "").strip() or _hash_payload(raw_entry)
        conn.execute(
            """
            INSERT INTO graph_semantic_edges
              (project_id, snapshot_id, edge_id, status, edge_signature_hash,
               semantic_json, branch_ref, operation_type, source_branch_ref,
               source_snapshot_id, source_event_id, payload_hash,
               feedback_round, batch_index, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, snapshot_id, edge_id) DO UPDATE SET
              status = excluded.status,
              edge_signature_hash = excluded.edge_signature_hash,
              semantic_json = excluded.semantic_json,
              branch_ref = excluded.branch_ref,
              operation_type = excluded.operation_type,
              source_branch_ref = excluded.source_branch_ref,
              source_snapshot_id = excluded.source_snapshot_id,
              source_event_id = excluded.source_event_id,
              payload_hash = excluded.payload_hash,
              feedback_round = excluded.feedback_round,
              batch_index = excluded.batch_index,
              updated_at = excluded.updated_at
            """,
            (
                project_id,
                snapshot_id,
                str(edge_id),
                edge_row_status,
                str(raw_entry.get("edge_signature_hash") or ""),
                _json(raw_entry),
                str(raw_entry.get("branch_ref") or state.get("branch_ref") or ""),
                edge_row_operation,
                str(raw_entry.get("source_branch_ref") or raw_entry.get("source_branch") or ""),
                str(raw_entry.get("source_snapshot_id") or raw_entry.get("carried_forward_from_snapshot_id") or ""),
                str(raw_entry.get("source_event_id") or ""),
                edge_payload_hash,
                int(raw_entry.get("feedback_round") or 0),
                raw_entry.get("batch_index"),
                str(raw_entry.get("updated_at") or state.get("updated_at") or ""),
            ),
        )
    semantic_jobs = state.get("semantic_jobs") if isinstance(state.get("semantic_jobs"), dict) else {}
    for node_id, raw_job in semantic_jobs.items():
        if not isinstance(raw_job, dict):
            continue
        existing_job = conn.execute(
            """
            SELECT status, updated_at
            FROM graph_semantic_jobs
            WHERE project_id = ? AND snapshot_id = ? AND node_id = ?
            """,
            (project_id, snapshot_id, str(node_id)),
        ).fetchone()
        if _semantic_job_existing_terminal_wins(existing_job, raw_job):
            continue
        conn.execute(
            """
            INSERT INTO graph_semantic_jobs
              (project_id, snapshot_id, node_id, status, feature_hash,
               file_hashes_json, branch_ref, operation_type,
               feedback_round, batch_index, attempt_count,
               worker_id, claim_id, claimed_at, lease_expires_at, claimed_by,
               last_error, updated_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, snapshot_id, node_id) DO UPDATE SET
              status = excluded.status,
              feature_hash = excluded.feature_hash,
              file_hashes_json = excluded.file_hashes_json,
              branch_ref = excluded.branch_ref,
              operation_type = excluded.operation_type,
              feedback_round = excluded.feedback_round,
              batch_index = excluded.batch_index,
              attempt_count = excluded.attempt_count,
              worker_id = excluded.worker_id,
              claim_id = excluded.claim_id,
              claimed_at = excluded.claimed_at,
              lease_expires_at = excluded.lease_expires_at,
              claimed_by = excluded.claimed_by,
              last_error = excluded.last_error,
              updated_at = excluded.updated_at
            """,
            (
                project_id,
                snapshot_id,
                str(node_id),
                str(raw_job.get("status") or "pending_ai"),
                str(raw_job.get("feature_hash") or ""),
                _json(raw_job.get("file_hashes") or {}),
                str(raw_job.get("branch_ref") or state.get("branch_ref") or ""),
                str(raw_job.get("operation_type") or "ai_enrich"),
                int(raw_job.get("feedback_round") or 0),
                raw_job.get("batch_index"),
                int(raw_job.get("attempt_count") or 0),
                str(raw_job.get("worker_id") or ""),
                str(raw_job.get("claim_id") or ""),
                str(raw_job.get("claimed_at") or ""),
                str(raw_job.get("lease_expires_at") or ""),
                str(raw_job.get("claimed_by") or ""),
                str(raw_job.get("last_error") or ""),
                str(raw_job.get("updated_at") or state.get("updated_at") or ""),
                str(raw_job.get("created_at") or raw_job.get("updated_at") or state.get("updated_at") or ""),
            ),
        )


def _load_semantic_graph_state_source(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    state = _load_semantic_graph_state_from_db(conn, project_id, snapshot_id, snapshot)
    if state.get("node_semantics") or state.get("edge_semantics") or state.get("semantic_jobs"):
        state["source"] = "db"
        return state
    # One-time compatibility import for snapshots created before DB-backed
    # semantic state. After this point DB rows are the source of truth.
    companion_state = _load_semantic_graph_state(project_id, snapshot_id, snapshot)
    if companion_state.get("node_semantics") or companion_state.get("edge_semantics") or companion_state.get("semantic_jobs"):
        with _semantic_write_lock():
            _persist_semantic_state_to_db(conn, project_id, snapshot_id, companion_state)
            _commit_semantic_write(conn)
        companion_state["source"] = "imported_companion"
        return companion_state
    state["source"] = "db_empty"
    return state


def _upsert_semantic_job(
    state: dict[str, Any],
    feature: dict[str, Any],
    *,
    status: str,
    feedback_round: int,
    batch_index: int | None,
    updated_at: str,
    last_error: str = "",
    increment_attempt: bool = False,
) -> None:
    node_id = str(feature.get("node_id") or "")
    if not node_id:
        return
    jobs = state.setdefault("semantic_jobs", {})
    if not isinstance(jobs, dict):
        jobs = {}
        state["semantic_jobs"] = jobs
    existing = jobs.get(node_id) if isinstance(jobs.get(node_id), dict) else {}
    attempt_count = int(existing.get("attempt_count") or 0) + (1 if increment_attempt else 0)
    now = updated_at
    keep_claim = status == "running"
    jobs[node_id] = {
        "node_id": node_id,
        "status": status,
        "feature_hash": str(feature.get("feature_hash") or ""),
        "file_hashes": feature.get("file_hashes") or {},
        "feedback_round": int(feedback_round or 0),
        "batch_index": batch_index,
        "attempt_count": attempt_count,
        "worker_id": str(existing.get("worker_id") or "") if keep_claim else "",
        "claim_id": str(existing.get("claim_id") or "") if keep_claim else "",
        "claimed_at": str(existing.get("claimed_at") or "") if keep_claim else "",
        "lease_expires_at": str(existing.get("lease_expires_at") or "") if keep_claim else "",
        "claimed_by": str(existing.get("claimed_by") or "") if keep_claim else "",
        "last_error": last_error,
        "updated_at": now,
        "created_at": existing.get("created_at") or now,
    }
    state["updated_at"] = now
    _rebuild_semantic_graph_state_indexes(state)


def _parse_utc(raw: Any) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _semantic_job_claim_visible(row: sqlite3.Row, worker_id: str, now: str) -> bool:
    lease = str(row["lease_expires_at"] or "")
    owner = str(row["worker_id"] or "")
    if not lease:
        return True
    expires = _parse_utc(lease)
    if expires is None:
        return True
    if expires <= _parse_utc(now):
        return True
    return bool(worker_id and owner == worker_id)


def claim_semantic_jobs(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    worker_id: str,
    statuses: list[str] | tuple[str, ...] | None = None,
    limit: int = 10,
    lease_seconds: int = 1800,
    actor: str = "observer",
) -> dict[str, Any]:
    """Claim semantic AI jobs using short DB writes and per-row leases.

    This is the concurrency-safe primitive for executor-backed semantic
    runners.  Model calls should happen after this returns, outside the DB lock.
    """
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        raise ValueError("worker_id is required")
    _ensure_semantic_state_schema(conn)
    allowed_statuses = [
        str(item or "").strip()
        for item in (statuses or ("pending_ai", "ai_pending", "ai_failed"))
        if str(item or "").strip()
    ]
    if not allowed_statuses:
        allowed_statuses = ["pending_ai", "ai_pending", "ai_failed"]
    limit = max(1, min(int(limit or 10), 500))
    lease_seconds = max(30, min(int(lease_seconds or 1800), 24 * 60 * 60))
    now = utc_now()
    now_dt = _parse_utc(now) or datetime.now(timezone.utc)
    lease_expires_at = (
        now_dt + timedelta(seconds=lease_seconds)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    placeholders = ",".join("?" for _ in allowed_statuses)
    claim_id = f"sjc-{uuid.uuid4().hex[:10]}"
    with _semantic_write_lock():
        rows = conn.execute(
            f"""
            SELECT *
            FROM graph_semantic_jobs
            WHERE project_id = ?
              AND snapshot_id = ?
              AND status IN ({placeholders})
            ORDER BY updated_at, node_id
            LIMIT ?
            """,
            (project_id, snapshot_id, *allowed_statuses, limit * 3),
        ).fetchall()
        claimed: list[dict[str, Any]] = []
        for row in rows:
            if len(claimed) >= limit:
                break
            if not _semantic_job_claim_visible(row, worker_id, now):
                continue
            cur = conn.execute(
                """
                UPDATE graph_semantic_jobs
                SET status = ?,
                    worker_id = ?,
                    claim_id = ?,
                    claimed_at = ?,
                    lease_expires_at = ?,
                    claimed_by = ?,
                    attempt_count = attempt_count + 1,
                    updated_at = ?
                WHERE project_id = ?
                  AND snapshot_id = ?
                  AND node_id = ?
                  AND status = ?
                  AND (
                    lease_expires_at = ''
                    OR lease_expires_at <= ?
                    OR worker_id = ?
                  )
                """,
                (
                    "running",
                    worker_id,
                    claim_id,
                    now,
                    lease_expires_at,
                    str(actor or worker_id),
                    now,
                    project_id,
                    snapshot_id,
                    str(row["node_id"] or ""),
                    str(row["status"] or ""),
                    now,
                    worker_id,
                ),
            )
            if int(cur.rowcount or 0) != 1:
                continue
            try:
                file_hashes = json.loads(row["file_hashes_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                file_hashes = {}
            claimed.append({
                "node_id": str(row["node_id"] or ""),
                "previous_status": str(row["status"] or ""),
                "status": "running",
                "feature_hash": str(row["feature_hash"] or ""),
                "file_hashes": file_hashes if isinstance(file_hashes, dict) else {},
                "feedback_round": int(row["feedback_round"] or 0),
                "batch_index": row["batch_index"],
                "attempt_count": int(row["attempt_count"] or 0) + 1,
                "worker_id": worker_id,
                "claim_id": claim_id,
                "claimed_at": now,
                "lease_expires_at": lease_expires_at,
            })
        _commit_semantic_write(conn)
    return {
        "ok": True,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "worker_id": worker_id,
        "claim_id": claim_id,
        "lease_expires_at": lease_expires_at,
        "claimed_count": len(claimed),
        "jobs": claimed,
    }


def _projection_current_node_ids(
    conn: sqlite3.Connection,
    project_id: str,
    base_snapshot_id: str | None,
) -> set[str]:
    """Return node ids the base snapshot's projection considers semantic-current.

    Used by `run_semantic_enrichment` to avoid spuriously re-enqueuing
    `ai_pending` rows for nodes that the base projection already labels
    `semantic_current` or `semantic_carried_forward_current`. Without this
    filter, every dashboard scope-reconcile click queued ~90 phantom rows
    because the legacy per-snapshot persistent layer (state.json /
    graph_semantic_nodes) only stored the freshly enriched subset, while the
    projection (event-derived) carried the full chain forward by feature_hash.

    See OPT-BACKLOG-PENDING-SCOPE-PHANTOM-NODES-REQUEUE (MF-2026-05-10-015).
    Long-term cleanup tracked under OPT-BACKLOG-DEPRECATE-GRAPH-SEMANTIC-NODES.
    """
    base_snapshot_id = str(base_snapshot_id or "").strip()
    if not base_snapshot_id:
        return set()
    try:
        from . import graph_events  # local import to avoid module cycle
    except Exception:
        return set()
    try:
        proj = graph_events.get_semantic_projection(conn, project_id, base_snapshot_id)
    except Exception:
        return set()
    if not isinstance(proj, dict):
        return set()
    node_semantics = (
        proj.get("projection", {}).get("node_semantics")
        if isinstance(proj.get("projection"), dict)
        else None
    )
    if not isinstance(node_semantics, dict):
        return set()
    current_ids: set[str] = set()
    for nid, entry in node_semantics.items():
        if not isinstance(entry, dict):
            continue
        validity = entry.get("validity") if isinstance(entry.get("validity"), dict) else {}
        status = str(validity.get("status") or "").strip().lower()
        if status in {"semantic_current", "semantic_carried_forward_current"}:
            current_ids.add(str(nid))
    return current_ids


def _carry_forward_semantic_graph_state(
    state: dict[str, Any],
    base_state: dict[str, Any],
    feature_index: dict[str, dict[str, Any]],
    *,
    base_snapshot_id: str,
    updated_at: str,
    edge_index: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reuse semantic entries whose structural feature hash is unchanged.

    MF 2026-05-11: extended to also carry forward `state.edge_semantics`
    when `edge_index` is provided. Edge carry-forward compares
    `edge_signature_hash` (the drift detector composed by the adapter
    from endpoint feature_hashes + edge_type + evidence keys); edges
    whose endpoints are no longer in the snapshot are skipped naturally
    because `edge_index` only contains edges from the current structure.
    """
    current = state.setdefault("node_semantics", {})
    if not isinstance(current, dict):
        current = {}
        state["node_semantics"] = current
    base_nodes = base_state.get("node_semantics")
    base_edges = base_state.get("edge_semantics") if isinstance(base_state.get("edge_semantics"), dict) else {}
    if not isinstance(base_nodes, dict) and not base_edges:
        return {
            "base_snapshot_id": base_snapshot_id,
            "carried_forward_count": 0,
            "skipped_existing_count": 0,
            "skipped_missing_node_count": 0,
            "skipped_hash_mismatch_count": 0,
            "edge_carried_forward_count": 0,
            "edge_skipped_existing_count": 0,
            "edge_skipped_missing_count": 0,
            "edge_skipped_hash_mismatch_count": 0,
        }

    carried = 0
    skipped_existing = 0
    skipped_missing = 0
    skipped_hash = 0
    if isinstance(base_nodes, dict):
        for node_id, raw_entry in sorted(base_nodes.items()):
            node_id = str(node_id or "")
            if not node_id or not isinstance(raw_entry, dict):
                continue
            if node_id in current:
                skipped_existing += 1
                continue
            feature = feature_index.get(node_id)
            if not feature:
                skipped_missing += 1
                continue
            base_hash = str(raw_entry.get("feature_hash") or "")
            current_hash = str(feature.get("feature_hash") or "")
            if not base_hash or not current_hash or base_hash != current_hash:
                skipped_hash += 1
                continue
            entry = dict(raw_entry)
            entry["carried_forward_from_snapshot_id"] = base_snapshot_id
            entry["carried_forward_at"] = updated_at
            entry["feature_hash"] = current_hash
            entry["primary"] = _path_list(feature.get("primary"))
            entry["secondary"] = _path_list(feature.get("secondary"))
            entry["test"] = _path_list(feature.get("test"))
            entry["config"] = _path_list(feature.get("config"))
            entry["file_hashes"] = feature.get("file_hashes") or entry.get("file_hashes") or {}
            current[node_id] = entry
            carried += 1
    # Edge carry-forward (mirror of the node loop above).
    edge_current = state.setdefault("edge_semantics", {})
    if not isinstance(edge_current, dict):
        edge_current = {}
        state["edge_semantics"] = edge_current
    edge_carried = 0
    edge_skipped_existing = 0
    edge_skipped_missing = 0
    edge_skipped_hash = 0
    if edge_index is not None:
        for edge_id, raw_entry in sorted(base_edges.items()):
            edge_id = str(edge_id or "")
            if not edge_id or not isinstance(raw_entry, dict):
                continue
            if edge_id in edge_current:
                edge_skipped_existing += 1
                continue
            edge_meta = edge_index.get(edge_id)
            if not edge_meta:
                # Endpoint deleted OR edge_type changed → no structural row,
                # don't carry semantic forward. Same cascade as nodes.
                edge_skipped_missing += 1
                continue
            base_hash = str(raw_entry.get("edge_signature_hash") or "")
            current_hash = str(edge_meta.get("edge_signature_hash") or "")
            if not base_hash or not current_hash or base_hash != current_hash:
                edge_skipped_hash += 1
                continue
            entry = dict(raw_entry)
            entry["carried_forward_from_snapshot_id"] = base_snapshot_id
            entry["carried_forward_at"] = updated_at
            entry["edge_signature_hash"] = current_hash
            entry["stable_edge_key"] = str(edge_meta.get("stable_edge_key") or "")
            edge_current[edge_id] = entry
            edge_carried += 1
    if carried or edge_carried:
        state["updated_at"] = updated_at
    _rebuild_semantic_graph_state_indexes(state)
    return {
        "base_snapshot_id": base_snapshot_id,
        "carried_forward_count": carried,
        "skipped_existing_count": skipped_existing,
        "skipped_missing_node_count": skipped_missing,
        "skipped_hash_mismatch_count": skipped_hash,
        "edge_carried_forward_count": edge_carried,
        "edge_skipped_existing_count": edge_skipped_existing,
        "edge_skipped_missing_count": edge_skipped_missing,
        "edge_skipped_hash_mismatch_count": edge_skipped_hash,
    }


def _semantic_review_status(review: Any) -> str:
    if not isinstance(review, dict):
        return ""
    status = str(review.get("status") or "").strip()
    if status:
        return status
    if review.get("bound") is True:
        return "bound"
    if review.get("bound") is False:
        return "missing"
    return ""


def _semantic_open_issues(node_id: str, ai_response: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for item in ai_response.get("open_issues") or []:
        if isinstance(item, dict):
            issue = dict(item)
            issue.setdefault("node_id", node_id)
            issues.append(issue)
    for key in (
        "merge_suggestions",
        "split_suggestions",
        "dependency_patch_suggestions",
        "dead_code_candidates",
    ):
        value = ai_response.get(key)
        if not value:
            continue
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, dict):
                issues.append({
                    "node_id": node_id,
                    "reason": key,
                    "type": str(item.get("type") or item.get("kind") or ""),
                    "target": str(item.get("target") or item.get("suggested_target") or ""),
                    "summary": _shorten_text(
                        item.get("reason")
                        or item.get("rationale")
                        or item.get("proposed_action")
                        or item,
                        500,
                    ),
                })
            else:
                issues.append({
                    "node_id": node_id,
                    "reason": key,
                    "summary": _shorten_text(item, 500),
                })
    return issues


def _short_issue_id(payload: dict[str, Any]) -> str:
    return "shi-" + hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()[:12]


def _issue_nodes(node_id: str, raw: dict[str, Any]) -> list[str]:
    nodes = _path_list(
        raw.get("affected_node_ids")
        or raw.get("source_node_ids")
        or raw.get("node_ids")
        or raw.get("nodes")
    )
    for key in ("node_id", "source_node_id", "target", "target_id"):
        value = str(raw.get(key) or "").strip()
        if value.startswith(("L", "graph.", "agent.", "governance.")) and value not in nodes:
            nodes.append(value)
    if node_id and node_id not in nodes:
        nodes.insert(0, node_id)
    return nodes[:20]


def _issue_confidence(value: Any, default: float = 0.6) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = default
    return max(0.0, min(1.0, round(confidence, 3)))


def _issue_severity(value: Any, *, default: str = "low") -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"info", "low", "medium", "high", "critical"}:
        return text
    return default


def _issue_category(raw: dict[str, Any], text: str) -> str:
    explicit = str(raw.get("category") or raw.get("issue_category") or "").strip().lower()
    if explicit:
        return explicit.replace("-", "_")
    lowered = text.lower()
    if "duplicate" in lowered or "overlap" in lowered or "merge_suggestions" in lowered:
        return "duplicate_or_overlap"
    if "split" in lowered or "broad" in lowered:
        return "broad_responsibility"
    if "test" in lowered:
        return "test_gap"
    if "doc" in lowered:
        return "doc_gap"
    if "config" in lowered:
        return "config_gap"
    if "relation" in lowered or "dependency" in lowered or "edge" in lowered:
        return "dependency_gap"
    if "hash" in lowered or "drift" in lowered:
        return "semantic_drift"
    if "observer" in lowered or "signoff" in lowered:
        return "observer_decision"
    if "code_review" in lowered:
        return "code_review"
    return "semantic_review"


def _category_default_severity(category: str) -> str:
    return {
        "duplicate_or_overlap": "medium",
        "broad_responsibility": "medium",
        "test_gap": "medium",
        "doc_gap": "low",
        "config_gap": "low",
        "dependency_gap": "low",
        "semantic_drift": "high",
        "observer_decision": "high",
        "code_review": "medium",
        "semantic_ai": "medium",
        "semantic_review": "low",
    }.get(category, "low")


def _normalize_health_issue(
    node_id: str,
    raw: dict[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    issue_type = str(raw.get("type") or raw.get("kind") or raw.get("issue_type") or "").strip()
    reason = str(raw.get("reason") or "").strip()
    summary = _shorten_text(
        raw.get("summary")
        or raw.get("issue")
        or raw.get("message")
        or raw.get("suggested_action")
        or raw.get("suggestion")
        or reason
        or issue_type,
        700,
    )
    text = " ".join(item for item in (issue_type, reason, summary) if item)
    category = _issue_category(raw, text)
    severity = _issue_severity(raw.get("severity"), default=_category_default_severity(category))
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
    evidence = {
        **evidence,
        "reason": reason,
        "source": source,
    }
    action = _shorten_text(
        raw.get("suggested_action")
        or raw.get("proposed_action")
        or raw.get("action")
        or summary,
        700,
    )
    issue = {
        "schema_version": SEMANTIC_HEALTH_ISSUE_SCHEMA_VERSION,
        "issue_id": str(raw.get("issue_id") or raw.get("id") or ""),
        "node_id": node_id,
        "category": category,
        "severity": severity,
        "confidence": _issue_confidence(raw.get("confidence"), default=0.6),
        "evidence": {key: value for key, value in evidence.items() if value not in ("", [], {})},
        "affected_node_ids": _issue_nodes(node_id, raw),
        "suggested_action": action,
        "source": str(raw.get("source") or source),
        "type": issue_type,
        "reason": reason,
        "summary": summary,
    }
    if not issue["issue_id"]:
        issue["issue_id"] = _short_issue_id({
            "node_id": node_id,
            "category": category,
            "severity": severity,
            "source": issue["source"],
            "type": issue_type,
            "reason": reason,
            "summary": summary,
            "affected_node_ids": issue["affected_node_ids"],
        })
    return {
        key: value
        for key, value in issue.items()
        if value not in ("", [], {}, None)
    }


def _health_issue_from_quality_flag(node_id: str, flag: str) -> dict[str, Any]:
    text = str(flag or "").strip()
    category = {
        "missing_doc_binding": "doc_gap",
        "missing_test_binding": "test_gap",
        "missing_symbol_refs": "symbol_gap",
        "has_review_feedback": "observer_review",
        "semantic_hash_mismatch": "semantic_drift",
        "semantic_ai_error": "semantic_ai",
        "review_required": "observer_review",
    }.get(text, "semantic_review")
    severity = {
        "missing_test_binding": "medium",
        "semantic_hash_mismatch": "high",
        "semantic_ai_error": "medium",
        "has_review_feedback": "low",
    }.get(text, _category_default_severity(category))
    return _normalize_health_issue(
        node_id,
        {
            "type": text,
            "category": category,
            "severity": severity,
            "confidence": 0.8,
            "summary": text,
            "suggested_action": f"Review semantic quality flag: {text}.",
        },
        source="quality_flag",
    )


def _semantic_health_issues(
    node_id: str,
    semantic_entry: dict[str, Any],
    *,
    open_issues: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(raw: dict[str, Any], source: str) -> None:
        if not isinstance(raw, dict):
            return
        normalized = _normalize_health_issue(node_id, raw, source=source)
        fingerprint = _short_issue_id({
            "node_id": normalized.get("node_id"),
            "category": normalized.get("category"),
            "type": normalized.get("type"),
            "reason": normalized.get("reason"),
            "summary": normalized.get("summary"),
            "affected_node_ids": normalized.get("affected_node_ids"),
        })
        if fingerprint in seen:
            return
        seen.add(fingerprint)
        issues.append(normalized)

    for raw in semantic_entry.get("health_issues") or []:
        if isinstance(raw, dict):
            add(raw, "ai_health_issue")
    for raw in (open_issues if open_issues is not None else _semantic_open_issues(node_id, semantic_entry)):
        if isinstance(raw, dict):
            add(raw, "legacy_open_issue")
    for flag in _path_list(semantic_entry.get("quality_flags")):
        add(_health_issue_from_quality_flag(node_id, flag), "quality_flag")
    return issues


def _semantic_state_entry(
    feature: dict[str, Any],
    semantic_entry: dict[str, Any],
    *,
    feedback_round: int,
    batch_index: int | None,
    updated_at: str,
) -> dict[str, Any]:
    node_id = str(feature.get("node_id") or semantic_entry.get("node_id") or "")
    ai_route = semantic_entry.get("semantic_ai_route")
    open_issues = _semantic_open_issues(node_id, semantic_entry)
    health_issues = _semantic_health_issues(node_id, semantic_entry, open_issues=open_issues)
    return {
        "node_id": node_id,
        "source_title": semantic_entry.get("source_title") or feature.get("title") or "",
        "feature_name": semantic_entry.get("feature_name") or feature.get("title") or node_id,
        "semantic_summary": _shorten_text(semantic_entry.get("semantic_summary"), 1200),
        "intent": _shorten_text(semantic_entry.get("intent"), 1200),
        "domain_label": semantic_entry.get("domain_label") or "",
        "status": semantic_entry.get("enrichment_status") or "",
        "quality_flags": _path_list(semantic_entry.get("quality_flags")),
        "primary": _path_list(feature.get("primary")),
        "secondary": _path_list(feature.get("secondary")),
        "test": _path_list(feature.get("test")),
        "config": _path_list(feature.get("config")),
        "feature_hash": feature.get("feature_hash") or semantic_entry.get("feature_hash") or "",
        "file_hashes": feature.get("file_hashes") or semantic_entry.get("file_hashes") or {},
        "doc_status": _semantic_review_status(semantic_entry.get("doc_coverage_review")),
        "test_status": _semantic_review_status(semantic_entry.get("test_coverage_review")),
        "config_status": _semantic_review_status(semantic_entry.get("config_coverage_review")),
        "graph_structure_suggestions": semantic_entry.get("graph_structure_suggestions") or [],
        "graph_structure_candidates": semantic_entry.get("graph_structure_candidates") or [],
        "graph_structure_ops": semantic_entry.get("graph_structure_ops") or {},
        "graph_enrich_config_suggestions": semantic_entry.get("graph_enrich_config_suggestions") or [],
        "graph_enrich_config_candidates": semantic_entry.get("graph_enrich_config_candidates") or [],
        "graph_enrich_config_ops": semantic_entry.get("graph_enrich_config_ops") or {},
        "open_issues": open_issues,
        "health_issues": health_issues,
        "feedback_round": feedback_round,
        "batch_index": batch_index,
        "updated_at": updated_at,
        "ai_route": ai_route if isinstance(ai_route, dict) else {},
    }


def _rebuild_semantic_graph_state_indexes(state: dict[str, Any]) -> None:
    nodes = state.get("node_semantics") if isinstance(state.get("node_semantics"), dict) else {}
    accepted: dict[str, dict[str, Any]] = {}
    file_ownership: dict[str, str] = {}
    open_issues: list[dict[str, Any]] = []
    health_issues: list[dict[str, Any]] = []
    completed: list[str] = []
    for node_id, raw in nodes.items():
        if not isinstance(raw, dict):
            continue
        node_id = str(node_id)
        status = str(raw.get("status") or "")
        if status == "ai_complete":
            completed.append(node_id)
        feature_name = str(raw.get("feature_name") or raw.get("source_title") or node_id)
        feature = accepted.setdefault(
            feature_name,
            {
                "feature_name": feature_name,
                "node_ids": [],
                "owned_files": [],
                "candidate_docs": [],
                "candidate_tests": [],
                "candidate_configs": [],
                "purpose": raw.get("intent") or raw.get("semantic_summary") or "",
            },
        )
        if str(node_id) not in feature["node_ids"]:
            feature["node_ids"].append(str(node_id))
        for path in _path_list(raw.get("primary")):
            file_ownership.setdefault(path, feature_name)
            feature["owned_files"].append(path)
        feature["candidate_docs"].extend(_path_list(raw.get("secondary")))
        feature["candidate_tests"].extend(_path_list(raw.get("test")))
        feature["candidate_configs"].extend(_path_list(raw.get("config")))
        for issue in raw.get("open_issues") or []:
            if isinstance(issue, dict):
                open_issues.append(issue)
        raw_health_issues = raw.get("health_issues")
        if isinstance(raw_health_issues, list) and raw_health_issues:
            node_health_issues = [
                _normalize_health_issue(node_id, issue, source=str(issue.get("source") or "semantic_state"))
                for issue in raw_health_issues
                if isinstance(issue, dict)
            ]
        else:
            node_health_issues = _semantic_health_issues(
                node_id,
                raw,
                open_issues=[
                    issue for issue in (raw.get("open_issues") or [])
                    if isinstance(issue, dict)
                ],
            )
            if node_health_issues:
                raw["health_issues"] = node_health_issues
        health_issues.extend(node_health_issues)
    for feature in accepted.values():
        for key in ("node_ids", "owned_files", "candidate_docs", "candidate_tests", "candidate_configs"):
            feature[key] = sorted(set(_path_list(feature.get(key))))
        feature["purpose"] = _shorten_text(feature.get("purpose"), 600)
    state["accepted_features"] = dict(sorted(accepted.items()))
    state["file_ownership"] = dict(sorted(file_ownership.items()))
    state["open_issues"] = open_issues[-200:]
    state["health_issues"] = health_issues[-300:]
    state["completed_node_ids"] = sorted(set(completed))
    jobs = state.get("semantic_jobs") if isinstance(state.get("semantic_jobs"), dict) else {}
    job_counts: dict[str, int] = {}
    for raw_job in jobs.values():
        if not isinstance(raw_job, dict):
            continue
        status = str(raw_job.get("status") or "unknown")
        job_counts[status] = job_counts.get(status, 0) + 1
    state["semantic_job_counts"] = dict(sorted(job_counts.items()))


def _file_hashes_match(current: Any, stored: Any) -> bool:
    current_hashes = current if isinstance(current, dict) else {}
    stored_hashes = stored if isinstance(stored, dict) else {}
    if not current_hashes or not stored_hashes:
        return True
    return current_hashes == stored_hashes


def _semantic_state_validation(feature: dict[str, Any], state_entry: dict[str, Any]) -> dict[str, Any]:
    current_hash = str(feature.get("feature_hash") or "")
    stored_hash = str(state_entry.get("feature_hash") or "")
    feature_hash_match = bool(current_hash and stored_hash and current_hash == stored_hash)
    file_hash_match = _file_hashes_match(feature.get("file_hashes"), state_entry.get("file_hashes"))
    status = "current" if feature_hash_match and file_hash_match else "stale_hash_mismatch"
    return {
        "status": status,
        "valid": status == "current",
        "feature_hash": current_hash,
        "stored_feature_hash": stored_hash,
        "feature_hash_match": feature_hash_match,
        "file_hash_match": file_hash_match,
    }


def _semantic_run_status(
    *,
    feature_count: int,
    effective_use_ai: bool,
    ai_selected_count: int,
    ai_attempted_count: int,
    ai_complete_count: int,
    ai_error_count: int,
    semantic_graph_state_hit_count: int,
) -> str:
    valid_semantic_count = ai_complete_count + semantic_graph_state_hit_count
    if feature_count > 0 and valid_semantic_count >= feature_count:
        return "ai_complete"
    if valid_semantic_count > 0:
        return "ai_partial"
    if ai_selected_count <= 0:
        return "index_only"
    if not effective_use_ai:
        return "ai_pending"
    if ai_attempted_count > 0 and ai_error_count >= ai_attempted_count:
        return "ai_failed"
    return "ai_pending"


def feedback_review_gate(
    summary: dict[str, Any] | None,
    *,
    allow_heuristic_feedback_review: bool = False,
    allow_partial_semantic_feedback_review: bool = False,
) -> dict[str, Any]:
    summary = summary if isinstance(summary, dict) else {}
    if allow_heuristic_feedback_review:
        return {"allowed": True, "reason": "heuristic_feedback_review_explicitly_allowed"}
    graph_state = summary.get("semantic_graph_state") if isinstance(summary.get("semantic_graph_state"), dict) else {}
    valid_semantic_count = int(summary.get("ai_complete_count") or 0) + int(graph_state.get("hit_count") or 0)
    feature_count = int(summary.get("feature_count") or 0)
    if feature_count > 0 and valid_semantic_count >= feature_count:
        return {
            "allowed": True,
            "reason": "all_selected_semantics_current",
            "valid_semantic_count": valid_semantic_count,
            "feature_count": feature_count,
        }
    if allow_partial_semantic_feedback_review and valid_semantic_count > 0:
        return {
            "allowed": True,
            "reason": "partial_ai_semantic_available",
            "valid_semantic_count": valid_semantic_count,
            "feature_count": feature_count,
        }
    return {
        "allowed": False,
        "reason": "semantic_ai_not_complete" if valid_semantic_count <= 0 else "semantic_ai_partial",
        "semantic_run_status": summary.get("semantic_run_status") or "",
        "feature_count": feature_count,
        "ai_selected_count": int(summary.get("ai_selected_count") or 0),
        "ai_complete_count": int(summary.get("ai_complete_count") or 0),
        "valid_semantic_count": valid_semantic_count,
    }


def _upsert_semantic_graph_state_entry(
    state: dict[str, Any],
    feature: dict[str, Any],
    semantic_entry: dict[str, Any],
    *,
    feedback_round: int,
    batch_index: int | None,
    updated_at: str,
) -> None:
    node_id = str(feature.get("node_id") or semantic_entry.get("node_id") or "")
    if not node_id:
        return
    node_semantics = state.setdefault("node_semantics", {})
    node_semantics[node_id] = _semantic_state_entry(
        feature,
        semantic_entry,
        feedback_round=feedback_round,
        batch_index=batch_index,
        updated_at=updated_at,
    )
    state["updated_at"] = updated_at
    _rebuild_semantic_graph_state_indexes(state)


def _semantic_graph_state_summary(state: dict[str, Any]) -> dict[str, Any]:
    accepted = state.get("accepted_features") if isinstance(state.get("accepted_features"), dict) else {}
    features: list[dict[str, Any]] = []
    for name in sorted(accepted):
        item = accepted.get(name) if isinstance(accepted.get(name), dict) else {}
        features.append({
            "feature_name": name,
            "node_ids": _path_list(item.get("node_ids"))[:20],
            "purpose": _shorten_text(item.get("purpose"), 360),
            "owned_files": _path_list(item.get("owned_files"))[:20],
            "candidate_docs": _path_list(item.get("candidate_docs"))[:20],
            "candidate_tests": _path_list(item.get("candidate_tests"))[:20],
            "candidate_configs": _path_list(item.get("candidate_configs"))[:20],
        })
    return {
        "schema_version": state.get("schema_version") or SEMANTIC_ENRICHMENT_SCHEMA_VERSION,
        "source": state.get("source") or "db",
        "snapshot_id": state.get("snapshot_id") or "",
        "commit_sha": state.get("commit_sha") or "",
        "semanticized_node_count": len(state.get("node_semantics") or {}),
        "completed_node_count": len(state.get("completed_node_ids") or []),
        "accepted_feature_count": len(features),
        "file_ownership_count": len(state.get("file_ownership") or {}),
        "open_issue_count": len(state.get("open_issues") or []),
        "health_issue_count": len(state.get("health_issues") or []),
        "semantic_job_counts": state.get("semantic_job_counts") or {},
        "completed_node_ids": _path_list(state.get("completed_node_ids"))[:300],
        "accepted_features": features,
        "open_issues": [_compact_memory_conflict(item) for item in (state.get("open_issues") or [])[-30:]],
        "health_issues": [
            _compact_health_issue(item)
            for item in (state.get("health_issues") or [])[-30:]
            if isinstance(item, dict)
        ],
    }


def _semantic_graph_related_features(state: dict[str, Any], feature: dict[str, Any]) -> list[dict[str, Any]]:
    accepted = state.get("accepted_features") if isinstance(state.get("accepted_features"), dict) else {}
    current_paths = set(_feature_paths(feature))
    if not current_paths:
        return []
    related: list[dict[str, Any]] = []
    for name, item in accepted.items():
        if not isinstance(item, dict):
            continue
        paths = set(
            _path_list(item.get("owned_files"))
            + _path_list(item.get("candidate_docs"))
            + _path_list(item.get("candidate_tests"))
            + _path_list(item.get("candidate_configs"))
        )
        overlap = sorted(current_paths.intersection(paths))
        if overlap:
            related.append({
                "feature_name": str(name),
                "matching_files": overlap[:30],
                "node_ids": _path_list(item.get("node_ids"))[:20],
                "reason": "semantic_graph_file_overlap",
            })
    return related[:20]


def _semantic_entry_from_state(feature: dict[str, Any], state_entry: dict[str, Any]) -> dict[str, Any]:
    entry = _heuristic_semantic_entry(
        feature,
        [],
        enrichment_status="semantic_graph_state",
        ai_response={
            "feature_name": state_entry.get("feature_name"),
            "semantic_summary": state_entry.get("semantic_summary"),
            "intent": state_entry.get("intent"),
            "domain_label": state_entry.get("domain_label"),
            "doc_coverage_review": {"status": state_entry.get("doc_status")},
            "test_coverage_review": {"status": state_entry.get("test_status")},
            "config_coverage_review": {"status": state_entry.get("config_status")},
        },
    )
    entry["quality_flags"] = _path_list(state_entry.get("quality_flags"))
    entry["open_issues"] = [
        item for item in (state_entry.get("open_issues") or [])
        if isinstance(item, dict)
    ]
    entry["health_issues"] = [
        item for item in (state_entry.get("health_issues") or [])
        if isinstance(item, dict)
    ]
    entry["semantic_graph_state_status"] = state_entry.get("status") or ""
    entry["semantic_graph_state_updated_at"] = state_entry.get("updated_at") or ""
    return entry


def _materialize_semantic_graph(
    graph_json: dict[str, Any],
    state: dict[str, Any],
    *,
    feature_index: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    graph = copy.deepcopy(graph_json if isinstance(graph_json, dict) else {})
    deps = graph.get("deps_graph") if isinstance(graph.get("deps_graph"), dict) else {}
    nodes = deps.get("nodes") if isinstance(deps.get("nodes"), list) else []
    semantics = state.get("node_semantics") if isinstance(state.get("node_semantics"), dict) else {}
    feature_index = feature_index or {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = _node_id(node)
        if not node_id or node_id not in semantics:
            continue
        metadata = node.setdefault("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
            node["metadata"] = metadata
        feature = _feature_context_from_node(
            node,
            feature_index=feature_index,
            project_root=None,
            max_excerpt_chars=0,
        )
        validation = _semantic_state_validation(feature, semantics[node_id])
        metadata["semantic_status"] = validation
        if validation["valid"]:
            metadata["semantic"] = semantics[node_id]
        else:
            metadata.pop("semantic", None)
    graph.setdefault("metadata", {})
    if isinstance(graph["metadata"], dict):
        graph["metadata"]["semantic_graph_state"] = {
            "snapshot_id": state.get("snapshot_id") or "",
            "updated_at": state.get("updated_at") or "",
            "completed_node_count": len(state.get("completed_node_ids") or []),
            "accepted_feature_count": len(state.get("accepted_features") or {}),
            "source": state.get("source") or "db",
        }
    return graph


def _write_semantic_graph_state_artifacts(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    graph_json: dict[str, Any],
    state: dict[str, Any],
    *,
    round_number: int,
    feature_index: dict[str, dict[str, Any]] | None = None,
    submit_for_review: bool = False,
    review_node_ids: set[str] | None = None,
    review_edge_ids: set[str] | None = None,
) -> tuple[Path, Path, Path, Path]:
    base = _semantic_base_dir(project_id, snapshot_id)
    rdir = _round_dir(project_id, snapshot_id, round_number)
    latest_state_path = base / SEMANTIC_GRAPH_STATE_NAME
    latest_graph_path = base / SEMANTIC_GRAPH_NAME
    round_state_path = rdir / SEMANTIC_GRAPH_STATE_NAME
    round_graph_path = rdir / SEMANTIC_GRAPH_NAME
    with _semantic_write_lock():
        _persist_semantic_state_to_db(
            conn, project_id, snapshot_id, state,
            submit_for_review=submit_for_review,
            review_node_ids=review_node_ids,
            review_edge_ids=review_edge_ids,
        )
        _commit_semantic_write(conn)
    semantic_graph = _materialize_semantic_graph(
        graph_json,
        state,
        feature_index=feature_index,
    )
    _write_json(latest_state_path, state)
    _write_json(round_state_path, state)
    _write_json(latest_graph_path, semantic_graph)
    _write_json(round_graph_path, semantic_graph)
    return latest_state_path, latest_graph_path, round_state_path, round_graph_path


def _semantic_memory_related_features(batch: dict[str, Any], feature: dict[str, Any]) -> list[dict[str, Any]]:
    if not batch:
        return []
    try:
        from . import reconcile_batch_memory as bm

        return bm.find_related_features(batch, {
            "primary_files": feature.get("primary") or [],
            "candidate_tests": feature.get("test") or [],
            "candidate_docs": feature.get("secondary") or [],
        })[:20]
    except Exception:  # noqa: BLE001 - advisory context only
        return []


def _semantic_memory_conflicts(ai_response: dict[str, Any]) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    for key in (
        "merge_suggestions",
        "split_suggestions",
        "dependency_patch_suggestions",
        "dead_code_candidates",
    ):
        value = ai_response.get(key)
        if not value:
            continue
        conflicts.append({
            "reason": key,
            "items": value if isinstance(value, list) else [value],
        })
    return conflicts


def _semantic_memory_decision_payload(
    feature: dict[str, Any],
    ai_response: dict[str, Any],
) -> dict[str, Any]:
    feature_name = str(ai_response.get("feature_name") or feature.get("title") or feature.get("node_id") or "")
    target_feature = str(ai_response.get("target_feature") or ai_response.get("merge_into") or "")
    return {
        "decision": "merge_into_existing_feature" if target_feature else "new_feature",
        "feature_name": feature_name,
        "target_feature": target_feature,
        "owned_files": feature.get("primary") or [],
        "candidate_tests": feature.get("test") or [],
        "candidate_docs": feature.get("secondary") or [],
        "reserved_names": [feature_name] if feature_name else [],
        "purpose": ai_response.get("intent")
        or ai_response.get("semantic_summary")
        or ai_response.get("purpose")
        or "",
        "reason": ai_response.get("semantic_summary") or ai_response.get("reason") or "",
        "conflicts": _semantic_memory_conflicts(ai_response),
        "decided_by": "semantic_ai",
        "actor": "reconcile_semantic_enrichment",
    }


def _record_semantic_memory_decision(
    conn: sqlite3.Connection,
    project_id: str,
    batch_id: str,
    snapshot_id: str,
    feature: dict[str, Any],
    ai_response: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    if not batch_id:
        return {}, ""
    try:
        from . import reconcile_batch_memory as bm

        node_id = str(feature.get("node_id") or "")
        feature_hash = str(feature.get("feature_hash") or "")
        fingerprint = f"semantic:{snapshot_id}:{node_id}:{feature_hash[:16]}"
        batch = bm.record_pm_decision(
            conn,
            project_id,
            batch_id,
            fingerprint,
            _semantic_memory_decision_payload(feature, ai_response),
        )
        return batch, ""
    except Exception as exc:  # noqa: BLE001 - report but keep enrichment alive
        return {}, str(exc)


def run_semantic_enrichment(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    project_root: str | Path | None = None,
    *,
    feedback_items: list[dict[str, Any]] | dict[str, Any] | None = None,
    feedback_round: int | None = None,
    use_ai: bool | None = None,
    ai_call: FeedbackAiCall | None = None,
    created_by: str = "observer",
    max_excerpt_chars: int | None = None,
    semantic_config_path: str | Path | None = None,
    semantic_ai_provider: str | None = None,
    semantic_ai_model: str | None = None,
    semantic_ai_role: str | None = None,
    semantic_ai_chain_role: str | None = None,
    semantic_analyzer_role: str | None = None,
    ai_feature_limit: int | None = None,
    semantic_ai_scope: str | None = None,
    semantic_node_ids: Any = None,
    semantic_layers: Any = None,
    semantic_quality_flags: Any = None,
    semantic_missing: Any = None,
    semantic_changed_paths: Any = None,
    semantic_path_prefixes: Any = None,
    semantic_selector_match: str | None = None,
    semantic_include_structural: bool = False,
    semantic_ai_batch_size: int | None = None,
    semantic_ai_batch_by: str = "subsystem",
    semantic_ai_input_mode: str | None = None,
    semantic_dynamic_graph_state: bool | None = None,
    semantic_graph_state: bool = True,
    semantic_skip_completed: bool = True,
    semantic_batch_memory: bool | None = None,
    semantic_batch_memory_id: str | None = None,
    semantic_base_snapshot_id: str | None = None,
    trace_dir: str | Path | None = None,
    persist_feature_payloads: bool = True,
    submit_for_review: bool = False,
    enqueue_stale: bool = True,
) -> dict[str, Any]:
    """Create semantic companion artifacts for a graph snapshot.

    The same function is intentionally snapshot-kind agnostic; callers can use
    it for full, scope, imported, or future graph snapshot kinds.

    MF-2026-05-10-016: when `submit_for_review=True`, the persistent layer
    writes graph_semantic_nodes rows with status="pending_review". The
    backfill→event step then maps that to event status=PROPOSED, keeping the
    projection blind until an operator accepts via /feedback/decision with
    action="accept_semantic_enrichment". Default False preserves the existing
    reconcile-chain behavior (immediate ai_complete + observed events).

    OPT-BACKLOG-MATERIALIZE-NO-WORKER-NOTIFY: when `enqueue_stale=False`, the
    function still carries forward existing semantic state (so the projection
    keeps showing prior enrichment) but does NOT write fresh ai_pending rows
    for stale nodes selected for AI. Default True preserves the legacy
    "queue everything stale" behavior used by full reconcile cycles. The
    dashboard's /reconcile/pending-scope handler flips this to False so an
    operator-driven catchup doesn't silently fill graph_semantic_jobs with
    work the worker won't auto-drain.
    """
    ensure_schema(conn)
    snapshot = get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    root = Path(project_root).resolve() if project_root else None
    semantic_config = load_semantic_enrichment_config(
        project_root=root,
        config_path=semantic_config_path,
    )
    if semantic_ai_provider is not None:
        semantic_config.provider = str(semantic_ai_provider or "")
    if semantic_ai_model is not None:
        semantic_config.model = str(semantic_ai_model or "")
    if semantic_analyzer_role is not None:
        semantic_config.analyzer_role = str(semantic_analyzer_role or "")
    if semantic_ai_chain_role is not None:
        semantic_config.chain_role = str(semantic_ai_chain_role or "")
        semantic_config.role = semantic_config.chain_role
    if semantic_ai_role is not None:
        # Legacy alias: semantic_ai_role is the pipeline/chain role used for
        # model routing, not the semantic analyzer identity.
        semantic_config.chain_role = str(semantic_ai_role or "")
        semantic_config.role = semantic_config.chain_role
    effective_use_ai = semantic_config.use_ai_default if use_ai is None else bool(use_ai)
    effective_excerpt_chars = (
        semantic_config.input_policy.max_excerpt_chars
        if max_excerpt_chars is None
        else int(max_excerpt_chars)
    )
    if not semantic_config.input_policy.include_source_excerpt:
        effective_excerpt_chars = 0
    if feedback_items:
        append_review_feedback(
            conn,
            project_id,
            snapshot_id,
            feedback_items,
            created_by=created_by,
        )
    feedback = load_review_feedback(project_id, snapshot_id)
    graph_path = snapshot_graph_path(project_id, snapshot_id)
    graph_json = _read_json(graph_path, {})
    selector = _selector_from_kwargs(
        semantic_ai_scope=semantic_ai_scope,
        semantic_node_ids=semantic_node_ids,
        semantic_layers=semantic_layers,
        semantic_quality_flags=semantic_quality_flags,
        semantic_missing=semantic_missing,
        semantic_changed_paths=semantic_changed_paths,
        semantic_path_prefixes=semantic_path_prefixes,
        semantic_selector_match=semantic_selector_match,
        semantic_include_structural=semantic_include_structural,
    )
    nodes = _semantic_candidate_nodes(graph_json, selector)
    feature_index = _load_feature_index(snapshot)
    carry_forward_features = {
        _node_id(node): _feature_context_from_node(
            node,
            feature_index=feature_index,
            project_root=root,
            max_excerpt_chars=0,
        )
        for node in nodes
        if _node_id(node)
    }
    # MF 2026-05-11: edge carry-forward index. We use the full nodes list
    # (not just enrichment candidates) so endpoint metadata for stable_key
    # lookup is available; edges with deleted endpoints naturally drop out
    # because _graph_edges only returns edges from the current structure.
    all_nodes_for_edges = _graph_nodes(graph_json)
    carry_forward_edges = _build_edge_carry_index(
        all_nodes_for_edges,
        _graph_edges(graph_json),
    )
    existing_rounds = sorted((_semantic_base_dir(project_id, snapshot_id) / "rounds").glob("round-*"))
    round_number = int(feedback_round) if feedback_round is not None else len(existing_rounds)
    generated_at = utc_now()
    semantic_state_enabled = bool(semantic_graph_state)
    _ensure_semantic_state_schema(conn)
    semantic_state = (
        _load_semantic_graph_state_source(conn, project_id, snapshot_id, snapshot)
        if semantic_state_enabled
        else _empty_semantic_graph_state(project_id, snapshot_id, snapshot)
    )
    carry_forward_report = {
        "base_snapshot_id": str(semantic_base_snapshot_id or ""),
        "carried_forward_count": 0,
        "skipped_existing_count": 0,
        "skipped_missing_node_count": 0,
        "skipped_hash_mismatch_count": 0,
        "error": "",
    }
    if semantic_state_enabled and semantic_base_snapshot_id:
        try:
            base_snapshot = get_graph_snapshot(conn, project_id, str(semantic_base_snapshot_id))
            if base_snapshot:
                base_state = _load_semantic_graph_state_source(
                    conn,
                    project_id,
                    str(semantic_base_snapshot_id),
                    base_snapshot,
                )
                carry_forward_report.update(_carry_forward_semantic_graph_state(
                    semantic_state,
                    base_state,
                    carry_forward_features,
                    base_snapshot_id=str(semantic_base_snapshot_id),
                    updated_at=generated_at,
                    edge_index=carry_forward_edges,
                ))
            else:
                carry_forward_report["error"] = "base_snapshot_not_found"
        except Exception as exc:  # noqa: BLE001 - semantic carry-forward is advisory
            carry_forward_report["error"] = str(exc)
    semantic_features: list[dict[str, Any]] = []
    ai_complete_count = 0
    ai_unavailable_count = 0
    ai_error_count = 0
    ai_skipped_count = 0
    ai_selected_count = 0
    ai_skipped_selector_count = 0
    semantic_hash_mismatch_count = 0
    requested_ai_batch_size = _normal_batch_size(semantic_ai_batch_size)
    ai_input_mode = _normal_ai_input_mode(
        semantic_ai_input_mode,
        default=semantic_config.execution_policy.ai_input_mode,
    )
    dynamic_graph_state = (
        semantic_config.execution_policy.dynamic_semantic_graph_state
        if semantic_dynamic_graph_state is None
        else bool(semantic_dynamic_graph_state)
    )
    ai_batch_size = 1 if ai_input_mode == "feature" else requested_ai_batch_size
    ai_batch_count = 0
    ai_batch_complete_count = 0
    ai_batch_error_count = 0
    semantic_graph_state_hit_count = 0
    payload_input_paths: list[str] = []
    payload_output_paths: list[str] = []
    payload_trace_base = Path(trace_dir) if trace_dir else _round_dir(project_id, snapshot_id, round_number)
    records: list[dict[str, Any]] = []
    for node in nodes:
        feature = _feature_context_from_node(
            node,
            feature_index=feature_index,
            project_root=root,
            max_excerpt_chars=effective_excerpt_chars,
        )
        node_id = str(feature.get("node_id") or "")
        relevant_feedback = [
            item for item in feedback if _feedback_matches_feature(item, feature)
        ]
        flags = _quality_flags(feature, relevant_feedback)
        selected_for_ai, selection_reasons = _selector_decision(feature, flags, selector)
        existing_semantic = (
            semantic_state.get("node_semantics", {}).get(node_id)
            if semantic_state_enabled
            else None
        )
        existing_semantic = existing_semantic if isinstance(existing_semantic, dict) else {}
        semantic_state_validation = (
            _semantic_state_validation(feature, existing_semantic)
            if existing_semantic
            else {"status": "missing", "valid": False}
        )
        skipped_completed = bool(
            semantic_state_enabled
            and semantic_skip_completed
            and existing_semantic.get("status") == "ai_complete"
            and semantic_state_validation.get("valid")
            and not relevant_feedback
        )
        if (
            existing_semantic.get("status") == "ai_complete"
            and not semantic_state_validation.get("valid")
        ):
            semantic_hash_mismatch_count += 1
        if skipped_completed:
            selected_for_ai = False
            selection_reasons = list(selection_reasons) + ["semantic_graph_state_complete"]
        elif existing_semantic.get("status") == "ai_complete" and existing_semantic:
            selection_reasons = list(selection_reasons) + ["semantic_graph_state_hash_mismatch"]
        if selected_for_ai:
            ai_selected_count += 1
        payload_feature = dict(feature)
        if not semantic_config.input_policy.include_symbol_refs:
            payload_feature["symbol_refs"] = []
        if not semantic_config.input_policy.include_doc_refs:
            payload_feature["doc_refs"] = []
        if not semantic_config.input_policy.include_config_refs:
            payload_feature["config_refs"] = []
            payload_feature["config"] = []
        if not semantic_config.input_policy.include_file_hashes:
            payload_feature["file_hashes"] = {}
        payload_feedback = relevant_feedback if semantic_config.input_policy.include_review_feedback else []
        payload = {
            "schema_version": SEMANTIC_ENRICHMENT_SCHEMA_VERSION,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "snapshot_kind": snapshot.get("snapshot_kind") or "",
            "commit_sha": snapshot.get("commit_sha") or "",
            "feedback_round": round_number,
            "feature": payload_feature,
            "review_feedback": payload_feedback,
            "instructions": semantic_config.to_instruction_payload("node"),
            "semantic_selector": selector,
            "semantic_ai_input_mode": ai_input_mode,
            "dynamic_semantic_graph_state": bool(dynamic_graph_state and semantic_state_enabled),
            "semantic_selection": {
                "status": "selected" if selected_for_ai else "not_selected",
                "reasons": selection_reasons,
            },
            "semantic_graph_state": _semantic_graph_state_summary(semantic_state) if semantic_state_enabled else {},
            "existing_semantic": existing_semantic,
        }
        node_name = _safe_node_filename(str(feature.get("node_id") or "feature"))
        if persist_feature_payloads:
            payload_input_paths.append(write_json(
                payload_trace_base / "feature-inputs" / f"{node_name}.json",
                payload,
            ))
        records.append({
            "feature": feature,
            "feedback": relevant_feedback,
            "flags": flags,
            "payload": payload,
            "node_name": node_name,
            "selected_for_ai": selected_for_ai,
            "selection_reasons": selection_reasons,
            "existing_semantic": existing_semantic,
            "semantic_state_validation": semantic_state_validation,
            "skipped_completed": skipped_completed,
        })

    selected_records = [
        record for record in records
        if bool(effective_use_ai) and record["selected_for_ai"]
    ]
    if ai_feature_limit is not None and ai_feature_limit >= 0:
        allowed_records = selected_records[: int(ai_feature_limit)]
    else:
        allowed_records = selected_records
    allowed_node_ids = {
        str(record["feature"].get("node_id") or "")
        for record in allowed_records
    }
    review_node_ids: set[str] = set()
    review_edge_ids: set[str] = set()
    # MF-2026-05-10-015: load the base projection's current-node set so
    # we don't spuriously re-enqueue phantom-current carried_forward nodes.
    projection_current_ids = _projection_current_node_ids(
        conn, project_id, semantic_base_snapshot_id
    ) if semantic_state_enabled else set()
    # OPT-BACKLOG-MATERIALIZE-NO-WORKER-NOTIFY: the auto-enqueue below writes
    # ai_pending rows for every stale node when use_ai=False (the dashboard's
    # incremental reconcile path). The MF-016 worker doesn't auto-drain those
    # rows because no semantic_job.enqueued event fires from this code path,
    # so the rows sit silently until an operator manually triggers POST
    # /semantic/jobs. Skip the whole block when the caller said
    # enqueue_stale=False — they want operator-driven enqueueing instead.
    if semantic_state_enabled and enqueue_stale:
        for record in records:
            if not record.get("selected_for_ai"):
                continue
            node_id = str(record["feature"].get("node_id") or "")
            # MF-2026-05-10-015: skip nodes the base projection already labels
            # current/carried-forward-current. The legacy persistent layer
            # (state.json) doesn't see those phantoms; without this filter
            # every reconcile re-queues ~90 ai_pending rows for unchanged code.
            if node_id and node_id in projection_current_ids:
                continue
            if effective_use_ai and node_id in allowed_node_ids:
                job_status = "pending_ai"
            elif effective_use_ai:
                job_status = "skipped_limit"
            else:
                job_status = "ai_pending"
            _upsert_semantic_job(
                semantic_state,
                record["feature"],
                status=job_status,
                feedback_round=round_number,
                batch_index=None,
                updated_at=generated_at,
            )
    ai_responses: dict[str, dict[str, Any]] = {}
    memory_enabled = bool(semantic_batch_memory)
    memory_batch: dict[str, Any] = {}
    memory_batch_id = ""
    memory_error = ""
    memory_decision_count = 0
    memory_update_error_count = 0
    if memory_enabled:
        memory_batch, memory_error = _create_semantic_batch_memory(
            conn,
            project_id,
            snapshot_id,
            round_number,
            created_by=created_by,
            batch_id=semantic_batch_memory_id,
        )
        memory_batch_id = str(memory_batch.get("batch_id") or _semantic_batch_memory_id(
            snapshot_id,
            round_number,
            semantic_batch_memory_id,
        ))
        if memory_error:
            memory_enabled = False
    if allowed_records:
        if semantic_state_enabled:
            _write_semantic_graph_state_artifacts(
                conn,
                project_id,
                snapshot_id,
                graph_json,
                semantic_state,
                round_number=round_number,
                feature_index=feature_index,
                submit_for_review=submit_for_review,
                review_node_ids=review_node_ids,
                review_edge_ids=review_edge_ids,
            )
        if ai_batch_size <= 1:
            for record in allowed_records:
                node_id = str(record["feature"].get("node_id") or "")
                if semantic_state_enabled:
                    if dynamic_graph_state:
                        semantic_state = _load_semantic_graph_state_source(
                            conn,
                            project_id,
                            snapshot_id,
                            snapshot,
                        )
                    record["payload"]["semantic_graph_state"] = _semantic_graph_state_summary(semantic_state)
                    record["payload"]["related_graph_features"] = _semantic_graph_related_features(
                        semantic_state,
                        record["feature"],
                    )
                    if persist_feature_payloads:
                        write_json(
                            payload_trace_base / "feature-inputs" / f"{record['node_name']}.json",
                            record["payload"],
                        )
                if memory_enabled:
                    memory_batch = _refresh_semantic_batch_memory(conn, project_id, memory_batch_id) or memory_batch
                    record["payload"]["batch_memory"] = _semantic_batch_memory_summary(memory_batch)
                    record["payload"]["related_batch_features"] = _semantic_memory_related_features(
                        memory_batch,
                        record["feature"],
                    )
                    if persist_feature_payloads:
                        write_json(
                            payload_trace_base / "feature-inputs" / f"{record['node_name']}.json",
                            record["payload"],
                        )
                if semantic_state_enabled:
                    _upsert_semantic_job(
                        semantic_state,
                        record["feature"],
                        status="running",
                        feedback_round=round_number,
                        batch_index=None,
                        updated_at=utc_now(),
                        increment_attempt=True,
                    )
                    _write_semantic_graph_state_artifacts(
                        conn,
                        project_id,
                        snapshot_id,
                        graph_json,
                        semantic_state,
                        round_number=round_number,
                        feature_index=feature_index,
                        submit_for_review=submit_for_review,
                        review_node_ids=review_node_ids,
                        review_edge_ids=review_edge_ids,
                    )
                response = _call_ai(
                    ai_call,
                    stage="reconcile_semantic_feature",
                    payload=record["payload"],
                    )
                if response is not None:
                    ai_responses[node_id] = response
                if semantic_state_enabled:
                    _upsert_semantic_job(
                        semantic_state,
                        record["feature"],
                        status=(
                            "ai_failed"
                            if response is None or response.get("_ai_error")
                            else "ai_complete"
                        ),
                        feedback_round=round_number,
                        batch_index=None,
                        updated_at=utc_now(),
                        last_error=(
                            str(response.get("_ai_error") or "")
                            if isinstance(response, dict)
                            else "ai_response_missing"
                        ),
                    )
                if semantic_state_enabled and response is not None and not response.get("_ai_error"):
                    state_entry = _heuristic_semantic_entry(
                        record["feature"],
                        record["feedback"],
                        enrichment_status="ai_complete",
                        ai_response=response,
                    )
                    if response.get("_ai_route"):
                        state_entry["semantic_ai_route"] = response.get("_ai_route")
                    _upsert_semantic_graph_state_entry(
                        semantic_state,
                        record["feature"],
                        state_entry,
                        feedback_round=round_number,
                        batch_index=None,
                        updated_at=utc_now(),
                    )
                    review_node_ids.add(node_id)
                if memory_enabled and response is not None and not response.get("_ai_error"):
                    updated_batch, update_error = _record_semantic_memory_decision(
                        conn,
                        project_id,
                        memory_batch_id,
                        snapshot_id,
                        record["feature"],
                        response,
                    )
                    if update_error:
                        memory_update_error_count += 1
                    else:
                        memory_decision_count += 1
                        memory_batch = updated_batch or memory_batch
                if semantic_state_enabled:
                    _write_semantic_graph_state_artifacts(
                        conn,
                        project_id,
                        snapshot_id,
                        graph_json,
                        semantic_state,
                        round_number=round_number,
                        feature_index=feature_index,
                        submit_for_review=submit_for_review,
                        review_node_ids=review_node_ids,
                        review_edge_ids=review_edge_ids,
                    )
        else:
            for batch_index, batch in enumerate(
                _batch_records(
                    allowed_records,
                    batch_size=ai_batch_size,
                    batch_by=semantic_ai_batch_by,
                )
            ):
                ai_batch_count += 1
                batch_key = _batch_key(batch[0]["feature"], semantic_ai_batch_by) if batch else "all"
                if semantic_state_enabled and dynamic_graph_state:
                    semantic_state = _load_semantic_graph_state_source(
                        conn,
                        project_id,
                        snapshot_id,
                        snapshot,
                    )
                if memory_enabled:
                    memory_batch = _refresh_semantic_batch_memory(conn, project_id, memory_batch_id) or memory_batch
                memory_summary = _semantic_batch_memory_summary(memory_batch) if memory_enabled else {}
                graph_state_summary = (
                    _semantic_graph_state_summary(semantic_state)
                    if semantic_state_enabled
                    else {}
                )
                batch_payload = {
                    "schema_version": SEMANTIC_ENRICHMENT_SCHEMA_VERSION,
                    "project_id": project_id,
                    "snapshot_id": snapshot_id,
                    "snapshot_kind": snapshot.get("snapshot_kind") or "",
                    "commit_sha": snapshot.get("commit_sha") or "",
                    "feedback_round": round_number,
                    "batch_index": batch_index,
                    "batch_key": batch_key,
                    "batch_by": semantic_ai_batch_by,
                    "feature_count": len(batch),
                    "semantic_ai_input_mode": ai_input_mode,
                    "dynamic_semantic_graph_state": bool(dynamic_graph_state and semantic_state_enabled),
                    "features": [
                        {
                            "feature": record["payload"]["feature"],
                            "review_feedback": record["payload"]["review_feedback"],
                            "semantic_selection": record["payload"]["semantic_selection"],
                            "quality_flags": record["flags"],
                            "related_batch_features": (
                                _semantic_memory_related_features(memory_batch, record["feature"])
                                if memory_enabled
                                else []
                            ),
                            "related_graph_features": (
                                _semantic_graph_related_features(semantic_state, record["feature"])
                                if semantic_state_enabled
                                else []
                            ),
                        }
                        for record in batch
                    ],
                    "semantic_graph_state": graph_state_summary,
                    "batch_memory": memory_summary,
                    "instructions": {
                        **semantic_config.to_instruction_payload("node"),
                        "batch_mode": True,
                        "semantic_ai_input_mode": ai_input_mode,
                        "use_semantic_graph_state": bool(semantic_state_enabled),
                        "use_batch_memory": bool(memory_enabled),
                        "output_contract": (
                            "Return one JSON object with a features array. Each item must include "
                            "node_id and the same semantic fields used for single-feature enrichment."
                        ),
                    },
                    "semantic_selector": selector,
                }
                batch_name = f"batch-{batch_index:03d}-{_safe_node_filename(batch_key)}"
                if persist_feature_payloads:
                    write_json(
                        payload_trace_base / "batch-inputs" / f"{batch_name}.json",
                        batch_payload,
                    )
                if semantic_state_enabled:
                    for record in batch:
                        _upsert_semantic_job(
                            semantic_state,
                            record["feature"],
                            status="running",
                            feedback_round=round_number,
                            batch_index=batch_index,
                            updated_at=utc_now(),
                            increment_attempt=True,
                        )
                    _write_semantic_graph_state_artifacts(
                        conn,
                        project_id,
                        snapshot_id,
                        graph_json,
                        semantic_state,
                        round_number=round_number,
                        feature_index=feature_index,
                        submit_for_review=submit_for_review,
                        review_node_ids=review_node_ids,
                        review_edge_ids=review_edge_ids,
                    )
                batch_response = _call_ai(
                    ai_call,
                    stage="reconcile_semantic_feature_batch",
                    payload=batch_payload,
                )
                if batch_response and not batch_response.get("_ai_error"):
                    ai_batch_complete_count += 1
                else:
                    ai_batch_error_count += 1
                if persist_feature_payloads:
                    write_json(
                        payload_trace_base / "batch-outputs" / f"{batch_name}.json",
                        {
                            "batch_index": batch_index,
                            "batch_key": batch_key,
                            "node_ids": [
                                record["feature"].get("node_id") or ""
                                for record in batch
                            ],
                            "ai_response_present": bool(batch_response and not batch_response.get("_ai_error")),
                            "ai_error": (
                                batch_response.get("_ai_error")
                                if isinstance(batch_response, dict)
                                else ""
                            ),
                            "ai_response": batch_response if isinstance(batch_response, dict) else None,
                        },
                    )
                ai_responses.update(_extract_batch_ai_responses(batch_response, batch))
                for record in batch:
                    node_id = str(record["feature"].get("node_id") or "")
                    response = ai_responses.get(node_id)
                    if semantic_state_enabled:
                        _upsert_semantic_job(
                            semantic_state,
                            record["feature"],
                            status=(
                                "ai_failed"
                                if response is None or response.get("_ai_error")
                                else "ai_complete"
                            ),
                            feedback_round=round_number,
                            batch_index=batch_index,
                            updated_at=utc_now(),
                            last_error=(
                                str(response.get("_ai_error") or "")
                                if isinstance(response, dict)
                                else "ai_response_missing"
                            ),
                        )
                    if semantic_state_enabled and response is not None and not response.get("_ai_error"):
                        state_entry = _heuristic_semantic_entry(
                            record["feature"],
                            record["feedback"],
                            enrichment_status="ai_complete",
                            ai_response=response,
                        )
                        if response.get("_ai_route"):
                            state_entry["semantic_ai_route"] = response.get("_ai_route")
                        _upsert_semantic_graph_state_entry(
                            semantic_state,
                            record["feature"],
                            state_entry,
                            feedback_round=round_number,
                            batch_index=batch_index,
                            updated_at=utc_now(),
                        )
                        review_node_ids.add(node_id)
                    if not (memory_enabled and response is not None and not response.get("_ai_error")):
                        continue
                    updated_batch, update_error = _record_semantic_memory_decision(
                        conn,
                        project_id,
                        memory_batch_id,
                        snapshot_id,
                        record["feature"],
                        response,
                    )
                    if update_error:
                        memory_update_error_count += 1
                    else:
                        memory_decision_count += 1
                        memory_batch = updated_batch or memory_batch
                if semantic_state_enabled:
                    _write_semantic_graph_state_artifacts(
                        conn,
                        project_id,
                        snapshot_id,
                        graph_json,
                        semantic_state,
                        round_number=round_number,
                        feature_index=feature_index,
                        submit_for_review=submit_for_review,
                        review_node_ids=review_node_ids,
                        review_edge_ids=review_edge_ids,
                    )

    for record in records:
        feature = record["feature"]
        relevant_feedback = record["feedback"]
        selected_for_ai = bool(record["selected_for_ai"])
        selection_reasons = record["selection_reasons"]
        node_id = str(feature.get("node_id") or "")
        node_name = record["node_name"]
        ai_allowed = node_id in allowed_node_ids
        ai_response = ai_responses.get(node_id)
        if record.get("skipped_completed") and record.get("existing_semantic"):
            status = "semantic_graph_state"
            semantic_graph_state_hit_count += 1
            semantic_entry = _semantic_entry_from_state(feature, record["existing_semantic"])
        else:
            if ai_response is not None and not ai_response.get("_ai_error"):
                status = "ai_complete"
                ai_complete_count += 1
            elif ai_response is not None and ai_response.get("_ai_error"):
                status = "ai_unavailable"
                ai_unavailable_count += 1
                ai_error_count += 1
            elif effective_use_ai and not selected_for_ai:
                status = "ai_skipped_selector"
                ai_skipped_count += 1
                ai_skipped_selector_count += 1
            elif effective_use_ai and not ai_allowed:
                status = "ai_skipped_limit"
                ai_skipped_count += 1
            else:
                status = "ai_unavailable" if effective_use_ai else "heuristic"
                if effective_use_ai:
                    ai_unavailable_count += 1
            semantic_entry = _heuristic_semantic_entry(
                feature,
                relevant_feedback,
                enrichment_status=status,
                ai_response=ai_response if ai_response and not ai_response.get("_ai_error") else None,
            )
            validation = record.get("semantic_state_validation") or {}
            if validation.get("status") == "stale_hash_mismatch":
                semantic_entry.setdefault("quality_flags", []).append("semantic_hash_mismatch")
                semantic_entry["semantic_state_validation"] = validation
            if ai_response and ai_response.get("_ai_error"):
                semantic_entry.setdefault("quality_flags", []).append("semantic_ai_error")
                semantic_entry["semantic_ai_error"] = ai_response.get("_ai_error")
            elif ai_response:
                if ai_response.get("_ai_route"):
                    semantic_entry["semantic_ai_route"] = ai_response.get("_ai_route")
                if ai_response.get("_ai_elapsed_ms") is not None:
                    semantic_entry["semantic_ai_elapsed_ms"] = ai_response.get("_ai_elapsed_ms")
        semantic_entry["semantic_selection_status"] = "selected" if selected_for_ai else "not_selected"
        semantic_entry["semantic_selection_reasons"] = selection_reasons
        semantic_entry["health_issues"] = _semantic_health_issues(
            node_id,
            semantic_entry,
            open_issues=_semantic_open_issues(node_id, semantic_entry),
        )
        if persist_feature_payloads:
            payload_output_paths.append(write_json(
                payload_trace_base / "feature-outputs" / f"{node_name}.json",
                {
                    "node_id": feature.get("node_id"),
                    "enrichment_status": status,
                    "ai_response_present": bool(ai_response and not ai_response.get("_ai_error")),
                    "ai_error": ai_response.get("_ai_error") if isinstance(ai_response, dict) else "",
                    "ai_response": ai_response if isinstance(ai_response, dict) else None,
                    "semantic_selector": selector,
                    "semantic_selection_status": semantic_entry["semantic_selection_status"],
                    "semantic_selection_reasons": selection_reasons,
                    "semantic_entry": semantic_entry,
                },
            ))
        semantic_features.append(semantic_entry)

    if memory_enabled and memory_batch_id:
        memory_batch = _refresh_semantic_batch_memory(conn, project_id, memory_batch_id) or memory_batch
    memory_summary = _semantic_batch_memory_summary(memory_batch) if memory_enabled else {}
    memory_report = {
        "enabled": bool(memory_enabled),
        "batch_id": memory_batch_id,
        "error": memory_error,
        "decision_count": memory_decision_count,
        "update_error_count": memory_update_error_count,
        "accepted_feature_count": memory_summary.get("accepted_feature_count", 0),
        "file_ownership_count": memory_summary.get("file_ownership_count", 0),
        "open_conflict_count": memory_summary.get("open_conflict_count", 0),
    }
    ai_attempted_count = len(allowed_records)
    semantic_status = _semantic_run_status(
        feature_count=len(semantic_features),
        effective_use_ai=bool(effective_use_ai),
        ai_selected_count=ai_selected_count,
        ai_attempted_count=ai_attempted_count,
        ai_complete_count=ai_complete_count,
        ai_error_count=ai_error_count,
        semantic_graph_state_hit_count=semantic_graph_state_hit_count,
    )
    semantic_graph_state_report = {
        "enabled": bool(semantic_state_enabled),
        "source": semantic_state.get("source") or "db",
        "hit_count": semantic_graph_state_hit_count,
        "skip_completed": bool(semantic_skip_completed),
        "completed_node_count": len(semantic_state.get("completed_node_ids") or []),
        "accepted_feature_count": len(semantic_state.get("accepted_features") or {}),
        "file_ownership_count": len(semantic_state.get("file_ownership") or {}),
        "open_issue_count": len(semantic_state.get("open_issues") or []),
        "health_issue_count": len(semantic_state.get("health_issues") or []),
        "semantic_job_counts": semantic_state.get("semantic_job_counts") or {},
        "hash_mismatch_count": semantic_hash_mismatch_count,
        "base_snapshot_id": carry_forward_report.get("base_snapshot_id", ""),
        "carried_forward_count": carry_forward_report.get("carried_forward_count", 0),
        "carry_forward": carry_forward_report,
        "state_path": "",
        "semantic_graph_path": "",
        "round_state_path": "",
        "round_semantic_graph_path": "",
    }
    if semantic_state_enabled:
        (
            latest_state_path,
            latest_graph_path,
            round_state_path,
            round_graph_path,
        ) = _write_semantic_graph_state_artifacts(
            conn,
            project_id,
            snapshot_id,
            graph_json,
            semantic_state,
            round_number=round_number,
            feature_index=feature_index,
            submit_for_review=submit_for_review,
            review_node_ids=review_node_ids,
            review_edge_ids=review_edge_ids,
        )
        semantic_graph_state_report.update({
            "state_path": str(latest_state_path),
            "semantic_graph_path": str(latest_graph_path),
            "round_state_path": str(round_state_path),
            "round_semantic_graph_path": str(round_graph_path),
        })

    semantic_index = {
        "schema_version": SEMANTIC_ENRICHMENT_SCHEMA_VERSION,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "snapshot_kind": snapshot.get("snapshot_kind") or "",
        "commit_sha": snapshot.get("commit_sha") or "",
        "feedback_round": round_number,
        "generated_at": generated_at,
        "created_by": created_by,
        "ai_requested": bool(effective_use_ai),
        "semantic_run_status": semantic_status,
        "semantic_selector": selector,
        "semantic_config": semantic_config.summary(),
        "semantic_batching": {
            "input_mode": ai_input_mode,
            "dynamic_semantic_graph_state": bool(dynamic_graph_state and semantic_state_enabled),
            "requested_batch_size": requested_ai_batch_size,
            "batch_size": ai_batch_size,
            "batch_by": semantic_ai_batch_by,
            "batch_count": ai_batch_count,
        },
        "semantic_graph_state": semantic_graph_state_report,
        "semantic_batch_memory": memory_report,
        "feature_count": len(semantic_features),
        "features": sorted(semantic_features, key=lambda item: str(item.get("node_id") or "")),
    }
    unresolved_feedback = sorted({
        feedback_id
        for item in semantic_features
        for feedback_id in (item.get("unresolved_feedback_ids") or [])
    })
    report = {
        "schema_version": SEMANTIC_ENRICHMENT_SCHEMA_VERSION,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "feedback_round": round_number,
        "generated_at": generated_at,
        "feature_count": len(semantic_features),
        "semantic_run_status": semantic_status,
        "ai_complete_count": ai_complete_count,
        "ai_unavailable_count": ai_unavailable_count,
        "ai_error_count": ai_error_count,
        "ai_skipped_count": ai_skipped_count,
        "ai_selected_count": ai_selected_count,
        "ai_attempted_count": ai_attempted_count,
        "ai_skipped_selector_count": ai_skipped_selector_count,
        "semantic_hash_mismatch_count": semantic_hash_mismatch_count,
        "ai_input_mode": ai_input_mode,
        "dynamic_semantic_graph_state": bool(dynamic_graph_state and semantic_state_enabled),
        "requested_ai_batch_size": requested_ai_batch_size,
        "ai_batch_size": ai_batch_size,
        "ai_batch_by": semantic_ai_batch_by,
        "ai_batch_count": ai_batch_count,
        "ai_batch_complete_count": ai_batch_complete_count,
        "ai_batch_error_count": ai_batch_error_count,
        "semantic_graph_state": semantic_graph_state_report,
        "semantic_batch_memory": memory_report,
        "feedback_count": len(feedback),
        "semantic_selector": selector,
        "semantic_config": semantic_config.summary(),
        "unresolved_feedback_count": len(unresolved_feedback),
        "unresolved_feedback_ids": unresolved_feedback,
        "quality_flag_counts": _count_quality_flags(semantic_features),
        "health_issue_counts": _count_health_issues(semantic_features),
        "feature_payload_input_count": len(payload_input_paths),
        "feature_payload_output_count": len(payload_output_paths),
        "feature_payload_input_dir": str(payload_trace_base / "feature-inputs") if persist_feature_payloads else "",
        "feature_payload_output_dir": str(payload_trace_base / "feature-outputs") if persist_feature_payloads else "",
        "batch_payload_input_dir": str(payload_trace_base / "batch-inputs") if persist_feature_payloads else "",
        "batch_payload_output_dir": str(payload_trace_base / "batch-outputs") if persist_feature_payloads else "",
    }

    base = _semantic_base_dir(project_id, snapshot_id)
    rdir = _round_dir(project_id, snapshot_id, round_number)
    semantic_index_path = rdir / SEMANTIC_INDEX_NAME
    review_report_path = rdir / SEMANTIC_REVIEW_REPORT_NAME
    latest_semantic_path = base / SEMANTIC_INDEX_NAME
    latest_report_path = base / SEMANTIC_REVIEW_REPORT_NAME
    _write_json(semantic_index_path, semantic_index)
    _write_json(review_report_path, report)
    _write_json(latest_semantic_path, semantic_index)
    _write_json(latest_report_path, report)
    notes_patch = {
        "semantic_enrichment": {
            "schema_version": SEMANTIC_ENRICHMENT_SCHEMA_VERSION,
            "latest_round": round_number,
            "semantic_index_path": str(latest_semantic_path),
            "review_report_path": str(latest_report_path),
            "latest_round_semantic_index_path": str(semantic_index_path),
            "latest_round_review_report_path": str(review_report_path),
            "feature_count": len(semantic_features),
            "semantic_run_status": semantic_status,
            "ai_complete_count": ai_complete_count,
            "ai_selected_count": ai_selected_count,
            "ai_attempted_count": ai_attempted_count,
            "ai_skipped_selector_count": ai_skipped_selector_count,
            "semantic_hash_mismatch_count": semantic_hash_mismatch_count,
            "ai_input_mode": ai_input_mode,
            "dynamic_semantic_graph_state": bool(dynamic_graph_state and semantic_state_enabled),
            "requested_ai_batch_size": requested_ai_batch_size,
            "ai_batch_size": ai_batch_size,
            "ai_batch_by": semantic_ai_batch_by,
            "ai_batch_count": ai_batch_count,
            "semantic_graph_state": semantic_graph_state_report,
            "semantic_batch_memory": memory_report,
            "feedback_count": len(feedback),
            "unresolved_feedback_count": len(unresolved_feedback),
            "updated_at": generated_at,
            "created_by": created_by,
        }
    }
    _update_snapshot_notes(conn, project_id, snapshot_id, notes_patch)
    return {
        "ok": True,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "feedback_round": round_number,
        "semantic_index_path": str(latest_semantic_path),
        "review_report_path": str(latest_report_path),
        "round_semantic_index_path": str(semantic_index_path),
        "round_review_report_path": str(review_report_path),
        "summary": report,
        "semantic_index": semantic_index,
    }


def _count_quality_flags(features: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for feature in features:
        for flag in feature.get("quality_flags") or []:
            key = str(flag)
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _count_health_issues(features: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for feature in features:
        for issue in feature.get("health_issues") or []:
            if not isinstance(issue, dict):
                continue
            key = str(issue.get("category") or "semantic_review")
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


__all__ = [
    "SEMANTIC_ENRICHMENT_SCHEMA_VERSION",
    "append_review_feedback",
    "claim_semantic_jobs",
    "feedback_review_gate",
    "load_review_feedback",
    "normalize_feedback_item",
    "run_semantic_enrichment",
]
