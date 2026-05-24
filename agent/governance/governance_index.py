"""State-only governance index scanner.

The governance index is the substrate that full/scope reconcile can consume
before any code-writing chain stage runs. It scans project files, hashes, symbol
locations, documentation headings, and the active graph snapshot mapping, then
optionally persists those artifacts as governance state.
"""
from __future__ import annotations

import json
import hashlib
import sqlite3
import subprocess
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from agent.governance.external_project_governance import (
    build_coverage_state,
    build_doc_index,
    build_symbol_index,
)
from agent.governance.checkout_provenance import describe_checkout
from agent.governance.asset_projection import upsert_doc_asset_projection
from agent.governance.doc_asset_state import build_doc_asset_state
from agent.governance.graph_snapshot_store import (
    ensure_schema as ensure_graph_snapshot_schema,
    get_active_graph_snapshot,
)
from agent.governance.governance_hints import apply_binding_hints_to_graph_nodes
from agent.governance.project_profile import ProjectProfile, discover_project_profile
from agent.governance.reconcile_file_inventory import (
    build_file_inventory,
    summarize_file_inventory,
    upsert_file_inventory,
)


GOVERNANCE_INDEX_SCHEMA_VERSION = 1


def _utc_now() -> str:
    from agent.governance.graph_snapshot_store import utc_now

    return utc_now()


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)


def _hash_payload(payload: Any) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_json_safe(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )


def _decode_json_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            values = parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            values = [raw]
    else:
        values = []
    out: list[str] = []
    for item in values:
        text = str(item or "").replace("\\", "/").strip("/")
        if text:
            out.append(text)
    return sorted(set(out))


def _decode_json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


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


def _make_run_id(commit_sha: str) -> str:
    short = (commit_sha or "unknown")[:7] or "unknown"
    return f"governance-index-{short}-{uuid.uuid4().hex[:8]}"


def load_snapshot_nodes_for_inventory(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
) -> list[dict[str, Any]]:
    """Load active graph index rows in the node shape expected by inventory."""
    if not snapshot_id:
        return []
    ensure_graph_snapshot_schema(conn)
    rows = conn.execute(
        """
        SELECT node_id, layer, title, kind, primary_files_json,
               secondary_files_json, test_files_json, metadata_json
        FROM graph_nodes_index
        WHERE project_id = ? AND snapshot_id = ?
        ORDER BY node_id
        """,
        (project_id, snapshot_id),
    ).fetchall()
    nodes: list[dict[str, Any]] = []
    for row in rows:
        get = row.__getitem__ if hasattr(row, "keys") else lambda key: row[key]
        metadata = _decode_json_object(get("metadata_json"))
        kind = str(get("kind") or metadata.get("kind") or "")
        node = {
            "id": str(get("node_id") or ""),
            "node_id": str(get("node_id") or ""),
            "layer": str(get("layer") or ""),
            "title": str(get("title") or ""),
            "kind": kind,
            "primary": _decode_json_list(get("primary_files_json")),
            "secondary": _decode_json_list(get("secondary_files_json")),
            "test": _decode_json_list(get("test_files_json")),
            "metadata": metadata,
        }
        config_files = _decode_json_list(metadata.get("config_files"))
        if config_files:
            node["config"] = config_files
        nodes.append(node)
    return [node for node in nodes if node.get("id")]


