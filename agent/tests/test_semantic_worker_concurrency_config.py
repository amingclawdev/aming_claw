from __future__ import annotations

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

    def fake_claim_semantic_jobs(*args, **kwargs):
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
