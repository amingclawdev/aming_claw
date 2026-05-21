"""Tests for package and local-plugin installation contracts."""

import importlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load_pyproject() -> dict:
    try:
        import tomllib
        with (ROOT / "pyproject.toml").open("rb") as f:
            return tomllib.load(f)
    except ImportError:
        try:
            import pip._vendor.tomli as _tomli
            return _tomli.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        except (ImportError, AttributeError):
            import tomli as _tomli2
            with (ROOT / "pyproject.toml").open("rb") as f:
                return _tomli2.load(f)


class TestImportWithoutRedis:
    """AC5: agent package imports without redis installed."""

    def test_agent_import(self):
        import agent
        assert hasattr(agent, "AmingConfig")

    def test_redis_client_has_fallback(self):
        from agent.governance.redis_client import HAS_REDIS
        # HAS_REDIS may be True or False depending on environment,
        # but the import itself must not fail.
        assert isinstance(HAS_REDIS, bool)


class TestPublicAPI:
    """AC7: from aming_claw import AmingConfig works."""

    def test_aming_claw_import(self):
        mod = importlib.import_module("aming_claw")
        assert hasattr(mod, "AmingConfig")

    def test_agent_public_api(self):
        from agent import AmingConfig, bootstrap_project, create_task
        assert callable(bootstrap_project)
        assert callable(create_task)


class TestPyprojectOptionalDeps:
    """AC10: pyproject.toml has optional dependency groups."""

    def test_optional_deps_in_toml(self):
        data = _load_pyproject()
        opt = data["project"]["optional-dependencies"]
        assert "redis" in opt
        assert "docker" in opt
        assert "full" in opt

    def test_project_license_points_to_fsl_file(self):
        data = _load_pyproject()
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")

        assert data["project"]["license"] == {"file": "LICENSE"}
        assert "Functional Source License, Version 1.1, MIT Future License" in license_text
        assert "FSL-1.1-MIT" in license_text
        assert "Copyright 2026 Aming Claw" in license_text


class TestPackagedDashboardAssets:
    def test_pyproject_includes_dashboard_dist_package_data(self):
        data = _load_pyproject()
        package_data = data["tool"]["setuptools"]["package-data"]
        find_config = data["tool"]["setuptools"]["packages"]["find"]

        assert package_data["agent.governance.dashboard_dist"] == ["**/*"]
        assert package_data["agent.mcp"] == ["resources/*"]
        assert find_config["namespaces"] is True
        assert "agent.tests*" in find_config["exclude"]
        assert "agent.governance.chain_history*" in find_config["exclude"]
        assert (ROOT / "agent" / "governance" / "dashboard_dist" / "assets").is_dir()

    def test_manifest_includes_dashboard_and_plugin_assets(self):
        manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

        assert "recursive-include agent/governance/dashboard_dist *" in manifest
        assert "recursive-include agent/mcp/resources *" in manifest
        assert "recursive-include skills/aming-claw *" in manifest
        assert "recursive-include skills/aming-claw-launcher *" in manifest
        assert "recursive-include docs/assets *.png" in manifest
        assert "include LICENSE" in manifest
        assert "include .codex-plugin/plugin.json" in manifest
        assert "include .claude-plugin/plugin.json" in manifest
        assert "include .claude-plugin/marketplace.json" in manifest
        assert "include .agents/plugins/marketplace.json" in manifest
        assert "include CLAUDE.md" in manifest
        assert "include scripts/install_from_git.py" in manifest
        assert "include scripts/check_self_graph_bundle.py" in manifest

    def test_dashboard_build_sync_script_is_wired_to_npm_build(self):
        package_json = json.loads((ROOT / "frontend" / "dashboard" / "package.json").read_text(encoding="utf-8"))

        assert (ROOT / "frontend" / "dashboard" / "scripts" / "sync-dist-to-python-package.mjs").is_file()
        assert "sync-dist-to-python-package.mjs" in package_json["scripts"]["build"]

    def test_cross_platform_package_build_script_exists(self):
        script = ROOT / "scripts" / "build_package.py"

        assert script.is_file()
        assert "npm" in script.read_text(encoding="utf-8")
        assert "build_meta.build_wheel" in script.read_text(encoding="utf-8")


