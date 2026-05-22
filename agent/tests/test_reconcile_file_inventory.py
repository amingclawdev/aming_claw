"""Tests for reconcile full-project file inventory ledger."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess

from agent.governance.db import _ensure_schema
from agent.governance.reconcile_file_inventory import (
    build_file_inventory,
    query_file_inventory,
    summarize_file_inventory,
    upsert_file_inventory,
)


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _by_path(rows):
    return {row["path"]: row for row in rows}


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_inventory_respects_gitignore_even_for_force_tracked_drafts(tmp_path):
    project = tmp_path / "project"
    _write(str(project / "agent" / "service.py"), "def run():\n    return 1\n")
    _write(str(project / "docs" / "dev" / "draft.md"), "scratch draft\n")
    _write(str(project / "docs" / "dev" / "tracked.md"), "legacy tracked scratch\n")
    _write(str(project / ".gitignore"), "docs/dev/\n")
    _git(project, "init")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "Test User")
    _git(project, "add", ".gitignore", "agent/service.py")
    _git(project, "add", "-f", "docs/dev/tracked.md")
    _git(project, "commit", "-m", "initial")

    rows = build_file_inventory(
        project_root=str(project),
        run_id="run-gitignore",
        nodes=[
            {
                "node_id": "agent.service",
                "primary_file": "agent/service.py",
            }
        ],
        feature_clusters=[],
        last_scanned_commit="commit-1",
    )
    rows_by_path = _by_path(rows)

    assert "agent/service.py" in rows_by_path
    assert "docs/dev/draft.md" not in rows_by_path
    assert "docs/dev/tracked.md" not in rows_by_path


def test_git_inventory_is_tracked_only_across_worktrees(tmp_path):
    project = tmp_path / "project"
    _write(str(project / "agent" / "service.py"), "def run():\n    return 1\n")
    _write(str(project / "README.md"), "# demo\n")
    _git(project, "init")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "Test User")
    _git(project, "add", ".")
    _git(project, "commit", "-m", "initial")

    linked = tmp_path / "linked"
    _git(project, "worktree", "add", "--detach", str(linked), "HEAD")
    _write(str(project / "agent" / "local_only.py"), "VALUE = 1\n")
    _write(str(project / ".codex" / "config.toml"), "local = true\n")
    _write(str(linked / "agent" / "other_local_only.py"), "VALUE = 2\n")

    rows_main = build_file_inventory(project_root=str(project), run_id="main")
    rows_linked = build_file_inventory(project_root=str(linked), run_id="linked")

    paths_main = {row["path"] for row in rows_main}
    paths_linked = {row["path"] for row in rows_linked}
    assert paths_main == paths_linked
    assert "agent/service.py" in paths_main
    assert "README.md" in paths_main
    assert "agent/local_only.py" not in paths_main
    assert ".codex/config.toml" not in paths_main
    assert "agent/other_local_only.py" not in paths_linked


def test_inventory_classifies_clustered_attached_and_orphan_files(tmp_path):
    project = tmp_path / "project"
    _write(str(project / "agent" / "service.py"), "def run():\n    return 1\n")
    _write(str(project / "agent" / "orphan_source.py"), "VALUE = 1\n")
    _write(str(project / "tests" / "test_service.py"), "def test_run():\n    assert True\n")
    _write(str(project / "tests" / "test_orphan.py"), "def test_orphan():\n    assert True\n")
    _write(str(project / "tests" / "conftest.py"), "import pytest\n")
    _write(str(project / "tests" / "fixtures" / "replay_data.py"), "DATA = {}\n")
    _write(str(project / "web" / "src" / "vite-env.d.ts"), "/// <reference types=\"vite/client\" />\n")
    _write(str(project / "web" / "src" / "worker.d.mts"), "export interface WorkerContract {}\n")
    _write(str(project / "web" / "src" / "legacy.d.cts"), "export interface LegacyContract {}\n")
    _write(str(project / "docs" / "service.md"), "See agent/service.py\n")
    _write(str(project / "docs" / "orphan.md"), "Unattached note\n")
    _write(str(project / "docs" / "dev" / "handoff-2026-04-24-post-audit.md"), "Session handoff\n")
    _write(str(project / "MEMORY.md"), "Operator memory\n")
    _write(str(project / "docs" / "dev" / "scratch" / "backlog.json"), "{}\n")
    _write(str(project / "pyproject.toml"), "[project]\nname='x'\n")
    _write(str(project / "requirements.txt"), "requests\n")
    _write(str(project / "aming_claw_governance.egg-info" / "SOURCES.txt"), "generated\n")
    _write(str(project / ".coverage"), "generated\n")
    _write(str(project / "Dockerfile.governance"), "FROM python:3.12\n")
    _write(str(project / "node_modules" / "pkg" / "index.js"), "ignored();\n")
    _write(str(project / "search-workspace" / "long_task_test.txt"), "scratch\n")
    _write(str(project / ".observer-cache" / "scratch.json"), "{}\n")

    rows = build_file_inventory(
        project_root=str(project),
        run_id="run-1",
        nodes=[
            {
                "node_id": "agent.service",
                "primary_file": str(project / "agent" / "service.py"),
                "secondary": ["docs/service.md"],
                "test": ["tests/test_service.py"],
            }
        ],
        feature_clusters=[
            {
                "cluster_fingerprint": "cluster-1",
                "primary_files": ["agent/service.py"],
                "secondary_files": ["tests/test_service.py", "docs/service.md"],
            }
        ],
        last_scanned_commit="commit-1",
    )
    rows_by_path = _by_path(rows)

    assert "node_modules/pkg/index.js" not in rows_by_path
    assert "search-workspace/long_task_test.txt" not in rows_by_path
    assert ".observer-cache/scratch.json" not in rows_by_path
    assert rows_by_path["agent/service.py"]["scan_status"] == "clustered"
    assert rows_by_path["agent/service.py"]["candidate_node_id"] == "agent.service"
    assert rows_by_path["agent/service.py"]["graph_status"] == "mapped"
    assert rows_by_path["agent/service.py"]["mapped_node_ids"] == ["agent.service"]
    assert rows_by_path["tests/test_service.py"]["scan_status"] == "secondary_attached"
    assert rows_by_path["tests/test_service.py"]["graph_status"] == "attached"
    assert rows_by_path["tests/test_service.py"]["mapped_node_ids"] == ["agent.service"]
    assert rows_by_path["docs/service.md"]["scan_status"] == "secondary_attached"
    assert rows_by_path["agent/orphan_source.py"]["scan_status"] == "orphan"
    assert rows_by_path["agent/orphan_source.py"]["graph_status"] == "unmapped"
    assert rows_by_path["tests/test_orphan.py"]["scan_status"] == "orphan"
    assert rows_by_path["tests/conftest.py"]["scan_status"] == "support"
    assert rows_by_path["tests/fixtures/replay_data.py"]["scan_status"] == "support"
    assert rows_by_path["web/src/vite-env.d.ts"]["file_kind"] == "type_contract"
    assert rows_by_path["web/src/vite-env.d.ts"]["scan_status"] == "support"
    assert rows_by_path["web/src/vite-env.d.ts"]["graph_status"] == "support"
    assert rows_by_path["web/src/vite-env.d.ts"]["language"] == "typescript"
    assert rows_by_path["web/src/worker.d.mts"]["file_kind"] == "type_contract"
    assert rows_by_path["web/src/worker.d.mts"]["scan_status"] == "support"
    assert rows_by_path["web/src/legacy.d.cts"]["file_kind"] == "type_contract"
    assert rows_by_path["web/src/legacy.d.cts"]["scan_status"] == "support"
    assert rows_by_path["docs/orphan.md"]["scan_status"] == "orphan"
    assert rows_by_path["docs/dev/handoff-2026-04-24-post-audit.md"]["scan_status"] == "archive"
    assert rows_by_path["MEMORY.md"]["scan_status"] == "archive"
    assert rows_by_path["pyproject.toml"]["file_kind"] == "config"
    assert rows_by_path["pyproject.toml"]["scan_status"] == "pending_decision"
    assert rows_by_path["requirements.txt"]["file_kind"] == "config"
    assert rows_by_path["requirements.txt"]["scan_status"] == "pending_decision"
    assert rows_by_path["Dockerfile.governance"]["file_kind"] == "config"
    assert rows_by_path[".coverage"]["file_kind"] == "generated"
    assert rows_by_path[".coverage"]["scan_status"] == "ignored"
    assert rows_by_path["aming_claw_governance.egg-info/SOURCES.txt"]["file_kind"] == "generated"
    assert rows_by_path["aming_claw_governance.egg-info/SOURCES.txt"]["scan_status"] == "ignored"
    assert rows_by_path["docs/dev/scratch/backlog.json"]["file_kind"] == "generated"
    assert rows_by_path["docs/dev/scratch/backlog.json"]["scan_status"] == "ignored"
    assert rows_by_path["agent/service.py"]["sha256"]
    assert rows_by_path["agent/service.py"]["file_hash"] == f"sha256:{rows_by_path['agent/service.py']['sha256']}"
    assert rows_by_path["agent/service.py"]["size_bytes"] > 0
    assert rows_by_path["agent/service.py"]["last_scanned_commit"] == "commit-1"

    summary = summarize_file_inventory(rows)
    assert summary["by_status"]["clustered"] == 1
    assert summary["by_status"]["secondary_attached"] == 2
    assert summary["by_status"]["orphan"] == 3
    assert summary["by_status"]["support"] == 5
    assert summary["by_status"]["archive"] == 2
    assert summary["by_status"]["pending_decision"] == 3
    assert summary["by_status"]["ignored"] == 3


def test_inventory_maps_candidate_fallback_source_nodes(tmp_path):
    project = tmp_path / "project"
    _write(str(project / "agent" / "service.py"), "def run():\n    return 1\n")
    _write(str(project / "agent" / "fallback.py"), "VALUE = 1\n")

    rows = build_file_inventory(
        project_root=str(project),
        run_id="run-fallback",
        nodes=[
            {
                "node_id": "agent.fallback",
                "primary": ["agent/fallback.py"],
            }
        ],
        feature_clusters=[],
    )
    row = _by_path(rows)["agent/fallback.py"]

    assert row["scan_status"] == "clustered"
    assert row["graph_status"] == "mapped"
    assert row["candidate_node_id"] == "agent.fallback"
    assert row["mapped_node_ids"] == ["agent.fallback"]
    assert row["decision"] == "govern"


def test_inventory_uses_shared_language_policy_for_mixed_js_ts_fixture(tmp_path):
    project = tmp_path / "project"
    _write(str(project / "agent" / "service.py"), "def run():\n    return 1\n")
    _write(str(project / "web" / "src" / "index.js"), "export function main() {}\n")
    _write(str(project / "web" / "src" / "App.tsx"), "export function App() { return null }\n")
    _write(str(project / "web" / "src" / "vite-env.d.ts"), "/// <reference types=\"vite/client\" />\n")
    _write(str(project / "web" / "src" / "App.test.tsx"), "test('app', () => {})\n")
    _write(str(project / "web" / "src" / "config.json"), "{}\n")
    _write(str(project / "web" / "vite.config.ts"), "export default {}\n")
    _write(str(project / "web" / "eslint.config.js"), "export default []\n")
    _write(str(project / "web" / "package-lock.json"), "{}\n")
    _write(str(project / "docs" / "README.md"), "# docs\n")
    _write(str(project / "web" / "node_modules" / "pkg" / "index.js"), "ignored();\n")
    _write(str(project / "web" / "dist" / "bundle.js"), "ignored();\n")
    _write(str(project / "coverage" / "lcov.info"), "ignored\n")

    rows = build_file_inventory(
        project_root=str(project),
        run_id="run-mixed-policy",
        nodes=[],
        feature_clusters=[],
    )
    rows_by_path = _by_path(rows)

    assert rows_by_path["web/src/index.js"]["file_kind"] == "source"
    assert rows_by_path["web/src/index.js"]["language"] == "javascript"
    assert rows_by_path["web/src/App.tsx"]["file_kind"] == "source"
    assert rows_by_path["web/src/App.tsx"]["language"] == "typescript"
    assert rows_by_path["web/src/vite-env.d.ts"]["file_kind"] == "type_contract"
    assert rows_by_path["web/src/vite-env.d.ts"]["scan_status"] == "support"
    assert rows_by_path["web/src/vite-env.d.ts"]["language"] == "typescript"
    assert rows_by_path["web/src/App.test.tsx"]["file_kind"] == "test"
    assert rows_by_path["web/src/App.test.tsx"]["language"] == "typescript"
    assert rows_by_path["web/src/config.json"]["file_kind"] == "config"
    assert rows_by_path["web/src/config.json"]["language"] == "json"
    assert rows_by_path["web/vite.config.ts"]["file_kind"] == "config"
    assert rows_by_path["web/eslint.config.js"]["file_kind"] == "config"
    assert rows_by_path["web/package-lock.json"]["file_kind"] == "generated"
    assert rows_by_path["web/package-lock.json"]["scan_status"] == "ignored"
    assert rows_by_path["docs/README.md"]["file_kind"] == "index_doc"
    assert "web/node_modules/pkg/index.js" not in rows_by_path
    assert "web/dist/bundle.js" not in rows_by_path
    assert "coverage/lcov.info" not in rows_by_path


def test_inventory_persists_to_governance_table(tmp_path):
    project = tmp_path / "project"
    _write(str(project / "agent" / "service.py"), "def run():\n    return 1\n")
    rows = build_file_inventory(
        project_root=str(project),
        run_id="run-db",
        nodes=[],
        feature_clusters=[],
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    count = upsert_file_inventory(conn, "aming-claw-test", rows)
    conn.commit()

    assert count == len(rows)
    persisted = conn.execute(
        """
        SELECT project_id, run_id, path, file_kind, scan_status,
               file_hash, size_bytes, graph_status, mapped_node_ids
        FROM reconcile_file_inventory
        WHERE project_id = ? AND run_id = ?
        """,
        ("aming-claw-test", "run-db"),
    ).fetchall()
    assert len(persisted) == len(rows)
    persisted_row = dict(persisted[0])
    assert persisted_row["path"] == "agent/service.py"
    assert persisted_row["file_hash"].startswith("sha256:")
    assert persisted_row["size_bytes"] > 0
    assert persisted_row["graph_status"] == "unmapped"
    assert json.loads(persisted_row["mapped_node_ids"]) == []


def test_inventory_upsert_replaces_stale_rows_for_same_run(tmp_path):
    project = tmp_path / "project"
    _write(str(project / "agent" / "service.py"), "def run():\n    return 1\n")
    _write(str(project / "search-workspace" / "stale.txt"), "scratch\n")
    old_rows = [
        {
            "run_id": "run-prune",
            "path": "agent/service.py",
            "file_kind": "source",
            "language": "python",
            "sha256": "old",
            "scan_status": "clustered",
            "updated_at": "2026-05-06T00:00:00Z",
        },
        {
            "run_id": "run-prune",
            "path": "search-workspace/stale.txt",
            "file_kind": "doc",
            "language": "text",
            "sha256": "stale",
            "scan_status": "orphan",
            "updated_at": "2026-05-06T00:00:00Z",
        },
    ]
    new_rows = build_file_inventory(
        project_root=str(project),
        run_id="run-prune",
        nodes=[],
        feature_clusters=[],
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    upsert_file_inventory(conn, "aming-claw-test", old_rows)
    upsert_file_inventory(conn, "aming-claw-test", new_rows)
    conn.commit()

    paths = {
        row["path"]
        for row in conn.execute(
            """
            SELECT path FROM reconcile_file_inventory
            WHERE project_id = ? AND run_id = ?
            """,
            ("aming-claw-test", "run-prune"),
        ).fetchall()
    }
    assert "agent/service.py" in paths
    assert "search-workspace/stale.txt" not in paths


def test_inventory_query_returns_latest_summary_and_filters(tmp_path):
    project = tmp_path / "project"
    _write(str(project / "agent" / "service.py"), "def run():\n    return 1\n")
    _write(str(project / "docs" / "readme.md"), "# doc\n")
    rows = build_file_inventory(
        project_root=str(project),
        run_id="run-query",
        nodes=[],
        feature_clusters=[],
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    upsert_file_inventory(conn, "aming-claw-test", rows)
    conn.commit()

    result = query_file_inventory(
        conn,
        "aming-claw-test",
        scan_status="orphan",
        limit=10,
    )

    assert result["run_id"] == "run-query"
    assert result["summary"]["total"] == len(rows)
    assert result["rows"]
    assert {row["scan_status"] for row in result["rows"]} == {"orphan"}
    assert all(isinstance(row["mapped_node_ids"], list) for row in result["rows"])
    assert all(row["file_hash"].startswith("sha256:") for row in result["rows"])


def test_phase_z_artifact_contains_file_inventory(tmp_path):
    from agent.governance.reconcile_phases.phase_z_v2 import build_graph_v2_from_symbols

    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _write(str(project / "agent" / "service.py"), "def run():\n    return 1\n")
    _write(str(project / "tests" / "test_service.py"), "def test_run():\n    assert True\n")
    _write(str(project / "docs" / "service.md"), "See agent/service.py\n")

    result = build_graph_v2_from_symbols(
        str(project),
        dry_run=True,
        scratch_dir=str(scratch),
        run_id="phase-z-explicit-run",
    )

    assert result["run_id"] == "phase-z-explicit-run"
    assert result["file_inventory"]
    assert {row["run_id"] for row in result["file_inventory"]} == {
        "phase-z-explicit-run"
    }
    assert result["file_inventory_summary"]["total"] >= 3
    with open(result["report_path"], "r", encoding="utf-8") as f:
        payload = json.load(f)
    assert payload["file_inventory"]
    assert {row["run_id"] for row in payload["file_inventory"]} == {
        "phase-z-explicit-run"
    }
    assert payload["file_inventory_summary"]["total"] >= 3
    service_row = next(row for row in payload["file_inventory"] if row["path"] == "agent/service.py")
    assert service_row["file_hash"].startswith("sha256:")
    assert service_row["size_bytes"] > 0
    assert "confidence" not in json.dumps(service_row)


def test_phase_z_artifact_respects_runtime_extra_excludes(tmp_path):
    from agent.governance.reconcile_phases.phase_z_v2 import build_graph_v2_from_symbols

    project = tmp_path / "project"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _write(str(project / "agent" / "service.py"), "def run():\n    return 1\n")
    _write(str(project / "node" / "tool.py"), "def local_tool():\n    return 2\n")

    result = build_graph_v2_from_symbols(
        str(project),
        dry_run=True,
        scratch_dir=str(scratch),
        run_id="phase-z-runtime-excludes",
        extra_exclude_roots=["node"],
    )

    inventory_paths = {row["path"] for row in result["file_inventory"]}
    graph_paths = json.dumps(result)
    assert "agent/service.py" in inventory_paths
    assert "node/tool.py" not in inventory_paths
    assert "node/tool.py" not in graph_paths