def load_active_snapshot_nodes(
    conn: sqlite3.Connection,
    project_id: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Return the active graph snapshot row and decoded node index."""
    active = get_active_graph_snapshot(conn, project_id)
    if not active:
        return None, []
    return active, load_snapshot_nodes_for_inventory(conn, project_id, active["snapshot_id"])


def _candidate_graph_from_nodes(nodes: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {"deps_graph": {"nodes": list(nodes), "edges": []}}


def _nodes_from_candidate_graph(candidate_graph: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(candidate_graph, dict):
        return []
    deps = candidate_graph.get("deps_graph")
    if not isinstance(deps, dict):
        return []
    nodes = deps.get("nodes") or []
    return [node for node in nodes if isinstance(node, dict)]


def merge_feature_hashes_into_graph_nodes(
    graph_json: dict[str, Any],
    governance_index: dict[str, Any],
) -> dict[str, int]:
    """Copy indexed feature/file hashes into graph node metadata in-place."""
    if not isinstance(graph_json, dict):
        return {"features": 0, "nodes_updated": 0, "nodes_missing": 0}
    feature_index = governance_index.get("feature_index") if isinstance(governance_index.get("feature_index"), dict) else governance_index
    features = feature_index.get("features") if isinstance(feature_index, dict) else []
    by_id = {
        str(feature.get("node_id") or ""): feature
        for feature in features or []
        if isinstance(feature, dict) and str(feature.get("node_id") or "")
    }
    deps = graph_json.get("deps_graph")
    if isinstance(deps, dict) and isinstance(deps.get("nodes"), list):
        nodes = [node for node in deps.get("nodes") or [] if isinstance(node, dict)]
    else:
        nodes = []
    updated = 0
    missing = 0
    for node in nodes:
        node_id = str(node.get("id") or node.get("node_id") or "")
        feature = by_id.get(node_id)
        if not feature:
            missing += 1
            continue
        feature_hash = str(feature.get("feature_hash") or "")
        file_hashes = feature.get("file_hashes") if isinstance(feature.get("file_hashes"), dict) else {}
        function_hashes = feature.get("function_hashes") if isinstance(feature.get("function_hashes"), dict) else {}
        test_function_hashes = (
            feature.get("test_function_hashes")
            if isinstance(feature.get("test_function_hashes"), dict)
            else {}
        )
        test_functions = feature.get("test_functions") if isinstance(feature.get("test_functions"), list) else []
        test_function_lines = (
            feature.get("test_function_lines")
            if isinstance(feature.get("test_function_lines"), dict)
            else {}
        )
        if not feature_hash and not file_hashes and not function_hashes and not test_function_hashes:
            missing += 1
            continue
        metadata = dict(node.get("metadata") or {}) if isinstance(node.get("metadata"), dict) else {}
        if feature_hash:
            metadata["feature_hash"] = feature_hash
            metadata["hash_scheme"] = "indexed_sha256"
        if file_hashes:
            metadata["file_hashes"] = {
                str(path): str(value)
                for path, value in file_hashes.items()
                if str(path)
            }
        if function_hashes:
            metadata["function_hashes"] = {
                str(function_id): str(value)
                for function_id, value in function_hashes.items()
                if str(function_id) and str(value)
            }
        if test_function_hashes:
            metadata["test_function_hashes"] = {
                str(function_id): str(value)
                for function_id, value in test_function_hashes.items()
                if str(function_id) and str(value)
            }
        if test_functions:
            metadata["test_functions"] = sorted(str(function_id) for function_id in test_functions if str(function_id))
        if test_function_lines:
            metadata["test_function_lines"] = {
                str(function_name): list(value)
                for function_name, value in test_function_lines.items()
                if str(function_name) and isinstance(value, list)
            }
        node["metadata"] = metadata
        updated += 1
    return {
        "features": len(by_id),
        "nodes_updated": updated,
        "nodes_missing": missing,
    }


def _path_list(node: dict[str, Any], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        raw = node.get(key)
        if isinstance(raw, list):
            values.extend(str(item or "") for item in raw)
        elif isinstance(raw, str) and raw:
            values.append(raw)
    out = []
    for value in values:
        norm = value.replace("\\", "/").strip("/")
        if norm:
            out.append(norm)
    return sorted(set(out))


def _node_attachment_index(nodes: Iterable[dict[str, Any]]) -> dict[str, dict[str, set[str]]]:
    index: dict[str, dict[str, set[str]]] = {}

    def add(node_id: str, role: str, paths: Iterable[str]) -> None:
        for path in paths:
            norm = str(path or "").replace("\\", "/").strip("/")
            if not norm:
                continue
            index.setdefault(norm, {}).setdefault(node_id, set()).add(role)

    for node in nodes:
        node_id = str(node.get("id") or node.get("node_id") or "")
        if not node_id:
            continue
        add(node_id, "primary", _path_list(node, "primary", "primary_files", "primary_file"))
        add(node_id, "doc", _path_list(node, "secondary", "secondary_files", "docs", "doc_files"))
        add(node_id, "test", _path_list(node, "test", "tests", "test_files"))
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        config_paths = _path_list(node, "config", "config_files")
        config_paths.extend(_path_list(metadata, "config_files"))
        add(node_id, "config", config_paths)
    return index


def _choose_attachment_role(file_kind: str, roles_by_node: dict[str, set[str]]) -> str:
    roles: set[str] = set()
    for values in roles_by_node.values():
        roles.update(values)
    if "primary" in roles:
        return "primary"
    if file_kind == "test" or "test" in roles:
        return "test"
    if file_kind in {"doc", "index_doc"} or "doc" in roles:
        return "doc"
    if file_kind == "config" or "config" in roles:
        return "config"
    return sorted(roles)[0] if roles else ""


def _enrich_file_inventory_attachments(
    file_inventory: list[dict[str, Any]],
    nodes: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach inventory rows to current graph nodes using explicit file roles."""
    attachment_index = _node_attachment_index(nodes)
    enriched: list[dict[str, Any]] = []
    for row in file_inventory:
        item = dict(row)
        path = str(item.get("path") or "").replace("\\", "/").strip("/")
        roles_by_node = attachment_index.get(path) or {}
        if roles_by_node:
            node_ids = sorted(roles_by_node)
            item["attached_node_ids"] = node_ids
            item["attachment_role"] = _choose_attachment_role(str(item.get("file_kind") or ""), roles_by_node)
            item["attachment_source"] = "graph_node"
            item["mapped_node_ids"] = node_ids
            if not item.get("candidate_node_id"):
                item["candidate_node_id"] = node_ids[0]
            if not item.get("attached_to"):
                item["attached_to"] = node_ids[0]
        else:
            item.setdefault("attached_node_ids", [])
            item.setdefault("attachment_role", "")
            item.setdefault("attachment_source", "")
        enriched.append(item)
    return enriched


def build_feature_index(
    *,
    nodes: Iterable[dict[str, Any]],
    file_inventory: list[dict[str, Any]],
    symbol_index: dict[str, Any],
    doc_index: dict[str, Any],
    commit_sha: str,
    snapshot_id: str = "",
) -> dict[str, Any]:
    """Build stable feature-to-file/symbol/doc/test bindings.

    This is a derived governance index.  It does not assert that semantic
    ownership is perfect; it records the graph's current feature bindings plus
    hashes/locations so drift and prompt context can be computed cheaply.
    """
    file_hashes = {
        str(row.get("path") or "").replace("\\", "/").strip("/"): str(
            row.get("file_hash") or (f"sha256:{row.get('sha256')}" if row.get("sha256") else "")
        )
        for row in file_inventory
        if row.get("path")
    }
    symbols_by_path: dict[str, list[dict[str, Any]]] = {}
    for symbol in (symbol_index.get("symbols") or []):
        if not isinstance(symbol, dict):
            continue
        path = str(symbol.get("path") or "").replace("\\", "/").strip("/")
        if path:
            symbols_by_path.setdefault(path, []).append(symbol)
    docs_by_path: dict[str, dict[str, Any]] = {}
    for doc in (doc_index.get("documents") or []):
        if not isinstance(doc, dict):
            continue
        path = str(doc.get("path") or "").replace("\\", "/").strip("/")
        if path:
            docs_by_path[path] = doc

    features: list[dict[str, Any]] = []
    for node in nodes:
        node_id = str(node.get("id") or node.get("node_id") or "")
        if not node_id:
            continue
        primary = _path_list(node, "primary", "primary_files")
        secondary = _path_list(node, "secondary", "secondary_files")
        tests = _path_list(node, "test", "test_files")
        config = _path_list(node, "config", "config_files")
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        if not config:
            config = _path_list(metadata, "config_files")
        if not (primary or secondary or tests or config):
            continue
        function_hashes = {
            str(function_id): str(value)
            for function_id, value in (
                metadata.get("function_hashes")
                if isinstance(metadata.get("function_hashes"), dict)
                else {}
            ).items()
            if str(function_id) and str(value)
        }
        symbol_refs = []
        for path in primary:
            for symbol in symbols_by_path.get(path, []):
                symbol_ref = {
                    "id": symbol.get("id") or "",
                    "kind": symbol.get("kind") or "",
                    "path": path,
                    "line_start": symbol.get("line_start", 0),
                    "line_end": symbol.get("line_end", 0),
                    "source_hash": symbol.get("source_hash") or "",
                }
                symbol_refs.append(symbol_ref)
                if symbol_ref["id"] and symbol_ref["source_hash"]:
                    function_hashes[str(symbol_ref["id"])] = str(symbol_ref["source_hash"])
        doc_refs = []
        for path in secondary:
            doc = docs_by_path.get(path)
            if not doc:
                continue
            doc_refs.append({
                "path": path,
                "heading_count": len(doc.get("headings") or []),
                "headings": doc.get("headings") or [],
            })
        test_symbol_refs = []
        test_function_hashes: dict[str, str] = {}
        test_function_lines: dict[str, list[int]] = {}
        test_functions: list[str] = []
        for path in tests:
            for symbol in symbols_by_path.get(path, []):
                symbol_id = str(symbol.get("id") or "")
                symbol_hash = str(symbol.get("source_hash") or "")
                symbol_kind = str(symbol.get("kind") or "")
                test_symbol_refs.append({
                    "id": symbol.get("id") or "",
                    "kind": symbol.get("kind") or "",
                    "path": path,
                    "line_start": symbol.get("line_start", 0),
                    "line_end": symbol.get("line_end", 0),
                    "source_hash": symbol.get("source_hash") or "",
                })
                if symbol_kind == "test_function" and symbol_id:
                    test_functions.append(symbol_id)
                    if symbol_hash:
                        test_function_hashes[symbol_id] = symbol_hash
                    function_name = symbol_id.rsplit("::", 1)[-1]
                    line_start = int(symbol.get("line_start", 0) or 0)
                    line_end = int(symbol.get("line_end", line_start) or line_start)
                    if function_name and line_start > 0:
                        line_range = [line_start, max(line_start, line_end)]
                        test_function_lines[symbol_id] = line_range
                        test_function_lines[function_name] = line_range
        config_refs = [
            {
                "path": path,
                "file_hash": file_hashes.get(path, ""),
            }
            for path in config
        ]
        payload = {
            "node_id": node_id,
            "title": node.get("title") or "",
            "layer": node.get("layer") or "",
            "primary": primary,
            "secondary": secondary,
            "test": tests,
            "config": config,
            "file_hashes": {path: file_hashes.get(path, "") for path in primary + secondary + tests + config},
            "function_hashes": function_hashes,
            "test_function_hashes": test_function_hashes,
            "test_functions": sorted(set(test_functions)),
            "test_function_lines": dict(sorted(test_function_lines.items())),
            "symbol_ids": [ref["id"] for ref in symbol_refs],
            "test_symbol_ids": [ref["id"] for ref in test_symbol_refs],
            "doc_paths": [ref["path"] for ref in doc_refs],
        }
        features.append({
            **payload,
            "feature_hash": _hash_payload(payload),
            "symbol_refs": symbol_refs,
            "test_symbol_refs": test_symbol_refs,
            "doc_refs": doc_refs,
            "config_refs": config_refs,
        })

    return {
        "generated_at": _utc_now(),
        "schema_version": GOVERNANCE_INDEX_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "commit_sha": commit_sha,
        "feature_count": len(features),
        "features": sorted(features, key=lambda item: str(item.get("node_id") or "")),
    }


def build_governance_index(
    conn: sqlite3.Connection,
    project_id: str,
    project_root: str | Path,
    *,
    run_id: str = "",
    commit_sha: str = "",
    profile: ProjectProfile | None = None,
    include_active_graph: bool = True,
    candidate_graph: dict[str, Any] | None = None,
    snapshot_id: str = "",
    snapshot_kind: str = "",
    file_inventory: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a governance index without mutating project source files."""
    ensure_graph_snapshot_schema(conn)
    root = Path(project_root).resolve()
    commit = commit_sha or _git_commit(root) or "unknown"
    rid = run_id or _make_run_id(commit)
    profile = profile or discover_project_profile(str(root))
    checkout_provenance = describe_checkout(
        root,
        project_id=project_id,
        commit_sha=commit,
    )

    source_snapshot: dict[str, Any] | None = None
    source_nodes: list[dict[str, Any]] = []
    index_scope = "no_graph"
    candidate_nodes = _nodes_from_candidate_graph(candidate_graph)
    if candidate_nodes:
        source_nodes = candidate_nodes
        source_snapshot = {
            "snapshot_id": snapshot_id,
            "commit_sha": commit,
            "snapshot_kind": snapshot_kind or "candidate",
            "candidate": True,
        }
        index_scope = "candidate_snapshot"
    elif include_active_graph:
        source_snapshot, source_nodes = load_active_snapshot_nodes(conn, project_id)
        index_scope = "active_snapshot" if source_snapshot else "no_graph"

    if source_nodes:
        governance_hint_bindings = apply_binding_hints_to_graph_nodes(root, source_nodes)
    else:
        governance_hint_bindings = {
            "hint_count": 0,
            "applied_count": 0,
            "skipped_count": 0,
            "applied": [],
            "skipped": [],
        }

    if file_inventory is None:
        file_inventory = build_file_inventory(
            project_root=str(root),
            run_id=rid,
            nodes=source_nodes,
            profile=profile,
            last_scanned_commit=commit,
        )
    else:
        file_inventory = [dict(row) for row in file_inventory if isinstance(row, dict)]
    file_inventory = _enrich_file_inventory_attachments(file_inventory, source_nodes)
    symbol_index = build_symbol_index(
        project_root=root,
        file_inventory=file_inventory,
        profile=profile,
    )
    doc_index = build_doc_index(project_root=root, file_inventory=file_inventory)
    doc_asset_state = build_doc_asset_state(
        project_id=project_id,
        run_id=rid,
        commit_sha=commit,
        file_inventory=file_inventory,
        graph_nodes=source_nodes,
    )
    coverage_state = build_coverage_state(
        candidate_graph=candidate_graph or _candidate_graph_from_nodes(source_nodes),
        file_inventory=file_inventory,
    )
    feature_index = build_feature_index(
        nodes=source_nodes,
        file_inventory=file_inventory,
        symbol_index=symbol_index,
        doc_index=doc_index,
        commit_sha=commit,
        snapshot_id=snapshot_id or (source_snapshot or {}).get("snapshot_id", ""),
    )
    coverage_state.update({
        "active_snapshot_id": source_snapshot.get("snapshot_id") if source_snapshot else "",
        "active_graph_commit": source_snapshot.get("commit_sha") if source_snapshot else "",
        "index_scope": index_scope,
        "commit_sha": commit,
        "run_id": rid,
        "schema_version": GOVERNANCE_INDEX_SCHEMA_VERSION,
        "symbol_count": symbol_index.get("symbol_count", 0),
        "doc_heading_count": doc_index.get("heading_count", 0),
        "feature_count": feature_index.get("feature_count", 0),
        "governance_hint_bindings": governance_hint_bindings,
    })

    return {
        "schema_version": GOVERNANCE_INDEX_SCHEMA_VERSION,
        "project_id": project_id,
        "run_id": rid,
        "commit_sha": commit,
        "generated_at": _utc_now(),
        "project_root": str(root),
        "project_root_role": "execution_root",
        "checkout_provenance": checkout_provenance,
        "active_snapshot": dict(source_snapshot) if source_snapshot else {},
        "active_node_count": len(source_nodes),
        "index_scope": index_scope,
        "profile": {
            **_json_safe(asdict(profile)),
            "project_root_role": "execution_root",
            "checkout_provenance": checkout_provenance,
        },
        "file_inventory": file_inventory,
        "file_inventory_summary": summarize_file_inventory(file_inventory),
        "governance_hint_bindings": governance_hint_bindings,
        "symbol_index": symbol_index,
        "doc_index": doc_index,
        "doc_asset_state": doc_asset_state,
        "feature_index": feature_index,
        "coverage_state": coverage_state,
    }


def governance_index_artifact_dir(
    project_id: str,
    run_id: str,
    *,
    artifact_root: str | Path | None = None,
) -> Path:
    if artifact_root is not None:
        return Path(artifact_root).resolve() / run_id
    from .db import _governance_root

    return _governance_root() / project_id / "governance-index" / run_id


def persist_governance_index(
    conn: sqlite3.Connection,
    project_id: str,
    index: dict[str, Any],
    *,
    artifact_root: str | Path | None = None,
    persist_inventory: bool = True,
) -> dict[str, Any]:
    """Write governance index artifacts and optionally persist inventory rows."""
    run_id = str(index.get("run_id") or "")
    if not run_id:
        raise ValueError("governance index is missing run_id")
    base = governance_index_artifact_dir(project_id, run_id, artifact_root=artifact_root)
    artifacts = {
        "profile_path": base / "project-profile.json",
        "file_inventory_path": base / "file-inventory.json",
        "symbol_index_path": base / "symbol-index.json",
        "doc_index_path": base / "doc-index.json",
        "doc_asset_state_path": base / "doc-asset-state.json",
        "feature_index_path": base / "feature-index.json",
        "coverage_state_path": base / "coverage-state.json",
        "summary_path": base / "summary.json",
    }
    _write_json(artifacts["profile_path"], index.get("profile") or {})
    _write_json(artifacts["file_inventory_path"], index.get("file_inventory") or [])
    _write_json(artifacts["symbol_index_path"], index.get("symbol_index") or {})
    _write_json(artifacts["doc_index_path"], index.get("doc_index") or {})
    _write_json(artifacts["doc_asset_state_path"], index.get("doc_asset_state") or {})
    _write_json(artifacts["feature_index_path"], index.get("feature_index") or {})
    _write_json(artifacts["coverage_state_path"], index.get("coverage_state") or {})

    inventory_count = 0
    asset_projection_count = 0
    asset_binding_count = 0
    if persist_inventory:
        inventory_count = upsert_file_inventory(
            conn,
            project_id,
            index.get("file_inventory") or [],
            replace_run=True,
        )
        projection_summary = upsert_doc_asset_projection(
            conn,
            project_id=project_id,
            snapshot_id=(index.get("active_snapshot") or {}).get("snapshot_id", ""),
            doc_asset_state=index.get("doc_asset_state") or {},
        )
        asset_projection_count = int(projection_summary.get("projection_count") or 0)
        asset_binding_count = int(projection_summary.get("binding_count") or 0)

    summary = {
        "schema_version": GOVERNANCE_INDEX_SCHEMA_VERSION,
        "project_id": project_id,
        "run_id": run_id,
        "commit_sha": index.get("commit_sha") or "",
        "active_snapshot_id": (index.get("active_snapshot") or {}).get("snapshot_id", ""),
        "active_graph_commit": (index.get("active_snapshot") or {}).get("commit_sha", ""),
        "active_node_count": index.get("active_node_count", 0),
        "index_scope": index.get("index_scope", ""),
        "file_inventory_summary": index.get("file_inventory_summary") or {},
        "symbol_count": (index.get("symbol_index") or {}).get("symbol_count", 0),
        "doc_heading_count": (index.get("doc_index") or {}).get("heading_count", 0),
        "doc_asset_state": (index.get("doc_asset_state") or {}).get("summary", {}),
        "asset_projection_rows_persisted": asset_projection_count,
        "asset_binding_rows_persisted": asset_binding_count,
        "feature_count": (index.get("feature_index") or {}).get("feature_count", 0),
        "inventory_rows_persisted": inventory_count,
        "artifacts": {name: str(path) for name, path in artifacts.items()},
        "generated_at": _utc_now(),
    }
    _write_json(artifacts["summary_path"], summary)
    return summary


def build_and_persist_governance_index(
    conn: sqlite3.Connection,
    project_id: str,
    project_root: str | Path,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build and persist a governance index in one explicit state operation."""
    persist_keys = {"artifact_root", "persist_inventory"}
    build_kwargs = {k: v for k, v in kwargs.items() if k not in persist_keys}
    index = build_governance_index(conn, project_id, project_root, **build_kwargs)
    summary = persist_governance_index(
        conn,
        project_id,
        index,
        artifact_root=kwargs.get("artifact_root"),
        persist_inventory=bool(kwargs.get("persist_inventory", True)),
    )
    return {**index, "persist_summary": summary}


__all__ = [
    "GOVERNANCE_INDEX_SCHEMA_VERSION",
    "build_feature_index",
    "build_and_persist_governance_index",
    "build_governance_index",
    "governance_index_artifact_dir",
    "load_active_snapshot_nodes",
    "load_snapshot_nodes_for_inventory",
    "merge_feature_hashes_into_graph_nodes",
    "persist_governance_index",
]
