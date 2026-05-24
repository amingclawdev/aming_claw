from __future__ import annotations

import json
import sqlite3

from agent.governance.asset_projection import (
    list_asset_bindings_for_node,
    list_asset_projection,
    upsert_doc_asset_projection,
)
from agent.governance.db import _ensure_schema
from agent.governance import graph_snapshot_store as store
from agent.governance.reconcile_status_observations import build_status_observation_issues


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def test_doc_asset_projection_persists_commit_bound_rows_and_bindings() -> None:
    conn = _conn()
    doc_state = {
        "schema_version": "doc_asset_state.v1",
        "project_id": "proj",
        "run_id": "run-a",
        "commit_sha": "abc1234",
        "docs": [
            {
                "schema_version": "doc_asset_state.v1",
                "project_id": "proj",
                "run_id": "run-a",
                "commit_sha": "abc1234",
                "path": "docs/ref.md",
                "doc_kind": "doc",
                "sha256": "hash-a",
                "file_hash": "sha256:hash-a",
                "size_bytes": 42,
                "scan_status": "secondary_attached",
                "graph_status": "attached",
                "binding_status": "accepted",
                "accepted_bindings": [
                    {
                        "node_id": "L7.1",
                        "title": "agent.service",
                        "role": "doc",
                        "source": "graph_node",
                    }
                ],
                "binding_candidates": [],
                "impact_scope_policy": "accepted_bindings_only",
            },
            {
                "schema_version": "doc_asset_state.v1",
                "project_id": "proj",
                "run_id": "run-a",
                "commit_sha": "abc1234",
                "path": "docs/candidate.md",
                "doc_kind": "doc",
                "sha256": "hash-b",
                "file_hash": "sha256:hash-b",
                "size_bytes": 99,
                "scan_status": "orphan",
                "graph_status": "unmapped",
                "binding_status": "candidate",
                "accepted_bindings": [],
                "binding_candidates": [
                    {
                        "proposal_hash": "sha256:proposal",
                        "target_node_id": "L7.2",
                        "target_title": "agent.other",
                        "role": "doc",
                        "source": "asset_binding_proposal",
                    }
                ],
                "impact_scope_policy": "accepted_bindings_only",
            },
        ],
    }

    summary = upsert_doc_asset_projection(
        conn,
        project_id="proj",
        snapshot_id="scope-abc1234",
        doc_asset_state=doc_state,
    )

    assert summary["projection_count"] == 2
    assert summary["binding_count"] == 2

    rows = list_asset_projection(
        conn,
        project_id="proj",
        snapshot_id="scope-abc1234",
        asset_kind="doc",
    )
    assert [row["asset_path"] for row in rows] == ["docs/candidate.md", "docs/ref.md"]
    accepted = next(row for row in rows if row["asset_path"] == "docs/ref.md")
    assert accepted["commit_sha"] == "abc1234"
    assert accepted["file_hash"] == "sha256:hash-a"
    assert accepted["binding_status"] == "accepted"
    assert accepted["accepted_bindings"][0]["node_id"] == "L7.1"
    assert accepted["impact_scope_policy"] == "accepted_bindings_only"

    bindings = list_asset_bindings_for_node(
        conn,
        project_id="proj",
        snapshot_id="scope-abc1234",
        node_id="L7.1",
        asset_kind="doc",
    )
    assert len(bindings) == 1
    assert bindings[0]["asset_path"] == "docs/ref.md"
    assert bindings[0]["binding_status"] == "accepted"
    assert bindings[0]["evidence"]["source"] == "graph_node"


def test_doc_asset_projection_replaces_same_snapshot_commit_kind() -> None:
    conn = _conn()
    first = {
        "run_id": "run-a",
        "commit_sha": "abc1234",
        "docs": [
            {
                "path": "docs/old.md",
                "doc_kind": "doc",
                "binding_status": "unbound",
                "accepted_bindings": [],
                "binding_candidates": [],
            }
        ],
    }
    second = {
        "run_id": "run-b",
        "commit_sha": "abc1234",
        "docs": [
            {
                "path": "docs/new.md",
                "doc_kind": "doc",
                "binding_status": "unbound",
                "accepted_bindings": [],
                "binding_candidates": [],
            }
        ],
    }

    upsert_doc_asset_projection(
        conn,
        project_id="proj",
        snapshot_id="scope-abc1234",
        doc_asset_state=first,
    )
    upsert_doc_asset_projection(
        conn,
        project_id="proj",
        snapshot_id="scope-abc1234",
        doc_asset_state=second,
    )

    rows = list_asset_projection(
        conn,
        project_id="proj",
        snapshot_id="scope-abc1234",
        asset_kind="doc",
    )
    assert [row["asset_path"] for row in rows] == ["docs/new.md"]


