"""State-only routing for reconcile semantic feedback.

This module turns semantic ``open_issues`` into auditable feedback items.  It
does not mutate project files or activate graph snapshots; callers decide
whether a feedback item becomes a graph-only correction or a project backlog
row.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import graph_snapshot_store as store


FEEDBACK_EVENTS_NAME = "reconcile-feedback-events.jsonl"
FEEDBACK_STATE_NAME = "reconcile-feedback-state.json"

try:  # pragma: no cover - exercised through the lock path on Unix runners.
    import fcntl as _fcntl
except Exception:  # noqa: BLE001 - Windows fallback keeps the in-process lock.
    _fcntl = None

KIND_GRAPH_CORRECTION = "graph_correction"
KIND_PROJECT_IMPROVEMENT = "project_improvement"
KIND_STATUS_OBSERVATION = "status_observation"
KIND_NEEDS_OBSERVER_DECISION = "needs_observer_decision"
KIND_FALSE_POSITIVE = "false_positive"

STATUS_CATEGORY_STALE_TEST = "stale_test_expectation"
STATUS_CATEGORY_DOC_DRIFT = "doc_drift"
STATUS_CATEGORY_COVERAGE_GAP = "coverage_gap"
STATUS_CATEGORY_PROJECT_REGRESSION = "project_regression"
STATUS_CATEGORY_ORPHAN_REVIEW = "orphan_review"
STATUS_CATEGORY_FALSE_POSITIVE = "false_positive"
STATUS_CATEGORY_NEEDS_HUMAN = "needs_human_signoff"

STATUS_OBSERVATION_CATEGORIES = {
    STATUS_CATEGORY_STALE_TEST,
    STATUS_CATEGORY_DOC_DRIFT,
    STATUS_CATEGORY_COVERAGE_GAP,
    STATUS_CATEGORY_PROJECT_REGRESSION,
    STATUS_CATEGORY_ORPHAN_REVIEW,
    STATUS_CATEGORY_FALSE_POSITIVE,
    STATUS_CATEGORY_NEEDS_HUMAN,
}

STATUS_CLASSIFIED = "classified"
STATUS_REVIEWED = "reviewed"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
STATUS_BACKLOG_FILED = "backlog_filed"
STATUS_NEEDS_HUMAN_SIGNOFF = "needs_human_signoff"

FEEDBACK_CATEGORY_SEMANTIC = "semantic"
FEEDBACK_CATEGORY_GRAPH_STRUCTURE = "graph_structure"
FEEDBACK_CATEGORY_GRAPH_ENRICH_CONFIG = "graph_enrich_config"
FEEDBACK_CATEGORY_ASSET_BINDING = "asset_binding"
FEEDBACK_CATEGORY_DOC_BINDING = "doc_binding"
FEEDBACK_CATEGORY_TEST_BINDING = "test_binding"
FEEDBACK_CATEGORY_CONFIG_BINDING = "config_binding"
FEEDBACK_CATEGORY_STATUS_OBSERVATION = "status_observation"
FEEDBACK_CATEGORY_BACKLOG = "backlog"
FEEDBACK_CATEGORY_OTHER = "other"

FEEDBACK_CATEGORIES: dict[str, dict[str, str]] = {
    FEEDBACK_CATEGORY_SEMANTIC: {
        "label": "Semantic",
        "description": "Semantic memory or AI enrichment review.",
    },
    FEEDBACK_CATEGORY_GRAPH_STRUCTURE: {
        "label": "Graph structure",
        "description": "Graph topology, relation, node, or role correction.",
    },
    FEEDBACK_CATEGORY_GRAPH_ENRICH_CONFIG: {
        "label": "Graph enrich config",
        "description": "Semantic enrichment configuration, predicate, or action registration.",
    },
    FEEDBACK_CATEGORY_ASSET_BINDING: {
        "label": "Asset binding",
        "description": "Generic source-controlled asset binding review.",
    },
    FEEDBACK_CATEGORY_DOC_BINDING: {
        "label": "Doc binding",
        "description": "Documentation asset binding or documentation coverage review.",
    },
    FEEDBACK_CATEGORY_TEST_BINDING: {
        "label": "Test binding",
        "description": "Test asset binding, stale expectation, or coverage review.",
    },
    FEEDBACK_CATEGORY_CONFIG_BINDING: {
        "label": "Config binding",
        "description": "Configuration asset binding or config coverage review.",
    },
    FEEDBACK_CATEGORY_STATUS_OBSERVATION: {
        "label": "Status observation",
        "description": "Informational observation kept out of action lanes by default.",
    },
    FEEDBACK_CATEGORY_BACKLOG: {
        "label": "Backlog",
        "description": "Project improvement or backlog filing candidate.",
    },
    FEEDBACK_CATEGORY_OTHER: {
        "label": "Other",
        "description": "Review item that does not match a more specific category.",
    },
}

FEEDBACK_DECISION_ACTIONS = {
    "accept_graph_correction",
    "accept_project_improvement",
    "accept_semantic_enrichment",  # MF-2026-05-10-016: gates semantic worker output
    "revise_semantic_enrichment",
    "keep_status_observation",
    "reject_false_positive",
    "needs_human_signoff",
    "reclassify",
}

REVIEW_DECISIONS = {
    KIND_GRAPH_CORRECTION,
    KIND_PROJECT_IMPROVEMENT,
    KIND_STATUS_OBSERVATION,
    KIND_FALSE_POSITIVE,
    "needs_human_signoff",
}

ReviewerAiCall = Callable[[str, dict[str, Any]], dict[str, Any]]


def feedback_action_catalog() -> dict[str, Any]:
    return {
        "lanes": {
            "review_required": {
                "label": "Review required",
                "primary_actions": ["review", "decision"],
            },
            "candidate_backlog": {
                "label": "Candidate backlog",
                "primary_actions": ["review", "file_backlog", "decision"],
            },
            "graph_patch_candidate": {
                "label": "Graph patch candidate",
                "primary_actions": ["review", "accept_graph_correction", "graph_patches", "decision"],
            },
            "status_only": {
                "label": "Status observation",
                "primary_actions": ["keep_status_observation", "file_backlog", "reject_false_positive"],
            },
            "resolved": {
                "label": "Resolved",
                "primary_actions": [],
            },
        },
        "decision_actions": sorted(FEEDBACK_DECISION_ACTIONS),
        "review_decisions": sorted(REVIEW_DECISIONS),
        "categories": FEEDBACK_CATEGORIES,
        "category_order": list(FEEDBACK_CATEGORIES.keys()),
        "status_observation_categories": sorted(STATUS_OBSERVATION_CATEGORIES),
        "endpoints": {
            "submit_feedback": "POST /api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback",
            "queue": "GET /api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/queue",
            "review": "POST /api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/review",
            "decision": "POST /api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/decision",
            "graph_patches": "POST /api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/graph-patches",
            "file_backlog": "POST /api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/file-backlog",
        },
    }

MAX_READ_EXCERPT_LINES = 200
MAX_GREP_PATTERN_CHARS = 200
MAX_GREP_FILE_BYTES = 2_000_000
DEFAULT_REVIEW_LEASE_SECONDS = 1800

_REVIEW_CARRY_FORWARD_KEYS = {
    "status",
    "final_feedback_kind",
    "reviewed_status_observation_category",
    "reviewer_decision",
    "reviewer_rationale",
    "reviewer_model",
    "reviewer_confidence",
    "reviewed_by",
    "reviewed_at",
    "requires_human_signoff",
    "accepted_by",
    "accepted_at",
    "backlog_bug_id",
    "graph_correction_patch_id",
    "graph_correction_patch_status",
    "graph_correction_patch_type",
}

_FEEDBACK_STATE_LOCKS_GUARD = threading.Lock()
_FEEDBACK_STATE_LOCKS: dict[str, threading.RLock] = {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            fh.write(_json(payload))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
        except Exception:  # noqa: BLE001 - best-effort directory fsync.
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(_json(row) + "\n")


def feedback_base_dir(project_id: str, snapshot_id: str) -> Path:
    return store.snapshot_companion_dir(project_id, snapshot_id) / "semantic-enrichment"


def feedback_state_path(project_id: str, snapshot_id: str) -> Path:
    return feedback_base_dir(project_id, snapshot_id) / FEEDBACK_STATE_NAME


def feedback_events_path(project_id: str, snapshot_id: str) -> Path:
    return feedback_base_dir(project_id, snapshot_id) / FEEDBACK_EVENTS_NAME


def _feedback_state_thread_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _FEEDBACK_STATE_LOCKS_GUARD:
        lock = _FEEDBACK_STATE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _FEEDBACK_STATE_LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def _feedback_state_update_lock(project_id: str, snapshot_id: str):
    state_path = feedback_state_path(project_id, snapshot_id)
    thread_lock = _feedback_state_thread_lock(state_path)
    with thread_lock:
        lock_path = state_path.with_suffix(state_path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as fh:
            if _fcntl is not None:
                _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
            try:
                yield
            finally:
                if _fcntl is not None:
                    _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)


def semantic_graph_state_path(project_id: str, snapshot_id: str) -> Path:
    return feedback_base_dir(project_id, snapshot_id) / "semantic-graph-state.json"


def _snapshot_graph_path(project_id: str, snapshot_id: str) -> Path:
    return store.snapshot_companion_dir(project_id, snapshot_id) / "graph.json"


def _truncate_text(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def _safe_project_file(project_root: str | Path | None, rel_path: str) -> Path | None:
    if not project_root or not rel_path:
        return None
    root = Path(project_root).resolve()
    try:
        candidate = (root / rel_path).resolve()
        candidate.relative_to(root)
    except Exception:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _safe_relative_path(rel_path: str) -> str:
    text = str(rel_path or "").replace("\\", "/").strip()
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return ""
    parts = [part for part in text.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _file_excerpt(project_root: str | Path | None, rel_path: str, *, max_chars: int) -> dict[str, Any] | None:
    rel_path = _safe_relative_path(rel_path)
    path = _safe_project_file(project_root, rel_path)
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    return {
        "path": rel_path,
        "size_bytes": path.stat().st_size,
        "excerpt": _truncate_text(text, max_chars),
    }


def _line_excerpt(
    project_root: str | Path | None,
    rel_path: str,
    *,
    line_start: int,
    line_end: int,
    context_lines: int = 8,
    max_chars: int = 4000,
) -> dict[str, Any] | None:
    rel_path = _safe_relative_path(rel_path)
    path = _safe_project_file(project_root, rel_path)
    if path is None:
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None
    start = max(1, int(line_start or 1) - context_lines)
    end = min(len(lines), int(line_end or line_start or 1) + context_lines)
    numbered = [
        f"{line_no}: {lines[line_no - 1]}"
        for line_no in range(start, end + 1)
    ]
    return {
        "path": rel_path,
        "line_start": start,
        "line_end": end,
        "excerpt": _truncate_text("\n".join(numbered), max_chars),
    }


def _graph_nodes_by_id(project_id: str, snapshot_id: str) -> dict[str, dict[str, Any]]:
    graph = _read_json(_snapshot_graph_path(project_id, snapshot_id), {})
    nodes = (((graph or {}).get("deps_graph") or {}).get("nodes") or [])
    return {
        str(node.get("id")): node
        for node in nodes
        if isinstance(node, dict) and str(node.get("id") or "")
    }


def _semantic_features_by_id(project_id: str, snapshot_id: str) -> dict[str, dict[str, Any]]:
    index = _read_json(feedback_base_dir(project_id, snapshot_id) / "semantic-index.json", {})
    features = index.get("features") if isinstance(index, dict) else []
    return {
        str(feature.get("node_id")): feature
        for feature in (features or [])
        if isinstance(feature, dict) and str(feature.get("node_id") or "")
    }


def _compact_graph_node(node: dict[str, Any]) -> dict[str, Any]:
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    return {
        "id": node.get("id", ""),
        "title": node.get("title", ""),
        "layer": node.get("layer", ""),
        "kind": node.get("kind", ""),
        "primary": node.get("primary") or [],
        "secondary": node.get("secondary") or [],
        "test": node.get("test") or [],
        "config": node.get("config") or metadata.get("config_files") or [],
        "metadata": {
            "module": metadata.get("module", ""),
            "hierarchy_parent": metadata.get("hierarchy_parent", ""),
            "roles": (((metadata.get("architecture_signals") or {}).get("roles")) or []),
            "typed_relations": (metadata.get("typed_relations") or [])[:20],
        },
    }


def _graph_edges(project_id: str, snapshot_id: str) -> list[dict[str, Any]]:
    graph = _read_json(_snapshot_graph_path(project_id, snapshot_id), {})
    edges = (((graph or {}).get("deps_graph") or {}).get("edges") or [])
    return [dict(edge) for edge in edges if isinstance(edge, dict)]


def _node_scope_paths(nodes: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        compact = _compact_graph_node(node)
        for key in ("primary", "secondary", "test", "config"):
            for raw_path in _string_list(compact.get(key)):
                rel_path = _safe_relative_path(raw_path)
                if rel_path and rel_path not in seen:
                    seen.add(rel_path)
                    paths.append(rel_path)
    return paths


def graph_query_context(
    project_id: str,
    snapshot_id: str,
    *,
    node_ids: list[str] | None = None,
    depth: int = 1,
    max_nodes: int = 24,
    max_edges: int = 80,
) -> dict[str, Any]:
    """Return a compact, read-only graph slice for reviewer/AI context."""
    requested = [str(node_id).strip() for node_id in (node_ids or []) if str(node_id).strip()]
    nodes_by_id = _graph_nodes_by_id(project_id, snapshot_id)
    edges = _graph_edges(project_id, snapshot_id)
    depth = max(0, min(int(depth), 3))
    selected: set[str] = set(requested)
    frontier: set[str] = set(requested)
    for _ in range(depth):
        if not frontier:
            break
        next_frontier: set[str] = set()
        for edge in edges:
            source = str(edge.get("source") or edge.get("from") or "")
            target = str(edge.get("target") or edge.get("to") or "")
            if source in frontier and target:
                next_frontier.add(target)
            if target in frontier and source:
                next_frontier.add(source)
        next_frontier -= selected
        selected.update(next_frontier)
        frontier = next_frontier
    all_selected_count = len(selected)
    ordered_node_ids = [node_id for node_id in requested if node_id in selected]
    ordered_node_ids.extend(sorted(node_id for node_id in selected if node_id not in ordered_node_ids))
    ordered_node_ids = ordered_node_ids[: max(0, int(max_nodes))]
    selected = set(ordered_node_ids)
    selected_edges = []
    for edge in edges:
        source = str(edge.get("source") or edge.get("from") or "")
        target = str(edge.get("target") or edge.get("to") or "")
        if source in selected or target in selected:
            selected_edges.append({
                "source": source,
                "target": target,
                "edge_type": edge.get("edge_type") or edge.get("type") or "",
                "direction": edge.get("direction", ""),
                "evidence": edge.get("evidence", {}),
            })
        if len(selected_edges) >= max(0, int(max_edges)):
            break
    selected_nodes = [
        _compact_graph_node(nodes_by_id[node_id])
        for node_id in ordered_node_ids
        if node_id in nodes_by_id
    ]
    return {
        "node_ids": ordered_node_ids,
        "nodes": selected_nodes,
        "edges": selected_edges,
        "truncated": len(ordered_node_ids) < all_selected_count,
    }


def read_project_excerpt(
    project_root: str | Path | None,
    rel_path: str,
    *,
    line_start: int = 1,
    line_end: int | None = None,
    max_lines: int = MAX_READ_EXCERPT_LINES,
    max_chars: int = 8000,
) -> dict[str, Any]:
    """Read a bounded project-root-relative excerpt."""
    rel_path = _safe_relative_path(rel_path)
    if not rel_path:
        return {"ok": False, "error": "invalid_path", "path": str(rel_path or "")}
    path = _safe_project_file(project_root, rel_path)
    if path is None:
        return {"ok": False, "error": "path_not_found_or_out_of_scope", "path": rel_path}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "path": rel_path}
    start = max(1, int(line_start or 1))
    if line_end is None:
        end = min(len(lines), start + max(1, int(max_lines)) - 1)
    else:
        end = min(len(lines), max(start, int(line_end)))
    if end - start + 1 > max_lines:
        end = start + max(1, int(max_lines)) - 1
    numbered = [f"{line_no}: {lines[line_no - 1]}" for line_no in range(start, end + 1)]
    return {
        "ok": True,
        "path": rel_path,
        "line_start": start,
        "line_end": end,
        "excerpt": _truncate_text("\n".join(numbered), max_chars),
        "line_count": len(lines),
    }


def grep_in_scope(
    project_id: str,
    snapshot_id: str,
    *,
    project_root: str | Path | None,
    pattern: str,
    node_ids: list[str] | None = None,
    paths: list[str] | None = None,
    case_sensitive: bool = False,
    regex: bool = False,
    max_matches: int = 20,
    max_chars: int = 8000,
) -> dict[str, Any]:
    """Run bounded read-only grep over graph-scoped files."""
    pattern = str(pattern or "")
    if not pattern.strip():
        return {"ok": False, "error": "empty_pattern", "matches": []}
    if len(pattern) > MAX_GREP_PATTERN_CHARS:
        return {"ok": False, "error": "pattern_too_long", "matches": []}
    scoped_paths = [_safe_relative_path(path) for path in (paths or [])]
    scoped_paths = [path for path in scoped_paths if path]
    if not scoped_paths:
        nodes_by_id = _graph_nodes_by_id(project_id, snapshot_id)
        selected_nodes = [
            nodes_by_id[node_id]
            for node_id in (node_ids or [])
            if node_id in nodes_by_id
        ]
        scoped_paths = _node_scope_paths(selected_nodes)
    if not scoped_paths:
        return {"ok": False, "error": "empty_scope", "matches": []}

    matcher = None
    needle = pattern if case_sensitive else pattern.lower()
    if regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            matcher = re.compile(pattern, flags)
        except re.error as exc:
            return {"ok": False, "error": f"invalid_regex: {exc}", "matches": []}

    matches: list[dict[str, Any]] = []
    scanned_paths: list[str] = []
    for rel_path in scoped_paths:
        if len(matches) >= max_matches:
            break
        path = _safe_project_file(project_root, rel_path)
        if path is None:
            continue
        try:
            if path.stat().st_size > MAX_GREP_FILE_BYTES:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        scanned_paths.append(rel_path)
        for line_no, line in enumerate(lines, start=1):
            haystack = line if case_sensitive else line.lower()
            found = bool(matcher.search(line)) if matcher else needle in haystack
            if not found:
                continue
            matches.append({
                "path": rel_path,
                "line_no": line_no,
                "line": _truncate_text(line.strip(), 600),
            })
            if len(matches) >= max_matches:
                break
    payload = {
        "ok": True,
        "pattern": pattern,
        "regex": bool(regex),
        "case_sensitive": bool(case_sensitive),
        "scanned_paths": scanned_paths,
        "matches": matches,
        "match_count": len(matches),
        "truncated": len(matches) >= max_matches,
    }
    payload["matches_excerpt"] = _truncate_text(_json(matches), max_chars)
    return payload


def _feedback_keyword_patterns(item: dict[str, Any], *, limit: int = 4) -> list[str]:
    text = " ".join([
        str(item.get("issue") or ""),
        str(item.get("suggested_action") or ""),
        str(item.get("target_id") or ""),
        str(((item.get("evidence") or {}).get("raw_issue") or {}).get("summary") or ""),
    ])
    candidates: list[str] = []
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{4,}", text):
        if token.lower() in {"false", "true", "none", "graph", "node", "feature", "should"}:
            continue
        if token not in candidates:
            candidates.append(token)
        if len(candidates) >= limit:
            break
    return candidates


def build_feedback_retrieval_context(
    project_id: str,
    snapshot_id: str,
    item: dict[str, Any],
    *,
    project_root: str | Path | None = None,
    grep_patterns: list[str] | None = None,
    max_grep_matches: int = 12,
    max_chars: int = 12000,
) -> dict[str, Any]:
    """Precompute safe graph/grep/read results for an AI reviewer."""
    source_node_ids = _source_nodes(item)
    graph_context = graph_query_context(
        project_id,
        snapshot_id,
        node_ids=source_node_ids,
        depth=1,
    )
    scoped_paths = _node_scope_paths([
        node
        for node in _graph_nodes_by_id(project_id, snapshot_id).values()
        if str(node.get("id") or "") in set(source_node_ids)
    ])
    patterns = [str(pattern) for pattern in (grep_patterns or []) if str(pattern or "").strip()]
    if not patterns:
        patterns = _feedback_keyword_patterns(item)
    grep_results = [
        grep_in_scope(
            project_id,
            snapshot_id,
            project_root=project_root,
            pattern=pattern,
            paths=scoped_paths,
            max_matches=max_grep_matches,
            max_chars=max(1000, int(max_chars / max(1, len(patterns) or 1))),
        )
        for pattern in patterns[:6]
    ]
    return {
        "tool_contract": {
            "mode": "read_only",
            "allowed_tools": ["graph_query", "grep_in_scope", "read_excerpt"],
            "root_scope": "project_root_only",
            "limits": {
                "max_grep_pattern_chars": MAX_GREP_PATTERN_CHARS,
                "max_grep_file_bytes": MAX_GREP_FILE_BYTES,
                "max_read_excerpt_lines": MAX_READ_EXCERPT_LINES,
            },
        },
        "graph_query": graph_context,
        "grep_results": grep_results,
        "scoped_paths": scoped_paths,
    }


def _build_review_context(
    project_id: str,
    snapshot_id: str,
    item: dict[str, Any],
    *,
    project_root: str | Path | None = None,
    max_excerpt_chars: int = 6000,
    enable_read_tools: bool = True,
    grep_patterns: list[str] | None = None,
) -> dict[str, Any]:
    semantic_state = _read_json(semantic_graph_state_path(project_id, snapshot_id), {})
    node_semantics = semantic_state.get("node_semantics") if isinstance(semantic_state, dict) else {}
    if not isinstance(node_semantics, dict):
        node_semantics = {}
    graph_nodes = _graph_nodes_by_id(project_id, snapshot_id)
    semantic_features = _semantic_features_by_id(project_id, snapshot_id)
    source_node_ids = _source_nodes(item)

    source_nodes: list[dict[str, Any]] = []
    candidate_paths: list[str] = []
    candidate_symbol_refs: list[dict[str, Any]] = []
    issue_text = " ".join([
        str(item.get("issue") or ""),
        str(item.get("suggested_action") or ""),
        str(item.get("target_id") or ""),
        str(((item.get("evidence") or {}).get("raw_issue") or {}).get("summary") or ""),
    ]).lower()
    for node_id in source_node_ids:
        graph_node = graph_nodes.get(node_id) or {}
        state_semantic = node_semantics.get(node_id) if isinstance(node_semantics.get(node_id), dict) else {}
        index_semantic = semantic_features.get(node_id) or {}
        semantic_node = {**index_semantic, **state_semantic}
        compact = _compact_graph_node(graph_node) if graph_node else {"id": node_id}
        source_nodes.append({
            "node_id": node_id,
            "graph_node": compact,
            "semantic": {
                "feature_name": semantic_node.get("feature_name", ""),
                "semantic_summary": semantic_node.get("semantic_summary", ""),
                "intent": semantic_node.get("intent", ""),
                "domain_label": semantic_node.get("domain_label", ""),
                "feedback_round": semantic_node.get("feedback_round", ""),
                "quality_flags": semantic_node.get("quality_flags") or [],
                "symbol_refs": (semantic_node.get("symbol_refs") or [])[:40],
                "test_symbol_refs": (semantic_node.get("test_symbol_refs") or [])[:40],
            },
        })
        for key in ("primary", "secondary", "test", "config"):
            candidate_paths.extend(_string_list(compact.get(key)))
        for ref in (semantic_node.get("symbol_refs") or []) + (semantic_node.get("test_symbol_refs") or []):
            if not isinstance(ref, dict):
                continue
            symbol_id = str(ref.get("id") or "")
            symbol_name = symbol_id.rsplit("::", 1)[-1].lower()
            if symbol_name and (symbol_name in issue_text or symbol_id.lower() in issue_text):
                candidate_symbol_refs.append(ref)

    target_id = str(item.get("target_id") or "")
    if "/" in target_id or "\\" in target_id or "." in Path(target_id).name:
        candidate_paths.insert(0, target_id.replace("\\", "/"))

    excerpts: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    per_file = max(1000, int(max_excerpt_chars / max(1, min(6, len(candidate_paths) or 1))))
    for rel_path in candidate_paths:
        rel_path = str(rel_path or "").replace("\\", "/")
        if not rel_path or rel_path in seen_paths:
            continue
        excerpt = _file_excerpt(project_root, rel_path, max_chars=per_file)
        if excerpt:
            excerpts.append(excerpt)
            seen_paths.add(rel_path)
        if len(excerpts) >= 6:
            break

    symbol_excerpts: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for ref in candidate_symbol_refs:
        symbol_id = str(ref.get("id") or "")
        if not symbol_id or symbol_id in seen_symbols:
            continue
        excerpt = _line_excerpt(
            project_root,
            str(ref.get("path") or ""),
            line_start=int(ref.get("line_start") or 1),
            line_end=int(ref.get("line_end") or ref.get("line_start") or 1),
            max_chars=max(1200, int(max_excerpt_chars / max(1, min(4, len(candidate_symbol_refs) or 1)))),
        )
        if excerpt:
            excerpt["symbol_id"] = symbol_id
            excerpt["kind"] = ref.get("kind", "")
            symbol_excerpts.append(excerpt)
            seen_symbols.add(symbol_id)
        if len(symbol_excerpts) >= 4:
            break

    context = {
        "snapshot_id": snapshot_id,
        "source_node_ids": source_node_ids,
        "source_nodes": source_nodes,
        "target": {
            "target_type": item.get("target_type", ""),
            "target_id": target_id,
            "issue_type": item.get("issue_type", ""),
        },
        "file_excerpts": excerpts,
        "symbol_excerpts": symbol_excerpts,
    }
    if enable_read_tools:
        context["read_tools"] = build_feedback_retrieval_context(
            project_id,
            snapshot_id,
            item,
            project_root=project_root,
            grep_patterns=grep_patterns,
            max_chars=max_excerpt_chars,
        )
    return context


def _new_state(project_id: str, snapshot_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "items": {},
        "updated_at": _utc_now(),
    }


def load_feedback_state(project_id: str, snapshot_id: str) -> dict[str, Any]:
    state = _read_json(feedback_state_path(project_id, snapshot_id), {})
    if not isinstance(state, dict) or not isinstance(state.get("items"), dict):
        state = _new_state(project_id, snapshot_id)
    state.setdefault("schema_version", 1)
    state.setdefault("project_id", project_id)
    state.setdefault("snapshot_id", snapshot_id)
    state.setdefault("items", {})
    return state


def save_feedback_state(project_id: str, snapshot_id: str, state: dict[str, Any]) -> None:
    state["updated_at"] = _utc_now()
    _write_json(feedback_state_path(project_id, snapshot_id), state)


def list_feedback_items(
    project_id: str,
    snapshot_id: str,
    *,
    feedback_kind: str = "",
    status: str = "",
    node_id: str = "",
    limit: int | None = None,
) -> list[dict[str, Any]]:
    state = load_feedback_state(project_id, snapshot_id)
    items = list((state.get("items") or {}).values())
    if feedback_kind:
        items = [item for item in items if item.get("feedback_kind") == feedback_kind]
    if status:
        items = [item for item in items if item.get("status") == status]
    if node_id:
        items = [item for item in items if node_id in (item.get("source_node_ids") or [])]
    items.sort(key=lambda item: (str(item.get("priority") or "P3"), str(item.get("feedback_id") or "")))
    if limit is not None and limit >= 0:
        items = items[: int(limit)]
    return items


def _priority_rank(priority: Any) -> int:
    return {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(str(priority or "P3").upper(), 4)


def _feedback_lane(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "").strip()
    kind = str(item.get("final_feedback_kind") or item.get("feedback_kind") or "").strip()
    if status in {STATUS_REVIEWED, STATUS_ACCEPTED, STATUS_REJECTED, STATUS_BACKLOG_FILED}:
        return "resolved"
    if status == STATUS_NEEDS_HUMAN_SIGNOFF or item.get("requires_human_signoff") or kind == KIND_NEEDS_OBSERVER_DECISION:
        return "review_required"
    if kind == KIND_PROJECT_IMPROVEMENT:
        return "candidate_backlog"
    if kind == KIND_STATUS_OBSERVATION:
        return "status_only"
    if kind == KIND_FALSE_POSITIVE:
        return "resolved"
    return "graph_patch_candidate"


def _feedback_category_text(item: dict[str, Any]) -> str:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    raw_issue = evidence.get("raw_issue") if isinstance(evidence.get("raw_issue"), dict) else {}
    return " ".join(
        str(part or "").lower()
        for part in [
            item.get("feedback_kind"),
            item.get("final_feedback_kind"),
            item.get("issue_type"),
            item.get("status_observation_category"),
            item.get("reviewed_status_observation_category"),
            item.get("target_type"),
            item.get("target_id"),
            item.get("source_round"),
            item.get("issue"),
            item.get("suggested_action"),
            evidence.get("reason"),
            raw_issue.get("reason"),
            raw_issue.get("kind"),
            raw_issue.get("type"),
            raw_issue.get("source"),
            raw_issue.get("category"),
        ]
    )


def _feedback_category(item: dict[str, Any]) -> str:
    """Derive a stable Review Queue category from server-side item metadata."""
    kind = str(item.get("final_feedback_kind") or item.get("feedback_kind") or "").strip()
    status = str(item.get("status") or "").strip()
    target_type = str(item.get("target_type") or "").strip().lower()
    issue_type = str(item.get("issue_type") or "").strip().lower()
    status_category = str(
        item.get("reviewed_status_observation_category")
        or item.get("status_observation_category")
        or ""
    ).strip().lower()
    text = _feedback_category_text(item)

    if kind == KIND_STATUS_OBSERVATION:
        return FEEDBACK_CATEGORY_STATUS_OBSERVATION
    if kind == KIND_PROJECT_IMPROVEMENT or status == STATUS_BACKLOG_FILED or item.get("backlog_bug_id"):
        return FEEDBACK_CATEGORY_BACKLOG
    if kind == KIND_FALSE_POSITIVE:
        return FEEDBACK_CATEGORY_OTHER
    if (
        "graph_enrich_config" in text
        or "graph enrich config" in text
        or "enrich_config" in text
        or "semantic_enrichment_config" in text
        or "semantic config" in text
        or "config_patch" in text
        or "registered_action" in text
        or "enricher" in text
        or "predicate" in text
    ):
        return FEEDBACK_CATEGORY_GRAPH_ENRICH_CONFIG
    binding_text = (
        "binding" in text
        or "asset" in text
        or "orphan" in text
        or "unmapped" in text
        or "coverage_gap" in text
        or "coverage gap" in text
    )
    if binding_text:
        if target_type == "doc" or "doc" in issue_type or "doc" in status_category:
            return FEEDBACK_CATEGORY_DOC_BINDING
        if target_type == "test" or "test" in issue_type or "test" in status_category:
            return FEEDBACK_CATEGORY_TEST_BINDING
        if target_type == "config" or "config" in issue_type or "config" in text:
            return FEEDBACK_CATEGORY_CONFIG_BINDING
        if "asset" in text or "binding" in text or "orphan" in text or "unmapped" in text:
            return FEEDBACK_CATEGORY_ASSET_BINDING
    if (
        kind == KIND_GRAPH_CORRECTION
        or "graph_structure" in text
        or "graph structure" in text
        or "add_relation" in issue_type
        or "typed_relation" in issue_type
        or "relation" in text
        or "edge" in text
        or "split" in text
        or "merge" in text
        or "reclassify" in text
    ):
        return FEEDBACK_CATEGORY_GRAPH_STRUCTURE
    if (
        "semantic" in text
        or str(item.get("source_round") or "").startswith("round-")
        or "ai_enrich" in text
        or "ai enrich" in text
    ):
        return FEEDBACK_CATEGORY_SEMANTIC
    return FEEDBACK_CATEGORY_OTHER


def _feedback_category_label(category: str) -> str:
    metadata = FEEDBACK_CATEGORIES.get(category) or FEEDBACK_CATEGORIES[FEEDBACK_CATEGORY_OTHER]
    return metadata["label"]


def _lane_rank(lane: str) -> int:
    return {
        "review_required": 0,
        "candidate_backlog": 1,
        "graph_patch_candidate": 2,
        "status_only": 3,
        "resolved": 4,
    }.get(lane, 5)


def _queue_group_key(item: dict[str, Any], lane: str, *, group_by: str = "target") -> str:
    review_category = _feedback_category(item)
    if group_by == "lane":
        return "|".join([lane, review_category])
    nodes = _source_nodes(item)
    node_key = ",".join(nodes) if nodes else ""
    category = str(
        item.get("reviewed_status_observation_category")
        or item.get("status_observation_category")
        or ""
    )
    if group_by in {"feature", "node", "source_node"} and node_key:
        return "|".join([lane, review_category, node_key, category])
    parts = [
        lane,
        review_category,
        node_key,
        str(item.get("target_type") or ""),
        str(item.get("target_id") or ""),
        str(item.get("issue_type") or ""),
        category,
    ]
    return "|".join(parts)


def _queue_action_hint(lane: str) -> str:
    if lane == "review_required":
        return "review_required_before_action"
    if lane == "candidate_backlog":
        return "review_then_file_backlog"
    if lane == "graph_patch_candidate":
        return "review_then_apply_graph_correction"
    if lane == "status_only":
        return "display_until_user_requests_action"
    return "no_action"


def _active_review_claim(item: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    claim = item.get("review_claim") if isinstance(item.get("review_claim"), dict) else {}
    if not claim:
        return {}
    if str(item.get("status") or "") != STATUS_CLASSIFIED:
        return {}
    expires = _parse_utc(claim.get("lease_expires_at"))
    now = now or datetime.now(timezone.utc)
    if expires is None or expires <= now:
        return {}
    return dict(claim)


def _claim_visible_to_worker(item: dict[str, Any], worker_id: str = "") -> bool:
    claim = _active_review_claim(item)
    if not claim:
        return True
    return bool(worker_id and str(claim.get("worker_id") or "") == worker_id)


def _semantic_review_readiness(
    item: dict[str, Any],
    semantic_state: dict[str, Any],
) -> dict[str, Any]:
    nodes = _source_nodes(item)
    node_semantics = semantic_state.get("node_semantics") if isinstance(semantic_state, dict) else {}
    node_semantics = node_semantics if isinstance(node_semantics, dict) else {}
    if not nodes:
        return {
            "ready": False,
            "reason": "missing_source_node",
            "source_node_ids": [],
            "statuses": {},
        }
    statuses: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    pending: list[str] = []
    stale: list[str] = []
    for node_id in nodes:
        entry = node_semantics.get(node_id) if isinstance(node_semantics.get(node_id), dict) else {}
        status = str(entry.get("status") or "")
        feature_hash = str(entry.get("feature_hash") or "")
        file_hashes = entry.get("file_hashes") if isinstance(entry.get("file_hashes"), dict) else {}
        updated_at = str(entry.get("updated_at") or "")
        statuses[node_id] = {
            "status": status,
            "feature_hash": feature_hash,
            "has_file_hashes": bool(file_hashes),
            "updated_at": updated_at,
        }
        if not entry:
            missing.append(node_id)
        elif status != "ai_complete":
            pending.append(node_id)
        elif not feature_hash:
            stale.append(node_id)
    if missing:
        reason = "missing_semantic_state"
    elif pending:
        reason = "semantic_not_ai_complete"
    elif stale:
        reason = "missing_feature_hash"
    else:
        reason = "current_semantic_ready"
    return {
        "ready": not (missing or pending or stale),
        "reason": reason,
        "source_node_ids": nodes,
        "statuses": statuses,
        "missing_node_ids": missing,
        "pending_node_ids": pending,
        "stale_node_ids": stale,
    }


def _load_semantic_review_state(
    project_id: str,
    snapshot_id: str,
    *,
    conn: Any | None = None,
) -> dict[str, Any]:
    state = _read_json(semantic_graph_state_path(project_id, snapshot_id), {})
    if not isinstance(state, dict):
        state = {}
    node_semantics = state.get("node_semantics")
    if not isinstance(node_semantics, dict):
        node_semantics = {}
    else:
        node_semantics = dict(node_semantics)
    if conn is not None:
        node_semantics = _merge_db_semantic_nodes(
            node_semantics,
            conn=conn,
            project_id=project_id,
            snapshot_id=snapshot_id,
        )
    state["node_semantics"] = node_semantics
    return state


def _merge_db_semantic_nodes(
    node_semantics: dict[str, Any],
    *,
    conn: Any,
    project_id: str,
    snapshot_id: str,
) -> dict[str, Any]:
    merged = dict(node_semantics)
    try:
        rows = conn.execute(
            """
            SELECT node_id, status, feature_hash, file_hashes_json,
                   semantic_json, updated_at
            FROM graph_semantic_nodes
            WHERE project_id = ? AND snapshot_id = ?
            ORDER BY node_id
            """,
            (project_id, snapshot_id),
        ).fetchall()
    except Exception:
        return merged
    for row in rows:
        node_id = str(row["node_id"] or "")
        if not node_id:
            continue
        current = merged.get(node_id) if isinstance(merged.get(node_id), dict) else {}
        try:
            semantic = json.loads(row["semantic_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            semantic = {}
        if not isinstance(semantic, dict):
            semantic = {}
        try:
            file_hashes = json.loads(row["file_hashes_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            file_hashes = {}
        payload = {**current, **semantic}
        payload["status"] = str(row["status"] or payload.get("status") or "")
        payload["feature_hash"] = str(row["feature_hash"] or payload.get("feature_hash") or "")
        payload["file_hashes"] = file_hashes if isinstance(file_hashes, dict) else {}
        payload["updated_at"] = str(row["updated_at"] or payload.get("updated_at") or "")
        merged[node_id] = payload
    return merged


def build_feedback_review_queue(
    project_id: str,
    snapshot_id: str,
    *,
    feedback_kind: str = "",
    status: str = "",
    node_id: str = "",
    source_round: str = "",
    lane: str = "",
    group_by: str = "target",
    include_status_observations: bool = False,
    include_resolved: bool = False,
    include_claimed: bool = True,
    claimable_only: bool = False,
    require_current_semantic: bool = False,
    worker_id: str = "",
    limit: int | None = None,
    conn: Any | None = None,
) -> dict[str, Any]:
    """Return a dashboard-safe, grouped projection over raw feedback items.

    Raw feedback remains append-only in ``reconcile-feedback-state.json``.  This
    view collapses repeated suggestions by node/target/type and hides
    status-only observations by default so semantic expansion can be reviewed in
    human-sized chunks.
    """
    raw_items = list_feedback_items(
        project_id,
        snapshot_id,
        feedback_kind=feedback_kind,
        status=status,
        node_id=node_id,
        limit=None,
    )
    if source_round:
        raw_items = [
            item for item in raw_items
            if str(item.get("source_round") or "") == str(source_round)
        ]
    group_by = str(group_by or "target").strip().lower()
    if group_by not in {"target", "feature", "node", "source_node", "lane"}:
        group_by = "target"

    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_lane_all: dict[str, int] = {}
    by_category_all: dict[str, int] = {}
    hidden_status = 0
    hidden_resolved = 0
    hidden_claimed = 0
    hidden_semantic_pending = 0
    groups: dict[str, dict[str, Any]] = {}
    semantic_state = (
        _load_semantic_review_state(project_id, snapshot_id, conn=conn)
        if require_current_semantic
        else {}
    )

    for item in raw_items:
        kind = str(item.get("feedback_kind") or "")
        item_status = str(item.get("status") or "")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_status[item_status] = by_status.get(item_status, 0) + 1
        item_lane = _feedback_lane(item)
        item_category = _feedback_category(item)
        by_lane_all[item_lane] = by_lane_all.get(item_lane, 0) + 1
        by_category_all[item_category] = by_category_all.get(item_category, 0) + 1
        if lane and item_lane != lane:
            continue
        if item_lane == "status_only" and not include_status_observations:
            hidden_status += 1
            continue
        if item_lane == "resolved" and not include_resolved:
            hidden_resolved += 1
            continue
        active_claim = _active_review_claim(item)
        if active_claim and claimable_only and not _claim_visible_to_worker(item, worker_id):
            hidden_claimed += 1
            continue
        if active_claim and not include_claimed and not _claim_visible_to_worker(item, worker_id):
            hidden_claimed += 1
            continue
        semantic_readiness = (
            _semantic_review_readiness(item, semantic_state)
            if require_current_semantic
            else {"ready": True, "reason": "not_required"}
        )
        if require_current_semantic and not semantic_readiness.get("ready"):
            hidden_semantic_pending += 1
            continue

        key = _queue_group_key(item, item_lane, group_by=group_by)
        group = groups.get(key)
        nodes = _source_nodes(item)
        priority = str(item.get("priority") or "P3").upper()
        target_id = str(item.get("target_id") or "")
        target_type = str(item.get("target_type") or "")
        issue_type = str(item.get("issue_type") or "")
        if group is None:
            group_target_type = "feedback_lane" if group_by == "lane" else target_type
            group_target_id = item_lane if group_by == "lane" else target_id
            group_issue_type = "" if group_by == "lane" else issue_type
            group = {
                "queue_id": f"fq-{_short_hash({'snapshot_id': snapshot_id, 'key': key})}",
                "group_by": group_by,
                "lane": item_lane,
                "category": item_category,
                "category_label": _feedback_category_label(item_category),
                "action_hint": _queue_action_hint(item_lane),
                "priority": priority,
                "source_node_ids": nodes,
                "target_type": group_target_type,
                "target_id": group_target_id,
                "issue_type": group_issue_type,
                "target_ids": [],
                "target_count": 0,
                "target_type_counts": {},
                "issue_type_counts": {},
                "status_observation_category": str(
                    item.get("reviewed_status_observation_category")
                    or item.get("status_observation_category")
                    or ""
                ),
                "representative_feedback_id": str(item.get("feedback_id") or ""),
                "representative_issue": str(item.get("issue") or ""),
                "feedback_ids": [],
                "item_count": 0,
                "suppressed_count": 0,
                "active_claim_count": 0,
                "claim": {},
                "semantic_review_ready": bool(semantic_readiness.get("ready")),
                "semantic_review_gate": semantic_readiness,
                "requires_human_signoff": bool(item.get("requires_human_signoff")),
                "confidence": float(item.get("confidence") or 0.0),
                "created_at": str(item.get("created_at") or ""),
                "updated_at": str(item.get("updated_at") or ""),
            }
            groups[key] = group
        else:
            group["source_node_ids"] = sorted(set(_string_list(group.get("source_node_ids")) + nodes))
            if _priority_rank(priority) < _priority_rank(group.get("priority")):
                group["priority"] = priority
                group["representative_feedback_id"] = str(item.get("feedback_id") or "")
                group["representative_issue"] = str(item.get("issue") or "")
                group["confidence"] = float(item.get("confidence") or 0.0)
        group["feedback_ids"].append(str(item.get("feedback_id") or ""))
        if target_id and target_id not in group["target_ids"]:
            group["target_ids"].append(target_id)
        group["target_count"] = len(group["target_ids"])
        if target_type:
            counts = group["target_type_counts"]
            counts[target_type] = int(counts.get(target_type) or 0) + 1
        if issue_type:
            counts = group["issue_type_counts"]
            counts[issue_type] = int(counts.get(issue_type) or 0) + 1
        group["item_count"] = int(group.get("item_count") or 0) + 1
        group["suppressed_count"] = max(0, int(group["item_count"]) - 1)
        if active_claim:
            group["active_claim_count"] = int(group.get("active_claim_count") or 0) + 1
            group["claim"] = active_claim
        group["semantic_review_ready"] = bool(group.get("semantic_review_ready") and semantic_readiness.get("ready"))
        group["requires_human_signoff"] = bool(group.get("requires_human_signoff") or item.get("requires_human_signoff"))
        group["updated_at"] = max(str(group.get("updated_at") or ""), str(item.get("updated_at") or ""))

    grouped = list(groups.values())
    grouped.sort(
        key=lambda group: (
            _lane_rank(str(group.get("lane") or "")),
            _priority_rank(group.get("priority")),
            -int(group.get("item_count") or 0),
            str(group.get("queue_id") or ""),
        )
    )
    if limit is not None and limit >= 0:
        grouped = grouped[: int(limit)]

    by_lane_visible: dict[str, int] = {}
    by_category_visible: dict[str, int] = {}
    for group in grouped:
        group_lane = str(group.get("lane") or "")
        group_category = str(group.get("category") or FEEDBACK_CATEGORY_OTHER)
        by_lane_visible[group_lane] = by_lane_visible.get(group_lane, 0) + 1
        by_category_visible[group_category] = by_category_visible.get(group_category, 0) + 1

    return {
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "group_by": group_by,
        "summary": {
            "raw_count": len(raw_items),
            "visible_group_count": len(grouped),
            "visible_item_count": sum(int(group.get("item_count") or 0) for group in grouped),
            "hidden_status_observation_count": hidden_status,
            "hidden_resolved_count": hidden_resolved,
            "hidden_claimed_count": hidden_claimed,
            "hidden_semantic_pending_count": hidden_semantic_pending,
            "require_current_semantic": bool(require_current_semantic),
            "by_kind": dict(sorted(by_kind.items())),
            "by_status": dict(sorted(by_status.items())),
            "by_lane_all_items": dict(sorted(by_lane_all.items())),
            "by_lane_visible_groups": dict(sorted(by_lane_visible.items())),
            "by_category_all_items": dict(sorted(by_category_all.items())),
            "by_category_visible_groups": dict(sorted(by_category_visible.items())),
        },
        "groups": grouped,
        "count": len(grouped),
        "group_count": len(grouped),
        "action_catalog": feedback_action_catalog(),
    }


def claim_feedback_review_queue(
    project_id: str,
    snapshot_id: str,
    *,
    worker_id: str,
    feedback_kind: str = "",
    status: str = STATUS_CLASSIFIED,
    node_id: str = "",
    source_round: str = "",
    lane: str = "",
    group_by: str = "feature",
    include_status_observations: bool = False,
    include_resolved: bool = False,
    require_current_semantic: bool = False,
    limit_groups: int = 1,
    max_items: int = 25,
    lease_seconds: int = DEFAULT_REVIEW_LEASE_SECONDS,
    actor: str = "observer",
    conn: Any | None = None,
) -> dict[str, Any]:
    """Claim queue items for a worker using per-item lease metadata."""
    worker_id = str(worker_id or "").strip()
    if not worker_id:
        raise ValueError("worker_id is required")
    lease_seconds = max(30, min(int(lease_seconds or DEFAULT_REVIEW_LEASE_SECONDS), 24 * 60 * 60))
    queue = build_feedback_review_queue(
        project_id,
        snapshot_id,
        feedback_kind=feedback_kind,
        status=status,
        node_id=node_id,
        source_round=source_round,
        lane=lane,
        group_by=group_by,
        include_status_observations=include_status_observations,
        include_resolved=include_resolved,
        include_claimed=False,
        claimable_only=True,
        require_current_semantic=require_current_semantic,
        worker_id=worker_id,
        limit=limit_groups,
        conn=conn,
    )
    feedback_ids: list[str] = []
    selected_groups: list[dict[str, Any]] = []
    for group in queue.get("groups") or []:
        group_ids: list[str] = []
        for feedback_id in group.get("feedback_ids") or []:
            feedback_id = str(feedback_id or "").strip()
            if not feedback_id or feedback_id in feedback_ids:
                continue
            feedback_ids.append(feedback_id)
            group_ids.append(feedback_id)
            if max_items and len(feedback_ids) >= max_items:
                break
        if group_ids:
            selected = dict(group)
            selected["claimed_feedback_ids"] = group_ids
            selected_groups.append(selected)
        if max_items and len(feedback_ids) >= max_items:
            break

    state = load_feedback_state(project_id, snapshot_id)
    existing = state.setdefault("items", {})
    now = _utc_now()
    now_dt = _parse_utc(now) or datetime.now(timezone.utc)
    lease_expires = (
        now_dt.timestamp() + lease_seconds
    )
    lease_expires_at = datetime.fromtimestamp(lease_expires, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    claim_id = f"fqc-{uuid.uuid4().hex[:10]}"
    claimed_items: list[dict[str, Any]] = []
    for feedback_id in feedback_ids:
        item = dict(existing.get(feedback_id) or {})
        if not item or not _claim_visible_to_worker(item, worker_id):
            continue
        item["review_claim"] = {
            "claim_id": claim_id,
            "worker_id": worker_id,
            "claimed_by": actor,
            "claimed_at": now,
            "lease_expires_at": lease_expires_at,
            "lease_seconds": lease_seconds,
        }
        claimed_items.append(item)
    result = _upsert_items(
        project_id,
        snapshot_id,
        claimed_items,
        event_type="feedback.review_claimed",
        actor=actor,
    ) if claimed_items else {
        "ok": True,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "created": 0,
        "updated": 0,
        "count": 0,
        "items": [],
    }
    return {
        "ok": True,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "claim_id": claim_id,
        "worker_id": worker_id,
        "lease_expires_at": lease_expires_at,
        "queue_summary": queue.get("summary", {}),
        "group_count": len(selected_groups),
        "selected_groups": selected_groups,
        "feedback_ids": [item.get("feedback_id") for item in claimed_items],
        "claimed_count": len(claimed_items),
        "claim_result": result,
    }


def _short_hash(payload: Any, length: int = 10) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()[:length]


def _feedback_fingerprint(item: dict[str, Any]) -> str:
    return _short_hash({
        "source_node_ids": sorted(_source_nodes(item)),
        "feedback_kind": str(item.get("feedback_kind") or ""),
        "target_type": str(item.get("target_type") or ""),
        "target_id": str(item.get("target_id") or ""),
        "issue_type": str(item.get("issue_type") or ""),
        "issue": str(item.get("issue") or ""),
        "status_observation_category": str(item.get("status_observation_category") or ""),
    }, 16)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return _json(value)
    except Exception:
        return str(value)


def _string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, (list, tuple, set)):
        return [str(item) for item in raw if str(item or "").strip()]
    return [str(raw)]


def _issue_type(issue: dict[str, Any]) -> str:
    return str(
        issue.get("type")
        or issue.get("kind")
        or issue.get("issue_type")
        or issue.get("category")
        or ""
    ).strip()


def _issue_summary(issue: dict[str, Any]) -> str:
    return str(
        issue.get("summary")
        or issue.get("issue")
        or issue.get("detail")
        or issue.get("message")
        or issue.get("suggested_action")
        or ""
    ).strip()


def _source_nodes(issue: dict[str, Any]) -> list[str]:
    nodes = _string_list(
        issue.get("source_node_ids")
        or issue.get("affected_node_ids")
        or issue.get("node_ids")
        or issue.get("nodes")
    )
    source_node_id = str(issue.get("source_node_id") or "").strip()
    if source_node_id and source_node_id not in nodes:
        nodes.insert(0, source_node_id)
    node_id = str(issue.get("node_id") or "").strip()
    if node_id and node_id not in nodes:
        nodes.insert(0, node_id)
    return nodes


def _health_issue_to_open_issue(issue: dict[str, Any], *, node_id: str = "") -> dict[str, Any]:
    converted = dict(issue)
    converted.setdefault("reason", str(issue.get("source") or "health_issues"))
    converted.setdefault("type", str(issue.get("type") or issue.get("category") or "semantic_health_issue"))
    converted.setdefault("summary", _issue_summary(issue) or str(issue.get("category") or "Semantic health issue."))
    if node_id and not _source_nodes(converted):
        converted["node_id"] = node_id
    return converted


def _entry_review_issues(entry: dict[str, Any], *, node_id: str = "") -> list[dict[str, Any]]:
    open_issues = [
        item for item in (entry.get("open_issues") or [])
        if isinstance(item, dict)
    ]
    if open_issues:
        return open_issues
    return [
        _health_issue_to_open_issue(item, node_id=node_id)
        for item in (entry.get("health_issues") or [])
        if isinstance(item, dict)
    ]


def _round_number(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    if not text:
        return None
    if text.startswith("round-"):
        text = text.split("round-", 1)[1]
    match = re.search(r"\d+", text)
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def _issue_round(issue: dict[str, Any]) -> int | None:
    for key in ("feedback_round", "source_round", "round"):
        number = _round_number(issue.get(key))
        if number is not None:
            return number
    return None


def _select_semantic_state_issues(
    semantic_state: dict[str, Any],
    *,
    source_round: str | int = "",
    node_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    requested_round = _round_number(source_round)
    requested_nodes = {str(node_id).strip() for node_id in (node_ids or []) if str(node_id).strip()}
    node_semantics = semantic_state.get("node_semantics")
    if isinstance(node_semantics, dict) and (requested_round is not None or requested_nodes):
        selected: list[dict[str, Any]] = []
        for node_id, raw_entry in sorted(node_semantics.items()):
            if not isinstance(raw_entry, dict):
                continue
            node_id = str(node_id)
            if requested_nodes and node_id not in requested_nodes:
                continue
            entry_round = _round_number(raw_entry.get("feedback_round"))
            if requested_round is not None and entry_round != requested_round:
                continue
            for raw_issue in _entry_review_issues(raw_entry, node_id=node_id):
                issue = dict(raw_issue)
                if not _source_nodes(issue):
                    issue["node_id"] = node_id
                selected.append(issue)
        return selected

    raw_issues = semantic_state.get("open_issues")
    if not isinstance(raw_issues, list) or not raw_issues:
        raw_issues = [
            _health_issue_to_open_issue(item)
            for item in (semantic_state.get("health_issues") or [])
            if isinstance(item, dict)
        ]
    if not isinstance(raw_issues, list):
        return []
    selected = []
    for raw_issue in raw_issues:
        if not isinstance(raw_issue, dict):
            continue
        issue_nodes = set(_source_nodes(raw_issue))
        if requested_nodes and not issue_nodes.intersection(requested_nodes):
            continue
        issue_round = _issue_round(raw_issue)
        if requested_round is not None and issue_round is not None and issue_round != requested_round:
            continue
        selected.append(raw_issue)
    return selected


def _semantic_state_feedback_rounds(semantic_state: dict[str, Any]) -> list[int]:
    rounds: set[int] = set()
    node_semantics = semantic_state.get("node_semantics")
    if isinstance(node_semantics, dict):
        for raw_entry in node_semantics.values():
            if not isinstance(raw_entry, dict) or not _entry_review_issues(raw_entry):
                continue
            entry_round = _round_number(raw_entry.get("feedback_round"))
            if entry_round is not None:
                rounds.add(entry_round)
    raw_issues = semantic_state.get("open_issues")
    if not isinstance(raw_issues, list) or not raw_issues:
        raw_issues = semantic_state.get("health_issues")
    if isinstance(raw_issues, list):
        for raw_issue in raw_issues:
            if not isinstance(raw_issue, dict):
                continue
            issue_round = _issue_round(raw_issue)
            if issue_round is not None:
                rounds.add(issue_round)
    return sorted(rounds)


def _round_label(round_number: int) -> str:
    return f"round-{round_number:03d}"


def _target_type(issue_type: str, summary: str, reason: str) -> str:
    text = f"{issue_type} {reason} {summary}".lower()
    if "relation" in text or "edge" in text or "dependency" in text:
        return "edge"
    if "doc" in text:
        return "doc"
    if "test" in text:
        return "test"
    if "config" in text or "yaml" in text or "env" in text:
        return "config"
    if "split" in text or "merge" in text or "dead" in text or "delete" in text:
        return "node"
    return "node"


def _confidence(issue_type: str, summary: str) -> float:
    text = f"{issue_type} {summary}".lower()
    if "confidence" in text:
        if "high" in text:
            return 0.8
        if "low" in text:
            return 0.35
    if "already present" in text or "mis-extraction" in text:
        return 0.75
    if "verify" in text or "consider" in text or "likely" in text:
        return 0.45
    return 0.6


def _priority(feedback_kind: str, issue_type: str, summary: str) -> str:
    text = f"{issue_type} {summary}".lower()
    if "p0" in text:
        return "P0"
    if "p1" in text:
        return "P1"
    if feedback_kind == KIND_NEEDS_OBSERVER_DECISION:
        return "P1"
    if "mis-extraction" in text or "false" in text:
        return "P1"
    if "missing_test_binding" in text or "missing_doc_binding" in text:
        return "P2"
    if feedback_kind == KIND_STATUS_OBSERVATION:
        return "P3"
    return "P2" if feedback_kind == KIND_PROJECT_IMPROVEMENT else "P3"


def infer_status_observation_category(item: dict[str, Any]) -> str:
    """Suggest a review category for a status-only graph/file observation."""
    issue_type = str(item.get("issue_type") or item.get("type") or "").lower()
    text = " ".join([
        issue_type,
        str(item.get("issue") or item.get("summary") or ""),
        str((item.get("evidence") or {}).get("reason") or ""),
    ]).lower()
    if "failed_test" in issue_type or "regression" in text or "test failure" in text:
        return STATUS_CATEGORY_PROJECT_REGRESSION
    if "stale_test" in issue_type or "test_expectation" in issue_type:
        return STATUS_CATEGORY_STALE_TEST
    if "doc_drift" in issue_type or "stale_doc" in issue_type:
        return STATUS_CATEGORY_DOC_DRIFT
    if (
        "missing_doc" in text
        or "missing_test" in text
        or "doc_gap" in text
        or "test_gap" in text
        or "config_gap" in text
        or "coverage" in text
    ):
        return STATUS_CATEGORY_COVERAGE_GAP
    if "orphan" in issue_type or "pending_file_decision" in issue_type or "unmapped" in issue_type:
        return STATUS_CATEGORY_ORPHAN_REVIEW
    if "false" in text or "ignore" in text:
        return STATUS_CATEGORY_FALSE_POSITIVE
    return STATUS_CATEGORY_NEEDS_HUMAN


def classify_open_issue(issue: dict[str, Any]) -> str:
    """Route one semantic open issue into a feedback lane.

    The classifier is intentionally conservative: structural split/merge/delete
    suggestions require observer or reviewer confirmation before they become
    graph deltas or project backlog rows.
    """
    issue_type = _issue_type(issue)
    reason = str(issue.get("reason") or "").strip()
    summary = _issue_summary(issue)
    text = f"{issue_type} {reason} {summary}".lower()

    uncertain = (
        "split_suggestions" in reason
        or "merge_suggestions" in reason
        or "dead_code_candidates" in reason
        or re.search(r"\b(consider|verify|confirm|audit|whether|if no|if zero|possible|likely)\b", text)
        or "two separate" in text
        or "source of truth" in text
        or "safe to delete" in text
    )
    if uncertain:
        return KIND_NEEDS_OBSERVER_DECISION

    graph_tokens = {
        "add_relation",
        "add_typed_relation",
        "graph_relation",
        "typed_relation",
        "feature_relation",
        "missing_relation",
        "intra_module_relation",
        "doc_binding",
        "add_doc_binding",
        "doc_binding_addition",
        "doc_link",
        "test_binding",
        "test_binding_realign",
        "test_link",
        "config_binding",
        "config_binding_addition",
        "prune_test_list",
        "remove_secondary_doc_refs",
        "review_typed_relation",
        "reclassify_role",
        "tighten_role",
    }
    if issue_type in graph_tokens or "relation" in text or "edge" in text:
        return KIND_GRAPH_CORRECTION

    if (
        "missing_test_binding" in text
        or "missing_doc_binding" in text
        or "missing_config_binding" in text
        or "test_gap" in text
        or "doc_gap" in text
        or "config_gap" in text
        or "coverage" in text
        or "drift" in text
        or "orphan" in text
        or "pending_decision" in text
        or "low confidence" in text
        or "low-confidence" in text
        or "weak test" in text
        or "weak doc" in text
        or "missing doc" in text
        or "missing test" in text
    ):
        return KIND_STATUS_OBSERVATION

    if (
        "add explicit" in text
        or "document " in text
        or "unit test" in text
        or "implement " in text
        or "refactor " in text
    ):
        return KIND_PROJECT_IMPROVEMENT

    return KIND_GRAPH_CORRECTION


def normalize_open_issue(
    issue: dict[str, Any],
    *,
    project_id: str,
    snapshot_id: str,
    source_round: str | int = "",
    created_by: str = "system",
    feedback_kind: str = "",
) -> dict[str, Any]:
    if not isinstance(issue, dict):
        raise ValueError("open issue must be an object")
    issue_type = _issue_type(issue)
    reason = str(issue.get("reason") or "").strip()
    summary = _issue_summary(issue)
    nodes = _source_nodes(issue)
    kind = feedback_kind or classify_open_issue(issue)
    seed = {
        "snapshot_id": snapshot_id,
        "source_round": str(source_round),
        "nodes": nodes,
        "type": issue_type,
        "reason": reason,
        "summary": summary,
    }
    feedback_id = str(issue.get("feedback_id") or issue.get("id") or f"rf-{_short_hash(seed)}")
    target_id = str(issue.get("target") or issue.get("target_id") or (nodes[0] if nodes else "")).strip()
    now = _utc_now()
    normalized = {
        "feedback_id": feedback_id,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "source_snapshot_id": snapshot_id,
        "source_round": str(source_round),
        "source_node_ids": nodes,
        "feedback_kind": kind,
        "final_feedback_kind": "",
        "status": STATUS_CLASSIFIED,
        "target_type": _target_type(issue_type, summary, reason),
        "target_id": target_id,
        "paths": _string_list(issue.get("paths") or issue.get("path")),
        "issue_type": issue_type,
        "issue": summary,
        "status_observation_category": (
            infer_status_observation_category({
                "issue_type": issue_type,
                "issue": summary,
                "evidence": {"reason": reason},
            })
            if kind == KIND_STATUS_OBSERVATION
            else ""
        ),
        "reviewed_status_observation_category": "",
        "evidence": {
            "reason": reason,
            "raw_issue": issue,
        },
        "suggested_action": summary,
        "confidence": _confidence(issue_type, summary),
        "priority": _priority(kind, issue_type, summary),
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
        "reviewer_decision": "",
        "reviewer_rationale": "",
        "reviewer_model": "",
        "reviewer_confidence": 0.0,
        "requires_human_signoff": kind == KIND_NEEDS_OBSERVER_DECISION,
        "accepted_by": "",
        "accepted_at": "",
        "backlog_bug_id": "",
    }
    normalized["feedback_fingerprint"] = _feedback_fingerprint(normalized)
    return normalized


def submit_feedback_item(
    project_id: str,
    snapshot_id: str,
    *,
    feedback_kind: str,
    issue: dict[str, Any],
    actor: str = "dashboard_user",
    source_round: str | int = "user",
) -> dict[str, Any]:
    """Create one dashboard/operator feedback item using the review-lane vocabulary."""
    kind = str(feedback_kind or "").strip() or classify_open_issue(issue)
    if kind not in REVIEW_DECISIONS and kind != KIND_NEEDS_OBSERVER_DECISION:
        raise ValueError(f"invalid feedback_kind: {feedback_kind}")
    item = normalize_open_issue(
        issue,
        project_id=project_id,
        snapshot_id=snapshot_id,
        source_round=source_round,
        created_by=actor,
        feedback_kind=kind,
    )
    if issue.get("priority"):
        item["priority"] = str(issue.get("priority") or item.get("priority") or "P2").upper()
    if issue.get("confidence") is not None:
        item["confidence"] = float(issue.get("confidence") or 0.0)
    if issue.get("requires_human_signoff") is not None:
        item["requires_human_signoff"] = bool(issue.get("requires_human_signoff"))
    return _upsert_items(
        project_id,
        snapshot_id,
        [item],
        event_type="feedback.user_submitted",
        actor=actor,
    )


def _upsert_items(
    project_id: str,
    snapshot_id: str,
    items: list[dict[str, Any]],
    *,
    event_type: str,
    actor: str,
) -> dict[str, Any]:
    with _feedback_state_update_lock(project_id, snapshot_id):
        state = load_feedback_state(project_id, snapshot_id)
        existing = state.setdefault("items", {})
        now = _utc_now()
        events: list[dict[str, Any]] = []
        created = 0
        updated = 0
        for item in items:
            fid = str(item.get("feedback_id") or "")
            if not fid:
                fid = f"rf-{uuid.uuid4().hex[:10]}"
                item["feedback_id"] = fid
            previous = existing.get(fid)
            merged = {**(previous or {}), **item, "updated_at": now}
            if (
                event_type == "feedback.classified"
                and previous
                and previous.get("status") not in {"", STATUS_CLASSIFIED}
            ):
                merged["status"] = previous.get("status")
            existing[fid] = merged
            if previous:
                updated += 1
            else:
                created += 1
            events.append({
                "event_id": f"rfe-{uuid.uuid4().hex[:10]}",
                "event_type": event_type,
                "feedback_id": fid,
                "actor": actor,
                "created_at": now,
                "item": merged,
            })
        save_feedback_state(project_id, snapshot_id, state)
        _append_jsonl(feedback_events_path(project_id, snapshot_id), events)
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "created": created,
            "updated": updated,
            "count": len(items),
            "state_path": str(feedback_state_path(project_id, snapshot_id)),
            "events_path": str(feedback_events_path(project_id, snapshot_id)),
            "items": [existing[str(item["feedback_id"])] for item in items],
        }


def carry_forward_feedback_review_state(
    project_id: str,
    snapshot_id: str,
    base_snapshot_id: str,
    *,
    actor: str = "system",
    feedback_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Carry reviewer decisions from a base snapshot when feedback fingerprints match."""
    if not base_snapshot_id or base_snapshot_id == snapshot_id:
        return {"ok": True, "carried_forward_count": 0, "base_snapshot_id": base_snapshot_id}
    state = load_feedback_state(project_id, snapshot_id)
    base_state = load_feedback_state(project_id, base_snapshot_id)
    current_items = state.setdefault("items", {})
    base_items = (base_state.get("items") or {})
    base_by_fingerprint: dict[str, dict[str, Any]] = {}
    for base_item in base_items.values():
        if not isinstance(base_item, dict):
            continue
        fingerprint = str(base_item.get("feedback_fingerprint") or _feedback_fingerprint(base_item))
        if not fingerprint:
            continue
        if (
            str(base_item.get("status") or "") != STATUS_CLASSIFIED
            or str(base_item.get("reviewer_decision") or "")
            or str(base_item.get("backlog_bug_id") or "")
        ):
            base_by_fingerprint.setdefault(fingerprint, dict(base_item))

    requested = set(str(fid) for fid in (feedback_ids or []) if str(fid or ""))
    now = _utc_now()
    carried: list[dict[str, Any]] = []
    for feedback_id, item in list(current_items.items()):
        if requested and feedback_id not in requested:
            continue
        if str(item.get("status") or "") != STATUS_CLASSIFIED:
            continue
        fingerprint = str(item.get("feedback_fingerprint") or _feedback_fingerprint(item))
        base_item = base_by_fingerprint.get(fingerprint)
        if not base_item:
            continue
        patched = dict(item)
        for key in _REVIEW_CARRY_FORWARD_KEYS:
            if key in base_item:
                patched[key] = base_item[key]
        patched["feedback_fingerprint"] = fingerprint
        patched["carried_from_snapshot_id"] = base_snapshot_id
        patched["carried_from_feedback_id"] = base_item.get("feedback_id", "")
        patched["carried_forward_at"] = now
        carried.append(patched)

    if not carried:
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "base_snapshot_id": base_snapshot_id,
            "carried_forward_count": 0,
        }
    result = _upsert_items(
        project_id,
        snapshot_id,
        carried,
        event_type="feedback.review_carry_forward",
        actor=actor,
    )
    return {
        **result,
        "base_snapshot_id": base_snapshot_id,
        "carried_forward_count": len(carried),
    }


