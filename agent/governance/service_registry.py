"""Deterministic governance service registry for contract-bound routing."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any


ServiceHandler = Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]


READ_SIDE_EFFECTS = {"none", "read", "gate"}
WRITE_SIDE_EFFECTS = {"write"}
ALLOWED_SERVICE_MODES = {"preview", "gate", "apply"}
ALLOWED_SIDE_EFFECTS = {*READ_SIDE_EFFECTS, *WRITE_SIDE_EFFECTS}
ROUTE_PROMPT_BUNDLE_SCHEMA_VERSION = "aming_route_prompt_alert_bundle.v1"
VISIBLE_INJECTION_MANIFEST_SCHEMA_VERSION = "visible_injection_manifest.v1"
ROUTE_PROMPT_CONTRACT_SCHEMA_VERSION = "route_prompt_contract.v1"
WORKER_PROMPT_CONTRACT_SCHEMA_VERSION = "worker_prompt_contract.v1"

LIGHTWEIGHT_SINGLE_LANE_TOPOLOGY = "lightweight_single_lane"
OBSERVER_LED_PARALLEL_TOPOLOGY = "observer_led_parallel_lanes"
SINGLE_LANE_RECOMMENDATION = "single_lane.v1"
PARALLEL_LANE_RECOMMENDATION = "mf_parallel.v1"
HIGH_RISK_PRIORITIES = {"P0", "P1"}
INDEPENDENT_VERIFICATION_EVIDENCE_IDS = (
    "independent_verification",
    "independent_verification_lane",
    "independent_verification_evidence",
)
OBSERVER_AUTHORITIES = (
    "merge",
    "redeploy_governance",
    "graph_reconcile",
    "backlog_close",
    "waiver_approval",
    "merge_queue_mutation",
)

_HIGH_RISK_PATH_MARKERS = (
    "agent/governance/service_router.py",
    "agent/governance/service_registry.py",
    "agent/governance/precheck_service.py",
    "agent/governance/mf_subagent_contract.py",
    "agent/governance/mf_workflow_runtime.py",
    "agent/governance/parallel_branch_runtime.py",
    "agent/governance/parallel_agent_contract.py",
    "agent/governance/backlog_",
    "agent/governance/contract_templates/",
    "agent/governance/server.py",
    "agent/mcp/",
    "frontend/dashboard/",
    "shared-volume/",
)
_HIGH_RISK_TEXT_MARKERS = (
    "governance",
    "routing",
    "route",
    "precheck",
    "runtime",
    "backlog",
    "permission",
    "observer",
    "merge",
    "reconcile",
    "redeploy",
    "waiver",
    "fence",
    "contract",
    "graph",
    "executor",
    "servicemanager",
)
_IMPLEMENTATION_MUTATION_ACTIONS = {
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
_DIRECT_IMPLEMENTATION_RISK_FLAGS = (
    "observer_direct_mutation",
    "same_observer_direct_mutation",
    "direct_mutation",
    "direct_implementation_risk",
    "implementation_mutation_requested",
)

DEFAULT_ROUTE_ALERTS: tuple[dict[str, Any], ...] = (
    {
        "code": "observer_judger_must_not_implement",
        "severity": "block",
        "applies_to": ["observer", "judger"],
        "blocked_actions": [
            "apply_patch",
            "edit_file",
            "edit_files",
            "write_file",
            "implementation_exec",
            "mutate_files",
            "run_implementation_command",
        ],
    },
    {
        "code": "implementation_prompt_must_live_in_route",
        "severity": "block",
        "applies_to": ["observer", "judger", "implementation_worker", "mf_sub", "qa"],
        "blocked_actions": [
            "use_untracked_implementation_prompt",
            "dispatch_worker_without_route_prompt_contract",
        ],
    },
    {
        "code": "judger_reuses_route_context",
        "severity": "warning",
        "applies_to": ["judger"],
    },
    {
        "code": "identity_context_not_skill_text",
        "severity": "info",
        "applies_to": ["observer", "judger", "implementation_worker", "mf_sub", "qa"],
    },
    {
        "code": "external_injection_requires_visible_route_ref",
        "severity": "block",
        "applies_to": ["observer", "judger", "implementation_worker", "mf_sub", "qa"],
        "blocked_actions": [
            "use_untracked_external_prompt",
            "use_context_outside_visible_injection_manifest",
        ],
    },
    {
        "code": "strategic_task_requires_parallel_lanes",
        "severity": "block",
        "applies_to": ["observer", "judger"],
    },
)

TOPOLOGY_ALERTS: tuple[dict[str, Any], ...] = (
    {
        "code": "independent_verification_required",
        "severity": "block",
        "applies_to": ["observer", "mf_sub", "implementation_worker", "qa"],
        "blocked_actions": [
            "merge_without_independent_verification",
            "close_without_independent_verification",
        ],
    },
    {
        "code": "observer_only_privileged_authorities",
        "severity": "block",
        "applies_to": ["observer", "mf_sub", "implementation_worker", "qa"],
        "blocked_actions": [
            "worker_merge",
            "worker_redeploy_governance",
            "worker_graph_reconcile",
            "worker_backlog_close",
            "worker_waiver_approval",
            "worker_merge_queue_mutation",
        ],
    },
    {
        "code": "lightweight_single_lane_selected",
        "severity": "info",
        "applies_to": ["observer", "mf_sub", "implementation_worker"],
    },
)


@dataclass(frozen=True)
class ServiceDescriptor:
    """Static descriptor for a deterministic local governance service."""

    service_id: str
    mode: str
    side_effect: str
    supported_events: tuple[str, ...] = field(default_factory=tuple)
    required_permissions: tuple[str, ...] = field(default_factory=tuple)
    idempotency_fields: tuple[str, ...] = field(default_factory=tuple)
    input_schema: Mapping[str, Any] = field(default_factory=dict)
    output_schema: Mapping[str, Any] = field(default_factory=dict)
    handler: ServiceHandler | None = None

    def __post_init__(self) -> None:
        if not self.service_id:
            raise ValueError("service_id is required")
        if self.mode not in ALLOWED_SERVICE_MODES:
            raise ValueError(f"{self.service_id}: unsupported service mode {self.mode!r}")
        if self.side_effect not in ALLOWED_SIDE_EFFECTS:
            raise ValueError(f"{self.service_id}: unsupported side_effect {self.side_effect!r}")
        if self.mode == "apply" or self.side_effect in WRITE_SIDE_EFFECTS:
            if not self.required_permissions:
                raise ValueError(f"{self.service_id}: apply/write service requires permissions")

    @property
    def side_effect_class(self) -> str:
        """Canonical route field alias for the descriptor side-effect class."""

        return self.side_effect


class ServiceRegistry:
    """In-memory registry for deterministic local governance service descriptors."""

    def __init__(self, descriptors: Mapping[str, ServiceDescriptor] | None = None) -> None:
        self._descriptors: dict[str, ServiceDescriptor] = dict(descriptors or {})

    def register(self, descriptor: ServiceDescriptor) -> None:
        self._descriptors[descriptor.service_id] = descriptor

    def get(self, service_id: str) -> ServiceDescriptor | None:
        return self._descriptors.get(service_id)

    def require(self, service_id: str) -> ServiceDescriptor:
        descriptor = self.get(service_id)
        if descriptor is None:
            raise KeyError(service_id)
        return descriptor

    def ids(self) -> set[str]:
        return set(self._descriptors)

    def as_dict(self) -> dict[str, ServiceDescriptor]:
        return dict(self._descriptors)


def deterministic_default_handler(
    event: Mapping[str, Any],
    route_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a compact deterministic service summary without external calls."""

    service_id = str(route_context.get("service_id") or "")
    route_id = str(route_context.get("route_id") or "")
    event_kind = str(event.get("event_kind") or event.get("kind") or "")
    return {
        "ok": True,
        "service_id": service_id,
        "route_id": route_id,
        "event_kind": event_kind,
        "summary": f"{service_id} handled {event_kind} via {route_id}",
    }


