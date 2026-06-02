"""MCP (Model Context Protocol) server for the governance service.

Implements JSON-RPC 2.0 over stdio transport (per MCP spec).

Capabilities:
  - initialize / initialized handshake
  - tools/list  → returns registered governance tools
  - tools/call  → dispatches to governance API
  - Subscribes to Redis Pub/Sub and forwards events as MCP notifications

Usage:
    python -m agent.governance.mcp_server
  or
    python agent/governance/mcp_server.py

Environment variables:
    REDIS_URL          Redis connection URL (default: redis://localhost:6379/0)
    GOVERNANCE_URL     Governance HTTP base URL (default: http://localhost:40000)
    GOV_TOKEN          Bearer token for governance API calls
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Ensure the agent package root is on sys.path so relative imports work when
# the file is executed directly (python mcp_server.py).
# ---------------------------------------------------------------------------
_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

log = logging.getLogger(__name__)


def _int_arg(args: dict, key: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(args.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _backlog_list_query(args: dict) -> dict:
    query: dict[str, Any] = {
        "view": str(args.get("view") or "compact"),
        "limit": _int_arg(args, "limit", 50, minimum=1, maximum=100),
        "offset": _int_arg(args, "offset", 0, minimum=0, maximum=1_000_000),
    }
    if args.get("priority"):
        query["priority"] = args["priority"]
    if args.get("q"):
        query["q"] = args["q"]
    if args.get("status"):
        query["status"] = args["status"]
    elif "include_closed" in args:
        query["include_closed"] = "true" if args.get("include_closed") else "false"
    else:
        query["status"] = "OPEN"
    return query


def _task_timeline_query(args: dict) -> dict:
    query: dict[str, Any] = {}
    for key in (
        "task_id",
        "backlog_id",
        "trace_id",
        "phase",
        "event_kind",
        "scenario_id",
        "correlation_id",
        "severity",
        "decision",
    ):
        if args.get(key):
            query[key] = str(args[key])
    if args.get("parent_event_id"):
        query["parent_event_id"] = str(_int_arg(args, "parent_event_id", 0, minimum=1, maximum=1_000_000_000))
    if args.get("limit"):
        query["limit"] = str(_int_arg(args, "limit", 200, minimum=1, maximum=1000))
    return query


def _task_timeline_body(args: dict) -> dict:
    allowed = {
        "task_id",
        "backlog_id",
        "mf_id",
        "attempt_num",
        "event_type",
        "phase",
        "event_kind",
        "scenario_id",
        "parent_event_id",
        "correlation_id",
        "severity",
        "decision",
        "schema_version",
        "actor",
        "status",
        "payload",
        "verification",
        "artifact_refs",
        "trace_id",
        "commit_sha",
        "route_token",
        "route_waiver",
        "route_token_waiver",
    }
    return {key: args[key] for key in allowed if key in args and args[key] is not None}

# ---------------------------------------------------------------------------
# MCP protocol constants
# ---------------------------------------------------------------------------
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "aming-claw-governance"
SERVER_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------
TOOLS: list[dict] = [
    {
        "name": "gov_node_list",
        "description": "List all workflow nodes in a project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "Project identifier.",
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "gov_node_status_update",
        "description": "Update the verify status of a workflow node.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "node_id": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "testing", "t2_pass", "qa_pass", "failed", "waived", "skipped"],
                },
            },
            "required": ["project_id", "node_id", "status"],
        },
    },
    {
        "name": "gov_gate_check",
        "description": "Check whether all gates for a node are satisfied.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "node_id": {"type": "string"},
            },
            "required": ["project_id", "node_id"],
        },
    },
    {
        "name": "gov_memory_write",
        "description": "Append a memory entry (decision, pitfall, workaround…) to the project knowledge base.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "node_id": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": ["decision", "pitfall", "workaround", "invariant", "ownership", "pattern", "api", "stub"],
                },
                "content": {"type": "string"},
                "author": {"type": "string"},
            },
            "required": ["project_id", "node_id", "kind", "content"],
        },
    },
    # --- Backlog tools (OPT-DB-BACKLOG) ---
    {
        "name": "backlog_list",
        "description": "List backlog bugs for a project. Defaults to compact OPEN rows to avoid oversized MCP context; use backlog_get for full detail.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project identifier."},
                "status": {"type": "string", "description": "Filter by status (e.g. OPEN, FIXED)."},
                "priority": {"type": "string", "description": "Filter by priority (e.g. P1, P2, P3)."},
                "limit": {"type": "integer", "description": "Maximum rows to return, default 50, max 100."},
                "offset": {"type": "integer", "description": "Pagination offset."},
                "q": {"type": "string", "description": "Case-insensitive search across id, title, details, and file fields."},
                "view": {"type": "string", "enum": ["compact", "full"], "description": "Row shape; compact is the default."},
                "include_closed": {"type": "boolean", "description": "When true and no status is supplied, include closed statuses."},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "backlog_get",
        "description": "Get details of a single backlog bug by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project identifier."},
                "bug_id": {"type": "string", "description": "Bug identifier (e.g. B47)."},
            },
            "required": ["project_id", "bug_id"],
        },
    },
    {
        "name": "backlog_upsert",
        "description": "Create or update a backlog bug entry.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "bug_id": {"type": "string"},
                "title": {"type": "string"},
                "status": {"type": "string"},
                "priority": {"type": "string"},
                "target_files": {"type": "array", "items": {"type": "string"}},
                "test_files": {"type": "array", "items": {"type": "string"}},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                "chain_task_id": {"type": "string"},
                "commit": {"type": "string"},
                "discovered_at": {"type": "string"},
                "details_md": {"type": "string"},
                "chain_trigger_json": {"type": "object"},
                "fixed_at": {"type": "string"},
                "actor": {"type": "string"},
                "route_token": {"type": "object", "description": "Route-token evidence required for protected backlog state/close evidence writes."},
                "route_waiver": {"type": "object", "description": "Explicit route-context-consuming waiver for protected route-token gates."},
                "route_token_waiver": {"type": "object", "description": "Alias for route_waiver."},
            },
            "required": ["project_id", "bug_id"],
        },
    },
    {
        "name": "backlog_close",
        "description": "Close a backlog bug (set status=FIXED with commit hash).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "bug_id": {"type": "string"},
                "commit": {"type": "string", "description": "Git commit hash that fixes the bug."},
                "route_token": {"type": "object", "description": "Route-token evidence required for protected backlog close."},
                "route_waiver": {"type": "object", "description": "Explicit manual-fix/same-worktree waiver for protected route-token gates."},
                "route_token_waiver": {"type": "object", "description": "Alias for route_waiver."},
            },
            "required": ["project_id", "bug_id"],
        },
    },
    {
        "name": "task_timeline_append",
        "description": "Append observer/agent execution evidence to the task timeline. Use this during MF work before close.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "task_id": {"type": "string"},
                "backlog_id": {"type": "string"},
                "mf_id": {"type": "string"},
                "attempt_num": {"type": "integer"},
                "event_type": {"type": "string"},
                "phase": {"type": "string"},
                "event_kind": {"type": "string", "description": "For MF close gate use implementation, verification, or close_ready."},
                "scenario_id": {"type": "string"},
                "parent_event_id": {"type": "integer"},
                "correlation_id": {"type": "string"},
                "severity": {"type": "string"},
                "decision": {"type": "string"},
                "schema_version": {"type": "integer"},
                "actor": {"type": "string"},
                "status": {"type": "string", "description": "Use accepted/ok/passed/succeeded for close-gate evidence."},
                "payload": {"type": "object"},
                "verification": {"type": "object"},
                "artifact_refs": {"type": "object"},
                "trace_id": {"type": "string"},
                "commit_sha": {"type": "string"},
                "route_token": {"type": "object", "description": "Route-token evidence required for protected close-gate timeline evidence."},
                "route_waiver": {"type": "object", "description": "Explicit route-context-consuming waiver for protected route-token gates."},
                "route_token_waiver": {"type": "object", "description": "Alias for route_waiver."},
            },
            "required": ["project_id", "event_type"],
        },
    },
    {
        "name": "task_timeline_list",
        "description": "List append-only observer/agent timeline events by backlog, task, trace, phase, or event kind.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "task_id": {"type": "string"},
                "backlog_id": {"type": "string"},
                "trace_id": {"type": "string"},
                "phase": {"type": "string"},
                "event_kind": {"type": "string"},
                "scenario_id": {"type": "string"},
                "correlation_id": {"type": "string"},
                "severity": {"type": "string"},
                "decision": {"type": "string"},
                "parent_event_id": {"type": "integer"},
                "limit": {"type": "integer", "description": "Maximum events to return, default 200, max 1000"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "mf_timeline_precheck",
        "description": "Precheck whether an MF/observer backlog row has the required timeline evidence before backlog_close.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "bug_id": {"type": "string"},
                "include_events": {"type": "boolean", "description": "Include matching timeline rows in the response."},
                "limit": {"type": "integer", "description": "Maximum events to inspect/return, default 1000, max 1000"},
            },
            "required": ["project_id", "bug_id"],
        },
    },
    {
        "name": "observer_repair_run_plan",
        "description": "Build a read-only replayable observer repair-run plan for cross-system recovery. Does not authorize protected writes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "root_backlog_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Root backlog ids to diagnose and order.",
                },
                "backlog_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Alias for root_backlog_ids.",
                },
                "blockers": {
                    "type": "array",
                    "items": {},
                    "description": "Optional blocker messages or structured failures to classify.",
                },
                "include_timeline_precheck": {
                    "type": "boolean",
                    "description": "When true, include read-only MF timeline precheck summaries for root backlog ids.",
                },
                "route_context_seed": {
                    "type": "object",
                    "description": "Public-safe seed material for deterministic route context identity.",
                },
                "version_check": {
                    "type": "object",
                    "description": "Optional clean-workspace/version evidence for route action precheck preview.",
                },
                "actor": {"type": "string"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "observer_repair_run_route_evidence",
        "description": "Dry-run or record replayable route-service evidence for an observer repair-run plan. Defaults to dry-run and does not fabricate worker, QA, implementation, verification, or close_ready evidence.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "root_backlog_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Root backlog ids to diagnose and attach route-service evidence to.",
                },
                "backlog_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Alias for root_backlog_ids.",
                },
                "blockers": {
                    "type": "array",
                    "items": {},
                    "description": "Optional blocker messages or structured failures to classify.",
                },
                "include_timeline_precheck": {
                    "type": "boolean",
                    "description": "When true, include read-only MF timeline precheck summaries while building the plan.",
                },
                "route_context_seed": {
                    "type": "object",
                    "description": "Public-safe seed material for deterministic route context identity.",
                },
                "version_check": {
                    "type": "object",
                    "description": "Optional clean-workspace/version evidence for route action precheck.",
                },
                "action_precheck_id": {
                    "type": "string",
                    "description": "Route action precheck to record; defaults to observer_dispatch_bounded_worker.",
                },
                "record": {
                    "type": "boolean",
                    "description": "When true, append route-service source events to the timeline. Defaults to false.",
                },
                "include_plan": {
                    "type": "boolean",
                    "description": "Include the full repair-run plan in dry-run output.",
                },
                "actor": {"type": "string"},
            },
            "required": ["project_id"],
        },
    },
]

# ---------------------------------------------------------------------------
# Governance HTTP client helpers
# ---------------------------------------------------------------------------

def _gov_url() -> str:
    return os.environ.get("GOVERNANCE_URL", "http://localhost:40000").rstrip("/")


def _gov_token() -> str:
    return os.environ.get("GOV_TOKEN", "")


def _http(method: str, path: str, body: dict | None = None) -> dict:
    """Make an HTTP request to the governance service."""
    url = f"{_gov_url()}{path}"
    data = json.dumps(body, ensure_ascii=False).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Gov-Token": _gov_token(),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() if exc.fp else ""
        try:
            return json.loads(raw)
        except Exception:
            return {"error": str(exc), "body": raw}
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

def _dispatch_tool(name: str, args: dict) -> Any:
    """Dispatch a tools/call to the governance HTTP API."""
    if name == "gov_node_list":
        pid = args["project_id"]
        return _http("GET", f"/api/wf/{pid}/nodes")

    if name == "gov_node_status_update":
        pid = args["project_id"]
        nid = args["node_id"]
        return _http("POST", f"/api/wf/{pid}/nodes/{nid}/status", {"status": args["status"]})

    if name == "gov_gate_check":
        pid = args["project_id"]
        nid = args["node_id"]
        return _http("GET", f"/api/wf/{pid}/gates/{nid}")

    if name == "gov_memory_write":
        pid = args["project_id"]
        return _http("POST", f"/api/wf/{pid}/memory", args)

    # --- Backlog tools (OPT-DB-BACKLOG) ---
    if name == "backlog_list":
        pid = args["project_id"]
        query = _backlog_list_query(args)
        qs = f"?{urllib.parse.urlencode(query)}" if query else ""
        return _http("GET", f"/api/backlog/{pid}{qs}")

    if name == "backlog_get":
        pid = args["project_id"]
        bug_id = args["bug_id"]
        return _http("GET", f"/api/backlog/{pid}/{bug_id}")

    if name == "backlog_upsert":
        pid = args["project_id"]
        bug_id = args["bug_id"]
        return _http("POST", f"/api/backlog/{pid}/{bug_id}", args)

    if name == "backlog_close":
        pid = args["project_id"]
        bug_id = args["bug_id"]
        return _http("POST", f"/api/backlog/{pid}/{bug_id}/close", args)

    if name == "task_timeline_append":
        pid = args["project_id"]
        return _http("POST", f"/api/task/{pid}/timeline", _task_timeline_body(args))

    if name == "task_timeline_list":
        pid = args["project_id"]
        query = _task_timeline_query(args)
        qs = f"?{urllib.parse.urlencode(query)}" if query else ""
        return _http("GET", f"/api/task/{pid}/timeline{qs}")

    if name == "mf_timeline_precheck":
        pid = args["project_id"]
        bug_id = urllib.parse.quote(str(args["bug_id"]), safe="")
        query = {}
        if "include_events" in args:
            query["include_events"] = "true" if args.get("include_events") else "false"
        if args.get("limit"):
            query["limit"] = str(_int_arg(args, "limit", 1000, minimum=1, maximum=1000))
        qs = f"?{urllib.parse.urlencode(query)}" if query else ""
        return _http("GET", f"/api/backlog/{pid}/{bug_id}/timeline-gate{qs}")

    if name == "observer_repair_run_plan":
        pid = args["project_id"]
        body = {
            key: value
            for key, value in args.items()
            if key != "project_id" and value is not None
        }
        return _http("POST", f"/api/projects/{pid}/observer-repair-run/plan", body)

    if name == "observer_repair_run_route_evidence":
        pid = args["project_id"]
        body = {
            key: value
            for key, value in args.items()
            if key != "project_id" and value is not None
        }
        return _http("POST", f"/api/projects/{pid}/observer-repair-run/route-evidence", body)

    raise ValueError(f"Unknown tool: {name!r}")


# ---------------------------------------------------------------------------
# Stdio transport — thread-safe output
# ---------------------------------------------------------------------------

_stdout_lock = threading.Lock()


def _write(msg: dict) -> None:
    """Serialize *msg* as a single JSON line and write to stdout."""
    line = json.dumps(msg, ensure_ascii=False, separators=(",", ":"))
    with _stdout_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _response(req_id: Any, result: Any) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error_response(req_id: Any, code: int, message: str, data: Any = None) -> None:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    _write({"jsonrpc": "2.0", "id": req_id, "error": err})


def _notification(method: str, params: dict) -> None:
    """Send a server-initiated notification (no id field)."""
    _write({"jsonrpc": "2.0", "method": method, "params": params})


# ---------------------------------------------------------------------------
# JSON-RPC error codes
# ---------------------------------------------------------------------------
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

def _handle(raw: str) -> None:
    """Parse and handle one JSON-RPC message."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        _error_response(None, PARSE_ERROR, f"Parse error: {exc}")
        return

    req_id = msg.get("id")  # None for notifications from client
    method = msg.get("method", "")
    params = msg.get("params") or {}

    # -----------------------------------------------------------------------
    # initialize
    # -----------------------------------------------------------------------
    if method == "initialize":
        _response(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
            },
        })
        return

    # -----------------------------------------------------------------------
    # notifications/initialized  (client acknowledges initialize)
    # -----------------------------------------------------------------------
    if method == "notifications/initialized":
        # No response required for notifications
        return

    # -----------------------------------------------------------------------
    # tools/list
    # -----------------------------------------------------------------------
    if method == "tools/list":
        _response(req_id, {"tools": TOOLS})
        return

    # -----------------------------------------------------------------------
    # tools/call
    # -----------------------------------------------------------------------
    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments") or {}
        try:
            result = _dispatch_tool(tool_name, tool_args)
            _response(req_id, {
                "content": [
                    {"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)},
                ],
            })
        except ValueError as exc:
            _error_response(req_id, METHOD_NOT_FOUND, str(exc))
        except Exception as exc:
            log.exception("Tool dispatch error: %s", tool_name)
            _error_response(req_id, INTERNAL_ERROR, str(exc))
        return

    # -----------------------------------------------------------------------
    # ping
    # -----------------------------------------------------------------------
    if method == "ping":
        _response(req_id, {})
        return

    # -----------------------------------------------------------------------
    # Unknown method
    # -----------------------------------------------------------------------
    if req_id is not None:
        _error_response(req_id, METHOD_NOT_FOUND, f"Method not found: {method!r}")


# ---------------------------------------------------------------------------
# Redis event subscriber → MCP notifications
# ---------------------------------------------------------------------------

def _redis_subscriber_thread() -> None:
    """Subscribe to Redis governance events and emit MCP notifications."""
    try:
        from .redis_client import get_redis
        from .event_bus import REDIS_CHANNEL_PREFIX
    except ImportError:
        try:
            # fallback when run as __main__
            from governance.redis_client import get_redis
            from governance.event_bus import REDIS_CHANNEL_PREFIX
        except ImportError:
            log.warning("Cannot import redis_client; Redis notifications disabled.")
            return

    # Retry loop — Redis may not be available at startup
    while True:
        try:
            r = get_redis()
            if not r.available or r._client is None:
                log.debug("Redis not available, retrying in 5s…")
                time.sleep(5)
                continue

            pubsub = r._client.pubsub()
            # Subscribe to the global channel and all project channels (wildcard)
            pubsub.psubscribe(f"{REDIS_CHANNEL_PREFIX}:*")
            log.info("MCP server subscribed to Redis pattern %s:*", REDIS_CHANNEL_PREFIX)

            for raw_msg in pubsub.listen():
                if raw_msg.get("type") not in ("pmessage", "message"):
                    continue
                data = raw_msg.get("data", "")
                if not data:
                    continue
                try:
                    payload = json.loads(data) if isinstance(data, str) else data
                except (json.JSONDecodeError, TypeError):
                    payload = {"raw": str(data)}

                _notification("governance/event", {
                    "channel": raw_msg.get("channel", ""),
                    "event": payload.get("event", "unknown"),
                    "payload": payload.get("payload", payload),
                })

        except Exception as exc:
            log.warning("Redis subscriber error (%s), reconnecting in 5s…", exc)
            time.sleep(5)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    """Start the MCP server: read stdin, dispatch, emit notifications."""
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    # Start Redis subscriber in background daemon thread
    t = threading.Thread(target=_redis_subscriber_thread, daemon=True, name="redis-sub")
    t.start()

    log.info("MCP governance server started (PID %d)", os.getpid())

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            _handle(raw)
        except Exception:
            log.exception("Unhandled error processing message: %s", raw[:200])


if __name__ == "__main__":
    run()
