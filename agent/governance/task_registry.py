"""Task Registry — SQLite-backed task lifecycle management (v5).

Dual-field state model:
  execution_status: queued → claimed → running → succeeded/failed/timed_out/cancelled
  notification_status: none → pending → sent → read

Supports: retry, priority, assignment, fencing token, progress heartbeat.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import nullcontext

log = logging.getLogger(__name__)


def _governance_write_lock():
    try:
        from .db import sqlite_write_lock

        return sqlite_write_lock()
    except Exception:
        return nullcontext()

# ---------------------------------------------------------------------------
# DB lock retry helper (B5)
# ---------------------------------------------------------------------------

_DB_LOCK_MAX_RETRIES = 3
_DB_LOCK_BASE_DELAY = 0.1  # seconds; backoff = base * 3^attempt


def _retry_on_db_lock(
    func,
    *args,
    _context: str = "",
    _conn: sqlite3.Connection | None = None,
    **kwargs,
):
    """Retry *func* on sqlite3.OperationalError('database is locked').

    Uses exponential backoff: 0.1s, 0.3s, 0.9s.
    Only retries on 'database is locked'; all other errors propagate immediately.
    """
    last_err: sqlite3.OperationalError | None = None
    for attempt in range(_DB_LOCK_MAX_RETRIES + 1):
        try:
            return func(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc):
                raise
            if _conn is not None:
                try:
                    _conn.rollback()
                except Exception:
                    pass
            last_err = exc
            if attempt < _DB_LOCK_MAX_RETRIES:
                delay = _DB_LOCK_BASE_DELAY * (3 ** attempt)
                log.warning(
                    "DB lock retry %d/%d (%s): waiting %.2fs — %s",
                    attempt + 1, _DB_LOCK_MAX_RETRIES, _context, delay, exc,
                )
                time.sleep(delay)
    # All retries exhausted
    raise last_err  # type: ignore[misc]


EXECUTION_STATUSES = {
    "queued", "claimed", "running", "waiting_human", "blocked",
    "succeeded", "failed", "cancelled", "timed_out", "enqueue_failed",
    "design_mismatch", "observer_hold",
}
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "timed_out", "design_mismatch"}

NOTIFICATION_STATUSES = {"none", "pending", "sent", "read"}

# R10: Accepted task types for create_task validation.
# 'reconcile' added in Phase J. Types starting with 'reconcile_' also accepted.
VALID_TASK_TYPES = {
    "task", "pm", "dev", "test", "qa", "gatekeeper", "merge", "deploy",
    "coordinator", "reconcile",
}

# Backward compat
VALID_STATUSES = EXECUTION_STATUSES


def _utc_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_task_id() -> str:
    return f"task-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _utc_iso_after(seconds: int) -> str:
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_metadata(raw: str | dict | None) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _mirror_backlog_runtime(
    conn: sqlite3.Connection,
    project_id: str,
    task_id: str,
    task_type: str,
    metadata_raw: str | dict | None,
    stage: str,
    *,
    runtime_state: str,
    failure_reason: str = "",
    result: dict | None = None,
) -> None:
    """Best-effort backlog runtime mirror for task_registry-only transitions."""
    if not project_id:
        return
    metadata = _parse_metadata(metadata_raw)
    bug_id = metadata.get("bug_id", "")
    if not bug_id:
        return
    try:
        from . import backlog_runtime

        backlog_runtime.update_backlog_runtime(
            conn,
            bug_id,
            stage,
            project_id=project_id,
            failure_reason=failure_reason,
            task_id=task_id,
            task_type=task_type or "task",
            metadata=metadata,
            result=result or {},
            runtime_state=runtime_state,
        )
    except Exception:
        log.debug(
            "task_registry: backlog runtime mirror failed for task=%s stage=%s",
            task_id,
            stage,
            exc_info=True,
        )


def _is_observer_mode(conn: sqlite3.Connection, project_id: str) -> bool:
    """Return True if observer_mode is enabled for this project."""
    try:
        row = conn.execute(
            "SELECT observer_mode FROM project_version WHERE project_id = ?", (project_id,)
        ).fetchone()
        return bool(row and row["observer_mode"])
    except Exception:
        return False


def _check_version_drift(conn: sqlite3.Connection, project_id: str) -> str | None:
    """Check if git HEAD differs from chain_version. Returns warning string or None.

    Advisory only — any exception is caught and returns None.
    """
    def _head_short() -> str:
        head_full = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode().strip()
        return head_full[:7]

    try:
        from .chain_trailer import get_chain_state

        chain_state = get_chain_state()
        chain_sha = (
            chain_state.get("chain_sha")
            or chain_state.get("version")
            or ""
        )
        if chain_sha and chain_sha != "unknown":
            chain_short = chain_sha[:7]
            head_short = _head_short()
            if head_short != chain_short:
                log.warning(
                    "HEAD (%s) != CHAIN_VERSION (%s, source=%s); auto_chain dispatch may be blocked",
                    head_short,
                    chain_short,
                    chain_state.get("source", "trailer"),
                )
                return (
                    f"HEAD ({head_short}) != CHAIN_VERSION ({chain_short}); "
                    f"auto_chain dispatch may be blocked"
                )
            return None
    except Exception:
        pass

    try:
        row = conn.execute(
            "SELECT chain_version FROM project_version WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if not row or not row["chain_version"]:
            return None
        chain_short = row["chain_version"][:7]

        head_short = _head_short()

        if head_short != chain_short:
            log.warning("HEAD (%s) != CHAIN_VERSION (%s); auto_chain dispatch may be blocked", head_short, chain_short)
            return (
                f"HEAD ({head_short}) != CHAIN_VERSION ({chain_short}); "
                f"auto_chain dispatch may be blocked"
            )
        return None
    except Exception:
        return None


def create_task(
    conn: sqlite3.Connection,
    project_id: str,
    prompt: str,
    task_type: str = "task",
    related_nodes: list[str] = None,
    created_by: str = "",
    priority: int = 0,
    max_attempts: int = 3,
    metadata: dict = None,
    parent_task_id: str = None,
    retry_round: int = 0,
    trace_id: str = None,
    chain_id: str = None,
) -> dict:
    """Create a new task. If observer_mode is on, task starts as observer_hold.

    R10: task_type must be in VALID_TASK_TYPES or start with 'reconcile_'.
    """
    # R10: validate task type
    if task_type not in VALID_TASK_TYPES and not task_type.startswith("reconcile_"):
        log.warning("create_task: unknown task_type '%s' (accepted: %s + reconcile_*)",
                     task_type, sorted(VALID_TASK_TYPES))

    task_id = _new_task_id()
    now = _utc_iso()

    # Auto-store original prompt and durable chain context for retry/restart
    # recovery.  Auto-chain historically carried chain_id mostly in dedicated
    # columns; graph event consumers read metadata_json, so mirror it here.
    metadata = dict(metadata or {})
    try:
        from .batch_jobs import normalize_job_metadata

        metadata = normalize_job_metadata(metadata, task_type=task_type)
    except Exception:
        # job_type is advisory metadata; never break legacy task creation.
        metadata = dict(metadata or {})
    metadata.setdefault("project_id", project_id)
    if parent_task_id:
        metadata.setdefault("parent_task_id", parent_task_id)
    if trace_id:
        metadata.setdefault("trace_id", trace_id)
    if chain_id:
        metadata.setdefault("chain_id", chain_id)
    if "_original_prompt" not in metadata:
        metadata["_original_prompt"] = prompt

    # Observer mode: new tasks start held, not queued
    initial_status = "observer_hold" if _is_observer_mode(conn, project_id) else "queued"

    notify = "pending" if metadata.get("chat_id") else "none"
    def _do_create_task():
        with _governance_write_lock():
            conn.execute(
                """INSERT INTO tasks
                   (task_id, project_id, status, execution_status, notification_status,
                    type, prompt, related_nodes,
                    created_by, created_at, updated_at, priority, max_attempts, metadata_json,
                    parent_task_id, retry_round, trace_id, chain_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id, project_id, initial_status, initial_status, notify,
                    task_type, prompt,
                    json.dumps(related_nodes or []),
                    created_by, now, now, priority, max_attempts,
                    json.dumps(metadata or {}),
                    parent_task_id, retry_round, trace_id, chain_id,
                ),
            )
            conn.commit()

    _retry_on_db_lock(_do_create_task, _context=f"create_task({task_id})", _conn=conn)

    log.info("Task created: %s (project: %s, type: %s, status: %s, retry_round: %d, trace_id: %s)",
             task_id, project_id, task_type, initial_status, retry_round, trace_id)

    result = {
        "task_id": task_id,
        "project_id": project_id,
        "status": initial_status,
        "type": task_type,
        "created_at": now,
        "observer_hold": initial_status == "observer_hold",
        "trace_id": trace_id,
        "chain_id": chain_id,
    }

    # Advisory version drift warning (B3) — never blocks task creation
    drift_warning = _check_version_drift(conn, project_id)
    if drift_warning:
        result["version_warning"] = drift_warning

    return result


