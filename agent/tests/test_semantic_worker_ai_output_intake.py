from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agent.governance import ai_output_intake
from agent.governance import graph_events
from agent.governance import reconcile_semantic_enrichment as semantic
from agent.governance import semantic_graph_structure_bridge
from agent.governance import semantic_worker


class _NoCloseConn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    semantic._ensure_semantic_state_schema(conn)
    graph_events.ensure_schema(conn)
    ai_output_intake.ensure_schema(conn)
    return conn


def _insert_running_node_job(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO graph_semantic_jobs
          (project_id, snapshot_id, node_id, status, worker_id, claim_id,
           claimed_at, lease_expires_at, claimed_by, last_error, updated_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "demo",
            "scope-demo",
            "L7.1",
            "running",
            "semantic_worker_inproc",
            "claim-1",
            "2026-05-20T00:00:00Z",
            "2026-05-20T00:10:00Z",
            "semantic_worker_inproc",
            "",
            "2026-05-20T00:00:00Z",
            "2026-05-20T00:00:00Z",
        ),
    )
    conn.commit()


def test_process_node_semantic_job_mirrors_structured_output(monkeypatch, tmp_path):
    conn = _conn()
    _insert_running_node_job(conn)
    monkeypatch.setattr("agent.governance.db.get_connection", lambda _project_id: _NoCloseConn(conn))
    monkeypatch.setattr(
        "agent.governance.reconcile_feedback.submit_feedback_item",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        semantic_graph_structure_bridge,
        "bridge_semantic_events_to_graph_structure_jobs",
        lambda *args, **kwargs: {"events": []},
    )
    monkeypatch.setattr(
        semantic_graph_structure_bridge,
        "bridge_semantic_events_to_graph_enrich_config_jobs",
        lambda *args, **kwargs: {"events": []},
    )

    semantic_payload = {
        "node_id": "L7.1",
        "feature_name": "Intake mirror",
        "semantic_summary": "Structured semantic output for dogfood.",
        "intent": "audit semantic output",
        "domain_label": "governance",
        "self_check": {
            "valid": True,
            "status": "passed",
            "checked_rules": semantic.NODE_SEMANTIC_SELF_CHECK_RULES,
        },
        "graph_query_audit": {"trace_id": "gqt-node-demo", "status": "ok"},
    }

    def fake_run_semantic_enrichment(*args, **kwargs):
        conn.execute(
            """
            INSERT INTO graph_semantic_nodes
              (project_id, snapshot_id, node_id, status, feature_hash,
               semantic_json, payload_hash, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "demo",
                "scope-demo",
                "L7.1",
                "pending_review",
                "feature-hash-demo",
                json.dumps(semantic_payload),
                "payload-hash-demo",
                "2026-05-20T00:00:01Z",
            ),
        )
        return {
            "summary": {"ai_complete_count": 1},
            "semantic_index": {
                "features": [{"node_id": "L7.1", "enrichment_status": "ai_complete"}]
            },
        }

    def fake_backfill(conn_arg, project_id, snapshot_id, actor):
        graph_events.create_event(
            conn_arg,
            project_id,
            snapshot_id,
            event_id="semnode-demo-L7-1",
            event_type="semantic_node_enriched",
            event_kind="imported_semantic_cache",
            target_type="node",
            target_id="L7.1",
            status=graph_events.EVENT_STATUS_PROPOSED,
            payload={"semantic_payload": semantic_payload},
            created_by=actor,
        )

    monkeypatch.setattr(semantic, "run_semantic_enrichment", fake_run_semantic_enrichment)
    monkeypatch.setattr(graph_events, "backfill_existing_semantic_events", fake_backfill)

    result = semantic_worker._process_node_semantic_job(
        "demo",
        "scope-demo",
        root=Path(tmp_path),
        ai_call=lambda *_args, **_kwargs: semantic_payload,
        node_id="L7.1",
    )

    assert result["ok"] is True
    assert result["ai_output_intake"]["ok"] is True
    output = ai_output_intake.list_ai_outputs(conn, "demo", task_type="semantic_node")[0]
    assert output["target_id"] == "L7.1"
    assert output["route_status"] == "review_pending"
    assert output["payload"]["semantic_summary"] == "Structured semantic output for dogfood."
    assert output["self_precheck"]["model_self_check"]["valid"] is True
    assert output["self_precheck"]["gate_precheck"]["status"] == "passed"
    assert output["self_precheck"]["gate_precheck"]["gate_name"] == "semantic_node_self_check"
    assert output["graph_query_trace_ids"] == ["gqt-node-demo"]
    assert ai_output_intake.list_ai_output_queue(conn, "demo") == []
    review_pending = ai_output_intake.list_ai_output_queue(conn, "demo", status="review_pending")
    assert [row["output_id"] for row in review_pending] == [output["output_id"]]


def test_process_node_semantic_job_gate_fails_invalid_self_check(monkeypatch, tmp_path):
    conn = _conn()
    _insert_running_node_job(conn)
    submitted_feedback = []
    monkeypatch.setattr("agent.governance.db.get_connection", lambda _project_id: _NoCloseConn(conn))
    monkeypatch.setattr(
        "agent.governance.reconcile_feedback.submit_feedback_item",
        lambda *args, **kwargs: submitted_feedback.append((args, kwargs)),
    )
    monkeypatch.setattr(
        semantic_graph_structure_bridge,
        "bridge_semantic_events_to_graph_structure_jobs",
        lambda *args, **kwargs: {"events": []},
    )
    monkeypatch.setattr(
        semantic_graph_structure_bridge,
        "bridge_semantic_events_to_graph_enrich_config_jobs",
        lambda *args, **kwargs: {"events": []},
    )

    semantic_payload = {
        "node_id": "L7.1",
        "feature_name": "Intake mirror",
        "semantic_summary": "Structured semantic output with a failed self-check.",
        "intent": "audit semantic output",
        "domain_label": "governance",
        "self_check": {
            "valid": False,
            "status": "failed",
            "checked_rules": ["required_fields_present"],
            "known_risks": ["missing_graph_suggestions_contract_checked"],
        },
        "graph_query_audit": {"trace_id": "gqt-node-demo", "status": "ok"},
    }

    def fake_run_semantic_enrichment(*args, **kwargs):
        conn.execute(
            """
            INSERT INTO graph_semantic_nodes
              (project_id, snapshot_id, node_id, status, feature_hash,
               semantic_json, payload_hash, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "demo",
                "scope-demo",
                "L7.1",
                "pending_review",
                "feature-hash-demo",
                json.dumps(semantic_payload),
                "payload-hash-demo",
                "2026-05-20T00:00:01Z",
            ),
        )
        return {
            "summary": {"ai_complete_count": 1},
            "semantic_index": {
                "features": [{"node_id": "L7.1", "enrichment_status": "ai_complete"}]
            },
        }

    def fake_backfill(conn_arg, project_id, snapshot_id, actor):
        graph_events.create_event(
            conn_arg,
            project_id,
            snapshot_id,
            event_id="semnode-demo-L7-1",
            event_type="semantic_node_enriched",
            event_kind="imported_semantic_cache",
            target_type="node",
            target_id="L7.1",
            status=graph_events.EVENT_STATUS_PROPOSED,
            payload={"semantic_payload": semantic_payload},
            created_by=actor,
        )

    monkeypatch.setattr(semantic, "run_semantic_enrichment", fake_run_semantic_enrichment)
    monkeypatch.setattr(graph_events, "backfill_existing_semantic_events", fake_backfill)

    result = semantic_worker._process_node_semantic_job(
        "demo",
        "scope-demo",
        root=Path(tmp_path),
        ai_call=lambda *_args, **_kwargs: semantic_payload,
        node_id="L7.1",
    )

    assert result["ok"] is False
    assert result["status"] == "gate_failed"
    assert result["ai_output_intake"]["ok"] is True
    output = ai_output_intake.list_ai_outputs(conn, "demo", task_type="semantic_node")[0]
    assert output["route_status"] == "gate_failed"
    assert output["self_precheck"]["model_self_check"]["valid"] is False
    assert output["self_precheck"]["gate_precheck"]["status"] == "failed"
    assert output["self_precheck"]["gate_precheck"]["gate_name"] == "semantic_node_self_check"
    assert ai_output_intake.list_ai_output_queue(conn, "demo", status="review_pending") == []
    gate_failed = ai_output_intake.list_ai_output_queue(conn, "demo", status="gate_failed")
    assert [row["output_id"] for row in gate_failed] == [output["output_id"]]
    assert submitted_feedback == []
    event = graph_events.get_event(conn, "demo", "scope-demo", "semnode-demo-L7-1")
    assert event["status"] == graph_events.EVENT_STATUS_REJECTED
    job = conn.execute(
        """
        SELECT status, last_error FROM graph_semantic_jobs
        WHERE project_id='demo' AND snapshot_id='scope-demo' AND node_id='L7.1'
        """
    ).fetchone()
    assert job["status"] == "rejected"
    assert "semantic_node_self_check" in job["last_error"]


