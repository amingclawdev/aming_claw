"""Aming Claw MCP Server — Worker Pool + Event Push.

Single entry point that manages:
  - Worker pool (Claude CLI task execution)
  - Event bridge (Redis → MCP notifications)
  - MCP tools (task/workflow/executor management)

Talks to existing governance HTTP API. Does NOT replace it.

Usage:
    python -m agent.mcp.server --project aming-claw
    python -m agent.mcp.server --project aming-claw --workers 3
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

# Ensure agent package is importable
_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from mcp.tools import TOOLS, ToolDispatcher
from mcp.executor import WorkerPool
from mcp.events import EventBridge

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP protocol constants
# ---------------------------------------------------------------------------
PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "aming-claw"
SERVER_VERSION = "1.1.0"

RESOURCE_FILES: dict[str, tuple[str, str, str]] = {
    "aming-claw://skill": (
        "Aming Claw Skill",
        "skills/aming-claw/SKILL.md",
        "text/markdown",
    ),
    "aming-claw://graph-first": (
        "Graph-first Playbook",
        "skills/aming-claw/references/graph-first.md",
        "text/markdown",
    ),
    "aming-claw://mcp-tools": (
        "MCP Tools Guide",
        "skills/aming-claw/references/mcp-tools.md",
        "text/markdown",
    ),
    "aming-claw://mf-sop": (
        "Manual Fix SOP",
        "skills/aming-claw/references/mf-sop.md",
        "text/markdown",
    ),
    "aming-claw://plugin-packaging": (
        "Plugin Packaging Guide",
        "skills/aming-claw/references/plugin-packaging.md",
        "text/markdown",
    ),
    "aming-claw://seed-graph-summary": (
        "Seed Graph Summary",
        "agent/mcp/resources/seed-graph-summary.json",
        "application/json",
    ),
    "aming-claw://self-graph-bundle-manifest": (
        "Self Graph Bundle Manifest",
        "agent/mcp/resources/self-graph-bundle-manifest.json",
        "application/json",
    ),
}

# JSON-RPC error codes
PARSE_ERROR = -32700
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603

# ---------------------------------------------------------------------------
# Thread-safe stdio transport
# ---------------------------------------------------------------------------
_stdout_lock = threading.Lock()


def _git_status(workspace: str) -> dict:
    """Get git HEAD and dirty status from workspace."""
    import subprocess
    from datetime import datetime, timezone
    result = {"head": "unknown", "dirty": False, "dirty_files": [],
              "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=workspace, timeout=10
        ).decode().strip()
        result["head"] = head
    except Exception:
        pass
    try:
        diff = subprocess.check_output(
            ["git", "diff", "--name-only"],
            cwd=workspace, timeout=10
        ).decode().strip()
        if diff:
            result["dirty"] = True
            result["dirty_files"] = [f for f in diff.splitlines() if f.strip()]
    except Exception:
        pass
    return result


def _write(msg: dict) -> None:
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
    _write({"jsonrpc": "2.0", "method": method, "params": params})


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

class AmingClawMCP:
    """MCP Server main class."""

    def __init__(self, project_id: str, governance_url: str, workspace: str,
                 redis_url: str, manager_url: str = "http://127.0.0.1:40101",
                 max_workers: int = 0, autostart_executor: bool = False,
                 enable_events: bool = False):
        self.project_id = project_id
        self.gov_url = governance_url.rstrip("/")
        self.manager_url = manager_url.rstrip("/")
        self._workspace = workspace
        self._autostart_executor = autostart_executor
        self._enable_events = enable_events

        # Worker pool — only if explicitly requested (default: 0 = no workers)
        # Executor should run as independent process to avoid blocking MCP stdio
        self.worker_pool = None
        if max_workers > 0:
            self.worker_pool = WorkerPool(
                governance_url=governance_url,
                project_id=project_id,
                workspace=workspace,
                max_workers=max_workers,
                on_event=self._on_worker_event,
            )

        # Event bridge (Redis → MCP notifications)
        self.event_bridge = None
        if self._enable_events:
            self.event_bridge = EventBridge(
                redis_url=redis_url,
                notify_fn=self._on_redis_event,
            )

        # Service manager is only constructed when this MCP session explicitly
        # owns executor lifecycle. Ad-hoc MCP sessions should stay read/control
        # only and must not have any path that can accidentally spawn a worker.
        self.service_mgr = None
        if self._autostart_executor:
            from service_manager import ServiceManager
            self.service_mgr = ServiceManager(
                project_id=project_id,
                governance_url=governance_url,
                executor_cmd=[
                    sys.executable, str(Path(__file__).resolve().parents[1] / "executor_worker.py"),
                    "--project", project_id,
                    "--url", governance_url,
                    "--workspace", workspace,
                ],
            )

        # Tool dispatcher (worker_pool may be None — executor tools return status only)
        self.dispatcher = ToolDispatcher(
            api_fn=self._http,
            worker_pool=self.worker_pool,
            service_mgr=self.service_mgr,
            manager_api_fn=self._manager_http,
            workspace=workspace,
        )

    # -----------------------------------------------------------------------
    # MCP resources
    # -----------------------------------------------------------------------

    def _resources_list(self) -> dict:
        resources = []
        for uri, (name, rel_path, mime_type) in RESOURCE_FILES.items():
            resources.append({
                "uri": uri,
                "name": name,
                "description": f"Aming Claw operating guidance from {rel_path}.",
                "mimeType": mime_type,
            })
        resources.append({
            "uri": "aming-claw://current-context",
            "name": "Current Aming Claw Context",
            "description": "Project id, governance URL, manager URL, dashboard URL, workspace, and safe first actions.",
            "mimeType": "text/markdown",
        })
        return {"resources": resources}

    def _resource_templates_list(self) -> dict:
        return {
            "resourceTemplates": [
                {
                    "uriTemplate": "aming-claw://project/{project_id}/context",
                    "name": "Project Context",
                    "description": "Runtime-safe startup context for an Aming Claw project id.",
                    "mimeType": "text/markdown",
                },
            ],
        }

    def _read_resource_text(self, uri: str) -> str:
        if uri == "aming-claw://current-context":
            return self._current_context_text(None)
        prefix = "aming-claw://project/"
        suffix = "/context"
        if uri.startswith(prefix) and uri.endswith(suffix):
            project_id = uri[len(prefix):-len(suffix)]
            return self._current_context_text(project_id or None)
        spec = RESOURCE_FILES.get(uri)
        if not spec:
            raise ValueError(f"Unknown resource URI: {uri}")
        _, rel_path, _ = spec
        candidates = [
            Path(self._workspace).resolve(),
            Path(__file__).resolve().parents[2],
        ]
        for root in candidates:
            path = (root / rel_path).resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"Resource escapes workspace: {uri}") from exc
            if path.exists():
                return path.read_text(encoding="utf-8")
        raise FileNotFoundError(f"Resource file not found for {uri}: {rel_path}")

    def _resource_mime_type(self, uri: str) -> str:
        if uri == "aming-claw://current-context" or (uri.startswith("aming-claw://project/") and uri.endswith("/context")):
            return "text/markdown"
        spec = RESOURCE_FILES.get(uri)
        return spec[2] if spec else "text/plain"

    def _current_context_text(self, requested_project_id: str | None) -> str:
        context = self._resolve_context_project(requested_project_id)
        project_id = context["active_project_id"]
        health = self._request_json("GET", f"{self.gov_url}/api/health", timeout=2)
        version = self._request_json("GET", f"{self.gov_url}/api/version-check/{project_id}", timeout=2)
        graph = self._request_json("GET", f"{self.gov_url}/api/graph-governance/{project_id}/status", timeout=2)
        ops = self._request_json("GET", f"{self.gov_url}/api/graph-governance/{project_id}/operations/queue", timeout=2)
        backlog = self._request_json("GET", f"{self.gov_url}/api/backlog/{project_id}?status=OPEN", timeout=2)
        graph_missing = context["graph_missing"]
        if not graph.get("error"):
            graph_missing = graph_missing or not str(graph.get("active_snapshot_id") or "").strip()
        dashboard_view = "projects" if graph_missing else "graph"
        dashboard_url = f"{self.gov_url}/dashboard?project_id={project_id}&view={dashboard_view}"
        dashboard_graph_url = f"{self.gov_url}/dashboard?project_id={project_id}&view=graph"
        health_line = self._format_context_health(health)
        version_line = self._format_context_version(version)
        graph_line = self._format_context_graph(graph)
        ops_line = self._format_context_ops(ops)
        backlog_line = self._format_context_backlog(backlog)
        project_note = self._format_context_project_note(context)
        primary_actions = self._context_primary_actions(
            context=context,
            health=health,
            graph=graph,
            dashboard_url=dashboard_url,
        )
        project_note_lines = [f"- selected_project_note: {project_note}"] if project_note else []
        action_lines = [
            f"{idx}. **{label}** - {detail}"
            for idx, (label, detail) in enumerate(primary_actions, start=1)
        ]
        return "\n".join([
            "# Aming Claw Current Context",
            "",
            f"- project_id: `{project_id}`",
            f"- default_project_id: `{context['default_project_id']}`",
            f"- workspace_project_id: `{context['workspace_project_id'] or '-'}`",
            f"- dashboard_project_id: `{context['dashboard_project_id'] or '-'}`",
            f"- active_project_id: `{project_id}`",
            f"- context_source: `{context['context_source']}`",
            f"- governance_url: `{self.gov_url}`",
            f"- manager_url: `{self.manager_url}`",
            f"- dashboard_url: `{dashboard_url}`",
            f"- dashboard_graph_url: `{dashboard_graph_url}`",
            f"- workspace: `{self._workspace}`",
            f"- health: {health_line}",
            f"- version: {version_line}",
            f"- graph: {graph_line}",
            f"- operations_queue: {ops_line}",
            f"- backlog: {backlog_line}",
            *project_note_lines,
            "",
            "## Primary Next Actions",
            "",
            *action_lines,
            "",
            "## Guardrails",
            "",
            "- Read `aming-claw://skill` before mutations.",
            "- Call `graph_query` with `tool=query_schema` before broad filesystem scans.",
            "- File or update a backlog row before code, docs, config, dashboard, runtime, or graph mutations.",
            "- Use browser-use/dashboard as the shared visual control plane when available.",
            "- If governance is offline, ask the user to run `aming-claw start` or open the launcher; do not silently start services.",
            "",
        ])

    def _resolve_context_project(self, requested_project_id: str | None) -> dict[str, Any]:
        default_project_id = self.project_id
        dashboard_project_id = (requested_project_id or os.getenv("AMING_CLAW_DASHBOARD_PROJECT") or "").strip()
        projects_payload = self._request_json("GET", f"{self.gov_url}/api/projects", timeout=2)
        projects = projects_payload.get("projects") if isinstance(projects_payload, dict) else None
        if not isinstance(projects, list):
            projects = []
        workspace_match_project_id = self._project_id_from_workspace_registry(projects)
        workspace_config_project_id = self._project_id_from_workspace_config()
        workspace_project_id = workspace_match_project_id or workspace_config_project_id

        if dashboard_project_id:
            active_project_id = dashboard_project_id
            context_source = "resource_uri" if requested_project_id else "dashboard_env"
        elif workspace_project_id:
            active_project_id = workspace_project_id
            context_source = "workspace_match" if workspace_match_project_id else "workspace_config"
        else:
            active_project_id = default_project_id
            context_source = "default_project_id"

        project_entry = next(
            (p for p in projects if isinstance(p, dict) and str(p.get("project_id") or "") == active_project_id),
            None,
        )
        registered = project_entry is not None
        graph_missing = False
        if not registered and projects:
            graph_missing = True
        elif project_entry is not None and not str(project_entry.get("active_snapshot_id") or "").strip():
            graph_missing = True

        return {
            "default_project_id": default_project_id,
            "workspace_project_id": workspace_project_id,
            "dashboard_project_id": dashboard_project_id,
            "active_project_id": active_project_id,
            "context_source": context_source,
            "project_registered": registered,
            "graph_missing": graph_missing,
        }

    def _project_id_from_workspace_registry(self, projects: list[dict]) -> str:
        workspace = self._normalized_workspace_path(self._workspace)
        if not workspace:
            return ""
        best = ""
        best_len = -1
        for project in projects:
            if not isinstance(project, dict):
                continue
            raw_path = str(project.get("workspace_path") or "").strip()
            project_path = self._normalized_workspace_path(raw_path)
            if not project_path:
                continue
            # Prefer the most specific registered workspace that contains the
            # MCP cwd, so sessions opened in a subdirectory still resolve to
            # the intended project.
            if workspace == project_path or workspace.startswith(project_path + os.sep):
                if len(project_path) > best_len:
                    best = str(project.get("project_id") or "").strip()
                    best_len = len(project_path)
        return best

    def _project_id_from_workspace_config(self) -> str:
        try:
            workspace = Path(self._workspace).resolve()
        except Exception:
            return ""
        for root in [workspace, *workspace.parents]:
            config = root / ".aming-claw.yaml"
            if not config.exists():
                continue
            try:
                text = config.read_text(encoding="utf-8")
            except Exception:
                return ""
            match = re.search(r"(?m)^\s*project_id\s*:\s*['\"]?([^'\"\s#]+)", text)
            return match.group(1).strip() if match else ""
        return ""

    @staticmethod
    def _normalized_workspace_path(path: str) -> str:
        if not path:
            return ""
        try:
            resolved = str(Path(path).resolve())
        except Exception:
            resolved = path
        return os.path.normcase(os.path.normpath(resolved))

    @staticmethod
    def _format_context_health(payload: dict) -> str:
        if payload.get("error"):
            return f"`unavailable` ({payload['error']})"
        status = payload.get("status") or payload.get("ok") or "unknown"
        version = payload.get("version") or payload.get("runtime_version") or "-"
        return f"`{status}` version `{version}`"

    @staticmethod
    def _format_context_version(payload: dict) -> str:
        if payload.get("error"):
            return f"`unavailable` ({payload['error']})"
        head = str(payload.get("head") or "-")[:7]
        dirty = payload.get("dirty")
        runtime_match = payload.get("runtime_match")
        return f"HEAD `{head}` dirty `{dirty}` runtime_match `{runtime_match}`"

    @staticmethod
    def _format_context_graph(payload: dict) -> str:
        if payload.get("error"):
            return f"`unavailable` ({payload['error']})"
        snapshot_id = payload.get("active_snapshot_id") or "-"
        state = payload.get("current_state") or {}
        stale = (state.get("graph_stale") or {}).get("is_stale")
        pending = payload.get("pending_scope_reconcile_count")
        return f"snapshot `{snapshot_id}` stale `{stale}` pending_scope `{pending}`"

    @staticmethod
    def _format_context_ops(payload: dict) -> str:
        if payload.get("error"):
            return f"`unavailable` ({payload['error']})"
        return f"count `{payload.get('count', '-')}`"

    @staticmethod
    def _format_context_backlog(payload: dict) -> str:
        if payload.get("error"):
            return f"`unavailable` ({payload['error']})"
        count = payload.get("count")
        if count is None and isinstance(payload.get("bugs"), list):
            count = len(payload["bugs"])
        return f"open `{count if count is not None else '-'}`"

    @staticmethod
    def _format_context_project_note(context: dict[str, Any]) -> str:
        active = context.get("active_project_id")
        default = context.get("default_project_id")
        if active and default and active != default:
            return (
                f"active project `{active}` differs from default `{default}`; "
                "use the active project for graph and backlog actions."
            )
        return ""

    @staticmethod
    def _context_graph_state(context: dict[str, Any], graph: dict) -> str:
        if graph.get("error"):
            return "unknown"
        if context.get("graph_missing") or not str(graph.get("active_snapshot_id") or "").strip():
            return "missing"
        state = graph.get("current_state") or {}
        stale = (state.get("graph_stale") or {}).get("is_stale")
        if stale is True:
            return "stale"
        if stale is False:
            return "current"
        return "unknown"

    @classmethod
    def _context_primary_actions(
        cls,
        *,
        context: dict[str, Any],
        health: dict,
        graph: dict,
        dashboard_url: str,
    ) -> list[tuple[str, str]]:
        if health.get("error"):
            return [
                ("Start Services", "run `aming-claw launcher` then `aming-claw start`; MCP will not auto-start services."),
                ("Read Seed Graph", "open `aming-claw://seed-graph-summary` for packaged project context."),
                ("Check Current Project Status", "read this resource again after governance is online."),
            ]

        graph_state = cls._context_graph_state(context, graph)
        if graph_state == "missing":
            return [
                ("Initialize Project", f"open `{dashboard_url}` and bootstrap or build the graph."),
                ("Check Current Project Status", "confirm active project, runtime, graph, queue, and backlog before analysis."),
                ("Explain Graph Concepts", "ask for a short explanation of graph, node, edge, snapshot, and backlog."),
            ]
        if graph_state == "stale":
            return [
                ("Update Graph", f"open `{dashboard_url}` and run the visible graph update action."),
                ("Check Current Project Status", "review graph stale/current, operations queue, and open backlog."),
                ("Find PR Opportunities", "after graph is current, rank candidates by graph evidence."),
            ]
        return [
            ("Check Current Project Status", "summarize runtime, graph, operations queue, semantic/review state, and backlog."),
            ("Find PR Opportunities", "use graph-native queries for high fan-out, low coverage, docs/tests gaps, and risky edges."),
            ("Explain Graph Concepts", "give the user a compact graph/node/edge/snapshot/backlog walkthrough."),
        ]

    def run(self) -> None:
        """Start services, enter stdin read loop, shutdown on EOF."""
        log.info("Starting Aming Claw MCP Server (project=%s, gov=%s)",
                 self.project_id, self.gov_url)

        # Start subsystems
        if self.worker_pool:
            self.worker_pool.start()
        if self.event_bridge:
            try:
                self.event_bridge.start()
            except Exception:
                log.exception("EventBridge failed to start; continuing without event notifications")
                self.event_bridge = None
        # Note: :40020 HTTP removed — executor syncs git status via governance API

        # Optional host-side executor ownership. Default off so ad-hoc MCP
        # sessions do not accidentally spawn duplicate queue consumers.
        if self._autostart_executor:
            self.service_mgr.start()
            log.info("Executor subprocess started via ServiceManager")
        else:
            log.info("Executor autostart disabled for this MCP session")

        # Read stdin (JSON-RPC messages from Claude Code)
        try:
            for raw in sys.stdin:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    self._handle(raw)
                except Exception:
                    log.exception("Error handling message: %s", raw[:200])
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()

    def _start_http_api(self):
        """Lightweight HTTP API on :40020 for Docker services to query git status."""
        from http.server import HTTPServer, BaseHTTPRequestHandler
        workspace = self._workspace

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self_):
                if self_.path == "/git-status":
                    result = _git_status(workspace)
                    body = json.dumps(result).encode()
                    self_.send_response(200)
                    self_.send_header("Content-Type", "application/json")
                    self_.end_headers()
                    self_.wfile.write(body)
                else:
                    self_.send_response(404)
                    self_.end_headers()

            def log_message(self_, *args):
                pass  # suppress HTTP access logs

        try:
            http = HTTPServer(("0.0.0.0", 40020), Handler)
            t = threading.Thread(target=http.serve_forever, daemon=True)
            t.start()
            log.info("HTTP API listening on :40020 (/git-status)")
        except OSError as e:
            log.warning("Could not bind :40020: %s (git-status unavailable)", e)

    def _shutdown(self) -> None:
        log.info("Shutting down MCP server...")
        if self.service_mgr:
            self.service_mgr.stop()
        if self.event_bridge:
            self.event_bridge.stop()
        if self.worker_pool:
            self.worker_pool.stop(timeout=30)
        log.info("MCP server stopped")

    # -----------------------------------------------------------------------
    # HTTP helper (governance API)
    # -----------------------------------------------------------------------

    def _http(self, method: str, path: str, data: dict = None) -> dict:
        url = f"{self.gov_url}{path}"
        return self._request_json(method, url, data, timeout=15)

    def _manager_http(self, method: str, path: str, data: dict = None) -> dict:
        url = f"{self.manager_url}{path}"
        return self._request_json(method, url, data, timeout=90)

    def _request_json(self, method: str, url: str, data: dict = None, timeout: int = 15) -> dict:
        try:
            if data is not None:
                body = json.dumps(data).encode()
                req = urllib.request.Request(url, data=body, method=method,
                                             headers={"Content-Type": "application/json"})
            else:
                req = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode() if exc.fp else ""
            try:
                return json.loads(raw)
            except Exception:
                return {"error": str(exc), "body": raw}
        except Exception as exc:
            return {"error": str(exc)}

    # -----------------------------------------------------------------------
    # Event callbacks
    # -----------------------------------------------------------------------

    def _on_worker_event(self, event_name: str, payload: dict) -> None:
        """Worker pool emits events (gate.blocked, task.created, etc.)."""
        _notification(f"aming-claw/{event_name}", payload)

    def _on_redis_event(self, event_name: str, payload: dict) -> None:
        """Redis bridge forwards governance events as MCP notifications."""
        _notification(f"aming-claw/{event_name}", payload)

    # -----------------------------------------------------------------------
    # JSON-RPC request handler
    # -----------------------------------------------------------------------

    def _handle(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as exc:
            _error_response(None, PARSE_ERROR, f"Parse error: {exc}")
            return

        req_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}

        # --- initialize ---
        if method == "initialize":
            _response(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
            })
            return

        # --- notifications/initialized ---
        if method == "notifications/initialized":
            return

        # --- tools/list ---
        if method == "tools/list":
            _response(req_id, {"tools": TOOLS})
            return

        # --- resources/list ---
        if method == "resources/list":
            _response(req_id, self._resources_list())
            return

        # --- resources/templates/list ---
        if method == "resources/templates/list":
            _response(req_id, self._resource_templates_list())
            return

        # --- resources/read ---
        if method == "resources/read":
            uri = str(params.get("uri") or "")
            try:
                _response(req_id, {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": self._resource_mime_type(uri),
                            "text": self._read_resource_text(uri),
                        },
                    ],
                })
            except ValueError as exc:
                _error_response(req_id, METHOD_NOT_FOUND, str(exc))
            except Exception as exc:
                log.exception("Resource read error: %s", uri)
                _error_response(req_id, INTERNAL_ERROR, str(exc))
            return

        # --- tools/call ---
        if method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments") or {}
            try:
                result = self.dispatcher.dispatch(tool_name, tool_args)
                _response(req_id, {
                    "content": [
                        {"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)},
                    ],
                })
            except ValueError as exc:
                _error_response(req_id, METHOD_NOT_FOUND, str(exc))
            except Exception as exc:
                log.exception("Tool error: %s", tool_name)
                _error_response(req_id, INTERNAL_ERROR, str(exc))
            return

        # --- ping ---
        if method == "ping":
            _response(req_id, {})
            return

        # --- unknown ---
        if req_id is not None:
            _error_response(req_id, METHOD_NOT_FOUND, f"Method not found: {method!r}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Aming Claw MCP Server")
    parser.add_argument("--project", default="aming-claw", help="Project ID")
    parser.add_argument("--governance-url", default=os.getenv("GOVERNANCE_URL", "http://localhost:40000"))
    parser.add_argument("--manager-url", default=os.getenv("MANAGER_URL", "http://127.0.0.1:40101"))
    parser.add_argument("--workspace", default=os.getenv("CODEX_WORKSPACE", str(Path(__file__).resolve().parents[2])))
    parser.add_argument("--redis-url", default=os.getenv("REDIS_URL", "redis://localhost:40079/0"))
    parser.add_argument("--workers", type=int, default=int(os.getenv("MCP_WORKERS", "1")))
    parser.add_argument(
        "--enable-events",
        action="store_true",
        default=os.getenv("MCP_ENABLE_EVENTS", "0") == "1",
        help="Enable optional Redis event notifications over MCP stdio",
    )
    parser.add_argument(
        "--autostart-executor",
        action="store_true",
        default=os.getenv("MCP_AUTOSTART_EXECUTOR", "0") == "1",
        help="Start executor_worker via ServiceManager for this MCP server process",
    )
    args = parser.parse_args()

    log_level_name = os.getenv("MCP_LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        level=getattr(logging, log_level_name, logging.WARNING),
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,  # MCP protocol uses stdout, logs go to stderr
    )

    server = AmingClawMCP(
        project_id=args.project,
        governance_url=args.governance_url,
        workspace=args.workspace,
        redis_url=args.redis_url,
        manager_url=args.manager_url,
        max_workers=args.workers,
        autostart_executor=args.autostart_executor,
        enable_events=args.enable_events,
    )
    server.run()


if __name__ == "__main__":
    main()
