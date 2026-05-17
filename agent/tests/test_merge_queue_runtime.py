"""Executable dry-run scenarios for parallel branch merge queue decisions."""

from __future__ import annotations

import sqlite3
import pytest

from agent.tests.fixtures.parallel_project import create_merge_preview_fixture_project
from agent.governance.parallel_branch_runtime import (
    ACTION_ALLOW_MERGE,
    ACTION_BLOCKED_BY_DEPENDENCY,
    ACTION_OPERATOR_APPROVE_LIVE_MERGE,
    ACTION_REVALIDATE_AFTER_DEPENDENCY_MERGE,
    ACTION_WAIT_FOR_DEPENDENCY,
    BATCH_STATE_ROLLBACK_REQUIRED,
    BranchRuntimeFenceError,
    BranchTaskRuntimeContext,
    MERGE_GATE_REQUIRED_EVIDENCE,
    STATE_DEPENDENCY_BLOCKED,
    STATE_MERGE_READY,
    STATE_MERGED,
    STATE_RUNNING,
    STATE_STALE_AFTER_DEPENDENCY_MERGE,
    STATE_WAITING_DEPENDENCY,
    MergeQueueItem,
    decide_merge_gate,
    decide_merge_queue,
    decide_persisted_merge_gate,
    decide_persisted_merge_queue,
    get_branch_context,
    git_merge_preview_evidence,
    list_merge_queue_items,
    merge_gate_plan_to_dict,
    record_merge_queue_result,
    upsert_branch_context,
    upsert_merge_queue_items,
)

PROJECT_ID = "fixture-parallel-project"
QUEUE_ID = "mergeq-PB002"
TARGET_REF = "refs/heads/main"


def _by_task(plan):
    return {decision.task_id: decision for decision in plan.decisions}


def _runtime_conn(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _passing_merge_evidence() -> dict[str, dict[str, str]]:
    return {
        key: {"status": "pass", "evidence_id": f"evidence-{key}"}
        for key in MERGE_GATE_REQUIRED_EVIDENCE
    }


def test_pb002_downstream_merge_waits_for_unmerged_foundation_dependency() -> None:
    """PB-002: downstream requests merge before upstream foundation is merged."""
    items = [
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id=QUEUE_ID,
            queue_item_id="item-T2",
            task_id="T2",
            branch_ref="refs/heads/codex/PB002-T2-dashboard-read-model",
            queue_index=2,
            status=STATE_MERGE_READY,
            depends_on=("T1",),
            target_ref=TARGET_REF,
            base_commit="target-base",
            branch_head="head-T2",
            validated_target_head="target-base",
            current_target_head="target-base",
            validation_attempt=1,
            merge_preview_id="preview-T2",
        ),
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id=QUEUE_ID,
            queue_item_id="item-T1",
            task_id="T1",
            branch_ref="refs/heads/codex/PB002-T1-scope-reconcile-foundation",
            queue_index=1,
            status=STATE_MERGE_READY,
            target_ref=TARGET_REF,
            base_commit="target-base",
            branch_head="head-T1",
            validated_target_head="target-base",
            current_target_head="target-base",
            validation_attempt=1,
            merge_preview_id="preview-T1",
        ),
    ]

    plan = decide_merge_queue(items, scenario_id="PB-002")
    decisions = _by_task(plan)

    assert [decision.task_id for decision in plan.decisions] == ["T1", "T2"]
    assert plan.mergeable_task_ids == ("T1",)
    assert plan.blocked_task_ids == ("T2",)
    assert plan.target_mutation_blocked_for == ("T2",)

    assert decisions["T1"].queue_state == STATE_MERGE_READY
    assert decisions["T1"].action == ACTION_ALLOW_MERGE
    assert decisions["T1"].merge_allowed is True
    assert decisions["T1"].target_branch_mutation_allowed is True

    assert decisions["T2"].queue_state == STATE_WAITING_DEPENDENCY
    assert decisions["T2"].action == ACTION_WAIT_FOR_DEPENDENCY
    assert decisions["T2"].dependency_blockers == ("T1",)
    assert decisions["T2"].dependency_blocker_types == {"T1": ("hard_depends_on",)}
    assert decisions["T2"].next_actions == ("wait_for_dependency", "do_not_merge")
    assert decisions["T2"].merge_allowed is False
    assert decisions["T2"].target_branch_mutation_allowed is False
    assert decisions["T2"].target_graph_activation_allowed is False
    assert decisions["T2"].target_semantic_activation_allowed is False


