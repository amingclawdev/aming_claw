from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent.governance import server
from agent.governance.db import _ensure_schema
from agent.governance.graph_enrich_config_ops import (
    SCHEMA_VERSION,
    evaluate_graph_enrich_config_rules,
    graph_enrich_config_ops_output_contract,
    run_graph_enrich_config_ai_output_pipeline,
)
from agent.governance.reconcile_semantic_config import (
    PROJECT_OVERRIDE_PATH,
    load_semantic_enrichment_config,
)
from agent.tests.fixtures.semantic_project_config_scenarios import (
    core_semantic_config_texts,
    create_external_semantic_project,
    project_local_policy_payload,
    register_function_payload,
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


def test_graph_enrich_config_contract_exposes_policy_op_constraints():
    contract = graph_enrich_config_ops_output_contract()

    constraints = contract["operation_constraints"]["upsert_edge_evidence_policy"]
    assert constraints["edges"] == ["calls"]
    assert constraints["source_evidence"] == ["import_only"]
    assert constraints["actions"] == ["allow", "downgrade", "reject"]
    assert "function_calls" in constraints["note"]
    assert "register_function" in contract["supported_upstream_proposal_operations"]
    assert (
        contract["operation_constraints"]["upstream_proposal_ops"]["recommended_action"]
        == "propose_upstream_pr"
    )
    assert (
        "allow"
        in contract["operation_constraints"]["rule_op_action_compatibility"]["tighten_rule"][
            "disallowed_actions"
        ]
    )
    assert "op_action_compatible" in contract["self_precheck"]["checked_rules_required"]
    assert "language_is" in contract["supported_predicates"]
    assert "receiver_kind_in" in contract["supported_predicates"]
    must_not_mark_valid = contract["self_precheck"]["must_not_mark_valid_when"]
    assert any(item["error"] == "predicate_underconstrained_weak_call" for item in must_not_mark_valid)
    assert any(
        item["error"] == "predicate_underconstrained_string_literal"
        for item in must_not_mark_valid
    )
    required_rules = contract["self_precheck"]["checked_rules_required"]
    assert "predicate_guard_weak_call_requires_call_syntax_or_receiver" in required_rules
    assert "predicate_guard_string_literal_requires_raw_target" in required_rules


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


def _predicate_rule_payload() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "analyzer_role": "reconcile_graph_enrich_config_analyzer",
        },
        "operations": [
            {
                "op": "tighten_rule",
                "rule_id": "python.container_attribute_add_not_cross_module_call",
                "edge": "calls",
                "source_evidence": "weak_call_resolver_ambiguous_add",
                "action": "ignore",
                "confidence": 0.82,
                "when": {
                    "all": [
                        {"predicate": "language_is", "value": "python"},
                        {"predicate": "call_syntax_is", "value": "attribute_call"},
                        {
                            "predicate": "receiver_kind_in",
                            "values": ["builtin_collection", "local_collection"],
                        },
                        {"predicate": "raw_target_in", "values": ["add"]},
                    ]
                },
                "evidence": {
                    "reason": "Python container .add() is not a cross-module calls edge.",
                },
            },
        ],
        "self_check": {
            "valid": True,
            "checked_rules": [
                "op_supported",
                "predicate_supported",
                "config_patch_previewed",
                "observer_approval_required",
            ],
            "known_risks": [],
        },
    }


def _write_generated_add_case_project(project):
    project.mkdir()
    (project / "service.py").write_text(
        "\n".join(
            [
                "from math_ops import add",
                "",
                "def container_case(value):",
                "    items = set()",
                "    items.add(value)",
                "    return items",
                "",
                "def direct_import_case(left, right):",
                "    return add(left, right)",
            ]
        ),
        encoding="utf-8",
    )
    (project / "math_ops.py").write_text(
        "\n".join(
            [
                "def add(left, right):",
                "    return left + right",
            ]
        ),
        encoding="utf-8",
    )


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


def test_graph_enrich_config_accept_isolates_external_project_override_from_core_repo(
    tmp_path,
):
    repo_root = Path(__file__).resolve().parents[2]
    core_before = core_semantic_config_texts(repo_root)
    scenario = create_external_semantic_project(tmp_path / "user-project")

    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=json.dumps(project_local_policy_payload()),
        mode="accept",
        project_root=scenario.root,
    )

    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["mutated"] is True
    assert result["write"]["config_path"] == str(scenario.override_path)
    assert scenario.override_path.exists()
    assert "import_only_action: downgrade" in scenario.override_path.read_text(encoding="utf-8")
    assert core_semantic_config_texts(repo_root) == core_before


