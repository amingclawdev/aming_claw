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
