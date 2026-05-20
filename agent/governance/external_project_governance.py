"""External project governance workspace and scan artifacts.

This module is the minimal bootstrap surface for governing a project outside
aming-claw itself.  It creates the project-local ``.aming-claw`` workspace and
materializes scan artifacts there without mutating the canonical graph.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.governance.batch_jobs import JOB_FULL_RECONCILE
from agent.governance.project_profile import ProjectProfile, discover_project_profile
from agent.governance.reconcile_file_inventory import summarize_file_inventory
from agent.governance.reconcile_phases.phase_z_v2 import (
    build_graph_v2_from_symbols,
    build_rebase_candidate_graph,
    function_source_hashes,
    parse_production_modules,
)


GOVERNANCE_DIR = ".aming-claw"
TRACKED_PROJECT_FILE = "project.yaml"
FEATURE_INDEX_FILE = "feature-index.md"
COVERAGE_STATE_FILE = "coverage-state.json"
IGNORED_RUNTIME_DIRS = ("cache", "sessions", "baselines", "logs")
ROOT_GITIGNORE_MARKER = "# aming-claw runtime artifacts"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def governance_root(project_root: str | Path) -> Path:
    return Path(project_root).resolve() / GOVERNANCE_DIR


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip())
    text = text.strip("-._")
    return text or "external-project"


def _git_head_short(project_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--short=7", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return "nogit"
    return (result.stdout or "").strip() or "nogit"


def make_session_id(job_type: str, base_commit: str) -> str:
    return f"{_slug(job_type)}-{_slug(base_commit)}-{uuid.uuid4().hex[:8]}"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def ensure_root_gitignore_entries(project_root: str | Path) -> list[str]:
    """Ensure external project runtime outputs are ignored by its root git."""
    root = Path(project_root).resolve()
    gitignore = root / ".gitignore"
    desired = [f"{GOVERNANCE_DIR}/{name}/" for name in IGNORED_RUNTIME_DIRS]
    existing_text = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    existing_lines = {
        line.strip()
        for line in existing_text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    missing = [line for line in desired if line not in existing_lines]
    if not missing:
        return []
    parts = [existing_text.rstrip(), ROOT_GITIGNORE_MARKER, *missing]
    _write_text(gitignore, "\n".join(part for part in parts if part) + "\n")
    return missing


def ensure_governance_layout(
    project_root: str | Path,
    *,
    project_id: str | None = None,
) -> dict[str, str]:
    """Create the project-local ``.aming-claw`` workspace."""
    root = Path(project_root).resolve()
    gov_root = governance_root(root)
    gov_root.mkdir(parents=True, exist_ok=True)
    for dirname in IGNORED_RUNTIME_DIRS:
        (gov_root / dirname).mkdir(parents=True, exist_ok=True)
    _write_text(
        gov_root / ".gitignore",
        "\n".join(f"{dirname}/" for dirname in IGNORED_RUNTIME_DIRS) + "\n",
    )
    ensure_root_gitignore_entries(root)

    pid = project_id or _slug(root.name)
    project_file = gov_root / TRACKED_PROJECT_FILE
    if not project_file.exists():
        _write_text(
            project_file,
            "\n".join([
                "storage_version: 1",
                f"project_id: {pid}",
                f"project_root_name: {root.name}",
                f"created_at: {utc_now()}",
                "runtime_artifacts: ignored",
                "",
            ]),
        )
    return {
        "project_root": str(root),
        "governance_root": str(gov_root),
        "project_file": str(project_file),
    }


def _profile_payload(profile: ProjectProfile) -> dict[str, Any]:
    return asdict(profile)


def _deps_graph_nodes(candidate_graph: dict[str, Any]) -> list[dict[str, Any]]:
    graph = candidate_graph.get("deps_graph") if isinstance(candidate_graph, dict) else {}
    nodes = graph.get("nodes") if isinstance(graph, dict) else []
    return [node for node in nodes or [] if isinstance(node, dict)]


def _paths_from_node(node: dict[str, Any], key: str) -> list[str]:
    raw = node.get(key) or []
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for item in raw:
        text = str(item or "").replace("\\", "/").strip("/")
        if text:
            out.append(text)
    return sorted(set(out))


def build_coverage_state(
    *,
    candidate_graph: dict[str, Any],
    file_inventory: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize graph/file coverage for dashboard and drift checks."""
    nodes = _deps_graph_nodes(candidate_graph)
    source_nodes = [node for node in nodes if _paths_from_node(node, "primary")]
    referenced_files: set[str] = set()
    missing_doc_nodes: list[str] = []
    missing_test_nodes: list[str] = []
    for node in source_nodes:
        node_id = str(node.get("id") or node.get("node_id") or "")
        primary = _paths_from_node(node, "primary")
        docs = _paths_from_node(node, "secondary")
        tests = _paths_from_node(node, "test")
        referenced_files.update(primary)
        referenced_files.update(docs)
        referenced_files.update(tests)
        if not docs:
            missing_doc_nodes.append(node_id)
        if not tests:
            missing_test_nodes.append(node_id)
    return {
        "generated_at": utc_now(),
        "candidate_node_count": len(nodes),
        "source_leaf_count": len(source_nodes),
        "missing_doc_node_count": len(missing_doc_nodes),
        "missing_test_node_count": len(missing_test_nodes),
        "missing_doc_node_sample": missing_doc_nodes[:25],
        "missing_test_node_sample": missing_test_nodes[:25],
        "referenced_file_count": len(referenced_files),
        "file_inventory_summary": summarize_file_inventory(file_inventory),
        "file_hashes": {
            str(row.get("path") or ""): str(row.get("file_hash") or row.get("sha256") or "")
            for row in file_inventory
            if row.get("path")
        },
        "file_states": {
            str(row.get("path") or ""): {
                "file_hash": str(row.get("file_hash") or row.get("sha256") or ""),
                "size_bytes": int(row.get("size_bytes") or 0),
                "graph_status": str(row.get("graph_status") or ""),
                "mapped_node_ids": list(row.get("mapped_node_ids") or []),
                "attached_node_ids": list(row.get("attached_node_ids") or []),
                "attachment_role": str(row.get("attachment_role") or ""),
                "attachment_source": str(row.get("attachment_source") or ""),
                "file_kind": str(row.get("file_kind") or ""),
                "scan_status": str(row.get("scan_status") or ""),
                "candidate_node_id": str(row.get("candidate_node_id") or ""),
                "attached_to": str(row.get("attached_to") or ""),
                "last_scanned_commit": str(row.get("last_scanned_commit") or ""),
            }
            for row in file_inventory
            if row.get("path")
        },
    }


