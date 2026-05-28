from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.governance import observer_session, raw_requirement


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    observer_session.ensure_schema(conn)
    raw_requirement.ensure_schema(conn)
    return conn


def _register(conn: sqlite3.Connection, project_id: str = "demo") -> dict:
    return observer_session.register_session(conn, project_id=project_id)


def test_command_enqueue_and_list_preserve_business_payload_in_db():
    conn = _conn()

    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_ANALYZE_REQUIREMENTS,
        payload={"raw_id": "raw-1", "source": "dashboard"},
        created_by="dashboard",
    )
    listed = observer_session.list_commands(conn, project_id="demo")

    assert command["status"] == observer_session.COMMAND_STATUS_QUEUED
    assert command["payload"] == {"raw_id": "raw-1", "source": "dashboard"}
    assert listed[0]["command_id"] == command["command_id"]
    assert listed[0]["payload"]["raw_id"] == "raw-1"
    assert observer_session.command_pending_reminder("demo") == {
        "kind": "observer_command_pending",
        "project_id": "demo",
        "message": "pending observer commands exist; call observer_command_next",
        "payload_included": False,
    }


def test_claim_requires_valid_token_and_project_match():
    conn = _conn()
    session = _register(conn)
    observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_CONFIRM_REQUIREMENT,
        payload={"raw_id": "raw-1"},
    )

    with pytest.raises(observer_session.ObserverAuthError):
        observer_session.claim_command(
            conn,
            project_id="demo",
            session_id=session["session_id"],
            session_token="wrong",
        )

    with pytest.raises(observer_session.ObserverPermissionError):
        observer_session.claim_command(
            conn,
            project_id="other",
            session_id=session["session_id"],
            session_token=session["session_token"],
        )


def test_claim_is_idempotent_for_same_session_and_rejects_double_claim():
    conn = _conn()
    session = _register(conn)
    other = _register(conn)
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_PAUSE_WORKER,
        payload={"task_id": "task-1"},
    )

    claimed = observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
    )
    repeated = observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
    )

    assert claimed["command"]["status"] == observer_session.COMMAND_STATUS_CLAIMED
    assert repeated["command"]["command_id"] == command["command_id"]

    with pytest.raises(observer_session.ObserverCommandConflict):
        observer_session.claim_command(
            conn,
            project_id="demo",
            session_id=other["session_id"],
            session_token=other["session_token"],
            command_id=command["command_id"],
        )


def test_complete_and_fail_require_same_claimed_session():
    conn = _conn()
    session = _register(conn)
    other = _register(conn)
    complete_command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_CONTINUE_WORKER,
        payload={"task_id": "task-1"},
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=complete_command["command_id"],
    )

    with pytest.raises(observer_session.ObserverPermissionError, match="same claimed session"):
        observer_session.complete_command(
            conn,
            project_id="demo",
            session_id=other["session_id"],
            session_token=other["session_token"],
            command_id=complete_command["command_id"],
            result={"ok": True},
        )

    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=complete_command["command_id"],
        result={"ok": True},
    )
    assert completed["command"]["status"] == observer_session.COMMAND_STATUS_COMPLETED

    fail_command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_CANCEL_WORKER,
        payload={"task_id": "task-2"},
    )
    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=other["session_id"],
        session_token=other["session_token"],
        command_id=fail_command["command_id"],
    )
    with pytest.raises(observer_session.ObserverPermissionError, match="same claimed session"):
        observer_session.fail_command(
            conn,
            project_id="demo",
            session_id=session["session_id"],
            session_token=session["session_token"],
            command_id=fail_command["command_id"],
            error="wrong owner",
        )

    failed = observer_session.fail_command(
        conn,
        project_id="demo",
        session_id=other["session_id"],
        session_token=other["session_token"],
        command_id=fail_command["command_id"],
        error="cancel rejected",
    )
    assert failed["command"]["status"] == observer_session.COMMAND_STATUS_FAILED
    assert failed["command"]["error"] == "cancel rejected"


def test_actor_self_report_does_not_authorize_command_claim():
    from agent.governance import server

    conn = _conn()
    ctx = SimpleNamespace(
        path_params={"project_id": "demo"},
        query={},
        body={"actor": "observer"},
        get_project_id=lambda: "demo",
    )

    with patch("agent.governance.server.get_connection", return_value=conn):
        code, payload = server.handle_observer_command_claim(ctx)

    assert code == 401
    assert payload["error"] == "observer_auth_failed"


