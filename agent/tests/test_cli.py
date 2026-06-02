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
        codex_cache_plugin_root,
        install_codex_marketplace,
        install_codex_plugin_cache,
        plugin_root_for,
    )
    HAS_CLICK = True
except ImportError:
    HAS_CLICK = False

pytestmark = pytest.mark.skipif(not HAS_CLICK, reason="click not installed")


def test_observer_run_dry_run_emits_route_bound_invocation():
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "observer",
            "run",
            "--project-id",
            "aming-claw",
            "--backlog-id",
            "AC-TEST",
            "--route-context-hash",
            "sha256:route",
            "--prompt-contract-id",
            "rprompt-test",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["status"] == "planned"
    assert payload["execute"] is False
    evidence = payload["invocation"]
    assert evidence["schema_version"] == "ai_invocation_result.v1"
    assert evidence["backend_mode"] == "codex_cli"
    assert evidence["calls_models"] is False
    assert evidence["route_prompt_contract"]["route_context_hash"] == "sha256:route"
    assert evidence["route_prompt_contract"]["prompt_contract_id"] == "rprompt-test"
    assert evidence["route_alert_ack"]["status"] == "acknowledged"
    assert evidence["raw_output_stored"] is False


def test_observer_run_rejects_missing_route_identity():
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "observer",
            "run",
            "--project-id",
            "aming-claw",
            "--backlog-id",
            "AC-TEST",
            "--route-context-hash",
            "",
            "--prompt-contract-id",
            "rprompt-test",
            "--json-output",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert "route_context_hash" in payload["missing"]
    assert payload["execute"] is False


def test_observer_run_execute_codex_requires_one_hop_dispatch_gate():
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "observer",
            "run",
            "--project-id",
            "aming-claw",
            "--backlog-id",
            "AC-TEST",
            "--route-context-hash",
            "sha256:route",
            "--prompt-contract-id",
            "rprompt-test",
            "--backend-mode",
            "codex_cli",
            "--execute",
            "--json-output",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["status"] == "rejected"
    assert payload["execute"] is True
    gate = payload["one_hop_execution_gate"]
    assert gate["required"] is True
    assert gate["allowed"] is False
    assert "dispatch_gate" in gate["missing"]
    assert "invocation" not in payload


def test_observer_run_execute_fixture_does_not_require_one_hop_dispatch_gate():
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "observer",
            "run",
            "--project-id",
            "aming-claw",
            "--backlog-id",
            "AC-TEST",
            "--route-context-hash",
            "sha256:route",
            "--prompt-contract-id",
            "rprompt-test",
            "--provider",
            "fixture",
            "--backend-mode",
            "fixture",
            "--execute",
            "--json-output",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["status"] == "completed"
    assert payload["one_hop_execution_gate"]["required"] is False
    assert payload["invocation"]["calls_models"] is False


