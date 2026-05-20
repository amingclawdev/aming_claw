from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent.governance import reconcile_batch_memory as bm
from agent.governance import graph_snapshot_store as store
from agent.governance import reconcile_feedback
from agent.governance.reconcile_semantic_enrichment import (
    NODE_SEMANTIC_SELF_CHECK_RULES,
    _batch_key,
    _ensure_semantic_state_schema,
    _persist_semantic_state_to_db,
    _slice_response_self_check,
    _semantic_state_validation,
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


def test_slice_response_self_check_preserves_required_contract_rules():
    result = _slice_response_self_check([
        {
            "self_check": {
                "valid": True,
                "status": "passed",
                "checked_rules": NODE_SEMANTIC_SELF_CHECK_RULES,
                "known_risks": ["source_excerpt_omitted"],
            }
        }
    ])

    assert result["valid"] is True
    assert result["status"] == "passed"
    assert result["checked_rules"] == [
        *NODE_SEMANTIC_SELF_CHECK_RULES,
        "chunk_slices_accounted_for",
    ]
    assert result["checked_rules_count"] == len(result["checked_rules"])
    assert "source_excerpt_omitted" in result["known_risks"]


def test_slice_response_self_check_fails_when_chunk_self_check_missing():
    result = _slice_response_self_check([{"semantic_summary": "chunk without self-check"}])

    assert result["valid"] is False
    assert result["status"] == "failed"
    assert "missing_chunk_self_check" in result["known_risks"]


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


def test_running_semantic_job_without_claim_is_not_double_counted():
    state = {
        "semantic_jobs": {
            "L7.1": {
                "node_id": "L7.1",
                "status": "running",
                "attempt_count": 1,
                "worker_id": "",
                "claim_id": "",
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
    assert job["worker_id"] == ""
    assert job["claim_id"] == ""


def test_pending_semantic_job_does_not_downgrade_running_claim():
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
        status="pending_ai",
        feedback_round=1,
        batch_index=None,
        updated_at="2026-05-18T00:01:00Z",
    )

    job = state["semantic_jobs"]["L7.1"]
    assert job["status"] == "running"
    assert job["attempt_count"] == 1
    assert job["worker_id"] == "semantic-worker-1"
    assert job["claim_id"] == "claim-1"


def test_persist_semantic_jobs_preserves_claimed_attempt_from_stale_state(conn):
    _ensure_semantic_state_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_semantic_jobs
          (project_id, snapshot_id, node_id, status, attempt_count,
           worker_id, claim_id, claimed_at, lease_expires_at, claimed_by,
           updated_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            PID,
            "scope-demo",
            "L7.1",
            "running",
            1,
            "semantic_worker_inproc",
            "claim-1",
            "2026-05-18T00:00:00Z",
            "2026-05-18T00:10:00Z",
            "semantic_worker_inproc",
            "2026-05-18T00:00:00Z",
            "2026-05-18T00:00:00Z",
        ),
    )

    _persist_semantic_state_to_db(
        conn,
        PID,
        "scope-demo",
        {
            "semantic_jobs": {
                "L7.1": {
                    "node_id": "L7.1",
                    "status": "pending_ai",
                    "attempt_count": 1,
                    "updated_at": "2026-05-18T00:00:30Z",
                }
            }
        },
    )

    row = conn.execute(
        """
        SELECT status, attempt_count, worker_id, claim_id
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ? AND node_id = ?
        """,
        (PID, "scope-demo", "L7.1"),
    ).fetchone()
    assert dict(row) == {
        "status": "running",
        "attempt_count": 1,
        "worker_id": "semantic_worker_inproc",
        "claim_id": "claim-1",
    }

    _persist_semantic_state_to_db(
        conn,
        PID,
        "scope-demo",
        {
            "semantic_jobs": {
                "L7.1": {
                    "node_id": "L7.1",
                    "status": "running",
                    "attempt_count": 2,
                    "updated_at": "2026-05-18T00:01:00Z",
                }
            }
        },
    )

    row = conn.execute(
        """
        SELECT status, attempt_count, worker_id, claim_id
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ? AND node_id = ?
        """,
        (PID, "scope-demo", "L7.1"),
    ).fetchone()
    assert dict(row) == {
        "status": "running",
        "attempt_count": 1,
        "worker_id": "semantic_worker_inproc",
        "claim_id": "claim-1",
    }

    _persist_semantic_state_to_db(
        conn,
        PID,
        "scope-demo",
        {
            "semantic_jobs": {
                "L7.1": {
                    "node_id": "L7.1",
                    "status": "ai_complete",
                    "attempt_count": 2,
                    "updated_at": "2026-05-18T00:02:00Z",
                }
            }
        },
    )

    row = conn.execute(
        """
        SELECT status, attempt_count, worker_id, claim_id
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ? AND node_id = ?
        """,
        (PID, "scope-demo", "L7.1"),
    ).fetchone()
    assert dict(row) == {
        "status": "ai_complete",
        "attempt_count": 1,
        "worker_id": "",
        "claim_id": "",
    }

    _persist_semantic_state_to_db(
        conn,
        PID,
        "scope-demo",
        {
            "semantic_jobs": {
                "L7.1": {
                    "node_id": "L7.1",
                    "status": "pending_ai",
                    "attempt_count": 1,
                    "updated_at": "2026-05-18T00:03:00Z",
                }
            }
        },
    )

    row = conn.execute(
        """
        SELECT status, attempt_count, worker_id, claim_id
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ? AND node_id = ?
        """,
        (PID, "scope-demo", "L7.1"),
    ).fetchone()
    assert dict(row) == {
        "status": "ai_complete",
        "attempt_count": 1,
        "worker_id": "",
        "claim_id": "",
    }


def test_persist_semantic_state_can_scope_node_rows_for_parallel_worker_runs(conn):
    _ensure_semantic_state_schema(conn)

    def entry(node_id: str, feature_name: str, round_no: int) -> dict:
        return {
            "node_id": node_id,
            "status": "ai_complete",
            "feature_hash": f"sha256:{node_id}",
            "file_hashes": {},
            "feature_name": feature_name,
            "semantic_summary": f"summary {feature_name}",
            "feedback_round": round_no,
            "updated_at": f"2026-05-19T00:0{round_no}:00Z",
        }

    _persist_semantic_state_to_db(
        conn,
        PID,
        "scope-demo",
        {
            "node_semantics": {
                "L7.1": entry("L7.1", "fresh one", 4),
            },
            "semantic_jobs": {
                "L7.1": {
                    "node_id": "L7.1",
                    "status": "ai_complete",
                    "feature_hash": "sha256:L7.1",
                    "feedback_round": 4,
                    "attempt_count": 1,
                    "updated_at": "2026-05-19T00:04:00Z",
                },
            },
        },
        submit_for_review=True,
        review_node_ids={"L7.1"},
        persist_node_ids={"L7.1"},
    )

    stale_worker_state = {
        "node_semantics": {
            "L7.1": entry("L7.1", "stale one", 2),
            "L7.2": entry("L7.2", "fresh two", 4),
        },
        "semantic_jobs": {
            "L7.1": {
                "node_id": "L7.1",
                "status": "ai_pending",
                "feature_hash": "sha256:L7.1",
                "feedback_round": 2,
                "attempt_count": 0,
                "updated_at": "2026-05-19T00:02:00Z",
            },
            "L7.2": {
                "node_id": "L7.2",
                "status": "ai_complete",
                "feature_hash": "sha256:L7.2",
                "feedback_round": 4,
                "attempt_count": 1,
                "updated_at": "2026-05-19T00:04:00Z",
            },
        },
    }
    _persist_semantic_state_to_db(
        conn,
        PID,
        "scope-demo",
        stale_worker_state,
        submit_for_review=True,
        review_node_ids={"L7.2"},
        persist_node_ids={"L7.2"},
    )

    rows = conn.execute(
        """
        SELECT node_id, status, semantic_json, feedback_round
        FROM graph_semantic_nodes
        WHERE project_id = ? AND snapshot_id = ?
        ORDER BY node_id
        """,
        (PID, "scope-demo"),
    ).fetchall()
    by_node = {
        row["node_id"]: {
            "status": row["status"],
            "feedback_round": row["feedback_round"],
            "semantic": json.loads(row["semantic_json"]),
        }
        for row in rows
    }
    assert by_node["L7.1"]["semantic"]["feature_name"] == "fresh one"
    assert by_node["L7.1"]["feedback_round"] == 4
    assert by_node["L7.2"]["semantic"]["feature_name"] == "fresh two"
    assert by_node["L7.2"]["feedback_round"] == 4
    assert by_node["L7.1"]["status"] == "pending_review"
    assert by_node["L7.2"]["status"] == "pending_review"

    jobs = conn.execute(
        """
        SELECT node_id, status, feedback_round, attempt_count
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ?
        ORDER BY node_id
        """,
        (PID, "scope-demo"),
    ).fetchall()
    by_job = {row["node_id"]: dict(row) for row in jobs}
    assert by_job["L7.1"]["status"] == "ai_complete"
    assert by_job["L7.1"]["feedback_round"] == 4
    assert by_job["L7.2"]["status"] == "ai_complete"
    assert by_job["L7.2"]["feedback_round"] == 4


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


def _graph(
    node_id: str = "L7.1",
    *,
    include_extra: bool = False,
    include_function_hash_evidence: bool = False,
) -> dict:
    metadata = {
        "subsystem": "backlog",
        "config_files": ["config/roles/default/pm.yaml"],
        "functions": [
            {
                "name": "claim_next",
                "path": "agent/governance/backlog_runtime.py",
                "lineno": 12,
            }
        ],
    }
    if include_function_hash_evidence:
        metadata.update({
            "function_hashes": {
                "agent.governance.backlog_runtime::claim_next": "sha256:source-claim-next",
            },
            "test_functions": [
                "agent.tests.test_backlog_runtime::test_claim_next",
            ],
            "test_function_hashes": {
                "agent.tests.test_backlog_runtime::test_claim_next": "sha256:test-claim-next",
            },
            "test_function_lines": {
                "test_claim_next": [1, 2],
                "agent.tests.test_backlog_runtime::test_claim_next": [1, 2],
            },
        })
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
            "metadata": metadata,
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
    include_function_hash_evidence: bool = False,
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
        graph_json=_graph(
            include_extra=include_extra,
            include_function_hash_evidence=include_function_hash_evidence,
        ),
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
    assert "source_excerpt" not in seen_payloads[0]["payload"]["feature"]
    assert seen_payloads[0]["payload"]["feature"]["function_index"]["mode"] == "function_index"
    assert seen_payloads[0]["payload"]["semantic_retrieval"]["mode"] == "function_index"
    assert seen_payloads[0]["payload"]["semantic_evidence"]["schema_version"] == "semantic_evidence.v1"
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


def test_semantic_enrichment_payload_carries_test_function_hash_evidence(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project, include_function_hash_evidence=True)
    seen_payloads: list[dict] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        seen_payloads.append(payload)
        feature = payload["feature"]
        assert feature["function_hashes"] == {
            "agent.governance.backlog_runtime::claim_next": "sha256:source-claim-next",
        }
        assert feature["test_functions"] == [
            "agent.tests.test_backlog_runtime::test_claim_next",
        ]
        assert feature["test_function_hashes"] == {
            "agent.tests.test_backlog_runtime::test_claim_next": "sha256:test-claim-next",
        }
        assert feature["test_function_lines"]["test_claim_next"] == [1, 2]
        return {
            "feature_name": "Backlog Runtime State Flow",
            "semantic_summary": "Owns backlog task state transitions.",
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
    )

    assert seen_payloads
    feature = result["semantic_index"]["features"][0]
    assert feature["function_hashes"] == {
        "agent.governance.backlog_runtime::claim_next": "sha256:source-claim-next",
    }
    assert feature["test_function_hashes"] == {
        "agent.tests.test_backlog_runtime::test_claim_next": "sha256:test-claim-next",
    }
    assert feature["test_function_lines"]["agent.tests.test_backlog_runtime::test_claim_next"] == [1, 2]


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


def test_semantic_enrichment_builds_ai_graph_query_audit_trace(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project)
    graph = _graph()
    store.index_graph_snapshot(
        conn,
        PID,
        "full-semantic-test",
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    conn.commit()
    seen_payloads: list[dict] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        seen_payloads.append(payload)
        audit = payload["graph_query_audit"]
        assert audit["query_source"] == "ai_semantic_review"
        assert audit["query_purpose"] == "semantic_enrichment"
        assert audit["target_node_id"] == "L7.1"
        assert audit["trace_id"]
        assert audit["status"] == "complete"
        assert [query["tool"] for query in audit["queries"][:2]] == ["get_node", "get_neighbors"]
        assert [query["tool"] for query in audit["queries"][2:]] == ["find_node_by_path"] * 4
        assert audit["queries"][0]["ok"] is True
        contract = payload["instructions"]["graph_contract"]["deps_graph"]["depends_on"]
        assert contract["direction"] == "dependency_to_dependent"
        context_contract = payload["graph_query_context"]["graph_contract"]["deps_graph"]["depends_on"]
        assert context_contract["source_role"] == "dependency_provider_prerequisite"
        neighbor_contract = payload["graph_query_context"]["neighbors"]["graph_contract"]["deps_graph"]["depends_on"]
        assert "B -> A" in neighbor_contract["interpretation"]
        bindings = {
            item["path"]: set(item["roles"])
            for item in payload["graph_query_context"]["path_bindings"]
        }
        assert bindings["agent/governance/backlog_runtime.py"] == {"primary"}
        assert bindings["agent/tests/test_backlog_runtime.py"] == {"test"}
        assert bindings["docs/dev/backlog-runtime.md"] == {"doc"}
        assert bindings["config/roles/default/pm.yaml"] == {"config", "config_ref"}
        evidence = payload["semantic_evidence"]
        assert evidence["trace_id"] == audit["trace_id"]
        assert evidence["coverage"]["path_binding_count"] == 4
        assert {item["kind"] for item in evidence["evidence_items"]} >= {
            "graph_node",
            "graph_neighbors",
            "file_binding",
            "function_index",
        }
        return {
            "feature_name": "Audited Backlog Runtime",
            "semantic_summary": "Uses audited graph context before semantic output.",
            "intent": "audit semantic graph evidence",
            "domain_label": "governance.audit",
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
        semantic_ai_scope="selected",
        semantic_node_ids=["L7.1"],
        created_by="semantic-worker-test",
        trace_dir=project / "semantic-trace",
    )

    assert len(seen_payloads) == 1
    audit = seen_payloads[0]["graph_query_audit"]
    trace = conn.execute(
        """
        SELECT query_source, query_purpose, actor, run_id, status
        FROM graph_query_traces
        WHERE project_id=? AND snapshot_id=? AND trace_id=?
        """,
        (PID, "full-semantic-test", audit["trace_id"]),
    ).fetchone()
    assert dict(trace) == {
        "query_source": "ai_semantic_review",
        "query_purpose": "semantic_enrichment",
        "actor": "semantic-worker-test",
        "run_id": audit["run_id"],
        "status": "complete",
    }
    event_tools = [
        row["tool"]
        for row in conn.execute(
            "SELECT tool FROM graph_query_events WHERE trace_id=? ORDER BY seq",
            (audit["trace_id"],),
        ).fetchall()
    ]
    assert event_tools == [
        "get_node",
        "get_neighbors",
        "find_node_by_path",
        "find_node_by_path",
        "find_node_by_path",
        "find_node_by_path",
    ]

    feature = result["semantic_index"]["features"][0]
    assert feature["graph_query_audit"]["trace_id"] == audit["trace_id"]
    output = json.loads((project / "semantic-trace" / "feature-outputs" / "L7.1.json").read_text())
    assert output["semantic_entry"]["graph_query_audit"]["trace_id"] == audit["trace_id"]
    row = conn.execute(
        """
        SELECT semantic_json FROM graph_semantic_nodes
        WHERE project_id=? AND snapshot_id=? AND node_id=?
        """,
        (PID, "full-semantic-test", "L7.1"),
    ).fetchone()
    persisted = json.loads(row["semantic_json"])
    assert persisted["graph_query_audit"]["trace_id"] == audit["trace_id"]


def test_semantic_ai_graph_query_audit_uses_budget_safe_compact_neighbors(conn, tmp_path):
    project = tmp_path / "project"
    _write(
        project / "agent" / "governance" / "backlog_runtime.py",
        "def claim_next():\n    return 'task'\n",
    )
    _write(
        project / "agent" / "governance" / "large_neighbor.py",
        "def large_neighbor():\n    return None\n",
    )
    huge_functions = [
        {"name": f"generated_{idx}", "path": "agent/governance/large_neighbor.py", "lineno": idx}
        for idx in range(2000)
    ]
    graph = {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.1",
                    "layer": "L7",
                    "title": "Backlog Runtime",
                    "kind": "service_runtime",
                    "primary": ["agent/governance/backlog_runtime.py"],
                    "metadata": {
                        "subsystem": "backlog",
                        "functions": [{"name": "claim_next", "path": "agent/governance/backlog_runtime.py"}],
                    },
                },
                {
                    "id": "L7.2",
                    "layer": "L7",
                    "title": "Large Neighbor",
                    "kind": "service_runtime",
                    "primary": ["agent/governance/large_neighbor.py"],
                    "metadata": {
                        "subsystem": "backlog",
                        "function_count": len(huge_functions),
                        "functions": huge_functions,
                    },
                },
            ],
            "edges": [{"source": "L7.1", "target": "L7.2", "type": "depends_on"}],
        }
    }
    store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-test",
        commit_sha="abc1234",
        snapshot_kind="full",
        graph_json=graph,
        notes=json.dumps({"state_only": True}),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        "full-semantic-test",
        nodes=graph["deps_graph"]["nodes"],
        edges=store.graph_payload_edges(graph),
    )
    conn.commit()
    seen_payloads: list[dict] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        seen_payloads.append(payload)
        audit = payload["graph_query_audit"]
        assert audit["status"] == "complete"
        assert audit["usage"]["result_chars"] < 80_000
        neighbors = payload["graph_query_context"]["neighbors"]
        assert neighbors["compact"] is True
        assert neighbors["nodes"][0]["metadata"]["function_count"] == len(huge_functions)
        assert "functions" not in neighbors["nodes"][0]["metadata"]
        return {
            "feature_name": "Budget Safe Semantic Context",
            "semantic_summary": "Uses compact audited graph context.",
            "intent": "avoid oversized graph payloads",
            "domain_label": "governance.audit",
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
        semantic_ai_scope="selected",
        semantic_node_ids=["L7.1"],
        created_by="semantic-worker-test",
    )

    assert len(seen_payloads) == 1
    audit = seen_payloads[0]["graph_query_audit"]
    assert [query["tool"] for query in audit["queries"]] == [
        "get_node",
        "get_neighbors",
        "find_node_by_path",
    ]
    trace = conn.execute(
        """
        SELECT status FROM graph_query_traces
        WHERE project_id=? AND snapshot_id=? AND trace_id=?
        """,
        (PID, "full-semantic-test", audit["trace_id"]),
    ).fetchone()
    assert trace["status"] == "complete"
    assert result["semantic_index"]["features"][0]["graph_query_audit"]["status"] == "complete"


