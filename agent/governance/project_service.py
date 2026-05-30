"""Project service — project initialization, isolation, and routing.

Trust chain:
  1. Human calls POST /api/init {project, password} → gets coordinator token (one-time)
  2. Same project re-init → 403 (unless password provided for token reset)
  3. Human gives coordinator token to Coordinator agent
  4. Coordinator uses its token to assign roles to other agents via /api/role/assign
"""

from __future__ import annotations

import json
import os
import sys
import hashlib
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from datetime import datetime, timezone

_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from utils import tasks_root
from .db import get_connection, _governance_root
from .graph import AcceptanceGraph
from . import state_service
from . import role_service
from . import audit_service
from .errors import ValidationError, AuthError, PermissionDeniedError

_PROJECTS_LOCK = threading.RLock()


def _projects_file() -> Path:
    p = _governance_root() / "projects.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def _load_projects() -> dict:
    path = _projects_file()
    if not path.exists():
        return {"version": 1, "projects": {}}
    last_error = None
    for attempt in range(3):
        try:
            with _PROJECTS_LOCK:
                with open(str(path), "r", encoding="utf-8") as f:
                    data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("version", 1)
                data.setdefault("projects", {})
                return data
            raise ValueError("projects registry root must be a JSON object")
        except json.JSONDecodeError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.03)
                continue
            break
    raise ValidationError(f"project registry is not valid JSON: {path}: {last_error}")


def _save_projects(data: dict):
    path = _projects_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    with _PROJECTS_LOCK:
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as f:
                tmp_name = f.name
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        finally:
            if tmp_name:
                try:
                    Path(tmp_name).unlink(missing_ok=True)
                except OSError:
                    pass


