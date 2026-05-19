"""Read-model helpers for clustering graph/enrich config proposals."""

from __future__ import annotations

import copy
import json
import re
from collections import defaultdict
from typing import Any, Iterable, Mapping


TEST_IMPORT_FANIN_DIRECT_SYMBOL_RULE = "tests.test_import_fanin.require_direct_symbol_import"


def normalize_rule_id(rule_id: Any) -> str:
    """Normalize AI rule id spelling without discarding review-facing detail."""

    tokens = _rule_tokens(rule_id)
    if _is_test_import_fanin_direct_symbol_tokens(tokens):
        return TEST_IMPORT_FANIN_DIRECT_SYMBOL_RULE
    return ".".join(tokens)


def aggregate_graph_enrich_config_proposals(
    events: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate accepted graph_enrich_config operations into review clusters.

    The input is intentionally shaped like graph_events payload excerpts so the
    same helper can index live DB events and fixture data.
    """

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for operation in _iter_accepted_operations(events):
        family = _issue_family(operation)
        canonical_rule_id = _canonical_rule_id(family, operation)
        grouped[(family, canonical_rule_id)].append(operation)

    clusters = [
        _build_cluster(issue_family, canonical_rule_id, operations)
        for (issue_family, canonical_rule_id), operations in grouped.items()
    ]
    return sorted(clusters, key=lambda item: item["cluster_id"])


def _iter_accepted_operations(
    events: Iterable[Mapping[str, Any]],
) -> Iterable[dict[str, Any]]:
    for event_order, event in enumerate(events):
        operations = event.get("operations") if isinstance(event.get("operations"), list) else []
        for operation_index, raw_operation in enumerate(operations):
            if not isinstance(raw_operation, Mapping):
                continue
            if str(raw_operation.get("status") or "") != "accepted":
                continue
            operation = copy.deepcopy(dict(raw_operation))
            operation["event_id"] = str(event.get("event_id") or "")
            operation["target_id"] = str(event.get("target_id") or "")
            operation["event_status"] = str(event.get("status") or "")
            operation["event_precheck"] = str(event.get("precheck") or "")
            operation["operation_index"] = int(raw_operation.get("index", operation_index) or 0)
            operation["_proposal_order"] = (event_order, operation_index)
            yield operation


def _build_cluster(
    issue_family: str,
    canonical_rule_id: str,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    selected = max(operations, key=_selection_score)
    selected_operation = _public_operation(selected)
    public_operations = [_public_operation(operation) for operation in operations]
    support_event_ids = _stable_unique(operation["event_id"] for operation in operations)
    support_rule_ids = _stable_unique(str(operation.get("rule_id") or "") for operation in operations)
    signatures = {
        (
            str(operation.get("op") or ""),
            str(operation.get("edge") or ""),
            str(operation.get("source_evidence") or ""),
            str(operation.get("action") or ""),
            str(operation.get("downgrade_to") or ""),
            json.dumps(operation.get("when") or {}, sort_keys=True),
        )
        for operation in operations
    }
    variant_count = max(0, len(signatures) - 1)
    status = "clustered_with_variants" if variant_count else "clustered"
    return {
        "cluster_id": f"graph_enrich_config:{issue_family}:{canonical_rule_id}",
        "issue_family": issue_family,
        "canonical_rule_id": canonical_rule_id,
        "target_scope": "project_config",
        "status": status,
        "operation_count": len(operations),
        "raw_event_count": len(support_event_ids),
        "support_event_ids": support_event_ids,
        "support_rule_ids": support_rule_ids,
        "selected_event_id": selected_operation["event_id"],
        "selected_operation": selected_operation,
        "operations": public_operations,
        "variant_count": variant_count,
    }


def _selection_score(operation: Mapping[str, Any]) -> tuple[int, int, int, int, int, int, int, int]:
    rule_id = str(operation.get("rule_id") or "")
    when = operation.get("when") if isinstance(operation.get("when"), Mapping) else {}
    has_raw_target = _has_predicate(when, "raw_target_in")
    event_order, operation_index = operation.get("_proposal_order", (0, 0))
    return (
        1 if normalize_rule_id(rule_id) == TEST_IMPORT_FANIN_DIRECT_SYMBOL_RULE else 0,
        1 if rule_id == TEST_IMPORT_FANIN_DIRECT_SYMBOL_RULE else 0,
        1 if str(operation.get("op") or "") == "tighten_rule" else 0,
        1 if str(operation.get("downgrade_to") or "") == "weak_tests" else 0,
        1 if _has_predicate(when, "source_evidence_is") else 0,
        0 if has_raw_target else 1,
        -int(event_order),
        -int(operation_index),
    )


def _public_operation(operation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value)
        for key, value in operation.items()
        if not str(key).startswith("_")
    }


def _issue_family(operation: Mapping[str, Any]) -> str:
    edge = _normalized_token(operation.get("edge"))
    source_evidence = _normalized_token(operation.get("source_evidence"))
    action = _normalized_token(operation.get("action"))
    if (
        edge == "tests"
        and source_evidence == "test_import_fanin"
        and action == "require_direct_symbol_import"
    ):
        return "test_import_fanin_direct_symbol_gate"
    if source_evidence == "string_literal" and edge in {"emits_event", "consumes_event"}:
        return "string_literal_event_false_positive"
    if source_evidence == "string_literal" and edge == "documents":
        return "string_literal_document_artifact_false_positive"
    if edge == "calls" and source_evidence.startswith("weak_call_resolver"):
        return "weak_call_ambiguous_short_name"
    if edge == "documents" and source_evidence == "import_only":
        return "import_only_document_binding_review"
    return ".".join(
        token for token in (source_evidence, edge, action) if token
    ) or "unknown_graph_enrich_config_proposal"


def _canonical_rule_id(issue_family: str, operation: Mapping[str, Any]) -> str:
    if issue_family == "test_import_fanin_direct_symbol_gate":
        return TEST_IMPORT_FANIN_DIRECT_SYMBOL_RULE
    if issue_family == "string_literal_event_false_positive":
        return "string_literal.event_false_positive"
    if issue_family == "string_literal_document_artifact_false_positive":
        return "string_literal.document_artifact_false_positive"
    if issue_family == "weak_call_ambiguous_short_name":
        return "weak_call_resolver.ambiguous_short_name"
    if issue_family == "import_only_document_binding_review":
        return "import_only.document_binding_review"
    return normalize_rule_id(operation.get("rule_id"))


def _has_predicate(when: Mapping[str, Any], predicate: str) -> bool:
    checks = when.get("all") if isinstance(when.get("all"), list) else []
    return any(
        isinstance(check, Mapping) and check.get("predicate") == predicate
        for check in checks
    )


def _rule_tokens(rule_id: Any) -> list[str]:
    raw = str(rule_id or "").strip().lower()
    return [token for token in re.split(r"[^a-z0-9]+", raw.replace("_", ".")) if token]


def _is_test_import_fanin_direct_symbol_tokens(tokens: list[str]) -> bool:
    token_set = set(tokens)
    return {
        "test",
        "import",
        "fanin",
        "require",
        "direct",
        "symbol",
    }.issubset(token_set)


def _normalized_token(value: Any) -> str:
    return "_".join(_rule_tokens(value))


def _stable_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result
