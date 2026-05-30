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
    DISPATCH_DEFAULT,
    DISPATCH_GATE_SCHEMA_VERSION,
    FINISH_GATE_REPLAY_SOURCE,
    FINISH_GATE_SCHEMA_VERSION,
    MF_SUB_FORBIDDEN_ACTIONS,
    MF_SUB_ROLE,
    OBSERVER_COORDINATOR_ROLE,
    OBSERVER_DIRECT_MUTATION_SCHEMA_VERSION,
    ROUTE_ACTION_GATE_SCHEMA_VERSION,
    WORKTREE_POLICY_MODE,
    MfSubagentContractError,
    build_mf_subagent_input,
    normalize_mf_subagent_result,
    validate_observer_direct_mutation_exception,
    validate_mf_subagent_dispatch_gate,
    validate_mf_subagent_finish_gate,
    validate_route_action_gate,
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
            "route_identity",
            "selected_topology",
            "recommended_topology",
            "target_files",
            "test_files",
            "test_commands",
            "review_evidence",
        }
    )

    worker_prompt_contract = worker_contract["worker_prompt_contract"]
    assert "target_files" in worker_prompt_contract["bounded_fields_only"]
    assert "test_files" in worker_prompt_contract["bounded_fields_only"]
    assert "route_identity" in worker_prompt_contract["bounded_fields_only"]
    assert "observer_only_context" in worker_prompt_contract["forbidden_context_sources"]

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

    worktree_policy = worker_contract["worktree_policy"]
    assert worktree_policy["mode"] == "isolated_worktree_required"
    assert worktree_policy["same_worktree_allowed"] is False
    assert worktree_policy["target_main_worktree_dispatch"] == "blocked_by_default"
    assert set(worktree_policy["required_dispatch_fields"]).issuperset(
        {
            "branch",
            "worktree",
            "base_commit",
            "target_head_commit",
            "merge_queue_id",
            "fence_token",
            "owned_files",
            "dirty_scope_check",
        }
    )
    assert set(worktree_policy["override_policy"]["requires"]).issuperset(
        {
            "same_worktree_allowed=true",
            "explicit_operator_reason",
            "dirty_scope_exact_match",
            "observer_timeline_event_before_dispatch",
        }
    )


def test_mf_parallel_template_exposes_observer_no_direct_code_boundary() -> None:
    template_path = (
        _repo_root
        / "agent"
        / "governance"
        / "contract_templates"
        / "mf_parallel.v1.json"
    )
    template = json.loads(template_path.read_text(encoding="utf-8"))

    observer_contract = template["observer_contract"]
    assert observer_contract["mode"] == "observer_only"
    assert observer_contract["observer_direct_code"] is False
    assert observer_contract["role_boundary"]["default"] == (
        "no_direct_implementation_code"
    )
    assert "direct_implementation_code" in observer_contract["default_forbidden_actions"]

    judgment_preflight = observer_contract["judgment_preflight"]
    assert judgment_preflight["when_judgment_brain_available"] is True
    assert judgment_preflight["protocol_registry_preflight"]["tool"] == "protocol_list"
    assert judgment_preflight["topology_precheck"]["tool"] == "judgment_plan_precheck"
    assert judgment_preflight["topology_precheck"]["required_before"] == (
        "implementation_planning"
    )

    exception_policy = observer_contract["direct_mutation_exception_policy"]
    assert exception_policy["schema_version"] == OBSERVER_DIRECT_MUTATION_SCHEMA_VERSION
    assert exception_policy["default"] == "reject"
    assert set(exception_policy["requires"]).issuperset(
        {
            "observer_direct_mutation=true",
            "observer_role=observer",
            "tiny_deterministic_scope",
            "explicit_reason",
            "allowed_files",
            "dirty_scope_exact_match",
            "timeline_evidence_before_mutation",
        }
    )
    assert exception_policy["local_precheck"]["function"] == (
        "agent.governance.mf_subagent_contract."
        "validate_observer_direct_mutation_exception"
    )

    nontrivial = template["worker_contract"]["nontrivial_implementation"]
    assert nontrivial["default_topology"] == "dispatch_to_bounded_worker_lane"
    assert set(nontrivial["required_lane_evidence"]).issuperset(
        {
            "target_files",
            "test_commands",
            "worktree_path",
            "fence_token",
            "dirty_scope_check",
            "review_evidence",
        }
    )


