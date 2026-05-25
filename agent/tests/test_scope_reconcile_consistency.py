from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent.governance import graph_snapshot_store as store
from agent.governance.db import _ensure_schema
from agent.governance.state_reconcile import (
    _read_snapshot_graph,
    _snapshot_inventory_rows,
    normalize_reconcile_snapshot_for_comparison,
    run_pending_scope_reconcile_candidate,
    run_state_only_full_reconcile,
)


PID = "scope-consistency-test"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    yield c
    c.close()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return (result.stdout or "").strip()


def _init_git(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")


def _write_project(root: Path) -> None:
    _write(
        root / "agent" / "service.py",
        "def service_entry():\n"
        "    return helper()\n\n"
        "def helper():\n"
        "    return 'ok'\n",
    )
    _write(
        root / "agent" / "tests" / "test_service.py",
        "from agent.service import service_entry\n\n"
        "def test_service_entry():\n"
        "    assert service_entry() == 'ok'\n",
    )
    _write(root / "README.md", "# Service\n\nScope consistency fixture.\n")


def _write_call_free_project(root: Path, *, result: str = "ok") -> None:
    _write(
        root / "agent" / "service.py",
        "def service_entry():\n"
        f"    return {result!r}\n",
    )
    _write(
        root / "agent" / "tests" / "test_service.py",
        "from agent.service import service_entry\n\n"
        "def test_service_entry():\n"
        f"    assert service_entry() == {result!r}\n",
    )
    _write(root / "README.md", "# Service\n\nCall-free source consistency fixture.\n")


def _write_dependency_project(root: Path, *, helper_module: str = "helper_a") -> None:
    _write(
        root / "agent" / "service.py",
        f"from agent.{helper_module} import run_helper\n\n"
        "def service_entry():\n"
        "    return run_helper()\n",
    )
    _write(
        root / "agent" / "helper_a.py",
        "def run_helper():\n"
        "    return 'a'\n",
    )
    _write(
        root / "agent" / "helper_b.py",
        "def run_helper():\n"
        "    return 'b'\n",
    )
    _write(
        root / "agent" / "tests" / "test_service.py",
        "from agent.service import service_entry\n\n"
        "def test_service_entry():\n"
        "    assert service_entry() in {'a', 'b'}\n",
    )
    _write(root / "README.md", "# Service\n\nSource dependency consistency fixture.\n")


def _normalized_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> dict:
    graph = _read_snapshot_graph(PID, snapshot_id)
    inventory = _snapshot_inventory_rows(conn, PID, snapshot_id)
    return normalize_reconcile_snapshot_for_comparison(graph, file_inventory=inventory)


def _node_ids_by_primary_file(graph: dict) -> dict[str, str]:
    nodes = ((graph.get("deps_graph") or {}).get("nodes") or [])
    mapping: dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or node.get("node_id") or "")
        primary_files = list(node.get("primary") or []) + list(node.get("primary_files") or [])
        for path in primary_files:
            if node_id and path:
                mapping[str(path).replace("\\", "/").strip("/")] = node_id
    return mapping


def _nodes_by_module(graph: dict) -> dict[str, dict]:
    nodes = ((graph.get("deps_graph") or {}).get("nodes") or [])
    out: dict[str, dict] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        module = str(metadata.get("module") or "")
        if module:
            out[module] = node
    return out


