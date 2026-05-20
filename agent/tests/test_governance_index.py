from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent.governance import graph_snapshot_store as store
from agent.governance.db import _ensure_schema
from agent.governance.governance_index import (
    build_governance_index,
    load_snapshot_nodes_for_inventory,
    merge_feature_hashes_into_graph_nodes,
    persist_governance_index,
)


PID = "governance-index-test"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    yield c
    c.close()


def _write_project(root: Path) -> None:
    (root / "src" / "demo_app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "docs").mkdir()
    (root / "README.md").write_text(
        "# Demo App\n\nThis index explains the demo service.\n",
        encoding="utf-8",
    )
    (root / "docs" / "usage.md").write_text(
        "# Usage\n\nCall the service from a route.\n",
        encoding="utf-8",
    )
    (root / "src" / "demo_app" / "service.py").write_text(
        "def calculate_total(items):\n"
        "    return sum(items)\n\n"
        "STATUS_READY = 'ready'\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_service.py").write_text(
        "from src.demo_app.service import calculate_total\n\n"
        "def test_calculate_total():\n"
        "    assert calculate_total([1, 2]) == 3\n\n"
        "def test_calculate_total_empty():\n"
        "    assert calculate_total([]) == 0\n",
        encoding="utf-8",
    )


def _activate_graph(conn) -> str:
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="imported-abc1234-index",
        commit_sha="abc1234",
        snapshot_kind="imported",
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=[
            {
                "id": "L7.service",
                "layer": "L7",
                "title": "Demo Service",
                "kind": "feature",
                "primary": ["src/demo_app/service.py"],
                "secondary": ["README.md", "docs/usage.md"],
                "test": ["tests/test_service.py"],
                "metadata": {"subsystem": "demo"},
            }
        ],
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    return snapshot["snapshot_id"]


def test_load_snapshot_nodes_for_inventory_decodes_file_mappings(conn):
    snapshot_id = _activate_graph(conn)

    nodes = load_snapshot_nodes_for_inventory(conn, PID, snapshot_id)

    assert nodes == [
        {
            "id": "L7.service",
            "node_id": "L7.service",
            "layer": "L7",
            "title": "Demo Service",
            "kind": "feature",
            "primary": ["src/demo_app/service.py"],
            "secondary": ["README.md", "docs/usage.md"],
            "test": ["tests/test_service.py"],
            "metadata": {"subsystem": "demo"},
        }
    ]


def test_build_and_persist_governance_index_maps_hashes_symbols_docs_and_graph(conn, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _write_project(project)
    _activate_graph(conn)

    index = build_governance_index(
        conn,
        PID,
        project,
        run_id="index-abc1234-test",
        commit_sha="abc1234",
    )

    rows = {row["path"]: row for row in index["file_inventory"]}
    assert rows["src/demo_app/service.py"]["scan_status"] == "clustered"
    assert rows["src/demo_app/service.py"]["graph_status"] == "mapped"
    assert rows["src/demo_app/service.py"]["mapped_node_ids"] == ["L7.service"]
    assert rows["src/demo_app/service.py"]["attached_node_ids"] == ["L7.service"]
    assert rows["src/demo_app/service.py"]["attachment_role"] == "primary"
    assert rows["src/demo_app/service.py"]["attachment_source"] == "graph_node"
    assert rows["README.md"]["scan_status"] == "secondary_attached"
    assert rows["README.md"]["graph_status"] == "attached"
    assert rows["README.md"]["attached_node_ids"] == ["L7.service"]
    assert rows["README.md"]["attachment_role"] == "doc"
    assert rows["docs/usage.md"]["scan_status"] == "secondary_attached"
    assert rows["tests/test_service.py"]["scan_status"] == "secondary_attached"
    assert rows["tests/test_service.py"]["attached_node_ids"] == ["L7.service"]
    assert rows["tests/test_service.py"]["attachment_role"] == "test"
    assert rows["src/demo_app/service.py"]["file_hash"].startswith("sha256:")
    assert rows["src/demo_app/service.py"]["last_scanned_commit"] == "abc1234"

    symbol_index = index["symbol_index"]
    symbol = next(
        item for item in symbol_index["symbols"]
        if item["id"].endswith("::calculate_total")
    )
    assert symbol["path"] == "src/demo_app/service.py"
    assert symbol["line_start"] == 1
    assert symbol["line_end"] >= symbol["line_start"]
    assert any(
        item["id"] == "src.demo_app.service::STATUS_READY" and item["kind"] == "constant"
        for item in symbol_index["symbols"]
    )
    test_symbol = next(
        item for item in symbol_index["symbols"]
        if item["id"] == "tests.test_service::test_calculate_total"
    )
    assert test_symbol["kind"] == "test_function"
    assert test_symbol["source_hash"].startswith("sha256:")

    doc_index = index["doc_index"]
    readme = next(item for item in doc_index["documents"] if item["path"] == "README.md")
    assert readme["headings"][0]["title"] == "Demo App"
    feature_index = index["feature_index"]
    feature = next(item for item in feature_index["features"] if item["node_id"] == "L7.service")
    assert feature["feature_hash"].startswith("sha256:")
    function_ref = next(item for item in feature["symbol_refs"] if item["id"].endswith("::calculate_total"))
    assert function_ref["line_start"] == 1
    assert function_ref["source_hash"].startswith("sha256:")
    assert feature["function_hashes"][function_ref["id"]] == function_ref["source_hash"]
    test_ref = next(
        item for item in feature["test_symbol_refs"]
        if item["id"] == "tests.test_service::test_calculate_total"
    )
    assert test_ref["source_hash"] == test_symbol["source_hash"]
    assert feature["test_function_hashes"][test_ref["id"]] == test_ref["source_hash"]
    assert feature["test_functions"] == [
        "tests.test_service::test_calculate_total",
        "tests.test_service::test_calculate_total_empty",
    ]
    assert feature["test_function_lines"]["test_calculate_total"] == [3, 4]
    assert feature["test_function_lines"]["tests.test_service::test_calculate_total"] == [3, 4]
    assert feature["doc_refs"][0]["path"] in {"README.md", "docs/usage.md"}
    graph_payload = {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.service",
                    "metadata": {},
                }
            ]
        }
    }
    merge = merge_feature_hashes_into_graph_nodes(graph_payload, index)
    assert merge["nodes_updated"] == 1
    assert graph_payload["deps_graph"]["nodes"][0]["metadata"]["function_hashes"] == feature["function_hashes"]
    assert graph_payload["deps_graph"]["nodes"][0]["metadata"]["test_function_hashes"] == feature["test_function_hashes"]
    assert graph_payload["deps_graph"]["nodes"][0]["metadata"]["test_function_lines"] == feature["test_function_lines"]
    assert index["coverage_state"]["active_snapshot_id"] == "imported-abc1234-index"
    assert index["coverage_state"]["feature_count"] == 1
    assert index["coverage_state"]["file_states"]["src/demo_app/service.py"]["file_hash"]
    assert (
        index["coverage_state"]["file_states"]["tests/test_service.py"]["attached_node_ids"]
        == ["L7.service"]
    )
    assert "confidence" not in json.dumps(index, ensure_ascii=False)
    assert index["project_root_role"] == "execution_root"
    assert index["checkout_provenance"]["execution_root_role"] == "execution_root"
    assert index["checkout_provenance"]["canonical_project_identity"]["project_id"] == PID
    assert index["profile"]["project_root_role"] == "execution_root"
    assert index["profile"]["checkout_provenance"]["execution_root"] == index["project_root"]

    summary = persist_governance_index(
        conn,
        PID,
        index,
        artifact_root=tmp_path / "artifacts",
    )

    assert summary["inventory_rows_persisted"] == len(index["file_inventory"])
    assert summary["feature_count"] == 1
    for path in summary["artifacts"].values():
        assert Path(path).exists()
    profile_payload = json.loads(Path(summary["artifacts"]["profile_path"]).read_text(encoding="utf-8"))
    assert profile_payload["project_root_role"] == "execution_root"
    assert profile_payload["checkout_provenance"]["canonical_project_identity"]["project_id"] == PID

    persisted = conn.execute(
        """
        SELECT scan_status, file_hash, attached_node_ids, attachment_role
        FROM reconcile_file_inventory
        WHERE project_id=? AND run_id=? AND path=?
        """,
        (PID, "index-abc1234-test", "README.md"),
    ).fetchone()
    assert persisted["scan_status"] == "secondary_attached"
    assert persisted["file_hash"].startswith("sha256:")
    assert json.loads(persisted["attached_node_ids"]) == ["L7.service"]
    assert persisted["attachment_role"] == "doc"


