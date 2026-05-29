from __future__ import annotations

import json

from agent.governance.service_router import route_event


def _contract(service_routes=None, event_routes=None, **extra):
    return {
        "contract_instance_id": "contract-1",
        "service_routes": service_routes or [],
        "event_routes": event_routes or [],
        **extra,
    }


def _service_route(
    service_id="test_governance.preview",
    mode="preview",
    side_effect_class="read",
    **extra,
):
    return {
        "route_id": f"service.{service_id}",
        "service_id": service_id,
        "mode": mode,
        "side_effect_class": side_effect_class,
        "idempotency_key_policy": {
            "fields": [
                "event_id",
                "event_kind",
                "stage",
                "task_id",
                "backlog_id",
                "route_id",
                "service_id",
            ]
        },
        **extra,
    }


def test_unmatched_event_returns_no_op():
    result = route_event({"event_kind": "task.started"}, _contract())

    assert result["decision"] == "no_op"
    assert result["status"] == "no_op"
    assert result["routes"] == []


def test_preview_route_allows_and_runs_default_handler():
    contract = _contract(
        service_routes=[_service_route()],
        event_routes=[
            {
                "route_id": "event.task_completed.preview",
                "event_kind": "task.completed",
                "stage": "review_ready",
                "service_route_id": "service.test_governance.preview",
                "enabled": True,
            }
        ],
    )

    result = route_event(
        {
            "event_id": "evt-1",
            "event_kind": "task.completed",
            "stage": "review_ready",
            "task_id": "task-1",
            "backlog_id": "bug-1",
        },
        contract,
    )

    assert result["decision"] == "allow"
    assert result["status"] == "routed"
    assert result["routes"][0]["status"] == "allowed"
    assert result["routes"][0]["side_effect_class"] == "read"
    assert result["routes"][0]["side_effect"] == "read"
    assert result["routes"][0]["result"]["service_id"] == "test_governance.preview"
    assert result["routes"][0]["requirement_ids"] == []
    assert result["routes"][0]["contract_evidence"] == []


def test_ai_validated_event_route_exposes_declared_contract_evidence():
    contract = _contract(
        service_routes=[
            _service_route(
                requirement_ids=["service_route_evidence"],
                contract_evidence=[{"id": "service_contract_evidence"}],
            )
        ],
        event_routes=[
            {
                "route_id": "event.ai_structured_output.validated",
                "event_kind": "ai.structured_output.validated",
                "service_route_id": "service.test_governance.preview",
                "required_evidence_ids": ["ai_output_validated"],
                "enabled": True,
            }
        ],
    )

    result = route_event(
        {
            "event_id": "evt-ai-validated",
            "event_kind": "ai.structured_output.validated",
            "stage": "review_ready",
            "task_id": "task-ai",
            "backlog_id": "BUG-AI",
        },
        contract,
    )

    route = result["routes"][0]
    assert result["decision"] == "allow"
    assert route["status"] == "allowed"
    assert route["requirement_ids"] == [
        "service_route_evidence",
        "service_contract_evidence",
        "ai_output_validated",
    ]
    assert [item["requirement_id"] for item in route["contract_evidence"]] == route[
        "requirement_ids"
    ]
    assert {item["status"] for item in route["contract_evidence"]} == {"passed"}
    assert route["evidence"]["event_kind"] == "ai.structured_output.validated"
    assert route["evidence"]["contract_evidence"] == route["contract_evidence"]


def test_route_stages_array_matches_current_event_stage():
    contract = _contract(
        service_routes=[_service_route()],
        event_routes=[
            {
                "route_id": "event.task_completed.preview",
                "event_kind": "task.completed",
                "stages": ["review_ready", "waiting_merge"],
                "service_route_id": "service.test_governance.preview",
                "enabled": True,
            }
        ],
    )

    result = route_event(
        {
            "event_id": "evt-1",
            "event_kind": "task.completed",
            "stage": "waiting_merge",
            "task_id": "task-1",
            "backlog_id": "bug-1",
        },
        contract,
    )

    assert result["decision"] == "allow"
    assert result["routes"][0]["status"] == "allowed"


def test_unknown_service_blocks():
    contract = _contract(
        event_routes=[
            {
                "route_id": "event.task_completed.unknown",
                "event_kind": "task.completed",
                "service_id": "missing.service",
                "enabled": True,
            }
        ]
    )

    result = route_event({"event_kind": "task.completed"}, contract)

    assert result["decision"] == "block"
    assert result["routes"][0]["status"] == "unknown_service"
    assert "missing.service" in result["routes"][0]["reason"]


