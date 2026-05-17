from __future__ import annotations

import io
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent.governance import graph_correction_patches
from agent.governance import graph_events
from agent.governance import graph_snapshot_store as store
from agent.governance import reconcile_feedback
from agent.governance import reconcile_semantic_enrichment as semantic_enrichment
from agent.governance import server
from agent.governance.db import _ensure_schema
from agent.governance.errors import PermissionDeniedError, ValidationError
from agent.governance.governance_index import merge_feature_hashes_into_graph_nodes
from agent.governance.mf_subagent_contract import MfSubagentContractError
from agent.governance.parallel_branch_runtime import (
    BATCH_STATE_OPEN,
    BranchRuntimeFenceError,
    BranchTaskRuntimeContext,
    BatchMergeItem,
    BatchMergeRuntime,
    MergeQueueItem,
    upsert_batch_merge_runtime,
    upsert_branch_context,
    upsert_merge_queue_items,
)


PID = "graph-api-test"


class _NoCloseConn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


def _ctx(path_params: dict, *, method: str = "GET", query: dict | None = None, body: dict | None = None):
    return server.RequestContext(
        None,
        method,
        path_params,
        query or {},
        body or {},
        "req-test",
        "",
        "",
    )


def _ctx_with_role(
    path_params: dict,
    role: str,
    *,
    method: str = "GET",
    query: dict | None = None,
    body: dict | None = None,
):
    ctx = _ctx(path_params, method=method, query=query, body=body)
    ctx._session = {
        "session_id": f"ses-{role}",
        "principal_id": f"{role}-principal",
        "project_id": path_params.get("project_id", PID),
        "role": role,
        "scope": [],
    }
    return ctx


def _bare_handler():
    handler = object.__new__(server.GovernanceHandler)
    handler.path = "/api/health"
    handler.headers = {}
    handler.wfile = io.BytesIO()
    handler.requestline = "GET /api/health HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.command = "GET"
    handler.client_address = ("127.0.0.1", 0)
    handler.sent_statuses = []
    handler.sent_headers = []
    handler.send_response = lambda code: handler.sent_statuses.append(code)
    handler.send_header = lambda key, value: handler.sent_headers.append((key, value))
    handler.end_headers = lambda: None
    return handler


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


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    store.ensure_schema(c)
    monkeypatch.setattr(server, "get_connection", lambda _project_id: _NoCloseConn(c))
    yield c
    c.close()


def _graph(node_id: str = "L7.1") -> dict:
    return {
        "deps_graph": {
            "nodes": [
                {
                    "id": node_id,
                    "layer": "L7",
                    "title": "Feature Node",
                    "kind": "service_runtime",
                    "primary": ["agent/governance/server.py"],
                    "secondary": ["docs/dev/proposal.md"],
                    "test": ["agent/tests/test_graph_governance_api.py"],
                    "metadata": {"subsystem": "governance"},
                }
            ],
            "edges": [
                {
                    "source": node_id,
                    "target": "L3.1",
                    "edge_type": "contains",
                    "direction": "hierarchy",
                    "evidence": {"source": "test"},
                }
            ],
        }
    }


def _graph_with_dependency() -> dict:
    graph = _graph("L7.1")
    graph["deps_graph"]["nodes"].append(
        {
            "id": "L7.2",
            "layer": "L7",
            "title": "Dependency Node",
            "kind": "service_runtime",
            "primary": ["agent/governance/dependency.py"],
            "secondary": [],
            "test": [],
            "metadata": {"subsystem": "governance"},
        }
    )
    graph["deps_graph"]["edges"].append(
        {
            "source": "L7.1",
            "target": "L7.2",
            "edge_type": "depends_on",
            "direction": "dependency",
            "evidence": {"source": "test-dependency"},
        }
    )
    return graph


def test_parallel_branch_read_model_route_returns_durable_runtime_state(conn):
    batch_id = "PB-010-api"
    queue_id = "mergeq-PB010-api"
    target_ref = "refs/heads/main"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id=batch_id,
            task_id="T1",
            backlog_id="OPT-PB010-API",
            branch_ref="refs/heads/codex/PB010-api-T1",
            status="running",
            merge_queue_id=queue_id,
            checkpoint_id="checkpoint-T1",
            snapshot_id="scope-T1",
            projection_id="semproj-T1",
        ),
        now_iso="2026-05-17T06:20:00Z",
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PID,
                merge_queue_id=queue_id,
                queue_item_id="item-T1",
                task_id="T1",
                branch_ref="refs/heads/codex/PB010-api-T1",
                queue_index=1,
                status="merge_ready",
                target_ref=target_ref,
                merge_preview_id="preview-T1",
            )
        ],
        now_iso="2026-05-17T06:20:00Z",
    )
    upsert_batch_merge_runtime(
        conn,
        BatchMergeRuntime(
            project_id=PID,
            batch_id=batch_id,
            target_ref=target_ref,
            batch_base_commit="B0",
            current_target_head="B0",
            batch_status=BATCH_STATE_OPEN,
            items=(
                BatchMergeItem(
                    task_id="T1",
                    branch_ref="refs/heads/codex/PB010-api-T1",
                    worktree_path="/tmp/worktrees/PB010-api-T1",
                    queue_index=1,
                    status="merge_ready",
                    branch_head="H1",
                    base_commit="B0",
                    snapshot_id="scope-T1",
                    projection_id="semproj-T1",
                ),
            ),
        ),
        now_iso="2026-05-17T06:20:00Z",
    )

    result = server.handle_graph_governance_parallel_branches(
        _ctx(
            {"project_id": PID},
            query={
                "batch_id": batch_id,
                "merge_queue_id": queue_id,
                "target_ref": target_ref,
                "limit": "5",
            },
        )
    )

    assert result["ok"] is True
    payload = result["read_model"]
    assert payload["project_id"] == PID
    assert payload["batch_id"] == batch_id
    assert payload["summary"]["lane_count"] == 1
    assert payload["summary"]["mergeable_count"] == 1
    assert payload["branch_lanes"][0]["task_id"] == "T1"
    assert payload["merge_queue"]["rows"][0]["merge_preview_id"] == "preview-T1"
    assert payload["rollback"]["cleanup_allowed"] is False
    assert payload["truncated"] == {
        "branch_lanes": False,
        "merge_queue_rows": False,
        "rollback_rows": False,
    }


def test_parallel_branch_allocate_route_materializes_worktree_and_updates_read_model(conn, tmp_path):
    repo = _git_repo(tmp_path)

    status, created = server.handle_graph_governance_parallel_branch_allocate(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "API Branch Task",
                "batch_id": "PB-api-alloc",
                "backlog_id": "ARCH-PB-ALLOC",
                "workspace_root": str(repo),
                "worker_id": "worker api",
                "merge_queue_id": "mergeq-api-alloc",
                "create_worktree": True,
                "now_iso": "2026-05-17T07:10:00Z",
            },
        )
    )

    assert status == 201
    assert created["ok"] is True
    context = created["context"]
    assert context["status"] == "worktree_ready"
    assert context["branch_ref"] == "refs/heads/codex/api-branch-task"
    assert context["fence_token"].startswith("fence-")
    assert context["worktree_path"] == str(repo / ".worktrees" / "worker-api" / "api-branch-task")
    assert created["worktree"]["created"] is True
    assert created["worktree"]["branch_graph"]["status"] == "ready"

    checkpoint = server.handle_graph_governance_parallel_branch_checkpoint(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "API Branch Task",
                "checkpoint_id": "checkpoint-api-alloc",
                "fence_token": context["fence_token"],
                "now_iso": "2026-05-17T07:11:00Z",
            },
        )
    )
    assert checkpoint["context"]["checkpoint_id"] == "checkpoint-api-alloc"

    read = server.handle_graph_governance_parallel_branches(
        _ctx(
            {"project_id": PID},
            query={
                "batch_id": "PB-api-alloc",
                "merge_queue_id": "mergeq-api-alloc",
                "limit": "5",
            },
        )
    )
    lanes = read["read_model"]["branch_lanes"]
    assert len(lanes) == 1
    assert lanes[0]["task_id"] == "API Branch Task"
    assert lanes[0]["status"] == "worktree_ready"
    assert lanes[0]["worktree_path"] == context["worktree_path"]
    assert lanes[0]["graph_epoch"]["base_commit"]


def test_parallel_branch_recover_and_checkpoint_routes_enforce_fence(conn):
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id="PB-api-recover",
            task_id="recover-task",
            branch_ref="refs/heads/codex/recover-task",
            status="running",
            lease_id="lease-old",
            lease_expires_at="2026-05-17T07:00:00Z",
            fence_token="fence-old",
            checkpoint_id="checkpoint-old",
            replay_source="checkpoint",
        ),
        now_iso="2026-05-17T07:00:00Z",
    )

    recovered = server.handle_graph_governance_parallel_branch_recover_expired(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "now_iso": "2026-05-17T07:10:00Z",
                "actor": "observer-test",
            },
        )
    )

    assert recovered["recovered_count"] == 1
    context = recovered["contexts"][0]
    assert context["status"] == "reclaimable"
    assert context["attempt"] == 2
    assert context["fence_token"] != "fence-old"

    with pytest.raises(BranchRuntimeFenceError):
        server.handle_graph_governance_parallel_branch_checkpoint(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "task_id": "recover-task",
                    "checkpoint_id": "checkpoint-stale",
                    "fence_token": "fence-old",
                },
            )
        )

    checkpointed = server.handle_graph_governance_parallel_branch_checkpoint(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "recover-task",
                "checkpoint_id": "checkpoint-after-reclaim",
                "fence_token": context["fence_token"],
                "now_iso": "2026-05-17T07:11:00Z",
            },
        )
    )

    assert checkpointed["ok"] is True
    assert checkpointed["context"]["checkpoint_id"] == "checkpoint-after-reclaim"


def test_parallel_branch_merge_queue_route_enforces_fence_and_returns_decision(conn):
    queue_id = "mergeq-api-fenced"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id="PB-api-queue",
            task_id="queue-task",
            branch_ref="refs/heads/codex/queue-task",
            status="worktree_ready",
            fence_token="fence-queue-current",
            base_commit="base-queue",
            head_commit="head-queue",
            target_head_commit="target-queue",
            snapshot_id="scope-queue",
            projection_id="semproj-queue",
        ),
        now_iso="2026-05-17T07:20:00Z",
    )

    with pytest.raises(BranchRuntimeFenceError):
        server.handle_graph_governance_parallel_branch_merge_queue(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "task_id": "queue-task",
                    "merge_queue_id": queue_id,
                    "fence_token": "fence-stale",
                },
            )
        )

    queued = server.handle_graph_governance_parallel_branch_merge_queue(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "queue-task",
                "merge_queue_id": queue_id,
                "queue_index": 1,
                "fence_token": "fence-queue-current",
                "hard_depends_on": ["foundation-task"],
                "merge_preview_id": "preview-queue",
                "now_iso": "2026-05-17T07:21:00Z",
            },
        )
    )

    assert queued["ok"] is True
    assert queued["context"]["status"] == "queued_for_merge"
    assert queued["queue_item"]["merge_preview_id"] == "preview-queue"
    assert queued["decision"]["blocked_task_ids"] == ["queue-task"]
    row = queued["decision"]["rows"][0]
    assert row["dependency_blockers"] == ["foundation-task"]
    assert row["dependency_blocker_types"] == {"foundation-task": ["hard_depends_on"]}

    read = server.handle_graph_governance_parallel_branches(
        _ctx(
            {"project_id": PID},
            query={
                "batch_id": "PB-api-queue",
                "merge_queue_id": queue_id,
                "limit": "5",
            },
        )
    )
    assert read["read_model"]["merge_queue"]["blocked_task_ids"] == ["queue-task"]
    assert read["read_model"]["branch_lanes"][0]["merge_queue_id"] == queue_id


def test_parallel_branch_finish_gate_records_validated_checkpoint(conn):
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id="PB-api-finish",
            task_id="finish-task",
            backlog_id="FEAT-FINISH-GATE",
            branch_ref="refs/heads/codex/finish-task",
            status="worktree_ready",
            fence_token="fence-finish-current",
            worktree_path="/tmp/nonexistent-finish-task",
            base_commit="base-finish",
            head_commit="base-finish",
            target_head_commit="target-finish",
        ),
        now_iso="2026-05-17T07:30:00Z",
    )

    finished = server.handle_graph_governance_parallel_branch_finish_gate(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "project_id": PID,
                "task_id": "finish-task",
                "backlog_id": "FEAT-FINISH-GATE",
                "branch_ref": "refs/heads/codex/finish-task",
                "worktree_path": "/tmp/nonexistent-finish-task",
                "base_commit": "base-finish",
                "target_head_commit": "target-finish",
                "head_commit": "head-finish",
                "status": "succeeded",
                "changed_files": ["agent/governance/server.py"],
                "test_results": {"status": "passed", "command": "pytest -q"},
                "checkpoint_id": "ckpt-finish-gate",
                "fence_token": "fence-finish-current",
                "agent_id": "codex-subagent-1",
                "now_iso": "2026-05-17T07:31:00Z",
            },
        )
    )

    assert finished["ok"] is True
    assert finished["gate"]["checkpoint_id"] == "ckpt-finish-gate"
    assert finished["gate"]["validated_head_commit"] == "head-finish"
    assert finished["context"]["checkpoint_id"] == "ckpt-finish-gate"
    assert finished["context"]["replay_source"] == "mf_sub_finish_gate"
    assert finished["context"]["status"] == "validated"
    assert finished["context"]["head_commit"] == "head-finish"


def test_parallel_branch_finish_gate_accepts_mf_sub_session(conn):
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id="PB-api-finish-mf-sub",
            task_id="finish-mf-sub-task",
            backlog_id="FEAT-FINISH-GATE",
            branch_ref="refs/heads/codex/finish-mf-sub-task",
            status="worktree_ready",
            fence_token="fence-finish-mf-sub",
            worktree_path="/tmp/nonexistent-finish-mf-sub-task",
            base_commit="base-finish",
            head_commit="base-finish",
            target_head_commit="target-finish",
        ),
        now_iso="2026-05-17T07:30:00Z",
    )

    finished = server.handle_graph_governance_parallel_branch_finish_gate(
        _ctx_with_role(
            {"project_id": PID},
            "mf_sub",
            method="POST",
            body={
                "project_id": PID,
                "task_id": "finish-mf-sub-task",
                "status": "succeeded",
                "changed_files": ["agent/governance/server.py"],
                "test_results": {"status": "passed"},
                "checkpoint_id": "ckpt-finish-mf-sub",
                "fence_token": "fence-finish-mf-sub",
                "head_commit": "head-finish-mf-sub",
                "agent_id": "codex-subagent-mf-sub",
            },
        )
    )

    assert finished["ok"] is True
    assert finished["context"]["checkpoint_id"] == "ckpt-finish-mf-sub"
    assert finished["context"]["replay_source"] == "mf_sub_finish_gate"


def test_parallel_branch_finish_gate_rejects_stale_fence(conn):
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="finish-stale-task",
            backlog_id="FEAT-FINISH-GATE",
            branch_ref="refs/heads/codex/finish-stale-task",
            status="worktree_ready",
            fence_token="fence-current",
            worktree_path="/tmp/nonexistent-finish-stale-task",
            base_commit="base",
            target_head_commit="target",
        ),
        now_iso="2026-05-17T07:30:00Z",
    )

    with pytest.raises(MfSubagentContractError, match="stale"):
        server.handle_graph_governance_parallel_branch_finish_gate(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "task_id": "finish-stale-task",
                    "status": "succeeded",
                    "changed_files": ["agent/governance/server.py"],
                    "test_results": {"status": "passed"},
                    "checkpoint_id": "ckpt-stale",
                    "fence_token": "fence-old",
                },
            )
        )


def test_parallel_branch_finish_gate_validates_worktree_changed_files(conn, tmp_path):
    repo = _git_repo(tmp_path)
    base = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (repo / "README.md").write_text("# changed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "worker change"], cwd=repo, check=True, capture_output=True, text=True)
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id="PB-api-finish-diff",
            task_id="finish-diff-task",
            backlog_id="FEAT-FINISH-GATE",
            branch_ref="refs/heads/codex/finish-diff-task",
            status="worktree_ready",
            fence_token="fence-finish-diff",
            worktree_path=str(repo),
            base_commit=base,
            head_commit=base,
            target_head_commit=base,
        ),
        now_iso="2026-05-17T07:33:00Z",
    )

    with pytest.raises(ValidationError, match="changed_files do not match assigned worktree diff"):
        server.handle_graph_governance_parallel_branch_finish_gate(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "task_id": "finish-diff-task",
                    "status": "succeeded",
                    "changed_files": ["agent/governance/server.py"],
                    "test_results": {"status": "passed"},
                    "checkpoint_id": "ckpt-finish-diff-bad",
                    "fence_token": "fence-finish-diff",
                    "head_commit": head,
                },
            )
        )

    finished = server.handle_graph_governance_parallel_branch_finish_gate(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "finish-diff-task",
                "status": "succeeded",
                "changed_files": ["README.md"],
                "test_results": {"status": "passed"},
                "checkpoint_id": "ckpt-finish-diff",
                "fence_token": "fence-finish-diff",
                "head_commit": head,
            },
        )
    )

    assert finished["ok"] is True
    assert finished["gate"]["validated_changed_files"] == ["README.md"]
    assert finished["context"]["head_commit"] == head


def test_mf_sub_merge_queue_requires_finish_gate_checkpoint(conn):
    queue_id = "mergeq-api-finish-required"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="mf-sub-queue-task",
            backlog_id="FEAT-FINISH-GATE",
            branch_ref="refs/heads/codex/mf-sub-queue-task",
            status="worktree_ready",
            fence_token="fence-mf-sub",
            worktree_path="/tmp/nonexistent-mf-sub-queue-task",
            base_commit="base",
            head_commit="head",
            target_head_commit="target",
        ),
        now_iso="2026-05-17T07:32:00Z",
    )

    with pytest.raises(ValueError, match="checkpoint_id is required"):
        server.handle_graph_governance_parallel_branch_merge_queue(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "task_id": "mf-sub-queue-task",
                    "merge_queue_id": queue_id,
                    "worker_role": "mf_sub",
                    "fence_token": "fence-mf-sub",
                },
            )
        )

    server.handle_graph_governance_parallel_branch_finish_gate(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "mf-sub-queue-task",
                "status": "succeeded",
                "changed_files": ["agent/governance/server.py"],
                "test_results": {"status": "passed"},
                "checkpoint_id": "ckpt-mf-sub",
                "fence_token": "fence-mf-sub",
                "head_commit": "head",
            },
        )
    )

    queued = server.handle_graph_governance_parallel_branch_merge_queue(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "task_id": "mf-sub-queue-task",
                "merge_queue_id": queue_id,
                "worker_role": "mf_sub",
                "checkpoint_id": "ckpt-mf-sub",
                "fence_token": "fence-mf-sub",
            },
        )
    )

    assert queued["ok"] is True
    assert queued["context"]["status"] == "queued_for_merge"
    assert queued["context"]["checkpoint_id"] == "ckpt-mf-sub"


def test_mf_sub_session_cannot_enqueue_or_execute_merge(conn):
    queue_id = "mergeq-api-mf-sub-denied"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            task_id="mf-sub-denied-task",
            backlog_id="FEAT-FINISH-GATE",
            branch_ref="refs/heads/codex/mf-sub-denied-task",
            status="validated",
            fence_token="fence-mf-sub-denied",
            worktree_path="/tmp/nonexistent-mf-sub-denied-task",
            base_commit="base",
            head_commit="head",
            target_head_commit="target",
            checkpoint_id="ckpt-mf-sub-denied",
            replay_source="mf_sub_finish_gate",
        ),
        now_iso="2026-05-17T07:32:00Z",
    )

    with pytest.raises(PermissionDeniedError, match="merge-queue"):
        server.handle_graph_governance_parallel_branch_merge_queue(
            _ctx_with_role(
                {"project_id": PID},
                "mf_sub",
                method="POST",
                body={
                    "task_id": "mf-sub-denied-task",
                    "merge_queue_id": queue_id,
                    "worker_role": "mf_sub",
                    "checkpoint_id": "ckpt-mf-sub-denied",
                    "fence_token": "fence-mf-sub-denied",
                },
            )
        )

    with pytest.raises(PermissionDeniedError, match="merge-execute"):
        server.handle_graph_governance_parallel_branch_merge_execute(
            _ctx_with_role(
                {"project_id": PID},
                "mf_sub",
                method="POST",
                body={
                    "merge_queue_id": queue_id,
                    "target_ref": "main",
                    "task_id": "mf-sub-denied-task",
                    "evidence": {},
                    "dry_run": True,
                },
            )
        )


def test_parallel_branch_merge_gate_route_returns_dry_run_plan(conn):
    queue_id = "mergeq-api-gate"
    evidence = {
        "git_conflict_check": {"status": "pass", "evidence_id": "preview-api-gate"},
        "dirty_worktree_check": {"status": "pass"},
        "test_evidence": {"status": "pass"},
        "graph_currentness": {"status": "current"},
        "scope_reconcile": {"status": "pass"},
        "semantic_projection": {"status": "pass"},
        "backlog_acceptance": {"status": "satisfied"},
    }
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PID,
                merge_queue_id=queue_id,
                queue_item_id="item-gate-task",
                task_id="gate-task",
                branch_ref="refs/heads/codex/gate-task",
                queue_index=1,
                status="merge_ready",
                target_ref="refs/heads/main",
                branch_head="head-gate",
                validated_target_head="target-gate",
                current_target_head="target-gate",
                merge_preview_id="preview-api-gate",
                snapshot_id="scope-gate",
                projection_id="semproj-gate",
            )
        ],
        now_iso="2026-05-17T08:10:00Z",
    )

    result = server.handle_graph_governance_parallel_branch_merge_gate(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "merge_queue_id": queue_id,
                "task_id": "gate-task",
                "evidence": evidence,
            },
        )
    )

    assert result["ok"] is True
    plan = result["plan"]
    assert plan["dry_run"] is True
    assert plan["merge_gate_passed"] is True
    assert plan["merge_allowed"] is True
    assert plan["target_branch_mutation_allowed"] is False
    assert plan["target_graph_activation_allowed"] is False
    assert plan["next_actions"] == ["operator_approve_live_merge"]
    assert plan["merge_preview_id"] == "preview-api-gate"
    assert plan["evidence"][0]["key"] == "git_conflict_check"