def classify_semantic_open_issues(
    project_id: str,
    snapshot_id: str,
    *,
    source_round: str | int = "",
    created_by: str = "system",
    issues: list[dict[str, Any]] | None = None,
    feedback_kind: str = "",
    limit: int | None = None,
    node_ids: list[str] | None = None,
    base_snapshot_id: str = "",
) -> dict[str, Any]:
    raw_issues = issues
    if raw_issues is None:
        semantic_state = _read_json(semantic_graph_state_path(project_id, snapshot_id), {})
        raw_issues = (
            _select_semantic_state_issues(
                semantic_state,
                source_round=source_round,
                node_ids=node_ids,
            )
            if isinstance(semantic_state, dict)
            else []
        )
    if not isinstance(raw_issues, list):
        raw_issues = []
    selected = [item for item in raw_issues if isinstance(item, dict)]
    if node_ids:
        requested_nodes = {str(node_id).strip() for node_id in node_ids if str(node_id).strip()}
        selected = [
            item for item in selected
            if not requested_nodes or set(_source_nodes(item)).intersection(requested_nodes)
        ]
    if limit is not None and limit >= 0:
        selected = selected[: int(limit)]
    items = [
        normalize_open_issue(
            issue,
            project_id=project_id,
            snapshot_id=snapshot_id,
            source_round=source_round,
            created_by=created_by,
            feedback_kind=feedback_kind,
        )
        for issue in selected
    ]
    result = _upsert_items(
        project_id,
        snapshot_id,
        items,
        event_type="feedback.classified",
        actor=created_by,
    )
    if base_snapshot_id:
        carry = carry_forward_feedback_review_state(
            project_id,
            snapshot_id,
            base_snapshot_id,
            actor=created_by,
            feedback_ids=[str(item.get("feedback_id") or "") for item in items],
        )
        result["carry_forward"] = {
            "base_snapshot_id": base_snapshot_id,
            "carried_forward_count": carry.get("carried_forward_count", 0),
        }
        if carry.get("carried_forward_count"):
            result["items"] = [
                (load_feedback_state(project_id, snapshot_id).get("items") or {}).get(
                    str(item.get("feedback_id") or ""),
                    item,
                )
                for item in items
            ]
    result["summary"] = feedback_summary(project_id, snapshot_id)
    return result


