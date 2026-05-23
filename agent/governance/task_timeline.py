"""Append-only task implementation timeline.

Backlog rows describe the intended work. Task timeline rows describe execution
facts proposed by agents, verified by executors/gates, and accepted by
observers. This module centralizes writes so parallel agents do not scatter
SQLite mutations across the codebase.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from typing import Any

log = logging.getLogger(__name__)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS task_timeline_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id           TEXT NOT NULL,
    backlog_id           TEXT NOT NULL DEFAULT '',
    mf_id                TEXT NOT NULL DEFAULT '',
    task_id              TEXT NOT NULL DEFAULT '',
    attempt_num          INTEGER NOT NULL DEFAULT 0,
    event_type           TEXT NOT NULL,
    actor                TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL DEFAULT '',
    payload_json         TEXT NOT NULL DEFAULT '{}',
    verification_json    TEXT NOT NULL DEFAULT '{}',
    artifact_refs_json   TEXT NOT NULL DEFAULT '{}',
    trace_id             TEXT NOT NULL DEFAULT '',
    commit_sha           TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_timeline_task
    ON task_timeline_events(project_id, task_id, attempt_num, id);
CREATE INDEX IF NOT EXISTS idx_task_timeline_backlog
    ON task_timeline_events(project_id, backlog_id, id);
CREATE INDEX IF NOT EXISTS idx_task_timeline_trace
    ON task_timeline_events(project_id, trace_id, id);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json(value: Any, default: Any) -> str:
    if value is None:
        value = default
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return json.dumps({"unserializable": repr(value)}, ensure_ascii=False)


def _insert_event(conn: sqlite3.Connection, event: dict[str, Any]) -> dict[str, Any]:
    from .db import sqlite_write_lock

    created_at = event.get("created_at") or _utc_iso()
    with sqlite_write_lock():
        cur = conn.execute(
            """INSERT INTO task_timeline_events
               (project_id, backlog_id, mf_id, task_id, attempt_num, event_type,
                actor, status, payload_json, verification_json,
                artifact_refs_json, trace_id, commit_sha, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.get("project_id", ""),
                event.get("backlog_id", ""),
                event.get("mf_id", ""),
                event.get("task_id", ""),
                int(event.get("attempt_num") or 0),
                event.get("event_type", ""),
                event.get("actor", ""),
                event.get("status", ""),
                _json(event.get("payload"), {}),
                _json(event.get("verification"), {}),
                _json(event.get("artifact_refs"), {}),
                event.get("trace_id", ""),
                event.get("commit_sha", ""),
                created_at,
            ),
        )
    return {
        "id": cur.lastrowid,
        "project_id": event.get("project_id", ""),
        "task_id": event.get("task_id", ""),
        "event_type": event.get("event_type", ""),
        "created_at": created_at,
    }


def record_event(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    event_type: str,
    task_id: str = "",
    backlog_id: str = "",
    mf_id: str = "",
    attempt_num: int = 0,
    actor: str = "",
    status: str = "",
    payload: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    artifact_refs: dict[str, Any] | None = None,
    trace_id: str = "",
    commit_sha: str = "",
) -> dict[str, Any]:
    """Append a timeline event using the caller's transaction."""

    if not project_id or not event_type:
        raise ValueError("project_id and event_type are required")
    return _insert_event(
        conn,
        {
            "project_id": project_id,
            "backlog_id": backlog_id,
            "mf_id": mf_id,
            "task_id": task_id,
            "attempt_num": attempt_num,
            "event_type": event_type,
            "actor": actor,
            "status": status,
            "payload": payload or {},
            "verification": verification or {},
            "artifact_refs": artifact_refs or {},
            "trace_id": trace_id,
            "commit_sha": commit_sha,
        },
    )


class _TimelineWriteQueue:
    """Small process-local serialized writer for executor-side evidence."""

    def __init__(self) -> None:
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure_started(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run,
                name="task-timeline-writer",
                daemon=True,
            )
            self._thread.start()

    def enqueue(self, event: dict[str, Any], *, wait: bool = True, timeout: float = 10.0) -> dict[str, Any]:
        if not event.get("project_id") or not event.get("event_type"):
            raise ValueError("project_id and event_type are required")
        self._ensure_started()
        done = threading.Event()
        item = {"event": event, "done": done, "result": None, "error": None}
        self._queue.put(item)
        if not wait:
            return {"queued": True}
        if not done.wait(timeout):
            raise TimeoutError("task timeline write queue timed out")
        if item["error"] is not None:
            raise item["error"]
        return item["result"] or {"queued": True}

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            event = item["event"]
            try:
                from .db import get_connection

                conn = get_connection(event["project_id"])
                try:
                    item["result"] = _insert_event(conn, event)
                    conn.commit()
                finally:
                    conn.close()
            except Exception as exc:
                log.debug("task timeline write failed", exc_info=True)
                item["error"] = exc
            finally:
                item["done"].set()
                self._queue.task_done()


