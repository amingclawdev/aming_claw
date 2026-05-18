from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent.governance import graph_events
from agent.governance import graph_snapshot_store as store
from agent.governance import server
from agent.governance import semantic_worker
from agent.governance import state_reconcile
from agent.governance.db import _ensure_schema
from agent.governance.graph_structure_ops import SCHEMA_VERSION
from agent.governance.state_reconcile import run_state_only_full_reconcile


PID = "graph-structure-ops-api-test"


class _NoCloseConn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


def _ctx(snapshot_id: str, body: dict):
    return server.RequestContext(
        None,
        "POST",
        {"project_id": PID, "snapshot_id": snapshot_id},
        {},
        body,
        "req-graph-structure-ops-api-test",
        "",
        "",
    )


def _get_ctx(body: dict | None = None, query: dict | None = None):
    return server.RequestContext(
        None,
        "GET",
        {"project_id": PID},
        query or {},
        body or {},
        "req-graph-structure-ops-api-test",
        "",
        "",
    )


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    store.ensure_schema(c)
    monkeypatch.setattr(server, "get_connection", lambda _project_id: _NoCloseConn(c))
    monkeypatch.setattr("agent.governance.db.get_connection", lambda _project_id: _NoCloseConn(c))
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    yield c
    c.close()


def _graph() -> dict:
    return {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.1",
                    "layer": "L7",
                    "title": "Runtime",
                    "primary": ["agent/governance/server.py"],
                    "test": [],
                    "metadata": {},
                },
                {
                    "id": "L7.2",
                    "layer": "L7",
                    "title": "Ops",
                    "primary": ["agent/governance/graph_structure_ops.py"],
                    "test": [],
                    "metadata": {},
                },
            ],
            "edges": [],
        }
    }


def _create_snapshot(conn: sqlite3.Connection) -> str:
    graph = _graph()
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-ops-api-test",
        commit_sha="abc1234",
        snapshot_kind="scope",
        graph_json=graph,
        file_inventory=[
            {"path": "agent/governance/server.py", "file_kind": "source"},
            {"path": "agent/governance/graph_structure_ops.py", "file_kind": "source"},
        ],
        status=store.SNAPSHOT_STATUS_ACTIVE,
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=[],
    )
    conn.commit()
    return snapshot["snapshot_id"]


def _payload(snapshot_id: str, *, source_path: str = "agent/governance/server.py") -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "snapshot_id": snapshot_id,
            "base_commit": "abc1234",
            "analyzer_role": "reconcile_graph_structure_analyzer",
        },
        "operations": [
            {
                "op": "add_edge",
                "hint_id": "ai.edge.runtime-to-ops",
                "source_path": source_path,
                "target_node_id": "L7.2",
                "edge": "depends_on",
                "confidence": 0.82,
                "evidence": {"reason": "runtime imports graph ops gate"},
            }
        ],
        "self_check": {
            "valid": True,
            "checked_rules": ["hint-compatible-op", "snapshot-match"],
            "known_risks": [],
        },
    }


def _write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_generated_project(root) -> None:
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
    _write(root / "README.md", "# Generated Project\n")


def _service_node_id(snapshot_id: str) -> str:
    graph = state_reconcile._read_snapshot_graph(PID, snapshot_id)
    service_node = next(
        node for node in state_reconcile._deps_graph_nodes(graph)
        if "agent/service.py" in (node.get("primary") or [])
    )
    return state_reconcile._node_id(service_node)


def test_graph_structure_ops_dry_run_returns_projection_preview(conn):
    snapshot_id = _create_snapshot(conn)

    status, result = server.handle_graph_governance_snapshot_graph_structure_ops_dry_run(
        _ctx(snapshot_id, {"payload": _payload(snapshot_id)})
    )

    assert status == 200
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["mutated"] is False
    assert result["gate"]["accepted_count"] == 1
    assert result["projection"]["status"] == "ok"
    assert result["projection"]["materialized_count"] == 1
    assert result["projection"]["effect_counts"]["edges_added"] == 1


def test_graph_structure_ops_dry_run_rejects_invalid_payload_without_projection(conn):
    snapshot_id = _create_snapshot(conn)

    status, result = server.handle_graph_governance_snapshot_graph_structure_ops_dry_run(
        _ctx(snapshot_id, {"payload": _payload(snapshot_id, source_path="missing.py")})
    )

    assert status == 422
    assert result["ok"] is False
    assert result["dry_run"] is True
    assert result["mutated"] is False
    assert result["gate"]["rejected_count"] == 1
    assert result["gate"]["operations"][0]["errors"] == ["source_path_missing"]
    assert result["projection"]["status"] == "not_run"


