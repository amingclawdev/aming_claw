"""Bridge semantic graph suggestions into graph-structure gate jobs."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import graph_events
from . import graph_snapshot_store as store
from .graph_structure_ops import (
    ANALYZER_ROLE,
    EDGE_ALLOWLIST,
    GRAPH_STRUCTURE_SELF_PRECHECK_RULES,
    SCHEMA_VERSION,
    SUPPORTED_HINT_OPS,
)
from .graph_enrich_config_ops import (
    ANALYZER_ROLE as CONFIG_ANALYZER_ROLE,
    CONFIG_DOWNGRADE_TARGETS,
    CONFIG_EDGE_ALIASES,
    CONFIG_EDGE_ALLOWLIST,
    GRAPH_ENRICH_CONFIG_SELF_PRECHECK_RULES,
    CONFIG_RULE_OPS,
    SCHEMA_VERSION as CONFIG_SCHEMA_VERSION,
    SUPPORTED_ACTIONS as CONFIG_SUPPORTED_ACTIONS,
    SUPPORTED_OPS as CONFIG_SUPPORTED_OPS,
    SUPPORTED_SOURCE_EVIDENCE as CONFIG_SUPPORTED_SOURCE_EVIDENCE,
)


DIRECT_SUGGESTION_KEYS = (
    "graph_structure_ops",
    "graph_structure_suggestions",
    "graph_structure_candidates",
    "dependency_patch_suggestions",
    "open_issues",
    "health_issues",
)

CONFIG_SUGGESTION_KEYS = (
    "graph_enrich_config_ops",
    "graph_enrich_config_suggestions",
    "graph_enrich_config_candidates",
)

EDGE_KIND_ALIASES = {
    "add_depends_on": "depends_on",
    "add_dependency": "depends_on",
    "add_relation": "depends_on",
    "missing_relation": "depends_on",
    "typed_relation": "depends_on",
    "add_typed_relation": "depends_on",
    "add_edge": "depends_on",
    "depends_on": "depends_on",
    "dependency": "depends_on",
    "add_called_by": "calls",
    "called_by": "calls",
    "caller": "calls",
    "add_test_consumer": "tests",
    "add_test_binding": "tests",
    "test_binding": "tests",
    "tests": "tests",
    "add_doc_binding": "documents",
    "doc_binding": "documents",
    "documents": "documents",
    "add_config_binding": "configures",
    "config_binding": "configures",
    "configures": "configures",
    "import_module": "imports",
    "imports_module": "imports",
    "module_import": "imports",
    "imports": "imports",
    "calls": "calls",
    "uses": "uses",
}
_DEFAULT_BRIDGE_POLICY = {
    "calls": {
        "require_concrete_evidence": True,
        "weak_evidence_action": "downgrade",
        "downgrade_to": "imports",
        "evidence_kinds": [
            "call",
            "calls",
            "call_reference",
            "direct_call",
            "function_call",
            "function_calls",
            "resolved_call",
            "resolved_function_call",
            "runtime_call",
            "strong_call",
        ],
    }
}
_CALL_EXPRESSION_RE = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?\s*\("
)


def bridge_semantic_events_to_graph_structure_jobs(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    event_ids: Iterable[str] | None = None,
    node_ids: Iterable[str] | None = None,
    mode: str = "dry_run",
    actor: str = "semantic_graph_structure_bridge",
    limit: int = 100,
    project_root: str | Path = "",
    bridge_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Queue graph-structure dry-run jobs derived from semantic proposals.

    The bridge is intentionally conservative. It only converts suggestions that
    can be represented as source-hint-compatible graph_structure_ops.v1 output;
    everything else is preserved as an audited skip reason.
    """
    graph_events.ensure_schema(conn)
    snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        return {
            "ok": False,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "errors": ["snapshot_not_found"],
            "queued_count": 0,
            "skipped_count": 0,
            "events": [],
        }

    semantic_events = _select_semantic_events(
        conn,
        project_id,
        snapshot_id,
        event_ids=event_ids,
        node_ids=node_ids,
        limit=limit,
    )
    node_index = _node_index(conn, project_id, snapshot_id)
    inventory_paths = _inventory_paths(project_id, snapshot_id)
    commit_sha = str(snapshot.get("commit_sha") or "")
    effective_bridge_policy = _effective_bridge_policy(
        project_id,
        project_root=project_root,
        bridge_policy=bridge_policy,
    )
    queued: list[dict[str, Any]] = []
    audited_skips: list[dict[str, Any]] = []

    for semantic_event in semantic_events:
        raw_output = semantic_event_to_graph_structure_output(
            semantic_event,
            project_id=project_id,
            snapshot_id=snapshot_id,
            base_commit=commit_sha,
            node_index=node_index,
            inventory_paths=inventory_paths,
            bridge_policy=effective_bridge_policy,
        )
        skipped = raw_output.get("bridge", {}).get("skipped") or []
        audited_skips.extend([
            {
                "semantic_event_id": semantic_event.get("event_id", ""),
                **skip,
            }
            for skip in skipped
            if isinstance(skip, dict)
        ])
        operations = raw_output.get("operations") if isinstance(raw_output.get("operations"), list) else []
        if not operations:
            queued.append(_create_bridge_audit_event(
                conn,
                project_id,
                snapshot_id,
                semantic_event,
                raw_output,
                actor=actor,
            ))
            continue
        request = graph_events.create_event(
            conn,
            project_id,
            snapshot_id,
            event_id=_bridge_event_id("gsbridge", semantic_event, raw_output),
            event_type="graph_structure_requested",
            event_kind="semantic_job",
            target_type="node",
            target_id=str(semantic_event.get("target_id") or ""),
            status=graph_events.EVENT_STATUS_OBSERVED,
            operation_type="graph_structure",
            source_event_id=str(semantic_event.get("event_id") or ""),
            payload={
                "mode": _normalized_mode(mode),
                "ai_output": raw_output,
                "selector": {
                    "source_semantic_event_id": semantic_event.get("event_id", ""),
                    "source_node_id": semantic_event.get("target_id", ""),
                },
                "operator_request": {
                    "goal": "Dry-run graph_structure_ops candidates derived from semantic AI suggestions.",
                },
                "instructions": {
                    "source": "semantic_graph_structure_bridge",
                    "apply_policy": "observer_must_approve_before_accept",
                },
                "options": {
                    "bridge": raw_output.get("bridge", {}),
                    "converted_count": len(operations),
                    "skipped_count": len(skipped),
                },
            },
            evidence={
                "source": "semantic_graph_structure_bridge",
                "source_semantic_event_id": semantic_event.get("event_id", ""),
                "converted_count": len(operations),
                "skipped_count": len(skipped),
                "requires_gate": True,
                "requires_observer_approval": True,
            },
            created_by=actor,
        )
        queued.append(request)
    return {
        "ok": True,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "semantic_event_count": len(semantic_events),
        "queued_count": sum(
            1 for event in queued
            if event.get("event_type") == "graph_structure_requested"
        ),
        "audit_event_count": sum(
            1 for event in queued
            if event.get("event_type") == "graph_structure_completed"
        ),
        "skipped_count": len(audited_skips),
        "events": queued,
        "skipped": audited_skips,
    }


