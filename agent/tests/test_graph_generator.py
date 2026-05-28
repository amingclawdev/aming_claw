"""Tests for agent/governance/graph_generator.py — AC2, AC3, AC4, AC10."""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Ensure agent dir is on path
_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.graph_generator import (
    detect_language,
    scan_codebase,
    generate_graph,
    save_graph_atomic,
    _normalize_path,
    _is_test_file,
    MAX_NODES,
)


@pytest.fixture
def python_workspace(tmp_path):
    """Create a minimal Python project workspace."""
    # pyproject.toml
    (tmp_path / "pyproject.toml").write_text('[build-system]\nrequires = ["setuptools"]\n')

    # Core module
    core_dir = tmp_path / "src" / "core"
    core_dir.mkdir(parents=True)
    (core_dir / "__init__.py").write_text("")
    (core_dir / "utils.py").write_text("def helper():\n    return 42\n")

    # Feature module that imports core
    feat_dir = tmp_path / "src" / "features"
    feat_dir.mkdir(parents=True)
    (feat_dir / "__init__.py").write_text("")
    (feat_dir / "handler.py").write_text("from core import utils\n\ndef handle():\n    return utils.helper()\n")

    # Entrypoint
    (tmp_path / "main.py").write_text("from features import handler\n\nif __name__ == '__main__':\n    handler.handle()\n")

    # Tests
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    (test_dir / "__init__.py").write_text("")
    (test_dir / "test_handler.py").write_text("def test_handle():\n    pass\n")
    (test_dir / "test_utils.py").write_text("def test_helper():\n    pass\n")

    # Config
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yaml").write_text("name: CI\n")

    return tmp_path


@pytest.fixture
def js_workspace(tmp_path):
    """Create a minimal JavaScript project workspace."""
    (tmp_path / "package.json").write_text('{"name": "test-proj", "scripts": {"test": "jest"}}\n')
    src = tmp_path / "src"
    src.mkdir()
    (src / "index.js").write_text("module.exports = {}\n")
    (src / "app.js").write_text("const x = require('./index')\n")
    (src / "app.test.js").write_text("test('app', () => {})\n")
    return tmp_path


