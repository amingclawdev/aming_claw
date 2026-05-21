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


def _git_head_short() -> str:
    try:
        import subprocess

        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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


def _chain_state_from_git() -> dict | None:
    try:
        from .chain_trailer import get_chain_state

        return get_chain_state()
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


def check_version(conn, project_id: str, *, prefer_trailer: bool | None = None) -> dict:
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
            trailer_state = _chain_state_from_git()
            if trailer_state:
                chain_ver = (
                    trailer_state.get("chain_sha")
                    or trailer_state.get("version")
                    or ""
                )
                git_head = _git_head_short()
                source = trailer_state.get("source", "trailer")
                issues = {}
                if chain_ver and git_head and not _prefix_match(git_head, chain_ver):
                    issues["version_mismatch"] = {
                        "chain_version": chain_ver,
                        "git_head": git_head,
                        "source": source,
                    }
                    return _fail(issues)

                dirty_files = trailer_state.get("dirty_files") or []
                if dirty_files:
                    return _warn({
                        "dirty_files": dirty_files,
                        "chain_version": chain_ver,
                        "git_head": git_head,
                        "source": source,
                    })

                details = {
                    "chain_version": chain_ver,
                    "git_head": git_head,
                    "source": source,
                }
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

def check_graph(conn, project_id: str) -> dict:
    """Check for orphan or stuck nodes in the acceptance graph."""
    try:
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

        if not pending_ids:
            return _pass({"pending_count": 0})
        elif orphan_pending:
            return _warn({
                "pending_count": len(pending_ids),
                "orphan_pending": orphan_pending,
            })
        else:
            return _pass({"pending_count": len(pending_ids), "all_have_active_tasks": True})
    except Exception as e:
        return _fail({"error": str(e)})


# ---------------------------------------------------------------------------
# 4. Coverage check — CODE_DOC_MAP completeness
# ---------------------------------------------------------------------------

def check_coverage(project_id: str = None) -> dict:
    """Scan governance/*.py and agent/*.py for CODE_DOC_MAP gaps.

    Uses project-specific code_doc_map.json when *project_id* is given,
    falling back to the hardcoded CODE_DOC_MAP otherwise (R3 / AC3).
    """
    try:
        from .impact_analyzer import _load_project_code_doc_map

        code_doc_map = _load_project_code_doc_map(project_id)

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
            if any(sf.startswith(nd) for nd in _NOISE_DIRS):
                continue
            covered = False
            for pattern in mapped_patterns:
                if pattern in sf or sf == pattern:
                    covered = True
                    break
            if not covered:
                unmapped.append(sf)

        if not unmapped:
            return _pass({"mapped_files": len(scan_files)})
        else:
            return _warn({
                "mapped_files": len(scan_files) - len(unmapped),
                "total_files": len(scan_files),
                "unmapped_files": unmapped,
            })
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

def check_batch_worktrees(conn, project_id: str) -> dict:
    """Report stale .worktrees entries not referenced by active batch metadata."""
    try:
        from .batch_jobs import report_stale_worktrees

        details = report_stale_worktrees(conn, project_id, repo_root_path=".")
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

def run_preflight(conn, project_id: str, auto_fix: bool = False) -> dict:
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

    # Run all checks independently
    checks["system"] = check_system(conn)
    checks["version"] = check_version(conn, project_id)
    checks["graph"] = check_graph(conn, project_id)
    checks["coverage"] = check_coverage(project_id)
    checks["queue"] = check_queue(conn, project_id)
    checks["batch_worktrees"] = check_batch_worktrees(conn, project_id)
    checks["plugin_update_state"] = check_plugin_update_state()

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
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "auto_fixed": auto_fixed,
    }
