from __future__ import annotations

import json
import sqlite3

import pytest

from agent.governance import server
from agent.governance.db import _ensure_schema
from agent.governance.graph_enrich_config_ops import (
    SCHEMA_VERSION,
    run_graph_enrich_config_ai_output_pipeline,
)
from agent.governance.reconcile_semantic_config import (
    PROJECT_OVERRIDE_PATH,
    load_semantic_enrichment_config,
)


PID = "graph-enrich-config-ops-test"


class _NoCloseConn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    monkeypatch.setattr(server, "get_connection", lambda _project_id: _NoCloseConn(c))
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    yield c
    c.close()


def _ctx(body: dict):
    return server.RequestContext(
        None,
        "POST",
        {"project_id": PID},
        {},
        body,
        "req-graph-enrich-config-ops-test",
        "",
        "",
    )


def _payload() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "analyzer_role": "reconcile_graph_enrich_config_analyzer",
        },
        "operations": [
            {
                "op": "upsert_edge_evidence_policy",
                "rule_id": "calls-import-only-downgrade",
                "edge": "calls",
                "source_evidence": "import_only",
                "action": "downgrade",
                "downgrade_to": "imports",
                "confidence": 0.94,
                "evidence": {
                    "reason": "import-only type annotations should not create calls edges",
                },
            }
        ],
        "self_check": {
            "valid": True,
            "checked_rules": ["edge_supported", "action_supported", "config_path"],
            "known_risks": [],
        },
    }


def _rule_payload() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "analyzer_role": "reconcile_graph_enrich_config_analyzer",
        },
        "operations": [
            {
                "op": "review_rule",
                "rule_id": "weak_call_resolver.ambiguous_add",
                "edge": "calls",
                "source_evidence": "weak_call_resolver.ambiguous_add",
                "action": "downgrade",
                "downgrade_to": "ignored",
                "confidence": 0.81,
                "evidence": {
                    "reason": "Ambiguous weak-call suggestions need observer review before calls edges.",
                },
            },
            {
                "op": "promote_rule",
                "rule_id": "function_calls.strong_resolved_to_depends_on",
                "edge": "depends_on",
                "source_evidence": "function_calls",
                "action": "promote",
                "confidence": 0.9,
                "evidence": {
                    "reason": "Strong resolved function calls are dependency evidence.",
                },
            },
            {
                "op": "add_rule",
                "rule_id": "event_bus.subscribe_to_consumes_event",
                "edge": "consumes_event",
                "source_evidence": "event_bus.subscribe",
                "action": "add",
                "confidence": 0.86,
                "evidence": {
                    "reason": "event_bus subscribers consume published events.",
                },
            },
        ],
        "self_check": {
            "valid": True,
            "checked_rules": ["op_supported", "edge_supported", "config_patch_previewed"],
            "known_risks": [],
        },
    }


def _flexible_rule_payload() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "analyzer_role": "reconcile_graph_enrich_config_analyzer",
        },
        "operations": [
            {
                "op": "downgrade_relation_confidence",
                "rule_id": "emits_event.string_literal",
                "edge": "emits_event",
                "source_evidence": "string literal",
                "action": "downgrade",
                "downgrade_to": "references_schema",
                "confidence": 0.77,
                "evidence": {
                    "reason": "Prompt-template schema literals should not become runtime event emits.",
                },
            },
            {
                "op": "tighten_rule",
                "rule_id": "tests_edge_from_filename_match",
                "edge": "tests",
                "source_evidence": "test_import_fanin",
                "action": "require_direct_symbol_import",
                "downgrade_to": "weak_tests",
                "confidence": 0.73,
                "evidence": {
                    "reason": "Filename-only matches should not be strong tests edges.",
                },
            },
            {
                "op": "downgrade_rule",
                "rule_id": "weak_call_resolver.ambiguous_short_name",
                "edge": "calls",
                "source_evidence": "function_weak_calls ambiguous add candidates",
                "action": "downgrade",
                "downgrade_to": "drop",
                "confidence": 0.7,
                "evidence": {
                    "reason": "Bare collection method names should not create cross-module calls.",
                },
            },
        ],
        "self_check": {
            "valid": True,
            "checked_rules": ["op_supported", "edge_supported", "config_patch_previewed"],
            "known_risks": [],
        },
    }


def test_graph_enrich_config_ops_dry_run_is_non_mutating_generated_project(tmp_path):
    project = tmp_path / "generated-project"
    project.mkdir()

    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=json.dumps(_payload()),
        mode="dry_run",
        project_root=project,
    )

    assert result["ok"] is True
    assert result["precheck"]["status"] == "passed"
    assert result["precheck"]["classification"] == "passed"
    assert result["mutated"] is False
    assert result["accepted"] is False
    assert result["preview"]["config_path"] == str(project / PROJECT_OVERRIDE_PATH)
    assert result["preview"]["graph_structure_ops"]["evidence_policy"]["calls"] == {
        "import_only_action": "downgrade",
        "downgrade_to": "imports",
        "require_call_evidence": True,
    }
    assert not (project / PROJECT_OVERRIDE_PATH).exists()