def _dispatch_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "task_id": "task-mf-sub-1",
        "parent_task_id": "task-mf-parent",
        "worker_role": "mf_sub",
        "branch": "mf/subagent-1",
        "worktree": "/repo/.worktrees/mf-subagent-1",
        "base_commit": "base123",
        "target_head_commit": "target123",
        "merge_queue_id": "mq-1",
        "fence_token": "fence-1",
        "route_context_hash": "sha256:route-context",
        "prompt_contract_id": "rprompt-1",
        "prompt_contract_hash": "sha256:prompt-contract",
        "owned_files": ["agent/governance/mf_subagent_contract.py"],
        "dirty_scope_check": {
            "status": "passed",
            "dirty_scope_exact_match": True,
            "changed_files": [],
            "owned_files": ["agent/governance/mf_subagent_contract.py"],
        },
    }
    payload.update(overrides)
    return payload


def test_dispatch_gate_accepts_isolated_worktree_with_compact_evidence() -> None:
    evidence = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(),
        target_worktree_path="/repo",
    )

    assert evidence["schema_version"] == DISPATCH_GATE_SCHEMA_VERSION
    assert evidence["allowed"] is True
    assert evidence["role"] == MF_SUB_ROLE
    assert evidence["dispatch_default"] == DISPATCH_DEFAULT
    assert evidence["worktree_policy"] == WORKTREE_POLICY_MODE
    assert evidence["branch"] == "mf/subagent-1"
    assert evidence["worktree"] == "/repo/.worktrees/mf-subagent-1"
    assert evidence["merge_queue_id"] == "mq-1"
    assert evidence["route_context_hash"] == "sha256:route-context"
    assert evidence["prompt_contract_id"] == "rprompt-1"
    assert evidence["prompt_contract_hash"] == "sha256:prompt-contract"
    assert evidence["isolated_worktree"] is True
    assert evidence["same_worktree_allowed"] is False
    assert evidence["override"]["used"] is False
    assert evidence["dirty_scope_check"]["passed"] is True


@pytest.mark.parametrize(
    ("field", "override"),
    [
        ("branch", {"branch": ""}),
        ("worktree", {"worktree": ""}),
        ("fence_token", {"fence_token": ""}),
        ("base_commit", {"base_commit": ""}),
        ("target_head_commit", {"target_head_commit": ""}),
        ("merge_queue_id", {"merge_queue_id": ""}),
        ("route_context_hash", {"route_context_hash": ""}),
        ("prompt_contract_id", {"prompt_contract_id": ""}),
        ("prompt_contract_hash", {"prompt_contract_hash": ""}),
    ],
)
def test_dispatch_gate_rejects_missing_branch_worktree_fence_or_commits(
    field: str,
    override: dict[str, object],
) -> None:
    with pytest.raises(MfSubagentContractError, match=field):
        validate_mf_subagent_dispatch_gate(
            _dispatch_payload(**override),
            target_worktree_path="/repo",
        )


def test_dispatch_gate_rejects_same_worktree_by_default() -> None:
    with pytest.raises(MfSubagentContractError, match="blocked by default"):
        validate_mf_subagent_dispatch_gate(
            _dispatch_payload(worktree="/repo"),
            target_worktree_path="/repo",
        )


