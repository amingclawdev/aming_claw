from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from agent.mcp.server import AmingClawMCP
from agent.mcp.tools import ToolDispatcher


ROOT = Path(__file__).resolve().parents[2]


def _run_mcp_probe(
    messages: list[dict],
    *,
    extra_args: list[str] | None = None,
    cwd: Path = ROOT,
) -> tuple[list[dict], str, int]:
    args = [
        sys.executable,
        "-m",
        "agent.mcp.server",
        "--project",
        "aming-claw",
        "--workers",
        "0",
        "--governance-url",
        "http://127.0.0.1:9",
    ]
    if extra_args:
        args.extend(extra_args)
    proc = subprocess.Popen(
        args,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    for message in messages:
        proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.close()
    stdout = proc.stdout.read()
    stderr = proc.stderr.read() if proc.stderr else ""
    returncode = proc.wait(timeout=10)
    responses = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    return responses, stderr, returncode


def test_mcp_stdio_initialize_and_health_survive_missing_governance():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "health", "arguments": {}},
        },
    ])

    assert returncode == 0
    assert stderr == ""
    assert responses[0]["result"]["serverInfo"]["name"] == "aming-claw"
    assert "resources" in responses[0]["result"]["capabilities"]
    text = responses[1]["result"]["content"][0]["text"]
    payload = json.loads(text)
    assert "error" in payload


def test_mcp_stdio_tools_list_does_not_require_redis_or_governance():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])

    assert returncode == 0
    assert stderr == ""
    names = {tool["name"] for tool in responses[0]["result"]["tools"]}
    assert {"health", "manager_health", "graph_query", "backlog_upsert"}.issubset(names)


def test_mcp_stdio_backlog_close_schema_exposes_route_gate_fields():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])

    assert returncode == 0
    assert stderr == ""
    tools = responses[0]["result"]["tools"]
    backlog_close = next(tool for tool in tools if tool["name"] == "backlog_close")
    properties = backlog_close["inputSchema"]["properties"]
    assert "route_token" in properties
    assert "route_waiver" in properties
    assert "route_token_waiver" in properties


def test_mcp_stdio_protected_write_schemas_expose_route_gate_fields():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])

    assert returncode == 0
    assert stderr == ""
    tools = {tool["name"]: tool for tool in responses[0]["result"]["tools"]}
    for name in ("backlog_upsert", "task_timeline_append"):
        properties = tools[name]["inputSchema"]["properties"]
        assert "route_token" in properties
        assert "route_waiver" in properties
        assert "route_token_waiver" in properties


def test_mcp_stdio_observer_repair_run_plan_schema_is_read_only_entrypoint():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    ])

    assert returncode == 0
    assert stderr == ""
    tools = {tool["name"]: tool for tool in responses[0]["result"]["tools"]}
    schema = tools["observer_repair_run_plan"]["inputSchema"]
    properties = schema["properties"]
    assert schema["required"] == ["project_id"]
    assert "root_backlog_ids" in properties
    assert "blockers" in properties
    assert "include_timeline_precheck" in properties
    assert "version_check" in properties
    assert "route_token" not in properties
    assert "route_waiver" not in properties


def test_mcp_backlog_close_forwards_route_gate_payloads():
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        return {"ok": True}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )
    route_token = {
        "route_context_hash": "sha256:test-route",
        "prompt_contract_id": "prompt-contract",
        "caller_role": "observer",
        "allowed_action": "backlog_close",
        "scope": {"project_id": "aming-claw", "backlog_id": "BUG-ROUTE"},
        "expires_at": "2999-01-01T00:00:00Z",
        "evidence_refs": ["timeline:event-1"],
    }
    route_waiver = {
        "accepted": True,
        "waiver_type": "manual_fix",
        "route_context_hash": "sha256:test-route-waiver",
        "prompt_contract_id": "prompt-contract",
        "caller_role": "observer",
        "allowed_action": "backlog_close",
        "scope": {"project_id": "aming-claw", "backlog_id": "BUG-ROUTE"},
        "reason": "Unit test supplies explicit route waiver evidence.",
        "timeline_evidence": {"event_id": "event-2"},
    }

    result = dispatcher.dispatch(
        "backlog_close",
        {
            "project_id": "aming-claw",
            "bug_id": "BUG-ROUTE",
            "commit": "abc123",
            "actor": "observer",
            "route_token": route_token,
            "route_waiver": route_waiver,
        },
    )

    assert result == {"ok": True}
    assert calls == [
        (
            "POST",
            "/api/backlog/aming-claw/BUG-ROUTE/close",
            {
                "commit": "abc123",
                "actor": "observer",
                "route_token": route_token,
                "route_waiver": route_waiver,
            },
        )
    ]