def test_semantic_enrichment_chunks_large_function_node_and_aggregates(conn, tmp_path):
    project = tmp_path / "project"
    source = "\n\n".join(
        f"def generated_{idx}():\n    return {idx}\n"
        for idx in range(6)
    )
    _write(project / "agent" / "governance" / "large_node.py", source)
    functions = [
        {
            "name": f"generated_{idx}",
            "path": "agent/governance/large_node.py",
            "lineno": idx * 3 + 1,
        }
        for idx in range(6)
    ]
    graph = {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.large",
                    "layer": "L7",
                    "title": "Large Semantic Node",
                    "kind": "service_runtime",
                    "primary": ["agent/governance/large_node.py"],
                    "metadata": {
                        "subsystem": "semantic",
                        "function_count": len(functions),
                        "functions": functions,
                        "function_hashes": {
                            f"agent.governance.large_node::generated_{idx}": f"sha256:fn-{idx}"
                            for idx in range(6)
                        },
                        "symbol_refs": [
                            {"name": f"symbol_{idx}", "path": "agent/governance/large_node.py"}
                            for idx in range(200)
                        ],
                    },
                    "symbol_refs": [
                        {"name": f"symbol_{idx}", "path": "agent/governance/large_node.py"}
                        for idx in range(200)
                    ],
                    "test_symbol_refs": [
                        {"name": f"test_symbol_{idx}", "path": "agent/tests/test_large_node.py"}
                        for idx in range(200)
                    ],
                    "test_functions": [f"test_generated_{idx}" for idx in range(200)],
                    "test_function_hashes": {
                        f"agent.tests.test_large_node::test_generated_{idx}": f"sha256:test-{idx}"
                        for idx in range(200)
                    },
                }
            ],
            "edges": [],
        }
    }
    store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-test",
        commit_sha="abc1234",
        snapshot_kind="full",
        graph_json=graph,
        notes=json.dumps({"state_only": True}),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        "full-semantic-test",
        nodes=graph["deps_graph"]["nodes"],
        edges=[],
    )
    conn.commit()
    calls: list[dict] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        calls.append({"stage": stage, "payload": payload})
        assert stage == "reconcile_semantic_feature_slice"
        chunk = payload["semantic_chunk"]
        assert chunk["mode"] == "function_slice"
        assert chunk["context_mode"] == "function_index"
        assert len(chunk["covered_functions"]) <= 2
        assert len(payload["feature"]["metadata"]["functions"]) <= 2
        assert payload["feature"]["metadata"]["total_function_count"] == 6
        assert "source_excerpt" not in payload["feature"]
        assert payload["feature"]["function_index"]["mode"] == "function_index"
        assert payload["semantic_retrieval"]["mode"] == "function_index"
        assert payload["semantic_retrieval"]["source_excerpt_included"] is False
        assert payload["semantic_retrieval"]["audit_boundary"] == "payload_only"
        assert payload["semantic_evidence"]["coverage"]["function_index_present"] is True
        assert "symbol_refs" not in payload["feature"]
        assert "test_symbol_refs" not in payload["feature"]
        assert "test_functions" not in payload["feature"]
        assert "test_function_hashes" not in payload["feature"]
        assert "graph_query_audit" in payload
        names = ", ".join(item["name"] for item in chunk["covered_functions"])
        return {
            "feature_name": f"Slice {chunk['slice_index']}",
            "semantic_summary": f"Slice covers {names}.",
            "intent": "chunked semantic coverage",
            "domain_label": "semantic.chunk",
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
        semantic_ai_scope="selected",
        semantic_node_ids=["L7.large"],
        semantic_ai_chunk_function_threshold=3,
        semantic_ai_chunk_max_functions_per_slice=2,
        semantic_ai_chunk_max_slices=8,
        created_by="semantic-worker-test",
        trace_dir=project / "semantic-trace",
    )

    assert [call["stage"] for call in calls] == [
        "reconcile_semantic_feature_slice",
        "reconcile_semantic_feature_slice",
        "reconcile_semantic_feature_slice",
    ]
    assert result["summary"]["ai_complete_count"] == 1
    assert result["summary"]["ai_chunked_node_count"] == 1
    assert result["summary"]["ai_chunk_call_count"] == 3
    feature = result["semantic_index"]["features"][0]
    assert feature["enrichment_status"] == "ai_complete"
    assert feature["semantic_chunking"]["status"] == "complete"
    assert feature["semantic_chunking"]["slice_count"] == 3
    assert feature["semantic_chunking"]["completed_slice_count"] == 3
    assert feature["self_check"]["checked_rules"][-1] == "chunk_slices_accounted_for"
    assert Path(result["summary"]["chunk_payload_input_dir"]).exists()
    chunk_payload = json.loads(
        (project / "semantic-trace" / "chunk-inputs" / "L7.large-slice-000.json").read_text()
    )
    assert len(json.dumps(chunk_payload, sort_keys=True)) < 50000
    assert "source_excerpt" not in chunk_payload["feature"]
    assert chunk_payload["semantic_retrieval"]["mode"] == "function_index"
    output = json.loads((project / "semantic-trace" / "feature-outputs" / "L7.large.json").read_text())
    assert output["semantic_entry"]["semantic_chunking"]["mode"] == "function_slices"


