"""Gate AI-produced graph/enrich config operations before writing project config."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import yaml

from agent.governance.graph_structure_ops import EDGE_ALLOWLIST
from agent.governance.reconcile_semantic_config import PROJECT_OVERRIDE_PATH


SCHEMA_VERSION = "graph_enrich_config_ops.v1"
ANALYZER_ROLE = "reconcile_graph_enrich_config_analyzer"
SUPPORTED_OPS = {"upsert_edge_evidence_policy"}
SUPPORTED_SOURCE_EVIDENCE = {"import_only"}
SUPPORTED_ACTIONS = {"allow", "downgrade", "reject"}


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
    return {
        "ok": ok,
        "status": "passed" if ok else "failed",
        "schema_version": SCHEMA_VERSION,
        "errors": _dedupe(global_errors),
        "accepted_count": len(accepted),
        "rejected_count": rejected_count,
        "operations": operations,
        "config_patch": patch,
    }


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
        }
    dry_run = dry_run_graph_enrich_config_ops(parsed["payload"], project_root=project_root)
    if normalized_mode in {"dry_run", "dryrun", "preview"} or not dry_run["ok"]:
        return {
            **dry_run,
            "mode": normalized_mode,
            "accepted": False,
            "mutated": False,
            "parse": parsed,
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
    op_name = str(op.get("op") or "").strip()
    rule_id = str(op.get("rule_id") or "").strip()
    edge = str(op.get("edge") or "").strip()
    source_evidence = str(op.get("source_evidence") or "").strip().lower().replace("-", "_")
    action = str(op.get("action") or "").strip().lower()
    downgrade_to = str(op.get("downgrade_to") or "").strip()
    if op_name not in SUPPORTED_OPS:
        errors.append("unsupported_config_op")
    if not rule_id:
        errors.append("rule_id_missing")
    elif rule_id in seen_rule_ids:
        errors.append("rule_id_duplicate")
    seen_rule_ids.add(rule_id)
    if edge not in EDGE_ALLOWLIST:
        errors.append("edge_unsupported")
    if source_evidence not in SUPPORTED_SOURCE_EVIDENCE:
        errors.append("source_evidence_unsupported")
    if action not in SUPPORTED_ACTIONS:
        errors.append("action_unsupported")
    if action == "downgrade" and downgrade_to not in EDGE_ALLOWLIST:
        errors.append("downgrade_to_unsupported")
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
        "reason": _reason(op.get("evidence")),
    }


def _config_patch_for_operations(operations: list[dict[str, Any]]) -> dict[str, Any]:
    policy: dict[str, Any] = {"dedupe_operations": True}
    for op in operations:
        if op["op"] != "upsert_edge_evidence_policy":
            continue
        edge = op["edge"]
        if edge == "calls" and op["source_evidence"] == "import_only":
            policy.setdefault("calls", {})
            policy["calls"].update(
                {
                    "require_call_evidence": True,
                    "import_only_action": op["action"],
                    "downgrade_to": op["downgrade_to"] or "imports",
                }
            )
    return {"graph_structure_ops": {"evidence_policy": policy}}


def _preview(project_root: str | Path, patch: Mapping[str, Any]) -> dict[str, Any]:
    root = Path(project_root).resolve()
    out = dict(patch)
    out["config_path"] = str(root / PROJECT_OVERRIDE_PATH)
    return out


def _empty_preview(project_root: str | Path) -> dict[str, Any]:
    return {
        "config_path": str(Path(project_root).resolve() / PROJECT_OVERRIDE_PATH),
        "graph_structure_ops": {"evidence_policy": {}},
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


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return out