def test_mcp_protected_write_dispatch_forwards_route_gate_payloads():
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        return {"ok": True}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )
    route_token = {
        "route_context_hash": "sha256:test-route",
        "prompt_contract_id": "prompt-contract",
        "caller_role": "observer",
        "allowed_action": "backlog_upsert",
        "scope": {"project_id": "aming-claw", "backlog_id": "BUG-ROUTE"},
        "expires_at": "2999-01-01T00:00:00Z",
        "evidence_refs": ["timeline:event-1"],
    }
    route_waiver = {
        "accepted": True,
        "waiver_type": "manual_fix",
        "route_context_hash": "sha256:test-route-waiver",
        "prompt_contract_id": "prompt-contract",
        "caller_role": "observer",
        "allowed_action": "task_timeline_append",
        "scope": {"project_id": "aming-claw", "backlog_id": "BUG-ROUTE"},
        "reason": "Unit test supplies explicit route waiver evidence.",
        "timeline_evidence": {"event_id": "event-2"},
    }

    dispatcher.dispatch(
        "backlog_upsert",
        {
            "project_id": "aming-claw",
            "bug_id": "BUG-ROUTE",
            "status": "FIXED",
            "route_token": route_token,
        },
    )
    dispatcher.dispatch(
        "task_timeline_append",
        {
            "project_id": "aming-claw",
            "backlog_id": "BUG-ROUTE",
            "event_type": "mf.verification",
            "event_kind": "verification",
            "route_waiver": route_waiver,
        },
    )

    assert calls == [
        (
            "POST",
            "/api/backlog/aming-claw/BUG-ROUTE",
            {"status": "FIXED", "route_token": route_token},
        ),
        (
            "POST",
            "/api/task/aming-claw/timeline",
            {
                "backlog_id": "BUG-ROUTE",
                "event_type": "mf.verification",
                "event_kind": "verification",
                "route_waiver": route_waiver,
            },
        ),
    ]


def test_mcp_protected_write_dispatch_preserves_structured_gate_failure():
    structured_failure = {
        "error": "route_token_required",
        "message": "route_token is required for protected governance action",
        "details": {
            "fault_domain": "caller_missing_route_evidence",
            "expected_behavior": True,
            "do_not_file_system_bug": True,
            "is_system_bug": False,
            "next_valid_actions": ["return_to_route_context_and_request_a_valid_route_token"],
            "system_bug_preconditions": ["valid route token was supplied and still rejected"],
        },
    }

    def fake_api(method: str, path: str, data: dict | None = None):
        return structured_failure

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )

    result = dispatcher.dispatch(
        "task_timeline_append",
        {
            "project_id": "aming-claw",
            "backlog_id": "BUG-ROUTE",
            "event_type": "mf.verification",
            "event_kind": "verification",
        },
    )

    assert result["error"] == "route_token_required"
    assert result["details"]["fault_domain"] == "caller_missing_route_evidence"
    assert result["details"]["expected_behavior"] is True
    assert result["details"]["is_system_bug"] is False


def test_mcp_observer_repair_run_plan_dispatches_to_read_only_endpoint():
    calls = []

    def fake_api(method: str, path: str, data: dict | None = None):
        calls.append((method, path, data))
        return {"ok": True, "repair_run_id": "repair-test"}

    dispatcher = ToolDispatcher(
        api_fn=fake_api,
        worker_pool=None,
        manager_api_fn=fake_api,
        workspace=str(ROOT),
    )

    result = dispatcher.dispatch(
        "observer_repair_run_plan",
        {
            "project_id": "aming-claw",
            "root_backlog_ids": ["AC-ROUTE-FLOW-SESSION-GUIDANCE-20260602"],
            "blockers": ["route_token_required"],
            "include_timeline_precheck": True,
            "actor": "observer-test",
        },
    )

    assert result == {"ok": True, "repair_run_id": "repair-test"}
    assert calls == [
        (
            "POST",
            "/api/projects/aming-claw/observer-repair-run/plan",
            {
                "root_backlog_ids": ["AC-ROUTE-FLOW-SESSION-GUIDANCE-20260602"],
                "blockers": ["route_token_required"],
                "include_timeline_precheck": True,
                "actor": "observer-test",
            },
        )
    ]


