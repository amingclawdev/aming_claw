from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent.governance import graph_events
from agent.governance import graph_snapshot_store as store
from agent.governance import reconcile_semantic_enrichment as semantic
from agent.governance import semantic_graph_structure_bridge as bridge
from agent.governance import semantic_worker
from agent.governance import server
from agent.governance import state_reconcile
from agent.governance.db import _ensure_schema
from agent.governance.reconcile_semantic_config import PROJECT_OVERRIDE_PATH
from agent.governance.state_reconcile import run_state_only_full_reconcile


PID = "semantic-graph-structure-bridge-test"


class _NoCloseConn:
    def __init__(self, raw: sqlite3.Connection):
        self._raw = raw

    def __getattr__(self, name: str):
        return getattr(self._raw, name)

    def close(self) -> None:
        pass


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    store.ensure_schema(c)
    graph_events.ensure_schema(c)
    monkeypatch.setattr(server, "get_connection", lambda _project_id: _NoCloseConn(c))
    monkeypatch.setattr("agent.governance.db.get_connection", lambda _project_id: _NoCloseConn(c))
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    yield c
    c.close()


def _ctx(snapshot_id: str, body: dict):
    return server.RequestContext(
        None,
        "POST",
        {"project_id": PID, "snapshot_id": snapshot_id},
        {},
        body,
        "req-semantic-graph-structure-bridge-test",
        "",
        "",
    )


def _get_ctx(snapshot_id: str):
    return server.RequestContext(
        None,
        "GET",
        {"project_id": PID},
        {"snapshot_id": snapshot_id},
        {},
        "req-semantic-graph-structure-bridge-test",
        "",
        "",
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_generated_project(root: Path) -> None:
    _write(root / "agent" / "__init__.py", "")
    _write(
        root / "agent" / "api.py",
        "from agent.storage import load_state\n\n"
        "def api_entry():\n"
        "    return load_state()['status']\n",
    )
    _write(
        root / "agent" / "storage.py",
        "def load_state():\n"
        "    return {'status': 'ok'}\n",
    )
    _write(
        root / "agent" / "service.py",
        "from agent.storage import load_state\n\n"
        "def service_entry():\n"
        "    return load_state()['status']\n",
    )
    _write(
        root / "agent" / "tests" / "test_service.py",
        "from agent.service import service_entry\n\n"
        "def test_service_entry():\n"
        "    assert service_entry() == 'ok'\n",
    )


def _create_snapshot(conn: sqlite3.Connection, project: Path, snapshot_id: str) -> str:
    result = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id=snapshot_id,
        commit_sha=f"{snapshot_id}-commit",
        snapshot_id=snapshot_id,
        created_by="test",
    )
    assert result["ok"] is True
    return snapshot_id


def _node_id_for_primary(snapshot_id: str, primary_path: str) -> str:
    graph = state_reconcile._read_snapshot_graph(PID, snapshot_id)
    for node in state_reconcile._deps_graph_nodes(graph):
        if primary_path in (node.get("primary") or node.get("primary_files") or []):
            return state_reconcile._node_id(node)
    raise AssertionError(f"node not found for primary path: {primary_path}")


def _semantic_event(
    conn: sqlite3.Connection,
    snapshot_id: str,
    node_id: str,
    semantic_payload: dict,
    *,
    event_id: str = "sem-bridge-node",
) -> dict:
    event = graph_events.create_event(
        conn,
        PID,
        snapshot_id,
        event_id=event_id,
        event_type="semantic_node_enriched",
        event_kind="imported_semantic_cache",
        target_type="node",
        target_id=node_id,
        status=graph_events.EVENT_STATUS_PROPOSED,
        operation_type="ai_enrich",
        payload={"semantic_payload": {"node_id": node_id, **semantic_payload}},
        evidence={"source": "test"},
        created_by="test",
    )
    conn.commit()
    return event


