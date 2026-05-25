"""Tests for the contract-driven MF workflow runtime."""

from __future__ import annotations

from pathlib import Path
import sys


_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from agent.governance.mf_workflow_runtime import (
    gate_kind_for_stage,
    load_workflow_contract,
    run_until_pause,
    run_workflow_stage,
    stage_map,
)
from agent.tests.fixtures.mf_workflow_runtime import (
    CONTRACT_ID,
    FENCE_TOKEN,
    SCENARIOS,
    commit_worker_candidate,
    create_runtime_fixture,
    make_precheck_token,
    merge_worker_candidate,
)


def test_scn_mf_wf_001_contract_declares_runtime_stage_graph_and_lanes() -> None:
    contract = load_workflow_contract()
    stages = stage_map(contract)

    assert set(SCENARIOS).issuperset(
        {
            "SCN-MF-WF-001",
            "SCN-MF-WF-002",
            "SCN-MF-WF-003",
            "SCN-MF-WF-004",
            "SCN-MF-WF-005",
            "SCN-MF-WF-006",
            "SCN-MF-WF-007",
            "SCN-MF-WF-008",
            "SCN-MF-WF-009",
        }
    )
    assert list(stages) == [
        "dispatch",
        "implementation_wait",
        "handoff_gate",
        "merge_gate",
        "merge_queue_entry",
        "merge_preview",
        "live_merge",
        "reconcile",
        "close_gate",
    ]
    assert gate_kind_for_stage(contract, "dispatch") == "mf_subagent.dispatch"
    assert gate_kind_for_stage(contract, "handoff_gate") == "mf_subagent.handoff"
    assert gate_kind_for_stage(contract, "merge_gate") == "workflow.merge"
    assert gate_kind_for_stage(contract, "merge_queue_entry") == "workflow.merge_queue_entry"
    assert gate_kind_for_stage(contract, "merge_preview") == "workflow.merge_preview"
    assert gate_kind_for_stage(contract, "live_merge") == "workflow.live_merge"
    assert gate_kind_for_stage(contract, "reconcile") == "workflow.reconcile_policy"
    assert gate_kind_for_stage(contract, "close_gate") == "backlog.close"
    assert set(contract["lane_policy"]) == {"green", "yellow", "red"}
    assert contract["fixture_policy"]["fixture_path"] == "agent/tests/fixtures/mf_workflow_runtime.py"
    assert "precheck_run_id" in contract["precheck_result_contract"]["required_fields"]


def test_contract_declares_stage_inputs_and_outputs() -> None:
    contract = load_workflow_contract()
    stage_io = contract["stage_io"]
    stage_names = set(stage_map(contract))

    assert set(stage_io) == stage_names
    for stage_name, spec in stage_io.items():
        assert spec["inputs"], stage_name
        assert spec["outputs"], stage_name

    assert set(stage_io["dispatch"]["inputs"]).issuperset(
        {
            "worker_worktree",
            "target_worktree",
            "branch_ref",
            "merge_queue_id",
            "base_commit",
            "fence_token",
        }
    )
    assert set(stage_io["merge_gate"]["inputs"]).issuperset(
        {
            "main_worktree",
            "source_worktree",
            "source_commit",
            "merge_queue_id",
            "precheck_token",
        }
    )
    assert set(stage_io["merge_gate"]["outputs"]).issuperset(
        {"observed_source_head", "source_commit", "precheck_run_id", "decision"}
    )
    assert set(stage_io["merge_queue_entry"]["inputs"]).issuperset(
        {"merge_queue_id", "branch_ref", "target_head_commit", "precheck_token"}
    )
    assert set(stage_io["live_merge"]["inputs"]).issuperset(
        {"merge_commit", "target_head_before_merge", "target_head_after_merge"}
    )
    assert set(stage_io["close_gate"]["inputs"]).issuperset(
        {"merge_commit", "precheck_token", "contract_evidence", "timeline_evidence"}
    )


def test_runtime_dispatch_calls_precheck_and_advances_green_lane(tmp_path: Path) -> None:
    contract = {**load_workflow_contract(), "contract_instance_id": CONTRACT_ID}
    fixture = create_runtime_fixture(tmp_path)

    result = run_workflow_stage(
        contract,
        "dispatch",
        fixture.dispatch_subject(contract),
        actor="pytest",
    )

    assert result["decision"] == "allow"
    assert result["lane"] == "green"
    assert result["next_stage"] == "implementation_wait"
    assert result["precheck"]["kind"] == "mf_subagent.dispatch"


