from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.governance import raw_requirement


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    raw_requirement.ensure_schema(conn)
    return conn


def test_capture_preserves_raw_text_without_creating_backlog():
    conn = _conn()

    row = raw_requirement.create_raw_requirement(
        conn,
        project_id="demo",
        raw_text="While the first task runs, add calendar color filters",
        source="chat",
        session_id="sess-1",
        captured_by="observer",
        metadata={"attachment_count": 0},
    )

    assert row["project_id"] == "demo"
    assert row["raw_text"] == "While the first task runs, add calendar color filters"
    assert row["status"] == raw_requirement.STATUS_RAW_INBOX
    assert row["promoted_bug_id"] == ""
    assert row["metadata"] == {"attachment_count": 0}

    tables = {
        item["name"]
        for item in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "backlog" not in tables


def test_status_lifecycle_requires_explicit_promoted_bug_id():
    conn = _conn()
    row = raw_requirement.create_raw_requirement(
        conn,
        project_id="demo",
        raw_text="Add drag-and-drop rescheduling",
    )

    needs_confirmation = raw_requirement.update_status(
        conn,
        project_id="demo",
        raw_id=row["raw_id"],
        new_status=raw_requirement.STATUS_NEEDS_CONFIRMATION,
        note="grouped with planner interaction work",
    )
    assert needs_confirmation["status"] == raw_requirement.STATUS_NEEDS_CONFIRMATION
    assert needs_confirmation["note"] == "grouped with planner interaction work"

    with pytest.raises(ValueError, match="promoted_bug_id is required"):
        raw_requirement.update_status(
            conn,
            project_id="demo",
            raw_id=row["raw_id"],
            new_status=raw_requirement.STATUS_PROMOTED,
        )

    promoted = raw_requirement.update_status(
        conn,
        project_id="demo",
        raw_id=row["raw_id"],
        new_status=raw_requirement.STATUS_PROMOTED,
        promoted_bug_id="PLANNER-DRAG-DROP",
    )
    assert promoted["status"] == raw_requirement.STATUS_PROMOTED
    assert promoted["promoted_bug_id"] == "PLANNER-DRAG-DROP"


def test_project_inbox_lanes_count_raw_and_confirmation_items():
    conn = _conn()
    raw = raw_requirement.create_raw_requirement(
        conn,
        project_id="demo",
        raw_text="Need a weekly view",
    )
    confirm = raw_requirement.create_raw_requirement(
        conn,
        project_id="demo",
        raw_text="Maybe recurring events too",
    )
    raw_requirement.update_status(
        conn,
        project_id="demo",
        raw_id=confirm["raw_id"],
        new_status=raw_requirement.STATUS_NEEDS_CONFIRMATION,
    )

    assert raw_requirement.lane_counts(conn, project_id="demo") == {
        raw_requirement.STATUS_RAW_INBOX: 1,
        raw_requirement.STATUS_NEEDS_CONFIRMATION: 1,
        raw_requirement.STATUS_PROMOTED: 0,
        raw_requirement.STATUS_DISMISSED: 0,
    }
    rows = raw_requirement.list_raw_requirements(
        conn,
        project_id="demo",
        status=[
            raw_requirement.STATUS_RAW_INBOX,
            raw_requirement.STATUS_NEEDS_CONFIRMATION,
        ],
    )
    assert {item["raw_id"] for item in rows} == {raw["raw_id"], confirm["raw_id"]}


def test_server_create_and_project_inbox_handlers_do_not_promote_backlog():
    from agent.governance import server

    conn = _conn()
    create_ctx = SimpleNamespace(
        path_params={"project_id": "demo"},
        query={},
        body={
            "raw_text": "Add a quick capture field",
            "source": "chat",
            "actor": "observer",
        },
        get_project_id=lambda: "demo",
    )
    with patch("agent.governance.server.get_connection", return_value=conn):
        code, payload = server.handle_project_raw_requirement_create(create_ctx)

    assert code == 201
    assert payload["created_backlog"] is False
    raw_id = payload["raw_requirement"]["raw_id"]

    status_ctx = SimpleNamespace(
        path_params={"project_id": "demo", "raw_id": raw_id},
        query={},
        body={"status": raw_requirement.STATUS_NEEDS_CONFIRMATION},
        get_project_id=lambda: "demo",
    )
    with patch("agent.governance.server.get_connection", return_value=conn):
        status_payload = server.handle_project_raw_requirement_status(status_ctx)

    assert status_payload["raw_requirement"]["status"] == raw_requirement.STATUS_NEEDS_CONFIRMATION

    inbox_ctx = SimpleNamespace(
        path_params={"project_id": "demo"},
        query={},
        body={},
        get_project_id=lambda: "demo",
    )
    with patch("agent.governance.server.get_connection", return_value=conn):
        inbox = server.handle_project_inbox(inbox_ctx)

    assert inbox["homepage_view"] == "project_inbox"
    assert inbox["lanes"]["needs_confirmation"]["count"] == 1
    assert inbox["lanes"]["ready_backlog"]["source"] == "backlog"


def test_project_inbox_groups_backlog_rows_into_operator_lanes():
    from agent.governance import server

    conn = _conn()
    conn.execute(
        """
        CREATE TABLE backlog_bugs (
            bug_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'OPEN',
            priority TEXT NOT NULL DEFAULT 'P3',
            target_files TEXT NOT NULL DEFAULT '[]',
            test_files TEXT NOT NULL DEFAULT '[]',
            acceptance_criteria TEXT NOT NULL DEFAULT '[]',
            chain_task_id TEXT NOT NULL DEFAULT '',
            "commit" TEXT NOT NULL DEFAULT '',
            fixed_at TEXT NOT NULL DEFAULT '',
            details_md TEXT NOT NULL DEFAULT '',
            chain_trigger_json TEXT NOT NULL DEFAULT '{}',
            required_docs TEXT NOT NULL DEFAULT '[]',
            provenance_paths TEXT NOT NULL DEFAULT '[]',
            chain_stage TEXT NOT NULL DEFAULT '',
            last_failure_reason TEXT NOT NULL DEFAULT '',
            runtime_state TEXT NOT NULL DEFAULT '',
            current_task_id TEXT NOT NULL DEFAULT '',
            root_task_id TEXT NOT NULL DEFAULT '',
            worktree_path TEXT NOT NULL DEFAULT '',
            worktree_branch TEXT NOT NULL DEFAULT '',
            mf_type TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    rows = [
        ("READY-1", "Ready backlog row", "OPEN", "", "", ""),
        ("WORK-1", "In-progress row", "OPEN", "manual_fix_in_progress", "", ""),
        ("REVIEW-1", "Needs review row", "OPEN", "blocked", "", "needs human"),
        ("DONE-1", "Done row", "FIXED", "", "", ""),
    ]
    for bug_id, title, status, runtime_state, chain_stage, failure_reason in rows:
        conn.execute(
            """
            INSERT INTO backlog_bugs (
                bug_id, title, status, priority, runtime_state, chain_stage,
                last_failure_reason, created_at, updated_at
            ) VALUES (?, ?, ?, 'P2', ?, ?, ?, '2026-05-28T00:00:00Z', '2026-05-28T00:00:00Z')
            """,
            (bug_id, title, status, runtime_state, chain_stage, failure_reason),
        )
    conn.commit()

    inbox_ctx = SimpleNamespace(
        path_params={"project_id": "demo"},
        query={},
        body={},
        get_project_id=lambda: "demo",
    )
    with patch("agent.governance.server.get_connection", return_value=conn):
        inbox = server.handle_project_inbox(inbox_ctx)

    assert inbox["lanes"]["ready_backlog"]["items"][0]["bug_id"] == "READY-1"
    assert inbox["lanes"]["in_progress"]["items"][0]["bug_id"] == "WORK-1"
    assert inbox["lanes"]["review_needed"]["items"][0]["bug_id"] == "REVIEW-1"
    assert inbox["lanes"]["done"]["items"][0]["bug_id"] == "DONE-1"