def claim_task(
    conn: sqlite3.Connection,
    project_id: str,
    assigned_to: str,
    worker_id: str = "",
    caller_pid: int = 0,
) -> tuple[dict, str] | tuple[None, str]:
    """Claim the next available task with fencing token.

    Args:
        caller_pid: PID of the calling process (executor). Stored as worker_pid
                    for liveness checks during stale recovery. 0 = unknown.

    Returns (task_dict, fence_token) or (None, "") if no tasks.
    """
    def _do_claim():
        now = _utc_iso()
        with _governance_write_lock():
            row = conn.execute(
                """SELECT task_id, type, prompt, related_nodes, priority, attempt_count, max_attempts, metadata_json
                   FROM tasks
                   WHERE project_id = ? AND execution_status IN ('queued')
                   ORDER BY priority DESC, created_at ASC
                   LIMIT 1""",
                (project_id,),
            ).fetchone()

            if not row:
                return None, ""

            task_id = row["task_id"]
            attempt_num = row["attempt_count"] + 1
            fence_token = f"fence-{int(time.time())}-{uuid.uuid4().hex[:6]}"
            lease_expires = _utc_iso_after(300)  # 5 min lease

            # Use caller_pid if provided, otherwise fall back to current process PID
            effective_pid = caller_pid if caller_pid else os.getpid()

            stale_attempt_result = json.dumps({
                "error": "attempt_superseded",
                "reason": "Task was reclaimed after retry or executor recovery",
            }, ensure_ascii=False)

            # CAS update: only queued -> claimed. Clear terminal fields from a prior
            # retry/recovery so an active task cannot still look completed.
            result = conn.execute(
                """UPDATE tasks SET status = 'claimed', execution_status = 'claimed',
                   assigned_to = ?, started_at = ?, updated_at = ?, attempt_count = ?,
                   completed_at = NULL, result_json = NULL, error_message = '',
                   metadata_json = json_set(COALESCE(metadata_json, '{}'),
                     '$.fence_token', ?,
                     '$.lease_owner', ?,
                     '$.lease_expires_at', ?,
                     '$.worker_pid', ?
                   )
                   WHERE task_id = ? AND execution_status IN ('queued')""",
                (assigned_to, now, now, attempt_num,
                 fence_token, worker_id or assigned_to, lease_expires, str(effective_pid),
                 task_id),
            )
            if result.rowcount == 0:
                return None, ""  # Already claimed by another worker

            conn.execute(
                """UPDATE task_attempts SET status = 'failed',
                     completed_at = COALESCE(completed_at, ?),
                     result_json = COALESCE(result_json, ?),
                     error_message = COALESCE(error_message, ?)
                   WHERE task_id = ? AND status = 'running'""",
                (now, stale_attempt_result, "Task reclaimed by a new attempt", task_id),
            )
            conn.execute(
                """INSERT INTO task_attempts (task_id, attempt_num, status, started_at)
                   VALUES (?, ?, 'running', ?)""",
                (task_id, attempt_num, now),
            )
            conn.commit()

        log.info("task.claimed: %s type=%s by=%s attempt=%d fence=%s",
                 task_id, row["type"], worker_id or assigned_to, attempt_num, fence_token)
        return {
            "task_id": task_id,
            "type": row["type"],
            "prompt": row["prompt"],
            "related_nodes": json.loads(row["related_nodes"] or "[]"),
            "priority": row["priority"],
            "attempt_num": attempt_num,
            "metadata": json.loads(row["metadata_json"] or "{}"),
        }, fence_token

    claimed, fence_token = _retry_on_db_lock(
        _do_claim,
        _context=f"claim_task({project_id})",
        _conn=conn,
    )
    if claimed:
        try:
            from . import task_timeline

            metadata = claimed.get("metadata", {}) if isinstance(claimed, dict) else {}
            task_timeline.enqueue_event(
                project_id,
                task_id=claimed.get("task_id", ""),
                backlog_id=str(metadata.get("bug_id") or ""),
                mf_id=str(metadata.get("mf_id") or ""),
                attempt_num=int(claimed.get("attempt_num") or 0),
                event_type="task.claimed",
                actor=worker_id or assigned_to,
                status="claimed",
                payload={
                    "task_type": claimed.get("type", ""),
                    "worker_id": worker_id or assigned_to,
                    "caller_pid": caller_pid,
                    "fence_token_present": bool(fence_token),
                },
                trace_id=str(metadata.get("trace_id") or ""),
                wait=True,
            )
        except Exception:
            log.debug("task.claimed timeline write failed", exc_info=True)
    return claimed, fence_token


