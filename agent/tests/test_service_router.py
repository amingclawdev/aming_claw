from __future__ import annotations

import json

from agent.governance.service_router import route_event
from agent.governance.service_registry import ServiceDescriptor, ServiceRegistry


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


def _route_token(
    *,
    action="service_route",
    project_id="",
    backlog_id="",
    task_id="",
):
    scope = {}
    if project_id:
        scope["project_id"] = project_id
    if backlog_id:
        scope["backlog_id"] = backlog_id
    if task_id:
        scope["task_id"] = task_id
    return {
        "route_context_hash": "sha256:test-route-context",
        "prompt_contract_id": "rprompt-test-service-route",
        "prompt_contract_hash": "sha256:test-prompt-contract",
        "caller_role": "mf_sub",
        "allowed_action": action,
        "scope": scope,
        "expires_at": "2999-01-01T00:00:00Z",
        "evidence_refs": ["timeline:test-service-route-token"],
    }


def _route_waiver(
    *,
    action="service_route",
    project_id="",
    backlog_id="",
    task_id="",
):
    scope = {}
    if project_id:
        scope["project_id"] = project_id
    if backlog_id:
        scope["backlog_id"] = backlog_id
    if task_id:
        scope["task_id"] = task_id
    return {
        "accepted": True,
        "waiver_type": "manual_fix",
        "route_context_hash": "sha256:test-route-waiver-context",
        "prompt_contract_id": "rprompt-test-service-route-waiver",
        "prompt_contract_hash": "sha256:test-waiver-prompt-contract",
        "caller_role": "observer",
        "allowed_action": action,
        "scope": scope,
        "reason": "Unit test accepts a bounded route context waiver.",
        "timeline_evidence": {"event_id": "timeline:test-route-waiver"},
    }


def _with_route_token(event):
    out = dict(event)
    out["route_token"] = _route_token(
        project_id=out.get("project_id", ""),
        backlog_id=out.get("backlog_id", ""),
        task_id=out.get("task_id", ""),
    )
    return out


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
        _with_route_token({
            "event_id": "evt-1",
            "event_kind": "task.completed",
            "stage": "review_ready",
            "task_id": "task-1",
            "backlog_id": "bug-1",
        }),
        contract,
    )

    assert result["decision"] == "allow"
    assert result["status"] == "routed"
    assert result["routes"][0]["status"] == "allowed"
    assert result["routes"][0]["side_effect_class"] == "read"
    assert result["routes"][0]["side_effect"] == "read"
    assert result["routes"][0]["result"]["service_id"] == "test_governance.preview"
    assert result["routes"][0]["evidence"]["route_context_hash"] == "sha256:test-route-context"
    assert result["routes"][0]["evidence"]["prompt_contract_id"] == (
        "rprompt-test-service-route"
    )
    assert result["routes"][0]["evidence"]["prompt_contract_hash"] == (
        "sha256:test-prompt-contract"
    )
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
        _with_route_token({
            "event_id": "evt-ai-validated",
            "event_kind": "ai.structured_output.validated",
            "stage": "review_ready",
            "task_id": "task-ai",
            "backlog_id": "BUG-AI",
        }),
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
        _with_route_token({
            "event_id": "evt-1",
            "event_kind": "task.completed",
            "stage": "waiting_merge",
            "task_id": "task-1",
            "backlog_id": "bug-1",
        }),
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


def test_non_route_service_without_route_token_blocks_before_handler():
    calls = []

    def handler(event, route_context):
        calls.append((event, route_context))
        return {"ok": True}

    registry = ServiceRegistry({
        "custom.preview": ServiceDescriptor(
            service_id="custom.preview",
            mode="preview",
            side_effect="read",
            supported_events=("custom.requested",),
            handler=handler,
        )
    })
    contract = _contract(
        service_routes=[_service_route(service_id="custom.preview")],
        event_routes=[
            {
                "route_id": "event.custom.preview",
                "event_kind": "custom.requested",
                "service_route_id": "service.custom.preview",
                "enabled": True,
            }
        ],
    )

    result = route_event(
        {"event_id": "evt-custom", "event_kind": "custom.requested"},
        contract,
        registry=registry,
    )

    route = result["routes"][0]
    assert calls == []
    assert result["decision"] == "block"
    assert route["decision"] == "block"
    assert route["status"] == "route_context_token_required"
    assert route["evidence"]["route_status"] == "route_context_token_required"
    assert route["result"]["route_context_gate"]["action"] == "service_route"