def _rewrite_inventory_status(snapshot_id: str, path: str, *, scan_status: str, graph_status: str) -> None:
    inventory_path = store.snapshot_companion_dir(PID, snapshot_id) / "file_inventory.json"
    rows = json.loads(inventory_path.read_text(encoding="utf-8"))
    for row in rows:
        if isinstance(row, dict) and row.get("path") == path:
            row["scan_status"] = scan_status
            row["graph_status"] = graph_status
            break
    else:
        raise AssertionError(f"inventory path not found: {path}")
    inventory_path.write_text(
        json.dumps(rows, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def test_scope_reconcile_output_matches_full_rebuild_for_same_final_state(conn, tmp_path):
    project = tmp_path / "project"
    _write_project(project)
    _init_git(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "base")
    base_commit = _git(project, "rev-parse", "HEAD")

    base = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-base-consistency",
        commit_sha=base_commit,
        snapshot_id="full-base-consistency",
        created_by="test",
        activate=True,
        semantic_enrich=False,
    )
    assert base["ok"] is True

    _write(
        project / "README.md",
        "# Service\n\nScope consistency fixture with a documentation update.\n",
    )
    _git(project, "add", "README.md")
    _git(project, "commit", "-m", "change docs")
    head_commit = _git(project, "rev-parse", "HEAD")
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha=head_commit,
        parent_commit_sha=base_commit,
        evidence={"source": "test"},
    )

    scope = run_pending_scope_reconcile_candidate(
        conn,
        PID,
        project,
        target_commit_sha=head_commit,
        run_id="scope-head-consistency",
        snapshot_id="scope-head-consistency",
        created_by="test",
        semantic_enrich=False,
    )
    assert scope["ok"] is True
    assert scope["scope_file_delta"]["strategy"] == "incremental_graph_delta"
    assert scope["scope_file_delta"]["graph_delta_mode"] == "metadata_only"
    assert scope["scope_file_delta"]["changed_files"] == ["README.md"]
    assert scope["scope_graph_delta"]["strategy"] == "incremental_graph_delta"
    assert scope["scope_graph_delta"]["mode"] == "metadata_only"
    assert scope["scope_graph_delta"]["added_nodes"] == []
    assert scope["scope_graph_delta"]["removed_nodes"] == []
    assert scope["scope_graph_delta"]["added_edges"] == []
    assert scope["scope_graph_delta"]["removed_edges"] == []

    full = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-head-consistency",
        commit_sha=head_commit,
        snapshot_id="full-head-consistency",
        created_by="test",
        semantic_enrich=False,
    )
    assert full["ok"] is True

    assert _normalized_snapshot(conn, "scope-head-consistency") == _normalized_snapshot(
        conn,
        "full-head-consistency",
    )

    base_node_ids = _node_ids_by_primary_file(_read_snapshot_graph(PID, "full-base-consistency"))
    scope_node_ids = _node_ids_by_primary_file(_read_snapshot_graph(PID, "scope-head-consistency"))
    assert scope_node_ids["agent/service.py"] == base_node_ids["agent/service.py"]


