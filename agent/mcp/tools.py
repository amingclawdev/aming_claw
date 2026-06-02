"""MCP Tool definitions and dispatch for Aming Claw.

All tools proxy to the governance HTTP API or the in-process worker pool.
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
import subprocess
import sys
import urllib.parse
import urllib.request
import urllib.error
from typing import Any

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
# Tool schema definitions (per MCP spec)
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    # --- Task Management ---
    {
        "name": "task_create",
        "description": "Create a new task in the governance queue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project identifier"},
                "prompt": {"type": "string", "description": "Task description/instructions"},
                "type": {"type": "string", "enum": ["pm", "dev", "test", "qa", "merge", "task"],
                         "description": "Task type (determines role and chain stage)"},
                "priority": {"type": "integer", "description": "Priority (1=highest)", "default": 5},
                "metadata": {"type": "object", "description": "Additional metadata (target_files, etc.)"},
                "route_token": {"type": "object", "description": "Route-token evidence required for protected task dispatch mutations."},
                "route_waiver": {"type": "object", "description": "Explicit manual-fix/same-worktree waiver for protected route-token gates."},
                "route_token_waiver": {"type": "object", "description": "Alias for route_waiver."},
            },
            "required": ["project_id", "prompt", "type"],
        },
    },
    {
        "name": "task_list",
        "description": "List tasks in a project, optionally filtered by status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "status": {"type": "string", "description": "Filter: queued, claimed, succeeded, failed"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "task_claim",
        "description": "Manually claim the next queued task (Observer takeover).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "worker_id": {"type": "string", "default": "observer"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "task_complete",
        "description": "Mark a task as complete (triggers auto-chain to next stage).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "task_id": {"type": "string"},
                "status": {"type": "string", "enum": ["succeeded", "failed"]},
                "result": {"type": "object", "description": "Task result (changed_files, test_report, etc.)"},
                "route_token": {"type": "object", "description": "Route-token evidence required for mutation-bearing task completion."},
                "route_waiver": {"type": "object", "description": "Explicit manual-fix/same-worktree waiver for protected route-token gates."},
                "route_token_waiver": {"type": "object", "description": "Alias for route_waiver."},
            },
            "required": ["project_id", "task_id", "status"],
        },
    },
    # --- Observer Control ---
    {
        "name": "observer_mode",
        "description": "Enable or disable observer mode. When enabled, all new tasks start as observer_hold and cannot be auto-claimed by executor or auto-chain.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "enabled": {"type": "boolean", "description": "True to enable, False to disable"},
            },
            "required": ["project_id", "enabled"],
        },
    },
    {
        "name": "observer_session_register",
        "description": "Register this AI observer session and return a one-time session token. The DB stores only a token hash.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "observer_kind": {"type": "string"},
                "session_label": {"type": "string"},
                "pid": {"type": "integer"},
                "cwd": {"type": "string"},
                "capabilities": {"type": "object"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "observer_session_heartbeat",
        "description": "Heartbeat a registered observer session using its session token.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "session_id": {"type": "string"},
                "session_token": {"type": "string"},
            },
            "required": ["project_id", "session_id", "session_token"],
        },
    },
    {
        "name": "observer_session_close",
        "description": "Close a registered observer session using its session token.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "session_id": {"type": "string"},
                "session_token": {"type": "string"},
            },
            "required": ["project_id", "session_id", "session_token"],
        },
    },
    {
        "name": "observer_session_revoke",
        "description": "Revoke a registered observer session using its session token.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "session_id": {"type": "string"},
                "session_token": {"type": "string"},
            },
            "required": ["project_id", "session_id", "session_token"],
        },
    },
    {
        "name": "observer_command_list",
        "description": "List durable observer command queue rows for a project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "status": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "observer_command_enqueue",
        "description": "Enqueue a dashboard-originated observer command. Hook reminders must not carry the business payload.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "command_type": {
                    "type": "string",
                    "enum": [
                        "analyze_requirements",
                        "confirm_requirement",
                        "move_to_execution_queue",
                        "pause_worker",
                        "continue_worker",
                        "cancel_worker",
                    ],
                },
                "payload": {"type": "object"},
                "target_session_id": {"type": "string"},
                "created_by": {"type": "string"},
            },
            "required": ["project_id", "command_type"],
        },
    },
    {
        "name": "observer_command_next",
        "description": "Claim the next allowed observer command using a registered session token.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "session_id": {"type": "string"},
                "session_token": {"type": "string"},
            },
            "required": ["project_id", "session_id", "session_token"],
        },
    },
    {
        "name": "observer_command_claim",
        "description": "Claim a specific observer command, or the next allowed command, using a registered session token.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "session_id": {"type": "string"},
                "session_token": {"type": "string"},
                "command_id": {"type": "string"},
            },
            "required": ["project_id", "session_id", "session_token"],
        },
    },
    {
        "name": "observer_command_complete",
        "description": "Complete a claimed observer command. Requires the same claimed session token.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "session_id": {"type": "string"},
                "session_token": {"type": "string"},
                "command_id": {"type": "string"},
                "result": {"type": "object"},
            },
            "required": ["project_id", "session_id", "session_token", "command_id"],
        },
    },
    {
        "name": "observer_command_fail",
        "description": "Fail a claimed observer command. Requires the same claimed session token.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "session_id": {"type": "string"},
                "session_token": {"type": "string"},
                "command_id": {"type": "string"},
                "error": {"type": "string"},
                "result": {"type": "object"},
            },
            "required": ["project_id", "session_id", "session_token", "command_id"],
        },
    },
    {
        "name": "task_hold",
        "description": "Put a queued task into observer_hold state — pauses executor pickup and auto-chain progression. Use before claiming a task for manual review.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "required": ["project_id", "task_id"],
        },
    },
    {
        "name": "task_release",
        "description": "Release a task from observer_hold back to queued — resumes normal executor and auto-chain flow.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "task_id": {"type": "string"},
            },
            "required": ["project_id", "task_id"],
        },
    },
    {
        "name": "task_cancel",
        "description": "Cancel a task (no auto-chain, no retry). Terminal state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "task_id": {"type": "string"},
                "reason": {"type": "string", "description": "Optional cancellation reason"},
            },
            "required": ["project_id", "task_id"],
        },
    },
    # --- Workflow / Nodes ---
    {
        "name": "wf_summary",
        "description": "Get workflow node status summary (pending/testing/t2_pass/qa_pass/waived counts).",
        "inputSchema": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
        },
    },
    {
        "name": "wf_impact",
        "description": "Analyze impact of file changes on workflow nodes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "files": {"type": "string", "description": "Comma-separated file paths"},
            },
            "required": ["project_id", "files"],
        },
    },
    {
        "name": "node_update",
        "description": "Update verification status of workflow nodes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "nodes": {"type": "array", "items": {"type": "string"}, "description": "Node IDs"},
                "status": {"type": "string", "enum": ["pending", "testing", "t2_pass", "qa_pass", "failed", "waived"]},
                "evidence": {"type": "object", "description": "Evidence for the status change"},
            },
            "required": ["project_id", "nodes", "status"],
        },
    },
    # --- Backlog ---
    {
        "name": "backlog_list",
        "description": "List backlog bugs for a project. Defaults to compact OPEN rows to avoid oversized MCP context; use backlog_get for full detail.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "status": {"type": "string", "description": "Optional status filter, e.g. OPEN or FIXED"},
                "priority": {"type": "string", "description": "Optional priority filter, e.g. P1"},
                "limit": {"type": "integer", "description": "Maximum rows to return, default 50, max 100"},
                "offset": {"type": "integer", "description": "Pagination offset"},
                "q": {"type": "string", "description": "Case-insensitive search across id, title, details, and file fields"},
                "view": {"type": "string", "enum": ["compact", "full"], "description": "Row shape; compact is the default"},
                "include_closed": {"type": "boolean", "description": "When true and no status is supplied, include closed statuses"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "backlog_get",
        "description": "Get one backlog bug by id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "bug_id": {"type": "string"},
            },
            "required": ["project_id", "bug_id"],
        },
    },
    {
        "name": "backlog_upsert",
        "description": "Create or update a backlog bug. Use this before MF/observer hotfix code changes.",
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
                "details_md": {"type": "string"},
                "commit": {"type": "string"},
                "fixed_at": {"type": "string"},
                "required_docs": {"type": "array", "items": {"type": "string"}},
                "provenance_paths": {"type": "array", "items": {"type": "string"}},
                "chain_trigger_json": {"type": "object"},
                "bypass_policy": {"type": "object"},
                "mf_type": {"type": "string"},
                "actor": {"type": "string"},
                "force_admit": {"type": "boolean"},
                "route_token": {"type": "object", "description": "Route-token evidence required for protected backlog state/close evidence writes."},
                "route_waiver": {"type": "object", "description": "Explicit route-context-consuming waiver for protected route-token gates."},
                "route_token_waiver": {"type": "object", "description": "Alias for route_waiver."},
            },
            "required": ["project_id", "bug_id"],
        },
    },
    {
        "name": "backlog_close",
        "description": "Close a backlog bug as FIXED with commit evidence.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "bug_id": {"type": "string"},
                "commit": {"type": "string"},
                "actor": {"type": "string"},
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
        "name": "backlog_export",
        "description": "Export backlog rows as a portable JSON payload.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "status": {"type": "string", "description": "Optional status filter, e.g. OPEN or FIXED"},
                "priority": {"type": "string", "description": "Optional priority filter, e.g. P1"},
                "bug_ids": {"type": "array", "items": {"type": "string"}, "description": "Optional bug ids to export"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "backlog_import",
        "description": "Import portable backlog rows into a project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "payload": {"type": "object", "description": "Portable backlog export payload"},
                "on_conflict": {"type": "string", "enum": ["skip", "overwrite", "fail"], "description": "Existing bug id behavior"},
                "dry_run": {"type": "boolean", "description": "Validate and report planned changes without writing rows"},
                "actor": {"type": "string"},
            },
            "required": ["project_id", "payload"],
        },
    },
    # --- Graph Governance ---
    {
        "name": "graph_status",
        "description": "Get active graph snapshot status and pending scope reconcile summary.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "target_commit": {"type": "string"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "graph_operations_queue",
        "description": "Get the dashboard operations queue: semantic jobs, graph stale/scope reconcile, feedback, and patches.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "snapshot_id": {"type": "string"},
                "require_current_semantic": {"type": "boolean"},
                "include_status_observations": {"type": "boolean"},
                "include_resolved": {"type": "boolean"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "graph_query",
        "description": "Run an audited graph query. Preferred first step before implementing or inventing modules.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "tool": {
                    "type": "string",
                    "description": "Graph query tool, e.g. query_schema, find_node_by_path, search_structure, function_index, function_callers, function_callees, high_function_degree, degree_summary, high_degree_nodes, search_semantic, get_node, get_neighbors, search_docs, get_file_excerpt.",
                },
                "args": {"type": "object"},
                "snapshot_id": {"type": "string"},
                "actor": {"type": "string"},
                "query_source": {"type": "string"},
                "query_purpose": {"type": "string"},
                "repo_root": {"type": "string"},
                "project_root": {"type": "string"},
                "task_id": {
                    "type": "string",
                    "description": "Required when query_source=mf_subagent: the calling worker's task_id.",
                },
                "parent_task_id": {
                    "type": "string",
                    "description": "Required when query_source=mf_subagent: the parent observer/MF task_id.",
                },
                "worker_role": {
                    "type": "string",
                    "description": "When query_source=mf_subagent: 'mf_sub'.",
                },
                "fence_token": {
                    "type": "string",
                    "description": "Required when query_source=mf_subagent: the worker's fence_token.",
                },
            },
            "required": ["project_id", "tool"],
        },
    },
    {
        "name": "graph_pending_scope_queue",
        "description": "Queue or update a pending scope-reconcile row for a target commit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "commit_sha": {"type": "string"},
                "target_commit_sha": {"type": "string"},
                "parent_commit_sha": {"type": "string"},
                "status": {"type": "string"},
                "snapshot_id": {"type": "string"},
                "evidence": {"type": "object"},
                "actor": {"type": "string"},
                "force_requeue": {
                    "type": "boolean",
                    "description": "Reopen an existing materialized/waived pending-scope row when its snapshot input is suspect.",
                    "default": False,
                },
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "preflight_check",
        "description": "Run pre-flight self-check: system, version, graph, coverage, queue, and plugin update state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "auto_fix": {"type": "boolean", "description": "Auto-fix recoverable issues (orphan nodes, stuck tasks)", "default": False},
            },
            "required": ["project_id"],
        },
    },
    # --- Executor ---
    {
        "name": "executor_status",
        "description": "Get worker pool status (workers, active tasks, etc.).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "executor_scale",
        "description": "Set the number of worker threads.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workers": {"type": "integer", "description": "Target worker count", "minimum": 0, "maximum": 10},
            },
            "required": ["workers"],
        },
    },
    # --- Host Operations ---
    {
        "name": "manager_health",
        "description": "Check the host ServiceManager sidecar health and runtime version.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "manager_start",
        "description": "Bootstrap ServiceManager via the fixed host script when the manager sidecar is unavailable. Does not expose arbitrary shell execution.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "health_wait_seconds": {
                    "type": "integer",
                    "description": "Maximum seconds for scripts/start-manager.{ps1,sh} to wait for the managed worker.",
                    "default": 90,
                    "minimum": 5,
                    "maximum": 300,
                },
                "takeover": {
                    "type": "boolean",
                    "description": "Rejected from MCP because script takeover can terminate the current MCP process.",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "governance_redeploy",
        "description": "Redeploy governance via the ServiceManager sidecar. Governance remains a manager-owned process, not an MCP child.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "chain_version": {"type": "string", "description": "Short commit hash. Defaults to current git short HEAD."},
                "sync_version": {"type": "boolean", "description": "Sync full git HEAD to governance after a successful redeploy.", "default": True},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "executor_respawn",
        "description": "Ask ServiceManager to respawn the external executor worker via manager_signal.json.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "chain_version": {"type": "string", "description": "Short commit hash. Defaults to current git short HEAD."},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "runtime_status",
        "description": "Aggregate governance health, manager health, and version_check into one runtime status report.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
            },
            "required": ["project_id"],
        },
    },
    # --- Contract Templates ---
    {
        "name": "contract_template_list",
        "description": "List source-controlled contract templates, optionally filtered by task type or stage.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_type": {"type": "string"},
                "stage": {"type": "string"},
            },
        },
    },
    {
        "name": "contract_template_get",
        "description": "Get one source-controlled contract template by exact versioned template id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "template_id": {"type": "string"},
            },
            "required": ["template_id"],
        },
    },
    {
        "name": "contract_template_resolve",
        "description": "Resolve a source-controlled contract template by template id and optional task type, stage, or version.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "template_id": {"type": "string"},
                "task_type": {"type": "string"},
                "stage": {"type": "string"},
                "version": {"type": "string"},
            },
        },
    },
    {
        "name": "ue_audit_validate",
        "description": "Validate UE audit inputs and machine-readable audit output against ue_audit.v1.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "payload": {"type": "object"},
            },
            "required": ["payload"],
        },
    },
    {
        "name": "review_pack_list",
        "description": "List source-controlled expert review packs, optionally filtered by task type or stage.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_type": {"type": "string"},
                "stage": {"type": "string"},
            },
        },
    },
    {
        "name": "review_pack_get",
        "description": "Get one source-controlled expert review pack by exact versioned template id.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "template_id": {"type": "string"},
            },
            "required": ["template_id"],
        },
    },
    {
        "name": "review_pack_resolve",
        "description": "Resolve an expert review pack by template id, task type, stage, or version.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "template_id": {"type": "string"},
                "task_type": {"type": "string"},
                "stage": {"type": "string"},
                "version": {"type": "string"},
            },
        },
    },
    {
        "name": "review_pack_validate_output",
        "description": "Validate a machine-readable review output against a source-controlled review pack.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "template_id": {"type": "string"},
                "payload": {"type": "object"},
            },
            "required": ["template_id", "payload"],
        },
    },
    # --- Context Registry ---
    {
        "name": "context_pack_list",
        "description": "List local role-scoped context packs for a project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "role": {"type": "string", "description": "Caller role, e.g. observer or mf_sub."},
                "visibility": {"type": "string"},
                "include_body": {"type": "boolean", "description": "Include pack body when role is allowed."},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "context_pack_get",
        "description": "Get one local context pack by id, with role-aware body redaction.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "pack_id": {"type": "string"},
                "role": {"type": "string"},
                "include_body": {"type": "boolean"},
            },
            "required": ["project_id", "pack_id"],
        },
    },
    {
        "name": "context_pack_upsert",
        "description": "Create or update a local context pack in governance DB.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "pack_id": {"type": "string"},
                "title": {"type": "string"},
                "visibility": {
                    "type": "string",
                    "description": "public_skill, internal_product, task_context, or private_founder.",
                },
                "allowed_roles": {"type": "array", "items": {"type": "string"}},
                "mode_scope": {"type": "array", "items": {"type": "string"}},
                "project_scope": {"type": "string"},
                "backlog_id": {"type": "string"},
                "source_type": {"type": "string"},
                "source_path": {"type": "string"},
                "summary": {"type": "string"},
                "body": {"type": "string"},
                "version": {"type": "string"},
                "no_export": {"type": "boolean"},
                "enabled": {"type": "boolean"},
                "created_by": {"type": "string"},
            },
            "required": ["project_id", "pack_id"],
        },
    },
    {
        "name": "context_pack_resolve",
        "description": "Resolve effective context for a role/mode/backlog with DB-first, docs-fallback behavior.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "role": {"type": "string", "description": "observer, mf_sub, dev, test, qa, merge, etc."},
                "mode": {"type": "string"},
                "backlog_id": {"type": "string"},
                "requested_by": {"type": "string"},
                "include_body": {"type": "boolean"},
                "record_resolution": {"type": "boolean"},
            },
            "required": ["project_id", "role"],
        },
    },
    {
        "name": "context_pack_seed_private_file",
        "description": "Import a local file into an observer-only private_founder context pack without committing the body.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "source_path": {"type": "string"},
                "pack_id": {"type": "string"},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "created_by": {"type": "string"},
            },
            "required": ["project_id", "source_path"],
        },
    },
    # --- System ---
    {
        "name": "health",
        "description": "Check governance service health and version.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "version_check",
        "description": "Check if working tree is clean and HEAD matches CHAIN_VERSION. Returns ok, head, chain_version, dirty_files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project identifier"},
            },
            "required": ["project_id"],
        },
    },
    {
        "name": "telegram_send",
        "description": "Send a message to Telegram via the bot.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Telegram chat ID"},
                "text": {"type": "string", "description": "Message text"},
            },
            "required": ["chat_id", "text"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

def _runtime_status_classification(
    governance: dict[str, Any],
    manager: dict[str, Any],
    version: dict[str, Any],
) -> dict[str, Any]:
    gov_ok = str(governance.get("status") or "").lower() == "ok" or bool(governance.get("ok"))
    manager_ok = bool(manager.get("ok"))
    version_ok = bool(version.get("ok"))
    runtime_match = bool(version.get("runtime_match"))
    dirty = bool(version.get("dirty"))
    core_ok = bool(gov_ok and version_ok and not dirty)
    advanced_chain_ops_ok = bool(manager_ok and runtime_match and not dirty)
    strict_ok = core_ok

    if not gov_ok:
        severity = "blocking"
        summary = "Governance API is unavailable; graph, backlog, and dashboard actions are blocked."
    elif dirty:
        severity = "warning"
        summary = "Governance is usable, but the worktree has uncommitted files."
    elif not version_ok:
        severity = "warning"
        summary = "Governance is usable, but version metadata needs attention."
    elif not manager_ok or not runtime_match:
        severity = "ok"
        summary = "Governance core is healthy; advanced chain/ops readiness needs attention."
    else:
        severity = "ok"
        summary = "Governance core runtime is healthy."

    usable = severity != "blocking"
    return {
        "ok": usable,
        "strict_ok": strict_ok,
        "severity": severity,
        "usable": usable,
        "summary": summary,
        "capabilities": {
            "dashboard": gov_ok,
            "graph_queries": gov_ok,
            "backlog": gov_ok,
            "core_runtime": core_ok,
            "advanced_chain_ops": advanced_chain_ops_ok,
            "service_manager": manager_ok,
            "executor": advanced_chain_ops_ok,
        },
    }


def _governance_offline_hint(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    has_error = bool(payload.get("error"))
    is_online = str(payload.get("status") or "").lower() == "ok" or bool(payload.get("ok"))
    if not has_error or is_online:
        return payload
    enriched = dict(payload)
    enriched.setdefault("ok", False)
    enriched.setdefault("governance_online", False)
    enriched.setdefault("mcp_loaded", True)
    enriched.setdefault("service_required", "governance")
    enriched.setdefault("recommended_action", "start_governance")
    enriched.setdefault(
        "message",
        "MCP server is loaded, but the Aming Claw governance HTTP service is "
        "offline or timed out. Start it with `aming-claw start` or the "
        "launcher, then retry.",
    )
    return enriched


class ToolDispatcher:
    """Routes MCP tool calls to governance API or in-process worker pool."""

    def __init__(
        self,
        api_fn,
        worker_pool,
        service_mgr=None,
        manager_api_fn=None,
        workspace: str | None = None,
    ):
        """
        Args:
            api_fn: Callable(method, path, data) → dict (HTTP to governance)
            worker_pool: WorkerPool instance for executor tools (may be None)
            service_mgr: ServiceManager for executor subprocess lifecycle
            manager_api_fn: Callable(method, path, data) → dict (HTTP to manager sidecar)
            workspace: Host workspace used for fixed bootstrap scripts and git status
        """
        self._api = api_fn
        self._pool = worker_pool
        self._svc = service_mgr
        self._manager_api = manager_api_fn or self._default_manager_api
        self._workspace = workspace or os.environ.get(
            "CODEX_WORKSPACE",
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        )

    def dispatch(self, name: str, args: dict) -> Any:
        args = dict(args or {})
        # --- Task tools ---
        if name == "task_create":
            pid = args["project_id"]
            body = {"prompt": args["prompt"], "type": args["type"]}
            if args.get("priority"):
                body["priority"] = args["priority"]
            if args.get("metadata"):
                body["metadata"] = args["metadata"]
            for key in ("route_token", "route_waiver", "route_token_waiver"):
                if args.get(key):
                    body[key] = args[key]
            return self._api("POST", f"/api/task/{pid}/create", body)

        if name == "task_list":
            pid = args["project_id"]
            qs = f"?status={args['status']}" if args.get("status") else ""
            return self._api("GET", f"/api/task/{pid}/list{qs}")

        if name == "task_claim":
            pid = args["project_id"]
            wid = args.get("worker_id", "observer")
            return self._api("POST", f"/api/task/{pid}/claim", {"worker_id": wid})

        if name == "task_complete":
            pid = args["project_id"]
            body = {"task_id": args["task_id"], "status": args["status"]}
            if args.get("result"):
                body["result"] = args["result"]
            for key in ("route_token", "route_waiver", "route_token_waiver"):
                if args.get(key):
                    body[key] = args[key]
            return self._api("POST", f"/api/task/{pid}/complete", body)

        # --- Observer tools ---
        if name == "observer_mode":
            pid = args["project_id"]
            return self._api("POST", f"/api/project/{pid}/observer-mode", {"enabled": args["enabled"]})

        if name == "observer_session_register":
            pid = args["project_id"]
            body = {
                key: args[key]
                for key in ("observer_kind", "session_label", "pid", "cwd", "capabilities")
                if key in args and args[key] is not None
            }
            return self._api("POST", f"/api/projects/{pid}/observer-sessions/register", body)

        if name == "observer_session_heartbeat":
            pid = args["project_id"]
            sid = urllib.parse.quote(str(args["session_id"]), safe="")
            return self._api(
                "POST",
                f"/api/projects/{pid}/observer-sessions/{sid}/heartbeat",
                {"session_token": args["session_token"]},
            )

        if name == "observer_session_close":
            pid = args["project_id"]
            sid = urllib.parse.quote(str(args["session_id"]), safe="")
            return self._api(
                "POST",
                f"/api/projects/{pid}/observer-sessions/{sid}/close",
                {"session_token": args["session_token"]},
            )

        if name == "observer_session_revoke":
            pid = args["project_id"]
            sid = urllib.parse.quote(str(args["session_id"]), safe="")
            return self._api(
                "POST",
                f"/api/projects/{pid}/observer-sessions/{sid}/revoke",
                {"session_token": args["session_token"]},
            )

        if name == "observer_command_list":
            pid = args["project_id"]
            query = {}
            if args.get("status"):
                query["status"] = args["status"]
            if args.get("limit"):
                query["limit"] = str(_int_arg(args, "limit", 100, minimum=1, maximum=1000))
            qs = f"?{urllib.parse.urlencode(query)}" if query else ""
            return self._api("GET", f"/api/projects/{pid}/observer-commands{qs}")

        if name == "observer_command_enqueue":
            pid = args["project_id"]
            body = {
                key: args[key]
                for key in ("command_type", "payload", "target_session_id", "created_by")
                if key in args and args[key] is not None
            }
            return self._api("POST", f"/api/projects/{pid}/observer-commands", body)

        if name == "observer_command_next":
            pid = args["project_id"]
            return self._api(
                "POST",
                f"/api/projects/{pid}/observer-commands/next",
                {"session_id": args["session_id"], "session_token": args["session_token"]},
            )

        if name == "observer_command_claim":
            pid = args["project_id"]
            body = {"session_id": args["session_id"], "session_token": args["session_token"]}
            if args.get("command_id"):
                body["command_id"] = args["command_id"]
            return self._api("POST", f"/api/projects/{pid}/observer-commands/claim", body)

        if name == "observer_command_complete":
            pid = args["project_id"]
            cid = urllib.parse.quote(str(args["command_id"]), safe="")
            body = {"session_id": args["session_id"], "session_token": args["session_token"]}
            if args.get("result"):
                body["result"] = args["result"]
            return self._api("POST", f"/api/projects/{pid}/observer-commands/{cid}/complete", body)

        if name == "observer_command_fail":
            pid = args["project_id"]
            cid = urllib.parse.quote(str(args["command_id"]), safe="")
            body = {"session_id": args["session_id"], "session_token": args["session_token"]}
            if args.get("error"):
                body["error"] = args["error"]
            if args.get("result"):
                body["result"] = args["result"]
            return self._api("POST", f"/api/projects/{pid}/observer-commands/{cid}/fail", body)

        if name == "task_hold":
            pid = args["project_id"]
            return self._api("POST", f"/api/task/{pid}/hold", {"task_id": args["task_id"]})

        if name == "task_release":
            pid = args["project_id"]
            return self._api("POST", f"/api/task/{pid}/release", {"task_id": args["task_id"]})

        if name == "task_cancel":
            pid = args["project_id"]
            return self._api("POST", f"/api/task/{pid}/cancel", {"task_id": args["task_id"], "reason": args.get("reason", "")})

        # --- Workflow tools ---
        if name == "wf_summary":
            return self._api("GET", f"/api/wf/{args['project_id']}/summary")

        if name == "wf_impact":
            return self._api("GET", f"/api/wf/{args['project_id']}/impact?files={args['files']}")

        if name == "node_update":
            pid = args["project_id"]
            body = {"nodes": args["nodes"], "status": args["status"]}
            if args.get("evidence"):
                body["evidence"] = args["evidence"]
            return self._api("POST", f"/api/wf/{pid}/verify-update", body)

        # --- Backlog tools ---
        if name == "backlog_list":
            pid = args["project_id"]
            query = _backlog_list_query(args)
            qs = f"?{urllib.parse.urlencode(query)}" if query else ""
            return self._api("GET", f"/api/backlog/{pid}{qs}")

        if name == "backlog_get":
            pid = args["project_id"]
            bug_id = urllib.parse.quote(str(args["bug_id"]), safe="")
            return self._api("GET", f"/api/backlog/{pid}/{bug_id}")

        if name == "backlog_upsert":
            pid = args["project_id"]
            bug_id = urllib.parse.quote(str(args["bug_id"]), safe="")
            body = {
                key: value
                for key, value in args.items()
                if key not in {"project_id", "bug_id"} and value is not None
            }
            return self._api("POST", f"/api/backlog/{pid}/{bug_id}", body)

        if name == "backlog_close":
            pid = args["project_id"]
            bug_id = urllib.parse.quote(str(args["bug_id"]), safe="")
            body = {
                key: args[key]
                for key in ("commit", "actor", "route_token", "route_waiver", "route_token_waiver")
                if args.get(key)
            }
            return self._api("POST", f"/api/backlog/{pid}/{bug_id}/close", body)

        if name == "task_timeline_append":
            pid = args["project_id"]
            return self._api("POST", f"/api/task/{pid}/timeline", _task_timeline_body(args))

        if name == "task_timeline_list":
            pid = args["project_id"]
            query = _task_timeline_query(args)
            qs = f"?{urllib.parse.urlencode(query)}" if query else ""
            return self._api("GET", f"/api/task/{pid}/timeline{qs}")

        if name == "mf_timeline_precheck":
            pid = args["project_id"]
            bug_id = urllib.parse.quote(str(args["bug_id"]), safe="")
            query = {}
            if "include_events" in args:
                query["include_events"] = "true" if args.get("include_events") else "false"
            if args.get("limit"):
                query["limit"] = str(_int_arg(args, "limit", 1000, minimum=1, maximum=1000))
            qs = f"?{urllib.parse.urlencode(query)}" if query else ""
            return self._api("GET", f"/api/backlog/{pid}/{bug_id}/timeline-gate{qs}")

        if name == "observer_repair_run_plan":
            pid = args["project_id"]
            body = {
                key: value
                for key, value in args.items()
                if key != "project_id" and value is not None
            }
            return self._api("POST", f"/api/projects/{pid}/observer-repair-run/plan", body)

        if name == "backlog_export":
            pid = args["project_id"]
            query = {
                key: args[key]
                for key in ("status", "priority")
                if args.get(key)
            }
            bug_ids = args.get("bug_ids") or []
            if bug_ids:
                query["bug_id"] = ",".join(str(item) for item in bug_ids)
            qs = f"?{urllib.parse.urlencode(query)}" if query else ""
            return self._api("GET", f"/api/backlog/{pid}/portable/export{qs}")

        if name == "backlog_import":
            pid = args["project_id"]
            body = {
                key: args[key]
                for key in ("payload", "on_conflict", "dry_run", "actor")
                if key in args and args[key] is not None
            }
            return self._api("POST", f"/api/backlog/{pid}/portable/import", body)

        # --- Graph governance tools ---
        if name == "graph_status":
            pid = args["project_id"]
            query = {key: args[key] for key in ("target_commit",) if args.get(key)}
            qs = f"?{urllib.parse.urlencode(query)}" if query else ""
            return self._api("GET", f"/api/graph-governance/{pid}/status{qs}")

        if name == "graph_operations_queue":
            pid = args["project_id"]
            query = {}
            for key in ("snapshot_id",):
                if args.get(key):
                    query[key] = args[key]
            for key in ("require_current_semantic", "include_status_observations", "include_resolved"):
                if key in args:
                    query[key] = "true" if args.get(key) else "false"
            qs = f"?{urllib.parse.urlencode(query)}" if query else ""
            return self._api("GET", f"/api/graph-governance/{pid}/operations/queue{qs}")

        if name == "graph_query":
            pid = args["project_id"]
            body = {
                key: value
                for key, value in args.items()
                if key != "project_id" and value is not None
            }
            body.setdefault("actor", "mcp")
            body.setdefault("query_source", "observer")
            body.setdefault("query_purpose", "prompt_context_build")
            return self._api("POST", f"/api/graph-governance/{pid}/query", body)

        if name == "graph_pending_scope_queue":
            pid = args["project_id"]
            body = {
                key: value
                for key, value in args.items()
                if key != "project_id" and value is not None
            }
            return self._api("POST", f"/api/graph-governance/{pid}/pending-scope", body)

        if name == "preflight_check":
            pid = args["project_id"]
            af = "true" if args.get("auto_fix") else "false"
            return self._api("GET", f"/api/wf/{pid}/preflight-check?auto_fix={af}")

        # --- Executor tools ---
        if name == "executor_status":
            result = {}
            if self._svc:
                result = self._svc.status()
            # R9: Include worker pool status if available
            if self._pool:
                pool_status = self._pool.status()
                result.update(pool_status)
            elif hasattr(self._svc, '_worker_pool_status'):
                result.update(self._svc._worker_pool_status())
            if not result:
                return {"mode": "external", "message": "No executor manager configured"}
            return result

        if name == "executor_scale":
            if self._svc:
                workers = args.get("workers", 1)
                if workers == 0:
                    self._svc.stop()
                    return {"action": "stopped"}
                else:
                    self._svc.start()
                    return self._svc.status()
            if self._pool:
                return self._pool.scale(args["workers"])
            return {"error": "No executor manager configured"}

        # --- Host operations ---
        if name == "manager_health":
            return self._manager_api("GET", "/api/manager/health")

        if name == "manager_start":
            if args.get("takeover"):
                return {
                    "ok": False,
                    "error": "takeover_not_supported_from_mcp",
                    "message": "scripts/start-manager.ps1 -Takeover can terminate MCP server processes; run takeover from an external ops shell.",
                }
            health = self._manager_api("GET", "/api/manager/health")
            if health.get("ok"):
                return {"ok": True, "action": "already_running", "manager": health}
            wait_seconds = int(args.get("health_wait_seconds") or 90)
            wait_seconds = max(5, min(wait_seconds, 300))
            started = self._start_manager(wait_seconds)
            started["previous_health"] = health
            return started

        if name == "governance_redeploy":
            pid = args["project_id"]
            chain_version = args.get("chain_version") or self._git_head(short=True)
            if not chain_version:
                return {"ok": False, "error": "missing_chain_version"}
            result = self._manager_api(
                "POST",
                "/api/manager/redeploy/governance",
                {"chain_version": chain_version},
            )
            if result.get("ok") and args.get("sync_version", True):
                head = self._git_head(short=False)
                if head:
                    result["version_sync"] = self._api(
                        "POST",
                        f"/api/version-sync/{pid}",
                        {"git_head": head, "dirty_files": self._git_dirty_files()},
                    )
            return result

        if name == "executor_respawn":
            chain_version = args.get("chain_version") or self._git_head(short=True)
            body = {"chain_version": chain_version} if chain_version else {}
            return self._manager_api("POST", "/api/manager/respawn-executor", body)

        if name == "runtime_status":
            pid = args["project_id"]
            governance = _governance_offline_hint(self._api("GET", "/api/health"))
            manager = self._manager_api("GET", "/api/manager/health")
            version = _governance_offline_hint(self._api("GET", f"/api/version-check/{pid}"))
            classification = _runtime_status_classification(governance, manager, version)
            status = {
                **classification,
                "project_id": pid,
                "governance": governance,
                "manager": manager,
                "version_check": version,
                "recommended_actions": [],
            }
            if isinstance(version, dict):
                target_version = version.get("target_project_version")
                if not isinstance(target_version, dict):
                    target_version = {
                        "project_id": pid,
                        "project_root": version.get("project_root") or version.get("target_project_root", ""),
                        "head": version.get("target_head") or version.get("head", ""),
                        "chain_version": version.get("target_chain_version") or version.get("chain_version", ""),
                        "dirty": bool(version.get("target_dirty", version.get("dirty", False))),
                        "dirty_files": version.get("target_dirty_files") or version.get("dirty_files", []),
                        "synced_with_governance": bool(version.get("target_synced_with_governance", False)),
                        "governance_synced_head": version.get("governance_synced_head", ""),
                        "git_synced_at": version.get("git_synced_at", ""),
                    }
                governance_runtime = version.get("governance_runtime")
                if not isinstance(governance_runtime, dict):
                    governance_runtime = {
                        "chain_version": version.get("governance_chain_version", ""),
                        "gov_runtime_version": version.get("gov_runtime_version", ""),
                        "sm_runtime_version": version.get("sm_runtime_version", ""),
                        "runtime_match": bool(version.get("runtime_match", False)),
                    }
                status["target_project_version"] = target_version
                status["governance_runtime"] = {
                    "health_version": governance.get("version", "") if isinstance(governance, dict) else "",
                    **governance_runtime,
                }
                status["governance_runtime_version"] = status["governance_runtime"]
            if governance.get("governance_online") is False:
                status["recommended_actions"].append("start_governance")
            elif governance.get("status") != "ok":
                status["recommended_actions"].append("governance_redeploy")
            if not manager.get("ok"):
                status["recommended_actions"].append("advanced_chain_ops_manager_start")
            if version.get("dirty"):
                status["recommended_actions"].append("commit_or_stash_dirty_files")
            if not version.get("runtime_match"):
                status["recommended_actions"].append("advanced_chain_ops_redeploy_or_restart")
            return status

        # --- Contract Templates ---
        if name in {"contract_template_list", "contract_template_get", "contract_template_resolve"}:
            from agent.governance.contract_template_registry import (
                ContractTemplateError,
                get_contract_template,
                list_contract_templates,
                resolve_contract_template,
            )

            try:
                if name == "contract_template_list":
                    return {
                        "ok": True,
                        "templates": list_contract_templates(
                            task_type=args.get("task_type"),
                            stage=args.get("stage"),
                        ),
                    }
                if name == "contract_template_get":
                    return {"ok": True, "template": get_contract_template(str(args["template_id"]))}
                return {
                    "ok": True,
                    "template": resolve_contract_template(
                        template_id=args.get("template_id"),
                        task_type=args.get("task_type"),
                        stage=args.get("stage"),
                        version=args.get("version"),
                    ),
                }
            except ContractTemplateError as exc:
                return {"ok": False, "error": str(exc)}

        if name == "ue_audit_validate":
            from agent.governance.ue_audit_contract import validate_ue_audit_payload

            payload = args.get("payload")
            if not isinstance(payload, dict):
                return {"ok": False, "errors": ["payload must be an object"]}
            return validate_ue_audit_payload(payload)

        if name in {
            "review_pack_list",
            "review_pack_get",
            "review_pack_resolve",
            "review_pack_validate_output",
        }:
            from agent.governance.review_contracts import (
                ReviewContractError,
                get_review_pack,
                list_review_packs,
                resolve_review_pack,
                validate_review_output,
            )

            try:
                if name == "review_pack_list":
                    return {
                        "ok": True,
                        "review_packs": list_review_packs(
                            task_type=args.get("task_type"),
                            stage=args.get("stage"),
                        ),
                    }
                if name == "review_pack_get":
                    return {
                        "ok": True,
                        "review_pack": get_review_pack(str(args["template_id"])),
                    }
                if name == "review_pack_resolve":
                    return {
                        "ok": True,
                        "review_pack": resolve_review_pack(
                            template_id=args.get("template_id"),
                            task_type=args.get("task_type"),
                            stage=args.get("stage"),
                            version=args.get("version"),
                        ),
                    }
                payload = args.get("payload")
                if not isinstance(payload, dict):
                    return {"ok": False, "errors": ["payload must be an object"]}
                return validate_review_output(str(args.get("template_id") or ""), payload)
            except ReviewContractError as exc:
                return {"ok": False, "error": str(exc)}

        if name in {
            "context_pack_list",
            "context_pack_get",
            "context_pack_upsert",
            "context_pack_resolve",
            "context_pack_seed_private_file",
        }:
            from agent.governance import context_registry
            from agent.governance.db import get_connection, sqlite_write_lock

            pid = str(args.get("project_id") or "")
            if not pid:
                return {"ok": False, "error": "project_id required"}
            conn = get_connection(pid)
            try:
                if name == "context_pack_list":
                    packs = context_registry.list_context_packs(
                        conn,
                        project_id=pid,
                        role=str(args.get("role") or "observer"),
                        visibility=args.get("visibility"),
                        include_body=bool(args.get("include_body", False)),
                    )
                    return {"ok": True, "project_id": pid, "context_packs": packs, "count": len(packs)}
                if name == "context_pack_get":
                    pack = context_registry.get_context_pack(
                        conn,
                        project_id=pid,
                        pack_id=str(args.get("pack_id") or ""),
                        role=str(args.get("role") or "observer"),
                        include_body=bool(args.get("include_body", False)),
                    )
                    if pack is None:
                        return {"ok": False, "error": "context_pack_not_found", "pack_id": args.get("pack_id")}
                    return {"ok": True, "project_id": pid, "context_pack": pack}
                if name == "context_pack_upsert":
                    with sqlite_write_lock():
                        pack = context_registry.upsert_context_pack(
                            conn,
                            project_id=pid,
                            pack_id=str(args.get("pack_id") or ""),
                            title=str(args.get("title") or ""),
                            visibility=str(args.get("visibility") or context_registry.VISIBILITY_PUBLIC_SKILL),
                            allowed_roles=args.get("allowed_roles"),
                            mode_scope=args.get("mode_scope"),
                            project_scope=str(args.get("project_scope") or ""),
                            backlog_id=str(args.get("backlog_id") or ""),
                            source_type=str(args.get("source_type") or "local_db"),
                            source_path=str(args.get("source_path") or ""),
                            summary=str(args.get("summary") or ""),
                            body=str(args.get("body") or ""),
                            version=str(args.get("version") or "v1"),
                            no_export=args.get("no_export"),
                            enabled=args.get("enabled", True),
                            created_by=str(args.get("created_by") or ""),
                        )
                    return {"ok": True, "project_id": pid, "context_pack": pack}
                if name == "context_pack_seed_private_file":
                    with sqlite_write_lock():
                        pack = context_registry.seed_private_context_from_file(
                            conn,
                            project_id=pid,
                            source_path=str(args.get("source_path") or ""),
                            pack_id=str(args.get("pack_id") or "private_founder_paradigm.v1"),
                            title=str(args.get("title") or "Private founder judgment context"),
                            summary=str(
                                args.get("summary")
                                or "Private observer-only context imported from a local evidence file."
                            ),
                            created_by=str(args.get("created_by") or ""),
                        )
                    return {"ok": True, "project_id": pid, "context_pack": pack}
                return context_registry.resolve_context(
                    conn,
                    project_id=pid,
                    role=str(args.get("role") or ""),
                    mode=str(args.get("mode") or ""),
                    backlog_id=str(args.get("backlog_id") or ""),
                    requested_by=str(args.get("requested_by") or ""),
                    include_body=bool(args.get("include_body", True)),
                    record_resolution=bool(args.get("record_resolution", True)),
                )
            except context_registry.ContextRegistryError as exc:
                return {"ok": False, "error": str(exc)}

        # --- System ---
        if name == "health":
            return _governance_offline_hint(self._api("GET", "/api/health"))

        if name == "version_check":
            pid = args["project_id"]
            # Get chain_version from governance DB
            result = _governance_offline_hint(self._api("GET", f"/api/version-check/{pid}"))
            governance_head = str(result.get("governance_synced_head") or "")
            if governance_head:
                result.setdefault("governance_synced_head", governance_head)
            # Enrich with git dirty check (MCP runs on host, has git)
            has_target_root = bool(result.get("target_project_root") or result.get("project_root"))
            workspace = str(
                result.get("target_project_root")
                or result.get("project_root")
                or self._workspace
            )
            governance_runtime = result.get("governance_runtime")
            if not isinstance(governance_runtime, dict):
                governance_runtime = {}
            governance_root = str(governance_runtime.get("project_root") or "")
            root_source = str(result.get("target_project_root_source") or "")
            external_target_root = bool(
                has_target_root
                and (
                    (
                        governance_root
                        and os.path.realpath(workspace) != os.path.realpath(governance_root)
                    )
                    or (
                        not governance_root
                        and pid != "aming-claw"
                        and root_source in {"explicit_project", "registered_project"}
                    )
                )
            )
            try:
                head = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"],
                    cwd=workspace, timeout=5
                ).decode().strip()
                result["mcp_workspace_root"] = workspace
                result["mcp_workspace_head"] = head
                dirty = subprocess.check_output(
                    ["git", "diff", "--name-only"],
                    cwd=workspace, timeout=5
                ).decode().strip()
                probe = {
                    "root": workspace,
                    "head": head,
                    "dirty": bool(dirty),
                    "dirty_files": [f for f in dirty.splitlines() if f.strip()],
                }
                result["mcp_workspace_probe"] = probe
                if has_target_root:
                    result.setdefault("target_head", head)
                    result.setdefault("head", result.get("target_head") or head)
                if dirty:
                    if has_target_root:
                        result["target_dirty"] = True
                        result["target_dirty_files"] = probe["dirty_files"]
                        result["dirty"] = True
                        result["dirty_files"] = probe["dirty_files"]
                        result["ok"] = False
                        result["message"] = (result.get("message", "") + "; " if result.get("message") else "") + f"{len(probe['dirty_files'])} uncommitted files"
                # Also check HEAD vs chain_version
                # B35: normalize short/full hash mismatch — short is a prefix of full.
                chain_ver = result.get("target_chain_version") or result.get("chain_version", "")
                if (has_target_root and chain_ver and chain_ver != "(not set)"
                    and not (head.startswith(chain_ver) or chain_ver.startswith(head))):
                    result["ok"] = False
                    commits = subprocess.check_output(
                        ["git", "log", "--oneline", f"{chain_ver}..HEAD"],
                        cwd=workspace, timeout=5
                    ).decode().strip().splitlines()
                    result["commits_since_chain"] = len(commits)
                    result["message"] = (
                        (result.get("message", "") + "; " if result.get("message") else "")
                        + f"MCP workspace HEAD ({head}) != CHAIN_VERSION ({chain_ver}); "
                        + f"{len(commits)} manual commits"
                    )
                if has_target_root and governance_head and governance_head != head:
                    result["target_synced_with_governance"] = False
                    result["governance_sync_diagnostics"] = {
                        "mismatch": True,
                        "governance_synced_head": governance_head,
                        "target_head": head,
                        "target_project_root": workspace,
                        "external_target_root": external_target_root,
                        "affects_ok": not external_target_root,
                    }
                    if not external_target_root:
                        result["ok"] = False
                        result["message"] = (
                            (result.get("message", "") + "; " if result.get("message") else "")
                            + f"governance synced HEAD ({governance_head}) differs from MCP workspace HEAD ({head})"
                        )
                elif has_target_root and governance_head:
                    result["target_synced_with_governance"] = True
                    result["governance_sync_diagnostics"] = {
                        "mismatch": False,
                        "governance_synced_head": governance_head,
                        "target_head": head,
                        "target_project_root": workspace,
                        "external_target_root": external_target_root,
                        "affects_ok": False,
                    }
            except Exception:
                pass  # fail-open if git unavailable
            return result

        # --- Telegram ---
        if name == "telegram_send":
            return self._send_telegram(args["chat_id"], args["text"])

        raise ValueError(f"Unknown tool: {name!r}")

    def _default_manager_api(self, method: str, path: str, data: dict | None = None) -> dict:
        """HTTP helper for manager sidecar when the MCP server did not inject one."""
        manager_url = os.environ.get("MANAGER_URL", "http://127.0.0.1:40101").rstrip("/")
        url = f"{manager_url}{path}"
        try:
            if data is not None:
                body = json.dumps(data).encode()
                req = urllib.request.Request(
                    url,
                    data=body,
                    method=method,
                    headers={"Content-Type": "application/json"},
                )
            else:
                req = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode() if exc.fp else ""
            try:
                return json.loads(raw)
            except Exception:
                return {"ok": False, "error": str(exc), "body": raw}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _git_head(self, short: bool) -> str:
        args = ["git", "rev-parse"]
        if short:
            args.append("--short")
        args.append("HEAD")
        try:
            return subprocess.check_output(args, cwd=self._workspace, timeout=5).decode().strip()
        except Exception:
            return ""

    def _git_dirty_files(self) -> list[str]:
        try:
            dirty = subprocess.check_output(
                ["git", "diff", "--name-only"],
                cwd=self._workspace,
                timeout=5,
            ).decode().strip()
        except Exception:
            return []
        return [line for line in dirty.splitlines() if line.strip()]

    def _start_manager(self, health_wait_seconds: int) -> dict:
        if sys.platform == "win32":
            script_name = "start-manager.ps1"
            script = os.path.join(self._workspace, "scripts", script_name)
            cmd = [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script,
                "-HealthWaitSeconds",
                str(health_wait_seconds),
            ]
            missing_error = "start_manager_script_missing"
        else:
            script_name = "start-manager.sh"
            script = posixpath.join(str(self._workspace).replace("\\", "/").rstrip("/"), "scripts", script_name)
            cmd = [
                "bash",
                script,
                "--health-wait-seconds",
                str(health_wait_seconds),
            ]
            missing_error = "start_manager_posix_script_missing"
        if not os.path.exists(script):
            return {"ok": False, "error": missing_error, "script": script, "platform": sys.platform}
        try:
            proc = subprocess.run(
                cmd,
                cwd=self._workspace,
                capture_output=True,
                text=True,
                timeout=health_wait_seconds + 30,
            )
        except FileNotFoundError as exc:
            return {
                "ok": False,
                "error": "manager_start_launcher_not_found",
                "detail": str(exc),
                "command": cmd[:1],
                "platform": sys.platform,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "error": "manager_start_timeout",
                "stdout": (exc.stdout or "")[-2000:],
                "stderr": (exc.stderr or "")[-2000:],
            }

        health = self._manager_api("GET", "/api/manager/health")
        return {
            "ok": bool(proc.returncode == 0 and health.get("ok")),
            "action": "manager_start",
            "script": script_name,
            "platform": sys.platform,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-4000:],
            "stderr": (proc.stderr or "")[-4000:],
            "manager": health,
        }

    def _send_telegram(self, chat_id: str, text: str) -> dict:
        """Send message directly via Telegram Bot API."""
        import os
        import urllib.request
        import urllib.error
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            return {"error": "TELEGRAM_BOT_TOKEN not set"}
        import json as _json
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        body = _json.dumps({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return _json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return {"error": str(exc), "body": exc.read().decode()[:200]}
        except Exception as exc:
            return {"error": str(exc)}
