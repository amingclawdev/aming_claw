"""UE audit contract constants and deterministic validation helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


TEMPLATE_ID = "ue_audit.v1"
CONTRACT_VERSION = "v1"

REQUIRED_INPUT_FIELDS = (
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
)

AUDIT_DIMENSIONS = (
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
)

GATE_DECISIONS = ("pass", "pass_with_followups", "block")
SEVERITIES = ("info", "minor", "major", "critical")

REQUIRED_FINDING_FIELDS = (
    "severity",
    "screen_flow",
    "user_impact",
    "evidence_refs",
    "recommendation",
    "acceptance_impact",
)

BUNDLED_EXPERT_SOURCE = {
    "source_id": "aming_claw.bundled_ue_expert.v1",
    "source_type": "bundled_governance_template",
    "source_record": "agent.governance.ue_audit_contract:BUNDLED_UE_EXPERT_PROFILE",
}

BUNDLED_UE_EXPERT_PROFILE = {
    **BUNDLED_EXPERT_SOURCE,
    "profile_name": "Bundled UE Audit Expert",
    "purpose": (
        "Review user-facing flows for ordinary users before implementation, "
        "after screenshot smoke, and before close gates."
    ),
    "primary_user_archetype": {
        "id": "ordinary_vibe_coding_builder.v1",
        "target_user_type": "non_technical_vibe_coding_builder",
        "skill_level": "ordinary_user",
        "primary_need": "seeing their requests and request states",
        "description": (
            "A non-technical builder who describes desired product behavior in "
            "plain language and needs clear visibility into captured requests, "
            "AI interpretation, progress state, failures, and completion evidence."
        ),
    },
    "non_goals": (
        "replace_product_owner_judgment",
        "auto_approve_ui_changes_from_ai_only",
        "block_non_user_facing_backend_by_default",
    ),
}


class UEAuditContractError(ValueError):
    """Raised when a UE audit contract payload is invalid."""


def contract_definition() -> dict[str, Any]:
    """Return the deterministic V1 UE audit contract shape."""

    return {
        "template_id": TEMPLATE_ID,
        "version": CONTRACT_VERSION,
        "required_inputs": list(REQUIRED_INPUT_FIELDS),
        "audit_dimensions": list(AUDIT_DIMENSIONS),
        "gate_decisions": list(GATE_DECISIONS),
        "severity_values": list(SEVERITIES),
        "required_finding_fields": list(REQUIRED_FINDING_FIELDS),
        "expert_profile": dict(BUNDLED_UE_EXPERT_PROFILE),
    }


def validate_audit_inputs(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the input context needed to run a UE audit."""

    errors: list[str] = []
    for field in REQUIRED_INPUT_FIELDS:
        if _is_missing(payload.get(field)):
            errors.append(f"missing required input: {field}")

    if payload.get("task_stage") and not isinstance(payload.get("task_stage"), str):
        errors.append("task_stage must be a string")
    _require_non_empty_list(payload, "artifact_refs", errors)
    _require_non_empty_list(payload, "constraints", errors)
    _require_non_empty_list(payload, "non_goals", errors)
    _require_non_empty_list(payload, "success_criteria", errors)
    return _validation_result(errors)


def validate_audit_output(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate machine-readable UE audit output."""

    errors: list[str] = []
    gate_decision = payload.get("gate_decision")
    if gate_decision not in GATE_DECISIONS:
        errors.append("gate_decision must be one of: " + ", ".join(GATE_DECISIONS))

    expert_source = payload.get("expert_source")
    if not isinstance(expert_source, Mapping) or not expert_source.get("source_id"):
        errors.append("expert_source.source_id is required")

    findings = payload.get("findings")
    if not isinstance(findings, list):
        errors.append("findings must be a list")
    else:
        for index, finding in enumerate(findings):
            if not isinstance(finding, Mapping):
                errors.append(f"findings[{index}] must be an object")
                continue
            for field in REQUIRED_FINDING_FIELDS:
                if _is_missing(finding.get(field)):
                    errors.append(f"findings[{index}] missing required field: {field}")
            if finding.get("severity") and finding.get("severity") not in SEVERITIES:
                errors.append(f"findings[{index}].severity must be one of: " + ", ".join(SEVERITIES))
            _require_non_empty_list(finding, "evidence_refs", errors, label=f"findings[{index}].evidence_refs")

    return _validation_result(errors)


def validate_ue_audit_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate inputs and/or output sections for MCP and tests."""

    errors: list[str] = []
    template_id = payload.get("template_id", TEMPLATE_ID)
    if template_id != TEMPLATE_ID:
        errors.append(f"template_id must be {TEMPLATE_ID}")
    if payload.get("inputs") is not None:
        inputs = payload.get("inputs")
        if not isinstance(inputs, Mapping):
            errors.append("inputs must be an object")
        else:
            errors.extend(validate_audit_inputs(inputs)["errors"])
    if payload.get("output") is not None:
        output = payload.get("output")
        if not isinstance(output, Mapping):
            errors.append("output must be an object")
        else:
            errors.extend(validate_audit_output(output)["errors"])
    if payload.get("inputs") is None and payload.get("output") is None:
        errors.append("payload must include inputs or output")
    result = _validation_result(errors)
    result["template_id"] = TEMPLATE_ID
    result["expert_source"] = dict(BUNDLED_EXPERT_SOURCE)
    return result


def require_valid_ue_audit_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and raise a compact error if the payload is invalid."""

    result = validate_ue_audit_payload(payload)
    if not result["ok"]:
        raise UEAuditContractError("; ".join(result["errors"]))
    return result


def _validation_result(errors: list[str]) -> dict[str, Any]:
    return {"ok": not errors, "errors": errors}


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _require_non_empty_list(
    payload: Mapping[str, Any],
    field: str,
    errors: list[str],
    *,
    label: str | None = None,
) -> None:
    value = payload.get(field)
    name = label or field
    if value is None:
        return
    if not isinstance(value, list) or not value or any(_is_missing(item) for item in value):
        errors.append(f"{name} must be a non-empty list")
