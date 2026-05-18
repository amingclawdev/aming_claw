from __future__ import annotations

import builtins
import json
from pathlib import Path
from types import SimpleNamespace

from agent.governance import server


def _ctx(
    project_id: str,
    method: str = "GET",
    body: dict | None = None,
    query: dict | None = None,
):
    return server.RequestContext(
        None,
        method,
        {"project_id": project_id},
        query or {},
        body or {},
        "req-project-config-test",
        "",
        "",
    )


def _write_project_config(root: Path) -> None:
    (root / ".aming-claw.yaml").write_text(
        "\n".join([
            "project_id: dashboard-demo",
            "language: typescript",
            "testing:",
            "  unit_command: npm test",
            "  e2e:",
            "    auto_run: false",
            "    suites:",
            "      dashboard.semantic.safe:",
            "        label: Dashboard semantic safe path",
            "        command: node scripts/e2e-trunk.mjs --skip-dashboard",
            "        live_ai: false",
            "        mutates_db: true",
            "        trigger:",
            "          paths:",
            "            - src/**",
            "          tags:",
            "            - dashboard",
            "governance:",
            "  enabled: true",
            "  test_tool_label: vitest",
            "  exclude_roots:",
            "    - examples",
            "graph:",
            "  exclude_paths:",
            "    - docs/dev",
            "  nested_projects:",
            "    mode: exclude",
            "    roots:",
            "      - examples/demo",
            "ai:",
            "  routing:",
            "    pm:",
            "      provider: openai",
            "      model: gpt-5.5",
            "    semantic:",
            "      provider: anthropic",
            "      model: claude-opus-4-7",
            "",
        ]),
        encoding="utf-8",
    )


def _patch_project_registry(monkeypatch, data: dict) -> dict:
    monkeypatch.setattr(server.project_service, "_load_projects", lambda: data)

    def _save_projects(updated: dict) -> None:
        snapshot = json.loads(json.dumps(updated))
        data.clear()
        data.update(snapshot)

    monkeypatch.setattr(server.project_service, "_save_projects", _save_projects)
    return data


def test_project_config_endpoint_exposes_governance_exclude_roots(tmp_path, monkeypatch):
    _write_project_config(tmp_path)
    monkeypatch.setattr(
        server.project_service,
        "list_projects",
        lambda: [{
            "project_id": "dashboard-demo",
            "workspace_path": str(tmp_path),
            "status": "active",
        }],
    )

    payload = server.handle_project_config(_ctx("dashboard-demo"))

    assert payload["project_id"] == "dashboard-demo"
    assert payload["language"] == "typescript"
    assert payload["governance"]["exclude_roots"] == ["examples"]
    assert payload["testing"]["e2e"]["suites"]["dashboard.semantic.safe"]["command"].startswith("node scripts/")
    assert payload["graph"]["exclude_paths"] == ["docs/dev"]
    assert payload["graph"]["effective_exclude_roots"] == [
        "examples",
        "docs/dev",
        "examples/demo",
    ]
    assert payload["ai"]["routing"]["pm"] == {
        "provider": "openai",
        "model": "gpt-5.5",
    }


def test_project_config_endpoint_falls_back_to_repo_root_for_aming_claw(monkeypatch):
    monkeypatch.setattr(
        server.project_service,
        "list_projects",
        lambda: [{
            "project_id": "aming-claw",
            "workspace_path": "",
            "status": "active",
        }],
    )

    payload = server.handle_project_config(_ctx("aming-claw"))

    assert payload["project_id"] == "aming-claw"
    assert "examples" in payload["governance"]["exclude_roots"]


def test_project_e2e_config_endpoint_exposes_suite_registry(tmp_path, monkeypatch):
    _write_project_config(tmp_path)
    monkeypatch.setattr(
        server,
        "_graph_governance_project_root",
        lambda _project_id, _body: tmp_path,
    )

    payload = server.handle_project_e2e_config(_ctx("dashboard-demo"))

    assert payload["ok"] is True
    assert payload["project_id"] == "dashboard-demo"
    suites = payload["e2e"]["suites"]
    assert suites["dashboard.semantic.safe"]["trigger"]["paths"] == ["src/**"]
    assert suites["dashboard.semantic.safe"]["live_ai"] is False


