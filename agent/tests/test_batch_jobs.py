from __future__ import annotations

import json
import os
import sqlite3
import subprocess

import pytest

from agent.governance.db import _ensure_schema
from agent.governance import batch_jobs


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "-b", "main"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)
    return repo


def _write_project_graph(repo, project_id="proj", files=None):
    files = files or ["README.md"]
    graph_dir = repo / "shared-volume" / "codex-tasks" / "state" / "governance" / project_id
    graph_dir.mkdir(parents=True, exist_ok=True)
    graph = {
        "version": "test",
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.1",
                    "primary": [files[0]],
                    "secondary": files[1:],
                    "test": [],
                }
            ],
            "links": [],
        },
    }
    path = graph_dir / "graph.json"
    path.write_text(json.dumps(graph), encoding="utf-8")
    return path


def _write_overlay_coverage(branch_graph: dict, files):
    overlay_path = branch_graph["overlay_path"]
    overlay = json.loads(open(overlay_path, encoding="utf-8").read())
    overlay["covered_files"] = list(files)
    with open(overlay_path, "w", encoding="utf-8") as f:
        json.dump(overlay, f, indent=2, sort_keys=True)
        f.write("\n")


def _metadata(conn, task_id: str) -> dict:
    raw = conn.execute(
        "SELECT metadata_json FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()["metadata_json"]
    return json.loads(raw)


def test_job_type_defaults_to_feature_work_for_normal_chain():
    from agent.governance.task_registry import create_task

    conn = _conn()
    task = create_task(conn, "proj", "plan feature", task_type="pm")

    meta = _metadata(conn, task["task_id"])
    assert meta["job_type"] == "feature_work"
    assert meta["stage_type"] == "pm"
    row = conn.execute("SELECT type FROM tasks WHERE task_id=?", (task["task_id"],)).fetchone()
    assert row["type"] == "pm"


def test_stage_type_routing_ignores_job_type():
    from agent.governance.task_registry import claim_task, create_task

    conn = _conn()
    create_task(
        conn,
        "proj",
        "batch-owned pm stage",
        task_type="pm",
        metadata={"job_type": "batch_migration"},
    )

    claimed, _fence = claim_task(conn, "proj", "worker-1")
    assert claimed["type"] == "pm"
    assert claimed["metadata"]["job_type"] == "batch_migration"
    assert claimed["metadata"]["stage_type"] == "pm"


def test_batch_migration_strategy_uses_codex_batch_branch(tmp_path):
    repo = _git_repo(tmp_path)
    base = batch_jobs.git_commit(repo)

    strategy = batch_jobs.resolve_branch_strategy(
        job_type="batch_migration",
        repo_root_path=repo,
        project_id="proj",
        base_commit=base,
        batch_id="batch-001",
    )

    assert strategy.base_commit == base
    assert strategy.work_branch == "codex/batch-batch-001"
    assert strategy.worktree_relpath == ".worktrees/batch-batch-001"
    assert strategy.direct is False
    assert strategy.project_id == "proj"


def test_manual_fix_strategy_is_direct_main(tmp_path):
    repo = _git_repo(tmp_path)
    strategy = batch_jobs.resolve_branch_strategy(
        job_type="manual_fix",
        repo_root_path=repo,
        target_branch="main",
        base_commit=batch_jobs.git_commit(repo),
    )

    assert strategy.direct is True
    assert strategy.work_branch == "main"
    assert strategy.worktree_path == ""


def test_batch_metadata_round_trips_through_task_metadata(tmp_path):
    repo = _git_repo(tmp_path)
    conn = _conn()

    created = batch_jobs.create_batch_task(
        conn,
        "proj",
        "implement migration",
        repo_root_path=repo,
        batch_id="batch-002",
        base_commit=batch_jobs.git_commit(repo),
        metadata={"bug_id": "OPT-BATCH"},
    )

    meta = _metadata(conn, created["task_id"])
    row = conn.execute(
        "SELECT status, execution_status FROM tasks WHERE task_id=?",
        (created["task_id"],),
    ).fetchone()
    assert row["status"] == "queued"
    assert row["execution_status"] == "queued"
    assert meta["job_type"] == "batch_migration"
    assert meta["batch_id"] == "batch-002"
    assert meta["batch_status"] == "created"
    assert meta["work_branch"] == "codex/batch-batch-002"
    assert meta["branch_graph_required"] is True
    assert meta["branch_graph"]["status"] == "planned"
    assert meta["branch_graph"]["work_branch"] == "codex/batch-batch-002"
    assert meta["batch_state_history"][0]["status"] == "created"


def test_batch_status_is_metadata_not_task_status(tmp_path):
    repo = _git_repo(tmp_path)
    conn = _conn()
    created = batch_jobs.create_batch_task(
        conn,
        "proj",
        "implement migration",
        repo_root_path=repo,
        batch_id="batch-003",
        base_commit=batch_jobs.git_commit(repo),
    )

    batch_jobs.record_task_batch_state(
        conn,
        created["task_id"],
        "worktree_ready",
        evidence={"worktree_path": created["branch_strategy"]["worktree_path"]},
    )
    row = conn.execute(
        "SELECT status, execution_status, metadata_json FROM tasks WHERE task_id=?",
        (created["task_id"],),
    ).fetchone()
    meta = json.loads(row["metadata_json"])
    assert row["status"] == "queued"
    assert row["execution_status"] == "queued"
    assert meta["batch_status"] == "worktree_ready"


def test_one_active_batch_migration_per_project(tmp_path):
    repo = _git_repo(tmp_path)
    conn = _conn()
    base = batch_jobs.git_commit(repo)
    batch_jobs.create_batch_task(
        conn,
        "proj",
        "first",
        repo_root_path=repo,
        batch_id="batch-004",
        base_commit=base,
    )

    with pytest.raises(batch_jobs.ActiveBatchExistsError):
        batch_jobs.create_batch_task(
            conn,
            "proj",
            "second",
            repo_root_path=repo,
            batch_id="batch-005",
            base_commit=base,
        )

    created = batch_jobs.create_batch_task(
        conn,
        "proj",
        "second override",
        repo_root_path=repo,
        batch_id="batch-006",
        base_commit=base,
        observer_override=True,
    )
    assert created["metadata"]["observer_override"] is True


def test_worktree_create_abandon_and_stale_report(tmp_path):
    repo = _git_repo(tmp_path)
    conn = _conn()
    created = batch_jobs.create_batch_task(
        conn,
        "proj",
        "worktree smoke",
        repo_root_path=repo,
        batch_id="batch-007",
        base_commit=batch_jobs.git_commit(repo),
    )
    strategy = batch_jobs.BranchStrategy(**created["branch_strategy"])

    out = batch_jobs.create_worktree(strategy, repo_root_path=repo)
    assert out["created"] is True
    assert (repo / ".worktrees" / "batch-batch-007").exists()
    assert out["branch_graph"]["status"] == "ready"
    assert out["branch_graph"]["base_graph_sha256"]
    assert out["branch_graph"]["graph_policy"] == batch_jobs.BRANCH_GRAPH_POLICY_ONE_HOP
    assert out["branch_graph"]["candidate_kind"] == batch_jobs.BRANCH_GRAPH_CANDIDATE_KIND
    assert out["branch_graph"]["chain_depth"] == 1
    assert out["branch_graph"]["active_target_graph_truth"] is False
    assert out["branch_graph"]["recompute_when_target_moves"] is True
    assert out["branch_graph"]["derives_from"]["base_commit"] == strategy.base_commit
    assert batch_jobs.branch_graph_plan(strategy)["overlay_path"] == out["branch_graph"]["overlay_path"]
    assert os.path.exists(out["branch_graph"]["snapshot_path"])
    assert os.path.exists(out["branch_graph"]["overlay_path"])
    with open(out["branch_graph"]["overlay_path"], encoding="utf-8") as f:
        overlay = json.load(f)
    assert overlay["graph_policy"] == batch_jobs.BRANCH_GRAPH_POLICY_ONE_HOP
    assert overlay["candidate_kind"] == batch_jobs.BRANCH_GRAPH_CANDIDATE_KIND
    assert overlay["chain_depth"] == 1
    assert overlay["active_target_graph_truth"] is False
    assert overlay["derives_from"]["base_commit"] == strategy.base_commit

    stale = batch_jobs.report_stale_worktrees(conn, "proj", repo_root_path=repo)
    assert stale["stale_count"] == 0

    batch_jobs.record_task_batch_state(conn, created["task_id"], "abandoned")
    stale_after = batch_jobs.report_stale_worktrees(conn, "proj", repo_root_path=repo)
    assert stale_after["stale_count"] == 1

    removed = batch_jobs.abandon_worktree(strategy, repo_root_path=repo, remove_branch=True)
    assert removed["removed"] is True
    assert not (repo / ".worktrees" / "batch-batch-007").exists()


def test_batch_merge_dry_run_records_ready_for_review(tmp_path):
    repo = _git_repo(tmp_path)
    _write_project_graph(repo)
    conn = _conn()
    created = batch_jobs.create_batch_task(
        conn,
        "proj",
        "merge dry run",
        repo_root_path=repo,
        batch_id="batch-008",
        base_commit=batch_jobs.git_commit(repo),
    )
    strategy = batch_jobs.BranchStrategy(**created["branch_strategy"])
    worktree = batch_jobs.create_worktree(strategy, repo_root_path=repo)
    batch_jobs.record_task_batch_state(
        conn,
        created["task_id"],
        "worktree_ready",
        evidence={"worktree": worktree},
    )

    result = batch_jobs.merge_batch_branch(
        conn,
        created["task_id"],
        repo_root_path=repo,
        dry_run=True,
    )

    assert result["dry_run"] is True
    assert result["merge_plan"]["work_branch"] == "codex/batch-batch-008"
    assert result["merge_plan"]["graph_gate"]["status"] == "pass"
    meta = _metadata(conn, created["task_id"])
    assert meta["batch_status"] == "ready_for_review"
    assert "merge_commit" not in meta


def test_batch_merge_requires_branch_graph_metadata(tmp_path):
    repo = _git_repo(tmp_path)
    conn = _conn()
    created = batch_jobs.create_batch_task(
        conn,
        "proj",
        "merge dry run",
        repo_root_path=repo,
        batch_id="batch-no-graph",
        base_commit=batch_jobs.git_commit(repo),
    )

    with pytest.raises(batch_jobs.BatchJobError, match="branch graph gate failed"):
        batch_jobs.merge_batch_branch(
            conn,
            created["task_id"],
            repo_root_path=repo,
            dry_run=True,
        )


def test_branch_graph_gate_rejects_uncovered_branch_files(tmp_path):
    repo = _git_repo(tmp_path)
    _write_project_graph(repo, files=["README.md"])
    conn = _conn()
    created = batch_jobs.create_batch_task(
        conn,
        "proj",
        "merge real",
        repo_root_path=repo,
        batch_id="batch-uncovered",
        base_commit=batch_jobs.git_commit(repo),
    )
    strategy = batch_jobs.BranchStrategy(**created["branch_strategy"])
    worktree = batch_jobs.create_worktree(strategy, repo_root_path=repo)
    batch_jobs.record_task_batch_state(
        conn,
        created["task_id"],
        "worktree_ready",
        evidence={"worktree": worktree},
    )
    wt = repo / ".worktrees" / "batch-batch-uncovered"
    (wt / "feature.txt").write_text("batch change\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-m", "batch change"], cwd=wt, check=True, capture_output=True, text=True)

    with pytest.raises(batch_jobs.BatchJobError, match="branch graph gate failed"):
        batch_jobs.merge_batch_branch(
            conn,
            created["task_id"],
            repo_root_path=repo,
            dry_run=True,
        )