def test_parallel_branch_merge_gate_route_blocks_batch_rollback(conn):
    queue_id = "mergeq-api-gate-blocked"
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PID,
                merge_queue_id=queue_id,
                queue_item_id="item-gate-blocked",
                task_id="gate-blocked",
                branch_ref="refs/heads/codex/gate-blocked",
                queue_index=1,
                status="merge_ready",
                target_ref="refs/heads/main",
                branch_head="head-gate-blocked",
                validated_target_head="target-gate",
                current_target_head="target-gate",
            )
        ],
        now_iso="2026-05-17T08:15:00Z",
    )

    result = server.handle_graph_governance_parallel_branch_merge_gate(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "merge_queue_id": queue_id,
                "task_id": "gate-blocked",
                "batch_status": "rollback_required",
            },
        )
    )

    assert result["plan"]["merge_gate_passed"] is False
    assert "batch_rollback_required" in result["plan"]["blocker_codes"]
    assert "missing_evidence:git_conflict_check" in result["plan"]["blocker_codes"]
    assert result["plan"]["target_branch_mutation_allowed"] is False


def test_parallel_branch_merge_preview_route_builds_gate_evidence(conn, tmp_path):
    repo = _git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "feature-preview"], cwd=repo, check=True)
    (repo / "preview.txt").write_text("preview\n", encoding="utf-8")
    subprocess.run(["git", "add", "preview.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "preview branch"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True, text=True)
    main_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    queue_id = "mergeq-api-preview"
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PID,
                merge_queue_id=queue_id,
                queue_item_id="item-preview-task",
                task_id="preview-task",
                branch_ref="feature-preview",
                queue_index=1,
                status="merge_ready",
                target_ref="main",
                branch_head="feature-preview",
                validated_target_head=main_head,
                current_target_head=main_head,
                merge_preview_id="preview-route",
                snapshot_id="scope-preview",
                projection_id="semproj-preview",
            )
        ],
        now_iso="2026-05-17T08:24:00Z",
    )

    result = server.handle_graph_governance_parallel_branch_merge_preview(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "repo_root_path": str(repo),
                "merge_queue_id": queue_id,
                "target_ref": "main",
                "task_id": "preview-task",
                "include_gate_plan": True,
                "evidence": {
                    "dirty_worktree_check": {"status": "pass"},
                    "test_evidence": {"status": "pass"},
                    "graph_currentness": {"status": "current"},
                    "scope_reconcile": {"status": "pass"},
                    "semantic_projection": {"status": "pass"},
                    "backlog_acceptance": {"status": "satisfied"},
                },
            },
        )
    )

    assert result["ok"] is True
    assert result["preview"]["status"] == "pass"
    assert result["preview"]["target_commit"] == main_head
    assert result["gate_plan"]["merge_gate_passed"] is True
    assert result["gate_plan"]["dry_run"] is True
    assert result["gate_plan"]["target_branch_mutation_allowed"] is False


def test_parallel_branch_merge_execute_route_dry_run_then_live_merge(conn, tmp_path):
    repo = _git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "feature-live"], cwd=repo, check=True)
    (repo / "live.txt").write_text("live\n", encoding="utf-8")
    subprocess.run(["git", "add", "live.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "live branch"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True, text=True)
    main_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    queue_id = "mergeq-api-execute"
    evidence = {
        "dirty_worktree_check": {"status": "pass"},
        "test_evidence": {"status": "pass"},
        "graph_currentness": {"status": "current"},
        "scope_reconcile": {"status": "pass"},
        "semantic_projection": {"status": "pass"},
        "backlog_acceptance": {"status": "satisfied"},
    }
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id="PB-api-execute",
            task_id="execute-task",
            branch_ref="feature-live",
            status="merge_ready",
            fence_token="fence-execute-current",
            target_head_commit=main_head,
            merge_queue_id=queue_id,
        ),
        now_iso="2026-05-17T08:28:00Z",
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PID,
                merge_queue_id=queue_id,
                queue_item_id="item-execute-task",
                task_id="execute-task",
                branch_ref="feature-live",
                queue_index=1,
                status="merge_ready",
                target_ref="main",
                branch_head="feature-live",
                validated_target_head=main_head,
                current_target_head=main_head,
                snapshot_id="scope-execute",
                projection_id="semproj-execute",
            )
        ],
        now_iso="2026-05-17T08:28:00Z",
    )

    dry_run = server.handle_graph_governance_parallel_branch_merge_execute(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "repo_root_path": str(repo),
                "merge_queue_id": queue_id,
                "target_ref": "main",
                "task_id": "execute-task",
                "evidence": evidence,
                "dry_run": True,
            },
        )
    )

    assert dry_run["ok"] is True
    assert dry_run["executed"] is False
    assert dry_run["gate_plan"]["merge_gate_passed"] is True
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == main_head

    live = server.handle_graph_governance_parallel_branch_merge_execute(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "repo_root_path": str(repo),
                "merge_queue_id": queue_id,
                "target_ref": "main",
                "task_id": "execute-task",
                "evidence": evidence,
                "dry_run": False,
                "allow_target_ref_mutation": True,
                "fence_token": "fence-execute-current",
                "message": "merge feature-live",
                "bug_id": "ARCH-PARALLEL-AGENT-MULTIBRANCH-EXECUTION",
                "now_iso": "2026-05-17T08:29:00Z",
            },
        )
    )

    assert live["ok"] is True
    assert live["executed"] is True
    assert live["merge_commit"]
    assert live["recorded"]["queue_item"]["status"] == "merged"
    assert live["recorded"]["context"]["status"] == "merged"
    assert live["decision"]["rows"][0]["queue_state"] == "merged"
    assert live["decision"]["rows"][0]["target_graph_activation_allowed"] is True
    assert (repo / "live.txt").read_text(encoding="utf-8") == "live\n"
    assert subprocess.run(
        ["git", "log", "-1", "--format=%B"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.find("Chain-Source-Stage: merge") != -1


def test_parallel_branch_merge_result_route_records_with_fence(conn):
    queue_id = "mergeq-api-result"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id="PB-api-result",
            task_id="result-task",
            branch_ref="refs/heads/codex/result-task",
            status="merge_ready",
            fence_token="fence-result-current",
            target_head_commit="target-before",
            merge_queue_id=queue_id,
            merge_preview_id="preview-result",
        ),
        now_iso="2026-05-17T08:25:00Z",
    )
    upsert_merge_queue_items(
        conn,
        [
            MergeQueueItem(
                project_id=PID,
                merge_queue_id=queue_id,
                queue_item_id="item-result-task",
                task_id="result-task",
                branch_ref="refs/heads/codex/result-task",
                queue_index=1,
                status="merge_ready",
                target_ref="refs/heads/main",
                branch_head="head-result",
                validated_target_head="target-before",
                current_target_head="target-before",
                merge_preview_id="preview-result",
                snapshot_id="scope-result",
                projection_id="semproj-result",
            )
        ],
        now_iso="2026-05-17T08:25:00Z",
    )

    with pytest.raises(BranchRuntimeFenceError):
        server.handle_graph_governance_parallel_branch_merge_result(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "merge_queue_id": queue_id,
                    "task_id": "result-task",
                    "status": "merged",
                    "merge_commit": "merge-result",
                    "target_head_after_merge": "target-after",
                    "fence_token": "fence-stale",
                },
            )
        )

    result = server.handle_graph_governance_parallel_branch_merge_result(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "merge_queue_id": queue_id,
                "task_id": "result-task",
                "status": "merged",
                "merge_commit": "merge-result",
                "target_head_before_merge": "target-before",
                "target_head_after_merge": "target-after",
                "fence_token": "fence-result-current",
                "now_iso": "2026-05-17T08:26:00Z",
            },
        )
    )

    assert result["ok"] is True
    assert result["queue_item"]["status"] == "merged"
    assert result["queue_item"]["merge_commit"] == "merge-result"
    assert result["context"]["status"] == "merged"
    assert result["context"]["target_head_commit"] == "target-after"
    row = result["decision"]["rows"][0]
    assert row["queue_state"] == "merged"
    assert row["target_graph_activation_allowed"] is True


def test_parallel_branch_batch_runtime_route_returns_rollback_plan(conn):
    batch_id = "PB-api-batch"
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id=batch_id,
            task_id="T1",
            branch_ref="refs/heads/codex/batch-t1",
            worktree_path="/repo/.worktrees/batch-t1",
            status="merged",
            base_commit="base-batch",
            head_commit="head-T1",
            snapshot_id="scope-T1",
            projection_id="semproj-T1",
            merge_queue_id="mergeq-batch",
        ),
        now_iso="2026-05-17T07:30:00Z",
    )
    upsert_branch_context(
        conn,
        BranchTaskRuntimeContext(
            project_id=PID,
            batch_id=batch_id,
            task_id="T2",
            branch_ref="refs/heads/codex/batch-t2",
            worktree_path="/repo/.worktrees/batch-t2",
            status="merge_failed",
            base_commit="base-batch",
            head_commit="head-T2",
            snapshot_id="scope-T2",
            projection_id="semproj-T2",
            merge_queue_id="mergeq-batch",
            depends_on=("T1",),
        ),
        now_iso="2026-05-17T07:30:00Z",
    )

    result = server.handle_graph_governance_parallel_branch_batch_runtime(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "batch_id": batch_id,
                "target_ref": "refs/heads/main",
                "batch_base_commit": "base-batch",
                "current_target_head": "bad-target",
                "severe_integration_failure": True,
                "corrected_replay_order": ["T2", "T1"],
                "failure_reason": "wrong merge order",
                "items": [
                    {"task_id": "T1", "queue_index": 1, "merge_commit": "merge-T1"},
                    {"task_id": "T2", "queue_index": 2},
                ],
                "now_iso": "2026-05-17T07:31:00Z",
            },
        )
    )

    assert result["ok"] is True
    assert result["runtime"]["batch_status"] == "rollback_required"
    assert result["plan"]["rollback_required"] is True
    assert result["plan"]["rollback_target_commit"] == "base-batch"
    assert result["plan"]["retained_branch_refs"] == [
        "refs/heads/codex/batch-t1",
        "refs/heads/codex/batch-t2",
    ]
    assert result["plan"]["replay_task_ids"] == ["T2", "T1"]
    assert result["plan"]["cleanup_allowed"] is False
    assert result["plan"]["cleanup_blockers"] == ["T1", "T2"]

    read = server.handle_graph_governance_parallel_branches(
        _ctx(
            {"project_id": PID},
            query={
                "batch_id": batch_id,
                "merge_queue_id": "mergeq-batch",
                "corrected_replay_order": "T2,T1",
                "limit": "5",
            },
        )
    )

    assert read["read_model"]["rollback"]["rollback_required"] is True
    assert read["read_model"]["rollback"]["replay_task_ids"] == ["T2", "T1"]
    assert read["read_model"]["rollback"]["cleanup_blockers"] == ["T1", "T2"]


def test_governance_handler_json_response_includes_dev_cors_headers():
    handler = _bare_handler()

    handler._respond(200, {"ok": True})

    headers = dict(handler.sent_headers)
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert "GET" in headers["Access-Control-Allow-Methods"]
    assert "POST" in headers["Access-Control-Allow-Methods"]
    assert "OPTIONS" in headers["Access-Control-Allow-Methods"]
    assert "Content-Type" in headers["Access-Control-Allow-Headers"]
    assert "X-Gov-Token" in headers["Access-Control-Allow-Headers"]


def test_governance_handler_options_preflight_includes_dev_cors_headers():
    handler = _bare_handler()

    handler.do_OPTIONS()

    headers = dict(handler.sent_headers)
    assert handler.sent_statuses == [204]
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert "OPTIONS" in headers["Access-Control-Allow-Methods"]
    assert headers["Access-Control-Max-Age"] == "86400"
    assert headers["Content-Length"] == "0"


def test_graph_governance_status_and_snapshot_query_api(conn):
    old = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="imported-old",
        commit_sha="old",
        snapshot_kind="imported",
    )
    store.activate_graph_snapshot(conn, PID, old["snapshot_id"])
    candidate = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-head",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        candidate["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="head",
        parent_commit_sha="old",
    )
    conn.commit()

    status = server.handle_graph_governance_status(
        _ctx({"project_id": PID}, query={"target_commit": "head"})
    )
    assert status["ok"] is True
    assert status["active_snapshot_id"] == "imported-old"
    assert status["pending_scope_reconcile_count"] == 1
    assert status["strict_ready"]["ok"] is False

    snapshots = server.handle_graph_governance_snapshot_list(
        _ctx({"project_id": PID}, query={"status": "candidate,active"})
    )
    assert snapshots["count"] == 2
    assert {row["snapshot_id"] for row in snapshots["snapshots"]} == {"imported-old", "full-head"}

    nodes = server.handle_graph_governance_snapshot_nodes(
        _ctx({"project_id": PID, "snapshot_id": "full-head"})
    )
    assert nodes["count"] == 1
    assert nodes["nodes"][0]["primary_files"] == ["agent/governance/server.py"]
    assert nodes["nodes"][0]["metadata"]["subsystem"] == "governance"

    edges = server.handle_graph_governance_snapshot_edges(
        _ctx({"project_id": PID, "snapshot_id": "full-head"})
    )
    assert edges["count"] == 1
    assert edges["edges"][0]["edge_type"] == "contains"


def test_graph_governance_correction_patch_api_lifecycle(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )

    created = server.handle_graph_governance_correction_patch_create(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "patch_id": "patch-api-package-marker",
                "patch_type": "mark_package_marker",
                "target_node_id": "L7.1",
                "patch_json": {"target_node_id": "L7.1"},
                "evidence": {"reason": "empty package initializer"},
                "actor": "observer",
            },
        )
    )
    status, payload = created
    assert status == 201
    assert payload["patch_id"] == "patch-api-package-marker"

    listed = server.handle_graph_governance_correction_patch_list(
        _ctx({"project_id": PID}, query={"status": "proposed"})
    )
    assert listed["count"] == 1
    assert listed["patches"][0]["patch_json"]["target_node_id"] == "L7.1"

    accepted = server.handle_graph_governance_correction_patch_accept(
        _ctx(
            {"project_id": PID, "patch_id": "patch-api-package-marker"},
            method="POST",
            body={"actor": "observer"},
        )
    )
    assert accepted["status"] == "accepted"

    listed = server.handle_graph_governance_correction_patch_list(
        _ctx({"project_id": PID}, query={"status": "accepted"})
    )
    assert listed["count"] == 1
    assert listed["patches"][0]["status"] == "accepted"


def test_feedback_decision_accept_graph_correction_creates_patch(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-feedback-decision",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    from agent.governance import reconcile_feedback

    classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        source_round="round-001",
        created_by="semantic-ai",
        issues=[
            {
                "node_id": "L7.1",
                "reason": "dependency_patch_suggestions",
                "type": "add_relation",
                "target": "L7.2",
                "edge_type": "depends_on",
                "summary": "L7.1 depends on L7.2",
            }
        ],
    )
    feedback_id = classified["items"][0]["feedback_id"]

    decided = server.handle_graph_governance_snapshot_feedback_decision(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "feedback_id": feedback_id,
                "action": "accept_graph_correction",
                "actor": "observer",
            },
        )
    )

    assert decided["decided_count"] == 1
    assert decided["graph_patches"]["created_count"] == 1
    assert decided["graph_patches"]["patches"][0]["status"] == "accepted"

    listed = server.handle_graph_governance_correction_patch_list(
        _ctx({"project_id": PID}, query={"status": "accepted"})
    )
    assert listed["count"] == 1
    assert listed["patches"][0]["patch_json"]["edge"]["dst"] == "L7.2"


def test_graph_governance_active_alias_resolves_for_nodes_and_edges(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-active-alias",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()

    nodes = server.handle_graph_governance_snapshot_nodes(
        _ctx({"project_id": PID, "snapshot_id": "active"})
    )
    edges = server.handle_graph_governance_snapshot_edges(
        _ctx({"project_id": PID, "snapshot_id": "active"})
    )

    assert nodes["snapshot_id"] == "full-active-alias"
    assert nodes["count"] == 1
    assert edges["snapshot_id"] == "full-active-alias"
    assert edges["count"] == 1


def test_graph_governance_snapshot_nodes_include_semantic_overlay(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-nodes",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    semantic_enrichment._ensure_semantic_state_schema(conn)
    semantic_payload = {
        "feature_name": "Governance Server Feature",
        "domain_label": "governance/api",
        "intent": "Expose graph-governance HTTP routes for dashboard users.",
        "doc_status": "adequate",
        "test_status": "adequate",
        "config_status": "n/a",
        "quality_flags": [],
    }
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           semantic_json, feedback_round, batch_index, updated_at)
        VALUES (?, ?, 'L7.1', 'ai_complete', 'sha256:feature',
                '{"agent/governance/server.py":"sha256:file"}', ?, 2, 7, '2026-05-09T20:31:24Z')
        """,
        (PID, snapshot["snapshot_id"], json.dumps(semantic_payload)),
    )
    conn.execute(
        """
        INSERT INTO graph_semantic_jobs
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           feedback_round, batch_index, attempt_count, updated_at, created_at)
        VALUES (?, ?, 'L7.1', 'ai_complete', 'sha256:feature',
                '{"agent/governance/server.py":"sha256:file"}', 2, 7, 1,
                '2026-05-09T20:31:24Z', '2026-05-09T20:00:00Z')
        """,
        (PID, snapshot["snapshot_id"]),
    )
    conn.commit()

    nodes = server.handle_graph_governance_snapshot_nodes(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"]})
    )

    semantic = nodes["nodes"][0]["semantic"]
    assert semantic["status"] == "ai_complete"
    assert semantic["node_status"] == "ai_complete"
    assert semantic["job_status"] == "ai_complete"
    assert semantic["hash_state"] == "current"
    assert semantic["has_semantic_payload"] is True
    assert semantic["feature_name"] == "Governance Server Feature"
    assert semantic["domain_label"] == "governance/api"
    assert semantic["file_hashes"]["agent/governance/server.py"] == "sha256:file"
    assert semantic["job"]["attempt_count"] == 1

    structure_only = server.handle_graph_governance_snapshot_nodes(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"include_semantic": "false"},
        )
    )
    assert "semantic" not in structure_only["nodes"][0]


def test_graph_governance_snapshot_nodes_normalize_pending_review_overlay(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-pending-review-overlay",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    semantic_enrichment._ensure_semantic_state_schema(conn)
    semantic_payload = {
        "feature_name": "Pending Review Feature",
        "semantic_summary": "Generated by AI but not approved yet.",
    }
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           semantic_json, feedback_round, batch_index, updated_at)
        VALUES (?, ?, 'L7.1', 'pending_review', 'sha256:feature',
                '{"agent/governance/server.py":"sha256:file"}', ?, 2, 7, '2026-05-09T20:31:24Z')
        """,
        (PID, snapshot["snapshot_id"], json.dumps(semantic_payload)),
    )
    conn.commit()

    nodes = server.handle_graph_governance_snapshot_nodes(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"]})
    )

    semantic = nodes["nodes"][0]["semantic"]
    assert semantic["status"] == "review_pending"
    assert semantic["node_status"] == "pending_review"
    assert semantic["hash_state"] == "pending"
    assert semantic["has_semantic_payload"] is True