def test_graph_enrich_config_rejects_register_function_payload_without_mutation(
    tmp_path,
):
    repo_root = Path(__file__).resolve().parents[2]
    core_before = core_semantic_config_texts(repo_root)
    scenario = create_external_semantic_project(tmp_path / "user-project")

    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=json.dumps(register_function_payload()),
        mode="accept",
        project_root=scenario.root,
    )

    assert result["ok"] is False
    assert result["accepted"] is False
    assert result["mutated"] is False
    assert result["recommended_action"] == "propose_upstream_pr"
    assert result["upstream_proposal_count"] == 1
    assert result["gate"]["upstream_proposal_count"] == 1
    assert result["gate"]["recommended_action"] == "propose_upstream_pr"
    operation = result["gate"]["operations"][0]
    assert operation["errors"] == ["unsupported_config_op"]
    assert operation["upstream_proposal"]["op"] == "register_function"
    assert operation["upstream_proposal"]["function_name"] == "resolve_project_specific_calls"
    assert operation["upstream_proposal"]["proposal_scope"] == "upstream"
    assert operation["upstream_proposal"]["requires_observer_review"] is True
    assert not scenario.override_path.exists()
    assert core_semantic_config_texts(repo_root) == core_before


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


def test_graph_enrich_config_rejects_policy_op_for_non_calls_edge(tmp_path):
    project = tmp_path / "generated-project"
    project.mkdir()
    payload = _payload()
    payload["operations"][0]["edge"] = "depends_on"
    payload["operations"][0]["rule_id"] = "depends-on-import-only-policy"

    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=json.dumps(payload),
        mode="dry_run",
        project_root=project,
    )

    assert result["ok"] is False
    operation = result["gate"]["operations"][0]
    assert operation["status"] == "rejected"
    assert "edge_unsupported_for_policy" in operation["errors"]
    assert result["preview"]["graph_enrich_config_ops"]["rules"] == {}
    assert result["preview"]["graph_structure_ops"]["evidence_policy"] == {}


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


def test_graph_enrich_config_predicate_rule_orchestrates_generated_add_case(tmp_path):
    project = tmp_path / "generated-add-case"
    _write_generated_add_case_project(project)

    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=json.dumps(_predicate_rule_payload()),
        mode="dry_run",
        project_root=project,
    )

    assert result["ok"] is True
    rule = result["preview"]["graph_enrich_config_ops"]["rules"][
        "python.container_attribute_add_not_cross_module_call"
    ]
    assert rule["when"]["all"][0] == {"predicate": "language_is", "value": "python"}
    assert rule["when"]["all"][2] == {
        "predicate": "receiver_kind_in",
        "values": ["builtin_collection", "local_collection"],
    }

    container_decision = evaluate_graph_enrich_config_rules(
        result["preview"]["graph_enrich_config_ops"]["rules"],
        {
            "edge": "calls",
            "source_evidence": "weak_call_resolver_ambiguous_add",
            "language": "python",
            "call_syntax": "attribute_call",
            "receiver_kind": "builtin_collection",
            "raw_target": "add",
            "source_path": "service.py",
        },
    )
    assert container_decision["matched"] is True
    assert container_decision["rule_id"] == "python.container_attribute_add_not_cross_module_call"
    assert container_decision["action"] == "ignore"

    direct_import_decision = evaluate_graph_enrich_config_rules(
        result["preview"]["graph_enrich_config_ops"]["rules"],
        {
            "edge": "calls",
            "source_evidence": "weak_call_resolver_ambiguous_add",
            "language": "python",
            "call_syntax": "name_call",
            "receiver_kind": "",
            "raw_target": "add",
            "source_path": "service.py",
        },
    )
    assert direct_import_decision["matched"] is False
    assert direct_import_decision["action"] == ""


def test_graph_enrich_config_call_syntax_method_alias_matches_attribute_call(tmp_path):
    project = tmp_path / "generated-add-case"
    _write_generated_add_case_project(project)
    payload = _predicate_rule_payload()
    payload["operations"][0]["rule_id"] = "calls.weak_resolver.short_name_add_method_drop"
    payload["operations"][0]["source_evidence"] = "weak_call_resolver_ambiguous_short_name"
    payload["operations"][0]["action"] = "drop"
    payload["operations"][0]["when"]["all"][0] = {
        "predicate": "source_evidence_is",
        "value": "weak_call_resolver_ambiguous_short_name",
    }
    payload["operations"][0]["when"]["all"][1] = {
        "predicate": "raw_target_in",
        "values": ["add"],
    }
    payload["operations"][0]["when"]["all"][2] = {
        "predicate": "call_syntax_is",
        "value": "method",
    }
    payload["operations"][0]["when"]["all"] = payload["operations"][0]["when"]["all"][:3]

    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=json.dumps(payload),
        mode="dry_run",
        project_root=project,
    )

    assert result["ok"] is True
    rules = result["preview"]["graph_enrich_config_ops"]["rules"]
    decision = evaluate_graph_enrich_config_rules(
        rules,
        {
            "edge": "calls",
            "source_evidence": "weak_call_resolver_ambiguous_short_name",
            "call_syntax": "attribute_call",
            "raw_target": "add",
        },
    )
    assert decision["matched"] is True
    assert decision["action"] == "drop"