def test_branch_graph_gate_preserves_hidden_file_paths(tmp_path):
    repo = _git_repo(tmp_path)
    conn = _conn()
    created = batch_jobs.create_batch_task(
        conn,
        "proj",
        "merge hidden file",
        repo_root_path=repo,
        batch_id="batch-hidden-file",
        base_commit=batch_jobs.git_commit(repo),
    )
    strategy = batch_jobs.BranchStrategy(**created["branch_strategy"])
    worktree = batch_jobs.create_worktree(strategy, repo_root_path=repo)
    batch_jobs.record_task_batch_state(
        conn,
        created["task_id"],
        "worktree_ready",
        evidence={"worktree": worktree},
    )
    _write_overlay_coverage(worktree["branch_graph"], [".env.example"])
    wt = repo / ".worktrees" / "batch-batch-hidden-file"
    (wt / ".env.example").write_text("KEY=value\n", encoding="utf-8")
    subprocess.run(["git", "add", ".env.example"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-m", "hidden file"], cwd=wt, check=True, capture_output=True, text=True)

    plan = batch_jobs.batch_merge_plan(conn, created["task_id"], repo_root_path=repo)

    assert ".env.example" in plan["graph_gate"]["changed_files"]
    assert ".env.example" in plan["graph_gate"]["overlay_covered_files"]


def test_batch_merge_uses_chain_trailer_helper(tmp_path):
    repo = _git_repo(tmp_path)
    conn = _conn()
    created = batch_jobs.create_batch_task(
        conn,
        "proj",
        "merge real",
        repo_root_path=repo,
        batch_id="batch-009",
        base_commit=batch_jobs.git_commit(repo),
        metadata={"bug_id": "OPT-BATCH-009"},
    )
    strategy = batch_jobs.BranchStrategy(**created["branch_strategy"])
    worktree = batch_jobs.create_worktree(strategy, repo_root_path=repo)
    batch_jobs.record_task_batch_state(
        conn,
        created["task_id"],
        "worktree_ready",
        evidence={"worktree": worktree},
    )
    _write_overlay_coverage(worktree["branch_graph"], ["feature.txt"])

    wt = repo / ".worktrees" / "batch-batch-009"
    (wt / "feature.txt").write_text("batch change\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.txt"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-m", "batch change"], cwd=wt, check=True, capture_output=True, text=True)

    merged = batch_jobs.merge_batch_branch(
        conn,
        created["task_id"],
        repo_root_path=repo,
        message="batch merge",
        dry_run=False,
    )

    assert merged["dry_run"] is False
    assert merged["merge_commit"]
    assert (repo / "feature.txt").read_text(encoding="utf-8") == "batch change\n"
    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"Chain-Source-Task: {created['task_id']}" in log
    assert "Chain-Source-Stage: merge" in log
    assert "Chain-Bug-Id: OPT-BATCH-009" in log
    meta = _metadata(conn, created["task_id"])
    assert meta["batch_status"] == "merged"
    assert meta["merge_commit"] == merged["merge_commit"]


def test_unsafe_worktree_path_rejected(tmp_path):
    repo = _git_repo(tmp_path)
    with pytest.raises(batch_jobs.BatchJobError):
        batch_jobs.ensure_worktree_path_safe(repo, tmp_path / "outside")
