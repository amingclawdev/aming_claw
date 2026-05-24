"""Shared Asset Inbox contract helpers.

The Asset Inbox is a read model for graph/file hygiene assets. It is not a
backlog table. Backlog rows are created only from selected actionable assets.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "asset_inbox.v1"

ASSET_STATUSES = {
    "source_orphan",
    "doc_unbound",
    "doc_candidate",
    "accepted",
    "test_candidate",
    "config_pending_decision",
    "ignored",
    "archive",
    "stale",
}

ASSET_KINDS = {
    "source",
    "doc",
    "index_doc",
    "test",
    "config",
    "generated",
    "unknown",
}

BATCH_ACTIONS = {
    "queue_asset_binding_proposals",
    "queue_semantic_enrich",
    "reject_or_waive_candidates",
    "create_backlog_from_selection",
    "write_governance_hint",
}

ACCEPTED_BINDING_STATUSES = {"accepted"}
CANDIDATE_STATUSES = {"doc_candidate", "test_candidate"}
BACKLOG_ELIGIBLE_STATUSES = {
    "source_orphan",
    "config_pending_decision",
    "stale",
}


def validate_asset_inbox_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the shared mock/read-model shape used by backend and frontend."""

    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(payload, Mapping):
        return {"ok": False, "errors": ["payload_must_be_object"], "warnings": []}
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version_mismatch")
    if payload.get("impact_scope_policy") != "accepted_bindings_only":
        errors.append("impact_scope_policy_must_be_accepted_bindings_only")
    backlog_policy = payload.get("backlog_policy")
    if not isinstance(backlog_policy, Mapping):
        errors.append("backlog_policy_required")
    else:
        if backlog_policy.get("default_container") is not False:
            errors.append("backlog_default_container_must_be_false")
        if backlog_policy.get("create_from_selected_assets_only") is not True:
            errors.append("backlog_must_be_selection_only")

    items = payload.get("items")
    if not isinstance(items, list):
        errors.append("items_must_be_list")
        items = []
    seen_ids: set[str] = set()
    statuses: list[str] = []
    for index, raw_item in enumerate(items):
        item_path = f"items[{index}]"
        if not isinstance(raw_item, Mapping):
            errors.append(f"{item_path}_must_be_object")
            continue
        asset_id = str(raw_item.get("asset_id") or "")
        if not asset_id:
            errors.append(f"{item_path}.asset_id_required")
        elif asset_id in seen_ids:
            errors.append(f"{item_path}.asset_id_duplicate")
        seen_ids.add(asset_id)
        path = str(raw_item.get("path") or "")
        if not path:
            errors.append(f"{item_path}.path_required")
        asset_kind = str(raw_item.get("asset_kind") or "")
        if asset_kind not in ASSET_KINDS:
            errors.append(f"{item_path}.asset_kind_invalid")
        status = str(raw_item.get("asset_status") or "")
        statuses.append(status)
        if status not in ASSET_STATUSES:
            errors.append(f"{item_path}.asset_status_invalid")
        if not raw_item.get("file_hash"):
            errors.append(f"{item_path}.file_hash_required")
        accepted_bindings = raw_item.get("accepted_bindings") or []
        candidates = raw_item.get("binding_candidates") or []
        if status in ACCEPTED_BINDING_STATUSES and not accepted_bindings:
            errors.append(f"{item_path}.accepted_status_requires_binding")
        if status in CANDIDATE_STATUSES and not candidates:
            errors.append(f"{item_path}.candidate_status_requires_candidate")
        if accepted_bindings and candidates:
            warnings.append(f"{item_path}.accepted_and_candidate_present")
        for c_index, candidate in enumerate(candidates):
            if not isinstance(candidate, Mapping):
                errors.append(f"{item_path}.binding_candidates[{c_index}]_must_be_object")
                continue
            precheck = candidate.get("precheck")
            if not isinstance(precheck, Mapping):
                errors.append(f"{item_path}.binding_candidates[{c_index}].precheck_required")
                continue
            if precheck.get("ok") is not True:
                errors.append(f"{item_path}.binding_candidates[{c_index}].precheck_not_ok")
            if not precheck.get("proposal_hash"):
                errors.append(f"{item_path}.binding_candidates[{c_index}].proposal_hash_required")
        recommended = raw_item.get("recommended_actions") or []
        if not isinstance(recommended, list):
            errors.append(f"{item_path}.recommended_actions_must_be_list")
        backlog = raw_item.get("backlog")
        if not isinstance(backlog, Mapping):
            errors.append(f"{item_path}.backlog_required")
        elif backlog.get("eligible") is True and status not in BACKLOG_ELIGIBLE_STATUSES:
            warnings.append(f"{item_path}.backlog_eligible_status_unusual")

    summary = payload.get("summary")
    if not isinstance(summary, Mapping):
        errors.append("summary_required")
    else:
        counts = Counter(statuses)
        if _int_value(summary.get("total"), -1) != len(items):
            errors.append("summary_total_mismatch")
        by_status = summary.get("by_status")
        if not isinstance(by_status, Mapping):
            errors.append("summary_by_status_required")
        else:
            normalized = {str(key): _int_value(value, 0) for key, value in by_status.items()}
            if normalized != dict(sorted(counts.items())):
                errors.append("summary_by_status_mismatch")

    actions = payload.get("batch_actions")
    if not isinstance(actions, list):
        errors.append("batch_actions_must_be_list")
    else:
        action_names = {
            str(action.get("action") or "")
            for action in actions
            if isinstance(action, Mapping)
        }
        missing_actions = sorted(BATCH_ACTIONS - action_names)
        if missing_actions:
            errors.append("batch_actions_missing:" + ",".join(missing_actions))
        for action in actions:
            if not isinstance(action, Mapping):
                continue
            action_name = str(action.get("action") or "")
            if action_name == "create_backlog_from_selection":
                if action.get("creates_backlog") is not True:
                    errors.append("create_backlog_action_must_mark_creates_backlog")
                if action.get("requires_selection") is not True:
                    errors.append("create_backlog_action_must_require_selection")
            if action_name == "write_governance_hint":
                if action.get("mutates_source") is not True:
                    errors.append("hint_action_must_mark_source_mutation")
                if action.get("requires_review") is not True:
                    errors.append("hint_action_must_require_review")

    return {
        "schema_version": "asset_inbox_precheck.v1",
        "ok": not errors,
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "status_count": len(set(statuses)),
        "item_count": len(items),
    }


