"""Gate AI-produced graph structure operations before hint materialization."""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "graph_structure_ops.v1"
SUPPORTED_HINT_OPS = {"move_file", "add_edge", "suppress_edge"}
ROLE_ALLOWLIST = {"primary", "secondary", "test", "config", "doc"}
EDGE_ALLOWLIST = {
    "depends_on",
    "tests",
    "documents",
    "configures",
    "uses",
    "calls",
    "imports",
}
_HINT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


def validate_graph_structure_ops(
    payload: Mapping[str, Any],
    *,
    graph: Mapping[str, Any],
    inventory_paths: Iterable[str],
    snapshot_id: str = "",
    base_commit: str = "",
) -> dict[str, Any]:
    """Validate graph_structure_ops.v1 and emit hint-compatible operations.

    This is the server-side trust boundary. AI self_check is advisory and must
    be present, but the gate recomputes all supported materialization rules.
    """
    source = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
    operations_raw = payload.get("operations") if isinstance(payload.get("operations"), list) else []
    self_check = payload.get("self_check") if isinstance(payload.get("self_check"), Mapping) else {}
    node_ids = _graph_node_ids(graph)
    paths = {_norm_path(path) for path in inventory_paths if _norm_path(path)}

    global_errors: list[str] = []
    if str(payload.get("schema_version") or "") != SCHEMA_VERSION:
        global_errors.append("schema_version_invalid")
    if snapshot_id and str(source.get("snapshot_id") or "") != snapshot_id:
        global_errors.append("source_snapshot_mismatch")
    if base_commit and str(source.get("base_commit") or "") != base_commit:
        global_errors.append("source_base_commit_mismatch")
    if self_check.get("valid") is not True:
        global_errors.append("self_check_invalid")
    if not isinstance(self_check.get("checked_rules"), list) or not self_check.get("checked_rules"):
        global_errors.append("self_check_missing_checked_rules")
    if not isinstance(operations_raw, list) or not operations_raw:
        global_errors.append("operations_missing")

    normalized: list[dict[str, Any]] = []
    seen_hint_ids: set[str] = set()
    for index, raw in enumerate(operations_raw):
        op = raw if isinstance(raw, Mapping) else {}
        entry = _validate_operation(
            op,
            index=index,
            node_ids=node_ids,
            inventory_paths=paths,
            seen_hint_ids=seen_hint_ids,
        )
        normalized.append(entry)

    conflict_errors = _mark_conflicts(normalized)
    global_errors.extend(conflict_errors)

    accepted_hints = [
        entry["hint"]
        for entry in normalized
        if entry["status"] == "accepted" and not entry["errors"]
    ]
    rejected_count = len(normalized) - len(accepted_hints)
    ok = not global_errors and rejected_count == 0
    return {
        "ok": ok,
        "status": "passed" if ok else "failed",
        "schema_version": SCHEMA_VERSION,
        "errors": _dedupe(global_errors),
        "accepted_count": len(accepted_hints),
        "rejected_count": rejected_count,
        "conflict_count": len(conflict_errors),
        "operations": normalized,
        "normalized_hint_index": {
            "hint_count": len(accepted_hints),
            "hints": accepted_hints,
        },
    }


