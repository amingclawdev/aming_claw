from __future__ import annotations

from pathlib import Path

from agent.governance.graph_rule_fingerprint import build_graph_rule_fingerprint


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_generated_project_rule_fingerprint_tracks_config_and_hint_rollback(tmp_path):
    project = tmp_path / "generated-rule-fingerprint-project"
    _write(project / "agent" / "service.py", "def run():\n    return 'ok'\n")

    anchor = build_graph_rule_fingerprint(project)

    config = project / ".aming-claw" / "reconcile" / "semantic_enrichment.yaml"
    _write(config, "graph_structure:\n  allowed_ops:\n    - add_edge\n")
    config_changed = build_graph_rule_fingerprint(project)
    assert config_changed["fingerprint"] != anchor["fingerprint"]
    assert config_changed["components"]["semantic_enrichment_config"]["fingerprint"] != (
        anchor["components"]["semantic_enrichment_config"]["fingerprint"]
    )

    config.unlink()
    config_rolled_back = build_graph_rule_fingerprint(project)
    assert config_rolled_back["fingerprint"] == anchor["fingerprint"]

    _write(
        project / "agent" / "service.py",
        "def run():\n"
        "    # aming-claw-hint:start id=hint-rollback op=add_edge edge=tests target=L7.1\n"
        "    # reason: generated project rollback test\n"
        "    # aming-claw-hint:end\n"
        "    return 'ok'\n",
    )
    hint_changed = build_graph_rule_fingerprint(project)
    assert hint_changed["fingerprint"] != anchor["fingerprint"]
    assert hint_changed["components"]["source_hints"]["hint_count"] == 1

    _write(project / "agent" / "service.py", "def run():\n    return 'ok'\n")
    hint_rolled_back = build_graph_rule_fingerprint(project)
    assert hint_rolled_back["fingerprint"] == anchor["fingerprint"]