def test_scope_reconcile_doc_hint_binding_falls_back_with_named_metrics(conn, tmp_path):
    project = tmp_path / "project"
    _write_project(project)
    _write(project / "docs" / "orphan.md", "# Loose Notes\n\nNo binding yet.\n")
    _init_git(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "base")
    base_commit = _git(project, "rev-parse", "HEAD")

    base = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-doc-hint-base-consistency",
        commit_sha=base_commit,
        snapshot_id="full-doc-hint-base-consistency",
        created_by="test",
        activate=True,
        semantic_enrich=False,
    )
    assert base["ok"] is True

    _write(
        project / "docs" / "orphan.md",
        "<!-- governance-hint "
        '{"asset_binding_event":{"operation":"bind","path":"docs/orphan.md",'
        '"role":"doc","target_module":"agent.service"}}'
        " -->\n# Loose Notes\n\nNow bound through source-controlled evidence.\n",
    )
    _git(project, "add", "docs/orphan.md")
    _git(project, "commit", "-m", "bind orphan doc through hint")
    head_commit = _git(project, "rev-parse", "HEAD")
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha=head_commit,
        parent_commit_sha=base_commit,
        evidence={"source": "test"},
    )

    scope = run_pending_scope_reconcile_candidate(
        conn,
        PID,
        project,
        target_commit_sha=head_commit,
        run_id="scope-doc-hint-head-consistency",
        snapshot_id="scope-doc-hint-head-consistency",
        created_by="test",
        semantic_enrich=False,
    )

    assert scope["ok"] is True
    assert scope["scope_file_delta"]["strategy"] == "full_rebuild_fallback"
    assert scope["scope_file_delta"]["graph_delta_mode"] == "full_rebuild"
    assert scope["scope_file_delta"]["fallback_reason"] == "inventory_status_change_requires_full_rebuild"
    assert scope["scope_graph_delta"]["strategy"] == "full_rebuild_fallback"
    assert scope["scope_graph_delta"]["mode"] == "full_rebuild"
    assert scope["scope_graph_delta"]["fallback_reason"] == "inventory_status_change_requires_full_rebuild"
    assert scope["scope_graph_events"]["by_type"]["doc_binding_added"] == 1
    metric = conn.execute(
        """
        SELECT strategy, graph_delta_mode, fallback_reason,
               changed_file_count, impacted_file_count
        FROM reconcile_run_metrics
        WHERE project_id=? AND run_id=? AND snapshot_id=?
        """,
        (PID, "scope-doc-hint-head-consistency", "scope-doc-hint-head-consistency"),
    ).fetchone()
    assert metric["strategy"] == "full_rebuild_fallback"
    assert metric["graph_delta_mode"] == "full_rebuild"
    assert metric["fallback_reason"] == "inventory_status_change_requires_full_rebuild"
    assert metric["changed_file_count"] == 1
    assert metric["impacted_file_count"] >= 1

    full = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-doc-hint-head-consistency",
        commit_sha=head_commit,
        snapshot_id="full-doc-hint-head-consistency",
        created_by="test",
        semantic_enrich=False,
    )
    assert full["ok"] is True
    assert _normalized_snapshot(conn, "scope-doc-hint-head-consistency") == _normalized_snapshot(
        conn,
        "full-doc-hint-head-consistency",
    )

    node = _nodes_by_module(_read_snapshot_graph(PID, "scope-doc-hint-head-consistency"))["agent.service"]
    assert "docs/orphan.md" in node["secondary"]
    inventory = {
        row["path"]: row
        for row in _snapshot_inventory_rows(conn, PID, "scope-doc-hint-head-consistency")
    }
    assert inventory["docs/orphan.md"]["scan_status"] == "secondary_attached"
    assert inventory["docs/orphan.md"]["effective_binding_status"] == "accepted"


def test_scope_reconcile_source_hash_only_matches_full_rebuild_for_same_final_state(conn, tmp_path):
    project = tmp_path / "project"
    _write_call_free_project(project)
    _init_git(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "base")
    base_commit = _git(project, "rev-parse", "HEAD")

    base = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-source-base-consistency",
        commit_sha=base_commit,
        snapshot_id="full-source-base-consistency",
        created_by="test",
        activate=True,
        semantic_enrich=False,
    )
    assert base["ok"] is True

    _write(
        project / "agent" / "service.py",
        "def service_entry():\n"
        "    return 'changed'\n",
    )
    _git(project, "add", "agent/service.py")
    _git(project, "commit", "-m", "change source body")
    head_commit = _git(project, "rev-parse", "HEAD")
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha=head_commit,
        parent_commit_sha=base_commit,
        evidence={"source": "test"},
    )

    scope = run_pending_scope_reconcile_candidate(
        conn,
        PID,
        project,
        target_commit_sha=head_commit,
        run_id="scope-source-head-consistency",
        snapshot_id="scope-source-head-consistency",
        created_by="test",
        semantic_enrich=False,
    )
    assert scope["ok"] is True
    assert scope["scope_file_delta"]["strategy"] == "incremental_graph_delta"
    assert scope["scope_file_delta"]["graph_delta_mode"] == "source_hash_only"
    assert scope["scope_file_delta"]["changed_files"] == ["agent/service.py"]
    assert scope["scope_graph_delta"]["mode"] == "source_hash_only"
    assert scope["scope_graph_delta"]["added_nodes"] == []
    assert scope["scope_graph_delta"]["removed_nodes"] == []
    assert scope["scope_graph_delta"]["added_edges"] == []
    assert scope["scope_graph_delta"]["removed_edges"] == []

    full = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-source-head-consistency",
        commit_sha=head_commit,
        snapshot_id="full-source-head-consistency",
        created_by="test",
        semantic_enrich=False,
    )
    assert full["ok"] is True

    assert _normalized_snapshot(conn, "scope-source-head-consistency") == _normalized_snapshot(
        conn,
        "full-source-head-consistency",
    )

    base_node_ids = _node_ids_by_primary_file(_read_snapshot_graph(PID, "full-source-base-consistency"))
    scope_node_ids = _node_ids_by_primary_file(_read_snapshot_graph(PID, "scope-source-head-consistency"))
    assert scope_node_ids["agent/service.py"] == base_node_ids["agent/service.py"]