def test_dispatch_gate_requires_complete_same_worktree_override() -> None:
    base_payload = _dispatch_payload(
        worktree="/repo",
        same_worktree_allowed=True,
    )
    with pytest.raises(MfSubagentContractError, match="operator reason"):
        validate_mf_subagent_dispatch_gate(base_payload, target_worktree_path="/repo")

    with pytest.raises(MfSubagentContractError, match="dirty_scope_exact_match"):
        validate_mf_subagent_dispatch_gate(
            {
                **base_payload,
                "operator_reason": "Emergency docs-only repair in exact dirty scope.",
                "dirty_scope_check": {
                    "status": "passed",
                    "dirty_scope_exact_match": False,
                    "changed_files": ["agent/governance/mf_subagent_contract.py"],
                },
            },
            target_worktree_path="/repo",
        )

    with pytest.raises(MfSubagentContractError, match="timeline evidence"):
        validate_mf_subagent_dispatch_gate(
            {
                **base_payload,
                "operator_reason": "Emergency docs-only repair in exact dirty scope.",
                "dirty_scope_check": {
                    "status": "passed",
                    "dirty_scope_exact_match": True,
                    "changed_files": ["agent/governance/mf_subagent_contract.py"],
                },
            },
            target_worktree_path="/repo",
        )

    evidence = validate_mf_subagent_dispatch_gate(
        {
            **base_payload,
            "operator_reason": "Emergency docs-only repair in exact dirty scope.",
            "dispatch_timeline_evidence": {"event_id": 42},
        },
        target_worktree_path="/repo",
    )

    assert evidence["isolated_worktree"] is False
    assert evidence["override"]["used"] is True
    assert evidence["override"]["timeline_evidence_recorded"] is True


def _route_action_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "caller_role": "observer",
        "action": "apply_patch",
        "route_context_hash": "sha256:route-context",
        "prompt_contract_id": "rprompt-1",
        "prompt_contract_hash": "sha256:prompt-contract",
        "route_alerts": [{"code": "observer_judger_must_not_implement"}],
        "version_check": {"status": "passed", "dirty": False, "dirty_files": []},
        "graph_status": {"current_state": {"graph_stale": {"is_stale": False}}},
    }
    payload.update(overrides)
    return payload


def test_route_action_gate_rejects_observer_direct_implementation_action() -> None:
    with pytest.raises(MfSubagentContractError, match="observer_judger_must_not_implement"):
        validate_route_action_gate(_route_action_payload())


def test_route_action_gate_allows_bounded_worker_with_route_prompt_identity() -> None:
    evidence = validate_route_action_gate(
        _route_action_payload(caller_role="implementation_worker")
    )

    assert evidence["schema_version"] == ROUTE_ACTION_GATE_SCHEMA_VERSION
    assert evidence["allowed"] is True
    assert evidence["implementation_action"] is True
    assert evidence["route_context_hash"] == "sha256:route-context"
    assert evidence["prompt_contract_id"] == "rprompt-1"
    assert evidence["prompt_contract_hash"] == "sha256:prompt-contract"
    assert evidence["version_workspace_gate"]["passed"] is True
    assert evidence["graph_current_gate"]["passed"] is True


def test_route_action_gate_rejects_implementation_without_route_identity() -> None:
    with pytest.raises(MfSubagentContractError, match="route_context_hash"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                route_context_hash="",
            )
        )


def test_route_action_gate_rejects_implementation_without_prompt_contract_hash() -> None:
    with pytest.raises(MfSubagentContractError, match="prompt_contract_hash"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                prompt_contract_hash="",
            )
        )


def test_route_action_gate_rejects_dirty_workspace_without_waiver() -> None:
    with pytest.raises(MfSubagentContractError, match="version/workspace"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                version_check={
                    "status": "failed",
                    "dirty": True,
                    "dirty_files": ["agent/governance/mf_subagent_contract.py"],
                },
            )
        )


def test_route_action_gate_rejects_stale_graph_without_waiver() -> None:
    with pytest.raises(MfSubagentContractError, match="current graph"):
        validate_route_action_gate(
            _route_action_payload(
                caller_role="implementation_worker",
                graph_status={
                    "current_state": {
                        "graph_stale": {
                            "is_stale": True,
                            "changed_files": ["agent/governance/service_router.py"],
                        }
                    }
                },
            )
        )