def bridge_semantic_events_to_graph_enrich_config_jobs(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    event_ids: Iterable[str] | None = None,
    node_ids: Iterable[str] | None = None,
    mode: str = "dry_run",
    actor: str = "semantic_graph_structure_bridge",
    limit: int = 100,
    project_root: str = "",
) -> dict[str, Any]:
    """Queue config-rule gate jobs derived from semantic AI proposals."""
    graph_events.ensure_schema(conn)
    snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        return {
            "ok": False,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "errors": ["snapshot_not_found"],
            "queued_count": 0,
            "skipped_count": 0,
            "events": [],
        }

    semantic_events = _select_semantic_events(
        conn,
        project_id,
        snapshot_id,
        event_ids=event_ids,
        node_ids=node_ids,
        limit=limit,
    )
    queued: list[dict[str, Any]] = []
    audited_skips: list[dict[str, Any]] = []
    for semantic_event in semantic_events:
        raw_output = semantic_event_to_graph_enrich_config_output(
            semantic_event,
            project_id=project_id,
            snapshot_id=snapshot_id,
        )
        skipped = raw_output.get("bridge", {}).get("skipped") or []
        audited_skips.extend([
            {
                "semantic_event_id": semantic_event.get("event_id", ""),
                **skip,
            }
            for skip in skipped
            if isinstance(skip, dict)
        ])
        operations = raw_output.get("operations") if isinstance(raw_output.get("operations"), list) else []
        if not operations:
            if skipped:
                queued.append(_create_config_bridge_audit_event(
                    conn,
                    project_id,
                    snapshot_id,
                    semantic_event,
                    raw_output,
                    actor=actor,
                ))
            continue
        payload: dict[str, Any] = {
            "mode": _normalized_mode(mode),
            "ai_output": raw_output,
            "selector": {
                "source_semantic_event_id": semantic_event.get("event_id", ""),
                "source_node_id": semantic_event.get("target_id", ""),
            },
            "operator_request": {
                "goal": "Dry-run graph_enrich_config_ops candidates derived from semantic AI suggestions.",
            },
            "instructions": {
                "source": "semantic_graph_structure_bridge",
                "apply_policy": "observer_must_approve_before_accept",
            },
            "options": {
                "bridge": raw_output.get("bridge", {}),
                "converted_count": len(operations),
                "skipped_count": len(skipped),
            },
        }
        if project_root:
            payload["project_root"] = project_root
        request = graph_events.create_event(
            conn,
            project_id,
            snapshot_id,
            event_id=_bridge_event_id("gecbridge", semantic_event, raw_output),
            event_type="graph_enrich_config_requested",
            event_kind="semantic_job",
            target_type="project",
            target_id=project_id,
            status=graph_events.EVENT_STATUS_OBSERVED,
            operation_type="graph_enrich_config",
            source_event_id=str(semantic_event.get("event_id") or ""),
            payload=payload,
            evidence={
                "source": "semantic_graph_structure_bridge",
                "source_semantic_event_id": semantic_event.get("event_id", ""),
                "converted_count": len(operations),
                "skipped_count": len(skipped),
                "requires_gate": True,
                "requires_observer_approval": True,
            },
            created_by=actor,
        )
        queued.append(request)
    return {
        "ok": True,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "semantic_event_count": len(semantic_events),
        "queued_count": sum(
            1 for event in queued
            if event.get("event_type") == "graph_enrich_config_requested"
        ),
        "audit_event_count": sum(
            1 for event in queued
            if event.get("event_type") == "graph_enrich_config_completed"
        ),
        "skipped_count": len(audited_skips),
        "events": queued,
        "skipped": audited_skips,
    }


