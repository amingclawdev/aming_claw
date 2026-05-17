"""Batch job metadata, branch strategy, and worktree lifecycle helpers.

This module intentionally keeps job_type separate from task/stage type.  The
existing task ``type`` column still drives PM/Dev/Test/QA/Merge routing; job_type
describes the broader work container and branch strategy.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


JOB_FEATURE_WORK = "feature_work"
JOB_FULL_RECONCILE = "full_reconcile"
JOB_SCOPE_RECONCILE = "scope_reconcile"
JOB_MANUAL_FIX = "manual_fix"
JOB_BATCH_MIGRATION = "batch_migration"
JOB_WORKFLOW_IMPROVEMENT = "workflow_improvement"
BRANCH_GRAPH_SCHEMA_VERSION = 1
BRANCH_GRAPH_CACHE_REL = ".aming-claw/cache/branches"
BRANCH_GRAPH_POLICY_ONE_HOP = "one_hop_target_graph_candidate"
BRANCH_GRAPH_CANDIDATE_KIND = "branch_delta"
GRAPH_JSON_FILENAME = "graph.json"

VALID_JOB_TYPES = {
    JOB_FEATURE_WORK,
    JOB_FULL_RECONCILE,
    JOB_SCOPE_RECONCILE,
    JOB_MANUAL_FIX,
    JOB_BATCH_MIGRATION,
    JOB_WORKFLOW_IMPROVEMENT,
}

STAGE_TYPES = {"pm", "dev", "test", "qa", "gatekeeper", "merge", "deploy", "task"}
BATCH_ACTIVE_STATUSES = {
    "created",
    "branch_created",
    "worktree_ready",
    "implementation_started",
    "tests_running",
    "tests_passed",
    "ready_for_review",
}
BATCH_TERMINAL_STATUSES = {
    "merged",
    "redeployed",
    "closed",
    "failed",
    "abandoned",
}


class BatchJobError(ValueError):
    """Raised for invalid batch job state or unsafe branch/worktree requests."""


class ActiveBatchExistsError(BatchJobError):
    """Raised when one-active-batch policy blocks a new batch job."""


@dataclass(frozen=True)
class BranchStrategy:
    job_type: str
    target_branch: str
    base_commit: str
    work_branch: str
    worktree_path: str
    worktree_relpath: str
    direct: bool = False
    merge_policy: str = "merge_gatekeeper"
    project_id: str = ""

    def to_metadata(self) -> dict[str, Any]:
        return asdict(self)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_metadata(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
    return {}


def _normalize_token(value: Any, fallback: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9._/-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-/.")
    return text or fallback


def _path_token(value: Any, fallback: str) -> str:
    return _normalize_token(value, fallback).replace("/", "-")


def _short_commit(value: str) -> str:
    text = str(value or "").strip()
    return text[:7] if text else "unknown"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return default


def _git_output(args: list[str], *, cwd: Path, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if check and proc.returncode != 0:
        raise BatchJobError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def repo_root(path: str | Path | None = None) -> Path:
    start = Path(path or ".").resolve()
    try:
        root = _git_output(["rev-parse", "--show-toplevel"], cwd=start)
        if root:
            return Path(root).resolve()
    except BatchJobError:
        pass
    return start


def git_commit(path: str | Path | None = None, ref: str = "HEAD", *, short: bool = False) -> str:
    root = repo_root(path)
    flag = "--short" if short else "--verify"
    args = ["rev-parse", flag, ref] if short else ["rev-parse", "--verify", ref]
    return _git_output(args, cwd=root)


def infer_job_type(task_type: str = "task", metadata: dict[str, Any] | None = None) -> str:
    """Infer job_type without changing task/stage routing."""
    metadata = metadata or {}
    raw = str(metadata.get("job_type") or "").strip()
    if raw:
        normalized = raw.lower().replace("-", "_")
        if normalized not in VALID_JOB_TYPES:
            raise BatchJobError(f"unknown job_type: {raw}")
        return normalized

    operation_type = str(metadata.get("operation_type") or "").strip()
    if operation_type == "reconcile-cluster":
        return JOB_FULL_RECONCILE

    task_type = str(task_type or "task")
    if task_type == "reconcile":
        return JOB_FULL_RECONCILE
    if task_type.startswith("reconcile_"):
        return JOB_SCOPE_RECONCILE
    if metadata.get("mf_id") or metadata.get("mf_type"):
        return JOB_MANUAL_FIX
    return JOB_FEATURE_WORK


def normalize_job_metadata(metadata: dict[str, Any] | None, *, task_type: str = "task") -> dict[str, Any]:
    """Attach job_type/stage_type metadata while preserving existing routing."""
    out = dict(metadata or {})
    job_type = infer_job_type(task_type, out)
    out["job_type"] = job_type
    out.setdefault("stage_type", task_type or "task")
    return out


def make_batch_id(prefix: str = "batch") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{_normalize_token(prefix, 'batch')}-{stamp}-{uuid.uuid4().hex[:6]}"


def resolve_branch_strategy(
    *,
    job_type: str,
    repo_root_path: str | Path,
    project_id: str = "",
    target_branch: str = "main",
    base_commit: str = "",
    batch_id: str = "",
    scope_id: str = "",
) -> BranchStrategy:
    """Resolve a deterministic branch/worktree strategy without mutating git."""
    root = repo_root(repo_root_path)
    job_type = infer_job_type("task", {"job_type": job_type})
    target_branch = _normalize_token(target_branch, "main")
    base = str(base_commit or "").strip() or git_commit(root)
    short = _short_commit(base)
    project_token = _normalize_token(project_id, "project")

    direct = False
    merge_policy = "merge_gatekeeper"
    work_branch = ""
    worktree_rel = ""

    if job_type == JOB_MANUAL_FIX:
        direct = True
        merge_policy = "direct_main"
        work_branch = target_branch
    elif job_type == JOB_FULL_RECONCILE:
        work_branch = f"reconcile/full-{short}"
        worktree_rel = f".worktrees/reconcile-full-{short}"
    elif job_type == JOB_SCOPE_RECONCILE:
        token = _normalize_token(scope_id or f"catchup-{short}", f"catchup-{short}")
        work_branch = f"scope/{token}"
        worktree_rel = f".worktrees/scope-{token}"
    elif job_type == JOB_BATCH_MIGRATION:
        token = _normalize_token(batch_id or make_batch_id("batch"), "batch")
        work_branch = f"codex/batch-{token}"
        worktree_rel = f".worktrees/batch-{token}"
    elif job_type == JOB_WORKFLOW_IMPROVEMENT:
        token = _normalize_token(batch_id or make_batch_id("workflow"), "workflow")
        work_branch = f"codex/workflow-{token}"
        worktree_rel = f".worktrees/workflow-{token}"
    else:
        token = _normalize_token(batch_id or f"{project_token}-{short}", f"{project_token}-{short}")
        work_branch = target_branch
        worktree_rel = f".worktrees/feature-{token}"

    worktree_path = "" if direct else str((root / worktree_rel).resolve())
    return BranchStrategy(
        job_type=job_type,
        target_branch=target_branch,
        base_commit=base,
        work_branch=work_branch,
        worktree_path=worktree_path,
        worktree_relpath=worktree_rel,
        direct=direct,
        merge_policy=merge_policy,
        project_id=project_id,
    )


def branch_graph_plan(strategy: BranchStrategy, *, status: str = "planned") -> dict[str, Any]:
    """Return deterministic branch-local graph paths for a worktree strategy."""
    if strategy.direct or not strategy.worktree_path:
        return {"required": False, "status": "not_applicable"}
    token = _path_token(strategy.work_branch, "branch")
    graph_dir = Path(strategy.worktree_path) / BRANCH_GRAPH_CACHE_REL / token
    return {
        "required": True,
        "status": status,
        "schema_version": BRANCH_GRAPH_SCHEMA_VERSION,
        "graph_policy": BRANCH_GRAPH_POLICY_ONE_HOP,
        "candidate_kind": BRANCH_GRAPH_CANDIDATE_KIND,
        "chain_depth": 1,
        "active_target_graph_truth": False,
        "recompute_when_target_moves": True,
        "project_id": strategy.project_id,
        "work_branch": strategy.work_branch,
        "target_branch": strategy.target_branch,
        "base_commit": strategy.base_commit,
        "graph_dir": str(graph_dir),
        "snapshot_path": str(graph_dir / "graph.base.json"),
        "overlay_path": str(graph_dir / "graph.branch.overlay.json"),
        "manifest_path": str(graph_dir / "manifest.json"),
    }


def _resolve_graph_source_path(
    *,
    repo_root_path: str | Path,
    project_id: str,
    metadata: dict[str, Any] | None = None,
) -> Path | None:
    metadata = metadata or {}
    root = repo_root(repo_root_path)
    override = str(metadata.get("graph_path") or metadata.get("graph_json_path") or "").strip()
    if override:
        p = Path(override)
        return p if p.is_absolute() else root / p
    if project_id:
        try:
            from .db import _resolve_project_dir
            resolved = _resolve_project_dir(project_id) / GRAPH_JSON_FILENAME
            if resolved.exists():
                return resolved
        except Exception:
            pass
        return (
            root
            / "shared-volume"
            / "codex-tasks"
            / "state"
            / "governance"
            / project_id
            / GRAPH_JSON_FILENAME
        )
    return None


def _read_graph_source(path: Path | None) -> tuple[bytes, bool]:
    if path and path.exists():
        return path.read_bytes(), True
    return b"{}\n", False


def initialize_branch_graph(
    strategy: BranchStrategy,
    *,
    repo_root_path: str | Path,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create branch-local graph snapshot and overlay artifacts.

    The artifacts live under ``.aming-claw/cache`` in the worktree. They are
    runtime governance state for the branch and are read by the batch merge gate.
    """
    if strategy.direct:
        return branch_graph_plan(strategy, status="not_applicable")
    worktree = ensure_worktree_path_safe(repo_root_path, strategy.worktree_path)
    if not worktree.exists():
        raise BatchJobError(f"worktree does not exist for branch graph init: {worktree}")

    plan = branch_graph_plan(strategy, status="ready")
    graph_dir = Path(plan["graph_dir"])
    graph_dir.mkdir(parents=True, exist_ok=True)

    source_path = _resolve_graph_source_path(
        repo_root_path=repo_root_path,
        project_id=strategy.project_id,
        metadata=metadata,
    )
    graph_bytes, source_exists = _read_graph_source(source_path)
    base_graph_sha = _sha256_bytes(graph_bytes)

    snapshot_path = Path(plan["snapshot_path"])
    snapshot_path.write_bytes(graph_bytes)

    overlay_path = Path(plan["overlay_path"])
    if not overlay_path.exists():
        overlay_doc = {
            "schema_version": BRANCH_GRAPH_SCHEMA_VERSION,
            "graph_policy": BRANCH_GRAPH_POLICY_ONE_HOP,
            "candidate_kind": BRANCH_GRAPH_CANDIDATE_KIND,
            "chain_depth": 1,
            "active_target_graph_truth": False,
            "recompute_when_target_moves": True,
            "project_id": strategy.project_id,
            "work_branch": strategy.work_branch,
            "target_branch": strategy.target_branch,
            "base_commit": strategy.base_commit,
            "base_graph_sha256": base_graph_sha,
            "derives_from": {
                "target_branch": strategy.target_branch,
                "base_commit": strategy.base_commit,
                "base_graph_sha256": base_graph_sha,
            },
            "covered_files": [],
            "file_states": {},
            "graph_delta": {},
            "created_at": utc_now(),
        }
        overlay_path.write_text(
            json.dumps(overlay_doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    manifest = {
        **plan,
        "status": "ready",
        "source_graph_path": str(source_path or ""),
        "source_graph_exists": source_exists,
        "base_graph_sha256": base_graph_sha,
        "derives_from": {
            "target_branch": strategy.target_branch,
            "base_commit": strategy.base_commit,
            "base_graph_sha256": base_graph_sha,
        },
        "created_at": utc_now(),
    }
    Path(plan["manifest_path"]).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _metadata_from_row(row: sqlite3.Row | tuple) -> dict[str, Any]:
    if hasattr(row, "keys"):
        return parse_metadata(row["metadata_json"])
    return parse_metadata(row[-1])


def list_active_batches(conn: sqlite3.Connection, project_id: str) -> list[dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT task_id, status, execution_status, metadata_json FROM tasks WHERE project_id=?",
            (project_id,),
        ).fetchall()
        has_execution_status = True
    except sqlite3.OperationalError as exc:
        if "execution_status" not in str(exc):
            raise
        rows = conn.execute(
            "SELECT task_id, status, metadata_json FROM tasks WHERE project_id=?",
            (project_id,),
        ).fetchall()
        has_execution_status = False
    active: list[dict[str, Any]] = []
    for row in rows:
        meta = _metadata_from_row(row)
        if meta.get("job_type") != JOB_BATCH_MIGRATION:
            continue
        batch_status = str(meta.get("batch_status") or "created")
        if has_execution_status:
            execution_status = row["execution_status"] if hasattr(row, "keys") else row[2]
        else:
            execution_status = row["status"] if hasattr(row, "keys") else row[1]
        if batch_status in BATCH_TERMINAL_STATUSES:
            continue
        if str(execution_status) in {"succeeded", "failed", "cancelled", "timed_out", "design_mismatch"}:
            continue
        active.append({
            "task_id": row["task_id"] if hasattr(row, "keys") else row[0],
            "execution_status": execution_status,
            "batch_status": batch_status,
            "metadata": meta,
        })
    return active


def assert_no_active_batch(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    observer_override: bool = False,
) -> None:
    active = list_active_batches(conn, project_id)
    if active and not observer_override:
        ids = [item["task_id"] for item in active]
        raise ActiveBatchExistsError(
            f"active batch_migration exists for {project_id}: {ids}"
        )


def record_task_batch_state(
    conn: sqlite3.Connection,
    task_id: str,
    batch_status: str,
    *,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT metadata_json FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise BatchJobError(f"task not found: {task_id}")
    meta = parse_metadata(row["metadata_json"] if hasattr(row, "keys") else row[0])
    meta["batch_status"] = batch_status
    evidence = dict(evidence or {})
    branch_graph = evidence.get("branch_graph")
    worktree_evidence = evidence.get("worktree")
    if not branch_graph and isinstance(worktree_evidence, dict):
        branch_graph = worktree_evidence.get("branch_graph")
    if isinstance(branch_graph, dict):
        meta["branch_graph"] = dict(branch_graph)
    history = meta.get("batch_state_history")
    if not isinstance(history, list):
        history = []
    history.append({
        "status": batch_status,
        "ts": utc_now(),
        "evidence": evidence,
    })
    meta["batch_state_history"] = history
    conn.execute(
        "UPDATE tasks SET metadata_json=?, updated_at=? WHERE task_id=?",
        (json.dumps(meta, ensure_ascii=False, sort_keys=True), utc_now(), task_id),
    )
    return meta


def load_task_metadata(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT metadata_json FROM tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        raise BatchJobError(f"task not found: {task_id}")
    return parse_metadata(row["metadata_json"] if hasattr(row, "keys") else row[0])


def create_batch_task(
    conn: sqlite3.Connection,
    project_id: str,
    prompt: str,
    *,
    repo_root_path: str | Path,
    batch_id: str = "",
    target_branch: str = "main",
    base_commit: str = "",
    created_by: str = "observer-batch",
    metadata: dict[str, Any] | None = None,
    observer_override: bool = False,
) -> dict[str, Any]:
    """Create an observer-driven batch task and store branch strategy metadata."""
    assert_no_active_batch(conn, project_id, observer_override=observer_override)
    batch_id = batch_id or make_batch_id("batch")
    strategy = resolve_branch_strategy(
        job_type=JOB_BATCH_MIGRATION,
        repo_root_path=repo_root_path,
        project_id=project_id,
        target_branch=target_branch,
        base_commit=base_commit,
        batch_id=batch_id,
    )
    meta = normalize_job_metadata(metadata, task_type="task")
    meta.update({
        "job_type": JOB_BATCH_MIGRATION,
        "batch_id": batch_id,
        "batch_status": "created",
        "target_branch": strategy.target_branch,
        "base_commit": strategy.base_commit,
        "work_branch": strategy.work_branch,
        "worktree_path": strategy.worktree_path,
        "worktree_relpath": strategy.worktree_relpath,
        "engine_version": meta.get("engine_version", ""),
        "observer_override": bool(observer_override),
        "project_id": project_id,
        "branch_graph_required": bool(meta.get("branch_graph_required", True)),
        "branch_graph": branch_graph_plan(strategy, status="planned"),
    })
    from . import task_registry

    created = task_registry.create_task(
        conn,
        project_id,
        prompt,
        task_type="task",
        created_by=created_by,
        metadata=meta,
    )
    record_task_batch_state(
        conn,
        created["task_id"],
        "created",
        evidence={"work_branch": strategy.work_branch, "worktree_path": strategy.worktree_path},
    )
    conn.commit()
    return {**created, "metadata": meta, "branch_strategy": strategy.to_metadata()}


def ensure_worktree_path_safe(repo_root_path: str | Path, worktree_path: str | Path) -> Path:
    root = repo_root(repo_root_path)
    allowed_root = (root / ".worktrees").resolve()
    target = Path(worktree_path).resolve()
    if target == allowed_root or allowed_root not in target.parents:
        raise BatchJobError(f"unsafe worktree path outside .worktrees: {target}")
    return target


def branch_exists(repo_root_path: str | Path, branch: str) -> bool:
    root = repo_root(repo_root_path)
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode == 0


def create_worktree(strategy: BranchStrategy, *, repo_root_path: str | Path) -> dict[str, Any]:
    """Create branch/worktree if needed. This is idempotent for existing worktrees."""
    if strategy.direct:
        return {"created": False, "direct": True}
    root = repo_root(repo_root_path)
    worktree = ensure_worktree_path_safe(root, strategy.worktree_path)

    if worktree.exists() and (worktree / ".git").exists():
        branch_graph = initialize_branch_graph(strategy, repo_root_path=repo_root_path)
        return {
            "created": False,
            "branch": strategy.work_branch,
            "worktree_path": str(worktree),
            "reason": "already_exists",
            "branch_graph": branch_graph,
        }

    if not branch_exists(root, strategy.work_branch):
        _git_output(["branch", strategy.work_branch, strategy.base_commit], cwd=root)

    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git_output(["worktree", "add", str(worktree), strategy.work_branch], cwd=root)
    branch_graph = initialize_branch_graph(strategy, repo_root_path=repo_root_path)
    return {
        "created": True,
        "branch": strategy.work_branch,
        "worktree_path": str(worktree),
        "branch_graph": branch_graph,
    }


def abandon_worktree(
    strategy: BranchStrategy,
    *,
    repo_root_path: str | Path,
    remove_branch: bool = False,
) -> dict[str, Any]:
    """Remove a batch worktree after safety validation."""
    if strategy.direct:
        return {"removed": False, "direct": True}
    root = repo_root(repo_root_path)
    worktree = ensure_worktree_path_safe(root, strategy.worktree_path)
    removed = False
    if worktree.exists():
        _git_output(["worktree", "remove", "--force", str(worktree)], cwd=root)
        removed = True
    branch_removed = False
    if remove_branch and branch_exists(root, strategy.work_branch):
        _git_output(["branch", "-D", strategy.work_branch], cwd=root)
        branch_removed = True
    return {
        "removed": removed,
        "branch_removed": branch_removed,
        "worktree_path": str(worktree),
        "branch": strategy.work_branch,
    }


def _strategy_from_metadata(meta: dict[str, Any]) -> BranchStrategy:
    strategy = meta.get("branch_strategy")
    if isinstance(strategy, dict):
        return BranchStrategy(**strategy)
    required = {
        "job_type": meta.get("job_type", JOB_BATCH_MIGRATION),
        "target_branch": meta.get("target_branch", "main"),
        "base_commit": meta.get("base_commit", ""),
        "work_branch": meta.get("work_branch", ""),
        "worktree_path": meta.get("worktree_path", ""),
        "worktree_relpath": meta.get("worktree_relpath", ""),
        "direct": bool(meta.get("direct", False)),
        "merge_policy": meta.get("merge_policy", "merge_gatekeeper"),
        "project_id": meta.get("project_id", ""),
    }
    return BranchStrategy(**required)


def _normalize_relpath(path: str) -> str:
    rel = str(path or "").replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel


def _graph_referenced_files(graph_doc: Any) -> set[str]:
    files: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, str):
            rel = _normalize_relpath(value)
            if rel:
                files.add(rel)
        elif isinstance(value, list):
            for item in value:
                add(item)

    nodes: list[Any] = []
    if isinstance(graph_doc, dict):
        raw_nodes = graph_doc.get("nodes")
        if isinstance(raw_nodes, dict):
            nodes.extend(raw_nodes.values())
        elif isinstance(raw_nodes, list):
            nodes.extend(raw_nodes)
        deps_graph = graph_doc.get("deps_graph")
        deps_nodes = deps_graph.get("nodes") if isinstance(deps_graph, dict) else []
        if isinstance(deps_nodes, dict):
            nodes.extend(deps_nodes.values())
        elif isinstance(deps_nodes, list):
            nodes.extend(deps_nodes)

    for node in nodes:
        if not isinstance(node, dict):
            continue
        for key in ("primary", "primary_file", "secondary", "test", "tests", "test_files"):
            add(node.get(key))
    return files


def _overlay_covered_files(overlay_doc: Any) -> set[str]:
    files: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, str):
            rel = _normalize_relpath(value)
            if rel:
                files.add(rel)
        elif isinstance(value, list):
            for item in value:
                add(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                add(key)
                add(item)

    if isinstance(overlay_doc, dict):
        for key in ("covered_files", "changed_files", "target_files"):
            add(overlay_doc.get(key))
        add(overlay_doc.get("file_states"))
        graph_delta = overlay_doc.get("graph_delta")
        if isinstance(graph_delta, dict):
            for key in ("covered_files", "changed_files", "target_files"):
                add(graph_delta.get(key))
    return files


def _changed_files_since_base(root: Path, base_commit: str, work_branch: str) -> list[str]:
    if not base_commit or not work_branch:
        return []
    out = _git_output(["diff", "--name-only", f"{base_commit}..{work_branch}"], cwd=root)
    return [
        rel for rel in (_normalize_relpath(line.strip()) for line in out.splitlines())
        if rel and not rel.startswith(".aming-claw/cache/")
    ]


def validate_branch_graph_gate(
    *,
    repo_root_path: str | Path,
    strategy: BranchStrategy,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Validate branch-local graph state before a batch merge."""
    required = bool(metadata.get("branch_graph_required", True))
    if not required:
        return {"status": "skipped", "required": False, "reason": "branch_graph_required=false"}

    root = repo_root(repo_root_path)
    branch_graph = metadata.get("branch_graph")
    if not isinstance(branch_graph, dict) or branch_graph.get("status") != "ready":
        return {"status": "fail", "required": True, "reason": "branch graph snapshot is not ready"}

    snapshot_path = Path(str(branch_graph.get("snapshot_path") or ""))
    overlay_path = Path(str(branch_graph.get("overlay_path") or ""))
    manifest_path = Path(str(branch_graph.get("manifest_path") or ""))
    missing = [str(p) for p in (snapshot_path, overlay_path, manifest_path) if not p.exists()]
    if missing:
        return {"status": "fail", "required": True, "reason": "branch graph artifact missing", "missing": missing}

    snapshot_bytes = snapshot_path.read_bytes()
    snapshot_sha = _sha256_bytes(snapshot_bytes)
    expected_sha = str(branch_graph.get("base_graph_sha256") or "")
    if expected_sha and snapshot_sha != expected_sha:
        return {
            "status": "fail",
            "required": True,
            "reason": "branch graph snapshot hash mismatch",
            "expected": expected_sha,
            "actual": snapshot_sha,
        }

    source_path_raw = str(branch_graph.get("source_graph_path") or "")
    source_path = Path(source_path_raw) if source_path_raw else None
    if branch_graph.get("source_graph_exists") and source_path and source_path.exists():
        current_source_sha = _sha256_bytes(source_path.read_bytes())
        if expected_sha and current_source_sha != expected_sha:
            return {
                "status": "fail",
                "required": True,
                "reason": "target graph changed since branch snapshot",
                "expected": expected_sha,
                "actual": current_source_sha,
                "source_graph_path": str(source_path),
            }

    changed_files = _changed_files_since_base(root, strategy.base_commit, strategy.work_branch)
    graph_doc = _json_load(snapshot_path, {})
    overlay_doc = _json_load(overlay_path, {})
    known_files = _graph_referenced_files(graph_doc)
    overlay_files = _overlay_covered_files(overlay_doc)
    uncovered = [
        path for path in changed_files
        if path not in known_files and path not in overlay_files
    ]
    return {
        "status": "pass" if not uncovered else "fail",
        "required": True,
        "base_graph_sha256": expected_sha or snapshot_sha,
        "snapshot_path": str(snapshot_path),
        "overlay_path": str(overlay_path),
        "manifest_path": str(manifest_path),
        "source_graph_path": str(source_path or ""),
        "changed_files": changed_files,
        "known_changed_files": [path for path in changed_files if path in known_files],
        "overlay_covered_files": [path for path in changed_files if path in overlay_files],
        "uncovered_changed_files": uncovered,
        "coverage_status": "covered" if not uncovered else "uncovered",
    }


def batch_merge_plan(conn: sqlite3.Connection, task_id: str, *, repo_root_path: str | Path) -> dict[str, Any]:
    """Return a merge plan without mutating git."""
    meta = load_task_metadata(conn, task_id)
    if meta.get("job_type") != JOB_BATCH_MIGRATION:
        raise BatchJobError(f"task {task_id} is not a batch_migration job")
    strategy = _strategy_from_metadata(meta)
    root = repo_root(repo_root_path)
    if strategy.direct:
        raise BatchJobError("batch merge requires a branch/worktree strategy")
    graph_gate = validate_branch_graph_gate(
        repo_root_path=root,
        strategy=strategy,
        metadata=meta,
    )
    if graph_gate.get("status") == "fail":
        raise BatchJobError(
            f"branch graph gate failed: {graph_gate.get('reason') or graph_gate.get('coverage_status')}"
        )
    return {
        "task_id": task_id,
        "job_type": meta.get("job_type"),
        "target_branch": strategy.target_branch,
        "work_branch": strategy.work_branch,
        "worktree_path": strategy.worktree_path,
        "merge_policy": strategy.merge_policy,
        "repo_root": str(root),
        "graph_gate": graph_gate,
    }


def merge_batch_branch(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    repo_root_path: str | Path,
    message: str = "batch migration merge",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Merge a batch branch through the chain trailer helper or return a dry-run plan.

    This is a small adapter around the existing merge helper. It does not invent
    a second invisible merge path; callers can keep dry_run=True until review.
    """
    plan = batch_merge_plan(conn, task_id, repo_root_path=repo_root_path)
    if dry_run:
        meta = record_task_batch_state(conn, task_id, "ready_for_review", evidence=plan)
        conn.commit()
        return {"dry_run": True, "merge_plan": plan, "metadata": meta}

    root = repo_root(repo_root_path)
    current = _git_output(["branch", "--show-current"], cwd=root, check=False)
    if current != plan["target_branch"]:
        _git_output(["checkout", plan["target_branch"]], cwd=root)

    from .chain_trailer import write_merge_with_trailer

    meta = load_task_metadata(conn, task_id)
    ok, commit_hash, error = write_merge_with_trailer(
        message,
        branch=plan["work_branch"],
        cwd=str(root),
        task_id=task_id,
        parent_chain_sha=str(meta.get("base_commit") or ""),
        bug_id=str(meta.get("bug_id") or "none"),
    )
    if not ok:
        record_task_batch_state(conn, task_id, "failed", evidence={"error": error, **plan})
        conn.commit()
        raise BatchJobError(error)

    merged_meta = record_task_batch_state(
        conn,
        task_id,
        "merged",
        evidence={"merge_commit": commit_hash, **plan},
    )
    merged_meta["merge_commit"] = commit_hash
    conn.execute(
        "UPDATE tasks SET metadata_json=?, updated_at=? WHERE task_id=?",
        (json.dumps(merged_meta, ensure_ascii=False, sort_keys=True), utc_now(), task_id),
    )
    conn.commit()
    return {
        "dry_run": False,
        "merge_commit": commit_hash,
        "merge_plan": plan,
        "metadata": merged_meta,
    }


def mark_batch_redeployed(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    redeploy_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = record_task_batch_state(
        conn,
        task_id,
        "redeployed",
        evidence={"redeploy_result": dict(redeploy_result or {})},
    )
    conn.commit()
    return meta


def report_stale_worktrees(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    repo_root_path: str | Path,
) -> dict[str, Any]:
    """Report .worktrees entries not referenced by active batch metadata."""
    root = repo_root(repo_root_path)
    worktrees_dir = root / ".worktrees"
    active_paths = {
        str(Path(item["metadata"].get("worktree_path", "")).resolve())
        for item in list_active_batches(conn, project_id)
        if item["metadata"].get("worktree_path")
    }
    if not worktrees_dir.exists():
        return {"stale_count": 0, "stale_worktrees": []}
    stale = []
    for child in sorted(worktrees_dir.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        resolved = str(child.resolve())
        if resolved not in active_paths:
            stale.append(str(child))
    return {"stale_count": len(stale), "stale_worktrees": stale}