def test_graph_enrich_config_rejects_unknown_rule_predicate(tmp_path):
    project = tmp_path / "generated-project"
    project.mkdir()
    payload = _predicate_rule_payload()
    payload["operations"][0]["when"]["all"].append(
        {"predicate": "execute_python", "value": "print('nope')"}
    )

    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=json.dumps(payload),
        mode="dry_run",
        project_root=project,
    )

    assert result["ok"] is False
    operation = result["gate"]["operations"][0]
    assert operation["status"] == "rejected"
    assert "predicate_unsupported" in operation["errors"]


def test_graph_enrich_config_rejects_underconstrained_weak_call_add_rule(tmp_path):
    project = tmp_path / "generated-project"
    project.mkdir()
    payload = _predicate_rule_payload()
    payload["operations"][0] = {
        "op": "review_rule",
        "rule_id": "weak_calls_add_short_name_in_reconcile",
        "edge": "calls",
        "source_evidence": "weak_call_resolver_ambiguous_short_name",
        "action": "downgrade",
        "downgrade_to": "drop",
        "confidence": 0.5,
        "when": {
            "all": [
                {"predicate": "raw_target_in", "values": ["add"]},
                {
                    "predicate": "source_evidence_is",
                    "value": "weak_call_resolver_ambiguous_short_name",
                },
            ]
        },
        "evidence": {
            "reason": "Dogfood regression: raw add alone also matches direct imported add().",
        },
    }

    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=json.dumps(payload),
        mode="dry_run",
        project_root=project,
    )

    assert result["ok"] is False
    operation = result["gate"]["operations"][0]
    assert operation["status"] == "rejected"
    assert "predicate_underconstrained_weak_call" in operation["errors"]


def test_graph_enrich_config_rejects_broad_string_literal_event_rule(tmp_path):
    project = tmp_path / "generated-project"
    project.mkdir()
    payload = _predicate_rule_payload()
    payload["operations"][0] = {
        "op": "tighten_rule",
        "rule_id": "emits_event.string_literal.executable_extensions",
        "edge": "emits_event",
        "source_evidence": "string_literal",
        "action": "ignore",
        "confidence": 0.7,
        "when": {
            "all": [
                {"predicate": "source_evidence_is", "value": "string_literal"},
                {"predicate": "language_is", "value": "python"},
            ]
        },
        "evidence": {
            "reason": "Dogfood regression: this suppresses every Python string-literal event.",
        },
    }

    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=json.dumps(payload),
        mode="dry_run",
        project_root=project,
    )

    assert result["ok"] is False
    operation = result["gate"]["operations"][0]
    assert operation["status"] == "rejected"
    assert "predicate_underconstrained_string_literal" in operation["errors"]


def test_graph_enrich_config_accepts_precise_string_literal_target_rule(tmp_path):
    project = tmp_path / "generated-project"
    project.mkdir()
    payload = _predicate_rule_payload()
    payload["operations"][0] = {
        "op": "tighten_rule",
        "rule_id": "emits_event.string_literal.cli_binary_filenames",
        "edge": "emits_event",
        "source_evidence": "string_literal",
        "action": "ignore",
        "confidence": 0.85,
        "when": {
            "all": [
                {"predicate": "source_evidence_is", "value": "string_literal"},
                {
                    "predicate": "raw_target_in",
                    "values": ["claude.cmd", "claude.exe", "codex.cmd", "codex.ps1"],
                },
            ]
        },
        "evidence": {
            "reason": "CLI binary filename candidates are not emitted runtime events.",
        },
    }

    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=json.dumps(payload),
        mode="dry_run",
        project_root=project,
    )

    assert result["ok"] is True
    rule = result["preview"]["graph_enrich_config_ops"]["rules"][
        "emits_event.string_literal.cli_binary_filenames"
    ]
    assert rule["when"]["all"][1]["values"] == [
        "claude_cmd",
        "claude_exe",
        "codex_cmd",
        "codex_ps1",
    ]


def test_graph_enrich_config_rejects_tighten_rule_that_allows_evidence(tmp_path):
    project = tmp_path / "generated-project"
    project.mkdir()
    payload = _predicate_rule_payload()
    payload["operations"][0] = {
        "op": "tighten_rule",
        "rule_id": "calls.policy_op.require_import_only_source_evidence",
        "edge": "calls",
        "source_evidence": "import_only",
        "action": "allow",
        "downgrade_to": "weak",
        "confidence": 0.5,
        "when": {
            "all": [
                {"predicate": "source_evidence_is", "value": "import_only"},
            ]
        },
        "evidence": {
            "reason": "Dogfood regression: tighten_rule must not allow import-only calls evidence.",
        },
    }

    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=json.dumps(payload),
        mode="dry_run",
        project_root=project,
    )

    assert result["ok"] is False
    operation = result["gate"]["operations"][0]
    assert operation["status"] == "rejected"
    assert "op_action_incompatible" in operation["errors"]
    assert result["preview"]["graph_enrich_config_ops"]["rules"] == {}


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