def _config_ops_payload() -> dict:
    return {
        "schema_version": "graph_enrich_config_ops.v1",
        "source": {
            "analyzer_role": "reconcile_graph_enrich_config_analyzer",
        },
        "operations": [
            {
                "op": "upsert_edge_evidence_policy",
                "rule_id": "calls-import-only-downgrade",
                "edge": "calls",
                "source_evidence": "import_only",
                "action": "downgrade",
                "downgrade_to": "imports",
                "confidence": 0.93,
                "evidence": {"reason": "import-only references should become imports edges"},
            }
        ],
        "self_check": {
            "valid": True,
            "checked_rules": ["schema_version", "op_supported", "action_supported"],
            "known_risks": [],
        },
    }


def test_bridge_converts_structured_semantic_dependency_to_gate_job(conn, tmp_path):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-dependency")
    service_id = _node_id_for_primary(snapshot_id, "agent/service.py")
    storage_id = _node_id_for_primary(snapshot_id, "agent/storage.py")
    event = _semantic_event(
        conn,
        snapshot_id,
        service_id,
        {
            "dependency_patch_suggestions": [
                {
                    "kind": "add_depends_on",
                    "target": "agent/storage.py",
                    "confidence": 0.91,
                    "reason": "service imports load_state from storage",
                }
            ],
        },
    )

    result = bridge.bridge_semantic_events_to_graph_structure_jobs(
        conn,
        PID,
        snapshot_id,
        event_ids=[event["event_id"]],
        actor="test",
    )
    conn.commit()

    assert result["ok"] is True
    assert result["queued_count"] == 1
    request = result["events"][0]
    assert request["event_type"] == "graph_structure_requested"
    assert request["source_event_id"] == event["event_id"]
    ai_output = request["payload"]["ai_output"]
    assert ai_output["schema_version"] == "graph_structure_ops.v1"
    assert ai_output["bridge"]["converted_count"] == 1
    assert ai_output["bridge"]["skipped_count"] == 0
    operation = ai_output["operations"][0]
    assert operation["op"] == "add_edge"
    assert operation["source_path"] == "agent/service.py"
    assert operation["target_node_id"] == storage_id
    assert operation["edge"] == "depends_on"

    semantic_worker._drain_graph_structure(PID, snapshot_id)

    request_after = graph_events.get_event(conn, PID, snapshot_id, request["event_id"])
    assert request_after["status"] == graph_events.EVENT_STATUS_MATERIALIZED
    completed = graph_events.list_events(
        conn,
        PID,
        snapshot_id,
        event_types=["graph_structure_completed"],
    )
    gated = [
        event for event in completed
        if event.get("source_event_id") == request["event_id"]
    ]
    assert len(gated) == 1
    gate_result = gated[0]["payload"]["result"]
    assert gate_result["ok"] is True
    assert gate_result["mutated"] is False
    assert gate_result["gate"]["accepted_count"] == 1


def test_bridge_resolves_explicit_source_module_for_cross_node_dependency(conn, tmp_path):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-cross-source")
    service_id = _node_id_for_primary(snapshot_id, "agent/service.py")
    storage_id = _node_id_for_primary(snapshot_id, "agent/storage.py")
    event = _semantic_event(
        conn,
        snapshot_id,
        service_id,
        {
            "dependency_patch_suggestions": [
                {
                    "kind": "add_depends_on",
                    "source": "agent.api",
                    "target": "agent/storage.py",
                    "confidence": 0.91,
                    "reason": "api imports load_state from storage",
                }
            ],
        },
        event_id="sem-bridge-cross-source",
    )

    result = bridge.bridge_semantic_events_to_graph_structure_jobs(
        conn,
        PID,
        snapshot_id,
        event_ids=[event["event_id"]],
        actor="test",
    )

    assert result["ok"] is True
    assert result["queued_count"] == 1
    operation = result["events"][0]["payload"]["ai_output"]["operations"][0]
    assert operation["source_path"] == "agent/api.py"
    assert operation["target_node_id"] == storage_id
    assert operation["edge"] == "depends_on"


