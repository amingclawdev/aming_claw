"""Tests for agent.cli — AC1, AC8."""

import os
import hashlib
import json
import subprocess
import sys
import types
from pathlib import Path
import pytest

try:
    from click.testing import CliRunner
    from agent.cli import main
    from agent.plugin_installer import (
        configure_codex_plugin,
        install_codex_marketplace,
        install_codex_plugin_cache,
        plugin_root_for,
    )
    HAS_CLICK = True
except ImportError:
    HAS_CLICK = False

pytestmark = pytest.mark.skipif(not HAS_CLICK, reason="click not installed")


def _git(args: list[str], cwd):
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _write_cli_plugin_fixture(root):
    seed_payload = {"schema_version": 1, "project_id": "aming-claw"}
    seed_text = json.dumps(seed_payload)
    seed_hash = hashlib.sha256(seed_text.encode("utf-8")).hexdigest()
    for rel, text in {
        ".codex-plugin/plugin.json": {"name": "aming-claw"},
        ".agents/plugins/marketplace.json": {
            "name": "aming-claw-local",
            "plugins": [
                {"name": "aming-claw", "source": {"source": "local", "path": "./."}}
            ],
        },
        ".claude-plugin/plugin.json": {
            "name": "aming-claw",
            "version": "0.1.0",
            "description": "Test plugin.",
            "mcpServers": {"aming-claw": {"command": "python", "args": ["-m", "agent.mcp.server"]}},
        },
        ".claude-plugin/marketplace.json": {
            "name": "aming-claw-local",
            "metadata": {"description": "Test marketplace."},
            "owner": {"name": "Aming Claw"},
            "plugins": [{"name": "aming-claw", "source": "./", "version": "0.1.0"}],
        },
        ".mcp.json": {"mcpServers": {"aming-claw": {"command": "python"}}},
        "agent/mcp/resources/seed-graph-summary.json": seed_payload,
        "agent/mcp/resources/self-graph-bundle-manifest.json": {
            "schema_version": 1,
            "bundle_kind": "aming_claw_self_graph_semantic_bundle",
            "bundle_major": 1,
            "bundle_version": "1.0.0",
            "project_id": "aming-claw",
            "source_commit": "abc1234",
            "snapshot_id": "scope-abc1234-test",
            "projection_id": "semproj-abc1234-test",
            "event_watermark": 7,
            "resources": [
                {
                    "path": "agent/mcp/resources/seed-graph-summary.json",
                    "role": "seed_graph_summary",
                    "required": True,
                    "sha256": seed_hash,
                }
            ],
        },
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(text), encoding="utf-8")
    for rel in ("skills/aming-claw/SKILL.md", "skills/aming-claw-launcher/SKILL.md"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nname: test\n---\n", encoding="utf-8")
    server_path = root / "agent" / "mcp" / "server.py"
    server_path.parent.mkdir(parents=True, exist_ok=True)
    server_path.write_text("# test runtime entrypoint\n", encoding="utf-8")


def _make_cli_remote_plugin_repo(tmp_path):
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    _git(["init", "--bare", str(remote)], tmp_path)
    source.mkdir()
    _git(["init"], source)
    _git(["checkout", "-b", "main"], source)
    _write_cli_plugin_fixture(source)
    _git(["add", "."], source)
    _git(["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "initial plugin"], source)
    _git(["remote", "add", "origin", str(remote)], source)
    _git(["push", "-u", "origin", "main"], source)
    return remote


class TestCliHelp:
    """AC1: aming-claw --help contains subcommands."""

    def test_help_output(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        for cmd in ("init", "bootstrap", "scan", "status", "start", "open", "launcher", "run-executor", "backlog", "plugin", "mf"):
            assert cmd in result.output


class TestCliInit:
    """AC8: init creates .aming-claw.yaml."""

    def test_init_creates_yaml(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(main, ["init"])
            assert result.exit_code == 0
            assert os.path.exists(".aming-claw.yaml")

    def test_init_idempotent(self, tmp_path):
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            runner.invoke(main, ["init"])
            result = runner.invoke(main, ["init"])
            assert "already exists" in result.output


class TestCliLauncher:
    def test_launcher_writes_local_html(self, tmp_path):
        runner = CliRunner()
        output = tmp_path / "launcher.html"

        result = runner.invoke(main, [
            "launcher",
            "--governance-url",
            "http://127.0.0.1:45555",
            "--output",
            str(output),
        ])

        assert result.exit_code == 0
        text = output.read_text(encoding="utf-8")
        assert "Aming Claw Launcher" in text
        assert "http://127.0.0.1:45555/dashboard" in text
        assert "aming-claw start" in text


class TestCliStart:
    def test_start_without_workspace_uses_plugin_runtime_root_not_cwd(self, monkeypatch, tmp_path):
        import agent.cli as cli

        runner = CliRunner()
        calls = []
        fake_start_governance = types.SimpleNamespace(
            main=lambda workspace_root=None: calls.append(Path(workspace_root).resolve())
        )
        monkeypatch.setitem(sys.modules, "start_governance", fake_start_governance)
        monkeypatch.setattr(cli, "_probe_governance", lambda port: None)
        monkeypatch.setattr(cli, "_port_is_open", lambda port: False)
        monkeypatch.delenv("AMING_CLAW_HOME", raising=False)
        monkeypatch.delenv("SHARED_VOLUME_PATH", raising=False)

        with runner.isolated_filesystem(temp_dir=tmp_path):
            cwd = Path.cwd()
            result = runner.invoke(main, ["start", "--port", "45555"])

        assert result.exit_code == 0
        assert calls == [Path(cli.__file__).resolve().parents[1]]
        assert not (cwd / "shared-volume").exists()
        assert not (cwd / ".mcp.json").exists()

    def test_start_exits_when_governance_already_healthy(self, monkeypatch, tmp_path):
        import agent.cli as cli

        runner = CliRunner()
        monkeypatch.setattr(
            cli,
            "_probe_governance",
            lambda port: {"status": "ok", "service": "governance", "version": "abc123", "port": port},
        )
        monkeypatch.setattr(cli, "_port_is_open", lambda port: False)

        result = runner.invoke(main, ["start", "--workspace", str(tmp_path), "--port", "45555"])

        assert result.exit_code == 0
        assert "Governance already running on port 45555" in result.output
        assert "http://localhost:45555/dashboard" in result.output

    def test_start_reports_non_governance_port_conflict(self, monkeypatch, tmp_path):
        import agent.cli as cli

        runner = CliRunner()
        monkeypatch.setattr(cli, "_probe_governance", lambda port: None)
        monkeypatch.setattr(cli, "_port_is_open", lambda port: True)
        monkeypatch.setattr(cli, "_port_owner_hint", lambda port: " PID=1234")

        result = runner.invoke(main, ["start", "--workspace", str(tmp_path), "--port", "45555"])

        assert result.exit_code != 0
        assert "Port 45555 is already in use PID=1234" in result.output
        assert "not Aming Claw governance" in result.output


class TestCliPlugin:
    def test_plugin_install_dry_run_prints_plan(self, tmp_path):
        runner = CliRunner()

        result = runner.invoke(main, [
            "plugin",
            "install",
            "https://github.com/amingclawdev/aming-claw.git",
            "--install-root",
            str(tmp_path),
            "--dry-run",
            "--no-pip",
        ])

        assert result.exit_code == 0
        assert "Aming Claw plugin bootstrap" in result.output
        assert "git clone" in result.output
        assert "Claude Code: /plugin marketplace add" in result.output

    def test_plugin_doctor_reports_aftercare(self, tmp_path):
        runner = CliRunner()
        _write_cli_plugin_fixture(tmp_path)

        codex_home = tmp_path / "codex-home"
        marketplace_root = install_codex_marketplace(tmp_path, marketplace_root=tmp_path / "marketplace-root")
        install_codex_plugin_cache(tmp_path, codex_home=codex_home)
        config = configure_codex_plugin(
            codex_config=codex_home / "config.toml",
            marketplace_root=marketplace_root,
        )

        result = runner.invoke(main, [
            "plugin",
            "doctor",
            "--plugin-root",
            str(tmp_path),
            "--codex-config",
            str(config),
            "--codex-home",
            str(codex_home),
            "--skip-governance",
        ])

        assert result.exit_code == 0
        assert "Aming Claw plugin doctor" in result.output
        assert "Restart/reload Codex" in result.output
        assert "dashboard_static_assets" in result.output
        assert "ai_cli_openai" in result.output

    def test_plugin_update_check_json_reports_current(self, tmp_path):
        runner = CliRunner()
        remote = _make_cli_remote_plugin_repo(tmp_path)
        install_root = tmp_path / "install"
        install_root.mkdir()
        plugin_root = plugin_root_for(str(remote), install_root)
        _git(["clone", str(remote), str(plugin_root)], install_root)
        _git(["checkout", "main"], plugin_root)

        result = runner.invoke(main, [
            "plugin",
            "update",
            str(remote),
            "--check",
            "--install-root",
            str(install_root),
            "--plugin-state",
            str(tmp_path / "state.json"),
            "--no-pip",
            "--no-codex-install",
            "--json-output",
        ])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["status"] == "current"
        assert payload["update_available"] is False

    def test_plugin_update_missing_checkout_exits_nonzero(self, tmp_path):
        runner = CliRunner()

        result = runner.invoke(main, [
            "plugin",
            "update",
            "https://example.com/aming-claw.git",
            "--check",
            "--install-root",
            str(tmp_path / "missing-install"),
            "--plugin-state",
            str(tmp_path / "state.json"),
            "--json-output",
        ])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert payload["status"] == "failed"
        assert "plugin checkout not found" in payload["error"]


class TestCliBacklog:
    def test_backlog_export_writes_payload(self, monkeypatch, tmp_path):
        import agent.cli as cli

        calls = []

        def fake_http(method, url, payload=None, timeout=30.0):
            calls.append((method, url, payload))
            return 200, {
                "schema": "aming-claw.backlog.export",
                "schema_version": 1,
                "project_id": "aming-claw",
                "row_count": 1,
                "rows": [{"bug_id": "BUG-1"}],
            }

        monkeypatch.setattr(cli, "_http_json", fake_http)
        runner = CliRunner()
        output = tmp_path / "backlog.json"

        result = runner.invoke(main, [
            "backlog",
            "export",
            "--project-id",
            "aming-claw",
            "--status",
            "OPEN",
            "--bug-id",
            "BUG-1",
            "--output",
            str(output),
        ])

        assert result.exit_code == 0
        assert "Exported 1 backlog row" in result.output
        assert json.loads(output.read_text(encoding="utf-8"))["rows"][0]["bug_id"] == "BUG-1"
        assert calls[0][0] == "GET"
        assert "/api/backlog/aming-claw/portable/export" in calls[0][1]
        assert "status=OPEN" in calls[0][1]

    def test_backlog_import_posts_payload_and_exits_nonzero_on_conflict(self, monkeypatch, tmp_path):
        import agent.cli as cli

        input_path = tmp_path / "backlog.json"
        input_path.write_text(json.dumps({
            "schema": "aming-claw.backlog.export",
            "schema_version": 1,
            "rows": [{"bug_id": "BUG-1"}],
        }), encoding="utf-8")
        calls = []

        def fake_http(method, url, payload=None, timeout=30.0):
            calls.append((method, url, payload))
            return 409, {
                "ok": False,
                "inserted_count": 0,
                "updated_count": 0,
                "skipped_count": 0,
                "error_count": 1,
                "errors": [{"bug_id": "BUG-1", "error": "bug_id already exists"}],
            }

        monkeypatch.setattr(cli, "_http_json", fake_http)
        runner = CliRunner()

        result = runner.invoke(main, [
            "backlog",
            "import",
            "--project-id",
            "aming-claw",
            "--input",
            str(input_path),
            "--on-conflict",
            "fail",
            "--json-output",
        ])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert calls[0][0] == "POST"
        assert calls[0][2]["on_conflict"] == "fail"
        assert calls[0][2]["payload"]["rows"][0]["bug_id"] == "BUG-1"


class TestCliMf:
    def test_mf_precommit_check_passes_on_missing_state_warning(self, tmp_path):
        runner = CliRunner()

        result = runner.invoke(main, [
            "mf",
            "precommit-check",
            "--plugin-state",
            str(tmp_path / "missing.json"),
        ])

        assert result.exit_code == 0
        assert "Aming Claw MF precommit check" in result.output
        assert "plugin update state file not found" in result.output

    def test_mf_precommit_check_fails_on_restart_blocker(self, tmp_path):
        runner = CliRunner()
        state_path = tmp_path / "state.json"
        state_path.write_text(json.dumps({
            "schema_version": 1,
            "plugin_id": "aming-claw@aming-claw-local",
            "update_status": "applied_pending_restart",
            "restart_required": {
                "mcp": {"required": True, "reason": "skills changed"}
            },
        }), encoding="utf-8")

        result = runner.invoke(main, [
            "mf",
            "precommit-check",
            "--plugin-state",
            str(state_path),
            "--json-output",
        ])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert payload["checks"]["plugin_update_state"]["status"] == "fail"
