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
    assert "calls_require_concrete_evidence" in ai_output["self_check"]["checked_rules"]
    assert ai_output["bridge"]["self_precheck"]["max_repair_attempts"] == 1
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
    assert gate_result["precheck"]["status"] == "passed"
    assert gate_result["mutated"] is False
    assert gate_result["gate"]["accepted_count"] == 1


def test_bridge_aliases_imports_module_graph_structure_edge(conn, tmp_path):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-imports-module")
    service_id = _node_id_for_primary(snapshot_id, "agent/service.py")
    storage_id = _node_id_for_primary(snapshot_id, "agent/storage.py")
    event = _semantic_event(
        conn,
        snapshot_id,
        service_id,
        {
            "graph_structure_suggestions": [
                {
                    "op": "add_edge",
                    "edge": "imports_module",
                    "source_path": "agent/service.py",
                    "target": "agent/storage.py",
                    "confidence": 0.9,
                    "evidence": "from agent.storage import load_state",
                }
            ],
        },
        event_id="sem-bridge-imports-module",
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
    assert result["skipped_count"] == 0
    operation = result["events"][0]["payload"]["ai_output"]["operations"][0]
    assert operation["edge"] == "imports"
    assert operation["source_path"] == "agent/service.py"
    assert operation["target_node_id"] == storage_id


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
                    "evidence": {
                        "source_evidence": "function_call",
                        "line_evidence": "return load_state()['status']",
                    },
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
    assert operation["evidence"]["source_evidence"] == "function_call"


@pytest.mark.parametrize(
    ("evidence", "expected_source_evidence"),
    [
        (
            [
                {
                    "kind": "resolved_call",
                    "caller": "agent.service::service_entry",
                    "callee": "agent.storage::load_state",
                    "line_evidence": "return load_state()['status']",
                }
            ],
            "resolved_call",
        ),
        (
            json.dumps(
                [
                    {
                        "type": "function_calls",
                        "caller": "agent.service::service_entry",
                        "callee": "agent.storage::load_state",
                        "line_evidence": "return load_state()['status']",
                    }
                ]
            ),
            "function_calls",
        ),
    ],
)
def test_bridge_recognizes_structured_ai_call_evidence_on_direct_ops(
    conn,
    tmp_path,
    evidence,
    expected_source_evidence,
):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(
        conn,
        project,
        f"semantic-bridge-ai-call-{expected_source_evidence}",
    )
    service_id = _node_id_for_primary(snapshot_id, "agent/service.py")
    storage_id = _node_id_for_primary(snapshot_id, "agent/storage.py")
    event = _semantic_event(
        conn,
        snapshot_id,
        service_id,
        {
            "graph_structure_suggestions": [
                {
                    "op": "add_edge",
                    "source_path": "agent/service.py",
                    "target_node_id": storage_id,
                    "edge": "calls",
                    "confidence": 0.89,
                    "evidence": evidence,
                }
            ],
        },
        event_id=f"sem-bridge-ai-call-{expected_source_evidence}",
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
    assert operation["evidence"]["source_evidence"] == expected_source_evidence
    assert operation["evidence"]["bridge_policy"] == "calls_concrete_evidence_present"
    assert (
        operation["evidence"]["evidence_items"][0]["line_evidence"]
        == "return load_state()['status']"
    )


def test_bridge_downgrades_weak_called_by_suggestion_before_gate(conn, tmp_path):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-weak-called-by")
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
                    "confidence": 0.72,
                    "reason": "storage appears to be used from service, but no call site was provided",
                }
            ],
        },
        event_id="sem-bridge-weak-called-by",
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
    operation = result["events"][0]["payload"]["ai_output"]["operations"][0]
    assert operation["source_path"] == "agent/service.py"
    assert operation["target_node_id"] == storage_id
    assert operation["edge"] == "imports"
    assert operation["evidence"]["original_edge"] == "calls"
    assert operation["evidence"]["bridge_policy"] == "calls_weak_evidence_downgraded_to_imports"
    assert (
        result["events"][0]["payload"]["ai_output"]["bridge"]["policy"]["calls"]["downgrade_to"]
        == "imports"
    )

    semantic_worker._drain_graph_structure(PID, snapshot_id)

    completed = graph_events.list_events(
        conn,
        PID,
        snapshot_id,
        event_types=["graph_structure_completed"],
    )
    gate_result = completed[0]["payload"]["result"]
    assert gate_result["ok"] is True
    assert gate_result["gate"]["accepted_count"] == 1
    assert gate_result["gate"]["operations"][0]["edge"] == "imports"


