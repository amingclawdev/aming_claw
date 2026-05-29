"""Deterministic governance service registry for contract-bound routing."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any


ServiceHandler = Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]


READ_SIDE_EFFECTS = {"none", "read", "gate"}
WRITE_SIDE_EFFECTS = {"write"}
ALLOWED_SERVICE_MODES = {"preview", "gate", "apply"}
ALLOWED_SIDE_EFFECTS = {*READ_SIDE_EFFECTS, *WRITE_SIDE_EFFECTS}


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
    }


def default_service_registry() -> ServiceRegistry:
    """Build a registry with built-in deterministic governance services."""

    return ServiceRegistry(default_service_descriptors())
