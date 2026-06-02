"""Backend-neutral contract for MF subagent branch workers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from agent.governance.asset_binding_proposals import (
    PRECHECK_SCHEMA_VERSION as ASSET_BINDING_PRECHECK_SCHEMA_VERSION,
    PROPOSAL_SCHEMA_VERSION as ASSET_BINDING_PROPOSAL_SCHEMA_VERSION,
)
from agent.governance.parallel_branch_runtime import BranchTaskRuntimeContext


MF_SUB_ROLE = "mf_sub"
OBSERVER_COORDINATOR_ROLE = "observer"
INPUT_SCHEMA_VERSION = "mf_subagent_input.v1"
RESULT_SCHEMA_VERSION = "mf_subagent_result.v1"
FINISH_GATE_SCHEMA_VERSION = "mf_subagent_finish_gate.v1"
DISPATCH_GATE_SCHEMA_VERSION = "mf_subagent_dispatch_gate.v1"
OBSERVER_DIRECT_MUTATION_SCHEMA_VERSION = "observer_direct_mutation_exception.v1"
ROUTE_ACTION_GATE_SCHEMA_VERSION = "route_action_gate.v1"
ROUTE_TOKEN_MUTATION_GATE_SCHEMA_VERSION = "route_token_mutation_gate.v1"
ROUTE_TOKEN_REQUIRED_FAILURE_SCHEMA_VERSION = "route_token_required_failure.v1"
FINISH_GATE_REPLAY_SOURCE = "mf_sub_finish_gate"
BACKEND_CONTRACT = "parallel_branch_worker.v1"
DISPATCH_DEFAULT = "non_blocking_after_gate"
WORKTREE_POLICY_MODE = "isolated_worktree_required"
OBSERVER_DIRECT_MUTATION_DEFAULT = "reject"
ROUTE_OBSERVER_JUDGER_BLOCK_ALERT = "observer_judger_must_not_implement"
ROUTE_OBSERVER_INDEPENDENT_REVIEWER_BLOCK_ALERT = (
    "observer_independent_reviewer_must_not_implement"
)
ROUTE_DIRECT_IMPLEMENTATION_BLOCK_ALERTS = {
    ROUTE_OBSERVER_JUDGER_BLOCK_ALERT,
    ROUTE_OBSERVER_INDEPENDENT_REVIEWER_BLOCK_ALERT,
}

MF_SUB_ALLOWED_CAPABILITIES = (
    "modify_code",
    "run_tests",
    "git_diff",
    "checkpoint_branch_task",
    "report_blocker",
)
MF_SUB_FORBIDDEN_ACTIONS = (
    "merge",
    "push",
    "activate_graph",
    "release_gate",
    "create_task",
    "delete_worktree",
    "modify_merge_queue",
)
MF_SUB_REQUIRED_OUTPUT = (
    "status",
    "changed_files",
    "test_results",
    "checkpoint_id",
    "fence_token",
)

_REQUIRED_CONTEXT_FIELDS = (
    "project_id",
    "task_id",
    "backlog_id",
    "branch_ref",
    "worktree_path",
    "base_commit",
    "target_head_commit",
    "merge_queue_id",
    "fence_token",
)
_READY_STATUSES = {"completed", "succeeded", "ready_for_merge"}
_PASS_STATUSES = {"pass", "passed", "ok", "succeeded", "success", "clean"}
_FAIL_STATUSES = {
    "block",
    "blocked",
    "deny",
    "denied",
    "error",
    "errored",
    "fail",
    "failed",
    "not_allowed",
    "reject",
    "rejected",
}
_FORBIDDEN_RESULT_FLAGS = {
    "merge_commit": "merge",
    "push_performed": "push",
    "graph_activated": "activate_graph",
    "release_gate_passed": "release_gate",
    "task_created": "create_task",
    "worktree_deleted": "delete_worktree",
}
_FINISH_IDENTITY_FIELDS = (
    "project_id",
    "task_id",
    "backlog_id",
    "branch_ref",
    "worktree_path",
    "base_commit",
    "target_head_commit",
    "merge_queue_id",
)
_DISPATCH_REQUIRED_FIELDS = (
    "branch",
    "worktree",
    "base_commit",
    "target_head_commit",
    "merge_queue_id",
    "fence_token",
    "route_context_hash",
    "prompt_contract_id",
)
_IMPLEMENTATION_ACTIONS = {
    "apply_patch",
    "apply_patch_within_target_files",
    "edit_file",
    "edit_files",
    "implementation_exec",
    "implementation_file_edit",
    "mutate_files",
    "run_implementation_command",
    "write_file",
    "write_files",
}
_OBSERVER_JUDGER_ROLES = {
    "observer",
    "judger",
    "reviewer",
    "independent_reviewer",
    "observer_independent_reviewer",
}
_WORKER_ROLES = {
    "implementation_worker",
    "mf_sub",
    "mf_subagent",
    "subagent",
    "worker",
}
_HIGH_RISK_ROUTE_PRIORITIES = {"P0", "P1"}
_PARALLEL_ROUTE_TOPOLOGIES = {
    "mf_parallel",
    "mf_parallel_v1",
    "mf_parallel.v1",
    "observer_led_parallel_lanes",
    "parallel",
    "parallel_lanes",
}
_HIGH_RISK_ROUTE_PATH_MARKERS = (
    "agent/governance/",
    "agent/mcp/",
    "frontend/dashboard/",
    "shared-volume/",
    "docs/governance/",
    "skills/aming-claw/",
)
_ROUTE_PROVIDER_STATUS_CONTAINER_KEYS = (
    "provider_runtime_status",
    "mcp_runtime_status",
    "runtime_status",
    "route_provider_runtime_status",
    "route_context_runtime_status",
    "route_precheck_runtime_status",
    "route_provider_status",
    "provider_status",
    "route_context_status",
)
_ROUTE_PROVIDER_HASH_PAIRS = (
    ("loaded_source_hash", "current_source_hash"),
    ("loaded_provider_source_hash", "current_provider_source_hash"),
    ("loaded_route_source_hash", "current_route_source_hash"),
)
_ROUTE_TOKEN_WAIVER_TYPES = {
    "manual_fix",
    "manual-fix",
    "manual_fix_route_gate",
    "observer_manual_fix",
    "same_worktree",
    "same-worktree",
}
_ROUTE_TOKEN_REQUIRED_FIELDS = (
    "route_context_hash",
    "prompt_contract_id",
    "caller_role",
    "allowed_action",
    "expires_at",
    "evidence_refs",
)


class MfSubagentContractError(ValueError):
    """Raised when an MF subagent payload violates the worker contract."""


def _string(value: Any) -> str:
    return str(value or "").strip()


def _string_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        raise MfSubagentContractError(f"{field_name} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise MfSubagentContractError(f"{field_name} must be a list of strings")
        if item:
            result.append(item)
    return result


def _mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MfSubagentContractError(f"{field_name} must be a mapping")
    return dict(value)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _explicit_false(value: Any) -> bool:
    if isinstance(value, bool):
        return value is False
    if isinstance(value, str):
        return value.strip().lower() in {"0", "false", "no", "n", "off"}
    return False


def _normalize_worktree_path(path: str) -> str:
    token = _string(path)
    if not token:
        return ""
    return str(Path(token).expanduser().resolve())


def _nested_mapping(payload: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def _dispatch_string(
    payload: Mapping[str, Any],
    *,
    names: Sequence[str],
    nested_keys: Sequence[tuple[str, Sequence[str]]] = (),
) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, Mapping):
            for nested_name in (
                "branch_ref",
                "ref_name",
                "name",
                "path",
                "worktree_path",
            ):
                token = _string(value.get(nested_name))
                if token:
                    return token
        token = _string(value)
        if token:
            return token
    for parent_key, child_names in nested_keys:
        nested = _nested_mapping(payload, parent_key)
        for child_name in child_names:
            token = _string(nested.get(child_name))
            if token:
                return token
    return ""


def _route_prompt_contract_id(payload: Mapping[str, Any]) -> str:
    prompt_contract = _nested_mapping(payload, "prompt_contract")
    route_prompt_contract = _nested_mapping(payload, "route_prompt_contract")
    route_context = _nested_mapping(payload, "route_context")
    route_prompt_bundle = _nested_mapping(payload, "route_prompt_bundle")
    bundle = _nested_mapping(payload, "bundle")
    route_prompt_bundle_prompt_contract = _nested_mapping(
        route_prompt_bundle, "prompt_contract"
    )
    bundle_prompt_contract = _nested_mapping(bundle, "prompt_contract")
    return _string(
        payload.get("prompt_contract_id")
        or prompt_contract.get("prompt_contract_id")
        or prompt_contract.get("id")
        or route_prompt_contract.get("prompt_contract_id")
        or route_prompt_contract.get("id")
        or route_context.get("prompt_contract_id")
        or route_prompt_bundle.get("prompt_contract_id")
        or route_prompt_bundle_prompt_contract.get("prompt_contract_id")
        or route_prompt_bundle_prompt_contract.get("id")
        or bundle.get("prompt_contract_id")
        or bundle_prompt_contract.get("prompt_contract_id")
        or bundle_prompt_contract.get("id")
    )


def _route_context_hash(payload: Mapping[str, Any]) -> str:
    prompt_contract = _nested_mapping(payload, "prompt_contract")
    route_prompt_contract = _nested_mapping(payload, "route_prompt_contract")
    route_context = _nested_mapping(payload, "route_context")
    route_prompt_bundle = _nested_mapping(payload, "route_prompt_bundle")
    bundle = _nested_mapping(payload, "bundle")
    return _string(
        payload.get("route_context_hash")
        or route_context.get("route_context_hash")
        or prompt_contract.get("route_context_hash")
        or route_prompt_contract.get("route_context_hash")
        or route_prompt_bundle.get("route_context_hash")
        or bundle.get("route_context_hash")
    )


def _route_prompt_contract_hash(payload: Mapping[str, Any]) -> str:
    prompt_contract = _nested_mapping(payload, "prompt_contract")
    route_prompt_contract = _nested_mapping(payload, "route_prompt_contract")
    route_context = _nested_mapping(payload, "route_context")
    route_prompt_bundle = _nested_mapping(payload, "route_prompt_bundle")
    bundle = _nested_mapping(payload, "bundle")
    return _string(
        payload.get("prompt_contract_hash")
        or prompt_contract.get("prompt_contract_hash")
        or route_context.get("prompt_contract_hash")
        or route_prompt_contract.get("prompt_contract_hash")
        or route_prompt_bundle.get("prompt_contract_hash")
        or bundle.get("prompt_contract_hash")
    )


def _alert_codes(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise MfSubagentContractError("route_alerts must be a list of alerts")
    codes: list[str] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            code = _string(item)
        elif isinstance(item, Mapping):
            code = _string(item.get("code"))
        else:
            continue
        if code and code not in seen:
            codes.append(code)
            seen.add(code)
    return codes


def _route_alert_codes(payload: Mapping[str, Any]) -> list[str]:
    candidates = [
        payload.get("route_alerts"),
        payload.get("alerts"),
    ]
    for key in ("route_context", "route_prompt_bundle", "bundle"):
        nested = _nested_mapping(payload, key)
        candidates.extend([nested.get("route_alerts"), nested.get("alerts")])

    codes: list[str] = []
    seen: set[str] = set()
    for alerts in candidates:
        for code in _alert_codes(alerts):
            if code and code not in seen:
                codes.append(code)
                seen.add(code)
    return codes


def _normalized_action(value: Any) -> str:
    return _string(value).lower().replace("-", "_").replace(".", "_")


def _route_action_name(payload: Mapping[str, Any], action: str = "") -> str:
    candidates: list[Any] = [
        action,
        payload.get("action"),
        payload.get("requested_action"),
        payload.get("tool_name"),
    ]
    for container in _route_identity_containers(payload):
        candidates.extend([
            container.get("action"),
            container.get("requested_action"),
            container.get("tool_name"),
        ])
    for candidate in candidates:
        token = _normalized_action(candidate)
        if token:
            return token
    return ""


def _route_caller_role(payload: Mapping[str, Any]) -> str:
    candidates: list[Any] = [
        payload.get("caller_role"),
        payload.get("role"),
        payload.get("actor_role"),
    ]
    for container in _route_identity_containers(payload):
        candidates.extend([
            container.get("caller_role"),
            container.get("role"),
            container.get("actor_role"),
        ])
    for candidate in candidates:
        token = _string(candidate).lower()
        if token:
            return token
    return ""


def _route_machine_containers(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    containers: list[Mapping[str, Any]] = [payload]
    for key in (
        "route",
        "route_context",
        "route_prompt_bundle",
        "bundle",
        "prompt_contract",
        "route_prompt_contract",
        "worker_prompt_contract",
        "verification_policy",
        "hashes",
    ):
        nested = _nested_mapping(payload, key)
        if nested:
            containers.append(nested)
            for child_key in (
                "route",
                "prompt_contract",
                "route_prompt_contract",
                "worker_prompt_contract",
                "verification_policy",
                "hashes",
            ):
                child = _nested_mapping(nested, child_key)
                if child:
                    containers.append(child)
    return containers


def _route_text_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Mapping):
        for key in (
            "id",
            "name",
            "role",
            "action",
            "allowed_action",
            "requirement_id",
            "evidence_id",
        ):
            token = _string(value.get(key))
            if token:
                return [token]
        return ["<mapping>"] if value else []
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        result: list[str] = []
        for item in value:
            result.extend(_route_text_values(item))
        return result
    token = _string(value)
    return [token] if token else []


def _route_collect_texts(
    payload: Mapping[str, Any],
    *field_names: str,
) -> list[str]:
    values: list[str] = []
    for container in _route_machine_containers(payload):
        for field_name in field_names:
            values.extend(_route_text_values(container.get(field_name)))
    return _dedupe_strings(values)


def _route_alert_mappings(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    alerts: list[Mapping[str, Any]] = []
    for container in _route_machine_containers(payload):
        for key in ("route_alerts", "alerts"):
            value = container.get(key)
            if isinstance(value, Mapping):
                alerts.append(value)
            elif isinstance(value, Sequence) and not isinstance(
                value, (str, bytes, bytearray)
            ):
                alerts.extend(item for item in value if isinstance(item, Mapping))
    return alerts


def _route_blocked_actions(payload: Mapping[str, Any]) -> list[str]:
    values = _route_collect_texts(payload, "blocked_actions", "blocked_action")
    for alert in _route_alert_mappings(payload):
        values.extend(_route_text_values(alert.get("blocked_actions")))
    return _dedupe_strings(values)


def _route_hard_blocked_actions(
    payload: Mapping[str, Any],
    *,
    caller_role: str,
) -> list[str]:
    values = _route_collect_texts(payload, "blocked_actions", "blocked_action")
    for alert in _route_alert_mappings(payload):
        if not _route_alert_applies_to_role(alert, caller_role=caller_role):
            continue
        alert_code = _string(alert.get("code"))
        if (
            caller_role in _OBSERVER_JUDGER_ROLES
            and alert_code in ROUTE_DIRECT_IMPLEMENTATION_BLOCK_ALERTS
        ):
            continue
        values.extend(_route_text_values(alert.get("blocked_actions")))
    return _dedupe_strings(values)


def _route_alert_applies_to_role(alert: Mapping[str, Any], *, caller_role: str) -> bool:
    applies_to = {
        _normalized_action(item)
        for item in _route_text_values(alert.get("applies_to"))
    }
    if not applies_to:
        return True
    role = _normalized_action(caller_role)
    if not role:
        return True
    aliases = {role}
    if role in {"implementation_worker", "implementation", "worker", "mf_sub"}:
        aliases.update({"implementation_worker", "implementation", "worker", "mf_sub"})
    if role in {"qa", "reviewer", "independent_reviewer", "verification"}:
        aliases.update({"qa", "reviewer", "independent_reviewer", "verification"})
    if role in {"observer", "judger", "judge"}:
        aliases.update({"observer", "judger", "judge"})
    return bool(aliases.intersection(applies_to))


def _route_explicit_allowed_actions(payload: Mapping[str, Any]) -> list[str]:
    return _route_collect_texts(payload, "allowed_actions", "allowed_action")


def _route_required_lanes(payload: Mapping[str, Any]) -> list[str]:
    return _route_collect_texts(payload, "required_lanes", "required_lane")


def _route_required_evidence(payload: Mapping[str, Any]) -> list[str]:
    return _route_collect_texts(
        payload,
        "required_evidence",
        "required_evidence_ids",
        "evidence_required",
        "evidence_requirements",
        "contract_evidence",
    )


def _route_visible_injection_manifest_present(payload: Mapping[str, Any]) -> bool:
    for container in _route_machine_containers(payload):
        if _string(container.get("visible_injection_manifest_hash")):
            return True
        hashes = _nested_mapping(container, "hashes")
        if _string(hashes.get("visible_injection_manifest_hash")):
            return True
        manifest = container.get("visible_injection_manifest")
        if isinstance(manifest, Mapping) and bool(manifest):
            return True
    return False


def _route_priority(payload: Mapping[str, Any]) -> str:
    for container in _route_machine_containers(payload):
        for key in ("priority", "severity", "risk_priority"):
            token = _string(container.get(key)).upper()
            if token:
                return token
    return ""


def _route_topology_values(payload: Mapping[str, Any]) -> list[str]:
    values = _route_collect_texts(
        payload,
        "selected_topology",
        "recommended_topology",
        "topology",
    )
    return [_normalized_action(value) for value in values if _string(value)]


def _route_file_values(payload: Mapping[str, Any]) -> list[str]:
    return _route_collect_texts(
        payload,
        "target_files",
        "test_files",
        "changed_files",
        "owned_files",
        "write_scope",
    )


def _route_cross_module_change(files: Sequence[str]) -> bool:
    normalized = [_string(path).replace("\\", "/") for path in files if _string(path)]
    buckets = {"/".join(path.split("/")[:2]) for path in normalized}
    return len(normalized) > 3 or len(buckets) > 1


def _route_action_high_risk_policy(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    priority = _route_priority(payload)
    topologies = _route_topology_values(payload)
    files = _route_file_values(payload)
    required_lanes = {
        _normalized_action(item) for item in _route_required_lanes(payload) if item
    }
    required_evidence = {
        _normalized_action(item) for item in _route_required_evidence(payload) if item
    }
    risk_class = _string(
        payload.get("risk_class")
        or payload.get("risk")
        or _nested_mapping(payload, "route_context").get("risk_class")
    ).lower()
    reason_codes: list[str] = []
    if priority in _HIGH_RISK_ROUTE_PRIORITIES:
        reason_codes.append(f"priority_{priority.lower()}")
    if any(topology in _PARALLEL_ROUTE_TOPOLOGIES for topology in topologies):
        reason_codes.append("parallel_route_topology")
    if required_lanes.intersection(
        {
            "bounded_implementation_worker",
            "independent_verification_lane",
            "observer_led_parallel_lanes",
            "parallel_lanes",
        }
    ):
        reason_codes.append("parallel_required_lanes")
    if required_evidence.intersection(
        {
            "bounded_implementation_worker_dispatch",
            "mf_subagent_dispatch",
            "mf_subagent_startup",
            "bounded_dispatch_evidence",
        }
    ):
        reason_codes.append("bounded_worker_evidence_required")
    if _route_cross_module_change(files):
        reason_codes.append("cross_module_change")
    if any(
        any(marker in _string(path).replace("\\", "/") for marker in _HIGH_RISK_ROUTE_PATH_MARKERS)
        for path in files
    ):
        reason_codes.append("high_risk_governance_surface")
    if risk_class in {"high", "critical", "p0", "p1", "high_risk"}:
        reason_codes.append("explicit_high_risk")
    return {
        "required": bool(reason_codes),
        "priority": priority,
        "topologies": topologies,
        "file_count": len(files),
        "reason_codes": _dedupe_strings(reason_codes),
    }


def _route_provider_unavailable_reason(payload: Mapping[str, Any]) -> str:
    bool_reason_fields = {
        "route_provider_unavailable": "route provider unavailable",
        "route_context_unavailable": "route context unavailable",
        "route_precheck_provider_unavailable": "route precheck provider unavailable",
        "provider_unavailable": "route provider unavailable",
        "mcp_unavailable": "route provider unavailable",
        "runtime_unavailable": "route runtime unavailable",
        "unavailable": "route provider unavailable",
        "transport_closed": "route provider transport closed",
        "transport_is_closed": "route provider transport closed",
        "closed": "route provider transport closed",
        "route_context_stale": "route context stale",
        "route_evidence_stale": "route evidence stale",
        "stale_route_evidence": "route evidence stale",
        "stale": "route runtime stale",
        "runtime_stale": "route runtime stale",
        "provider_stale": "route provider stale",
        "mcp_stale": "route MCP runtime stale",
    }
    status_fields = (
        "status",
        "state",
        "runtime_state",
        "transport_status",
        "connection_status",
        "availability",
        "route_provider_status",
        "provider_status",
        "mcp_status",
        "runtime_status",
        "route_context_status",
        "route_evidence_status",
        "route_action_precheck_status",
    )
    error_fields = (
        "route_provider_error",
        "provider_error",
        "route_context_error",
        "route_precheck_error",
        "error",
        "last_error",
        "message",
    )
    for prefix, container in _route_provider_status_containers(payload):
        for field_name, reason in bool_reason_fields.items():
            if _bool(container.get(field_name)):
                return f"{prefix}.{field_name}=True" if prefix else reason
        if _explicit_false(container.get("available")):
            return f"{prefix}.available=False" if prefix else "route provider unavailable"
        hash_mismatch = _route_provider_hash_mismatch(container)
        if hash_mismatch:
            return f"{prefix}.{hash_mismatch}" if prefix else hash_mismatch
        for field_name in status_fields:
            status = _normalized_action(container.get(field_name))
            if status in {
                "unavailable",
                "provider_unavailable",
                "route_provider_unavailable",
                "mcp_unavailable",
                "runtime_unavailable",
                "transport_closed",
                "transportclosed",
                "connection_closed",
                "closed",
                "stale",
                "runtime_stale",
                "provider_stale",
                "mcp_stale",
                "stale_evidence",
                "route_context_stale",
                "route_evidence_stale",
                "hash_mismatch",
                "source_hash_mismatch",
                "stale_hash_mismatch",
            }:
                name = f"{prefix}.{field_name}" if prefix else field_name
                return f"{name}={_string(container.get(field_name))}"
        for field_name in error_fields:
            raw_text = _string(container.get(field_name))
            text = raw_text.lower()
            normalized = _normalized_action(raw_text)
            if (
                "transport closed" in text
                or "transport_closed" in normalized
                or "transportclosed" in normalized
                or "closed transport" in text
                or "connection closed" in text
                or "provider unavailable" in text
                or "route context unavailable" in text
                or "stale route" in text
                or "route evidence stale" in text
                or "source hash mismatch" in text
            ):
                name = f"{prefix}.{field_name}" if prefix else field_name
                return f"{name}: {raw_text}" if prefix else raw_text
    return ""


def _route_provider_status_containers(
    payload: Mapping[str, Any],
) -> list[tuple[str, Mapping[str, Any]]]:
    containers: list[tuple[str, Mapping[str, Any]]] = [
        ("", container) for container in _route_machine_containers(payload)
    ]
    seen = {id(container) for _, container in containers}
    for parent_prefix, parent in list(containers):
        for key in _ROUTE_PROVIDER_STATUS_CONTAINER_KEYS:
            child = parent.get(key)
            if not isinstance(child, Mapping) or id(child) in seen:
                continue
            prefix = f"{parent_prefix}.{key}" if parent_prefix else key
            containers.append((prefix, child))
            seen.add(id(child))
    return containers


def _route_provider_hash_mismatch(container: Mapping[str, Any]) -> str:
    for loaded_key, current_key in _ROUTE_PROVIDER_HASH_PAIRS:
        loaded = _string(container.get(loaded_key))
        current = _string(container.get(current_key))
        if loaded and current and loaded != current:
            return f"{loaded_key}/{current_key}_mismatch"
    return ""


def _route_identity_containers(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    containers: list[Mapping[str, Any]] = []
    route = _nested_mapping(payload, "route")
    if route:
        containers.append(route)
    for key in ("route_context", "route_prompt_bundle", "bundle"):
        nested = _nested_mapping(payload, key)
        if nested:
            containers.append(nested)
            nested_route = _nested_mapping(nested, "route")
            if nested_route:
                containers.append(nested_route)
    return containers


def _accepted_waiver_matches(
    waiver: Mapping[str, Any],
    *,
    route_context_hash: str,
    prompt_contract_id: str,
    prompt_contract_hash: str,
) -> bool:
    status = _string(waiver.get("status") or waiver.get("decision")).lower()
    accepted = _bool(waiver.get("accepted")) or status in {
        "accepted",
        "approved",
        "allow",
        "allowed",
        "waived",
    }
    if not accepted:
        return False
    waiver_prompt_hash = _string(waiver.get("prompt_contract_hash"))
    return (
        _string(waiver.get("route_context_hash")) == route_context_hash
        and _string(waiver.get("prompt_contract_id")) == prompt_contract_id
        and (
            not waiver_prompt_hash
            or not prompt_contract_hash
            or waiver_prompt_hash == prompt_contract_hash
        )
    )


def _dispatch_evidence_matches(
    evidence: Mapping[str, Any],
    *,
    route_context_hash: str,
    prompt_contract_id: str,
    prompt_contract_hash: str,
) -> bool:
    status = _string(evidence.get("status") or evidence.get("decision")).lower()
    if (
        _explicit_false(evidence.get("allowed"))
        or _explicit_false(evidence.get("ok"))
        or status in _FAIL_STATUSES
    ):
        return False
    allowed = (
        _bool(evidence.get("allowed"))
        or _bool(evidence.get("ok"))
        or status in _PASS_STATUSES
        or status in {"allow", "allowed"}
        or _string(evidence.get("schema_version")) == DISPATCH_GATE_SCHEMA_VERSION
    )
    role = _string(
        evidence.get("role") or evidence.get("worker_role") or evidence.get("caller_role")
    ).lower()
    worker_role_ok = not role or role in _WORKER_ROLES or role == MF_SUB_ROLE
    evidence_prompt_hash = _string(evidence.get("prompt_contract_hash"))
    return (
        allowed
        and worker_role_ok
        and _string(evidence.get("route_context_hash")) == route_context_hash
        and _string(evidence.get("prompt_contract_id")) == prompt_contract_id
        and (
            not evidence_prompt_hash
            or not prompt_contract_hash
            or evidence_prompt_hash == prompt_contract_hash
        )
    )


def _fence_evidence_present(evidence: Mapping[str, Any]) -> bool:
    for key in (
        "fence_token",
        "worker_fence_token",
        "route_fence_token",
        "actual_fence_token",
        "reported_fence_token",
    ):
        if _string(evidence.get(key)):
            return True
    for key in (
        "fence_token_present",
        "actual_fence_token_present",
        "fence_token_matches",
    ):
        if _bool(evidence.get(key)):
            return True
    if _string(evidence.get("fence_token_hash")):
        return True
    fence = _nested_mapping(evidence, "fence")
    if _string(fence.get("token") or fence.get("fence_token") or fence.get("hash")):
        return True
    return False


def _bounded_dispatch_evidence_present(evidence: Mapping[str, Any]) -> bool:
    if _explicit_false(evidence.get("bounded")):
        return False
    return (
        _bool(evidence.get("bounded"))
        or _string(evidence.get("schema_version")) == DISPATCH_GATE_SCHEMA_VERSION
        or bool(
            _string(evidence.get("worktree") or evidence.get("worktree_path"))
            and _string(evidence.get("fence_token"))
        )
    )


def _bounded_startup_evidence_present(evidence: Mapping[str, Any]) -> bool:
    if _explicit_false(evidence.get("bounded")):
        return False
    gate_kind = _string(evidence.get("gate_kind") or evidence.get("kind")).lower()
    return (
        _bool(evidence.get("bounded"))
        or gate_kind == "mf_subagent.startup"
        or _bool(evidence.get("same_as_expected_worker"))
        or _bool(evidence.get("fence_token_matches"))
        or bool(
            _string(evidence.get("worktree") or evidence.get("worktree_path"))
            and _fence_evidence_present(evidence)
        )
    )


def _startup_evidence_matches(
    evidence: Mapping[str, Any],
    *,
    route_context_hash: str,
    prompt_contract_id: str,
    prompt_contract_hash: str,
) -> bool:
    status = _string(evidence.get("status") or evidence.get("decision")).lower()
    if (
        _explicit_false(evidence.get("allowed"))
        or _explicit_false(evidence.get("ok"))
        or status in _FAIL_STATUSES
    ):
        return False
    gate_kind = _string(evidence.get("gate_kind") or evidence.get("kind")).lower()
    allowed = (
        _bool(evidence.get("allowed"))
        or _bool(evidence.get("ok"))
        or status in _PASS_STATUSES
        or status in {"allow", "allowed"}
        or gate_kind == "mf_subagent.startup"
        or _bool(evidence.get("started"))
        or _bool(evidence.get("startup_complete"))
        or _bool(evidence.get("same_as_expected_worker"))
    )
    role = _string(
        evidence.get("role") or evidence.get("worker_role") or evidence.get("caller_role")
    ).lower()
    worker_role_ok = not role or role in _WORKER_ROLES or role == MF_SUB_ROLE
    evidence_prompt_hash = _string(evidence.get("prompt_contract_hash"))
    return (
        allowed
        and worker_role_ok
        and _string(evidence.get("route_context_hash")) == route_context_hash
        and _string(evidence.get("prompt_contract_id")) == prompt_contract_id
        and (
            not evidence_prompt_hash
            or not prompt_contract_hash
            or evidence_prompt_hash == prompt_contract_hash
        )
    )


def _route_startup_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _mapping(
        payload.get("bounded_startup_evidence")
        or payload.get("startup_evidence")
        or payload.get("mf_subagent_startup_gate"),
        field_name="bounded_startup_evidence",
    )


def _bounded_worker_evidence_matches(
    dispatch_evidence: Mapping[str, Any],
    startup_evidence: Mapping[str, Any],
    *,
    route_context_hash: str,
    prompt_contract_id: str,
    prompt_contract_hash: str,
) -> dict[str, Any]:
    dispatch_matches = _dispatch_evidence_matches(
        dispatch_evidence,
        route_context_hash=route_context_hash,
        prompt_contract_id=prompt_contract_id,
        prompt_contract_hash=prompt_contract_hash,
    )
    startup_matches = _startup_evidence_matches(
        startup_evidence,
        route_context_hash=route_context_hash,
        prompt_contract_id=prompt_contract_id,
        prompt_contract_hash=prompt_contract_hash,
    )
    dispatch_fence = _fence_evidence_present(dispatch_evidence)
    startup_fence = _fence_evidence_present(startup_evidence)
    dispatch_bounded = _bounded_dispatch_evidence_present(dispatch_evidence)
    startup_bounded = _bounded_startup_evidence_present(startup_evidence)
    dispatch_present = dispatch_matches and dispatch_fence and dispatch_bounded
    startup_present = startup_matches and startup_fence and startup_bounded
    return {
        "present": dispatch_present and startup_present,
        "dispatch_present": dispatch_present,
        "startup_present": startup_present,
        "dispatch_matches": dispatch_matches,
        "startup_matches": startup_matches,
        "fence_present": dispatch_fence and startup_fence,
        "bounded_present": dispatch_bounded and startup_bounded,
        "dispatch_fence_present": dispatch_fence,
        "startup_fence_present": startup_fence,
        "dispatch_bounded_present": dispatch_bounded,
        "startup_bounded_present": startup_bounded,
    }


def _first_mapping(payload: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _version_workspace_gate(payload: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _first_mapping(
        payload,
        (
            "version_check",
            "workspace_gate",
            "workspace_evidence",
            "version_gate",
        ),
    )
    if not evidence:
        return {"present": False, "passed": False, "reason": "missing"}
    status = _string(evidence.get("status") or evidence.get("result")).lower()
    dirty = _bool(evidence.get("dirty"))
    dirty_files = _string_list(evidence.get("dirty_files"), field_name="dirty_files")
    passed_signal = (
        _bool(evidence.get("ok"))
        or _bool(evidence.get("passed"))
        or status in _PASS_STATUSES
    )
    passed = passed_signal and not dirty and not dirty_files
    reason = ""
    if dirty:
        reason = "dirty worktree"
    elif dirty_files:
        reason = "dirty files present"
    elif not passed_signal:
        reason = "not passed"
    return {
        "present": True,
        "passed": passed,
        "status": status or ("passed" if passed_signal else ""),
        "dirty": dirty,
        "dirty_file_count": len(dirty_files),
        "reason": reason,
    }


def _graph_current_gate(payload: Mapping[str, Any]) -> dict[str, Any]:
    evidence = _first_mapping(
        payload,
        (
            "graph_status",
            "graph_gate",
            "current_graph",
            "graph_evidence",
        ),
    )
    if not evidence:
        return {"present": False, "passed": False, "reason": "missing"}
    current_state = _mapping(evidence.get("current_state"), field_name="current_state")
    raw_graph_stale = evidence.get("graph_stale")
    if isinstance(raw_graph_stale, Mapping):
        graph_stale = dict(raw_graph_stale)
    elif isinstance(current_state.get("graph_stale"), Mapping):
        graph_stale = dict(current_state["graph_stale"])
    else:
        graph_stale = {}
    stale_known = "is_stale" in graph_stale or isinstance(raw_graph_stale, bool)
    if "is_stale" in graph_stale:
        stale = _bool(graph_stale.get("is_stale"))
    elif isinstance(raw_graph_stale, bool):
        stale = raw_graph_stale
    else:
        stale = False
    status = _string(evidence.get("status") or evidence.get("result")).lower()
    passed_signal = (
        _bool(evidence.get("ok"))
        or _bool(evidence.get("passed"))
        or _bool(evidence.get("current"))
        or _bool(evidence.get("graph_current"))
        or status in _PASS_STATUSES
        or (stale_known and not stale)
    )
    passed = passed_signal and not stale
    reason = "graph stale" if stale else ("" if passed_signal else "not current")
    return {
        "present": True,
        "passed": passed,
        "status": status or ("passed" if passed_signal else ""),
        "graph_stale": stale,
        "reason": reason,
    }


def validate_route_action_gate(
    payload: Mapping[str, Any],
    *,
    action: str = "",
) -> dict[str, Any]:
    """Validate route-owned role/action policy before implementation mutation."""

    if not isinstance(payload, Mapping):
        raise MfSubagentContractError("route action gate payload must be a mapping")
    payload = dict(payload)
    action_name = _route_action_name(payload, action=action)
    caller_role = _route_caller_role(payload)
    route_context_hash = _route_context_hash(payload)
    prompt_contract_id = _route_prompt_contract_id(payload)
    prompt_contract_hash = _route_prompt_contract_hash(payload)
    route_alert_codes = _route_alert_codes(payload)
    implementation_action = action_name in _IMPLEMENTATION_ACTIONS
    high_risk_policy = _route_action_high_risk_policy(payload)
    provider_unavailable_reason = _route_provider_unavailable_reason(payload)
    direct_implementation_block_alerts = sorted(
        ROUTE_DIRECT_IMPLEMENTATION_BLOCK_ALERTS.intersection(route_alert_codes)
    )
    waiver = _mapping(
        payload.get("route_action_waiver")
        or payload.get("accepted_waiver")
        or payload.get("waiver"),
        field_name="route_action_waiver",
    )
    waiver_matches = _accepted_waiver_matches(
        waiver,
        route_context_hash=route_context_hash,
        prompt_contract_id=prompt_contract_id,
        prompt_contract_hash=prompt_contract_hash,
    )

    if implementation_action and provider_unavailable_reason:
        raise MfSubagentContractError(
            "blocked_route_context_unavailable: "
            f"{provider_unavailable_reason}"
        )
    if implementation_action and (not route_context_hash or not prompt_contract_id):
        raise MfSubagentContractError(
            "implementation action requires route_context_hash and prompt_contract_id"
        )
    if (
        caller_role in _OBSERVER_JUDGER_ROLES
        and implementation_action
        and direct_implementation_block_alerts
    ):
        alert_code = ",".join(direct_implementation_block_alerts)
        raise MfSubagentContractError(
            f"{alert_code} blocks {caller_role or 'unknown'} direct implementation "
            f"action {action_name or 'unknown'}; route waiver, dispatch, or startup "
            "evidence cannot authorize observer/reviewer direct implementation"
        )
    if implementation_action and not _route_visible_injection_manifest_present(payload):
        raise MfSubagentContractError(
            "implementation action requires visible_injection_manifest_hash "
            "or visible_injection_manifest"
        )
    machine_context = {
        "visible_injection_manifest_present": _route_visible_injection_manifest_present(
            payload
        ),
        "allowed_actions": _route_explicit_allowed_actions(payload),
        "blocked_actions": _route_blocked_actions(payload),
        "required_lanes": _route_required_lanes(payload),
        "required_evidence": _route_required_evidence(payload),
    }
    if implementation_action and high_risk_policy["required"]:
        missing_machine_fields: list[str] = []
        if not caller_role:
            missing_machine_fields.append("caller_role")
        if not machine_context["visible_injection_manifest_present"]:
            missing_machine_fields.append("visible_injection_manifest")
        for field_name in (
            "allowed_actions",
            "blocked_actions",
            "required_lanes",
            "required_evidence",
        ):
            if not machine_context[field_name]:
                missing_machine_fields.append(field_name)
        if missing_machine_fields:
            raise MfSubagentContractError(
                "high-risk implementation action requires machine route "
                "context fields: " + ", ".join(missing_machine_fields)
            )
        if not _route_action_allowed(action_name, machine_context["allowed_actions"]):
            raise MfSubagentContractError(
                f"route allowed_actions do not allow implementation action {action_name}"
            )
    hard_blocked_actions = _route_hard_blocked_actions(
        payload,
        caller_role=caller_role,
    )
    if implementation_action and _route_action_allowed(action_name, hard_blocked_actions):
        raise MfSubagentContractError(
            "route blocked_actions explicitly block implementation action "
            f"{action_name}"
        )
    dispatch_evidence = _mapping(
        payload.get("bounded_dispatch_evidence")
        or payload.get("dispatch_evidence")
        or payload.get("mf_subagent_dispatch_gate"),
        field_name="bounded_dispatch_evidence",
    )
    startup_evidence = _route_startup_evidence(payload)
    bounded_worker_evidence = _bounded_worker_evidence_matches(
        dispatch_evidence,
        startup_evidence,
        route_context_hash=route_context_hash,
        prompt_contract_id=prompt_contract_id,
        prompt_contract_hash=prompt_contract_hash,
    )
    dispatch_matches = bool(bounded_worker_evidence["dispatch_present"])
    startup_matches = bool(bounded_worker_evidence["startup_present"])
    bounded_worker_matches = bool(bounded_worker_evidence["present"])
    if (
        implementation_action
        and high_risk_policy["required"]
        and not bounded_worker_matches
    ):
        raise MfSubagentContractError(
            "high-risk implementation action requires matching bounded dispatch/startup "
            "evidence before mutation"
        )
    version_workspace_gate = _version_workspace_gate(payload)
    graph_current_gate = _graph_current_gate(payload)
    precondition_waiver_used = False
    if implementation_action:
        if not version_workspace_gate["passed"]:
            if not waiver_matches:
                raise MfSubagentContractError(
                    "implementation action requires clean version/workspace evidence"
                )
            precondition_waiver_used = True
        if not graph_current_gate["passed"]:
            if not waiver_matches:
                raise MfSubagentContractError(
                    "implementation action requires current graph evidence"
                )
            precondition_waiver_used = True

    return {
        "schema_version": ROUTE_ACTION_GATE_SCHEMA_VERSION,
        "allowed": True,
        "action": action_name,
        "caller_role": caller_role,
        "implementation_action": implementation_action,
        "route_alert_codes": route_alert_codes,
        "route_context_hash": route_context_hash,
        "prompt_contract_id": prompt_contract_id,
        "prompt_contract_hash": prompt_contract_hash,
        "machine_context_required": bool(high_risk_policy["required"]),
        "machine_context_policy": high_risk_policy,
        "route_machine_context": machine_context,
        "accepted_waiver_present": waiver_matches,
        "bounded_dispatch_evidence_present": dispatch_matches,
        "bounded_startup_evidence_present": startup_matches,
        "bounded_worker_evidence_present": bounded_worker_matches,
        "bounded_dispatch_evidence": bounded_worker_evidence,
        "version_workspace_gate": version_workspace_gate,
        "graph_current_gate": graph_current_gate,
        "precondition_waiver_used": precondition_waiver_used,
    }


def validate_route_token_mutation_gate(
    payload: Mapping[str, Any],
    *,
    action: str,
    project_id: str = "",
    backlog_id: str = "",
    task_id: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate route-token or explicit waiver evidence for protected mutations."""

    if not isinstance(payload, Mapping):
        raise MfSubagentContractError("route token gate payload must be a mapping")
    payload = dict(payload)
    action_name = _normalized_action(action)
    if not action_name:
        raise MfSubagentContractError("route token gate requires an action")

    request_scope = {
        "project_id": _string(project_id),
        "backlog_id": _string(backlog_id),
        "task_id": _string(task_id),
    }
    token = _route_token_payload(payload)
    if token:
        return _validate_route_token(
            token,
            action=action_name,
            request_scope=request_scope,
            now=now,
        )

    waiver = _route_token_waiver(payload)
    if waiver:
        return _validate_route_token_waiver(
            waiver,
            action=action_name,
            request_scope=request_scope,
        )

    raise MfSubagentContractError(
        f"route_token is required for protected governance action {action_name}; "
        "pass route_token with route_context_hash, prompt_contract_id, caller_role, "
        "allowed action, scope, expiry, and evidence_refs, or pass an explicit "
        "route_waiver with reason and timeline evidence"
    )