def test_graph_governance_snapshot_nodes_do_not_treat_completed_job_as_semantic(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-job-only-overlay",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    semantic_enrichment._ensure_semantic_state_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_semantic_jobs
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           feedback_round, batch_index, attempt_count, updated_at, created_at)
        VALUES (?, ?, 'L7.1', 'ai_complete', 'sha256:job-only',
                '{"agent/governance/server.py":"sha256:file"}', 2, 7, 1,
                '2026-05-09T20:31:24Z', '2026-05-09T20:00:00Z')
        """,
        (PID, snapshot["snapshot_id"]),
    )
    conn.commit()

    nodes = server.handle_graph_governance_snapshot_nodes(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"]})
    )

    semantic = nodes["nodes"][0]["semantic"]
    assert semantic["status"] == "structure_only"
    assert semantic["node_status"] == ""
    assert semantic["job_status"] == "ai_complete"
    assert semantic["feature_hash"] == ""
    assert semantic["hash_state"] == "unknown"
    assert semantic["has_semantic_payload"] is False
    assert semantic["job"]["feature_hash"] == "sha256:job-only"


def test_graph_governance_semantic_jobs_endpoint_enqueues_existing_semantic_jobs(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    from agent.governance import event_bus

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(event_bus, "publish", lambda event, payload: published.append((event, payload)))
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-jobs-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    conn.commit()

    created = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(tmp_path),
                "target_scope": "node",
                "target_ids": ["L7.1"],
                "options": {"skip_current": False},
                "created_by": "dashboard_user",
            },
        )
    )

    status, payload = created
    assert status == 202
    assert payload["ok"] is True
    assert payload["status"] == "queued"
    assert payload["summary"]["by_status"]["ai_pending"] == 1
    assert payload["summary"]["progress"]["open"] == 1
    assert payload["operator_request"]["requested_by"] == "dashboard_user"
    assert payload["operator_request"]["query_source"] == "dashboard"
    assert payload["operator_request"]["analyzer"]["model"]
    assert payload["batch_plan"]["target_scope"] == "node"
    assert payload["batch_plan"]["target_ids"] == ["L7.1"]
    assert published == [
        (
            "semantic_job.enqueued",
            {
                "project_id": PID,
                "snapshot_id": snapshot["snapshot_id"],
                "queued_count": 1,
                "target_scope": "node",
                "source": "semantic_jobs_create_api",
            },
        )
    ]

    listed = server.handle_graph_governance_snapshot_semantic_jobs_list(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"status": "ai_pending"},
        )
    )
    assert listed["count"] == 1
    assert listed["summary"]["progress"]["pending"] == 1
    assert listed["jobs"][0]["node_id"] == "L7.1"
    assert listed["jobs"][0]["status"] == "ai_pending"
    assert listed["jobs"][0]["job_id"] == "L7.1"
    fetched = server.handle_graph_governance_snapshot_semantic_job_get(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"], "job_id": "L7.1"})
    )
    assert fetched["job"]["status"] == "ai_pending"
    cancelled = server.handle_graph_governance_snapshot_semantic_job_cancel(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"], "job_id": "L7.1"},
            method="POST",
            body={"actor": "dashboard_user"},
        )
    )
    assert cancelled["job"]["status"] == "cancelled"
    retried = server.handle_graph_governance_snapshot_semantic_job_retry(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"], "job_id": "L7.1"},
            method="POST",
            body={"actor": "dashboard_user"},
        )
    )
    assert retried["job"]["status"] == "pending_ai"
    events = server.handle_graph_governance_snapshot_events_list(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"event_type": "semantic_retry_requested"},
        )
    )
    assert events["count"] == 2
    assert events["events"][0]["target_type"] == "node"
    assert events["events"][0]["target_id"] == "L7.1"
    assert events["events"][0]["payload"]["operator_request"]["requested_by"] == "dashboard_user"
    assert events["events"][0]["payload"]["batch_plan"]["target_ids"] == ["L7.1"]


def test_semantic_jobs_operator_request_uses_project_ai_routing(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    monkeypatch.setattr(
        server.project_service,
        "get_project_config_metadata",
        lambda project_id: {
            "project_id": project_id,
            "ai": {
                "routing": {
                    "semantic": {"provider": "openai", "model": "gpt-5.5"}
                }
            },
        },
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="semantic-jobs-project-routing",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    conn.commit()

    status, payload = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(tmp_path),
                "target_scope": "node",
                "target_ids": ["L7.1"],
                "options": {"skip_current": False},
                "created_by": "dashboard_user",
            },
        )
    )

    assert status == 202
    analyzer = payload["operator_request"]["analyzer"]
    assert analyzer["provider"] == "openai"
    assert analyzer["model"] == "gpt-5.5"
    assert "ai.routing.semantic" in analyzer["override_path"]


def test_semantic_jobs_requires_project_route_when_registry_config_exists(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    monkeypatch.setattr(
        server.project_service,
        "get_project_config_metadata",
        lambda project_id: {"project_id": project_id, "ai": {"routing": {}}},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="semantic-jobs-missing-project-routing",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    conn.commit()

    with pytest.raises(ValidationError, match="AI enrich blocked"):
        server.handle_graph_governance_snapshot_semantic_jobs_create(
            _ctx(
                {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
                method="POST",
                body={
                    "project_root": str(tmp_path),
                    "target_scope": "node",
                    "target_ids": ["L7.1"],
                    "options": {"skip_current": False},
                    "created_by": "dashboard_user",
                },
            )
        )


def test_graph_governance_semantic_jobs_endpoint_records_edge_requests_as_events(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-edge-jobs-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()

    status, payload = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(tmp_path),
                "target_scope": "edge",
                "edges": [{"src": "L7.1", "dst": "L3.1", "edge_type": "contains"}],
            },
        )
    )

    assert status == 202
    assert payload["target_scope"] == "edge"
    assert payload["queued_count"] == 1
    assert payload["operator_request"]["query_source"] == "dashboard"
    assert payload["batch_plan"]["target_scope"] == "edge"
    assert payload["events"][0]["event_type"] == "edge_semantic_requested"
    assert payload["events"][0]["target_id"] == "L7.1->L3.1:contains"
    assert payload["events"][0]["payload"]["operator_request"]["batch_plan"]["target_scope"] == "edge"
    assert payload["events"][0]["payload"]["edge_context"]["edge_id"] == "L7.1->L3.1:contains"


def test_semantic_jobs_edge_targets_hydrates_edge_dict_when_only_target_ids_given(conn):
    """Regression for MF 2026-05-11 / BACKLOG-EDGE-AI-ENRICH-BROKEN bug 1.

    Dashboard sends `target_ids: ["<src>|<dst>|<type>"]` with no `edges` array
    and no `all_eligible: true`. Previously the backend created
    {"edge_id": ..., "edge": {}} — an empty edge dict — and the downstream
    event payload's `edge_context.src/dst/edge_type/evidence` were all empty
    strings, causing the AI to reply risk=insufficient_context. The fix is
    to look up the matching edge in the snapshot and hydrate the dict.
    """
    graph = _graph_with_dependency()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="edge-targets-hydration",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    conn.commit()

    # Dashboard sends the pipe-form edge_id. Arrow-form should also work.
    rows = server._semantic_jobs_edge_targets(
        {"target_ids": ["L7.1|L7.2|depends_on"]},
        conn=conn,
        project_id=PID,
        snapshot_id=snapshot["snapshot_id"],
    )
    assert len(rows) == 1
    edge = rows[0]["edge"]
    # Snapshot edges normalize src/dst into `src`/`dst` keys (not source/target).
    assert (edge.get("src") or edge.get("source")) == "L7.1"
    assert (edge.get("dst") or edge.get("target")) == "L7.2"
    assert (edge.get("edge_type") or edge.get("type")) == "depends_on"

    rows_arrow = server._semantic_jobs_edge_targets(
        {"target_ids": ["L7.1->L7.2:depends_on"]},
        conn=conn,
        project_id=PID,
        snapshot_id=snapshot["snapshot_id"],
    )
    assert len(rows_arrow) == 1
    assert (rows_arrow[0]["edge"].get("src") or rows_arrow[0]["edge"].get("source")) == "L7.1"

    # Unknown edge_id should fall through to {} (graceful, not a crash).
    rows_missing = server._semantic_jobs_edge_targets(
        {"target_ids": ["L7.99|L7.999|nonexistent"]},
        conn=conn,
        project_id=PID,
        snapshot_id=snapshot["snapshot_id"],
    )
    assert rows_missing == [{"edge_id": "L7.99|L7.999|nonexistent", "edge": {}}]


def test_semantic_jobs_endpoint_populates_edge_context_when_only_target_ids_given(conn, tmp_path, monkeypatch):
    """End-to-end version of the bug-1 fix: confirm the graph_events row
    emitted by /semantic/jobs has a non-empty edge_context."""
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph_with_dependency()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="edge-context-hydrated",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    conn.commit()

    status, payload = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(tmp_path),
                "target_scope": "edge",
                "target_ids": ["L7.1|L7.2|depends_on"],
            },
        )
    )
    assert status == 202
    assert payload["queued_count"] == 1
    edge_context = payload["events"][0]["payload"]["edge_context"]
    assert edge_context["src"] == "L7.1"
    assert edge_context["dst"] == "L7.2"
    assert edge_context["edge_type"] == "depends_on"
    # evidence should also flow through from the snapshot edge row when
    # present (the fixture sets it to {"source": "test-dependency"}).
    assert edge_context["evidence"] == {"source": "test-dependency"}


def test_semantic_job_cancel_routes_edge_job_to_graph_events(conn, tmp_path, monkeypatch):
    """Regression for MF 2026-05-11 / BACKLOG-EDGE-AI-ENRICH-BROKEN bug 3.

    Edge jobs live in graph_events, not graph_semantic_jobs. The cancel
    endpoint used to look up the job_id in graph_semantic_jobs only and
    raise ValidationError when not found — operator clicks Cancel on an
    edge job and gets 500. Now the endpoint detects edge-shaped job_id
    (parseable as `<src>|<dst>|<type>` or arrow form) and updates the
    matching graph_events row to status=stale.
    """
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph_with_dependency()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="edge-cancel-test",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    conn.commit()
    # Submit the edge job via the public endpoint first.
    status, _payload = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(tmp_path),
                "target_scope": "edge",
                "target_ids": ["L7.1|L7.2|depends_on"],
            },
        )
    )
    assert status == 202
    # Now cancel it — dashboard passes the edge_id as job_id.
    result = server.handle_graph_governance_snapshot_semantic_job_cancel(
        _ctx(
            {
                "project_id": PID,
                "snapshot_id": snapshot["snapshot_id"],
                "job_id": "L7.1|L7.2|depends_on",
            },
            method="POST",
            body={"actor": "dashboard_user"},
        )
    )
    assert result["ok"] is True
    assert result["job"]["target_scope"] == "edge"
    assert result["job"]["edge_id"] == "L7.1|L7.2|depends_on"
    # dashboard-facing status comes from _edge_semantic_job_status, which
    # surfaces 'rejected' for an operator-cancelled edge event (main MF
    # 2026-05-10-011 split stale=auto-supersede from rejected=manual cancel;
    # the dashboard test was originally written against the older 'stale'
    # contract and is updated here to match main's semantics).
    assert result["job"]["status"] == "rejected"
    assert result["event"]["status"] == graph_events.EVENT_STATUS_REJECTED
    # And the counts now reflect the cancellation.
    assert result["summary"]["by_status"].get("rejected", 0) >= 1


def test_graph_governance_edge_semantic_projection_tracks_requested_and_enriched_edges(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph_with_dependency()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-edge-projection",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    conn.commit()

    status, payload = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(tmp_path),
                "target_scope": "edge",
                "selector": {"all_eligible": True, "edge_types": ["depends_on"], "limit": 10},
                "actor": "dashboard_user",
            },
        )
    )

    assert status == 202
    assert payload["queued_count"] == 1
    assert payload["batch_plan"]["target_ids"] == ["L7.1->L7.2:depends_on"]
    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-edge-requested"},
        )
    )
    edge_semantic = projected["projection"]["edge_semantics"]["L7.1->L7.2:depends_on"]
    assert edge_semantic["validity"]["status"] == "edge_semantic_requested"
    assert projected["health"]["edge_semantic_eligible_count"] == 1
    assert projected["health"]["edge_semantic_requested_count"] == 1
    assert projected["health"]["edge_semantic_current_count"] == 0
    edge_jobs = server.handle_graph_governance_snapshot_semantic_jobs_list(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"target_scope": "edge"},
        )
    )
    assert edge_jobs["target_scope"] == "edge"
    assert edge_jobs["count"] == 1
    assert edge_jobs["jobs"][0]["edge_id"] == "L7.1->L7.2:depends_on"
    assert edge_jobs["jobs"][0]["status"] == "ai_pending"
    assert edge_jobs["summary"]["by_status"] == {"ai_pending": 1}

    status, enriched = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(tmp_path),
                "target_scope": "edge",
                "edges": [{"src": "L7.1", "dst": "L7.2", "edge_type": "depends_on"}],
                "edge_semantics": [
                    {
                        "src": "L7.1",
                        "dst": "L7.2",
                        "edge_type": "depends_on",
                        "relation_purpose": "Feature Node calls Dependency Node.",
                        "confidence": 0.9,
                    }
                ],
                "actor": "semantic-ai",
            },
        )
    )
    assert status == 202
    assert enriched["events"][0]["event_type"] == "edge_semantic_enriched"
    assert enriched["events"][0]["status"] == graph_events.EVENT_STATUS_PROPOSED

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-edge-enriched"},
        )
    )
    edge_semantic = projected["projection"]["edge_semantics"]["L7.1->L7.2:depends_on"]
    assert edge_semantic["validity"]["status"] == "edge_semantic_requested"
    assert projected["health"]["edge_semantic_current_count"] == 0
    assert projected["health"]["edge_semantic_coverage_ratio"] == 0.0
    edge_jobs = server.handle_graph_governance_snapshot_semantic_jobs_list(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"target_scope": "edge"},
        )
    )
    assert edge_jobs["count"] == 1
    assert edge_jobs["jobs"][0]["status"] == "pending_review"
    assert edge_jobs["jobs"][0]["semantic"]["relation_purpose"] == "Feature Node calls Dependency Node."

    feedback_items = reconcile_feedback.list_feedback_items(PID, snapshot["snapshot_id"])
    edge_id_variants = set(server._semantic_edge_id_variants("L7.1|L7.2|depends_on"))
    edge_feedback = [
        item for item in feedback_items
        if item.get("target_type") == "edge" and item.get("target_id") in edge_id_variants
    ]
    assert edge_feedback, "inline edge semantic proposal must create review feedback"
    decision = server.handle_graph_governance_snapshot_feedback_decision(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "actor": "observer",
                "feedback_ids": [edge_feedback[0]["feedback_id"]],
                "action": "accept_semantic_enrichment",
            },
        )
    )
    assert decision["semantic_enrichment_accepted"]["edge_ids_flipped"] == ["L7.1->L7.2:depends_on"]

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-edge-accepted"},
        )
    )
    edge_semantic = projected["projection"]["edge_semantics"]["L7.1->L7.2:depends_on"]
    assert edge_semantic["validity"]["status"] == "edge_semantic_current"
    assert edge_semantic["semantic"]["relation_purpose"] == "Feature Node calls Dependency Node."
    assert projected["health"]["edge_semantic_current_count"] == 1
    assert projected["health"]["edge_semantic_coverage_ratio"] == 1.0

    summary = server.handle_graph_governance_snapshot_summary(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"]})
    )
    semantic_health = summary["health"]["semantic_health"]
    assert semantic_health["edge_semantic_eligible_count"] == 1
    assert semantic_health["edge_semantic_current_count"] == 1
    assert semantic_health["edge_semantic_requested_count"] == 0
    assert semantic_health["edge_semantic_missing_count"] == 0
    assert semantic_health["edge_semantic_coverage_ratio"] == 1.0


def test_graph_governance_edge_semantic_inline_reject_stays_unprojected(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph_with_dependency()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-edge-inline-reject",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    conn.commit()

    status, enriched = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(tmp_path),
                "target_scope": "edge",
                "edges": [{"src": "L7.1", "dst": "L7.2", "edge_type": "depends_on"}],
                "edge_semantics": [
                    {
                        "src": "L7.1",
                        "dst": "L7.2",
                        "edge_type": "depends_on",
                        "relation_purpose": "Rejected payload must not become current.",
                        "confidence": 0.9,
                    }
                ],
                "actor": "semantic-ai",
            },
        )
    )
    assert status == 202
    event_id = enriched["events"][0]["event_id"]
    assert enriched["events"][0]["status"] == graph_events.EVENT_STATUS_PROPOSED

    feedback_items = reconcile_feedback.list_feedback_items(PID, snapshot["snapshot_id"])
    edge_feedback = [
        item for item in feedback_items
        if item.get("target_type") == "edge" and item.get("target_id") == "L7.1->L7.2:depends_on"
    ]
    assert edge_feedback
    decision = server.handle_graph_governance_snapshot_feedback_decision(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "actor": "observer",
                "feedback_ids": [edge_feedback[0]["feedback_id"]],
                "action": "reject_false_positive",
            },
        )
    )
    assert decision["semantic_enrichment_rejected"]["edge_ids_cleared"] == ["L7.1->L7.2:depends_on"]
    event = graph_events.get_event(conn, PID, snapshot["snapshot_id"], event_id)
    assert event["status"] == graph_events.EVENT_STATUS_REJECTED
    pending_edges = conn.execute(
        """
        SELECT COUNT(*) AS count FROM graph_semantic_edges
        WHERE project_id=? AND snapshot_id=? AND edge_id=?
        """,
        (PID, snapshot["snapshot_id"], "L7.1->L7.2:depends_on"),
    ).fetchone()
    assert pending_edges["count"] == 0

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-edge-inline-reject"},
        )
    )
    edge_semantic = projected["projection"]["edge_semantics"]["L7.1->L7.2:depends_on"]
    assert edge_semantic["validity"]["status"] == "edge_semantic_missing"
    assert projected["health"]["edge_semantic_current_count"] == 0
    assert projected["health"]["edge_semantic_missing_count"] == 1


def test_edge_semantic_projection_accepts_dashboard_pipe_edge_ids(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph_with_dependency()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-edge-pipe-id",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    conn.commit()

    graph_events.create_event(
        conn,
        PID,
        snapshot["snapshot_id"],
        event_type="edge_semantic_enriched",
        event_kind="semantic_job",
        target_type="edge",
        target_id="L7.1|L7.2|depends_on",
        status=graph_events.EVENT_STATUS_OBSERVED,
        payload={
            "semantic_payload": {
                "relation_purpose": "Dashboard pipe id enriches the dependency.",
                "confidence": 0.9,
                "evidence": {"source": "semantic_ai"},
            }
        },
        created_by="dashboard",
    )

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-edge-pipe-id"},
        )
    )

    edge_semantic = projected["projection"]["edge_semantics"]["L7.1->L7.2:depends_on"]
    assert edge_semantic["validity"]["status"] == "edge_semantic_current"
    assert edge_semantic["semantic"]["relation_purpose"] == "Dashboard pipe id enriches the dependency."
    assert projected["health"]["edge_semantic_current_count"] == 1
    assert projected["health"]["edge_semantic_missing_count"] == 0


def test_edge_semantic_projection_prefers_same_snapshot_pipe_ai_over_carried_rule(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph_with_dependency()
    prev = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-edge-carried-rule",
        commit_sha="prev",
        snapshot_kind="full",
        graph_json=graph,
    )
    current = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-edge-current-ai",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    for snapshot in (prev, current):
        store.index_graph_snapshot(
            conn,
            PID,
            snapshot["snapshot_id"],
            nodes=graph["deps_graph"]["nodes"],
            edges=graph["deps_graph"]["edges"],
        )
    nodes_by_id = {node["id"]: node for node in graph["deps_graph"]["nodes"]}
    edge = graph["deps_graph"]["edges"][0]
    stable_edge_key = graph_events.stable_edge_key_for_edge(
        edge,
        nodes_by_id["L7.1"],
        nodes_by_id["L7.2"],
    )
    graph_events.create_event(
        conn,
        PID,
        prev["snapshot_id"],
        event_type="edge_semantic_enriched",
        event_kind="imported_semantic_cache",
        target_type="edge",
        target_id="L7.1->L7.2:depends_on",
        status=graph_events.EVENT_STATUS_OBSERVED,
        stable_node_key=stable_edge_key,
        payload={
            "semantic_payload": {
                "relation_purpose": "Rule fallback should not beat same-snapshot AI.",
                "confidence": 0.55,
                "evidence": {"source": "edge_semantic_rule"},
            }
        },
        created_by="carry-forward",
    )
    graph_events.create_event(
        conn,
        PID,
        current["snapshot_id"],
        event_type="edge_semantic_enriched",
        event_kind="semantic_job",
        target_type="edge",
        target_id="L7.1|L7.2|depends_on",
        status=graph_events.EVENT_STATUS_OBSERVED,
        payload={
            "semantic_payload": {
                "relation_purpose": "Same snapshot pipe AI wins.",
                "confidence": 0.95,
                "evidence": {"source": "semantic_ai"},
            }
        },
        created_by="dashboard",
    )

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": current["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-edge-current-ai"},
        )
    )

    edge_semantic = projected["projection"]["edge_semantics"]["L7.1->L7.2:depends_on"]
    assert edge_semantic["validity"]["status"] == "edge_semantic_current"
    assert edge_semantic["semantic"]["relation_purpose"] == "Same snapshot pipe AI wins."
    assert edge_semantic["source_event"]["snapshot_id"] == current["snapshot_id"]
    assert projected["health"]["edge_semantic_current_count"] == 1
    assert projected["health"]["edge_semantic_rule_count"] == 0


def test_graph_governance_edge_semantic_jobs_auto_enrich_and_controls(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph_with_dependency()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-edge-auto-runner",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()

    status, payload = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(tmp_path),
                "target_scope": "edge",
                "selector": {"all_eligible": True, "edge_types": ["depends_on"], "limit": 10},
                "semantic_mode": "auto",
                "actor": "dashboard_user",
            },
        )
    )

    assert status == 202
    assert payload["queued_count"] == 1
    assert payload["enriched_count"] == 1
    assert [event["event_type"] for event in payload["events"]] == [
        "edge_semantic_requested",
        "edge_semantic_enriched",
    ]
    assert payload["jobs"][0]["status"] == "rule_complete"
    assert payload["jobs"][0]["semantic"]["relation_purpose"] == "L7.1 depends on L7.2."
    assert payload["jobs"][0]["semantic"]["analyzer_role"] == "reconcile_edge_semantic_analyzer"
    assert payload["jobs"][0]["semantic_source"] == "edge_semantic_rule"
    assert payload["jobs"][0]["requires_ai"] is True

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-edge-auto-runner"},
        )
    )
    assert projected["health"]["edge_semantic_current_count"] == 0
    assert projected["health"]["edge_semantic_rule_count"] == 1
    assert projected["health"]["edge_semantic_missing_count"] == 1
    assert projected["health"]["edge_semantic_needs_ai_count"] == 1
    assert projected["health"]["edge_semantic_payload_current_count"] == 1
    assert projected["health"]["edge_semantic_coverage_ratio"] == 0.0
    assert projected["health"]["edge_semantic_payload_coverage_ratio"] == 1.0

    # MF-2026-05-10-013: terminal-status edge rows (including rule_complete)
    # are now hidden by default; pass include_terminal to assert on them.
    queue = server.handle_graph_governance_operations_queue(
        _ctx(
            {"project_id": PID},
            query={
                "snapshot_id": snapshot["snapshot_id"],
                "include_terminal": "true",
            },
        )
    )
    operations = {row["operation_type"]: row for row in queue["operations"]}
    assert operations["edge_semantic"]["status"] == "rule_complete"
    assert "run_edge_semantics" in operations["edge_semantic"]["supported_actions"]
    assert "retry" in operations["edge_semantic"]["supported_actions"]
    assert "edge-semantic:not-queued" not in {row["operation_id"] for row in queue["operations"]}

    edge_event_id = payload["jobs"][0]["event_id"]
    cancel = server.handle_graph_governance_snapshot_semantic_job_cancel(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"], "job_id": edge_event_id},
            method="POST",
            body={"actor": "dashboard_user"},
        )
    )
    assert cancel["job"]["status"] == "rejected"
    retry = server.handle_graph_governance_snapshot_semantic_job_retry(
        _ctx(
            {
                "project_id": PID,
                "snapshot_id": snapshot["snapshot_id"],
                "job_id": "L7.1->L7.2:depends_on",
            },
            method="POST",
            body={"actor": "dashboard_user"},
        )
    )
    assert retry["job"]["status"] == "ai_pending"
    assert retry["event"]["event_type"] == "edge_semantic_requested"


def test_edge_semantic_auto_enrich_ai_response_projects_current(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    auto_ai_body = server._edge_semantic_ai_body(
        {"options": {"auto_enrich": True}},
        "sid",
        auto_enrich=True,
    )
    assert auto_ai_body["semantic_use_ai"] is True
    assert "semantic_use_ai" not in server._edge_semantic_ai_body(
        {"semantic_mode": "auto"},
        "sid",
        auto_enrich=True,
    )
    assert server._edge_semantic_ai_body(
        {"semantic_use_ai": False},
        "sid",
        auto_enrich=True,
    )["semantic_use_ai"] is False

    ai_body = {}

    def fake_ai_call(_project_id, _root, _body):
        ai_body.update(_body)
        return lambda _stage, _payload: {
            "relation_purpose": "AI confirms the feature depends on the dependency.",
            "confidence": 0.93,
        }

    monkeypatch.setattr(server, "_semantic_ai_call_from_body", fake_ai_call)

    graph = _graph_with_dependency()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-edge-auto-ai",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    conn.commit()

    status, payload = server.handle_graph_governance_snapshot_semantic_jobs_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(tmp_path),
                "target_scope": "edge",
                "target_ids": ["L7.1|L7.2|depends_on"],
                "options": {"mode": "auto_enrich", "auto_enrich": True},
                "actor": "dashboard_user",
            },
        )
    )

    assert status == 202
    assert payload["queued_count"] == 1
    assert payload["enriched_count"] == 1
    assert payload["ai_error_count"] == 0
    assert ai_body["semantic_use_ai"] is True
    assert payload["events"][-1]["status"] == graph_events.EVENT_STATUS_PROPOSED
    assert payload["jobs"][0]["semantic_source"] == "semantic_ai"
    assert payload["jobs"][0]["status"] == "pending_review"

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-edge-auto-ai"},
        )
    )
    edge_semantic = projected["projection"]["edge_semantics"]["L7.1->L7.2:depends_on"]
    assert edge_semantic["validity"]["status"] == "edge_semantic_requested"
    assert projected["health"]["edge_semantic_current_count"] == 0
    assert projected["health"]["edge_semantic_missing_count"] == 1

    feedback_items = reconcile_feedback.list_feedback_items(PID, snapshot["snapshot_id"])
    edge_id_variants = set(server._semantic_edge_id_variants("L7.1|L7.2|depends_on"))
    edge_feedback = [
        item for item in feedback_items
        if item.get("target_type") == "edge" and item.get("target_id") in edge_id_variants
    ]
    assert edge_feedback
    server.handle_graph_governance_snapshot_feedback_decision(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "actor": "observer",
                "feedback_ids": [edge_feedback[0]["feedback_id"]],
                "action": "accept_semantic_enrichment",
            },
        )
    )
    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-edge-auto-ai-accepted"},
        )
    )
    edge_semantic = projected["projection"]["edge_semantics"]["L7.1->L7.2:depends_on"]
    assert edge_semantic["validity"]["status"] == "edge_semantic_current"
    assert edge_semantic["semantic"]["relation_purpose"] == "AI confirms the feature depends on the dependency."
    assert projected["health"]["edge_semantic_current_count"] == 1
    assert projected["health"]["edge_semantic_missing_count"] == 0


def test_graph_governance_semantic_events_backfill_and_projection_are_hash_aware(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph()
    feature_hash = graph_events.feature_hash_for_node(graph["deps_graph"]["nodes"][0])
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-event-source",
        commit_sha="commit-a",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    semantic_enrichment._ensure_semantic_state_schema(conn)
    semantic_payload = {
        "feature_name": "Graph Governance API",
        "semantic_purpose": "Expose graph state and semantic controls for dashboard workflows.",
        "domain_label": "governance/graph",
        "quality_flags": [],
    }
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           semantic_json, feedback_round, batch_index, updated_at)
        VALUES (?, ?, 'L7.1', 'ai_complete', ?, '{"agent/governance/server.py":"sha256:file-a"}',
                ?, 1, 0, '2026-05-09T20:31:24Z')
        """,
        (PID, snapshot["snapshot_id"], feature_hash, json.dumps(semantic_payload)),
    )
    conn.commit()

    backfilled = server.handle_graph_governance_snapshot_semantic_events_backfill(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer"},
        )
    )
    assert backfilled["semantic_node_events_created"] == 1
    events = server.handle_graph_governance_snapshot_events_list(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"event_type": "semantic_node_enriched"},
        )
    )
    assert events["count"] == 1
    assert events["events"][0]["feature_hash"] == feature_hash
    assert events["events"][0]["file_hashes"]["agent/governance/server.py"] == "sha256:file-a"

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-current"},
        )
    )
    assert projected["health"]["semantic_current_count"] == 1
    assert projected["projection"]["node_semantics"]["L7.1"]["validity"]["status"] == "semantic_current"
    assert projected["projection"]["node_semantics"]["L7.1"]["semantic"]["feature_name"] == "Graph Governance API"
    fetched = server.handle_graph_governance_snapshot_semantic_projection_get(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"]})
    )
    assert fetched["projection_id"] == "semproj-current"

    changed_graph = _graph()
    changed_graph["deps_graph"]["nodes"][0]["title"] = "Renamed Feature Node"
    changed_snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-semantic-event-source",
        commit_sha="commit-b",
        snapshot_kind="scope",
        parent_snapshot_id=snapshot["snapshot_id"],
        graph_json=changed_graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        changed_snapshot["snapshot_id"],
        nodes=changed_graph["deps_graph"]["nodes"],
        edges=changed_graph["deps_graph"]["edges"],
    )
    conn.commit()

    changed_projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": changed_snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "backfill_existing": False},
        )
    )
    changed_semantic = changed_projected["projection"]["node_semantics"]["L7.1"]
    assert changed_semantic["semantic"]["feature_name"] == "Graph Governance API"
    assert changed_semantic["validity"]["status"] == "semantic_stale_feature_hash"
    assert changed_projected["health"]["semantic_stale_count"] == 1