def test_semantic_enrichment_source_excerpt_chunk_mode_remains_available(conn, tmp_path):
    project = tmp_path / "project"
    source = "\n\n".join(
        f"def generated_{idx}():\n    return {idx}\n"
        for idx in range(4)
    )
    _write(project / "agent" / "governance" / "large_excerpt.py", source)
    functions = [
        {
            "name": f"generated_{idx}",
            "path": "agent/governance/large_excerpt.py",
            "lineno": idx * 3 + 1,
        }
        for idx in range(4)
    ]
    graph = {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.excerpt",
                    "layer": "L7",
                    "title": "Large Excerpt Node",
                    "primary": ["agent/governance/large_excerpt.py"],
                    "metadata": {
                        "function_count": len(functions),
                        "functions": functions,
                    },
                }
            ],
            "edges": [],
        }
    }
    store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-test",
        commit_sha="abc1234",
        snapshot_kind="full",
        graph_json=graph,
        notes=json.dumps({"state_only": True}),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        "full-semantic-test",
        nodes=graph["deps_graph"]["nodes"],
        edges=[],
    )
    conn.commit()

    def fake_ai(stage: str, payload: dict) -> dict:
        assert stage == "reconcile_semantic_feature_slice"
        assert payload["semantic_chunk"]["context_mode"] == "source_excerpt"
        assert payload["semantic_retrieval"]["mode"] == "source_excerpt"
        assert payload["semantic_retrieval"]["source_excerpt_included"] is True
        excerpt = payload["feature"]["source_excerpt"]["agent/governance/large_excerpt.py"]
        assert "def generated_0" in excerpt or "def generated_2" in excerpt
        return {
            "feature_name": "Excerpt slice",
            "semantic_summary": "Excerpt-backed bounded function slice.",
            "self_check": {
                "required": True,
                "valid": True,
                "status": "passed",
                "checked_rules": ["required_fields_present"],
                "checked_rules_count": 1,
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
        semantic_ai_scope="selected",
        semantic_node_ids=["L7.excerpt"],
        semantic_ai_chunk_function_threshold=3,
        semantic_ai_chunk_max_functions_per_slice=2,
        semantic_ai_chunk_context_mode="source_excerpt",
        created_by="semantic-worker-test",
    )

    assert result["summary"]["ai_complete_count"] == 1
    assert result["semantic_index"]["features"][0]["semantic_chunking"]["context_mode"] == "source_excerpt"


def test_semantic_enrichment_retries_prompt_too_long_with_function_slices(conn, tmp_path):
    project = tmp_path / "project"
    source = "\n\n".join(
        f"def generated_{idx}():\n    return {idx}\n"
        for idx in range(4)
    )
    _write(project / "agent" / "governance" / "large_retry.py", source)
    functions = [
        {
            "name": f"generated_{idx}",
            "path": "agent/governance/large_retry.py",
            "lineno": idx * 3 + 1,
        }
        for idx in range(4)
    ]
    graph = {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.retry",
                    "layer": "L7",
                    "title": "Retry Semantic Node",
                    "kind": "service_runtime",
                    "primary": ["agent/governance/large_retry.py"],
                    "metadata": {
                        "subsystem": "semantic",
                        "function_count": len(functions),
                        "functions": functions,
                    },
                }
            ],
            "edges": [],
        }
    }
    store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-semantic-test",
        commit_sha="abc1234",
        snapshot_kind="full",
        graph_json=graph,
        notes=json.dumps({"state_only": True}),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        "full-semantic-test",
        nodes=graph["deps_graph"]["nodes"],
        edges=[],
    )
    conn.commit()
    stages: list[str] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        stages.append(stage)
        if stage == "reconcile_semantic_feature":
            return {"_ai_error": "Prompt is too long", "terminal_reason": "prompt_too_long"}
        assert stage == "reconcile_semantic_feature_slice"
        assert payload["semantic_chunk"]["fallback_error"] == "Prompt is too long"
        return {
            "feature_name": "Retried slice",
            "semantic_summary": "Retried bounded function slice.",
            "self_check": {
                "required": True,
                "valid": True,
                "status": "passed",
                "checked_rules": ["required_fields_present"],
                "checked_rules_count": 1,
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
        semantic_ai_scope="selected",
        semantic_node_ids=["L7.retry"],
        semantic_ai_chunk_large_nodes=True,
        semantic_ai_chunk_function_threshold=99,
        semantic_ai_chunk_max_functions_per_slice=2,
        created_by="semantic-worker-test",
    )

    assert stages[0] == "reconcile_semantic_feature"
    assert stages[1:] == [
        "reconcile_semantic_feature_slice",
        "reconcile_semantic_feature_slice",
    ]
    assert result["summary"]["ai_complete_count"] == 1
    assert result["summary"]["ai_error_count"] == 0
    feature = result["semantic_index"]["features"][0]
    assert feature["semantic_chunking"]["status"] == "complete"
    row = conn.execute(
        """
        SELECT status, semantic_json FROM graph_semantic_nodes
        WHERE project_id=? AND snapshot_id=? AND node_id=?
        """,
        (PID, "full-semantic-test", "L7.retry"),
    ).fetchone()
    assert row["status"] == "ai_complete"
    assert json.loads(row["semantic_json"])["semantic_chunking"]["slice_count"] == 2


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


def test_semantic_enrichment_batch_payload_carries_per_feature_graph_query_audit(conn, tmp_path):
    project = tmp_path / "project"
    _create_snapshot(conn, project, include_extra=True)
    graph = _graph(include_extra=True)
    store.index_graph_snapshot(
        conn,
        PID,
        "full-semantic-test",
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    conn.commit()
    seen_payloads: list[dict] = []

    def fake_ai(stage: str, payload: dict) -> dict:
        seen_payloads.append(payload)
        features = payload["features"]
        assert {item["feature"]["node_id"] for item in features} == {"L7.1", "L7.2"}
        for item in features:
            audit = item["graph_query_audit"]
            assert audit["query_source"] == "ai_semantic_review"
            assert audit["query_purpose"] == "semantic_enrichment"
            assert audit["run_id"].endswith(":batch-000")
            assert audit["status"] == "complete"
            assert item["graph_query_context"]["node"]
            assert "source_excerpt" not in item["feature"]
            assert item["semantic_retrieval"]["mode"] == "function_index"
            assert item["semantic_evidence"]["trace_id"] == audit["trace_id"]
        return {
            "features": [
                {
                    "node_id": item["feature"]["node_id"],
                    "feature_name": f"Audited batch {item['feature']['node_id']}",
                    "semantic_summary": f"Audited batch summary {item['feature']['node_id']}",
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
                for item in features
            ],
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
        created_by="semantic-worker-test",
    )

    assert len(seen_payloads) == 1
    traces = conn.execute(
        """
        SELECT query_source, query_purpose, actor, run_id, status
        FROM graph_query_traces
        WHERE project_id=? AND snapshot_id=?
        ORDER BY run_id
        """,
        (PID, "full-semantic-test"),
    ).fetchall()
    assert len(traces) == 2
    assert {row["query_source"] for row in traces} == {"ai_semantic_review"}
    assert {row["query_purpose"] for row in traces} == {"semantic_enrichment"}
    assert {row["actor"] for row in traces} == {"semantic-worker-test"}
    assert {row["status"] for row in traces} == {"complete"}

    by_id = {item["node_id"]: item for item in result["semantic_index"]["features"]}
    assert by_id["L7.1"]["graph_query_audit"]["trace_id"]
    assert by_id["L7.2"]["graph_query_audit"]["trace_id"]
    assert by_id["L7.1"]["graph_query_audit"]["trace_id"] != by_id["L7.2"]["graph_query_audit"]["trace_id"]


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


def test_semantic_state_validation_marks_test_function_hash_mismatch_stale():
    feature = {
        "feature_hash": "sha256:feature-a",
        "file_hashes": {"agent/governance/backlog_runtime.py": "sha256:file-a"},
        "function_hashes": {
            "agent.governance.backlog_runtime::claim_next": "sha256:source-a",
        },
        "test_function_hashes": {
            "agent.tests.test_backlog_runtime::test_claim_next": "sha256:test-b",
        },
    }
    state_entry = {
        "feature_hash": "sha256:feature-a",
        "file_hashes": {"agent/governance/backlog_runtime.py": "sha256:file-a"},
        "function_hashes": {
            "agent.governance.backlog_runtime::claim_next": "sha256:source-a",
        },
        "test_function_hashes": {
            "agent.tests.test_backlog_runtime::test_claim_next": "sha256:test-a",
        },
    }

    validation = _semantic_state_validation(feature, state_entry)

    assert validation["status"] == "stale_hash_mismatch"
    assert validation["valid"] is False
    assert validation["feature_hash_match"] is True
    assert validation["file_hash_match"] is True
    assert validation["function_hash_match"] is True
    assert validation["test_function_hash_match"] is False


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
                "execution_policy:",
                "  chunk_context_mode: source_excerpt",
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