def test_graph_structure_ops_ai_output_dry_run_rejects_malformed_output(conn):
    snapshot_id = _create_snapshot(conn)

    status, result = server.handle_graph_governance_snapshot_graph_structure_ops_ai_output(
        _ctx(snapshot_id, {"mode": "dry_run", "ai_output": "not json"})
    )

    assert status == 422
    assert result["ok"] is False
    assert result["mutated"] is False
    assert result["parse"]["errors"] == ["ai_output_json_invalid"]


def test_graph_structure_ops_ai_output_dry_run_accepts_raw_json(conn):
    snapshot_id = _create_snapshot(conn)

    status, result = server.handle_graph_governance_snapshot_graph_structure_ops_ai_output(
        _ctx(snapshot_id, {"mode": "dry_run", "ai_output": json.dumps(_payload(snapshot_id))})
    )

    assert status == 200
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["mutated"] is False
    assert result["gate"]["accepted_count"] == 1


def test_graph_structure_ops_jobs_enqueue_surfaces_in_operations_queue(conn, monkeypatch):
    snapshot_id = _create_snapshot(conn)
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "agent.governance.event_bus.publish",
        lambda topic, payload: published.append((topic, payload)),
    )

    status, result = server.handle_graph_governance_snapshot_graph_structure_ops_jobs_create(
        _ctx(snapshot_id, {"mode": "dry_run", "ai_output": json.dumps(_payload(snapshot_id))})
    )

    assert status == 202
    assert result["ok"] is True
    assert result["queued"] is True
    assert result["event"]["event_type"] == "graph_structure_requested"
    assert published == [
        (
            "semantic_job.enqueued",
            {
                "project_id": PID,
                "snapshot_id": snapshot_id,
                "target_scope": "graph_structure",
                "event_id": result["event"]["event_id"],
            },
        )
    ]

    queue = server.handle_graph_governance_operations_queue(
        _get_ctx(query={"snapshot_id": snapshot_id})
    )
    graph_ops = [
        op for op in queue["operations"]
        if op["operation_type"] == "graph_structure"
    ]
    assert len(graph_ops) == 1
    assert graph_ops[0]["status"] == "queued"
    assert queue["summary"]["by_type"]["graph_structure"] == 1
    assert queue["summary"]["graph_structure_jobs"]["by_status"]["observed"] == 1


def test_graph_structure_ops_jobs_selector_request_drains_through_ai_generated_project(
    conn,
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    base = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="graph-ops-selector-ai-base",
        commit_sha="selectorbase",
        snapshot_id="graph-ops-selector-ai-base",
        created_by="test",
    )
    assert base["ok"] is True
    service_id = _service_node_id("graph-ops-selector-ai-base")
    published: list[tuple[str, dict]] = []
    ai_calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "agent.governance.event_bus.publish",
        lambda topic, payload: published.append((topic, payload)),
    )

    def _stub_build_ai_call(*, semantic_config, project_id, snapshot_id, project_root):
        assert semantic_config.job_profile("graph_structure").analyzer_role == (
            "reconcile_graph_structure_analyzer"
        )
        assert project_id == PID
        assert snapshot_id == "graph-ops-selector-ai-base"
        assert Path(project_root) == project

        def _ai_call(stage: str, payload: dict):
            ai_calls.append((stage, payload))
            assert stage == "graph_structure"
            assert payload["operator_request"]["goal"] == "attach generated test to service"
            assert payload["selector"]["paths"] == ["agent/tests/test_service.py"]
            assert payload["output_contract"]["schema_version"] == SCHEMA_VERSION
            assert "agent/tests/test_service.py" in payload["inventory_paths"]
            return {
                "schema_version": SCHEMA_VERSION,
                "source": {
                    "snapshot_id": "graph-ops-selector-ai-base",
                    "base_commit": "selectorbase",
                    "analyzer_role": "reconcile_graph_structure_analyzer",
                },
                "operations": [
                    {
                        "op": "add_edge",
                        "hint_id": "api-selector-test-edge",
                        "source_path": "agent/tests/test_service.py",
                        "target_node_id": service_id,
                        "edge": "tests",
                        "confidence": 0.92,
                        "evidence": {"reason": "selector requested test binding"},
                    }
                ],
                "self_check": {
                    "valid": True,
                    "checked_rules": ["hint-compatible-op", "snapshot-match"],
                    "known_risks": [],
                },
            }

        return _ai_call

    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_ai.build_semantic_ai_call",
        _stub_build_ai_call,
    )
    status, queued = server.handle_graph_governance_snapshot_graph_structure_ops_jobs_create(
        _ctx(
            "graph-ops-selector-ai-base",
            {
                "mode": "accept",
                "project_root": str(project),
                "selector": {"paths": ["agent/tests/test_service.py"]},
                "operator_request": {"goal": "attach generated test to service"},
                "instructions": {"risk_tolerance": "low"},
            },
        )
    )

    assert status == 202
    event_payload = queued["event"]["payload"]
    assert "ai_output" not in event_payload
    assert event_payload["project_root"] == str(project.resolve())
    assert event_payload["selector"]["paths"] == ["agent/tests/test_service.py"]
    assert event_payload["operator_request"]["goal"] == "attach generated test to service"
    assert event_payload["instructions"]["risk_tolerance"] == "low"
    assert published[0][0] == "semantic_job.enqueued"

    semantic_worker._drain_graph_structure(PID, "graph-ops-selector-ai-base")

    assert len(ai_calls) == 1
    request_after = graph_events.get_event(
        conn,
        PID,
        "graph-ops-selector-ai-base",
        queued["event"]["event_id"],
    )
    assert request_after["status"] == graph_events.EVENT_STATUS_MATERIALIZED
    assert "api-selector-test-edge" in (
        project / "agent" / "tests" / "test_service.py"
    ).read_text(encoding="utf-8")

    materialized = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="graph-ops-selector-ai-materialized",
        commit_sha="selectorhead",
        snapshot_id="graph-ops-selector-ai-materialized",
        created_by="test",
    )
    assert materialized["ok"] is True
    graph = state_reconcile._read_snapshot_graph(PID, "graph-ops-selector-ai-materialized")
    assert any(
        edge.get("src") == "agent/tests/test_service.py"
        and edge.get("dst") == service_id
        and edge.get("edge_type") == "tests"
        and edge.get("direction") == "source_hint"
        for edge in state_reconcile._deps_graph_edges(graph)
    )