def test_apply_route_without_permission_blocks():
    contract = _contract(
        service_routes=[
            _service_route(
                service_id="cleanup.apply",
                mode="apply",
                side_effect_class="write",
                required_permissions=["cleanup.apply"],
            )
        ],
        event_routes=[
            {
                "route_id": "event.cleanup.apply",
                "event_kind": "cleanup.requested",
                "service_route_id": "service.cleanup.apply",
                "enabled": True,
            }
        ],
    )

    result = route_event({"event_kind": "cleanup.requested", "event_id": "evt-2"}, contract)

    assert result["decision"] == "block"
    assert result["routes"][0]["status"] == "permission_blocked"
    assert "cleanup.apply" in result["routes"][0]["reason"]


def test_apply_route_with_explicit_permission_allows():
    contract = _contract(
        service_routes=[
            _service_route(
                service_id="cleanup.apply",
                mode="apply",
                side_effect_class="write",
                required_permissions=["cleanup.apply"],
            )
        ],
        event_routes=[
            {
                "route_id": "event.cleanup.apply",
                "event_kind": "cleanup.requested",
                "service_route_id": "service.cleanup.apply",
                "enabled": True,
            }
        ],
    )

    result = route_event(
        {
            "event_kind": "cleanup.requested",
            "event_id": "evt-2",
            "permissions": ["cleanup.apply"],
        },
        contract,
    )

    assert result["decision"] == "allow"
    assert result["routes"][0]["status"] == "allowed"


def test_idempotency_key_is_stable_for_same_event_and_route():
    contract = _contract(
        service_routes=[_service_route()],
        event_routes=[
            {
                "route_id": "event.task_completed.preview",
                "event_kind": "task.completed",
                "service_route_id": "service.test_governance.preview",
                "enabled": True,
            }
        ],
    )
    event = {
        "event_id": "evt-stable",
        "event_kind": "task.completed",
        "stage": "review_ready",
        "task_id": "task-1",
        "backlog_id": "bug-1",
    }

    first = route_event(event, contract)
    second = route_event(dict(event), contract)

    assert first["routes"][0]["idempotency_key"] == second["routes"][0]["idempotency_key"]


def test_legacy_side_effect_alias_still_routes():
    legacy_route = _service_route()
    legacy_route["side_effect"] = legacy_route.pop("side_effect_class")
    contract = _contract(
        service_routes=[legacy_route],
        event_routes=[
            {
                "route_id": "event.task_completed.preview",
                "event_kind": "task.completed",
                "service_route_id": "service.test_governance.preview",
                "enabled": True,
            }
        ],
    )

    result = route_event({"event_kind": "task.completed", "event_id": "evt-legacy"}, contract)

    assert result["decision"] == "allow"
    assert result["routes"][0]["side_effect_class"] == "read"
    assert result["routes"][0]["side_effect"] == "read"


def test_observer_reminder_echo_route_returns_only_safe_reminder_fields():
    contract = _contract(
        service_routes=[
            _service_route(
                service_id="observer.reminder_echo",
                mode="preview",
                side_effect_class="read",
                route_id="service.observer.reminder_echo",
                requirement_ids=["received_reminder_echo", "payload_boundary_preserved"],
            )
        ],
        event_routes=[
            {
                "route_id": "event.observer_command_notified.reminder_echo",
                "event_kind": "observer.command.notified",
                "service_route_id": "service.observer.reminder_echo",
                "enabled": True,
            }
        ],
    )

    result = route_event(
        {
            "event_id": "evt-reminder",
            "event_kind": "observer.command.notified",
            "project_id": "demo",
            "payload": {
                "hook_reminder": {
                    "kind": "observer_command_pending",
                    "project_id": "demo",
                    "message": "pending observer commands exist; call observer_command_next",
                    "payload_included": False,
                    "next_action": {
                        "tool": "observer_command_next",
                        "description": "claim the next pending observer command",
                        "raw_id": "raw-in-next-action",
                    },
                    "raw_id": "raw-1",
                    "source": "dashboard",
                    "command_type": "analyze_requirements",
                    "command_id": "cmd-1",
                }
            },
        },
        contract,
    )

    route = result["routes"][0]
    received_reminder = route["result"]["received_reminder"]
    echo = route["result"]["received_reminder_echo"]

    assert result["decision"] == "allow"
    assert route["service_id"] == "observer.reminder_echo"
    assert route["mode"] == "preview"
    assert route["side_effect_class"] == "read"
    assert received_reminder == echo
    assert set(echo) == {"kind", "project_id", "message", "payload_included", "next_action"}
    assert echo == {
        "kind": "observer_command_pending",
        "project_id": "demo",
        "message": "pending observer commands exist; call observer_command_next",
        "payload_included": False,
        "next_action": {
            "tool": "observer_command_next",
            "description": "claim the next pending observer command",
        },
    }
    assert "raw_id" not in echo
    assert "source" not in echo
    assert "command_type" not in echo
    assert "command_id" not in echo
    result_json = json.dumps(route["result"], sort_keys=True)
    assert "raw_id" not in result_json
    assert "raw-in-next-action" not in result_json
    assert "source" not in result_json
    assert "command_type" not in result_json
    assert "command_id" not in result_json
    assert route["result"]["payload_boundary"]["business_payload_excluded"] is True