def test_projection_api_builds_and_fetches_branch_ref_specific_projection(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="api-branch-projection",
        commit_sha="commit-api-branch",
        snapshot_kind="scope",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    branch_ref = "refs/heads/codex/api-branch"
    node = graph["deps_graph"]["nodes"][0]
    graph_events.create_event(
        conn,
        PID,
        snapshot["snapshot_id"],
        event_id="api-branch-semantic",
        event_type="semantic_node_enriched",
        event_kind="semantic_job",
        target_type="node",
        target_id="L7.1",
        status=graph_events.EVENT_STATUS_ACCEPTED,
        branch_ref=branch_ref,
        operation_type="accept",
        stable_node_key=graph_events.stable_node_key_for_node(node),
        feature_hash=graph_events.feature_hash_for_node(node),
        payload={"semantic_payload": {"feature_name": "API branch semantic"}},
        created_by="test",
    )

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "actor": "observer",
                "projection_id": "semproj-api-branch",
                "ref_name": branch_ref,
                "branch_ref": branch_ref,
                "backfill_existing": False,
            },
        )
    )
    fetched = server.handle_graph_governance_snapshot_semantic_projection_get(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"ref_name": branch_ref, "branch_ref": branch_ref},
        )
    )

    assert projected["ref_name"] == branch_ref
    assert projected["branch_ref"] == branch_ref
    assert fetched["projection_id"] == "semproj-api-branch"
    assert fetched["ref_name"] == branch_ref
    assert fetched["branch_ref"] == branch_ref
    assert fetched["projection"]["node_semantics"]["L7.1"]["semantic"]["feature_name"] == "API branch semantic"


def test_semantic_projection_rejects_target_id_fallback_when_lid_is_reused(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    old_graph = _graph("L7.1")
    old_node = old_graph["deps_graph"]["nodes"][0]
    old_node["title"] = "gateway.executors.plan_task"
    old_node["primary"] = ["gateway/executors/plan_task.py"]
    old_node["metadata"] = {"module": "gateway.executors.plan_task"}
    old_snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-old-lid-owner",
        commit_sha="commit-old",
        snapshot_kind="scope",
        graph_json=old_graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        old_snapshot["snapshot_id"],
        nodes=old_graph["deps_graph"]["nodes"],
        edges=old_graph["deps_graph"]["edges"],
    )
    graph_events.create_event(
        conn,
        PID,
        old_snapshot["snapshot_id"],
        event_id="old-lid-semantic",
        event_type="semantic_node_enriched",
        event_kind="semantic",
        target_type="node",
        target_id="L7.1",
        status=graph_events.EVENT_STATUS_OBSERVED,
        baseline_commit="commit-old",
        target_commit="commit-old",
        stable_node_key=graph_events.stable_node_key_for_node(old_node),
        feature_hash=graph_events.feature_hash_for_node(old_node),
        payload={
            "semantic_payload": {
                "feature_name": "plan_task executor",
                "primary": ["gateway/executors/plan_task.py"],
                "source_title": "gateway.executors.plan_task",
            }
        },
        created_by="test",
    )

    new_graph = _graph("L7.1")
    new_node = new_graph["deps_graph"]["nodes"][0]
    new_node["title"] = "frontend.dashboard.scripts.verify-acceptance"
    new_node["primary"] = ["frontend/dashboard/scripts/verify-acceptance.mjs"]
    new_node["metadata"] = {"module": "frontend.dashboard.scripts.verify-acceptance"}
    assert graph_events.stable_node_key_for_node(old_node) != graph_events.stable_node_key_for_node(new_node)

    new_snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-new-lid-owner",
        commit_sha="commit-new",
        snapshot_kind="scope",
        parent_snapshot_id=old_snapshot["snapshot_id"],
        graph_json=new_graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        new_snapshot["snapshot_id"],
        nodes=new_graph["deps_graph"]["nodes"],
        edges=new_graph["deps_graph"]["edges"],
    )
    conn.commit()

    projection = graph_events.build_semantic_projection(
        conn,
        PID,
        new_snapshot["snapshot_id"],
        actor="test",
        backfill_existing=False,
    )

    node_semantic = projection["projection"]["node_semantics"]["L7.1"]
    assert node_semantic["validity"]["status"] == "semantic_missing"
    assert node_semantic["semantic"] == {}
    assert node_semantic["source_event"]["event_id"] == ""


def test_graph_governance_current_state_contract_reports_graph_and_semantic_drift(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    monkeypatch.setattr(server, "_graph_governance_project_root", lambda _project_id, _body: Path("."))
    monkeypatch.setattr(server, "_git_head_commit", lambda _root: "commit-b")
    monkeypatch.setattr(
        server,
        "_git_changed_paths_between",
        lambda _root, _base, _target, limit=25: ["agent/governance/server.py"],
    )
    base_graph = _graph_with_dependency()
    feature_hash = graph_events.feature_hash_for_node(base_graph["deps_graph"]["nodes"][0])
    base_snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-current-state-base",
        commit_sha="commit-base",
        snapshot_kind="full",
        graph_json=base_graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        base_snapshot["snapshot_id"],
        nodes=base_graph["deps_graph"]["nodes"],
        edges=base_graph["deps_graph"]["edges"],
    )
    semantic_enrichment._ensure_semantic_state_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           semantic_json, feedback_round, batch_index, updated_at)
        VALUES (?, ?, 'L7.1', 'ai_complete', ?,
                '{"agent/governance/server.py":"sha256:file-base"}',
                ?, 1, 0, '2026-05-10T00:00:00Z')
        """,
        (
            PID,
            base_snapshot["snapshot_id"],
            feature_hash,
            json.dumps({"feature_name": "Stale semantic"}),
        ),
    )
    conn.commit()
    server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": base_snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-current-state-base"},
        )
    )
    changed_graph = _graph_with_dependency()
    changed_graph["deps_graph"]["nodes"][0]["title"] = "Renamed Feature Node"
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-current-state-contract",
        commit_sha="commit-a",
        snapshot_kind="full",
        parent_snapshot_id=base_snapshot["snapshot_id"],
        graph_json=changed_graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=changed_graph["deps_graph"]["nodes"],
        edges=changed_graph["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-current-state", "backfill_existing": False},
        )
    )
    assert projected["health"]["semantic_stale_count"] == 1
    assert projected["health"]["semantic_missing_count"] == 1
    assert projected["health"]["edge_semantic_missing_count"] == 1

    status = server.handle_graph_governance_status(_ctx({"project_id": PID}))

    current = status["current_state"]
    assert current["graph_stale"]["is_stale"] is True
    assert current["graph_stale"]["active_graph_commit"] == "commit-a"
    assert current["graph_stale"]["head_commit"] == "commit-b"
    assert current["graph_stale"]["changed_file_count"] == 1
    assert current["semantic_snapshot"]["projection_id"] == "semproj-current-state"
    assert current["semantic_snapshot"]["base_commit"] == "commit-a"
    assert current["semantic_drift"]["node_stale"] == 1
    assert current["semantic_drift"]["node_missing"] == 1
    assert current["semantic_drift"]["edge_missing"] == 1
    assert current["semantic_drift"]["semantic_status_counts"]["semantic_stale_feature_hash"] == 1
    assert current["drift_ledger"]["count"] == 0
    assert current["drift_ledger"]["ledger_only"] is True

    drift = server.handle_graph_governance_drift_list(_ctx({"project_id": PID}))
    assert drift["count"] == 0
    assert drift["ledger_only"] is True
    assert drift["graph_stale"]["is_stale"] is True
    assert drift["semantic_drift"]["edge_missing"] == 1

    operations = server.handle_graph_governance_operations_queue(
        _ctx(
            {"project_id": PID},
            query={"include_status_observations": "true", "include_resolved": "true"},
        )
    )
    assert operations["summary"]["current_state"]["graph_stale"]["is_stale"] is True
    assert operations["summary"]["semantic_snapshot"]["projection_id"] == "semproj-current-state"
    assert operations["summary"]["semantic_drift"]["node_stale"] == 1
    ops_by_id = {row["operation_id"]: row for row in operations["operations"]}
    stale_node_row = ops_by_id["node-semantic:not-queued"]
    assert stale_node_row["operation_type"] == "node_semantic"
    assert stale_node_row["status"] == "not_queued"
    assert stale_node_row["progress"] == {"done": 0, "total": 1}
    assert stale_node_row["supported_actions"] == ["queue_node_semantics", "file_backlog", "view_trace"]
    assert operations["summary"]["by_type"]["node_semantic"] == 1
    assert operations["summary"]["by_status"]["not_queued"] == 3


def test_graph_governance_semantic_projection_treats_hash_source_gap_as_internal(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    base_snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-unverified-base",
        commit_sha="commit-base",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        base_snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    semantic_enrichment._ensure_semantic_state_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           semantic_json, feedback_round, batch_index, updated_at)
        VALUES (?, ?, 'L7.1', 'ai_complete',
                'sha256:1111111111111111111111111111111111111111111111111111111111111111',
                '{"agent/governance/server.py":"sha256:file-a"}',
                ?, 1, 0, '2026-05-10T00:00:00Z')
        """,
        (
            PID,
            base_snapshot["snapshot_id"],
            json.dumps({"feature_name": "Old semantic with indexed hash"}),
        ),
    )
    conn.commit()
    server.handle_graph_governance_snapshot_semantic_events_backfill(
        _ctx(
            {"project_id": PID, "snapshot_id": base_snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer"},
        )
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-semantic-unverified",
        commit_sha="commit-unverified",
        snapshot_kind="scope",
        parent_snapshot_id=base_snapshot["snapshot_id"],
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    conn.commit()

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "projection_id": "semproj-unverified", "backfill_existing": False},
        )
    )

    health = projected["health"]
    assert health["semantic_unverified_hash_count"] == 0
    assert health["semantic_review_debt_count"] == 0
    assert health["semantic_trusted_ratio"] == 1.0
    assert health["semantic_debt_penalty"] == 0.0
    assert health["project_health_score"] > 90
    assert projected["projection"]["node_semantics"]["L7.1"]["validity"]["status"] == (
        "semantic_carried_forward_current"
    )
    assert projected["projection"]["node_semantics"]["L7.1"]["validity"]["hash_validation"] == (
        "hash_source_unavailable"
    )


def test_graph_governance_active_dashboard_bundle_returns_common_dashboard_data(conn):
    graph = _graph()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-dashboard-active-bundle",
        commit_sha="commit-dashboard",
        snapshot_kind="full",
        graph_json=graph,
        file_inventory=[
            {
                "path": "agent/governance/server.py",
                "file_kind": "source",
                "scan_status": "clustered",
                "graph_status": "mapped",
            },
        ],
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    semantic_enrichment._ensure_semantic_state_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_semantic_jobs
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           feedback_round, batch_index, attempt_count, updated_at, created_at)
        VALUES (?, ?, 'L7.1', 'pending_ai', 'sha256:feature', '{}',
                1, 0, 0, '2026-05-10T00:01:00Z', '2026-05-10T00:00:00Z')
        """,
        (PID, snapshot["snapshot_id"]),
    )
    graph_events.create_event(
        conn,
        PID,
        snapshot["snapshot_id"],
        event_type="semantic_retry_requested",
        event_kind="semantic_job",
        target_type="node",
        target_id="L7.1",
        status="observed",
        payload={"reason": "dashboard bundle test"},
        created_by="observer",
    )
    graph_events.build_semantic_projection(
        conn,
        PID,
        snapshot["snapshot_id"],
        actor="observer",
        projection_id="semproj-dashboard-bundle",
    )
    reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        created_by="observer",
        issues=[{
            "node_id": "L7.1",
            "reason": "dependency_patch_suggestions",
            "summary": "Add a relation for the dashboard bundle.",
            "target": "agent.governance.server",
            "type": "add_typed_relation",
        }],
    )
    conn.commit()

    bundle = server.handle_graph_governance_dashboard_active_bundle(
        _ctx(
            {"project_id": PID},
            query={"node_limit": "10", "edge_limit": "10", "event_limit": "10", "job_limit": "10"},
        )
    )

    assert bundle["ok"] is True
    assert bundle["snapshot_id"] == snapshot["snapshot_id"]
    assert bundle["summary"]["snapshot_id"] == snapshot["snapshot_id"]
    assert bundle["nodes"][0]["node_id"] == "L7.1"
    assert bundle["events"]["count"] >= 1
    assert bundle["semantic_jobs"]["summary"]["by_status"] == {"pending_ai": 1}
    assert bundle["semantic_projection"]["projection_id"] == "semproj-dashboard-bundle"
    assert bundle["feedback_queue"]["summary"]["raw_count"] == 1
    assert bundle["commit_timeline"]["count"] == 1
    assert "semantic_projection" in bundle["endpoints"]


def test_graph_governance_node_timeline_combines_events_semantics_jobs_and_feedback(conn):
    graph = _graph()
    feature_hash = graph_events.feature_hash_for_node(graph["deps_graph"]["nodes"][0])
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-node-timeline",
        commit_sha="commit-node-timeline",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    semantic_enrichment._ensure_semantic_state_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_semantic_jobs
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           feedback_round, batch_index, attempt_count, updated_at, created_at)
        VALUES (?, ?, 'L7.1', 'ai_complete', ?, '{"agent/governance/server.py":"sha256:file"}',
                1, 0, 1, '2026-05-10T01:02:00Z', '2026-05-10T01:00:00Z')
        """,
        (PID, snapshot["snapshot_id"], feature_hash),
    )
    graph_events.create_event(
        conn,
        PID,
        snapshot["snapshot_id"],
        event_type="semantic_node_enriched",
        event_kind="semantic_job",
        target_type="node",
        target_id="L7.1",
        status="observed",
        feature_hash=feature_hash,
        file_hashes={"agent/governance/server.py": "sha256:file"},
        payload={"semantic_payload": {"feature_name": "Timeline Feature"}},
        created_by="semantic-ai",
    )
    graph_events.build_semantic_projection(
        conn,
        PID,
        snapshot["snapshot_id"],
        actor="observer",
        projection_id="semproj-node-timeline",
    )
    reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        created_by="observer",
        issues=[{
            "node_id": "L7.1",
            "reason": "coverage_gap",
            "summary": "Timeline feature needs review.",
            "type": "missing_doc_binding",
        }],
    )
    conn.commit()

    result = server.handle_graph_governance_snapshot_node_timeline(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"], "node_id": "L7.1"})
    )

    assert result["ok"] is True
    assert result["node"]["node_id"] == "L7.1"
    assert result["semantic"]["semantic"]["feature_name"] == "Timeline Feature"
    assert result["semantic_job"]["status"] == "ai_complete"
    assert result["summary"]["event_count"] >= 1
    assert result["summary"]["feedback_count"] == 1
    assert {item["source"] for item in result["timeline"]} >= {
        "snapshot_node",
        "semantic_projection",
        "semantic_job",
        "graph_event",
        "feedback",
    }


def test_graph_governance_semantic_projection_excludes_package_markers_from_feature_health(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    graph = _graph()
    graph["deps_graph"]["nodes"].append({
        "id": "L7.2",
        "layer": "L7",
        "title": "agent.governance",
        "kind": "service_runtime",
        "primary": ["agent/governance/__init__.py"],
        "secondary": [],
        "test": [],
        "metadata": {
            "exclude_as_feature": True,
            "file_role": "package_marker",
        },
    })
    graph["deps_graph"]["edges"].append({
        "source": "L7.2",
        "target": "L3.1",
        "edge_type": "contains",
        "direction": "hierarchy",
        "evidence": {"source": "test"},
    })
    feature_hash = graph_events.feature_hash_for_node(graph["deps_graph"]["nodes"][0])
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-exclude-marker",
        commit_sha="commit-marker",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    semantic_enrichment._ensure_semantic_state_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           semantic_json, feedback_round, batch_index, updated_at)
        VALUES (?, ?, 'L7.1', 'ai_complete', ?, '{}',
                ?, 1, 0, '2026-05-10T00:00:00Z')
        """,
        (
            PID,
            snapshot["snapshot_id"],
            feature_hash,
            json.dumps({"feature_name": "Governed feature"}),
        ),
    )
    conn.commit()

    projected = server.handle_graph_governance_snapshot_semantic_projection_build(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer"},
        )
    )

    assert projected["health"]["raw_feature_count"] == 2
    assert projected["health"]["governed_feature_count"] == 1
    assert projected["health"]["excluded_feature_count"] == 1
    assert projected["health"]["feature_count"] == 1
    assert projected["health"]["semantic_current_count"] == 1
    assert projected["health"]["doc_coverage_ratio"] == 1.0
    assert projected["health"]["test_coverage_ratio"] == 1.0


def test_graph_governance_events_lifecycle_and_materialize_candidate_snapshot(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-events-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    semantic_enrichment._ensure_semantic_state_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           semantic_json, feedback_round, batch_index, updated_at)
        VALUES (?, ?, 'L7.1', 'semantic_complete', 'oldhash', '{}', '{}', 1, 0, 'now')
        """,
        (PID, snapshot["snapshot_id"]),
    )
    conn.commit()

    status, created = server.handle_graph_governance_snapshot_events_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "event_type": "node_rename_proposed",
                "target_type": "node",
                "target_id": "L7.1",
                "payload": {"new_title": "Renamed Feature"},
                "actor": "dashboard_user",
            },
        )
    )
    assert status == 201
    event_id = created["event"]["event_id"]

    accepted = server.handle_graph_governance_snapshot_event_accept(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"], "event_id": event_id},
            method="POST",
            body={"actor": "observer"},
        )
    )
    assert accepted["event"]["status"] == graph_events.EVENT_STATUS_ACCEPTED
    status, stale_candidate = server.handle_graph_governance_snapshot_events_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "event_type": "doc_binding_added",
                "target_type": "node",
                "target_id": "L7.1",
                "payload": {"files": ["docs/dev/new-doc.md"]},
                "precondition": {"expected_node_title": "Not The Current Title"},
            },
        )
    )
    assert status == 201
    stale_event_id = stale_candidate["event"]["event_id"]
    server.handle_graph_governance_snapshot_event_accept(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"], "event_id": stale_event_id},
            method="POST",
            body={"actor": "observer"},
        )
    )

    materialized = server.handle_graph_governance_snapshot_events_materialize(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer"},
        )
    )
    assert materialized["materialized_count"] == 1
    assert materialized["new_snapshot_id"]
    graph_json = json.loads(store.snapshot_graph_path(PID, materialized["new_snapshot_id"]).read_text(encoding="utf-8"))
    assert graph_json["deps_graph"]["nodes"][0]["title"] == "Renamed Feature"
    event = graph_events.get_event(conn, PID, snapshot["snapshot_id"], event_id)
    assert event["status"] == graph_events.EVENT_STATUS_MATERIALIZED
    stale_event = graph_events.get_event(conn, PID, snapshot["snapshot_id"], stale_event_id)
    assert stale_event["status"] == graph_events.EVENT_STATUS_STALE
    semantic_row = conn.execute(
        """
        SELECT status FROM graph_semantic_nodes
        WHERE project_id = ? AND snapshot_id = ? AND node_id = 'L7.1'
        """,
        (PID, snapshot["snapshot_id"]),
    ).fetchone()
    assert semantic_row["status"] == "semantic_stale"


