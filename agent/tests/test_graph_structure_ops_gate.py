"""Gate contracts for AI-produced graph_structure_ops.v1 output."""

from __future__ import annotations

from pathlib import Path


def _future_api():
    from agent.governance.graph_structure_ops import validate_graph_structure_ops

    return validate_graph_structure_ops


def test_graph_structure_ai_output_parser_accepts_only_one_json_object() -> None:
    from agent.governance.graph_structure_ops import parse_graph_structure_ai_output

    parsed = parse_graph_structure_ai_output('{"schema_version":"graph_structure_ops.v1","operations":[]}')
    assert parsed["ok"] is True
    assert parsed["payload"]["schema_version"] == "graph_structure_ops.v1"

    assert parse_graph_structure_ai_output("not json")["errors"] == ["ai_output_json_invalid"]
    assert parse_graph_structure_ai_output("[]")["errors"] == ["ai_output_not_object"]
    assert parse_graph_structure_ai_output('{"a": 1}\ntrailing')["errors"] == ["ai_output_extra_content"]


def test_graph_structure_ai_output_pipeline_dry_run_and_accept_requires_project_root() -> None:
    import json

    from agent.governance.graph_structure_ops import run_graph_structure_ai_output_pipeline

    payload = {
        "schema_version": "graph_structure_ops.v1",
        "source": {
            "snapshot_id": "snap",
            "base_commit": "head",
            "analyzer_role": "reconcile_graph_structure_analyzer",
        },
        "operations": [
            {
                "op": "add_edge",
                "hint_id": "pipeline-edge",
                "source_path": "agent/tests/test_service.py",
                "target_node_id": "L7.service",
                "edge": "tests",
            }
        ],
        "self_check": {"valid": True, "checked_rules": ["json", "snapshot"]},
    }
    graph = {"deps_graph": {"nodes": [{"id": "L7.service"}], "edges": []}}

    dry_run = run_graph_structure_ai_output_pipeline(
        raw_output=json.dumps(payload),
        mode="dry_run",
        graph=graph,
        inventory_paths=["agent/tests/test_service.py"],
        snapshot_id="snap",
        base_commit="head",
    )
    assert dry_run["ok"] is True
    assert dry_run["projection"]["effect_counts"]["edges_added"] == 1

    accept = run_graph_structure_ai_output_pipeline(
        raw_output=json.dumps(payload),
        mode="accept",
        graph=graph,
        inventory_paths=["agent/tests/test_service.py"],
        snapshot_id="snap",
        base_commit="head",
    )
    assert accept["ok"] is False
    assert accept["errors"] == ["project_root_required"]


def _graph():
    return {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L7.runtime",
                    "layer": "L7",
                    "title": "agent.governance.parallel_branch_runtime",
                    "primary": ["agent/governance/parallel_branch_runtime.py"],
                    "test": [],
                    "metadata": {"module": "agent.governance.parallel_branch_runtime"},
                },
                {
                    "id": "L7.server",
                    "layer": "L7",
                    "title": "agent.governance.server",
                    "primary": ["agent/governance/server.py"],
                    "test": ["agent/tests/test_parallel_branch_runtime.py"],
                    "metadata": {"module": "agent.governance.server"},
                },
            ],
            "edges": [
                {
                    "src": "agent/tests/test_parallel_branch_runtime.py",
                    "dst": "L7.server",
                    "edge_type": "tests",
                    "source": "inferred_fan_in",
                }
            ],
        }
    }


def _inventory_paths() -> set[str]:
    return {
        "agent/governance/parallel_branch_runtime.py",
        "agent/governance/server.py",
        "agent/tests/test_parallel_branch_runtime.py",
    }