def _ensure_clean_git_worktree_for_graph(workspace: Path) -> dict:
    """Require clean git state before commit-bound graph snapshots are built."""
    try:
        root_proc = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return {"is_git_repo": False, "dirty": False, "reason": "git unavailable"}
    if root_proc.returncode != 0:
        return {"is_git_repo": False, "dirty": False}
    git_root_raw = (root_proc.stdout or "").strip()
    git_root = Path(git_root_raw).resolve() if git_root_raw else workspace.resolve()
    try:
        status_proc = subprocess.run(
            ["git", "-C", str(git_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception as exc:
        raise ValidationError(f"cannot inspect git worktree before graph build: {exc}") from exc
    if status_proc.returncode != 0:
        detail = (status_proc.stderr or status_proc.stdout or "").strip()
        raise ValidationError(f"cannot inspect git worktree before graph build: {detail}")
    dirty = [line for line in (status_proc.stdout or "").splitlines() if line.strip()]
    if dirty:
        sample = "; ".join(dirty[:5])
        more = "" if len(dirty) <= 5 else f"; +{len(dirty) - 5} more"
        raise ValidationError(
            "cannot bootstrap graph from a dirty git worktree; "
            "graph snapshots are commit-bound. Commit or stash local changes "
            f"before bootstrap. Dirty files: {sample}{more}"
        )
    return {"is_git_repo": True, "dirty": False, "git_root": str(git_root)}


def _safe_read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_aming_claw_plugin_root(root: Path) -> bool:
    manifest = _safe_read_json(root / ".codex-plugin" / "plugin.json")
    return (
        str(manifest.get("name") or "") == "aming-claw"
        and (root / "agent" / "mcp" / "server.py").is_file()
        and (root / "skills" / "aming-claw" / "SKILL.md").is_file()
    )


def _mcp_config_points_at_self_project(path: Path) -> bool:
    payload = _safe_read_json(path)
    servers = payload.get("mcpServers") if isinstance(payload, dict) else {}
    server = servers.get("aming-claw") if isinstance(servers, dict) else None
    if not isinstance(server, dict):
        return False
    args = [str(item) for item in (server.get("args") or []) if str(item)]
    for index, item in enumerate(args[:-1]):
        if item == "--project" and args[index + 1] == "aming-claw":
            return True
    return False


def _marketplace_contains_aming_claw(path: Path) -> bool:
    payload = _safe_read_json(path)
    plugins = payload.get("plugins") if isinstance(payload, dict) else []
    if not isinstance(plugins, list):
        return False
    return any(isinstance(item, dict) and item.get("name") == "aming-claw" for item in plugins)


def inspect_target_workspace_pollution(workspace: str | Path) -> dict:
    """Detect Aming Claw plugin/runtime artifacts inside an external target."""
    root = Path(workspace).resolve()
    if _is_aming_claw_plugin_root(root):
        return {"ok": True, "issues": [], "workspace_path": str(root)}

    issues: list[dict] = []

    mcp_path = root / ".mcp.json"
    if mcp_path.is_file() and _mcp_config_points_at_self_project(mcp_path):
        issues.append({
            "path": ".mcp.json",
            "kind": "self_project_mcp_config",
            "message": "targets project_id `aming-claw` instead of the target project",
        })

    plugin_manifests = (
        (".codex-plugin/plugin.json", "codex_plugin_manifest"),
        (".claude-plugin/plugin.json", "claude_plugin_manifest"),
    )
    for rel, kind in plugin_manifests:
        payload = _safe_read_json(root / rel)
        if payload.get("name") == "aming-claw":
            issues.append({
                "path": rel,
                "kind": kind,
                "message": "Aming Claw plugin manifest is present in the target root",
            })

    for rel in (".agents/plugins/marketplace.json", ".claude-plugin/marketplace.json"):
        if _marketplace_contains_aming_claw(root / rel):
            issues.append({
                "path": rel,
                "kind": "plugin_marketplace",
                "message": "Aming Claw plugin marketplace metadata is present in the target root",
            })

    if (root / "shared-volume" / "codex-tasks").exists():
        issues.append({
            "path": "shared-volume/codex-tasks",
            "kind": "runtime_shared_volume",
            "message": "governance runtime state is inside the target project",
        })

    if (root / "agent" / "mcp" / "resources" / "self-graph-bundle-manifest.json").exists():
        issues.append({
            "path": "agent/mcp/resources/self-graph-bundle-manifest.json",
            "kind": "self_graph_bundle",
            "message": "Aming Claw self-graph bundle is present in the target project",
        })

    return {"ok": not issues, "issues": issues, "workspace_path": str(root)}


def _progress_with_elapsed(progress: object) -> object:
    if not isinstance(progress, dict):
        return progress
    out = dict(progress)
    started_at = str(out.get("started_at") or "").strip()
    if started_at:
        try:
            started = datetime.strptime(started_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            out["elapsed_seconds"] = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
        except ValueError:
            pass
    return out


# ============================================================
# Project ID normalization
# ============================================================

def _normalize_project_id(raw: str) -> str:
    """Normalize project ID to lowercase kebab-case.
    Delegates to shared utility in utils.py.
    """
    # Import from shared utils to avoid duplication.
    # Uses try/except for Docker context where utils may not be on path.
    try:
        from utils import normalize_project_id
        return normalize_project_id(raw)
    except ImportError:
        pass
    # Fallback: inline logic (same as utils.normalize_project_id)
    import re
    s = raw.strip()
    if not s:
        return ""
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1-\2', s)
    s = re.sub(r'[\s_]+', '-', s)
    s = re.sub(r'-+', '-', s)
    return s.lower().strip('-')


def _check_id_conflict(normalized: str, projects: dict) -> str | None:
    """Check if a normalized ID conflicts with existing projects.
    Returns the conflicting project_id or None.
    """
    for existing_id in projects.get("projects", {}):
        if _normalize_project_id(existing_id) == normalized and existing_id != normalized:
            return existing_id
    return None


# ============================================================
# /api/init — one-time project initialization
# ============================================================

def init_project(project_id: str, password: str = "", project_name: str = "", workspace_path: str = "") -> dict:
    """Initialize a project. No password or token required.

    Rules:
      - project_id is normalized to lowercase kebab-case
      - First call: creates project → returns project info
      - Repeat call: returns existing project info (idempotent)

    Returns: {project: {project_id, name, status, created_at}}
    """
    if not project_id:
        raise ValidationError("project_id is required")

    # Normalize ID
    original_id = project_id
    project_id = _normalize_project_id(project_id)

    if not project_id or not project_id.replace("-", "").isalnum():
        raise ValidationError(f"Invalid project_id: {original_id!r} (normalized: {project_id!r})")

    # Check for conflicting IDs
    projects = _load_projects()
    conflict = _check_id_conflict(project_id, projects)
    if conflict:
        raise ValidationError(
            f"Project ID conflict: {original_id!r} normalizes to {project_id!r} "
            f"which conflicts with existing project {conflict!r}"
        )

    existing = projects["projects"].get(project_id)

    if existing and existing.get("initialized"):
        # Already exists — return existing project (idempotent)
        return {
            "project": {
                "project_id": project_id,
                "name": existing.get("name", project_id),
                "status": existing.get("status", "active"),
                "created_at": existing.get("created_at", ""),
            },
            "message": "Project already initialized",
        }

    # First-time initialization
    project_dir = _governance_root() / project_id
    project_dir.mkdir(parents=True, exist_ok=True)

    entry = {
        "project_id": project_id,
        "name": project_name or project_id,
        "workspace_path": workspace_path,
        "created_at": _utc_iso(),
        "initialized": True,
        "status": "active",
        "node_count": 0,
    }
    projects["projects"][project_id] = entry
    _save_projects(projects)

    # Ensure DB exists
    conn = get_connection(project_id)
    conn.close()

    result = {
        "project": {
            "project_id": project_id,
            "name": entry["name"],
            "status": "active",
            "created_at": entry["created_at"],
        },
        "message": "Project initialized. Submit tasks via API or Telegram.",
    }
    if original_id != project_id:
        result["normalized_from"] = original_id
        result["message"] += f" Note: project_id normalized from '{original_id}' to '{project_id}'."
    return result


def _reset_coordinator_token(project_id: str, projects: dict, entry: dict) -> dict:
    """Reset coordinator token for an existing project."""
    conn = get_connection(project_id)
    try:
        # Re-register coordinator (will refresh existing session)
        coord_result = role_service.register(
            conn, "coordinator", project_id, "coordinator",
        )
        conn.commit()

        audit_service.record(
            conn, project_id, "coordinator_token_reset",
            actor="human",
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "project": {
            "project_id": project_id,
            "name": entry.get("name", project_id),
            "status": entry.get("status", "active"),
        },
        "coordinator": {
            "session_id": coord_result["session_id"],
            "token": coord_result["token"],
        },
        "message": "Coordinator token has been reset.",
    }


# ============================================================
# Role assignment (coordinator only)
# ============================================================

def assign_role(
    conn,
    project_id: str,
    coordinator_session: dict,
    principal_id: str,
    role: str,
    scope: list = None,
) -> dict:
    """Coordinator assigns a role to another agent.

    Only coordinators can call this. Returns the new agent's token.
    """
    if coordinator_session.get("role") != "coordinator":
        raise PermissionDeniedError(
            coordinator_session.get("role", "unknown"),
            "assign_role",
            {"detail": "Only coordinator can assign roles"},
        )
    if role == "coordinator":
        raise PermissionDeniedError(
            "coordinator", "assign_role",
            {"detail": "Cannot assign coordinator role. Use /api/init to get coordinator token."},
        )

    result = role_service.register(
        conn, principal_id, project_id, role, scope=scope,
    )

    audit_service.record(
        conn, project_id, "role_assigned",
        actor=coordinator_session.get("principal_id", ""),
        assigned_principal=principal_id,
        assigned_role=role,
        session_id=coordinator_session.get("session_id", ""),
    )

    return {
        "principal_id": principal_id,
        "role": role,
        "session_id": result["session_id"],
        "token": result["token"],
        "scope": scope or [],
        "expires_at": result.get("expires_at", ""),
        "message": f"Give this token to {principal_id}. It grants {role} access to {project_id}.",
    }


def revoke_role(
    conn,
    project_id: str,
    coordinator_session: dict,
    session_id: str,
) -> dict:
    """Coordinator revokes an agent's session."""
    if coordinator_session.get("role") != "coordinator":
        raise PermissionDeniedError(
            coordinator_session.get("role", "unknown"),
            "revoke_role",
        )

    result = role_service.deregister(conn, session_id)

    audit_service.record(
        conn, project_id, "role_revoked",
        actor=coordinator_session.get("principal_id", ""),
        revoked_session=session_id,
    )

    return result


# ============================================================
# Project query helpers
# ============================================================

def get_project(project_id: str) -> dict | None:
    projects = _load_projects()
    return projects["projects"].get(project_id)


def resolve_project_root(
    project_id: str,
    explicit_root: str | Path | None = None,
    *,
    fallback_self: bool = True,
) -> Path | None:
    """Resolve the registered workspace root for a governed project."""
    if explicit_root:
        return Path(explicit_root).resolve()

    normalized = _normalize_project_id(project_id or "")
    entry = get_project(normalized) or get_project(project_id)
    if entry and entry.get("workspace_path"):
        return Path(entry["workspace_path"]).resolve()

    if fallback_self and normalized == "aming-claw":
        return Path(__file__).resolve().parents[2]
    return None


def list_projects() -> list[dict]:
    projects = _load_projects()
    result = []
    for p in projects["projects"].values():
        # Never expose password_hash
        safe = {k: v for k, v in p.items() if k != "password_hash"}
        if "bootstrap_progress" in safe:
            safe["bootstrap_progress"] = _progress_with_elapsed(safe["bootstrap_progress"])
        result.append(safe)
    return result


def update_project_metadata(project_id: str, updates: dict) -> dict:
    """Persist small dashboard/project metadata fields in projects.json."""
    project_id = _normalize_project_id(project_id)
    projects = _load_projects()
    entry = projects["projects"].get(project_id)
    if not entry:
        raise ValidationError(f"Project {project_id!r} not registered")
    allowed = {
        "name",
        "selected_ref",
        "selected_ref_updated_at",
        "selected_ref_updated_by",
    }
    for key, value in (updates or {}).items():
        if key in allowed:
            entry[key] = value
    _save_projects(projects)
    return {k: v for k, v in entry.items() if k != "password_hash"}


def _sanitize_ai_routing(routing: dict) -> dict:
    """Normalize dashboard role routing before storing it in projects.json."""
    out: dict[str, dict[str, str]] = {}
    if not isinstance(routing, dict):
        return out
    for role, route in routing.items():
        role_key = str(role or "").strip().lower()
        if not role_key:
            continue
        if isinstance(route, dict):
            provider = str(route.get("provider") or "").strip()
            model = str(route.get("model") or "").strip()
        else:
            provider = ""
            model = str(route or "").strip()
        if provider or model:
            out[role_key] = {"provider": provider, "model": model}
    return out


def _copy_json_dict(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    try:
        return json.loads(json.dumps(raw))
    except (TypeError, ValueError):
        return dict(raw)


def project_config_to_metadata(config) -> dict:
    """Serialize a ProjectConfig-like object for central registry storage."""
    try:
        from project_config import e2e_config_to_dict, effective_graph_exclude_roots
    except Exception:
        e2e_config_to_dict = None
        effective_graph_exclude_roots = None

    testing = getattr(config, "testing", None)
    build = getattr(config, "build", None)
    deploy = getattr(config, "deploy", None)
    governance = getattr(config, "governance", None)
    graph = getattr(config, "graph", None)
    nested = getattr(graph, "nested_projects", None)
    ai = getattr(config, "ai", None)
    e2e = getattr(testing, "e2e", None)
    e2e_payload = e2e_config_to_dict(e2e) if e2e_config_to_dict and e2e is not None else {}
    effective_excludes = (
        effective_graph_exclude_roots(config) if effective_graph_exclude_roots else []
    )
    return {
        "project_id": str(getattr(config, "project_id", "") or "").strip(),
        "language": str(getattr(config, "language", "") or "python").strip(),
        "testing": {
            "unit_command": str(getattr(testing, "unit_command", "") or ""),
            "e2e_command": str(getattr(testing, "e2e_command", "") or ""),
            "e2e": e2e_payload,
        },
        "build": {
            "command": str(getattr(build, "command", "") or ""),
            "release_checks": list(getattr(build, "release_checks", []) or []),
        },
        "deploy": {
            "strategy": str(getattr(deploy, "strategy", "") or "none"),
            "service_rules_count": len(getattr(deploy, "service_rules", []) or []),
        },
        "governance": {
            "enabled": bool(getattr(governance, "enabled", False)),
            "test_tool_label": str(getattr(governance, "test_tool_label", "") or ""),
            "exclude_roots": list(getattr(governance, "exclude_roots", []) or []),
        },
        "graph": {
            "exclude_paths": list(getattr(graph, "exclude_paths", []) or []),
            "ignore_globs": list(getattr(graph, "ignore_globs", []) or []),
            "nested_projects": {
                "mode": str(getattr(nested, "mode", "exclude") or "exclude"),
                "roots": list(getattr(nested, "roots", []) or []),
            },
            "effective_exclude_roots": list(effective_excludes or []),
        },
        "ai": {
            "routing": _sanitize_ai_routing(getattr(ai, "routing", {}) or {}),
        },
    }


def get_project_config_metadata(project_id: str) -> dict:
    """Return the central project config snapshot stored in projects.json."""
    project_id = _normalize_project_id(project_id)
    entry = get_project(project_id) or {}
    return _copy_json_dict(entry.get("project_config"))


def set_project_config_metadata(
    project_id: str,
    config: dict,
    *,
    source: str = "aming_claw_registry",
    actor: str = "",
) -> dict:
    """Persist a central, non-invasive project config snapshot."""
    project_id = _normalize_project_id(project_id)
    projects = _load_projects()
    entry = projects["projects"].get(project_id)
    if not entry:
        raise ValidationError(f"Project {project_id!r} not registered")

    payload = _copy_json_dict(config)
    payload["project_id"] = project_id
    entry["project_config"] = payload
    entry["project_config_source"] = source or "aming_claw_registry"
    entry["project_config_updated_at"] = _utc_iso()
    entry["project_config_updated_by"] = actor or "system"
    _save_projects(projects)
    return {k: v for k, v in entry.items() if k != "password_hash"}


def update_project_ai_routing_metadata(
    project_id: str,
    routing: dict,
    *,
    base_config=None,
    actor: str = "",
) -> dict:
    """Persist dashboard AI routing in Aming-claw's registry, not the target repo."""
    project_id = _normalize_project_id(project_id)
    projects = _load_projects()
    entry = projects["projects"].get(project_id)
    if not entry:
        raise ValidationError(f"Project {project_id!r} not registered")

    payload = _copy_json_dict(entry.get("project_config"))
    if not payload and base_config is not None:
        payload = (
            _copy_json_dict(base_config)
            if isinstance(base_config, dict)
            else project_config_to_metadata(base_config)
        )
    if not payload:
        payload = {
            "project_id": project_id,
            "language": "python",
            "testing": {"unit_command": "python -m pytest", "e2e": {}},
            "graph": {"exclude_paths": [], "ignore_globs": [], "effective_exclude_roots": []},
            "ai": {"routing": {}},
        }

    payload["project_id"] = project_id
    ai_raw = payload.get("ai") if isinstance(payload.get("ai"), dict) else {}
    merged_routing = _sanitize_ai_routing(ai_raw.get("routing") if isinstance(ai_raw.get("routing"), dict) else {})
    merged_routing.update(_sanitize_ai_routing(routing))
    ai_raw["routing"] = merged_routing
    payload["ai"] = ai_raw

    entry["project_config"] = payload
    entry["project_config_source"] = "aming_claw_registry"
    entry["project_config_updated_at"] = _utc_iso()
    entry["project_config_updated_by"] = actor or "dashboard"
    _save_projects(projects)
    return {k: v for k, v in entry.items() if k != "password_hash"}


def update_project_operation_progress(
    project_id: str,
    *,
    operation: str,
    status: str,
    phase: str,
    message: str = "",
) -> dict | None:
    """Persist lightweight project operation progress for dashboard polling."""
    project_id = _normalize_project_id(project_id)
    projects = _load_projects()
    entry = projects["projects"].get(project_id)
    if not entry:
        return None

    now = _utc_iso()
    previous = entry.get("bootstrap_progress") if isinstance(entry.get("bootstrap_progress"), dict) else {}
    same_operation = previous.get("operation") == operation and previous.get("status") == "running"
    started_at = previous.get("started_at") if same_operation else now
    heartbeat = int(previous.get("heartbeat") or 0) + 1 if same_operation else 1
    progress = {
        "operation": operation,
        "status": status,
        "phase": phase,
        "message": message or phase,
        "started_at": started_at,
        "updated_at": now,
        "heartbeat": heartbeat,
    }
    if status in {"succeeded", "failed", "cancelled"}:
        progress["completed_at"] = now
    entry["bootstrap_progress"] = progress
    _save_projects(projects)
    return progress


def project_exists(project_id: str) -> bool:
    return get_project(project_id) is not None


# ============================================================
# Graph import
# ============================================================

def import_graph(project_id: str, md_path: str) -> dict:
    """Import acceptance graph from markdown for a project."""
    if not project_exists(project_id):
        raise ValidationError(f"Project {project_id!r} not registered")

    graph = AcceptanceGraph()
    result = graph.import_from_markdown(md_path)

    graph_path = _governance_root() / project_id / "graph.json"
    graph.save(graph_path)

    conn = get_connection(project_id)
    try:
        count = state_service.init_node_states(conn, project_id, graph)
        conn.commit()
    finally:
        conn.close()

    projects = _load_projects()
    if project_id in projects["projects"]:
        projects["projects"][project_id]["node_count"] = graph.node_count()
        _save_projects(projects)

    result["node_states_initialized"] = count
    return result


def sync_node_state_from_graph(project_id: str) -> dict:
    """Rebuild or sync runtime node_state rows from the persisted graph definition.

    This is intended for governance recovery paths. It never infers new business
    acceptance; it only re-materializes node_state rows from graph.json and
    import-declared statuses already encoded in the graph.
    """
    if not project_exists(project_id):
        raise ValidationError(f"Project {project_id!r} not registered")

    graph = load_project_graph(project_id)

    conn = get_connection(project_id)
    try:
        initialized = state_service.init_node_states(conn, project_id, graph)
        total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM node_state WHERE project_id = ?",
            (project_id,),
        ).fetchone()["cnt"]
        conn.commit()
    finally:
        conn.close()

    return {
        "project_id": project_id,
        "graph_nodes": graph.node_count(),
        "node_states_initialized": initialized,
        "node_state_total": total,
        "repair_mode": "sync_from_graph",
    }


def bootstrap_project(
    workspace_path: str,
    project_name: str = "",
    config_override: dict = None,
    scan_depth: int = 3,
    exclude_patterns: list = None,
) -> dict:
    """Bootstrap a project from workspace — atomic orchestrator (R4).

    Steps: config discovery -> init_project -> scan_codebase -> generate_graph
           -> node_state init -> version seed -> preflight check.

    Rollback on failure: removes project entry if it was freshly created.

    Returns: {project_id, graph_stats, config, preflight, warning?}
    """
    import sys as _sys
    _agent_root = str(Path(__file__).resolve().parents[1])
    if _agent_root not in _sys.path:
        _sys.path.insert(0, _agent_root)

    from project_config import (
        effective_graph_exclude_roots,
        generate_default_config,
        load_project_config,
    )

    ws = Path(workspace_path).resolve()
    if not ws.is_dir():
        raise ValidationError(f"workspace_path does not exist or is not a directory: {workspace_path}")
    pollution = inspect_target_workspace_pollution(ws)
    if not pollution["ok"]:
        shown = "; ".join(
            f"{issue['path']} ({issue['kind']})" for issue in pollution["issues"][:6]
        )
        more = "" if len(pollution["issues"]) <= 6 else f"; +{len(pollution['issues']) - 6} more"
        raise ValidationError(
            "target workspace contains Aming Claw plugin/runtime artifacts that do not belong "
            f"to this project: {shown}{more}. Remove them from the target project or choose the "
            "real project root before bootstrap."
        )

    # Step 1: Config discovery
    try:
        config = load_project_config(ws)
        config_source = "workspace_config"
    except (FileNotFoundError, ValueError):
        config = generate_default_config(str(ws), project_name)
        config_source = "generated_default"

    if config_override:
        # Apply overrides
        if "project_id" in config_override:
            config.project_id = config_override["project_id"]
        if "language" in config_override:
            config.language = config_override["language"]
        if "testing" in config_override and "unit_command" in config_override["testing"]:
            config.testing.unit_command = config_override["testing"]["unit_command"]
        if "graph" in config_override and isinstance(config_override["graph"], dict):
            graph_override = config_override["graph"]
            if "exclude_paths" in graph_override:
                config.graph.exclude_paths = [
                    str(value).replace("\\", "/").strip().strip("/")
                    for value in graph_override.get("exclude_paths") or []
                    if str(value or "").strip()
                ]
            if "ignore_globs" in graph_override:
                config.graph.ignore_globs = [
                    str(value).replace("\\", "/").strip().strip("/")
                    for value in graph_override.get("ignore_globs") or []
                    if str(value or "").strip()
                ]
        if "ai" in config_override and isinstance(config_override["ai"], dict):
            ai_override = config_override["ai"]
            if isinstance(ai_override.get("routing"), dict):
                for role, route in ai_override["routing"].items():
                    if isinstance(route, dict):
                        config.ai.routing[str(role).lower()] = {
                            "provider": str(route.get("provider", "") or "").strip(),
                            "model": str(route.get("model", "") or "").strip(),
                        }

    pid = config.project_id or project_name or ws.name.lower().replace("_", "-")
    pid = _normalize_project_id(pid)
    git_gate = _ensure_clean_git_worktree_for_graph(ws)
    update_project_operation_progress(
        pid,
        operation="bootstrap",
        status="running",
        phase="config",
        message="Project config resolved.",
    )

    # Step 2: init_project (idempotent — AC6)
    is_new = not project_exists(pid)
    try:
        update_project_operation_progress(
            pid,
            operation="bootstrap",
            status="running",
            phase="register",
            message="Registering project workspace.",
        )
        init_result = init_project(
            project_id=pid,
            project_name=project_name or pid,
            workspace_path=str(ws),
        )
        if project_name.strip():
            projects = _load_projects()
            if pid in projects["projects"]:
                projects["projects"][pid]["name"] = project_name.strip()
                _save_projects(projects)
    except Exception as e:
        raise ValidationError(f"Project initialization failed: {e}")

    try:
        # Step 3: snapshot-native full reconcile + activation. The old
        # generate_graph path wrote a legacy graph.json only; dashboard and
        # graph-governance now consume active graph snapshots.
        update_project_operation_progress(
            pid,
            operation="bootstrap",
            status="running",
            phase="scan",
            message="Scanning files and building graph snapshot.",
        )
        configured_excludes = effective_graph_exclude_roots(config)
        configured_ignore_globs = [
            str(value).replace("\\", "/").strip().strip("/")
            for value in (getattr(getattr(config, "graph", None), "ignore_globs", []) or [])
            if str(value or "").strip()
        ]
        effective_excludes = sorted({
            str(value).replace("\\", "/").strip().strip("/")
            for value in ((exclude_patterns or []) + configured_excludes)
            if str(value or "").strip()
        })
        conn = get_connection(pid)
        try:
            # Step 4: version seed remains for legacy gates and health checks.
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                "INSERT OR REPLACE INTO project_version "
                "(project_id, chain_version, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?)",
                (pid, "bootstrap", now, "bootstrap"),
            )
            conn.commit()

            from .state_reconcile import run_state_only_full_reconcile

            update_project_operation_progress(
                pid,
                operation="bootstrap",
                status="running",
                phase="full_reconcile",
                message="Running full graph reconcile.",
            )
            reconcile_result = run_state_only_full_reconcile(
                conn,
                pid,
                ws,
                run_id=f"bootstrap-full-{pid}",
                snapshot_kind="full",
                created_by="bootstrap",
                activate=True,
                notes_extra={
                    "source": "bootstrap_project_v2",
                    "effective_exclude_roots": effective_excludes,
                    "effective_ignore_globs": configured_ignore_globs,
                    "scan_depth": scan_depth,
                    "git_gate": git_gate,
                },
                semantic_enrich=True,
                semantic_use_ai=False,
                semantic_enqueue_stale=False,
                graph_exclude_paths=effective_excludes,
                graph_ignore_globs=configured_ignore_globs,
            )
            conn.commit()

            graph_stats = reconcile_result.get("graph_stats") or {}
            index_counts = reconcile_result.get("index_counts") or {}
            node_count = int(graph_stats.get("node_count") or index_counts.get("nodes") or 0)
            edge_count = int(graph_stats.get("edge_count") or index_counts.get("edges") or 0)
            preflight_result = {
                "status": "pass" if reconcile_result.get("ok") else "fail",
                "details": {
                    "bootstrap_mode": "snapshot_full_reconcile",
                    "snapshot_id": reconcile_result.get("snapshot_id", ""),
                    "activation": reconcile_result.get("activation") or {},
                    "projection_status": (
                        reconcile_result.get("activation") or {}
                    ).get("projection_status", ""),
                    "node_count": node_count,
                    "edge_count": edge_count,
                },
            }

            # Update project metadata
            projects = _load_projects()
            if pid in projects["projects"]:
                projects["projects"][pid]["node_count"] = node_count
                projects["projects"][pid]["active_snapshot_id"] = reconcile_result.get("snapshot_id", "")
                _save_projects(projects)

            # Step 5: Backfill chain history for this project at bootstrap.
            try:
                update_project_operation_progress(
                    pid,
                    operation="bootstrap",
                    status="running",
                    phase="chain_history",
                    message="Backfilling chain history.",
                )
                from .chain_trailer import backfill_legacy_chain_history
                backfill_legacy_chain_history(project_id=pid, incremental=False)
            except Exception:
                pass  # Non-fatal — git may not be available in all contexts
        finally:
            conn.close()

    except Exception as e:
        update_project_operation_progress(
            pid,
            operation="bootstrap",
            status="failed",
            phase="failed",
            message=str(e),
        )
        # Rollback: remove project if newly created
        if is_new:
            projects = _load_projects()
            projects["projects"].pop(pid, None)
            _save_projects(projects)
        raise ValidationError(f"Bootstrap failed: {e}")

    # Build response
    config_dict = project_config_to_metadata(config)
    config_dict["project_id"] = pid
    set_project_config_metadata(
        pid,
        config_dict,
        source=config_source,
        actor="bootstrap",
    )

    result = {
        "project_id": pid,
        "graph_stats": {
            "node_count": node_count,
            "edge_count": edge_count,
            "layers": (graph_stats or {}).get("layers") or {},
        },
        "config": config_dict,
        "preflight": preflight_result,
        "snapshot_id": reconcile_result.get("snapshot_id", ""),
        "activation": reconcile_result.get("activation") or {},
        "bootstrap_mode": "snapshot_full_reconcile",
        "git_gate": git_gate,
    }
    update_project_operation_progress(
        pid,
        operation="bootstrap",
        status="succeeded",
        phase="complete",
        message=f"Graph ready: {node_count} nodes, {edge_count} edges.",
    )

    return result


def load_project_graph(project_id: str) -> AcceptanceGraph:
    from .db import _resolve_project_dir
    project_dir = _resolve_project_dir(project_id)
    graph_path = project_dir / "graph.json"
    if not graph_path.exists():
        raise ValidationError(f"No graph found for project {project_id!r}. Run import-graph first.")
    graph = AcceptanceGraph()
    graph.load(graph_path)
    return graph