def test_bridge_uses_project_bridge_policy_for_weak_calls(conn, tmp_path):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    override_path = project / PROJECT_OVERRIDE_PATH
    override_path.parent.mkdir(parents=True)
    override_path.write_text(
        "\n".join(
            [
                "graph_structure_ops:",
                "  bridge_policy:",
                "    calls:",
                "      weak_evidence_action: downgrade",
                "      downgrade_to: depends_on",
            ]
        ),
        encoding="utf-8",
    )
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-policy-called-by")
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
                    "confidence": 0.72,
                    "reason": "storage is related to service without concrete call evidence",
                }
            ],
        },
        event_id="sem-bridge-policy-called-by",
    )

    result = bridge.bridge_semantic_events_to_graph_structure_jobs(
        conn,
        PID,
        snapshot_id,
        event_ids=[event["event_id"]],
        actor="test",
        project_root=project,
    )

    assert result["ok"] is True
    assert result["queued_count"] == 1
    operation = result["events"][0]["payload"]["ai_output"]["operations"][0]
    assert operation["target_node_id"] == storage_id
    assert operation["edge"] == "depends_on"
    assert operation["evidence"]["bridge_policy"] == "calls_weak_evidence_downgraded_to_depends_on"


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
    assert (
        "edge_supported_or_canonical_alias"
        in request["payload"]["ai_output"]["self_check"]["checked_rules"]
    )
    assert request["payload"]["ai_output"]["bridge"]["self_precheck"]["max_repair_attempts"] == 1
    assert operation["op"] == "upsert_edge_evidence_policy"
    assert operation["edge"] == "calls"
    assert operation["source_evidence"] == "import_only"

    semantic_worker._drain_graph_enrich_config(PID, snapshot_id)

    request_after = graph_events.get_event(conn, PID, snapshot_id, request["event_id"])
    assert request_after["status"] == graph_events.EVENT_STATUS_PROPOSED
    assert request_after["evidence"]["requires_observer_approval"] is True
    completed = graph_events.list_events(
        conn,
        PID,
        snapshot_id,
        event_types=["graph_enrich_config_completed"],
    )
    assert len(completed) == 1
    gate_result = completed[0]["payload"]["result"]
    assert gate_result["ok"] is True
    assert gate_result["precheck"]["status"] == "passed"
    assert gate_result["mutated"] is False
    assert gate_result["preview"]["config_path"] == str(project / PROJECT_OVERRIDE_PATH)
    assert not (project / PROJECT_OVERRIDE_PATH).exists()

    queue = server.handle_graph_governance_operations_queue(_get_ctx(snapshot_id))
    config_ops = [
        op for op in queue["operations"]
        if op["operation_type"] == "graph_enrich_config"
    ]
    assert {op["status"] for op in config_ops} == {"review_required"}
    assert all("observer_takeover" in op["supported_actions"] for op in config_ops)
    assert queue["summary"]["by_status"]["review_required"] == 2
    assert queue["summary"]["graph_enrich_config_jobs"]["by_status"] == {
        "observed": 1,
        "proposed": 1,
    }


