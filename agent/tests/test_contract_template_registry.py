from __future__ import annotations

import json

import pytest

from agent.governance.contract_template_registry import (
    MalformedContractTemplateError,
    UnknownContractTemplateError,
    get_contract_template,
    list_contract_templates,
    resolve_contract_template,
)
from agent.mcp.tools import TOOLS, ToolDispatcher


def _tool_names() -> set[str]:
    return {str(tool.get("name") or "") for tool in TOOLS}


def test_template_loading_includes_ue_audit_contract():
    templates = list_contract_templates()
    ids = {template["template_id"] for template in templates}

    assert "ue_audit.v1" in ids
    assert "observer_reminder_echo_demo.v1" in ids


def test_template_filtering_by_task_type_and_stage():
    templates = list_contract_templates(
        task_type="ue_audit",
        stage="pre_frontend_implementation",
    )

    assert [template["template_id"] for template in templates] == ["ue_audit.v1"]


def test_get_template_returns_versioned_source_controlled_template():
    template = get_contract_template("ue_audit.v1")

    assert template["version"] == "v1"
    assert template["source"]["type"] == "source_controlled"
    assert template["expert_profile"]["source_id"] == "aming_claw.bundled_ue_expert.v1"


def test_observer_reminder_echo_template_declares_route_and_worker_echo_requirement():
    template = get_contract_template("observer_reminder_echo_demo.v1")

    assert template["version"] == "v1"
    assert template["service_routes"][0]["service_id"] == "observer.reminder_echo"
    assert template["service_routes"][0]["mode"] == "preview"
    assert template["service_routes"][0]["side_effect_class"] == "read"
    assert template["event_routes"][0]["event_kind"] == "observer.command.notified"
    assert template["event_routes"][0]["service_route_id"] == "service.observer.reminder_echo"
    assert "received_reminder_echo" in template["event_routes"][0]["required_evidence_ids"]
    assert "received_reminder_echo" in template["worker_contract"]["required_fields"]
    assert "received_reminder_echo" in template["worker_contract"]["final_output"]["required_fields"]


def test_unknown_template_id_raises_explicit_error():
    with pytest.raises(UnknownContractTemplateError):
        get_contract_template("missing.v1")


def test_versioned_resolution_accepts_base_id_plus_version():
    template = resolve_contract_template(template_id="ue_audit", version="v1")

    assert template["template_id"] == "ue_audit.v1"


def test_resolution_by_task_type_and_stage():
    template = resolve_contract_template(
        task_type="design_review",
        stage="prd_design_review",
    )

    assert template["template_id"] == "ue_audit.v1"


def test_malformed_template_raises_explicit_error(tmp_path):
    (tmp_path / "bad.v1.json").write_text(json.dumps({"schema_version": "x"}), encoding="utf-8")

    with pytest.raises(MalformedContractTemplateError):
        list_contract_templates(template_dir=tmp_path)


def _write_template(tmp_path, payload):
    path = tmp_path / "routes.v1.json"
    base = {
        "schema_version": "test_contract_template.v1",
        "template_id": "routes.v1",
        "task_types": ["task"],
        "stages": ["review_ready"],
    }
    path.write_text(json.dumps({**base, **payload}), encoding="utf-8")
    return path


def _valid_service_route(**extra):
    return {
        "route_id": "service.preview",
        "service_id": "test_governance.preview",
        "mode": "preview",
        "side_effect_class": "read",
        "idempotency_key_policy": {"fields": ["event_id", "event_kind", "route_id"]},
        **extra,
    }


def test_template_validation_accepts_event_and_service_routes(tmp_path):
    _write_template(
        tmp_path,
        {
            "service_routes": [_valid_service_route()],
            "event_routes": [
                {
                    "route_id": "event.task_completed.preview",
                    "event_kind": "task.completed",
                    "stage": "review_ready",
                    "service_route_id": "service.preview",
                    "enabled": True,
                }
            ],
        },
    )

    templates = list_contract_templates(template_dir=tmp_path)

    assert templates[0]["event_routes"][0]["route_id"] == "event.task_completed.preview"
    assert templates[0]["service_routes"][0]["service_id"] == "test_governance.preview"


def test_template_validation_accepts_service_routes_as_object(tmp_path):
    _write_template(
        tmp_path,
        {
            "service_routes": {
                "service.preview": {
                    "service_id": "test_governance.preview",
                    "mode": "preview",
                    "side_effect_class": "read",
                    "idempotency_key_policy": {"fields": ["event_id"]},
                }
            },
            "event_routes": [
                {
                    "route_id": "event.task_completed.preview",
                    "event_kind": "task.completed",
                    "service_route_id": "service.preview",
                }
            ],
        },
    )

    template = list_contract_templates(template_dir=tmp_path)[0]

    assert template["service_routes"][0]["route_id"] == "service.preview"


