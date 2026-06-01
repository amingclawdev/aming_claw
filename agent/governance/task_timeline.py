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
import re
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
    phase                TEXT NOT NULL DEFAULT '',
    event_kind           TEXT NOT NULL DEFAULT '',
    scenario_id          TEXT NOT NULL DEFAULT '',
    parent_event_id      INTEGER NOT NULL DEFAULT 0,
    correlation_id       TEXT NOT NULL DEFAULT '',
    severity             TEXT NOT NULL DEFAULT '',
    decision             TEXT NOT NULL DEFAULT '',
    schema_version       INTEGER NOT NULL DEFAULT 2,
    actor                TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL DEFAULT '',
    payload_json         TEXT NOT NULL DEFAULT '{}',
    verification_json    TEXT NOT NULL DEFAULT '{}',
    artifact_refs_json   TEXT NOT NULL DEFAULT '{}',
    trace_id             TEXT NOT NULL DEFAULT '',
    commit_sha           TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL
);
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_task_timeline_task
    ON task_timeline_events(project_id, task_id, attempt_num, id);
CREATE INDEX IF NOT EXISTS idx_task_timeline_backlog
    ON task_timeline_events(project_id, backlog_id, id);
CREATE INDEX IF NOT EXISTS idx_task_timeline_trace
    ON task_timeline_events(project_id, trace_id, id);
CREATE INDEX IF NOT EXISTS idx_task_timeline_scenario
    ON task_timeline_events(project_id, scenario_id, id);
CREATE INDEX IF NOT EXISTS idx_task_timeline_correlation
    ON task_timeline_events(project_id, correlation_id, id);
CREATE INDEX IF NOT EXISTS idx_task_timeline_kind
    ON task_timeline_events(project_id, event_kind, phase, id);