def semantic_event_to_graph_structure_output(
    semantic_event: Mapping[str, Any],
    *,
    project_id: str,
    snapshot_id: str,
    base_commit: str,
    node_index: Mapping[str, Mapping[str, Any]],
    inventory_paths: set[str],
    bridge_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    semantic_payload = _semantic_payload(semantic_event)
    source_node_id = str(semantic_event.get("target_id") or semantic_payload.get("node_id") or "").strip()
    source_node = node_index.get(source_node_id) or {}
    suggestions = _extract_suggestions(semantic_payload)
    operations: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    effective_bridge_policy = _normalize_bridge_policy(bridge_policy)
    for index, suggestion in enumerate(suggestions):
        converted = _convert_suggestion(
            suggestion,
            index=index,
            semantic_event=semantic_event,
            source_node=source_node,
            node_index=node_index,
            inventory_paths=inventory_paths,
            bridge_policy=effective_bridge_policy,
        )
        if converted.get("operation"):
            operations.append(converted["operation"])
        else:
            skipped.append({
                "index": index,
                "reason": converted.get("reason") or "unsupported_suggestion",
                "suggestion": converted.get("suggestion", suggestion),
            })
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "snapshot_id": snapshot_id,
            "base_commit": base_commit,
            "analyzer_role": ANALYZER_ROLE,
            "bridge": "semantic_graph_structure_bridge",
            "source_semantic_event_id": semantic_event.get("event_id", ""),
            "source_node_id": source_node_id,
        },
        "operations": operations,
        "self_check": {
            "valid": True,
            "checked_rules": list(GRAPH_STRUCTURE_SELF_PRECHECK_RULES),
            "precheck_status": "passed" if operations else "no_ops",
            "repair_attempts": 0,
            "max_repair_attempts": 1,
            "known_risks": [
                skip.get("reason", "")
                for skip in skipped
                if str(skip.get("reason") or "").strip()
            ][:20],
        },
        "bridge": {
            "source": "semantic_graph_structure_bridge",
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "source_semantic_event_id": semantic_event.get("event_id", ""),
            "source_node_id": source_node_id,
            "converted_count": len(operations),
            "skipped_count": len(skipped),
            "skipped": skipped,
            "policy": _bridge_policy_summary(effective_bridge_policy),
            "self_precheck": {
                "status": "passed" if operations else "no_ops",
                "checked_rules": list(GRAPH_STRUCTURE_SELF_PRECHECK_RULES),
                "repair_attempts": 0,
                "max_repair_attempts": 1,
            },
        },
    }


def semantic_event_to_graph_enrich_config_output(
    semantic_event: Mapping[str, Any],
    *,
    project_id: str,
    snapshot_id: str,
) -> dict[str, Any]:
    semantic_payload = _semantic_payload(semantic_event)
    suggestions = _extract_config_suggestions(semantic_payload)
    operations: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_rule_ids: set[str] = set()
    for index, suggestion in enumerate(suggestions):
        converted = _convert_config_suggestion(
            suggestion,
            index=index,
            semantic_event=semantic_event,
        )
        if converted.get("operation"):
            operation = converted["operation"]
            rule_id = str(operation.get("rule_id") or "").strip()
            if rule_id and rule_id in seen_rule_ids:
                skipped.append({
                    "index": index,
                    "reason": "rule_id_duplicate_deduped",
                    "suggestion": suggestion,
                    "operation": operation,
                })
                continue
            if rule_id:
                seen_rule_ids.add(rule_id)
            operations.append(operation)
        else:
            skipped.append({
                "index": index,
                "reason": converted.get("reason") or "unsupported_config_suggestion",
                "suggestion": converted.get("suggestion", suggestion),
            })
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "source": {
            "snapshot_id": snapshot_id,
            "analyzer_role": CONFIG_ANALYZER_ROLE,
            "bridge": "semantic_graph_structure_bridge",
            "source_semantic_event_id": semantic_event.get("event_id", ""),
            "source_node_id": semantic_event.get("target_id", ""),
        },
        "operations": operations,
        "self_check": {
            "valid": True,
            "checked_rules": list(GRAPH_ENRICH_CONFIG_SELF_PRECHECK_RULES),
            "precheck_status": "passed" if operations else "no_ops",
            "repair_attempts": 0,
            "max_repair_attempts": 1,
            "known_risks": [
                skip.get("reason", "")
                for skip in skipped
                if str(skip.get("reason") or "").strip()
            ][:20],
        },
        "bridge": {
            "source": "semantic_graph_structure_bridge",
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "source_semantic_event_id": semantic_event.get("event_id", ""),
            "source_node_id": semantic_event.get("target_id", ""),
            "converted_count": len(operations),
            "skipped_count": len(skipped),
            "skipped": skipped,
            "self_precheck": {
                "status": "passed" if operations else "no_ops",
                "checked_rules": list(GRAPH_ENRICH_CONFIG_SELF_PRECHECK_RULES),
                "repair_attempts": 0,
                "max_repair_attempts": 1,
            },
        },
    }


def _select_semantic_events(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    event_ids: Iterable[str] | None,
    node_ids: Iterable[str] | None,
    limit: int,
) -> list[dict[str, Any]]:
    ids = [str(item or "").strip() for item in (event_ids or []) if str(item or "").strip()]
    nodes = {str(item or "").strip() for item in (node_ids or []) if str(item or "").strip()}
    if ids:
        events = [
            graph_events.get_event(conn, project_id, snapshot_id, event_id)
            for event_id in ids
        ]
        selected = [event for event in events if event and event.get("event_type") == "semantic_node_enriched"]
    else:
        selected = graph_events.list_events(
            conn,
            project_id,
            snapshot_id,
            event_types=["semantic_node_enriched"],
            statuses=[
                graph_events.EVENT_STATUS_PROPOSED,
                graph_events.EVENT_STATUS_OBSERVED,
                graph_events.EVENT_STATUS_ACCEPTED,
            ],
            limit=limit,
        )
    if nodes:
        selected = [
            event for event in selected
            if str(event.get("target_id") or "").strip() in nodes
        ]
    return selected[: max(1, min(int(limit or 100), 1000))]


