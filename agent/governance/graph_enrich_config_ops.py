"""Gate AI-produced graph/enrich config operations before writing project config."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import yaml

from agent.governance.graph_structure_ops import EDGE_ALLOWLIST
from agent.governance.reconcile_semantic_config import PROJECT_OVERRIDE_PATH
from agent.governance.semantic_precheck import (
    gate_precheck,
    parse_precheck,
    request_precheck_error,
)


SCHEMA_VERSION = "graph_enrich_config_ops.v1"
ANALYZER_ROLE = "reconcile_graph_enrich_config_analyzer"
CONFIG_RULE_OPS = {
    "add_rule",
    "downgrade_relation_confidence",
    "downgrade_rule",
    "promote_rule",
    "review_rule",
    "tighten_rule",
    "update_rule",
}
SUPPORTED_OPS = {
    *CONFIG_RULE_OPS,
    "upsert_edge_evidence_policy",
}
CONFIG_EDGE_ALLOWLIST = set(EDGE_ALLOWLIST) | {
    "consumes_event",
    "creates_task",
    "emits_event",
    "http_route",
    "configures_analyzer",
    "configures_model_routing",
    "configures_role",
    "configures_runtime",
    "references_schema",
}
CONFIG_DOWNGRADE_TARGETS = CONFIG_EDGE_ALLOWLIST | {
    "drop",
    "ignore",
    "ignored",
    "weak",
    "weak_tests",
}
CONFIG_EDGE_ALIASES = {
    "import_module": "imports",
    "imports_module": "imports",
    "module_import": "imports",
}
SUPPORTED_SOURCE_EVIDENCE = {
    "event_bus_subscribe",
    "function_calls",
    "import_only",
    "semantic_feedback",
    "string_literal",
    "test_import_fanin",
    "weak_call_resolver_ambiguous_add",
    "weak_call_resolver_ambiguous_short_name",
}
SUPPORTED_ACTIONS = {
    "add",
    "allow",
    "downgrade",
    "drop",
    "ignore",
    "promote",
    "reject",
    "require_direct_symbol_import",
}
POLICY_OP_SOURCE_EVIDENCE = {"import_only"}
POLICY_OP_ACTIONS = {"allow", "downgrade", "reject"}
POLICY_OP_EDGES = {"calls"}
GRAPH_ENRICH_CONFIG_SELF_PRECHECK_RULES = [
    "schema_version",
    "semantic_bridge_normalized",
    "op_supported",
    "required_fields_present",
    "edge_supported_or_canonical_alias",
    "source_evidence_present",
    "action_present",
    "config_patch_previewed",
    "observer_approval_required",
]
GRAPH_ENRICH_CONFIG_NON_RETRYABLE_GATE_ERRORS: set[str] = set()
MAX_AI_REPAIR_ATTEMPTS = 1
_REQUIRED_OPERATION_FIELDS = {
    name: ["op", "rule_id", "edge", "source_evidence", "action"]
    for name in sorted(CONFIG_RULE_OPS)
}
_REQUIRED_OPERATION_FIELDS.update({
    "upsert_edge_evidence_policy": [
        "op",
        "rule_id",
        "edge",
        "source_evidence",
        "action",
    ],
})


def graph_enrich_config_ops_output_contract() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "return_exactly_one_json_object": True,
        "supported_operations": sorted(SUPPORTED_OPS),
        "supported_edges": sorted(CONFIG_EDGE_ALLOWLIST),
        "supported_downgrade_targets": sorted(CONFIG_DOWNGRADE_TARGETS),
        "supported_source_evidence": sorted(SUPPORTED_SOURCE_EVIDENCE),
        "supported_actions": sorted(SUPPORTED_ACTIONS),
        "custom_rule_tokens": {
            "source_evidence": "Rule ops accept non-empty custom evidence tokens; policy ops remain strict.",
            "action": "Rule ops accept non-empty custom actions for observer-reviewed config proposals.",
            "downgrade_to": "Rule ops accept non-empty custom downgrade targets.",
        },
        "operation_constraints": {
            "upsert_edge_evidence_policy": {
                "edges": sorted(POLICY_OP_EDGES),
                "source_evidence": sorted(POLICY_OP_SOURCE_EVIDENCE),
                "actions": sorted(POLICY_OP_ACTIONS),
                "note": (
                    "Use config rule ops such as tighten_rule/update_rule/promote_rule for "
                    "function_calls, test_import_fanin, or require_direct_symbol_import cases."
                ),
            }
        },
        "required_top_level_fields": ["schema_version", "source", "operations", "self_check"],
        "required_operation_fields": {
            name: list(fields)
            for name, fields in sorted(_REQUIRED_OPERATION_FIELDS.items())
        },
        "source": {
            "analyzer_role": ANALYZER_ROLE,
        },
        "self_check_required": True,
        "self_precheck": {
            "required": True,
            "checked_rules_required": list(GRAPH_ENRICH_CONFIG_SELF_PRECHECK_RULES),
            "must_not_mark_valid_when": [],
            "repair_policy": {
                "max_attempts": MAX_AI_REPAIR_ATTEMPTS,
                "retry_only_model_repairable": True,
            },
        },
        "no_markdown": True,
    }


def parse_graph_enrich_config_ai_output(raw_output: Any) -> dict[str, Any]:
    if isinstance(raw_output, Mapping):
        return {"ok": True, "payload": dict(raw_output), "errors": []}
    if not isinstance(raw_output, str) or not raw_output.strip():
        return {"ok": False, "payload": {}, "errors": ["ai_output_missing"]}
    decoder = json.JSONDecoder()
    text = raw_output.strip()
    try:
        payload, end = decoder.raw_decode(text)
    except json.JSONDecodeError:
        return {"ok": False, "payload": {}, "errors": ["ai_output_json_invalid"]}
    if text[end:].strip():
        return {"ok": False, "payload": {}, "errors": ["ai_output_extra_content"]}
    if not isinstance(payload, dict):
        return {"ok": False, "payload": {}, "errors": ["ai_output_not_object"]}
    return {"ok": True, "payload": payload, "errors": []}


def validate_graph_enrich_config_ops(payload: Mapping[str, Any]) -> dict[str, Any]:
    source = payload.get("source") if isinstance(payload.get("source"), Mapping) else {}
    operations_raw = payload.get("operations") if isinstance(payload.get("operations"), list) else []
    self_check = payload.get("self_check") if isinstance(payload.get("self_check"), Mapping) else {}
    global_errors: list[str] = []
    if str(payload.get("schema_version") or "") != SCHEMA_VERSION:
        global_errors.append("schema_version_invalid")
    if str(source.get("analyzer_role") or "") != ANALYZER_ROLE:
        global_errors.append("source_analyzer_role_invalid")
    if self_check.get("valid") is not True:
        global_errors.append("self_check_invalid")
    if not isinstance(self_check.get("checked_rules"), list) or not self_check.get("checked_rules"):
        global_errors.append("self_check_missing_checked_rules")
    if not operations_raw:
        global_errors.append("operations_missing")

    operations: list[dict[str, Any]] = []
    seen_rule_ids: set[str] = set()
    for index, raw in enumerate(operations_raw):
        op = raw if isinstance(raw, Mapping) else {}
        operations.append(_validate_operation(op, index=index, seen_rule_ids=seen_rule_ids))

    accepted = [item for item in operations if item["status"] == "accepted"]
    rejected_count = len(operations) - len(accepted)
    patch = _config_patch_for_operations(accepted)
    ok = not global_errors and rejected_count == 0
    report = {
        "ok": ok,
        "status": "passed" if ok else "failed",
        "schema_version": SCHEMA_VERSION,
        "self_check": {
            "valid": self_check.get("valid") is True,
            "checked_rules_count": len(self_check.get("checked_rules") or [])
            if isinstance(self_check.get("checked_rules"), list)
            else 0,
        },
        "errors": _dedupe(global_errors),
        "accepted_count": len(accepted),
        "rejected_count": rejected_count,
        "operations": operations,
        "config_patch": patch,
    }
    report["precheck"] = gate_precheck(
        report,
        non_retryable_error_codes=GRAPH_ENRICH_CONFIG_NON_RETRYABLE_GATE_ERRORS,
        max_repair_attempts=MAX_AI_REPAIR_ATTEMPTS,
    )
    return report


def dry_run_graph_enrich_config_ops(
    payload: Mapping[str, Any],
    *,
    project_root: str | Path,
) -> dict[str, Any]:
    gate = validate_graph_enrich_config_ops(payload)
    if not gate["ok"]:
        return {
            "ok": False,
            "status": "failed",
            "gate": gate,
            "preview": _empty_preview(project_root),
        }
    return {
        "ok": True,
        "status": "passed",
        "gate": gate,
        "preview": _preview(project_root, gate["config_patch"]),
    }


def run_graph_enrich_config_ai_output_pipeline(
    *,
    raw_output: Any,
    mode: str = "dry_run",
    project_root: str | Path,
) -> dict[str, Any]:
    normalized_mode = str(mode or "dry_run").strip().lower().replace("-", "_")
    parsed = parse_graph_enrich_config_ai_output(raw_output)
    if not parsed["ok"]:
        return {
            "ok": False,
            "status": "failed",
            "mode": normalized_mode,
            "accepted": False,
            "mutated": False,
            "parse": parsed,
            "precheck": parse_precheck(parsed, max_repair_attempts=MAX_AI_REPAIR_ATTEMPTS),
        }
    if normalized_mode not in {"dry_run", "dryrun", "preview", "accept", "apply", "write"}:
        return {
            "ok": False,
            "status": "failed",
            "mode": normalized_mode,
            "accepted": False,
            "mutated": False,
            "parse": parsed,
            "errors": ["mode_unsupported"],
            "precheck": request_precheck_error("mode_unsupported"),
        }
    dry_run = dry_run_graph_enrich_config_ops(parsed["payload"], project_root=project_root)
    if normalized_mode in {"dry_run", "dryrun", "preview"} or not dry_run["ok"]:
        return {
            **dry_run,
            "mode": normalized_mode,
            "accepted": False,
            "mutated": False,
            "parse": parsed,
            "precheck": dry_run.get("gate", {}).get("precheck"),
        }
    write = write_graph_enrich_config(project_root, dry_run["gate"]["config_patch"])
    return {
        "ok": write["ok"],
        "status": "passed" if write["ok"] else "failed",
        "mode": normalized_mode,
        "accepted": write["ok"],
        "mutated": write["written_count"] > 0,
        "requires_commit": write["written_count"] > 0,
        "update_graph_after_commit": write["written_count"] > 0,
        "parse": parsed,
        "gate": dry_run["gate"],
        "preview": dry_run["preview"],
        "precheck": dry_run["gate"].get("precheck"),
        "write": write,
    }


def write_graph_enrich_config(
    project_root: str | Path,
    config_patch: Mapping[str, Any],
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    target = root / PROJECT_OVERRIDE_PATH
    current = _read_yaml_mapping(target)
    merged = _deep_merge(current, dict(config_patch))
    before = yaml.safe_dump(current, sort_keys=True)
    after = yaml.safe_dump(merged, sort_keys=True)
    if before == after:
        return {
            "ok": True,
            "written_count": 0,
            "config_path": str(target),
            "skipped": [{"reason": "already_present", "config_path": str(target)}],
            "errors": [],
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(after, encoding="utf-8")
    return {
        "ok": True,
        "written_count": 1,
        "config_path": str(target),
        "skipped": [],
        "errors": [],
    }


def _validate_operation(
    op: Mapping[str, Any],
    *,
    index: int,
    seen_rule_ids: set[str],
) -> dict[str, Any]:
    errors: list[str] = []
    normalizations: list[str] = []
    op_name = str(op.get("op") or "").strip()
    rule_id = str(op.get("rule_id") or "").strip()
    edge = _normalize_edge(op.get("edge") or op.get("edge_type"))
    source_evidence = _normalize_source_evidence(op.get("source_evidence"))
    action = _normalize_action(op.get("action"))
    downgrade_to = _normalize_edge(op.get("downgrade_to"))
    if action == "downgrade" and downgrade_to in {"ignore", "ignored"}:
        action = "ignore"
        downgrade_to = ""
        normalizations.append("downgrade_ignored_normalized_to_ignore")
    is_rule_op = op_name in CONFIG_RULE_OPS
    is_policy_op = op_name == "upsert_edge_evidence_policy"
    if op_name not in SUPPORTED_OPS:
        errors.append("unsupported_config_op")
    if not rule_id:
        errors.append("rule_id_missing")
    elif rule_id in seen_rule_ids:
        errors.append("rule_id_duplicate")
    seen_rule_ids.add(rule_id)
    if edge not in CONFIG_EDGE_ALLOWLIST:
        errors.append("edge_unsupported")
    if not source_evidence:
        errors.append("source_evidence_missing")
    elif source_evidence not in SUPPORTED_SOURCE_EVIDENCE and not is_rule_op:
        errors.append("source_evidence_unsupported")
    elif source_evidence not in SUPPORTED_SOURCE_EVIDENCE and is_rule_op:
        normalizations.append("custom_source_evidence")
    if not action:
        errors.append("action_missing")
    elif action not in SUPPORTED_ACTIONS and not is_rule_op:
        errors.append("action_unsupported")
    elif action not in SUPPORTED_ACTIONS and is_rule_op:
        normalizations.append("custom_action")
    if action == "downgrade" and downgrade_to not in CONFIG_DOWNGRADE_TARGETS and not is_rule_op:
        errors.append("downgrade_to_unsupported")
    elif action == "downgrade" and downgrade_to not in CONFIG_DOWNGRADE_TARGETS and is_rule_op:
        normalizations.append("custom_downgrade_target")
    if is_policy_op and edge not in POLICY_OP_EDGES:
        errors.append("edge_unsupported_for_policy")
    if is_policy_op and action not in POLICY_OP_ACTIONS:
        errors.append("action_unsupported_for_policy")
    if is_policy_op and source_evidence not in POLICY_OP_SOURCE_EVIDENCE:
        errors.append("source_evidence_unsupported_for_policy")
    confidence = op.get("confidence")
    if confidence is not None:
        try:
            confidence_f = float(confidence)
        except (TypeError, ValueError):
            confidence_f = -1.0
        if confidence_f < 0.0 or confidence_f > 1.0:
            errors.append("confidence_out_of_range")
    return {
        "index": index,
        "op": op_name,
        "rule_id": rule_id,
        "edge": edge,
        "source_evidence": source_evidence,
        "action": action,
        "downgrade_to": downgrade_to,
        "status": "accepted" if not errors else "rejected",
        "errors": errors,
        "normalizations": normalizations,
        "reason": _reason(op.get("evidence")),
    }


def _config_patch_for_operations(operations: list[dict[str, Any]]) -> dict[str, Any]:
    policy: dict[str, Any] = {"dedupe_operations": True}
    rules: dict[str, dict[str, Any]] = {}
    for op in operations:
        if op["op"] == "upsert_edge_evidence_policy":
            edge = op["edge"]
            if edge != "calls" or op["source_evidence"] != "import_only":
                continue
            policy.setdefault("calls", {})
            policy["calls"].update(
                {
                    "require_call_evidence": True,
                    "import_only_action": op["action"],
                    "downgrade_to": op["downgrade_to"] or "imports",
                }
            )
            continue
        rules[op["rule_id"]] = {
            "op": op["op"],
            "edge": op["edge"],
            "source_evidence": op["source_evidence"],
            "action": op["action"],
            "downgrade_to": op["downgrade_to"],
            "reason": op["reason"],
        }
    patch: dict[str, Any] = {"graph_structure_ops": {"evidence_policy": policy}}
    if rules:
        patch["graph_enrich_config_ops"] = {"rules": rules}
    return patch


def _preview(project_root: str | Path, patch: Mapping[str, Any]) -> dict[str, Any]:
    root = Path(project_root).resolve()
    out = dict(patch)
    out["config_path"] = str(root / PROJECT_OVERRIDE_PATH)
    return out


def _empty_preview(project_root: str | Path) -> dict[str, Any]:
    return {
        "config_path": str(Path(project_root).resolve() / PROJECT_OVERRIDE_PATH),
        "graph_structure_ops": {"evidence_policy": {}},
        "graph_enrich_config_ops": {"rules": {}},
    }


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(payload) if isinstance(payload, dict) else {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


def _reason(evidence: Any) -> str:
    if isinstance(evidence, Mapping):
        return str(evidence.get("reason") or evidence.get("summary") or "").strip()
    return str(evidence or "").strip()


def _normalize_edge(value: Any) -> str:
    edge = (
        str(value or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(".", "_")
        .replace(" ", "_")
    )
    return CONFIG_EDGE_ALIASES.get(edge, edge)


def _normalize_source_evidence(value: Any) -> str:
    return _normalize_edge(value)


def _normalize_action(value: Any) -> str:
    action = _normalize_edge(value)
    if action == "ignored":
        return "ignore"
    return action


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return out
