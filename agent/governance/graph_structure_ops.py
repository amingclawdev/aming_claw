"""Gate AI-produced graph structure operations before hint materialization."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from agent.governance.graph_hint_projection import build_hint_projection
from agent.governance.graph_structure_hints import write_graph_structure_hints


SCHEMA_VERSION = "graph_structure_ops.v1"
ANALYZER_ROLE = "reconcile_graph_structure_analyzer"
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
_DEFAULT_REQUIRED_FIELDS = {
    "move_file": ["op", "hint_id", "source_path", "target_node_id", "role"],
    "add_edge": ["op", "hint_id", "source_path", "target_node_id", "edge"],
    "suppress_edge": ["op", "hint_id", "source_path", "target_node_id", "edge"],
}
_DEFAULT_EVIDENCE_POLICY = {
    "dedupe_operations": True,
    "calls": {
        "require_call_evidence": True,
        "import_only_action": "downgrade",
        "downgrade_to": "imports",
    },
}


def default_graph_structure_ops_contract() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "analyzer_role": ANALYZER_ROLE,
        "evidence_policy": _copy_evidence_policy(_DEFAULT_EVIDENCE_POLICY),
        "operations": {
            op: {
                "enabled": True,
                "materializer": "source_hint",
                "required_fields": list(_DEFAULT_REQUIRED_FIELDS[op]),
                "role_allowlist": sorted(ROLE_ALLOWLIST) if op == "move_file" else [],
                "edge_allowlist": sorted(EDGE_ALLOWLIST) if op in {"add_edge", "suppress_edge"} else [],
            }
            for op in sorted(SUPPORTED_HINT_OPS)
        },
    }


def normalize_graph_structure_ops_contract(raw: Mapping[str, Any] | None = None) -> dict[str, Any]:
    contract = default_graph_structure_ops_contract()
    if raw is None:
        return contract
    if not isinstance(raw, Mapping):
        raise ValueError("graph_structure_ops contract must be a mapping")
    if raw.get("schema_version") is not None:
        contract["schema_version"] = str(raw.get("schema_version") or "").strip()
    if raw.get("analyzer_role") is not None:
        contract["analyzer_role"] = str(raw.get("analyzer_role") or "").strip()
    if raw.get("evidence_policy") is not None:
        contract["evidence_policy"] = _normalize_evidence_policy(raw.get("evidence_policy"))
    operations_raw = raw.get("operations")
    if operations_raw is None:
        return _validate_contract(contract)
    if not isinstance(operations_raw, Mapping):
        raise ValueError("graph_structure_ops.operations must be a mapping")
    operations = dict(contract["operations"])
    for op_name_raw, spec_raw in operations_raw.items():
        op_name = str(op_name_raw or "").strip()
        if not op_name:
            raise ValueError("graph_structure_ops operation name cannot be empty")
        if op_name not in SUPPORTED_HINT_OPS:
            raise ValueError(f"graph_structure_ops operation has no source_hint handler: {op_name}")
        if spec_raw is None:
            continue
        if not isinstance(spec_raw, Mapping):
            raise ValueError("graph_structure_ops operation specs must be mappings")
        spec = dict(operations.get(op_name) or {})
        for key in ("enabled", "materializer"):
            if key in spec_raw:
                spec[key] = bool(spec_raw[key]) if key == "enabled" else str(spec_raw[key] or "").strip()
        for key in ("required_fields", "role_allowlist", "edge_allowlist"):
            if key in spec_raw:
                value = spec_raw[key]
                if isinstance(value, str):
                    value = [item.strip() for item in value.split(",")]
                if not isinstance(value, list):
                    raise ValueError(f"graph_structure_ops.{op_name}.{key} must be a list")
                spec[key] = [str(item).strip() for item in value if str(item).strip()]
        operations[op_name] = spec
    contract["operations"] = operations
    return _validate_contract(contract)


def graph_structure_ops_output_contract(contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
    normalized = normalize_graph_structure_ops_contract(contract)
    operations = normalized["operations"]
    enabled = {
        name: spec
        for name, spec in operations.items()
        if spec.get("enabled") is True
    }
    return {
        "schema_version": normalized["schema_version"],
        "return_exactly_one_json_object": True,
        "supported_operations": sorted(enabled),
        "supported_roles": sorted({
            role
            for spec in enabled.values()
            for role in spec.get("role_allowlist", [])
        }),
        "supported_edges": sorted({
            edge
            for spec in enabled.values()
            for edge in spec.get("edge_allowlist", [])
        }),
        "required_top_level_fields": ["schema_version", "source", "operations", "self_check"],
        "required_operation_fields": {
            name: list(spec.get("required_fields") or [])
            for name, spec in sorted(enabled.items())
        },
        "source": {
            "analyzer_role": normalized["analyzer_role"],
        },
        "evidence_policy": _copy_evidence_policy(normalized.get("evidence_policy") or {}),
        "self_check_required": True,
        "no_markdown": True,
    }


def parse_graph_structure_ai_output(raw_output: Any) -> dict[str, Any]:
    """Parse one AI-produced graph_structure_ops JSON object.

    The analyzer prompt requires exactly one JSON object. This parser keeps that
    boundary strict so downstream dry-run/accept code never has to guess around
    prose, arrays, or partial JSON fragments.
    """
    if isinstance(raw_output, Mapping):
        return {"ok": True, "payload": dict(raw_output), "errors": []}
    if not isinstance(raw_output, str) or not raw_output.strip():
        return {"ok": False, "payload": {}, "errors": ["ai_output_missing"]}
    decoder = json.JSONDecoder()
    text = raw_output.strip()
    try:
        payload, end = decoder.raw_decode(text)
    except json.JSONDecodeError:
        return {"ok": False, "payload": {}, "errors": ["ai_output_json_invalid"]}
    if text[end:].strip():
        return {"ok": False, "payload": {}, "errors": ["ai_output_extra_content"]}
    if not isinstance(payload, dict):
        return {"ok": False, "payload": {}, "errors": ["ai_output_not_object"]}
    return {"ok": True, "payload": payload, "errors": []}


def validate_graph_structure_ops(
    payload: Mapping[str, Any],
    *,
    graph: Mapping[str, Any],
    inventory_paths: Iterable[str],
    snapshot_id: str = "",
    base_commit: str = "",
    operation_contract: Mapping[str, Any] | None = None,
    project_root: str | Path = "",
) -> dict[str, Any]:
    """Validate graph_structure_ops.v1 and emit hint-compatible operations.

    This is the server-side trust boundary. AI self_check is advisory and must
    be present, but the gate recomputes all supported materialization rules.
    """
    source = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
    operations_raw = payload.get("operations") if isinstance(payload.get("operations"), list) else []
    self_check = payload.get("self_check") if isinstance(payload.get("self_check"), Mapping) else {}
    contract = normalize_graph_structure_ops_contract(operation_contract)
    graph_nodes = _graph_nodes_by_id(graph)
    paths = {_norm_path(path) for path in inventory_paths if _norm_path(path)}

    global_errors: list[str] = []
    if str(payload.get("schema_version") or "") != contract["schema_version"]:
        global_errors.append("schema_version_invalid")
    if snapshot_id and str(source.get("snapshot_id") or "") != snapshot_id:
        global_errors.append("source_snapshot_mismatch")
    if base_commit and str(source.get("base_commit") or "") != base_commit:
        global_errors.append("source_base_commit_mismatch")
    if str(source.get("analyzer_role") or "") != contract["analyzer_role"]:
        global_errors.append("source_analyzer_role_invalid")
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
            graph_nodes=graph_nodes,
            inventory_paths=paths,
            seen_hint_ids=seen_hint_ids,
            operation_contract=contract,
            project_root=project_root,
        )
        normalized.append(entry)

    deduped_count = _mark_duplicate_operations(normalized, contract)
    conflict_errors = _mark_conflicts(normalized)
    global_errors.extend(conflict_errors)

    accepted_hints = [
        entry["hint"]
        for entry in normalized
        if entry["status"] == "accepted" and not entry["errors"]
    ]
    rejected_count = sum(1 for entry in normalized if entry["status"] == "rejected")
    ok = not global_errors and rejected_count == 0
    return {
        "ok": ok,
        "status": "passed" if ok else "failed",
        "schema_version": contract["schema_version"],
        "operation_contract": graph_structure_ops_output_contract(contract),
        "errors": _dedupe(global_errors),
        "accepted_count": len(accepted_hints),
        "deduped_count": deduped_count,
        "rejected_count": rejected_count,
        "conflict_count": len(conflict_errors),
        "operations": normalized,
        "normalized_hint_index": {
            "hint_count": len(accepted_hints),
            "hints": accepted_hints,
        },
    }


def dry_run_graph_structure_ops(
    payload: Mapping[str, Any],
    *,
    graph: Mapping[str, Any],
    inventory_paths: Iterable[str],
    snapshot_id: str = "",
    base_commit: str = "",
    operation_contract: Mapping[str, Any] | None = None,
    project_root: str | Path = "",
) -> dict[str, Any]:
    """Validate AI graph-structure ops and preview hint projection effects.

    Dry-run is intentionally state-only: it does not write hint comments,
    persist graph events, or mutate the graph DB. The returned projection
    summary is enough for dashboard/observer review before an accept step.
    """
    gate = validate_graph_structure_ops(
        payload,
        graph=graph,
        inventory_paths=inventory_paths,
        snapshot_id=snapshot_id,
        base_commit=base_commit,
        operation_contract=operation_contract,
        project_root=project_root,
    )
    if not gate["ok"]:
        return {
            "ok": False,
            "status": "failed",
            "gate": gate,
            "projection": _empty_projection_preview(),
        }

    projection = build_hint_projection(graph, gate["normalized_hint_index"])
    preview = _projection_preview(projection)
    return {
        "ok": preview["status"] == "ok",
        "status": "passed" if preview["status"] == "ok" else "failed",
        "gate": gate,
        "projection": preview,
    }


def run_graph_structure_ai_output_pipeline(
    *,
    raw_output: Any,
    mode: str = "dry_run",
    graph: Mapping[str, Any],
    inventory_paths: Iterable[str],
    snapshot_id: str = "",
    base_commit: str = "",
    project_root: str = "",
    operation_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run raw AI graph-structure output through parse, gate, and action.

    This is the worker-ready orchestration surface. It performs no model calls;
    callers pass the raw AI output they already obtained.
    """
    normalized_mode = str(mode or "dry_run").strip().lower().replace("-", "_")
    parsed = parse_graph_structure_ai_output(raw_output)
    if not parsed["ok"]:
        return {
            "ok": False,
            "status": "failed",
            "mode": normalized_mode,
            "accepted": False,
            "mutated": False,
            "parse": parsed,
        }
    if normalized_mode not in {"dry_run", "dryrun", "preview", "accept", "apply", "write"}:
        return {
            "ok": False,
            "status": "failed",
            "mode": normalized_mode,
            "accepted": False,
            "mutated": False,
            "parse": parsed,
            "errors": ["mode_unsupported"],
        }

    dry_run = dry_run_graph_structure_ops(
        parsed["payload"],
        graph=graph,
        inventory_paths=inventory_paths,
        snapshot_id=snapshot_id,
        base_commit=base_commit,
        operation_contract=operation_contract,
        project_root=project_root,
    )
    if normalized_mode in {"dry_run", "dryrun", "preview"} or not dry_run["ok"]:
        return {
            **dry_run,
            "mode": normalized_mode,
            "accepted": False,
            "mutated": False,
            "parse": parsed,
        }
    if not project_root:
        return {
            "ok": False,
            "status": "failed",
            "mode": normalized_mode,
            "accepted": False,
            "mutated": False,
            "parse": parsed,
            "gate": dry_run["gate"],
            "projection": dry_run["projection"],
            "errors": ["project_root_required"],
        }

    write_result = write_graph_structure_hints(
        project_root,
        dry_run["gate"]["normalized_hint_index"]["hints"],
    )
    return {
        "ok": write_result["ok"],
        "status": "passed" if write_result["ok"] else "failed",
        "mode": normalized_mode,
        "accepted": write_result["ok"],
        "mutated": write_result["written_count"] > 0,
        "requires_commit": write_result["written_count"] > 0,
        "update_graph_after_commit": write_result["written_count"] > 0,
        "parse": parsed,
        "gate": dry_run["gate"],
        "projection": dry_run["projection"],
        "write": write_result,
    }