def build_asset_inbox_response(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> dict[str, Any]:
    """Materialize the read-only Asset Inbox response for a graph snapshot."""

    from . import graph_snapshot_store as store

    snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    file_inventory_path = store.snapshot_companion_dir(project_id, snapshot_id) / "file_inventory.json"
    file_inventory = _read_json_file(file_inventory_path, [])
    files = [dict(item) for item in file_inventory if isinstance(item, Mapping)] if isinstance(file_inventory, list) else []
    nodes = _load_nodes(conn, project_id, snapshot_id)
    node_by_id = {node["node_id"]: node for node in nodes if node.get("node_id")}
    node_by_title = {str(node.get("title") or ""): node for node in nodes if node.get("title")}
    stale_paths = _stale_source_paths(conn, project_id, snapshot_id, nodes)
    doc_asset_state_path = _doc_asset_state_path(project_id, files)
    doc_assets = _doc_assets_by_path_from_db(conn, project_id, snapshot_id)
    doc_asset_source = "db_projection" if doc_assets else "json_artifact"
    if not doc_assets:
        doc_asset_state = _read_json_file(doc_asset_state_path, {}) if doc_asset_state_path else {}
        doc_assets = _doc_assets_by_path(doc_asset_state)

    items: list[dict[str, Any]] = []
    for row in sorted(files, key=lambda item: str(item.get("path") or "")):
        item = _asset_item_from_inventory(
            row,
            node_by_id=node_by_id,
            node_by_title=node_by_title,
            doc_asset=doc_assets.get(str(row.get("path") or "")),
            stale_paths=stale_paths,
        )
        if item:
            items.append(item)

    by_status = Counter(str(item.get("asset_status") or "") for item in items)
    by_kind = Counter(str(item.get("asset_kind") or "") for item in items)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "commit_sha": str(snapshot.get("commit_sha") or ""),
        "generated_at": _utc_now(),
        "source_artifacts": {
            "file_inventory_path": str(file_inventory_path),
            "doc_asset_state_path": str(doc_asset_state_path or ""),
            "doc_asset_projection_source": doc_asset_source,
            "active_snapshot_id": snapshot_id,
        },
        "impact_scope_policy": "accepted_bindings_only",
        "backlog_policy": {
            "default_container": False,
            "create_from_selected_assets_only": True,
            "reason": "Asset Inbox tracks graph/file hygiene state. Backlog rows are created only for selected actionable work.",
        },
        "summary": {
            "total": len(items),
            "by_status": dict(sorted(by_status.items())),
            "by_kind": dict(sorted(by_kind.items())),
            "candidate_count": by_status.get("doc_candidate", 0) + by_status.get("test_candidate", 0),
            "accepted_count": by_status.get("accepted", 0),
            "unbound_count": by_status.get("doc_unbound", 0),
            "backlog_eligible_count": sum(
                1 for item in items
                if isinstance(item.get("backlog"), Mapping) and item["backlog"].get("eligible") is True
            ),
            "operator_review_count": sum(
                1 for item in items
                if str(item.get("asset_status") or "") in {
                    "source_orphan",
                    "doc_candidate",
                    "test_candidate",
                    "config_pending_decision",
                    "stale",
                }
            ),
        },
        "items": items,
        "batch_actions": asset_inbox_batch_actions(),
    }
    precheck = validate_asset_inbox_payload(payload)
    payload["precheck"] = precheck
    payload["ok"] = bool(precheck.get("ok"))
    return payload