def classify_semantic_state_rounds(
    project_id: str,
    snapshot_id: str,
    *,
    created_by: str = "system",
    source_rounds: list[str | int] | None = None,
    limit_per_round: int | None = None,
    base_snapshot_id: str = "",
) -> dict[str, Any]:
    """Classify all round-scoped semantic graph-state issues for a snapshot."""
    semantic_state = _read_json(semantic_graph_state_path(project_id, snapshot_id), {})
    if not isinstance(semantic_state, dict):
        semantic_state = {}
    rounds = [_round_number(raw_round) for raw_round in (source_rounds or [])]
    if not source_rounds:
        rounds = _semantic_state_feedback_rounds(semantic_state)
    normalized_rounds = sorted({round_number for round_number in rounds if round_number is not None})

    results: list[dict[str, Any]] = []
    created = 0
    updated = 0
    total = 0
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for round_number in normalized_rounds:
        round_label = _round_label(round_number)
        result = classify_semantic_open_issues(
            project_id,
            snapshot_id,
            source_round=round_label,
            created_by=created_by,
            limit=limit_per_round,
            base_snapshot_id=base_snapshot_id,
        )
        results.append({
            "source_round": round_label,
            "created": result.get("created", 0),
            "updated": result.get("updated", 0),
            "count": result.get("count", 0),
            "summary": result.get("summary", {}),
        })
        created += int(result.get("created") or 0)
        updated += int(result.get("updated") or 0)
        total += int(result.get("count") or 0)
        for item in result.get("items") or []:
            kind = str(item.get("feedback_kind") or "")
            status = str(item.get("status") or "")
            by_kind[kind] = by_kind.get(kind, 0) + 1
            by_status[status] = by_status.get(status, 0) + 1

    return {
        "ok": True,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "rounds": [_round_label(round_number) for round_number in normalized_rounds],
        "round_count": len(normalized_rounds),
        "created": created,
        "updated": updated,
        "count": total,
        "summary": {
            "count": total,
            "by_kind": dict(sorted(by_kind.items())),
            "by_status": dict(sorted(by_status.items())),
        },
        "carry_forward": {
            "base_snapshot_id": base_snapshot_id,
            "carried_forward_count": sum(
                int((result.get("carry_forward") or {}).get("carried_forward_count") or 0)
                for result in results
            ),
        },
        "results": results,
        "state_path": str(feedback_state_path(project_id, snapshot_id)),
        "events_path": str(feedback_events_path(project_id, snapshot_id)),
    }


