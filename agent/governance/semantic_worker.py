"""In-process event-driven semantic enrichment worker.

MF-2026-05-10-016. Replaces the missing daemon for the
`/semantic/jobs` queue. Subscribes to EventBus topics:

- `semantic_job.enqueued` — fired by `POST /semantic/jobs` after writing
  ai_pending rows. Worker drains the affected snapshot.
- `system.startup` — fired during governance startup catchup so any
  ai_pending rows that survived a restart get processed.

For each drain, the worker claims a small batch via the existing
`claim_semantic_jobs` API (lease + claim_id ensure no double-claim if a
future external daemon is added), then runs `run_semantic_enrichment`
in-process for that single node with `submit_for_review=True`. The
result lands in `graph_semantic_nodes` with `status='pending_review'`,
which `backfill_existing_semantic_events` maps to
`EVENT_STATUS_PROPOSED` — invisible to the projection until an operator
flips it via `/feedback/decision` action `accept_semantic_enrichment`.

Scope guardrail: worker only handles `operation_type IN
('node_semantic', 'edge_semantic')`. Other op types (scope_reconcile,
feedback_review) are ignored at the claim layer (`claim_semantic_jobs`
already filters node-shaped rows).

Concurrency: a per-(project, snapshot) lock prevents overlapping
drains. A semantic enrichment config policy caps total concurrent AI
calls and claim batch size, with conservative defaults if config cannot
be loaded. SQLite WAL + the existing `sqlite_write_lock` handles
cross-thread write serialization.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None
_executor_max_workers: int | None = None
_executor_guard = threading.Lock()
_busy_locks: dict[tuple[str, str], threading.Lock] = {}
_busy_locks_guard = threading.Lock()
_registered = False
_DEFAULT_WORKER_MAX_CONCURRENCY = 4
_DEFAULT_DRAIN_BATCH_SIZE = 4
_DEFAULT_DRAIN_LEASE_SECONDS = 600


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _worker_runtime_config(project_id: str = "") -> dict[str, int]:
    defaults = {
        "max_workers": _DEFAULT_WORKER_MAX_CONCURRENCY,
        "claim_batch_size": _DEFAULT_DRAIN_BATCH_SIZE,
        "lease_seconds": _DEFAULT_DRAIN_LEASE_SECONDS,
    }
    try:
        from .reconcile_semantic_config import (
            apply_project_ai_routing,
            load_semantic_enrichment_config,
        )

        root = _project_root_for(project_id) if project_id else Path.cwd()
        cfg = apply_project_ai_routing(
            load_semantic_enrichment_config(project_root=root),
            project_id=project_id,
        )
        policy = cfg.execution_policy
        return {
            "max_workers": _positive_int(
                getattr(policy, "worker_max_concurrency", None),
                defaults["max_workers"],
            ),
            "claim_batch_size": _positive_int(
                getattr(policy, "worker_claim_batch_size", None),
                defaults["claim_batch_size"],
            ),
            "lease_seconds": _positive_int(
                getattr(policy, "worker_lease_seconds", None),
                defaults["lease_seconds"],
            ),
        }
    except Exception as exc:  # noqa: BLE001 - worker must stay alive on bad config
        log.warning("semantic_worker: config load failed; using defaults: %s", exc)
        return defaults


def _get_executor(max_workers: int | None = None) -> ThreadPoolExecutor:
    global _executor, _executor_max_workers
    configured_max_workers = _positive_int(max_workers, _DEFAULT_WORKER_MAX_CONCURRENCY)
    old_executor: ThreadPoolExecutor | None = None
    with _executor_guard:
        if _executor is not None and _executor_max_workers == configured_max_workers:
            return _executor
        old_executor = _executor
        _executor = ThreadPoolExecutor(
            max_workers=configured_max_workers,
            thread_name_prefix="semantic-worker",
        )
        _executor_max_workers = configured_max_workers
        current = _executor
    if old_executor is not None:
        old_executor.shutdown(wait=False, cancel_futures=False)
    return current


def _reset_worker_runtime_for_tests() -> None:
    global _executor, _executor_max_workers, _busy_locks, _registered
    with _executor_guard:
        old_executor = _executor
        _executor = None
        _executor_max_workers = None
    if old_executor is not None:
        old_executor.shutdown(wait=False, cancel_futures=True)
    with _busy_locks_guard:
        _busy_locks = {}
    _registered = False


def _drain_lock_for(project_id: str, snapshot_id: str) -> threading.Lock:
    key = (project_id, snapshot_id)
    with _busy_locks_guard:
        lock = _busy_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _busy_locks[key] = lock
    return lock


def _project_root_for(project_id: str) -> Path:
    """Best-effort project root resolution. Worker runs in same process as
    governance which has its own root resolver — reuse that."""
    from .db import _governance_root

    # Project source root is the project workdir; governance root holds DB.
    # For aming-claw the workdir IS the repo root that hosts agent/.
    # When invoked from server.main(), CWD is the repo root.
    return Path.cwd()


def handle_graph_structure_ai_output(
    project_id: str,
    snapshot_id: str,
    *,
    raw_output: Any,
    mode: str = "dry_run",
    project_root: str | Path | None = None,
    operation_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Process one graph_structure AI output without making a model call.

    This is the bridge for future semantic worker/job orchestration. The worker
    already owns AI invocation; once it has raw model output, this function
    handles parse/gate/projection and optional source-hint accept.
    """
    from . import db as governance_db
    from . import graph_snapshot_store as store
    from .graph_structure_ops import run_graph_structure_ai_output_pipeline

    conn = governance_db.get_connection(project_id)
    try:
        snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
        if not snapshot:
            return {
                "ok": False,
                "status": "failed",
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "mode": mode,
                "accepted": False,
                "mutated": False,
                "errors": ["snapshot_not_found"],
            }
        graph, inventory = _snapshot_graph_and_inventory(project_id, snapshot_id)
        root = str(project_root or "") if project_root is not None else ""
        result = run_graph_structure_ai_output_pipeline(
            raw_output=raw_output,
            mode=mode,
            graph=graph,
            inventory_paths=[str(row.get("path") or "") for row in inventory],
            snapshot_id=snapshot_id,
            base_commit=str(snapshot.get("commit_sha") or ""),
            project_root=root,
            operation_contract=operation_contract,
        )
        return {
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "commit_sha": str(snapshot.get("commit_sha") or ""),
            **result,
        }
    finally:
        conn.close()


def handle_graph_enrich_config_ai_output(
    project_id: str,
    snapshot_id: str = "",
    *,
    raw_output: Any,
    mode: str = "dry_run",
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """Process one graph_enrich_config AI output without making a model call."""
    from .graph_enrich_config_ops import run_graph_enrich_config_ai_output_pipeline

    root = Path(project_root or _project_root_for(project_id)).resolve()
    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=raw_output,
        mode=mode,
        project_root=root,
    )
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "project_root": str(root),
        **result,
    }


