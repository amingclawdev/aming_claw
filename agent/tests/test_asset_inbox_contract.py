from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent.governance import graph_events
from agent.governance import graph_snapshot_store as store
from agent.governance import server
from agent.governance.asset_inbox_contract import (
    ASSET_STATUSES,
    BATCH_ACTIONS,
    asset_inbox_batch_actions,
    build_asset_inbox_response,
    validate_asset_inbox_payload,
)
from agent.governance.db import _ensure_schema
from agent.governance.reconcile_semantic_enrichment import _ensure_semantic_state_schema


FIXTURE_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "fixtures"
    / "asset-inbox-contract-mock.json"
)


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_asset_inbox_mock_payload_passes_shared_precheck() -> None:
    payload = _fixture()

    result = validate_asset_inbox_payload(payload)

    assert result["ok"] is True
    assert result["errors"] == []
    assert result["item_count"] == payload["summary"]["total"]
    assert result["status_count"] == len(ASSET_STATUSES)


def test_asset_inbox_precheck_accepts_empty_read_model() -> None:
    payload = {
        "schema_version": "asset_inbox.v1",
        "ok": True,
        "impact_scope_policy": "accepted_bindings_only",
        "backlog_policy": {
            "default_container": False,
            "create_from_selected_assets_only": True,
        },
        "summary": {"total": 0, "by_status": {}},
        "items": [],
        "batch_actions": asset_inbox_batch_actions(),
    }

    result = validate_asset_inbox_payload(payload)

    assert result["ok"] is True
    assert result["item_count"] == 0


def test_asset_inbox_fixture_covers_every_status_and_batch_action() -> None:
    payload = _fixture()

    assert set(payload["summary"]["by_status"]) == ASSET_STATUSES
    assert {action["action"] for action in payload["batch_actions"]} == BATCH_ACTIONS
    assert payload["impact_scope_policy"] == "accepted_bindings_only"
    assert payload["backlog_policy"] == {
        "default_container": False,
        "create_from_selected_assets_only": True,
        "reason": "Asset Inbox tracks graph/file hygiene state. Backlog rows are created only for selected actionable work.",
    }


def test_candidates_are_reviewable_but_not_trusted_bindings() -> None:
    payload = _fixture()
    candidates = [
        item
        for item in payload["items"]
        if item["asset_status"] in {"doc_candidate", "test_candidate"}
    ]

    assert {item["asset_kind"] for item in candidates} == {"doc", "test"}
    for item in candidates:
        assert item["accepted_bindings"] == []
        assert item["binding_candidates"]
        candidate = item["binding_candidates"][0]
        assert candidate["precheck"]["ok"] is True
        assert candidate["precheck"]["decision"] == "review_required"
        assert candidate["precheck"]["binding_strength"] == "weak"
        assert candidate["precheck"]["proposal_hash"] == candidate["proposal_hash"]


def test_accepted_bindings_only_enter_impact_scope() -> None:
    payload = _fixture()
    accepted = [
        item for item in payload["items"]
        if item["asset_status"] == "accepted"
    ]

    assert len(accepted) == 1
    assert accepted[0]["binding_candidates"] == []
    assert accepted[0]["accepted_bindings"] == [
        {
            "node_id": "L7.runtime",
            "title": "src.runtime",
            "role": "doc",
            "source": "source_controlled_hint",
        }
    ]


def test_backlog_is_created_from_selected_assets_not_orphan_container() -> None:
    payload = _fixture()
    eligible = [
        item["asset_status"]
        for item in payload["items"]
        if item["backlog"]["eligible"] is True
    ]
    action = next(
        action for action in payload["batch_actions"]
        if action["action"] == "create_backlog_from_selection"
    )
    hint_action = next(
        action for action in payload["batch_actions"]
        if action["action"] == "write_governance_hint"
    )

    assert sorted(eligible) == ["config_pending_decision", "source_orphan", "stale"]
    assert action["creates_backlog"] is True
    assert action["requires_selection"] is True
    assert action["allowed_statuses"] == [
        "source_orphan",
        "config_pending_decision",
        "stale",
    ]
    assert hint_action["mutates_source"] is True
    assert hint_action["requires_review"] is True


class _NoCloseConn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


@pytest.fixture()
def asset_inbox_conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    store.ensure_schema(conn)
    _ensure_semantic_state_schema(conn)
    monkeypatch.setattr(server, "get_connection", lambda _project_id: _NoCloseConn(conn))
    yield conn
    conn.close()


def _asset_inbox_graph() -> dict:
    return {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.runtime",
                    "layer": "L7",
                    "title": "src.runtime",
                    "kind": "service_runtime",
                    "primary": ["src/runtime.py"],
                    "secondary": ["docs/accepted-runtime.md"],
                    "test": [],
                    "metadata": {"module": "src.runtime"},
                }
            ],
            "edges": [],
        }
    }