def test_bridge_converts_called_by_suggestion_to_calls_from_explicit_source(conn, tmp_path):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-called-by")
    storage_id = _node_id_for_primary(snapshot_id, "agent/storage.py")
    event = _semantic_event(
        conn,
        snapshot_id,
        storage_id,
        {
            "dependency_patch_suggestions": [
                {
                    "kind": "add_called_by",
                    "source": "agent.service",
                    "target": "agent.storage",
                    "confidence": 0.87,
                    "reason": "service_entry calls load_state from storage",
                }
            ],
        },
        event_id="sem-bridge-called-by",
    )

    result = bridge.bridge_semantic_events_to_graph_structure_jobs(
        conn,
        PID,
        snapshot_id,
        event_ids=[event["event_id"]],
        actor="test",
    )

    assert result["ok"] is True
    assert result["queued_count"] == 1
    operation = result["events"][0]["payload"]["ai_output"]["operations"][0]
    assert operation["source_path"] == "agent/service.py"
    assert operation["target_node_id"] == storage_id
    assert operation["edge"] == "calls"


def test_bridge_rejects_unresolved_explicit_source_instead_of_fallback(conn, tmp_path):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-unresolved-source")
    service_id = _node_id_for_primary(snapshot_id, "agent/service.py")
    event = _semantic_event(
        conn,
        snapshot_id,
        service_id,
        {
            "dependency_patch_suggestions": [
                {
                    "kind": "add_depends_on",
                    "source": "agent.missing",
                    "target": "agent.storage",
                    "confidence": 0.91,
                    "reason": "source cannot be resolved",
                }
            ],
        },
        event_id="sem-bridge-unresolved-source",
    )

    result = bridge.bridge_semantic_events_to_graph_structure_jobs(
        conn,
        PID,
        snapshot_id,
        event_ids=[event["event_id"]],
        actor="test",
    )

    assert result["ok"] is True
    assert result["queued_count"] == 0
    assert result["audit_event_count"] == 1
    assert result["skipped_count"] == 1
    assert result["skipped"][0]["reason"] == "source_path_unresolved"


def test_bridge_audits_malformed_or_unsupported_semantic_suggestions(conn, tmp_path):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-malformed")
    service_id = _node_id_for_primary(snapshot_id, "agent/service.py")
    event = _semantic_event(
        conn,
        snapshot_id,
        service_id,
        {
            "health_issues": [
                "{'kind': 'split_node', 'summary': 'service might contain two concerns'}",
                "{not valid",
            ],
        },
        event_id="sem-bridge-malformed",
    )

    result = bridge.bridge_semantic_events_to_graph_structure_jobs(
        conn,
        PID,
        snapshot_id,
        event_ids=[event["event_id"]],
        actor="test",
    )
    conn.commit()

    assert result["ok"] is True
    assert result["queued_count"] == 0
    assert result["audit_event_count"] == 1
    assert result["skipped_count"] == 2
    reasons = {skip["reason"] for skip in result["skipped"]}
    assert reasons == {"unsupported_suggestion_kind", "suggestion_parse_error"}
    audit = result["events"][0]
    assert audit["event_type"] == "graph_structure_completed"
    assert audit["status"] == graph_events.EVENT_STATUS_MATERIALIZED

    queue = server.handle_graph_governance_operations_queue(_get_ctx(snapshot_id))
    assert [
        op for op in queue["operations"]
        if op["operation_type"] == "graph_structure"
    ] == []


