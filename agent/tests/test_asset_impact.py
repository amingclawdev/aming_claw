from __future__ import annotations

import json
import sqlite3

from agent.governance import graph_snapshot_store as store
from agent.governance.asset_impact import (
    EVENT_IMPACT_DETECTED,
    EVENT_RESOLUTION_RECORDED,
    STATUS_RECORDED,
    build_asset_impact_reminder_projection,
    get_asset_impact_reminder_events,
    list_asset_impact_events,
    list_pending_asset_impact_reminders,
    record_asset_impact_resolution,
    record_scope_asset_impacts,
    resolve_asset_impact_reminder,
)
from agent.governance.asset_projection import upsert_doc_asset_projection
from agent.governance.db import _ensure_schema


PID = "asset-impact-test"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _runtime_graph() -> dict:
    return {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.runtime",
                    "layer": "L7",
                    "title": "Runtime Service",
                    "kind": "service_runtime",
                    "primary": ["src/runtime.py"],
                    "secondary": [],
                    "test": ["tests/test_runtime.py"],
                    "metadata": {
                        "config_files": ["config/runtime.yaml"],
                    },
                }
            ],
            "edges": [],
        }
    }


def _index_runtime_snapshot(conn: sqlite3.Connection, snapshot_id: str, commit_sha: str) -> None:
    graph = _runtime_graph()
    store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id=snapshot_id,
        commit_sha=commit_sha,
        snapshot_kind="scope",
        graph_json=graph,
        file_inventory=[
            {
                "path": "src/runtime.py",
                "file_kind": "source",
                "scan_status": "clustered",
                "graph_status": "mapped",
                "attached_node_ids": ["L7.runtime"],
                "mapped_node_ids": ["L7.runtime"],
            },
            {
                "path": "docs/runtime.md",
                "file_kind": "doc",
                "scan_status": "secondary_attached",
                "graph_status": "attached",
                "attached_node_ids": ["L7.runtime"],
                "mapped_node_ids": ["L7.runtime"],
            },
        ],
        notes=json.dumps({"pending_scope_reconcile": {}}),
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot_id,
        nodes=graph["deps_graph"]["nodes"],
        edges=[],
    )
    upsert_doc_asset_projection(
        conn,
        project_id=PID,
        snapshot_id=snapshot_id,
        doc_asset_state={
            "run_id": f"run-{commit_sha}",
            "commit_sha": commit_sha,
            "docs": [
                {
                    "path": "docs/runtime.md",
                    "doc_kind": "doc",
                    "binding_status": "accepted",
                    "accepted_bindings": [
                        {
                            "node_id": "L7.runtime",
                            "title": "Runtime Service",
                            "role": "doc",
                            "source": "graph_node",
                        }
                    ],
                    "binding_candidates": [],
                    "impact_scope_policy": "accepted_bindings_only",
                }
            ],
        },
    )
    conn.commit()


def _scope_delta(*changed_paths: str) -> dict:
    changed = list(changed_paths)
    return {
        "updated_nodes": ["L7.runtime"],
        "file_inventory_delta": {
            "hash_changed_files": changed,
            "impacted_files": changed,
            "changed_file_count": len(changed),
            "impacted_file_count": len(changed),
        },
    }


def test_doc_asset_impacts_aggregate_until_resolution_covers_events() -> None:
    conn = _conn()
    _index_runtime_snapshot(conn, "scope-c1", "c1")
    first = record_scope_asset_impacts(
        conn,
        PID,
        snapshot_id="scope-c1",
        commit_sha="c1",
        scope_graph_delta=_scope_delta("src/runtime.py"),
        actor="test",
    )
    _index_runtime_snapshot(conn, "scope-c2", "c2")
    second = record_scope_asset_impacts(
        conn,
        PID,
        snapshot_id="scope-c2",
        commit_sha="c2",
        scope_graph_delta=_scope_delta("config/runtime.yaml"),
        actor="test",
    )

    assert first["event_count"] == 1
    assert second["event_count"] == 1
    events = list_asset_impact_events(conn, PID, event_type=EVENT_IMPACT_DETECTED)
    assert [event["commit_sha"] for event in events] == ["c1", "c2"]
    reminders = list_pending_asset_impact_reminders(conn, PID, asset_kind="doc")
    assert len(reminders) == 1
    assert reminders[0]["asset_path"] == "docs/runtime.md"
    assert reminders[0]["node_id"] == "L7.runtime"
    assert reminders[0]["impact_count"] == 2
    assert reminders[0]["open_event_ids"] == [events[0]["id"], events[1]["id"]]
    assert reminders[0]["latest_commit_sha"] == "c2"

    record_asset_impact_resolution(
        conn,
        project_id=PID,
        covers_event_ids=[event["id"] for event in events],
        resolution_kind="keep_unchanged",
        actor="observer",
        evidence={"reason": "runtime docs still match"},
    )
    assert list_pending_asset_impact_reminders(conn, PID, asset_kind="doc") == []

    _index_runtime_snapshot(conn, "scope-c3", "c3")
    third = record_scope_asset_impacts(
        conn,
        PID,
        snapshot_id="scope-c3",
        commit_sha="c3",
        scope_graph_delta=_scope_delta("src/runtime.py"),
        actor="test",
    )
    assert third["event_count"] == 1
    reopened = list_pending_asset_impact_reminders(conn, PID, asset_kind="doc")
    assert len(reopened) == 1
    assert reopened[0]["impact_count"] == 1
    assert reopened[0]["latest_commit_sha"] == "c3"


