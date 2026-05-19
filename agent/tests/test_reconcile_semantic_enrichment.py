from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent.governance import reconcile_batch_memory as bm
from agent.governance import graph_snapshot_store as store
from agent.governance import reconcile_feedback
from agent.governance.reconcile_semantic_enrichment import (
    _batch_key,
    _semantic_batch_memory_summary,
    _upsert_semantic_job,
    append_review_feedback,
    claim_semantic_jobs,
    load_review_feedback,
    run_semantic_enrichment,
)
from agent.governance.db import _ensure_schema


PID = "semantic-enrichment-test"


def test_semantic_batch_key_prefers_hierarchy_parent_for_feature_groups():
    feature = {
        "layer": "L7",
        "kind": "",
        "metadata": {"hierarchy_parent": "L3.19"},
    }

    assert _batch_key(feature, "subsystem") == "L3.19"


def test_semantic_jobs_can_be_claimed_with_worker_lease(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)

    run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=False,
    )

    first = claim_semantic_jobs(
        conn,
        PID,
        "full-semantic-test",
        worker_id="semantic-worker-1",
        limit=1,
        lease_seconds=300,
    )
    assert first["claimed_count"] == 1
    assert first["jobs"][0]["status"] == "running"
    assert first["jobs"][0]["worker_id"] == "semantic-worker-1"
    assert first["jobs"][0]["lease_expires_at"]

    second = claim_semantic_jobs(
        conn,
        PID,
        "full-semantic-test",
        worker_id="semantic-worker-2",
        limit=1,
        lease_seconds=300,
    )
    assert second["claimed_count"] == 0

    row = conn.execute(
        """
        SELECT status, worker_id, claim_id, lease_expires_at, attempt_count
        FROM graph_semantic_jobs
        WHERE project_id=? AND snapshot_id=? AND node_id=?
        """,
        (PID, "full-semantic-test", "L7.1"),
    ).fetchone()
    assert row["status"] == "running"
    assert row["worker_id"] == "semantic-worker-1"
    assert row["claim_id"] == first["claim_id"]
    assert row["attempt_count"] == 1


def test_running_claimed_semantic_job_attempt_is_not_double_counted():
    state = {
        "semantic_jobs": {
            "L7.1": {
                "node_id": "L7.1",
                "status": "running",
                "attempt_count": 1,
                "worker_id": "semantic-worker-1",
                "claim_id": "claim-1",
                "claimed_at": "2026-05-18T00:00:00Z",
                "lease_expires_at": "2026-05-18T00:10:00Z",
                "claimed_by": "semantic-worker-1",
                "created_at": "2026-05-18T00:00:00Z",
            }
        }
    }

    _upsert_semantic_job(
        state,
        {"node_id": "L7.1", "feature_hash": "sha256:demo", "file_hashes": {}},
        status="running",
        feedback_round=1,
        batch_index=None,
        updated_at="2026-05-18T00:01:00Z",
        increment_attempt=True,
    )

    job = state["semantic_jobs"]["L7.1"]
    assert job["attempt_count"] == 1
    assert job["worker_id"] == "semantic-worker-1"
    assert job["claim_id"] == "claim-1"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    store.ensure_schema(c)
    yield c
    c.close()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _graph(node_id: str = "L7.1", *, include_extra: bool = False) -> dict:
    nodes = [
        {
            "id": node_id,
            "layer": "L7",
            "title": "Backlog Runtime",
            "kind": "service_runtime",
            "primary": ["agent/governance/backlog_runtime.py"],
            "secondary": ["docs/dev/backlog-runtime.md"],
            "test": ["agent/tests/test_backlog_runtime.py"],
            "config": ["config/roles/default/pm.yaml"],
            "metadata": {
                "subsystem": "backlog",
                "config_files": ["config/roles/default/pm.yaml"],
                "functions": [
                    {
                        "name": "claim_next",
                        "path": "agent/governance/backlog_runtime.py",
                        "lineno": 12,
                    }
                ],
            },
        }
    ]
    if include_extra:
        nodes.extend([
            {
                "id": "L3.1",
                "layer": "L3",
                "title": "Backlog State Management",
                "kind": "subsystem",
                "primary": [],
                "secondary": [],
                "test": [],
                "metadata": {"subsystem": "backlog"},
            },
            {
                "id": "L7.2",
                "layer": "L7",
                "title": "Trace Writer",
                "kind": "service_runtime",
                "primary": ["agent/governance/reconcile_trace.py"],
                "secondary": [],
                "test": [],
                "metadata": {"subsystem": "reconcile"},
            },
        ])
    return {
        "deps_graph": {
            "nodes": nodes,
            "edges": [],
        }
    }


def _create_snapshot(
    conn: sqlite3.Connection,
    project: Path,
    *,
    snapshot_kind: str = "full",
    include_extra: bool = False,
) -> None:
    _write(
        project / "agent" / "governance" / "backlog_runtime.py",
        "def claim_next():\n    return 'task'\n",
    )
    _write(project / "docs" / "dev" / "backlog-runtime.md", "# Backlog Runtime\n")
    _write(project / "agent" / "tests" / "test_backlog_runtime.py", "def test_claim_next():\n    assert True\n")
    _write(project / "config" / "roles" / "default" / "pm.yaml", "role: pm\n")
    if include_extra:
        _write(project / "agent" / "governance" / "reconcile_trace.py", "def write_json():\n    return None\n")
    store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id=f"{snapshot_kind}-semantic-test",
        commit_sha="abc1234",
        snapshot_kind=snapshot_kind,
        graph_json=_graph(include_extra=include_extra),
        notes=json.dumps({"state_only": True}),
    )
    conn.commit()