def test_pb002_persisted_merge_queue_replays_dependency_blockers_after_restart(tmp_path) -> None:
    db_path = str(tmp_path / "runtime.db")
    conn = _runtime_conn(db_path)
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=QUEUE_ID,
                queue_item_id="item-T2",
                task_id="T2",
                branch_ref="refs/heads/codex/PB002-T2-dashboard-read-model",
                queue_index=2,
                status=STATE_MERGE_READY,
                hard_depends_on=("T1",),
                serializes_after=("T1",),
                requires_graph_epoch=("T1",),
                target_ref=TARGET_REF,
                base_commit="target-base",
                branch_head="head-T2",
                validated_target_head="target-base",
                current_target_head="target-base",
                validation_attempt=1,
                merge_preview_id="preview-T2",
                snapshot_id="scope-T2",
                projection_id="semproj-T2",
            ),
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=QUEUE_ID,
                queue_item_id="item-T1",
                task_id="T1",
                branch_ref="refs/heads/codex/PB002-T1-scope-reconcile-foundation",
                queue_index=1,
                status=STATE_RUNNING,
                target_ref=TARGET_REF,
                base_commit="target-base",
                branch_head="head-T1",
            ),
        ],
        now_iso="2026-05-17T06:00:00Z",
    )
    conn.commit()
    conn.close()

    restarted = _runtime_conn(db_path)
    persisted = list_merge_queue_items(restarted, PROJECT_ID, QUEUE_ID, target_ref=TARGET_REF)
    assert [item.task_id for item in persisted] == ["T1", "T2"]
    assert persisted[1].hard_depends_on == ("T1",)
    assert persisted[1].serializes_after == ("T1",)
    assert persisted[1].requires_graph_epoch == ("T1",)
    assert persisted[1].merge_preview_id == "preview-T2"
    assert persisted[1].snapshot_id == "scope-T2"
    assert persisted[1].projection_id == "semproj-T2"

    plan = decide_persisted_merge_queue(
        restarted,
        PROJECT_ID,
        QUEUE_ID,
        target_ref=TARGET_REF,
        scenario_id="PB-002",
    )
    decisions = _by_task(plan)

    assert plan.mergeable_task_ids == ()
    assert plan.blocked_task_ids == ("T2",)
    assert decisions["T2"].queue_state == STATE_DEPENDENCY_BLOCKED
    assert decisions["T2"].dependency_blocker_types == {
        "T1": ("hard_depends_on", "requires_graph_epoch", "serializes_after")
    }
    assert decisions["T2"].target_branch_mutation_allowed is False