def _asset_inbox_inventory() -> list[dict]:
    return [
        {
            "path": "src/newFeature.ts",
            "file_kind": "source",
            "language": "typescript",
            "scan_status": "orphan",
            "graph_status": "unmapped",
            "decision": "pending",
            "file_hash": "sha256:111",
            "sha256": "111",
        },
        {
            "path": "docs/service.md",
            "file_kind": "doc",
            "language": "markdown",
            "scan_status": "orphan",
            "graph_status": "unmapped",
            "decision": "pending",
            "file_hash": "sha256:222",
            "sha256": "222",
        },
        {
            "path": "docs/runtime.md",
            "file_kind": "doc",
            "language": "markdown",
            "scan_status": "orphan",
            "graph_status": "unmapped",
            "decision": "pending",
            "candidate_node_id": "L7.runtime",
            "file_hash": "sha256:333",
            "sha256": "333",
        },
        {
            "path": "docs/accepted-runtime.md",
            "file_kind": "doc",
            "language": "markdown",
            "scan_status": "secondary_attached",
            "graph_status": "attached",
            "decision": "attach_to_node",
            "attached_node_ids": ["L7.runtime"],
            "attachment_role": "doc",
            "attachment_source": "graph_node",
            "file_hash": "sha256:444",
            "sha256": "444",
        },
        {
            "path": "tests/test_runtime_bridge.py",
            "file_kind": "test",
            "language": "python",
            "scan_status": "orphan",
            "graph_status": "unmapped",
            "decision": "pending",
            "candidate_node_id": "L7.runtime",
            "file_hash": "sha256:555",
            "sha256": "555",
        },
        {
            "path": "config/runtime.yaml",
            "file_kind": "config",
            "language": "yaml",
            "scan_status": "pending_decision",
            "graph_status": "pending_decision",
            "decision": "pending",
            "file_hash": "sha256:666",
            "sha256": "666",
        },
        {
            "path": "dist/bundle.js",
            "file_kind": "generated",
            "scan_status": "ignored",
            "graph_status": "ignored",
            "decision": "ignore",
            "file_hash": "sha256:777",
            "sha256": "777",
        },
        {
            "path": "docs/archive.md",
            "file_kind": "doc",
            "language": "markdown",
            "scan_status": "archive",
            "graph_status": "archive",
            "decision": "keep",
            "file_hash": "sha256:888",
            "sha256": "888",
        },
        {
            "path": "src/runtime.py",
            "file_kind": "source",
            "language": "python",
            "scan_status": "clustered",
            "graph_status": "mapped",
            "decision": "govern",
            "mapped_node_ids": ["L7.runtime"],
            "file_hash": "sha256:999",
            "sha256": "999",
        },
    ]


def _create_asset_inbox_snapshot(conn: sqlite3.Connection, *, semantic_table_stale: bool = True) -> str:
    graph = _asset_inbox_graph()
    snapshot = store.create_graph_snapshot(
        conn,
        "asset-inbox-live-test",
        snapshot_id="asset-inbox-live",
        commit_sha="livecommit",
        snapshot_kind="full",
        graph_json=graph,
        file_inventory=_asset_inbox_inventory(),
    )
    store.index_graph_snapshot(
        conn,
        "asset-inbox-live-test",
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=graph["deps_graph"]["edges"],
    )
    if semantic_table_stale:
        conn.execute(
            """
            INSERT INTO graph_semantic_nodes
              (project_id, snapshot_id, node_id, status, feature_hash, file_hashes_json, semantic_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "asset-inbox-live-test",
                snapshot["snapshot_id"],
                "L7.runtime",
                "semantic_stale_feature_hash",
                "old-feature",
                "{}",
                "{}",
                "2026-05-23T00:00:00Z",
            ),
        )
    store.activate_graph_snapshot(conn, "asset-inbox-live-test", snapshot["snapshot_id"])
    conn.commit()
    return snapshot["snapshot_id"]


def test_live_asset_inbox_response_materializes_from_snapshot_state(asset_inbox_conn) -> None:
    snapshot_id = _create_asset_inbox_snapshot(asset_inbox_conn)

    payload = build_asset_inbox_response(asset_inbox_conn, "asset-inbox-live-test", snapshot_id)
    by_path = {item["path"]: item for item in payload["items"]}

    assert payload["ok"] is True
    assert payload["precheck"]["ok"] is True
    assert payload["summary"]["by_status"] == {
        "accepted": 1,
        "archive": 1,
        "config_pending_decision": 1,
        "doc_candidate": 1,
        "doc_unbound": 1,
        "ignored": 1,
        "source_orphan": 1,
        "stale": 1,
        "test_candidate": 1,
    }
    assert by_path["docs/runtime.md"]["binding_candidates"][0]["precheck"]["decision"] == "review_required"
    assert by_path["tests/test_runtime_bridge.py"]["asset_status"] == "test_candidate"
    assert by_path["docs/accepted-runtime.md"]["asset_status"] == "accepted"
    assert by_path["src/runtime.py"]["asset_status"] == "stale"
    assert by_path["src/runtime.py"]["backlog"]["eligible"] is True


def test_asset_inbox_api_supports_active_snapshot(asset_inbox_conn) -> None:
    snapshot_id = _create_asset_inbox_snapshot(asset_inbox_conn)

    payload = server.handle_graph_governance_snapshot_asset_inbox(
        server.RequestContext(
            None,
            "GET",
            {"project_id": "asset-inbox-live-test", "snapshot_id": "active"},
            {},
            {},
            "req-test",
            "",
            "",
        )
    )

    assert payload["ok"] is True
    assert payload["snapshot_id"] == snapshot_id
    assert payload["summary"]["operator_review_count"] == 5


def test_live_asset_inbox_response_uses_semantic_projection_stale_state(asset_inbox_conn, monkeypatch) -> None:
    snapshot_id = _create_asset_inbox_snapshot(asset_inbox_conn, semantic_table_stale=False)

    monkeypatch.setattr(
        graph_events,
        "get_semantic_projection",
        lambda _conn, _project_id, _snapshot_id: {
            "projection": {
                "node_semantics": {
                    "L7.runtime": {
                        "validity": {
                            "status": "semantic_stale_feature_hash",
                        }
                    }
                }
            }
        },
    )

    payload = build_asset_inbox_response(asset_inbox_conn, "asset-inbox-live-test", snapshot_id)
    by_path = {item["path"]: item for item in payload["items"]}

    assert payload["ok"] is True
    assert by_path["src/runtime.py"]["asset_status"] == "stale"
    assert payload["summary"]["by_status"]["stale"] == 1