def test_runtime_implementation_wait_advances_only_after_review_ready() -> None:
    contract = load_workflow_contract()

    waiting = run_workflow_stage(contract, "implementation_wait", {"worker_status": "running"})
    assert waiting["decision"] == "review_required"
    assert waiting["lane"] == "yellow"
    assert waiting["next_stage"] == "implementation_wait"

    ready = run_workflow_stage(contract, "implementation_wait", {"worker_status": "review_ready"})
    assert ready["decision"] == "allow"
    assert ready["next_stage"] == "handoff_gate"


def test_runtime_routes_warning_to_observer_review_without_policy_duplication(
    tmp_path: Path,
) -> None:
    contract = {**load_workflow_contract(), "contract_instance_id": CONTRACT_ID}
    fixture = create_runtime_fixture(tmp_path)
    subject = fixture.handoff_subject(contract)
    subject["tests_evidence"] = {}

    result = run_workflow_stage(contract, "handoff_gate", subject, actor="pytest")

    assert result["decision"] == "block"
    assert result["lane"] == "red"
    assert result["next_stage"] == "blocked"
    assert "missing_tests_evidence" in result["precheck"]["evidence"]["errors"]


def test_runtime_reconcile_graph_rule_warning_routes_to_observer_review() -> None:
    contract = {**load_workflow_contract(), "contract_instance_id": CONTRACT_ID}
    source_commit = "2" * 40
    token = make_precheck_token(source_commit)

    result = run_workflow_stage(
        contract,
        "reconcile",
        {
            "source_commit": source_commit,
            "fence_token": FENCE_TOKEN,
            "precheck_token": token,
            "changed_files": ["agent/governance/graph_rule_fingerprint.py"],
            "scope_kind": "code_module",
            "e2e_decision": "e2e_not_applicable",
            "graph_rule_changed": True,
        },
        actor="pytest",
    )

    assert result["decision"] == "review_required"
    assert result["lane"] == "yellow"
    assert result["next_stage"] == "observer_review"


def test_runtime_can_advance_green_stages_until_worker_wait(tmp_path: Path) -> None:
    contract = {**load_workflow_contract(), "contract_instance_id": CONTRACT_ID}
    fixture = create_runtime_fixture(tmp_path)

    result = run_until_pause(
        contract,
        "dispatch",
        {"dispatch": fixture.dispatch_subject(contract)},
        actor="pytest",
    )

    assert result["current_stage"] == "implementation_wait"
    assert result["history"][0]["lane"] == "green"


def test_runtime_can_run_merge_to_close_gate_after_candidate_commit(
    tmp_path: Path,
) -> None:
    contract = {**load_workflow_contract(), "contract_instance_id": CONTRACT_ID}
    fixture = create_runtime_fixture(tmp_path)
    source_commit = commit_worker_candidate(fixture)
    token = make_precheck_token(source_commit)

    result = run_workflow_stage(
        contract,
        "merge_gate",
        fixture.merge_subject(contract, source_commit=source_commit, precheck_token=token),
        actor="pytest",
    )

    assert result["decision"] == "allow"
    assert result["next_stage"] == "merge_queue_entry"


def test_runtime_can_run_merge_queue_preview_and_live_merge_after_candidate_commit(
    tmp_path: Path,
) -> None:
    contract = {**load_workflow_contract(), "contract_instance_id": CONTRACT_ID}
    fixture = create_runtime_fixture(tmp_path)
    source_commit = commit_worker_candidate(fixture)
    token = make_precheck_token(source_commit)

    queued = run_workflow_stage(
        contract,
        "merge_queue_entry",
        fixture.merge_queue_subject(contract, source_commit=source_commit, precheck_token=token),
        actor="pytest",
    )
    preview = run_workflow_stage(
        contract,
        "merge_preview",
        fixture.merge_preview_subject(contract, source_commit=source_commit, precheck_token=token),
        actor="pytest",
    )
    merge_commit = merge_worker_candidate(fixture)
    live = run_workflow_stage(
        contract,
        "live_merge",
        fixture.live_merge_subject(
            contract,
            source_commit=source_commit,
            merge_commit=merge_commit,
            precheck_token=token,
        ),
        actor="pytest",
    )

    assert queued["decision"] == "allow"
    assert queued["next_stage"] == "merge_preview"
    assert preview["decision"] == "allow"
    assert preview["next_stage"] == "live_merge"
    assert live["decision"] == "allow"
    assert live["next_stage"] == "reconcile"