def route_token_required_failure_details(
    *,
    action: str,
    reason: str = "",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return machine-readable details for expected protected-route failures."""

    action_name = _normalized_action(action) or _string(action)
    details: dict[str, Any] = {
        "schema_version": ROUTE_TOKEN_REQUIRED_FAILURE_SCHEMA_VERSION,
        "protected_action": action_name,
        "route_token_required": True,
        "fault_domain": "caller_missing_route_evidence",
        "expected_behavior": True,
        "do_not_file_system_bug": True,
        "is_system_bug": False,
        "classification": "expected_protected_route_gate",
        "required_route_token_fields": [
            "route_context_hash",
            "prompt_contract_id",
            "caller_role",
            "allowed_action",
            "scope.project_id",
            "expires_at",
            "evidence_refs",
        ],
        "waiver_fields": [
            "route_waiver.waiver_type",
            "route_waiver.reason",
            "route_waiver.route_context_hash",
            "route_waiver.prompt_contract_id",
            "route_waiver.caller_role",
            "route_waiver.scope.project_id",
            "route_waiver.scope.backlog_id",
            "route_waiver.scope.task_id",
            "route_waiver.timeline_evidence",
            "route_waiver.allowed_action",
        ],
        "next_valid_actions": [
            "return_to_route_context_and_request_a_valid_route_token",
            "dispatch_or_start_the_bounded_mf_subagent_worker_and_record_route_context_consumption",
            "record_route_waiver_as_waiver_evidence_only_when_no_route_token_is_available",
            "retry_the_protected_action_only_after_matching_route_token_or_required_route_evidence_exists",
        ],
        "system_bug_preconditions": [
            "a_valid_unexpired_route_token_with_matching_action_scope_and_evidence_refs_was_supplied",
            "or_required_bounded_worker_route_context_consumption_evidence_exists_with_matching_route_identity",
            "and_the_protected_gate_still_rejected_or_stripped_the_structured_route_details",
        ],
    }
    if reason:
        details["reason"] = reason
    if isinstance(extra, Mapping):
        details.update(dict(extra))
    return details


def _route_token_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    token = payload.get("route_token")
    if isinstance(token, str):
        try:
            parsed = json.loads(token)
        except json.JSONDecodeError:
            parsed = {}
        token = parsed
    if isinstance(token, Mapping):
        return dict(token)
    return {}


def _route_token_waiver(payload: Mapping[str, Any]) -> dict[str, Any]:
    waiver = (
        payload.get("route_waiver")
        or payload.get("route_token_waiver")
        or payload.get("protected_route_waiver")
    )
    if isinstance(waiver, str):
        try:
            parsed = json.loads(waiver)
        except json.JSONDecodeError:
            parsed = {}
        waiver = parsed
    if isinstance(waiver, Mapping):
        return dict(waiver)
    return {}


def _validate_route_token(
    token: Mapping[str, Any],
    *,
    action: str,
    request_scope: Mapping[str, str],
    now: datetime | None,
) -> dict[str, Any]:
    route_context_hash = _string(token.get("route_context_hash"))
    prompt_contract_id = _string(token.get("prompt_contract_id"))
    prompt_contract_hash = _string(token.get("prompt_contract_hash"))
    caller_role = _string(token.get("caller_role") or token.get("role")).lower()
    expires_at = _string(token.get("expires_at") or token.get("expiry"))
    evidence_refs = _route_evidence_refs(token)
    allowed_actions = _route_allowed_actions(token)

    missing = []
    if not route_context_hash:
        missing.append("route_context_hash")
    if not prompt_contract_id:
        missing.append("prompt_contract_id")
    if not caller_role:
        missing.append("caller_role")
    if not allowed_actions:
        missing.append("allowed_action")
    if not expires_at:
        missing.append("expires_at")
    if not evidence_refs:
        missing.append("evidence_refs")
    if missing:
        raise MfSubagentContractError(
            "route_token missing required fields: " + ", ".join(missing)
        )

    if not _route_action_allowed(action, allowed_actions):
        raise MfSubagentContractError(
            f"route_token does not allow protected action {action}"
        )

    expires_dt = _parse_route_expiry(expires_at)
    now_dt = now or datetime.now(timezone.utc)
    if expires_dt <= now_dt:
        raise MfSubagentContractError("route_token expired")

    _validate_route_scope(token, request_scope=request_scope)
    return {
        "schema_version": ROUTE_TOKEN_MUTATION_GATE_SCHEMA_VERSION,
        "allowed": True,
        "status": "accepted",
        "action": action,
        "decision": "route_token",
        "route_context_hash": route_context_hash,
        "prompt_contract_id": prompt_contract_id,
        "prompt_contract_hash": prompt_contract_hash,
        "caller_role": caller_role,
        "route_token_hash": _stable_hash(token),
        "expires_at": expires_at,
        "evidence_refs": evidence_refs,
        "scope": _route_scope_summary(token, request_scope),
        "required_fields": list(_ROUTE_TOKEN_REQUIRED_FIELDS),
    }


def _validate_route_token_waiver(
    waiver: Mapping[str, Any],
    *,
    action: str,
    request_scope: Mapping[str, str],
) -> dict[str, Any]:
    route_context_hash = _route_context_hash(waiver)
    prompt_contract_id = _route_prompt_contract_id(waiver)
    caller_role = _route_caller_role(waiver)
    missing_identity = []
    if not route_context_hash:
        missing_identity.append("route_context_hash")
    if not prompt_contract_id:
        missing_identity.append("prompt_contract_id")
    if not caller_role:
        missing_identity.append("caller_role")
    if missing_identity:
        raise MfSubagentContractError(
            "route_waiver missing required route identity fields: "
            + ", ".join(missing_identity)
        )

    status = _string(waiver.get("status") or waiver.get("decision")).lower()
    accepted = _bool(waiver.get("accepted")) or status in {
        "accepted",
        "approved",
        "allow",
        "allowed",
        "waived",
    }
    if not accepted:
        raise MfSubagentContractError("route_waiver must be explicitly accepted")

    waiver_type = _string(
        waiver.get("waiver_type")
        or waiver.get("type")
        or waiver.get("kind")
    ).lower()
    manual_fix = _bool(waiver.get("manual_fix") or waiver.get("manual_fix_allowed"))
    same_worktree = _bool(
        waiver.get("same_worktree_allowed") or waiver.get("same_worktree")
    )
    if waiver_type not in _ROUTE_TOKEN_WAIVER_TYPES and not (manual_fix or same_worktree):
        raise MfSubagentContractError(
            "route_waiver requires manual_fix or same_worktree waiver type"
        )

    reason = _string(waiver.get("reason") or waiver.get("operator_reason"))
    if len(reason) < 20:
        raise MfSubagentContractError(
            "route_waiver requires reason with at least 20 characters"
        )

    allowed_actions = _route_allowed_actions(waiver)
    if not allowed_actions or not _route_action_allowed(action, allowed_actions):
        raise MfSubagentContractError(
            f"route_waiver does not allow protected action {action}"
        )

    timeline_evidence = _timeline_evidence_refs(waiver)
    if not timeline_evidence:
        raise MfSubagentContractError(
            "route_waiver requires timeline evidence"
        )

    _validate_route_scope(waiver, request_scope=request_scope)
    return {
        "schema_version": ROUTE_TOKEN_MUTATION_GATE_SCHEMA_VERSION,
        "allowed": True,
        "status": "accepted",
        "action": action,
        "decision": "route_waiver",
        "route_context_hash": route_context_hash,
        "prompt_contract_id": prompt_contract_id,
        "caller_role": caller_role,
        "waiver_hash": _stable_hash(waiver),
        "waiver_type": waiver_type or ("manual_fix" if manual_fix else "same_worktree"),
        "reason": reason,
        "timeline_evidence": timeline_evidence,
        "scope": _route_scope_summary(waiver, request_scope),
    }


def _route_allowed_actions(value: Mapping[str, Any]) -> list[str]:
    candidates: list[Any] = [
        value.get("allowed_action"),
        value.get("action"),
        value.get("requested_action"),
    ]
    allowed_actions = value.get("allowed_actions")
    if isinstance(allowed_actions, Sequence) and not isinstance(
        allowed_actions, (str, bytes, bytearray)
    ):
        candidates.extend(allowed_actions)
    elif allowed_actions:
        candidates.append(allowed_actions)
    actions: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        action = _normalized_action(candidate)
        if action and action not in seen:
            actions.append(action)
            seen.add(action)
    return actions


def _route_action_allowed(action: str, allowed_actions: Sequence[str]) -> bool:
    allowed = {_normalized_action(item) for item in allowed_actions if _string(item)}
    return "*" in allowed or _normalized_action(action) in allowed


def _route_evidence_refs(token: Mapping[str, Any]) -> list[str]:
    refs = _string_list_forgiving(token.get("evidence_refs"))
    for key in ("evidence_ref", "trace_id", "timeline_event_id", "source_event_id"):
        ref = _string(token.get(key))
        if ref:
            refs.append(ref)
    return _dedupe_strings(refs)


def _timeline_evidence_refs(waiver: Mapping[str, Any]) -> list[str]:
    refs = _string_list_forgiving(waiver.get("timeline_evidence_refs"))
    for key in ("timeline_event_id", "event_id", "trace_id"):
        ref = _string(waiver.get(key))
        if ref:
            refs.append(ref)
    timeline_evidence = waiver.get("timeline_evidence")
    if isinstance(timeline_evidence, Mapping):
        for key in ("event_id", "id", "trace_id"):
            ref = _string(timeline_evidence.get(key))
            if ref:
                refs.append(ref)
    return _dedupe_strings(refs)


def _string_list_forgiving(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        token = _string(value)
        return [token] if token else []
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        return []
    refs: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            ref = _string(
                item.get("id")
                or item.get("event_id")
                or item.get("trace_id")
                or item.get("ref")
            )
        else:
            ref = _string(item)
        if ref:
            refs.append(ref)
    return refs


def _dedupe_strings(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = _string(value)
        if token and token not in seen:
            out.append(token)
            seen.add(token)
    return out


def _parse_route_expiry(value: str) -> datetime:
    raw = _string(value)
    if not raw:
        raise MfSubagentContractError("route_token expires_at is required")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MfSubagentContractError("route_token expires_at must be ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _route_scope_value(value: Mapping[str, Any], *names: str) -> str:
    scope = _nested_mapping(value, "scope")
    for name in names:
        token = _string(value.get(name) or scope.get(name))
        if token:
            return token
    return ""


def _validate_route_scope(
    value: Mapping[str, Any],
    *,
    request_scope: Mapping[str, str],
) -> None:
    project_id = _string(request_scope.get("project_id"))
    if project_id:
        token_project_id = _route_scope_value(value, "project_id")
        if not token_project_id:
            raise MfSubagentContractError("route token scope requires project_id")
        if token_project_id != project_id:
            raise MfSubagentContractError(
                f"route token project scope {token_project_id!r} does not match {project_id!r}"
            )

    backlog_id = _string(request_scope.get("backlog_id"))
    if backlog_id:
        token_backlog_id = _route_scope_value(value, "backlog_id", "bug_id")
        if not token_backlog_id:
            raise MfSubagentContractError("route token scope requires backlog_id")
        if token_backlog_id != backlog_id:
            raise MfSubagentContractError(
                f"route token backlog scope {token_backlog_id!r} does not match {backlog_id!r}"
            )

    task_id = _string(request_scope.get("task_id"))
    if task_id:
        token_task_id = _route_scope_value(value, "task_id")
        if not token_task_id:
            raise MfSubagentContractError("route token scope requires task_id")
        if token_task_id != task_id:
            raise MfSubagentContractError(
                f"route token task scope {token_task_id!r} does not match {task_id!r}"
            )


def _route_scope_summary(
    value: Mapping[str, Any],
    request_scope: Mapping[str, str],
) -> dict[str, str]:
    return {
        "project_id": _route_scope_value(value, "project_id") or _string(request_scope.get("project_id")),
        "backlog_id": _route_scope_value(value, "backlog_id", "bug_id") or _string(request_scope.get("backlog_id")),
        "task_id": _route_scope_value(value, "task_id") or _string(request_scope.get("task_id")),
    }


def _stable_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _dirty_scope_evidence(value: Any) -> dict[str, Any]:
    check = _mapping(value, field_name="dirty_scope_check")
    status = _string(check.get("status") or check.get("result")).lower()
    passed = _bool(check.get("passed")) or status in _PASS_STATUSES
    exact_match = _bool(
        check.get("dirty_scope_exact_match")
        or check.get("exact_match")
        or check.get("owned_scope_only")
    )
    evidence_fields = [
        "changed_files",
        "dirty_files",
        "owned_files",
        "checked_paths",
        "allowed_dirty_files",
    ]
    has_file_evidence = any(key in check for key in evidence_fields)
    if not passed or not has_file_evidence:
        raise MfSubagentContractError(
            "dirty_scope_check must include passing dirty-scope evidence"
        )
    return {
        "status": status or "passed",
        "passed": True,
        "dirty_scope_exact_match": exact_match,
        "changed_file_count": len(
            _string_list(check.get("changed_files"), field_name="changed_files")
        ),
        "dirty_file_count": len(
            _string_list(check.get("dirty_files"), field_name="dirty_files")
        ),
    }


def _override_reason(payload: Mapping[str, Any], override: Mapping[str, Any]) -> str:
    for key in (
        "same_worktree_reason",
        "operator_reason",
        "explicit_operator_reason",
        "reason",
    ):
        token = _string(payload.get(key))
        if token:
            return token
    for key in ("operator_reason", "explicit_operator_reason", "reason"):
        token = _string(override.get(key))
        if token:
            return token
    return ""


def _timeline_evidence_present(
    payload: Mapping[str, Any],
    override: Mapping[str, Any],
    *,
    require_before_mutation: bool = False,
) -> bool:
    if _bool(payload.get("timeline_event_recorded")) or _bool(
        override.get("timeline_event_recorded")
    ):
        return not require_before_mutation
    for key in (
        "timeline_evidence",
        "observer_timeline_event",
        "dispatch_timeline_evidence",
        "direct_mutation_timeline_evidence",
    ):
        value = payload.get(key) if key in payload else override.get(key)
        if isinstance(value, Mapping) and (
            _string(value.get("event_id"))
            or _string(value.get("event_type"))
            or _string(value.get("recorded_at"))
        ):
            if require_before_mutation and not (
                _bool(value.get("recorded_before_mutation"))
                or _bool(value.get("before_mutation"))
                or _string(value.get("phase")).lower()
                in {"pre_mutation", "before_mutation", "pre_implementation"}
            ):
                continue
            return True
    return False


def _direct_mutation_exception(payload: Mapping[str, Any]) -> dict[str, Any]:
    exception = _nested_mapping(payload, "observer_direct_mutation_exception")
    if not exception:
        exception = _nested_mapping(payload, "direct_mutation_exception")
    return exception


def validate_observer_direct_mutation_exception(
    payload: Mapping[str, Any],
    *,
    allowed_files: Sequence[str] | None = None,
    dirty_files: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Validate the narrow exception for observer-authored mutations.

    Governed nontrivial implementation belongs in bounded `mf_sub` or worker
    lanes. This validator is only for tiny deterministic same-observer edits
    and is separate from worker dispatch so the observer role boundary is
    machine-checkable.
    """

    if not isinstance(payload, Mapping):
        raise MfSubagentContractError(
            "observer direct mutation exception payload must be a mapping"
        )
    payload = dict(payload)
    direct_mutation = _bool(
        payload.get("observer_direct_mutation")
        or payload.get("same_observer_direct_mutation")
        or payload.get("direct_mutation")
    )
    if not direct_mutation:
        raise MfSubagentContractError(
            "observer direct mutation exception requires observer_direct_mutation=true"
        )

    exception = _direct_mutation_exception(payload)
    role = _string(
        payload.get("observer_role")
        or payload.get("role")
        or payload.get("actor")
        or exception.get("observer_role")
        or exception.get("role")
        or exception.get("actor")
    ).lower()
    if role != OBSERVER_COORDINATOR_ROLE:
        raise MfSubagentContractError(
            "observer direct mutation exception requires observer role evidence"
        )

    tiny_deterministic = _bool(
        exception.get("tiny_deterministic")
        or exception.get("tiny_deterministic_scope")
        or payload.get("tiny_deterministic")
        or payload.get("tiny_deterministic_scope")
    )
    if not tiny_deterministic:
        raise MfSubagentContractError(
            "observer direct mutation exception requires tiny deterministic scope"
        )

    reason = _override_reason(payload, exception)
    if not reason:
        raise MfSubagentContractError(
            "observer direct mutation exception requires explicit reason"
        )

    exception_allowed_files = _string_list(
        exception.get("allowed_files") or payload.get("allowed_files"),
        field_name="allowed_files",
    )
    if not exception_allowed_files:
        raise MfSubagentContractError(
            "observer direct mutation exception requires allowed_files"
        )
    expected_allowed_files = set(
        _string_list(allowed_files, field_name="allowed_files")
    )
    if expected_allowed_files and not set(exception_allowed_files).issubset(
        expected_allowed_files
    ):
        raise MfSubagentContractError(
            "observer direct mutation exception allowed_files exceed owned scope"
        )

    dirty_scope_input = exception.get("dirty_scope_check") or payload.get(
        "dirty_scope_check"
    )
    if not dirty_scope_input:
        if dirty_files is None:
            raise MfSubagentContractError(
                "observer direct mutation exception requires dirty-scope evidence"
            )
        dirty_scope_input = {
            "status": "passed",
            "dirty_scope_exact_match": True,
            "dirty_files": list(dirty_files),
            "owned_files": exception_allowed_files,
        }
    dirty_scope = _dirty_scope_evidence(dirty_scope_input)
    dirty_scope_mapping = _mapping(
        dirty_scope_input,
        field_name="dirty_scope_check",
    )
    if not dirty_scope["dirty_scope_exact_match"]:
        raise MfSubagentContractError(
            "observer direct mutation exception requires dirty_scope_exact_match evidence"
        )
    scoped_dirty_files = set(
        _string_list(
            dirty_scope_mapping.get("dirty_files")
            or dirty_scope_mapping.get("changed_files")
            or dirty_files,
            field_name="dirty_files",
        )
    )
    if scoped_dirty_files and not scoped_dirty_files.issubset(
        set(exception_allowed_files)
    ):
        raise MfSubagentContractError(
            "observer direct mutation exception dirty files must match allowed_files"
        )

    if not _timeline_evidence_present(
        payload,
        exception,
        require_before_mutation=True,
    ):
        raise MfSubagentContractError(
            "observer direct mutation exception requires timeline evidence before mutation"
        )

    return {
        "schema_version": OBSERVER_DIRECT_MUTATION_SCHEMA_VERSION,
        "role": OBSERVER_COORDINATOR_ROLE,
        "policy_default": OBSERVER_DIRECT_MUTATION_DEFAULT,
        "observer_direct_mutation": True,
        "allowed": True,
        "exception": {
            "used": True,
            "tiny_deterministic": True,
            "reason": reason,
            "allowed_files": exception_allowed_files,
            "timeline_evidence_recorded_before_mutation": True,
        },
        "dirty_scope_check": dirty_scope,
    }


def validate_mf_subagent_dispatch_gate(
    payload: Mapping[str, Any],
    *,
    target_worktree_path: str = "",
    main_worktree_path: str = "",
) -> dict[str, Any]:
    """Validate local MF subagent dispatch evidence before handoff.

    The gate is intentionally local and backend-neutral: observers and AI
    self-checks can run it before spawning a bounded `mf_sub` worker.
    """

    if not isinstance(payload, Mapping):
        raise MfSubagentContractError("MF subagent dispatch payload must be a mapping")
    payload = dict(payload)
    branch = _dispatch_string(
        payload,
        names=("branch", "branch_ref", "ref_name"),
        nested_keys=(("branch_context", ("branch_ref", "ref_name")),),
    )
    worktree = _dispatch_string(
        payload,
        names=("worktree", "worktree_path"),
        nested_keys=(("branch", ("worktree_path", "path")),),
    )
    base_commit = _dispatch_string(
        payload,
        names=("base_commit",),
        nested_keys=(("branch", ("base_commit",)),),
    )
    target_head_commit = _dispatch_string(
        payload,
        names=("target_head_commit",),
        nested_keys=(("branch", ("target_head_commit", "head_commit")),),
    )
    merge_queue_id = _dispatch_string(
        payload,
        names=("merge_queue_id",),
        nested_keys=(
            ("branch_context", ("merge_queue_id",)),
            ("graph_identity", ("merge_queue_id",)),
            ("branch", ("merge_queue_id",)),
        ),
    )
    fence_token = _dispatch_string(payload, names=("fence_token",))
    route_context_hash = _dispatch_string(
        payload,
        names=("route_context_hash",),
        nested_keys=(
            ("route_context", ("route_context_hash",)),
            ("prompt_contract", ("route_context_hash",)),
            ("route_prompt_contract", ("route_context_hash",)),
        ),
    )
    prompt_contract_id = _dispatch_string(
        payload,
        names=("prompt_contract_id",),
        nested_keys=(
            ("route_context", ("prompt_contract_id",)),
            ("prompt_contract", ("prompt_contract_id", "id")),
            ("route_prompt_contract", ("prompt_contract_id", "id")),
        ),
    )
    prompt_contract_hash = _dispatch_string(
        payload,
        names=("prompt_contract_hash",),
        nested_keys=(
            ("route_context", ("prompt_contract_hash",)),
            ("prompt_contract", ("prompt_contract_hash",)),
            ("route_prompt_contract", ("prompt_contract_hash",)),
        ),
    )
    values = {
        "branch": branch,
        "worktree": worktree,
        "base_commit": base_commit,
        "target_head_commit": target_head_commit,
        "merge_queue_id": merge_queue_id,
        "fence_token": fence_token,
        "route_context_hash": route_context_hash,
        "prompt_contract_id": prompt_contract_id,
        "prompt_contract_hash": prompt_contract_hash,
    }
    missing = [field for field in _DISPATCH_REQUIRED_FIELDS if not values[field]]
    if missing:
        raise MfSubagentContractError(
            "MF subagent dispatch missing required fields: " + ", ".join(missing)
        )

    owned_files = _string_list(
        payload.get("owned_files") or payload.get("write_scope"),
        field_name="owned_files",
    )
    if not owned_files:
        raise MfSubagentContractError("MF subagent dispatch missing owned_files fence")
    dirty_scope = _dirty_scope_evidence(payload.get("dirty_scope_check"))

    policy = _nested_mapping(payload, "worktree_policy")
    override = _nested_mapping(payload, "same_worktree_override")
    if not override:
        override = _nested_mapping(payload, "override_policy")
    same_worktree_allowed = _bool(
        payload.get("same_worktree_allowed")
        or policy.get("same_worktree_allowed")
        or override.get("same_worktree_allowed")
    )
    normalized_worktree = _normalize_worktree_path(worktree)
    target_paths = [
        _normalize_worktree_path(path)
        for path in (
            target_worktree_path,
            main_worktree_path,
            _string(payload.get("target_worktree_path")),
            _string(payload.get("main_worktree_path")),
            _string(policy.get("target_worktree_path")),
            _string(policy.get("main_worktree_path")),
        )
        if _string(path)
    ]
    target_role = _string(
        payload.get("worktree_role") or policy.get("worktree_role")
    ).lower()
    same_worktree = normalized_worktree in target_paths or target_role in {
        "target",
        "main",
    }
    if same_worktree and not same_worktree_allowed:
        raise MfSubagentContractError(
            "same-worktree dispatch is blocked by default for local mf_sub workers"
        )

    override_used = same_worktree and same_worktree_allowed
    override_reason = ""
    if override_used:
        override_reason = _override_reason(payload, override)
        if not override_reason:
            raise MfSubagentContractError(
                "same-worktree dispatch override requires explicit operator reason"
            )
        if not dirty_scope["dirty_scope_exact_match"]:
            raise MfSubagentContractError(
                "same-worktree dispatch override requires dirty_scope_exact_match evidence"
            )
        if not _timeline_evidence_present(payload, override):
            raise MfSubagentContractError(
                "same-worktree dispatch override requires observer timeline evidence"
            )

    return {
        "schema_version": DISPATCH_GATE_SCHEMA_VERSION,
        "allowed": True,
        "role": MF_SUB_ROLE,
        "dispatch_default": DISPATCH_DEFAULT,
        "worktree_policy": WORKTREE_POLICY_MODE,
        "branch": branch,
        "worktree": worktree,
        "base_commit": base_commit,
        "target_head_commit": target_head_commit,
        "merge_queue_id": merge_queue_id,
        "fence_token": fence_token,
        "route_context_hash": route_context_hash,
        "prompt_contract_id": prompt_contract_id,
        "prompt_contract_hash": prompt_contract_hash,
        "owned_files": owned_files,
        "isolated_worktree": not same_worktree,
        "same_worktree_allowed": same_worktree_allowed,
        "override": {
            "used": override_used,
            "reason": override_reason,
            "timeline_evidence_recorded": override_used,
        },
        "dirty_scope_check": dirty_scope,
    }


def _require_context(context: BranchTaskRuntimeContext) -> None:
    missing = [field for field in _REQUIRED_CONTEXT_FIELDS if not getattr(context, field)]
    if missing:
        raise MfSubagentContractError(
            f"MF subagent context missing required fields: {', '.join(missing)}"
        )


def build_mf_subagent_input(
    context: BranchTaskRuntimeContext,
    *,
    prompt: str,
    acceptance_criteria: Sequence[str] | None = None,
    target_files: Sequence[str] | None = None,
    test_commands: Sequence[str] | None = None,
    operator_notes: str = "",
    backend: str = "codex_subagent",
    route_context_hash: str = "",
    prompt_contract_id: str = "",
    prompt_contract_hash: str = "",
) -> dict[str, Any]:
    """Build the stable input payload for a branch-isolated MF subagent."""

    _require_context(context)
    return {
        "schema_version": INPUT_SCHEMA_VERSION,
        "role": MF_SUB_ROLE,
        "backend": backend,
        "backend_contract": BACKEND_CONTRACT,
        "project_id": context.project_id,
        "task_id": context.task_id,
        "batch_id": context.batch_id,
        "backlog_id": context.backlog_id,
        "branch": {
            "branch_ref": context.branch_ref,
            "ref_name": context.ref_name,
            "worktree_id": context.worktree_id,
            "worktree_path": context.worktree_path,
            "base_commit": context.base_commit,
            "head_commit": context.head_commit,
            "target_head_commit": context.target_head_commit,
        },
        "runtime_identity": {
            "agent_id": context.agent_id,
            "worker_id": context.worker_id,
            "attempt": context.attempt,
            "lease_id": context.lease_id,
            "fence_token": context.fence_token,
            "checkpoint_id": context.checkpoint_id,
            "depends_on": list(context.depends_on),
        },
        "chain_identity": {
            "chain_id": context.chain_id,
            "root_task_id": context.root_task_id,
            "stage_task_id": context.stage_task_id,
            "stage_type": context.stage_type,
            "retry_round": context.retry_round,
        },
        "graph_identity": {
            "snapshot_id": context.snapshot_id,
            "projection_id": context.projection_id,
            "merge_queue_id": context.merge_queue_id,
            "merge_preview_id": context.merge_preview_id,
            "rollback_epoch": context.rollback_epoch,
            "replay_epoch": context.replay_epoch,
        },
        "work": {
            "prompt": prompt,
            "acceptance_criteria": _string_list(
                acceptance_criteria, field_name="acceptance_criteria"
            ),
            "target_files": _string_list(target_files, field_name="target_files"),
            "test_commands": _string_list(test_commands, field_name="test_commands"),
            "operator_notes": operator_notes,
        },
        "route_prompt_contract": {
            "route_context_hash": _string(route_context_hash),
            "prompt_contract_id": _string(prompt_contract_id),
            "prompt_contract_hash": _string(prompt_contract_hash),
        },
        "capabilities": {
            "can": list(MF_SUB_ALLOWED_CAPABILITIES),
            "cannot": list(MF_SUB_FORBIDDEN_ACTIONS),
        },
        "prechecks": {
            "asset_binding_proposal": {
                "proposal_schema_version": ASSET_BINDING_PROPOSAL_SCHEMA_VERSION,
                "precheck_schema_version": ASSET_BINDING_PRECHECK_SCHEMA_VERSION,
                "local_function": (
                    "agent.governance.asset_binding_proposals."
                    "precheck_asset_binding_proposal"
                ),
                "gate_rule": (
                    "Run the same precheck on any doc/test/config binding proposal "
                    "before submitting it; include the compact self_precheck object "
                    "with the proposal so the server gate can verify the hash."
                ),
            },
        },
        "required_output": list(MF_SUB_REQUIRED_OUTPUT),
    }


def normalize_mf_subagent_result(
    payload: Mapping[str, Any],
    *,
    expected_fence_token: str,
) -> dict[str, Any]:
    """Validate and normalize a branch worker result before queueing merge review."""

    if not expected_fence_token:
        raise MfSubagentContractError("expected_fence_token is required")
    missing = [field for field in MF_SUB_REQUIRED_OUTPUT if field not in payload]
    if missing:
        raise MfSubagentContractError(
            f"MF subagent result missing required fields: {', '.join(missing)}"
        )

    fence_token = str(payload.get("fence_token") or "")
    if fence_token != expected_fence_token:
        raise MfSubagentContractError("MF subagent result fence token is stale")

    actions = {action.lower() for action in _string_list(payload.get("actions"), field_name="actions")}
    for field_name, action in _FORBIDDEN_RESULT_FLAGS.items():
        if payload.get(field_name):
            actions.add(action)
    forbidden = sorted(actions.intersection(MF_SUB_FORBIDDEN_ACTIONS))
    if forbidden:
        raise MfSubagentContractError(
            f"MF subagent result attempted forbidden actions: {', '.join(forbidden)}"
        )

    status = str(payload.get("status") or "")
    changed_files = _string_list(payload.get("changed_files"), field_name="changed_files")
    new_files = _string_list(payload.get("new_files"), field_name="new_files")
    blockers = _string_list(payload.get("blockers"), field_name="blockers")
    test_results = _mapping(payload.get("test_results"), field_name="test_results")
    test_status = str(test_results.get("status") or "").lower()
    tests_passed = bool(test_results.get("passed")) or test_status in _PASS_STATUSES
    merge_queue_ready = status in _READY_STATUSES and tests_passed and not blockers

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "role": MF_SUB_ROLE,
        "status": status,
        "changed_files": changed_files,
        "new_files": new_files,
        "test_results": test_results,
        "checkpoint_id": str(payload.get("checkpoint_id") or ""),
        "fence_token": fence_token,
        "merge_queue_ready": merge_queue_ready,
        "blockers": blockers,
        "summary": str(payload.get("summary") or ""),
        "evidence": _mapping(payload.get("evidence"), field_name="evidence"),
    }


