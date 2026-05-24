"""Deterministic status-observation detector for reconcile snapshots.

The detector turns graph/index evidence into visible reconcile feedback.  It is
deliberately conservative: observations describe candidate drift or coverage
state, but they do not create project-improvement backlog rows unless a user or
observer explicitly routes them later.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import graph_snapshot_store as store
from . import reconcile_feedback


STATUS_OBSERVATION_SOURCE = "deterministic-status-observations"
DEFAULT_LIMIT = 300


def _read_json(path: str | Path, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _decode_notes(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _path_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = [raw]
    out = []
    for item in values:
        text = str(item or "").replace("\\", "/").strip("/")
        if text:
            out.append(text)
    return sorted(set(out))


def _short(text: Any, limit: int = 260) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "..."


def _node_id(feature: dict[str, Any]) -> str:
    return str(feature.get("node_id") or feature.get("id") or "").strip()


def _feature_paths(feature: dict[str, Any], *keys: str) -> list[str]:
    paths: list[str] = []
    metadata = feature.get("metadata") if isinstance(feature.get("metadata"), dict) else {}
    for key in keys:
        paths.extend(_path_list(feature.get(key)))
        if key == "config":
            paths.extend(_path_list(metadata.get("config_files")))
    return sorted(set(paths))


def _features_from_nodes(conn, project_id: str, snapshot_id: str) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    offset = 0
    while True:
        rows = store.list_graph_snapshot_nodes(
            conn,
            project_id,
            snapshot_id,
            limit=1000,
            offset=offset,
        )
        if not rows:
            break
        for row in rows:
            features.append({
                "node_id": row.get("node_id") or "",
                "title": row.get("title") or "",
                "layer": row.get("layer") or "",
                "kind": row.get("kind") or "",
                "primary": _path_list(row.get("primary_files")),
                "secondary": _path_list(row.get("secondary_files")),
                "test": _path_list(row.get("test_files")),
                "config": _path_list((row.get("metadata") or {}).get("config_files")),
                "metadata": row.get("metadata") or {},
            })
        offset += len(rows)
    return features


def _table_exists(conn, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
            (table,),
        ).fetchone()
    except Exception:
        return False
    return row is not None


def _merge_feature_path(feature: dict[str, Any], key: str, path: str) -> None:
    values = _path_list(feature.get(key))
    values.extend(_path_list(feature.get(f"{key}_files")))
    values.append(path)
    merged = sorted(set(_path_list(values)))
    feature[key] = merged
    feature[f"{key}_files"] = merged
    if key == "config":
        metadata = dict(feature.get("metadata") or {})
        metadata["config_files"] = merged
        feature["metadata"] = metadata


def _overlay_asset_projection_bindings(
    conn,
    project_id: str,
    snapshot_id: str,
    features: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add accepted DB-projected asset bindings to feature impact scope."""

    if not features or not _table_exists(conn, "graph_asset_bindings"):
        return features
    by_node = {_node_id(feature): feature for feature in features if _node_id(feature)}
    if not by_node:
        return features
    rows = conn.execute(
        """
        SELECT asset_kind, asset_path, node_id, source, evidence_json
        FROM graph_asset_bindings
        WHERE project_id = ?
          AND snapshot_id = ?
          AND binding_status = 'accepted'
          AND asset_kind IN ('doc', 'test', 'config')
        ORDER BY asset_kind, asset_path
        """,
        (project_id, snapshot_id),
    ).fetchall()
    key_by_kind = {
        "doc": "secondary",
        "test": "test",
        "config": "config",
    }
    for row in rows:
        feature = by_node.get(str(row["node_id"] or ""))
        key = key_by_kind.get(str(row["asset_kind"] or ""))
        path = str(row["asset_path"] or "").replace("\\", "/").strip("/")
        if not feature or not key or not path:
            continue
        _merge_feature_path(feature, key, path)
        metadata = dict(feature.get("metadata") or {})
        projected = metadata.setdefault("asset_projection_bindings", [])
        if isinstance(projected, list):
            projected.append({
                "asset_kind": str(row["asset_kind"] or ""),
                "asset_path": path,
                "source": str(row["source"] or ""),
            })
        feature["metadata"] = metadata
    return features