def test_build_governance_index_can_use_candidate_graph_before_activation(conn, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _write_project(project)
    candidate_graph = {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.candidate",
                    "layer": "L7",
                    "title": "Candidate Service",
                    "kind": "feature",
                    "primary": ["src/demo_app/service.py"],
                    "secondary": ["README.md"],
                    "test": ["tests/test_service.py"],
                    "metadata": {"subsystem": "candidate"},
                }
            ],
            "edges": [],
        }
    }

    index = build_governance_index(
        conn,
        PID,
        project,
        run_id="index-candidate-test",
        commit_sha="def5678",
        candidate_graph=candidate_graph,
        snapshot_id="full-def5678-candidate",
        snapshot_kind="full",
    )

    assert index["index_scope"] == "candidate_snapshot"
    assert index["active_snapshot"]["snapshot_id"] == "full-def5678-candidate"
    assert index["coverage_state"]["active_snapshot_id"] == "full-def5678-candidate"
    rows = {row["path"]: row for row in index["file_inventory"]}
    assert rows["src/demo_app/service.py"]["mapped_node_ids"] == ["L7.candidate"]
    assert rows["tests/test_service.py"]["attached_node_ids"] == ["L7.candidate"]
    assert rows["tests/test_service.py"]["attachment_role"] == "test"
    assert index["feature_index"]["features"][0]["node_id"] == "L7.candidate"


