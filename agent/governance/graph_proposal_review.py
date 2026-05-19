"""Audited review helpers for clustered graph/enrich config proposals."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import graph_events
from .graph_enrich_config_ops import (
    parse_graph_enrich_config_ai_output,
    run_graph_enrich_config_ai_output_pipeline,
)


def apply_graph_enrich_config_observer_override(
    conn,
    project_id: str,
    snapshot_id: str,
    *,
    cluster: Mapping[str, Any] | None = None,
    cluster_id: str = "",
    rejected_event_ids: Sequence[str] | None = None,
    raw_output: Any,
    mode: str = "dry_run",
    project_root: str | Path,
    actor: str = "observer",
    rationale: str = "",
) -> dict[str, Any]:
    """Apply an observer-authored graph_enrich_config override with audit events."""

    normalized_mode = str(mode or "dry_run").strip().lower().replace("-", "_")
    root = Path(project_root).resolve()
    review = _observer_override_review(
        cluster=cluster,
        cluster_id=cluster_id,
        rejected_event_ids=rejected_event_ids,
        actor=actor,
        rationale=rationale,
    )
    operation_hash = _operation_hash(raw_output)
    request_event = graph_events.create_event(
        conn,
        project_id,
        snapshot_id,
        event_type="graph_enrich_config_requested",
        event_kind="user_feedback",
        target_type="project",
        target_id=project_id,
        status=graph_events.EVENT_STATUS_OBSERVED,
        operation_type="graph_enrich_config",
        payload={
            "mode": normalized_mode,
            "project_root": str(root),
            "ai_output": raw_output,
            "review": review,
        },
        precondition={
            "requires_gate": True,
            "gate": "graph_enrich_config_ops.v1",
            "observer_approval_required": True,
        },
        evidence={
            "source": "observer_graph_enrich_config_override",
            "actor": actor,
            "mode": normalized_mode,
            "cluster_id": review["cluster_id"],
            "review_action": review["review_action"],
            "selected_event_id": review["selected_event_id"],
            "rejected_event_ids": review["rejected_event_ids"],
            "operation_hash": operation_hash,
        },
        created_by=actor,
    )

    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=raw_output,
        mode=normalized_mode,
        project_root=root,
    )
    precheck = _result_precheck(result)
    event_type = "graph_enrich_config_completed" if result.get("ok") else "graph_enrich_config_failed"
    event_status = (
        graph_events.EVENT_STATUS_OBSERVED
        if result.get("ok")
        else graph_events.EVENT_STATUS_FAILED
    )
    result_event = graph_events.create_event(
        conn,
        project_id,
        snapshot_id,
        event_type=event_type,
        event_kind="user_feedback",
        target_type="project",
        target_id=project_id,
        status=event_status,
        operation_type="graph_enrich_config",
        source_event_id=str(request_event.get("event_id") or ""),
        payload={
            "result": result,
            "review": review,
        },
        evidence={
            "source": "observer_graph_enrich_config_override",
            "actor": actor,
            "mode": normalized_mode,
            "cluster_id": review["cluster_id"],
            "review_action": review["review_action"],
            "selected_event_id": review["selected_event_id"],
            "rejected_event_ids": review["rejected_event_ids"],
            "operation_hash": operation_hash,
            "precheck": precheck,
        },
        created_by=actor,
    )
    request_status = (
        graph_events.EVENT_STATUS_MATERIALIZED
        if result.get("ok")
        else graph_events.EVENT_STATUS_FAILED
    )
    request_event = graph_events.update_event_status(
        conn,
        project_id,
        snapshot_id,
        str(request_event.get("event_id") or ""),
        status=request_status,
        actor=actor,
        operation_type="graph_enrich_config",
        evidence={
            "source": "observer_graph_enrich_config_override",
            "completed": bool(result.get("ok")),
            "mode": normalized_mode,
            "precheck": precheck,
            "result_event_id": result_event.get("event_id", ""),
        },
    )
    dry_run = normalized_mode in {"dry_run", "dryrun", "preview"}
    return {
        **result,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "dry_run": dry_run,
        "review": review,
        "audit": {
            "operation_hash": operation_hash,
            "request_event_id": request_event.get("event_id", ""),
            "result_event_id": result_event.get("event_id", ""),
        },
        "request_event": request_event,
        "result_event": result_event,
    }


def _observer_override_review(
    *,
    cluster: Mapping[str, Any] | None,
    cluster_id: str,
    rejected_event_ids: Sequence[str] | None,
    actor: str,
    rationale: str,
) -> dict[str, Any]:
    cluster_data = cluster if isinstance(cluster, Mapping) else {}
    support_event_ids = cluster_data.get("support_event_ids")
    support_rule_ids = cluster_data.get("support_rule_ids")
    return {
        "review_action": "observer_override",
        "cluster_id": str(cluster_id or cluster_data.get("cluster_id") or ""),
        "issue_family": str(cluster_data.get("issue_family") or ""),
        "canonical_rule_id": str(cluster_data.get("canonical_rule_id") or ""),
        "selected_event_id": "",
        "rejected_event_ids": _string_list(rejected_event_ids or support_event_ids or []),
        "support_rule_ids": _string_list(support_rule_ids or []),
        "actor": str(actor or "observer"),
        "rationale": str(rationale or ""),
    }


def _operation_hash(raw_output: Any) -> str:
    parsed = parse_graph_enrich_config_ai_output(raw_output)
    value: Any
    if parsed.get("ok"):
        value = parsed.get("payload") or {}
    else:
        value = raw_output
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _result_precheck(result: Mapping[str, Any]) -> dict[str, Any]:
    precheck = result.get("precheck")
    if isinstance(precheck, Mapping):
        return dict(precheck)
    gate = result.get("gate") if isinstance(result.get("gate"), Mapping) else {}
    gate_precheck = gate.get("precheck") if isinstance(gate.get("precheck"), Mapping) else {}
    return dict(gate_precheck)


def _string_list(values: Sequence[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "")
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result