def asset_inbox_batch_actions() -> list[dict[str, Any]]:
    """Return the V1 read-model action contract rendered by the dashboard."""

    return [
        {
            "action": "queue_asset_binding_proposals",
            "label": "AI propose binding",
            "allowed_statuses": ["doc_unbound", "config_pending_decision"],
            "requires_selection": True,
            "requires_review": True,
            "mutates_source": False,
            "payload_example": {
                "asset_ids": ["file:docs/service.md"],
                "actor": "observer",
                "mode": "proposal_only",
            },
        },
        {
            "action": "queue_semantic_enrich",
            "label": "Queue semantic enrich",
            "allowed_statuses": ["source_orphan", "stale"],
            "requires_selection": True,
            "requires_review": True,
            "mutates_source": False,
            "payload_example": {
                "asset_ids": ["file:src/newFeature.ts"],
                "actor": "observer",
            },
        },
        {
            "action": "reject_or_waive_candidates",
            "label": "Reject or waive candidates",
            "allowed_statuses": ["doc_candidate", "test_candidate"],
            "requires_selection": True,
            "requires_review": True,
            "mutates_source": False,
            "payload_example": {
                "asset_ids": ["file:docs/runtime.md"],
                "decision": "reject",
                "rationale": "Path mention is weak evidence only.",
            },
        },
        {
            "action": "create_backlog_from_selection",
            "label": "Create backlog from selected assets",
            "allowed_statuses": ["source_orphan", "config_pending_decision", "stale"],
            "requires_selection": True,
            "requires_review": False,
            "mutates_source": False,
            "creates_backlog": True,
            "payload_example": {
                "asset_ids": ["file:src/newFeature.ts", "file:config/runtime.yaml"],
                "priority": "P1",
                "reason": "Selected assets need implementation or rules work.",
            },
        },
        {
            "action": "write_governance_hint",
            "label": "Write governance hint",
            "allowed_statuses": ["doc_candidate", "test_candidate", "config_pending_decision"],
            "requires_selection": True,
            "requires_review": True,
            "mutates_source": True,
            "payload_example": {
                "asset_id": "file:docs/runtime.md",
                "target_node_id": "L7.runtime",
                "role": "doc",
                "actor": "observer",
            },
        },
    ]


