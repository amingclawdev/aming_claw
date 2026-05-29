"""Contract-bound event router for deterministic governance services."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import sqlite3
from typing import Any

from agent.governance.contract_template_registry import (
    ContractTemplateError,
    get_contract_template,
)
from agent.governance.service_registry import (
    ServiceDescriptor,
    ServiceRegistry,
    WRITE_SIDE_EFFECTS,
    default_service_registry,
)


def route_event(
    event: Mapping[str, Any],
    contract: Mapping[str, Any],
    registry: ServiceRegistry | None = None,
    *,
    call_handlers: bool = True,
) -> dict[str, Any]:
    """Resolve one governance event against a contract route table."""

    router = ServiceRouter(registry or default_service_registry())
    return router.route(event, contract, call_handlers=call_handlers)


def route_timeline_event(
    conn: sqlite3.Connection,
    timeline_event: Mapping[str, Any],
    registry: ServiceRegistry | None = None,
    *,
    call_handlers: bool = True,
    record: bool = True,
) -> dict[str, Any]:
    """Route one durable task_timeline event and optionally record evidence rows."""

    event_type = _string(timeline_event.get("event_type"))
    payload = _mapping(timeline_event.get("payload"))
    if event_type.startswith("service.route.") or payload.get("service_router_suppress") is True:
        return {
            "status": "suppressed",
            "decision": "no_op",
            "event_kind": event_type,
            "route_count": 0,
            "routes": [],
        }

    contract = _resolve_timeline_contract(conn, timeline_event)
    if not contract:
        return {
            "status": "no_contract",
            "decision": "no_op",
            "event_kind": event_type,
            "route_count": 0,
            "routes": [],
        }

    router_event = _normalize_timeline_event(timeline_event)
    result = route_event(
        router_event,
        contract,
        registry=registry,
        call_handlers=call_handlers,
    )
    if record and result.get("routes"):
        _record_timeline_route_results(conn, timeline_event, router_event, result)
    return result


class ServiceRouter:
    """Small deterministic router over contract event/service route declarations."""

    def __init__(self, registry: ServiceRegistry | None = None) -> None:
        self.registry = registry or default_service_registry()

    def route(
        self,
        event: Mapping[str, Any],
        contract: Mapping[str, Any],
        *,
        call_handlers: bool = True,
    ) -> dict[str, Any]:
        event_kind = _event_kind(event)
        stage = _string(event.get("stage"))
        event_routes = _event_routes(contract)
        service_routes = _service_routes(contract)
        matching_routes = [
            route for route in event_routes if _route_matches(route, event_kind=event_kind, stage=stage)
        ]
        if not matching_routes:
            return {
                "status": "no_op",
                "decision": "no_op",
                "event_kind": event_kind,
                "route_count": 0,
                "routes": [],
            }

        results = [
            self._run_route(
                event,
                contract,
                event_route,
                service_routes,
                call_handlers=call_handlers,
            )
            for event_route in matching_routes
        ]
        blocked = [result for result in results if result["decision"] == "block"]
        allowed = [result for result in results if result["decision"] == "allow"]
        return {
            "status": "blocked" if blocked else "routed",
            "decision": "block" if blocked else "allow",
            "event_kind": event_kind,
            "route_count": len(results),
            "routes": results,
            "allowed_count": len(allowed),
            "blocked_count": len(blocked),
        }

    def _run_route(
        self,
        event: Mapping[str, Any],
        contract: Mapping[str, Any],
        event_route: Mapping[str, Any],
        service_routes: Mapping[str, Mapping[str, Any]],
        *,
        call_handlers: bool,
    ) -> dict[str, Any]:
        route_id = _route_id(event_route)
        service_route = _resolve_service_route(event_route, service_routes)
        service_id = _string(
            service_route.get("service_id")
            or event_route.get("service_id")
            or event_route.get("service")
        )
        descriptor = self.registry.get(service_id)
        if descriptor is None:
            idempotency_key = _fallback_idempotency_key(event, contract, route_id, service_id)
            return {
                "route_id": route_id,
                "service_id": service_id,
                "decision": "block",
                "status": "unknown_service",
                "reason": f"unknown service: {service_id}",
                "idempotency_key": idempotency_key,
            }
        event_kind = _event_kind(event)
        if descriptor.supported_events and event_kind not in descriptor.supported_events:
            idempotency_key = _fallback_idempotency_key(event, contract, route_id, service_id)
            return {
                "route_id": route_id,
                "service_id": service_id,
                "decision": "block",
                "status": "unsupported_event",
                "reason": f"{service_id} does not support {event_kind}",
                "idempotency_key": idempotency_key,
            }

        merged = _merge_descriptor_route(descriptor, service_route, event_route)
        idempotency_key = _idempotency_key(event, contract, merged, route_id, service_id)
        permission_check = _permission_check(event, contract, merged)
        if permission_check:
            return {
                "route_id": route_id,
                "service_id": service_id,
                "mode": merged["mode"],
                "side_effect_class": merged["side_effect_class"],
                "side_effect": merged["side_effect_class"],
                "decision": "block",
                "status": "permission_blocked",
                "reason": permission_check,
                "idempotency_key": idempotency_key,
            }

        result_summary: dict[str, Any] | None = None
        if call_handlers:
            handler = descriptor.handler
            if handler is not None:
                result_summary = handler(
                    event,
                    {
                        "contract_id": _contract_id(contract),
                        "route_id": route_id,
                        "service_id": service_id,
                        "mode": merged["mode"],
                        "side_effect_class": merged["side_effect_class"],
                        "side_effect": merged["side_effect_class"],
                        "idempotency_key": idempotency_key,
                    },
                )
        if result_summary is None:
            result_summary = {
                "ok": True,
                "summary": f"{service_id} allowed for {_event_kind(event)}",
            }
        return {
            "route_id": route_id,
            "service_id": service_id,
            "mode": merged["mode"],
            "side_effect_class": merged["side_effect_class"],
            "side_effect": merged["side_effect_class"],
            "decision": "allow",
            "status": "allowed",
            "idempotency_key": idempotency_key,
            "result": result_summary,
        }


def _event_routes(contract: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    routes = contract.get("event_routes") or []
    if isinstance(routes, list):
        return [route for route in routes if isinstance(route, Mapping)]
    if isinstance(routes, Mapping):
        return [
            {**dict(route), "route_id": str(route.get("route_id") or route_id)}
            for route_id, route in routes.items()
            if isinstance(route, Mapping)
        ]
    return []


def _service_routes(contract: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    routes = contract.get("service_routes") or {}
    if isinstance(routes, Mapping):
        return {
            str(route_id): {**dict(route), "route_id": str(route.get("route_id") or route_id)}
            for route_id, route in routes.items()
            if isinstance(route, Mapping)
        }
    if isinstance(routes, list):
        out: dict[str, Mapping[str, Any]] = {}
        for route in routes:
            if isinstance(route, Mapping):
                route_id = _route_id(route)
                out[route_id] = dict(route)
        return out
    return {}


def _route_matches(route: Mapping[str, Any], *, event_kind: str, stage: str) -> bool:
    if route.get("enabled") is False:
        return False
    route_event_kind = _string(route.get("event_kind") or route.get("kind"))
    if route_event_kind != event_kind:
        return False
    route_stage = _string(route.get("stage"))
    route_stages = _list_of_strings(route.get("stages"))
    if not route_stage and not route_stages:
        return True
    if not stage:
        return False
    return route_stage == stage or stage in route_stages


def _resolve_service_route(
    event_route: Mapping[str, Any],
    service_routes: Mapping[str, Mapping[str, Any]],
) -> Mapping[str, Any]:
    service_route_id = _string(event_route.get("service_route_id"))
    if service_route_id:
        return service_routes.get(service_route_id, {})
    route_id = _route_id(event_route)
    return service_routes.get(route_id, {})


def _merge_descriptor_route(
    descriptor: ServiceDescriptor,
    service_route: Mapping[str, Any],
    event_route: Mapping[str, Any],
) -> dict[str, Any]:
    idempotency_policy = (
        service_route.get("idempotency_key_policy")
        or event_route.get("idempotency_key_policy")
        or {"fields": list(descriptor.idempotency_fields)}
    )
    return {
        "mode": _string(service_route.get("mode") or event_route.get("mode") or descriptor.mode),
        "side_effect_class": _string(
            service_route.get("side_effect_class")
            or event_route.get("side_effect_class")
            or service_route.get("side_effect")
            or event_route.get("side_effect")
            or descriptor.side_effect
        ),
        "required_permissions": _list_of_strings(
            service_route.get("required_permissions")
            or event_route.get("required_permissions")
            or list(descriptor.required_permissions)
        ),
        "idempotency_key_policy": idempotency_policy,
    }


def _permission_check(
    event: Mapping[str, Any],
    contract: Mapping[str, Any],
    route: Mapping[str, Any],
) -> str:
    mode = _string(route.get("mode"))
    side_effect_class = _string(route.get("side_effect_class") or route.get("side_effect"))
    required = _list_of_strings(route.get("required_permissions"))
    if mode != "apply" and side_effect_class not in WRITE_SIDE_EFFECTS:
        return ""
    if not required:
        return "apply/write route requires explicit permissions"
    granted = {
        *_list_of_strings(event.get("permissions")),
        *_list_of_strings(event.get("granted_permissions")),
        *_list_of_strings(_mapping(event.get("payload")).get("permissions")),
        *_list_of_strings(contract.get("permissions")),
        *_list_of_strings(contract.get("granted_permissions")),
    }
    missing = [permission for permission in required if permission not in granted]
    if missing:
        return "missing permissions: " + ", ".join(missing)
    return ""


def _idempotency_key(
    event: Mapping[str, Any],
    contract: Mapping[str, Any],
    route: Mapping[str, Any],
    route_id: str,
    service_id: str,
) -> str:
    policy = route.get("idempotency_key_policy")
    if isinstance(policy, Mapping):
        fields = _list_of_strings(policy.get("fields"))
    elif isinstance(policy, list):
        fields = _list_of_strings(policy)
    else:
        fields = []
    if not fields:
        fields = ["event_id", "event_kind", "stage", "task_id", "backlog_id"]
    values = {
        field: _value_for_field(event, contract, field, route_id=route_id, service_id=service_id)
        for field in fields
    }
    digest = hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"service-route:{service_id}:{route_id}:{digest}"


def _fallback_idempotency_key(
    event: Mapping[str, Any],
    contract: Mapping[str, Any],
    route_id: str,
    service_id: str,
) -> str:
    return _idempotency_key(
        event,
        contract,
        {"idempotency_key_policy": ["event_id", "event_kind", "stage", "task_id", "backlog_id"]},
        route_id,
        service_id or "unknown",
    )


def _normalize_timeline_event(timeline_event: Mapping[str, Any]) -> dict[str, Any]:
    payload = _mapping(timeline_event.get("payload"))
    event_id = timeline_event.get("id") or timeline_event.get("event_id") or ""
    phase = _string(timeline_event.get("phase"))
    return {
        "event_id": str(event_id),
        "source_event_id": str(event_id),
        "event_kind": _string(timeline_event.get("event_type")),
        "project_id": _string(timeline_event.get("project_id")),
        "task_id": _string(timeline_event.get("task_id")),
        "backlog_id": _string(timeline_event.get("backlog_id")),
        "mf_id": _string(timeline_event.get("mf_id")),
        "attempt_num": timeline_event.get("attempt_num") or 0,
        "phase": phase,
        "stage": _string(timeline_event.get("stage") or payload.get("stage") or ""),
        "status": _string(timeline_event.get("status")),
        "payload": payload,
        "artifact_refs": _mapping(timeline_event.get("artifact_refs")),
        "trace_id": _string(timeline_event.get("trace_id")),
    }


def _resolve_timeline_contract(
    conn: sqlite3.Connection,
    timeline_event: Mapping[str, Any],
) -> dict[str, Any]:
    explicit = _explicit_contract_from_timeline_event(timeline_event)
    if explicit:
        return _with_template_routes(explicit)

    backlog_id = _string(timeline_event.get("backlog_id"))
    if not backlog_id:
        return {}
    try:
        row = conn.execute(
            "SELECT chain_trigger_json FROM backlog_bugs WHERE bug_id = ?",
            (backlog_id,),
        ).fetchone()
    except sqlite3.Error:
        return {}
    if not row:
        return {}
    raw = row["chain_trigger_json"] if isinstance(row, sqlite3.Row) else row[0]
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return {}
    if not isinstance(data, Mapping):
        return {}
    return _with_template_routes(_contract_root(data))


def _explicit_contract_from_timeline_event(timeline_event: Mapping[str, Any]) -> dict[str, Any]:
    for container in (
        _mapping(timeline_event.get("payload")),
        _mapping(timeline_event.get("artifact_refs")),
    ):
        if not _looks_like_contract_container(container):
            continue
        root = _contract_root(container)
        if root:
            return root
    return {}


def _looks_like_contract_container(container: Mapping[str, Any]) -> bool:
    return any(
        key in container
        for key in (
            "parallel_contract",
            "mf_contract",
            "contract_instance",
            "contract",
            "template_id",
            "event_routes",
            "service_routes",
        )
    )


def _contract_root(contract: Mapping[str, Any] | None) -> dict[str, Any]:
    data = dict(contract) if isinstance(contract, Mapping) else {}
    for key in ("parallel_contract", "mf_contract", "contract_instance", "contract"):
        nested = data.get(key)
        if isinstance(nested, Mapping):
            return dict(nested)
    return data


def _with_template_routes(contract: Mapping[str, Any]) -> dict[str, Any]:
    root = dict(contract)
    if not root:
        return {}
    if root.get("event_routes") or root.get("service_routes"):
        return root
    template_id = _string(root.get("template_id"))
    if not template_id:
        return root
    try:
        template = get_contract_template(template_id)
    except ContractTemplateError:
        return root
    merged = dict(template)
    merged.update(root)
    if "event_routes" in template:
        merged.setdefault("event_routes", template["event_routes"])
    if "service_routes" in template:
        merged.setdefault("service_routes", template["service_routes"])
    return merged


def _record_timeline_route_results(
    conn: sqlite3.Connection,
    source_event: Mapping[str, Any],
    router_event: Mapping[str, Any],
    route_result: Mapping[str, Any],
) -> None:
    source_event_id = int(source_event.get("id") or 0)
    if not source_event_id:
        return
    project_id = _string(source_event.get("project_id"))
    if not project_id:
        return
    from agent.governance import task_timeline

    for route in route_result.get("routes") or []:
        if not isinstance(route, Mapping):
            continue
        idempotency_key = _string(route.get("idempotency_key"))
        if not idempotency_key or _route_evidence_exists(
            conn,
            project_id=project_id,
            parent_event_id=source_event_id,
            correlation_id=idempotency_key,
        ):
            continue
        decision = _string(route.get("decision"))
        event_type = "service.route.completed" if decision == "allow" else "service.route.blocked"
        task_timeline.record_event(
            conn,
            project_id=project_id,
            backlog_id=_string(source_event.get("backlog_id")),
            task_id=_string(source_event.get("task_id")),
            mf_id=_string(source_event.get("mf_id")),
            attempt_num=int(source_event.get("attempt_num") or 0),
            event_type=event_type,
            phase="service_router",
            event_kind="service_route",
            parent_event_id=source_event_id,
            correlation_id=idempotency_key,
            actor="service-router",
            status=_string(route.get("status")),
            decision=decision,
            payload={
                "service_router_suppress": True,
                "source_event_id": _string(router_event.get("source_event_id")),
                "source_event_type": _string(source_event.get("event_type")),
                "route_id": _string(route.get("route_id")),
                "service_id": _string(route.get("service_id")),
                "mode": _string(route.get("mode")),
                "side_effect_class": _string(route.get("side_effect_class") or route.get("side_effect")),
                "decision": decision,
                "status": _string(route.get("status")),
                "result": _mapping(route.get("result")),
                "reason": _string(route.get("reason")),
            },
        )


def _route_evidence_exists(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    parent_event_id: int,
    correlation_id: str,
) -> bool:
    try:
        row = conn.execute(
            """SELECT 1 FROM task_timeline_events
               WHERE project_id = ?
                 AND parent_event_id = ?
                 AND correlation_id = ?
                 AND event_type IN ('service.route.completed', 'service.route.blocked')
               LIMIT 1""",
            (project_id, int(parent_event_id), correlation_id),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _value_for_field(
    event: Mapping[str, Any],
    contract: Mapping[str, Any],
    field: str,
    *,
    route_id: str,
    service_id: str,
) -> Any:
    if field == "route_id":
        return route_id
    if field == "service_id":
        return service_id
    if field in {"event_kind", "kind"}:
        return _event_kind(event)
    if field == "contract_id":
        return _contract_id(contract)
    if "." in field:
        current: Any = {"event": event, "contract": contract, "payload": _mapping(event.get("payload"))}
        for part in field.split("."):
            if isinstance(current, Mapping):
                current = current.get(part)
            else:
                return ""
        return current if current is not None else ""
    if field == "event_id":
        return event.get("event_id") or event.get("source_event_id") or event.get("id") or ""
    if field in event:
        return event.get(field)
    payload = _mapping(event.get("payload"))
    if field in payload:
        return payload.get(field)
    if field in contract:
        return contract.get(field)
    return ""


def _event_kind(event: Mapping[str, Any]) -> str:
    return _string(event.get("event_kind") or event.get("kind"))


def _contract_id(contract: Mapping[str, Any]) -> str:
    return _string(
        contract.get("contract_instance_id")
        or contract.get("contract_id")
        or contract.get("template_id")
    )


def _route_id(route: Mapping[str, Any]) -> str:
    return _string(route.get("route_id") or route.get("id"))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list_of_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""
