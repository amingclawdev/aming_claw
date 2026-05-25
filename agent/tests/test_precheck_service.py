"""Fixture-backed tests for the unified MF precheck service."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys


_repo_root = Path(__file__).resolve().parents[2]
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from agent.governance.mf_workflow_runtime import load_workflow_contract
from agent.governance.precheck_service import run_precheck
from agent.tests.fixtures.mf_workflow_runtime import (
    CONTRACT_ID,
    FENCE_TOKEN,
    advance_target_head,
    commit_worker_candidate,
    create_runtime_fixture,
    make_forbidden_change,
    make_handoff_dirty_scope,
    make_many_ignored_files,
    make_precheck_token,
    make_target_dirty_owned_file,
    merge_worker_candidate,
)


def test_scn_mf_wf_002_dispatch_collects_git_evidence_and_blocks_bad_state(
    tmp_path: Path,
) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)

    result = run_precheck(
        "mf_subagent.dispatch",
        CONTRACT_ID,
        "dispatch",
        fixture.dispatch_subject(contract),
        "pytest",
    )

    assert _result_contract_fields_present(result)
    assert result["decision"] == "allow"
    assert result["status"] == "passed"
    assert result["evidence"]["worker_git"]["head"] == fixture.base_commit
    assert result["evidence"]["target_git"]["head"] == fixture.target_head_commit
    assert result["evidence"]["worker_git"]["root"] != result["evidence"]["target_git"]["root"]
    assert result["evidence"]["merge_queue_id"] == fixture.merge_queue_id

    dirty_main = fixture.dispatch_subject(contract)
    (fixture.main_worktree / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    dirty_result = run_precheck(
        "mf_subagent.dispatch",
        CONTRACT_ID,
        "dispatch",
        dirty_main,
        "pytest",
    )
    assert dirty_result["decision"] == "block"
    assert "dirty_target_main_worktree" in dirty_result["evidence"]["errors"]

    same_worktree = fixture.dispatch_subject(contract)
    same_worktree["worker_worktree"] = str(fixture.main_worktree)
    same_result = run_precheck(
        "mf_subagent.dispatch",
        CONTRACT_ID,
        "dispatch",
        same_worktree,
        "pytest",
    )
    assert same_result["decision"] == "block"
    assert "same_worktree_non_isolated_worker" in same_result["evidence"]["errors"]

    mismatch = fixture.dispatch_subject(contract)
    mismatch["base_commit"] = "0" * 40
    mismatch_result = run_precheck(
        "mf_subagent.dispatch",
        CONTRACT_ID,
        "dispatch",
        mismatch,
        "pytest",
    )
    assert "worker_head_mismatch" in mismatch_result["evidence"]["errors"]


def test_dispatch_blocks_missing_merge_queue_target_move_graph_stale_and_bad_adoption(
    tmp_path: Path,
) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)

    missing_queue = fixture.dispatch_subject(contract)
    missing_queue["merge_queue_id"] = ""
    missing_result = run_precheck(
        "mf_subagent.dispatch",
        CONTRACT_ID,
        "dispatch",
        missing_queue,
        "pytest",
    )
    assert missing_result["decision"] == "block"
    assert "missing_merge_queue_id" in missing_result["evidence"]["errors"]

    graph_stale = fixture.dispatch_subject(contract)
    graph_stale["active_graph_stale"] = True
    stale_graph_result = run_precheck(
        "mf_subagent.dispatch",
        CONTRACT_ID,
        "dispatch",
        graph_stale,
        "pytest",
    )
    assert stale_graph_result["decision"] == "block"
    assert "active_graph_stale_at_dispatch" in stale_graph_result["evidence"]["errors"]

    bad_adoption = fixture.dispatch_subject(contract)
    bad_adoption["adoption_mode"] = "existing_branch"
    bad_adoption_result = run_precheck(
        "mf_subagent.dispatch",
        CONTRACT_ID,
        "dispatch",
        bad_adoption,
        "pytest",
    )
    assert bad_adoption_result["decision"] == "block"
    assert "missing_existing_branch_adoption_evidence" in bad_adoption_result["evidence"]["errors"]

    good_adoption = fixture.dispatch_subject(contract)
    good_adoption["adoption_mode"] = "existing_branch"
    good_adoption["branch_adoption_evidence"] = {"status": "passed", "branch": fixture.branch}
    good_adoption_result = run_precheck(
        "mf_subagent.dispatch",
        CONTRACT_ID,
        "dispatch",
        good_adoption,
        "pytest",
    )
    assert good_adoption_result["decision"] == "allow"
    assert good_adoption_result["evidence"]["branch_adoption_mode"] == "existing_branch"

    moved_root = tmp_path / "target-move"
    moved_root.mkdir()
    moved_fixture = create_runtime_fixture(moved_root)
    advance_target_head(moved_fixture)
    moved_result = run_precheck(
        "mf_subagent.dispatch",
        CONTRACT_ID,
        "dispatch",
        moved_fixture.dispatch_subject(contract),
        "pytest",
    )
    assert moved_result["decision"] == "block"
    assert "target_head_mismatch" in moved_result["evidence"]["errors"]


def test_scn_mf_wf_010_startup_identity_allows_assigned_worker_worktree(
    tmp_path: Path,
) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)

    result = run_precheck(
        "mf_subagent.startup",
        CONTRACT_ID,
        "startup_gate",
        fixture.startup_subject(contract),
        "pytest",
    )

    assert _result_contract_fields_present(result)
    assert result["decision"] == "allow"
    assert result["evidence"]["expected_worker_git"]["root"] == str(fixture.worker_worktree)
    assert result["evidence"]["actual_runtime_git"]["root"] == str(fixture.worker_worktree)
    assert result["evidence"]["target_git"]["root"] == str(fixture.main_worktree)
    assert result["evidence"]["same_as_expected_worker"] is True
    assert result["evidence"]["same_as_target_main"] is False
    assert result["evidence"]["fence_token_matches"] is True


def test_scn_mf_wf_010_startup_blocks_wrong_main_worktree_and_dirty_owned_overlap(
    tmp_path: Path,
) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)
    make_target_dirty_owned_file(fixture)

    result = run_precheck(
        "mf_subagent.startup",
        CONTRACT_ID,
        "startup_gate",
        fixture.startup_subject(contract, actual_git_root=fixture.main_worktree),
        "pytest",
    )

    assert result["decision"] == "block"
    assert result["evidence"]["expected_worker_git"]["dirty"] is False
    assert result["evidence"]["actual_runtime_git"]["root"] == str(fixture.main_worktree)
    assert result["evidence"]["target_dirty_owned_files"] == [
        "agent/governance/precheck_service.py"
    ]
    assert "actual_worktree_is_target_main" in result["evidence"]["errors"]
    assert "actual_worktree_mismatch" in result["evidence"]["errors"]
    assert "dirty_target_main_worktree_at_startup" in result["evidence"]["errors"]
    assert "target_dirty_owned_file_overlap" in result["evidence"]["errors"]


def test_unknown_gate_and_invalid_contract_block(tmp_path: Path) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)
    subject = fixture.dispatch_subject(contract)

    unknown = run_precheck("unknown.gate", CONTRACT_ID, "dispatch", subject, "pytest")
    assert unknown["decision"] == "block"
    assert "unknown_gate_kind" in unknown["evidence"]["errors"]

    invalid = fixture.dispatch_subject({"contract_instance_id": CONTRACT_ID})
    invalid_result = run_precheck(
        "mf_subagent.dispatch",
        CONTRACT_ID,
        "dispatch",
        invalid,
        "pytest",
    )
    assert invalid_result["decision"] == "block"
    assert "contract_stage_graph_missing" in invalid_result["evidence"]["errors"]


def test_scn_mf_wf_003_handoff_allows_owned_dirty_scope_and_counts_files(
    tmp_path: Path,
) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)
    make_handoff_dirty_scope(fixture)

    result = run_precheck(
        "mf_subagent.handoff",
        CONTRACT_ID,
        "handoff_gate",
        fixture.handoff_subject(contract),
        "pytest",
    )

    assert result["decision"] == "allow"
    assert result["evidence"]["dirty_scope_exact_match"] is True
    assert result["evidence"]["worker_git"]["untracked_count"] == 1
    assert result["evidence"]["worker_git"]["ignored_count"] == 1
    assert result["evidence"]["tests_evidence_present"] is True
    assert result["evidence"]["timeline_evidence_present"] is True

    missing_tests = fixture.handoff_subject(contract)
    missing_tests["tests_evidence"] = {}
    missing_result = run_precheck(
        "mf_subagent.handoff",
        CONTRACT_ID,
        "handoff_gate",
        missing_tests,
        "pytest",
    )
    assert missing_result["decision"] == "block"
    assert "missing_tests_evidence" in missing_result["evidence"]["errors"]


def test_scn_mf_wf_008_handoff_compacts_large_ignored_file_evidence(
    tmp_path: Path,
) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)
    make_many_ignored_files(fixture, count=70)

    result = run_precheck(
        "mf_subagent.handoff",
        CONTRACT_ID,
        "handoff_gate",
        fixture.handoff_subject(contract),
        "pytest",
    )

    worker_git = result["evidence"]["worker_git"]
    assert result["decision"] == "allow"
    assert worker_git["ignored_count"] == 70
    assert worker_git["ignored_truncated"] is True
    assert worker_git["ignored_files_omitted_count"] == 20
    assert len(worker_git["ignored_files"]) == worker_git["ignored_path_limit"]


def test_handoff_blocks_forbidden_paths(tmp_path: Path) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)
    make_forbidden_change(fixture)

    result = run_precheck(
        "mf_subagent.handoff",
        CONTRACT_ID,
        "handoff_gate",
        fixture.handoff_subject(contract),
        "pytest",
    )

    assert result["decision"] == "block"
    assert "forbidden_path_changes" in result["evidence"]["errors"]
    assert result["evidence"]["forbidden_path_hits"] == ["frontend/dashboard/src/App.tsx"]


def test_scn_mf_wf_004_merge_requires_clean_source_token_and_timeline(
    tmp_path: Path,
) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)
    source_commit = commit_worker_candidate(fixture)
    token = make_precheck_token(source_commit)

    result = run_precheck(
        "workflow.merge",
        CONTRACT_ID,
        "merge_gate",
        fixture.merge_subject(contract, source_commit=source_commit, precheck_token=token),
        "pytest",
    )

    assert result["decision"] == "allow"
    assert result["evidence"]["source_commit"] == source_commit
    assert result["evidence"]["timeline_evidence_present"] is True
    assert result["evidence"]["missing_required_evidence"] == []

    stale_token = make_precheck_token("0" * 40)
    stale = run_precheck(
        "workflow.merge",
        CONTRACT_ID,
        "merge_gate",
        fixture.merge_subject(
            contract,
            source_commit=source_commit,
            precheck_token=stale_token,
        ),
        "pytest",
    )
    assert stale["decision"] == "block"
    assert "precheck_token_subject_commit_mismatch" in stale["evidence"]["errors"]


def test_scn_mf_wf_005_merge_queue_entry_blocks_stale_target_head(
    tmp_path: Path,
) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)
    source_commit = commit_worker_candidate(fixture)
    token = make_precheck_token(source_commit)

    allowed = run_precheck(
        "workflow.merge_queue_entry",
        CONTRACT_ID,
        "merge_queue_entry",
        fixture.merge_queue_subject(contract, source_commit=source_commit, precheck_token=token),
        "pytest",
    )
    assert allowed["decision"] == "allow"
    assert allowed["evidence"]["merge_queue_id"] == fixture.merge_queue_id

    advance_target_head(fixture)
    stale = run_precheck(
        "workflow.merge_queue_entry",
        CONTRACT_ID,
        "merge_queue_entry",
        fixture.merge_queue_subject(contract, source_commit=source_commit, precheck_token=token),
        "pytest",
    )
    assert stale["decision"] == "block"
    assert "merge_queue_target_head_stale" in stale["evidence"]["errors"]


def test_merge_preview_requires_preview_evidence_and_live_merge_records_target_head(
    tmp_path: Path,
) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)
    source_commit = commit_worker_candidate(fixture)
    token = make_precheck_token(source_commit)

    preview = run_precheck(
        "workflow.merge_preview",
        CONTRACT_ID,
        "merge_preview",
        fixture.merge_preview_subject(contract, source_commit=source_commit, precheck_token=token),
        "pytest",
    )
    assert preview["decision"] == "allow"
    assert preview["evidence"]["merge_preview_evidence_present"] is True

    missing_preview = fixture.merge_preview_subject(
        contract,
        source_commit=source_commit,
        precheck_token=token,
    )
    missing_preview["merge_preview_evidence"] = {}
    blocked_preview = run_precheck(
        "workflow.merge_preview",
        CONTRACT_ID,
        "merge_preview",
        missing_preview,
        "pytest",
    )
    assert blocked_preview["decision"] == "block"
    assert "missing_merge_preview_evidence" in blocked_preview["evidence"]["errors"]

    merge_commit = merge_worker_candidate(fixture)
    live = run_precheck(
        "workflow.live_merge",
        CONTRACT_ID,
        "live_merge",
        fixture.live_merge_subject(
            contract,
            source_commit=source_commit,
            merge_commit=merge_commit,
            precheck_token=token,
        ),
        "pytest",
    )
    assert live["decision"] == "allow"
    assert live["evidence"]["merge_commit"] == merge_commit
    assert live["evidence"]["observed_target_head"] == merge_commit


def test_merge_reconcile_and_close_block_weak_empty_subject_token(
    tmp_path: Path,
) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)
    source_commit = commit_worker_candidate(fixture)
    weak_token = {
        "precheck_run_id": "precheck-audit-weak-token",
        "evidence_hash": "sha256:" + ("b" * 64),
        "subject": {},
    }

    merge = run_precheck(
        "workflow.merge",
        CONTRACT_ID,
        "merge_gate",
        fixture.merge_subject(
            contract,
            source_commit=source_commit,
            precheck_token=weak_token,
        ),
        "pytest",
    )
    assert merge["decision"] == "block"
    assert "missing_precheck_token_subject_commit" in merge["evidence"]["errors"]
    assert "missing_precheck_token_subject_fence" in merge["evidence"]["errors"]

    reconcile = run_precheck(
        "workflow.reconcile_policy",
        CONTRACT_ID,
        "reconcile",
        {
            "contract": {**contract, "contract_instance_id": CONTRACT_ID},
            "source_commit": source_commit,
            "fence_token": FENCE_TOKEN,
            "precheck_token": weak_token,
            "changed_files": ["agent/governance/precheck_service.py"],
            "scope_kind": "code_module",
            "e2e_decision": "e2e_not_applicable",
        },
        "pytest",
    )
    assert reconcile["decision"] == "block"
    assert "missing_precheck_token_subject_commit" in reconcile["evidence"]["errors"]
    assert "missing_precheck_token_subject_fence" in reconcile["evidence"]["errors"]

    close = run_precheck(
        "backlog.close",
        CONTRACT_ID,
        "close_gate",
        fixture.close_subject(
            contract,
            merge_commit=source_commit,
            precheck_token=weak_token,
        ),
        "pytest",
    )
    assert close["decision"] == "block"
    assert "missing_precheck_token_subject_commit" in close["evidence"]["errors"]
    assert "missing_precheck_token_subject_fence" in close["evidence"]["errors"]


def test_workflow_merge_blocks_stale_source_commit_after_source_head_advances(
    tmp_path: Path,
) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)
    candidate_one = commit_worker_candidate(fixture, message="candidate one")
    stale_token = make_precheck_token(candidate_one)
    (fixture.worker_worktree / "agent/governance/mf_workflow_runtime.py").write_text(
        "BASE = 4\n",
        encoding="utf-8",
    )
    _git(fixture.worker_worktree, "add", "agent/governance/mf_workflow_runtime.py")
    _git(fixture.worker_worktree, "commit", "-m", "candidate two")
    candidate_two = _git(fixture.worker_worktree, "rev-parse", "HEAD")

    result = run_precheck(
        "workflow.merge",
        CONTRACT_ID,
        "merge_gate",
        fixture.merge_subject(
            contract,
            source_commit=candidate_one,
            precheck_token=stale_token,
        ),
        "pytest",
    )

    assert result["decision"] == "block"
    assert result["evidence"]["subject_source_commit"] == candidate_one
    assert result["evidence"]["observed_source_head"] == candidate_two
    assert result["evidence"]["source_commit"] == candidate_two
    assert "source_commit_head_mismatch" in result["evidence"]["errors"]
    assert "precheck_token_subject_commit_mismatch" in result["evidence"]["errors"]


def test_reconcile_policy_verifies_token_and_blocks_runtime_without_e2e() -> None:
    contract = load_workflow_contract()
    source_commit = "1" * 40
    token = make_precheck_token(source_commit)

    allowed = run_precheck(
        "workflow.reconcile_policy",
        CONTRACT_ID,
        "reconcile",
        {
            "contract": {**contract, "contract_instance_id": CONTRACT_ID},
            "source_commit": source_commit,
            "fence_token": FENCE_TOKEN,
            "precheck_token": token,
            "changed_files": ["agent/governance/precheck_service.py"],
            "scope_kind": "code_module",
            "e2e_decision": "e2e_not_applicable",
        },
        "pytest",
    )
    assert allowed["decision"] == "allow"

    blocked = run_precheck(
        "workflow.reconcile_policy",
        CONTRACT_ID,
        "reconcile",
        {
            "contract": {**contract, "contract_instance_id": CONTRACT_ID},
            "source_commit": source_commit,
            "fence_token": FENCE_TOKEN,
            "precheck_token": token,
            "changed_files": ["agent/governance/server.py"],
            "scope_kind": "code_module",
            "e2e_decision": "e2e_not_applicable",
        },
        "pytest",
    )
    assert blocked["decision"] == "block"
    assert "runtime_api_dashboard_change_requires_e2e_or_review" in blocked["evidence"]["errors"]


def test_scn_mf_wf_006_close_gate_requires_close_ready_merge_commit_and_evidence(
    tmp_path: Path,
) -> None:
    contract = load_workflow_contract()
    fixture = create_runtime_fixture(tmp_path)
    source_commit = commit_worker_candidate(fixture)
    token = make_precheck_token(source_commit)

    result = run_precheck(
        "backlog.close",
        CONTRACT_ID,
        "close_gate",
        fixture.close_subject(contract, merge_commit=source_commit, precheck_token=token),
        "pytest",
    )

    assert result["decision"] == "allow"
    assert result["evidence"]["close_ready_present"] is True
    assert result["evidence"]["mf_timeline_precheck_compatible"] is True

    missing = fixture.close_subject(contract, merge_commit=source_commit, precheck_token=token)
    missing["timeline_evidence"] = missing["timeline_evidence"][:-1]
    missing_result = run_precheck(
        "backlog.close",
        CONTRACT_ID,
        "close_gate",
        missing,
        "pytest",
    )
    assert missing_result["decision"] == "block"
    assert "missing_close_ready_timeline" in missing_result["evidence"]["errors"]


def _result_contract_fields_present(result: dict[str, object]) -> bool:
    required = {
        "precheck_run_id",
        "kind",
        "contract_id",
        "stage",
        "decision",
        "status",
        "subject",
        "evidence",
        "evidence_hash",
        "created_at",
    }
    return required.issubset(result)


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()