def test_semantic_enrichment_uses_feedback_on_retry(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)
    seen_payloads: list[dict] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        seen_payloads.append({"stage": stage, "payload": payload})
        feedback_ids = [item["feedback_id"] for item in payload["review_feedback"]]
        return {
            "feature_name": "Backlog Runtime State Flow",
            "semantic_summary": "Owns backlog task state transitions.",
            "intent": "stateful backlog runtime",
            "domain_label": "state",
            "applied_feedback_ids": feedback_ids,
            "doc_coverage_review": {"bound": True, "action": "keep"},
            "test_coverage_review": {"bound": True, "action": "keep"},
            "config_coverage_review": {"bound": True, "action": "keep"},
        }

    first = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        created_by="test",
        trace_dir=project / "semantic-trace",
    )

    assert first["summary"]["ai_complete_count"] == 1
    assert first["semantic_index"]["features"][0]["feature_name"] == "Backlog Runtime State Flow"
    assert Path(first["semantic_index_path"]).exists()
    assert seen_payloads[0]["stage"] == "reconcile_semantic_feature"
    assert seen_payloads[0]["payload"]["instructions"]["mutate_project_files"] is False
    assert seen_payloads[0]["payload"]["instructions"]["analyzer"] == "reconcile_semantic"
    assert seen_payloads[0]["payload"]["instructions"]["prompt_template"]
    assert seen_payloads[0]["payload"]["feature"]["source_excerpt"]
    assert seen_payloads[0]["payload"]["feature"]["config"] == ["config/roles/default/pm.yaml"]
    assert seen_payloads[0]["payload"]["feature"]["config_refs"][0]["path"] == "config/roles/default/pm.yaml"
    assert first["summary"]["feature_payload_input_count"] == 1
    assert Path(first["summary"]["feature_payload_input_dir"]).exists()
    assert (project / "semantic-trace" / "feature-inputs" / "L7.1.json").exists()
    assert (project / "semantic-trace" / "feature-outputs" / "L7.1.json").exists()

    second = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        feedback_items={
            "feedback_id": "fb-doc-1",
            "target_type": "node",
            "target_id": "L7.1",
            "priority": "P1",
            "issue": "Feature name is too runtime-specific.",
            "expected_change": "Mention persisted backlog state.",
        },
        use_ai=True,
        ai_call=fake_ai,
        created_by="reviewer",
    )

    assert second["feedback_round"] == 1
    feature = second["semantic_index"]["features"][0]
    assert feature["applied_feedback_ids"] == ["fb-doc-1"]
    assert feature["config_coverage_review"]["bound"] is True
    assert feature["config"] == ["config/roles/default/pm.yaml"]
    assert feature["unresolved_feedback_ids"] == []
    assert load_review_feedback(PID, "full-semantic-test")[0]["feedback_id"] == "fb-doc-1"
    assert seen_payloads[-1]["payload"]["review_feedback"][0]["expected_change"] == "Mention persisted backlog state."
    notes = json.loads(
        conn.execute(
            "SELECT notes FROM graph_snapshots WHERE project_id=? AND snapshot_id=?",
            (PID, "full-semantic-test"),
        ).fetchone()["notes"]
    )
    assert notes["semantic_enrichment"]["latest_round"] == 1
    assert notes["semantic_feedback"]["feedback_count"] == 1


def test_semantic_enrichment_persists_node_ai_self_check(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)

    def fake_ai(stage: str, payload: dict) -> dict:
        return {
            "feature_name": "Backlog Runtime State Flow",
            "semantic_summary": "Owns backlog task state transitions.",
            "intent": "stateful backlog runtime",
            "domain_label": "state",
            "applied_feedback_ids": [],
            "doc_coverage_review": {"bound": True, "action": "keep"},
            "test_coverage_review": {"bound": True, "action": "keep"},
            "config_coverage_review": {"bound": True, "action": "keep"},
            "self_check": {
                "required": True,
                "valid": True,
                "status": "passed",
                "checked_rules": [
                    "required_fields_present",
                    "source_payload_only",
                    "no_project_mutation",
                    "review_feedback_accounted_for",
                    "graph_suggestions_contract_checked",
                ],
                "checked_rules_count": 5,
                "repair_attempts": 0,
                "max_repair_attempts": 1,
                "known_risks": [],
            },
        }

    result = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        created_by="test",
        trace_dir=project / "semantic-trace",
    )

    feature = result["semantic_index"]["features"][0]
    assert feature["self_check"]["required"] is True
    assert feature["self_check"]["valid"] is True
    assert feature["self_check"]["checked_rules_count"] == 5
    output = json.loads((project / "semantic-trace" / "feature-outputs" / "L7.1.json").read_text())
    assert output["semantic_entry"]["semantic_ai_self_check"]["status"] == "passed"
    row = conn.execute(
        """
        SELECT semantic_json FROM graph_semantic_nodes
        WHERE project_id=? AND snapshot_id=? AND node_id=?
        """,
        (PID, "full-semantic-test", "L7.1"),
    ).fetchone()
    persisted = json.loads(row["semantic_json"])
    assert persisted["self_check"]["status"] == "passed"
    assert persisted["semantic_ai_self_check"]["valid"] is True


def test_semantic_enrichment_marks_missing_node_ai_self_check(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)

    def fake_ai(stage: str, payload: dict) -> dict:
        return {
            "feature_name": "Backlog Runtime State Flow",
            "semantic_summary": "Owns backlog task state transitions.",
            "intent": "stateful backlog runtime",
            "domain_label": "state",
            "applied_feedback_ids": [],
        }

    result = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        created_by="test",
    )

    feature = result["semantic_index"]["features"][0]
    assert feature["enrichment_status"] == "ai_complete"
    assert feature["self_check"]["required"] is True
    assert feature["self_check"]["valid"] is False
    assert feature["self_check"]["status"] == "missing"
    assert feature["self_check"]["known_risks"] == ["missing_ai_self_check"]
    row = conn.execute(
        """
        SELECT semantic_json FROM graph_semantic_nodes
        WHERE project_id=? AND snapshot_id=? AND node_id=?
        """,
        (PID, "full-semantic-test", "L7.1"),
    ).fetchone()
    persisted = json.loads(row["semantic_json"])
    assert persisted["self_check"]["status"] == "missing"
    assert persisted["self_check"]["known_risks"] == ["missing_ai_self_check"]


