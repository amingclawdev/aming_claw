from __future__ import annotations

from types import SimpleNamespace

from agent.mcp import tools as mcp_tools
from agent.mcp.tools import TOOLS, ToolDispatcher


def _tool_names() -> set[str]:
    return {str(tool.get("name") or "") for tool in TOOLS}


class _Recorder:
    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []

    def api(self, method: str, path: str, data: dict | None = None) -> dict:
        self.calls.append((method, path, data))
        return {"ok": True, "method": method, "path": path, "data": data}


class _RuntimeGovRecorder(_Recorder):
    def api(self, method: str, path: str, data: dict | None = None) -> dict:
        self.calls.append((method, path, data))
        if path == "/api/health":
            return {"status": "ok", "version": "abc1234"}
        if path == "/api/version-check/aming-claw":
            return {
                "ok": True,
                "head": "abc1234",
                "chain_version": "abc1234",
                "dirty": False,
                "runtime_match": True,
                "gov_runtime_version": "abc1234",
                "sm_runtime_version": "abc1234",
            }
        return {"ok": True, "method": method, "path": path, "data": data}


class _RuntimeMismatchGovRecorder(_Recorder):
    def api(self, method: str, path: str, data: dict | None = None) -> dict:
        self.calls.append((method, path, data))
        if path == "/api/health":
            return {"status": "ok", "version": "new1234"}
        if path == "/api/version-check/aming-claw":
            return {
                "ok": False,
                "head": "new1234",
                "chain_version": "old1234",
                "dirty": False,
                "runtime_match": False,
                "gov_runtime_version": "new1234",
                "sm_runtime_version": "old1234",
                "message": "HEAD (new1234) != CHAIN_VERSION (old1234)",
            }
        return {"ok": True, "method": method, "path": path, "data": data}


class _AdvancedRuntimeMismatchGovRecorder(_Recorder):
    def api(self, method: str, path: str, data: dict | None = None) -> dict:
        self.calls.append((method, path, data))
        if path == "/api/health":
            return {"status": "ok", "version": "new1234"}
        if path == "/api/version-check/aming-claw":
            return {
                "ok": True,
                "head": "new1234",
                "chain_version": "new1234",
                "dirty": False,
                "runtime_match": False,
                "gov_runtime_version": "new1234",
                "sm_runtime_version": "old1234",
                "message": "ServiceManager runtime is behind",
            }
        return {"ok": True, "method": method, "path": path, "data": data}


class _OfflineGovRecorder(_Recorder):
    def api(self, method: str, path: str, data: dict | None = None) -> dict:
        self.calls.append((method, path, data))
        return {"ok": False, "error": "<urlopen error timed out>"}


def _dispatcher(recorder: _Recorder, manager: _Recorder | None = None) -> ToolDispatcher:
    return ToolDispatcher(
        api_fn=recorder.api,
        worker_pool=None,
        service_mgr=None,
        manager_api_fn=manager.api if manager else None,
        workspace=".",
    )


def test_active_mcp_exposes_backlog_and_graph_governance_tools():
    names = _tool_names()

    assert {
        "backlog_list",
        "backlog_get",
        "backlog_upsert",
        "backlog_close",
        "task_timeline_append",
        "task_timeline_list",
        "mf_timeline_precheck",
        "backlog_export",
        "backlog_import",
        "graph_status",
        "graph_operations_queue",
        "graph_query",
        "graph_pending_scope_queue",
        "manager_health",
        "manager_start",
        "governance_redeploy",
        "executor_respawn",
        "runtime_status",
        "observer_session_register",
        "observer_session_heartbeat",
        "observer_session_close",
        "observer_session_revoke",
        "observer_command_list",
        "observer_command_enqueue",
        "observer_command_next",
        "observer_command_claim",
        "observer_command_complete",
        "observer_command_fail",
    }.issubset(names)


