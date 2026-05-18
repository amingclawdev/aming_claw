"""Auditable graph governance events for dashboard/operator actions.

Graph events are a state-only layer between dashboard feedback and graph
snapshot materialization.  They let users, observers, or AI analyzers propose
the same operation vocabulary, then materialize accepted operations into a new
candidate snapshot instead of mutating the current snapshot in place.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import uuid
from typing import Any, Iterable

from . import graph_snapshot_store as store


GRAPH_EVENTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_events (
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_seq INTEGER NOT NULL DEFAULT 0,
  event_kind TEXT NOT NULL DEFAULT 'user_feedback',
  event_type TEXT NOT NULL,
  target_type TEXT NOT NULL DEFAULT '',
  target_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'proposed',
  risk_level TEXT NOT NULL DEFAULT 'low',
  confidence REAL NOT NULL DEFAULT 0.0,
  baseline_commit TEXT NOT NULL DEFAULT '',
  target_commit TEXT NOT NULL DEFAULT '',
  branch_ref TEXT NOT NULL DEFAULT '',
  operation_type TEXT NOT NULL DEFAULT '',
  source_branch_ref TEXT NOT NULL DEFAULT '',
  source_snapshot_id TEXT NOT NULL DEFAULT '',
  source_event_id TEXT NOT NULL DEFAULT '',
  payload_hash TEXT NOT NULL DEFAULT '',
  stable_node_key TEXT NOT NULL DEFAULT '',
  feature_hash TEXT NOT NULL DEFAULT '',
  file_hashes_json TEXT NOT NULL DEFAULT '{}',
  payload_json TEXT NOT NULL DEFAULT '{}',
  precondition_json TEXT NOT NULL DEFAULT '{}',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  ai_review_json TEXT NOT NULL DEFAULT '{}',
  backlog_bug_id TEXT NOT NULL DEFAULT '',
  materialized_snapshot_id TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '',
  updated_by TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(project_id, snapshot_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_events_status
  ON graph_events(project_id, snapshot_id, status, event_type);

CREATE INDEX IF NOT EXISTS idx_graph_events_target
  ON graph_events(project_id, snapshot_id, target_type, target_id);

CREATE INDEX IF NOT EXISTS idx_graph_events_seq
  ON graph_events(project_id, snapshot_id, event_seq);

CREATE INDEX IF NOT EXISTS idx_graph_events_stable_key
  ON graph_events(project_id, stable_node_key, event_type, status, updated_at);

CREATE TABLE IF NOT EXISTS graph_semantic_projections (
  project_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL,
  projection_id TEXT NOT NULL,
  base_commit TEXT NOT NULL DEFAULT '',
  ref_name TEXT NOT NULL DEFAULT '',
  branch_ref TEXT NOT NULL DEFAULT '',
  projection_rule_version TEXT NOT NULL DEFAULT '',
  event_watermark INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'current',
  projection_json TEXT NOT NULL DEFAULT '{}',
  health_json TEXT NOT NULL DEFAULT '{}',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(project_id, snapshot_id, projection_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_semantic_projections_latest
  ON graph_semantic_projections(project_id, snapshot_id, created_at);
"""


EVENT_STATUS_PROPOSED = "proposed"
EVENT_STATUS_ACCEPTED = "accepted"
EVENT_STATUS_REJECTED = "rejected"
EVENT_STATUS_STALE = "stale"
EVENT_STATUS_FAILED = "failed"
EVENT_STATUS_MATERIALIZED = "materialized"
EVENT_STATUS_AI_REVIEWING = "ai_reviewing"
EVENT_STATUS_BACKLOG_FILED = "backlog_filed"
EVENT_STATUS_OBSERVED = "observed"

ALLOWED_EVENT_STATUSES = {
    EVENT_STATUS_PROPOSED,
    EVENT_STATUS_ACCEPTED,
    EVENT_STATUS_REJECTED,
    EVENT_STATUS_STALE,
    EVENT_STATUS_FAILED,
    EVENT_STATUS_MATERIALIZED,
    EVENT_STATUS_AI_REVIEWING,
    EVENT_STATUS_BACKLOG_FILED,
    EVENT_STATUS_OBSERVED,
}

ALLOWED_EVENT_TYPES = {
    "file_hash_changed",
    "node_added",
    "node_removed",
    "node_rename_proposed",
    "node_reparented",
    "node_split",
    "node_merged",
    "edge_added",
    "edge_removed",
    "edge_reclassified",
    "doc_binding_added",
    "doc_binding_removed",
    "test_binding_added",
    "test_binding_removed",
    "config_binding_added",
    "config_binding_removed",
    "package_marker_excluded",
    "feature_marked_dead_code",
    "feature_marked_duplicate",
    "semantic_retry_requested",
    "semantic_enriched",
    "semantic_job_requested",
    "semantic_node_enriched",
    "semantic_stale",
    "semantic_global_review_generated",
    "semantic_projection_generated",
    "edge_semantic_requested",
    "edge_semantic_enriched",
    "graph_structure_requested",
    "graph_structure_completed",
    "graph_structure_failed",
    "graph_correction_proposed",
    "graph_correction_accepted",
    "graph_correction_rejected",
    "backlog_candidate_requested",
    "file_attach_requested",
    "file_node_create_requested",
    "file_delete_candidate",
    "file_waived",
}

GRAPH_MUTATION_EVENT_TYPES = {
    "node_rename_proposed",
    "node_reparented",
    "edge_added",
    "edge_removed",
    "edge_reclassified",
    "doc_binding_added",
    "doc_binding_removed",
    "test_binding_added",
    "test_binding_removed",
    "config_binding_added",
    "config_binding_removed",
    "package_marker_excluded",
    "feature_marked_dead_code",
    "feature_marked_duplicate",
    "semantic_stale",
}

SEMANTIC_PROJECTION_RULE_VERSION = "semantic_projection_v3_branch_timeline"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(GRAPH_EVENTS_SCHEMA_SQL)
    _ensure_graph_event_columns(conn)
    _ensure_semantic_projection_columns(conn)


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {
        str(row["name"] if hasattr(row, "keys") else row[1])
        for row in rows
    }


def _ensure_columns(conn: sqlite3.Connection, table_name: str, columns: dict[str, str]) -> None:
    existing = _table_columns(conn, table_name)
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {ddl}")


def _ensure_graph_event_columns(conn: sqlite3.Connection) -> None:
    _ensure_columns(conn, "graph_events", {
        "branch_ref": "TEXT NOT NULL DEFAULT ''",
        "operation_type": "TEXT NOT NULL DEFAULT ''",
        "source_branch_ref": "TEXT NOT NULL DEFAULT ''",
        "source_snapshot_id": "TEXT NOT NULL DEFAULT ''",
        "source_event_id": "TEXT NOT NULL DEFAULT ''",
        "payload_hash": "TEXT NOT NULL DEFAULT ''",
        "stable_node_key": "TEXT NOT NULL DEFAULT ''",
        "feature_hash": "TEXT NOT NULL DEFAULT ''",
        "file_hashes_json": "TEXT NOT NULL DEFAULT '{}'",
    })


def _ensure_semantic_projection_columns(conn: sqlite3.Connection) -> None:
    _ensure_columns(conn, "graph_semantic_projections", {
        "ref_name": "TEXT NOT NULL DEFAULT ''",
        "branch_ref": "TEXT NOT NULL DEFAULT ''",
        "projection_rule_version": "TEXT NOT NULL DEFAULT ''",
    })


def _json(data: Any) -> str:
    return json.dumps(data if data is not None else {}, ensure_ascii=False, sort_keys=True)


def _json_load(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return default
        return value if value is not None else default
    return default


def _string_list(raw: Any) -> list[str]:
    if raw is None:
        values: Iterable[Any] = []
    elif isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, Iterable):
        values = raw
    else:
        values = [raw]
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item or "").replace("\\", "/").strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


def _node_id(node: dict[str, Any]) -> str:
    return str(node.get("id") or node.get("node_id") or "").strip()