def build_symbol_index(
    *,
    project_root: str | Path,
    file_inventory: list[dict[str, Any]],
    profile: ProjectProfile | None = None,
) -> dict[str, Any]:
    """Build the MVP symbol/location index for prompt context and drift checks."""
    root = Path(project_root).resolve()
    profile = profile or discover_project_profile(str(root))
    symbols: list[dict[str, Any]] = []
    covered_files: set[str] = set()
    seen_symbol_ids: set[str] = set()
    try:
        modules = parse_production_modules(str(root), profile=profile)
    except Exception:
        modules = {}
    for module_name, module in sorted(modules.items()):
        rel = _relpath(root, module.path)
        covered_files.add(rel)
        function_hashes = function_source_hashes(module)
        for func in module.functions:
            symbol = {
                "id": func.qualified_name,
                "kind": "function",
                "language": module.language or "",
                "module": module_name,
                "path": rel,
                "line_start": func.lineno,
                "line_end": func.end_lineno,
                "source_hash": function_hashes.get(func.qualified_name, ""),
                "decorators": list(func.decorators or []),
                "calls": list(func.calls or []),
            }
            symbols.append(symbol)
            seen_symbol_ids.add(str(symbol["id"]))

    for row in sorted(file_inventory, key=lambda r: str(r.get("path") or "")):
        rel = str(row.get("path") or "").replace("\\", "/").strip("/")
        if not rel.endswith(".py"):
            continue
        if row.get("file_kind") not in {"source", "test"} and row.get("language") != "python":
            continue
        for symbol in _parse_python_file_symbols(root, rel, str(row.get("file_kind") or "")):
            symbol_id = str(symbol.get("id") or "")
            if not symbol_id or symbol_id in seen_symbol_ids:
                continue
            symbols.append(symbol)
            seen_symbol_ids.add(symbol_id)

    file_symbols: list[dict[str, Any]] = []
    for row in sorted(file_inventory, key=lambda r: str(r.get("path") or "")):
        if row.get("file_kind") != "source":
            continue
        path = str(row.get("path") or "")
        language = str(row.get("language") or "")
        if not path or (path in covered_files and language == "python"):
            continue
        file_symbols.append({
            "id": f"file::{path}",
            "kind": "file",
            "language": language,
            "path": path,
            "line_start": 1,
            "line_end": _count_lines(root / path),
            "sha256": row.get("sha256") or "",
        })

    return {
        "generated_at": utc_now(),
        "schema_version": 1,
        "symbol_count": len(symbols) + len(file_symbols),
        "symbols": symbols + file_symbols,
    }