def test_pb003_downstream_validated_branch_goes_stale_after_dependency_merge() -> None:
    """PB-003: dependency merge moves target head after downstream validation."""
    items = [
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id="mergeq-PB003",
            queue_item_id="item-T1",
            task_id="T1",
            branch_ref="refs/heads/codex/PB003-T1-scope-reconcile-foundation",
            queue_index=1,
            status=STATE_MERGED,
            target_ref=TARGET_REF,
            base_commit="target-base",
            branch_head="head-T1",
            current_target_head="target-after-T1",
        ),
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id="mergeq-PB003",
            queue_item_id="item-T2",
            task_id="T2",
            branch_ref="refs/heads/codex/PB003-T2-dashboard-read-model",
            queue_index=2,
            status=STATE_MERGE_READY,
            depends_on=("T1",),
            target_ref=TARGET_REF,
            base_commit="target-base",
            branch_head="head-T2",
            validated_target_head="target-base",
            current_target_head="target-after-T1",
            validation_attempt=1,
            merge_preview_id="preview-T2-before-T1",
            snapshot_id="scope-T2-before-T1",
            projection_id="semproj-T2-before-T1",
        ),
    ]

    plan = decide_merge_queue(items, scenario_id="PB-003")
    decisions = _by_task(plan)

    assert plan.mergeable_task_ids == ()
    assert plan.blocked_task_ids == ()
    assert plan.stale_task_ids == ("T2",)
    assert plan.target_mutation_blocked_for == ("T1", "T2")

    assert decisions["T1"].queue_state == STATE_MERGED
    assert decisions["T1"].target_graph_activation_allowed is True
    assert decisions["T1"].target_semantic_activation_allowed is True

    assert decisions["T2"].queue_state == STATE_STALE_AFTER_DEPENDENCY_MERGE
    assert decisions["T2"].action == ACTION_REVALIDATE_AFTER_DEPENDENCY_MERGE
    assert decisions["T2"].stale_target_head is True
    assert decisions["T2"].dependency_blockers == ()
    assert decisions["T2"].next_actions == (
        "rebase_or_sync",
        "run_scope_reconcile",
        "verify_semantic_projection",
        "refresh_merge_preview",
    )
    assert decisions["T2"].merge_allowed is False
    assert decisions["T2"].target_branch_mutation_allowed is False
    assert decisions["T2"].target_graph_activation_allowed is False
    assert decisions["T2"].target_semantic_activation_allowed is False


def test_pb003_persisted_merge_queue_rehydrates_stale_validation_after_restart(tmp_path) -> None:
    db_path = str(tmp_path / "runtime.db")
    conn = _runtime_conn(db_path)
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="mergeq-PB003",
                queue_item_id="item-T1",
                task_id="T1",
                branch_ref="refs/heads/codex/PB003-T1-scope-reconcile-foundation",
                queue_index=1,
                status=STATE_MERGED,
                target_ref=TARGET_REF,
                base_commit="target-base",
                branch_head="head-T1",
                current_target_head="target-after-T1",
                snapshot_id="scope-T1",
                projection_id="semproj-T1",
            ),
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="mergeq-PB003",
                queue_item_id="item-T2",
                task_id="T2",
                branch_ref="refs/heads/codex/PB003-T2-dashboard-read-model",
                queue_index=2,
                status=STATE_MERGE_READY,
                depends_on=("T1",),
                target_ref=TARGET_REF,
                base_commit="target-base",
                branch_head="head-T2",
                validated_target_head="target-base",
                current_target_head="target-after-T1",
                validation_attempt=1,
                merge_preview_id="preview-T2-before-T1",
                snapshot_id="scope-T2-before-T1",
                projection_id="semproj-T2-before-T1",
            ),
        ],
        now_iso="2026-05-17T06:05:00Z",
    )
    conn.commit()
    conn.close()

    restarted = _runtime_conn(db_path)
    plan = decide_persisted_merge_queue(
        restarted,
        PROJECT_ID,
        "mergeq-PB003",
        target_ref=TARGET_REF,
        scenario_id="PB-003",
    )
    decisions = _by_task(plan)

    assert plan.stale_task_ids == ("T2",)
    assert decisions["T1"].target_graph_activation_allowed is True
    assert decisions["T1"].target_semantic_activation_allowed is True
    assert decisions["T2"].queue_state == STATE_STALE_AFTER_DEPENDENCY_MERGE
    assert decisions["T2"].stale_target_head is True
    assert decisions["T2"].merge_preview_id == "preview-T2-before-T1"
    assert decisions["T2"].next_actions == (
        "rebase_or_sync",
        "run_scope_reconcile",
        "verify_semantic_projection",
        "refresh_merge_preview",
    )


