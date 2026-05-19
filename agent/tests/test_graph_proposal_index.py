from __future__ import annotations

import json
from pathlib import Path

from agent.governance.graph_proposal_index import (
    aggregate_graph_enrich_config_proposals,
    normalize_rule_id,
)


FIXTURE = Path(__file__).parent / "fixtures" / "graph_enrich_config_dogfood_proposals.json"


def _fixture_events() -> list[dict]:
    return json.loads(FIXTURE.read_text())["events"]


def _cluster_by_family(family: str) -> dict:
    clusters = aggregate_graph_enrich_config_proposals(_fixture_events())
    return next(cluster for cluster in clusters if cluster["issue_family"] == family)


def test_dogfood_fixture_clusters_test_import_fanin_variants():
    cluster = _cluster_by_family("test_import_fanin_direct_symbol_gate")

    assert cluster["canonical_rule_id"] == "tests.test_import_fanin.require_direct_symbol_import"
    assert cluster["operation_count"] == 8
    assert set(cluster["support_event_ids"]) == {
        "ge-feadb0d045f6",
        "ge-9b08ce33297c",
        "ge-14036bc70d9f",
        "ge-cd027948ed37",
        "ge-396b6e8471b8",
        "ge-0b1f45f3c2f0",
        "ge-16edc78d5bdd",
        "ge-a657f53a17d2",
    }
    assert set(cluster["support_rule_ids"]) == {
        "test_import_fanin_agent_mcp_executor",
        "tests.test_import_fanin.require_direct_symbol_import",
        "tests-test_import_fanin-require-direct-symbol",
        "tests_test_import_fanin_require_direct_symbol_import",
    }


def test_dogfood_fixture_selects_observer_preferred_canonical_operation():
    cluster = _cluster_by_family("test_import_fanin_direct_symbol_gate")

    assert cluster["selected_event_id"] == "ge-0b1f45f3c2f0"
    assert cluster["selected_operation"]["rule_id"] == "tests.test_import_fanin.require_direct_symbol_import"
    assert cluster["selected_operation"]["op"] == "tighten_rule"
    assert cluster["selected_operation"]["downgrade_to"] == "weak_tests"
    assert cluster["selected_operation"]["when"] == {
        "all": [{"predicate": "source_evidence_is", "value": "test_import_fanin"}]
    }


def test_dogfood_fixture_keeps_unrelated_families_separate():
    clusters = aggregate_graph_enrich_config_proposals(_fixture_events())
    by_family = {cluster["issue_family"]: cluster for cluster in clusters}

    assert "test_import_fanin_direct_symbol_gate" in by_family
    assert "string_literal_event_false_positive" in by_family
    assert "weak_call_ambiguous_short_name" in by_family

    test_cluster_events = set(by_family["test_import_fanin_direct_symbol_gate"]["support_event_ids"])
    string_cluster_events = set(by_family["string_literal_event_false_positive"]["support_event_ids"])
    weak_call_events = set(by_family["weak_call_ambiguous_short_name"]["support_event_ids"])
    assert test_cluster_events.isdisjoint(string_cluster_events)
    assert test_cluster_events.isdisjoint(weak_call_events)


def test_cluster_retains_raw_evidence_and_variant_rule_ids():
    cluster = _cluster_by_family("test_import_fanin_direct_symbol_gate")

    assert cluster["raw_event_count"] == len(cluster["support_event_ids"])
    assert normalize_rule_id("tests-test_import_fanin-require-direct-symbol") == (
        "tests.test_import_fanin.require_direct_symbol_import"
    )
    assert any(
        operation["event_id"] == "ge-feadb0d045f6"
        and operation["rule_id"] == "test_import_fanin_agent_mcp_executor"
        and operation["when"]["all"][1]["predicate"] == "raw_target_in"
        for operation in cluster["operations"]
    )
