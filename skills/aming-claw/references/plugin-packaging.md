# Plugin Packaging Notes

This repo is treated as the plugin root for the initial Aming Claw plugin package.

## MVP Status

- Codex local plugin shape is present: `.codex-plugin/plugin.json`,
  `skills/`, demo docs/scripts, and `.mcp.json`.
- Claude Code local plugin shape is present: `.claude-plugin/plugin.json`
  and `.claude-plugin/marketplace.json` at the repo root; `skills/` and
  `.mcp.json` are auto-discovered. Skills are namespaced
  `/aming-claw:<skill-name>`. After plugin updates, restart or reload the
  AI host so the slash-command index re-reads newly added skills.
- MCP runs through the stdio module entrypoint:
  `python -m agent.mcp.server --project aming-claw --workers 0`.
- Governance stays host-owned. Plugin MCP sessions should query/control
  governance and should not spawn duplicate executor workers. ServiceManager is
  an advanced chain/ops surface, not a V1 first-run requirement.
- The dashboard is served by governance at `/dashboard`. The root path `/` is
  not the dashboard and may return `404`.
- Dashboard static assets are required. No build is needed if
  `agent/governance/dashboard_dist/index.html` or
  `frontend/dashboard/dist/index.html` exists. Raw checkouts missing both should
  run `npm --prefix frontend/dashboard install` and
  `npm --prefix frontend/dashboard run build` before static smoke testing.
- Pip install works as a Python package entrypoint. The release build must run
  `npm --prefix frontend/dashboard run build` first; that command syncs the
  dashboard into `agent/governance/dashboard_dist` so the wheel can serve
  `/dashboard` without a target-machine npm build.
- A plugin launcher is explicit: `aming-claw launcher` writes a local HTML
  entry artifact with status/start guidance and a dashboard link. It does not
  auto-start governance or advanced ServiceManager/chain services.
- Git URL bootstrap is explicit: `aming-claw plugin install <repo-url>` and
  `python scripts/install_from_git.py <repo-url>` clone/update a user-local
  checkout, validate Codex/Claude plugin assets, optionally pip-install the
  runtime, install the versioned Codex plugin cache, generate a Codex-compatible
  local marketplace, update Codex config, and print next steps. They do not
  silently install credentials.
- Git URL update is explicit: `aming-claw plugin update --check` fetches the
  configured remote and writes local update state, while
  `aming-claw plugin update --apply` fast-forwards the checkout, refreshes the
  Python/Codex install surfaces, and records whether MCP or governance must be
  reloaded/restarted before MF close. ServiceManager restart obligations are
  advanced chain/ops checks, not default plugin aftercare. After those
  actions are complete, rerun `aming-claw plugin update --check` to mark the
  installed commit current.
- Installing plugin assets, installing the Python package, starting governance,
  serving the dashboard, loading MCP tools in the current Codex/Claude session,
  and optional chain/executor readiness are separate states. After plugin
  install or update, open a new editor session before expecting new skills/MCP
  tools.

## Layout

- `.codex-plugin/plugin.json`: Codex local plugin manifest (explicit
  `skills` and `mcpServers` pointers).
- `.agents/plugins/marketplace.json`: repo-local compatibility metadata. Real
  Codex CLI loading is verified against the installed cache
  `plugins/cache/aming-claw-local/aming-claw/<version>/` plus a generated local
  marketplace whose plugin payload lives inside that marketplace root. Do not
  treat a current-session reload as the fix for cache/marketplace errors.
- `.claude-plugin/plugin.json`: Claude Code plugin manifest. `skills/` and
  `.mcp.json` are auto-discovered from the plugin root, so the manifest only
  declares `name` + `description` + metadata.
- `.claude-plugin/marketplace.json`: repo-local Claude Code marketplace entry
  with one plugin (`source: "."`), so `/plugin marketplace add <repo>` then
  `/plugin install aming-claw@aming-claw-local` works without an external
  registry.
- `skills/aming-claw/`: main governance skill loaded for graph, backlog, MF,
  semantic, and chain work.
- `skills/aming-claw-launcher/`: onboarding skill loaded for preview, start,
  status, and dashboard flows.
- `skills/aming-claw-hn-challenge/`: public HN multi-agent challenge skill.
- `skills/aming-claw-hn-demo*/`: compatibility and supporting case walkthrough
  skills for the older before/during/after-work narrative.
- `skills/aming-claw-vibe-queue-demo/`,
  `skills/aming-claw-drift-demo/`, and
  `skills/aming-claw-backlog-dupe-demo/`: everyday AI coding demos for
  requirement queueing, stale-doc drift, and duplicate backlog detection.
- `frontend/dashboard/scripts/e2e-hn-demo.mjs`: packaged first-run HN demo
  runner. It can create the isolated fixture with `--ensure-fixture
  --no-browser` without requiring the full dashboard source tree or npm install.