def test_status_observations_use_db_doc_projection_for_drift_scope() -> None:
    conn = _conn()
    snapshot = _create_runtime_snapshot(
        conn,
        scope_file_delta={
            "changed_files": ["src/runtime.py"],
            "impacted_files": ["src/runtime.py"],
        },
    )
    _upsert_runtime_doc_projection(conn, snapshot["snapshot_id"])

    built = build_status_observation_issues(
        conn,
        "proj",
        snapshot["snapshot_id"],
        include_missing_bindings=False,
        include_file_state=False,
        include_scope_delta=True,
    )

    assert built["issue_count"] == 1
    issue = built["issues"][0]
    assert issue["type"] == "doc_drift_candidate"
    assert issue["node_id"] == "L7.runtime"
    assert issue["paths"] == ["docs/runtime.md", "src/runtime.py"]
    assert issue["evidence"]["linked_docs"] == ["docs/runtime.md"]


def test_status_observations_do_not_flag_drift_when_bound_doc_changes() -> None:
    conn = _conn()
    snapshot = _create_runtime_snapshot(
        conn,
        scope_file_delta={
            "changed_files": ["src/runtime.py", "docs/runtime.md"],
            "impacted_files": ["src/runtime.py", "docs/runtime.md"],
        },
    )
    _upsert_runtime_doc_projection(conn, snapshot["snapshot_id"])

    built = build_status_observation_issues(
        conn,
        "proj",
        snapshot["snapshot_id"],
        include_missing_bindings=False,
        include_file_state=False,
        include_scope_delta=True,
    )

    assert built["issue_count"] == 0


def _create_runtime_snapshot(
    conn: sqlite3.Connection,
    *,
    scope_file_delta: dict[str, list[str]],
) -> dict[str, str]:
    graph = {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.runtime",
                    "layer": "L7",
                    "title": "Runtime Service",
                    "kind": "service_runtime",
                    "primary": ["src/runtime.py"],
                    "secondary": [],
                    "test": [],
                    "metadata": {},
                }
            ],
            "edges": [],
        }
    }
    snapshot = store.create_graph_snapshot(
        conn,
        "proj",
        snapshot_id="scope-runtime",
        commit_sha="abc1234",
        snapshot_kind="scope",
        graph_json=graph,
        file_inventory=[
            {
                "path": "src/runtime.py",
                "file_kind": "source",
                "scan_status": "clustered",
                "graph_status": "mapped",
                "attached_node_ids": ["L7.runtime"],
                "mapped_node_ids": ["L7.runtime"],
            }
        ],
        notes=json.dumps({
            "pending_scope_reconcile": {
                "scope_file_delta": scope_file_delta,
            }
        }),
    )
    store.index_graph_snapshot(
        conn,
        "proj",
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=[],
    )
    conn.commit()
    return snapshot


def _upsert_runtime_doc_projection(conn: sqlite3.Connection, snapshot_id: str) -> None:
    upsert_doc_asset_projection(
        conn,
        project_id="proj",
        snapshot_id=snapshot_id,
        doc_asset_state={
            "run_id": "run-a",
            "commit_sha": "abc1234",
            "docs": [
                {
                    "path": "docs/runtime.md",
                    "doc_kind": "doc",
                    "binding_status": "accepted",
                    "accepted_bindings": [
                        {
                            "node_id": "L7.runtime",
                            "title": "Runtime Service",
                            "role": "doc",
                            "source": "review_decision",
                        }
                    ],
                    "binding_candidates": [],
                    "impact_scope_policy": "accepted_bindings_only",
                }
            ],
        },
    )
    conn.commit()
