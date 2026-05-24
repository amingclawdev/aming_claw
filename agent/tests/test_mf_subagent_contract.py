"""Tests for the MF subagent worker contract."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from agent.governance.mf_subagent_contract import (
    BACKEND_CONTRACT,
    FINISH_GATE_REPLAY_SOURCE,
    FINISH_GATE_SCHEMA_VERSION,
    MF_SUB_FORBIDDEN_ACTIONS,
    MF_SUB_ROLE,
    MfSubagentContractError,
    build_mf_subagent_input,
    normalize_mf_subagent_result,
    validate_mf_subagent_finish_gate,
)
from agent.governance.parallel_branch_runtime import BranchTaskRuntimeContext


def test_mf_parallel_template_requires_subagent_fence_and_graph_trace_contract() -> None:
    template_path = (
        _repo_root
        / "agent"
        / "governance"
        / "contract_templates"
        / "mf_parallel.v1.json"
    )
    template = json.loads(template_path.read_text(encoding="utf-8"))

    worker_contract = template["worker_contract"]
    assert set(worker_contract["required_fields"]).issuperset(
        {
            "task_id",
            "parent_task_id",
            "worker_role",
            "fence_token",
            "graph_queries",
        }
    )

    runtime_identity = worker_contract["runtime_identity"]
    assert runtime_identity["worker_role"] == "mf_sub"
    assert set(runtime_identity["required_fields"]) == {
        "task_id",
        "parent_task_id",
        "worker_role",
        "fence_token",
    }

    graph_queries = worker_contract["graph_queries"]
    assert graph_queries["query_source"] == "mf_subagent"
    assert graph_queries["audited"] is True
    assert set(graph_queries["required_context_fields"]).issuperset(
        {"task_id", "parent_task_id", "worker_role", "fence_token"}
    )
    assert graph_queries["timeline_trace_requirement"] == "graph_trace_ids"

    timeline_contract = template["timeline_contract"]
    assert "payload.graph_trace_ids" in timeline_contract["trace_id_locations"]
    assert "verification.graph_trace_ids" in timeline_contract["trace_id_locations"]


def _context(**overrides: object) -> BranchTaskRuntimeContext:
    fields = {
        "project_id": "aming-claw",
        "task_id": "task-mf-sub-1",
        "batch_id": "batch-parallel-1",
        "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
        "branch_ref": "refs/heads/codex/task-mf-sub-1",
        "status": "running",
        "agent_id": "codex",
        "worker_id": "codex-subagent-1",
        "attempt": 2,
        "lease_id": "lease-1",
        "fence_token": "fence-2",
        "ref_name": "main",
        "worktree_id": "wt-1",
        "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
        "base_commit": "base123",
        "head_commit": "head123",
        "target_head_commit": "target123",
        "snapshot_id": "scope-target123",
        "projection_id": "semantic-target123",
        "merge_queue_id": "mq-1",
        "merge_preview_id": "mp-1",
        "depends_on": ("task-foundation",),
        "checkpoint_id": "ckpt-old",
    }
    fields.update(overrides)
    return BranchTaskRuntimeContext(**fields)


def test_build_input_carries_branch_runtime_identity() -> None:
    payload = build_mf_subagent_input(
        _context(),
        prompt="Implement the isolated change.",
        acceptance_criteria=["tests pass"],
        target_files=["agent/governance/mf_subagent_contract.py"],
        test_commands=["python -m pytest agent/tests/test_mf_subagent_contract.py -q"],
    )

    assert payload["role"] == MF_SUB_ROLE
    assert payload["backend_contract"] == BACKEND_CONTRACT
    assert payload["project_id"] == "aming-claw"
    assert payload["backlog_id"] == "ARCH-MF-SUBAGENT-BACKEND"
    assert payload["branch"]["worktree_path"] == "/tmp/aming-claw-wt/task-mf-sub-1"
    assert payload["runtime_identity"]["fence_token"] == "fence-2"
    assert payload["runtime_identity"]["depends_on"] == ["task-foundation"]
    assert payload["work"]["acceptance_criteria"] == ["tests pass"]
    assert "modify_code" in payload["capabilities"]["can"]
    assert set(MF_SUB_FORBIDDEN_ACTIONS).issubset(payload["capabilities"]["cannot"])
    assert payload["prechecks"]["asset_binding_proposal"]["proposal_schema_version"] == (
        "asset_binding_proposal.v1"
    )
    assert payload["prechecks"]["asset_binding_proposal"]["precheck_schema_version"] == (
        "asset_binding_precheck.v1"
    )
    assert payload["required_output"] == [
        "status",
        "changed_files",
        "test_results",
        "checkpoint_id",
        "fence_token",
    ]


@pytest.mark.parametrize("field", ["backlog_id", "worktree_path", "fence_token"])
def test_build_input_rejects_missing_required_identity(field: str) -> None:
    with pytest.raises(MfSubagentContractError, match=field):
        build_mf_subagent_input(_context(**{field: ""}), prompt="Do work.")


def test_normalize_result_marks_ready_only_after_tests_and_fence_match() -> None:
    normalized = normalize_mf_subagent_result(
        {
            "status": "succeeded",
            "changed_files": ["agent/governance/mf_subagent_contract.py"],
            "test_results": {"status": "passed", "command": "pytest -q"},
            "checkpoint_id": "ckpt-new",
            "fence_token": "fence-2",
            "summary": "Implemented contract.",
        },
        expected_fence_token="fence-2",
    )

    assert normalized["role"] == MF_SUB_ROLE
    assert normalized["merge_queue_ready"] is True
    assert normalized["checkpoint_id"] == "ckpt-new"
    assert normalized["changed_files"] == ["agent/governance/mf_subagent_contract.py"]


def test_normalize_result_rejects_stale_fence() -> None:
    with pytest.raises(MfSubagentContractError, match="stale"):
        normalize_mf_subagent_result(
            {
                "status": "succeeded",
                "changed_files": [],
                "test_results": {"status": "passed"},
                "checkpoint_id": "ckpt-new",
                "fence_token": "old-fence",
            },
            expected_fence_token="fence-2",
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"actions": ["merge"]},
        {"actions": ["push"]},
        {"merge_commit": "abc123"},
        {"graph_activated": True},
    ],
)
def test_normalize_result_rejects_forbidden_actions(payload: dict[str, object]) -> None:
    result = {
        "status": "succeeded",
        "changed_files": ["x.py"],
        "test_results": {"status": "passed"},
        "checkpoint_id": "ckpt-new",
        "fence_token": "fence-2",
    }
    result.update(payload)

    with pytest.raises(MfSubagentContractError, match="forbidden actions"):
        normalize_mf_subagent_result(result, expected_fence_token="fence-2")


def test_normalize_result_blocks_merge_queue_when_tests_fail() -> None:
    normalized = normalize_mf_subagent_result(
        {
            "status": "succeeded",
            "changed_files": ["x.py"],
            "test_results": {"status": "failed"},
            "checkpoint_id": "ckpt-new",
            "fence_token": "fence-2",
            "blockers": ["test failure"],
        },
        expected_fence_token="fence-2",
    )

    assert normalized["merge_queue_ready"] is False
    assert normalized["blockers"] == ["test failure"]


def test_finish_gate_returns_validated_checkpoint_evidence() -> None:
    gate = validate_mf_subagent_finish_gate(
        {
            "project_id": "aming-claw",
            "task_id": "task-mf-sub-1",
            "backlog_id": "ARCH-MF-SUBAGENT-BACKEND",
            "branch_ref": "refs/heads/codex/task-mf-sub-1",
            "worktree_path": "/tmp/aming-claw-wt/task-mf-sub-1",
            "base_commit": "base123",
            "target_head_commit": "target123",
            "head_commit": "head456",
            "status": "succeeded",
            "changed_files": ["agent/governance/mf_subagent_contract.py"],
            "test_results": {"status": "passed", "command": "pytest -q"},
            "checkpoint_id": "ckpt-finish",
            "fence_token": "fence-2",
            "summary": "Ready.",
        },
        context=_context(),
    )

    assert gate["schema_version"] == FINISH_GATE_SCHEMA_VERSION
    assert gate["checkpoint_id"] == "ckpt-finish"
    assert gate["head_commit"] == "head456"
    assert gate["replay_source"] == FINISH_GATE_REPLAY_SOURCE
    assert gate["merge_queue_ready"] is True


def test_finish_gate_rejects_identity_mismatch() -> None:
    with pytest.raises(MfSubagentContractError, match="identity mismatch"):
        validate_mf_subagent_finish_gate(
            {
                "project_id": "other-project",
                "status": "succeeded",
                "changed_files": ["x.py"],
                "test_results": {"status": "passed"},
                "checkpoint_id": "ckpt-finish",
                "fence_token": "fence-2",
            },
            context=_context(),
        )


def test_finish_gate_rejects_not_ready_result() -> None:
    with pytest.raises(MfSubagentContractError, match="not merge-queue ready"):
        validate_mf_subagent_finish_gate(
            {
                "status": "succeeded",
                "changed_files": ["x.py"],
                "test_results": {"status": "failed"},
                "checkpoint_id": "ckpt-finish",
                "fence_token": "fence-2",
                "blockers": ["tests failed"],
            },
            context=_context(),
        )