def test_project_ai_config_endpoint_returns_writable_dashboard_contract(tmp_path, monkeypatch):
    _write_project_config(tmp_path)
    monkeypatch.setattr(
        server.project_service,
        "list_projects",
        lambda: [{
            "project_id": "dashboard-demo",
            "workspace_path": str(tmp_path),
            "status": "active",
        }],
    )

    payload = server.handle_project_ai_config(_ctx("dashboard-demo"))

    assert payload["project_id"] == "dashboard-demo"
    assert payload["read_only"] is False
    assert payload["write_supported"] is True
    assert payload["write_target"] == "aming-claw project registry"
    assert "role_routing" in payload
    assert "semantic" in payload
    assert payload["model_catalog"]["models"]["anthropic"]
    assert payload["model_catalog"]["providers"]["openai"]["runtime"] == "Codex CLI"
    assert payload["tool_health"]["anthropic"]["runtime"] == "Claude Code CLI"
    assert payload["semantic"]["analyzer_role"]
    assert payload["project_config"]["ai"]["routing"]["semantic"]["model"] == "claude-opus-4-7"
    assert "dashboard.semantic.safe" in payload["project_config"]["testing"]["e2e"]["suites"]


def test_project_ai_config_live_check_marks_claude_auth_error(tmp_path, monkeypatch):
    _write_project_config(tmp_path)
    monkeypatch.setattr(
        server.project_service,
        "list_projects",
        lambda: [{
            "project_id": "dashboard-demo",
            "workspace_path": str(tmp_path),
            "status": "active",
        }],
    )
    monkeypatch.setattr(server.shutil, "which", lambda candidate: f"/bin/{candidate}")

    def _run(cmd, **kwargs):
        if "--version" in cmd:
            return SimpleNamespace(returncode=0, stdout="2.1.140 (Claude Code)\n", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "is_error": True,
                "api_error_status": 401,
                "result": "Failed to authenticate. API Error: 401 Invalid authentication credentials",
            }),
            stderr="",
        )

    monkeypatch.setattr(server.subprocess, "run", _run)

    payload = server.handle_project_ai_config(_ctx("dashboard-demo", query={"live_check": "1"}))

    anthropic = payload["tool_health"]["anthropic"]
    assert anthropic["status"] == "auth_error"
    assert anthropic["auth_status"] == "live_failed"
    assert "Invalid authentication credentials" in anthropic["error"]


def test_project_ai_config_endpoint_updates_project_routing(tmp_path, monkeypatch):
    _write_project_config(tmp_path)
    data = _patch_project_registry(
        monkeypatch,
        {
            "version": 1,
            "projects": {
                "dashboard-demo": {
                    "project_id": "dashboard-demo",
                    "workspace_path": str(tmp_path),
                    "status": "active",
                },
            },
        },
    )
    before = (tmp_path / ".aming-claw.yaml").read_text(encoding="utf-8")

    payload = server.handle_project_ai_config_update(_ctx(
        "dashboard-demo",
        method="POST",
        body={
            "routing": {
                "pm": {"provider": "openai", "model": "gpt-5.5"},
                "dev": {"provider": "openai", "model": "gpt-5.4-mini"},
                "semantic": {"provider": "anthropic", "model": "claude-sonnet-4-5"},
            },
            "actor": "dashboard-test",
        },
    ))

    assert payload["ok"] is True
    assert payload["updated"] is True
    assert payload["project_config"]["ai"]["routing"]["dev"]["model"] == "gpt-5.4-mini"
    assert payload["project_config"]["ai"]["routing"]["semantic"]["model"] == "claude-sonnet-4-5"
    assert payload["project_config_source"] == "aming_claw_registry"
    assert (tmp_path / ".aming-claw.yaml").read_text(encoding="utf-8") == before
    assert data["projects"]["dashboard-demo"]["project_config"]["ai"]["routing"]["dev"]["model"] == "gpt-5.4-mini"