def test_non_route_service_with_accepted_route_waiver_allows():
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
        "event_id": "evt-waiver",
        "event_kind": "task.completed",
        "project_id": "demo",
        "task_id": "task-waiver",
        "backlog_id": "BUG-WAIVER",
        "route_waiver": _route_waiver(
            project_id="demo",
            backlog_id="BUG-WAIVER",
            task_id="task-waiver",
        ),
    }

    result = route_event(event, contract)

    route = result["routes"][0]
    assert result["decision"] == "allow"
    assert route["status"] == "allowed"
    assert route["result"]["route_context_gate"]["decision"] == "route_waiver"
    assert route["evidence"]["route_context_hash"] == "sha256:test-route-waiver-context"


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

    result = route_event(
        _with_route_token({"event_kind": "cleanup.requested", "event_id": "evt-2"}),
        contract,
    )

    assert result["decision"] == "block"
    assert result["routes"][0]["status"] == "permission_blocked"
    assert "cleanup.apply" in result["routes"][0]["reason"]


def test_apply_route_with_permission_but_no_route_token_blocks():
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
            "event_id": "evt-cleanup-tokenless",
            "permissions": ["cleanup.apply"],
        },
        contract,
    )

    route = result["routes"][0]
    assert result["decision"] == "block"
    assert route["status"] == "route_context_token_required"
    assert route["result"]["route_context_gate"]["action"] == "service_route"


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
        _with_route_token({
            "event_kind": "cleanup.requested",
            "event_id": "evt-2",
            "permissions": ["cleanup.apply"],
        }),
        contract,
    )

    assert result["decision"] == "allow"
    assert result["routes"][0]["status"] == "allowed"


def test_gate_route_explicit_handler_block_becomes_route_block():
    def gate_handler(event, route_context):
        return {
            "ok": False,
            "allowed": False,
            "status": "policy_blocked",
            "reason": "gate handler rejected the action",
        }

    registry = ServiceRegistry({
        "custom.gate": ServiceDescriptor(
            service_id="custom.gate",
            mode="gate",
            side_effect="gate",
            supported_events=("precheck.requested",),
            handler=gate_handler,
        )
    })
    contract = _contract(
        service_routes=[
            _service_route(
                service_id="custom.gate",
                mode="gate",
                side_effect_class="gate",
                requirement_ids=["gate_policy_evidence"],
            )
        ],
        event_routes=[
            {
                "route_id": "event.precheck.gate",
                "event_kind": "precheck.requested",
                "service_route_id": "service.custom.gate",
                "enabled": True,
            }
        ],
    )

    result = route_event(
        _with_route_token({"event_id": "evt-gate-block", "event_kind": "precheck.requested"}),
        contract,
        registry=registry,
    )

    route = result["routes"][0]
    assert result["decision"] == "block"
    assert result["status"] == "blocked"
    assert route["decision"] == "block"
    assert route["status"] == "policy_blocked"
    assert route["evidence"]["status"] == "blocked"
    assert route["contract_evidence"][0]["status"] == "blocked"


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
    event = _with_route_token(event)

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

    result = route_event(
        _with_route_token({"event_kind": "task.completed", "event_id": "evt-legacy"}),
        contract,
    )

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
        _with_route_token({
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
        }),
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