def test_route_action_gate_waiver_can_bypass_dirty_or_stale_preconditions() -> None:
    evidence = validate_route_action_gate(
        _route_action_payload(
            caller_role="implementation_worker",
            version_check={
                "status": "failed",
                "dirty": True,
                "dirty_files": ["agent/governance/mf_subagent_contract.py"],
            },
            graph_status={"current_state": {"graph_stale": {"is_stale": True}}},
            route_action_waiver={
                "accepted": True,
                "route_context_hash": "sha256:route-context",
                "prompt_contract_id": "rprompt-1",
                "prompt_contract_hash": "sha256:prompt-contract",
            },
        )
    )

    assert evidence["allowed"] is True
    assert evidence["accepted_waiver_present"] is True
    assert evidence["precondition_waiver_used"] is True
    assert evidence["version_workspace_gate"]["passed"] is False
    assert evidence["graph_current_gate"]["passed"] is False


def test_route_action_gate_accepts_observer_with_waiver_and_matching_dispatch() -> None:
    dispatch = validate_mf_subagent_dispatch_gate(
        _dispatch_payload(),
        target_worktree_path="/repo",
    )

    assert dispatch["allowed"] is True

    evidence = validate_route_action_gate(
        _route_action_payload(
            route_action_waiver={
                "accepted": True,
                "route_context_hash": "sha256:route-context",
                "prompt_contract_id": "rprompt-1",
                "prompt_contract_hash": "sha256:prompt-contract",
            },
            bounded_dispatch_evidence=dispatch,
        )
    )

    assert evidence["allowed"] is True
    assert evidence["accepted_waiver_present"] is True
    assert evidence["bounded_dispatch_evidence_present"] is True


def test_route_action_gate_rejects_observer_when_dispatch_explicitly_failed() -> None:
    failed_dispatch = {
        "schema_version": DISPATCH_GATE_SCHEMA_VERSION,
        "allowed": False,
        "status": "failed",
        "role": MF_SUB_ROLE,
        "route_context_hash": "sha256:route-context",
        "prompt_contract_id": "rprompt-1",
        "prompt_contract_hash": "sha256:prompt-contract",
    }

    with pytest.raises(MfSubagentContractError, match="matching bounded dispatch"):
        validate_route_action_gate(
            _route_action_payload(
                route_action_waiver={
                    "accepted": True,
                    "route_context_hash": "sha256:route-context",
                    "prompt_contract_id": "rprompt-1",
                    "prompt_contract_hash": "sha256:prompt-contract",
                },
                bounded_dispatch_evidence=failed_dispatch,
            )
        )


def _observer_direct_mutation_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "role": "observer",
        "observer_direct_mutation": True,
        "direct_mutation_exception": {
            "tiny_deterministic": True,
            "reason": "Correct a deterministic one-line contract typo.",
            "allowed_files": ["docs/governance/manual-fix-sop.md"],
            "dirty_scope_check": {
                "status": "passed",
                "dirty_scope_exact_match": True,
                "changed_files": ["docs/governance/manual-fix-sop.md"],
                "owned_files": ["docs/governance/manual-fix-sop.md"],
            },
            "timeline_evidence": {
                "event_id": 1001,
                "event_type": "observer_direct_mutation_exception",
                "recorded_before_mutation": True,
            },
        },
    }
    payload.update(overrides)
    return payload


def test_observer_direct_mutation_exception_accepts_tiny_deterministic_scope() -> None:
    evidence = validate_observer_direct_mutation_exception(
        _observer_direct_mutation_payload(),
        allowed_files=["docs/governance/manual-fix-sop.md"],
    )

    assert evidence["schema_version"] == OBSERVER_DIRECT_MUTATION_SCHEMA_VERSION
    assert evidence["role"] == OBSERVER_COORDINATOR_ROLE
    assert evidence["policy_default"] == "reject"
    assert evidence["observer_direct_mutation"] is True
    assert evidence["allowed"] is True
    assert evidence["exception"]["used"] is True
    assert evidence["exception"]["timeline_evidence_recorded_before_mutation"] is True
    assert evidence["dirty_scope_check"]["dirty_scope_exact_match"] is True