def test_semantic_enrichment_is_snapshot_kind_agnostic(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project, snapshot_kind="scope")

    result = run_semantic_enrichment(
        conn,
        PID,
        "scope-semantic-test",
        project,
        use_ai=False,
        created_by="test",
    )

    assert result["semantic_index"]["snapshot_kind"] == "scope"
    assert result["semantic_index"]["features"][0]["enrichment_status"] == "heuristic"
    assert result["summary"]["quality_flag_counts"]["missing_symbol_refs"] == 1


def test_semantic_enrichment_can_select_explicit_node_for_ai(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project, include_extra=True)
    seen_nodes: list[str] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        seen_nodes.append(payload["feature"]["node_id"])
        return {"feature_name": f"AI {payload['feature']['node_id']}"}

    result = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        semantic_ai_scope="selected",
        semantic_node_ids=["L7.2"],
        created_by="test",
    )

    assert seen_nodes == ["L7.2"]
    assert result["summary"]["ai_selected_count"] == 1
    assert result["summary"]["ai_complete_count"] == 1
    assert result["summary"]["ai_skipped_selector_count"] == 1
    by_id = {item["node_id"]: item for item in result["semantic_index"]["features"]}
    assert by_id["L7.2"]["enrichment_status"] == "ai_complete"
    assert by_id["L7.1"]["enrichment_status"] == "ai_skipped_selector"
    assert by_id["L7.2"]["semantic_selection_reasons"] == ["node_id"]


def test_semantic_enrichment_skips_package_markers_by_default_but_allows_explicit_node(
    conn,
    tmp_path,
):
    project = tmp_path / "project"
    _write(project / "agent" / "__init__.py", "def bootstrap_project():\n    return None\n")
    _write(project / "agent" / "service.py", "def service_entry():\n    return 'ok'\n")
    _write(project / "agent" / "types.ts", "export interface ServiceContract { operation_type: string }\n")
    graph = {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.1",
                    "layer": "L7",
                    "title": "Service",
                    "primary": ["agent/service.py"],
                    "metadata": {"file_role": "implementation"},
                },
                {
                    "id": "L7.pkg",
                    "layer": "L7",
                    "title": "agent",
                    "primary": ["agent/__init__.py"],
                    "metadata": {
                        "exclude_as_feature": True,
                        "file_role": "package_marker",
                    },
                },
                {
                    "id": "L7.types",
                    "layer": "L7",
                    "title": "agent.types",
                    "primary": ["agent/types.ts"],
                    "metadata": {
                        "exclude_as_feature": True,
                        "file_role": "type_contract",
                    },
                },
            ],
            "edges": [],
        }
    }
    store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="package-marker-semantic-test",
        commit_sha="abc1234",
        snapshot_kind="full",
        graph_json=graph,
    )
    seen_nodes: list[str] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        seen_nodes.append(payload["feature"]["node_id"])
        return {"feature_name": f"AI {payload['feature']['node_id']}"}

    result = run_semantic_enrichment(
        conn,
        PID,
        "package-marker-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        semantic_ai_scope="all",
        created_by="test",
    )

    assert seen_nodes == ["L7.1"]
    assert [item["node_id"] for item in result["semantic_index"]["features"]] == ["L7.1"]

    seen_nodes.clear()
    explicit = run_semantic_enrichment(
        conn,
        PID,
        "package-marker-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        semantic_ai_scope="selected",
        semantic_node_ids=["L7.pkg"],
        semantic_skip_completed=False,
        created_by="test",
    )

    assert seen_nodes == ["L7.pkg"]
    assert explicit["summary"]["ai_selected_count"] == 1


def test_semantic_enrichment_can_select_structural_layer(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project, include_extra=True)
    seen_nodes: list[str] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        seen_nodes.append(payload["feature"]["node_id"])
        return {
            "feature_name": "Backlog State Architecture",
            "semantic_summary": "Groups backlog state-management nodes.",
        }

    result = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        semantic_ai_scope="selected",
        semantic_layers=["L3"],
        created_by="test",
    )

    assert seen_nodes == ["L3.1"]
    assert result["summary"]["ai_selected_count"] == 1
    by_id = {item["node_id"]: item for item in result["semantic_index"]["features"]}
    assert by_id["L3.1"]["enrichment_status"] == "ai_complete"
    assert by_id["L3.1"]["feature_name"] == "Backlog State Architecture"
    assert by_id["L7.1"]["enrichment_status"] == "ai_skipped_selector"


def test_semantic_enrichment_can_select_missing_doc_nodes(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project, include_extra=True)
    seen_nodes: list[str] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        seen_nodes.append(payload["feature"]["node_id"])
        return {"feature_name": f"Needs docs {payload['feature']['node_id']}"}

    result = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        semantic_ai_scope="issues",
        semantic_missing=["doc"],
        semantic_layers=["L7"],
        created_by="test",
    )

    assert seen_nodes == ["L7.2"]
    assert result["summary"]["semantic_selector"]["missing"] == ["doc"]
    assert result["summary"]["semantic_selector"]["layers"] == ["L7"]


def test_semantic_enrichment_primary_selector_ignores_test_binding_changes(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)
    seen_nodes: list[str] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        seen_nodes.append(payload["feature"]["node_id"])
        return {"feature_name": f"AI {payload['feature']['node_id']}"}

    result = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        semantic_ai_scope="changed",
        semantic_changed_paths=["agent/tests/test_backlog_runtime.py"],
        semantic_selector_match="primary",
        created_by="test",
    )

    assert seen_nodes == []
    assert result["summary"]["semantic_selector"]["match_mode"] == "primary"
    assert result["summary"]["ai_selected_count"] == 0
    assert result["summary"]["ai_skipped_selector_count"] == 1