def test_route_prompt_alert_bundle_returns_visible_hashable_context_only():
    contract = _contract(
        service_routes=[
            _service_route(
                service_id="route.prompt_alert_bundle",
                mode="preview",
                side_effect_class="read",
                route_id="service.route.prompt_alert_bundle",
                requirement_ids=["route_context_hash", "prompt_contract_hash"],
            )
        ],
        event_routes=[
            {
                "route_id": "event.route_prompt_context.preview",
                "event_kind": "route.prompt_context.requested",
                "service_route_id": "service.route.prompt_alert_bundle",
                "enabled": True,
            }
        ],
    )
    event = {
        "event_id": "evt-route",
        "event_kind": "route.prompt_context.requested",
        "payload": {
            "intent": "implementation",
            "route_id": "route-1",
            "stage": "implementation",
            "caller_role": "implementation_worker",
            "content": {
                "kind": "task_summary",
                "summary": "Implement route-owned mutation policy.",
                "raw_prompt": "do not leak this raw prompt",
            },
            "route_alerts": [
                {
                    "code": "observer_judger_must_not_implement",
                    "severity": "block",
                    "applies_to": ["observer", "judger"],
                    "message": "long text is not part of compact evidence",
                }
            ],
            "prompt_contract": {
                "prompt_contract_id": "rprompt-1",
                "target_files": ["agent/governance/service_router.py"],
                "acceptance_criteria": ["tests pass"],
                "raw_prompt": "hidden worker prompt",
            },
            "visible_injection_manifest": {
                "allowed_injections": [
                    {
                        "kind": "route_doc",
                        "id": "route.bootloader.v1",
                        "path": "routes/docs/route_bootloader.md",
                        "sha256": "sha256:doc",
                        "content": "raw doc text should not be returned",
                    }
                ]
            },
            "hidden_context": "private context should not be returned",
        },
    }

    result = route_event(event, contract)
    repeat = route_event(dict(event), contract)

    route = result["routes"][0]
    bundle = route["result"]["route_prompt_bundle"]
    bundle_json = json.dumps(bundle, sort_keys=True)

    assert result["decision"] == "allow"
    assert route["service_id"] == "route.prompt_alert_bundle"
    assert bundle["intent"] == "implementation"
    assert bundle["route"]["route_id"] == "route-1"
    assert "observer_judger_must_not_implement" not in {
        alert["code"] for alert in bundle["alerts"]
    }
    assert bundle["prompt_contract"]["prompt_contract_id"] == "rprompt-1"
    assert bundle["route_context_hash"].startswith("sha256:")
    assert bundle["prompt_contract_hash"].startswith("sha256:")
    assert repeat["routes"][0]["result"]["route_prompt_bundle"]["route_context_hash"] == (
        bundle["route_context_hash"]
    )
    assert route["evidence"]["route_context_hash"] == bundle["route_context_hash"]
    assert route["evidence"]["prompt_contract_id"] == "rprompt-1"
    assert route["evidence"]["prompt_contract_hash"] == bundle["prompt_contract_hash"]
    assert route["evidence"]["visible_injection_manifest_hash"].startswith("sha256:")
    assert all(
        item["route_context_hash"] == bundle["route_context_hash"]
        and item["prompt_contract_id"] == "rprompt-1"
        and item["prompt_contract_hash"] == bundle["prompt_contract_hash"]
        for item in route["contract_evidence"]
    )
    assert "raw_prompt" not in bundle_json
    assert "hidden worker prompt" not in bundle_json
    assert "private context" not in bundle_json
    assert "raw doc text" not in bundle_json


def test_route_prompt_alert_bundle_selects_lightweight_single_lane_for_small_work():
    contract = _contract(
        service_routes=[
            _service_route(
                service_id="route.prompt_alert_bundle",
                mode="preview",
                side_effect_class="read",
                route_id="service.route.prompt_alert_bundle",
            )
        ],
        event_routes=[
            {
                "route_id": "event.route_prompt_context.preview",
                "event_kind": "route.prompt_context.requested",
                "service_route_id": "service.route.prompt_alert_bundle",
                "enabled": True,
            }
        ],
    )

    result = route_event(
        {
            "event_id": "evt-route-small",
            "event_kind": "route.prompt_context.requested",
            "stage": "implementation_wait",
            "payload": {
                "risk_class": "small_deterministic",
                "route_id": "route-small",
                "stage": "implementation_wait",
                "caller_role": "observer",
                "content": {"summary": "Adjust a deterministic unit test fixture."},
                "prompt_contract": {
                    "prompt_contract_id": "rprompt-small",
                    "target_files": ["agent/tests/test_service_router.py"],
                    "test_files": ["agent/tests/test_service_router.py"],
                    "acceptance_criteria": ["focused test passes"],
                    "evidence_required": ["focused_tests"],
                    "raw_prompt": "do not expose",
                },
                "observer_only_context": "private observer note",
            },
        },
        contract,
    )

    bundle = result["routes"][0]["result"]["route_prompt_bundle"]
    worker_contract = bundle["worker_prompt_contract"]
    bundle_json = json.dumps(bundle, sort_keys=True)

    assert bundle["selected_topology"] == "lightweight_single_lane"
    assert bundle["recommended_topology"] == "single_lane.v1"
    assert bundle["required_lanes"] == [
        {
            "id": "single_bounded_worker",
            "role": "mf_sub",
            "purpose": "perform deterministic implementation and focused verification in one bounded lane",
        }
    ]
    assert bundle["reason_codes"] == ["small_deterministic"]
    assert bundle["route"]["caller_role"] == "observer"
    assert "observer_direct_implementation_risk" not in bundle["reason_codes"]
    assert worker_contract["target_files"] == ["agent/tests/test_service_router.py"]
    assert worker_contract["test_files"] == ["agent/tests/test_service_router.py"]
    assert worker_contract["acceptance_criteria"] == ["focused test passes"]
    assert "private observer note" not in bundle_json
    assert "do not expose" not in bundle_json


