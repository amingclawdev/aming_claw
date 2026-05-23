from __future__ import annotations

import json
from pathlib import Path

from agent.governance.asset_inbox_contract import (
    ASSET_STATUSES,
    BATCH_ACTIONS,
    validate_asset_inbox_payload,
)


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