def test_semantic_enrichment_can_batch_ai_features(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project, include_extra=True)
    calls: list[dict] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        calls.append({"stage": stage, "payload": payload})
        return {
            "features": [
                {
                    "node_id": item["feature"]["node_id"],
                    "feature_name": f"Batch {item['feature']['node_id']}",
                    "semantic_summary": f"Batch summary {item['feature']['node_id']}",
                }
                for item in payload["features"]
            ],
            "self_check": {
                "required": True,
                "valid": True,
                "status": "passed",
                "checked_rules": [
                    "required_fields_present",
                    "source_payload_only",
                    "no_project_mutation",
                    "review_feedback_accounted_for",
                    "graph_suggestions_contract_checked",
                ],
                "checked_rules_count": 5,
                "repair_attempts": 0,
                "max_repair_attempts": 1,
                "known_risks": [],
            },
            "_ai_route": {"provider": "test", "model": "batch-model"},
            "_ai_elapsed_ms": 42,
        }

    result = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        semantic_ai_scope="all",
        semantic_ai_batch_size=10,
        semantic_ai_batch_by="none",
        semantic_ai_input_mode="batch",
        created_by="test",
    )

    assert [call["stage"] for call in calls] == ["reconcile_semantic_feature_batch"]
    assert len(calls[0]["payload"]["features"]) == 2
    assert calls[0]["payload"]["instructions"]["batch_mode"] is True
    assert calls[0]["payload"]["instructions"]["semantic_ai_input_mode"] == "batch"
    assert calls[0]["payload"]["instructions"]["use_semantic_graph_state"] is True
    assert calls[0]["payload"]["instructions"]["use_batch_memory"] is False
    assert calls[0]["payload"]["semantic_graph_state"]["completed_node_count"] == 0
    assert calls[0]["payload"]["batch_memory"] == {}
    assert calls[0]["payload"]["features"][0]["related_batch_features"] == []
    assert calls[0]["payload"]["features"][0]["related_graph_features"] == []
    assert result["summary"]["ai_batch_count"] == 1
    assert result["summary"]["ai_batch_complete_count"] == 1
    assert result["summary"]["ai_complete_count"] == 2
    assert result["summary"]["semantic_graph_state"]["enabled"] is True
    assert result["summary"]["semantic_graph_state"]["completed_node_count"] == 2
    assert result["summary"]["semantic_graph_state"]["accepted_feature_count"] == 2
    assert result["summary"]["semantic_batch_memory"]["enabled"] is False
    by_id = {item["node_id"]: item for item in result["semantic_index"]["features"]}
    assert by_id["L7.1"]["feature_name"] == "Batch L7.1"
    assert by_id["L7.2"]["feature_name"] == "Batch L7.2"
    assert by_id["L7.1"]["semantic_ai_route"]["model"] == "batch-model"
    assert by_id["L7.1"]["self_check"]["status"] == "passed"
    assert by_id["L7.2"]["self_check"]["checked_rules_count"] == 5
    assert Path(result["summary"]["semantic_graph_state"]["state_path"]).exists()
    semantic_graph = json.loads(Path(result["summary"]["semantic_graph_state"]["semantic_graph_path"]).read_text())
    graph_nodes = {node["id"]: node for node in semantic_graph["deps_graph"]["nodes"]}
    assert graph_nodes["L7.1"]["metadata"]["semantic"]["feature_name"] == "Batch L7.1"
    assert Path(result["summary"]["batch_payload_input_dir"]).exists()
    assert Path(result["summary"]["batch_payload_output_dir"]).exists()


def test_semantic_enrichment_defaults_to_dynamic_feature_input(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project, include_extra=True)
    calls: list[dict] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        calls.append({
            "stage": stage,
            "node_id": payload["feature"]["node_id"],
            "completed": payload["semantic_graph_state"]["completed_node_count"],
            "related": payload["related_graph_features"],
            "input_mode": payload["semantic_ai_input_mode"],
            "dynamic": payload["dynamic_semantic_graph_state"],
        })
        return {
            "feature_name": f"Dynamic {payload['feature']['node_id']}",
            "semantic_summary": f"Dynamic summary {payload['feature']['node_id']}",
        }

    result = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        semantic_ai_scope="all",
        semantic_ai_batch_size=10,
        semantic_ai_batch_by="none",
        created_by="test",
    )

    assert [call["stage"] for call in calls] == [
        "reconcile_semantic_feature",
        "reconcile_semantic_feature",
    ]
    assert [call["completed"] for call in calls] == [0, 1]
    assert [call["input_mode"] for call in calls] == ["feature", "feature"]
    assert all(call["dynamic"] is True for call in calls)
    assert result["summary"]["ai_input_mode"] == "feature"
    assert result["summary"]["requested_ai_batch_size"] == 10
    assert result["summary"]["ai_batch_size"] == 1
    assert result["semantic_index"]["semantic_batching"]["input_mode"] == "feature"


def test_semantic_graph_state_accumulates_and_skips_completed(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project, include_extra=True)
    completed_counts: list[int] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        assert stage == "reconcile_semantic_feature"
        completed_counts.append(payload["semantic_graph_state"]["completed_node_count"])
        return {
            "feature_name": f"Feature {payload['feature']['node_id']}",
            "semantic_summary": f"Summary {payload['feature']['node_id']}",
        }

    result = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        semantic_ai_scope="all",
        created_by="test",
    )

    assert completed_counts == [0, 1]
    assert result["summary"]["semantic_graph_state"]["completed_node_count"] == 2

    calls_after_first = len(completed_counts)
    second = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        semantic_ai_scope="all",
        created_by="test",
    )
    assert len(completed_counts) == calls_after_first
    assert second["summary"]["semantic_graph_state"]["hit_count"] == 2
    by_id = {item["node_id"]: item for item in second["semantic_index"]["features"]}
    assert by_id["L7.1"]["enrichment_status"] == "semantic_graph_state"
    assert by_id["L7.1"]["feature_name"] == "Feature L7.1"
    db_count = conn.execute(
        "SELECT COUNT(*) FROM graph_semantic_nodes WHERE project_id=? AND snapshot_id=?",
        (PID, "full-semantic-test"),
    ).fetchone()[0]
    assert db_count == 2