def test_low_risk_bundle_still_blocks_observer_direct_implementation():
    prompt_contract = _contract(
        service_routes=[
            _service_route(
                service_id="route.prompt_alert_bundle",
                mode="preview",
                side_effect_class="read",
                route_id="service.route.prompt_alert_bundle",
            )
        ],
        event_routes=[
            {
                "route_id": "event.route_prompt_context.preview",
                "event_kind": "route.prompt_context.requested",
                "service_route_id": "service.route.prompt_alert_bundle",
                "enabled": True,
            }
        ],
    )
    action_contract = _contract(
        service_routes=[
            _service_route(
                service_id="route.action_precheck",
                mode="gate",
                side_effect_class="gate",
                route_id="service.route.action_precheck",
                requirement_ids=["route_action_blocked"],
            )
        ],
        event_routes=[
            {
                "route_id": "event.route_action.pre_mutation",
                "event_kind": "route.action.requested",
                "service_route_id": "service.route.action_precheck",
                "enabled": True,
            }
        ],
    )

    prompt_result = route_event(
        {
            "event_id": "evt-route-small-observer",
            "event_kind": "route.prompt_context.requested",
            "stage": "implementation_wait",
            "payload": {
                "risk_class": "small_deterministic",
                "route_id": "route-small-observer",
                "stage": "implementation_wait",
                "caller_role": "observer",
                "content": {"summary": "Adjust a deterministic unit test fixture."},
                "prompt_contract": {
                    "prompt_contract_id": "rprompt-small-observer",
                    "target_files": ["agent/tests/test_service_router.py"],
                    "test_files": ["agent/tests/test_service_router.py"],
                    "acceptance_criteria": ["focused test passes"],
                    "evidence_required": ["focused_tests"],
                },
            },
        },
        prompt_contract,
    )
    bundle = prompt_result["routes"][0]["result"]["route_prompt_bundle"]

    action_result = route_event(
        {
            "event_id": "evt-route-small-observer-action",
            "event_kind": "route.action.requested",
            "payload": {
                "caller_role": "observer",
                "action": "apply_patch",
                "route_context_hash": bundle["route_context_hash"],
                "prompt_contract_id": bundle["prompt_contract"]["prompt_contract_id"],
                "prompt_contract_hash": bundle["prompt_contract_hash"],
                "route_alerts": bundle["alerts"],
                "version_check": {
                    "status": "passed",
                    "dirty": False,
                    "dirty_files": [],
                },
                "graph_status": {
                    "current_state": {"graph_stale": {"is_stale": False}}
                },
            },
        },
        action_contract,
    )

    route = action_result["routes"][0]
    gate = route["result"]["route_action_gate"]
    alert_codes = {alert["code"] for alert in bundle["alerts"]}

    assert bundle["selected_topology"] == "lightweight_single_lane"
    assert "observer_judger_must_not_implement" in alert_codes
    assert "observer_independent_reviewer_must_not_implement" in alert_codes
    assert action_result["decision"] == "block"
    assert route["status"] == "route_action_policy_blocked"
    assert gate["allowed"] is False
    assert "observer_judger_must_not_implement" in gate["reason"]