def test_project_ai_config_endpoint_merges_partial_routing(tmp_path, monkeypatch):
    data = _patch_project_registry(
        monkeypatch,
        {
            "version": 1,
            "projects": {
                "dashboard-demo": {
                    "project_id": "dashboard-demo",
                    "workspace_path": str(tmp_path),
                    "status": "active",
                    "project_config": {
                        "project_id": "dashboard-demo",
                        "language": "typescript",
                        "ai": {
                            "routing": {
                                "pm": {"provider": "openai", "model": "gpt-5.5"},
                                "dev": {"provider": "openai", "model": "gpt-5.4"},
                                "semantic": {"provider": "anthropic", "model": "claude-opus-4-7"},
                            }
                        },
                    },
                },
            },
        },
    )

    payload = server.handle_project_ai_config_update(_ctx(
        "dashboard-demo",
        method="POST",
        body={
            "routing": {
                "semantic": {"provider": "openai", "model": "gpt-5.4-mini"},
            },
            "actor": "dashboard-test",
        },
    ))

    routing = payload["project_config"]["ai"]["routing"]
    assert routing["semantic"] == {"provider": "openai", "model": "gpt-5.4-mini"}
    assert routing["pm"] == {"provider": "openai", "model": "gpt-5.5"}
    assert routing["dev"] == {"provider": "openai", "model": "gpt-5.4"}
    assert data["projects"]["dashboard-demo"]["project_config"]["ai"]["routing"]["pm"]["model"] == "gpt-5.5"


def test_project_ai_config_update_stores_missing_project_config_in_registry(tmp_path, monkeypatch):
    data = _patch_project_registry(
        monkeypatch,
        {
            "version": 1,
            "projects": {
                "dashboard-demo": {
                    "project_id": "dashboard-demo",
                    "workspace_path": str(tmp_path),
                    "status": "active",
                },
            },
        },
    )

    payload = server.handle_project_ai_config_update(_ctx(
        "dashboard-demo",
        method="POST",
        body={
            "routing": {
                "semantic": {"provider": "anthropic", "model": "claude-opus-4-7"},
                "dev": {"provider": "openai", "model": "gpt-5.4"},
            },
            "actor": "dashboard-test",
        },
    ))

    config_path = tmp_path / ".aming-claw.yaml"
    assert not config_path.exists()
    assert payload["ok"] is True
    assert payload["updated"] is True
    assert payload["project_config_error"] == ""
    assert payload["project_config_source"] == "aming_claw_registry"
    assert payload["write_target"] == "aming-claw project registry"
    assert payload["project_config"]["ai"]["routing"]["semantic"] == {
        "provider": "anthropic",
        "model": "claude-opus-4-7",
    }
    assert payload["project_config"]["ai"]["routing"]["dev"]["model"] == "gpt-5.4"
    assert data["projects"]["dashboard-demo"]["project_config"]["ai"]["routing"]["semantic"]["model"] == "claude-opus-4-7"


def test_project_config_endpoint_uses_registry_fallback_without_local_file(tmp_path, monkeypatch):
    _patch_project_registry(
        monkeypatch,
        {
            "version": 1,
            "projects": {
                "dashboard-demo": {
                    "project_id": "dashboard-demo",
                    "workspace_path": str(tmp_path),
                    "status": "active",
                    "project_config_source": "aming_claw_registry",
                    "project_config": {
                        "project_id": "dashboard-demo",
                        "language": "typescript",
                        "testing": {"unit_command": "npm test", "e2e": {}},
                        "graph": {"exclude_paths": ["node_modules"], "ignore_globs": []},
                        "ai": {
                            "routing": {
                                "semantic": {
                                    "provider": "anthropic",
                                    "model": "claude-opus-4-7",
                                }
                            }
                        },
                    },
                },
            },
        },
    )

    payload = server.handle_project_config(_ctx("dashboard-demo"))

    assert payload["project_id"] == "dashboard-demo"
    assert payload["config_source"] == "aming_claw_registry"
    assert payload["language"] == "typescript"
    assert payload["ai"]["routing"]["semantic"]["model"] == "claude-opus-4-7"