def test_semantic_graph_state_resumes_from_db_when_companion_is_missing(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)
    calls: list[str] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        calls.append(stage)
        return {
            "feature_name": "DB-backed Semantic State",
            "semantic_summary": "Persisted in DB, exported as companion JSON.",
        }

    first = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        created_by="test",
    )
    assert first["summary"]["ai_complete_count"] == 1
    state_path = Path(first["summary"]["semantic_graph_state"]["state_path"])
    state_path.unlink()

    calls_after_first = len(calls)
    second = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        created_by="test",
    )

    assert len(calls) == calls_after_first
    assert second["summary"]["semantic_graph_state"]["source"] == "db"
    assert second["summary"]["semantic_graph_state"]["hit_count"] == 1
    assert second["semantic_index"]["features"][0]["feature_name"] == "DB-backed Semantic State"


def test_semantic_graph_state_hash_mismatch_forces_resemanticization(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)
    calls: list[str] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        calls.append(stage)
        return {
            "feature_name": f"Run {len(calls)}",
            "semantic_summary": "Hash-gated semantic result.",
        }

    first = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        created_by="test",
    )
    assert first["summary"]["ai_complete_count"] == 1
    conn.execute(
        """
        UPDATE graph_semantic_nodes
        SET feature_hash='sha256:stale'
        WHERE project_id=? AND snapshot_id=? AND node_id=?
        """,
        (PID, "full-semantic-test", "L7.1"),
    )
    conn.commit()

    second = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        created_by="test",
    )

    assert len(calls) == 2
    assert second["summary"]["semantic_hash_mismatch_count"] == 1
    assert second["summary"]["ai_complete_count"] == 1
    assert second["semantic_index"]["features"][0]["feature_name"] == "Run 2"


def test_semantic_graph_state_carries_forward_unchanged_snapshot_entries(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project, snapshot_kind="full")
    _create_snapshot(conn, project, snapshot_kind="scope")
    calls: list[str] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        calls.append(stage)
        return {
            "feature_name": "Backlog Runtime Flow",
            "semantic_summary": "Owns backlog task state transitions.",
            "intent": "Govern backlog runtime state.",
            "doc_coverage_review": {"bound": True, "status": "bound"},
            "test_coverage_review": {"bound": True, "status": "bound"},
            "config_coverage_review": {"bound": True, "status": "bound"},
        }

    first = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        created_by="test",
    )
    assert first["summary"]["ai_complete_count"] == 1
    assert calls == ["reconcile_semantic_feature"]

    second = run_semantic_enrichment(
        conn,
        PID,
        "scope-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        semantic_base_snapshot_id="full-semantic-test",
        created_by="test",
    )

    assert calls == ["reconcile_semantic_feature"]
    state_report = second["summary"]["semantic_graph_state"]
    assert state_report["base_snapshot_id"] == "full-semantic-test"
    assert state_report["carried_forward_count"] == 1
    assert state_report["hit_count"] == 1
    feature = second["semantic_index"]["features"][0]
    assert feature["enrichment_status"] == "semantic_graph_state"
    assert feature["feature_name"] == "Backlog Runtime Flow"


def test_semantic_batch_memory_remains_explicit_advisory(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project, include_extra=True)
    memory_counts: list[int] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        assert stage == "reconcile_semantic_feature"
        memory_counts.append(payload["batch_memory"]["accepted_feature_count"])
        return {
            "feature_name": f"Memory {payload['feature']['node_id']}",
            "semantic_summary": f"Summary {payload['feature']['node_id']}",
        }

    result = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
        semantic_ai_scope="all",
        semantic_batch_memory=True,
        semantic_skip_completed=False,
        created_by="test",
    )

    assert memory_counts == [0, 1]
    assert result["summary"]["semantic_batch_memory"]["decision_count"] == 2
    batch = bm.get_batch(conn, PID, "semantic-full-semantic-test-round-000")
    assert sorted(batch["memory"]["accepted_features"]) == ["Memory L7.1", "Memory L7.2"]


def test_semantic_batch_memory_summary_compacts_large_conflicts():
    summary = _semantic_batch_memory_summary({
        "batch_id": "batch-1",
        "session_id": "session-1",
        "memory": {
            "accepted_features": {
                "Feature A": {
                    "purpose": "x" * 500,
                    "owned_files": ["a.py"],
                }
            },
            "file_ownership": {"a.py": "Feature A"},
            "open_conflicts": [
                {
                    "reason": "dependency_patch_suggestions",
                    "items": [
                        {
                            "type": "document_gate_taxonomy",
                            "reason": "r" * 600,
                            "proposed_action": "p" * 600,
                        }
                    ],
                }
            ],
        },
    })

    assert summary["accepted_features"][0]["purpose"].endswith("...")
    item = summary["open_conflicts"][0]["items"][0]
    assert item["reason"].endswith("...")
    assert item["proposed_action"].endswith("...")
    assert len(json.dumps(summary)) < 1600