def _validate_operation(
    op: Mapping[str, Any],
    *,
    index: int,
    graph_nodes: Mapping[str, Mapping[str, Any]],
    inventory_paths: set[str],
    seen_hint_ids: set[str],
    operation_contract: Mapping[str, Any],
    project_root: str | Path = "",
) -> dict[str, Any]:
    errors: list[str] = []
    normalizations: list[str] = []
    op_name = str(op.get("op") or "").strip()
    hint_id = str(op.get("hint_id") or "").strip()
    source_path = _norm_path(op.get("source_path"))
    target_node_id = str(op.get("target_node_id") or "").strip()
    role = str(op.get("role") or "").strip()
    edge = str(op.get("edge") or op.get("edge_type") or "").strip()

    operations = operation_contract.get("operations") if isinstance(operation_contract.get("operations"), Mapping) else {}
    spec = operations.get(op_name) if isinstance(operations.get(op_name), Mapping) else {}
    enabled = bool(spec.get("enabled")) and op_name in SUPPORTED_HINT_OPS
    required_fields = set(spec.get("required_fields") or [])
    role_allowlist = set(spec.get("role_allowlist") or [])
    edge_allowlist = set(spec.get("edge_allowlist") or [])

    if not enabled:
        errors.append("unsupported_op_for_hint_materialization")
    if not hint_id:
        errors.append("hint_id_missing")
    elif not _HINT_ID_RE.match(hint_id):
        errors.append("hint_id_invalid")
    elif hint_id in seen_hint_ids:
        errors.append("hint_id_duplicate")
    seen_hint_ids.add(hint_id)
    if enabled:
        if not source_path:
            errors.append("source_path_missing")
        elif source_path not in inventory_paths:
            errors.append("source_path_missing")
        if not target_node_id:
            errors.append("target_node_missing")
        elif target_node_id not in graph_nodes:
            errors.append("target_node_missing")
        if "role" in required_fields or op_name == "move_file":
            if role not in role_allowlist:
                errors.append("role_unsupported")
        if op_name == "add_edge" and edge == "calls":
            edge, evidence_note = _normalize_calls_edge_from_evidence(
                op,
                source_path=source_path,
                target_node=graph_nodes.get(target_node_id) or {},
                project_root=project_root,
                policy=operation_contract.get("evidence_policy") or {},
            )
            if evidence_note == "calls_import_only_rejected":
                errors.append(evidence_note)
            elif evidence_note:
                normalizations.append(evidence_note)
        if "edge" in required_fields or op_name in {"add_edge", "suppress_edge"}:
            if edge not in edge_allowlist:
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
        "normalizations": normalizations,
        "hint": hint,
    }