def test_semantic_enrichment_persists_structured_graph_suggestions_for_bridge(conn, tmp_path):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-persisted")
    service_id = _node_id_for_primary(snapshot_id, "agent/service.py")
    storage_id = _node_id_for_primary(snapshot_id, "agent/storage.py")

    def fake_ai(stage: str, payload: dict) -> dict:
        assert stage == "reconcile_semantic_feature"
        assert payload["feature"]["node_id"] == service_id
        return {
            "feature_name": "Service Runtime",
            "semantic_summary": "Service runtime delegates state loading to storage.",
            "intent": "Expose a small service entrypoint.",
            "domain_label": "runtime",
            "doc_coverage_review": {"bound": False, "action": "none"},
            "test_coverage_review": {"bound": True, "action": "keep"},
            "config_coverage_review": {"bound": False, "action": "none"},
            "graph_structure_suggestions": [
                {
                    "op": "add_edge",
                    "source_path": "agent/service.py",
                    "target": "agent/storage.py",
                    "edge": "depends_on",
                    "confidence": 0.88,
                    "evidence": {"reason": "service imports load_state"},
                }
            ],
        }

    result = semantic.run_semantic_enrichment(
        conn,
        PID,
        snapshot_id,
        project,
        use_ai=True,
        ai_call=fake_ai,
        semantic_ai_scope="selected",
        semantic_node_ids=[service_id],
        submit_for_review=True,
        created_by="test",
    )
    assert result["summary"]["ai_complete_count"] == 1
    row = conn.execute(
        """
        SELECT semantic_json FROM graph_semantic_nodes
        WHERE project_id=? AND snapshot_id=? AND node_id=?
        """,
        (PID, snapshot_id, service_id),
    ).fetchone()
    persisted = json.loads(row["semantic_json"])
    assert persisted["graph_structure_suggestions"][0]["source_path"] == "agent/service.py"

    graph_events.backfill_existing_semantic_events(conn, PID, snapshot_id, actor="test")
    event = graph_events.list_events(
        conn,
        PID,
        snapshot_id,
        event_types=["semantic_node_enriched"],
        statuses=[graph_events.EVENT_STATUS_PROPOSED],
        target_type="node",
        target_id=service_id,
    )[0]

    bridge_result = bridge.bridge_semantic_events_to_graph_structure_jobs(
        conn,
        PID,
        snapshot_id,
        event_ids=[event["event_id"]],
        actor="test",
    )

    assert bridge_result["queued_count"] == 1
    operation = bridge_result["events"][0]["payload"]["ai_output"]["operations"][0]
    assert operation["target_node_id"] == storage_id
    assert operation["edge"] == "depends_on"


def test_semantic_graph_structure_candidates_api_surfaces_queue_operation(
    conn,
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-api")
    service_id = _node_id_for_primary(snapshot_id, "agent/service.py")
    event = _semantic_event(
        conn,
        snapshot_id,
        service_id,
        {
            "graph_structure_suggestions": [
                {
                    "op": "add_edge",
                    "source_path": "agent/service.py",
                    "target": "agent/storage.py",
                    "edge": "depends_on",
                    "confidence": 0.82,
                    "evidence": {"reason": "service uses storage"},
                }
            ],
        },
        event_id="sem-bridge-api",
    )
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "agent.governance.event_bus.publish",
        lambda topic, payload: published.append((topic, payload)),
    )

    status, result = server.handle_graph_governance_snapshot_semantic_graph_structure_candidates(
        _ctx(snapshot_id, {"semantic_event_ids": [event["event_id"]], "actor": "test"})
    )

    assert status == 202
    assert result["ok"] is True
    assert result["queued"] is True
    assert result["queued_count"] == 1
    assert result["published_count"] == 1
    assert published == [
        (
            "semantic_job.enqueued",
            {
                "project_id": PID,
                "snapshot_id": snapshot_id,
                "target_scope": "graph_structure",
                "event_id": result["events"][0]["event_id"],
            },
        )
    ]

    queue = server.handle_graph_governance_operations_queue(_get_ctx(snapshot_id))
    graph_ops = [
        op for op in queue["operations"]
        if op["operation_type"] == "graph_structure"
    ]
    assert len(graph_ops) == 1
    assert graph_ops[0]["status"] == "queued"
    assert graph_ops[0]["source_event_id"] == event["event_id"]
    assert queue["summary"]["by_type"]["graph_structure"] == 1
    assert queue["summary"]["graph_structure_jobs"]["by_status"]["observed"] == 1