def observer_reminder_echo_handler(
    event: Mapping[str, Any],
    route_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Echo only the safe observer reminder fields for demo evidence."""

    service_id = str(route_context.get("service_id") or "")
    route_id = str(route_context.get("route_id") or "")
    event_kind = str(event.get("event_kind") or event.get("kind") or "")
    reminder = _reminder_payload(event)
    payload_included = reminder.get("payload_included")
    received_reminder = {
        "kind": _text(reminder.get("kind")),
        "project_id": _text(reminder.get("project_id") or event.get("project_id")),
        "message": _text(reminder.get("message")),
        "payload_included": payload_included if isinstance(payload_included, bool) else False,
        "next_action": _safe_next_action(
            reminder.get("next_action") or reminder.get("claim_instruction")
        ),
    }
    return {
        "ok": True,
        "service_id": service_id,
        "route_id": route_id,
        "event_kind": event_kind,
        "received_reminder": received_reminder,
        "received_reminder_echo": received_reminder,
        "payload_boundary": {
            "payload_included": received_reminder["payload_included"],
            "business_payload_excluded": True,
            "safe_fields": list(received_reminder.keys()),
        },
    }


def route_prompt_alert_bundle_handler(
    event: Mapping[str, Any],
    route_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a compact visible route bundle without raw prompt material."""

    service_id = _text(route_context.get("service_id"))
    service_route_id = _text(route_context.get("route_id"))
    event_kind = _text(event.get("event_kind") or event.get("kind"))
    payload = _route_prompt_payload(event)
    topology = classify_route_topology(payload)
    alerts = _route_alerts(
        payload.get("route_alerts"),
        stage=topology["stage"],
        topology_policy=topology,
        caller_role=_route_caller_role(payload),
    )
    content = _route_content(payload)
    route = _route_identity(payload, event)
    route["selected_topology"] = topology["selected_topology"]
    route["recommended_topology"] = topology["recommended_topology"]
    prompt_contract = _route_prompt_contract(payload, route=route)
    worker_prompt_contract = _worker_prompt_contract(
        payload,
        route=route,
        prompt_contract=prompt_contract,
        topology_policy=topology,
    )
    visible_manifest = _visible_injection_manifest(
        payload.get("visible_injection_manifest")
    )
    prompt_contract_hash = _sha256_json(worker_prompt_contract)
    bundle_without_hashes = {
        "schema_version": ROUTE_PROMPT_BUNDLE_SCHEMA_VERSION,
        "intent": _text(payload.get("intent") or payload.get("route_intent") or "implementation"),
        "content": content,
        "route": route,
        "selected_topology": topology["selected_topology"],
        "recommended_topology": topology["recommended_topology"],
        "required_lanes": topology["required_lanes"],
        "reason_codes": topology["reason_codes"],
        "observer_authorities": list(OBSERVER_AUTHORITIES),
        "verification_policy": {
            "independent_verification_required": topology[
                "independent_verification_required"
            ],
            "required_evidence_ids": list(INDEPENDENT_VERIFICATION_EVIDENCE_IDS),
        },
        "alerts": alerts,
        "prompt_contract": prompt_contract,
        "worker_prompt_contract": worker_prompt_contract,
        "visible_injection_manifest": visible_manifest,
        "prompt_contract_hash": prompt_contract_hash,
    }
    route_context_hash = _sha256_json(bundle_without_hashes)
    bundle = {
        **bundle_without_hashes,
        "route_context_hash": route_context_hash,
    }
    return {
        "ok": True,
        "service_id": service_id,
        "route_id": service_route_id,
        "event_kind": event_kind,
        "route_prompt_bundle": bundle,
        "bundle": bundle,
        "hashes": {
            "route_context_hash": route_context_hash,
            "prompt_contract_hash": prompt_contract_hash,
        },
        "payload_boundary": {
            "prompt_text_excluded": True,
            "context_text_excluded": True,
            "visible_manifest_only": True,
        },
    }


def classify_route_topology(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Classify route topology from bounded deterministic task evidence."""

    payload = dict(payload) if isinstance(payload, Mapping) else {}
    files = _route_file_list(payload)
    priority = _route_priority(payload)
    stage = _route_stage(payload)
    caller_role = _route_caller_role(payload)
    task_text = _route_task_text(payload)
    text_l = task_text.lower()
    reason_codes: list[str] = []

    explicit_topology = _text(
        payload.get("selected_topology")
        or payload.get("recommended_topology")
        or payload.get("topology")
    )
    explicit_parallel = explicit_topology in {
        OBSERVER_LED_PARALLEL_TOPOLOGY,
        PARALLEL_LANE_RECOMMENDATION,
        "mf_parallel",
        "parallel",
    }
    explicit_single = explicit_topology in {
        LIGHTWEIGHT_SINGLE_LANE_TOPOLOGY,
        SINGLE_LANE_RECOMMENDATION,
        "single_lane",
        "single",
    }

    if priority in HIGH_RISK_PRIORITIES:
        reason_codes.append(f"priority_{priority.lower()}")
    if any(_path_is_high_risk(path) for path in files):
        reason_codes.append("governance_routing_runtime_surface")
    if any(marker in text_l for marker in _HIGH_RISK_TEXT_MARKERS):
        reason_codes.append("governance_routing_runtime_terms")
    if _cross_module_change(files):
        reason_codes.append("cross_module_change")
    if caller_role in {"observer", "judger"} and _has_direct_implementation_evidence(payload):
        reason_codes.append("observer_direct_implementation_risk")
    if explicit_parallel:
        reason_codes.append("explicit_parallel_topology")

    tiny_deterministic = _boolish(
        payload.get("tiny_deterministic")
        or payload.get("tiny_deterministic_scope")
        or payload.get("small_deterministic")
    )
    risk_class = _text(payload.get("risk_class") or payload.get("risk")).lower()
    small_deterministic = (
        tiny_deterministic
        or risk_class in {"low", "small", "small_deterministic", "deterministic"}
    )
    if explicit_single:
        reason_codes.append("explicit_single_lane_topology")
    if small_deterministic and not reason_codes:
        reason_codes.append("small_deterministic")

    high_risk = explicit_parallel or any(
        code
        for code in reason_codes
        if code
        not in {
            "small_deterministic",
            "explicit_single_lane_topology",
        }
    )
    if explicit_single and not high_risk:
        high_risk = False

    selected_topology = (
        OBSERVER_LED_PARALLEL_TOPOLOGY if high_risk else LIGHTWEIGHT_SINGLE_LANE_TOPOLOGY
    )
    recommended_topology = (
        PARALLEL_LANE_RECOMMENDATION if high_risk else SINGLE_LANE_RECOMMENDATION
    )
    if not reason_codes:
        reason_codes.append("small_deterministic")

    return {
        "schema_version": "route_topology_selection.v1",
        "selected_topology": selected_topology,
        "recommended_topology": recommended_topology,
        "required_lanes": _required_lanes_for_topology(selected_topology),
        "reason_codes": _dedupe_text(reason_codes),
        "observer_authorities": list(OBSERVER_AUTHORITIES),
        "priority": priority,
        "stage": stage,
        "target_file_count": len(files),
        "independent_verification_required": selected_topology
        == OBSERVER_LED_PARALLEL_TOPOLOGY,
    }


def route_action_precheck_handler(
    event: Mapping[str, Any],
    route_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Run the local route action gate from a deterministic service route."""

    from agent.governance.mf_subagent_contract import (
        MfSubagentContractError,
        ROUTE_ACTION_GATE_SCHEMA_VERSION,
        validate_route_action_gate,
    )

    raw_payload = event.get("payload")
    payload = raw_payload if isinstance(raw_payload, Mapping) else _route_prompt_payload(event)
    try:
        evidence = validate_route_action_gate(payload)
        ok = True
        status = "allowed"
        reason = ""
    except MfSubagentContractError as exc:
        evidence = _route_action_blocked_evidence(
            payload,
            schema_version=ROUTE_ACTION_GATE_SCHEMA_VERSION,
            reason=_text(exc),
        )
        ok = False
        status = evidence["status"]
        reason = evidence["reason"]
    return {
        "ok": ok,
        "allowed": ok,
        "status": status,
        "reason": reason,
        "service_id": _text(route_context.get("service_id")),
        "route_id": _text(route_context.get("route_id")),
        "event_kind": _text(event.get("event_kind") or event.get("kind")),
        "route_action_gate": evidence,
        "payload_boundary": {
            "prompt_text_excluded": True,
            "context_text_excluded": True,
            "visible_manifest_only": True,
        },
    }


def _reminder_payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = event.get("payload")
    payload_map = payload if isinstance(payload, Mapping) else {}
    for key in ("hook_reminder", "received_reminder"):
        value = event.get(key)
        if isinstance(value, Mapping):
            return value
        value = payload_map.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _route_prompt_payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = event.get("payload")
    payload_map = payload if isinstance(payload, Mapping) else {}
    for key in ("route_context", "route_prompt_bundle", "prompt_route"):
        value = payload_map.get(key)
        if isinstance(value, Mapping):
            return {**dict(payload_map), **dict(value)}
    return payload_map if payload_map else event


def _route_action_blocked_evidence(
    payload: Mapping[str, Any],
    *,
    schema_version: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "allowed": False,
        "status": "route_action_policy_blocked",
        "reason": reason,
        "action": _text(
            payload.get("action")
            or payload.get("requested_action")
            or payload.get("tool_name")
        ),
        "caller_role": _text(
            payload.get("caller_role")
            or payload.get("role")
            or payload.get("actor_role")
        ),
        "route_context_hash": _payload_route_context_hash(payload),
        "prompt_contract_id": _payload_prompt_contract_id(payload),
        "prompt_contract_hash": _payload_prompt_contract_hash(payload),
    }


def _payload_route_context_hash(payload: Mapping[str, Any]) -> str:
    route_context = payload.get("route_context")
    route_map = route_context if isinstance(route_context, Mapping) else {}
    prompt_contract = payload.get("prompt_contract")
    prompt_map = prompt_contract if isinstance(prompt_contract, Mapping) else {}
    route_prompt_contract = payload.get("route_prompt_contract")
    route_prompt_map = (
        route_prompt_contract if isinstance(route_prompt_contract, Mapping) else {}
    )
    route_prompt_bundle = payload.get("route_prompt_bundle")
    route_bundle_map = (
        route_prompt_bundle if isinstance(route_prompt_bundle, Mapping) else {}
    )
    bundle = payload.get("bundle")
    bundle_map = bundle if isinstance(bundle, Mapping) else {}
    return _text(
        payload.get("route_context_hash")
        or route_map.get("route_context_hash")
        or prompt_map.get("route_context_hash")
        or route_prompt_map.get("route_context_hash")
        or route_bundle_map.get("route_context_hash")
        or bundle_map.get("route_context_hash")
    )


def _payload_prompt_contract_id(payload: Mapping[str, Any]) -> str:
    route_context = payload.get("route_context")
    route_map = route_context if isinstance(route_context, Mapping) else {}
    prompt_contract = payload.get("prompt_contract")
    prompt_map = prompt_contract if isinstance(prompt_contract, Mapping) else {}
    route_prompt_contract = payload.get("route_prompt_contract")
    route_prompt_map = (
        route_prompt_contract if isinstance(route_prompt_contract, Mapping) else {}
    )
    route_prompt_bundle = payload.get("route_prompt_bundle")
    route_bundle_map = (
        route_prompt_bundle if isinstance(route_prompt_bundle, Mapping) else {}
    )
    route_bundle_prompt = route_bundle_map.get("prompt_contract")
    route_bundle_prompt_map = (
        route_bundle_prompt if isinstance(route_bundle_prompt, Mapping) else {}
    )
    bundle = payload.get("bundle")
    bundle_map = bundle if isinstance(bundle, Mapping) else {}
    bundle_prompt = bundle_map.get("prompt_contract")
    bundle_prompt_map = bundle_prompt if isinstance(bundle_prompt, Mapping) else {}
    return _text(
        payload.get("prompt_contract_id")
        or route_map.get("prompt_contract_id")
        or prompt_map.get("prompt_contract_id")
        or prompt_map.get("id")
        or route_prompt_map.get("prompt_contract_id")
        or route_prompt_map.get("id")
        or route_bundle_map.get("prompt_contract_id")
        or route_bundle_prompt_map.get("prompt_contract_id")
        or route_bundle_prompt_map.get("id")
        or bundle_map.get("prompt_contract_id")
        or bundle_prompt_map.get("prompt_contract_id")
        or bundle_prompt_map.get("id")
    )


def _payload_prompt_contract_hash(payload: Mapping[str, Any]) -> str:
    route_context = payload.get("route_context")
    route_map = route_context if isinstance(route_context, Mapping) else {}
    prompt_contract = payload.get("prompt_contract")
    prompt_map = prompt_contract if isinstance(prompt_contract, Mapping) else {}
    route_prompt_contract = payload.get("route_prompt_contract")
    route_prompt_map = (
        route_prompt_contract if isinstance(route_prompt_contract, Mapping) else {}
    )
    route_prompt_bundle = payload.get("route_prompt_bundle")
    route_bundle_map = (
        route_prompt_bundle if isinstance(route_prompt_bundle, Mapping) else {}
    )
    bundle = payload.get("bundle")
    bundle_map = bundle if isinstance(bundle, Mapping) else {}
    return _text(
        payload.get("prompt_contract_hash")
        or prompt_map.get("prompt_contract_hash")
        or route_map.get("prompt_contract_hash")
        or route_prompt_map.get("prompt_contract_hash")
        or route_bundle_map.get("prompt_contract_hash")
        or bundle_map.get("prompt_contract_hash")
    )


def _route_alerts(
    value: Any,
    *,
    stage: str = "",
    topology_policy: Mapping[str, Any] | None = None,
    caller_role: str = "",
) -> list[dict[str, Any]]:
    source = value if isinstance(value, list) and value else list(DEFAULT_ROUTE_ALERTS)
    topology = topology_policy if isinstance(topology_policy, Mapping) else {}
    selected_topology = _text(topology.get("selected_topology"))
    role = _text(caller_role).lower()
    route_owned_alerts: list[dict[str, Any]] = [DEFAULT_ROUTE_ALERTS[0]]
    if value is None and selected_topology == LIGHTWEIGHT_SINGLE_LANE_TOPOLOGY:
        source = [
            DEFAULT_ROUTE_ALERTS[1],
            DEFAULT_ROUTE_ALERTS[4],
        ]
    if selected_topology == OBSERVER_LED_PARALLEL_TOPOLOGY:
        route_owned_alerts.extend(
            alert
            for alert in TOPOLOGY_ALERTS
            if alert["code"] != "lightweight_single_lane_selected"
        )
    elif selected_topology == LIGHTWEIGHT_SINGLE_LANE_TOPOLOGY:
        route_owned_alerts.append(TOPOLOGY_ALERTS[2])
    source = [*route_owned_alerts, *source]
    alerts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in source:
        if isinstance(item, str):
            alert = {"code": _text(item)}
        elif isinstance(item, Mapping):
            alert = {
                "code": _text(item.get("code")),
                "severity": _text(item.get("severity") or "info"),
                "applies_to": _list_of_text(item.get("applies_to")),
                "blocked_actions": _list_of_text(item.get("blocked_actions")),
            }
        else:
            continue
        code = alert.get("code", "")
        if not code or code in seen:
            continue
        applies_to = {_text(value).lower() for value in alert.get("applies_to", [])}
        if role and applies_to and role not in applies_to:
            continue
        if not _alert_applies_to_stage(code, stage):
            continue
        seen.add(code)
        alerts.append({key: value for key, value in alert.items() if value not in ("", [])})
    return alerts


def _alert_applies_to_stage(code: str, stage: str) -> bool:
    stage = _text(stage)
    if code == "observer_only_privileged_authorities":
        return stage in {"", "merge_gate", "merge_queue_entry", "merge_preview", "live_merge", "reconcile", "close_gate"}
    if code == "independent_verification_required":
        return stage in {"", "dispatch", "implementation_wait", "handoff_gate", "merge_gate", "close_gate"}
    return True


def _has_direct_implementation_evidence(payload: Mapping[str, Any]) -> bool:
    if any(_boolish(payload.get(flag)) for flag in _DIRECT_IMPLEMENTATION_RISK_FLAGS):
        return True
    action = _normalized_action(
        payload.get("action")
        or payload.get("requested_action")
        or payload.get("tool_name")
    )
    return action in _IMPLEMENTATION_MUTATION_ACTIONS


def _normalized_action(value: Any) -> str:
    return _text(value).lower().replace("-", "_").replace(".", "_")


def _route_content(payload: Mapping[str, Any]) -> dict[str, Any]:
    content = payload.get("content")
    content_map = content if isinstance(content, Mapping) else {}
    summary = _text(
        content_map.get("summary")
        or payload.get("content_summary")
        or payload.get("task_summary")
        or payload.get("summary")
    )
    kind = _text(content_map.get("kind") or payload.get("content_kind") or "task_summary")
    visible = {
        "kind": kind,
        "summary": summary,
        "prompt_text_included": False,
        "context_text_included": False,
    }
    visible["content_hash"] = _sha256_json({
        "kind": visible["kind"],
        "summary": visible["summary"],
    })
    return visible


def _route_identity(payload: Mapping[str, Any], event: Mapping[str, Any]) -> dict[str, Any]:
    route = payload.get("route")
    route_map = route if isinstance(route, Mapping) else {}
    return {
        "route_id": _text(route_map.get("route_id") or payload.get("route_id") or event.get("route_id")),
        "stage": _text(route_map.get("stage") or payload.get("stage") or event.get("stage")),
        "caller_role": _text(
            route_map.get("caller_role")
            or payload.get("caller_role")
            or payload.get("role")
            or event.get("caller_role")
        ),
        "route_intent": _text(
            route_map.get("route_intent")
            or payload.get("route_intent")
            or payload.get("intent")
            or "implementation"
        ),
    }


def _route_prompt_contract(
    payload: Mapping[str, Any],
    *,
    route: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    prompt_contract = payload.get("prompt_contract")
    contract_map = prompt_contract if isinstance(prompt_contract, Mapping) else {}
    route_identity = dict(route) if isinstance(route, Mapping) else {}
    return {
        "schema_version": _text(
            contract_map.get("schema_version") or ROUTE_PROMPT_CONTRACT_SCHEMA_VERSION
        ),
        "prompt_contract_id": _text(
            contract_map.get("prompt_contract_id")
            or contract_map.get("id")
            or payload.get("prompt_contract_id")
        ),
        "prompt_kind": _text(
            contract_map.get("prompt_kind")
            or payload.get("prompt_kind")
            or "implementation"
        ),
        "target_files": _list_of_text(contract_map.get("target_files") or payload.get("target_files")),
        "test_files": _list_of_text(contract_map.get("test_files") or payload.get("test_files")),
        "acceptance_criteria": _list_of_text(
            contract_map.get("acceptance_criteria") or payload.get("acceptance_criteria")
        ),
        "evidence_required": _list_of_text(
            contract_map.get("evidence_required") or payload.get("evidence_required")
        ),
        "route_identity": {
            key: value
            for key, value in {
                "route_id": _text(route_identity.get("route_id")),
                "stage": _text(route_identity.get("stage")),
                "caller_role": _text(route_identity.get("caller_role")),
                "route_intent": _text(route_identity.get("route_intent")),
                "project_id": _text(payload.get("project_id")),
                "backlog_id": _text(payload.get("backlog_id")),
                "task_id": _text(payload.get("task_id")),
                "parent_task_id": _text(payload.get("parent_task_id")),
                "worker_role": _text(payload.get("worker_role") or payload.get("role")),
                "branch": _text(payload.get("branch") or payload.get("branch_ref")),
                "fence_token": _text(payload.get("fence_token")),
            }.items()
            if value
        },
    }


def _worker_prompt_contract(
    payload: Mapping[str, Any],
    *,
    route: Mapping[str, Any],
    prompt_contract: Mapping[str, Any],
    topology_policy: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": WORKER_PROMPT_CONTRACT_SCHEMA_VERSION,
        "prompt_contract_id": _text(prompt_contract.get("prompt_contract_id")),
        "prompt_kind": _text(prompt_contract.get("prompt_kind") or "implementation"),
        "target_files": _list_of_text(prompt_contract.get("target_files")),
        "test_files": _list_of_text(prompt_contract.get("test_files")),
        "acceptance_criteria": _list_of_text(prompt_contract.get("acceptance_criteria")),
        "evidence_required": _list_of_text(prompt_contract.get("evidence_required")),
        "route_identity": dict(_mapping(prompt_contract.get("route_identity"))),
        "selected_topology": _text(topology_policy.get("selected_topology")),
        "recommended_topology": _text(topology_policy.get("recommended_topology")),
        "required_lanes": list(_list_of_mappings(topology_policy.get("required_lanes"))),
        "reason_codes": _list_of_text(topology_policy.get("reason_codes")),
        "observer_authorities": list(OBSERVER_AUTHORITIES),
        "forbidden_context_sources": [
            "observer_only_context",
            "raw_private_memory",
            "hidden_context",
            "unmanifested_prompt_text",
        ],
        "payload_boundary": {
            "target_and_test_files_only": True,
            "observer_only_context_excluded": True,
            "prompt_text_excluded": True,
        },
    }


def _visible_injection_manifest(value: Any) -> dict[str, Any]:
    manifest = value if isinstance(value, Mapping) else {}
    allowed = manifest.get("allowed_injections")
    entries = allowed if isinstance(allowed, list) else []
    compact_entries: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        compact = {
            "kind": _text(entry.get("kind")),
            "id": _text(entry.get("id")),
            "source_ref": _text(entry.get("source_ref") or entry.get("path") or entry.get("ref")),
            "sha256": _text(entry.get("sha256") or entry.get("hash") or entry.get("content_hash")),
            "status": _text(entry.get("status")),
        }
        compact_entries.append(
            {key: item for key, item in compact.items() if item}
        )
    return {
        "schema_version": _text(
            manifest.get("schema_version") or VISIBLE_INJECTION_MANIFEST_SCHEMA_VERSION
        ),
        "policy": _text(manifest.get("policy") or "route_owned_visible_refs_only"),
        "allowed_injections": compact_entries,
    }


def _route_file_list(payload: Mapping[str, Any]) -> list[str]:
    files: list[str] = []
    for key in (
        "target_files",
        "test_files",
        "changed_files",
        "owned_files",
        "write_scope",
    ):
        files.extend(_list_of_text(payload.get(key)))
    for nested_key in ("prompt_contract", "worker_prompt_contract", "work"):
        nested = _mapping(payload.get(nested_key))
        for key in (
            "target_files",
            "test_files",
            "changed_files",
            "owned_files",
            "write_scope",
        ):
            files.extend(_list_of_text(nested.get(key)))
    return _dedupe_text(files)


def _route_priority(payload: Mapping[str, Any]) -> str:
    for key in ("priority", "severity", "risk_priority"):
        token = _text(payload.get(key)).upper()
        if token:
            return token
    content = _mapping(payload.get("content"))
    return _text(content.get("priority")).upper()


def _route_stage(payload: Mapping[str, Any]) -> str:
    route = _mapping(payload.get("route"))
    return _text(route.get("stage") or payload.get("stage"))


def _route_caller_role(payload: Mapping[str, Any]) -> str:
    route = _mapping(payload.get("route"))
    return _text(
        route.get("caller_role")
        or payload.get("caller_role")
        or payload.get("role")
        or payload.get("actor_role")
    ).lower()


def _route_task_text(payload: Mapping[str, Any]) -> str:
    content = _mapping(payload.get("content"))
    parts = [
        content.get("summary"),
        payload.get("content_summary"),
        payload.get("task_summary"),
        payload.get("summary"),
        payload.get("title"),
        payload.get("description"),
    ]
    return " ".join(_text(part) for part in parts if _text(part))


def _path_is_high_risk(path: str) -> bool:
    normalized = _text(path).replace("\\", "/")
    return any(marker in normalized for marker in _HIGH_RISK_PATH_MARKERS)


def _cross_module_change(files: Sequence[str]) -> bool:
    buckets = {
        "/".join(_text(path).replace("\\", "/").split("/")[:2])
        for path in files
        if _text(path)
    }
    return len(files) > 3 or len(buckets) > 1


def _required_lanes_for_topology(selected_topology: str) -> list[dict[str, str]]:
    if selected_topology == OBSERVER_LED_PARALLEL_TOPOLOGY:
        return [
            {
                "id": "observer_coordinator",
                "role": "observer",
                "purpose": "own routing, dispatch, review, merge, reconcile, close, and waiver decisions",
            },
            {
                "id": "bounded_implementation_worker",
                "role": "mf_sub",
                "purpose": "implement only the bounded target files and required evidence",
            },
            {
                "id": "independent_verification_lane",
                "role": "qa",
                "purpose": "independently verify behavior, evidence, and dirty scope before merge or close",
            },
            {
                "id": "observer_merge_close_gate",
                "role": "observer",
                "purpose": "accept merge, redeploy, graph reconcile, backlog close, and waivers only after evidence passes",
            },
        ]
    return [
        {
            "id": "single_bounded_worker",
            "role": "mf_sub",
            "purpose": "perform deterministic implementation and focused verification in one bounded lane",
        }
    ]


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list_of_mappings(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        return [dict(value)]
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _list_of_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        return []
    return [_text(item) for item in value if _text(item)]


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _dedupe_text(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = _text(value)
        if token and token not in seen:
            result.append(token)
            seen.add(token)
    return result


def _sha256_json(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_next_action(value: Any) -> dict[str, str]:
    if isinstance(value, Mapping):
        return {
            "tool": _text(value.get("tool") or "observer_command_next"),
            "description": _text(
                value.get("description") or "claim the next pending observer command"
            ),
        }
    description = _text(value) or "claim the next pending observer command"
    return {
        "tool": "observer_command_next",
        "description": description,
    }


def default_service_descriptors() -> dict[str, ServiceDescriptor]:
    """Return the built-in deterministic governance service descriptors."""

    common_idempotency = ("event_id", "event_kind", "stage", "task_id", "backlog_id")
    return {
        "test_governance.preview": ServiceDescriptor(
            service_id="test_governance.preview",
            mode="preview",
            side_effect="read",
            supported_events=("task.completed", "ai.structured_output.validated"),
            idempotency_fields=common_idempotency,
            handler=deterministic_default_handler,
        ),
        "cleanup.preview": ServiceDescriptor(
            service_id="cleanup.preview",
            mode="preview",
            side_effect="read",
            supported_events=("cleanup.requested",),
            idempotency_fields=common_idempotency,
            handler=deterministic_default_handler,
        ),
        "cleanup.apply": ServiceDescriptor(
            service_id="cleanup.apply",
            mode="apply",
            side_effect="write",
            supported_events=("cleanup.requested",),
            required_permissions=("cleanup.apply",),
            idempotency_fields=common_idempotency,
            handler=deterministic_default_handler,
        ),
        "review.recommendations": ServiceDescriptor(
            service_id="review.recommendations",
            mode="preview",
            side_effect="read",
            supported_events=("task.completed", "review.requested"),
            idempotency_fields=common_idempotency,
            handler=deterministic_default_handler,
        ),
        "precheck.run": ServiceDescriptor(
            service_id="precheck.run",
            mode="gate",
            side_effect="gate",
            supported_events=("precheck.requested", "stage.completed"),
            idempotency_fields=common_idempotency,
            handler=deterministic_default_handler,
        ),
        "gate.close": ServiceDescriptor(
            service_id="gate.close",
            mode="gate",
            side_effect="gate",
            supported_events=("backlog.close.requested",),
            idempotency_fields=common_idempotency,
            handler=deterministic_default_handler,
        ),
        "observer.reminder_echo": ServiceDescriptor(
            service_id="observer.reminder_echo",
            mode="preview",
            side_effect="read",
            supported_events=("observer.command.notified",),
            idempotency_fields=(
                "event_id",
                "event_kind",
                "project_id",
                "route_id",
                "service_id",
            ),
            handler=observer_reminder_echo_handler,
        ),
        "route.prompt_alert_bundle": ServiceDescriptor(
            service_id="route.prompt_alert_bundle",
            mode="preview",
            side_effect="read",
            supported_events=("route.prompt_context.requested",),
            idempotency_fields=(
                "event_id",
                "event_kind",
                "stage",
                "payload.route_id",
                "payload.prompt_contract_id",
            ),
            handler=route_prompt_alert_bundle_handler,
        ),
        "route.action_precheck": ServiceDescriptor(
            service_id="route.action_precheck",
            mode="gate",
            side_effect="gate",
            supported_events=("route.action.requested",),
            idempotency_fields=(
                "event_id",
                "event_kind",
                "stage",
                "payload.route_context_hash",
                "payload.prompt_contract_id",
                "payload.prompt_contract_hash",
                "payload.action",
                "payload.caller_role",
            ),
            handler=route_action_precheck_handler,
        ),
    }


def default_service_registry() -> ServiceRegistry:
    """Build a registry with built-in deterministic governance services."""

    return ServiceRegistry(default_service_descriptors())