def _asset_item_from_inventory(
    row: Mapping[str, Any],
    *,
    node_by_id: Mapping[str, Mapping[str, Any]],
    node_by_title: Mapping[str, Mapping[str, Any]],
    doc_asset: Mapping[str, Any] | None,
    stale_paths: set[str],
) -> dict[str, Any] | None:
    path = str(row.get("path") or "")
    if not path:
        return None
    kind = _asset_kind(row)
    bindings = _accepted_bindings(row, doc_asset, node_by_id=node_by_id, node_by_title=node_by_title)
    candidates = _binding_candidates(row, doc_asset, node_by_id=node_by_id, node_by_title=node_by_title)
    status = _asset_status(row, kind, bindings=bindings, candidates=candidates, doc_asset=doc_asset, stale_paths=stale_paths)
    if not status:
        return None
    return {
        "asset_id": f"file:{path}",
        "path": path,
        "asset_kind": kind,
        "language": str(row.get("language") or ""),
        "asset_status": status,
        "scan_status": str(row.get("scan_status") or ""),
        "graph_status": str(row.get("graph_status") or ""),
        "doc_kind": str((doc_asset or {}).get("doc_kind") or row.get("file_kind") or ""),
        "binding_status": _binding_status(status, doc_asset),
        "file_hash": str(row.get("file_hash") or _prefixed_hash(row.get("sha256"))),
        "sha256": str(row.get("sha256") or "").removeprefix("sha256:"),
        "size_bytes": int(row.get("size_bytes") or 0),
        "accepted_bindings": bindings,
        "binding_candidates": candidates,
        "recommended_actions": _recommended_actions(status),
        "batch_eligible_actions": _batch_eligible_actions(status),
        "risk": _risk_for_status(status),
        "evidence": _asset_evidence(row, status=status, doc_asset=doc_asset, stale=status == "stale"),
        "backlog": _backlog_state(status),
    }


def _asset_status(
    row: Mapping[str, Any],
    kind: str,
    *,
    bindings: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    doc_asset: Mapping[str, Any] | None,
    stale_paths: set[str],
) -> str:
    path = str(row.get("path") or "")
    scan = str(row.get("scan_status") or "")
    graph = str(row.get("graph_status") or "")
    doc_binding = str((doc_asset or {}).get("binding_status") or "")
    if kind == "source" and path in stale_paths:
        return "stale"
    if scan == "archive" or graph == "archive":
        return "archive"
    if scan == "ignored" or graph == "ignored" or kind == "generated":
        return "ignored"
    if bindings and kind in {"doc", "index_doc", "test", "config"}:
        return "accepted"
    if kind in {"doc", "index_doc"}:
        if doc_binding == "candidate" and candidates:
            return "doc_candidate"
        if candidates:
            return "doc_candidate"
        if doc_binding == "accepted" and bindings:
            return "accepted"
        if scan == "orphan" or graph == "unmapped":
            return "doc_unbound"
    if kind == "test":
        if candidates:
            return "test_candidate"
        if bindings:
            return "accepted"
    if kind == "config":
        if bindings:
            return "accepted"
        if scan == "pending_decision" or graph == "pending_decision":
            return "config_pending_decision"
    if kind == "source":
        if scan == "orphan" or graph == "unmapped":
            return "source_orphan"
    if scan == "pending_decision" or graph == "pending_decision":
        return "config_pending_decision"
    return ""


def _asset_kind(row: Mapping[str, Any]) -> str:
    kind = str(row.get("file_kind") or "unknown")
    if kind in ASSET_KINDS:
        return kind
    return "unknown"