def test_scope_reconcile_source_dependency_delta_matches_full_rebuild(conn, tmp_path):
    project = tmp_path / "project"
    _write_dependency_project(project, helper_module="helper_a")
    _init_git(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "base")
    base_commit = _git(project, "rev-parse", "HEAD")

    base = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-source-dep-base-consistency",
        commit_sha=base_commit,
        snapshot_id="full-source-dep-base-consistency",
        created_by="test",
        activate=True,
        semantic_enrich=False,
    )
    assert base["ok"] is True

    _write_dependency_project(project, helper_module="helper_b")
    _git(project, "add", "agent/service.py")
    _git(project, "commit", "-m", "retarget service dependency")
    head_commit = _git(project, "rev-parse", "HEAD")
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha=head_commit,
        parent_commit_sha=base_commit,
        evidence={"source": "test"},
    )

    scope = run_pending_scope_reconcile_candidate(
        conn,
        PID,
        project,
        target_commit_sha=head_commit,
        run_id="scope-source-dep-head-consistency",
        snapshot_id="scope-source-dep-head-consistency",
        created_by="test",
        semantic_enrich=False,
    )
    assert scope["ok"] is True
    assert scope["scope_file_delta"]["strategy"] == "incremental_graph_delta"
    assert scope["scope_file_delta"]["graph_delta_mode"] == "source_dependency_delta"
    assert scope["scope_graph_delta"]["mode"] == "source_dependency_delta"
    assert scope["scope_graph_delta"]["added_nodes"] == []
    assert scope["scope_graph_delta"]["removed_nodes"] == []
    assert scope["scope_graph_delta"]["added_edges"]
    assert scope["scope_graph_delta"]["removed_edges"]

    full = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-source-dep-head-consistency",
        commit_sha=head_commit,
        snapshot_id="full-source-dep-head-consistency",
        created_by="test",
        semantic_enrich=False,
    )
    assert full["ok"] is True
    assert _normalized_snapshot(conn, "scope-source-dep-head-consistency") == _normalized_snapshot(
        conn,
        "full-source-dep-head-consistency",
    )

    base_node_ids = _node_ids_by_primary_file(_read_snapshot_graph(PID, "full-source-dep-base-consistency"))
    scope_node_ids = _node_ids_by_primary_file(_read_snapshot_graph(PID, "scope-source-dep-head-consistency"))
    assert scope_node_ids["agent/service.py"] == base_node_ids["agent/service.py"]
    assert scope_node_ids["agent/helper_a.py"] == base_node_ids["agent/helper_a.py"]
    assert scope_node_ids["agent/helper_b.py"] == base_node_ids["agent/helper_b.py"]


