"""Gate contracts for AI-produced graph_structure_ops.v1 output."""

from __future__ import annotations


def _future_api():
    from agent.governance.graph_structure_ops import validate_graph_structure_ops

    return validate_graph_structure_ops


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
        "source": {"snapshot_id": "scope-current", "base_commit": "abc123"},
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
    assert report["operations"][0]["status"] == "rejected"
    assert report["operations"][0]["errors"] == ["unsupported_op_for_hint_materialization"]


def test_graph_structure_ops_gate_requires_real_snapshot_targets_and_self_check() -> None:
    validate_graph_structure_ops = _future_api()
    payload = {
        "schema_version": "graph_structure_ops.v1",
        "source": {"snapshot_id": "wrong-snapshot", "base_commit": "abc123"},
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
        "source": {"snapshot_id": "scope-current", "base_commit": "abc123"},
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