_WRITE_QUEUE = _TimelineWriteQueue()


def enqueue_event(
    project_id: str,
    *,
    event_type: str,
    task_id: str = "",
    backlog_id: str = "",
    mf_id: str = "",
    attempt_num: int = 0,
    actor: str = "",
    status: str = "",
    payload: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    artifact_refs: dict[str, Any] | None = None,
    trace_id: str = "",
    commit_sha: str = "",
    wait: bool = True,
) -> dict[str, Any]:
    """Queue a timeline write from executor/worker code.

    The default waits until the event is durable. Callers that cannot block may
    set wait=False and accept best-effort delivery.
    """

    return _WRITE_QUEUE.enqueue(
        {
            "project_id": project_id,
            "backlog_id": backlog_id,
            "mf_id": mf_id,
            "task_id": task_id,
            "attempt_num": attempt_num,
            "event_type": event_type,
            "actor": actor,
            "status": status,
            "payload": payload or {},
            "verification": verification or {},
            "artifact_refs": artifact_refs or {},
            "trace_id": trace_id,
            "commit_sha": commit_sha,
        },
        wait=wait,
    )


def completion_verification(status: str, result: dict[str, Any] | None) -> dict[str, Any]:
    """Gate-style checks for task completion evidence.

    These checks do not prove correctness. They make implementation evidence
    explicit and machine-visible before later merge/review gates consume it.
    """

    result = result if isinstance(result, dict) else {}
    warnings: list[str] = []
    errors: list[str] = []

    changed_files = result.get("changed_files", [])
    if "changed_files" in result and not isinstance(changed_files, list):
        errors.append("changed_files must be a list when present")
    if status == "succeeded" and "changed_files" not in result:
        warnings.append("changed_files missing")

    evidence = result.get("implementation_evidence", [])
    if evidence and not isinstance(evidence, list):
        errors.append("implementation_evidence must be a list when present")
    elif status == "succeeded" and not evidence:
        warnings.append("implementation_evidence missing")

    self_check = result.get("self_check", {})
    if self_check and not isinstance(self_check, dict):
        errors.append("self_check must be an object when present")
    elif status == "succeeded" and not self_check:
        warnings.append("self_check missing")

    artifacts = result.get("_artifacts", {})
    if artifacts and not isinstance(artifacts, dict):
        errors.append("_artifacts must be an object when present")

    failure = result.get("failure") or {}
    if status in {"failed", "timed_out"} and not failure:
        warnings.append("synthetic failure envelope missing")

    return {
        "passed": not errors,
        "status": "passed" if not errors else "failed",
        "warnings": warnings,
        "errors": errors,
        "checks": {
            "has_structured_result": isinstance(result, dict),
            "has_changed_files": isinstance(changed_files, list),
            "has_implementation_evidence": isinstance(evidence, list) and bool(evidence),
            "has_self_check": isinstance(self_check, dict) and bool(self_check),
            "has_artifact_refs": isinstance(artifacts, dict) and bool(artifacts),
            "has_failure_envelope": bool(failure),
        },
    }


def synthetic_failure_envelope(
    *,
    failure_class: str,
    phase: str,
    summary: str,
    session_result: dict[str, Any] | None = None,
    retryable: bool = True,
    recommended_next_action: str = "retry_or_observer_takeover",
) -> dict[str, Any]:
    session_result = session_result if isinstance(session_result, dict) else {}
    return {
        "failure": {
            "failure_class": failure_class,
            "phase": phase,
            "summary": summary,
            "session_id": session_result.get("session_id", ""),
            "exit_code": session_result.get("exit_code"),
            "elapsed_sec": session_result.get("elapsed_sec"),
            "stdout_bytes": len(session_result.get("stdout", "") or ""),
            "stderr_bytes": len(session_result.get("stderr", "") or ""),
            "retryable": retryable,
            "recommended_next_action": recommended_next_action,
        }
    }


def list_events(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    task_id: str = "",
    backlog_id: str = "",
    trace_id: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if task_id:
        clauses.append("task_id = ?")
        params.append(task_id)
    if backlog_id:
        clauses.append("backlog_id = ?")
        params.append(backlog_id)
    if trace_id:
        clauses.append("trace_id = ?")
        params.append(trace_id)
    params.append(max(1, min(int(limit or 200), 1000)))
    rows = conn.execute(
        f"""SELECT * FROM task_timeline_events
            WHERE {' AND '.join(clauses)}
            ORDER BY id ASC
            LIMIT ?""",
        params,
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for key in ("payload_json", "verification_json", "artifact_refs_json"):
        try:
            result[key[:-5]] = json.loads(result.get(key) or "{}")
        except Exception:
            result[key[:-5]] = {}
    return result
