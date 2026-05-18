from __future__ import annotations

from agent.governance.graph_rule_fingerprint import build_graph_rule_fingerprint
from agent.tests.fixtures.rule_fingerprint_project import (
    RULE_FINGERPRINT_SCENARIO_ID,
    apply_config_change,
    apply_hint_change,
    create_rule_fingerprint_fixture_project,
    rollback_config_change,
    rollback_hint_change,
)


def test_generated_project_rule_fingerprint_tracks_config_and_hint_rollback(tmp_path):
    assert RULE_FINGERPRINT_SCENARIO_ID == "RULE-FINGERPRINT-ROLLBACK-001"
    fixture = create_rule_fingerprint_fixture_project(tmp_path)

    anchor = build_graph_rule_fingerprint(fixture.root)

    apply_config_change(fixture)
    config_changed = build_graph_rule_fingerprint(fixture.root)
    assert config_changed["fingerprint"] != anchor["fingerprint"]
    assert config_changed["components"]["semantic_enrichment_config"]["fingerprint"] != (
        anchor["components"]["semantic_enrichment_config"]["fingerprint"]
    )

    rollback_config_change(fixture)
    config_rolled_back = build_graph_rule_fingerprint(fixture.root)
    assert config_rolled_back["fingerprint"] == anchor["fingerprint"]

    apply_hint_change(fixture)
    hint_changed = build_graph_rule_fingerprint(fixture.root)
    assert hint_changed["fingerprint"] != anchor["fingerprint"]
    assert hint_changed["components"]["source_hints"]["hint_count"] == 1

    rollback_hint_change(fixture)
    hint_rolled_back = build_graph_rule_fingerprint(fixture.root)
    assert hint_rolled_back["fingerprint"] == anchor["fingerprint"]
