"""Pre-flight self-check for governance auto-chain.

Validates system, version, graph, coverage, and queue health BEFORE
chain execution. Each check is independent — errors in one don't block others.

Usage:
    from .preflight import run_preflight
    report = run_preflight(conn, "aming-claw")
"""

from __future__ import annotations

import json
import logging
import os
import glob as _glob
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Check result helpers
# ---------------------------------------------------------------------------

def _pass(details=None):
    return {"status": "pass", "details": details or {}}

def _warn(details=None):
    return {"status": "warn", "details": details or {}}

def _fail(details=None):
    return {"status": "fail", "details": details or {}}


# ---------------------------------------------------------------------------
# 1. System check — DB accessible, required tables exist
# ---------------------------------------------------------------------------

_REQUIRED_TABLES = [
    "node_state", "node_history", "tasks", "task_attempts",
    "project_version", "sessions", "schema_meta",
]

def check_system(conn) -> dict:
    """Verify database is accessible and has required tables."""
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        existing = {r["name"] if hasattr(r, "keys") else r[0] for r in rows}
        missing = [t for t in _REQUIRED_TABLES if t not in existing]
        if missing:
            return _fail({"missing_tables": missing})
        return _pass({"table_count": len(existing)})
    except Exception as e:
        return _fail({"error": str(e)})


# ---------------------------------------------------------------------------
# 2. Version check — chain_version == git_head, freshness
# ---------------------------------------------------------------------------

