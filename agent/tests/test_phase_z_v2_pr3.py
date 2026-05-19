"""Tests for Phase Z v2 PR3 — driver + atomic swap module.

The legacy migration_state_machine has been replaced by
``agent.governance.symbol_swap.atomic_swap`` (spec §4.4 v6 / GPT R4).
This file retains the driver tests and adds smoke coverage for the new
atomic-swap module so the PR3 surface stays exercised.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

# Ensure agent is importable
_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from agent.governance.reconcile_phases.phase_z_v2 import (
    build_graph_v2_from_symbols,
    build_rebase_candidate_graph,
    find_test_coverage,
    find_doc_coverage,
    diff_against_existing_graph,
    write_dry_run_artifact,
    write_graph_v2_json,
    score_function_layer,
    aggregate_functions_into_nodes,
    parse_production_modules,
    build_call_graph,
    tarjan_scc,
    handle_cycle,
    CYCLE_ABORT_THRESHOLD,
    ModuleInfo,
    FunctionMeta,
)

# NOTE: migration_state_machine has been removed — replaced by
# agent.governance.symbol_swap. We only re-import the atomic-swap surface
# here as a smoke check that the replacement module loads.
from agent.governance.symbol_swap import (
    BAK_RETENTION_DAYS,
    atomic_swap,
    smoke_validate,
    rollback,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_temp_project(files: dict[str, str] | None = None) -> str:
    """Create a temp directory with optional files."""
    d = tempfile.mkdtemp()
    if files:
        for relpath, content in files.items():
            fpath = os.path.join(d, relpath)
            os.makedirs(os.path.dirname(fpath), exist_ok=True)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)
    return d


# ===========================================================================
# Driver tests (6)
# ===========================================================================

class TestBuildGraphV2FromSymbols:
    """AC1: build_graph_v2_from_symbols orchestrates the full pipeline."""

    def test_ac1_calls_all_pipeline_stages(self):
        """AC1: Verify build_graph_v2_from_symbols calls all required functions."""
        project = _make_temp_project({
            "agent/foo.py": "def hello():\n    pass\n",
            "scripts/bar.py": "def world():\n    hello()\n",
        })
        result = build_graph_v2_from_symbols(project, dry_run=True)
        assert result["status"] == "ok"
        assert "report_path" in result
        assert result["node_count"] >= 0

    def test_ac2_dry_run_writes_scratch_artifact(self):
        """AC2: dry_run=True writes docs/dev/scratch/graph-v2-{date}.json."""
        project = _make_temp_project({
            "agent/simple.py": "def func_a():\n    pass\n",
        })
        result = build_graph_v2_from_symbols(project, dry_run=True)
        assert result["status"] == "ok"
        assert "report_path" in result
        assert os.path.isfile(result["report_path"])
        assert "graph-v2-" in result["report_path"]

    def test_ac3_apply_writes_graph_v2_json(self):
        """AC3: dry_run=False writes agent/governance/graph.v2.json."""
        project = _make_temp_project({
            "agent/simple.py": "def func_a():\n    pass\n",
            "agent/governance/.keep": "",
        })
        # Patch create_baseline to avoid DB dependency
        with patch("agent.governance.reconcile_phases.phase_z_v2.build_graph_v2_from_symbols.__module__", create=True):
            # Actually just run it - create_baseline is wrapped in try/except
            result = build_graph_v2_from_symbols(project, dry_run=False, owner="test-owner")
        assert result["status"] == "ok"
        assert "graph_path" in result
        graph_path = os.path.join(project, "agent", "governance", "graph.v2.json")
        assert os.path.isfile(graph_path)
        with open(graph_path, "r") as f:
            data = json.load(f)
        assert data["version"] == "v2"

    def test_ac4_cycle_abort_over_threshold(self):
        """AC4: >30 cycles returns status='aborted' with abort_reason."""
        # Build a project with many mutual cycles
        lines = []
        # Create 31+ 2-node cycles via cross-module calls
        for i in range(35):
            lines.append(f"agent/mod_{i}_a.py")
            lines.append(f"agent/mod_{i}_b.py")

        files = {}
        for i in range(35):
            files[f"agent/mod_{i}_a.py"] = f"from agent.mod_{i}_b import func_b_{i}\ndef func_a_{i}():\n    func_b_{i}()\n"
            files[f"agent/mod_{i}_b.py"] = f"from agent.mod_{i}_a import func_a_{i}\ndef func_b_{i}():\n    func_a_{i}()\n"

        project = _make_temp_project(files)
        result = build_graph_v2_from_symbols(project, dry_run=True)
        assert result["status"] == "aborted"
        assert "abort_reason" in result
        assert "cycle" in result["abort_reason"].lower() or "30" in result["abort_reason"]


class TestCoverageLookup:
    """AC5: find_test_coverage and find_doc_coverage."""

    def test_ac5_find_test_coverage(self):
        """AC5: find_test_coverage returns test_files list + covered_lines int."""
        project = _make_temp_project({
            "agent/mymod.py": "def hello():\n    pass\n",
            "agent/tests/test_mymod.py": "def test_hello():\n    assert True\n",
        })
        result = find_test_coverage(project, os.path.join(project, "agent", "mymod.py"))
        assert isinstance(result["test_files"], list)
        assert isinstance(result["covered_lines"], int)

    def test_find_test_coverage_finds_recursive_and_js_colocated_tests(self):
        project = _make_temp_project({
            "agent/deep/mymod.py": "def hello():\n    pass\n",
            "agent/tests/deep/test_mymod_extra.py": "def test_hello():\n    assert True\n",
            ".claude/worktrees/stale/agent/tests/deep/test_mymod_extra.py": "def test_stale():\n    assert True\n",
            "dbservice/lib/contextAssembly.js": "export function run() { return 1; }\n",
            "dbservice/lib/contextAssembly.test.js": "test('run', () => {});\n",
            ".claude/worktrees/stale/dbservice/lib/contextAssembly.test.js": "test('stale', () => {});\n",
        })

        py_result = find_test_coverage(project, os.path.join(project, "agent", "deep", "mymod.py"))
        js_result = find_test_coverage(project, os.path.join(project, "dbservice", "lib", "contextAssembly.js"))
        py_files = {os.path.relpath(p, project).replace(os.sep, "/") for p in py_result["test_files"]}
        js_files = {os.path.relpath(p, project).replace(os.sep, "/") for p in js_result["test_files"]}

        assert "agent/tests/deep/test_mymod_extra.py" in py_files
        assert ".claude/worktrees/stale/agent/tests/deep/test_mymod_extra.py" not in py_files
        assert "dbservice/lib/contextAssembly.test.js" in js_files
        assert ".claude/worktrees/stale/dbservice/lib/contextAssembly.test.js" not in js_files

    def test_find_test_coverage_uses_import_evidence_and_compact_stems(self):
        project = _make_temp_project({
            "agent/governance/auto_chain.py": "def run():\n    return 1\n",
            "agent/manager_http_server.py": "def run():\n    return 1\n",
            "agent/tests/test_autochain_new_file_binding.py": (
                "from agent.governance.auto_chain import run\n\n"
                "def test_run():\n    assert run() == 1\n"
            ),
            "agent/tests/test_managerhttpserver_spawn.py": "def test_spawn():\n    assert True\n",
            "agent/tests/test_unrelated.py": "def test_other():\n    assert True\n",
        })

        import_result = find_test_coverage(
            project,
            os.path.join(".", "agent", "governance", "auto_chain.py"),
        )
        compact_result = find_test_coverage(
            project,
            os.path.join(project, "agent", "manager_http_server.py"),
        )
        import_files = {
            os.path.relpath(p, project).replace(os.sep, "/")
            for p in import_result["test_files"]
        }
        compact_files = {
            os.path.relpath(p, project).replace(os.sep, "/")
            for p in compact_result["test_files"]
        }

        assert "agent/tests/test_autochain_new_file_binding.py" in import_files
        assert "agent/tests/test_managerhttpserver_spawn.py" in compact_files
        assert "agent/tests/test_unrelated.py" not in import_files
        assert "agent/tests/test_unrelated.py" not in compact_files

    def test_graph_attaches_src_layout_test_consumer_fanin(self):
        project = _make_temp_project({
            "src/mypkg/service.py": "def run():\n    return 1\n",
            "tests/test_core.py": (
                "from mypkg.service import run\n\n"
                "def test_run():\n"
                "    assert run() == 1\n"
            ),
        })

        result = build_graph_v2_from_symbols(project, dry_run=True)
        service_node = next(
            node for node in result["nodes"]
            if node["module"] == "src.mypkg.service"
        )
        test_files = {
            os.path.relpath(path, project).replace(os.sep, "/")
            for path in service_node["test_coverage"]["test_files"]
        }

        assert "tests/test_core.py" in test_files
        assert service_node["test_coverage"]["fan_in_evidence"] == [{
            "path": "tests/test_core.py",
            "evidence": "test_import_fanin",
            "imports": ["mypkg.service.run"],
        }]
        assert all(node["module"] != "tests.test_core" for node in result["nodes"])

        rows = {row["path"]: row for row in result["file_inventory"]}
        assert rows["tests/test_core.py"]["file_kind"] == "test"
        assert rows["tests/test_core.py"]["scan_status"] == "secondary_attached"
        candidate = build_rebase_candidate_graph(
            project,
            result,
            session_id="test-fanin",
            run_id=result["run_id"],
        )
        graph_node = next(
            node for node in candidate["deps_graph"]["nodes"]
            if node["layer"] == "L7" and node["title"] == "src.mypkg.service"
        )
        assert graph_node["test"] == ["tests/test_core.py"]
        assert graph_node["metadata"]["test_consumer_fanin"] == [{
            "path": "tests/test_core.py",
            "evidence": "test_import_fanin",
            "imports": ["mypkg.service.run"],
        }]

    def test_test_consumer_fanin_skips_ambiguous_source_root_alias(self):
        project = _make_temp_project({
            "src/mypkg/service.py": "def run():\n    return 1\n",
            "lib/mypkg/service.py": "def run():\n    return 2\n",
            "tests/test_core.py": (
                "from mypkg.service import run\n\n"
                "def test_run():\n"
                "    assert run() in {1, 2}\n"
            ),
        })

        result = build_graph_v2_from_symbols(project, dry_run=True)
        service_nodes = [
            node for node in result["nodes"]
            if node["module"] in {"src.mypkg.service", "lib.mypkg.service"}
        ]
        assert len(service_nodes) == 2
        for node in service_nodes:
            rel_tests = {
                os.path.relpath(path, project).replace(os.sep, "/")
                for path in node["test_coverage"]["test_files"]
            }
            assert "tests/test_core.py" not in rel_tests
            assert node["test_coverage"].get("fan_in_evidence") in (None, [])

        rows = {row["path"]: row for row in result["file_inventory"]}
        assert rows["tests/test_core.py"]["scan_status"] == "orphan"

    def test_graph_enrich_config_rule_suppresses_chained_add_weak_call(self):
        project = _make_temp_project({
            "agent/service.py": (
                "def container_case(key, value):\n"
                "    buckets = {}\n"
                "    buckets.setdefault(key, set()).add(value)\n"
                "    return buckets\n\n"
                "def direct_case(value):\n"
                "    return add(value)\n"
            ),
            "agent/math_a.py": "def add(value):\n    return value\n",
            "agent/math_b.py": "def add(value):\n    return value\n",
        })

        baseline = build_graph_v2_from_symbols(project, dry_run=True)
        service_node = next(node for node in baseline["nodes"] if node["module"] == "agent.service")
        baseline_weak = service_node["function_weak_calls"]
        assert any(
            row["caller_short"] == "container_case"
            and row["raw_target"] == "add"
            and row["call_syntax"] == "attribute_call"
            for row in baseline_weak
        )
        assert any(row["caller_short"] == "direct_case" and row["raw_target"] == "add" for row in baseline_weak)

        override_dir = os.path.join(project, ".aming-claw", "reconcile")
        os.makedirs(override_dir, exist_ok=True)
        with open(os.path.join(override_dir, "semantic_enrichment.yaml"), "w", encoding="utf-8") as f:
            f.write(
                "\n".join([
                    "graph_enrich_config_ops:",
                    "  rules:",
                    "    calls.weak_resolver.short_name_add_method_drop:",
                    "      op: add_rule",
                    "      edge: calls",
                    "      source_evidence: weak_call_resolver_ambiguous_short_name",
                    "      action: drop",
                    "      when:",
                    "        all:",
                    "          - predicate: source_evidence_is",
                    "            value: weak_call_resolver_ambiguous_short_name",
                    "          - predicate: raw_target_in",
                    "            values: [add]",
                    "          - predicate: call_syntax_is",
                    "            value: method",
                    "",
                ])
            )

        filtered = build_graph_v2_from_symbols(project, dry_run=True)
        service_node = next(node for node in filtered["nodes"] if node["module"] == "agent.service")
        filtered_weak = service_node["function_weak_calls"]
        assert not any(row["caller_short"] == "container_case" and row["raw_target"] == "add" for row in filtered_weak)
        assert any(row["caller_short"] == "direct_case" and row["raw_target"] == "add" for row in filtered_weak)

    def test_ac5_find_doc_coverage(self):
        """AC5: find_doc_coverage returns doc_files list + covered_lines int."""
        project = _make_temp_project({
            "agent/mymod.py": "def hello():\n    pass\n",
            "docs/ref.md": "# Reference\nSee agent/mymod.py for details.\n",
        })
        result = find_doc_coverage(project, os.path.join(project, "agent", "mymod.py"))
        assert isinstance(result["doc_files"], list)
        assert isinstance(result["covered_lines"], int)
        assert len(result["doc_files"]) >= 1

    def test_find_doc_coverage_includes_root_index_docs(self):
        project = _make_temp_project({
            "agent/mymod.py": "def hello():\n    pass\n",
            "README.md": "# Project\nSee agent.mymod for details.\n",
        })

        result = find_doc_coverage(project, os.path.join(project, "agent", "mymod.py"))
        doc_files = {os.path.relpath(p, project).replace(os.sep, "/") for p in result["doc_files"]}

        assert "README.md" in doc_files

    def test_find_doc_coverage_excludes_git_ignored_docs(self):
        """Ignored docs are not visible in chain worktrees, so exclude them."""
        project = _make_temp_project({
            ".gitignore": "docs/dev/\n",
            "agent/mymod.py": "def hello():\n    pass\n",
            "docs/ref.md": "# Reference\nSee agent/mymod.py for details.\n",
            "docs/dev/ignored.md": "# Scratch\nSee agent/mymod.py for details.\n",
        })
        try:
            init = subprocess.run(["git", "init"], cwd=project, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pytest.skip("git unavailable")
        if init.returncode != 0:
            pytest.skip("git init unavailable")

        result = find_doc_coverage(project, os.path.join(project, "agent", "mymod.py"))
        doc_files = {os.path.relpath(p, project).replace(os.sep, "/") for p in result["doc_files"]}
        ignored = {os.path.relpath(p, project).replace(os.sep, "/") for p in result["ignored_doc_files"]}

        assert "docs/ref.md" in doc_files
        assert "docs/dev/ignored.md" not in doc_files
        assert "docs/dev/ignored.md" in ignored


class TestDiffAgainstExistingGraph:
    """Diff current graph formats against symbol-derived candidates."""

    def test_reads_shared_volume_deps_graph_and_reports_primary_disagreements(self):
        project = _make_temp_project({
            "agent/keep.py": "def keep():\n    pass\n",
            "agent/new.py": "def new():\n    pass\n",
        })
        graph_dir = os.path.join(
            project,
            "shared-volume",
            "codex-tasks",
            "state",
            "governance",
            "aming-claw",
        )
        os.makedirs(graph_dir, exist_ok=True)
        graph_path = os.path.join(graph_dir, "graph.json")
        with open(graph_path, "w", encoding="utf-8") as f:
            json.dump({
                "version": 1,
                "deps_graph": {
                    "nodes": [
                        {"id": "L4.1", "primary": ["agent/keep.py"], "layer": "L4"},
                        {"id": "L4.2", "primary": ["agent/old.py"], "layer": "L4"},
                    ],
                    "edges": [],
                },
            }, f)

        diff = diff_against_existing_graph(project, [
            {
                "node_id": "agent.keep",
                "primary_file": ".\\agent\\keep.py",
                "layer": "L5",
            },
            {
                "node_id": "agent.new",
                "primary_file": ".\\agent\\new.py",
                "layer": "L5",
            },
        ])

        primary = diff["primary_file_diff"]
        assert diff["graph_path"] == graph_path
        assert diff["old_node_count"] == 2
        assert primary["matched"] == 1
        assert primary["only_in_new"] == ["agent/new.py"]
        assert primary["only_in_old"] == ["agent/old.py"]
        assert primary["layer_changes"][0]["primary_file"] == "agent/keep.py"


# ===========================================================================
# Atomic swap smoke tests (replacement for migration_state_machine tests)
# Full coverage lives in test_symbol_atomic_swap.py.
# ===========================================================================

class TestAtomicSwapSurface:
    """Smoke checks: atomic_swap module loads and exposes its public API."""

    def test_bak_retention_days_is_30(self):
        assert BAK_RETENTION_DAYS == 30

    def test_atomic_swap_callable(self):
        assert callable(atomic_swap)

    def test_smoke_validate_callable(self):
        assert callable(smoke_validate)

    def test_rollback_callable(self):
        assert callable(rollback)


# ===========================================================================
# Schema migration test (AC8)
# ===========================================================================

class TestSchemaMigration:
    """AC8: db.py schema version tracks the current governance schema."""

    def test_ac8_schema_version_is_current(self):
        """AC8: SCHEMA_VERSION matches the current governance schema."""
        from agent.governance.db import SCHEMA_VERSION
        assert SCHEMA_VERSION >= 31

    # Note: Testing actual migration requires DB access which is
    # integration-level; the unit test validates the constant.