def test_project_config_endpoint_generates_non_invasive_default_for_no_config(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    _patch_project_registry(
        monkeypatch,
        {
            "version": 1,
            "projects": {
                "dashboard-demo": {
                    "project_id": "dashboard-demo",
                    "workspace_path": str(tmp_path),
                    "status": "active",
                },
            },
        },
    )

    payload = server.handle_project_config(_ctx("dashboard-demo"))

    assert not (tmp_path / ".aming-claw.yaml").exists()
    assert payload["project_id"] == "dashboard-demo"
    assert payload["config_source"] == "generated_default"
    assert payload["language"] == "javascript"
    assert payload["testing"]["unit_command"] == "npm test"


def test_project_git_ref_endpoints_return_and_persist_selected_ref(tmp_path, monkeypatch):
    projects = {
        "dashboard-demo": {
            "project_id": "dashboard-demo",
            "workspace_path": str(tmp_path),
            "status": "active",
        }
    }

    monkeypatch.setattr(server, "_graph_governance_project_root", lambda _pid, _body: tmp_path)
    monkeypatch.setattr(
        server,
        "_git_refs_for_root",
        lambda _root: {
            "head_commit": "abc123",
            "current_branch": "main",
            "branches": ["feature/dashboard", "main"],
            "tags": [],
            "is_git_repo": True,
        },
    )
    monkeypatch.setattr(server, "_git_ref_exists", lambda _root, ref: ref in {"main", "feature/dashboard"})
    monkeypatch.setattr(server.project_service, "get_project", lambda pid: projects.get(pid))

    def _update(pid, updates):
        projects[pid].update(updates)
        return projects[pid]

    monkeypatch.setattr(server.project_service, "update_project_metadata", _update)

    initial = server.handle_project_git_refs(_ctx("dashboard-demo"))
    assert initial["selected_ref"] == "main"

    updated = server.handle_project_git_ref_select(_ctx(
        "dashboard-demo",
        method="POST",
        body={"selected_ref": "feature/dashboard", "actor": "dashboard-test"},
    ))
    assert updated["ok"] is True
    assert updated["selected_ref"] == "feature/dashboard"
    assert projects["dashboard-demo"]["selected_ref_updated_by"] == "dashboard-test"


def test_local_choose_directory_endpoint_returns_selected_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_open_local_directory_picker",
        lambda initial_path="", title="", timeout_seconds=12.0: str(tmp_path),
    )

    payload = server.handle_local_choose_directory(_ctx(
        "dashboard-demo",
        method="POST",
        body={"initial_path": str(tmp_path.parent), "title": "Pick one"},
    ))

    assert payload["ok"] is True
    assert payload["selected"] is True
    assert payload["path"] == str(tmp_path)
    assert payload["manual_entry"] is False


def test_local_choose_directory_endpoint_handles_unavailable_picker(monkeypatch):
    def _raise_unavailable(initial_path="", title="", timeout_seconds=12.0):
        raise RuntimeError("picker unavailable")

    monkeypatch.setattr(server, "_open_local_directory_picker", _raise_unavailable)

    payload = server.handle_local_choose_directory(_ctx(
        "dashboard-demo",
        method="POST",
        body={"initial_path": "C:/missing"},
    ))

    assert payload["ok"] is False
    assert payload["selected"] is False
    assert payload["manual_entry"] is True
    assert "picker unavailable" in payload["error"]


def test_local_choose_directory_endpoint_passes_clamped_timeout(tmp_path, monkeypatch):
    seen = {}

    def _select(initial_path="", title="", timeout_seconds=12.0):
        seen["timeout_seconds"] = timeout_seconds
        return str(tmp_path)

    monkeypatch.setattr(server, "_open_local_directory_picker", _select)

    payload = server.handle_local_choose_directory(_ctx(
        "dashboard-demo",
        method="POST",
        body={"timeout_seconds": 120},
    ))

    assert payload["ok"] is True
    assert seen["timeout_seconds"] == 60.0


