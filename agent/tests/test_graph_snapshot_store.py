from __future__ import annotations

import json
import sqlite3

import pytest

from agent.governance import graph_snapshot_store as store
from agent.governance import db
from agent.governance.baseline_service import create_baseline
from agent.governance.db import _ensure_schema


PID = "graph-snapshot-test"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def test_schema_migration_is_idempotent(conn):
    _ensure_schema(conn)
    _ensure_schema(conn)

    table_names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "graph_snapshots",
        "graph_snapshot_refs",
        "graph_ref_events",
        "graph_nodes_index",
        "graph_edges_index",
        "graph_drift_ledger",
        "pending_scope_reconcile",
        "reconcile_run_metrics",
    }.issubset(table_names)
    snapshot_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(graph_snapshots)").fetchall()
    }
    assert {"ref_name", "branch_ref"}.issubset(snapshot_columns)

    version = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    assert version["value"] == str(db.SCHEMA_VERSION)


def test_create_index_and_activate_snapshot(conn, tmp_path):
    _ensure_schema(conn)
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-abc1234-test",
        commit_sha="abc1234deadbeef",
        snapshot_kind="full",
        graph_json={"deps_graph": {"nodes": []}},
        file_inventory=[{"path": "agent/governance/foo.py"}],
        drift_ledger=[],
        created_by="test",
    )

    assert snapshot["snapshot_id"] == "full-abc1234-test"
    assert snapshot["graph_sha256"]
    assert (tmp_path / PID / "graph-snapshots" / snapshot["snapshot_id"] / "graph.json").exists()

    counts = store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=[
            {
                "id": "L7.1",
                "layer": "L7",
                "title": "Graph Store",
                "primary": ["agent/governance/graph_snapshot_store.py"],
                "secondary": ["docs/dev/proposal-graph-governance-unified-v3.md"],
                "test": ["agent/tests/test_graph_snapshot_store.py"],
                "metadata": {"kind": "state_store", "subsystem": "governance"},
            }
        ],
        edges=[
            {
                "source": "L7.1",
                "target": "L7.2",
                "edge_type": "depends_on",
                "direction": "dependency",
                "evidence": {"reason": "unit-test"},
            }
        ],
    )
    assert counts == {"nodes": 1, "edges": 1}

    activation = store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    assert activation["previous_snapshot_id"] == ""
    assert activation["graph_ref_event_id"]

    active = store.get_active_graph_snapshot(conn, PID)
    assert active["snapshot_id"] == snapshot["snapshot_id"]
    assert active["commit_sha"] == "abc1234deadbeef"

    ref_events = store.list_graph_ref_events(conn, PID, ref_name="active")
    assert len(ref_events) == 1
    assert ref_events[0]["operation_type"] == "activate"
    assert ref_events[0]["old_snapshot_id"] == ""
    assert ref_events[0]["new_snapshot_id"] == snapshot["snapshot_id"]
    assert ref_events[0]["new_commit"] == "abc1234deadbeef"
    assert ref_events[0]["evidence"]["projection_status"] in {"rebuilt", "already_present", "skipped"}

    node = conn.execute(
        "SELECT * FROM graph_nodes_index WHERE project_id=? AND snapshot_id=? AND node_id=?",
        (PID, snapshot["snapshot_id"], "L7.1"),
    ).fetchone()
    assert json.loads(node["primary_files_json"]) == ["agent/governance/graph_snapshot_store.py"]
    assert json.loads(node["metadata_json"])["kind"] == "state_store"


