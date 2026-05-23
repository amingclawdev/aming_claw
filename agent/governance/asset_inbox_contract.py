"""Shared Asset Inbox contract helpers.

The Asset Inbox is a read model for graph/file hygiene assets. It is not a
backlog table. Backlog rows are created only from selected actionable assets.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Mapping


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
        if int(summary.get("total") or -1) != len(items):
            errors.append("summary_total_mismatch")
        by_status = summary.get("by_status")
        if not isinstance(by_status, Mapping):
            errors.append("summary_by_status_required")
        else:
            normalized = {str(key): int(value) for key, value in by_status.items()}
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


__all__ = [
    "ASSET_KINDS",
    "ASSET_STATUSES",
    "BACKLOG_ELIGIBLE_STATUSES",
    "BATCH_ACTIONS",
    "SCHEMA_VERSION",
    "validate_asset_inbox_payload",
]