def _mark_duplicate_operations(
    operations: list[dict[str, Any]],
    operation_contract: Mapping[str, Any],
) -> int:
    policy = operation_contract.get("evidence_policy")
    if isinstance(policy, Mapping) and policy.get("dedupe_operations") is False:
        return 0
    seen: dict[tuple[str, str, str, str, str], str] = {}
    deduped_count = 0
    for entry in operations:
        if entry["status"] != "accepted" or entry["errors"]:
            continue
        key = (
            str(entry.get("op") or ""),
            str(entry.get("source_path") or ""),
            str(entry.get("target_node_id") or ""),
            str(entry.get("edge") or ""),
            str(entry.get("role") or ""),
        )
        first_hint_id = seen.get(key)
        if first_hint_id:
            entry["status"] = "deduped"
            entry["duplicate_of"] = first_hint_id
            entry["hint"]["status"] = "deduped"
            deduped_count += 1
            continue
        seen[key] = str(entry.get("hint_id") or "")
    return deduped_count


def _mark_conflicts(operations: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    move_targets: dict[tuple[str, str], set[str]] = {}
    edge_actions: dict[tuple[str, str, str], set[str]] = {}

    for entry in operations:
        if entry["status"] != "accepted" or entry["errors"]:
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


def _empty_projection_preview() -> dict[str, Any]:
    return {
        "status": "not_run",
        "materialized_count": 0,
        "conflict_count": 0,
        "effect_counts": {
            "edges_added": 0,
            "edges_suppressed": 0,
            "files_moved": 0,
        },
        "hint_states": {},
        "suppressed_edges": [],
    }


def _projection_preview(projection: Mapping[str, Any]) -> dict[str, Any]:
    hint_states = (
        projection.get("hint_states")
        if isinstance(projection.get("hint_states"), Mapping)
        else {}
    )
    counts = {
        "edges_added": 0,
        "edges_suppressed": 0,
        "files_moved": 0,
    }
    for state in hint_states.values():
        if not isinstance(state, Mapping):
            continue
        effect = state.get("effect") if isinstance(state.get("effect"), Mapping) else {}
        counts["edges_added"] += len(effect.get("edges_added") or [])
        counts["edges_suppressed"] += len(effect.get("edges_suppressed") or [])
        counts["files_moved"] += len(effect.get("files_moved") or [])
    return {
        "status": str(projection.get("status") or ""),
        "materialized_count": int(projection.get("materialized_count") or 0),
        "conflict_count": int(projection.get("conflict_count") or 0),
        "effect_counts": counts,
        "hint_states": dict(hint_states),
        "suppressed_edges": projection.get("suppressed_edges")
        if isinstance(projection.get("suppressed_edges"), list)
        else [],
    }


def _graph_node_ids(graph: Mapping[str, Any]) -> set[str]:
    return set(_graph_nodes_by_id(graph))


def _graph_nodes_by_id(graph: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    deps_graph = graph.get("deps_graph") if isinstance(graph.get("deps_graph"), Mapping) else {}
    nodes = deps_graph.get("nodes") if isinstance(deps_graph.get("nodes"), list) else []
    out: dict[str, Mapping[str, Any]] = {}
    for raw in nodes:
        node = raw if isinstance(raw, Mapping) else {}
        node_id = str(node.get("id") or "").strip()
        if node_id:
            out[node_id] = node
    return out


def _normalize_calls_edge_from_evidence(
    op: Mapping[str, Any],
    *,
    source_path: str,
    target_node: Mapping[str, Any],
    project_root: str | Path = "",
    policy: Mapping[str, Any],
) -> tuple[str, str]:
    edge = str(op.get("edge") or op.get("edge_type") or "").strip()
    calls_policy = policy.get("calls") if isinstance(policy.get("calls"), Mapping) else {}
    if not calls_policy or edge != "calls":
        return edge, ""
    evidence_kind = _evidence_kind(op.get("evidence"))
    if not evidence_kind:
        evidence_kind = _source_evidence_kind(
            project_root,
            source_path=source_path,
            target_node=target_node,
        )
    if evidence_kind != "import_only":
        return edge, ""
    action = str(calls_policy.get("import_only_action") or "downgrade").strip().lower()
    if action == "reject":
        return edge, "calls_import_only_rejected"
    if action == "downgrade":
        downgrade_to = str(calls_policy.get("downgrade_to") or "imports").strip()
        return downgrade_to, f"calls_import_only_downgraded_to_{downgrade_to}"
    return edge, ""


def _evidence_kind(evidence: Any) -> str:
    if not isinstance(evidence, Mapping):
        return ""
    return str(
        evidence.get("source_evidence")
        or evidence.get("evidence_kind")
        or evidence.get("kind")
        or ""
    ).strip().lower().replace("-", "_")


def _source_evidence_kind(
    project_root: str | Path,
    *,
    source_path: str,
    target_node: Mapping[str, Any],
) -> str:
    if not project_root or not source_path:
        return ""
    target = Path(project_root).resolve() / source_path
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    tokens = _target_reference_tokens(target_node)
    if not tokens:
        return ""
    import_lines: list[str] = []
    body_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            import_lines.append(stripped)
        else:
            body_lines.append(line)
    matching_imports = [
        line for line in import_lines
        if any(token in line for token in tokens)
    ]
    import_present = bool(matching_imports)
    if not import_present:
        return ""
    body = "\n".join(body_lines)
    call_tokens = tokens + _imported_symbol_tokens(matching_imports)
    call_present = any(
        re.search(rf"\b{re.escape(token)}\s*(?:\.|\()", body)
        for token in call_tokens
    )
    return "" if call_present else "import_only"


def _imported_symbol_tokens(import_lines: list[str]) -> list[str]:
    tokens: list[str] = []
    for line in import_lines:
        match = re.search(r"\bimport\s+(.+)$", line)
        if not match:
            continue
        for raw in match.group(1).split(","):
            token = raw.strip().split(" as ", 1)[0].strip()
            if token and token != "*" and token not in tokens:
                tokens.append(token)
    return tokens


def _target_reference_tokens(target_node: Mapping[str, Any]) -> list[str]:
    metadata = target_node.get("metadata") if isinstance(target_node.get("metadata"), Mapping) else {}
    raw_values = [
        target_node.get("title"),
        metadata.get("module"),
    ]
    tokens: list[str] = []
    for raw in raw_values:
        text = str(raw or "").strip()
        if not text:
            continue
        for candidate in (text, text.split(".")[-1]):
            if candidate and candidate not in tokens:
                tokens.append(candidate)
    return tokens


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


def _copy_evidence_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in policy.items():
        if isinstance(value, Mapping):
            out[str(key)] = _copy_evidence_policy(value)
        else:
            out[str(key)] = value
    return out


def _normalize_evidence_policy(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("graph_structure_ops.evidence_policy must be a mapping")
    policy = _copy_evidence_policy(_DEFAULT_EVIDENCE_POLICY)
    for key, value in raw.items():
        name = str(key or "").strip()
        if not name:
            continue
        if isinstance(value, Mapping) and isinstance(policy.get(name), Mapping):
            nested = dict(policy[name])
            nested.update({str(k): v for k, v in value.items()})
            policy[name] = nested
        else:
            policy[name] = value
    calls = policy.get("calls") if isinstance(policy.get("calls"), Mapping) else {}
    action = str(calls.get("import_only_action") or "downgrade").strip().lower()
    if action not in {"allow", "downgrade", "reject"}:
        raise ValueError("graph_structure_ops.evidence_policy.calls.import_only_action must be allow, downgrade, or reject")
    downgrade_to = str(calls.get("downgrade_to") or "imports").strip()
    if downgrade_to and downgrade_to not in EDGE_ALLOWLIST:
        raise ValueError("graph_structure_ops.evidence_policy.calls.downgrade_to must be a supported edge")
    calls["import_only_action"] = action
    calls["downgrade_to"] = downgrade_to
    calls["require_call_evidence"] = bool(calls.get("require_call_evidence", True))
    policy["calls"] = dict(calls)
    policy["dedupe_operations"] = bool(policy.get("dedupe_operations", True))
    return policy


def _validate_contract(contract: dict[str, Any]) -> dict[str, Any]:
    if contract.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("graph_structure_ops.schema_version must be graph_structure_ops.v1")
    if not contract.get("analyzer_role"):
        raise ValueError("graph_structure_ops.analyzer_role cannot be empty")
    operations = contract.get("operations")
    if not isinstance(operations, Mapping):
        raise ValueError("graph_structure_ops.operations must be a mapping")
    if not isinstance(contract.get("evidence_policy"), Mapping):
        raise ValueError("graph_structure_ops.evidence_policy must be a mapping")
    for name, spec in operations.items():
        if name not in SUPPORTED_HINT_OPS:
            raise ValueError(f"graph_structure_ops operation has no source_hint handler: {name}")
        if not isinstance(spec, Mapping):
            raise ValueError("graph_structure_ops operation specs must be mappings")
        if spec.get("materializer") != "source_hint":
            raise ValueError(f"graph_structure_ops.{name}.materializer must be source_hint")
        if not isinstance(spec.get("required_fields"), list):
            raise ValueError(f"graph_structure_ops.{name}.required_fields must be a list")
        if not isinstance(spec.get("role_allowlist"), list):
            raise ValueError(f"graph_structure_ops.{name}.role_allowlist must be a list")
        if not isinstance(spec.get("edge_allowlist"), list):
            raise ValueError(f"graph_structure_ops.{name}.edge_allowlist must be a list")
    return contract