def test_valid_graph_structure_ops_gate_emits_hint_compatible_operations() -> None:
    validate_graph_structure_ops = _future_api()
    payload = {
        "schema_version": "graph_structure_ops.v1",
        "source": {
            "snapshot_id": "scope-current",
            "base_commit": "abc123",
            "analyzer_role": "reconcile_graph_structure_analyzer",
        },
        "operations": [
            {
                "op": "move_file",
                "hint_id": "gsh-move-runtime-test",
                "source_path": "agent/tests/test_parallel_branch_runtime.py",
                "target_node_id": "L7.runtime",
                "role": "test",
                "confidence": 0.91,
                "evidence": {"reason": "runtime branch recovery tests cover the runtime module"},
            },
            {
                "op": "suppress_edge",
                "hint_id": "gsh-suppress-server-test",
                "source_path": "agent/tests/test_parallel_branch_runtime.py",
                "target_node_id": "L7.server",
                "edge": "tests",
                "confidence": 0.88,
                "evidence": {"reason": "fan-in matched helper import only"},
            },
            {
                "op": "add_edge",
                "hint_id": "gsh-runtime-test-edge",
                "source_path": "agent/tests/test_parallel_branch_runtime.py",
                "target_node_id": "L7.runtime",
                "edge": "tests",
                "confidence": 0.9,
                "evidence": {"reason": "asserts runtime checkpoint and recovery behavior"},
            },
        ],
        "self_check": {
            "valid": True,
            "checked_rules": [
                "schema_version",
                "op_supported",
                "target_node_exists",
                "source_path_exists",
                "role_allowed",
                "edge_allowed",
            ],
            "known_risks": [],
        },
    }

    report = validate_graph_structure_ops(
        payload,
        graph=_graph(),
        inventory_paths=_inventory_paths(),
        snapshot_id="scope-current",
        base_commit="abc123",
    )

    assert report["ok"] is True
    assert report["status"] == "passed"
    assert report["precheck"]["status"] == "passed"
    assert report["precheck"]["classification"] == "passed"
    assert report["accepted_count"] == 3
    assert report["rejected_count"] == 0
    assert report["normalized_hint_index"] == {
        "hint_count": 3,
        "hints": [
            {
                "hint_id": "gsh-move-runtime-test",
                "op": "move_file",
                "edge": "",
                "role": "test",
                "target_node_id": "L7.runtime",
                "source_path": "agent/tests/test_parallel_branch_runtime.py",
                "reason": "runtime branch recovery tests cover the runtime module",
                "evidence": "",
                "anchor": {"symbol": "", "line_start": 0, "line_end": 0},
                "status": "accepted",
            },
            {
                "hint_id": "gsh-suppress-server-test",
                "op": "suppress_edge",
                "edge": "tests",
                "role": "",
                "target_node_id": "L7.server",
                "source_path": "agent/tests/test_parallel_branch_runtime.py",
                "reason": "fan-in matched helper import only",
                "evidence": "",
                "anchor": {"symbol": "", "line_start": 0, "line_end": 0},
                "status": "accepted",
            },
            {
                "hint_id": "gsh-runtime-test-edge",
                "op": "add_edge",
                "edge": "tests",
                "role": "",
                "target_node_id": "L7.runtime",
                "source_path": "agent/tests/test_parallel_branch_runtime.py",
                "reason": "asserts runtime checkpoint and recovery behavior",
                "evidence": "",
                "anchor": {"symbol": "", "line_start": 0, "line_end": 0},
                "status": "accepted",
            },
        ],
    }


def test_graph_structure_ops_gate_rejects_unsupported_structural_ops() -> None:
    validate_graph_structure_ops = _future_api()
    payload = {
        "schema_version": "graph_structure_ops.v1",
        "source": {
            "snapshot_id": "scope-current",
            "base_commit": "abc123",
            "analyzer_role": "reconcile_graph_structure_analyzer",
        },
        "operations": [
            {
                "op": "merge_nodes",
                "hint_id": "gsh-merge-runtime-server",
                "source_node_ids": ["L7.runtime", "L7.server"],
                "target_node_id": "L7.runtime",
                "confidence": 0.72,
            }
        ],
        "self_check": {"valid": True, "checked_rules": ["op_supported"], "known_risks": []},
    }

    report = validate_graph_structure_ops(
        payload,
        graph=_graph(),
        inventory_paths=_inventory_paths(),
        snapshot_id="scope-current",
        base_commit="abc123",
    )

    assert report["ok"] is False
    assert report["status"] == "failed"
    assert report["accepted_count"] == 0
    assert report["rejected_count"] == 1
    assert report["precheck"]["classification"] == "model_repairable"
    assert report["precheck"]["retryable"] is True
    assert report["precheck"]["recommended_action"] == "retry_ai_repair_once"
    assert report["operations"][0]["status"] == "rejected"
    assert report["operations"][0]["errors"] == ["unsupported_op_for_hint_materialization"]