def test_route_action_precheck_blocks_observer_action_from_generated_bundle_shape():
    prompt_contract = _contract(
        service_routes=[
            _service_route(
                service_id="route.prompt_alert_bundle",
                mode="preview",
                side_effect_class="read",
                route_id="service.route.prompt_alert_bundle",
            )
        ],
        event_routes=[
            {
                "route_id": "event.route_prompt_context.preview",
                "event_kind": "route.prompt_context.requested",
                "service_route_id": "service.route.prompt_alert_bundle",
                "enabled": True,
            }
        ],
    )
    action_contract = _contract(
        service_routes=[
            _service_route(
                service_id="route.action_precheck",
                mode="gate",
                side_effect_class="gate",
                route_id="service.route.action_precheck",
                requirement_ids=["route_action_blocked"],
            )
        ],
        event_routes=[
            {
                "route_id": "event.route_action.pre_mutation",
                "event_kind": "route.action.requested",
                "service_route_id": "service.route.action_precheck",
                "enabled": True,
            }
        ],
    )

    prompt_result = route_event(
        {
            "event_id": "evt-route-generated-bundle",
            "event_kind": "route.prompt_context.requested",
            "stage": "implementation_wait",
            "payload": {
                "risk_class": "small_deterministic",
                "route_id": "route-generated-bundle",
                "stage": "implementation_wait",
                "caller_role": "observer",
                "content": {"summary": "Adjust a deterministic unit test fixture."},
                "prompt_contract": {
                    "prompt_contract_id": "rprompt-generated-bundle",
                    "target_files": ["agent/tests/test_service_router.py"],
                    "test_files": ["agent/tests/test_service_router.py"],
                    "acceptance_criteria": ["focused test passes"],
                    "evidence_required": ["focused_tests"],
                },
            },
        },
        prompt_contract,
    )
    bundle = prompt_result["routes"][0]["result"]["route_prompt_bundle"]

    action_result = route_event(
        {
            "event_id": "evt-route-generated-bundle-action",
            "event_kind": "route.action.requested",
            "payload": {
                "caller_role": "observer",
                "action": "apply_patch",
                "route_prompt_bundle": bundle,
                "version_check": {
                    "status": "passed",
                    "dirty": False,
                    "dirty_files": [],
                },
                "graph_status": {
                    "current_state": {"graph_stale": {"is_stale": False}}
                },
            },
        },
        action_contract,
    )

    route = action_result["routes"][0]
    gate = route["result"]["route_action_gate"]

    assert action_result["decision"] == "block"
    assert route["status"] == "route_action_policy_blocked"
    assert gate["allowed"] is False
    assert gate["route_context_hash"] == bundle["route_context_hash"]
    assert gate["prompt_contract_id"] == "rprompt-generated-bundle"
    assert gate["prompt_contract_hash"] == bundle["prompt_contract_hash"]
    assert "observer_judger_must_not_implement" in gate["reason"]