def test_mcp_stdio_resources_expose_skill_and_context_without_governance():
    responses, stderr, returncode = _run_mcp_probe([
        {"jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "resources/templates/list",
            "params": {},
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/read",
            "params": {"uri": "aming-claw://skill"},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "resources/read",
            "params": {"uri": "aming-claw://current-context"},
        },
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "resources/read",
            "params": {"uri": "aming-claw://seed-graph-summary"},
        },
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "resources/read",
            "params": {"uri": "aming-claw://self-graph-bundle-manifest"},
        },
    ])

    assert returncode == 0
    assert stderr == ""
    resources = {r["uri"]: r for r in responses[0]["result"]["resources"]}
    assert "aming-claw://skill" in resources
    assert "aming-claw://current-context" in resources
    assert "aming-claw://seed-graph-summary" in resources
    assert "aming-claw://self-graph-bundle-manifest" in resources
    assert "aming-claw://self-graph-bundle/manifest" in resources
    assert "aming-claw://self-graph-bundle/graph-structure" in resources
    assert "aming-claw://self-graph-bundle/semantic-projection" in resources
    templates = responses[1]["result"]["resourceTemplates"]
    assert templates[0]["uriTemplate"] == "aming-claw://project/{project_id}/context"
    skill_text = responses[2]["result"]["contents"][0]["text"]
    assert "## Capabilities" in skill_text
    assert "graph_query" in skill_text
    context_text = responses[3]["result"]["contents"][0]["text"]
    assert "project_id: `aming-claw`" in context_text
    assert "dashboard_url:" in context_text
    assert "health: `unavailable`" in context_text
    assert "backlog: `unavailable`" in context_text
    assert "## Primary Next Actions" in context_text
    assert "Start Services" in context_text
    assert "aming-claw start" in context_text
    assert "Call `graph_query` with `tool=query_schema`" in context_text
    seed = json.loads(responses[4]["result"]["contents"][0]["text"])
    assert seed["project_id"] == "aming-claw"
    assert "graph-native" in " ".join(seed["recommended_first_actions"]).lower()
    mcp_surface = next(s for s in seed["core_surfaces"] if s["name"] == "mcp-plugin")
    assert ".codex-plugin/plugin.json" in mcp_surface["paths"]
    assert ".claude-plugin/plugin.json" in mcp_surface["paths"]
    manifest = json.loads(responses[5]["result"]["contents"][0]["text"])
    assert manifest["bundle_major"] == 1
    assert manifest["consumer_contract"]["incompatible_major_action"] == "emit_plugin_update_reminder"
    assert manifest["resource_uris"]["graph_structure"] == "aming-claw://self-graph-bundle/graph-structure"
    assert manifest["resource_uris"]["semantic_projection"] == "aming-claw://self-graph-bundle/semantic-projection"


def test_mcp_current_context_prefers_workspace_project_config(tmp_path: Path):
    workspace = tmp_path / "external-project"
    workspace.mkdir()
    (workspace / ".aming-claw.yaml").write_text("project_id: instructor\n", encoding="utf-8")

    responses, stderr, returncode = _run_mcp_probe(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "resources/read",
                "params": {"uri": "aming-claw://current-context"},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "resources/read",
                "params": {"uri": "aming-claw://project/dashboard-e2e-demo/context"},
            },
        ],
        extra_args=["--workspace", str(workspace)],
    )

    assert returncode == 0
    assert stderr == ""
    current_text = responses[0]["result"]["contents"][0]["text"]
    assert "default_project_id: `aming-claw`" in current_text
    assert "workspace_project_id: `instructor`" in current_text
    assert "dashboard_project_id: `-`" in current_text
    assert "active_project_id: `instructor`" in current_text
    assert "context_source: `workspace_config`" in current_text
    assert "dashboard?project_id=instructor" in current_text

    project_text = responses[1]["result"]["contents"][0]["text"]
    assert "default_project_id: `aming-claw`" in project_text
    assert "workspace_project_id: `instructor`" in project_text
    assert "dashboard_project_id: `dashboard-e2e-demo`" in project_text
    assert "active_project_id: `dashboard-e2e-demo`" in project_text
    assert "context_source: `resource_uri`" in project_text


