"""CLI entry point for aming-claw.

Usage:
    aming-claw init            - create .aming-claw.yaml in current directory
    aming-claw bootstrap       - bootstrap an external project
    aming-claw status          - show governance status
    aming-claw plugin install  - install/update plugin assets from Git
    aming-claw plugin update   - check/apply plugin updates from Git
    aming-claw backlog export  - export portable backlog JSON
    aming-claw backlog import  - import portable backlog JSON
    aming-claw start           - start governance in the foreground
    aming-claw open            - open the dashboard URL
    aming-claw launcher        - write a local launcher HTML artifact
    aming-claw run-executor    - start executor worker
    aming-claw observer run    - build or execute route-bound observer invocation
    aming-claw mf precommit-check - run MF pre-commit guards
    aming-claw mf dispatch-gate - validate MF subagent dispatch evidence
"""

import os
import sys
import logging
import json
import webbrowser
import socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

try:
    import click
except ImportError:
    # Provide a helpful error when click isn't installed
    print("Error: 'click' package is required. Install with: pip install click", file=sys.stderr)
    sys.exit(1)

log = logging.getLogger(__name__)

DEFAULT_GOVERNANCE_URL = "http://localhost:40000"

_YAML_TEMPLATE = """\
# aming-claw project configuration
project_id: ""
workspace_path: "."
governance_port: 40000
notification_backend: "telegram"
redis_url: "redis://localhost:6379/0"
max_workers: 4
db_path: ""
"""


@click.group()
@click.version_option(package_name="aming-claw")
def main():
    """aming-claw - governance-driven workflow platform."""
    pass


@main.command()
def init():
    """Initialize project: create .aming-claw.yaml in the current directory."""
    target = os.path.join(os.getcwd(), ".aming-claw.yaml")
    if os.path.exists(target):
        click.echo(f".aming-claw.yaml already exists at {target}")
        return
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(_YAML_TEMPLATE)
    click.echo(f"Created {target}")


@main.command()
@click.option("--path", default=".", help="Workspace path to bootstrap")
@click.option("--name", default="", help="Project name")
def bootstrap(path, name):
    """Bootstrap an external project into aming-claw governance."""
    from agent.governance.project_service import bootstrap_project
    result = bootstrap_project(workspace_path=path, project_name=name)
    click.echo(f"Bootstrap result: {result}")


@main.command("scan")
@click.option("--path", default=".", help="External project path to scan")
@click.option("--project-id", default="", help="Governance project id")
@click.option("--session-id", default="", help="Optional deterministic scan session id")
def scan(path, project_id, session_id):
    """Scan an external project into a local .aming-claw candidate workspace."""
    from agent.governance.external_project_governance import scan_external_project

    result = scan_external_project(
        path,
        project_id=project_id or None,
        session_id=session_id or None,
    )
    click.echo(json.dumps(result, indent=2, sort_keys=True))


@main.command()
def status():
    """Show governance service status."""
    from agent.config import AmingConfig
    import requests as _requests
    cfg = AmingConfig.load()
    url = f"http://localhost:{cfg.governance_port}/api/health"
    try:
        resp = _requests.get(url, timeout=5)
        click.echo(resp.json())
    except Exception as exc:
        click.echo(f"Governance unreachable: {exc}", err=True)
        sys.exit(1)


def _dashboard_url(governance_url: str) -> str:
    return governance_url.rstrip("/") + "/dashboard"


def _default_runtime_workspace() -> Path:
    """Return the plugin/runtime root used for local governance state."""
    return Path(__file__).resolve().parents[1]


def _probe_governance(port: int, *, timeout: float = 2.0) -> Optional[dict]:
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - localhost probe
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _http_json(method: str, url: str, payload: dict | None = None, *, timeout: float = 30.0) -> tuple[int, dict]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - local governance URL by default
            body = resp.read().decode("utf-8")
            parsed = json.loads(body) if body else {}
            return resp.status, parsed if isinstance(parsed, dict) else {"ok": False, "error": "non_object_response"}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {"ok": False, "error": "http_error", "message": body}
        if not isinstance(parsed, dict):
            parsed = {"ok": False, "error": "http_error", "message": body}
        return exc.code, parsed