def test_route_action_precheck_blocks_observer_action_from_nested_bundle_role():
    prompt_contract = _contract(
        service_routes=[
            _service_route(
                service_id="route.prompt_alert_bundle",
                mode="preview",
                side_effect_class="read",
                route_id="service.route.prompt_alert_bundle",
            )
        ],
        event_routes=[
            {
                "route_id": "event.route_prompt_context.preview",
                "event_kind": "route.prompt_context.requested",
                "service_route_id": "service.route.prompt_alert_bundle",
                "enabled": True,
            }
        ],
    )
    action_contract = _contract(
        service_routes=[
            _service_route(
                service_id="route.action_precheck",
                mode="gate",
                side_effect_class="gate",
                route_id="service.route.action_precheck",
                requirement_ids=["route_action_blocked"],
            )
        ],
        event_routes=[
            {
                "route_id": "event.route_action.pre_mutation",
                "event_kind": "route.action.requested",
                "service_route_id": "service.route.action_precheck",
                "enabled": True,
            }
        ],
    )

    prompt_result = route_event(
        {
            "event_id": "evt-route-nested-role-bundle",
            "event_kind": "route.prompt_context.requested",
            "stage": "implementation_wait",
            "payload": {
                "risk_class": "small_deterministic",
                "route_id": "route-nested-role-bundle",
                "stage": "implementation_wait",
                "caller_role": "observer",
                "content": {"summary": "Adjust a deterministic unit test fixture."},
                "prompt_contract": {
                    "prompt_contract_id": "rprompt-nested-role-bundle",
                    "target_files": ["agent/tests/test_service_router.py"],
                    "test_files": ["agent/tests/test_service_router.py"],
                    "acceptance_criteria": ["focused test passes"],
                    "evidence_required": ["focused_tests"],
                },
            },
        },
        prompt_contract,
    )
    bundle = prompt_result["routes"][0]["result"]["route_prompt_bundle"]

    action_result = route_event(
        {
            "event_id": "evt-route-nested-role-bundle-action",
            "event_kind": "route.action.requested",
            "payload": {
                "action": "apply_patch",
                "route_prompt_bundle": bundle,
                "version_check": {
                    "status": "passed",
                    "dirty": False,
                    "dirty_files": [],
                },
                "graph_status": {
                    "current_state": {"graph_stale": {"is_stale": False}}
                },
            },
        },
        action_contract,
    )

    route = action_result["routes"][0]
    gate = route["result"]["route_action_gate"]

    assert bundle["route"]["caller_role"] == "observer"
    assert action_result["decision"] == "block"
    assert route["status"] == "route_action_policy_blocked"
    assert gate["allowed"] is False
    assert gate["route_context_hash"] == bundle["route_context_hash"]
    assert gate["prompt_contract_id"] == "rprompt-nested-role-bundle"
    assert gate["prompt_contract_hash"] == bundle["prompt_contract_hash"]
    assert "observer_judger_must_not_implement" in gate["reason"]


def test_route_prompt_alert_bundle_selects_parallel_lanes_for_high_risk_route_work():
    contract = _contract(
        service_routes=[
            _service_route(
                service_id="route.prompt_alert_bundle",
                mode="preview",
                side_effect_class="read",
                route_id="service.route.prompt_alert_bundle",
            )
        ],
        event_routes=[
            {
                "route_id": "event.route_prompt_context.preview",
                "event_kind": "route.prompt_context.requested",
                "service_route_id": "service.route.prompt_alert_bundle",
                "enabled": True,
            }
        ],
    )

    result = route_event(
        {
            "event_id": "evt-route-high",
            "event_kind": "route.prompt_context.requested",
            "stage": "dispatch",
            "payload": {
                "priority": "P1",
                "route_id": "route-high",
                "stage": "dispatch",
                "caller_role": "observer",
                "content": {
                    "summary": "Change governance route precheck and permission runtime behavior."
                },
                "target_files": [
                    "agent/governance/service_router.py",
                    "agent/governance/precheck_service.py",
                    "agent/governance/service_registry.py",
                ],
                "test_files": [
                    "agent/tests/test_service_router.py",
                    "agent/tests/test_precheck_service.py",
                ],
                "acceptance_criteria": ["independent verification required"],
                "evidence_required": ["focused_tests", "independent_verification_lane"],
            },
        },
        contract,
    )

    bundle = result["routes"][0]["result"]["route_prompt_bundle"]
    lane_ids = {lane["id"] for lane in bundle["required_lanes"]}
    alert_codes = {alert["code"] for alert in bundle["alerts"]}

    assert bundle["selected_topology"] == "observer_led_parallel_lanes"
    assert bundle["recommended_topology"] == "mf_parallel.v1"
    assert {
        "observer_coordinator",
        "bounded_implementation_worker",
        "independent_verification_lane",
        "observer_merge_close_gate",
    }.issubset(lane_ids)
    assert {"priority_p1", "governance_routing_runtime_surface"}.issubset(
        set(bundle["reason_codes"])
    )
    assert bundle["verification_policy"]["independent_verification_required"] is True
    assert "merge" in bundle["observer_authorities"]
    assert "redeploy_governance" in bundle["observer_authorities"]
    assert "independent_verification_required" in alert_codes