def _server_with_context_payloads(
    tmp_path: Path,
    *,
    graph: dict,
    project_id: str = "instructor",
    projects: list[dict] | None = None,
    backlog_count: int = 2,
    health: dict | None = None,
) -> AmingClawMCP:
    workspace = tmp_path / project_id
    workspace.mkdir()
    if projects is None:
        projects = [
            {
                "project_id": project_id,
                "workspace_path": str(workspace),
                "active_snapshot_id": graph.get("active_snapshot_id"),
            }
        ]
    server = AmingClawMCP(
        project_id="aming-claw",
        governance_url="http://governance.test",
        manager_url="http://manager.test",
        workspace=str(workspace),
        redis_url="redis://unused",
    )

    def fake_request(method: str, url: str, data: dict | None = None, timeout: int = 15) -> dict:
        if url.endswith("/api/projects"):
            return {"projects": projects}
        if url.endswith("/api/health"):
            return health or {"status": "ok", "version": "test-version"}
        if "/api/version-check/" in url:
            return {"head": "abcdef123456", "dirty": False, "runtime_match": True}
        if "/api/graph-governance/" in url and url.endswith("/status"):
            return graph
        if "/api/graph-governance/" in url and url.endswith("/operations/queue"):
            return {"count": 3}
        if "/api/backlog/" in url:
            return {"count": backlog_count, "bugs": [{} for _ in range(backlog_count)]}
        return {"error": f"unexpected url {url}"}

    server._request_json = fake_request  # type: ignore[method-assign]
    return server


def _primary_action_lines(context_text: str) -> list[str]:
    return [line for line in context_text.splitlines() if re.match(r"^\d+\. \*\*", line)]


def test_mcp_current_context_online_current_graph_shows_minimal_actions(tmp_path: Path):
    server = _server_with_context_payloads(
        tmp_path,
        graph={
            "active_snapshot_id": "full-abcdef-1234",
            "pending_scope_reconcile_count": 0,
            "current_state": {"graph_stale": {"is_stale": False}},
        },
        backlog_count=4,
    )

    text = server._current_context_text("instructor")

    assert "project_id: `instructor`" in text
    assert "dashboard?project_id=instructor&view=graph" in text
    assert "graph: snapshot `full-abcdef-1234` stale `False` pending_scope `0`" in text
    assert "operations_queue: count `3`" in text
    assert "backlog: open `4`" in text
    assert "selected_project_note: active project `instructor` differs from default `aming-claw`" in text
    actions = _primary_action_lines(text)
    assert len(actions) == 3
    assert "Check Current Project Status" in actions[0]
    assert "Find PR Opportunities" in actions[1]
    assert "Explain Graph Concepts" in actions[2]


def test_mcp_current_context_online_stale_graph_prioritizes_update(tmp_path: Path):
    server = _server_with_context_payloads(
        tmp_path,
        graph={
            "active_snapshot_id": "scope-abcdef-1234",
            "pending_scope_reconcile_count": 1,
            "current_state": {"graph_stale": {"is_stale": True}},
        },
    )

    text = server._current_context_text("instructor")

    actions = _primary_action_lines(text)
    assert len(actions) == 3
    assert "Update Graph" in actions[0]
    assert "Check Current Project Status" in actions[1]
    assert "Find PR Opportunities" in actions[2]


def test_mcp_current_context_online_missing_graph_opens_projects(tmp_path: Path):
    server = _server_with_context_payloads(
        tmp_path,
        graph={
            "active_snapshot_id": "",
            "pending_scope_reconcile_count": 0,
            "current_state": {},
        },
        projects=[{"project_id": "instructor", "workspace_path": str(tmp_path / "instructor"), "active_snapshot_id": ""}],
    )

    text = server._current_context_text("instructor")

    assert "dashboard?project_id=instructor&view=projects" in text
    actions = _primary_action_lines(text)
    assert len(actions) == 3
    assert "Initialize Project" in actions[0]
    assert "Check Current Project Status" in actions[1]
    assert "Explain Graph Concepts" in actions[2]
