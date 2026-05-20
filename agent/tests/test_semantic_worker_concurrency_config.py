from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
import sqlite3

from agent.governance import reconcile_semantic_enrichment as semantic
from agent.governance import graph_events
from agent.governance import semantic_worker


class _FakeConn:
    def __init__(self) -> None:
        self.committed = False
        self.closed = False

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True


class _NoCloseConn:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self._conn.executescript(*args, **kwargs)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        pass


def test_semantic_worker_executor_recreates_when_configured_concurrency_changes():
    semantic_worker._reset_worker_runtime_for_tests()
    try:
        first = semantic_worker._get_executor(2)
        assert semantic_worker._executor_max_workers == 2

        same = semantic_worker._get_executor(2)
        assert same is first

        second = semantic_worker._get_executor(3)
        assert second is not first
        assert semantic_worker._executor_max_workers == 3
    finally:
        semantic_worker._reset_worker_runtime_for_tests()


def test_semantic_worker_event_listener_uses_configured_executor(monkeypatch):
    semantic_worker._reset_worker_runtime_for_tests()
    monkeypatch.setattr(
        semantic_worker,
        "_worker_runtime_config",
        lambda project_id="": {
            "max_workers": 6,
            "claim_batch_size": 10,
            "lease_seconds": 600,
        },
    )
    monkeypatch.setattr(semantic_worker, "_drain_node", lambda project_id, snapshot_id: None)
    try:
        semantic_worker.on_semantic_job_enqueued(
            {"project_id": "demo", "snapshot_id": "scope-demo"}
        )

        assert semantic_worker._executor_max_workers == 6
    finally:
        semantic_worker._reset_worker_runtime_for_tests()


def test_semantic_worker_node_drain_uses_configured_claim_policy(monkeypatch):
    fake_conn = _FakeConn()
    captured: dict[str, int] = {}
    semantic_worker._reset_worker_runtime_for_tests()
    monkeypatch.setattr(
        semantic_worker,
        "_worker_runtime_config",
        lambda project_id="": {
            "max_workers": 10,
            "claim_batch_size": 10,
            "lease_seconds": 321,
        },
    )
    monkeypatch.setattr(
        "agent.governance.db.get_connection",
        lambda project_id: fake_conn,
    )

    def fake_claim_semantic_jobs(
        conn,
        project_id,
        snapshot_id,
        *,
        worker_id,
        statuses,
        limit,
        lease_seconds,
        actor,
    ):
        captured["limit"] = limit
        captured["lease_seconds"] = lease_seconds
        return {"claim_id": "claim-demo", "claimed_count": 0, "jobs": []}

    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_enrichment.claim_semantic_jobs",
        fake_claim_semantic_jobs,
    )

    semantic_worker._drain_node("demo", "scope-demo")

    assert captured == {"limit": 10, "lease_seconds": 321}
    assert fake_conn.closed is True


def test_semantic_worker_node_drain_processes_claimed_nodes_in_parallel(monkeypatch):
    fake_conn = _FakeConn()
    active = 0
    max_active = 0
    active_guard = threading.Lock()
    start_barrier = threading.Barrier(3)
    semantic_worker._reset_worker_runtime_for_tests()
    monkeypatch.setattr(
        semantic_worker,
        "_worker_runtime_config",
        lambda project_id="": {
            "max_workers": 3,
            "claim_batch_size": 3,
            "lease_seconds": 600,
        },
    )
    monkeypatch.setattr(semantic_worker, "_project_root_for", lambda project_id: ".")
    monkeypatch.setattr(
        "agent.governance.db.get_connection",
        lambda project_id: fake_conn,
    )
    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_config.load_semantic_enrichment_config",
        lambda project_root=None: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_config.apply_project_ai_routing",
        lambda config, project_id=None: config,
    )
    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_ai.build_semantic_ai_call",
        lambda **kwargs: (lambda stage, payload: {}),
    )

    claim_calls = 0

    def fake_claim_semantic_jobs(*args, **kwargs):
        nonlocal claim_calls
        claim_calls += 1
        if claim_calls > 1:
            return {"claim_id": "claim-empty", "claimed_count": 0, "jobs": []}
        return {
            "claim_id": "claim-demo",
            "claimed_count": 3,
            "jobs": [
                {"node_id": "L7.1"},
                {"node_id": "L7.2"},
                {"node_id": "L7.3"},
            ],
        }

    def fake_process_node_job(project_id, snapshot_id, *, root, ai_call, node_id):
        nonlocal active, max_active
        with active_guard:
            active += 1
            max_active = max(max_active, active)
        start_barrier.wait(timeout=2)
        time.sleep(0.01)
        with active_guard:
            active -= 1
        return {"ok": True, "node_id": node_id}

    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_enrichment.claim_semantic_jobs",
        fake_claim_semantic_jobs,
    )
    monkeypatch.setattr(
        semantic_worker,
        "_process_node_semantic_job",
        fake_process_node_job,
    )
    monkeypatch.setattr(
        semantic_worker,
        "_finalize_completed_node_jobs_from_events",
        lambda project_id, snapshot_id, *, node_ids: len(node_ids),
    )

    semantic_worker._drain_node("demo", "scope-demo")

    assert max_active == 3