def feedback_summary(project_id: str, snapshot_id: str) -> dict[str, Any]:
    items = list_feedback_items(project_id, snapshot_id)
    by_kind: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_status_category: dict[str, int] = {}
    for item in items:
        by_kind[str(item.get("feedback_kind") or "")] = by_kind.get(str(item.get("feedback_kind") or ""), 0) + 1
        by_status[str(item.get("status") or "")] = by_status.get(str(item.get("status") or ""), 0) + 1
        if item.get("feedback_kind") == KIND_STATUS_OBSERVATION:
            category = str(
                item.get("reviewed_status_observation_category")
                or item.get("status_observation_category")
                or ""
            )
            if category:
                by_status_category[category] = by_status_category.get(category, 0) + 1
    return {
        "count": len(items),
        "by_kind": by_kind,
        "by_status": by_status,
        "by_status_observation_category": by_status_category,
    }


def _parse_ai_review(ai_result: dict[str, Any]) -> dict[str, Any]:
    decision = str(
        ai_result.get("decision")
        or ai_result.get("reviewer_decision")
        or ai_result.get("final_feedback_kind")
        or ""
    ).strip()
    if decision not in REVIEW_DECISIONS:
        decision = "needs_human_signoff"
    category = str(
        ai_result.get("status_observation_category")
        or ai_result.get("observation_category")
        or ai_result.get("category")
        or ""
    ).strip()
    if category and category not in STATUS_OBSERVATION_CATEGORIES:
        category = STATUS_CATEGORY_NEEDS_HUMAN
    return {
        "decision": decision,
        "status_observation_category": category,
        "rationale": str(ai_result.get("rationale") or ai_result.get("reviewer_rationale") or ""),
        "confidence": float(ai_result.get("confidence") or ai_result.get("reviewer_confidence") or 0.0),
        "model": _as_text(ai_result.get("_ai_route") or ai_result.get("model") or ""),
        "raw": ai_result,
    }