def test_append_review_feedback_normalizes_append_only_items(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)

    result = append_review_feedback(
        conn,
        PID,
        "full-semantic-test",
        {
            "target_type": "path",
            "path": "agent/governance/backlog_runtime.py",
            "issue": "Needs clearer state ownership.",
        },
        created_by="observer",
    )

    assert result["added_count"] == 1
    feedback = load_review_feedback(PID, "full-semantic-test")
    assert len(feedback) == 1
    assert feedback[0]["target_id"] == "agent/governance/backlog_runtime.py"
    assert feedback[0]["created_by"] == "observer"


def test_semantic_enrichment_normalizes_structured_health_issues(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)

    def fake_ai(stage: str, payload: dict) -> dict:
        assert stage == "reconcile_semantic_feature"
        return {
            "feature_name": "Backlog runtime",
            "semantic_summary": "Claims and records backlog runtime work.",
            "health_issues": [
                {
                    "category": "test_gap",
                    "severity": "high",
                    "confidence": 0.91,
                    "affected_node_ids": ["L7.1"],
                    "summary": "Focused retry-state regression tests are missing.",
                    "suggested_action": "Add a retry-state regression test.",
                }
            ],
            "open_issues": [],
        }

    result = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=True,
        ai_call=fake_ai,
    )

    assert result["summary"]["ai_complete_count"] == 1
    assert result["summary"]["health_issue_counts"]["test_gap"] == 1
    row = conn.execute(
        """
        SELECT semantic_json FROM graph_semantic_nodes
        WHERE project_id=? AND snapshot_id=? AND node_id='L7.1'
        """,
        (PID, "full-semantic-test"),
    ).fetchone()
    semantic_json = json.loads(row["semantic_json"])
    health_issue = semantic_json["health_issues"][0]
    assert health_issue["schema_version"] == 1
    assert health_issue["category"] == "test_gap"
    assert health_issue["severity"] == "high"
    assert health_issue["confidence"] == 0.91
    assert health_issue["affected_node_ids"] == ["L7.1"]
    assert health_issue["suggested_action"] == "Add a retry-state regression test."


def test_reconcile_feedback_classifies_reviews_and_files_state(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)
    state_path = store.snapshot_companion_dir(PID, "full-semantic-test") / "semantic-enrichment" / "semantic-graph-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "open_issues": [
            {
                "node_id": "L7.7",
                "reason": "merge_suggestions",
                "summary": "L7.7 and L7.136 both claim .aming-claw.yaml; confirm whether to merge or split responsibilities.",
                "type": "",
            },
            {
                "node_id": "L7.51",
                "reason": "dependency_patch_suggestions",
                "summary": "Mis-extraction: .aming-claw.yaml is read, not written by this module.",
                "type": "typed_relation",
            },
            {
                "node_id": "L7.5",
                "reason": "dependency_patch_suggestions",
                "summary": "missing_test_binding flag — primary file has many functions and zero direct tests.",
                "type": "",
            },
        ]
    }), encoding="utf-8")

    result = reconcile_feedback.classify_semantic_open_issues(
        PID,
        "full-semantic-test",
        source_round="round-017",
        created_by="observer",
    )

    summary = result["summary"]
    assert summary["by_kind"]["needs_observer_decision"] == 1
    assert summary["by_kind"]["graph_correction"] == 1
    assert summary["by_kind"]["status_observation"] == 1
    items = {item["source_node_ids"][0]: item for item in reconcile_feedback.list_feedback_items(PID, "full-semantic-test")}
    assert items["L7.7"]["requires_human_signoff"] is True
    assert items["L7.51"]["target_type"] == "edge"
    assert items["L7.5"]["feedback_kind"] == "status_observation"
    assert items["L7.5"]["status_observation_category"] == "coverage_gap"

    def fake_reviewer(stage: str, payload: dict) -> dict:
        assert stage == "reconcile_feedback_review"
        assert payload["instructions"]["mutate_project_files"] is False
        return {
            "decision": "project_improvement",
            "rationale": "The issue describes real duplicate configuration ownership.",
            "confidence": 0.72,
        }

    review = reconcile_feedback.review_feedback_item(
        PID,
        "full-semantic-test",
        items["L7.7"]["feedback_id"],
        actor="observer",
        accept=True,
        ai_call=fake_reviewer,
    )
    reviewed = review["items"][0]
    assert reviewed["status"] == "accepted"
    assert reviewed["final_feedback_kind"] == "project_improvement"

    status_review = reconcile_feedback.review_feedback_item(
        PID,
        "full-semantic-test",
        items["L7.5"]["feedback_id"],
        actor="observer",
        decision="status_observation",
        status_observation_category="stale_test_expectation",
        rationale="The test binding may be stale, but this stays visible until user action.",
    )
    status_item = status_review["items"][0]
    assert status_item["status"] == "reviewed"
    assert status_item["reviewed_status_observation_category"] == "stale_test_expectation"

    backlog = reconcile_feedback.build_project_improvement_backlog(
        PID,
        "full-semantic-test",
        items["L7.7"]["feedback_id"],
        bug_id="OPT-BACKLOG-FEEDBACK-CONFIG-BOUNDARY",
        actor="observer",
    )
    assert backlog["bug_id"] == "OPT-BACKLOG-FEEDBACK-CONFIG-BOUNDARY"
    assert "reconcile_feedback" in backlog["payload"]["chain_trigger_json"]["source"]

    filed = reconcile_feedback.mark_feedback_backlog_filed(
        PID,
        "full-semantic-test",
        items["L7.7"]["feedback_id"],
        bug_id=backlog["bug_id"],
        actor="observer",
    )
    assert filed["items"][0]["status"] == "backlog_filed"
    assert filed["items"][0]["backlog_bug_id"] == "OPT-BACKLOG-FEEDBACK-CONFIG-BOUNDARY"

    with pytest.raises(ValueError):
        reconcile_feedback.build_project_improvement_backlog(
            PID,
            "full-semantic-test",
            items["L7.5"]["feedback_id"],
            bug_id="OPT-BACKLOG-FEEDBACK-MISSING-TEST",
            actor="observer",
        )
    status_backlog = reconcile_feedback.build_project_improvement_backlog(
        PID,
        "full-semantic-test",
        items["L7.5"]["feedback_id"],
        bug_id="OPT-BACKLOG-FEEDBACK-MISSING-TEST",
        actor="observer",
        allow_status_observation=True,
    )
    assert status_backlog["bug_id"] == "OPT-BACKLOG-FEEDBACK-MISSING-TEST"
    assert status_backlog["payload"]["chain_trigger_json"]["feedback_kind"] == "status_observation"
    assert (
        status_backlog["payload"]["chain_trigger_json"]["status_observation_category"]
        == "stale_test_expectation"
    )
    assert status_backlog["payload"]["title"].startswith("User-requested backlog")