def test_process_node_semantic_job_scopes_persist_and_trace_dir(monkeypatch, tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    semantic._ensure_semantic_state_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_semantic_jobs
          (project_id, snapshot_id, node_id, status, updated_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("demo", "scope-demo", "L7.1", "running", "2026-05-19T00:00:00Z", "2026-05-19T00:00:00Z"),
    )
    conn.commit()
    captured: dict = {}

    monkeypatch.setattr(
        "agent.governance.db.get_connection",
        lambda project_id: _NoCloseConn(conn),
    )

    def fake_run_semantic_enrichment(*args, **kwargs):
        captured.update(kwargs)
        semantic_payload = {
            "node_id": "L7.1",
            "feature_name": "Scoped Feature",
            "semantic_summary": "Generated semantic proposal.",
            "intent": "Exercise node worker scoping.",
            "self_check": {
                "valid": True,
                "status": "passed",
                "checked_rules": semantic.NODE_SEMANTIC_SELF_CHECK_RULES,
            },
        }
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
        return {"summary": {"ai_complete_count": 1}}

    monkeypatch.setattr(semantic, "run_semantic_enrichment", fake_run_semantic_enrichment)
    monkeypatch.setattr(graph_events, "backfill_existing_semantic_events", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "agent.governance.reconcile_feedback.submit_feedback_item",
        lambda *args, **kwargs: None,
    )

    result = semantic_worker._process_node_semantic_job(
        "demo",
        "scope-demo",
        root=tmp_path,
        ai_call=lambda stage, payload: {},
        node_id="L7.1",
    )

    assert result["ok"] is True
    assert captured["semantic_node_ids"] == ["L7.1"]
    assert captured["semantic_persist_node_ids"] == ["L7.1"]
    assert "worker-runs" in str(captured["trace_dir"])
    assert "L7.1" in str(captured["trace_dir"])


def test_semantic_worker_node_drain_retries_empty_claim_with_pending_jobs(monkeypatch):
    fake_conn = _FakeConn()
    processed: list[str] = []
    published: list[tuple[str, dict]] = []
    claim_calls = 0
    semantic_worker._reset_worker_runtime_for_tests()
    monkeypatch.setattr(
        semantic_worker,
        "_worker_runtime_config",
        lambda project_id="": {
            "max_workers": 1,
            "claim_batch_size": 2,
            "lease_seconds": 600,
        },
    )
    monkeypatch.setattr(semantic_worker, "_project_root_for", lambda project_id: ".")
    monkeypatch.setattr(
        "agent.governance.db.get_connection",
        lambda project_id: fake_conn,
    )
    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_config.load_semantic_enrichment_config",
        lambda project_root=None: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_config.apply_project_ai_routing",
        lambda config, project_id=None: config,
    )
    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_ai.build_semantic_ai_call",
        lambda **kwargs: (lambda stage, payload: {}),
    )

    def fake_claim_semantic_jobs(*args, **kwargs):
        nonlocal claim_calls
        claim_calls += 1
        if claim_calls == 1:
            return {
                "claim_id": "claim-first",
                "claimed_count": 2,
                "jobs": [{"node_id": "L7.1"}, {"node_id": "L7.2"}],
            }
        if claim_calls == 2:
            return {"claim_id": "claim-gap", "claimed_count": 0, "jobs": []}
        if claim_calls == 3:
            return {
                "claim_id": "claim-retry",
                "claimed_count": 1,
                "jobs": [{"node_id": "L7.3"}],
            }
        return {"claim_id": "claim-empty", "claimed_count": 0, "jobs": []}

    def fake_pending_count(conn, project_id, snapshot_id, *, worker_id):
        return 1 if claim_calls == 2 else 0

    def fake_process_node_job(project_id, snapshot_id, *, root, ai_call, node_id):
        processed.append(node_id)
        return {"ok": True, "node_id": node_id}

    def fake_publish(topic, payload):
        published.append((topic, payload))

    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_enrichment.claim_semantic_jobs",
        fake_claim_semantic_jobs,
    )
    monkeypatch.setattr(
        semantic_worker,
        "_count_claimable_pending_node_jobs",
        fake_pending_count,
    )
    monkeypatch.setattr(
        semantic_worker,
        "_process_node_semantic_job",
        fake_process_node_job,
    )
    monkeypatch.setattr(
        semantic_worker,
        "_finalize_completed_node_jobs_from_events",
        lambda project_id, snapshot_id, *, node_ids: len(node_ids),
    )
    monkeypatch.setattr("agent.governance.event_bus.publish", fake_publish)

    semantic_worker._drain_node("demo", "scope-demo")

    assert processed == ["L7.1", "L7.2", "L7.3"]
    assert claim_calls == 4
    assert any(topic == "semantic_worker.drain_gap" for topic, _payload in published)
    gap_payload = next(
        payload for topic, payload in published if topic == "semantic_worker.drain_gap"
    )
    assert gap_payload["pending_count"] == 1
    assert gap_payload["claim_id"] == "claim-gap"