def test_template_validation_accepts_legacy_side_effect_alias(tmp_path):
    legacy_route = _valid_service_route()
    legacy_route["side_effect"] = legacy_route.pop("side_effect_class")
    _write_template(
        tmp_path,
        {
            "service_routes": [legacy_route],
            "event_routes": [
                {
                    "route_id": "event.task_completed.preview",
                    "event_kind": "task.completed",
                    "service_route_id": "service.preview",
                }
            ],
        },
    )

    template = list_contract_templates(template_dir=tmp_path)[0]

    assert template["service_routes"][0]["side_effect"] == "read"


def test_template_validation_rejects_malformed_route_shape(tmp_path):
    _write_template(
        tmp_path,
        {
            "service_routes": [_valid_service_route()],
            "event_routes": "not a list",
        },
    )

    with pytest.raises(MalformedContractTemplateError, match="event_routes must be a list or object"):
        list_contract_templates(template_dir=tmp_path)


def test_template_validation_rejects_invalid_event_route_stages(tmp_path):
    _write_template(
        tmp_path,
        {
            "service_routes": [_valid_service_route()],
            "event_routes": [
                {
                    "route_id": "event.task_completed.preview",
                    "event_kind": "task.completed",
                    "stages": [],
                    "service_route_id": "service.preview",
                }
            ],
        },
    )

    with pytest.raises(MalformedContractTemplateError, match="stages must be a non-empty"):
        list_contract_templates(template_dir=tmp_path)


def test_template_validation_rejects_unknown_service(tmp_path):
    _write_template(
        tmp_path,
        {
            "service_routes": [
                _valid_service_route(service_id="missing.service"),
            ],
            "event_routes": [
                {
                    "route_id": "event.task_completed.preview",
                    "event_kind": "task.completed",
                    "service_route_id": "service.preview",
                }
            ],
        },
    )

    with pytest.raises(MalformedContractTemplateError, match="unknown service_id"):
        list_contract_templates(template_dir=tmp_path)


def test_template_validation_rejects_ai_route_fields(tmp_path):
    _write_template(
        tmp_path,
        {
            "service_routes": [
                _valid_service_route(ai_provider="openai"),
            ],
            "event_routes": [
                {
                    "route_id": "event.task_completed.preview",
                    "event_kind": "task.completed",
                    "service_route_id": "service.preview",
                }
            ],
        },
    )

    with pytest.raises(MalformedContractTemplateError, match="forbidden AI field"):
        list_contract_templates(template_dir=tmp_path)


def test_template_validation_rejects_apply_without_permission(tmp_path):
    _write_template(
        tmp_path,
        {
            "service_routes": [
                _valid_service_route(
                    route_id="service.cleanup.apply",
                    service_id="cleanup.apply",
                    mode="apply",
                    side_effect_class="write",
                ),
            ],
            "event_routes": [
                {
                    "route_id": "event.cleanup.apply",
                    "event_kind": "cleanup.requested",
                    "service_route_id": "service.cleanup.apply",
                }
            ],
        },
    )

    with pytest.raises(MalformedContractTemplateError, match="apply/write requires"):
        list_contract_templates(template_dir=tmp_path)


def test_mcp_contract_template_tools_resolve_in_process():
    assert {
        "contract_template_list",
        "contract_template_get",
        "contract_template_resolve",
        "ue_audit_validate",
    }.issubset(_tool_names())

    dispatcher = ToolDispatcher(
        api_fn=lambda method, path, data=None: {"ok": True},
        worker_pool=None,
        service_mgr=None,
        manager_api_fn=lambda method, path, data=None: {"ok": True},
        workspace=".",
    )

    listed = dispatcher.dispatch("contract_template_list", {"task_type": "ue_audit"})
    fetched = dispatcher.dispatch("contract_template_get", {"template_id": "ue_audit.v1"})
    resolved = dispatcher.dispatch(
        "contract_template_resolve",
        {"template_id": "ue_audit", "version": "v1"},
    )
    missing = dispatcher.dispatch("contract_template_get", {"template_id": "missing.v1"})

    assert listed["ok"] is True
    assert [template["template_id"] for template in listed["templates"]] == ["ue_audit.v1"]
    assert fetched["template"]["template_id"] == "ue_audit.v1"
    assert resolved["template"]["template_id"] == "ue_audit.v1"
    assert missing["ok"] is False
