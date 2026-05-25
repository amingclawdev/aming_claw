"""Guards for the parallel branch dry-run scenario design contract."""

from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOC_PATH = REPO_ROOT / "docs" / "dev" / "parallel-agent-multibranch-test-scenarios.md"

REQUIRED_SCENARIOS = {
    "PB-001": "Machine restart recovery",
    "PB-002": "Merge dependency ordering",
    "PB-003": "Target branch moves",
    "PB-004": "Wrong merge order rollback/replay",
    "PB-005": "DB rollback consistency",
    "PB-006": "Governance Hint rollback",
    "PB-007": "Chain compatibility",
    "PB-008": "Old agent resurrection",
    "PB-009": "Cleanup retention",
    "PB-010": "Dashboard/MCP compact read model",
    "PB-011": "Branch graph artifact isolation",
    "PB-012": "Multi-project and batch isolation",
    "PB-013": "Existing long-lived ref governance",
    "PB-014": "Managed ref bootstrap/import",
    "PB-015": "MF subagent backend contract",
    "PB-016": "Contract-driven MF workflow gates",
}

REQUIRED_ORACLE_DIMENSIONS = [
    "expected.task_runtime",
    "expected.merge_queue",
    "expected.git",
    "expected.graph",
    "expected.semantic",
    "expected.pending_scope",
    "expected.dashboard_mcp",
    "blocked_by",
]

REQUIRED_INFRA_FLAGS = [
    "I0",
    "I1",
    "I2",
    "I3",
    "I4",
    "I5",
    "I6",
    "I7",
    "I8",
    "I9",
    "I10",
    "I11",
]


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC_PATH.exists(), f"Document not found: {DOC_PATH}"
    return DOC_PATH.read_text(encoding="utf-8")


def test_parallel_branch_test_scenario_doc_exists() -> None:
    assert DOC_PATH.exists()


@pytest.mark.parametrize(("scenario_id", "title"), sorted(REQUIRED_SCENARIOS.items()))
def test_required_scenarios_are_documented(doc_text: str, scenario_id: str, title: str) -> None:
    assert scenario_id in doc_text
    assert title in doc_text


@pytest.mark.parametrize("dimension", REQUIRED_ORACLE_DIMENSIONS)
def test_oracle_dimensions_are_documented(doc_text: str, dimension: str) -> None:
    assert dimension in doc_text


@pytest.mark.parametrize("flag", REQUIRED_INFRA_FLAGS)
def test_infrastructure_flags_are_documented(doc_text: str, flag: str) -> None:
    assert f"| {flag} |" in doc_text


def test_machine_restart_fixture_preserves_mixed_five_task_state(doc_text: str) -> None:
    required_fragments = [
        "T1 | `codex/PB001-T1-scope-reconcile`",
        "T2 | `codex/PB001-T2-branch-graph-refs`",
        "T3 | `codex/PB001-T3-task-runtime`",
        "T4 | `codex/PB001-T4-dashboard-read-model`",
        "T5 | `codex/PB001-T5-chain-adapter`",
        "T1 merged, T2 merge_failed, T4 queued_for_merge, T3/T5 unfinished",
    ]
    for fragment in required_fragments:
        assert fragment in doc_text


def test_pr_gate_requires_scenario_or_pending_update(doc_text: str) -> None:
    assert "Any PR that changes parallel branch runtime schema" in doc_text
    assert "Implement or update a test mapped to the affected scenario" in doc_text
    assert "Mark the scenario `pending` with an explicit infrastructure blocker" in doc_text
    assert "Update this matrix when the behavior intentionally changes" in doc_text


def test_dry_run_oracle_cannot_mutate_production_state(doc_text: str) -> None:
    normalized = " ".join(doc_text.split())
    assert "must not mutate the production git checkout or production governance DB" in normalized
    assert "isolated temporary repository" in doc_text
    assert "ephemeral SQLite database" in doc_text


def test_branch_graph_policy_is_one_hop_candidate_evidence(doc_text: str) -> None:
    normalized = " ".join(doc_text.split())
    assert "one-hop candidate deltas from a target graph" in normalized
    assert "must not chain from prior branch candidates" in normalized
    assert "do not chain branch candidates" in normalized


def test_existing_long_lived_refs_are_not_modeled_as_new_projects(doc_text: str) -> None:
    normalized = " ".join(doc_text.split())
    assert "Keep one project identity" in doc_text
    assert "create managed ref contexts" in normalized
    assert "archive source ref context instead of deleting a project" in normalized


def test_managed_ref_bootstrap_requires_dry_run_classification(doc_text: str) -> None:
    normalized = " ".join(doc_text.split())
    assert "Managed ref bootstrap/import" in doc_text
    assert "Dry-run classifies target, short-lived agent, managed, ignored, unmanaged, and blocked refs" in doc_text
    assert "managed-ref bootstrap dry-run before apply" in normalized


def test_mf_subagent_contract_is_bounded_to_branch_worker_actions(doc_text: str) -> None:
    normalized = " ".join(doc_text.split())
    assert "MF subagent backend contract" in doc_text
    assert "Build an `mf_sub` payload from `BranchTaskRuntimeContext`" in doc_text
    assert "reject stale fences and merge/push/graph activation attempts" in normalized
    assert "MF subagents are branch workers, not merge workers" in doc_text
    assert "Contract-driven MF workflow gates" in doc_text
    assert "merge_queue_entry" in doc_text
    assert "active graph stale state" in normalized