def test_route_prompt_alert_bundle_merges_topology_alerts_with_custom_alerts():
    contract = _contract(
        service_routes=[
            _service_route(
                service_id="route.prompt_alert_bundle",
                mode="preview",
                side_effect_class="read",
                route_id="service.route.prompt_alert_bundle",
            )
        ],
        event_routes=[
            {
                "route_id": "event.route_prompt_context.preview",
                "event_kind": "route.prompt_context.requested",
                "service_route_id": "service.route.prompt_alert_bundle",
                "enabled": True,
            }
        ],
    )

    result = route_event(
        {
            "event_id": "evt-route-custom-alerts",
            "event_kind": "route.prompt_context.requested",
            "stage": "close_gate",
            "payload": {
                "priority": "P1",
                "route_id": "route-custom-alerts",
                "stage": "close_gate",
                "caller_role": "observer",
                "content": {"summary": "Close governance routing runtime work."},
                "route_alerts": [
                    {
                        "code": "independent_verification_required",
                        "severity": "info",
                        "applies_to": ["observer"],
                    },
                    {
                        "code": "observer_only_privileged_authorities",
                        "severity": "info",
                        "applies_to": ["observer"],
                    },
                    {
                        "code": "custom_operator_note",
                        "severity": "info",
                        "applies_to": ["observer"],
                    }
                ],
                "target_files": ["agent/governance/service_registry.py"],
                "acceptance_criteria": ["custom alerts do not suppress topology alerts"],
                "evidence_required": ["independent_verification_lane"],
            },
        },
        contract,
    )

    bundle = result["routes"][0]["result"]["route_prompt_bundle"]
    alerts_by_code = {alert["code"]: alert for alert in bundle["alerts"]}

    assert bundle["selected_topology"] == "observer_led_parallel_lanes"
    assert "custom_operator_note" in alerts_by_code
    assert alerts_by_code["independent_verification_required"]["severity"] == "block"
    assert "merge_without_independent_verification" in alerts_by_code[
        "independent_verification_required"
    ]["blocked_actions"]
    assert "close_without_independent_verification" in alerts_by_code[
        "independent_verification_required"
    ]["blocked_actions"]
    assert alerts_by_code["observer_only_privileged_authorities"]["severity"] == "block"
    assert "worker_merge" in alerts_by_code[
        "observer_only_privileged_authorities"
    ]["blocked_actions"]
    assert "worker_backlog_close" in alerts_by_code[
        "observer_only_privileged_authorities"
    ]["blocked_actions"]


def test_route_action_precheck_route_allows_bounded_worker_action():
    contract = _contract(
        service_routes=[
            _service_route(
                service_id="route.action_precheck",
                mode="gate",
                side_effect_class="gate",
                route_id="service.route.action_precheck",
                requirement_ids=["route_action_allowed"],
            )
        ],
        event_routes=[
            {
                "route_id": "event.route_action.pre_mutation",
                "event_kind": "route.action.requested",
                "service_route_id": "service.route.action_precheck",
                "enabled": True,
            }
        ],
    )

    result = route_event(
        {
            "event_id": "evt-route-action",
            "event_kind": "route.action.requested",
            "payload": {
                "caller_role": "implementation_worker",
                "action": "apply_patch",
                "route_context_hash": "sha256:route-context",
                "prompt_contract_id": "rprompt-1",
                "prompt_contract_hash": "sha256:prompt-contract",
                "visible_injection_manifest_hash": "sha256:visible-manifest",
                "route_alerts": [{"code": "observer_judger_must_not_implement"}],
                "version_check": {
                    "status": "passed",
                    "dirty": False,
                    "dirty_files": [],
                },
                "graph_status": {
                    "current_state": {"graph_stale": {"is_stale": False}}
                },
            },
        },
        contract,
    )

    route = result["routes"][0]
    gate = result["routes"][0]["result"]["route_action_gate"]
    assert result["decision"] == "allow"
    assert gate["allowed"] is True
    assert gate["route_context_hash"] == "sha256:route-context"
    assert gate["prompt_contract_id"] == "rprompt-1"
    assert gate["prompt_contract_hash"] == "sha256:prompt-contract"
    assert route["evidence"]["route_context_hash"] == "sha256:route-context"
    assert route["evidence"]["prompt_contract_id"] == "rprompt-1"
    assert route["evidence"]["prompt_contract_hash"] == "sha256:prompt-contract"
    assert route["contract_evidence"][0]["route_context_hash"] == "sha256:route-context"
    assert route["contract_evidence"][0]["prompt_contract_id"] == "rprompt-1"
    assert route["contract_evidence"][0]["prompt_contract_hash"] == (
        "sha256:prompt-contract"
    )