def test_mcp_backlog_tools_route_to_governance_api():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

    dispatcher.dispatch(
        "backlog_upsert",
        {
            "project_id": "aming-claw",
            "bug_id": "OPT-BACKLOG-MCP-PLUGIN-TOOLS-PARITY",
            "title": "Tool parity",
            "force_admit": True,
        },
    )
    dispatcher.dispatch(
        "backlog_close",
        {
            "project_id": "aming-claw",
            "bug_id": "OPT-BACKLOG-MCP-PLUGIN-TOOLS-PARITY",
            "commit": "abc1234",
        },
    )
    dispatcher.dispatch(
        "backlog_export",
        {
            "project_id": "aming-claw",
            "status": "OPEN",
            "bug_ids": ["BUG-1", "BUG-2"],
        },
    )
    dispatcher.dispatch(
        "backlog_import",
        {
            "project_id": "aming-claw",
            "payload": {"schema": "aming-claw.backlog.export", "rows": []},
            "on_conflict": "skip",
            "dry_run": True,
        },
    )

    assert recorder.calls[0] == (
        "POST",
        "/api/backlog/aming-claw/OPT-BACKLOG-MCP-PLUGIN-TOOLS-PARITY",
        {"title": "Tool parity", "force_admit": True},
    )
    assert recorder.calls[1] == (
        "POST",
        "/api/backlog/aming-claw/OPT-BACKLOG-MCP-PLUGIN-TOOLS-PARITY/close",
        {"commit": "abc1234"},
    )
    assert recorder.calls[2] == (
        "GET",
        "/api/backlog/aming-claw/portable/export?status=OPEN&bug_id=BUG-1%2CBUG-2",
        None,
    )
    assert recorder.calls[3] == (
        "POST",
        "/api/backlog/aming-claw/portable/import",
        {
            "payload": {"schema": "aming-claw.backlog.export", "rows": []},
            "on_conflict": "skip",
            "dry_run": True,
        },
    )


def test_mcp_backlog_list_defaults_to_compact_open_page():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

    dispatcher.dispatch("backlog_list", {"project_id": "aming-claw"})

    assert recorder.calls == [
        (
            "GET",
            "/api/backlog/aming-claw?view=compact&limit=50&offset=0&status=OPEN",
            None,
        )
    ]


def test_mcp_backlog_list_supports_search_and_closed_page():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

    dispatcher.dispatch(
        "backlog_list",
        {
            "project_id": "aming-claw",
            "q": "portable import",
            "limit": 500,
            "offset": 3,
            "include_closed": True,
            "view": "full",
        },
    )

    assert recorder.calls == [
        (
            "GET",
            "/api/backlog/aming-claw?view=full&limit=100&offset=3&q=portable+import&include_closed=true",
            None,
        )
    ]


def test_mcp_timeline_tools_route_to_governance_api():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

    dispatcher.dispatch(
        "task_timeline_append",
        {
            "project_id": "aming-claw",
            "backlog_id": "BUG-1",
            "event_type": "mf.implementation",
            "event_kind": "implementation",
            "status": "accepted",
            "payload": {"changed_files": ["agent/mcp/tools.py"]},
        },
    )
    dispatcher.dispatch(
        "task_timeline_list",
        {
            "project_id": "aming-claw",
            "backlog_id": "BUG-1",
            "event_kind": "implementation",
            "limit": 25,
        },
    )
    dispatcher.dispatch(
        "mf_timeline_precheck",
        {
            "project_id": "aming-claw",
            "bug_id": "BUG-1",
            "include_events": True,
            "limit": 25,
        },
    )

    assert recorder.calls == [
        (
            "POST",
            "/api/task/aming-claw/timeline",
            {
                "backlog_id": "BUG-1",
                "event_type": "mf.implementation",
                "event_kind": "implementation",
                "status": "accepted",
                "payload": {"changed_files": ["agent/mcp/tools.py"]},
            },
        ),
        (
            "GET",
            "/api/task/aming-claw/timeline?backlog_id=BUG-1&event_kind=implementation&limit=25",
            None,
        ),
        (
            "GET",
            "/api/backlog/aming-claw/BUG-1/timeline-gate?include_events=true&limit=25",
            None,
        ),
    ]