def test_reconcile_feedback_filters_semantic_state_by_round_and_nodes(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)
    state_path = store.snapshot_companion_dir(PID, "full-semantic-test") / "semantic-enrichment" / "semantic-graph-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    old_issue = {
        "node_id": "L7.old",
        "reason": "dependency_patch_suggestions",
        "summary": "Historical issue from an older semantic round.",
        "type": "typed_relation",
    }
    new_issue = {
        "node_id": "L7.new",
        "reason": "dependency_patch_suggestions",
        "summary": "missing_doc_binding flag for the new canary node.",
        "type": "",
    }
    sibling_issue = {
        "node_id": "L7.sibling",
        "reason": "merge_suggestions",
        "summary": "Confirm whether this sibling should merge with the canary feature.",
        "type": "",
    }
    state_path.write_text(json.dumps({
        "node_semantics": {
            "L7.old": {"feedback_round": 0, "open_issues": [old_issue]},
            "L7.new": {"feedback_round": 1, "open_issues": [new_issue]},
            "L7.sibling": {"feedback_round": 1, "open_issues": [sibling_issue]},
        },
        "open_issues": [old_issue, new_issue, sibling_issue],
    }), encoding="utf-8")

    node_scoped = reconcile_feedback.classify_semantic_open_issues(
        PID,
        "full-semantic-test",
        source_round="round-001",
        node_ids=["L7.new"],
        created_by="observer",
    )
    assert node_scoped["count"] == 1
    assert node_scoped["items"][0]["source_node_ids"] == ["L7.new"]
    assert "L7.old" not in {
        item["source_node_ids"][0]
        for item in reconcile_feedback.list_feedback_items(PID, "full-semantic-test")
    }

    round_scoped = reconcile_feedback.classify_semantic_open_issues(
        PID,
        "full-semantic-test",
        source_round="round-001",
        created_by="observer",
    )
    assert round_scoped["count"] == 2
    items = {
        item["source_node_ids"][0]
        for item in reconcile_feedback.list_feedback_items(PID, "full-semantic-test")
    }
    assert items == {"L7.new", "L7.sibling"}


def test_reconcile_feedback_classifies_health_issues_when_open_issues_absent(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)
    state_path = reconcile_feedback.semantic_graph_state_path(PID, "full-semantic-test")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "node_semantics": {
            "L7.1": {
                "feedback_round": 3,
                "health_issues": [
                    {
                        "category": "test_gap",
                        "severity": "medium",
                        "summary": "Missing focused tests for retry-state behavior.",
                        "affected_node_ids": ["L7.1"],
                        "suggested_action": "Add a retry-state regression test.",
                    }
                ],
            }
        },
        "health_issues": [
            {
                "category": "test_gap",
                "summary": "Missing focused tests for retry-state behavior.",
                "affected_node_ids": ["L7.1"],
            }
        ],
    }), encoding="utf-8")

    result = reconcile_feedback.classify_semantic_open_issues(
        PID,
        "full-semantic-test",
        source_round="round-003",
        created_by="observer",
    )

    assert result["count"] == 1
    item = result["items"][0]
    assert item["source_node_ids"] == ["L7.1"]
    assert item["feedback_kind"] == "status_observation"
    assert item["status_observation_category"] == "coverage_gap"


def test_reconcile_feedback_classifies_all_semantic_state_rounds(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)
    state_path = reconcile_feedback.semantic_graph_state_path(PID, "full-semantic-test")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({
        "node_semantics": {
            "L7.1": {
                "feedback_round": 1,
                "open_issues": [
                    {
                        "reason": "dependency_patch_suggestions",
                        "summary": "Add typed relation to the task registry.",
                        "target": "agent.governance.task_registry",
                        "type": "add_typed_relation",
                    },
                ],
            },
            "L7.2": {
                "feedback_round": 2,
                "open_issues": [
                    {
                        "reason": "split_suggestions",
                        "summary": "Observer should decide whether this node should split.",
                        "type": "split",
                    },
                ],
            },
        },
    }), encoding="utf-8")

    result = reconcile_feedback.classify_semantic_state_rounds(
        PID,
        "full-semantic-test",
        created_by="observer",
    )
    assert result["rounds"] == ["round-001", "round-002"]
    assert result["created"] == 2
    assert result["summary"]["by_kind"] == {
        "graph_correction": 1,
        "needs_observer_decision": 1,
    }

    again = reconcile_feedback.classify_semantic_state_rounds(
        PID,
        "full-semantic-test",
        created_by="observer",
    )
    assert again["created"] == 0
    assert again["updated"] == 2