def test_analyze_complete_projects_raw_requirement_to_confirmation():
    conn = _conn()
    session = _register(conn)
    raw = raw_requirement.create_raw_requirement(
        conn,
        project_id="demo",
        raw_text="Let users drag captured requirements into execution",
        source="dashboard",
    )
    command = observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_ANALYZE_REQUIREMENTS,
        payload={"raw_id": raw["raw_id"]},
        created_by="dashboard",
    )

    observer_session.claim_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
    )
    completed = observer_session.complete_command(
        conn,
        project_id="demo",
        session_id=session["session_id"],
        session_token=session["session_token"],
        command_id=command["command_id"],
        result={
            "raw_id": raw["raw_id"],
            "ai_interpretation": "User wants a queue promotion control.",
            "proposed_backlog_mapping": {
                "bug_id": "REQ-QUEUE-PROMOTE",
                "title": "Promote raw requirements to execution queue",
            },
        },
    )

    updated = raw_requirement.get_raw_requirement(conn, project_id="demo", raw_id=raw["raw_id"])
    assert completed["command"]["status"] == observer_session.COMMAND_STATUS_COMPLETED
    assert updated["status"] == raw_requirement.STATUS_NEEDS_CONFIRMATION
    assert "AI interpretation" in updated["note"]
    assert "REQ-QUEUE-PROMOTE" in updated["note"]


def test_api_smoke_capture_enqueue_claim_complete_reflects_project_inbox():
    from agent.governance import server

    conn = _conn()

    def ctx(path_params: dict, body: dict | None = None):
        return SimpleNamespace(
            path_params=path_params,
            query={},
            body=body or {},
            get_project_id=lambda: "demo",
        )

    with patch("agent.governance.server.get_connection", return_value=conn):
        register_code, register_payload = server.handle_observer_session_register(
            ctx(
                {"project_id": "demo"},
                {"observer_kind": "codex", "session_label": "smoke"},
            )
        )
        create_code, create_payload = server.handle_project_raw_requirement_create(
            ctx(
                {"project_id": "demo"},
                {
                    "raw_text": "Add one button that asks the observer to analyze this",
                    "source": "dashboard_project_inbox",
                    "actor": "dashboard",
                },
            )
        )
        raw_id = create_payload["raw_requirement"]["raw_id"]
        enqueue_code, enqueue_payload = server.handle_observer_command_enqueue(
            ctx(
                {"project_id": "demo"},
                {
                    "command_type": "analyze_requirements",
                    "payload": {"raw_id": raw_id, "source": "project_inbox"},
                    "created_by": "dashboard",
                },
            )
        )
        command_id = enqueue_payload["observer_command"]["command_id"]
        claim_payload = server.handle_observer_command_claim(
            ctx(
                {"project_id": "demo"},
                {
                    "session_id": register_payload["session_id"],
                    "session_token": register_payload["session_token"],
                    "command_id": command_id,
                },
            )
        )
        complete_payload = server.handle_observer_command_complete(
            ctx(
                {"project_id": "demo", "command_id": command_id},
                {
                    "session_id": register_payload["session_id"],
                    "session_token": register_payload["session_token"],
                    "result": {
                        "raw_id": raw_id,
                        "ai_interpretation": "User wants dashboard command queue wiring.",
                        "proposed_backlog_mapping": {
                            "bug_id": "SMOKE-OBSERVER-COMMAND",
                            "title": "Wire AI Analyze to observer command queue",
                        },
                    },
                },
            )
        )
        inbox = server.handle_project_inbox(ctx({"project_id": "demo"}))

    assert register_code == 201
    assert create_code == 201
    assert enqueue_code == 201
    assert claim_payload["command"]["status"] == observer_session.COMMAND_STATUS_CLAIMED
    assert complete_payload["command"]["status"] == observer_session.COMMAND_STATUS_COMPLETED
    assert inbox["lanes"]["raw_inbox"]["count"] == 0
    assert inbox["lanes"]["needs_confirmation"]["count"] == 1
    assert inbox["observer"]["connected"] is True
    assert inbox["observer_commands"]["counts"]["completed"] == 1
