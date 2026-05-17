"""MF-2026-05-10-012: activate_graph_snapshot auto-rebuilds the semantic
projection on the activated snapshot so the dashboard counters never come
up empty after a reconcile.
"""

from __future__ import annotations

import sqlite3

import pytest

from agent.governance import graph_events
from agent.governance import graph_snapshot_store as store
from agent.governance.db import _ensure_schema


PID = "activate-projection-test"


def _node(node_id: str, layer: str = "L7") -> dict:
    return {
        "id": node_id,
        "layer": layer,
        "title": f"Feature {node_id}",
        "kind": "service_runtime",
        "primary": [f"agent/governance/{node_id.replace('.', '_')}.py"],
        "secondary": [],
        "test": [],
        "metadata": {"subsystem": "governance"},
    }


def _make_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> dict:
    nodes = [_node("L7.1"), _node("L7.2")]
    edges = [{
        "source": "L7.1",
        "target": "L7.2",
        "edge_type": "depends_on",
        "direction": "dependency",
        "evidence": {"source": "test"},
    }]
    graph = {"deps_graph": {"nodes": nodes, "edges": edges}}
    snap = store.create_graph_snapshot(
        conn, PID, snapshot_id=snapshot_id, commit_sha="head",
        snapshot_kind="scope", graph_json=graph,
    )
    store.index_graph_snapshot(conn, PID, snap["snapshot_id"], nodes=nodes, edges=edges)
    conn.commit()
    return snap


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    store.ensure_schema(c)
    yield c
    c.close()


def test_activate_rebuilds_projection_when_missing(conn):
    """Fresh snapshot with no projection → auto-rebuild fires."""
    snap = _make_snapshot(conn, "fresh-no-projection")
    sid = snap["snapshot_id"]
    # Sanity: projection truly absent.
    assert graph_events.get_semantic_projection(conn, PID, sid) is None

    result = store.activate_graph_snapshot(conn, PID, sid)
    conn.commit()

    assert result["projection_status"] == "rebuilt"
    # Projection should now exist.
    projection = graph_events.get_semantic_projection(conn, PID, sid)
    assert projection is not None
    # status field on a freshly built projection should NOT be "missing"
    assert projection.get("status") not in (None, "", "missing")


def test_activate_skips_when_projection_already_present(conn):
    """Activating a snapshot that already has a projection is a no-op rebuild."""
    snap = _make_snapshot(conn, "already-projected")
    sid = snap["snapshot_id"]
    # Pre-build a projection.
    graph_events.materialize_events(conn, PID, sid, actor="setup")
    graph_events.build_semantic_projection(conn, PID, sid, actor="setup")
    conn.commit()
    before = graph_events.get_semantic_projection(conn, PID, sid)
    assert before is not None
    before_id = before.get("projection_id")

    result = store.activate_graph_snapshot(conn, PID, sid)
    conn.commit()

    assert result["projection_status"] == "already_present"
    after = graph_events.get_semantic_projection(conn, PID, sid)
    # Same projection row — we did NOT rebuild on top.
    assert after.get("projection_id") == before_id


def test_activate_with_opt_out_does_not_touch_projection(conn):
    """Recovery scripts can pass auto_rebuild_projection=False to skip the hook."""
    snap = _make_snapshot(conn, "opt-out")
    sid = snap["snapshot_id"]
    result = store.activate_graph_snapshot(
        conn, PID, sid, auto_rebuild_projection=False,
    )
    conn.commit()
    assert result["projection_status"] == "skipped"
    assert graph_events.get_semantic_projection(conn, PID, sid) is None


def test_activate_non_active_ref_name_skips_rebuild(conn):
    """Only activations to ref_name=='active' rebuild. Other refs stay opaque."""
    snap = _make_snapshot(conn, "candidate-ref")
    sid = snap["snapshot_id"]
    result = store.activate_graph_snapshot(
        conn, PID, sid, ref_name="candidate",
    )
    conn.commit()
    assert result["projection_status"] == "skipped"
    assert result["candidate_ref_update"] is True
    stored = store.get_graph_snapshot(conn, PID, sid)
    assert stored["ref_name"] == "candidate"
    assert stored["branch_ref"] == "candidate"
    assert stored["status"] == store.SNAPSHOT_STATUS_CANDIDATE
    ref_event = store.list_graph_ref_events(conn, PID, ref_name="candidate")[0]
    assert ref_event["branch_ref"] == "candidate"


def test_pending_scope_activate_param_is_plumbed_through():
    """MF-2026-05-10-014: run_pending_scope_reconcile_candidate must accept
    `activate` and forward it to the inner full-reconcile call so the
    dashboard can incrementally catch up + auto-projection in one round-trip.

    Static contract check (signature + source forwarding) — running the real
    function needs full pending-scope DB setup which is out of scope here.
    The integration smoke test happens against the live HTTP endpoint after
    governance restart.
    """
    import inspect
    from agent.governance import state_reconcile

    sig = inspect.signature(state_reconcile.run_pending_scope_reconcile_candidate)
    assert "activate" in sig.parameters, (
        "MF-014 contract: run_pending_scope_reconcile_candidate must expose `activate` kwarg"
    )
    assert sig.parameters["activate"].default is False, (
        "default must be False so existing callers (codex chain) are unchanged"
    )

    src = inspect.getsource(state_reconcile.run_pending_scope_reconcile_candidate)
    assert "activate=activate" in src, (
        "MF-014 contract: pending-scope must forward `activate=activate` to "
        "run_state_only_full_reconcile (not hardcode False)"
    )


def test_activate_does_not_rollback_when_rebuild_fails(conn, monkeypatch):
    """Projection rebuild is advisory; activation must still commit even if
    the hook raises."""
    snap = _make_snapshot(conn, "rebuild-fails")
    sid = snap["snapshot_id"]

    def _boom(*a, **kw):
        raise RuntimeError("synthetic projection failure")

    # Make materialize_events explode; activation should still succeed.
    monkeypatch.setattr(graph_events, "materialize_events", _boom)

    result = store.activate_graph_snapshot(conn, PID, sid)
    conn.commit()

    # Snapshot is active even though projection failed.
    refs = conn.execute(
        "SELECT snapshot_id FROM graph_snapshot_refs WHERE project_id = ? AND ref_name = 'active'",
        (PID,),
    ).fetchone()
    assert refs["snapshot_id"] == sid
    assert result["projection_status"].startswith("rebuild_failed:")
    assert "synthetic projection failure" in result["projection_status"]