def _validate_operation(
    op: Mapping[str, Any],
    *,
    index: int,
    node_ids: set[str],
    inventory_paths: set[str],
    seen_hint_ids: set[str],
) -> dict[str, Any]:
    errors: list[str] = []
    op_name = str(op.get("op") or "").strip()
    hint_id = str(op.get("hint_id") or "").strip()
    source_path = _norm_path(op.get("source_path"))
    target_node_id = str(op.get("target_node_id") or "").strip()
    role = str(op.get("role") or "").strip()
    edge = str(op.get("edge") or op.get("edge_type") or "").strip()

    if op_name not in SUPPORTED_HINT_OPS:
        errors.append("unsupported_op_for_hint_materialization")
    if not hint_id:
        errors.append("hint_id_missing")
    elif not _HINT_ID_RE.match(hint_id):
        errors.append("hint_id_invalid")
    elif hint_id in seen_hint_ids:
        errors.append("hint_id_duplicate")
    seen_hint_ids.add(hint_id)
    if op_name in SUPPORTED_HINT_OPS:
        if not source_path:
            errors.append("source_path_missing")
        elif source_path not in inventory_paths:
            errors.append("source_path_missing")
        if not target_node_id:
            errors.append("target_node_missing")
        elif target_node_id not in node_ids:
            errors.append("target_node_missing")
        if op_name == "move_file":
            if role not in ROLE_ALLOWLIST:
                errors.append("role_unsupported")
        elif op_name in {"add_edge", "suppress_edge"}:
            if edge not in EDGE_ALLOWLIST:
                errors.append("edge_unsupported")
    confidence = op.get("confidence")
    if confidence is not None:
        try:
            confidence_f = float(confidence)
        except (TypeError, ValueError):
            confidence_f = -1.0
        if confidence_f < 0.0 or confidence_f > 1.0:
            errors.append("confidence_out_of_range")

    hint = {
        "hint_id": hint_id,
        "op": op_name,
        "edge": edge if op_name in {"add_edge", "suppress_edge"} else "",
        "role": role if op_name == "move_file" else "",
        "target_node_id": target_node_id,
        "source_path": source_path,
        "reason": _reason(op.get("evidence")),
        "evidence": _evidence_text(op.get("evidence")),
        "anchor": {"symbol": "", "line_start": 0, "line_end": 0},
        "status": "accepted" if not errors else "rejected",
    }
    return {
        "index": index,
        "op": op_name,
        "hint_id": hint_id,
        "source_path": source_path,
        "target_node_id": target_node_id,
        "role": role,
        "edge": edge,
        "status": "accepted" if not errors else "rejected",
        "errors": errors,
        "hint": hint,
    }


def _mark_conflicts(operations: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    move_targets: dict[tuple[str, str], set[str]] = {}
    edge_actions: dict[tuple[str, str, str], set[str]] = {}

    for entry in operations:
        if entry["errors"]:
            continue
        if entry["op"] == "move_file":
            key = (entry["source_path"], entry["role"])
            move_targets.setdefault(key, set()).add(entry["target_node_id"])
        elif entry["op"] in {"add_edge", "suppress_edge"}:
            key = (entry["source_path"], entry["target_node_id"], entry["edge"])
            edge_actions.setdefault(key, set()).add(entry["op"])

    conflicting_moves = {
        key for key, targets in move_targets.items() if len(targets) > 1
    }
    conflicting_edges = {
        key for key, actions in edge_actions.items()
        if {"add_edge", "suppress_edge"} <= actions
    }

    for entry in operations:
        if entry["op"] == "move_file" and (entry["source_path"], entry["role"]) in conflicting_moves:
            entry["errors"].append("conflicting_move_file_target")
            entry["status"] = "rejected"
            entry["hint"]["status"] = "rejected"
        elif (
            entry["op"] in {"add_edge", "suppress_edge"}
            and (entry["source_path"], entry["target_node_id"], entry["edge"]) in conflicting_edges
        ):
            entry["errors"].append("conflicting_edge_add_suppress")
            entry["status"] = "rejected"
            entry["hint"]["status"] = "rejected"

    if conflicting_moves:
        errors.append("conflicting_move_file_target")
    if conflicting_edges:
        errors.append("conflicting_edge_add_suppress")
    return errors


def _graph_node_ids(graph: Mapping[str, Any]) -> set[str]:
    deps_graph = graph.get("deps_graph") if isinstance(graph.get("deps_graph"), Mapping) else {}
    nodes = deps_graph.get("nodes") if isinstance(deps_graph.get("nodes"), list) else []
    return {
        str((node if isinstance(node, Mapping) else {}).get("id") or "").strip()
        for node in nodes
        if str((node if isinstance(node, Mapping) else {}).get("id") or "").strip()
    }


def _norm_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip("/")


def _reason(evidence: Any) -> str:
    if isinstance(evidence, Mapping):
        return str(evidence.get("reason") or evidence.get("summary") or "").strip()
    return ""


def _evidence_text(evidence: Any) -> str:
    if isinstance(evidence, Mapping):
        text = evidence.get("evidence") or evidence.get("basis") or ""
        return str(text or "").strip()
    return str(evidence or "").strip()


def _dedupe(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return out
