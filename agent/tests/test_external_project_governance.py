"""Tests for external project governance bootstrap artifacts."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.governance import preflight as preflight_module
from agent.governance import project_service as project_service_module
from agent.governance.errors import ValidationError
from agent.governance.external_project_governance import (
    COVERAGE_STATE_FILE,
    FEATURE_INDEX_FILE,
    GOVERNANCE_DIR,
    scan_external_project,
)
from agent.governance.project_service import (
    bootstrap_project,
    inspect_target_workspace_pollution,
)
from agent.governance.project_profile import discover_project_profile
from agent.governance.reconcile_file_inventory import build_file_inventory
from agent.governance.server import handle_version_check
from agent.mcp.tools import ToolDispatcher


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _copy_demo_project(tmp_path: Path) -> Path:
    source = _repo_root() / "examples" / "external-governance-demo"
    project = tmp_path / "external-demo"
    shutil.copytree(source, project)
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial external demo"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    return project


def _external_preflight_db(project_id: str, git_head: str) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    now = datetime.now(timezone.utc).isoformat()
    conn.executescript("""
        CREATE TABLE node_state (
            project_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            verify_status TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE TABLE node_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT,
            node_id TEXT,
            from_status TEXT,
            to_status TEXT,
            role TEXT,
            evidence_json TEXT,
            session_id TEXT,
            ts TEXT,
            version INTEGER
        );
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'created',
            type TEXT NOT NULL DEFAULT 'task',
            updated_at TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            parent_task_id TEXT,
            metadata_json TEXT
        );
        CREATE TABLE task_attempts (id INTEGER PRIMARY KEY AUTOINCREMENT);
        CREATE TABLE project_version (
            project_id TEXT PRIMARY KEY,
            chain_version TEXT,
            updated_at TEXT,
            updated_by TEXT,
            git_head TEXT,
            dirty_files TEXT,
            git_synced_at TEXT
        );
        CREATE TABLE sessions (session_id TEXT PRIMARY KEY, project_id TEXT, role TEXT, created_at TEXT);
        CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    conn.execute(
        "INSERT INTO project_version VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project_id, "bootstrap", now, "test", git_head, "[]", now),
    )
    conn.commit()
    return conn


class _VersionCtx:
    body = {}
    query = {}

    def __init__(self, project_id: str):
        self._project_id = project_id

    def get_project_id(self) -> str:
        return self._project_id


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _ManagerRecorder:
    def __init__(self, runtime_version: str):
        self.runtime_version = runtime_version

    def api(self, method: str, path: str, data: dict | None = None) -> dict:
        if path == "/api/manager/health":
            return {"ok": True, "runtime_version": self.runtime_version}
        return {"ok": True, "method": method, "path": path, "data": data}


def test_profile_excludes_project_local_aming_claw_workspace(tmp_path):
    project = _copy_demo_project(tmp_path)
    (project / GOVERNANCE_DIR / "sessions" / "old").mkdir(parents=True)
    (project / GOVERNANCE_DIR / "sessions" / "old" / "generated.py").write_text(
        "def generated():\n    return 1\n",
        encoding="utf-8",
    )

    profile = discover_project_profile(str(project))
    rows = build_file_inventory(project_root=str(project), run_id="scan-exclude", profile=profile)
    paths = {row["path"] for row in rows}

    assert GOVERNANCE_DIR in profile.exclude_roots
    assert ".aming-claw/sessions/old/generated.py" not in paths


def test_bootstrap_rejects_self_plugin_artifacts_in_external_target(tmp_path):
    project = tmp_path / "my-app"
    (project / "src").mkdir(parents=True)
    (project / "src" / "App.js").write_text("export default function App() { return null; }\n", encoding="utf-8")
    (project / "package.json").write_text('{"scripts":{"test":"echo ok"}}\n', encoding="utf-8")
    (project / ".mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "aming-claw": {
                    "command": "python",
                    "args": ["-m", "agent.mcp.server", "--project", "aming-claw"],
                }
            }
        }),
        encoding="utf-8",
    )
    (project / "shared-volume" / "codex-tasks").mkdir(parents=True)

    pollution = inspect_target_workspace_pollution(project)

    assert pollution["ok"] is False
    assert {issue["path"] for issue in pollution["issues"]} == {
        ".mcp.json",
        "shared-volume/codex-tasks",
    }
    with pytest.raises(ValidationError, match="Aming Claw plugin/runtime artifacts"):
        bootstrap_project(str(project), project_name="my-app")