def test_activate_snapshot_compare_and_swap_rejects_stale_writer(conn):
    _ensure_schema(conn)
    first = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-a111111-one",
        commit_sha="a111111",
        snapshot_kind="full",
    )
    second = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-b222222-two",
        commit_sha="b222222",
        snapshot_kind="scope",
    )

    store.activate_graph_snapshot(conn, PID, first["snapshot_id"])

    with pytest.raises(store.GraphSnapshotConflictError):
        store.activate_graph_snapshot(
            conn,
            PID,
            second["snapshot_id"],
            expected_old_snapshot_id="not-the-active-snapshot",
        )

    store.activate_graph_snapshot(
        conn,
        PID,
        second["snapshot_id"],
        expected_old_snapshot_id=first["snapshot_id"],
    )
    active = store.get_active_graph_snapshot(conn, PID)
    assert active["snapshot_id"] == second["snapshot_id"]

    first_row = conn.execute(
        "SELECT status FROM graph_snapshots WHERE project_id=? AND snapshot_id=?",
        (PID, first["snapshot_id"]),
    ).fetchone()
    assert first_row["status"] == store.SNAPSHOT_STATUS_SUPERSEDED

    ref_events = store.list_graph_ref_events(conn, PID, ref_name="active")
    by_new = {event["new_snapshot_id"]: event for event in ref_events}
    assert set(by_new) == {first["snapshot_id"], second["snapshot_id"]}
    assert by_new[second["snapshot_id"]]["old_snapshot_id"] == first["snapshot_id"]
    assert by_new[second["snapshot_id"]]["old_commit"] == "a111111"
    assert by_new[second["snapshot_id"]]["new_commit"] == "b222222"


def test_graph_ref_events_record_rollback_epoch_and_branch_ref_isolation(conn):
    _ensure_schema(conn)
    base = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-base-rollback",
        commit_sha="base",
        snapshot_kind="full",
    )
    branch = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-branch-candidate",
        commit_sha="branch-head",
        snapshot_kind="scope",
    )
    rollback = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-base-rollback",
        commit_sha="base",
        snapshot_kind="scope",
    )

    store.activate_graph_snapshot(conn, PID, base["snapshot_id"], auto_rebuild_projection=False)
    store.activate_graph_snapshot(
        conn,
        PID,
        branch["snapshot_id"],
        ref_name="refs/heads/codex/feature",
        operation_type="merge",
        branch_ref="refs/heads/codex/feature",
        batch_id="batch-rollback",
        merge_queue_id="mergeq-rollback",
        merge_epoch="merge-001",
        auto_rebuild_projection=False,
    )
    active = store.get_active_graph_snapshot(conn, PID, ref_name="active")
    assert active["snapshot_id"] == base["snapshot_id"]
    branch_snapshot = store.get_graph_snapshot(conn, PID, branch["snapshot_id"])
    assert branch_snapshot["status"] == store.SNAPSHOT_STATUS_CANDIDATE

    result = store.activate_graph_snapshot(
        conn,
        PID,
        rollback["snapshot_id"],
        operation_type="rollback",
        batch_id="batch-rollback",
        rollback_epoch="rollback-001",
        source_event_id="merge-001",
        evidence={"reason": "wrong merge order"},
        auto_rebuild_projection=False,
    )

    assert result["previous_snapshot_id"] == base["snapshot_id"]
    active_events = store.list_graph_ref_events(conn, PID, ref_name="active")
    branch_events = store.list_graph_ref_events(conn, PID, ref_name="refs/heads/codex/feature")
    active_by_op = {event["operation_type"]: event for event in active_events}
    assert set(active_by_op) == {"activate", "rollback"}
    rollback_event = active_by_op["rollback"]
    assert rollback_event["rollback_epoch"] == "rollback-001"
    assert rollback_event["source_event_id"] == "merge-001"
    assert rollback_event["evidence"]["reason"] == "wrong merge order"
    assert branch_events[0]["operation_type"] == "merge"
    assert branch_events[0]["branch_ref"] == "refs/heads/codex/feature"
    assert branch_events[0]["merge_epoch"] == "merge-001"


def test_branch_candidate_snapshot_cannot_be_promoted_to_active_target(conn):
    _ensure_schema(conn)
    active = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-target-active",
        commit_sha="target",
        snapshot_kind="full",
    )
    branch = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-branch-one-hop-candidate",
        commit_sha="branch-head",
        snapshot_kind="scope",
        ref_name="refs/heads/codex/feature",
        branch_ref="refs/heads/codex/feature",
    )
    store.activate_graph_snapshot(conn, PID, active["snapshot_id"], auto_rebuild_projection=False)

    with pytest.raises(ValueError, match="branch graph candidate cannot be activated"):
        store.activate_graph_snapshot(
            conn,
            PID,
            branch["snapshot_id"],
            auto_rebuild_projection=False,
        )

    current = store.get_active_graph_snapshot(conn, PID)
    assert current["snapshot_id"] == active["snapshot_id"]
    stored_branch = store.get_graph_snapshot(conn, PID, branch["snapshot_id"])
    assert stored_branch["status"] == store.SNAPSHOT_STATUS_CANDIDATE
    active_events = store.list_graph_ref_events(conn, PID, ref_name="active")
    assert active_events[-1]["new_snapshot_id"] == active["snapshot_id"]