def _reviewer_instructions() -> dict[str, Any]:
    return {
        "reviewer": "reconcile_feedback_reviewer",
        "mutate_project_files": False,
        "allowed_decisions": sorted(REVIEW_DECISIONS),
        "status_observation_categories": sorted(STATUS_OBSERVATION_CATEGORIES),
        "decision_meaning": {
            KIND_GRAPH_CORRECTION: "Only graph/semantic state should change.",
            KIND_PROJECT_IMPROVEMENT: "Project code/docs/tests likely need a backlog item.",
            KIND_STATUS_OBSERVATION: "Keep this as visible graph/file status until a user chooses an action.",
            KIND_FALSE_POSITIVE: "Close the feedback without action.",
            "needs_human_signoff": "Evidence is insufficient; user or observer must decide.",
        },
        "status_observation_category_meaning": {
            STATUS_CATEGORY_STALE_TEST: "A test likely asserts an old contract and may need update after user approval.",
            STATUS_CATEGORY_DOC_DRIFT: "A linked document may be stale relative to changed graph/code state.",
            STATUS_CATEGORY_COVERAGE_GAP: "A node/file is missing doc/test/config coverage or graph attachment.",
            STATUS_CATEGORY_PROJECT_REGRESSION: "The observation may indicate a product/code behavior regression.",
            STATUS_CATEGORY_ORPHAN_REVIEW: "An orphan or pending file needs keep/attach/delete review.",
            STATUS_CATEGORY_FALSE_POSITIVE: "The observation should be closed without action.",
            STATUS_CATEGORY_NEEDS_HUMAN: "Evidence is insufficient for automatic routing.",
        },
        "output_contract": (
            "Return JSON with decision, optional status_observation_category, "
            "rationale, confidence, and model. Do not mutate project files."
        ),
    }