def test_asset_impact_reminder_projection_history_and_resolve() -> None:
    conn = _conn()
    _index_runtime_snapshot(conn, "scope-c1", "c1")
    record_scope_asset_impacts(
        conn,
        PID,
        snapshot_id="scope-c1",
        commit_sha="c1",
        scope_graph_delta=_scope_delta("src/runtime.py"),
        actor="test",
    )
    _index_runtime_snapshot(conn, "scope-c2", "c2")
    record_scope_asset_impacts(
        conn,
        PID,
        snapshot_id="scope-c2",
        commit_sha="c2",
        scope_graph_delta=_scope_delta("config/runtime.yaml"),
        actor="test",
    )

    projection = build_asset_impact_reminder_projection(
        conn,
        PID,
        asset_kind="doc",
        status="pending",
    )

    assert projection["count"] == 1
    assert projection["summary"]["pending_count"] == 1
    assert projection["summary"]["open_event_count"] == 2
    assert projection["action_catalog"]["primary_actions"] == [
        "updated",
        "keep_unchanged",
        "waived",
    ]
    reminder = projection["reminders"][0]

    history = get_asset_impact_reminder_events(conn, PID, reminder["reminder_id"])
    assert history["reminder"]["impact_count"] == 2
    assert [event["event_type"] for event in history["events"]] == [
        EVENT_IMPACT_DETECTED,
        EVENT_IMPACT_DETECTED,
    ]

    resolved = resolve_asset_impact_reminder(
        conn,
        PID,
        reminder["reminder_id"],
        resolution_kind="waived",
        note="Docs intentionally stay terse.",
        actor="operator",
    )

    assert resolved["resolution"]["covers_event_ids"] == reminder["open_event_ids"]
    assert resolved["reminder"]["status"] == STATUS_RECORDED
    assert list_pending_asset_impact_reminders(conn, PID, asset_kind="doc") == []
    resolution_events = [
        event for event in resolved["events"]
        if event["event_type"] == EVENT_RESOLUTION_RECORDED
    ]
    assert len(resolution_events) == 1
    assert resolution_events[0]["actor"] == "operator"
    assert resolution_events[0]["evidence"]["resolution_kind"] == "waived"
    assert resolution_events[0]["evidence"]["note"] == "Docs intentionally stay terse."


def test_scope_doc_impact_skips_when_bound_doc_changed_in_same_commit() -> None:
    conn = _conn()
    _index_runtime_snapshot(conn, "scope-doc-changed", "c-doc")

    result = record_scope_asset_impacts(
        conn,
        PID,
        snapshot_id="scope-doc-changed",
        commit_sha="c-doc",
        scope_graph_delta=_scope_delta("src/runtime.py", "docs/runtime.md"),
        actor="test",
    )

    assert result["event_count"] == 0
    assert result["skipped_changed_asset"] == 1
    assert list_pending_asset_impact_reminders(conn, PID, asset_kind="doc") == []


def test_scope_doc_impact_requires_code_or_config_change() -> None:
    conn = _conn()
    _index_runtime_snapshot(conn, "scope-test-only", "c-test")

    result = record_scope_asset_impacts(
        conn,
        PID,
        snapshot_id="scope-test-only",
        commit_sha="c-test",
        scope_graph_delta=_scope_delta("tests/test_runtime.py"),
        actor="test",
    )

    assert result["event_count"] == 0
    assert result["skipped_non_code_node_change"] == 1
    assert list_pending_asset_impact_reminders(conn, PID, asset_kind="doc") == []


def test_db_migration_from_v43_adds_asset_impact_tables() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('schema_version', '43')"
    )

    _ensure_schema(conn)

    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "graph_asset_impact_events" in tables
    assert "graph_asset_impact_reminders" in tables
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    assert row["value"] == "44"
