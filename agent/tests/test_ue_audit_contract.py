from __future__ import annotations

from agent.governance.ue_audit_contract import (
    AUDIT_DIMENSIONS,
    BUNDLED_EXPERT_SOURCE,
    GATE_DECISIONS,
    REQUIRED_INPUT_FIELDS,
    TEMPLATE_ID,
    validate_audit_inputs,
    validate_audit_output,
    validate_ue_audit_payload,
)
from agent.mcp.tools import ToolDispatcher


def _valid_inputs() -> dict:
    return {
        "target_user_type": "non_technical_vibe_coding_builder",
        "skill_level": "ordinary_user",
        "jtbd": "Track what I asked the agent to build.",
        "product_surface": "Project Inbox",
        "flow_scenario": "Capture a requirement and see its status.",
        "task_stage": "pre_frontend_implementation",
        "artifact_refs": ["docs/dev/simple-requirement-workspace-design.md"],
        "constraints": ["Keep graph details out of the primary path."],
        "non_goals": ["Do not replace product owner judgment."],
        "success_criteria": ["The user can see request state without jargon."],
    }


def _valid_output() -> dict:
    return {
        "expert_source": dict(BUNDLED_EXPERT_SOURCE),
        "gate_decision": "pass_with_followups",
        "findings": [
            {
                "severity": "minor",
                "screen_flow": "Project Inbox capture flow",
                "user_impact": "The user might miss that analysis is only queued.",
                "evidence_refs": ["screenshot:project-inbox-empty"],
                "recommendation": "Use a clear queued state until an observer claims the command.",
                "acceptance_impact": "Follow-up copy polish, not a close blocker.",
            }
        ],
    }


def test_ue_audit_contract_declares_required_inputs_and_dimensions():
    assert TEMPLATE_ID == "ue_audit.v1"
    assert set(REQUIRED_INPUT_FIELDS) == {
        "target_user_type",
        "skill_level",
        "jtbd",
        "product_surface",
        "flow_scenario",
        "task_stage",
        "artifact_refs",
        "constraints",
        "non_goals",
        "success_criteria",
    }
    assert set(AUDIT_DIMENSIONS) == {
        "hierarchy",
        "object_visibility",
        "status_visibility",
        "next_action_clarity",
        "terminology",
        "error_empty_loading",
        "feedback_progress",
        "accessibility_basics",
        "mobile_desktop",
        "developer_jargon_leakage",
    }


def test_validate_audit_inputs_accepts_complete_payload():
    result = validate_audit_inputs(_valid_inputs())

    assert result == {"ok": True, "errors": []}


def test_validate_audit_inputs_reports_missing_required_context():
    payload = _valid_inputs()
    payload.pop("jtbd")
    payload["artifact_refs"] = []

    result = validate_audit_inputs(payload)

    assert result["ok"] is False
    assert "missing required input: jtbd" in result["errors"]
    assert "missing required input: artifact_refs" in result["errors"]


def test_validate_audit_output_rejects_invalid_gate_decision():
    payload = _valid_output()
    payload["gate_decision"] = "auto_approve"

    result = validate_audit_output(payload)

    assert result["ok"] is False
    assert "gate_decision must be one of: " + ", ".join(GATE_DECISIONS) in result["errors"]


def test_validate_audit_output_requires_machine_readable_findings():
    payload = _valid_output()
    payload["findings"][0].pop("user_impact")

    result = validate_audit_output(payload)

    assert result["ok"] is False
    assert "findings[0] missing required field: user_impact" in result["errors"]


def test_validate_ue_audit_payload_accepts_inputs_and_output():
    result = validate_ue_audit_payload(
        {
            "template_id": "ue_audit.v1",
            "inputs": _valid_inputs(),
            "output": _valid_output(),
        }
    )

    assert result["ok"] is True
    assert result["template_id"] == "ue_audit.v1"
    assert result["expert_source"]["source_id"] == "aming_claw.bundled_ue_expert.v1"


def test_mcp_ue_audit_validate_uses_in_process_contract():
    dispatcher = ToolDispatcher(
        api_fn=lambda method, path, data=None: {"ok": True},
        worker_pool=None,
        service_mgr=None,
        manager_api_fn=lambda method, path, data=None: {"ok": True},
        workspace=".",
    )

    result = dispatcher.dispatch(
        "ue_audit_validate",
        {"payload": {"template_id": "ue_audit.v1", "inputs": _valid_inputs()}},
    )

    assert result["ok"] is True