def test_bridge_skips_graph_enrich_policy_ops_that_gate_would_reject(
    conn,
    tmp_path,
):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-config-policy-skip")
    service_id = _node_id_for_primary(snapshot_id, "agent/service.py")
    event = _semantic_event(
        conn,
        snapshot_id,
        service_id,
        {
            "graph_enrich_config_suggestions": [
                {
                    "op": "upsert_edge_evidence_policy",
                    "rule_id": "semantic_bridge.calls.require_concrete_evidence",
                    "edge": "calls",
                    "source_evidence": "function_calls",
                    "action": "require_direct_symbol_import",
                    "downgrade_to": "imports",
                    "confidence": 0.55,
                    "evidence": {
                        "reason": "Policy ops are strict; this should be proposed as a rule op.",
                    },
                }
            ],
        },
        event_id="sem-bridge-config-policy-skip",
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
    assert result["queued_count"] == 0
    assert result["audit_event_count"] == 1
    assert result["skipped_count"] == 1
    skip = result["skipped"][0]
    assert skip["reason"] == "source_evidence_unsupported_for_policy"
    assert skip["errors"] == [
        "source_evidence_unsupported_for_policy",
        "action_unsupported_for_policy",
    ]
    audit = result["events"][0]
    assert audit["event_type"] == "graph_enrich_config_completed"
    assert audit["payload"]["result"]["status"] == "skipped"
    assert audit["payload"]["result"]["converted_count"] == 0


def test_bridge_skips_graph_enrich_policy_ops_for_non_calls_edges(
    conn,
    tmp_path,
):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-config-policy-edge-skip")
    service_id = _node_id_for_primary(snapshot_id, "agent/service.py")
    event = _semantic_event(
        conn,
        snapshot_id,
        service_id,
        {
            "graph_enrich_config_suggestions": [
                {
                    "op": "upsert_edge_evidence_policy",
                    "rule_id": "depends_on.import_only.downgrade",
                    "edge": "depends_on",
                    "source_evidence": "import_only",
                    "action": "downgrade",
                    "downgrade_to": "imports",
                    "confidence": 0.55,
                    "evidence": {
                        "reason": "Policy ops only materialize calls/import_only evidence policy.",
                    },
                }
            ],
        },
        event_id="sem-bridge-config-policy-edge-skip",
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

    assert result["queued_count"] == 0
    assert result["audit_event_count"] == 1
    skip = result["skipped"][0]
    assert skip["reason"] == "edge_unsupported_for_policy"
    assert skip["errors"] == ["edge_unsupported_for_policy"]


def test_bridge_converts_semantic_rule_config_suggestions_to_dry_run_job(
    conn,
    tmp_path,
):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-config-rules")
    service_id = _node_id_for_primary(snapshot_id, "agent/service.py")
    event = _semantic_event(
        conn,
        snapshot_id,
        service_id,
        {
            "graph_enrich_config_suggestions": [
                {
                    "op": "review_rule",
                    "rule_id": "weak_call_resolver.ambiguous_add",
                    "edge": "calls",
                    "source_evidence": "weak_call_resolver.ambiguous_add",
                    "action": "downgrade",
                    "downgrade_to": "ignored",
                    "confidence": 0.84,
                    "evidence": {
                        "reason": "Weak resolver additions should be ignored until stronger evidence exists.",
                    },
                },
                {
                    "op": "add_rule",
                    "rule_id": "event_bus.subscribe_to_consumes_event",
                    "edge": "consumes_event",
                    "source_evidence": "event_bus.subscribe",
                    "action": "add",
                    "confidence": 0.88,
                    "evidence": {
                        "reason": "Subscribers consume the event stream they register for.",
                    },
                },
            ],
        },
        event_id="sem-bridge-config-rules",
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
    assert result["skipped_count"] == 0
    request = result["events"][0]
    operations = request["payload"]["ai_output"]["operations"]
    assert [operation["op"] for operation in operations] == ["review_rule", "add_rule"]

    semantic_worker._drain_graph_enrich_config(PID, snapshot_id)

    completed = graph_events.list_events(
        conn,
        PID,
        snapshot_id,
        event_types=["graph_enrich_config_completed"],
    )
    gate_result = completed[0]["payload"]["result"]
    assert gate_result["ok"] is True
    rules = gate_result["preview"]["graph_enrich_config_ops"]["rules"]
    assert rules["weak_call_resolver.ambiguous_add"]["action"] == "ignore"
    assert rules["event_bus.subscribe_to_consumes_event"]["edge"] == "consumes_event"


def test_bridge_prechecks_graph_enrich_config_predicate_guards_before_queue(
    conn,
    tmp_path,
):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-config-guard-precheck")
    service_id = _node_id_for_primary(snapshot_id, "agent/service.py")
    event = _semantic_event(
        conn,
        snapshot_id,
        service_id,
        {
            "graph_enrich_config_suggestions": [
                {
                    "op": "tighten_rule",
                    "rule_id": "emits_event.string_literal.cli_binary_filenames",
                    "edge": "emits_event",
                    "source_evidence": "string_literal",
                    "action": "ignore",
                    "confidence": 0.85,
                    "when": {
                        "all": [
                            {"predicate": "source_evidence_is", "value": "string_literal"},
                            {
                                "predicate": "raw_target_in",
                                "values": ["claude.cmd", "codex.ps1"],
                            },
                        ]
                    },
                },
                {
                    "op": "tighten_rule",
                    "rule_id": "emits_event.string_literal.executable_extensions_python",
                    "edge": "emits_event",
                    "source_evidence": "string_literal",
                    "action": "ignore",
                    "confidence": 0.7,
                    "when": {
                        "all": [
                            {"predicate": "source_evidence_is", "value": "string_literal"},
                            {"predicate": "language_is", "value": "python"},
                        ]
                    },
                },
                {
                    "op": "review_rule",
                    "rule_id": "weak_calls_add_short_name_in_reconcile",
                    "edge": "calls",
                    "source_evidence": "weak_call_resolver_ambiguous_short_name",
                    "action": "downgrade",
                    "downgrade_to": "drop",
                    "confidence": 0.5,
                    "when": {
                        "all": [
                            {
                                "predicate": "source_evidence_is",
                                "value": "weak_call_resolver_ambiguous_short_name",
                            },
                            {"predicate": "raw_target_in", "values": ["add"]},
                        ]
                    },
                },
                {
                    "op": "review_rule",
                    "rule_id": "weak_calls_container_add_attr_call",
                    "edge": "calls",
                    "source_evidence": "weak_call_resolver_ambiguous_short_name",
                    "action": "downgrade",
                    "downgrade_to": "drop",
                    "confidence": 0.6,
                    "when": {
                        "all": [
                            {
                                "predicate": "source_evidence_is",
                                "value": "weak_call_resolver_ambiguous_short_name",
                            },
                            {"predicate": "raw_target_in", "values": ["add"]},
                            {"predicate": "call_syntax_is", "value": "attribute_call"},
                            {"predicate": "receiver_kind_in", "values": ["set", "list"]},
                        ]
                    },
                },
            ],
        },
        event_id="sem-bridge-config-guard-precheck",
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
    assert result["skipped_count"] == 2
    skipped_reasons = {item["reason"] for item in result["skipped"]}
    assert skipped_reasons == {
        "predicate_underconstrained_string_literal",
        "predicate_underconstrained_weak_call",
    }
    request = result["events"][0]
    ai_output = request["payload"]["ai_output"]
    assert [operation["rule_id"] for operation in ai_output["operations"]] == [
        "emits_event.string_literal.cli_binary_filenames",
        "weak_calls_container_add_attr_call",
    ]
    assert "predicate_guard_string_literal_requires_raw_target" in ai_output["self_check"]["checked_rules"]
    assert ai_output["bridge"]["skipped_count"] == 2

    semantic_worker._drain_graph_enrich_config(PID, snapshot_id)

    completed = graph_events.list_events(
        conn,
        PID,
        snapshot_id,
        event_types=["graph_enrich_config_completed"],
    )
    gate_result = completed[0]["payload"]["result"]
    assert gate_result["ok"] is True
    assert gate_result["gate"]["accepted_count"] == 2
    assert gate_result["gate"]["rejected_count"] == 0


def test_bridge_dedupes_config_rule_from_ops_and_suggestions(
    conn,
    tmp_path,
):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-config-dedupe")
    service_id = _node_id_for_primary(snapshot_id, "agent/service.py")
    operation = {
        "op": "downgrade_relation_confidence",
        "rule_id": "calls.weak_resolver.ambiguous_add",
        "edge": "calls",
        "source_evidence": "weak_call_resolver_ambiguous_add",
        "action": "downgrade",
        "downgrade_to": "weak",
        "confidence": 0.8,
        "evidence": {
            "reason": "Ambiguous weak call resolver additions should not become calls edges.",
        },
    }
    event = _semantic_event(
        conn,
        snapshot_id,
        service_id,
        {
            "graph_enrich_config_ops": {"operations": [dict(operation)]},
            "graph_enrich_config_suggestions": [dict(operation)],
        },
        event_id="sem-bridge-config-dedupe",
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
    assert result["skipped_count"] == 1
    request = result["events"][0]
    ai_output = request["payload"]["ai_output"]
    assert len(ai_output["operations"]) == 1
    assert ai_output["operations"][0]["rule_id"] == "calls.weak_resolver.ambiguous_add"
    assert ai_output["bridge"]["skipped"][0]["reason"] == "rule_id_duplicate_deduped"

    semantic_worker._drain_graph_enrich_config(PID, snapshot_id)

    completed = graph_events.list_events(
        conn,
        PID,
        snapshot_id,
        event_types=["graph_enrich_config_completed"],
    )
    gate_result = completed[0]["payload"]["result"]
    assert gate_result["ok"] is True
    assert gate_result["gate"]["accepted_count"] == 1
    assert gate_result["gate"]["rejected_count"] == 0


def test_bridge_converts_flexible_config_rule_ops_and_ignores_empty_ops_object(
    conn,
    tmp_path,
):
    project = tmp_path / "generated-project"
    _write_generated_project(project)
    snapshot_id = _create_snapshot(conn, project, "semantic-bridge-config-flex")
    service_id = _node_id_for_primary(snapshot_id, "agent/service.py")
    event = _semantic_event(
        conn,
        snapshot_id,
        service_id,
        {
            "graph_structure_ops": {},
            "graph_enrich_config_ops": {},
            "graph_enrich_config_suggestions": [
                {
                    "op": "downgrade_relation_confidence",
                    "rule_id": "emits_event.string_literal",
                    "edge": "emits_event",
                    "source_evidence": "string literal",
                    "action": "downgrade",
                    "downgrade_to": "references_schema",
                    "confidence": 0.76,
                    "evidence": {
                        "reason": "Prompt contract literals are schema references, not runtime emits.",
                    },
                },
                {
                    "op": "tighten_rule",
                    "rule_id": "tests_edge_from_filename_match",
                    "edge": "tests",
                    "source_evidence": "test_import_fanin",
                    "action": "require_direct_symbol_import",
                    "downgrade_to": "weak_tests",
                    "confidence": 0.72,
                    "evidence": {
                        "reason": "Filename matches should remain weak until symbol imports prove coverage.",
                    },
                },
                {
                    "op": "update_rule",
                    "rule_id": "imports_module_from_top_level_from_import",
                    "edge": "imports_module",
                    "source_evidence": "import_only",
                    "action": "allow",
                    "confidence": 0.7,
                    "evidence": {
                        "reason": "Direct from-imports should normalize to the standard imports edge.",
                    },
                },
                {
                    "op": "tighten_rule",
                    "rule_id": "python.container_attribute_add_not_cross_module_call",
                    "edge": "calls",
                    "source_evidence": "weak_call_resolver_ambiguous_add",
                    "action": "ignore",
                    "confidence": 0.78,
                    "when": {
                        "all": [
                            {"predicate": "language_is", "value": "python"},
                            {"predicate": "call_syntax_is", "value": "attribute_call"},
                            {
                                "predicate": "receiver_kind_in",
                                "values": ["builtin_collection", "local_collection"],
                            },
                            {"predicate": "raw_target_in", "values": ["add"]},
                        ]
                    },
                    "evidence": {
                        "reason": "Container .add() weak calls should not become module calls.",
                    },
                },
            ],
        },
        event_id="sem-bridge-config-flex",
    )

    structure = bridge.bridge_semantic_events_to_graph_structure_jobs(
        conn,
        PID,
        snapshot_id,
        event_ids=[event["event_id"]],
        actor="test",
    )
    config = bridge.bridge_semantic_events_to_graph_enrich_config_jobs(
        conn,
        PID,
        snapshot_id,
        event_ids=[event["event_id"]],
        actor="test",
        project_root=str(project),
    )
    conn.commit()

    assert structure["queued_count"] == 0
    assert structure["skipped_count"] == 0
    assert config["queued_count"] == 1
    assert config["skipped_count"] == 0
    operations = config["events"][0]["payload"]["ai_output"]["operations"]
    assert [operation["op"] for operation in operations] == [
        "downgrade_relation_confidence",
        "tighten_rule",
        "update_rule",
        "tighten_rule",
    ]
    assert operations[2]["edge"] == "imports"
    assert operations[3]["when"]["all"][0] == {"predicate": "language_is", "value": "python"}

    semantic_worker._drain_graph_enrich_config(PID, snapshot_id)

    completed = graph_events.list_events(
        conn,
        PID,
        snapshot_id,
        event_types=["graph_enrich_config_completed"],
    )
    gate_result = completed[0]["payload"]["result"]
    assert gate_result["ok"] is True
    rules = gate_result["preview"]["graph_enrich_config_ops"]["rules"]
    assert rules["emits_event.string_literal"]["downgrade_to"] == "references_schema"
    assert rules["tests_edge_from_filename_match"]["action"] == "require_direct_symbol_import"
    assert rules["imports_module_from_top_level_from_import"]["edge"] == "imports"
    assert rules["python.container_attribute_add_not_cross_module_call"]["when"]["all"][2] == {
        "predicate": "receiver_kind_in",
        "values": ["builtin_collection", "local_collection"],
    }


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