def _normalize_reviewed_item(
    item: dict[str, Any],
    *,
    decision: str,
    rationale: str,
    confidence: float | None,
    status_observation_category: str,
    actor: str,
    accept: bool,
    ai_review: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ai_review = ai_review or {}
    if not decision:
        if item.get("feedback_kind") == KIND_NEEDS_OBSERVER_DECISION:
            # MF-2026-05-10-016 follow-up: when the caller explicitly says
            # accept=True (e.g. dashboard accept_semantic_enrichment click),
            # treat that as the human signoff instead of routing back to
            # needs_human_signoff. The operator IS the human, the explicit
            # accept IS the signoff. Without this override the row stays
            # pending and the dashboard looks like "Accept did nothing".
            if accept:
                decision = KIND_PROJECT_IMPROVEMENT
            else:
                decision = "needs_human_signoff"
        else:
            decision = item.get("feedback_kind") or "needs_human_signoff"
    if decision not in REVIEW_DECISIONS:
        raise ValueError(f"invalid reviewer decision: {decision}")
    if status_observation_category and status_observation_category not in STATUS_OBSERVATION_CATEGORIES:
        raise ValueError(f"invalid status_observation_category: {status_observation_category}")
    if decision == KIND_FALSE_POSITIVE:
        status_observation_category = STATUS_CATEGORY_FALSE_POSITIVE
    if item.get("feedback_kind") == KIND_STATUS_OBSERVATION and not status_observation_category:
        status_observation_category = infer_status_observation_category(item)
    now = _utc_now()
    reviewed = dict(item)
    reviewed.update({
        "reviewer_decision": decision,
        "reviewed_status_observation_category": status_observation_category,
        "reviewer_rationale": rationale,
        "reviewer_confidence": float(confidence if confidence is not None else item.get("confidence") or 0.0),
        "reviewer_model": ai_review.get("model") or item.get("reviewer_model") or "",
        "reviewed_by": actor,
        "reviewed_at": now,
        "updated_at": now,
    })
    if decision == KIND_FALSE_POSITIVE:
        reviewed["status"] = STATUS_REJECTED
        reviewed["final_feedback_kind"] = KIND_FALSE_POSITIVE
        reviewed["requires_human_signoff"] = False
    elif decision == "needs_human_signoff":
        reviewed["status"] = STATUS_NEEDS_HUMAN_SIGNOFF
        reviewed["requires_human_signoff"] = True
    else:
        reviewed["final_feedback_kind"] = decision
        reviewed["requires_human_signoff"] = False
        reviewed["status"] = STATUS_ACCEPTED if accept else STATUS_REVIEWED
        if accept:
            reviewed["accepted_by"] = actor
            reviewed["accepted_at"] = now
    claim = reviewed.get("review_claim") if isinstance(reviewed.get("review_claim"), dict) else {}
    if claim:
        claim["completed_at"] = now
        claim["completed_by"] = actor
        reviewed["review_claim"] = claim
    return reviewed


def _parse_ai_review_batch(
    ai_result: dict[str, Any],
    feedback_ids: list[str],
) -> dict[str, dict[str, Any]]:
    raw_items = (
        ai_result.get("items")
        or ai_result.get("reviews")
        or ai_result.get("decisions")
        or []
    )
    if isinstance(raw_items, dict):
        raw_items = [
            {**(value if isinstance(value, dict) else {}), "feedback_id": key}
            for key, value in raw_items.items()
        ]
    if not isinstance(raw_items, list):
        raw_items = []

    reviews: dict[str, dict[str, Any]] = {}
    route = ai_result.get("_ai_route") or ai_result.get("model") or ""
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            continue
        feedback_id = str(
            raw.get("feedback_id")
            or raw.get("id")
            or raw.get("feedbackId")
            or (feedback_ids[index] if index < len(feedback_ids) else "")
        ).strip()
        if not feedback_id:
            continue
        parsed_raw = dict(raw)
        if "_ai_route" not in parsed_raw and route:
            parsed_raw["_ai_route"] = route
        reviews[feedback_id] = _parse_ai_review(parsed_raw)
    return reviews


def review_feedback_item(
    project_id: str,
    snapshot_id: str,
    feedback_id: str,
    *,
    decision: str = "",
    rationale: str = "",
    confidence: float | None = None,
    status_observation_category: str = "",
    actor: str = "observer",
    accept: bool = False,
    ai_call: ReviewerAiCall | None = None,
    project_root: str | Path | None = None,
    max_context_chars: int = 6000,
    enable_read_tools: bool = True,
    grep_patterns: list[str] | None = None,
) -> dict[str, Any]:
    state = load_feedback_state(project_id, snapshot_id)
    item = dict((state.get("items") or {}).get(feedback_id) or {})
    if not item:
        raise KeyError(f"feedback item not found: {feedback_id}")

    ai_review: dict[str, Any] = {}
    if not decision and ai_call is not None:
        payload = {
            "instructions": _reviewer_instructions(),
            "feedback": item,
            "review_context": _build_review_context(
                project_id,
                snapshot_id,
                item,
                project_root=project_root,
                max_excerpt_chars=max_context_chars,
                enable_read_tools=enable_read_tools,
                grep_patterns=grep_patterns,
            ),
        }
        ai_review = _parse_ai_review(ai_call("reconcile_feedback_review", payload) or {})
        decision = ai_review["decision"]
        status_observation_category = (
            status_observation_category
            or ai_review.get("status_observation_category", "")
        )
        rationale = rationale or ai_review["rationale"]
        confidence = confidence if confidence is not None else ai_review["confidence"]

    item = _normalize_reviewed_item(
        item,
        decision=decision,
        rationale=rationale,
        confidence=confidence,
        status_observation_category=status_observation_category,
        actor=actor,
        accept=accept,
        ai_review=ai_review,
    )

    return _upsert_items(
        project_id,
        snapshot_id,
        [item],
        event_type="feedback.reviewed",
        actor=actor,
    )


def review_feedback_items_batch(
    project_id: str,
    snapshot_id: str,
    feedback_ids: list[str],
    *,
    ai_call: ReviewerAiCall,
    project_root: str | Path | None = None,
    max_context_chars: int = 6000,
    enable_read_tools: bool = True,
    grep_patterns: list[str] | None = None,
    actor: str = "observer",
    accept: bool = False,
) -> dict[str, Any]:
    """Review several feedback items with one AI call and one state update."""
    ids = [str(item or "").strip() for item in feedback_ids if str(item or "").strip()]
    if not ids:
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "count": 0,
            "items": [],
            "errors": [],
            "error_count": 0,
        }
    state = load_feedback_state(project_id, snapshot_id)
    existing = state.get("items") or {}
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for feedback_id in ids:
        item = dict(existing.get(feedback_id) or {})
        if not item:
            errors.append({"feedback_id": feedback_id, "error": "feedback item not found"})
            continue
        items.append(item)
    if not items:
        return {
            "ok": False,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "count": 0,
            "items": [],
            "errors": errors,
            "error_count": len(errors),
        }

    payload = {
        "instructions": {
            **_reviewer_instructions(),
            "output_contract": (
                "Return JSON with an items array. Each item must include feedback_id, "
                "decision, optional status_observation_category, rationale, confidence, and model. "
                "Do not mutate project files."
            ),
        },
        "feedback_items": items,
        "review_contexts": {
            str(item.get("feedback_id") or ""): _build_review_context(
                project_id,
                snapshot_id,
                item,
                project_root=project_root,
                max_excerpt_chars=max_context_chars,
                enable_read_tools=enable_read_tools,
                grep_patterns=grep_patterns,
            )
            for item in items
        },
    }
    ai_result = ai_call("reconcile_feedback_review_batch", payload) or {}
    ai_reviews = _parse_ai_review_batch(ai_result, [str(item.get("feedback_id") or "") for item in items])

    reviewed_items: list[dict[str, Any]] = []
    for item in items:
        feedback_id = str(item.get("feedback_id") or "")
        ai_review = ai_reviews.get(feedback_id)
        if not ai_review:
            ai_review = {
                "decision": "needs_human_signoff",
                "status_observation_category": STATUS_CATEGORY_NEEDS_HUMAN,
                "rationale": "Batch reviewer did not return a decision for this feedback item.",
                "confidence": 0.0,
                "model": _as_text(ai_result.get("_ai_route") or ai_result.get("model") or ""),
                "raw": ai_result,
            }
        try:
            reviewed_items.append(
                _normalize_reviewed_item(
                    item,
                    decision=str(ai_review.get("decision") or ""),
                    rationale=str(ai_review.get("rationale") or ""),
                    confidence=(
                        float(ai_review["confidence"])
                        if ai_review.get("confidence") is not None
                        else None
                    ),
                    status_observation_category=str(ai_review.get("status_observation_category") or ""),
                    actor=actor,
                    accept=accept,
                    ai_review=ai_review,
                )
            )
        except Exception as exc:
            errors.append({"feedback_id": feedback_id, "error": str(exc)})

    result = _upsert_items(
        project_id,
        snapshot_id,
        reviewed_items,
        event_type="feedback.reviewed",
        actor=actor,
    )
    result["errors"] = errors
    result["error_count"] = len(errors)
    result["ok"] = not errors
    return result