def test_local_directory_picker_on_macos_prefers_osascript(tmp_path, monkeypatch):
    calls = {}

    def _fake_macos(initial_path="", title="", timeout_seconds=12.0):
        calls["initial_path"] = initial_path
        calls["title"] = title
        calls["timeout_seconds"] = timeout_seconds
        return str(tmp_path)

    def _fail_tk():
        raise AssertionError("macOS picker should not use in-process tkinter")

    fake_tk = SimpleNamespace(
        Tk=_fail_tk,
        filedialog=SimpleNamespace(askdirectory=lambda **kwargs: _fail_tk()),
    )
    real_import = builtins.__import__

    def _fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "tkinter":
            return fake_tk
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server, "_open_local_directory_picker_macos", _fake_macos)
    monkeypatch.setattr(builtins, "__import__", _fake_import)

    selected = server._open_local_directory_picker(
        initial_path=str(tmp_path.parent),
        title="Pick project",
        timeout_seconds=4,
    )

    assert selected == str(tmp_path)
    assert calls == {
        "initial_path": str(tmp_path.parent),
        "title": "Pick project",
        "timeout_seconds": 4,
    }


def test_macos_directory_picker_uses_osascript(tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/osascript" if name == "osascript" else None)

    def _run(args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=str(tmp_path), stderr="")

    monkeypatch.setattr(server.subprocess, "run", _run)

    selected = server._open_local_directory_picker_macos(title="Pick project", timeout_seconds=4)

    assert selected == str(tmp_path.resolve())
    assert calls["args"][0] == "/usr/bin/osascript"
    assert calls["kwargs"]["timeout"] == 4.0


def test_linux_directory_picker_uses_zenity(tmp_path, monkeypatch):
    calls = {}
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/zenity" if name == "zenity" else None)

    def _run(args, **kwargs):
        calls["args"] = args
        return SimpleNamespace(returncode=0, stdout=str(tmp_path), stderr="")

    monkeypatch.setattr(server.subprocess, "run", _run)

    selected = server._open_local_directory_picker_linux(initial_path=str(tmp_path), title="Pick project")

    assert selected == str(tmp_path.resolve())
    assert calls["args"][0] == "/usr/bin/zenity"
    assert "--file-selection" in calls["args"]
    assert "--directory" in calls["args"]


def test_linux_directory_picker_requires_gui_fallback(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda _name: None)

    try:
        server._open_local_directory_picker_linux()
    except RuntimeError as exc:
        assert "zenity" in str(exc)
    else:
        raise AssertionError("expected RuntimeError when no Linux picker is available")


def test_project_registry_exposes_bootstrap_progress(monkeypatch):
    data = {
        "version": 1,
        "projects": {
            "dashboard-demo": {
                "project_id": "dashboard-demo",
                "workspace_path": "C:/demo",
                "status": "active",
            },
        },
    }

    monkeypatch.setattr(server.project_service, "_load_projects", lambda: data)

    monkeypatch.setattr(server.project_service, "_save_projects", lambda _updated: None)

    progress = server.project_service.update_project_operation_progress(
        "dashboard-demo",
        operation="bootstrap",
        status="running",
        phase="full_reconcile",
        message="Running full graph reconcile.",
    )

    assert progress["phase"] == "full_reconcile"
    projects = server.project_service.list_projects()
    exposed = projects[0]["bootstrap_progress"]
    assert exposed["operation"] == "bootstrap"
    assert exposed["status"] == "running"
    assert exposed["elapsed_seconds"] >= 0


def test_projects_list_endpoint_returns_registered_projects(monkeypatch):
    monkeypatch.setattr(
        server.project_service,
        "list_projects",
        lambda: [{
            "project_id": "dashboard-demo",
            "workspace_path": "C:/demo",
            "status": "active",
        }],
    )

    payload = server.handle_projects_list(_ctx("aming-claw"))

    assert payload["ok"] is True
    assert payload["projects"][0]["project_id"] == "dashboard-demo"


def test_graph_stale_scope_operation_ignores_outside_workspace_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        server,
        "_graph_governance_project_root",
        lambda _project_id, _body: tmp_path,
    )
    monkeypatch.setattr(server, "_git_head_commit", lambda _root: "head-commit")
    monkeypatch.setattr(
        server,
        "_git_changed_paths_between",
        lambda _root, _base, _target, limit=None: [],
    )

    operation, summary = server._graph_stale_scope_operation(
        "dashboard-demo",
        status={"graph_snapshot_commit": "old-commit"},
        pending_rows=[],
    )

    assert operation is None
    assert summary["is_stale"] is False
    assert summary["head_commit"] == "head-commit"
    assert summary["changed_files"] == []
    assert summary["changed_file_count"] == 0