def test_external_project_version_runtime_and_preflight_use_registered_root(tmp_path, monkeypatch):
    project_id = "external-version-fixture"
    project = tmp_path / "external-version-fixture"
    (project / "src").mkdir(parents=True)
    (project / "docs").mkdir(parents=True)
    (project / "src" / "service.py").write_text(
        "def value():\n    return 42\n",
        encoding="utf-8",
    )
    doc_path = project / "docs" / "runbook.md"
    doc_path.write_text("# Runbook\n\nInitial notes.\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial external fixture"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    external_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        text=True,
    ).strip()
    external_short = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=project,
        text=True,
    ).strip()
    governance_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=_repo_root(),
        text=True,
    ).strip()
    governance_short = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=_repo_root(),
        text=True,
    ).strip()
    assert external_head != governance_head

    entry = {
        "project_id": project_id,
        "name": project_id,
        "workspace_path": str(project),
        "initialized": True,
        "status": "active",
    }
    monkeypatch.setattr(
        project_service_module,
        "get_project",
        lambda pid: entry if pid == project_id else None,
    )
    monkeypatch.setattr(project_service_module, "list_projects", lambda: [entry])
    monkeypatch.setattr(
        preflight_module,
        "check_plugin_update_state",
        lambda state_path=None: {
            "status": "pass",
            "details": {
                "state_path": "test-plugin-state.json",
                "state_exists": True,
                "update_status": "current",
                "blockers": [],
                "warnings": [],
            },
        },
    )
    monkeypatch.setattr(
        "agent.governance.chain_trailer.get_runtime_version",
        lambda: governance_short,
    )
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response({"runtime_version": governance_short}),
    )

    conn = _external_preflight_db(project_id, external_head)
    monkeypatch.setattr("agent.governance.server.get_connection", lambda *_args, **_kwargs: conn)

    server_version = handle_version_check(_VersionCtx(project_id))
    assert server_version["project_root"] == str(project.resolve())
    assert server_version["target_project_root"] == str(project.resolve())
    assert server_version["head"] == external_head
    assert server_version["target_head"] == external_head
    assert server_version["target_chain_version"] == external_short
    assert server_version["target_project_version"]["project_root"] == str(project.resolve())
    assert server_version["target_project_version"]["head"] == external_head
    assert server_version["target_project_version"]["chain_version"] == external_short
    assert server_version["legacy_project_version"]["git_head"] == external_head
    assert server_version["governance_chain_version"] == governance_short
    assert server_version["governance_runtime"]["chain_version"] == governance_short
    assert server_version["governance_runtime"]["chain_version"] != server_version["target_project_version"]["chain_version"]
    assert server_version["target_synced_with_governance"] is True

    stale_governance_sync_version = dict(server_version)
    stale_governance_sync_version["ok"] = True
    stale_governance_sync_version["governance_synced_head"] = governance_head
    stale_governance_sync_version["target_synced_with_governance"] = False
    stale_governance_sync_version["message"] = (
        f"governance synced HEAD ({governance_head}) differs from target HEAD ({external_head})"
    )
    stale_governance_sync_version["target_project_version"] = dict(
        server_version["target_project_version"],
        synced_with_governance=False,
        governance_synced_head=governance_head,
        legacy_project_version=dict(
            server_version["legacy_project_version"],
            git_head=governance_head,
            synced_with_target=False,
        ),
    )
    stale_governance_sync_version["legacy_project_version"] = dict(
        server_version["legacy_project_version"],
        git_head=governance_head,
        synced_with_target=False,
    )

    def api(method: str, path: str, data: dict | None = None) -> dict:
        if path == "/api/health":
            return {"status": "ok", "version": governance_short}
        if path == f"/api/version-check/{project_id}":
            return dict(stale_governance_sync_version)
        return {"ok": True, "method": method, "path": path, "data": data}

    dispatcher = ToolDispatcher(
        api_fn=api,
        worker_pool=None,
        service_mgr=None,
        manager_api_fn=_ManagerRecorder(governance_short).api,
        workspace=str(project),
    )

    mcp_version = dispatcher.dispatch("version_check", {"project_id": project_id})
    assert mcp_version["mcp_workspace_root"] == str(project.resolve())
    assert mcp_version["ok"] is True
    assert mcp_version["head"] == external_head
    assert mcp_version["target_head"] == external_head
    assert mcp_version["target_project_root"] == str(project.resolve())
    assert mcp_version["mcp_workspace_head"] != governance_head
    assert mcp_version["target_project_version"]["head"] == external_head
    assert mcp_version["governance_synced_head"] == governance_head
    assert mcp_version["target_synced_with_governance"] is False
    assert mcp_version["governance_sync_diagnostics"] == {
        "mismatch": True,
        "governance_synced_head": governance_head,
        "target_head": external_head,
        "target_project_root": str(project.resolve()),
        "external_target_root": True,
        "affects_ok": False,
    }

    runtime = dispatcher.dispatch("runtime_status", {"project_id": project_id})
    assert runtime["target_project_version"]["project_root"] == str(project.resolve())
    assert runtime["target_project_version"]["head"] == external_head
    assert runtime["target_project_version"]["chain_version"] == external_short
    assert runtime["governance_runtime"]["chain_version"] == governance_short
    assert runtime["governance_runtime"]["chain_version"] != runtime["target_project_version"]["chain_version"]

    (project / ".worktrees" / "stale-external").mkdir(parents=True)
    doc_path.write_text(
        "# Runbook\n\n<!-- governance-hint {\"attach_to_node\":{\"target_title\":\"External\"}} -->\n",
        encoding="utf-8",
    )
    preflight = preflight_module.run_preflight(conn, project_id)
    assert preflight["project_root"] == str(project.resolve())
    assert preflight["checks"]["version"]["details"]["project_root"] == str(project.resolve())
    assert preflight["checks"]["version"]["details"]["git_head"] == external_short
    assert preflight["checks"]["coverage"]["details"] == {
        "skipped": True,
        "reason": "no_code_doc_map_for_external_project",
        "project_root": str(project.resolve()),
    }
    stale_worktrees = preflight["checks"]["batch_worktrees"]["details"]["stale_worktrees"]
    assert stale_worktrees == [str(project / ".worktrees" / "stale-external")]
    hints = preflight["checks"]["pending_governance_hints"]["details"]["pending_governance_hints"]
    assert hints == [{"path": "docs/runbook.md", "status": "M"}]