def test_mcp_observer_command_tools_route_to_governance_api():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

    dispatcher.dispatch(
        "observer_session_register",
        {
            "project_id": "aming-claw",
            "observer_kind": "codex",
            "session_label": "local",
            "pid": 123,
            "cwd": "/repo",
            "capabilities": {"actions": ["*"], "command_types": ["*"]},
        },
    )
    dispatcher.dispatch(
        "observer_session_heartbeat",
        {
            "project_id": "aming-claw",
            "session_id": "obs-1",
            "session_token": "tok",
        },
    )
    dispatcher.dispatch(
        "observer_command_enqueue",
        {
            "project_id": "aming-claw",
            "command_type": "analyze_requirements",
            "payload": {"raw_id": "raw-1"},
            "created_by": "dashboard",
        },
    )
    dispatcher.dispatch(
        "observer_command_list",
        {
            "project_id": "aming-claw",
            "status": "queued,claimed",
            "limit": 2000,
        },
    )
    dispatcher.dispatch(
        "observer_command_next",
        {
            "project_id": "aming-claw",
            "session_id": "obs-1",
            "session_token": "tok",
        },
    )
    dispatcher.dispatch(
        "observer_command_claim",
        {
            "project_id": "aming-claw",
            "session_id": "obs-1",
            "session_token": "tok",
            "command_id": "cmd-1",
        },
    )
    dispatcher.dispatch(
        "observer_command_complete",
        {
            "project_id": "aming-claw",
            "session_id": "obs-1",
            "session_token": "tok",
            "command_id": "cmd-1",
            "result": {"ok": True},
        },
    )
    dispatcher.dispatch(
        "observer_command_fail",
        {
            "project_id": "aming-claw",
            "session_id": "obs-1",
            "session_token": "tok",
            "command_id": "cmd-2",
            "error": "blocked",
        },
    )
    dispatcher.dispatch(
        "observer_session_close",
        {
            "project_id": "aming-claw",
            "session_id": "obs-1",
            "session_token": "tok",
        },
    )
    dispatcher.dispatch(
        "observer_session_revoke",
        {
            "project_id": "aming-claw",
            "session_id": "obs-1",
            "session_token": "tok",
        },
    )

    assert recorder.calls == [
        (
            "POST",
            "/api/projects/aming-claw/observer-sessions/register",
            {
                "observer_kind": "codex",
                "session_label": "local",
                "pid": 123,
                "cwd": "/repo",
                "capabilities": {"actions": ["*"], "command_types": ["*"]},
            },
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-sessions/obs-1/heartbeat",
            {"session_token": "tok"},
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-commands",
            {
                "command_type": "analyze_requirements",
                "payload": {"raw_id": "raw-1"},
                "created_by": "dashboard",
            },
        ),
        (
            "GET",
            "/api/projects/aming-claw/observer-commands?status=queued%2Cclaimed&limit=1000",
            None,
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-commands/next",
            {"session_id": "obs-1", "session_token": "tok"},
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-commands/claim",
            {"session_id": "obs-1", "session_token": "tok", "command_id": "cmd-1"},
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-commands/cmd-1/complete",
            {"session_id": "obs-1", "session_token": "tok", "result": {"ok": True}},
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-commands/cmd-2/fail",
            {"session_id": "obs-1", "session_token": "tok", "error": "blocked"},
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-sessions/obs-1/close",
            {"session_token": "tok"},
        ),
        (
            "POST",
            "/api/projects/aming-claw/observer-sessions/obs-1/revoke",
            {"session_token": "tok"},
        ),
    ]


def test_mcp_graph_tools_route_to_governance_api():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

    dispatcher.dispatch(
        "graph_operations_queue",
        {
            "project_id": "aming-claw",
            "require_current_semantic": True,
        },
    )
    dispatcher.dispatch(
        "graph_query",
        {
            "project_id": "aming-claw",
            "tool": "search_semantic",
            "args": {"query": "mcp", "limit": 5},
        },
    )
    dispatcher.dispatch(
        "graph_pending_scope_queue",
        {
            "project_id": "aming-claw",
            "commit_sha": "head",
            "parent_commit_sha": "old",
            "evidence": {"source": "test"},
        },
    )

    assert recorder.calls[0] == (
        "GET",
        "/api/graph-governance/aming-claw/operations/queue?require_current_semantic=true",
        None,
    )
    assert recorder.calls[1] == (
        "POST",
        "/api/graph-governance/aming-claw/query",
        {
            "tool": "search_semantic",
            "args": {"query": "mcp", "limit": 5},
            "actor": "mcp",
            "query_source": "observer",
            "query_purpose": "prompt_context_build",
        },
    )
    assert recorder.calls[2] == (
        "POST",
        "/api/graph-governance/aming-claw/pending-scope",
        {"commit_sha": "head", "parent_commit_sha": "old", "evidence": {"source": "test"}},
    )