def test_activate_snapshot_rejects_invalid_ref_operation_without_moving_active(conn):
    _ensure_schema(conn)
    active = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-valid-active",
        commit_sha="valid",
        snapshot_kind="full",
    )
    candidate = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-invalid-op",
        commit_sha="candidate",
        snapshot_kind="scope",
    )
    store.activate_graph_snapshot(conn, PID, active["snapshot_id"], auto_rebuild_projection=False)

    with pytest.raises(ValueError, match="invalid graph ref operation_type"):
        store.activate_graph_snapshot(
            conn,
            PID,
            candidate["snapshot_id"],
            operation_type="unsafe_direct_write",
            auto_rebuild_projection=False,
        )

    current = store.get_active_graph_snapshot(conn, PID)
    assert current["snapshot_id"] == active["snapshot_id"]
    assert store.list_graph_ref_events(conn, PID, operation_type="unsafe_direct_write") == []


def test_drift_ledger_allows_multiple_target_symbols(conn):
    _ensure_schema(conn)
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-c333333-drift",
        commit_sha="c333333",
        snapshot_kind="full",
    )

    store.record_drift(
        conn,
        PID,
        snapshot_id=snapshot["snapshot_id"],
        commit_sha="c333333",
        path="agent/service.py",
        drift_type="missing_test",
        target_symbol="agent.service.create",
        evidence={"reason": "no direct test"},
    )
    store.record_drift(
        conn,
        PID,
        snapshot_id=snapshot["snapshot_id"],
        commit_sha="c333333",
        path="agent/service.py",
        drift_type="missing_test",
        target_symbol="agent.service.delete",
        evidence={"reason": "no direct test"},
    )

    rows = conn.execute(
        """
        SELECT target_symbol FROM graph_drift_ledger
        WHERE project_id=? AND snapshot_id=? AND path=? AND drift_type=?
        ORDER BY target_symbol
        """,
        (PID, snapshot["snapshot_id"], "agent/service.py", "missing_test"),
    ).fetchall()
    assert [row["target_symbol"] for row in rows] == [
        "agent.service.create",
        "agent.service.delete",
    ]
    listed = store.list_graph_drift(
        conn,
        PID,
        snapshot_id=snapshot["snapshot_id"],
        drift_type="missing_test",
    )
    assert len(listed) == 2
    assert {row["target_symbol"] for row in listed} == {
        "agent.service.create",
        "agent.service.delete",
    }
    assert all(row["evidence"]["reason"] == "no direct test" for row in listed)


def test_graph_payload_edges_include_hierarchy_and_dependency_sections():
    graph = {
        "hierarchy_graph": {
            "nodes": [{"id": "L1.1"}, {"id": "L2.1"}],
            "links": [{"source": "L1.1", "target": "L2.1", "type": "contains"}],
        },
        "deps_graph": {
            "nodes": [{"id": "L1.1"}, {"id": "L2.1"}],
            "links": [{"source": "L2.1", "target": "L1.1", "type": "depends_on"}],
        },
    }

    edges = store.graph_payload_edges(graph)

    assert store.graph_payload_stats(graph) == {"nodes": 2, "edges": 2}
    assert {
        (edge["src"], edge["dst"], edge["edge_type"], edge["direction"])
        for edge in edges
    } == {
        ("L1.1", "L2.1", "contains", "hierarchy"),
        ("L2.1", "L1.1", "depends_on", "dependency"),
    }