def validate_mf_subagent_finish_gate(
    payload: Mapping[str, Any],
    *,
    context: BranchTaskRuntimeContext,
) -> dict[str, Any]:
    """Validate a subagent finish claim against durable branch runtime facts.

    The subagent payload is a claim. This function only returns evidence that
    matches the current runtime context and is ready to become a checkpoint.
    """

    _require_context(context)
    normalized = normalize_mf_subagent_result(
        payload,
        expected_fence_token=context.fence_token,
    )
    if not normalized["merge_queue_ready"]:
        raise MfSubagentContractError("MF subagent finish gate is not merge-queue ready")

    identity_mismatches: list[str] = []
    for field in _FINISH_IDENTITY_FIELDS:
        claimed = str(payload.get(field) or "")
        expected = str(getattr(context, field) or "")
        if claimed and expected and claimed != expected:
            identity_mismatches.append(field)
    if identity_mismatches:
        raise MfSubagentContractError(
            "MF subagent finish gate identity mismatch: "
            + ", ".join(sorted(identity_mismatches))
        )

    claimed_head = str(payload.get("head_commit") or payload.get("branch_head") or "")
    checkpoint_id = str(normalized.get("checkpoint_id") or "").strip()
    if not checkpoint_id:
        raise MfSubagentContractError("checkpoint_id is required")

    return {
        "schema_version": FINISH_GATE_SCHEMA_VERSION,
        "role": MF_SUB_ROLE,
        "project_id": context.project_id,
        "task_id": context.task_id,
        "backlog_id": context.backlog_id,
        "branch_ref": context.branch_ref,
        "worktree_path": context.worktree_path,
        "base_commit": context.base_commit,
        "target_head_commit": context.target_head_commit,
        "merge_queue_id": context.merge_queue_id,
        "head_commit": claimed_head or context.head_commit,
        "checkpoint_id": checkpoint_id,
        "fence_token": context.fence_token,
        "replay_source": FINISH_GATE_REPLAY_SOURCE,
        "changed_files": normalized["changed_files"],
        "new_files": normalized["new_files"],
        "test_results": normalized["test_results"],
        "blockers": normalized["blockers"],
        "summary": normalized["summary"],
        "evidence": normalized["evidence"],
        "merge_queue_ready": True,
    }
