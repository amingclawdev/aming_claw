"""Shared graph interpretation contracts for query and AI context payloads."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


GRAPH_DIRECTION_CONTRACT: dict[str, Any] = {
    "schema_version": "graph_direction_contract.v1",
    "deps_graph": {
        "depends_on": {
            "direction": "dependency_to_dependent",
            "interpretation": (
                "If module A imports or calls module B, B is the dependency "
                "and A is the dependent, so the deps_graph edge is B -> A."
            ),
            "source_role": "dependency_provider_prerequisite",
            "target_role": "dependent_consumer_impacted_by_source_change",
            "fan_out_meaning": "dependents/impact surface for dependency edges",
            "fan_in_meaning": "upstream prerequisites for dependency edges",
            "ai_review_rule": (
                "Do not flag B -> A as reversed when evidence says A imports "
                "or calls B; this is the canonical deps_graph direction."
            ),
        },
        "typed_asset_relations": {
            "producer_to_asset": [
                "emits_event",
                "owns_state",
                "writes_artifact",
                "writes_state",
            ],
            "asset_to_consumer": [
                "configures_analyzer",
                "configures_model_routing",
                "configures_role",
                "configures_runtime",
                "consumes_event",
                "creates_task",
                "http_route",
                "reads_artifact",
                "reads_state",
                "uses_task_metadata",
            ],
        },
    },
    "function_facts": {
        "function_calls_direction": "caller_to_callee",
        "function_called_by_direction": "callee_indexed_by_inbound_callers",
        "note": (
            "Function fact direction is source-code call direction and can be "
            "opposite to the collapsed deps_graph depends_on direction."
        ),
    },
}


def graph_direction_contract() -> dict[str, Any]:
    """Return a detached copy so callers can safely attach it to payloads."""
    return deepcopy(GRAPH_DIRECTION_CONTRACT)