def test_graph_enrich_config_ops_accept_writes_project_override_and_loader_reads_it(
    tmp_path,
):
    project = tmp_path / "generated-project"
    project.mkdir()

    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=json.dumps(_payload()),
        mode="accept",
        project_root=project,
    )

    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["mutated"] is True
    assert result["requires_commit"] is True
    override_path = project / PROJECT_OVERRIDE_PATH
    assert override_path.exists()
    assert "import_only_action: downgrade" in override_path.read_text(encoding="utf-8")

    config = load_semantic_enrichment_config(project_root=project)
    assert config.graph_structure_ops.evidence_policy["calls"] == {
        "import_only_action": "downgrade",
        "downgrade_to": "imports",
        "require_call_evidence": True,
    }


def test_graph_enrich_config_rule_ops_dry_run_previews_project_override(tmp_path):
    project = tmp_path / "generated-project"
    project.mkdir()

    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=json.dumps(_rule_payload()),
        mode="dry_run",
        project_root=project,
    )

    assert result["ok"] is True
    assert result["gate"]["accepted_count"] == 3
    rules = result["preview"]["graph_enrich_config_ops"]["rules"]
    assert rules["weak_call_resolver.ambiguous_add"]["action"] == "ignore"
    assert rules["weak_call_resolver.ambiguous_add"]["downgrade_to"] == ""
    assert rules["function_calls.strong_resolved_to_depends_on"]["action"] == "promote"
    assert rules["event_bus.subscribe_to_consumes_event"]["edge"] == "consumes_event"
    assert not (project / PROJECT_OVERRIDE_PATH).exists()


def test_graph_enrich_config_flexible_rule_ops_capture_observer_review_rules(tmp_path):
    project = tmp_path / "generated-project"
    project.mkdir()

    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=json.dumps(_flexible_rule_payload()),
        mode="dry_run",
        project_root=project,
    )

    assert result["ok"] is True
    assert result["gate"]["accepted_count"] == 3
    rules = result["preview"]["graph_enrich_config_ops"]["rules"]
    assert rules["emits_event.string_literal"]["downgrade_to"] == "references_schema"
    assert rules["tests_edge_from_filename_match"]["action"] == "require_direct_symbol_import"
    assert rules["weak_call_resolver.ambiguous_short_name"]["downgrade_to"] == "drop"
    weak_call = next(
        op for op in result["gate"]["operations"]
        if op["rule_id"] == "weak_call_resolver.ambiguous_short_name"
    )
    assert weak_call["normalizations"] == ["custom_source_evidence"]


def test_graph_enrich_config_aliases_update_rule_and_imports_module_edge(tmp_path):
    project = tmp_path / "generated-project"
    project.mkdir()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "analyzer_role": "reconcile_graph_enrich_config_analyzer",
        },
        "operations": [
            {
                "op": "update_rule",
                "rule_id": "imports_module_from_top_level_from_import",
                "edge": "imports_module",
                "source_evidence": "import_only",
                "action": "allow",
                "confidence": 0.7,
                "evidence": {
                    "reason": "Direct from-imports should map to the standard imports edge.",
                },
            },
            {
                "op": "update_rule",
                "rule_id": "weak_call_resolver.bare_builtin_names",
                "edge": "calls",
                "source_evidence": "function_weak_calls",
                "action": "downgrade",
                "downgrade_to": "ignore",
                "confidence": 0.69,
                "evidence": {
                    "reason": "Bare collection method names need stronger call evidence.",
                },
            },
        ],
        "self_check": {
            "valid": True,
            "checked_rules": ["op_supported", "edge_alias_normalized", "config_patch_previewed"],
            "known_risks": [],
        },
    }

    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=json.dumps(payload),
        mode="dry_run",
        project_root=project,
    )

    assert result["ok"] is True
    assert result["gate"]["accepted_count"] == 2
    assert result["gate"]["precheck"]["status"] == "passed"
    rules = result["preview"]["graph_enrich_config_ops"]["rules"]
    assert rules["imports_module_from_top_level_from_import"]["edge"] == "imports"
    assert rules["weak_call_resolver.bare_builtin_names"]["action"] == "ignore"
    assert rules["weak_call_resolver.bare_builtin_names"]["downgrade_to"] == ""


def test_graph_enrich_config_ops_precheck_marks_malformed_output_repairable(tmp_path):
    project = tmp_path / "generated-project"
    project.mkdir()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "analyzer_role": "reconcile_graph_enrich_config_analyzer",
        },
        "operations": [
            {
                "op": "invent_config_patch",
                "rule_id": "bad-op",
                "edge": "imaginary_edge",
                "source_evidence": "",
                "action": "",
            }
        ],
        "self_check": {"valid": True, "checked_rules": ["op_supported"], "known_risks": []},
    }

    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=json.dumps(payload),
        mode="dry_run",
        project_root=project,
    )

    assert result["ok"] is False
    assert result["precheck"]["classification"] == "model_repairable"
    assert result["precheck"]["retryable"] is True
    assert "unsupported_config_op" in result["precheck"]["repairable_errors"]


def test_graph_enrich_config_ops_api_accepts_ai_output_and_writes_project_override(
    conn,
    tmp_path,
):
    project = tmp_path / "generated-project"
    project.mkdir()

    status, result = server.handle_graph_governance_graph_enrich_config_ops_ai_output(
        _ctx(
            {
                "mode": "accept",
                "project_root": str(project),
                "ai_output": json.dumps(_payload()),
            }
        )
    )

    assert status == 200
    assert result["ok"] is True
    assert result["dry_run"] is False
    assert result["mutated"] is True
    assert result["project_root"] == str(project.resolve())
    assert (project / PROJECT_OVERRIDE_PATH).exists()