def test_bridge_converts_semantic_graph_enrich_config_suggestion_to_dry_run_job(
    conn,
    tmp_path,
):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-config")
    service_id = _node_id_for_primary(snapshot_id, "agent/service.py")
    event = _semantic_event(
        conn,
        snapshot_id,
        service_id,
        {
            "graph_enrich_config_suggestions": [
                {
                    "op": "upsert_edge_evidence_policy",
                    "rule_id": "calls-import-only-downgrade",
                    "edge": "calls",
                    "source_evidence": "import_only",
                    "action": "downgrade",
                    "downgrade_to": "imports",
                    "confidence": 0.91,
                    "evidence": {
                        "reason": "AI found import-only call suggestions should be downgraded",
                    },
                }
            ],
        },
        event_id="sem-bridge-config",
    )

    result = bridge.bridge_semantic_events_to_graph_enrich_config_jobs(
        conn,
        PID,
        snapshot_id,
        event_ids=[event["event_id"]],
        actor="test",
        project_root=str(project),
    )
    conn.commit()

    assert result["ok"] is True
    assert result["queued_count"] == 1
    request = result["events"][0]
    assert request["event_type"] == "graph_enrich_config_requested"
    assert request["operation_type"] == "graph_enrich_config"
    operation = request["payload"]["ai_output"]["operations"][0]
    assert operation["op"] == "upsert_edge_evidence_policy"
    assert operation["edge"] == "calls"
    assert operation["source_evidence"] == "import_only"

    semantic_worker._drain_graph_enrich_config(PID, snapshot_id)

    request_after = graph_events.get_event(conn, PID, snapshot_id, request["event_id"])
    assert request_after["status"] == graph_events.EVENT_STATUS_MATERIALIZED
    completed = graph_events.list_events(
        conn,
        PID,
        snapshot_id,
        event_types=["graph_enrich_config_completed"],
    )
    assert len(completed) == 1
    gate_result = completed[0]["payload"]["result"]
    assert gate_result["ok"] is True
    assert gate_result["mutated"] is False
    assert gate_result["preview"]["config_path"] == str(project / PROJECT_OVERRIDE_PATH)
    assert not (project / PROJECT_OVERRIDE_PATH).exists()


def test_semantic_worker_drains_graph_enrich_config_accept_job_generated_project(
    conn,
    tmp_path,
):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-config-accept")
    request = graph_events.create_event(
        conn,
        PID,
        snapshot_id,
        event_type="graph_enrich_config_requested",
        event_kind="semantic_job",
        target_type="project",
        target_id=PID,
        status=graph_events.EVENT_STATUS_OBSERVED,
        operation_type="graph_enrich_config",
        payload={
            "mode": "accept",
            "ai_output": _config_ops_payload(),
            "project_root": str(project),
        },
        created_by="test",
    )
    conn.commit()

    semantic_worker._drain_graph_enrich_config(PID, snapshot_id)

    request_after = graph_events.get_event(conn, PID, snapshot_id, request["event_id"])
    assert request_after["status"] == graph_events.EVENT_STATUS_MATERIALIZED
    override_path = project / PROJECT_OVERRIDE_PATH
    assert override_path.exists()
    assert "import_only_action: downgrade" in override_path.read_text(encoding="utf-8")


