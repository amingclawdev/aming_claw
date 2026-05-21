"""Git URL bootstrap helpers for local Aming Claw plugin installs."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Union


DEFAULT_REPO_URL = "https://github.com/amingclawdev/aming-claw"
MIN_PYTHON_VERSION = (3, 9)
CODEX_MARKETPLACE_NAME = "aming-claw-local"
CODEX_PLUGIN_NAME = "aming-claw"
CODEX_PLUGIN_ID = f"{CODEX_PLUGIN_NAME}@{CODEX_MARKETPLACE_NAME}"
REQUIRED_PLUGIN_FILES = (
    ".codex-plugin/plugin.json",
    ".agents/plugins/marketplace.json",
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    "skills/aming-claw/SKILL.md",
    "skills/aming-claw-launcher/SKILL.md",
    ".mcp.json",
)
CODEX_PLUGIN_PAYLOAD = (
    ".codex-plugin",
    "skills",
    ".mcp.json",
    "README.md",
)
PLUGIN_STATE_SCHEMA_VERSION = 1
PLUGIN_UPDATE_STATUSES = {
    "current",
    "available",
    "applied_pending_restart",
    "failed",
    "unknown",
}
PLUGIN_RESTART_COMPONENTS = ("mcp", "governance", "service_manager")
AI_CLI_REQUIREMENTS = {
    "openai": {
        "runtime": "Codex CLI",
        "command": "codex",
        "env_var": "CODEX_BIN",
    },
    "anthropic": {
        "runtime": "Claude Code CLI",
        "command": "claude",
        "env_var": "CLAUDE_BIN",
    },
}


@dataclass
class CommandRecord:
    args: list[str]
    cwd: str = ""
    skipped: bool = False


@dataclass
class InstallResult:
    repo_url: str
    install_root: str
    plugin_root: str
    dry_run: bool
    installed_package: bool
    installed_codex_plugin: bool
    started: bool
    codex_home: str = ""
    codex_cache_path: str = ""
    codex_marketplace_root: str = ""
    codex_config_path: str = ""
    plugin_state_path: str = ""
    validated_files: list[str] = field(default_factory=list)
    commands: list[CommandRecord] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "repo_url": self.repo_url,
            "install_root": self.install_root,
            "plugin_root": self.plugin_root,
            "dry_run": self.dry_run,
            "installed_package": self.installed_package,
            "installed_codex_plugin": self.installed_codex_plugin,
            "codex_home": self.codex_home,
            "codex_cache_path": self.codex_cache_path,
            "codex_marketplace_root": self.codex_marketplace_root,
            "codex_config_path": self.codex_config_path,
            "plugin_state_path": self.plugin_state_path,
            "started": self.started,
            "validated_files": list(self.validated_files),
            "commands": [
                {"args": list(cmd.args), "cwd": cmd.cwd, "skipped": cmd.skipped}
                for cmd in self.commands
            ],
            "next_steps": list(self.next_steps),
        }


@dataclass
class DoctorCheck:
    name: str
    status: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class DoctorResult:
    plugin_root: str
    governance_url: str
    checks: list[DoctorCheck] = field(default_factory=list)
    manual_steps: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.status != "fail" for check in self.checks)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "plugin_root": self.plugin_root,
            "governance_url": self.governance_url,
            "checks": [check.to_dict() for check in self.checks],
            "manual_steps": list(self.manual_steps),
        }


class PluginInstallError(RuntimeError):
    """Raised when a Git URL plugin bootstrap step cannot complete."""


@dataclass
class PluginUpdateResult:
    repo_url: str
    install_root: str
    plugin_root: str
    action: str
    status: str
    ref: str = ""
    dry_run: bool = False
    update_available: bool = False
    applied: bool = False
    installed_package: bool = False
    installed_codex_plugin: bool = False
    installed_commit: str = ""
    remote_commit: str = ""
    changed_files: list[str] = field(default_factory=list)
    changed_surfaces: list[str] = field(default_factory=list)
    restart_required: dict[str, dict[str, Any]] = field(default_factory=dict)
    plugin_state_path: str = ""
    error: str = ""
    commands: list[CommandRecord] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status != "failed"

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "repo_url": self.repo_url,
            "install_root": self.install_root,
            "plugin_root": self.plugin_root,
            "action": self.action,
            "status": self.status,
            "ref": self.ref,
            "dry_run": self.dry_run,
            "update_available": self.update_available,
            "applied": self.applied,
            "installed_package": self.installed_package,
            "installed_codex_plugin": self.installed_codex_plugin,
            "installed_commit": self.installed_commit,
            "remote_commit": self.remote_commit,
            "changed_files": list(self.changed_files),
            "changed_surfaces": list(self.changed_surfaces),
            "restart_required": self.restart_required,
            "plugin_state_path": self.plugin_state_path,
            "error": self.error,
            "commands": [
                {"args": list(cmd.args), "cwd": cmd.cwd, "skipped": cmd.skipped}
                for cmd in self.commands
            ],
            "next_steps": list(self.next_steps),
        }


def default_install_root() -> Path:
    raw = os.environ.get("AMING_CLAW_PLUGIN_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".aming-claw" / "plugins"


def default_plugin_state_home() -> Path:
    raw = os.environ.get("AMING_CLAW_PLUGIN_STATE_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".aming-claw" / "plugin-state"


def default_plugin_update_state_path() -> Path:
    return default_plugin_state_home() / CODEX_MARKETPLACE_NAME / f"{CODEX_PLUGIN_NAME}.json"


def slug_from_repo_url(repo_url: str) -> str:
    cleaned = repo_url.rstrip("/").rstrip()
    tail = cleaned.rsplit("/", 1)[-1] or "aming-claw"
    tail = tail[:-4] if tail.endswith(".git") else tail
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", tail).strip(".-")
    return slug or "aming-claw"


def plugin_root_for(repo_url: str, install_root: Path) -> Path:
    return install_root.expanduser().resolve() / slug_from_repo_url(repo_url)


def _command_text(args: Sequence[str]) -> str:
    parts = [str(part) for part in args]
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def _run(
    args: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    dry_run: bool = False,
    commands: Optional[list[CommandRecord]] = None,
) -> None:
    record = CommandRecord(args=[str(part) for part in args], cwd=str(cwd or ""), skipped=dry_run)
    if commands is not None:
        commands.append(record)
    if dry_run:
        return
    try:
        subprocess.run([str(part) for part in args], cwd=str(cwd) if cwd else None, check=True)
    except FileNotFoundError as exc:
        raise PluginInstallError(f"command not found: {args[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise PluginInstallError(
            f"command failed ({exc.returncode}): {_command_text(record.args)}"
        ) from exc


def _git_commit(plugin_root: Path, *, short: bool = False) -> str:
    args = ["git", "rev-parse"]
    if short:
        args.append("--short")
    args.append("HEAD")
    try:
        proc = subprocess.run(
            args,
            cwd=str(plugin_root.expanduser().resolve()),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _git_capture(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: int = 15,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(part) for part in args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _git_output(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: int = 15,
) -> str:
    proc = _git_capture(args, cwd=cwd, timeout=timeout)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise PluginInstallError(f"command failed ({proc.returncode}): {_command_text(args)}{suffix}")
    return proc.stdout.strip()


def _git_output_optional(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: int = 15,
) -> str:
    try:
        proc = _git_capture(args, cwd=cwd, timeout=timeout)
    except Exception:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _git_rev_parse(root: Path, rev: str) -> str:
    return _git_output_optional(["git", "rev-parse", "--verify", f"{rev}^{{commit}}"], cwd=root)


def _spawn_long_running(
    args: Sequence[str],
    *,
    cwd: Path,
    dry_run: bool = False,
    commands: Optional[list[CommandRecord]] = None,
) -> None:
    record = CommandRecord(args=[str(part) for part in args], cwd=str(cwd), skipped=dry_run)
    if commands is not None:
        commands.append(record)
    if dry_run:
        return

    log_path = cwd / ".aming-claw-start.log"
    try:
        log_handle = log_path.open("ab")
    except OSError as exc:
        raise PluginInstallError(f"cannot open start log {log_path}: {exc}") from exc

    popen_kwargs = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [str(part) for part in args],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            **popen_kwargs,
        )
    except FileNotFoundError as exc:
        raise PluginInstallError(f"command not found: {args[0]}") from exc
    except OSError as exc:
        raise PluginInstallError(f"failed to start long-running service: {exc}") from exc
    finally:
        log_handle.close()


def validate_plugin_root(plugin_root: Path) -> list[str]:
    root = plugin_root.expanduser().resolve()
    missing = [rel for rel in REQUIRED_PLUGIN_FILES if not (root / rel).is_file()]
    if missing:
        raise PluginInstallError(
            "plugin root is missing required files: " + ", ".join(missing)
        )

    for rel in (
        ".codex-plugin/plugin.json",
        ".agents/plugins/marketplace.json",
        ".claude-plugin/plugin.json",
        ".claude-plugin/marketplace.json",
        ".mcp.json",
    ):
        try:
            json.loads((root / rel).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PluginInstallError(f"invalid JSON in {rel}: {exc}") from exc

    return list(REQUIRED_PLUGIN_FILES)


def default_codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def default_codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".codex"


def default_codex_marketplace_root() -> Path:
    return Path.home() / ".aming-claw" / "codex-marketplaces" / CODEX_MARKETPLACE_NAME


def _read_codex_manifest_version(plugin_root: Path) -> str:
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    if not manifest_path.is_file():
        return "0.1.0"
    manifest = _read_json_file(manifest_path)
    version = str(manifest.get("version") or "").strip()
    return version or "0.1.0"


def codex_cache_plugin_root(
    plugin_root: Path,
    *,
    codex_home: Optional[Union[Path, str]] = None,
) -> Path:
    home = Path(codex_home).expanduser() if codex_home else default_codex_home()
    version = _read_codex_manifest_version(plugin_root)
    return home / "plugins" / "cache" / CODEX_MARKETPLACE_NAME / CODEX_PLUGIN_NAME / version


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _copy_plugin_payload(plugin_root: Path, target_root: Path, *, dry_run: bool = False) -> None:
    source_root = plugin_root.expanduser().resolve()
    target = target_root.expanduser().resolve()
    if dry_run:
        return
    target.mkdir(parents=True, exist_ok=True)
    for rel in CODEX_PLUGIN_PAYLOAD:
        source = source_root / rel
        if not source.exists():
            continue
        destination = target / rel
        if source.is_dir():
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def _cache_runtime_mcp_config(plugin_root: Path, *, python_executable: Optional[str] = None) -> dict:
    source_path = plugin_root.expanduser().resolve() / ".mcp.json"
    payload = _read_json_file(source_path)
    servers = payload.get("mcpServers") if isinstance(payload, dict) else {}
    if not isinstance(servers, dict):
        servers = {}
    server = dict(servers.get("aming-claw") or {})

    runtime_root = str(plugin_root.expanduser().resolve())
    server["command"] = python_executable or str(server.get("command") or "python")
    if not isinstance(server.get("args"), list) or not server["args"]:
        server["args"] = [
            "-m",
            "agent.mcp.server",
            "--project",
            "aming-claw",
            "--workers",
            "0",
            "--governance-url",
            "http://localhost:40000",
        ]
    server["cwd"] = runtime_root

    env = server.get("env") if isinstance(server.get("env"), dict) else {}
    env = dict(env)
    pythonpath = str(env.get("PYTHONPATH") or "")
    parts = [part for part in pythonpath.split(os.pathsep) if part]
    if runtime_root not in parts:
        parts.insert(0, runtime_root)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    server["env"] = env

    payload["mcpServers"] = servers
    payload["mcpServers"]["aming-claw"] = server
    return payload


def _write_cache_runtime_mcp_config(
    plugin_root: Path,
    target_root: Path,
    *,
    python_executable: Optional[str] = None,
    dry_run: bool = False,
) -> None:
    if dry_run:
        return
    target = target_root.expanduser().resolve() / ".mcp.json"
    target.write_text(
        json.dumps(
            _cache_runtime_mcp_config(plugin_root, python_executable=python_executable),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def install_codex_plugin_cache(
    plugin_root: Path,
    *,
    codex_home: Optional[Union[Path, str]] = None,
    python_executable: Optional[str] = None,
    dry_run: bool = False,
    commands: Optional[list[CommandRecord]] = None,
) -> Path:
    target = codex_cache_plugin_root(plugin_root, codex_home=codex_home)
    home = Path(codex_home).expanduser().resolve() if codex_home else default_codex_home().resolve()
    if not _is_relative_to(target, home / "plugins" / "cache"):
        raise PluginInstallError(f"refusing to write Codex plugin cache outside {home / 'plugins' / 'cache'}: {target}")
    if commands is not None:
        commands.append(CommandRecord(args=["install-codex-cache", str(target)], skipped=dry_run))
    _copy_plugin_payload(plugin_root, target, dry_run=dry_run)
    _write_cache_runtime_mcp_config(
        plugin_root,
        target,
        python_executable=python_executable,
        dry_run=dry_run,
    )
    return target


def _codex_marketplace_payload() -> dict:
    return {
        "name": CODEX_MARKETPLACE_NAME,
        "interface": {"displayName": "Aming Claw Local"},
        "plugins": [
            {
                "name": CODEX_PLUGIN_NAME,
                "source": {"source": "local", "path": f"./{CODEX_PLUGIN_NAME}"},
                "policy": {
                    "installation": "INSTALLED_BY_DEFAULT",
                    "authentication": "ON_INSTALL",
                },
                "category": "Productivity",
            }
        ],
    }


def install_codex_marketplace(
    plugin_root: Path,
    *,
    marketplace_root: Optional[Union[Path, str]] = None,
    python_executable: Optional[str] = None,
    dry_run: bool = False,
    commands: Optional[list[CommandRecord]] = None,
) -> Path:
    root = Path(marketplace_root).expanduser() if marketplace_root else default_codex_marketplace_root()
    root = root.resolve()
    agents_root = root / ".agents" / "plugins"
    plugin_target = agents_root / CODEX_PLUGIN_NAME
    if commands is not None:
        commands.append(CommandRecord(args=["install-codex-marketplace", str(root)], skipped=dry_run))
    if dry_run:
        return root
    agents_root.mkdir(parents=True, exist_ok=True)
    (agents_root / "marketplace.json").write_text(
        json.dumps(_codex_marketplace_payload(), indent=2),
        encoding="utf-8",
    )
    _copy_plugin_payload(plugin_root, plugin_target, dry_run=False)
    _write_cache_runtime_mcp_config(
        plugin_root,
        plugin_target,
        python_executable=python_executable,
        dry_run=False,
    )
    return root


def _toml_quote(value: Union[str, Path]) -> str:
    text = str(value)
    if "'" not in text:
        return f"'{text}'"
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_table_pattern(table_name: str) -> re.Pattern[str]:
    escaped = re.escape(table_name)
    return re.compile(rf"(?ms)^\[{escaped}\]\s*\r?\n.*?(?=^\[|\Z)")


def _upsert_toml_table(text: str, table_name: str, block_body: str) -> str:
    block = f"[{table_name}]\n{block_body.rstrip()}\n"
    pattern = _toml_table_pattern(table_name)
    if pattern.search(text):
        return pattern.sub(lambda _match: block, text).rstrip() + "\n"
    prefix = text.rstrip()
    return (prefix + "\n\n" if prefix else "") + block


def configure_codex_plugin(
    *,
    codex_config: Optional[Union[Path, str]] = None,
    marketplace_root: Optional[Union[Path, str]] = None,
    dry_run: bool = False,
    commands: Optional[list[CommandRecord]] = None,
) -> Path:
    config_path = Path(codex_config).expanduser() if codex_config else default_codex_config_path()
    market_root = Path(marketplace_root).expanduser() if marketplace_root else default_codex_marketplace_root()
    if commands is not None:
        commands.append(CommandRecord(args=["configure-codex-plugin", str(config_path)], skipped=dry_run))
    if dry_run:
        return config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)
    text = config_path.read_text(encoding="utf-8", errors="replace") if config_path.is_file() else ""
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    text = _upsert_toml_table(
        text,
        f"marketplaces.{CODEX_MARKETPLACE_NAME}",
        "\n".join(
            [
                'source_type = "local"',
                f"source = {_toml_quote(market_root.resolve())}",
                f"last_updated = {_toml_quote(timestamp)}",
            ]
        ),
    )
    text = _upsert_toml_table(
        text,
        f'plugins."{CODEX_PLUGIN_ID}"',
        "enabled = true",
    )
    config_path.write_text(text, encoding="utf-8")
    return config_path


def _default_doctor_root() -> Path:
    cwd = Path.cwd()
    if (cwd / ".codex-plugin" / "plugin.json").is_file():
        return cwd.resolve()
    return plugin_root_for(DEFAULT_REPO_URL, default_install_root())


def _doctor_check(name: str, status: str, detail: str = "") -> DoctorCheck:
    return DoctorCheck(name=name, status=status, detail=detail)


def _read_json_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _restart_required_template() -> dict[str, dict[str, Any]]:
    return {
        component: {
            "required": False,
            "reason": "",
            "satisfied_by": "",
            "satisfied": False,
        }
        for component in PLUGIN_RESTART_COMPONENTS
    }


def _normalize_restart_required(value: Any) -> dict[str, dict[str, Any]]:
    normalized = _restart_required_template()
    if not isinstance(value, dict):
        return normalized
    for component in PLUGIN_RESTART_COMPONENTS:
        raw = value.get(component)
        if not isinstance(raw, dict):
            continue
        normalized[component] = {
            "required": bool(raw.get("required")),
            "reason": str(raw.get("reason") or ""),
            "satisfied_by": str(raw.get("satisfied_by") or ""),
            "satisfied": bool(raw.get("satisfied")),
        }
    return normalized


def normalize_plugin_update_state(payload: Optional[dict[str, Any]]) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    status = str(raw.get("update_status") or "unknown").strip() or "unknown"
    if status not in PLUGIN_UPDATE_STATUSES:
        status = "unknown"
    try:
        schema_version = int(raw.get("schema_version") or PLUGIN_STATE_SCHEMA_VERSION)
    except (TypeError, ValueError):
        schema_version = PLUGIN_STATE_SCHEMA_VERSION
    return {
        "schema_version": schema_version,
        "plugin_id": str(raw.get("plugin_id") or CODEX_PLUGIN_ID),
        "plugin_root": str(raw.get("plugin_root") or ""),
        "repo_url": str(raw.get("repo_url") or DEFAULT_REPO_URL),
        "ref": str(raw.get("ref") or ""),
        "installed_commit": str(raw.get("installed_commit") or ""),
        "installed_version": str(raw.get("installed_version") or ""),
        "remote_commit": str(raw.get("remote_commit") or ""),
        "update_status": status,
        "last_checked_at": str(raw.get("last_checked_at") or ""),
        "last_applied_at": str(raw.get("last_applied_at") or ""),
        "last_error": str(raw.get("last_error") or ""),
        "changed_surfaces": [
            str(item)
            for item in raw.get("changed_surfaces", [])
            if str(item).strip()
        ] if isinstance(raw.get("changed_surfaces"), list) else [],
        "restart_required": _normalize_restart_required(raw.get("restart_required")),
    }


def write_plugin_update_state(
    *,
    plugin_root: Union[Path, str],
    repo_url: str = DEFAULT_REPO_URL,
    ref: str = "",
    update_status: str = "current",
    remote_commit: str = "",
    changed_surfaces: Optional[list[str]] = None,
    restart_required: Optional[dict[str, Any]] = None,
    last_error: str = "",
    state_path: Optional[Union[Path, str]] = None,
    dry_run: bool = False,
) -> Path:
    """Write the local plugin update state file used by MF/preflight checks."""

    path = Path(state_path).expanduser() if state_path else default_plugin_update_state_path()
    root = Path(plugin_root).expanduser().resolve()
    now = _utc_now()
    payload = normalize_plugin_update_state(
        {
            "schema_version": PLUGIN_STATE_SCHEMA_VERSION,
            "plugin_id": CODEX_PLUGIN_ID,
            "plugin_root": str(root),
            "repo_url": repo_url,
            "ref": ref,
            "installed_commit": _git_commit(root),
            "installed_version": _read_codex_manifest_version(root),
            "remote_commit": remote_commit,
            "update_status": update_status,
            "last_checked_at": now if remote_commit else "",
            "last_applied_at": now if update_status in {"current", "applied_pending_restart"} else "",
            "last_error": last_error,
            "changed_surfaces": changed_surfaces or [],
            "restart_required": restart_required or _restart_required_template(),
        }
    )
    if dry_run:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)
    return path


def _self_graph_bundle_status(
    *,
    plugin_root: Optional[Union[Path, str]] = None,
    manifest_path: Optional[Union[Path, str]] = None,
) -> dict[str, Any]:
    try:
        from agent.governance.self_graph_bundle_check import check_self_graph_bundle
    except ImportError:
        try:
            from governance.self_graph_bundle_check import check_self_graph_bundle  # type: ignore
        except ImportError as exc:
            return {
                "ok": True,
                "status": "warn",
                "warnings": [f"self graph bundle check unavailable: {exc}"],
                "blockers": [],
                "events": [],
            }
    try:
        return check_self_graph_bundle(
            plugin_root=plugin_root,
            manifest_path=manifest_path,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics should report, not crash caller.
        return {
            "ok": False,
            "status": "fail",
            "warnings": [],
            "blockers": [f"self graph bundle check failed: {exc}"],
            "events": [],
        }


def _merge_self_graph_bundle_status(
    blockers: list[str],
    warnings: list[str],
    bundle_status: dict[str, Any],
) -> None:
    if not bundle_status:
        return
    for item in bundle_status.get("blockers") or []:
        blockers.append(f"self graph bundle: {item}")
    for item in bundle_status.get("warnings") or []:
        warnings.append(f"self graph bundle: {item}")
    for event in bundle_status.get("events") or []:
        reason = event.get("reason", "plugin_update_reminder")
        warnings.append(f"self graph bundle event: {reason}")


def plugin_update_state_status(
    *,
    state_path: Optional[Union[Path, str]] = None,
    plugin_root: Optional[Union[Path, str]] = None,
    self_graph_bundle_manifest: Optional[Union[Path, str]] = None,
    include_self_graph_bundle: bool = True,
) -> dict[str, Any]:
    """Return read-only plugin update/restart obligation status.

    This is intentionally local-only: it never contacts the remote Git source.
    `plugin update --check` / `plugin doctor` can refresh remote commit data
    explicitly, while preflight can safely call this on every MF run.
    """

    path = Path(state_path).expanduser() if state_path else default_plugin_update_state_path()
    bundle_status = (
        _self_graph_bundle_status(plugin_root=plugin_root, manifest_path=self_graph_bundle_manifest)
        if include_self_graph_bundle
        else {}
    )
    if not path.is_file():
        blockers: list[str] = []
        warnings = ["plugin update state file not found"]
        _merge_self_graph_bundle_status(blockers, warnings, bundle_status)
        status = "fail" if blockers else ("warn" if warnings else "pass")
        return {
            "ok": not blockers,
            "status": status,
            "state_exists": False,
            "state_path": str(path),
            "update_status": "unknown",
            "blockers": blockers,
            "warnings": warnings,
            "state": normalize_plugin_update_state({}),
            "self_graph_bundle": bundle_status,
        }

    try:
        payload = _read_json_file(path)
    except Exception as exc:
        blockers = [f"cannot read plugin update state: {exc}"]
        warnings: list[str] = []
        _merge_self_graph_bundle_status(blockers, warnings, bundle_status)
        return {
            "ok": False,
            "status": "fail",
            "state_exists": True,
            "state_path": str(path),
            "update_status": "unknown",
            "blockers": blockers,
            "warnings": warnings,
            "state": normalize_plugin_update_state({}),
            "self_graph_bundle": bundle_status,
        }

    state = normalize_plugin_update_state(payload)
    if include_self_graph_bundle and not plugin_root and state.get("plugin_root"):
        bundle_status = _self_graph_bundle_status(
            plugin_root=state.get("plugin_root"),
            manifest_path=self_graph_bundle_manifest,
        )
    blockers: list[str] = []
    warnings: list[str] = []
    update_status = state["update_status"]

    if update_status == "available":
        warnings.append("plugin update available")
    elif update_status == "unknown":
        warnings.append("plugin update state unknown")
    elif update_status == "failed":
        detail = f": {state['last_error']}" if state.get("last_error") else ""
        blockers.append(f"plugin update failed{detail}")

    pending_components = []
    for component, restart in state["restart_required"].items():
        if restart.get("required") and not restart.get("satisfied"):
            pending_components.append(component)
    if pending_components:
        blockers.append(
            "restart/reload required for " + ", ".join(sorted(pending_components))
        )
    elif update_status == "applied_pending_restart":
        blockers.append("plugin update applied but restart/reload satisfaction is unknown")

    _merge_self_graph_bundle_status(blockers, warnings, bundle_status)
    status = "fail" if blockers else ("warn" if warnings else "pass")
    return {
        "ok": not blockers,
        "status": status,
        "state_exists": True,
        "state_path": str(path),
        "update_status": update_status,
        "blockers": blockers,
        "warnings": warnings,
        "state": state,
        "self_graph_bundle": bundle_status,
    }


def classify_plugin_changed_surfaces(changed_files: Sequence[str]) -> list[str]:
    """Classify changed paths into runtime surfaces that need operator action."""

    surfaces: set[str] = set()
    for raw_path in changed_files:
        path = str(raw_path).replace("\\", "/").lstrip("./")
        if not path:
            continue
        if (
            path == ".mcp.json"
            or path == "README.md"
            or path.startswith(".codex-plugin/")
            or path.startswith(".claude-plugin/")
            or path.startswith(".agents/plugins/")
            or path.startswith("skills/")
            or path.startswith("agent/mcp/")
            or path in {"agent/plugin_installer.py", "agent/cli.py"}
        ):
            surfaces.add("mcp")
        if (
            path == "start_governance.py"
            or path.startswith("agent/governance/")
        ):
            surfaces.add("governance")
        if path in {
            "agent/service_manager.py",
            "agent/manager_http_server.py",
            "scripts/start-manager.sh",
            "scripts/start-manager.ps1",
        }:
            surfaces.add("service_manager")
    return [component for component in PLUGIN_RESTART_COMPONENTS if component in surfaces]


def plugin_restart_required_for_surfaces(
    surfaces: Sequence[str],
) -> dict[str, dict[str, Any]]:
    restart = _restart_required_template()
    reasons = {
        "mcp": (
            "plugin MCP, skill, manifest, or runtime entrypoint changed",
            "reload Codex/Claude or open a new session",
        ),
        "governance": (
            "governance service files changed",
            "redeploy or restart governance",
        ),
        "service_manager": (
            "ServiceManager files changed",
            "restart ServiceManager",
        ),
    }
    for component in surfaces:
        if component not in restart:
            continue
        reason, satisfied_by = reasons[component]
        restart[component] = {
            "required": True,
            "reason": reason,
            "satisfied_by": satisfied_by,
            "satisfied": False,
        }
    return restart


def _require_git_checkout(plugin_root: Path) -> None:
    if not (plugin_root / ".git").exists():
        raise PluginInstallError(
            f"plugin checkout not found at {plugin_root}; run `aming-claw plugin install` first"
        )


def _current_git_branch(plugin_root: Path) -> str:
    return _git_output_optional(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=plugin_root,
    )


def _remote_ref_candidates(plugin_root: Path, ref: str) -> list[str]:
    if ref:
        return [
            f"refs/remotes/origin/{ref}",
            f"origin/{ref}",
            f"refs/tags/{ref}",
            ref,
        ]
    upstream = _git_output_optional(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        cwd=plugin_root,
    )
    candidates = [upstream] if upstream else []
    branch = _current_git_branch(plugin_root)
    if branch and branch != "HEAD":
        candidates.append(f"refs/remotes/origin/{branch}")
        candidates.append(f"origin/{branch}")
    return [candidate for candidate in candidates if candidate]


def _resolve_remote_commit(plugin_root: Path, ref: str) -> str:
    candidates = _remote_ref_candidates(plugin_root, ref)
    for candidate in candidates:
        commit = _git_rev_parse(plugin_root, candidate)
        if commit:
            return commit
    if ref:
        raise PluginInstallError(f"cannot resolve remote ref `{ref}` after fetch")
    raise PluginInstallError(
        "cannot resolve upstream remote for plugin checkout; pass --ref or set branch upstream"
    )


def _changed_files_between(plugin_root: Path, old_commit: str, new_commit: str) -> list[str]:
    if not old_commit or not new_commit or old_commit == new_commit:
        return []
    output = _git_output(
        ["git", "diff", "--name-only", old_commit, new_commit],
        cwd=plugin_root,
        timeout=20,
    )
    return [line.strip() for line in output.splitlines() if line.strip()]


def _apply_plugin_commit(
    plugin_root: Path,
    remote_commit: str,
    *,
    commands: list[CommandRecord],
    dry_run: bool,
) -> None:
    branch = _current_git_branch(plugin_root)
    if branch and branch != "HEAD":
        _run(
            ["git", "merge", "--ff-only", remote_commit],
            cwd=plugin_root,
            dry_run=dry_run,
            commands=commands,
        )
    else:
        _run(
            ["git", "checkout", "--detach", remote_commit],
            cwd=plugin_root,
            dry_run=dry_run,
            commands=commands,
        )


def _write_failed_plugin_update_state(
    *,
    plugin_root: Path,
    repo_url: str,
    ref: str,
    remote_commit: str,
    error: str,
    state_path: Optional[Union[Path, str]],
    dry_run: bool,
) -> str:
    path = write_plugin_update_state(
        plugin_root=plugin_root,
        repo_url=repo_url,
        ref=ref,
        update_status="failed",
        remote_commit=remote_commit,
        last_error=error,
        state_path=state_path,
        dry_run=dry_run,
    )
    return str(path)


def update_plugin_from_git(
    repo_url: str = DEFAULT_REPO_URL,
    *,
    install_root: Optional[Union[Path, str]] = None,
    ref: str = "",
    apply_update: bool = False,
    python_executable: Optional[str] = None,
    install_package: bool = True,
    install_codex_plugin: bool = True,
    codex_home: Optional[Union[Path, str]] = None,
    codex_config: Optional[Union[Path, str]] = None,
    codex_marketplace_root: Optional[Union[Path, str]] = None,
    state_path: Optional[Union[Path, str]] = None,
    dry_run: bool = False,
) -> PluginUpdateResult:
    """Check or apply a Git-backed plugin update and refresh local state."""

    root = Path(install_root).expanduser() if install_root else default_install_root()
    plugin_root = plugin_root_for(repo_url, root)
    python = python_executable or sys.executable
    commands: list[CommandRecord] = []
    action = "apply" if apply_update else "check"
    installed_commit = _git_commit(plugin_root)
    remote_commit = ""
    restart_required = _restart_required_template()
    changed_files: list[str] = []
    changed_surfaces: list[str] = []
    state_target = str(Path(state_path).expanduser() if state_path else default_plugin_update_state_path())

    try:
        _require_git_checkout(plugin_root)
        _run(
            ["git", "fetch", "--all", "--prune"],
            cwd=plugin_root,
            dry_run=dry_run,
            commands=commands,
        )
        if dry_run:
            status = "dry_run"
            next_steps = ["Run without --dry-run to refresh plugin update state or apply the update."]
            return PluginUpdateResult(
                repo_url=repo_url,
                install_root=str(root.expanduser().resolve()),
                plugin_root=str(plugin_root),
                action=action,
                status=status,
                ref=ref,
                dry_run=True,
                installed_commit=installed_commit,
                plugin_state_path=state_target,
                commands=commands,
                next_steps=next_steps,
            )

        remote_commit = _resolve_remote_commit(plugin_root, ref)
        installed_commit = _git_commit(plugin_root)
        changed_files = _changed_files_between(plugin_root, installed_commit, remote_commit)
        changed_surfaces = classify_plugin_changed_surfaces(changed_files)

        if installed_commit == remote_commit:
            state_target = str(write_plugin_update_state(
                plugin_root=plugin_root,
                repo_url=repo_url,
                ref=ref,
                update_status="current",
                remote_commit=remote_commit,
                changed_surfaces=[],
                restart_required=restart_required,
                state_path=state_path,
            ))
            return PluginUpdateResult(
                repo_url=repo_url,
                install_root=str(root.expanduser().resolve()),
                plugin_root=str(plugin_root),
                action=action,
                status="current",
                ref=ref,
                installed_commit=installed_commit,
                remote_commit=remote_commit,
                plugin_state_path=state_target,
                commands=commands,
                next_steps=["Plugin checkout is already current."],
            )

        if not apply_update:
            state_target = str(write_plugin_update_state(
                plugin_root=plugin_root,
                repo_url=repo_url,
                ref=ref,
                update_status="available",
                remote_commit=remote_commit,
                changed_surfaces=changed_surfaces,
                restart_required=restart_required,
                state_path=state_path,
            ))
            return PluginUpdateResult(
                repo_url=repo_url,
                install_root=str(root.expanduser().resolve()),
                plugin_root=str(plugin_root),
                action=action,
                status="available",
                ref=ref,
                update_available=True,
                installed_commit=installed_commit,
                remote_commit=remote_commit,
                changed_files=changed_files,
                changed_surfaces=changed_surfaces,
                restart_required=restart_required,
                plugin_state_path=state_target,
                commands=commands,
                next_steps=["Run `aming-claw plugin update --apply` to fast-forward the local plugin checkout."],
            )

        _apply_plugin_commit(plugin_root, remote_commit, commands=commands, dry_run=dry_run)
        validated = validate_plugin_root(plugin_root)
        installed_package = False
        if install_package:
            _ensure_supported_python(python)
            _run(
                [python, "-m", "pip", "install", "-e", str(plugin_root)],
                dry_run=False,
                commands=commands,
            )
            installed_package = True

        installed_codex_plugin = False
        if install_codex_plugin:
            cache_target = install_codex_plugin_cache(
                plugin_root,
                codex_home=codex_home,
                python_executable=python,
                commands=commands,
            )
            marketplace_target = install_codex_marketplace(
                plugin_root,
                marketplace_root=codex_marketplace_root,
                python_executable=python,
                commands=commands,
            )
            configure_codex_plugin(
                codex_config=codex_config,
                marketplace_root=marketplace_target,
                commands=commands,
            )
            installed_codex_plugin = bool(cache_target)

        restart_required = plugin_restart_required_for_surfaces(changed_surfaces)
        update_status = (
            "applied_pending_restart"
            if any(item.get("required") for item in restart_required.values())
            else "current"
        )
        state_target = str(write_plugin_update_state(
            plugin_root=plugin_root,
            repo_url=repo_url,
            ref=ref,
            update_status=update_status,
            remote_commit=remote_commit,
            changed_surfaces=changed_surfaces,
            restart_required=restart_required,
            state_path=state_path,
        ))
        next_steps = [
            "Restart/reload any required surfaces listed in the plugin update state.",
            f"Verify update: {python} -m agent.cli plugin doctor --plugin-root {plugin_root}",
            "After restart/reload, run `aming-claw plugin update --check` to mark the installed commit current.",
        ]
        if validated:
            next_steps.append("Then run `aming-claw mf precommit-check` to confirm no plugin update blockers remain.")
        return PluginUpdateResult(
            repo_url=repo_url,
            install_root=str(root.expanduser().resolve()),
            plugin_root=str(plugin_root),
            action=action,
            status=update_status,
            ref=ref,
            applied=True,
            installed_package=installed_package,
            installed_codex_plugin=installed_codex_plugin,
            installed_commit=installed_commit,
            remote_commit=remote_commit,
            changed_files=changed_files,
            changed_surfaces=changed_surfaces,
            restart_required=restart_required,
            plugin_state_path=state_target,
            commands=commands,
            next_steps=next_steps,
        )
    except Exception as exc:
        error = str(exc)
        try:
            state_target = _write_failed_plugin_update_state(
                plugin_root=plugin_root,
                repo_url=repo_url,
                ref=ref,
                remote_commit=remote_commit,
                error=error,
                state_path=state_path,
                dry_run=dry_run,
            )
        except Exception:
            pass
        return PluginUpdateResult(
            repo_url=repo_url,
            install_root=str(root.expanduser().resolve()),
            plugin_root=str(plugin_root),
            action=action,
            status="failed",
            ref=ref,
            dry_run=dry_run,
            installed_commit=installed_commit,
            remote_commit=remote_commit,
            changed_files=changed_files,
            changed_surfaces=changed_surfaces,
            restart_required=restart_required,
            plugin_state_path=state_target,
            error=error,
            commands=commands,
            next_steps=["Resolve the error, then rerun `aming-claw plugin update --check`."],
        )


def _parse_python_version(text: str) -> Optional[tuple[int, int, int]]:
    match = re.search(r"Python\s+(\d+)\.(\d+)(?:\.(\d+))?", str(text or ""))
    if not match:
        return None
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3) or 0),
    )


def _python_version_check(python_executable: str) -> DoctorCheck:
    try:
        proc = subprocess.run(
            [python_executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return _doctor_check(
            "python_runtime",
            "fail",
            f"{python_executable}: cannot run --version ({exc})",
        )
    version_text = (proc.stdout or proc.stderr or "").strip()
    parsed = _parse_python_version(version_text)
    if proc.returncode != 0 or parsed is None:
        return _doctor_check(
            "python_runtime",
            "fail",
            f"{python_executable}: cannot determine Python version ({version_text or proc.returncode})",
        )
    required = ".".join(str(part) for part in MIN_PYTHON_VERSION)
    found = ".".join(str(part) for part in parsed)
    if parsed < (*MIN_PYTHON_VERSION, 0):
        return _doctor_check(
            "python_runtime",
            "fail",
            f"{python_executable}: Python {found} detected; Aming Claw requires Python {required}+; pass --python <path-to-python-{required}-or-newer>",
        )
    return _doctor_check("python_runtime", "ok", f"{python_executable}: Python {found}")


def _ensure_supported_python(python_executable: str) -> None:
    check = _python_version_check(python_executable)
    if check.status != "ok":
        raise PluginInstallError(check.detail)


def _check_codex_manifest(plugin_root: Path) -> DoctorCheck:
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    try:
        manifest = _read_json_file(manifest_path)
    except Exception as exc:
        return _doctor_check("codex_manifest", "fail", f"{manifest_path}: {exc}")
    interface = manifest.get("interface") if isinstance(manifest, dict) else {}
    prompts = interface.get("defaultPrompt") if isinstance(interface, dict) else []
    if prompts is None:
        prompts = []
    if not isinstance(prompts, list):
        return _doctor_check("codex_manifest", "fail", "interface.defaultPrompt must be a list")
    if len(prompts) > 3:
        return _doctor_check("codex_manifest", "fail", "interface.defaultPrompt must contain at most 3 prompts")
    too_long = [str(prompt) for prompt in prompts if len(str(prompt)) > 128]
    if too_long:
        return _doctor_check("codex_manifest", "fail", "interface.defaultPrompt entries must be <=128 chars")
    return _doctor_check("codex_manifest", "ok", f"{manifest_path} defaultPrompt count={len(prompts)}")


def _check_marketplace(plugin_root: Path) -> DoctorCheck:
    marketplace_path = plugin_root / ".agents" / "plugins" / "marketplace.json"
    try:
        marketplace = _read_json_file(marketplace_path)
    except Exception as exc:
        return _doctor_check("codex_marketplace", "fail", f"{marketplace_path}: {exc}")

    plugins = marketplace.get("plugins") if isinstance(marketplace, dict) else []
    match = next(
        (
            item
            for item in plugins or []
            if isinstance(item, dict) and item.get("name") == "aming-claw"
        ),
        None,
    )
    if not match:
        return _doctor_check("codex_marketplace", "fail", "missing plugin entry `aming-claw`")

    source = match.get("source") if isinstance(match.get("source"), dict) else {}
    raw_path = str(source.get("path") or "").strip()
    if not raw_path:
        return _doctor_check("codex_marketplace", "fail", "missing source.path")
    if raw_path in {".", "./"}:
        return _doctor_check(
            "codex_marketplace",
            "fail",
            f"source.path={raw_path!r} normalizes to an empty local plugin path for Codex CLI",
        )

    marketplace_root = marketplace_path.parent.resolve()
    resolved = (marketplace_root / raw_path).resolve()
    if not _is_relative_to(resolved, marketplace_root):
        return _doctor_check(
            "codex_marketplace",
            "warn",
            f"{marketplace_path} is a repo-local compatibility manifest; source.path={raw_path!r} escapes the Codex marketplace root. The installer writes a generated marketplace/cache for real Codex CLI loading.",
        )
    if not (resolved / ".codex-plugin" / "plugin.json").is_file():
        return _doctor_check(
            "codex_marketplace",
            "warn",
            f"source.path={raw_path!r} resolves to {resolved}, but no .codex-plugin/plugin.json was found",
        )
    if not raw_path.startswith("./"):
        return _doctor_check(
            "codex_marketplace",
            "warn",
            f"source.path={raw_path!r} resolves to {resolved}; prefer './{CODEX_PLUGIN_NAME}' inside the marketplace root",
        )
    return _doctor_check(
        "codex_marketplace",
        "ok",
        f"{marketplace_path} -> source.path {raw_path!r} resolves to {resolved}",
    )


def _check_claude_marketplace(plugin_root: Path) -> DoctorCheck:
    marketplace_path = plugin_root / ".claude-plugin" / "marketplace.json"
    try:
        marketplace = _read_json_file(marketplace_path)
    except Exception as exc:
        return _doctor_check("claude_marketplace", "fail", f"{marketplace_path}: {exc}")

    if not isinstance(marketplace, dict):
        return _doctor_check("claude_marketplace", "fail", f"{marketplace_path}: top-level must be an object")
    if not str(marketplace.get("name") or "").strip():
        return _doctor_check("claude_marketplace", "fail", "missing top-level `name`")

    owner = marketplace.get("owner")
    if not isinstance(owner, dict) or not str(owner.get("name") or "").strip():
        return _doctor_check("claude_marketplace", "fail", "missing or empty `owner.name`")

    plugins = marketplace.get("plugins")
    match = next(
        (
            item
            for item in plugins or []
            if isinstance(item, dict) and item.get("name") == "aming-claw"
        ),
        None,
    )
    if not match:
        return _doctor_check("claude_marketplace", "fail", "missing plugin entry `aming-claw`")

    source = str(match.get("source") or "").strip()
    if not source:
        return _doctor_check("claude_marketplace", "fail", "missing plugins[].source")
    if not source.startswith("./"):
        # Claude Code 2.1.140 rejects bare "." as Invalid input; "./" is the
        # canonical relative form (see MF-2026-05-15-CLAUDE-MARKETPLACE-SOURCE-SCHEMA).
        return _doctor_check(
            "claude_marketplace",
            "fail",
            f"plugins[].source={source!r} must start with './' (Claude Code rejects bare '.' as Invalid input)",
        )

    metadata = marketplace.get("metadata")
    if not isinstance(metadata, dict) or not str(metadata.get("description") or "").strip():
        # claude plugin validate warns when metadata.description is missing.
        return _doctor_check(
            "claude_marketplace",
            "warn",
            f"{marketplace_path}: missing metadata.description (claude plugin validate warns)",
        )

    return _doctor_check(
        "claude_marketplace",
        "ok",
        f"{marketplace_path} -> name={marketplace.get('name')!r} source={source!r} metadata.description set",
    )


def _check_claude_manifest(plugin_root: Path) -> DoctorCheck:
    manifest_path = plugin_root / ".claude-plugin" / "plugin.json"
    try:
        manifest = _read_json_file(manifest_path)
    except Exception as exc:
        return _doctor_check("claude_manifest", "fail", f"{manifest_path}: {exc}")

    if not isinstance(manifest, dict):
        return _doctor_check("claude_manifest", "fail", f"{manifest_path}: top-level must be an object")
    for field in ("name", "version", "description"):
        if not str(manifest.get(field) or "").strip():
            return _doctor_check("claude_manifest", "fail", f"missing or empty `{field}`")

    mcp_servers = manifest.get("mcpServers")
    if mcp_servers is None:
        # mcpServers is optional in the Claude Code schema; without it the plugin
        # install will not expose any MCP server (see MF #2a manifest fix).
        return _doctor_check(
            "claude_manifest",
            "warn",
            f"{manifest_path}: no `mcpServers` declared; plugin install will not expose an MCP server",
        )

    if isinstance(mcp_servers, str):
        if not mcp_servers.strip():
            return _doctor_check("claude_manifest", "fail", "`mcpServers` path is empty")
    elif isinstance(mcp_servers, dict):
        if not mcp_servers:
            return _doctor_check("claude_manifest", "fail", "`mcpServers` object is empty")
        for server_name, spec in mcp_servers.items():
            if not isinstance(spec, dict):
                return _doctor_check(
                    "claude_manifest",
                    "fail",
                    f"mcpServers[{server_name!r}] must be an object",
                )
            command = str(spec.get("command") or "").strip()
            if not command:
                return _doctor_check(
                    "claude_manifest",
                    "fail",
                    f"mcpServers[{server_name!r}].command must be non-empty",
                )
            args = spec.get("args")
            if not isinstance(args, list):
                return _doctor_check(
                    "claude_manifest",
                    "fail",
                    f"mcpServers[{server_name!r}].args must be a list",
                )
    else:
        return _doctor_check(
            "claude_manifest",
            "fail",
            "`mcpServers` must be a path string or an object map of server specs",
        )

    return _doctor_check(
        "claude_manifest",
        "ok",
        f"{manifest_path} -> name={manifest.get('name')!r} mcpServers declared",
    )


def _check_mcp_config(plugin_root: Path) -> DoctorCheck:
    path = plugin_root / ".mcp.json"
    try:
        payload = _read_json_file(path)
    except Exception as exc:
        return _doctor_check("mcp_config", "fail", f"{path}: {exc}")
    servers = payload.get("mcpServers") if isinstance(payload, dict) else {}
    if not isinstance(servers, dict) or "aming-claw" not in servers:
        return _doctor_check("mcp_config", "fail", "missing mcpServers.aming-claw")
    return _doctor_check("mcp_config", "ok", str(path))


def _load_toml_text(text: str) -> dict:
    try:
        import tomllib  # type: ignore[import-not-found]

        return tomllib.loads(text)
    except ImportError:
        try:
            import pip._vendor.tomli as tomli  # type: ignore[import-not-found]

            return tomli.loads(text)
        except (ImportError, AttributeError):
            import tomli  # type: ignore[import-not-found]

            return tomli.loads(text)


def _extract_toml_table(text: str, table_name: str) -> str:
    text = text.lstrip("\ufeff")
    match = _toml_table_pattern(table_name).search(text)
    return match.group(0) if match else ""


def _extract_toml_string(block: str, key: str) -> str:
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*(['\"])(.*?)\1\s*$", block)
    if not match:
        return ""
    value = match.group(2)
    if match.group(1) == '"':
        value = value.replace("\\\\", "\\").replace('\\"', '"')
    return value


def _validate_mcp_runtime_entrypoint(mcp_path: Path) -> tuple[bool, str]:
    try:
        payload = _read_json_file(mcp_path)
    except Exception as exc:
        return False, f"{mcp_path}: {exc}"
    servers = payload.get("mcpServers") if isinstance(payload, dict) else {}
    server = servers.get("aming-claw") if isinstance(servers, dict) else None
    if not isinstance(server, dict):
        return False, f"{mcp_path}: missing mcpServers.aming-claw"
    command = str(server.get("command") or "").strip()
    if not command:
        return False, f"{mcp_path}: mcpServers.aming-claw.command is empty"
    args = server.get("args")
    if not isinstance(args, list) or "agent.mcp.server" not in [str(arg) for arg in args]:
        return False, f"{mcp_path}: mcpServers.aming-claw.args must launch agent.mcp.server"

    cwd = Path(str(server.get("cwd") or ".")).expanduser()
    if not cwd.is_absolute():
        cwd = (mcp_path.parent / cwd).resolve()
    runtime_paths = [cwd]
    env = server.get("env") if isinstance(server.get("env"), dict) else {}
    for item in str(env.get("PYTHONPATH") or "").split(os.pathsep):
        if item:
            runtime_paths.append(Path(item).expanduser())
    for candidate in runtime_paths:
        if (candidate / "agent" / "mcp" / "server.py").is_file():
            return True, f"{mcp_path}: runtime import root {candidate}"
    checked = ", ".join(str(path) for path in runtime_paths)
    return False, f"{mcp_path}: cannot import agent.mcp.server from cwd/PYTHONPATH ({checked})"


def _validate_codex_marketplace_root(root: Path) -> tuple[bool, str]:
    marketplace_path = root / ".agents" / "plugins" / "marketplace.json"
    try:
        marketplace = _read_json_file(marketplace_path)
    except Exception as exc:
        return False, f"{marketplace_path}: {exc}"
    plugins = marketplace.get("plugins") if isinstance(marketplace, dict) else []
    match = next(
        (
            item
            for item in plugins or []
            if isinstance(item, dict) and item.get("name") == CODEX_PLUGIN_NAME
        ),
        None,
    )
    if not match:
        return False, f"{marketplace_path}: missing plugin entry `{CODEX_PLUGIN_NAME}`"
    source = match.get("source") if isinstance(match.get("source"), dict) else {}
    raw_path = str(source.get("path") or "").strip()
    if not raw_path:
        return False, f"{marketplace_path}: missing source.path"
    marketplace_root = marketplace_path.parent.resolve()
    resolved = (marketplace_root / raw_path).resolve()
    if not _is_relative_to(resolved, marketplace_root):
        return False, f"{marketplace_path}: source.path {raw_path!r} escapes marketplace root"
    if not (resolved / ".codex-plugin" / "plugin.json").is_file():
        return False, f"{marketplace_path}: source.path {raw_path!r} has no .codex-plugin/plugin.json"
    mcp_ok, mcp_detail = _validate_mcp_runtime_entrypoint(resolved / ".mcp.json")
    if not mcp_ok:
        return False, mcp_detail
    return True, f"{marketplace_path} -> {resolved}; {mcp_detail}"


def _codex_plugin_enabled(text: str) -> bool:
    block = _extract_toml_table(text, f'plugins."{CODEX_PLUGIN_ID}"')
    return bool(re.search(r"(?m)^\s*enabled\s*=\s*true\s*$", block, flags=re.IGNORECASE))


def _check_codex_config(path: Path) -> DoctorCheck:
    if not path.is_file():
        return _doctor_check(
            "codex_config",
            "warn",
            f"{path} not found; run `aming-claw plugin install` to enable {CODEX_PLUGIN_ID}",
        )
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        parsed = _load_toml_text(text)
    except Exception as exc:
        return _doctor_check("codex_config", "fail", f"{path}: invalid TOML ({exc})")

    plugins = parsed.get("plugins") if isinstance(parsed, dict) else {}
    plugin_table = plugins.get(CODEX_PLUGIN_ID) if isinstance(plugins, dict) else {}
    plugin_enabled = isinstance(plugin_table, dict) and plugin_table.get("enabled") is True
    marketplaces = parsed.get("marketplaces") if isinstance(parsed, dict) else {}
    marketplace_table = marketplaces.get(CODEX_MARKETPLACE_NAME) if isinstance(marketplaces, dict) else {}
    marketplace_source = str(marketplace_table.get("source") or "") if isinstance(marketplace_table, dict) else ""
    if plugin_enabled and marketplace_source:
        ok, detail = _validate_codex_marketplace_root(Path(marketplace_source).expanduser())
        if not ok:
            return _doctor_check("codex_config", "fail", f"{path}: {detail}")
        return _doctor_check("codex_config", "ok", f"{path} enables {CODEX_PLUGIN_ID}; {detail}")
    if plugin_enabled:
        return _doctor_check(
            "codex_config",
            "ok",
            f"{path} enables {CODEX_PLUGIN_ID}; no marketplace source configured, so Codex relies on installed cache",
        )
    return _doctor_check(
        "codex_config",
        "warn",
        f"{path} exists but {CODEX_PLUGIN_ID} is not enabled",
    )


def _check_codex_cache(plugin_root: Path, *, codex_home: Optional[Union[Path, str]] = None, codex_config: Optional[Path] = None) -> DoctorCheck:
    home = Path(codex_home).expanduser() if codex_home else (
        codex_config.parent if codex_config else default_codex_home()
    )
    try:
        cache_root = codex_cache_plugin_root(plugin_root, codex_home=home)
    except Exception as exc:
        return _doctor_check("codex_plugin_cache", "fail", f"cannot compute cache path: {exc}")
    manifest = cache_root / ".codex-plugin" / "plugin.json"
    if manifest.is_file():
        mcp_ok, mcp_detail = _validate_mcp_runtime_entrypoint(cache_root / ".mcp.json")
        if not mcp_ok:
            return _doctor_check("codex_plugin_cache", "fail", mcp_detail)
        return _doctor_check("codex_plugin_cache", "ok", f"{manifest}; {mcp_detail}")
    return _doctor_check(
        "codex_plugin_cache",
        "fail",
        f"missing installed plugin cache at {cache_root}; run `aming-claw plugin install`",
    )


def _check_governance(governance_url: str) -> DoctorCheck:
    url = governance_url.rstrip("/") + "/api/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return _doctor_check("governance_health", "warn", f"{url}: {exc}")
    if payload.get("status") == "ok" or payload.get("ok") is True:
        return _doctor_check("governance_health", "ok", f"{url} status ok")
    return _doctor_check("governance_health", "warn", f"{url} returned {payload}")


def _check_dashboard_assets(plugin_root: Path) -> DoctorCheck:
    candidates = [
        plugin_root / "agent" / "governance" / "dashboard_dist" / "index.html",
        plugin_root / "frontend" / "dashboard" / "dist" / "index.html",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return _doctor_check("dashboard_static_assets", "ok", str(candidate))
    return _doctor_check(
        "dashboard_static_assets",
        "warn",
        "missing dashboard index; installed plugins should include agent/governance/dashboard_dist/index.html. In a raw checkout, run `cd frontend/dashboard && npm install && npm run build`.",
    )


def _check_self_graph_bundle(plugin_root: Path) -> DoctorCheck:
    status = _self_graph_bundle_status(plugin_root=plugin_root)
    detail_parts = [
        f"major={status.get('bundle_major', '-')}",
        f"supported={status.get('supported_bundle_major', '-')}",
    ]
    events = status.get("events") or []
    if events:
        detail_parts.append(
            "event=" + ",".join(str(event.get("event_type") or "unknown") for event in events)
        )
    messages = (status.get("blockers") or []) + (status.get("warnings") or [])
    if messages:
        detail_parts.append("; ".join(str(item) for item in messages))
    check_status = "ok" if status.get("status") == "pass" else str(status.get("status") or "warn")
    return _doctor_check("self_graph_bundle", check_status, "; ".join(detail_parts))


def _check_dashboard_route(governance_url: str) -> DoctorCheck:
    url = governance_url.rstrip("/") + "/dashboard"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            status = getattr(response, "status", response.getcode())
    except urllib.error.HTTPError as exc:
        return _doctor_check(
            "dashboard_http_route",
            "warn",
            f"{url} returned HTTP {exc.code}; root `/` is not the dashboard",
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _doctor_check("dashboard_http_route", "warn", f"{url}: {exc}")
    if status == 200:
        return _doctor_check("dashboard_http_route", "ok", f"{url} returned 200")
    return _doctor_check("dashboard_http_route", "warn", f"{url} returned HTTP {status}")


def _check_manager_health(manager_url: str = "http://127.0.0.1:40101") -> DoctorCheck:
    url = manager_url.rstrip("/") + "/api/manager/health"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return _doctor_check(
            "service_manager_health",
            "warn",
            f"{url}: {exc}; governance/dashboard can still be usable, chain/executor is degraded",
        )
    if payload.get("ok") is True:
        version = payload.get("runtime_version") or ""
        detail = f"{url} ok"
        if version:
            detail += f", runtime_version={version}"
        return _doctor_check("service_manager_health", "ok", detail)
    return _doctor_check("service_manager_health", "warn", f"{url} returned {payload}")


def _check_ai_cli(provider: str, requirement: dict[str, str]) -> DoctorCheck:
    env_var = requirement["env_var"]
    configured = os.environ.get(env_var, "").strip()
    command = configured or requirement["command"]
    resolved = command if os.path.isabs(command) else shutil.which(command)
    label = requirement["runtime"]
    if not resolved:
        return _doctor_check(
            f"ai_cli_{provider}",
            "warn",
            f"{label} missing; expected `{requirement['command']}` or {env_var}",
        )
    try:
        proc = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        return _doctor_check(
            f"ai_cli_{provider}",
            "warn",
            f"{label} found at {resolved}, but version probe failed: {exc}",
        )
    version = (proc.stdout or proc.stderr or "").strip().splitlines()[0:1]
    if proc.returncode != 0:
        return _doctor_check(
            f"ai_cli_{provider}",
            "warn",
            f"{label} found at {resolved}, but version probe exited {proc.returncode}",
        )
    suffix = f", version {version[0]}" if version else ""
    return _doctor_check(
        f"ai_cli_{provider}",
        "ok",
        f"{label}: detected at {resolved}{suffix}, auth unknown",
    )


def doctor_plugin(
    *,
    plugin_root: Optional[Union[Path, str]] = None,
    governance_url: str = "http://localhost:40000",
    codex_config: Optional[Union[Path, str]] = None,
    codex_home: Optional[Union[Path, str]] = None,
    check_governance: bool = True,
    python_executable: Optional[str] = None,
) -> DoctorResult:
    """Run read-only aftercare checks for a local plugin install."""

    root = Path(plugin_root).expanduser().resolve() if plugin_root else _default_doctor_root()
    result = DoctorResult(plugin_root=str(root), governance_url=governance_url)

    try:
        validated = validate_plugin_root(root)
        result.checks.append(_doctor_check("plugin_assets", "ok", ", ".join(validated)))
    except PluginInstallError as exc:
        result.checks.append(_doctor_check("plugin_assets", "fail", str(exc)))

    result.checks.append(_python_version_check(python_executable or sys.executable))
    result.checks.append(_check_codex_manifest(root))
    result.checks.append(_check_marketplace(root))
    result.checks.append(_check_claude_manifest(root))
    result.checks.append(_check_claude_marketplace(root))
    result.checks.append(_check_mcp_config(root))
    codex_config_path = Path(codex_config).expanduser() if codex_config else default_codex_config_path()
    result.checks.append(_check_codex_config(codex_config_path))
    result.checks.append(_check_codex_cache(root, codex_home=codex_home, codex_config=codex_config_path))
    result.checks.append(_check_dashboard_assets(root))
    result.checks.append(_check_self_graph_bundle(root))
    for provider, requirement in AI_CLI_REQUIREMENTS.items():
        result.checks.append(_check_ai_cli(provider, requirement))
    if check_governance:
        result.checks.append(_check_governance(governance_url))
        result.checks.append(_check_dashboard_route(governance_url))
        result.checks.append(_check_manager_health(os.environ.get("MANAGER_URL", "http://127.0.0.1:40101")))

    result.manual_steps.extend(
        [
            "Restart/reload Codex or open a new session after installing the plugin; existing threads may not hot-load new skills/MCP tools.",
            "In the new session, confirm the Aming Claw skill is visible and mcp__aming_claw tools are available.",
            "Remember: `aming-claw start` only starts governance; it does not prove plugin loading, dashboard assets, ServiceManager, executor, or AI auth.",
        ]
    )
    return result


def clone_or_update(
    repo_url: str,
    plugin_root: Path,
    *,
    ref: str = "",
    dry_run: bool = False,
    commands: Optional[list[CommandRecord]] = None,
) -> None:
    root = plugin_root.expanduser().resolve()
    git_dir = root / ".git"
    if git_dir.is_dir():
        _run(["git", "fetch", "--all", "--prune"], cwd=root, dry_run=dry_run, commands=commands)
        if ref:
            _run(["git", "checkout", ref], cwd=root, dry_run=dry_run, commands=commands)
        _run(["git", "pull", "--ff-only"], cwd=root, dry_run=dry_run, commands=commands)
        return

    if root.exists() and any(root.iterdir()):
        raise PluginInstallError(f"install target exists and is not a git checkout: {root}")

    if not dry_run:
        root.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", repo_url, str(root)], dry_run=dry_run, commands=commands)
    if ref:
        _run(["git", "checkout", ref], cwd=root, dry_run=dry_run, commands=commands)


def build_next_steps(plugin_root: Path, python_executable: str) -> list[str]:
    root = str(plugin_root)
    return [
        f"Codex: plugin cache and config are installed for `{CODEX_PLUGIN_ID}`; reload Codex or open a new session, then confirm the Aming Claw skill/MCP tools are visible.",
        f"Claude Code: /plugin marketplace add {root}",
        "Claude Code: /plugin install aming-claw@aming-claw-local",
        f"Verify install: {python_executable} -m agent.cli plugin doctor --plugin-root {root}",
        "Start services in a separate terminal/window; this is a long-running command:",
        f"  cd {root}",
        f"  {python_executable} -m agent.cli start --workspace {root}",
        "Do not wait for the start command to exit; verify with `aming-claw status` or the dashboard.",
        "Dashboard: http://localhost:40000/dashboard",
    ]


def format_plugin_update_state_status(result: dict[str, Any]) -> str:
    lines = [
        "Aming Claw plugin update state",
        f"  state path:    {result.get('state_path', '')}",
        f"  overall:       {result.get('status', 'unknown')}",
        f"  update status: {result.get('update_status', 'unknown')}",
    ]
    blockers = result.get("blockers") or []
    warnings = result.get("warnings") or []
    if blockers:
        lines.append("")
        lines.append("Blockers:")
        lines.extend(f"  - {item}" for item in blockers)
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"  - {item}" for item in warnings)
    state = result.get("state") if isinstance(result.get("state"), dict) else {}
    restart_required = state.get("restart_required") if isinstance(state, dict) else {}
    if restart_required:
        lines.append("")
        lines.append("Restart obligations:")
        for component in PLUGIN_RESTART_COMPONENTS:
            info = restart_required.get(component) or {}
            required = "required" if info.get("required") else "not required"
            satisfied = "satisfied" if info.get("satisfied") else "unsatisfied"
            suffix = f", {satisfied}" if info.get("required") else ""
            reason = f" - {info.get('reason')}" if info.get("reason") else ""
            lines.append(f"  - {component}: {required}{suffix}{reason}")
    bundle = result.get("self_graph_bundle") if isinstance(result.get("self_graph_bundle"), dict) else {}
    if bundle:
        lines.append("")
        lines.append("Self graph bundle:")
        lines.append(f"  status:          {bundle.get('status', 'unknown')}")
        lines.append(f"  bundle major:    {bundle.get('bundle_major', '-')}")
        lines.append(f"  supported major: {bundle.get('supported_bundle_major', '-')}")
        for event in bundle.get("events") or []:
            reason = event.get("reason", "plugin_update_reminder")
            lines.append(f"  event:           {event.get('event_type', 'unknown')} ({reason})")
    return "\n".join(lines)


def install_from_git(
    repo_url: str = DEFAULT_REPO_URL,
    *,
    install_root: Optional[Union[Path, str]] = None,
    ref: str = "",
    python_executable: Optional[str] = None,
    install_package: bool = True,
    install_codex_plugin: bool = True,
    codex_home: Optional[Union[Path, str]] = None,
    codex_config: Optional[Union[Path, str]] = None,
    codex_marketplace_root: Optional[Union[Path, str]] = None,
    start: bool = False,
    dry_run: bool = False,
    validate_only: bool = False,
) -> InstallResult:
    """Clone/update a plugin checkout, validate it, and optionally install runtime."""

    root = Path(install_root).expanduser() if install_root else default_install_root()
    plugin_root = plugin_root_for(repo_url, root)
    python = python_executable or sys.executable
    commands: list[CommandRecord] = []

    if not validate_only:
        clone_or_update(repo_url, plugin_root, ref=ref, dry_run=dry_run, commands=commands)

    validated: list[str] = []
    if dry_run and not plugin_root.exists():
        # Network-free dry-runs are allowed to plan a fresh clone without
        # validating files that do not exist yet.
        validated = []
    else:
        validated = validate_plugin_root(plugin_root)

    installed_package = False
    if install_package:
        if not dry_run:
            _ensure_supported_python(python)
        _run(
            [python, "-m", "pip", "install", "-e", str(plugin_root)],
            dry_run=dry_run,
            commands=commands,
        )
        installed_package = not dry_run

    codex_cache_path = ""
    codex_marketplace_path = ""
    codex_config_path = ""
    installed_codex_plugin = False
    if install_codex_plugin and not validate_only:
        cache_target = install_codex_plugin_cache(
            plugin_root,
            codex_home=codex_home,
            python_executable=python,
            dry_run=dry_run,
            commands=commands,
        )
        marketplace_target = install_codex_marketplace(
            plugin_root,
            marketplace_root=codex_marketplace_root,
            python_executable=python,
            dry_run=dry_run,
            commands=commands,
        )
        config_target = configure_codex_plugin(
            codex_config=codex_config,
            marketplace_root=marketplace_target,
            dry_run=dry_run,
            commands=commands,
        )
        codex_cache_path = str(cache_target)
        codex_marketplace_path = str(marketplace_target)
        codex_config_path = str(config_target)
        installed_codex_plugin = not dry_run

    started = False
    if start:
        _spawn_long_running(
            [python, "-m", "agent.cli", "start"],
            cwd=plugin_root,
            dry_run=dry_run,
            commands=commands,
        )
        started = not dry_run

    plugin_state_path = ""
    if not validate_only:
        state_target = write_plugin_update_state(
            plugin_root=plugin_root,
            repo_url=repo_url,
            ref=ref,
            update_status="current",
            dry_run=dry_run,
        )
        plugin_state_path = str(state_target)

    return InstallResult(
        repo_url=repo_url,
        install_root=str(root.expanduser().resolve()),
        plugin_root=str(plugin_root),
        dry_run=dry_run,
        installed_package=installed_package,
        installed_codex_plugin=installed_codex_plugin,
        codex_home=str(Path(codex_home).expanduser() if codex_home else default_codex_home()),
        codex_cache_path=codex_cache_path,
        codex_marketplace_root=codex_marketplace_path,
        codex_config_path=codex_config_path,
        plugin_state_path=plugin_state_path,
        started=started,
        validated_files=validated,
        commands=commands,
        next_steps=build_next_steps(plugin_root, python),
    )


def format_result(result: InstallResult) -> str:
    lines = [
        "Aming Claw plugin bootstrap",
        f"  repo:        {result.repo_url}",
        f"  plugin root: {result.plugin_root}",
        f"  dry run:     {str(result.dry_run).lower()}",
    ]
    if result.commands:
        lines.append("")
        lines.append("Commands:")
        for command in result.commands:
            prefix = "  would run:" if command.skipped else "  ran:"
            cwd = f" (cwd={command.cwd})" if command.cwd else ""
            lines.append(f"{prefix} {_command_text(command.args)}{cwd}")
    if result.validated_files:
        lines.append("")
        lines.append("Validated plugin assets:")
        lines.extend(f"  - {rel}" for rel in result.validated_files)
    if result.codex_cache_path or result.codex_config_path:
        lines.append("")
        lines.append("Codex plugin install:")
        if result.codex_cache_path:
            lines.append(f"  cache:       {result.codex_cache_path}")
        if result.codex_marketplace_root:
            lines.append(f"  marketplace: {result.codex_marketplace_root}")
        if result.codex_config_path:
            lines.append(f"  config:      {result.codex_config_path}")
    if result.plugin_state_path:
        lines.append("")
        lines.append(f"Plugin state: {result.plugin_state_path}")
    lines.append("")
    lines.append("Next steps:")
    lines.extend(f"  {step}" for step in result.next_steps)
    return "\n".join(lines)


def format_plugin_update_result(result: PluginUpdateResult) -> str:
    lines = [
        "Aming Claw plugin update",
        f"  repo:        {result.repo_url}",
        f"  plugin root: {result.plugin_root}",
        f"  action:      {result.action}",
        f"  status:      {result.status}",
        f"  dry run:     {str(result.dry_run).lower()}",
    ]
    if result.installed_commit or result.remote_commit:
        lines.append("")
        lines.append("Commits:")
        if result.installed_commit:
            lines.append(f"  installed: {result.installed_commit}")
        if result.remote_commit:
            lines.append(f"  remote:    {result.remote_commit}")
    if result.error:
        lines.append("")
        lines.append(f"Error: {result.error}")
    if result.commands:
        lines.append("")
        lines.append("Commands:")
        for command in result.commands:
            prefix = "  would run:" if command.skipped else "  ran:"
            cwd = f" (cwd={command.cwd})" if command.cwd else ""
            lines.append(f"{prefix} {_command_text(command.args)}{cwd}")
    if result.changed_files:
        lines.append("")
        lines.append("Changed files:")
        lines.extend(f"  - {path}" for path in result.changed_files[:25])
        if len(result.changed_files) > 25:
            lines.append(f"  - ... {len(result.changed_files) - 25} more")
    if result.changed_surfaces:
        lines.append("")
        lines.append("Changed surfaces:")
        lines.extend(f"  - {surface}" for surface in result.changed_surfaces)
    if result.restart_required:
        lines.append("")
        lines.append("Restart obligations:")
        for component in PLUGIN_RESTART_COMPONENTS:
            info = result.restart_required.get(component) or {}
            required = "required" if info.get("required") else "not required"
            suffix = f" - {info.get('reason')}" if info.get("reason") else ""
            lines.append(f"  - {component}: {required}{suffix}")
    if result.plugin_state_path:
        lines.append("")
        lines.append(f"Plugin state: {result.plugin_state_path}")
    if result.next_steps:
        lines.append("")
        lines.append("Next steps:")
        lines.extend(f"  {step}" for step in result.next_steps)
    return "\n".join(lines)


def format_doctor_result(result: DoctorResult) -> str:
    lines = [
        "Aming Claw plugin doctor",
        f"  plugin root:    {result.plugin_root}",
        f"  governance url: {result.governance_url}",
        f"  overall:        {'ok' if result.ok else 'needs attention'}",
        "",
        "Checks:",
    ]
    for check in result.checks:
        detail = f" - {check.detail}" if check.detail else ""
        lines.append(f"  [{check.status}] {check.name}{detail}")
    if result.manual_steps:
        lines.append("")
        lines.append("Manual aftercare:")
        lines.extend(f"  - {step}" for step in result.manual_steps)
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install the Aming Claw local plugin/runtime from a Git URL."
    )
    parser.add_argument("repo_url", nargs="?", default=DEFAULT_REPO_URL)
    parser.add_argument("--install-root", default="", help="User-local plugin cache root.")
    parser.add_argument("--ref", default="", help="Optional branch, tag, or commit to checkout.")
    parser.add_argument("--python", default=sys.executable, help="Python executable for pip/start commands.")
    parser.add_argument("--no-pip", action="store_true", help="Clone and validate only; do not pip install.")
    parser.add_argument("--no-codex-install", action="store_true", help="Do not install Codex plugin cache/config.")
    parser.add_argument("--codex-home", default="", help="Override Codex home for plugin cache/config.")
    parser.add_argument("--codex-config", default="", help="Override Codex config.toml path.")
    parser.add_argument("--codex-marketplace-root", default="", help="Override generated Codex marketplace root.")
    parser.add_argument("--start", action="store_true", help="Run the start command after install.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned commands without changing state.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate the computed checkout path without cloning or fetching.",
    )
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        result = install_from_git(
            args.repo_url,
            install_root=args.install_root or None,
            ref=args.ref,
            python_executable=args.python,
            install_package=not args.no_pip,
            install_codex_plugin=not args.no_codex_install,
            codex_home=args.codex_home or None,
            codex_config=args.codex_config or None,
            codex_marketplace_root=args.codex_marketplace_root or None,
            start=args.start,
            dry_run=args.dry_run,
            validate_only=args.validate_only,
        )
    except PluginInstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(format_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
