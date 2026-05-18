from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent.governance import graph_snapshot_store as store
from agent.governance import graph_events
from agent.governance import semantic_worker
from agent.governance import state_reconcile
from agent.governance.db import _ensure_schema
from agent.governance.graph_structure_ops import SCHEMA_VERSION
from agent.governance.state_reconcile import run_state_only_full_reconcile


PID = "semantic-worker-graph-structure-test"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    store.ensure_schema(c)

    class _NoCloseConn:
        def __init__(self, raw: sqlite3.Connection):
            self._raw = raw

        def __getattr__(self, name: str):
            return getattr(self._raw, name)

        def close(self) -> None:
            pass

    monkeypatch.setattr("agent.governance.db.get_connection", lambda _project_id: _NoCloseConn(c))
    yield c
    c.close()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_generated_project(root: Path) -> None:
    _write(
        root / "agent" / "service.py",
        "def service_entry():\n"
        "    return helper()\n\n"
        "def helper():\n"
        "    return 'ok'\n",
    )
    _write(
        root / "agent" / "tests" / "test_service.py",
        "from agent.service import service_entry\n\n"
        "def test_service_entry():\n"
        "    assert service_entry() == 'ok'\n",
    )


def _service_node_id(snapshot_id: str) -> str:
    graph = state_reconcile._read_snapshot_graph(PID, snapshot_id)
    service_node = next(
        node for node in state_reconcile._deps_graph_nodes(graph)
        if "agent/service.py" in (node.get("primary") or [])
    )
    return state_reconcile._node_id(service_node)


def _payload(snapshot_id: str, base_commit: str, service_id: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "snapshot_id": snapshot_id,
            "base_commit": base_commit,
            "analyzer_role": "reconcile_graph_structure_analyzer",
        },
        "operations": [
            {
                "op": "add_edge",
                "hint_id": "worker-generated-test-edge",
                "source_path": "agent/tests/test_service.py",
                "target_node_id": service_id,
                "edge": "tests",
                "confidence": 0.93,
                "evidence": {"reason": "generated project test covers service entry"},
            }
        ],
        "self_check": {
            "valid": True,
            "checked_rules": ["hint-compatible-op", "snapshot-match"],
            "known_risks": [],
        },
    }


def test_semantic_worker_graph_structure_bridge_accepts_generated_project_output(conn, tmp_path):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    base = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="worker-graph-structure-base",
        commit_sha="workerbase",
        snapshot_id="worker-graph-structure-base",
        created_by="test",
    )
    assert base["ok"] is True
    service_id = _service_node_id("worker-graph-structure-base")
    raw_output = json.dumps(_payload("worker-graph-structure-base", "workerbase", service_id))

    preview = semantic_worker.handle_graph_structure_ai_output(
        PID,
        "worker-graph-structure-base",
        raw_output=raw_output,
        mode="dry_run",
    )
    assert preview["ok"] is True
    assert preview["mutated"] is False
    assert preview["projection"]["effect_counts"]["edges_added"] == 1

    accepted = semantic_worker.handle_graph_structure_ai_output(
        PID,
        "worker-graph-structure-base",
        raw_output=raw_output,
        mode="accept",
        project_root=project,
    )
    assert accepted["ok"] is True
    assert accepted["mutated"] is True
    assert "worker-generated-test-edge" in (
        project / "agent" / "tests" / "test_service.py"
    ).read_text(encoding="utf-8")

    materialized = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="worker-graph-structure-materialized",
        commit_sha="workerhead",
        snapshot_id="worker-graph-structure-materialized",
        created_by="test",
    )
    assert materialized["ok"] is True
    graph = state_reconcile._read_snapshot_graph(PID, "worker-graph-structure-materialized")
    assert any(
        edge.get("src") == "agent/tests/test_service.py"
        and edge.get("dst") == service_id
        and edge.get("edge_type") == "tests"
        and edge.get("direction") == "source_hint"
        for edge in state_reconcile._deps_graph_edges(graph)
    )