class TestLocalPluginPackaging:
    """MVP contract for Codex/Claude local-plugin packaging."""

    def test_codex_plugin_manifest_points_to_existing_assets(self):
        manifest = json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))

        assert manifest["name"] == "aming-claw"
        assert manifest["license"] == "FSL-1.1-MIT"
        assert (ROOT / manifest["skills"]).is_dir()
        assert (ROOT / manifest["mcpServers"]).is_file()
        assert "MCP" in manifest["interface"]["capabilities"]
        assert "MCP resources" in manifest["interface"]["longDescription"]
        assert any("aming-claw://skill" in prompt for prompt in manifest["interface"]["defaultPrompt"])
        assert any("aming-claw://seed-graph-summary" in prompt for prompt in manifest["interface"]["defaultPrompt"])
        assert (ROOT / "CLAUDE.md").is_file()

    def test_codex_repo_marketplace_compatibility_metadata_exists(self):
        marketplace_path = ROOT / ".agents" / "plugins" / "marketplace.json"
        marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))

        assert marketplace["name"] == "aming-claw-local"
        assert marketplace["interface"]["displayName"] == "Aming Claw Local"
        plugins = marketplace["plugins"]
        assert len(plugins) == 1
        plugin = plugins[0]
        assert plugin["name"] == "aming-claw"
        assert plugin["source"] == {"source": "local", "path": "./."}
        assert plugin["policy"]["installation"] == "INSTALLED_BY_DEFAULT"
        assert plugin["policy"]["authentication"] == "ON_INSTALL"
        assert plugin["category"] == "Productivity"
        assert (ROOT / plugin["source"]["path"] / ".codex-plugin" / "plugin.json").is_file()

    def test_mcp_seed_graph_resource_is_packaged(self):
        seed = ROOT / "agent" / "mcp" / "resources" / "seed-graph-summary.json"
        data = json.loads(seed.read_text(encoding="utf-8"))

        assert data["project_id"] == "aming-claw"
        assert data["mvp_boundaries"]["primary"]
        manifest = ROOT / "agent" / "mcp" / "resources" / "self-graph-bundle-manifest.json"
        bundle = json.loads(manifest.read_text(encoding="utf-8"))
        assert bundle["bundle_major"] == 1
        assert bundle["resources"][0]["path"] == "agent/mcp/resources/seed-graph-summary.json"

    def test_mcp_config_is_relocatable_and_uses_stdio_module_entrypoint(self):
        config_text = (ROOT / ".mcp.json").read_text(encoding="utf-8")
        config = json.loads(config_text)
        server = config["mcpServers"]["aming-claw"]

        assert server["command"] == "python"
        assert server["args"][:2] == ["-m", "agent.mcp.server"]
        assert "--workers" in server["args"]
        assert server["args"][server["args"].index("--workers") + 1] == "0"
        assert server["cwd"] == "."
        assert "C:\\Users\\" not in config_text

    def test_pyproject_exposes_governance_console_scripts(self):
        data = _load_pyproject()

        scripts = data["project"]["scripts"]
        assert scripts["aming-claw"] == "agent.cli:main"
        assert scripts["aming-governance"] == "agent.governance.server:main"
        assert scripts["aming-governance-host"] == "start_governance:main"

    def test_git_url_plugin_installer_script_is_packaged(self):
        script = ROOT / "scripts" / "install_from_git.py"

        assert script.is_file()
        assert "agent.plugin_installer" in script.read_text(encoding="utf-8")

        from agent.plugin_installer import DEFAULT_REPO_URL, REQUIRED_PLUGIN_FILES

        assert DEFAULT_REPO_URL == "https://github.com/amingclawdev/aming-claw"
        assert ".codex-plugin/plugin.json" in REQUIRED_PLUGIN_FILES
        assert ".claude-plugin/plugin.json" in REQUIRED_PLUGIN_FILES

    def test_codex_repo_marketplace_path_is_a_compatibility_path(self):
        marketplace_path = ROOT / ".agents" / "plugins" / "marketplace.json"
        marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
        plugin = next(item for item in marketplace["plugins"] if item["name"] == "aming-claw")
        source_path = plugin["source"]["path"]

        # This repo-local metadata is kept for compatibility. The real Codex
        # CLI loader is validated through the generated marketplace plus the
        # versioned plugins/cache layout installed by agent.plugin_installer.
        assert source_path.startswith("./")
        assert source_path not in {".", "./"}
        resolved = (ROOT / source_path).resolve()
        assert (resolved / ".codex-plugin" / "plugin.json").is_file()
        # Resolving this file relative to .agents/plugins demonstrates why the
        # installer must generate a Codex-compatible marketplace root.
        assert not ((marketplace_path.parent / source_path) / ".codex-plugin" / "plugin.json").is_file()

    def test_readme_git_install_does_not_inline_long_running_start(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        install_lines = [
            line
            for line in readme.splitlines()
            if "install_from_git.py" in line
            or "aming-claw plugin install https://github.com/amingclawdev/aming-claw" in line
        ]
        assert install_lines
        assert all("--start" not in line for line in install_lines)
        assert "long-running service command" in readme
        assert "Start-Process powershell" in readme
        assert "nohup python3 -m agent.cli start" in readme
        assert "plugin doctor" in readme
        assert "open a new Codex session" in readme


class TestClaudePluginPackaging:
    """MVP contract for Claude Code local-plugin packaging (parity with Codex)."""

    def test_claude_plugin_manifest_exists_and_is_valid_json(self):
        manifest_path = ROOT / ".claude-plugin" / "plugin.json"
        assert manifest_path.is_file(), "missing .claude-plugin/plugin.json"

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        assert manifest["name"] == "aming-claw"
        assert manifest.get("description"), "manifest must declare a description"
        assert manifest["homepage"] == "https://github.com/amingclawdev/aming-claw"
        assert manifest["repository"] == "https://github.com/amingclawdev/aming-claw"

    def test_claude_plugin_auto_discovered_assets_exist(self):
        # Claude Code auto-discovers skills/ and .mcp.json from the plugin root,
        # so the manifest itself does not have to point at them. We assert they
        # exist where the runtime expects them.
        assert (ROOT / "skills" / "aming-claw" / "SKILL.md").is_file()
        assert (ROOT / "skills" / "aming-claw-launcher" / "SKILL.md").is_file()
        assert (ROOT / ".mcp.json").is_file()
        assert (ROOT / "CLAUDE.md").is_file()

    def test_claude_launcher_skill_documents_cli_surface(self):
        skill = (ROOT / "skills" / "aming-claw-launcher" / "SKILL.md").read_text(encoding="utf-8")

        assert skill.startswith("---"), "launcher skill must start with YAML frontmatter"
        assert "name: aming-claw-launcher" in skill
        assert "description:" in skill
        # Launcher skill must document the preview/start/open/status surface.
        for command in (
            "aming-claw launcher",
            "aming-claw start",
            "aming-claw status",
            "aming-claw open",
            "aming-claw plugin install",
        ):
            assert command in skill, f"launcher skill missing reference to `{command}`"
        # Launcher skill must hand off to the main governance skill for non-preview work.
        assert "aming-claw/SKILL.md" in skill

    def test_claude_plugin_manifest_does_not_leak_user_machine_paths(self):
        manifest_text = (ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        assert "C:\\Users\\" not in manifest_text
        assert "/home/" not in manifest_text
        assert "web3ToolBoxDev" not in manifest_text
        assert "aming_claw.git" not in manifest_text

    def test_claude_local_marketplace_points_to_root_plugin(self):
        marketplace_path = ROOT / ".claude-plugin" / "marketplace.json"
        assert marketplace_path.is_file(), "missing .claude-plugin/marketplace.json"

        marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))

        assert marketplace["name"] == "aming-claw-local"
        assert marketplace["metadata"]["description"], "marketplace must declare metadata.description"
        assert marketplace["owner"]["name"], "marketplace must declare an owner name"

        plugins = marketplace["plugins"]
        assert len(plugins) == 1
        plugin = plugins[0]
        assert plugin["name"] == "aming-claw"
        # Same-repo plugin: marketplace root == plugin root == repo root.
        # Source must be "./" not bare "." — Claude Code rejects "." as Invalid input.
        assert plugin["source"] == "./"
        assert (ROOT / plugin["source"] / ".claude-plugin" / "plugin.json").is_file()

    def test_claude_plugin_declares_mcp_servers(self):
        """Claude plugin manifest must declare mcpServers so plugin install exposes the MCP server (otherwise Claude Code reports MCP servers (0))."""
        manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        servers = manifest.get("mcpServers")
        assert servers, "Claude plugin manifest must declare mcpServers"
        assert "aming-claw" in servers, "mcpServers must include 'aming-claw'"
        aming = servers["aming-claw"]
        assert aming.get("command") == "python"
        args = aming.get("args") or []
        assert "-m" in args and "agent.mcp.server" in args
        # cwd must use ${CLAUDE_PLUGIN_ROOT} so python -m agent.mcp.server can
        # locate the agent package in the plugin install dir, not the caller's
        # CWD which is unpredictable after install.
        assert aming.get("cwd") == "${CLAUDE_PLUGIN_ROOT}"
