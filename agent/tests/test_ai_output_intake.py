from __future__ import annotations

import sqlite3

import pytest

from agent.governance import ai_output_intake
from agent.governance import server
from agent.governance.db import _ensure_schema
from agent.governance.errors import ValidationError


PID = "ai-output-intake-test"


class _NoCloseConn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ctx(method: str, path_params: dict, body: dict | None = None, query: dict | None = None, idem_key: str = ""):
    return server.RequestContext(
        None,
        method,
        path_params,
        query or {},
        body or {},
        "req-ai-output-intake-test",
        "",
        idem_key,
    )


def _request(task_type: str = "graph_structure_ops") -> dict:
    return {
        "task_type": task_type,
        "snapshot_id": "scope-test",
        "base_commit": "abc1234",
        "target_type": "node",
        "target_id": "L7.1",
        "producer": "semantic-worker",
        "provider": "openai",
        "model": "gpt-5.4-mini",
        "payload": {
            "schema_version": "test.v1",
            "operations": [{"op": "add_edge", "from": "L7.1", "to": "L7.2"}],
        },
        "self_precheck": {"ok": True, "script": "validate_graph_structure_ops.py"},
        "graph_query_trace_ids": ["gq-test-1"],
        "metadata": {"batch_id": "batch-test"},
    }


def test_submit_ai_output_creates_output_event_and_queue():
    conn = _conn()

    result = ai_output_intake.submit_ai_output(
        conn,
        PID,
        _request(),
        actor="observer-test",
        request_id="req-test",
    )
    conn.commit()

    assert result["ok"] is True
    assert result["idempotent"] is False
    assert result["route_status"] == "queued"
    assert result["payload"]["operations"][0]["op"] == "add_edge"

    event_count = conn.execute("SELECT count(*) FROM ai_output_events").fetchone()[0]
    assert event_count == 1
    queue_row = conn.execute("SELECT * FROM ai_output_queue WHERE output_id = ?", (result["output_id"],)).fetchone()
    assert queue_row["status"] == "queued"
    assert queue_row["task_type"] == "graph_structure_ops"

    listed = ai_output_intake.list_ai_outputs(conn, PID, task_type="graph_structure_ops")
    assert [row["output_id"] for row in listed] == [result["output_id"]]
    fetched = ai_output_intake.get_ai_output(conn, PID, result["output_id"])
    assert fetched["graph_query_trace_ids"] == ["gq-test-1"]


def test_submit_ai_output_is_idempotent_by_explicit_key():
    conn = _conn()
    body = _request()

    first = ai_output_intake.submit_ai_output(conn, PID, body, idempotency_key="idem-explicit")
    second = ai_output_intake.submit_ai_output(conn, PID, body, idempotency_key="idem-explicit")

    assert second["idempotent"] is True
    assert second["output_id"] == first["output_id"]
    assert conn.execute("SELECT count(*) FROM ai_outputs").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM ai_output_queue").fetchone()[0] == 1


def test_chain_stage_result_is_reserved_and_does_not_complete_tasks():
    conn = _conn()

    result = ai_output_intake.submit_ai_output(
        conn,
        PID,
        {
            **_request("chain_stage_result"),
            "target_type": "task",
            "target_id": "task-1",
            "payload": {"status": "succeeded", "result": {"changed_files": ["x.py"]}},
        },
    )

    assert result["route_status"] == "reserved"
    assert conn.execute("SELECT status FROM ai_output_queue WHERE output_id = ?", (result["output_id"],)).fetchone()[0] == "reserved"
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_submit_ai_output_rejects_invalid_shape():
    conn = _conn()

    with pytest.raises(ValidationError):
        ai_output_intake.submit_ai_output(conn, PID, {"task_type": "unknown", "payload": {}})

    with pytest.raises(ValidationError):
        ai_output_intake.submit_ai_output(conn, PID, {"task_type": "semantic_node", "payload": []})


def test_ai_output_http_submit_list_get_and_queue(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(server, "get_connection", lambda _project_id: _NoCloseConn(conn))
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"principal_id": "observer-test", "role": "observer"},
    )

    submit_ctx = _ctx(
        "POST",
        {"project_id": PID},
        _request("graph_enrich_config_ops"),
        idem_key="http-idem",
    )
    code, submit_body = server.handle_ai_output_submit(submit_ctx)
    assert code == 201
    assert submit_body["ok"] is True

    replay_ctx = _ctx(
        "POST",
        {"project_id": PID},
        _request("graph_enrich_config_ops"),
        idem_key="http-idem",
    )
    code, replay_body = server.handle_ai_output_submit(replay_ctx)
    assert code == 200
    assert replay_body["output_id"] == submit_body["output_id"]

    list_body = server.handle_ai_output_list(
        _ctx("GET", {"project_id": PID}, query={"task_type": "graph_enrich_config_ops"})
    )
    assert list_body["count"] == 1
    assert list_body["outputs"][0]["output_id"] == submit_body["output_id"]

    get_body = server.handle_ai_output_get(
        _ctx("GET", {"project_id": PID, "output_id": submit_body["output_id"]})
    )
    assert get_body["output"]["payload"]["schema_version"] == "test.v1"

    queue_body = server.handle_ai_output_queue(_ctx("GET", {"project_id": PID}))
    assert queue_body["count"] == 1
    assert queue_body["queue"][0]["output_id"] == submit_body["output_id"]