def test_scope_reconcile_test_fanin_incremental_matches_full_rebuild(conn, tmp_path):
    project = tmp_path / "project"
    _write_project(project)
    _write(
        project / "agent" / "other.py",
        "def other_entry():\n"
        "    return 'ok'\n",
    )
    _write(
        project / "agent" / "tests" / "test_integration.py",
        "from agent.service import service_entry\n\n"
        "def test_integration():\n"
        "    assert service_entry() == 'ok'\n",
    )
    _init_git(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "base")
    base_commit = _git(project, "rev-parse", "HEAD")

    base = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-test-fanin-base-consistency",
        commit_sha=base_commit,
        snapshot_id="full-test-fanin-base-consistency",
        created_by="test",
        activate=True,
        semantic_enrich=False,
    )
    assert base["ok"] is True

    _write(
        project / "agent" / "tests" / "test_integration.py",
        "from agent.other import other_entry\n\n"
        "def test_integration():\n"
        "    assert other_entry() == 'ok'\n",
    )
    _git(project, "add", "agent/tests/test_integration.py")
    _git(project, "commit", "-m", "move integration test fanin")
    head_commit = _git(project, "rev-parse", "HEAD")
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha=head_commit,
        parent_commit_sha=base_commit,
        evidence={"source": "test"},
    )

    scope = run_pending_scope_reconcile_candidate(
        conn,
        PID,
        project,
        target_commit_sha=head_commit,
        run_id="scope-test-fanin-head-consistency",
        snapshot_id="scope-test-fanin-head-consistency",
        created_by="test",
        semantic_enrich=False,
    )
    assert scope["ok"] is True
    assert scope["scope_file_delta"]["strategy"] == "incremental_graph_delta"
    assert scope["scope_file_delta"]["graph_delta_mode"] == "test_fanin_hash_only"
    assert scope["scope_file_delta"]["changed_files"] == ["agent/tests/test_integration.py"]
    assert scope["scope_graph_delta"]["mode"] == "test_fanin_hash_only"
    assert scope["scope_graph_delta"]["added_nodes"] == []
    assert scope["scope_graph_delta"]["removed_nodes"] == []
    assert scope["scope_graph_delta"]["added_edges"] == []
    assert scope["scope_graph_delta"]["removed_edges"] == []

    full = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-test-fanin-head-consistency",
        commit_sha=head_commit,
        snapshot_id="full-test-fanin-head-consistency",
        created_by="test",
        semantic_enrich=False,
    )
    assert full["ok"] is True

    assert _normalized_snapshot(conn, "scope-test-fanin-head-consistency") == _normalized_snapshot(
        conn,
        "full-test-fanin-head-consistency",
    )

    scope_nodes = _nodes_by_module(_read_snapshot_graph(PID, "scope-test-fanin-head-consistency"))
    service = scope_nodes["agent.service"]
    other = scope_nodes["agent.other"]
    assert "agent/tests/test_integration.py" not in service["test"]
    assert "agent/tests/test_integration.py" in other["test"]
    service_fanin = (service.get("metadata") or {}).get("test_consumer_fanin") or []
    other_fanin = (other.get("metadata") or {}).get("test_consumer_fanin") or []
    assert {entry["path"] for entry in service_fanin} == {"agent/tests/test_service.py"}
    assert {entry["path"] for entry in other_fanin} == {"agent/tests/test_integration.py"}
    assert service_fanin[0]["evidence"] == "test_import_fanin"
    assert other_fanin[0]["evidence"] == "test_import_fanin"
    assert "agent.service.service_entry" in service_fanin[0]["imports"]
    assert "agent.other.other_entry" in other_fanin[0]["imports"]