def _prefix_match(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return left.startswith(right) or right.startswith(left)


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git_head_short(repo_root_path: str | os.PathLike | None = None) -> str:
    try:
        import subprocess

        repo_root = Path(repo_root_path).resolve() if repo_root_path else _default_repo_root()
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


def _chain_state_from_git(repo_root_path: str | os.PathLike | None = None) -> dict | None:
    try:
        from .chain_trailer import get_chain_state

        root = str(Path(repo_root_path).resolve()) if repo_root_path else None
        return get_chain_state(cwd=root)
    except Exception as exc:
        log.debug("preflight version: chain_trailer unavailable: %s", exc)
        return None


def _check_version_db_legacy(row) -> dict:
    chain_ver = row["chain_version"] if hasattr(row, "keys") else row[0]
    git_head = row["git_head"] if hasattr(row, "keys") else row[1]
    synced_at = row["git_synced_at"] if hasattr(row, "keys") else row[2]
    dirty = row["dirty_files"] if hasattr(row, "keys") else row[3]

    issues = {}
    status = "pass"

    if git_head and chain_ver and not _prefix_match(git_head, chain_ver):
        issues["version_mismatch"] = {
            "chain_version": chain_ver, "git_head": git_head
        }
        status = "fail"

    if dirty:
        dirty_list = json.loads(dirty) if isinstance(dirty, str) else dirty
        if dirty_list:
            issues["dirty_files"] = dirty_list
            if status != "fail":
                status = "warn"

    if synced_at:
        try:
            synced_dt = datetime.fromisoformat(synced_at.replace("Z", "+00:00"))
            age = datetime.now(timezone.utc) - synced_dt
            if age > timedelta(minutes=5):
                issues["sync_stale_seconds"] = int(age.total_seconds())
                if status != "fail":
                    status = "warn"
        except (ValueError, TypeError):
            pass

    if status == "pass":
        return _pass({"chain_version": chain_ver, "source": "db"})
    if status == "warn":
        return _warn(issues)
    return _fail(issues)


def check_version(
    conn,
    project_id: str,
    *,
    prefer_trailer: bool | None = None,
    project_root: str | os.PathLike | None = None,
) -> dict:
    """Verify chain state against git HEAD.

    The auto-chain version gate treats git trailers as the effective source of
    truth.  Preflight mirrors that behavior by default and keeps the legacy DB
    comparison available for focused tests/fallback.
    """
    try:
        row = conn.execute(
            "SELECT chain_version, git_head, git_synced_at, dirty_files "
            "FROM project_version WHERE project_id=?",
            (project_id,)
        ).fetchone()

        if prefer_trailer is None:
            prefer_trailer = (
                os.environ.get("OPT_PREFLIGHT_VERSION_SOURCE", "trailer").strip().lower()
                != "db"
            )

        if prefer_trailer:
            trailer_state = (
                _chain_state_from_git(project_root)
                if project_root
                else _chain_state_from_git()
            )
            if trailer_state:
                chain_ver = (
                    trailer_state.get("chain_sha")
                    or trailer_state.get("version")
                    or ""
                )
                git_head = _git_head_short(project_root) if project_root else _git_head_short()
                source = trailer_state.get("source", "trailer")
                issues = {}
                if chain_ver and git_head and not _prefix_match(git_head, chain_ver):
                    issues["version_mismatch"] = {
                        "chain_version": chain_ver,
                        "git_head": git_head,
                        "source": source,
                    }
                    if project_root:
                        issues["version_mismatch"]["project_root"] = str(Path(project_root).resolve())
                    return _fail(issues)

                dirty_files = trailer_state.get("dirty_files") or []
                if dirty_files:
                    details = {
                        "dirty_files": dirty_files,
                        "chain_version": chain_ver,
                        "git_head": git_head,
                        "source": source,
                    }
                    if project_root:
                        details["project_root"] = str(Path(project_root).resolve())
                    return _warn(details)

                details = {
                    "chain_version": chain_ver,
                    "git_head": git_head,
                    "source": source,
                }
                if project_root:
                    details["project_root"] = str(Path(project_root).resolve())
                if row:
                    legacy_chain = row["chain_version"] if hasattr(row, "keys") else row[0]
                    legacy_head = row["git_head"] if hasattr(row, "keys") else row[1]
                    synced_at = row["git_synced_at"] if hasattr(row, "keys") else row[2]
                    if legacy_chain and chain_ver and not _prefix_match(legacy_chain, chain_ver):
                        details["legacy_chain_version"] = legacy_chain
                    if legacy_head and git_head and not _prefix_match(legacy_head, git_head):
                        details["legacy_git_head"] = legacy_head
                    if synced_at:
                        details["legacy_git_synced_at"] = synced_at
                return _pass(details)

        if not row:
            return _fail({"error": "no project_version row"})

        return _check_version_db_legacy(row)
    except Exception as e:
        return _fail({"error": str(e)})


# ---------------------------------------------------------------------------
# 3. Graph check — orphan nodes, pending without active tasks
# ---------------------------------------------------------------------------

def _graph_governance_preflight_details(conn, project_id: str) -> dict:
    """Return graph_status-compatible details used for the graph decision."""
    from . import graph_snapshot_store as store
    from . import server as governance_server

    status = store.graph_governance_status(conn, project_id)
    pending_rows = list(status.get("pending_scope_reconcile") or [])
    _operation, graph_stale = governance_server._graph_stale_scope_operation(
        project_id,
        status=status,
        pending_rows=pending_rows,
    )
    active_graph_commit = str(
        graph_stale.get("active_graph_commit")
        or status.get("graph_snapshot_commit")
        or ""
    )
    target_head = str(graph_stale.get("head_commit") or "")
    is_stale = bool(graph_stale.get("is_stale"))
    pending_count = int(status.get("pending_scope_reconcile_count") or len(pending_rows))
    graph_state = "stale" if is_stale else "current"
    if not target_head:
        graph_state = "unknown"
    return {
        "active_snapshot_id": str(status.get("active_snapshot_id") or ""),
        "active_graph_commit": active_graph_commit,
        "target_head": target_head,
        "graph_stale": is_stale,
        "graph_state": graph_state,
        "pending_scope_reconcile_count": pending_count,
    }


def check_graph(conn, project_id: str) -> dict:
    """Check for orphan/stuck acceptance nodes and stale graph reconcile state."""
    try:
        graph_details = _graph_governance_preflight_details(conn, project_id)

        # Pending nodes
        pending = conn.execute(
            "SELECT node_id FROM node_state WHERE project_id=? AND verify_status='pending'",
            (project_id,)
        ).fetchall()
        pending_ids = [r["node_id"] if hasattr(r, "keys") else r[0] for r in pending]

        # Active tasks (queued or claimed)
        active = conn.execute(
            "SELECT task_id, metadata_json FROM tasks "
            "WHERE project_id=? AND status IN ('queued', 'claimed')",
            (project_id,)
        ).fetchall()

        # Check if pending nodes have active tasks targeting them
        active_nodes = set()
        for t in active:
            meta = t["metadata_json"] if hasattr(t, "keys") else t[-1]
            if meta:
                try:
                    m = json.loads(meta)
                    for n in m.get("related_nodes", []):
                        active_nodes.add(n)
                except (json.JSONDecodeError, TypeError):
                    pass

        orphan_pending = [n for n in pending_ids if n not in active_nodes]

        details = {
            "pending_count": len(pending_ids),
            **graph_details,
        }
        if graph_details.get("graph_stale"):
            details["reason"] = "active_graph_stale"
            return _fail(details)
        if graph_details.get("pending_scope_reconcile_count", 0) > 0:
            details["reason"] = "pending_scope_reconcile"
            return _warn(details)
        if not pending_ids:
            return _pass(details)
        elif orphan_pending:
            details.update({
                "orphan_pending": orphan_pending,
            })
            return _warn(details)
        else:
            details["all_have_active_tasks"] = True
            return _pass(details)
    except Exception as e:
        return _fail({"error": str(e)})


# ---------------------------------------------------------------------------
# 4. Coverage check — CODE_DOC_MAP completeness
# ---------------------------------------------------------------------------

def _is_internal_project_root(project_root: str | os.PathLike | None) -> bool:
    if not project_root:
        return True
    try:
        return Path(project_root).resolve() == _default_repo_root()
    except Exception:
        return False


def _target_python_files(project_root: Path) -> list[str]:
    noise_dirs = {
        ".git",
        ".hg",
        ".svn",
        ".worktrees",
        ".claude",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "build",
        "dist",
        "node_modules",
        "runtime",
        "shared-volume",
        ".aming-claw/cache",
        ".aming-claw/sessions",
        ".aming-claw/baselines",
        ".aming-claw/logs",
    }
    files: list[str] = []
    for path in sorted(project_root.rglob("*.py")):
        try:
            rel = path.relative_to(project_root).as_posix()
        except ValueError:
            continue
        parts = rel.split("/")
        if any("/".join(parts[:idx]) in noise_dirs for idx in range(1, len(parts) + 1)):
            continue
        if path.name == "__init__.py" or path.name.startswith("test_"):
            continue
        files.append(rel)
    return files


def check_coverage(project_id: str = None, project_root: str | os.PathLike | None = None) -> dict:
    """Scan governance/*.py and agent/*.py for CODE_DOC_MAP gaps.

    Uses project-specific code_doc_map.json when *project_id* is given,
    falling back to the hardcoded CODE_DOC_MAP otherwise (R3 / AC3).
    """
    try:
        from .impact_analyzer import _load_project_code_doc_map

        root = Path(project_root).resolve() if project_root else None
        is_internal = _is_internal_project_root(root)
        code_doc_map = _load_project_code_doc_map(
            project_id,
            project_root=root,
            fallback_to_builtin=is_internal,
        )
        if not code_doc_map and not is_internal:
            return _pass({
                "skipped": True,
                "reason": "no_code_doc_map_for_external_project",
                "project_root": str(root) if root else "",
            })

        if root and not is_internal:
            scan_files = _target_python_files(root)
            if not scan_files:
                return _pass({
                    "skipped": True,
                    "reason": "no_target_python_files",
                    "project_root": str(root),
                })
        else:
            # Find the agent directory
            agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            gov_dir = os.path.join(agent_dir, "governance")

            # Noise directories to exclude from unmapped scan (R4)
            _NOISE_DIRS = {
                "docs/dev/scratch", ".worktrees", ".claude/worktrees",
                "node_modules", "runtime", ".venv", "shared-volume/codex-tasks",
            }

            # Collect all .py files that should be mapped
            scan_files = []

            # governance/*.py (exclude __init__, __pycache__, tests)
            for f in sorted(os.listdir(gov_dir)):
                if f.endswith(".py") and f != "__init__.py" and not f.startswith("test_"):
                    rel = f"agent/governance/{f}"
                    scan_files.append(rel)

            # agent/*.py (key files only, not all)
            for f in sorted(os.listdir(agent_dir)):
                if f.endswith(".py") and f != "__init__.py" and not f.startswith("test_"):
                    rel = f"agent/{f}"
                    scan_files.append(rel)

        # Check which files are covered by code_doc_map
        mapped_patterns = set(code_doc_map.keys())
        unmapped = []
        for sf in scan_files:
            # Skip noise directories (R4)
            if is_internal and any(sf.startswith(nd) for nd in _NOISE_DIRS):
                continue
            covered = False
            for pattern in mapped_patterns:
                if pattern in sf or sf == pattern:
                    covered = True
                    break
            if not covered:
                unmapped.append(sf)

        if not unmapped:
            details = {"mapped_files": len(scan_files)}
            if root:
                details["project_root"] = str(root)
            return _pass(details)
        else:
            details = {
                "mapped_files": len(scan_files) - len(unmapped),
                "total_files": len(scan_files),
                "unmapped_files": unmapped,
            }
            if root:
                details["project_root"] = str(root)
            return _warn(details)
    except Exception as e:
        return _fail({"error": str(e)})


# ---------------------------------------------------------------------------
# 5. Queue check — stuck tasks, circular retries
# ---------------------------------------------------------------------------

_STUCK_THRESHOLD_SECONDS = 1800  # 30 minutes

def check_queue(conn, project_id: str) -> dict:
    """Check for stuck claimed tasks and circular retry patterns."""
    try:
        now = datetime.now(timezone.utc)

        # Claimed tasks older than threshold
        claimed = conn.execute(
            "SELECT task_id, type, updated_at, attempt_count, parent_task_id "
            "FROM tasks WHERE project_id=? AND status='claimed'",
            (project_id,)
        ).fetchall()

        stuck = []
        for t in claimed:
            updated = t["updated_at"] if hasattr(t, "keys") else t[2]
            try:
                updated_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                age = (now - updated_dt).total_seconds()
                if age > _STUCK_THRESHOLD_SECONDS:
                    stuck.append({
                        "task_id": t["task_id"] if hasattr(t, "keys") else t[0],
                        "type": t["type"] if hasattr(t, "keys") else t[1],
                        "stuck_seconds": int(age),
                    })
            except (ValueError, TypeError):
                pass

        # Circular retries: tasks with attempt_count >= max_attempts that spawned children
        circular = conn.execute(
            "SELECT task_id, type, attempt_count FROM tasks "
            "WHERE project_id=? AND status='failed' AND attempt_count >= 3 "
            "AND task_id IN (SELECT DISTINCT parent_task_id FROM tasks WHERE parent_task_id IS NOT NULL)",
            (project_id,)
        ).fetchall()
        circular_ids = [
            (t["task_id"] if hasattr(t, "keys") else t[0])
            for t in circular
        ]

        issues = {}
        status = "pass"

        if stuck:
            issues["stuck_tasks"] = stuck
            status = "warn"
        if circular_ids:
            issues["circular_retry_roots"] = circular_ids
            if status != "fail":
                status = "warn"

        if status == "pass":
            queued_count = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE project_id=? AND status='queued'",
                (project_id,)
            ).fetchone()
            qc = queued_count[0] if queued_count else 0
            return _pass({"queued": qc, "claimed": len(claimed)})
        elif status == "warn":
            return _warn(issues)
        else:
            return _fail(issues)
    except Exception as e:
        return _fail({"error": str(e)})