def test_graph_structure_ops_gate_requires_real_snapshot_targets_and_self_check() -> None:
    validate_graph_structure_ops = _future_api()
    payload = {
        "schema_version": "graph_structure_ops.v1",
        "source": {
            "snapshot_id": "wrong-snapshot",
            "base_commit": "abc123",
            "analyzer_role": "reconcile_node_semantic_analyzer",
        },
        "operations": [
            {
                "op": "move_file",
                "hint_id": "bad id with spaces",
                "source_path": "agent/tests/missing.py",
                "target_node_id": "L7.missing",
                "role": "banana",
                "confidence": 1.3,
            }
        ],
        "self_check": {"valid": False, "checked_rules": [], "known_risks": ["guessed target"]},
    }

    report = validate_graph_structure_ops(
        payload,
        graph=_graph(),
        inventory_paths=_inventory_paths(),
        snapshot_id="scope-current",
        base_commit="abc123",
    )
    errors = report["operations"][0]["errors"]

    assert report["ok"] is False
    assert "source_snapshot_mismatch" in report["errors"]
    assert "source_analyzer_role_invalid" in report["errors"]
    assert "self_check_invalid" in report["errors"]
    assert "hint_id_invalid" in errors
    assert "source_path_missing" in errors
    assert "target_node_missing" in errors
    assert "role_unsupported" in errors
    assert "confidence_out_of_range" in errors


def test_graph_structure_ops_gate_detects_conflicting_operations() -> None:
    validate_graph_structure_ops = _future_api()
    payload = {
        "schema_version": "graph_structure_ops.v1",
        "source": {
            "snapshot_id": "scope-current",
            "base_commit": "abc123",
            "analyzer_role": "reconcile_graph_structure_analyzer",
        },
        "operations": [
            {
                "op": "move_file",
                "hint_id": "gsh-move-test-runtime",
                "source_path": "agent/tests/test_parallel_branch_runtime.py",
                "target_node_id": "L7.runtime",
                "role": "test",
                "confidence": 0.8,
            },
            {
                "op": "move_file",
                "hint_id": "gsh-move-test-server",
                "source_path": "agent/tests/test_parallel_branch_runtime.py",
                "target_node_id": "L7.server",
                "role": "test",
                "confidence": 0.8,
            },
            {
                "op": "add_edge",
                "hint_id": "gsh-add-server-test",
                "source_path": "agent/tests/test_parallel_branch_runtime.py",
                "target_node_id": "L7.server",
                "edge": "tests",
                "confidence": 0.8,
            },
            {
                "op": "suppress_edge",
                "hint_id": "gsh-suppress-server-test",
                "source_path": "agent/tests/test_parallel_branch_runtime.py",
                "target_node_id": "L7.server",
                "edge": "tests",
                "confidence": 0.8,
            },
        ],
        "self_check": {"valid": True, "checked_rules": ["conflict_check"], "known_risks": []},
    }

    report = validate_graph_structure_ops(
        payload,
        graph=_graph(),
        inventory_paths=_inventory_paths(),
        snapshot_id="scope-current",
        base_commit="abc123",
    )

    assert report["ok"] is False
    assert report["conflict_count"] == 2
    assert {
        "conflicting_move_file_target",
        "conflicting_edge_add_suppress",
    } <= set(report["errors"])


def test_graph_structure_ops_gate_uses_configured_operation_contract() -> None:
    validate_graph_structure_ops = _future_api()
    payload = {
        "schema_version": "graph_structure_ops.v1",
        "source": {
            "snapshot_id": "scope-current",
            "base_commit": "abc123",
            "analyzer_role": "reconcile_graph_structure_analyzer",
        },
        "operations": [
            {
                "op": "add_edge",
                "hint_id": "gsh-import-edge",
                "source_path": "agent/tests/test_parallel_branch_runtime.py",
                "target_node_id": "L7.runtime",
                "edge": "imports",
                "confidence": 0.8,
            },
            {
                "op": "suppress_edge",
                "hint_id": "gsh-suppress-disabled",
                "source_path": "agent/tests/test_parallel_branch_runtime.py",
                "target_node_id": "L7.server",
                "edge": "tests",
                "confidence": 0.8,
            },
        ],
        "self_check": {"valid": True, "checked_rules": ["configured_contract"], "known_risks": []},
    }
    contract = {
        "schema_version": "graph_structure_ops.v1",
        "analyzer_role": "reconcile_graph_structure_analyzer",
        "operations": {
            "add_edge": {"enabled": True, "edge_allowlist": ["tests"]},
            "suppress_edge": {"enabled": False},
        },
    }

    report = validate_graph_structure_ops(
        payload,
        graph=_graph(),
        inventory_paths=_inventory_paths(),
        snapshot_id="scope-current",
        base_commit="abc123",
        operation_contract=contract,
    )

    assert report["ok"] is False
    assert report["operation_contract"]["supported_operations"] == ["add_edge", "move_file"]
    assert report["operations"][0]["errors"] == ["edge_unsupported"]
    assert report["operations"][1]["errors"] == ["unsupported_op_for_hint_materialization"]