def test_scope_reconcile_test_fanin_ignores_unrelated_inventory_status_churn(conn, tmp_path):
    project = tmp_path / "project"
    _write_project(project)
    _write(
        project / "agent" / "other.py",
        "def other_entry():\n"
        "    return 'ok'\n",
    )
    _write(
        project / "agent" / "tests" / "test_integration.py",
        "from agent.service import service_entry\n\n"
        "def test_integration():\n"
        "    assert service_entry() == 'ok'\n",
    )
    _init_git(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "base")
    base_commit = _git(project, "rev-parse", "HEAD")

    base = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-test-fanin-base-churn",
        commit_sha=base_commit,
        snapshot_id="full-test-fanin-base-churn",
        created_by="test",
        activate=True,
        semantic_enrich=False,
    )
    assert base["ok"] is True
    _rewrite_inventory_status(
        "full-test-fanin-base-churn",
        "README.md",
        scan_status="stale_fixture_status",
        graph_status="stale_fixture_graph_status",
    )

    _write(
        project / "agent" / "tests" / "test_integration.py",
        "from agent.other import other_entry\n\n"
        "def test_integration():\n"
        "    assert other_entry() == 'ok'\n",
    )
    _git(project, "add", "agent/tests/test_integration.py")
    _git(project, "commit", "-m", "move integration test fanin")
    head_commit = _git(project, "rev-parse", "HEAD")
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha=head_commit,
        parent_commit_sha=base_commit,
        evidence={"source": "test"},
    )

    scope = run_pending_scope_reconcile_candidate(
        conn,
        PID,
        project,
        target_commit_sha=head_commit,
        run_id="scope-test-fanin-head-churn",
        snapshot_id="scope-test-fanin-head-churn",
        created_by="test",
        semantic_enrich=False,
    )
    assert scope["ok"] is True
    assert scope["scope_file_delta"]["strategy"] == "incremental_graph_delta"
    assert scope["scope_file_delta"]["graph_delta_mode"] == "test_fanin_hash_only"
    assert scope["scope_file_delta"]["changed_files"] == ["agent/tests/test_integration.py"]
    assert scope["scope_file_delta"]["status_changed_files"] == []
    assert scope["scope_file_delta"]["ignored_status_changed_files"] == ["README.md"]
    assert scope["scope_graph_delta"]["mode"] == "test_fanin_hash_only"

    full = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id="full-test-fanin-head-churn",
        commit_sha=head_commit,
        snapshot_id="full-test-fanin-head-churn",
        created_by="test",
        semantic_enrich=False,
    )
    assert full["ok"] is True

    assert _normalized_snapshot(conn, "scope-test-fanin-head-churn") == _normalized_snapshot(
        conn,
        "full-test-fanin-head-churn",
    )