def test_observer_direct_mutation_exception_rejects_default_empty_payload() -> None:
    with pytest.raises(MfSubagentContractError, match="observer_direct_mutation=true"):
        validate_observer_direct_mutation_exception({})


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"observer_direct_mutation": False}, "observer_direct_mutation=true"),
        ({"role": ""}, "observer role"),
        (
            {"direct_mutation_exception": {"tiny_deterministic": False}},
            "tiny deterministic",
        ),
        (
            {
                "direct_mutation_exception": {
                    "tiny_deterministic": True,
                    "allowed_files": ["docs/governance/manual-fix-sop.md"],
                }
            },
            "explicit reason",
        ),
        (
            {
                "direct_mutation_exception": {
                    "tiny_deterministic": True,
                    "reason": "Small typo.",
                }
            },
            "allowed_files",
        ),
        (
            {
                "direct_mutation_exception": {
                    "tiny_deterministic": True,
                    "reason": "Small typo.",
                    "allowed_files": ["docs/governance/manual-fix-sop.md"],
                    "timeline_evidence": {
                        "event_id": 1001,
                        "recorded_before_mutation": True,
                    },
                }
            },
            "dirty-scope evidence",
        ),
        (
            {
                "direct_mutation_exception": {
                    "tiny_deterministic": True,
                    "reason": "Small typo.",
                    "allowed_files": ["docs/governance/manual-fix-sop.md"],
                    "dirty_scope_check": {
                        "status": "passed",
                        "dirty_scope_exact_match": False,
                        "changed_files": ["docs/governance/manual-fix-sop.md"],
                    },
                    "timeline_evidence": {
                        "event_id": 1001,
                        "recorded_before_mutation": True,
                    },
                }
            },
            "dirty_scope_exact_match",
        ),
        (
            {
                "direct_mutation_exception": {
                    "tiny_deterministic": True,
                    "reason": "Small typo.",
                    "allowed_files": ["docs/governance/manual-fix-sop.md"],
                    "dirty_scope_check": {
                        "status": "passed",
                        "dirty_scope_exact_match": True,
                        "changed_files": ["docs/governance/manual-fix-sop.md"],
                    },
                }
            },
            "timeline evidence before mutation",
        ),
    ],
)
def test_observer_direct_mutation_exception_rejects_missing_evidence(
    override: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(MfSubagentContractError, match=match):
        validate_observer_direct_mutation_exception(
            _observer_direct_mutation_payload(**override),
            allowed_files=["docs/governance/manual-fix-sop.md"],
        )


def test_observer_direct_mutation_exception_rejects_dirty_files_outside_scope() -> None:
    with pytest.raises(MfSubagentContractError, match="dirty files"):
        validate_observer_direct_mutation_exception(
            _observer_direct_mutation_payload(
                direct_mutation_exception={
                    "tiny_deterministic": True,
                    "reason": "Small typo.",
                    "allowed_files": ["docs/governance/manual-fix-sop.md"],
                    "dirty_scope_check": {
                        "status": "passed",
                        "dirty_scope_exact_match": True,
                        "changed_files": ["agent/governance/server.py"],
                    },
                    "timeline_evidence": {
                        "event_id": 1001,
                        "recorded_before_mutation": True,
                    },
                }
            ),
            allowed_files=["docs/governance/manual-fix-sop.md"],
        )


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
        route_context_hash="sha256:route-context",
        prompt_contract_id="rprompt-1",
        prompt_contract_hash="sha256:prompt-contract",
    )

    assert payload["role"] == MF_SUB_ROLE
    assert payload["backend_contract"] == BACKEND_CONTRACT
    assert payload["project_id"] == "aming-claw"
    assert payload["backlog_id"] == "ARCH-MF-SUBAGENT-BACKEND"
    assert payload["branch"]["worktree_path"] == "/tmp/aming-claw-wt/task-mf-sub-1"
    assert payload["runtime_identity"]["fence_token"] == "fence-2"
    assert payload["runtime_identity"]["depends_on"] == ["task-foundation"]
    assert payload["work"]["acceptance_criteria"] == ["tests pass"]
    assert payload["route_prompt_contract"] == {
        "route_context_hash": "sha256:route-context",
        "prompt_contract_id": "rprompt-1",
        "prompt_contract_hash": "sha256:prompt-contract",
    }
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


@pytest.mark.parametrize("field", ["backlog_id", "worktree_path", "fence_token", "merge_queue_id"])
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
            "merge_queue_id": "mq-1",
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
    assert gate["merge_queue_id"] == "mq-1"
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