def test_pending_scope_reconcile_queue_is_idempotent(conn):
    _ensure_schema(conn)
    first = store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="d444444",
        parent_commit_sha="c333333",
        evidence={"source": "dispatch-hook"},
    )
    second = store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="d444444",
        parent_commit_sha="ignored-parent",
        evidence={"source": "retry"},
    )

    assert first["commit_sha"] == second["commit_sha"]
    assert second["parent_commit_sha"] == "c333333"
    assert second["status"] == store.PENDING_STATUS_QUEUED

    count = conn.execute(
        "SELECT COUNT(*) AS count FROM pending_scope_reconcile WHERE project_id=? AND commit_sha=?",
        (PID, "d444444"),
    ).fetchone()["count"]
    assert count == 1


def test_reconcile_run_metrics_record_and_summarize(conn):
    _ensure_schema(conn)
    store.record_reconcile_run_metric(
        conn,
        PID,
        run_id="scope-fast",
        snapshot_id="scope-fast",
        commit_sha="fast",
        snapshot_kind="scope",
        strategy="incremental_graph_delta",
        graph_delta_mode="test_fanin_hash_only",
        status="ok",
        changed_file_count=2,
        event_count=12,
        elapsed_ms=4700,
    )
    store.record_reconcile_run_metric(
        conn,
        PID,
        run_id="scope-full",
        snapshot_id="scope-full",
        commit_sha="full",
        snapshot_kind="scope",
        strategy="full_rebuild_fallback",
        graph_delta_mode="full_rebuild",
        status="ok",
        changed_file_count=3,
        event_count=13,
        elapsed_ms=36000,
    )

    rows = store.list_reconcile_run_metrics(conn, PID)
    assert {row["run_id"] for row in rows} == {"scope-fast", "scope-full"}
    summary = store.summarize_reconcile_run_metrics(conn, PID)
    assert summary["by_strategy"]["incremental_graph_delta"]["avg_elapsed_ms"] == 4700
    assert summary["by_strategy"]["full_rebuild_fallback"]["avg_elapsed_ms"] == 36000
    assert summary["speedup"]["speedup_x"] == pytest.approx(7.66, rel=0.01)
    assert summary["speedup"]["elapsed_reduction_pct"] == pytest.approx(86.9, rel=0.01)


def test_reconcile_run_metrics_backfills_from_snapshot_notes(conn, tmp_path):
    _ensure_schema(conn)
    trace_dir = tmp_path / "trace"
    trace_dir.mkdir()
    trace_summary = trace_dir / "summary.json"
    trace_summary.write_text(
        json.dumps({"status": "ok", "elapsed_ms": 1234}),
        encoding="utf-8",
    )
    notes = {
        "run_id": "scope-reconcile-head",
        "scope_reconcile_strategy": "incremental_graph_delta",
        "scope_graph_delta_mode": "metadata_only",
        "scope_file_delta": {"changed_file_count": 1, "impacted_file_count": 1},
        "pending_scope_reconcile": {
            "active_graph_commit": "base",
            "scope_graph_events": {"event_count": 2},
        },
        "trace": {"summary_path": str(trace_summary)},
    }
    store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-head",
        commit_sha="head",
        snapshot_kind="scope",
        graph_json={"deps_graph": {"nodes": [{"id": "L7.1"}], "edges": []}},
        notes=json.dumps(notes),
    )

    result = store.backfill_reconcile_run_metrics_from_snapshots(conn, PID)
    assert result["imported"] == 1
    row = store.list_reconcile_run_metrics(conn, PID)[0]
    assert row["run_id"] == "scope-reconcile-head"
    assert row["elapsed_ms"] == 1234
    assert row["event_count"] == 2


def test_mark_pending_scope_failed_preserves_recovery_evidence(conn):
    _ensure_schema(conn)
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="head",
        parent_commit_sha="base",
        status=store.PENDING_STATUS_RUNNING,
        evidence={"source": "direct_update_graph"},
    )

    result = store.mark_pending_scope_reconcile_failed(
        conn,
        PID,
        commit_sha="head",
        actor="test",
        reason="client disconnected",
    )

    assert result["updated_count"] == 1
    row = store.list_pending_scope_reconcile(conn, PID, commit_shas=["head"])[0]
    assert row["status"] == store.PENDING_STATUS_FAILED
    evidence = json.loads(row["evidence_json"])
    assert evidence["recoverable"] is True
    assert evidence["recovery_action"] == "force_requeue_pending_scope"


