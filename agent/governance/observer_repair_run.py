"""Replayable observer repair-run planning contract.

This module is intentionally read-only. It creates deterministic recovery plans
for cross-system observer work, but it never mints route tokens or satisfies
protected close-gate evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from typing import Any


SCHEMA_VERSION = "observer_repair_run_plan.v1"
ROUTE_CONTEXT_SCHEMA_VERSION = "observer_repair_route_context.v1"
ROUTE_SERVICE_PREVIEW_SCHEMA_VERSION = "observer_repair_route_service_preview.v1"
ROUTE_WORKFLOW_TEMPLATE_ID = "mf_workflow_runtime.v1"
ROUTE_PROMPT_EVENT_KIND = "route.prompt_context.requested"
ROUTE_ACTION_EVENT_KIND = "route.action.requested"
ROUTE_SERVICE_STAGE = "dispatch"

CHECKPOINTS = [
    "diagnosed",
    "route_context_ready",
    "dispatch_ready",
    "worker_started",
    "implementation_done",
    "verification_done",
    "graph_reconcile_done",
    "close_precheck_passed",
    "closed",
]

LANE_PRIORITY = {
    "runtime_schema": 10,
    "route_context": 20,
    "graph_reconcile": 30,
    "subsystem_evidence": 40,
    "independent_verification": 50,
    "close_gate": 60,
    "observer_triage": 90,
}

LANE_ACTIONS = {
    "runtime_schema": [
        "compare_current_mcp_schema_to_source",
        "fix_mcp_schema_or_runtime_passthrough",
        "redeploy_or_reload_governance_surfaces",
        "rerun_schema_parity_preflight",
    ],
    "route_context": [
        "request_route_prompt_alert_bundle",
        "run_route_action_precheck",
        "supersede_or_reset_stale_route_identity",
        "retry_protected_action_with_matching_route_token_or_valid_waiver",
    ],
    "graph_reconcile": [
        "inspect_graph_status",
        "prefer_direct_scope_reconcile_with_activation",
        "fall_back_to_full_reconcile_on_rule_fingerprint_change",
        "rerun_graph_status_until_current",
    ],
    "subsystem_evidence": [
        "dispatch_bounded_implementation_lane",
        "record_worker_startup_evidence",
        "append_implementation_evidence_after_worker_result",
    ],
    "independent_verification": [
        "dispatch_independent_verification_lane",
        "run_focused_tests_or_e2e",
        "append_verification_evidence_after_results_pass",
    ],
    "close_gate": [
        "run_mf_timeline_precheck",
        "append_close_ready_only_after_required_evidence_passes",
        "retry_backlog_close_with_matching_route_token_or_valid_waiver",
    ],
    "observer_triage": [
        "group_blockers_by_recovery_class",
        "produce_next_legal_actions",
    ],
}

LANE_BLOCKED_ACTIONS = [
    "close_without_mf_timeline_precheck",
    "protected_write_without_route_token_or_valid_waiver",
    "use_judgment_brain_as_execution_dependency",
    "count_diagnostic_alert_as_route_or_close_evidence",
    "dispatch_worker_without_file_fence",
]

BLOCKER_RULES = [
    (
        "route_token_required",
        ("route_token_required", "route-token", "route token"),
        "route_context",
        "return_to_route_context_and_request_valid_route_token",
    ),
    (
        "schema_mismatch",
        ("schema mismatch", "schema gap", "schema not", "does not expose", "does not consume"),
        "runtime_schema",
        "fix_mcp_schema_runtime_parity",
    ),
    (
        "graph_stale",
        ("graph stale", "active_graph_stale", "pending scope", "pending-scope", "scope reconcile"),
        "graph_reconcile",
        "run_graph_reconcile_or_actionable_fallback",
    ),
    (
        "pending_scope_timeout",
        ("timeout", "timed out"),
        "graph_reconcile",
        "replace_queue_wait_with_bounded_reconcile_fallback",
    ),
    (
        "missing_verification",
        ("missing verification", "independent_verification", "independent verification"),
        "independent_verification",
        "dispatch_independent_verification_lane",
    ),
    (
        "missing_timeline_evidence",
        ("implementation", "verification", "close_ready", "missing_event_kinds"),
        "subsystem_evidence",
        "append_required_timeline_evidence_after_real_work",
    ),
    (
        "route_identity_mismatch",
        ("route_identity_mismatch", "identity mismatch", "stale route"),
        "route_context",
        "supersede_or_reset_stale_route_identity_before_retry",
    ),
]


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


def _stable_hash(payload: Any, *, length: int = 16) -> str:
    raw = json.dumps(_jsonable(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]


def _string(value: Any) -> str:
    return str(value or "").strip()


def _object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value in (None, ""):
        return []
    return [value]


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _string_list_field(value: Any) -> list[str]:
    if isinstance(value, str):
        parsed = _parse_json_list(value)
        if parsed:
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [
            item.strip()
            for item in value.replace("\r", "\n").replace(",", "\n").split("\n")
            if item.strip()
        ]
    return [str(item).strip() for item in _list(value) if str(item).strip()]


def _aggregate_row_list(rows: Sequence[Mapping[str, Any]], key: str) -> list[str]:
    values: list[str] = []
    for row in rows:
        values.extend(_string_list_field(row.get(key)))
    return sorted(set(values))


def _highest_priority(rows: Sequence[Mapping[str, Any]]) -> str:
    rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}
    priorities = [_string(row.get("priority")).upper() for row in rows]
    priorities = [item for item in priorities if item]
    if not priorities:
        return ""
    return sorted(priorities, key=lambda item: rank.get(item, 99))[0]


def _row_text(row: Mapping[str, Any]) -> str:
    parts = [
        row.get("bug_id"),
        row.get("title"),
        row.get("details_md"),
        row.get("target_files"),
        row.get("acceptance_criteria"),
        row.get("required_docs"),
    ]
    return " ".join(str(part or "") for part in parts).lower()


def classify_text(text: str) -> dict[str, Any]:
    lowered = str(text or "").lower()
    matches: list[dict[str, str]] = []
    for blocker_id, needles, lane, action in BLOCKER_RULES:
        if any(needle in lowered for needle in needles):
            matches.append(
                {
                    "blocker_id": blocker_id,
                    "lane_id": lane,
                    "recovery_action": action,
                }
            )
    if not matches:
        matches.append(
            {
                "blocker_id": "unknown",
                "lane_id": "observer_triage",
                "recovery_action": "inspect_evidence_and_file_bounded_followup",
            }
        )
    return {
        "input": str(text or ""),
        "matches": matches,
    }


def _classify_backlog_row(row: Mapping[str, Any]) -> dict[str, Any]:
    classification = classify_text(_row_text(row))
    lane_ids = sorted({match["lane_id"] for match in classification["matches"]}, key=lambda lane: LANE_PRIORITY[lane])
    return {
        "bug_id": _string(row.get("bug_id")),
        "status": _string(row.get("status")),
        "priority": _string(row.get("priority")),
        "lane_ids": lane_ids or ["observer_triage"],
        "blocker_ids": sorted({match["blocker_id"] for match in classification["matches"]}),
        "recovery_actions": sorted({match["recovery_action"] for match in classification["matches"]}),
    }


def _extract_declared_dependencies(row: Mapping[str, Any]) -> list[str]:
    contract = _parse_json_object(row.get("chain_trigger_json"))
    candidates: list[Any] = []
    for key in ("depends_on", "related_backlog_ids", "dependencies"):
        candidates.extend(_list(contract.get(key)))
    details = _string(row.get("details_md"))
    for token in details.replace(",", " ").replace("\n", " ").split(" "):
        token = token.strip().strip(".,;:()[]")
        if token.startswith(("AC-", "JB-", "CONTENT-", "MS-")):
            candidates.append(token)
    bug_id = _string(row.get("bug_id"))
    return sorted({str(item).strip() for item in candidates if str(item).strip() and str(item).strip() != bug_id})


def _build_route_context(project_id: str, root_backlog_ids: Sequence[str], seed: Mapping[str, Any]) -> dict[str, Any]:
    base = {
        "project_id": project_id,
        "root_backlog_ids": sorted(root_backlog_ids),
        "seed": _jsonable(seed),
    }
    digest = _stable_hash(base, length=16)
    return {
        "schema_version": ROUTE_CONTEXT_SCHEMA_VERSION,
        "route_id": f"route-repair-{digest}",
        "route_context_hash": f"sha256:{_stable_hash(base, length=64)}",
        "prompt_contract_id": f"rprompt-repair-{digest}",
        "topology": "observer_led_repair_run",
        "owner": "aming-claw",
        "judgment_brain_required": False,
        "read_only": True,
        "authorizes_protected_write": False,
        "allowed_actions": [
            "diagnose_backlog_dependency_dag",
            "create_read_only_repair_run_plan",
            "dispatch_bounded_lanes_after_route_token",
            "run_close_gate_precheck",
        ],
        "blocked_actions": list(LANE_BLOCKED_ACTIONS),
    }


def _build_lanes(classified_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_lane: dict[str, set[str]] = {}
    for row in classified_rows:
        bug_id = _string(row.get("bug_id"))
        for lane in _list(row.get("lane_ids")):
            by_lane.setdefault(str(lane), set()).add(bug_id)
    if not by_lane:
        by_lane["observer_triage"] = set()
    lanes: list[dict[str, Any]] = []
    for lane_id in sorted(by_lane, key=lambda lane: LANE_PRIORITY.get(lane, 999)):
        lanes.append(
            {
                "lane_id": lane_id,
                "role": "observer" if lane_id in {"observer_triage", "close_gate"} else "mf_sub",
                "status": "pending",
                "target_backlog_ids": sorted(item for item in by_lane[lane_id] if item),
                "requires_file_fence": lane_id not in {"observer_triage", "close_gate", "route_context"},
                "requires_route_token_for_write": lane_id != "observer_triage",
                "allowed_actions": LANE_ACTIONS.get(lane_id, LANE_ACTIONS["observer_triage"]),
                "blocked_actions": list(LANE_BLOCKED_ACTIONS),
            }
        )
    if "close_gate" not in by_lane:
        lanes.append(
            {
                "lane_id": "close_gate",
                "role": "observer",
                "status": "pending",
                "target_backlog_ids": sorted(
                    row["bug_id"] for row in classified_rows if _string(row.get("bug_id"))
                ),
                "requires_file_fence": False,
                "requires_route_token_for_write": True,
                "allowed_actions": LANE_ACTIONS["close_gate"],
                "blocked_actions": list(LANE_BLOCKED_ACTIONS),
            }
        )
    return lanes


def _build_dependency_dag(classified_rows: Sequence[Mapping[str, Any]], rows_by_id: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    edges: set[tuple[str, str, str]] = set()
    for classified in classified_rows:
        bug_id = _string(classified.get("bug_id"))
        if not bug_id:
            continue
        lane_ids = _list(classified.get("lane_ids")) or ["observer_triage"]
        primary_lane = str(lane_ids[0])
        nodes.append(
            {
                "id": bug_id,
                "kind": "backlog",
                "lane_id": primary_lane,
                "status": _string(classified.get("status")),
                "priority": _string(classified.get("priority")),
                "blocker_ids": _list(classified.get("blocker_ids")),
                "recovery_actions": _list(classified.get("recovery_actions")),
            }
        )
        row = rows_by_id.get(bug_id, {})
        for dep in _extract_declared_dependencies(row):
            edges.add((dep, bug_id, "declared_dependency"))
        if "runtime_schema" not in lane_ids and any(l in lane_ids for l in ("route_context", "subsystem_evidence", "close_gate")):
            schema_nodes = [
                other["bug_id"]
                for other in classified_rows
                if "runtime_schema" in _list(other.get("lane_ids"))
            ]
            for schema_node in schema_nodes:
                edges.add((schema_node, bug_id, "schema_before_protected_write"))
        if "close_gate" in lane_ids:
            for other in classified_rows:
                other_id = _string(other.get("bug_id"))
                if other_id and other_id != bug_id:
                    edges.add((other_id, bug_id, "evidence_before_close"))
    return {
        "nodes": sorted(nodes, key=lambda node: (LANE_PRIORITY.get(node["lane_id"], 999), node["id"])),
        "edges": [
            {"from": src, "to": dst, "reason": reason}
            for src, dst, reason in sorted(edges)
        ],
    }


def _build_checkpoints(route_context: Mapping[str, Any], lanes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "checkpoint_id": checkpoint,
            "status": "passed" if checkpoint == "diagnosed" else "pending",
            "route_context_hash": route_context.get("route_context_hash", ""),
            "requires_evidence": checkpoint not in {"diagnosed", "route_context_ready"},
            "lane_ids": [str(lane.get("lane_id")) for lane in lanes],
        }
        for checkpoint in CHECKPOINTS
    ]


def _route_service_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    routes = _list(result.get("routes"))
    first = _object(routes[0]) if routes else {}
    return {
        "decision": _string(result.get("decision")),
        "status": _string(result.get("status")),
        "route_count": int(result.get("route_count") or len(routes)),
        "route_id": _string(first.get("route_id")),
        "service_id": _string(first.get("service_id")),
        "route_status": _string(first.get("status")),
        "route_decision": _string(first.get("decision")),
        "reason": _string(first.get("reason")),
        "evidence": _object(first.get("evidence")),
        "contract_evidence": _list(first.get("contract_evidence")),
    }


def _first_route_result(result: Mapping[str, Any]) -> dict[str, Any]:
    routes = _list(result.get("routes"))
    return _object(routes[0]) if routes else {}


def _service_route_identity(route: Mapping[str, Any], bundle: Mapping[str, Any]) -> dict[str, str]:
    evidence = _object(route.get("evidence"))
    prompt_contract = _object(bundle.get("prompt_contract"))
    return {
        "route_context_hash": _string(
            bundle.get("route_context_hash") or evidence.get("route_context_hash")
        ),
        "prompt_contract_id": _string(
            prompt_contract.get("prompt_contract_id")
            or evidence.get("prompt_contract_id")
        ),
        "prompt_contract_hash": _string(
            bundle.get("prompt_contract_hash") or evidence.get("prompt_contract_hash")
        ),
        "visible_injection_manifest_hash": _string(evidence.get("visible_injection_manifest_hash")),
    }


def _route_prompt_payload(
    *,
    project_id: str,
    root_backlog_ids: Sequence[str],
    backlog_rows: Sequence[Mapping[str, Any]],
    classified_rows: Sequence[Mapping[str, Any]],
    route_context: Mapping[str, Any],
    repair_run_id: str,
) -> dict[str, Any]:
    target_files = _aggregate_row_list(backlog_rows, "target_files")
    test_files = _aggregate_row_list(backlog_rows, "test_files")
    acceptance_criteria = _aggregate_row_list(backlog_rows, "acceptance_criteria")
    priority = _highest_priority(backlog_rows)
    titles = [
        _string(row.get("title") or row.get("bug_id"))
        for row in backlog_rows
        if _string(row.get("title") or row.get("bug_id"))
    ]
    blocker_ids = sorted(
        {
            str(blocker)
            for row in classified_rows
            for blocker in _list(row.get("blocker_ids"))
            if str(blocker)
        }
    )
    summary = "; ".join(titles[:3]) or f"Observer repair run for {project_id}"
    if len(titles) > 3:
        summary += f"; +{len(titles) - 3} more"
    prompt_contract_id = _string(route_context.get("prompt_contract_id"))
    evidence_required = [
        "route_context",
        "route_action_precheck",
        "bounded_implementation_worker_dispatch",
        "mf_subagent_startup",
        "independent_verification",
        "implementation",
        "verification",
        "close_ready",
    ]
    return {
        "intent": "observer_repair_run",
        "project_id": project_id,
        "backlog_id": root_backlog_ids[0] if len(root_backlog_ids) == 1 else "",
        "root_backlog_ids": sorted(root_backlog_ids),
        "priority": priority,
        "risk_class": "high_risk" if priority in {"P0", "P1"} else "small_deterministic",
        "route_id": _string(route_context.get("route_id")),
        "stage": ROUTE_SERVICE_STAGE,
        "caller_role": "observer",
        "selected_topology": "observer_led_parallel_lanes",
        "recommended_topology": "mf_parallel.v1",
        "content": {
            "kind": "observer_repair_summary",
            "summary": summary,
        },
        "prompt_contract": {
            "prompt_contract_id": prompt_contract_id,
            "prompt_kind": "observer_repair_run",
            "target_files": target_files,
            "test_files": test_files,
            "acceptance_criteria": acceptance_criteria,
            "evidence_required": evidence_required,
        },
        "visible_injection_manifest": {
            "schema_version": "visible_injection_manifest.v1",
            "policy": "route_owned_visible_refs_only",
            "allowed_injections": [
                {
                    "kind": "observer_repair_plan",
                    "id": repair_run_id,
                    "source_ref": f"observer_repair_run:{repair_run_id}",
                    "sha256": _string(route_context.get("route_context_hash")),
                    "status": "generated",
                }
            ],
        },
        "route_alerts": [
            "implementation_prompt_must_live_in_route",
            "external_injection_requires_visible_route_ref",
        ],
        "blocker_ids": blocker_ids,
    }


def _route_action_precheck_payload(
    *,
    action: str,
    caller_role: str,
    bundle: Mapping[str, Any],
    route_identity: Mapping[str, Any],
    graph_status: Mapping[str, Any],
    version_check: Mapping[str, Any],
) -> dict[str, Any]:
    prompt_contract = _object(bundle.get("prompt_contract"))
    verification_policy = _object(bundle.get("verification_policy"))
    required_evidence = [
        *_string_list_field(prompt_contract.get("evidence_required")),
        *_string_list_field(verification_policy.get("required_evidence_ids")),
    ]
    return {
        "caller_role": caller_role,
        "action": action,
        "route_prompt_bundle": dict(bundle),
        "route_context_hash": _string(route_identity.get("route_context_hash")),
        "prompt_contract_id": _string(route_identity.get("prompt_contract_id")),
        "prompt_contract_hash": _string(route_identity.get("prompt_contract_hash")),
        "visible_injection_manifest_hash": _string(
            route_identity.get("visible_injection_manifest_hash")
        ),
        "visible_injection_manifest": _object(bundle.get("visible_injection_manifest")),
        "route_alerts": _list(bundle.get("alerts")),
        "selected_topology": _string(bundle.get("selected_topology")),
        "recommended_topology": _string(bundle.get("recommended_topology")),
        "required_lanes": _list(bundle.get("required_lanes")),
        "required_evidence": sorted(set(required_evidence)),
        "allowed_actions": [action],
        "target_files": _string_list_field(prompt_contract.get("target_files")),
        "test_files": _string_list_field(prompt_contract.get("test_files")),
        "version_check": dict(version_check) if isinstance(version_check, Mapping) else {},
        "graph_status": dict(graph_status) if isinstance(graph_status, Mapping) else {},
    }


def _build_route_service_preview(
    *,
    project_id: str,
    root_backlog_ids: Sequence[str],
    backlog_rows: Sequence[Mapping[str, Any]],
    classified_rows: Sequence[Mapping[str, Any]],
    lanes: Sequence[Mapping[str, Any]],
    route_context: Mapping[str, Any],
    graph_status: Mapping[str, Any],
    version_check: Mapping[str, Any],
    repair_run_id: str,
) -> dict[str, Any]:
    try:
        from .contract_template_registry import get_contract_template
        from .service_router import route_event

        contract = get_contract_template(ROUTE_WORKFLOW_TEMPLATE_ID)
        prompt_payload = _route_prompt_payload(
            project_id=project_id,
            root_backlog_ids=root_backlog_ids,
            backlog_rows=backlog_rows,
            classified_rows=classified_rows,
            route_context=route_context,
            repair_run_id=repair_run_id,
        )
        prompt_event = {
            "event_id": f"{repair_run_id}:route_prompt_context",
            "event_kind": ROUTE_PROMPT_EVENT_KIND,
            "stage": ROUTE_SERVICE_STAGE,
            "project_id": project_id,
            "backlog_id": root_backlog_ids[0] if len(root_backlog_ids) == 1 else "",
            "payload": prompt_payload,
        }
        prompt_result = route_event(prompt_event, contract)
        prompt_route = _first_route_result(prompt_result)
        prompt_route_result = _object(prompt_route.get("result"))
        bundle = _object(
            prompt_route_result.get("route_prompt_bundle")
            or prompt_route_result.get("bundle")
        )
        route_identity = _service_route_identity(prompt_route, bundle)
        action_prechecks: list[dict[str, Any]] = []
        if bundle and route_identity.get("route_context_hash"):
            precheck_specs = [
                {
                    "precheck_id": "observer_dispatch_bounded_worker",
                    "lane_id": "subsystem_evidence",
                    "caller_role": "observer",
                    "action": "dispatch_bounded_worker",
                },
                {
                    "precheck_id": "implementation_worker_apply_patch",
                    "lane_id": "subsystem_evidence",
                    "caller_role": "implementation_worker",
                    "action": "apply_patch",
                },
                {
                    "precheck_id": "independent_verification_lane",
                    "lane_id": "independent_verification",
                    "caller_role": "qa",
                    "action": "run_independent_verification",
                },
                {
                    "precheck_id": "observer_close_gate_precheck",
                    "lane_id": "close_gate",
                    "caller_role": "observer",
                    "action": "run_close_gate_precheck",
                },
            ]
            active_lane_ids = {str(lane.get("lane_id")) for lane in lanes}
            for spec in precheck_specs:
                if spec["lane_id"] not in active_lane_ids and spec["lane_id"] != "subsystem_evidence":
                    continue
                payload = _route_action_precheck_payload(
                    action=spec["action"],
                    caller_role=spec["caller_role"],
                    bundle=bundle,
                    route_identity=route_identity,
                    graph_status=graph_status,
                    version_check=version_check,
                )
                action_event = {
                    "event_id": f"{repair_run_id}:{spec['precheck_id']}",
                    "event_kind": ROUTE_ACTION_EVENT_KIND,
                    "stage": ROUTE_SERVICE_STAGE,
                    "project_id": project_id,
                    "backlog_id": root_backlog_ids[0] if len(root_backlog_ids) == 1 else "",
                    "payload": payload,
                }
                action_result = route_event(action_event, contract)
                action_prechecks.append(
                    {
                        **spec,
                        "result": _route_service_summary(action_result),
                        "route_action_gate": _object(
                            _object(_first_route_result(action_result).get("result")).get(
                                "route_action_gate"
                            )
                        ),
                    }
                )
        return {
            "schema_version": ROUTE_SERVICE_PREVIEW_SCHEMA_VERSION,
            "available": True,
            "template_id": ROUTE_WORKFLOW_TEMPLATE_ID,
            "stage": ROUTE_SERVICE_STAGE,
            "prompt_context_event": {
                "event_kind": ROUTE_PROMPT_EVENT_KIND,
                "stage": ROUTE_SERVICE_STAGE,
                "event_id": prompt_event["event_id"],
            },
            "prompt_context_result": _route_service_summary(prompt_result),
            "prompt_bundle": bundle,
            "service_generated_route_identity": route_identity,
            "action_prechecks": action_prechecks,
            "counts_as_close_evidence": False,
            "authorizes_protected_write": False,
        }
    except Exception as exc:
        return {
            "schema_version": ROUTE_SERVICE_PREVIEW_SCHEMA_VERSION,
            "available": False,
            "template_id": ROUTE_WORKFLOW_TEMPLATE_ID,
            "error": type(exc).__name__,
            "message": str(exc),
            "counts_as_close_evidence": False,
            "authorizes_protected_write": False,
        }


def build_repair_run_plan(
    *,
    project_id: str,
    root_backlog_ids: Sequence[str],
    backlog_rows: Sequence[Mapping[str, Any]] = (),
    blockers: Sequence[Any] = (),
    graph_status: Mapping[str, Any] | None = None,
    operations_queue: Mapping[str, Any] | None = None,
    version_check: Mapping[str, Any] | None = None,
    timeline_prechecks: Sequence[Mapping[str, Any]] = (),
    route_context_seed: Mapping[str, Any] | None = None,
    actor: str = "observer",
) -> dict[str, Any]:
    """Build a deterministic, replayable observer repair-run plan."""

    project = _string(project_id)
    roots = sorted({_string(item) for item in root_backlog_ids if _string(item)})
    normalized_rows = [dict(row) for row in backlog_rows if isinstance(row, Mapping)]
    rows_by_id = {_string(row.get("bug_id")): row for row in normalized_rows if _string(row.get("bug_id"))}
    classified_rows = [_classify_backlog_row(row) for row in normalized_rows]
    blocker_inputs = [str(item.get("error") or item.get("message") or item) if isinstance(item, Mapping) else str(item) for item in blockers]
    blocker_classes = [classify_text(item) for item in blocker_inputs if item.strip()]
    synthetic_rows: list[dict[str, Any]] = []
    if not classified_rows and blocker_classes:
        for idx, classification in enumerate(blocker_classes, start=1):
            synthetic_rows.append(
                {
                    "bug_id": f"blocker:{idx}",
                    "status": "blocked",
                    "priority": "P0",
                    "lane_ids": sorted({match["lane_id"] for match in classification["matches"]}, key=lambda lane: LANE_PRIORITY[lane]),
                    "blocker_ids": sorted({match["blocker_id"] for match in classification["matches"]}),
                    "recovery_actions": sorted({match["recovery_action"] for match in classification["matches"]}),
                }
            )
        classified_rows = synthetic_rows
    seed = _object(route_context_seed)
    route_context = _build_route_context(
        project,
        roots,
        {
            **seed,
            "actor": actor,
            "backlog_count": len(normalized_rows),
            "blocker_count": len(blocker_classes),
        },
    )
    lanes = _build_lanes(classified_rows)
    dag = _build_dependency_dag(classified_rows, rows_by_id)
    repair_run_id = f"repair-{_stable_hash({'project_id': project, 'roots': roots, 'route': route_context})}"
    route_service_preview = _build_route_service_preview(
        project_id=project,
        root_backlog_ids=roots,
        backlog_rows=normalized_rows,
        classified_rows=classified_rows,
        lanes=lanes,
        route_context=route_context,
        graph_status=graph_status or {},
        version_check=version_check or {},
        repair_run_id=repair_run_id,
    )
    recovery_actions = sorted(
        {
            action
            for row in classified_rows
            for action in _list(row.get("recovery_actions"))
        }
        | {
            match["recovery_action"]
            for classification in blocker_classes
            for match in classification["matches"]
        }
    )
    graph_stale = bool(
        _object(_object(graph_status).get("current_state"))
        .get("graph_stale", {})
        .get("is_stale", False)
    )
    operation_count = int(_object(operations_queue).get("count") or 0)
    timeline_blocked = [
        {
            "bug_id": _string(item.get("bug_id")),
            "can_close": bool(item.get("can_close")),
            "missing": _object(item.get("timeline_gate")).get("missing_event_kinds", []),
        }
        for item in timeline_prechecks
        if isinstance(item, Mapping) and item.get("can_close") is False
    ]
    return {
        "ok": True,
        "schema_version": SCHEMA_VERSION,
        "repair_run_id": repair_run_id,
        "project_id": project,
        "actor": actor,
        "root_backlog_ids": roots,
        "route_context": route_context,
        "route_service_preview": route_service_preview,
        "backlog_dependency_dag": dag,
        "lane_dispatches": lanes,
        "checkpoints": _build_checkpoints(route_context, lanes),
        "blocker_classification": blocker_classes,
        "recovery_actions": recovery_actions,
        "runtime_independent_of_judgment_brain": True,
        "protected_write_policy": {
            "plan_is_read_only": True,
            "requires_route_token_for_protected_writes": True,
            "diagnostic_events_count_as_close_evidence": False,
        },
        "graph_summary": {
            "graph_stale": graph_stale,
            "operation_count": operation_count,
        },
        "timeline_precheck_summary": {
            "blocked_count": len(timeline_blocked),
            "blocked": timeline_blocked,
        },
        "next_legal_actions": recovery_actions or ["inspect_evidence_and_file_bounded_followup"],
    }