def test_semantic_enrichment_persists_graph_enrich_config_suggestions_for_bridge(
    conn,
    tmp_path,
):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-config-persisted")
    service_id = _node_id_for_primary(snapshot_id, "agent/service.py")

    def fake_ai(stage: str, payload: dict) -> dict:
        assert stage == "reconcile_semantic_feature"
        assert payload["feature"]["node_id"] == service_id
        return {
            "feature_name": "Service Runtime",
            "semantic_summary": "Service runtime uses storage through imports.",
            "intent": "Expose a small service entrypoint.",
            "domain_label": "runtime",
            "doc_coverage_review": {"bound": False, "action": "none"},
            "test_coverage_review": {"bound": True, "action": "keep"},
            "config_coverage_review": {"bound": False, "action": "none"},
            "graph_enrich_config_suggestions": [
                {
                    "op": "upsert_edge_evidence_policy",
                    "rule_id": "calls-import-only-downgrade",
                    "edge": "calls",
                    "source_evidence": "import_only",
                    "action": "downgrade",
                    "downgrade_to": "imports",
                    "confidence": 0.89,
                    "evidence": {"reason": "import-only references should not be calls"},
                }
            ],
        }

    result = semantic.run_semantic_enrichment(
        conn,
        PID,
        snapshot_id,
        project,
        use_ai=True,
        ai_call=fake_ai,
        semantic_ai_scope="selected",
        semantic_node_ids=[service_id],
        submit_for_review=True,
        created_by="test",
    )
    assert result["summary"]["ai_complete_count"] == 1
    row = conn.execute(
        """
        SELECT semantic_json FROM graph_semantic_nodes
        WHERE project_id=? AND snapshot_id=? AND node_id=?
        """,
        (PID, snapshot_id, service_id),
    ).fetchone()
    persisted = json.loads(row["semantic_json"])
    assert persisted["graph_enrich_config_suggestions"][0]["edge"] == "calls"

    graph_events.backfill_existing_semantic_events(conn, PID, snapshot_id, actor="test")
    event = graph_events.list_events(
        conn,
        PID,
        snapshot_id,
        event_types=["semantic_node_enriched"],
        statuses=[graph_events.EVENT_STATUS_PROPOSED],
        target_type="node",
        target_id=service_id,
    )[0]

    bridge_result = bridge.bridge_semantic_events_to_graph_enrich_config_jobs(
        conn,
        PID,
        snapshot_id,
        event_ids=[event["event_id"]],
        actor="test",
        project_root=str(project),
    )

    assert bridge_result["queued_count"] == 1
    operation = bridge_result["events"][0]["payload"]["ai_output"]["operations"][0]
    assert operation["source_evidence"] == "import_only"


def test_semantic_graph_enrich_config_candidates_api_surfaces_queue_operation(
    conn,
    tmp_path,
    monkeypatch,
):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-config-api")
    service_id = _node_id_for_primary(snapshot_id, "agent/service.py")
    event = _semantic_event(
        conn,
        snapshot_id,
        service_id,
        {
            "graph_enrich_config_ops": _config_ops_payload(),
        },
        event_id="sem-bridge-config-api",
    )
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "agent.governance.event_bus.publish",
        lambda topic, payload: published.append((topic, payload)),
    )

    status, result = server.handle_graph_governance_snapshot_semantic_graph_enrich_config_candidates(
        _ctx(
            snapshot_id,
            {
                "semantic_event_ids": [event["event_id"]],
                "actor": "test",
                "project_root": str(project),
            },
        )
    )

    assert status == 202
    assert result["ok"] is True
    assert result["queued"] is True
    assert result["queued_count"] == 1
    assert result["published_count"] == 1
    assert published == [
        (
            "semantic_job.enqueued",
            {
                "project_id": PID,
                "snapshot_id": snapshot_id,
                "target_scope": "graph_enrich_config",
                "event_id": result["events"][0]["event_id"],
            },
        )
    ]

    queue = server.handle_graph_governance_operations_queue(_get_ctx(snapshot_id))
    config_ops = [
        op for op in queue["operations"]
        if op["operation_type"] == "graph_enrich_config"
    ]
    assert len(config_ops) == 1
    assert config_ops[0]["status"] == "queued"
    assert config_ops[0]["source_event_id"] == event["event_id"]
    assert queue["summary"]["by_type"]["graph_enrich_config"] == 1
    assert queue["summary"]["graph_enrich_config_jobs"]["by_status"]["observed"] == 1