"""

TIMELINE_SCHEMA_VERSION = 2

_V2_COLUMNS = {
    "phase": "TEXT NOT NULL DEFAULT ''",
    "event_kind": "TEXT NOT NULL DEFAULT ''",
    "scenario_id": "TEXT NOT NULL DEFAULT ''",
    "parent_event_id": "INTEGER NOT NULL DEFAULT 0",
    "correlation_id": "TEXT NOT NULL DEFAULT ''",
    "severity": "TEXT NOT NULL DEFAULT ''",
    "decision": "TEXT NOT NULL DEFAULT ''",
    "schema_version": f"INTEGER NOT NULL DEFAULT {TIMELINE_SCHEMA_VERSION}",
}

MF_TEST_SCENARIO_POLICIES = {
    "none",
    "reuse_existing",
    "new_scenario_required",
}

MF_TEST_SCENARIO_POLICY_MODE = "observer_configured"

MF_TEST_SCENARIO_E2E_DECISIONS = {
    "e2e_current",
    "e2e_added",
    "e2e_deferred",
    "e2e_not_applicable",
}

MF_CLOSE_REQUIRED_EVENT_KINDS = {
    "implementation",
    "verification",
    "close_ready",
}

MF_CLOSE_PASS_STATUSES = {
    "accepted",
    "ok",
    "passed",
    "succeeded",
}

MF_CONTRACT_SCHEMA_VERSION = "mf_contract_gate.v1"

MF_ROUTE_CONTEXT_GATE_SCHEMA_VERSION = "mf_route_context_consumption_gate.v1"
MF_ROUTE_IDENTITY_FIELDS = (
    "route_context_hash",
    "prompt_contract_id",
)
MF_ROUTE_OPTIONAL_IDENTITY_FIELDS = ("prompt_contract_hash",)
MF_ROUTE_CONTEXT_REQUIRED_EVIDENCE_IDS = (
    "route_context",
    "route_action_precheck",
    "bounded_implementation_worker_dispatch",
    "mf_subagent_startup",
)
MF_ROUTE_CONTEXT_INDEPENDENT_VERIFICATION_ID = "independent_verification_lane"
MF_ROUTE_CONTEXT_PASS_STATUSES = {
    *MF_CLOSE_PASS_STATUSES,
    "allow",
    "allowed",
    "approved",
}


def is_protected_close_evidence(event: dict[str, Any] | None) -> bool:
    """Return true when a timeline append can satisfy MF close evidence."""

    if not isinstance(event, dict):
        return False
    tokens = {
        _text(event.get("event_kind")).lower().replace("-", "_"),
        _text(event.get("phase")).lower().replace("-", "_"),
    }
    event_type = _text(event.get("event_type")).lower().replace("-", "_")
    if event_type:
        tokens.add(event_type)
        tokens.update(part for part in re.split(r"[._:/]+", event_type) if part)
    protected = {item.lower().replace("-", "_") for item in MF_CLOSE_REQUIRED_EVENT_KINDS}
    protected.update(
        {
            "checkpoint",
            "checkpoint_branch_task",
            "evidence_checkpoint",
            "evidence_export",
            "export",
            "independent_verification",
            "qa_verification",
        }
    )
    return bool(tokens & protected)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    existing = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(task_timeline_events)").fetchall()
    }
    for column, ddl in _V2_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE task_timeline_events ADD COLUMN {column} {ddl}")
    conn.executescript(INDEX_SQL)


def _utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json(value: Any, default: Any) -> str:
    if value is None:
        value = default
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return json.dumps({"unserializable": repr(value)}, ensure_ascii=False)


def _text(value: Any) -> str:
    return str(value or "")


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item or "").strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _scenario_spec(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("test_scenario_spec", "test_scenario", "scenario"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _test_scenario_policy(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    raw = payload.get("test_scenario_policy")
    if isinstance(raw, dict):
        policy = str(raw.get("decision") or raw.get("policy") or "").strip()
        return policy, raw
    return str(raw or "").strip(), {}


def _insert_event(conn: sqlite3.Connection, event: dict[str, Any]) -> dict[str, Any]:
    from .db import sqlite_write_lock

    created_at = event.get("created_at") or _utc_iso()
    payload = event.get("payload") or {}
    verification = event.get("verification") or {}
    artifact_refs = event.get("artifact_refs") or {}
    with sqlite_write_lock():
        cur = conn.execute(
            """INSERT INTO task_timeline_events
               (project_id, backlog_id, mf_id, task_id, attempt_num, event_type,
                phase, event_kind, scenario_id, parent_event_id, correlation_id,
                severity, decision, schema_version, actor, status, payload_json,
                verification_json, artifact_refs_json, trace_id, commit_sha, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _text(event.get("project_id")),
                _text(event.get("backlog_id")),
                _text(event.get("mf_id")),
                _text(event.get("task_id")),
                int(event.get("attempt_num") or 0),
                _text(event.get("event_type")),
                _text(event.get("phase")),
                _text(event.get("event_kind")),
                _text(event.get("scenario_id")),
                int(event.get("parent_event_id") or 0),
                _text(event.get("correlation_id")),
                _text(event.get("severity")),
                _text(event.get("decision")),
                int(event.get("schema_version") or TIMELINE_SCHEMA_VERSION),
                _text(event.get("actor")),
                _text(event.get("status")),
                _json(payload, {}),
                _json(verification, {}),
                _json(artifact_refs, {}),
                _text(event.get("trace_id")),
                _text(event.get("commit_sha")),
                created_at,
            ),
        )
    inserted = {
        "id": cur.lastrowid,
        "project_id": _text(event.get("project_id")),
        "backlog_id": _text(event.get("backlog_id")),
        "mf_id": _text(event.get("mf_id")),
        "task_id": _text(event.get("task_id")),
        "attempt_num": int(event.get("attempt_num") or 0),
        "event_type": _text(event.get("event_type")),
        "phase": _text(event.get("phase")),
        "event_kind": _text(event.get("event_kind")),
        "scenario_id": _text(event.get("scenario_id")),
        "parent_event_id": int(event.get("parent_event_id") or 0),
        "correlation_id": _text(event.get("correlation_id")),
        "severity": _text(event.get("severity")),
        "decision": _text(event.get("decision")),
        "schema_version": int(event.get("schema_version") or TIMELINE_SCHEMA_VERSION),
        "actor": _text(event.get("actor")),
        "status": _text(event.get("status")),
        "payload": payload if isinstance(payload, dict) else {},
        "verification": verification if isinstance(verification, dict) else {},
        "artifact_refs": artifact_refs if isinstance(artifact_refs, dict) else {},
        "trace_id": _text(event.get("trace_id")),
        "commit_sha": _text(event.get("commit_sha")),
        "created_at": created_at,
    }
    _run_service_router_hook(conn, inserted)
    return inserted


def _run_service_router_hook(conn: sqlite3.Connection, inserted_event: dict[str, Any]) -> None:
    event_type = _text(inserted_event.get("event_type"))
    payload = _mapping(inserted_event.get("payload"))
    if event_type.startswith("service.route.") or payload.get("service_router_suppress") is True:
        return
    try:
        from agent.governance.service_router import route_timeline_event

        route_timeline_event(conn, inserted_event, record=True)
    except Exception:
        log.debug("service router timeline hook failed", exc_info=True)