def _load_features(conn, project_id: str, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    notes = _decode_notes(snapshot.get("notes"))
    path = (
        notes.get("governance_index", {})
        .get("artifacts", {})
        .get("feature_index_path", "")
    )
    payload = _read_json(path, {}) if path else {}
    raw_features = payload.get("features") if isinstance(payload, dict) else None
    if isinstance(raw_features, list) and raw_features:
        return [dict(item) for item in raw_features if isinstance(item, dict)]
    return _features_from_nodes(conn, project_id, str(snapshot.get("snapshot_id") or ""))


def _load_file_inventory(conn, project_id: str, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    notes = _decode_notes(snapshot.get("notes"))
    path = (
        notes.get("governance_index", {})
        .get("artifacts", {})
        .get("file_inventory_path", "")
    )
    raw = _read_json(path, []) if path else []
    if isinstance(raw, list) and raw:
        return [dict(row) for row in raw if isinstance(row, dict)]
    payload = store.list_graph_snapshot_files(
        conn,
        project_id,
        str(snapshot.get("snapshot_id") or ""),
        limit=1000,
    )
    return [dict(row) for row in payload.get("files") or [] if isinstance(row, dict)]


def _load_coverage_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    notes = _decode_notes(snapshot.get("notes"))
    path = (
        notes.get("governance_index", {})
        .get("artifacts", {})
        .get("coverage_state_path", "")
    )
    payload = _read_json(path, {}) if path else {}
    return payload if isinstance(payload, dict) else {}


def _scope_file_delta(snapshot: dict[str, Any]) -> dict[str, Any]:
    notes = _decode_notes(snapshot.get("notes"))
    payload = notes.get("pending_scope_reconcile", {})
    if not isinstance(payload, dict):
        return {}
    delta = payload.get("scope_file_delta", {})
    return delta if isinstance(delta, dict) else {}


def _issue(
    *,
    issue_type: str,
    reason: str,
    summary: str,
    node_id: str = "",
    paths: list[str] | None = None,
    target: str = "",
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "type": issue_type,
        "reason": reason,
        "summary": _short(summary),
        "paths": _path_list(paths),
        "target": target or node_id,
        "evidence": evidence or {},
    }
    if node_id:
        payload["node_id"] = node_id
    return payload


def _missing_binding_issues(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for feature in features:
        node_id = _node_id(feature)
        if not node_id:
            continue
        layer = str(feature.get("layer") or "")
        primary = _feature_paths(feature, "primary", "primary_files")
        if layer != "L7" or not primary:
            continue
        title = str(feature.get("title") or feature.get("feature_name") or node_id)
        docs = _feature_paths(feature, "secondary", "secondary_files")
        tests = _feature_paths(feature, "test", "test_files")
        if not docs:
            issues.append(_issue(
                issue_type="missing_doc_binding",
                reason="coverage_state",
                node_id=node_id,
                paths=primary,
                summary=f"{node_id} {title} has code bindings but no graph-linked doc file.",
                evidence={"primary": primary, "layer": layer},
            ))
        if not tests:
            issues.append(_issue(
                issue_type="missing_test_binding",
                reason="coverage_state",
                node_id=node_id,
                paths=primary,
                summary=f"{node_id} {title} has code bindings but no graph-linked test file.",
                evidence={"primary": primary, "layer": layer},
            ))
    return issues


def _asset_binding_candidate_issues(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for feature in features:
        node_id = _node_id(feature)
        if not node_id:
            continue
        metadata = feature.get("metadata") if isinstance(feature.get("metadata"), dict) else {}
        candidates = metadata.get("asset_binding_candidates")
        if not isinstance(candidates, list):
            continue
        title = str(feature.get("title") or feature.get("feature_name") or node_id)
        for candidate in candidates[:25]:
            if not isinstance(candidate, dict):
                continue
            precheck = candidate.get("self_precheck") if isinstance(candidate.get("self_precheck"), dict) else {}
            asset_kind = str(candidate.get("asset_kind") or "asset")
            asset_path = str(candidate.get("asset_path") or "").replace("\\", "/").strip("/")
            if not asset_path:
                continue
            decision = str(precheck.get("decision") or "")
            if decision and decision != "review_required":
                continue
            issues.append(_issue(
                issue_type=f"{asset_kind}_binding_candidate_review",
                reason="weak_evidence_requires_review",
                node_id=node_id,
                paths=[asset_path],
                summary=(
                    f"{node_id} {title} has weak {asset_kind} binding candidate "
                    f"{asset_path}; review or add a source-controlled governance hint before graph binding."
                ),
                evidence={
                    "candidate": candidate,
                    "precheck": precheck,
                    "proposal_policy": "weak_asset_binding_proposal_first",
                },
            ))
    return issues


def _file_state_issues(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    watched_statuses = {"orphan", "pending_decision", "error"}
    for row in rows:
        path = str(row.get("path") or "").replace("\\", "/").strip("/")
        if not path:
            continue
        scan_status = str(row.get("scan_status") or "")
        graph_status = str(row.get("graph_status") or "")
        if scan_status not in watched_statuses and graph_status not in {"unmapped", "error"}:
            continue
        kind = str(row.get("file_kind") or "file")
        nodes = _path_list(row.get("attached_node_ids") or row.get("mapped_node_ids"))
        node_id = nodes[0] if nodes else ""
        issue_type = {
            "orphan": "orphan_file",
            "pending_decision": "pending_file_decision",
            "error": "file_scan_error",
        }.get(scan_status, "unmapped_file")
        issues.append(_issue(
            issue_type=issue_type,
            reason="file_inventory",
            node_id=node_id,
            target=path,
            paths=[path],
            summary=(
                f"{kind} file {path} has scan_status={scan_status or 'unknown'} "
                f"and graph_status={graph_status or 'unknown'}; keep visible until user/AI decides."
            ),
            evidence={
                "file_kind": kind,
                "scan_status": scan_status,
                "graph_status": graph_status,
                "decision": row.get("decision") or "",
                "reason": row.get("reason") or "",
                "attached_node_ids": nodes,
            },
        ))
    return issues


def _path_to_features(features: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for feature in features:
        for key in ("primary", "secondary", "test", "config"):
            for path in _feature_paths(feature, key, f"{key}_files"):
                index.setdefault(path, []).append(feature)
    return index


def _scope_delta_issues(features: list[dict[str, Any]], delta: dict[str, Any]) -> list[dict[str, Any]]:
    changed = set(_path_list(delta.get("changed_files") or delta.get("impacted_files")))
    if not changed:
        return []
    by_path = _path_to_features(features)
    impacted_features: dict[str, dict[str, Any]] = {}
    for path in changed:
        for feature in by_path.get(path, []):
            node_id = _node_id(feature)
            if node_id:
                impacted_features[node_id] = feature
    issues: list[dict[str, Any]] = []
    for node_id, feature in sorted(impacted_features.items()):
        primary = set(_feature_paths(feature, "primary", "primary_files"))
        config = set(_feature_paths(feature, "config", "config_files"))
        docs = set(_feature_paths(feature, "secondary", "secondary_files"))
        tests = set(_feature_paths(feature, "test", "test_files"))
        changed_code = sorted(changed.intersection(primary | config))
        if not changed_code:
            continue
        changed_docs = sorted(changed.intersection(docs))
        changed_tests = sorted(changed.intersection(tests))
        title = str(feature.get("title") or feature.get("feature_name") or node_id)
        if docs and not changed_docs:
            issues.append(_issue(
                issue_type="doc_drift_candidate",
                reason="scope_file_delta",
                node_id=node_id,
                paths=changed_code + sorted(docs),
                summary=(
                    f"{node_id} {title} code/config changed without linked doc changes; "
                    "mark as doc drift candidate for review."
                ),
                evidence={
                    "changed_code_or_config": changed_code,
                    "linked_docs": sorted(docs),
                    "scope_delta": delta,
                },
            ))
        if tests and not changed_tests:
            issues.append(_issue(
                issue_type="stale_test_expectation_candidate",
                reason="scope_file_delta",
                node_id=node_id,
                paths=changed_code + sorted(tests),
                summary=(
                    f"{node_id} {title} code/config changed without linked test changes; "
                    "mark as stale-test or coverage review candidate."
                ),
                evidence={
                    "changed_code_or_config": changed_code,
                    "linked_tests": sorted(tests),
                    "scope_delta": delta,
                },
            ))
    return issues


def _test_failure_issues(
    failures: list[dict[str, Any]],
    features: list[dict[str, Any]],
    file_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not failures:
        return []
    by_path = _path_to_features(features)
    row_nodes = {
        str(row.get("path") or "").replace("\\", "/").strip("/"): _path_list(
            row.get("attached_node_ids") or row.get("mapped_node_ids")
        )
        for row in file_rows
    }
    issues: list[dict[str, Any]] = []
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        path = str(failure.get("path") or failure.get("file") or "").replace("\\", "/").strip("/")
        if not path and failure.get("nodeid"):
            path = str(failure.get("nodeid")).split("::", 1)[0].replace("\\", "/").strip("/")
        if not path:
            continue
        nodes = [feature for feature in by_path.get(path, []) if _node_id(feature)]
        node_ids = [_node_id(feature) for feature in nodes]
        if not node_ids:
            node_ids = row_nodes.get(path, [])
        issues.append(_issue(
            issue_type="failed_test_candidate",
            reason="test_result",
            node_id=node_ids[0] if node_ids else "",
            target=path,
            paths=[path],
            summary=(
                f"Test failure in {path}: "
                f"{failure.get('nodeid') or failure.get('test') or failure.get('message') or 'unknown failure'}"
            ),
            evidence={
                "failure": failure,
                "candidate_node_ids": node_ids,
            },
        ))
    return issues


def build_status_observation_issues(
    conn,
    project_id: str,
    snapshot_id: str,
    *,
    test_failures: list[dict[str, Any]] | None = None,
    include_missing_bindings: bool = True,
    include_file_state: bool = True,
    include_scope_delta: bool = True,
) -> dict[str, Any]:
    """Build deterministic candidate status observations for a snapshot."""
    snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
    if not snapshot:
        raise KeyError(f"graph snapshot not found: {project_id}/{snapshot_id}")
    features = _load_features(conn, project_id, snapshot)
    features = _overlay_asset_projection_bindings(conn, project_id, snapshot_id, features)
    file_rows = _load_file_inventory(conn, project_id, snapshot)
    coverage_state = _load_coverage_state(snapshot)
    delta = _scope_file_delta(snapshot) if include_scope_delta else {}
    issues: list[dict[str, Any]] = []
    if include_missing_bindings:
        issues.extend(_missing_binding_issues(features))
        issues.extend(_asset_binding_candidate_issues(features))
    if include_file_state:
        issues.extend(_file_state_issues(file_rows))
    if include_scope_delta:
        issues.extend(_scope_delta_issues(features, delta))
    issues.extend(_test_failure_issues(test_failures or [], features, file_rows))
    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "snapshot_commit": snapshot.get("commit_sha") or "",
        "feature_count": len(features),
        "file_count": len(file_rows),
        "coverage_state_summary": {
            "missing_doc_node_count": coverage_state.get("missing_doc_node_count", 0),
            "missing_test_node_count": coverage_state.get("missing_test_node_count", 0),
            "referenced_file_count": coverage_state.get("referenced_file_count", 0),
        },
        "scope_file_delta": delta,
        "issues": issues,
        "issue_count": len(issues),
    }


def classify_status_observations(
    conn,
    project_id: str,
    snapshot_id: str,
    *,
    test_failures: list[dict[str, Any]] | None = None,
    actor: str = "status-observation-detector",
    limit: int | None = DEFAULT_LIMIT,
    include_missing_bindings: bool = True,
    include_file_state: bool = True,
    include_scope_delta: bool = True,
) -> dict[str, Any]:
    """Write deterministic status observations into reconcile feedback state."""
    built = build_status_observation_issues(
        conn,
        project_id,
        snapshot_id,
        test_failures=test_failures,
        include_missing_bindings=include_missing_bindings,
        include_file_state=include_file_state,
        include_scope_delta=include_scope_delta,
    )
    issues = built["issues"]
    if limit is not None and limit >= 0:
        issues = issues[: int(limit)]
    result = reconcile_feedback.classify_semantic_open_issues(
        project_id,
        snapshot_id,
        source_round=STATUS_OBSERVATION_SOURCE,
        created_by=actor,
        issues=issues,
        feedback_kind=reconcile_feedback.KIND_STATUS_OBSERVATION,
    )
    result["detector"] = {
        **{key: value for key, value in built.items() if key != "issues"},
        "classified_count": len(issues),
    }
    return result


__all__ = [
    "DEFAULT_LIMIT",
    "STATUS_OBSERVATION_SOURCE",
    "build_status_observation_issues",
    "classify_status_observations",
]