def test_build_governance_index_attaches_orphan_doc_from_governance_hint(conn, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _write_project(project)
    (project / "docs" / "orphan.md").write_text(
        "<!-- governance-hint\n"
        '{"attach_to_node":{"target_module":"src.demo_app.service","role":"doc"}}'
        "\n-->\n# Orphan Service Notes\n",
        encoding="utf-8",
    )
    candidate_graph = {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.service",
                    "layer": "L7",
                    "title": "Demo Service",
                    "kind": "feature",
                    "primary": ["src/demo_app/service.py"],
                    "secondary": [],
                    "test": ["tests/test_service.py"],
                    "metadata": {"module": "src.demo_app.service"},
                }
            ],
            "edges": [],
        }
    }

    index = build_governance_index(
        conn,
        PID,
        project,
        run_id="index-hint-test",
        commit_sha="def5678",
        candidate_graph=candidate_graph,
        snapshot_id="full-def5678-hint",
        snapshot_kind="full",
    )

    node = candidate_graph["deps_graph"]["nodes"][0]
    assert node["secondary"] == ["docs/orphan.md"]
    assert index["governance_hint_bindings"]["applied_count"] == 1
    rows = {row["path"]: row for row in index["file_inventory"]}
    assert rows["docs/orphan.md"]["scan_status"] == "secondary_attached"
    assert rows["docs/orphan.md"]["graph_status"] == "attached"
    assert rows["docs/orphan.md"]["attached_node_ids"] == ["L7.service"]
    assert rows["docs/orphan.md"]["attachment_role"] == "doc"
    feature = index["feature_index"]["features"][0]
    assert any(ref["path"] == "docs/orphan.md" for ref in feature["doc_refs"])
