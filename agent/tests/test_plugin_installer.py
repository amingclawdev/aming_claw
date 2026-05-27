"""Tests for Git URL plugin bootstrap helpers."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent.plugin_installer import (
    AI_CLI_REQUIREMENTS,
    CODEX_PLUGIN_ID,
    PluginInstallError,
    _check_ai_cli,
    _check_claude_manifest,
    _check_claude_marketplace,
    _load_toml_text,
    _upsert_toml_table,
    classify_plugin_changed_surfaces,
    configure_codex_plugin,
    default_plugin_update_state_path,
    doctor_plugin,
    format_plugin_update_state_status,
    format_plugin_update_result,
    format_result,
    format_doctor_result,
    install_codex_marketplace,
    install_codex_plugin_cache,
    install_from_git,
    plugin_restart_required_for_surfaces,
    plugin_update_state_status,
    plugin_root_for,
    slug_from_repo_url,
    update_plugin_from_git,
    validate_plugin_root,
    write_plugin_update_state,
)


def _write_plugin_fixture(root: Path) -> None:
    seed_payload = {"schema_version": 1, "project_id": "aming-claw"}
    seed_text = json.dumps(seed_payload)
    seed_hash = hashlib.sha256(seed_text.encode("utf-8")).hexdigest()
    for rel, text in {
        ".codex-plugin/plugin.json": {"name": "aming-claw"},
        ".agents/plugins/marketplace.json": {
            "name": "aming-claw-local",
            "plugins": [
                {
                    "name": "aming-claw",
                    "source": {"source": "local", "path": "./."},
                    "policy": {"installation": "INSTALLED_BY_DEFAULT"},
                }
            ],
        },
        ".claude-plugin/plugin.json": {
            "name": "aming-claw",
            "version": "0.1.0",
            "description": "Test plugin.",
            "mcpServers": {
                "aming-claw": {
                    "command": "python",
                    "args": ["-m", "agent.mcp.server"],
                }
            },
        },
        ".claude-plugin/marketplace.json": {
            "name": "aming-claw-local",
            "metadata": {"description": "Test marketplace."},
            "owner": {"name": "Aming Claw"},
            "plugins": [
                {"name": "aming-claw", "source": "./", "version": "0.1.0"}
            ],
        },
        ".mcp.json": {
            "mcpServers": {
                "aming-claw": {
                    "command": "python",
                    "args": [
                        "-m",
                        "agent.mcp.server",
                        "--project",
                        "aming-claw",
                        "--workers",
                        "0",
                    ],
                    "cwd": ".",
                    "env": {"PYTHONDONTWRITEBYTECODE": "1"},
                }
            }
        },
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
        "docker/hn-install-audit/validate-report.mjs",
        "docker/hn-install-audit/codex/Dockerfile",
        "docker/hn-install-audit/claude/Dockerfile",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".mjs"):
            path.write_text("#!/usr/bin/env node\nconsole.log('hn demo fixture ok');\n", encoding="utf-8")
        elif rel.endswith(".sh"):
            path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        elif rel.endswith("Dockerfile"):
            path.write_text("FROM scratch\n", encoding="utf-8")
        elif rel.endswith(".md") and rel.startswith("docs/"):
            path.write_text(f"# {Path(rel).parent.name}\n", encoding="utf-8")
        elif rel.endswith(".json"):
            path.write_text("{}\n", encoding="utf-8")
        else:
            path.write_text("---\nname: test\n---\n", encoding="utf-8")
    server_path = root / "agent" / "mcp" / "server.py"
    server_path.parent.mkdir(parents=True, exist_ok=True)
    server_path.write_text("# test runtime entrypoint\n", encoding="utf-8")


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _git_commit_all(repo: Path, message: str) -> str:
    _git(["add", "."], repo)
    _git(["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", message], repo)
    return _git(["rev-parse", "HEAD"], repo)


def _make_remote_plugin_repo(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    _git(["init", "--bare", str(remote)], tmp_path)
    source.mkdir()
    _git(["init"], source)
    _git(["checkout", "-b", "main"], source)
    _write_plugin_fixture(source)
    _git_commit_all(source, "initial plugin")
    _git(["remote", "add", "origin", str(remote)], source)
    _git(["push", "-u", "origin", "main"], source)
    return remote, source


def _clone_plugin_repo(remote: Path, install_root: Path) -> Path:
    plugin_root = plugin_root_for(str(remote), install_root)
    plugin_root.parent.mkdir(parents=True, exist_ok=True)
    _git(["clone", str(remote), str(plugin_root)], install_root)
    _git(["checkout", "main"], plugin_root)
    return plugin_root


def test_slug_from_repo_url_handles_https_and_git_suffix():
    assert slug_from_repo_url("https://github.com/amingclawdev/aming-claw.git") == "aming-claw"
    assert slug_from_repo_url("git@github.com:amingclawdev/aming-claw.git") == "aming-claw"


def test_validate_plugin_root_requires_expected_assets(tmp_path):
    with pytest.raises(PluginInstallError, match="plugin root is missing required files"):
        validate_plugin_root(tmp_path)

    _write_plugin_fixture(tmp_path)

    validated = validate_plugin_root(tmp_path)
    assert ".codex-plugin/plugin.json" in validated
    assert "skills/aming-claw/SKILL.md" in validated


def test_default_plugin_update_state_path_uses_user_state_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AMING_CLAW_PLUGIN_STATE_HOME", str(tmp_path / "state-home"))

    path = default_plugin_update_state_path()

    assert path == tmp_path / "state-home" / "aming-claw-local" / "aming-claw.json"


def test_plugin_update_state_status_warns_when_missing(tmp_path):
    result = plugin_update_state_status(state_path=tmp_path / "missing.json")

    assert result["ok"] is True
    assert result["status"] == "warn"
    assert result["update_status"] == "unknown"
    assert "not found" in result["warnings"][0]


def test_plugin_update_state_status_blocks_pending_restart(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "schema_version": 1,
        "plugin_id": "aming-claw@aming-claw-local",
        "update_status": "applied_pending_restart",
        "restart_required": {
            "mcp": {
                "required": True,
                "reason": "skills changed",
                "satisfied_by": "open a new session",
            }
        },
    }), encoding="utf-8")

    result = plugin_update_state_status(state_path=state_path)

    assert result["ok"] is False
    assert result["status"] == "fail"
    assert "mcp" in result["blockers"][0]
    assert "required" in format_plugin_update_state_status(result)


def test_plugin_update_state_status_blocks_failed_update(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "schema_version": 1,
        "plugin_id": "aming-claw@aming-claw-local",
        "update_status": "failed",
    }), encoding="utf-8")

    result = plugin_update_state_status(state_path=state_path)

    assert result["ok"] is False
    assert result["status"] == "fail"
    assert "failed" in result["blockers"][0]


def test_write_plugin_update_state_records_current_install(tmp_path):
    _write_plugin_fixture(tmp_path)
    state_path = write_plugin_update_state(
        plugin_root=tmp_path,
        repo_url="https://github.com/amingclawdev/aming-claw.git",
        state_path=tmp_path / "state" / "plugin.json",
    )

    result = plugin_update_state_status(state_path=state_path)

    assert result["ok"] is True
    assert result["status"] == "pass"
    assert result["state"]["installed_version"] == "0.1.0"
    assert result["state"]["plugin_root"] == str(tmp_path.resolve())
    assert result["self_graph_bundle"]["status"] == "pass"


def test_plugin_update_state_status_blocks_newer_self_bundle_major(tmp_path):
    _write_plugin_fixture(tmp_path)
    manifest_path = tmp_path / "agent" / "mcp" / "resources" / "self-graph-bundle-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["bundle_major"] = 2
    manifest["bundle_version"] = "2.0.0"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    state_path = write_plugin_update_state(
        plugin_root=tmp_path,
        repo_url="https://github.com/amingclawdev/aming-claw.git",
        state_path=tmp_path / "state" / "plugin.json",
    )

    result = plugin_update_state_status(state_path=state_path)

    assert result["ok"] is False
    assert result["status"] == "fail"
    assert "self graph bundle" in result["blockers"][0]
    assert result["self_graph_bundle"]["events"][0]["event_type"] == "plugin_update_reminder"


def test_plugin_changed_surface_classification_maps_restart_obligations():
    surfaces = classify_plugin_changed_surfaces([
        "skills/aming-claw/SKILL.md",
        "agent/governance/server.py",
        "agent/manager_http_server.py",
        "docs/governance/manual-fix-sop.md",
    ])
    restart = plugin_restart_required_for_surfaces(surfaces)

    assert surfaces == ["mcp", "governance", "service_manager"]
    assert restart["mcp"]["required"] is True
    assert "reload Codex" in restart["mcp"]["satisfied_by"]
    assert restart["governance"]["required"] is True
    assert restart["service_manager"]["required"] is True


def test_plugin_update_check_reports_current_checkout(tmp_path):
    remote, _source = _make_remote_plugin_repo(tmp_path)
    install_root = tmp_path / "install"
    _clone_plugin_repo(remote, install_root)
    state_path = tmp_path / "state.json"

    result = update_plugin_from_git(
        str(remote),
        install_root=install_root,
        install_package=False,
        install_codex_plugin=False,
        state_path=state_path,
    )

    assert result.ok is True
    assert result.status == "current"
    assert result.update_available is False
    assert result.remote_commit == result.installed_commit
    state = plugin_update_state_status(state_path=state_path)
    assert state["status"] == "pass"
    assert state["state"]["update_status"] == "current"


def test_plugin_update_check_reports_available_and_writes_state(tmp_path):
    remote, source = _make_remote_plugin_repo(tmp_path)
    install_root = tmp_path / "install"
    _clone_plugin_repo(remote, install_root)
    skill = source / "skills" / "aming-claw" / "SKILL.md"
    skill.write_text("---\nname: test\n---\nupdated\n", encoding="utf-8")
    remote_commit = _git_commit_all(source, "update skill")
    _git(["push", "origin", "main"], source)
    state_path = tmp_path / "state.json"

    result = update_plugin_from_git(
        str(remote),
        install_root=install_root,
        install_package=False,
        install_codex_plugin=False,
        state_path=state_path,
    )

    assert result.status == "available"
    assert result.update_available is True
    assert result.remote_commit == remote_commit
    assert "skills/aming-claw/SKILL.md" in result.changed_files
    assert result.changed_surfaces == ["mcp"]
    state = plugin_update_state_status(state_path=state_path)
    assert state["ok"] is True
    assert state["status"] == "warn"
    assert state["state"]["update_status"] == "available"
    assert state["state"]["remote_commit"] == remote_commit


def test_plugin_update_apply_fast_forwards_and_blocks_until_restart(tmp_path):
    remote, source = _make_remote_plugin_repo(tmp_path)
    install_root = tmp_path / "install"
    plugin_root = _clone_plugin_repo(remote, install_root)
    governance_file = source / "agent" / "governance" / "server.py"
    governance_file.parent.mkdir(parents=True, exist_ok=True)
    governance_file.write_text("# governance update\n", encoding="utf-8")
    remote_commit = _git_commit_all(source, "update governance")
    _git(["push", "origin", "main"], source)
    state_path = tmp_path / "state.json"

    result = update_plugin_from_git(
        str(remote),
        install_root=install_root,
        apply_update=True,
        install_package=False,
        install_codex_plugin=False,
        state_path=state_path,
    )

    assert result.ok is True
    assert result.applied is True
    assert result.status == "applied_pending_restart"
    assert _git(["rev-parse", "HEAD"], plugin_root) == remote_commit
    assert result.changed_surfaces == ["governance"]
    state = plugin_update_state_status(state_path=state_path)
    assert state["ok"] is False
    assert "governance" in state["blockers"][0]
    assert "applied_pending_restart" in format_plugin_update_result(result)


def test_plugin_update_failure_writes_failed_state(tmp_path):
    state_path = tmp_path / "state.json"

    result = update_plugin_from_git(
        "https://example.com/aming-claw.git",
        install_root=tmp_path / "missing-install",
        install_package=False,
        install_codex_plugin=False,
        state_path=state_path,
    )

    assert result.ok is False
    assert result.status == "failed"
    assert "plugin checkout not found" in result.error
    state = plugin_update_state_status(state_path=state_path)
    assert state["ok"] is False
    assert state["state"]["update_status"] == "failed"
    assert "plugin checkout not found" in state["state"]["last_error"]


def test_install_from_git_dry_run_plans_fresh_clone_without_writing(tmp_path):
    repo_url = "https://github.com/amingclawdev/aming-claw.git"

    result = install_from_git(
        repo_url,
        install_root=tmp_path,
        dry_run=True,
        install_package=False,
    )

    plugin_root = plugin_root_for(repo_url, tmp_path)
    assert not plugin_root.exists()
    assert result.dry_run is True
    assert result.validated_files == []
    assert result.commands[0].args[:2] == ["git", "clone"]
    assert "Claude Code: /plugin marketplace add" in "\n".join(result.next_steps)


def test_install_from_git_validate_only_existing_checkout(tmp_path):
    repo_url = "https://github.com/amingclawdev/aming-claw.git"
    plugin_root = plugin_root_for(repo_url, tmp_path)
    _write_plugin_fixture(plugin_root)

    result = install_from_git(
        repo_url,
        install_root=tmp_path,
        dry_run=True,
        validate_only=True,
        install_package=False,
    )

    assert result.validated_files
    assert result.commands == []
    assert str(plugin_root) in format_result(result)


def test_doctor_plugin_validates_aftercare_without_governance(tmp_path):
    _write_plugin_fixture(tmp_path)
    codex_home = tmp_path / "codex-home"
    marketplace_root = tmp_path / "marketplace-root"
    install_codex_plugin_cache(tmp_path, codex_home=codex_home)
    install_codex_marketplace(tmp_path, marketplace_root=marketplace_root)
    codex_config = configure_codex_plugin(
        codex_config=codex_home / "config.toml",
        marketplace_root=marketplace_root,
    )

    result = doctor_plugin(
        plugin_root=tmp_path,
        codex_config=codex_config,
        codex_home=codex_home,
        check_governance=False,
    )

    assert result.ok is True
    assert {check.name for check in result.checks} >= {
        "plugin_assets",
        "codex_marketplace",
        "codex_plugin_cache",
        "claude_marketplace",
        "claude_manifest",
        "mcp_config",
        "codex_config",
        "self_graph_bundle",
        "dashboard_static_assets",
        "ai_cli_openai",
        "ai_cli_anthropic",
    }
    assert "Restart/reload Codex" in format_doctor_result(result)
    assert "auth unknown" in format_doctor_result(result) or "missing" in format_doctor_result(result)
    assert "service_manager_health" not in {check.name for check in result.checks}
    assert "ServiceManager/executor checks are advanced" in format_doctor_result(result)


def test_ai_cli_check_uses_env_override(monkeypatch, tmp_path):
    fake_codex = tmp_path / "codex-custom"
    monkeypatch.setenv("CODEX_BIN", str(fake_codex))

    class _Proc:
        returncode = 0
        stdout = "codex-cli 9.9.9\n"
        stderr = ""

    def fake_run(args, **_kwargs):
        assert args == [str(fake_codex), "--version"]
        return _Proc()

    monkeypatch.setattr("agent.plugin_installer.subprocess.run", fake_run)

    check = _check_ai_cli("openai", AI_CLI_REQUIREMENTS["openai"])

    assert check.status == "ok"
    assert str(fake_codex) in check.detail
    assert "auth unknown" in check.detail


def test_doctor_plugin_flags_bad_marketplace_path(tmp_path):
    _write_plugin_fixture(tmp_path)
    marketplace = tmp_path / ".agents" / "plugins" / "marketplace.json"
    payload = json.loads(marketplace.read_text(encoding="utf-8"))
    payload["plugins"][0]["source"]["path"] = ".agents/plugins"
    marketplace.write_text(json.dumps(payload), encoding="utf-8")

    result = doctor_plugin(
        plugin_root=tmp_path,
        codex_config=tmp_path / "missing-config.toml",
        check_governance=False,
    )

    checks = {check.name: check for check in result.checks}
    assert checks["codex_marketplace"].status == "warn"
    assert ".codex-plugin/plugin.json" in checks["codex_marketplace"].detail


def test_doctor_plugin_rejects_empty_root_marketplace_path(tmp_path):
    _write_plugin_fixture(tmp_path)
    marketplace = tmp_path / ".agents" / "plugins" / "marketplace.json"
    payload = json.loads(marketplace.read_text(encoding="utf-8"))
    payload["plugins"][0]["source"]["path"] = "./"
    marketplace.write_text(json.dumps(payload), encoding="utf-8")

    result = doctor_plugin(
        plugin_root=tmp_path,
        codex_config=tmp_path / "missing-config.toml",
        check_governance=False,
    )

    checks = {check.name: check for check in result.checks}
    assert result.ok is False
    assert checks["codex_marketplace"].status == "fail"
    assert "empty local plugin path" in checks["codex_marketplace"].detail


def test_doctor_plugin_fails_when_enabled_cache_is_missing(tmp_path):
    _write_plugin_fixture(tmp_path)
    codex_home = tmp_path / "codex-home"
    marketplace_root = tmp_path / "marketplace-root"
    install_codex_marketplace(tmp_path, marketplace_root=marketplace_root)
    codex_config = configure_codex_plugin(
        codex_config=codex_home / "config.toml",
        marketplace_root=marketplace_root,
    )

    result = doctor_plugin(
        plugin_root=tmp_path,
        codex_config=codex_config,
        codex_home=codex_home,
        check_governance=False,
    )

    checks = {check.name: check for check in result.checks}
    assert result.ok is False
    assert checks["codex_plugin_cache"].status == "fail"
    assert "missing installed plugin cache" in checks["codex_plugin_cache"].detail


def test_install_codex_plugin_cache_uses_versioned_codex_loader_layout(tmp_path):
    _write_plugin_fixture(tmp_path)
    codex_home = tmp_path / "codex-home"

    target = install_codex_plugin_cache(tmp_path, codex_home=codex_home, python_executable="python3.12")

    assert target == codex_home / "plugins" / "cache" / "aming-claw-local" / "aming-claw" / "0.1.0"
    assert (target / ".codex-plugin" / "plugin.json").is_file()
    assert (target / "skills" / "aming-claw" / "SKILL.md").is_file()
    assert (target / "skills" / "aming-claw-hn-demo" / "SKILL.md").is_file()
    assert (target / "skills" / "aming-claw-hn-demo-before-work" / "SKILL.md").is_file()
    assert (target / "skills" / "aming-claw-hn-demo-during-work" / "SKILL.md").is_file()
    assert (target / "skills" / "aming-claw-hn-demo-after-work" / "SKILL.md").is_file()
    assert (target / "skills" / "aming-claw-vibe-queue-demo" / "SKILL.md").is_file()
    assert (target / "skills" / "aming-claw-drift-demo" / "SKILL.md").is_file()
    assert (target / "skills" / "aming-claw-backlog-dupe-demo" / "SKILL.md").is_file()
    assert (target / "frontend" / "dashboard" / "scripts" / "e2e-hn-demo.mjs").is_file()
    assert (target / "frontend" / "dashboard" / "scripts" / "e2e-vibe-queue-fixture.mjs").is_file()
    assert (target / "frontend" / "dashboard" / "scripts" / "e2e-drift-demo-fixture.mjs").is_file()
    assert (target / "frontend" / "dashboard" / "scripts" / "e2e-backlog-dupe-fixture.mjs").is_file()
    assert (target / "docs" / "vibe-queue-demo" / "README.md").is_file()
    assert (target / "docs" / "drift-demo" / "README.md").is_file()
    assert (target / "docs" / "backlog-dupe-demo" / "README.md").is_file()
    assert not (target / "agent" / "mcp" / "server.py").exists()

    mcp = json.loads((target / ".mcp.json").read_text(encoding="utf-8"))
    server = mcp["mcpServers"]["aming-claw"]
    assert server["command"] == "python3.12"
    assert server["cwd"] == str(tmp_path.resolve())
    assert str(tmp_path.resolve()) in server["env"]["PYTHONPATH"].split(":") or str(tmp_path.resolve()) in server["env"]["PYTHONPATH"].split(";")
    assert server["args"][:2] == ["-m", "agent.mcp.server"]


def test_install_codex_plugin_cache_replaces_existing_symlink_payload(tmp_path):
    _write_plugin_fixture(tmp_path)
    codex_home = tmp_path / "codex-home"
    target = install_codex_plugin_cache(tmp_path, codex_home=codex_home)
    linked_payload = tmp_path / "linked-payload"
    linked_payload.mkdir()
    (target / "skills").rename(target / "skills-old")
    try:
        (target / "skills").symlink_to(linked_payload, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    refreshed = install_codex_plugin_cache(tmp_path, codex_home=codex_home)

    assert refreshed == target
    assert not (target / "skills").is_symlink()
    assert (target / "skills" / "aming-claw" / "SKILL.md").is_file()


def test_install_codex_marketplace_replaces_existing_symlink_payload(tmp_path):
    _write_plugin_fixture(tmp_path)
    marketplace_root = install_codex_marketplace(tmp_path, marketplace_root=tmp_path / "marketplace-root")
    plugin_target = marketplace_root / ".agents" / "plugins" / "aming-claw"
    linked_payload = tmp_path / "linked-marketplace-payload"
    linked_payload.mkdir()
    (plugin_target / "skills").rename(plugin_target / "skills-old")
    try:
        (plugin_target / "skills").symlink_to(linked_payload, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    refreshed = install_codex_marketplace(tmp_path, marketplace_root=marketplace_root)

    assert refreshed == marketplace_root
    assert not (plugin_target / "skills").is_symlink()
    assert (plugin_target / "skills" / "aming-claw" / "SKILL.md").is_file()


def test_doctor_plugin_fails_when_cache_payload_is_partial(tmp_path):
    _write_plugin_fixture(tmp_path)
    codex_home = tmp_path / "codex-home"
    marketplace_root = tmp_path / "marketplace-root"
    cache_target = install_codex_plugin_cache(tmp_path, codex_home=codex_home)
    install_codex_marketplace(tmp_path, marketplace_root=marketplace_root)
    codex_config = configure_codex_plugin(
        codex_config=codex_home / "config.toml",
        marketplace_root=marketplace_root,
    )
    shutil.rmtree(cache_target / "skills")

    result = doctor_plugin(
        plugin_root=tmp_path,
        codex_config=codex_config,
        codex_home=codex_home,
        check_governance=False,
    )

    checks = {check.name: check for check in result.checks}
    assert result.ok is False
    assert checks["codex_plugin_cache"].status == "fail"
    assert "missing payload skills" in checks["codex_plugin_cache"].detail


def test_codex_install_surfaces_do_not_write_external_project_cwd(tmp_path, monkeypatch):
    plugin_root = tmp_path / "plugin-root"
    _write_plugin_fixture(plugin_root)
    external_project = tmp_path / "my-app"
    (external_project / "src").mkdir(parents=True)
    (external_project / "src" / "App.js").write_text("export default function App() { return null; }\n", encoding="utf-8")
    monkeypatch.chdir(external_project)

    codex_home = tmp_path / "codex-home"
    marketplace_root = tmp_path / "marketplace-root"

    cache_target = install_codex_plugin_cache(plugin_root, codex_home=codex_home)
    marketplace_target = install_codex_marketplace(plugin_root, marketplace_root=marketplace_root)
    configure_codex_plugin(
        codex_config=codex_home / "config.toml",
        marketplace_root=marketplace_target,
    )

    assert (cache_target / ".mcp.json").is_file()
    assert (marketplace_target / ".agents" / "plugins" / "aming-claw" / ".mcp.json").is_file()
    assert (cache_target / "frontend" / "dashboard" / "scripts" / "e2e-hn-demo.mjs").is_file()
    assert (
        marketplace_target / ".agents" / "plugins" / "aming-claw" / "frontend" / "dashboard" / "scripts" / "e2e-hn-demo.mjs"
    ).is_file()
    for rel in (
        ".mcp.json",
        "shared-volume",
        ".codex-plugin",
        ".claude-plugin",
        ".agents/plugins",
        "agent/mcp/resources",
    ):
        assert not (external_project / rel).exists(), f"unexpected target-local plugin artifact: {rel}"


def test_doctor_plugin_fails_when_cache_mcp_cannot_import_runtime(tmp_path):
    _write_plugin_fixture(tmp_path)
    codex_home = tmp_path / "codex-home"
    marketplace_root = tmp_path / "marketplace-root"
    cache_target = install_codex_plugin_cache(tmp_path, codex_home=codex_home)
    install_codex_marketplace(tmp_path, marketplace_root=marketplace_root)
    codex_config = configure_codex_plugin(
        codex_config=codex_home / "config.toml",
        marketplace_root=marketplace_root,
    )
    mcp_path = cache_target / ".mcp.json"
    payload = json.loads(mcp_path.read_text(encoding="utf-8"))
    payload["mcpServers"]["aming-claw"]["cwd"] = "."
    payload["mcpServers"]["aming-claw"]["env"].pop("PYTHONPATH", None)
    mcp_path.write_text(json.dumps(payload), encoding="utf-8")

    result = doctor_plugin(
        plugin_root=tmp_path,
        codex_config=codex_config,
        codex_home=codex_home,
        check_governance=False,
    )

    checks = {check.name: check for check in result.checks}
    assert result.ok is False
    assert checks["codex_plugin_cache"].status == "fail"
    assert "cannot import agent.mcp.server" in checks["codex_plugin_cache"].detail


def test_configure_codex_plugin_enables_plugin_and_valid_marketplace(tmp_path):
    _write_plugin_fixture(tmp_path)
    marketplace_root = install_codex_marketplace(tmp_path, marketplace_root=tmp_path / "marketplace-root")
    config_path = configure_codex_plugin(
        codex_config=tmp_path / "config.toml",
        marketplace_root=marketplace_root,
    )
    text = config_path.read_text(encoding="utf-8")
    parsed = _load_toml_text(text)

    assert f'[plugins."{CODEX_PLUGIN_ID}"]' in text
    assert "enabled = true" in text
    assert parsed["marketplaces"]["aming-claw-local"]["source"] == str(marketplace_root.resolve())
    assert f"source = '{marketplace_root.resolve()}'" in text


def test_upsert_toml_table_replaces_windows_path_without_regex_escape_error():
    old_text = "[marketplaces.aming-claw-local]\nsource = 'old'\n"
    windows_path = "C:" + "\\Users\\z5866\\.aming-claw\\plugins\\aming-claw"

    text = _upsert_toml_table(
        old_text,
        "marketplaces.aming-claw-local",
        f"source_type = \"local\"\nsource = '{windows_path}'",
    )

    parsed = _load_toml_text(text)
    assert parsed["marketplaces"]["aming-claw-local"]["source"] == windows_path


def test_doctor_plugin_fails_on_invalid_codex_config_toml(tmp_path):
    _write_plugin_fixture(tmp_path)
    codex_config = tmp_path / "config.toml"
    codex_config.write_text("[plugins.\n", encoding="utf-8")

    result = doctor_plugin(
        plugin_root=tmp_path,
        codex_config=codex_config,
        codex_home=tmp_path / "codex-home",
        check_governance=False,
    )

    checks = {check.name: check for check in result.checks}
    assert result.ok is False
    assert checks["codex_config"].status == "fail"
    assert "invalid TOML" in checks["codex_config"].detail


def test_doctor_plugin_rejects_manifest_with_too_many_default_prompts(tmp_path):
    _write_plugin_fixture(tmp_path)
    manifest = tmp_path / ".codex-plugin" / "plugin.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["interface"] = {"defaultPrompt": ["one", "two", "three", "four"]}
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = doctor_plugin(
        plugin_root=tmp_path,
        codex_config=tmp_path / "missing-config.toml",
        check_governance=False,
    )

    checks = {check.name: check for check in result.checks}
    assert result.ok is False
    assert checks["codex_manifest"].status == "fail"
    assert "at most 3" in checks["codex_manifest"].detail


def test_install_from_git_rejects_unsupported_python_before_pip(tmp_path, monkeypatch):
    repo_url = "https://github.com/amingclawdev/aming-claw.git"
    plugin_root = plugin_root_for(repo_url, tmp_path)
    _write_plugin_fixture(plugin_root)

    def fake_run(args, **_kwargs):
        class _Proc:
            returncode = 0
            stdout = "Python 3.8.18\n"
            stderr = ""

        assert args == ["old-python", "--version"]
        return _Proc()

    monkeypatch.setattr("agent.plugin_installer.subprocess.run", fake_run)

    with pytest.raises(PluginInstallError, match="requires Python 3.9"):
        install_from_git(
            repo_url,
            install_root=tmp_path,
            validate_only=True,
            python_executable="old-python",
            install_package=True,
        )


def test_check_claude_marketplace_passes_on_valid_manifest(tmp_path):
    claude_dir = tmp_path / ".claude-plugin"
    claude_dir.mkdir()
    (claude_dir / "marketplace.json").write_text(
        json.dumps({
            "name": "aming-claw-local",
            "metadata": {"description": "Test marketplace."},
            "owner": {"name": "Aming Claw"},
            "plugins": [
                {"name": "aming-claw", "source": "./", "version": "0.1.0"}
            ],
        }),
        encoding="utf-8",
    )
    check = _check_claude_marketplace(tmp_path)
    assert check.status == "ok"


def test_check_claude_marketplace_fails_on_bare_dot_source(tmp_path):
    """MF #1 P0: claude plugin validate rejects plugins[].source=='.' as Invalid input."""
    claude_dir = tmp_path / ".claude-plugin"
    claude_dir.mkdir()
    (claude_dir / "marketplace.json").write_text(
        json.dumps({
            "name": "aming-claw-local",
            "metadata": {"description": "Test."},
            "owner": {"name": "Aming Claw"},
            "plugins": [
                {"name": "aming-claw", "source": ".", "version": "0.1.0"}
            ],
        }),
        encoding="utf-8",
    )
    check = _check_claude_marketplace(tmp_path)
    assert check.status == "fail"
    assert "must start with './'" in check.detail