def test_graph_structure_and_config_mirrors_are_idempotent():
    conn = _conn()
    structure_payload = {
        "schema_version": "graph_structure_ops.v1",
        "source": {"snapshot_id": "scope-demo", "base_commit": "abc1234"},
        "operations": [{"op": "add_edge", "source_path": "a.py", "target_node_id": "L7.1"}],
        "self_check": {"valid": True, "precheck_status": "passed"},
    }
    structure_result = {
        "ok": True,
        "parse": {"payload": structure_payload},
        "precheck": {"status": "passed", "classification": "passed"},
    }

    first = semantic_worker._mirror_ai_output_intake(
        conn,
        "demo",
        "scope-demo",
        task_type="graph_structure_ops",
        target_type="node",
        target_id="L7.1",
        raw_output=structure_payload,
        result=structure_result,
        source_run_id="gs-event-1",
    )
    second = semantic_worker._mirror_ai_output_intake(
        conn,
        "demo",
        "scope-demo",
        task_type="graph_structure_ops",
        target_type="node",
        target_id="L7.1",
        raw_output=dict(structure_payload),
        result=structure_result,
        source_run_id="gs-event-1",
    )

    assert first["ok"] is True
    assert second["idempotent"] is True

    config_payload = {
        "schema_version": "graph_enrich_config_ops.v1",
        "source": {"analyzer_role": "reconcile_graph_enrich_config_analyzer"},
        "operations": [
            {
                "op": "upsert_edge_evidence_policy",
                "rule_id": "calls-import-only",
                "edge": "calls",
                "source_evidence": "import_only",
                "action": "downgrade",
                "downgrade_to": "imports",
            }
        ],
        "self_check": {"valid": True, "precheck_status": "passed"},
    }
    config_result = {
        "ok": True,
        "parse": {"payload": config_payload},
        "precheck": {"status": "passed", "classification": "passed"},
    }
    config = semantic_worker._mirror_ai_output_intake(
        conn,
        "demo",
        "scope-demo",
        task_type="graph_enrich_config_ops",
        target_type="project",
        target_id="demo",
        raw_output=config_payload,
        result=config_result,
        source_run_id="gec-event-1",
    )

    assert config["ok"] is True
    outputs = ai_output_intake.list_ai_outputs(conn, "demo")
    by_type = {row["task_type"]: row for row in outputs}
    assert set(by_type) == {"graph_structure_ops", "graph_enrich_config_ops"}
    assert by_type["graph_structure_ops"]["route_status"] == "completed"
    assert by_type["graph_enrich_config_ops"]["route_status"] == "completed"
    assert by_type["graph_structure_ops"]["self_precheck"]["gate_precheck"]["status"] == "passed"
    assert by_type["graph_enrich_config_ops"]["payload"]["operations"][0]["rule_id"] == "calls-import-only"
    assert ai_output_intake.list_ai_output_queue(conn, "demo") == []