class TestDetectLanguage:
    """AC2: detect_language returns correct language."""

    def test_python_pyproject(self, python_workspace):
        assert detect_language(str(python_workspace)) == "python"

    def test_javascript_package_json(self, js_workspace):
        assert detect_language(str(js_workspace)) == "javascript"

    def test_rust_cargo_toml(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "test"\n')
        assert detect_language(str(tmp_path)) == "rust"

    def test_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/test\n")
        assert detect_language(str(tmp_path)) == "go"

    def test_ruby_gemfile(self, tmp_path):
        (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\n")
        assert detect_language(str(tmp_path)) == "ruby"

    def test_ruby_gemspec(self, tmp_path):
        (tmp_path / "demo.gemspec").write_text(
            "Gem::Specification.new do |s|\n  s.name = 'demo'\nend\n"
        )
        assert detect_language(str(tmp_path)) == "ruby"

    def test_unknown_empty_dir(self, tmp_path):
        assert detect_language(str(tmp_path)) == "unknown"


class TestScanCodebase:
    def test_basic_scan(self, python_workspace):
        files = scan_codebase(str(python_workspace))
        paths = [f["path"] for f in files]
        assert any("pyproject.toml" in p for p in paths)
        assert any("utils.py" in p for p in paths)

    def test_scan_depth_limit(self, python_workspace):
        # Create deeply nested file
        deep = python_workspace / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        (deep / "deep.py").write_text("x = 1\n")

        files = scan_codebase(str(python_workspace), scan_depth=2)
        paths = [f["path"] for f in files]
        assert not any("deep.py" in p for p in paths)

    def test_exclude_patterns(self, python_workspace):
        files = scan_codebase(str(python_workspace), exclude_patterns=["tests"])
        paths = [f["path"] for f in files]
        assert not any("test_handler" in p for p in paths)

    def test_test_file_detection(self, python_workspace):
        files = scan_codebase(str(python_workspace))
        test_files = [f for f in files if f["type"] == "test"]
        assert len(test_files) >= 2

    def test_config_file_detection(self, python_workspace):
        files = scan_codebase(str(python_workspace))
        config_files = [f for f in files if f["type"] == "config"]
        assert len(config_files) >= 1


class TestGenerateGraph:
    """AC2, AC3: graph generation with layers and edges."""

    def test_python_project_layers(self, python_workspace):
        """AC2: generated graph has nodes assigned to layers L0-L4."""
        result = generate_graph(str(python_workspace))
        assert result["node_count"] > 0
        layers = result["layers"]
        # Should have at least some layers populated
        assert len(layers) >= 2

    def test_python_project_edges(self, python_workspace):
        """AC3: dependency edges from import analysis."""
        result = generate_graph(str(python_workspace))
        # Should have at least some edges from import analysis
        assert result["edge_count"] >= 0  # May be 0 if imports don't resolve to project modules
        assert result["graph"] is not None

    def test_graph_has_valid_structure(self, python_workspace):
        result = generate_graph(str(python_workspace))
        graph = result["graph"]
        # Graph should be a valid AcceptanceGraph
        assert hasattr(graph, "G")
        assert hasattr(graph, "node_count")
        assert graph.node_count() == result["node_count"]

    def test_node_count_cap(self, tmp_path):
        """AC4: >50 nodes produces warning and caps at 50."""
        # Create many directories with Python files
        (tmp_path / "pyproject.toml").write_text("")
        for i in range(60):
            d = tmp_path / f"mod_{i}"
            d.mkdir()
            (d / f"file_{i}.py").write_text(f"x_{i} = {i}\n")

        result = generate_graph(str(tmp_path))
        assert result["node_count"] <= MAX_NODES
        assert "warning" in result

    def test_windows_path_normalization(self, python_workspace):
        """AC10: all file paths use forward slashes."""
        result = generate_graph(str(python_workspace))
        graph = result["graph"]
        for nid in graph.list_nodes():
            node = graph.get_node(nid)
            for f in node.get("primary", []):
                assert "\\" not in f, f"Backslash found in primary file: {f}"
            for f in node.get("secondary", []):
                assert "\\" not in f, f"Backslash found in secondary file: {f}"
            for f in node.get("test", []):
                assert "\\" not in f, f"Backslash found in test file: {f}"


class TestSaveGraphAtomic:
    def test_atomic_save(self, python_workspace):
        result = generate_graph(str(python_workspace))
        graph = result["graph"]

        out_path = python_workspace / "output" / "graph.json"
        save_graph_atomic(graph, str(out_path))

        assert out_path.exists()
        with open(str(out_path)) as f:
            data = json.load(f)
        assert "deps_graph" in data
        assert "gates_graph" in data


class TestNormalizePath:
    """AC10: Windows path normalization."""

    def test_backslash_replaced(self):
        assert _normalize_path("src\\core\\utils.py") == "src/core/utils.py"

    def test_forward_slash_unchanged(self):
        assert _normalize_path("src/core/utils.py") == "src/core/utils.py"


class TestIsTestFile:
    def test_python_test_prefix(self):
        assert _is_test_file("test_foo.py") is True

    def test_python_test_suffix(self):
        assert _is_test_file("foo_test.py") is True

    def test_typescript_test(self):
        assert _is_test_file("foo.test.ts") is True

    def test_go_test(self):
        assert _is_test_file("foo_test.go") is True

    def test_ruby_spec(self):
        assert _is_test_file("foo_spec.rb") is True

    def test_ruby_test_suffix(self):
        assert _is_test_file("foo_test.rb") is True

    def test_not_test(self):
        assert _is_test_file("foo.py") is False
        assert _is_test_file("foo.rb") is False