def test_graph_structure_ops_accept_writes_hint_in_generated_project_and_reconcile_materializes(
    conn,
    tmp_path,
):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    base = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="graph-ops-accept-base",
        commit_sha="acceptbase",
        snapshot_id="graph-ops-accept-base",
        created_by="test",
    )
    assert base["ok"] is True
    service_id = _service_node_id("graph-ops-accept-base")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "snapshot_id": "graph-ops-accept-base",
            "base_commit": "acceptbase",
            "analyzer_role": "reconcile_graph_structure_analyzer",
        },
        "operations": [
            {
                "op": "add_edge",
                "hint_id": "generated-project-test-edge",
                "source_path": "agent/tests/test_service.py",
                "target_node_id": service_id,
                "edge": "tests",
                "confidence": 0.91,
                "evidence": {"reason": "generated project test covers service entry"},
            }
        ],
        "self_check": {
            "valid": True,
            "checked_rules": ["hint-compatible-op", "snapshot-match"],
            "known_risks": [],
        },
    }

    status, accepted = server.handle_graph_governance_snapshot_graph_structure_ops_ai_output(
        _ctx(
            "graph-ops-accept-base",
            {
                "mode": "accept",
                "project_root": str(project),
                "ai_output": json.dumps(payload),
            },
        )
    )

    assert status == 200
    assert accepted["ok"] is True
    assert accepted["mutated"] is True
    assert accepted["requires_commit"] is True
    test_file = project / "agent" / "tests" / "test_service.py"
    text = test_file.read_text(encoding="utf-8")
    assert "aming-claw-hint:start id=\"generated-project-test-edge\"" in text
    assert "target=" + f'"{service_id}"' in text

    status, second = server.handle_graph_governance_snapshot_graph_structure_ops_accept(
        _ctx("graph-ops-accept-base", {"project_root": str(project), "payload": payload})
    )
    assert status == 200
    assert second["ok"] is True
    assert second["mutated"] is False
    assert second["write"]["skipped"][0]["reason"] == "already_present"

    materialized = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="graph-ops-accept-materialized",
        commit_sha="accepthead",
        snapshot_id="graph-ops-accept-materialized",
        created_by="test",
    )
    assert materialized["ok"] is True
    graph = state_reconcile._read_snapshot_graph(PID, "graph-ops-accept-materialized")
    assert any(
        edge.get("src") == "agent/tests/test_service.py"
        and edge.get("dst") == service_id
        and edge.get("edge_type") == "tests"
        and edge.get("direction") == "source_hint"
        for edge in state_reconcile._deps_graph_edges(graph)
    )