def test_recover_stale_pending_scope_marks_old_running_failed(conn):
    _ensure_schema(conn)
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

    result = store.recover_stale_pending_scope_reconcile(
        conn,
        PID,
        max_running_seconds=1,
        actor="test",
    )

    assert result["recovered_count"] == 1
    row = store.list_pending_scope_reconcile(conn, PID, commit_shas=["old-running"])[0]
    assert row["status"] == store.PENDING_STATUS_FAILED
    evidence = json.loads(row["evidence_json"])
    assert evidence["source"] == "pending_scope_stale_running_recovery"
    assert evidence["recoverable"] is True


def test_waive_pending_scope_reconcile_preserves_materialized_rows(conn):
    _ensure_schema(conn)
    for commit, status in [
        ("queued", store.PENDING_STATUS_QUEUED),
        ("running", store.PENDING_STATUS_RUNNING),
        ("failed", store.PENDING_STATUS_FAILED),
        ("done", store.PENDING_STATUS_MATERIALIZED),
    ]:
        store.queue_pending_scope_reconcile(
            conn,
            PID,
            commit_sha=commit,
            parent_commit_sha="old",
            status=status,
            evidence={"source": "test"},
        )

    result = store.waive_pending_scope_reconcile(
        conn,
        PID,
        snapshot_id="full-head",
        actor="test",
        reason="scope materializer bug",
    )

    assert result["waived_count"] == 3
    rows = conn.execute(
        """
        SELECT commit_sha, status, snapshot_id, evidence_json
        FROM pending_scope_reconcile
        WHERE project_id=? ORDER BY commit_sha
        """,
        (PID,),
    ).fetchall()
    statuses = {row["commit_sha"]: row["status"] for row in rows}
    assert statuses == {
        "done": store.PENDING_STATUS_MATERIALIZED,
        "failed": store.PENDING_STATUS_WAIVED,
        "queued": store.PENDING_STATUS_WAIVED,
        "running": store.PENDING_STATUS_WAIVED,
    }
    waived = next(row for row in rows if row["commit_sha"] == "queued")
    assert waived["snapshot_id"] == "full-head"
    assert json.loads(waived["evidence_json"])["reason"] == "scope materializer bug"


def test_finalize_graph_snapshot_activates_and_materializes_matching_pending(conn):
    _ensure_schema(conn)
    old = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="imported-old-finalize",
        commit_sha="old",
        snapshot_kind="imported",
    )
    new = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-new-finalize",
        commit_sha="new",
        snapshot_kind="full",
    )
    store.activate_graph_snapshot(conn, PID, old["snapshot_id"])
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="new",
        parent_commit_sha="old",
        evidence={"source": "test"},
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="other",
        parent_commit_sha="old",
        evidence={"source": "test"},
    )

    result = store.finalize_graph_snapshot(
        conn,
        PID,
        new["snapshot_id"],
        target_commit_sha="new",
        expected_old_snapshot_id=old["snapshot_id"],
        actor="test",
        evidence={"signoff": "unit-test"},
    )

    assert result["pending_materialized_count"] == 1
    assert result["activation"]["previous_snapshot_id"] == old["snapshot_id"]
    active = store.get_active_graph_snapshot(conn, PID)
    assert active["snapshot_id"] == new["snapshot_id"]
    pending = conn.execute(
        "SELECT status, snapshot_id, evidence_json FROM pending_scope_reconcile WHERE project_id=? AND commit_sha=?",
        (PID, "new"),
    ).fetchone()
    assert pending["status"] == store.PENDING_STATUS_MATERIALIZED
    assert pending["snapshot_id"] == new["snapshot_id"]
    assert json.loads(pending["evidence_json"])["signoff"] == "unit-test"
    other = conn.execute(
        "SELECT status FROM pending_scope_reconcile WHERE project_id=? AND commit_sha=?",
        (PID, "other"),
    ).fetchone()
    assert other["status"] == store.PENDING_STATUS_QUEUED


