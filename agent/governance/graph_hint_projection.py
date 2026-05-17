"""Project source hint indexes into derived graph structure."""

from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping


def build_hint_projection(graph: Mapping[str, Any], hint_index: Mapping[str, Any]) -> dict[str, Any]:
    """Apply indexed source hints to an in-memory graph copy."""
    graph_copy = copy.deepcopy(dict(graph))
    nodes = _nodes(graph_copy)
    nodes_by_id = {str(node.get("id") or ""): node for node in nodes}
    edges = _edges(graph_copy)
    hint_states: dict[str, dict[str, Any]] = {}
    suppressed_edges: list[dict[str, str]] = []
    materialized_count = 0
    conflict_count = 0

    for hint in list(hint_index.get("hints") or []):
        hint_id = str(hint.get("hint_id") or "")
        op = str(hint.get("op") or "")
        target_node_id = str(hint.get("target_node_id") or "")
        source_path = str(hint.get("source_path") or "")
        edge_type = str(hint.get("edge") or "")
        role = str(hint.get("role") or "")
        state = {
            "status": "materialized",
            "source_path": source_path,
            "target_node_id": target_node_id,
            "effect": {},
            "last_error": "",
        }

        if target_node_id and target_node_id not in nodes_by_id:
            conflict_count += 1
            state["status"] = "conflict"
            state["last_error"] = "target_node_missing"
            hint_states[hint_id] = state
            continue

        if op == "add_edge":
            _add_hint_edge(edges, source_path, target_node_id, edge_type, hint_id)
            state["effect"] = {
                "edges_added": [
                    {
                        "src": source_path,
                        "dst": target_node_id,
                        "edge_type": edge_type,
                    }
                ]
            }
            materialized_count += 1
        elif op == "suppress_edge":
            removed = _suppress_edges(edges, source_path, target_node_id, edge_type)
            suppressed_edges.extend(
                {
                    "src": str(edge.get("src") or ""),
                    "dst": str(edge.get("dst") or ""),
                    "edge_type": str(edge.get("edge_type") or ""),
                    "hint_id": hint_id,
                }
                for edge in removed
            )
            state["effect"] = {"edges_suppressed": suppressed_edges[-len(removed):] if removed else []}
            materialized_count += 1
        elif op == "move_file":
            moved = _move_file(nodes, source_path, target_node_id, role)
            state["effect"] = {"files_moved": moved}
            materialized_count += 1
        else:
            conflict_count += 1
            state["status"] = "conflict"
            state["last_error"] = "unsupported_op"
        hint_states[hint_id] = state

    return {
        "status": "conflict" if conflict_count else "ok",
        "materialized_count": materialized_count,
        "conflict_count": conflict_count,
        "graph": graph if conflict_count else graph_copy,
        "hint_states": hint_states,
        "suppressed_edges": suppressed_edges,
    }


def diff_hint_projection_state(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    source_commit: str = "",
    target_commit: str = "",
) -> dict[str, Any]:
    previous_ids = _hint_ids(previous.get("hints") or [])
    current_ids = _hint_ids(current.get("hints") or [])
    withdrawn = sorted(previous_ids - current_ids)
    states = {
        hint_id: {
            "status": "withdrawn",
            "source_commit": source_commit,
            "target_commit": target_commit,
            "rollback_action": "remove_materialized_effect",
        }
        for hint_id in withdrawn
    }
    return {
        "withdrawn_hint_ids": withdrawn,
        "states": states,
        "source_commit": source_commit,
        "target_commit": target_commit,
    }


def _nodes(graph: dict[str, Any]) -> list[dict[str, Any]]:
    deps_graph = graph.setdefault("deps_graph", {})
    nodes = deps_graph.setdefault("nodes", [])
    return nodes if isinstance(nodes, list) else []


def _edges(graph: dict[str, Any]) -> list[dict[str, Any]]:
    deps_graph = graph.setdefault("deps_graph", {})
    edges = deps_graph.setdefault("edges", [])
    return edges if isinstance(edges, list) else []


def _hint_ids(hints: Iterable[Any]) -> set[str]:
    return {str(hint.get("hint_id") or "") for hint in hints if str(hint.get("hint_id") or "")}


def _add_hint_edge(
    edges: list[dict[str, Any]],
    source_path: str,
    target_node_id: str,
    edge_type: str,
    hint_id: str,
) -> None:
    candidate = {
        "src": source_path,
        "dst": target_node_id,
        "edge_type": edge_type,
        "source": "source_hint",
        "hint_id": hint_id,
    }
    if candidate not in edges:
        edges.append(candidate)


def _suppress_edges(
    edges: list[dict[str, Any]],
    source_path: str,
    target_node_id: str,
    edge_type: str,
) -> list[dict[str, Any]]:
    removed: list[dict[str, Any]] = []
    keep: list[dict[str, Any]] = []
    for edge in edges:
        if (
            str(edge.get("src") or "") == source_path
            and str(edge.get("dst") or "") == target_node_id
            and str(edge.get("edge_type") or "") == edge_type
        ):
            removed.append(edge)
        else:
            keep.append(edge)
    edges[:] = keep
    return removed


def _move_file(
    nodes: list[dict[str, Any]],
    source_path: str,
    target_node_id: str,
    role: str,
) -> list[dict[str, str]]:
    moved: list[dict[str, str]] = []
    for node in nodes:
        node_id = str(node.get("id") or "")
        values = node.get(role)
        if not isinstance(values, list) or source_path not in values:
            continue
        node[role] = [value for value in values if value != source_path]
        if node_id != target_node_id:
            moved.append(
                {
                    "path": source_path,
                    "from_node_id": node_id,
                    "to_node_id": target_node_id,
                    "role": role,
                }
            )
    target = next((node for node in nodes if str(node.get("id") or "") == target_node_id), None)
    if target is not None:
        values = target.get(role)
        if not isinstance(values, list):
            values = []
        if source_path not in values:
            values.append(source_path)
        target[role] = sorted(values)
    return moved