def _port_is_open(port: int, *, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _port_owner_hint(port: int) -> str:
    if sys.platform.startswith("win"):
        try:
            proc = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except Exception:
            return ""
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
                if parts[1].endswith(f":{port}"):
                    return f" PID={parts[-1]}"
        return ""
    try:
        proc = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return ""
    pid = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    return f" PID={pid}" if pid else ""


def _launcher_html(governance_url: str) -> str:
    dashboard_url = _dashboard_url(governance_url)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Aming Claw Launcher</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #172033; }}
    main {{ max-width: 760px; }}
    a.button {{ display: inline-block; padding: 10px 14px; border: 1px solid #b6c7e6; border-radius: 6px; text-decoration: none; color: #0f3d7a; background: #f6f9ff; }}
    code {{ background: #f2f5fa; padding: 2px 5px; border-radius: 4px; }}
    pre {{ background: #0f172a; color: #e2e8f0; padding: 14px; border-radius: 6px; overflow: auto; }}
  </style>
</head>
<body>
  <main>
    <h1>Aming Claw Launcher</h1>
    <p>This local launcher never starts governance automatically. Start services explicitly, then open the dashboard.</p>
    <p><a class="button" href="{dashboard_url}">Open dashboard</a></p>
    <h2>Start locally</h2>
    <pre>aming-claw start</pre>
    <p>If the console script is not on PATH yet, use:</p>
    <pre>python -m agent.cli start</pre>
    <h2>Install/update plugin from Git</h2>
    <pre>aming-claw plugin install https://github.com/amingclawdev/aming-claw</pre>
    <h2>Check status</h2>
    <pre>aming-claw status</pre>
    <p>Codex and Claude Code should connect through the project <code>.mcp.json</code> after governance is available at <code>{governance_url}</code>.</p>
  </main>
</body>
</html>
"""


@main.command()
@click.option(
    "--workspace",
    default="",
    help="Runtime workspace root for shared-volume/project state. Defaults to the plugin runtime root, not the current project.",
)
@click.option("--port", default=40000, type=int, help="Governance HTTP port.")
def start(workspace, port):
    """Start governance in the foreground without spawning plugin-owned workers."""
    health = _probe_governance(port)
    if health and health.get("status") == "ok" and health.get("service") == "governance":
        dashboard = _dashboard_url(f"http://localhost:{port}")
        version = health.get("version") or health.get("runtime_version") or "unknown"
        click.echo(f"Governance already running on port {port} (version {version}).")
        click.echo(f"Dashboard: {dashboard}")
        return
    if _port_is_open(port):
        owner = _port_owner_hint(port)
        raise click.ClickException(
            f"Port {port} is already in use{owner}, but /api/health is not Aming Claw governance. "
            "Stop that process or choose a different --port."
        )
    os.environ["GOVERNANCE_PORT"] = str(port)
    runtime_workspace = Path(workspace).resolve() if workspace else _default_runtime_workspace()
    os.environ["AMING_CLAW_HOME"] = str(runtime_workspace)
    os.environ.setdefault("SHARED_VOLUME_PATH", str(runtime_workspace / "shared-volume"))
    import start_governance

    start_governance.main(workspace_root=runtime_workspace)


@main.command("open")
@click.option("--governance-url", default=DEFAULT_GOVERNANCE_URL, help="Governance service base URL.")
def open_dashboard(governance_url):
    """Open the dashboard in the default browser."""
    url = _dashboard_url(governance_url)
    webbrowser.open(url)
    click.echo(url)


@main.command()
@click.option("--governance-url", default=DEFAULT_GOVERNANCE_URL, help="Governance service base URL.")
@click.option("--output", default="", help="Output HTML path. Defaults to .aming-claw/aming-claw-launcher.html.")
@click.option("--open-browser", is_flag=True, help="Open the generated launcher in the default browser.")
def launcher(governance_url, output, open_browser):
    """Write a local launcher HTML artifact with dashboard links and start commands."""
    target = Path(output) if output else Path.cwd() / ".aming-claw" / "aming-claw-launcher.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_launcher_html(governance_url), encoding="utf-8")
    if open_browser:
        webbrowser.open(target.resolve().as_uri())
    click.echo(str(target))


@main.command("run-executor")
def run_executor():
    """Start the executor worker."""
    from agent.executor_worker import main as worker_main
    worker_main()


@main.group()
def backlog():
    """Export and import portable backlog data."""
    pass


@backlog.command("export")
@click.option("--project-id", default="aming-claw", help="Governance project id.")
@click.option("--governance-url", default=DEFAULT_GOVERNANCE_URL, help="Governance service base URL.")
@click.option("--output", default="", help="Output JSON path. Prints JSON to stdout when omitted.")
@click.option("--status", default="", help="Optional backlog status filter, e.g. OPEN or FIXED.")
@click.option("--priority", default="", help="Optional priority filter, e.g. P1.")
@click.option("--bug-id", "bug_ids", multiple=True, help="Optional bug id to export. Can be repeated.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON even when --output is used.")
def backlog_export(project_id, governance_url, output, status, priority, bug_ids, json_output):
    """Export backlog rows as portable JSON."""
    query = {
        key: value
        for key, value in {
            "status": status,
            "priority": priority,
            "bug_id": ",".join(bug_ids),
        }.items()
        if value
    }
    qs = f"?{urllib.parse.urlencode(query)}" if query else ""
    url = f"{governance_url.rstrip('/')}/api/backlog/{urllib.parse.quote(project_id, safe='')}/portable/export{qs}"
    code, payload = _http_json("GET", url)
    if code >= 400 or payload.get("ok") is False:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        raise click.exceptions.Exit(1)

    if output:
        target = Path(output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if json_output or not output:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        click.echo(f"Exported {payload.get('row_count', 0)} backlog row(s) to {output}")


@backlog.command("import")
@click.option("--project-id", default="aming-claw", help="Governance project id.")
@click.option("--governance-url", default=DEFAULT_GOVERNANCE_URL, help="Governance service base URL.")
@click.option("--input", "input_path", required=True, help="Input JSON path, or '-' for stdin.")
@click.option("--on-conflict", default="skip", type=click.Choice(["skip", "overwrite", "fail"]), help="How to handle existing bug ids.")
@click.option("--dry-run", is_flag=True, help="Validate and report planned changes without writing rows.")
@click.option("--actor", default="cli", help="Actor recorded in the import result.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def backlog_import_cmd(project_id, governance_url, input_path, on_conflict, dry_run, actor, json_output):
    """Import portable backlog JSON into a governance project."""
    try:
        raw = sys.stdin.read() if input_path == "-" else Path(input_path).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"Cannot read backlog import JSON: {exc}") from exc

    url = f"{governance_url.rstrip('/')}/api/backlog/{urllib.parse.quote(project_id, safe='')}/portable/import"
    body = {
        "payload": payload,
        "on_conflict": on_conflict,
        "dry_run": dry_run,
        "actor": actor,
    }
    code, result = _http_json("POST", url, body)
    if json_output:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        click.echo(
            "Backlog import "
            f"{'dry-run ' if dry_run else ''}"
            f"inserted={result.get('inserted_count', 0)} "
            f"updated={result.get('updated_count', 0)} "
            f"skipped={result.get('skipped_count', 0)} "
            f"errors={result.get('error_count', 0)}"
        )
    if code >= 400 or not result.get("ok", False):
        raise click.exceptions.Exit(1)


@main.group()
def plugin():
    """Install and validate local Aming Claw plugin assets."""
    pass


@plugin.command("install")
@click.argument("repo_url", required=False)
@click.option("--install-root", default="", help="User-local plugin cache root.")
@click.option("--ref", default="", help="Optional branch, tag, or commit to checkout.")
@click.option("--python", "python_executable", default=sys.executable, help="Python executable for pip/start commands.")
@click.option("--no-pip", is_flag=True, help="Clone and validate only; do not pip install.")
@click.option("--no-codex-install", is_flag=True, help="Do not install Codex plugin cache/config.")
@click.option("--codex-home", default="", help="Override Codex home for plugin cache/config.")
@click.option("--codex-config", default="", help="Override Codex config.toml path.")
@click.option("--codex-marketplace-root", default="", help="Override generated Codex marketplace root.")
@click.option("--start", is_flag=True, help="Run the start command after install.")
@click.option("--dry-run", is_flag=True, help="Print planned commands without changing state.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
@click.option("--validate-only", is_flag=True, help="Validate the computed checkout path without cloning or fetching.")
def plugin_install(repo_url, install_root, ref, python_executable, no_pip, no_codex_install, codex_home, codex_config, codex_marketplace_root, start, dry_run, json_output, validate_only):
    """Clone/update the plugin from a Git URL and print next steps."""
    from agent.plugin_installer import (
        DEFAULT_REPO_URL,
        PluginInstallError,
        format_result,
        install_from_git,
    )

    try:
        result = install_from_git(
            repo_url or DEFAULT_REPO_URL,
            install_root=install_root or None,
            ref=ref,
            python_executable=python_executable,
            install_package=not no_pip,
            install_codex_plugin=not no_codex_install,
            codex_home=codex_home or None,
            codex_config=codex_config or None,
            codex_marketplace_root=codex_marketplace_root or None,
            start=start,
            dry_run=dry_run,
            validate_only=validate_only,
            suppress_command_output=json_output,
        )
    except PluginInstallError as exc:
        raise click.ClickException(str(exc)) from exc
    if json_output:
        click.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        click.echo(format_result(result))


@plugin.command("update")
@click.argument("repo_url", required=False)
@click.option("--check", "check_only", is_flag=True, help="Check for updates and refresh local state without applying.")
@click.option("--apply", "apply_update", is_flag=True, help="Apply a fast-forward update to the local plugin checkout.")
@click.option("--install-root", default="", help="User-local plugin cache root.")
@click.option("--ref", default="", help="Optional branch, tag, or commit to compare/apply.")
@click.option("--python", "python_executable", default=sys.executable, help="Python executable for pip/cache commands.")
@click.option("--no-pip", is_flag=True, help="Do not pip install after applying.")
@click.option("--no-codex-install", is_flag=True, help="Do not refresh Codex plugin cache/config after applying.")
@click.option("--codex-home", default="", help="Override Codex home for plugin cache checks.")
@click.option("--codex-config", default="", help="Override Codex config.toml path.")
@click.option("--codex-marketplace-root", default="", help="Override generated Codex marketplace root.")
@click.option("--plugin-state", default="", help="Optional plugin update state JSON path.")
@click.option("--dry-run", is_flag=True, help="Print planned update commands without changing state.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def plugin_update(repo_url, check_only, apply_update, install_root, ref, python_executable, no_pip, no_codex_install, codex_home, codex_config, codex_marketplace_root, plugin_state, dry_run, json_output):
    """Check or apply updates for a Git-backed local plugin checkout."""
    if check_only and apply_update:
        raise click.ClickException("Use either --check or --apply, not both.")
    from agent.plugin_installer import (
        DEFAULT_REPO_URL,
        format_plugin_update_result,
        update_plugin_from_git,
    )

    result = update_plugin_from_git(
        repo_url or DEFAULT_REPO_URL,
        install_root=install_root or None,
        ref=ref,
        apply_update=apply_update,
        python_executable=python_executable,
        install_package=not no_pip,
        install_codex_plugin=not no_codex_install,
        codex_home=codex_home or None,
        codex_config=codex_config or None,
        codex_marketplace_root=codex_marketplace_root or None,
        state_path=plugin_state or None,
        suppress_command_output=json_output,
        dry_run=dry_run,
    )
    if json_output:
        click.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        click.echo(format_plugin_update_result(result))
    if not result.ok:
        raise click.exceptions.Exit(1)


@plugin.command("doctor")
@click.option("--plugin-root", default="", help="Local Aming Claw plugin checkout root.")
@click.option("--governance-url", default="http://localhost:40000", help="Governance service URL.")
@click.option("--codex-config", default="", help="Optional Codex config.toml path.")
@click.option("--codex-home", default="", help="Optional Codex home for plugin cache checks.")
@click.option("--python", "python_executable", default=sys.executable, help="Python executable to validate for local runtime.")
@click.option("--skip-governance", is_flag=True, help="Skip governance health probe.")
@click.option("--check-service-manager", is_flag=True, help="Also check advanced chain/executor ServiceManager health.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def plugin_doctor(plugin_root, governance_url, codex_config, codex_home, python_executable, skip_governance, check_service_manager, json_output):
    """Run read-only aftercare checks for a local plugin install."""
    from agent.plugin_installer import doctor_plugin, format_doctor_result

    result = doctor_plugin(
        plugin_root=plugin_root or None,
        governance_url=governance_url,
        codex_config=codex_config or None,
        codex_home=codex_home or None,
        python_executable=python_executable,
        check_governance=not skip_governance,
        check_service_manager=check_service_manager,
    )
    if json_output:
        click.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        click.echo(format_doctor_result(result))
    if not result.ok:
        raise click.exceptions.Exit(1)


@main.group()
def observer():
    """Observer runtime launcher."""
    pass


@observer.command("run")
@click.option("--project-id", required=True, help="Governance project id.")
@click.option("--backlog-id", required=True, help="Backlog id the observer will supervise.")
@click.option("--route-context-hash", required=True, help="Route context hash for this observer run.")
@click.option("--prompt-contract-id", required=True, help="Prompt contract id for this observer run.")
@click.option("--prompt-contract-hash", default="", help="Optional prompt contract hash.")
@click.option("--route-token-ref", default="", help="Optional route token id/ref.")
@click.option("--provider", default="openai", help="Provider name, e.g. openai or anthropic.")
@click.option("--model", default="", help="Optional provider model override.")
@click.option("--backend-mode", default="codex_cli", help="Invocation backend, e.g. codex_cli, claude_cli, openai_api, anthropic_api.")
@click.option("--workspace", default="", help="Observer workspace. Defaults to current working directory.")
@click.option("--prompt-file", default=None, type=click.Path(exists=True, dir_okay=False, readable=True), help="Optional observer prompt file.")
@click.option(
    "--dispatch-gate-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="MF subagent dispatch gate evidence JSON required for live code-mutating backends.",
)
@click.option("--main-worktree", default="", help="Target/main worktree path blocked by one-hop dispatch policy.")
@click.option("--execute", is_flag=True, help="Actually invoke the configured provider. Default is dry-run evidence only.")
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def observer_run(
    project_id,
    backlog_id,
    route_context_hash,
    prompt_contract_id,
    prompt_contract_hash,
    route_token_ref,
    provider,
    model,
    backend_mode,
    workspace,
    prompt_file,
    dispatch_gate_file,
    main_worktree,
    execute,
    json_output,
):
    """Build or execute a route-bound observer invocation."""
    from agent.observer_runtime import ObserverRunRequest, run_observer
    from agent.ai_invocation import RoutePromptContract

    prompt = Path(prompt_file).read_text(encoding="utf-8") if prompt_file else ""
    dispatch_gate = {}
    if dispatch_gate_file:
        try:
            parsed_gate = json.loads(Path(dispatch_gate_file).read_text(encoding="utf-8"))
        except Exception as exc:
            raise click.ClickException(f"invalid dispatch gate file: {exc}") from exc
        if not isinstance(parsed_gate, dict):
            raise click.ClickException("dispatch gate file must contain a JSON object")
        dispatch_gate = parsed_gate
    request = ObserverRunRequest(
        project_id=project_id,
        backlog_id=backlog_id,
        route=RoutePromptContract(
            route_context_hash=route_context_hash,
            prompt_contract_id=prompt_contract_id,
            prompt_contract_hash=prompt_contract_hash,
            route_token_ref=route_token_ref,
        ),
        provider=provider,
        model=model,
        backend_mode=backend_mode,
        workspace=workspace or os.getcwd(),
        prompt=prompt,
        dispatch_gate=dispatch_gate,
        main_worktree=main_worktree or os.getcwd(),
    )
    result = run_observer(request, execute=execute)
    if json_output:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        click.echo(f"observer run: {result.get('status')} project={project_id} backlog={backlog_id}")
        invocation = result.get("invocation") or result.get("invocation_request") or {}
        click.echo(f"backend: {invocation.get('backend_mode', backend_mode)} execute={execute}")
        click.echo(f"route: {route_context_hash}")
        if not result.get("ok"):
            click.echo("missing: " + ", ".join(result.get("missing") or []), err=True)
    if not result.get("ok"):
        raise click.exceptions.Exit(1)


@main.group()
def mf():
    """Manual-fix workflow checks."""
    pass


@mf.command("precommit-check")
@click.option("--plugin-state", default="", help="Optional plugin update state JSON path.")
@click.option(
    "--route-consumption-file",
    default=None,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Optional route-context consumption evidence JSON path.",
)
@click.option("--json-output", is_flag=True, help="Print machine-readable JSON.")
def mf_precommit_check(plugin_state, route_consumption_file, json_output):
    """Run local MF pre-commit guards that do not mutate governance state."""
    from agent.plugin_installer import (
        format_plugin_update_state_status,
        plugin_update_state_status,
    )

    plugin_status = plugin_update_state_status(state_path=plugin_state or None)
    route_status = _mf_route_consumption_file_status(route_consumption_file)
    result = {
        "ok": bool(plugin_status.get("ok")) and bool(route_status.get("ok")),
        "checks": {
            "plugin_update_state": plugin_status,
            "route_context_consumption": route_status,
        },
    }
    if json_output:
        click.echo(json.dumps(result, indent=2, sort_keys=True))
    else:
        click.echo("Aming Claw MF precommit check")
        click.echo("")
        click.echo(format_plugin_update_state_status(plugin_status))
        if route_consumption_file:
            status = "pass" if route_status.get("ok") else "fail"
            click.echo(f"route context consumption: {status}")
            missing = route_status.get("missing_requirement_ids") or []
            if missing:
                click.echo(f"missing: {', '.join(missing)}")
    if not result["ok"]:
        raise click.exceptions.Exit(1)


def _mf_route_consumption_file_status(path: str) -> dict:
    if not path:
        return {"status": "skipped", "ok": True}
    from agent.governance.task_timeline import mf_route_context_gate_verification

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "fail", "ok": False, "error": f"invalid route consumption file: {exc}"}
    if not isinstance(payload, dict):
        return {"status": "fail", "ok": False, "error": "route consumption file must be a JSON object"}
    raw_events = payload.get("timeline_evidence") or payload.get("events") or payload.get("route_events")
    if isinstance(raw_events, dict):
        events = [raw_events]
    elif isinstance(raw_events, list):
        events = [item for item in raw_events if isinstance(item, dict)]
    else:
        events = [payload] if any(key in payload for key in ("route_context_hash", "route_identity")) else []
    contract = payload.get("contract") if isinstance(payload.get("contract"), dict) else payload
    gate = mf_route_context_gate_verification(events, contract=contract)
    return {
        "status": "pass" if gate.get("passed") else "fail",
        "ok": bool(gate.get("passed")),
        "required": bool(gate.get("required")),
        "missing_requirement_ids": gate.get("missing_requirement_ids") or [],
        "present_requirement_ids": gate.get("present_requirement_ids") or [],
        "topology_policy": gate.get("topology_policy") or {},
    }


@mf.command("dispatch-gate")
@click.option(
    "--contract-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="Existing MF subagent dispatch contract JSON path.",
)
@click.option("--target-worktree", default="", help="Target worktree path to block same-worktree dispatch.")
@click.option("--main-worktree", default="", help="Main worktree path to block same-worktree dispatch.")
def mf_dispatch_gate(contract_file, target_worktree, main_worktree):
    """Validate MF subagent dispatch evidence before worker handoff."""
    from agent.governance.mf_subagent_contract import validate_mf_subagent_dispatch_gate

    try:
        payload = json.loads(Path(contract_file).read_text(encoding="utf-8"))
        result = validate_mf_subagent_dispatch_gate(
            payload,
            target_worktree_path=target_worktree,
            main_worktree_path=main_worktree,
        )
    except Exception as exc:
        click.echo(f"REJECT: {exc}", err=True)
        raise click.exceptions.Exit(1) from exc

    click.echo(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