def test_graph_structure_ops_gate_deduplicates_repeated_edges_before_projection() -> None:
    validate_graph_structure_ops = _future_api()
    payload = {
        "schema_version": "graph_structure_ops.v1",
        "source": {
            "snapshot_id": "scope-current",
            "base_commit": "abc123",
            "analyzer_role": "reconcile_graph_structure_analyzer",
        },
        "operations": [
            {
                "op": "add_edge",
                "hint_id": "gsh-runtime-test-edge-1",
                "source_path": "agent/tests/test_parallel_branch_runtime.py",
                "target_node_id": "L7.runtime",
                "edge": "tests",
                "confidence": 0.91,
                "evidence": {"reason": "same generated test binding"},
            },
            {
                "op": "add_edge",
                "hint_id": "gsh-runtime-test-edge-2",
                "source_path": "agent/tests/test_parallel_branch_runtime.py",
                "target_node_id": "L7.runtime",
                "edge": "tests",
                "confidence": 0.92,
                "evidence": {"reason": "same generated test binding repeated"},
            },
        ],
        "self_check": {"valid": True, "checked_rules": ["dedupe"], "known_risks": []},
    }

    report = validate_graph_structure_ops(
        payload,
        graph=_graph(),
        inventory_paths=_inventory_paths(),
        snapshot_id="scope-current",
        base_commit="abc123",
    )

    assert report["ok"] is True
    assert report["accepted_count"] == 1
    assert report["deduped_count"] == 1
    assert report["rejected_count"] == 0
    assert report["operations"][0]["status"] == "accepted"
    assert report["operations"][1]["status"] == "deduped"
    assert report["operations"][1]["duplicate_of"] == "gsh-runtime-test-edge-1"
    assert report["normalized_hint_index"]["hint_count"] == 1


def test_graph_structure_ops_gate_downgrades_import_only_calls_with_project_config(
    tmp_path: Path,
) -> None:
    validate_graph_structure_ops = _future_api()
    source = tmp_path / "agent" / "consumer.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from agent.governance.parallel_branch_runtime import BranchTaskRuntimeContext\n\n"
        "def describe(context: BranchTaskRuntimeContext) -> str:\n"
        "    return context.task_id\n",
        encoding="utf-8",
    )
    payload = {
        "schema_version": "graph_structure_ops.v1",
        "source": {
            "snapshot_id": "scope-current",
            "base_commit": "abc123",
            "analyzer_role": "reconcile_graph_structure_analyzer",
        },
        "operations": [
            {
                "op": "add_edge",
                "hint_id": "gsh-import-only-call",
                "source_path": "agent/consumer.py",
                "target_node_id": "L7.runtime",
                "edge": "calls",
                "confidence": 0.91,
                "evidence": {"reason": "import-only type annotation should not become calls"},
            }
        ],
        "self_check": {"valid": True, "checked_rules": ["call_evidence"], "known_risks": []},
    }
    contract = {
        "schema_version": "graph_structure_ops.v1",
        "analyzer_role": "reconcile_graph_structure_analyzer",
        "evidence_policy": {
            "dedupe_operations": True,
            "calls": {
                "require_call_evidence": True,
                "import_only_action": "downgrade",
                "downgrade_to": "imports",
            },
        },
    }

    report = validate_graph_structure_ops(
        payload,
        graph=_graph(),
        inventory_paths={*_inventory_paths(), "agent/consumer.py"},
        snapshot_id="scope-current",
        base_commit="abc123",
        operation_contract=contract,
        project_root=tmp_path,
    )

    assert report["ok"] is True
    assert report["accepted_count"] == 1
    assert report["operations"][0]["edge"] == "imports"
    assert report["operations"][0]["normalizations"] == [
        "calls_import_only_downgraded_to_imports"
    ]
    assert report["normalized_hint_index"]["hints"][0]["edge"] == "imports"