def test_finalize_graph_snapshot_materializes_explicit_covered_commits(conn):
    _ensure_schema(conn)
    old = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="imported-old-covered",
        commit_sha="old",
        snapshot_kind="imported",
    )
    new = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="scope-new-covered",
        commit_sha="new",
        snapshot_kind="scope",
    )
    store.activate_graph_snapshot(conn, PID, old["snapshot_id"])
    for commit in ("a1", "a2", "new", "future"):
        store.queue_pending_scope_reconcile(
            conn,
            PID,
            commit_sha=commit,
            parent_commit_sha="old",
            evidence={"source": "test"},
        )

    result = store.finalize_graph_snapshot(
        conn,
        PID,
        new["snapshot_id"],
        target_commit_sha="new",
        expected_old_snapshot_id=old["snapshot_id"],
        covered_commit_shas=["a1", "a2", "new"],
    )

    assert result["pending_materialized_count"] == 3
    rows = conn.execute(
        """
        SELECT commit_sha, status, snapshot_id FROM pending_scope_reconcile
        WHERE project_id=? ORDER BY commit_sha
        """,
        (PID,),
    ).fetchall()
    statuses = {row["commit_sha"]: row["status"] for row in rows}
    assert statuses == {
        "a1": store.PENDING_STATUS_MATERIALIZED,
        "a2": store.PENDING_STATUS_MATERIALIZED,
        "future": store.PENDING_STATUS_QUEUED,
        "new": store.PENDING_STATUS_MATERIALIZED,
    }
    assert {
        row["snapshot_id"] for row in rows
        if row["status"] == store.PENDING_STATUS_MATERIALIZED
    } == {new["snapshot_id"]}


def test_finalize_graph_snapshot_rejects_commit_mismatch_and_stale_active(conn):
    _ensure_schema(conn)
    old = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="imported-old-stale",
        commit_sha="old",
        snapshot_kind="imported",
    )
    new = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-new-stale",
        commit_sha="new",
        snapshot_kind="full",
    )
    store.activate_graph_snapshot(conn, PID, old["snapshot_id"])

    with pytest.raises(ValueError):
        store.finalize_graph_snapshot(
            conn,
            PID,
            new["snapshot_id"],
            target_commit_sha="different",
        )

    with pytest.raises(store.GraphSnapshotConflictError):
        store.finalize_graph_snapshot(
            conn,
            PID,
            new["snapshot_id"],
            target_commit_sha="new",
            expected_old_snapshot_id="not-active",
        )

    active = store.get_active_graph_snapshot(conn, PID)
    assert active["snapshot_id"] == old["snapshot_id"]


def _small_graph(node_id="L7.1"):
    return {
        "version": 1,
        "deps_graph": {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": [
                {
                    "id": node_id,
                    "layer": "L7",
                    "title": "Imported Node",
                    "primary": ["agent/governance/imported.py"],
                    "metadata": {"kind": "imported"},
                }
            ],
            "edges": [
                {
                    "source": node_id,
                    "target": "L7.2",
                    "edge_type": "depends_on",
                    "direction": "dependency",
                }
            ],
        },
    }


def test_import_existing_graph_skips_empty_baseline_and_uses_shared_current(conn, tmp_path):
    _ensure_schema(conn)
    create_baseline(
        conn,
        PID,
        chain_version="scan-only",
        trigger="reconcile-task",
        triggered_by="auto-chain",
        graph_json={},
    )
    conn.execute(
        """
        INSERT INTO project_version(project_id, chain_version, updated_at, updated_by, git_head)
        VALUES (?, ?, ?, ?, ?)
        """,
        (PID, "governed-commit", "2026-05-07T00:00:00Z", "test", "newer-mf-head"),
    )
    graph_path = tmp_path / PID / "graph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph_path.write_text(json.dumps(_small_graph("L7.imported")), encoding="utf-8")

    result = store.import_existing_graph_snapshot(
        conn,
        PID,
        snapshot_id="imported-governed-test",
        activate=True,
        created_by="test",
    )

    assert result["source"]["source_kind"] == "shared_volume_current"
    assert result["commit_sha"] == "governed-commit"
    assert result["index_counts"] == {"nodes": 1, "edges": 1}
    assert result["activation"]["snapshot_id"] == "imported-governed-test"

    active = store.get_active_graph_snapshot(conn, PID)
    assert active["snapshot_id"] == "imported-governed-test"
    assert active["commit_sha"] == "governed-commit"

    node = conn.execute(
        "SELECT node_id FROM graph_nodes_index WHERE project_id=? AND snapshot_id=?",
        (PID, "imported-governed-test"),
    ).fetchone()
    assert node["node_id"] == "L7.imported"