def complete_task(
    conn: sqlite3.Connection,
    task_id: str,
    status: str = "succeeded",
    result: dict = None,
    error_message: str = "",
    fence_token: str = "",
    project_id: str = "",
    completed_by: str = "",
    override_reason: str = "",
) -> dict:
    """Mark a task as completed (succeeded/failed). Dual-field update."""
    if status not in ("succeeded", "failed", "timed_out"):
        from .errors import ValidationError
        raise ValidationError(f"Invalid completion status: {status}")

    now = _utc_iso()
    row = conn.execute(
        "SELECT attempt_count, max_attempts, notification_status, metadata_json, assigned_to, type, "
        "status, completed_at, result_json, trace_id FROM tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()

    if not row:
        from .errors import GovernanceError
        raise GovernanceError(f"Task not found: {task_id}", 404)

    # M1: Ownership check — only assignee or observer can complete
    assigned_to = row["assigned_to"] or ""
    if completed_by and assigned_to and completed_by != assigned_to:
        is_observer = completed_by.startswith("observer")
        if not is_observer:
            from .errors import GovernanceError
            raise GovernanceError(
                f"Ownership violation: task assigned to {assigned_to}, "
                f"completed_by {completed_by}", 403)
        # M2: Observer override — allow but audit + warn
        log.warning("task_registry: observer override: %s completing task %s "
                     "assigned to %s (reason: %s)",
                     completed_by, task_id, assigned_to,
                     override_reason or "not provided")
        try:
            from . import event_bus, audit_service
            event_bus.publish("task.observer_override", {
                "project_id": project_id,
                "task_id": task_id,
                "assigned_to": assigned_to,
                "override_by": completed_by,
                "override_reason": override_reason,
            })
            audit_service.record(
                conn, project_id, "task.observer_override",
                actor=completed_by,
                details={
                    "task_id": task_id,
                    "assigned_to": assigned_to,
                    "override_reason": override_reason,
                },
            )
        except Exception:
            pass  # audit failure should not block completion

    # Fence token check (if provided)
    if fence_token:
        stored_fence = json.loads(row["metadata_json"] or "{}").get("fence_token", "")
        if stored_fence and stored_fence != fence_token:
            from .errors import GovernanceError
            raise GovernanceError("Fence token mismatch: task reclaimed by another worker", 409)

    current_status = row["status"] or ""
    task_type_val = row["type"] if row["type"] else ""
    if current_status in TERMINAL_STATUSES:
        response = {
            "task_id": task_id,
            "status": current_status,
            "retrying": False,
            "completed_at": row["completed_at"] or now,
            "idempotent": True,
        }
        if override_reason == "replay_auto_chain" and project_id:
            meta = json.loads(row["metadata_json"] or "{}")
            stored_result = result
            if stored_result is None:
                try:
                    stored_result = json.loads(row["result_json"] or "{}")
                except Exception:
                    stored_result = {}
            # Observer replay can emit audit rows above. Release this request's
            # transaction before auto-chain opens its own connection, otherwise
            # SQLite can self-lock while persisting chain_context events.
            conn.commit()
            if current_status == "succeeded":
                chain_result = _dispatch_auto_chain_success(
                    project_id, task_id, task_type_val, current_status, stored_result or {}, meta
                )
            else:
                chain_result = _dispatch_auto_chain_failed(
                    project_id, task_id, task_type_val, stored_result or {}, meta,
                    error_message or (stored_result or {}).get("error", ""),
                )
            response["auto_chain"] = _build_auto_chain_response(chain_result)
            response["replayed_auto_chain"] = True
        else:
            response["auto_chain"] = {
                "dispatched": False,
                "reason": "task already terminal",
            }
        return response

    # Determine execution status
    exec_status = status
    if status == "failed" and row["attempt_count"] < row["max_attempts"]:
        if _is_observer_mode(conn, project_id):
            exec_status = "observer_hold"  # Auto-retry but hold for observer
        else:
            exec_status = "queued"  # Auto-retry

    # Determine notification status
    notify_status = row["notification_status"]
    if exec_status in TERMINAL_STATUSES and notify_status == "none":
        # Has chat_id → needs notification
        meta = json.loads(row["metadata_json"] or "{}")
        if meta.get("chat_id"):
            # If executor already sent the reply directly (coordinator flow),
            # mark as "sent" to prevent gateway from sending a duplicate.
            if (result or {}).get("_reply_sent"):
                notify_status = "sent"
            else:
                notify_status = "pending"

    # --- Test report gate: reject test-type succeeded without valid test_report ---
    if task_type_val == "test" and status == "succeeded":
        result_obj = result or {}
        test_report = result_obj.get("test_report")
        if (
            not isinstance(test_report, dict)
            or "passed" not in test_report
            or "failed" not in test_report
        ):
            from .errors import ValidationError
            raise ValidationError(
                "Test task succeeded without valid test_report. "
                "Result must contain test_report dict with 'passed' and 'failed' integer keys.",
                details={"task_id": task_id, "result_keys": list(result_obj.keys())},
            )

    task_is_terminal = exec_status in TERMINAL_STATUSES
    task_completed_at = now if task_is_terminal else None
    task_result_json = (
        json.dumps(result or {}, ensure_ascii=False) if task_is_terminal else None
    )
    task_error_message = error_message if task_is_terminal else ""
    attempt_result_json = json.dumps(result or {}, ensure_ascii=False)

    def _do_complete_updates():
        conn.execute(
            """UPDATE tasks SET status = ?, execution_status = ?,
               notification_status = ?,
               completed_at = ?, updated_at = ?,
               result_json = ?, error_message = ?
               WHERE task_id = ?""",
            (exec_status, exec_status, notify_status,
             task_completed_at, now,
             task_result_json, task_error_message,
             task_id),
        )
        conn.execute(
            """UPDATE task_attempts SET status = ?, completed_at = ?,
               result_json = ?, error_message = ?
               WHERE task_id = ? AND attempt_num = ? AND status = 'running'""",
            (status, now, attempt_result_json, error_message,
             task_id, int(row["attempt_count"] or 0)),
        )
        try:
            from . import task_timeline

            meta = json.loads(row["metadata_json"] or "{}")
            attempt_num = int(row["attempt_count"] or 0)
            actor = completed_by or assigned_to or meta.get("lease_owner", "")
            task_timeline.record_event(
                conn,
                project_id=project_id,
                task_id=task_id,
                backlog_id=str(meta.get("bug_id") or ""),
                mf_id=str(meta.get("mf_id") or ""),
                attempt_num=attempt_num,
                event_type="gate.evidence.verified",
                actor=actor,
                status="passed" if task_timeline.completion_verification(status, result).get("passed") else "failed",
                payload={
                    "task_type": task_type_val,
                    "completion_status": status,
                    "worker_id": completed_by,
                    "fence_token_present": bool(fence_token),
                    "result_keys": sorted((result or {}).keys()) if isinstance(result, dict) else [],
                },
                verification=task_timeline.completion_verification(status, result),
                artifact_refs=(result or {}).get("_artifacts", {}) if isinstance(result, dict) else {},
                trace_id=str(row["trace_id"] or meta.get("trace_id") or ""),
            )
            task_timeline.record_event(
                conn,
                project_id=project_id,
                task_id=task_id,
                backlog_id=str(meta.get("bug_id") or ""),
                mf_id=str(meta.get("mf_id") or ""),
                attempt_num=attempt_num,
                event_type="task.completed",
                actor=actor,
                status=status,
                payload={
                    "task_type": task_type_val,
                    "execution_status": exec_status,
                    "retrying": exec_status == "queued",
                    "worker_id": completed_by,
                    "fence_token_present": bool(fence_token),
                },
                artifact_refs=(result or {}).get("_artifacts", {}) if isinstance(result, dict) else {},
                trace_id=str(row["trace_id"] or meta.get("trace_id") or ""),
            )
        except Exception:
            log.debug("task.complete timeline write failed", exc_info=True)

    with _governance_write_lock():
        _retry_on_db_lock(
            _do_complete_updates,
            _context=f"complete_task({task_id})",
            _conn=conn,
        )

        result_summary = str(result)[:200] if result else "{}"
        log.info("task.complete: %s status=%s exec_status=%s by=%s result=%s",
                 task_id, status, exec_status, completed_by or assigned_to, result_summary)

        response = {
            "task_id": task_id,
            "status": exec_status,
            "retrying": exec_status == "queued",
            "completed_at": now,
        }
        failure_reason = error_message or (result or {}).get("error", "")
        mirror_task_type = task_type_val or "task"
        if status == "failed" and exec_status in ("queued", "observer_hold"):
            _mirror_backlog_runtime(
                conn,
                project_id,
                task_id,
                mirror_task_type,
                row["metadata_json"],
                f"{mirror_task_type}_{exec_status}",
                runtime_state=exec_status,
                failure_reason=failure_reason,
                result=result or {},
            )
        elif exec_status in TERMINAL_STATUSES and exec_status != "succeeded":
            _mirror_backlog_runtime(
                conn,
                project_id,
                task_id,
                mirror_task_type,
                row["metadata_json"],
                f"{mirror_task_type}_{exec_status}",
                runtime_state=exec_status,
                failure_reason=failure_reason,
                result=result or {},
            )

        # --- Subtask fan-in check (R6): when a merge task in a subtask group succeeds ---
        if exec_status == "succeeded" and task_type_val == "merge":
            try:
                _check_subtask_fanin(conn, project_id, task_id)
            except Exception:
                log.error("subtask fan-in check failed for %s", task_id, exc_info=True)

        # --- Subtask failure cascade (R9): terminal failure in a subtask chain ---
        if exec_status in TERMINAL_STATUSES and exec_status != "succeeded":
            try:
                _check_subtask_failure_cascade(conn, project_id, task_id, row)
            except Exception:
                log.error("subtask failure cascade check failed for %s", task_id, exc_info=True)
        conn.commit()

    # Auto-chain: dispatch next stage asynchronously on success/failure.
    # Non-chain types (task, coordinator) are ignored by auto_chain.CHAIN
    # so they pass through without spawning a thread.
    # Cancelled: terminal, no auto-chain, no retry.
    if exec_status == "cancelled":
        pass  # skip auto-chain
    elif status == "failed" and project_id:
        meta = json.loads(row["metadata_json"] or "{}")
        type_row = conn.execute(
            "SELECT type FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        task_type = type_row["type"] if type_row else "task"
        conn.commit()
        # AC1: Run dispatch and reflect actual result in response
        chain_result = _dispatch_auto_chain_failed(
            project_id, task_id, task_type,
            result or {}, meta,
            error_message or (result or {}).get("error", ""),
        )
        response["auto_chain"] = _build_auto_chain_response(chain_result)
    elif exec_status == "succeeded" and project_id:
        meta = json.loads(row["metadata_json"] or "{}")
        type_row = conn.execute(
            "SELECT type FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        task_type = type_row["type"] if type_row else "task"
        # Commit before auto_chain opens its independent connection.
        # Without this, the caller's open transaction holds a write lock and
        # auto_chain's separate conn fails with "database is locked".
        conn.commit()
        # AC1: Run dispatch and reflect actual result in response
        chain_result = _dispatch_auto_chain_success(
            project_id, task_id, task_type,
            exec_status, result or {}, meta,
        )
        response["auto_chain"] = _build_auto_chain_response(chain_result)

    return response


# ---------------------------------------------------------------------------
# Auto-chain dispatch helpers
# ---------------------------------------------------------------------------

def _build_auto_chain_response(chain_result: dict | None) -> dict:
    """Build the auto_chain response dict from chain dispatch result.

    AC1/AC2: Response reflects actual dispatch outcome, not assumed success.
    """
    if chain_result is None:
        # Not a chain-eligible task or non-succeeded status
        return {"dispatched": False}
    if chain_result.get("preflight_blocked"):
        response = {
            "dispatched": False,
            "preflight_blocked": True,
            "stage": chain_result.get("stage", "preflight"),
            "reason": chain_result.get("reason", "unknown"),
        }
        if chain_result.get("queue_outcome") is not None:
            response["queue_outcome"] = chain_result.get("queue_outcome")
        return response
    if chain_result.get("gate_blocked"):
        return {
            "dispatched": False,
            "gate_blocked": True,
            "gate_reason": chain_result.get("reason", "unknown"),
        }
    if chain_result.get("routing_blocked"):
        return {
            "dispatched": False,
            "routing_blocked": True,
            "reason": chain_result.get("reason", "unknown"),
        }
    if chain_result.get("chain_stopped"):
        return {"dispatched": False, "chain_stopped": True, "reason": chain_result.get("reason", "")}
    if chain_result.get("task_id"):
        return {
            "dispatched": True,
            "task_id": chain_result.get("task_id"),
            "type": chain_result.get("type"),
            "dedup": bool(chain_result.get("dedup")),
        }
    if chain_result.get("next_task_id"):
        return {
            "dispatched": True,
            "task_id": chain_result.get("next_task_id"),
            "type": chain_result.get("type"),
        }
    # Successful dispatch
    return {"dispatched": True}


def _dispatch_auto_chain_success(
    project_id: str,
    task_id: str,
    task_type: str,
    exec_status: str,
    result: dict,
    metadata: dict,
) -> dict | None:
    """Fire auto_chain.on_task_completed synchronously.

    AC1: Returns the chain result so the caller can reflect it in the response.
    Errors are logged (not swallowed) so they appear in service logs.
    """
    try:
        from . import auto_chain
        from .db import get_connection

        def _do_chain_success():
            conn = get_connection(project_id)
            # Unit tests may patch get_connection with a caller-owned handle.
            should_close = not hasattr(get_connection, "mock_calls")
            try:
                return auto_chain.on_task_completed(
                    conn, project_id, task_id,
                    task_type=task_type,
                    status=exec_status,
                    result=result,
                    metadata=metadata,
                )
            finally:
                if should_close:
                    conn.close()

        return _retry_on_db_lock(
            _do_chain_success,
            _context=f"auto_chain_success({task_id})",
        )
    except Exception:
        log.error(
            "auto_chain.on_task_completed failed for task %s (project=%s, type=%s)",
            task_id, project_id, task_type,
            exc_info=True,
        )
        # Z0-sequel observer-hotfix 2026-04-24: on Windows governance, stdout
        # goes nowhere (no redirect in scripts/start-governance.ps1). Without
        # this file-log, silent-drop exceptions are invisible. See
        # handoff-2026-04-24-post-z0.md §3 + project_auto_chain_silent_dispatch_drop.md.
        try:
            import traceback as _tb
            _log_path = os.path.join(
                "shared-volume", "codex-tasks", "logs", "auto-chain-errors.log",
            )
            os.makedirs(os.path.dirname(_log_path), exist_ok=True)
            with open(_log_path, "a", encoding="utf-8") as _f:
                _f.write(
                    f"\n=== {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
                    f"task={task_id} project={project_id} type={task_type} "
                    f"path=success ===\n"
                )
                _tb.print_exc(file=_f)
        except Exception:
            pass
        return None


def _dispatch_auto_chain_failed(
    project_id: str,
    task_id: str,
    task_type: str,
    result: dict,
    metadata: dict,
    reason: str,
) -> dict | None:
    """Fire auto_chain.on_task_failed synchronously.

    AC1: Returns the chain result so the caller can reflect it in the response.
    Errors are logged (not swallowed) so they appear in service logs.
    """
    try:
        from . import auto_chain
        from .db import get_connection

        def _do_chain_failed():
            conn = get_connection(project_id)
            # Unit tests may patch get_connection with a caller-owned handle.
            should_close = not hasattr(get_connection, "mock_calls")
            try:
                return auto_chain.on_task_failed(
                    conn, project_id, task_id,
                    task_type=task_type,
                    result=result,
                    metadata=metadata,
                    reason=reason,
                )
            finally:
                if should_close:
                    conn.close()

        return _retry_on_db_lock(
            _do_chain_failed,
            _context=f"auto_chain_failed({task_id})",
        )
    except Exception:
        log.error(
            "auto_chain.on_task_failed failed for task %s (project=%s, type=%s)",
            task_id, project_id, task_type,
            exc_info=True,
        )
        try:
            import traceback as _tb
            _log_path = os.path.join(
                "shared-volume", "codex-tasks", "logs", "auto-chain-errors.log",
            )
            os.makedirs(os.path.dirname(_log_path), exist_ok=True)
            with open(_log_path, "a", encoding="utf-8") as _f:
                _f.write(
                    f"\n=== {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
                    f"task={task_id} project={project_id} type={task_type} "
                    f"path=failed ===\n"
                )
                _tb.print_exc(file=_f)
        except Exception:
            pass
        return None


def hold_task(conn: sqlite3.Connection, task_id: str) -> dict:
    """Put a queued task into observer_hold — pauses auto-chain and executor pickup."""
    now = _utc_iso()
    conn.execute(
        """UPDATE tasks SET status = 'observer_hold', execution_status = 'observer_hold',
           updated_at = ? WHERE task_id = ? AND execution_status = 'queued'""",
        (now, task_id),
    )
    if conn.total_changes == 0:
        row = conn.execute("SELECT execution_status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        current = row["execution_status"] if row else "not found"
        raise ValueError(f"Task {task_id} cannot be held (current status: {current})")
    log.info("Task held by observer: %s", task_id)
    return {"task_id": task_id, "status": "observer_hold"}


def release_task(conn: sqlite3.Connection, task_id: str) -> dict:
    """Release an observer_hold task back to queued — resumes auto-chain and executor."""
    now = _utc_iso()
    conn.execute(
        """UPDATE tasks SET status = 'queued', execution_status = 'queued',
           updated_at = ? WHERE task_id = ? AND execution_status = 'observer_hold'""",
        (now, task_id),
    )
    if conn.total_changes == 0:
        row = conn.execute("SELECT execution_status FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        current = row["execution_status"] if row else "not found"
        raise ValueError(f"Task {task_id} cannot be released (current status: {current})")
    log.info("Task released by observer: %s", task_id)
    return {"task_id": task_id, "status": "queued"}


def set_observer_mode(conn: sqlite3.Connection, project_id: str, enabled: bool) -> dict:
    """Enable or disable observer_mode for a project."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """UPDATE project_version SET observer_mode = ?, updated_at = ?
           WHERE project_id = ?""",
        (1 if enabled else 0, now, project_id),
    )
    log.info("Observer mode %s for project %s", "enabled" if enabled else "disabled", project_id)
    return {"project_id": project_id, "observer_mode": enabled}


def get_observer_mode(conn: sqlite3.Connection, project_id: str) -> bool:
    """Return current observer_mode flag for a project."""
    return _is_observer_mode(conn, project_id)


def cancel_task(
    conn: sqlite3.Connection,
    task_id: str,
    reason: str = "",
    *,
    project_id: str = "",
) -> dict:
    """Cancel a task. Terminal state — no auto-chain, no retry."""
    now = _utc_iso()
    row = conn.execute(
        "SELECT status, type, metadata_json FROM tasks WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if not row:
        from .errors import GovernanceError
        raise GovernanceError(f"Task not found: {task_id}", 404)
    cancel_reason = reason or "Cancelled by observer"
    cancel_result = json.dumps({
        "error": "cancelled",
        "reason": cancel_reason,
    }, ensure_ascii=False)
    conn.execute(
        """UPDATE tasks SET status = 'cancelled', execution_status = 'cancelled',
           completed_at = ?, updated_at = ?, result_json = COALESCE(result_json, ?),
           error_message = ?
           WHERE task_id = ?""",
        (now, now, cancel_result, cancel_reason, task_id),
    )
    conn.execute(
        """UPDATE task_attempts SET status = 'cancelled', completed_at = ?,
             result_json = COALESCE(result_json, ?),
             error_message = COALESCE(error_message, ?)
           WHERE task_id = ? AND status = 'running'""",
        (now, cancel_result, cancel_reason, task_id),
    )
    metadata = _parse_metadata(row["metadata_json"])
    parsed_cancel_result = json.loads(cancel_result)
    _mirror_backlog_runtime(
        conn,
        project_id,
        task_id,
        row["type"] or "task",
        metadata,
        f"{row['type'] or 'task'}_cancelled",
        runtime_state="cancelled",
        failure_reason=cancel_reason,
        result=parsed_cancel_result,
    )
    if project_id:
        try:
            from . import auto_chain
            auto_chain.on_task_completed(
                conn, project_id, task_id,
                task_type=row["type"] or "task",
                status="cancelled",
                result=parsed_cancel_result,
                metadata=metadata,
            )
        except Exception:
            log.debug("task.cancelled: auto_chain cancel hook failed for %s",
                      task_id, exc_info=True)
    log.info("task.cancelled: %s reason=%s", task_id, cancel_reason)
    return {"task_id": task_id, "status": "cancelled"}


def mark_notified(conn: sqlite3.Connection, task_id: str) -> dict:
    """Mark a task's notification as sent."""
    now = _utc_iso()
    conn.execute(
        "UPDATE tasks SET notification_status = 'sent', notified_at = ? WHERE task_id = ?",
        (now, task_id),
    )
    return {"task_id": task_id, "notification_status": "sent"}


def list_pending_notifications(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    """List tasks that need notification (execution done but user not notified)."""
    rows = conn.execute(
        """SELECT task_id, execution_status, result_json, error_message,
                  completed_at, metadata_json
           FROM tasks
           WHERE project_id = ? AND notification_status = 'pending'
             AND execution_status IN ('succeeded', 'failed', 'timed_out', 'cancelled')
           ORDER BY completed_at ASC""",
        (project_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_progress(conn: sqlite3.Connection, task_id: str,
                    phase: str, percent: int, message: str) -> dict:
    """Update task progress heartbeat."""
    now = _utc_iso()
    conn.execute(
        """UPDATE tasks SET
           execution_status = 'running',
           updated_at = ?,
           metadata_json = json_set(COALESCE(metadata_json, '{}'),
             '$.progress_phase', ?,
             '$.progress_percent', ?,
             '$.progress_message', ?,
             '$.progress_at', ?,
             '$.lease_expires_at', ?
           )
           WHERE task_id = ? AND execution_status IN ('claimed', 'running')""",
        (now, phase, percent, message, now, _utc_iso_after(300), task_id),
    )
    return {"task_id": task_id, "phase": phase, "percent": percent}


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running.

    Uses a Windows-safe process handle query on Windows, otherwise
    os.kill(pid, 0), which checks existence without sending a signal.
    Returns False for pid <= 0.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            process_query_limited_information = 0x1000
            still_active = 259
            handle = kernel32.OpenProcess(
                process_query_limited_information,
                False,
                int(pid),
            )
            if not handle:
                # Access denied means the process exists but is not queryable by
                # this user; any other error is treated as not alive.
                return ctypes.get_last_error() == 5
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return int(exit_code.value) == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't have permission to signal it
        return True
    except (OSError, SystemError):
        # observer-hotfix 2026-04-25: Windows os.kill on a stale PID can raise
        # SystemError ("returned a result with an exception set") instead of OSError.
        # Treat both as "process dead" rather than crashing the request handler.
        return False


def recover_stale_tasks(conn: sqlite3.Connection, project_id: str) -> dict:
    """Recover tasks with expired leases or dead worker PIDs — re-queue them.

    Phase 1: Re-queue tasks with expired leases (original behavior).
    Phase 2: Check worker_pid liveness for claimed tasks with unexpired leases.
              Re-queue if the worker PID is dead. Skip if pid=0 (unknown).
    """
    now = _utc_iso()

    # Phase 1: Expired lease recovery
    rows = conn.execute(
        """SELECT task_id, type, metadata_json FROM tasks
           WHERE project_id = ? AND execution_status IN ('claimed', 'running')
             AND json_extract(metadata_json, '$.lease_expires_at') < ?""",
        (project_id, now),
    ).fetchall()

    recovered = 0
    for row in rows:
        recovery_result = json.dumps({
            "error": "executor_crash_recovery",
            "reason": "Lease expired while task was claimed/running",
        }, ensure_ascii=False)
        conn.execute(
            """UPDATE task_attempts SET status = 'failed',
                 completed_at = COALESCE(completed_at, ?),
                 result_json = COALESCE(result_json, ?),
                 error_message = COALESCE(error_message, ?)
               WHERE task_id = ? AND status = 'running'""",
            (now, recovery_result, "Lease expired during execution",
             row["task_id"]),
        )
        conn.execute(
            """UPDATE tasks SET execution_status = 'queued', status = 'queued',
                 completed_at = NULL, result_json = NULL, error_message = '',
                 updated_at = ?
               WHERE task_id = ?""",
            (now, row["task_id"]),
        )
        _mirror_backlog_runtime(
            conn,
            project_id,
            row["task_id"],
            row["type"] or "task",
            row["metadata_json"],
            f"{row['type'] or 'task'}_queued",
            runtime_state="queued",
            failure_reason="Lease expired during execution",
            result=json.loads(recovery_result),
        )
        recovered += 1
        log.info("Recovered stale task (expired lease): %s", row["task_id"])

    # Phase 2: PID liveness check for claimed tasks with valid leases
    live_rows = conn.execute(
        """SELECT task_id, type, metadata_json, json_extract(metadata_json, '$.worker_pid') as worker_pid
           FROM tasks
           WHERE project_id = ? AND execution_status IN ('claimed', 'running')
             AND (json_extract(metadata_json, '$.lease_expires_at') >= ? OR
                  json_extract(metadata_json, '$.lease_expires_at') IS NULL)""",
        (project_id, now),
    ).fetchall()

    pid_recovered = 0
    for row in live_rows:
        raw_pid = row["worker_pid"]
        if not raw_pid:
            continue  # No PID recorded, skip
        try:
            pid = int(raw_pid)
        except (ValueError, TypeError):
            continue
        if pid == 0:
            continue  # Unknown PID, skip
        if not _is_pid_alive(pid):
            recovery_result = json.dumps({
                "error": "executor_crash_recovery",
                "reason": f"Worker PID {pid} died while task was claimed/running",
            }, ensure_ascii=False)
            conn.execute(
                """UPDATE task_attempts SET status = 'failed',
                     completed_at = COALESCE(completed_at, ?),
                     result_json = COALESCE(result_json, ?),
                     error_message = COALESCE(error_message, ?)
                   WHERE task_id = ? AND status = 'running'""",
                (now, recovery_result, f"Worker PID {pid} is not alive",
                 row["task_id"]),
            )
            conn.execute(
                """UPDATE tasks SET execution_status = 'queued', status = 'queued',
                     completed_at = NULL, result_json = NULL, error_message = '',
                     updated_at = ?
                   WHERE task_id = ?""",
                (now, row["task_id"]),
            )
            _mirror_backlog_runtime(
                conn,
                project_id,
                row["task_id"],
                row["type"] or "task",
                row["metadata_json"],
                f"{row['type'] or 'task'}_queued",
                runtime_state="queued",
                failure_reason=f"Worker PID {pid} is not alive",
                result=json.loads(recovery_result),
            )
            pid_recovered += 1
            log.info("Recovered stale task (dead PID %d): %s", pid, row["task_id"])

    return {"recovered": recovered + pid_recovered, "expired_lease": recovered, "dead_pid": pid_recovered}


def list_tasks(
    conn: sqlite3.Connection,
    project_id: str,
    status: str = None,
    limit: int = 50,
) -> list[dict]:
    """List tasks for a project."""
    cols = """task_id, status, type, prompt, assigned_to, created_by,
                      created_at, updated_at, attempt_count, priority,
                      result_json, metadata_json"""
    if status:
        rows = conn.execute(
            f"""SELECT {cols}
               FROM tasks WHERE project_id = ? AND status = ?
               ORDER BY updated_at DESC LIMIT ?""",
            (project_id, status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""SELECT {cols}
               FROM tasks WHERE project_id = ?
               ORDER BY updated_at DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        # Parse JSON fields for API consumers
        for field in ("result_json", "metadata_json"):
            raw = d.get(field)
            if raw and isinstance(raw, str):
                try:
                    d[field.replace("_json", "")] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    d[field.replace("_json", "")] = raw
            else:
                d[field.replace("_json", "")] = raw
        results.append(d)
    return results


def get_task(conn: sqlite3.Connection, task_id: str) -> dict | None:
    """Get a single task with attempts."""
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    if not row:
        return None

    task = dict(row)
    attempts = conn.execute(
        "SELECT * FROM task_attempts WHERE task_id = ? ORDER BY attempt_num",
        (task_id,),
    ).fetchall()
    task["attempts"] = [dict(a) for a in attempts]
    return task


def _check_subtask_fanin(conn, project_id, task_id):
    """Check if a completed merge task belongs to a subtask group and trigger fan-in."""
    # Walk up from merge → find the dev task with subtask_group_id
    meta_row = conn.execute(
        "SELECT metadata_json FROM tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if not meta_row:
        return
    meta = json.loads(meta_row["metadata_json"] or "{}")
    parent_id = meta.get("parent_task_id")

    # Walk the chain up to find the dev task with subtask_group_id
    visited = set()
    current_id = parent_id
    for _ in range(10):
        if not current_id or current_id in visited:
            break
        visited.add(current_id)
        row = conn.execute(
            "SELECT subtask_group_id, subtask_local_id, metadata_json FROM tasks WHERE task_id=?",
            (current_id,)
        ).fetchone()
        if not row:
            break
        if row["subtask_group_id"]:
            from . import auto_chain
            auto_chain.on_subtask_merge_completed(conn, project_id, current_id)
            return
        parent_meta = json.loads(row["metadata_json"] or "{}")
        current_id = parent_meta.get("parent_task_id")


def _check_subtask_failure_cascade(conn, project_id, task_id, task_row):
    """Check if a terminally-failed task belongs to a subtask group and trigger cascade."""
    # Check if this task itself has subtask_group_id
    try:
        row = conn.execute(
            "SELECT subtask_group_id FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        if row and row["subtask_group_id"]:
            # Check if max retries exhausted
            attempt_count = task_row["attempt_count"] if task_row else 0
            max_attempts = task_row["max_attempts"] if task_row else 3
            if attempt_count >= max_attempts:
                from . import auto_chain
                auto_chain.on_subtask_terminal_failure(conn, project_id, task_id)
                return
    except Exception:
        pass

    # Walk parent chain
    meta = json.loads(task_row["metadata_json"] or "{}") if task_row else {}
    parent_id = meta.get("parent_task_id")
    visited = set()
    current_id = parent_id
    for _ in range(10):
        if not current_id or current_id in visited:
            break
        visited.add(current_id)
        row = conn.execute(
            "SELECT subtask_group_id, metadata_json, attempt_count, max_attempts FROM tasks WHERE task_id=?",
            (current_id,)
        ).fetchone()
        if not row:
            break
        if row["subtask_group_id"]:
            attempt_count = task_row["attempt_count"] if task_row else 0
            max_attempts = task_row["max_attempts"] if task_row else 3
            if attempt_count >= max_attempts:
                from . import auto_chain
                auto_chain.on_subtask_terminal_failure(conn, project_id, current_id)
            return
        parent_meta = json.loads(row["metadata_json"] or "{}")
        current_id = parent_meta.get("parent_task_id")


def escalate_task(conn: sqlite3.Connection, task_id: str) -> str | None:
    """Escalate a task via QA→Dev retry loop (max 3 rounds).

    - retry_round < 3: increment retry_round, create a child task with parent linkage,
      return new task_id.
    - retry_round >= 3: mark task as design_mismatch, log user notification, return None.
    """
    row = conn.execute(
        """SELECT project_id, type, prompt, related_nodes, created_by, priority,
                  max_attempts, metadata_json, retry_round
           FROM tasks WHERE task_id = ?""",
        (task_id,),
    ).fetchone()

    if not row:
        from .errors import GovernanceError
        raise GovernanceError(f"Task not found: {task_id}", 404)

    retry_round = row["retry_round"] or 0

    if retry_round < 3:
        new_round = retry_round + 1
        result = create_task(
            conn,
            project_id=row["project_id"],
            prompt=row["prompt"],
            task_type=row["type"],
            related_nodes=json.loads(row["related_nodes"] or "[]"),
            created_by=row["created_by"] or "",
            priority=row["priority"],
            max_attempts=row["max_attempts"],
            metadata=json.loads(row["metadata_json"] or "{}"),
            parent_task_id=task_id,
            retry_round=new_round,
        )
        log.info(
            "Escalated task %s → %s (retry_round=%d)",
            task_id, result["task_id"], new_round,
        )
        return result["task_id"]
    else:
        now = _utc_iso()
        conn.execute(
            """UPDATE tasks SET status = 'design_mismatch', execution_status = 'design_mismatch',
               updated_at = ? WHERE task_id = ?""",
            (now, task_id),
        )
        log.warning(
            "Task %s reached max escalation (retry_round=%d) — marked design_mismatch. "
            "Manual intervention required.",
            task_id, retry_round,
        )
        return None