def build_doc_index(
    *,
    project_root: str | Path,
    file_inventory: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the MVP doc heading index for stale-doc detection."""
    root = Path(project_root).resolve()
    documents: list[dict[str, Any]] = []
    for row in sorted(file_inventory, key=lambda r: str(r.get("path") or "")):
        if row.get("file_kind") not in {"doc", "index_doc"}:
            continue
        rel = str(row.get("path") or "")
        if not rel:
            continue
        path = root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="latin-1")
        except OSError:
            text = ""
        headings = _extract_doc_headings(text)
        documents.append({
            "path": rel,
            "file_kind": row.get("file_kind") or "",
            "sha256": row.get("sha256") or "",
            "scan_status": row.get("scan_status") or "",
            "headings": headings,
        })
    return {
        "generated_at": utc_now(),
        "schema_version": 1,
        "document_count": len(documents),
        "heading_count": sum(len(doc.get("headings") or []) for doc in documents),
        "documents": documents,
    }


def _relpath(root: Path, path: str | Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(root)).replace("\\", "/")
    except Exception:
        return str(path).replace("\\", "/").strip("/")


def _count_lines(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8") as f:
            count = sum(1 for _line in f)
    except UnicodeDecodeError:
        with path.open("r", encoding="latin-1") as f:
            count = sum(1 for _line in f)
    except OSError:
        return 0
    return max(1, count)


def _source_span_hash(lines: list[str], node: ast.AST) -> str:
    start = int(getattr(node, "lineno", 0) or 0)
    end = int(getattr(node, "end_lineno", start) or start)
    if start <= 0:
        return ""
    end = max(start, end)
    snippet = "\n".join(lines[start - 1 : min(len(lines), end)])
    return f"sha256:{hashlib.sha256(snippet.encode('utf-8')).hexdigest()}"


def _module_name_from_rel(rel_path: str) -> str:
    rel = str(rel_path or "").replace("\\", "/").strip("/")
    if rel.endswith(".py"):
        rel = rel[:-3]
    parts = [part for part in rel.split("/") if part]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or "module"


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _decorator_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return ""


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _function_calls(node: ast.AST) -> list[str]:
    calls: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _call_name(child.func)
            if name:
                calls.add(name)
    return sorted(calls)


def _assigned_names(node: ast.AST) -> list[str]:
    targets: list[ast.AST] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    names: list[str] = []
    for target in targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            names.extend(elt.id for elt in target.elts if isinstance(elt, ast.Name))
    return sorted(set(names))


def _parse_python_file_symbols(root: Path, rel_path: str, file_kind: str) -> list[dict[str, Any]]:
    path = root / rel_path
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="latin-1")
    except OSError:
        return []
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return []

    module = _module_name_from_rel(rel_path)
    lines = text.splitlines()
    symbols: list[dict[str, Any]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "test_function" if node.name.startswith("test_") else "function"
            symbols.append({
                "id": f"{module}::{node.name}",
                "kind": kind,
                "language": "python",
                "module": module,
                "path": rel_path,
                "line_start": int(getattr(node, "lineno", 1) or 1),
                "line_end": int(getattr(node, "end_lineno", getattr(node, "lineno", 1)) or 1),
                "source_hash": _source_span_hash(lines, node),
                "decorators": [
                    name for name in (_decorator_name(item) for item in node.decorator_list) if name
                ],
                "calls": _function_calls(node),
            })
        elif isinstance(node, ast.ClassDef):
            symbols.append({
                "id": f"{module}::{node.name}",
                "kind": "class",
                "language": "python",
                "module": module,
                "path": rel_path,
                "line_start": int(getattr(node, "lineno", 1) or 1),
                "line_end": int(getattr(node, "end_lineno", getattr(node, "lineno", 1)) or 1),
                "decorators": [
                    name for name in (_decorator_name(item) for item in node.decorator_list) if name
                ],
                "calls": _function_calls(node),
            })
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for name in _assigned_names(node):
                if not name.isupper():
                    continue
                symbols.append({
                    "id": f"{module}::{name}",
                    "kind": "constant",
                    "language": "python",
                    "module": module,
                    "path": rel_path,
                    "line_start": int(getattr(node, "lineno", 1) or 1),
                    "line_end": int(getattr(node, "end_lineno", getattr(node, "lineno", 1)) or 1),
                })
    return symbols


def _extract_doc_headings(text: str) -> list[dict[str, Any]]:
    headings: list[dict[str, Any]] = []
    for idx, line in enumerate((text or "").splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        marker = stripped.split(" ", 1)[0]
        if not marker or any(ch != "#" for ch in marker):
            continue
        title = stripped[len(marker):].strip()
        if not title:
            continue
        headings.append({
            "title": title,
            "level": len(marker),
            "line_start": idx,
            "anchor": _doc_anchor(title),
        })
    if not headings and text:
        headings.append({
            "title": "document",
            "level": 0,
            "line_start": 1,
            "anchor": "document",
        })
    return headings


def _doc_anchor(title: str) -> str:
    anchor = re.sub(r"[^a-z0-9 -]", "", title.lower())
    anchor = re.sub(r"\s+", "-", anchor.strip())
    return anchor or "document"


def render_candidate_feature_index(
    *,
    project_id: str,
    session_id: str,
    candidate_graph: dict[str, Any],
    coverage_state: dict[str, Any],
) -> str:
    """Render the project-local index for a candidate governance scan."""
    nodes = sorted(
        [node for node in _deps_graph_nodes(candidate_graph) if _paths_from_node(node, "primary")],
        key=lambda n: str(n.get("id") or ""),
    )
    summary = coverage_state.get("file_inventory_summary") or {}
    lines = [
        "# Aming Claw Feature Index",
        "",
        "This file is generated from the latest candidate governance scan. The",
        "candidate graph is reviewable state, not the canonical graph until a",
        "reconcile signoff materializes it.",
        "",
        "## Scan",
        "",
        f"- project_id: `{project_id}`",
        f"- session_id: `{session_id}`",
        f"- candidate_nodes: `{coverage_state.get('candidate_node_count', 0)}`",
        f"- source_leafs: `{coverage_state.get('source_leaf_count', 0)}`",
        f"- missing_docs: `{coverage_state.get('missing_doc_node_count', 0)}`",
        f"- missing_tests: `{coverage_state.get('missing_test_node_count', 0)}`",
        "",
        "## Feature Nodes",
        "",
        "| Node | Feature | Code | Docs | Tests | Debt |",
        "|---|---|---|---|---|---|",
    ]
    for node in nodes:
        docs = _paths_from_node(node, "secondary")
        tests = _paths_from_node(node, "test")
        debt = []
        if not docs:
            debt.append("doc")
        if not tests:
            debt.append("test")
        lines.append(
            "| {node} | {title} | {code} | {docs} | {tests} | {debt} |".format(
                node=f"`{str(node.get('id') or '')}`",
                title=str(node.get("title") or "").replace("|", "\\|"),
                code=_format_paths(_paths_from_node(node, "primary")),
                docs=_format_paths(docs),
                tests=_format_paths(tests),
                debt=", ".join(debt) if debt else "none",
            )
        )
    lines.extend([
        "",
        "## File Inventory",
        "",
        f"- total: `{summary.get('total', 0)}`",
        f"- pending_decision_count: `{summary.get('pending_decision_count', 0)}`",
        "",
        "### Pending Sample",
        "",
    ])
    sample = summary.get("pending_decision_sample") or []
    if sample:
        lines.extend(f"- `{path}`" for path in sample)
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def _format_paths(paths: list[str], max_items: int = 3) -> str:
    if not paths:
        return "missing"
    shown = [f"`{path}`" for path in paths[:max_items]]
    if len(paths) > max_items:
        shown.append(f"+{len(paths) - max_items} more")
    return "<br>".join(shown)


def scan_external_project(
    project_root: str | Path,
    *,
    project_id: str | None = None,
    job_type: str = JOB_FULL_RECONCILE,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Run a candidate full-project governance scan for an external project."""
    root = Path(project_root).resolve()
    pid = project_id or _slug(root.name)
    base_commit = _git_head_short(root)
    sid = session_id or make_session_id(job_type, base_commit)
    layout = ensure_governance_layout(root, project_id=pid)
    gov_root = Path(layout["governance_root"])
    session_dir = gov_root / "sessions" / sid
    session_dir.mkdir(parents=True, exist_ok=True)

    profile = discover_project_profile(str(root))
    phase_result = build_graph_v2_from_symbols(
        str(root),
        dry_run=True,
        scratch_dir=str(session_dir),
        run_id=sid,
    )
    candidate_graph = build_rebase_candidate_graph(
        str(root),
        phase_result,
        session_id=sid,
        run_id=sid,
    )
    file_inventory = [
        row for row in (phase_result.get("file_inventory") or [])
        if isinstance(row, dict)
    ]
    for row in file_inventory:
        if not row.get("last_scanned_commit"):
            row["last_scanned_commit"] = base_commit
        if row.get("sha256") and not row.get("file_hash"):
            row["file_hash"] = f"sha256:{row['sha256']}"
    coverage_state = build_coverage_state(
        candidate_graph=candidate_graph,
        file_inventory=file_inventory,
    )

    profile_path = session_dir / "project-profile.json"
    inventory_path = session_dir / "file-inventory.json"
    candidate_path = session_dir / "graph.rebase.candidate.json"
    symbol_index_path = session_dir / "symbol-index.json"
    doc_index_path = session_dir / "doc-index.json"
    coverage_path = gov_root / COVERAGE_STATE_FILE
    coverage_cache_path = gov_root / "cache" / "coverage-state.live.json"
    inventory_cache_path = gov_root / "cache" / "file-inventory.json"
    symbol_index_cache_path = gov_root / "cache" / "symbol-index.json"
    doc_index_cache_path = gov_root / "cache" / "doc-index.json"
    feature_index_path = gov_root / FEATURE_INDEX_FILE
    summary_path = session_dir / "summary.json"
    symbol_index = build_symbol_index(
        project_root=root,
        file_inventory=file_inventory,
        profile=profile,
    )
    doc_index = build_doc_index(project_root=root, file_inventory=file_inventory)
    coverage_state["symbol_count"] = symbol_index.get("symbol_count", 0)
    coverage_state["doc_heading_count"] = doc_index.get("heading_count", 0)

    _write_json(profile_path, _profile_payload(profile))
    _write_json(inventory_path, file_inventory)
    _write_json(inventory_cache_path, file_inventory)
    _write_json(candidate_path, candidate_graph)
    _write_json(symbol_index_path, symbol_index)
    _write_json(symbol_index_cache_path, symbol_index)
    _write_json(doc_index_path, doc_index)
    _write_json(doc_index_cache_path, doc_index)
    _write_json(coverage_path, coverage_state)
    _write_json(coverage_cache_path, coverage_state)
    _write_text(
        feature_index_path,
        render_candidate_feature_index(
            project_id=pid,
            session_id=sid,
            candidate_graph=candidate_graph,
            coverage_state=coverage_state,
        ),
    )

    summary = {
        "project_id": pid,
        "job_type": job_type,
        "session_id": sid,
        "base_commit": base_commit,
        "status": phase_result.get("status", "unknown"),
        "governance_root": str(gov_root),
        "session_dir": str(session_dir),
        "profile_path": str(profile_path),
        "candidate_graph_path": str(candidate_path),
        "symbol_index_path": str(symbol_index_path),
        "doc_index_path": str(doc_index_path),
        "file_inventory_path": str(inventory_path),
        "coverage_state_path": str(coverage_path),
        "coverage_state_cache_path": str(coverage_cache_path),
        "feature_index_path": str(feature_index_path),
        "phase_report_path": str(phase_result.get("report_path") or ""),
        "candidate_node_count": coverage_state.get("candidate_node_count", 0),
        "source_leaf_count": coverage_state.get("source_leaf_count", 0),
        "symbol_count": symbol_index.get("symbol_count", 0),
        "doc_heading_count": doc_index.get("heading_count", 0),
        "file_inventory_summary": coverage_state.get("file_inventory_summary") or {},
    }
    _write_json(summary_path, summary)
    summary["summary_path"] = str(summary_path)
    return summary


__all__ = [
    "COVERAGE_STATE_FILE",
    "FEATURE_INDEX_FILE",
    "GOVERNANCE_DIR",
    "build_coverage_state",
    "build_doc_index",
    "build_symbol_index",
    "ensure_governance_layout",
    "ensure_root_gitignore_entries",
    "governance_root",
    "make_session_id",
    "render_candidate_feature_index",
    "scan_external_project",
]