# ---------------------------------------------------------------------------
# 6. Batch worktree check — stale batch worktrees are reported, never deleted
# ---------------------------------------------------------------------------

def check_batch_worktrees(
    conn,
    project_id: str,
    project_root: str | os.PathLike | None = None,
) -> dict:
    """Report stale .worktrees entries not referenced by active batch metadata."""
    try:
        from .batch_jobs import report_stale_worktrees

        root = Path(project_root).resolve() if project_root else Path(".").resolve()
        details = report_stale_worktrees(conn, project_id, repo_root_path=root)
        details.setdefault("project_root", str(root))
        if details.get("stale_count"):
            return _warn(details)
        return _pass(details)
    except Exception as e:
        return _fail({"error": str(e)})


# ---------------------------------------------------------------------------
# 7. Bootstrap check — graph has nodes, node_state populated, version exists
# ---------------------------------------------------------------------------

def check_bootstrap(conn, project_id: str) -> dict:
    """Verify bootstrap generated valid state (R9).

    Checks:
      1. Graph has at least one node (node_state rows exist)
      2. node_state table is populated for this project
      3. project_version record exists

    Returns: {"status": "pass"|"fail", "details": {...}}
    """
    try:
        details = {}
        failures = []

        # Check node_state has rows
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM node_state WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        node_count = row["cnt"] if hasattr(row, "keys") else row[0]
        details["node_count"] = node_count
        if node_count == 0:
            failures.append("no nodes in node_state")

        # Check project_version exists
        ver_row = conn.execute(
            "SELECT chain_version, updated_at FROM project_version WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if ver_row:
            details["chain_version"] = ver_row["chain_version"] if hasattr(ver_row, "keys") else ver_row[0]
            details["version_exists"] = True
        else:
            details["version_exists"] = False
            failures.append("no project_version record")

        if failures:
            details["failures"] = failures
            return _fail(details)
        return _pass(details)
    except Exception as e:
        return _fail({"error": str(e)})


# ---------------------------------------------------------------------------
# 8. Plugin update state check — local state only, no network
# ---------------------------------------------------------------------------

def check_plugin_update_state(state_path: str | None = None) -> dict:
    """Check local plugin update/restart obligations without contacting Git remotes."""
    try:
        try:
            from agent.plugin_installer import plugin_update_state_status
        except ImportError:
            from plugin_installer import plugin_update_state_status  # type: ignore

        status = plugin_update_state_status(state_path=state_path)
        details = {
            "state_path": status.get("state_path", ""),
            "state_exists": status.get("state_exists", False),
            "update_status": status.get("update_status", "unknown"),
            "blockers": status.get("blockers", []),
            "warnings": status.get("warnings", []),
            "state": status.get("state", {}),
            "self_graph_bundle": status.get("self_graph_bundle", {}),
        }
        if status.get("status") == "fail":
            return _fail(details)
        if status.get("status") == "warn":
            return _warn(details)
        return _pass(details)
    except Exception as e:
        return _warn({"error": str(e), "update_status": "unknown"})


# ---------------------------------------------------------------------------
# 9. Governance hint check — source hints must be committed before reconcile
# ---------------------------------------------------------------------------

def _git_repo_root(repo_root_path: str | os.PathLike | None = None) -> Path | None:
    """Return the Git root for a path, or None when the path is not in Git."""
    try:
        import subprocess

        start = Path(repo_root_path or os.getcwd()).resolve()
        cwd = start if start.is_dir() else start.parent
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return None
        root = proc.stdout.strip()
        return Path(root).resolve() if root else None
    except Exception:
        return None


def _dirty_tracked_files(repo_root: Path) -> tuple[list[dict], str | None]:
    """Return tracked dirty files from porcelain status output."""
    try:
        import subprocess

        proc = subprocess.run(
            ["git", "status", "--porcelain=1", "-z", "--untracked-files=no"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0:
            return [], proc.stderr.strip() or "git status failed"

        files = []
        parts = proc.stdout.split("\0")
        idx = 0
        while idx < len(parts):
            item = parts[idx]
            idx += 1
            if not item:
                continue
            status = item[:2]
            rel_path = item[3:]
            if not rel_path:
                continue
            if status[0] in {"R", "C"} and idx < len(parts):
                # Porcelain -z includes the source path as the next field for
                # renames/copies.  The current path is the one that can carry
                # a newly written governance hint.
                idx += 1
            files.append({"path": rel_path, "status": status})
        return files, None
    except Exception as exc:
        return [], str(exc)


def check_pending_governance_hints(repo_root_path: str | os.PathLike | None = None) -> dict:
    """Warn when source-controlled governance-hint comments are dirty.

    Governance Hint writes source comments that only become durable graph input
    after commit + reconcile.  A dirty tracked file containing such a hint is a
    pending operator action, not just ordinary workspace noise.
    """
    root = _git_repo_root(repo_root_path)
    empty = {"pending_count": 0, "pending_governance_hints": []}
    if root is None:
        return _pass({**empty, "skipped": True, "reason": "not_git_repo"})

    dirty_files, error = _dirty_tracked_files(root)
    if error:
        return _warn({**empty, "error": error})

    pending = []
    for item in dirty_files:
        status = item.get("status", "")
        rel_path = item.get("path", "")
        if "D" in status or not rel_path:
            continue
        path = root / rel_path
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "governance-hint" in text:
            pending.append({"path": rel_path, "status": status.strip() or status})

    if not pending:
        return _pass(empty)

    return _warn({
        "pending_count": len(pending),
        "pending_governance_hints": pending,
        "recommended_action": (
            "commit the governance hint file(s) and run Update Graph/reconcile, "
            "or intentionally revert the hint before continuing"
        ),
    })


# ---------------------------------------------------------------------------
# Auto-fix actions
# ---------------------------------------------------------------------------

def _auto_fix_graph(conn, project_id: str, orphan_nodes: list) -> list:
    """Waive orphan pending nodes."""
    fixed = []
    now = datetime.now(timezone.utc).isoformat()
    for node_id in orphan_nodes:
        try:
            conn.execute(
                "UPDATE node_state SET verify_status='waived', updated_by='preflight-autofix', "
                "updated_at=?, evidence_json=? WHERE project_id=? AND node_id=? AND verify_status='pending'",
                (now, json.dumps({"type": "manual_review", "reason": "preflight auto-fix: orphan pending node"}),
                 project_id, node_id)
            )
            conn.execute(
                "INSERT INTO node_history (project_id, node_id, from_status, to_status, role, "
                "evidence_json, session_id, ts, version) VALUES (?, ?, 'pending', 'waived', 'preflight', ?, ?, ?, 1)",
                (project_id, node_id,
                 json.dumps({"type": "manual_review", "reason": "preflight auto-fix"}),
                 "preflight", now)
            )
            fixed.append(f"waived orphan node {node_id}")
        except Exception as e:
            log.warning("preflight auto-fix failed for node %s: %s", node_id, e)
    if fixed:
        conn.commit()
    return fixed


def _auto_fix_queue(conn, project_id: str, stuck_tasks: list) -> list:
    """Mark stuck claimed tasks as failed."""
    fixed = []
    now = datetime.now(timezone.utc).isoformat()
    for task in stuck_tasks:
        tid = task["task_id"]
        try:
            conn.execute(
                "UPDATE tasks SET status='failed', updated_at=?, error_message=? "
                "WHERE task_id=? AND status='claimed'",
                (now, "preflight auto-fix: stuck >30min", tid)
            )
            fixed.append(f"failed stuck task {tid}")
        except Exception as e:
            log.warning("preflight auto-fix failed for task %s: %s", tid, e)
    if fixed:
        conn.commit()
    return fixed


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_preflight(
    conn,
    project_id: str,
    auto_fix: bool = False,
    project_root: str | os.PathLike | None = None,
) -> dict:
    """Run all pre-flight checks.

    Args:
        conn: SQLite connection for the project
        project_id: Project identifier
        auto_fix: If True, attempt to fix recoverable issues

    Returns:
        PreflightReport dict with ok, checks, blockers, warnings, auto_fixed
    """
    checks = {}
    warnings = []
    blockers = []
    auto_fixed = []
    resolved_root = None
    try:
        from . import project_service

        resolved_root = project_service.resolve_project_root(
            project_id,
            project_root,
            fallback_self=True,
        )
    except Exception as exc:
        log.debug("preflight: project root unavailable for %s: %s", project_id, exc)

    # Run all checks independently
    checks["system"] = check_system(conn)
    checks["version"] = check_version(conn, project_id, project_root=resolved_root)
    checks["graph"] = check_graph(conn, project_id)
    checks["coverage"] = check_coverage(project_id, project_root=resolved_root)
    checks["queue"] = check_queue(conn, project_id)
    checks["batch_worktrees"] = check_batch_worktrees(conn, project_id, project_root=resolved_root)
    checks["plugin_update_state"] = check_plugin_update_state()
    checks["pending_governance_hints"] = check_pending_governance_hints(resolved_root)

    # Collect warnings and blockers
    for name, result in checks.items():
        if result["status"] == "warn":
            details = result.get("details", {})
            if name == "graph" and details.get("orphan_pending"):
                warnings.append(f"{len(details['orphan_pending'])} orphan pending node(s)")
            elif name == "coverage" and details.get("unmapped_files"):
                warnings.append(f"{len(details['unmapped_files'])} unmapped file(s) in CODE_DOC_MAP")
            elif name == "queue" and details.get("stuck_tasks"):
                warnings.append(f"{len(details['stuck_tasks'])} stuck task(s)")
            elif name == "batch_worktrees" and details.get("stale_worktrees"):
                warnings.append(f"{len(details['stale_worktrees'])} stale batch worktree(s)")
            elif name == "version":
                if details.get("sync_stale_seconds"):
                    warnings.append(f"version sync stale ({details['sync_stale_seconds']}s)")
                if details.get("dirty_files"):
                    warnings.append(f"{len(details['dirty_files'])} dirty file(s)")
            elif name == "plugin_update_state":
                warnings.extend(
                    f"plugin update state: {item}"
                    for item in details.get("warnings", [])
                )
            elif name == "pending_governance_hints" and details.get("pending_count"):
                warnings.append(
                    f"{details['pending_count']} uncommitted governance hint file(s)"
                )
            else:
                warnings.append(f"{name}: {result['status']}")
        elif result["status"] == "fail":
            blockers.append(f"{name}: {json.dumps(result.get('details', {}))}")

    # Auto-fix if requested
    if auto_fix:
        graph_details = checks["graph"].get("details", {})
        if graph_details.get("orphan_pending"):
            fixes = _auto_fix_graph(conn, project_id, graph_details["orphan_pending"])
            auto_fixed.extend(fixes)
            if fixes:
                checks["graph"] = check_graph(conn, project_id)

        queue_details = checks["queue"].get("details", {})
        if queue_details.get("stuck_tasks"):
            fixes = _auto_fix_queue(conn, project_id, queue_details["stuck_tasks"])
            auto_fixed.extend(fixes)
            if fixes:
                checks["queue"] = check_queue(conn, project_id)

    ok = all(c["status"] != "fail" for c in checks.values())

    return {
        "ok": ok,
        "project_id": project_id,
        "project_root": str(resolved_root) if resolved_root else "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "auto_fixed": auto_fixed,
    }