def test_import_existing_graph_prefers_non_empty_baseline_companion(conn):
    _ensure_schema(conn)
    create_baseline(
        conn,
        PID,
        chain_version="baseline-commit",
        trigger="reconcile-task",
        triggered_by="auto-chain",
        graph_json=_small_graph("L7.baseline"),
    )

    source = store.select_existing_graph_source(conn, PID)
    assert source["source_kind"] == "baseline_companion"
    assert source["source_ref"] == "1"
    assert source["stats"] == {"nodes": 1, "edges": 1}


def test_strict_graph_ready_ignores_scan_baseline_when_active_graph_is_stale(conn):
    _ensure_schema(conn)
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="imported-active-old",
        commit_sha="old-graph",
        snapshot_kind="imported",
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    create_baseline(
        conn,
        PID,
        chain_version="new-scan",
        trigger="reconcile-task",
        triggered_by="auto-chain",
        scope_kind="commit_sweep",
        scope_value="old-graph..new-scan",
    )
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="new-scan",
        parent_commit_sha="old-graph",
        evidence={"source": "test"},
    )

    status = store.graph_governance_status(conn, PID)
    assert status["materialized_graph_baseline_commit"] == "old-graph"
    assert status["scan_baseline_commit"] == "new-scan"
    assert status["pending_scope_reconcile_count"] == 1

    readiness = store.strict_graph_ready(conn, PID, target_commit="new-scan")
    assert readiness["ok"] is False
    assert readiness["reason"] == "graph_snapshot_commit_mismatch"
    assert readiness["scan_baseline_commit"] == "new-scan"

    ready = store.strict_graph_ready(conn, PID, target_commit="old-graph")
    assert ready["ok"] is True
    assert ready["reason"] == ""


def test_graph_status_surfaces_snapshot_materialization_warnings(conn):
    _ensure_schema(conn)
    notes = {
        "checkout_provenance": {
            "execution_root": "/private/tmp/aming-claw-scope/repo",
            "execution_root_role": "execution_root",
            "execution_root_is_ephemeral": True,
            "canonical_project_identity": {
                "type": "git",
                "project_id": PID,
                "identity_hash": "abc123",
            },
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
        snapshot_id="scope-suspect-root",
        commit_sha="head",
        snapshot_kind="scope",
        notes=json.dumps(notes, sort_keys=True),
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])

    status = store.graph_governance_status(conn, PID)

    assert status["active_snapshot_materialization"]["execution_root_role"] == "execution_root"
    assert status["active_snapshot_materialization"]["warning_count"] == 1
    assert status["active_snapshot_warnings"][0]["code"] == "ephemeral_execution_root"


def test_pending_scope_force_requeue_reopens_materialized_rows(conn):
    _ensure_schema(conn)
    first = store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="head",
        parent_commit_sha="old",
        status=store.PENDING_STATUS_MATERIALIZED,
        snapshot_id="scope-old",
        evidence={"source": "test"},
    )
    assert first["status"] == store.PENDING_STATUS_MATERIALIZED

    preserved = store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="head",
        status=store.PENDING_STATUS_QUEUED,
        evidence={"source": "normal_requeue"},
    )
    assert preserved["status"] == store.PENDING_STATUS_MATERIALIZED
    assert preserved["snapshot_id"] == "scope-old"

    reopened = store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha="head",
        status=store.PENDING_STATUS_QUEUED,
        evidence={"source": "suspect_snapshot_requeue"},
        force_requeue=True,
    )

    assert reopened["status"] == store.PENDING_STATUS_QUEUED
    assert reopened["snapshot_id"] == ""
    assert reopened["retry_count"] == 1
    evidence = json.loads(reopened["evidence_json"])
    assert evidence["source"] == "suspect_snapshot_requeue"
    assert evidence["force_requeue"] is True