def test_mcp_pending_scope_queue_can_force_requeue_suspect_materialization():
    recorder = _Recorder()
    dispatcher = _dispatcher(recorder)

    dispatcher.dispatch(
        "graph_pending_scope_queue",
        {
            "project_id": "aming-claw",
            "commit_sha": "head",
            "status": "queued",
            "force_requeue": True,
            "evidence": {"source": "suspect_snapshot"},
        },
    )

    assert recorder.calls == [
        (
            "POST",
            "/api/graph-governance/aming-claw/pending-scope",
            {
                "commit_sha": "head",
                "status": "queued",
                "force_requeue": True,
                "evidence": {"source": "suspect_snapshot"},
            },
        )
    ]


def test_mcp_host_ops_tools_route_to_manager_sidecar():
    governance = _Recorder()
    manager = _Recorder()
    dispatcher = _dispatcher(governance, manager)

    dispatcher.dispatch("manager_health", {})
    dispatcher.dispatch(
        "governance_redeploy",
        {
            "project_id": "aming-claw",
            "chain_version": "abc1234",
            "sync_version": False,
        },
    )
    dispatcher.dispatch(
        "executor_respawn",
        {
            "project_id": "aming-claw",
            "chain_version": "abc1234",
        },
    )

    assert manager.calls == [
        ("GET", "/api/manager/health", None),
        ("POST", "/api/manager/redeploy/governance", {"chain_version": "abc1234"}),
        ("POST", "/api/manager/respawn-executor", {"chain_version": "abc1234"}),
    ]
    assert governance.calls == []


def test_mcp_runtime_status_aggregates_governance_and_manager():
    governance = _RuntimeGovRecorder()
    manager = _Recorder()
    dispatcher = _dispatcher(governance, manager)

    status = dispatcher.dispatch("runtime_status", {"project_id": "aming-claw"})

    assert status["ok"] is True
    assert status["strict_ok"] is True
    assert status["severity"] == "ok"
    assert status["usable"] is True
    assert status["capabilities"]["graph_queries"] is True
    assert status["capabilities"]["core_runtime"] is True
    assert status["capabilities"]["advanced_chain_ops"] is True
    assert status["governance"]["status"] == "ok"
    assert status["manager"]["ok"] is True
    assert status["version_check"]["runtime_match"] is True
    assert governance.calls == [
        ("GET", "/api/health", None),
        ("GET", "/api/version-check/aming-claw", None),
    ]
    assert manager.calls == [("GET", "/api/manager/health", None)]


def test_mcp_runtime_status_runtime_mismatch_is_advanced_ops_only():
    governance = _RuntimeMismatchGovRecorder()
    manager = _Recorder()
    dispatcher = _dispatcher(governance, manager)

    status = dispatcher.dispatch("runtime_status", {"project_id": "aming-claw"})

    assert status["ok"] is True
    assert status["strict_ok"] is False
    assert status["severity"] == "warning"
    assert status["usable"] is True
    assert status["capabilities"]["graph_queries"] is True
    assert status["capabilities"]["backlog"] is True
    assert status["capabilities"]["core_runtime"] is False
    assert status["capabilities"]["advanced_chain_ops"] is False
    assert status["capabilities"]["executor"] is False
    assert "version metadata needs attention" in status["summary"]
    assert "advanced_chain_ops_redeploy_or_restart" in status["recommended_actions"]


def test_mcp_runtime_status_service_manager_mismatch_keeps_core_ok():
    governance = _AdvancedRuntimeMismatchGovRecorder()
    manager = _Recorder()
    dispatcher = _dispatcher(governance, manager)

    status = dispatcher.dispatch("runtime_status", {"project_id": "aming-claw"})

    assert status["ok"] is True
    assert status["strict_ok"] is True
    assert status["severity"] == "ok"
    assert status["capabilities"]["core_runtime"] is True
    assert status["capabilities"]["advanced_chain_ops"] is False
    assert status["capabilities"]["executor"] is False
    assert "Governance core is healthy" in status["summary"]
    assert "advanced_chain_ops_redeploy_or_restart" in status["recommended_actions"]


