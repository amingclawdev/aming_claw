"""State-only reconcile runners.

These helpers materialize reconcile outputs as governance state. They are not
chain stages and they must not edit project source, documentation, or tests.
Observer signoff or a later merge/finalize path decides when a candidate graph
snapshot becomes active.
"""
from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from agent.governance import graph_events
from agent.governance.graph_snapshot_store import (
    PENDING_STATUS_FAILED,
    PENDING_STATUS_MATERIALIZED,
    PENDING_STATUS_QUEUED,
    PENDING_STATUS_RUNNING,
    SNAPSHOT_STATUS_CANDIDATE,
    activate_graph_snapshot,
    create_graph_snapshot,
    ensure_schema as ensure_graph_snapshot_schema,
    finalize_graph_snapshot,
    get_active_graph_snapshot,
    get_graph_snapshot,
    graph_payload_edges,
    graph_payload_stats,
    index_graph_snapshot,
    list_graph_snapshot_files,
    list_pending_scope_reconcile,
    normalize_pending_scope_identity,
    record_reconcile_run_metric,
    snapshot_companion_dir,
    snapshot_graph_path,
    snapshot_id_for,
    waive_pending_scope_reconcile,
    write_companion_files,
)
from agent.governance.graph_correction_patches import (
    annotate_graph_node_roles,
    annotate_graph_relationship_metrics,
    apply_correction_patches,
    ensure_schema as ensure_graph_correction_schema,
    list_replayable_patches,
    persist_node_migrations,
    record_patch_apply_report,
)
from agent.governance.graph_hint_projection import build_hint_projection
from agent.governance.graph_rule_fingerprint import (
    build_full_reconcile_anchor,
    build_graph_rule_fingerprint,
    compare_rule_fingerprint,
    snapshot_rule_fingerprint,
)
from agent.governance.graph_structure_hints import load_graph_structure_hints
from agent.governance.db import sqlite_write_lock
from agent.governance.dirty_worktree import filter_dirty_files, parse_git_porcelain_paths
from agent.governance.governance_index import (
    build_governance_index,
    merge_feature_hashes_into_graph_nodes,
    persist_governance_index,
)
from agent.governance.checkout_provenance import describe_checkout
from agent.governance.reconcile_semantic_enrichment import run_semantic_enrichment
from agent.governance.reconcile_trace import ReconcileTrace, artifact_ref
from agent.governance.reconcile_file_inventory import (
    filter_governed_inventory_rows,
    filter_governed_paths,
    git_tracked_paths,
)
from agent.governance.reconcile_phases.phase_z_v2 import (
    FunctionMeta,
    ModuleInfo,
    build_call_graph,
    build_function_call_facts,
    build_test_consumer_fanin_index,
    function_source_hashes,
    build_graph_v2_from_symbols,
    build_rebase_candidate_graph,
    build_module_dependency_edges,
    enrich_nodes_with_architecture_signals,
    extract_typed_relations,
    _graph_enrich_config_rule_decision_for_test_fanin_entry,
    _load_graph_enrich_config_rules,
    parse_production_modules,
    parse_production_module_file,
    _test_fanin_entry_is_strong,
)


def _git_commit(project_root: str | Path, ref: str = "HEAD") -> str:
    root = Path(project_root).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", ref],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return ""
    return (result.stdout or "").strip()


def _short_commit(commit_sha: str) -> str:
    text = str(commit_sha or "").strip()
    return text[:7] if text else "unknown"


def _governance_state_dir(project_id: str, run_id: str) -> Path:
    from .db import _governance_root

    return _governance_root() / project_id / "state-reconcile" / run_id


def _project_graph_structure_hints(project_root: str | Path, candidate_graph: dict[str, Any]) -> dict[str, Any]:
    hint_index = load_graph_structure_hints(project_root)
    projection = build_hint_projection(candidate_graph, hint_index)
    return {
        "hint_index": hint_index,
        "projection": projection,
    }


def _git_changed_files(project_root: str | Path, base_ref: str, target_ref: str) -> list[str]:
    base = str(base_ref or "").strip()
    target = str(target_ref or "").strip()
    if not base or not target or base == target:
        return []
    root = Path(project_root).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-status", "--no-renames", f"{base}..{target}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return []
    return sorted({
        path.replace("\\", "/").strip("/")
        for line in (result.stdout or "").splitlines()
        for path in line.split("\t")[1:]
        if path.strip()
    })


_DIFF_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _git_changed_line_ranges(
    project_root: str | Path,
    base_ref: str,
    target_ref: str,
    paths: list[str] | None = None,
) -> dict[str, list[list[int]]]:
    base = str(base_ref or "").strip()
    target = str(target_ref or "").strip()
    if not base or not target or base == target:
        return {}
    root = Path(project_root).resolve()
    cmd = [
        "git",
        "-C",
        str(root),
        "diff",
        "--unified=0",
        "--no-renames",
        f"{base}..{target}",
        "--",
    ]
    cmd.extend(path.replace("\\", "/").strip("/") for path in (paths or []) if str(path or "").strip())
    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return {}
    ranges: dict[str, list[list[int]]] = {}
    current_path = ""
    for line in (result.stdout or "").splitlines():
        if line.startswith("diff --git "):
            current_path = ""
            continue
        if line.startswith("+++ "):
            raw_path = line[4:].strip()
            if raw_path == "/dev/null":
                current_path = ""
                continue
            current_path = raw_path[2:] if raw_path.startswith("b/") else raw_path
            current_path = current_path.replace("\\", "/").strip("/")
            continue
        if not current_path:
            continue
        match = _DIFF_HUNK_RE.match(line)
        if not match:
            continue
        start = max(1, int(match.group(1) or 1))
        count = int(match.group(2) or 1)
        end = start if count <= 0 else start + count - 1
        ranges.setdefault(current_path, []).append([start, end])
    return {path: values for path, values in sorted(ranges.items()) if values}


def _changed_functions_for_line_ranges(
    nodes: list[dict[str, Any]],
    changed_line_ranges_by_path: dict[str, list[list[int]]],
) -> dict[str, Any]:
    return _changed_symbols_for_line_ranges(
        nodes,
        changed_line_ranges_by_path,
        node_path_role="primary",
        metadata_symbols_key="functions",
        metadata_lines_key="function_lines",
        changed_ids_key="changed_function_ids",
        changed_by_node_key="changed_functions_by_node",
        changed_count_key="changed_function_count",
    )


def _changed_test_functions_for_line_ranges(
    nodes: list[dict[str, Any]],
    changed_line_ranges_by_path: dict[str, list[list[int]]],
) -> dict[str, Any]:
    return _changed_symbols_for_line_ranges(
        nodes,
        changed_line_ranges_by_path,
        node_path_role="test",
        metadata_symbols_key="test_functions",
        metadata_lines_key="test_function_lines",
        changed_ids_key="changed_test_function_ids",
        changed_by_node_key="changed_test_functions_by_node",
        changed_count_key="changed_test_function_count",
    )


def _changed_symbols_for_line_ranges(
    nodes: list[dict[str, Any]],
    changed_line_ranges_by_path: dict[str, list[list[int]]],
    *,
    node_path_role: str,
    metadata_symbols_key: str,
    metadata_lines_key: str,
    changed_ids_key: str,
    changed_by_node_key: str,
    changed_count_key: str,
) -> dict[str, Any]:
    changed_by_node: dict[str, list[str]] = {}
    changed_ids: set[str] = set()
    changed_paths = {
        path.replace("\\", "/").strip("/"): ranges
        for path, ranges in changed_line_ranges_by_path.items()
        if path and ranges
    }
    for node in nodes:
        node_id = _node_id(node)
        metadata = _node_metadata(node)
        functions = metadata.get(metadata_symbols_key) if isinstance(metadata.get(metadata_symbols_key), list) else []
        line_index = metadata.get(metadata_lines_key) if isinstance(metadata.get(metadata_lines_key), dict) else {}
        node_paths = set(_path_values(node, node_path_role))
        node_changed: set[str] = set()
        for path in node_paths.intersection(changed_paths):
            ranges = changed_paths.get(path) or []
            for raw_function in functions:
                function_id = str(raw_function or "")
                if not function_id:
                    continue
                lines = _function_line_range(line_index, function_id)
                if not lines:
                    continue
                start = int(lines[0] or 0)
                end = int(lines[1] if len(lines) > 1 else lines[0] or 0)
                if start <= 0:
                    continue
                end = max(start, end)
                if any(max(start, int(rng[0] or 0)) <= min(end, int(rng[1] or rng[0] or 0)) for rng in ranges):
                    node_changed.add(function_id)
                    changed_ids.add(function_id)
        if node_changed:
            changed_by_node[node_id] = sorted(node_changed)
    return {
        changed_ids_key: sorted(changed_ids),
        changed_by_node_key: dict(sorted(changed_by_node.items())),
        changed_count_key: len(changed_ids),
    }


def _function_line_range(line_index: dict[str, Any], function_id: str) -> list[Any]:
    lines = line_index.get(function_id)
    if isinstance(lines, list):
        return lines
    function_name = function_id.rsplit("::", 1)[-1]
    lines = line_index.get(function_name)
    return lines if isinstance(lines, list) else []


def _git_dirty_files(project_root: str | Path) -> list[str]:
    root = Path(project_root).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return []
    return filter_dirty_files(parse_git_porcelain_paths(result.stdout or ""))


