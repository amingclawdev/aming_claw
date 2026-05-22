"""Tests for external project governance bootstrap artifacts."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

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
