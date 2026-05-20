from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time

from agent.governance import graph_correction_patches as patches
from agent.governance import graph_snapshot_store as store
from agent.governance import reconcile_feedback


PID = "feedback-patch-test"
SID = "full-feedback-patch"


def _conn(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent.governance.db._governance_root",
        lambda: tmp_path / "state",
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    store.ensure_schema(conn)
    patches.ensure_schema(conn)
    return conn


def test_concurrent_feedback_submissions_preserve_all_items(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent.governance.db._governance_root",
        lambda: tmp_path / "state",
    )
    snapshot_id = "feedback-concurrent"
    worker_count = 8
    barrier = threading.Barrier(worker_count)
    original_read_json = reconcile_feedback._read_json

    def _slow_read(path, default):
        data = original_read_json(path, default)
        time.sleep(0.02)
        return data

    monkeypatch.setattr(reconcile_feedback, "_read_json", _slow_read)

    def _submit(index: int) -> str:
        barrier.wait(timeout=5)
        result = reconcile_feedback.submit_feedback_item(
            PID,
            snapshot_id,
            feedback_kind=reconcile_feedback.KIND_NEEDS_OBSERVER_DECISION,
            issue={
                "issue": f"AI semantic enrichment generated for L7.{index}",
                "source_node_ids": [f"L7.{index}"],
                "target_id": f"L7.{index}",
                "target_type": "node",
                "priority": "P3",
            },
            actor="semantic_worker_inproc",
        )
        return str(result["items"][0]["feedback_id"])

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        feedback_ids = list(pool.map(_submit, range(worker_count)))

    state = reconcile_feedback.load_feedback_state(PID, snapshot_id)
    items = state["items"]
    assert len(items) == worker_count
    assert set(items) == set(feedback_ids)
    assert {
        item["target_id"]
        for item in items.values()
    } == {f"L7.{index}" for index in range(worker_count)}


def test_dependency_feedback_promotes_to_accepted_add_edge_patch(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    store.index_graph_snapshot(
        conn,
        PID,
        SID,
        nodes=[
            {"id": "L7.source", "title": "source", "metadata": {"module": "module.source"}},
            {"id": "L7.target", "title": "target", "metadata": {"module": "module.target"}},
        ],
    )
    classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        SID,
        source_round="round-001",
        created_by="semantic-ai",
        issues=[
            {
                "node_id": "L7.source",
                "reason": "dependency_patch_suggestions",
                "type": "add_relation",
                "target": "module.target",
                "edge_type": "reads_state",
                "summary": "source reads state owned by target",
            }
        ],
    )
    feedback_id = classified["items"][0]["feedback_id"]

    result = reconcile_feedback.promote_feedback_items_to_graph_patches(
        conn,
        PID,
        SID,
        [feedback_id],
        actor="observer",
        accept_patch=True,
        base_commit="abc123",
    )
    conn.commit()

    assert result["ok"] is True
    assert result["patches"][0]["patch_type"] == "add_edge"
    assert result["patches"][0]["status"] == "accepted"
    row = conn.execute(
        "SELECT status, patch_type, patch_json FROM graph_correction_patches"
    ).fetchone()
    assert row["status"] == "accepted"
    assert row["patch_type"] == "add_edge"
    patch_json = patches._json_load(row["patch_json"], {})
    assert patch_json["edge"]["src"] == "L7.source"
    assert patch_json["edge"]["dst"] == "L7.target"
    assert patch_json["edge"]["edge_type"] == "reads_state"

    state = reconcile_feedback.load_feedback_state(PID, SID)
    item = state["items"][feedback_id]
    assert item["status"] == "accepted"
    assert item["graph_correction_patch_status"] == "accepted"


def test_merge_feedback_stays_proposed_without_high_risk_override(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        SID,
        source_round="round-001",
        created_by="semantic-ai",
        issues=[
            {
                "node_id": "L7.a",
                "reason": "merge_suggestions",
                "type": "merge",
                "target": "L7.b",
                "nodes": ["L7.a", "L7.b"],
                "summary": "two leaves describe the same feature",
            }
        ],
    )
    feedback_id = classified["items"][0]["feedback_id"]

    result = reconcile_feedback.promote_feedback_items_to_graph_patches(
        conn,
        PID,
        SID,
        [feedback_id],
        actor="observer",
        accept_patch=True,
        base_commit="abc123",
    )
    conn.commit()

    assert result["patches"][0]["patch_type"] == "merge_nodes"
    assert result["patches"][0]["risk_level"] == "high"
    assert result["patches"][0]["status"] == "proposed"
    row = conn.execute(
        "SELECT status, risk_level FROM graph_correction_patches"
    ).fetchone()
    assert row["status"] == "proposed"
    assert row["risk_level"] == "high"


def test_dependency_feedback_without_target_does_not_create_self_edge(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    store.index_graph_snapshot(
        conn,
        PID,
        SID,
        nodes=[
            {"id": "L7.source", "title": "source", "metadata": {"module": "module.source"}},
        ],
    )
    classified = reconcile_feedback.classify_semantic_open_issues(
        PID,
        SID,
        source_round="round-001",
        created_by="semantic-ai",
        issues=[
            {
                "node_id": "L7.source",
                "reason": "dependency_patch_suggestions",
                "type": "add_relation",
                "summary": "Pairs with another suggestion, but does not name the target.",
            }
        ],
    )
    feedback_id = classified["items"][0]["feedback_id"]

    result = reconcile_feedback.promote_feedback_items_to_graph_patches(
        conn,
        PID,
        SID,
        [feedback_id],
        actor="observer",
        accept_patch=False,
        base_commit="abc123",
    )
    conn.commit()

    assert result["ok"] is False
    assert result["created_count"] == 0
    assert result["error_count"] == 1
    assert "explicit target" in result["errors"][0]["error"]
    assert conn.execute("SELECT COUNT(*) FROM graph_correction_patches").fetchone()[0] == 0