def build_project_improvement_backlog(
    project_id: str,
    snapshot_id: str,
    feedback_id: str,
    *,
    bug_id: str = "",
    actor: str = "observer",
    allow_status_observation: bool = False,
) -> dict[str, Any]:
    state = load_feedback_state(project_id, snapshot_id)
    item = dict((state.get("items") or {}).get(feedback_id) or {})
    if not item:
        raise KeyError(f"feedback item not found: {feedback_id}")
    final_kind = item.get("final_feedback_kind") or item.get("feedback_kind")
    if final_kind != KIND_PROJECT_IMPROVEMENT:
        if not (allow_status_observation and final_kind == KIND_STATUS_OBSERVATION):
            raise ValueError(f"feedback item is not project_improvement: {feedback_id}")
    nodes = item.get("source_node_ids") or []
    suffix = _short_hash({"snapshot_id": snapshot_id, "feedback_id": feedback_id}, 8)
    bug = bug_id or f"OPT-BACKLOG-FEEDBACK-{snapshot_id[:12]}-{suffix}"
    paths = item.get("paths") or []
    title_node = f" {nodes[0]}" if nodes else ""
    payload = {
        "actor": actor,
        "title": (
            f"Project improvement from reconcile feedback{title_node}"
            if final_kind == KIND_PROJECT_IMPROVEMENT
            else f"User-requested backlog from reconcile status{title_node}"
        ),
        "status": "OPEN",
        "priority": item.get("priority") or "P2",
        "target_files": paths,
        "test_files": [],
        "acceptance_criteria": [
            "Backlog row records source semantic feedback and snapshot provenance.",
            "Implementation updates project files only through chain or authorized MF.",
            "Scope reconcile updates graph state after the project change lands.",
        ],
        "details_md": (
            "Filed from reconcile feedback.\n\n"
            f"snapshot_id: {snapshot_id}\n"
            f"feedback_id: {feedback_id}\n"
            f"nodes: {', '.join(nodes)}\n"
            f"issue: {item.get('issue') or ''}\n\n"
            f"reviewer_decision: {item.get('reviewer_decision') or item.get('feedback_kind')}\n"
            f"status_observation_category: {item.get('reviewed_status_observation_category') or item.get('status_observation_category') or ''}\n"
            f"reviewer_rationale: {item.get('reviewer_rationale') or ''}\n"
        ),
        "provenance_paths": [
            str(feedback_state_path(project_id, snapshot_id)).replace("\\", "/"),
            f"graph_snapshot:{snapshot_id}",
            f"reconcile_feedback:{feedback_id}",
        ],
        "chain_trigger_json": {
            "source": "reconcile_feedback",
            "snapshot_id": snapshot_id,
            "feedback_id": feedback_id,
            "feedback_kind": final_kind,
            "status_observation_category": (
                item.get("reviewed_status_observation_category")
                or item.get("status_observation_category")
                or ""
            ),
            "source_node_ids": nodes,
        },
        "force_admit": True,
    }
    return {"bug_id": bug, "payload": payload, "feedback": item}


def mark_feedback_backlog_filed(
    project_id: str,
    snapshot_id: str,
    feedback_id: str,
    *,
    bug_id: str,
    actor: str = "observer",
) -> dict[str, Any]:
    state = load_feedback_state(project_id, snapshot_id)
    item = dict((state.get("items") or {}).get(feedback_id) or {})
    if not item:
        raise KeyError(f"feedback item not found: {feedback_id}")
    item["status"] = STATUS_BACKLOG_FILED
    item["backlog_bug_id"] = bug_id
    item["updated_at"] = _utc_now()
    return _upsert_items(
        project_id,
        snapshot_id,
        [item],
        event_type="feedback.backlog_filed",
        actor=actor,
    )


def _clean_patch_id(seed: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(seed or "").strip()).strip("-")
    return value[:80] or uuid.uuid4().hex[:12]


def _raw_issue(item: dict[str, Any]) -> dict[str, Any]:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    raw = evidence.get("raw_issue")
    return dict(raw) if isinstance(raw, dict) else {}


def _feedback_patch_type(item: dict[str, Any]) -> str:
    raw = _raw_issue(item)
    reason = str((item.get("evidence") or {}).get("reason") or raw.get("reason") or "").lower()
    issue_type = str(item.get("issue_type") or raw.get("type") or raw.get("kind") or "").lower()
    text = " ".join([reason, issue_type, str(item.get("issue") or "").lower()])
    if "merge_suggestions" in reason or "merge" in issue_type:
        return "merge_nodes"
    if "split_suggestions" in reason or "split" in issue_type:
        return "split_node"
    if "dead_code_candidates" in reason or "orphan" in issue_type or "dead" in issue_type:
        return "mark_orphan"
    if "doc" in text and "binding" in text:
        return "move_doc_binding"
    if "test" in text and "binding" in text:
        return "move_test_binding"
    if any(token in text for token in ("remove", "prune", "delete edge", "drop edge")):
        return "remove_edge"
    if "role" in text and ("package" in text or "marker" in text):
        return "mark_package_marker"
    return "add_edge"


def _first_node(item: dict[str, Any]) -> str:
    nodes = _source_nodes(item)
    return nodes[0] if nodes else str(item.get("target_id") or "").strip()


def _target_from_feedback(item: dict[str, Any], raw: dict[str, Any]) -> str:
    return str(
        raw.get("destination_node_id")
        or raw.get("target_node_id")
        or raw.get("target")
        or raw.get("dst")
        or raw.get("to")
        or item.get("target_id")
        or ""
    ).strip()


def _explicit_target_from_feedback(raw: dict[str, Any]) -> str:
    return str(
        raw.get("destination_node_id")
        or raw.get("target_node_id")
        or raw.get("target")
        or raw.get("dst")
        or raw.get("to")
        or ""
    ).strip()


def _feedback_patch_payload(item: dict[str, Any], patch_type: str) -> dict[str, Any]:
    raw = _raw_issue(item)
    source = str(raw.get("source_node_id") or raw.get("src") or raw.get("from") or _first_node(item)).strip()
    explicit_target = _explicit_target_from_feedback(raw)
    target = explicit_target or _target_from_feedback(item, raw)
    base = {
        "feedback_id": item.get("feedback_id") or "",
        "feedback_fingerprint": item.get("feedback_fingerprint") or "",
        "source_snapshot_id": item.get("snapshot_id") or item.get("source_snapshot_id") or "",
        "source_node_ids": _source_nodes(item),
        "raw_issue": raw,
        "summary": item.get("issue") or "",
    }
    if patch_type in {"add_edge", "remove_edge"}:
        target = explicit_target
        if not target:
            raise ValueError("edge graph correction requires an explicit target node or alias")
        edge_type = str(
            raw.get("edge_type")
            or raw.get("relation_type")
            or raw.get("type")
            or raw.get("kind")
            or "depends_on"
        ).strip()
        if edge_type in {"", "add_relation", "missing_relation", "typed_relation"}:
            edge_type = "depends_on"
        return {
            **base,
            "edge": {
                "src": source,
                "dst": target,
                "edge_type": edge_type,
                "direction": str(raw.get("direction") or "dependency"),
                "evidence": {
                    "source": "semantic_feedback",
                    "feedback_id": item.get("feedback_id") or "",
                    "reason": (item.get("evidence") or {}).get("reason") or "",
                },
            },
        }
    if patch_type in {"move_doc_binding", "move_test_binding"}:
        return {
            **base,
            "target_node_id": source,
            "destination_node_id": target,
            "files": _string_list(raw.get("files") or raw.get("paths") or item.get("paths")),
        }
    if patch_type == "merge_nodes":
        nodes = _string_list(raw.get("source_node_ids") or raw.get("nodes") or item.get("source_node_ids"))
        if target and target not in nodes:
            nodes.append(target)
        return {
            **base,
            "target_node_id": target or source,
            "source_node_ids": nodes,
            "semantic_policy": "merge_semantic_candidates_review_required",
            "feedback_policy": "move_open_feedback_to_target",
            "doc_test_policy": "merge_bindings_review_required",
        }
    if patch_type == "split_node":
        return {
            **base,
            "target_node_id": source,
            "proposed_nodes": raw.get("proposed_nodes") or raw.get("splits") or raw.get("targets") or [],
            "semantic_policy": "copy_provenance_only",
            "feedback_policy": "copy_open_feedback_to_split_candidates",
            "doc_test_policy": "recalculate_coverage",
        }
    if patch_type == "mark_package_marker":
        return {
            **base,
            "target_node_id": source,
            "file_role": "package_marker",
            "confidence": item.get("confidence") or 0.0,
            "semantic_policy": "drop_leaf_semantic_keep_evidence",
            "feedback_policy": "move_open_feedback_to_parent",
            "doc_test_policy": "recalculate_coverage",
        }
    return {
        **base,
        "target_node_id": source,
        "file_role": "orphan_candidate",
        "semantic_policy": "keep_evidence_mark_stale",
        "feedback_policy": "keep_on_node",
        "doc_test_policy": "recalculate_coverage",
    }