def _deps_graph_nodes(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    deps = graph_json.get("deps_graph") if isinstance(graph_json, dict) else {}
    nodes = deps.get("nodes") if isinstance(deps, dict) else []
    return [node for node in nodes or [] if isinstance(node, dict)]


def _deps_graph_edges(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    return graph_payload_edges(graph_json)


def _normalize_inventory_commit(
    rows: list[dict[str, Any]],
    *,
    commit_sha: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if commit_sha and not item.get("last_scanned_commit"):
            item["last_scanned_commit"] = commit_sha
        if item.get("sha256") and not item.get("file_hash"):
            item["file_hash"] = f"sha256:{item['sha256']}"
        out.append(item)
    return out


def _rows_by_path(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = str(row.get("path") or "").replace("\\", "/").strip("/")
        if path:
            out[path] = row
    return out


def _snapshot_inventory_rows(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> list[dict[str, Any]]:
    if not snapshot_id:
        return []
    rows: list[dict[str, Any]] = []
    offset = 0
    page_size = 1000
    while True:
        try:
            payload = list_graph_snapshot_files(
                conn,
                project_id,
                snapshot_id,
                limit=page_size,
                offset=offset,
            )
        except Exception:
            return rows
        page = [dict(row) for row in payload.get("files") or [] if isinstance(row, dict)]
        rows.extend(page)
        filtered_count = int(payload.get("filtered_count") or payload.get("total_count") or 0)
        offset += len(page)
        if not page or offset >= filtered_count:
            return rows


def _row_file_hash(row: dict[str, Any]) -> str:
    value = str(row.get("file_hash") or "").strip()
    if value:
        return value
    sha = str(row.get("sha256") or "").strip()
    return f"sha256:{sha}" if sha else ""


def _build_scope_file_delta(
    *,
    project_root: str | Path | None = None,
    old_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
    changed_files: list[str],
) -> dict[str, Any]:
    if project_root is not None:
        old_rows = filter_governed_inventory_rows(project_root, old_rows)
        new_rows = filter_governed_inventory_rows(project_root, new_rows)
        changed_files = filter_governed_paths(project_root, changed_files)
        tracked_paths = git_tracked_paths(project_root)
        if tracked_paths is not None:
            def row_path(row: dict[str, Any]) -> str:
                return str(row.get("path") or "").replace("\\", "/").strip("/")

            changed_path_set = {
                str(path or "").replace("\\", "/").strip("/")
                for path in changed_files
                if str(path or "").strip()
            }
            old_rows = [
                row for row in old_rows
                if row_path(row) in tracked_paths
                or row_path(row) in changed_path_set
            ]
            new_rows = [
                row for row in new_rows
                if row_path(row) in tracked_paths
            ]
    old_by_path = _rows_by_path(old_rows)
    new_by_path = _rows_by_path(new_rows)
    old_paths = set(old_by_path)
    new_paths = set(new_by_path)
    added = sorted(new_paths - old_paths)
    removed = sorted(old_paths - new_paths)
    changed = sorted({path.replace("\\", "/").strip("/") for path in changed_files if path})
    hash_changed = sorted(
        path for path in (old_paths & new_paths)
        if _row_file_hash(old_by_path[path]) != _row_file_hash(new_by_path[path])
    )
    status_candidate_paths = set(changed) | set(hash_changed)
    status_changed = sorted(
        path for path in (old_paths & new_paths)
        if path in status_candidate_paths
        and (
            str(old_by_path[path].get("graph_status") or "")
            != str(new_by_path[path].get("graph_status") or "")
            or str(old_by_path[path].get("scan_status") or "")
            != str(new_by_path[path].get("scan_status") or "")
        )
    )
    ignored_status_changed = sorted(
        path for path in (old_paths & new_paths)
        if path not in status_candidate_paths
        and (
            str(old_by_path[path].get("graph_status") or "")
            != str(new_by_path[path].get("graph_status") or "")
            or str(old_by_path[path].get("scan_status") or "")
            != str(new_by_path[path].get("scan_status") or "")
        )
    )
    impacted = sorted(set(changed) | set(added) | set(removed) | set(hash_changed) | set(status_changed))
    return {
        "strategy": "full_scan_with_incremental_file_delta",
        "changed_files": changed,
        "added_files": added,
        "removed_files": removed,
        "hash_changed_files": hash_changed,
        "status_changed_files": status_changed,
        "ignored_status_changed_files": ignored_status_changed,
        "impacted_files": impacted,
        "changed_file_count": len(changed),
        "impacted_file_count": len(impacted),
    }


def _read_snapshot_graph(project_id: str, snapshot_id: str) -> dict[str, Any]:
    if not snapshot_id:
        return {}
    try:
        payload = json.loads(snapshot_graph_path(project_id, snapshot_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_snapshot_companion(project_id: str, snapshot_id: str, filename: str, default: Any) -> Any:
    try:
        payload = json.loads((snapshot_companion_dir(project_id, snapshot_id) / filename).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return payload


_RECONCILE_COMPARISON_VOLATILE_KEYS = {
    "artifact_path",
    "attached_to",
    "attachment_source",
    "candidate_node_id",
    "cluster_id",
    "created_at",
    "drift_sha256",
    "generated_at",
    "graph_sha256",
    "inventory_sha256",
    "phase_report_path",
    "project_root",
    "reason",
    "review_report_path",
    "run_id",
    "scratch_dir",
    "semantic_index_path",
    "snapshot_artifact",
    "snapshot_id",
    "snapshot_path",
    "state_dir",
    "summary_path",
    "trace",
    "trace_dir",
    "updated_at",
    "version",
}


def _stable_json_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_for_reconcile_comparison(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _normalize_for_reconcile_comparison(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _RECONCILE_COMPARISON_VOLATILE_KEYS
        }
    if isinstance(value, list):
        normalized = [_normalize_for_reconcile_comparison(item) for item in value]
        return sorted(normalized, key=_stable_json_key)
    return value


def normalize_reconcile_snapshot_for_comparison(
    graph_json: dict[str, Any],
    *,
    file_inventory: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a stable structural view for full-vs-scope reconcile comparison.

    The helper deliberately strips run-specific metadata, artifact paths,
    cluster provenance, timestamps, and snapshot ids while preserving node ids,
    graph structure, file bindings, file hashes, function metadata, and
    inventory state. It is intended for regression tests that compare full
    rebuild output with scope reconcile output for the same final project
    state.
    """
    nodes = _deps_graph_nodes(graph_json)
    edges = _deps_graph_edges(graph_json)
    inventory = file_inventory or []
    return {
        "nodes": _normalize_for_reconcile_comparison(nodes),
        "edges": _normalize_for_reconcile_comparison(edges),
        "file_inventory": _normalize_for_reconcile_comparison(inventory),
    }


def repair_snapshot_feature_hash_metadata(
    conn: sqlite3.Connection,
    project_id: str,
    project_root: str | Path,
    *,
    snapshot_id: str = "",
    actor: str = "observer",
) -> dict[str, Any]:
    """Backfill indexed feature/file hashes into an existing snapshot and node index."""
    ensure_graph_snapshot_schema(conn)
    snapshot = (
        get_graph_snapshot(conn, project_id, snapshot_id)
        if snapshot_id
        else get_active_graph_snapshot(conn, project_id)
    )
    if not snapshot:
        raise KeyError(f"graph snapshot not found for project {project_id}: {snapshot_id or 'active'}")
    sid = str(snapshot.get("snapshot_id") or "")
    graph_json = _read_snapshot_graph(project_id, sid)
    if not graph_json:
        raise ValueError(f"snapshot graph companion is empty or unreadable: {project_id}/{sid}")
    file_inventory = _read_snapshot_companion(project_id, sid, "file_inventory.json", [])
    if not isinstance(file_inventory, list):
        file_inventory = []
    drift_ledger = _read_snapshot_companion(project_id, sid, "drift_ledger.json", [])
    if not isinstance(drift_ledger, list):
        drift_ledger = []
    governance_index = build_governance_index(
        conn,
        project_id,
        project_root,
        run_id=f"hash-repair-{_short_commit(str(snapshot.get('commit_sha') or ''))}",
        commit_sha=str(snapshot.get("commit_sha") or ""),
        candidate_graph=graph_json,
        snapshot_id=sid,
        snapshot_kind=str(snapshot.get("snapshot_kind") or ""),
        file_inventory=file_inventory,
    )
    merge_summary = merge_feature_hashes_into_graph_nodes(graph_json, governance_index)
    artifacts = write_companion_files(
        project_id,
        sid,
        graph_json=graph_json,
        file_inventory=file_inventory,
        drift_ledger=drift_ledger,
    )
    index_counts = index_graph_snapshot(
        conn,
        project_id,
        sid,
        nodes=_deps_graph_nodes(graph_json),
        edges=_deps_graph_edges(graph_json),
    )
    try:
        notes = json.loads(str(snapshot.get("notes") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        notes = {}
    if not isinstance(notes, dict):
        notes = {}
    notes["feature_hash_metadata_repair"] = {
        "actor": actor,
        "merge_summary": merge_summary,
        "index_counts": index_counts,
        "artifacts": {
            "graph_sha256": artifacts.get("graph_sha256", ""),
            "inventory_sha256": artifacts.get("inventory_sha256", ""),
            "drift_sha256": artifacts.get("drift_sha256", ""),
        },
    }
    conn.execute(
        """
        UPDATE graph_snapshots
        SET graph_sha256 = ?, inventory_sha256 = ?, drift_sha256 = ?, notes = ?
        WHERE project_id = ? AND snapshot_id = ?
        """,
        (
            artifacts.get("graph_sha256", ""),
            artifacts.get("inventory_sha256", ""),
            artifacts.get("drift_sha256", ""),
            json.dumps(notes, ensure_ascii=False, sort_keys=True),
            project_id,
            sid,
        ),
    )
    return {
        "snapshot_id": sid,
        "commit_sha": snapshot.get("commit_sha", ""),
        "merge_summary": merge_summary,
        "index_counts": index_counts,
        "artifacts": artifacts,
    }


def _graph_nodes(graph_json: dict[str, Any]) -> list[dict[str, Any]]:
    deps = graph_json.get("deps_graph") if isinstance(graph_json, dict) else {}
    if isinstance(deps, dict) and isinstance(deps.get("nodes"), list):
        return [node for node in deps.get("nodes", []) if isinstance(node, dict)]
    nodes = graph_json.get("nodes") if isinstance(graph_json, dict) else []
    if isinstance(nodes, list):
        return [node for node in nodes if isinstance(node, dict)]
    if isinstance(nodes, dict):
        out: list[dict[str, Any]] = []
        for node_id, node in nodes.items():
            item = dict(node) if isinstance(node, dict) else {}
            item.setdefault("id", node_id)
            out.append(item)
        return out
    return []


def _node_id(node: dict[str, Any]) -> str:
    return str(node.get("id") or node.get("node_id") or "")


def _node_metadata(node: dict[str, Any]) -> dict[str, Any]:
    metadata = node.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def _node_parent(node: dict[str, Any]) -> str:
    metadata = _node_metadata(node)
    return str(metadata.get("hierarchy_parent") or node.get("parent") or node.get("parent_id") or "")


def _path_values(node: dict[str, Any], key: str) -> list[str]:
    aliases = {
        "primary": ("primary", "primary_files"),
        "secondary": ("secondary", "secondary_files"),
        "test": ("test", "test_files"),
        "config": ("config", "config_files"),
    }.get(key, (key,))
    out: list[str] = []
    seen: set[str] = set()
    metadata = _node_metadata(node)
    for alias in aliases:
        raw = node.get(alias)
        if raw is None and alias.endswith("_files"):
            raw = metadata.get(alias)
        values = raw if isinstance(raw, list) else [raw] if raw else []
        for value in values:
            path = str(value or "").replace("\\", "/").strip("/")
            if path and path not in seen:
                seen.add(path)
                out.append(path)
    return out


def _node_file_hashes(node: dict[str, Any]) -> dict[str, str]:
    metadata = _node_metadata(node)
    raw = metadata.get("file_hashes")
    if not isinstance(raw, dict):
        return {}
    return {
        str(path or "").replace("\\", "/").strip("/"): str(value or "")
        for path, value in raw.items()
        if str(path or "").strip()
    }


def _edge_key(edge: dict[str, Any]) -> tuple[str, str, str, str]:
    metadata = edge.get("metadata") if isinstance(edge.get("metadata"), dict) else {}
    return (
        str(edge.get("source") or edge.get("from") or ""),
        str(edge.get("target") or edge.get("to") or ""),
        str(edge.get("type") or edge.get("relation") or edge.get("relation_type") or ""),
        str(metadata.get("edge_kind") or edge.get("kind") or ""),
    )


_INCREMENTAL_METADATA_FILE_KINDS = {"config", "doc", "index_doc"}
_RULESET_SCOPE_MARKERS = {
    "graph_fact_interpretation",
    "graph_relation_rules",
    "graph_ruleset",
    "ruleset",
}
_RULESET_PATH_MARKERS = (
    "/graph_rules/",
    "/rulesets/",
    "/relation_rules/",
    "config/graph_rules/",
    "config/reconcile/rules/",
)


def _norm_repo_path(path: Any) -> str:
    return str(path or "").replace("\\", "/").strip("/")


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _node_for_primary_path(graph_json: dict[str, Any], path: str) -> dict[str, Any] | None:
    norm = str(path or "").replace("\\", "/").strip("/")
    if not norm:
        return None
    matches = [
        node for node in _graph_nodes(graph_json)
        if norm in set(_path_values(node, "primary"))
    ]
    return matches[0] if len(matches) == 1 else None


def _is_ruleset_interpretation_row(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    scope = str(row.get("ruleset_scope") or row.get("interpretation_scope") or "").strip()
    if scope in _RULESET_SCOPE_MARKERS:
        return True
    role = str(row.get("config_role") or row.get("rule_role") or "").strip()
    return role in _RULESET_SCOPE_MARKERS


def _looks_like_ruleset_path(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/").strip("/")
    if not normalized:
        return False
    return any(marker.strip("/") in normalized for marker in _RULESET_PATH_MARKERS)


def _module_function_signature(module: Any) -> list[dict[str, Any]]:
    signature: list[dict[str, Any]] = []
    for func in getattr(module, "functions", []) or []:
        name = str(getattr(func, "name", "") or "")
        qualified_name = str(getattr(func, "qualified_name", "") or "")
        if not name or not qualified_name:
            continue
        signature.append({
            "name": name,
            "qualified_name": qualified_name,
            "lineno": int(getattr(func, "lineno", 0) or 0),
            "end_lineno": int(getattr(func, "end_lineno", 0) or 0),
        })
    return sorted(signature, key=lambda item: item["qualified_name"])


def _node_function_signature(node: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = _node_metadata(node)
    raw_functions = metadata.get("functions") if isinstance(metadata.get("functions"), list) else []
    raw_lines = metadata.get("function_lines") if isinstance(metadata.get("function_lines"), dict) else {}
    signature: list[dict[str, Any]] = []
    for raw in raw_functions:
        qualified_name = str(raw or "")
        if not qualified_name:
            continue
        name = qualified_name.rsplit("::", 1)[-1]
        lines = raw_lines.get(name) if isinstance(raw_lines.get(name), list) else []
        signature.append({
            "name": name,
            "qualified_name": qualified_name,
            "lineno": int(lines[0]) if len(lines) >= 1 else 0,
            "end_lineno": int(lines[1]) if len(lines) >= 2 else int(lines[0]) if lines else 0,
        })
    return sorted(signature, key=lambda item: item["qualified_name"])


def _source_path_incremental_eligibility(
    project_root: str | Path,
    active_graph_json: dict[str, Any],
    path: str,
) -> dict[str, Any]:
    node = _node_for_primary_path(active_graph_json, path)
    if not node:
        return {"supported": False, "reason": "source_primary_node_not_unique", "path": path}
    module = parse_production_module_file(str(Path(project_root).resolve()), path)
    if module is None:
        return {"supported": False, "reason": "source_adapter_parse_failed", "path": path}
    metadata = _node_metadata(node)
    expected_module = str(metadata.get("module") or "")
    parsed_module = str(getattr(module, "module_name", "") or "")
    if expected_module and parsed_module != expected_module:
        return {
            "supported": False,
            "reason": "source_module_identity_changed",
            "path": path,
            "expected_module": expected_module,
            "parsed_module": parsed_module,
        }
    try:
        project_root_resolved = str(Path(project_root).resolve())
        typed_relations = extract_typed_relations(
            project_root_resolved,
            {parsed_module: module},
            graph_enrich_config_rules=_load_graph_enrich_config_rules(project_root_resolved),
        )
    except Exception:
        return {"supported": False, "reason": "source_typed_relation_scan_failed", "path": path}
    if getattr(module, "source_kind", "") == "filetree_fallback":
        return {
            "supported": False,
            "reason": "source_filetree_fallback_requires_full_rebuild",
            "path": path,
        }
    parsed_signature = _module_function_signature(module)
    active_signature = _node_function_signature(node)
    if parsed_signature != active_signature:
        return {
            "supported": False,
            "reason": "source_function_signature_changed",
            "path": path,
            "parsed_signature": parsed_signature,
            "active_signature": active_signature,
        }
    return {
        "supported": True,
        "reason": "source_dependency_structure_stable"
        if (
            getattr(module, "import_map", None)
            or getattr(module, "adapter_imports", None)
            or getattr(module, "adapter_relations", None)
            or typed_relations
            or any(getattr(func, "calls", None) for func in getattr(module, "functions", []) or [])
            or any(getattr(func, "decorators", None) for func in getattr(module, "functions", []) or [])
        )
        else "source_hash_only_structure_stable",
        "path": path,
        "node_id": _node_id(node),
        "module": parsed_module,
        "requires_dependency_delta": bool(
            getattr(module, "import_map", None)
            or getattr(module, "adapter_imports", None)
            or getattr(module, "adapter_relations", None)
            or typed_relations
            or any(getattr(func, "calls", None) for func in getattr(module, "functions", []) or [])
            or any(getattr(func, "decorators", None) for func in getattr(module, "functions", []) or [])
        ),
        "typed_relation_count": len(typed_relations),
    }


def _node_ids_for_paths(graph_json: dict[str, Any], paths: set[str]) -> list[str]:
    if not paths:
        return []
    node_ids: set[str] = set()
    for node in _graph_nodes(graph_json):
        node_id = _node_id(node)
        if not node_id:
            continue
        for role in ("primary", "secondary", "test", "config"):
            if paths.intersection(_path_values(node, role)):
                node_ids.add(node_id)
                break
    return sorted(node_ids)


def _edge_delta_payload(key: tuple[str, str, str, str]) -> dict[str, str]:
    source, target, relation_type, edge_kind = key
    return {
        "source": source,
        "target": target,
        "relation_type": relation_type,
        "edge_kind": edge_kind,
    }


def _mark_scope_file_delta_strategy(
    scope_file_delta: dict[str, Any],
    *,
    strategy: str,
    graph_delta_mode: str = "",
    fallback_reason: str = "",
) -> dict[str, Any]:
    out = dict(scope_file_delta)
    out["strategy"] = strategy
    if graph_delta_mode:
        out["graph_delta_mode"] = graph_delta_mode
    if fallback_reason:
        out["fallback_reason"] = fallback_reason
    return out


def _incremental_metadata_scope_eligibility(
    scope_file_delta: dict[str, Any],
    *,
    project_root: str | Path,
    active_graph_json: dict[str, Any],
    old_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if scope_file_delta.get("status_changed_files"):
        return {"supported": False, "reason": "inventory_status_change_requires_full_rebuild"}

    old_by_path = _rows_by_path(old_rows)
    new_by_path = _rows_by_path(new_rows)
    added = {
        str(path or "").replace("\\", "/").strip("/")
        for path in scope_file_delta.get("added_files", [])
        if str(path or "").strip()
    }
    removed = {
        str(path or "").replace("\\", "/").strip("/")
        for path in scope_file_delta.get("removed_files", [])
        if str(path or "").strip()
    }
    hash_changed = {
        str(path or "").replace("\\", "/").strip("/")
        for path in scope_file_delta.get("hash_changed_files", [])
        if str(path or "").strip()
    }
    impacted = {
        str(path or "").replace("\\", "/").strip("/")
        for path in scope_file_delta.get("impacted_files", [])
        if str(path or "").strip()
    }
    if not impacted:
        return {"supported": False, "reason": "no_impacted_files"}

    ruleset_paths = sorted(
        path for path in impacted
        if _is_ruleset_interpretation_row(old_by_path.get(path) or {})
        or _is_ruleset_interpretation_row(new_by_path.get(path) or {})
        or _looks_like_ruleset_path(path)
    )
    if ruleset_paths:
        return {
            "supported": False,
            "reason": "ruleset_change_requires_rule_aware_reconcile",
            "rule_aware": True,
            "ruleset_paths": ruleset_paths,
        }

    def _is_non_primary_test(path: str) -> bool:
        old_row = old_by_path.get(path) or {}
        new_row = new_by_path.get(path) or {}
        kind = str(new_row.get("file_kind") or old_row.get("file_kind") or "")
        role = str(new_row.get("attachment_role") or old_row.get("attachment_role") or "")
        return kind == "test" and role != "primary"

    added_test_paths = sorted(path for path in added if _is_non_primary_test(path))
    removed_test_paths = sorted(path for path in removed if _is_non_primary_test(path))
    unsupported_added = sorted(added - set(added_test_paths))
    if unsupported_added:
        return {
            "supported": False,
            "reason": "added_files_require_full_rebuild",
            "paths": unsupported_added,
        }
    unsupported_removed = sorted(removed - set(removed_test_paths))
    if unsupported_removed:
        return {
            "supported": False,
            "reason": "removed_files_require_full_rebuild",
            "paths": unsupported_removed,
        }

    test_file_set_paths = set(added_test_paths) | set(removed_test_paths)
    non_hash_impacted = impacted - hash_changed - test_file_set_paths
    if non_hash_impacted:
        return {
            "supported": False,
            "reason": "non_hash_impacted_files_require_full_rebuild",
            "paths": sorted(non_hash_impacted),
        }

    unsupported: list[dict[str, str]] = []
    source_checks: list[dict[str, Any]] = []
    source_paths: list[str] = []
    metadata_paths: list[str] = []
    test_paths: list[str] = []
    for path in sorted(impacted):
        if path in test_file_set_paths:
            test_paths.append(path)
            continue
        old_row = old_by_path.get(path) or {}
        new_row = new_by_path.get(path) or {}
        kind = str(new_row.get("file_kind") or old_row.get("file_kind") or "")
        role = str(new_row.get("attachment_role") or old_row.get("attachment_role") or "")
        if kind == "test" and role != "primary":
            test_paths.append(path)
            continue
        if kind in _INCREMENTAL_METADATA_FILE_KINDS and role != "primary":
            metadata_paths.append(path)
            continue
        if kind == "source" and role == "primary":
            check = _source_path_incremental_eligibility(project_root, active_graph_json, path)
            source_checks.append(check)
            if not check.get("supported"):
                unsupported.append({
                    "path": path,
                    "file_kind": kind,
                    "attachment_role": role,
                    "reason": str(check.get("reason") or ""),
                })
            else:
                source_paths.append(path)
            continue
        if kind not in _INCREMENTAL_METADATA_FILE_KINDS or role == "primary":
            unsupported.append({"path": path, "file_kind": kind, "attachment_role": role})
    if unsupported:
        detailed_reasons = sorted({
            str(item.get("reason") or "")
            for item in unsupported
            if str(item.get("reason") or "")
        })
        return {
            "supported": False,
            "reason": detailed_reasons[0] if len(detailed_reasons) == 1 else "structural_or_unknown_file_requires_full_rebuild",
            "unsupported": unsupported,
            "source_checks": source_checks,
        }
    if test_paths and (source_paths or metadata_paths):
        return {
            "supported": False,
            "reason": "test_fanin_mixed_changes_require_full_rebuild",
            "test_paths": test_paths,
            "source_paths": source_paths,
            "metadata_paths": metadata_paths,
            "source_checks": source_checks,
        }
    source_dependency_delta = any(
        bool(check.get("requires_dependency_delta"))
        for check in source_checks
        if isinstance(check, dict)
    )
    mode = "metadata_only"
    if test_paths:
        mode = "test_fanin_file_set" if test_file_set_paths else "test_fanin_hash_only"
    elif source_paths and metadata_paths:
        mode = "mixed_dependency_delta" if source_dependency_delta else "mixed_hash_only"
    elif source_paths:
        mode = "source_dependency_delta" if source_dependency_delta else "source_hash_only"
    return {
        "supported": True,
        "reason": (
            "test_fanin_file_set_structure_stable"
            if test_file_set_paths
            else "source_dependency_structure_stable"
            if source_dependency_delta
            else "hash_only_structure_stable"
        ),
        "mode": mode,
        "source_paths": source_paths,
        "metadata_paths": metadata_paths,
        "test_paths": test_paths,
        "added_test_paths": added_test_paths,
        "removed_test_paths": removed_test_paths,
        "source_checks": source_checks,
    }


def _fanin_entry_path(entry: dict[str, Any]) -> str:
    return _norm_repo_path(entry.get("rel_path") or entry.get("path"))


def _graph_fanin_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": _fanin_entry_path(entry),
        "evidence": str(entry.get("evidence") or "test_import_fanin"),
        "imports": sorted({
            str(token or "").strip()
            for token in entry.get("imports", []) or []
            if str(token or "").strip()
        }),
    }


def _apply_incremental_test_fanin_bindings(
    project_root: str | Path,
    candidate_graph: dict[str, Any],
    *,
    changed_test_paths: list[str],
    removed_test_paths: list[str] | None = None,
) -> dict[str, Any]:
    changed = {
        _norm_repo_path(path)
        for path in changed_test_paths
        if _norm_repo_path(path)
    }
    removed = {
        _norm_repo_path(path)
        for path in (removed_test_paths or [])
        if _norm_repo_path(path)
    }
    affected = changed | removed
    if not affected:
        return {"ok": True, "changed_test_paths": [], "updated_node_ids": []}

    from agent.governance.project_profile import discover_project_profile

    root = Path(project_root).resolve()
    profile = discover_project_profile(str(root))
    modules = parse_production_modules(str(root), profile=profile)
    graph_enrich_config_rules = _load_graph_enrich_config_rules(str(root))
    fanin_index = build_test_consumer_fanin_index(str(root), modules, profile=profile)
    new_fanin_by_module: dict[str, list[dict[str, Any]]] = {}
    for module_name, entries in fanin_index.items():
        kept = [
            dict(entry)
            for entry in entries or []
            if _fanin_entry_path(entry) in changed
        ]
        if kept:
            new_fanin_by_module[str(module_name)] = kept

    updated_node_ids: list[str] = []
    updated_modules: list[str] = []
    for node in _deps_graph_nodes(candidate_graph):
        node_id = _node_id(node)
        metadata = _node_metadata(node)
        module_name = str(metadata.get("module") or node.get("module") or "")
        if not module_name:
            continue
        old_fanin = [
            dict(entry)
            for entry in metadata.get("test_consumer_fanin", []) or []
            if isinstance(entry, dict)
        ]
        old_changed_paths = {
            _norm_repo_path(entry.get("path"))
            for entry in old_fanin
            if _norm_repo_path(entry.get("path")) in affected
        }
        new_changed_fanin = [
            _graph_fanin_entry(entry)
            for entry in new_fanin_by_module.get(module_name, [])
            if _test_fanin_entry_is_strong(
                entry,
                _graph_enrich_config_rule_decision_for_test_fanin_entry(
                    entry,
                    node=node,
                    rules=graph_enrich_config_rules,
                ),
            )
        ]
        old_tests = sorted(_path_values(node, "test"))
        old_affected_tests = {path for path in old_tests if path in affected}
        if not old_changed_paths and not new_changed_fanin and not old_affected_tests:
            continue

        kept_fanin = [
            _graph_fanin_entry(entry)
            for entry in old_fanin
            if _norm_repo_path(entry.get("path")) not in affected
        ]
        merged_fanin = sorted(
            kept_fanin + new_changed_fanin,
            key=lambda item: (str(item.get("path") or ""), ",".join(item.get("imports") or [])),
        )
        old_fanin_paths = {
            _norm_repo_path(entry.get("path"))
            for entry in old_fanin
            if _norm_repo_path(entry.get("path"))
        }
        direct_tests = {
            path for path in old_tests
            if path not in old_fanin_paths and path not in removed
        }
        fanin_paths = {
            _norm_repo_path(entry.get("path"))
            for entry in merged_fanin
            if _norm_repo_path(entry.get("path"))
        }
        new_tests = sorted(direct_tests | fanin_paths)
        old_normalized_fanin = sorted(
            [_graph_fanin_entry(entry) for entry in old_fanin],
            key=lambda item: (str(item.get("path") or ""), ",".join(item.get("imports") or [])),
        )
        if new_tests == old_tests and merged_fanin == old_normalized_fanin:
            continue
        node["test"] = new_tests
        node["test_coverage"] = "direct" if new_tests else "none"
        metadata["test_consumer_fanin"] = merged_fanin
        node["metadata"] = metadata
        if node_id:
            updated_node_ids.append(node_id)
        updated_modules.append(module_name)

    return {
        "ok": True,
        "changed_test_paths": sorted(changed),
        "removed_test_paths": sorted(removed),
        "updated_node_ids": sorted(set(updated_node_ids)),
        "updated_modules": sorted(set(updated_modules)),
    }


def _module_info_from_graph_node(project_root: str | Path, node: dict[str, Any]) -> ModuleInfo | None:
    metadata = _node_metadata(node)
    module_name = str(metadata.get("module") or node.get("module") or "")
    primary = sorted(_path_values(node, "primary"))
    if not module_name or not primary:
        return None
    function_lines = metadata.get("function_lines") if isinstance(metadata.get("function_lines"), dict) else {}
    functions: list[FunctionMeta] = []
    for qualified_name in metadata.get("functions", []) or []:
        qname = str(qualified_name or "")
        if not qname:
            continue
        name = qname.rsplit("::", 1)[-1]
        lines = function_lines.get(name) if isinstance(function_lines.get(name), list) else []
        start = int(lines[0]) if len(lines) >= 1 else 0
        end = int(lines[1]) if len(lines) >= 2 else start
        functions.append(
            FunctionMeta(
                module=module_name,
                name=name,
                qualified_name=qname,
                lineno=start,
                end_lineno=end,
            )
        )
    return ModuleInfo(
        path=str(Path(project_root).resolve() / primary[0]),
        module_name=module_name,
        functions=functions,
    )


def _active_module_index(
    project_root: str | Path,
    graph_json: dict[str, Any],
) -> tuple[dict[str, ModuleInfo], dict[str, dict[str, Any]], dict[str, str]]:
    modules: dict[str, ModuleInfo] = {}
    nodes_by_module: dict[str, dict[str, Any]] = {}
    node_ids_by_module: dict[str, str] = {}
    for node in _deps_graph_nodes(graph_json):
        module = _module_info_from_graph_node(project_root, node)
        if module is None:
            continue
        node_id = _node_id(node)
        modules[module.module_name] = module
        nodes_by_module[module.module_name] = node
        if node_id:
            node_ids_by_module[module.module_name] = node_id
    return modules, nodes_by_module, node_ids_by_module


def _asset_identity_for_relation(target_kind: str, target: str) -> tuple[str, bool]:
    kind = str(target_kind or "")
    value = str(target or "")
    lower = value.lower()
    if kind == "db_table":
        return f"db_table:{value}", False
    if kind == "artifact":
        basename = lower.rsplit("/", 1)[-1]
        important = (
            basename.startswith("graph")
            or basename in {"governance.db", "context_store.db", "manager_signal.json", "manager_status.json"}
            or lower.endswith((".db", ".sqlite", ".sqlite3"))
        )
        return (f"artifact:{value}", False) if important else ("artifact:__artifact_assets", True)
    if kind == "config":
        return f"config:{value}", False
    if kind == "event":
        return "event:__event_contracts", True
    if kind == "interface":
        return (f"interface:{value}", False) if "/api/" in lower else ("interface:__interface_contracts", True)
    if kind == "task":
        return "task:__task_contracts", True
    if kind == "task_metadata":
        return "task_metadata:__task_metadata_contracts", True
    return f"{kind}:{value}", False


def _asset_ids_by_key(graph_json: dict[str, Any]) -> dict[str, tuple[str, bool]]:
    out: dict[str, tuple[str, bool]] = {}
    for node in _deps_graph_nodes(graph_json):
        if str(node.get("layer") or "") != "L4":
            continue
        metadata = _node_metadata(node)
        key = str(metadata.get("asset_key") or node.get("asset_key") or "")
        node_id = _node_id(node)
        if key and node_id:
            out[key] = (node_id, bool(metadata.get("aggregate_asset") or node.get("aggregate_asset")))
    return out


def _link_kind(link: dict[str, Any]) -> str:
    metadata = link.get("metadata") if isinstance(link.get("metadata"), dict) else {}
    return str(metadata.get("edge_kind") or "")


def _add_indexed_graph_link(
    links: list[dict[str, Any]],
    index: dict[tuple[str, str, str], dict[str, Any]],
    source: str,
    target: str,
    relation_type: str,
    evidence: str = "",
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    if not source or not target or source == target:
        return
    key = (source, target, relation_type)
    if key in index:
        item = index[key]
        item["evidence_count"] = int(item.get("evidence_count") or 1) + 1
        if evidence:
            sample = item.setdefault("evidence_sample", [])
            if isinstance(sample, list) and evidence not in sample and len(sample) < 5:
                sample.append(evidence)
        return
    item: dict[str, Any] = {"source": source, "target": target, "type": relation_type}
    if evidence:
        item["evidence"] = evidence
        item["evidence_sample"] = [evidence]
        item["evidence_count"] = 1
    if metadata:
        item["metadata"] = dict(metadata)
    index[key] = item
    links.append(item)


def _dependency_direction_for_relation(source: str, target: str, relation_type: str) -> tuple[str, str]:
    producer_to_asset = {
        "owns_state",
        "writes_state",
        "writes_artifact",
        "emits_event",
    }
    asset_to_consumer = {
        "reads_state",
        "reads_artifact",
        "consumes_event",
        "uses_task_metadata",
        "http_route",
        "creates_task",
        "configures_role",
        "configures_analyzer",
        "configures_model_routing",
        "configures_runtime",
    }
    if relation_type in producer_to_asset:
        return source, target
    if relation_type in asset_to_consumer:
        return target, source
    return source, target


def _graph_links_have_cycle(links: list[dict[str, Any]]) -> bool:
    adjacency: dict[str, set[str]] = {}
    for link in links:
        source = str(link.get("source") or "")
        target = str(link.get("target") or "")
        if source and target and source != target:
            adjacency.setdefault(source, set()).add(target)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        for child in adjacency.get(node_id, set()):
            if visit(child):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(visit(node_id) for node_id in list(adjacency))


def _refresh_candidate_dependency_links(candidate_graph: dict[str, Any]) -> dict[str, Any]:
    deps_graph = candidate_graph.get("deps_graph") if isinstance(candidate_graph.get("deps_graph"), dict) else {}
    hierarchy_graph = (
        candidate_graph.get("hierarchy_graph")
        if isinstance(candidate_graph.get("hierarchy_graph"), dict)
        else {}
    )
    nodes = [node for node in deps_graph.get("nodes", []) or [] if isinstance(node, dict)]
    nodes_by_id = {_node_id(node): node for node in nodes if _node_id(node)}
    links = [dict(link) for link in deps_graph.get("links", []) or [] if isinstance(link, dict)]
    base_links = [
        link for link in links
        if _link_kind(link) not in {
            "aggregated_l2_dependency",
            "aggregated_l3_dependency",
            "shared_asset_dependency",
        }
    ]
    parent: dict[str, str] = {}
    for link in hierarchy_graph.get("links", []) or []:
        if not isinstance(link, dict) or str(link.get("type") or "") != "contains":
            continue
        source = str(link.get("source") or "")
        target = str(link.get("target") or "")
        if source and target:
            parent[target] = source
    for node_id, node in nodes_by_id.items():
        metadata = _node_metadata(node)
        parent_id = str(metadata.get("hierarchy_parent") or "")
        if parent_id and node_id not in parent:
            parent[node_id] = parent_id

    def node_layer(node_id: str) -> str:
        return str((nodes_by_id.get(node_id) or {}).get("layer") or "")

    def is_aggregate_asset(node_id: str) -> bool:
        node = nodes_by_id.get(node_id) or {}
        metadata = _node_metadata(node)
        return node.get("layer") == "L4" and bool(metadata.get("aggregate_asset") or node.get("aggregate_asset"))

    def parent_at(node_id: str, layer: str) -> str:
        current = node_id
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            if node_layer(current) == layer:
                return current
            current = parent.get(current, "")
        return ""

    index = {
        (
            str(link.get("source") or ""),
            str(link.get("target") or ""),
            str(link.get("type") or ""),
        ): link
        for link in base_links
    }
    asset_producers: dict[str, set[str]] = {}
    asset_consumers: dict[str, set[str]] = {}
    for link in base_links:
        source = str(link.get("source") or "")
        target = str(link.get("target") or "")
        source_layer = node_layer(source)
        target_layer = node_layer(target)
        if source_layer == "L7" and target_layer == "L4":
            asset_producers.setdefault(target, set()).add(source)
        elif source_layer == "L4" and target_layer == "L7":
            asset_consumers.setdefault(source, set()).add(target)
    for asset_id, producers in sorted(asset_producers.items()):
        if is_aggregate_asset(asset_id):
            continue
        for producer in sorted(producers):
            for consumer in sorted(asset_consumers.get(asset_id, set())):
                _add_indexed_graph_link(
                    base_links,
                    index,
                    producer,
                    consumer,
                    "depends_on",
                    f"shared asset {asset_id}",
                    metadata={"edge_kind": "shared_asset_dependency", "asset": asset_id},
                )

    l7_links = [
        link for link in base_links
        if node_layer(str(link.get("source") or "")) == "L7"
        and node_layer(str(link.get("target") or "")) == "L7"
    ]
    for link in l7_links:
        source_l7 = str(link.get("source") or "")
        target_l7 = str(link.get("target") or "")
        source_l3 = parent_at(source_l7, "L3")
        target_l3 = parent_at(target_l7, "L3")
        source_l2 = parent_at(source_l7, "L2")
        target_l2 = parent_at(target_l7, "L2")
        if source_l3 and target_l3 and source_l3 != target_l3:
            _add_indexed_graph_link(
                base_links,
                index,
                source_l3,
                target_l3,
                "depends_on",
                f"aggregated from {source_l7}->{target_l7}",
                metadata={"edge_kind": "aggregated_l3_dependency"},
            )
        if source_l2 and target_l2 and source_l2 != target_l2:
            _add_indexed_graph_link(
                base_links,
                index,
                source_l2,
                target_l2,
                "depends_on",
                f"aggregated from {source_l7}->{target_l7}",
                metadata={"edge_kind": "aggregated_l2_dependency"},
            )

    deps_graph["links"] = sorted(base_links, key=lambda item: (
        str(item.get("source") or ""),
        str(item.get("target") or ""),
        str(item.get("type") or ""),
    ))
    deps_by_child: dict[str, set[str]] = {}
    for link in deps_graph.get("links", []) or []:
        source = str(link.get("source") or "")
        target = str(link.get("target") or "")
        if source and target:
            deps_by_child.setdefault(target, set()).add(source)
    for node in nodes:
        node["_deps"] = sorted(deps_by_child.get(_node_id(node), set()))

    summary = candidate_graph.setdefault("architecture_summary", {})
    if isinstance(summary, dict):
        summary["dependency_link_count"] = len(deps_graph.get("links") or [])
        summary["link_count"] = (
            len((candidate_graph.get("hierarchy_graph") or {}).get("links") or [])
            + len((candidate_graph.get("evidence_graph") or {}).get("links") or [])
            + len(deps_graph.get("links") or [])
        )
    return {"ok": True, "dependency_link_count": len(deps_graph.get("links") or [])}


def _refresh_relationship_metrics_in_place(candidate_graph: dict[str, Any]) -> None:
    nodes = _deps_graph_nodes(candidate_graph)
    node_map = {_node_id(node): node for node in nodes if _node_id(node)}
    metrics: dict[str, dict[str, int]] = {
        node_id: {"fan_in": 0, "fan_out": 0, "hierarchy_in": 0, "hierarchy_out": 0}
        for node_id in node_map
    }
    hierarchy_graph = candidate_graph.get("hierarchy_graph") if isinstance(candidate_graph.get("hierarchy_graph"), dict) else {}
    for link in hierarchy_graph.get("links", []) or []:
        if not isinstance(link, dict) or str(link.get("type") or "") != "contains":
            continue
        source = str(link.get("source") or "")
        target = str(link.get("target") or "")
        if source in metrics:
            metrics[source]["hierarchy_out"] += 1
        if target in metrics:
            metrics[target]["hierarchy_in"] += 1
    deps_graph = candidate_graph.get("deps_graph") if isinstance(candidate_graph.get("deps_graph"), dict) else {}
    for link in deps_graph.get("links", []) or []:
        if not isinstance(link, dict):
            continue
        source = str(link.get("source") or "")
        target = str(link.get("target") or "")
        weight = max(1, int(link.get("evidence_count") or 1))
        if source in metrics:
            metrics[source]["fan_out"] += weight
        if target in metrics:
            metrics[target]["fan_in"] += weight
    for node in nodes:
        metadata = _node_metadata(node)
        flags = [
            str(flag)
            for flag in metadata.get("quality_flags", []) or []
            if str(flag) != "no_direct_dependency_edges"
        ]
        node_id = _node_id(node)
        node_metrics = metrics.get(node_id, {"fan_in": 0, "fan_out": 0, "hierarchy_in": 0, "hierarchy_out": 0})
        metadata["graph_metrics"] = node_metrics
        if (
            str(node.get("layer") or "").upper() == "L7"
            and node_metrics["fan_in"] == 0
            and node_metrics["fan_out"] == 0
        ):
            flags.append("no_direct_dependency_edges")
        metadata["quality_flags"] = sorted(set(flags))
        node["metadata"] = metadata


def _merge_function_facts_for_changed_modules(
    candidate_graph: dict[str, Any],
    modules: dict[str, ModuleInfo],
    changed_modules: set[str],
    project_root: str | Path | None = None,
) -> None:
    call_graph = build_call_graph(modules)
    graph_enrich_config_rules = None
    if project_root is not None:
        try:
            from agent.governance.reconcile_phases.phase_z_v2 import _load_graph_enrich_config_rules

            graph_enrich_config_rules = _load_graph_enrich_config_rules(project_root)
        except Exception:
            graph_enrich_config_rules = None
    facts = build_function_call_facts(
        modules,
        call_graph,
        graph_enrich_config_rules=graph_enrich_config_rules,
    )
    for node in _deps_graph_nodes(candidate_graph):
        metadata = _node_metadata(node)
        module_name = str(metadata.get("module") or node.get("module") or "")
        if not module_name:
            continue
        module_facts = facts.get(module_name) or {}
        old_called_by = [
            item for item in metadata.get("function_called_by", []) or []
            if str((item or {}).get("caller_module") or "") not in changed_modules
        ]
        new_called_by = [
            item for item in module_facts.get("called_by", []) or []
            if str((item or {}).get("caller_module") or "") in changed_modules
        ]
        if module_name in changed_modules:
            metadata["function_calls"] = list(module_facts.get("calls") or [])
            metadata["function_weak_calls"] = list(module_facts.get("weak_calls") or [])
            metadata["function_call_count"] = len(metadata["function_calls"])
            metadata["function_weak_call_count"] = len(metadata["function_weak_calls"])
        metadata["function_called_by"] = sorted(
            old_called_by + new_called_by,
            key=lambda item: (
                str((item or {}).get("callee") or ""),
                str((item or {}).get("caller") or ""),
            ),
        )
        metadata["function_called_by_count"] = len(metadata["function_called_by"])
        node["metadata"] = metadata


def _apply_incremental_source_dependency_delta(
    project_root: str | Path,
    candidate_graph: dict[str, Any],
    *,
    source_paths: list[str],
) -> dict[str, Any]:
    source_set = {
        _norm_repo_path(path)
        for path in source_paths
        if _norm_repo_path(path)
    }
    if not source_set:
        return {"ok": True, "source_paths": [], "updated_node_ids": []}
    modules, _nodes_by_module, node_ids_by_module = _active_module_index(project_root, candidate_graph)
    changed_modules: set[str] = set()
    updated_node_ids: set[str] = set()
    parsed_modules: dict[str, ModuleInfo] = {}
    typed_relations: list[dict[str, Any]] = []
    for path in sorted(source_set):
        node = _node_for_primary_path(candidate_graph, path)
        if not node:
            return {"ok": False, "reason": "source_primary_node_not_unique", "path": path}
        module = parse_production_module_file(str(Path(project_root).resolve()), path)
        if module is None:
            return {"ok": False, "reason": "source_adapter_parse_failed", "path": path}
        metadata = _node_metadata(node)
        module_name = str(metadata.get("module") or "")
        if not module_name or module.module_name != module_name:
            return {
                "ok": False,
                "reason": "source_module_identity_changed",
                "path": path,
                "expected_module": module_name,
                "parsed_module": module.module_name,
            }
        node_id = _node_id(node)
        if not node_id:
            return {"ok": False, "reason": "source_node_id_missing", "path": path}
        changed_modules.add(module_name)
        updated_node_ids.add(node_id)
        parsed_modules[module_name] = module
        modules[module_name] = module
        metadata["function_count"] = len(module.functions)
        metadata["functions"] = sorted(func.qualified_name for func in module.functions)
        metadata["function_lines"] = {
            func.name: [int(func.lineno or 0), int(func.end_lineno or func.lineno or 0)]
            for func in module.functions
            if func.name
        }
        metadata["function_hashes"] = function_source_hashes(module)
        temp_node = {"module": module_name}
        project_root_resolved = str(Path(project_root).resolve())
        module_relations = extract_typed_relations(
            project_root_resolved,
            {module_name: module},
            graph_enrich_config_rules=_load_graph_enrich_config_rules(project_root_resolved),
        )
        enrich_nodes_with_architecture_signals([temp_node], module_relations)
        metadata["typed_relations"] = temp_node.get("typed_relations") or []
        metadata["architecture_signals"] = temp_node.get("architecture_signals") or {}
        node["metadata"] = metadata
        typed_relations.extend(metadata["typed_relations"])

    asset_index = _asset_ids_by_key(candidate_graph)
    evidence_graph = candidate_graph.get("evidence_graph") if isinstance(candidate_graph.get("evidence_graph"), dict) else {}
    deps_graph = candidate_graph.get("deps_graph") if isinstance(candidate_graph.get("deps_graph"), dict) else {}
    changed_node_ids = {node_ids_by_module[module] for module in changed_modules if module in node_ids_by_module}
    if not changed_node_ids:
        return {"ok": False, "reason": "source_changed_node_missing"}

    evidence_links = [
        dict(link) for link in evidence_graph.get("links", []) or []
        if isinstance(link, dict)
        and not (
            _link_kind(link) == "module_dependency"
            and str(link.get("target") or "") in changed_node_ids
        )
        and not (
            _link_kind(link) == "typed_evidence"
            and str(link.get("source") or "") in changed_node_ids
        )
    ]
    dependency_links = [
        dict(link) for link in deps_graph.get("links", []) or []
        if isinstance(link, dict)
        and not (
            _link_kind(link) == "module_dependency"
            and str(link.get("target") or "") in changed_node_ids
        )
        and not (
            _link_kind(link) == "typed_dependency"
            and (
                str((link.get("metadata") or {}).get("evidence_source") or "") in changed_node_ids
                or str((link.get("metadata") or {}).get("evidence_target") or "") in changed_node_ids
                or str(link.get("source") or "") in changed_node_ids
                or str(link.get("target") or "") in changed_node_ids
            )
        )
    ]
    evidence_index = {
        (
            str(link.get("source") or ""),
            str(link.get("target") or ""),
            str(link.get("type") or ""),
        ): link
        for link in evidence_links
    }
    dependency_index = {
        (
            str(link.get("source") or ""),
            str(link.get("target") or ""),
            str(link.get("type") or ""),
        ): link
        for link in dependency_links
    }

    module_edges = [
        edge for edge in build_module_dependency_edges(modules, build_call_graph(modules))
        if str(edge.get("target_module") or "") in changed_modules
    ]
    for edge in module_edges:
        source_id = node_ids_by_module.get(str(edge.get("source_module") or ""), "")
        target_id = node_ids_by_module.get(str(edge.get("target_module") or ""), "")
        evidence = str(edge.get("evidence") or "")
        metadata = {
            "edge_kind": "module_dependency",
            "relation_type": edge.get("relation_type") or "",
        }
        _add_indexed_graph_link(evidence_links, evidence_index, source_id, target_id, "depends_on", evidence, metadata=metadata)
        _add_indexed_graph_link(dependency_links, dependency_index, source_id, target_id, "depends_on", evidence, metadata=metadata)

    for rel in typed_relations:
        module_name = str(rel.get("source_module") or "")
        source_id = node_ids_by_module.get(module_name, "")
        relation_type = str(rel.get("relation_type") or "")
        target_kind = str(rel.get("target_kind") or "")
        target = str(rel.get("target") or "")
        asset_key, aggregate_asset = _asset_identity_for_relation(target_kind, target)
        asset_entry = asset_index.get(asset_key)
        if not asset_entry:
            return {
                "ok": False,
                "reason": "source_typed_relation_asset_unknown",
                "asset_key": asset_key,
                "target_kind": target_kind,
                "target": target,
            }
        target_id, active_aggregate_asset = asset_entry
        aggregate_asset = aggregate_asset or active_aggregate_asset
        evidence = str(rel.get("evidence") or "")
        if aggregate_asset and target and asset_key.endswith("__artifact_assets"):
            evidence = " | ".join(part for part in [evidence, target] if part)
        _add_indexed_graph_link(
            evidence_links,
            evidence_index,
            source_id,
            target_id,
            relation_type,
            evidence,
            metadata={"edge_kind": "typed_evidence"},
        )
        if aggregate_asset:
            continue
        dep_source, dep_target = _dependency_direction_for_relation(source_id, target_id, relation_type)
        _add_indexed_graph_link(
            dependency_links,
            dependency_index,
            dep_source,
            dep_target,
            relation_type,
            evidence,
            metadata={
                "edge_kind": "typed_dependency",
                "evidence_source": source_id,
                "evidence_target": target_id,
            },
        )

    evidence_graph["links"] = sorted(evidence_links, key=lambda item: (
        str(item.get("source") or ""),
        str(item.get("target") or ""),
        str(item.get("type") or ""),
    ))
    deps_graph["links"] = sorted(dependency_links, key=lambda item: (
        str(item.get("source") or ""),
        str(item.get("target") or ""),
        str(item.get("type") or ""),
    ))
    _merge_function_facts_for_changed_modules(candidate_graph, modules, changed_modules, project_root)
    _refresh_candidate_dependency_links(candidate_graph)
    _refresh_relationship_metrics_in_place(candidate_graph)
    if _graph_links_have_cycle(deps_graph.get("links", []) or []):
        return {"ok": False, "reason": "source_dependency_delta_cycle_risk"}
    return {
        "ok": True,
        "source_paths": sorted(source_set),
        "updated_node_ids": sorted(updated_node_ids),
        "changed_modules": sorted(changed_modules),
        "module_dependency_edge_count": len(module_edges),
        "typed_relation_count": len(typed_relations),
    }


def _build_scope_graph_delta(
    *,
    old_graph_json: dict[str, Any],
    new_graph_json: dict[str, Any],
    scope_file_delta: dict[str, Any],
    strategy: str,
    mode: str,
    fallback_reason: str = "",
) -> dict[str, Any]:
    old_nodes = {_node_id(node): node for node in _graph_nodes(old_graph_json) if _node_id(node)}
    new_nodes = {_node_id(node): node for node in _graph_nodes(new_graph_json) if _node_id(node)}
    old_edges = {
        _edge_key(edge): edge
        for edge in graph_payload_edges(old_graph_json)
        if _edge_key(edge)[:3] != ("", "", "")
    }
    new_edges = {
        _edge_key(edge): edge
        for edge in graph_payload_edges(new_graph_json)
        if _edge_key(edge)[:3] != ("", "", "")
    }
    changed_paths = {
        str(path or "").replace("\\", "/").strip("/")
        for path in (
            list(scope_file_delta.get("hash_changed_files") or [])
            + list(scope_file_delta.get("status_changed_files") or [])
            + list(scope_file_delta.get("added_files") or [])
            + list(scope_file_delta.get("removed_files") or [])
        )
        if str(path or "").strip()
    }
    changed_node_ids = sorted(
        set(_node_ids_for_paths(old_graph_json, changed_paths))
        | set(_node_ids_for_paths(new_graph_json, changed_paths))
    )
    return {
        "strategy": strategy,
        "mode": mode,
        "fallback_reason": fallback_reason,
        "added_nodes": sorted(set(new_nodes) - set(old_nodes)),
        "updated_nodes": changed_node_ids,
        "removed_nodes": sorted(set(old_nodes) - set(new_nodes)),
        "added_edges": [
            _edge_delta_payload(key)
            for key in sorted(set(new_edges) - set(old_edges))
        ],
        "removed_edges": [
            _edge_delta_payload(key)
            for key in sorted(set(old_edges) - set(new_edges))
        ],
        "file_inventory_delta": {
            "added_files": list(scope_file_delta.get("added_files") or []),
            "removed_files": list(scope_file_delta.get("removed_files") or []),
            "hash_changed_files": list(scope_file_delta.get("hash_changed_files") or []),
            "status_changed_files": list(scope_file_delta.get("status_changed_files") or []),
            "impacted_files": list(scope_file_delta.get("impacted_files") or []),
            "changed_file_count": int(scope_file_delta.get("changed_file_count") or 0),
            "impacted_file_count": int(scope_file_delta.get("impacted_file_count") or 0),
        },
        "semantic_stale_node_ids": changed_node_ids,
    }


def _scope_event_id(event_type: str, target_type: str, target_id: str, payload: dict[str, Any]) -> str:
    raw = json.dumps(
        {
            "event_type": event_type,
            "target_type": target_type,
            "target_id": target_id,
            "payload": payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"scope-{event_type}-{digest}"


def _emit_scope_graph_events(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    old_snapshot_id: str,
    new_snapshot_id: str,
    old_graph_json: dict[str, Any],
    new_graph_json: dict[str, Any],
    scope_file_delta: dict[str, Any],
    baseline_commit: str,
    target_commit: str,
    created_by: str,
) -> dict[str, Any]:
    graph_events.ensure_schema(conn)
    old_nodes = {_node_id(node): node for node in _graph_nodes(old_graph_json) if _node_id(node)}
    new_nodes = {_node_id(node): node for node in _graph_nodes(new_graph_json) if _node_id(node)}
    changed_files = {
        str(path or "").replace("\\", "/").strip("/")
        for path in scope_file_delta.get("hash_changed_files", [])
        if str(path or "").strip()
    }
    emitted: list[dict[str, Any]] = []

    def emit(
        event_type: str,
        target_type: str,
        target_id: str,
        payload: dict[str, Any],
        *,
        stable_node_key: str = "",
        feature_hash: str = "",
        file_hashes: dict[str, Any] | None = None,
    ) -> None:
        event = graph_events.create_event(
            conn,
            project_id,
            new_snapshot_id,
            event_type=event_type,
            event_kind="scope_reconcile",
            target_type=target_type,
            target_id=target_id,
            status=graph_events.EVENT_STATUS_OBSERVED,
            confidence=1.0,
            baseline_commit=baseline_commit,
            target_commit=target_commit,
            stable_node_key=stable_node_key,
            feature_hash=feature_hash,
            file_hashes=file_hashes or {},
            payload=payload,
            evidence={
                "source": "scope_reconcile",
                "old_snapshot_id": old_snapshot_id,
                "new_snapshot_id": new_snapshot_id,
            },
            created_by=created_by,
            event_id=_scope_event_id(event_type, target_type, target_id, payload),
        )
        emitted.append(event)

    old_ids = set(old_nodes)
    new_ids = set(new_nodes)
    for node_id in sorted(new_ids - old_ids):
        node = new_nodes[node_id]
        metadata = _node_metadata(node)
        emit(
            "node_added",
            "node",
            node_id,
            {
                "node_id": node_id,
                "title": node.get("title") or "",
                "layer": node.get("layer") or "",
                "primary": _path_values(node, "primary"),
                "hierarchy_parent": _node_parent(node),
            },
            stable_node_key=str(metadata.get("stable_node_key") or ""),
            feature_hash=str(metadata.get("feature_hash") or ""),
        )
    for node_id in sorted(old_ids - new_ids):
        node = old_nodes[node_id]
        emit(
            "node_removed",
            "node",
            node_id,
            {
                "node_id": node_id,
                "title": node.get("title") or "",
                "layer": node.get("layer") or "",
                "primary": _path_values(node, "primary"),
                "hierarchy_parent": _node_parent(node),
            },
        )

    binding_events = {
        "secondary": ("doc_binding_added", "doc_binding_removed"),
        "test": ("test_binding_added", "test_binding_removed"),
        "config": ("config_binding_added", "config_binding_removed"),
    }
    for node_id in sorted(old_ids & new_ids):
        old_node = old_nodes[node_id]
        new_node = new_nodes[node_id]
        old_parent = _node_parent(old_node)
        new_parent = _node_parent(new_node)
        if old_parent != new_parent:
            emit(
                "node_reparented",
                "node",
                node_id,
                {"node_id": node_id, "old_parent": old_parent, "new_parent": new_parent},
            )
        metadata = _node_metadata(new_node)
        current_file_hashes = _node_file_hashes(new_node)
        for file_role in ("primary", "secondary", "test", "config"):
            role_changed = sorted(set(_path_values(new_node, file_role)).intersection(changed_files))
            if not role_changed:
                continue
            event_file_hashes = {
                path: current_file_hashes[path]
                for path in role_changed
                if path in current_file_hashes
            }
            emit(
                "file_hash_changed",
                "node",
                node_id,
                {"node_id": node_id, "files": role_changed, "file_role": file_role},
                stable_node_key=str(metadata.get("stable_node_key") or ""),
                feature_hash=str(metadata.get("feature_hash") or ""),
                file_hashes=event_file_hashes,
            )
        old_meta = _node_metadata(old_node)
        new_meta = metadata
        if new_meta.get("exclude_as_feature") is True and old_meta.get("exclude_as_feature") is not True:
            emit(
                "package_marker_excluded",
                "node",
                node_id,
                {
                    "node_id": node_id,
                    "primary": _path_values(new_node, "primary"),
                    "file_role": new_meta.get("file_role") or "",
                },
            )
        for key, (added_type, removed_type) in binding_events.items():
            old_paths = set(_path_values(old_node, key))
            new_paths = set(_path_values(new_node, key))
            for path in sorted(new_paths - old_paths):
                emit(added_type, "node", node_id, {"node_id": node_id, "path": path, "binding": key})
            for path in sorted(old_paths - new_paths):
                emit(removed_type, "node", node_id, {"node_id": node_id, "path": path, "binding": key})

    old_edges = {_edge_key(edge): edge for edge in graph_payload_edges(old_graph_json) if _edge_key(edge)[:3] != ("", "", "")}
    new_edges = {_edge_key(edge): edge for edge in graph_payload_edges(new_graph_json) if _edge_key(edge)[:3] != ("", "", "")}
    for key in sorted(set(new_edges) - set(old_edges)):
        source, target, relation, edge_kind = key
        emit(
            "edge_added",
            "edge",
            f"{source}->{target}:{relation}",
            {
                "source": source,
                "target": target,
                "relation_type": relation,
                "edge_kind": edge_kind,
                "edge": new_edges[key],
            },
        )
    for key in sorted(set(old_edges) - set(new_edges)):
        source, target, relation, edge_kind = key
        emit(
            "edge_removed",
            "edge",
            f"{source}->{target}:{relation}",
            {
                "source": source,
                "target": target,
                "relation_type": relation,
                "edge_kind": edge_kind,
                "edge": old_edges[key],
            },
        )

    by_type: dict[str, int] = {}
    for event in emitted:
        event_type = str(event.get("event_type") or "")
        by_type[event_type] = by_type.get(event_type, 0) + 1
    return {
        "enabled": True,
        "event_count": len(emitted),
        "by_type": dict(sorted(by_type.items())),
        "snapshot_id": new_snapshot_id,
        "old_snapshot_id": old_snapshot_id,
        "target_commit": target_commit,
    }


def _semantic_enrichment_summary(result: dict[str, Any] | None) -> dict[str, Any]:
    if not result:
        return {}
    summary = dict(result.get("summary") or {})
    return {
        "ok": bool(result.get("ok")),
        "feedback_round": result.get("feedback_round", 0),
        "semantic_index_path": result.get("semantic_index_path", ""),
        "review_report_path": result.get("review_report_path", ""),
        "round_semantic_index_path": result.get("round_semantic_index_path", ""),
        "round_review_report_path": result.get("round_review_report_path", ""),
        "feature_count": summary.get("feature_count", 0),
        "semantic_run_status": summary.get("semantic_run_status", ""),
        "ai_complete_count": summary.get("ai_complete_count", 0),
        "ai_unavailable_count": summary.get("ai_unavailable_count", 0),
        "ai_error_count": summary.get("ai_error_count", 0),
        "ai_skipped_count": summary.get("ai_skipped_count", 0),
        "feedback_count": summary.get("feedback_count", 0),
        "unresolved_feedback_count": summary.get("unresolved_feedback_count", 0),
        "quality_flag_counts": summary.get("quality_flag_counts") or {},
        "feature_payload_input_count": summary.get("feature_payload_input_count", 0),
        "feature_payload_output_count": summary.get("feature_payload_output_count", 0),
        "feature_payload_input_dir": summary.get("feature_payload_input_dir", ""),
        "feature_payload_output_dir": summary.get("feature_payload_output_dir", ""),
        "batch_payload_input_dir": summary.get("batch_payload_input_dir", ""),
        "batch_payload_output_dir": summary.get("batch_payload_output_dir", ""),
        "ai_selected_count": summary.get("ai_selected_count", 0),
        "ai_attempted_count": summary.get("ai_attempted_count", 0),
        "ai_skipped_selector_count": summary.get("ai_skipped_selector_count", 0),
        "semantic_hash_mismatch_count": summary.get("semantic_hash_mismatch_count", 0),
        "ai_input_mode": summary.get("ai_input_mode", ""),
        "dynamic_semantic_graph_state": summary.get("dynamic_semantic_graph_state", False),
        "requested_ai_batch_size": summary.get("requested_ai_batch_size"),
        "ai_batch_size": summary.get("ai_batch_size", 1),
        "ai_batch_by": summary.get("ai_batch_by", ""),
        "ai_batch_count": summary.get("ai_batch_count", 0),
        "ai_batch_complete_count": summary.get("ai_batch_complete_count", 0),
        "ai_batch_error_count": summary.get("ai_batch_error_count", 0),
        "semantic_graph_state": summary.get("semantic_graph_state") or {},
        "semantic_batch_memory": summary.get("semantic_batch_memory") or {},
        "semantic_selector": summary.get("semantic_selector") or {},
    }


def run_state_only_full_reconcile(
    conn: sqlite3.Connection,
    project_id: str,
    project_root: str | Path,
    *,
    run_id: str = "",
    commit_sha: str = "",
    snapshot_id: str | None = None,
    snapshot_kind: str = "full",
    created_by: str = "observer",
    activate: bool = False,
    expected_old_snapshot_id: str | None = None,
    ref_name: str = "active",
    branch_ref: str = "",
    notes_extra: dict[str, Any] | None = None,
    semantic_enrich: bool = True,
    semantic_use_ai: bool | None = None,
    semantic_feedback_items: list[dict[str, Any]] | dict[str, Any] | None = None,
    semantic_feedback_round: int | None = None,
    semantic_max_excerpt_chars: int | None = None,
    semantic_ai_call: Any = None,
    semantic_ai_feature_limit: int | None = None,
    semantic_ai_provider: str | None = None,
    semantic_ai_model: str | None = None,
    semantic_ai_role: str | None = None,
    semantic_ai_chain_role: str | None = None,
    semantic_analyzer_role: str | None = None,
    semantic_ai_scope: str | None = None,
    semantic_node_ids: Any = None,
    semantic_layers: Any = None,
    semantic_quality_flags: Any = None,
    semantic_missing: Any = None,
    semantic_changed_paths: Any = None,
    semantic_path_prefixes: Any = None,
    semantic_selector_match: str | None = None,
    semantic_include_structural: bool = False,
    semantic_ai_batch_size: int | None = None,
    semantic_ai_batch_by: str = "subsystem",
    semantic_ai_input_mode: str | None = None,
    semantic_dynamic_graph_state: bool | None = None,
    semantic_graph_state: bool = True,
    semantic_skip_completed: bool = True,
    semantic_classify_feedback: bool = True,
    semantic_batch_memory: bool | None = False,
    semantic_batch_memory_id: str | None = None,
    semantic_base_snapshot_id: str | None = None,
    semantic_config_path: str | Path | None = None,
    semantic_enqueue_stale: bool = True,
    graph_exclude_paths: Any = None,
    graph_ignore_globs: Any = None,
) -> dict[str, Any]:
    """Create a candidate full-reconcile graph snapshot from current files.

    The function writes only governance artifacts under shared governance state.
    It leaves repository files untouched and keeps activation optional.
    """
    ensure_graph_snapshot_schema(conn)
    root = Path(project_root).resolve()
    commit = commit_sha or _git_commit(root) or "unknown"
    sid = snapshot_id or snapshot_id_for(snapshot_kind, commit)
    rid = run_id or sid
    checkout_provenance = describe_checkout(
        root,
        project_id=project_id,
        commit_sha=commit,
    )
    identity = normalize_pending_scope_identity(ref_name=ref_name, branch_ref=branch_ref)
    activation_ref_name = identity["ref_name"]
    activation_branch_ref = identity["branch_ref"]
    state_dir = _governance_state_dir(project_id, rid)
    scratch_dir = state_dir / "scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    trace = ReconcileTrace(
        project_id=project_id,
        run_id=rid,
        snapshot_id=sid,
        trace_dir=state_dir / "trace",
    )
    trace.step(
        "run-input",
        input_payload={
            "project_id": project_id,
            "project_root": str(root),
            "project_root_role": "execution_root",
            "checkout_provenance": checkout_provenance,
            "run_id": rid,
            "snapshot_id": sid,
            "snapshot_kind": snapshot_kind,
            "commit_sha": commit,
            "created_by": created_by,
            "ref_name": activation_ref_name,
            "branch_ref": activation_branch_ref,
        },
        output_payload={
            "state_dir": str(state_dir),
            "scratch_dir": str(scratch_dir),
            "semantic_enrich": semantic_enrich,
            "semantic_use_ai": semantic_use_ai,
            "semantic_ai_feature_limit": semantic_ai_feature_limit,
            "semantic_ai_batch_size": semantic_ai_batch_size,
            "semantic_ai_batch_by": semantic_ai_batch_by,
            "semantic_ai_input_mode": semantic_ai_input_mode,
            "semantic_dynamic_graph_state": semantic_dynamic_graph_state,
            "semantic_graph_state": semantic_graph_state,
            "semantic_skip_completed": semantic_skip_completed,
            "semantic_batch_memory": semantic_batch_memory,
            "semantic_base_snapshot_id": semantic_base_snapshot_id,
            "semantic_ai_provider": semantic_ai_provider,
            "semantic_ai_model": semantic_ai_model,
            "semantic_ai_role": semantic_ai_role,
            "semantic_ai_chain_role": semantic_ai_chain_role,
            "semantic_analyzer_role": semantic_analyzer_role,
            "semantic_ai_scope": semantic_ai_scope,
            "semantic_node_ids": semantic_node_ids,
            "semantic_layers": semantic_layers,
            "semantic_quality_flags": semantic_quality_flags,
            "semantic_missing": semantic_missing,
            "graph_exclude_paths": graph_exclude_paths or [],
            "graph_ignore_globs": graph_ignore_globs or [],
        },
    )

    phase_result = build_graph_v2_from_symbols(
        str(root),
        dry_run=True,
        scratch_dir=str(scratch_dir),
        run_id=rid,
        extra_exclude_roots=list(graph_exclude_paths or []),
        extra_ignore_globs=list(graph_ignore_globs or []),
    )
    trace.step(
        "build-graph-v2",
        input_payload={
            "project_root": str(root),
            "dry_run": True,
            "scratch_dir": str(scratch_dir),
            "run_id": rid,
        },
        output_payload={
            "status": phase_result.get("status", ""),
            "report_path": phase_result.get("report_path") or "",
            "report": artifact_ref(phase_result.get("report_path") or ""),
            "node_count": phase_result.get("node_count", 0),
            "feature_cluster_count": len(phase_result.get("feature_clusters") or []),
            "file_inventory_summary": phase_result.get("file_inventory_summary") or {},
            "typed_relation_count": len(phase_result.get("typed_relations") or []),
        },
        status="ok" if phase_result.get("status") == "ok" else "failed",
    )
    if phase_result.get("status") != "ok":
        trace_summary = trace.finalize(status="failed", extra={"abort_reason": phase_result.get("abort_reason", "")})
        return {
            "ok": False,
            "project_id": project_id,
            "run_id": rid,
            "commit_sha": commit,
            "status": phase_result.get("status", "unknown"),
            "abort_reason": phase_result.get("abort_reason", ""),
            "phase_result": phase_result,
            "trace": trace_summary,
        }

    candidate_graph = build_rebase_candidate_graph(
        str(root),
        phase_result,
        session_id=rid,
        run_id=rid,
    )
    trace.step(
        "build-candidate-graph",
        input_payload={
            "phase_report_path": phase_result.get("report_path") or "",
            "session_id": rid,
        },
        output_payload={
            "graph_stats": graph_payload_stats(candidate_graph),
        },
    )
    active_snapshot_for_corrections = get_active_graph_snapshot(conn, project_id) or {}
    role_annotation = annotate_graph_node_roles(candidate_graph)
    candidate_graph = role_annotation["graph"]
    relationship_metrics = annotate_graph_relationship_metrics(candidate_graph)
    candidate_graph = relationship_metrics["graph"]
    ensure_graph_correction_schema(conn)
    replayable_patches = list_replayable_patches(conn, project_id)
    patch_application = apply_correction_patches(
        candidate_graph,
        replayable_patches,
        from_snapshot_id=str(active_snapshot_for_corrections.get("snapshot_id") or ""),
        to_snapshot_id=sid,
    )
    candidate_graph = patch_application["graph"]
    trace.step(
        "graph-correction-patches",
        input_payload={
            "active_snapshot_id": active_snapshot_for_corrections.get("snapshot_id", ""),
            "patch_count": len(replayable_patches),
        },
        output_payload={
            "file_role_annotation": role_annotation["report"],
            "relationship_metrics": relationship_metrics["report"],
            "patch_report": patch_application["report"],
            "graph_stats": graph_payload_stats(candidate_graph),
        },
    )
    hint_projection_state = _project_graph_structure_hints(root, candidate_graph)
    hint_index = hint_projection_state["hint_index"]
    hint_projection = hint_projection_state["projection"]
    candidate_graph = hint_projection["graph"]
    graph_structure_hint_report = {
        "status": hint_projection.get("status", ""),
        "hint_count": hint_index.get("hint_count", 0),
        "materialized_count": hint_projection.get("materialized_count", 0),
        "conflict_count": hint_projection.get("conflict_count", 0),
        "hint_states": hint_projection.get("hint_states") or {},
        "suppressed_edges": hint_projection.get("suppressed_edges") or [],
    }
    trace.step(
        "graph-structure-hints",
        input_payload={
            "project_root": str(root),
            "hint_count": hint_index.get("hint_count", 0),
            "hints": hint_index.get("hints") or [],
        },
        output_payload={
            "report": graph_structure_hint_report,
            "graph_stats": graph_payload_stats(candidate_graph),
        },
        status="ok" if hint_projection.get("status") == "ok" else "warning",
    )
    file_inventory = _normalize_inventory_commit(
        [
            row for row in (phase_result.get("file_inventory") or [])
            if isinstance(row, dict)
        ],
        commit_sha=commit,
    )
    trace.step(
        "normalize-file-inventory",
        input_payload={
            "raw_file_inventory_count": len(phase_result.get("file_inventory") or []),
            "commit_sha": commit,
        },
        output_payload={
            "file_inventory_count": len(file_inventory),
            "file_inventory_summary": phase_result.get("file_inventory_summary") or {},
        },
    )
    nodes = _deps_graph_nodes(candidate_graph)
    edges = _deps_graph_edges(candidate_graph)
    rule_fingerprint = build_graph_rule_fingerprint(
        root,
        commit_sha=commit,
        hint_index=hint_index,
        semantic_config_path=semantic_config_path,
    )
    notes = {
        "state_only": True,
        "run_id": rid,
        "snapshot_kind": snapshot_kind,
        "phase_report_path": phase_result.get("report_path") or "",
        "phase_node_count": phase_result.get("node_count", 0),
        "feature_cluster_count": len(phase_result.get("feature_clusters") or []),
        "file_inventory_summary": phase_result.get("file_inventory_summary") or {},
        "file_role_annotation": role_annotation["report"],
        "relationship_metrics": relationship_metrics["report"],
        "graph_correction_patch_report": patch_application["report"],
        "graph_structure_hint_projection": graph_structure_hint_report,
        "graph_rule_fingerprint": rule_fingerprint,
        "checkout_provenance": checkout_provenance,
        **(notes_extra or {}),
    }
    if snapshot_kind == "full":
        notes["full_reconcile_anchor"] = build_full_reconcile_anchor(
            project_id=project_id,
            snapshot_id=sid,
            anchor_commit=commit,
            rule_fingerprint=rule_fingerprint,
            reconcile_mode="full",
        )
    notes["trace"] = {
        "trace_dir": str(trace.trace_dir),
        "summary_path": str(trace.trace_dir / "summary.json"),
    }
    governance_index = build_governance_index(
        conn,
        project_id,
        root,
        run_id=rid,
        commit_sha=commit,
        candidate_graph=candidate_graph,
        snapshot_id=sid,
        snapshot_kind=snapshot_kind,
        file_inventory=file_inventory,
    )
    hash_metadata_merge = merge_feature_hashes_into_graph_nodes(candidate_graph, governance_index)
    enriched_inventory = governance_index.get("file_inventory")
    if isinstance(enriched_inventory, list):
        file_inventory = _normalize_inventory_commit(
            [row for row in enriched_inventory if isinstance(row, dict)],
            commit_sha=commit,
        )
        notes["file_inventory_summary"] = governance_index.get("file_inventory_summary") or {}
    notes["governance_hint_bindings"] = governance_index.get("governance_hint_bindings") or {}
    notes["governance_index_hash_metadata"] = hash_metadata_merge
    nodes = _deps_graph_nodes(candidate_graph)
    edges = _deps_graph_edges(candidate_graph)
    with sqlite_write_lock():
        snapshot = create_graph_snapshot(
            conn,
            project_id,
            snapshot_id=sid,
            commit_sha=commit,
            snapshot_kind=snapshot_kind,
            ref_name=activation_ref_name,
            branch_ref=activation_branch_ref,
            graph_json=candidate_graph,
            file_inventory=file_inventory,
            drift_ledger=[],
            status=SNAPSHOT_STATUS_CANDIDATE,
            created_by=created_by,
            notes=json.dumps(notes, ensure_ascii=False, sort_keys=True),
        )
        index_counts = index_graph_snapshot(
            conn,
            project_id,
            sid,
            nodes=nodes,
            edges=edges,
        )
        governance_index_summary = persist_governance_index(
            conn,
            project_id,
            governance_index,
            persist_inventory=True,
        )
        migration_count = persist_node_migrations(
            conn,
            project_id,
            from_snapshot_id=str(active_snapshot_for_corrections.get("snapshot_id") or ""),
            to_snapshot_id=sid,
            migrations=patch_application["report"].get("migrations") or [],
        )
        patch_apply_counts = record_patch_apply_report(
            conn,
            project_id,
            snapshot_id=sid,
            report=patch_application["report"],
        )
        notes["graph_correction_patch_report"]["migration_count"] = migration_count
        notes["graph_correction_patch_report"]["patch_apply_counts"] = patch_apply_counts
        notes["governance_index"] = governance_index_summary
        conn.execute(
            "UPDATE graph_snapshots SET notes = ? WHERE project_id = ? AND snapshot_id = ?",
            (json.dumps(notes, ensure_ascii=False, sort_keys=True), project_id, sid),
        )
        conn.commit()
    governance_index = {**governance_index, "persist_summary": governance_index_summary}
    trace.step(
        "create-graph-snapshot",
        input_payload={
            "snapshot_id": sid,
            "snapshot_kind": snapshot_kind,
            "commit_sha": commit,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "file_inventory_count": len(file_inventory),
        },
        output_payload={
            "snapshot": snapshot,
            "snapshot_path": str(snapshot_graph_path(project_id, sid)),
            "snapshot_artifact": artifact_ref(snapshot_graph_path(project_id, sid)),
        },
    )
    trace.step(
        "index-graph-snapshot",
        input_payload={
            "snapshot_id": sid,
            "node_count": len(nodes),
            "edge_count": len(edges),
        },
        output_payload={"index_counts": index_counts},
    )
    trace.step(
        "build-governance-index",
        input_payload={
            "snapshot_id": sid,
            "run_id": rid,
            "commit_sha": commit,
        },
        output_payload={
            "summary": governance_index_summary,
            "artifacts": governance_index_summary.get("artifacts") or {},
            "hash_metadata_merge": hash_metadata_merge,
        },
    )
    semantic_enrichment: dict[str, Any] = {}
    if semantic_enrich:
        semantic_result = run_semantic_enrichment(
            conn,
            project_id,
            sid,
            root,
            feedback_items=semantic_feedback_items,
            feedback_round=semantic_feedback_round,
            use_ai=semantic_use_ai,
            ai_call=semantic_ai_call,
            created_by=created_by,
            max_excerpt_chars=semantic_max_excerpt_chars,
            semantic_ai_provider=semantic_ai_provider,
            semantic_ai_model=semantic_ai_model,
            semantic_ai_role=semantic_ai_role,
            semantic_ai_chain_role=semantic_ai_chain_role,
            semantic_analyzer_role=semantic_analyzer_role,
            ai_feature_limit=semantic_ai_feature_limit,
            semantic_ai_batch_size=semantic_ai_batch_size,
            semantic_ai_batch_by=semantic_ai_batch_by,
            semantic_ai_input_mode=semantic_ai_input_mode,
            semantic_dynamic_graph_state=semantic_dynamic_graph_state,
            semantic_graph_state=semantic_graph_state,
            semantic_skip_completed=semantic_skip_completed,
            semantic_batch_memory=semantic_batch_memory,
            semantic_batch_memory_id=semantic_batch_memory_id,
            semantic_base_snapshot_id=semantic_base_snapshot_id,
            semantic_ai_scope=semantic_ai_scope,
            semantic_node_ids=semantic_node_ids,
            semantic_layers=semantic_layers,
            semantic_quality_flags=semantic_quality_flags,
            semantic_missing=semantic_missing,
            semantic_changed_paths=semantic_changed_paths,
            semantic_path_prefixes=semantic_path_prefixes,
            semantic_selector_match=semantic_selector_match,
            semantic_include_structural=semantic_include_structural,
            semantic_config_path=semantic_config_path,
            trace_dir=trace.trace_dir / "semantic-enrichment",
            enqueue_stale=semantic_enqueue_stale,
        )
        semantic_enrichment = _semantic_enrichment_summary(semantic_result)
        if semantic_classify_feedback:
            from agent.governance import reconcile_feedback
            from agent.governance import reconcile_semantic_enrichment

            review_gate = reconcile_semantic_enrichment.feedback_review_gate(
                semantic_result.get("summary") or {},
            )
            if review_gate.get("allowed"):
                semantic_enrichment["feedback_queue"] = reconcile_feedback.classify_semantic_state_rounds(
                    project_id,
                    sid,
                    created_by=created_by,
                    base_snapshot_id=semantic_base_snapshot_id or "",
                )
            else:
                semantic_enrichment["feedback_queue"] = {
                    "blocked": True,
                    "gate": review_gate,
                }
    trace.step(
        "semantic-enrichment",
        input_payload={
            "enabled": semantic_enrich,
            "snapshot_id": sid,
            "semantic_use_ai": semantic_use_ai,
            "semantic_ai_feature_limit": semantic_ai_feature_limit,
            "semantic_ai_batch_size": semantic_ai_batch_size,
            "semantic_ai_batch_by": semantic_ai_batch_by,
            "semantic_ai_input_mode": semantic_ai_input_mode,
            "semantic_dynamic_graph_state": semantic_dynamic_graph_state,
            "semantic_graph_state": semantic_graph_state,
            "semantic_skip_completed": semantic_skip_completed,
            "semantic_classify_feedback": semantic_classify_feedback,
            "semantic_batch_memory": semantic_batch_memory,
            "semantic_base_snapshot_id": semantic_base_snapshot_id,
            "semantic_ai_provider": semantic_ai_provider,
            "semantic_ai_model": semantic_ai_model,
            "semantic_ai_role": semantic_ai_role,
            "semantic_ai_chain_role": semantic_ai_chain_role,
            "semantic_analyzer_role": semantic_analyzer_role,
            "semantic_ai_scope": semantic_ai_scope,
            "semantic_node_ids": semantic_node_ids,
            "semantic_layers": semantic_layers,
            "semantic_quality_flags": semantic_quality_flags,
            "semantic_missing": semantic_missing,
            "semantic_changed_paths": semantic_changed_paths,
            "semantic_path_prefixes": semantic_path_prefixes,
            "semantic_selector_match": semantic_selector_match,
            "semantic_include_structural": semantic_include_structural,
            "semantic_config_path": str(semantic_config_path or ""),
        },
        output_payload=semantic_enrichment,
    )
    activation = None
    pending_scope_waiver = {
        "project_id": project_id,
        "waived_count": 0,
        "commit_shas": [],
        "snapshot_id": sid,
    }
    if activate:
        with sqlite_write_lock():
            pending_scope_commits: list[str] = []
            if str(snapshot_kind or "").strip().lower() == "full":
                pending_scope_rows = list_pending_scope_reconcile(
                    conn,
                    project_id,
                    statuses=[
                        PENDING_STATUS_QUEUED,
                        PENDING_STATUS_RUNNING,
                        PENDING_STATUS_FAILED,
                    ],
                    ref_name=activation_ref_name,
                    branch_ref=activation_branch_ref,
                    worktree_id="",
                    worktree_path="",
                )
                pending_scope_commits = _full_reconcile_pending_scope_waiver_commits(
                    root,
                    pending_scope_rows,
                    commit,
                )
            activation = activate_graph_snapshot(
                conn,
                project_id,
                sid,
                expected_old_snapshot_id=expected_old_snapshot_id,
                ref_name=activation_ref_name,
                branch_ref=activation_branch_ref,
            )
            if pending_scope_commits:
                pending_scope_waiver = waive_pending_scope_reconcile(
                    conn,
                    project_id,
                    commit_shas=pending_scope_commits,
                    ref_name=activation_ref_name,
                    branch_ref=activation_branch_ref,
                    worktree_id="",
                    worktree_path="",
                    snapshot_id=sid,
                    actor=created_by,
                    reason="full reconcile activated current graph snapshot",
                    evidence={
                        "source": "full_reconcile_activation",
                        "run_id": rid,
                        "snapshot_kind": snapshot_kind,
                        "target_commit_sha": commit,
                    },
                )
            conn.commit()
        trace.step(
            "activate-snapshot",
            input_payload={
                "snapshot_id": sid,
                "expected_old_snapshot_id": expected_old_snapshot_id,
                "ref_name": activation_ref_name,
                "branch_ref": activation_branch_ref,
            },
            output_payload={
                "activation": activation,
                "pending_scope_waiver": pending_scope_waiver,
            },
        )
    else:
        trace.step(
            "activate-snapshot",
            input_payload={"snapshot_id": sid, "activate": False},
            output_payload={
                "activation": None,
                "status": "skipped",
                "pending_scope_waiver": pending_scope_waiver,
            },
            status="skipped",
        )
    trace_summary = trace.finalize(status="ok")
    return {
        "ok": True,
        "project_id": project_id,
        "run_id": rid,
        "commit_sha": commit,
        "snapshot_id": sid,
        "snapshot_status": "active" if activation else SNAPSHOT_STATUS_CANDIDATE,
        "snapshot_path": str(snapshot_graph_path(project_id, sid)),
        "phase_report_path": phase_result.get("report_path") or "",
        "graph_stats": graph_payload_stats(candidate_graph),
        "index_counts": index_counts,
        "governance_index": governance_index_summary,
        "semantic_enrichment": semantic_enrichment,
        "trace": trace_summary,
        "file_inventory_count": len(file_inventory),
        "file_inventory_summary": phase_result.get("file_inventory_summary") or {},
        "feature_cluster_count": len(phase_result.get("feature_clusters") or []),
        "snapshot": snapshot,
        "activation": activation,
        "pending_scope_waiver": pending_scope_waiver,
    }


def _pending_commits_through_target(
    pending: list[dict[str, Any]],
    target_commit_sha: str,
) -> list[str]:
    commits = [
        str(row.get("commit_sha") or "").strip()
        for row in pending
        if str(row.get("commit_sha") or "").strip()
    ]
    if target_commit_sha in commits:
        return commits[: commits.index(target_commit_sha) + 1]
    return commits


def _git_commit_is_ancestor(
    project_root: str | Path,
    ancestor_ref: str,
    target_ref: str,
) -> bool | None:
    ancestor = str(ancestor_ref or "").strip()
    target = str(target_ref or "").strip()
    if not ancestor or not target:
        return None
    if ancestor == target:
        return True
    root = Path(project_root).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", ancestor, target],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def _git_ref_resolves(project_root: str | Path, ref: str) -> bool:
    target = str(ref or "").strip()
    if not target:
        return False
    root = Path(project_root).resolve()
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", f"{target}^{{commit}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return False
    return result.returncode == 0


def _full_reconcile_pending_scope_waiver_commits(
    project_root: str | Path,
    pending: list[dict[str, Any]],
    target_commit_sha: str,
) -> list[str]:
    commits = [
        str(row.get("commit_sha") or "").strip()
        for row in pending
        if str(row.get("commit_sha") or "").strip()
    ]
    if not commits:
        return []
    target = str(target_commit_sha or "").strip()
    if not _git_ref_resolves(project_root, target):
        return _pending_commits_through_target(pending, target)
    ancestor_checked = False
    covered: list[str] = []
    for commit in commits:
        is_ancestor = _git_commit_is_ancestor(project_root, commit, target)
        if is_ancestor is None:
            continue
        ancestor_checked = True
        if is_ancestor:
            covered.append(commit)
    if ancestor_checked:
        return covered
    return _pending_commits_through_target(pending, target)


def _update_pending_scope_candidate(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    covered_commit_shas: list[str],
    snapshot_id: str,
    target_commit_sha: str,
    run_id: str,
    activated: bool = False,
    ref_name: str = "active",
    branch_ref: str = "",
    worktree_id: str = "",
    worktree_path: str = "",
) -> int:
    """Mark pending_scope_reconcile rows bound to the just-built candidate.

    OPT-BACKLOG-PENDING-SCOPE-TRANSITION-MISSING: when `activated` is True the
    candidate snapshot has already replaced the active snapshot, so the
    pending row is fully materialized and shouldn't stay in `running`. When
    False the candidate is parked awaiting an explicit activate call, so
    `running` is the correct interim state.
    """
    commits = [c for c in covered_commit_shas if c]
    if not commits:
        return 0
    placeholders = ",".join("?" for _ in commits)
    final_status = PENDING_STATUS_MATERIALIZED if activated else PENDING_STATUS_RUNNING
    identity = normalize_pending_scope_identity(
        ref_name=ref_name,
        branch_ref=branch_ref,
        worktree_id=worktree_id,
        worktree_path=worktree_path,
    )
    evidence = {
        "source": "pending_scope_materializer",
        "snapshot_id": snapshot_id,
        "target_commit_sha": target_commit_sha,
        "run_id": run_id,
        "covered_commit_shas": commits,
        "activated": bool(activated),
        "final_status": final_status,
        **identity,
    }
    cur = conn.execute(
        f"""
        UPDATE pending_scope_reconcile
        SET status = ?,
            snapshot_id = ?,
            evidence_json = ?
        WHERE project_id = ?
          AND ref_name = ?
          AND worktree_id = ?
          AND commit_sha IN ({placeholders})
          AND status IN (?, ?, ?)
        """,
        (
            final_status,
            snapshot_id,
            json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            project_id,
            identity["ref_name"],
            identity["worktree_id"],
            *commits,
            PENDING_STATUS_QUEUED,
            PENDING_STATUS_RUNNING,
            PENDING_STATUS_FAILED,
        ),
    )
    return int(cur.rowcount or 0)


def _has_semantic_selector_override(*values: Any) -> bool:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, tuple, set, dict)) and not value:
            continue
        return True
    return False


def _run_scope_semantic_enrichment(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    root: Path,
    *,
    created_by: str,
    semantic_options: dict[str, Any],
    trace: ReconcileTrace,
) -> dict[str, Any]:
    semantic_enrich = bool(semantic_options.get("semantic_enrich", True))
    semantic_enrichment: dict[str, Any] = {}
    if semantic_enrich:
        semantic_result = run_semantic_enrichment(
            conn,
            project_id,
            snapshot_id,
            root,
            feedback_items=semantic_options.get("semantic_feedback_items"),
            feedback_round=semantic_options.get("semantic_feedback_round"),
            use_ai=semantic_options.get("semantic_use_ai"),
            ai_call=semantic_options.get("semantic_ai_call"),
            created_by=created_by,
            max_excerpt_chars=semantic_options.get("semantic_max_excerpt_chars"),
            semantic_ai_provider=semantic_options.get("semantic_ai_provider"),
            semantic_ai_model=semantic_options.get("semantic_ai_model"),
            semantic_ai_role=semantic_options.get("semantic_ai_role"),
            semantic_ai_chain_role=semantic_options.get("semantic_ai_chain_role"),
            semantic_analyzer_role=semantic_options.get("semantic_analyzer_role"),
            ai_feature_limit=semantic_options.get("semantic_ai_feature_limit"),
            semantic_ai_batch_size=semantic_options.get("semantic_ai_batch_size"),
            semantic_ai_batch_by=semantic_options.get("semantic_ai_batch_by", "subsystem"),
            semantic_ai_input_mode=semantic_options.get("semantic_ai_input_mode"),
            semantic_dynamic_graph_state=semantic_options.get("semantic_dynamic_graph_state"),
            semantic_graph_state=bool(semantic_options.get("semantic_graph_state", True)),
            semantic_skip_completed=bool(semantic_options.get("semantic_skip_completed", True)),
            semantic_batch_memory=semantic_options.get("semantic_batch_memory", False),
            semantic_batch_memory_id=semantic_options.get("semantic_batch_memory_id"),
            semantic_base_snapshot_id=semantic_options.get("semantic_base_snapshot_id") or "",
            semantic_ai_scope=semantic_options.get("semantic_ai_scope"),
            semantic_node_ids=semantic_options.get("semantic_node_ids"),
            semantic_layers=semantic_options.get("semantic_layers"),
            semantic_quality_flags=semantic_options.get("semantic_quality_flags"),
            semantic_missing=semantic_options.get("semantic_missing"),
            semantic_changed_paths=semantic_options.get("semantic_changed_paths"),
            semantic_path_prefixes=semantic_options.get("semantic_path_prefixes"),
            semantic_selector_match=semantic_options.get("semantic_selector_match"),
            semantic_include_structural=bool(semantic_options.get("semantic_include_structural", False)),
            semantic_config_path=semantic_options.get("semantic_config_path"),
            trace_dir=trace.trace_dir / "semantic-enrichment",
            enqueue_stale=bool(semantic_options.get("semantic_enqueue_stale", True)),
        )
        semantic_enrichment = _semantic_enrichment_summary(semantic_result)
        if bool(semantic_options.get("semantic_classify_feedback", True)):
            from agent.governance import reconcile_feedback
            from agent.governance import reconcile_semantic_enrichment

            review_gate = reconcile_semantic_enrichment.feedback_review_gate(
                semantic_result.get("summary") or {},
            )
            if review_gate.get("allowed"):
                semantic_enrichment["feedback_queue"] = reconcile_feedback.classify_semantic_state_rounds(
                    project_id,
                    snapshot_id,
                    created_by=created_by,
                    base_snapshot_id=semantic_options.get("semantic_base_snapshot_id") or "",
                )
            else:
                semantic_enrichment["feedback_queue"] = {
                    "blocked": True,
                    "gate": review_gate,
                }
    trace.step(
        "semantic-enrichment",
        input_payload={
            "enabled": semantic_enrich,
            "snapshot_id": snapshot_id,
            "semantic_use_ai": semantic_options.get("semantic_use_ai"),
            "semantic_ai_feature_limit": semantic_options.get("semantic_ai_feature_limit"),
            "semantic_ai_batch_size": semantic_options.get("semantic_ai_batch_size"),
            "semantic_ai_batch_by": semantic_options.get("semantic_ai_batch_by", "subsystem"),
            "semantic_ai_input_mode": semantic_options.get("semantic_ai_input_mode"),
            "semantic_dynamic_graph_state": semantic_options.get("semantic_dynamic_graph_state"),
            "semantic_graph_state": semantic_options.get("semantic_graph_state", True),
            "semantic_skip_completed": semantic_options.get("semantic_skip_completed", True),
            "semantic_classify_feedback": semantic_options.get("semantic_classify_feedback", True),
            "semantic_batch_memory": semantic_options.get("semantic_batch_memory", False),
            "semantic_base_snapshot_id": semantic_options.get("semantic_base_snapshot_id") or "",
            "semantic_ai_provider": semantic_options.get("semantic_ai_provider"),
            "semantic_ai_model": semantic_options.get("semantic_ai_model"),
            "semantic_ai_role": semantic_options.get("semantic_ai_role"),
            "semantic_ai_chain_role": semantic_options.get("semantic_ai_chain_role"),
            "semantic_analyzer_role": semantic_options.get("semantic_analyzer_role"),
            "semantic_ai_scope": semantic_options.get("semantic_ai_scope"),
            "semantic_node_ids": semantic_options.get("semantic_node_ids"),
            "semantic_layers": semantic_options.get("semantic_layers"),
            "semantic_quality_flags": semantic_options.get("semantic_quality_flags"),
            "semantic_missing": semantic_options.get("semantic_missing"),
            "semantic_changed_paths": semantic_options.get("semantic_changed_paths"),
            "semantic_path_prefixes": semantic_options.get("semantic_path_prefixes"),
            "semantic_selector_match": semantic_options.get("semantic_selector_match"),
            "semantic_include_structural": semantic_options.get("semantic_include_structural", False),
            "semantic_config_path": str(semantic_options.get("semantic_config_path") or ""),
        },
        output_payload=semantic_enrichment,
    )
    return semantic_enrichment


def _run_incremental_metadata_scope_reconcile_candidate(
    conn: sqlite3.Connection,
    project_id: str,
    root: Path,
    *,
    target: str,
    rid: str,
    sid: str,
    active: dict[str, Any],
    active_inventory: list[dict[str, Any]],
    changed_files: list[str],
    checkout_provenance: dict[str, Any],
    created_by: str,
    activate: bool,
    ref_name: str,
    branch_ref: str,
    expected_old_snapshot_id: str,
    semantic_options: dict[str, Any],
) -> dict[str, Any]:
    identity = normalize_pending_scope_identity(ref_name=ref_name, branch_ref=branch_ref)
    active_snapshot_id = str(active.get("snapshot_id") or "")
    active_commit = str(active.get("commit_sha") or "")
    active_graph_json = _read_snapshot_graph(project_id, active_snapshot_id)
    if not active_snapshot_id or not _deps_graph_nodes(active_graph_json):
        return {"ok": False, "fallback_reason": "no_active_graph_payload"}

    candidate_graph = _json_clone(active_graph_json)
    if isinstance(candidate_graph, dict):
        candidate_graph["run_id"] = rid
        candidate_graph["session_id"] = rid
        metadata = dict(candidate_graph.get("metadata") or {})
        metadata["incremental_scope_reconcile"] = {
            "strategy": "incremental_graph_delta",
            "mode": "metadata_only",
            "active_snapshot_id": active_snapshot_id,
            "active_graph_commit": active_commit,
            "target_commit": target,
            "ref_name": identity["ref_name"],
            "branch_ref": identity["branch_ref"],
        }
        candidate_graph["metadata"] = metadata

    governance_index = build_governance_index(
        conn,
        project_id,
        root,
        run_id=rid,
        commit_sha=target,
        candidate_graph=candidate_graph,
        snapshot_id=sid,
        snapshot_kind="scope",
    )
    file_inventory = _normalize_inventory_commit(
        [
            row for row in (governance_index.get("file_inventory") or [])
            if isinstance(row, dict)
        ],
        commit_sha=target,
    )
    scope_file_delta = _build_scope_file_delta(
        project_root=root,
        old_rows=active_inventory,
        new_rows=file_inventory,
        changed_files=changed_files,
    )
    eligibility = _incremental_metadata_scope_eligibility(
        scope_file_delta,
        project_root=root,
        active_graph_json=active_graph_json,
        old_rows=active_inventory,
        new_rows=file_inventory,
    )
    if not eligibility.get("supported"):
        return {
            "ok": False,
            "fallback_reason": str(eligibility.get("reason") or "incremental_metadata_unsupported"),
            "incremental_eligibility": eligibility,
        }
    graph_delta_mode = str(eligibility.get("mode") or "metadata_only")
    if isinstance(candidate_graph, dict):
        metadata = dict(candidate_graph.get("metadata") or {})
        incremental_metadata = dict(metadata.get("incremental_scope_reconcile") or {})
        incremental_metadata["mode"] = graph_delta_mode
        metadata["incremental_scope_reconcile"] = incremental_metadata
        candidate_graph["metadata"] = metadata
    test_fanin_update: dict[str, Any] = {}
    source_dependency_update: dict[str, Any] = {}
    if graph_delta_mode in {"source_dependency_delta", "mixed_dependency_delta"}:
        source_dependency_update = _apply_incremental_source_dependency_delta(
            root,
            candidate_graph,
            source_paths=[
                str(path)
                for path in eligibility.get("source_paths", []) or []
                if str(path).strip()
            ],
        )
        if not source_dependency_update.get("ok"):
            return {
                "ok": False,
                "fallback_reason": str(
                    source_dependency_update.get("reason") or "incremental_source_dependency_unsupported"
                ),
                "incremental_eligibility": eligibility,
                "source_dependency_update": source_dependency_update,
            }
        governance_index = build_governance_index(
            conn,
            project_id,
            root,
            run_id=rid,
            commit_sha=target,
            candidate_graph=candidate_graph,
            snapshot_id=sid,
            snapshot_kind="scope",
        )
        file_inventory = _normalize_inventory_commit(
            [
                row for row in (governance_index.get("file_inventory") or [])
                if isinstance(row, dict)
            ],
            commit_sha=target,
        )
        scope_file_delta = _build_scope_file_delta(
            project_root=root,
            old_rows=active_inventory,
            new_rows=file_inventory,
            changed_files=changed_files,
        )
        final_eligibility = _incremental_metadata_scope_eligibility(
            scope_file_delta,
            project_root=root,
            active_graph_json=active_graph_json,
            old_rows=active_inventory,
            new_rows=file_inventory,
        )
        if not final_eligibility.get("supported"):
            return {
                "ok": False,
                "fallback_reason": str(final_eligibility.get("reason") or "incremental_source_dependency_unsupported"),
                "incremental_eligibility": final_eligibility,
                "source_dependency_update": source_dependency_update,
            }
        graph_delta_mode = str(final_eligibility.get("mode") or graph_delta_mode)
        eligibility = {
            **final_eligibility,
            "source_dependency_update": source_dependency_update,
        }
    if graph_delta_mode in {"test_fanin_hash_only", "test_fanin_file_set"}:
        removed_test_paths = [
            str(path)
            for path in eligibility.get("removed_test_paths", []) or []
            if str(path).strip()
        ]
        removed_test_path_set = set(removed_test_paths)
        test_fanin_update = _apply_incremental_test_fanin_bindings(
            root,
            candidate_graph,
            changed_test_paths=[
                str(path)
                for path in eligibility.get("test_paths", []) or []
                if str(path).strip() and str(path) not in removed_test_path_set
            ],
            removed_test_paths=removed_test_paths,
        )
        governance_index = build_governance_index(
            conn,
            project_id,
            root,
            run_id=rid,
            commit_sha=target,
            candidate_graph=candidate_graph,
            snapshot_id=sid,
            snapshot_kind="scope",
        )
        file_inventory = _normalize_inventory_commit(
            [
                row for row in (governance_index.get("file_inventory") or [])
                if isinstance(row, dict)
            ],
            commit_sha=target,
        )
        scope_file_delta = _build_scope_file_delta(
            project_root=root,
            old_rows=active_inventory,
            new_rows=file_inventory,
            changed_files=changed_files,
        )
        final_eligibility = _incremental_metadata_scope_eligibility(
            scope_file_delta,
            project_root=root,
            active_graph_json=active_graph_json,
            old_rows=active_inventory,
            new_rows=file_inventory,
        )
        if not final_eligibility.get("supported"):
            return {
                "ok": False,
                "fallback_reason": str(final_eligibility.get("reason") or "incremental_test_fanin_unsupported"),
                "incremental_eligibility": final_eligibility,
                "test_fanin_update": test_fanin_update,
            }
        graph_delta_mode = str(final_eligibility.get("mode") or graph_delta_mode)
        eligibility = {
            **final_eligibility,
            "test_fanin_update": test_fanin_update,
        }

    state_dir = _governance_state_dir(project_id, rid)
    scratch_dir = state_dir / "scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    trace = ReconcileTrace(
        project_id=project_id,
        run_id=rid,
        snapshot_id=sid,
        trace_dir=state_dir / "trace",
    )
    trace.step(
        "run-input",
        input_payload={
            "project_id": project_id,
            "project_root": str(root),
            "project_root_role": "execution_root",
            "checkout_provenance": checkout_provenance,
            "run_id": rid,
            "snapshot_id": sid,
            "snapshot_kind": "scope",
            "commit_sha": target,
            "created_by": created_by,
            "scope_reconcile_strategy": "incremental_graph_delta",
            "scope_graph_delta_mode": graph_delta_mode,
        },
        output_payload={
            "state_dir": str(state_dir),
            "scratch_dir": str(scratch_dir),
            "active_snapshot_id": active_snapshot_id,
            "active_graph_commit": active_commit,
            "changed_files": changed_files,
            "semantic_enrich": semantic_options.get("semantic_enrich", True),
        },
    )
    trace.step(
        "reuse-active-graph",
        input_payload={
            "active_snapshot_id": active_snapshot_id,
            "active_graph_commit": active_commit,
            "target_commit": target,
        },
        output_payload={
            "graph_stats": graph_payload_stats(candidate_graph),
            "eligibility": eligibility,
            "test_fanin_update": test_fanin_update,
            "source_dependency_update": source_dependency_update,
        },
    )
    hash_metadata_merge = merge_feature_hashes_into_graph_nodes(candidate_graph, governance_index)
    file_inventory = _normalize_inventory_commit(
        [
            row for row in (governance_index.get("file_inventory") or [])
            if isinstance(row, dict)
        ],
        commit_sha=target,
    )
    enriched_inventory = governance_index.get("file_inventory")
    if isinstance(enriched_inventory, list):
        file_inventory = _normalize_inventory_commit(
            [row for row in enriched_inventory if isinstance(row, dict)],
            commit_sha=target,
        )
    nodes = _deps_graph_nodes(candidate_graph)
    edges = _deps_graph_edges(candidate_graph)
    scope_file_delta = _mark_scope_file_delta_strategy(
        scope_file_delta,
        strategy="incremental_graph_delta",
        graph_delta_mode=graph_delta_mode,
    )
    scope_graph_delta = _build_scope_graph_delta(
        old_graph_json=active_graph_json,
        new_graph_json=candidate_graph,
        scope_file_delta=scope_file_delta,
        strategy="incremental_graph_delta",
        mode=graph_delta_mode,
    )
    rule_fingerprint = build_graph_rule_fingerprint(root, commit_sha=target)
    notes = {
        "state_only": True,
        "run_id": rid,
        "snapshot_kind": "scope",
        "scope_reconcile_strategy": "incremental_graph_delta",
        "scope_graph_delta_mode": graph_delta_mode,
        "incremental_scope_reconcile": {
            "active_snapshot_id": active_snapshot_id,
            "active_graph_commit": active_commit,
            "target_commit": target,
            "eligibility": eligibility,
            "source_dependency_update": source_dependency_update,
        },
        "scope_file_delta": scope_file_delta,
        "scope_graph_delta": scope_graph_delta,
        "file_inventory_summary": governance_index.get("file_inventory_summary") or {},
        "governance_hint_bindings": governance_index.get("governance_hint_bindings") or {},
        "governance_index_hash_metadata": hash_metadata_merge,
        "graph_rule_fingerprint": rule_fingerprint,
        "checkout_provenance": checkout_provenance,
    }
    notes["trace"] = {
        "trace_dir": str(trace.trace_dir),
        "summary_path": str(trace.trace_dir / "summary.json"),
    }
    with sqlite_write_lock():
        snapshot = create_graph_snapshot(
            conn,
            project_id,
            snapshot_id=sid,
            commit_sha=target,
            parent_snapshot_id=active_snapshot_id,
            snapshot_kind="scope",
            ref_name=identity["ref_name"],
            branch_ref=identity["branch_ref"],
            graph_json=candidate_graph,
            file_inventory=file_inventory,
            drift_ledger=[],
            status=SNAPSHOT_STATUS_CANDIDATE,
            created_by=created_by,
            notes=json.dumps(notes, ensure_ascii=False, sort_keys=True),
        )
        index_counts = index_graph_snapshot(
            conn,
            project_id,
            sid,
            nodes=nodes,
            edges=edges,
        )
        governance_index_summary = persist_governance_index(
            conn,
            project_id,
            governance_index,
            persist_inventory=True,
        )
        notes["governance_index"] = governance_index_summary
        conn.execute(
            "UPDATE graph_snapshots SET notes = ? WHERE project_id = ? AND snapshot_id = ?",
            (json.dumps(notes, ensure_ascii=False, sort_keys=True), project_id, sid),
        )
        conn.commit()
    trace.step(
        "create-graph-snapshot",
        input_payload={
            "snapshot_id": sid,
            "snapshot_kind": "scope",
            "commit_sha": target,
            "node_count": len(nodes),
            "edge_count": len(edges),
            "file_inventory_count": len(file_inventory),
        },
        output_payload={
            "snapshot": snapshot,
            "snapshot_path": str(snapshot_graph_path(project_id, sid)),
            "snapshot_artifact": artifact_ref(snapshot_graph_path(project_id, sid)),
        },
    )
    trace.step(
        "index-graph-snapshot",
        input_payload={
            "snapshot_id": sid,
            "node_count": len(nodes),
            "edge_count": len(edges),
        },
        output_payload={"index_counts": index_counts},
    )
    trace.step(
        "build-governance-index",
        input_payload={
            "snapshot_id": sid,
            "run_id": rid,
            "commit_sha": target,
            "index_scope": "candidate_snapshot",
        },
        output_payload={
            "summary": governance_index_summary,
            "artifacts": governance_index_summary.get("artifacts") or {},
            "hash_metadata_merge": hash_metadata_merge,
        },
    )
    semantic_enrichment = _run_scope_semantic_enrichment(
        conn,
        project_id,
        sid,
        root,
        created_by=created_by,
        semantic_options=semantic_options,
        trace=trace,
    )
    notes["semantic_enrichment"] = semantic_enrichment
    activation = None
    if activate:
        with sqlite_write_lock():
            activation = activate_graph_snapshot(
                conn,
                project_id,
                sid,
                expected_old_snapshot_id=expected_old_snapshot_id or None,
                ref_name=identity["ref_name"],
                branch_ref=identity["branch_ref"],
            )
            conn.commit()
        trace.step(
            "activate-snapshot",
            input_payload={
                "snapshot_id": sid,
                "expected_old_snapshot_id": expected_old_snapshot_id,
                "ref_name": identity["ref_name"],
                "branch_ref": identity["branch_ref"],
            },
            output_payload={"activation": activation},
        )
    else:
        trace.step(
            "activate-snapshot",
            input_payload={"snapshot_id": sid, "activate": False},
            output_payload={"activation": None, "status": "skipped"},
            status="skipped",
        )
    with sqlite_write_lock():
        conn.execute(
            "UPDATE graph_snapshots SET notes = ? WHERE project_id = ? AND snapshot_id = ?",
            (json.dumps(notes, ensure_ascii=False, sort_keys=True), project_id, sid),
        )
        conn.commit()
    trace_summary = trace.finalize(status="ok")
    return {
        "ok": True,
        "project_id": project_id,
        "run_id": rid,
        "commit_sha": target,
        "snapshot_id": sid,
        "snapshot_status": "active" if activation else SNAPSHOT_STATUS_CANDIDATE,
        "snapshot_path": str(snapshot_graph_path(project_id, sid)),
        "phase_report_path": "",
        "graph_stats": graph_payload_stats(candidate_graph),
        "index_counts": index_counts,
        "governance_index": governance_index_summary,
        "semantic_enrichment": semantic_enrichment,
        "trace": trace_summary,
        "file_inventory_count": len(file_inventory),
        "file_inventory_summary": governance_index.get("file_inventory_summary") or {},
        "feature_cluster_count": governance_index_summary.get("feature_count", 0),
        "snapshot": snapshot,
        "activation": activation,
        "incremental_eligibility": eligibility,
    }


def _finalize_scope_reconcile_candidate(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    result: dict[str, Any],
    root: Path,
    active: dict[str, Any],
    active_inventory: list[dict[str, Any]],
    changed_files: list[str],
    pending_count: int,
    covered_commit_shas: list[str],
    target: str,
    rid: str,
    sid: str,
    created_by: str,
    ref_name: str,
    branch_ref: str,
    worktree_id: str,
    worktree_path: str,
    semantic_enqueue_stale: bool,
    strategy: str,
    graph_delta_mode: str,
    fallback_reason: str = "",
) -> dict[str, Any]:
    identity = normalize_pending_scope_identity(
        ref_name=ref_name,
        branch_ref=branch_ref,
        worktree_id=worktree_id,
        worktree_path=worktree_path,
    )
    scope_file_delta = _build_scope_file_delta(
        project_root=root,
        old_rows=active_inventory,
        new_rows=_snapshot_inventory_rows(conn, project_id, sid),
        changed_files=changed_files,
    )
    scope_file_delta = _mark_scope_file_delta_strategy(
        scope_file_delta,
        strategy=strategy,
        graph_delta_mode=graph_delta_mode,
        fallback_reason=fallback_reason,
    )
    old_graph_json = _read_snapshot_graph(project_id, str(active.get("snapshot_id") or ""))
    new_graph_json = _read_snapshot_graph(project_id, sid)
    changed_line_ranges = _git_changed_line_ranges(
        root,
        str(active.get("commit_sha") or ""),
        target,
        changed_files,
    )
    scope_function_delta = _changed_functions_for_line_ranges(
        _deps_graph_nodes(new_graph_json),
        changed_line_ranges,
    )
    if scope_function_delta.get("changed_function_ids"):
        scope_file_delta["scope_function_delta"] = scope_function_delta
    scope_test_function_delta = _changed_test_functions_for_line_ranges(
        _deps_graph_nodes(new_graph_json),
        changed_line_ranges,
    )
    if scope_test_function_delta.get("changed_test_function_ids"):
        scope_file_delta["scope_test_function_delta"] = scope_test_function_delta
    scope_graph_delta = _build_scope_graph_delta(
        old_graph_json=old_graph_json,
        new_graph_json=new_graph_json,
        scope_file_delta=scope_file_delta,
        strategy=strategy,
        mode=graph_delta_mode,
        fallback_reason=fallback_reason,
    )
    scope_event_summary: dict[str, Any] = {}
    pending_notes = {
        "covered_commit_shas": covered_commit_shas,
        "covered_commit_count": len(covered_commit_shas),
        "active_snapshot_id": active.get("snapshot_id", ""),
        "active_graph_commit": active.get("commit_sha", ""),
        **identity,
        "scope_file_delta": scope_file_delta,
        "scope_graph_delta": scope_graph_delta,
        "semantic_enqueue_stale": bool(semantic_enqueue_stale),
    }
    row = conn.execute(
        "SELECT notes FROM graph_snapshots WHERE project_id = ? AND snapshot_id = ?",
        (project_id, sid),
    ).fetchone()
    with sqlite_write_lock():
        scope_event_summary = _emit_scope_graph_events(
            conn,
            project_id,
            old_snapshot_id=str(active.get("snapshot_id") or ""),
            new_snapshot_id=sid,
            old_graph_json=old_graph_json,
            new_graph_json=new_graph_json,
            scope_file_delta=scope_file_delta,
            baseline_commit=str(active.get("commit_sha") or ""),
            target_commit=target,
            created_by=created_by,
        )
        pending_notes["scope_graph_events"] = scope_event_summary
        activation_succeeded = bool(result.get("activation"))
        updated = _update_pending_scope_candidate(
            conn,
            project_id,
            covered_commit_shas=covered_commit_shas,
            snapshot_id=sid,
            target_commit_sha=target,
            run_id=rid,
            activated=activation_succeeded,
            ref_name=identity["ref_name"],
            branch_ref=identity["branch_ref"],
            worktree_id=identity["worktree_id"],
            worktree_path=identity["worktree_path"],
        )
        if row:
            try:
                notes = json.loads(row["notes"] if hasattr(row, "keys") else row[0])
            except Exception:
                notes = {}
            notes["scope_file_delta"] = scope_file_delta
            notes["scope_graph_delta"] = scope_graph_delta
            notes["pending_scope_reconcile"] = pending_notes
            conn.execute(
                "UPDATE graph_snapshots SET notes = ? WHERE project_id = ? AND snapshot_id = ?",
                (json.dumps(notes, ensure_ascii=False, sort_keys=True), project_id, sid),
            )
        trace_info = result.get("trace") if isinstance(result.get("trace"), dict) else {}
        graph_stats = result.get("graph_stats") if isinstance(result.get("graph_stats"), dict) else {}
        notes_trace = notes.get("trace") if isinstance(notes.get("trace"), dict) else {}
        record_reconcile_run_metric(
            conn,
            project_id,
            run_id=rid,
            snapshot_id=sid,
            commit_sha=target,
            parent_commit_sha=str(active.get("commit_sha") or ""),
            snapshot_kind="scope",
            strategy=strategy,
            graph_delta_mode=graph_delta_mode,
            status=str(trace_info.get("status") or ("ok" if result.get("ok") else "failed")),
            changed_file_count=int(scope_file_delta.get("changed_file_count") or 0),
            impacted_file_count=int(scope_file_delta.get("impacted_file_count") or 0),
            event_count=int(scope_event_summary.get("event_count") or 0),
            node_count=int(graph_stats.get("nodes") or 0),
            edge_count=int(graph_stats.get("edges") or 0),
            elapsed_ms=int(trace_info.get("elapsed_ms") or 0),
            trace_summary_path=str(notes_trace.get("summary_path") or trace_info.get("summary_path") or ""),
            fallback_reason=fallback_reason,
            evidence={
                "source": "pending_scope_materializer",
                "covered_commit_shas": covered_commit_shas,
                "pending_rows_bound": updated,
                "semantic_enqueue_stale": bool(semantic_enqueue_stale),
                **identity,
            },
        )
        conn.commit()
    return {
        **result,
        "pending_count": pending_count,
        "covered_commit_shas": covered_commit_shas,
        "covered_pending_count": len(covered_commit_shas),
        "pending_rows_bound": updated,
        "scope_file_delta": scope_file_delta,
        "scope_graph_delta": scope_graph_delta,
        "scope_graph_events": scope_event_summary,
        "active_snapshot_id": active.get("snapshot_id", ""),
        "active_graph_commit": active.get("commit_sha", ""),
        "ref_name": identity["ref_name"],
        "branch_ref": identity["branch_ref"],
        "worktree_id": identity["worktree_id"],
        "worktree_path": identity["worktree_path"],
    }


def run_pending_scope_reconcile_candidate(
    conn: sqlite3.Connection,
    project_id: str,
    project_root: str | Path,
    *,
    target_commit_sha: str = "",
    run_id: str = "",
    snapshot_id: str | None = None,
    created_by: str = "observer",
    activate: bool = False,
    ref_name: str = "active",
    branch_ref: str = "",
    worktree_id: str = "",
    worktree_path: str = "",
    semantic_enrich: bool = True,
    semantic_use_ai: bool | None = None,
    semantic_feedback_items: list[dict[str, Any]] | dict[str, Any] | None = None,
    semantic_feedback_round: int | None = None,
    semantic_max_excerpt_chars: int | None = None,
    semantic_ai_call: Any = None,
    semantic_ai_feature_limit: int | None = None,
    semantic_ai_provider: str | None = None,
    semantic_ai_model: str | None = None,
    semantic_ai_role: str | None = None,
    semantic_ai_chain_role: str | None = None,
    semantic_analyzer_role: str | None = None,
    semantic_ai_scope: str | None = None,
    semantic_node_ids: Any = None,
    semantic_layers: Any = None,
    semantic_quality_flags: Any = None,
    semantic_missing: Any = None,
    semantic_changed_paths: Any = None,
    semantic_path_prefixes: Any = None,
    semantic_selector_match: str | None = None,
    semantic_include_structural: bool = False,
    semantic_ai_batch_size: int | None = None,
    semantic_ai_batch_by: str = "subsystem",
    semantic_ai_input_mode: str | None = None,
    semantic_dynamic_graph_state: bool | None = None,
    semantic_graph_state: bool = True,
    semantic_skip_completed: bool = True,
    semantic_classify_feedback: bool = True,
    semantic_batch_memory: bool | None = False,
    semantic_batch_memory_id: str | None = None,
    semantic_base_snapshot_id: str | None = None,
    semantic_config_path: str | Path | None = None,
    semantic_enqueue_stale: bool = True,
) -> dict[str, Any]:
    """Materialize pending scope rows as a reviewable candidate snapshot.

    The current MVP rebuilds a state-only candidate graph from the current
    worktree, then binds pending commits up to the target commit to that
    candidate. It intentionally does not activate the snapshot.

    OPT-BACKLOG-MATERIALIZE-NO-WORKER-NOTIFY: when called from the dashboard
    flow, the caller can set `semantic_enqueue_stale=False` so the materialize
    does not silently fill graph_semantic_jobs with ai_pending rows the
    in-process worker won't auto-drain. Operators then explicitly enqueue
    enrichment via POST /semantic/jobs (which publishes
    semantic_job.enqueued).
    """
    ensure_graph_snapshot_schema(conn)
    root = Path(project_root).resolve()
    identity = normalize_pending_scope_identity(
        ref_name=ref_name,
        branch_ref=branch_ref,
        worktree_id=worktree_id,
        worktree_path=worktree_path,
    )
    head = _git_commit(root) or "unknown"
    target = target_commit_sha or head
    if head != "unknown" and target != head:
        raise ValueError(
            "pending scope materializer scans the current worktree; "
            f"target_commit_sha must equal HEAD ({head}), got {target}"
        )
    pending = list_pending_scope_reconcile(
        conn,
        project_id,
        statuses=[PENDING_STATUS_QUEUED, PENDING_STATUS_RUNNING, PENDING_STATUS_FAILED],
        ref_name=identity["ref_name"],
        branch_ref=identity["branch_ref"],
        worktree_id=identity["worktree_id"],
        worktree_path=identity["worktree_path"],
    )
    if not pending:
        return {
            "ok": False,
            "project_id": project_id,
            "reason": "no_pending_scope_reconcile",
            "target_commit_sha": target,
            **identity,
            "pending_count": 0,
        }
    covered = _pending_commits_through_target(pending, target)
    if not covered:
        return {
            "ok": False,
            "project_id": project_id,
            "reason": "no_pending_commits_selected",
            "target_commit_sha": target,
            **identity,
            "pending_count": len(pending),
        }
    dirty_files = _git_dirty_files(root)
    if dirty_files:
        preview = ", ".join(dirty_files[:8])
        suffix = f", ... +{len(dirty_files) - 8} more" if len(dirty_files) > 8 else ""
        raise ValueError(
            "pending scope materializer requires a clean git worktree; "
            f"uncommitted files: {preview}{suffix}"
        )

    ref_active = get_active_graph_snapshot(conn, project_id, ref_name=identity["ref_name"]) or {}
    active = ref_active or get_active_graph_snapshot(conn, project_id) or {}
    active_rule_fingerprint = snapshot_rule_fingerprint(active)
    if active_rule_fingerprint:
        current_rule_fingerprint = build_graph_rule_fingerprint(
            root,
            commit_sha=target,
            semantic_config_path=semantic_config_path,
            include_source_hints=False,
        )
        rule_fingerprint_status = compare_rule_fingerprint(
            active_rule_fingerprint,
            current_rule_fingerprint,
        )
        if bool(rule_fingerprint_status.get("mismatch")):
            raise ValueError(
                "pending scope reconcile requires full reconcile when graph rule fingerprint changed; "
                "recommended_action=run_full_reconcile; "
                f"snapshot_fingerprint={rule_fingerprint_status.get('snapshot_fingerprint') or ''}; "
                f"current_fingerprint={rule_fingerprint_status.get('current_fingerprint') or ''}"
            )
    expected_old_snapshot_id = str(ref_active.get("snapshot_id") or "")
    if identity["ref_name"] == "active" and not expected_old_snapshot_id:
        expected_old_snapshot_id = str(active.get("snapshot_id") or "")
    active_inventory = _snapshot_inventory_rows(conn, project_id, active.get("snapshot_id", ""))
    changed_files = _git_changed_files(
        root,
        str(active.get("commit_sha") or ""),
        target,
    )
    has_semantic_selector_override = _has_semantic_selector_override(
        semantic_ai_scope,
        semantic_node_ids,
        semantic_layers,
        semantic_quality_flags,
        semantic_missing,
        semantic_changed_paths,
        semantic_path_prefixes,
        semantic_selector_match,
    )
    effective_semantic_ai_scope = semantic_ai_scope
    effective_semantic_changed_paths = semantic_changed_paths
    effective_semantic_selector_match = semantic_selector_match
    if changed_files and not has_semantic_selector_override:
        effective_semantic_ai_scope = "changed"
        effective_semantic_changed_paths = changed_files
        effective_semantic_selector_match = "primary"
    rid = run_id or f"scope-reconcile-{_short_commit(target)}-pending"
    sid = snapshot_id or snapshot_id_for("scope", target)
    checkout_provenance = describe_checkout(
        root,
        project_id=project_id,
        commit_sha=target,
    )
    semantic_options = {
        "semantic_enrich": semantic_enrich,
        "semantic_use_ai": semantic_use_ai,
        "semantic_feedback_items": semantic_feedback_items,
        "semantic_feedback_round": semantic_feedback_round,
        "semantic_max_excerpt_chars": semantic_max_excerpt_chars,
        "semantic_ai_call": semantic_ai_call,
        "semantic_ai_feature_limit": semantic_ai_feature_limit,
        "semantic_ai_provider": semantic_ai_provider,
        "semantic_ai_model": semantic_ai_model,
        "semantic_ai_role": semantic_ai_role,
        "semantic_ai_chain_role": semantic_ai_chain_role,
        "semantic_analyzer_role": semantic_analyzer_role,
        "semantic_ai_scope": effective_semantic_ai_scope,
        "semantic_node_ids": semantic_node_ids,
        "semantic_layers": semantic_layers,
        "semantic_quality_flags": semantic_quality_flags,
        "semantic_missing": semantic_missing,
        "semantic_changed_paths": effective_semantic_changed_paths,
        "semantic_path_prefixes": semantic_path_prefixes,
        "semantic_selector_match": effective_semantic_selector_match,
        "semantic_include_structural": semantic_include_structural,
        "semantic_ai_batch_size": semantic_ai_batch_size,
        "semantic_ai_batch_by": semantic_ai_batch_by,
        "semantic_ai_input_mode": semantic_ai_input_mode,
        "semantic_dynamic_graph_state": semantic_dynamic_graph_state,
        "semantic_graph_state": semantic_graph_state,
        "semantic_skip_completed": semantic_skip_completed,
        "semantic_classify_feedback": semantic_classify_feedback,
        "semantic_batch_memory": semantic_batch_memory,
        "semantic_batch_memory_id": semantic_batch_memory_id,
        "semantic_base_snapshot_id": semantic_base_snapshot_id or active.get("snapshot_id", ""),
        "semantic_config_path": semantic_config_path,
        "semantic_enqueue_stale": semantic_enqueue_stale,
    }
    incremental_result = _run_incremental_metadata_scope_reconcile_candidate(
        conn,
        project_id,
        root,
        target=target,
        rid=rid,
        sid=sid,
        active=active,
        active_inventory=active_inventory,
        changed_files=changed_files,
        checkout_provenance=checkout_provenance,
        created_by=created_by,
        activate=activate,
        ref_name=identity["ref_name"],
        branch_ref=identity["branch_ref"],
        expected_old_snapshot_id=expected_old_snapshot_id,
        semantic_options=semantic_options,
    )
    if incremental_result.get("ok"):
        incremental_mode = str(
            (incremental_result.get("incremental_eligibility") or {}).get("mode")
            or "metadata_only"
        )
        return _finalize_scope_reconcile_candidate(
            conn,
            project_id,
            result=incremental_result,
            root=root,
            active=active,
            active_inventory=active_inventory,
            changed_files=changed_files,
            pending_count=len(pending),
            covered_commit_shas=covered,
            target=target,
            rid=rid,
            sid=sid,
            created_by=created_by,
            ref_name=identity["ref_name"],
            branch_ref=identity["branch_ref"],
            worktree_id=identity["worktree_id"],
            worktree_path=identity["worktree_path"],
            semantic_enqueue_stale=semantic_enqueue_stale,
            strategy="incremental_graph_delta",
            graph_delta_mode=incremental_mode,
        )
    fallback_reason = str(
        incremental_result.get("fallback_reason")
        or "incremental_scope_unsupported"
    )
    result = run_state_only_full_reconcile(
        conn,
        project_id,
        root,
        run_id=rid,
        commit_sha=target,
        snapshot_id=sid,
        snapshot_kind="scope",
        created_by=created_by,
        # MF-2026-05-10-014: pass through caller's activate intent so the
        # dashboard "Queue scope reconcile" path can incrementally catch up
        # the active snapshot in one HTTP round-trip. MF-012's hook then
        # auto-rebuilds the projection on activation.
        activate=activate,
        ref_name=identity["ref_name"],
        branch_ref=identity["branch_ref"],
        expected_old_snapshot_id=expected_old_snapshot_id or None,
        semantic_enrich=semantic_enrich,
        semantic_use_ai=semantic_use_ai,
        semantic_feedback_items=semantic_feedback_items,
        semantic_feedback_round=semantic_feedback_round,
        semantic_max_excerpt_chars=semantic_max_excerpt_chars,
        semantic_ai_call=semantic_ai_call,
        semantic_ai_feature_limit=semantic_ai_feature_limit,
        semantic_ai_batch_size=semantic_ai_batch_size,
        semantic_ai_batch_by=semantic_ai_batch_by,
        semantic_ai_input_mode=semantic_ai_input_mode,
        semantic_dynamic_graph_state=semantic_dynamic_graph_state,
        semantic_graph_state=semantic_graph_state,
        semantic_skip_completed=semantic_skip_completed,
        semantic_classify_feedback=semantic_classify_feedback,
        semantic_batch_memory=semantic_batch_memory,
        semantic_batch_memory_id=semantic_batch_memory_id,
        semantic_base_snapshot_id=semantic_base_snapshot_id or active.get("snapshot_id", ""),
        semantic_ai_provider=semantic_ai_provider,
        semantic_ai_model=semantic_ai_model,
        semantic_ai_role=semantic_ai_role,
        semantic_ai_chain_role=semantic_ai_chain_role,
        semantic_analyzer_role=semantic_analyzer_role,
        semantic_ai_scope=effective_semantic_ai_scope,
        semantic_node_ids=semantic_node_ids,
        semantic_layers=semantic_layers,
        semantic_quality_flags=semantic_quality_flags,
        semantic_missing=semantic_missing,
        semantic_changed_paths=effective_semantic_changed_paths,
        semantic_path_prefixes=semantic_path_prefixes,
        semantic_selector_match=effective_semantic_selector_match,
        semantic_include_structural=semantic_include_structural,
        semantic_config_path=semantic_config_path,
        semantic_enqueue_stale=semantic_enqueue_stale,
        notes_extra={
            "pending_scope_reconcile": {
                "covered_commit_shas": covered,
                "covered_commit_count": len(covered),
                "active_snapshot_id": active.get("snapshot_id", ""),
                "active_graph_commit": active.get("commit_sha", ""),
                **identity,
                "semantic_selector_defaulted_to_changed_files": bool(
                    changed_files and not has_semantic_selector_override
                ),
                "semantic_enqueue_stale": bool(semantic_enqueue_stale),
            }
        },
    )
    if not result.get("ok"):
        return {
            **result,
            "pending_count": len(pending),
            "covered_commit_shas": covered,
        }
    return _finalize_scope_reconcile_candidate(
        conn,
        project_id,
        result=result,
        root=root,
        active=active,
        active_inventory=active_inventory,
        changed_files=changed_files,
        pending_count=len(pending),
        covered_commit_shas=covered,
        target=target,
        rid=rid,
        sid=sid,
        created_by=created_by,
        ref_name=identity["ref_name"],
        branch_ref=identity["branch_ref"],
        worktree_id=identity["worktree_id"],
        worktree_path=identity["worktree_path"],
        semantic_enqueue_stale=semantic_enqueue_stale,
        strategy="full_rebuild_fallback",
        graph_delta_mode="full_rebuild",
        fallback_reason=fallback_reason,
    )


def run_backfill_escape_hatch(
    conn: sqlite3.Connection,
    project_id: str,
    project_root: str | Path,
    *,
    target_commit_sha: str = "",
    run_id: str = "",
    snapshot_id: str | None = None,
    created_by: str = "observer",
    reason: str = "",
    expected_old_snapshot_id: str | None = None,
) -> dict[str, Any]:
    """Activate a HEAD full snapshot and waive stuck pending scope rows.

    This is the explicit observer escape hatch for early scope-reconcile bugs:
    it rebuilds graph state from the current commit, activates that state with
    normal snapshot CAS semantics, and preserves queued/running/failed pending
    rows as waived audit records instead of deleting them.
    """
    ensure_graph_snapshot_schema(conn)
    root = Path(project_root).resolve()
    head = _git_commit(root) or "unknown"
    target = target_commit_sha or head
    if head != "unknown" and target != head:
        raise ValueError(
            "backfill escape hatch scans the current worktree; "
            f"target_commit_sha must equal HEAD ({head}), got {target}"
        )
    pending = list_pending_scope_reconcile(
        conn,
        project_id,
        statuses=[PENDING_STATUS_QUEUED, PENDING_STATUS_RUNNING, PENDING_STATUS_FAILED],
    )
    pending_commits = [
        str(row.get("commit_sha") or "").strip()
        for row in pending
        if str(row.get("commit_sha") or "").strip()
    ]
    active = get_active_graph_snapshot(conn, project_id) or {}
    rid = run_id or f"backfill-escape-{_short_commit(target)}"
    sid = snapshot_id or snapshot_id_for("full", target)
    result = run_state_only_full_reconcile(
        conn,
        project_id,
        root,
        run_id=rid,
        commit_sha=target,
        snapshot_id=sid,
        snapshot_kind="full",
        created_by=created_by,
        activate=False,
        notes_extra={
            "backfill_escape_hatch": {
                "reason": reason,
                "pending_scope_commits": pending_commits,
                "pending_scope_count": len(pending_commits),
                "active_snapshot_id": active.get("snapshot_id", ""),
                "active_graph_commit": active.get("commit_sha", ""),
            }
        },
    )
    if not result.get("ok"):
        return {
            **result,
            "pending_scope_commits": pending_commits,
            "pending_scope_count": len(pending_commits),
        }
    with sqlite_write_lock():
        finalize = finalize_graph_snapshot(
            conn,
            project_id,
            result["snapshot_id"],
            target_commit_sha=target,
            expected_old_snapshot_id=expected_old_snapshot_id,
            actor=created_by,
            materialize_pending=False,
            evidence={"source": "backfill_escape_hatch", "reason": reason},
        )
        waiver = waive_pending_scope_reconcile(
            conn,
            project_id,
            commit_shas=pending_commits,
            snapshot_id=result["snapshot_id"],
            actor=created_by,
            reason=reason,
            evidence={"source": "backfill_escape_hatch"},
        )
        conn.commit()
    return {
        **result,
        "snapshot_status": "active",
        "activation": finalize,
        "pending_scope_commits": pending_commits,
        "pending_scope_count": len(pending_commits),
        "pending_scope_waiver": waiver,
        "active_snapshot_id": active.get("snapshot_id", ""),
        "active_graph_commit": active.get("commit_sha", ""),
    }


__all__ = [
    "normalize_reconcile_snapshot_for_comparison",
    "repair_snapshot_feature_hash_metadata",
    "run_backfill_escape_hatch",
    "run_pending_scope_reconcile_candidate",
    "run_state_only_full_reconcile",
]