def test_semantic_worker_drains_graph_structure_accept_job_generated_project(conn, tmp_path):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    base = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="worker-graph-structure-queue-base",
        commit_sha="workerqueuebase",
        snapshot_id="worker-graph-structure-queue-base",
        created_by="test",
    )
    assert base["ok"] is True
    service_id = _service_node_id("worker-graph-structure-queue-base")
    raw_output = json.dumps(
        _payload("worker-graph-structure-queue-base", "workerqueuebase", service_id)
    )
    request = graph_events.create_event(
        conn,
        PID,
        "worker-graph-structure-queue-base",
        event_type="graph_structure_requested",
        event_kind="semantic_job",
        target_type="snapshot",
        target_id="worker-graph-structure-queue-base",
        status=graph_events.EVENT_STATUS_OBSERVED,
        operation_type="graph_structure",
        payload={
            "mode": "accept",
            "ai_output": raw_output,
            "project_root": str(project),
        },
        created_by="test",
    )
    conn.commit()

    semantic_worker._drain_graph_structure(PID, "worker-graph-structure-queue-base")

    request_after = graph_events.get_event(
        conn,
        PID,
        "worker-graph-structure-queue-base",
        request["event_id"],
    )
    assert request_after["status"] == graph_events.EVENT_STATUS_MATERIALIZED
    completed = graph_events.list_events(
        conn,
        PID,
        "worker-graph-structure-queue-base",
        event_types=["graph_structure_completed"],
    )
    assert len(completed) == 1
    result = completed[0]["payload"]["result"]
    assert result["ok"] is True
    assert result["mutated"] is True
    assert "worker-generated-test-edge" in (
        project / "agent" / "tests" / "test_service.py"
    ).read_text(encoding="utf-8")

    materialized = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="worker-graph-structure-queue-materialized",
        commit_sha="workerqueuehead",
        snapshot_id="worker-graph-structure-queue-materialized",
        created_by="test",
    )
    assert materialized["ok"] is True
    graph = state_reconcile._read_snapshot_graph(PID, "worker-graph-structure-queue-materialized")
    assert any(
        edge.get("src") == "agent/tests/test_service.py"
        and edge.get("dst") == service_id
        and edge.get("edge_type") == "tests"
        and edge.get("direction") == "source_hint"
        for edge in state_reconcile._deps_graph_edges(graph)
    )


def test_semantic_worker_drains_graph_structure_invalid_job_as_failed(conn, tmp_path):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    base = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="worker-graph-structure-failed-base",
        commit_sha="workerfailedbase",
        snapshot_id="worker-graph-structure-failed-base",
        created_by="test",
    )
    assert base["ok"] is True
    request = graph_events.create_event(
        conn,
        PID,
        "worker-graph-structure-failed-base",
        event_type="graph_structure_requested",
        event_kind="semantic_job",
        target_type="snapshot",
        target_id="worker-graph-structure-failed-base",
        status=graph_events.EVENT_STATUS_OBSERVED,
        operation_type="graph_structure",
        payload={"mode": "dry_run", "ai_output": "not json"},
        created_by="test",
    )
    conn.commit()

    semantic_worker._drain_graph_structure(PID, "worker-graph-structure-failed-base")

    request_after = graph_events.get_event(
        conn,
        PID,
        "worker-graph-structure-failed-base",
        request["event_id"],
    )
    assert request_after["status"] == graph_events.EVENT_STATUS_FAILED
    failed = graph_events.list_events(
        conn,
        PID,
        "worker-graph-structure-failed-base",
        event_types=["graph_structure_failed"],
    )
    assert len(failed) == 1
    assert failed[0]["evidence"]["errors"] == ["ai_output_json_invalid"]


def test_semantic_worker_graph_structure_bridge_rejects_missing_snapshot(conn):
    result = semantic_worker.handle_graph_structure_ai_output(
        PID,
        "missing-snapshot",
        raw_output="{}",
        mode="dry_run",
    )

    assert result["ok"] is False
    assert result["errors"] == ["snapshot_not_found"]
