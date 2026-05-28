from __future__ import annotations

import sqlite3

import pytest

from agent.governance import observer_session


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    observer_session.ensure_schema(conn)
    return conn


def test_register_returns_token_once_and_stores_only_hash():
    conn = _conn()

    result = observer_session.register_session(
        conn,
        project_id="demo",
        observer_kind="codex",
        session_label="local observer",
        pid=123,
        cwd="/tmp/demo",
        now="2026-05-28T00:00:00Z",
    )

    session_id = result["observer_session_id"]
    token = result["session_token"]
    assert session_id
    assert token
    assert result["heartbeat_interval_sec"] == observer_session.HEARTBEAT_INTERVAL_SEC

    stored = conn.execute(
        "SELECT token_hash FROM observer_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    assert stored["token_hash"].startswith("sha256:")
    assert stored["token_hash"] != token
    assert token not in stored["token_hash"]

    fetched = observer_session.get_session(conn, project_id="demo", session_id=session_id)
    listed = observer_session.list_sessions(conn, project_id="demo")
    assert "session_token" not in fetched
    assert "token_hash" not in fetched
    assert "session_token" not in listed[0]
    assert "token_hash" not in listed[0]


def test_heartbeat_updates_last_seen_and_restores_active_status():
    conn = _conn()
    result = observer_session.register_session(
        conn,
        project_id="demo",
        now="2026-05-28T00:00:00Z",
    )

    heartbeat = observer_session.heartbeat_session(
        conn,
        project_id="demo",
        session_id=result["session_id"],
        session_token=result["session_token"],
        now="2026-05-28T00:01:00Z",
    )

    assert heartbeat["session"]["last_seen_at"] == "2026-05-28T00:01:00Z"
    assert heartbeat["session"]["computed_status"] == "active"


def test_stale_status_is_computed_from_last_seen():
    conn = _conn()
    result = observer_session.register_session(
        conn,
        project_id="demo",
        now="2026-05-28T00:00:00Z",
    )

    current = observer_session.get_session(
        conn,
        project_id="demo",
        session_id=result["session_id"],
        now="2026-05-28T00:00:30Z",
    )
    idle = observer_session.get_session(
        conn,
        project_id="demo",
        session_id=result["session_id"],
        now="2026-05-28T00:01:30Z",
    )
    stale = observer_session.get_session(
        conn,
        project_id="demo",
        session_id=result["session_id"],
        now="2026-05-28T00:03:00Z",
    )

    assert current["computed_status"] == "active"
    assert idle["computed_status"] == "idle"
    assert stale["computed_status"] == "stale"


def test_auth_rejects_wrong_token_and_wrong_project():
    conn = _conn()
    result = observer_session.register_session(conn, project_id="demo")

    with pytest.raises(observer_session.ObserverAuthError):
        observer_session.heartbeat_session(
            conn,
            project_id="demo",
            session_id=result["session_id"],
            session_token="wrong",
        )

    with pytest.raises(observer_session.ObserverPermissionError):
        observer_session.heartbeat_session(
            conn,
            project_id="other",
            session_id=result["session_id"],
            session_token=result["session_token"],
        )


def test_revoked_session_rejects_privileged_command_claim():
    conn = _conn()
    result = observer_session.register_session(conn, project_id="demo")
    observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_ANALYZE_REQUIREMENTS,
        payload={"raw_id": "raw-1"},
    )
    observer_session.revoke_session(
        conn,
        project_id="demo",
        session_id=result["session_id"],
        session_token=result["session_token"],
    )

    with pytest.raises(observer_session.ObserverPermissionError, match="revoked"):
        observer_session.claim_command(
            conn,
            project_id="demo",
            session_id=result["session_id"],
            session_token=result["session_token"],
        )


def test_stale_session_rejects_privileged_command_claim():
    conn = _conn()
    result = observer_session.register_session(
        conn,
        project_id="demo",
        now="2026-05-28T00:00:00Z",
    )
    observer_session.enqueue_command(
        conn,
        project_id="demo",
        command_type=observer_session.COMMAND_TYPE_ANALYZE_REQUIREMENTS,
        payload={"raw_id": "raw-1"},
        now="2026-05-28T00:00:01Z",
    )

    with pytest.raises(observer_session.ObserverPermissionError, match="stale"):
        observer_session.claim_command(
            conn,
            project_id="demo",
            session_id=result["session_id"],
            session_token=result["session_token"],
            now="2026-05-28T00:03:00Z",
        )