def test_persisted_merge_queue_marks_supplied_target_head_drift(tmp_path) -> None:
    db_path = str(tmp_path / "runtime.db")
    conn = _runtime_conn(db_path)
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="mergeq-target-drift",
                queue_item_id="item-T1",
                task_id="T1",
                branch_ref="refs/heads/codex/T1",
                queue_index=1,
                status=STATE_MERGE_READY,
                target_ref=TARGET_REF,
                branch_head="head-T1",
                validated_target_head="target-before",
                current_target_head="target-before",
                merge_preview_id="preview-before",
            )
        ],
        now_iso="2026-05-17T09:10:00Z",
    )
    conn.commit()

    plan = decide_persisted_merge_queue(
        conn,
        PROJECT_ID,
        "mergeq-target-drift",
        target_ref=TARGET_REF,
        current_target_head="target-after",
        scenario_id="PB-target-drift",
    )
    decision = plan.decisions[0]

    assert plan.mergeable_task_ids == ()
    assert plan.stale_task_ids == ("T1",)
    assert decision.queue_state == STATE_STALE_AFTER_DEPENDENCY_MERGE
    assert decision.stale_target_head is True
    assert decision.merge_allowed is False
    assert decision.next_actions == (
        "rebase_or_sync",
        "run_scope_reconcile",
        "verify_semantic_projection",
        "refresh_merge_preview",
    )


def test_merge_queue_dashboard_rows_are_deterministic_and_reviewable() -> None:
    items = [
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id=QUEUE_ID,
            queue_item_id="item-T2",
            task_id="T2",
            branch_ref="refs/heads/codex/PB002-T2-dashboard-read-model",
            queue_index=2,
            status=STATE_MERGE_READY,
            depends_on=("T1",),
            target_ref=TARGET_REF,
        ),
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id=QUEUE_ID,
            queue_item_id="item-T1",
            task_id="T1",
            branch_ref="refs/heads/codex/PB002-T1-scope-reconcile-foundation",
            queue_index=1,
            status=STATE_MERGE_READY,
            target_ref=TARGET_REF,
        ),
    ]

    plan = decide_merge_queue(items, scenario_id="PB-002")

    assert [row["task_id"] for row in plan.dashboard_rows] == ["T1", "T2"]
    assert plan.dashboard_rows[1] == {
        "queue_item_id": "item-T2",
        "task_id": "T2",
        "branch_ref": "refs/heads/codex/PB002-T2-dashboard-read-model",
        "observed_status": STATE_MERGE_READY,
        "queue_state": STATE_WAITING_DEPENDENCY,
        "action": ACTION_WAIT_FOR_DEPENDENCY,
        "dependency_blockers": ["T1"],
        "dependency_blocker_types": {"T1": ["hard_depends_on"]},
        "stale_target_head": False,
        "next_actions": ["wait_for_dependency", "do_not_merge"],
        "merge_allowed": False,
        "target_branch_mutation_allowed": False,
        "target_graph_activation_allowed": False,
        "target_semantic_activation_allowed": False,
        "validation_attempt": 0,
        "merge_preview_id": "",
    }


def test_typed_dependency_blockers_are_compact_and_merge_blocking() -> None:
    items = [
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id=QUEUE_ID,
            queue_item_id="item-T1",
            task_id="T1",
            branch_ref="refs/heads/codex/PB002-T1-foundation",
            queue_index=1,
            status=STATE_RUNNING,
            target_ref=TARGET_REF,
        ),
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id=QUEUE_ID,
            queue_item_id="item-T2",
            task_id="T2",
            branch_ref="refs/heads/codex/PB002-T2-feature",
            queue_index=2,
            status=STATE_MERGE_READY,
            target_ref=TARGET_REF,
            hard_depends_on=("T1",),
            serializes_after=("T1",),
            requires_graph_epoch=("T1",),
        ),
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id=QUEUE_ID,
            queue_item_id="item-T3",
            task_id="T3",
            branch_ref="refs/heads/codex/PB002-T3-independent",
            queue_index=3,
            status=STATE_MERGE_READY,
            target_ref=TARGET_REF,
        ),
    ]

    plan = decide_merge_queue(items, scenario_id="PB-002")
    decisions = _by_task(plan)

    assert decisions["T2"].queue_state == STATE_DEPENDENCY_BLOCKED
    assert decisions["T2"].action == ACTION_BLOCKED_BY_DEPENDENCY
    assert decisions["T2"].dependency_blockers == ("T1",)
    assert decisions["T2"].dependency_blocker_types == {
        "T1": ("hard_depends_on", "requires_graph_epoch", "serializes_after")
    }
    assert decisions["T2"].merge_allowed is False
    assert decisions["T3"].queue_state == STATE_MERGE_READY
    assert decisions["T3"].merge_allowed is True
    assert plan.mergeable_task_ids == ("T3",)


