"""Tests for bootstrap_project() and related integration — AC1, AC5-AC9."""

import json
import os
import sys
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure agent dir is on path
_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.graph_generator import detect_language, generate_graph
from project_config import (
    ProjectConfig,
    effective_graph_exclude_roots,
    generate_default_config,
    load_project_config,
)
from governance.preflight import check_bootstrap, _pass, _fail


@pytest.fixture
def python_workspace(tmp_path):
    """Create a minimal Python project workspace."""
    (tmp_path / "pyproject.toml").write_text('[build-system]\nrequires = ["setuptools"]\n')
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("print('hello')\n")
    (src / "utils.py").write_text("def helper(): return 1\n")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_main.py").write_text("def test_main(): pass\n")
    return tmp_path


@pytest.fixture
def empty_workspace(tmp_path):
    """Workspace with no config files."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.py").write_text("x = 1\n")
    return tmp_path


@pytest.fixture
def governance_db(tmp_path):
    """Create a minimal governance DB for testing."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS node_state (
        project_id TEXT NOT NULL,
        node_id TEXT NOT NULL,
        verify_status TEXT NOT NULL DEFAULT 'pending',
        build_status TEXT NOT NULL DEFAULT 'impl:missing',
        evidence_json TEXT,
        updated_by TEXT,
        updated_at TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (project_id, node_id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS project_version (
        project_id TEXT PRIMARY KEY,
        chain_version TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        updated_by TEXT NOT NULL,
        git_head TEXT DEFAULT '',
        dirty_files TEXT DEFAULT '[]',
        git_synced_at TEXT DEFAULT ''
    )""")
    conn.commit()
    return conn


class TestGenerateDefaultConfig:
    """AC7: Generate sensible defaults without writing files."""

    def test_python_project(self, python_workspace):
        config = generate_default_config(str(python_workspace))
        assert config.language == "python"
        assert "pytest" in config.testing.unit_command

    def test_javascript_project(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name": "test"}\n')
        config = generate_default_config(str(tmp_path))
        assert config.language == "javascript"
        assert "npm test" in config.testing.unit_command

    def test_rust_project(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "test"\n')
        config = generate_default_config(str(tmp_path))
        assert config.language == "rust"
        assert "cargo test" in config.testing.unit_command

    def test_go_project(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/test\n")
        config = generate_default_config(str(tmp_path))
        assert config.language == "go"
        assert "go test" in config.testing.unit_command

    def test_no_files_written(self, python_workspace):
        """AC7: No files written to workspace_path."""
        before = set(os.listdir(str(python_workspace)))
        generate_default_config(str(python_workspace))
        after = set(os.listdir(str(python_workspace)))
        assert before == after, "generate_default_config should not write files to workspace"

    def test_docker_strategy_detected(self, tmp_path):
        (tmp_path / "Dockerfile").write_text("FROM python:3.11\n")
        (tmp_path / "pyproject.toml").write_text("")
        config = generate_default_config(str(tmp_path))
        assert config.deploy.strategy == "docker"

    def test_custom_project_name(self, python_workspace):
        config = generate_default_config(str(python_workspace), project_name="my-proj")
        assert config.project_id == "my-proj"


class TestCheckBootstrap:
    """AC9: check_bootstrap() pass/fail conditions."""

    def test_pass_with_nodes_and_version(self, governance_db):
        """AC9: pass when graph has nodes, node_state populated, version exists."""
        conn = governance_db
        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("test-proj", "L0.1", "pending", "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?)",
            ("test-proj", "bootstrap", "2026-01-01T00:00:00Z", "bootstrap"),
        )
        conn.commit()

        result = check_bootstrap(conn, "test-proj")
        assert result["status"] == "pass"
        assert result["details"]["node_count"] >= 1
        assert result["details"]["version_exists"] is True

    def test_fail_no_nodes(self, governance_db):
        """AC9: fail when no nodes in node_state."""
        conn = governance_db
        conn.execute(
            "INSERT INTO project_version (project_id, chain_version, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?)",
            ("test-proj", "bootstrap", "2026-01-01T00:00:00Z", "bootstrap"),
        )
        conn.commit()

        result = check_bootstrap(conn, "test-proj")
        assert result["status"] == "fail"
        assert "no nodes" in str(result["details"].get("failures", []))

    def test_fail_no_version(self, governance_db):
        """AC9: fail when no project_version record."""
        conn = governance_db
        conn.execute(
            "INSERT INTO node_state (project_id, node_id, verify_status, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("test-proj", "L0.1", "pending", "2026-01-01T00:00:00Z"),
        )
        conn.commit()

        result = check_bootstrap(conn, "test-proj")
        assert result["status"] == "fail"
        assert "no project_version" in str(result["details"].get("failures", []))

    def test_fail_empty_project(self, governance_db):
        """AC9: fail when both nodes and version are missing."""
        result = check_bootstrap(governance_db, "nonexistent-proj")
        assert result["status"] == "fail"


class TestCodeDocMapIntegration:
    """AC5: code_doc_map.json loaded by ImpactAnalyzer."""

    def test_code_doc_map_in_impact_analyzer(self):
        """AC5: Grep-verifiable: 'code_doc_map.json' in impact_analyzer.py."""
        impact_analyzer_path = Path(__file__).parent.parent / "governance" / "impact_analyzer.py"
        content = impact_analyzer_path.read_text(encoding="utf-8")
        assert "code_doc_map.json" in content

    def test_load_project_code_doc_map_fallback(self):
        """When no project-specific map exists, falls back to CODE_DOC_MAP."""
        from governance.impact_analyzer import _load_project_code_doc_map, CODE_DOC_MAP
        result = _load_project_code_doc_map("nonexistent-project-xyz")
        assert result == CODE_DOC_MAP

    def test_load_project_code_doc_map_from_file(self, tmp_path):
        """When code_doc_map.json exists, it is loaded."""
        from governance.impact_analyzer import _load_project_code_doc_map

        custom_map = {"src/app.py": ["docs/app.md"]}
        cdm_path = tmp_path / "test-proj" / "code_doc_map.json"
        cdm_path.parent.mkdir(parents=True)
        cdm_path.write_text(json.dumps(custom_map))

        with patch("governance.db._governance_root", return_value=tmp_path):
            result = _load_project_code_doc_map("test-proj")
        assert result == custom_map


class TestBackwardCompatibility:
    """AC8: Existing functions unaffected."""

    def test_init_project_signature(self):
        """AC8: init_project() signature unchanged — verified via source inspection."""
        # Verify signature by reading source (avoids import chain issues on Py3.9)
        ps_path = Path(__file__).parent.parent / "governance" / "project_service.py"
        content = ps_path.read_text(encoding="utf-8")
        assert "def init_project(project_id: str, password: str" in content

    def test_import_graph_signature(self):
        """AC8: import_graph() signature unchanged — verified via source inspection."""
        ps_path = Path(__file__).parent.parent / "governance" / "project_service.py"
        content = ps_path.read_text(encoding="utf-8")
        assert "def import_graph(project_id: str, md_path: str)" in content

    def test_load_project_config_still_works(self, python_workspace):
        """AC8: load_project_config still works for workspace with config."""
        (python_workspace / ".aming-claw.yaml").write_text(
            "project_id: test-proj\nlanguage: python\n"
        )
        config = load_project_config(python_workspace)
        assert config.project_id == "test-proj"

    def test_load_project_config_parses_governance_exclude_roots(self, python_workspace):
        (python_workspace / ".aming-claw.yaml").write_text(
            "\n".join([
                "project_id: test-proj",
                "language: python",
                "governance:",
                "  enabled: true",
                "  exclude_roots:",
                "    - examples",
                "    - sandbox/demo",
                "",
            ])
        )
        config = load_project_config(python_workspace)
        assert config.governance.exclude_roots == ["examples", "sandbox/demo"]

    def test_load_project_config_parses_graph_and_ai_routing(self, python_workspace):
        (python_workspace / ".aming-claw.yaml").write_text(
            "\n".join([
                "version: 2",
                "project_id: test-proj",
                "language: python",
                "governance:",
                "  exclude_roots:",
                "    - legacy-demo",
                "graph:",
                "  exclude_paths:",
                "    - examples",
                "    - docs/dev",
                "  ignore_globs:",
                "    - '**/dist/**'",
                "  nested_projects:",
                "    mode: exclude",
                "    roots:",
                "      - sandbox/project-a",
                "ai:",
                "  routing:",
                "    pm:",
                "      provider: openai",
                "      model: gpt-5.5",
                "    semantic:",
                "      provider: anthropic",
                "      model: claude-opus-4-7",
                "",
            ])
        )

        config = load_project_config(python_workspace)

        assert config.graph.exclude_paths == ["examples", "docs/dev"]
        assert config.graph.ignore_globs == ["**/dist/**"]
        assert config.graph.nested_projects.roots == ["sandbox/project-a"]
        assert config.ai.routing["pm"] == {"provider": "openai", "model": "gpt-5.5"}
        assert config.ai.routing["semantic"]["model"] == "claude-opus-4-7"
        assert effective_graph_exclude_roots(config) == [
            "legacy-demo",
            "examples",
            "docs/dev",
            "sandbox/project-a",
        ]


class TestGraphGeneratorAC10:
    """AC10: as_posix or replace verifiable in graph_generator.py."""

    def test_replace_in_graph_generator(self):
        """AC10: Grep-verifiable: 'replace' in graph_generator.py."""
        gg_path = Path(__file__).parent.parent / "governance" / "graph_generator.py"
        content = gg_path.read_text(encoding="utf-8")
        # Must contain replace or as_posix for path normalization
        assert "replace" in content or "as_posix" in content


class TestIdempotentBootstrap:
    """AC6: Calling bootstrap twice returns same project_id, no duplicates."""

    def test_generate_graph_idempotent(self, python_workspace):
        """AC6: Generating graph twice produces same structure."""
        result1 = generate_graph(str(python_workspace))
        result2 = generate_graph(str(python_workspace))
        assert result1["node_count"] == result2["node_count"]
        assert result1["edge_count"] == result2["edge_count"]
        assert result1["layers"] == result2["layers"]

    def test_generate_graph_respects_configured_exclude_patterns(self, python_workspace):
        examples = python_workspace / "examples" / "demo"
        examples.mkdir(parents=True)
        (examples / "app.py").write_text("def demo(): return 1\n")

        result = generate_graph(str(python_workspace), exclude_patterns=["examples"])
        node_files = []
        for _node_id, node in result["graph"].G.nodes(data=True):
            node_files.extend(node.get("primary", []) or [])
            node_files.extend(node.get("secondary", []) or [])
            node_files.extend(node.get("test", []) or [])

        assert all(not str(path).startswith("examples/") for path in node_files)


def test_bootstrap_project_uses_snapshot_full_reconcile(tmp_path, monkeypatch):
    from agent.governance import db as gov_db
    from agent.governance import project_service
    from agent.governance import state_reconcile

    state_root = tmp_path / "state"
    monkeypatch.setattr(gov_db, "_governance_root", lambda: state_root)
    monkeypatch.setattr(project_service, "_governance_root", lambda: state_root)

    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    (workspace / ".aming-claw.yaml").write_text(
        "\n".join([
            "version: 2",
            "project_id: bootstrap-demo",
            "language: python",
            "graph:",
            "  exclude_paths:",
            "    - examples",
            "",
        ]),
        encoding="utf-8",
    )

    observed = {}

    def fake_reconcile(conn, project_id, project_root, **kwargs):
        observed.update({
            "project_id": project_id,
            "project_root": str(project_root),
            **kwargs,
        })
        return {
            "ok": True,
            "snapshot_id": "full-bootstrap-demo",
            "activation": {"snapshot_id": "full-bootstrap-demo", "projection_status": "rebuilt"},
            "graph_stats": {"node_count": 0, "edge_count": 0, "layers": {"L7": 1}},
            "index_counts": {"nodes": 3, "edges": 2},
        }

    monkeypatch.setattr(state_reconcile, "run_state_only_full_reconcile", fake_reconcile)

    result = project_service.bootstrap_project(str(workspace), exclude_patterns=["node"])

    assert result["bootstrap_mode"] == "snapshot_full_reconcile"
    assert result["snapshot_id"] == "full-bootstrap-demo"
    assert result["graph_stats"]["node_count"] == 3
    assert observed["project_id"] == "bootstrap-demo"
    assert observed["activate"] is True
    assert observed["semantic_use_ai"] is False
    assert observed["semantic_enqueue_stale"] is False
    assert observed["notes_extra"]["effective_exclude_roots"] == ["examples", "node"]
    assert observed["graph_exclude_paths"] == ["examples", "node"]


def test_bootstrap_git_gate_rejects_dirty_worktree(tmp_path, monkeypatch):
    from agent.governance import project_service

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class _Proc:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if "rev-parse" in args:
            return _Proc(stdout=str(workspace))
        if "status" in args:
            return _Proc(stdout=" M src/app.py\n?? notes.md\n")
        return _Proc(returncode=1, stderr="unexpected")

    monkeypatch.setattr(project_service.subprocess, "run", fake_run)

    with pytest.raises(project_service.ValidationError, match="dirty git worktree"):
        project_service._ensure_clean_git_worktree_for_graph(workspace)

    assert any("rev-parse" in call for call in calls)
    assert any("status" in call for call in calls)


def test_bootstrap_git_gate_allows_non_git_workspace(tmp_path, monkeypatch):
    from agent.governance import project_service

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class _Proc:
        returncode = 128
        stdout = ""
        stderr = "not a git repository"

    monkeypatch.setattr(project_service.subprocess, "run", lambda *args, **kwargs: _Proc())

    assert project_service._ensure_clean_git_worktree_for_graph(workspace) == {
        "is_git_repo": False,
        "dirty": False,
    }