def test_check_claude_marketplace_warns_on_missing_metadata_description(tmp_path):
    """MF #1 secondary: claude plugin validate warns when metadata.description is missing."""
    claude_dir = tmp_path / ".claude-plugin"
    claude_dir.mkdir()
    (claude_dir / "marketplace.json").write_text(
        json.dumps({
            "name": "aming-claw-local",
            "owner": {"name": "Aming Claw"},
            "plugins": [
                {"name": "aming-claw", "source": "./", "version": "0.1.0"}
            ],
        }),
        encoding="utf-8",
    )
    check = _check_claude_marketplace(tmp_path)
    assert check.status == "warn"
    assert "metadata.description" in check.detail


def test_check_claude_manifest_passes_when_mcpservers_declared(tmp_path):
    """MF #2a: declared mcpServers is the manifest-level fix."""
    claude_dir = tmp_path / ".claude-plugin"
    claude_dir.mkdir()
    (claude_dir / "plugin.json").write_text(
        json.dumps({
            "name": "aming-claw",
            "version": "0.1.0",
            "description": "Test plugin.",
            "mcpServers": {
                "aming-claw": {
                    "command": "python",
                    "args": ["-m", "agent.mcp.server"],
                }
            },
        }),
        encoding="utf-8",
    )
    check = _check_claude_manifest(tmp_path)
    assert check.status == "ok"


def test_check_claude_manifest_warns_when_no_mcpservers(tmp_path):
    """Without mcpServers the Claude plugin install will not expose an MCP server."""
    claude_dir = tmp_path / ".claude-plugin"
    claude_dir.mkdir()
    (claude_dir / "plugin.json").write_text(
        json.dumps({
            "name": "aming-claw",
            "version": "0.1.0",
            "description": "Test plugin.",
        }),
        encoding="utf-8",
    )
    check = _check_claude_manifest(tmp_path)
    assert check.status == "warn"
    assert "mcpServers" in check.detail


def test_check_claude_manifest_fails_when_required_field_missing(tmp_path):
    claude_dir = tmp_path / ".claude-plugin"
    claude_dir.mkdir()
    (claude_dir / "plugin.json").write_text(
        json.dumps({
            "name": "aming-claw",
            "version": "0.1.0",
            # description intentionally missing
        }),
        encoding="utf-8",
    )
    check = _check_claude_manifest(tmp_path)
    assert check.status == "fail"
    assert "description" in check.detail