def test_conflict_dependencies_require_operator_resolution() -> None:
    items = [
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id=QUEUE_ID,
            queue_item_id="item-T1",
            task_id="T1",
            branch_ref="refs/heads/codex/PB002-T1-node-change",
            queue_index=1,
            status=STATE_MERGE_READY,
            target_ref=TARGET_REF,
        ),
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id=QUEUE_ID,
            queue_item_id="item-T2",
            task_id="T2",
            branch_ref="refs/heads/codex/PB002-T2-conflicting-node-change",
            queue_index=2,
            status=STATE_MERGE_READY,
            target_ref=TARGET_REF,
            conflicts_with=("T1",),
            same_node_or_file_conflicts=("T1",),
        ),
    ]

    plan = decide_merge_queue(items, scenario_id="PB-002")
    decisions = _by_task(plan)

    assert decisions["T1"].merge_allowed is True
    assert decisions["T2"].queue_state == STATE_DEPENDENCY_BLOCKED
    assert decisions["T2"].action == ACTION_BLOCKED_BY_DEPENDENCY
    assert decisions["T2"].dependency_blocker_types == {
        "T1": ("conflicts_with", "same_node_or_file_conflict")
    }
    assert decisions["T2"].next_actions == ("resolve_dependency", "do_not_merge")


def test_merge_gate_blocks_target_mutation_until_evidence_is_complete() -> None:
    item = MergeQueueItem(
        project_id=PROJECT_ID,
        merge_queue_id="mergeq-gate",
        queue_item_id="item-T1",
        task_id="T1",
        branch_ref="refs/heads/codex/PB013-T1-ready",
        queue_index=1,
        status=STATE_MERGE_READY,
        target_ref=TARGET_REF,
        branch_head="head-T1",
        current_target_head="target-base",
        validated_target_head="target-base",
        merge_preview_id="preview-T1",
        snapshot_id="scope-T1",
        projection_id="semproj-T1",
    )

    missing = decide_merge_gate([item], task_id="T1", scenario_id="PB-013")

    assert missing.merge_gate_passed is False
    assert missing.merge_allowed is False
    assert missing.target_branch_mutation_allowed is False
    assert "missing_evidence:git_conflict_check" in missing.blocker_codes
    assert "provide_required_merge_evidence" in missing.next_actions

    dry_run = decide_merge_gate(
        [item],
        task_id="T1",
        evidence=_passing_merge_evidence(),
        scenario_id="PB-013",
    )

    assert dry_run.merge_gate_passed is True
    assert dry_run.merge_allowed is True
    assert dry_run.dry_run is True
    assert dry_run.target_branch_mutation_allowed is False
    assert dry_run.target_graph_activation_allowed is False
    assert dry_run.next_actions == (ACTION_OPERATOR_APPROVE_LIVE_MERGE,)
    assert dry_run.merge_steps == (
        "lock_target_ref",
        "verify_target_head",
        "merge_branch",
        "record_merge_result",
        "run_scope_catchup",
        "activate_target_graph_refs",
        "activate_target_semantic_projection",
    )

    payload = merge_gate_plan_to_dict(dry_run)
    assert payload["evidence"][0]["evidence_id"].startswith("evidence-")
    assert payload["blockers"] == []


