"""Executable scenario contracts for source-hint graph projection.

They define the first implementation slice for source hints as graph structure
truth: scan hint blocks, project them onto derived graph structure, and track
withdrawn hints without introducing a user-facing patch state machine.
"""

from __future__ import annotations

def _future_api():
    from agent.governance.graph_hint_projection import (  # type: ignore[attr-defined]
        build_hint_projection,
        diff_hint_projection_state,
    )
    from agent.governance.graph_structure_hints import (  # type: ignore[attr-defined]
        scan_graph_structure_hints,
    )

    return scan_graph_structure_hints, build_hint_projection, diff_hint_projection_state


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
                    "test": [],
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


def test_hint_scanner_indexes_line_anchored_relation_blocks() -> None:
    scan_graph_structure_hints, _, _ = _future_api()

    files = {
        "agent/tests/test_parallel_branch_runtime.py": "\n".join(
            [
                "def test_checkpoint_refresh():",
                "    # aming-claw-hint:start id=gsh-runtime-tests op=add_edge edge=tests target=L7.runtime",
                "    # reason: covers checkpoint refresh and merge queue branch head behavior",
                "    # evidence: exercises queue_merge_item_for_branch_context",
                "    # aming-claw-hint:end",
                "    assert True",
            ]
        )
    }

    index = scan_graph_structure_hints(files)

    assert index["hint_count"] == 1
    hint = index["hints"][0]
    assert hint["hint_id"] == "gsh-runtime-tests"
    assert hint["op"] == "add_edge"
    assert hint["edge"] == "tests"
    assert hint["target_node_id"] == "L7.runtime"
    assert hint["source_path"] == "agent/tests/test_parallel_branch_runtime.py"
    assert hint["anchor"]["symbol"] == "test_checkpoint_refresh"
    assert hint["anchor"]["line_start"] == 2
    assert hint["status"] == "indexed"


def test_hint_scanner_ignores_quoted_hint_examples() -> None:
    scan_graph_structure_hints, _, _ = _future_api()

    index = scan_graph_structure_hints(
        {
            "agent/tests/test_graph_hint_projection.py": (
                "def test_example():\n"
                "    text = \"# aming-claw-hint:start id=example op=add_edge target=L7.fake\\n\"\n"
                "    assert text\n"
            )
        }
    )

    assert index["hint_count"] == 0


def test_projection_materializes_add_edge_hint_as_graph_truth() -> None:
    scan_graph_structure_hints, build_hint_projection, _ = _future_api()
    files = {
        "agent/tests/test_parallel_branch_runtime.py": (
            "# aming-claw-hint:start id=gsh-runtime-tests op=add_edge edge=tests target=L7.runtime\n"
            "# reason: this test file covers runtime behavior\n"
            "# aming-claw-hint:end\n"
        )
    }

    projection = build_hint_projection(_graph(), scan_graph_structure_hints(files))

    assert projection["status"] == "ok"
    assert projection["materialized_count"] == 1
    assert any(
        {
            "src": "agent/tests/test_parallel_branch_runtime.py",
            "dst": "L7.runtime",
            "edge_type": "tests",
            "direction": "source_hint",
            "source": "source_hint",
            "hint_id": "gsh-runtime-tests",
        }.items()
        <= edge.items()
        for edge in projection["graph"]["deps_graph"]["edges"]
    )
    assert projection["hint_states"]["gsh-runtime-tests"]["status"] == "materialized"