def _load_node_aliases(conn, project_id: str, snapshot_id: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    try:
        rows = conn.execute(
            """
            SELECT node_id, title, metadata_json
            FROM graph_nodes_index
            WHERE project_id = ? AND snapshot_id = ?
            """,
            (project_id, snapshot_id),
        ).fetchall()
    except Exception:
        return aliases
    for row in rows:
        node_id = str(row["node_id"] if hasattr(row, "keys") else row[0]).strip()
        title = str(row["title"] if hasattr(row, "keys") else row[1]).strip()
        raw_metadata = row["metadata_json"] if hasattr(row, "keys") else row[2]
        metadata = {}
        if isinstance(raw_metadata, str) and raw_metadata.strip():
            try:
                metadata = json.loads(raw_metadata)
            except json.JSONDecodeError:
                metadata = {}
        if node_id:
            aliases[node_id.lower()] = node_id
        if title:
            aliases[title.lower()] = node_id
        module = str(metadata.get("module") or "").strip() if isinstance(metadata, dict) else ""
        if module:
            aliases[module.lower()] = node_id
    return aliases


def _resolve_node_alias(value: Any, aliases: dict[str, str]) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return aliases.get(text.lower(), text)


def _resolve_patch_payload_aliases(patch: dict[str, Any], aliases: dict[str, str]) -> dict[str, Any]:
    if not aliases:
        return patch
    out = dict(patch)
    payload = dict(out.get("patch_json") or {})
    out["target_node_id"] = _resolve_node_alias(out.get("target_node_id"), aliases)
    if payload.get("target_node_id"):
        payload["target_node_id"] = _resolve_node_alias(payload.get("target_node_id"), aliases)
    if payload.get("destination_node_id"):
        payload["destination_node_id"] = _resolve_node_alias(payload.get("destination_node_id"), aliases)
    if isinstance(payload.get("source_node_ids"), list):
        payload["source_node_ids"] = [
            _resolve_node_alias(node_id, aliases)
            for node_id in payload["source_node_ids"]
        ]
    edge = payload.get("edge")
    if isinstance(edge, dict):
        edge = dict(edge)
        edge["src"] = _resolve_node_alias(edge.get("src"), aliases)
        edge["dst"] = _resolve_node_alias(edge.get("dst"), aliases)
        payload["edge"] = edge
    out["patch_json"] = payload
    return out


def _validate_graph_patch_payload(patch: dict[str, Any]) -> None:
    patch_type = str(patch.get("patch_type") or "")
    payload = patch.get("patch_json") or {}
    if patch_type in {"add_edge", "remove_edge"}:
        edge = payload.get("edge") if isinstance(payload, dict) else None
        if not isinstance(edge, dict):
            raise ValueError("edge graph correction is missing edge payload")
        src = str(edge.get("src") or "").strip()
        dst = str(edge.get("dst") or "").strip()
        if not src or not dst:
            raise ValueError("edge graph correction requires non-empty source and destination")
        if src == dst:
            raise ValueError("edge graph correction cannot create a self edge")


def build_graph_correction_patch_from_feedback(
    item: dict[str, Any],
    *,
    base_commit: str = "",
) -> dict[str, Any]:
    """Convert a reviewed semantic feedback item into a patch proposal."""
    if not isinstance(item, dict) or not item.get("feedback_id"):
        raise ValueError("feedback item is required")
    kind = str(item.get("final_feedback_kind") or item.get("feedback_kind") or "")
    if kind != KIND_GRAPH_CORRECTION:
        raise ValueError(f"feedback item is not a graph correction: {kind}")
    patch_type = _feedback_patch_type(item)
    source = _first_node(item)
    fingerprint = str(item.get("feedback_fingerprint") or _feedback_fingerprint(item))
    risk = "high" if patch_type in {"merge_nodes", "split_node"} else "medium"
    if patch_type in {"mark_package_marker", "add_edge", "remove_edge", "move_doc_binding", "move_test_binding"}:
        risk = "low"
    return {
        "patch_id": f"gcp-{_clean_patch_id(fingerprint)}",
        "patch_type": patch_type,
        "risk_level": risk,
        "base_snapshot_id": str(item.get("snapshot_id") or item.get("source_snapshot_id") or ""),
        "base_commit": base_commit,
        "target_node_id": source,
        "stable_node_key": "",
        "patch_json": _feedback_patch_payload(item, patch_type),
        "evidence": {
            "source": "reconcile_feedback",
            "feedback_id": item.get("feedback_id") or "",
            "feedback_fingerprint": fingerprint,
            "issue_type": item.get("issue_type") or "",
            "issue": item.get("issue") or "",
            "raw_issue": _raw_issue(item),
        },
    }


def promote_feedback_items_to_graph_patches(
    conn,
    project_id: str,
    snapshot_id: str,
    feedback_ids: list[str],
    *,
    actor: str = "observer",
    accept_patch: bool = False,
    allow_high_risk_accept: bool = False,
    base_commit: str = "",
) -> dict[str, Any]:
    """Create graph correction patch rows from semantic feedback items.

    High-risk structural operations such as merge/split remain proposed unless
    ``allow_high_risk_accept`` is explicit, so accepting a feedback item cannot
    silently rewrite topology on the next reconcile pass.
    """
    from . import graph_correction_patches as patches

    patches.ensure_schema(conn)
    aliases = _load_node_aliases(conn, project_id, snapshot_id)
    state = load_feedback_state(project_id, snapshot_id)
    items = state.setdefault("items", {})
    ids = [str(item or "").strip() for item in feedback_ids if str(item or "").strip()]
    if not ids:
        raise ValueError("at least one feedback_id is required")
    now = _utc_now()
    created: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    updated_items: list[dict[str, Any]] = []
    for feedback_id in ids:
        item = dict(items.get(feedback_id) or {})
        if not item:
            errors.append({"feedback_id": feedback_id, "error": "feedback item not found"})
            continue
        try:
            if str(item.get("final_feedback_kind") or item.get("feedback_kind") or "") != KIND_GRAPH_CORRECTION:
                item = _normalize_reviewed_item(
                    item,
                    decision=KIND_GRAPH_CORRECTION,
                    rationale="Promoted to graph correction patch queue.",
                    confidence=item.get("confidence") or 0.0,
                    status_observation_category="",
                    actor=actor,
                    accept=accept_patch,
                )
            patch = _resolve_patch_payload_aliases(
                build_graph_correction_patch_from_feedback(item, base_commit=base_commit),
                aliases,
            )
            _validate_graph_patch_payload(patch)
            row = patches.create_patch(
                conn,
                project_id,
                patch_id=patch["patch_id"],
                patch_type=patch["patch_type"],
                patch_json=patch["patch_json"],
                evidence=patch["evidence"],
                status=patches.PATCH_STATUS_PROPOSED,
                risk_level=patch["risk_level"],
                base_snapshot_id=patch["base_snapshot_id"],
                base_commit=patch["base_commit"],
                target_node_id=patch["target_node_id"],
                stable_key=patch["stable_node_key"],
                created_by=actor,
            )
            patch_status = row["status"]
            accepted = False
            if accept_patch and (patch["risk_level"] != "high" or allow_high_risk_accept):
                accepted = patches.accept_patch(conn, project_id, patch["patch_id"], accepted_by=actor)
                patch_status = patches.PATCH_STATUS_ACCEPTED if accepted else patch_status
            item["graph_correction_patch_id"] = patch["patch_id"]
            item["graph_correction_patch_status"] = patch_status
            item["graph_correction_patch_type"] = patch["patch_type"]
            item["graph_correction_patch_risk_level"] = patch["risk_level"]
            item["updated_at"] = now
            if accept_patch:
                item["status"] = STATUS_ACCEPTED
                item["final_feedback_kind"] = KIND_GRAPH_CORRECTION
                item["accepted_by"] = actor
                item["accepted_at"] = now
            elif item.get("status") == STATUS_CLASSIFIED:
                item["status"] = STATUS_REVIEWED
                item["final_feedback_kind"] = KIND_GRAPH_CORRECTION
            created.append({
                **row,
                "status": patch_status,
                "accepted": accepted,
                "feedback_id": feedback_id,
                "risk_level": patch["risk_level"],
            })
            updated_items.append(item)
        except Exception as exc:
            errors.append({"feedback_id": feedback_id, "error": str(exc)})
    if updated_items:
        _upsert_items(
            project_id,
            snapshot_id,
            updated_items,
            event_type="feedback.graph_patch_promoted",
            actor=actor,
        )
    return {
        "ok": not errors,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "requested_count": len(ids),
        "created_count": len(created),
        "error_count": len(errors),
        "patches": created,
        "errors": errors,
        "summary": feedback_summary(project_id, snapshot_id),
    }


def decide_feedback_items(
    project_id: str,
    snapshot_id: str,
    feedback_ids: list[str],
    *,
    action: str,
    actor: str = "observer",
    rationale: str = "",
    decision: str = "",
    status_observation_category: str = "",
    accept: bool | None = None,
) -> dict[str, Any]:
    """Apply explicit observer/user decisions to feedback items.

    This is a state-only operation. It does not mutate project files, graph
    topology, or backlog rows.
    """
    normalized_action = str(action or "").strip()
    if normalized_action not in FEEDBACK_DECISION_ACTIONS:
        raise ValueError(f"invalid feedback decision action: {action}")
    ids = [str(item or "").strip() for item in feedback_ids if str(item or "").strip()]
    if not ids:
        raise ValueError("at least one feedback_id is required")

    mapped_decision = str(decision or "").strip()
    mapped_accept = bool(accept) if accept is not None else False
    if normalized_action == "accept_graph_correction":
        mapped_decision = KIND_GRAPH_CORRECTION
        mapped_accept = True
    elif normalized_action == "accept_project_improvement":
        mapped_decision = KIND_PROJECT_IMPROVEMENT
        mapped_accept = True
    elif normalized_action == "accept_semantic_enrichment":
        # MF-2026-05-10-016 follow-up: the operator's accept_semantic_enrichment
        # click IS the human signoff. Without this mapping the decision falls
        # through to mapped_accept=False and the row lands in
        # needs_human_signoff, which the dashboard surfaces as "Accept did
        # nothing". Map the action to mapped_accept=True so the call is
        # idempotent regardless of whether the caller also sends accept=true.
        mapped_accept = True
    elif normalized_action == "revise_semantic_enrichment":
        mapped_accept = True
    elif normalized_action == "keep_status_observation":
        mapped_decision = KIND_STATUS_OBSERVATION
        mapped_accept = True
    elif normalized_action == "reject_false_positive":
        mapped_decision = KIND_FALSE_POSITIVE
        mapped_accept = False
    elif normalized_action == "needs_human_signoff":
        mapped_decision = "needs_human_signoff"
        mapped_accept = False
    elif normalized_action == "reclassify" and not mapped_decision:
        raise ValueError("decision is required for reclassify")

    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for feedback_id in ids:
        try:
            reviewed = review_feedback_item(
                project_id,
                snapshot_id,
                feedback_id,
                decision=mapped_decision,
                rationale=rationale,
                status_observation_category=status_observation_category,
                actor=actor,
                accept=mapped_accept,
            )
            item = dict((reviewed.get("items") or [{}])[0])
            item["decision_action"] = normalized_action
            results.append(item)
        except Exception as exc:
            errors.append({"feedback_id": feedback_id, "error": str(exc)})

    return {
        "ok": not errors,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "action": normalized_action,
        "requested_count": len(ids),
        "decided_count": len(results),
        "error_count": len(errors),
        "items": results,
        "errors": errors,
        "summary": feedback_summary(project_id, snapshot_id),
    }


__all__ = [
    "FEEDBACK_EVENTS_NAME",
    "FEEDBACK_STATE_NAME",
    "KIND_GRAPH_CORRECTION",
    "KIND_PROJECT_IMPROVEMENT",
    "KIND_STATUS_OBSERVATION",
    "KIND_NEEDS_OBSERVER_DECISION",
    "KIND_FALSE_POSITIVE",
    "FEEDBACK_DECISION_ACTIONS",
    "STATUS_OBSERVATION_CATEGORIES",
    "STATUS_CATEGORY_STALE_TEST",
    "STATUS_CATEGORY_DOC_DRIFT",
    "STATUS_CATEGORY_COVERAGE_GAP",
    "STATUS_CATEGORY_PROJECT_REGRESSION",
    "STATUS_CATEGORY_ORPHAN_REVIEW",
    "STATUS_CATEGORY_FALSE_POSITIVE",
    "STATUS_CATEGORY_NEEDS_HUMAN",
    "feedback_action_catalog",
    "submit_feedback_item",
    "classify_open_issue",
    "classify_semantic_open_issues",
    "classify_semantic_state_rounds",
    "build_feedback_review_queue",
    "claim_feedback_review_queue",
    "build_feedback_retrieval_context",
    "graph_query_context",
    "grep_in_scope",
    "read_project_excerpt",
    "infer_status_observation_category",
    "list_feedback_items",
    "load_feedback_state",
    "review_feedback_item",
    "review_feedback_items_batch",
    "build_project_improvement_backlog",
    "mark_feedback_backlog_filed",
    "build_graph_correction_patch_from_feedback",
    "promote_feedback_items_to_graph_patches",
    "decide_feedback_items",
    "feedback_summary",
    "feedback_state_path",
    "feedback_events_path",
]