def _snapshot_graph_and_inventory(project_id: str, snapshot_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    import json
    from . import graph_snapshot_store as store

    base = store.snapshot_companion_dir(project_id, snapshot_id)
    try:
        graph = json.loads((base / "graph.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        graph = {}
    try:
        inventory = json.loads((base / "file_inventory.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        inventory = []
    if not isinstance(graph, dict):
        graph = {}
    if not isinstance(inventory, list):
        inventory = []
    return graph, [row for row in inventory if isinstance(row, dict)]


def _result_precheck(result: Mapping[str, Any]) -> dict[str, Any]:
    precheck = result.get("precheck") if isinstance(result.get("precheck"), Mapping) else {}
    if precheck:
        return dict(precheck)
    gate = result.get("gate") if isinstance(result.get("gate"), Mapping) else {}
    gate_precheck = gate.get("precheck") if isinstance(gate.get("precheck"), Mapping) else {}
    return dict(gate_precheck)


def _result_errors(result: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    errors.extend(str(item or "") for item in result.get("errors") or [])
    parse = result.get("parse") if isinstance(result.get("parse"), Mapping) else {}
    errors.extend(str(item or "") for item in parse.get("errors") or [])
    gate = result.get("gate") if isinstance(result.get("gate"), Mapping) else {}
    errors.extend(str(item or "") for item in gate.get("errors") or [])
    operations = gate.get("operations") if isinstance(gate.get("operations"), list) else []
    for operation in operations:
        if isinstance(operation, Mapping):
            errors.extend(str(item or "") for item in operation.get("errors") or [])
    out: list[str] = []
    seen: set[str] = set()
    for error in errors:
        item = str(error or "").strip()
        if item and item not in seen:
            out.append(item)
            seen.add(item)
    return out


def _should_ai_repair(result: Mapping[str, Any]) -> bool:
    precheck = _result_precheck(result)
    return bool(precheck.get("retryable")) and int(precheck.get("max_repair_attempts") or 0) > 0


def _repair_payload(
    payload: Mapping[str, Any],
    *,
    previous_output: Any,
    result: Mapping[str, Any],
    attempt: int = 1,
) -> dict[str, Any]:
    out = dict(payload)
    out["repair_request"] = {
        "attempt": max(1, int(attempt or 1)),
        "max_attempts": int(_result_precheck(result).get("max_repair_attempts") or 1),
        "precheck": _result_precheck(result),
        "gate_errors": _result_errors(result),
        "previous_output": _json_safe(previous_output),
        "instructions": (
            "Return exactly one corrected JSON object matching payload.output_contract. "
            "Fix only model-repairable contract errors. Do not retry policy-rejected "
            "operations such as low-evidence calls or calls self-edges."
        ),
    }
    return out


def _json_safe(value: Any) -> Any:
    try:
        import json

        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _graph_structure_ai_payload(
    project_id: str,
    snapshot_id: str,
    *,
    event_payload: dict[str, Any] | None = None,
    operation_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from . import db as governance_db
    from . import graph_snapshot_store as store
    from .graph_structure_ops import (
        graph_structure_ops_output_contract,
    )

    event_payload = event_payload if isinstance(event_payload, dict) else {}
    conn = governance_db.get_connection(project_id)
    try:
        snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id) or {}
    finally:
        conn.close()
    graph, inventory = _snapshot_graph_and_inventory(project_id, snapshot_id)
    deps_graph = graph.get("deps_graph") if isinstance(graph.get("deps_graph"), dict) else {}
    nodes = deps_graph.get("nodes") if isinstance(deps_graph.get("nodes"), list) else []
    edges = deps_graph.get("edges") if isinstance(deps_graph.get("edges"), list) else []
    inventory_paths = [
        str(row.get("path") or "")
        for row in inventory
        if isinstance(row, dict) and str(row.get("path") or "").strip()
    ]
    selector = event_payload.get("selector") if isinstance(event_payload.get("selector"), dict) else {}
    operator_request = (
        event_payload.get("operator_request")
        if isinstance(event_payload.get("operator_request"), dict)
        else {}
    )
    instructions = (
        event_payload.get("instructions")
        if isinstance(event_payload.get("instructions"), dict)
        else {}
    )
    options = (
        event_payload.get("options")
        if isinstance(event_payload.get("options"), dict)
        else {}
    )
    output_contract = graph_structure_ops_output_contract(operation_contract)
    output_contract["source"] = {
        **output_contract["source"],
        "snapshot_id": snapshot_id,
        "base_commit": str(snapshot.get("commit_sha") or ""),
    }
    return {
        "schema_version": 1,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "base_commit": str(snapshot.get("commit_sha") or ""),
        "task": "graph_structure_ops",
        "mode": str(event_payload.get("mode") or "dry_run"),
        "selector": selector,
        "operator_request": operator_request,
        "instructions": instructions,
        "options": options,
        "graph": {
            "nodes": [
                {
                    "id": str(node.get("id") or node.get("node_id") or ""),
                    "layer": str(node.get("layer") or ""),
                    "title": str(node.get("title") or ""),
                    "primary": node.get("primary") or node.get("primary_files") or [],
                    "test": node.get("test") or node.get("test_files") or [],
                    "secondary": node.get("secondary") or node.get("secondary_files") or [],
                }
                for node in nodes[:200]
                if isinstance(node, dict)
            ],
            "edges": [
                {
                    "src": str(edge.get("src") or edge.get("source") or ""),
                    "dst": str(edge.get("dst") or edge.get("target") or ""),
                    "edge_type": str(edge.get("edge_type") or edge.get("type") or ""),
                    "direction": str(edge.get("direction") or ""),
                }
                for edge in edges[:500]
                if isinstance(edge, dict)
            ],
            "truncated": {
                "nodes": len(nodes) > 200,
                "edges": len(edges) > 500,
            },
        },
        "inventory_paths": inventory_paths[:1000],
        "output_contract": output_contract,
    }


def _graph_enrich_config_ai_payload(
    project_id: str,
    snapshot_id: str,
    *,
    event_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .graph_enrich_config_ops import graph_enrich_config_ops_output_contract

    event_payload = event_payload if isinstance(event_payload, dict) else {}
    selector = event_payload.get("selector") if isinstance(event_payload.get("selector"), dict) else {}
    operator_request = (
        event_payload.get("operator_request")
        if isinstance(event_payload.get("operator_request"), dict)
        else {}
    )
    instructions = (
        event_payload.get("instructions")
        if isinstance(event_payload.get("instructions"), dict)
        else {}
    )
    options = (
        event_payload.get("options")
        if isinstance(event_payload.get("options"), dict)
        else {}
    )
    return {
        "schema_version": 1,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "task": "graph_enrich_config_ops",
        "mode": str(event_payload.get("mode") or "dry_run"),
        "selector": selector,
        "operator_request": operator_request,
        "instructions": instructions,
        "options": options,
        "output_contract": graph_enrich_config_ops_output_contract(),
    }


def _drain(project_id: str, snapshot_id: str) -> None:
    """Backwards-compat shim. Pre MF-2026-05-10-017 the worker only handled
    nodes; callers expecting `_drain(project_id, snapshot_id)` still work."""
    _drain_node(project_id, snapshot_id)


def _drain_graph_structure(project_id: str, snapshot_id: str) -> None:
    """Drain queued graph-structure events for one snapshot."""
    runtime_config = _worker_runtime_config(project_id)
    lock = _drain_lock_for(project_id, snapshot_id + ":graph_structure")
    if not lock.acquire(blocking=False):
        log.debug("semantic_worker: graph-structure drain skipped (busy) %s/%s",
                  project_id, snapshot_id)
        return
    try:
        from . import db as governance_db
        from . import graph_events
        from .db import sqlite_write_lock
        from .reconcile_semantic_ai import build_semantic_ai_call
        from .reconcile_semantic_config import (
            apply_project_ai_routing,
            load_semantic_enrichment_config,
        )

        conn = governance_db.get_connection(project_id)
        try:
            graph_events.ensure_schema(conn)
            queued = graph_events.list_events(
                conn,
                project_id,
                snapshot_id,
                event_types=["graph_structure_requested"],
                statuses=[graph_events.EVENT_STATUS_OBSERVED],
                limit=runtime_config["claim_batch_size"],
            )
            if not queued:
                log.info("semantic_worker: no graph-structure jobs to drain for %s/%s",
                         project_id, snapshot_id)
                return
            for event in queued:
                event_id = str(event.get("event_id") or "")
                if not event_id:
                    continue
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                mode = str(payload.get("mode") or "dry_run").strip().lower().replace("-", "_")
                raw_output = (
                    payload.get("ai_output")
                    if "ai_output" in payload
                    else payload.get("output")
                )
                ai_generated = False
                ai_payload: dict[str, Any] | None = None
                project_root = payload.get("project_root") if mode in {"accept", "apply", "write"} else None
                try:
                    with sqlite_write_lock():
                        graph_events.update_event_status(
                            conn,
                            project_id,
                            snapshot_id,
                            event_id,
                            status=graph_events.EVENT_STATUS_AI_REVIEWING,
                            actor="semantic_worker_inproc_graph_structure",
                            operation_type="graph_structure",
                            evidence={"source": "semantic_worker_inproc_graph_structure"},
                        )
                        conn.commit()
                    root = Path(project_root or _project_root_for(project_id))
                    cfg = apply_project_ai_routing(
                        load_semantic_enrichment_config(project_root=root),
                        project_id=project_id,
                    )
                    operation_contract = asdict(cfg.graph_structure_ops)
                    if raw_output in (None, ""):
                        ai_call = build_semantic_ai_call(
                            semantic_config=cfg,
                            project_id=project_id,
                            snapshot_id=snapshot_id,
                            project_root=root,
                        )
                        if ai_call is None:
                            raise RuntimeError("graph_structure_ai_not_configured")
                        ai_payload = _graph_structure_ai_payload(
                            project_id,
                            snapshot_id,
                            event_payload=payload,
                            operation_contract=asdict(cfg.graph_structure_ops),
                        )
                        raw_output = ai_call(
                            "graph_structure",
                            ai_payload,
                        )
                        ai_generated = True
                    result = handle_graph_structure_ai_output(
                        project_id,
                        snapshot_id,
                        raw_output=raw_output,
                        mode=mode,
                        project_root=project_root,
                        operation_contract=operation_contract,
                    )
                    if ai_generated and ai_payload is not None and _should_ai_repair(result):
                        repair = {
                            "attempted": True,
                            "attempts": 1,
                            "precheck_before": _result_precheck(result),
                        }
                        try:
                            repaired_output = ai_call(
                                "graph_structure",
                                _repair_payload(
                                    ai_payload,
                                    previous_output=raw_output,
                                    result=result,
                                    attempt=1,
                                ),
                            )
                            repaired_result = handle_graph_structure_ai_output(
                                project_id,
                                snapshot_id,
                                raw_output=repaired_output,
                                mode=mode,
                                project_root=project_root,
                                operation_contract=operation_contract,
                            )
                            repair.update({
                                "status": "passed" if repaired_result.get("ok") else "failed",
                                "precheck_after": _result_precheck(repaired_result),
                            })
                            result = {**repaired_result, "repair": repair}
                        except Exception as exc:  # noqa: BLE001 - keep original gate failure visible
                            repair.update({"status": "failed", "error": str(exc)})
                            result = {**result, "repair": repair}
                    if result.get("ok"):
                        precheck = _result_precheck(result)
                        with sqlite_write_lock():
                            graph_events.create_event(
                                conn,
                                project_id,
                                snapshot_id,
                                event_type="graph_structure_completed",
                                event_kind="semantic_job",
                                target_type="snapshot",
                                target_id=snapshot_id,
                                status=graph_events.EVENT_STATUS_OBSERVED,
                                operation_type="graph_structure",
                                source_event_id=event_id,
                                payload={"result": result},
                                evidence={
                                    "source": "semantic_worker_inproc_graph_structure",
                                    "mode": mode,
                                    "precheck": precheck,
                                },
                                created_by="semantic_worker_inproc",
                            )
                            graph_events.update_event_status(
                                conn,
                                project_id,
                                snapshot_id,
                                event_id,
                                status=graph_events.EVENT_STATUS_MATERIALIZED,
                                actor="semantic_worker_inproc_graph_structure",
                                operation_type="graph_structure",
                                evidence={
                                    "source": "semantic_worker_inproc_graph_structure",
                                    "completed": True,
                                    "mode": mode,
                                    "precheck": precheck,
                                },
                            )
                            conn.commit()
                    else:
                        errors = _result_errors(result)
                        precheck = _result_precheck(result)
                        with sqlite_write_lock():
                            graph_events.create_event(
                                conn,
                                project_id,
                                snapshot_id,
                                event_type="graph_structure_failed",
                                event_kind="semantic_job",
                                target_type="snapshot",
                                target_id=snapshot_id,
                                status=graph_events.EVENT_STATUS_FAILED,
                                operation_type="graph_structure",
                                source_event_id=event_id,
                                payload={"result": result},
                                evidence={
                                    "source": "semantic_worker_inproc_graph_structure",
                                    "errors": errors,
                                    "mode": mode,
                                    "precheck": precheck,
                                },
                                created_by="semantic_worker_inproc",
                            )
                            graph_events.update_event_status(
                                conn,
                                project_id,
                                snapshot_id,
                                event_id,
                                status=graph_events.EVENT_STATUS_FAILED,
                                actor="semantic_worker_inproc_graph_structure",
                                operation_type="graph_structure",
                                evidence={
                                    "source": "semantic_worker_inproc_graph_structure",
                                    "errors": errors,
                                    "mode": mode,
                                    "precheck": precheck,
                                },
                            )
                            conn.commit()
                except Exception as exc:  # noqa: BLE001 - record and continue
                    log.exception("semantic_worker: graph-structure job failed %s: %s",
                                  event_id, exc)
                    with sqlite_write_lock():
                        graph_events.create_event(
                            conn,
                            project_id,
                            snapshot_id,
                            event_type="graph_structure_failed",
                            event_kind="semantic_job",
                            target_type="snapshot",
                            target_id=snapshot_id,
                            status=graph_events.EVENT_STATUS_FAILED,
                            operation_type="graph_structure",
                            source_event_id=event_id,
                            payload={},
                            evidence={
                                "source": "semantic_worker_inproc_graph_structure",
                                "errors": [str(exc)],
                            },
                            created_by="semantic_worker_inproc",
                        )
                        graph_events.update_event_status(
                            conn,
                            project_id,
                            snapshot_id,
                            event_id,
                            status=graph_events.EVENT_STATUS_FAILED,
                            actor="semantic_worker_inproc_graph_structure",
                            operation_type="graph_structure",
                            evidence={
                                "source": "semantic_worker_inproc_graph_structure",
                                "errors": [str(exc)],
                            },
                        )
                        conn.commit()
        finally:
            conn.close()
    finally:
        lock.release()


def _drain_graph_enrich_config(project_id: str, snapshot_id: str) -> None:
    """Drain queued graph-enrich-config events for one snapshot."""
    runtime_config = _worker_runtime_config(project_id)
    lock = _drain_lock_for(project_id, snapshot_id + ":graph_enrich_config")
    if not lock.acquire(blocking=False):
        log.debug("semantic_worker: graph-enrich-config drain skipped (busy) %s/%s",
                  project_id, snapshot_id)
        return
    try:
        from . import db as governance_db
        from . import graph_events
        from .db import sqlite_write_lock
        from .reconcile_semantic_ai import build_semantic_ai_call
        from .reconcile_semantic_config import (
            apply_project_ai_routing,
            load_semantic_enrichment_config,
        )

        conn = governance_db.get_connection(project_id)
        try:
            graph_events.ensure_schema(conn)
            queued = graph_events.list_events(
                conn,
                project_id,
                snapshot_id,
                event_types=["graph_enrich_config_requested"],
                statuses=[graph_events.EVENT_STATUS_OBSERVED],
                limit=runtime_config["claim_batch_size"],
            )
            if not queued:
                log.info("semantic_worker: no graph-enrich-config jobs to drain for %s/%s",
                         project_id, snapshot_id)
                return
            for event in queued:
                event_id = str(event.get("event_id") or "")
                if not event_id:
                    continue
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                mode = str(payload.get("mode") or "dry_run").strip().lower().replace("-", "_")
                raw_output = (
                    payload.get("ai_output")
                    if "ai_output" in payload
                    else payload.get("output")
                )
                ai_generated = False
                ai_payload: dict[str, Any] | None = None
                project_root = payload.get("project_root") or str(_project_root_for(project_id))
                try:
                    with sqlite_write_lock():
                        graph_events.update_event_status(
                            conn,
                            project_id,
                            snapshot_id,
                            event_id,
                            status=graph_events.EVENT_STATUS_AI_REVIEWING,
                            actor="semantic_worker_inproc_graph_enrich_config",
                            operation_type="graph_enrich_config",
                            evidence={"source": "semantic_worker_inproc_graph_enrich_config"},
                        )
                        conn.commit()
                    root = Path(project_root)
                    cfg = apply_project_ai_routing(
                        load_semantic_enrichment_config(project_root=root),
                        project_id=project_id,
                    )
                    if raw_output in (None, ""):
                        ai_call = build_semantic_ai_call(
                            semantic_config=cfg,
                            project_id=project_id,
                            snapshot_id=snapshot_id,
                            project_root=root,
                        )
                        if ai_call is None:
                            raise RuntimeError("graph_enrich_config_ai_not_configured")
                        ai_payload = _graph_enrich_config_ai_payload(
                            project_id,
                            snapshot_id,
                            event_payload=payload,
                        )
                        raw_output = ai_call(
                            "graph_enrich_config",
                            ai_payload,
                        )
                        ai_generated = True
                    result = handle_graph_enrich_config_ai_output(
                        project_id,
                        snapshot_id,
                        raw_output=raw_output,
                        mode=mode,
                        project_root=root,
                    )
                    if ai_generated and ai_payload is not None and _should_ai_repair(result):
                        repair = {
                            "attempted": True,
                            "attempts": 1,
                            "precheck_before": _result_precheck(result),
                        }
                        try:
                            repaired_output = ai_call(
                                "graph_enrich_config",
                                _repair_payload(
                                    ai_payload,
                                    previous_output=raw_output,
                                    result=result,
                                    attempt=1,
                                ),
                            )
                            repaired_result = handle_graph_enrich_config_ai_output(
                                project_id,
                                snapshot_id,
                                raw_output=repaired_output,
                                mode=mode,
                                project_root=root,
                            )
                            repair.update({
                                "status": "passed" if repaired_result.get("ok") else "failed",
                                "precheck_after": _result_precheck(repaired_result),
                            })
                            result = {**repaired_result, "repair": repair}
                        except Exception as exc:  # noqa: BLE001 - keep original gate failure visible
                            repair.update({"status": "failed", "error": str(exc)})
                            result = {**result, "repair": repair}
                    if result.get("ok"):
                        precheck = _result_precheck(result)
                        with sqlite_write_lock():
                            graph_events.create_event(
                                conn,
                                project_id,
                                snapshot_id,
                                event_type="graph_enrich_config_completed",
                                event_kind="semantic_job",
                                target_type="project",
                                target_id=project_id,
                                status=graph_events.EVENT_STATUS_OBSERVED,
                                operation_type="graph_enrich_config",
                                source_event_id=event_id,
                                payload={"result": result},
                                evidence={
                                    "source": "semantic_worker_inproc_graph_enrich_config",
                                    "mode": mode,
                                    "precheck": precheck,
                                },
                                created_by="semantic_worker_inproc",
                            )
                            graph_events.update_event_status(
                                conn,
                                project_id,
                                snapshot_id,
                                event_id,
                                status=graph_events.EVENT_STATUS_MATERIALIZED,
                                actor="semantic_worker_inproc_graph_enrich_config",
                                operation_type="graph_enrich_config",
                                evidence={
                                    "source": "semantic_worker_inproc_graph_enrich_config",
                                    "completed": True,
                                    "mode": mode,
                                    "precheck": precheck,
                                },
                            )
                            conn.commit()
                    else:
                        errors = _result_errors(result)
                        precheck = _result_precheck(result)
                        with sqlite_write_lock():
                            graph_events.create_event(
                                conn,
                                project_id,
                                snapshot_id,
                                event_type="graph_enrich_config_failed",
                                event_kind="semantic_job",
                                target_type="project",
                                target_id=project_id,
                                status=graph_events.EVENT_STATUS_FAILED,
                                operation_type="graph_enrich_config",
                                source_event_id=event_id,
                                payload={"result": result},
                                evidence={
                                    "source": "semantic_worker_inproc_graph_enrich_config",
                                    "errors": errors,
                                    "mode": mode,
                                    "precheck": precheck,
                                },
                                created_by="semantic_worker_inproc",
                            )
                            graph_events.update_event_status(
                                conn,
                                project_id,
                                snapshot_id,
                                event_id,
                                status=graph_events.EVENT_STATUS_FAILED,
                                actor="semantic_worker_inproc_graph_enrich_config",
                                operation_type="graph_enrich_config",
                                evidence={
                                    "source": "semantic_worker_inproc_graph_enrich_config",
                                    "errors": errors,
                                    "mode": mode,
                                    "precheck": precheck,
                                },
                            )
                            conn.commit()
                except Exception as exc:  # noqa: BLE001 - record and continue
                    log.exception("semantic_worker: graph-enrich-config job failed %s: %s",
                                  event_id, exc)
                    with sqlite_write_lock():
                        graph_events.create_event(
                            conn,
                            project_id,
                            snapshot_id,
                            event_type="graph_enrich_config_failed",
                            event_kind="semantic_job",
                            target_type="project",
                            target_id=project_id,
                            status=graph_events.EVENT_STATUS_FAILED,
                            operation_type="graph_enrich_config",
                            source_event_id=event_id,
                            payload={},
                            evidence={
                                "source": "semantic_worker_inproc_graph_enrich_config",
                                "errors": [str(exc)],
                            },
                            created_by="semantic_worker_inproc",
                        )
                        graph_events.update_event_status(
                            conn,
                            project_id,
                            snapshot_id,
                            event_id,
                            status=graph_events.EVENT_STATUS_FAILED,
                            actor="semantic_worker_inproc_graph_enrich_config",
                            operation_type="graph_enrich_config",
                            evidence={
                                "source": "semantic_worker_inproc_graph_enrich_config",
                                "errors": [str(exc)],
                            },
                        )
                        conn.commit()
        finally:
            conn.close()
    finally:
        lock.release()


def _drain_node(project_id: str, snapshot_id: str) -> None:
    """Drain ai_pending semantic jobs for one snapshot.

    Claims configured batches until no claimable rows remain. Each batch
    processes claimed nodes concurrently up to
    execution_policy.worker_max_concurrency. The snapshot lock only protects
    claim ownership; each node uses its own DB connection.
    """
    runtime_config = _worker_runtime_config(project_id)
    lock = _drain_lock_for(project_id, snapshot_id)
    if not lock.acquire(blocking=False):
        log.debug("semantic_worker: drain skipped (busy) %s/%s", project_id, snapshot_id)
        return
    try:
        from . import db as governance_db
        from . import reconcile_semantic_enrichment as semantic
        from .reconcile_semantic_ai import build_semantic_ai_call
        from .reconcile_semantic_config import (
            apply_project_ai_routing,
            load_semantic_enrichment_config,
        )
        from . import reconcile_feedback

        conn = governance_db.get_connection(project_id)
        try:
            root: Path | None = None
            ai_call: Any | None = None
            batch_count = 0
            empty_claim_retry_count = 0
            while True:
                try:
                    claim = semantic.claim_semantic_jobs(
                        conn,
                        project_id,
                        snapshot_id,
                        worker_id="semantic_worker_inproc",
                        statuses=["ai_pending", "pending_ai"],
                        limit=runtime_config["claim_batch_size"],
                        lease_seconds=runtime_config["lease_seconds"],
                        actor="semantic_worker_inproc",
                    )
                except Exception as exc:  # noqa: BLE001 - claim is best-effort
                    log.warning("semantic_worker: claim failed %s/%s: %s",
                                project_id, snapshot_id, exc)
                    conn.commit()
                    return
                claim_id = str(claim.get("claim_id") or "")
                # MF-2026-05-10-016 fix: claim_semantic_jobs returns `jobs` (list
                # of row dicts), not `node_ids`. Extract node_id per row.
                jobs = claim.get("jobs") or []
                node_ids = [
                    str(j.get("node_id") or "").strip()
                    for j in jobs
                    if j.get("node_id")
                ]
                if not node_ids:
                    pending_count = _count_claimable_pending_node_jobs(
                        conn,
                        project_id,
                        snapshot_id,
                        worker_id="semantic_worker_inproc",
                    )
                    if pending_count > 0 and empty_claim_retry_count < 2:
                        empty_claim_retry_count += 1
                        log.warning(
                            "semantic_worker: empty claim but %d pending job(s) remain "
                            "%s/%s (claim_id=%s retry=%d)",
                            pending_count,
                            project_id,
                            snapshot_id,
                            claim_id,
                            empty_claim_retry_count,
                        )
                        _record_node_drain_gap(
                            conn,
                            project_id,
                            snapshot_id,
                            pending_count=pending_count,
                            claim_id=claim_id,
                            batch_count=batch_count,
                            retry_count=empty_claim_retry_count,
                        )
                        _publish_node_drain_gap(
                            project_id,
                            snapshot_id,
                            pending_count=pending_count,
                            claim_id=claim_id,
                            batch_count=batch_count,
                            retry_count=empty_claim_retry_count,
                        )
                        continue
                    log.info(
                        "semantic_worker: nothing claimed %s/%s (claim_id=%s claimed_count=%d batches=%d)",
                        project_id,
                        snapshot_id,
                        claim_id,
                        int(claim.get("claimed_count") or 0),
                        batch_count,
                    )
                    return
                empty_claim_retry_count = 0
                batch_count += 1
                log.info(
                    "semantic_worker: claim_id=%s batch=%d node_ids=%s",
                    claim_id,
                    batch_count,
                    list(node_ids)[:5],
                )
                if root is None:
                    root = _project_root_for(project_id)
                if ai_call is None:
                    cfg = apply_project_ai_routing(
                        load_semantic_enrichment_config(project_root=root),
                        project_id=project_id,
                    )
                    try:
                        ai_call = build_semantic_ai_call(
                            semantic_config=cfg,
                            project_id=project_id,
                            snapshot_id=snapshot_id,
                            project_root=root,
                        )
                    except Exception as exc:  # noqa: BLE001 - record + leave rows for next drain
                        log.error("semantic_worker: build_semantic_ai_call failed: %s", exc)
                        return
                max_node_workers = min(runtime_config["max_workers"], len(node_ids))
                if max_node_workers <= 1:
                    for node_id in node_ids:
                        _process_node_semantic_job(
                            project_id, snapshot_id, root=root, ai_call=ai_call, node_id=node_id
                        )
                else:
                    with ThreadPoolExecutor(
                        max_workers=max_node_workers,
                        thread_name_prefix="semantic-node",
                    ) as node_pool:
                        futures = {
                            node_pool.submit(
                                _process_node_semantic_job,
                                project_id,
                                snapshot_id,
                                root=root,
                                ai_call=ai_call,
                                node_id=node_id,
                            ): node_id
                            for node_id in node_ids
                        }
                        for future in as_completed(futures):
                            node_id = futures[future]
                            try:
                                future.result()
                            except Exception as exc:  # noqa: BLE001 - record + carry on
                                log.exception(
                                    "semantic_worker: node job failed for %s: %s",
                                    node_id,
                                    exc,
                                )
                finalized = _finalize_completed_node_jobs_from_events(
                    project_id,
                    snapshot_id,
                    node_ids=node_ids,
                )
                if finalized:
                    log.info(
                        "semantic_worker: finalized %d completed node job(s) for %s/%s",
                        finalized,
                        project_id,
                        snapshot_id,
                    )
        finally:
            conn.close()
    finally:
        lock.release()


def _count_claimable_pending_node_jobs(
    conn: Any,
    project_id: str,
    snapshot_id: str,
    *,
    worker_id: str,
) -> int:
    from . import reconcile_semantic_enrichment as semantic

    try:
        semantic._ensure_semantic_state_schema(conn)
        now = semantic.utc_now()
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM graph_semantic_jobs
            WHERE project_id = ?
              AND snapshot_id = ?
              AND status IN ('ai_pending', 'pending_ai')
              AND (
                COALESCE(lease_expires_at, '') = ''
                OR lease_expires_at <= ?
                OR COALESCE(worker_id, '') = ?
              )
            """,
            (project_id, snapshot_id, now, str(worker_id or "")),
        ).fetchone()
    except Exception as exc:  # noqa: BLE001 - advisory drain guard
        log.debug(
            "semantic_worker: pending node job count failed for %s/%s: %s",
            project_id,
            snapshot_id,
            exc,
        )
        return 0
    try:
        return int(row["n"] if row else 0)
    except Exception:
        try:
            return int(row[0] if row else 0)
        except Exception:
            return 0


def _record_node_drain_gap(
    conn: Any,
    project_id: str,
    snapshot_id: str,
    *,
    pending_count: int,
    claim_id: str,
    batch_count: int,
    retry_count: int,
) -> None:
    try:
        from . import graph_events

        payload = {
            "pending_count": int(pending_count or 0),
            "claim_id": str(claim_id or ""),
            "batch_count": int(batch_count or 0),
            "retry_count": int(retry_count or 0),
            "target_scope": "node",
        }
        graph_events.create_event(
            conn,
            project_id,
            snapshot_id,
            event_type="semantic_job_requested",
            event_kind="semantic_job",
            target_type="snapshot",
            target_id=snapshot_id,
            status=graph_events.EVENT_STATUS_OBSERVED,
            operation_type="node_semantic_drain_gap",
            payload=payload,
            evidence={
                "source": "semantic_worker_inproc",
                "reason": "empty_claim_with_claimable_pending_jobs",
            },
            created_by="semantic_worker_inproc",
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 - audit evidence is advisory
        log.debug("semantic_worker: drain-gap audit event failed: %s", exc)


def _publish_node_drain_gap(
    project_id: str,
    snapshot_id: str,
    *,
    pending_count: int,
    claim_id: str,
    batch_count: int,
    retry_count: int,
) -> None:
    try:
        from . import event_bus

        payload = {
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "pending_count": int(pending_count or 0),
            "claim_id": str(claim_id or ""),
            "batch_count": int(batch_count or 0),
            "retry_count": int(retry_count or 0),
            "target_scope": "node",
            "source": "semantic_worker_inproc",
        }
        event_bus.publish("semantic_worker.drain_gap", payload)
        event_bus.publish("dashboard.changed", {
            "project_id": project_id,
            "path": "/semantic_worker/drain_gap",
            "method": "WORKER",
            "source": "semantic_worker_inproc",
        })
    except Exception as exc:  # noqa: BLE001 - notification is advisory
        log.debug("semantic_worker: drain-gap publish failed: %s", exc)


def _process_node_semantic_job(
    project_id: str,
    snapshot_id: str,
    *,
    root: Path,
    ai_call: Any,
    node_id: str,
) -> dict[str, Any]:
    from . import db as governance_db
    from . import reconcile_feedback
    from . import reconcile_semantic_enrichment as semantic

    node_id_s = str(node_id or "").strip()
    if not node_id_s:
        return {"ok": False, "status": "skipped", "reason": "empty_node_id"}
    conn = governance_db.get_connection(project_id)
    try:
        # MF 2026-05-11: emit ai_reviewing interstitial publish for the
        # dashboard's operations queue. There's no per-node graph_events row
        # to update at claim time, so this is publish-only.
        try:
            from . import event_bus
            event_bus.publish("semantic_node.running", {
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "node_id": node_id_s,
                "source": "semantic_worker_inproc",
            })
            event_bus.publish("dashboard.changed", {
                "project_id": project_id,
                "path": "/semantic_worker/node_running",
                "method": "WORKER",
                "source": "semantic_worker_inproc",
            })
        except Exception as exc:  # noqa: BLE001 - advisory
            log.debug("semantic_worker: node running publish failed for %s: %s",
                      node_id_s, exc)
        try:
            from uuid import uuid4

            trace_dir = (
                semantic._semantic_base_dir(project_id, snapshot_id)
                / "worker-runs"
                / f"{semantic._safe_node_filename(node_id_s)}-{uuid4().hex[:8]}"
            )
            result = semantic.run_semantic_enrichment(
                conn, project_id, snapshot_id, str(root),
                use_ai=True,
                ai_call=ai_call,
                semantic_node_ids=[node_id_s],
                semantic_skip_completed=False,
                semantic_persist_node_ids=[node_id_s],
                trace_dir=trace_dir,
                submit_for_review=True,
                created_by="semantic_worker_inproc",
            )
        except Exception as exc:  # noqa: BLE001 - record + carry on
            log.exception("semantic_worker: enrich failed for %s: %s",
                          node_id_s, exc)
            _finalize_node_semantic_job(
                conn,
                project_id,
                snapshot_id,
                node_id_s,
                status="ai_failed",
                last_error=str(exc),
            )
            conn.commit()
            return {"ok": False, "status": "failed", "node_id": node_id_s, "error": str(exc)}
        summary = result.get("summary") if isinstance(result, dict) else {}
        ai_complete = (summary or {}).get("ai_complete_count", 0)
        if not ai_complete:
            log.warning("semantic_worker: enrich returned 0 ai_complete for %s",
                        node_id_s)
            _finalize_node_semantic_job(
                conn,
                project_id,
                snapshot_id,
                node_id_s,
                status="ai_failed",
                last_error="ai_complete_count_zero",
            )
            conn.commit()
            return {"ok": False, "status": "ai_incomplete", "node_id": node_id_s}
        feature_hash = ""
        row = conn.execute(
            "SELECT feature_hash FROM graph_semantic_nodes WHERE project_id=? AND snapshot_id=? AND node_id=?",
            (project_id, snapshot_id, node_id_s),
        ).fetchone()
        if row:
            feature_hash = str(row["feature_hash"] or "")
        try:
            from . import graph_events
            graph_events.backfill_existing_semantic_events(
                conn, project_id, snapshot_id, actor="semantic_worker_inproc",
            )
        except Exception as exc:  # noqa: BLE001 - advisory
            log.warning("semantic_worker: backfill failed for %s: %s",
                        node_id_s, exc)
            conn.commit()
            return {"ok": False, "status": "backfill_failed", "node_id": node_id_s}
        event_id = ""
        try:
            ev_row = conn.execute(
                """
                SELECT event_id FROM graph_events
                WHERE project_id = ? AND snapshot_id = ?
                  AND event_type = 'semantic_node_enriched'
                  AND target_id = ?
                  AND status = 'proposed'
                ORDER BY event_seq DESC LIMIT 1
                """,
                (project_id, snapshot_id, node_id_s),
            ).fetchone()
            if ev_row:
                event_id = str(ev_row["event_id"] or "")
        except Exception as exc:  # noqa: BLE001
            log.warning("semantic_worker: event lookup failed for %s: %s",
                        node_id_s, exc)
        try:
            reconcile_feedback.submit_feedback_item(
                project_id,
                snapshot_id,
                feedback_kind=reconcile_feedback.KIND_NEEDS_OBSERVER_DECISION,
                issue={
                    "issue": f"AI semantic enrichment generated for {node_id_s} — awaiting operator review",
                    "source_node_ids": [node_id_s],
                    "target_id": node_id_s,
                    "target_type": "node",
                    "priority": "P3",
                    "evidence": {
                        "source": "semantic_worker_inproc",
                        "node_id": node_id_s,
                        "feature_hash": feature_hash,
                        "linked_event_ids": [event_id] if event_id else [],
                    },
                },
                actor="semantic_worker_inproc",
            )
        except Exception as exc:  # noqa: BLE001 - feedback row is advisory
            log.warning("semantic_worker: feedback submit failed for %s: %s",
                        node_id_s, exc)
        bridge_result: dict[str, Any] = {}
        config_bridge_result: dict[str, Any] = {}
        if event_id:
            try:
                from . import semantic_graph_structure_bridge

                bridge_result = (
                    semantic_graph_structure_bridge
                    .bridge_semantic_events_to_graph_structure_jobs(
                        conn,
                        project_id,
                        snapshot_id,
                        event_ids=[event_id],
                        mode="dry_run",
                        actor="semantic_worker_inproc",
                        limit=1,
                    )
                )
                config_bridge_result = (
                    semantic_graph_structure_bridge
                    .bridge_semantic_events_to_graph_enrich_config_jobs(
                        conn,
                        project_id,
                        snapshot_id,
                        event_ids=[event_id],
                        mode="dry_run",
                        actor="semantic_worker_inproc",
                        limit=1,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - graph-structure bridge is advisory
                log.warning(
                    "semantic_worker: semantic graph-structure bridge failed for %s: %s",
                    node_id_s,
                        exc,
                )
        _finalize_node_semantic_job(
            conn,
            project_id,
            snapshot_id,
            node_id_s,
            status="ai_complete",
        )
        conn.commit()
        try:
            from . import event_bus
            payload = {
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "node_id": node_id_s,
                "event_id": event_id,
                "feature_hash": feature_hash,
                "source": "semantic_worker_inproc",
            }
            event_bus.publish("semantic_node.proposed", payload)
            event_bus.publish("dashboard.changed", {
                "project_id": project_id,
                "path": "/semantic_worker/node_proposed",
                "method": "WORKER",
                "source": "semantic_worker_inproc",
            })
            for bridge_event in bridge_result.get("events", []):
                if bridge_event.get("event_type") != "graph_structure_requested":
                    continue
                event_bus.publish("semantic_job.enqueued", {
                    "project_id": project_id,
                    "snapshot_id": snapshot_id,
                    "target_scope": "graph_structure",
                    "event_id": bridge_event.get("event_id", ""),
                })
            for bridge_event in config_bridge_result.get("events", []):
                if bridge_event.get("event_type") != "graph_enrich_config_requested":
                    continue
                event_bus.publish("semantic_job.enqueued", {
                    "project_id": project_id,
                    "snapshot_id": snapshot_id,
                    "target_scope": "graph_enrich_config",
                    "event_id": bridge_event.get("event_id", ""),
                })
        except Exception as exc:  # noqa: BLE001 - notification is advisory
            log.debug("semantic_worker: node eventbus publish failed for %s: %s",
                      node_id_s, exc)
        _drain_semantic_bridge_followups(
            project_id,
            snapshot_id,
            bridge_result=bridge_result,
            config_bridge_result=config_bridge_result,
        )
        return {
            "ok": True,
            "status": "proposed",
            "node_id": node_id_s,
            "event_id": event_id,
            "bridge": bridge_result,
            "config_bridge": config_bridge_result,
        }
    finally:
        conn.close()


def _drain_semantic_bridge_followups(
    project_id: str,
    snapshot_id: str,
    *,
    bridge_result: Mapping[str, Any] | None = None,
    config_bridge_result: Mapping[str, Any] | None = None,
) -> None:
    """Best-effort fallback drain for bridge-created graph jobs.

    The event bus remains the normal wakeup path. This fallback handles the
    tail case where a node job commits bridge events but no later event arrives
    to wake the graph-structure/config drains.
    """
    bridge_events = (
        bridge_result.get("events")
        if isinstance(bridge_result, Mapping) and isinstance(bridge_result.get("events"), list)
        else []
    )
    config_events = (
        config_bridge_result.get("events")
        if isinstance(config_bridge_result, Mapping) and isinstance(config_bridge_result.get("events"), list)
        else []
    )
    if any(event.get("event_type") == "graph_structure_requested" for event in bridge_events if isinstance(event, Mapping)):
        try:
            _drain_graph_structure(project_id, snapshot_id)
        except Exception as exc:  # noqa: BLE001 - fallback should not fail node completion
            log.debug("semantic_worker: bridge graph_structure fallback drain failed: %s", exc)
    if any(event.get("event_type") == "graph_enrich_config_requested" for event in config_events if isinstance(event, Mapping)):
        try:
            _drain_graph_enrich_config(project_id, snapshot_id)
        except Exception as exc:  # noqa: BLE001 - fallback should not fail node completion
            log.debug("semantic_worker: bridge graph_enrich_config fallback drain failed: %s", exc)


def _finalize_node_semantic_job(
    conn: Any,
    project_id: str,
    snapshot_id: str,
    node_id: str,
    *,
    status: str,
    last_error: str = "",
) -> None:
    from .db import sqlite_write_lock
    from . import reconcile_semantic_enrichment as semantic

    node_id_s = str(node_id or "").strip()
    if not node_id_s:
        return
    now = semantic.utc_now()
    with sqlite_write_lock():
        conn.execute(
            """
            UPDATE graph_semantic_jobs
            SET status = ?,
                worker_id = '',
                claim_id = '',
                claimed_at = '',
                lease_expires_at = '',
                claimed_by = '',
                last_error = ?,
                updated_at = ?
            WHERE project_id = ?
              AND snapshot_id = ?
              AND node_id = ?
            """,
            (
                str(status or ""),
                str(last_error or ""),
                now,
                project_id,
                snapshot_id,
                node_id_s,
            ),
        )


def _finalize_completed_node_jobs_from_events(
    project_id: str,
    snapshot_id: str,
    *,
    node_ids: list[str],
) -> int:
    from . import db as governance_db

    target_ids = [str(node_id or "").strip() for node_id in node_ids if str(node_id or "").strip()]
    if not target_ids:
        return 0
    conn = governance_db.get_connection(project_id)
    try:
        placeholders = ",".join("?" for _ in target_ids)
        rows = conn.execute(
            f"""
            SELECT DISTINCT target_id
            FROM graph_events
            WHERE project_id = ?
              AND snapshot_id = ?
              AND event_type = 'semantic_node_enriched'
              AND status = 'proposed'
              AND target_id IN ({placeholders})
            """,
            (project_id, snapshot_id, *target_ids),
        ).fetchall()
        completed_ids = [str(row["target_id"] or "").strip() for row in rows if str(row["target_id"] or "").strip()]
        for node_id in completed_ids:
            _finalize_node_semantic_job(
                conn,
                project_id,
                snapshot_id,
                node_id,
                status="ai_complete",
            )
        conn.commit()
        return len(completed_ids)
    finally:
        conn.close()


def _drain_edge(project_id: str, snapshot_id: str) -> None:
    """MF-2026-05-10-017: drain unenriched edge_semantic_requested events.

    Edges don't live in graph_semantic_jobs — the queue substrate for edges is
    graph_events. A request lands as `edge_semantic_requested status=observed`;
    once an `edge_semantic_enriched` event for the same target_id exists
    (proposed/observed/accepted/materialized), the edge is considered handled.
    This drain claims unenriched requests one batch at a time, runs AI, writes
    a PROPOSED enriched event, and submits a needs_observer_decision feedback
    row so the Review Queue picks it up — same review gate as the node path.
    """
    runtime_config = _worker_runtime_config(project_id)
    # Use a separate lock from the node drain so node + edge work in parallel.
    lock = _drain_lock_for(project_id, snapshot_id + ":edge")
    if not lock.acquire(blocking=False):
        log.debug("semantic_worker: edge drain skipped (busy) %s/%s",
                  project_id, snapshot_id)
        return
    try:
        import json
        from . import db as governance_db
        from . import graph_events
        from . import reconcile_feedback
        from .reconcile_semantic_ai import build_semantic_ai_call
        from .reconcile_semantic_config import (
            apply_project_ai_routing,
            load_semantic_enrichment_config,
        )

        conn = governance_db.get_connection(project_id)
        try:
            # The dedup compares event_seq (monotonic insertion order). The
            # old query was `target_id NOT IN (any enriched event)` which
            # silently dropped legitimate re-enrich requests — operator submits
            # a second AI enrich for an edge that already has a proposed
            # (and possibly garbage) enrichment, and the worker treats it as
            # already-handled. With event_seq we only skip if there's an
            # enriched event NEWER than the request itself, which lets the
            # re-request go through.
            rows = conn.execute(
                """
                SELECT r.event_id, r.target_id, r.payload_json, r.event_seq
                FROM graph_events r
                WHERE r.project_id = ?
                  AND r.snapshot_id = ?
                  AND r.event_type = 'edge_semantic_requested'
                  AND r.status = 'observed'
                  AND NOT EXISTS (
                    SELECT 1 FROM graph_events e
                    WHERE e.project_id = r.project_id
                      AND e.snapshot_id = r.snapshot_id
                      AND e.event_type = 'edge_semantic_enriched'
                      AND e.target_id = r.target_id
                      AND e.status IN ('observed', 'proposed', 'accepted', 'materialized')
                      AND e.event_seq > r.event_seq
                  )
                ORDER BY r.created_at
                LIMIT ?
                """,
                (project_id, snapshot_id, runtime_config["claim_batch_size"]),
            ).fetchall()
            if not rows:
                log.info("semantic_worker: no edges to drain for %s/%s",
                         project_id, snapshot_id)
                return
            log.info("semantic_worker: edge drain %s/%s candidates=%d",
                     project_id, snapshot_id, len(rows))
            root = _project_root_for(project_id)
            cfg = apply_project_ai_routing(
                load_semantic_enrichment_config(project_root=root),
                project_id=project_id,
            )
            try:
                ai_call = build_semantic_ai_call(
                    semantic_config=cfg,
                    project_id=project_id,
                    snapshot_id=snapshot_id,
                    project_root=root,
                )
            except Exception as exc:  # noqa: BLE001 - record + leave events for next drain
                log.error("semantic_worker: edge build_semantic_ai_call failed: %s", exc)
                return
            if ai_call is None:
                log.warning("semantic_worker: edge AI not configured for %s", project_id)
                return
            for row in rows:
                edge_id = str(row["target_id"] or "").strip()
                if not edge_id:
                    continue
                payload = {}
                try:
                    if row["payload_json"]:
                        payload = json.loads(row["payload_json"]) or {}
                except Exception:  # noqa: BLE001 - payload is advisory
                    payload = {}
                raw_edge = payload.get("edge") or {}
                edge_context = (
                    payload.get("edge_context") if isinstance(payload.get("edge_context"), dict) else {}
                )
                operator_request = (
                    payload.get("operator_request")
                    if isinstance(payload.get("operator_request"), dict)
                    else {}
                )
                instructions = (
                    payload.get("instructions") if isinstance(payload.get("instructions"), dict) else {}
                )
                ai_payload = {
                    "schema_version": 1,
                    "project_id": project_id,
                    "snapshot_id": snapshot_id,
                    "edge": raw_edge,
                    "edge_context": edge_context,
                    "operator_request": operator_request,
                    "instructions": instructions,
                    "output_contract": {
                        "required": ["relation_purpose", "confidence", "evidence"],
                        "optional": ["risk", "directionality", "semantic_label", "open_issues"],
                    },
                }
                # MF 2026-05-11: emit an ai_reviewing interstitial event +
                # dashboard.changed publish so the dashboard's operations
                # queue can show this edge as "running" during the 5-30s
                # AI call. Without this the operator sees ai_pending stuck
                # and then a sudden jump to proposed.
                try:
                    graph_events.update_event_status(
                        conn,
                        project_id,
                        snapshot_id,
                        str(row["event_id"] or ""),
                        status=graph_events.EVENT_STATUS_AI_REVIEWING,
                        actor="semantic_worker_inproc_edge",
                        evidence={"source": "semantic_worker_inproc_edge", "transition": "ai_start"},
                    )
                    conn.commit()
                    from . import event_bus
                    event_bus.publish("edge_semantic.running", {
                        "project_id": project_id,
                        "snapshot_id": snapshot_id,
                        "edge_id": edge_id,
                        "event_id": str(row["event_id"] or ""),
                        "source": "semantic_worker_inproc_edge",
                    })
                    event_bus.publish("dashboard.changed", {
                        "project_id": project_id,
                        "path": "/semantic_worker/edge_running",
                        "method": "WORKER",
                        "source": "semantic_worker_inproc_edge",
                    })
                except Exception as exc:  # noqa: BLE001 - advisory
                    log.debug("semantic_worker: edge running publish failed for %s: %s",
                              edge_id, exc)
                try:
                    ai_response = ai_call("edge", ai_payload)
                except Exception as exc:  # noqa: BLE001 - record + skip
                    log.exception("semantic_worker: edge AI failed for %s: %s",
                                  edge_id, exc)
                    continue
                semantic_payload = ai_response if isinstance(ai_response, dict) else {}
                if "_ai_error" in semantic_payload:
                    log.warning("semantic_worker: edge AI error for %s: %s",
                                edge_id, semantic_payload.get("_ai_error"))
                    continue
                enriched_payload = dict(payload)
                enriched_payload["semantic_payload"] = semantic_payload
                enriched_payload["enriched_by"] = "semantic_worker_inproc_edge"
                # MF 2026-05-11: compute stable_edge_key + edge_signature_hash
                # before writing the event so the event row carries the
                # cross-snapshot identity (stable_node_key column reused).
                edge_struct = edge_context if isinstance(edge_context, dict) else {}
                src_node_id = str(edge_struct.get("src") or "")
                dst_node_id = str(edge_struct.get("dst") or "")
                # Best-effort endpoint node lookup. Worker has the snapshot id
                # so we can query graph_snapshot_store; failure leaves the
                # endpoint dict empty and the hash falls back to raw id-based.
                src_node_meta: dict | None = None
                dst_node_meta: dict | None = None
                try:
                    from . import graph_snapshot_store as _store
                    snap_nodes = _store.list_graph_snapshot_nodes(
                        conn, project_id, snapshot_id,
                        include_semantic=False, limit=2000,
                    )
                    nodes_by_id = {
                        str(n.get("node_id") or n.get("id") or ""): n
                        for n in snap_nodes
                    }
                    src_node_meta = nodes_by_id.get(src_node_id)
                    dst_node_meta = nodes_by_id.get(dst_node_id)
                except Exception:
                    pass
                edge_for_hash = dict(raw_edge or {})
                edge_for_hash.setdefault("src", src_node_id)
                edge_for_hash.setdefault("dst", dst_node_id)
                edge_for_hash.setdefault(
                    "edge_type",
                    str(edge_context.get("edge_type") or "depends_on")
                    if isinstance(edge_context, dict) else "depends_on",
                )
                stable_edge_key = graph_events.stable_edge_key_for_edge(
                    edge_for_hash, src_node_meta, dst_node_meta,
                )
                edge_sig_hash = graph_events.edge_signature_hash_for_edge(
                    edge_for_hash, src_node_meta, dst_node_meta,
                )
                try:
                    enriched = graph_events.create_event(
                        conn,
                        project_id,
                        snapshot_id,
                        event_type="edge_semantic_enriched",
                        event_kind="semantic_job",
                        target_type="edge",
                        target_id=edge_id,
                        status=graph_events.EVENT_STATUS_PROPOSED,
                        stable_node_key=stable_edge_key,  # reused column
                        feature_hash=edge_sig_hash,        # reused column
                        payload=enriched_payload,
                        evidence={"source": "semantic_worker_inproc_edge"},
                        created_by="semantic_worker_inproc",
                    )
                except Exception as exc:  # noqa: BLE001 - record + carry on
                    log.exception("semantic_worker: create edge enriched event failed for %s: %s",
                                  edge_id, exc)
                    continue
                event_id = str(enriched.get("event_id") or "")
                # MF 2026-05-11: also write graph_semantic_edges so the next
                # scope-catchup snapshot can carry this enrichment forward.
                try:
                    from . import reconcile_semantic_enrichment as _semantic
                    _semantic._ensure_semantic_state_schema(conn)
                    import json as _json
                    semantic_entry = {
                        "edge_id": edge_id,
                        "stable_edge_key": stable_edge_key,
                        "edge_signature_hash": edge_sig_hash,
                        "semantic_payload": semantic_payload,
                        "status": "pending_review",
                        "updated_at": "",
                    }
                    conn.execute(
                        """
                        INSERT INTO graph_semantic_edges
                          (project_id, snapshot_id, edge_id, status,
                           edge_signature_hash, semantic_json,
                           feedback_round, batch_index, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(project_id, snapshot_id, edge_id) DO UPDATE SET
                          status = excluded.status,
                          edge_signature_hash = excluded.edge_signature_hash,
                          semantic_json = excluded.semantic_json,
                          updated_at = excluded.updated_at
                        """,
                        (
                            project_id,
                            snapshot_id,
                            edge_id,
                            "pending_review",
                            edge_sig_hash,
                            _json.dumps(semantic_entry, ensure_ascii=False),
                            0,
                            None,
                            "",
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - state write is advisory
                    log.warning("semantic_worker: graph_semantic_edges write failed for %s: %s",
                                edge_id, exc)
                try:
                    reconcile_feedback.submit_feedback_item(
                        project_id,
                        snapshot_id,
                        feedback_kind=reconcile_feedback.KIND_NEEDS_OBSERVER_DECISION,
                        issue={
                            "issue": f"AI edge semantic enrichment generated for {edge_id} — awaiting operator review",
                            "target_id": edge_id,
                            "target_type": "edge",
                            "priority": "P3",
                            "evidence": {
                                "source": "semantic_worker_inproc_edge",
                                "edge_id": edge_id,
                                "linked_event_ids": [event_id] if event_id else [],
                            },
                        },
                        actor="semantic_worker_inproc",
                    )
                except Exception as exc:  # noqa: BLE001 - feedback is advisory
                    log.warning("semantic_worker: edge feedback submit failed for %s: %s",
                                edge_id, exc)
                conn.commit()
                # Notify EventBus so dashboard SSE clients refetch. The
                # worker writes graph_events rows + feedback rows entirely
                # in-process, which never goes through HTTP and therefore
                # never fires _emit_dashboard_changed. Without this publish
                # the dashboard sits on the stale "queued" snapshot until
                # the operator hits ↻ Refresh manually. We publish both a
                # typed `edge_semantic.proposed` and a generic
                # `dashboard.changed` — the latter is what the dashboard
                # SSE hook actually subscribes to.
                try:
                    from . import event_bus
                    payload = {
                        "project_id": project_id,
                        "snapshot_id": snapshot_id,
                        "edge_id": edge_id,
                        "event_id": event_id,
                        "source": "semantic_worker_inproc_edge",
                    }
                    event_bus.publish("edge_semantic.proposed", payload)
                    event_bus.publish("dashboard.changed", {
                        "project_id": project_id,
                        "path": "/semantic_worker/edge_proposed",
                        "method": "WORKER",
                        "source": "semantic_worker_inproc_edge",
                    })
                except Exception as exc:  # noqa: BLE001 - notification is advisory
                    log.debug("semantic_worker: edge eventbus publish failed for %s: %s",
                              edge_id, exc)
        finally:
            conn.close()
    finally:
        lock.release()


def on_semantic_job_enqueued(payload: Any) -> None:
    """EventBus listener for `semantic_job.enqueued`. Spawns a drain task.

    MF-2026-05-10-017: payload may include `target_scope` ("node" | "edge").
    Default is "node" for backwards compatibility with existing publish sites
    that don't set the field.
    """
    try:
        if not isinstance(payload, dict):
            return
        project_id = str(payload.get("project_id") or "").strip()
        snapshot_id = str(payload.get("snapshot_id") or "").strip()
        if not project_id or not snapshot_id:
            return
        target_scope = str(payload.get("target_scope") or "node").strip().lower()
        log.info(
            "semantic_worker: enqueue event %s/%s scope=%s",
            project_id, snapshot_id, target_scope,
        )
        if target_scope == "edge":
            _get_executor(_worker_runtime_config(project_id)["max_workers"]).submit(
                _drain_edge, project_id, snapshot_id
            )
        elif target_scope in {"graph_structure", "graph-structure"}:
            _get_executor(_worker_runtime_config(project_id)["max_workers"]).submit(
                _drain_graph_structure, project_id, snapshot_id
            )
        elif target_scope in {"graph_enrich_config", "graph-enrich-config"}:
            _get_executor(_worker_runtime_config(project_id)["max_workers"]).submit(
                _drain_graph_enrich_config, project_id, snapshot_id
            )
        else:
            _get_executor(_worker_runtime_config(project_id)["max_workers"]).submit(
                _drain_node, project_id, snapshot_id
            )
    except Exception as exc:  # noqa: BLE001 - listener must not raise
        log.exception("semantic_worker: on_semantic_job_enqueued failed: %s", exc)


def on_governance_startup(payload: Any = None) -> None:
    """EventBus listener for `system.startup`. Catches up on rows
    that were enqueued before this process started.

    Scope guardrail: ONLY drains the active snapshot per project.
    Superseded snapshots may have ai_pending rows from old reconcile
    cycles — those are irrelevant to the live dashboard and would
    waste AI calls. Operators wanting to backfill old snapshots can
    manually re-fire enrichment.
    """
    try:
        from . import db as governance_db
        gov_root = governance_db._governance_root()
        if not gov_root.exists():
            return
        for pdir in gov_root.iterdir():
            if not pdir.is_dir():
                continue
            db_path = pdir / "governance.db"
            if not db_path.exists():
                continue
            project_id = pdir.name
            try:
                conn = governance_db.get_connection(project_id)
                try:
                    # Active snapshot id only.
                    active_row = conn.execute(
                        "SELECT snapshot_id FROM graph_snapshot_refs WHERE project_id = ? AND ref_name = 'active'",
                        (project_id,),
                    ).fetchone()
                    if not active_row:
                        continue
                    sid = str(active_row["snapshot_id"] or "")
                    if not sid:
                        continue
                    pending = conn.execute(
                        """
                        SELECT COUNT(*) AS n FROM graph_semantic_jobs
                        WHERE project_id = ? AND snapshot_id = ?
                          AND status IN ('ai_pending', 'pending_ai')
                        """,
                        (project_id, sid),
                    ).fetchone()
                    n = int(pending["n"] if pending else 0)
                    if n > 0:
                        log.info(
                            "semantic_worker: startup catchup %s/%s nodes=%d",
                            project_id, sid, n,
                        )
                        _get_executor(_worker_runtime_config(project_id)["max_workers"]).submit(
                            _drain_node, project_id, sid
                        )
                    # MF-2026-05-10-017: also drain unenriched edge requests.
                    # observer-hotfix 2026-05-11: mirror the dedup-by-event_seq
                    # fix from _drain_edge — startup catchup must use the SAME
                    # logic, otherwise a re-request submitted before restart
                    # silently never gets queued (operator clicked Enrich, AI
                    # ran on prior payload returning garbage, operator
                    # re-submitted; old query excluded that request entirely).
                    edge_pending = conn.execute(
                        """
                        SELECT COUNT(*) AS n FROM graph_events r
                        WHERE r.project_id = ? AND r.snapshot_id = ?
                          AND r.event_type = 'edge_semantic_requested'
                          AND r.status = 'observed'
                          AND NOT EXISTS (
                            SELECT 1 FROM graph_events e
                            WHERE e.project_id = r.project_id
                              AND e.snapshot_id = r.snapshot_id
                              AND e.event_type = 'edge_semantic_enriched'
                              AND e.target_id = r.target_id
                              AND e.status IN ('observed', 'proposed', 'accepted', 'materialized')
                              AND e.event_seq > r.event_seq
                          )
                        """,
                        (project_id, sid),
                    ).fetchone()
                    en = int(edge_pending["n"] if edge_pending else 0)
                    if en > 0:
                        log.info(
                            "semantic_worker: startup catchup %s/%s edges=%d",
                            project_id, sid, en,
                        )
                        _get_executor(_worker_runtime_config(project_id)["max_workers"]).submit(
                            _drain_edge, project_id, sid
                        )
                    graph_structure_pending = conn.execute(
                        """
                        SELECT COUNT(*) AS n FROM graph_events
                        WHERE project_id = ? AND snapshot_id = ?
                          AND event_type = 'graph_structure_requested'
                          AND status = 'observed'
                        """,
                        (project_id, sid),
                    ).fetchone()
                    gn = int(graph_structure_pending["n"] if graph_structure_pending else 0)
                    if gn > 0:
                        log.info(
                            "semantic_worker: startup catchup %s/%s graph_structure=%d",
                            project_id, sid, gn,
                        )
                        _get_executor(_worker_runtime_config(project_id)["max_workers"]).submit(
                            _drain_graph_structure, project_id, sid
                        )
                    graph_enrich_config_pending = conn.execute(
                        """
                        SELECT COUNT(*) AS n FROM graph_events
                        WHERE project_id = ? AND snapshot_id = ?
                          AND event_type = 'graph_enrich_config_requested'
                          AND status = 'observed'
                        """,
                        (project_id, sid),
                    ).fetchone()
                    cn = int(graph_enrich_config_pending["n"] if graph_enrich_config_pending else 0)
                    if cn > 0:
                        log.info(
                            "semantic_worker: startup catchup %s/%s graph_enrich_config=%d",
                            project_id, sid, cn,
                        )
                        _get_executor(_worker_runtime_config(project_id)["max_workers"]).submit(
                            _drain_graph_enrich_config, project_id, sid
                        )
                    if n <= 0 and en <= 0 and gn <= 0 and cn <= 0:
                        continue
                finally:
                    conn.close()
            except Exception as exc:  # noqa: BLE001 - per-project failure shouldn't abort
                log.warning(
                    "semantic_worker: startup catchup failed for %s: %s",
                    project_id, exc,
                )
    except Exception as exc:  # noqa: BLE001 - listener must not raise
        log.exception("semantic_worker: on_governance_startup failed: %s", exc)


def register() -> None:
    """Subscribe listeners + run startup catchup. Idempotent."""
    global _registered
    if _registered:
        return
    try:
        from . import event_bus
        bus = event_bus.get_event_bus()
        bus.subscribe("semantic_job.enqueued", on_semantic_job_enqueued)
        bus.subscribe("system.startup", on_governance_startup)
        _registered = True
        log.info("semantic_worker: registered EventBus subscribers")
        # Fire startup catchup immediately (don't wait for system.startup
        # event publication — register() is called during startup itself).
        on_governance_startup({})
    except Exception as exc:  # noqa: BLE001 - registration failure should not block governance
        log.exception("semantic_worker: register failed: %s", exc)