def test_mcp_runtime_status_governance_offline_reports_loaded_mcp():
    governance = _OfflineGovRecorder()
    manager = _Recorder()
    dispatcher = _dispatcher(governance, manager)

    status = dispatcher.dispatch("runtime_status", {"project_id": "aming-claw"})

    assert status["ok"] is False
    assert status["severity"] == "blocking"
    assert status["governance"]["governance_online"] is False
    assert status["governance"]["mcp_loaded"] is True
    assert status["version_check"]["governance_online"] is False
    assert "start_governance" in status["recommended_actions"]
    assert "MCP server is loaded" in status["governance"]["message"]


def test_mcp_version_check_preserves_governance_and_workspace_heads(monkeypatch):
    governance = _Recorder()

    def api(method: str, path: str, data: dict | None = None) -> dict:
        governance.calls.append((method, path, data))
        if path == "/api/version-check/aming-claw":
            return {
                "ok": False,
                "head": "gov-old",
                "chain_version": "chain-old",
                "dirty": False,
                "message": "HEAD (gov-old) != CHAIN_VERSION (chain-old)",
            }
        return {"ok": True}

    def fake_check_output(cmd, **kwargs):
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return b"workspace-new\n"
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return b""
        if cmd[:3] == ["git", "log", "--oneline"]:
            return b"workspace-new commit\n"
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(mcp_tools.subprocess, "check_output", fake_check_output)
    dispatcher = ToolDispatcher(api_fn=api, worker_pool=None, service_mgr=None, workspace=".")

    result = dispatcher.dispatch("version_check", {"project_id": "aming-claw"})

    assert result["head"] == "workspace-new"
    assert result["mcp_workspace_head"] == "workspace-new"
    assert result["governance_synced_head"] == "gov-old"
    assert "MCP workspace HEAD (workspace-new) != CHAIN_VERSION (chain-old)" in result["message"]
    assert "governance synced HEAD (gov-old) differs from MCP workspace HEAD (workspace-new)" in result["message"]


def test_mcp_version_check_governance_offline_preserves_workspace_head(monkeypatch):
    governance = _OfflineGovRecorder()

    def fake_check_output(cmd, **kwargs):
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return b"workspace-new\n"
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return b""
        return b""

    monkeypatch.setattr(mcp_tools.subprocess, "check_output", fake_check_output)
    dispatcher = ToolDispatcher(api_fn=governance.api, worker_pool=None, service_mgr=None, workspace=".")

    result = dispatcher.dispatch("version_check", {"project_id": "aming-claw"})

    assert result["ok"] is False
    assert result["governance_online"] is False
    assert result["mcp_loaded"] is True
    assert result["recommended_action"] == "start_governance"
    assert result["mcp_workspace_head"] == "workspace-new"
    assert result["head"] == "workspace-new"
    assert "MCP server is loaded" in result["message"]


def test_mcp_manager_start_refuses_takeover_from_mcp():
    governance = _Recorder()
    dispatcher = _dispatcher(governance, _Recorder())

    result = dispatcher.dispatch("manager_start", {"takeover": True})

    assert result["ok"] is False
    assert result["error"] == "takeover_not_supported_from_mcp"


def test_mcp_manager_start_uses_posix_script_on_macos(monkeypatch):
    governance = _Recorder()
    manager = _Recorder()

    def manager_api(method: str, path: str, data: dict | None = None) -> dict:
        manager.calls.append((method, path, data))
        return {"ok": len(manager.calls) > 1}

    dispatcher = ToolDispatcher(
        api_fn=governance.api,
        worker_pool=None,
        service_mgr=None,
        manager_api_fn=manager_api,
        workspace="/repo",
    )
    calls = []

    monkeypatch.setattr(mcp_tools.sys, "platform", "darwin")
    monkeypatch.setattr(mcp_tools.os.path, "exists", lambda path: path == "/repo/scripts/start-manager.sh")

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="Manager healthy.", stderr="")

    monkeypatch.setattr(mcp_tools.subprocess, "run", fake_run)

    result = dispatcher.dispatch("manager_start", {"health_wait_seconds": 7})

    assert result["ok"] is True
    assert result["script"] == "start-manager.sh"
    assert result["platform"] == "darwin"
    assert calls[0][0] == [
        "bash",
        "/repo/scripts/start-manager.sh",
        "--health-wait-seconds",
        "7",
    ]
    assert manager.calls == [
        ("GET", "/api/manager/health", None),
        ("GET", "/api/manager/health", None),
    ]