def _node_index(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> dict[str, dict[str, Any]]:
    rows = store.list_graph_snapshot_nodes(
        conn,
        project_id,
        snapshot_id,
        include_semantic=False,
        limit=1000,
    )
    return {
        str(row.get("node_id") or ""): row
        for row in rows
        if str(row.get("node_id") or "")
    }


def _inventory_paths(project_id: str, snapshot_id: str) -> set[str]:
    path = store.snapshot_companion_dir(project_id, snapshot_id) / "file_inventory.json"
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        rows = []
    paths: set[str] = set()
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, Mapping):
                rel = _norm_path(row.get("path"))
                if rel:
                    paths.add(rel)
    return paths


def _semantic_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    semantic_payload = payload.get("semantic_payload")
    if isinstance(semantic_payload, Mapping):
        return dict(semantic_payload)
    return dict(payload)


def _extract_suggestions(semantic_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in DIRECT_SUGGESTION_KEYS:
        raw = semantic_payload.get(key)
        if key == "graph_structure_ops" and isinstance(raw, Mapping):
            raw_ops = raw.get("operations")
            if isinstance(raw_ops, list):
                raw = raw_ops
            else:
                continue
        for item in _coerce_suggestion_list(raw):
            if isinstance(item, dict):
                item = dict(item)
                item.setdefault("_semantic_suggestion_source", key)
                out.append(item)
    return out


def _extract_config_suggestions(semantic_payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in CONFIG_SUGGESTION_KEYS:
        raw = semantic_payload.get(key)
        if key == "graph_enrich_config_ops" and isinstance(raw, Mapping):
            raw_ops = raw.get("operations")
            if isinstance(raw_ops, list):
                raw = raw_ops
            else:
                continue
        for item in _coerce_suggestion_list(raw):
            if isinstance(item, dict):
                item = dict(item)
                item.setdefault("_semantic_suggestion_source", key)
                out.append(item)
    return out


def _coerce_suggestion_list(raw: Any) -> list[Any]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, tuple):
        values = list(raw)
    else:
        values = [raw]
    out: list[Any] = []
    for item in values:
        if isinstance(item, str):
            parsed = _parse_structured_string(item)
            if isinstance(parsed, list):
                out.extend(parsed)
            else:
                out.append(parsed)
        else:
            out.append(item)
    return out


def _effective_bridge_policy(
    project_id: str,
    *,
    project_root: str | Path = "",
    bridge_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if bridge_policy is not None:
        return _normalize_bridge_policy(bridge_policy)
    try:
        from .reconcile_semantic_config import (
            apply_project_ai_routing,
            load_semantic_enrichment_config,
        )

        root = _bridge_project_root(project_id, project_root)
        config = apply_project_ai_routing(
            load_semantic_enrichment_config(project_root=root),
            project_id=project_id,
        )
        return _normalize_bridge_policy(config.graph_structure_ops.bridge_policy)
    except Exception:
        return _normalize_bridge_policy({})


def _bridge_project_root(project_id: str, project_root: str | Path = "") -> Path:
    if project_root:
        return Path(project_root).resolve()
    try:
        from . import project_service

        for project in project_service.list_projects():
            if project.get("project_id") == project_id and project.get("workspace_path"):
                return Path(str(project["workspace_path"])).resolve()
    except Exception:
        pass
    if project_id == "aming-claw":
        return Path(__file__).resolve().parents[2]
    return Path.cwd()


def _normalize_bridge_policy(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    policy = _deep_merge_mapping(_DEFAULT_BRIDGE_POLICY, raw or {})
    calls = policy.get("calls") if isinstance(policy.get("calls"), Mapping) else {}
    action = (
        str(calls.get("weak_evidence_action") or "downgrade")
        .strip()
        .lower()
        .replace("-", "_")
    )
    action = {
        "allow": "keep",
        "allowed": "keep",
        "keep": "keep",
        "downgrade": "downgrade",
        "reject": "skip",
        "skip": "skip",
    }.get(action, action)
    downgrade_to = (
        str(calls.get("downgrade_to") or "imports")
        .strip()
        .lower()
        .replace("-", "_")
    )
    if action not in {"keep", "downgrade", "skip"}:
        action = "downgrade"
    if downgrade_to not in EDGE_ALLOWLIST:
        downgrade_to = "imports"
    evidence_kinds = calls.get("evidence_kinds") or []
    if isinstance(evidence_kinds, str):
        evidence_kinds = [item.strip() for item in evidence_kinds.split(",")]
    if not isinstance(evidence_kinds, list):
        evidence_kinds = []
    calls = {
        **calls,
        "require_concrete_evidence": bool(calls.get("require_concrete_evidence", True)),
        "weak_evidence_action": action,
        "downgrade_to": downgrade_to,
        "evidence_kinds": [
            _normalize_policy_token(item)
            for item in evidence_kinds
            if _normalize_policy_token(item)
        ],
    }
    policy["calls"] = calls
    return policy


def _deep_merge_mapping(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key, value in base.items():
        merged[str(key)] = _deep_merge_mapping(value, {}) if isinstance(value, Mapping) else value
    for key, value in override.items():
        name = str(key)
        if isinstance(value, Mapping) and isinstance(merged.get(name), Mapping):
            merged[name] = _deep_merge_mapping(merged[name], value)
        else:
            merged[name] = value
    return merged


def _bridge_policy_summary(policy: Mapping[str, Any]) -> dict[str, Any]:
    calls = policy.get("calls") if isinstance(policy.get("calls"), Mapping) else {}
    return {
        "calls": {
            "require_concrete_evidence": bool(calls.get("require_concrete_evidence", True)),
            "weak_evidence_action": str(calls.get("weak_evidence_action") or "downgrade"),
            "downgrade_to": str(calls.get("downgrade_to") or "imports"),
            "evidence_kind_count": len(calls.get("evidence_kinds") or []),
        }
    }


def _apply_bridge_edge_policy(
    edge: str,
    raw: Mapping[str, Any],
    *,
    op_name: str,
    bridge_policy: Mapping[str, Any],
) -> dict[str, Any]:
    if op_name != "add_edge" or edge != "calls":
        return {"edge": edge}
    calls = bridge_policy.get("calls") if isinstance(bridge_policy.get("calls"), Mapping) else {}
    if not bool(calls.get("require_concrete_evidence", True)):
        return {"edge": edge, "note": "calls_concrete_evidence_not_required"}
    has_evidence, evidence_kind = _has_concrete_call_evidence(raw, calls)
    if has_evidence:
        return {
            "edge": edge,
            "note": "calls_concrete_evidence_present",
            "source_evidence": evidence_kind or "call_reference",
        }
    action = str(calls.get("weak_evidence_action") or "downgrade")
    if action == "keep":
        return {
            "edge": edge,
            "note": "calls_weak_evidence_kept_by_policy",
            "source_evidence": "weak_call_evidence",
        }
    if action == "skip":
        return {"edge": edge, "skip_reason": "calls_weak_evidence_skipped"}
    downgrade_to = str(calls.get("downgrade_to") or "imports")
    if downgrade_to not in EDGE_ALLOWLIST:
        downgrade_to = "imports"
    return {
        "edge": downgrade_to,
        "note": f"calls_weak_evidence_downgraded_to_{downgrade_to}",
        "source_evidence": "missing_concrete_call_evidence",
        "original_edge": "calls",
    }


def _has_concrete_call_evidence(
    raw: Mapping[str, Any],
    calls_policy: Mapping[str, Any],
) -> tuple[bool, str]:
    evidence_kind = _bridge_evidence_kind(raw)
    allowed = {
        _normalize_policy_token(item)
        for item in (calls_policy.get("evidence_kinds") or [])
        if _normalize_policy_token(item)
    }
    if evidence_kind and evidence_kind in allowed:
        return True, evidence_kind
    for key in ("call_site", "callsite", "call_expression", "callee", "callee_symbol"):
        if str(raw.get(key) or "").strip():
            return True, evidence_kind or "call_reference"
    text = _bridge_evidence_text(raw)
    if text and _CALL_EXPRESSION_RE.search(text):
        return True, evidence_kind or "call_reference"
    return False, evidence_kind


def _bridge_evidence_kind(raw: Mapping[str, Any]) -> str:
    for key in (
        "source_evidence",
        "evidence_kind",
        "kind_of_evidence",
        "evidence_type",
        "source_evidence_kind",
        "call_evidence_kind",
    ):
        value = _normalize_policy_token(raw.get(key))
        if value:
            return value
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), Mapping) else {}
    for key in (
        "source_evidence",
        "evidence_kind",
        "kind_of_evidence",
        "evidence_type",
        "kind",
        "type",
    ):
        value = _normalize_policy_token(evidence.get(key))
        if value:
            return value
    return ""


def _bridge_evidence_text(raw: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key in (
        "call_evidence",
        "evidence",
        "line_evidence",
        "source_ref",
        "details",
        "detail",
        "reason",
        "summary",
        "rationale",
    ):
        value = raw.get(key)
        if isinstance(value, Mapping):
            for nested_key in (
                "evidence",
                "line_evidence",
                "source_ref",
                "details",
                "detail",
                "reason",
                "summary",
            ):
                nested = value.get(nested_key)
                if nested is not None:
                    parts.append(str(nested))
        elif value is not None:
            parts.append(str(value))
    return "\n".join(part for part in parts if part.strip())


def _operation_evidence(
    raw: Mapping[str, Any],
    semantic_event: Mapping[str, Any],
    *,
    fallback_reason: str,
) -> dict[str, Any]:
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), Mapping) else {}
    out = dict(evidence)
    if not out.get("reason") and fallback_reason:
        out["reason"] = fallback_reason
    raw_evidence = raw.get("evidence")
    if raw_evidence is not None and not isinstance(raw_evidence, Mapping):
        out.setdefault("evidence", str(raw_evidence))
    out.setdefault("semantic_suggestion_source", raw.get("_semantic_suggestion_source", ""))
    out.setdefault("source_semantic_event_id", semantic_event.get("event_id", ""))
    return out


def _annotate_bridge_evidence(
    evidence: dict[str, Any],
    edge_policy: Mapping[str, Any],
) -> None:
    note = str(edge_policy.get("note") or "").strip()
    if note:
        evidence["bridge_policy"] = note
    source_evidence = str(edge_policy.get("source_evidence") or "").strip()
    if source_evidence:
        evidence.setdefault("source_evidence", source_evidence)
    original_edge = str(edge_policy.get("original_edge") or "").strip()
    if original_edge:
        evidence.setdefault("original_edge", original_edge)


def _normalize_policy_token(value: Any) -> str:
    return (
        str(value or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(".", "_")
        .replace(" ", "_")
    )


def _parse_structured_string(value: str) -> Any:
    text = str(value or "").strip()
    if not text:
        return {}
    if text[0] not in "[{":
        return {"summary": text}
    try:
        return json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    try:
        parsed = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return {"summary": text, "_parse_error": "structured_string_invalid"}
    return parsed if isinstance(parsed, (dict, list)) else {"summary": text}


def _convert_suggestion(
    suggestion: Mapping[str, Any],
    *,
    index: int,
    semantic_event: Mapping[str, Any],
    source_node: Mapping[str, Any],
    node_index: Mapping[str, Mapping[str, Any]],
    inventory_paths: set[str],
    bridge_policy: Mapping[str, Any],
) -> dict[str, Any]:
    raw = dict(suggestion)
    if raw.get("_parse_error"):
        return {"reason": "suggestion_parse_error", "suggestion": raw}
    op = str(raw.get("op") or "").strip()
    if op in SUPPORTED_HINT_OPS:
        direct = _normalize_direct_operation(
            raw,
            index=index,
            semantic_event=semantic_event,
            source_node=source_node,
            node_index=node_index,
            inventory_paths=inventory_paths,
            bridge_policy=bridge_policy,
        )
        return direct

    kind = _suggestion_kind(raw)
    edge = _edge_from_suggestion(raw, kind)
    if not edge:
        return {"reason": "unsupported_suggestion_kind", "suggestion": raw}
    edge_policy = _apply_bridge_edge_policy(
        edge,
        raw,
        op_name="add_edge",
        bridge_policy=bridge_policy,
    )
    if edge_policy.get("skip_reason"):
        return {"reason": edge_policy["skip_reason"], "suggestion": raw}
    edge = str(edge_policy.get("edge") or edge)
    source_path = _resolve_source_path(raw, source_node, inventory_paths, node_index=node_index, edge=edge)
    if not source_path:
        return {"reason": "source_path_unresolved", "suggestion": raw}
    target_node_id = _resolve_target_node(raw, semantic_event, node_index, edge=edge)
    if not target_node_id:
        return {"reason": "target_node_unresolved", "suggestion": raw}
    evidence = _operation_evidence(
        raw,
        semantic_event,
        fallback_reason=_reason(raw) or f"semantic suggestion {kind}",
    )
    _annotate_bridge_evidence(evidence, edge_policy)
    return {
        "operation": {
            "op": "add_edge",
            "hint_id": _hint_id(semantic_event, index, raw),
            "source_path": source_path,
            "target_node_id": target_node_id,
            "edge": edge,
            "confidence": _confidence(raw),
            "evidence": evidence,
        }
    }


def _convert_config_suggestion(
    suggestion: Mapping[str, Any],
    *,
    index: int,
    semantic_event: Mapping[str, Any],
) -> dict[str, Any]:
    raw = dict(suggestion)
    if raw.get("_parse_error"):
        return {"reason": "suggestion_parse_error", "suggestion": raw}
    op = str(
        raw.get("op")
        or raw.get("operation")
        or raw.get("kind")
        or ""
    ).strip()
    if op not in CONFIG_SUPPORTED_OPS:
        return {"reason": "unsupported_config_op", "suggestion": raw}
    is_rule_op = op in CONFIG_RULE_OPS
    edge = _normalize_config_token(raw.get("edge") or raw.get("edge_type"))
    source_evidence = str(
        raw.get("source_evidence")
        or raw.get("evidence_kind")
        or raw.get("kind_of_evidence")
        or ""
    ).strip().lower().replace("-", "_").replace(".", "_")
    action = _normalize_config_action(raw.get("action") or raw.get("import_only_action"))
    downgrade_to = _normalize_config_token(raw.get("downgrade_to"))
    if action == "downgrade" and downgrade_to in {"ignore", "ignored"}:
        action = "ignore"
        downgrade_to = ""
    if edge not in CONFIG_EDGE_ALLOWLIST:
        return {"reason": "edge_unsupported", "suggestion": raw}
    if not source_evidence:
        return {"reason": "source_evidence_missing", "suggestion": raw}
    if source_evidence not in CONFIG_SUPPORTED_SOURCE_EVIDENCE and not is_rule_op:
        return {"reason": "source_evidence_unsupported", "suggestion": raw}
    if not action:
        return {"reason": "action_missing", "suggestion": raw}
    if action not in CONFIG_SUPPORTED_ACTIONS and not is_rule_op:
        return {"reason": "action_unsupported", "suggestion": raw}
    if action == "downgrade" and downgrade_to not in CONFIG_DOWNGRADE_TARGETS and not is_rule_op:
        return {"reason": "downgrade_to_unsupported", "suggestion": raw}
    operation = {
        "op": op,
        "rule_id": str(raw.get("rule_id") or _hint_id(semantic_event, index, raw)).strip(),
        "edge": edge,
        "source_evidence": source_evidence,
        "action": action,
        "confidence": _confidence(raw),
        "evidence": raw.get("evidence") if isinstance(raw.get("evidence"), Mapping) else {
            "reason": _reason(raw),
            "semantic_suggestion_source": raw.get("_semantic_suggestion_source", ""),
            "source_semantic_event_id": semantic_event.get("event_id", ""),
        },
    }
    if downgrade_to:
        operation["downgrade_to"] = downgrade_to
    return {"operation": operation}


def _normalize_direct_operation(
    raw: Mapping[str, Any],
    *,
    index: int,
    semantic_event: Mapping[str, Any],
    source_node: Mapping[str, Any],
    node_index: Mapping[str, Mapping[str, Any]],
    inventory_paths: set[str],
    bridge_policy: Mapping[str, Any],
) -> dict[str, Any]:
    op = str(raw.get("op") or "").strip()
    source_path = _norm_path(raw.get("source_path") or raw.get("path") or raw.get("file"))
    if not source_path:
        source_path = _resolve_source_path(raw, source_node, inventory_paths, node_index=node_index)
    if source_path not in inventory_paths:
        return {"reason": "source_path_unresolved", "suggestion": dict(raw)}
    target_node_id = str(raw.get("target_node_id") or "").strip()
    if target_node_id not in node_index:
        target_node_id = _resolve_target_node(raw, semantic_event, node_index)
    if not target_node_id:
        return {"reason": "target_node_unresolved", "suggestion": dict(raw)}
    operation = {
        "op": op,
        "hint_id": str(raw.get("hint_id") or _hint_id(semantic_event, index, raw)).strip(),
        "source_path": source_path,
        "target_node_id": target_node_id,
        "confidence": _confidence(raw),
        "evidence": _operation_evidence(
            raw,
            semantic_event,
            fallback_reason=_reason(raw),
        ),
    }
    if op == "move_file":
        role = str(raw.get("role") or "").strip()
        if not role:
            return {"reason": "role_unresolved", "suggestion": dict(raw)}
        operation["role"] = role
    if op in {"add_edge", "suppress_edge"}:
        edge = _edge_from_suggestion(raw, _suggestion_kind(raw))
        if not edge:
            return {"reason": "edge_unresolved", "suggestion": dict(raw)}
        edge_policy = _apply_bridge_edge_policy(
            edge,
            raw,
            op_name=op,
            bridge_policy=bridge_policy,
        )
        if edge_policy.get("skip_reason"):
            return {"reason": edge_policy["skip_reason"], "suggestion": dict(raw)}
        edge = str(edge_policy.get("edge") or edge)
        _annotate_bridge_evidence(operation["evidence"], edge_policy)
        operation["edge"] = edge
    return {"operation": operation}


def _suggestion_kind(raw: Mapping[str, Any]) -> str:
    return str(
        raw.get("kind")
        or raw.get("type")
        or raw.get("issue_type")
        or raw.get("category")
        or raw.get("edge_type")
        or raw.get("relation_type")
        or ""
    ).strip().lower()


def _edge_from_suggestion(raw: Mapping[str, Any], kind: str = "") -> str:
    edge = (
        str(raw.get("edge") or raw.get("edge_type") or raw.get("relation_type") or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(".", "_")
        .replace(" ", "_")
    )
    if edge in EDGE_ALLOWLIST:
        return edge
    normalized = EDGE_KIND_ALIASES.get(edge) or EDGE_KIND_ALIASES.get(kind)
    return normalized if normalized in EDGE_ALLOWLIST else ""


def _normalize_config_token(value: Any) -> str:
    token = (
        str(value or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(".", "_")
        .replace(" ", "_")
    )
    return CONFIG_EDGE_ALIASES.get(token, token)


def _normalize_config_action(value: Any) -> str:
    action = _normalize_config_token(value)
    if action == "ignored":
        return "ignore"
    return action


def _resolve_source_path(
    raw: Mapping[str, Any],
    source_node: Mapping[str, Any],
    inventory_paths: set[str],
    *,
    node_index: Mapping[str, Mapping[str, Any]] | None = None,
    edge: str = "",
) -> str:
    for key in ("source_path", "path", "file", "source_file", "test_path", "doc_path", "config_path"):
        path = _norm_path(raw.get(key))
        if path and path in inventory_paths:
            return path
    explicit_source_seen = False
    if node_index:
        for key in ("source_node_id", "source_module", "source", "src", "from", "caller", "origin"):
            value = raw.get(key)
            text = str(value or "").strip()
            if not text:
                continue
            explicit_source_seen = True
            path = _norm_path(value)
            if path and path in inventory_paths:
                return path
            node_id = _resolve_node_alias(value, node_index)
            if not node_id:
                continue
            for candidate in _node_paths(node_index[node_id], "primary_files", "primary"):
                if candidate in inventory_paths:
                    return candidate
    for key in ("paths", "files"):
        values = raw.get(key)
        if isinstance(values, (list, tuple)):
            for value in values:
                path = _norm_path(value)
                if path and path in inventory_paths:
                    return path
    if explicit_source_seen:
        return ""
    if edge in {"tests", "documents", "configures"}:
        return ""
    for path in _node_paths(source_node, "primary_files", "primary"):
        if path in inventory_paths:
            return path
    return ""


def _resolve_target_node(
    raw: Mapping[str, Any],
    semantic_event: Mapping[str, Any],
    node_index: Mapping[str, Mapping[str, Any]],
    *,
    edge: str = "",
) -> str:
    target_keys = (
        "target_node_id",
        "destination_node_id",
        "target",
        "target_id",
        "dst",
        "to",
    )
    for key in target_keys:
        resolved = _resolve_node_alias(raw.get(key), node_index)
        if resolved:
            return resolved
    if edge in {"tests", "documents", "configures"}:
        event_target = str(semantic_event.get("target_id") or "").strip()
        if event_target in node_index:
            return event_target
    return ""


def _resolve_node_alias(value: Any, node_index: Mapping[str, Mapping[str, Any]]) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text in node_index:
        return text
    lowered = text.lower()
    for node_id, node in node_index.items():
        if node_id.lower() == lowered:
            return node_id
        if str(node.get("title") or "").strip().lower() == lowered:
            return node_id
        metadata = node.get("metadata") if isinstance(node.get("metadata"), Mapping) else {}
        if str(metadata.get("module") or "").strip().lower() == lowered:
            return node_id
        for path in _node_paths(node, "primary_files", "secondary_files", "test_files"):
            if path.lower() == lowered:
                return node_id
    return ""


def _node_paths(node: Mapping[str, Any], *keys: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for key in keys:
        raw = node.get(key)
        if raw is None:
            continue
        values = raw if isinstance(raw, (list, tuple, set)) else [raw]
        for value in values:
            path = _norm_path(value)
            if path and path not in seen:
                out.append(path)
                seen.add(path)
    return out


def _create_bridge_audit_event(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    semantic_event: Mapping[str, Any],
    raw_output: Mapping[str, Any],
    *,
    actor: str,
) -> dict[str, Any]:
    bridge = raw_output.get("bridge") if isinstance(raw_output.get("bridge"), Mapping) else {}
    return graph_events.create_event(
        conn,
        project_id,
        snapshot_id,
        event_id=_bridge_event_id("gsbridge-noop", semantic_event, raw_output),
        event_type="graph_structure_completed",
        event_kind="semantic_job",
        target_type="node",
        target_id=str(semantic_event.get("target_id") or ""),
        status=graph_events.EVENT_STATUS_MATERIALIZED,
        operation_type="graph_structure",
        source_event_id=str(semantic_event.get("event_id") or ""),
        payload={
            "result": {
                "ok": True,
                "status": "skipped",
                "mode": "dry_run",
                "accepted": False,
                "mutated": False,
                "converted_count": 0,
                "skipped": bridge.get("skipped") or [],
            },
            "bridge": bridge,
        },
        evidence={
            "source": "semantic_graph_structure_bridge",
            "source_semantic_event_id": semantic_event.get("event_id", ""),
            "converted_count": 0,
            "skipped_count": int(bridge.get("skipped_count") or 0),
        },
        created_by=actor,
    )


def _create_config_bridge_audit_event(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    semantic_event: Mapping[str, Any],
    raw_output: Mapping[str, Any],
    *,
    actor: str,
) -> dict[str, Any]:
    bridge = raw_output.get("bridge") if isinstance(raw_output.get("bridge"), Mapping) else {}
    return graph_events.create_event(
        conn,
        project_id,
        snapshot_id,
        event_id=_bridge_event_id("gecbridge-noop", semantic_event, raw_output),
        event_type="graph_enrich_config_completed",
        event_kind="semantic_job",
        target_type="project",
        target_id=project_id,
        status=graph_events.EVENT_STATUS_MATERIALIZED,
        operation_type="graph_enrich_config",
        source_event_id=str(semantic_event.get("event_id") or ""),
        payload={
            "result": {
                "ok": True,
                "status": "skipped",
                "mode": "dry_run",
                "accepted": False,
                "mutated": False,
                "converted_count": 0,
                "skipped": bridge.get("skipped") or [],
            },
            "bridge": bridge,
        },
        evidence={
            "source": "semantic_graph_structure_bridge",
            "source_semantic_event_id": semantic_event.get("event_id", ""),
            "converted_count": 0,
            "skipped_count": int(bridge.get("skipped_count") or 0),
        },
        created_by=actor,
    )


def _bridge_event_id(prefix: str, semantic_event: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    seed = {
        "source_event_id": semantic_event.get("event_id", ""),
        "target_id": semantic_event.get("target_id", ""),
        "operations": payload.get("operations") or [],
        "skipped": (payload.get("bridge") or {}).get("skipped") if isinstance(payload.get("bridge"), Mapping) else [],
    }
    digest = hashlib.sha256(json.dumps(seed, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
    raw = f"{prefix}-{semantic_event.get('event_id', '')}-{digest}"
    safe = re.sub(r"[^A-Za-z0-9_.:-]+", "-", raw).strip("-._:")
    return safe[:120] or f"{prefix}-{digest}"


def _hint_id(semantic_event: Mapping[str, Any], index: int, suggestion: Mapping[str, Any]) -> str:
    raw = {
        "source_event_id": semantic_event.get("event_id", ""),
        "target_id": semantic_event.get("target_id", ""),
        "index": index,
        "suggestion": suggestion,
    }
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
    seed = f"semantic-bridge-{semantic_event.get('target_id', '')}-{index}-{digest}"
    return re.sub(r"[^A-Za-z0-9_.:-]+", "-", seed).strip("-._:")[:96]


def _normalized_mode(mode: str) -> str:
    value = str(mode or "dry_run").strip().lower().replace("-", "_")
    return value if value in {"dry_run", "dryrun", "preview"} else "dry_run"


def _confidence(raw: Mapping[str, Any]) -> float:
    try:
        value = float(raw.get("confidence") if raw.get("confidence") is not None else 0.5)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, value))


def _reason(raw: Mapping[str, Any]) -> str:
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), Mapping) else {}
    return str(
        raw.get("reason")
        or raw.get("summary")
        or raw.get("rationale")
        or raw.get("issue")
        or evidence.get("reason")
        or evidence.get("summary")
        or ""
    ).strip()


def _norm_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip("/")