def test_merge_gate_blocks_dependency_stale_target_and_batch_rollback() -> None:
    items = [
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id="mergeq-gate-blocked",
            queue_item_id="item-T1",
            task_id="T1",
            branch_ref="refs/heads/codex/PB013-T1-foundation",
            queue_index=1,
            status=STATE_RUNNING,
            target_ref=TARGET_REF,
        ),
        MergeQueueItem(
            project_id=PROJECT_ID,
            merge_queue_id="mergeq-gate-blocked",
            queue_item_id="item-T2",
            task_id="T2",
            branch_ref="refs/heads/codex/PB013-T2-downstream",
            queue_index=2,
            status=STATE_MERGE_READY,
            hard_depends_on=("T1",),
            target_ref=TARGET_REF,
            branch_head="head-T2",
            validated_target_head="target-before",
            current_target_head="target-after",
        ),
    ]

    plan = decide_merge_gate(
        items,
        task_id="T2",
        evidence=_passing_merge_evidence(),
        batch_status=BATCH_STATE_ROLLBACK_REQUIRED,
        dry_run=False,
        scenario_id="PB-013",
    )

    assert plan.merge_gate_passed is False
    assert plan.target_branch_mutation_allowed is False
    assert "queue_dependency_blocked" in plan.blocker_codes
    assert "batch_rollback_required" in plan.blocker_codes
    assert "resolve_queue_dependencies" in plan.next_actions
    assert "resolve_batch_rollback" in plan.next_actions


def test_persisted_merge_gate_replays_after_restart(tmp_path) -> None:
    db_path = str(tmp_path / "runtime.db")
    conn = _runtime_conn(db_path)
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="mergeq-gate-restart",
                queue_item_id="item-T1",
                task_id="T1",
                branch_ref="refs/heads/codex/PB013-T1-ready",
                queue_index=1,
                status=STATE_MERGE_READY,
                target_ref=TARGET_REF,
                branch_head="head-T1",
                validated_target_head="target-base",
                current_target_head="target-base",
                merge_preview_id="preview-T1",
                snapshot_id="scope-T1",
                projection_id="semproj-T1",
            ),
        ],
        now_iso="2026-05-17T08:00:00Z",
    )
    conn.commit()
    conn.close()

    restarted = _runtime_conn(db_path)
    plan = decide_persisted_merge_gate(
        restarted,
        PROJECT_ID,
        "mergeq-gate-restart",
        target_ref=TARGET_REF,
        task_id="T1",
        evidence={
            **_passing_merge_evidence(),
            "semantic_projection": {
                "status": "intentionally_deferred",
                "evidence_id": "semantic-deferred",
            },
        },
        scenario_id="PB-013",
    )

    assert plan.merge_gate_passed is True
    assert plan.warnings == (
        {
            "code": "deferred_evidence:semantic_projection",
            "source": "evidence",
            "message": "semantic_projection is intentionally deferred",
        },
    )
    assert plan.merge_preview_id == "preview-T1"
    assert plan.snapshot_id == "scope-T1"


def test_git_merge_preview_evidence_reports_clean_conflict_and_stale(tmp_path) -> None:
    fixture = create_merge_preview_fixture_project(tmp_path)

    clean = git_merge_preview_evidence(
        repo_root_path=fixture.root,
        target_ref="main",
        branch_ref=fixture.clean_branch,
        expected_target_head=fixture.main_head,
    )
    assert clean["status"] == "pass"
    assert clean["passed"] is True
    assert clean["preview_tree"]
    assert clean["target_commit"] == fixture.main_head

    conflict = git_merge_preview_evidence(
        repo_root_path=fixture.root,
        target_ref="main",
        branch_ref=fixture.conflict_branch,
        expected_target_head=fixture.main_head,
    )
    assert conflict["status"] == "fail"
    assert conflict["passed"] is False
    assert "CONFLICT" in conflict["stdout"]

    stale = git_merge_preview_evidence(
        repo_root_path=fixture.root,
        target_ref="main",
        branch_ref=fixture.clean_branch,
        expected_target_head="not-the-current-head",
    )
    assert stale["status"] == "stale"
    assert stale["passed"] is False
    assert stale["reason"] == "target head differs from expected_target_head"