def _graph_nodes(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    deps = graph_json.get("deps_graph") if isinstance(graph_json, dict) else {}
    if isinstance(deps, dict) and isinstance(deps.get("nodes"), list):
        return [node for node in deps.get("nodes", []) if isinstance(node, dict)]
    nodes = graph_json.get("nodes") if isinstance(graph_json, dict) else []
    if isinstance(nodes, list):
        return [node for node in nodes if isinstance(node, dict)]
    if isinstance(nodes, dict):
        out: list[dict[str, Any]] = []
        for node_id, node in nodes.items():
            item = dict(node) if isinstance(node, dict) else {}
            item.setdefault("id", str(node_id))
            out.append(item)
        return out
    return []


def _ensure_deps_graph(graph_json: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    graph = graph_json if isinstance(graph_json, dict) else {}
    deps = graph.setdefault("deps_graph", {})
    if not isinstance(deps, dict):
        deps = {}
        graph["deps_graph"] = deps
    nodes = deps.setdefault("nodes", [])
    edges = deps.setdefault("edges", [])
    if not isinstance(nodes, list):
        nodes = []
        deps["nodes"] = nodes
    if not isinstance(edges, list):
        edges = []
        deps["edges"] = edges
    return nodes, edges


def _node_map(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {_node_id(node): node for node in nodes if _node_id(node)}


def _metadata(node: dict[str, Any]) -> dict[str, Any]:
    metadata = node.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
        node["metadata"] = metadata
    return metadata


def _node_metadata(node: dict[str, Any]) -> dict[str, Any]:
    metadata = node.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


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


def _hash_payload(payload: Any) -> str:
    import hashlib

    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _feature_hash_payload(payload: Any) -> str:
    import hashlib

    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _hash_values_equal(left: str, right: str) -> bool:
    lhs = str(left or "").strip()
    rhs = str(right or "").strip()
    if not lhs or not rhs:
        return False
    if lhs == rhs:
        return True
    if lhs.startswith("sha256:"):
        lhs = lhs.split(":", 1)[1]
    if rhs.startswith("sha256:"):
        rhs = rhs.split(":", 1)[1]
    return bool(lhs and rhs and lhs == rhs)


def _hash_scheme(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "missing"
    if text.startswith("sha256:"):
        return "indexed_sha256"
    if len(text) == 64 and all(ch in "0123456789abcdefABCDEF" for ch in text):
        return "fallback_sha256"
    return "opaque"


def _row_get(row: sqlite3.Row | dict[str, Any], key: str, default: Any = "") -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key] if key in row.keys() else default
    except Exception:
        return default


def _snapshot_branch_ref(conn: sqlite3.Connection, project_id: str, snapshot_id: str) -> str:
    """Return the best known branch/ref for a snapshot.

    Structural snapshots remain commit evidence.  The semantic timeline needs
    a branch/ref so future branch projections do not import another branch's
    proposed semantics by accident.  Existing installations only have the
    generic `active` ref, so this function is intentionally conservative.
    """
    try:
        row = conn.execute(
            """
            SELECT ref_name, branch_ref FROM graph_snapshots
            WHERE project_id = ? AND snapshot_id = ?
            """,
            (project_id, snapshot_id),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row:
        branch = str(_row_get(row, "branch_ref") or "").strip()
        if branch:
            return branch
        ref = str(_row_get(row, "ref_name") or "").strip()
        if ref and ref != "active":
            return ref
    try:
        rows = conn.execute(
            """
            SELECT ref_name FROM graph_snapshot_refs
            WHERE project_id = ? AND snapshot_id = ?
            ORDER BY CASE WHEN ref_name = 'active' THEN 1 ELSE 0 END, ref_name
            """,
            (project_id, snapshot_id),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        ref = str(_row_get(row, "ref_name") or "").strip()
        if ref and ref != "active":
            return ref
    return ""


def _projection_ref_identity(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    ref_name: str = "",
    branch_ref: str = "",
) -> dict[str, str]:
    ref = str(ref_name or "").strip()
    branch = str(branch_ref or "").strip()
    snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id) or {}
    if not ref:
        ref = str(snapshot.get("ref_name") or "").strip()
    if not branch:
        branch = str(snapshot.get("branch_ref") or "").strip()
    if not branch and ref and ref != "active":
        branch = ref
    if not branch:
        branch = _snapshot_branch_ref(conn, project_id, snapshot_id)
    if not ref:
        ref = branch or "active"
    return {"ref_name": ref, "branch_ref": branch}


def _semantic_operation_type(
    event_type: str,
    status: str,
    payload: dict[str, Any] | None,
    evidence: dict[str, Any] | None,
    explicit: str = "",
) -> str:
    explicit = str(explicit or "").strip()
    if explicit:
        return explicit
    payload = payload if isinstance(payload, dict) else {}
    evidence = evidence if isinstance(evidence, dict) else {}
    semantic_payload = _event_payload_semantic({"payload": payload})
    if not isinstance(semantic_payload, dict):
        semantic_payload = {}
    if semantic_payload.get("operation_type"):
        return str(semantic_payload.get("operation_type") or "")
    if semantic_payload.get("carried_forward_from_snapshot_id") or payload.get("source_snapshot_id"):
        return "carry_forward"
    if status == EVENT_STATUS_REJECTED:
        return "reject"
    if status == EVENT_STATUS_ACCEPTED:
        return "accept"
    if event_type in {"semantic_node_enriched", "semantic_enriched", "edge_semantic_enriched"}:
        return "ai_enrich"
    if event_type in {"semantic_job_requested", "semantic_retry_requested", "edge_semantic_requested"}:
        return "retry" if "retry" in event_type else "ai_enrich"
    if event_type.startswith("graph_structure_"):
        return "graph_structure"
    if event_type == "semantic_projection_generated":
        return "projection_build"
    return event_type


def _payload_hash(payload: dict[str, Any] | None, explicit: str = "") -> str:
    explicit = str(explicit or "").strip()
    if explicit:
        return explicit
    return _hash_payload(payload or {})


def stable_node_key_for_node(node: dict[str, Any]) -> str:
    """Return a semantic-stable key that survives L-id and title churn."""
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    explicit = str(metadata.get("stable_node_key") or metadata.get("stable_key") or "").strip()
    if explicit:
        return explicit
    module = str(metadata.get("module") or "").strip()
    primary = "|".join(_node_files(node, "primary"))
    layer = str(node.get("layer") or "").strip()
    kind = str(node.get("kind") or metadata.get("kind") or metadata.get("file_role") or "").strip()
    return _hash_payload({
        "module": module,
        "primary": primary,
        "layer": layer,
        "kind": kind,
    })


def feature_hash_for_node(node: dict[str, Any]) -> str:
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    indexed_hash = str(metadata.get("feature_hash") or "").strip()
    if indexed_hash:
        return indexed_hash
    return _feature_hash_payload({
        "node_id": _node_id(node),
        "title": node.get("title") or "",
        "primary": _node_files(node, "primary"),
        "secondary": _node_files(node, "secondary"),
        "test": _node_files(node, "test"),
        "config": _node_files(node, "config"),
        "functions": metadata.get("functions") or [],
    })


def stable_edge_key_for_edge(
    edge: dict[str, Any],
    src_node: dict[str, Any] | None,
    dst_node: dict[str, Any] | None,
) -> str:
    """Hash that survives node L-id renumbering — pairs the endpoints'
    stable_node_keys with the edge_type. Used to find an edge's latest
    semantic event across snapshots even when src/dst node_ids were
    renumbered by the graph adapter.

    Falls back to raw src/dst when stable keys can't be derived (e.g.
    the endpoint node isn't in the current snapshot's node set yet).
    """
    src_key = stable_node_key_for_node(src_node) if src_node else ""
    dst_key = stable_node_key_for_node(dst_node) if dst_node else ""
    edge_type = str(edge.get("edge_type") or edge.get("type") or "depends_on").strip() or "depends_on"
    return _hash_payload({
        "src_stable": src_key or str(edge.get("src") or edge.get("source") or ""),
        "dst_stable": dst_key or str(edge.get("dst") or edge.get("target") or ""),
        "edge_type": edge_type,
    })


def edge_signature_hash_for_edge(
    edge: dict[str, Any],
    src_node: dict[str, Any] | None,
    dst_node: dict[str, Any] | None,
) -> str:
    """Drift detector — the edge analog of feature_hash for nodes. Carry-
    forward compares this hash between snapshots; if it differs, the edge
    needs re-enrichment. Composed of:
      - stable_edge_key (identity)
      - both endpoints' feature_hash (so endpoint content drift triggers)
      - edge_type
      - sorted evidence keys (so evidence shape change triggers)
    Adapters that pre-compute this and stash it in edge.metadata.
    edge_signature_hash should win — that's the indexed value used at
    snapshot build time.
    """
    metadata = edge.get("metadata") if isinstance(edge.get("metadata"), dict) else {}
    indexed_hash = str(metadata.get("edge_signature_hash") or "").strip()
    if indexed_hash:
        return indexed_hash
    src_hash = feature_hash_for_node(src_node) if src_node else ""
    dst_hash = feature_hash_for_node(dst_node) if dst_node else ""
    evidence = edge.get("evidence") if isinstance(edge.get("evidence"), dict) else {}
    return _feature_hash_payload({
        "stable_edge_key": stable_edge_key_for_edge(edge, src_node, dst_node),
        "src_feature_hash": src_hash,
        "dst_feature_hash": dst_hash,
        "edge_type": str(edge.get("edge_type") or edge.get("type") or "depends_on"),
        "direction": str(edge.get("direction") or ""),
        "evidence_keys": sorted(evidence.keys()),
    })


def _node_files(node: dict[str, Any], key: str) -> list[str]:
    aliases = {
        "primary": ("primary", "primary_files"),
        "secondary": ("secondary", "secondary_files"),
        "test": ("test", "test_files"),
        "config": ("config", "config_files"),
    }
    for alias in aliases.get(key, (key,)):
        if alias in node:
            return _string_list(node.get(alias))
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    if key == "config":
        return _string_list(metadata.get("config_files"))
    return []


def _set_node_files(node: dict[str, Any], key: str, files: list[str]) -> None:
    aliases = {
        "secondary": ("secondary", "secondary_files"),
        "test": ("test", "test_files"),
        "config": ("config", "config_files"),
    }
    target = aliases.get(key, (key,))[0]
    node[target] = _string_list(files)
    if key == "config":
        _metadata(node)["config_files"] = _string_list(files)


def _add_unique(items: list[str], item: str) -> None:
    if item and item not in items:
        items.append(item)


def _edge_key(edge: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(edge.get("src") or edge.get("source") or ""),
        str(edge.get("dst") or edge.get("target") or ""),
        str(edge.get("edge_type") or edge.get("type") or "depends_on"),
        str(edge.get("direction") or "dependency"),
    )


def _edge_id(edge: dict[str, Any]) -> str:
    src, dst, edge_type, _direction = _edge_key(edge)
    return f"{src}->{dst}:{edge_type}" if src and dst else ""


def _edge_id_variants(edge_id: str) -> list[str]:
    raw = str(edge_id or "").strip()
    if not raw:
        return []
    variants = [raw]
    if "|" in raw:
        parts = [part.strip() for part in raw.split("|")]
        if len(parts) == 3 and all(parts):
            variants.insert(0, f"{parts[0]}->{parts[1]}:{parts[2]}")
    if "->" in raw and ":" in raw:
        src, rest = raw.split("->", 1)
        dst, edge_type = rest.rsplit(":", 1)
        pipe = f"{src}|{dst}|{edge_type}"
        variants.append(pipe)
    deduped: list[str] = []
    for variant in variants:
        if variant and variant not in deduped:
            deduped.append(variant)
    return deduped


def _edge_from_id(edge_id: str) -> dict[str, Any]:
    text = str(edge_id or "").strip()
    if "->" not in text:
        return {"edge_id": text}
    src, rest = text.split("->", 1)
    if ":" in rest:
        dst, edge_type = rest.rsplit(":", 1)
    else:
        dst, edge_type = rest, "depends_on"
    return {
        "edge_id": text,
        "src": src,
        "dst": dst,
        "edge_type": edge_type or "depends_on",
    }


def _append_edge(edges: list[dict[str, Any]], edge: dict[str, Any]) -> bool:
    src, dst, edge_type, direction = _edge_key(edge)
    if not src or not dst:
        raise ValueError("edge_added requires src and dst")
    for existing in edges:
        if _edge_key(existing) == (src, dst, edge_type, direction):
            return False
    item = dict(edge)
    item["source"] = src
    item["target"] = dst
    item["edge_type"] = edge_type
    item["direction"] = direction
    edges.append(item)
    return True


def _remove_edge(edges: list[dict[str, Any]], edge: dict[str, Any]) -> int:
    src, dst, edge_type, _direction = _edge_key(edge)
    if not src or not dst:
        raise ValueError("edge_removed requires src and dst")
    before = len(edges)
    edges[:] = [
        existing for existing in edges
        if not (
            str(existing.get("src") or existing.get("source") or "") == src
            and str(existing.get("dst") or existing.get("target") or "") == dst
            and str(existing.get("edge_type") or existing.get("type") or "depends_on") == edge_type
        )
    ]
    return before - len(edges)


def _next_event_seq(conn: sqlite3.Connection, project_id: str, snapshot_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(event_seq), 0) AS max_seq FROM graph_events WHERE project_id = ? AND snapshot_id = ?",
        (project_id, snapshot_id),
    ).fetchone()
    return int(row["max_seq"] if row else 0) + 1


def _row_to_event(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["file_hashes"] = _json_load(item.pop("file_hashes_json", "{}"), {})
    item["payload"] = _json_load(item.pop("payload_json", "{}"), {})
    item["precondition"] = _json_load(item.pop("precondition_json", "{}"), {})
    item["evidence"] = _json_load(item.pop("evidence_json", "{}"), {})
    item["ai_review"] = _json_load(item.pop("ai_review_json", "{}"), {})
    return item


def create_event(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    event_type: str,
    event_kind: str = "user_feedback",
    target_type: str = "",
    target_id: str = "",
    status: str = EVENT_STATUS_PROPOSED,
    risk_level: str = "low",
    confidence: float = 0.0,
    baseline_commit: str = "",
    target_commit: str = "",
    branch_ref: str = "",
    operation_type: str = "",
    source_branch_ref: str = "",
    source_snapshot_id: str = "",
    source_event_id: str = "",
    payload_hash: str = "",
    stable_node_key: str = "",
    feature_hash: str = "",
    file_hashes: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    precondition: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    created_by: str = "",
    event_id: str | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    if event_type not in ALLOWED_EVENT_TYPES:
        raise ValueError(f"unsupported graph event type: {event_type}")
    if status not in ALLOWED_EVENT_STATUSES:
        raise ValueError(f"invalid graph event status: {status}")
    snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id) or {}
    commit = str(snapshot.get("commit_sha") or "")
    eid = event_id or f"ge-{uuid.uuid4().hex[:12]}"
    now = store.utc_now()
    payload_value = payload or {}
    evidence_value = evidence or {}
    branch_value = str(branch_ref or "").strip() or _snapshot_branch_ref(conn, project_id, snapshot_id)
    operation_value = _semantic_operation_type(
        event_type,
        status,
        payload_value,
        evidence_value,
        operation_type,
    )
    conn.execute(
        """
        INSERT INTO graph_events
          (project_id, snapshot_id, event_id, event_seq, event_kind, event_type,
           target_type, target_id, status, risk_level, confidence, baseline_commit,
           target_commit, branch_ref, operation_type, source_branch_ref,
           source_snapshot_id, source_event_id, payload_hash,
           stable_node_key, feature_hash, file_hashes_json,
           payload_json, precondition_json, evidence_json,
           created_by, created_at, updated_by, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id, snapshot_id, event_id) DO UPDATE SET
          event_kind = excluded.event_kind,
          event_type = excluded.event_type,
          target_type = excluded.target_type,
          target_id = excluded.target_id,
          status = excluded.status,
          risk_level = excluded.risk_level,
          confidence = excluded.confidence,
          baseline_commit = excluded.baseline_commit,
          target_commit = excluded.target_commit,
          branch_ref = excluded.branch_ref,
          operation_type = excluded.operation_type,
          source_branch_ref = excluded.source_branch_ref,
          source_snapshot_id = excluded.source_snapshot_id,
          source_event_id = excluded.source_event_id,
          payload_hash = excluded.payload_hash,
          stable_node_key = excluded.stable_node_key,
          feature_hash = excluded.feature_hash,
          file_hashes_json = excluded.file_hashes_json,
          payload_json = excluded.payload_json,
          precondition_json = excluded.precondition_json,
          evidence_json = excluded.evidence_json,
          updated_by = excluded.updated_by,
          updated_at = excluded.updated_at
        """,
        (
            project_id,
            snapshot_id,
            eid,
            _next_event_seq(conn, project_id, snapshot_id),
            event_kind or "user_feedback",
            event_type,
            target_type,
            target_id,
            status,
            risk_level or "low",
            float(confidence or 0.0),
            baseline_commit or commit,
            target_commit or commit,
            branch_value,
            operation_value,
            str(source_branch_ref or ""),
            str(source_snapshot_id or ""),
            str(source_event_id or ""),
            _payload_hash(payload_value, payload_hash),
            stable_node_key,
            feature_hash,
            _json(file_hashes or {}),
            _json(payload_value),
            _json(precondition or {}),
            _json(evidence_value),
            created_by,
            now,
            created_by,
            now,
        ),
    )
    return get_event(conn, project_id, snapshot_id, eid) or {}


def get_event(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    event_id: str,
) -> dict[str, Any] | None:
    ensure_schema(conn)
    row = conn.execute(
        """
        SELECT * FROM graph_events
        WHERE project_id = ? AND snapshot_id = ? AND event_id = ?
        """,
        (project_id, snapshot_id, event_id),
    ).fetchone()
    return _row_to_event(row) if row else None


def list_events(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    statuses: Iterable[str] | None = None,
    event_types: Iterable[str] | None = None,
    target_type: str = "",
    target_id: str = "",
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    params: list[Any] = [project_id, snapshot_id]
    sql = "SELECT * FROM graph_events WHERE project_id = ? AND snapshot_id = ?"
    status_values = [str(item) for item in statuses or [] if str(item or "")]
    if status_values:
        sql += " AND status IN (" + ",".join("?" for _ in status_values) + ")"
        params.extend(status_values)
    type_values = [str(item) for item in event_types or [] if str(item or "")]
    if type_values:
        sql += " AND event_type IN (" + ",".join("?" for _ in type_values) + ")"
        params.extend(type_values)
    if target_type:
        sql += " AND target_type = ?"
        params.append(target_type)
    if target_id:
        sql += " AND target_id = ?"
        params.append(target_id)
    sql += " ORDER BY event_seq ASC LIMIT ? OFFSET ?"
    params.extend([max(1, min(int(limit or 200), 1000)), max(0, int(offset or 0))])
    return [_row_to_event(row) for row in conn.execute(sql, params).fetchall()]


def status_counts(conn: sqlite3.Connection, project_id: str, snapshot_id: str) -> dict[str, int]:
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM graph_events
        WHERE project_id = ? AND snapshot_id = ?
        GROUP BY status
        ORDER BY status
        """,
        (project_id, snapshot_id),
    ).fetchall()
    return {str(row["status"] or ""): int(row["count"] or 0) for row in rows}


def update_event_status(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    event_id: str,
    *,
    status: str,
    actor: str = "",
    evidence: dict[str, Any] | None = None,
    operation_type: str = "",
) -> dict[str, Any]:
    ensure_schema(conn)
    if status not in ALLOWED_EVENT_STATUSES:
        raise ValueError(f"invalid graph event status: {status}")
    event = get_event(conn, project_id, snapshot_id, event_id)
    if not event:
        raise KeyError(f"graph event not found: {event_id}")
    merged_evidence = dict(event.get("evidence") or {})
    if evidence:
        merged_evidence.setdefault("status_events", [])
        if isinstance(merged_evidence["status_events"], list):
            merged_evidence["status_events"].append({
                "status": status,
                "actor": actor,
                "at": store.utc_now(),
                "evidence": evidence,
            })
        merged_evidence.update({k: v for k, v in evidence.items() if k != "status_events"})
    now = store.utc_now()
    op_value = str(operation_type or "").strip()
    conn.execute(
        """
        UPDATE graph_events
        SET status = ?,
            operation_type = CASE WHEN ? = '' THEN operation_type ELSE ? END,
            evidence_json = ?, updated_by = ?, updated_at = ?
        WHERE project_id = ? AND snapshot_id = ? AND event_id = ?
        """,
        (
            status,
            op_value,
            op_value,
            _json(merged_evidence),
            actor,
            now,
            project_id,
            snapshot_id,
            event_id,
        ),
    )
    return get_event(conn, project_id, snapshot_id, event_id) or {}


def refine_event(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    event_id: str,
    *,
    actor: str = "",
    payload: dict[str, Any] | None = None,
    ai_review: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_schema(conn)
    event = get_event(conn, project_id, snapshot_id, event_id)
    if not event:
        raise KeyError(f"graph event not found: {event_id}")
    merged_payload = dict(event.get("payload") or {})
    if payload:
        merged_payload.update(payload)
    merged_review = dict(event.get("ai_review") or {})
    if ai_review:
        merged_review.update(ai_review)
    merged_evidence = dict(event.get("evidence") or {})
    if evidence:
        merged_evidence.update(evidence)
    now = store.utc_now()
    conn.execute(
        """
        UPDATE graph_events
        SET payload_json = ?, ai_review_json = ?, evidence_json = ?,
            status = CASE WHEN status = ? THEN ? ELSE status END,
            updated_by = ?, updated_at = ?
        WHERE project_id = ? AND snapshot_id = ? AND event_id = ?
        """,
        (
            _json(merged_payload),
            _json(merged_review),
            _json(merged_evidence),
            EVENT_STATUS_AI_REVIEWING,
            EVENT_STATUS_PROPOSED,
            actor,
            now,
            project_id,
            snapshot_id,
            event_id,
        ),
    )
    return get_event(conn, project_id, snapshot_id, event_id) or {}


def mark_backlog_filed(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    event_id: str,
    *,
    bug_id: str,
    actor: str = "",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = update_event_status(
        conn,
        project_id,
        snapshot_id,
        event_id,
        status=EVENT_STATUS_BACKLOG_FILED,
        actor=actor,
        evidence=evidence or {},
    )
    conn.execute(
        """
        UPDATE graph_events
        SET backlog_bug_id = ?, updated_by = ?, updated_at = ?
        WHERE project_id = ? AND snapshot_id = ? AND event_id = ?
        """,
        (bug_id, actor, store.utc_now(), project_id, snapshot_id, event_id),
    )
    return get_event(conn, project_id, snapshot_id, event_id) or event


def _load_graph_json(project_id: str, snapshot_id: str) -> dict[str, Any]:
    path = store.snapshot_graph_path(project_id, snapshot_id)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _edge_from_payload(payload: dict[str, Any], target_id: str = "") -> dict[str, Any]:
    edge = payload.get("edge") if isinstance(payload.get("edge"), dict) else {}
    item = dict(edge or payload)
    if target_id and "src" not in item and "source" not in item and "dst" not in item and "target" not in item:
        parts = target_id.split("->", 1)
        if len(parts) == 2:
            item["src"] = parts[0]
            item["dst"] = parts[1].split(":", 1)[0]
    return item


def _event_file_key(event_type: str) -> str:
    if event_type.startswith("doc_"):
        return "secondary"
    if event_type.startswith("test_"):
        return "test"
    if event_type.startswith("config_"):
        return "config"
    raise ValueError(f"unsupported binding event type: {event_type}")


def _apply_event(graph_json: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    nodes, edges = _ensure_deps_graph(graph_json)
    nodes_by_id = _node_map(nodes)
    event_type = str(event.get("event_type") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    target_id = str(event.get("target_id") or payload.get("target_node_id") or payload.get("node_id") or "")
    impacted: set[str] = set()

    if event_type == "node_rename_proposed":
        node = nodes_by_id.get(target_id)
        if not node:
            raise ValueError("target node missing")
        new_title = str(payload.get("new_title") or payload.get("title") or payload.get("feature_name") or "").strip()
        if not new_title:
            raise ValueError("node_rename_proposed requires new_title")
        node["title"] = new_title
        _metadata(node)["semantic_title_override"] = True
        impacted.add(target_id)
    elif event_type == "node_reparented":
        node = nodes_by_id.get(target_id)
        new_parent_id = str(payload.get("new_parent_id") or payload.get("parent_id") or "").strip()
        if not node or new_parent_id not in nodes_by_id:
            raise ValueError("node_reparented requires existing target and parent")
        edges[:] = [
            edge for edge in edges
            if not (
                str(edge.get("dst") or edge.get("target") or "") == target_id
                and str(edge.get("edge_type") or edge.get("type") or "") == "contains"
            )
        ]
        _append_edge(edges, {
            "source": new_parent_id,
            "target": target_id,
            "edge_type": "contains",
            "direction": "hierarchy",
            "evidence": {"source": "graph_event", "event_id": event.get("event_id", "")},
        })
        _metadata(node)["parent_id"] = new_parent_id
        _metadata(node)["semantic_parent_override"] = True
        impacted.update({target_id, new_parent_id})
    elif event_type == "edge_added":
        edge = _edge_from_payload(payload, str(event.get("target_id") or ""))
        edge.setdefault("evidence", {"source": "graph_event", "event_id": event.get("event_id", "")})
        _append_edge(edges, edge)
        impacted.update({str(edge.get("src") or edge.get("source") or ""), str(edge.get("dst") or edge.get("target") or "")})
    elif event_type == "edge_removed":
        edge = _edge_from_payload(payload, str(event.get("target_id") or ""))
        _remove_edge(edges, edge)
        impacted.update({str(edge.get("src") or edge.get("source") or ""), str(edge.get("dst") or edge.get("target") or "")})
    elif event_type == "edge_reclassified":
        old_edge = _edge_from_payload(payload.get("old_edge") if isinstance(payload.get("old_edge"), dict) else payload, str(event.get("target_id") or ""))
        removed = _remove_edge(edges, old_edge)
        new_edge = dict(old_edge)
        new_edge["edge_type"] = str(payload.get("new_edge_type") or payload.get("edge_type") or "depends_on")
        new_edge["direction"] = str(payload.get("new_direction") or payload.get("direction") or "dependency")
        new_edge["evidence"] = {"source": "graph_event", "event_id": event.get("event_id", ""), "removed": removed}
        _append_edge(edges, new_edge)
        impacted.update({str(new_edge.get("src") or new_edge.get("source") or ""), str(new_edge.get("dst") or new_edge.get("target") or "")})
    elif event_type.endswith("_binding_added") or event_type.endswith("_binding_removed"):
        node = nodes_by_id.get(target_id)
        if not node:
            raise ValueError("binding event target node missing")
        key = _event_file_key(event_type)
        files = _string_list(payload.get("files") or payload.get("paths") or payload.get("path"))
        if not files:
            raise ValueError("binding event requires files")
        current = _node_files(node, key)
        if event_type.endswith("_added"):
            for path in files:
                _add_unique(current, path)
        else:
            current = [path for path in current if path not in set(files)]
        _set_node_files(node, key, current)
        impacted.add(target_id)
    elif event_type == "package_marker_excluded":
        node = nodes_by_id.get(target_id)
        if not node:
            raise ValueError("target node missing")
        metadata = _metadata(node)
        metadata["file_role"] = "package_marker"
        metadata["exclude_as_feature"] = True
        flags = _string_list(metadata.get("quality_flags"))
        _add_unique(flags, "coverage_noise_candidate")
        metadata["quality_flags"] = flags
        impacted.add(target_id)
    elif event_type in {"feature_marked_dead_code", "feature_marked_duplicate"}:
        node = nodes_by_id.get(target_id)
        if not node:
            raise ValueError("target node missing")
        metadata = _metadata(node)
        flag = "dead_code_candidate" if event_type == "feature_marked_dead_code" else "duplicate_feature_candidate"
        flags = _string_list(metadata.get("quality_flags"))
        _add_unique(flags, flag)
        metadata["quality_flags"] = flags
        metadata[flag] = True
        if payload.get("duplicate_of"):
            metadata["duplicate_of"] = str(payload.get("duplicate_of"))
            impacted.add(str(payload.get("duplicate_of")))
        impacted.add(target_id)
    elif event_type == "semantic_stale":
        if target_id:
            impacted.add(target_id)
    else:
        raise ValueError(f"event type is not materializable: {event_type}")

    graph_json.setdefault("metadata", {})
    if isinstance(graph_json["metadata"], dict):
        applied = graph_json["metadata"].setdefault("graph_events_applied", [])
        if isinstance(applied, list):
            applied.append({
                "event_id": event.get("event_id", ""),
                "event_type": event_type,
                "target_id": target_id,
                "applied_at": store.utc_now(),
            })
    return {"event_id": event.get("event_id", ""), "event_type": event_type, "impacted_node_ids": sorted(node for node in impacted if node)}


def _edge_exists(edges: list[dict[str, Any]], edge: dict[str, Any]) -> bool:
    src, dst, edge_type, _direction = _edge_key(edge)
    if not src or not dst:
        return False
    for existing in edges:
        if (
            str(existing.get("src") or existing.get("source") or "") == src
            and str(existing.get("dst") or existing.get("target") or "") == dst
            and str(existing.get("edge_type") or existing.get("type") or "depends_on") == edge_type
        ):
            return True
    return False


def _precondition_error(
    graph_json: dict[str, Any],
    event: dict[str, Any],
    *,
    snapshot_id: str,
) -> str:
    precondition = event.get("precondition") if isinstance(event.get("precondition"), dict) else {}
    if not precondition:
        return ""
    expected_snapshot = str(precondition.get("expected_snapshot_id") or "").strip()
    if expected_snapshot and expected_snapshot != snapshot_id:
        return f"expected_snapshot_id mismatch: expected {expected_snapshot}, got {snapshot_id}"

    nodes, edges = _ensure_deps_graph(graph_json)
    nodes_by_id = _node_map(nodes)
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    target_id = str(event.get("target_id") or payload.get("target_node_id") or payload.get("node_id") or "")

    if precondition.get("node_exists") is not False and str(event.get("target_type") or "") == "node":
        if target_id and target_id not in nodes_by_id:
            return f"target node missing: {target_id}"
    expected_title = str(precondition.get("expected_node_title") or precondition.get("expected_title") or "").strip()
    if expected_title and target_id:
        node = nodes_by_id.get(target_id)
        if not node:
            return f"target node missing: {target_id}"
        if str(node.get("title") or "") != expected_title:
            return f"expected_node_title mismatch for {target_id}"

    edge_present = precondition.get("expected_edge_present")
    if isinstance(edge_present, dict) and not _edge_exists(edges, edge_present):
        return "expected_edge_present missing"
    edge_absent = precondition.get("expected_edge_absent")
    if isinstance(edge_absent, dict) and _edge_exists(edges, edge_absent):
        return "expected_edge_absent already present"
    return ""


def _mark_semantic_stale(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    node_ids: Iterable[str],
) -> int:
    ids = [str(node_id or "").strip() for node_id in node_ids if str(node_id or "").strip()]
    if not ids:
        return 0
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='graph_semantic_nodes'"
    ).fetchone()
    if not exists:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"""
        UPDATE graph_semantic_nodes
        SET status = ?, updated_at = ?
        WHERE project_id = ? AND snapshot_id = ? AND node_id IN ({placeholders})
        """,
        ("semantic_stale", store.utc_now(), project_id, snapshot_id, *ids),
    )
    return int(cur.rowcount or 0)


def _select_events_for_apply_or_preview(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    event_ids: Iterable[str] | None = None,
    statuses: Iterable[str] | None = None,
    default_status: str | None = EVENT_STATUS_ACCEPTED,
) -> list[dict[str, Any]]:
    requested_ids = [str(item or "").strip() for item in event_ids or [] if str(item or "").strip()]
    status_values = [str(item or "").strip() for item in statuses or [] if str(item or "").strip()]
    if not status_values and default_status:
        status_values = [default_status]
    params: list[Any] = [project_id, snapshot_id]
    sql = "SELECT * FROM graph_events WHERE project_id = ? AND snapshot_id = ?"
    if requested_ids:
        sql += " AND event_id IN (" + ",".join("?" for _ in requested_ids) + ")"
        params.extend(requested_ids)
    if status_values:
        sql += " AND status IN (" + ",".join("?" for _ in status_values) + ")"
        params.extend(status_values)
    sql += " ORDER BY event_seq ASC"
    return [_row_to_event(row) for row in conn.execute(sql, params).fetchall()]


def _node_preview_summary(node: dict[str, Any]) -> dict[str, Any]:
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    return {
        "node_id": _node_id(node),
        "title": node.get("title", ""),
        "layer": node.get("layer", ""),
        "kind": node.get("kind", ""),
        "primary_files": _node_files(node, "primary"),
        "secondary_files": _node_files(node, "secondary"),
        "test_files": _node_files(node, "test"),
        "config_files": _node_files(node, "config"),
        "hierarchy_parent": metadata.get("hierarchy_parent") or metadata.get("parent_id") or "",
        "file_role": metadata.get("file_role", ""),
        "exclude_as_feature": bool(metadata.get("exclude_as_feature")),
        "quality_flags": _string_list(metadata.get("quality_flags")),
    }


def _node_preview_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    fields = [
        "title",
        "layer",
        "kind",
        "primary_files",
        "secondary_files",
        "test_files",
        "config_files",
        "hierarchy_parent",
        "file_role",
        "exclude_as_feature",
        "quality_flags",
    ]
    changed = [
        field for field in fields
        if before.get(field) != after.get(field)
    ]
    return {
        "node_id": after.get("node_id") or before.get("node_id") or "",
        "changed_fields": changed,
        "before": {field: before.get(field) for field in changed},
        "after": {field: after.get(field) for field in changed},
    }


def _edge_preview_summary(edge: dict[str, Any]) -> dict[str, Any]:
    src, dst, edge_type, direction = _edge_key(edge)
    return {
        "edge_id": f"{src}->{dst}:{edge_type}" if src and dst else "",
        "src": src,
        "dst": dst,
        "edge_type": edge_type,
        "direction": direction,
    }


def _graph_preview_diff(before_graph: dict[str, Any], after_graph: dict[str, Any]) -> dict[str, Any]:
    before_nodes = {_node_id(node): _node_preview_summary(node) for node in _graph_nodes(before_graph)}
    after_nodes = {_node_id(node): _node_preview_summary(node) for node in _graph_nodes(after_graph)}
    before_edges = {_edge_key(edge): _edge_preview_summary(edge) for edge in store.graph_payload_edges(before_graph)}
    after_edges = {_edge_key(edge): _edge_preview_summary(edge) for edge in store.graph_payload_edges(after_graph)}
    added_node_ids = sorted(set(after_nodes) - set(before_nodes))
    removed_node_ids = sorted(set(before_nodes) - set(after_nodes))
    changed_nodes = [
        diff for node_id in sorted(set(before_nodes).intersection(after_nodes))
        if (diff := _node_preview_diff(before_nodes[node_id], after_nodes[node_id]))["changed_fields"]
    ]
    added_edges = [after_edges[key] for key in sorted(set(after_edges) - set(before_edges))]
    removed_edges = [before_edges[key] for key in sorted(set(before_edges) - set(after_edges))]
    return {
        "nodes": {
            "added_count": len(added_node_ids),
            "removed_count": len(removed_node_ids),
            "changed_count": len(changed_nodes),
            "added": [after_nodes[node_id] for node_id in added_node_ids[:100]],
            "removed": [before_nodes[node_id] for node_id in removed_node_ids[:100]],
            "changed": changed_nodes[:200],
        },
        "edges": {
            "added_count": len(added_edges),
            "removed_count": len(removed_edges),
            "added": added_edges[:200],
            "removed": removed_edges[:200],
        },
    }


def preview_events(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    event_ids: Iterable[str] | None = None,
    statuses: Iterable[str] | None = None,
    actor: str = "observer",
    include_graph: bool = False,
) -> dict[str, Any]:
    """Preview graph event materialization without changing DB state."""
    ensure_schema(conn)
    snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    requested_ids = [str(item or "").strip() for item in event_ids or [] if str(item or "").strip()]
    events = _select_events_for_apply_or_preview(
        conn,
        project_id,
        snapshot_id,
        event_ids=requested_ids or None,
        statuses=statuses,
        default_status=None if requested_ids else EVENT_STATUS_ACCEPTED,
    )
    materializable = [event for event in events if str(event.get("event_type") or "") in GRAPH_MUTATION_EVENT_TYPES]
    original_graph = _load_graph_json(project_id, snapshot_id)
    preview_graph = copy.deepcopy(original_graph)
    applied: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    impacted: set[str] = set()
    for event in events:
        if str(event.get("event_type") or "") not in GRAPH_MUTATION_EVENT_TYPES:
            skipped.append({
                "event_id": event.get("event_id", ""),
                "event_type": event.get("event_type", ""),
                "reason": "not_graph_mutation_event",
            })
            continue
        try:
            precondition = _precondition_error(preview_graph, event, snapshot_id=snapshot_id)
            if precondition:
                errors.append({
                    "event_id": event.get("event_id", ""),
                    "event_type": event.get("event_type", ""),
                    "error": precondition,
                    "would_status": EVENT_STATUS_STALE,
                })
                continue
            result = _apply_event(preview_graph, event)
        except Exception as exc:  # noqa: BLE001 - preview reports event-specific errors
            errors.append({
                "event_id": event.get("event_id", ""),
                "event_type": event.get("event_type", ""),
                "error": str(exc),
                "would_status": EVENT_STATUS_FAILED,
            })
            continue
        applied.append(result)
        impacted.update(result.get("impacted_node_ids") or [])
    diff = _graph_preview_diff(original_graph, preview_graph)
    result = {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "commit_sha": snapshot.get("commit_sha", ""),
        "actor": actor,
        "requested_event_ids": requested_ids,
        "selected_event_count": len(events),
        "materializable_event_count": len(materializable),
        "would_materialize_count": len(applied),
        "would_error_count": len(errors),
        "would_skip_count": len(skipped),
        "would_create_snapshot": bool(applied),
        "would_mark_semantic_stale_count": len(impacted),
        "impacted_node_ids": sorted(impacted),
        "applied": applied,
        "errors": errors,
        "skipped": skipped,
        "diff": diff,
    }
    if include_graph:
        result["preview_graph"] = preview_graph
    return result


def materialize_events(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    event_ids: Iterable[str] | None = None,
    actor: str = "observer",
    new_snapshot_id: str | None = None,
    activate: bool = False,
) -> dict[str, Any]:
    ensure_schema(conn)
    snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    requested_ids = [str(item or "").strip() for item in event_ids or [] if str(item or "").strip()]
    events = _select_events_for_apply_or_preview(
        conn,
        project_id,
        snapshot_id,
        event_ids=requested_ids or None,
        statuses=[EVENT_STATUS_ACCEPTED],
        default_status=EVENT_STATUS_ACCEPTED,
    )
    graph_events = [event for event in events if str(event.get("event_type") or "") in GRAPH_MUTATION_EVENT_TYPES]
    if not graph_events:
        return {
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "materialized_count": 0,
            "new_snapshot_id": "",
            "applied": [],
            "errors": [],
            "semantic_stale_count": 0,
        }

    graph_json = copy.deepcopy(_load_graph_json(project_id, snapshot_id))
    applied: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    impacted: set[str] = set()
    for event in graph_events:
        try:
            precondition = _precondition_error(graph_json, event, snapshot_id=snapshot_id)
            if precondition:
                update_event_status(
                    conn,
                    project_id,
                    snapshot_id,
                    str(event.get("event_id") or ""),
                    status=EVENT_STATUS_STALE,
                    actor=actor,
                    evidence={"precondition_error": precondition},
                )
                errors.append({"event_id": event.get("event_id", ""), "error": precondition, "status": "stale"})
                continue
            result = _apply_event(graph_json, event)
        except Exception as exc:  # noqa: BLE001 - event is recorded for operator review
            errors.append({"event_id": event.get("event_id", ""), "error": str(exc)})
            continue
        applied.append(result)
        impacted.update(result.get("impacted_node_ids") or [])

    if not applied:
        for error in errors:
            if error.get("status") == EVENT_STATUS_STALE:
                continue
            if error.get("event_id"):
                update_event_status(
                    conn,
                    project_id,
                    snapshot_id,
                    str(error["event_id"]),
                    status=EVENT_STATUS_FAILED,
                    actor=actor,
                    evidence={"materialize_error": error.get("error", "")},
                )
        return {
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "materialized_count": 0,
            "new_snapshot_id": "",
            "applied": [],
            "errors": errors,
            "semantic_stale_count": 0,
        }

    commit_sha = str(snapshot.get("commit_sha") or "")
    sid = new_snapshot_id or store.snapshot_id_for("event", commit_sha, uuid.uuid4().hex[:6])
    created = store.create_graph_snapshot(
        conn,
        project_id,
        snapshot_id=sid,
        commit_sha=commit_sha,
        snapshot_kind="event",
        parent_snapshot_id=snapshot_id,
        graph_json=graph_json,
        status=store.SNAPSHOT_STATUS_CANDIDATE,
        created_by=actor,
        notes=f"Materialized {len(applied)} graph event(s) from {snapshot_id}",
    )
    store.index_graph_snapshot(
        conn,
        project_id,
        sid,
        nodes=_graph_nodes(graph_json),
        edges=store.graph_payload_edges(graph_json),
    )
    if activate:
        store.activate_graph_snapshot(conn, project_id, sid, expected_old_snapshot_id=snapshot_id)
    stale_count = _mark_semantic_stale(conn, project_id, snapshot_id, impacted)
    now = store.utc_now()
    for result in applied:
        conn.execute(
            """
            UPDATE graph_events
            SET status = ?, materialized_snapshot_id = ?, updated_by = ?, updated_at = ?
            WHERE project_id = ? AND snapshot_id = ? AND event_id = ?
            """,
            (
                EVENT_STATUS_MATERIALIZED,
                sid,
                actor,
                now,
                project_id,
                snapshot_id,
                result.get("event_id", ""),
            ),
        )
    for error in errors:
        if error.get("status") == EVENT_STATUS_STALE:
            continue
        if error.get("event_id"):
            update_event_status(
                conn,
                project_id,
                snapshot_id,
                str(error["event_id"]),
                status=EVENT_STATUS_FAILED,
                actor=actor,
                evidence={"materialize_error": error.get("error", "")},
            )
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "new_snapshot_id": sid,
        "snapshot": created,
        "activated": bool(activate),
        "materialized_count": len(applied),
        "error_count": len(errors),
        "applied": applied,
        "errors": errors,
        "impacted_node_ids": sorted(impacted),
        "semantic_stale_count": stale_count,
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _safe_event_id(*parts: str) -> str:
    import re

    raw = "-".join(str(part or "") for part in parts if str(part or ""))
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._")
    if len(safe) > 120:
        safe = f"{safe[:88]}-{_hash_payload(raw)[:16]}"
    return safe or f"ge-{uuid.uuid4().hex[:12]}"


def _nodes_by_id_for_snapshot(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> dict[str, dict[str, Any]]:
    nodes = store.list_graph_snapshot_nodes(
        conn,
        project_id,
        snapshot_id,
        include_semantic=False,
        limit=1000,
    )
    return {str(node.get("node_id") or ""): node for node in nodes if str(node.get("node_id") or "")}


def _event_payload_semantic(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    semantic_payload = payload.get("semantic_payload")
    if isinstance(semantic_payload, dict):
        return semantic_payload
    return payload


def _event_payload_edge_semantic(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    semantic_payload = payload.get("semantic_payload")
    if isinstance(semantic_payload, dict):
        return semantic_payload
    nested = payload.get("semantic")
    if isinstance(nested, dict):
        return nested
    if event.get("event_type") == "edge_semantic_requested":
        request = payload.get("operator_request")
        return {
            "status": "semantic_requested",
            "operator_request": request if isinstance(request, dict) else {},
        }
    return {}


def backfill_existing_semantic_events(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    actor: str = "semantic_event_backfill",
) -> dict[str, Any]:
    """Import existing semantic cache rows into the graph_events timeline."""
    ensure_schema(conn)
    snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    nodes_by_id = _nodes_by_id_for_snapshot(conn, project_id, snapshot_id)
    commit_sha = str(snapshot.get("commit_sha") or "")
    created = 0
    updated = 0
    skipped = 0
    if _table_exists(conn, "graph_semantic_nodes"):
        node_columns = _table_columns(conn, "graph_semantic_nodes")
        node_optional = [
            name for name in (
                "branch_ref",
                "operation_type",
                "source_branch_ref",
                "source_snapshot_id",
                "source_event_id",
                "payload_hash",
            )
            if name in node_columns
        ]
        node_optional_sql = (", " + ", ".join(node_optional)) if node_optional else ""
        rows = conn.execute(
            f"""
            SELECT node_id, status, feature_hash, file_hashes_json, semantic_json,
                   feedback_round, batch_index, updated_at{node_optional_sql}
            FROM graph_semantic_nodes
            WHERE project_id = ? AND snapshot_id = ?
            """,
            (project_id, snapshot_id),
        ).fetchall()
        for row in rows:
            node_id = str(row["node_id"] or "")
            if not node_id:
                skipped += 1
                continue
            node = nodes_by_id.get(node_id, {"node_id": node_id})
            semantic_payload = _json_load(row["semantic_json"], {})
            status = str(row["status"] or "")
            # MF-2026-05-10-016: `pending_review` is the worker's
            # "submit for review" outcome. Backfill that as a PROPOSED event
            # so the projection (which filters for observed/materialized/accepted)
            # stays blind until an operator accepts via accept_semantic_enrichment.
            if status not in {"ai_complete", "semantic_graph_state", "reviewed", "pending_review"} and not semantic_payload:
                skipped += 1
                continue
            event_status = (
                EVENT_STATUS_PROPOSED
                if status == "pending_review"
                else EVENT_STATUS_OBSERVED
            )
            event_id = _safe_event_id(
                "semnode",
                snapshot_id,
                node_id,
                str(row["feature_hash"] or "")[:12],
            )
            existed = get_event(conn, project_id, snapshot_id, event_id)
            source_snapshot_id = str(_row_get(row, "source_snapshot_id") or "")
            if not source_snapshot_id and isinstance(semantic_payload, dict):
                source_snapshot_id = str(semantic_payload.get("carried_forward_from_snapshot_id") or "")
            create_event(
                conn,
                project_id,
                snapshot_id,
                event_id=event_id,
                event_type="semantic_node_enriched",
                event_kind="imported_semantic_cache",
                target_type="node",
                target_id=node_id,
                status=event_status,
                branch_ref=str(_row_get(row, "branch_ref") or ""),
                operation_type=str(_row_get(row, "operation_type") or ""),
                source_branch_ref=str(_row_get(row, "source_branch_ref") or ""),
                source_snapshot_id=source_snapshot_id,
                source_event_id=str(_row_get(row, "source_event_id") or ""),
                payload_hash=str(_row_get(row, "payload_hash") or ""),
                stable_node_key=stable_node_key_for_node(node),
                feature_hash=str(row["feature_hash"] or ""),
                file_hashes=_json_load(row["file_hashes_json"], {}),
                baseline_commit=commit_sha,
                target_commit=commit_sha,
                payload={
                    "semantic_payload": semantic_payload,
                    "semantic_status": status,
                    "feedback_round": row["feedback_round"],
                    "batch_index": row["batch_index"],
                    "source_updated_at": row["updated_at"],
                },
                evidence={"source": "graph_semantic_nodes_backfill"},
                created_by=actor,
            )
            if existed:
                updated += 1
            else:
                created += 1
    job_created = 0
    if _table_exists(conn, "graph_semantic_jobs"):
        rows = conn.execute(
            """
            SELECT node_id, status, feature_hash, file_hashes_json,
                   feedback_round, batch_index, attempt_count, updated_at
            FROM graph_semantic_jobs
            WHERE project_id = ? AND snapshot_id = ?
            """,
            (project_id, snapshot_id),
        ).fetchall()
        for row in rows:
            node_id = str(row["node_id"] or "")
            if not node_id:
                continue
            event_id = _safe_event_id("semjob", snapshot_id, node_id, str(row["status"] or ""))
            existed = get_event(conn, project_id, snapshot_id, event_id)
            if existed:
                continue
            node = nodes_by_id.get(node_id, {"node_id": node_id})
            create_event(
                conn,
                project_id,
                snapshot_id,
                event_id=event_id,
                event_type="semantic_job_requested",
                event_kind="imported_semantic_cache",
                target_type="node",
                target_id=node_id,
                status=EVENT_STATUS_OBSERVED,
                stable_node_key=stable_node_key_for_node(node),
                feature_hash=str(row["feature_hash"] or ""),
                file_hashes=_json_load(row["file_hashes_json"], {}),
                baseline_commit=commit_sha,
                target_commit=commit_sha,
                payload={
                    "job_status": row["status"],
                    "feedback_round": row["feedback_round"],
                    "batch_index": row["batch_index"],
                    "attempt_count": row["attempt_count"],
                    "source_updated_at": row["updated_at"],
                },
                evidence={"source": "graph_semantic_jobs_backfill"},
                created_by=actor,
            )
            job_created += 1
    # MF 2026-05-11: backfill edge_semantic_enriched events from the new
    # graph_semantic_edges table, mirroring the node loop above. Lets a
    # carry-forward'd edge_semantics row produce a PROPOSED/OBSERVED event
    # in the new snapshot's graph_events without re-running AI.
    edge_event_created = 0
    edge_event_updated = 0
    edge_event_skipped = 0
    edges_by_id: dict[str, dict[str, Any]] = {}
    for edge in store.list_graph_snapshot_edges(conn, project_id, snapshot_id, limit=5000):
        eid = _edge_id(edge)
        if eid:
            edges_by_id[eid] = edge
    if _table_exists(conn, "graph_semantic_edges"):
        edge_columns = _table_columns(conn, "graph_semantic_edges")
        edge_optional = [
            name for name in (
                "branch_ref",
                "operation_type",
                "source_branch_ref",
                "source_snapshot_id",
                "source_event_id",
                "payload_hash",
            )
            if name in edge_columns
        ]
        edge_optional_sql = (", " + ", ".join(edge_optional)) if edge_optional else ""
        rows = conn.execute(
            f"""
            SELECT edge_id, status, edge_signature_hash, semantic_json,
                   feedback_round, batch_index, updated_at{edge_optional_sql}
            FROM graph_semantic_edges
            WHERE project_id = ? AND snapshot_id = ?
            """,
            (project_id, snapshot_id),
        ).fetchall()
        for row in rows:
            edge_id = str(row["edge_id"] or "")
            if not edge_id:
                edge_event_skipped += 1
                continue
            semantic_entry = _json_load(row["semantic_json"], {})
            semantic_payload = semantic_entry.get("semantic_payload") if isinstance(semantic_entry, dict) else None
            if not isinstance(semantic_payload, dict):
                # graph_semantic_edges stores the FULL entry (state-shaped);
                # the AI semantic payload is under semantic_entry itself if
                # the worker wrote it flat. Accept either shape.
                semantic_payload = semantic_entry if isinstance(semantic_entry, dict) else {}
            status = str(row["status"] or "")
            if status not in {"ai_complete", "semantic_graph_state", "reviewed", "pending_review"} and not semantic_payload:
                edge_event_skipped += 1
                continue
            event_status = (
                EVENT_STATUS_PROPOSED
                if status == "pending_review"
                else EVENT_STATUS_OBSERVED
            )
            event_id = _safe_event_id(
                "semedge",
                snapshot_id,
                edge_id,
                str(row["edge_signature_hash"] or "")[:12],
            )
            existed = get_event(conn, project_id, snapshot_id, event_id)
            edge_struct = edges_by_id.get(edge_id, {})
            source_snapshot_id = str(_row_get(row, "source_snapshot_id") or "")
            if not source_snapshot_id and isinstance(semantic_entry, dict):
                source_snapshot_id = str(semantic_entry.get("carried_forward_from_snapshot_id") or "")
            create_event(
                conn,
                project_id,
                snapshot_id,
                event_id=event_id,
                event_type="edge_semantic_enriched",
                event_kind="imported_semantic_cache",
                target_type="edge",
                target_id=edge_id,
                status=event_status,
                branch_ref=str(_row_get(row, "branch_ref") or ""),
                operation_type=str(_row_get(row, "operation_type") or ""),
                source_branch_ref=str(_row_get(row, "source_branch_ref") or ""),
                source_snapshot_id=source_snapshot_id,
                source_event_id=str(_row_get(row, "source_event_id") or ""),
                payload_hash=str(_row_get(row, "payload_hash") or ""),
                # MF 2026-05-11: stash stable_edge_key into the existing
                # stable_node_key column so _latest_edge_semantic_events
                # can find this event cross-snapshot. Column name is
                # legacy node-only; semantic content here is the edge's
                # stable key.
                stable_node_key=str(semantic_entry.get("stable_edge_key") or "") if isinstance(semantic_entry, dict) else "",
                feature_hash=str(row["edge_signature_hash"] or ""),
                file_hashes={},
                baseline_commit=commit_sha,
                target_commit=commit_sha,
                payload={
                    "edge": dict(edge_struct) if edge_struct else {},
                    "edge_context": {
                        "edge_id": edge_id,
                        "src": str(edge_struct.get("src") or edge_struct.get("source") or ""),
                        "dst": str(edge_struct.get("dst") or edge_struct.get("target") or ""),
                        "edge_type": str(edge_struct.get("edge_type") or edge_struct.get("type") or ""),
                        "evidence": edge_struct.get("evidence") if isinstance(edge_struct.get("evidence"), dict) else {},
                    },
                    "semantic_payload": semantic_payload,
                    "semantic_status": status,
                    "feedback_round": row["feedback_round"],
                    "batch_index": row["batch_index"],
                    "source_updated_at": row["updated_at"],
                },
                evidence={"source": "graph_semantic_edges_backfill"},
                created_by=actor,
            )
            if existed:
                edge_event_updated += 1
            else:
                edge_event_created += 1
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "semantic_node_events_created": created,
        "semantic_node_events_updated": updated,
        "semantic_rows_skipped": skipped,
        "semantic_job_events_created": job_created,
        "edge_semantic_events_created": edge_event_created,
        "edge_semantic_events_updated": edge_event_updated,
        "edge_semantic_rows_skipped": edge_event_skipped,
    }


def _latest_semantic_event_for_node(
    conn: sqlite3.Connection,
    project_id: str,
    node_id: str,
    stable_key: str,
    branch_ref: str = "",
    *,
    current_feature_hash: str = "",
    current_file_hashes: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    ensure_schema(conn)
    params: list[Any] = [project_id, "semantic_node_enriched", branch_ref]
    sql = """
        SELECT * FROM graph_events
        WHERE project_id = ?
          AND event_type = ?
          AND status IN ('observed', 'materialized', 'accepted')
          AND (branch_ref = ? OR branch_ref = '')
    """
    if stable_key:
        sql += " AND stable_node_key = ?"
        params.append(stable_key)
    else:
        sql += " AND target_type = 'node' AND target_id = ?"
        params.append(node_id)
    params.append(branch_ref)
    sql += """
        ORDER BY CASE WHEN branch_ref = ? THEN 0 ELSE 1 END,
                 updated_at DESC, created_at DESC, event_seq DESC
        LIMIT 1
    """
    row = conn.execute(sql, params).fetchone()
    if row:
        return _row_to_event(row)
    if stable_key:
        row = conn.execute(
            """
            SELECT * FROM graph_events
            WHERE project_id = ?
              AND event_type = 'semantic_node_enriched'
              AND target_type = 'node'
              AND target_id = ?
              AND status IN ('observed', 'materialized', 'accepted')
              AND (branch_ref = ? OR branch_ref = '')
            ORDER BY CASE WHEN branch_ref = ? THEN 0 ELSE 1 END,
                     updated_at DESC, created_at DESC, event_seq DESC
            LIMIT 1
            """,
            (project_id, node_id, branch_ref, branch_ref),
        ).fetchone()
        if not row:
            return None
        event = _row_to_event(row)
        if not _target_id_fallback_matches_current_node(
            event,
            stable_key=stable_key,
            current_feature_hash=current_feature_hash,
            current_file_hashes=current_file_hashes or {},
        ):
            return None
        return event
    return None


def _target_id_fallback_matches_current_node(
    event: dict[str, Any],
    *,
    stable_key: str,
    current_feature_hash: str,
    current_file_hashes: dict[str, Any],
) -> bool:
    """Guard the legacy target_id fallback against L-id reuse.

    New semantic events carry stable_node_key and should be found by that
    key. The target_id fallback only exists for older rows; once L7 ids are
    renumbered, target_id alone can point at a completely different feature.
    """
    event_stable_key = str(event.get("stable_node_key") or "").strip()
    if stable_key and event_stable_key:
        return event_stable_key == stable_key

    stored_feature_hash = str(event.get("feature_hash") or "").strip()
    if current_feature_hash and stored_feature_hash:
        return _hash_values_equal(stored_feature_hash, current_feature_hash)

    stored_file_hashes = event.get("file_hashes") if isinstance(event.get("file_hashes"), dict) else {}
    if current_file_hashes and stored_file_hashes:
        return current_file_hashes == stored_file_hashes

    return False


def _eligible_semantic_edges(edges: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    eligible: dict[str, dict[str, Any]] = {}
    for edge in edges:
        edge_type = str(edge.get("edge_type") or edge.get("type") or "").strip()
        if edge_type == "contains":
            continue
        edge_id = _edge_id(edge)
        if edge_id:
            item = dict(edge)
            item["edge_id"] = edge_id
            eligible[edge_id] = item
    return eligible


def _latest_edge_semantic_events(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    edge_index: dict[str, dict[str, Any]] | None = None,
    branch_ref: str = "",
) -> dict[str, dict[str, Any]]:
    """Latest edge_semantic_* event per edge_id.

    MF 2026-05-11: previously this filtered by snapshot_id, which meant
    every fresh snapshot started with zero edge_semantic events even when
    the backfill had populated graph_semantic_edges from a carry-forward.
    The query now walks ALL snapshots for the project, preferring events
    whose `stable_node_key` (legacy column reused for stable_edge_key)
    matches the current snapshot's edge_index entry — same cross-snapshot
    pattern as `_latest_semantic_event_for_node`. Per-snapshot rows still
    win when they exist (event_seq ordering puts them last).
    """
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT * FROM graph_events
        WHERE project_id = ?
          AND target_type = 'edge'
          AND event_type IN ('edge_semantic_requested', 'edge_semantic_enriched')
          AND status IN ('observed', 'materialized', 'accepted')
          AND (branch_ref = ? OR branch_ref = '')
        ORDER BY CASE WHEN branch_ref = ? THEN 1 ELSE 0 END,
                 event_seq ASC, updated_at ASC, created_at ASC
        """,
        (project_id, branch_ref, branch_ref),
    ).fetchall()
    def event_rank(event: dict[str, Any]) -> tuple[int, int, str, str, int]:
        return (
            1 if str(event.get("snapshot_id") or "") == snapshot_id else 0,
            1 if branch_ref and str(event.get("branch_ref") or "") == branch_ref else 0,
            str(event.get("updated_at") or ""),
            str(event.get("created_at") or ""),
            int(event.get("event_seq") or 0),
        )

    def remember_latest(mapping: dict[str, dict[str, Any]], key: str, event: dict[str, Any]) -> None:
        if not key:
            return
        previous = mapping.get(key)
        if previous is None or event_rank(event) >= event_rank(previous):
            mapping[key] = event

    # Build lookup by target_id and (if available) by stable_edge_key.
    by_target_id: dict[str, dict[str, Any]] = {}
    by_stable_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        event = _row_to_event(row)
        target_id = str(event.get("target_id") or "").strip()
        stable_key = str(event.get("stable_node_key") or "").strip()
        for target_variant in _edge_id_variants(target_id):
            remember_latest(by_target_id, target_variant, event)
        if stable_key:
            remember_latest(by_stable_key, stable_key, event)
    def target_id_fallback_matches_current_edge(
        event: dict[str, Any],
        *,
        stable_key: str,
        current_edge_signature_hash: str,
    ) -> bool:
        event_stable_key = str(event.get("stable_node_key") or "").strip()
        if stable_key and event_stable_key:
            return event_stable_key == stable_key
        stored_signature_hash = str(event.get("feature_hash") or "").strip()
        if current_edge_signature_hash and stored_signature_hash:
            return _hash_values_equal(stored_signature_hash, current_edge_signature_hash)
        return False

    # If the caller provided edge_index, prefer stable_edge_key matches —
    # this is what lets a carry-forward'd edge in the new snapshot find
    # the latest semantic event even when target_id was renumbered.
    latest: dict[str, dict[str, Any]] = {}
    if edge_index:
        for edge_id, meta in edge_index.items():
            candidates: list[dict[str, Any]] = []
            stable_key = str(meta.get("stable_edge_key") or "")
            edge_signature_hash = str(meta.get("edge_signature_hash") or "")
            if stable_key and stable_key in by_stable_key:
                candidates.append(by_stable_key[stable_key])
            for edge_variant in _edge_id_variants(edge_id):
                if edge_variant in by_target_id:
                    event = by_target_id[edge_variant]
                    if (
                        str(event.get("snapshot_id") or "") == snapshot_id
                        or target_id_fallback_matches_current_edge(
                            event,
                            stable_key=stable_key,
                            current_edge_signature_hash=edge_signature_hash,
                        )
                    ):
                        candidates.append(event)
            if candidates:
                latest[edge_id] = max(candidates, key=event_rank)
        return latest
    # Backwards-compat path: no edge_index → return target_id-keyed
    # latest events. Used by older callers that didn't compute the
    # edge_index.
    return by_target_id


def _edge_semantic_status(edge: dict[str, Any] | None, event: dict[str, Any] | None) -> str:
    if not event:
        return "edge_semantic_missing"
    if not edge:
        return "edge_semantic_orphaned"
    if event.get("event_type") == "edge_semantic_enriched":
        semantic_payload = _event_payload_edge_semantic(event)
        evidence = semantic_payload.get("evidence") if isinstance(semantic_payload.get("evidence"), dict) else {}
        if str(evidence.get("source") or "") == "edge_semantic_rule":
            return "edge_semantic_rule"
        return "edge_semantic_current"
    return "edge_semantic_requested"


def _build_edge_semantics(
    edges: list[dict[str, Any]],
    edge_events: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    eligible_edges = _eligible_semantic_edges(edges)
    # MF 2026-05-11: drop the previous `set(eligible) | set(edge_events)`
    # union so orphans (semantic event exists but structural edge is
    # gone) DON'T appear in the projection. Matches the node-side
    # `for node in nodes` loop in build_semantic_projection that scopes
    # node_semantics to the current snapshot's structural nodes.
    # Orphan history is still in graph_events for audit; just hidden from
    # the projection view.
    edge_ids = sorted(eligible_edges)
    out: dict[str, dict[str, Any]] = {}
    for edge_id in edge_ids:
        edge = eligible_edges.get(edge_id)
        event = edge_events.get(edge_id)
        status = _edge_semantic_status(edge, event)
        edge_payload = dict(edge or _edge_from_id(edge_id))
        edge_payload.setdefault("edge_id", edge_id)
        out[edge_id] = {
            "edge_id": edge_id,
            "edge": edge_payload,
            "semantic": _event_payload_edge_semantic(event or {}),
            "validity": {
                "status": status,
                "valid": status == "edge_semantic_current",
                "requires_ai": status != "edge_semantic_current",
                "edge_exists": bool(edge),
                "semantic_event_id": (event or {}).get("event_id", ""),
                "semantic_event_snapshot_id": (event or {}).get("snapshot_id", ""),
                "semantic_event_commit": (event or {}).get("target_commit", "")
                or (event or {}).get("baseline_commit", ""),
            },
            "source_event": {
                "event_id": (event or {}).get("event_id", ""),
                "snapshot_id": (event or {}).get("snapshot_id", ""),
                "event_seq": (event or {}).get("event_seq", 0),
                "event_type": (event or {}).get("event_type", ""),
                "branch_ref": (event or {}).get("branch_ref", ""),
                "operation_type": (event or {}).get("operation_type", ""),
                "source_branch_ref": (event or {}).get("source_branch_ref", ""),
                "source_snapshot_id": (event or {}).get("source_snapshot_id", ""),
                "source_event_id": (event or {}).get("source_event_id", ""),
                "payload_hash": (event or {}).get("payload_hash", ""),
                "updated_at": (event or {}).get("updated_at", ""),
            },
        }
    return out


def _edge_projection_health(
    edges: list[dict[str, Any]],
    edge_semantics: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    eligible_count = len(_eligible_semantic_edges(edges))
    status_counts: dict[str, int] = {}
    for entry in edge_semantics.values():
        validity = entry.get("validity") if isinstance(entry.get("validity"), dict) else {}
        status = str(validity.get("status") or "edge_semantic_missing")
        status_counts[status] = status_counts.get(status, 0) + 1
    current = int(status_counts.get("edge_semantic_current", 0) or 0)
    requested = int(status_counts.get("edge_semantic_requested", 0) or 0)
    rule = int(status_counts.get("edge_semantic_rule", 0) or 0)
    orphaned = int(status_counts.get("edge_semantic_orphaned", 0) or 0)
    unqueued = int(status_counts.get("edge_semantic_missing", 0) or 0)
    missing = max(0, eligible_count - current)
    payload_current = current + rule
    return {
        "edge_count": len(edges),
        "edge_semantic_eligible_count": eligible_count,
        "edge_semantic_current_count": current,
        "edge_semantic_requested_count": requested,
        "edge_semantic_rule_count": rule,
        "edge_semantic_missing_count": missing,
        "edge_semantic_unqueued_count": unqueued,
        "edge_semantic_needs_ai_count": missing,
        "edge_semantic_payload_current_count": payload_current,
        "edge_semantic_orphaned_count": orphaned,
        "edge_semantic_status_counts": dict(sorted(status_counts.items())),
        "edge_semantic_coverage_ratio": round(current / eligible_count, 4) if eligible_count else 1.0,
        "edge_semantic_payload_coverage_ratio": (
            round(payload_current / eligible_count, 4) if eligible_count else 1.0
        ),
    }


def _file_hash_status(current: dict[str, Any], stored: dict[str, Any]) -> str:
    if not current or not stored:
        return "unknown"
    return "match" if current == stored else "mismatch"


def _semantic_validity(
    node: dict[str, Any],
    event: dict[str, Any] | None,
    *,
    snapshot_id: str,
    commit_sha: str,
) -> dict[str, Any]:
    current_feature_hash = feature_hash_for_node(node)
    current_file_hashes = {}
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    if isinstance(metadata.get("file_hashes"), dict):
        current_file_hashes = metadata.get("file_hashes")
    if not event:
        return {
            "status": "semantic_missing",
            "valid": False,
            "current_feature_hash": current_feature_hash,
            "stored_feature_hash": "",
            "hash_validation": "missing_semantic_event",
            "file_hash_status": "unknown",
        }
    stored_feature_hash = str(event.get("feature_hash") or "")
    stored_file_hashes = event.get("file_hashes") if isinstance(event.get("file_hashes"), dict) else {}
    feature_hash_match = _hash_values_equal(stored_feature_hash, current_feature_hash)
    file_hash_status = _file_hash_status(current_file_hashes, stored_file_hashes)
    file_hash_match = file_hash_status in {"match", "unknown"}
    base_commit = str(event.get("baseline_commit") or event.get("target_commit") or "")
    event_snapshot_id = str(event.get("snapshot_id") or "")
    same_snapshot_event = bool(event_snapshot_id == snapshot_id and (not base_commit or base_commit == commit_sha))
    hash_scheme_mismatch = (
        _hash_scheme(stored_feature_hash) == "indexed_sha256"
        and _hash_scheme(current_feature_hash) == "fallback_sha256"
        and not feature_hash_match
    )
    if same_snapshot_event and stored_feature_hash:
        status = "semantic_current"
        hash_validation = "same_snapshot_event"
    elif feature_hash_match and file_hash_match and base_commit == commit_sha:
        status = "semantic_current"
        hash_validation = "matched"
    elif feature_hash_match and file_hash_match:
        status = "semantic_carried_forward_current"
        hash_validation = "matched_carried_forward"
    elif file_hash_status == "match" and stored_file_hashes and current_file_hashes:
        status = "semantic_carried_forward_current"
        hash_validation = "file_hash_matched"
    elif hash_scheme_mismatch or (
        _hash_scheme(stored_feature_hash) == "indexed_sha256"
        and stored_file_hashes
        and not current_file_hashes
    ):
        status = "semantic_carried_forward_current"
        hash_validation = "hash_source_unavailable"
    elif not feature_hash_match:
        status = "semantic_stale_feature_hash"
        hash_validation = "mismatch"
    else:
        status = "semantic_stale_file_hash"
        hash_validation = "mismatch"
    return {
        "status": status,
        "valid": status in {"semantic_current", "semantic_carried_forward_current"},
        "current_feature_hash": current_feature_hash,
        "stored_feature_hash": stored_feature_hash,
        "feature_hash_match": feature_hash_match,
        "current_hash_scheme": _hash_scheme(current_feature_hash),
        "stored_hash_scheme": _hash_scheme(stored_feature_hash),
        "hash_validation": hash_validation,
        "file_hash_status": file_hash_status,
        "file_hash_match": file_hash_match,
        "semantic_event_id": event.get("event_id", ""),
        "semantic_event_snapshot_id": event.get("snapshot_id", ""),
        "semantic_event_commit": base_commit,
    }


def _projection_health(
    nodes: list[dict[str, Any]],
    node_semantics: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    status_score_penalties = {
        "semantic_missing": 45,
        "semantic_carried_forward_unverified_hash": 35,
    }
    raw_l7_nodes = [node for node in nodes if str(node.get("layer") or "").upper() == "L7"]
    l7_nodes = [node for node in raw_l7_nodes if _is_governed_feature_node(node)]
    total = len(l7_nodes)
    status_counts: dict[str, int] = {}
    current_count = 0
    stale_count = 0
    missing_count = 0
    unverified_count = 0
    doc_bound = 0
    test_bound = 0
    semantic_issue_count = 0
    low_health: list[dict[str, Any]] = []
    for node in l7_nodes:
        node_id = str(node.get("node_id") or "")
        semantic = node_semantics.get(node_id, {})
        validity = semantic.get("validity") if isinstance(semantic.get("validity"), dict) else {}
        status = str(validity.get("status") or "semantic_missing")
        status_counts[status] = status_counts.get(status, 0) + 1
        if status in {"semantic_current", "semantic_carried_forward_current"}:
            current_count += 1
        elif status == "semantic_missing":
            missing_count += 1
        elif status == "semantic_carried_forward_unverified_hash":
            unverified_count += 1
        else:
            stale_count += 1
        node_score = 100
        issues: list[str] = []
        if not _string_list(node.get("secondary_files")):
            node_score -= 6
            issues.append("missing_doc_binding")
        else:
            doc_bound += 1
        if not _string_list(node.get("test_files")):
            node_score -= 8
            issues.append("missing_test_binding")
        else:
            test_bound += 1
        if status == "semantic_missing":
            node_score -= status_score_penalties["semantic_missing"]
            issues.append("semantic_missing")
        elif status.startswith("semantic_stale"):
            node_score -= 40
            issues.append(status)
        elif status == "semantic_carried_forward_unverified_hash":
            node_score -= status_score_penalties["semantic_carried_forward_unverified_hash"]
            issues.append(status)
        semantic_payload = semantic.get("semantic") if isinstance(semantic.get("semantic"), dict) else {}
        for issue in semantic_payload.get("open_issues") or []:
            if isinstance(issue, dict):
                issue_type = str(issue.get("type") or issue.get("kind") or "open_issue")
            else:
                issue_type = "open_issue"
            issues.append(issue_type)
            semantic_issue_count += 1
            node_score -= 2
        node_score = max(0, node_score)
        if issues or node_score < 90:
            low_health.append({
                "node_id": node_id,
                "title": node.get("title") or node_id,
                "score": node_score,
                "issues": sorted(set(issues)),
                "semantic_status": status,
                "primary_files": _string_list(node.get("primary_files"))[:8],
            })
    low_health.sort(key=lambda item: (int(item.get("score") or 0), str(item.get("node_id") or "")))
    def ratio(value: int) -> float:
        return round(value / total, 4) if total else 0.0
    review_debt_count = missing_count + stale_count + unverified_count
    health_score = 100.0
    if total:
        semantic_debt_penalty = (
            missing_count * status_score_penalties["semantic_missing"]
            + stale_count * 40
            + unverified_count * status_score_penalties["semantic_carried_forward_unverified_hash"]
        ) / total
        binding_context_penalty = ((total - doc_bound) * 2 + (total - test_bound) * 2) / total
        open_issue_penalty = min(10.0, (semantic_issue_count * 2) / total)
        health_score -= round(semantic_debt_penalty, 2)
        health_score -= round(binding_context_penalty, 2)
        health_score -= round(open_issue_penalty, 2)
    else:
        semantic_debt_penalty = 0.0
        binding_context_penalty = 0.0
        open_issue_penalty = 0.0
    return {
        "score_version": "semantic_projection_v2_hash_validity_review_debt",
        "feature_count": total,
        "raw_feature_count": len(raw_l7_nodes),
        "governed_feature_count": total,
        "excluded_feature_count": max(0, len(raw_l7_nodes) - total),
        "semantic_current_count": current_count,
        "semantic_stale_count": stale_count,
        "semantic_missing_count": missing_count,
        "semantic_unverified_hash_count": unverified_count,
        "semantic_current_ratio": ratio(current_count),
        "semantic_trusted_count": current_count,
        "semantic_trusted_ratio": ratio(current_count),
        "semantic_review_debt_count": review_debt_count,
        "semantic_review_debt_ratio": ratio(review_debt_count),
        "doc_bound_count": doc_bound,
        "doc_coverage_ratio": ratio(doc_bound),
        "test_bound_count": test_bound,
        "test_coverage_ratio": ratio(test_bound),
        "semantic_status_counts": dict(sorted(status_counts.items())),
        "semantic_debt_penalty": round(semantic_debt_penalty, 2),
        "binding_context_penalty": round(binding_context_penalty, 2),
        "open_issue_penalty": round(open_issue_penalty, 2),
        "semantic_open_issue_count": semantic_issue_count,
        "project_health_score": round(max(0.0, health_score), 2),
        "low_health_count": len(low_health),
        "low_health_nodes": low_health[:100],
    }


def build_semantic_projection(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    actor: str = "semantic_projection_builder",
    projection_id: str | None = None,
    ref_name: str = "",
    branch_ref: str = "",
    backfill_existing: bool = True,
) -> dict[str, Any]:
    """Materialize a query-friendly semantic view from structure + events."""
    ensure_schema(conn)
    snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    if backfill_existing:
        backfill_existing_semantic_events(conn, project_id, snapshot_id, actor=actor)
    commit_sha = str(snapshot.get("commit_sha") or "")
    identity = _projection_ref_identity(
        conn,
        project_id,
        snapshot_id,
        ref_name=ref_name,
        branch_ref=branch_ref,
    )
    ref_name = identity["ref_name"]
    branch_ref = identity["branch_ref"]
    nodes = store.list_graph_snapshot_nodes(
        conn,
        project_id,
        snapshot_id,
        include_semantic=False,
        limit=1000,
    )
    edges = store.list_graph_snapshot_edges(
        conn,
        project_id,
        snapshot_id,
        limit=2000,
    )
    node_semantics: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        if not node_id:
            continue
        stable_key = stable_node_key_for_node(node)
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        current_file_hashes = (
            metadata.get("file_hashes")
            if isinstance(metadata.get("file_hashes"), dict)
            else {}
        )
        event = _latest_semantic_event_for_node(
            conn,
            project_id,
            node_id,
            stable_key,
            branch_ref=branch_ref,
            current_feature_hash=feature_hash_for_node(node),
            current_file_hashes=current_file_hashes,
        )
        validity = _semantic_validity(node, event, snapshot_id=snapshot_id, commit_sha=commit_sha)
        node_semantics[node_id] = {
            "node_id": node_id,
            "stable_node_key": stable_key,
            "semantic": _event_payload_semantic(event or {}),
            "validity": validity,
            "source_event": {
                "event_id": (event or {}).get("event_id", ""),
                "snapshot_id": (event or {}).get("snapshot_id", ""),
                "event_seq": (event or {}).get("event_seq", 0),
                "branch_ref": (event or {}).get("branch_ref", ""),
                "operation_type": (event or {}).get("operation_type", ""),
                "source_branch_ref": (event or {}).get("source_branch_ref", ""),
                "source_snapshot_id": (event or {}).get("source_snapshot_id", ""),
                "source_event_id": (event or {}).get("source_event_id", ""),
                "payload_hash": (event or {}).get("payload_hash", ""),
                "updated_at": (event or {}).get("updated_at", ""),
            },
        }
    # MF 2026-05-11: build an edge_index keyed by edge_id with stable_edge_key
    # so _latest_edge_semantic_events can find events cross-snapshot when an
    # edge was carried forward but its event row sits in a previous snapshot.
    edge_carry_index: dict[str, dict[str, Any]] = {}
    node_lookup_for_edges: dict[str, dict[str, Any]] = {}
    for node in nodes:
        nid = str(node.get("node_id") or node.get("id") or "").strip()
        if nid:
            node_lookup_for_edges[nid] = node
    for edge in edges:
        edge_type = str(edge.get("edge_type") or edge.get("type") or "").strip()
        if edge_type == "contains":
            continue
        eid = _edge_id(edge)
        if not eid:
            continue
        src = str(edge.get("src") or edge.get("source") or "")
        dst = str(edge.get("dst") or edge.get("target") or "")
        edge_carry_index[eid] = {
            "edge_id": eid,
            "stable_edge_key": stable_edge_key_for_edge(
                edge,
                node_lookup_for_edges.get(src),
                node_lookup_for_edges.get(dst),
            ),
            "edge_signature_hash": edge_signature_hash_for_edge(
                edge,
                node_lookup_for_edges.get(src),
                node_lookup_for_edges.get(dst),
            ),
        }
    edge_semantics = _build_edge_semantics(
        edges,
        _latest_edge_semantic_events(
            conn, project_id, snapshot_id, edge_index=edge_carry_index, branch_ref=branch_ref,
        ),
    )
    watermark_row = conn.execute(
        "SELECT COALESCE(MAX(event_seq), 0) AS watermark FROM graph_events WHERE project_id = ? AND snapshot_id = ?",
        (project_id, snapshot_id),
    ).fetchone()
    event_watermark = int(watermark_row["watermark"] if watermark_row else 0)
    health = _projection_health(nodes, node_semantics)
    health.update(_edge_projection_health(edges, edge_semantics))
    projection = {
        "schema_version": 1,
        "projection_rule_version": SEMANTIC_PROJECTION_RULE_VERSION,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "ref_name": ref_name,
        "branch_ref": branch_ref,
        "commit_sha": commit_sha,
        "event_watermark": event_watermark,
        "node_semantics": node_semantics,
        "edge_semantics": edge_semantics,
        "health_review": health,
    }
    pid = projection_id or f"semproj-{str(commit_sha or 'unknown')[:7]}-{event_watermark}-{uuid.uuid4().hex[:6]}"
    now = store.utc_now()
    conn.execute(
        """
        INSERT INTO graph_semantic_projections
          (project_id, snapshot_id, projection_id, base_commit, ref_name, branch_ref,
           projection_rule_version, event_watermark,
           status, projection_json, health_json, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id, snapshot_id, projection_id) DO UPDATE SET
          base_commit = excluded.base_commit,
          ref_name = excluded.ref_name,
          branch_ref = excluded.branch_ref,
          projection_rule_version = excluded.projection_rule_version,
          event_watermark = excluded.event_watermark,
          status = excluded.status,
          projection_json = excluded.projection_json,
          health_json = excluded.health_json,
          updated_at = excluded.updated_at
        """,
        (
            project_id,
            snapshot_id,
            pid,
            commit_sha,
            ref_name,
            branch_ref,
            SEMANTIC_PROJECTION_RULE_VERSION,
            event_watermark,
            "current",
            _json(projection),
            _json(health),
            actor,
            now,
            now,
        ),
    )
    create_event(
        conn,
        project_id,
        snapshot_id,
        event_id=_safe_event_id("semproj", pid),
        event_type="semantic_projection_generated",
        event_kind="projection",
        target_type="snapshot",
        target_id=snapshot_id,
        status=EVENT_STATUS_OBSERVED,
        baseline_commit=commit_sha,
        target_commit=commit_sha,
        branch_ref=branch_ref,
        payload={
            "projection_id": pid,
            "ref_name": ref_name,
            "branch_ref": branch_ref,
            "projection_rule_version": SEMANTIC_PROJECTION_RULE_VERSION,
            "event_watermark": event_watermark,
            "health": health,
        },
        evidence={"source": "semantic_projection_builder"},
        created_by=actor,
    )
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "projection_id": pid,
        "base_commit": commit_sha,
        "ref_name": ref_name,
        "branch_ref": branch_ref,
        "projection_rule_version": SEMANTIC_PROJECTION_RULE_VERSION,
        "event_watermark": event_watermark,
        "projection": projection,
        "health": health,
    }


def get_semantic_projection(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    projection_id: str = "",
    *,
    ref_name: str = "",
    branch_ref: str | None = None,
) -> dict[str, Any] | None:
    ensure_schema(conn)
    if projection_id:
        row = conn.execute(
            """
            SELECT * FROM graph_semantic_projections
            WHERE project_id = ? AND snapshot_id = ? AND projection_id = ?
            """,
            (project_id, snapshot_id, projection_id),
        ).fetchone()
    else:
        filters: list[str] = []
        params: list[Any] = [project_id, snapshot_id]
        if ref_name:
            if str(ref_name or "") == "active":
                filters.append("(ref_name = ? OR ref_name = '')")
                params.append("active")
            else:
                filters.append("ref_name = ?")
                params.append(str(ref_name or ""))
        if branch_ref is not None:
            filters.append("branch_ref = ?")
            params.append(str(branch_ref or ""))
        where_extra = (" AND " + " AND ".join(filters)) if filters else ""
        row = conn.execute(
            f"""
            SELECT * FROM graph_semantic_projections
            WHERE project_id = ? AND snapshot_id = ?
            {where_extra}
            ORDER BY event_watermark DESC, created_at DESC LIMIT 1
            """,
            params,
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["projection"] = _json_load(item.pop("projection_json", "{}"), {})
    item["health"] = _json_load(item.pop("health_json", "{}"), {})
    return item


__all__ = [
    "ALLOWED_EVENT_STATUSES",
    "ALLOWED_EVENT_TYPES",
    "EVENT_STATUS_ACCEPTED",
    "EVENT_STATUS_BACKLOG_FILED",
    "EVENT_STATUS_MATERIALIZED",
    "EVENT_STATUS_OBSERVED",
    "EVENT_STATUS_PROPOSED",
    "EVENT_STATUS_REJECTED",
    "EVENT_STATUS_STALE",
    "GRAPH_EVENTS_SCHEMA_SQL",
    "create_event",
    "backfill_existing_semantic_events",
    "build_semantic_projection",
    "ensure_schema",
    "feature_hash_for_node",
    "get_event",
    "get_semantic_projection",
    "list_events",
    "mark_backlog_filed",
    "materialize_events",
    "preview_events",
    "refine_event",
    "status_counts",
    "stable_node_key_for_node",
    "stable_edge_key_for_edge",
    "edge_signature_hash_for_edge",
    "update_event_status",
]