def test_reconcile_feedback_review_queue_groups_and_hides_status_noise(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)
    issues = [
        {
            "node_id": "L7.1",
            "reason": "dependency_patch_suggestions",
            "summary": "Add relation from this feature to the task registry.",
            "target": "agent.governance.task_registry",
            "type": "add_relation",
        },
        {
            "node_id": "L7.1",
            "reason": "dependency_patch_suggestions",
            "summary": "Add a second edge note for the same task registry relation.",
            "target": "agent.governance.task_registry",
            "type": "add_relation",
        },
        {
            "node_id": "L7.2",
            "reason": "merge_suggestions",
            "summary": "Confirm whether two features should merge before changing graph state.",
            "type": "",
        },
        {
            "node_id": "L7.3",
            "reason": "dependency_patch_suggestions",
            "summary": "missing_test_binding flag should stay visible until a user asks for a backlog.",
            "type": "",
        },
    ]
    classify = reconcile_feedback.classify_semantic_open_issues(
        PID,
        "full-semantic-test",
        source_round="round-002",
        created_by="observer",
        issues=issues,
    )
    assert classify["count"] == 4

    queue = reconcile_feedback.build_feedback_review_queue(
        PID,
        "full-semantic-test",
        source_round="round-002",
    )
    assert queue["summary"]["raw_count"] == 4
    assert queue["summary"]["hidden_status_observation_count"] == 1
    assert queue["summary"]["visible_group_count"] == 2
    lanes = {group["lane"]: group for group in queue["groups"]}
    assert lanes["review_required"]["requires_human_signoff"] is True
    assert lanes["review_required"]["source_node_ids"] == ["L7.2"]
    assert lanes["graph_patch_candidate"]["item_count"] == 2
    assert lanes["graph_patch_candidate"]["suppressed_count"] == 1
    assert lanes["graph_patch_candidate"]["source_node_ids"] == ["L7.1"]

    with_status = reconcile_feedback.build_feedback_review_queue(
        PID,
        "full-semantic-test",
        source_round="round-002",
        include_status_observations=True,
    )
    assert with_status["summary"]["visible_group_count"] == 3
    assert "status_only" in {group["lane"] for group in with_status["groups"]}


def test_reconcile_feedback_review_queue_can_group_by_feature(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)
    issues = [
        {
            "node_id": "L7.1",
            "reason": "dependency_patch_suggestions",
            "summary": "Add relation from this feature to the task registry.",
            "target": "agent.governance.task_registry",
            "type": "add_typed_relation",
        },
        {
            "node_id": "L7.1",
            "reason": "dependency_patch_suggestions",
            "summary": "Add relation from this feature to the event module.",
            "target": "agent.governance.events",
            "type": "add_typed_relation",
        },
        {
            "node_id": "L7.2",
            "reason": "split_suggestions",
            "summary": "Observer must decide whether this feature should split.",
            "type": "split",
        },
    ]
    classify = reconcile_feedback.classify_semantic_open_issues(
        PID,
        "full-semantic-test",
        source_round="round-003",
        created_by="observer",
        issues=issues,
    )
    assert classify["count"] == 3

    target_queue = reconcile_feedback.build_feedback_review_queue(
        PID,
        "full-semantic-test",
        source_round="round-003",
    )
    assert target_queue["group_by"] == "target"
    assert target_queue["summary"]["visible_group_count"] == 3

    feature_queue = reconcile_feedback.build_feedback_review_queue(
        PID,
        "full-semantic-test",
        source_round="round-003",
        group_by="feature",
    )
    assert feature_queue["group_by"] == "feature"
    assert feature_queue["summary"]["visible_group_count"] == 2
    by_node = {
        tuple(group["source_node_ids"]): group
        for group in feature_queue["groups"]
    }
    assert by_node[("L7.1",)]["item_count"] == 2
    assert by_node[("L7.1",)]["target_count"] == 2
    assert by_node[("L7.1",)]["issue_type_counts"] == {
        "add_typed_relation": 2,
    }
    assert by_node[("L7.2",)]["lane"] == "review_required"

    lane_queue = reconcile_feedback.build_feedback_review_queue(
        PID,
        "full-semantic-test",
        source_round="round-003",
        group_by="lane",
    )
    assert lane_queue["group_by"] == "lane"
    assert lane_queue["summary"]["visible_group_count"] == 2
    by_lane = {group["lane"]: group for group in lane_queue["groups"]}
    assert by_lane["graph_patch_candidate"]["group_by"] == "lane"
    assert by_lane["graph_patch_candidate"]["target_type"] == "feedback_lane"
    assert by_lane["graph_patch_candidate"]["target_id"] == "graph_patch_candidate"
    assert by_lane["graph_patch_candidate"]["item_count"] == 2
    assert by_lane["graph_patch_candidate"]["target_count"] == 2
    assert by_lane["graph_patch_candidate"]["source_node_ids"] == ["L7.1"]
    assert by_lane["review_required"]["item_count"] == 1


def test_semantic_enrichment_uses_project_config_override(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)
    override_path = project / ".aming-claw" / "reconcile" / "semantic_enrichment.yaml"
    override_path.parent.mkdir(parents=True)
    override_path.write_text(
        "\n".join(
            [
                'model: "gpt-test-semantic"',
                "use_ai_default: true",
                "input_policy:",
                "  max_excerpt_chars: 8",
                "prompt_template: |-",
                "  Project-specific semantic analyzer prompt.",
            ]
        ),
        encoding="utf-8",
    )
    seen_payloads: list[dict] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        seen_payloads.append(payload)
        return {"feature_name": "Configured Semantic Feature"}

    result = run_semantic_enrichment(
        conn,
        PID,
        "full-semantic-test",
        project,
        use_ai=None,
        ai_call=fake_ai,
        created_by="test",
    )

    feature = result["semantic_index"]["features"][0]
    assert feature["feature_name"] == "Configured Semantic Feature"
    assert result["semantic_index"]["semantic_config"]["model"] == "gpt-test-semantic"
    assert seen_payloads[0]["instructions"]["model"] == "gpt-test-semantic"
    assert seen_payloads[0]["instructions"]["prompt_template"] == "Project-specific semantic analyzer prompt."
    excerpt = seen_payloads[0]["feature"]["source_excerpt"]["agent/governance/backlog_runtime.py"]
    assert len(excerpt) <= 8
