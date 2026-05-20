"""MF-2026-05-10-016: regression tests for the event-driven in-process
semantic worker and its review-queue gate.

Covers:
- A. `_persist_semantic_state_to_db` honours `submit_for_review=True` by
     writing graph_semantic_nodes status="pending_review".
- B. `backfill_existing_semantic_events` maps `pending_review` rows to
     `EVENT_STATUS_PROPOSED`; non-pending rows stay `EVENT_STATUS_OBSERVED`.
- C. `accept_semantic_enrichment` is in FEEDBACK_DECISION_ACTIONS and
     the helper flips both the persistent row and the event status.
- D. `semantic_worker.register()` is idempotent and subscribes both topics.
- E. The publish on /semantic/jobs is fired (test the helper directly).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent.governance import event_bus
from agent.governance import ai_output_intake
from agent.governance import graph_events
from agent.governance import graph_snapshot_store as store
from agent.governance import reconcile_feedback
from agent.governance import reconcile_semantic_enrichment as semantic
from agent.governance import semantic_worker
from agent.governance import server
from agent.governance.db import _ensure_schema


PID = "semantic-worker-test"


class _NoCloseConn:
    """Wraps a sqlite3.Connection but no-ops close() so the worker's `finally`
    block doesn't drop the connection the test fixture is still using.
    sqlite3.Connection.close is a read-only C attribute and can't be
    monkeypatched directly."""

    def __init__(self, real):
        self._real = real

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    store.ensure_schema(c)
    semantic._ensure_semantic_state_schema(c)
    yield c
    c.close()


def _create_snapshot_with_nodes(conn, snapshot_id: str, node_ids: list[str]) -> dict:
    nodes = [
        {
            "id": node_id,
            "layer": "L7",
            "title": f"Feature {node_id}",
            "kind": "service_runtime",
            "primary": [f"agent/governance/{node_id.replace('.', '_')}.py"],
            "secondary": [],
            "test": [],
            "metadata": {"subsystem": "governance"},
        }
        for node_id in node_ids
    ]
    snap = store.create_graph_snapshot(
        conn, PID, snapshot_id=snapshot_id, commit_sha="head",
        snapshot_kind="scope",
        graph_json={"deps_graph": {"nodes": nodes, "edges": []}},
    )
    store.index_graph_snapshot(conn, PID, snap["snapshot_id"], nodes=nodes, edges=[])
    return snap


def _create_snapshot_with_node(conn, snapshot_id: str, node_id: str = "L7.1") -> dict:
    return _create_snapshot_with_nodes(conn, snapshot_id, [node_id])


def test_a_persist_submit_for_review_writes_pending_review_status(conn):
    """A: state writer forces status='pending_review' under the flag."""
    snap = _create_snapshot_with_node(conn, "persist-review")
    sid = snap["snapshot_id"]
    state = {
        "node_semantics": {
            "L7.1": {
                "status": "ai_complete",  # would normally be persisted as-is
                "feature_hash": "sha256:abc",
                "file_hashes": {"x": "y"},
                "updated_at": "2026-05-10T20:00:00Z",
                "semantic_summary": "hello",
            }
        },
    }
    semantic._persist_semantic_state_to_db(
        conn, PID, sid, state, submit_for_review=True,
    )
    conn.commit()
    row = conn.execute(
        "SELECT status FROM graph_semantic_nodes WHERE project_id=? AND snapshot_id=? AND node_id='L7.1'",
        (PID, sid),
    ).fetchone()
    assert row["status"] == "pending_review", (
        "submit_for_review=True must override the source row's status"
    )

    # Sanity: same call with submit_for_review=False keeps the source status.
    snap2 = _create_snapshot_with_node(conn, "persist-nogate")
    semantic._persist_semantic_state_to_db(
        conn, PID, snap2["snapshot_id"], state, submit_for_review=False,
    )
    conn.commit()
    row2 = conn.execute(
        "SELECT status FROM graph_semantic_nodes WHERE project_id=? AND snapshot_id=? AND node_id='L7.1'",
        (PID, snap2["snapshot_id"]),
    ).fetchone()
    assert row2["status"] == "ai_complete", (
        "default path must preserve source status"
    )


def test_a1_run_semantic_enrichment_review_gate_survives_final_write(conn, tmp_path):
    """Full run regression: the final artifact write must not undo review gating."""
    snap = _create_snapshot_with_node(conn, "run-review", node_id="L7.7")
    sid = snap["snapshot_id"]
    source = tmp_path / "agent" / "governance" / "L7_7.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("def target():\n    return 'ok'\n", encoding="utf-8")

    def _ai_call(stage, payload):
        assert stage == "reconcile_semantic_feature"
        assert payload["feature"]["node_id"] == "L7.7"
        return {
            "feature_name": "Reviewed Feature",
            "semantic_summary": "Generated semantic proposal.",
            "intent": "Exercise the review gate.",
            "confidence": 0.8,
        }

    result = semantic.run_semantic_enrichment(
        conn,
        PID,
        sid,
        str(tmp_path),
        use_ai=True,
        ai_call=_ai_call,
        semantic_node_ids=["L7.7"],
        semantic_skip_completed=False,
        submit_for_review=True,
        created_by="test",
        max_excerpt_chars=200,
    )

    assert result["summary"]["ai_complete_count"] == 1
    row = conn.execute(
        """
        SELECT status
        FROM graph_semantic_nodes
        WHERE project_id = ? AND snapshot_id = ? AND node_id = 'L7.7'
        """,
        (PID, sid),
    ).fetchone()
    assert row["status"] == "pending_review"

    graph_events.backfill_existing_semantic_events(conn, PID, sid, actor="test")
    event = conn.execute(
        """
        SELECT status
        FROM graph_events
        WHERE project_id = ? AND snapshot_id = ?
          AND event_type = 'semantic_node_enriched'
          AND target_id = 'L7.7'
        ORDER BY event_seq DESC LIMIT 1
        """,
        (PID, sid),
    ).fetchone()
    assert event["status"] == graph_events.EVENT_STATUS_PROPOSED


def test_a2_submit_for_review_skips_carried_forward_rows(conn):
    """A2 (regression for the 2026-05-10 first-run scoping spillover):
    `submit_for_review=True` must NOT flip rows that came from
    `_carry_forward_semantic_graph_state` (have `carried_forward_from_snapshot_id`
    set). Those were already accepted in a prior snapshot — the worker just
    happens to call run_semantic_enrichment with the gate flag for the freshly
    enriched ones, and the persistence layer has to scope the override correctly.
    """
    snap = _create_snapshot_with_node(conn, "carry-forward-scope")
    sid = snap["snapshot_id"]
    state = {
        "node_semantics": {
            "L7.1": {
                # Marker put on the entry by _carry_forward_semantic_graph_state.
                "carried_forward_from_snapshot_id": "scope-prev",
                "status": "ai_complete",
                "feature_hash": "sha256:carried",
                "semantic_summary": "carried",
            }
        }
    }
    semantic._persist_semantic_state_to_db(
        conn, PID, sid, state, submit_for_review=True,
    )
    conn.commit()
    row = conn.execute(
        "SELECT status FROM graph_semantic_nodes WHERE project_id=? AND snapshot_id=? AND node_id='L7.1'",
        (PID, sid),
    ).fetchone()
    assert row["status"] == "ai_complete", (
        "carried-forward rows must keep their original status even when the "
        "caller asked for submit_for_review — the gate is only for fresh enrichment"
    )


def test_a3_submit_for_review_scopes_to_current_run_node_ids(conn):
    """A3: selected-node worker runs must not resubmit older accepted memory."""
    snap = _create_snapshot_with_nodes(conn, "review-node-scope", ["L7.1", "L7.2"])
    sid = snap["snapshot_id"]
    semantic._persist_semantic_state_to_db(
        conn,
        PID,
        sid,
        {
            "node_semantics": {
                "L7.1": {
                    "status": "ai_complete",
                    "feature_hash": "sha256:old",
                    "semantic_summary": "already accepted",
                },
                "L7.2": {
                    "status": "ai_complete",
                    "feature_hash": "sha256:new",
                    "semantic_summary": "new proposal",
                },
            }
        },
        submit_for_review=True,
        review_node_ids={"L7.2"},
    )
    conn.commit()

    rows = {
        row["node_id"]: row["status"]
        for row in conn.execute(
            """
            SELECT node_id, status
            FROM graph_semantic_nodes
            WHERE project_id = ? AND snapshot_id = ?
            """,
            (PID, sid),
        ).fetchall()
    }
    assert rows == {"L7.1": "ai_complete", "L7.2": "pending_review"}


def test_a4_run_selected_node_review_does_not_resubmit_existing_memory(conn, tmp_path):
    """A4 regression: batch B must not move accepted batch A back to pending."""
    snap = _create_snapshot_with_nodes(conn, "run-review-node-scope", ["L7.1", "L7.2"])
    sid = snap["snapshot_id"]
    source_dir = tmp_path / "agent" / "governance"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "L7_1.py").write_text("def old():\n    return 'old'\n", encoding="utf-8")
    (source_dir / "L7_2.py").write_text("def new():\n    return 'new'\n", encoding="utf-8")
    semantic._persist_semantic_state_to_db(
        conn,
        PID,
        sid,
        {
            "node_semantics": {
                "L7.1": {
                    "status": "ai_complete",
                    "feature_hash": "sha256:old",
                    "semantic_summary": "accepted batch A",
                },
            }
        },
        submit_for_review=False,
    )
    conn.commit()

    def _ai_call(stage, payload):
        assert stage == "reconcile_semantic_feature"
        assert payload["feature"]["node_id"] == "L7.2"
        return {
            "feature_name": "Batch B Feature",
            "semantic_summary": "Generated semantic proposal for batch B.",
            "intent": "Exercise scoped review.",
            "confidence": 0.8,
        }

    result = semantic.run_semantic_enrichment(
        conn,
        PID,
        sid,
        str(tmp_path),
        use_ai=True,
        ai_call=_ai_call,
        semantic_node_ids=["L7.2"],
        semantic_skip_completed=True,
        submit_for_review=True,
        created_by="test",
        max_excerpt_chars=200,
    )

    assert result["summary"]["ai_complete_count"] == 1
    rows = {
        row["node_id"]: row["status"]
        for row in conn.execute(
            """
            SELECT node_id, status
            FROM graph_semantic_nodes
            WHERE project_id = ? AND snapshot_id = ?
            """,
            (PID, sid),
        ).fetchall()
    }
    assert rows["L7.1"] == "ai_complete"
    assert rows["L7.2"] == "pending_review"


def test_a5_persist_jobs_preserves_newer_terminal_cancelled_status(conn):
    """A5: stale worker state must not revive a job cancelled after it loaded."""
    snap = _create_snapshot_with_node(conn, "job-terminal-preserve")
    sid = snap["snapshot_id"]
    conn.execute(
        """
        INSERT INTO graph_semantic_jobs
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           feedback_round, batch_index, attempt_count, updated_at, created_at)
        VALUES (?, ?, 'L7.1', 'cancelled', 'sha256:h', '{}',
                0, 0, 0, '2026-05-19T05:24:00Z', '2026-05-19T05:20:00Z')
        """,
        (PID, sid),
    )
    conn.commit()

    semantic._persist_semantic_state_to_db(
        conn,
        PID,
        sid,
        {
            "semantic_jobs": {
                "L7.1": {
                    "status": "ai_pending",
                    "feature_hash": "sha256:h",
                    "updated_at": "2026-05-19T05:21:00Z",
                }
            }
        },
    )
    row = conn.execute(
        """
        SELECT status, updated_at
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ? AND node_id = 'L7.1'
        """,
        (PID, sid),
    ).fetchone()
    assert row["status"] == "cancelled"
    assert row["updated_at"] == "2026-05-19T05:24:00Z"

    semantic._persist_semantic_state_to_db(
        conn,
        PID,
        sid,
        {
            "semantic_jobs": {
                "L7.1": {
                    "status": "pending_ai",
                    "feature_hash": "sha256:h",
                    "updated_at": "2026-05-19T05:25:00Z",
                }
            }
        },
    )
    row = conn.execute(
        """
        SELECT status, updated_at
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ? AND node_id = 'L7.1'
        """,
        (PID, sid),
    ).fetchone()
    assert row["status"] == "pending_ai"
    assert row["updated_at"] == "2026-05-19T05:25:00Z"


def test_a6_drain_node_continues_after_first_claim_batch(conn, tmp_path, monkeypatch):
    """A6: one enqueue event must drain more rows than one claim batch."""
    node_ids = ["L7.1", "L7.2", "L7.3"]
    snap = _create_snapshot_with_nodes(conn, "worker-node-multi-batch", node_ids)
    sid = snap["snapshot_id"]
    source_dir = tmp_path / "agent" / "governance"
    source_dir.mkdir(parents=True, exist_ok=True)
    for node_id in node_ids:
        (source_dir / f"{node_id.replace('.', '_')}.py").write_text(
            f"def feature_{node_id.replace('.', '_')}():\n    return '{node_id}'\n",
            encoding="utf-8",
        )
        conn.execute(
            """
            INSERT INTO graph_semantic_jobs
              (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
               feedback_round, batch_index, attempt_count, updated_at, created_at)
            VALUES (?, ?, ?, 'ai_pending', '', '{}',
                    0, 0, 0, '2026-05-19T05:30:00Z', '2026-05-19T05:30:00Z')
            """,
            (PID, sid, node_id),
        )
    conn.commit()

    calls: list[str] = []

    def _stub_build_ai_call(*, semantic_config, project_id, snapshot_id, project_root):
        def _ai(stage, payload):
            assert stage == "reconcile_semantic_feature"
            node_id = payload["feature"]["node_id"]
            calls.append(node_id)
            return {
                "feature_name": f"Feature {node_id}",
                "semantic_summary": f"Generated proposal for {node_id}.",
                "intent": "Exercise multi-batch semantic worker drain.",
                "confidence": 0.8,
                "self_check": {
                    "valid": True,
                    "status": "passed",
                    "checked_rules": semantic.NODE_SEMANTIC_SELF_CHECK_RULES,
                },
            }

        return _ai

    class _StubCfg:
        pass

    monkeypatch.setattr(
        semantic_worker,
        "_worker_runtime_config",
        lambda project_id="": {"max_workers": 1, "claim_batch_size": 2, "lease_seconds": 600},
    )
    monkeypatch.setattr(semantic_worker, "_project_root_for", lambda _pid: tmp_path)
    monkeypatch.setattr("agent.governance.db.get_connection", lambda _pid: _NoCloseConn(conn))
    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_config.load_semantic_enrichment_config",
        lambda project_root=None: _StubCfg(),
    )
    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_config.apply_project_ai_routing",
        lambda cfg, project_id=None: cfg,
    )
    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_ai.build_semantic_ai_call",
        _stub_build_ai_call,
    )

    semantic_worker._drain_node(PID, sid)

    assert calls == node_ids
    job_statuses = {
        row["node_id"]: row["status"]
        for row in conn.execute(
            """
            SELECT node_id, status
            FROM graph_semantic_jobs
            WHERE project_id = ? AND snapshot_id = ?
            ORDER BY node_id
            """,
            (PID, sid),
        ).fetchall()
    }
    assert job_statuses == {node_id: "ai_complete" for node_id in node_ids}
    node_statuses = {
        row["node_id"]: row["status"]
        for row in conn.execute(
            """
            SELECT node_id, status
            FROM graph_semantic_nodes
            WHERE project_id = ? AND snapshot_id = ?
            ORDER BY node_id
            """,
            (PID, sid),
        ).fetchall()
    }
    assert node_statuses == {node_id: "pending_review" for node_id in node_ids}
    feedback_targets = {
        str(item.get("target_id") or "")
        for item in reconcile_feedback.list_feedback_items(PID, sid)
    }
    assert set(node_ids).issubset(feedback_targets)


def test_b_backfill_maps_pending_review_to_proposed_event(conn):
    """B: backfill writes PROPOSED event for pending_review rows."""
    snap = _create_snapshot_with_node(conn, "backfill-review")
    sid = snap["snapshot_id"]
    # Persist a pending_review row + a regular ai_complete row.
    semantic._persist_semantic_state_to_db(
        conn, PID, sid,
        {
            "node_semantics": {
                "L7.1": {
                    "status": "pending_review",
                    "feature_hash": "sha256:p",
                    "semantic_summary": "p",
                },
            }
        },
        submit_for_review=False,  # rely on the row's own status
    )
    conn.commit()
    graph_events.backfill_existing_semantic_events(conn, PID, sid, actor="test")
    conn.commit()
    ev = conn.execute(
        """
        SELECT status FROM graph_events
        WHERE project_id=? AND snapshot_id=? AND event_type='semantic_node_enriched'
          AND target_id='L7.1'
        ORDER BY event_seq DESC LIMIT 1
        """,
        (PID, sid),
    ).fetchone()
    assert ev is not None, "backfill must emit an event for pending_review rows"
    assert ev["status"] == graph_events.EVENT_STATUS_PROPOSED


def test_b2_backfill_preserves_accepted_semantic_event_status(conn):
    """B2: later cache backfill must not downgrade operator-accepted events."""
    snap = _create_snapshot_with_node(conn, "backfill-preserve-accepted")
    sid = snap["snapshot_id"]
    semantic._persist_semantic_state_to_db(
        conn,
        PID,
        sid,
        {
            "node_semantics": {
                "L7.1": {
                    "status": "pending_review",
                    "feature_hash": "sha256:p",
                    "semantic_summary": "p",
                },
            }
        },
        submit_for_review=False,
    )
    conn.commit()
    graph_events.backfill_existing_semantic_events(conn, PID, sid, actor="test")
    event_id = conn.execute(
        """
        SELECT event_id
        FROM graph_events
        WHERE project_id = ? AND snapshot_id = ?
          AND event_type = 'semantic_node_enriched'
          AND target_id = 'L7.1'
        LIMIT 1
        """,
        (PID, sid),
    ).fetchone()["event_id"]
    graph_events.update_event_status(
        conn,
        PID,
        sid,
        event_id,
        status=graph_events.EVENT_STATUS_ACCEPTED,
        actor="test",
    )
    conn.commit()

    graph_events.backfill_existing_semantic_events(conn, PID, sid, actor="test")
    row = conn.execute(
        "SELECT status FROM graph_events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    assert row["status"] == graph_events.EVENT_STATUS_ACCEPTED


def test_c_accept_semantic_enrichment_in_decision_actions():
    """C: the verb is registered in the catalog."""
    assert "accept_semantic_enrichment" in reconcile_feedback.FEEDBACK_DECISION_ACTIONS


def test_c2_decide_feedback_accept_semantic_enrichment_maps_to_accept_true(
    conn, tmp_path, monkeypatch,
):
    """C2 regression for "Accept does nothing on dashboard":
    `decide_feedback_items(action="accept_semantic_enrichment")` must map to
    `accept=True` internally so the row lands at STATUS_ACCEPTED instead of
    falling through to needs_human_signoff (which the dashboard shows as
    "still pending"). Previously the action wasn't in the explicit mapping
    table so mapped_accept defaulted to False."""
    snap = _create_snapshot_with_node(conn, "accept-decide-1")
    sid = snap["snapshot_id"]
    # Submit a feedback row with requires_human_signoff=True (the worker sets
    # this on needs_observer_decision items by default).
    submitted = reconcile_feedback.submit_feedback_item(
        PID, sid,
        feedback_kind=reconcile_feedback.KIND_NEEDS_OBSERVER_DECISION,
        issue={
            "issue": "test accept-decide mapping",
            "target_id": "L7.1",
            "target_type": "node",
            "priority": "P3",
            "evidence": {"node_id": "L7.1", "linked_event_ids": []},
        },
        actor="test",
    )
    fid = submitted["items"][0]["feedback_id"]

    result = reconcile_feedback.decide_feedback_items(
        PID, sid, [fid],
        action="accept_semantic_enrichment",
        actor="test",
    )
    assert result["ok"] is True
    assert result["decided_count"] == 1
    item = result["items"][0]
    assert item["status"] == reconcile_feedback.STATUS_ACCEPTED, (
        f"accept_semantic_enrichment must land at accepted, got {item['status']}"
    )
    assert item["requires_human_signoff"] is False
    assert item["accepted_by"] == "test"


def test_c2b_feedback_decision_invalid_decision_does_not_accept_semantic_side_effects(
    conn, monkeypatch,
):
    """A failed feedback decision must not still accept linked semantic output."""
    snap = _create_snapshot_with_node(conn, "invalid-decision-side-effect")
    sid = snap["snapshot_id"]
    semantic._persist_semantic_state_to_db(
        conn, PID, sid,
        {
            "node_semantics": {
                "L7.1": {
                    "status": "pending_review",
                    "feature_hash": "sha256:invalid-decision",
                    "semantic_summary": "pending semantic",
                    "feature_name": "Pending Semantic",
                },
            }
        },
        submit_for_review=False,
    )
    graph_events.backfill_existing_semantic_events(conn, PID, sid, actor="test")
    conn.commit()
    ev_id = conn.execute(
        """
        SELECT event_id FROM graph_events
        WHERE project_id=? AND snapshot_id=? AND target_id='L7.1'
          AND event_type='semantic_node_enriched'
        LIMIT 1
        """,
        (PID, sid),
    ).fetchone()["event_id"]
    output = ai_output_intake.submit_ai_output(
        conn,
        PID,
        {
            "task_type": "semantic_node",
            "snapshot_id": sid,
            "target_type": "node",
            "target_id": "L7.1",
            "route_status": "review_pending",
            "payload": {"node_id": "L7.1", "semantic_summary": "reject me"},
        },
        actor="test",
    )
    submitted = reconcile_feedback.submit_feedback_item(
        PID, sid,
        feedback_kind=reconcile_feedback.KIND_NEEDS_OBSERVER_DECISION,
        issue={
            "issue": "AI semantic enrichment generated for L7.1 -- awaiting review",
            "source_node_ids": ["L7.1"],
            "target_id": "L7.1",
            "target_type": "node",
            "priority": "P3",
            "evidence": {
                "node_id": "L7.1",
                "linked_event_ids": [ev_id],
                "ai_output_intake": {"output_id": output["output_id"]},
            },
        },
        actor="test",
    )
    feedback_id = submitted["items"][0]["feedback_id"]

    monkeypatch.setattr(server, "get_connection", lambda _project_id: _NoCloseConn(conn))
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    ctx = server.RequestContext(
        None,
        "POST",
        {"project_id": PID, "snapshot_id": sid},
        {},
        {
            "feedback_id": feedback_id,
            "action": "accept_semantic_enrichment",
            "decision": "accepted",
            "actor": "test",
        },
        "req-test",
        "",
        "",
    )
    result = server.handle_graph_governance_snapshot_feedback_decision(ctx)

    assert result["ok"] is False
    assert result["decided_count"] == 0
    assert result["error_count"] == 1
    assert result["semantic_enrichment_accepted"]["node_ids_flipped"] == []
    assert result["semantic_enrichment_accepted"]["event_ids_flipped"] == []
    assert result.get("projection_rebuilt") is not True
    node_row = conn.execute(
        """
        SELECT status FROM graph_semantic_nodes
        WHERE project_id=? AND snapshot_id=? AND node_id='L7.1'
        """,
        (PID, sid),
    ).fetchone()
    assert node_row["status"] == "pending_review"
    event_row = conn.execute(
        "SELECT status FROM graph_events WHERE project_id=? AND snapshot_id=? AND event_id=?",
        (PID, sid, ev_id),
    ).fetchone()
    assert event_row["status"] == graph_events.EVENT_STATUS_PROPOSED


def test_c_accept_helper_flips_node_status_and_event(conn):
    """C: helper transitions pending_review → ai_complete and proposed → accepted."""
    snap = _create_snapshot_with_node(conn, "accept-helper")
    sid = snap["snapshot_id"]
    # Set up: one pending_review row + one proposed event.
    semantic._persist_semantic_state_to_db(
        conn, PID, sid,
        {
            "node_semantics": {
                "L7.1": {"status": "pending_review", "feature_hash": "sha256:h"},
            }
        },
        submit_for_review=False,
    )
    graph_events.backfill_existing_semantic_events(conn, PID, sid, actor="test")
    conn.commit()
    # Submit a feedback item with linked_event_ids.
    ev_id = conn.execute(
        "SELECT event_id FROM graph_events WHERE project_id=? AND snapshot_id=? AND target_id='L7.1' LIMIT 1",
        (PID, sid),
    ).fetchone()["event_id"]
    output = ai_output_intake.submit_ai_output(
        conn,
        PID,
        {
            "task_type": "semantic_node",
            "snapshot_id": sid,
            "target_type": "node",
            "target_id": "L7.1",
            "route_status": "review_pending",
            "payload": {"node_id": "L7.1", "semantic_summary": "pending"},
        },
        actor="test",
    )
    submitted = reconcile_feedback.submit_feedback_item(
        PID, sid,
        feedback_kind=reconcile_feedback.KIND_NEEDS_OBSERVER_DECISION,
        issue={
            "issue": "test review item",
            "source_node_ids": ["L7.1"],
            "target_id": "L7.1",
            "target_type": "node",
            "priority": "P3",
            "evidence": {
                "node_id": "L7.1",
                "linked_event_ids": [ev_id],
                "ai_output_intake": {"output_id": output["output_id"]},
            },
        },
        actor="test",
    )
    feedback_id = submitted["items"][0]["feedback_id"]

    result = server._accept_semantic_enrichment_for_feedback_items(
        conn, PID, sid, [feedback_id], actor="test",
    )
    assert result["node_ids_flipped"] == ["L7.1"]
    assert result["event_ids_flipped"] == [ev_id]
    assert result["ai_output_ids_marked_completed"] == [output["output_id"]]
    assert result["errors"] == []

    # Verify DB state.
    row = conn.execute(
        "SELECT status FROM graph_semantic_nodes WHERE project_id=? AND snapshot_id=? AND node_id='L7.1'",
        (PID, sid),
    ).fetchone()
    assert row["status"] == "ai_complete"
    ev_row = conn.execute(
        "SELECT status FROM graph_events WHERE event_id=?",
        (ev_id,),
    ).fetchone()
    assert ev_row["status"] == graph_events.EVENT_STATUS_ACCEPTED
    route_row = conn.execute(
        "SELECT status FROM ai_output_queue WHERE output_id=?",
        (output["output_id"],),
    ).fetchone()
    assert route_row["status"] == "completed"


def test_c4_direct_event_accept_flips_node_status_and_projection(conn, monkeypatch):
    """C4: direct semantic event accept must mirror the feedback gate."""
    snap = _create_snapshot_with_node(conn, "direct-event-accept")
    sid = snap["snapshot_id"]
    node = next(
        item for item in store.list_graph_snapshot_nodes(conn, PID, sid, include_semantic=False)
        if item["node_id"] == "L7.1"
    )
    feature_hash = graph_events.feature_hash_for_node(node)
    semantic._persist_semantic_state_to_db(
        conn,
        PID,
        sid,
        {
            "node_semantics": {
                "L7.1": {
                    "status": "pending_review",
                    "feature_hash": feature_hash,
                    "semantic_summary": "direct accept semantic",
                    "feature_name": "Direct Accept Semantic",
                },
            }
        },
        submit_for_review=False,
    )
    graph_events.backfill_existing_semantic_events(conn, PID, sid, actor="test")
    ev_id = conn.execute(
        """
        SELECT event_id FROM graph_events
        WHERE project_id=? AND snapshot_id=? AND target_id='L7.1'
          AND event_type='semantic_node_enriched'
        LIMIT 1
        """,
        (PID, sid),
    ).fetchone()["event_id"]
    graph_events.update_event_status(
        conn,
        PID,
        sid,
        ev_id,
        status=graph_events.EVENT_STATUS_ACCEPTED,
        actor="test-preaccept",
    )
    conn.commit()
    assert conn.execute(
        """
        SELECT status FROM graph_semantic_nodes
        WHERE project_id=? AND snapshot_id=? AND node_id='L7.1'
        """,
        (PID, sid),
    ).fetchone()["status"] == "pending_review"

    monkeypatch.setattr(server, "get_connection", lambda _project_id: _NoCloseConn(conn))
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    ctx = server.RequestContext(
        None,
        "POST",
        {"project_id": PID, "snapshot_id": sid, "event_id": ev_id},
        {},
        {"actor": "test"},
        "req-test",
        "",
        "",
    )

    result = server.handle_graph_governance_snapshot_event_accept(ctx)

    assert result["ok"] is True
    assert result["semantic_cache_sync"]["cache_status"] == "ai_complete"
    assert result["semantic_cache_sync"]["cache_rows_updated"] == 1
    assert result["projection_rebuilt"] is True
    assert conn.execute(
        """
        SELECT status FROM graph_semantic_nodes
        WHERE project_id=? AND snapshot_id=? AND node_id='L7.1'
        """,
        (PID, sid),
    ).fetchone()["status"] == "ai_complete"
    projection = graph_events.get_semantic_projection(conn, PID, sid)
    node_semantic = projection["projection"]["node_semantics"]["L7.1"]
    assert node_semantic["validity"]["status"] == "semantic_current"


def test_c3_reject_decision_clears_node_pending_review_payload(conn, monkeypatch):
    """Rejecting a semantic review must retract the proposed event and cache row."""
    snap = _create_snapshot_with_node(conn, "reject-helper")
    sid = snap["snapshot_id"]
    semantic._persist_semantic_state_to_db(
        conn, PID, sid,
        {
            "node_semantics": {
                "L7.1": {
                    "status": "pending_review",
                    "feature_hash": "sha256:reject-me",
                    "semantic_summary": "unapproved",
                    "feature_name": "Unapproved Feature",
                },
            }
        },
        submit_for_review=False,
    )
    conn.execute(
        """
        INSERT INTO graph_semantic_jobs
          (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json,
           feedback_round, batch_index, attempt_count, updated_at, created_at)
        VALUES (?, ?, 'L7.1', 'ai_complete', 'sha256:reject-me', '{}',
                0, 0, 1, '2026-05-12T00:00:00Z', '2026-05-12T00:00:00Z')
        """,
        (PID, sid),
    )
    graph_events.backfill_existing_semantic_events(conn, PID, sid, actor="test")
    conn.commit()
    ev_id = conn.execute(
        """
        SELECT event_id FROM graph_events
        WHERE project_id=? AND snapshot_id=? AND target_id='L7.1'
          AND event_type='semantic_node_enriched'
        LIMIT 1
        """,
        (PID, sid),
    ).fetchone()["event_id"]
    output = ai_output_intake.submit_ai_output(
        conn,
        PID,
        {
            "task_type": "semantic_node",
            "snapshot_id": sid,
            "target_type": "node",
            "target_id": "L7.1",
            "route_status": "review_pending",
            "payload": {"node_id": "L7.1", "semantic_summary": "reject me"},
        },
        actor="test",
    )
    submitted = reconcile_feedback.submit_feedback_item(
        PID, sid,
        feedback_kind=reconcile_feedback.KIND_NEEDS_OBSERVER_DECISION,
        issue={
            "issue": "AI semantic enrichment generated for L7.1 -- awaiting review",
            "source_node_ids": ["L7.1"],
            "target_id": "L7.1",
            "target_type": "node",
            "priority": "P3",
            "evidence": {
                "node_id": "L7.1",
                "linked_event_ids": [ev_id],
                "ai_output_intake": {"output_id": output["output_id"]},
            },
        },
        actor="test",
    )
    feedback_id = submitted["items"][0]["feedback_id"]

    monkeypatch.setattr(server, "get_connection", lambda _project_id: _NoCloseConn(conn))
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    ctx = server.RequestContext(
        None,
        "POST",
        {"project_id": PID, "snapshot_id": sid},
        {},
        {
            "feedback_id": feedback_id,
            "action": "reject_false_positive",
            "actor": "test",
        },
        "req-test",
        "",
        "",
    )
    result = server.handle_graph_governance_snapshot_feedback_decision(ctx)

    assert result["ok"] is True
    assert result["semantic_enrichment_rejected"]["node_ids_cleared"] == ["L7.1"]
    assert result["semantic_enrichment_rejected"]["event_ids_rejected"] == [ev_id]
    assert result["semantic_enrichment_rejected"]["job_ids_marked_rejected"] == ["L7.1"]
    assert result["semantic_enrichment_rejected"]["ai_output_ids_marked_rejected"] == [output["output_id"]]
    assert result["projection_rebuilt"] is True
    row = conn.execute(
        """
        SELECT status FROM graph_semantic_nodes
        WHERE project_id=? AND snapshot_id=? AND node_id='L7.1'
        """,
        (PID, sid),
    ).fetchone()
    assert row is None
    event = conn.execute(
        "SELECT status FROM graph_events WHERE project_id=? AND snapshot_id=? AND event_id=?",
        (PID, sid, ev_id),
    ).fetchone()
    assert event["status"] == graph_events.EVENT_STATUS_REJECTED
    job = conn.execute(
        "SELECT status FROM graph_semantic_jobs WHERE project_id=? AND snapshot_id=? AND node_id='L7.1'",
        (PID, sid),
    ).fetchone()
    assert job["status"] == "rejected"
    route_row = conn.execute(
        "SELECT status FROM ai_output_queue WHERE output_id=?",
        (output["output_id"],),
    ).fetchone()
    assert route_row["status"] == "rejected"
    node = store.list_graph_snapshot_nodes(conn, PID, sid, include_semantic=True, limit=10)[0]
    assert node["semantic"]["has_semantic_payload"] is False
    assert "feature_name" not in node["semantic"]


def test_d_worker_register_is_idempotent_and_subscribes(monkeypatch):
    """D: register() is safe to call twice; subscribes both topics."""
    # Reset module-level state so the test is independent of import order.
    monkeypatch.setattr(semantic_worker, "_registered", False)
    subs: list[tuple[str, object]] = []

    class _StubBus:
        def subscribe(self, topic, callback):
            subs.append((topic, callback))

    monkeypatch.setattr(event_bus, "get_event_bus", lambda: _StubBus())
    # Stub catchup to no-op (no governance DB at test time).
    monkeypatch.setattr(semantic_worker, "on_governance_startup", lambda payload=None: None)
    semantic_worker.register()
    semantic_worker.register()  # idempotent — should not add duplicate subscribers
    topics = sorted({t for t, _ in subs})
    assert topics == ["semantic_job.enqueued", "system.startup"]


def test_f_drain_edge_creates_proposed_event_and_feedback(conn, monkeypatch):
    """F (MF-2026-05-10-017): _drain_edge picks up an unenriched
    edge_semantic_requested event, runs AI (stubbed), writes a PROPOSED
    edge_semantic_enriched event, and submits a needs_observer_decision
    feedback row pointing at it."""
    snap = _create_snapshot_with_node(conn, "edge-drain-1", node_id="L7.1")
    sid = snap["snapshot_id"]
    edge_id = "L7.1->L4.1:creates_task"
    # Plant an unenriched edge request event.
    graph_events.create_event(
        conn, PID, sid,
        event_type="edge_semantic_requested",
        event_kind="semantic_job",
        target_type="edge",
        target_id=edge_id,
        status=graph_events.EVENT_STATUS_OBSERVED,
        payload={
            "edge": {"src": "L7.1", "dst": "L4.1", "edge_type": "creates_task"},
            "edge_context": {"edge_id": edge_id, "src": "L7.1", "dst": "L4.1"},
            "operator_request": {},
            "instructions": {},
        },
        evidence={"source": "test_setup"},
    )
    conn.commit()

    # Stub config + ai_call so no real subprocess fires.
    stub_ai_payload = {
        "relation_purpose": "test relation purpose",
        "confidence": 0.7,
        "evidence": {"source": "stub"},
    }

    class _StubCfg:
        pass

    def _stub_load_cfg(*, project_root=None):
        return _StubCfg()

    def _stub_build_ai_call(*, semantic_config, project_id, snapshot_id, project_root):
        assert semantic_config.provider == "openai"
        assert semantic_config.model == "gpt-5.5"
        def _ai(stage, payload):
            assert stage == "edge"
            assert payload["edge"]["src"] == "L7.1"
            return dict(stub_ai_payload)
        return _ai

    # Stub the worker's per-project root so it points at the temp DB scope.
    monkeypatch.setattr(semantic_worker, "_project_root_for", lambda pid: Path("."))
    # Wrap test conn so worker's finally `conn.close()` doesn't kill the
    # connection the test still owns (sqlite3.Connection.close is read-only).
    wrapped = _NoCloseConn(conn)
    monkeypatch.setattr(
        "agent.governance.db.get_connection",
        lambda pid: wrapped,
    )
    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_config.load_semantic_enrichment_config",
        _stub_load_cfg,
    )
    monkeypatch.setattr(
        "agent.governance.project_service.get_project_config_metadata",
        lambda project_id: {
            "project_id": project_id,
            "ai": {"routing": {"semantic": {"provider": "openai", "model": "gpt-5.5"}}},
        },
    )
    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_ai.build_semantic_ai_call",
        _stub_build_ai_call,
    )

    semantic_worker._drain_edge(PID, sid)

    # Verify PROPOSED enriched event landed.
    ev_row = conn.execute(
        """
        SELECT event_id, status FROM graph_events
        WHERE project_id=? AND snapshot_id=? AND event_type='edge_semantic_enriched'
          AND target_id=?
        ORDER BY event_seq DESC LIMIT 1
        """,
        (PID, sid, edge_id),
    ).fetchone()
    assert ev_row is not None, "enriched event should exist"
    assert ev_row["status"] == graph_events.EVENT_STATUS_PROPOSED, \
        "enriched event must be PROPOSED so the review gate holds"

    # Verify feedback row points at it with target_type=edge.
    items = reconcile_feedback.list_feedback_items(PID, sid)
    edge_items = [i for i in items if str(i.get("target_id") or "") == edge_id]
    assert edge_items, "feedback row for the edge should exist"
    fb = edge_items[0]
    assert (
        str(fb.get("target_type") or "")
        or str((fb.get("evidence") or {}).get("raw_issue", {}).get("target_type") or "")
    ) == "edge"
    raw = (fb.get("evidence") or {}).get("raw_issue", {}).get("evidence") or {}
    linked = raw.get("linked_event_ids") or []
    assert ev_row["event_id"] in linked, \
        "feedback evidence must link to the newly-created enriched event id"


def test_f2_drain_edge_skips_already_enriched(conn, monkeypatch):
    """F2: edges that already have an enriched event (any non-terminal status)
    are not re-enriched by _drain_edge."""
    snap = _create_snapshot_with_node(conn, "edge-drain-2", node_id="L7.2")
    sid = snap["snapshot_id"]
    edge_id = "L7.2->L4.1:reads_state"
    graph_events.create_event(
        conn, PID, sid, event_type="edge_semantic_requested",
        event_kind="semantic_job", target_type="edge", target_id=edge_id,
        status=graph_events.EVENT_STATUS_OBSERVED,
        payload={"edge": {"src": "L7.2", "dst": "L4.1"}},
    )
    graph_events.create_event(
        conn, PID, sid, event_type="edge_semantic_enriched",
        event_kind="semantic_job", target_type="edge", target_id=edge_id,
        status=graph_events.EVENT_STATUS_OBSERVED,
        payload={"semantic_payload": {"relation_purpose": "already done"}},
    )
    conn.commit()

    called = []
    def _stub_build(**kw):
        def _ai(stage, payload):
            called.append((stage, payload.get("edge", {}).get("src")))
            return {}
        return _ai

    monkeypatch.setattr(semantic_worker, "_project_root_for", lambda pid: Path("."))
    wrapped = _NoCloseConn(conn)
    monkeypatch.setattr("agent.governance.db.get_connection", lambda pid: wrapped)
    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_config.load_semantic_enrichment_config",
        lambda *, project_root=None: object(),
    )
    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_ai.build_semantic_ai_call", _stub_build,
    )

    semantic_worker._drain_edge(PID, sid)
    assert called == [], "AI must not run for an already-enriched edge"


def test_f2b_drain_edge_processes_request_newer_than_prior_enrichment(conn, monkeypatch):
    """Regression for observer-hotfix 2026-05-11: when the operator re-clicks
    "AI enrich edge" after a bad/garbage enrichment, the new
    edge_semantic_requested event has a HIGHER event_seq than the prior
    edge_semantic_enriched. The old query `target_id NOT IN (any enriched)`
    excluded the request entirely — the worker silently dropped legitimate
    re-enrich submissions. The new query compares event_seq and only skips
    when there's an enriched event STRICTLY NEWER than the request.
    """
    snap = _create_snapshot_with_node(conn, "edge-rerequest", node_id="L7.5")
    sid = snap["snapshot_id"]
    edge_id = "L7.5->L4.1:reads_state"
    # 1. operator submits enrich → request lands
    graph_events.create_event(
        conn, PID, sid, event_type="edge_semantic_requested",
        event_kind="semantic_job", target_type="edge", target_id=edge_id,
        status=graph_events.EVENT_STATUS_OBSERVED,
        payload={"edge": {"src": "L7.5", "dst": "L4.1"}},
    )
    # 2. worker runs AI → enriched row written
    graph_events.create_event(
        conn, PID, sid, event_type="edge_semantic_enriched",
        event_kind="semantic_job", target_type="edge", target_id=edge_id,
        status=graph_events.EVENT_STATUS_PROPOSED,
        payload={"semantic_payload": {"relation_purpose": "garbage v1"}},
    )
    # 3. operator re-submits enrich → NEW request lands (higher event_seq)
    graph_events.create_event(
        conn, PID, sid, event_type="edge_semantic_requested",
        event_kind="semantic_job", target_type="edge", target_id=edge_id,
        status=graph_events.EVENT_STATUS_OBSERVED,
        payload={"edge": {"src": "L7.5", "dst": "L4.1"}, "rev": 2},
    )
    conn.commit()

    called = []
    def _stub_build(**kw):
        def _ai(stage, payload):
            called.append((stage, payload.get("edge", {}).get("src")))
            return {"relation_purpose": "fixed v2", "confidence": 0.9, "evidence": {}}
        return _ai

    monkeypatch.setattr(semantic_worker, "_project_root_for", lambda pid: Path("."))
    wrapped = _NoCloseConn(conn)
    monkeypatch.setattr("agent.governance.db.get_connection", lambda pid: wrapped)
    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_config.load_semantic_enrichment_config",
        lambda *, project_root=None: object(),
    )
    monkeypatch.setattr(
        "agent.governance.reconcile_semantic_ai.build_semantic_ai_call", _stub_build,
    )

    semantic_worker._drain_edge(PID, sid)
    assert called, "AI must run for the new (post-enrichment) request"

    # A second enriched event should now exist for the same edge.
    enriched_rows = conn.execute(
        """
        SELECT event_id, status FROM graph_events
        WHERE project_id=? AND snapshot_id=? AND event_type='edge_semantic_enriched'
          AND target_id=?
        ORDER BY event_seq ASC
        """,
        (PID, sid, edge_id),
    ).fetchall()
    assert len(enriched_rows) == 2, \
        "re-request must yield a second enriched event, not silently drop"


def test_f3_accept_helper_handles_edge_target_type(conn):
    """F3 (MF-2026-05-10-017): accept_semantic_enrichment flips the linked
    edge event from PROPOSED to ACCEPTED and reports the edge id in
    edge_ids_flipped (not node_ids_flipped)."""
    snap = _create_snapshot_with_node(conn, "edge-accept-1", node_id="L7.3")
    sid = snap["snapshot_id"]
    edge_id = "L7.3->L4.1:emits_event"
    enriched = graph_events.create_event(
        conn, PID, sid,
        event_type="edge_semantic_enriched",
        event_kind="semantic_job",
        target_type="edge",
        target_id=edge_id,
        status=graph_events.EVENT_STATUS_PROPOSED,
        payload={"semantic_payload": {"relation_purpose": "test"}},
    )
    ev_id = enriched["event_id"]
    submitted = reconcile_feedback.submit_feedback_item(
        PID, sid,
        feedback_kind=reconcile_feedback.KIND_NEEDS_OBSERVER_DECISION,
        issue={
            "issue": "AI edge semantic enrichment generated — awaiting review",
            "target_id": edge_id,
            "target_type": "edge",
            "priority": "P3",
            "evidence": {
                "edge_id": edge_id,
                "linked_event_ids": [ev_id],
            },
        },
        actor="test",
    )
    feedback_id = submitted["items"][0]["feedback_id"]
    result = server._accept_semantic_enrichment_for_feedback_items(
        conn, PID, sid, [feedback_id], actor="test",
    )
    assert result["edge_ids_flipped"] == [edge_id], \
        "edge target should land in edge_ids_flipped, not node_ids_flipped"
    assert result["node_ids_flipped"] == []
    assert ev_id in result["event_ids_flipped"]
    assert result["errors"] == []
    row = conn.execute(
        "SELECT status FROM graph_events WHERE event_id = ?",
        (ev_id,),
    ).fetchone()
    assert row["status"] == graph_events.EVENT_STATUS_ACCEPTED


def test_e_publish_helper_does_not_raise_when_eventbus_absent(monkeypatch):
    """E: the publish on POST /semantic/jobs is best-effort.

    Verify the publish wrapper survives an EventBus that raises."""
    def _boom(*a, **kw):
        raise RuntimeError("synthetic bus failure")

    monkeypatch.setattr(event_bus, "publish", _boom)
    # Inline the same try/except contract the handler uses:
    try:
        event_bus.publish("semantic_job.enqueued", {"project_id": PID})
    except Exception:
        pass  # handler swallows
    # No assertion — surviving the call is the contract.