- `.mcp.json`: active MCP server config using `agent.mcp.server`.
- `agent/plugin_installer.py` + `scripts/install_from_git.py`: Git URL plugin
  installer used by the CLI and first-run fallback flows.

The Codex manifest points to:

```json
{
  "skills": "./skills/",
  "mcpServers": "./.mcp.json"
}
```

The Claude Code manifest relies on auto-discovery for `skills/`; it declares
the MCP server directly so plugin-mode sessions can expose the same stdio
server as `.mcp.json`. Sample shape:

```json
{
  "name": "aming-claw",
  "version": "0.1.1",
  "description": "Graph-first governance workflow guard ...",
  "author": { "name": "Aming Claw" },
  "keywords": ["governance", "graph", "mcp"],
  "mcpServers": {
    "aming-claw": {
      "command": "python",
      "args": ["-m", "agent.mcp.server"]
    }
  }
}
```

## MCP Config

The active MCP server entrypoint is:

```text
python -m agent.mcp.server --project aming-claw --workers 0 --governance-url http://localhost:40000
```

Keep `--workers 0` for normal editor/plugin sessions. External executor
lifecycle belongs to the advanced chain/ops path, not the V1 dashboard, graph,
backlog, Review Queue, or Manual Fix path.
Redis event forwarding is off by default for local plugin sessions; use
`MCP_ENABLE_EVENTS=1` or `--enable-events` only when push notifications are
needed.

`.mcp.json` must remain relocatable. Use `"cwd": "."` and avoid absolute
developer-machine paths such as `C:\Users\...`; package tests enforce this.

Claude Code can read project-scoped MCP servers from `.mcp.json` when the
workspace/plugin host loads that file. Treat MCP visibility as a separate
runtime check: plugin skill install does not by itself prove Claude Code loaded
`mcpServers`. Prefer `claude mcp list` or a new-session tool visibility check;
`claude plugin details` can report `MCP servers (0)` even when the plugin
server connects. Codex local plugin packaging reads the plugin manifest's
`mcpServers` pointer. Keeping the same stdio entrypoint at `.mcp.json` lets both
surfaces reuse one MCP contract.

## Compatibility Checks

Run these before publishing a local plugin bundle or pip package:

```text
python -m pytest agent/tests/test_package_install.py agent/tests/test_mcp_server_stdio.py agent/tests/test_dashboard_static_route.py -q
python -m pytest agent/tests/test_plugin_installer.py agent/tests/test_cli.py -q
npm --prefix frontend/dashboard run build
python scripts/build_package.py --skip-dashboard-build
node frontend/dashboard/scripts/e2e-trunk.mjs --probe --static-route --dashboard http://localhost:40000/dashboard
```

Directory picker smoke: `/api/local/choose-directory` should prefer `tkinter`
and then use PowerShell on Windows, `osascript` on macOS, and `zenity`/`kdialog`
on Linux. Manual path entry remains the documented fallback when no GUI picker
is available.

## Packaging Gap Matrix

| Surface | Current | Gap Before Public Release |
| --- | --- | --- |
| Pip package | `pyproject.toml` exposes `aming-claw`, `aming-governance`, and `aming-governance-host`; dashboard assets are synced into `agent/governance/dashboard_dist` before wheel build. | Run clean wheel install smoke on each release target. |
| Codex local plugin | `.codex-plugin/plugin.json` points at skills and `.mcp.json`; `aming-claw plugin install <git-url>` prepares a user-local checkout, installs a versioned Codex cache entry, writes a generated marketplace, and enables `aming-claw@aming-claw-local` in Codex config. Tests ensure paths exist, cache layout matches the real loader, and `.mcp.json` is relocatable. | Sanitize env and host URLs before publishing outside trusted local/team installs. Host-native "paste Git URL to install" support still depends on the editor/plugin host. |
| Claude Code local plugin | `.claude-plugin/plugin.json` at repo root, `.claude-plugin/marketplace.json`, skills, and `.mcp.json`. Skills should be visible as `/aming-claw:aming-claw` and `/aming-claw:aming-claw-launcher`; MCP visibility must be verified separately because host schema/loading behavior can differ by Claude Code version. | Sanitize env/host URLs before publishing outside trusted local/team installs. Resolve marketplace/MCP manifest validation warnings before public release. Global Claude Code settings remain out of scope. |
| Cross-platform desktop | Windows, macOS, and Linux directory picker fallbacks are implemented with manual entry fallback. | Add real-machine smoke evidence for macOS and Linux/WSL before public release. |

## Publish Caution

Before publishing or sharing the plugin outside the local machine, sanitize
`.mcp.json` and environment variables. Never commit local credentials into MCP
env blocks; provide them through the host environment instead.

Sources checked 2026-05-13: Claude Code settings/MCP/plugin scopes
(`https://code.claude.com/docs/en/settings`,
`https://code.claude.com/docs/en/mcp`) and OpenAI Docs MCP/Codex docs
(`https://platform.openai.com/docs/docs-mcp`,
`https://platform.openai.com/docs/codex`).