def test_graph_governance_materialize_preview_does_not_mutate_events_or_snapshots(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-events-preview-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    conn.commit()

    status, created = server.handle_graph_governance_snapshot_events_create(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "event_type": "node_rename_proposed",
                "target_type": "node",
                "target_id": "L7.1",
                "payload": {"new_title": "Previewed Feature"},
                "actor": "dashboard_user",
            },
        )
    )
    assert status == 201
    event_id = created["event"]["event_id"]

    preview = server.handle_graph_governance_snapshot_events_materialize_preview(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"actor": "observer", "event_id": event_id},
        )
    )

    assert preview["ok"] is True
    assert preview["would_create_snapshot"] is True
    assert preview["would_materialize_count"] == 1
    assert preview["diff"]["nodes"]["changed_count"] == 1
    assert preview["diff"]["nodes"]["changed"][0]["after"]["title"] == "Previewed Feature"
    event = graph_events.get_event(conn, PID, snapshot["snapshot_id"], event_id)
    assert event["status"] == graph_events.EVENT_STATUS_PROPOSED
    rows = conn.execute(
        """
        SELECT COUNT(*) AS count FROM graph_snapshots
        WHERE project_id=? AND parent_snapshot_id=?
        """,
        (PID, snapshot["snapshot_id"]),
    ).fetchone()
    assert rows["count"] == 0


def test_graph_governance_dashboard_api_summarizes_active_state(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-dashboard",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
        file_inventory=[
            {
                "path": "agent/service.py",
                "file_kind": "source",
                "scan_status": "clustered",
                "graph_status": "mapped",
                "decision": "govern",
            },
            {
                "path": "README.md",
                "file_kind": "index_doc",
                "scan_status": "index_asset",
                "graph_status": "index_asset",
                "decision": "attach_to_index_wrapper",
            },
        ],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    store.record_drift(
        conn,
        PID,
        snapshot_id=snapshot["snapshot_id"],
        commit_sha="head",
        path="README.md",
        drift_type="missing_test",
        target_symbol="doc:index",
    )
    conn.commit()

    dashboard = server.handle_graph_governance_dashboard(
        _ctx({"project_id": PID}, query={"file_sample_limit": "1"})
    )

    assert dashboard["ok"] is True
    assert dashboard["snapshot_id"] == snapshot["snapshot_id"]
    assert dashboard["status"]["active_snapshot_id"] == snapshot["snapshot_id"]
    assert dashboard["file_state"]["summary"]["by_kind"]["source"] == 1
    assert dashboard["file_state"]["total_count"] == 2
    assert dashboard["drift_summary"]["by_status"]["open"] == 1
    assert dashboard["drift_summary"]["by_type"]["missing_test"] == 1


def test_graph_governance_commit_anchored_dashboard_p0_apis(conn, monkeypatch):
    monkeypatch.setattr(server, "_git_commit_subject", lambda sha: f"subject {sha[:7]}")
    old = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-old-dashboard",
        commit_sha="oldcommit",
        snapshot_kind="scope",
        graph_json=_graph(),
        file_inventory=[
            {
                "path": "agent/governance/server.py",
                "file_kind": "source",
                "scan_status": "clustered",
                "graph_status": "mapped",
                "decision": "govern",
            },
            {
                "path": "docs/orphan.md",
                "file_kind": "doc",
                "scan_status": "orphan",
                "graph_status": "unmapped",
                "decision": "pending",
            },
        ],
        notes=json.dumps({
            "semantic_enrichment": {
                "semantic_graph_state": {"open_issue_count": 3}
            },
            "global_semantic_review": {
                "latest_full_semantic_coverage_ratio": 0.5
            },
        }),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        old["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    # MF-2026-05-10-012: keep this fixture's semantic_health=="metadata_only"
    # behaviour by skipping the new auto-rebuild hook. The test asserts the
    # placeholder status that legacy snapshots carry before any projection
    # has been built.
    store.activate_graph_snapshot(
        conn, PID, old["snapshot_id"], auto_rebuild_projection=False
    )
    candidate = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-new-dashboard",
        commit_sha="newcommit",
        snapshot_kind="scope",
        graph_json=_graph("L7.2"),
        file_inventory=[],
    )
    store.index_graph_snapshot(
        conn,
        PID,
        candidate["snapshot_id"],
        nodes=_graph("L7.2")["deps_graph"]["nodes"],
        edges=_graph("L7.2")["deps_graph"]["edges"],
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="pendingcommit",
        parent_commit_sha="oldcommit",
    )
    graph_correction_patches.create_patch(
        conn,
        PID,
        patch_id="patch-summary-accepted",
        patch_type="add_edge",
        risk_level="low",
        target_node_id="L7.1",
        patch_json={
            "edge": {
                "src": "L7.1",
                "dst": "L7.2",
                "edge_type": "depends_on",
                "direction": "dependency",
            }
        },
        evidence={"reason": "dashboard summary test"},
    )
    graph_correction_patches.create_patch(
        conn,
        PID,
        patch_id="patch-summary-proposed-high",
        patch_type="merge_nodes",
        risk_level="high",
        target_node_id="L7.1",
        patch_json={"source_node_ids": ["L7.1", "L7.2"], "target_node_id": "L7.1"},
        evidence={"reason": "dashboard summary test"},
    )
    graph_correction_patches.accept_patch(conn, PID, "patch-summary-accepted", accepted_by="observer")
    conn.commit()

    timeline = server.handle_graph_governance_commit_timeline(
        _ctx({"project_id": PID}, query={"include_backlog": "false"})
    )
    assert timeline["ok"] is True
    assert timeline["active_snapshot_id"] == old["snapshot_id"]
    commits = {row["commit_sha"]: row for row in timeline["commits"]}
    assert commits["oldcommit"]["is_active"] is True
    assert commits["oldcommit"]["subject"] == "subject oldcomm"
    assert commits["oldcommit"]["counts"]["features"] == 1
    assert commits["oldcommit"]["counts"]["orphan_files"] == 1
    assert commits["oldcommit"]["counts"]["ai_review_feedback"] == 3
    assert commits["newcommit"]["snapshot_status"] == "candidate"

    exact = server.handle_graph_governance_commit_graph_state(
        _ctx({"project_id": PID, "commit_sha": "oldcommit"})
    )
    assert exact["resolution"] == "exact"
    assert exact["resolved_snapshot_id"] == old["snapshot_id"]
    assert exact["is_active"] is True
    assert exact["has_semantic_review"] is True

    pending = server.handle_graph_governance_commit_graph_state(
        _ctx({"project_id": PID, "commit_sha": "pendingcommit"})
    )
    assert pending["resolution"] == "pending"
    assert pending["pending_scope_reconcile"] is True

    advisory = server.handle_graph_governance_commit_graph_state(
        _ctx({"project_id": PID, "commit_sha": "missingcommit"})
    )
    assert advisory["resolution"] == "advisory_latest"
    assert advisory["resolved_snapshot_id"] == old["snapshot_id"]
    assert advisory["warnings"]

    summary = server.handle_graph_governance_snapshot_summary(
        _ctx({"project_id": PID, "snapshot_id": old["snapshot_id"]})
    )
    assert summary["counts"]["nodes"] == 1
    assert summary["counts"]["edges"] == 1
    assert summary["counts"]["files"] == 2
    assert summary["health"]["semantic_coverage_ratio"] == 0.5
    assert summary["health"]["structure_health_score"] is not None
    assert summary["health"]["structure_health"]["governed_feature_count"] == 1
    assert summary["health"]["structure_health"]["l4_asset_health"]["asset_count"] == 0
    assert summary["health"]["semantic_health"]["status"] == "metadata_only"
    assert summary["health"]["project_insight_health"]["status"] == "metadata_only"
    assert summary["graph_correction_patches"]["total"] == 2
    assert summary["graph_correction_patches"]["replayable_count"] == 1
    assert summary["graph_correction_patches"]["high_risk_proposed_count"] == 1


def test_graph_governance_summary_project_health_prefers_structure_when_no_legacy_score(conn):
    graph = _graph()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-health-taxonomy",
        commit_sha="health-taxonomy",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    graph_events.build_semantic_projection(
        conn,
        PID,
        snapshot["snapshot_id"],
        actor="observer",
        projection_id="semproj-health-taxonomy",
        backfill_existing=False,
    )

    summary = server.handle_graph_governance_snapshot_summary(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"]})
    )

    health = summary["health"]
    assert health["structure_health_score"] > health["semantic_health_score"]
    assert health["project_health_score"] == health["structure_health_score"]


def test_graph_governance_summary_exposes_file_hygiene_review_samples(conn, tmp_path):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-file-hygiene-summary",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    report_path = tmp_path / "global-review.json"
    report_path.write_text(
        json.dumps(
            {
                "health_picture": {
                    "project_health_score": 81.5,
                    "file_hygiene_score": 57.45,
                    "low_health_count": 3,
                    "project_health_issue_counts": {"file_hygiene": 2},
                    "file_hygiene": {
                        "available": True,
                        "run_id": "inventory-run",
                        "total_files": 7,
                        "review_required_count": 2,
                        "orphan_count": 1,
                        "pending_decision_count": 1,
                        "cleanup_candidate_count": 1,
                        "cleanup_candidate_mb": 4.5,
                        "by_kind": {"doc": 1, "generated": 1},
                        "by_scan_status": {"orphan": 1, "ignored": 1},
                        "by_graph_status": {"unmapped": 1, "ignored": 1},
                        "review_required_sample": [
                            {
                                "path": "docs/orphan.md",
                                "file_kind": "doc",
                                "suggested_dashboard_actions": ["attach_to_node", "waive"],
                            }
                        ],
                        "cleanup_candidate_sample": [
                            {
                                "path": "docs/dev/scratch/ai-output.json",
                                "file_kind": "generated",
                                "size_bytes": 4718592,
                                "suggested_dashboard_actions": ["delete_candidate", "waive"],
                            }
                        ],
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    notes = {
        "global_semantic_review": {
            "latest_full_review_path": str(report_path),
            "latest_full_run_id": "full-review-file-hygiene",
            "latest_full_status": "completed",
        }
    }
    conn.execute(
        "UPDATE graph_snapshots SET notes=? WHERE project_id=? AND snapshot_id=?",
        (json.dumps(notes), PID, snapshot["snapshot_id"]),
    )
    conn.commit()

    summary = server.handle_graph_governance_snapshot_summary(
        _ctx({"project_id": PID, "snapshot_id": snapshot["snapshot_id"]})
    )

    insight = summary["health"]["project_insight_health"]
    assert insight["status"] == "reviewed"
    assert insight["file_hygiene_score"] == 57.45
    assert insight["file_hygiene"]["available"] is True
    assert insight["file_hygiene"]["review_required_count"] == 2
    assert insight["file_hygiene"]["cleanup_candidate_count"] == 1
    assert insight["file_hygiene"]["review_required_sample"][0]["path"] == "docs/orphan.md"
    assert (
        insight["file_hygiene"]["cleanup_candidate_sample"][0]["path"]
        == "docs/dev/scratch/ai-output.json"
    )


def test_graph_governance_file_hygiene_actions_create_auditable_events(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-file-hygiene-actions",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.write_companion_files(
        PID,
        snapshot["snapshot_id"],
        graph_json=_graph(),
        file_inventory=[
            {
                "path": "docs/orphan.md",
                "file_kind": "doc",
                "scan_status": "orphan",
                "graph_status": "unmapped",
                "decision": "pending",
                "size_bytes": 123,
            },
            {
                "path": "docs/dev/scratch/ai-output.json",
                "file_kind": "generated",
                "scan_status": "ignored",
                "graph_status": "ignored",
                "decision": "ignore",
                "size_bytes": 456,
            },
        ],
    )
    conn.commit()

    status, attached = server.handle_graph_governance_snapshot_file_hygiene_action(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "action": "attach_to_node",
                "path": "docs/orphan.md",
                "node_id": "L7.1",
                "actor": "dashboard-user",
            },
        )
    )
    assert status == 201
    assert attached["event"]["event_type"] == "doc_binding_added"
    assert attached["event"]["target_type"] == "node"
    assert attached["event"]["target_id"] == "L7.1"
    assert attached["event"]["payload"]["files"] == ["docs/orphan.md"]
    assert attached["event"]["payload"]["destructive_mutation_performed"] is False

    with pytest.raises(ValidationError, match="confirm_delete_candidate"):
        server.handle_graph_governance_snapshot_file_hygiene_action(
            _ctx(
                {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
                method="POST",
                body={
                    "action": "delete_candidate",
                    "path": "docs/dev/scratch/ai-output.json",
                    "actor": "dashboard-user",
                },
            )
        )

    status, delete_candidate = server.handle_graph_governance_snapshot_file_hygiene_action(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "action": "delete_candidate",
                "path": "docs/dev/scratch/ai-output.json",
                "confirm_delete_candidate": True,
                "actor": "dashboard-user",
            },
        )
    )
    assert status == 201
    assert delete_candidate["event"]["event_type"] == "file_delete_candidate"
    assert delete_candidate["event"]["target_type"] == "file"
    assert delete_candidate["event"]["target_id"] == "docs/dev/scratch/ai-output.json"
    assert delete_candidate["event"]["risk_level"] == "high"
    assert delete_candidate["event"]["payload"]["destructive_mutation_performed"] is False


def test_graph_governance_file_hygiene_hint_attach_writes_source_hint(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    project = tmp_path / "project"
    doc = project / "docs" / "orphan.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Orphan\n\nNeeds binding.\n", encoding="utf-8")
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-file-hygiene-hint-attach",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.write_companion_files(
        PID,
        snapshot["snapshot_id"],
        graph_json=_graph(),
        file_inventory=[
            {
                "path": "docs/orphan.md",
                "file_kind": "doc",
                "scan_status": "orphan",
                "graph_status": "unmapped",
                "decision": "pending",
                "attached_node_ids": [],
                "size_bytes": 123,
            },
        ],
    )
    conn.commit()

    result = server.handle_graph_governance_snapshot_file_hygiene_hint_attach(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "path": "docs/orphan.md",
                "target_node_id": "L7.1",
                "project_root": str(project),
                "actor": "dashboard-user",
            },
        )
    )

    assert result["ok"] is True
    assert result["state"] == "written_uncommitted"
    assert result["requires_commit"] is True
    assert result["update_graph_after_commit"] is True
    text = doc.read_text(encoding="utf-8")
    assert text.startswith("<!-- governance-hint ")
    assert '"target_node_id": "L7.1"' in text
    assert '"path": "docs/orphan.md"' in text

    second = server.handle_graph_governance_snapshot_file_hygiene_hint_attach(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "path": "docs/orphan.md",
                "target_node_id": "L7.1",
                "project_root": str(project),
                "actor": "dashboard-user",
            },
        )
    )
    assert second["already_present"] is True
    assert doc.read_text(encoding="utf-8").count("governance-hint") == 1


def test_graph_governance_file_hygiene_batch_actions_create_auditable_events(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-file-hygiene-batch-actions",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.write_companion_files(
        PID,
        snapshot["snapshot_id"],
        graph_json=_graph(),
        file_inventory=[
            {
                "path": "docs/orphan.md",
                "file_kind": "doc",
                "scan_status": "orphan",
                "graph_status": "unmapped",
                "decision": "pending",
                "size_bytes": 123,
            },
            {
                "path": "docs/dev/scratch/ai-output.json",
                "file_kind": "generated",
                "scan_status": "ignored",
                "graph_status": "ignored",
                "decision": "ignore",
                "size_bytes": 456,
            },
        ],
    )
    conn.commit()

    with pytest.raises(ValidationError, match="file inventory row not found"):
        server.handle_graph_governance_snapshot_file_hygiene_actions_batch(
            _ctx(
                {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
                method="POST",
                body={
                    "actor": "dashboard-user",
                    "actions": [
                        {"action": "waive", "path": "missing.md"},
                    ],
                },
            )
        )

    status, result = server.handle_graph_governance_snapshot_file_hygiene_actions_batch(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "actor": "dashboard-user",
                "confirm_delete_candidate": True,
                "actions": [
                    {"action": "attach_to_node", "path": "docs/orphan.md", "node_id": "L7.1"},
                    {"action": "delete_candidate", "path": "docs/dev/scratch/ai-output.json"},
                ],
            },
        )
    )

    assert status == 201
    assert result["count"] == 2
    assert [event["event_type"] for event in result["events"]] == [
        "doc_binding_added",
        "file_delete_candidate",
    ]
    assert result["events"][0]["target_type"] == "node"
    assert result["events"][0]["target_id"] == "L7.1"
    assert result["events"][1]["risk_level"] == "high"
    assert result["events"][1]["payload"]["destructive_mutation_performed"] is False
    persisted = graph_events.list_events(conn, PID, snapshot["snapshot_id"])
    assert [event["evidence"]["source"] for event in persisted] == [
        "file_hygiene_batch_action_api",
        "file_hygiene_batch_action_api",
    ]


def test_graph_governance_query_trace_api_records_source_and_events(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-query-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()

    started = server.handle_graph_governance_query_trace_start(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "snapshot_id": "active",
                "query_source": "ai_global_review",
                "query_purpose": "global_architecture_review",
                "actor": "test-ai",
                "query_budget": {"max_queries": 3},
            },
        )
    )
    trace_id = started["trace"]["trace_id"]
    assert started["trace"]["query_source"] == "ai_global_review"

    queried = server.handle_graph_governance_query(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "snapshot_id": "active",
                "trace_id": trace_id,
                "tool": "get_node",
                "args": {"node_id": "L7.1"},
            },
        )
    )
    assert queried["ok"] is True
    assert queried["result"]["node"]["title"] == "Feature Node"

    fetched = server.handle_graph_governance_query_trace_get(
        _ctx({"project_id": PID, "trace_id": trace_id})
    )
    assert fetched["trace"]["event_count"] == 1
    assert fetched["trace"]["events"][0]["tool"] == "get_node"

    finished = server.handle_graph_governance_query_trace_finish(
        _ctx(
            {"project_id": PID, "trace_id": trace_id},
            method="POST",
            body={"status": "complete"},
        )
    )
    assert finished["trace"]["status"] == "complete"


def test_mf_sub_graph_query_requires_task_scope_and_uses_bounded_source(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-query-mf-sub",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()

    with pytest.raises(ValidationError, match="fence_token is required"):
        server.handle_graph_governance_query(
            _ctx_with_role(
                {"project_id": PID},
                "mf_sub",
                method="POST",
                body={
                    "snapshot_id": "active",
                    "tool": "query_schema",
                    "query_source": "mf_subagent",
                    "parent_task_id": "subtask-1",
                },
            )
        )

    queried = server.handle_graph_governance_query(
        _ctx_with_role(
            {"project_id": PID},
            "mf_sub",
            method="POST",
            body={
                "snapshot_id": "active",
                "tool": "query_schema",
                "query_source": "mf_subagent",
                "query_purpose": "subagent_context_build",
                "parent_task_id": "subtask-1",
                "fence_token": "fence-subtask-1",
            },
        )
    )

    assert queried["ok"] is True
    assert "mf_subagent" in queried["result"]["query_sources"]
    fetched = server.handle_graph_governance_query_trace_get(
        _ctx_with_role(
            {"project_id": PID, "trace_id": queried["trace_id"]},
            "mf_sub",
        )
    )
    assert fetched["trace"]["query_source"] == "mf_subagent"
    assert fetched["trace"]["parent_task_id"] == "subtask-1"


def test_graph_governance_query_api_exposes_graph_native_discovery(conn):
    graph = _graph()
    graph["deps_graph"]["nodes"][0]["metadata"]["functions"] = [
        "agent.governance.server::serve"
    ]
    graph["deps_graph"]["nodes"][0]["metadata"]["function_lines"] = {"serve": [5, 9]}
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-query-native-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()

    schema = server.handle_graph_governance_query(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={"snapshot_id": "active", "tool": "query_schema"},
        )
    )
    assert "find_node_by_path" in schema["result"]["tool_names"]

    by_path = server.handle_graph_governance_query(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "snapshot_id": "active",
                "tool": "find_node_by_path",
                "args": {"path": "agent/governance/server.py"},
            },
        )
    )
    assert by_path["result"]["matches"][0]["node"]["node_id"] == "L7.1"

    functions = server.handle_graph_governance_query(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "snapshot_id": "active",
                "tool": "function_index",
                "args": {"query": "serve"},
            },
        )
    )
    assert functions["result"]["matches"][0]["line_start"] == 5


def test_graph_governance_queue_finalize_and_abandon_api(conn):
    old = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="imported-old",
        commit_sha="old",
        snapshot_kind="imported",
    )
    store.activate_graph_snapshot(conn, PID, old["snapshot_id"])
    candidate = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-head",
        commit_sha="head",
        snapshot_kind="scope",
        graph_json=_graph("L7.2"),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        candidate["snapshot_id"],
        nodes=_graph("L7.2")["deps_graph"]["nodes"],
        edges=[],
    )
    abandon_candidate = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-abandon",
        commit_sha="head",
        snapshot_kind="full",
    )
    conn.commit()

    code, queued = server.handle_graph_governance_pending_scope_queue(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={"commit_sha": "head", "parent_commit_sha": "old"},
        )
    )
    assert code == 201
    assert queued["pending_scope_reconcile"]["status"] == store.PENDING_STATUS_QUEUED

    finalized = server.handle_graph_governance_snapshot_finalize(
        _ctx(
            {"project_id": PID, "snapshot_id": "scope-head"},
            method="POST",
            body={
                "target_commit_sha": "head",
                "expected_old_snapshot_id": "imported-old",
                "covered_commit_shas": ["head"],
            },
        )
    )
    assert finalized["ok"] is True
    assert finalized["activation"]["snapshot_id"] == "scope-head"
    assert finalized["pending_materialized_count"] == 1
    assert store.get_active_graph_snapshot(conn, PID)["snapshot_id"] == "scope-head"

    abandoned = server.handle_graph_governance_snapshot_abandon(
        _ctx(
            {"project_id": PID, "snapshot_id": abandon_candidate["snapshot_id"]},
            method="POST",
            body={"reason": "superseded by scope candidate"},
        )
    )
    assert abandoned["ok"] is True
    assert abandoned["status"] == store.SNAPSHOT_STATUS_ABANDONED