def test_merge_result_recording_updates_queue_and_context_with_fence() -> None:
    conn = _runtime_conn()
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PROJECT_ID,
            batch_id="PB-014",
            task_id="T1",
            branch_ref="refs/heads/codex/PB014-T1-ready",
            status=STATE_MERGE_READY,
            fence_token="fence-merge-current",
            target_head_commit="target-before",
            merge_queue_id="mergeq-result",
            merge_preview_id="preview-result",
        ),
        now_iso="2026-05-17T08:20:00Z",
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="mergeq-result",
                queue_item_id="item-T1",
                task_id="T1",
                branch_ref="refs/heads/codex/PB014-T1-ready",
                queue_index=1,
                status=STATE_MERGE_READY,
                target_ref=TARGET_REF,
                branch_head="head-T1",
                validated_target_head="target-before",
                current_target_head="target-before",
                merge_preview_id="preview-result",
                snapshot_id="scope-T1",
                projection_id="semproj-T1",
            ),
        ],
        now_iso="2026-05-17T08:20:00Z",
    )

    with pytest.raises(BranchRuntimeFenceError):
        record_merge_queue_result(
            conn,
            project_id=PROJECT_ID,
            merge_queue_id="mergeq-result",
            task_id="T1",
            status=STATE_MERGED,
            merge_commit="merge-T1",
            target_head_after_merge="target-after",
            fence_token="fence-stale",
            now_iso="2026-05-17T08:21:00Z",
        )

    recorded = record_merge_queue_result(
        conn,
        project_id=PROJECT_ID,
        merge_queue_id="mergeq-result",
        task_id="T1",
        status=STATE_MERGED,
        merge_commit="merge-T1",
        target_head_before_merge="target-before",
        target_head_after_merge="target-after",
        fence_token="fence-merge-current",
        now_iso="2026-05-17T08:22:00Z",
    )

    assert recorded["queue_item"]["status"] == STATE_MERGED
    assert recorded["queue_item"]["merge_commit"] == "merge-T1"
    assert recorded["queue_item"]["target_head_before_merge"] == "target-before"
    assert recorded["queue_item"]["target_head_after_merge"] == "target-after"
    assert recorded["queue_item"]["completed_at"] == "2026-05-17T08:22:00Z"
    assert recorded["context"]["status"] == STATE_MERGED
    assert recorded["context"]["target_head_commit"] == "target-after"

    context = get_branch_context(conn, PROJECT_ID, "T1")
    assert context is not None
    assert context.status == STATE_MERGED
    assert context.target_head_commit == "target-after"

    plan = decide_persisted_merge_queue(
        conn,
        PROJECT_ID,
        "mergeq-result",
        target_ref=TARGET_REF,
        scenario_id="PB-014",
    )
    assert plan.decisions[0].target_graph_activation_allowed is True
    assert plan.decisions[0].target_semantic_activation_allowed is True


def test_pb012_merge_queue_rejects_mixed_project_queue_or_target_scope() -> None:
    base = MergeQueueItem(
        project_id=PROJECT_ID,
        merge_queue_id=QUEUE_ID,
        queue_item_id="item-T1",
        task_id="T1",
        branch_ref="refs/heads/codex/PB012-T1",
        queue_index=1,
        status=STATE_MERGE_READY,
        target_ref=TARGET_REF,
    )

    with pytest.raises(ValueError, match="project_id"):
        decide_merge_queue([
            base,
            MergeQueueItem(
                project_id="other-project",
                merge_queue_id=QUEUE_ID,
                queue_item_id="item-T2",
                task_id="T2",
                branch_ref="refs/heads/codex/PB012-T2",
                queue_index=2,
                status=STATE_MERGE_READY,
                target_ref=TARGET_REF,
            ),
        ], scenario_id="PB-012")

    with pytest.raises(ValueError, match="merge_queue_id"):
        decide_merge_queue([
            base,
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id="other-queue",
                queue_item_id="item-T2",
                task_id="T2",
                branch_ref="refs/heads/codex/PB012-T2",
                queue_index=2,
                status=STATE_MERGE_READY,
                target_ref=TARGET_REF,
            ),
        ], scenario_id="PB-012")

    with pytest.raises(ValueError, match="target_ref"):
        decide_merge_queue([
            base,
            MergeQueueItem(
                project_id=PROJECT_ID,
                merge_queue_id=QUEUE_ID,
                queue_item_id="item-T2",
                task_id="T2",
                branch_ref="refs/heads/codex/PB012-T2",
                queue_index=2,
                status=STATE_MERGE_READY,
                target_ref="refs/heads/release",
            ),
        ], scenario_id="PB-012")
