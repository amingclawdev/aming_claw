"""Contract-driven MF workflow runtime.

The runtime owns stage movement only. Gate policy stays in precheck_service.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any

from agent.governance.precheck_service import ALLOW, BLOCK, REVIEW_REQUIRED, run_precheck


CONTRACT_TEMPLATE_ID = "mf_workflow_runtime.v1"
DEFAULT_CONTRACT_PATH = (
    Path(__file__).resolve().parent
    / "contract_templates"
    / "mf_workflow_runtime.v1.json"
)
TERMINAL_STAGES = {"observer_review", "blocked", "done"}


class MfWorkflowRuntimeError(ValueError):
    """Raised when a workflow contract cannot drive the requested stage."""


def load_workflow_contract(path: str | Path = DEFAULT_CONTRACT_PATH) -> dict[str, Any]:
    """Load the source-controlled MF workflow runtime contract template."""

    return json.loads(Path(path).read_text(encoding="utf-8"))


def stage_rows(contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = contract.get("stage_graph")
    if not isinstance(rows, list):
        raise MfWorkflowRuntimeError("workflow contract missing stage_graph")
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise MfWorkflowRuntimeError("workflow stage row must be an object")
        out.append(dict(row))
    return out


def stage_map(contract: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row.get("stage") or ""): row for row in stage_rows(contract)}


def gate_kind_for_stage(contract: Mapping[str, Any], stage: str) -> str:
    row = stage_map(contract).get(stage)
    if not row:
        raise MfWorkflowRuntimeError(f"unknown workflow stage: {stage}")
    return str(row.get("gate_kind") or "")


def transition_for_decision(stage_row: Mapping[str, Any], decision: str) -> str:
    if decision == ALLOW:
        return str(stage_row.get("on_allow") or "")
    if decision == REVIEW_REQUIRED:
        return str(stage_row.get("on_review_required") or "observer_review")
    if decision == BLOCK:
        return str(stage_row.get("on_block") or "blocked")
    raise MfWorkflowRuntimeError(f"unknown precheck decision: {decision}")


def lane_for_decision(decision: str) -> str:
    if decision == ALLOW:
        return "green"
    if decision == REVIEW_REQUIRED:
        return "yellow"
    return "red"


def run_workflow_stage(
    contract: Mapping[str, Any],
    stage: str,
    subject: Mapping[str, Any],
    *,
    actor: str = "workflow_worker",
) -> dict[str, Any]:
    """Evaluate one stage and return the next stage plus precheck evidence."""

    rows = stage_map(contract)
    if stage in TERMINAL_STAGES:
        return {
            "stage": stage,
            "next_stage": stage,
            "decision": ALLOW if stage == "done" else BLOCK,
            "lane": "green" if stage == "done" else "red",
            "status": "terminal",
            "precheck": None,
        }
    row = rows.get(stage)
    if row is None:
        raise MfWorkflowRuntimeError(f"unknown workflow stage: {stage}")

    if stage == "implementation_wait":
        worker_status = str(subject.get("worker_status") or subject.get("status") or "")
        ready = worker_status in {"review_ready", "waiting_merge", "succeeded", "completed"}
        return {
            "stage": stage,
            "next_stage": str(row.get("on_worker_review_ready") or "handoff_gate")
            if ready
            else "implementation_wait",
            "decision": ALLOW if ready else REVIEW_REQUIRED,
            "lane": "green" if ready else "yellow",
            "status": "waiting" if not ready else "advanced",
            "precheck": None,
        }

    gate_kind = str(row.get("gate_kind") or "")
    if not gate_kind:
        raise MfWorkflowRuntimeError(f"stage {stage} has no gate_kind")
    contract_id = str(
        contract.get("contract_instance_id")
        or subject.get("contract_id")
        or subject.get("backlog_id")
        or ""
    )
    precheck_subject = {**dict(subject), "contract": dict(contract)}
    precheck = run_precheck(gate_kind, contract_id, stage, precheck_subject, actor)
    decision = str(precheck["decision"])
    return {
        "stage": stage,
        "gate_kind": gate_kind,
        "next_stage": transition_for_decision(row, decision),
        "decision": decision,
        "lane": lane_for_decision(decision),
        "status": precheck["status"],
        "precheck": precheck,
    }


def run_until_pause(
    contract: Mapping[str, Any],
    start_stage: str,
    subjects_by_stage: Mapping[str, Mapping[str, Any]],
    *,
    actor: str = "workflow_worker",
    max_steps: int = 16,
) -> dict[str, Any]:
    """Advance green stages until waiting, review, block, done, or missing input."""

    stage = start_stage
    history: list[dict[str, Any]] = []
    for _ in range(max_steps):
        if stage in TERMINAL_STAGES:
            break
        subject = subjects_by_stage.get(stage)
        if subject is None:
            break
        result = run_workflow_stage(contract, stage, subject, actor=actor)
        history.append(result)
        stage = str(result["next_stage"])
        if result["decision"] != ALLOW or stage in {"implementation_wait", *TERMINAL_STAGES}:
            break
    return {
        "start_stage": start_stage,
        "current_stage": stage,
        "history": history,
        "status": "paused" if stage not in TERMINAL_STAGES else stage,
    }