def test_pending_scope_materialize_auto_creates_running_row(conn, tmp_path, monkeypatch):
    """Direct Update graph should not require a prior /pending-scope call."""
    from agent.governance import state_reconcile

    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setattr(server, "_graph_governance_project_root", lambda _project_id, _body: project_root)

    active = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-old",
        commit_sha="old",
        snapshot_kind="scope",
        graph_json=_graph("L7.1"),
    )
    store.activate_graph_snapshot(conn, PID, active["snapshot_id"])
    conn.commit()

    captured = {}

    def fake_run_pending_scope(conn_arg, project_id, root, **kwargs):
        rows = store.list_pending_scope_reconcile(
            conn_arg,
            project_id,
            commit_shas=["head"],
            statuses=[store.PENDING_STATUS_RUNNING],
        )
        captured["rows"] = rows
        captured["kwargs"] = kwargs
        conn_arg.execute(
            """
            UPDATE pending_scope_reconcile
            SET status = ?, snapshot_id = ?
            WHERE project_id = ? AND commit_sha = ?
            """,
            (store.PENDING_STATUS_MATERIALIZED, "scope-head", project_id, "head"),
        )
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": "scope-head",
            "covered_pending_count": 1,
            "pending_rows_bound": 1,
        }

    monkeypatch.setattr(state_reconcile, "run_pending_scope_reconcile_candidate", fake_run_pending_scope)

    code, result = server.handle_graph_governance_pending_scope_materialize(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "target_commit_sha": "head",
                "parent_commit_sha": "old",
                "actor": "dashboard",
                "activate": True,
            },
        )
    )

    assert code == 201
    assert result["ok"] is True
    assert captured["kwargs"]["target_commit_sha"] == "head"
    assert captured["rows"], "handler should create a running row before materializing"
    assert captured["rows"][0]["status"] == store.PENDING_STATUS_RUNNING

    final_rows = store.list_pending_scope_reconcile(conn, PID, commit_shas=["head"])
    assert final_rows[0]["status"] == store.PENDING_STATUS_MATERIALIZED


def test_pending_scope_queue_allows_same_commit_on_different_refs(conn):
    code_main, main = server.handle_graph_governance_pending_scope_queue(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "commit_sha": "same-head",
                "parent_commit_sha": "base",
                "branch_ref": "refs/heads/main",
            },
        )
    )
    code_feature, feature = server.handle_graph_governance_pending_scope_queue(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "commit_sha": "same-head",
                "parent_commit_sha": "base",
                "branch_ref": "refs/heads/feature",
            },
        )
    )

    assert code_main == 201
    assert code_feature == 201
    assert main["pending_scope_reconcile"]["ref_name"] == "refs/heads/main"
    assert feature["pending_scope_reconcile"]["ref_name"] == "refs/heads/feature"

    rows = store.list_pending_scope_reconcile(conn, PID, commit_shas=["same-head"])
    assert {row["ref_name"] for row in rows} == {"refs/heads/main", "refs/heads/feature"}

    feature_rows = store.list_pending_scope_reconcile(
        conn,
        PID,
        commit_shas=["same-head"],
        branch_ref="refs/heads/feature",
    )
    assert len(feature_rows) == 1
    assert feature_rows[0]["ref_name"] == "refs/heads/feature"