def test_scan_external_project_writes_governance_artifacts(tmp_path):
    project = _copy_demo_project(tmp_path)

    result = scan_external_project(
        project,
        project_id="external-demo",
        session_id="full_reconcile-demo1234-test0001",
    )

    gov_root = project / GOVERNANCE_DIR
    candidate_path = Path(result["candidate_graph_path"])
    symbol_index_path = Path(result["symbol_index_path"])
    doc_index_path = Path(result["doc_index_path"])
    inventory_path = Path(result["file_inventory_path"])
    coverage_path = Path(result["coverage_state_path"])
    coverage_cache_path = Path(result["coverage_state_cache_path"])
    feature_index_path = gov_root / FEATURE_INDEX_FILE

    assert result["status"] == "ok"
    assert (gov_root / "project.yaml").exists()
    assert candidate_path.exists()
    assert symbol_index_path.exists()
    assert doc_index_path.exists()
    assert inventory_path.exists()
    assert coverage_path.exists()
    assert coverage_path == gov_root / COVERAGE_STATE_FILE
    assert coverage_cache_path.exists()
    assert feature_index_path.exists()
    assert ".aming-claw/cache/" in (project / ".gitignore").read_text(encoding="utf-8")

    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    nodes = candidate["deps_graph"]["nodes"]
    assert candidate["hierarchy_graph"]["links"]
    source_nodes = [node for node in nodes if node.get("primary")]
    primary_paths = {
        path
        for node in source_nodes
        for path in (node.get("primary") or [])
    }
    assert "src/demo_app/service.py" in primary_paths
    assert "web/widget.js" in primary_paths

    symbol_index = json.loads(symbol_index_path.read_text(encoding="utf-8"))
    symbol_ids = {item["id"] for item in symbol_index["symbols"]}
    assert any(symbol_id.endswith("::calculate_total") for symbol_id in symbol_ids)
    assert any(item["id"] == "file::web/widget.js" for item in symbol_index["symbols"])
    service_symbol = next(
        item for item in symbol_index["symbols"]
        if item["id"].endswith("::calculate_total")
    )
    assert service_symbol["line_start"] > 0
    assert service_symbol["line_end"] >= service_symbol["line_start"]

    doc_index = json.loads(doc_index_path.read_text(encoding="utf-8"))
    readme_doc = next(item for item in doc_index["documents"] if item["path"] == "README.md")
    assert readme_doc["headings"][0]["title"] == "External Governance Demo"

    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    rows = {row["path"]: row for row in inventory}
    assert rows["README.md"]["file_kind"] == "index_doc"
    assert rows["README.md"]["scan_status"] == "index_asset"
    assert rows["src/demo_app/service.py"]["sha256"]
    assert rows["src/demo_app/service.py"]["file_hash"].startswith("sha256:")
    assert rows["src/demo_app/service.py"]["size_bytes"] > 0
    assert rows["src/demo_app/service.py"]["last_scanned_commit"] == result["base_commit"]
    assert rows["src/demo_app/__init__.py"]["scan_status"] == "clustered"
    assert rows["src/demo_app/__init__.py"]["graph_status"] == "mapped"
    assert rows["src/demo_app/__init__.py"]["mapped_node_ids"]
    assert all(not row["path"].startswith(".aming-claw/") for row in inventory)

    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    assert coverage["source_leaf_count"] >= 2
    assert coverage["symbol_count"] == symbol_index["symbol_count"]
    assert coverage["doc_heading_count"] == doc_index["heading_count"]
    assert coverage["file_hashes"]["src/demo_app/service.py"] == rows["src/demo_app/service.py"]["file_hash"]
    assert coverage["file_states"]["src/demo_app/service.py"]["file_hash"] == rows["src/demo_app/service.py"]["file_hash"]
    assert coverage["file_states"]["src/demo_app/service.py"]["last_scanned_commit"] == result["base_commit"]
    assert coverage["file_states"]["src/demo_app/__init__.py"]["graph_status"] == "mapped"
    assert "confidence" not in json.dumps(coverage)
    assert "confidence" not in json.dumps(symbol_index)
    assert "confidence" not in json.dumps(doc_index)
    assert "Aming Claw Feature Index" in feature_index_path.read_text(encoding="utf-8")