def test_semantic_worker_records_drain_gap_event():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    semantic_worker._record_node_drain_gap(
        conn,
        "demo",
        "scope-demo",
        pending_count=1,
        claim_id="claim-gap",
        batch_count=1,
        retry_count=1,
    )

    row = conn.execute(
        """
        SELECT event_type, event_kind, target_type, target_id, status,
               operation_type, payload_json, evidence_json, created_by
        FROM graph_events
        """
    ).fetchone()
    assert row is not None
    payload = json.loads(row["payload_json"])
    evidence = json.loads(row["evidence_json"])
    assert row["event_type"] == "semantic_job_requested"
    assert row["event_kind"] == "semantic_job"
    assert row["target_type"] == "snapshot"
    assert row["target_id"] == "scope-demo"
    assert row["status"] == graph_events.EVENT_STATUS_OBSERVED
    assert row["operation_type"] == "node_semantic_drain_gap"
    assert row["created_by"] == "semantic_worker_inproc"
    assert payload["pending_count"] == 1
    assert payload["claim_id"] == "claim-gap"
    assert evidence["source"] == "semantic_worker_inproc"


def test_claim_semantic_jobs_does_not_increment_unclaimed_attempts():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    semantic._ensure_semantic_state_schema(conn)
    for node_id in ["L7.1", "L7.2"]:
        conn.execute(
            """
            INSERT INTO graph_semantic_jobs
              (project_id, snapshot_id, node_id, status, updated_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "demo",
                "scope-demo",
                node_id,
                "pending_ai",
                "2026-05-18T00:00:00Z",
                "2026-05-18T00:00:00Z",
            ),
        )

    claim = semantic.claim_semantic_jobs(
        conn,
        "demo",
        "scope-demo",
        worker_id="worker-1",
        statuses=["pending_ai"],
        limit=1,
        lease_seconds=600,
        actor="test",
    )

    assert claim["claimed_count"] == 1
    rows = {
        row["node_id"]: row["attempt_count"]
        for row in conn.execute(
            """
            SELECT node_id, attempt_count
            FROM graph_semantic_jobs
            ORDER BY node_id
            """
        )
    }
    assert rows == {"L7.1": 1, "L7.2": 0}


def test_semantic_worker_finalize_node_job_clears_claim_state():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    semantic._ensure_semantic_state_schema(conn)
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
            "2026-05-18T00:00:00Z",
            "2026-05-18T00:10:00Z",
            "semantic_worker_inproc",
            "",
            "2026-05-18T00:00:00Z",
            "2026-05-18T00:00:00Z",
        ),
    )

    semantic_worker._finalize_node_semantic_job(
        conn,
        "demo",
        "scope-demo",
        "L7.1",
        status="ai_complete",
    )

    row = conn.execute(
        "SELECT status, worker_id, claim_id, claimed_at, lease_expires_at, claimed_by, last_error FROM graph_semantic_jobs"
    ).fetchone()
    assert dict(row) == {
        "status": "ai_complete",
        "worker_id": "",
        "claim_id": "",
        "claimed_at": "",
        "lease_expires_at": "",
        "claimed_by": "",
        "last_error": "",
    }


def test_semantic_worker_process_node_requires_target_ai_complete(monkeypatch, tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    semantic._ensure_semantic_state_schema(conn)
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
            "2026-05-19T10:00:00Z",
            "2026-05-19T10:10:00Z",
            "semantic_worker_inproc",
            "",
            "2026-05-19T10:00:00Z",
            "2026-05-19T10:00:00Z",
        ),
    )
    monkeypatch.setattr("agent.governance.db.get_connection", lambda project_id: _NoCloseConn(conn))
    monkeypatch.setattr(
        semantic,
        "run_semantic_enrichment",
        lambda *args, **kwargs: {
            "summary": {"ai_complete_count": 1},
            "semantic_index": {
                "features": [
                    {
                        "node_id": "L7.1",
                        "enrichment_status": "ai_unavailable",
                        "semantic_ai_error": "socket closed",
                    },
                    {"node_id": "L7.2", "enrichment_status": "ai_complete"},
                ]
            },
        },
    )

    result = semantic_worker._process_node_semantic_job(
        "demo",
        "scope-demo",
        root=tmp_path,
        ai_call=lambda *_args, **_kwargs: {},
        node_id="L7.1",
    )

    row = conn.execute(
        "SELECT status, last_error, worker_id, claim_id FROM graph_semantic_jobs WHERE node_id='L7.1'"
    ).fetchone()
    assert result["status"] == "ai_incomplete"
    assert row["status"] == "ai_failed"
    assert row["last_error"] == "socket closed"
    assert row["worker_id"] == ""
    assert row["claim_id"] == ""


def test_semantic_worker_batch_finalizer_uses_proposed_events(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    semantic._ensure_semantic_state_schema(conn)
    graph_events.ensure_schema(conn)
    for node_id in ["L7.1", "L7.2"]:
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
                node_id,
                "running",
                "semantic_worker_inproc",
                "claim-1",
                "2026-05-18T00:00:00Z",
                "2026-05-18T00:10:00Z",
                "semantic_worker_inproc",
                "",
                "2026-05-18T00:00:00Z",
                "2026-05-18T00:00:00Z",
            ),
        )
    graph_events.create_event(
        conn,
        "demo",
        "scope-demo",
        event_type="semantic_node_enriched",
        event_kind="semantic_job",
        target_type="node",
        target_id="L7.1",
        status="proposed",
        created_by="test",
    )
    monkeypatch.setattr("agent.governance.db.get_connection", lambda project_id: _NoCloseConn(conn))

    count = semantic_worker._finalize_completed_node_jobs_from_events(
        "demo",
        "scope-demo",
        node_ids=["L7.1", "L7.2"],
    )

    rows = {
        row["node_id"]: row["status"]
        for row in conn.execute("SELECT node_id, status FROM graph_semantic_jobs")
    }
    assert count == 1
    assert rows == {"L7.1": "ai_complete", "L7.2": "running"}


def test_semantic_worker_batch_finalizer_ignores_stale_proposed_events(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    semantic._ensure_semantic_state_schema(conn)
    graph_events.ensure_schema(conn)
    for node_id in ["L7.1", "L7.2"]:
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
                node_id,
                "running",
                "semantic_worker_inproc",
                "claim-1",
                "2026-05-19T10:00:00Z",
                "2026-05-19T10:10:00Z",
                "semantic_worker_inproc",
                "",
                "2026-05-19T10:00:00Z",
                "2026-05-19T10:00:00Z",
            ),
        )
    stale = graph_events.create_event(
        conn,
        "demo",
        "scope-demo",
        event_type="semantic_node_enriched",
        event_kind="semantic_job",
        target_type="node",
        target_id="L7.1",
        status="proposed",
        created_by="test",
    )
    fresh = graph_events.create_event(
        conn,
        "demo",
        "scope-demo",
        event_type="semantic_node_enriched",
        event_kind="semantic_job",
        target_type="node",
        target_id="L7.2",
        status="proposed",
        created_by="test",
    )
    conn.execute(
        "UPDATE graph_events SET created_at=?, updated_at=? WHERE event_id=?",
        ("2026-05-19T09:59:59Z", "2026-05-19T09:59:59Z", stale["event_id"]),
    )
    conn.execute(
        "UPDATE graph_events SET created_at=?, updated_at=? WHERE event_id=?",
        ("2026-05-19T10:00:01Z", "2026-05-19T10:00:01Z", fresh["event_id"]),
    )
    conn.commit()
    monkeypatch.setattr("agent.governance.db.get_connection", lambda project_id: _NoCloseConn(conn))

    count = semantic_worker._finalize_completed_node_jobs_from_events(
        "demo",
        "scope-demo",
        node_ids=["L7.1", "L7.2"],
    )

    rows = {
        row["node_id"]: row["status"]
        for row in conn.execute("SELECT node_id, status FROM graph_semantic_jobs")
    }
    assert count == 1
    assert rows == {"L7.1": "running", "L7.2": "ai_complete"}