def test_route_action_precheck_blocks_provider_unavailable_before_write():
    contract = _contract(
        service_routes=[
            _service_route(
                service_id="route.action_precheck",
                mode="gate",
                side_effect_class="gate",
                route_id="service.route.action_precheck",
                requirement_ids=["route_action_blocked"],
            )
        ],
        event_routes=[
            {
                "route_id": "event.route_action.pre_mutation",
                "event_kind": "route.action.requested",
                "service_route_id": "service.route.action_precheck",
                "enabled": True,
            }
        ],
    )

    result = route_event(
        {
            "event_id": "evt-route-action-provider-down",
            "event_kind": "route.action.requested",
            "payload": {
                "caller_role": "implementation_worker",
                "action": "apply_patch",
                "route_context_hash": "sha256:route-context",
                "prompt_contract_id": "rprompt-1",
                "prompt_contract_hash": "sha256:prompt-contract",
                "route_provider_error": "Transport closed",
                "version_check": {
                    "status": "passed",
                    "dirty": False,
                    "dirty_files": [],
                },
                "graph_status": {
                    "current_state": {"graph_stale": {"is_stale": False}}
                },
            },
        },
        contract,
    )

    route = result["routes"][0]
    gate = route["result"]["route_action_gate"]
    assert result["decision"] == "block"
    assert route["status"] == "route_action_policy_blocked"
    assert gate["allowed"] is False
    assert "blocked_route_context_unavailable" in gate["reason"]


def test_route_action_precheck_blocks_observer_action_with_visible_identity():
    contract = _contract(
        service_routes=[
            _service_route(
                service_id="route.action_precheck",
                mode="gate",
                side_effect_class="gate",
                route_id="service.route.action_precheck",
                requirement_ids=["route_action_blocked"],
            )
        ],
        event_routes=[
            {
                "route_id": "event.route_action.pre_mutation",
                "event_kind": "route.action.requested",
                "service_route_id": "service.route.action_precheck",
                "enabled": True,
            }
        ],
    )

    result = route_event(
        {
            "event_id": "evt-route-action-block",
            "event_kind": "route.action.requested",
            "payload": {
                "caller_role": "observer",
                "action": "apply_patch",
                "route_context_hash": "sha256:route-context",
                "prompt_contract_id": "rprompt-1",
                "prompt_contract_hash": "sha256:prompt-contract",
                "route_alerts": [{"code": "observer_judger_must_not_implement"}],
                "version_check": {
                    "status": "passed",
                    "dirty": False,
                    "dirty_files": [],
                },
                "graph_status": {
                    "current_state": {"graph_stale": {"is_stale": False}}
                },
                "raw_prompt": "do not leak this prompt",
                "hidden_context": "do not leak this context",
            },
        },
        contract,
    )

    route = result["routes"][0]
    gate = route["result"]["route_action_gate"]
    route_json = json.dumps(route, sort_keys=True)

    assert result["decision"] == "block"
    assert route["decision"] == "block"
    assert route["status"] == "route_action_policy_blocked"
    assert gate["allowed"] is False
    assert gate["status"] == "route_action_policy_blocked"
    assert gate["route_context_hash"] == "sha256:route-context"
    assert gate["prompt_contract_id"] == "rprompt-1"
    assert gate["prompt_contract_hash"] == "sha256:prompt-contract"
    assert route["evidence"]["route_context_hash"] == "sha256:route-context"
    assert route["evidence"]["prompt_contract_id"] == "rprompt-1"
    assert route["evidence"]["prompt_contract_hash"] == "sha256:prompt-contract"
    assert route["evidence"]["route_status"] == "route_action_policy_blocked"
    assert route["contract_evidence"][0]["route_context_hash"] == "sha256:route-context"
    assert route["contract_evidence"][0]["prompt_contract_id"] == "rprompt-1"
    assert route["contract_evidence"][0]["prompt_contract_hash"] == (
        "sha256:prompt-contract"
    )
    assert "do not leak this prompt" not in route_json
    assert "do not leak this context" not in route_json