def test_pending_scope_schema_migrates_commit_only_identity(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        """
        CREATE TABLE pending_scope_reconcile (
          project_id TEXT NOT NULL,
          commit_sha TEXT NOT NULL,
          parent_commit_sha TEXT NOT NULL DEFAULT '',
          queued_at TEXT NOT NULL,
          status TEXT NOT NULL,
          retry_count INTEGER NOT NULL DEFAULT 0,
          snapshot_id TEXT NOT NULL DEFAULT '',
          evidence_json TEXT NOT NULL DEFAULT '{}',
          PRIMARY KEY(project_id, commit_sha)
        )
        """
    )
    c.execute(
        """
        INSERT INTO pending_scope_reconcile
          (project_id, commit_sha, parent_commit_sha, queued_at, status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (PID, "same-head", "base", "2026-05-17T00:00:00Z", store.PENDING_STATUS_QUEUED),
    )

    store.ensure_schema(c)
    store.queue_pending_scope_reconcile(
        c,
        PID,
        commit_sha="same-head",
        parent_commit_sha="base",
        branch_ref="refs/heads/feature",
    )

    rows = store.list_pending_scope_reconcile(c, PID, commit_shas=["same-head"])
    assert {row["ref_name"] for row in rows} == {"active", "refs/heads/feature"}
    c.close()


def test_pending_scope_materialize_selects_branch_worktree_identity(conn, tmp_path, monkeypatch):
    from agent.governance import state_reconcile

    worktree = tmp_path / "feature-worktree"
    worktree.mkdir()
    active_identity = store.normalize_pending_scope_identity()
    feature_identity = store.normalize_pending_scope_identity(
        branch_ref="codex/feature",
        worktree_path=str(worktree),
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="same-head",
        parent_commit_sha="base",
        ref_name=active_identity["ref_name"],
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="same-head",
        parent_commit_sha="base",
        branch_ref=feature_identity["branch_ref"],
        worktree_id=feature_identity["worktree_id"],
        worktree_path=feature_identity["worktree_path"],
    )
    conn.commit()

    captured = {}

    def fake_run_pending_scope(conn_arg, project_id, root, **kwargs):
        captured["root"] = Path(root)
        captured["kwargs"] = kwargs
        rows = store.list_pending_scope_reconcile(
            conn_arg,
            project_id,
            commit_shas=["same-head"],
            ref_name=kwargs["ref_name"],
            branch_ref=kwargs["branch_ref"],
            worktree_id=kwargs["worktree_id"],
            worktree_path=kwargs["worktree_path"],
            statuses=[store.PENDING_STATUS_QUEUED],
        )
        assert len(rows) == 1
        assert rows[0]["ref_name"] == "codex/feature"
        conn_arg.execute(
            """
            UPDATE pending_scope_reconcile
            SET status = ?, snapshot_id = ?
            WHERE project_id = ? AND ref_name = ? AND worktree_id = ? AND commit_sha = ?
            """,
            (
                store.PENDING_STATUS_MATERIALIZED,
                "scope-feature",
                project_id,
                kwargs["ref_name"],
                kwargs["worktree_id"],
                "same-head",
            ),
        )
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": "scope-feature",
            "covered_pending_count": 1,
            "pending_rows_bound": 1,
            "ref_name": kwargs["ref_name"],
            "worktree_id": kwargs["worktree_id"],
        }

    monkeypatch.setattr(state_reconcile, "run_pending_scope_reconcile_candidate", fake_run_pending_scope)

    code, result = server.handle_graph_governance_pending_scope_materialize(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "worktree_path": str(worktree),
                "target_commit_sha": "same-head",
                "branch_ref": "codex/feature",
                "actor": "dashboard",
            },
        )
    )

    assert code == 201
    assert result["ok"] is True
    assert captured["root"] == worktree.resolve()
    assert captured["kwargs"]["ref_name"] == "codex/feature"
    assert captured["kwargs"]["worktree_id"] == feature_identity["worktree_id"]

    active_rows = store.list_pending_scope_reconcile(
        conn,
        PID,
        commit_shas=["same-head"],
        ref_name=active_identity["ref_name"],
    )
    feature_rows = store.list_pending_scope_reconcile(
        conn,
        PID,
        commit_shas=["same-head"],
        ref_name=feature_identity["ref_name"],
        worktree_id=feature_identity["worktree_id"],
    )
    assert active_rows[0]["status"] == store.PENDING_STATUS_QUEUED
    assert feature_rows[0]["status"] == store.PENDING_STATUS_MATERIALIZED


def test_pending_scope_materialize_already_current_is_idempotent(conn, tmp_path, monkeypatch):
    """Direct Update graph should treat an active target commit as a no-op success."""
    from agent.governance import state_reconcile

    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setattr(server, "_graph_governance_project_root", lambda _project_id, _body: project_root)

    active = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-head",
        commit_sha="head",
        snapshot_kind="scope",
        graph_json=_graph("L7.1"),
    )
    store.activate_graph_snapshot(conn, PID, active["snapshot_id"])
    conn.commit()

    def fail_if_materializer_runs(*_args, **_kwargs):
        raise AssertionError("already-current direct update should not rematerialize")

    monkeypatch.setattr(
        state_reconcile,
        "run_pending_scope_reconcile_candidate",
        fail_if_materializer_runs,
    )

    code, result = server.handle_graph_governance_pending_scope_materialize(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "target_commit_sha": "head",
                "parent_commit_sha": "head",
                "actor": "dashboard",
                "activate": True,
            },
        )
    )

    assert code == 200
    assert result["ok"] is True
    assert result["status"] == "already_current"
    assert result["snapshot_id"] == "scope-head"
    assert store.list_pending_scope_reconcile(conn, PID, commit_shas=["head"]) == []


def test_pending_scope_materialize_existing_running_failure_becomes_recoverable(conn, tmp_path, monkeypatch):
    from agent.governance import state_reconcile

    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setattr(server, "_graph_governance_project_root", lambda _project_id, _body: project_root)
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="head",
        parent_commit_sha="old",
        status=store.PENDING_STATUS_RUNNING,
        evidence={"source": "previous_direct_update"},
    )
    conn.commit()

    def fail_materialize(*_args, **_kwargs):
        raise RuntimeError("client disconnected during materialize")

    monkeypatch.setattr(state_reconcile, "run_pending_scope_reconcile_candidate", fail_materialize)

    with pytest.raises(RuntimeError):
        server.handle_graph_governance_pending_scope_materialize(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={
                    "target_commit_sha": "head",
                    "parent_commit_sha": "old",
                    "actor": "dashboard",
                    "activate": True,
                },
            )
        )

    row = store.list_pending_scope_reconcile(conn, PID, commit_shas=["head"])[0]
    assert row["status"] == store.PENDING_STATUS_FAILED
    evidence = json.loads(row["evidence_json"])
    assert evidence["recoverable"] is True
    assert evidence["recovery_action"] == "force_requeue_pending_scope"
    assert "client disconnected" in evidence["reason"]


def test_pending_scope_catch_up_queues_range_and_materializes_head(conn, tmp_path, monkeypatch):
    from agent.governance import state_reconcile

    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setattr(server, "_graph_governance_project_root", lambda _project_id, _body: project_root)
    monkeypatch.setattr(server, "_git_head_commit", lambda _root: "c3")
    monkeypatch.setattr(server, "_git_commit_range", lambda _root, _base, _target: ["c1", "c2", "c3"])
    active = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-base",
        commit_sha="base",
        snapshot_kind="scope",
        graph_json=_graph("L7.1"),
    )
    store.activate_graph_snapshot(conn, PID, active["snapshot_id"])
    conn.commit()

    captured = {}

    def fake_materialize(conn_arg, project_id, root, **kwargs):
        rows = store.list_pending_scope_reconcile(conn_arg, project_id)
        captured["rows"] = rows
        captured["kwargs"] = kwargs
        for row in rows:
            conn_arg.execute(
                """
                UPDATE pending_scope_reconcile
                SET status=?, snapshot_id=?
                WHERE project_id=? AND commit_sha=?
                """,
                (store.PENDING_STATUS_MATERIALIZED, "scope-c3", project_id, row["commit_sha"]),
            )
        return {
            "ok": True,
            "snapshot_id": "scope-c3",
            "covered_commit_shas": ["c1", "c2", "c3"],
        }

    monkeypatch.setattr(state_reconcile, "run_pending_scope_reconcile_candidate", fake_materialize)

    code, result = server.handle_graph_governance_pending_scope_catch_up(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={"target_commit_sha": "c3", "activate": True, "actor": "dashboard"},
        )
    )

    assert code == 201
    assert result["commit_count"] == 3
    assert result["progress"] == {"done": 3, "total": 3}
    assert captured["kwargs"]["target_commit_sha"] == "c3"
    assert [row["commit_sha"] for row in captured["rows"]] == ["c1", "c2", "c3"]
    assert [item["covered"] for item in result["commits"]] == [True, True, True]


def test_reconcile_metrics_endpoint_reports_speedup(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda *_args, **_kwargs: {"role": "observer"},
    )
    store.record_reconcile_run_metric(
        conn,
        PID,
        run_id="fast",
        snapshot_id="scope-fast",
        strategy="incremental_graph_delta",
        graph_delta_mode="metadata_only",
        status="ok",
        elapsed_ms=5000,
    )
    store.record_reconcile_run_metric(
        conn,
        PID,
        run_id="full",
        snapshot_id="scope-full",
        strategy="full_rebuild_fallback",
        graph_delta_mode="full_rebuild",
        status="ok",
        elapsed_ms=40000,
    )
    conn.commit()

    result = server.handle_graph_governance_reconcile_metrics(
        _ctx({"project_id": PID}, query={"backfill": "false"})
    )

    assert result["ok"] is True
    assert result["summary"]["speedup"]["speedup_x"] == 8
    assert result["summary"]["speedup"]["elapsed_reduction_pct"] == 87.5
    assert {row["run_id"] for row in result["metrics"]} == {"fast", "full"}


def test_pending_scope_recover_stale_endpoint_marks_running_failed(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda *_args, **_kwargs: {"role": "observer"},
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="old-running",
        parent_commit_sha="base",
        status=store.PENDING_STATUS_RUNNING,
        evidence={"source": "direct_update_graph"},
    )
    conn.execute(
        """
        UPDATE pending_scope_reconcile
        SET queued_at='2026-01-01T00:00:00Z'
        WHERE project_id=? AND commit_sha=?
        """,
        (PID, "old-running"),
    )
    conn.commit()

    code, result = server.handle_graph_governance_pending_scope_recover_stale(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={"max_running_seconds": 1, "actor": "dashboard"},
        )
    )

    assert code == 200
    assert result["recovered_count"] == 1
    row = store.list_pending_scope_reconcile(conn, PID, commit_shas=["old-running"])[0]
    assert row["status"] == store.PENDING_STATUS_FAILED


def test_graph_governance_semantic_feedback_and_enrich_api(conn, tmp_path):
    project = tmp_path / "project"
    primary = project / "agent" / "governance" / "server.py"
    primary.parent.mkdir(parents=True, exist_ok=True)
    primary.write_text("def handle_graph_governance():\n    return 'ok'\n", encoding="utf-8")
    docs = project / "docs" / "dev" / "proposal.md"
    docs.parent.mkdir(parents=True, exist_ok=True)
    docs.write_text("# Proposal\n", encoding="utf-8")
    tests = project / "agent" / "tests" / "test_graph_governance_api.py"
    tests.parent.mkdir(parents=True, exist_ok=True)
    tests.write_text("def test_api():\n    assert True\n", encoding="utf-8")
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    conn.commit()

    feedback = server.handle_graph_governance_snapshot_semantic_feedback(
        _ctx(
            {"project_id": PID, "snapshot_id": "full-semantic-api"},
            method="POST",
            body={
                "actor": "observer",
                "feedback_items": {
                    "feedback_id": "fb-api-1",
                    "target_type": "node",
                    "target_id": "L7.1",
                    "issue": "Name should mention API governance.",
                },
            },
        )
    )
    assert feedback["ok"] is True
    assert feedback["feedback_count"] == 1

    enriched = server.handle_graph_governance_snapshot_semantic_enrich(
        _ctx(
            {"project_id": PID, "snapshot_id": "full-semantic-api"},
            method="POST",
            body={
                "project_root": str(project),
                "actor": "observer",
                "use_ai": False,
            },
        )
    )

    assert enriched["ok"] is True
    assert enriched["summary"]["feature_count"] == 1
    assert enriched["semantic_index"]["features"][0]["feedback_count"] == 1
    assert enriched["semantic_index"]["features"][0]["enrichment_status"] == "heuristic"
    assert Path(enriched["semantic_index_path"]).exists()


def test_graph_governance_semantic_review_queue_waits_for_ai_semantics(conn, tmp_path):
    project = tmp_path / "project"
    primary = project / "agent" / "governance" / "server.py"
    primary.parent.mkdir(parents=True, exist_ok=True)
    primary.write_text("def handle_graph_governance():\n    return 'ok'\n", encoding="utf-8")
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-review-gate-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    conn.commit()

    enriched = server.handle_graph_governance_snapshot_semantic_enrich(
        _ctx(
            {"project_id": PID, "snapshot_id": "full-semantic-review-gate-api"},
            method="POST",
            body={
                "project_root": str(project),
                "actor": "observer",
                "semantic_mode": "manual",
                "use_ai": False,
                "feedback_review_mode": "auto",
            },
        )
    )

    assert enriched["ok"] is True
    assert enriched["summary"]["semantic_run_status"] == "ai_pending"
    assert enriched["summary"]["ai_complete_count"] == 0
    assert enriched["feedback_queue"]["blocked"] is True
    assert enriched["feedback_queue"]["gate"]["reason"] == "semantic_ai_not_complete"
    rows = conn.execute(
        "SELECT COUNT(*) AS count FROM graph_semantic_jobs WHERE project_id=? AND snapshot_id=?",
        (PID, snapshot["snapshot_id"]),
    ).fetchone()
    assert rows["count"] == 0


def test_graph_governance_semantic_enrich_enqueue_stale_publishes_worker_event(
    conn, tmp_path, monkeypatch
):
    from agent.governance import event_bus

    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(event_bus, "publish", lambda event, payload: published.append((event, payload)))
    project = tmp_path / "project"
    primary = project / "agent" / "governance" / "server.py"
    primary.parent.mkdir(parents=True, exist_ok=True)
    primary.write_text("def handle_graph_governance():\n    return 'ok'\n", encoding="utf-8")
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-enqueue-stale-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    conn.commit()

    enriched = server.handle_graph_governance_snapshot_semantic_enrich(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(project),
                "actor": "observer",
                "semantic_mode": "manual",
                "use_ai": False,
                "enqueue_stale": True,
            },
        )
    )

    assert enriched["ok"] is True
    rows = conn.execute(
        "SELECT node_id, status FROM graph_semantic_jobs WHERE project_id=? AND snapshot_id=?",
        (PID, snapshot["snapshot_id"]),
    ).fetchall()
    assert [dict(row) for row in rows] == [{"node_id": "L7.1", "status": "ai_pending"}]
    assert published == [
        (
            "semantic_job.enqueued",
            {
                "project_id": PID,
                "snapshot_id": snapshot["snapshot_id"],
                "queued_count": 1,
                "target_scope": "node",
                "source": "semantic_enrich_api",
            },
        )
    ]


def test_graph_governance_semantic_enrich_can_run_full_global_review_after_semantic(conn, tmp_path):
    project = tmp_path / "project"
    primary = project / "agent" / "governance" / "server.py"
    primary.parent.mkdir(parents=True, exist_ok=True)
    primary.write_text("def handle_graph_governance():\n    return 'ok'\n", encoding="utf-8")
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-global-review-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    conn.commit()

    enriched = server.handle_graph_governance_snapshot_semantic_enrich(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(project),
                "actor": "observer",
                "semantic_mode": "manual",
                "use_ai": False,
                "feedback_review_mode": "manual",
                "run_global_review_after_semantic": True,
                "run_id": "dogfood-health-picture",
            },
        )
    )

    assert enriched["ok"] is True
    assert enriched["global_review"]["ok"] is True
    assert enriched["global_review"]["run_id"] == "dogfood-health-picture"
    assert enriched["global_review"]["health_picture"]["project_health_score"] >= 0
    assert Path(enriched["global_review"]["latest_report_path"]).exists()


def test_graph_governance_status_observation_requires_explicit_backlog_action(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-status-observation-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        created_by="observer",
        issues=[
            {
                "node_id": "L7.1",
                "reason": "coverage_review",
                "summary": "missing_test_binding flag: this node has no direct test binding.",
                "type": "",
            }
        ],
    )
    item = classified["items"][0]
    assert item["feedback_kind"] == "status_observation"

    with pytest.raises(ValidationError):
        server.handle_graph_governance_snapshot_feedback_file_backlog(
            _ctx(
                {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
                method="POST",
                body={"feedback_id": item["feedback_id"], "bug_id": "OPT-STATUS-NO-AUTO"},
            )
        )

    filed = server.handle_graph_governance_snapshot_feedback_file_backlog(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "feedback_id": item["feedback_id"],
                "bug_id": "OPT-STATUS-USER-FILED",
                "allow_status_observation": True,
            },
        )
    )

    assert filed["bug_id"] == "OPT-STATUS-USER-FILED"
    assert filed["feedback"]["status"] == "backlog_filed"
    row = conn.execute(
        "SELECT chain_trigger_json FROM backlog_bugs WHERE bug_id=?",
        ("OPT-STATUS-USER-FILED",),
    ).fetchone()
    trigger = json.loads(row["chain_trigger_json"])
    assert trigger["feedback_kind"] == "status_observation"


def test_graph_governance_feedback_submit_creates_queue_item_and_graph_event(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-feedback-submit-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()

    status, submitted = server.handle_graph_governance_snapshot_feedback_submit(
        _ctx(
            {"project_id": PID, "snapshot_id": "active"},
            method="POST",
            body={
                "feedback_kind": "graph_correction",
                "source_node_ids": ["L7.1"],
                "target_type": "edge",
                "target_id": "L7.1->L3.1:contains",
                "issue_type": "add_relation",
                "summary": "User thinks this edge needs semantic review.",
                "actor": "dashboard-user",
            },
        )
    )

    assert status == 201
    assert submitted["feedback"]["feedback_kind"] == "graph_correction"
    assert submitted["event"]["event_type"] == "graph_correction_proposed"
    assert submitted["event"]["payload"]["feedback_id"] == submitted["feedback"]["feedback_id"]

    queue = server.handle_graph_governance_snapshot_feedback_queue(
        _ctx(
            {"project_id": PID, "snapshot_id": "active"},
            query={"lane": "graph_patch_candidate"},
        )
    )
    assert queue["group_count"] == 1
    assert queue["action_catalog"]["lanes"]["graph_patch_candidate"]["primary_actions"]

    lane_queue = server.handle_graph_governance_snapshot_feedback_queue(
        _ctx(
            {"project_id": PID, "snapshot_id": "active"},
            query={"group_by": "lane"},
        )
    )
    assert lane_queue["group_by"] == "lane"
    assert lane_queue["groups"][0]["group_by"] == "lane"
    assert lane_queue["groups"][0]["target_type"] == "feedback_lane"


def test_graph_governance_feedback_file_backlog_allows_dashboard_overrides(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-feedback-backlog-override",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    submitted_status, submitted = server.handle_graph_governance_snapshot_feedback_submit(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "feedback_kind": "project_improvement",
                "source_node_ids": ["L7.1"],
                "summary": "User wants a targeted test coverage backlog.",
                "paths": ["agent/governance/server.py"],
                "actor": "dashboard-user",
            },
        )
    )
    assert submitted_status == 201

    filed = server.handle_graph_governance_snapshot_feedback_file_backlog(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "feedback_id": submitted["feedback"]["feedback_id"],
                "bug_id": "OPT-FEEDBACK-OVERRIDE",
                "overrides": {
                    "title": "Dashboard edited backlog title",
                    "priority": "P1",
                    "target_files": ["agent/governance/server.py"],
                    "acceptance_criteria": ["Dashboard override is persisted."],
                },
            },
        )
    )

    assert filed["bug_id"] == "OPT-FEEDBACK-OVERRIDE"
    row = conn.execute(
        "SELECT title, priority, target_files, acceptance_criteria FROM backlog_bugs WHERE bug_id=?",
        ("OPT-FEEDBACK-OVERRIDE",),
    ).fetchone()
    assert row["title"] == "Dashboard edited backlog title"
    assert row["priority"] == "P1"
    assert json.loads(row["target_files"]) == ["agent/governance/server.py"]
    assert json.loads(row["acceptance_criteria"]) == ["Dashboard override is persisted."]


def test_graph_governance_feedback_review_use_reviewer_ai_enables_ai(conn, tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-reviewer-ai-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        created_by="observer",
        issues=[
            {
                "node_id": "L7.1",
                "reason": "dependency_patch_suggestions",
                "summary": "Doc binding should be attached to the feedback router.",
                "target": "docs/governance/reconcile-workflow.md",
                "type": "add_doc_binding",
            }
        ],
    )
    item = classified["items"][0]
    calls = []

    def fake_builder(**kwargs):
        assert kwargs["semantic_config"].model == "claude-opus-4-7"

        def fake_call(stage, payload):
            calls.append({
                "stage": stage,
                "feedback_id": payload["feedback"]["feedback_id"],
                "has_review_context": bool(payload.get("review_context")),
                "has_read_tools": bool((payload.get("review_context") or {}).get("read_tools")),
            })
            return {
                "decision": "graph_correction",
                "rationale": "AI reviewer confirms this is graph metadata only.",
                "confidence": 0.91,
                "_ai_route": {"provider": "anthropic", "model": "claude-opus-4-7"},
            }

        return fake_call

    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_ai.build_semantic_ai_call",
        fake_builder,
    )

    reviewed = server.handle_graph_governance_snapshot_feedback_review(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(project),
                "feedback_id": item["feedback_id"],
                "use_reviewer_ai": True,
                "semantic_ai_provider": "anthropic",
                "semantic_ai_model": "claude-opus-4-7",
            },
        )
    )

    assert calls == [{
        "stage": "reconcile_feedback_review",
        "feedback_id": item["feedback_id"],
        "has_review_context": True,
        "has_read_tools": True,
    }]
    reviewed_item = reviewed["items"][0]
    assert reviewed_item["reviewer_decision"] == "graph_correction"
    assert reviewed_item["reviewer_rationale"] == "AI reviewer confirms this is graph metadata only."
    assert reviewed_item["reviewer_confidence"] == 0.91


def test_graph_governance_feedback_review_queue_uses_reviewer_ai(conn, tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-reviewer-ai-queue-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        source_round="round-001",
        created_by="observer",
        issues=[
            {
                "node_id": "L7.1",
                "reason": "dependency_patch_suggestions",
                "summary": "Add typed relation to the feedback router.",
                "target": "agent.governance.reconcile_feedback",
                "type": "add_typed_relation",
            },
            {
                "node_id": "L7.2",
                "reason": "dependency_patch_suggestions",
                "summary": "Add typed relation to the event service.",
                "target": "agent.governance.event_service",
                "type": "add_typed_relation",
            },
        ],
    )
    assert classified["count"] == 2
    calls = []

    def fake_builder(**kwargs):
        def fake_call(stage, payload):
            calls.append({
                "stage": stage,
                "feedback_id": payload["feedback"]["feedback_id"],
                "has_review_context": bool(payload.get("review_context")),
            })
            return {
                "decision": "graph_correction",
                "rationale": "AI reviewer confirms graph-only correction.",
                "confidence": 0.88,
                "_ai_route": {"provider": "anthropic", "model": "claude-opus-4-7"},
            }

        return fake_call

    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_ai.build_semantic_ai_call",
        fake_builder,
    )

    reviewed = server.handle_graph_governance_snapshot_feedback_review_queue(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(project),
                "use_reviewer_ai": True,
                "source_round": "round-001",
                "lane": "graph_patch_candidate",
                "group_by": "feature",
                "limit_groups": 10,
                "max_items": 2,
                "semantic_ai_provider": "anthropic",
                "semantic_ai_model": "claude-opus-4-7",
            },
        )
    )

    assert reviewed["ok"] is True
    assert reviewed["selected_count"] == 2
    assert reviewed["reviewed_count"] == 2
    assert [call["stage"] for call in calls] == ["reconcile_feedback_review", "reconcile_feedback_review"]
    assert all(call["has_review_context"] for call in calls)
    assert {item["reviewer_decision"] for item in reviewed["reviewed"]} == {"graph_correction"}
    assert reviewed["summary"]["by_status"] == {"reviewed": 2}


def test_graph_governance_feedback_review_queue_can_require_current_semantics(conn, tmp_path):
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    graph = _graph("L7.1")
    graph["deps_graph"]["nodes"].append({
        "id": "L7.2",
        "layer": "L7",
        "title": "Pending Feature Node",
        "kind": "service_runtime",
        "primary": ["agent/governance/reconcile_feedback.py"],
        "secondary": [],
        "test": [],
        "metadata": {"subsystem": "governance"},
    })
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-review-current-semantics-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    conn.commit()
    state_path = reconcile_feedback.semantic_graph_state_path(PID, snapshot["snapshot_id"])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({
            "node_semantics": {
                "L7.1": {
                    "status": "ai_complete",
                    "feature_hash": "hash-current",
                    "file_hashes": {"agent/governance/server.py": "a"},
                    "updated_at": "2026-05-09T00:00:00Z",
                },
                "L7.2": {
                    "status": "ai_failed",
                    "feature_hash": "hash-pending",
                    "updated_at": "2026-05-09T00:00:00Z",
                },
            }
        }),
        encoding="utf-8",
    )
    reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        source_round="round-001",
        created_by="observer",
        issues=[
            {
                "node_id": "L7.1",
                "reason": "dependency_patch_suggestions",
                "summary": "Add typed relation to the current feature.",
                "target": "agent.governance.reconcile_feedback",
                "type": "add_typed_relation",
            },
            {
                "node_id": "L7.2",
                "reason": "dependency_patch_suggestions",
                "summary": "Add typed relation to the pending feature.",
                "target": "agent.governance.server",
                "type": "add_typed_relation",
            },
            {
                "node_id": "L7.2",
                "reason": "coverage_gap",
                "summary": "missing doc binding on the pending feature.",
                "type": "missing_doc_binding",
            },
        ],
    )

    queue = server.handle_graph_governance_snapshot_feedback_queue(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={
                "source_round": "round-001",
                "lane": "graph_patch_candidate",
                "group_by": "feature",
                "require_current_semantic": "true",
            },
        )
    )

    assert queue["summary"]["require_current_semantic"] is True
    assert queue["summary"]["hidden_semantic_pending_count"] == 1
    assert queue["summary"]["by_lane_all_items"]["status_only"] == 1
    assert queue["summary"]["visible_item_count"] == 1
    assert queue["groups"][0]["source_node_ids"] == ["L7.1"]
    assert queue["groups"][0]["semantic_review_ready"] is True


def test_graph_governance_feedback_review_queue_can_batch_reviewer_ai(conn, tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-reviewer-ai-batch-queue-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        source_round="round-001",
        created_by="observer",
        issues=[
            {
                "node_id": "L7.1",
                "reason": "dependency_patch_suggestions",
                "summary": "Add typed relation to the feedback router.",
                "target": "agent.governance.reconcile_feedback",
                "type": "add_typed_relation",
            },
            {
                "node_id": "L7.2",
                "reason": "dependency_patch_suggestions",
                "summary": "Add typed relation to the event service.",
                "target": "agent.governance.event_service",
                "type": "add_typed_relation",
            },
        ],
    )
    assert classified["count"] == 2
    calls = []

    def fake_builder(**kwargs):
        def fake_call(stage, payload):
            calls.append({
                "stage": stage,
                "count": len(payload["feedback_items"]),
                "context_count": len(payload["review_contexts"]),
            })
            return {
                "items": [
                    {
                        "feedback_id": item["feedback_id"],
                        "decision": "graph_correction",
                        "rationale": "Batch reviewer confirms graph-only correction.",
                        "confidence": 0.86,
                    }
                    for item in payload["feedback_items"]
                ],
                "_ai_route": {"provider": "anthropic", "model": "claude-opus-4-7"},
            }

        return fake_call

    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_ai.build_semantic_ai_call",
        fake_builder,
    )

    reviewed = server.handle_graph_governance_snapshot_feedback_review_queue(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(project),
                "use_reviewer_ai": True,
                "batch_review": True,
                "source_round": "round-001",
                "lane": "graph_patch_candidate",
                "group_by": "feature",
                "limit_groups": 10,
                "max_items": 2,
                "semantic_ai_provider": "anthropic",
                "semantic_ai_model": "claude-opus-4-7",
            },
        )
    )

    assert reviewed["ok"] is True
    assert reviewed["selected_count"] == 2
    assert reviewed["reviewed_count"] == 2
    assert calls == [{"stage": "reconcile_feedback_review_batch", "count": 2, "context_count": 2}]
    assert {item["reviewer_decision"] for item in reviewed["reviewed"]} == {"graph_correction"}
    assert reviewed["summary"]["by_status"] == {"reviewed": 2}


def test_graph_governance_feedback_retrieval_tools_are_project_scoped(conn, tmp_path):
    project = tmp_path / "project"
    source = project / "agent" / "governance" / "server.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "def feedback_router():\n    return 'graph retrieval evidence'\n",
        encoding="utf-8",
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-retrieval-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        created_by="observer",
        issues=[{
            "node_id": "L7.1",
            "reason": "dependency_patch_suggestions",
            "summary": "feedback_router should be linked to graph retrieval.",
            "type": "add_relation",
        }],
    )
    item = classified["items"][0]

    result = server.handle_graph_governance_snapshot_feedback_retrieval(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "project_root": str(project),
                "feedback_id": item["feedback_id"],
                "operations": [
                    {"tool": "graph_query", "node_ids": ["L7.1"], "depth": 1},
                    {"tool": "grep_in_scope", "pattern": "feedback_router", "node_ids": ["L7.1"]},
                    {"tool": "read_excerpt", "path": "agent/governance/server.py", "line_start": 1, "line_end": 1},
                    {"tool": "read_excerpt", "path": "../outside.txt", "line_start": 1},
                ],
            },
        )
    )

    assert result["ok"] is True
    assert result["count"] == 4
    assert result["results"][0]["result"]["nodes"][0]["id"] == "L7.1"
    assert result["results"][1]["result"]["matches"][0]["line_no"] == 1
    assert "feedback_router" in result["results"][2]["result"]["excerpt"]
    assert result["results"][3]["result"]["ok"] is False
    assert result["results"][3]["result"]["error"] == "invalid_path"


def test_graph_governance_feedback_queue_claim_lease_blocks_duplicate_workers(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-claim-api",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        source_round="round-001",
        created_by="observer",
        issues=[{
            "node_id": "L7.1",
            "reason": "dependency_patch_suggestions",
            "summary": "Add typed relation to review queue.",
            "target": "agent.governance.reconcile_feedback",
            "type": "add_typed_relation",
        }],
    )
    assert classified["count"] == 1

    first = server.handle_graph_governance_snapshot_feedback_queue_claim(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "worker_id": "reviewer-a",
                "source_round": "round-001",
                "lane": "graph_patch_candidate",
                "limit_groups": 1,
                "max_items": 1,
            },
        )
    )
    second = server.handle_graph_governance_snapshot_feedback_queue_claim(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "worker_id": "reviewer-b",
                "source_round": "round-001",
                "lane": "graph_patch_candidate",
                "limit_groups": 1,
                "max_items": 1,
            },
        )
    )

    assert first["claimed_count"] == 1
    assert second["claimed_count"] == 0
    state = reconcile_feedback.load_feedback_state(PID, snapshot["snapshot_id"])
    item = next(iter(state["items"].values()))
    assert item["review_claim"]["worker_id"] == "reviewer-a"


def test_feedback_review_state_carries_forward_by_fingerprint(conn):
    base = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-base-feedback",
        commit_sha="base",
        snapshot_kind="scope",
        graph_json=_graph(),
    )
    current = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-current-feedback",
        commit_sha="head",
        snapshot_kind="scope",
        graph_json=_graph(),
    )
    conn.commit()
    issue = {
        "node_id": "L7.1",
        "reason": "dependency_patch_suggestions",
        "summary": "Add typed relation to feedback router.",
        "target": "agent.governance.reconcile_feedback",
        "type": "add_typed_relation",
    }
    base_classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        base["snapshot_id"],
        source_round="round-001",
        created_by="observer",
        issues=[issue],
    )
    base_item = base_classified["items"][0]
    reconcile_feedback.review_feedback_item(
        PID,
        base["snapshot_id"],
        base_item["feedback_id"],
        decision="graph_correction",
        rationale="Reviewed on base snapshot.",
        confidence=0.82,
        actor="reviewer-a",
    )

    current_classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        current["snapshot_id"],
        source_round="round-001",
        created_by="observer",
        issues=[issue],
        base_snapshot_id=base["snapshot_id"],
    )

    carried = current_classified["items"][0]
    assert current_classified["carry_forward"]["carried_forward_count"] == 1
    assert carried["status"] == "reviewed"
    assert carried["reviewer_decision"] == "graph_correction"
    assert carried["reviewer_rationale"] == "Reviewed on base snapshot."
    assert carried["carried_from_snapshot_id"] == base["snapshot_id"]


def test_graph_governance_feedback_decision_marks_user_state(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-feedback-decision",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()
    classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        created_by="observer",
        issues=[{
            "node_id": "L7.1",
            "reason": "dependency_patch_suggestions",
            "summary": "Add typed relation to the feedback router.",
            "target": "agent.governance.reconcile_feedback",
            "type": "add_typed_relation",
        }],
    )
    item = classified["items"][0]
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()

    queue = server.handle_graph_governance_snapshot_feedback_queue(
        _ctx(
            {"project_id": PID, "snapshot_id": "active"},
            query={"lane": "graph_patch_candidate"},
        )
    )
    assert queue["summary"]["raw_count"] == 1

    decided = server.handle_graph_governance_snapshot_feedback_decision(
        _ctx(
            {"project_id": PID, "snapshot_id": "active"},
            method="POST",
            body={
                "feedback_id": item["feedback_id"],
                "action": "accept_graph_correction",
                "actor": "dashboard-user",
                "rationale": "User accepts graph-only correction.",
            },
        )
    )

    assert decided["ok"] is True
    assert decided["decided_count"] == 1
    decided_item = decided["items"][0]
    assert decided_item["status"] == "accepted"
    assert decided_item["final_feedback_kind"] == "graph_correction"
    assert decided_item["accepted_by"] == "dashboard-user"


def test_graph_governance_dashboard_review_bundle_exposes_two_graphs(conn, tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    graph = {
        "deps_graph": {
            "nodes": [
                {"id": "L1.1", "layer": "L1", "title": "Project", "kind": "system", "primary": []},
                {"id": "L2.1", "layer": "L2", "title": "Governance", "kind": "subsystem", "primary": []},
                {"id": "L7.1", "layer": "L7", "title": "Feedback Router", "kind": "service_runtime", "primary": ["agent/governance/reconcile_feedback.py"]},
                {"id": "L7.2", "layer": "L7", "title": "Server API", "kind": "service_runtime", "primary": ["agent/governance/server.py"]},
            ],
            "edges": [
                {"source": "L1.1", "target": "L2.1", "edge_type": "contains", "direction": "hierarchy"},
                {"source": "L2.1", "target": "L7.1", "edge_type": "contains", "direction": "hierarchy"},
                {"source": "L7.2", "target": "L7.1", "edge_type": "calls", "direction": "dependency"},
            ],
        }
    }
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-dashboard-review",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
        file_inventory=[
            {"path": "agent/governance/reconcile_feedback.py", "file_kind": "source", "scan_status": "clustered", "graph_status": "mapped"},
            {"path": "docs/reconcile.md", "file_kind": "doc", "scan_status": "orphan", "graph_status": "unmapped"},
        ],
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    conn.commit()
    reconcile_feedback.classify_semantic_open_issues(
        PID,
        snapshot["snapshot_id"],
        created_by="observer",
        issues=[{
            "node_id": "L7.1",
            "reason": "coverage_review",
            "summary": "missing_doc_binding flag: this node has no direct doc binding.",
        }],
    )

    bundle = server.handle_graph_governance_snapshot_dashboard_review(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"persist": "true", "node_limit": "20", "edge_limit": "20"},
        )
    )

    assert bundle["ok"] is True
    assert bundle["status"]["node_count"] == 4
    assert bundle["graphs"]["architecture_hierarchy"]["node_count"] == 2
    assert "graph TD" in bundle["graphs"]["architecture_hierarchy"]["mermaid"]
    assert bundle["graphs"]["feature_dependency"]["edge_count"] == 1
    assert bundle["ai_review"]["feedback_summary"]["count"] == 1
    assert Path(bundle["artifact_path"]).exists()


def test_graph_governance_status_observation_detector_classifies_graph_candidates(conn):
    graph = {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.1",
                    "layer": "L7",
                    "title": "Service Feature",
                    "kind": "service_runtime",
                    "primary": ["agent/service.py"],
                    "secondary": ["docs/service.md"],
                    "test": ["tests/test_service.py"],
                    "metadata": {"subsystem": "service"},
                },
                {
                    "id": "L7.2",
                    "layer": "L7",
                    "title": "Uncovered Feature",
                    "kind": "service_runtime",
                    "primary": ["agent/uncovered.py"],
                    "secondary": [],
                    "test": [],
                    "metadata": {"subsystem": "service"},
                },
            ],
            "edges": [],
        }
    }
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-status-detector",
        commit_sha="head",
        snapshot_kind="scope",
        graph_json=graph,
        file_inventory=[
            {
                "path": "agent/service.py",
                "file_kind": "source",
                "scan_status": "clustered",
                "graph_status": "mapped",
                "decision": "govern",
                "attached_node_ids": ["L7.1"],
                "mapped_node_ids": ["L7.1"],
            },
            {
                "path": "docs/service.md",
                "file_kind": "doc",
                "scan_status": "secondary_attached",
                "graph_status": "attached",
                "decision": "attach_to_node",
                "attached_node_ids": ["L7.1"],
            },
            {
                "path": "tests/test_service.py",
                "file_kind": "test",
                "scan_status": "secondary_attached",
                "graph_status": "attached",
                "decision": "attach_to_node",
                "attached_node_ids": ["L7.1"],
            },
            {
                "path": "docs/legacy.md",
                "file_kind": "doc",
                "scan_status": "orphan",
                "graph_status": "unmapped",
                "decision": "pending",
            },
        ],
        notes=json.dumps({
            "pending_scope_reconcile": {
                "scope_file_delta": {
                    "changed_files": ["agent/service.py"],
                    "impacted_files": ["agent/service.py"],
                }
            }
        }),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=[],
    )
    conn.commit()

    result = server.handle_graph_governance_snapshot_feedback_status_observations(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "actor": "observer",
                "test_failures": [
                    {
                        "path": "tests/test_service.py",
                        "nodeid": "tests/test_service.py::test_service_contract",
                        "message": "expected old status",
                    }
                ],
            },
        )
    )

    assert result["ok"] is True
    assert result["detector"]["classified_count"] >= 5
    items = reconcile_feedback.list_feedback_items(PID, snapshot["snapshot_id"])
    assert {item["feedback_kind"] for item in items} == {"status_observation"}
    by_type = {item["issue_type"]: item for item in items}
    assert by_type["missing_doc_binding"]["feedback_kind"] == "status_observation"
    assert by_type["missing_test_binding"]["feedback_kind"] == "status_observation"
    assert by_type["orphan_file"]["paths"] == ["docs/legacy.md"]
    assert by_type["doc_drift_candidate"]["source_node_ids"] == ["L7.1"]
    assert by_type["stale_test_expectation_candidate"]["source_node_ids"] == ["L7.1"]
    assert by_type["failed_test_candidate"]["target_id"] == "tests/test_service.py"
    assert by_type["stale_test_expectation_candidate"]["status_observation_category"] == "stale_test_expectation"

    reviewed = server.handle_graph_governance_snapshot_feedback_review(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={
                "feedback_id": by_type["stale_test_expectation_candidate"]["feedback_id"],
                "decision": "status_observation",
                "status_observation_category": "stale_test_expectation",
                "rationale": "Keep visible for user approval before filing backlog.",
            },
        )
    )
    assert reviewed["items"][0]["reviewed_status_observation_category"] == "stale_test_expectation"


def test_graph_governance_drift_api_records_and_lists_rows(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-drift-api",
        commit_sha="head",
        snapshot_kind="full",
    )
    conn.commit()

    code, recorded = server.handle_graph_governance_drift_record(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "snapshot_id": snapshot["snapshot_id"],
                "commit_sha": "head",
                "path": "agent/service.py",
                "drift_type": "missing_doc",
                "target_symbol": "agent.service.create",
                "evidence": {"source": "unit-test"},
            },
        )
    )
    assert code == 201
    assert recorded["ok"] is True

    listed = server.handle_graph_governance_drift_list(
        _ctx(
            {"project_id": PID},
            query={"snapshot_id": snapshot["snapshot_id"], "drift_type": "missing_doc"},
        )
    )

    assert listed["ok"] is True
    assert listed["count"] == 1
    assert listed["drift"][0]["target_symbol"] == "agent.service.create"
    assert listed["drift"][0]["evidence"]["source"] == "unit-test"


def test_graph_governance_snapshot_files_api_reads_companion_inventory(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-files-api",
        commit_sha="head",
        snapshot_kind="full",
        file_inventory=[
            {
                "path": "agent/service.py",
                "file_kind": "source",
                "scan_status": "clustered",
                "graph_status": "mapped",
                "decision": "govern",
                "mapped_node_ids": ["L7.1"],
            },
            {
                "path": "docs/missing.md",
                "file_kind": "doc",
                "scan_status": "orphan",
                "graph_status": "unmapped",
                "decision": "pending",
                "mapped_node_ids": [],
            },
            {
                "path": ".coverage",
                "file_kind": "generated",
                "scan_status": "ignored",
                "graph_status": "ignored",
                "decision": "ignore",
                "size_bytes": 512,
            },
            {
                "path": "dbservice/package-lock.json",
                "file_kind": "generated",
                "scan_status": "ignored",
                "graph_status": "ignored",
                "decision": "ignore",
                "size_bytes": 4096,
            },
            {
                "path": "agent/.coverage",
                "file_kind": "generated",
                "scan_status": "ignored",
                "graph_status": "ignored",
                "decision": "ignore",
                "size_bytes": 1024,
            },
        ],
    )
    conn.commit()

    files = server.handle_graph_governance_snapshot_files(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"scan_status": "orphan"},
        )
    )

    assert files["ok"] is True
    assert files["summary"]["by_scan_status"]["orphan"] == 1
    assert files["filtered_count"] == 1
    assert files["files"][0]["path"] == "docs/missing.md"

    cleanup = server.handle_graph_governance_snapshot_files(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            query={"file_kind": "generated", "sort": "size_desc"},
        )
    )
    assert cleanup["sort"] == "size_desc"
    assert [item["path"] for item in cleanup["files"]] == [
        "dbservice/package-lock.json",
        "agent/.coverage",
        ".coverage",
    ]

    with pytest.raises(ValidationError, match="unsupported file inventory sort"):
        server.handle_graph_governance_snapshot_files(
            _ctx(
                {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
                query={"sort": "unknown"},
            )
        )


def test_graph_governance_snapshot_export_cache_writes_non_authoritative_files(conn, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-export-cache",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    conn.commit()

    code, result = server.handle_graph_governance_snapshot_export_cache(
        _ctx(
            {"project_id": PID, "snapshot_id": snapshot["snapshot_id"]},
            method="POST",
            body={"project_root": str(project)},
        )
    )

    assert code == 201
    assert result["ok"] is True
    graph_path = Path(result["graph_path"])
    manifest_path = Path(result["manifest_path"])
    assert graph_path.exists()
    assert manifest_path.exists()
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert graph["deps_graph"]["nodes"][0]["id"] == "L7.1"
    assert manifest["snapshot_id"] == snapshot["snapshot_id"]
    assert manifest["non_authoritative"] is True


def test_graph_governance_drift_file_backlog_files_bug_and_updates_drift(conn):
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-drift-backlog",
        commit_sha="head",
        snapshot_kind="full",
    )
    store.record_drift(
        conn,
        PID,
        snapshot_id=snapshot["snapshot_id"],
        commit_sha="head",
        path="README.md",
        drift_type="missing_test",
        target_symbol="doc:index",
        evidence={"source": "unit-test"},
    )
    conn.commit()

    code, result = server.handle_graph_governance_drift_file_backlog(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "snapshot_id": snapshot["snapshot_id"],
                "path": "README.md",
                "drift_type": "missing_test",
                "target_symbol": "doc:index",
                "bug_id": "GRAPH-DRIFT-UNIT-1",
                "actor": "unit-test",
            },
        )
    )

    assert code == 201
    assert result["bug_id"] == "GRAPH-DRIFT-UNIT-1"
    assert result["drift"]["status"] == "backlog_filed"
    row = conn.execute(
        "SELECT bug_id, status, target_files FROM backlog_bugs WHERE bug_id = ?",
        ("GRAPH-DRIFT-UNIT-1",),
    ).fetchone()
    assert row is not None
    assert row["status"] == "OPEN"
    assert "README.md" in row["target_files"]


def test_graph_governance_index_and_full_reconcile_api_call_helpers(conn, tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("# Demo\n", encoding="utf-8")

    def fake_index(conn_arg, project_id, project_root, **kwargs):
        assert conn_arg is not None
        assert project_id == PID
        assert Path(project_root) == project
        return {
            "run_id": "idx",
            "commit_sha": "head",
            "active_snapshot": {},
            "file_inventory_summary": {"total": 1},
            "symbol_index": {"symbol_count": 0},
            "doc_index": {"heading_count": 1},
            "coverage_state": {"schema_version": 1},
            "persist_summary": {"summary_path": "summary.json"},
        }

    def fake_full(conn_arg, project_id, project_root, **kwargs):
        assert conn_arg is not None
        assert project_id == PID
        assert Path(project_root) == project
        assert kwargs["semantic_enrich"] is True
        assert kwargs["semantic_use_ai"] is None
        return {
            "ok": True,
            "snapshot_id": "full-head",
            "commit_sha": kwargs["commit_sha"],
            "graph_stats": {"nodes": 1, "edges": 0},
            "semantic_enrichment": {"feature_count": 1},
        }

    def fake_backfill(conn_arg, project_id, project_root, **kwargs):
        assert conn_arg is not None
        assert project_id == PID
        assert Path(project_root) == project
        assert kwargs["reason"] == "scope blocked"
        return {
            "ok": True,
            "snapshot_id": "full-head-escape",
            "pending_scope_waiver": {"waived_count": 2},
        }

    monkeypatch.setattr(
        "agent.governance.governance_index.build_and_persist_governance_index",
        fake_index,
    )
    monkeypatch.setattr(
        "agent.governance.state_reconcile.run_state_only_full_reconcile",
        fake_full,
    )
    monkeypatch.setattr(
        "agent.governance.state_reconcile.run_backfill_escape_hatch",
        fake_backfill,
    )

    index = server.handle_graph_governance_index_build(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={"project_root": str(project), "run_id": "idx"},
        )
    )
    assert index["ok"] is True
    assert index["doc_heading_count"] == 1
    assert index["persist_summary"]["summary_path"] == "summary.json"

    code, full = server.handle_graph_governance_full_reconcile(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={"project_root": str(project), "commit_sha": "head"},
        )
    )
    assert code == 201
    assert full["ok"] is True
    assert full["snapshot_id"] == "full-head"

    code2, backfill = server.handle_graph_governance_backfill_escape(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={"project_root": str(project), "reason": "scope blocked"},
        )
    )
    assert code2 == 201
    assert backfill["ok"] is True
    assert backfill["pending_scope_waiver"]["waived_count"] == 2


def test_graph_governance_backfill_input_errors_are_validation_errors(conn, tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()

    def fake_backfill(*_args, **_kwargs):
        raise ValueError("target_commit_sha must equal HEAD")

    monkeypatch.setattr(
        "agent.governance.state_reconcile.run_backfill_escape_hatch",
        fake_backfill,
    )

    from agent.governance.errors import ValidationError

    with pytest.raises(ValidationError) as exc:
        server.handle_graph_governance_backfill_escape(
            _ctx(
                {"project_id": PID},
                method="POST",
                body={"project_root": str(project), "target_commit_sha": "not-head"},
            )
        )
    assert "target_commit_sha must equal HEAD" in str(exc.value)


def test_semantic_projection_uses_indexed_hash_metadata(conn):
    graph = _graph("L7.1")
    merge = merge_feature_hashes_into_graph_nodes(
        graph,
        {
            "feature_index": {
                "features": [
                    {
                        "node_id": "L7.1",
                        "feature_hash": "sha256:indexed-feature",
                        "file_hashes": {"agent/governance/server.py": "sha256:file-a"},
                    }
                ]
            }
        },
    )
    assert merge["nodes_updated"] == 1
    node = graph["deps_graph"]["nodes"][0]
    assert node["metadata"]["feature_hash"] == "sha256:indexed-feature"
    assert node["metadata"]["hash_scheme"] == "indexed_sha256"

    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="semantic-hash-current",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    graph_events.create_event(
        conn,
        PID,
        snapshot["snapshot_id"],
        event_type="semantic_node_enriched",
        event_kind="semantic",
        target_type="node",
        target_id="L7.1",
        status=graph_events.EVENT_STATUS_OBSERVED,
        baseline_commit="old",
        target_commit="old",
        stable_node_key=graph_events.stable_node_key_for_node(node),
        feature_hash="sha256:old-indexed-feature",
        file_hashes={"agent/governance/server.py": "sha256:file-a"},
        payload={"semantic_payload": {"summary": "ok", "open_issues": []}},
        created_by="test",
    )
    conn.commit()

    projection = graph_events.build_semantic_projection(
        conn,
        PID,
        snapshot["snapshot_id"],
        actor="test",
        backfill_existing=False,
    )
    health = projection["health"]
    assert health["semantic_current_count"] == 1
    assert health["semantic_unverified_hash_count"] == 0
    validity = projection["projection"]["node_semantics"]["L7.1"]["validity"]
    assert validity["status"] == "semantic_carried_forward_current"
    assert validity["current_hash_scheme"] == "indexed_sha256"
    assert validity["feature_hash_match"] is False
    assert validity["hash_validation"] == "file_hash_matched"


def test_operations_queue_unifies_jobs_and_edge_not_queued(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda *_args, **_kwargs: {"role": "observer"},
    )
    graph = _graph_with_dependency()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="ops-active",
        commit_sha="head",
        snapshot_kind="full",
        graph_json=graph,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    graph_events.build_semantic_projection(
        conn,
        PID,
        snapshot["snapshot_id"],
        actor="test",
        backfill_existing=False,
    )
    semantic_enrichment._ensure_semantic_state_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_semantic_jobs
          (project_id, snapshot_id, node_id, status, feature_hash,
           file_hashes_json, updated_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            PID,
            snapshot["snapshot_id"],
            "L7.1",
            "ai_pending",
            "sha256:indexed-feature",
            json.dumps({"agent/governance/server.py": "sha256:file-a"}),
            "2026-05-10T00:00:00Z",
            "2026-05-10T00:00:00Z",
        ),
    )
    conn.commit()

    queue = server.handle_graph_governance_operations_queue(
        _ctx({"project_id": PID}, query={"require_current_semantic": "true"})
    )

    assert queue["ok"] is True
    assert queue["snapshot_id"] == "ops-active"
    assert queue["summary"]["node_semantic_jobs"]["by_status"] == {"ai_pending": 1}
    assert queue["summary"]["feedback_queue"]["visible_item_count"] == 0
    operations = {row["operation_id"]: row for row in queue["operations"]}
    assert operations["node-semantic:L7.1"]["status"] == "ai_pending"
    edge_row = operations["edge-semantic:not-queued"]
    assert edge_row["status"] == "not_queued"
    assert edge_row["progress"] == {"done": 0, "total": 1}
    assert "1 edge semantics missing, 0 queued" == edge_row["last_result"]
    assert "queue_edge_semantics" in edge_row["supported_actions"]
    assert "run_edge_semantics" in edge_row["supported_actions"]


def test_operations_queue_includes_pending_scope_reconcile(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda *_args, **_kwargs: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-active",
        commit_sha="old",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="head",
        parent_commit_sha="old",
    )
    conn.commit()

    queue = server.handle_graph_governance_operations_queue(_ctx({"project_id": PID}))

    assert queue["summary"]["pending_scope_reconcile_count"] == 1
    assert queue["summary"]["by_type"]["scope_reconcile"] == 1
    row = next(item for item in queue["operations"] if item["operation_type"] == "scope_reconcile")
    assert row["target_id"] == "head"
    assert row["status"] == "queued"


def test_operations_queue_reports_pending_scope_branch_identity(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda *_args, **_kwargs: {"role": "observer"},
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-active",
        commit_sha="old",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    worktree = tmp_path / "feature-worktree"
    worktree.mkdir()
    identity = store.normalize_pending_scope_identity(
        branch_ref="codex/feature",
        worktree_path=str(worktree),
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="head",
        parent_commit_sha="old",
        branch_ref=identity["branch_ref"],
        worktree_id=identity["worktree_id"],
        worktree_path=identity["worktree_path"],
    )
    conn.commit()

    queue = server.handle_graph_governance_operations_queue(_ctx({"project_id": PID}))

    row = next(item for item in queue["operations"] if item["operation_type"] == "scope_reconcile")
    assert row["target_id"] == "head"
    assert row["ref_name"] == "codex/feature"
    assert row["branch_ref"] == "codex/feature"
    assert row["worktree_id"] == identity["worktree_id"]
    assert row["worktree_path"] == identity["worktree_path"]
    assert row["target_label"] == "head @ codex/feature"


def test_operations_queue_surfaces_pending_scope_recovery_evidence(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda *_args, **_kwargs: {"role": "observer"},
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="head",
        parent_commit_sha="old",
        status=store.PENDING_STATUS_RUNNING,
        evidence={"source": "direct_update_graph"},
    )
    store.mark_pending_scope_reconcile_failed(
        conn,
        PID,
        commit_sha="head",
        actor="test",
        reason="timeout",
    )
    conn.commit()

    queue = server.handle_graph_governance_operations_queue(_ctx({"project_id": PID}))

    row = next(item for item in queue["operations"] if item["operation_id"] == "scope-reconcile:head")
    assert row["status"] == "failed"
    assert row["last_error"] == "timeout"
    assert row["last_result"] == "force_requeue_pending_scope"
    assert row["evidence"]["recoverable"] is True
    assert "retry_scope_reconcile" in row["supported_actions"]


def test_operations_queue_synthesizes_stale_scope_reconcile(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda *_args, **_kwargs: {"role": "observer"},
    )
    monkeypatch.setattr(server, "_graph_governance_project_root", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(server, "_git_head_commit", lambda _root: "head-commit")
    changed_paths = [f"docs/governance/manual-fix-{i}.md" for i in range(30)]
    monkeypatch.setattr(
        server,
        "_git_changed_paths_between",
        lambda _root, _base, _head, limit=25: changed_paths if limit is None else changed_paths[:limit],
    )
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-stale-active",
        commit_sha="old-commit",
        snapshot_kind="full",
        graph_json=_graph(),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()

    queue = server.handle_graph_governance_operations_queue(_ctx({"project_id": PID}))

    row = next(item for item in queue["operations"] if item["operation_id"] == "scope-reconcile:stale:head-commit")
    assert row["operation_type"] == "scope_reconcile"
    assert row["status"] == "not_queued"
    assert row["target_id"] == "head-commit"
    assert row["active_graph_commit"] == "old-commit"
    assert row["changed_files"] == changed_paths[:25]
    assert "30 changed files" in row["last_result"]
    assert queue["summary"]["graph_stale"]["is_stale"] is True
    assert queue["summary"]["graph_stale"]["changed_file_count"] == 30


def test_operations_queue_surfaces_suspect_snapshot_root_warning(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda *_args, **_kwargs: {"role": "observer"},
    )
    monkeypatch.setattr(server, "_graph_governance_project_root", lambda *_args, **_kwargs: tmp_path)
    monkeypatch.setattr(server, "_git_head_commit", lambda _root: "head-commit")
    notes = {
        "checkout_provenance": {
            "execution_root": "/private/tmp/aming-claw-scope/repo",
            "execution_root_role": "execution_root",
            "execution_root_is_ephemeral": True,
            "canonical_project_identity": {"type": "git", "project_id": PID},
            "warnings": [
                {
                    "code": "ephemeral_execution_root",
                    "message": "graph snapshot was materialized from a temporary execution root",
                }
            ],
        }
    }
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-suspect-root-active",
        commit_sha="head-commit",
        snapshot_kind="scope",
        graph_json=_graph(),
        notes=json.dumps(notes, sort_keys=True),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=_graph()["deps_graph"]["nodes"],
        edges=_graph()["deps_graph"]["edges"],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()

    queue = server.handle_graph_governance_operations_queue(_ctx({"project_id": PID}))

    row = next(
        item for item in queue["operations"]
        if item["operation_id"] == "scope-reconcile:suspect-root:head-commit"
    )
    assert row["status"] == "not_queued"
    assert row["warnings"][0]["code"] == "ephemeral_execution_root"
    assert queue["summary"]["graph_stale"]["active_snapshot_warnings"][0]["code"] == "ephemeral_execution_root"


def test_managed_ref_api_tracks_existing_long_lived_branch_without_new_project(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )

    status, created = server.handle_graph_governance_managed_ref_upsert(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "ref_name": "refs/heads/release/1.x",
                "target_ref": "refs/heads/main",
                "merge_base_commit": "B0",
                "ref_head_commit": "R1",
                "target_head_commit": "M0",
                "status": "imported",
                "evidence": {"source": "project_import"},
                "now_iso": "2026-05-17T10:10:00Z",
            },
        )
    )

    assert status == 201
    assert created["project_id"] == PID
    assert created["ref"]["project_id"] == PID
    assert created["ref"]["ref_name"] == "refs/heads/release/1.x"
    assert created["decision"]["action"] == "materialize_ref_graph"

    listed = server.handle_graph_governance_managed_refs(
        _ctx(
            {"project_id": PID},
            query={"current_target_head": "M0"},
        )
    )

    assert listed["ok"] is True
    assert listed["refs"][0]["project_id"] == PID
    assert listed["refs"][0]["evidence"]["source"] == "project_import"
    assert listed["deletion_guard"]["allowed"] is False
    assert listed["deletion_guard"]["required_action"] == "archive_or_abandon_managed_refs"


def test_managed_ref_api_surfaces_stale_target_movement(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    server.handle_graph_governance_managed_ref_upsert(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "ref_name": "refs/heads/feature/long-lived",
                "target_ref": "refs/heads/main",
                "merge_base_commit": "B0",
                "ref_head_commit": "F4",
                "target_head_commit": "M0",
                "validated_target_head": "M0",
                "snapshot_id": "scope-feature-F4",
                "projection_id": "semproj-feature-F4",
                "merge_preview_id": "preview-F4-into-M0",
                "status": "merge_candidate",
                "now_iso": "2026-05-17T10:20:00Z",
            },
        )
    )

    listed = server.handle_graph_governance_managed_refs(
        _ctx(
            {"project_id": PID},
            query={"current_target_head": "M1"},
        )
    )

    decision = listed["decisions"][0]
    assert decision["decision_state"] == "stale"
    assert decision["action"] == "recompute_ref_context"
    assert decision["target_moved"] is True
    assert decision["blockers"] == ["target_ref_moved"]
    assert decision["merge_ready"] is False


def test_managed_ref_api_records_merge_then_archives_ref_context(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    server.handle_graph_governance_managed_ref_upsert(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "ref_name": "refs/heads/feature/large-refactor",
                "target_ref": "refs/heads/main",
                "merge_base_commit": "B0",
                "ref_head_commit": "F9",
                "target_head_commit": "M8",
                "validated_target_head": "M8",
                "snapshot_id": "scope-feature-F9",
                "projection_id": "semproj-feature-F9",
                "merge_preview_id": "preview-F9-into-M8",
                "status": "merge_candidate",
                "now_iso": "2026-05-17T10:30:00Z",
            },
        )
    )

    listed = server.handle_graph_governance_managed_refs(
        _ctx(
            {"project_id": PID},
            query={"current_target_head": "M8"},
        )
    )
    assert listed["decisions"][0]["merge_ready"] is True
    assert listed["decisions"][0]["action"] == "queue_merge_gate"

    merged = server.handle_graph_governance_managed_ref_merged(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "ref_name": "refs/heads/feature/large-refactor",
                "merge_commit": "M9",
                "target_head_commit": "M9",
                "merge_queue_id": "mergeq-long-ref",
                "now_iso": "2026-05-17T10:31:00Z",
            },
        )
    )

    assert merged["ref"]["status"] == "merged"
    assert merged["decision"]["action"] == "archive_ref_context"
    assert merged["decision"]["archive_allowed"] is True

    archived = server.handle_graph_governance_managed_ref_archive(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "ref_name": "refs/heads/feature/large-refactor",
                "evidence": {"reason": "merged_to_target_and_retained"},
                "now_iso": "2026-05-17T10:32:00Z",
            },
        )
    )

    assert archived["ref"]["status"] == "archived"
    assert archived["deletion_guard"]["allowed"] is True
    visible = server.handle_graph_governance_managed_refs(_ctx({"project_id": PID}))
    assert visible["refs"] == []
    retained = server.handle_graph_governance_managed_refs(
        _ctx(
            {"project_id": PID},
            query={"include_archived": "true"},
        )
    )
    assert retained["refs"][0]["status"] == "archived"


def test_managed_ref_bootstrap_api_dry_run_discovers_git_branches(conn, monkeypatch, tmp_path):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    repo = _git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "release/1.x"], cwd=repo, check=True, capture_output=True, text=True)
    (repo / "release.txt").write_text("release\n", encoding="utf-8")
    subprocess.run(["git", "add", "release.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "release work"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "-b", "codex/task-1"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True, text=True)

    result = server.handle_graph_governance_managed_ref_bootstrap_dry_run(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "project_root": str(repo),
                "target_ref": "refs/heads/main",
            },
        )
    )

    assert result["ok"] is True
    assert result["discovery"]["source"] == "git_for_each_ref"
    by_ref = {candidate["ref_name"]: candidate for candidate in result["candidates"]}
    assert by_ref["refs/heads/main"]["classification"] == "target_ref"
    assert by_ref["refs/heads/codex/task-1"]["classification"] == "short_lived_agent_ref"
    release = by_ref["refs/heads/release/1.x"]
    assert release["classification"] == "managed_ref"
    assert release["action"] == "import"
    assert release["ahead_count"] == 1
    assert release["behind_count"] == 0
    listed = server.handle_graph_governance_managed_refs(_ctx({"project_id": PID}))
    assert listed["refs"] == []


def test_managed_ref_bootstrap_api_applies_supplied_refs(conn, monkeypatch):
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )

    status, payload = server.handle_graph_governance_managed_ref_bootstrap(
        _ctx(
            {"project_id": PID},
            method="POST",
            body={
                "target_ref": "refs/heads/main",
                "target_head_commit": "M0",
                "refs": [
                    {"ref_name": "refs/heads/main", "ref_head_commit": "M0"},
                    {
                        "ref_name": "refs/heads/release/1.x",
                        "ref_head_commit": "R1",
                        "target_head_commit": "M0",
                        "merge_base_commit": "B0",
                    },
                    {"ref_name": "refs/heads/codex/task-1", "ref_head_commit": "C1"},
                ],
                "evidence": {"source": "operator_dry_run_accept"},
                "now_iso": "2026-05-17T11:20:00Z",
            },
        )
    )

    assert status == 201
    assert payload["applied_count"] == 1
    assert payload["skipped_count"] == 2
    assert payload["refs"][0]["ref_name"] == "refs/heads/release/1.x"
    listed = server.handle_graph_governance_managed_refs(_ctx({"project_id": PID}))
    assert listed["refs"][0]["ref_name"] == "refs/heads/release/1.x"
    assert listed["refs"][0]["evidence"]["source"] == "operator_dry_run_accept"
    assert listed["decisions"][0]["action"] == "materialize_ref_graph"