def test_observer_run_execute_rejects_incomplete_dispatch_gate(tmp_path):
    gate_file = tmp_path / "dispatch-gate.json"
    gate_file.write_text(
        json.dumps(
            {
                "route_context_hash": "sha256:route",
                "prompt_contract_id": "rprompt-test",
                "owned_files": ["agent/observer_runtime.py"],
                "dirty_scope_check": {"dirty_scope_exact_match": True},
            }
        ),
        encoding="utf-8",
    )
    runner = CliRunner()

    result = runner.invoke(
        main,
        [
            "observer",
            "run",
            "--project-id",
            "aming-claw",
            "--backlog-id",
            "AC-TEST",
            "--route-context-hash",
            "sha256:route",
            "--prompt-contract-id",
            "rprompt-test",
            "--backend-mode",
            "codex_cli",
            "--dispatch-gate-file",
            str(gate_file),
            "--execute",
            "--json-output",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    gate = payload["one_hop_execution_gate"]
    assert gate["allowed"] is False
    for field in (
        "branch",
        "worktree",
        "base_commit",
        "target_head_commit",
        "merge_queue_id",
        "fence_token",
    ):
        assert field in gate["error"]


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
        ".codex-plugin/plugin.json": {"name": "aming-claw", "version": "0.1.1"},
        ".agents/plugins/marketplace.json": {
            "name": "aming-claw-local",
            "plugins": [
                {"name": "aming-claw", "source": {"source": "local", "path": "./."}}
            ],
        },
        ".claude-plugin/plugin.json": {
            "name": "aming-claw",
            "version": "0.1.1",
            "description": "Test plugin.",
            "mcpServers": {"aming-claw": {"command": "python", "args": ["-m", "agent.mcp.server"]}},
        },
        ".claude-plugin/marketplace.json": {
            "name": "aming-claw-local",
            "metadata": {"description": "Test marketplace."},
            "owner": {"name": "Aming Claw"},
            "plugins": [{"name": "aming-claw", "source": "./", "version": "0.1.1"}],
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
    for rel in (
        "skills/aming-claw/SKILL.md",
        "skills/aming-claw-hn-challenge/SKILL.md",
        "skills/aming-claw-hn-demo/SKILL.md",
        "skills/aming-claw-hn-demo-after-work/SKILL.md",
        "skills/aming-claw-hn-demo-before-work/SKILL.md",
        "skills/aming-claw-hn-demo-during-work/SKILL.md",
        "skills/aming-claw-vibe-queue-demo/SKILL.md",
        "skills/aming-claw-drift-demo/SKILL.md",
        "skills/aming-claw-backlog-dupe-demo/SKILL.md",
        "skills/aming-claw-launcher/SKILL.md",
        "frontend/dashboard/scripts/e2e-hn-demo.mjs",
        "frontend/dashboard/scripts/e2e-vibe-queue-fixture.mjs",
        "frontend/dashboard/scripts/e2e-vibe-queue-audit.mjs",
        "frontend/dashboard/scripts/e2e-drift-demo-fixture.mjs",
        "frontend/dashboard/scripts/e2e-drift-demo-audit.mjs",
        "frontend/dashboard/scripts/e2e-backlog-dupe-fixture.mjs",
        "frontend/dashboard/scripts/e2e-backlog-dupe-audit.mjs",
        "docs/vibe-queue-demo/README.md",
        "docs/vibe-queue-demo/prompts.md",
        "docs/drift-demo/README.md",
        "docs/drift-demo/prompts.md",
        "docs/backlog-dupe-demo/README.md",
        "docs/backlog-dupe-demo/prompts.md",
        "docker/hn-install-audit/run-install-audit.sh",
        "docker/hn-install-audit/common/install-audit.mjs",
        "docker/hn-install-audit/common/state-manager.mjs",
        "docker/hn-install-audit/validate-report.mjs",
        "docker/hn-install-audit/codex/Dockerfile",
        "docker/hn-install-audit/claude/Dockerfile",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".mjs"):
            path.write_text("#!/usr/bin/env node\nconsole.log('hn demo fixture ok');\n", encoding="utf-8")
        elif rel.endswith(".sh"):
            path.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        elif rel.endswith("Dockerfile"):
            path.write_text("FROM scratch\n", encoding="utf-8")
        else:
            path.write_text("---\nname: test\n---\n", encoding="utf-8")
    server_path = root / "agent" / "mcp" / "server.py"
    server_path.parent.mkdir(parents=True, exist_ok=True)
    server_path.write_text("# test runtime entrypoint\n", encoding="utf-8")


def _make_cli_remote_plugin_repo_with_source(tmp_path):
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
    return remote, source


def _make_cli_remote_plugin_repo(tmp_path):
    remote, _source = _make_cli_remote_plugin_repo_with_source(tmp_path)
    return remote


def _git_commit_all(repo: Path, message: str) -> str:
    _git(["add", "."], repo)
    _git(
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            message,
        ],
        repo,
    )
    return _git(["rev-parse", "HEAD"], repo)


def _write_noisy_fake_python(tmp_path: Path) -> Path:
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import sys",
                "if sys.argv[1:] == ['--version']:",
                "    print('Python 3.11.0')",
                "    raise SystemExit(0)",
                "if sys.argv[1:4] == ['-m', 'pip', 'install']:",
                "    print('PIP NOISE THAT MUST NOT POLLUTE JSON')",
                "    raise SystemExit(0)",
                "print('unexpected fake-python args: ' + repr(sys.argv), file=sys.stderr)",
                "raise SystemExit(1)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    return fake_python


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
    def test_plugin_install_json_suppresses_subprocess_stdout(self, tmp_path):
        runner = CliRunner()
        remote = _make_cli_remote_plugin_repo(tmp_path)
        fake_python = _write_noisy_fake_python(tmp_path)
        install_root = tmp_path / "install"
        codex_home = tmp_path / "codex-home"
        marketplace_root = tmp_path / "marketplace-root"

        result = runner.invoke(main, [
            "plugin",
            "install",
            str(remote),
            "--install-root",
            str(install_root),
            "--python",
            str(fake_python),
            "--codex-home",
            str(codex_home),
            "--codex-config",
            str(codex_home / "config.toml"),
            "--codex-marketplace-root",
            str(marketplace_root),
            "--json-output",
        ], env={"AMING_CLAW_PLUGIN_STATE_HOME": str(tmp_path / "state-home")})

        assert result.exit_code == 0
        assert "PIP NOISE" not in result.output
        payload = json.loads(result.output)
        assert payload["installed_package"] is True
        assert payload["installed_codex_plugin"] is True

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
        assert "service_manager_health" not in result.output
        assert "ServiceManager/executor checks are advanced" in result.output

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

    def test_plugin_update_apply_from_external_cwd_does_not_pollute_target(self, tmp_path, monkeypatch):
        runner = CliRunner()
        remote, source = _make_cli_remote_plugin_repo_with_source(tmp_path)
        install_root = tmp_path / "install"
        install_root.mkdir()
        plugin_root = plugin_root_for(str(remote), install_root)
        _git(["clone", str(remote), str(plugin_root)], install_root)
        _git(["checkout", "main"], plugin_root)

        skill = source / "skills" / "aming-claw" / "SKILL.md"
        skill.write_text("---\nname: test\n---\nupdated\n", encoding="utf-8")
        remote_commit = _git_commit_all(source, "update skill")
        _git(["push", "origin", "main"], source)

        external_project = tmp_path / "my-app"
        (external_project / "src").mkdir(parents=True)
        (external_project / "src" / "App.js").write_text(
            "export default function App() { return null; }\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(external_project)

        codex_home = tmp_path / "codex-home"
        marketplace_root = tmp_path / "marketplace-root"
        state_path = tmp_path / "state.json"
        result = runner.invoke(main, [
            "plugin",
            "update",
            str(remote),
            "--apply",
            "--install-root",
            str(install_root),
            "--plugin-state",
            str(state_path),
            "--no-pip",
            "--codex-home",
            str(codex_home),
            "--codex-config",
            str(codex_home / "config.toml"),
            "--codex-marketplace-root",
            str(marketplace_root),
            "--json-output",
        ])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["applied"] is True
        assert payload["installed_package"] is False
        assert payload["installed_codex_plugin"] is True
        assert payload["status"] == "applied_pending_restart"
        assert payload["changed_surfaces"] == ["mcp"]
        assert _git(["rev-parse", "HEAD"], plugin_root) == remote_commit
        assert (codex_cache_plugin_root(plugin_root, codex_home=codex_home) / ".mcp.json").is_file()
        assert (marketplace_root / ".agents" / "plugins" / "aming-claw" / ".mcp.json").is_file()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["update_status"] == "applied_pending_restart"
        assert state["remote_commit"] == remote_commit

        for rel in (
            ".mcp.json",
            "shared-volume",
            ".codex-plugin",
            ".claude-plugin",
            ".agents/plugins",
            "agent/mcp/resources",
        ):
            assert not (external_project / rel).exists(), f"unexpected target-local plugin artifact: {rel}"

    def test_plugin_update_apply_json_suppresses_subprocess_stdout(self, tmp_path):
        runner = CliRunner()
        remote, source = _make_cli_remote_plugin_repo_with_source(tmp_path)
        install_root = tmp_path / "install"
        install_root.mkdir()
        plugin_root = plugin_root_for(str(remote), install_root)
        _git(["clone", str(remote), str(plugin_root)], install_root)
        _git(["checkout", "main"], plugin_root)

        skill = source / "skills" / "aming-claw" / "SKILL.md"
        skill.write_text("---\nname: test\n---\nupdated\n", encoding="utf-8")
        _git_commit_all(source, "update skill")
        _git(["push", "origin", "main"], source)

        fake_python = _write_noisy_fake_python(tmp_path)
        codex_home = tmp_path / "codex-home"
        marketplace_root = tmp_path / "marketplace-root"
        result = runner.invoke(main, [
            "plugin",
            "update",
            str(remote),
            "--apply",
            "--install-root",
            str(install_root),
            "--python",
            str(fake_python),
            "--plugin-state",
            str(tmp_path / "state.json"),
            "--codex-home",
            str(codex_home),
            "--codex-config",
            str(codex_home / "config.toml"),
            "--codex-marketplace-root",
            str(marketplace_root),
            "--json-output",
        ])

        assert result.exit_code == 0
        assert "PIP NOISE" not in result.output
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["applied"] is True
        assert payload["installed_package"] is True
        assert payload["installed_codex_plugin"] is True

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
    def test_mf_dispatch_gate_help_visible(self):
        runner = CliRunner()

        result = runner.invoke(main, ["mf", "--help"])
        assert result.exit_code == 0
        assert "dispatch-gate" in result.output

        command_help = runner.invoke(main, ["mf", "dispatch-gate", "--help"])
        assert command_help.exit_code == 0
        assert "--contract-file" in command_help.output
        assert "--target-worktree" in command_help.output
        assert "--main-worktree" in command_help.output

    def test_mf_dispatch_gate_rejects_invalid_payload(self, tmp_path):
        runner = CliRunner(mix_stderr=False)
        contract_path = tmp_path / "dispatch.json"
        contract_path.write_text(json.dumps({"owned_files": []}), encoding="utf-8")

        result = runner.invoke(main, [
            "mf",
            "dispatch-gate",
            "--contract-file",
            str(contract_path),
        ])

        assert result.exit_code == 1
        assert result.output == ""
        assert "REJECT: MF subagent dispatch missing required fields:" in result.stderr
        assert "branch" in result.stderr

    def test_mf_dispatch_gate_prints_pretty_json_on_pass(self, tmp_path):
        runner = CliRunner()
        contract_path = tmp_path / "dispatch.json"
        contract_path.write_text(json.dumps({
            "branch": "mf/test-worker",
            "worktree": str(tmp_path / "worker"),
            "base_commit": "abc123",
            "target_head_commit": "def456",
            "merge_queue_id": "mq-test",
            "fence_token": "fence-test",
            "route_context_hash": "sha256:test-route-context",
            "prompt_contract_id": "prompt-contract-test",
            "prompt_contract_hash": "sha256:test-prompt-contract",
            "owned_files": ["agent/cli.py"],
            "dirty_scope_check": {
                "status": "passed",
                "changed_files": [],
            },
        }), encoding="utf-8")

        result = runner.invoke(main, [
            "mf",
            "dispatch-gate",
            "--contract-file",
            str(contract_path),
        ])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["schema_version"] == "mf_subagent_dispatch_gate.v1"
        assert payload["fence_token"] == "fence-test"
        assert payload["route_context_hash"] == "sha256:test-route-context"
        assert payload["base_commit"] == "abc123"
        assert payload["target_head_commit"] == "def456"
        assert payload["owned_files"] == ["agent/cli.py"]
        assert "\n  \"base_commit\": \"abc123\"" in result.output

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

    def test_mf_precommit_check_blocks_missing_route_consumption(self, tmp_path):
        runner = CliRunner()
        route_path = tmp_path / "route.json"
        route_path.write_text(json.dumps({
            "contract": {
                "selected_topology": "observer_led_parallel_lanes",
                "recommended_topology": "mf_parallel.v1",
            },
            "timeline_evidence": [
                {
                    "event_kind": "route_context_advisory",
                    "status": "passed",
                    "payload": {"message": "route docs say to use a worker"},
                }
            ],
        }), encoding="utf-8")

        result = runner.invoke(main, [
            "mf",
            "precommit-check",
            "--route-consumption-file",
            str(route_path),
            "--json-output",
        ])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert "bounded_implementation_worker_dispatch" in payload["checks"][
            "route_context_consumption"
        ]["missing_requirement_ids"]

    def test_mf_precommit_check_accepts_consumed_route_context(self, tmp_path):
        runner = CliRunner()
        identity = {
            "route_context_hash": "sha256:test-route-context",
            "prompt_contract_id": "prompt-contract-test",
            "prompt_contract_hash": "sha256:test-prompt-contract",
        }
        route_path = tmp_path / "route.json"
        route_path.write_text(json.dumps({
            "contract": {
                "selected_topology": "observer_led_parallel_lanes",
                "recommended_topology": "mf_parallel.v1",
            },
            "timeline_evidence": [
                {
                    "event_kind": "route_context",
                    "status": "passed",
                    "payload": {"route_context": identity},
                },
                {
                    "event_kind": "route_action_precheck",
                    "status": "allowed",
                    "verification": {**identity, "allowed_action": "dispatch_worker"},
                },
                {
                    "event_kind": "mf_subagent_dispatch",
                    "status": "passed",
                    "payload": {"mf_subagent_dispatch_gate": {**identity, "bounded": True}},
                },
                {
                    "event_kind": "mf_subagent_startup",
                    "status": "passed",
                    "payload": {"mf_subagent_startup_gate": {**identity, "worker_id": "mf-sub"}},
                },
                {
                    "event_kind": "qa_verification",
                    "status": "passed",
                    "verification": {
                        **identity,
                        "contract_evidence": [
                            {
                                "requirement_id": "independent_verification_lane",
                                "status": "passed",
                                "reviewer_role": "qa",
                            }
                        ],
                    },
                },
            ],
        }), encoding="utf-8")

        result = runner.invoke(main, [
            "mf",
            "precommit-check",
            "--plugin-state",
            str(tmp_path / "missing.json"),
            "--route-consumption-file",
            str(route_path),
            "--json-output",
        ])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["checks"]["route_context_consumption"]["status"] == "pass"
