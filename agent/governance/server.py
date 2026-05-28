"""HTTP server for the governance service.

Uses stdlib http.server (Starlette upgrade deferred to when dependencies are added).
Provides routing, middleware (auth, idempotency, request_id, audit), and JSON handling.
"""
from __future__ import annotations

import json
import mimetypes
import re
import sys
import uuid
import hashlib
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from pathlib import Path
from typing import Any

_agent_dir = str(Path(__file__).resolve().parents[1])
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from .errors import GovernanceError, ValidationError
from .dirty_worktree import filter_dirty_files
import logging
import sqlite3
import time

log = logging.getLogger(__name__)
from .db import get_connection, DBContext, independent_connection
from . import role_service
from . import state_service
from . import project_service
from . import memory_service
from . import audit_service
from . import reconcile_session
from . import backlog_runtime
from . import raw_requirement
from .idempotency import check_idempotency, store_idempotency
from .redis_client import get_redis
from .models import Evidence, MemoryEntry, NodeDef
from .enums import VerifyStatus
from .impact_analyzer import ImpactAnalyzer
from .models import ImpactAnalysisRequest, FileHitPolicy

import os
import shutil
import signal
import subprocess
PORT = int(os.environ.get("GOVERNANCE_PORT", "40000"))
DASHBOARD_ROUTE_PREFIX = "/dashboard"

AI_MODEL_CATALOG = {
    "anthropic": [
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
    ],
    "openai": [
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.3-codex",
    ],
}

AI_PROVIDER_REQUIREMENTS = {
    "anthropic": {
        "label": "Anthropic",
        "runtime": "Claude Code CLI",
        "command": "claude",
        "env_var": "CLAUDE_BIN",
    },
    "openai": {
        "label": "OpenAI",
        "runtime": "Codex CLI",
        "command": "codex",
        "env_var": "CODEX_BIN",
    },
}

# --- Server Version (dynamic with 30s cache) ---
_version_cache = {"value": "unknown", "ts": 0}


def get_server_version():
    """Return current git HEAD hash, cached for 30 seconds."""
    if time.time() - _version_cache["ts"] < 30:
        return _version_cache["value"]
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        ).stdout.strip()
        _version_cache["value"] = head or "unknown"
        _version_cache["ts"] = time.time()
    except Exception:
        pass
    return _version_cache["value"]


# Backward compatibility alias
SERVER_VERSION = get_server_version()
SERVER_PID = os.getpid()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clamped_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _open_local_directory_picker(
    initial_path: str = "",
    title: str = "Choose project directory",
    timeout_seconds: float = 12.0,
) -> str:
    """Open a local directory picker and return the selected absolute path."""
    errors: list[str] = []
    if sys.platform == "darwin":
        try:
            return _open_local_directory_picker_macos(
                initial_path=initial_path,
                title=title,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:  # pragma: no cover - depends on host desktop
            errors.append(f"macos picker unavailable: {exc}")
            raise RuntimeError("local directory picker unavailable: " + "; ".join(errors))

    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - depends on host Python build
        errors.append(f"tkinter unavailable: {exc}")
        tk = None
        filedialog = None

    if tk is None or filedialog is None:
        if os.name == "nt":
            try:
                return _open_local_directory_picker_windows(
                    initial_path=initial_path,
                    title=title,
                    timeout_seconds=timeout_seconds,
                )
            except Exception as exc:  # pragma: no cover - depends on host desktop
                errors.append(f"windows picker unavailable: {exc}")
        elif os.name == "posix":
            try:
                return _open_local_directory_picker_linux(
                    initial_path=initial_path,
                    title=title,
                    timeout_seconds=timeout_seconds,
                )
            except Exception as exc:  # pragma: no cover - depends on host desktop
                errors.append(f"linux picker unavailable: {exc}")
        raise RuntimeError("local directory picker unavailable: " + "; ".join(errors))

    initial_dir = ""
    if initial_path:
        candidate = Path(initial_path).expanduser()
        if candidate.exists():
            initial_dir = str(candidate if candidate.is_dir() else candidate.parent)

    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
            root.update()
        except Exception:
            pass
        options = {"parent": root, "title": title, "mustexist": True}
        if initial_dir:
            options["initialdir"] = initial_dir
        selected = filedialog.askdirectory(**options)
        return str(Path(selected).resolve()) if selected else ""
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


def _open_local_directory_picker_windows(
    initial_path: str = "",
    title: str = "Choose project directory",
    timeout_seconds: float = 12.0,
) -> str:
    """Fallback folder picker for Windows Python builds without tkinter."""
    exe = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        raise RuntimeError("PowerShell is not available")
    env = os.environ.copy()
    env["AMING_CLAW_PICKER_INITIAL"] = initial_path
    env["AMING_CLAW_PICKER_TITLE"] = title
    script = r"""
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = $env:AMING_CLAW_PICKER_TITLE
$dialog.ShowNewFolderButton = $false
$initial = $env:AMING_CLAW_PICKER_INITIAL
if ($initial -and (Test-Path -LiteralPath $initial)) {
  $resolved = Resolve-Path -LiteralPath $initial
  if ($resolved) { $dialog.SelectedPath = $resolved.Path }
}
$result = $dialog.ShowDialog()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) {
  [Console]::Out.Write($dialog.SelectedPath)
}
"""
    args = [exe, "-NoProfile"]
    if "powershell" in Path(exe).name.lower():
        args.append("-STA")
    args.extend(["-Command", script])
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            env=env,
            timeout=max(3.0, min(float(timeout_seconds or 12.0), 60.0)),
        )
    except subprocess.TimeoutExpired as exc:
        if exc.cmd:
            raise RuntimeError("directory picker timed out; paste the path manually") from exc
        raise
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(detail or f"PowerShell picker exited {proc.returncode}")
    selected = proc.stdout.strip()
    return str(Path(selected).resolve()) if selected else ""


def _open_local_directory_picker_macos(
    initial_path: str = "",
    title: str = "Choose project directory",
    timeout_seconds: float = 12.0,
) -> str:
    """Fallback folder picker for macOS Python builds without tkinter."""
    exe = shutil.which("osascript")
    if not exe:
        raise RuntimeError("osascript is not available")
    initial = ""
    if initial_path:
        candidate = Path(initial_path).expanduser()
        if candidate.exists():
            initial = str(candidate if candidate.is_dir() else candidate.parent)
    prompt = title.replace('"', '\\"')
    script = f'set selectedFolder to choose folder with prompt "{prompt}"'
    if initial:
        script += f' default location POSIX file "{initial}"'
    script += "\nPOSIX path of selectedFolder"
    proc = subprocess.run(
        [exe, "-e", script],
        capture_output=True,
        text=True,
        timeout=max(3.0, min(float(timeout_seconds or 12.0), 60.0)),
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(detail or f"osascript picker exited {proc.returncode}")
    selected = proc.stdout.strip()
    return str(Path(selected).resolve()) if selected else ""


def _open_local_directory_picker_linux(
    initial_path: str = "",
    title: str = "Choose project directory",
    timeout_seconds: float = 12.0,
) -> str:
    """Fallback folder picker for Linux Python builds without tkinter."""
    initial_dir = ""
    if initial_path:
        candidate = Path(initial_path).expanduser()
        if candidate.exists():
            initial_dir = str(candidate if candidate.is_dir() else candidate.parent)
    exe = shutil.which("zenity")
    if exe:
        args = [exe, "--file-selection", "--directory", f"--title={title}"]
        if initial_dir:
            args.append(f"--filename={initial_dir}{os.sep}")
    else:
        exe = shutil.which("kdialog")
        if exe:
            args = [exe, "--getexistingdirectory", initial_dir or str(Path.home()), title]
        else:
            raise RuntimeError("Linux folder picker requires tkinter, zenity, or kdialog")
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=max(3.0, min(float(timeout_seconds or 12.0), 60.0)),
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(detail or f"{Path(exe).name} picker exited {proc.returncode}")
    selected = proc.stdout.strip()
    return str(Path(selected).resolve()) if selected else ""


def _repo_dashboard_dist_dir() -> Path:
    return (Path(__file__).resolve().parents[2] / "frontend" / "dashboard" / "dist").resolve()


def _packaged_dashboard_dist_dir() -> Path | None:
    try:
        from importlib import resources
        root = resources.files("agent.governance.dashboard_dist")
        index = root.joinpath("index.html")
        if not index.is_file():
            return None
        try:
            return Path(os.fspath(root)).resolve()
        except TypeError:
            return None
    except Exception:
        return None


def _dashboard_dist_dir() -> Path:
    """Return the built dashboard directory served by the governance process."""
    override = str(os.environ.get("GOVERNANCE_DASHBOARD_DIST") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    repo_dist = _repo_dashboard_dist_dir()
    if (repo_dist / "index.html").is_file():
        return repo_dist
    packaged_dist = _packaged_dashboard_dist_dir()
    if packaged_dist is not None:
        return packaged_dist
    return repo_dist


def _path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _dashboard_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".js":
        return "application/javascript; charset=utf-8"
    if suffix == ".css":
        return "text/css; charset=utf-8"
    if suffix in {".html", ".htm"}:
        return "text/html; charset=utf-8"
    if suffix == ".svg":
        return "image/svg+xml"
    if suffix == ".json":
        return "application/json; charset=utf-8"
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _resolve_dashboard_static_request(url_path: str, dist_dir: Path | None = None) -> dict[str, Any]:
    """Resolve a /dashboard request to a built SPA asset.

    Returns a small dict so unit tests can verify routing without standing up an
    HTTP server. Non-dashboard paths return {"handled": False}.
    """
    parsed_path = urlparse(url_path).path or "/"
    if parsed_path != DASHBOARD_ROUTE_PREFIX and not parsed_path.startswith(f"{DASHBOARD_ROUTE_PREFIX}/"):
        return {"handled": False}

    dist = (dist_dir or _dashboard_dist_dir()).resolve()
    index = dist / "index.html"
    if not index.is_file():
        return {
            "handled": True,
            "status": 503,
            "path": None,
            "content_type": "text/plain; charset=utf-8",
            "cache_control": "no-store",
            "body": (
                "Dashboard build not found. Run `npm --prefix frontend/dashboard run build` "
                "or set GOVERNANCE_DASHBOARD_DIST."
            ).encode("utf-8"),
        }

    suffix = parsed_path[len(DASHBOARD_ROUTE_PREFIX):].lstrip("/")
    if not suffix:
        target = index
        spa_fallback = True
    else:
        clean_suffix = unquote(suffix).replace("\\", "/")
        parts = [part for part in clean_suffix.split("/") if part]
        if any(part in {".", ".."} for part in parts):
            return {
                "handled": True,
                "status": 404,
                "path": None,
                "content_type": "text/plain; charset=utf-8",
                "cache_control": "no-store",
                "body": b"Not found",
            }
        requested = dist.joinpath(*parts).resolve()
        if not _path_is_relative_to(requested, dist):
            return {
                "handled": True,
                "status": 404,
                "path": None,
                "content_type": "text/plain; charset=utf-8",
                "cache_control": "no-store",
                "body": b"Not found",
            }
        if requested.is_file():
            target = requested
            spa_fallback = False
        elif parts and parts[0] == "assets":
            return {
                "handled": True,
                "status": 404,
                "path": None,
                "content_type": "text/plain; charset=utf-8",
                "cache_control": "no-store",
                "body": b"Not found",
            }
        elif parts and "." in parts[-1]:
            return {
                "handled": True,
                "status": 404,
                "path": None,
                "content_type": "text/plain; charset=utf-8",
                "cache_control": "no-store",
                "body": b"Not found",
            }
        else:
            target = index
            spa_fallback = True

    cache_control = "no-cache" if spa_fallback or target.name == "index.html" else "public, max-age=31536000, immutable"
    return {
        "handled": True,
        "status": 200,
        "path": target,
        "content_type": _dashboard_content_type(target),
        "cache_control": cache_control,
        "body": None,
    }


def _row_get(row, key: str, default=""):
    if row is None:
        return default
    try:
        value = row[key]
    except Exception:
        if isinstance(row, dict):
            value = row.get(key, default)
        else:
            value = default
    return default if value is None else value


def _apply_mf_takeover(conn, project_id: str, bug_id: str, body: dict, row, policy: dict) -> dict:
    """Hold/cancel an unfinished chain task when MF takes ownership."""
    current_task_id = str(_row_get(row, "current_task_id", "") or "")
    task_id = (
        str(body.get("taken_over_task_id") or body.get("takeover_task_id") or current_task_id or "")
        .strip()
    )
    action = str(body.get("takeover_action") or "").strip().lower()
    if not action and task_id:
        action = "hold_current_chain"
    if not action:
        action = "none"

    allowed = {"none", "hold_current_chain", "cancel_current_chain"}
    if action not in allowed:
        raise GovernanceError(
            "invalid_takeover_action",
            f"takeover_action must be one of {sorted(allowed)}, got: {action}",
            422,
        )
    takeover = {
        "action": action,
        "taken_over_task_id": task_id,
        "mf_id": body.get("mf_id", ""),
        "mf_type": policy.get("mf_type", ""),
        "actor": body.get("actor", "api"),
        "reason": body.get("takeover_reason") or body.get("reason", ""),
        "ts": _utc_now(),
    }
    if action == "none":
        takeover["outcome"] = "none"
        return takeover
    if not task_id:
        takeover["outcome"] = "no_task_id"
        return takeover

    task_row = conn.execute(
        "SELECT task_id, status, execution_status, metadata_json FROM tasks "
        "WHERE project_id = ? AND task_id = ?",
        (project_id, task_id),
    ).fetchone()
    if not task_row:
        takeover["outcome"] = "task_missing"
        return takeover

    prior_status = str(_row_get(task_row, "status", "") or "")
    prior_exec = str(_row_get(task_row, "execution_status", prior_status) or prior_status)
    takeover["prior_status"] = prior_status
    takeover["prior_execution_status"] = prior_exec

    task_meta = backlog_runtime.parse_json_object(_row_get(task_row, "metadata_json", "{}"))
    task_meta["mf_takeover"] = takeover
    task_meta["mf_superseded"] = True
    task_meta["mf_type"] = policy.get("mf_type", "")
    task_meta["bug_id"] = task_meta.get("bug_id") or bug_id

    terminal = {"succeeded", "failed", "cancelled", "timed_out", "design_mismatch"}
    if prior_exec in terminal or prior_status in terminal:
        conn.execute(
            "UPDATE tasks SET metadata_json = ?, updated_at = ? WHERE task_id = ?",
            (json.dumps(task_meta, ensure_ascii=False), _utc_now(), task_id),
        )
        takeover["outcome"] = "already_terminal"
        return takeover

    if action == "cancel_current_chain":
        new_status = "cancelled"
        error_message = takeover["reason"] or "Cancelled by MF takeover"
        conn.execute(
            """UPDATE tasks
               SET status = ?, execution_status = ?, completed_at = ?,
                   updated_at = ?, error_message = ?, metadata_json = ?
               WHERE task_id = ?""",
            (
                new_status,
                new_status,
                _utc_now(),
                _utc_now(),
                error_message,
                json.dumps(task_meta, ensure_ascii=False),
                task_id,
            ),
        )
    else:
        new_status = "observer_hold"
        conn.execute(
            """UPDATE tasks
               SET status = ?, execution_status = ?, updated_at = ?,
                   error_message = ?, metadata_json = ?
               WHERE task_id = ?""",
            (
                new_status,
                new_status,
                _utc_now(),
                takeover["reason"] or "Held by MF takeover",
                json.dumps(task_meta, ensure_ascii=False),
                task_id,
            ),
        )
    takeover["outcome"] = new_status
    return takeover


# ---------------------------------------------------------------------------
# SQLite BUSY retry helper
# ---------------------------------------------------------------------------
_BUSY_RETRY_DELAYS = (0.5, 1.0, 2.0)  # seconds between attempts 1→2, 2→3


def _retry_on_busy(fn, *args, **kwargs):
    """Call *fn* up to 3 times, retrying on SQLITE_BUSY / 'database is locked'.

    Uses an exponential-style back-off: 0.5 s → 1 s → 2 s between attempts.
    Intended for short write transactions (version-update, version-sync).

    Args:
        fn: Callable that performs the SQLite operation.  It must be
            idempotent or use INSERT OR REPLACE semantics so retries are safe.
        *args / **kwargs: Forwarded verbatim to *fn*.

    Returns:
        The return value of *fn* on success.

    Raises:
        sqlite3.OperationalError: Re-raised after all 3 attempts are exhausted.
    """
    last_exc = None
    for attempt, delay in enumerate(_BUSY_RETRY_DELAYS, start=1):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc).lower():
                last_exc = exc
                time.sleep(delay)
            else:
                raise
    # Final attempt (no sleep after this one)
    try:
        return fn(*args, **kwargs)
    except sqlite3.OperationalError:
        raise last_exc


def _acquire_pid_lock():
    """Write PID lockfile. Kill old process if still alive."""
    lock_dir = os.path.join(
        os.environ.get("SHARED_VOLUME_PATH",
                        os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "shared-volume")),
        "codex-tasks", "state")
    os.makedirs(lock_dir, exist_ok=True)
    lock_path = os.path.join(lock_dir, "governance.pid")

    # Check old PID
    if os.path.exists(lock_path):
        try:
            old_pid = int(open(lock_path).read().strip())
            if old_pid != os.getpid():
                os.kill(old_pid, signal.SIGTERM)
                import logging
                logging.getLogger(__name__).info("Killed old governance process PID %d", old_pid)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass  # Old process already dead

    # Write new PID
    with open(lock_path, "w") as f:
        f.write(str(os.getpid()))

# --- Route Registry ---
ROUTES = []


def route(method: str, path: str):
    def decorator(fn):
        ROUTES.append((method, path, fn))
        return fn
    return decorator


class _StreamedResponse:
    """Sentinel returned by handlers that wrote the full response themselves
    (e.g. the SSE /events/stream endpoint). _handle skips _respond when it
    sees this so headers/body aren't written twice."""
    __slots__ = ()


STREAMED_RESPONSE = _StreamedResponse()


# Regex once at module import time — _emit_dashboard_changed runs on the hot
# path for every graph-governance mutation, no point recompiling per call.
_GRAPH_GOV_PATH_PID = re.compile(r"^/api/graph-governance/([^/]+)/")


def _emit_dashboard_changed(path: str, method: str) -> None:
    """Fan out a 'dashboard.changed' event for any successful POST/DELETE
    under /api/graph-governance/<project_id>/... so SSE clients can refetch.

    Intentionally lightweight: parses project_id from the path, slices off
    the query string, and publishes via the global EventBus. Failures are
    swallowed by the caller; this is best-effort live-sync, not a contract.
    """
    try:
        parsed = urlparse(path)
        clean_path = parsed.path
        match = _GRAPH_GOV_PATH_PID.match(clean_path)
        if not match:
            return
        project_id = match.group(1)
        from . import event_bus
        event_bus._bus.publish("dashboard.changed", {
            "project_id": project_id,
            "path": clean_path,
            "method": method,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        # Live-sync is best-effort — never let it surface to callers.
        pass


class GovernanceHandler(BaseHTTPRequestHandler):
    """HTTP request handler with routing and middleware."""

    CORS_HEADERS = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": (
            "Content-Type, Authorization, X-Gov-Token, Idempotency-Key, X-Requested-With"
        ),
        "Access-Control-Expose-Headers": "X-Request-Id",
    }

    def _find_handler(self, method: str):
        path = urlparse(self.path).path.rstrip("/")
        for m, prefix, handler in ROUTES:
            if m != method:
                continue
            # Exact match or parameterized match
            if path == prefix:
                return handler, {}, ""
            # Simple path parameter matching: /api/wf/{project_id}/...
            parts_route = prefix.split("/")
            parts_path = path.split("/")
            if len(parts_route) != len(parts_path):
                continue
            params = {}
            match = True
            for rp, pp in zip(parts_route, parts_path):
                if rp.startswith("{") and rp.endswith("}"):
                    params[rp[1:-1]] = pp
                elif rp != pp:
                    match = False
                    break
            if match:
                return handler, params, ""
        return None, {}, ""

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _query_params(self) -> dict:
        parsed = urlparse(self.path)
        return {k: v[0] if len(v) == 1 else v for k, v in parse_qs(parsed.query).items()}

    def _respond(self, code: int, body: dict, extra_headers: dict | None = None):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            headers = dict(self.CORS_HEADERS)
            if extra_headers:
                headers.update(extra_headers)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
            # observer-hotfix 2026-04-25: Windows clients drop connections
            # mid-write (gateway timeouts, executor restarts). Don't let
            # the connection death propagate up the request thread and
            # crash the gov server. Just log and move on.
            log.debug("client connection dropped during _respond: %s", e)

    def _respond_bytes(
        self,
        code: int,
        payload: bytes,
        content_type: str,
        extra_headers: dict | None = None,
    ):
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            headers = dict(self.CORS_HEADERS)
            if extra_headers:
                headers.update(extra_headers)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
            log.debug("client connection dropped during _respond_bytes: %s", e)

    def _serve_dashboard_static(self) -> bool:
        resolved = _resolve_dashboard_static_request(self.path)
        if not resolved.get("handled"):
            return False
        payload = resolved.get("body")
        if payload is None:
            static_path = resolved.get("path")
            if not isinstance(static_path, Path):
                payload = b"Not found"
            else:
                try:
                    payload = static_path.read_bytes()
                except OSError:
                    resolved = {
                        **resolved,
                        "status": 404,
                        "content_type": "text/plain; charset=utf-8",
                        "cache_control": "no-store",
                    }
                    payload = b"Not found"
        self._respond_bytes(
            int(resolved.get("status") or 200),
            payload,
            str(resolved.get("content_type") or "application/octet-stream"),
            {"Cache-Control": str(resolved.get("cache_control") or "no-store")},
        )
        return True

    def _handle(self, method: str):
        request_id = f"req-{uuid.uuid4().hex[:12]}"
        handler, path_params, _ = self._find_handler(method)
        if not handler:
            if method == "GET" and self._serve_dashboard_static():
                return
            self._respond(404, {"error": "not_found", "message": "Endpoint not found"})
            return
        try:
            ctx = RequestContext(
                handler=self,
                method=method,
                path_params=path_params,
                query=self._query_params(),
                body=self._read_body() if method == "POST" else {},
                request_id=request_id,
                token=self.headers.get("X-Gov-Token", ""),
                idem_key=self.headers.get("Idempotency-Key", ""),
            )
            result = handler(ctx)
            # Streaming handlers (SSE) write headers + body directly via
            # self.wfile and return the STREAMED_RESPONSE sentinel; skip the
            # normal JSON response path so we don't double-write.
            if isinstance(result, _StreamedResponse):
                return
            if isinstance(result, tuple) and len(result) == 3:
                code, body, extra_headers = result
            elif isinstance(result, tuple):
                # Support both (code, body) and (body, code) return styles
                if isinstance(result[0], int):
                    code, body = result[0], result[1]
                else:
                    body, code = result[0], result[1]
                extra_headers = None
            else:
                code, body = 200, result
                extra_headers = None
            body["request_id"] = request_id
            self._respond(code, body, extra_headers)
            # Dashboard live-sync: any successful mutating call against the
            # graph-governance namespace fans out a 'dashboard.changed' event
            # so connected SSE clients can debounce-refetch. Cheap, in-process,
            # best-effort — failures are swallowed.
            try:
                if (
                    method in ("POST", "DELETE")
                    and 200 <= code < 300
                    and "/api/graph-governance/" in self.path
                ):
                    _emit_dashboard_changed(self.path, method)
            except Exception:
                pass
        except GovernanceError as e:
            body = e.to_dict()
            body["request_id"] = request_id
            self._respond(e.status, body)
        except Exception as e:
            traceback.print_exc()
            self._respond(500, {
                "error": "internal_error",
                "message": str(e),
                "request_id": request_id,
            })

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_OPTIONS(self):
        try:
            self.send_response(204)
            for k, v in self.CORS_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Access-Control-Max-Age", "86400")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
            log.debug("client connection dropped during CORS preflight: %s", e)

    def log_message(self, format, *args):
        pass  # Suppress default logging


class RequestContext:
    """Encapsulates a single request's state."""
    def __init__(self, handler, method, path_params, query, body, request_id, token, idem_key):
        self.handler = handler
        self.method = method
        self.path_params = path_params
        self.query = query
        self.body = body
        self.request_id = request_id
        self.token = token
        self.idem_key = idem_key
        self._session = None
        self._conn = None

    def get_project_id(self) -> str:
        raw = self.path_params.get("project_id", self.body.get("project_id", ""))
        return project_service._normalize_project_id(raw) if raw else raw

    def require_auth(self, conn) -> dict:
        """Authenticate and return session. Caches result.

        Token-free mode: when no token is provided, returns a default
        coordinator session so all APIs work without authentication.
        Tokens still work if provided (for backward compatibility).
        """
        if self._session is None:
            if not self.token:
                # Anonymous access — full coordinator permissions
                project_id = self.get_project_id()
                self._session = {
                    "session_id": "anonymous",
                    "principal_id": "anonymous",
                    "project_id": project_id,
                    "role": "coordinator",
                    "scope": [],
                    "token": "",
                    "permissions": ["*"],
                }
            else:
                self._session = role_service.authenticate(conn, self.token)
        return self._session


# ============================================================
# ROUTES
# ============================================================

# --- Init (one-time project initialization) ---

@route("POST", "/api/init")
def handle_init(ctx: RequestContext):
    """Human calls this once to create project + get coordinator token.
    Repeat call without password → 403.
    Repeat call with correct password → reset coordinator token.
    """
    result = project_service.init_project(
        project_id=ctx.body.get("project_id", ctx.body.get("project", "")),
        password=ctx.body.get("password", ""),
        project_name=ctx.body.get("project_name", ctx.body.get("name", "")),
        workspace_path=ctx.body.get("workspace_path", ""),
    )
    return 201, result


# --- Project ---


@route("POST", "/api/project/bootstrap")
def handle_project_bootstrap(ctx: RequestContext):
    """Bootstrap a project from workspace (R1).

    Body: {
        "workspace_path": "/path/to/project" (required),
        "project_name": "my-project" (optional),
        "config_override": {"graph": {"exclude_paths": [], "ignore_globs": []}} (optional),
        "scan_depth": 3 (optional),
        "exclude_patterns": [] (optional),
    }
    Returns: {project_id, graph_stats, config, preflight, warning?}
    """
    workspace_path = ctx.body.get("workspace_path", "").strip()
    if not workspace_path:
        return 400, {"error": "workspace_path is required"}

    try:
        result = project_service.bootstrap_project(
            workspace_path=workspace_path,
            project_name=ctx.body.get("project_name", ""),
            config_override=ctx.body.get("config_override"),
            scan_depth=ctx.body.get("scan_depth", 3),
            exclude_patterns=ctx.body.get("exclude_patterns"),
        )
        return 200, result
    except Exception as e:
        return 400, {"error": str(e)}


@route("POST", "/api/local/choose-directory")
def handle_local_choose_directory(ctx: RequestContext):
    """Open a local directory picker for the dashboard import form.

    Plain browser directory APIs intentionally do not expose absolute paths.
    Since this dashboard is a local plugin surface, the governance process owns
    the native picker and returns a path that the bootstrap API can consume.
    """
    initial_path = str(ctx.body.get("initial_path") or ctx.body.get("workspace_path") or "").strip()
    title = str(ctx.body.get("title") or "Choose project directory").strip() or "Choose project directory"
    timeout_seconds = _clamped_float(ctx.body.get("timeout_seconds"), default=12.0, minimum=3.0, maximum=60.0)
    try:
        selected = _open_local_directory_picker(
            initial_path=initial_path,
            title=title,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        return {
            "ok": False,
            "selected": False,
            "path": "",
            "manual_entry": True,
            "error": str(exc),
        }
    if not selected:
        return {
            "ok": True,
            "selected": False,
            "path": "",
            "manual_entry": True,
        }
    return {
        "ok": True,
        "selected": True,
        "path": selected,
        "manual_entry": False,
    }


@route("GET", "/api/project/list")
def handle_project_list(ctx: RequestContext):
    return {"projects": project_service.list_projects()}


@route("GET", "/api/projects")
def handle_projects_list(ctx: RequestContext):
    """List registered projects for dashboard project switching."""
    return {"ok": True, "projects": project_service.list_projects()}


@route("GET", "/api/projects/{project_id}/raw-requirements")
def handle_project_raw_requirements_list(ctx: RequestContext):
    project_id = ctx.get_project_id()
    status = ctx.query.get("status", "")
    try:
        limit = int(ctx.query.get("limit", 200) or 200)
    except (TypeError, ValueError):
        limit = 200
    conn = get_connection(project_id)
    rows = raw_requirement.list_raw_requirements(
        conn,
        project_id=project_id,
        status=status or None,
        limit=limit,
    )
    return {
        "ok": True,
        "project_id": project_id,
        "raw_requirements": rows,
        "count": len(rows),
        "lane_counts": raw_requirement.lane_counts(conn, project_id=project_id),
    }


@route("POST", "/api/projects/{project_id}/raw-requirements")
def handle_project_raw_requirement_create(ctx: RequestContext):
    project_id = ctx.get_project_id()
    conn = get_connection(project_id)
    try:
        row = raw_requirement.create_raw_requirement(
            conn,
            project_id=project_id,
            raw_text=str(ctx.body.get("raw_text") or ctx.body.get("text") or ""),
            source=str(ctx.body.get("source") or ""),
            session_id=str(ctx.body.get("session_id") or ""),
            captured_by=str(ctx.body.get("captured_by") or ctx.body.get("actor") or ""),
            metadata=ctx.body.get("metadata") if isinstance(ctx.body.get("metadata"), dict) else {},
            raw_id=str(ctx.body.get("raw_id") or "") or None,
        )
    except ValueError as exc:
        return 400, {"ok": False, "error": "invalid_raw_requirement", "message": str(exc)}
    return 201, {
        "ok": True,
        "project_id": project_id,
        "raw_requirement": row,
        "created_backlog": False,
    }


@route("POST", "/api/projects/{project_id}/raw-requirements/{raw_id}/status")
def handle_project_raw_requirement_status(ctx: RequestContext):
    project_id = ctx.get_project_id()
    raw_id = unquote(str(ctx.path_params.get("raw_id") or ""))
    conn = get_connection(project_id)
    try:
        row = raw_requirement.update_status(
            conn,
            project_id=project_id,
            raw_id=raw_id,
            new_status=str(ctx.body.get("status") or ""),
            note=ctx.body.get("note") if "note" in ctx.body else None,
            promoted_bug_id=(
                str(ctx.body.get("promoted_bug_id") or ctx.body.get("bug_id") or "")
                if ("promoted_bug_id" in ctx.body or "bug_id" in ctx.body)
                else None
            ),
        )
    except LookupError as exc:
        return 404, {"ok": False, "error": "raw_requirement_not_found", "message": str(exc)}
    except ValueError as exc:
        return 400, {"ok": False, "error": "invalid_raw_requirement_status", "message": str(exc)}
    return {"ok": True, "project_id": project_id, "raw_requirement": row}


@route("GET", "/api/projects/{project_id}/project-inbox")
def handle_project_inbox(ctx: RequestContext):
    project_id = ctx.get_project_id()
    conn = get_connection(project_id)
    backlog_lanes = _project_inbox_backlog_lanes(conn)
    raw_rows = raw_requirement.list_raw_requirements(
        conn,
        project_id=project_id,
        status=raw_requirement.STATUS_RAW_INBOX,
        limit=50,
    )
    confirmation_rows = raw_requirement.list_raw_requirements(
        conn,
        project_id=project_id,
        status=raw_requirement.STATUS_NEEDS_CONFIRMATION,
        limit=50,
    )
    counts = raw_requirement.lane_counts(conn, project_id=project_id)
    return {
        "ok": True,
        "project_id": project_id,
        "homepage_view": "project_inbox",
        "lanes": {
            "raw_inbox": {
                "count": counts.get(raw_requirement.STATUS_RAW_INBOX, 0),
                "items": raw_rows,
            },
            "needs_confirmation": {
                "count": counts.get(raw_requirement.STATUS_NEEDS_CONFIRMATION, 0),
                "items": confirmation_rows,
            },
            "ready_backlog": backlog_lanes["ready_backlog"],
            "in_progress": backlog_lanes["in_progress"],
            "review_needed": backlog_lanes["review_needed"],
            "done": backlog_lanes["done"],
        },
    }


def _project_inbox_backlog_lanes(conn: sqlite3.Connection, *, item_limit: int = 50) -> dict[str, dict]:
    """Build operator-facing backlog lanes for the Project Inbox homepage."""
    lanes = {
        "ready_backlog": {"count": 0, "items": [], "source": "backlog"},
        "in_progress": {"count": 0, "items": [], "source": "backlog"},
        "review_needed": {"count": 0, "items": [], "source": "backlog"},
        "done": {"count": 0, "items": [], "source": "backlog"},
    }
    try:
        rows = conn.execute(
            "SELECT * FROM backlog_bugs ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return lanes
        raise

    def add(lane: str, item: dict) -> None:
        bucket = lanes[lane]
        bucket["count"] += 1
        if len(bucket["items"]) < item_limit:
            bucket["items"].append(item)

    for row in rows:
        raw = dict(row)
        bug = _backlog_compact_bug(row)
        bug["kind"] = "backlog"
        status = str(raw.get("status") or "OPEN").upper()
        runtime_state = str(raw.get("runtime_state") or "").strip().lower()
        chain_stage = str(raw.get("chain_stage") or "").strip().lower()
        current_task_id = str(raw.get("current_task_id") or "").strip()
        failure_reason = str(raw.get("last_failure_reason") or "").strip()

        if status in _BACKLOG_CLOSED_STATUSES:
            add("done", bug)
        elif status in {"REVIEW", "QA", "READY_FOR_REVIEW", "VERIFY", "VERIFICATION"}:
            add("review_needed", bug)
        elif runtime_state in {"blocked", "failed"} or failure_reason:
            add("review_needed", bug)
        elif runtime_state or chain_stage or current_task_id:
            add("in_progress", bug)
        else:
            add("ready_backlog", bug)
    return lanes


@route("GET", "/api/projects/{project_id}/git-refs")
def handle_project_git_refs(ctx: RequestContext):
    """Return current git branch/ref information for a registered project."""
    project_id = ctx.get_project_id()
    try:
        root = _graph_governance_project_root(project_id, {})
    except Exception as exc:
        return 404, {"error": f"project root not found: {exc}"}
    refs = _git_refs_for_root(root)
    project = project_service.get_project(project_id) or {}
    selected_ref = str(project.get("selected_ref") or "").strip()
    if not selected_ref:
        selected_ref = refs.get("current_branch") or refs.get("head_commit") or ""
    return {
        "ok": True,
        "project_id": project_id,
        "workspace_path": str(root),
        "selected_ref": selected_ref,
        **refs,
    }


@route("POST", "/api/projects/{project_id}/git-ref")
def handle_project_git_ref_select(ctx: RequestContext):
    """Persist the dashboard-selected branch/ref without checking it out."""
    project_id = ctx.get_project_id()
    selected_ref = str(
        ctx.body.get("selected_ref")
        or ctx.body.get("ref")
        or ctx.body.get("branch")
        or ""
    ).strip()
    if not selected_ref:
        return 400, {"error": "selected_ref is required"}
    try:
        root = _graph_governance_project_root(project_id, {})
    except Exception as exc:
        return 404, {"error": f"project root not found: {exc}"}
    if not _git_ref_exists(root, selected_ref):
        return 400, {"error": f"unknown git ref: {selected_ref}"}
    project = project_service.update_project_metadata(
        project_id,
        {
            "selected_ref": selected_ref,
            "selected_ref_updated_at": _utc_now(),
            "selected_ref_updated_by": str(ctx.body.get("actor") or "dashboard"),
        },
    )
    refs = _git_refs_for_root(root)
    return {
        "ok": True,
        "project_id": project_id,
        "workspace_path": str(root),
        "selected_ref": project.get("selected_ref", selected_ref),
        "project": project,
        **refs,
    }


@route("POST", "/api/projects/register")
def handle_project_register(ctx: RequestContext):
    """Register a project workspace with config validation.

    Body: {"workspace_path": "/path/to/project"}
    Returns: {"project_id", "config_hash", "registered": true}
    """
    import sys as _sys
    _agent_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)))
    if _agent_dir not in _sys.path:
        _sys.path.insert(0, _agent_dir)

    workspace_path = ctx.body.get("workspace_path", "").strip()
    if not workspace_path:
        return 400, {"error": "workspace_path is required"}

    from pathlib import Path
    ws = Path(workspace_path)

    # In Docker, host paths are not accessible — skip path validation
    # but still validate config if accessible
    try:
        from project_config import effective_graph_exclude_roots, load_project_config, validate_commands
        config = load_project_config(ws)
    except (ValueError, FileNotFoundError) as e:
        # Path not accessible (Docker) or no config — try /workspace mount
        workspace_mount = Path("/workspace")
        if workspace_mount.exists():
            try:
                config = load_project_config(workspace_mount)
            except (ValueError, FileNotFoundError) as e2:
                return 400, {"error": f"config not found: {e2}"}
        else:
            return 400, {"error": f"config not found: {e}"}

    # Command safety
    cmd_violations = validate_commands(config)
    if cmd_violations:
        return 400, {"error": "unsafe commands", "violations": cmd_violations}

    # Check uniqueness
    existing = project_service.get_project(config.project_id)
    if existing and existing.get("workspace_path") and existing["workspace_path"] != str(ws):
        return 409, {"error": f"project_id '{config.project_id}' already registered to different workspace"}

    # Register in governance
    project_id = config.project_id
    try:
        if not existing:
            project_service.init_project(
                project_id=project_id,
                password="auto-registered",
                project_name=config.project_id,
                workspace_path=str(ws),
            )
    except Exception as e:
        # May already exist with different password — that's OK
        if "already exists" not in str(e).lower():
            return 500, {"error": f"registration failed: {e}"}

    # workspace_registry removed — workspace info stored in governance projects.json
    try:
        project_service.set_project_config_metadata(
            project_id,
            project_service.project_config_to_metadata(config),
            source="workspace_config",
            actor="register",
        )
    except Exception:
        pass

    return 201, {
        "project_id": project_id,
        "config_hash": str(hash(str(config))),
        "registered": True,
        "language": config.language,
        "test_command": config.testing.unit_command,
        "deploy_strategy": config.deploy.strategy,
        "graph": {"effective_exclude_roots": effective_graph_exclude_roots(config)},
        "ai": {"routing": dict(getattr(config.ai, "routing", {}) or {})},
    }


def _project_workspace_for_config(project_id: str) -> Path:
    """Resolve a registered project workspace for dashboard config APIs."""
    try:
        return _graph_governance_project_root(project_id, {})
    except Exception:
        pass
    for project in project_service.list_projects():
        if project.get("project_id") == project_id and project.get("workspace_path"):
            return Path(str(project["workspace_path"])).resolve()
    if project_id == "aming-claw":
        return Path(__file__).resolve().parents[2]
    if Path("/workspace").exists():
        return Path("/workspace")
    raise ValidationError(f"no workspace registered for {project_id}")


def _normalize_project_config_payload(
    project_id: str,
    payload: dict,
    *,
    source: str,
    write_target: str = "aming-claw project registry",
    local_config_error: str = "",
) -> dict:
    out = dict(payload or {})
    out["project_id"] = str(out.get("project_id") or project_id)
    out.setdefault("language", "python")
    out.setdefault("testing", {})
    out.setdefault("build", {})
    out.setdefault("deploy", {})
    out.setdefault("governance", {})
    out.setdefault("graph", {})
    out.setdefault("ai", {})
    if not isinstance(out["ai"], dict):
        out["ai"] = {}
    out["ai"].setdefault("routing", {})
    out["config_source"] = source
    out["write_target"] = write_target
    if local_config_error:
        out["local_config_error"] = local_config_error
    return out


def _registry_project_config(project_id: str) -> tuple[dict, str]:
    entry = project_service.get_project(project_id) or {}
    payload = project_service.get_project_config_metadata(project_id)
    source = str(entry.get("project_config_source") or "aming_claw_registry")
    return payload, source


def _resolved_project_config_payload(
    project_id: str,
    root: Path,
    *,
    allow_generated: bool = True,
) -> dict:
    """Resolve project config without writing into the governed workspace."""
    local_error = ""
    try:
        from project_config import load_project_config

        config = load_project_config(root)
        payload = project_service.project_config_to_metadata(config)
        source = "workspace_config"
    except Exception as exc:
        local_error = str(exc)
        payload, source = _registry_project_config(project_id)
        if not payload:
            if not allow_generated:
                raise
            from project_config import generate_default_config

            config = generate_default_config(str(root), project_id)
            payload = project_service.project_config_to_metadata(config)
            payload["project_id"] = project_id
            source = "generated_default"

    registry_payload, registry_source = _registry_project_config(project_id)
    registry_routing = (
        registry_payload.get("ai", {}).get("routing", {})
        if isinstance(registry_payload.get("ai"), dict)
        else {}
    )
    if registry_routing:
        ai_payload = payload.get("ai") if isinstance(payload.get("ai"), dict) else {}
        ai_payload["routing"] = dict(registry_routing)
        payload["ai"] = ai_payload
        source = registry_source or "aming_claw_registry"

    return _normalize_project_config_payload(
        project_id,
        payload,
        source=source,
        write_target="aming-claw project registry",
        local_config_error=local_error if source != "workspace_config" else "",
    )


@route("GET", "/api/projects/{project_id}/config")
def handle_project_config(ctx: RequestContext):
    """Return resolved project config."""
    import sys as _sys
    _agent_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)))
    if _agent_dir not in _sys.path:
        _sys.path.insert(0, _agent_dir)

    project_id = ctx.get_project_id()
    try:
        root = _project_workspace_for_config(project_id)
        payload = _resolved_project_config_payload(project_id, root)
        return payload
    except Exception as e:
        return 404, {"error": f"config not found: {e}"}


@route("GET", "/api/projects/{project_id}/e2e/config")
def handle_project_e2e_config(ctx: RequestContext):
    """Return the resolved E2E suite registry for a project."""
    project_id = ctx.get_project_id()
    try:
        root = _project_workspace_for_config(project_id)
        config = _resolved_project_config_payload(project_id, root)
        return {
            "ok": True,
            "project_id": config.get("project_id") or project_id,
            "workspace_path": str(root),
            "e2e": ((config.get("testing") or {}).get("e2e") or {}),
            "config_source": config.get("config_source", ""),
        }
    except Exception as exc:
        return 404, {"error": f"e2e config not found: {exc}"}


def _ai_tool_live_check(
    provider: str,
    resolved: str,
    model: str,
) -> dict[str, str]:
    if provider != "anthropic" or not resolved:
        return {"auth_status": "unknown", "error": ""}
    cmd = [resolved, "-p", "--output-format", "json"]
    if model:
        cmd.extend(["--model", model])
    cmd.append('Return exactly {"ok": true}')
    env = {
        k: v for k, v in os.environ.items()
        if k not in {
            "CLAUDECODE",
            "CLAUDE_CODE_ENTRYPOINT",
            "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
            "CLAUDE_CODE_EXECPATH",
            "CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH",
            "CLAUDE_CODE_EMIT_TOOL_USE_SUMMARIES",
            "CLAUDE_CODE_ENABLE_ASK_USER_QUESTION_TOOL",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_API_KEY",
        }
    }
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            env=env,
        )
    except Exception as exc:
        return {"auth_status": "smoke_error", "error": str(exc)[:400]}
    raw = (proc.stdout or proc.stderr or "").strip()
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {}
    if proc.returncode == 0 and isinstance(parsed, dict) and parsed.get("is_error") is not True:
        return {"auth_status": "live_ok", "error": ""}
    message = ""
    if isinstance(parsed, dict):
        message = str(parsed.get("result") or parsed.get("error") or parsed.get("message") or "")
    return {"auth_status": "live_failed", "error": (message or raw or "live AI check failed")[:400]}


def _semantic_route_from_project_config(project_config: dict) -> dict[str, str]:
    ai_config = project_config.get("ai") if isinstance(project_config.get("ai"), dict) else {}
    routing = ai_config.get("routing") if isinstance(ai_config.get("routing"), dict) else {}
    route = routing.get("semantic") if isinstance(routing.get("semantic"), dict) else {}
    return {
        "provider": str(route.get("provider") or "").strip(),
        "model": str(route.get("model") or "").strip(),
    }


def _ai_tool_health(
    *,
    live_check: bool = False,
    semantic_route: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Detect local CLI tools used by dashboard-configured AI providers."""
    health: dict[str, Any] = {}
    for provider, requirement in AI_PROVIDER_REQUIREMENTS.items():
        env_var = str(requirement["env_var"])
        configured = os.environ.get(env_var, "").strip()
        candidates = [configured] if configured else []
        candidates.append(str(requirement["command"]))
        resolved = ""
        source = ""
        for candidate in candidates:
            if not candidate:
                continue
            path = candidate if os.path.isabs(candidate) else shutil.which(candidate)
            if path:
                resolved = path
                source = env_var if configured and candidate == configured else "PATH"
                break
        status = "missing"
        version = ""
        error = ""
        if resolved:
            status = "detected"
            try:
                proc = subprocess.run(
                    [resolved, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                first_line = (proc.stdout or proc.stderr or "").strip().splitlines()[0:1]
                version = first_line[0] if first_line else ""
                if proc.returncode != 0:
                    status = "version_error"
                    error = (proc.stderr or proc.stdout or "").strip()[:400]
            except Exception as exc:
                status = "version_error"
                error = str(exc)
        auth_status = "unknown"
        if live_check and resolved:
            route = semantic_route or {}
            route_provider = str(route.get("provider") or "").strip()
            route_model = str(route.get("model") or "").strip()
            if provider == route_provider:
                live = _ai_tool_live_check(provider, resolved, route_model)
                auth_status = live.get("auth_status", "unknown")
                if auth_status != "live_ok":
                    status = "auth_error"
                    error = live.get("error", error)
        health[provider] = {
            "provider": provider,
            "label": requirement["label"],
            "runtime": requirement["runtime"],
            "command": requirement["command"],
            "env_var": env_var,
            "path": resolved,
            "source": source,
            "status": status,
            "version": version,
            "auth_status": auth_status,
            "error": error,
        }
    return health


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/e2e/evidence")
def handle_graph_governance_snapshot_e2e_evidence(ctx: RequestContext):
    """Record E2E evidence bound to a graph snapshot."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.e2e.evidence")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        from .e2e_evidence import record_e2e_evidence

        return record_e2e_evidence(conn, project_id, snapshot_id, ctx.body)
    except KeyError as exc:
        return 404, {"error": str(exc)}
    except Exception as exc:
        return 400, {"error": f"e2e evidence record failed: {exc}"}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/e2e/impact")
def handle_graph_governance_snapshot_e2e_impact(ctx: RequestContext):
    """Plan which E2E suites are current, stale, missing, or blocked."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    conn = get_connection(project_id)
    try:
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        root = _graph_governance_project_root(project_id, {})
        from project_config import e2e_config_to_dict, load_project_config
        from .e2e_evidence import plan_e2e_impact

        config = load_project_config(root)
        changed_raw = ctx.query.get("changed_files") or ctx.query.get("changed_file") or ""
        node_raw = ctx.query.get("changed_node_ids") or ctx.query.get("changed_node_id") or ""

        def _split(value):
            if isinstance(value, list):
                return value
            return [item.strip() for item in str(value or "").split(",") if item.strip()]

        return plan_e2e_impact(
            conn,
            project_id,
            snapshot_id,
            e2e_config_to_dict(config.testing.e2e),
            changed_files=_split(changed_raw),
            changed_node_ids=_split(node_raw),
        )
    except KeyError as exc:
        return 404, {"error": str(exc)}
    except Exception as exc:
        return 400, {"error": f"e2e impact planning failed: {exc}"}
    finally:
        conn.close()


@route("GET", "/api/projects/{project_id}/ai-config")
def handle_project_ai_config(ctx: RequestContext):
    """Return AI/model routing config for dashboard operators."""
    import sys as _sys
    _agent_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)))
    if _agent_dir not in _sys.path:
        _sys.path.insert(0, _agent_dir)

    project_id = ctx.get_project_id()
    try:
        root = _graph_governance_project_root(project_id, {})
    except Exception as exc:
        return 404, {"error": f"project root not found: {exc}"}

    pipeline = {}
    pipeline_error = ""
    role_routing = {}
    try:
        from pipeline_config import get_effective_pipeline_config, resolve_role_config

        pipeline = get_effective_pipeline_config()
        for role in ("pm", "dev", "tester", "qa", "coordinator", "gatekeeper", "observer"):
            role_routing[role] = resolve_role_config(role, pipeline)
    except Exception as exc:
        pipeline_error = str(exc)

    role_configs = {}
    role_config_error = ""
    try:
        from .role_config import load_all_role_configs

        loaded = load_all_role_configs(project_id=project_id)
        for role, config in loaded.items():
            role_configs[role] = {
                "max_turns": config.max_turns,
                "tools": list(config.tools or []),
                "task_type_alias": config.task_type_alias or "",
            }
    except Exception as exc:
        role_config_error = str(exc)

    semantic = {}
    semantic_error = ""
    try:
        from .reconcile_semantic_config import load_semantic_enrichment_config

        semantic_config = load_semantic_enrichment_config(project_root=root)
        semantic = {
            "provider": semantic_config.provider,
            "model": semantic_config.model,
            "analyzer_role": semantic_config.analyzer_role,
            "chain_role": semantic_config.chain_role,
            "use_ai_default": semantic_config.use_ai_default,
            "automation_policy": {
                "semantic_mode": semantic_config.automation_policy.semantic_mode,
                "feedback_review_mode": semantic_config.automation_policy.feedback_review_mode,
                "graph_apply_mode": semantic_config.automation_policy.graph_apply_mode,
                "review_workers": semantic_config.automation_policy.review_workers,
            },
            "job_profiles": {
                name: {
                    "analyzer_role": profile.analyzer_role,
                    "provider": profile.provider,
                    "model": profile.model,
                }
                for name, profile in semantic_config.job_profiles.items()
            },
            "source_path": semantic_config.source_path,
            "override_path": semantic_config.override_path,
        }
    except Exception as exc:
        semantic_error = str(exc)

    project_config = {}
    project_config_error = ""
    project_config_source = ""
    write_target = "aming-claw project registry"
    try:
        project_config = _resolved_project_config_payload(project_id, root)
        project_config_source = str(project_config.get("config_source") or "")
        write_target = str(project_config.get("write_target") or write_target)
    except Exception as exc:
        project_config_error = str(exc)

    live_check = _query_bool(ctx.query, "live_check", False)
    semantic_route = _semantic_route_from_project_config(project_config)
    return {
        "project_id": project_id,
        "workspace_path": str(root),
        "project_config": project_config,
        "project_config_source": project_config_source,
        "project_config_error": project_config_error,
        "write_target": write_target,
        "pipeline": pipeline,
        "pipeline_error": pipeline_error,
        "role_routing": role_routing,
        "role_configs": role_configs,
        "role_config_error": role_config_error,
        "semantic": semantic,
        "semantic_error": semantic_error,
        "tool_health": _ai_tool_health(
            live_check=live_check,
            semantic_route=semantic_route,
        ),
        "model_catalog": {
            "providers": AI_PROVIDER_REQUIREMENTS,
            "models": AI_MODEL_CATALOG,
        },
        "read_only": False,
        "write_supported": True,
    }


@route("POST", "/api/projects/{project_id}/ai-config")
def handle_project_ai_config_update(ctx: RequestContext):
    """Update project-level ai.routing in Aming-claw's central registry."""
    project_id = ctx.get_project_id()
    routing = ctx.body.get("routing")
    if not isinstance(routing, dict):
        return 400, {"error": "routing object is required"}
    try:
        root = _project_workspace_for_config(project_id)
    except Exception as exc:
        return 404, {"error": f"project root not found: {exc}"}
    try:
        base_config = _resolved_project_config_payload(project_id, root)
        project_service.update_project_ai_routing_metadata(
            project_id,
            routing,
            base_config=base_config,
            actor=str(ctx.body.get("actor") or "dashboard"),
        )
    except Exception as exc:
        return 400, {"error": f"ai config update failed: {exc}"}
    payload = handle_project_ai_config(ctx)
    if isinstance(payload, tuple):
        return payload
    payload["ok"] = True
    payload["updated"] = True
    payload["read_only"] = False
    payload["write_supported"] = True
    payload["write_target"] = "aming-claw project registry"
    return payload


@route("POST", "/api/ai-output/{project_id}/submit")
def handle_ai_output_submit(ctx: RequestContext):
    """Submit one structured AI output envelope for MF/observer processing."""
    from . import ai_output_intake

    project_id = ctx.get_project_id()
    conn = get_connection(project_id)
    try:
        session = _require_graph_governance_operator(ctx, conn, "ai-output.submit")
        actor = str(ctx.body.get("actor") or session.get("principal_id") or "observer")
        result = ai_output_intake.submit_ai_output(
            conn,
            project_id,
            ctx.body,
            actor=actor,
            request_id=ctx.request_id,
            idempotency_key=ctx.idem_key,
        )
        conn.commit()
        return (200 if result.get("idempotent") else 201), result
    finally:
        conn.close()


@route("GET", "/api/ai-output/{project_id}/outputs")
def handle_ai_output_list(ctx: RequestContext):
    """List submitted AI output envelopes for observer inspection."""
    from . import ai_output_intake

    project_id = ctx.get_project_id()
    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "ai-output.read")
        outputs = ai_output_intake.list_ai_outputs(
            conn,
            project_id,
            task_type=str(ctx.query.get("task_type") or ""),
            status=str(ctx.query.get("status") or ""),
            producer=str(ctx.query.get("producer") or ""),
            target_id=str(ctx.query.get("target_id") or ""),
            limit=_query_int(ctx.query, "limit", 50),
            offset=_query_int(ctx.query, "offset", 0),
        )
        return {"ok": True, "project_id": project_id, "outputs": outputs, "count": len(outputs)}
    finally:
        conn.close()


@route("GET", "/api/ai-output/{project_id}/outputs/{output_id}")
def handle_ai_output_get(ctx: RequestContext):
    """Return one submitted AI output envelope by id."""
    from . import ai_output_intake

    project_id = ctx.get_project_id()
    output_id = unquote(str(ctx.path_params.get("output_id") or ""))
    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "ai-output.read")
        output = ai_output_intake.get_ai_output(conn, project_id, output_id)
        if not output:
            return 404, {"ok": False, "error": "ai_output_not_found", "output_id": output_id}
        return {"ok": True, "project_id": project_id, "output": output}
    finally:
        conn.close()


@route("GET", "/api/ai-output/{project_id}/queue")
def handle_ai_output_queue(ctx: RequestContext):
    """List queued AI outputs waiting for downstream processors/gates."""
    from . import ai_output_intake

    project_id = ctx.get_project_id()
    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "ai-output.read")
        queue = ai_output_intake.list_ai_output_queue(
            conn,
            project_id,
            task_type=str(ctx.query.get("task_type") or ""),
            status=str(ctx.query.get("status") or ""),
            limit=_query_int(ctx.query, "limit", 50),
            offset=_query_int(ctx.query, "offset", 0),
        )
        return {"ok": True, "project_id": project_id, "queue": queue, "count": len(queue)}
    finally:
        conn.close()


@route("POST", "/api/projects/{project_id}/explain")
def handle_project_explain(ctx: RequestContext):
    """Dry-run: explain what would happen for given changed files."""
    import sys as _sys
    _agent_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)))
    if _agent_dir not in _sys.path:
        _sys.path.insert(0, _agent_dir)

    project_id = ctx.get_project_id()
    changed_files = ctx.body.get("changed_files", [])
    try:
        from project_config import explain_config, load_project_config
        from pathlib import Path
        # Resolve workspace from governance project data
        proj_data = project_service.list_projects()
        ws_entry = None
        for p in proj_data:
            if p.get("project_id") == project_id and p.get("workspace_path"):
                ws_entry = {"path": p["workspace_path"]}
                break
        if ws_entry:
            config = load_project_config(Path(ws_entry['path']))
            # Build explain manually since explain_config uses registry
            from deploy_chain import detect_affected_services
            affected = detect_affected_services(changed_files, project_id=project_id) if changed_files else []
            return {
                "project_id": config.project_id,
                "test_command": config.testing.unit_command,
                "deploy_strategy": config.deploy.strategy,
                "affected_services": affected,
                "changed_files": changed_files,
            }
        else:
            ws = Path('/workspace')
            if ws.exists():
                config = load_project_config(ws)
                from deploy_chain import detect_affected_services
                affected = detect_affected_services(changed_files, project_id=project_id) if changed_files else []
                return {
                    "project_id": config.project_id,
                    "test_command": config.testing.unit_command,
                    "deploy_strategy": config.deploy.strategy,
                    "affected_services": affected,
                    "changed_files": changed_files,
                }
            else:
                return 404, {'error': f'no workspace registered for {project_id}'}
        return explain_config(project_id, changed_files=changed_files)
    except Exception as e:
        return 404, {"error": f"explain failed: {e}"}


# --- Role (coordinator assigns roles to other agents) ---

@route("POST", "/api/role/assign")
def handle_role_assign(ctx: RequestContext):
    """Coordinator assigns a role+token to another agent."""
    project_id = ctx.body.get("project_id", "")
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        result = project_service.assign_role(
            conn, project_id, session,
            principal_id=ctx.body.get("principal_id", ""),
            role=ctx.body.get("role", ""),
            scope=ctx.body.get("scope"),
        )
    return 201, result


@route("POST", "/api/role/revoke")
def handle_role_revoke(ctx: RequestContext):
    """Coordinator revokes an agent's session."""
    project_id = ctx.body.get("project_id", "")
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        result = project_service.revoke_role(
            conn, project_id, session,
            session_id=ctx.body.get("session_id", ""),
        )
    return result


@route("POST", "/api/role/heartbeat")
def handle_heartbeat(ctx: RequestContext):
    # Need to find which project this session belongs to
    # First authenticate to get session
    # We check all projects (or the session tells us)
    # For simplicity, authenticate against a known project
    project_id = ctx.body.get("project_id", "")
    if not project_id:
        # Try to find from token
        rc = get_redis()
        from .role_service import _hash_token
        token_hash = _hash_token(ctx.token)
        session_id = rc.get_session_by_token(token_hash)
        if session_id:
            cached = rc.get_cached_session(session_id)
            if cached:
                project_id = cached.get("project_id", "")

    if not project_id:
        from .errors import AuthError
        raise AuthError("Cannot determine project. Provide project_id or use a valid token.")

    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        result = role_service.heartbeat(
            conn, session["session_id"],
            ctx.body.get("status", "idle"),
        )
    return result


@route("GET", "/api/role/verify")
def handle_role_verify(ctx: RequestContext):
    """Verify a token and return session info. Used by Gateway for auth."""
    if not ctx.token:
        from .errors import AuthError
        raise AuthError("Missing token")

    # Try to find session from token across all projects
    rc = get_redis()
    from .role_service import _hash_token
    th = _hash_token(ctx.token)
    session_id = rc.get_session_by_token(th) if rc else None
    project_id = ""

    if session_id:
        cached = rc.get_cached_session(session_id)
        if cached:
            project_id = cached.get("project_id", "")

    if not project_id:
        # Fallback: scan projects
        for p in project_service.list_projects():
            try:
                with DBContext(p["project_id"]) as conn:
                    session = role_service.authenticate(conn, ctx.token)
                    return {
                        "valid": True,
                        "session_id": session["session_id"],
                        "principal_id": session.get("principal_id", ""),
                        "role": session.get("role", ""),
                        "project_id": p["project_id"],
                    }
            except Exception:
                continue
        from .errors import AuthError
        raise AuthError("Invalid token")

    with DBContext(project_id) as conn:
        session = role_service.authenticate(conn, ctx.token)
        return {
            "valid": True,
            "session_id": session["session_id"],
            "principal_id": session.get("principal_id", ""),
            "role": session.get("role", ""),
            "project_id": project_id,
        }


@route("GET", "/api/role/{project_id}/sessions")
def handle_list_sessions(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        sessions = role_service.list_sessions(conn, project_id)
    return {"sessions": sessions}


# --- Token ---

@route("POST", "/api/token/revoke")
def handle_token_revoke(ctx: RequestContext):
    """Revoke a refresh token."""
    refresh_token = ctx.body.get("refresh_token", "")
    if not refresh_token:
        from .errors import ValidationError
        raise ValidationError("refresh_token required")

    from . import token_service
    for p in project_service.list_projects():
        try:
            with DBContext(p["project_id"]) as conn:
                return token_service.revoke_refresh_token(conn, refresh_token)
        except Exception:
            continue
    from .errors import AuthError
    raise AuthError("Token not found")


@route("POST", "/api/token/rotate")
def handle_token_rotate(ctx: RequestContext):
    """DEPRECATED (v5): Use revoke + re-init instead.
    Removal timeline: deprecated since v5, scheduled for removal in v8.
    """
    # Deprecation headers: deprecated since v5, removal planned for v8
    _deprecation_headers = {
        "X-Deprecated-Since": "v5",
        "X-Removal-Date": "v8",
    }
    refresh_token = ctx.body.get("refresh_token", "")
    if not refresh_token:
        from .errors import ValidationError
        raise ValidationError("refresh_token required")

    from . import token_service
    for p in project_service.list_projects():
        try:
            with DBContext(p["project_id"]) as conn:
                result = token_service.rotate_refresh_token(conn, refresh_token)
                return 200, result, _deprecation_headers
        except Exception:
            continue
    from .errors import AuthError
    raise AuthError("Token not found")


# --- Agent Lifecycle ---

@route("POST", "/api/agent/register")
def handle_agent_register(ctx: RequestContext):
    """Register an agent and get a lease."""
    project_id = ctx.body.get("project_id", "")
    if not project_id:
        from .errors import ValidationError
        raise ValidationError("project_id required")

    from . import agent_lifecycle
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        return agent_lifecycle.register_agent(
            conn, project_id, session,
            expected_duration_sec=int(ctx.body.get("expected_duration_sec", 0)),
        )


@route("POST", "/api/agent/heartbeat")
def handle_agent_heartbeat(ctx: RequestContext):
    """Renew agent lease."""
    lease_id = ctx.body.get("lease_id", "")
    if not lease_id:
        from .errors import ValidationError
        raise ValidationError("lease_id required")

    from . import agent_lifecycle
    return agent_lifecycle.heartbeat(
        lease_id, status=ctx.body.get("status", "idle"),
    )


@route("POST", "/api/agent/deregister")
def handle_agent_deregister(ctx: RequestContext):
    """Deregister an agent."""
    lease_id = ctx.body.get("lease_id", "")
    if not lease_id:
        from .errors import ValidationError
        raise ValidationError("lease_id required")

    from . import agent_lifecycle
    return agent_lifecycle.deregister(lease_id)


@route("GET", "/api/agent/orphans")
def handle_agent_orphans(ctx: RequestContext):
    """List orphaned agents (expired leases)."""
    project_id = ctx.query.get("project_id", "")
    from . import agent_lifecycle
    orphans = agent_lifecycle.find_orphans(project_id or None)
    return {"orphans": orphans, "count": len(orphans)}


@route("POST", "/api/agent/cleanup")
def handle_agent_cleanup(ctx: RequestContext):
    """Clean up orphaned agents. Coordinator only."""
    project_id = ctx.body.get("project_id", "")
    if not project_id:
        from .errors import ValidationError
        raise ValidationError("project_id required")

    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        if session.get("role") != "coordinator":
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(session.get("role", ""), "agent.cleanup",
                                        {"detail": "Only coordinator can cleanup orphans"})

    from . import agent_lifecycle
    return agent_lifecycle.cleanup_orphans(project_id)


# --- Session Context ---

@route("POST", "/api/context/{project_id}/save")
def handle_context_save(ctx: RequestContext):
    """Save session context snapshot."""
    project_id = ctx.get_project_id()
    from . import session_context
    return session_context.save_snapshot(
        project_id, ctx.body.get("context", ctx.body),
        expected_version=ctx.body.get("expected_version"),
    )


@route("GET", "/api/context/{project_id}/load")
def handle_context_load(ctx: RequestContext):
    """Load session context snapshot."""
    project_id = ctx.get_project_id()
    from . import session_context
    data = session_context.load_snapshot(project_id)
    if data is None:
        return {"context": None, "exists": False}
    return {"context": data, "exists": True}


@route("POST", "/api/context/{project_id}/log")
def handle_context_log_append(ctx: RequestContext):
    """Append entry to session log."""
    project_id = ctx.get_project_id()
    from . import session_context
    content = ctx.body.get("content")
    if not isinstance(content, dict):
        content = {key: value for key, value in ctx.body.items() if key != "type"}
    return session_context.append_log(
        project_id,
        entry_type=ctx.body.get("type", "action"),
        content=content,
    )


@route("GET", "/api/context/{project_id}/log")
def handle_context_log_read(ctx: RequestContext):
    """Read session log entries."""
    project_id = ctx.get_project_id()
    from . import session_context
    entries = session_context.read_log(project_id, limit=int(ctx.query.get("limit", "50")))
    return {"entries": entries, "count": len(entries)}


@route("POST", "/api/context/{project_id}/assemble")
def handle_context_assemble(ctx: RequestContext):
    """Assemble context from dbservice for a task type."""
    project_id = ctx.get_project_id()
    task_type = ctx.body.get("task_type", "dev_general")
    token_budget = int(ctx.body.get("token_budget", 5000))

    import requests as http_requests
    dbservice_url = os.environ.get("DBSERVICE_URL", "")
    if not dbservice_url:
        return {"context": [], "degraded": True, "reason": "DBSERVICE_URL not set"}

    try:
        resp = http_requests.post(
            f"{dbservice_url}/assemble-context",
            json={"taskType": task_type, "scope": project_id, "tokenBudget": token_budget},
            timeout=5,
        )
        if resp.status_code == 200:
            return resp.json()
        return {"context": [], "degraded": True, "reason": f"dbservice returned {resp.status_code}"}
    except Exception as e:
        return {"context": [], "degraded": True, "reason": str(e)}


@route("POST", "/api/context/{project_id}/archive")
def handle_context_archive(ctx: RequestContext):
    """Archive context to long-term memory and clear."""
    project_id = ctx.get_project_id()
    from . import session_context
    return session_context.archive_context(project_id)


# --- Workflow ---

@route("POST", "/api/wf/{project_id}/import-graph")
def handle_import_graph(ctx: RequestContext):
    """Import acceptance graph from a markdown file.

    Coordinator can always import. Observer can import only as a governance
    recovery action and must provide a non-empty reason.
    """
    project_id = ctx.get_project_id()
    md_path = ctx.body.get("md_path", ctx.body.get("graph_source", ""))
    reason = str(ctx.body.get("reason", "")).strip()
    if not md_path:
        from .errors import ValidationError
        raise ValidationError("md_path is required")
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        role = session.get("role", "")
        if role not in ("coordinator", "observer"):
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(role, "import-graph",
                                        {"detail": "Only coordinator or observer can import graphs"})
        if role == "observer" and not reason:
            from .errors import ValidationError
            raise ValidationError("reason is required for observer import-graph")
    result = project_service.import_graph(project_id, md_path)
    with DBContext(project_id) as conn:
        audit_service.record(
            conn, project_id,
            "observer_graph_import" if role == "observer" else "graph_import",
            actor=session.get("principal_id", ""),
            role=role,
            reason=reason,
            graph_source=md_path,
            graph_nodes=result.get("node_count", 0),
            node_states_initialized=result.get("node_states_initialized", 0),
        )
    return result


@route("POST", "/api/wf/{project_id}/observer-sync-node-state")
def handle_observer_sync_node_state(ctx: RequestContext):
    """Rebuild runtime node_state rows from the persisted graph definition.

    This is a governance recovery path only. It does not mark nodes as verified.
    """
    project_id = ctx.get_project_id()
    reason = str(ctx.body.get("reason", "")).strip()
    if not reason:
        from .errors import ValidationError
        raise ValidationError("reason is required")

    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        role = session.get("role", "")
        if role not in ("coordinator", "observer"):
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(role, "observer-sync-node-state",
                                        {"detail": "Only coordinator or observer can sync node_state"})

    result = project_service.sync_node_state_from_graph(project_id)
    with DBContext(project_id) as conn:
        audit_service.record(
            conn, project_id,
            "observer_node_state_sync" if role == "observer" else "node_state_sync",
            actor=session.get("principal_id", ""),
            role=role,
            reason=reason,
            graph_nodes=result.get("graph_nodes", 0),
            node_states_initialized=result.get("node_states_initialized", 0),
            node_state_total=result.get("node_state_total", 0),
            repair_mode=result.get("repair_mode", ""),
        )
    return result


@route("POST", "/api/wf/{project_id}/reconcile")
def handle_reconcile(ctx: RequestContext):
    """Unified reconcile: scan/diff/merge/sync/verify with two-phase commit.

    Body: {workspace_path, scan_depth?, dry_run?, auto_fix_stale?, require_high_confidence_only?,
           max_auto_fix_count?, mark_orphans_waived?, update_version?, operator_id?}
    """
    from .reconcile import reconcile_project, MergeOptions

    project_id = ctx.get_project_id()
    body = ctx.body

    workspace_path = body.get("workspace_path", "")
    if not workspace_path:
        from .errors import ValidationError
        raise ValidationError("workspace_path is required")

    merge_options = MergeOptions(
        auto_fix_stale=body.get("auto_fix_stale", True),
        require_high_confidence_only=body.get("require_high_confidence_only", True),
        mark_orphans_waived=body.get("mark_orphans_waived", False),
        max_auto_fix_count=body.get("max_auto_fix_count", 50),
        dry_run=body.get("dry_run", False),
    )

    result = reconcile_project(
        project_id=project_id,
        workspace_path=workspace_path,
        scan_depth=body.get("scan_depth", 3),
        merge_options=merge_options,
        update_version=body.get("update_version", False),
        dry_run=body.get("dry_run", False),
        operator_id=body.get("operator_id", "observer"),
    )
    return result


@route("POST", "/api/wf/{project_id}/reconcile-v2")
def handle_reconcile_v2(ctx: RequestContext):
    """Reconcile V2: creates a reconcile task and returns task_id + status_url (R9).

    Body: {metadata?, _meta_circular?, scenario?, reason?, observer_acknowledged_by?}

    Returns: {task_id, status_url, status}
    """
    from . import task_registry

    project_id = ctx.get_project_id()
    body = ctx.body

    metadata = body.get("metadata") or {}
    if isinstance(metadata, str):
        import json as _json
        try:
            metadata = _json.loads(metadata)
        except Exception:
            metadata = {}

    # Forward meta-circular fields
    for key in ("_meta_circular", "scenario", "reason", "observer_acknowledged_by"):
        if key in body and key not in metadata:
            metadata[key] = body[key]

    # Forward scope fields if present
    scope_data = body.get("scope")
    if scope_data and isinstance(scope_data, dict):
        metadata["scope"] = scope_data

    # Forward legacy fields for compat
    for key in ("workspace_path", "dry_run", "phases", "auto_fix_threshold",
                "scan_depth", "since"):
        if key in body:
            metadata[key] = body[key]

    prompt = body.get("prompt", "Reconcile project graph and node state")

    with DBContext(project_id) as conn:
        result = task_registry.create_task(
            conn,
            project_id,
            prompt=prompt,
            task_type="reconcile",
            metadata=metadata,
            created_by=body.get("operator_id", "reconcile-v2-api"),
        )
        conn.commit()

    task_id = result["task_id"]
    return {
        "task_id": task_id,
        "status_url": f"/api/task/{project_id}/{task_id}",
        "status": result.get("status", "queued"),
    }


# ---------------------------------------------------------------------------
# CR0b: Reconcile session HTTP API
# ---------------------------------------------------------------------------

def _session_to_dict(sess) -> dict:
    """Serialize a ReconcileSession dataclass to a JSON-safe dict."""
    if sess is None:
        return None
    return {
        "project_id": sess.project_id,
        "session_id": sess.session_id,
        "run_id": sess.run_id,
        "status": sess.status,
        "started_at": sess.started_at,
        "finalized_at": sess.finalized_at,
        "cluster_count_total": sess.cluster_count_total,
        "cluster_count_resolved": sess.cluster_count_resolved,
        "cluster_count_failed": sess.cluster_count_failed,
        "bypass_gates": list(sess.bypass_gates or []),
        "started_by": sess.started_by,
        "snapshot_path": sess.snapshot_path,
        "snapshot_head_sha": sess.snapshot_head_sha,
        "base_commit_sha": getattr(sess, "base_commit_sha", "") or "",
        "target_branch": getattr(sess, "target_branch", "") or "",
        "target_head_sha": getattr(sess, "target_head_sha", "") or "",
        "finalize_error": dict(getattr(sess, "finalize_error", {}) or {}),
    }


def _row_to_session_dict(row) -> dict:
    """Serialize a sqlite3.Row from reconcile_sessions to a JSON-safe dict."""
    if row is None:
        return None
    raw = row["bypass_gates_json"] if row["bypass_gates_json"] is not None else "[]"
    try:
        bypass = list(json.loads(raw) or [])
    except Exception:
        bypass = []
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    finalize_error = {}
    if "finalize_error_json" in keys:
        try:
            finalize_error = dict(json.loads(row["finalize_error_json"] or "{}") or {})
        except Exception:
            finalize_error = {}
    return {
        "project_id": row["project_id"],
        "session_id": row["session_id"],
        "run_id": row["run_id"],
        "status": row["status"],
        "started_at": row["started_at"],
        "finalized_at": row["finalized_at"],
        "cluster_count_total": int(row["cluster_count_total"] or 0),
        "cluster_count_resolved": int(row["cluster_count_resolved"] or 0),
        "cluster_count_failed": int(row["cluster_count_failed"] or 0),
        "bypass_gates": bypass,
        "started_by": row["started_by"],
        "snapshot_path": row["snapshot_path"],
        "snapshot_head_sha": row["snapshot_head_sha"],
        "base_commit_sha": row["base_commit_sha"] if "base_commit_sha" in keys else "",
        "target_branch": (
            row["target_branch"] if "target_branch" in keys and row["target_branch"]
            else reconcile_session.default_target_branch(row["project_id"], row["session_id"])
        ),
        "target_head_sha": (
            row["target_head_sha"] if "target_head_sha" in keys and row["target_head_sha"]
            else (row["base_commit_sha"] if "base_commit_sha" in keys else "")
        ),
        "finalize_error": finalize_error,
    }


@route("POST", "/api/reconcile/{project_id}/sessions/start")
def handle_reconcile_session_start(ctx: RequestContext):
    """Start a new reconcile session. 409 if one already exists."""
    project_id = ctx.get_project_id()
    body = ctx.body or {}
    bypass_gates = body.get("bypass_gates") or []
    started_by = body.get("started_by") or ""
    run_id = body.get("run_id")
    full_rebase = bool(body.get("full_rebase", False))
    dropped = body.get("dropped_cluster_fingerprints")
    base_commit_sha = body.get("base_commit_sha") or body.get("base_commit")
    target_branch = body.get("target_branch")
    try:
        with DBContext(project_id) as conn:
            from .db import _resolve_project_dir

            project_dir = _resolve_project_dir(project_id)
            # Pre-check: active session already exists?
            existing = reconcile_session.get_active_session(conn, project_id)
            if existing is not None:
                return 409, {
                    "error": "reconcile_session_active_exists",
                    "session_id": existing.session_id,
                    "status": existing.status,
                }
            sess = reconcile_session.start_session(
                conn, project_id,
                run_id=run_id,
                started_by=started_by or None,
                bypass_gates=list(bypass_gates),
                full_rebase=full_rebase,
                dropped_cluster_fingerprints=dropped,
                base_commit_sha=base_commit_sha,
                target_branch=target_branch,
                governance_dir=project_dir,
            )
    except reconcile_session.SessionAlreadyActiveError as exc:
        return 409, {
            "error": "reconcile_session_active_exists",
            "message": str(exc),
        }
    except ValueError as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}
    return 201, {"session": _session_to_dict(sess)}


@route("GET", "/api/reconcile/{project_id}/sessions/active")
def handle_reconcile_session_active(ctx: RequestContext):
    """Return the active/finalizing session for a project, else null."""
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        sess = reconcile_session.get_active_session(conn, project_id)
    return {"session": _session_to_dict(sess)}


@route("GET", "/api/reconcile/{project_id}/sessions/history")
def handle_reconcile_session_history(ctx: RequestContext):
    """Return all sessions for a project ordered by started_at DESC."""
    project_id = ctx.get_project_id()
    try:
        limit = int(ctx.query.get("limit", "50"))
    except (TypeError, ValueError):
        limit = 50
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM reconcile_sessions WHERE project_id = ? "
            "ORDER BY started_at DESC LIMIT ?",
            (project_id, max(1, limit)),
        ).fetchall()
    sessions = [_row_to_session_dict(r) for r in rows]
    return {"sessions": sessions, "count": len(sessions)}


@route("GET", "/api/reconcile/{project_id}/sessions/{session_id}")
def handle_reconcile_session_get(ctx: RequestContext):
    """Return a single session by id, else 404."""
    project_id = ctx.get_project_id()
    session_id = ctx.path_params.get("session_id", "")
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM reconcile_sessions WHERE project_id=? AND session_id=?",
            (project_id, session_id),
        ).fetchone()
    if row is None:
        return 404, {"error": "session_not_found", "session_id": session_id}
    return {"session": _row_to_session_dict(row)}


@route("POST", "/api/reconcile/{project_id}/sessions/{session_id}/doc-index")
def handle_reconcile_session_doc_index(ctx: RequestContext):
    """Generate final reconcile doc/test/source coverage report for signoff."""
    project_id = ctx.get_project_id()
    session_id = ctx.path_params.get("session_id", "")
    body = ctx.body or {}
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM reconcile_sessions WHERE project_id=? AND session_id=?",
            (project_id, session_id),
        ).fetchone()
        if row is None:
            return 404, {"error": "session_not_found", "session_id": session_id}
        try:
            from .db import _resolve_project_dir

            project_dir = _resolve_project_dir(project_id)
            candidate = Path(body.get("candidate_graph_path") or project_dir / "graph.rebase.candidate.json")
            overlay = Path(body.get("overlay_path") or project_dir / "graph.rebase.overlay.json")
            report = reconcile_session.generate_final_doc_index_report(
                conn,
                project_id,
                session_id,
                governance_dir=project_dir,
                candidate_graph_path=candidate,
                overlay_path=overlay,
                output_dir=project_dir,
            )
            conn.commit()
        except ValueError as exc:
            return 400, {"error": "doc_index_failed", "message": str(exc)}
    return {"result": report}


@route("POST", "/api/reconcile/{project_id}/sessions/{session_id}/finalize")
def handle_reconcile_session_finalize(ctx: RequestContext):
    """Transition session to finalizing then finalize it. Idempotent on already-finalized sessions."""
    project_id = ctx.get_project_id()
    session_id = ctx.path_params.get("session_id", "")
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM reconcile_sessions WHERE project_id=? AND session_id=?",
            (project_id, session_id),
        ).fetchone()
        if row is None:
            return 404, {"error": "session_not_found", "session_id": session_id}
        current_status = row["status"]
        if current_status == "finalized":
            # Idempotent: already finalized
            row2 = conn.execute(
                "SELECT * FROM reconcile_sessions WHERE project_id=? AND session_id=?",
                (project_id, session_id),
            ).fetchone()
            return {
                "result": {
                    "project_id": project_id,
                    "session_id": session_id,
                    "status": "finalized",
                    "finalized_at": row2["finalized_at"] if row2 else None,
                },
                "idempotent": True,
            }
        if current_status == "rolled_back":
            return 409, {
                "error": "session_terminal",
                "status": current_status,
                "message": "session is rolled_back; cannot finalize",
            }
        body = ctx.body or {}
        try:
            from .db import _resolve_project_dir

            project_dir = _resolve_project_dir(project_id)
            candidate_graph_path = project_dir / "graph.rebase.candidate.json"
            result = reconcile_session.finalize_session(
                conn,
                project_id,
                session_id,
                governance_dir=project_dir,
                graph_path=project_dir / "graph.json",
                workspace_dir=Path(__file__).resolve().parents[2],
                candidate_graph_path=(
                    candidate_graph_path if candidate_graph_path.exists() else None
                ),
                full_rebase=bool(body.get("full_rebase", False)),
            )
        except reconcile_session.SessionClusterGateError as exc:
            return 409, {
                "error": "reconcile_clusters_incomplete",
                "message": str(exc),
                "summary": exc.summary,
            }
        except ValueError as exc:
            return 400, {"error": "invalid_state", "message": str(exc)}
    return {
        "result": {
            "project_id": result.project_id,
            "session_id": result.session_id,
            "status": result.status,
            "finalized_at": result.finalized_at,
            "overlay_archived_to": result.overlay_archived_to,
            "graph_path": result.graph_path,
            "graph_backup_path": result.graph_backup_path,
            "materialized_node_count": result.materialized_node_count,
            "materialization_counts": result.materialization_counts,
        },
        "idempotent": False,
    }


@route("POST", "/api/reconcile/{project_id}/sessions/{session_id}/rollback")
def handle_reconcile_session_rollback(ctx: RequestContext):
    """Roll back an active or finalizing session. Writes audit event."""
    project_id = ctx.get_project_id()
    session_id = ctx.path_params.get("session_id", "")
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status FROM reconcile_sessions WHERE project_id=? AND session_id=?",
            (project_id, session_id),
        ).fetchone()
        if row is None:
            return 404, {"error": "session_not_found", "session_id": session_id}
        try:
            body = ctx.body or {}
            from .db import _resolve_project_dir

            project_dir = _resolve_project_dir(project_id)
            result = reconcile_session.rollback_session(
                conn,
                project_id,
                session_id,
                governance_dir=project_dir,
                restore_graph_snapshot=bool(body.get("restore_graph_snapshot", False)),
            )
        except ValueError as exc:
            return 409, {"error": "invalid_state", "message": str(exc)}
        # Audit the rollback event
        try:
            audit_service.record(
                conn, project_id,
                event="reconcile_session.rolled_back",
                actor=(ctx.body or {}).get("actor", "anonymous"),
                ok=True, node_ids=None, request_id=ctx.request_id,
                session_id=session_id,
            )
        except Exception:
            log.debug("rollback audit failed (non-critical)", exc_info=True)
    return {
        "result": {
            "project_id": result.project_id,
            "session_id": result.session_id,
            "status": result.status,
            "rolled_back_at": result.rolled_back_at,
            "snapshot_path": result.snapshot_path,
        },
    }


# ---------------------------------------------------------------------------
# Reconcile Batch Memory HTTP API
# ---------------------------------------------------------------------------

@route("POST", "/api/reconcile/{project_id}/batch-memory")
def handle_reconcile_batch_memory_create(ctx: RequestContext):
    """Create or fetch durable batch memory for PM semantic merge context."""
    from . import reconcile_batch_memory as bm

    project_id = ctx.get_project_id()
    body = ctx.body or {}
    with DBContext(project_id) as conn:
        batch = bm.create_or_get_batch(
            conn,
            project_id,
            session_id=str(body.get("session_id") or ""),
            batch_id=body.get("batch_id"),
            created_by=str(body.get("created_by") or body.get("actor") or ""),
            initial_memory=body.get("initial_memory") if isinstance(body.get("initial_memory"), dict) else None,
        )
    return 201, {"ok": True, "batch": batch}


@route("GET", "/api/reconcile/{project_id}/batch-memory/{batch_id}")
def handle_reconcile_batch_memory_get(ctx: RequestContext):
    """Return one reconcile batch memory document."""
    from . import reconcile_batch_memory as bm

    project_id = ctx.get_project_id()
    batch_id = ctx.path_params.get("batch_id", "")
    with DBContext(project_id) as conn:
        batch = bm.get_batch(conn, project_id, batch_id)
    if not batch:
        return 404, {"error": "batch_memory_not_found", "batch_id": batch_id}
    return {"ok": True, "batch": batch}


@route("POST", "/api/reconcile/{project_id}/batch-memory/{batch_id}/pm-decision")
def handle_reconcile_batch_memory_pm_decision(ctx: RequestContext):
    """Record one PM semantic decision into batch memory."""
    from . import reconcile_batch_memory as bm

    project_id = ctx.get_project_id()
    batch_id = ctx.path_params.get("batch_id", "")
    body = ctx.body or {}
    cluster_fp = str(
        body.get("cluster_fingerprint")
        or body.get("cluster_id")
        or ctx.path_params.get("cluster_fingerprint", "")
    )
    try:
        with DBContext(project_id) as conn:
            batch = bm.record_pm_decision(
                conn,
                project_id,
                batch_id,
                cluster_fp,
                body,
            )
    except KeyError:
        return 404, {"error": "batch_memory_not_found", "batch_id": batch_id}
    except ValueError as exc:
        return 400, {"error": "invalid_pm_decision", "message": str(exc)}
    return {"ok": True, "batch": batch}


# ---------------------------------------------------------------------------
# CR3 — Reconcile Deferred-Cluster Queue HTTP API (R7)
# ---------------------------------------------------------------------------


def _deferred_cluster_row_to_dict(row) -> dict:
    if row is None:
        return {}
    out = {}
    for key in row.keys():
        out[key] = row[key]
    if isinstance(out.get("payload_json"), str):
        try:
            out["payload"] = json.loads(out["payload_json"]) if out["payload_json"] else {}
        except Exception:
            out["payload"] = {}
    return out


@route("GET", "/api/reconcile/{project_id}/deferred-clusters")
def handle_reconcile_deferred_clusters_list(ctx: RequestContext):
    """List queue rows; supports ?status=&priority=&run_id= filters."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    status_filter = ctx.query.get("status")
    priority_filter = ctx.query.get("priority")
    run_id_filter = ctx.query.get("run_id")
    sql = (
        "SELECT * FROM reconcile_deferred_clusters WHERE project_id = ?"
    )
    args: list = [project_id]
    if run_id_filter:
        sql += " AND run_id = ?"
        args.append(run_id_filter)
    if status_filter:
        sql += " AND status = ?"
        args.append(status_filter)
    if priority_filter is not None and priority_filter != "":
        try:
            sql += " AND priority = ?"
            args.append(int(priority_filter))
        except (TypeError, ValueError):
            pass
    sql += " ORDER BY priority ASC, first_seen_at ASC"

    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        q.ensure_schema(conn)
        rows = conn.execute(sql, tuple(args)).fetchall()
    items = [_deferred_cluster_row_to_dict(r) for r in rows]
    return {"clusters": items, "count": len(items)}


@route("GET", "/api/reconcile/{project_id}/deferred-clusters/summary")
def handle_reconcile_deferred_clusters_summary(ctx: RequestContext):
    """Return completion gate state for a project/run."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    run_id = ctx.query.get("run_id") or ""
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        summary = q.completion_summary(
            project_id,
            run_id=run_id or None,
            conn=conn,
        )
    return {"summary": summary}


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/register-run")
def handle_reconcile_deferred_clusters_register_run(ctx: RequestContext):
    """Register all FeatureClusters from a Phase Z run into the durable queue."""
    from . import reconcile_deferred_queue as q
    from . import auto_backlog_bridge

    project_id = ctx.get_project_id()
    body = ctx.body or {}
    run_id = str(body.get("run_id") or "").strip()
    clusters = body.get("feature_clusters") or body.get("clusters") or []
    if not run_id:
        return 400, {"error": "missing_run_id"}
    if not isinstance(clusters, list):
        return 400, {"error": "invalid_feature_clusters"}
    try:
        priority = int(body.get("priority", 100))
    except (TypeError, ValueError):
        priority = 100
    try:
        from .db import _resolve_project_dir

        project_dir = _resolve_project_dir(project_id)
        candidate_path = project_dir / "graph.rebase.candidate.json"
        overlay_path = project_dir / "graph.rebase.overlay.json"
        candidate_graph = {}
        if candidate_path.exists():
            candidate_graph = json.loads(candidate_path.read_text(encoding="utf-8"))
        clusters = [
            auto_backlog_bridge.enrich_feature_cluster_payload(
                cluster,
                candidate_graph=candidate_graph,
                candidate_graph_path=str(candidate_path),
                overlay_path=str(overlay_path),
                run_id=run_id,
            )
            for cluster in clusters
            if isinstance(cluster, dict)
        ]
    except Exception:
        log.warning(
            "reconcile_deferred_clusters.register-run: payload enrichment skipped",
            exc_info=True,
        )
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        result = q.register_feature_clusters(
            project_id,
            run_id,
            clusters,
            conn=conn,
            priority=priority,
        )
    return {"result": result}


@route("GET", "/api/reconcile/{project_id}/file-inventory")
def handle_reconcile_file_inventory_list(ctx: RequestContext):
    """List file inventory rows; supports ?run_id=&scan_status=&file_kind=&limit=."""
    from .reconcile_file_inventory import query_file_inventory

    project_id = ctx.get_project_id()
    try:
        limit = int(ctx.query.get("limit", "200"))
    except (TypeError, ValueError):
        limit = 200

    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        result = query_file_inventory(
            conn,
            project_id,
            run_id=ctx.query.get("run_id", ""),
            scan_status=ctx.query.get("scan_status", ""),
            file_kind=ctx.query.get("file_kind", ""),
            limit=limit,
        )
    result["project_id"] = project_id
    return result


# ---------------------------------------------------------------------------
# Graph Governance State API (proposal-graph-governance-unified-v3)
# ---------------------------------------------------------------------------

def _graph_governance_project_root(project_id: str, body: dict) -> Path:
    raw = (
        body.get("project_root")
        or body.get("worktree_path")
        or body.get("workspace_path")
        or body.get("repo_root")
        or ""
    )
    if raw:
        return Path(str(raw)).resolve()
    for project in project_service.list_projects():
        if project.get("project_id") == project_id and project.get("workspace_path"):
            return Path(project["workspace_path"]).resolve()
    if project_id == "aming-claw":
        return Path(__file__).resolve().parents[2]
    from .errors import ValidationError
    raise ValidationError("project_root or workspace_path is required")


def _pending_scope_identity_from_body(body: dict) -> dict[str, str]:
    from . import graph_snapshot_store as store

    return store.normalize_pending_scope_identity(
        ref_name=str(body.get("ref_name") or ""),
        branch_ref=str(body.get("branch_ref") or body.get("worktree_branch") or ""),
        worktree_id=str(body.get("worktree_id") or ""),
        worktree_path=str(body.get("worktree_path") or ""),
    )


def _semantic_use_ai_from_body(body: dict) -> bool | None:
    if body.get("semantic_use_ai") is not None:
        return bool(body["semantic_use_ai"])
    if body.get("use_ai") is not None:
        return bool(body["use_ai"])
    if body.get("reviewer_use_ai") is not None:
        return bool(body["reviewer_use_ai"])
    if body.get("use_reviewer_ai") is not None:
        return bool(body["use_reviewer_ai"])
    return None


def _automation_mode_from_body(body: dict, *keys: str, default: str = "manual") -> str:
    for key in keys:
        if body.get(key) is None:
            continue
        mode = str(body.get(key) or "").strip().lower().replace("-", "_")
        break
    else:
        mode = default
    if mode in {"off", "disabled", "false"}:
        mode = "manual"
    if mode not in {"manual", "enqueue_only", "auto"}:
        from .errors import ValidationError
        raise ValidationError(
            f"automation mode must be one of manual, enqueue_only, auto; got {mode}"
        )
    return mode


def _semantic_ai_call_from_body(project_id: str, root: Path, body: dict):
    use_ai = _semantic_use_ai_from_body(body)
    if use_ai is False:
        return None
    try:
        from .reconcile_semantic_ai import build_semantic_ai_call
        from .reconcile_semantic_config import (
            apply_project_ai_routing,
            load_semantic_enrichment_config,
        )
        semantic_config = load_semantic_enrichment_config(
            project_root=root,
            config_path=body.get("semantic_config_path"),
        )
        semantic_config = apply_project_ai_routing(
            semantic_config,
            project_id=project_id,
        )
        if body.get("semantic_ai_provider") is not None:
            semantic_config.provider = str(body.get("semantic_ai_provider") or "")
        if body.get("semantic_ai_model") is not None:
            semantic_config.model = str(body.get("semantic_ai_model") or "")
        if body.get("semantic_analyzer_role") is not None:
            semantic_config.analyzer_role = str(body.get("semantic_analyzer_role") or "")
        elif body.get("reconcile_analyzer_role") is not None:
            semantic_config.analyzer_role = str(body.get("reconcile_analyzer_role") or "")
        if body.get("semantic_ai_chain_role") is not None:
            semantic_config.chain_role = str(body.get("semantic_ai_chain_role") or "")
            semantic_config.role = semantic_config.chain_role
        elif body.get("semantic_ai_pipeline_role") is not None:
            semantic_config.chain_role = str(body.get("semantic_ai_pipeline_role") or "")
            semantic_config.role = semantic_config.chain_role
        if body.get("semantic_ai_role") is not None:
            # Backward compatibility: semantic_ai_role historically selected the
            # chain/pipeline model-routing role, not the reconcile analyzer identity.
            semantic_config.chain_role = str(body.get("semantic_ai_role") or "")
            semantic_config.role = semantic_config.chain_role
        effective_use_ai = semantic_config.use_ai_default if use_ai is None else use_ai
        if not effective_use_ai:
            return None
        return build_semantic_ai_call(
            semantic_config=semantic_config,
            project_id=project_id,
            snapshot_id=str(body.get("snapshot_id") or body.get("run_id") or "candidate"),
            project_root=root,
        )
    except Exception:
        return None


def _semantic_ai_feature_limit_from_body(body: dict) -> int | None:
    value = body.get("semantic_ai_feature_limit")
    if value is None:
        value = body.get("ai_feature_limit")
    if value is None:
        return None
    return int(value)


def _semantic_ai_batch_kwargs_from_body(body: dict) -> dict:
    size = body.get("semantic_ai_batch_size")
    if size is None:
        size = body.get("ai_batch_size")
    return {
        "semantic_ai_batch_size": int(size) if size is not None else None,
        "semantic_ai_batch_by": str(
            body.get("semantic_ai_batch_by")
            or body.get("ai_batch_by")
            or "subsystem"
        ),
        "semantic_ai_input_mode": (
            body.get("semantic_ai_input_mode")
            or body.get("semantic_input_mode")
            or body.get("ai_input_mode")
        ),
        "semantic_dynamic_graph_state": _semantic_bool_from_body(
            body,
            "semantic_dynamic_graph_state",
            "dynamic_semantic_graph_state",
            default=None,
        ),
    }


def _semantic_bool_from_body(body: dict, *keys: str, default: bool | None = None) -> bool | None:
    for key in keys:
        if body.get(key) is None:
            continue
        value = body.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off", ""}
        return bool(value)
    return default


def _semantic_state_kwargs_from_body(body: dict) -> dict:
    return {
        "semantic_graph_state": bool(
            _semantic_bool_from_body(body, "semantic_graph_state", "graph_state", default=True)
        ),
        "semantic_skip_completed": bool(
            _semantic_bool_from_body(body, "semantic_skip_completed", "skip_completed", default=True)
        ),
        "semantic_batch_memory": _semantic_bool_from_body(
            body,
            "semantic_batch_memory",
            "batch_memory",
            default=False,
        ),
        "semantic_batch_memory_id": body.get("semantic_batch_memory_id") or body.get("batch_memory_id"),
        "semantic_base_snapshot_id": body.get("semantic_base_snapshot_id") or body.get("base_snapshot_id"),
    }


def _semantic_selector_kwargs_from_body(body: dict) -> dict:
    return {
        "semantic_ai_scope": body.get("semantic_ai_scope") or body.get("ai_scope"),
        "semantic_node_ids": body.get("semantic_node_ids") or body.get("node_ids"),
        "semantic_layers": body.get("semantic_layers") or body.get("layers"),
        "semantic_quality_flags": body.get("semantic_quality_flags") or body.get("quality_flags"),
        "semantic_missing": body.get("semantic_missing") or body.get("missing"),
        "semantic_changed_paths": body.get("semantic_changed_paths") or body.get("changed_paths"),
        "semantic_path_prefixes": body.get("semantic_path_prefixes") or body.get("path_prefixes"),
        "semantic_selector_match": body.get("semantic_selector_match") or body.get("selector_match"),
        "semantic_include_structural": bool(
            body.get("semantic_include_structural")
            or body.get("include_structural")
        ),
    }


def _semantic_ai_config_kwargs_from_body(body: dict) -> dict:
    return {
        "semantic_ai_provider": (
            str(body.get("semantic_ai_provider"))
            if body.get("semantic_ai_provider") is not None
            else None
        ),
        "semantic_ai_model": (
            str(body.get("semantic_ai_model"))
            if body.get("semantic_ai_model") is not None
            else None
        ),
        "semantic_ai_role": (
            str(body.get("semantic_ai_role"))
            if body.get("semantic_ai_role") is not None
            else None
        ),
        "semantic_ai_chain_role": (
            str(body.get("semantic_ai_chain_role") or body.get("semantic_ai_pipeline_role"))
            if body.get("semantic_ai_chain_role") is not None or body.get("semantic_ai_pipeline_role") is not None
            else None
        ),
        "semantic_analyzer_role": (
            str(body.get("semantic_analyzer_role") or body.get("reconcile_analyzer_role"))
            if body.get("semantic_analyzer_role") is not None or body.get("reconcile_analyzer_role") is not None
            else None
        ),
    }


def _query_int(query: dict, key: str, default: int) -> int:
    try:
        return int(query.get(key, default))
    except (TypeError, ValueError):
        return default


def _query_bool(query: dict, key: str, default: bool = False) -> bool:
    value = query.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def _query_statuses(query: dict, key: str = "status") -> list[str]:
    raw = query.get(key, "")
    values = raw if isinstance(raw, list) else str(raw or "").split(",")
    return [str(value).strip() for value in values if str(value).strip()]


def _body_bool(body: dict, key: str, default: bool = False) -> bool:
    value = body.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def _body_string_list(body: dict, key: str) -> list[str] | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    raise ValidationError(f"{key} must be a list or comma-separated string")


def _managed_ref_bootstrap_refs_from_body(project_id: str, body: dict) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    refs = body.get("refs")
    target_ref = str(body.get("target_ref") or "refs/heads/main")
    if refs is not None:
        if not isinstance(refs, list):
            raise ValidationError("refs must be a list when provided")
        rows = [row if isinstance(row, dict) else {"ref_name": str(row)} for row in refs]
        return rows, {
            "source": "request_body",
            "ref_count": len(rows),
            "target_ref": target_ref,
            "target_head_commit": str(body.get("target_head_commit") or ""),
        }

    root = _graph_governance_project_root(project_id, body)
    include_remotes = _body_bool(body, "include_remotes", False)
    target_head_commit = str(body.get("target_head_commit") or "").strip()
    rows, discovery = _git_managed_ref_bootstrap_rows(
        root,
        target_ref=target_ref,
        target_head_commit=target_head_commit,
        include_remotes=include_remotes,
    )
    return rows, discovery


def _require_graph_governance_operator(ctx: RequestContext, conn, action: str) -> dict:
    session = ctx.require_auth(conn)
    from .permissions import require_operator_capability
    require_operator_capability(session, action)
    return session


def _require_graph_governance_mf_subagent(ctx: RequestContext, conn, action: str) -> dict:
    session = ctx.require_auth(conn)
    from .permissions import require_mf_subagent_capability
    require_mf_subagent_capability(session, action)
    return session


def _require_graph_query_capability(ctx: RequestContext, conn, body: dict, action: str) -> dict:
    query_source = str(body.get("query_source") or "api_debug").strip().lower().replace("-", "_")
    if query_source != "mf_subagent":
        return _require_graph_governance_operator(ctx, conn, action)

    session = _require_graph_governance_mf_subagent(ctx, conn, action)
    task_id = str(body.get("task_id") or "").strip()
    parent_task_id = str(body.get("parent_task_id") or "").strip()
    validation_task_id = task_id or parent_task_id
    fence_token = str(body.get("fence_token") or "").strip()
    if not validation_task_id:
        raise ValidationError("parent_task_id or task_id is required for mf_subagent graph query")
    if not fence_token:
        raise ValidationError("fence_token is required for mf_subagent graph query")
    from .parallel_branch_runtime import (
        BranchRuntimeFenceError,
        validate_mf_subagent_graph_query_identity,
    )
    try:
        context = validate_mf_subagent_graph_query_identity(
            conn,
            project_id=ctx.get_project_id(),
            task_id=validation_task_id,
            parent_task_id=parent_task_id,
            worker_role=str(body.get("worker_role") or ""),
            fence_token=fence_token,
        )
    except BranchRuntimeFenceError as exc:
        raise GovernanceError(
            "fence_invalidated_or_unknown",
            "mf_subagent graph query fence is invalidated or unknown",
            403,
            {
                "task_id": validation_task_id,
                "parent_task_id": parent_task_id,
                "reason": "fence_invalidated_or_unknown",
            },
        ) from exc
    fence_hash = hashlib.sha256(fence_token.encode("utf-8")).hexdigest()[:16]
    body["task_id"] = context.task_id
    body["parent_task_id"] = parent_task_id or context.root_task_id or context.task_id
    body["query_source"] = "mf_subagent"
    body["run_id"] = str(body.get("run_id") or "") or f"mf_subagent:{context.task_id}:fence:{fence_hash}"
    return session


def _require_graph_query_trace_capability(ctx: RequestContext, conn, trace: dict, action: str) -> dict:
    session = ctx.require_auth(conn)
    from .permissions import require_mf_subagent_capability, require_operator_capability, session_role
    role = session_role(session)
    if str(trace.get("query_source") or "") == "mf_subagent":
        require_mf_subagent_capability(session, action)
    else:
        require_operator_capability(session, action)
    if role == "mf_sub":
        parent_task_id = str(trace.get("parent_task_id") or "").strip()
        if not parent_task_id:
            raise ValidationError("mf_subagent graph query trace must be task-scoped")
    return session


def _raise_graph_api_validation(exc: Exception):
    from .errors import ValidationError
    raise ValidationError(str(exc)) from exc


def _raise_graph_api_conflict(exc: Exception):
    raise GovernanceError("graph_snapshot_conflict", str(exc), 409) from exc


def _parallel_branch_current_target_head(
    project_id: str,
    source: dict,
    *,
    target_ref: str = "",
) -> str:
    provided = str(
        source.get("current_target_head")
        or source.get("latest_target_head")
        or ""
    ).strip()
    if provided:
        return provided
    if not _query_bool(source, "resolve_current_target_head", False):
        return ""
    ref = str(target_ref or source.get("target_ref") or "refs/heads/main").strip()
    if not ref:
        return ""
    root = _graph_governance_project_root(project_id, source)
    return _git_output(root, ["rev-parse", "--verify", ref])


def _resolve_graph_snapshot_id(conn, project_id: str, snapshot_id: str) -> str:
    if snapshot_id and snapshot_id != "active":
        return snapshot_id
    from . import graph_snapshot_store as store

    active = store.get_active_graph_snapshot(conn, project_id)
    if not active:
        from .errors import ValidationError
        raise ValidationError("no active graph snapshot for project")
    return active["snapshot_id"]


def _graph_drift_backlog_id(snapshot_id: str, path: str, drift_type: str, target_symbol: str) -> str:
    seed = f"{snapshot_id}|{path}|{drift_type}|{target_symbol}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    safe_type = re.sub(r"[^A-Za-z0-9]+", "-", drift_type or "drift").strip("-").upper()
    return f"GRAPH-DRIFT-{safe_type}-{digest}"


def _summarize_graph_drift_rows(rows: list[dict]) -> dict:
    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    open_sample: list[dict] = []
    for row in rows:
        status = str(row.get("status") or "")
        drift_type = str(row.get("drift_type") or "")
        if status:
            by_status[status] = by_status.get(status, 0) + 1
        if drift_type:
            by_type[drift_type] = by_type.get(drift_type, 0) + 1
        if status == "open" and len(open_sample) < 20:
            open_sample.append({
                "path": row.get("path", ""),
                "drift_type": drift_type,
                "node_id": row.get("node_id", ""),
                "target_symbol": row.get("target_symbol", ""),
            })
    return {
        "total": len(rows),
        "by_status": dict(sorted(by_status.items())),
        "by_type": dict(sorted(by_type.items())),
        "open_sample": open_sample,
    }


@route("GET", "/api/graph-governance/{project_id}/events/stream")
def handle_graph_governance_events_stream(ctx: RequestContext):
    """Server-Sent Events stream of governance events for the dashboard.

    Emits:
      - `ready` once on connect (project_id + server_version) so the client
        can flip into "live" state.
      - One event per EventBus publish whose payload's project_id matches
        (or whose payload has no project_id — those are treated as global).
      - `: ping\\n\\n` heartbeat comment every 15s so reverse proxies and
        EventSource don't kill the idle connection.

    The handler subscribes a queue-pushing callback via subscribe_all and
    unsubscribes in the finally block when the client disconnects or the
    server shuts down. Each connection runs on its own thread courtesy of
    ThreadingHTTPServer, so multiple dashboards can stream concurrently.
    """
    import queue
    from . import event_bus

    handler = ctx.handler
    project_id = ctx.get_project_id()

    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache, no-transform")
        handler.send_header("Connection", "keep-alive")
        handler.send_header("X-Accel-Buffering", "no")  # disable nginx buffering
        for k, v in handler.CORS_HEADERS.items():
            handler.send_header(k, v)
        handler.end_headers()
    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
        return STREAMED_RESPONSE

    q: "queue.Queue[tuple[str, dict]]" = queue.Queue(maxsize=500)

    def on_event(event_name: str, payload: dict) -> None:
        # Bound the queue: drop on overflow rather than block the publisher.
        # Filter by project_id when payload carries one; global events
        # (no project_id) are forwarded to every connection.
        pid = payload.get("project_id") if isinstance(payload, dict) else None
        if pid and project_id and pid != project_id:
            return
        try:
            q.put_nowait((event_name, payload))
        except queue.Full:
            pass

    bus = event_bus.get_event_bus()
    bus.subscribe_all(on_event)

    def _write_sse(event_name: str, data: dict) -> None:
        try:
            line = (
                f"event: {event_name}\n"
                f"data: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
            )
            handler.wfile.write(line.encode("utf-8"))
            handler.wfile.flush()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            raise

    try:
        _write_sse("ready", {
            "ts": datetime.now(timezone.utc).isoformat(),
            "project_id": project_id,
            "server_version": get_server_version(),
        })

        while True:
            try:
                event_name, payload = q.get(timeout=15.0)
            except queue.Empty:
                try:
                    handler.wfile.write(b": ping\n\n")
                    handler.wfile.flush()
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
                    break
                continue

            try:
                _write_sse(event_name, {
                    "event": event_name,
                    "payload": payload,
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
                break
    finally:
        bus.unsubscribe_all(on_event)

    return STREAMED_RESPONSE


@route("GET", "/api/graph-governance/{project_id}/status")
def handle_graph_governance_status(ctx: RequestContext):
    """Return active graph snapshot, scan baseline, and pending scope status."""
    project_id = ctx.get_project_id()
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        status = store.graph_governance_status(conn, project_id)
        status["current_state"] = _dashboard_current_state(
            conn,
            project_id,
            status=status,
            snapshot_id=str(status.get("active_snapshot_id") or ""),
        )
        target_commit = str(ctx.query.get("target_commit") or "")
        if target_commit:
            status["strict_ready"] = store.strict_graph_ready(
                conn,
                project_id,
                target_commit=target_commit,
            )
        return {"ok": True, **status}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/asset-impact/reminders")
def handle_graph_governance_asset_impact_reminders(ctx: RequestContext):
    """Return pending asset impact reminders for Review Queue surfaces."""
    project_id = ctx.get_project_id()
    from . import asset_impact

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.asset-impact.reminders")
        try:
            result = asset_impact.build_asset_impact_reminder_projection(
                conn,
                project_id,
                asset_kind=str(ctx.query.get("asset_kind") or ""),
                node_id=str(ctx.query.get("node_id") or ""),
                status=str(ctx.query.get("status") or asset_impact.STATUS_PENDING),
                limit=_query_int(ctx.query, "limit", 500),
            )
        except ValueError as exc:
            _raise_graph_api_validation(exc)
        return {"ok": True, **result}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/asset-impact/trace")
def handle_graph_governance_asset_impact_trace(ctx: RequestContext):
    """Return a bidirectional asset/node trace across bindings, events, and drift state."""
    project_id = ctx.get_project_id()
    from . import asset_impact

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.asset-impact.trace")
        result = asset_impact.build_asset_impact_trace(
            conn,
            project_id,
            snapshot_id=str(ctx.query.get("snapshot_id") or ""),
            asset_kind=str(ctx.query.get("asset_kind") or ""),
            asset_path=str(ctx.query.get("asset_path") or ""),
            node_id=str(ctx.query.get("node_id") or ""),
            include_candidates=_query_bool(ctx.query, "include_candidates", True),
            limit=_query_int(ctx.query, "limit", 500),
        )
        return {"ok": True, **result}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/asset-impact/reminders/{reminder_id}/events")
def handle_graph_governance_asset_impact_reminder_events(ctx: RequestContext):
    """Return one asset impact reminder and its event history."""
    project_id = ctx.get_project_id()
    reminder_id = unquote(str(ctx.path_params.get("reminder_id") or ""))
    from . import asset_impact

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.asset-impact.reminder.events")
        result = asset_impact.get_asset_impact_reminder_events(
            conn,
            project_id,
            reminder_id,
            limit=_query_int(ctx.query, "limit", 500),
        )
        if not result.get("reminder"):
            raise GovernanceError("not_found", f"Asset impact reminder {reminder_id} not found", 404)
        return {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/asset-impact/reminders/{reminder_id}/resolve")
def handle_graph_governance_asset_impact_reminder_resolve(ctx: RequestContext):
    """Record an operator resolution for a pending asset impact reminder."""
    project_id = ctx.get_project_id()
    reminder_id = unquote(str(ctx.path_params.get("reminder_id") or ""))
    body = ctx.body
    from . import asset_impact

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.asset-impact.reminder.resolve")
        try:
            result = asset_impact.resolve_asset_impact_reminder(
                conn,
                project_id,
                reminder_id,
                resolution_kind=str(body.get("resolution_kind") or ""),
                note=str(body.get("note") or ""),
                actor=str(body.get("actor") or "observer"),
            )
        except KeyError as exc:
            raise GovernanceError("not_found", str(exc), 404) from exc
        except ValueError as exc:
            _raise_graph_api_validation(exc)
        audit_service.record(
            conn,
            project_id,
            "asset_impact_reminder_resolved",
            actor=str(body.get("actor") or "observer"),
            request_id=ctx.request_id,
            details=json.dumps({
                "reminder_id": reminder_id,
                "resolution_kind": result.get("resolution", {}).get("resolution_kind", ""),
                "covers_event_ids": result.get("resolution", {}).get("covers_event_ids", []),
            }, ensure_ascii=False, sort_keys=True),
        )
        conn.commit()
        return {"ok": True, **result}
    finally:
        conn.close()


def _asset_drift_ai_route_ready(project_id: str) -> tuple[bool, str]:
    try:
        project_config = project_service.get_project_config_metadata(project_id)
    except Exception as exc:
        return False, f"project AI config unavailable: {exc}"
    if not isinstance(project_config, dict) or not project_config:
        return False, "project AI config is missing"
    route = _semantic_route_from_project_config(project_config)
    provider = str(route.get("provider") or "")
    model = str(route.get("model") or "")
    if not provider or not model:
        return False, "semantic AI provider/model route is not configured"
    return True, f"semantic route configured: {provider}/{model}"


@route("POST", "/api/graph-governance/{project_id}/asset-drift/state")
def handle_graph_governance_asset_drift_state_record(ctx: RequestContext):
    """Record manual asset drift state with audit evidence."""
    project_id = ctx.get_project_id()
    body = ctx.body
    from . import asset_impact

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.asset-drift.state")
        try:
            result = asset_impact.record_asset_drift_state(
                conn,
                project_id=project_id,
                asset_kind=str(body.get("asset_kind") or ""),
                asset_path=str(body.get("asset_path") or body.get("path") or ""),
                drift_state=str(body.get("drift_state") or ""),
                snapshot_id=str(body.get("snapshot_id") or ""),
                commit_sha=str(body.get("commit_sha") or ""),
                actor=str(body.get("actor") or "observer"),
                evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else {},
            )
            conn.commit()
        except ValueError as exc:
            _raise_graph_api_validation(exc)
        audit_service.record(
            conn,
            project_id,
            "asset_drift_state_recorded",
            actor=str(body.get("actor") or "observer"),
            request_id=ctx.request_id,
            details=json.dumps(result.get("drift_state", {}), ensure_ascii=False, sort_keys=True),
        )
        conn.commit()
        return 201, {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/asset-drift/proposals")
def handle_graph_governance_asset_drift_proposal_queue(ctx: RequestContext):
    """Queue or record an AI-assisted asset drift proposal without materializing it."""
    project_id = ctx.get_project_id()
    body = ctx.body
    from . import asset_impact

    ai_available, ai_reason = _asset_drift_ai_route_ready(project_id)
    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.asset-drift.proposal")
        try:
            result = asset_impact.queue_asset_drift_proposal(
                conn,
                project_id=project_id,
                asset_kind=str(body.get("asset_kind") or ""),
                asset_path=str(body.get("asset_path") or body.get("path") or ""),
                snapshot_id=str(body.get("snapshot_id") or ""),
                commit_sha=str(body.get("commit_sha") or ""),
                node_id=str(body.get("node_id") or body.get("target_node_id") or ""),
                actor=str(body.get("actor") or "observer"),
                ai_available=ai_available,
                ai_reason=ai_reason,
                evidence={
                    "mode": str(body.get("mode") or "ai_assisted_proposal"),
                    "operator_note": str(body.get("note") or body.get("reason") or ""),
                },
            )
            conn.commit()
        except ValueError as exc:
            _raise_graph_api_validation(exc)
        audit_service.record(
            conn,
            project_id,
            "asset_drift_proposal_queued",
            actor=str(body.get("actor") or "observer"),
            request_id=ctx.request_id,
            details=json.dumps(result.get("proposal", {}), ensure_ascii=False, sort_keys=True),
        )
        conn.commit()
        return 201, {"ok": True, "ai_available": ai_available, "ai_reason": ai_reason, **result}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/parallel-branches")
def handle_graph_governance_parallel_branches(ctx: RequestContext):
    """Return compact parallel branch lanes, queue, and rollback state."""
    project_id = ctx.get_project_id()
    from .parallel_branch_runtime import build_parallel_branch_read_model_from_db

    target_ref = str(ctx.query.get("target_ref") or "")
    conn = get_connection(project_id)
    try:
        model = build_parallel_branch_read_model_from_db(
            conn,
            project_id=project_id,
            batch_id=str(ctx.query.get("batch_id") or ""),
            merge_queue_id=str(ctx.query.get("merge_queue_id") or ""),
            target_ref=target_ref,
            current_target_head=_parallel_branch_current_target_head(
                project_id,
                ctx.query,
                target_ref=target_ref,
            ),
            now_iso=str(ctx.query.get("now_iso") or ""),
            limit=_query_int(ctx.query, "limit", 50),
            scenario_id=str(ctx.query.get("scenario_id") or "PB-010"),
            severe_integration_failure=_query_bool(
                ctx.query,
                "severe_integration_failure",
                False,
            ),
            corrected_replay_order=tuple(_query_statuses(ctx.query, "corrected_replay_order")),
        )
        return {"ok": True, "read_model": model.to_dict()}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/parallel-branches/allocate")
def handle_graph_governance_parallel_branch_allocate(ctx: RequestContext):
    """Allocate and optionally materialize one parallel branch runtime context."""
    project_id = ctx.get_project_id()
    from . import batch_jobs
    from .db import sqlite_write_lock
    from .parallel_branch_runtime import (
        branch_context_to_dict,
        materialize_branch_worktree,
        plan_branch_runtime_context,
        upsert_branch_context,
    )

    task_id = str(ctx.body.get("task_id") or "").strip()
    if not task_id:
        raise ValidationError("task_id is required")

    workspace_root = str(
        ctx.body.get("workspace_root")
        or ctx.body.get("repo_root_path")
        or os.getcwd()
    )
    base_commit = str(ctx.body.get("base_commit") or "").strip()
    target_head_commit = str(ctx.body.get("target_head_commit") or "").strip()
    if _query_bool(ctx.body, "create_worktree", False) and not base_commit:
        base_commit = batch_jobs.git_commit(workspace_root)
    if not target_head_commit:
        target_head_commit = base_commit

    allocation_fence_token = str(ctx.body.get("fence_token") or f"fence-{uuid.uuid4().hex[:12]}")
    context = plan_branch_runtime_context(
        project_id=project_id,
        task_id=task_id,
        workspace_root=workspace_root,
        batch_id=str(ctx.body.get("batch_id") or ""),
        backlog_id=str(ctx.body.get("backlog_id") or ""),
        chain_id=str(ctx.body.get("chain_id") or ""),
        root_task_id=str(ctx.body.get("root_task_id") or ctx.body.get("parent_task_id") or ""),
        stage_task_id=str(ctx.body.get("stage_task_id") or task_id),
        stage_type=str(ctx.body.get("stage_type") or "mf_sub"),
        agent_id=str(ctx.body.get("agent_id") or ctx.body.get("actor") or ""),
        worker_id=str(ctx.body.get("worker_id") or ""),
        attempt=_query_int(ctx.body, "attempt", 1),
        branch_prefix=str(ctx.body.get("branch_prefix") or "codex"),
        worktree_root=str(ctx.body.get("worktree_root") or ".worktrees"),
        ref_name=str(ctx.body.get("ref_name") or ctx.body.get("target_branch") or "main"),
        base_commit=base_commit,
        target_head_commit=target_head_commit,
        merge_queue_id=str(ctx.body.get("merge_queue_id") or ""),
        fence_token=allocation_fence_token,
    )

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.parallel-branches.allocate")
        with sqlite_write_lock():
            saved = upsert_branch_context(
                conn,
                context,
                now_iso=str(ctx.body.get("now_iso") or ""),
            )
            conn.commit()

        worktree_result: dict[str, Any] | None = None
        if _query_bool(ctx.body, "create_worktree", False):
            worktree_result = materialize_branch_worktree(
                conn,
                project_id=project_id,
                task_id=task_id,
                repo_root_path=workspace_root,
                fence_token=allocation_fence_token,
                now_iso=str(ctx.body.get("now_iso") or ""),
            )
            conn.commit()
            saved = saved.__class__(**worktree_result["context"])

        return 201, {
            "ok": True,
            "project_id": project_id,
            "context": branch_context_to_dict(saved),
            "worktree": worktree_result["worktree"] if worktree_result else None,
            "branch_strategy": worktree_result["branch_strategy"] if worktree_result else None,
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/parallel-branches/checkpoint")
def handle_graph_governance_parallel_branch_checkpoint(ctx: RequestContext):
    """Record a checkpoint for a branch runtime context with fence protection."""
    project_id = ctx.get_project_id()
    from . import batch_jobs
    from .db import sqlite_write_lock
    from .parallel_branch_runtime import (
        branch_context_to_dict,
        get_branch_context,
        record_branch_checkpoint,
    )

    task_id = str(ctx.body.get("task_id") or "").strip()
    checkpoint_id = str(ctx.body.get("checkpoint_id") or "").strip()
    fence_token = str(ctx.body.get("fence_token") or "").strip()
    if not task_id:
        raise ValidationError("task_id is required")
    if not checkpoint_id:
        raise ValidationError("checkpoint_id is required")
    if not fence_token:
        raise ValidationError("fence_token is required")

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.parallel-branches.checkpoint")
        with sqlite_write_lock():
            head_commit = str(ctx.body.get("head_commit") or "").strip()
            if _query_bool(ctx.body, "refresh_head_from_worktree", False) or _query_bool(
                ctx.body,
                "refresh_head",
                False,
            ):
                current = get_branch_context(conn, project_id, task_id)
                if current is None:
                    raise KeyError(f"branch runtime context not found: {project_id}/{task_id}")
                worktree_path = str(current.worktree_path or "")
                actual_head = ""
                if worktree_path and os.path.exists(worktree_path):
                    try:
                        actual_head = batch_jobs.git_commit(worktree_path)
                    except batch_jobs.BatchJobError:
                        actual_head = ""
                if actual_head and head_commit and actual_head != head_commit:
                    raise ValidationError("head_commit does not match assigned worktree HEAD")
                head_commit = actual_head or head_commit
            context = record_branch_checkpoint(
                conn,
                project_id=project_id,
                task_id=task_id,
                checkpoint_id=checkpoint_id,
                fence_token=fence_token,
                head_commit=head_commit,
                replay_source=str(ctx.body.get("replay_source") or "checkpoint"),
                now_iso=str(ctx.body.get("now_iso") or ""),
            )
            conn.commit()
        return {"ok": True, "project_id": project_id, "context": branch_context_to_dict(context)}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/parallel-branches/finish-gate")
def handle_graph_governance_parallel_branch_finish_gate(ctx: RequestContext):
    """Validate an mf_sub worker finish claim and record a checkpoint."""
    project_id = ctx.get_project_id()
    from . import batch_jobs
    from .db import sqlite_write_lock
    from .mf_subagent_contract import (
        FINISH_GATE_REPLAY_SOURCE,
        validate_mf_subagent_finish_gate,
    )
    from .parallel_branch_runtime import (
        branch_context_to_dict,
        get_branch_context,
        record_branch_finish_gate,
    )

    task_id = str(ctx.body.get("task_id") or "").strip()
    if not task_id:
        raise ValidationError("task_id is required")

    conn = get_connection(project_id)
    try:
        _require_graph_governance_mf_subagent(ctx, conn, "graph-governance.parallel-branches.finish-gate")
        with sqlite_write_lock():
            context = get_branch_context(conn, project_id, task_id)
            if context is None:
                raise KeyError(f"branch runtime context not found: {project_id}/{task_id}")

            gate = validate_mf_subagent_finish_gate(ctx.body, context=context)
            claimed_head = str(gate.get("head_commit") or "").strip()
            actual_head = ""
            worktree_path = str(context.worktree_path or "")
            if worktree_path and os.path.exists(worktree_path):
                try:
                    actual_head = batch_jobs.git_commit(worktree_path)
                except batch_jobs.BatchJobError:
                    actual_head = ""
            if actual_head and claimed_head and actual_head != claimed_head:
                raise ValidationError("head_commit does not match assigned worktree HEAD")
            validated_head = actual_head or claimed_head or context.head_commit
            actual_changed_files: list[str] = []
            if worktree_path and os.path.exists(worktree_path) and context.base_commit:
                try:
                    actual_changed_files = batch_jobs.git_changed_files(
                        worktree_path,
                        base_ref=context.base_commit,
                        head_ref=validated_head or "HEAD",
                    )
                except batch_jobs.BatchJobError:
                    actual_changed_files = []
            if actual_changed_files:
                claimed_changed = set(gate.get("changed_files") or [])
                claimed_changed.update(gate.get("new_files") or [])
                actual_changed = set(actual_changed_files)
                if claimed_changed != actual_changed:
                    missing = sorted(actual_changed - claimed_changed)
                    extra = sorted(claimed_changed - actual_changed)
                    raise ValidationError(
                        "changed_files do not match assigned worktree diff"
                        f"; missing={missing}; extra={extra}"
                    )
                gate["validated_changed_files"] = sorted(actual_changed)

            saved = record_branch_finish_gate(
                conn,
                project_id=project_id,
                task_id=task_id,
                checkpoint_id=str(gate["checkpoint_id"]),
                fence_token=str(gate["fence_token"]),
                head_commit=validated_head,
                replay_source=FINISH_GATE_REPLAY_SOURCE,
                now_iso=str(ctx.body.get("now_iso") or ""),
            )
            gate["validated_head_commit"] = saved.head_commit
            gate["runtime_status"] = saved.status
            try:
                audit_service.record(
                    conn,
                    project_id,
                    "mf_subagent.finish_gate_passed",
                    actor=str(ctx.body.get("agent_id") or ctx.body.get("actor") or "mf_subagent"),
                    task_id=task_id,
                    bug_id=saved.backlog_id,
                    checkpoint_id=saved.checkpoint_id,
                )
            except Exception:
                pass
            conn.commit()
        return {
            "ok": True,
            "project_id": project_id,
            "gate": gate,
            "context": branch_context_to_dict(saved),
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/parallel-branches/merge-queue")
def handle_graph_governance_parallel_branch_merge_queue(ctx: RequestContext):
    """Enter one branch runtime context into the durable merge queue."""
    project_id = ctx.get_project_id()
    from .db import sqlite_write_lock
    from .parallel_branch_runtime import (
        decide_persisted_merge_queue,
        queue_merge_item_for_branch_context,
    )

    task_id = str(ctx.body.get("task_id") or "").strip()
    merge_queue_id = str(ctx.body.get("merge_queue_id") or "").strip()
    if not task_id:
        raise ValidationError("task_id is required")
    if not merge_queue_id:
        raise ValidationError("merge_queue_id is required")
    target_ref = str(ctx.body.get("target_ref") or "refs/heads/main")

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.parallel-branches.merge-queue")
        with sqlite_write_lock():
            queued = queue_merge_item_for_branch_context(
                conn,
                project_id=project_id,
                task_id=task_id,
                merge_queue_id=merge_queue_id,
                queue_item_id=str(ctx.body.get("queue_item_id") or ""),
                queue_index=_query_int(ctx.body, "queue_index", 0),
                status=str(ctx.body.get("status") or "queued_for_merge"),
                fence_token=str(ctx.body.get("fence_token") or ""),
                depends_on=tuple(_query_statuses(ctx.body, "depends_on")),
                hard_depends_on=tuple(_query_statuses(ctx.body, "hard_depends_on")),
                serializes_after=tuple(_query_statuses(ctx.body, "serializes_after")),
                conflicts_with=tuple(_query_statuses(ctx.body, "conflicts_with")),
                same_node_or_file_conflicts=tuple(
                    _query_statuses(ctx.body, "same_node_or_file_conflicts")
                ),
                requires_graph_epoch=tuple(_query_statuses(ctx.body, "requires_graph_epoch")),
                target_ref=target_ref,
                current_target_head=_parallel_branch_current_target_head(
                    project_id,
                    ctx.body,
                    target_ref=target_ref,
                ),
                validated_target_head=str(ctx.body.get("validated_target_head") or ""),
                validation_attempt=_query_int(ctx.body, "validation_attempt", 0),
                merge_preview_id=str(ctx.body.get("merge_preview_id") or ""),
                checkpoint_id=str(ctx.body.get("checkpoint_id") or ""),
                require_finish_gate=(
                    _query_bool(ctx.body, "require_finish_gate", False)
                    or str(ctx.body.get("worker_role") or "") == "mf_sub"
                ),
                now_iso=str(ctx.body.get("now_iso") or ""),
            )
            decision = decide_persisted_merge_queue(
                conn,
                project_id,
                merge_queue_id,
                target_ref=target_ref,
                current_target_head=_parallel_branch_current_target_head(
                    project_id,
                    ctx.body,
                    target_ref=target_ref,
                ),
                scenario_id=str(ctx.body.get("scenario_id") or "PB-002"),
            )
            conn.commit()
        return {
            "ok": True,
            "project_id": project_id,
            **queued,
            "decision": {
                "scenario_id": decision.scenario_id,
                "mergeable_task_ids": list(decision.mergeable_task_ids),
                "blocked_task_ids": list(decision.blocked_task_ids),
                "stale_task_ids": list(decision.stale_task_ids),
                "target_mutation_blocked_for": list(decision.target_mutation_blocked_for),
                "rows": list(decision.dashboard_rows),
            },
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/parallel-branches/merge-gate")
def handle_graph_governance_parallel_branch_merge_gate(ctx: RequestContext):
    """Return a side-effect-free merge gate plan for one queued branch."""
    project_id = ctx.get_project_id()
    from .parallel_branch_runtime import (
        decide_persisted_merge_gate,
        merge_gate_plan_to_dict,
    )

    merge_queue_id = str(ctx.body.get("merge_queue_id") or "").strip()
    if not merge_queue_id:
        raise ValidationError("merge_queue_id is required")
    evidence = ctx.body.get("evidence") or {}
    if not isinstance(evidence, dict):
        raise ValidationError("evidence must be an object when provided")

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.parallel-branches.merge-gate")
        plan = decide_persisted_merge_gate(
            conn,
            project_id,
            merge_queue_id,
            target_ref=str(ctx.body.get("target_ref") or "refs/heads/main"),
            queue_item_id=str(ctx.body.get("queue_item_id") or ""),
            task_id=str(ctx.body.get("task_id") or ""),
            evidence=evidence,
            batch_id=str(ctx.body.get("batch_id") or ""),
            batch_status=str(ctx.body.get("batch_status") or ""),
            dry_run=_query_bool(ctx.body, "dry_run", True),
            scenario_id=str(ctx.body.get("scenario_id") or "PB-013"),
        )
        return {
            "ok": True,
            "project_id": project_id,
            "plan": merge_gate_plan_to_dict(plan),
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/parallel-branches/merge-preview")
def handle_graph_governance_parallel_branch_merge_preview(ctx: RequestContext):
    """Return side-effect-free git merge preview evidence for a queued branch."""
    project_id = ctx.get_project_id()
    from .parallel_branch_runtime import (
        decide_persisted_merge_gate,
        git_merge_preview_evidence,
        list_merge_queue_items,
        merge_gate_plan_to_dict,
        select_merge_queue_item,
    )

    repo_root = str(
        ctx.body.get("repo_root_path")
        or ctx.body.get("workspace_root")
        or os.getcwd()
    )
    merge_queue_id = str(ctx.body.get("merge_queue_id") or "").strip()
    target_ref = str(ctx.body.get("target_ref") or "refs/heads/main")
    branch_ref = str(ctx.body.get("branch_ref") or "")
    expected_target_head = str(ctx.body.get("expected_target_head") or "")
    queue_item_id = str(ctx.body.get("queue_item_id") or "")
    task_id = str(ctx.body.get("task_id") or "")

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(
            ctx,
            conn,
            "graph-governance.parallel-branches.merge-preview",
        )
        if merge_queue_id:
            items = list_merge_queue_items(
                conn,
                project_id,
                merge_queue_id,
                target_ref=target_ref,
            )
            item = select_merge_queue_item(
                items,
                queue_item_id=queue_item_id,
                task_id=task_id,
            )
            branch_ref = branch_ref or item.branch_ref
            expected_target_head = (
                expected_target_head
                or item.current_target_head
                or item.validated_target_head
            )
            task_id = task_id or item.task_id
            queue_item_id = queue_item_id or item.queue_item_id
        if not branch_ref:
            raise ValidationError("branch_ref is required when merge_queue_id is not provided")

        preview = git_merge_preview_evidence(
            repo_root_path=repo_root,
            target_ref=target_ref,
            branch_ref=branch_ref,
            expected_target_head=expected_target_head,
            timeout_seconds=_query_int(ctx.body, "timeout_seconds", 30),
        )
        gate_plan = None
        if merge_queue_id and _query_bool(ctx.body, "include_gate_plan", False):
            evidence = ctx.body.get("evidence") or {}
            if not isinstance(evidence, dict):
                raise ValidationError("evidence must be an object when provided")
            gate_evidence = dict(evidence)
            gate_evidence["git_conflict_check"] = preview
            gate_plan = decide_persisted_merge_gate(
                conn,
                project_id,
                merge_queue_id,
                target_ref=target_ref,
                queue_item_id=queue_item_id,
                task_id=task_id,
                evidence=gate_evidence,
                batch_id=str(ctx.body.get("batch_id") or ""),
                batch_status=str(ctx.body.get("batch_status") or ""),
                dry_run=_query_bool(ctx.body, "dry_run", True),
                scenario_id=str(ctx.body.get("scenario_id") or "PB-015"),
            )
        return {
            "ok": True,
            "project_id": project_id,
            "merge_queue_id": merge_queue_id,
            "queue_item_id": queue_item_id,
            "task_id": task_id,
            "preview": preview,
            "gate_plan": merge_gate_plan_to_dict(gate_plan) if gate_plan is not None else None,
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/parallel-branches/merge-result")
def handle_graph_governance_parallel_branch_merge_result(ctx: RequestContext):
    """Record an externally executed merge result in branch runtime state."""
    project_id = ctx.get_project_id()
    from .db import sqlite_write_lock
    from .parallel_branch_runtime import (
        decide_persisted_merge_queue,
        record_merge_queue_result,
    )

    merge_queue_id = str(ctx.body.get("merge_queue_id") or "").strip()
    if not merge_queue_id:
        raise ValidationError("merge_queue_id is required")
    status = str(ctx.body.get("status") or "").strip()
    if not status:
        raise ValidationError("status is required")

    target_ref = str(ctx.body.get("target_ref") or "refs/heads/main")
    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(
            ctx,
            conn,
            "graph-governance.parallel-branches.merge-result",
        )
        with sqlite_write_lock():
            recorded = record_merge_queue_result(
                conn,
                project_id=project_id,
                merge_queue_id=merge_queue_id,
                queue_item_id=str(ctx.body.get("queue_item_id") or ""),
                task_id=str(ctx.body.get("task_id") or ""),
                target_ref=target_ref,
                status=status,
                merge_commit=str(ctx.body.get("merge_commit") or ""),
                target_head_before_merge=str(ctx.body.get("target_head_before_merge") or ""),
                target_head_after_merge=str(ctx.body.get("target_head_after_merge") or ""),
                failure_reason=str(ctx.body.get("failure_reason") or ""),
                snapshot_id=str(ctx.body.get("snapshot_id") or ""),
                projection_id=str(ctx.body.get("projection_id") or ""),
                fence_token=str(ctx.body.get("fence_token") or ""),
                now_iso=str(ctx.body.get("now_iso") or ""),
            )
            decision = decide_persisted_merge_queue(
                conn,
                project_id,
                merge_queue_id,
                target_ref=target_ref,
                current_target_head=str(
                    ctx.body.get("current_target_head")
                    or ctx.body.get("latest_target_head")
                    or ctx.body.get("target_head_after_merge")
                    or ""
                ),
                scenario_id=str(ctx.body.get("scenario_id") or "PB-014"),
            )
            conn.commit()
        return {
            "ok": True,
            "project_id": project_id,
            **recorded,
            "decision": {
                "scenario_id": decision.scenario_id,
                "mergeable_task_ids": list(decision.mergeable_task_ids),
                "blocked_task_ids": list(decision.blocked_task_ids),
                "stale_task_ids": list(decision.stale_task_ids),
                "target_mutation_blocked_for": list(decision.target_mutation_blocked_for),
                "rows": list(decision.dashboard_rows),
            },
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/parallel-branches/merge-execute")
def handle_graph_governance_parallel_branch_merge_execute(ctx: RequestContext):
    """Dry-run or explicitly execute one gated queue merge."""
    project_id = ctx.get_project_id()
    from .db import sqlite_write_lock
    from .parallel_branch_runtime import (
        decide_persisted_merge_queue,
        execute_merge_queue_item,
    )

    merge_queue_id = str(ctx.body.get("merge_queue_id") or "").strip()
    if not merge_queue_id:
        raise ValidationError("merge_queue_id is required")
    evidence = ctx.body.get("evidence") or {}
    if not isinstance(evidence, dict):
        raise ValidationError("evidence must be an object when provided")
    repo_root = str(
        ctx.body.get("repo_root_path")
        or ctx.body.get("workspace_root")
        or os.getcwd()
    )
    target_ref = str(ctx.body.get("target_ref") or "refs/heads/main")

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(
            ctx,
            conn,
            "graph-governance.parallel-branches.merge-execute",
        )
        with sqlite_write_lock():
            result = execute_merge_queue_item(
                conn,
                project_id=project_id,
                merge_queue_id=merge_queue_id,
                repo_root_path=repo_root,
                queue_item_id=str(ctx.body.get("queue_item_id") or ""),
                task_id=str(ctx.body.get("task_id") or ""),
                target_ref=target_ref,
                evidence=evidence,
                batch_status=str(ctx.body.get("batch_status") or ""),
                dry_run=_query_bool(ctx.body, "dry_run", True),
                allow_target_ref_mutation=_query_bool(
                    ctx.body,
                    "allow_target_ref_mutation",
                    False,
                ),
                message=str(ctx.body.get("message") or ""),
                bug_id=str(ctx.body.get("bug_id") or ""),
                fence_token=str(ctx.body.get("fence_token") or ""),
                now_iso=str(ctx.body.get("now_iso") or ""),
                timeout_seconds=_query_int(ctx.body, "timeout_seconds", 30),
                scenario_id=str(ctx.body.get("scenario_id") or "PB-016"),
            )
            decision = decide_persisted_merge_queue(
                conn,
                project_id,
                merge_queue_id,
                target_ref=target_ref,
                current_target_head=(
                    str(result.get("target_head_after_merge") or "").strip()
                    or _parallel_branch_current_target_head(
                        project_id,
                        ctx.body,
                        target_ref=target_ref,
                    )
                ),
                scenario_id=str(ctx.body.get("scenario_id") or "PB-016"),
            )
            conn.commit()
        return {
            "ok": bool(result.get("ok")),
            "project_id": project_id,
            **result,
            "decision": {
                "scenario_id": decision.scenario_id,
                "mergeable_task_ids": list(decision.mergeable_task_ids),
                "blocked_task_ids": list(decision.blocked_task_ids),
                "stale_task_ids": list(decision.stale_task_ids),
                "target_mutation_blocked_for": list(decision.target_mutation_blocked_for),
                "rows": list(decision.dashboard_rows),
            },
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/parallel-branches/batch-runtime")
def handle_graph_governance_parallel_branch_batch_runtime(ctx: RequestContext):
    """Persist batch runtime state and return rollback/replay planning."""
    project_id = ctx.get_project_id()
    from .db import sqlite_write_lock
    from .parallel_branch_runtime import (
        BATCH_STATE_OPEN,
        BATCH_STATE_ROLLBACK_REQUIRED,
        BatchMergeItem,
        BatchMergeRuntime,
        batch_merge_runtime_to_dict,
        batch_rollback_plan_to_dict,
        decide_persisted_batch_rollback_replay,
        list_branch_contexts,
        upsert_batch_merge_runtime,
    )

    batch_id = str(ctx.body.get("batch_id") or "").strip()
    if not batch_id:
        raise ValidationError("batch_id is required")

    explicit_items = ctx.body.get("items") or []
    if isinstance(explicit_items, dict):
        explicit_items = list(explicit_items.values())
    overrides = {
        str(item.get("task_id") or ""): item
        for item in explicit_items
        if isinstance(item, dict) and str(item.get("task_id") or "")
    }

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.parallel-branches.batch-runtime")
        contexts = list_branch_contexts(conn, project_id, batch_id=batch_id)
        if not contexts:
            raise ValidationError(f"no branch runtime contexts found for batch_id: {batch_id}")

        items: list[BatchMergeItem] = []
        for index, context in enumerate(contexts, start=1):
            override = overrides.get(context.task_id, {})
            items.append(
                BatchMergeItem(
                    task_id=context.task_id,
                    branch_ref=str(override.get("branch_ref") or context.branch_ref),
                    worktree_path=str(override.get("worktree_path") or context.worktree_path),
                    queue_index=int(override.get("queue_index") or index),
                    status=str(override.get("status") or context.status),
                    branch_head=str(override.get("branch_head") or context.head_commit),
                    base_commit=str(override.get("base_commit") or context.base_commit),
                    checkpoint_id=str(override.get("checkpoint_id") or context.checkpoint_id),
                    merge_commit=str(override.get("merge_commit") or ""),
                    target_head_before_merge=str(override.get("target_head_before_merge") or ""),
                    target_head_after_merge=str(override.get("target_head_after_merge") or ""),
                    snapshot_id=str(override.get("snapshot_id") or context.snapshot_id),
                    projection_id=str(override.get("projection_id") or context.projection_id),
                    merge_queue_id=str(override.get("merge_queue_id") or context.merge_queue_id),
                    merge_preview_id=str(override.get("merge_preview_id") or context.merge_preview_id),
                    depends_on=tuple(override.get("depends_on") or context.depends_on),
                    retained=not str(override.get("retained") or "").strip().lower() in {
                        "0",
                        "false",
                        "no",
                        "off",
                    },
                )
            )

        severe = _query_bool(ctx.body, "severe_integration_failure", False)
        batch_status = str(
            ctx.body.get("batch_status")
            or (BATCH_STATE_ROLLBACK_REQUIRED if severe else BATCH_STATE_OPEN)
        )
        batch_base_commit = str(ctx.body.get("batch_base_commit") or items[0].base_commit)
        runtime = BatchMergeRuntime(
            project_id=project_id,
            batch_id=batch_id,
            target_ref=str(ctx.body.get("target_ref") or "refs/heads/main"),
            batch_base_commit=batch_base_commit,
            current_target_head=str(ctx.body.get("current_target_head") or batch_base_commit),
            items=tuple(items),
            batch_status=batch_status,
            rollback_epoch=str(ctx.body.get("rollback_epoch") or ""),
            replay_epoch=str(ctx.body.get("replay_epoch") or ""),
            rollback_target_commit=str(ctx.body.get("rollback_target_commit") or batch_base_commit),
            rollback_snapshot_id=str(ctx.body.get("rollback_snapshot_id") or ""),
            rollback_projection_id=str(ctx.body.get("rollback_projection_id") or ""),
            failure_reason=str(ctx.body.get("failure_reason") or ""),
        )

        with sqlite_write_lock():
            saved = upsert_batch_merge_runtime(
                conn,
                runtime,
                now_iso=str(ctx.body.get("now_iso") or ""),
            )
            plan = decide_persisted_batch_rollback_replay(
                conn,
                project_id,
                batch_id,
                severe_integration_failure=severe,
                corrected_replay_order=tuple(_query_statuses(ctx.body, "corrected_replay_order")),
                scenario_id=str(ctx.body.get("scenario_id") or "PB-004"),
            )
            conn.commit()

        return {
            "ok": True,
            "project_id": project_id,
            "runtime": batch_merge_runtime_to_dict(saved),
            "plan": batch_rollback_plan_to_dict(plan),
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/parallel-branches/recover-expired")
def handle_graph_governance_parallel_branch_recover_expired(ctx: RequestContext):
    """Recover expired running branch contexts after an agent/session restart."""
    project_id = ctx.get_project_id()
    from .db import sqlite_write_lock
    from .parallel_branch_runtime import (
        branch_context_to_dict,
        recover_expired_branch_contexts,
    )

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.parallel-branches.recover-expired")
        with sqlite_write_lock():
            recovered = recover_expired_branch_contexts(
                conn,
                project_id,
                now_iso=str(ctx.body.get("now_iso") or ""),
                actor=str(ctx.body.get("actor") or "observer_recovery"),
            )
            conn.commit()
        return {
            "ok": True,
            "project_id": project_id,
            "recovered_count": len(recovered),
            "contexts": [branch_context_to_dict(context) for context in recovered],
        }
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/managed-refs")
def handle_graph_governance_managed_refs(ctx: RequestContext):
    """List same-project managed refs for existing long-lived branches."""
    project_id = ctx.get_project_id()
    from .managed_ref_runtime import (
        decide_managed_ref,
        decide_project_deletion_guard,
        list_managed_refs,
        managed_ref_to_dict,
    )

    conn = get_connection(project_id)
    try:
        refs = list_managed_refs(
            conn,
            project_id,
            include_archived=_query_bool(ctx.query, "include_archived", False),
            target_ref=str(ctx.query.get("target_ref") or ""),
        )
        current_target_head = str(ctx.query.get("current_target_head") or "")
        return {
            "ok": True,
            "project_id": project_id,
            "refs": [managed_ref_to_dict(ref) for ref in refs],
            "decisions": [
                decide_managed_ref(ref, current_target_head=current_target_head).to_dict()
                for ref in refs
            ],
            "deletion_guard": decide_project_deletion_guard(refs),
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/managed-refs/bootstrap/dry-run")
def handle_graph_governance_managed_ref_bootstrap_dry_run(ctx: RequestContext):
    """Dry-run same-project managed-ref bootstrap for existing branch refs."""
    project_id = ctx.get_project_id()
    from .managed_ref_runtime import build_managed_ref_bootstrap_plan

    body = ctx.body
    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.managed-refs.bootstrap.dry-run")
        refs, discovery = _managed_ref_bootstrap_refs_from_body(project_id, body)
        plan = build_managed_ref_bootstrap_plan(
            conn,
            project_id,
            refs,
            target_ref=str(body.get("target_ref") or "refs/heads/main"),
            target_head_commit=str(body.get("target_head_commit") or discovery.get("target_head_commit") or ""),
            managed_patterns=_body_string_list(body, "managed_patterns"),
            agent_patterns=_body_string_list(body, "agent_patterns"),
            ignored_patterns=_body_string_list(body, "ignored_patterns"),
            manage_unmatched_refs=_body_bool(body, "manage_unmatched_refs", False),
            evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else {},
        )
        return {
            **plan.to_dict(),
            "discovery": discovery,
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/managed-refs/bootstrap")
def handle_graph_governance_managed_ref_bootstrap(ctx: RequestContext):
    """Persist managed-ref bootstrap import/refresh actions."""
    project_id = ctx.get_project_id()
    from .db import sqlite_write_lock
    from .managed_ref_runtime import (
        apply_managed_ref_bootstrap_plan,
        build_managed_ref_bootstrap_plan,
    )

    body = ctx.body
    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.managed-refs.bootstrap")
        refs, discovery = _managed_ref_bootstrap_refs_from_body(project_id, body)
        plan = build_managed_ref_bootstrap_plan(
            conn,
            project_id,
            refs,
            target_ref=str(body.get("target_ref") or "refs/heads/main"),
            target_head_commit=str(body.get("target_head_commit") or discovery.get("target_head_commit") or ""),
            managed_patterns=_body_string_list(body, "managed_patterns"),
            agent_patterns=_body_string_list(body, "agent_patterns"),
            ignored_patterns=_body_string_list(body, "ignored_patterns"),
            manage_unmatched_refs=_body_bool(body, "manage_unmatched_refs", False),
            evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else {},
        )
        with sqlite_write_lock():
            result = apply_managed_ref_bootstrap_plan(
                conn,
                plan,
                actor=str(body.get("actor") or "api"),
                now_iso=str(body.get("now_iso") or ""),
            )
            conn.commit()
        return 201, {
            **result,
            "discovery": discovery,
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/managed-refs")
def handle_graph_governance_managed_ref_upsert(ctx: RequestContext):
    """Create or update a same-project managed ref context."""
    project_id = ctx.get_project_id()
    from .db import sqlite_write_lock
    from .managed_ref_runtime import (
        STATE_IMPORTED,
        ManagedRefContext,
        decide_managed_ref,
        get_managed_ref,
        managed_ref_to_dict,
        upsert_managed_ref,
    )

    ref_name = str(ctx.body.get("ref_name") or "").strip()
    if not ref_name:
        raise ValidationError("ref_name is required")
    evidence = ctx.body.get("evidence") or {}
    if not isinstance(evidence, dict):
        raise ValidationError("evidence must be an object when provided")

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.managed-refs.upsert")
        existing = get_managed_ref(conn, project_id, ref_name)
        base = managed_ref_to_dict(existing) if existing else {
            "project_id": project_id,
            "ref_name": ref_name,
        }
        for key in (
            "ref_type",
            "target_ref",
            "merge_base_commit",
            "ref_head_commit",
            "target_head_commit",
            "validated_target_head",
            "snapshot_id",
            "projection_id",
            "merge_preview_id",
            "merge_queue_id",
            "merge_commit",
            "rollback_epoch",
            "archive_policy",
            "status",
        ):
            if key in ctx.body:
                base[key] = str(ctx.body.get(key) or "")
        base.setdefault("target_ref", "refs/heads/main")
        base.setdefault("status", STATE_IMPORTED)
        base["evidence"] = {**dict(base.get("evidence") or {}), **evidence}
        context = ManagedRefContext(**base)
        with sqlite_write_lock():
            saved = upsert_managed_ref(
                conn,
                context,
                actor=str(ctx.body.get("actor") or "api"),
                operation_type=str(ctx.body.get("operation_type") or "upsert"),
                now_iso=str(ctx.body.get("now_iso") or ""),
            )
            conn.commit()
        return 201, {
            "ok": True,
            "project_id": project_id,
            "ref": managed_ref_to_dict(saved),
            "decision": decide_managed_ref(
                saved,
                current_target_head=str(ctx.body.get("current_target_head") or ""),
            ).to_dict(),
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/managed-refs/merged")
def handle_graph_governance_managed_ref_merged(ctx: RequestContext):
    """Record that a managed source ref merged into its target ref."""
    project_id = ctx.get_project_id()
    from .db import sqlite_write_lock
    from .managed_ref_runtime import (
        decide_managed_ref,
        managed_ref_to_dict,
        mark_managed_ref_merged,
    )

    ref_name = str(ctx.body.get("ref_name") or "").strip()
    merge_commit = str(ctx.body.get("merge_commit") or "").strip()
    target_head_commit = str(ctx.body.get("target_head_commit") or merge_commit).strip()
    if not ref_name:
        raise ValidationError("ref_name is required")
    if not merge_commit:
        raise ValidationError("merge_commit is required")

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.managed-refs.merged")
        with sqlite_write_lock():
            saved = mark_managed_ref_merged(
                conn,
                project_id,
                ref_name,
                merge_commit=merge_commit,
                target_head_commit=target_head_commit,
                merge_queue_id=str(ctx.body.get("merge_queue_id") or ""),
                actor=str(ctx.body.get("actor") or "api"),
                now_iso=str(ctx.body.get("now_iso") or ""),
            )
            conn.commit()
        return {
            "ok": True,
            "project_id": project_id,
            "ref": managed_ref_to_dict(saved),
            "decision": decide_managed_ref(saved).to_dict(),
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/managed-refs/archive")
def handle_graph_governance_managed_ref_archive(ctx: RequestContext):
    """Archive a resolved managed ref context without deleting the project."""
    project_id = ctx.get_project_id()
    from .db import sqlite_write_lock
    from .managed_ref_runtime import (
        archive_managed_ref,
        decide_project_deletion_guard,
        list_managed_refs,
        managed_ref_to_dict,
    )

    ref_name = str(ctx.body.get("ref_name") or "").strip()
    if not ref_name:
        raise ValidationError("ref_name is required")
    evidence = ctx.body.get("evidence") or {}
    if not isinstance(evidence, dict):
        raise ValidationError("evidence must be an object when provided")

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.managed-refs.archive")
        with sqlite_write_lock():
            saved = archive_managed_ref(
                conn,
                project_id,
                ref_name,
                actor=str(ctx.body.get("actor") or "api"),
                evidence=evidence,
                now_iso=str(ctx.body.get("now_iso") or ""),
            )
            refs = list_managed_refs(conn, project_id, include_archived=True)
            conn.commit()
        return {
            "ok": True,
            "project_id": project_id,
            "ref": managed_ref_to_dict(saved),
            "deletion_guard": decide_project_deletion_guard(refs),
        }
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/dashboard")
def handle_graph_governance_dashboard(ctx: RequestContext):
    """Return a compact dashboard projection over graph, drift, and file state."""
    project_id = ctx.get_project_id()
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        status = store.graph_governance_status(conn, project_id)
        snapshot_id = str(ctx.query.get("snapshot_id") or status.get("active_snapshot_id") or "")
        snapshots = store.list_graph_snapshots(conn, project_id, limit=_query_int(ctx.query, "snapshot_limit", 10))
        file_state: dict = {
            "summary": {},
            "total_count": 0,
            "sample": [],
        }
        if snapshot_id:
            try:
                files = store.list_graph_snapshot_files(
                    conn,
                    project_id,
                    snapshot_id,
                    limit=_query_int(ctx.query, "file_sample_limit", 10),
                    scan_status=str(ctx.query.get("scan_status") or ""),
                )
                file_state = {
                    "summary": files["summary"],
                    "total_count": files["total_count"],
                    "filtered_count": files["filtered_count"],
                    "sample": files["files"],
                }
            except KeyError:
                file_state["error"] = "snapshot_file_inventory_not_found"
        drift_rows = store.list_graph_drift(
            conn,
            project_id,
            snapshot_id=snapshot_id,
            limit=_query_int(ctx.query, "drift_limit", 1000),
        ) if snapshot_id else []
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "status": status,
            "current_state": _dashboard_current_state(
                conn,
                project_id,
                status=status,
                snapshot_id=snapshot_id,
            ),
            "recent_snapshots": snapshots,
            "file_state": file_state,
            "drift_summary": _summarize_graph_drift_rows(drift_rows),
            "drift_sample": drift_rows[:_query_int(ctx.query, "drift_sample_limit", 20)],
        }
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/dashboard/active")
def handle_graph_governance_dashboard_active_bundle(ctx: RequestContext):
    """Return the common active dashboard payload in one request."""
    project_id = ctx.get_project_id()
    from . import graph_events
    from . import graph_snapshot_store as store
    from . import reconcile_feedback

    conn = get_connection(project_id)
    try:
        status = store.graph_governance_status(conn, project_id)
        snapshot_id = str(ctx.query.get("snapshot_id") or status.get("active_snapshot_id") or "")
        if not snapshot_id:
            current_state = _dashboard_current_state(
                conn,
                project_id,
                status=status,
                snapshot_id="",
            )
            return {
                "ok": True,
                "project_id": project_id,
                "snapshot_id": "",
                "status": status,
                "current_state": current_state,
                "summary": {},
                "nodes": [],
                "edges": [],
                "files": {"summary": {}, "files": [], "total_count": 0, "filtered_count": 0},
                "events": {"events": [], "count": 0, "summary": {}},
                "feedback_queue": {"items": [], "groups": [], "count": 0},
                "semantic_jobs": {"jobs": [], "summary": {}},
                "semantic_projection": {"projection_id": "", "health": {}},
                "graph_structure_hints": _empty_graph_structure_hint_projection(),
                "commit_timeline": {"commits": [], "count": 0},
            }
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        node_limit = _query_int(ctx.query, "node_limit", 500)
        edge_limit = _query_int(ctx.query, "edge_limit", 1000)
        event_limit = _query_int(ctx.query, "event_limit", 50)
        feedback_limit = _query_int(ctx.query, "feedback_limit", 50)
        job_limit = _query_int(ctx.query, "job_limit", 50)
        file_limit = _query_int(ctx.query, "file_limit", 50)
        commit_limit = _query_int(ctx.query, "commit_limit", 20)

        events = graph_events.list_events(conn, project_id, snapshot_id, limit=event_limit)
        job_counts = _semantic_job_status_counts(conn, project_id, snapshot_id)
        commits = store.list_commit_timeline(
            conn,
            project_id,
            limit=commit_limit,
            include_backlog=_query_bool(ctx.query, "include_backlog", True),
        )
        feedback_queue = reconcile_feedback.build_feedback_review_queue(
            project_id,
            snapshot_id,
            include_status_observations=_query_bool(ctx.query, "include_status_observations", False),
            include_resolved=_query_bool(ctx.query, "include_resolved", False),
            include_claimed=True,
            limit=feedback_limit,
            conn=conn,
        )
        projection = graph_events.get_semantic_projection(conn, project_id, snapshot_id) or {}
        summary = store.summarize_graph_snapshot(conn, project_id, snapshot_id)
        snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id) or {}
        graph_structure_hints = _graph_structure_hint_projection_from_snapshot(snapshot)
        current_state = _dashboard_current_state(
            conn,
            project_id,
            status=status,
            snapshot_id=snapshot_id,
            snapshot_summary=summary,
            projection=projection,
        )
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "status": status,
            "current_state": current_state,
            "summary": summary,
            "nodes": store.list_graph_snapshot_nodes(
                conn,
                project_id,
                snapshot_id,
                limit=node_limit,
                include_semantic=_query_bool(ctx.query, "include_node_semantic", True),
            ),
            "edges": store.list_graph_snapshot_edges(
                conn,
                project_id,
                snapshot_id,
                limit=edge_limit,
                edge_type=str(ctx.query.get("edge_type") or ""),
            ),
            "files": store.list_graph_snapshot_files(
                conn,
                project_id,
                snapshot_id,
                limit=file_limit,
                file_kind=str(ctx.query.get("file_kind") or ""),
                scan_status=str(ctx.query.get("scan_status") or ""),
                graph_status=str(ctx.query.get("graph_status") or ""),
                decision=str(ctx.query.get("decision") or ""),
            ),
            "events": {
                "events": events,
                "count": len(events),
                "summary": {"by_status": graph_events.status_counts(conn, project_id, snapshot_id)},
            },
            "feedback_queue": feedback_queue,
            "semantic_jobs": {
                "jobs": _semantic_job_rows(conn, project_id, snapshot_id, limit=job_limit),
                "summary": {
                    "by_status": job_counts,
                    "progress": _semantic_job_progress(job_counts),
                },
            },
            "semantic_projection": {
                "projection_id": projection.get("projection_id", ""),
                "event_watermark": projection.get("event_watermark", 0),
                "base_commit": projection.get("base_commit", ""),
                "health": projection.get("health", {}),
            },
            "graph_structure_hints": graph_structure_hints,
            "commit_timeline": {
                "commits": commits,
                "count": len(commits),
            },
            "endpoints": {
                "summary": f"/api/graph-governance/{project_id}/snapshots/{snapshot_id}/summary",
                "nodes": f"/api/graph-governance/{project_id}/snapshots/{snapshot_id}/nodes",
                "edges": f"/api/graph-governance/{project_id}/snapshots/{snapshot_id}/edges",
                "files": f"/api/graph-governance/{project_id}/snapshots/{snapshot_id}/files",
                "events": f"/api/graph-governance/{project_id}/snapshots/{snapshot_id}/events",
                "semantic_jobs": f"/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic/jobs",
                "semantic_projection": f"/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic/projection",
                "feedback_queue": f"/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/queue",
                "graph_structure_hints": f"/api/graph-governance/{project_id}/snapshots/{snapshot_id}/graph-structure-hints",
            },
        }
    finally:
        conn.close()


def _normalize_operation_status(status: str) -> str:
    value = str(status or "").strip()
    if value in {"pending_ai", "ai_pending"}:
        return "ai_pending"
    if value in {"running", "claimed", "ai_reviewing"}:
        return "running"
    if value in {"proposed", "review_required"}:
        return "review_required"
    if value in {"rule_complete", "heuristic_complete"}:
        return "rule_complete"
    if value in {"ai_complete", "complete", "succeeded", "reviewed", "materialized"}:
        return "complete"
    if value in {"ai_error", "failed", "error"}:
        return "failed"
    if value in {"cancelled", "canceled", "rejected"}:
        return "cancelled"
    if value in {"blocked", "not_queued", "queued"}:
        return value
    return value or "queued"


def _operation_unit_progress(status: str) -> dict[str, int]:
    normalized = _normalize_operation_status(status)
    return {"done": 1 if normalized in {"complete", "cancelled", "failed"} else 0, "total": 1}


def _json_loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return default
    return default


def _git_head_commit(project_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=project_root,
        )
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _git_output(project_root: Path, args: list[str], *, timeout: int = 5) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=project_root,
        )
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _git_refs_for_root(project_root: Path) -> dict[str, Any]:
    """Return a small read-only git ref summary for dashboard project rows."""
    head_commit = _git_output(project_root, ["rev-parse", "HEAD"])
    current_branch = _git_output(project_root, ["branch", "--show-current"])
    branch_raw = _git_output(
        project_root,
        ["for-each-ref", "--format=%(refname:short)", "refs/heads"],
    )
    branches = sorted({line.strip() for line in branch_raw.splitlines() if line.strip()})
    tag_raw = _git_output(
        project_root,
        ["for-each-ref", "--format=%(refname:short)", "refs/tags"],
    )
    tags = sorted({line.strip() for line in tag_raw.splitlines() if line.strip()})
    return {
        "head_commit": head_commit,
        "current_branch": current_branch,
        "branches": branches,
        "tags": tags,
        "is_git_repo": bool(head_commit),
    }


def _git_managed_ref_bootstrap_rows(
    project_root: Path,
    *,
    target_ref: str,
    target_head_commit: str = "",
    include_remotes: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Discover branch refs and code-distance evidence for managed-ref bootstrap."""
    from .managed_ref_runtime import normalize_managed_ref_name

    normalized_target = normalize_managed_ref_name(target_ref) or "refs/heads/main"
    target_head = target_head_commit or _git_output(
        project_root,
        ["rev-parse", "--verify", f"{normalized_target}^{{commit}}"],
    )
    namespaces = ["refs/heads"]
    if include_remotes:
        namespaces.append("refs/remotes")
    raw = _git_output(
        project_root,
        ["for-each-ref", "--format=%(refname)%00%(objectname)", *namespaces],
        timeout=10,
    )
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.strip() or "\x00" not in line:
            continue
        ref_name, ref_head = line.split("\x00", 1)
        ref_name = ref_name.strip()
        ref_head = ref_head.strip()
        if not ref_name or ref_name.endswith("/HEAD"):
            continue
        merge_base = ""
        ahead_count = 0
        behind_count = 0
        if target_head:
            merge_base = _git_output(project_root, ["merge-base", normalized_target, ref_name], timeout=10)
            counts = _git_output(
                project_root,
                ["rev-list", "--left-right", "--count", f"{normalized_target}...{ref_name}"],
                timeout=10,
            )
            behind_count, ahead_count = _parse_git_ahead_behind(counts)
        rows.append({
            "ref_name": ref_name,
            "ref_head_commit": ref_head,
            "target_ref": normalized_target,
            "target_head_commit": target_head,
            "merge_base_commit": merge_base,
            "ahead_count": ahead_count,
            "behind_count": behind_count,
            "evidence": {
                "source": "git_for_each_ref",
                "project_root": str(project_root),
                "include_remotes": include_remotes,
            },
        })
    return rows, {
        "source": "git_for_each_ref",
        "project_root": str(project_root),
        "target_ref": normalized_target,
        "target_head_commit": target_head,
        "include_remotes": include_remotes,
        "ref_count": len(rows),
        "is_git_repo": bool(raw),
    }


def _parse_git_ahead_behind(raw: str) -> tuple[int, int]:
    parts = str(raw or "").split()
    if len(parts) < 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def _git_ref_exists(project_root: Path, ref_name: str) -> bool:
    ref_name = str(ref_name or "").strip()
    if not ref_name:
        return False
    resolved = _git_output(project_root, ["rev-parse", "--verify", f"{ref_name}^{{commit}}"])
    return bool(resolved)


def _git_changed_paths_between(project_root: Path, base_commit: str, target_commit: str, *, limit: int | None = 25) -> list[str]:
    base_commit = str(base_commit or "").strip()
    target_commit = str(target_commit or "").strip()
    if not base_commit or not target_commit:
        return []
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base_commit}..{target_commit}", "--", "."],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=project_root,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    paths = [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]
    if limit is None:
        return paths
    return paths[: max(0, int(limit))]


def _git_commit_range(project_root: Path, base_commit: str, target_commit: str) -> list[str]:
    base_commit = str(base_commit or "").strip()
    target_commit = str(target_commit or "").strip()
    if not base_commit or not target_commit or base_commit == target_commit:
        return []
    try:
        result = subprocess.run(
            ["git", "rev-list", "--reverse", f"{base_commit}..{target_commit}"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=project_root,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _current_graph_rule_fingerprint(project_root: Path) -> dict[str, Any]:
    from .graph_rule_fingerprint import build_graph_rule_fingerprint

    return build_graph_rule_fingerprint(project_root, include_source_hints=False)


def _graph_rule_fingerprint_status(
    project_root: Path,
    status: dict[str, Any],
) -> dict[str, Any]:
    from .graph_rule_fingerprint import compare_rule_fingerprint

    snapshot_fingerprint = status.get("active_snapshot_rule_fingerprint")
    if not isinstance(snapshot_fingerprint, dict) or not snapshot_fingerprint.get("fingerprint"):
        return {
            "available": False,
            "mismatch": False,
            "snapshot_fingerprint": "",
            "current_fingerprint": "",
        }
    try:
        current_fingerprint = _current_graph_rule_fingerprint(project_root)
    except Exception as exc:
        return {
            "available": False,
            "mismatch": False,
            "snapshot_fingerprint": str(snapshot_fingerprint.get("fingerprint") or ""),
            "current_fingerprint": "",
            "error": type(exc).__name__,
        }
    return compare_rule_fingerprint(snapshot_fingerprint, current_fingerprint)


def _graph_stale_scope_operation(
    project_id: str,
    *,
    status: dict[str, Any],
    pending_rows: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    active_warnings = [
        dict(item) for item in status.get("active_snapshot_warnings") or []
        if isinstance(item, dict)
    ]
    stale_summary: dict[str, Any] = {
        "is_stale": False,
        "active_graph_commit": str(status.get("graph_snapshot_commit") or ""),
        "head_commit": "",
        "changed_files": [],
        "changed_file_count": 0,
        "active_snapshot_warnings": active_warnings,
    }
    try:
        root = _graph_governance_project_root(project_id, {})
    except Exception:
        return None, stale_summary
    head_commit = _git_head_commit(root)
    graph_commit = str(status.get("graph_snapshot_commit") or "")
    stale_summary["head_commit"] = head_commit
    if not head_commit or not graph_commit:
        return None, stale_summary
    rule_fingerprint = _graph_rule_fingerprint_status(root, status)
    if rule_fingerprint.get("snapshot_fingerprint") or rule_fingerprint.get("current_fingerprint"):
        stale_summary["rule_fingerprint"] = rule_fingerprint
    rule_fingerprint_mismatch = bool(rule_fingerprint.get("mismatch"))
    if head_commit == graph_commit:
        if rule_fingerprint_mismatch:
            stale_summary.update({
                "is_stale": True,
                "stale_reason": "rule_fingerprint_mismatch",
                "recommended_action": "run_full_reconcile",
                "requires_reconcile": True,
            })
            operation = {
                "operation_id": f"scope-reconcile:rule-fingerprint:{head_commit[:12]}",
                "operation_type": "scope_reconcile",
                "target_scope": "snapshot",
                "target_id": head_commit,
                "target_label": head_commit[:12],
                "status": "not_queued",
                "progress": {"done": 0, "total": 1},
                "created_at": "",
                "updated_at": "",
                "claimed_by": "",
                "worker_id": "",
                "lease_expires_at": "",
                "last_error": "",
                "last_result": "graph rule fingerprint changed; run full reconcile before trusting active graph",
                "supported_actions": ["run_full_reconcile", "view_trace", "file_backlog"],
                "active_graph_commit": graph_commit,
                "head_commit": head_commit,
                "changed_files": [],
                "warnings": active_warnings,
                "rule_fingerprint": rule_fingerprint,
                "recommended_action": "run_full_reconcile",
            }
            return operation, stale_summary
        if not active_warnings:
            return None, stale_summary
        operation = {
            "operation_id": f"scope-reconcile:suspect-root:{graph_commit[:12]}",
            "operation_type": "scope_reconcile",
            "target_scope": "snapshot",
            "target_id": graph_commit,
            "target_label": graph_commit[:12],
            "status": "not_queued",
            "progress": {"done": 0, "total": 1},
            "created_at": "",
            "updated_at": "",
            "claimed_by": "",
            "worker_id": "",
            "lease_expires_at": "",
            "last_error": "",
            "last_result": "active graph snapshot has materialization provenance warnings",
            "supported_actions": ["queue_scope_reconcile", "view_trace", "file_backlog"],
            "active_graph_commit": graph_commit,
            "head_commit": head_commit,
            "changed_files": [],
            "warnings": active_warnings,
        }
        return operation, stale_summary
    all_changed_files = _git_changed_paths_between(root, graph_commit, head_commit, limit=None)
    if not all_changed_files:
        return None, stale_summary
    changed_files = all_changed_files[:25]
    stale_summary.update({
        "is_stale": True,
        "changed_files": changed_files,
        "changed_file_count": len(all_changed_files),
    })
    if rule_fingerprint_mismatch:
        stale_summary.update({
            "stale_reason": "commit_and_rule_fingerprint_mismatch",
            "recommended_action": "run_full_reconcile",
            "requires_reconcile": True,
        })
    pending_for_head = any(
        str(row.get("commit_sha") or row.get("target_commit") or "") == head_commit
        and str(row.get("ref_name") or "active") == "active"
        and not str(row.get("worktree_id") or "")
        for row in pending_rows
        if _normalize_operation_status(str(row.get("status") or "")) in {"queued", "running", "failed"}
    )
    if pending_for_head:
        return None, stale_summary
    changed_hint = f"; {len(all_changed_files)} changed files since snapshot" if all_changed_files else ""
    operation = {
        "operation_id": f"scope-reconcile:stale:{head_commit[:12]}",
        "operation_type": "scope_reconcile",
        "target_scope": "snapshot",
        "target_id": head_commit,
        "target_label": head_commit[:12],
        "status": "not_queued",
        "progress": {"done": 0, "total": 1},
        "created_at": "",
        "updated_at": "",
        "claimed_by": "",
        "worker_id": "",
        "lease_expires_at": "",
        "last_error": "",
        "last_result": f"active graph at {graph_commit[:12]}, HEAD at {head_commit[:12]}{changed_hint}",
        "supported_actions": ["queue_scope_reconcile", "view_trace", "file_backlog"],
        "active_graph_commit": graph_commit,
        "head_commit": head_commit,
        "changed_files": changed_files,
        "warnings": active_warnings,
    }
    return operation, stale_summary


def _semantic_current_state(
    conn: sqlite3.Connection,
    project_id: str,
    snapshot_id: str,
    *,
    snapshot_summary: dict[str, Any] | None = None,
    projection: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return compact semantic projection metadata and dashboard drift counts."""
    snapshot_id = str(snapshot_id or "")
    semantic_snapshot: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "projection_id": "",
        "base_commit": "",
        "event_watermark": 0,
        "projection_status": "missing",
        "created_at": "",
        "updated_at": "",
    }
    health: dict[str, Any] = {}
    if snapshot_id:
        if projection is None:
            from . import graph_events

            projection = graph_events.get_semantic_projection(conn, project_id, snapshot_id) or {}
        if projection:
            health = projection.get("health") if isinstance(projection.get("health"), dict) else {}
            semantic_snapshot.update({
                "projection_id": projection.get("projection_id", ""),
                "base_commit": projection.get("base_commit", ""),
                "event_watermark": projection.get("event_watermark", 0),
                "projection_status": projection.get("status", "current") or "current",
                "created_at": projection.get("created_at", ""),
                "updated_at": projection.get("updated_at", ""),
            })
        if not health and snapshot_summary:
            summary_health = snapshot_summary.get("health") if isinstance(snapshot_summary.get("health"), dict) else {}
            health = summary_health.get("semantic_health") if isinstance(summary_health.get("semantic_health"), dict) else {}
            if health and not semantic_snapshot["projection_id"]:
                semantic_snapshot["projection_id"] = str(health.get("projection_id") or "")
                semantic_snapshot["projection_status"] = str(health.get("status") or "current")

    node_current = int(health.get("semantic_current_count") or 0)
    node_unverified = int(health.get("semantic_unverified_hash_count") or 0)
    node_missing = int(health.get("semantic_missing_count") or 0)
    node_stale = int(health.get("semantic_stale_count") or 0)
    edge_eligible = int(health.get("edge_semantic_eligible_count") or 0)
    edge_current = int(health.get("edge_semantic_current_count") or 0)
    edge_requested = int(health.get("edge_semantic_requested_count") or 0)
    edge_rule = int(health.get("edge_semantic_rule_count") or 0)
    edge_missing = int(health.get("edge_semantic_missing_count") or 0)
    edge_needs_ai = int(health.get("edge_semantic_needs_ai_count") or edge_missing or 0)
    edge_unqueued = int(health.get("edge_semantic_unqueued_count") or 0)
    semantic_drift = {
        "node_total": health.get("feature_count", 0),
        "node_current": node_current,
        "node_unverified": node_unverified,
        "node_missing": node_missing,
        "node_stale": node_stale,
        "edge_eligible": edge_eligible,
        "edge_current": edge_current,
        "edge_requested": edge_requested,
        "edge_rule": edge_rule,
        "edge_missing": edge_missing,
        "edge_needs_ai": edge_needs_ai,
        "edge_unqueued": edge_unqueued,
        "edge_payload_current": int(health.get("edge_semantic_payload_current_count") or 0),
        "semantic_status_counts": health.get("semantic_status_counts", {}),
        "edge_semantic_status_counts": health.get("edge_semantic_status_counts", {}),
        "has_drift": bool(node_unverified or node_missing or node_stale or edge_needs_ai),
    }
    return semantic_snapshot, semantic_drift


def _dashboard_current_state(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    status: dict[str, Any],
    snapshot_id: str = "",
    snapshot_summary: dict[str, Any] | None = None,
    projection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Canonical dashboard state for graph staleness and semantic drift."""
    from . import graph_snapshot_store as store

    snapshot_id = str(snapshot_id or status.get("active_snapshot_id") or "")
    pending_rows = list(status.get("pending_scope_reconcile") or [])
    _operation, graph_stale = _graph_stale_scope_operation(
        project_id,
        status=status,
        pending_rows=pending_rows,
    )
    semantic_snapshot, semantic_drift = _semantic_current_state(
        conn,
        project_id,
        snapshot_id,
        snapshot_summary=snapshot_summary,
        projection=projection,
    )
    drift_rows = store.list_graph_drift(
        conn,
        project_id,
        snapshot_id=snapshot_id,
        limit=1000,
    ) if snapshot_id else []
    return {
        "snapshot_id": snapshot_id,
        "graph_stale": graph_stale,
        "semantic_snapshot": semantic_snapshot,
        "semantic_drift": semantic_drift,
        "drift_ledger": {
            "count": len(drift_rows),
            "ledger_only": True,
            "description": "Explicit graph_drift_ledger rows only; current graph-vs-HEAD state is graph_stale.",
        },
    }


@route("GET", "/api/graph-governance/{project_id}/operations/queue")
def handle_graph_governance_operations_queue(ctx: RequestContext):
    """Return a unified dashboard queue for active governance operations."""
    project_id = ctx.get_project_id()
    from . import graph_correction_patches
    from . import graph_events
    from . import graph_snapshot_store as store
    from . import reconcile_feedback

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.operations.queue")
        status = store.graph_governance_status(conn, project_id)
        snapshot_id = str(ctx.query.get("snapshot_id") or status.get("active_snapshot_id") or "")
        snapshot_summary: dict[str, Any] = {}
        if snapshot_id:
            snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
            snapshot_summary = store.summarize_graph_snapshot(conn, project_id, snapshot_id)
        job_limit = _query_int(ctx.query, "job_limit", 200)
        feedback_limit = _query_int(ctx.query, "feedback_limit", 100)
        # MF-2026-05-10-013: default-hide terminal node + edge rows so the
        # dashboard isn't drowned by cancelled / completed audit history.
        # Pass `?include_terminal=true` to see everything.
        include_terminal = _query_bool(ctx.query, "include_terminal", False)
        node_jobs = _semantic_job_rows(conn, project_id, snapshot_id, limit=job_limit) if snapshot_id else []
        edge_jobs = _edge_semantic_job_rows(conn, project_id, snapshot_id, limit=job_limit) if snapshot_id else []
        graph_structure_jobs = (
            graph_events.list_events(
                conn,
                project_id,
                snapshot_id,
                event_types=[
                    "graph_structure_requested",
                    "graph_structure_completed",
                    "graph_structure_failed",
                    "graph_enrich_config_requested",
                    "graph_enrich_config_completed",
                    "graph_enrich_config_failed",
                ],
                limit=job_limit,
            )
            if snapshot_id
            else []
        )
        if not include_terminal:
            node_jobs = [
                j for j in node_jobs
                if _semantic_cancel_status_bucket(str(j.get("status") or "")) != "terminal"
            ]
            edge_jobs = [
                j for j in edge_jobs
                if _semantic_cancel_status_bucket(str(j.get("status") or "")) != "terminal"
            ]
            graph_structure_jobs = [
                j for j in graph_structure_jobs
                if _normalize_operation_status(str(j.get("status") or "")) not in {"complete", "cancelled", "failed"}
                or str(j.get("event_type") or "") in {"graph_structure_requested", "graph_enrich_config_requested"}
            ]
        node_job_counts = _semantic_job_status_counts(conn, project_id, snapshot_id) if snapshot_id else {}
        edge_job_counts = _edge_semantic_job_status_counts(conn, project_id, snapshot_id) if snapshot_id else {}
        feedback_queue = (
            reconcile_feedback.build_feedback_review_queue(
                project_id,
                snapshot_id,
                include_status_observations=_query_bool(ctx.query, "include_status_observations", False),
                include_resolved=_query_bool(ctx.query, "include_resolved", False),
                include_claimed=True,
                require_current_semantic=_query_bool(ctx.query, "require_current_semantic", False),
                limit=feedback_limit,
                conn=conn,
            )
            if snapshot_id
            else {"summary": {}, "groups": [], "count": 0, "group_count": 0}
        )
        patch_summary = graph_correction_patches.correction_patch_summary(conn, project_id)
        operations: list[dict[str, Any]] = []
        pending_scope_rows = list(status.get("pending_scope_reconcile") or [])
        stale_operation, graph_stale_summary = _graph_stale_scope_operation(
            project_id,
            status=status,
            pending_rows=pending_scope_rows,
        )

        for row in pending_scope_rows:
            op_status = _normalize_operation_status(row.get("status", "queued"))
            commit = str(row.get("commit_sha") or row.get("target_commit") or "")
            ref_name = str(row.get("ref_name") or "active")
            branch_ref = str(row.get("branch_ref") or "")
            worktree_id = str(row.get("worktree_id") or "")
            worktree_path = str(row.get("worktree_path") or "")
            scope_key = commit
            if ref_name != "active" or worktree_id:
                key_parts = [ref_name or "active"]
                if worktree_id:
                    key_parts.append(worktree_id)
                key_parts.append(commit or str(len(operations)))
                scope_key = ":".join(key_parts)
            target_label = commit[:12]
            if ref_name != "active":
                target_label = f"{target_label} @ {ref_name}" if target_label else ref_name
            evidence = _json_loads(row.get("evidence_json"), {})
            evidence_summary = evidence if isinstance(evidence, dict) else {}
            last_error = str(
                row.get("last_error")
                or evidence_summary.get("error")
                or evidence_summary.get("reason")
                or ""
            )
            last_result = str(
                row.get("result_json")
                or evidence_summary.get("recovery_action")
                or evidence_summary.get("source")
                or ""
            )
            supported_actions = ["observer_takeover", "view_trace"]
            if op_status in {"failed", "running", "queued"}:
                supported_actions.insert(0, "retry_scope_reconcile")
            operations.append({
                "operation_id": f"scope-reconcile:{scope_key or len(operations)}",
                "operation_type": "scope_reconcile",
                "target_scope": "snapshot",
                "target_id": commit,
                "target_label": target_label,
                "status": op_status,
                "progress": _operation_unit_progress(op_status),
                "created_at": row.get("queued_at", ""),
                "updated_at": row.get("updated_at", row.get("started_at", "")),
                "claimed_by": row.get("claimed_by", ""),
                "worker_id": row.get("worker_id", ""),
                "lease_expires_at": row.get("lease_expires_at", ""),
                "last_error": last_error,
                "last_result": last_result,
                "evidence": evidence_summary,
                "ref_name": ref_name,
                "branch_ref": branch_ref,
                "worktree_id": worktree_id,
                "worktree_path": worktree_path,
                # MF-2026-05-10-011: cancel removed from scope_reconcile actions.
                "supported_actions": supported_actions,
            })

        if stale_operation:
            operations.append(stale_operation)

        for job in node_jobs:
            job_operation_type = str(job.get("operation_type") or "").strip().lower().replace("-", "_")
            is_summary_job = job_operation_type == "ai_summary"
            operation_type = "ai_summary" if is_summary_job else "node_semantic"
            operation_prefix = "ai-summary" if is_summary_job else "node-semantic"
            target_id = job.get("node_id", "")
            op_status = _normalize_operation_status(job.get("status", ""))
            operations.append({
                "operation_id": f"{operation_prefix}:{job.get('job_id') or target_id}",
                "operation_type": operation_type,
                "target_scope": "node",
                "target_id": target_id,
                "target_label": f"{target_id} summary" if is_summary_job else target_id,
                "status": op_status,
                "progress": _operation_unit_progress(op_status),
                "created_at": job.get("created_at", ""),
                "updated_at": job.get("updated_at", ""),
                "claimed_by": job.get("claimed_by", ""),
                "worker_id": job.get("worker_id", ""),
                "lease_expires_at": job.get("lease_expires_at", ""),
                "last_error": job.get("last_error", ""),
                "last_result": job_operation_type,
                "trace_id": job.get("claim_id", ""),
                "supported_actions": ["retry", "cancel", "view_trace"],
            })

        for job in edge_jobs:
            op_status = _normalize_operation_status(job.get("status", ""))
            if op_status == "ai_pending":
                supported_actions = ["run_edge_semantics", "cancel", "view_trace", "file_backlog"]
            elif op_status == "rule_complete":
                supported_actions = ["run_edge_semantics", "retry", "view_trace", "file_backlog"]
            elif op_status in {"failed", "cancelled"}:
                supported_actions = ["retry", "view_trace", "file_backlog"]
            else:
                supported_actions = ["retry", "view_trace", "file_backlog"]
            operations.append({
                "operation_id": f"edge-semantic:{job.get('edge_id') or job.get('event_id')}",
                "operation_type": "edge_semantic",
                "target_scope": "edge",
                "target_id": job.get("edge_id", ""),
                "target_label": job.get("edge_id", ""),
                "status": op_status,
                "progress": _operation_unit_progress(op_status),
                "created_at": job.get("created_at", ""),
                "updated_at": job.get("updated_at", ""),
                "claimed_by": "",
                "worker_id": "",
                "lease_expires_at": "",
                "last_error": "",
                "last_result": job.get("event_type", ""),
                "trace_id": job.get("event_id", ""),
                "supported_actions": supported_actions,
            })

        for job in graph_structure_jobs:
            event_type = str(job.get("event_type") or "")
            raw_status = str(job.get("status") or "")
            requested_event = event_type in {"graph_structure_requested", "graph_enrich_config_requested"}
            operation_type = (
                "graph_enrich_config"
                if event_type.startswith("graph_enrich_config_")
                else "graph_structure"
            )
            payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            evidence = job.get("evidence") if isinstance(job.get("evidence"), dict) else {}
            if requested_event and raw_status == "observed":
                op_status = "queued"
            elif requested_event and raw_status == "proposed":
                op_status = "review_required"
            elif (
                event_type in {"graph_structure_completed", "graph_enrich_config_completed"}
                and raw_status == "observed"
                and result.get("ok") is True
                and not bool(result.get("accepted"))
            ):
                op_status = "review_required"
            else:
                op_status = _normalize_operation_status(raw_status)
            errors = evidence.get("errors") or result.get("errors") or []
            if isinstance(errors, list):
                last_error = "; ".join(str(item) for item in errors if str(item))
            else:
                last_error = str(errors or "")
            supported_actions = ["view_trace", "file_backlog"]
            if op_status == "review_required":
                supported_actions.insert(0, "observer_takeover")
            if op_status in {"failed", "cancelled"}:
                supported_actions.insert(0, "retry")
            operations.append({
                "operation_id": f"{operation_type.replace('_', '-')}:{job.get('event_id')}",
                "operation_type": operation_type,
                "target_scope": job.get("target_type") or "snapshot",
                "target_id": job.get("target_id") or snapshot_id,
                "target_label": (
                    event_type
                    .replace("graph_structure_", "graph structure ")
                    .replace("graph_enrich_config_", "graph enrich config ")
                ),
                "status": op_status,
                "progress": _operation_unit_progress(op_status),
                "created_at": job.get("created_at", ""),
                "updated_at": job.get("updated_at", ""),
                "claimed_by": job.get("updated_by", ""),
                "worker_id": (
                    f"semantic_worker_inproc_{operation_type}"
                    if op_status == "running"
                    else ""
                ),
                "lease_expires_at": "",
                "last_error": last_error,
                "last_result": event_type,
                "trace_id": job.get("event_id", ""),
                "source_event_id": job.get("source_event_id", ""),
                "supported_actions": supported_actions,
            })

        semantic_health = (
            (snapshot_summary.get("health") or {}).get("semantic_health")
            if isinstance(snapshot_summary.get("health"), dict)
            else {}
        ) or {}
        node_stale = int(semantic_health.get("semantic_stale_count") or 0)
        edge_missing = int(semantic_health.get("edge_semantic_missing_count") or 0)
        edge_eligible = int(semantic_health.get("edge_semantic_eligible_count") or 0)
        edge_current = int(semantic_health.get("edge_semantic_current_count") or 0)
        if snapshot_id and node_stale > 0 and not node_jobs:
            operations.append({
                "operation_id": "node-semantic:not-queued",
                "operation_type": "node_semantic",
                "target_scope": "node",
                "target_id": "*",
                "target_label": "node semantics",
                "status": "not_queued",
                "progress": {"done": 0, "total": node_stale},
                "created_at": "",
                "updated_at": "",
                "claimed_by": "",
                "worker_id": "",
                "lease_expires_at": "",
                "last_error": "",
                "last_result": f"{node_stale} stale node semantics, 0 queued",
                "supported_actions": ["queue_node_semantics", "file_backlog", "view_trace"],
            })
        if snapshot_id and edge_missing > 0 and not edge_jobs:
            operations.append({
                "operation_id": "edge-semantic:not-queued",
                "operation_type": "edge_semantic",
                "target_scope": "edge",
                "target_id": "*",
                "target_label": "edge semantics",
                "status": "not_queued",
                "progress": {"done": edge_current, "total": edge_eligible},
                "created_at": "",
                "updated_at": "",
                "claimed_by": "",
                "worker_id": "",
                "lease_expires_at": "",
                "last_error": "",
                "last_result": f"{edge_missing} edge semantics missing, 0 queued",
                "supported_actions": ["queue_edge_semantics", "run_edge_semantics", "file_backlog"],
            })

        feedback_summary = feedback_queue.get("summary") if isinstance(feedback_queue.get("summary"), dict) else {}
        feedback_count = int(
            feedback_summary.get("visible_item_count")
            or feedback_queue.get("count")
            or feedback_queue.get("group_count")
            or 0
        )
        if feedback_count:
            operations.append({
                "operation_id": "feedback-review:queue",
                "operation_type": "feedback_review",
                "target_scope": "snapshot",
                "target_id": snapshot_id,
                "target_label": "feedback queue",
                "status": "queued",
                "progress": {"done": 0, "total": feedback_count},
                "created_at": "",
                "updated_at": "",
                "claimed_by": "",
                "worker_id": "",
                "lease_expires_at": "",
                "last_error": "",
                "last_result": "",
                "supported_actions": ["observer_takeover", "file_backlog", "view_trace"],
            })

        patch_total = int(patch_summary.get("proposed_count") or 0) + int(patch_summary.get("replayable_count") or 0)
        if patch_total:
            operations.append({
                "operation_id": "graph-patch:queue",
                "operation_type": "graph_patch_apply",
                "target_scope": "project",
                "target_id": project_id,
                "target_label": "graph correction patches",
                "status": "queued",
                "progress": {"done": 0, "total": patch_total},
                "created_at": "",
                "updated_at": "",
                "claimed_by": "",
                "worker_id": "",
                "lease_expires_at": "",
                "last_error": "",
                "last_result": "",
                "supported_actions": ["pause", "resume", "view_trace"],
            })

        current_state = _dashboard_current_state(
            conn,
            project_id,
            status=status,
            snapshot_id=snapshot_id,
            snapshot_summary=snapshot_summary,
        )
        current_state["graph_stale"] = graph_stale_summary
        reconcile_metrics = store.summarize_reconcile_run_metrics(
            conn,
            project_id,
            limit=_query_int(ctx.query, "reconcile_metric_limit", 100),
        )
        operations.sort(key=lambda item: (str(item.get("updated_at") or item.get("created_at") or ""), str(item.get("operation_id") or "")), reverse=True)
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "active_snapshot_id": status.get("active_snapshot_id", ""),
            "count": len(operations),
            "operations": operations,
            "summary": {
                "by_type": _count_by(operations, "operation_type"),
                "by_status": _count_by(operations, "status"),
                "pending_scope_reconcile_count": status.get("pending_scope_reconcile_count", 0),
                "node_semantic_jobs": {
                    "by_status": node_job_counts,
                    "progress": _semantic_job_progress(node_job_counts),
                },
                "edge_semantic_jobs": {
                    "by_status": edge_job_counts,
                    "progress": _semantic_job_progress(edge_job_counts),
                },
                "graph_structure_jobs": {
                    "by_status": _count_by(
                        [job for job in graph_structure_jobs if str(job.get("event_type") or "").startswith("graph_structure_")],
                        "status",
                    ),
                    "progress": _semantic_job_progress(_count_by(
                        [job for job in graph_structure_jobs if str(job.get("event_type") or "").startswith("graph_structure_")],
                        "status",
                    )),
                },
                "graph_enrich_config_jobs": {
                    "by_status": _count_by(
                        [job for job in graph_structure_jobs if str(job.get("event_type") or "").startswith("graph_enrich_config_")],
                        "status",
                    ),
                    "progress": _semantic_job_progress(_count_by(
                        [job for job in graph_structure_jobs if str(job.get("event_type") or "").startswith("graph_enrich_config_")],
                        "status",
                    )),
                },
                "semantic_denominators": {
                    "node_current": semantic_health.get("semantic_current_count", 0),
                    "node_unverified": semantic_health.get("semantic_unverified_hash_count", 0),
                    "node_missing": semantic_health.get("semantic_missing_count", 0),
                    "node_stale": semantic_health.get("semantic_stale_count", 0),
                    "edge_eligible": semantic_health.get("edge_semantic_eligible_count", 0),
                    "edge_current": semantic_health.get("edge_semantic_current_count", 0),
                    "edge_requested": semantic_health.get("edge_semantic_requested_count", 0),
                    "edge_rule": semantic_health.get("edge_semantic_rule_count", 0),
                    "edge_missing": semantic_health.get("edge_semantic_missing_count", 0),
                    "edge_needs_ai": semantic_health.get("edge_semantic_needs_ai_count", 0),
                    "edge_unqueued": semantic_health.get("edge_semantic_unqueued_count", 0),
                    "edge_payload_current": semantic_health.get("edge_semantic_payload_current_count", 0),
                },
                "feedback_queue": feedback_summary,
                "graph_correction_patches": patch_summary,
                "reconcile_metrics": reconcile_metrics,
                "graph_stale": graph_stale_summary,
                "semantic_snapshot": current_state["semantic_snapshot"],
                "semantic_drift": current_state["semantic_drift"],
                "current_state": current_state,
            },
        }
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/reconcile/metrics")
def handle_graph_governance_reconcile_metrics(ctx: RequestContext):
    """Return persisted scope/full reconcile timing metrics."""
    project_id = ctx.get_project_id()
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.reconcile.metrics")
        if _query_bool(ctx.query, "backfill", True):
            backfill = store.backfill_reconcile_run_metrics_from_snapshots(
                conn,
                project_id,
                limit=_query_int(ctx.query, "backfill_limit", 100),
            )
            conn.commit()
        else:
            backfill = {"project_id": project_id, "scanned": 0, "imported": 0}
        limit = _query_int(ctx.query, "limit", 50)
        strategy = str(ctx.query.get("strategy") or "")
        rows = store.list_reconcile_run_metrics(
            conn,
            project_id,
            limit=limit,
            strategy=strategy,
        )
        return {
            "ok": True,
            "project_id": project_id,
            "backfill": backfill,
            "summary": store.summarize_reconcile_run_metrics(conn, project_id, limit=max(limit, 100)),
            "metrics": rows,
            "count": len(rows),
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/reconcile/pending-scope/recover-stale")
def handle_graph_governance_pending_scope_recover_stale(ctx: RequestContext):
    """Fail stale running pending-scope rows so they can be force-requeued."""
    project_id = ctx.get_project_id()
    from . import graph_snapshot_store as store
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.reconcile.pending-scope.recover-stale")
        with sqlite_write_lock():
            result = store.recover_stale_pending_scope_reconcile(
                conn,
                project_id,
                max_running_seconds=_query_int(ctx.body, "max_running_seconds", 1800),
                actor=str(ctx.body.get("actor") or "observer"),
            )
            conn.commit()
        return 200, {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/reconcile/scope/cancel")
def handle_graph_governance_scope_reconcile_cancel(ctx: RequestContext):
    """Disabled (MF-2026-05-10-011): scope reconcile cancel has no business use.

    Previously waived the pending_scope_reconcile row, which permanently poisoned
    the same commit so the dashboard's "Queue scope reconcile" button silently
    no-op'd. Removed at operator request. Recovery path: make a new commit on
    main (HEAD changes → fresh pending row eligible) or call
    /reconcile/backfill-escape for stuck-row recovery.
    """
    return 410, {
        "ok": False,
        "error": "scope_reconcile_cancel_disabled",
        "message": (
            "scope_reconcile cancel was removed by MF-2026-05-10-011 — no business need. "
            "To recover from a stale pending row, push a new commit on main "
            "(fresh HEAD) or use POST /reconcile/backfill-escape."
        ),
    }


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _git_commit_subject(commit_sha: str) -> str:
    commit_sha = str(commit_sha or "").strip()
    if not commit_sha:
        return ""
    try:
        result = subprocess.run(
            ["git", "show", "-s", "--format=%s", commit_sha],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).resolve().parents[2],
        )
    except Exception:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


@route("GET", "/api/graph-governance/{project_id}/commits")
def handle_graph_governance_commit_timeline(ctx: RequestContext):
    """Return commit-anchored graph snapshot timeline for dashboard navigation."""
    project_id = ctx.get_project_id()
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        status = store.graph_governance_status(conn, project_id)
        commits = store.list_commit_timeline(
            conn,
            project_id,
            limit=_query_int(ctx.query, "limit", 50),
            include_backlog=_query_bool(ctx.query, "include_backlog", True),
        )
        if _query_bool(ctx.query, "include_git_subject", True):
            for row in commits:
                row["subject"] = row.get("subject") or _git_commit_subject(row.get("commit_sha", ""))
        return {
            "ok": True,
            "project_id": project_id,
            "active_commit_sha": status.get("graph_snapshot_commit", ""),
            "active_snapshot_id": status.get("active_snapshot_id", ""),
            "pending_scope_reconcile_count": status.get("pending_scope_reconcile_count", 0),
            "commits": commits,
            "count": len(commits),
        }
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/commits/{commit_sha}/graph-state")
def handle_graph_governance_commit_graph_state(ctx: RequestContext):
    """Resolve a commit to the graph snapshot dashboard should display."""
    project_id = ctx.get_project_id()
    commit_sha = ctx.path_params["commit_sha"]
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        try:
            result = store.resolve_commit_graph_state(conn, project_id, commit_sha)
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        result["subject"] = _git_commit_subject(commit_sha) if _query_bool(ctx.query, "include_git_subject", True) else ""
        return {"ok": True, **result}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots")
def handle_graph_governance_snapshot_list(ctx: RequestContext):
    """List graph snapshots for operator/dashboard review."""
    project_id = ctx.get_project_id()
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        snapshots = store.list_graph_snapshots(
            conn,
            project_id,
            statuses=_query_statuses(ctx.query),
            limit=_query_int(ctx.query, "limit", 50),
        )
        return {"ok": True, "project_id": project_id, "snapshots": snapshots, "count": len(snapshots)}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/correction-patches")
def handle_graph_governance_correction_patch_list(ctx: RequestContext):
    """List auditable graph correction patches for dashboard/observer review."""
    project_id = ctx.get_project_id()
    from . import graph_correction_patches as patches

    conn = get_connection(project_id)
    try:
        rows = patches.list_correction_patches(
            conn,
            project_id,
            statuses=_query_statuses(ctx.query),
            patch_type=str(ctx.query.get("patch_type") or ""),
            target_node_id=str(ctx.query.get("target_node_id") or ""),
            limit=_query_int(ctx.query, "limit", 100),
        )
        return {"ok": True, "project_id": project_id, "patches": rows, "count": len(rows)}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/correction-patches")
def handle_graph_governance_correction_patch_create(ctx: RequestContext):
    """Create a graph correction patch suggestion without mutating the graph."""
    project_id = ctx.get_project_id()
    body = ctx.body
    from . import graph_correction_patches as patches

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.correction-patch.create")
        try:
            result = patches.create_patch(
                conn,
                project_id,
                patch_id=body.get("patch_id"),
                patch_type=str(body.get("patch_type") or ""),
                patch_json=body.get("patch_json") if isinstance(body.get("patch_json"), dict) else {},
                evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else {},
                status=str(body.get("status") or patches.PATCH_STATUS_PROPOSED),
                risk_level=str(body.get("risk_level") or "medium"),
                base_snapshot_id=str(body.get("base_snapshot_id") or ""),
                base_commit=str(body.get("base_commit") or ""),
                target_node_id=str(body.get("target_node_id") or ""),
                stable_key=str(body.get("stable_node_key") or ""),
                created_by=str(body.get("actor") or "observer"),
            )
        except ValueError as exc:
            _raise_graph_api_validation(exc)
        conn.commit()
        return 201, {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/correction-patches/{patch_id}/accept")
def handle_graph_governance_correction_patch_accept(ctx: RequestContext):
    """Accept a graph correction patch so future reconcile runs replay it."""
    project_id = ctx.get_project_id()
    patch_id = ctx.path_params["patch_id"]
    body = ctx.body
    from . import graph_correction_patches as patches

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.correction-patch.accept")
        changed = patches.accept_patch(
            conn,
            project_id,
            patch_id,
            accepted_by=str(body.get("actor") or "observer"),
        )
        if not changed:
            _raise_graph_api_validation(ValueError(f"patch not found or not proposed: {patch_id}"))
        conn.commit()
        return {"ok": True, "project_id": project_id, "patch_id": patch_id, "status": "accepted"}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/correction-patches/{patch_id}/reject")
def handle_graph_governance_correction_patch_reject(ctx: RequestContext):
    """Reject a proposed/accepted graph correction patch."""
    project_id = ctx.get_project_id()
    patch_id = ctx.path_params["patch_id"]
    body = ctx.body
    from . import graph_correction_patches as patches

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.correction-patch.reject")
        changed = patches.reject_patch(
            conn,
            project_id,
            patch_id,
            rejected_by=str(body.get("actor") or "observer"),
            reason=str(body.get("reason") or ""),
        )
        if not changed:
            _raise_graph_api_validation(ValueError(f"patch not found or already terminal: {patch_id}"))
        conn.commit()
        return {"ok": True, "project_id": project_id, "patch_id": patch_id, "status": "rejected"}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/summary")
def handle_graph_governance_snapshot_summary(ctx: RequestContext):
    """Return compact dashboard summary for one graph snapshot."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        try:
            summary = store.summarize_graph_snapshot(conn, project_id, snapshot_id)
        except KeyError as exc:
            _raise_graph_api_validation(exc)
        return {"ok": True, **summary}
    finally:
        conn.close()


def _empty_graph_structure_hint_projection() -> dict:
    return {
        "status": "ok",
        "hint_count": 0,
        "materialized_count": 0,
        "conflict_count": 0,
        "hint_states": {},
        "suppressed_edges": [],
        "has_projection_notes": False,
    }


def _graph_structure_hint_projection_from_snapshot(snapshot: dict) -> dict:
    raw_notes = snapshot.get("notes") if isinstance(snapshot, dict) else ""
    notes = {}
    if isinstance(raw_notes, str) and raw_notes.strip():
        try:
            decoded = json.loads(raw_notes)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = {}
        if isinstance(decoded, dict):
            notes = decoded
    elif isinstance(raw_notes, dict):
        notes = raw_notes
    report = notes.get("graph_structure_hint_projection") if isinstance(notes, dict) else {}
    if not isinstance(report, dict):
        report = {}
    out = _empty_graph_structure_hint_projection()
    out.update({
        "status": str(report.get("status") or out["status"]),
        "hint_count": int(report.get("hint_count") or 0),
        "materialized_count": int(report.get("materialized_count") or 0),
        "conflict_count": int(report.get("conflict_count") or 0),
        "hint_states": report.get("hint_states") if isinstance(report.get("hint_states"), dict) else {},
        "suppressed_edges": report.get("suppressed_edges") if isinstance(report.get("suppressed_edges"), list) else [],
        "has_projection_notes": "graph_structure_hint_projection" in notes,
    })
    return out


def _snapshot_graph_and_inventory(store, project_id: str, snapshot_id: str) -> tuple[dict, list[dict]]:
    graph_path = store.snapshot_companion_dir(project_id, snapshot_id) / "graph.json"
    inventory_path = store.snapshot_companion_dir(project_id, snapshot_id) / "file_inventory.json"
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        graph = {}
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        inventory = []
    if not isinstance(graph, dict):
        graph = {}
    if not isinstance(inventory, list):
        inventory = []
    return graph, [row for row in inventory if isinstance(row, dict)]


def _graph_structure_ops_payload_from_body(body: dict) -> dict:
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else body
    return payload if isinstance(payload, dict) else {}


def _graph_structure_ops_contract_for_project(project_id: str, body: dict) -> dict:
    from .reconcile_semantic_config import (
        apply_project_ai_routing,
        load_semantic_enrichment_config,
    )

    try:
        root = _graph_governance_project_root(project_id, body)
    except ValidationError:
        root = None
    cfg = apply_project_ai_routing(
        load_semantic_enrichment_config(project_root=root),
        project_id=project_id,
    )
    return asdict(cfg.graph_structure_ops)


def _ctx_with_graph_structure_ops_payload(ctx: RequestContext, payload: dict) -> RequestContext:
    next_ctx = RequestContext(
        ctx.handler,
        ctx.method,
        dict(ctx.path_params),
        dict(ctx.query),
        {**ctx.body, "payload": payload},
        ctx.request_id,
        ctx.token,
        ctx.idem_key,
    )
    next_ctx._session = ctx._session
    next_ctx._conn = ctx._conn
    return next_ctx


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/graph-structure-hints")
def handle_graph_governance_snapshot_graph_structure_hints(ctx: RequestContext):
    """Return source-hint graph projection status recorded on a graph snapshot."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    from . import graph_snapshot_store as store
    from .errors import ValidationError

    conn = get_connection(project_id)
    try:
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
        if not snapshot:
            raise ValidationError(f"graph snapshot not found: {snapshot_id}")
        projection = _graph_structure_hint_projection_from_snapshot(snapshot)
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "commit_sha": snapshot.get("commit_sha", ""),
            "snapshot_status": snapshot.get("status", ""),
            "snapshot_kind": snapshot.get("snapshot_kind", ""),
            "created_at": snapshot.get("created_at", ""),
            "projection": projection,
            "status": projection["status"],
            "hint_count": projection["hint_count"],
            "materialized_count": projection["materialized_count"],
            "conflict_count": projection["conflict_count"],
            "hint_states": projection["hint_states"],
            "suppressed_edges": projection["suppressed_edges"],
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/graph-structure-ops/dry-run")
def handle_graph_governance_snapshot_graph_structure_ops_dry_run(ctx: RequestContext):
    """Validate AI graph-structure ops and preview hint projection effects."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import graph_snapshot_store as store
    from .errors import ValidationError
    from .graph_structure_ops import dry_run_graph_structure_ops
    from . import reconcile_feedback

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.graph-structure-ops.dry-run")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
        if not snapshot:
            raise ValidationError(f"graph snapshot not found: {snapshot_id}")
        graph, inventory = _snapshot_graph_and_inventory(store, project_id, snapshot_id)
        payload = _graph_structure_ops_payload_from_body(body)
        result = dry_run_graph_structure_ops(
            payload,
            graph=graph,
            inventory_paths=[str(row.get("path") or "") for row in inventory],
            snapshot_id=snapshot_id,
            base_commit=str(snapshot.get("commit_sha") or ""),
            operation_contract=_graph_structure_ops_contract_for_project(project_id, body),
            project_root=body.get("project_root") or "",
        )
        status_code = 200 if result["ok"] else 422
        return status_code, {
            "ok": result["ok"],
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "commit_sha": snapshot.get("commit_sha", ""),
            "dry_run": True,
            "mutated": False,
            **result,
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/graph-structure-ops/accept")
def handle_graph_governance_snapshot_graph_structure_ops_accept(ctx: RequestContext):
    """Accept validated graph-structure ops by writing source hint blocks."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import graph_snapshot_store as store
    from .errors import ValidationError
    from .graph_structure_hints import write_graph_structure_hints
    from .graph_structure_ops import dry_run_graph_structure_ops

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.graph-structure-ops.accept")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
        if not snapshot:
            raise ValidationError(f"graph snapshot not found: {snapshot_id}")
        root = _graph_governance_project_root(project_id, body)
        graph, inventory = _snapshot_graph_and_inventory(store, project_id, snapshot_id)
        payload = _graph_structure_ops_payload_from_body(body)
        dry_run = dry_run_graph_structure_ops(
            payload,
            graph=graph,
            inventory_paths=[str(row.get("path") or "") for row in inventory],
            snapshot_id=snapshot_id,
            base_commit=str(snapshot.get("commit_sha") or ""),
            operation_contract=_graph_structure_ops_contract_for_project(project_id, body),
            project_root=root,
        )
        if not dry_run["ok"]:
            return 422, {
                "ok": False,
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "commit_sha": snapshot.get("commit_sha", ""),
                "dry_run": True,
                "mutated": False,
                "accepted": False,
                **dry_run,
            }
        write_result = write_graph_structure_hints(
            root,
            dry_run["gate"]["normalized_hint_index"]["hints"],
        )
        review_feedback = {}
        if write_result["written_count"] > 0:
            paths = [
                str(item.get("path") or "")
                for item in write_result.get("written") or []
                if isinstance(item, dict) and item.get("path")
            ]
            review_feedback = reconcile_feedback.submit_feedback_item(
                project_id,
                snapshot_id,
                feedback_kind=reconcile_feedback.KIND_GRAPH_CORRECTION,
                issue={
                    "type": "ai_graph_structure_governance_hint",
                    "reason": str(body.get("reason") or "AI graph-structure proposal wrote source hints."),
                    "summary": "AI graph-structure proposal is waiting for commit/apply and Update Graph.",
                    "target": snapshot_id,
                    "target_type": "snapshot",
                    "paths": paths,
                    "changed_files": paths,
                    "intent": str(body.get("intent") or body.get("operator_intent") or "apply_ai_graph_structure_proposal"),
                    "priority": "P1",
                },
                actor=str(body.get("actor") or "dashboard_user"),
                source_round="graph_structure_lifecycle",
            )
            conn.commit()
        status_code = 200 if write_result["ok"] else 422
        return status_code, {
            "ok": write_result["ok"],
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "commit_sha": snapshot.get("commit_sha", ""),
            "project_root": str(root),
            "dry_run": False,
            "mutated": write_result["written_count"] > 0,
            "accepted": write_result["ok"],
            "requires_commit": write_result["written_count"] > 0,
            "update_graph_after_commit": write_result["written_count"] > 0,
            "gate": dry_run["gate"],
            "projection": dry_run["projection"],
            "write": write_result,
            "review_queue": {
                "queued": bool(review_feedback),
                "feedback": (review_feedback.get("items") or [{}])[0] if review_feedback else {},
                "operation_type": "graph_structure",
                "subtype": "ai_graph_structure",
            },
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/graph-structure-ops/ai-output")
def handle_graph_governance_snapshot_graph_structure_ops_ai_output(ctx: RequestContext):
    """Parse AI graph-structure JSON output and run dry-run or accept."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    mode = str(body.get("mode") or "dry_run").strip().lower().replace("-", "_")
    raw_output = body.get("ai_output") if "ai_output" in body else body.get("output")
    from . import graph_snapshot_store as store
    from .errors import ValidationError
    from .graph_structure_ops import run_graph_structure_ai_output_pipeline
    from . import reconcile_feedback

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.graph-structure-ops.ai-output")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
        if not snapshot:
            raise ValidationError(f"graph snapshot not found: {snapshot_id}")
        graph, inventory = _snapshot_graph_and_inventory(store, project_id, snapshot_id)
        root = ""
        if mode in {"accept", "apply", "write"}:
            root = str(_graph_governance_project_root(project_id, body))
        result = run_graph_structure_ai_output_pipeline(
            raw_output=raw_output,
            mode=mode,
            graph=graph,
            inventory_paths=[str(row.get("path") or "") for row in inventory],
            snapshot_id=snapshot_id,
            base_commit=str(snapshot.get("commit_sha") or ""),
            project_root=root,
            operation_contract=_graph_structure_ops_contract_for_project(project_id, body),
        )
        review_feedback = {}
        write_result = result.get("write") if isinstance(result.get("write"), dict) else {}
        if mode in {"accept", "apply", "write"} and int(write_result.get("written_count") or 0) > 0:
            paths = [
                str(item.get("path") or "")
                for item in write_result.get("written") or []
                if isinstance(item, dict) and item.get("path")
            ]
            review_feedback = reconcile_feedback.submit_feedback_item(
                project_id,
                snapshot_id,
                feedback_kind=reconcile_feedback.KIND_GRAPH_CORRECTION,
                issue={
                    "type": "ai_graph_structure_governance_hint",
                    "reason": str(body.get("reason") or "AI graph-structure output wrote source hints."),
                    "summary": "AI graph-structure output is waiting for commit/apply and Update Graph.",
                    "target": snapshot_id,
                    "target_type": "snapshot",
                    "paths": paths,
                    "changed_files": paths,
                    "intent": str(body.get("intent") or body.get("operator_intent") or "apply_ai_graph_structure_output"),
                    "priority": "P1",
                },
                actor=str(body.get("actor") or "dashboard_user"),
                source_round="graph_structure_lifecycle",
            )
            conn.commit()
        status_code = 200 if result["ok"] else 422
        return status_code, {
            "ok": result["ok"],
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "commit_sha": snapshot.get("commit_sha", ""),
            "dry_run": mode in {"dry_run", "dryrun", "preview"},
            "review_queue": {
                "queued": bool(review_feedback),
                "feedback": (review_feedback.get("items") or [{}])[0] if review_feedback else {},
                "operation_type": "graph_structure",
                "subtype": "ai_graph_structure",
            },
            **result,
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/graph-enrich-config-ops/ai-output")
def handle_graph_governance_graph_enrich_config_ops_ai_output(ctx: RequestContext):
    """Parse AI graph/enrich config JSON output and run dry-run or accept."""
    project_id = ctx.get_project_id()
    body = ctx.body
    mode = str(body.get("mode") or "dry_run").strip().lower().replace("-", "_")
    raw_output = body.get("ai_output") if "ai_output" in body else body.get("output")
    from .graph_enrich_config_ops import run_graph_enrich_config_ai_output_pipeline
    from . import reconcile_feedback

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.graph-enrich-config-ops.ai-output")
        root = _graph_governance_project_root(project_id, body)
        result = run_graph_enrich_config_ai_output_pipeline(
            raw_output=raw_output,
            mode=mode,
            project_root=root,
        )
        review_feedback = {}
        write_result = result.get("write") if isinstance(result.get("write"), dict) else {}
        if mode in {"accept", "apply", "write"} and int(write_result.get("written_count") or 0) > 0:
            config_path = str(write_result.get("config_path") or "")
            rel_path = config_path
            try:
                rel_path = str(Path(config_path).resolve().relative_to(root.resolve()))
            except Exception:
                pass
            review_feedback = reconcile_feedback.submit_feedback_item(
                project_id,
                "project",
                feedback_kind=reconcile_feedback.KIND_GRAPH_CORRECTION,
                issue={
                    "type": "graph_enrich_config_patch",
                    "reason": str(body.get("reason") or "AI graph enrich config proposal wrote project config."),
                    "summary": "Graph enrich config proposal is waiting for commit/apply and Update Graph.",
                    "target": "graph_enrich_config",
                    "target_type": "config",
                    "paths": [rel_path],
                    "intent": str(body.get("intent") or body.get("operator_intent") or "apply_ai_graph_enrich_config"),
                    "priority": "P1",
                },
                actor=str(body.get("actor") or "dashboard_user"),
                source_round="graph_structure_lifecycle",
            )
            conn.commit()
        status_code = 200 if result["ok"] else 422
        return status_code, {
            "ok": result["ok"],
            "project_id": project_id,
            "project_root": str(root),
            "dry_run": mode in {"dry_run", "dryrun", "preview"},
            "review_queue": {
                "queued": bool(review_feedback),
                "feedback": (review_feedback.get("items") or [{}])[0] if review_feedback else {},
                "operation_type": "graph_structure",
                "subtype": "ai_graph_structure",
            },
            **result,
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/graph-enrich-config/observer-override")
def handle_graph_governance_snapshot_graph_enrich_config_observer_override(ctx: RequestContext):
    """Apply an observer-authored graph/enrich config override with graph_events audit."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    mode = str(body.get("mode") or "dry_run").strip().lower().replace("-", "_")
    raw_output = body.get("ai_output") if "ai_output" in body else body.get("output")
    cluster = body.get("cluster") if isinstance(body.get("cluster"), dict) else None
    from . import graph_snapshot_store as store
    from .db import sqlite_write_lock
    from .graph_proposal_review import (
        apply_graph_enrich_config_observer_override,
        synthesize_graph_enrich_config_payload_from_cluster,
    )

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(
            ctx,
            conn,
            "graph-governance.snapshot.graph-enrich-config.observer-override",
        )
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
        if not snapshot:
            raise ValidationError(f"graph snapshot not found: {snapshot_id}")
        root = _graph_governance_project_root(project_id, body)
        synthesis: dict[str, Any] | None = None
        if _query_bool(body, "synthesize", False) and raw_output in (None, ""):
            if not cluster:
                raise ValidationError("cluster is required when synthesize=true")
            synthesis = synthesize_graph_enrich_config_payload_from_cluster(
                cluster,
                actor=str(body.get("actor") or "dashboard_user"),
                rationale=str(body.get("rationale") or body.get("reason") or ""),
            )
            if not synthesis.get("ok"):
                return 422, {
                    "ok": False,
                    "project_id": project_id,
                    "snapshot_id": snapshot_id,
                    "commit_sha": snapshot.get("commit_sha", ""),
                    "project_root": str(root),
                    "synthesis": synthesis,
                    "errors": synthesis.get("errors", []),
                }
            raw_output = synthesis.get("payload")
        with sqlite_write_lock():
            result = apply_graph_enrich_config_observer_override(
                conn,
                project_id,
                snapshot_id,
                cluster=cluster,
                cluster_id=str(body.get("cluster_id") or ""),
                rejected_event_ids=(
                    _body_string_list(body, "rejected_event_ids")
                    or _body_string_list(body, "rejected_graph_event_ids")
                    or []
                ),
                raw_output=raw_output,
                mode=mode,
                project_root=root,
                actor=str(body.get("actor") or "dashboard_user"),
                rationale=str(body.get("rationale") or body.get("reason") or ""),
            )
            conn.commit()
        status_code = 200 if result["ok"] else 422
        return status_code, {
            "ok": result["ok"],
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "commit_sha": snapshot.get("commit_sha", ""),
            "project_root": str(root),
            "synthesis": synthesis,
            **result,
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/graph-structure-ops/jobs")
def handle_graph_governance_snapshot_graph_structure_ops_jobs_create(ctx: RequestContext):
    """Queue a graph-structure AI-output task as an auditable event."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    mode = str(body.get("mode") or "dry_run").strip().lower().replace("-", "_")
    raw_output = body.get("ai_output") if "ai_output" in body else body.get("output")
    from . import event_bus
    from . import graph_events
    from . import graph_snapshot_store as store
    from .db import sqlite_write_lock
    from .errors import ValidationError

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.graph-structure-ops.jobs.create")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
        if not snapshot:
            raise ValidationError(f"graph snapshot not found: {snapshot_id}")
        payload = {"mode": mode}
        if raw_output not in (None, ""):
            payload["ai_output"] = raw_output
        for key in ("selector", "operator_request", "instructions", "options"):
            if isinstance(body.get(key), dict):
                payload[key] = body[key]
        if mode in {"accept", "apply", "write"}:
            payload["project_root"] = str(_graph_governance_project_root(project_id, body))
        elif body.get("project_root"):
            payload["project_root"] = str(body.get("project_root") or "")
        with sqlite_write_lock():
            event = graph_events.create_event(
                conn,
                project_id,
                snapshot_id,
                event_type="graph_structure_requested",
                event_kind="semantic_job",
                target_type="snapshot",
                target_id=snapshot_id,
                status=graph_events.EVENT_STATUS_OBSERVED,
                operation_type="graph_structure",
                payload=payload,
                evidence={
                    "source": "graph_structure_ops_jobs_api",
                    "mode": mode,
                },
                created_by=str(body.get("actor") or "dashboard_user"),
            )
            conn.commit()
        try:
            event_bus.publish("semantic_job.enqueued", {
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "target_scope": "graph_structure",
                "event_id": event.get("event_id", ""),
            })
        except Exception:
            pass
        return 202, {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "commit_sha": snapshot.get("commit_sha", ""),
            "queued": True,
            "operation_type": "graph_structure",
            "event": event,
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic/graph-structure-candidates")
def handle_graph_governance_snapshot_semantic_graph_structure_candidates(ctx: RequestContext):
    """Queue graph-structure gate jobs derived from semantic node proposals."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import event_bus
    from . import graph_snapshot_store as store
    from . import semantic_graph_structure_bridge
    from .db import sqlite_write_lock
    from .errors import ValidationError

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.semantic.graph-structure-candidates")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
        if not snapshot:
            raise ValidationError(f"graph snapshot not found: {snapshot_id}")
        event_ids = (
            _body_string_list(body, "event_ids")
            or _body_string_list(body, "semantic_event_ids")
            or []
        )
        node_ids = (
            _body_string_list(body, "node_ids")
            or _body_string_list(body, "semantic_node_ids")
            or []
        )
        with sqlite_write_lock():
            result = semantic_graph_structure_bridge.bridge_semantic_events_to_graph_structure_jobs(
                conn,
                project_id,
                snapshot_id,
                event_ids=event_ids,
                node_ids=node_ids,
                mode=str(body.get("mode") or "dry_run"),
                actor=str(body.get("actor") or "dashboard_user"),
                limit=_query_int(body, "limit", 100),
            )
            conn.commit()
        published = 0
        for event in result.get("events", []):
            if event.get("event_type") != "graph_structure_requested":
                continue
            try:
                event_bus.publish("semantic_job.enqueued", {
                    "project_id": project_id,
                    "snapshot_id": snapshot_id,
                    "target_scope": "graph_structure",
                    "event_id": event.get("event_id", ""),
                })
                published += 1
            except Exception:
                pass
        return 202, {
            **result,
            "commit_sha": snapshot.get("commit_sha", ""),
            "published_count": published,
            "queued": int(result.get("queued_count") or 0) > 0,
            "operation_type": "graph_structure",
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic/graph-enrich-config-candidates")
def handle_graph_governance_snapshot_semantic_graph_enrich_config_candidates(ctx: RequestContext):
    """Queue graph-enrich-config gate jobs derived from semantic node proposals."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import event_bus
    from . import graph_snapshot_store as store
    from . import semantic_graph_structure_bridge
    from .db import sqlite_write_lock
    from .errors import ValidationError

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.semantic.graph-enrich-config-candidates")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id)
        if not snapshot:
            raise ValidationError(f"graph snapshot not found: {snapshot_id}")
        event_ids = (
            _body_string_list(body, "event_ids")
            or _body_string_list(body, "semantic_event_ids")
            or []
        )
        node_ids = (
            _body_string_list(body, "node_ids")
            or _body_string_list(body, "semantic_node_ids")
            or []
        )
        project_root = ""
        if body.get("project_root"):
            project_root = str(_graph_governance_project_root(project_id, body))
        with sqlite_write_lock():
            result = semantic_graph_structure_bridge.bridge_semantic_events_to_graph_enrich_config_jobs(
                conn,
                project_id,
                snapshot_id,
                event_ids=event_ids,
                node_ids=node_ids,
                mode=str(body.get("mode") or "dry_run"),
                actor=str(body.get("actor") or "dashboard_user"),
                limit=_query_int(body, "limit", 100),
                project_root=project_root,
            )
            conn.commit()
        published = 0
        for event in result.get("events", []):
            if event.get("event_type") != "graph_enrich_config_requested":
                continue
            try:
                event_bus.publish("semantic_job.enqueued", {
                    "project_id": project_id,
                    "snapshot_id": snapshot_id,
                    "target_scope": "graph_enrich_config",
                    "event_id": event.get("event_id", ""),
                })
                published += 1
            except Exception:
                pass
        return 202, {
            **result,
            "commit_sha": snapshot.get("commit_sha", ""),
            "published_count": published,
            "queued": int(result.get("queued_count") or 0) > 0,
            "operation_type": "graph_enrich_config",
        }
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/nodes")
def handle_graph_governance_snapshot_nodes(ctx: RequestContext):
    """List indexed graph nodes for one snapshot."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        nodes = store.list_graph_snapshot_nodes(
            conn,
            project_id,
            snapshot_id,
            limit=_query_int(ctx.query, "limit", 200),
            offset=_query_int(ctx.query, "offset", 0),
            layer=str(ctx.query.get("layer") or ""),
            kind=str(ctx.query.get("kind") or ""),
            include_semantic=_query_bool(ctx.query, "include_semantic", True),
        )
        return {"ok": True, "project_id": project_id, "snapshot_id": snapshot_id, "nodes": nodes, "count": len(nodes)}
    finally:
        conn.close()


def _timeline_json(raw, default):
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return default
        return value if value is not None else default
    return default


def _timeline_node_from_row(row) -> dict:
    if not row:
        return {}
    return {
        "node_id": row["node_id"],
        "layer": row["layer"],
        "title": row["title"],
        "kind": row["kind"],
        "primary_files": _timeline_json(row["primary_files_json"], []),
        "secondary_files": _timeline_json(row["secondary_files_json"], []),
        "test_files": _timeline_json(row["test_files_json"], []),
        "metadata": _timeline_json(row["metadata_json"], {}),
    }


def _timeline_at(item: dict, *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value:
            return str(value)
    return ""


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/nodes/{node_id}/timeline")
def handle_graph_governance_snapshot_node_timeline(ctx: RequestContext):
    """Return a node-centered audit timeline for dashboard detail panels."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    node_id = ctx.path_params["node_id"]
    from . import graph_events
    from . import graph_snapshot_store as store
    from . import reconcile_feedback

    conn = get_connection(project_id)
    try:
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id) or {}
        row = conn.execute(
            """
            SELECT node_id, layer, title, kind, primary_files_json,
                   secondary_files_json, test_files_json, metadata_json
            FROM graph_nodes_index
            WHERE project_id = ? AND snapshot_id = ? AND node_id = ?
            """,
            (project_id, snapshot_id, node_id),
        ).fetchone()
        if not row:
            from .errors import ValidationError
            raise ValidationError(f"graph node not found: {node_id}")

        event_limit = _query_int(ctx.query, "event_limit", 100)
        feedback_limit = _query_int(ctx.query, "feedback_limit", 50)
        node = _timeline_node_from_row(row)
        events = graph_events.list_events(
            conn,
            project_id,
            snapshot_id,
            target_type="node",
            target_id=node_id,
            limit=event_limit,
        )
        feedback = reconcile_feedback.list_feedback_items(
            project_id,
            snapshot_id,
            node_id=node_id,
            limit=feedback_limit,
        )
        job = _semantic_job_row(conn, project_id, snapshot_id, node_id) or {}
        projection = graph_events.get_semantic_projection(conn, project_id, snapshot_id) or {}
        projection_payload = projection.get("projection") if isinstance(projection.get("projection"), dict) else {}
        node_semantics = (
            projection_payload.get("node_semantics")
            if isinstance(projection_payload.get("node_semantics"), dict)
            else {}
        )
        semantic = node_semantics.get(node_id, {}) if isinstance(node_semantics, dict) else {}

        timeline: list[dict] = [{
            "source": "snapshot_node",
            "kind": "structure",
            "at": str(snapshot.get("created_at") or ""),
            "summary": "Node exists in this graph snapshot.",
            "node": node,
        }]
        if semantic:
            validity = semantic.get("validity") if isinstance(semantic.get("validity"), dict) else {}
            source_event = semantic.get("source_event") if isinstance(semantic.get("source_event"), dict) else {}
            timeline.append({
                "source": "semantic_projection",
                "kind": "semantic",
                "at": str(source_event.get("updated_at") or projection.get("updated_at") or projection.get("created_at") or ""),
                "summary": str(validity.get("status") or "semantic_projected"),
                "projection_id": projection.get("projection_id", ""),
                "semantic": semantic,
            })
        if job:
            timeline.append({
                "source": "semantic_job",
                "kind": "semantic_job",
                "at": _timeline_at(job, "updated_at", "created_at"),
                "summary": str(job.get("status") or ""),
                "job": job,
            })
        for event in events:
            timeline.append({
                "source": "graph_event",
                "kind": str(event.get("event_type") or ""),
                "at": _timeline_at(event, "updated_at", "created_at"),
                "summary": str(event.get("event_type") or event.get("event_kind") or ""),
                "event": event,
            })
        for item in feedback:
            timeline.append({
                "source": "feedback",
                "kind": str(item.get("feedback_kind") or item.get("final_feedback_kind") or ""),
                "at": _timeline_at(item, "updated_at", "reviewed_at", "accepted_at", "created_at"),
                "summary": str(item.get("summary") or item.get("reason") or item.get("status") or ""),
                "feedback": item,
            })

        timeline.sort(key=lambda item: str(item.get("at") or ""), reverse=True)
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "node_id": node_id,
            "node": node,
            "semantic": semantic,
            "semantic_job": job,
            "events": events,
            "feedback": feedback,
            "timeline": timeline,
            "summary": {
                "event_count": len(events),
                "feedback_count": len(feedback),
                "has_semantic_projection": bool(semantic),
                "semantic_job_status": str(job.get("status") or ""),
            },
        }
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/edges")
def handle_graph_governance_snapshot_edges(ctx: RequestContext):
    """List indexed graph edges for one snapshot."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        edges = store.list_graph_snapshot_edges(
            conn,
            project_id,
            snapshot_id,
            limit=_query_int(ctx.query, "limit", 500),
            offset=_query_int(ctx.query, "offset", 0),
            edge_type=str(ctx.query.get("edge_type") or ""),
        )
        return {"ok": True, "project_id": project_id, "snapshot_id": snapshot_id, "edges": edges, "count": len(edges)}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/files")
def handle_graph_governance_snapshot_files(ctx: RequestContext):
    """List snapshot file inventory rows for dashboard orphan/doc/test review."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        try:
            result = store.list_graph_snapshot_files(
                conn,
                project_id,
                snapshot_id,
                limit=_query_int(ctx.query, "limit", 200),
                offset=_query_int(ctx.query, "offset", 0),
                file_kind=str(ctx.query.get("file_kind") or ""),
                scan_status=str(ctx.query.get("scan_status") or ""),
                graph_status=str(ctx.query.get("graph_status") or ""),
                decision=str(ctx.query.get("decision") or ""),
                path_contains=str(ctx.query.get("path") or ""),
                sort=str(ctx.query.get("sort") or ""),
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "summary": result["summary"],
            "total_count": result["total_count"],
            "filtered_count": result["filtered_count"],
            "sort": result.get("sort", ""),
            "files": result["files"],
        }
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/asset-inbox")
def handle_graph_governance_snapshot_asset_inbox(ctx: RequestContext):
    """Return the read-only Asset Inbox view for one snapshot."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    from .asset_inbox_contract import build_asset_inbox_response

    conn = get_connection(project_id)
    try:
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        try:
            return build_asset_inbox_response(conn, project_id, snapshot_id)
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
    finally:
        conn.close()


_FILE_HYGIENE_ACTIONS = {
    "attach_to_node",
    "remove_binding",
    "create_node",
    "delete_candidate",
    "waive",
    "file_backlog",
}


def _file_hygiene_event_type(action: str, file_kind: str, node_id: str = "") -> str:
    if action == "attach_to_node" and node_id:
        if file_kind in {"doc", "index_doc"}:
            return "doc_binding_added"
        if file_kind == "test":
            return "test_binding_added"
        if file_kind == "config":
            return "config_binding_added"
    return {
        "attach_to_node": "file_attach_requested",
        "remove_binding": "asset_binding_remove_requested",
        "create_node": "file_node_create_requested",
        "delete_candidate": "file_delete_candidate",
        "waive": "file_waived",
        "file_backlog": "backlog_candidate_requested",
    }[action]


def _find_file_inventory_row(files_result: dict, path: str) -> dict:
    for row in files_result.get("files") or []:
        if str(row.get("path") or "") == path:
            return dict(row)
    return {}


def _prepare_file_hygiene_action(conn, store, project_id: str, snapshot_id: str, body: dict) -> dict:
    from .errors import ValidationError

    action = str(body.get("action") or "").strip().lower()
    path = str(body.get("path") or body.get("file_path") or "").strip().replace("\\", "/")
    node_id = str(body.get("node_id") or body.get("target_node_id") or "").strip()
    if action not in _FILE_HYGIENE_ACTIONS:
        raise ValidationError(f"unsupported file hygiene action: {action}")
    if not path:
        raise ValidationError("path is required")
    if action == "remove_binding" and not node_id:
        raise ValidationError("remove_binding requires target_node_id")
    if action == "delete_candidate" and not bool(
        body.get("confirm_delete_candidate") or body.get("operator_signoff")
    ):
        raise ValidationError("delete_candidate requires confirm_delete_candidate=true")

    files_result = store.list_graph_snapshot_files(
        conn,
        project_id,
        snapshot_id,
        limit=1000,
        path_contains=path,
    )
    row = _find_file_inventory_row(files_result, path)
    if not row:
        raise ValidationError(
            f"file inventory row not found: {path}. "
            "Run Update graph/reconcile first so the file appears in the "
            "snapshot file inventory before filing a hygiene action."
        )

    file_kind = str(row.get("file_kind") or "unknown")
    event_type = _file_hygiene_event_type(action, file_kind, node_id=node_id)
    target_type = "node" if event_type.endswith("_binding_added") else "file"
    target_id = node_id if target_type == "node" else path
    risk = "high" if action in {"delete_candidate", "remove_binding"} else "medium" if action == "create_node" else "low"
    payload = {
        "action": action,
        "path": path,
        "files": [path],
        "target_files": [path],
        "file_inventory": row,
        "target_node_id": node_id,
        "title": str(body.get("title") or f"File hygiene action: {action} {path}"),
        "user_text": str(body.get("user_text") or body.get("reason") or ""),
        "destructive_mutation_performed": False,
    }
    payload.update(body.get("payload") if isinstance(body.get("payload"), dict) else {})
    precondition = {
        "snapshot_id": snapshot_id,
        "expected_file_path": path,
        "expected_file_kind": file_kind,
        "expected_scan_status": str(row.get("scan_status") or ""),
        "expected_graph_status": str(row.get("graph_status") or ""),
    }
    if target_type == "node":
        precondition["node_exists"] = True
    return {
        "action": action,
        "path": path,
        "file": row,
        "event_type": event_type,
        "target_type": target_type,
        "target_id": target_id,
        "risk_level": risk,
        "confidence": float(body.get("confidence") or 0.8),
        "payload": payload,
        "precondition": precondition,
        "actor": str(body.get("actor") or "dashboard-user"),
    }


def _create_file_hygiene_event(
    graph_events,
    conn,
    project_id: str,
    snapshot_id: str,
    prepared: dict,
    *,
    source: str,
) -> dict:
    try:
        return graph_events.create_event(
            conn,
            project_id,
            snapshot_id,
            event_type=prepared["event_type"],
            event_kind="proposed_event",
            target_type=prepared["target_type"],
            target_id=prepared["target_id"],
            status=graph_events.EVENT_STATUS_PROPOSED,
            risk_level=prepared["risk_level"],
            confidence=prepared["confidence"],
            payload=prepared["payload"],
            precondition=prepared["precondition"],
            evidence={
                "source": source,
                "action": prepared["action"],
                "actor": prepared["actor"],
            },
            created_by=prepared["actor"],
        )
    except (KeyError, ValueError) as exc:
        _raise_graph_api_validation(exc)


def _file_hygiene_graph_structure_issue(prepared: dict, *, source: str) -> dict[str, Any]:
    action = str(prepared.get("action") or "").strip().lower()
    path = str(prepared.get("path") or "").strip()
    row = prepared.get("file") if isinstance(prepared.get("file"), dict) else {}
    file_kind = str(row.get("file_kind") or "asset").strip().lower()
    kind_prefix = file_kind if file_kind in {"doc", "test", "config"} else "asset"
    issue_type = {
        "attach_to_node": f"{kind_prefix}_binding_addition",
        "remove_binding": f"{kind_prefix}_binding_removal",
        "delete_candidate": "asset_delete_candidate",
        "create_node": "graph_structure_create_node",
    }.get(action, "asset_binding_review")
    user_text = str((prepared.get("payload") or {}).get("user_text") or "").strip()
    default_reason = {
        "attach_to_node": "Operator requested asset binding review.",
        "remove_binding": "Operator requested asset binding removal review.",
        "delete_candidate": "Operator requested asset delete-candidate review.",
        "create_node": "Operator requested graph node creation review.",
    }.get(action, "Operator requested graph-structure review.")
    return {
        "type": issue_type,
        "category": "asset_binding",
        "reason": user_text or default_reason,
        "summary": "Asset graph-structure action is waiting for Review Queue apply/cancel.",
        "target": str(prepared.get("target_id") or prepared.get("target_node_id") or path),
        "target_type": kind_prefix,
        "paths": [path],
        "intent": action,
        "source": source,
        "operation_type": "graph_structure",
        "subtype": "asset_binding",
        "file_backed": False,
        "priority": "P1" if action in {"remove_binding", "delete_candidate"} else "P2",
        "requires_human_signoff": action in {"remove_binding", "delete_candidate"},
    }


def _queue_file_hygiene_review_feedback(
    reconcile_feedback,
    project_id: str,
    snapshot_id: str,
    prepared: dict,
    *,
    source: str,
) -> dict[str, Any]:
    action = str(prepared.get("action") or "").strip().lower()
    if action not in {"attach_to_node", "remove_binding", "delete_candidate", "create_node"}:
        return {}
    return reconcile_feedback.submit_feedback_item(
        project_id,
        snapshot_id,
        feedback_kind=(
            reconcile_feedback.KIND_NEEDS_OBSERVER_DECISION
            if action in {"remove_binding", "delete_candidate"}
            else reconcile_feedback.KIND_GRAPH_CORRECTION
        ),
        issue=_file_hygiene_graph_structure_issue(prepared, source=source),
        actor=str(prepared.get("actor") or "dashboard_user"),
        source_round="graph_structure_lifecycle",
    )


def _resolve_project_child(root: Path, rel_path: str) -> Path:
    from .errors import ValidationError

    rel = str(rel_path or "").strip().replace("\\", "/").strip("/")
    if not rel:
        raise ValidationError("path is required")
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        raise ValidationError("path must stay within project root")
    return candidate


def _hint_role_for_file_kind(file_kind: str, requested_role: str = "") -> str:
    role = str(requested_role or "").strip().lower()
    if role in {"doc", "test", "config"}:
        return role
    kind = str(file_kind or "").strip().lower()
    if kind == "doc":
        return "doc"
    if kind == "test":
        return "test"
    if kind == "config":
        return "config"
    return ""


def _snapshot_node_by_id(store, conn, project_id: str, snapshot_id: str, node_id: str) -> dict:
    for node in store.list_graph_snapshot_nodes(
        conn,
        project_id,
        snapshot_id,
        limit=1000,
        include_semantic=False,
    ):
        if str(node.get("node_id") or "") == node_id:
            return dict(node)
    return {}


def _snapshot_node_stable_hint_fields(node: dict) -> dict[str, str]:
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    module = str(node.get("module") or metadata.get("module") or "").strip()
    title = str(node.get("title") or "").strip()
    area_key = str(node.get("area_key") or metadata.get("area_key") or "").strip()
    subsystem_key = str(node.get("subsystem_key") or metadata.get("subsystem_key") or "").strip()
    asset_key = str(node.get("asset_key") or metadata.get("asset_key") or "").strip()
    out: dict[str, str] = {}
    if module:
        out["target_module"] = module
    if area_key:
        out["target_area_key"] = area_key
    if subsystem_key:
        out["target_subsystem_key"] = subsystem_key
    if asset_key:
        out["target_asset_key"] = asset_key
    if title:
        out["target_title"] = title
    return out


def _snapshot_node_asset_paths(node: dict, role: str) -> set[str]:
    metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
    aliases = {
        "doc": ("secondary", "secondary_files", "docs", "doc_files"),
        "test": ("test", "tests", "test_files"),
        "config": ("config", "config_files"),
    }.get(role, ())
    paths: set[str] = set()
    for key in aliases:
        paths.update(_server_path_list(node.get(key)))
    if role == "config":
        paths.update(_server_path_list(metadata.get("config_files")))
    return paths


def _server_path_list(raw: object) -> list[str]:
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, list):
        values = raw
    else:
        values = []
    return sorted({
        str(item or "").replace("\\", "/").strip("/")
        for item in values
        if str(item or "").strip()
    })


def _file_inventory_row_has_current_binding(
    row: dict,
    target_node: dict,
    *,
    path: str,
    node_id: str,
    role: str,
) -> bool:
    node_ids: set[str] = set()
    for key in ("attached_node_ids", "mapped_node_ids"):
        node_ids.update(_server_path_list(row.get(key)))
    attached_to = str(row.get("attached_to") or "").strip()
    if attached_to:
        node_ids.add(attached_to)
    row_role = str(row.get("attachment_role") or "").strip().lower()
    role_aliases = {
        "doc": {"doc", "secondary"},
        "test": {"test"},
        "config": {"config"},
    }.get(role, {role})
    if node_id in node_ids and (not row_role or row_role in role_aliases):
        return True
    rel = str(path or "").replace("\\", "/").strip("/")
    return rel in _snapshot_node_asset_paths(target_node, role)


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/file-hygiene/hints/attach")
def handle_graph_governance_snapshot_file_hygiene_hint_attach(ctx: RequestContext):
    """Write a source-controlled governance hint into an orphan file.

    The write changes the working tree only. Operators must commit the file and
    then run Update graph; reconcile is the only path that materializes the
    binding into graph metadata.
    """
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import graph_snapshot_store as store
    from .errors import ValidationError
    from .governance_hints import parse_governance_hint_bindings, render_governance_hint_comment
    from . import reconcile_feedback

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.file-hygiene.hint.attach")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        root = _graph_governance_project_root(project_id, body)
        path = str(body.get("path") or body.get("file_path") or "").strip().replace("\\", "/").strip("/")
        node_id = str(body.get("node_id") or body.get("target_node_id") or "").strip()
        if not node_id:
            raise ValidationError("target_node_id is required")

        files_result = store.list_graph_snapshot_files(
            conn,
            project_id,
            snapshot_id,
            limit=1000,
            path_contains=path,
        )
        row = _find_file_inventory_row(files_result, path)
        if not row:
            raise ValidationError(
                f"file inventory row not found: {path}. "
                "Run Update graph/reconcile first so the file appears in the "
                "snapshot file inventory before writing a governance hint."
            )
        if row.get("attached_node_ids"):
            raise ValidationError("file is already attached to a node")
        scan_status = str(row.get("scan_status") or "")
        if scan_status != "orphan":
            raise ValidationError(f"file is not attachable from scan_status={scan_status}; orphan required")
        role = _hint_role_for_file_kind(str(row.get("file_kind") or ""), str(body.get("role") or ""))
        if not role:
            raise ValidationError(f"file kind is not supported for hint attach: {row.get('file_kind') or 'unknown'}")
        target_node = _snapshot_node_by_id(store, conn, project_id, snapshot_id, node_id)
        if not target_node:
            raise ValidationError(f"target node not found: {node_id}")

        abs_path = _resolve_project_child(root, path)
        if not abs_path.exists() or not abs_path.is_file():
            raise ValidationError(f"file does not exist: {path}")
        payload = {
            "attach_to_node": {
                "path": path,
                "role": role,
                "target_node_id": node_id,
                **_snapshot_node_stable_hint_fields(target_node),
            }
        }
        comment = render_governance_hint_comment(path, payload)
        if not comment:
            raise ValidationError(f"file type does not support direct governance-hint comments: {path}")
        text = abs_path.read_text(encoding="utf-8")
        existing = parse_governance_hint_bindings(text, source_path=path)
        for hint in existing:
            if (
                hint.path == path
                and hint.field in {"secondary", "test", "config"}
                and hint.target_node_id == node_id
            ):
                feedback = reconcile_feedback.submit_feedback_item(
                    project_id,
                    snapshot_id,
                    feedback_kind=reconcile_feedback.KIND_GRAPH_CORRECTION,
                    issue={
                        "type": "governance_hint_attach",
                        "reason": str(body.get("reason") or "Governance hint already present for asset binding."),
                        "summary": "Governance hint asset binding is waiting for commit/apply and Update Graph.",
                        "target": node_id,
                        "target_type": role,
                        "paths": [path],
                        "intent": str(body.get("intent") or body.get("operator_intent") or "attach_asset_to_node"),
                        "priority": "P2",
                    },
                    actor=str(body.get("actor") or "dashboard_user"),
                    source_round="graph_structure_lifecycle",
                )
                conn.commit()
                return {
                    "ok": True,
                    "project_id": project_id,
                    "snapshot_id": snapshot_id,
                    "path": path,
                    "target_node_id": node_id,
                    "role": role,
                    "state": "written_uncommitted",
                    "hint_written": False,
                    "already_present": True,
                    "requires_commit": True,
                    "update_graph_after_commit": True,
                    "message": "Governance hint already exists. Commit the file, then run Update graph.",
                    "review_queue": {
                        "queued": True,
                        "feedback": (feedback.get("items") or [{}])[0],
                        "operation_type": "graph_structure",
                        "subtype": "governance_hint",
                    },
                    "file": row,
                    "target_node": target_node,
                }

        prefix = comment.rstrip() + "\n\n"
        abs_path.write_text(prefix + text, encoding="utf-8")
        feedback = reconcile_feedback.submit_feedback_item(
            project_id,
            snapshot_id,
            feedback_kind=reconcile_feedback.KIND_GRAPH_CORRECTION,
            issue={
                "type": "governance_hint_attach",
                "reason": str(body.get("reason") or "Operator requested source-controlled asset binding hint."),
                "summary": "Governance hint asset binding is waiting for commit/apply and Update Graph.",
                "target": node_id,
                "target_type": role,
                "paths": [path],
                "changed_files": [path],
                "intent": str(body.get("intent") or body.get("operator_intent") or "attach_asset_to_node"),
                "priority": "P2",
            },
            actor=str(body.get("actor") or "dashboard_user"),
            source_round="graph_structure_lifecycle",
        )
        conn.commit()
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "path": path,
            "target_node_id": node_id,
            "role": role,
            "state": "written_uncommitted",
            "hint_written": True,
            "already_present": False,
            "requires_commit": True,
            "update_graph_after_commit": True,
            "message": "Governance hint written. Commit the file, then run Update graph.",
            "hint": comment,
            "review_queue": {
                "queued": True,
                "feedback": (feedback.get("items") or [{}])[0],
                "operation_type": "graph_structure",
                "subtype": "governance_hint",
            },
            "file": row,
            "target_node": target_node,
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/file-hygiene/hints/unbind")
def handle_graph_governance_snapshot_file_hygiene_hint_unbind(ctx: RequestContext):
    """Append a source-controlled asset binding unbind command.

    The old bind evidence remains in source. Reconcile replays the command log
    and materializes the effective removal after the operator commits the file
    and runs Update Graph.
    """
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import graph_snapshot_store as store
    from . import reconcile_feedback
    from .errors import ValidationError
    from .governance_hints import parse_governance_hint_bindings, render_governance_hint_comment

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.file-hygiene.hint.unbind")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        root = _graph_governance_project_root(project_id, body)
        path = str(body.get("path") or body.get("file_path") or "").strip().replace("\\", "/").strip("/")
        node_id = str(body.get("node_id") or body.get("target_node_id") or "").strip()
        reason = str(body.get("reason") or "").strip()
        actor = str(body.get("actor") or "dashboard-user")
        dry_run = bool(body.get("dry_run")) if body.get("dry_run") is not None else False
        if not path:
            raise ValidationError("path is required")
        if not node_id:
            raise ValidationError("target_node_id is required")
        if not reason:
            raise ValidationError("reason is required")

        files_result = store.list_graph_snapshot_files(
            conn,
            project_id,
            snapshot_id,
            limit=1000,
            path_contains=path,
        )
        row = _find_file_inventory_row(files_result, path)
        if not row:
            raise ValidationError(
                f"file inventory row not found: {path}. "
                "Run Update graph/reconcile first so the file appears in the "
                "snapshot file inventory before writing a governance hint."
            )
        role = _hint_role_for_file_kind(str(row.get("file_kind") or ""), str(body.get("role") or ""))
        if not role:
            raise ValidationError(f"file kind is not supported for hint unbind: {row.get('file_kind') or 'unknown'}")
        target_node = _snapshot_node_by_id(store, conn, project_id, snapshot_id, node_id)
        if not target_node:
            raise ValidationError(f"target node not found: {node_id}")
        if not _file_inventory_row_has_current_binding(
            row,
            target_node,
            path=path,
            node_id=node_id,
            role=role,
        ):
            raise ValidationError(
                "source-controlled unbind requires an existing accepted binding "
                f"between {path} and {node_id}; run Update Graph first if the "
                "dashboard snapshot is stale."
            )

        abs_path = _resolve_project_child(root, path)
        if not abs_path.exists() or not abs_path.is_file():
            raise ValidationError(f"file does not exist: {path}")
        try:
            text = abs_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(f"file is not utf-8 text: {path}") from exc

        payload = {
            "asset_binding_event": {
                "schema_version": "asset_binding_event.v1",
                "operation": "unbind",
                "path": path,
                "role": role,
                "target_node_id": node_id,
                "reason": reason,
                "actor": actor,
                **_snapshot_node_stable_hint_fields(target_node),
            }
        }
        comment = render_governance_hint_comment(path, payload)
        if not comment:
            raise ValidationError(f"file type does not support direct governance-hint comments: {path}")
        already_present = any(
            hint.operation == "unbind"
            and hint.path == path
            and hint.target_node_id == node_id
            and hint.field in {"secondary", "test", "config"}
            for hint in parse_governance_hint_bindings(text, source_path=path)
        )
        changed = not already_present
        if changed and not dry_run:
            separator = "\n\n" if text and not text.endswith("\n\n") else ""
            newline = "" if text.endswith("\n") else "\n"
            abs_path.write_text(text + newline + separator + comment.rstrip() + "\n", encoding="utf-8")

        feedback = reconcile_feedback.submit_feedback_item(
            project_id,
            snapshot_id,
            feedback_kind=reconcile_feedback.KIND_NEEDS_OBSERVER_DECISION,
            issue={
                "type": f"{role}_binding_unbind",
                "category": "asset_binding",
                "reason": reason,
                "summary": "Source-controlled unbind command is waiting for commit/apply and Update Graph.",
                "target": node_id,
                "target_type": role,
                "paths": [path],
                "changed_files": [path] if changed and not dry_run else [],
                "intent": str(body.get("intent") or body.get("operator_intent") or "unbind_asset_from_node"),
                "operation_type": "graph_structure",
                "subtype": "governance_hint",
                "file_backed": True,
                "priority": "P1",
                "requires_human_signoff": True,
            },
            actor=actor,
            source_round="graph_structure_lifecycle",
        )
        audit_service.record(
            conn,
            project_id,
            "governance_hint_unbind",
            actor=actor,
            request_id=ctx.request_id,
            details=json.dumps({
                "snapshot_id": snapshot_id,
                "path": path,
                "target_node_id": node_id,
                "role": role,
                "dry_run": dry_run,
                "changed": changed,
                "already_present": already_present,
            }, ensure_ascii=False, sort_keys=True),
        )
        conn.commit()
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "path": path,
            "target_node_id": node_id,
            "role": role,
            "state": "planned" if dry_run else "written_uncommitted",
            "dry_run": dry_run,
            "hint_written": bool(changed and not dry_run),
            "already_present": already_present,
            "requires_commit": bool(changed and not dry_run),
            "update_graph_after_commit": bool(changed and not dry_run),
            "message": (
                "Governance unbind hint written. Commit the file, then run Update graph."
                if changed and not dry_run
                else "Governance unbind hint planned; no source file was changed."
                if dry_run
                else "Governance unbind hint already exists. Commit the file, then run Update graph."
            ),
            "hint": comment,
            "review_queue": {
                "queued": True,
                "feedback": (feedback.get("items") or [{}])[0],
                "operation_type": "graph_structure",
                "subtype": "governance_hint",
            },
            "file": row,
            "target_node": target_node,
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/file-hygiene/hints/repair")
def handle_graph_governance_snapshot_file_hygiene_hint_repair(ctx: RequestContext):
    """Repair or withdraw source-controlled governance hint comments.

    This endpoint edits only the project file containing the hint.  The graph
    changes only after the operator commits the file and runs Update Graph.
    """
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import graph_snapshot_store as store
    from .errors import ValidationError
    from .governance_hints import rewrite_governance_hint_text

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.file-hygiene.hint.repair")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        root = _graph_governance_project_root(project_id, body)
        path = str(body.get("path") or body.get("file_path") or "").strip().replace("\\", "/").strip("/")
        if not path:
            raise ValidationError("path is required")
        action = str(body.get("action") or "").strip().lower()
        if action not in {"stabilize", "withdraw"}:
            raise ValidationError("action must be stabilize or withdraw")
        abs_path = _resolve_project_child(root, path)
        if not abs_path.exists() or not abs_path.is_file():
            raise ValidationError(f"file does not exist: {path}")
        try:
            text = abs_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(f"file is not utf-8 text: {path}") from exc
        nodes = store.list_graph_snapshot_nodes(
            conn,
            project_id,
            snapshot_id,
            limit=2000,
            include_semantic=False,
        )
        rewrite = rewrite_governance_hint_text(
            text,
            source_path=path,
            nodes=[dict(node) for node in nodes],
            action=action,
            path=str(body.get("hint_path") or body.get("target_path") or path),
            role=str(body.get("role") or ""),
            target_node_id=str(body.get("target_node_id") or body.get("node_id") or ""),
            target_module=str(body.get("target_module") or ""),
        )
        dry_run = bool(body.get("dry_run")) if body.get("dry_run") is not None else False
        if rewrite["changed"] and not dry_run:
            abs_path.write_text(str(rewrite["text"]), encoding="utf-8")
        audit_service.record(
            conn,
            project_id,
            "governance_hint_repair",
            actor=str(body.get("actor") or "dashboard-user"),
            request_id=ctx.request_id,
            details=json.dumps({
                "snapshot_id": snapshot_id,
                "path": path,
                "action": action,
                "dry_run": dry_run,
                "changed": rewrite["changed"],
                "repaired_count": rewrite["repaired_count"],
                "withdrawn_count": rewrite["withdrawn_count"],
                "error_count": rewrite["error_count"],
            }, ensure_ascii=False, sort_keys=True),
        )
        conn.commit()
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "path": path,
            "action": action,
            "state": "planned" if dry_run else "written_uncommitted",
            "dry_run": dry_run,
            "changed": rewrite["changed"],
            "repaired_count": rewrite["repaired_count"],
            "withdrawn_count": rewrite["withdrawn_count"],
            "unchanged_count": rewrite["unchanged_count"],
            "error_count": rewrite["error_count"],
            "errors": rewrite["errors"],
            "requires_commit": bool(rewrite["changed"] and not dry_run),
            "update_graph_after_commit": bool(rewrite["changed"] and not dry_run),
            "message": (
                "Governance hint repair written. Commit the file, then run Update graph."
                if rewrite["changed"] and not dry_run
                else "Governance hint repair planned; no source file was changed."
                if dry_run
                else "No matching governance hint needed a change."
            ),
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/file-hygiene/actions")
def handle_graph_governance_snapshot_file_hygiene_action(ctx: RequestContext):
    """Turn dashboard file-hygiene actions into auditable graph events.

    This endpoint intentionally does not mutate or delete files.  Even a
    delete candidate is recorded as a high-risk proposed event so the existing
    event review / backlog / materialization flow remains the control point.
    """
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import graph_events
    from . import graph_snapshot_store as store
    from . import reconcile_feedback
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.file-hygiene.action")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        prepared = _prepare_file_hygiene_action(conn, store, project_id, snapshot_id, body)
        with sqlite_write_lock():
            event = _create_file_hygiene_event(
                graph_events,
                conn,
                project_id,
                snapshot_id,
                prepared,
                source="file_hygiene_action_api",
            )
            feedback = _queue_file_hygiene_review_feedback(
                reconcile_feedback,
                project_id,
                snapshot_id,
                prepared,
                source="file_hygiene_action_api",
            )
            conn.commit()
        return 201, {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "action": prepared["action"],
            "event": event,
            "review_queue": {
                "queued": bool(feedback),
                "feedback": (feedback.get("items") or [{}])[0] if feedback else {},
                "operation_type": "graph_structure",
                "subtype": "asset_binding",
            },
            "file": prepared["file"],
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/file-hygiene/actions/batch")
def handle_graph_governance_snapshot_file_hygiene_actions_batch(ctx: RequestContext):
    """Create multiple audit-only file-hygiene graph events in one request."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import graph_events
    from . import graph_snapshot_store as store
    from . import reconcile_feedback
    from .db import sqlite_write_lock
    from .errors import ValidationError

    raw_actions = body.get("actions") or body.get("items")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ValidationError("actions must be a non-empty list")
    if len(raw_actions) > 100:
        raise ValidationError("batch file hygiene actions are limited to 100 items")

    inherited = {
        key: body[key]
        for key in ("actor", "confidence", "operator_signoff", "confirm_delete_candidate")
        if key in body
    }
    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.file-hygiene.action")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        prepared_actions = []
        for index, raw in enumerate(raw_actions):
            if not isinstance(raw, dict):
                raise ValidationError(f"actions[{index}] must be an object")
            item = dict(inherited)
            item.update(raw)
            prepared = _prepare_file_hygiene_action(conn, store, project_id, snapshot_id, item)
            prepared["batch_index"] = index
            prepared_actions.append(prepared)

        events = []
        feedback_items = []
        with sqlite_write_lock():
            for prepared in prepared_actions:
                event = _create_file_hygiene_event(
                    graph_events,
                    conn,
                    project_id,
                    snapshot_id,
                    prepared,
                    source="file_hygiene_batch_action_api",
                )
                events.append(event)
                feedback = _queue_file_hygiene_review_feedback(
                    reconcile_feedback,
                    project_id,
                    snapshot_id,
                    prepared,
                    source="file_hygiene_batch_action_api",
                )
                feedback_items.extend(feedback.get("items") or [])
            conn.commit()
        return 201, {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "count": len(events),
            "events": events,
            "review_queue": {
                "queued": bool(feedback_items),
                "feedback": feedback_items,
                "operation_type": "graph_structure",
                "subtype": "asset_binding",
            },
            "files": [prepared["file"] for prepared in prepared_actions],
        }
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/dashboard-review")
def handle_graph_governance_snapshot_dashboard_review(ctx: RequestContext):
    """Return a dashboard-ready bundle with two graph views and review state."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    from . import reconcile_dashboard_review

    conn = get_connection(project_id)
    try:
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        try:
            return reconcile_dashboard_review.build_dashboard_review_bundle(
                conn,
                project_id,
                snapshot_id,
                node_limit=_query_int(ctx.query, "node_limit", 120),
                edge_limit=_query_int(ctx.query, "edge_limit", 240),
                queue_group_limit=_query_int(ctx.query, "queue_group_limit", 20),
                persist=_query_bool(ctx.query, "persist", True),
            )
        except KeyError as exc:
            _raise_graph_api_validation(exc)
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/query-traces/start")
def handle_graph_governance_query_trace_start(ctx: RequestContext):
    """Start an audited graph query trace for dashboard/AI/chain consumers."""
    project_id = ctx.get_project_id()
    body = ctx.body
    from . import graph_query_trace
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_query_capability(ctx, conn, body, "graph-governance.query-trace.start")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, str(body.get("snapshot_id") or "active"))
        try:
            with sqlite_write_lock():
                result = graph_query_trace.start_trace(
                    conn,
                    project_id,
                    snapshot_id,
                    actor=str(body.get("actor") or "observer"),
                    query_source=str(body.get("query_source") or "api_debug"),
                    query_purpose=str(body.get("query_purpose") or "api_debug"),
                    run_id=str(body.get("run_id") or ""),
                    parent_task_id=str(body.get("parent_task_id") or ""),
                    budget=body.get("query_budget") if isinstance(body.get("query_budget"), dict) else None,
                    trace_id=body.get("trace_id") or None,
                )
                conn.commit()
            return result
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/query")
def handle_graph_governance_query(ctx: RequestContext):
    """Run one graph query and append it to an auditable trace."""
    project_id = ctx.get_project_id()
    body = ctx.body
    from . import graph_query_trace
    from .db import sqlite_write_lock

    tool = str(body.get("tool") or "")
    root = None
    if (
        body.get("project_root")
        or body.get("workspace_path")
        or body.get("repo_root")
        or tool in {"search_docs", "get_file_excerpt"}
    ):
        root = _graph_governance_project_root(project_id, body)
    conn = get_connection(project_id)
    try:
        _require_graph_query_capability(ctx, conn, body, "graph-governance.query")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, str(body.get("snapshot_id") or "active"))
        try:
            with sqlite_write_lock():
                result = graph_query_trace.traced_query(
                    conn,
                    project_id,
                    snapshot_id,
                    tool=tool,
                    args=body.get("args") if isinstance(body.get("args"), dict) else {},
                    trace_id=str(body.get("trace_id") or ""),
                    actor=str(body.get("actor") or "observer"),
                    query_source=str(body.get("query_source") or "api_debug"),
                    query_purpose=str(body.get("query_purpose") or "api_debug"),
                    run_id=str(body.get("run_id") or ""),
                    parent_task_id=str(body.get("parent_task_id") or ""),
                    budget=body.get("query_budget") if isinstance(body.get("query_budget"), dict) else None,
                    project_root=root,
                )
                conn.commit()
            return result
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/query-traces/{trace_id}/finish")
def handle_graph_governance_query_trace_finish(ctx: RequestContext):
    """Finish an audited graph query trace."""
    project_id = ctx.get_project_id()
    trace_id = ctx.path_params["trace_id"]
    body = ctx.body
    from . import graph_query_trace
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        try:
            with sqlite_write_lock():
                existing = graph_query_trace.get_trace(conn, project_id, trace_id)["trace"]
                _require_graph_query_trace_capability(
                    ctx,
                    conn,
                    existing,
                    "graph-governance.query-trace.finish",
                )
                result = graph_query_trace.finish_trace(
                    conn,
                    project_id,
                    trace_id,
                    status=str(body.get("status") or "complete"),
                    reason=str(body.get("reason") or ""),
                )
                conn.commit()
            return result
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/query-traces/{trace_id}")
def handle_graph_governance_query_trace_get(ctx: RequestContext):
    """Return one audited graph query trace and event summary."""
    project_id = ctx.get_project_id()
    trace_id = ctx.path_params["trace_id"]
    from . import graph_query_trace

    conn = get_connection(project_id)
    try:
        try:
            result = graph_query_trace.get_trace(conn, project_id, trace_id)
            _require_graph_query_trace_capability(
                ctx,
                conn,
                result["trace"],
                "graph-governance.query-trace.get",
            )
            return result
        except KeyError as exc:
            _raise_graph_api_validation(exc)
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/export-cache")
def handle_graph_governance_snapshot_export_cache(ctx: RequestContext):
    """Export a non-authoritative .aming-claw/cache graph.current.json."""
    project_id = ctx.get_project_id()
    raw_snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    root = _graph_governance_project_root(project_id, body)
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.export-cache")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id)
        try:
            result = store.export_graph_snapshot_cache(
                conn,
                project_id,
                snapshot_id,
                project_root=root,
                cache_dir=body.get("cache_dir") or None,
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        return 201, {"ok": True, **result}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/drift")
def handle_graph_governance_drift_list(ctx: RequestContext):
    """List graph drift ledger rows for dashboard/operator review."""
    project_id = ctx.get_project_id()
    from . import graph_snapshot_store as store

    conn = get_connection(project_id)
    try:
        status_payload = store.graph_governance_status(conn, project_id)
        raw_snapshot_id = str(ctx.query.get("snapshot_id") or "")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, raw_snapshot_id) if raw_snapshot_id else ""
        rows = store.list_graph_drift(
            conn,
            project_id,
            snapshot_id=snapshot_id,
            status=str(ctx.query.get("status") or ""),
            drift_type=str(ctx.query.get("drift_type") or ""),
            limit=_query_int(ctx.query, "limit", 200),
            offset=_query_int(ctx.query, "offset", 0),
        )
        resolved_snapshot_id = snapshot_id or str(status_payload.get("active_snapshot_id") or "")
        current_state = _dashboard_current_state(
            conn,
            project_id,
            status=status_payload,
            snapshot_id=resolved_snapshot_id,
        )
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": resolved_snapshot_id,
            "drift": rows,
            "count": len(rows),
            "ledger_count": len(rows),
            "ledger_only": True,
            "current_state": current_state,
            "graph_stale": current_state["graph_stale"],
            "semantic_drift": current_state["semantic_drift"],
            "note": "drift rows are explicit graph_drift_ledger entries; use current_state.graph_stale and current_state.semantic_drift for synthesized dashboard drift.",
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/drift")
def handle_graph_governance_drift_record(ctx: RequestContext):
    """Record one graph drift row with evidence."""
    project_id = ctx.get_project_id()
    body = ctx.body
    required = ["snapshot_id", "commit_sha", "path", "drift_type"]
    missing = [key for key in required if not str(body.get(key) or "").strip()]
    if missing:
        from .errors import ValidationError
        raise ValidationError(f"missing required drift field(s): {', '.join(missing)}")
    from . import graph_snapshot_store as store
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.drift.record")
        with sqlite_write_lock():
            store.record_drift(
                conn,
                project_id,
                snapshot_id=str(body.get("snapshot_id") or ""),
                commit_sha=str(body.get("commit_sha") or ""),
                path=str(body.get("path") or ""),
                drift_type=str(body.get("drift_type") or ""),
                target_symbol=str(body.get("target_symbol") or ""),
                node_id=str(body.get("node_id") or ""),
                status=str(body.get("status") or "open"),
                evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else {
                    "source": "graph_governance_api",
                    "actor": body.get("actor", "api"),
                },
            )
            conn.commit()
        row = store.list_graph_drift(
            conn,
            project_id,
            snapshot_id=str(body.get("snapshot_id") or ""),
            drift_type=str(body.get("drift_type") or ""),
            status=str(body.get("status") or "open"),
            limit=20,
        )
        return 201, {"ok": True, "project_id": project_id, "drift": row}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/drift/file-backlog")
def handle_graph_governance_drift_file_backlog(ctx: RequestContext):
    """File one graph drift row into backlog and mark it backlog_filed."""
    project_id = ctx.get_project_id()
    body = ctx.body
    required = ["snapshot_id", "path", "drift_type"]
    missing = [key for key in required if not str(body.get(key) or "").strip()]
    if missing:
        from .errors import ValidationError
        raise ValidationError(f"missing required drift field(s): {', '.join(missing)}")
    from . import graph_snapshot_store as store
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.drift.file-backlog")
        with sqlite_write_lock():
            snapshot_id = _resolve_graph_snapshot_id(conn, project_id, str(body.get("snapshot_id") or ""))
            target_symbol_raw = body.get("target_symbol")
            target_symbol = None if target_symbol_raw is None else str(target_symbol_raw)
            try:
                drift = store.get_graph_drift(
                    conn,
                    project_id,
                    snapshot_id=snapshot_id,
                    path=str(body.get("path") or ""),
                    drift_type=str(body.get("drift_type") or ""),
                    target_symbol=target_symbol,
                )
            except (KeyError, ValueError) as exc:
                _raise_graph_api_validation(exc)

            bug_id = str(body.get("bug_id") or "").strip() or _graph_drift_backlog_id(
                snapshot_id,
                drift["path"],
                drift["drift_type"],
                drift["target_symbol"],
            )
            now = _utc_now()
            actor = str(body.get("actor") or "graph_governance_api")
            title = str(body.get("title") or "").strip() or (
                f"Resolve graph drift: {drift['drift_type']} in {drift['path']}"
            )
            details_md = str(body.get("details_md") or "").strip()
            if not details_md:
                details_md = "\n".join([
                    f"Graph drift row filed from snapshot `{snapshot_id}`.",
                    "",
                    f"- path: `{drift['path']}`",
                    f"- drift_type: `{drift['drift_type']}`",
                    f"- node_id: `{drift['node_id']}`",
                    f"- target_symbol: `{drift['target_symbol']}`",
                    f"- commit: `{drift['commit_sha']}`",
                    "",
                    "Review the graph/drift evidence, then either repair through chain or explicitly waive.",
                ])
            acceptance = body.get("acceptance_criteria")
            if not isinstance(acceptance, list):
                acceptance = [
                    "Drift row is fixed, waived, or converted into a more precise graph/document/test task.",
                    "Backlog close evidence references the graph snapshot and affected path.",
                    "Scope reconcile materializes graph state after any merge.",
                ]
            priority = str(body.get("priority") or "P2")
            target_files = body.get("target_files") if isinstance(body.get("target_files"), list) else [drift["path"]]
            conn.execute(
                """INSERT INTO backlog_bugs
                   (bug_id, title, status, priority, target_files, test_files,
                    acceptance_criteria, chain_task_id, "commit", discovered_at,
                    fixed_at, details_md, chain_trigger_json, required_docs,
                    provenance_paths, bypass_policy_json, mf_type, takeover_json,
                    created_at, updated_at)
                   VALUES (?, ?, 'OPEN', ?, ?, ?, ?, '', ?, ?, '', ?, ?, ?, ?, '{}', '', '{}', ?, ?)
                   ON CONFLICT(bug_id) DO UPDATE SET
                     title = excluded.title,
                     status = 'OPEN',
                     priority = excluded.priority,
                     target_files = excluded.target_files,
                     acceptance_criteria = excluded.acceptance_criteria,
                     details_md = excluded.details_md,
                     chain_trigger_json = excluded.chain_trigger_json,
                     provenance_paths = excluded.provenance_paths,
                     updated_at = excluded.updated_at
                """,
                (
                    bug_id,
                    title,
                    priority,
                    json.dumps(target_files, ensure_ascii=False, sort_keys=True),
                    json.dumps(body.get("test_files") if isinstance(body.get("test_files"), list) else [], ensure_ascii=False),
                    json.dumps(acceptance, ensure_ascii=False, sort_keys=True),
                    drift["commit_sha"],
                    now,
                    details_md,
                    json.dumps({
                        "source": "graph_drift_ledger",
                        "snapshot_id": snapshot_id,
                        "drift_type": drift["drift_type"],
                        "graph_gate_mode": "advisory",
                    }, ensure_ascii=False, sort_keys=True),
                    json.dumps(body.get("required_docs") if isinstance(body.get("required_docs"), list) else [], ensure_ascii=False),
                    json.dumps([drift["path"], f"graph_snapshot:{snapshot_id}"], ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            filed = store.update_graph_drift_status(
                conn,
                project_id,
                snapshot_id=snapshot_id,
                path=drift["path"],
                drift_type=drift["drift_type"],
                target_symbol=drift["target_symbol"],
                status="backlog_filed",
                evidence={
                    "backlog_bug_id": bug_id,
                    "filed_by": actor,
                    "filed_at": now,
                },
            )
            try:
                audit_service.record(
                    conn,
                    project_id,
                    "graph_drift_backlog_filed",
                    actor=actor,
                    bug_id=bug_id,
                    details=json.dumps({
                        "snapshot_id": snapshot_id,
                        "path": drift["path"],
                        "drift_type": drift["drift_type"],
                        "target_symbol": drift["target_symbol"],
                    }, ensure_ascii=False, sort_keys=True),
                )
            except Exception:
                pass
            conn.commit()
        return 201, {
            "ok": True,
            "project_id": project_id,
            "bug_id": bug_id,
            "drift": filed,
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/pending-scope")
def handle_graph_governance_pending_scope_queue(ctx: RequestContext):
    """Queue or update one pending scope-reconcile row."""
    project_id = ctx.get_project_id()
    body = ctx.body
    commit_sha = str(body.get("commit_sha") or body.get("target_commit_sha") or "").strip()
    if not commit_sha:
        from .errors import ValidationError
        raise ValidationError("commit_sha is required")
    from . import graph_snapshot_store as store
    from .db import sqlite_write_lock
    identity = _pending_scope_identity_from_body(body)

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.pending-scope")
        with sqlite_write_lock():
            try:
                row = store.queue_pending_scope_reconcile(
                    conn,
                    project_id,
                    commit_sha=commit_sha,
                    parent_commit_sha=str(body.get("parent_commit_sha") or ""),
                    ref_name=identity["ref_name"],
                    branch_ref=identity["branch_ref"],
                    worktree_id=identity["worktree_id"],
                    worktree_path=identity["worktree_path"],
                    status=str(body.get("status") or store.PENDING_STATUS_QUEUED),
                    snapshot_id=str(body.get("snapshot_id") or ""),
                    evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else {
                        "source": "graph_governance_api",
                        "actor": body.get("actor", "api"),
                        **identity,
                    },
                    force_requeue=_query_bool(body, "force_requeue", False),
                )
            except ValueError as exc:
                _raise_graph_api_validation(exc)
            conn.commit()
        return 201, {"ok": True, "project_id": project_id, "pending_scope_reconcile": row}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/index")
def handle_graph_governance_index_build(ctx: RequestContext):
    """Build and persist governance index artifacts without source mutation."""
    project_id = ctx.get_project_id()
    body = ctx.body
    root = _graph_governance_project_root(project_id, body)
    from .governance_index import build_and_persist_governance_index

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.index")
        result = build_and_persist_governance_index(
            conn,
            project_id,
            root,
            run_id=str(body.get("run_id") or ""),
            commit_sha=str(body.get("commit_sha") or ""),
            include_active_graph=bool(body.get("include_active_graph", True)),
            persist_inventory=bool(body.get("persist_inventory", True)),
        )
        conn.commit()
        return {
            "ok": True,
            "project_id": project_id,
            "run_id": result.get("run_id"),
            "commit_sha": result.get("commit_sha"),
            "active_snapshot": result.get("active_snapshot") or {},
            "file_inventory_summary": result.get("file_inventory_summary") or {},
            "symbol_count": (result.get("symbol_index") or {}).get("symbol_count", 0),
            "doc_heading_count": (result.get("doc_index") or {}).get("heading_count", 0),
            "coverage_state": result.get("coverage_state") or {},
            "persist_summary": result.get("persist_summary") or {},
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/import-existing")
def handle_graph_governance_import_existing(ctx: RequestContext):
    """Import the latest non-empty legacy/baseline graph as a graph snapshot."""
    project_id = ctx.get_project_id()
    body = ctx.body
    from . import graph_snapshot_store as store
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.import-existing")
        with sqlite_write_lock():
            try:
                result = store.import_existing_graph_snapshot(
                    conn,
                    project_id,
                    commit_sha=str(body.get("commit_sha") or ""),
                    snapshot_id=body.get("snapshot_id"),
                    created_by=str(body.get("actor") or "observer"),
                    activate=bool(body.get("activate", False)),
                    expected_old_snapshot_id=body.get("expected_old_snapshot_id"),
                    extra_graph_paths=body.get("extra_graph_paths") or [],
                )
            except store.GraphSnapshotConflictError as exc:
                _raise_graph_api_conflict(exc)
            except (FileNotFoundError, KeyError, ValueError) as exc:
                _raise_graph_api_validation(exc)
            conn.commit()
        return 201, {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/reconcile/full")
def handle_graph_governance_full_reconcile(ctx: RequestContext):
    """Create a state-only full-reconcile candidate snapshot at current HEAD."""
    project_id = ctx.get_project_id()
    body = ctx.body
    root = _graph_governance_project_root(project_id, body)
    from .state_reconcile import run_state_only_full_reconcile
    semantic_use_ai = _semantic_use_ai_from_body(body)
    semantic_ai_call = _semantic_ai_call_from_body(project_id, root, body)

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.reconcile.full")
        try:
            result = run_state_only_full_reconcile(
                conn,
                project_id,
                root,
                run_id=str(body.get("run_id") or ""),
                commit_sha=str(body.get("commit_sha") or ""),
                snapshot_id=body.get("snapshot_id"),
                snapshot_kind=str(body.get("snapshot_kind") or "full"),
                created_by=str(body.get("actor") or "observer"),
                activate=bool(body.get("activate", False)),
                expected_old_snapshot_id=body.get("expected_old_snapshot_id"),
                notes_extra=body.get("notes_extra") if isinstance(body.get("notes_extra"), dict) else None,
                semantic_enrich=bool(body.get("semantic_enrich", True)),
                semantic_use_ai=semantic_use_ai,
                semantic_feedback_items=body.get("semantic_feedback_items") or body.get("feedback_items"),
                semantic_feedback_round=body.get("semantic_feedback_round"),
                semantic_max_excerpt_chars=(
                    int(body["semantic_max_excerpt_chars"])
                    if body.get("semantic_max_excerpt_chars") is not None
                    else None
                ),
                semantic_ai_call=semantic_ai_call,
                semantic_ai_feature_limit=_semantic_ai_feature_limit_from_body(body),
                **_semantic_ai_batch_kwargs_from_body(body),
                **_semantic_state_kwargs_from_body(body),
                semantic_classify_feedback=bool(
                    _semantic_bool_from_body(body, "semantic_classify_feedback", "classify_feedback", default=True)
                ),
                **_semantic_ai_config_kwargs_from_body(body),
                **_semantic_selector_kwargs_from_body(body),
                semantic_config_path=body.get("semantic_config_path"),
                # Dashboard graph-build actions are structural by default:
                # rebuild the snapshot and projection without silently filling
                # graph_semantic_jobs. Operators enqueue AI explicitly later.
                semantic_enqueue_stale=bool(body.get("enqueue_stale", False)),
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        conn.commit()
        return 201, result
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/reconcile/pending-scope")
def handle_graph_governance_pending_scope_materialize(ctx: RequestContext):
    """Create a state-only scope candidate and activate it when requested.

    The old dashboard contract required callers to POST /pending-scope first,
    which left a visible queued operation while the long synchronous build ran.
    The direct Update graph contract lets this endpoint create the transient
    pending row itself, mark it running, then consume it in the same request.
    """
    project_id = ctx.get_project_id()
    body = ctx.body
    root = _graph_governance_project_root(project_id, body)
    from .state_reconcile import run_pending_scope_reconcile_candidate
    from . import graph_snapshot_store as store
    from .db import sqlite_write_lock
    semantic_use_ai = _semantic_use_ai_from_body(body)
    semantic_ai_call = _semantic_ai_call_from_body(project_id, root, body)
    identity = _pending_scope_identity_from_body(body)

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.reconcile.pending-scope")
        target_commit = str(body.get("target_commit_sha") or "").strip()
        target_commit_inferred = False
        if not target_commit:
            try:
                from . import batch_jobs
                target_commit = batch_jobs.git_commit(root)
                target_commit_inferred = bool(target_commit)
            except Exception:
                target_commit = ""
        if not target_commit:
            return 400, {
                "ok": False,
                "project_id": project_id,
                "reason": "target_commit_sha_required",
                "message": (
                    "target_commit_sha is required when governance cannot infer "
                    "the target git HEAD from the project root"
                ),
                "recommended_body": {
                    "target_commit_sha": "<git HEAD commit sha>",
                    "activate": True,
                    "semantic_use_ai": False,
                },
                **identity,
            }
        target_pending_for_failure = False
        if target_commit and bool(body.get("ensure_pending_scope", True)):
            pending = store.list_pending_scope_reconcile(
                conn,
                project_id,
                commit_shas=[target_commit],
                ref_name=identity["ref_name"],
                branch_ref=identity["branch_ref"],
                worktree_id=identity["worktree_id"],
                worktree_path=identity["worktree_path"],
                statuses=[
                    store.PENDING_STATUS_QUEUED,
                    store.PENDING_STATUS_RUNNING,
                    store.PENDING_STATUS_FAILED,
                ],
            )
            if pending:
                target_pending_for_failure = True
                if bool(body.get("force_requeue", False)):
                    active = (
                        store.get_active_graph_snapshot(conn, project_id, ref_name=identity["ref_name"])
                        or store.get_active_graph_snapshot(conn, project_id)
                        or {}
                    )
                    parent_commit = str(
                        body.get("parent_commit_sha") or active.get("commit_sha") or ""
                    )
                    with sqlite_write_lock():
                        store.queue_pending_scope_reconcile(
                            conn,
                            project_id,
                            commit_sha=target_commit,
                            parent_commit_sha=parent_commit,
                            ref_name=identity["ref_name"],
                            branch_ref=identity["branch_ref"],
                            worktree_id=identity["worktree_id"],
                            worktree_path=identity["worktree_path"],
                            status=store.PENDING_STATUS_RUNNING,
                            evidence={
                                "source": "direct_update_graph_force_requeue",
                                "actor": body.get("actor", "api"),
                                "parent_commit_sha": parent_commit,
                                **identity,
                            },
                            force_requeue=True,
                        )
                        conn.commit()
            else:
                active = store.get_active_graph_snapshot(conn, project_id, ref_name=identity["ref_name"]) or {}
                if str(active.get("commit_sha") or "").strip() == target_commit:
                    return 200, {
                        "ok": True,
                        "project_id": project_id,
                        "status": "already_current",
                        "reason": "already_current",
                        "target_commit_sha": target_commit,
                        "target_commit_inferred": target_commit_inferred,
                        **identity,
                        "snapshot_id": active.get("snapshot_id") or "",
                        "active_snapshot_id": active.get("snapshot_id") or "",
                        "pending_count": 0,
                    }
                parent_active = active or store.get_active_graph_snapshot(conn, project_id) or {}
                parent_commit = str(
                    body.get("parent_commit_sha") or parent_active.get("commit_sha") or ""
                )
                with sqlite_write_lock():
                    row = store.queue_pending_scope_reconcile(
                        conn,
                        project_id,
                        commit_sha=target_commit,
                        parent_commit_sha=parent_commit,
                        ref_name=identity["ref_name"],
                        branch_ref=identity["branch_ref"],
                        worktree_id=identity["worktree_id"],
                        worktree_path=identity["worktree_path"],
                        status=store.PENDING_STATUS_RUNNING,
                        evidence={
                            "source": "direct_update_graph",
                            "actor": body.get("actor", "api"),
                            "parent_commit_sha": parent_commit,
                            **identity,
                        },
                    )
                    _ = row
                    target_pending_for_failure = True
                    conn.commit()
        try:
            result = run_pending_scope_reconcile_candidate(
                conn,
                project_id,
                root,
                target_commit_sha=target_commit,
                run_id=str(body.get("run_id") or ""),
                snapshot_id=body.get("snapshot_id"),
                created_by=str(body.get("actor") or "observer"),
                ref_name=identity["ref_name"],
                branch_ref=identity["branch_ref"],
                worktree_id=identity["worktree_id"],
                worktree_path=identity["worktree_path"],
                # MF-2026-05-10-014: dashboard-driven incremental catchup
                # passes activate=true so the materialized snapshot becomes
                # active in one round-trip; MF-012 hook then auto-builds
                # the projection on activation.
                activate=bool(body.get("activate", False)),
                semantic_enrich=bool(body.get("semantic_enrich", True)),
                semantic_use_ai=semantic_use_ai,
                semantic_feedback_items=body.get("semantic_feedback_items") or body.get("feedback_items"),
                semantic_feedback_round=body.get("semantic_feedback_round"),
                semantic_max_excerpt_chars=(
                    int(body["semantic_max_excerpt_chars"])
                    if body.get("semantic_max_excerpt_chars") is not None
                    else None
                ),
                semantic_ai_call=semantic_ai_call,
                semantic_ai_feature_limit=_semantic_ai_feature_limit_from_body(body),
                **_semantic_ai_batch_kwargs_from_body(body),
                **_semantic_state_kwargs_from_body(body),
                semantic_classify_feedback=bool(
                    _semantic_bool_from_body(body, "semantic_classify_feedback", "classify_feedback", default=True)
                ),
                **_semantic_ai_config_kwargs_from_body(body),
                **_semantic_selector_kwargs_from_body(body),
                semantic_config_path=body.get("semantic_config_path"),
                # OPT-BACKLOG-MATERIALIZE-NO-WORKER-NOTIFY: default to NOT
                # auto-enqueueing stale nodes for AI on materialize. The
                # MF-016 in-process worker isn't notified by the persistence
                # path, so silent enqueues would just sit unfinished in
                # graph_semantic_jobs. Operators trigger enrichment
                # explicitly via POST /semantic/jobs (which DOES publish to
                # the EventBus). Caller can opt back into the legacy behavior
                # by passing `enqueue_stale: true` in the body.
                semantic_enqueue_stale=bool(body.get("enqueue_stale", False)),
            )
            if (
                isinstance(result, dict)
                and result.get("ok") is False
                and result.get("reason") == "no_pending_scope_reconcile"
                and target_commit
            ):
                active = store.get_active_graph_snapshot(conn, project_id, ref_name=identity["ref_name"]) or {}
                if str(active.get("commit_sha") or "").strip() == target_commit:
                    result = {
                        "ok": True,
                        "project_id": project_id,
                        "status": "already_current",
                        "reason": "already_current",
                        "target_commit_sha": target_commit,
                        "target_commit_inferred": target_commit_inferred,
                        **identity,
                        "snapshot_id": active.get("snapshot_id") or "",
                        "active_snapshot_id": active.get("snapshot_id") or "",
                        "pending_count": 0,
                    }
        except (KeyError, ValueError) as exc:
            if target_pending_for_failure:
                with sqlite_write_lock():
                    store.mark_pending_scope_reconcile_failed(
                        conn,
                        project_id,
                        commit_sha=target_commit,
                        ref_name=identity["ref_name"],
                        branch_ref=identity["branch_ref"],
                        worktree_id=identity["worktree_id"],
                        worktree_path=identity["worktree_path"],
                        actor=str(body.get("actor") or "api"),
                        reason=str(exc),
                        evidence={"source": "direct_update_graph", "error": str(exc), **identity},
                    )
                    conn.commit()
            _raise_graph_api_validation(exc)
        except Exception as exc:
            if target_pending_for_failure:
                with sqlite_write_lock():
                    store.mark_pending_scope_reconcile_failed(
                        conn,
                        project_id,
                        commit_sha=target_commit,
                        ref_name=identity["ref_name"],
                        branch_ref=identity["branch_ref"],
                        worktree_id=identity["worktree_id"],
                        worktree_path=identity["worktree_path"],
                        actor=str(body.get("actor") or "api"),
                        reason=str(exc),
                        evidence={"source": "direct_update_graph", "error": str(exc), **identity},
                    )
                    conn.commit()
            raise
        conn.commit()
        return 201, result
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/reconcile/pending-scope/catch-up")
def handle_graph_governance_pending_scope_catch_up(ctx: RequestContext):
    """Queue active..HEAD commits and materialize them through scope reconcile."""
    project_id = ctx.get_project_id()
    body = ctx.body
    root = _graph_governance_project_root(project_id, body)
    from .state_reconcile import run_pending_scope_reconcile_candidate
    from . import graph_snapshot_store as store
    from .db import sqlite_write_lock
    identity = _pending_scope_identity_from_body(body)

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.reconcile.pending-scope.catch-up")
        active = (
            store.get_active_graph_snapshot(conn, project_id, ref_name=identity["ref_name"])
            or store.get_active_graph_snapshot(conn, project_id)
            or {}
        )
        base_commit = str(body.get("base_commit_sha") or active.get("commit_sha") or "").strip()
        target_commit = str(body.get("target_commit_sha") or _git_head_commit(root) or "").strip()
        explicit_commits = [
            str(item or "").strip()
            for item in (body.get("commit_shas") or [])
            if str(item or "").strip()
        ]
        commit_shas = explicit_commits or _git_commit_range(root, base_commit, target_commit)
        if target_commit and commit_shas and commit_shas[-1] != target_commit:
            commit_shas.append(target_commit)
        if not commit_shas:
            return 200, {
                "ok": True,
                "project_id": project_id,
                "status": "already_current",
                "base_commit_sha": base_commit,
                "target_commit_sha": target_commit,
                **identity,
                "commit_count": 0,
                "commits": [],
            }
        if target_commit != _git_head_commit(root):
            return 400, {
                "ok": False,
                "error": "target_not_head",
                "message": "pending-scope catch-up materializes the current worktree; target_commit_sha must equal HEAD",
                "target_commit_sha": target_commit,
                "head_commit": _git_head_commit(root),
            }
        if bool(body.get("dry_run", False)):
            return 200, {
                "ok": True,
                "project_id": project_id,
                "dry_run": True,
                "base_commit_sha": base_commit,
                "target_commit_sha": target_commit,
                **identity,
                "commit_count": len(commit_shas),
                "commits": commit_shas,
                "progress": {"done": 0, "total": len(commit_shas)},
            }

        queued_rows: list[dict[str, Any]] = []
        parent = base_commit
        with sqlite_write_lock():
            for index, commit in enumerate(commit_shas, start=1):
                row = store.queue_pending_scope_reconcile(
                    conn,
                    project_id,
                    commit_sha=commit,
                    parent_commit_sha=parent,
                    ref_name=identity["ref_name"],
                    branch_ref=identity["branch_ref"],
                    worktree_id=identity["worktree_id"],
                    worktree_path=identity["worktree_path"],
                    status=store.PENDING_STATUS_QUEUED,
                    evidence={
                        "source": "pending_scope_catch_up",
                        "actor": body.get("actor", "api"),
                        "index": index,
                        "total": len(commit_shas),
                        "base_commit_sha": base_commit,
                        "target_commit_sha": target_commit,
                        **identity,
                    },
                    force_requeue=bool(body.get("force_requeue", False)),
                )
                queued_rows.append(row)
                parent = commit
            conn.commit()

        try:
            result = run_pending_scope_reconcile_candidate(
                conn,
                project_id,
                root,
                target_commit_sha=target_commit,
                run_id=str(body.get("run_id") or f"scope-catch-up-{target_commit[:7]}"),
                snapshot_id=body.get("snapshot_id"),
                created_by=str(body.get("actor") or "observer"),
                ref_name=identity["ref_name"],
                branch_ref=identity["branch_ref"],
                worktree_id=identity["worktree_id"],
                worktree_path=identity["worktree_path"],
                activate=bool(body.get("activate", True)),
                semantic_enrich=bool(body.get("semantic_enrich", True)),
                semantic_use_ai=_semantic_use_ai_from_body(body),
                semantic_ai_call=_semantic_ai_call_from_body(project_id, root, body),
                semantic_enqueue_stale=bool(body.get("enqueue_stale", False)),
            )
        except Exception as exc:
            with sqlite_write_lock():
                store.mark_pending_scope_reconcile_failed(
                    conn,
                    project_id,
                    commit_sha=target_commit,
                    ref_name=identity["ref_name"],
                    branch_ref=identity["branch_ref"],
                    worktree_id=identity["worktree_id"],
                    worktree_path=identity["worktree_path"],
                    actor=str(body.get("actor") or "api"),
                    reason=str(exc),
                    evidence={"source": "pending_scope_catch_up", "error": str(exc), **identity},
                )
                conn.commit()
            raise

        return 201, {
            "ok": bool(result.get("ok")),
            "project_id": project_id,
            "base_commit_sha": base_commit,
            "target_commit_sha": target_commit,
            **identity,
            "commit_count": len(commit_shas),
            "commits": [
                {
                    "index": index,
                    "commit_sha": commit,
                    "queued_status": queued_rows[index - 1].get("status", "") if index - 1 < len(queued_rows) else "",
                    "covered": commit in set(result.get("covered_commit_shas") or []),
                }
                for index, commit in enumerate(commit_shas, start=1)
            ],
            "progress": {
                "done": len(result.get("covered_commit_shas") or []),
                "total": len(commit_shas),
            },
            "materialize": result,
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/reconcile/backfill-escape")
def handle_graph_governance_backfill_escape(ctx: RequestContext):
    """Activate a HEAD full snapshot and waive stuck pending scope rows."""
    project_id = ctx.get_project_id()
    body = ctx.body
    root = _graph_governance_project_root(project_id, body)
    from .state_reconcile import run_backfill_escape_hatch

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.reconcile.backfill-escape")
        try:
            result = run_backfill_escape_hatch(
                conn,
                project_id,
                root,
                target_commit_sha=str(body.get("target_commit_sha") or ""),
                run_id=str(body.get("run_id") or ""),
                snapshot_id=body.get("snapshot_id"),
                created_by=str(body.get("actor") or "observer"),
                reason=str(body.get("reason") or ""),
                expected_old_snapshot_id=body.get("expected_old_snapshot_id"),
            )
        except ValueError as exc:
            _raise_graph_api_validation(exc)
        conn.commit()
        return 201, result
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/finalize")
def handle_graph_governance_snapshot_finalize(ctx: RequestContext):
    """Activate a candidate graph snapshot with compare-and-swap signoff."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import graph_snapshot_store as store
    from .db import sqlite_write_lock
    identity = _pending_scope_identity_from_body(body)

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.finalize")
        with sqlite_write_lock():
            try:
                result = store.finalize_graph_snapshot(
                    conn,
                    project_id,
                    snapshot_id,
                    target_commit_sha=str(body.get("target_commit_sha") or ""),
                    expected_old_snapshot_id=body.get("expected_old_snapshot_id"),
                    ref_name=identity["ref_name"],
                    branch_ref=identity["branch_ref"],
                    worktree_id=identity["worktree_id"],
                    worktree_path=identity["worktree_path"],
                    actor=str(body.get("actor") or "observer"),
                    materialize_pending=bool(body.get("materialize_pending", True)),
                    covered_commit_shas=body.get("covered_commit_shas") or None,
                    evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else None,
                )
            except store.GraphSnapshotConflictError as exc:
                _raise_graph_api_conflict(exc)
            except (KeyError, ValueError) as exc:
                _raise_graph_api_validation(exc)
            conn.commit()
        return {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/abandon")
def handle_graph_governance_snapshot_abandon(ctx: RequestContext):
    """Abandon a non-active candidate/finalizing graph snapshot."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import graph_snapshot_store as store
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.abandon")
        with sqlite_write_lock():
            try:
                result = store.abandon_graph_snapshot(
                    conn,
                    project_id,
                    snapshot_id,
                    actor=str(body.get("actor") or "observer"),
                    reason=str(body.get("reason") or ""),
                )
            except (KeyError, ValueError) as exc:
                _raise_graph_api_validation(exc)
            conn.commit()
        return {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic-feedback")
def handle_graph_governance_snapshot_semantic_feedback(ctx: RequestContext):
    """Append review feedback for the next snapshot semantic-enrichment round."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import reconcile_semantic_enrichment as semantic
    from .db import sqlite_write_lock

    feedback_items = body.get("feedback_items", body.get("feedback", []))
    if isinstance(feedback_items, dict):
        feedback_items = [feedback_items]
    if not isinstance(feedback_items, list) or not feedback_items:
        from .errors import ValidationError
        raise ValidationError("feedback_items must be a non-empty object or list")

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.semantic-feedback")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        with sqlite_write_lock():
            try:
                result = semantic.append_review_feedback(
                    conn,
                    project_id,
                    snapshot_id,
                    feedback_items,
                    created_by=str(body.get("actor") or "observer"),
                )
            except (KeyError, ValueError) as exc:
                _raise_graph_api_validation(exc)
            conn.commit()
        return {"ok": True, **result}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback")
def handle_graph_governance_snapshot_feedback_list(ctx: RequestContext):
    """List classified reconcile feedback items for a graph snapshot."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    from . import reconcile_feedback

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.list")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        items = reconcile_feedback.list_feedback_items(
            project_id,
            snapshot_id,
            feedback_kind=str(ctx.query.get("feedback_kind") or ctx.query.get("kind") or ""),
            status=str(ctx.query.get("status") or ""),
            node_id=str(ctx.query.get("node_id") or ""),
            limit=_query_int(ctx.query, "limit", 200),
        )
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "summary": reconcile_feedback.feedback_summary(project_id, snapshot_id),
            "items": items,
            "count": len(items),
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback")
def handle_graph_governance_snapshot_feedback_submit(ctx: RequestContext):
    """Submit dashboard/operator feedback into the same lanes used by AI review."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import graph_events
    from . import reconcile_feedback
    from .errors import ValidationError

    feedback_kind = str(body.get("feedback_kind") or body.get("kind") or "").strip()
    issue = body.get("issue") if isinstance(body.get("issue"), dict) else {}
    if not issue:
        source_node_ids = body.get("source_node_ids") or body.get("node_ids") or body.get("node_id") or []
        issue = {
            "feedback_id": body.get("feedback_id") or body.get("id") or "",
            "source_node_ids": source_node_ids,
            "type": body.get("issue_type") or body.get("type") or feedback_kind,
            "reason": body.get("reason") or body.get("rationale") or "dashboard feedback",
            "summary": body.get("issue") or body.get("summary") or body.get("text") or "",
            "target": body.get("target_id") or body.get("target") or "",
            "target_type": body.get("target_type") or "",
            "paths": body.get("paths") or body.get("target_files") or body.get("path") or [],
            "priority": body.get("priority") or "",
            "confidence": body.get("confidence"),
            "requires_human_signoff": body.get("requires_human_signoff"),
        }
    if not feedback_kind:
        feedback_kind = str(issue.get("feedback_kind") or issue.get("kind") or "")
    if not feedback_kind:
        raise ValidationError("feedback_kind is required for dashboard feedback")

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.submit")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        try:
            result = reconcile_feedback.submit_feedback_item(
                project_id,
                snapshot_id,
                feedback_kind=feedback_kind,
                issue=issue,
                actor=str(body.get("actor") or body.get("created_by") or "dashboard_user"),
                source_round=str(body.get("source_round") or body.get("feedback_round") or "user"),
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        item = dict((result.get("items") or [{}])[0])
        graph_event = None
        should_create_graph_event = bool(
            body.get("create_graph_event")
            or item.get("feedback_kind") == reconcile_feedback.KIND_GRAPH_CORRECTION
        )
        if should_create_graph_event:
            target_type = str(item.get("target_type") or body.get("target_type") or "node")
            target_id = str(item.get("target_id") or body.get("target_id") or "")
            graph_event = graph_events.create_event(
                conn,
                project_id,
                snapshot_id,
                event_type="graph_correction_proposed",
                event_kind="user_feedback",
                target_type=target_type,
                target_id=target_id,
                status="proposed",
                risk_level=str(body.get("risk_level") or "medium"),
                confidence=float(item.get("confidence") or 0.0),
                payload={
                    "feedback_id": item.get("feedback_id", ""),
                    "feedback": item,
                    "suggested_action": item.get("suggested_action", ""),
                },
                evidence={"source": "dashboard_feedback_submit"},
                created_by=str(body.get("actor") or body.get("created_by") or "dashboard_user"),
            )
        try:
            audit_service.record(
                conn,
                project_id,
                "reconcile_feedback_submitted",
                actor=str(body.get("actor") or "dashboard_user"),
                details=json.dumps({
                    "snapshot_id": snapshot_id,
                    "feedback_id": item.get("feedback_id", ""),
                    "feedback_kind": item.get("feedback_kind", ""),
                    "graph_event_id": (graph_event or {}).get("event_id", ""),
                }, ensure_ascii=False, sort_keys=True),
            )
        except Exception:
            pass
        conn.commit()
        return 201, {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "feedback": item,
            "event": graph_event,
            "summary": reconcile_feedback.feedback_summary(project_id, snapshot_id),
            "action_catalog": reconcile_feedback.feedback_action_catalog(),
        }
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/queue")
def handle_graph_governance_snapshot_feedback_queue(ctx: RequestContext):
    """Return grouped, dashboard-safe reconcile feedback review lanes."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    from . import reconcile_feedback

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.queue")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        queue = reconcile_feedback.build_feedback_review_queue(
            project_id,
            snapshot_id,
            feedback_kind=str(ctx.query.get("feedback_kind") or ctx.query.get("kind") or ""),
            status=str(ctx.query.get("status") or ""),
            node_id=str(ctx.query.get("node_id") or ""),
            source_round=str(ctx.query.get("source_round") or ctx.query.get("feedback_round") or ""),
            lane=str(ctx.query.get("lane") or ""),
            group_by=str(ctx.query.get("group_by") or "target"),
            include_status_observations=_query_bool(ctx.query, "include_status_observations", False),
            include_resolved=_query_bool(ctx.query, "include_resolved", False),
            include_claimed=_query_bool(ctx.query, "include_claimed", True),
            claimable_only=_query_bool(ctx.query, "claimable_only", False),
            require_current_semantic=_query_bool(ctx.query, "require_current_semantic", False),
            worker_id=str(ctx.query.get("worker_id") or ""),
            limit=_query_int(ctx.query, "limit", 100),
            conn=conn,
        )
        _attach_graph_structure_lifecycle_to_feedback_queue(project_id, snapshot_id, queue)
        return {
            "ok": True,
            **queue,
        }
    finally:
        conn.close()


def _attach_graph_structure_lifecycle_to_feedback_queue(
    project_id: str,
    snapshot_id: str,
    queue: dict[str, Any],
) -> None:
    """Add file-backed graph-structure lifecycle metadata to grouped feedback."""
    from . import reconcile_feedback

    groups = queue.get("groups")
    if not isinstance(groups, list):
        return
    state = reconcile_feedback.load_feedback_state(project_id, snapshot_id)
    items = state.get("items") if isinstance(state.get("items"), dict) else {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        feedback_ids = [str(item or "") for item in group.get("feedback_ids") or [] if str(item or "")]
        feedback_items = [dict(items.get(feedback_id) or {}) for feedback_id in feedback_ids]
        lifecycle = _graph_structure_lifecycle_from_feedback_items(feedback_items)
        if lifecycle:
            group["graph_structure_lifecycle"] = lifecycle


def _graph_structure_lifecycle_from_feedback_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    relevant = [item for item in items if _feedback_item_is_graph_structure_lifecycle(item)]
    if not relevant:
        return {}
    files: list[str] = []
    reasons: list[str] = []
    evidence: list[dict[str, Any]] = []
    subtypes: set[str] = set()
    requires_reconcile = False
    for item in relevant:
        files.extend(_feedback_item_changed_files(item))
        reason = _feedback_item_reason(item)
        if reason:
            reasons.append(reason)
        subtype = _graph_structure_subtype(item)
        subtypes.add(subtype)
        if subtype in {"governance_hint", "ai_graph_structure", "asset_binding"}:
            requires_reconcile = True
        evidence.append({
            "feedback_id": item.get("feedback_id", ""),
            "issue_type": item.get("issue_type", ""),
            "reason": reason,
            "intent": _feedback_item_intent(item),
            "subtype": subtype,
            "paths": _feedback_item_paths(item),
        })
    changed_files = _stable_strings(files)
    if not changed_files:
        return {}
    subtype = "mixed" if len(subtypes) > 1 else next(iter(subtypes), "graph_structure")
    return {
        "operation_type": "graph_structure",
        "subtype": subtype,
        "subtype_label": {
            "governance_hint": "Governance Hint",
            "ai_graph_structure": "AI graph structure",
            "asset_binding": "Asset binding",
            "mixed": "Mixed graph structure",
        }.get(subtype, "Graph structure"),
        "changed_files": changed_files,
        "file_count": len(changed_files),
        "requires_commit": bool(changed_files),
        "update_graph_after_commit": requires_reconcile,
        "semantic_lifecycle": "separate",
        "reasons": _stable_strings(reasons),
        "evidence": evidence,
        "supported_actions": ["cancel_graph_structure_operation", "commit_graph_structure_operation"],
        "message": (
            "Graph structure file changes become graph truth only after commit/apply and Update Graph/reconcile."
        ),
    }


def _feedback_item_is_graph_structure_lifecycle(item: dict[str, Any]) -> bool:
    category_text = " ".join(str(part or "").lower() for part in [
        item.get("feedback_kind"),
        item.get("final_feedback_kind"),
        item.get("target_type"),
        item.get("issue_type"),
        item.get("issue"),
        item.get("suggested_action"),
        _feedback_item_reason(item),
        _feedback_item_intent(item),
        " ".join(_feedback_item_paths(item)),
    ])
    return any(token in category_text for token in (
        "graph_structure",
        "graph structure",
        "governance_hint",
        "governance hint",
        "graph_enrich_config",
        "semantic_enrichment_config",
        "asset binding",
        "asset_binding",
        "doc_binding",
        "test_binding",
        "config_binding",
        "typed_relation",
        "add_relation",
        "remove_relation",
        "mount",
        "attach",
        "delete",
        "remove",
    ))


def _graph_structure_subtype(item: dict[str, Any]) -> str:
    text = " ".join(str(part or "").lower() for part in [
        item.get("issue_type"),
        item.get("target_type"),
        item.get("issue"),
        item.get("suggested_action"),
        _feedback_item_reason(item),
        _feedback_item_intent(item),
        " ".join(_feedback_item_paths(item)),
    ])
    if "governance_hint" in text or "governance hint" in text:
        return "governance_hint"
    if (
        "graph_enrich_config" in text
        or "semantic_enrichment_config" in text
        or "registered_action" in text
        or "registered_function" in text
        or "predicate" in text
        or "enricher" in text
        or "ai mount" in text
        or "ai attach" in text
    ):
        return "ai_graph_structure"
    if "binding" in text or "mount" in text or "attach" in text or "asset" in text:
        return "asset_binding"
    return "graph_structure"


def _feedback_item_reason(item: dict[str, Any]) -> str:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    raw = evidence.get("raw_issue") if isinstance(evidence.get("raw_issue"), dict) else {}
    return str(item.get("reason") or evidence.get("reason") or raw.get("reason") or "").strip()


def _feedback_item_intent(item: dict[str, Any]) -> str:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    raw = evidence.get("raw_issue") if isinstance(evidence.get("raw_issue"), dict) else {}
    return str(
        item.get("intent")
        or item.get("operator_intent")
        or raw.get("intent")
        or raw.get("operator_intent")
        or item.get("suggested_action")
        or ""
    ).strip()


def _feedback_item_paths(item: dict[str, Any]) -> list[str]:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    raw = evidence.get("raw_issue") if isinstance(evidence.get("raw_issue"), dict) else {}
    values: list[Any] = []
    for source in (item, raw, evidence):
        if not isinstance(source, dict):
            continue
        for key in ("paths", "path", "target_files", "changed_files", "modified_files", "files", "file_path"):
            value = source.get(key)
            if value:
                values.extend(value if isinstance(value, list) else [value])
    return _stable_strings(str(value or "").replace("\\", "/").strip("/") for value in values)


def _feedback_item_changed_files(item: dict[str, Any]) -> list[str]:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    raw = evidence.get("raw_issue") if isinstance(evidence.get("raw_issue"), dict) else {}
    values: list[Any] = []
    for source in (item, raw, evidence):
        if not isinstance(source, dict):
            continue
        for key in ("changed_files", "modified_files"):
            value = source.get(key)
            if value:
                values.extend(value if isinstance(value, list) else [value])
    return _stable_strings(str(value or "").replace("\\", "/").strip("/") for value in values)


def _stable_strings(values: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value or "").strip()
        if text and text not in seen:
            out.append(text)
            seen.add(text)
    return out


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/classify")
def handle_graph_governance_snapshot_feedback_classify(ctx: RequestContext):
    """Classify semantic open issues into graph/project/reviewer feedback lanes."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import reconcile_feedback

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.classify")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        result = reconcile_feedback.classify_semantic_open_issues(
            project_id,
            snapshot_id,
            source_round=body.get("source_round") or body.get("feedback_round") or "",
            created_by=str(body.get("actor") or body.get("created_by") or "observer"),
            issues=body.get("issues") if isinstance(body.get("issues"), list) else None,
            limit=int(body["limit"]) if body.get("limit") is not None else None,
            node_ids=body.get("node_ids") if isinstance(body.get("node_ids"), list) else None,
        )
        try:
            audit_service.record(
                conn,
                project_id,
                "reconcile_feedback_classified",
                actor=str(body.get("actor") or "observer"),
                details=json.dumps({
                    "snapshot_id": snapshot_id,
                    "created": result.get("created", 0),
                    "updated": result.get("updated", 0),
                    "summary": result.get("summary", {}),
                }, ensure_ascii=False, sort_keys=True),
            )
            conn.commit()
        except Exception:
            pass
        return result
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/retrieval")
def handle_graph_governance_snapshot_feedback_retrieval(ctx: RequestContext):
    """Run bounded read-only graph/grep/excerpt retrieval for a feedback item."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import reconcile_feedback
    from .errors import ValidationError

    root = _graph_governance_project_root(project_id, body)
    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.retrieval")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        feedback_id = str(body.get("feedback_id") or "").strip()
        item = {}
        if feedback_id:
            state = reconcile_feedback.load_feedback_state(project_id, snapshot_id)
            item = dict((state.get("items") or {}).get(feedback_id) or {})
            if not item:
                raise ValidationError(f"feedback item not found: {feedback_id}")
        node_ids = body.get("node_ids") if isinstance(body.get("node_ids"), list) else None
        if not node_ids and item:
            node_ids = item.get("source_node_ids") or item.get("node_ids") or []
        operations = body.get("operations") if isinstance(body.get("operations"), list) else []
        results: list[dict] = []
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            op = str(operation.get("tool") or operation.get("type") or "").strip()
            if op == "graph_query":
                results.append({
                    "tool": op,
                    "result": reconcile_feedback.graph_query_context(
                        project_id,
                        snapshot_id,
                        node_ids=operation.get("node_ids") or node_ids or [],
                        depth=int(operation.get("depth") or 1),
                    ),
                })
            elif op == "grep_in_scope":
                results.append({
                    "tool": op,
                    "result": reconcile_feedback.grep_in_scope(
                        project_id,
                        snapshot_id,
                        project_root=root,
                        pattern=str(operation.get("pattern") or ""),
                        node_ids=operation.get("node_ids") or node_ids or [],
                        paths=operation.get("paths") if isinstance(operation.get("paths"), list) else None,
                        case_sensitive=bool(operation.get("case_sensitive")),
                        regex=bool(operation.get("regex")),
                        max_matches=int(operation.get("max_matches") or 20),
                    ),
                })
            elif op == "read_excerpt":
                results.append({
                    "tool": op,
                    "result": reconcile_feedback.read_project_excerpt(
                        root,
                        str(operation.get("path") or ""),
                        line_start=int(operation.get("line_start") or 1),
                        line_end=(
                            int(operation["line_end"])
                            if operation.get("line_end") is not None
                            else None
                        ),
                    ),
                })
            else:
                raise ValidationError(f"unsupported retrieval tool: {op}")
        if not operations and item:
            results.append({
                "tool": "feedback_retrieval_context",
                "result": reconcile_feedback.build_feedback_retrieval_context(
                    project_id,
                    snapshot_id,
                    item,
                    project_root=root,
                    grep_patterns=(
                        body.get("grep_patterns")
                        if isinstance(body.get("grep_patterns"), list)
                        else None
                    ),
                    max_grep_matches=int(body.get("max_grep_matches") or 12),
                    max_chars=int(body.get("max_context_chars") or 12000),
                ),
            })
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "feedback_id": feedback_id,
            "results": results,
            "count": len(results),
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/status-observations")
def handle_graph_governance_snapshot_feedback_status_observations(ctx: RequestContext):
    """Classify deterministic graph/index drift candidates as status observations."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import reconcile_status_observations

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.status-observations")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        try:
            result = reconcile_status_observations.classify_status_observations(
                conn,
                project_id,
                snapshot_id,
                test_failures=body.get("test_failures") if isinstance(body.get("test_failures"), list) else [],
                actor=str(body.get("actor") or "status-observation-detector"),
                limit=(
                    int(body["limit"])
                    if body.get("limit") is not None
                    else reconcile_status_observations.DEFAULT_LIMIT
                ),
                include_missing_bindings=bool(body.get("include_missing_bindings", True)),
                include_file_state=bool(body.get("include_file_state", True)),
                include_scope_delta=bool(body.get("include_scope_delta", True)),
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        try:
            audit_service.record(
                conn,
                project_id,
                "reconcile_status_observations_classified",
                actor=str(body.get("actor") or "status-observation-detector"),
                details=json.dumps({
                    "snapshot_id": snapshot_id,
                    "classified_count": result.get("detector", {}).get("classified_count", 0),
                }, ensure_ascii=False, sort_keys=True),
            )
        except Exception:
            pass
        conn.commit()
        return result
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/review")
def handle_graph_governance_snapshot_feedback_review(ctx: RequestContext):
    """Review one feedback item and route it toward graph correction or backlog."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    feedback_id = str(body.get("feedback_id") or "").strip()
    if not feedback_id:
        from .errors import ValidationError
        raise ValidationError("feedback_id is required")
    from . import reconcile_feedback

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.review")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        use_reviewer_ai = bool(
            body.get("reviewer_use_ai")
            or body.get("use_reviewer_ai")
            or body.get("semantic_use_ai")
            or body.get("use_ai")
        )
        ai_call = None
        review_project_root = None
        if use_reviewer_ai and not body.get("decision") and not body.get("reviewer_decision"):
            review_project_root = _graph_governance_project_root(project_id, body)
            ai_call = _semantic_ai_call_from_body(project_id, review_project_root, {**body, "snapshot_id": snapshot_id})
        try:
            result = reconcile_feedback.review_feedback_item(
                project_id,
                snapshot_id,
                feedback_id,
                decision=str(body.get("decision") or body.get("reviewer_decision") or ""),
                rationale=str(body.get("rationale") or body.get("reviewer_rationale") or ""),
                confidence=float(body["confidence"]) if body.get("confidence") is not None else None,
                status_observation_category=str(
                    body.get("status_observation_category")
                    or body.get("observation_category")
                    or body.get("category")
                    or ""
                ),
                actor=str(body.get("actor") or body.get("reviewed_by") or "observer"),
                accept=bool(body.get("accept") or body.get("accepted")),
                ai_call=ai_call,
                project_root=review_project_root,
                max_context_chars=int(body.get("review_context_chars") or 6000),
                enable_read_tools=not bool(body.get("disable_read_tools")),
                grep_patterns=(
                    body.get("grep_patterns")
                    if isinstance(body.get("grep_patterns"), list)
                    else None
                ),
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        try:
            audit_service.record(
                conn,
                project_id,
                "reconcile_feedback_reviewed",
                actor=str(body.get("actor") or "observer"),
                details=json.dumps({
                    "snapshot_id": snapshot_id,
                    "feedback_id": feedback_id,
                    "decision": (result.get("items") or [{}])[0].get("reviewer_decision", ""),
                    "status_observation_category": (
                        (result.get("items") or [{}])[0].get("reviewed_status_observation_category", "")
                    ),
                }, ensure_ascii=False, sort_keys=True),
            )
            conn.commit()
        except Exception:
            pass
        return result
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/queue/claim")
def handle_graph_governance_snapshot_feedback_queue_claim(ctx: RequestContext):
    """Claim grouped feedback queue items for one reviewer worker."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import reconcile_feedback
    from .errors import ValidationError

    worker_id = str(body.get("worker_id") or body.get("reviewer_worker_id") or "").strip()
    if not worker_id:
        raise ValidationError("worker_id is required")
    limit_groups = int(body.get("limit_groups") or body.get("group_limit") or body.get("limit") or 1)
    max_items = int(body.get("max_items") or body.get("item_limit") or 25)
    if limit_groups < 0 or max_items < 0:
        raise ValidationError("limit_groups and max_items must be non-negative")

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.queue.claim")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        try:
            result = reconcile_feedback.claim_feedback_review_queue(
                project_id,
                snapshot_id,
                worker_id=worker_id,
                feedback_kind=str(body.get("feedback_kind") or body.get("kind") or ""),
                status=str(body.get("status") or "classified"),
                node_id=str(body.get("node_id") or ""),
                source_round=str(body.get("source_round") or body.get("feedback_round") or ""),
                lane=str(body.get("lane") or ""),
                group_by=str(body.get("group_by") or "feature"),
                include_status_observations=bool(body.get("include_status_observations")),
                include_resolved=bool(body.get("include_resolved")),
                require_current_semantic=bool(
                    body.get("require_current_semantic")
                    or body.get("current_semantics_only")
                    or body.get("semantic_review_ready_only")
                ),
                limit_groups=limit_groups,
                max_items=max_items,
                lease_seconds=int(body.get("lease_seconds") or body.get("claim_lease_seconds") or 1800),
                actor=str(body.get("actor") or worker_id),
                conn=conn,
            )
        except ValueError as exc:
            _raise_graph_api_validation(exc)
        try:
            audit_service.record(
                conn,
                project_id,
                "reconcile_feedback_queue_claimed",
                actor=str(body.get("actor") or worker_id),
                details=json.dumps({
                    "snapshot_id": snapshot_id,
                    "worker_id": worker_id,
                    "claim_id": result.get("claim_id", ""),
                    "claimed_count": result.get("claimed_count", 0),
                    "lane": body.get("lane", ""),
                    "group_by": body.get("group_by", "feature"),
                }, ensure_ascii=False, sort_keys=True),
            )
            conn.commit()
        except Exception:
            pass
        return result
    finally:
        conn.close()


def _semantic_feedback_ai_output_id(evidence: dict[str, Any], worker_evidence: dict[str, Any]) -> str:
    ai_output = worker_evidence.get("ai_output_intake")
    if not isinstance(ai_output, dict):
        ai_output = evidence.get("ai_output_intake")
    if not isinstance(ai_output, dict):
        raw_issue = evidence.get("raw_issue") if isinstance(evidence.get("raw_issue"), dict) else {}
        raw_evidence = raw_issue.get("evidence") if isinstance(raw_issue.get("evidence"), dict) else {}
        ai_output = raw_evidence.get("ai_output_intake")
    if not isinstance(ai_output, dict):
        return ""
    return str(ai_output.get("output_id") or "").strip()


_SEMANTIC_REVISION_FORBIDDEN_KEYS = {
    "graph_structure_ops",
    "graph_structure_suggestions",
    "graph_structure_candidates",
    "graph_enrich_config_ops",
    "graph_enrich_config_suggestions",
    "graph_enrich_config_candidates",
}


def _semantic_revision_nonempty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _semantic_revision_forbidden_key(payload: dict[str, Any]) -> str:
    candidates = [payload]
    nested = payload.get("semantic_payload")
    if isinstance(nested, dict):
        candidates.append(nested)
    for candidate in candidates:
        for key in sorted(_SEMANTIC_REVISION_FORBIDDEN_KEYS):
            if _semantic_revision_nonempty(candidate.get(key)):
                return key
    return ""


def _semantic_revision_payload_from_body_item(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict) and isinstance(raw.get("semantic_payload"), dict):
        raw = raw.get("semantic_payload")
    if not isinstance(raw, dict):
        raise ValueError("revised semantic payload must be a JSON object")
    try:
        return json.loads(json.dumps(raw, ensure_ascii=False, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 - validation should surface as 400.
        raise ValueError(f"revised semantic payload must be JSON-serializable: {exc}") from exc


def _semantic_revision_payloads_from_body(
    body: dict[str, Any],
    feedback_ids: list[str],
) -> dict[str, dict[str, Any]]:
    ids = [str(item or "").strip() for item in feedback_ids if str(item or "").strip()]
    revisions = body.get("semantic_revisions")
    if revisions is None:
        revisions = body.get("revisions")
    if revisions is not None:
        if not isinstance(revisions, dict):
            raise ValueError("semantic_revisions must be a feedback_id keyed object")
        payloads: dict[str, dict[str, Any]] = {}
        for fid in ids:
            if fid not in revisions:
                raise ValueError(f"missing semantic revision payload for feedback_id: {fid}")
            payloads[fid] = _semantic_revision_payload_from_body_item(revisions.get(fid))
        return payloads
    if len(ids) != 1:
        raise ValueError("revised_semantic_payload is only valid for a single feedback_id")
    raw_payload = body.get("revised_semantic_payload")
    if raw_payload is None:
        raw_payload = body.get("semantic_payload")
    return {ids[0]: _semantic_revision_payload_from_body_item(raw_payload)}


def _semantic_event_ids_for_feedback_item(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    raw_issue = evidence.get("raw_issue") if isinstance(evidence.get("raw_issue"), dict) else {}
    worker_evidence = (
        raw_issue.get("evidence") if isinstance(raw_issue.get("evidence"), dict) else {}
    )
    event_ids = worker_evidence.get("linked_event_ids") or evidence.get("linked_event_ids") or []
    if not isinstance(event_ids, list):
        event_ids = [event_ids]
    return evidence, worker_evidence, [str(eid or "").strip() for eid in event_ids if str(eid or "").strip()]


def _semantic_revision_event_id(snapshot_id: str, node_id: str, payload_hash: str) -> str:
    raw = f"semnode-revised-{snapshot_id}-{node_id}-{payload_hash[:12]}"
    return re.sub(r"[^A-Za-z0-9_.:-]+", "-", raw).strip("-")[:220]


def _plan_semantic_enrichment_revisions(
    conn,
    project_id: str,
    snapshot_id: str,
    feedback_ids: list[str],
    revision_payloads: dict[str, dict[str, Any]],
    *,
    rationale: str = "",
) -> list[dict[str, Any]]:
    from . import graph_events
    from . import graph_snapshot_store as store
    from . import reconcile_feedback

    items = reconcile_feedback.list_feedback_items(project_id, snapshot_id)
    by_id = {str(item.get("feedback_id") or ""): item for item in items}
    nodes = store.list_graph_snapshot_nodes(
        conn,
        project_id,
        snapshot_id,
        include_semantic=False,
        limit=1000,
    )
    nodes_by_id = {str(node.get("node_id") or node.get("id") or ""): node for node in nodes}
    plan: list[dict[str, Any]] = []
    for fid in feedback_ids:
        item = by_id.get(fid)
        if not item:
            raise ValueError(f"feedback_not_found: {fid}")
        revised_payload = revision_payloads.get(fid)
        if not isinstance(revised_payload, dict):
            raise ValueError(f"missing revised semantic payload for feedback_id: {fid}")
        forbidden = _semantic_revision_forbidden_key(revised_payload)
        if forbidden:
            raise ValueError(
                f"semantic revision cannot include {forbidden}; use the graph correction/config gate"
            )
        evidence, worker_evidence, event_ids = _semantic_event_ids_for_feedback_item(item)
        semantic_events: list[dict[str, Any]] = []
        for event_id in event_ids:
            event = graph_events.get_event(conn, project_id, snapshot_id, event_id)
            if event and str(event.get("event_type") or "") in {"semantic_node_enriched", "edge_semantic_enriched"}:
                semantic_events.append(event)
        if not semantic_events:
            raise ValueError(f"no linked semantic event found for feedback_id: {fid}")
        if len(semantic_events) > 1:
            raise ValueError(f"ambiguous linked semantic events for feedback_id: {fid}")
        event = semantic_events[0]
        if str(event.get("event_type") or "") != "semantic_node_enriched":
            raise ValueError("revise_semantic_enrichment currently supports node semantic events only")
        node_id = (
            str(event.get("target_id") or "").strip()
            or str(item.get("target_id") or "").strip()
            or str(worker_evidence.get("node_id") or "").strip()
        )
        if not node_id:
            raise ValueError(f"missing node_id for feedback_id: {fid}")
        node = nodes_by_id.get(node_id)
        if not node:
            raise ValueError(f"node_not_found for semantic revision: {node_id}")
        stable_key = graph_events.stable_node_key_for_node(node)
        event_stable_key = str(event.get("stable_node_key") or "").strip()
        if event_stable_key and stable_key and event_stable_key != stable_key:
            raise ValueError(f"stable_node_key_mismatch for semantic revision: {node_id}")
        current_feature_hash = graph_events.feature_hash_for_node(node)
        event_feature_hash = str(event.get("feature_hash") or "").strip()
        if event_feature_hash and current_feature_hash and event_feature_hash != current_feature_hash:
            raise ValueError(f"feature_hash_mismatch for semantic revision: {node_id}")
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        current_file_hashes = (
            metadata.get("file_hashes")
            if isinstance(metadata.get("file_hashes"), dict)
            else {}
        )
        event_file_hashes = event.get("file_hashes") if isinstance(event.get("file_hashes"), dict) else {}
        if event_file_hashes and current_file_hashes and event_file_hashes != current_file_hashes:
            raise ValueError(f"file_hash_mismatch for semantic revision: {node_id}")
        payload_hash = hashlib.sha256(
            json.dumps(revised_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        plan.append({
            "feedback_id": fid,
            "feedback_item": item,
            "evidence": evidence,
            "worker_evidence": worker_evidence,
            "event": event,
            "node": node,
            "node_id": node_id,
            "stable_node_key": stable_key,
            "feature_hash": event_feature_hash or current_feature_hash,
            "file_hashes": event_file_hashes or current_file_hashes or {},
            "revised_payload": revised_payload,
            "payload_hash": f"sha256:{payload_hash}",
            "revision_event_id": _semantic_revision_event_id(snapshot_id, node_id, payload_hash),
            "rationale": rationale,
        })
    return plan


def _apply_semantic_enrichment_revision_plan(
    conn,
    project_id: str,
    snapshot_id: str,
    revision_plan: list[dict[str, Any]],
    *,
    actor: str = "observer",
) -> dict[str, Any]:
    from . import ai_output_intake
    from . import graph_events

    revised_node_ids: list[str] = []
    event_ids_created: list[str] = []
    event_ids_superseded: list[str] = []
    ai_output_ids_marked_completed: list[str] = []
    errors: list[dict[str, Any]] = []
    for entry in revision_plan:
        fid = str(entry.get("feedback_id") or "")
        event = entry.get("event") if isinstance(entry.get("event"), dict) else {}
        event_id = str(event.get("event_id") or "")
        node_id = str(entry.get("node_id") or "")
        revised_payload = entry.get("revised_payload") if isinstance(entry.get("revised_payload"), dict) else {}
        payload_hash = str(entry.get("payload_hash") or "")
        revision_event_id = str(entry.get("revision_event_id") or "")
        rationale = str(entry.get("rationale") or "")
        try:
            event_payload = dict(event.get("payload") if isinstance(event.get("payload"), dict) else {})
            event_payload["semantic_payload"] = revised_payload
            event_payload["semantic_status"] = "ai_complete"
            event_payload["review_revision"] = {
                "feedback_id": fid,
                "supersedes_event_id": event_id,
                "rationale": rationale,
                "actor": actor,
                "revised_at": _utc_now(),
            }
            created = graph_events.create_event(
                conn,
                project_id,
                snapshot_id,
                event_id=revision_event_id,
                event_type="semantic_node_enriched",
                event_kind="observer_semantic_revision",
                target_type="node",
                target_id=node_id,
                status=graph_events.EVENT_STATUS_ACCEPTED,
                risk_level=str(event.get("risk_level") or "low"),
                confidence=float(event.get("confidence") or 0.0),
                baseline_commit=str(event.get("baseline_commit") or ""),
                target_commit=str(event.get("target_commit") or ""),
                branch_ref=str(event.get("branch_ref") or ""),
                operation_type="review_revision",
                source_branch_ref=str(event.get("branch_ref") or ""),
                source_snapshot_id=str(event.get("snapshot_id") or snapshot_id),
                source_event_id=event_id,
                payload_hash=payload_hash,
                stable_node_key=str(entry.get("stable_node_key") or ""),
                feature_hash=str(entry.get("feature_hash") or ""),
                file_hashes=entry.get("file_hashes") if isinstance(entry.get("file_hashes"), dict) else {},
                payload=event_payload,
                evidence={
                    "source": "revise_semantic_enrichment",
                    "feedback_id": fid,
                    "supersedes_event_id": event_id,
                    "rationale": rationale,
                },
                created_by=actor,
            )
            event_ids_created.append(str(created.get("event_id") or revision_event_id))
            graph_events.update_event_status(
                conn,
                project_id,
                snapshot_id,
                event_id,
                status=graph_events.EVENT_STATUS_REJECTED,
                actor=actor,
                operation_type="supersede",
                evidence={
                    "source": "revise_semantic_enrichment",
                    "feedback_id": fid,
                    "superseded_by_event_id": revision_event_id,
                },
            )
            event_ids_superseded.append(event_id)
            now = _utc_now()
            conn.execute(
                """
                INSERT INTO graph_semantic_nodes
                  (project_id, snapshot_id, node_id, status, feature_hash,
                   file_hashes_json, semantic_json, branch_ref, operation_type,
                   source_branch_ref, source_snapshot_id, source_event_id, payload_hash,
                   feedback_round, batch_index, updated_at)
                VALUES (?, ?, ?, 'ai_complete', ?, ?, ?, ?, 'review_revision',
                        ?, ?, ?, ?, 0, NULL, ?)
                ON CONFLICT(project_id, snapshot_id, node_id) DO UPDATE SET
                  status = excluded.status,
                  feature_hash = excluded.feature_hash,
                  file_hashes_json = excluded.file_hashes_json,
                  semantic_json = excluded.semantic_json,
                  branch_ref = excluded.branch_ref,
                  operation_type = excluded.operation_type,
                  source_branch_ref = excluded.source_branch_ref,
                  source_snapshot_id = excluded.source_snapshot_id,
                  source_event_id = excluded.source_event_id,
                  payload_hash = excluded.payload_hash,
                  feedback_round = excluded.feedback_round,
                  batch_index = excluded.batch_index,
                  updated_at = excluded.updated_at
                """,
                (
                    project_id,
                    snapshot_id,
                    node_id,
                    str(entry.get("feature_hash") or ""),
                    json.dumps(entry.get("file_hashes") or {}, ensure_ascii=False, sort_keys=True),
                    json.dumps(revised_payload, ensure_ascii=False, sort_keys=True),
                    str(event.get("branch_ref") or ""),
                    str(event.get("branch_ref") or ""),
                    str(event.get("snapshot_id") or snapshot_id),
                    event_id,
                    payload_hash,
                    now,
                ),
            )
            revised_node_ids.append(node_id)
            ai_output_id = _semantic_feedback_ai_output_id(
                entry.get("evidence") if isinstance(entry.get("evidence"), dict) else {},
                entry.get("worker_evidence") if isinstance(entry.get("worker_evidence"), dict) else {},
            )
            if ai_output_id:
                marked = ai_output_intake.mark_ai_output_route_status(
                    conn,
                    project_id,
                    ai_output_id,
                    "completed",
                    actor=actor,
                )
                if marked.get("ok"):
                    ai_output_ids_marked_completed.append(ai_output_id)
                else:
                    errors.append({
                        "feedback_id": fid,
                        "output_id": ai_output_id,
                        "error": marked.get("error") or "ai_output_route_update_failed",
                    })
        except Exception as exc:  # noqa: BLE001 - keep batch result inspectable.
            errors.append({"feedback_id": fid, "event_id": event_id, "error": str(exc)})
    conn.commit()
    return {
        "node_ids_revised": revised_node_ids,
        "event_ids_created": event_ids_created,
        "event_ids_superseded": event_ids_superseded,
        "ai_output_ids_marked_completed": ai_output_ids_marked_completed,
        "errors": errors,
    }


def _accept_semantic_enrichment_for_feedback_items(
    conn,
    project_id: str,
    snapshot_id: str,
    feedback_ids: list[str],
    *,
    actor: str = "observer",
) -> dict[str, Any]:
    """MF-2026-05-10-016 helper: promote worker-produced semantic from
    pending_review to ai_complete and flip the linked PROPOSED event(s)
    to ACCEPTED so the projection picks them up.

    Reads each feedback item's evidence to find linked event_id + node_id,
    then performs two writes:
      1. graph_semantic_nodes.status: pending_review → ai_complete
      2. graph_events.status: proposed → accepted

    Returns a summary dict with `node_ids_flipped`, `event_ids_flipped`,
    `errors`. Caller is responsible for triggering a projection rebuild.
    """
    from . import ai_output_intake
    from . import graph_events
    from . import reconcile_feedback

    items = reconcile_feedback.list_feedback_items(project_id, snapshot_id)
    by_id = {str(item.get("feedback_id") or ""): item for item in items}
    node_ids_flipped: list[str] = []
    edge_ids_flipped: list[str] = []
    event_ids_flipped: list[str] = []
    ai_output_ids_marked_completed: list[str] = []
    errors: list[dict[str, Any]] = []
    for fid in feedback_ids:
        item = by_id.get(fid)
        if not item:
            errors.append({"feedback_id": fid, "error": "feedback_not_found"})
            continue
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        # reconcile_feedback.normalize_open_issue wraps caller-supplied issue
        # under evidence.raw_issue (with the original issue dict intact). The
        # worker stores linked_event_ids inside the caller-supplied
        # issue.evidence, so they end up at evidence.raw_issue.evidence.
        raw_issue = evidence.get("raw_issue") if isinstance(evidence.get("raw_issue"), dict) else {}
        worker_evidence = (
            raw_issue.get("evidence") if isinstance(raw_issue.get("evidence"), dict) else {}
        )
        target_type_hint = str(
            item.get("target_type")
            or raw_issue.get("target_type")
            or ""
        ).strip().lower()
        target_id = (
            str(item.get("target_id") or "").strip()
            or str(worker_evidence.get("node_id") or "").strip()
            or str(worker_evidence.get("edge_id") or "").strip()
            or str(evidence.get("node_id") or "").strip()
        )
        event_ids = (
            worker_evidence.get("linked_event_ids")
            or evidence.get("linked_event_ids")
            or []
        )
        if not isinstance(event_ids, list):
            event_ids = [event_ids]
        ai_output_id = _semantic_feedback_ai_output_id(evidence, worker_evidence)
        if ai_output_id:
            try:
                marked = ai_output_intake.mark_ai_output_route_status(
                    conn,
                    project_id,
                    ai_output_id,
                    "completed",
                    actor=actor,
                )
                if marked.get("ok"):
                    ai_output_ids_marked_completed.append(ai_output_id)
                else:
                    errors.append({
                        "feedback_id": fid,
                        "output_id": ai_output_id,
                        "error": marked.get("error") or "ai_output_route_update_failed",
                    })
            except Exception as exc:  # noqa: BLE001 - advisory
                errors.append({"feedback_id": fid, "output_id": ai_output_id, "error": str(exc)})
        if not target_id and not event_ids:
            errors.append({"feedback_id": fid, "error": "missing_target_id_and_event_ids"})
            continue
        # MF-2026-05-10-017: try the persistent-layer UPDATE for nodes; the
        # UPDATE no-ops cleanly for edge targets (no row matches). Use rowcount
        # to decide which bucket the target landed in. Operator-supplied
        # target_type_hint is used as a fallback when rowcount is 0 to
        # distinguish a fresh edge (correctly bucketed) from a stale node
        # whose pending_review row was already flipped by an earlier accept.
        if target_id:
            try:
                if target_type_hint == "edge":
                    cur = conn.execute(
                        """
                        UPDATE graph_semantic_edges
                        SET status = 'ai_complete'
                        WHERE project_id = ? AND snapshot_id = ? AND edge_id = ?
                          AND status = 'pending_review'
                        """,
                        (project_id, snapshot_id, target_id),
                    )
                    if cur.rowcount > 0:
                        edge_ids_flipped.append(target_id)
                    else:
                        edge_ids_flipped.append(target_id)
                else:
                    cur = conn.execute(
                        """
                        UPDATE graph_semantic_nodes
                        SET status = 'ai_complete'
                        WHERE project_id = ? AND snapshot_id = ? AND node_id = ?
                          AND status = 'pending_review'
                        """,
                        (project_id, snapshot_id, target_id),
                    )
                    if cur.rowcount > 0:
                        node_ids_flipped.append(target_id)
                    else:
                        # Best-effort default — assume node target so callers that
                        # don't tag target_type don't lose accounting.
                        node_ids_flipped.append(target_id)
            except Exception as exc:  # noqa: BLE001 - advisory; record + keep going
                errors.append({"feedback_id": fid, "target_id": target_id, "error": str(exc)})
        for eid in event_ids:
            eid_s = str(eid or "").strip()
            if not eid_s:
                continue
            try:
                graph_events.update_event_status(
                    conn, project_id, snapshot_id, eid_s,
                    status=graph_events.EVENT_STATUS_ACCEPTED,
                    actor=actor,
                    operation_type="accept",
                    evidence={
                        "source": "accept_semantic_enrichment",
                        "feedback_id": fid,
                        "target_type": target_type_hint or "node",
                    },
                )
                event_ids_flipped.append(eid_s)
            except Exception as exc:  # noqa: BLE001 - advisory
                errors.append({"feedback_id": fid, "event_id": eid_s, "error": str(exc)})
    conn.commit()
    return {
        "node_ids_flipped": node_ids_flipped,
        "edge_ids_flipped": edge_ids_flipped,
        "event_ids_flipped": event_ids_flipped,
        "ai_output_ids_marked_completed": ai_output_ids_marked_completed,
        "errors": errors,
    }


def _reject_semantic_enrichment_for_feedback_items(
    conn,
    project_id: str,
    snapshot_id: str,
    feedback_ids: list[str],
    *,
    actor: str = "observer",
) -> dict[str, Any]:
    """Reject worker-produced semantic proposals linked to feedback items.

    Feedback review state alone is not enough: pending semantic rows are
    serialized by the node API, and proposed graph events can be backfilled
    into future projections. Rejecting the review must therefore also reject
    linked semantic events and clear the pending persistent cache row.
    """
    from . import ai_output_intake
    from . import graph_events
    from . import reconcile_feedback

    items = reconcile_feedback.list_feedback_items(project_id, snapshot_id)
    by_id = {str(item.get("feedback_id") or ""): item for item in items}
    node_ids_cleared: list[str] = []
    edge_ids_cleared: list[str] = []
    event_ids_rejected: list[str] = []
    job_ids_marked_rejected: list[str] = []
    ai_output_ids_marked_rejected: list[str] = []
    errors: list[dict[str, Any]] = []
    semantic_event_types = {"semantic_node_enriched", "edge_semantic_enriched"}
    for fid in feedback_ids:
        item = by_id.get(fid)
        if not item:
            errors.append({"feedback_id": fid, "error": "feedback_not_found"})
            continue
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        raw_issue = evidence.get("raw_issue") if isinstance(evidence.get("raw_issue"), dict) else {}
        worker_evidence = (
            raw_issue.get("evidence") if isinstance(raw_issue.get("evidence"), dict) else {}
        )
        target_type_hint = str(
            item.get("target_type")
            or raw_issue.get("target_type")
            or ""
        ).strip().lower()
        target_id = (
            str(item.get("target_id") or "").strip()
            or str(worker_evidence.get("node_id") or "").strip()
            or str(worker_evidence.get("edge_id") or "").strip()
            or str(evidence.get("node_id") or "").strip()
        )
        event_ids = (
            worker_evidence.get("linked_event_ids")
            or evidence.get("linked_event_ids")
            or []
        )
        if not isinstance(event_ids, list):
            event_ids = [event_ids]
        ai_output_id = _semantic_feedback_ai_output_id(evidence, worker_evidence)
        if ai_output_id:
            try:
                marked = ai_output_intake.mark_ai_output_route_status(
                    conn,
                    project_id,
                    ai_output_id,
                    "rejected",
                    actor=actor,
                )
                if marked.get("ok"):
                    ai_output_ids_marked_rejected.append(ai_output_id)
                else:
                    errors.append({
                        "feedback_id": fid,
                        "output_id": ai_output_id,
                        "error": marked.get("error") or "ai_output_route_update_failed",
                    })
            except Exception as exc:  # noqa: BLE001 - advisory
                errors.append({"feedback_id": fid, "output_id": ai_output_id, "error": str(exc)})
        linked_events: list[dict[str, Any]] = []
        for eid in event_ids:
            eid_s = str(eid or "").strip()
            if not eid_s:
                continue
            try:
                event = graph_events.get_event(conn, project_id, snapshot_id, eid_s)
                if not event:
                    errors.append({"feedback_id": fid, "event_id": eid_s, "error": "event_not_found"})
                    continue
                if str(event.get("event_type") or "") not in semantic_event_types:
                    continue
                updated = graph_events.update_event_status(
                    conn, project_id, snapshot_id, eid_s,
                    status=graph_events.EVENT_STATUS_REJECTED,
                    actor=actor,
                    operation_type="reject",
                    evidence={
                        "source": "reject_semantic_enrichment",
                        "feedback_id": fid,
                        "target_type": target_type_hint or str(event.get("target_type") or ""),
                    },
                )
                linked_events.append(updated)
                event_ids_rejected.append(eid_s)
            except Exception as exc:  # noqa: BLE001 - advisory
                errors.append({"feedback_id": fid, "event_id": eid_s, "error": str(exc)})

        node_event = next(
            (ev for ev in linked_events if str(ev.get("event_type") or "") == "semantic_node_enriched"),
            None,
        )
        edge_event = next(
            (ev for ev in linked_events if str(ev.get("event_type") or "") == "edge_semantic_enriched"),
            None,
        )
        if node_event:
            node_id = target_id or str(node_event.get("target_id") or "")
            feature_hash = str(node_event.get("feature_hash") or "")
            if node_id:
                cur = conn.execute(
                    """
                    DELETE FROM graph_semantic_nodes
                    WHERE project_id = ? AND snapshot_id = ? AND node_id = ?
                      AND status = 'pending_review'
                      AND (? = '' OR feature_hash = ?)
                    """,
                    (project_id, snapshot_id, node_id, feature_hash, feature_hash),
                )
                if cur.rowcount > 0:
                    node_ids_cleared.append(node_id)
                job_cur = conn.execute(
                    """
                    UPDATE graph_semantic_jobs
                    SET status = 'rejected', worker_id = '', claim_id = '',
                        claimed_at = '', lease_expires_at = '', updated_at = ?
                    WHERE project_id = ? AND snapshot_id = ? AND node_id = ?
                      AND status IN ('pending_ai', 'ai_pending', 'running', 'ai_running', 'ai_complete')
                    """,
                    (_utc_now(), project_id, snapshot_id, node_id),
                )
                if job_cur.rowcount > 0:
                    job_ids_marked_rejected.append(node_id)
        elif edge_event:
            edge_id = target_id or str(edge_event.get("target_id") or "")
            edge_hash = str(edge_event.get("feature_hash") or "")
            if edge_id:
                cur = conn.execute(
                    """
                    DELETE FROM graph_semantic_edges
                    WHERE project_id = ? AND snapshot_id = ? AND edge_id = ?
                      AND status = 'pending_review'
                      AND (? = '' OR edge_signature_hash = ?)
                    """,
                    (project_id, snapshot_id, edge_id, edge_hash, edge_hash),
                )
                if cur.rowcount > 0:
                    edge_ids_cleared.append(edge_id)
    conn.commit()
    return {
        "node_ids_cleared": node_ids_cleared,
        "edge_ids_cleared": edge_ids_cleared,
        "event_ids_rejected": event_ids_rejected,
        "job_ids_marked_rejected": job_ids_marked_rejected,
        "ai_output_ids_marked_rejected": ai_output_ids_marked_rejected,
        "errors": errors,
    }


def _successful_feedback_decision_ids(result: dict[str, Any]) -> list[str]:
    """Feedback side effects are allowed only for rows the decision step updated."""
    ids: list[str] = []
    for item in result.get("items") or []:
        if not isinstance(item, dict):
            continue
        feedback_id = str(item.get("feedback_id") or "").strip()
        if feedback_id:
            ids.append(feedback_id)
    return ids


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/decision")
def handle_graph_governance_snapshot_feedback_decision(ctx: RequestContext):
    """Apply explicit user/observer decisions to feedback items."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import reconcile_feedback
    from .errors import ValidationError

    raw_ids = body.get("feedback_ids")
    if raw_ids is None:
        raw_ids = [body.get("feedback_id")]
    if isinstance(raw_ids, str):
        feedback_ids = [raw_ids]
    elif isinstance(raw_ids, list):
        feedback_ids = [str(item or "").strip() for item in raw_ids]
    else:
        raise ValidationError("feedback_ids must be a string or list")
    action = str(body.get("action") or body.get("decision_action") or "").strip()
    if not action:
        raise ValidationError("action is required")

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.decision")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        try:
            revision_plan: list[dict[str, Any]] = []
            if action == "revise_semantic_enrichment":
                revision_payloads = _semantic_revision_payloads_from_body(body, feedback_ids)
                revision_plan = _plan_semantic_enrichment_revisions(
                    conn,
                    project_id,
                    snapshot_id,
                    feedback_ids,
                    revision_payloads,
                    rationale=str(body.get("rationale") or body.get("reviewer_rationale") or ""),
                )
            result = reconcile_feedback.decide_feedback_items(
                project_id,
                snapshot_id,
                feedback_ids,
                action=action,
                actor=str(body.get("actor") or "observer"),
                rationale=str(body.get("rationale") or body.get("reviewer_rationale") or ""),
                decision=str(body.get("decision") or body.get("reviewer_decision") or ""),
                status_observation_category=str(
                    body.get("status_observation_category")
                    or body.get("observation_category")
                    or body.get("category")
                    or ""
                ),
                accept=(
                    bool(body.get("accept") or body.get("accepted"))
                    if body.get("accept") is not None or body.get("accepted") is not None
                    else None
                ),
            )
            successful_feedback_ids = _successful_feedback_decision_ids(result)
            if action == "accept_graph_correction" and body.get("create_patch", True):
                from . import graph_snapshot_store as store

                snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id) or {}
                if successful_feedback_ids:
                    result["graph_patches"] = reconcile_feedback.promote_feedback_items_to_graph_patches(
                        conn,
                        project_id,
                        snapshot_id,
                        successful_feedback_ids,
                        actor=str(body.get("actor") or "observer"),
                        accept_patch=True,
                        allow_high_risk_accept=bool(body.get("allow_high_risk_patch_accept")),
                        base_commit=str(body.get("base_commit") or snapshot.get("commit_sha") or ""),
                    )
                else:
                    result["graph_patches"] = {
                        "ok": False,
                        "requested_count": 0,
                        "created_count": 0,
                        "error_count": 0,
                        "patches": [],
                        "errors": [],
                        "skipped_reason": "no_successful_feedback_decisions",
                    }
            elif action == "accept_semantic_enrichment":
                # MF-2026-05-10-016: promote worker-produced semantic from
                # pending_review (PROPOSED event) to ai_complete (ACCEPTED
                # event). The persistent layer flip is what
                # `backfill_existing_semantic_events` will read on the next
                # projection rebuild; we also flip live event status now and
                # rebuild projection so dashboard reflects the change without
                # waiting for the next reconcile.
                from . import graph_events
                accepted = _accept_semantic_enrichment_for_feedback_items(
                    conn,
                    project_id,
                    snapshot_id,
                    successful_feedback_ids,
                    actor=str(body.get("actor") or "observer"),
                )
                result["semantic_enrichment_accepted"] = accepted
                if accepted.get("event_ids_flipped"):
                    try:
                        graph_events.build_semantic_projection(
                            conn, project_id, snapshot_id,
                            actor=str(body.get("actor") or "observer"),
                        )
                        result["projection_rebuilt"] = True
                    except Exception as exc:  # noqa: BLE001 - advisory
                        result["projection_rebuilt"] = False
                        result["projection_rebuild_error"] = str(exc)
            elif action == "revise_semantic_enrichment":
                from . import graph_events

                successful = set(successful_feedback_ids)
                successful_plan = [
                    item for item in revision_plan
                    if str(item.get("feedback_id") or "") in successful
                ]
                revised = _apply_semantic_enrichment_revision_plan(
                    conn,
                    project_id,
                    snapshot_id,
                    successful_plan,
                    actor=str(body.get("actor") or "observer"),
                )
                result["semantic_enrichment_revised"] = revised
                if revised.get("errors"):
                    result["ok"] = False
                    result["error_count"] = int(result.get("error_count") or 0) + len(revised.get("errors") or [])
                    result.setdefault("errors", [])
                    if isinstance(result["errors"], list):
                        result["errors"].extend(revised.get("errors") or [])
                if revised.get("event_ids_created"):
                    try:
                        graph_events.build_semantic_projection(
                            conn, project_id, snapshot_id,
                            actor=str(body.get("actor") or "observer"),
                        )
                        result["projection_rebuilt"] = True
                    except Exception as exc:  # noqa: BLE001 - advisory
                        result["projection_rebuilt"] = False
                        result["projection_rebuild_error"] = str(exc)
            elif action == "reject_false_positive":
                from . import graph_events
                rejected = _reject_semantic_enrichment_for_feedback_items(
                    conn,
                    project_id,
                    snapshot_id,
                    successful_feedback_ids,
                    actor=str(body.get("actor") or "observer"),
                )
                result["semantic_enrichment_rejected"] = rejected
                if (
                    rejected.get("event_ids_rejected")
                    or rejected.get("node_ids_cleared")
                    or rejected.get("edge_ids_cleared")
                ):
                    try:
                        graph_events.build_semantic_projection(
                            conn, project_id, snapshot_id,
                            actor=str(body.get("actor") or "observer"),
                        )
                        result["projection_rebuilt"] = True
                    except Exception as exc:  # noqa: BLE001 - advisory
                        result["projection_rebuilt"] = False
                        result["projection_rebuild_error"] = str(exc)
        except ValueError as exc:
            _raise_graph_api_validation(exc)
        try:
            audit_service.record(
                conn,
                project_id,
                "reconcile_feedback_decision",
                actor=str(body.get("actor") or "observer"),
                details=json.dumps({
                    "snapshot_id": snapshot_id,
                    "feedback_ids": feedback_ids,
                    "action": action,
                    "decided_count": result.get("decided_count", 0),
                    "error_count": result.get("error_count", 0),
                }, ensure_ascii=False, sort_keys=True),
            )
            conn.commit()
        except Exception:
            pass
        return result
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/cancel")
def handle_graph_governance_snapshot_feedback_cancel(ctx: RequestContext):
    """Soft-cancel feedback review rows by keeping them as status observations."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body if isinstance(ctx.body, dict) else {}
    from . import reconcile_feedback
    from .errors import ValidationError

    raw_ids = body.get("feedback_ids")
    if raw_ids is None and body.get("feedback_id") is not None:
        raw_ids = [body.get("feedback_id")]
    if isinstance(raw_ids, str):
        feedback_ids = [raw_ids]
    elif isinstance(raw_ids, list):
        feedback_ids = [str(item or "").strip() for item in raw_ids if str(item or "").strip()]
    elif raw_ids is None:
        feedback_ids = []
    else:
        raise ValidationError("feedback_ids must be a string or list")

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.cancel")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        if raw_ids is None:
            terminal = {"reviewed", "accepted", "rejected", "backlog_filed"}
            feedback_ids = [
                str(item.get("feedback_id") or "")
                for item in reconcile_feedback.list_feedback_items(
                    project_id,
                    snapshot_id,
                    limit=int(body.get("limit") or 1000),
                )
                if str(item.get("status") or "") not in terminal
            ]
        if not feedback_ids:
            return {
                "ok": True,
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "status": "soft_cancelled",
                "cancelled_count": 0,
                "skipped_terminal": 0,
                "items": [],
                "summary": reconcile_feedback.feedback_summary(project_id, snapshot_id),
            }
        result = reconcile_feedback.decide_feedback_items(
            project_id,
            snapshot_id,
            feedback_ids,
            action="keep_status_observation",
            actor=str(body.get("actor") or body.get("created_by") or "dashboard_user"),
            rationale=str(body.get("rationale") or "Dashboard soft-cancelled feedback review."),
            status_observation_category=str(body.get("status_observation_category") or ""),
        )
        return {
            "ok": bool(result.get("ok")),
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "status": "soft_cancelled",
            "cancelled_count": int(result.get("decided_count") or 0),
            "skipped_terminal": 0,
            "feedback_cancel_contract": "keep_status_observation",
            **result,
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/graph-patches")
def handle_graph_governance_snapshot_feedback_graph_patches(ctx: RequestContext):
    """Promote feedback items into graph correction patch rows."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import graph_snapshot_store as store
    from . import reconcile_feedback
    from .errors import ValidationError

    raw_ids = body.get("feedback_ids")
    if raw_ids is None:
        raw_ids = [body.get("feedback_id")]
    if isinstance(raw_ids, str):
        feedback_ids = [raw_ids]
    elif isinstance(raw_ids, list):
        feedback_ids = [str(item or "").strip() for item in raw_ids]
    else:
        raise ValidationError("feedback_ids must be a string or list")

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.graph-patches")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        snapshot = store.get_graph_snapshot(conn, project_id, snapshot_id) or {}
        try:
            result = reconcile_feedback.promote_feedback_items_to_graph_patches(
                conn,
                project_id,
                snapshot_id,
                feedback_ids,
                actor=str(body.get("actor") or "observer"),
                accept_patch=bool(body.get("accept_patch") or body.get("accept")),
                allow_high_risk_accept=bool(body.get("allow_high_risk_patch_accept")),
                base_commit=str(body.get("base_commit") or snapshot.get("commit_sha") or ""),
            )
        except ValueError as exc:
            _raise_graph_api_validation(exc)
        try:
            audit_service.record(
                conn,
                project_id,
                "reconcile_feedback_graph_patch_promoted",
                actor=str(body.get("actor") or "observer"),
                details=json.dumps({
                    "snapshot_id": snapshot_id,
                    "feedback_ids": feedback_ids,
                    "created_count": result.get("created_count", 0),
                    "error_count": result.get("error_count", 0),
                    "accept_patch": bool(body.get("accept_patch") or body.get("accept")),
                }, ensure_ascii=False, sort_keys=True),
            )
        except Exception:
            pass
        conn.commit()
        return {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/graph-structure/cancel")
def handle_graph_governance_snapshot_feedback_graph_structure_cancel(ctx: RequestContext):
    """Cancel a Review Queue graph-structure operation by discarding only its files."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import reconcile_feedback
    from .errors import ValidationError

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.graph-structure.cancel")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        root = _graph_governance_project_root(project_id, body)
        feedback_ids = _feedback_ids_from_body(body)
        lifecycle = _graph_structure_lifecycle_for_feedback_ids(project_id, snapshot_id, feedback_ids)
        files = _operation_files_from_body_or_lifecycle(body, lifecycle)
        if not files:
            raise ValidationError("graph-structure operation has no file changes to cancel")
        guard = _graph_structure_dirty_guard(root, files)
        if guard["blocked"]:
            return 409, {
                "ok": False,
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "status": "blocked_dirty_overlap",
                "operation_type": "graph_structure",
                "feedback_ids": feedback_ids,
                "changed_files": files,
                "dirty_guard": guard,
                "message": "Cancel refused because the operation files include dirty state that cannot be safely discarded file-by-file.",
            }
        discarded = _git_discard_files(root, files)
        decision = reconcile_feedback.decide_feedback_items(
            project_id,
            snapshot_id,
            feedback_ids,
            action="reject_false_positive",
            actor=str(body.get("actor") or "dashboard_user"),
            rationale=str(body.get("reason") or body.get("rationale") or "Cancelled graph-structure file operation."),
        )
        conn.commit()
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "status": "cancelled",
            "operation_type": "graph_structure",
            "feedback_ids": feedback_ids,
            "changed_files": files,
            "discarded_files": discarded,
            "dirty_guard": guard,
            "decision": decision,
            "message": "Graph-structure operation cancelled; only the operation files were discarded.",
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/graph-structure/commit")
def handle_graph_governance_snapshot_feedback_graph_structure_commit(ctx: RequestContext):
    """Commit/apply Review Queue graph-structure file changes without staging unrelated files."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import reconcile_feedback
    from .errors import ValidationError

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.graph-structure.commit")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        root = _graph_governance_project_root(project_id, body)
        feedback_ids = _feedback_ids_from_body(body)
        lifecycle = _graph_structure_lifecycle_for_feedback_ids(project_id, snapshot_id, feedback_ids)
        files = _operation_files_from_body_or_lifecycle(body, lifecycle)
        if not files:
            raise ValidationError("graph-structure operation has no file changes to commit")
        guard = _graph_structure_dirty_guard(root, files, commit=True)
        if guard["blocked"]:
            return 409, {
                "ok": False,
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "status": "blocked_dirty_overlap",
                "operation_type": "graph_structure",
                "feedback_ids": feedback_ids,
                "changed_files": files,
                "dirty_guard": guard,
                "message": "Commit/apply refused because dirty scope includes unsafe overlap with the operation files.",
            }
        commit = _git_commit_files(
            root,
            files,
            message=str(body.get("message") or ""),
            bug_id=str(body.get("bug_id") or body.get("backlog_id") or ""),
            actor=str(body.get("actor") or "dashboard_user"),
        )
        decision = reconcile_feedback.decide_feedback_items(
            project_id,
            snapshot_id,
            feedback_ids,
            action="accept_graph_correction",
            actor=str(body.get("actor") or "dashboard_user"),
            rationale=str(body.get("reason") or body.get("rationale") or "Committed graph-structure Review Queue operation."),
        )
        conn.commit()
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "status": "committed",
            "operation_type": "graph_structure",
            "feedback_ids": feedback_ids,
            "changed_files": files,
            "dirty_guard": guard,
            "commit": commit,
            "decision": decision,
            "requires_update_graph": bool(lifecycle.get("update_graph_after_commit", True)),
            "message": "Committed graph-structure files. Run Update Graph/reconcile before treating bindings or structure as graph truth.",
        }
    finally:
        conn.close()


def _feedback_ids_from_body(body: dict[str, Any]) -> list[str]:
    raw = body.get("feedback_ids")
    if raw is None:
        raw = [body.get("feedback_id")]
    if isinstance(raw, str):
        ids = [raw]
    elif isinstance(raw, list):
        ids = [str(item or "") for item in raw]
    else:
        raise ValidationError("feedback_ids must be a string or list")
    ids = [item.strip() for item in ids if item.strip()]
    if not ids:
        raise ValidationError("at least one feedback_id is required")
    return ids


def _graph_structure_lifecycle_for_feedback_ids(
    project_id: str,
    snapshot_id: str,
    feedback_ids: list[str],
) -> dict[str, Any]:
    from . import reconcile_feedback

    state = reconcile_feedback.load_feedback_state(project_id, snapshot_id)
    items = state.get("items") if isinstance(state.get("items"), dict) else {}
    selected = [dict(items.get(feedback_id) or {}) for feedback_id in feedback_ids]
    missing = [feedback_id for feedback_id, item in zip(feedback_ids, selected) if not item]
    if missing:
        raise ValidationError(f"feedback item not found: {', '.join(missing)}")
    lifecycle = _graph_structure_lifecycle_from_feedback_items(selected)
    if not lifecycle:
        raise ValidationError("feedback item is not a graph-structure file operation")
    return lifecycle


def _operation_files_from_body_or_lifecycle(body: dict[str, Any], lifecycle: dict[str, Any]) -> list[str]:
    requested = _stable_strings(body.get("changed_files") or body.get("files") or body.get("paths") or [])
    lifecycle_files = _stable_strings(lifecycle.get("changed_files") or [])
    if requested:
        unknown = [path for path in requested if path not in lifecycle_files]
        if unknown:
            raise ValidationError(f"requested files are not part of the operation: {', '.join(unknown)}")
        return requested
    return lifecycle_files


def _git_status_entries(root: Path) -> dict[str, str]:
    proc = subprocess.run(
        ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
        cwd=root,
        check=False,
        capture_output=True,
        text=False,
    )
    if proc.returncode != 0:
        raise GovernanceError("git_status_failed", proc.stderr.decode("utf-8", "replace"), 500)
    parts = [part for part in proc.stdout.decode("utf-8", "replace").split("\0") if part]
    out: dict[str, str] = {}
    i = 0
    while i < len(parts):
        entry = parts[i]
        status = entry[:2]
        path = entry[3:].strip()
        if status.startswith("R") or status.startswith("C"):
            i += 1
            if i < len(parts):
                path = parts[i].strip()
        if path:
            out[path] = status
        i += 1
    return out


def _graph_structure_dirty_guard(root: Path, files: list[str], *, commit: bool = False) -> dict[str, Any]:
    statuses = _git_status_entries(root)
    operation_status = {path: statuses[path] for path in files if path in statuses}
    unrelated_dirty = sorted(path for path in statuses if path not in files)
    unsafe = {
        path: status
        for path, status in operation_status.items()
        if status in {"MM", "AM", "RM", "UU", "AA", "DD"} or status.startswith("U") or status.endswith("D")
    }
    if commit:
        missing_dirty = [path for path in files if path not in statuses]
        if missing_dirty:
            unsafe.update({path: "clean" for path in missing_dirty})
    return {
        "ok": not unsafe,
        "blocked": bool(unsafe),
        "operation_status": operation_status,
        "unsafe_overlap": unsafe,
        "unrelated_dirty_files": unrelated_dirty,
        "unrelated_dirty_count": len(unrelated_dirty),
        "policy": "commit_only_operation_files" if commit else "discard_only_operation_files",
    }


def _git_discard_files(root: Path, files: list[str]) -> list[str]:
    statuses = _git_status_entries(root)
    discarded: list[str] = []
    tracked = [path for path in files if statuses.get(path) != "??"]
    untracked = [path for path in files if statuses.get(path) == "??"]
    if tracked:
        subprocess.run(["git", "restore", "--staged", "--worktree", "--", *tracked], cwd=root, check=True)
        discarded.extend(tracked)
    for path in untracked:
        target = _resolve_project_child(root, path)
        if target.exists() and target.is_file():
            target.unlink()
            discarded.append(path)
    return discarded


def _git_commit_files(root: Path, files: list[str], *, message: str, bug_id: str, actor: str) -> dict[str, Any]:
    subprocess.run(["git", "add", "--", *files], cwd=root, check=True)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet", "--", *files], cwd=root, check=False)
    if diff.returncode == 0:
        raise ValidationError("operation files have no staged changes to commit")
    subject = message.strip().splitlines()[0] if message.strip() else "manual fix: apply graph-structure review operation"
    commit_message = "\n".join([
        subject,
        "",
        f"Actor: {actor}",
        "Review-Queue-Operation: graph_structure",
        "Requires-Update-Graph: true",
        "",
        "Chain-Source-Stage: observer-hotfix",
        "Chain-Project: aming-claw",
        f"Chain-Bug-Id: {bug_id or 'UI-REVIEW-QUEUE-GRAPH-STRUCTURE-LIFECYCLE-20260525'}",
    ])
    subprocess.run(["git", "commit", "-m", commit_message, "--", *files], cwd=root, check=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True)
    return {"commit_sha": head.stdout.strip(), "files": files}


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/review-queue")
def handle_graph_governance_snapshot_feedback_review_queue(ctx: RequestContext):
    """Review feedback items selected from the grouped feedback queue."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import reconcile_feedback
    from .errors import ValidationError

    limit_groups = int(body.get("limit_groups") or body.get("group_limit") or body.get("limit") or 10)
    max_items = int(body.get("max_items") or body.get("item_limit") or 25)
    if limit_groups < 0 or max_items < 0:
        raise ValidationError("limit_groups and max_items must be non-negative")
    review_automation_mode = _automation_mode_from_body(
        body,
        "feedback_review_mode",
        "review_automation_mode",
        "automation_mode",
        default="manual",
    )
    require_current_semantic = bool(
        body.get("require_current_semantic")
        or body.get("current_semantics_only")
        or body.get("semantic_review_ready_only")
    )

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.review-queue")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        worker_id = str(body.get("worker_id") or body.get("reviewer_worker_id") or "").strip()
        claim_before_review = bool(body.get("claim_before_review") or worker_id)
        claim_result: dict = {}
        if claim_before_review and not bool(body.get("dry_run")):
            if not worker_id:
                raise ValidationError("worker_id is required when claim_before_review=true")
            claim_result = reconcile_feedback.claim_feedback_review_queue(
                project_id,
                snapshot_id,
                worker_id=worker_id,
                feedback_kind=str(body.get("feedback_kind") or body.get("kind") or ""),
                status=str(body.get("status") or "classified"),
                node_id=str(body.get("node_id") or ""),
                source_round=str(body.get("source_round") or body.get("feedback_round") or ""),
                lane=str(body.get("lane") or ""),
                group_by=str(body.get("group_by") or "feature"),
                include_status_observations=bool(body.get("include_status_observations")),
                include_resolved=bool(body.get("include_resolved")),
                require_current_semantic=require_current_semantic,
                limit_groups=limit_groups,
                max_items=max_items,
                lease_seconds=int(body.get("lease_seconds") or body.get("claim_lease_seconds") or 1800),
                actor=str(body.get("actor") or worker_id),
                conn=conn,
            )
            queue = {
                "summary": claim_result.get("queue_summary", {}),
                "groups": claim_result.get("selected_groups", []),
            }
            feedback_ids = [str(item) for item in (claim_result.get("feedback_ids") or []) if str(item or "")]
        else:
            queue = reconcile_feedback.build_feedback_review_queue(
                project_id,
                snapshot_id,
                feedback_kind=str(body.get("feedback_kind") or body.get("kind") or ""),
                status=str(body.get("status") or "classified"),
                node_id=str(body.get("node_id") or ""),
                source_round=str(body.get("source_round") or body.get("feedback_round") or ""),
                lane=str(body.get("lane") or ""),
                group_by=str(body.get("group_by") or "feature"),
                include_status_observations=bool(body.get("include_status_observations")),
                include_resolved=bool(body.get("include_resolved")),
                include_claimed=bool(body.get("include_claimed", True)),
                claimable_only=bool(body.get("claimable_only")),
                require_current_semantic=require_current_semantic,
                worker_id=worker_id,
                limit=limit_groups,
                conn=conn,
            )
            feedback_ids: list[str] = []
            for group in queue.get("groups") or []:
                for feedback_id in group.get("feedback_ids") or []:
                    feedback_id = str(feedback_id or "").strip()
                    if not feedback_id or feedback_id in feedback_ids:
                        continue
                    feedback_ids.append(feedback_id)
                    if max_items and len(feedback_ids) >= max_items:
                        break
                if max_items and len(feedback_ids) >= max_items:
                    break

        if bool(body.get("dry_run")):
            return {
                "ok": True,
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "dry_run": True,
                "automation": {"feedback_review_mode": review_automation_mode},
                "queue_summary": queue.get("summary", {}),
                "group_count": len(queue.get("groups") or []),
                "selected_count": len(feedback_ids),
                "feedback_ids": feedback_ids,
            }

        use_reviewer_ai = bool(
            body.get("reviewer_use_ai")
            or body.get("use_reviewer_ai")
            or body.get("semantic_use_ai")
            or body.get("use_ai")
        )
        decision = str(body.get("decision") or body.get("reviewer_decision") or "")
        ai_call = None
        review_project_root = None
        if use_reviewer_ai and not decision:
            review_project_root = _graph_governance_project_root(project_id, body)
            ai_call = _semantic_ai_call_from_body(project_id, review_project_root, {**body, "snapshot_id": snapshot_id})
            if ai_call is None and not bool(body.get("allow_rule_fallback")):
                raise ValidationError("reviewer_use_ai=true but reviewer AI call could not be built")

        reviewed: list[dict] = []
        errors: list[dict] = []
        batch_review = bool(body.get("batch_review") or body.get("batch_ai_review") or body.get("use_batch_reviewer_ai"))
        if use_reviewer_ai and batch_review and not decision:
            batch_result = reconcile_feedback.review_feedback_items_batch(
                project_id,
                snapshot_id,
                feedback_ids,
                ai_call=ai_call,
                project_root=review_project_root,
                max_context_chars=int(body.get("review_context_chars") or 6000),
                enable_read_tools=not bool(body.get("disable_read_tools")),
                grep_patterns=(
                    body.get("grep_patterns")
                    if isinstance(body.get("grep_patterns"), list)
                    else None
                ),
                actor=str(body.get("actor") or body.get("reviewed_by") or "observer"),
                accept=bool(body.get("accept") or body.get("accepted")),
            )
            for item in batch_result.get("items") or []:
                reviewed.append({
                    "feedback_id": item.get("feedback_id", ""),
                    "status": item.get("status", ""),
                    "reviewer_decision": item.get("reviewer_decision", ""),
                    "final_feedback_kind": item.get("final_feedback_kind", ""),
                    "requires_human_signoff": bool(item.get("requires_human_signoff")),
                    "reviewer_confidence": item.get("reviewer_confidence", 0.0),
                    "source_node_ids": item.get("source_node_ids") or [],
                    "target_type": item.get("target_type", ""),
                    "target_id": item.get("target_id", ""),
                })
            errors.extend(batch_result.get("errors") or [])
        else:
            for feedback_id in feedback_ids:
                try:
                    result = reconcile_feedback.review_feedback_item(
                        project_id,
                        snapshot_id,
                        feedback_id,
                        decision=decision,
                        rationale=str(body.get("rationale") or body.get("reviewer_rationale") or ""),
                        confidence=float(body["confidence"]) if body.get("confidence") is not None else None,
                        status_observation_category=str(
                            body.get("status_observation_category")
                            or body.get("observation_category")
                            or body.get("category")
                            or ""
                        ),
                        actor=str(body.get("actor") or body.get("reviewed_by") or "observer"),
                        accept=bool(body.get("accept") or body.get("accepted")),
                        ai_call=ai_call,
                        project_root=review_project_root,
                        max_context_chars=int(body.get("review_context_chars") or 6000),
                        enable_read_tools=not bool(body.get("disable_read_tools")),
                        grep_patterns=(
                            body.get("grep_patterns")
                            if isinstance(body.get("grep_patterns"), list)
                            else None
                        ),
                    )
                    item = (result.get("items") or [{}])[0]
                    reviewed.append({
                        "feedback_id": feedback_id,
                        "status": item.get("status", ""),
                        "reviewer_decision": item.get("reviewer_decision", ""),
                        "final_feedback_kind": item.get("final_feedback_kind", ""),
                        "requires_human_signoff": bool(item.get("requires_human_signoff")),
                        "reviewer_confidence": item.get("reviewer_confidence", 0.0),
                        "source_node_ids": item.get("source_node_ids") or [],
                        "target_type": item.get("target_type", ""),
                        "target_id": item.get("target_id", ""),
                    })
                except Exception as exc:
                    errors.append({"feedback_id": feedback_id, "error": str(exc)})
                    if not bool(body.get("continue_on_error")):
                        break

        try:
            audit_service.record(
                conn,
                project_id,
                "reconcile_feedback_queue_reviewed",
                actor=str(body.get("actor") or "observer"),
                details=json.dumps({
                    "snapshot_id": snapshot_id,
                    "selected_count": len(feedback_ids),
                    "reviewed_count": len(reviewed),
                    "error_count": len(errors),
                    "lane": body.get("lane", ""),
                    "group_by": body.get("group_by", "feature"),
                    "use_reviewer_ai": use_reviewer_ai,
                    "claim_before_review": claim_before_review,
                    "claim_id": claim_result.get("claim_id", ""),
                    "feedback_review_mode": review_automation_mode,
                }, ensure_ascii=False, sort_keys=True),
            )
            conn.commit()
        except Exception:
            pass

        return {
            "ok": not errors,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "automation": {"feedback_review_mode": review_automation_mode},
            "claim": claim_result,
            "queue_summary": queue.get("summary", {}),
            "group_count": len(queue.get("groups") or []),
            "selected_count": len(feedback_ids),
            "reviewed_count": len(reviewed),
            "error_count": len(errors),
            "reviewed": reviewed,
            "errors": errors,
            "summary": reconcile_feedback.feedback_summary(project_id, snapshot_id),
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/feedback/file-backlog")
def handle_graph_governance_snapshot_feedback_file_backlog(ctx: RequestContext):
    """File an accepted project-improvement feedback item into backlog."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    feedback_id = str(body.get("feedback_id") or "").strip()
    if not feedback_id:
        from .errors import ValidationError
        raise ValidationError("feedback_id is required")
    from . import reconcile_feedback

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.feedback.file-backlog")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        try:
            backlog = reconcile_feedback.build_project_improvement_backlog(
                project_id,
                snapshot_id,
                feedback_id,
                bug_id=str(body.get("bug_id") or ""),
                actor=str(body.get("actor") or "observer"),
                allow_status_observation=bool(body.get("allow_status_observation")),
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        bug_id = backlog["bug_id"]
        payload = backlog["payload"]
        overrides = body.get("overrides") if isinstance(body.get("overrides"), dict) else {}
        for key in (
            "title",
            "status",
            "priority",
            "details_md",
            "target_files",
            "test_files",
            "acceptance_criteria",
            "chain_trigger_json",
            "required_docs",
            "provenance_paths",
        ):
            if key in body:
                payload[key] = body[key]
            if key in overrides:
                payload[key] = overrides[key]
        now = _utc_now()
        conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, target_files, test_files,
                acceptance_criteria, chain_task_id, "commit", discovered_at,
                fixed_at, details_md, chain_trigger_json, required_docs,
                provenance_paths, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, '', '', ?, '', ?, ?, ?, ?, ?, ?)
               ON CONFLICT(bug_id) DO UPDATE SET
                 title = excluded.title,
                 status = excluded.status,
                 priority = excluded.priority,
                 target_files = excluded.target_files,
                 test_files = excluded.test_files,
                 acceptance_criteria = excluded.acceptance_criteria,
                 details_md = excluded.details_md,
                 chain_trigger_json = excluded.chain_trigger_json,
                 required_docs = excluded.required_docs,
                 provenance_paths = excluded.provenance_paths,
                 updated_at = excluded.updated_at
            """,
            (
                bug_id,
                payload.get("title", ""),
                payload.get("status", "OPEN"),
                payload.get("priority", "P2"),
                json.dumps(payload.get("target_files", []), ensure_ascii=False),
                json.dumps(payload.get("test_files", []), ensure_ascii=False),
                json.dumps(payload.get("acceptance_criteria", []), ensure_ascii=False),
                now,
                payload.get("details_md", ""),
                json.dumps(payload.get("chain_trigger_json", {}), ensure_ascii=False, sort_keys=True),
                json.dumps(payload.get("required_docs", []), ensure_ascii=False),
                json.dumps(payload.get("provenance_paths", []), ensure_ascii=False),
                now,
                now,
            ),
        )
        conn.commit()
        mark = reconcile_feedback.mark_feedback_backlog_filed(
            project_id,
            snapshot_id,
            feedback_id,
            bug_id=bug_id,
            actor=str(body.get("actor") or "observer"),
        )
        try:
            audit_service.record(
                conn,
                project_id,
                "reconcile_feedback_backlog_filed",
                actor=str(body.get("actor") or "observer"),
                bug_id=bug_id,
                details=json.dumps({
                    "snapshot_id": snapshot_id,
                    "feedback_id": feedback_id,
                }, ensure_ascii=False, sort_keys=True),
            )
            conn.commit()
        except Exception:
            pass
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "feedback_id": feedback_id,
            "bug_id": bug_id,
            "payload": payload,
            "feedback": (mark.get("items") or [{}])[0],
        }
    finally:
        conn.close()


def _semantic_jobs_target_ids(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        values = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = [raw]
    return [str(value or "").strip() for value in values if str(value or "").strip()]


def _semantic_jobs_options(body: dict) -> dict:
    return body.get("options") if isinstance(body.get("options"), dict) else {}


def _semantic_jobs_dry_run(body: dict) -> bool:
    options = _semantic_jobs_options(body)
    value = body.get("dry_run") if body.get("dry_run") is not None else options.get("dry_run")
    return bool(_semantic_bool_from_body({"dry_run": value}, "dry_run", default=False))


def _semantic_jobs_target_scope(body: dict) -> str:
    options = _semantic_jobs_options(body)
    return str(
        body.get("target_scope")
        or options.get("target_scope")
        or "snapshot"
    ).strip().lower().replace("-", "_")


def _semantic_edge_id(edge: dict) -> str:
    src = str(edge.get("src") or edge.get("source") or "").strip()
    dst = str(edge.get("dst") or edge.get("target") or "").strip()
    edge_type = str(edge.get("edge_type") or edge.get("type") or "depends_on").strip() or "depends_on"
    return f"{src}->{dst}:{edge_type}" if src and dst else ""


def _semantic_edge_id_variants(edge_id: str) -> list[str]:
    """Return dashboard/API-compatible edge id spellings in canonical-first order."""
    raw = str(edge_id or "").strip()
    if not raw:
        return []
    variants = [raw]
    if "|" in raw:
        parts = [part.strip() for part in raw.split("|")]
        if len(parts) == 3 and all(parts):
            variants.insert(0, f"{parts[0]}->{parts[1]}:{parts[2]}")
    if "->" in raw and ":" in raw:
        src, rest = raw.split("->", 1)
        dst, edge_type = rest.rsplit(":", 1)
        pipe = f"{src}|{dst}|{edge_type}"
        if pipe not in variants:
            variants.append(pipe)
    deduped: list[str] = []
    for value in variants:
        if value and value not in deduped:
            deduped.append(value)
    return deduped


def _parse_edge_id(edge_id: str) -> tuple[str, str, str] | None:
    """Parse the two canonical edge_id formats into (src, dst, edge_type).

    Worker emits `<src>-><dst>:<type>` (e.g. `L7.1->L4.1:creates_task`).
    Dashboard ActionControlPanel emits `<src>|<dst>|<type>`. Both accepted.
    Returns None when the string doesn't match either shape.
    """
    if not edge_id:
        return None
    if "|" in edge_id:
        parts = edge_id.split("|")
        if len(parts) == 3 and all(p.strip() for p in parts):
            return parts[0].strip(), parts[1].strip(), parts[2].strip()
    if "->" in edge_id and ":" in edge_id:
        head, _, rest = edge_id.partition("->")
        dst, _, etype = rest.partition(":")
        if head.strip() and dst.strip() and etype.strip():
            return head.strip(), dst.strip(), etype.strip()
    return None


def _lookup_edge_for_hydration(
    conn: sqlite3.Connection | None,
    project_id: str,
    snapshot_id: str,
    edge_id: str,
) -> dict:
    """Fetch the full edge row from the active snapshot so downstream
    consumers (the AI prompt builder, the event payload) get populated
    src / dst / edge_type / evidence fields instead of empty strings.

    Returns an empty dict on any lookup failure — caller falls back to
    its previous behaviour (empty edge_context). The point of this helper
    is to fix the case where the dashboard sends only target_ids strings
    and the backend then built an empty edge dict, which silently fed
    garbage to the AI (it correctly replied risk=insufficient_context).
    """
    if conn is None or not project_id or not snapshot_id:
        return {}
    parsed = _parse_edge_id(edge_id)
    if not parsed:
        return {}
    src, dst, edge_type = parsed
    from . import graph_snapshot_store as store

    try:
        candidates = store.list_graph_snapshot_edges(
            conn, project_id, snapshot_id, limit=5000
        )
    except Exception:
        return {}
    for edge in candidates:
        if (
            str(edge.get("src") or edge.get("source") or "").strip() == src
            and str(edge.get("dst") or edge.get("target") or "").strip() == dst
            and str(edge.get("edge_type") or edge.get("type") or "").strip() == edge_type
        ):
            return edge
    return {}


def _semantic_jobs_edge_targets(
    body: dict,
    *,
    conn: sqlite3.Connection | None = None,
    project_id: str = "",
    snapshot_id: str = "",
) -> list[dict]:
    raw_edges = body.get("edges") or body.get("edge_targets")
    edge_rows: list[dict] = []
    if isinstance(raw_edges, list):
        for raw in raw_edges:
            if isinstance(raw, dict):
                edge_id = str(raw.get("edge_id") or raw.get("id") or "").strip()
                if not edge_id:
                    edge_id = _semantic_edge_id(raw)
                if edge_id:
                    edge_rows.append({"edge_id": edge_id, "edge": raw})
    for edge_id in _semantic_jobs_target_ids(
        body.get("target_ids")
        or body.get("target_id")
        or body.get("edge_ids")
        or body.get("edge_id")
    ):
        if edge_id and edge_id not in {row["edge_id"] for row in edge_rows}:
            # Hydrate the edge dict from the snapshot so downstream
            # edge_context.src/dst/edge_type/evidence aren't empty strings.
            # Previously this appended {"edge_id": ..., "edge": {}} which
            # caused the AI to reply risk=insufficient_context on every
            # dashboard-submitted edge enrich. _lookup_edge_for_hydration
            # falls back to {} when the edge isn't in the snapshot.
            edge_dict = _lookup_edge_for_hydration(
                conn, project_id, snapshot_id, edge_id
            )
            edge_rows.append({"edge_id": edge_id, "edge": edge_dict})
    if edge_rows:
        return edge_rows

    options = body.get("options") if isinstance(body.get("options"), dict) else {}
    selector = body.get("selector") if isinstance(body.get("selector"), dict) else {}
    all_eligible = bool(
        body.get("all_eligible")
        or options.get("all_eligible")
        or selector.get("all_eligible")
    )
    if not all_eligible or conn is None or not project_id or not snapshot_id:
        return []

    from . import graph_snapshot_store as store

    raw_types = (
        body.get("edge_types")
        or options.get("edge_types")
        or selector.get("edge_types")
        or []
    )
    allowed_types = set(_semantic_jobs_target_ids(raw_types))
    include_contains = bool(
        body.get("include_contains")
        or options.get("include_contains")
        or selector.get("include_contains")
    )
    limit = int(body.get("limit") or options.get("limit") or selector.get("limit") or 200)
    candidates = store.list_graph_snapshot_edges(
        conn,
        project_id,
        snapshot_id,
        limit=2000,
    )
    for edge in candidates:
        edge_id = _semantic_edge_id(edge)
        edge_type = str(edge.get("edge_type") or "").strip()
        if not edge_id:
            continue
        if edge_type == "contains" and not include_contains:
            continue
        if allowed_types and edge_type not in allowed_types:
            continue
        edge_rows.append({"edge_id": edge_id, "edge": edge})
        if len(edge_rows) >= max(1, min(limit, 1000)):
            break
    return edge_rows


def _semantic_jobs_enqueue_body(body: dict) -> dict:
    from .errors import ValidationError

    options = _semantic_jobs_options(body)
    target_scope = _semantic_jobs_target_scope(body)
    if target_scope in {"edge", "edges"}:
        raise ValidationError(
            "edge semantic jobs are not supported by the current backend queue yet; "
            "use graph feedback/edge correction review until edge semantics are implemented"
        )
    if target_scope not in {"snapshot", "node", "nodes", "subtree"}:
        raise ValidationError(f"unsupported semantic job target_scope: {target_scope}")

    target_ids = _semantic_jobs_target_ids(
        body.get("semantic_node_ids")
        or body.get("target_ids")
        or body.get("target_id")
        or body.get("node_ids")
        or body.get("node_id")
        or options.get("semantic_node_ids")
        or options.get("target_ids")
        or options.get("node_ids")
        or options.get("node_id")
    )
    if target_scope in {"node", "nodes", "subtree"} and not target_ids:
        raise ValidationError("target_ids is required for node or subtree semantic jobs")

    layers: list[str] = []
    if options.get("include_l7_features", True):
        layers.append("L7")
    if options.get("include_containers"):
        layers.extend(["L1", "L2", "L3"])
    if options.get("include_l4_assets"):
        layers.append("L4")
    if body.get("semantic_layers") or body.get("layers"):
        layers = _semantic_jobs_target_ids(body.get("semantic_layers") or body.get("layers"))
    layers = [layer.upper() for layer in layers if str(layer or "").strip()]

    skip_current = options.get("skip_current")
    if skip_current is None:
        skip_current = body.get("skip_current")
    if skip_current is None:
        skip_current = True
    if options.get("retry_stale_failed"):
        skip_current = False

    enqueue_body = dict(body)
    enqueue_body.update({
        "semantic_use_ai": False,
        "use_ai": False,
        "semantic_mode": "enqueue_only",
        "semantic_graph_state": True,
        "semantic_skip_completed": bool(skip_current),
        "actor": body.get("actor") or body.get("created_by") or "dashboard_user",
    })
    semantic_scope = (
        body.get("semantic_ai_scope")
        or body.get("ai_scope")
        or options.get("semantic_ai_scope")
        or options.get("scope")
    )
    if semantic_scope:
        normalized_scope = str(semantic_scope).strip().lower().replace("-", "_")
        if normalized_scope in {"selected_node", "selected_nodes", "selected_subtree"}:
            normalized_scope = "selected"
        enqueue_body["semantic_ai_scope"] = normalized_scope
    if target_ids:
        enqueue_body["semantic_node_ids"] = target_ids
        enqueue_body.pop("semantic_layers", None)
        enqueue_body.pop("layers", None)
    elif layers:
        enqueue_body["semantic_layers"] = layers
    else:
        enqueue_body["semantic_layers"] = ["L7"]
    if any(layer in {"L1", "L2", "L3", "L4"} for layer in enqueue_body.get("semantic_layers", [])):
        enqueue_body["semantic_include_structural"] = True
    return enqueue_body


def _semantic_jobs_node_plan_targets(
    conn,
    project_id: str,
    snapshot_id: str,
    enqueue_body: dict,
) -> list[str]:
    explicit = _semantic_jobs_target_ids(
        enqueue_body.get("semantic_node_ids")
        or enqueue_body.get("target_ids")
        or enqueue_body.get("node_ids")
        or enqueue_body.get("node_id")
    )
    if explicit:
        return explicit
    semantic_scope = str(enqueue_body.get("semantic_ai_scope") or "").strip().lower()
    if semantic_scope in {"stale", "stale_nodes", "stale_node", "semantic_stale"}:
        return _semantic_jobs_stale_node_targets(
            conn,
            project_id,
            snapshot_id,
            enqueue_body,
        )
    layers = {
        str(layer or "").upper()
        for layer in (enqueue_body.get("semantic_layers") or [])
        if str(layer or "").strip()
    }
    include_structural = bool(enqueue_body.get("semantic_include_structural"))
    from . import graph_snapshot_store as store

    nodes = store.list_graph_snapshot_nodes(
        conn,
        project_id,
        snapshot_id,
        limit=1000,
        include_semantic=False,
    )
    planned: list[str] = []
    for node in nodes:
        node_id = str(node.get("node_id") or node.get("id") or "").strip()
        layer = str(node.get("layer") or "").upper()
        if not node_id:
            continue
        if layers and layer not in layers:
            continue
        primary = node.get("primary_files") or node.get("primary") or []
        if primary or include_structural:
            planned.append(node_id)
    return planned


def _semantic_jobs_stale_node_targets(
    conn,
    project_id: str,
    snapshot_id: str,
    enqueue_body: dict,
) -> list[str]:
    """Return nodes whose current semantic projection is stale.

    The dashboard's stale semantic retry affordance is driven by projection
    health (`semantic_stale_count`), so the job selector must use the same
    source of truth instead of broad L7 layer matching.
    """
    from . import graph_events
    from . import graph_snapshot_store as store

    projection = graph_events.get_semantic_projection(conn, project_id, snapshot_id) or {}
    projection_body = projection.get("projection") if isinstance(projection.get("projection"), dict) else {}
    node_semantics = (
        projection_body.get("node_semantics")
        if isinstance(projection_body.get("node_semantics"), dict)
        else {}
    )
    if not node_semantics:
        return []
    layers = {
        str(layer or "").upper()
        for layer in (enqueue_body.get("semantic_layers") or [])
        if str(layer or "").strip()
    }
    include_structural = bool(enqueue_body.get("semantic_include_structural"))
    stale_statuses = {
        "semantic_stale_feature_hash",
        "semantic_stale_file_hash",
        "semantic_stale",
        "stale_hash_mismatch",
        "stale_file_hash",
        "hash_mismatch",
        "stale",
    }
    nodes = store.list_graph_snapshot_nodes(
        conn,
        project_id,
        snapshot_id,
        limit=1000,
        include_semantic=False,
    )
    planned: list[str] = []
    for node in nodes:
        node_id = str(node.get("node_id") or node.get("id") or "").strip()
        if not node_id:
            continue
        if layers and str(node.get("layer") or "").upper() not in layers:
            continue
        primary = node.get("primary_files") or node.get("primary") or []
        if not primary and not include_structural:
            continue
        semantic = node_semantics.get(node_id)
        if not isinstance(semantic, dict):
            continue
        validity = semantic.get("validity") if isinstance(semantic.get("validity"), dict) else {}
        status = str(validity.get("status") or "").strip().lower()
        if status in stale_statuses:
            planned.append(node_id)
    return planned


def _semantic_jobs_resolve_stale_scope_targets(
    conn,
    project_id: str,
    snapshot_id: str,
    enqueue_body: dict,
) -> list[str]:
    """Freeze stale scope to explicit node ids before enqueueing."""
    if _semantic_jobs_target_ids(
        enqueue_body.get("semantic_node_ids")
        or enqueue_body.get("target_ids")
        or enqueue_body.get("node_ids")
        or enqueue_body.get("node_id")
    ):
        return []
    semantic_scope = str(enqueue_body.get("semantic_ai_scope") or "").strip().lower()
    if semantic_scope not in {"stale", "stale_nodes", "stale_node", "semantic_stale"}:
        return []
    planned = _semantic_jobs_stale_node_targets(
        conn,
        project_id,
        snapshot_id,
        enqueue_body,
    )
    enqueue_body["semantic_node_ids"] = planned
    enqueue_body["semantic_ai_scope"] = "selected"
    enqueue_body.pop("semantic_layers", None)
    enqueue_body.pop("layers", None)
    return planned


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/events")
def handle_graph_governance_snapshot_events_list(ctx: RequestContext):
    """List auditable graph governance events for dashboard/operator workflows."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    from . import graph_events

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.events.list")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        events = graph_events.list_events(
            conn,
            project_id,
            snapshot_id,
            statuses=_query_statuses(ctx.query) or None,
            event_types=_query_statuses(ctx.query, "event_type") or None,
            target_type=str(ctx.query.get("target_type") or ""),
            target_id=str(ctx.query.get("target_id") or ""),
            limit=_query_int(ctx.query, "limit", 200),
            offset=_query_int(ctx.query, "offset", 0),
        )
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "count": len(events),
            "events": events,
            "summary": {"by_status": graph_events.status_counts(conn, project_id, snapshot_id)},
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/events")
def handle_graph_governance_snapshot_events_create(ctx: RequestContext):
    """Create a proposed/observed graph event using the shared event vocabulary."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import graph_events
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.events.create")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        with sqlite_write_lock():
            try:
                event = graph_events.create_event(
                    conn,
                    project_id,
                    snapshot_id,
                    event_type=str(body.get("event_type") or body.get("type") or ""),
                    event_kind=str(body.get("event_kind") or body.get("kind") or "user_feedback"),
                    target_type=str(body.get("target_type") or ""),
                    target_id=str(body.get("target_id") or body.get("node_id") or body.get("edge_id") or ""),
                    status=str(body.get("status") or "proposed"),
                    risk_level=str(body.get("risk_level") or body.get("risk") or "low"),
                    confidence=float(body.get("confidence") or 0.0),
                    baseline_commit=str(body.get("baseline_commit") or ""),
                    target_commit=str(body.get("target_commit") or ""),
                    stable_node_key=str(body.get("stable_node_key") or ""),
                    feature_hash=str(body.get("feature_hash") or ""),
                    file_hashes=body.get("file_hashes") if isinstance(body.get("file_hashes"), dict) else {},
                    payload=body.get("payload") if isinstance(body.get("payload"), dict) else {},
                    precondition=body.get("precondition") if isinstance(body.get("precondition"), dict) else {},
                    evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else {},
                    created_by=str(body.get("actor") or body.get("created_by") or "dashboard_user"),
                    event_id=str(body.get("event_id") or "") or None,
                )
            except (KeyError, ValueError) as exc:
                _raise_graph_api_validation(exc)
            conn.commit()
        return 201, {"ok": True, "project_id": project_id, "snapshot_id": snapshot_id, "event": event}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/events/materialize")
def handle_graph_governance_snapshot_events_materialize(ctx: RequestContext):
    """Materialize accepted graph events into a new candidate snapshot."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import graph_events
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.events.materialize")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        raw_ids = body.get("event_ids")
        if raw_ids is None and body.get("event_id"):
            raw_ids = [body.get("event_id")]
        event_ids = _semantic_jobs_target_ids(raw_ids) if raw_ids is not None else []
        with sqlite_write_lock():
            try:
                result = graph_events.materialize_events(
                    conn,
                    project_id,
                    snapshot_id,
                    event_ids=event_ids or None,
                    actor=str(body.get("actor") or "observer"),
                    new_snapshot_id=str(body.get("new_snapshot_id") or "") or None,
                    activate=bool(body.get("activate") or body.get("make_active")),
                )
            except (KeyError, ValueError) as exc:
                _raise_graph_api_validation(exc)
            conn.commit()
        return {"ok": not result.get("errors"), **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/events/materialize/preview")
def handle_graph_governance_snapshot_events_materialize_preview(ctx: RequestContext):
    """Preview graph-event materialization without mutating events or snapshots."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import graph_events

    raw_ids = body.get("event_ids")
    if raw_ids is None and body.get("event_id"):
        raw_ids = [body.get("event_id")]
    event_ids = _semantic_jobs_target_ids(raw_ids) if raw_ids is not None else []
    raw_statuses = body.get("statuses") or body.get("status")
    statuses = _semantic_jobs_target_ids(raw_statuses) if raw_statuses is not None else []

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.events.materialize.preview")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        try:
            result = graph_events.preview_events(
                conn,
                project_id,
                snapshot_id,
                event_ids=event_ids or None,
                statuses=statuses or None,
                actor=str(body.get("actor") or "observer"),
                include_graph=bool(body.get("include_graph") or body.get("include_preview_graph")),
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        return {"ok": not result.get("errors"), **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic/events/backfill")
def handle_graph_governance_snapshot_semantic_events_backfill(ctx: RequestContext):
    """Import existing semantic cache rows into graph_events as immutable evidence."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import graph_events
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.semantic.events.backfill")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        with sqlite_write_lock():
            try:
                result = graph_events.backfill_existing_semantic_events(
                    conn,
                    project_id,
                    snapshot_id,
                    actor=str(body.get("actor") or body.get("created_by") or "observer"),
                )
            except (KeyError, ValueError) as exc:
                _raise_graph_api_validation(exc)
            conn.commit()
        return {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic/seed/import")
def handle_graph_governance_snapshot_seed_semantic_import(ctx: RequestContext):
    """Import packaged seed graph context into local semantic state."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import seed_graph_semantics
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.semantic.seed.import")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        with sqlite_write_lock():
            try:
                result = seed_graph_semantics.import_seed_graph_semantics(
                    conn,
                    project_id,
                    snapshot_id,
                    seed_path=body.get("seed_path") or None,
                    actor=str(body.get("actor") or body.get("created_by") or "observer"),
                    projection_id=str(body.get("projection_id") or ""),
                    dry_run=bool(body.get("dry_run")),
                )
            except (FileNotFoundError, KeyError, ValueError) as exc:
                _raise_graph_api_validation(exc)
            conn.commit()
        return {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic/projection")
def handle_graph_governance_snapshot_semantic_projection_build(ctx: RequestContext):
    """Build a hash-aware semantic projection snapshot from structure + graph_events."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import graph_events
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.semantic.projection.build")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        with sqlite_write_lock():
            try:
                result = graph_events.build_semantic_projection(
                    conn,
                    project_id,
                    snapshot_id,
                    actor=str(body.get("actor") or body.get("created_by") or "observer"),
                    projection_id=str(body.get("projection_id") or "") or None,
                    ref_name=str(body.get("ref_name") or ""),
                    branch_ref=str(body.get("branch_ref") or body.get("worktree_branch") or ""),
                    backfill_existing=bool(body.get("backfill_existing", True)),
                )
            except (KeyError, ValueError) as exc:
                _raise_graph_api_validation(exc)
            conn.commit()
        return {"ok": True, **result}
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic/projection")
def handle_graph_governance_snapshot_semantic_projection_get(ctx: RequestContext):
    """Return the latest semantic projection/health view for a snapshot."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    projection_id = str(ctx.query.get("projection_id") or "")
    ref_name = str(ctx.query.get("ref_name") or "")
    branch_ref = str(ctx.query.get("branch_ref") or "")
    from . import graph_events

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.semantic.projection.get")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        projection = graph_events.get_semantic_projection(
            conn,
            project_id,
            snapshot_id,
            projection_id,
            ref_name=ref_name,
            branch_ref=branch_ref if "branch_ref" in ctx.query else None,
        )
        if not projection:
            return {
                "ok": True,
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "projection": None,
                "health": {},
                "status": "missing",
            }
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "projection_id": projection.get("projection_id", ""),
            "projection": projection.get("projection") or {},
            "health": projection.get("health") or {},
            "status": projection.get("status") or "current",
            "ref_name": projection.get("ref_name", ""),
            "branch_ref": projection.get("branch_ref", ""),
            "event_watermark": projection.get("event_watermark", 0),
            "base_commit": projection.get("base_commit", ""),
            "created_at": projection.get("created_at", ""),
            "updated_at": projection.get("updated_at", ""),
        }
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/events/{event_id}")
def handle_graph_governance_snapshot_event_get(ctx: RequestContext):
    """Fetch one graph event with parsed payload/evidence."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    event_id = ctx.path_params["event_id"]
    from . import graph_events

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.events.get")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        event = graph_events.get_event(conn, project_id, snapshot_id, event_id)
        if not event:
            from .errors import ValidationError
            raise ValidationError(f"graph event not found: {event_id}")
        return {"ok": True, "project_id": project_id, "snapshot_id": snapshot_id, "event": event}
    finally:
        conn.close()


def _sync_semantic_cache_for_event_status(
    conn,
    project_id: str,
    snapshot_id: str,
    event: dict[str, Any],
    *,
    status: str,
) -> dict[str, Any]:
    event_type = str(event.get("event_type") or "")
    target_id = str(event.get("target_id") or "").strip()
    if not target_id or event_type not in {"semantic_node_enriched", "edge_semantic_enriched"}:
        return {"semantic_event": False}

    if status == "accepted":
        table = "graph_semantic_edges" if event_type == "edge_semantic_enriched" else "graph_semantic_nodes"
        id_column = "edge_id" if event_type == "edge_semantic_enriched" else "node_id"
        cur = conn.execute(
            f"""
            UPDATE {table}
            SET status = 'ai_complete', updated_at = ?
            WHERE project_id = ? AND snapshot_id = ? AND {id_column} = ?
              AND status IN ('pending_review', 'review_pending', 'ai_complete')
            """,
            (_utc_now(), project_id, snapshot_id, target_id),
        )
        return {
            "semantic_event": True,
            "target_type": "edge" if event_type == "edge_semantic_enriched" else "node",
            "target_id": target_id,
            "cache_status": "ai_complete",
            "cache_rows_updated": int(cur.rowcount or 0),
        }

    if status == "rejected":
        table = "graph_semantic_edges" if event_type == "edge_semantic_enriched" else "graph_semantic_nodes"
        id_column = "edge_id" if event_type == "edge_semantic_enriched" else "node_id"
        cur = conn.execute(
            f"""
            DELETE FROM {table}
            WHERE project_id = ? AND snapshot_id = ? AND {id_column} = ?
              AND status IN ('pending_review', 'review_pending')
            """,
            (project_id, snapshot_id, target_id),
        )
        return {
            "semantic_event": True,
            "target_type": "edge" if event_type == "edge_semantic_enriched" else "node",
            "target_id": target_id,
            "cache_status": "rejected",
            "cache_rows_deleted": int(cur.rowcount or 0),
        }

    return {"semantic_event": True, "target_id": target_id, "cache_status": "unchanged"}


def _graph_event_status_action(ctx: RequestContext, status: str, action: str):
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    event_id = ctx.path_params["event_id"]
    body = ctx.body
    from . import graph_events
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, action)
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        with sqlite_write_lock():
            try:
                event = graph_events.update_event_status(
                    conn,
                    project_id,
                    snapshot_id,
                    event_id,
                    status=status,
                    actor=str(body.get("actor") or "observer"),
                    evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else {},
                )
                semantic_cache_sync = _sync_semantic_cache_for_event_status(
                    conn,
                    project_id,
                    snapshot_id,
                    event,
                    status=status,
                )
                projection = {}
                if semantic_cache_sync.get("semantic_event") and status in {"accepted", "rejected"}:
                    projection = graph_events.build_semantic_projection(
                        conn,
                        project_id,
                        snapshot_id,
                        actor=str(body.get("actor") or "observer"),
                    )
            except (KeyError, ValueError) as exc:
                _raise_graph_api_validation(exc)
            conn.commit()
        result = {"ok": True, "project_id": project_id, "snapshot_id": snapshot_id, "event": event}
        if semantic_cache_sync.get("semantic_event"):
            result["semantic_cache_sync"] = semantic_cache_sync
            result["projection_rebuilt"] = bool(projection)
            if projection:
                result["projection_id"] = projection.get("projection_id", "")
                result["event_watermark"] = projection.get("event_watermark", 0)
        return result
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/events/{event_id}/accept")
def handle_graph_governance_snapshot_event_accept(ctx: RequestContext):
    return _graph_event_status_action(ctx, "accepted", "graph-governance.snapshot.events.accept")


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/events/{event_id}/reject")
def handle_graph_governance_snapshot_event_reject(ctx: RequestContext):
    return _graph_event_status_action(ctx, "rejected", "graph-governance.snapshot.events.reject")


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/events/{event_id}/mark-stale")
def handle_graph_governance_snapshot_event_mark_stale(ctx: RequestContext):
    return _graph_event_status_action(ctx, "stale", "graph-governance.snapshot.events.mark-stale")


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/events/{event_id}/ai-refine")
def handle_graph_governance_snapshot_event_ai_refine(ctx: RequestContext):
    """Refine an event payload with explicit input or the reconcile analyzer AI."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    event_id = ctx.path_params["event_id"]
    body = ctx.body
    from . import graph_events
    from .db import sqlite_write_lock
    from .errors import ValidationError

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.events.ai-refine")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        event = graph_events.get_event(conn, project_id, snapshot_id, event_id)
        if not event:
            raise ValidationError(f"graph event not found: {event_id}")
        refined_payload = body.get("refined_payload") or body.get("payload")
        ai_review = body.get("ai_review") if isinstance(body.get("ai_review"), dict) else {}
        if body.get("use_ai") or body.get("semantic_use_ai") or body.get("reviewer_use_ai"):
            root = _graph_governance_project_root(project_id, body)
            ai_call = _semantic_ai_call_from_body(project_id, root, {**body, "snapshot_id": snapshot_id})
            if ai_call is None:
                raise ValidationError("use_ai=true but reconcile analyzer call could not be built")
            ai_result = ai_call("graph_event_refine", {
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "event": event,
                "instructions": str(body.get("instructions") or ""),
            }) or {}
            if isinstance(ai_result, dict):
                refined_payload = ai_result.get("refined_payload") or ai_result.get("payload") or refined_payload
                ai_review = ai_result
        if refined_payload is not None and not isinstance(refined_payload, dict):
            raise ValidationError("refined_payload must be an object")
        if refined_payload is None and not ai_review:
            raise ValidationError("refined_payload or use_ai=true is required")
        with sqlite_write_lock():
            try:
                refined = graph_events.refine_event(
                    conn,
                    project_id,
                    snapshot_id,
                    event_id,
                    actor=str(body.get("actor") or "reconcile_analyzer"),
                    payload=refined_payload if isinstance(refined_payload, dict) else None,
                    ai_review=ai_review,
                    evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else {},
                )
            except (KeyError, ValueError) as exc:
                _raise_graph_api_validation(exc)
            conn.commit()
        return {"ok": True, "project_id": project_id, "snapshot_id": snapshot_id, "event": refined}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/events/{event_id}/refine")
def handle_graph_governance_snapshot_event_refine(ctx: RequestContext):
    return handle_graph_governance_snapshot_event_ai_refine(ctx)


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/events/{event_id}/file-backlog")
def handle_graph_governance_snapshot_event_file_backlog(ctx: RequestContext):
    """File a graph event as a backlog row without requiring semantic feedback."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    event_id = ctx.path_params["event_id"]
    body = ctx.body
    from . import graph_events
    from .db import sqlite_write_lock
    from .errors import ValidationError

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.events.file-backlog")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        event = graph_events.get_event(conn, project_id, snapshot_id, event_id)
        if not event:
            raise ValidationError(f"graph event not found: {event_id}")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        target_files = payload.get("target_files") or payload.get("files") or payload.get("paths") or []
        if isinstance(target_files, str):
            target_files = [target_files]
        bug_id = str(body.get("bug_id") or payload.get("bug_id") or f"GRAPH-EVENT-{event_id}").strip()
        title = str(
            body.get("title")
            or payload.get("title")
            or f"Graph event follow-up: {event.get('event_type') or event_id}"
        ).strip()
        details = str(
            body.get("details_md")
            or payload.get("details_md")
            or (
                f"Filed from graph governance event.\n\n"
                f"snapshot_id: {snapshot_id}\n"
                f"event_id: {event_id}\n"
                f"event_type: {event.get('event_type')}\n"
                f"target: {event.get('target_type')}:{event.get('target_id')}\n"
            )
        )
        priority = str(body.get("priority") or payload.get("priority") or "P2")
        now = _utc_now()
        with sqlite_write_lock():
            conn.execute(
                """INSERT INTO backlog_bugs
                   (bug_id, title, status, priority, target_files, test_files,
                    acceptance_criteria, chain_task_id, "commit", discovered_at,
                    fixed_at, details_md, chain_trigger_json, required_docs,
                    provenance_paths, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, '', '', ?, '', ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(bug_id) DO UPDATE SET
                     title = excluded.title,
                     status = excluded.status,
                     priority = excluded.priority,
                     target_files = excluded.target_files,
                     details_md = excluded.details_md,
                     chain_trigger_json = excluded.chain_trigger_json,
                     provenance_paths = excluded.provenance_paths,
                     updated_at = excluded.updated_at
                """,
                (
                    bug_id,
                    title,
                    str(body.get("status") or "OPEN"),
                    priority,
                    json.dumps(target_files if isinstance(target_files, list) else [], ensure_ascii=False),
                    json.dumps(body.get("test_files") if isinstance(body.get("test_files"), list) else [], ensure_ascii=False),
                    json.dumps(body.get("acceptance_criteria") if isinstance(body.get("acceptance_criteria"), list) else [], ensure_ascii=False),
                    now,
                    details,
                    json.dumps({
                        "source": "graph_event",
                        "snapshot_id": snapshot_id,
                        "event_id": event_id,
                        "event_type": event.get("event_type"),
                    }, ensure_ascii=False, sort_keys=True),
                    json.dumps(body.get("required_docs") if isinstance(body.get("required_docs"), list) else [], ensure_ascii=False),
                    json.dumps([f"graph_events:{snapshot_id}:{event_id}"], ensure_ascii=False),
                    now,
                    now,
                ),
            )
            marked = graph_events.mark_backlog_filed(
                conn,
                project_id,
                snapshot_id,
                event_id,
                bug_id=bug_id,
                actor=str(body.get("actor") or "observer"),
                evidence={"source": "events.file-backlog"},
            )
            conn.commit()
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "event_id": event_id,
            "bug_id": bug_id,
            "event": marked,
        }
    finally:
        conn.close()


def _semantic_job_rows(conn, project_id: str, snapshot_id: str, *, statuses: list[str] | None = None, limit: int = 200, offset: int = 0) -> list[dict]:
    from . import reconcile_semantic_enrichment as semantic

    semantic._ensure_semantic_state_schema(conn)
    params: list[object] = [project_id, snapshot_id]
    sql = """
        SELECT node_id, status, feature_hash, file_hashes_json, feedback_round,
               batch_index, attempt_count, worker_id, claim_id, claimed_at,
               lease_expires_at, claimed_by, last_error, operation_type,
               updated_at, created_at
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ?
    """
    if statuses:
        placeholders = ", ".join("?" for _ in statuses)
        sql += f" AND status IN ({placeholders})"
        params.extend(statuses)
    sql += " ORDER BY updated_at DESC, node_id LIMIT ? OFFSET ?"
    params.extend([max(1, min(int(limit or 200), 1000)), max(0, int(offset or 0))])
    rows = conn.execute(sql, params).fetchall()
    jobs: list[dict] = []
    for row in rows:
        try:
            file_hashes = json.loads(row["file_hashes_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            file_hashes = {}
        jobs.append({
            "job_id": row["node_id"],
            "node_id": row["node_id"],
            "status": row["status"],
            "feature_hash": row["feature_hash"],
            "file_hashes": file_hashes,
            "feedback_round": row["feedback_round"],
            "batch_index": row["batch_index"],
            "attempt_count": row["attempt_count"],
            "worker_id": row["worker_id"],
            "claim_id": row["claim_id"],
            "claimed_at": row["claimed_at"],
            "lease_expires_at": row["lease_expires_at"],
            "claimed_by": row["claimed_by"],
            "last_error": row["last_error"],
            "operation_type": row["operation_type"],
            "updated_at": row["updated_at"],
            "created_at": row["created_at"],
        })
    return jobs


def _semantic_job_row(conn, project_id: str, snapshot_id: str, job_id: str) -> dict | None:
    rows = _semantic_job_rows(conn, project_id, snapshot_id, limit=1000)
    for row in rows:
        if str(row.get("job_id") or row.get("node_id") or "") == job_id:
            return row
    return None


def _semantic_job_status_counts(conn, project_id: str, snapshot_id: str) -> dict[str, int]:
    from . import reconcile_semantic_enrichment as semantic

    semantic._ensure_semantic_state_schema(conn)
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM graph_semantic_jobs
        WHERE project_id = ? AND snapshot_id = ?
        GROUP BY status
        ORDER BY status
        """,
        (project_id, snapshot_id),
    ).fetchall()
    return {str(row["status"] or ""): int(row["count"] or 0) for row in rows}


def _edge_semantic_job_status(event: dict) -> str:
    event_status = str(event.get("status") or "").strip()
    if event_status in {"cancelled", "failed", "rejected", "stale"}:
        return event_status
    # MF 2026-05-11: ai_reviewing is the worker's interstitial state between
    # claim and AI completion. Expose it as "running" so the dashboard's
    # operations queue shows the row as in flight instead of stuck on
    # ai_pending. semantic_worker.update_event_status sets this before
    # calling AI and the post-AI create_event writes a fresh row with
    # status=proposed.
    if event_status == "ai_reviewing":
        return "running"
    if event_status == "proposed":
        return "pending_review"
    event_type = str(event.get("event_type") or "").strip()
    if event_type == "edge_semantic_enriched":
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        semantic_payload = payload.get("semantic_payload") if isinstance(payload.get("semantic_payload"), dict) else {}
        evidence = (
            semantic_payload.get("evidence")
            if isinstance(semantic_payload.get("evidence"), dict)
            else {}
        )
        if str(evidence.get("source") or "") == "edge_semantic_rule":
            return "rule_complete"
        return "ai_complete"
    return "ai_pending"


def _edge_semantic_job_from_event(event: dict) -> dict:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    semantic_payload = payload.get("semantic_payload") if isinstance(payload.get("semantic_payload"), dict) else {}
    evidence = (
        semantic_payload.get("evidence")
        if isinstance(semantic_payload.get("evidence"), dict)
        else {}
    )
    return {
        "job_id": event.get("event_id", ""),
        "event_id": event.get("event_id", ""),
        "target_scope": "edge",
        "edge_id": event.get("target_id", ""),
        "target_id": event.get("target_id", ""),
        "status": _edge_semantic_job_status(event),
        "event_status": event.get("status", ""),
        "event_type": event.get("event_type", ""),
        "edge_context": payload.get("edge_context") if isinstance(payload.get("edge_context"), dict) else {},
        "semantic": semantic_payload,
        "semantic_source": str(evidence.get("source") or ""),
        "requires_ai": _edge_semantic_job_status(event) != "ai_complete",
        "operator_request": payload.get("operator_request") if isinstance(payload.get("operator_request"), dict) else {},
        "created_at": event.get("created_at", ""),
        "updated_at": event.get("updated_at", ""),
    }


def _edge_semantic_event_row(conn, project_id: str, snapshot_id: str, job_id: str) -> dict | None:
    from . import graph_events

    job_id = str(job_id or "").strip()
    if not job_id:
        return None
    for candidate in _semantic_edge_id_variants(job_id):
        event = graph_events.get_event(conn, project_id, snapshot_id, candidate)
        if event and event.get("target_type") == "edge" and event.get("event_type") in {
            "edge_semantic_requested",
            "edge_semantic_enriched",
        }:
            return event
        events = graph_events.list_events(
            conn,
            project_id,
            snapshot_id,
            event_types=["edge_semantic_requested", "edge_semantic_enriched"],
            target_type="edge",
            target_id=candidate,
            limit=1000,
        )
        if events:
            return events[-1]
    return None


def _edge_semantic_job_row(conn, project_id: str, snapshot_id: str, job_id: str) -> dict | None:
    event = _edge_semantic_event_row(conn, project_id, snapshot_id, job_id)
    return _edge_semantic_job_from_event(event) if event else None


def _edge_semantic_job_rows(
    conn,
    project_id: str,
    snapshot_id: str,
    *,
    statuses: list[str] | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    from . import graph_events

    events = graph_events.list_events(
        conn,
        project_id,
        snapshot_id,
        event_types=["edge_semantic_requested", "edge_semantic_enriched"],
        target_type="edge",
        limit=1000,
    )
    latest_by_edge: dict[str, dict] = {}
    for event in events:
        edge_id = str(event.get("target_id") or "").strip()
        if edge_id:
            latest_by_edge[edge_id] = event
    wanted = {str(status or "") for status in statuses or [] if str(status or "")}
    rows: list[dict] = []
    for event in latest_by_edge.values():
        status = _edge_semantic_job_status(event)
        if wanted and status not in wanted:
            continue
        rows.append(_edge_semantic_job_from_event(event))
    rows.sort(key=lambda row: (str(row.get("updated_at") or ""), str(row.get("edge_id") or "")), reverse=True)
    start = max(0, int(offset or 0))
    end = start + max(1, min(int(limit or 200), 1000))
    return rows[start:end]


def _edge_semantic_job_status_counts(conn, project_id: str, snapshot_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in _edge_semantic_job_rows(conn, project_id, snapshot_id, limit=1000):
        status = str(row.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _semantic_queued_op(job: dict, operation_type: str) -> dict[str, str]:
    if operation_type == "edge_semantic":
        target_id = str(job.get("edge_id") or job.get("target_id") or "").strip()
        return {
            "operation_id": f"edge-semantic:{target_id}",
            "operation_type": "edge_semantic",
            "target_scope": "edge",
            "target_id": target_id,
            "job_id": str(job.get("job_id") or target_id),
        }
    target_id = str(job.get("node_id") or job.get("target_id") or job.get("job_id") or "").strip()
    if operation_type == "ai_summary":
        return {
            "operation_id": f"ai-summary:{target_id}",
            "operation_type": "ai_summary",
            "target_scope": "node",
            "target_id": target_id,
            "job_id": str(job.get("job_id") or target_id),
        }
    return {
        "operation_id": f"node-semantic:{target_id}",
        "operation_type": "node_semantic",
        "target_scope": "node",
        "target_id": target_id,
        "job_id": str(job.get("job_id") or target_id),
    }


def _semantic_queued_ops(jobs: list[dict], operation_type: str) -> list[dict[str, str]]:
    ops: list[dict[str, str]] = []
    seen: set[str] = set()
    for job in jobs:
        op = _semantic_queued_op(job, operation_type)
        key = str(op.get("operation_id") or "")
        if key and key not in seen:
            seen.add(key)
            ops.append(op)
    return ops


def _semantic_cancel_status_bucket(status: str) -> str:
    normalized = _normalize_operation_status(status)
    if normalized in {"ai_pending", "queued", "pending_ai"}:
        return "queued"
    if normalized in {"running", "ai_running", "claimed", "ai_reviewing"}:
        return "running"
    if normalized in {
        "complete",
        "ai_complete",
        "failed",
        "ai_failed",
        "cancelled",
        "rejected",
        "rule_complete",
    }:
        return "terminal"
    return "other"


def _semantic_cancel_requested_bucket(status: str | None) -> str:
    value = str(status or "").strip().lower().replace("-", "_")
    if value in {"queued", "pending", "ai_pending", "pending_ai"}:
        return "queued"
    if value in {"running", "claimed", "ai_reviewing"}:
        return "running"
    return value


def _semantic_cancel_kind_allowed(kind: str, operation_type: str, target_scope: str) -> bool:
    operation_type = str(operation_type or "").strip().lower().replace("-", "_")
    target_scope = str(target_scope or "").strip().lower().replace("-", "_")
    if operation_type == "ai_summary":
        if kind != "node":
            return False
    elif operation_type and operation_type != f"{kind}_semantic":
        return False
    if target_scope in {"edge", "edges"}:
        return kind == "edge"
    if target_scope in {"node", "nodes", "subtree", "snapshot"}:
        return kind == "node"
    return True


def _semantic_cancel_before_allowed(row: dict, before_ts: str) -> bool:
    if not before_ts:
        return True
    row_ts = str(row.get("updated_at") or row.get("created_at") or "").strip()
    return bool(row_ts and row_ts <= before_ts)


def _semantic_cancel_jobs(
    conn,
    project_id: str,
    snapshot_id: str,
    *,
    actor: str,
    operation_type: str = "",
    target_scope: str = "",
    before_ts: str = "",
    status: str = "",
    target_node_ids: set[str] | None = None,
    target_edge_ids: set[str] | None = None,
    source: str = "semantic_jobs_cancel_all_api",
) -> dict:
    """Cancel non-terminal, non-running semantic queue rows.

    MF-2026-05-10-011: running rows are no longer cancellable. They count
    toward `skipped_running` so the dashboard can show the operator why some
    rows survived the cancel-all click. Terminal rows (complete / failed /
    cancelled / rejected / rule_complete / ai_failed) bump `skipped_terminal`.
    """
    from . import graph_events
    from . import reconcile_semantic_enrichment as semantic

    semantic._ensure_semantic_state_schema(conn)
    requested_bucket = _semantic_cancel_requested_bucket(status)
    if requested_bucket == "running":
        # Caller asked to cancel running rows specifically — refuse and surface
        # an explicit signal rather than silently skipping every row.
        from .errors import ValidationError

        raise ValidationError(
            "running cancel is not supported (MF-2026-05-10-011); "
            "only queued rows can be cancelled"
        )
    now = _utc_now()
    cancelled: list[dict] = []
    skipped_terminal = 0
    skipped_running = 0
    matched_count = 0

    if _semantic_cancel_kind_allowed("node", operation_type, target_scope):
        node_filter = target_node_ids if target_node_ids is not None else None
        for job in _semantic_job_rows(conn, project_id, snapshot_id, limit=1000):
            node_id = str(job.get("node_id") or job.get("job_id") or "").strip()
            if node_filter is not None and node_id not in node_filter:
                continue
            if not _semantic_cancel_before_allowed(job, before_ts):
                continue
            bucket = _semantic_cancel_status_bucket(str(job.get("status") or ""))
            if bucket == "terminal":
                skipped_terminal += 1
                continue
            if bucket == "running":
                skipped_running += 1
                continue
            if bucket != "queued":
                continue
            if requested_bucket and bucket != requested_bucket:
                continue
            matched_count += 1
            conn.execute(
                """
                UPDATE graph_semantic_jobs
                SET status = 'cancelled', worker_id = '', claim_id = '', claimed_at = '',
                    lease_expires_at = '', claimed_by = '', updated_at = ?
                WHERE project_id = ? AND snapshot_id = ? AND node_id = ?
                """,
                (now, project_id, snapshot_id, node_id),
            )
            cancelled.append({
                **_semantic_queued_op(job, "node_semantic"),
                "previous_status": str(job.get("status") or ""),
                "status": "cancelled",
            })

    if _semantic_cancel_kind_allowed("edge", operation_type, target_scope):
        edge_filter = target_edge_ids if target_edge_ids is not None else None
        canonical_edge_filter: set[str] | None = None
        if edge_filter is not None:
            canonical_edge_filter = set()
            for edge_id in edge_filter:
                canonical_edge_filter.update(_semantic_edge_id_variants(edge_id))
        for job in _edge_semantic_job_rows(conn, project_id, snapshot_id, limit=1000):
            edge_id = str(job.get("edge_id") or job.get("target_id") or "").strip()
            if canonical_edge_filter is not None and edge_id not in canonical_edge_filter:
                continue
            if not _semantic_cancel_before_allowed(job, before_ts):
                continue
            bucket = _semantic_cancel_status_bucket(str(job.get("status") or ""))
            if bucket == "terminal":
                skipped_terminal += 1
                continue
            if bucket == "running":
                skipped_running += 1
                continue
            if bucket != "queued":
                continue
            if requested_bucket and bucket != requested_bucket:
                continue
            matched_count += 1
            updated = graph_events.update_event_status(
                conn,
                project_id,
                snapshot_id,
                str(job.get("event_id") or job.get("job_id") or ""),
                status="rejected",
                actor=actor,
                evidence={
                    "source": source,
                    "previous_status": str(job.get("status") or ""),
                    "edge_id": edge_id,
                },
            )
            cancelled.append({
                **_semantic_queued_op(job, "edge_semantic"),
                "event_id": str(updated.get("event_id") or ""),
                "previous_status": str(job.get("status") or ""),
                "status": "cancelled",
                "storage_status": "rejected",
            })

    return {
        "cancelled_count": len(cancelled),
        "skipped_terminal": skipped_terminal,
        "skipped_running": skipped_running,
        "matched_count": matched_count,
        "cancelled_ops": cancelled,
        "summary": {
            "node_semantic": {"by_status": _semantic_job_status_counts(conn, project_id, snapshot_id)},
            "edge_semantic": {"by_status": _edge_semantic_job_status_counts(conn, project_id, snapshot_id)},
        },
    }


def _semantic_session_job_targets(
    conn,
    project_id: str,
    snapshot_id: str,
    session_job_id: str,
) -> tuple[set[str], set[str]]:
    from . import graph_events

    node_ids: set[str] = set()
    edge_ids: set[str] = set()
    events = graph_events.list_events(
        conn,
        project_id,
        snapshot_id,
        event_types=["semantic_retry_requested", "edge_semantic_requested", "edge_semantic_enriched"],
        limit=1000,
    )
    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        event_session = str(
            payload.get("semantic_session_job_id")
            or payload.get("session_job_id")
            or ""
        )
        if event_session != session_job_id:
            continue
        target_type = str(event.get("target_type") or "").strip()
        target_id = str(event.get("target_id") or "").strip()
        if target_type == "node" and target_id:
            node_ids.add(target_id)
        elif target_type == "edge" and target_id:
            edge_ids.add(target_id)
        elif target_type == "snapshot":
            raw_ids = payload.get("session_target_ids")
            if isinstance(raw_ids, list):
                node_ids.update(str(item or "").strip() for item in raw_ids if str(item or "").strip())
    return node_ids, edge_ids


def _semantic_job_progress(counts: dict[str, int]) -> dict[str, object]:
    total = sum(int(value or 0) for value in counts.values())
    pending = int(counts.get("ai_pending", 0) or 0) + int(counts.get("pending_ai", 0) or 0)
    running = int(counts.get("running", 0) or 0)
    rule = int(counts.get("rule_complete", 0) or 0) + int(counts.get("heuristic_complete", 0) or 0)
    complete = (
        int(counts.get("ai_complete", 0) or 0)
        + int(counts.get("complete", 0) or 0)
        + int(counts.get("succeeded", 0) or 0)
    )
    failed = int(counts.get("ai_error", 0) or 0) + int(counts.get("failed", 0) or 0)
    cancelled = int(counts.get("cancelled", 0) or 0)
    terminal = complete + failed + cancelled
    return {
        "total": total,
        "pending": pending,
        "running": running,
        "complete": complete,
        "rule_complete": rule,
        "needs_ai": pending + running + rule,
        "failed": failed,
        "cancelled": cancelled,
        "terminal": terminal,
        "open": pending + running + rule,
        "completion_ratio": round(terminal / total, 4) if total else 1.0,
    }


def _semantic_jobs_operator_request(
    body: dict,
    snapshot_id: str,
    root: Path,
    *,
    project_id: str,
    target_scope: str,
    target_ids: list[str],
    layers: list[str],
    selector: dict | None = None,
) -> dict[str, object]:
    requested_by = str(
        body.get("actor")
        or body.get("created_by")
        or body.get("requested_by")
        or "dashboard_user"
    )
    query_source = str(
        body.get("query_source")
        or body.get("source")
        or body.get("request_source")
        or "dashboard"
    )
    try:
        from .reconcile_semantic_config import (
            apply_project_ai_routing,
            load_semantic_enrichment_config,
        )

        config = load_semantic_enrichment_config(
            project_root=root,
            config_path=body.get("semantic_config_path"),
        )
        config = apply_project_ai_routing(config, project_id=project_id)
        analyzer = config.summary()
    except Exception as exc:  # noqa: BLE001 - metadata should not block queue creation
        analyzer = {"error": str(exc)}
    options = body.get("options") if isinstance(body.get("options"), dict) else {}
    batch_plan = {
        "target_scope": target_scope,
        "target_ids": target_ids,
        "layers": layers,
        "selector": selector or {},
        "skip_current": bool(options.get("skip_current", body.get("skip_current", True))),
        "parallel": bool(body.get("parallel") or body.get("allow_parallel")),
        "mode": str(body.get("semantic_mode") or options.get("semantic_mode") or "enqueue_only"),
    }
    return {
        "requested_by": requested_by,
        "query_source": query_source,
        "snapshot_id": snapshot_id,
        "projection_id": str(body.get("projection_id") or ""),
        "analyzer": {
            "name": analyzer.get("analyzer", ""),
            "provider": analyzer.get("provider", ""),
            "model": analyzer.get("model", ""),
            "role": analyzer.get("role", ""),
            "source_path": analyzer.get("source_path", ""),
            "override_path": analyzer.get("override_path", ""),
            "error": analyzer.get("error", ""),
        },
        "batch_plan": batch_plan,
    }


def _project_semantic_route_ready(project_id: str) -> tuple[bool, bool]:
    """Return (has_project_config, has_semantic_route) for AI queue gating."""
    try:
        project_config = project_service.get_project_config_metadata(project_id)
    except Exception:
        return False, False
    if not isinstance(project_config, dict) or not project_config:
        return False, False
    ai_config = project_config.get("ai") if isinstance(project_config.get("ai"), dict) else {}
    routing = ai_config.get("routing") if isinstance(ai_config.get("routing"), dict) else {}
    route = routing.get("semantic") if isinstance(routing.get("semantic"), dict) else {}
    provider = str(route.get("provider") or "").strip()
    model = str(route.get("model") or "").strip()
    return True, bool(provider and model)


def _require_project_semantic_route_for_jobs(project_id: str) -> None:
    has_project_config, has_semantic_route = _project_semantic_route_ready(project_id)
    if has_project_config and not has_semantic_route:
        raise ValidationError(
            "AI enrich blocked: configure this project's semantic provider/model in AI config first."
        )


def _edge_semantic_auto_enrich_enabled(body: dict) -> bool:
    options = body.get("options") if isinstance(body.get("options"), dict) else {}
    mode = str(
        body.get("semantic_mode")
        or body.get("mode")
        or options.get("semantic_mode")
        or options.get("mode")
        or ""
    ).strip().lower().replace("-", "_")
    return bool(
        body.get("auto_enrich")
        or body.get("run")
        or body.get("run_now")
        or options.get("auto_enrich")
        or options.get("run")
        or mode in {"auto", "run", "run_now", "auto_enrich"}
    )


def _edge_semantic_auto_enrich_requests_ai(body: dict) -> bool:
    options = body.get("options") if isinstance(body.get("options"), dict) else {}
    mode = str(
        body.get("semantic_mode")
        or body.get("mode")
        or options.get("semantic_mode")
        or options.get("mode")
        or ""
    ).strip().lower().replace("-", "_")
    return bool(
        body.get("auto_enrich")
        or body.get("run")
        or body.get("run_now")
        or options.get("auto_enrich")
        or options.get("run")
        or mode in {"run", "run_now", "auto_enrich"}
    )


def _edge_semantic_ai_body(body: dict, snapshot_id: str, *, auto_enrich: bool) -> dict:
    ai_body = {**body, "snapshot_id": snapshot_id}
    if (
        auto_enrich
        and _edge_semantic_auto_enrich_requests_ai(body)
        and _semantic_use_ai_from_body(ai_body) is None
    ):
        ai_body["semantic_use_ai"] = True
    return ai_body


def _edge_semantic_instructions(project_id: str, root: Path, body: dict) -> dict:
    try:
        from .reconcile_semantic_config import (
            apply_project_ai_routing,
            load_semantic_enrichment_config,
        )

        config = load_semantic_enrichment_config(
            project_root=root,
            config_path=body.get("semantic_config_path"),
        )
        config = apply_project_ai_routing(
            config,
            project_id=project_id,
        )
        return config.to_instruction_payload("edge")
    except Exception as exc:  # noqa: BLE001 - queue metadata should not block dashboard actions
        return {
            "job_type": "edge",
            "analyzer_role": "reconcile_edge_semantic_analyzer",
            "role": "reconcile_edge_semantic_analyzer",
            "mutate_graph_topology": False,
            "error": str(exc),
        }


def _edge_semantic_rule_payload(edge_context: dict, instructions: dict, ai_response: dict | None = None) -> dict:
    has_ai_response = isinstance(ai_response, dict) and not ai_response.get("_ai_error")
    if has_ai_response:
        payload = {
            key: value for key, value in ai_response.items()
            if not str(key).startswith("_") and value not in (None, "", [], {})
        }
    else:
        payload = {}
    src = str(edge_context.get("src") or edge_context.get("source") or "").strip()
    dst = str(edge_context.get("dst") or edge_context.get("target") or "").strip()
    edge_type = str(edge_context.get("edge_type") or edge_context.get("type") or "depends_on").strip() or "depends_on"
    relation_labels = {
        "depends_on": "depends on",
        "imports": "imports",
        "calls": "calls",
        "uses": "uses",
        "tests": "tests",
        "documents": "documents",
    }
    relation = relation_labels.get(edge_type, edge_type.replace("_", " "))
    payload.setdefault("relation_purpose", f"{src or 'source'} {relation} {dst or 'target'}.")
    payload.setdefault("confidence", 0.55)
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    evidence.setdefault("source", "semantic_ai" if has_ai_response else "edge_semantic_rule")
    evidence.setdefault("edge_type", edge_type)
    evidence.setdefault("src", src)
    evidence.setdefault("dst", dst)
    payload["evidence"] = evidence
    payload.setdefault("analyzer_role", instructions.get("analyzer_role") or instructions.get("role") or "")
    payload.setdefault("job_type", "edge")
    return payload


def _edge_semantic_payload_source(payload: dict) -> str:
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
    return str(evidence.get("source") or "").strip()


def _normalize_explicit_edge_semantic_payload(payload: dict) -> dict:
    normalized = dict(payload or {})
    evidence = normalized.get("evidence") if isinstance(normalized.get("evidence"), dict) else {}
    evidence = dict(evidence)
    evidence.setdefault("source", "semantic_ai")
    normalized["evidence"] = evidence
    normalized.setdefault("job_type", "edge")
    return normalized


def _edge_semantic_identity(
    conn,
    project_id: str,
    snapshot_id: str,
    raw_edge: dict,
    edge_context: dict,
) -> tuple[str, str]:
    from . import graph_events
    from . import graph_snapshot_store as store

    src_node_id = str(edge_context.get("src") or raw_edge.get("src") or raw_edge.get("source") or "")
    dst_node_id = str(edge_context.get("dst") or raw_edge.get("dst") or raw_edge.get("target") or "")
    edge_for_hash = dict(raw_edge or {})
    edge_for_hash.setdefault("src", src_node_id)
    edge_for_hash.setdefault("dst", dst_node_id)
    edge_for_hash.setdefault(
        "edge_type",
        str(edge_context.get("edge_type") or raw_edge.get("edge_type") or raw_edge.get("type") or "depends_on"),
    )
    src_node_meta: dict | None = None
    dst_node_meta: dict | None = None
    try:
        nodes = store.list_graph_snapshot_nodes(
            conn,
            project_id,
            snapshot_id,
            include_semantic=False,
            limit=5000,
        )
        nodes_by_id = {
            str(node.get("node_id") or node.get("id") or ""): node
            for node in nodes
        }
        src_node_meta = nodes_by_id.get(src_node_id)
        dst_node_meta = nodes_by_id.get(dst_node_id)
    except Exception:
        pass
    return (
        graph_events.stable_edge_key_for_edge(edge_for_hash, src_node_meta, dst_node_meta),
        graph_events.edge_signature_hash_for_edge(edge_for_hash, src_node_meta, dst_node_meta),
    )


def _write_edge_semantic_pending_review(
    conn,
    project_id: str,
    snapshot_id: str,
    *,
    edge_id: str,
    stable_edge_key: str,
    edge_signature_hash: str,
    semantic_payload: dict,
    source_event_id: str,
) -> None:
    from . import reconcile_semantic_enrichment as semantic

    semantic._ensure_semantic_state_schema(conn)
    semantic_entry = {
        "edge_id": edge_id,
        "stable_edge_key": stable_edge_key,
        "edge_signature_hash": edge_signature_hash,
        "semantic_payload": semantic_payload,
        "status": "pending_review",
        "source_event_id": source_event_id,
        "updated_at": "",
    }
    conn.execute(
        """
        INSERT INTO graph_semantic_edges
          (project_id, snapshot_id, edge_id, status,
           edge_signature_hash, semantic_json,
           source_event_id, feedback_round, batch_index, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_id, snapshot_id, edge_id) DO UPDATE SET
          status = excluded.status,
          edge_signature_hash = excluded.edge_signature_hash,
          semantic_json = excluded.semantic_json,
          source_event_id = excluded.source_event_id,
          updated_at = excluded.updated_at
        """,
        (
            project_id,
            snapshot_id,
            edge_id,
            "pending_review",
            edge_signature_hash,
            json.dumps(semantic_entry, ensure_ascii=False),
            source_event_id,
            0,
            None,
            "",
        ),
    )


def _submit_edge_semantic_review_feedback(
    project_id: str,
    snapshot_id: str,
    *,
    edge_id: str,
    event_id: str,
    actor: str,
    source: str,
) -> None:
    from . import reconcile_feedback

    reconcile_feedback.submit_feedback_item(
        project_id,
        snapshot_id,
        feedback_kind=reconcile_feedback.KIND_NEEDS_OBSERVER_DECISION,
        issue={
            "issue": f"AI edge semantic enrichment generated for {edge_id} — awaiting operator review",
            "target_id": edge_id,
            "target_type": "edge",
            "priority": "P3",
            "evidence": {
                "source": source,
                "edge_id": edge_id,
                "linked_event_ids": [event_id] if event_id else [],
            },
        },
        actor=actor,
    )


def _publish_semantic_job_enqueued(
    project_id: str,
    snapshot_id: str,
    queued_count: int,
    *,
    target_scope: str = "node",
    source: str,
) -> None:
    if int(queued_count or 0) <= 0:
        return
    try:
        from . import event_bus

        event_bus.publish(
            "semantic_job.enqueued",
            {
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "queued_count": int(queued_count or 0),
                "target_scope": target_scope,
                "source": source,
            },
        )
    except Exception:
        pass


def _edge_semantic_ai_payload(
    *,
    project_id: str,
    snapshot_id: str,
    edge: dict,
    edge_context: dict,
    operator_request: dict,
    instructions: dict,
) -> dict:
    return {
        "schema_version": 1,
        "project_id": project_id,
        "snapshot_id": snapshot_id,
        "edge": edge,
        "edge_context": edge_context,
        "operator_request": operator_request,
        "instructions": instructions,
        "output_contract": {
            "required": ["relation_purpose", "confidence", "evidence"],
            "optional": ["risk", "directionality", "semantic_label", "open_issues"],
        },
    }


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic/jobs")
def handle_graph_governance_snapshot_semantic_jobs_list(ctx: RequestContext):
    """List semantic enrichment queue rows for dashboard/job controls."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    statuses = _query_statuses(ctx.query)
    limit = _query_int(ctx.query, "limit", 200)
    offset = _query_int(ctx.query, "offset", 0)
    target_scope = str(
        ctx.query.get("target_scope")
        or ctx.query.get("scope")
        or "node"
    ).strip().lower().replace("-", "_")
    include_edge_events = (
        target_scope in {"edge", "edges", "all"}
        or _query_bool(ctx.query, "include_edge_events", False)
    )
    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.semantic.jobs.list")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        if target_scope in {"edge", "edges"}:
            edge_jobs = _edge_semantic_job_rows(
                conn,
                project_id,
                snapshot_id,
                statuses=statuses or None,
                limit=limit,
                offset=offset,
            )
            edge_counts = _edge_semantic_job_status_counts(conn, project_id, snapshot_id)
            return {
                "ok": True,
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "target_scope": "edge",
                "count": len(edge_jobs),
                "jobs": edge_jobs,
                "summary": {"by_status": edge_counts, "progress": _semantic_job_progress(edge_counts)},
            }
        jobs = _semantic_job_rows(
            conn,
            project_id,
            snapshot_id,
            statuses=statuses or None,
            limit=limit,
            offset=offset,
        )
        counts = _semantic_job_status_counts(conn, project_id, snapshot_id)
        response = {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "count": len(jobs),
            "jobs": jobs,
            "summary": {"by_status": counts, "progress": _semantic_job_progress(counts)},
        }
        if include_edge_events:
            edge_jobs = _edge_semantic_job_rows(
                conn,
                project_id,
                snapshot_id,
                limit=limit,
                offset=offset,
            )
            edge_counts = _edge_semantic_job_status_counts(conn, project_id, snapshot_id)
            response["edge_jobs"] = edge_jobs
            response["edge_summary"] = {
                "by_status": edge_counts,
                "progress": _semantic_job_progress(edge_counts),
            }
        return response
    finally:
        conn.close()


@route("GET", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic/jobs/{job_id}")
def handle_graph_governance_snapshot_semantic_job_get(ctx: RequestContext):
    """Fetch one semantic queue row by job_id/node_id."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    job_id = ctx.path_params["job_id"]

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.semantic.jobs.get")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        job = _semantic_job_row(conn, project_id, snapshot_id, job_id)
        if not job:
            job = _edge_semantic_job_row(conn, project_id, snapshot_id, job_id)
        if not job:
            from .errors import ValidationError
            raise ValidationError(f"semantic job not found: {job_id}")
        return {"ok": True, "project_id": project_id, "snapshot_id": snapshot_id, "job": job}
    finally:
        conn.close()


def _semantic_job_status_update(ctx: RequestContext, *, status: str, action: str):
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    job_id = ctx.path_params["job_id"]
    body = ctx.body
    from . import graph_events
    from . import reconcile_semantic_enrichment as semantic
    from .db import sqlite_write_lock
    from .errors import ValidationError

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, action)
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        semantic._ensure_semantic_state_schema(conn)
        with sqlite_write_lock():
            job = _semantic_job_row(conn, project_id, snapshot_id, job_id)
            if not job:
                edge_event = _edge_semantic_event_row(conn, project_id, snapshot_id, job_id)
                if not edge_event:
                    if status == "cancelled":
                        node_ids, edge_ids = _semantic_session_job_targets(
                            conn,
                            project_id,
                            snapshot_id,
                            job_id,
                        )
                        if node_ids or edge_ids:
                            result = _semantic_cancel_jobs(
                                conn,
                                project_id,
                                snapshot_id,
                                actor=str(body.get("actor") or "dashboard_user"),
                                target_node_ids=node_ids,
                                target_edge_ids=edge_ids,
                                source="semantic_jobs_session_cancel_api",
                            )
                            conn.commit()
                            return {
                                "ok": True,
                                "project_id": project_id,
                                "snapshot_id": snapshot_id,
                                "job_id": job_id,
                                "status": "cancelled",
                                **result,
                            }
                    raise ValidationError(f"semantic job not found: {job_id}")
                if status == "pending_ai":
                    payload = edge_event.get("payload") if isinstance(edge_event.get("payload"), dict) else {}
                    edge_context = (
                        payload.get("edge_context")
                        if isinstance(payload.get("edge_context"), dict)
                        else {}
                    )
                    edge = payload.get("edge") if isinstance(payload.get("edge"), dict) else {}
                    event = graph_events.create_event(
                        conn,
                        project_id,
                        snapshot_id,
                        event_type="edge_semantic_requested",
                        event_kind="semantic_job",
                        target_type="edge",
                        target_id=str(edge_event.get("target_id") or job_id),
                        status="observed",
                        payload={
                            "edge": edge,
                            "edge_context": edge_context,
                            "semantic_payload": {},
                            "operator_request": payload.get("operator_request")
                            if isinstance(payload.get("operator_request"), dict)
                            else {},
                            "retry_of_event_id": edge_event.get("event_id", ""),
                        },
                        evidence={
                            "source": "semantic_jobs_api_retry",
                            "previous_status": edge_event.get("status", ""),
                        },
                        created_by=str(body.get("actor") or "dashboard_user"),
                    )
                else:
                    # MF-2026-05-10-011: running edge jobs are not cancellable.
                    edge_status_bucket = _semantic_cancel_status_bucket(
                        str(edge_event.get("status") or "")
                    )
                    if edge_status_bucket == "running":
                        conn.commit()
                        return 409, {
                            "ok": False,
                            "error": "cancel_running_not_supported",
                            "project_id": project_id,
                            "snapshot_id": snapshot_id,
                            "job_id": job_id,
                            "current_status": edge_event.get("status", ""),
                            "message": "running edge semantic jobs cannot be cancelled (MF-2026-05-10-011)",
                        }
                    event = graph_events.update_event_status(
                        conn,
                        project_id,
                        snapshot_id,
                        str(edge_event.get("event_id") or job_id),
                        status="rejected",
                        actor=str(body.get("actor") or "dashboard_user"),
                        evidence={
                            "source": "semantic_jobs_api_cancel",
                            "requested_status": status,
                            "previous_status": edge_event.get("status", ""),
                        },
                    )
                conn.commit()
                updated_edge = _edge_semantic_job_row(
                    conn,
                    project_id,
                    snapshot_id,
                    str(event.get("event_id") or event.get("target_id") or job_id),
                )
                return {
                    "ok": True,
                    "project_id": project_id,
                    "snapshot_id": snapshot_id,
                    "job": updated_edge,
                    "event": event,
                    "summary": {"by_status": _edge_semantic_job_status_counts(conn, project_id, snapshot_id)},
                }
            now = _utc_now()
            if status == "cancelled":
                # MF-2026-05-10-011: running node jobs are not cancellable.
                node_status_bucket = _semantic_cancel_status_bucket(
                    str(job.get("status") or "")
                )
                if node_status_bucket == "running":
                    conn.commit()
                    return 409, {
                        "ok": False,
                        "error": "cancel_running_not_supported",
                        "project_id": project_id,
                        "snapshot_id": snapshot_id,
                        "job_id": job_id,
                        "current_status": job.get("status", ""),
                        "message": "running semantic jobs cannot be cancelled (MF-2026-05-10-011)",
                    }
                if node_status_bucket == "terminal":
                    conn.commit()
                    return {
                        "ok": True,
                        "project_id": project_id,
                        "snapshot_id": snapshot_id,
                        "job_id": job_id,
                        "status": "noop_terminal",
                        "current_status": job.get("status", ""),
                        "message": "row already terminal — cancel is a no-op",
                    }
            if status == "pending_ai":
                conn.execute(
                    """
                    UPDATE graph_semantic_jobs
                    SET status = ?, worker_id = '', claim_id = '', claimed_at = '',
                        lease_expires_at = '', claimed_by = '', last_error = '', updated_at = ?
                    WHERE project_id = ? AND snapshot_id = ? AND node_id = ?
                    """,
                    (status, now, project_id, snapshot_id, job_id),
                )
                event_type = "semantic_retry_requested"
            else:
                conn.execute(
                    """
                    UPDATE graph_semantic_jobs
                    SET status = ?, worker_id = '', claim_id = '', claimed_at = '',
                        lease_expires_at = '', claimed_by = '', updated_at = ?
                    WHERE project_id = ? AND snapshot_id = ? AND node_id = ?
                    """,
                    (status, now, project_id, snapshot_id, job_id),
                )
                event_type = "semantic_stale"
            event = graph_events.create_event(
                conn,
                project_id,
                snapshot_id,
                event_type=event_type,
                event_kind="semantic_job",
                target_type="node",
                target_id=job_id,
                status="observed",
                feature_hash=str(job.get("feature_hash") or ""),
                file_hashes=job.get("file_hashes") if isinstance(job.get("file_hashes"), dict) else {},
                payload={"job_action": action.rsplit(".", 1)[-1], "new_status": status},
                evidence={"source": "semantic_jobs_api", "previous_status": job.get("status", "")},
                created_by=str(body.get("actor") or "dashboard_user"),
            )
            conn.commit()
        updated = _semantic_job_row(conn, project_id, snapshot_id, job_id)
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "job": updated,
            "event": event,
            "summary": {"by_status": _semantic_job_status_counts(conn, project_id, snapshot_id)},
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic/jobs/cancel-all")
def handle_graph_governance_snapshot_semantic_jobs_cancel_all(ctx: RequestContext):
    """Cancel non-terminal semantic queue rows with dashboard-friendly filters."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body if isinstance(ctx.body, dict) else {}
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.semantic.jobs.cancel_all")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        with sqlite_write_lock():
            result = _semantic_cancel_jobs(
                conn,
                project_id,
                snapshot_id,
                actor=str(body.get("actor") or body.get("created_by") or "dashboard_user"),
                operation_type=str(body.get("operation_type") or ""),
                target_scope=str(body.get("target_scope") or ""),
                before_ts=str(body.get("before_ts") or ""),
                status=str(body.get("status") or ""),
            )
            conn.commit()
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            **result,
        }
    finally:
        conn.close()


@route(
    "POST",
    "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic/jobs/clear-terminal",
)
def handle_graph_governance_snapshot_semantic_jobs_clear_terminal(ctx: RequestContext):
    """Physically drain terminal semantic queue rows so the dashboard isn't
    swamped with cancelled / completed / failed history (MF-2026-05-10-011).

    Body filters (all optional, AND-combined):
      operation_type : "node_semantic" | "edge_semantic"
      before_ts      : ISO8601 — only rows with updated_at/created_at <= before_ts
      statuses       : list of statuses to clear; defaults to the full terminal set
                       ("cancelled", "complete", "ai_complete", "failed",
                        "ai_failed", "rejected", "rule_complete")

    Behaviour:
      - node_semantic rows: physical DELETE from graph_semantic_jobs.
      - edge_semantic rows: events are audit history and are NOT deleted; the
        dashboard already filters terminal events out of its live queue view.
        We report how many edge rows match so the operator sees coverage.
    """
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body if isinstance(ctx.body, dict) else {}
    from . import reconcile_semantic_enrichment as semantic
    from .db import sqlite_write_lock

    TERMINAL_DEFAULT = {
        "cancelled",
        "complete",
        "ai_complete",
        "failed",
        "ai_failed",
        "rejected",
        "rule_complete",
    }
    raw_statuses = body.get("statuses") or body.get("status")
    if isinstance(raw_statuses, str):
        requested_statuses = {raw_statuses.strip().lower()}
    elif isinstance(raw_statuses, list):
        requested_statuses = {str(s).strip().lower() for s in raw_statuses if str(s).strip()}
    else:
        requested_statuses = set(TERMINAL_DEFAULT)
    # Refuse to delete anything that isn't terminal.
    invalid = requested_statuses - TERMINAL_DEFAULT
    if invalid:
        from .errors import ValidationError

        raise ValidationError(
            f"clear-terminal only accepts terminal statuses; invalid: {sorted(invalid)}"
        )
    if not requested_statuses:
        requested_statuses = set(TERMINAL_DEFAULT)

    op_type_filter = str(body.get("operation_type") or "").strip().lower().replace("-", "_")
    before_ts = str(body.get("before_ts") or "").strip()

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(
            ctx, conn, "graph-governance.snapshot.semantic.jobs.clear_terminal"
        )
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        semantic._ensure_semantic_state_schema(conn)
        deleted_nodes = 0
        deleted_node_rows: list[dict] = []
        edge_matched = 0
        edge_matched_rows: list[dict] = []
        with sqlite_write_lock():
            if op_type_filter in {"", "node_semantic"}:
                for job in _semantic_job_rows(conn, project_id, snapshot_id, limit=5000):
                    status_val = str(job.get("status") or "").strip().lower()
                    if status_val not in requested_statuses:
                        continue
                    if not _semantic_cancel_before_allowed(job, before_ts):
                        continue
                    node_id = str(job.get("node_id") or job.get("job_id") or "").strip()
                    if not node_id:
                        continue
                    conn.execute(
                        """
                        DELETE FROM graph_semantic_jobs
                        WHERE project_id = ? AND snapshot_id = ? AND node_id = ?
                        """,
                        (project_id, snapshot_id, node_id),
                    )
                    deleted_nodes += 1
                    deleted_node_rows.append({
                        "operation_id": f"node-semantic:{node_id}",
                        "operation_type": "node_semantic",
                        "target_id": node_id,
                        "previous_status": status_val,
                    })
            if op_type_filter in {"", "edge_semantic"}:
                # Edge rows live in graph_events as audit history. We do not
                # physically remove events here — counting them gives the
                # operator visibility, and the dashboard already hides terminal
                # edge events from the live queue.
                for job in _edge_semantic_job_rows(conn, project_id, snapshot_id, limit=5000):
                    status_val = str(job.get("status") or "").strip().lower()
                    if status_val not in requested_statuses:
                        continue
                    if not _semantic_cancel_before_allowed(job, before_ts):
                        continue
                    edge_matched += 1
                    edge_matched_rows.append({
                        "operation_id": f"edge-semantic:{job.get('edge_id') or job.get('event_id') or ''}",
                        "operation_type": "edge_semantic",
                        "target_id": str(job.get("edge_id") or job.get("target_id") or ""),
                        "previous_status": status_val,
                        "note": "edge events kept as audit history",
                    })
            conn.commit()
        return {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "deleted_count": deleted_nodes,
            "edge_audit_matched": edge_matched,
            "requested_statuses": sorted(requested_statuses),
            "deleted_node_rows": deleted_node_rows[:200],
            "edge_audit_rows": edge_matched_rows[:200],
            "summary": {
                "node_semantic": {"by_status": _semantic_job_status_counts(conn, project_id, snapshot_id)},
                "edge_semantic": {"by_status": _edge_semantic_job_status_counts(conn, project_id, snapshot_id)},
            },
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic/jobs/{job_id}/cancel")
def handle_graph_governance_snapshot_semantic_job_cancel(ctx: RequestContext):
    return _semantic_job_status_update(
        ctx,
        status="cancelled",
        action="graph-governance.snapshot.semantic.jobs.cancel",
    )


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic/jobs/{job_id}/retry")
def handle_graph_governance_snapshot_semantic_job_retry(ctx: RequestContext):
    return _semantic_job_status_update(
        ctx,
        status="pending_ai",
        action="graph-governance.snapshot.semantic.jobs.retry",
    )


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic/jobs")
def handle_graph_governance_snapshot_semantic_jobs_create(ctx: RequestContext):
    """Queue semantic enrichment work using the existing semantic state substrate.

    This is the dashboard-facing compatibility endpoint. It intentionally
    enqueues DB-backed semantic rows without running AI inline.
    """
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    root = _graph_governance_project_root(project_id, body)
    from . import reconcile_semantic_enrichment as semantic
    from .db import sqlite_write_lock

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.semantic.jobs.create")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        _require_project_semantic_route_for_jobs(project_id)
        dry_run = _semantic_jobs_dry_run(body)
        job_type = str(body.get("job_type") or "semantic_enrichment").strip().lower().replace("-", "_")
        if job_type == "semantic_summary":
            from . import graph_events
            from . import reconcile_semantic_summary as summary
            from .errors import ValidationError

            target_scope = _semantic_jobs_target_scope(body)
            if target_scope in {"edge", "edges", "snapshot"}:
                raise ValidationError("semantic_summary supports only node or subtree targets")
            target_ids = _semantic_jobs_target_ids(
                body.get("target_ids")
                or body.get("target_id")
                or body.get("semantic_node_ids")
                or body.get("node_ids")
                or body.get("node_id")
            )
            planned_target_ids = summary.validate_summary_targets(
                conn,
                project_id,
                snapshot_id,
                target_scope=target_scope,
                target_ids=target_ids,
            )
            options = _semantic_jobs_options(body)
            operator_request = _semantic_jobs_operator_request(
                body,
                snapshot_id,
                root,
                project_id=project_id,
                target_scope=target_scope,
                target_ids=planned_target_ids,
                layers=[],
            )
            operator_request["operation_type"] = summary.SUMMARY_OPERATION_TYPE
            operator_request["summary_options"] = options
            session_job_id = f"semantic-summary-jobs-{snapshot_id}-{uuid.uuid4().hex[:8]}"
            if dry_run:
                return 202, {
                    "ok": True,
                    "project_id": project_id,
                    "snapshot_id": snapshot_id,
                    "job_id": session_job_id,
                    "job_type": "semantic_summary",
                    "target_scope": target_scope,
                    "status": "dry_run",
                    "dry_run": True,
                    "queued_count": 0,
                    "planned_count": len(planned_target_ids),
                    "operator_request": operator_request,
                    "batch_plan": operator_request["batch_plan"],
                    "jobs": [],
                    "queued_ops": [],
                }
            with sqlite_write_lock():
                jobs = summary.queue_summary_jobs(
                    conn,
                    project_id,
                    snapshot_id,
                    target_ids=planned_target_ids,
                    created_by=str(body.get("actor") or body.get("created_by") or "dashboard_user"),
                )
                for node_id in planned_target_ids:
                    graph_events.create_event(
                        conn,
                        project_id,
                        snapshot_id,
                        event_type="semantic_retry_requested",
                        event_kind="semantic_job",
                        target_type="node",
                        target_id=node_id,
                        status="observed",
                        operation_type=summary.SUMMARY_OPERATION_TYPE,
                        payload={
                            "semantic_session_job_id": session_job_id,
                            "job_type": "semantic_summary",
                            "operator_request": operator_request,
                            "batch_plan": operator_request["batch_plan"],
                            "selector": {"node_ids": planned_target_ids},
                            "summary_options": options,
                        },
                        evidence={"source": "semantic_jobs_api_summary"},
                        created_by=str(body.get("actor") or body.get("created_by") or "dashboard_user"),
                    )
                conn.commit()
            queued_count = len(jobs)
            _publish_semantic_job_enqueued(
                project_id,
                snapshot_id,
                queued_count,
                target_scope="node",
                source="semantic_jobs_create_api_summary",
            )
            persisted_jobs = _semantic_job_rows(conn, project_id, snapshot_id, limit=1000)
            target_set = set(planned_target_ids)
            persisted_jobs = [
                job for job in persisted_jobs
                if str(job.get("node_id") or "") in target_set
            ][: int(body.get("limit") or 200)]
            counts = _semantic_job_status_counts(conn, project_id, snapshot_id)
            return 202, {
                "ok": True,
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "job_id": session_job_id,
                "job_type": "semantic_summary",
                "target_scope": target_scope,
                "status": "queued",
                "dry_run": False,
                "queued_count": queued_count,
                "planned_count": len(planned_target_ids),
                "operator_request": operator_request,
                "batch_plan": operator_request["batch_plan"],
                "summary": {"by_status": counts, "progress": _semantic_job_progress(counts)},
                "jobs": persisted_jobs,
                "queued_ops": _semantic_queued_ops(persisted_jobs, summary.SUMMARY_OPERATION_TYPE),
            }
        if _semantic_jobs_target_scope(body) in {"edge", "edges"}:
            from . import graph_events
            from .errors import ValidationError

            edge_targets = _semantic_jobs_edge_targets(
                body,
                conn=conn,
                project_id=project_id,
                snapshot_id=snapshot_id,
            )
            if not edge_targets:
                raise ValidationError(
                    "target_ids, edge_ids, edges, or selector/all_eligible is required for edge semantic jobs"
                )
            semantic_by_edge: dict[str, dict] = {}
            raw_semantics = body.get("edge_semantics") or body.get("semantic_edges")
            if isinstance(raw_semantics, list):
                for raw in raw_semantics:
                    if not isinstance(raw, dict):
                        continue
                    edge_id = str(raw.get("edge_id") or raw.get("id") or "").strip()
                    if not edge_id:
                        edge_id = _semantic_edge_id(raw)
                    if edge_id:
                        semantic_by_edge[edge_id] = raw
            operator_request = _semantic_jobs_operator_request(
                body,
                snapshot_id,
                root,
                project_id=project_id,
                target_scope="edge",
                target_ids=[edge["edge_id"] for edge in edge_targets],
                layers=[],
            )
            session_job_id = f"edge-semantic-jobs-{snapshot_id}-{uuid.uuid4().hex[:8]}"
            if dry_run:
                return 202, {
                    "ok": True,
                    "project_id": project_id,
                    "snapshot_id": snapshot_id,
                    "job_id": session_job_id,
                    "target_scope": "edge",
                    "status": "dry_run",
                    "dry_run": True,
                    "queued_count": 0,
                    "planned_count": len(edge_targets),
                    "operator_request": operator_request,
                    "batch_plan": operator_request["batch_plan"],
                    "events": [],
                    "jobs": [],
                    "queued_ops": [],
                }
            auto_enrich = _edge_semantic_auto_enrich_enabled(body)
            instructions = _edge_semantic_instructions(project_id, root, body)
            ai_call = _semantic_ai_call_from_body(
                project_id,
                root,
                _edge_semantic_ai_body(body, snapshot_id, auto_enrich=auto_enrich),
            )
            with sqlite_write_lock():
                events = []
                requested_count = 0
                enriched_count = 0
                ai_error_count = 0
                for edge in edge_targets:
                    raw_edge = edge.get("edge") or {}
                    edge_context = {
                        "edge_id": edge["edge_id"],
                        "src": raw_edge.get("src") or raw_edge.get("source") or "",
                        "dst": raw_edge.get("dst") or raw_edge.get("target") or "",
                        "edge_type": raw_edge.get("edge_type") or raw_edge.get("type") or "",
                        "evidence": raw_edge.get("evidence")
                        if isinstance(raw_edge.get("evidence"), dict)
                        else {},
                    }
                    explicit_semantic = semantic_by_edge.get(edge["edge_id"], {})
                    should_enrich = bool(explicit_semantic or auto_enrich)
                    if not explicit_semantic:
                        requested = graph_events.create_event(
                            conn,
                            project_id,
                            snapshot_id,
                            event_type="edge_semantic_requested",
                            event_kind="semantic_job",
                            target_type="edge",
                            target_id=edge["edge_id"],
                            status="observed",
                            payload={
                                "semantic_session_job_id": session_job_id,
                                "edge": raw_edge,
                                "edge_context": edge_context,
                                "semantic_payload": {},
                                "options": body.get("options") if isinstance(body.get("options"), dict) else {},
                                "operator_request": operator_request,
                                "batch_plan": operator_request["batch_plan"],
                                "semantic_job_request": {
                                    "target_scope": "edge",
                                    "parallel": bool(body.get("parallel") or body.get("allow_parallel")),
                                },
                                "instructions": instructions,
                            },
                            evidence={"source": "semantic_jobs_api"},
                            created_by=str(body.get("actor") or body.get("created_by") or "dashboard_user"),
                        )
                        events.append(requested)
                        requested_count += 1
                    if not should_enrich:
                        continue
                    ai_response = None
                    if ai_call is not None and not explicit_semantic:
                        try:
                            ai_response = ai_call(
                                "edge",
                                _edge_semantic_ai_payload(
                                    project_id=project_id,
                                    snapshot_id=snapshot_id,
                                    edge=raw_edge,
                                    edge_context=edge_context,
                                    operator_request=operator_request,
                                    instructions=instructions,
                                ),
                            )
                        except Exception as exc:  # noqa: BLE001 - record failed edge job in events
                            ai_response = {"_ai_error": str(exc)}
                            ai_error_count += 1
                    semantic_payload = (
                        _normalize_explicit_edge_semantic_payload(explicit_semantic)
                        if explicit_semantic
                        else _edge_semantic_rule_payload(
                            edge_context,
                            instructions,
                            ai_response=ai_response,
                        )
                    )
                    ai_failed = isinstance(ai_response, dict) and bool(ai_response.get("_ai_error"))
                    semantic_source = _edge_semantic_payload_source(semantic_payload)
                    review_required = (not ai_failed) and semantic_source != "edge_semantic_rule"
                    stable_edge_key, edge_signature_hash = _edge_semantic_identity(
                        conn,
                        project_id,
                        snapshot_id,
                        raw_edge,
                        edge_context,
                    )
                    enriched = graph_events.create_event(
                        conn,
                        project_id,
                        snapshot_id,
                        event_type="edge_semantic_enriched",
                        event_kind="semantic_job",
                        target_type="edge",
                        target_id=edge["edge_id"],
                        status=(
                            graph_events.EVENT_STATUS_FAILED
                            if ai_failed
                            else graph_events.EVENT_STATUS_PROPOSED
                            if review_required
                            else graph_events.EVENT_STATUS_OBSERVED
                        ),
                        stable_node_key=stable_edge_key,
                        feature_hash=edge_signature_hash,
                        payload={
                            "semantic_session_job_id": session_job_id,
                            "edge": raw_edge,
                            "edge_context": edge_context,
                            "semantic_payload": semantic_payload,
                            "options": body.get("options") if isinstance(body.get("options"), dict) else {},
                            "operator_request": operator_request,
                            "batch_plan": operator_request["batch_plan"],
                            "semantic_job_request": {
                                "target_scope": "edge",
                                "parallel": bool(body.get("parallel") or body.get("allow_parallel")),
                                "auto_enrich": auto_enrich,
                            },
                            "instructions": instructions,
                            "ai_error": ai_response.get("_ai_error", "") if isinstance(ai_response, dict) else "",
                        },
                        evidence={
                            "source": "semantic_jobs_api_auto_enrich" if auto_enrich and not explicit_semantic else "semantic_jobs_api",
                            "ai_error": ai_response.get("_ai_error", "") if isinstance(ai_response, dict) else "",
                        },
                        created_by=str(body.get("actor") or body.get("created_by") or "dashboard_user"),
                    )
                    if review_required:
                        _write_edge_semantic_pending_review(
                            conn,
                            project_id,
                            snapshot_id,
                            edge_id=edge["edge_id"],
                            stable_edge_key=stable_edge_key,
                            edge_signature_hash=edge_signature_hash,
                            semantic_payload=semantic_payload,
                            source_event_id=str(enriched.get("event_id") or ""),
                        )
                        try:
                            _submit_edge_semantic_review_feedback(
                                project_id,
                                snapshot_id,
                                edge_id=edge["edge_id"],
                                event_id=str(enriched.get("event_id") or ""),
                                actor=str(body.get("actor") or body.get("created_by") or "dashboard_user"),
                                source="semantic_jobs_api",
                            )
                        except Exception:
                            pass
                    events.append(enriched)
                    enriched_count += 1
                conn.commit()
            jobs = _edge_semantic_job_rows(conn, project_id, snapshot_id, limit=int(body.get("limit") or 200))
            target_edge_ids = {str(edge.get("edge_id") or "") for edge in edge_targets}
            jobs = [job for job in jobs if str(job.get("edge_id") or "") in target_edge_ids]
            # MF-2026-05-10-017: publish semantic_job.enqueued so the
            # event-driven worker picks up unenriched edge_semantic_requested
            # events. Inline-enriched AI payloads are proposed for review, so
            # the worker has nothing left to claim for those rows.
            unenriched_count = requested_count - enriched_count
            _publish_semantic_job_enqueued(
                project_id,
                snapshot_id,
                unenriched_count,
                target_scope="edge",
                source="semantic_jobs_create_api_edge",
            )
            return 202, {
                "ok": True,
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "job_id": session_job_id,
                "target_scope": "edge",
                "status": "queued",
                "queued_count": requested_count,
                "enriched_count": enriched_count,
                "ai_error_count": ai_error_count,
                "operator_request": operator_request,
                "batch_plan": operator_request["batch_plan"],
                "events": events,
                "summary": {"events_by_status": graph_events.status_counts(conn, project_id, snapshot_id)},
                "jobs": jobs,
                "queued_ops": _semantic_queued_ops(jobs, "edge_semantic"),
            }
        enqueue_body = _semantic_jobs_enqueue_body(body)
        requested_target_ids = _semantic_jobs_target_ids(
            enqueue_body.get("semantic_node_ids")
            or body.get("target_ids")
            or body.get("node_ids")
            or body.get("node_id")
        )
        requested_layers = [
            str(layer or "").upper()
            for layer in (enqueue_body.get("semantic_layers") or [])
            if str(layer or "").strip()
        ]
        operator_request = _semantic_jobs_operator_request(
            body,
            snapshot_id,
            root,
            project_id=project_id,
            target_scope=_semantic_jobs_target_scope(body),
            target_ids=requested_target_ids,
            layers=requested_layers,
        )
        session_job_id = f"semantic-jobs-{snapshot_id}-{uuid.uuid4().hex[:8]}"
        if dry_run:
            planned_target_ids = _semantic_jobs_node_plan_targets(
                conn,
                project_id,
                snapshot_id,
                enqueue_body,
            )
            selector = {
                "dry_run": True,
                "node_ids": planned_target_ids,
                "semantic_node_ids": planned_target_ids,
                "semantic_layers": requested_layers,
                "semantic_ai_scope": enqueue_body.get("semantic_ai_scope") or "",
            }
            operator_request["batch_plan"]["selector"] = selector
            counts = _semantic_job_status_counts(conn, project_id, snapshot_id)
            progress = _semantic_job_progress(counts)
            return 202, {
                "ok": True,
                "project_id": project_id,
                "snapshot_id": snapshot_id,
                "job_id": session_job_id,
                "status": "dry_run",
                "dry_run": True,
                "queued_count": 0,
                "planned_count": len(planned_target_ids),
                "operator_request": operator_request,
                "batch_plan": operator_request["batch_plan"],
                "summary": {"by_status": counts, "progress": progress},
                "semantic_enrichment": {
                    "feedback_round": None,
                    "semantic_selector": selector,
                    "semantic_job_counts": counts,
                },
                "jobs": [],
                "queued_ops": [],
            }
        _semantic_jobs_resolve_stale_scope_targets(
            conn,
            project_id,
            snapshot_id,
            enqueue_body,
        )
        try:
            result = semantic.run_semantic_enrichment(
                conn,
                project_id,
                snapshot_id,
                root,
                use_ai=False,
                created_by=str(enqueue_body.get("actor") or "dashboard_user"),
                max_excerpt_chars=0,
                ai_feature_limit=0,
                **_semantic_state_kwargs_from_body(enqueue_body),
                **_semantic_selector_kwargs_from_body(enqueue_body),
                semantic_config_path=enqueue_body.get("semantic_config_path"),
                persist_feature_payloads=False,
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        from . import graph_events

        result_summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        target_ids = _semantic_jobs_target_ids(
            enqueue_body.get("semantic_node_ids")
            or body.get("target_ids")
            or body.get("node_ids")
            or body.get("node_id")
        )
        if not target_ids:
            selector = result_summary.get("semantic_selector") if isinstance(result_summary.get("semantic_selector"), dict) else {}
            target_ids = _semantic_jobs_target_ids(selector.get("node_ids"))
        operator_request["batch_plan"]["selector"] = result_summary.get("semantic_selector") or {}
        jobs_for_events = _semantic_job_rows(
            conn,
            project_id,
            snapshot_id,
            limit=1000,
        )
        job_by_node = {str(job.get("node_id") or ""): job for job in jobs_for_events}
        if target_ids:
            for node_id in target_ids:
                job = job_by_node.get(node_id, {})
                graph_events.create_event(
                    conn,
                    project_id,
                    snapshot_id,
                    event_type="semantic_retry_requested",
                    event_kind="semantic_job",
                    target_type="node",
                    target_id=node_id,
                    status="observed",
                    feature_hash=str(job.get("feature_hash") or ""),
                    file_hashes=job.get("file_hashes") if isinstance(job.get("file_hashes"), dict) else {},
                    payload={
                        "semantic_session_job_id": session_job_id,
                        "operator_request": operator_request,
                        "batch_plan": operator_request["batch_plan"],
                        "selector": result_summary.get("semantic_selector") or {},
                        "semantic_job_counts": (
                            (result_summary.get("semantic_graph_state") or {}).get("semantic_job_counts")
                            if isinstance(result_summary.get("semantic_graph_state"), dict)
                            else {}
                        ),
                    },
                    evidence={"source": "semantic_jobs_api"},
                    created_by=str(enqueue_body.get("actor") or "dashboard_user"),
                )
        else:
            graph_events.create_event(
                conn,
                project_id,
                snapshot_id,
                event_type="semantic_retry_requested",
                event_kind="semantic_job",
                target_type="snapshot",
                target_id=snapshot_id,
                status="observed",
                payload={
                    "semantic_session_job_id": session_job_id,
                    "session_target_ids": target_ids,
                    "operator_request": operator_request,
                    "batch_plan": operator_request["batch_plan"],
                    "selector": result_summary.get("semantic_selector") or {},
                    "semantic_job_counts": (
                        (result_summary.get("semantic_graph_state") or {}).get("semantic_job_counts")
                        if isinstance(result_summary.get("semantic_graph_state"), dict)
                        else {}
                    ),
                },
                evidence={"source": "semantic_jobs_api"},
                created_by=str(enqueue_body.get("actor") or "dashboard_user"),
            )
        conn.commit()
        job_limit = int(body.get("limit") or 200)
        jobs = _semantic_job_rows(
            conn,
            project_id,
            snapshot_id,
            limit=1000 if target_ids else job_limit,
        )
        if target_ids:
            target_set = set(target_ids)
            jobs = [job for job in jobs if str(job.get("node_id") or "") in target_set][:job_limit]
        counts = _semantic_job_status_counts(conn, project_id, snapshot_id)
        progress = _semantic_job_progress(counts)
        pending_jobs = [
            job for job in jobs
            if str(job.get("status") or "") in {"ai_pending", "pending_ai"}
        ]
        queued_count = (
            len(pending_jobs)
            if target_ids
            else int(result_summary.get("ai_selected_count") or 0) or len(pending_jobs)
        )
        queued_ops = _semantic_queued_ops(pending_jobs[:queued_count], "node_semantic") if queued_count else []
        # MF-2026-05-10-016: kick the event-driven semantic worker so it
        # immediately drains the newly enqueued ai_pending rows. Best-effort —
        # if EventBus or worker fails the rows still wait for startup catchup.
        _publish_semantic_job_enqueued(
            project_id,
            snapshot_id,
            queued_count,
            target_scope="node",
            source="semantic_jobs_create_api",
        )
        return 202, {
            "ok": True,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "job_id": session_job_id,
            "status": "queued",
            "dry_run": False,
            "queued_count": queued_count,
            "planned_count": queued_count,
            "operator_request": operator_request,
            "batch_plan": operator_request["batch_plan"],
            "summary": {"by_status": counts, "progress": progress},
            "semantic_enrichment": {
                "feedback_round": result.get("feedback_round"),
                "semantic_selector": result_summary.get("semantic_selector"),
                "semantic_job_counts": (
                    (result_summary.get("semantic_graph_state") or {}).get("semantic_job_counts")
                    if isinstance(result_summary.get("semantic_graph_state"), dict)
                    else {}
                ),
            },
            "jobs": jobs,
            "queued_ops": queued_ops,
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic-enrich")
def handle_graph_governance_snapshot_semantic_enrich(ctx: RequestContext):
    """Build/rebuild semantic companion artifacts for a graph snapshot."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    root = _graph_governance_project_root(project_id, body)
    from . import reconcile_semantic_enrichment as semantic
    from . import reconcile_feedback
    semantic_use_ai = _semantic_use_ai_from_body(body)
    semantic_mode = _automation_mode_from_body(
        body,
        "semantic_mode",
        "semantic_automation_mode",
        default=("auto" if semantic_use_ai else "manual"),
    )
    feedback_review_mode = _automation_mode_from_body(
        body,
        "feedback_review_mode",
        "review_automation_mode",
        default="manual",
    )
    if semantic_mode in {"manual", "enqueue_only"}:
        semantic_use_ai = False
    elif semantic_mode == "auto" and semantic_use_ai is None:
        semantic_use_ai = True
    enqueue_stale = bool(body.get("enqueue_stale", False))
    semantic_ai_call = _semantic_ai_call_from_body(project_id, root, {**body, "snapshot_id": snapshot_id})

    feedback_items = body.get("feedback_items")
    if feedback_items is not None and not isinstance(feedback_items, (list, dict)):
        from .errors import ValidationError
        raise ValidationError("feedback_items must be an object or list when provided")
    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.semantic-enrich")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        try:
            result = semantic.run_semantic_enrichment(
                conn,
                project_id,
                snapshot_id,
                root,
                feedback_items=feedback_items,
                feedback_round=body.get("feedback_round"),
                use_ai=semantic_use_ai,
                ai_call=semantic_ai_call,
                created_by=str(body.get("actor") or "observer"),
                max_excerpt_chars=(
                    int(body["max_excerpt_chars"])
                    if body.get("max_excerpt_chars") is not None
                    else None
                ),
                ai_feature_limit=_semantic_ai_feature_limit_from_body(body),
                **_semantic_ai_batch_kwargs_from_body(body),
                **_semantic_state_kwargs_from_body(body),
                **_semantic_ai_config_kwargs_from_body(body),
                **_semantic_selector_kwargs_from_body(body),
                semantic_config_path=body.get("semantic_config_path"),
                enqueue_stale=enqueue_stale,
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        if enqueue_stale:
            counts = _semantic_job_status_counts(conn, project_id, snapshot_id)
            queued_count = int(counts.get("ai_pending", 0) or 0) + int(counts.get("pending_ai", 0) or 0)
            _publish_semantic_job_enqueued(
                project_id,
                snapshot_id,
                queued_count,
                target_scope="node",
                source="semantic_enrich_api",
            )
        result.setdefault("automation", {})
        result["automation"].update({
            "semantic_mode": semantic_mode,
            "feedback_review_mode": feedback_review_mode,
        })
        if feedback_review_mode in {"enqueue_only", "auto"}:
            review_gate = semantic.feedback_review_gate(
                result.get("summary") or {},
                allow_heuristic_feedback_review=bool(body.get("allow_heuristic_feedback_review")),
                allow_partial_semantic_feedback_review=bool(
                    body.get("allow_partial_semantic_feedback_review")
                    or body.get("allow_partial_feedback_review")
                ),
            )
            if not review_gate.get("allowed"):
                result["feedback_queue"] = {
                    "mode": feedback_review_mode,
                    "blocked": True,
                    "gate": review_gate,
                }
            else:
                round_label = f"round-{int(result.get('feedback_round') or 0):03d}"
                classified = reconcile_feedback.classify_semantic_open_issues(
                    project_id,
                    snapshot_id,
                    source_round=round_label,
                    created_by=str(body.get("actor") or "observer"),
                    limit=(
                        int(body["feedback_classify_limit"])
                        if body.get("feedback_classify_limit") is not None
                        else None
                    ),
                    base_snapshot_id=str(body.get("semantic_base_snapshot_id") or body.get("base_snapshot_id") or ""),
                )
                result["feedback_queue"] = {
                    "mode": feedback_review_mode,
                    "source_round": round_label,
                    "classification": classified,
                    "gate": review_gate,
                }
        if bool(body.get("run_global_review_after_semantic") or body.get("full_global_review_after_semantic")):
            from . import reconcile_global_review

            global_review_use_ai = bool(
                _semantic_bool_from_body(
                    body,
                    "global_review_use_ai",
                    "use_global_review_ai",
                    default=False,
                )
            )
            global_review_ai_call = (
                _semantic_ai_call_from_body(project_id, root, {**body, "snapshot_id": snapshot_id})
                if global_review_use_ai
                else None
            )
            raw_budget = body.get("query_budget")
            query_budget = raw_budget if isinstance(raw_budget, dict) else None
            result["global_review"] = reconcile_global_review.run_full_global_review(
                conn,
                project_id,
                snapshot_id,
                root,
                global_review_use_ai=global_review_use_ai,
                global_review_ai_call=global_review_ai_call,
                classify_feedback=bool(body.get("classify_global_review_feedback", True)),
                base_snapshot_id=str(body.get("semantic_base_snapshot_id") or body.get("base_snapshot_id") or ""),
                actor=str(body.get("actor") or "observer"),
                run_id=str(body.get("global_review_run_id") or body.get("run_id") or ""),
                query_budget=query_budget,
            )
        conn.commit()
        return {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic/chunk-fix/replay")
def handle_graph_governance_snapshot_semantic_chunk_fix_replay(ctx: RequestContext):
    """Replay semantic chunk aggregate repair from persisted slice outputs."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    root = _graph_governance_project_root(project_id, body)
    from . import reconcile_semantic_enrichment as semantic
    from .errors import ValidationError

    source_trace_dir = (
        body.get("source_trace_dir")
        or body.get("source_trace")
        or body.get("chunk_trace_dir")
        or body.get("source_chunk_trace_dir")
    )
    if not source_trace_dir:
        raise ValidationError("source_trace_dir is required")
    node_ids = _string_list_field(
        body.get("node_ids")
        or body.get("semantic_node_ids")
        or body.get("target_ids")
        or body.get("node_id"),
        limit=50,
    )
    if not node_ids:
        raise ValidationError("node_id or node_ids is required")
    dry_run = bool(_semantic_bool_from_body(body, "dry_run", default=False))
    ai_body = {**body, "snapshot_id": snapshot_id}
    if (
        not dry_run
        and ai_body.get("use_ai") is None
        and ai_body.get("semantic_use_ai") is None
        and ai_body.get("reviewer_use_ai") is None
        and ai_body.get("use_reviewer_ai") is None
    ):
        ai_body["use_ai"] = True
    semantic_ai_call = None if dry_run else _semantic_ai_call_from_body(project_id, root, ai_body)
    output_trace_dir = (
        body.get("output_trace_dir")
        or body.get("replay_trace_dir")
        or body.get("trace_dir")
    )
    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.semantic.chunk-fix.replay")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        results = []
        for node_id in node_ids:
            node_trace_dir = output_trace_dir
            if output_trace_dir and len(node_ids) > 1:
                node_trace_dir = str(Path(output_trace_dir) / semantic._safe_node_filename(str(node_id)))
            try:
                results.append(semantic.replay_semantic_chunk_aggregate_fix(
                    conn,
                    project_id,
                    snapshot_id,
                    root,
                    node_id=str(node_id),
                    source_trace_dir=source_trace_dir,
                    trace_dir=node_trace_dir,
                    dry_run=dry_run,
                    ai_call=semantic_ai_call,
                    created_by=str(body.get("actor") or body.get("created_by") or "observer"),
                    semantic_config_path=body.get("semantic_config_path"),
                    semantic_ai_chunk_context_mode=body.get("semantic_ai_chunk_context_mode")
                    or body.get("chunk_context_mode"),
                    semantic_ai_chunk_max_slices=body.get("semantic_ai_chunk_max_slices")
                    or body.get("chunk_max_slices"),
                    semantic_ai_chunk_max_functions_per_slice=(
                        body.get("semantic_ai_chunk_max_functions_per_slice")
                        or body.get("chunk_max_functions_per_slice")
                    ),
                    semantic_ai_chunk_max_source_chars=body.get("semantic_ai_chunk_max_source_chars")
                    or body.get("chunk_max_source_chars"),
                    submit_for_review=bool(_semantic_bool_from_body(
                        body,
                        "submit_for_review",
                        default=True,
                    )),
                    persist_feature_payloads=bool(_semantic_bool_from_body(
                        body,
                        "persist_feature_payloads",
                        default=True,
                    )),
                    backfill_events=bool(_semantic_bool_from_body(
                        body,
                        "backfill_events",
                        default=True,
                    )),
                ))
            except (FileNotFoundError, KeyError, ValueError) as exc:
                _raise_graph_api_validation(exc)
        conn.commit()
        complete_count = sum(1 for item in results if item.get("status") == "complete")
        failed_count = sum(1 for item in results if item.get("ok") is False)
        return 200, {
            "ok": failed_count == 0,
            "project_id": project_id,
            "snapshot_id": snapshot_id,
            "status": "dry_run" if dry_run else ("complete" if failed_count == 0 else "partial"),
            "dry_run": dry_run,
            "node_count": len(results),
            "complete_count": complete_count,
            "failed_count": failed_count,
            "results": results,
        }
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/global-review/incremental")
def handle_graph_governance_snapshot_incremental_global_review(ctx: RequestContext):
    """Run post-scope semantic catch-up plus incremental global review."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    root = _graph_governance_project_root(project_id, body)
    from . import reconcile_global_review

    semantic_use_ai = _semantic_use_ai_from_body(body)
    semantic_mode = _automation_mode_from_body(
        body,
        "semantic_mode",
        "semantic_automation_mode",
        default=("auto" if semantic_use_ai else "manual"),
    )
    if semantic_mode in {"manual", "enqueue_only"}:
        semantic_use_ai = False
    elif semantic_mode == "auto" and semantic_use_ai is None:
        semantic_use_ai = True
    semantic_ai_call = _semantic_ai_call_from_body(project_id, root, {**body, "snapshot_id": snapshot_id})
    global_review_use_ai = bool(
        _semantic_bool_from_body(
            body,
            "global_review_use_ai",
            "use_global_review_ai",
            default=False,
        )
    )
    global_review_ai_call = (
        _semantic_ai_call_from_body(project_id, root, {**body, "snapshot_id": snapshot_id})
        if global_review_use_ai
        else None
    )
    semantic_batch_kwargs = _semantic_ai_batch_kwargs_from_body(body)
    raw_budget = body.get("query_budget")
    query_budget = raw_budget if isinstance(raw_budget, dict) else None

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.global-review.incremental")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        try:
            result = reconcile_global_review.run_incremental_global_review(
                conn,
                project_id,
                snapshot_id,
                root,
                base_snapshot_id=str(body.get("base_snapshot_id") or body.get("semantic_base_snapshot_id") or ""),
                changed_paths=body.get("changed_paths") or body.get("semantic_changed_paths"),
                changed_node_ids=body.get("changed_node_ids") or body.get("node_ids"),
                run_semantic=bool(
                    _semantic_bool_from_body(body, "run_semantic", "semantic_enrich", default=True)
                ),
                semantic_use_ai=semantic_use_ai,
                semantic_ai_call=semantic_ai_call,
                semantic_ai_feature_limit=_semantic_ai_feature_limit_from_body(body),
                semantic_ai_batch_size=semantic_batch_kwargs["semantic_ai_batch_size"],
                semantic_ai_batch_by=semantic_batch_kwargs["semantic_ai_batch_by"],
                semantic_ai_input_mode=semantic_batch_kwargs["semantic_ai_input_mode"],
                semantic_config_path=body.get("semantic_config_path"),
                classify_feedback=bool(
                    _semantic_bool_from_body(body, "classify_feedback", "semantic_classify_feedback", default=True)
                ),
                global_review_use_ai=global_review_use_ai,
                global_review_ai_call=global_review_ai_call,
                actor=str(body.get("actor") or "observer"),
                run_id=str(body.get("run_id") or ""),
                query_budget=query_budget,
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        conn.commit()
        return {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/global-review/full")
def handle_graph_governance_snapshot_full_global_review(ctx: RequestContext):
    """Build a full semantic health picture for a graph snapshot."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    root = _graph_governance_project_root(project_id, body)
    from . import reconcile_global_review

    global_review_use_ai = bool(
        _semantic_bool_from_body(
            body,
            "global_review_use_ai",
            "use_global_review_ai",
            default=False,
        )
    )
    global_review_ai_call = (
        _semantic_ai_call_from_body(project_id, root, {**body, "snapshot_id": snapshot_id})
        if global_review_use_ai
        else None
    )
    raw_budget = body.get("query_budget")
    query_budget = raw_budget if isinstance(raw_budget, dict) else None

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.global-review.full")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        try:
            result = reconcile_global_review.run_full_global_review(
                conn,
                project_id,
                snapshot_id,
                root,
                global_review_use_ai=global_review_use_ai,
                global_review_ai_call=global_review_ai_call,
                classify_feedback=bool(body.get("classify_feedback") or body.get("classify_global_review_feedback")),
                base_snapshot_id=str(body.get("base_snapshot_id") or body.get("semantic_base_snapshot_id") or ""),
                actor=str(body.get("actor") or "observer"),
                run_id=str(body.get("run_id") or ""),
                query_budget=query_budget,
            )
        except (KeyError, ValueError) as exc:
            _raise_graph_api_validation(exc)
        conn.commit()
        return {"ok": True, **result}
    finally:
        conn.close()


@route("POST", "/api/graph-governance/{project_id}/snapshots/{snapshot_id}/semantic/queue/claim")
def handle_graph_governance_snapshot_semantic_queue_claim(ctx: RequestContext):
    """Claim semantic AI jobs for an executor-backed runner."""
    project_id = ctx.get_project_id()
    snapshot_id = ctx.path_params["snapshot_id"]
    body = ctx.body
    from . import reconcile_semantic_enrichment as semantic
    from .errors import ValidationError

    worker_id = str(body.get("worker_id") or body.get("semantic_worker_id") or "").strip()
    if not worker_id:
        raise ValidationError("worker_id is required")
    limit = int(body.get("limit") or body.get("job_limit") or 10)
    if limit < 0:
        raise ValidationError("limit must be non-negative")
    raw_statuses = body.get("statuses") or body.get("status") or None
    if isinstance(raw_statuses, str):
        statuses = [raw_statuses]
    elif isinstance(raw_statuses, list):
        statuses = [str(item or "") for item in raw_statuses]
    else:
        statuses = None

    conn = get_connection(project_id)
    try:
        _require_graph_governance_operator(ctx, conn, "graph-governance.snapshot.semantic.queue.claim")
        snapshot_id = _resolve_graph_snapshot_id(conn, project_id, snapshot_id)
        try:
            result = semantic.claim_semantic_jobs(
                conn,
                project_id,
                snapshot_id,
                worker_id=worker_id,
                statuses=statuses,
                limit=limit,
                lease_seconds=int(body.get("lease_seconds") or body.get("claim_lease_seconds") or 1800),
                actor=str(body.get("actor") or worker_id),
            )
        except ValueError as exc:
            _raise_graph_api_validation(exc)
        try:
            audit_service.record(
                conn,
                project_id,
                "reconcile_semantic_jobs_claimed",
                actor=str(body.get("actor") or worker_id),
                details=json.dumps({
                    "snapshot_id": snapshot_id,
                    "worker_id": worker_id,
                    "claim_id": result.get("claim_id", ""),
                    "claimed_count": result.get("claimed_count", 0),
                }, ensure_ascii=False, sort_keys=True),
            )
            conn.commit()
        except Exception:
            pass
        return result
    finally:
        conn.close()


@route("GET", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}")
def handle_reconcile_deferred_cluster_get(ctx: RequestContext):
    """Get a single deferred-cluster row by fingerprint."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        q.ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM reconcile_deferred_clusters "
            "WHERE project_id = ? AND cluster_fingerprint = ?",
            (project_id, fp),
        ).fetchone()
    if row is None:
        return 404, {"error": "deferred_cluster_not_found",
                     "cluster_fingerprint": fp}
    return {"cluster": _deferred_cluster_row_to_dict(row)}


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}/skip")
def handle_reconcile_deferred_cluster_skip(ctx: RequestContext):
    """Mark a deferred-cluster as skipped with a reason."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    body = ctx.body or {}
    reason = str(body.get("reason") or "observer_skipped")
    changed = q.mark_terminal(project_id, fp, "skipped", reason)
    return {"ok": bool(changed), "cluster_fingerprint": fp, "status": "skipped",
            "reason": reason}


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}/file-now")
def handle_reconcile_deferred_cluster_file_now(ctx: RequestContext):
    """Force-file a queued cluster as a backlog/PM task immediately."""
    from . import reconcile_deferred_queue as q
    from . import auto_backlog_bridge

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        q.ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM reconcile_deferred_clusters "
            "WHERE project_id = ? AND cluster_fingerprint = ?",
            (project_id, fp),
        ).fetchone()
    if row is None:
        return 404, {"error": "deferred_cluster_not_found",
                     "cluster_fingerprint": fp}
    rec = _deferred_cluster_row_to_dict(row)
    payload = rec.get("payload") or {}
    run_id = rec.get("run_id") or ""
    if rec.get("status") not in ("queued", "failed_retryable"):
        return 409, {
            "error": "deferred_cluster_not_fileable",
            "cluster_fingerprint": fp,
            "status": rec.get("status"),
        }
    q.mark_filing(project_id, fp)
    try:
        out = auto_backlog_bridge.file_cluster_as_backlog(
            cluster_group=payload,
            cluster_report=payload.get("cluster_report") or {},
            run_id=run_id,
            project_id=project_id,
        )
    except Exception as exc:  # noqa: BLE001
        q.requeue_after_failure(project_id, fp, reason=f"file_cluster_exception: {exc}")
        return 500, {"error": "file_cluster_failed", "message": str(exc)}
    if out.get("filed") and out.get("task_id"):
        q.mark_in_chain(project_id, fp, out["task_id"], bug_id=out.get("backlog_id"))
    else:
        q.requeue_after_failure(
            project_id,
            fp,
            reason=str(out.get("reason") or "file_cluster_failed"),
        )
    return {"result": out, "cluster_fingerprint": fp}


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}/withdraw")
def handle_reconcile_deferred_cluster_withdraw(ctx: RequestContext):
    """Withdraw a filed cluster: cancels filed root_task_id and marks skipped."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    body = ctx.body or {}
    reason = str(body.get("reason") or "observer_withdraw")
    cancelled_task: str = ""
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        q.ensure_schema(conn)
        row = conn.execute(
            "SELECT root_task_id FROM reconcile_deferred_clusters "
            "WHERE project_id = ? AND cluster_fingerprint = ?",
            (project_id, fp),
        ).fetchone()
        if row is not None:
            cancelled_task = row[0] or ""
            if cancelled_task:
                try:
                    from . import task_registry
                    task_registry.cancel_task(
                        conn,
                        cancelled_task,
                        reason,
                        project_id=project_id,
                    )
                except Exception:
                    pass
    q.mark_terminal(project_id, fp, "skipped", reason)
    return {"ok": True, "cluster_fingerprint": fp,
            "cancelled_root_task_id": cancelled_task, "reason": reason}


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}/retry")
def handle_reconcile_deferred_cluster_retry(ctx: RequestContext):
    """Retry a failed_retryable cluster.  When body.force=True resets retry_count to 0."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    body = ctx.body or {}
    force = bool(body.get("force", False))
    with DBContext(project_id) as conn:
        if conn.row_factory is None:
            conn.row_factory = sqlite3.Row
        q.ensure_schema(conn)
        if force:
            changed = q.force_retry(
                project_id,
                fp,
                reason=str(body.get("reason") or "force_retry"),
                conn=conn,
            )
        else:
            cur = conn.execute(
                "UPDATE reconcile_deferred_clusters SET status = 'queued', "
                "  next_retry_at = NULL "
                "WHERE project_id = ? AND cluster_fingerprint = ? "
                "  AND status IN ('failed_retryable','expired')",
                (project_id, fp),
            )
            conn.commit()
            changed = (cur.rowcount or 0) > 0
    return {"ok": changed, "cluster_fingerprint": fp,
            "force": force, "status": "queued" if changed else "unchanged"}


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}/observer-hold")
def handle_reconcile_deferred_cluster_observer_hold(ctx: RequestContext):
    """Pause a cluster queue row before auto-flow picks it up again."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    body = ctx.body or {}
    reason = str(body.get("reason") or "observer_hold")
    actor = str(body.get("actor") or "observer")
    changed = q.mark_observer_hold(project_id, fp, reason=reason, actor=actor)
    return {
        "ok": bool(changed),
        "cluster_fingerprint": fp,
        "status": "observer_hold" if changed else "unchanged",
        "reason": reason,
    }


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}/observer-takeover")
def handle_reconcile_deferred_cluster_observer_takeover(ctx: RequestContext):
    """Transfer a chain-owned cluster row to observer/MF ownership."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    body = ctx.body or {}
    reason = str(body.get("reason") or "observer_takeover")
    actor = str(body.get("actor") or "observer")
    changed = q.mark_observer_takeover(project_id, fp, reason=reason, actor=actor)
    return {
        "ok": bool(changed),
        "cluster_fingerprint": fp,
        "status": "observer_takeover" if changed else "unchanged",
        "reason": reason,
    }


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}/observer-release")
def handle_reconcile_deferred_cluster_observer_release(ctx: RequestContext):
    """Release observer ownership back to queue or a terminal audit state."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    body = ctx.body or {}
    next_status = str(body.get("next_status") or "queued")
    reason = str(body.get("reason") or "observer_release")
    actor = str(body.get("actor") or "observer")
    try:
        changed = q.release_observer_takeover(
            project_id,
            fp,
            next_status=next_status,
            reason=reason,
            actor=actor,
        )
    except ValueError as exc:
        return 422, {"error": "invalid_next_status", "message": str(exc)}
    return {
        "ok": bool(changed),
        "cluster_fingerprint": fp,
        "status": next_status if changed else "unchanged",
        "reason": reason,
    }


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}/patch-accepted")
def handle_reconcile_deferred_cluster_patch_accepted(ctx: RequestContext):
    """Close an observer/MF repaired cluster as accepted."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    body = ctx.body or {}
    patch_id = str(body.get("patch_id") or "")
    reason = str(body.get("reason") or "observer_patch_accepted")
    changed = q.mark_patch_accepted(project_id, fp, patch_id=patch_id, reason=reason)
    return {
        "ok": bool(changed),
        "cluster_fingerprint": fp,
        "status": "patch_accepted" if changed else "unchanged",
        "patch_id": patch_id,
        "reason": reason,
    }


@route("POST", "/api/reconcile/{project_id}/deferred-clusters/{cluster_fingerprint}/supersede-bad-run")
def handle_reconcile_deferred_cluster_supersede_bad_run(ctx: RequestContext):
    """Quarantine a bad cluster run so finalize does not consume it."""
    from . import reconcile_deferred_queue as q

    project_id = ctx.get_project_id()
    fp = ctx.path_params.get("cluster_fingerprint", "")
    body = ctx.body or {}
    reason = str(body.get("reason") or "superseded_bad_run")
    changed = q.mark_superseded_bad_run(project_id, fp, reason=reason)
    return {
        "ok": bool(changed),
        "cluster_fingerprint": fp,
        "status": "superseded_bad_run" if changed else "unchanged",
        "reason": reason,
    }


@route("POST", "/api/wf/{project_id}/node-create")
def handle_node_create(ctx: RequestContext):
    """Create a single node. System allocates display_id.

    AI provides: parent_layer (int) + title + deps + primary
    System provides: display_id (L{layer}.{next_index})

    Body: {
        "parent_layer": 22,          // required: which layer
        "title": "ContextStore",     // required
        "node": {                    // optional extras
            "deps": ["L15.1"],
            "primary": ["agent/context_store.py"],
            "description": "..."
        }
    }
    """
    project_id = ctx.get_project_id()
    parent_layer = ctx.body.get("parent_layer")
    title = ctx.body.get("title", "")
    node = ctx.body.get("node", {})

    if not parent_layer and not title:
        # Fallback: try to read from node.id (legacy)
        node_id = node.get("id", "")
        if node_id:
            parent_layer = int(node_id.split(".")[0][1:]) if "." in node_id else None
            title = node.get("title", node_id)

    if parent_layer is None:
        from .errors import ValidationError
        raise ValidationError("parent_layer is required (e.g., 22 for L22.x)")

    if not title:
        from .errors import ValidationError
        raise ValidationError("title is required")

    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        if session.get("role") not in ("coordinator", "pm"):
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(session.get("role", ""), "node-create",
                                        {"detail": "Only coordinator or PM can create nodes"})

        # System allocates display_id: find max index in this layer
        prefix = f"L{parent_layer}."
        existing = conn.execute(
            "SELECT node_id FROM node_state WHERE project_id = ? AND node_id LIKE ?",
            (project_id, f"{prefix}%")
        ).fetchall()

        max_index = 0
        for row in existing:
            try:
                idx = int(row["node_id"].split(".")[1])
                max_index = max(max_index, idx)
            except (ValueError, IndexError):
                pass

        new_index = max_index + 1
        display_id = f"L{parent_layer}.{new_index}"

        # Insert node state
        now = __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
        conn.execute(
            """INSERT OR IGNORE INTO node_state
               (project_id, node_id, verify_status, build_status, updated_at, version)
               VALUES (?, ?, 'pending', 'unknown', ?, 1)""",
            (project_id, display_id, now)
        )

        # Record in history (use role field which exists in all schema versions)
        try:
            conn.execute(
                """INSERT INTO node_history (project_id, node_id, from_status, to_status, role, evidence_json, created_at)
                   VALUES (?, ?, 'none', 'pending', ?, ?, ?)""",
                (project_id, display_id, session.get("role", "coordinator"),
                 json.dumps({"title": title, "deps": node.get("deps", []), "primary": node.get("primary", [])}),
                 now)
            )
        except Exception:
            pass  # History is nice-to-have, don't block node creation

        # P0-2 fix: also add node to in-memory graph + persist graph.json
        try:
            from .models import NodeDef
            from .db import _resolve_project_dir
            graph = project_service.load_project_graph(project_id)
            node_def = NodeDef(
                id=display_id,
                title=title,
                layer=f"L{parent_layer}",
                primary=node.get("primary", []),
            )
            deps = node.get("deps", [])
            # Filter deps to only existing graph nodes
            valid_deps = [d for d in deps if graph.has_node(d)]
            graph.add_node(node_def, deps=valid_deps)
            graph.save(_resolve_project_dir(project_id) / "graph.json")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("node-create graph update failed: %s", e)

    return {
        "node_id": display_id,
        "parent_layer": parent_layer,
        "title": title,
        "created": True,
    }


@route("POST", "/api/wf/{project_id}/verify-update")
def handle_verify_update(ctx: RequestContext):
    project_id = ctx.get_project_id()

    # Input validation with helpful messages
    nodes = ctx.body.get("nodes", [])
    status = ctx.body.get("status", "")
    evidence = ctx.body.get("evidence")

    if not nodes:
        from .errors import ValidationError
        raise ValidationError(
            'Missing "nodes" field. Example: {"nodes": ["L1.3"], "status": "testing", '
            '"evidence": {"type": "test_report", "producer": "tester-001"}}'
        )
    if not isinstance(nodes, list):
        from .errors import ValidationError
        raise ValidationError(f'"nodes" must be a list, got {type(nodes).__name__}')
    if not status:
        from .errors import ValidationError
        raise ValidationError(
            'Missing "status" field. Valid values: pending, testing, t2_pass, qa_pass, failed, waived, skipped'
        )
    if evidence is not None and not isinstance(evidence, dict):
        from .errors import ValidationError
        raise ValidationError(
            f'"evidence" must be a dict, got {type(evidence).__name__}. '
            'Example: {"type": "test_report", "producer": "tester-001", "tool": "pytest", '
            '"summary": {"passed": 42, "failed": 0}}'
        )

    with DBContext(project_id) as conn:
        # Idempotency check
        rc = get_redis()
        if ctx.idem_key:
            cached = rc.check_idempotency(ctx.idem_key)
            if cached:
                return cached

        session = ctx.require_auth(conn)
        graph = project_service.load_project_graph(project_id)

        result = state_service.verify_update(
            conn, project_id, graph,
            node_ids=nodes,
            target_status=status,
            session=session,
            evidence_dict=evidence,
        )

        # Store idempotency
        if ctx.idem_key:
            rc.store_idempotency(ctx.idem_key, result)

    return result


@route("POST", "/api/wf/{project_id}/baseline")
def handle_baseline(ctx: RequestContext):
    """Coordinator batch-sets historical node states, bypassing checks."""
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        result = state_service.set_baseline(
            conn, project_id,
            node_statuses=ctx.body.get("nodes", {}),
            session=session,
            reason=ctx.body.get("reason", ""),
        )
    return result


@route("POST", "/api/wf/{project_id}/release-gate")
def handle_release_gate(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        graph = project_service.load_project_graph(project_id)
        result = state_service.release_gate(
            conn, project_id, graph,
            scope=ctx.body.get("scope"),
            profile=ctx.body.get("profile"),
            min_status=ctx.body.get("min_status", "qa_pass"),
        )
    return result


@route("POST", "/api/wf/{project_id}/artifacts-check")
def handle_artifacts_check(ctx: RequestContext):
    """Check artifacts for nodes before qa_pass."""
    project_id = ctx.get_project_id()
    node_ids = ctx.body.get("nodes", [])
    if not node_ids:
        from .errors import ValidationError
        raise ValidationError('Missing "nodes" field.')

    graph = project_service.load_project_graph(project_id)
    from .artifacts import check_artifacts_for_qa_pass
    return check_artifacts_for_qa_pass(node_ids, graph, project_id)


@route("POST", "/api/wf/{project_id}/coverage-check")
def handle_coverage_check(ctx: RequestContext):
    """Check if changed files are covered by acceptance graph nodes. Records result for gatekeeper."""
    project_id = ctx.get_project_id()
    changed_files = ctx.body.get("files", [])
    if not changed_files:
        from .errors import ValidationError
        raise ValidationError('Missing "files" field. Provide list of changed file paths.')

    graph = project_service.load_project_graph(project_id)
    from .coverage_check import check_feature_coverage
    result = check_feature_coverage(graph, changed_files)

    # Record result for gatekeeper
    try:
        from . import gatekeeper
        with DBContext(project_id) as conn:
            session = None
            try:
                session = ctx.require_auth(conn)
            except Exception:
                pass
            gatekeeper.record_check(
                conn, project_id, "coverage_check",
                passed=result.get("pass", False),
                result=result,
                created_by=session.get("principal_id", "") if session else "",
            )
    except Exception:
        pass  # Non-critical

    return result


@route("GET", "/api/wf/{project_id}/summary")
def handle_summary(ctx: RequestContext):
    project_id = ctx.get_project_id()
    try:
        graph = project_service.load_project_graph(project_id)
    except ValidationError as e:
        return _workflow_graph_missing(project_id, str(e))
    with DBContext(project_id) as conn:
        summary = state_service.get_summary(conn, project_id)
    summary["ok"] = True
    summary["workflow_graph_nodes"] = graph.node_count()
    return summary


def _workflow_graph_missing(project_id: str, detail: str = "") -> dict:
    return {
        "ok": False,
        "project_id": project_id,
        "error": "workflow_graph_missing",
        "needs_import_graph": True,
        "total_nodes": 0,
        "by_status": {},
        "message": (
            f"No workflow acceptance graph found for project {project_id!r}. "
            f"Run POST /api/wf/{project_id}/import-graph before using wf_summary, "
            "wf_impact, or node_update."
        ),
        "detail": detail,
        "next_action": f"POST /api/wf/{project_id}/import-graph",
    }


@route("GET", "/api/wf/{project_id}/preflight-check")
def handle_preflight_check(ctx: RequestContext):
    project_id = ctx.get_project_id()
    auto_fix = ctx.query.get("auto_fix", "false").lower() == "true"
    from .preflight import run_preflight
    with DBContext(project_id) as conn:
        return run_preflight(conn, project_id, auto_fix=auto_fix)


@route("GET", "/api/wf/{project_id}/node/{node_id}")
def handle_get_node(ctx: RequestContext):
    project_id = ctx.get_project_id()
    node_id = ctx.path_params.get("node_id", "")
    with DBContext(project_id) as conn:
        state = state_service.get_node_status(conn, project_id, node_id)
        if state is None:
            from .errors import NodeNotFoundError
            raise NodeNotFoundError(node_id)
    graph = project_service.load_project_graph(project_id)
    node_def = graph.get_node(node_id)
    return {**state, "definition": node_def}


@route("POST", "/api/wf/{project_id}/node-update")
def handle_node_update(ctx: RequestContext):
    """Update node attributes (e.g. secondary doc bindings). Coordinator only."""
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        if session.get("role") != "coordinator":
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(session.get("role", ""), "node-update",
                                        {"detail": "Only coordinator can update node attributes"})
    node_id = ctx.body.get("node_id")
    attrs = ctx.body.get("attrs", {})
    if not node_id or not attrs:
        from .errors import GovernanceError
        raise GovernanceError("missing node_id or attrs", "invalid_request")
    # Only allow safe attributes to be updated
    ALLOWED_ATTRS = {"secondary", "test", "description", "propagation"}
    rejected = set(attrs.keys()) - ALLOWED_ATTRS
    if rejected:
        from .errors import GovernanceError
        raise GovernanceError(f"Cannot update attrs: {rejected}. Allowed: {ALLOWED_ATTRS}", "forbidden_attr")
    graph = project_service.load_project_graph(project_id)
    graph.update_node_attrs(node_id, attrs)
    from .db import _resolve_project_dir
    graph.save(_resolve_project_dir(project_id) / "graph.json")
    return {"node_id": node_id, "updated_attrs": list(attrs.keys())}


@route("POST", "/api/wf/{project_id}/node-batch-update")
def handle_node_batch_update(ctx: RequestContext):
    """Batch update secondary doc bindings for multiple nodes. Coordinator only."""
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        if session.get("role") != "coordinator":
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(session.get("role", ""), "node-batch-update",
                                        {"detail": "Only coordinator can batch update node attributes"})
    updates = ctx.body.get("updates", [])
    if not updates:
        from .errors import GovernanceError
        raise GovernanceError("missing updates array", "invalid_request")
    graph = project_service.load_project_graph(project_id)
    results = []
    for upd in updates:
        node_id = upd.get("node_id")
        attrs = upd.get("attrs", {})
        try:
            ALLOWED_ATTRS = {"secondary", "test", "description", "propagation"}
            safe_attrs = {k: v for k, v in attrs.items() if k in ALLOWED_ATTRS}
            graph.update_node_attrs(node_id, safe_attrs)
            results.append({"node_id": node_id, "status": "updated"})
        except Exception as e:
            results.append({"node_id": node_id, "status": "error", "error": str(e)})
    from .db import _resolve_project_dir
    graph.save(_resolve_project_dir(project_id) / "graph.json")
    return {"updated": len([r for r in results if r["status"] == "updated"]), "results": results}


@route("POST", "/api/wf/{project_id}/node-delete")
def handle_node_delete(ctx: RequestContext):
    """Delete nodes from graph and node_state. Coordinator only.

    Body: {"nodes": ["L1.1", "L1.2", ...], "reason": "..."}
    """
    project_id = ctx.get_project_id()
    nodes = ctx.body.get("nodes", [])
    reason = ctx.body.get("reason", "")
    if not nodes:
        from .errors import GovernanceError
        raise GovernanceError("missing nodes array", "invalid_request")

    graph = project_service.load_project_graph(project_id)
    deleted = []
    skipped = []
    for nid in nodes:
        try:
            graph.remove_node(nid)
            deleted.append(nid)
        except Exception:
            skipped.append({"node_id": nid, "reason": "not in graph"})

    # Save graph
    from .db import _resolve_project_dir
    graph.save(_resolve_project_dir(project_id) / "graph.json")

    # Remove from node_state DB + audit
    with DBContext(project_id) as conn:
        for nid in deleted:
            conn.execute("DELETE FROM node_state WHERE project_id = ? AND node_id = ?",
                         (project_id, nid))
        audit_service.record(conn, project_id, "node.batch_delete",
                             node_ids=deleted, reason=reason)

    return {"deleted": len(deleted), "skipped": skipped, "reason": reason}


@route("POST", "/api/wf/{project_id}/node-soft-delete")
def handle_node_soft_delete(ctx: RequestContext):
    """Soft-delete nodes by setting verify_status to 'rolled_back'.

    PR-C scaffold: no production callsite yet. Sets status and writes audit record.

    Body: {"node_ids": ["L1.1", "L1.2"], "reason": "rolled back by graph delta"}
    """
    project_id = ctx.get_project_id()
    node_ids = ctx.body.get("node_ids", [])
    reason = ctx.body.get("reason", "")
    if not node_ids:
        from .errors import GovernanceError
        raise GovernanceError("missing node_ids array", "invalid_request")

    now = __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
    updated = []
    skipped = []

    with DBContext(project_id) as conn:
        for nid in node_ids:
            row = conn.execute(
                "SELECT verify_status, version FROM node_state WHERE project_id = ? AND node_id = ?",
                (project_id, nid),
            ).fetchone()
            if not row:
                skipped.append({"node_id": nid, "reason": "not found"})
                continue

            old_status = row["verify_status"]
            new_version = row["version"] + 1
            conn.execute(
                """UPDATE node_state SET verify_status = 'rolled_back',
                   updated_by = 'node-soft-delete', updated_at = ?, version = ?
                   WHERE project_id = ? AND node_id = ?""",
                (now, new_version, project_id, nid),
            )

            # Write audit record to node_history
            try:
                conn.execute(
                    """INSERT INTO node_history
                       (project_id, node_id, from_status, to_status, role, evidence_json, session_id, ts, version)
                       VALUES (?, ?, ?, 'rolled_back', 'coordinator', ?, 'node-soft-delete', ?, ?)""",
                    (project_id, nid, old_status,
                     json.dumps({"reason": reason, "type": "soft_delete"}),
                     now, new_version),
                )
            except Exception:
                pass  # History is best-effort

            updated.append(nid)

        # Audit
        audit_service.record(conn, project_id, "node.soft_delete",
                             node_ids=updated, reason=reason)

    return {"updated": updated, "skipped": skipped, "reason": reason}


@route("POST", "/api/wf/{project_id}/node-promote-backfill")
def handle_node_promote_backfill(ctx: RequestContext):
    """Promote a backfilled node from pending → qa_pass.

    Body: {
        "node_id": "L7.6",
        "merge_commit": "abc1234",
        "operator_id": "observer-1",
        "reason": "BF-005 historical backfill"
    }

    Role check: only observer or coordinator allowed.
    Returns 403 if node lacks backfill_ref, 400 if merge_commit invalid, 200 on success.
    """
    project_id = ctx.get_project_id()
    node_id = ctx.body.get("node_id", "")
    merge_commit = ctx.body.get("merge_commit", "")
    operator_id = ctx.body.get("operator_id", "")
    reason = ctx.body.get("reason", "")

    if not node_id or not merge_commit:
        return 400, {"error": "node_id and merge_commit are required"}

    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        role = session.get("role", "")
        if role not in ("observer", "coordinator"):
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(
                role, "node-promote-backfill",
                {"detail": "Only observer or coordinator can promote backfill nodes"},
            )

        try:
            result = state_service.promote_backfill_node(
                conn=conn,
                project_id=project_id,
                node_id=node_id,
                merge_commit=merge_commit,
                operator_id=operator_id or session.get("principal_id", "anonymous"),
                reason=reason,
            )
            conn.commit()
            return 200, result
        except GovernanceError:
            raise


@route("GET", "/api/wf/{project_id}/impact")
def handle_impact(ctx: RequestContext):
    project_id = ctx.get_project_id()
    files_str = ctx.query.get("files", "")
    files = [f.strip() for f in files_str.split(",") if f.strip()] if files_str else []
    # file_policy query param: "primary_only" disables secondary matching
    # Default: match both primary and secondary (doc/test reverse traceability)
    primary_only = ctx.query.get("file_policy", "") == "primary_only"

    try:
        graph = project_service.load_project_graph(project_id)
    except ValidationError as e:
        result = _workflow_graph_missing(project_id, str(e))
        result["files"] = files
        return result

    with DBContext(project_id) as conn:
        def get_status(nid):
            row = conn.execute(
                "SELECT verify_status FROM node_state WHERE project_id = ? AND node_id = ?",
                (project_id, nid),
            ).fetchone()
            return VerifyStatus.from_str(row["verify_status"]) if row else VerifyStatus.PENDING

        analyzer = ImpactAnalyzer(graph, get_status)
        request = ImpactAnalysisRequest(
            changed_files=files,
            file_policy=FileHitPolicy(match_primary=True, match_secondary=not primary_only),
        )
        return analyzer.analyze(request)


@route("GET", "/api/wf/{project_id}/export")
def handle_export(ctx: RequestContext):
    project_id = ctx.get_project_id()
    fmt = ctx.query.get("format", "json")
    graph = project_service.load_project_graph(project_id)

    if fmt == "mermaid":
        with DBContext(project_id) as conn:
            rows = conn.execute(
                "SELECT node_id, verify_status FROM node_state WHERE project_id = ?",
                (project_id,),
            ).fetchall()
            statuses = {r["node_id"]: r["verify_status"] for r in rows}
        return {"mermaid": graph.export_mermaid(statuses), "node_count": graph.node_count()}
    elif fmt == "json":
        return {"nodes": {nid: graph.get_node(nid) for nid in graph.list_nodes()}}
    else:
        from .errors import ValidationError
        raise ValidationError(f"Unknown export format: {fmt}")


@route("POST", "/api/wf/{project_id}/rollback")
def handle_rollback(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn)
        result = state_service.rollback(
            conn, project_id,
            target_version=ctx.body.get("target_version", 0),
            session=session,
        )
    return result


# --- Memory ---

@route("POST", "/api/mem/{project_id}/write")
def handle_mem_write(ctx: RequestContext):
    project_id = ctx.get_project_id()
    entry = MemoryEntry.from_dict(ctx.body)
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn) if ctx.token else {}
        result = memory_service.write_memory(conn, project_id, entry, session)
    return 201, result


@route("POST", "/api/mem/{project_id}/ttl-cleanup")
def handle_mem_ttl_cleanup(ctx: RequestContext):
    """Archive active memories whose durability TTL has elapsed (per domain pack)."""
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        result = memory_service.archive_expired_memories(conn, project_id)
    return result


@route("POST", "/api/mem/{project_id}/flush-index")
def handle_mem_flush_index(ctx: RequestContext):
    """Flush pending dbservice reindex queue (DockerBackend only)."""
    from .memory_backend import get_backend
    backend = get_backend()
    if hasattr(backend, "flush_pending_index"):
        flushed = backend.flush_pending_index()
        remaining = backend.pending_index_count()
    else:
        flushed, remaining = 0, 0
    return {"flushed": flushed, "remaining": remaining}


@route("GET", "/api/mem/{project_id}/query")
def handle_mem_query(ctx: RequestContext):
    project_id = ctx.get_project_id()
    module = ctx.query.get("module")
    kind = ctx.query.get("kind")
    node = ctx.query.get("node")

    if node:
        entries = memory_service.query_by_related_node(project_id, node)
    elif kind:
        entries = memory_service.query_by_kind(project_id, kind, module)
    elif module:
        entries = memory_service.query_by_module(project_id, module)
    else:
        entries = memory_service.query_all(project_id)
    return {"entries": entries, "count": len(entries)}


@route("GET", "/api/mem/{project_id}/search")
def handle_mem_search(ctx: RequestContext):
    """Full-text search across memories (FTS5 or semantic depending on backend)."""
    project_id = ctx.get_project_id()
    q = ctx.query.get("q", "")
    top_k = int(ctx.query.get("top_k", "5"))
    if not q:
        return {"error": "MISSING_QUERY", "message": "q parameter required"}, 400
    with DBContext(project_id) as conn:
        results = memory_service.search_memories(conn, project_id, q, top_k)
    return {"results": results, "count": len(results), "query": q}


@route("POST", "/api/mem/{project_id}/relate")
def handle_mem_relate(ctx: RequestContext):
    """Create a relation between two ref_ids."""
    project_id = ctx.get_project_id()
    body = ctx.body or {}
    from_ref = body.get("from_ref_id", "")
    relation = body.get("relation", "")
    to_ref = body.get("to_ref_id", "")
    if not from_ref or not relation or not to_ref:
        return {"error": "MISSING_FIELDS", "message": "from_ref_id, relation, to_ref_id required"}, 400
    from .memory_backend import get_backend
    with DBContext(project_id) as conn:
        result = get_backend().relate(conn, project_id, from_ref, relation, to_ref, body.get("metadata"))
    return 201, result


@route("GET", "/api/mem/{project_id}/expand")
def handle_mem_expand(ctx: RequestContext):
    """Traverse relation graph from a ref_id."""
    project_id = ctx.get_project_id()
    ref_id = ctx.query.get("ref_id", "")
    depth = int(ctx.query.get("depth", "2"))
    if not ref_id:
        return {"error": "MISSING_REF_ID", "message": "ref_id parameter required"}, 400
    from .memory_backend import get_backend
    with DBContext(project_id) as conn:
        results = get_backend().expand(conn, project_id, ref_id, depth)
    return {"results": results, "count": len(results), "ref_id": ref_id, "depth": depth}


@route("POST", "/api/mem/{project_id}/promote")
def handle_mem_promote(ctx: RequestContext):
    """Promote a memory to global scope (creates a cross-project copy)."""
    project_id = ctx.get_project_id()
    body = ctx.body or {}
    memory_id = body.get("memory_id", "")
    target_scope = body.get("target_scope", "global")
    reason = body.get("reason", "")
    if not memory_id:
        return {"error": "MISSING_FIELD", "message": "memory_id required"}, 400
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn) if ctx.token else {}
        result = memory_service.promote_memory(
            conn, project_id, memory_id,
            target_scope=target_scope, reason=reason,
            actor_id=session.get("principal_id", ""),
        )
    return result


@route("POST", "/api/mem/{project_id}/register-pack")
def handle_mem_register_pack(ctx: RequestContext):
    """Register a domain pack (kind definitions) for a project."""
    project_id = ctx.get_project_id()
    body = ctx.body or {}
    domain = body.get("domain", "development")
    types = body.get("types", {})
    if not types:
        return {"error": "MISSING_FIELD", "message": "types dict required"}, 400
    with DBContext(project_id) as conn:
        session = ctx.require_auth(conn) if ctx.token else {}
        result = memory_service.register_domain_pack(
            conn, project_id, domain, types,
            actor_id=session.get("principal_id", ""),
        )
    return result


# --- Audit ---

@route("GET", "/api/audit/{project_id}/log")
def handle_audit_log(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        entries = audit_service.read_log(
            conn, project_id,
            limit=int(ctx.query.get("limit", "100")),
            event_filter=ctx.query.get("event"),
            since=ctx.query.get("since"),
        )
    return {"entries": entries, "count": len(entries)}


@route("GET", "/api/audit/{project_id}/violations")
def handle_audit_violations(ctx: RequestContext):
    project_id = ctx.get_project_id()
    with DBContext(project_id) as conn:
        entries = audit_service.read_violations(
            conn, project_id,
            limit=int(ctx.query.get("limit", "100")),
            since=ctx.query.get("since"),
        )
    return {"entries": entries, "count": len(entries)}


# --- Task Registry ---


def _publish_event(event_name, payload):
    """Best-effort event publish to event bus (mirrors auto_chain pattern)."""
    try:
        from . import event_bus
        event_bus._bus.publish(event_name, payload)
    except Exception:
        pass


@route("POST", "/api/task/{project_id}/create")
def handle_task_create(ctx: RequestContext):
    """Create a task. Auth optional — uses principal_id if token provided, else 'anonymous'.

    Phase 4: Auto-enriches metadata with operation_type, intent_hash, and
    runs conflict rules for non-system task types.
    """
    project_id = ctx.get_project_id()
    log.info("API task.create: project=%s type=%s prompt=%r",
             project_id, ctx.body.get("type", "task"), (ctx.body.get("prompt", ""))[:80])
    from . import task_registry
    from .conflict_rules import extract_operation_type, compute_intent_hash, check_conflicts
    created_by = "anonymous"
    if ctx.token:
        try:
            with DBContext(project_id) as conn:
                session = ctx.require_auth(conn)
                created_by = session.get("principal_id", "anonymous")
        except Exception:
            pass

    prompt = ctx.body.get("prompt", "")
    task_type = ctx.body.get("type", "task")
    metadata = ctx.body.get("metadata") or {}
    if isinstance(metadata, str):
        import json as _json
        try:
            metadata = _json.loads(metadata)
        except Exception:
            metadata = {}

    # Auto-enrich metadata
    if "operation_type" not in metadata:
        metadata["operation_type"] = extract_operation_type(prompt)
    if "intent_hash" not in metadata:
        metadata["intent_hash"] = compute_intent_hash(prompt)
    if "intent_summary" not in metadata:
        metadata["intent_summary"] = prompt[:200]

    # §2.1-§2.3: Reconcile task creator allowlist + audit + rate limit (R2/R3/R4)
    _is_reconcile = task_type == "reconcile" or task_type.startswith("reconcile_")
    if _is_reconcile:
        # §2.1: Soft enforcement — warn for non-allowed creators (R2)
        _allowed_prefixes = ("observer-", "coordinator", "auto-chain-reconcile")
        if not any(created_by.startswith(p) for p in _allowed_prefixes):
            log.warning("reconcile_task: creator %r not in allowlist (soft enforce §2.1)", created_by)
        # §2.3: 3-tier rate limiting (R4)
        with DBContext(project_id) as _rl_conn:
            # Tier-1: max 1 active reconcile_run
            _active_runs = _rl_conn.execute(
                "SELECT COUNT(DISTINCT json_extract(metadata_json, '$.reconcile_run_id')) "
                "FROM tasks WHERE project_id=? AND type LIKE 'reconcile%' AND status IN ('pending','claimed','running')",
                (project_id,),
            ).fetchone()[0]
            if _active_runs > 1:
                raise GovernanceError("rate_limit", "Tier-1: max 1 active reconcile_run exceeded", status=429)
            # Tier-2: max 3 concurrent tasks per run
            _run_id = metadata.get("reconcile_run_id", "")
            if _run_id:
                _concurrent = _rl_conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE project_id=? AND type LIKE 'reconcile%' "
                    "AND status IN ('pending','claimed','running') AND json_extract(metadata_json, '$.reconcile_run_id')=?",
                    (project_id, _run_id),
                ).fetchone()[0]
                if _concurrent >= 3:
                    raise GovernanceError("rate_limit", "Tier-2: max 3 concurrent tasks per reconcile_run exceeded", status=429)
                # Tier-3: max 10 actions per task
                _actions = _rl_conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE project_id=? AND type LIKE 'reconcile%' "
                    "AND json_extract(metadata_json, '$.reconcile_run_id')=?",
                    (project_id, _run_id),
                ).fetchone()[0]
                if _actions >= 10:
                    raise GovernanceError("rate_limit", "Tier-3: max 10 actions per reconcile_run exceeded", status=429)

    # --- Backlog gate: check bug_id for code-change task types (R1/R4) ---
    # Z3 observer-hotfix 2026-04-24 (P0-1 + P0-2):
    #   - Default enforce mode changed from 'warn' to 'strict' (P0-1).
    #     Rollback: set env OPT_BACKLOG_ENFORCE=warn to revert.
    #   - Added bug_id existence check in backlog_bugs (P0-2). Reject if bug_id
    #     given but not found in backlog_bugs. Prevents typo'd or fabricated IDs
    #     (observed 2026-04-24: MCP task_create silently dropped metadata and
    #     3 tasks landed with bug_id=missing in `warn` mode).
    #   - auto-chain internal creator is exempt (auto_chain already copies
    #     bug_id from parent's metadata; gate would create chicken-and-egg).
    _CODE_CHANGE_TYPES = ("pm", "dev", "test", "qa", "gatekeeper", "merge", "deploy")
    if task_type in _CODE_CHANGE_TYPES and created_by not in ("auto-chain", "auto-chain-retry"):
        _bug_id = metadata.get("bug_id") or ""
        _force_bypass = metadata.get("force_no_backlog") is True
        _enforce_mode = os.environ.get("OPT_BACKLOG_ENFORCE", "strict")
        if _force_bypass:
            # R3: tighter force_no_backlog requirements — validate before bypass
            _bypass_reason = metadata.get("force_reason", "")
            _mf_id = metadata.get("mf_id", "")
            if not _bypass_reason or len(_bypass_reason) < 30:
                _msg = "force_no_backlog requires force_reason of at least 30 chars"
                log.warning("backlog_gate: %s (mode=%s)", _msg, _enforce_mode)
                if _enforce_mode == "strict":
                    raise GovernanceError("force_reason too short", _msg, status=422)
            if not _mf_id or not re.match(r'^MF-\d{4}-\d{2}-\d{2}-\d{3}$', _mf_id):
                _msg = "force_no_backlog requires mf_id matching MF-YYYY-MM-DD-NNN"
                log.warning("backlog_gate: %s (mode=%s)", _msg, _enforce_mode)
                if _enforce_mode == "strict":
                    raise GovernanceError("mf_id invalid", _msg, status=422)
            # R4: observer bypass — audit the event
            if not _bypass_reason:
                _bypass_reason = "no reason given"
            try:
                _publish_event("backlog_gate.observer_bypass", {
                    "project_id": project_id,
                    "task_type": task_type,
                    "force_reason": _bypass_reason,
                    "created_by": created_by,
                })
                with DBContext(project_id) as _evt_conn:
                    _evt_conn.execute(
                        "INSERT INTO chain_events (root_task_id, task_id, event_type, payload_json, ts) "
                        "VALUES (?, ?, ?, ?, datetime('now'))",
                        ("backlog_gate", "backlog_gate",
                         "backlog_gate.observer_bypass",
                         json.dumps({"project_id": project_id, "task_type": task_type,
                                     "force_reason": _bypass_reason, "created_by": created_by})),
                    )
                    _evt_conn.commit()
            except Exception:
                log.debug("backlog_gate: failed to audit observer bypass", exc_info=True)
            log.info("backlog_gate: observer bypass for %s task (reason: %s)", task_type, _bypass_reason)
        elif not _bug_id:
            log.warning("backlog_gate: missing bug_id for %s task in project %s (mode=%s)",
                        task_type, project_id, _enforce_mode)
            if _enforce_mode == "strict":
                raise GovernanceError(
                    "bug_id required",
                    f"Task type '{task_type}' requires metadata.bug_id (OPT_BACKLOG_ENFORCE=strict). "
                    f"Set metadata.force_no_backlog=true with force_reason to bypass.",
                    status=422,
                )
        else:
            # P0-2: bug_id existence check — must correspond to a real backlog row
            try:
                with DBContext(project_id) as _chk_conn:
                    _row = _chk_conn.execute(
                        "SELECT status, bypass_policy_json FROM backlog_bugs WHERE bug_id = ?",
                        (_bug_id,),
                    ).fetchone()
            except Exception:
                _row = None
            if _row is None:
                log.warning("backlog_gate: bug_id %r not found in backlog_bugs for %s task (mode=%s)",
                            _bug_id, task_type, _enforce_mode)
                if _enforce_mode == "strict":
                    raise GovernanceError(
                        "bug_id not in backlog",
                        f"metadata.bug_id '{_bug_id}' does not exist in backlog_bugs. "
                        f"Create the backlog row first via POST /api/backlog/{project_id}/{_bug_id}, "
                        f"or set force_no_backlog=true with force_reason to bypass.",
                        status=422,
                    )
            elif _row["status"] not in ("OPEN", "IN_PROGRESS", "MF_IN_PROGRESS"):
                # R2: bug_id status must be active; MF_IN_PROGRESS is allowed
                # because manual fixes are now audited through backlog runtime.
                _bug_status = _row["status"]
                _msg = (f"bug_id {_bug_id} is not active (current status={_bug_status}); "
                        f"cannot attach new work to closed bug")
                log.warning("backlog_gate: %s (mode=%s)", _msg, _enforce_mode)
                if _enforce_mode == "strict":
                    raise GovernanceError("bug_id not open", _msg, status=422)
            else:
                _policy = backlog_runtime.parse_json_object(_row["bypass_policy_json"])
                if _policy:
                    metadata = backlog_runtime.merge_policy_into_metadata(metadata, _policy)

        if not _force_bypass:
            try:
                from .parallel_agent_contract import (
                    ParallelAgentContractError,
                    validate_parallel_agent_task_gate,
                )

                with DBContext(project_id) as _contract_conn:
                    _contract_evidence = validate_parallel_agent_task_gate(
                        _contract_conn,
                        project_id,
                        task_type,
                        metadata,
                    )
                if _contract_evidence:
                    metadata["parallel_contract_evidence"] = _contract_evidence
            except ParallelAgentContractError as exc:
                _msg = str(exc)
                log.warning("parallel_contract_gate: %s (mode=%s)", _msg, _enforce_mode)
                if _enforce_mode == "strict":
                    raise GovernanceError("parallel_contract_invalid", _msg, status=422)

        # R1: parent_task_id requirement for non-pm code-change types
        _PARENT_REQUIRED_TYPES = ("dev", "test", "qa", "gatekeeper", "merge", "deploy")
        if task_type in _PARENT_REQUIRED_TYPES and not _force_bypass:
            _parent_task_id = metadata.get("parent_task_id") or ""
            if not _parent_task_id:
                _msg = (
                    "This task_create entrypoint creates a chain task "
                    "(pm->dev->test->qa->merge). For V1 observer-led Manual "
                    "Fix work, do NOT use this entrypoint. Instead: "
                    "1) backlog_upsert with chain_trigger_json.parallel_contract "
                    "using the mf_parallel.v1 template; 2) task_timeline_append "
                    "events tied to mf_id. See aming-claw://mf-sop and "
                    "skills/aming-claw/SKILL.md 'Observer Operating Modes'. "
                    "If you genuinely need to test chain automation, pass "
                    "metadata.parent_task_id pointing to an existing pm task_id."
                )
                log.warning("backlog_gate: %s (type=%s, mode=%s)", _msg, task_type, _enforce_mode)
                if _enforce_mode == "strict":
                    raise GovernanceError("parent_task_id missing", _msg, status=422)
            else:
                # Verify parent_task_id exists in tasks table
                try:
                    with DBContext(project_id) as _ptid_conn:
                        _ptid_row = _ptid_conn.execute(
                            "SELECT task_id FROM tasks WHERE task_id = ?",
                            (_parent_task_id,),
                        ).fetchone()
                except Exception:
                    _ptid_row = None
                if _ptid_row is None:
                    _msg = "parent_task_id not found in tasks table"
                    log.warning("backlog_gate: %s (parent=%r, mode=%s)",
                                _msg, _parent_task_id, _enforce_mode)
                    if _enforce_mode == "strict":
                        raise GovernanceError("parent_task_id invalid", _msg, status=422)

    # Run conflict rules for user-facing task types (not auto-chain internal)
    rule_decision = None
    if task_type in ("pm", "dev", "coordinator") and created_by not in ("auto-chain", "auto-chain-retry"):
        with DBContext(project_id) as conn:
            rule_decision = check_conflicts(
                conn, project_id,
                target_files=metadata.get("target_files", []),
                operation_type=metadata["operation_type"],
                intent_hash=metadata["intent_hash"],
                prompt=prompt,
                depends_on=metadata.get("depends_on"),
            )
        metadata["rule_decision"] = rule_decision["decision"]
        metadata["rule_reason"] = rule_decision["reason"]
        log.info("API conflict_rules: project=%s decision=%s reason=%s",
                 project_id, rule_decision["decision"], rule_decision["reason"])

    # CR0b R2: scoped-task blocker — when an active reconcile session exists,
    # block new scoped (reconcile_*) task dispatch BEFORE inserting. Existing
    # in-flight tasks are NOT cancelled; only NEW dispatch is blocked.
    if task_type.startswith("reconcile_"):
        try:
            with DBContext(project_id) as _sess_conn:
                _active_sess = reconcile_session.get_active_session(_sess_conn, project_id)
        except Exception:
            log.debug("reconcile session lookup failed (non-critical)", exc_info=True)
            _active_sess = None
        if _active_sess is not None:
            return 409, {
                "error": "reconcile_session_active_blocks_scoped",
                "session_id": _active_sess.session_id,
                "task_type": task_type,
            }

    with DBContext(project_id) as conn:
        result = task_registry.create_task(
            conn, project_id,
            prompt=prompt,
            task_type=task_type,
            related_nodes=ctx.body.get("related_nodes"),
            created_by=created_by,
            priority=int(ctx.body.get("priority", 0)),
            max_attempts=int(ctx.body.get("max_attempts", 3)),
            metadata=metadata,
        )
        _created_bug_id = metadata.get("bug_id", "")
        if _created_bug_id:
            backlog_runtime.update_backlog_runtime(
                conn,
                _created_bug_id,
                f"{task_type}_queued",
                project_id=project_id,
                task_id=result.get("task_id", ""),
                task_type=task_type,
                metadata=metadata,
                runtime_state=result.get("status", "queued"),
            )
    # §2.2: Audit reconcile task creation (R3)
    if _is_reconcile:
        try:
            with DBContext(project_id) as _ac:
                audit_service.record(_ac, project_id, event="reconcile_task.created",
                                     actor=created_by, ok=True, node_ids=None, request_id="",
                                     task_id=result.get("task_id", ""), task_type=task_type)
        except Exception:
            log.debug("reconcile_task.created audit failed (non-critical)", exc_info=True)
    # Best-effort publish task.created event to event bus
    try:
        _publish_event("task.created", {
            "task_id": result.get("task_id"),
            "project_id": project_id,
            "type": task_type,
            "created_by": created_by,
        })
    except Exception:
        pass
    # Attach rule decision to response
    if rule_decision:
        result["rule_decision"] = rule_decision
    return result


@route("POST", "/api/task/{project_id}/claim")
def handle_task_claim(ctx: RequestContext):
    """Claim a task. Auth optional — uses principal_id if token provided, else body worker_id."""
    project_id = ctx.get_project_id()
    log.info("API task.claim: project=%s worker=%s", project_id, ctx.body.get("worker_id", "anonymous"))
    from . import task_registry
    worker_id = ctx.body.get("worker_id", "anonymous")
    if ctx.token:
        try:
            with DBContext(project_id) as conn:
                session = ctx.require_auth(conn)
                worker_id = session.get("principal_id", worker_id)
        except Exception:
            pass
    caller_pid = int(ctx.body.get("caller_pid", 0) or 0)
    with DBContext(project_id) as conn:
        claimed = task_registry.claim_task(conn, project_id, worker_id, caller_pid=caller_pid)
        if isinstance(claimed, tuple):
            task, fence_token = claimed
        else:
            task, fence_token = claimed, ""
        if task is None:
            return {"task": None, "message": "No tasks available"}
        metadata = task.get("metadata", {}) if isinstance(task, dict) else {}
        bug_id = metadata.get("bug_id", "")
        if bug_id:
            backlog_runtime.update_backlog_runtime(
                conn,
                bug_id,
                f"{task.get('type', 'task')}_claimed",
                project_id=project_id,
                task_id=task.get("task_id", ""),
                task_type=task.get("type", "task"),
                metadata=metadata,
                runtime_state="claimed",
            )
        return {"task": task, "fence_token": fence_token}


@route("POST", "/api/task/{project_id}/complete")
def handle_task_complete(ctx: RequestContext):
    """Complete a task. No auth required."""
    project_id = ctx.get_project_id()
    log.info("API task.complete: project=%s task=%s status=%s result_keys=%s",
             project_id, ctx.body.get("task_id", "?"), ctx.body.get("status", "?"),
             list((ctx.body.get("result") or {}).keys()))
    from . import task_registry
    with DBContext(project_id) as conn:
        return task_registry.complete_task(
            conn, ctx.body.get("task_id", ""),
            status=ctx.body.get("status", "succeeded"),
            result=ctx.body.get("result"),
            error_message=ctx.body.get("error_message", ""),
            fence_token=ctx.body.get("fence_token", ""),
            project_id=project_id,
            completed_by=ctx.body.get("worker_id", ""),
            override_reason=ctx.body.get("override_reason", ""),
        )


@route("POST", "/api/task/{project_id}/hold")
def handle_task_hold(ctx: RequestContext):
    """Put a queued task into observer_hold — stops executor and auto-chain from touching it."""
    project_id = ctx.get_project_id()
    from . import task_registry
    task_id = ctx.body.get("task_id", "")
    if not task_id:
        return {"error": "missing task_id"}, 400
    with DBContext(project_id) as conn:
        return task_registry.hold_task(conn, task_id)


@route("POST", "/api/task/{project_id}/cancel")
def handle_task_cancel(ctx: RequestContext):
    """Cancel a task. No auto-chain, no retry. Terminal state."""
    project_id = ctx.get_project_id()
    log.info("API task.cancel: project=%s task=%s", project_id, ctx.body.get("task_id", "?"))
    from . import task_registry
    with DBContext(project_id) as conn:
        return task_registry.cancel_task(
            conn,
            ctx.body.get("task_id", ""),
            ctx.body.get("reason", ""),
            project_id=project_id,
        )


@route("POST", "/api/task/{project_id}/release")
def handle_task_release(ctx: RequestContext):
    """Release an observer_hold task back to queued flow."""
    project_id = ctx.get_project_id()
    from . import task_registry
    task_id = ctx.body.get("task_id", "")
    if not task_id:
        return {"error": "missing task_id"}, 400
    with DBContext(project_id) as conn:
        return task_registry.release_task(conn, task_id)


@route("GET", "/api/project/{project_id}/observer-mode")
def handle_observer_mode_get(ctx: RequestContext):
    """Get current observer_mode flag for a project."""
    project_id = ctx.get_project_id()
    from . import task_registry
    with DBContext(project_id) as conn:
        enabled = task_registry.get_observer_mode(conn, project_id)
    return {"project_id": project_id, "observer_mode": enabled}


@route("POST", "/api/project/{project_id}/observer-mode")
def handle_observer_mode_set(ctx: RequestContext):
    """Enable or disable observer_mode. When on, all new tasks start as observer_hold."""
    project_id = ctx.get_project_id()
    from . import task_registry
    enabled = ctx.body.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.lower() in ("true", "1", "on")
    with DBContext(project_id) as conn:
        return task_registry.set_observer_mode(conn, project_id, bool(enabled))


@route("GET", "/api/task/{project_id}/list")
def handle_task_list(ctx: RequestContext):
    project_id = ctx.get_project_id()
    from . import task_registry
    with DBContext(project_id) as conn:
        tasks = task_registry.list_tasks(
            conn, project_id,
            status=ctx.query.get("status"),
            limit=int(ctx.query.get("limit", "50")),
        )
    return {"tasks": tasks, "count": len(tasks)}


@route("GET", "/api/task/{project_id}/subtask-group/{group_id}")
def handle_subtask_group(ctx: RequestContext):
    """Return subtask group status and member tasks (R8)."""
    project_id = ctx.get_project_id()
    group_id = ctx.path_params.get("group_id", "")
    if not group_id:
        raise GovernanceError("group_id is required", 400)
    with DBContext(project_id) as conn:
        group_row = conn.execute(
            "SELECT * FROM subtask_groups WHERE group_id = ? AND project_id = ?",
            (group_id, project_id),
        ).fetchone()
        if not group_row:
            raise GovernanceError(f"Subtask group not found: {group_id}", 404)
        tasks = conn.execute(
            """SELECT task_id, status, execution_status, type, subtask_local_id,
                      subtask_depends_on, created_at, updated_at, completed_at
               FROM tasks WHERE subtask_group_id = ?
               ORDER BY created_at ASC""",
            (group_id,),
        ).fetchall()
    return {
        "group_id": group_row["group_id"],
        "project_id": group_row["project_id"],
        "pm_task_id": group_row["pm_task_id"],
        "status": group_row["status"],
        "total_count": group_row["total_count"],
        "completed_count": group_row["completed_count"],
        "created_at": group_row["created_at"],
        "completed_at": group_row["completed_at"],
        "tasks": [dict(t) for t in tasks],
    }


@route("GET", "/api/task/{project_id}/trace/{trace_id}")
def handle_task_trace(ctx: RequestContext):
    """List all tasks sharing a trace_id, ordered by creation time."""
    project_id = ctx.get_project_id()
    trace_id = ctx.path_params.get("trace_id", "")
    if not trace_id:
        raise GovernanceError("trace_id is required", 400)
    with DBContext(project_id) as conn:
        rows = conn.execute(
            """SELECT task_id, status, type, prompt, assigned_to, created_by,
                      created_at, updated_at, trace_id, chain_id,
                      result_json, metadata_json
               FROM tasks
               WHERE project_id = ? AND trace_id = ?
               ORDER BY created_at ASC""",
            (project_id, trace_id),
        ).fetchall()
    tasks = [dict(r) for r in rows]
    return {"tasks": tasks, "count": len(tasks), "trace_id": trace_id}


@route("GET", "/api/task/{project_id}/{task_id}/gates")
def handle_task_gates(ctx: RequestContext):
    """List all gate events for a specific task."""
    project_id = ctx.get_project_id()
    task_id = ctx.path_params.get("task_id", "")
    if not task_id:
        raise GovernanceError("task_id is required", 400)
    with DBContext(project_id) as conn:
        rows = conn.execute(
            """SELECT id, gate_name, passed, reason, trace_id, created_at
               FROM gate_events
               WHERE project_id = ? AND task_id = ?
               ORDER BY created_at ASC""",
            (project_id, task_id),
        ).fetchall()
    events = [dict(r) for r in rows]
    return {"task_id": task_id, "gate_events": events, "count": len(events)}


@route("POST", "/api/task/{project_id}/timeline")
def handle_task_timeline_append(ctx: RequestContext):
    """Append task timeline evidence from executor/agent code."""
    project_id = ctx.get_project_id()
    from . import task_timeline

    with DBContext(project_id) as conn:
        return task_timeline.record_event(
            conn,
            project_id=project_id,
            task_id=ctx.body.get("task_id", ""),
            backlog_id=ctx.body.get("backlog_id", ""),
            mf_id=ctx.body.get("mf_id", ""),
            attempt_num=int(ctx.body.get("attempt_num", 0) or 0),
            event_type=ctx.body.get("event_type", ""),
            phase=ctx.body.get("phase", ""),
            event_kind=ctx.body.get("event_kind", ""),
            scenario_id=ctx.body.get("scenario_id", ""),
            parent_event_id=_query_int(ctx.body, "parent_event_id", 0),
            correlation_id=ctx.body.get("correlation_id", ""),
            severity=ctx.body.get("severity", ""),
            decision=ctx.body.get("decision", ""),
            schema_version=_query_int(ctx.body, "schema_version", 2),
            actor=ctx.body.get("actor", ""),
            status=ctx.body.get("status", ""),
            payload=ctx.body.get("payload") or {},
            verification=ctx.body.get("verification") or {},
            artifact_refs=ctx.body.get("artifact_refs") or {},
            trace_id=ctx.body.get("trace_id", ""),
            commit_sha=ctx.body.get("commit_sha", ""),
        )


@route("GET", "/api/task/{project_id}/timeline")
def handle_task_timeline_list(ctx: RequestContext):
    """List append-only task implementation timeline events by query filters."""
    project_id = ctx.get_project_id()
    task_id = _first_query_value(ctx.query, "task_id")
    backlog_id = _first_query_value(ctx.query, "backlog_id")
    trace_id = _first_query_value(ctx.query, "trace_id")
    phase = _first_query_value(ctx.query, "phase")
    event_kind = _first_query_value(ctx.query, "event_kind")
    scenario_id = _first_query_value(ctx.query, "scenario_id")
    correlation_id = _first_query_value(ctx.query, "correlation_id")
    severity = _first_query_value(ctx.query, "severity")
    decision = _first_query_value(ctx.query, "decision")
    try:
        parent_event_id = int(_first_query_value(ctx.query, "parent_event_id", "0") or "0")
    except (TypeError, ValueError):
        parent_event_id = 0
    try:
        limit = int(_first_query_value(ctx.query, "limit", "200") or "200")
    except (TypeError, ValueError):
        limit = 200
    from . import task_timeline

    with DBContext(project_id) as conn:
        events = task_timeline.list_events(
            conn,
            project_id,
            task_id=task_id,
            backlog_id=backlog_id,
            trace_id=trace_id,
            phase=phase,
            event_kind=event_kind,
            scenario_id=scenario_id,
            correlation_id=correlation_id,
            severity=severity,
            decision=decision,
            parent_event_id=parent_event_id,
            limit=limit,
        )
    return {
        "ok": True,
        "project_id": project_id,
        "task_id": task_id,
        "backlog_id": backlog_id,
        "trace_id": trace_id,
        "phase": phase,
        "event_kind": event_kind,
        "scenario_id": scenario_id,
        "correlation_id": correlation_id,
        "severity": severity,
        "decision": decision,
        "parent_event_id": parent_event_id,
        "events": events,
        "count": len(events),
    }


@route("GET", "/api/task/{project_id}/{task_id}/timeline")
def handle_task_timeline_get(ctx: RequestContext):
    """List append-only task implementation timeline events."""
    project_id = ctx.get_project_id()
    task_id = ctx.path_params.get("task_id", "")
    try:
        parent_event_id = int(_first_query_value(ctx.query, "parent_event_id", "0") or "0")
    except (TypeError, ValueError):
        parent_event_id = 0
    try:
        limit = int(_first_query_value(ctx.query, "limit", "200") or "200")
    except (TypeError, ValueError):
        limit = 200
    from . import task_timeline

    with DBContext(project_id) as conn:
        events = task_timeline.list_events(
            conn,
            project_id,
            task_id=task_id,
            backlog_id=_first_query_value(ctx.query, "backlog_id"),
            trace_id=_first_query_value(ctx.query, "trace_id"),
            phase=_first_query_value(ctx.query, "phase"),
            event_kind=_first_query_value(ctx.query, "event_kind"),
            scenario_id=_first_query_value(ctx.query, "scenario_id"),
            correlation_id=_first_query_value(ctx.query, "correlation_id"),
            severity=_first_query_value(ctx.query, "severity"),
            decision=_first_query_value(ctx.query, "decision"),
            parent_event_id=parent_event_id,
            limit=limit,
        )
    return {"task_id": task_id, "events": events, "count": len(events)}


@route("GET", "/api/runtime/{project_id}")
def handle_runtime(ctx: RequestContext):
    """Runtime projection — read-only view from Task Registry. No state of its own."""
    project_id = ctx.get_project_id()
    from . import task_registry, session_context
    with DBContext(project_id) as conn:
        active = task_registry.list_tasks(conn, project_id, status="running")
        queued = task_registry.list_tasks(conn, project_id, status="queued")
        claimed = task_registry.list_tasks(conn, project_id, status="claimed")
        pending_notify = task_registry.list_pending_notifications(conn, project_id)

    context = session_context.load_snapshot(project_id)

    return {
        "project_id": project_id,
        "active_tasks": active,
        "queued_tasks": queued,
        "claimed_tasks": claimed,
        "pending_notifications": pending_notify,
        "context": context,
        "summary": {
            "active": len(active),
            "queued": len(queued),
            "claimed": len(claimed),
            "pending_notify": len(pending_notify),
        },
    }


@route("POST", "/api/task/{project_id}/progress")
def handle_task_progress(ctx: RequestContext):
    """Update task progress heartbeat."""
    project_id = ctx.get_project_id()
    from . import task_registry
    with DBContext(project_id) as conn:
        return task_registry.update_progress(
            conn, ctx.body.get("task_id", ""),
            phase=ctx.body.get("phase", "running"),
            percent=int(ctx.body.get("percent", 0)),
            message=ctx.body.get("message", ""),
        )


@route("POST", "/api/task/{project_id}/notify")
def handle_task_notify(ctx: RequestContext):
    """Mark task notification as sent."""
    project_id = ctx.get_project_id()
    from . import task_registry
    with DBContext(project_id) as conn:
        return task_registry.mark_notified(conn, ctx.body.get("task_id", ""))


@route("POST", "/api/task/{project_id}/recover")
def handle_task_recover(ctx: RequestContext):
    """Recover stale tasks with expired leases."""
    project_id = ctx.get_project_id()
    from . import task_registry
    with DBContext(project_id) as conn:
        return task_registry.recover_stale_tasks(conn, project_id)


# --- Health ---

@route("GET", "/api/health")
def handle_health(ctx: RequestContext):
    return {"status": "ok", "service": "governance", "port": PORT,
            "version": get_server_version(), "pid": SERVER_PID}


@route("GET", "/api/version-check/{project_id}")
def handle_version_check(ctx: RequestContext):
    """Check chain version vs git HEAD.

    Phase A hybrid: reads DB state (synced by executor) AND derives trailer
    state from git. Returns 'source' field indicating where version came from.
    """
    pid = ctx.get_project_id()
    conn = get_connection(pid)

    # Derive trailer state (best-effort, non-blocking)
    trailer_state = None
    try:
        from .chain_trailer import get_chain_state
        trailer_state = get_chain_state()
    except Exception as e:
        log.debug("version-check: chain_trailer unavailable: %s", e)

    row = conn.execute(
        "SELECT chain_version, updated_at, git_head, dirty_files, git_synced_at "
        "FROM project_version WHERE project_id=?", (pid,)
    ).fetchone()

    # Runtime version baking — detect stale-process-after-deploy
    gov_runtime = ""
    sm_runtime = ""
    runtime_match = False
    try:
        from .chain_trailer import get_runtime_version
        gov_runtime = get_runtime_version()
    except Exception as e:
        log.debug("version-check: gov runtime_version unavailable: %s", e)
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:40101/api/manager/health", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            sm_data = json.loads(resp.read().decode())
            sm_runtime = sm_data.get("runtime_version", "")
    except Exception as e:
        log.debug("version-check: sm runtime_version unavailable: %s", e)

    if not row:
        source = trailer_state["source"] if trailer_state else "none"
        version = trailer_state["version"] if trailer_state else "unknown"
        runtime_match = bool(gov_runtime and (gov_runtime.startswith(version) or version.startswith(gov_runtime))
                             and sm_runtime and (sm_runtime.startswith(version) or version.startswith(sm_runtime)))
        return {
            "ok": True, "project_id": pid,
            "head": version if trailer_state else "unknown",
            "governance_synced_head": "",
            "trailer_head": trailer_state.get("version", "") if trailer_state else "",
            "chain_version": version if trailer_state else "(not set)",
            "dirty": trailer_state["dirty"] if trailer_state else False,
            "dirty_files": trailer_state["dirty_files"] if trailer_state else [],
            "source": source,
            "message": "Project not initialized" + (f" (trailer source: {source})" if trailer_state else ""),
            "generated_at": _utc_now(), "project_version": version if trailer_state else "unknown",
            "gov_runtime_version": gov_runtime,
            "sm_runtime_version": sm_runtime,
            "runtime_match": runtime_match,
        }

    # R1/R2: Trailer-priority chain_ver. When trailer source='trailer', the git
    # log--first-parent trailer is authoritative (auto_chain._gate_version_check
    # uses chain_state.chain_sha for the same reason). Fall back to DB row
    # otherwise (preserves prior behavior, including post-deploy DB sync state).
    if trailer_state and trailer_state.get("source") == "trailer":
        chain_ver = trailer_state.get("version", "") or trailer_state.get("chain_sha", "")
    else:
        chain_ver = row["chain_version"]
    git_head = row["git_head"] or ""
    dirty_files_raw = json.loads(row["dirty_files"] or "[]")
    # B31: apply shared dirty-worktree filter (same policy as auto_chain/scope reconcile)
    dirty_files = filter_dirty_files(dirty_files_raw)
    git_synced = row["git_synced_at"] or ""

    # Determine source: prefer trailer if available
    source = "db"
    if trailer_state:
        source = trailer_state["source"]  # 'trailer' or 'head'
        # Merge trailer dirty_files with DB dirty_files (union, filtered)
        if trailer_state.get("dirty_files"):
            trailer_dirty = filter_dirty_files(trailer_state["dirty_files"])
            for f in trailer_dirty:
                if f not in dirty_files:
                    dirty_files.append(f)

    # Compare
    ok = True
    parts = []

    if not git_head:
        parts.append("Executor has not synced git status yet")
    elif not (git_head.startswith(chain_ver) or chain_ver.startswith(git_head)):
        ok = False
        parts.append(f"HEAD ({git_head}) != CHAIN_VERSION ({chain_ver})")
    if dirty_files:
        ok = False
        parts.append(f"{len(dirty_files)} uncommitted files")

    runtime_match = bool(gov_runtime and (gov_runtime.startswith(chain_ver) or chain_ver.startswith(gov_runtime))
                         and sm_runtime and (sm_runtime.startswith(chain_ver) or chain_ver.startswith(sm_runtime)))
    return {
        "ok": ok,
        "project_id": pid,
        "head": git_head or (trailer_state["version"] if trailer_state else "unknown"),
        "governance_synced_head": git_head,
        "trailer_head": trailer_state.get("version", "") if trailer_state else "",
        "chain_version": chain_ver,
        "chain_updated_at": row["updated_at"],
        "dirty": bool(dirty_files),
        "dirty_files": dirty_files,
        "git_synced_at": git_synced,
        "source": source,
        "message": "; ".join(parts),
        "generated_at": _utc_now(),
        "project_version": chain_ver,
        "gov_runtime_version": gov_runtime,
        "sm_runtime_version": sm_runtime,
        "runtime_match": runtime_match,
    }


@route("POST", "/api/version-sync/{project_id}")
def handle_version_sync(ctx: RequestContext):
    """Executor syncs git status from host machine. Lightweight, no auth."""
    pid = ctx.get_project_id()
    body = ctx.body or {}

    git_head = body.get("git_head", "")
    dirty_files = body.get("dirty_files", [])
    if not git_head:
        return {"error": "missing git_head"}, 400

    now = _utc_now()

    def _do_sync():
        conn = independent_connection(pid)
        try:
            conn.execute("""
                UPDATE project_version
                SET git_head = ?, dirty_files = ?, git_synced_at = ?
                WHERE project_id = ?
            """, (git_head, json.dumps(dirty_files), now, pid))
            conn.commit()
        finally:
            conn.close()

    _retry_on_busy(_do_sync)
    return {"ok": True, "git_head": git_head, "dirty_files": dirty_files, "synced_at": now}


@route("POST", "/api/version-update/{project_id}")
def handle_version_update(ctx: RequestContext):
    """DEPRECATED (Phase A §4.4): All writes are ignored. Returns git-derived chain_version.

    R7: This endpoint no longer writes to project_version. It logs a deprecation
    warning, audits the ignored call, and returns the current chain state derived
    from git trailers. The endpoint is preserved (R10) but neutered.
    """
    pid = ctx.get_project_id()
    body = ctx.body or {}
    now = _utc_now()

    log.warning("deprecated_write_ignored: handle_version_update called for %s by %s — "
                "writes are no longer accepted (Phase A §4.4, R7)",
                pid, body.get("updated_by", "unknown"))

    # Audit the ignored call
    conn = independent_connection(pid)
    try:
        audit_service.record(
            conn, pid, "version.update_attempt",
            actor=body.get("updated_by", "unknown"),
            details={
                "task_id": body.get("task_id", ""),
                "new_version": body.get("chain_version", ""),
                "updated_by": body.get("updated_by", ""),
                "result": "deprecated_write_ignored",
                "reason": "Phase A: version-update writes are ignored; git trailers are source of truth",
            },
        )
        conn.commit()
    except Exception as e:
        log.debug("version-update audit failed (non-fatal): %s", e)
    finally:
        conn.close()

    # Return git-derived chain state
    derived_state = None
    try:
        from .chain_trailer import get_chain_state
        derived_state = get_chain_state()
    except Exception as e:
        log.debug("version-update: chain_trailer unavailable: %s", e)

    chain_version = derived_state["chain_sha"] if derived_state else "unknown"
    result = {
        "ok": True,
        "chain_version": chain_version,
        "updated_at": now,
        "deprecated_write_ignored": True,
        "source": "git_trailer",
    }
    if derived_state:
        result["derived_state"] = derived_state
    return result


def _audit_version_update(conn, pid, body, result, reason):
    """Write audit record for every version-update attempt."""
    try:
        audit_service.record(
            conn, pid, "version.update_attempt",
            actor=body.get("updated_by", "unknown"),
            details={
                "task_id": body.get("task_id", ""),
                "old_version": body.get("old_version", ""),
                "new_version": body.get("chain_version", ""),
                "chain_stage": body.get("chain_stage", ""),
                "updated_by": body.get("updated_by", ""),
                "manual_fix_reason": body.get("manual_fix_reason", ""),
                "result": result,
                "reject_reason": reason,
            },
        )
    except Exception:
        pass  # audit failure should not block


# --- Redeploy-after-merge endpoint ---

@route("POST", "/api/governance/redeploy-after-merge/{project_id}")
def handle_redeploy_after_merge(ctx: RequestContext):
    """Audit-only ack; executor (deploy_chain) orchestrates SM calls."""
    pid = ctx.get_project_id()
    body = ctx.body
    task_id = body.get("task_id", "")
    chain_version = body.get("chain_version", "")
    with DBContext(pid) as conn:
        try:
            audit_service.record(conn, pid, "redeploy_after_merge.requested",
                                 actor="deploy_chain", details={"task_id": task_id, "chain_version": chain_version})
        except Exception:
            pass
    return {"ok": True, "message": "audit recorded; executor must orchestrate sm calls"}


# --- Redeploy Endpoints (PR-2) ---

@route("POST", "/api/governance/redeploy/executor")
def handle_redeploy_executor(ctx: RequestContext):
    """Redeploy executor via 5-step pipeline. See redeploy_handler.py."""
    from .redeploy_handler import handle_redeploy_executor as _handler
    return _handler(ctx)


@route("POST", "/api/governance/redeploy/gateway")
def handle_redeploy_gateway(ctx: RequestContext):
    """Redeploy gateway via 5-step pipeline. See redeploy_handler.py."""
    from .redeploy_handler import handle_redeploy_gateway as _handler
    return _handler(ctx)


@route("POST", "/api/governance/redeploy/coordinator")
def handle_redeploy_coordinator(ctx: RequestContext):
    """Redeploy coordinator via 5-step pipeline. See redeploy_handler.py."""
    from .redeploy_handler import handle_redeploy_coordinator as _handler
    return _handler(ctx)


@route("POST", "/api/governance/redeploy/service_manager")
def handle_redeploy_service_manager(ctx: RequestContext):
    """Redeploy service_manager via 5-step pipeline. See redeploy_handler.py."""
    from .redeploy_handler import handle_redeploy_service_manager as _handler
    return _handler(ctx)


@route("GET", "/api/metrics")
def handle_metrics(ctx: RequestContext):
    """Return in-memory metrics snapshot."""
    from .observability import get_metrics
    return get_metrics()


@route("GET", "/api/health/deep")
def handle_deep_health(ctx: RequestContext):
    """Deep health check: Redis, SQLite, outbox, queues."""
    from .observability import check_outbox_health
    checks = {"governance": "ok", "port": PORT}

    # Redis
    rc = get_redis()
    checks["redis"] = "ok" if rc.available else "degraded"

    # Outbox alerts
    alerts = []
    for p in project_service.list_projects():
        alerts.extend(check_outbox_health(p["project_id"]))
    checks["alerts"] = alerts
    checks["alert_count"] = len(alerts)

    return checks


@route("GET", "/api/context-snapshot/{project_id}")
def handle_context_snapshot(ctx: RequestContext):
    """Return minimal base context for AI session startup (~500 tokens).

    Single API call providing point-in-time consistent snapshot.
    AI can query on-demand APIs for more details.
    """
    pid = ctx.get_project_id()
    conn = get_connection(pid)
    role_raw = ctx.query.get("role", "coordinator")
    task_id_raw = ctx.query.get("task_id", "")
    role = role_raw[0] if isinstance(role_raw, list) else role_raw
    task_id = task_id_raw[0] if isinstance(task_id_raw, list) else task_id_raw
    now = _utc_now()

    # Task summary — recent 3 tasks
    task_summary = []
    try:
        for row in conn.execute(
            "SELECT task_id, type, status FROM tasks ORDER BY created_at DESC LIMIT 3"
        ).fetchall():
            task_summary.append({
                "task_id": row["task_id"],
                "type": row["type"],
                "status": row["status"],
            })
    except Exception:
        pass

    # Project state
    ver_row = conn.execute(
        "SELECT chain_version, updated_at, dirty_files FROM project_version WHERE project_id=?",
        (pid,)
    ).fetchone()
    dirty_files = json.loads(ver_row["dirty_files"] or "[]") if ver_row and ver_row["dirty_files"] else []
    project_state = {
        "chain_version": ver_row["chain_version"] if ver_row else "unknown",
        "dirty": bool(dirty_files),
    }

    # Node summary (one-line)
    node_counts = {}
    for row in conn.execute(
        "SELECT verify_status, COUNT(*) as cnt FROM node_state WHERE project_id=? GROUP BY verify_status",
        (pid,)
    ).fetchall():
        node_counts[row["verify_status"]] = row["cnt"]

    # Session context snapshot from DB/Redis
    session_snapshot = None
    try:
        from . import session_context
        session_snapshot = session_context.load_snapshot(pid)
    except Exception:
        pass

    # Recent memories (top 3 by relevance)
    recent_memories = []
    try:
        all_mems = memory_service.query_all(pid, active_only=True)
        task_prompt = ""
        if session_snapshot:
            task_prompt = (
                session_snapshot.get("current_focus", "")
                or session_snapshot.get("last_decision", "")
            )
        scored = []
        for m in all_mems:
            score = 0
            s = m.get("structured", {}) or {}
            if s.get("followup_needed"):
                score += 10
            if m.get("kind") == "failure_pattern":
                score += 5
            if m.get("kind") == "decision":
                score += 2
            if m.get("module", "") and m["module"] in task_prompt:
                score += 3
            scored.append((score, m))
        scored.sort(key=lambda x: -x[0])
        for _, m in scored[:3]:
            recent_memories.append({
                "module": m.get("module", ""),
                "kind": m.get("kind", ""),
                "content": (m.get("content", ""))[:200],
            })
    except Exception:
        pass

    # Task chain context (if task_id provided)
    task_chain = None
    if task_id:
        try:
            from .chain_context import get_store
            task_chain = get_store().get_chain(task_id, role=role)
        except Exception:
            pass

    result = {
        "snapshot_at": now,
        "project_id": pid,
        "role": role,
        "task_summary": task_summary,
        "project_state": project_state,
        "node_summary": node_counts,
        "recent_memories": recent_memories,
        "constraints": "All changes through auto-chain",
        "generated_at": now,
        "project_version": project_state["chain_version"],
    }
    if session_snapshot:
        result["session_context"] = {
            "current_focus": session_snapshot.get("current_focus", ""),
            "last_decision": session_snapshot.get("last_decision", ""),
            "version": session_snapshot.get("version", 0),
            "updated_at": session_snapshot.get("updated_at", ""),
        }
    if task_chain:
        result["task_chain"] = task_chain
    return result


# --- Documentation ---

_DOCS = {
    "overview": {
        "title": "Governance Service Overview",
        "description": "Workflow governance service for multi-agent coordination. Manages project initialization, role assignment, node verification, release gating, memory, and audit.",
        "base_url": "http://localhost:40000",
        "api_prefix": "/api",
        "gateway_prefix": "/gateway",
        "auth": "No authentication required. All APIs work without tokens. Optional X-Gov-Token header is accepted for backward compatibility but not enforced.",
    },
    "quickstart": {
        "title": "Coordinator Session Quickstart",
        "base_url": "http://localhost:40000",
        "prerequisites": "Human has already run init_project.py and has the coordinator refresh_token (gov-xxx).",
        "steps": [
            {
                "step": 1,
                "phase": "AUTH",
                "action": "Exchange refresh_token for access_token (4h TTL)",
                "method": "POST /api/token/refresh",
                "body": {"refresh_token": "gov-xxx (from init_project.py)"},
                "returns": "access_token (gat-xxx), expires_in_sec, session_id, project_id, role",
                "note": "Use access_token for all subsequent API calls. Auto-renew before expiry.",
            },
            {
                "step": 2,
                "phase": "LIFECYCLE",
                "action": "Register agent and get a lease",
                "method": "POST /api/agent/register",
                "headers": {"X-Gov-Token": "gat-xxx (access_token)"},
                "body": {"project_id": "amingClaw", "expected_duration_sec": 3600},
                "returns": "lease_id, heartbeat_interval_sec (120s)",
                "note": "Heartbeat every 2 min to renew lease. Lease expires in 5 min without heartbeat.",
            },
            {
                "step": 3,
                "phase": "CONTEXT",
                "action": "Load previous session context (if any)",
                "method": "GET /api/context/{project_id}/load",
                "headers": {"X-Gov-Token": "gat-xxx"},
                "returns": "{context: {...}, exists: true/false}",
                "note": "Contains current_focus, active_nodes, pending_tasks, recent_messages from last session.",
            },
            {
                "step": 4,
                "phase": "CONTEXT",
                "action": "Assemble task-aware context from memory",
                "method": "POST /api/context/{project_id}/assemble",
                "headers": {"X-Gov-Token": "gat-xxx"},
                "body": {"task_type": "dev_general", "token_budget": 5000},
                "returns": "Prioritized memories (pitfalls, decisions, architecture) within token budget",
                "note": "Task types: dev_general, telegram_handler, verify_node, code_review, release_check",
            },
            {
                "step": 5,
                "phase": "TELEGRAM",
                "action": "Bind to Telegram chat for message relay",
                "method": "POST /gateway/bind",
                "body": {"token": "gat-xxx", "chat_id": 7848961760, "project_id": "amingClaw"},
                "note": "After binding, user messages in Telegram are pushed to Redis Stream chat:inbox:{hash}.",
            },
            {
                "step": 6,
                "phase": "TELEGRAM",
                "action": "Consume messages from Redis Stream",
                "code": "from telegram_gateway.chat_proxy import ChatProxy\nproxy = ChatProxy(token='gat-xxx', gateway_url='http://localhost:40000', redis_url='redis://localhost:40079/0')\nproxy.start(on_message=handler)  # background thread",
                "note": "ChatProxy uses XREADGROUP+ACK. Unacked messages survive crashes.",
            },
            {
                "step": 7,
                "phase": "WORK",
                "action": "Check project status",
                "method": "GET /api/wf/{project_id}/summary",
                "headers": {"X-Gov-Token": "gat-xxx"},
                "returns": "{total_nodes, by_status: {pending, testing, t2_pass, qa_pass, ...}}",
            },
            {
                "step": 8,
                "phase": "WORK",
                "action": "Verify nodes (tester role)",
                "method": "POST /api/wf/{project_id}/verify-update",
                "headers": {"X-Gov-Token": "tester-token"},
                "body": {
                    "nodes": ["L1.3"],
                    "status": "t2_pass",
                    "evidence": {"type": "test_report", "producer": "tester-001", "tool": "pytest", "summary": {"passed": 42, "failed": 0}},
                },
                "note": "Flow: pending->testing->t2_pass (tester), t2_pass->qa_pass (qa). Evidence required.",
            },
            {
                "step": 9,
                "phase": "WORK",
                "action": "Reply to Telegram user",
                "method": "POST /gateway/reply",
                "body": {"token": "gat-xxx", "chat_id": 7848961760, "text": "Task completed"},
                "note": "Or use proxy.reply('text') from ChatProxy.",
            },
            {
                "step": 10,
                "phase": "SAVE",
                "action": "Save session context before exit",
                "method": "POST /api/context/{project_id}/save",
                "headers": {"X-Gov-Token": "gat-xxx"},
                "body": {"context": {"current_focus": "...", "active_nodes": ["..."], "pending_tasks": ["..."], "recent_messages": []}},
                "note": "Use expected_version for optimistic locking. Context persists to Redis (24h TTL) + SQLite.",
            },
            {
                "step": 11,
                "phase": "EXIT",
                "action": "Deregister agent",
                "method": "POST /api/agent/deregister",
                "body": {"lease_id": "lease-xxx"},
                "note": "Releases lease. Gateway detects offline, queues messages for next session.",
            },
        ],
        "lifecycle_summary": "AUTH(token) -> LIFECYCLE(register) -> CONTEXT(load+assemble) -> TELEGRAM(bind+consume) -> WORK(verify+reply) -> SAVE(context) -> EXIT(deregister)",
    },
    "endpoints": {
        "title": "API Endpoints",
        "groups": {
            "init": {
                "POST /api/init": "Create project + get coordinator token. Repeat with password to reset token.",
            },
            "project": {
                "GET /api/project/list": "List all projects with node counts.",
            },
            "role": {
                "POST /api/role/assign": "Coordinator assigns role+token to agent. Body: {project_id, principal_id, role}",
                "POST /api/role/revoke": "Revoke agent session. Body: {project_id, session_id}",
                "POST /api/role/heartbeat": "Agent keepalive. Body: {project_id?, status?}",
                "GET /api/role/verify": "Verify token, returns session info. Used by Gateway.",
                "GET /api/role/{project_id}/sessions": "List active sessions for a project.",
            },
            "workflow": {
                "POST /api/wf/{project_id}/import-graph": "Import acceptance graph from markdown.",
                "POST /api/wf/{project_id}/verify-update": "Update node verification status. Body: {nodes, status, evidence}",
                "POST /api/wf/{project_id}/baseline": "Batch set historical state (coordinator only). Body: {nodes: {id: status}, reason}",
                "POST /api/wf/{project_id}/release-gate": "Check if all nodes pass for release.",
                "POST /api/wf/{project_id}/rollback": "Rollback node state to a version.",
                "GET /api/wf/{project_id}/summary": "Status summary (counts by status).",
                "GET /api/wf/{project_id}/node/{node_id}": "Single node details.",
                "GET /api/wf/{project_id}/export": "Export graph as JSON or Mermaid. Query: format=json|mermaid",
                "GET /api/wf/{project_id}/impact": "File change impact analysis. Query: files=a.py,b.py",
            },
            "memory": {
                "POST /api/mem/{project_id}/write": "Write memory entry. Body: {module, kind, content, related_nodes?, supersedes?}",
                "GET /api/mem/{project_id}/query": "Query memory. Query: module=, kind=, node=",
            },
            "audit": {
                "GET /api/audit/{project_id}/log": "Query audit log. Query: limit=, event=, since=",
                "GET /api/audit/{project_id}/violations": "Query violations. Query: limit=, since=",
            },
        },
    },
    "workflow_rules": {
        "title": "Workflow Verification Rules",
        "status_flow": {
            "states": ["pending", "testing", "t2_pass", "qa_pass", "failed", "waived", "skipped"],
            "transitions": {
                "pending": ["testing"],
                "testing": ["t2_pass", "failed"],
                "t2_pass": ["qa_pass", "failed"],
                "qa_pass": "(terminal - verified)",
                "failed": ["testing"],
            },
        },
        "role_permissions": {
            "coordinator": "Can do everything: baseline, assign roles, rollback, import graph, verify-update.",
            "tester": "Can transition: pending->testing, testing->t2_pass/failed.",
            "qa": "Can transition: t2_pass->qa_pass/failed.",
            "dev": "Can transition: pending->testing, testing->t2_pass/failed (same as tester).",
            "observer": "Read-only. Can query status, summary, export.",
        },
        "evidence_format": {
            "description": "Evidence must be a dict, not a string.",
            "required_fields": ["type", "producer"],
            "optional_fields": ["tool", "summary", "artifact_uri", "checksum", "created_at"],
            "example": {
                "type": "test_report",
                "producer": "tester-001",
                "tool": "pytest",
                "summary": {"passed": 42, "failed": 0},
            },
        },
        "verify_update_example": {
            "method": "POST /api/wf/{project_id}/verify-update",
            "headers": {"X-Gov-Token": "agent-token"},
            "body": {
                "nodes": ["L1.3"],
                "status": "t2_pass",
                "evidence": {
                    "type": "test_report",
                    "producer": "tester-001",
                    "tool": "pytest",
                    "summary": {"passed": 10, "failed": 0},
                },
            },
        },
        "gate_rules": "Nodes with dependencies (gates) cannot advance until upstream nodes satisfy their gate policy. Use GET /api/wf/{project_id}/node/{node_id} to check gate status.",
        "release_gate": "POST /api/wf/{project_id}/release-gate checks if all nodes in scope are qa_pass. Returns {release: true/false, blocking_nodes: [...]}.",
    },
    "memory_guide": {
        "title": "Memory Service Guide",
        "description": "Store and query development knowledge (patterns, pitfalls, decisions, workarounds) per project.",
        "kinds": ["decision", "pitfall", "workaround", "invariant", "ownership", "pattern"],
        "write_example": {
            "method": "POST /api/mem/{project_id}/write",
            "headers": {"X-Gov-Token": "token"},
            "body": {
                "module": "auth",
                "kind": "pitfall",
                "content": "Never store session tokens in localStorage - use httpOnly cookies.",
                "related_nodes": ["L2.3"],
                "applies_when": "Implementing any auth-related feature",
            },
        },
        "query_examples": [
            "GET /api/mem/{project_id}/query?module=auth",
            "GET /api/mem/{project_id}/query?kind=pitfall",
            "GET /api/mem/{project_id}/query?node=L2.3",
        ],
    },
    "telegram_integration": {
        "title": "Telegram Gateway Integration (v5.1)",
        "description": "Gateway handles only message sending/receiving. Non-command messages start a Coordinator CLI session. Coordinator handles conversation, decisions, and task orchestration.",
        "architecture": "Telegram <-> Gateway (Docker) -> Claude CLI session (Coordinator) -> Governance API",
        "v5_1_change": "Gateway no longer classifies query/task/chat and no longer creates tasks directly. All decision-making belongs to Coordinator.",
        "role_boundary": {
            "gateway": "Message sending/receiving + /command handling. No decision-making, no task creation.",
            "coordinator": "Conversation + decision-making + task orchestration. Does not write code itself.",
            "dev_executor": "Code execution. Does not interact with users.",
        },
        "gateway_api": {
            "POST /gateway/bind": "Bind coordinator token to chat_id. Body: {token, chat_id, project_id}",
            "POST /gateway/reply": "Send message to Telegram. Body: {token, chat_id?, text}. If no chat_id, uses bound chat.",
            "POST /gateway/unbind": "Unbind chat_id. Body: {chat_id}",
            "GET /gateway/health": "Gateway health check.",
            "GET /gateway/status": "List all active routes (bound coordinators).",
        },
        "message_flow": {
            "user_to_coordinator": "User sends text -> Gateway launches Claude CLI session (Coordinator) with context -> Coordinator processes -> reply via Gateway",
            "coordinator_to_user": "Coordinator stdout -> Gateway sends to Telegram",
            "task_creation": "Only Coordinator can create tasks (POST /api/task/create). Gateway cannot.",
            "governance_events": "Governance publishes events to Redis gov:events:{project_id} -> Gateway formats and sends to admin chat",
        },
        "telegram_commands": {
            "/menu": "Interactive menu showing registered coordinators with switch buttons",
            "/bind <token>": "Bind coordinator to current chat",
            "/unbind": "Unbind current coordinator",
            "/status [project]": "Show project verification status",
            "/projects": "List all projects",
            "/health": "Service health check",
        },
    },
    "coverage_check": {
        "title": "Feature Coverage Check (workflow assurance)",
        "description": "Detect untracked code changes before release. Reverse impact analysis: checks if all changed files have corresponding acceptance graph nodes.",
        "problem_solved": "Prevents features from being shipped without workflow tracking. Catches cases where developers implement code without first creating acceptance nodes.",
        "api": {
            "POST /api/wf/{project_id}/coverage-check": {
                "description": "Check if changed files are covered by acceptance graph nodes.",
                "headers": {"X-Gov-Token": "required"},
                "body": {
                    "files": ["agent/governance/outbox.py", "agent/new_feature.py"],
                },
                "returns": {
                    "covered": [{"file": "agent/governance/outbox.py", "nodes": ["L5.2", "L7.2"]}],
                    "uncovered": [{"file": "agent/new_feature.py", "suggestion": "Create a new node..."}],
                    "coverage_pct": 50.0,
                    "pass": False,
                },
            },
        },
        "integration_with_release_gate": {
            "description": "Run coverage-check before release-gate. If pass=false, block release until all files have nodes.",
            "recommended_flow": [
                "1. git diff --name-only main..HEAD → get changed files",
                "2. POST /api/wf/{pid}/coverage-check {files: [...]}",
                "3. If pass=false → create missing nodes, verify them",
                "4. POST /api/wf/{pid}/release-gate {profile: 'full'}",
            ],
        },
        "gate_types": {
            "L9.1 Feature Coverage Check": "Checks file→node mapping. Uncovered files → warning/block.",
            "L9.2 Node-Before-Code Gate": "verify-update checks if evidence.changed_files are all covered by some node's primary/secondary. Enforces 'create node before writing code'.",
            "L9.3 Artifacts Check": "qa_pass time checks if companion deliverables (api_docs, tests) are complete.",
            "L9.5 Gatekeeper Coverage": "release-gate auto-checks latest coverage-check result. No run / stale / failed → block release.",
        },
    },
    "gatekeeper": {
        "title": "Gatekeeper (pre-release validation)",
        "description": "Gatekeeper is a program (not an AI role) embedded in the governance service. It enforces pre-release checks at two levels: verify-update time and release-gate time.",
        "check_points": {
            "verify-update (pre-check intercept)": {
                "when": "Any node transitions to t2_pass or qa_pass",
                "what": "Checks that the node's declared primary files are all covered by graph nodes",
                "blocks": "If primary files are uncovered → rejects verify-update with error message",
                "module": "state_service._check_node_coverage → coverage_check.check_feature_coverage",
            },
            "release-gate (release intercept)": {
                "when": "POST /api/wf/{pid}/release-gate is called",
                "what": "Checks that a coverage-check was run recently (within 1 hour) and passed",
                "blocks": "If never run → 'Run coverage-check first'. If stale → 'Re-run'. If failed → 'Uncovered files'.",
                "module": "gatekeeper.verify_pre_release → reads gatekeeper_checks table",
            },
        },
        "api": {
            "POST /api/wf/{project_id}/coverage-check": {
                "description": "Run coverage check AND auto-record result for gatekeeper.",
                "body": {"files": ["agent/governance/server.py"]},
                "side_effect": "Result written to gatekeeper_checks table for release-gate to read.",
            },
            "POST /api/wf/{project_id}/artifacts-check": {
                "description": "Check if nodes have required companion artifacts (docs, tests).",
                "body": {"nodes": ["L9.3"]},
            },
            "POST /api/wf/{project_id}/release-gate": {
                "description": "Release gate now includes gatekeeper check automatically.",
                "gatekeeper_field": "Response includes 'gatekeeper': {pass, checks, missing, stale}",
            },
        },
        "flow": [
            "1. Developer changes code",
            "2. POST /api/wf/{pid}/coverage-check {files: [changed files]}",
            "3a. pass:true → gatekeeper records pass → can proceed to release",
            "3b. pass:false → create missing nodes → re-run coverage-check",
            "4. POST /api/wf/{pid}/release-gate → gatekeeper auto-checks latest coverage result",
            "5. All pass → release approved",
        ],
        "storage": "gatekeeper_checks table in project SQLite DB. Each coverage-check auto-records.",
        "config": {
            "max_age_sec": "3600 (1 hour). Stale results require re-running coverage-check.",
            "required_checks": ["coverage_check"],
            "future_checks": ["security_scan", "dependency_audit", "performance_regression"],
        },
        "artifacts_auto_infer": {
            "title": "L9.6 Artifacts Auto-Inference",
            "description": "Nodes without explicit artifacts: declaration are auto-analyzed. If primary files contain @route → api_docs required. If test files declared → test_file required.",
            "rules": [
                "primary .py file has @route() → auto-require api_docs (section inferred from title)",
                "node declares test:[] with files → auto-require test_file existence",
                "declared artifacts take precedence over inferred",
            ],
            "module": "artifacts.infer_required_artifacts",
        },
        "deploy_coverage_check": {
            "title": "L9.7 Deploy Pre-flight Coverage-Check",
            "description": "deploy-governance.sh automatically runs coverage-check before building. Uncovered files block deployment.",
            "usage": "GOV_COORDINATOR_TOKEN=gov-xxx ./deploy-governance.sh",
            "bypass": "SKIP_COVERAGE_CHECK=1 ./deploy-governance.sh (not recommended)",
            "limitation": "Only protects deploy-governance.sh path. docker compose up --build bypasses this check.",
            "mitigation": "verify_loop.sh should be run after any deployment to catch violations.",
        },
        "verify_loop": {
            "title": "Post-Verification Self-Check Script",
            "description": "scripts/verify_loop.sh runs 7 checks after any verification. Catches process violations that individual checks miss.",
            "usage": "bash scripts/verify_loop.sh <token> <project_id>",
            "checks": [
                "1. Node status — all qa_pass?",
                "2. Coverage — all changed files have graph nodes?",
                "3. Docs/Artifacts — nodes with @route have api_docs?",
                "4. Memory — code changes have corresponding dbservice entries? (L9.8)",
                "5. Docs update — API nodes have documentation sections?",
                "6. Gatekeeper — release-gate passes?",
            ],
            "memory_check_rule": "If >5 code files changed but <5 memories → FAIL. If >10 changed but <10 memories → WARN. Forces developers to document decisions and pitfalls.",
        },
        "scheduled_task_management": {
            "title": "L9.9 Scheduled Task Management",
            "description": "Task prompt templates reside in scripts/task-templates/, tracked by git and protected by coverage-check.",
            "template_location": "scripts/task-templates/telegram-handler.md",
            "variables": "{PROJECT_ID}, {TOKEN}, {CHAT_ID}, {STREAM}, {GROUP}, {BASE}",
            "key_fix": "Messages must be consumed with XREADGROUP + XACK confirmation; XRANGE cannot be used (does not track consumption progress).",
        },
        "human_intervention": {
            "title": "Human Intervention Flow",
            "guide": "docs/human-intervention-guide.md",
            "boundaries": {
                "fully_automated": ["Code testing", "verify-update", "coverage-check", "Memory writes", "Message replies (non-sensitive)"],
                "needs_human_confirm": ["New node creation", "Baseline batch changes", "Cross-project operations"],
                "must_be_human": ["Token management", "Release confirmation", "rollback", "delete", "Scheduled task authorization"],
                "human_verification": ["Telegram interaction behavior", "UI changes", "Security features"],
            },
            "trigger_keywords": ["urgent", "urgent", "manual", "manual", "rollback", "delete", "release", "deploy"],
            "verification_flow": "AI notifies human → human tests → replies 'acceptance pass/fail' → AI submits verify-update",
        },
    },
    "token_model": {
        "title": "Token Model (v5 simplified)",
        "description": "Token simplified for message-driven mode: project_token never expires, Gateway proxies auth. Removed refresh/access dual-token design.",
        "tokens": {
            "project_token (gov-xxx)": {
                "holder": "Gateway / Human",
                "ttl": "non-expiring",
                "scope": "Full project API access (coordinator level)",
                "obtain": "POST /api/init {project_id, password}",
            },
            "agent_token (gov-xxx)": {
                "holder": "dev/tester/qa processes",
                "ttl": "24h",
                "scope": "Restricted API (verify-update, heartbeat, and other role operations)",
                "obtain": "POST /api/role/assign (coordinator assigns)",
            },
        },
        "api": {
            "POST /api/init": "Create project and obtain project_token",
            "POST /api/token/revoke": "Manually revoke project_token (requires password)",
            "POST /api/role/assign": "coordinator assigns agent_token",
        },
        "deprecated": [
            "POST /api/token/refresh — No longer needed; project_token never expires [deprecated: v5, removal: v8]",
            "POST /api/token/rotate — Simplified to revoke + re-init [deprecated: v5, removal: v8]",
            "access_token (gat-*) — no longer in use",
        ],
        "security": [
            "init password protection (reset token requires password)",
            "revoke capability retained (manually revocable)",
            "Network isolation (token only within localhost / Docker internal network)",
            "Gateway proxies auth (CLI session does not hold token directly)",
            "agent_token still has 24h TTL (independent process permissions are time-limited)",
        ],
    },
    "agent_lifecycle": {
        "title": "Agent Lifecycle (lease management)",
        "description": "Register/heartbeat/deregister agents with lease-based lifecycle. Orphan detection for stale agents.",
        "api": {
            "POST /api/agent/register": {
                "description": "Register an agent, get a lease.",
                "headers": {"X-Gov-Token": "required"},
                "body": {"project_id": "amingClaw", "expected_duration_sec": 3600},
                "returns": {"lease_id": "lease-xxx", "heartbeat_interval_sec": 120, "lease_ttl_sec": 600},
            },
            "POST /api/agent/heartbeat": {
                "description": "Renew lease. Call every 2 minutes.",
                "body": {"lease_id": "lease-xxx", "status": "idle|busy|processing", "worker_pid": 12345},
                "returns": {"ok": True, "lease_renewed_until": "..."},
            },
            "POST /api/agent/deregister": {
                "description": "Release lease on exit.",
                "body": {"lease_id": "lease-xxx"},
            },
            "GET /api/agent/orphans": {
                "description": "List agents with expired leases.",
                "query": "project_id=amingClaw (optional)",
                "returns": {"orphans": [{"session_id": "...", "principal_id": "...", "worker_pid": 12345, "reason": "no_active_lease"}]},
            },
            "POST /api/agent/cleanup": {
                "description": "Coordinator cleans up orphaned agents.",
                "headers": {"X-Gov-Token": "coordinator token"},
                "body": {"project_id": "amingClaw"},
            },
        },
        "lease_mechanism": "Agent registers → gets lease (5min TTL in Redis). Heartbeat every 2min renews. No heartbeat for 5min → lease expires → agent marked orphan. Gateway checks lease before routing messages.",
    },
    "session_context": {
        "title": "Session Context (cross-session state)",
        "description": "Persist coordinator working state across sessions. Snapshot + append log with optimistic locking.",
        "api": {
            "POST /api/context/{project_id}/save": {
                "description": "Save session context snapshot.",
                "body": {
                    "context": {"current_focus": "...", "active_nodes": ["L1.3"], "pending_tasks": ["..."], "chat_id": 123, "recent_messages": []},
                    "expected_version": 5,
                },
                "returns": {"ok": True, "version": 6},
                "note": "expected_version enables optimistic locking. Omit for unconditional save.",
            },
            "GET /api/context/{project_id}/load": {
                "description": "Load latest session context.",
                "returns": {"context": {"...": "..."}, "exists": True},
            },
            "POST /api/context/{project_id}/log": {
                "description": "Append entry to session log.",
                "body": {"type": "decision|msg_in|msg_out|action", "content": {"text": "..."}},
            },
            "GET /api/context/{project_id}/log": {
                "description": "Read session log entries.",
                "query": "limit=50",
            },
            "POST /api/context/{project_id}/assemble": {
                "description": "Assemble task-aware context from dbservice memory.",
                "body": {"task_type": "dev_general|telegram_handler|verify_node|code_review|release_check", "token_budget": 5000},
            },
            "POST /api/context/{project_id}/archive": {
                "description": "Archive valuable content to long-term memory, clear expired context.",
            },
        },
        "storage": "Redis (24h TTL) + SQLite (durable fallback). Auto-archived by OutboxWorker after 24h inactivity.",
    },
    "task_registry": {
        "title": "Task Registry (task management)",
        "description": "SQLite-backed task lifecycle with dual-field status: execution_status (queued/claimed/running/succeeded/failed/cancelled/timed_out) + notification_status (none/pending/notified).",
        "api": {
            "POST /api/task/{project_id}/create": {
                "description": "Create a new task. DB is source of truth, task file is secondary.",
                "headers": {"X-Gov-Token": "required"},
                "body": {"prompt": "...", "type": "task", "related_nodes": ["L1.3"], "priority": 1, "max_attempts": 3},
                "returns": {"task_id": "task-xxx", "status": "created"},
            },
            "POST /api/task/{project_id}/claim": {
                "description": "Claim next available task (FIFO by priority). Sets worker_id and lease_expires_at.",
                "body": {"task_id": "task-xxx", "worker_id": "executor-hostname"},
                "returns": {"task": {"task_id": "...", "prompt": "...", "attempt_num": 1}},
            },
            "POST /api/task/{project_id}/complete": {
                "description": "Mark task completed. Sets execution_status and notification_status=pending.",
                "body": {"task_id": "task-xxx", "execution_status": "succeeded|failed", "error_message": ""},
                "note": "Failed tasks auto-retry if attempt_count < max_attempts.",
            },
            "POST /api/task/{project_id}/notify": {
                "description": "Mark task as notified (user has been informed).",
                "body": {"task_id": "task-xxx"},
            },
            "GET /api/task/{project_id}/list": {
                "description": "List tasks.",
                "query": "status=running&limit=50",
            },
        },
    },
    "executor": {
        "title": "Executor (host machine task executor)",
        "description": "Persistent process monitors the pending/ directory, claims and executes Claude/Codex CLI tasks. Integrates Task Registry + Redis notifications.",
        "flow": {
            "1_pick": "scan pending/*.json (skip .tmp.json) → oldest first",
            "2_claim": "move to processing/ + Task Registry claim (DB insert queued→claimed→running)",
            "3_execute": "run_claude / run_codex / run_pipeline",
            "4_complete": "Task Registry complete (succeeded/failed) + Redis publish task:completed",
            "5_notify": "Gateway polls pending notifications → sends Telegram",
        },
        "features": {
            "atomic_write": "Gateway writes .tmp.json → fsync → rename to .json",
            "startup_recovery": "Scans processing/ for stale tasks (>5min), re-queues them",
            "heartbeat": "Background thread updates heartbeat_at every 30s",
            "tool_policy": "Commands checked against auto_allow/needs_approval/always_deny lists",
        },
    },
    "tool_policy": {
        "title": "Tool Policy (command security policy)",
        "description": "Executor checks security policy before executing commands. Three-tier classification.",
        "levels": {
            "auto_allow": "git diff, pytest, npm test, and other read-only/test commands → auto-execute",
            "needs_approval": "git push, docker compose down, npm publish → requires human confirmation",
            "always_deny": "rm -rf /, shutdown, reboot → always denied",
        },
        "note": "Currently string-matching; will be upgraded to a structured command capability model.",
    },
    "deployment": {
        "title": "Deployment (deployment workflow)",
        "description": "Automated detection and deployment workflow for switching from development to production.",
        "scripts": {
            "scripts/startup.sh": "One-click start of all services (Docker + domain pack + executor)",
            "scripts/pre-deploy-check.sh": "Pre-deploy checks (node status/coverage/docs/memory/gatekeeper/staging/config/gateway)",
            "deploy-governance.sh": "Zero-downtime deployment (auto-calls pre-deploy-check → build → staging verify → swap)",
        },
        "checks": {
            "node_status": "All nodes qa_pass",
            "coverage": "All changed files have corresponding nodes",
            "docs": "API docs >= 10 sections",
            "memory": "dbservice memories >= 5 entries",
            "gatekeeper": "release-gate PASS",
            "config_consistency": "dev/prod environment variables consistent",
            "staging": "staging container health + smoke test",
            "gateway_channel": "Telegram message channel reachable",
        },
        "usage": "GOV_COORDINATOR_TOKEN=gov-xxx ./deploy-governance.sh",
    },
    "executor_api": {
        "title": "Executor API (session intervention interface)",
        "description": "Host machine Executor embeds an HTTP API (:40100). Claude Code sessions can directly monitor, intervene, and debug via curl.",
        "port": 40100,
        "endpoints": {
            "monitoring": {
                "GET /health": "API health check",
                "GET /status": "Overall status (pending/processing/active sessions)",
                "GET /sessions": "Active AI process list",
                "GET /tasks": "Task list (supports project_id, status filtering)",
                "GET /task/{id}": "Single task details (including evidence, validator logs)",
                "GET /trace/{id}": "Trace details",
            },
            "intervention": {
                "POST /task/{id}/pause": "Pause a running task",
                "POST /task/{id}/cancel": "Cancel a task (terminate AI process)",
                "POST /task/{id}/retry": "Retry a failed task (move back to pending)",
                "POST /cleanup-orphans": "Clean up zombie processes and stuck tasks",
            },
            "direct_chat": {
                "POST /coordinator/chat": "Directly launch a Coordinator session (bypasses Telegram)",
                "body": {"message": "...", "project_id": "amingClaw", "chat_id": 0},
                "note": "Synchronously waits for AI to complete before returning, maximum 120s",
            },
            "debugging": {
                "GET /validator/last-result": "Most recent validation result (tier/pass/reject details)",
                "GET /context/{project_id}": "Current assembled context result",
                "GET /ai-session/{id}/output": "Raw AI output (stdout/stderr/exit_code)",
            },
        },
        "access": "Only accessible from host machine localhost:40100, does not go through nginx, no token required",
        "guide": "See docs/executor-api-guide.md for details",
    },
}


# ---------------------------------------------------------------------------
# Baseline Endpoints (Phase I)
# ---------------------------------------------------------------------------

@route("GET", "/api/baseline/{project_id}/list")
def handle_baseline_list(ctx: RequestContext):
    """List all baselines for a project."""
    pid = ctx.path_params["project_id"]
    conn = get_connection(pid)
    try:
        from . import baseline_service
        baselines = baseline_service.list_baselines(conn, pid)
        return {"ok": True, "baselines": baselines}
    finally:
        conn.close()


@route("GET", "/api/baseline/{project_id}/latest")
def handle_baseline_latest(ctx: RequestContext):
    """Get the latest baseline for a project."""
    pid = ctx.path_params["project_id"]
    conn = get_connection(pid)
    try:
        from . import baseline_service
        baselines = baseline_service.list_baselines(conn, pid)
        if not baselines:
            return ctx.handler._respond(404, {"error": "baseline_missing", "message": "No baselines found"})
        return {"ok": True, "baseline": baselines[0]}
    finally:
        conn.close()


@route("GET", "/api/baseline/{project_id}/{baseline_id}")
def handle_baseline_get(ctx: RequestContext):
    """Get a single baseline by ID."""
    pid = ctx.path_params["project_id"]
    baseline_id = int(ctx.path_params["baseline_id"])
    conn = get_connection(pid)
    try:
        from . import baseline_service
        bl = baseline_service.get_baseline(conn, pid, baseline_id)
        return {"ok": True, "baseline": bl}
    except baseline_service.BaselineMissingError as e:
        return ctx.handler._respond(404, e.to_dict())
    finally:
        conn.close()


@route("GET", "/api/baseline/{project_id}/by-commit/{sha}")
def handle_baseline_by_commit(ctx: RequestContext):
    """Get baseline by commit SHA."""
    pid = ctx.path_params["project_id"]
    sha = ctx.path_params["sha"]
    conn = get_connection(pid)
    try:
        from . import baseline_service
        bl = baseline_service.get_by_commit(conn, pid, sha)
        return {"ok": True, "baseline": bl}
    except baseline_service.BaselineMissingError as e:
        return ctx.handler._respond(404, e.to_dict())
    finally:
        conn.close()


@route("POST", "/api/baseline/{project_id}/diff")
def handle_baseline_diff(ctx: RequestContext):
    """Diff two baselines. Body: {from, to, scope}."""
    pid = ctx.path_params["project_id"]
    body = ctx.body
    from_id = body.get("from")
    to_id = body.get("to")
    scope = body.get("scope", "full")
    if from_id is None or to_id is None:
        return ctx.handler._respond(400, {"error": "invalid_request", "message": "'from' and 'to' are required"})
    conn = get_connection(pid)
    try:
        from . import baseline_service
        delta = baseline_service.diff(conn, pid, int(from_id), int(to_id), scope)
        return {"ok": True, "delta": delta}
    except baseline_service.BaselineMissingError as e:
        return ctx.handler._respond(404, e.to_dict())
    finally:
        conn.close()


@route("POST", "/api/baseline/{project_id}/create")
def handle_baseline_create(ctx: RequestContext):
    """Create a new baseline. R7: trigger allowlist enforcement."""
    pid = ctx.path_params["project_id"]
    body = ctx.body
    triggered_by = body.get("triggered_by", "")
    from . import baseline_service
    if triggered_by not in baseline_service.TRIGGER_ALLOWLIST:
        return ctx.handler._respond(400, {
            "error": "invalid_request",
            "message": f"triggered_by must be one of {sorted(baseline_service.TRIGGER_ALLOWLIST)}, got {triggered_by!r}"
        })
    conn = get_connection(pid)
    try:
        bl = baseline_service.create_baseline(
            conn, pid,
            chain_version=body.get("chain_version", ""),
            trigger=body.get("trigger", triggered_by),
            triggered_by=triggered_by,
            graph_json=body.get("graph_json", {}),
            code_doc_map_json=body.get("code_doc_map_json", {}),
            node_state_snap=body.get("node_state_snap", "{}"),
            chain_event_max=body.get("chain_event_max", 0),
            notes=body.get("notes", ""),
            reconstructed=body.get("reconstructed", 0),
        )
        return {"ok": True, "baseline": bl}
    except ValueError as e:
        return ctx.handler._respond(400, {"error": "invalid_request", "message": str(e)})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Baseline GC Endpoint
# ---------------------------------------------------------------------------

@route("POST", "/api/baseline/{project_id}/gc")
def handle_baseline_gc(ctx: RequestContext):
    """Run baseline garbage collection (coordinator-only). R8."""
    pid = ctx.path_params["project_id"]
    conn = get_connection(pid)
    try:
        session = ctx.require_auth(conn)
        if session.get("role") != "coordinator":
            from .errors import PermissionDeniedError
            raise PermissionDeniedError(
                session.get("role", ""), "baseline.gc",
                {"detail": "Only coordinator can run baseline GC"})
    finally:
        conn.close()

    body = ctx.body
    dry_run = body.get("dry_run", True)
    keep_last_n = body.get("keep_last_n", 100)

    from . import baseline_gc
    result = baseline_gc.gc_baselines(pid, dry_run=dry_run, keep_last_n=keep_last_n)
    return {"ok": True, **result}


# ---------------------------------------------------------------------------
# Backlog Endpoints (OPT-DB-BACKLOG)
# ---------------------------------------------------------------------------

_BACKLOG_CLOSED_STATUSES = {
    "FIXED",
    "CLOSED",
    "DONE",
    "RESOLVED",
    "CANCELLED",
    "MERGED",
    "SUPERSEDED",
    "VOID",
}
_BACKLOG_DEFAULT_LIST_LIMIT = 100
_BACKLOG_HARD_LIST_LIMIT = 200
_BACKLOG_COMPACT_PREVIEW_CHARS = 280


def _first_query_value(query: dict, key: str, default: str = "") -> str:
    value = query.get(key, default)
    if isinstance(value, list):
        value = value[0] if value else default
    return str(value or "")


def _query_has_key(query: dict, key: str) -> bool:
    return key in query and query.get(key) is not None


def _json_list_field(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
            if parsed in (None, ""):
                return []
            return [parsed]
        except (json.JSONDecodeError, TypeError):
            return [value] if value.strip() else []
    return [value]


def _string_list_field(value: Any, *, limit: int | None = None) -> list[str]:
    values = [str(item) for item in _json_list_field(value) if str(item).strip()]
    return values[:limit] if limit is not None else values


def _compact_preview(value: Any, limit: int = _BACKLOG_COMPACT_PREVIEW_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _backlog_full_bug(row: sqlite3.Row | dict) -> dict:
    bug = dict(row)
    for key in ("required_docs", "provenance_paths"):
        try:
            bug[key] = json.loads(bug.get(key, "[]"))
        except (json.JSONDecodeError, TypeError):
            bug[key] = []
    bug["bypass_policy"] = backlog_runtime.parse_json_object(bug.get("bypass_policy_json", "{}"))
    bug["takeover"] = backlog_runtime.parse_json_object(bug.get("takeover_json", "{}"))
    bug["contract_summary"] = _backlog_contract_summary(bug.get("chain_trigger_json"))
    return bug


def _backlog_compact_bug(row: sqlite3.Row | dict) -> dict:
    raw = dict(row)
    target_files = _string_list_field(raw.get("target_files"), limit=3)
    test_files = _string_list_field(raw.get("test_files"), limit=3)
    acceptance = _string_list_field(raw.get("acceptance_criteria"), limit=2)
    required_docs = _string_list_field(raw.get("required_docs"), limit=3)
    provenance_paths = _string_list_field(raw.get("provenance_paths"), limit=3)
    return {
        "bug_id": raw.get("bug_id", ""),
        "title": raw.get("title", ""),
        "status": raw.get("status", ""),
        "priority": raw.get("priority", ""),
        "target_files": target_files,
        "test_files": test_files,
        "acceptance_criteria": acceptance,
        "details_md": _compact_preview(raw.get("details_md", "")),
        "details_preview": _compact_preview(raw.get("details_md", "")),
        "commit": raw.get("commit", ""),
        "created_at": raw.get("created_at", ""),
        "updated_at": raw.get("updated_at", ""),
        "fixed_at": raw.get("fixed_at", ""),
        "chain_task_id": raw.get("chain_task_id", ""),
        "chain_stage": raw.get("chain_stage", ""),
        "runtime_state": raw.get("runtime_state", ""),
        "current_task_id": raw.get("current_task_id", ""),
        "root_task_id": raw.get("root_task_id", ""),
        "worktree_path": raw.get("worktree_path", ""),
        "worktree_branch": raw.get("worktree_branch", ""),
        "mf_type": raw.get("mf_type", ""),
        "target_file_count": len(_json_list_field(raw.get("target_files"))),
        "test_file_count": len(_json_list_field(raw.get("test_files"))),
        "acceptance_count": len(_json_list_field(raw.get("acceptance_criteria"))),
        "required_doc_count": len(_json_list_field(raw.get("required_docs"))),
        "provenance_count": len(_json_list_field(raw.get("provenance_paths"))),
        "required_docs": required_docs,
        "provenance_paths": provenance_paths,
        "contract_summary": _backlog_contract_summary(raw.get("chain_trigger_json")),
        "compact": True,
    }


def _backlog_contract_summary(value: Any) -> dict:
    contract = backlog_runtime.parse_json_object(value)
    root = contract.get("parallel_contract") if isinstance(contract.get("parallel_contract"), dict) else contract
    if not isinstance(root, dict) or not root:
        return {
            "has_contract": False,
            "template_id": "",
            "contract_instance_id": "",
            "required_evidence_count": 0,
            "optional_evidence_count": 0,
        }
    try:
        from . import task_timeline

        requirements = task_timeline.mf_contract_requirements(contract)
    except Exception:
        requirements = []
    required = [item for item in requirements if item.get("required", True)]
    optional = [item for item in requirements if not item.get("required", True)]
    return {
        "has_contract": bool(requirements or root.get("template_id") or root.get("contract_instance_id")),
        "template_id": str(root.get("template_id") or ""),
        "contract_instance_id": str(root.get("contract_instance_id") or ""),
        "required_evidence_count": len(required),
        "optional_evidence_count": len(optional),
    }


def _backlog_summary(conn) -> dict:
    by_status = {
        str(row["status"] or "OPEN"): int(row["count"] or 0)
        for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM backlog_bugs GROUP BY status"
        ).fetchall()
    }
    by_priority = {
        str(row["priority"] or "P3"): int(row["count"] or 0)
        for row in conn.execute(
            "SELECT priority, COUNT(*) AS count FROM backlog_bugs GROUP BY priority"
        ).fetchall()
    }
    open_count = sum(
        count for status, count in by_status.items()
        if status.upper() not in _BACKLOG_CLOSED_STATUSES
    )
    urgent_open_count = int(conn.execute(
        "SELECT COUNT(*) AS count FROM backlog_bugs "
        "WHERE UPPER(status) NOT IN (%s) AND priority IN ('P0', 'P1')"
        % ",".join("?" for _ in _BACKLOG_CLOSED_STATUSES),
        [status for status in _BACKLOG_CLOSED_STATUSES],
    ).fetchone()["count"] or 0)
    return {
        "total": sum(by_status.values()),
        "open": open_count,
        "fixed": by_status.get("FIXED", 0),
        "urgent_open": urgent_open_count,
        "by_status": by_status,
        "by_priority": by_priority,
    }


def _append_backlog_filters(sql: str, params: list[Any], ctx: RequestContext) -> tuple[str, list[Any]]:
    status_filter = _first_query_value(ctx.query, "status")
    priority_filter = _first_query_value(ctx.query, "priority")
    search = _first_query_value(ctx.query, "q").strip()
    if status_filter:
        sql += " AND status = ?"
        params.append(status_filter)
    if priority_filter:
        sql += " AND priority = ?"
        params.append(priority_filter)
    if _query_has_key(ctx.query, "include_closed") and not _query_bool(ctx.query, "include_closed", True):
        placeholders = ",".join("?" for _ in _BACKLOG_CLOSED_STATUSES)
        sql += f" AND UPPER(status) NOT IN ({placeholders})"
        params.extend(_BACKLOG_CLOSED_STATUSES)
    if search:
        needle = f"%{search.lower()}%"
        columns = (
            "bug_id",
            "title",
            "status",
            "priority",
            "details_md",
            "target_files",
            "test_files",
            "acceptance_criteria",
            "required_docs",
            "provenance_paths",
        )
        sql += " AND (" + " OR ".join(f"LOWER({col}) LIKE ?" for col in columns) + ")"
        params.extend([needle] * len(columns))
    return sql, params


@route("GET", "/api/backlog/{project_id}")
def handle_backlog_list(ctx: RequestContext):
    """List backlog bugs.

    Legacy no-query calls still return all full rows. Supplying view, limit,
    offset, q, or include_closed enables the optimized list path with compact
    rows and pagination metadata.
    """
    pid = ctx.path_params["project_id"]
    query = ctx.query or {}
    optimized = any(
        _query_has_key(query, key)
        for key in ("view", "limit", "offset", "q", "include_closed")
    )
    view = _first_query_value(query, "view", "full").strip().lower() or "full"
    if view not in {"full", "compact"}:
        raise GovernanceError("invalid_backlog_view", "view must be 'full' or 'compact'", 400)
    raw_limit = _query_int(query, "limit", _BACKLOG_DEFAULT_LIST_LIMIT)
    limit = max(1, min(raw_limit, _BACKLOG_HARD_LIST_LIMIT)) if optimized else None
    offset = max(0, _query_int(query, "offset", 0)) if optimized else 0
    conn = get_connection(pid)
    try:
        sql = "SELECT * FROM backlog_bugs WHERE 1=1"
        params: list[Any] = []
        sql, params = _append_backlog_filters(sql, params, ctx)
        count_sql = sql.replace("SELECT *", "SELECT COUNT(*) AS count", 1)
        filtered_count = int(conn.execute(count_sql, params).fetchone()["count"] or 0)
        sql += " ORDER BY created_at DESC"
        page_params = list(params)
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            page_params.extend([limit, offset])
        rows = conn.execute(sql, page_params).fetchall()
        bugs = [
            _backlog_compact_bug(r) if view == "compact" else _backlog_full_bug(r)
            for r in rows
        ]
        total_count = int(conn.execute("SELECT COUNT(*) AS count FROM backlog_bugs").fetchone()["count"] or 0)
        next_offset = offset + len(bugs)
        has_more = limit is not None and next_offset < filtered_count
        result = {
            "bugs": bugs,
            "count": len(bugs),
            "total_count": total_count,
            "filtered_count": filtered_count,
            "view": view,
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
            "next_offset": next_offset if has_more else None,
            "truncated": has_more,
            "summary": _backlog_summary(conn),
        }
        return result
    finally:
        conn.close()


@route("GET", "/api/backlog/{project_id}/portable/export")
def handle_backlog_portable_export(ctx: RequestContext):
    """Export backlog rows as a portable JSON payload."""
    pid = ctx.path_params["project_id"]
    status_filter = str(ctx.query.get("status") or "")
    priority_filter = str(ctx.query.get("priority") or "")
    bug_ids = _query_statuses(ctx.query, "bug_id")
    conn = get_connection(pid)
    try:
        from . import backlog_portable
        return backlog_portable.export_backlog_portable(
            conn,
            pid,
            status=status_filter,
            priority=priority_filter,
            bug_ids=bug_ids,
        )
    finally:
        conn.close()


@route("POST", "/api/backlog/{project_id}/portable/import")
def handle_backlog_portable_import(ctx: RequestContext):
    """Import portable backlog rows into the local project DB."""
    pid = ctx.path_params["project_id"]
    body = ctx.body
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else body
    on_conflict = str(body.get("on_conflict") or "skip")
    dry_run = _query_bool(ctx.query, "dry_run", bool(body.get("dry_run", False)))
    actor = str(body.get("actor") or "api")
    conn = get_connection(pid)
    try:
        from . import backlog_portable
        try:
            result = backlog_portable.import_backlog_portable(
                conn,
                pid,
                payload,
                on_conflict=on_conflict,
                dry_run=dry_run,
                actor=actor,
            )
        except ValueError as exc:
            raise GovernanceError("invalid_backlog_import", str(exc), 400) from exc
        if not result.get("ok"):
            return 409, result
        return {"ok": True, **result}
    finally:
        conn.close()


@route("GET", "/api/backlog/{project_id}/{bug_id}")
def handle_backlog_get(ctx: RequestContext):
    """Get a single backlog bug by ID. Returns 404 if missing."""
    pid = ctx.path_params["project_id"]
    bug_id = ctx.path_params["bug_id"]
    conn = get_connection(pid)
    try:
        row = conn.execute(
            "SELECT * FROM backlog_bugs WHERE bug_id = ?", (bug_id,)
        ).fetchone()
        if not row:
            raise GovernanceError("not_found", f"Bug {bug_id} not found", 404)
        result = dict(row)
        # Parse required_docs from JSON string to list
        try:
            result["required_docs"] = json.loads(result.get("required_docs", "[]"))
        except (json.JSONDecodeError, TypeError):
            result["required_docs"] = []
        # Parse provenance_paths from JSON string to list
        try:
            result["provenance_paths"] = json.loads(result.get("provenance_paths", "[]"))
        except (json.JSONDecodeError, TypeError):
            result["provenance_paths"] = []
        result["bypass_policy"] = backlog_runtime.parse_json_object(result.get("bypass_policy_json", "{}"))
        result["takeover"] = backlog_runtime.parse_json_object(result.get("takeover_json", "{}"))
        return result
    finally:
        conn.close()


@route("GET", "/api/backlog/{project_id}/{bug_id}/timeline-gate")
def handle_backlog_timeline_gate(ctx: RequestContext):
    """Read-only MF close-gate precheck for backlog timeline evidence."""
    pid = ctx.path_params["project_id"]
    bug_id = ctx.path_params["bug_id"]
    include_events = _query_bool(ctx.query, "include_events", False)
    limit = max(1, min(_query_int(ctx.query, "limit", 1000), 1000))
    conn = get_connection(pid)
    try:
        row = conn.execute(
            "SELECT * FROM backlog_bugs WHERE bug_id = ?", (bug_id,)
        ).fetchone()
        if not row:
            raise GovernanceError("not_found", f"Bug {bug_id} not found", 404)

        applicable = _mf_close_timeline_applicability(row)
        from . import task_timeline

        events = task_timeline.list_events(conn, pid, backlog_id=bug_id, limit=limit)
        if applicable["is_mf"]:
            contract = backlog_runtime.parse_json_object(_row_get(row, "chain_trigger_json", "{}"))
            verification = task_timeline.mf_close_gate_verification(events, contract=contract)
        else:
            verification = {
                "schema_version": "mf_close_timeline_gate.v1",
                "passed": True,
                "status": "not_applicable",
                "required_event_kinds": sorted(task_timeline.MF_CLOSE_REQUIRED_EVENT_KINDS),
                "present_event_kinds": [],
                "missing_event_kinds": [],
                "event_count": len(events),
                "ignored_required_events": [],
                "checks": {
                    "has_implementation": True,
                    "has_verification": True,
                    "has_close_ready": True,
                },
            }
        result = {
            "ok": True,
            "project_id": pid,
            "bug_id": bug_id,
            "applicable": applicable["is_mf"],
            "reason": applicable["reason"],
            "can_close": bool(verification.get("passed")),
            "timeline_gate": verification,
            "event_count": len(events),
        }
        reason_human = _MF_TIMELINE_REASON_HUMAN.get(applicable["reason"])
        if not reason_human:
            reason_parts = {part.strip() for part in applicable["reason"].split(",")}
            for reason_code, human_text in _MF_TIMELINE_REASON_HUMAN.items():
                if reason_code in reason_parts:
                    reason_human = human_text
                    break
        if reason_human:
            result["reason_human"] = reason_human
        if include_events:
            result["events"] = events
        return result
    finally:
        conn.close()


@route("POST", "/api/backlog/{project_id}/{bug_id}")
def handle_backlog_upsert(ctx: RequestContext):
    """Upsert a backlog bug (ON CONFLICT DO UPDATE)."""
    pid = ctx.path_params["project_id"]
    bug_id = ctx.path_params["bug_id"]
    body = ctx.body
    now = _utc_now()
    conn = get_connection(pid)
    try:
        existing_row = conn.execute(
            "SELECT * FROM backlog_bugs WHERE bug_id = ?", (bug_id,)
        ).fetchone()
        # --- AI triage gate (R2: before INSERT, skip if force_admit) ---
        decision = None
        if existing_row is None and not body.get("force_admit"):
            try:
                from .backlog_triage import triage_backlog_insert
                explicit_triage_action = str(body.get("triage_action") or "").strip()
                explicit_target_id = str(body.get("triage_target_bug_id") or "").strip()
                open_rows = conn.execute(
                    "SELECT bug_id, title, target_files FROM backlog_bugs WHERE status='OPEN'"
                ).fetchall()
                open_rows = [dict(r) for r in open_rows]
                decision = triage_backlog_insert(body | {"bug_id": bug_id}, open_rows)
                action = decision.get("action", "admit")
                try:
                    audit_service.record(conn, pid, "backlog_triage", actor="ai_triage",
                                         bug_id=bug_id, details=json.dumps(decision))
                    conn.commit()
                except Exception:
                    pass
                if explicit_triage_action:
                    allowed = {"admit", "merge_into", "supersede", "reject_dup"}
                    if explicit_triage_action not in allowed:
                        return 400, {
                            "ok": False,
                            "error": "invalid_triage_action",
                            "allowed_actions": sorted(allowed),
                            "triage": decision,
                        }
                    if explicit_triage_action == "admit":
                        decision = {"action": "admit", "reason": "observer admitted", "related_bug_ids": [], "confidence": 1.0}
                        action = "admit"
                    elif explicit_triage_action in {"merge_into", "supersede", "reject_dup"}:
                        related = list(decision.get("related_bug_ids") or [])
                        target_id = explicit_target_id or (related[0] if related else "")
                        if not target_id or target_id not in related:
                            return 409, {
                                "ok": False,
                                "error": "triage_target_not_candidate",
                                "triage": decision,
                                "triage_action": explicit_triage_action,
                                "triage_target_bug_id": target_id,
                            }
                        if explicit_triage_action == "merge_into":
                            conn.execute(
                                "UPDATE backlog_bugs SET details_md = details_md || ? , updated_at = ? WHERE bug_id = ?",
                                ("\n\n---\nMerged from %s: %s" % (bug_id, body.get("details_md", "")), now, target_id))
                            try:
                                audit_service.record(
                                    conn, pid, "backlog_triage_decision",
                                    actor=body.get("actor", "observer"),
                                    bug_id=bug_id,
                                    action="merge_into",
                                    target_bug_id=target_id,
                                    details=json.dumps(decision),
                                )
                            except Exception:
                                pass
                            conn.commit()
                            return {"ok": True, "bug_id": target_id, "action": "merge_into", "merged_from": bug_id, "triage": decision}
                        if explicit_triage_action == "reject_dup":
                            try:
                                audit_service.record(
                                    conn, pid, "backlog_triage_decision",
                                    actor=body.get("actor", "observer"),
                                    bug_id=bug_id,
                                    action="reject_dup",
                                    target_bug_id=target_id,
                                    details=json.dumps(decision),
                                )
                            except Exception:
                                pass
                            conn.commit()
                            return {"ok": True, "bug_id": bug_id, "action": "reject_dup", "duplicate_of": target_id, "triage": decision}
                        decision = dict(decision)
                        decision["action"] = "supersede"
                        decision["related_bug_ids"] = [target_id]
                        action = "supersede"
                if action == "reject_dup":
                    return 409, {"ok": False, "error": "duplicate", "duplicate_of": decision["related_bug_ids"],
                                 "reason": decision["reason"], "triage": decision}
                if action == "supersede":
                    if explicit_triage_action != "supersede":
                        return 409, {
                            "ok": False,
                            "error": "triage_review_required",
                            "recommended_action": "supersede",
                            "supported_actions": ["admit", "supersede", "reject_dup"],
                            "triage": decision,
                        }
                if action == "merge_into" and decision["related_bug_ids"]:
                    return 409, {
                        "ok": False,
                        "error": "triage_review_required",
                        "recommended_action": "merge_into",
                        "supported_actions": ["admit", "merge_into", "reject_dup"],
                        "triage": decision,
                    }
            except Exception:
                try:
                    audit_service.record(conn, pid, "backlog_triage_failed", actor="ai_triage", bug_id=bug_id)
                    conn.commit()
                except Exception:
                    pass
                decision = {"action": "admit", "reason": "agent failure", "related_bug_ids": [], "confidence": 0.0}

        def _value(key: str, default: Any = "") -> Any:
            if key in body:
                return body.get(key)
            if existing_row is not None:
                return existing_row[key]
            return default

        def _json_value(key: str, default: Any) -> str:
            if key in body:
                return json.dumps(body.get(key, default))
            if existing_row is not None:
                return str(existing_row[key] or json.dumps(default))
            return json.dumps(default)

        bypass_policy = backlog_runtime.parse_json_object(body.get("bypass_policy_json"))
        bypass_policy.update(backlog_runtime.parse_json_object(body.get("bypass_policy")))
        if body.get("mf_type"):
            bypass_policy["mf_type"] = backlog_runtime.normalize_mf_type(body.get("mf_type"), bypass_policy)
        elif bypass_policy.get("mf_type"):
            bypass_policy["mf_type"] = backlog_runtime.normalize_mf_type(bypass_policy.get("mf_type"), bypass_policy)
        bypass_policy_raw = backlog_runtime.policy_json(bypass_policy)
        mf_type_value = (
            backlog_runtime.normalize_mf_type(body.get("mf_type") or bypass_policy.get("mf_type"), bypass_policy)
            if body.get("mf_type") or bypass_policy.get("mf_type")
            else ""
        )
        takeover_raw = backlog_runtime.policy_json(backlog_runtime.parse_json_object(body.get("takeover_json")))
        conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, target_files, test_files,
                acceptance_criteria, chain_task_id, "commit", discovered_at,
                fixed_at, details_md, chain_trigger_json, required_docs,
                provenance_paths, bypass_policy_json, mf_type, takeover_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(bug_id) DO UPDATE SET
                 title = excluded.title,
                 status = excluded.status,
                 priority = excluded.priority,
                 target_files = excluded.target_files,
                 test_files = excluded.test_files,
                 acceptance_criteria = excluded.acceptance_criteria,
                 chain_task_id = excluded.chain_task_id,
                 "commit" = excluded."commit",
                 discovered_at = excluded.discovered_at,
                 fixed_at = excluded.fixed_at,
                 details_md = excluded.details_md,
                 chain_trigger_json = excluded.chain_trigger_json,
                 required_docs = excluded.required_docs,
                 provenance_paths = excluded.provenance_paths,
                 bypass_policy_json = CASE
                   WHEN excluded.bypass_policy_json != '{}' THEN excluded.bypass_policy_json
                   ELSE backlog_bugs.bypass_policy_json
                 END,
                 mf_type = CASE
                   WHEN excluded.mf_type != '' THEN excluded.mf_type
                   ELSE backlog_bugs.mf_type
                 END,
                 takeover_json = CASE
                   WHEN excluded.takeover_json != '{}' THEN excluded.takeover_json
                   ELSE backlog_bugs.takeover_json
                 END,
                 updated_at = excluded.updated_at
            """,
            (
                bug_id,
                _value("title", ""),
                _value("status", "OPEN"),
                _value("priority", "P3"),
                _json_value("target_files", []),
                _json_value("test_files", []),
                _json_value("acceptance_criteria", []),
                _value("chain_task_id", ""),
                _value("commit", ""),
                _value("discovered_at", ""),
                _value("fixed_at", ""),
                _value("details_md", ""),
                _json_value("chain_trigger_json", {}),
                _json_value("required_docs", []),
                _json_value("provenance_paths", []),
                bypass_policy_raw,
                mf_type_value,
                takeover_raw,
                now,
                now,
            ),
        )
        conn.commit()
        # Audit: backlog_upsert event
        try:
            audit_service.record(
                conn, pid, "backlog_upsert",
                actor=body.get("actor", "api"),
                bug_id=bug_id,
            )
            conn.commit()
        except Exception:
            pass  # best-effort audit
        # Supersede: close old rows after inserting new one
        if decision and decision.get("action") == "supersede":
            for old_id in decision.get("related_bug_ids", []):
                conn.execute("UPDATE backlog_bugs SET status='FIXED', updated_at=? WHERE bug_id=?", (now, old_id))
            try:
                audit_service.record(
                    conn, pid, "backlog_triage_decision",
                    actor=body.get("actor", "observer"),
                    bug_id=bug_id,
                    action="supersede",
                    target_bug_id=",".join(decision.get("related_bug_ids", [])),
                    details=json.dumps(decision),
                )
            except Exception:
                pass
            conn.commit()
            return {"ok": True, "bug_id": bug_id, "action": "superseded", "closed_bugs": decision["related_bug_ids"]}
        return {"ok": True, "bug_id": bug_id, "action": "upserted"}
    finally:
        conn.close()


@route("POST", "/api/backlog/{project_id}/{bug_id}/predeclare-mf")
def handle_backlog_predeclare_mf(ctx: RequestContext):
    """Predeclare a manual fix: transition OPEN -> MF_PLANNED with mf_id validation."""
    pid = ctx.path_params["project_id"]
    bug_id = ctx.path_params["bug_id"]
    body = ctx.body
    now = _utc_now()

    # Validate mf_id format
    mf_id = body.get("mf_id", "")
    if not re.match(r"^MF-\d{4}-\d{2}-\d{2}-\d{3}$", mf_id):
        raise GovernanceError(
            "invalid_mf_id",
            f"mf_id must match MF-YYYY-MM-DD-NNN, got: {mf_id}",
            422,
        )

    # Validate reason length
    reason = body.get("reason", "")
    if len(reason) < 20:
        raise GovernanceError(
            "reason_too_short",
            f"reason must be >= 20 chars, got {len(reason)}",
            422,
        )

    conn = get_connection(pid)
    try:
        row = conn.execute(
            "SELECT bug_id, status, details_md, current_task_id, root_task_id, runtime_state, "
            "bypass_policy_json, mf_type, takeover_json FROM backlog_bugs WHERE bug_id = ?",
            (bug_id,),
        ).fetchone()
        if not row:
            raise GovernanceError("not_found", f"Bug {bug_id} not found", 404)

        current_status = row["status"]
        if current_status != "OPEN":
            raise GovernanceError(
                "invalid_status",
                f"Bug must be OPEN to predeclare MF, currently: {current_status}",
                422,
            )

        # Store mf_id and reason in details_md for start-mf ownership check
        existing_md = row["details_md"] or ""
        marker = f"\n\n<!-- MF-PREDECLARE mf_id={mf_id} reason={reason} -->"
        new_details = existing_md + marker

        conn.execute(
            """UPDATE backlog_bugs
               SET status = 'MF_PLANNED',
                   details_md = ?,
                   updated_at = ?
               WHERE bug_id = ?""",
            (new_details, now, bug_id),
        )
        predeclare_policy = backlog_runtime.parse_json_object(body.get("bypass_policy"))
        mf_type = backlog_runtime.normalize_mf_type(body.get("mf_type"), predeclare_policy)
        predeclare_policy = backlog_runtime.build_mf_policy(
            mf_type,
            mf_id=mf_id,
            observer_authorized=bool(body.get("observer_authorized", True)),
            reason=reason,
            existing_policy=predeclare_policy,
        )
        predeclare_policy.update({
            "mf_id": mf_id,
            "observer_authorized": bool(body.get("observer_authorized", True)),
        })
        backlog_runtime.update_backlog_runtime(
            conn,
            bug_id,
            "manual_fix_planned",
            project_id=pid,
            metadata=predeclare_policy,
            runtime_state="manual_fix_planned",
            bypass_policy=predeclare_policy,
            mf_type=mf_type,
        )
        conn.commit()

        # Audit: best-effort
        try:
            audit_service.record(
                conn, pid, "backlog_predeclare_mf",
                actor=body.get("actor", "api"),
                bug_id=bug_id,
                mf_id=mf_id,
            )
            conn.commit()
        except Exception:
            pass

        return {
            "ok": True,
            "bug_id": bug_id,
            "status": "MF_PLANNED",
            "mf_id": mf_id,
            "mf_type": mf_type,
        }
    finally:
        conn.close()


@route("POST", "/api/backlog/{project_id}/{bug_id}/start-mf")
def handle_backlog_start_mf(ctx: RequestContext):
    """Start a manual fix: transition MF_PLANNED -> MF_IN_PROGRESS with mf_id ownership check."""
    pid = ctx.path_params["project_id"]
    bug_id = ctx.path_params["bug_id"]
    body = ctx.body
    now = _utc_now()

    mf_id = body.get("mf_id", "")

    conn = get_connection(pid)
    try:
        row = conn.execute(
            "SELECT bug_id, status, details_md FROM backlog_bugs WHERE bug_id = ?",
            (bug_id,),
        ).fetchone()
        if not row:
            raise GovernanceError("not_found", f"Bug {bug_id} not found", 404)

        current_status = row["status"]
        if current_status != "MF_PLANNED":
            raise GovernanceError(
                "invalid_status",
                f"Bug must be MF_PLANNED to start MF, currently: {current_status}",
                422,
            )

        # Verify mf_id ownership via substring check on details_md
        details_md = row["details_md"] or ""
        if mf_id not in details_md:
            raise GovernanceError(
                "mf_id_mismatch",
                f"mf_id {mf_id} not found in bug details; ownership check failed",
                422,
            )

        existing_policy = backlog_runtime.parse_json_object(_row_get(row, "bypass_policy_json", "{}"))
        start_policy = {**existing_policy, **backlog_runtime.parse_json_object(body.get("bypass_policy"))}
        requested_mf_type = body.get("mf_type") or _row_get(row, "mf_type", "") or start_policy.get("mf_type", "")
        if body.get("bypass_graph_governance") is True and not requested_mf_type:
            requested_mf_type = backlog_runtime.MF_TYPE_SYSTEM_RECOVERY
        mf_type = backlog_runtime.normalize_mf_type(requested_mf_type, start_policy)
        if mf_type == backlog_runtime.MF_TYPE_CHAIN_RESCUE and body.get("bypass_graph_governance") is True:
            raise GovernanceError(
                "invalid_mf_policy",
                "chain_rescue MF cannot bypass graph governance; use mf_type='system_recovery'",
                422,
            )
        start_policy = backlog_runtime.build_mf_policy(
            mf_type,
            mf_id=mf_id,
            observer_authorized=bool(body.get("observer_authorized", True)),
            reason=body.get("reason", ""),
            existing_policy=start_policy,
        )

        takeover = _apply_mf_takeover(conn, pid, bug_id, body, row, start_policy)

        conn.execute(
            """UPDATE backlog_bugs
               SET status = 'MF_IN_PROGRESS',
                   updated_at = ?
               WHERE bug_id = ?""",
            (now, bug_id),
        )
        backlog_runtime.update_backlog_runtime(
            conn,
            bug_id,
            "manual_fix_in_progress",
            project_id=pid,
            metadata=start_policy,
            runtime_state="manual_fix_in_progress",
            bypass_policy=start_policy,
            mf_type=mf_type,
            takeover=takeover,
        )
        conn.commit()

        # Audit: best-effort
        try:
            audit_service.record(
                conn, pid, "backlog_start_mf",
                actor=body.get("actor", "api"),
                bug_id=bug_id,
                mf_id=mf_id,
                mf_type=mf_type,
                takeover=json.dumps(takeover, ensure_ascii=False),
            )
            conn.commit()
        except Exception:
            pass

        return {
            "ok": True,
            "bug_id": bug_id,
            "status": "MF_IN_PROGRESS",
            "mf_id": mf_id,
            "mf_type": mf_type,
            "bypass_policy": start_policy,
            "takeover": takeover,
        }
    finally:
        conn.close()


@route("POST", "/api/backlog/{project_id}/{bug_id}/close")
def handle_backlog_close(ctx: RequestContext):
    """Close a backlog bug: set status=FIXED, commit, fixed_at."""
    pid = ctx.path_params["project_id"]
    bug_id = ctx.path_params["bug_id"]
    body = ctx.body
    now = _utc_now()
    conn = get_connection(pid)
    try:
        row = conn.execute(
            "SELECT bug_id, status, chain_stage, bypass_policy_json, mf_type, chain_trigger_json FROM backlog_bugs WHERE bug_id = ?",
            (bug_id,),
        ).fetchone()
        if not row:
            raise GovernanceError("not_found", f"Bug {bug_id} not found", 404)

        prior_status = row["status"]

        # Allow closing from OPEN or MF_IN_PROGRESS
        if prior_status not in ("OPEN", "MF_IN_PROGRESS"):
            raise GovernanceError(
                "invalid_status",
                f"Bug must be OPEN or MF_IN_PROGRESS to close, currently: {prior_status}",
                422,
            )

        # Verify commit SHA exists in git log (best-effort)
        commit_sha = body.get("commit", "")
        if commit_sha:
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--verify", commit_sha],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode != 0:
                    raise GovernanceError(
                        "commit_not_found",
                        f"Commit {commit_sha} does not resolve to a real commit",
                        422,
                    )
            except subprocess.TimeoutExpired:
                log.warning("git rev-parse timed out for commit %s; allowing close", commit_sha)
            except FileNotFoundError:
                log.warning("git not found; skipping commit verification for %s", commit_sha)

        timeline_gate = _verify_mf_close_timeline_gate(conn, pid, bug_id, row, body)

        # Determine chain_stage based on prior status
        chain_stage = "manual-fix" if prior_status == "MF_IN_PROGRESS" else None

        update_sql = """UPDATE backlog_bugs
               SET status = 'FIXED',
                   "commit" = ?,
                   fixed_at = ?,
                   updated_at = ?"""
        params = [body.get("commit", ""), now, now]

        if chain_stage:
            update_sql += """,
                   chain_stage = ?"""
            params.append(chain_stage)

        update_sql += """
               WHERE bug_id = ?"""
        params.append(bug_id)

        conn.execute(update_sql, params)
        backlog_runtime.update_backlog_runtime(
            conn,
            bug_id,
            "manual_fix" if prior_status == "MF_IN_PROGRESS" else "fixed",
            project_id=pid,
            result={"commit": body.get("commit", "")},
            runtime_state="fixed",
        )
        conn.commit()
        # Audit: backlog_close event
        try:
            audit_service.record(
                conn, pid, "backlog_close",
                actor=body.get("actor", "auto-chain"),
                bug_id=bug_id,
            )
            conn.commit()
        except Exception:
            pass  # best-effort audit
        result = {"ok": True, "bug_id": bug_id, "status": "FIXED", "fixed_at": now}
        if chain_stage:
            result["chain_stage"] = chain_stage
        if timeline_gate:
            result["timeline_gate"] = timeline_gate
        return result
    finally:
        conn.close()


def _verify_mf_close_timeline_gate(conn, project_id: str, bug_id: str, row, body: dict) -> dict:
    """Require append-only timeline evidence before observer/MF backlog close."""

    applicability = _mf_close_timeline_applicability(row)
    if not applicability["is_mf"]:
        return {}

    from . import task_timeline

    bypass = bool(body.get("bypass_timeline_gate"))
    if bypass:
        reason = str(body.get("timeline_bypass_reason") or "").strip()
        if len(reason) < 20:
            raise GovernanceError(
                "mf_timeline_bypass_reason_required",
                "bypass_timeline_gate requires timeline_bypass_reason with at least 20 characters",
                422,
            )
        verification = {
            "schema_version": "mf_close_timeline_gate.v1",
            "passed": True,
            "status": "bypassed",
            "reason": reason,
        }
        task_timeline.record_event(
            conn,
            project_id=project_id,
            backlog_id=bug_id,
            event_type="mf_timeline_gate_bypass",
            phase="close",
            event_kind="timeline_gate_bypass",
            actor=str(body.get("actor") or "observer"),
            status="accepted",
            payload={"reason": reason},
            verification=verification,
            commit_sha=str(body.get("commit") or ""),
        )
        return verification

    events = task_timeline.list_events(conn, project_id, backlog_id=bug_id, limit=1000)
    contract = backlog_runtime.parse_json_object(_row_get(row, "chain_trigger_json", "{}"))
    verification = task_timeline.mf_close_gate_verification(events, contract=contract)
    if not verification.get("passed"):
        missing_event_kinds = verification.get("missing_event_kinds") or []
        contract_gate = verification.get("contract_gate") if isinstance(verification.get("contract_gate"), dict) else {}
        missing_contract = contract_gate.get("missing_requirement_ids") or []
        missing = ", ".join([*missing_event_kinds, *missing_contract])
        raise GovernanceError(
            "mf_timeline_gate_failed",
            f"MF backlog close requires task timeline evidence before FIXED; missing: {missing}",
            422,
        )
    return verification


def _mf_close_timeline_applicability(row) -> dict:
    policy = backlog_runtime.parse_json_object(_row_get(row, "bypass_policy_json", "{}"))
    raw_mf_type = str(_row_get(row, "mf_type", "") or policy.get("mf_type") or "").strip()
    mf_type = backlog_runtime.normalize_mf_type(raw_mf_type, policy) if raw_mf_type else ""
    chain_stage = str(_row_get(row, "chain_stage", "") or "").strip()
    chain_stage_key = chain_stage.lower().replace("_", "-")
    prior_status = str(_row_get(row, "status", "") or "").strip()
    is_mf = bool(
        prior_status == "MF_IN_PROGRESS"
        or mf_type
        or chain_stage_key in {"manual-fix", "observer-hotfix", "chain-rescue"}
    )
    reasons = []
    if prior_status == "MF_IN_PROGRESS":
        reasons.append("status=MF_IN_PROGRESS")
    if mf_type:
        reasons.append(f"mf_type={mf_type}")
    if chain_stage_key in {"manual-fix", "observer-hotfix", "chain-rescue"}:
        reasons.append(f"chain_stage={chain_stage}")
    return {
        "is_mf": is_mf,
        "reason": ", ".join(reasons) if reasons else "not an MF/observer backlog row",
    }


_MF_TIMELINE_REASON_HUMAN = {
    "mf_type=chain_rescue": (
        "chain_rescue is the MVP internal storage label for observer-hotfix "
        "and manual-fix work, not an error or limitation. See aming-claw://mf-sop."
    ),
}


@route("GET", "/api/docs")
def handle_docs_index(ctx: RequestContext):
    """Return available documentation sections."""
    sections = []
    for key, doc in _DOCS.items():
        sections.append({
            "section": key,
            "title": doc.get("title", key),
            "url": f"/api/docs/{key}",
        })
    return {"sections": sections}


@route("GET", "/api/docs/{section}")
def handle_docs_section(ctx: RequestContext):
    """Return a specific documentation section."""
    section = ctx.path_params.get("section", "")
    if section not in _DOCS:
        from .errors import GovernanceError
        raise GovernanceError(f"Unknown doc section: {section}. Available: {list(_DOCS.keys())}", 404)
    return _DOCS[section]


# ============================================================
# Server Entry Point
# ============================================================

def create_server(port: int = None) -> HTTPServer:
    p = port or PORT
    # Z0-sequel observer-hotfix 2026-04-24: ThreadingHTTPServer so a slow
    # handler (e.g. on_task_completed waiting on Z1's 60s busy_timeout DB lock)
    # doesn't starve every other HTTP request — the "post-completion wedge"
    # symptom that blocked the Z0+Z2 verification chain 3× in 30min.
    server = ThreadingHTTPServer(("0.0.0.0", p), GovernanceHandler)
    return server


def main():
    # Configure logging to INFO level for observability
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    # PID lock — kill old process, prevent zombies
    _acquire_pid_lock()
    print(f"Governance v{get_server_version()} (PID {SERVER_PID})")

    # Enable Redis Pub/Sub bridge for EventBus
    from .event_bus import get_event_bus
    redis = get_redis()
    if redis.available:
        get_event_bus().enable_redis_bridge()
        print("EventBus: Redis Pub/Sub bridge enabled")
    else:
        print("EventBus: Redis unavailable, in-process only")

    # Register chain context EventBus subscribers + recover active chains
    try:
        from .chain_context import register_events, get_store
        register_events()
        # Recover active chains for known projects
        from .db import _governance_root
        gov_root = _governance_root()
        if gov_root.exists():
            for pdir in gov_root.iterdir():
                if pdir.is_dir() and (pdir / "governance.db").exists():
                    get_store().recover_from_db(pdir.name)
        print("ChainContext: registered + recovered")
    except Exception as e:
        print(f"ChainContext: failed to start ({e})")

    # Start doc generator listener
    try:
        from .doc_generator import setup_listener
        setup_listener()
        print("DocGenerator: listening for node.created events")
    except Exception as e:
        print(f"DocGenerator: failed to start ({e})")

    # MF-2026-05-10-016: event-driven in-process semantic worker.
    # Subscribes to semantic_job.enqueued + system.startup. Drains the
    # /semantic/jobs queue (node_semantic only) into pending_review state
    # so dashboard Review Queue can gate operator acceptance.
    try:
        from .semantic_worker import register as register_semantic_worker
        register_semantic_worker()
        print("SemanticWorker: registered (event-driven, in-process)")
    except Exception as e:
        print(f"SemanticWorker: failed to start ({e})")

    # Start outbox worker for reliable event delivery
    try:
        from .outbox import OutboxWorker
        outbox_worker = OutboxWorker()
        outbox_worker.start()
        print("OutboxWorker: started")
    except Exception as e:
        print(f"OutboxWorker: failed to start ({e})")

    # Per-project chain history backfill at startup (R5)
    try:
        from .chain_trailer import backfill_legacy_chain_history
        _conn = get_connection("aming-claw")
        try:
            _rows = _conn.execute(
                "SELECT DISTINCT project_id FROM project_version"
            ).fetchall()
            _pids = [r["project_id"] if isinstance(r, dict) else r[0] for r in _rows]
        except Exception:
            _pids = ["aming-claw"]
        finally:
            _conn.close()
        for _pid in _pids:
            try:
                _res = backfill_legacy_chain_history(project_id=_pid, incremental=True)
                print(f"ChainTrailer: backfill[{_pid}] {_res.get('scan_mode','?')} — "
                      f"{_res.get('new_entries',0)} new, {_res.get('total_entries',0)} total")
            except Exception as _e:
                print(f"ChainTrailer: backfill[{_pid}] failed ({_e})")
    except Exception as e:
        print(f"ChainTrailer: backfill failed ({e})")

    server = create_server()
    print(f"Governance service listening on port {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