def test_suppress_edge_hint_overrides_inferred_relation() -> None:
    scan_graph_structure_hints, build_hint_projection, _ = _future_api()
    files = {
        "agent/tests/test_parallel_branch_runtime.py": (
            "# aming-claw-hint:start id=gsh-suppress-wrong-server op=suppress_edge edge=tests target=L7.server\n"
            "# reason: fan-in matched helper import, but the scenario tests runtime state transitions\n"
            "# aming-claw-hint:end\n"
            "# aming-claw-hint:start id=gsh-runtime-tests op=add_edge edge=tests target=L7.runtime\n"
            "# reason: source-local truth for the intended coverage edge\n"
            "# aming-claw-hint:end\n"
        )
    }

    projection = build_hint_projection(_graph(), scan_graph_structure_hints(files))
    edges = projection["graph"]["deps_graph"]["edges"]

    assert {
        "src": "agent/tests/test_parallel_branch_runtime.py",
        "dst": "L7.server",
        "edge_type": "tests",
        "source": "inferred_fan_in",
    } not in edges
    assert projection["hint_states"]["gsh-suppress-wrong-server"]["status"] == "materialized"
    assert projection["suppressed_edges"] == [
        {
            "src": "agent/tests/test_parallel_branch_runtime.py",
            "dst": "L7.server",
            "edge_type": "tests",
            "hint_id": "gsh-suppress-wrong-server",
        }
    ]


def test_move_file_hint_reassigns_ownership_without_patch_state_machine() -> None:
    scan_graph_structure_hints, build_hint_projection, _ = _future_api()
    graph = _graph()
    graph["deps_graph"]["nodes"][1]["test"] = ["agent/tests/test_parallel_branch_runtime.py"]
    files = {
        "agent/tests/test_parallel_branch_runtime.py": (
            "# aming-claw-hint:start id=gsh-move-runtime-test op=move_file role=test target=L7.runtime\n"
            "# reason: this file belongs to runtime, not server\n"
            "# aming-claw-hint:end\n"
        )
    }

    projection = build_hint_projection(graph, scan_graph_structure_hints(files))
    nodes = {node["id"]: node for node in projection["graph"]["deps_graph"]["nodes"]}

    assert nodes["L7.server"]["test"] == []
    assert nodes["L7.runtime"]["test"] == ["agent/tests/test_parallel_branch_runtime.py"]
    assert projection["hint_states"]["gsh-move-runtime-test"]["effect"] == {
        "files_moved": [
            {
                "path": "agent/tests/test_parallel_branch_runtime.py",
                "from_node_id": "L7.server",
                "to_node_id": "L7.runtime",
                "role": "test",
            }
        ]
    }


def test_missing_target_hint_is_conflict_and_does_not_mutate_graph() -> None:
    scan_graph_structure_hints, build_hint_projection, _ = _future_api()
    files = {
        "agent/tests/test_parallel_branch_runtime.py": (
            "# aming-claw-hint:start id=gsh-missing-target op=add_edge edge=tests target=L7.missing\n"
            "# reason: stale target from an old graph snapshot\n"
            "# aming-claw-hint:end\n"
        )
    }
    graph = _graph()

    projection = build_hint_projection(graph, scan_graph_structure_hints(files))

    assert projection["status"] == "conflict"
    assert projection["materialized_count"] == 0
    assert projection["graph"] == graph
    assert projection["hint_states"]["gsh-missing-target"]["status"] == "conflict"
    assert projection["hint_states"]["gsh-missing-target"]["last_error"] == "target_node_missing"


def test_deleted_hint_block_is_withdrawn_by_projection_state_diff() -> None:
    scan_graph_structure_hints, _, diff_hint_projection_state = _future_api()
    previous = scan_graph_structure_hints(
        {
            "agent/tests/test_parallel_branch_runtime.py": (
                "# aming-claw-hint:start id=gsh-runtime-tests op=add_edge edge=tests target=L7.runtime\n"
                "# reason: previous source truth\n"
                "# aming-claw-hint:end\n"
            )
        }
    )
    current = scan_graph_structure_hints({"agent/tests/test_parallel_branch_runtime.py": "# no hint\n"})

    diff = diff_hint_projection_state(previous, current, source_commit="H1", target_commit="H2")

    assert diff["withdrawn_hint_ids"] == ["gsh-runtime-tests"]
    assert diff["states"]["gsh-runtime-tests"] == {
        "status": "withdrawn",
        "source_commit": "H1",
        "target_commit": "H2",
        "rollback_action": "remove_materialized_effect",
    }