def _accepted_bindings(
    row: Mapping[str, Any],
    doc_asset: Mapping[str, Any] | None,
    *,
    node_by_id: Mapping[str, Mapping[str, Any]],
    node_by_title: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    doc_bindings = (doc_asset or {}).get("accepted_bindings")
    if isinstance(doc_bindings, list) and doc_bindings:
        return [
            {
                "node_id": str(binding.get("node_id") or ""),
                "title": str(binding.get("title") or ""),
                "role": str(binding.get("role") or "doc"),
                "source": str(binding.get("source") or "doc_asset_state"),
            }
            for binding in doc_bindings
            if isinstance(binding, Mapping) and binding.get("node_id")
        ]
    node_ids = _list_strings(row.get("attached_node_ids") or row.get("mapped_node_ids"))
    if not node_ids:
        return []
    role = str(row.get("attachment_role") or _asset_kind(row) or "asset")
    source = str(row.get("attachment_source") or "file_inventory")
    bindings: list[dict[str, Any]] = []
    for raw_node_id in node_ids:
        node = _node_for_ref(raw_node_id, node_by_id=node_by_id, node_by_title=node_by_title)
        node_id = str(node.get("node_id") or raw_node_id)
        bindings.append({
            "node_id": node_id,
            "title": str(node.get("title") or row.get("attached_to") or raw_node_id),
            "role": role,
            "source": source,
        })
    return bindings


def _binding_candidates(
    row: Mapping[str, Any],
    doc_asset: Mapping[str, Any] | None,
    *,
    node_by_id: Mapping[str, Mapping[str, Any]],
    node_by_title: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    doc_candidates = (doc_asset or {}).get("binding_candidates")
    if isinstance(doc_candidates, list) and doc_candidates:
        return [_normalize_candidate(candidate) for candidate in doc_candidates if isinstance(candidate, Mapping)]
    raw_target = str(row.get("candidate_node_id") or "")
    if not raw_target or _list_strings(row.get("attached_node_ids") or row.get("mapped_node_ids")):
        return []
    node = _node_for_ref(raw_target, node_by_id=node_by_id, node_by_title=node_by_title)
    target_node_id = str(node.get("node_id") or raw_target)
    target_title = str(node.get("title") or raw_target)
    kind = _asset_kind(row)
    evidence_kind = "test_import_fanin" if kind == "test" else "path_reference"
    proposal_hash = _proposal_hash(str(row.get("path") or ""), target_node_id, evidence_kind)
    return [{
        "schema_version": "asset_binding_proposal.v1",
        "operation": "propose_binding",
        "asset_kind": kind,
        "asset_path": str(row.get("path") or ""),
        "target_node_id": target_node_id,
        "target_title": target_title,
        "evidence_kind": evidence_kind,
        "source": "file_inventory_candidate",
        "proposal_hash": proposal_hash,
        "precheck": {
            "schema_version": "asset_binding_precheck.v1",
            "ok": True,
            "mode": "deterministic_precheck",
            "decision": "review_required",
            "binding_strength": "weak",
            "proposal_hash": proposal_hash,
            "errors": [],
            "warnings": [],
        },
    }]


def _normalize_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(candidate)
    proposal_hash = str(payload.get("proposal_hash") or _proposal_hash(
        str(payload.get("asset_path") or ""),
        str(payload.get("target_node_id") or ""),
        str(payload.get("evidence_kind") or ""),
    ))
    payload["schema_version"] = str(payload.get("schema_version") or "asset_binding_proposal.v1")
    payload["operation"] = str(payload.get("operation") or "propose_binding")
    payload["proposal_hash"] = proposal_hash
    precheck = payload.get("precheck")
    if not isinstance(precheck, Mapping):
        precheck = {}
    payload["precheck"] = {
        "schema_version": str(precheck.get("schema_version") or "asset_binding_precheck.v1"),
        "ok": bool(precheck.get("ok", True)),
        "mode": str(precheck.get("mode") or "deterministic_precheck"),
        "decision": str(precheck.get("decision") or "review_required"),
        "binding_strength": str(precheck.get("binding_strength") or "weak"),
        "proposal_hash": str(precheck.get("proposal_hash") or proposal_hash),
        "errors": precheck.get("errors") if isinstance(precheck.get("errors"), list) else [],
        "warnings": precheck.get("warnings") if isinstance(precheck.get("warnings"), list) else [],
    }
    return payload


def _recommended_actions(status: str) -> list[str]:
    if status == "source_orphan":
        return ["queue_semantic_enrich", "create_backlog_from_selection"]
    if status == "doc_unbound":
        return ["queue_asset_binding_proposals"]
    if status in {"doc_candidate", "test_candidate"}:
        return ["reject_or_waive_candidates", "write_governance_hint"]
    if status == "config_pending_decision":
        return ["queue_asset_binding_proposals", "create_backlog_from_selection"]
    if status == "stale":
        return ["queue_semantic_enrich", "create_backlog_from_selection"]
    return []


def _batch_eligible_actions(status: str) -> list[str]:
    return [
        action["action"]
        for action in asset_inbox_batch_actions()
        if status in action.get("allowed_statuses", [])
    ]


def _risk_for_status(status: str) -> str:
    if status in {"source_orphan", "stale"}:
        return "high"
    if status in {"doc_candidate", "test_candidate", "config_pending_decision", "doc_unbound"}:
        return "medium"
    return "low"


def _backlog_state(status: str) -> dict[str, Any]:
    eligible = status in BACKLOG_ELIGIBLE_STATUSES
    reasons = {
        "source_orphan": "New source orphan may require graph ownership or adapter/config work.",
        "config_pending_decision": "Config-like asset needs classification or durable rule work.",
        "stale": "Stale mapped source may require semantic refresh or scoped implementation review.",
    }
    return {
        "eligible": eligible,
        "reason": reasons.get(status, "Asset state should be reviewed or accepted before creating implementation work."),
    }


def _binding_status(status: str, doc_asset: Mapping[str, Any] | None) -> str:
    if doc_asset and doc_asset.get("binding_status"):
        return str(doc_asset.get("binding_status") or "")
    if status == "accepted":
        return "accepted"
    if status in {"doc_candidate", "test_candidate"}:
        return "candidate"
    if status == "doc_unbound":
        return "unbound"
    return ""


def _asset_evidence(
    row: Mapping[str, Any],
    *,
    status: str,
    doc_asset: Mapping[str, Any] | None,
    stale: bool,
) -> list[dict[str, Any]]:
    evidence = []
    source = "doc_asset_state" if doc_asset else "file_inventory"
    evidence.append({
        "kind": source,
        "message": str(row.get("reason") or f"{status} from {source}."),
    })
    if stale:
        evidence.append({
            "kind": "semantic_projection",
            "message": "Mapped source has stale node semantic state for this snapshot.",
        })
    return evidence


def _load_nodes(conn: sqlite3.Connection, project_id: str, snapshot_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT node_id, layer, title, kind, primary_files_json, secondary_files_json,
               test_files_json, metadata_json
        FROM graph_nodes_index
        WHERE project_id = ? AND snapshot_id = ?
        ORDER BY node_id
        """,
        (project_id, snapshot_id),
    ).fetchall()
    return [
        {
            "node_id": row["node_id"],
            "layer": row["layer"],
            "title": row["title"],
            "kind": row["kind"],
            "primary_files": _json_value(row["primary_files_json"], []),
            "secondary_files": _json_value(row["secondary_files_json"], []),
            "test_files": _json_value(row["test_files_json"], []),
            "metadata": _json_value(row["metadata_json"], {}),
        }
        for row in rows
    ]


def _stale_source_paths(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    nodes: Iterable[Mapping[str, Any]],
) -> set[str]:
    stale_node_ids = set(_stale_node_ids_from_projection(conn, project_id, snapshot_id))
    if _table_exists(conn, "graph_semantic_nodes"):
        rows = conn.execute(
            """
            SELECT node_id
            FROM graph_semantic_nodes
            WHERE project_id = ? AND snapshot_id = ? AND status LIKE '%stale%'
            """,
            (project_id, snapshot_id),
        ).fetchall()
        stale_node_ids.update(str(row["node_id"]) for row in rows)
    paths: set[str] = set()
    for node in nodes:
        if str(node.get("node_id") or "") not in stale_node_ids:
            continue
        for path in _list_strings(node.get("primary_files")):
            paths.add(path)
    return paths


def _stale_node_ids_from_projection(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> set[str]:
    try:
        from . import graph_events

        projection = graph_events.get_semantic_projection(conn, project_id, snapshot_id) or {}
    except Exception:
        return set()
    payload = projection.get("projection") if isinstance(projection, Mapping) else None
    node_semantics = payload.get("node_semantics") if isinstance(payload, Mapping) else None
    if not isinstance(node_semantics, Mapping):
        return set()
    stale_ids: set[str] = set()
    for node_id, entry in node_semantics.items():
        if not isinstance(entry, Mapping):
            continue
        validity = entry.get("validity") if isinstance(entry.get("validity"), Mapping) else {}
        status = str(validity.get("status") or entry.get("status") or "").lower()
        if "stale" in status:
            stale_ids.add(str(node_id))
    return stale_ids


def _doc_asset_state_path(project_id: str, files: list[dict[str, Any]]) -> Path | None:
    run_id = ""
    for row in files:
        run_id = str(row.get("run_id") or "")
        if run_id:
            break
    if not run_id:
        return None
    from .db import _governance_root

    return _governance_root() / project_id / "governance-index" / run_id / "doc-asset-state.json"


def _doc_assets_by_path(doc_asset_state: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(doc_asset_state, Mapping):
        return {}
    docs = doc_asset_state.get("docs")
    if not isinstance(docs, list):
        return {}
    return {
        str(item.get("path") or ""): dict(item)
        for item in docs
        if isinstance(item, Mapping) and item.get("path")
    }


def _doc_assets_by_path_from_db(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> dict[str, dict[str, Any]]:
    try:
        from .asset_projection import list_asset_projection

        rows = list_asset_projection(
            conn,
            project_id=project_id,
            snapshot_id=snapshot_id,
            asset_kind="doc",
            limit=5000,
        )
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = str(row.get("asset_path") or "")
        if not path:
            continue
        out[path] = {
            "path": path,
            "doc_kind": str((row.get("metadata") or {}).get("doc_kind") or row.get("file_kind") or "doc"),
            "binding_status": str(row.get("binding_status") or ""),
            "accepted_bindings": row.get("accepted_bindings") if isinstance(row.get("accepted_bindings"), list) else [],
            "binding_candidates": row.get("binding_candidates") if isinstance(row.get("binding_candidates"), list) else [],
            "impact_scope_policy": str(row.get("impact_scope_policy") or "accepted_bindings_only"),
        }
    return out


def _node_for_ref(
    ref: str,
    *,
    node_by_id: Mapping[str, Mapping[str, Any]],
    node_by_title: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    return node_by_id.get(ref) or node_by_title.get(ref) or {}


def _list_strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str) and value:
        return [value]
    return []


def _prefixed_hash(value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    return raw if raw.startswith("sha256:") else f"sha256:{raw}"


def _proposal_hash(path: str, target_node_id: str, evidence_kind: str) -> str:
    digest = hashlib.sha256(f"{path}|{target_node_id}|{evidence_kind}".encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _read_json_file(path: Path | None, default: Any) -> Any:
    if path is None or not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return default


def _json_value(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return default
        return value if value is not None else default
    return default


def _int_value(raw: Any, default: int = 0) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "ASSET_KINDS",
    "ASSET_STATUSES",
    "BACKLOG_ELIGIBLE_STATUSES",
    "BATCH_ACTIONS",
    "SCHEMA_VERSION",
    "asset_inbox_batch_actions",
    "build_asset_inbox_response",
    "validate_asset_inbox_payload",
]