def record_event(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    event_type: str,
    task_id: str = "",
    backlog_id: str = "",
    mf_id: str = "",
    attempt_num: int = 0,
    phase: str = "",
    event_kind: str = "",
    scenario_id: str = "",
    parent_event_id: int = 0,
    correlation_id: str = "",
    severity: str = "",
    decision: str = "",
    schema_version: int = TIMELINE_SCHEMA_VERSION,
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
            "phase": phase,
            "event_kind": event_kind,
            "scenario_id": scenario_id,
            "parent_event_id": parent_event_id,
            "correlation_id": correlation_id,
            "severity": severity,
            "decision": decision,
            "schema_version": schema_version,
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
    phase: str = "",
    event_kind: str = "",
    scenario_id: str = "",
    parent_event_id: int = 0,
    correlation_id: str = "",
    severity: str = "",
    decision: str = "",
    schema_version: int = TIMELINE_SCHEMA_VERSION,
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
            "phase": phase,
            "event_kind": event_kind,
            "scenario_id": scenario_id,
            "parent_event_id": parent_event_id,
            "correlation_id": correlation_id,
            "severity": severity,
            "decision": decision,
            "schema_version": schema_version,
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


def mf_test_scenario_verification(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Validate the MF test-scenario decision shape.

    MF work can choose no new test, reuse an existing scenario, or require a new
    scenario. The helper does not judge coverage quality; it makes the decision
    explicit enough for later gates and observers to inspect.
    """

    payload = payload if isinstance(payload, dict) else {}
    policy, policy_object = _test_scenario_policy(payload)
    verification_notes = _string_list(payload.get("verification_notes"))
    tests_run = _string_list(payload.get("tests_run"))
    scenario_id = str(payload.get("scenario_id") or "").strip()
    scenario = _scenario_spec(payload)
    if not scenario_id and scenario:
        scenario_id = str(scenario.get("id") or "").strip()
    policy_mode = str(policy_object.get("mode") or "").strip()
    reason = str(policy_object.get("reason") or "").strip()
    allowed_decisions = _string_list(policy_object.get("allowed_decisions"))
    required_evidence_ids = _string_list(policy_object.get("required_evidence_ids"))
    e2e_decision = str(policy_object.get("e2e_decision") or "").strip()
    followup_backlog_id = str(policy_object.get("followup_backlog_id") or "").strip()

    errors: list[str] = []
    warnings: list[str] = []

    if policy_object:
        if policy_mode != MF_TEST_SCENARIO_POLICY_MODE:
            errors.append(
                f"test_scenario_policy.mode must be {MF_TEST_SCENARIO_POLICY_MODE}"
            )
        if not allowed_decisions:
            errors.append("test_scenario_policy.allowed_decisions must be non-empty")
        else:
            unsupported = sorted(set(allowed_decisions) - MF_TEST_SCENARIO_POLICIES)
            if unsupported:
                errors.append(
                    "test_scenario_policy.allowed_decisions contains unsupported "
                    "decision(s): " + ", ".join(unsupported)
                )
            if policy and policy not in allowed_decisions:
                errors.append("test_scenario_policy.decision must be allowed")
        if not reason:
            errors.append("test_scenario_policy.reason is required")
        if not required_evidence_ids:
            errors.append("test_scenario_policy.required_evidence_ids must be non-empty")
        if e2e_decision not in MF_TEST_SCENARIO_E2E_DECISIONS:
            errors.append(
                "test_scenario_policy.e2e_decision must be one of: "
                + ", ".join(sorted(MF_TEST_SCENARIO_E2E_DECISIONS))
            )
        elif e2e_decision == "e2e_deferred" and not followup_backlog_id:
            errors.append(
                "test_scenario_policy.followup_backlog_id is required when "
                "e2e_decision=e2e_deferred"
            )

    if policy not in MF_TEST_SCENARIO_POLICIES:
        errors.append(
            "test_scenario_policy must be one of: "
            + ", ".join(sorted(MF_TEST_SCENARIO_POLICIES))
        )
    elif policy == "none":
        if not verification_notes and not tests_run and not reason:
            errors.append("policy=none requires verification_notes or tests_run explaining why no scenario is needed")
    elif policy == "reuse_existing":
        if not scenario_id and not tests_run and not verification_notes:
            errors.append("policy=reuse_existing requires scenario_id, tests_run, or verification_notes")
    elif policy == "new_scenario_required":
        steps = _string_list(scenario.get("steps")) if scenario else []
        expected = _string_list(scenario.get("expected")) if scenario else []
        if not scenario:
            errors.append("policy=new_scenario_required requires test_scenario_spec")
        else:
            if not steps:
                errors.append("test_scenario_spec.steps must be non-empty")
            if not expected:
                errors.append("test_scenario_spec.expected must be non-empty")
        if not scenario_id:
            warnings.append("test_scenario_spec.id missing")

    has_new_scenario_spec = bool(
        scenario
        and _string_list(scenario.get("steps"))
        and _string_list(scenario.get("expected"))
    )
    return {
        "passed": not errors,
        "status": "passed" if not errors else "failed",
        "policy": policy,
        "effective_decision": policy,
        "policy_mode": policy_mode,
        "reason": reason,
        "allowed_decisions": allowed_decisions,
        "required_evidence_ids": required_evidence_ids,
        "e2e_decision": e2e_decision,
        "followup_backlog_id": followup_backlog_id,
        "scenario_id": scenario_id,
        "warnings": warnings,
        "errors": errors,
        "checks": {
            "has_explicit_policy": policy in MF_TEST_SCENARIO_POLICIES,
            "has_observer_configured_policy": policy_mode == MF_TEST_SCENARIO_POLICY_MODE,
            "has_decision_reason": bool(reason),
            "has_required_evidence_ids": bool(required_evidence_ids),
            "has_e2e_decision": e2e_decision in MF_TEST_SCENARIO_E2E_DECISIONS,
            "has_verification_notes": bool(verification_notes),
            "has_tests_run": bool(tests_run),
            "has_scenario_id": bool(scenario_id),
            "has_new_scenario_spec": has_new_scenario_spec,
        },
    }


def _contract_root(contract: dict[str, Any] | None) -> dict[str, Any]:
    data = _mapping(contract)
    for key in ("parallel_contract", "mf_contract", "contract_instance", "contract"):
        nested = data.get(key)
        if isinstance(nested, dict):
            return nested
    return data


def _copy_route_payload_fields(source: dict[str, Any], payload: dict[str, Any]) -> None:
    for key in (
        "priority",
        "selected_topology",
        "recommended_topology",
        "topology",
        "target_files",
        "test_files",
        "changed_files",
        "owned_files",
        "risk_class",
        "summary",
        "task_summary",
        "title",
        "caller_role",
        "observer_direct_mutation",
        "same_observer_direct_mutation",
        "direct_mutation",
        "implementation_mutation_requested",
    ):
        if key in source and source.get(key) not in (None, "", [], {}):
            payload[key] = source[key]


def _route_topology_policy(contract: dict[str, Any] | None) -> dict[str, Any]:
    data = _mapping(contract)
    root = _contract_root(contract)
    payload: dict[str, Any] = {}

    for source in (
        data,
        _mapping(data.get("close_context")),
        root,
        _mapping(root.get("close_context")),
    ):
        _copy_route_payload_fields(source, payload)

    route_policy = _mapping(root.get("route_topology_policy"))
    _copy_route_payload_fields(route_policy, payload)
    if str(root.get("template_id") or "").strip() == "mf_parallel.v1":
        payload.setdefault("selected_topology", "observer_led_parallel_lanes")
        payload.setdefault("recommended_topology", "mf_parallel.v1")
        _copy_route_payload_fields(_mapping(route_policy.get("high_risk")), payload)

    try:
        from .service_registry import classify_route_topology

        return classify_route_topology(payload)
    except Exception:
        selected = str(
            payload.get("selected_topology")
            or payload.get("recommended_topology")
            or payload.get("topology")
            or ""
        ).strip()
        high_risk = selected in {
            "observer_led_parallel_lanes",
            "mf_parallel.v1",
            "mf_parallel",
            "parallel",
        }
        return {
            "schema_version": "route_topology_selection.v1",
            "selected_topology": (
                "observer_led_parallel_lanes" if high_risk else "lightweight_single_lane"
            ),
            "recommended_topology": "mf_parallel.v1" if high_risk else "single_lane.v1",
            "required_lanes": (
                [
                    "observer_coordinator",
                    "bounded_implementation_worker",
                    "independent_verification_lane",
                    "observer_merge_close_gate",
                ]
                if high_risk
                else ["single_bounded_worker"]
            ),
            "reason_codes": ["explicit_parallel_topology"] if high_risk else ["small_deterministic"],
            "independent_verification_required": high_risk,
        }


def _route_context_required(topology_policy: dict[str, Any]) -> bool:
    selected = str(topology_policy.get("selected_topology") or "").strip()
    recommended = str(topology_policy.get("recommended_topology") or "").strip()
    required_lanes = {str(item).strip() for item in _list(topology_policy.get("required_lanes"))}
    return (
        selected == "observer_led_parallel_lanes"
        or recommended == "mf_parallel.v1"
        or "bounded_implementation_worker" in required_lanes
    )


def _route_marker(value: Any) -> str:
    return re.sub(r"[\s.\-]+", "_", str(value or "").strip().lower())


def _route_independent_verification_required(topology_policy: dict[str, Any]) -> bool:
    required_lanes: set[str] = set()
    for item in _list(topology_policy.get("required_lanes")):
        if isinstance(item, dict):
            required_lanes.update(
                _route_marker(item.get(key))
                for key in ("id", "requirement_id", "role", "lane", "kind", "type", "name")
                if item.get(key)
            )
        else:
            required_lanes.add(_route_marker(item))
    return bool(topology_policy.get("independent_verification_required")) or bool(
        required_lanes.intersection(
            {
                "independent_verification_lane",
                "independent_verification",
                "qa",
                "qa_lane",
                "qa_role",
                "qa_verification",
                "independent_qa",
                "independent_qa_lane",
            }
        )
    )


def _first_deep_text(value: Any, key: str) -> str:
    if isinstance(value, dict):
        if key in value and str(value.get(key) or "").strip():
            return str(value.get(key) or "").strip()
        for child in value.values():
            found = _first_deep_text(child, key)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_deep_text(child, key)
            if found:
                return found
    return ""


def _route_identity(value: Any) -> dict[str, str]:
    identity = {field: _first_deep_text(value, field) for field in MF_ROUTE_IDENTITY_FIELDS}
    for field in MF_ROUTE_OPTIONAL_IDENTITY_FIELDS:
        optional = _first_deep_text(value, field)
        if optional:
            identity[field] = optional
    return identity if all(identity.values()) else {}


def _route_identity_key(identity: dict[str, str]) -> tuple[str, ...]:
    return tuple(identity.get(field, "") for field in MF_ROUTE_IDENTITY_FIELDS)


def _route_visible_manifest_present(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return bool(
        _first_deep_text(value, "visible_injection_manifest_hash")
        or _first_deep_text(value, "visible_injection_manifest")
    )


def _route_event_passed(event: dict[str, Any]) -> bool:
    status = str(event.get("status") or event.get("decision") or "").strip().lower()
    return bool(event.get("passed")) or status in MF_ROUTE_CONTEXT_PASS_STATUSES


def _route_event_markers(event: dict[str, Any]) -> set[str]:
    markers: set[str] = set()
    for key in ("event_kind", "event_type", "phase", "schema_version"):
        value = str(event.get(key) or "").strip().lower()
        if value:
            markers.add(value)
    for key in event.keys():
        markers.add(str(key).strip().lower())
    for key in ("payload", "verification", "artifact_refs"):
        container = _mapping(event.get(key))
        for marker in container.keys():
            markers.add(str(marker).strip().lower())
        for nested_key in (
            "route_context",
            "route_prompt_bundle",
            "prompt_alert_bundle",
            "visible_injection_manifest",
            "route_action_gate",
            "route_action_precheck",
            "mf_subagent_dispatch_gate",
            "bounded_implementation_worker_dispatch",
            "mf_subagent_startup_gate",
            "dispatch_evidence",
            "startup_evidence",
            "contract_evidence",
        ):
            nested = container.get(nested_key)
            if isinstance(nested, dict):
                markers.add(nested_key)
                for marker in nested.keys():
                    markers.add(str(marker).strip().lower())
            for item in _list(nested):
                item = _mapping(item)
                for item_key in ("id", "requirement_id", "kind", "event_kind"):
                    value = str(item.get(item_key) or "").strip().lower()
                    if value:
                        markers.add(value)
    return markers


def _route_event_categories(event: dict[str, Any]) -> set[str]:
    markers = _route_event_markers(event)
    normalized_markers = {_route_marker(marker) for marker in markers}
    categories: set[str] = set()
    if markers.intersection(
        {
            "route_context",
            "route_prompt_bundle",
            "prompt_alert_bundle",
            "visible_injection_manifest",
            "visible_injection_manifest_hash",
        }
    ):
        categories.add("route_context")
    if markers.intersection(
        {
            "route_action",
            "route_action_gate",
            "route_action_precheck",
            "action_precheck",
            "pre_mutation",
            "route.action",
            "route.action.pre_mutation",
        }
    ):
        categories.add("route_action_precheck")
    if markers.intersection(
        {
            "mf_subagent_dispatch",
            "mf_subagent.dispatch",
            "mf_subagent_dispatch_gate",
            "bounded_implementation_worker_dispatch",
            "dispatch_evidence",
        }
    ):
        categories.add("bounded_implementation_worker_dispatch")
    if markers.intersection(
        {
            "mf_subagent_startup",
            "mf_subagent.startup",
            "mf_subagent_startup_gate",
            "startup_gate",
            "startup_evidence",
        }
    ):
        categories.add("mf_subagent_startup")
    if normalized_markers.intersection(
        {
            "independent_verification_lane",
            "independent_verification",
            "qa",
            "qa_lane",
            "qa_role",
            "qa_verification",
            "independent_qa",
            "independent_qa_lane",
        }
    ):
        categories.add(MF_ROUTE_CONTEXT_INDEPENDENT_VERIFICATION_ID)
    return categories


def mf_route_context_gate_verification(
    events: list[dict[str, Any]] | None,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify route context was consumed by route, dispatch, and startup gates."""

    rows = events if isinstance(events, list) else []
    topology_policy = _route_topology_policy(contract)
    route_context_required = _route_context_required(topology_policy)
    independent_verification_required = _route_independent_verification_required(
        topology_policy
    )
    required = route_context_required or independent_verification_required
    required_requirement_ids = list(MF_ROUTE_CONTEXT_REQUIRED_EVIDENCE_IDS)
    if independent_verification_required:
        required_requirement_ids.append(MF_ROUTE_CONTEXT_INDEPENDENT_VERIFICATION_ID)
    present: dict[str, list[dict[str, Any]]] = {
        req_id: [] for req_id in required_requirement_ids
    }
    identities: dict[str, list[dict[str, str]]] = {
        req_id: [] for req_id in required_requirement_ids
    }
    ignored: list[dict[str, Any]] = []

    for raw_event in rows:
        event = _mapping(raw_event)
        if not event:
            continue
        identity = _route_identity(event)
        categories = _route_event_categories(event)
        if not categories:
            continue
        if not identity:
            ignored.append({
                "id": event.get("id") or event.get("event_id"),
                "event_kind": event.get("event_kind"),
                "status": event.get("status") or event.get("decision"),
                "reason": "missing_route_identity",
                "categories": sorted(categories),
            })
            continue
        if "route_context" in categories and not _route_visible_manifest_present(event):
            ignored.append({
                "id": event.get("id") or event.get("event_id"),
                "event_kind": event.get("event_kind"),
                "status": event.get("status") or event.get("decision"),
                "reason": "missing_visible_injection_manifest",
                "categories": sorted(categories),
            })
            continue
        if not _route_event_passed(event):
            ignored.append({
                "id": event.get("id") or event.get("event_id"),
                "event_kind": event.get("event_kind"),
                "status": event.get("status") or event.get("decision"),
                "reason": "non_passing_route_evidence",
                "categories": sorted(categories),
            })
            continue
        event_ref = {
            "id": event.get("id") or event.get("event_id"),
            "event_kind": event.get("event_kind"),
            "phase": event.get("phase"),
            "status": event.get("status") or event.get("decision"),
        }
        for category in categories:
            if category in present:
                present[category].append(event_ref)
                identities[category].append(identity)

    missing = [req_id for req_id in required_requirement_ids if required and not present[req_id]]
    identity_keys = {
        _route_identity_key(identity)
        for category_id in required_requirement_ids
        for identity in identities[category_id]
        if identity
    }
    prompt_hashes = {
        identity.get("prompt_contract_hash", "")
        for category_id in required_requirement_ids
        for identity in identities[category_id]
        if identity.get("prompt_contract_hash")
    }
    same_route_identity = len(identity_keys) <= 1
    same_optional_prompt_contract_hash = len(prompt_hashes) <= 1
    if required and identity_keys and not (same_route_identity and same_optional_prompt_contract_hash):
        missing.append("route_identity_mismatch")
    passed = (not required) or (
        not missing and same_route_identity and same_optional_prompt_contract_hash
    )
    route_identity: dict[str, str] = {}
    if len(identity_keys) == 1:
        identity_key = next(iter(identity_keys))
        route_identity = {
            field: identity_key[idx]
            for idx, field in enumerate(MF_ROUTE_IDENTITY_FIELDS)
        }
        if len(prompt_hashes) == 1:
            route_identity["prompt_contract_hash"] = next(iter(prompt_hashes))
    return {
        "schema_version": MF_ROUTE_CONTEXT_GATE_SCHEMA_VERSION,
        "passed": passed,
        "status": "passed" if passed else "failed",
        "required": required,
        "required_requirement_ids": required_requirement_ids if required else [],
        "present_requirement_ids": [req_id for req_id in required_requirement_ids if present[req_id]],
        "missing_requirement_ids": missing,
        "topology_policy": topology_policy,
        "route_identity": route_identity,
        "same_route_identity": same_route_identity,
        "evidence_events": {
            req_id: present[req_id] for req_id in required_requirement_ids
        },
        "ignored_route_events": ignored,
        "checks": {
            "route_context_present": bool(present["route_context"]),
            "route_action_precheck_present": bool(present["route_action_precheck"]),
            "bounded_implementation_worker_dispatch_present": bool(
                present["bounded_implementation_worker_dispatch"]
            ),
            "mf_subagent_startup_present": bool(present["mf_subagent_startup"]),
            "independent_verification_required": independent_verification_required,
            "independent_verification_lane_present": bool(
                present.get(MF_ROUTE_CONTEXT_INDEPENDENT_VERIFICATION_ID)
            ),
            "same_route_identity": same_route_identity,
            "same_optional_prompt_contract_hash": same_optional_prompt_contract_hash,
        },
    }


def _normalize_requirement(item: Any, *, default_required: bool = True) -> dict[str, Any] | None:
    if isinstance(item, str):
        req_id = item.strip()
        return {"id": req_id, "required": default_required} if req_id else None
    item = _mapping(item)
    req_id = str(item.get("id") or item.get("requirement_id") or "").strip()
    if not req_id:
        return None
    return {
        "id": req_id,
        "required": bool(item.get("required", default_required)),
        "phase": str(item.get("phase") or ""),
        "kind": str(item.get("kind") or item.get("type") or ""),
        "command": str(item.get("command") or ""),
        "label": str(item.get("label") or ""),
    }


def mf_contract_requirements(contract: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return evidence requirements from an instantiated MF contract."""

    root = _contract_root(contract)
    if not root:
        return []

    raw_requirements: list[Any] = []
    raw_requirements.extend(_list(root.get("evidence_requirements")))
    raw_requirements.extend(_list(root.get("required_evidence")))

    integration = _mapping(root.get("integration"))
    raw_requirements.extend(
        {**_mapping(item), "required": True}
        for item in _list(integration.get("required_evidence"))
    )
    raw_requirements.extend(
        {**_mapping(item), "required": False}
        for item in _list(integration.get("optional_evidence"))
    )

    e2e_contract = _mapping(root.get("e2e_contract"))
    if e2e_contract and bool(e2e_contract.get("required")):
        raw_requirements.append({
            "id": e2e_contract.get("requirement_id") or "e2e",
            "required": True,
            "phase": "integration",
            "kind": "e2e",
            "command": e2e_contract.get("command")
            or " && ".join(_string_list(e2e_contract.get("commands"))),
            "label": e2e_contract.get("label") or "E2E",
        })

    test_policy = _mapping(root.get("test_scenario_policy"))
    raw_requirements.extend(
        {
            "id": req_id,
            "required": True,
            "phase": "verification",
            "kind": "test_scenario_policy",
        }
        for req_id in _string_list(test_policy.get("required_evidence_ids"))
    )

    requirements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_requirements:
        normalized = _normalize_requirement(raw)
        if not normalized:
            continue
        if normalized["id"] in seen:
            for existing in requirements:
                if existing["id"] != normalized["id"]:
                    continue
                if normalized.get("required", True):
                    existing["required"] = True
                for key in ("phase", "kind", "command", "label"):
                    if not existing.get(key) and normalized.get(key):
                        existing[key] = normalized[key]
                break
            continue
        seen.add(normalized["id"])
        requirements.append(normalized)
    return requirements


def _requirement_ids_from_container(container: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in ("requirement_id", "contract_requirement_id"):
        value = str(container.get(key) or "").strip()
        if value:
            ids.add(value)
    for key in ("requirement_ids", "contract_requirement_ids"):
        ids.update(_string_list(container.get(key)))
    return ids


def _contract_evidence_ids(event: dict[str, Any]) -> set[str]:
    status = str(event.get("status") or "").strip().lower()
    event_passed = status in MF_CLOSE_PASS_STATUSES
    ids: set[str] = set()
    payload = _mapping(event.get("payload"))
    verification = _mapping(event.get("verification"))
    artifact_refs = _mapping(event.get("artifact_refs"))

    if event_passed:
        ids.update(_requirement_ids_from_container(payload))
        ids.update(_requirement_ids_from_container(verification))
        ids.update(_requirement_ids_from_container(artifact_refs))

    for container in (payload, verification, artifact_refs):
        for item in _list(container.get("contract_evidence")):
            evidence = _mapping(item)
            evidence_status = str(evidence.get("status") or status).strip().lower()
            if evidence_status not in MF_CLOSE_PASS_STATUSES:
                continue
            evidence_id = str(evidence.get("requirement_id") or evidence.get("id") or "").strip()
            if evidence_id:
                ids.add(evidence_id)
    return ids


def mf_contract_gate_verification(
    events: list[dict[str, Any]] | None,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate timeline evidence against an instantiated MF contract."""

    rows = events if isinstance(events, list) else []
    requirements = mf_contract_requirements(contract)
    required = [item for item in requirements if item.get("required", True)]
    required_ids = [item["id"] for item in required]
    all_requirement_ids = {item["id"] for item in requirements}
    present: set[str] = set()
    evidence_events: list[dict[str, Any]] = []
    for event in rows:
        event = _mapping(event)
        ids = _contract_evidence_ids(event)
        if not ids:
            continue
        present.update(ids)
        evidence_events.append({
            "id": event.get("id"),
            "event_kind": event.get("event_kind"),
            "phase": event.get("phase"),
            "status": event.get("status"),
            "requirement_ids": sorted(ids),
        })
    missing = [req_id for req_id in required_ids if req_id not in present]
    root = _contract_root(contract)
    return {
        "schema_version": MF_CONTRACT_SCHEMA_VERSION,
        "passed": not missing,
        "status": "passed" if not missing else "failed",
        "template_id": str(root.get("template_id") or ""),
        "contract_instance_id": str(root.get("contract_instance_id") or ""),
        "required_requirement_ids": required_ids,
        "optional_requirement_ids": [
            item["id"] for item in requirements if not item.get("required", True)
        ],
        "present_requirement_ids": sorted(req_id for req_id in present if req_id in all_requirement_ids),
        "missing_requirement_ids": missing,
        "evidence_events": evidence_events,
        "checks": {
            "has_contract": bool(root),
            "required_count": len(required_ids),
            "missing_count": len(missing),
        },
    }


def mf_close_gate_verification(
    events: list[dict[str, Any]] | None,
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the minimum observer/MF timeline evidence before backlog close."""

    rows = events if isinstance(events, list) else []
    present: set[str] = set()
    ignored: list[dict[str, Any]] = []
    for event in rows:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("event_kind") or "").strip()
        phase = str(event.get("phase") or "").strip()
        status = str(event.get("status") or "").strip().lower()
        key = kind or phase
        if key in MF_CLOSE_REQUIRED_EVENT_KINDS and status in MF_CLOSE_PASS_STATUSES:
            present.add(key)
        elif key in MF_CLOSE_REQUIRED_EVENT_KINDS:
            ignored.append({
                "event_kind": kind,
                "phase": phase,
                "status": status,
                "id": event.get("id"),
            })
    missing = sorted(MF_CLOSE_REQUIRED_EVENT_KINDS - present)
    contract_gate = mf_contract_gate_verification(rows, contract)
    route_context_gate = mf_route_context_gate_verification(rows, contract)
    passed = (
        not missing
        and bool(contract_gate.get("passed"))
        and bool(route_context_gate.get("passed"))
    )
    return {
        "schema_version": "mf_close_timeline_gate.v1",
        "passed": passed,
        "status": "passed" if passed else "failed",
        "required_event_kinds": sorted(MF_CLOSE_REQUIRED_EVENT_KINDS),
        "present_event_kinds": sorted(present),
        "missing_event_kinds": missing,
        "event_count": len(rows),
        "ignored_required_events": ignored,
        "contract_gate": contract_gate,
        "route_context_gate": route_context_gate,
        "checks": {
            "has_implementation": "implementation" in present,
            "has_verification": "verification" in present,
            "has_close_ready": "close_ready" in present,
            "has_contract_evidence": bool(contract_gate.get("passed")),
            "has_route_context_consumption": bool(route_context_gate.get("passed")),
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
    phase: str = "",
    event_kind: str = "",
    scenario_id: str = "",
    correlation_id: str = "",
    severity: str = "",
    decision: str = "",
    parent_event_id: int = 0,
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
    if phase:
        clauses.append("phase = ?")
        params.append(phase)
    if event_kind:
        clauses.append("event_kind = ?")
        params.append(event_kind)
    if scenario_id:
        clauses.append("scenario_id = ?")
        params.append(scenario_id)
    if correlation_id:
        clauses.append("correlation_id = ?")
        params.append(correlation_id)
    if severity:
        clauses.append("severity = ?")
        params.append(severity)
    if decision:
        clauses.append("decision = ?")
        params.append(decision)
    if parent_event_id:
        clauses.append("parent_event_id = ?")
        params.append(int(parent_event_id))
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