@pytest.mark.parametrize("operation", ["add", "remove", "rename", "rename_retarget"])
def test_scope_reconcile_test_fanin_file_set_incremental_matches_full_rebuild(conn, tmp_path, operation):
    project = tmp_path / "project"
    _write_project(project)
    if operation == "rename_retarget":
        _write(
            project / "agent" / "other.py",
            "def other_entry():\n"
            "    return 'ok'\n",
        )
    if operation in {"remove", "rename", "rename_retarget"}:
        _write(
            project / "agent" / "tests" / "test_integration.py",
            "from agent.service import service_entry\n\n"
            "def test_integration():\n"
            "    assert service_entry() == 'ok'\n",
        )
    _init_git(project)
    _git(project, "add", ".")
    _git(project, "commit", "-m", "base")
    base_commit = _git(project, "rev-parse", "HEAD")

    base = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id=f"full-test-fanin-{operation}-base-consistency",
        commit_sha=base_commit,
        snapshot_id=f"full-test-fanin-{operation}-base-consistency",
        created_by="test",
        activate=True,
        semantic_enrich=False,
    )
    assert base["ok"] is True

    if operation == "add":
        _write(
            project / "agent" / "tests" / "test_integration.py",
            "from agent.service import service_entry\n\n"
            "def test_integration():\n"
            "    assert service_entry() == 'ok'\n",
        )
        _git(project, "add", "agent/tests/test_integration.py")
        _git(project, "commit", "-m", "add integration test fanin")
    else:
        if operation == "remove":
            _git(project, "rm", "agent/tests/test_integration.py")
            _git(project, "commit", "-m", "remove integration test fanin")
        else:
            _git(project, "mv", "agent/tests/test_integration.py", "agent/tests/test_integration_renamed.py")
            if operation == "rename_retarget":
                _write(
                    project / "agent" / "tests" / "test_integration_renamed.py",
                    "from agent.other import other_entry\n\n"
                    "def test_integration():\n"
                    "    assert other_entry() == 'ok'\n",
                )
                _git(project, "add", "agent/tests/test_integration_renamed.py")
            _git(project, "commit", "-m", "rename integration test fanin")
    head_commit = _git(project, "rev-parse", "HEAD")
    store.queue_pending_scope_reconcile(
        conn,
        PID,
        commit_sha=head_commit,
        parent_commit_sha=base_commit,
        evidence={"source": "test"},
    )

    scope_snapshot_id = f"scope-test-fanin-{operation}-head-consistency"
    scope = run_pending_scope_reconcile_candidate(
        conn,
        PID,
        project,
        target_commit_sha=head_commit,
        run_id=scope_snapshot_id,
        snapshot_id=scope_snapshot_id,
        created_by="test",
        semantic_enrich=False,
    )
    assert scope["ok"] is True
    assert scope["scope_file_delta"]["strategy"] == "incremental_graph_delta"
    assert scope["scope_file_delta"]["graph_delta_mode"] == "test_fanin_file_set"
    if operation == "add":
        assert scope["scope_file_delta"]["added_files"] == ["agent/tests/test_integration.py"]
        assert scope["scope_file_delta"]["removed_files"] == []
    elif operation == "remove":
        assert scope["scope_file_delta"]["added_files"] == []
        assert scope["scope_file_delta"]["removed_files"] == ["agent/tests/test_integration.py"]
    elif operation == "rename":
        assert scope["scope_file_delta"]["added_files"] == ["agent/tests/test_integration_renamed.py"]
        assert scope["scope_file_delta"]["removed_files"] == ["agent/tests/test_integration.py"]
    else:
        assert scope["scope_file_delta"]["added_files"] == ["agent/tests/test_integration_renamed.py"]
        assert scope["scope_file_delta"]["removed_files"] == ["agent/tests/test_integration.py"]
    assert scope["scope_graph_delta"]["mode"] == "test_fanin_file_set"
    assert scope["scope_graph_delta"]["added_nodes"] == []
    assert scope["scope_graph_delta"]["removed_nodes"] == []
    assert scope["scope_graph_delta"]["added_edges"] == []
    assert scope["scope_graph_delta"]["removed_edges"] == []

    full_snapshot_id = f"full-test-fanin-{operation}-head-consistency"
    full = run_state_only_full_reconcile(
        conn,
        PID,
        project,
        run_id=full_snapshot_id,
        commit_sha=head_commit,
        snapshot_id=full_snapshot_id,
        created_by="test",
        semantic_enrich=False,
    )
    assert full["ok"] is True

    assert _normalized_snapshot(conn, scope_snapshot_id) == _normalized_snapshot(
        conn,
        full_snapshot_id,
    )

    service = _nodes_by_module(_read_snapshot_graph(PID, scope_snapshot_id))["agent.service"]
    service_tests = set(service["test"])
    service_fanin_paths = {
        entry["path"]
        for entry in (service.get("metadata") or {}).get("test_consumer_fanin", [])
    }
    if operation == "add":
        assert "agent/tests/test_integration.py" in service_tests
        assert "agent/tests/test_integration.py" in service_fanin_paths
    elif operation == "remove":
        assert "agent/tests/test_integration.py" not in service_tests
        assert "agent/tests/test_integration.py" not in service_fanin_paths
    elif operation == "rename":
        assert "agent/tests/test_integration.py" not in service_tests
        assert "agent/tests/test_integration.py" not in service_fanin_paths
        assert "agent/tests/test_integration_renamed.py" in service_tests
        assert "agent/tests/test_integration_renamed.py" in service_fanin_paths
    else:
        other = _nodes_by_module(_read_snapshot_graph(PID, scope_snapshot_id))["agent.other"]
        other_tests = set(other["test"])
        other_fanin_paths = {
            entry["path"]
            for entry in (other.get("metadata") or {}).get("test_consumer_fanin", [])
        }
        assert "agent/tests/test_integration.py" not in service_tests
        assert "agent/tests/test_integration.py" not in service_fanin_paths
        assert "agent/tests/test_integration_renamed.py" not in service_tests
        assert "agent/tests/test_integration_renamed.py" not in service_fanin_paths
        assert "agent/tests/test_integration_renamed.py" in other_tests
        assert "agent/tests/test_integration_renamed.py" in other_fanin_paths