def test_graph_structure_ops_gate_keeps_calls_when_imported_symbol_is_called(
    tmp_path: Path,
) -> None:
    validate_graph_structure_ops = _future_api()
    source = tmp_path / "agent" / "consumer.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from agent.governance.parallel_branch_runtime import branch_context_from_chain_stage\n\n"
        "def build(payload):\n"
        "    return branch_context_from_chain_stage(payload)\n",
        encoding="utf-8",
    )
    payload = {
        "schema_version": "graph_structure_ops.v1",
        "source": {
            "snapshot_id": "scope-current",
            "base_commit": "abc123",
            "analyzer_role": "reconcile_graph_structure_analyzer",
        },
        "operations": [
            {
                "op": "add_edge",
                "hint_id": "gsh-real-call",
                "source_path": "agent/consumer.py",
                "target_node_id": "L7.runtime",
                "edge": "calls",
                "confidence": 0.91,
            }
        ],
        "self_check": {"valid": True, "checked_rules": ["call_evidence"], "known_risks": []},
    }

    report = validate_graph_structure_ops(
        payload,
        graph=_graph(),
        inventory_paths={*_inventory_paths(), "agent/consumer.py"},
        snapshot_id="scope-current",
        base_commit="abc123",
        project_root=tmp_path,
    )

    assert report["ok"] is True
    assert report["operations"][0]["edge"] == "calls"
    assert report["operations"][0]["normalizations"] == []
    assert report["normalized_hint_index"]["hints"][0]["edge"] == "calls"


def test_graph_structure_ops_gate_rejects_calls_without_concrete_evidence() -> None:
    validate_graph_structure_ops = _future_api()
    payload = {
        "schema_version": "graph_structure_ops.v1",
        "source": {
            "snapshot_id": "scope-current",
            "base_commit": "abc123",
            "analyzer_role": "reconcile_graph_structure_analyzer",
        },
        "operations": [
            {
                "op": "add_edge",
                "hint_id": "gsh-weak-call",
                "source_path": "agent/governance/server.py",
                "target_node_id": "L7.runtime",
                "edge": "calls",
                "confidence": 0.67,
                "evidence": {"reason": "AI inferred a possible call without line evidence"},
            }
        ],
        "self_check": {"valid": True, "checked_rules": ["call_evidence"], "known_risks": []},
    }

    report = validate_graph_structure_ops(
        payload,
        graph=_graph(),
        inventory_paths=_inventory_paths(),
        snapshot_id="scope-current",
        base_commit="abc123",
    )

    assert report["ok"] is False
    assert report["accepted_count"] == 0
    assert report["rejected_count"] == 1
    assert report["precheck"]["classification"] == "policy_rejected"
    assert report["precheck"]["retryable"] is False
    assert report["precheck"]["recommended_action"] == "observer_review_required"
    assert report["operations"][0]["errors"] == ["calls_evidence_missing"]


def test_graph_structure_ops_gate_rejects_calls_self_edges() -> None:
    validate_graph_structure_ops = _future_api()
    payload = {
        "schema_version": "graph_structure_ops.v1",
        "source": {
            "snapshot_id": "scope-current",
            "base_commit": "abc123",
            "analyzer_role": "reconcile_graph_structure_analyzer",
        },
        "operations": [
            {
                "op": "add_edge",
                "hint_id": "gsh-self-call",
                "source_path": "agent/governance/parallel_branch_runtime.py",
                "target_node_id": "L7.runtime",
                "edge": "calls",
                "confidence": 0.91,
                "evidence": {
                    "source_evidence": "function_call",
                    "reason": "AI found an internal helper call",
                    "evidence": "parallel_branch_runtime.py:42 calls helper()",
                },
            }
        ],
        "self_check": {"valid": True, "checked_rules": ["call_evidence"], "known_risks": []},
    }

    report = validate_graph_structure_ops(
        payload,
        graph=_graph(),
        inventory_paths=_inventory_paths(),
        snapshot_id="scope-current",
        base_commit="abc123",
    )

    assert report["ok"] is False
    assert report["precheck"]["classification"] == "policy_rejected"
    assert report["precheck"]["retryable"] is False
    assert report["operations"][0]["errors"] == ["calls_self_edge"]
    assert report["operations"][0]["hint"]["reason"] == "AI found an internal helper call"
    assert report["operations"][0]["hint"]["evidence"] == (
        "parallel_branch_runtime.py:42 calls helper()"
    )
