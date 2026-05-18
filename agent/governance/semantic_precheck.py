"""Shared precheck classification for semantic graph proposal gates."""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def parse_precheck(
    parsed: Mapping[str, Any],
    *,
    max_repair_attempts: int = 1,
) -> dict[str, Any]:
    errors = _dedupe([str(item or "") for item in parsed.get("errors") or []])
    ok = bool(parsed.get("ok"))
    if ok:
        return _precheck_result(
            status="passed",
            classification="passed",
            errors=[],
            repairable_errors=[],
            non_retryable_errors=[],
            max_repair_attempts=max_repair_attempts,
        )
    return _precheck_result(
        status="failed",
        classification="model_repairable",
        errors=errors,
        repairable_errors=errors,
        non_retryable_errors=[],
        max_repair_attempts=max_repair_attempts,
    )


def gate_precheck(
    gate: Mapping[str, Any],
    *,
    non_retryable_error_codes: Iterable[str] = (),
    max_repair_attempts: int = 1,
) -> dict[str, Any]:
    errors = collect_gate_errors(gate)
    if bool(gate.get("ok")) and not errors:
        return _precheck_result(
            status="passed",
            classification="passed",
            errors=[],
            repairable_errors=[],
            non_retryable_errors=[],
            max_repair_attempts=max_repair_attempts,
            self_check=_self_check_summary(gate),
        )

    blocked = set(non_retryable_error_codes)
    non_retryable = [error for error in errors if error in blocked]
    repairable = [error for error in errors if error not in blocked]
    if non_retryable and repairable:
        classification = "mixed"
    elif non_retryable:
        classification = "policy_rejected"
    else:
        classification = "model_repairable"
    return _precheck_result(
        status="failed",
        classification=classification,
        errors=errors,
        repairable_errors=repairable,
        non_retryable_errors=non_retryable,
        max_repair_attempts=max_repair_attempts,
        self_check=_self_check_summary(gate),
    )


def request_precheck_error(error: str, *, max_repair_attempts: int = 0) -> dict[str, Any]:
    error_s = str(error or "").strip()
    return _precheck_result(
        status="failed",
        classification="request_invalid",
        errors=[error_s] if error_s else [],
        repairable_errors=[],
        non_retryable_errors=[error_s] if error_s else [],
        max_repair_attempts=max_repair_attempts,
    )


def collect_gate_errors(gate: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    errors.extend(str(item or "") for item in gate.get("errors") or [])
    operations = gate.get("operations") if isinstance(gate.get("operations"), list) else []
    for operation in operations:
        if not isinstance(operation, Mapping):
            continue
        errors.extend(str(item or "") for item in operation.get("errors") or [])
    return _dedupe(errors)


def _precheck_result(
    *,
    status: str,
    classification: str,
    errors: list[str],
    repairable_errors: list[str],
    non_retryable_errors: list[str],
    max_repair_attempts: int,
    self_check: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    retryable = classification == "model_repairable" and bool(repairable_errors)
    if status == "passed":
        recommended_action = "none"
    elif retryable:
        recommended_action = "retry_ai_repair_once"
    elif classification in {"policy_rejected", "mixed"}:
        recommended_action = "observer_review_required"
    else:
        recommended_action = "fix_request_or_runtime"
    out: dict[str, Any] = {
        "status": status,
        "classification": classification,
        "retryable": retryable,
        "max_repair_attempts": max(0, int(max_repair_attempts or 0)),
        "recommended_action": recommended_action,
        "errors": errors,
        "repairable_errors": repairable_errors,
        "non_retryable_errors": non_retryable_errors,
    }
    if self_check is not None:
        out["self_check"] = dict(self_check)
    return out


def _self_check_summary(gate: Mapping[str, Any]) -> dict[str, Any]:
    raw = gate.get("self_check") if isinstance(gate.get("self_check"), Mapping) else {}
    checked_rules_count = int(raw.get("checked_rules_count") or 0)
    valid = raw.get("valid")
    return {
        "required": True,
        "valid": valid,
        "checked_rules_count": checked_rules_count,
    }


def _dedupe(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if item and item not in seen:
            out.append(item)
            seen.add(item)
    return out
