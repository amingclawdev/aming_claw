---
name: aming-claw-launcher
description: Use when a user wants to preview, start, onboard onto Aming Claw, open the dashboard, check runtime status, learn the basic CLI, or perform an end-to-end install. Triggers on "preview aming-claw", "start aming-claw", "launcher", "open dashboard", "is governance running", "how do I run this", "install and start", "install and open dashboard", "one-shot install", "full install", "install end-to-end", or any onboarding question. Defers to the main aming-claw skill for graph, backlog, manual-fix, semantic, or chain work.
---

# Aming Claw Launcher

Help the user start and verify Aming Claw locally. Default mode is show-the-command-and-wait — never spawn governance silently. Switch to one-shot mode only when the user explicitly invokes it (see [One-Shot Install Mode](#one-shot-install-mode) below).

## Preview Flow

1. Write the local launcher artifact:

   ```text
   aming-claw launcher
   ```

   Writes `.aming-claw/aming-claw-launcher.html` with the dashboard link and start commands. Add `--open-browser` to open it in the default browser, or `--governance-url <url>` to target a non-default host. The launcher never starts services on its own.

2. Start governance in the foreground from a separate terminal/window
   (host-owned, no plugin-spawned workers):

   ```text
   aming-claw start
   ```

   This checks `GOVERNANCE_PORT` from `--port` (default `40000`) first. If
   Aming Claw governance is already healthy, it prints the dashboard URL and
   exits. If another process owns the port, it reports a conflict. Otherwise it
   runs `start_governance.main` as a long-running foreground service; do not run
   that path as a normal one-shot Codex tool call and wait for it to exit.
   ServiceManager is started independently;
   do not let the plugin session spawn executor workers.

3. Confirm health (CLI):

   ```text
   aming-claw status
   aming-claw plugin doctor --python <path-to-python-3.9-or-newer>
   ```

   Or, when MCP is available, prefer structured probes for a richer snapshot:

   - `runtime_status(project_id="<project_id>")` — governance + ServiceManager + version_check in one call.
   - `version_check` — HEAD vs CHAIN_VERSION + dirty files.
   - `graph_status` — active graph snapshot + stale check + semantic drift summary.
   - `health` — bare governance ping.

4. Check local AI runtime readiness for the selected project before promising
   AI Enrich or chain/executor work:

   - HTTP fallback: `GET /api/projects/{project_id}/ai-config`.
   - Inspect `tool_health.openai`, `tool_health.anthropic`,
     `project_config.ai.routing`, `semantic.use_ai_default`, and
     `model_catalog`.
   - `openai` maps to the local Codex CLI command `codex`; `CODEX_BIN` may
     override the path.
   - `anthropic` maps to the local Claude Code CLI command `claude`;
     `CLAUDE_BIN` may override the path.
   - A detected CLI only means the command and version probe worked. Treat
     authentication as unknown unless the user explicitly asks for a real check.
   - If the semantic provider/model is unset, report that AI Enrich is blocked
     until AI config is saved for the project.
   - If ServiceManager or executor is unavailable, report chain/executor as
     degraded even when local AI CLIs are detected.

   Suggested status copy:

   ```text
   Codex CLI: detected at <path>, version <version>, auth unknown.
   Claude CLI: detected at <path>, version <version>, auth unknown.
   Semantic route: <provider/model or unset>.
   AI Enrich: ready / blocked because <reason>.
   Chain executor: ready / degraded because <reason>.
   ```

5. Open the dashboard:

   ```text
   aming-claw open
   ```

   Default URL: `http://localhost:40000/dashboard`. The root path `/` is not
   the dashboard and may return `404` without meaning governance failed.
   Governance serves the dashboard from packaged static assets. No build is
   needed when `agent/governance/dashboard_dist/index.html` or
   `frontend/dashboard/dist/index.html` already exists. In a raw checkout with
   missing assets, run:

   ```text
   cd frontend/dashboard
   npm install
   npm run build
   ```

   If `/api/health` is OK but `/dashboard` returns `503`, report dashboard
   static assets as missing instead of reporting governance as down.

6. Plugin aftercare:

   `aming-claw start` only starts the governance service. It does not prove that
   the current Codex thread loaded the plugin. After installing or updating the
   plugin, tell the user to reload Codex or open a new Codex session, then
   verify that the Aming Claw skill and `mcp__aming_claw` tools are visible.
   A reload only addresses current-session hot loading. If `codex exec` reports
   `failed to load plugin`, `plugin is not installed`, or invalid marketplace
   paths, run `aming-claw plugin install` and `aming-claw plugin doctor` first;
   do not present reload as the primary fix.
   Treat ServiceManager/executor offline as degraded runtime, not as dashboard
   or governance failure.

## Current Workspace Registration

Starting governance or opening the dashboard does not register the current
workspace. If `GET /api/projects` is empty or does not include the active
workspace, ask before bootstrap unless the user explicitly requested
initialize/register/bootstrap.

Use governance on port `40000`, not the ServiceManager sidecar on `40101`:

```text
POST http://127.0.0.1:40000/api/project/bootstrap
```

For explicit bootstrap, infer the project id from the folder name and use common
excludes such as `node_modules`, `dist`, `build`, `.expo`, `.next`, and
`coverage`. Before calling bootstrap, inspect the target root or ask the user
to confirm the dashboard exclude-path field: project-specific generated,
vendored, nested, or tool-owned directories such as `node`, `vendor`,
generated clients, fixture clones, scratch worktrees, or downloaded assets
should be added before graph build. Source-controlled projects can keep the
same rule in `graph.exclude_paths`, `graph.ignore_globs`, or
`graph.nested_projects`. When bootstrapping from an AI session instead of the
dashboard form, surface this as an explicit visible reminder before calling the
bootstrap API/CLI, and include the reviewed exclude list in the bootstrap
request. Bootstrap builds a commit-bound graph; if the workspace is a dirty git
repo, ask the user to commit/stash first.

## MVP Graph Model

The Aming Claw repo itself can use `aming-claw://seed-graph-summary` as packaged
MVP navigation when no active `aming-claw` graph exists. That is not an install
failure. Target/user projects need a registered active graph before graph-backed
claims are available.

## CLI Surface (`agent/cli.py`)

| Command | Purpose |
| --- | --- |
| `aming-claw init` | Write `.aming-claw.yaml` in the current directory. |
| `aming-claw bootstrap --path <dir> --name <id>` | Register an external project under governance. |
| `aming-claw scan --path <dir> --project-id <id>` | Scan an external project into a `.aming-claw` candidate workspace. |
| `aming-claw start --port 40000 [--workspace <runtime-root>]` | Start governance in the foreground from a separate terminal/window. By default the runtime root is the plugin checkout/package root, not the current target project. |
| `aming-claw status` | GET `/api/health` against the running governance service. |
| `aming-claw plugin doctor [--plugin-root <dir>] [--python <python3.9+>]` | Run read-only aftercare checks for plugin assets, generated marketplace, versioned Codex plugin cache, MCP config, Codex config hints, Python runtime, dashboard assets, AI CLI probes, and governance health. |
| `aming-claw open --governance-url <url>` | Open the dashboard in the default browser. |
| `aming-claw launcher [--open-browser] [--output path]` | Write the launcher HTML artifact. |
| `aming-claw plugin install <git-url>` | Clone/update a user-local plugin checkout, validate Codex/Claude manifests, optionally pip-install the runtime, install Codex cache/config, and print next steps. |
| `aming-claw plugin update --check\|--apply [<git-url>]` | Check a Git-backed plugin checkout for updates, apply fast-forward updates, refresh install surfaces, and write local restart/reload obligations. |
| `aming-claw backlog export\|import --project-id <id>` | Move backlog rows between local machines with portable JSON, dry-run, and explicit conflict handling. |
| `aming-claw mf precommit-check [--plugin-state <json>]` | Run local manual-fix pre-commit guards, including plugin update/restart state blockers. |
| `aming-claw run-executor` | Start an executor worker directly. Normally ServiceManager owns this — only use for explicit debugging. |

## Project-Local Plugin Contract

- MCP server config: `.mcp.json` at the Aming Claw plugin/repo root, stdio entrypoint `python -m agent.mcp.server --project aming-claw --workers 0 --governance-url http://localhost:40000`. Do not copy that file into an external target project; a target-local `.mcp.json` with `--project aming-claw` is install/startup pollution. Plugin sessions keep `--workers 0`; ServiceManager owns executor lifecycle.
- This skill is auto-discovered through the Claude Code plugin manifest at `.claude-plugin/plugin.json`. It is namespaced as `/aming-claw:aming-claw-launcher`. (Note: `CLAUDE.md` at repo root is **workspace** project rules — loaded by Claude Code when the repo is opened as a workspace, not part of plugin context; plugin-time guidance lives in this skill.)

## Offline / Fresh Install

If governance is offline or this is a fresh install:

1. If the user asks to install from a Git URL, prefer the host-native plugin
   flow first:

   ```text
   Install the Aming Claw plugin from https://github.com/amingclawdev/aming-claw
   ```

   If the host cannot install Git plugins directly yet, ask the user to clone
   once and run:

   ```text
   git clone https://github.com/amingclawdev/aming-claw.git
   cd aming-claw
   pip install -e .
   ```

   Then ask the user to start governance in a separate terminal/window:

   ```text
   cd aming-claw
   python -m agent.cli start
   ```

   Then run:

   ```text
   python -m agent.cli plugin doctor --plugin-root . --python python
   ```

   If the CLI is already available, use:

   ```text
   aming-claw plugin install https://github.com/amingclawdev/aming-claw
   aming-claw plugin doctor
   ```

2. Read `aming-claw://seed-graph-summary` (packaged MVP structure) when the MCP resource is available — do not invent module locations.
3. Show the explicit startup flow rather than auto-running it inline:

   ```text
   aming-claw launcher
   aming-claw start
   ```

   Make clear that `aming-claw start` only exits immediately when governance is
   already healthy or the port is conflicting. When it starts governance, it is
   long-running and should stay open in its own terminal; the assistant should
   return to status checks instead of waiting for the command to exit.

4. After plugin install, tell the user to reload Codex/open a new session. The
   current thread may not hot-load newly installed skills or MCP tools.
   For Claude Code, plugin install loads skills only; it does not install the
   Python runtime, start governance, prove MCP visibility in the current
   session, or validate CLI auth. If the sandbox blocks a remote installer
   script, prefer an explicit `git clone` plus local marketplace install.
5. After the user starts services, re-run `runtime_status` and confirm `version_check.ok == true` before recommending any mutation.

## When to Hand Off

Use the main `aming-claw` skill (`skills/aming-claw/SKILL.md`) for:

- Graph queries, node lookups, semantic search, function indexes.
- Backlog mutations, manual-fix / observer-hotfix work, Chain trailers.
- Chain debugging, version-gate, semantic reconcile, drift analysis.
- Dashboard governance flows beyond the basic preview.

## One-Shot Install Mode

Default behavior is "show the command and wait." Switch to one-shot mode only
when the user's prompt contains an explicit completion trigger:

- "install and start"
- "install and open dashboard"
- "install end-to-end"
- "one-shot install"
- "full install"
- "install + start"
- "bootstrap aming-claw end-to-end"

When triggered, run the bootstrap with tool calls instead of showing commands
to the user:

1. **Install plugin (marketplace + cache).** Try Claude Code slash commands
   first:
   - `/plugin marketplace add <git-url>`
   - `/plugin install <plugin-name>@<marketplace-id>` (typically
     `aming-claw@aming-claw-local`)

   `/plugin` is a CLI-harness slash command, not a tool. If the assistant gets
   "tool not available", "slash commands cannot be invoked from a tool", or
   similar errors, **do NOT ask the user to type the slash commands**. Fall
   back to manual replication — `/plugin` only does file-level work that the
   assistant can do too:

   a. `git clone --depth=1 <git-url> ~/.claude/plugins/marketplaces/<marketplace-id>/`
      (Windows path: `%USERPROFILE%\.claude\plugins\marketplaces\<marketplace-id>\`).
   b. Read `.claude-plugin/marketplace.json` from the clone to get the plugin
      name and version (currently `aming-claw` / `0.1.0`).
   c. Copy the marketplace clone into the plugin cache and drop `.git`:
      `mkdir -p ~/.claude/plugins/cache/<marketplace-id>/<plugin>/ &&`
      `cp -R ~/.claude/plugins/marketplaces/<marketplace-id> ~/.claude/plugins/cache/<marketplace-id>/<plugin>/<version> &&`
      `rm -rf ~/.claude/plugins/cache/<marketplace-id>/<plugin>/<version>/.git`.
      On Windows use `New-Item -ItemType Directory -Force` + `Copy-Item -Recurse` +
      `Remove-Item -Recurse -Force`.
   d. **Merge** (do not overwrite) an entry into
      `~/.claude/plugins/installed_plugins.json` for
      `"<plugin>@<marketplace-id>"`:
      `{"scope": "user", "installPath": "<abs path to cache .../<version>/>",`
      `"version": "<version>", "installedAt": "<ISO-8601 UTC now>",`
      `"lastUpdated": "<ISO-8601 UTC now>",`
      `"gitCommitSha": "<marketplace HEAD sha>"}`.
   e. **Merge** an entry into `~/.claude/plugins/known_marketplaces.json` for
      `<marketplace-id>`:
      `{"source": {"source": "github", "repo": "<owner>/<repo>"},`
      `"installLocation": "<abs path to marketplaces/<marketplace-id>>",`
      `"lastUpdated": "<ISO-8601 UTC now>"}`.

2. **Install Python runtime.** Run `pip install -e <abs path to marketplace
   clone>`. If `aming-claw` is already on `PATH`, prefer `aming-claw plugin
   install <git-url>` — that wraps pip install + marketplace refresh +
   versioned Codex cache in one call.

3. **Start governance in the background** — `aming-claw start` is long-running
   and will block the foreground tool call:
   - Windows: `Start-Process powershell -ArgumentList "-NoExit","-Command","aming-claw start"`.
   - macOS/Linux: `nohup aming-claw start > ~/.aming-claw/start.log 2>&1 &`.

4. **Poll** `aming-claw status` (or `GET http://localhost:40000/api/health`) for
   up to ~30 seconds until governance reports healthy on port `40000`.

5. **Open the dashboard.** Run `aming-claw open`.

6. **Announce the new-session requirement.** Do not phrase this as "reload and
   then it works" — the dashboard works *now* in the current session, but
   skills and MCP tools require a new session. Say something close to:

   > "Plugin is installed. The dashboard works now. To use Aming Claw skills
   > and MCP tools inside Claude Code conversations, open a new Claude Code
   > session."

Do not enter one-shot mode when:

- The user only asks to start, only asks to open, or only asks for status.
- The user asks for a dry-run, preview, or "show me the commands first".
- Governance is already healthy — just confirm health and run `aming-claw open`.

## What Not To Do

- Do not auto-start governance from a tool call **unless the user explicitly
  invoked One-Shot Install Mode** (see section above). Default behavior is to
  show `aming-claw start` as a separate-terminal command and wait for the user.
- Do not treat `aming-claw start` as plugin verification. Use
  `aming-claw plugin doctor` and a new Codex session visibility check.
- Do not bypass `aming-claw start` with `docker compose up` or raw `python -m agent.governance.server` unless the user is explicitly debugging.
- Do not modify `governance.db`, the version chain, or graph state from launcher flows — those go through the main `aming-claw` skill.
- Do not click HTML launcher buttons that would execute local shell commands. The launcher artifact is documentation, not a remote control.

## References

- Main governance skill: [SKILL.md](../aming-claw/SKILL.md).
- CLI source: [cli.py](../../agent/cli.py).
- Workspace project rules: [CLAUDE.md](../../CLAUDE.md) (workspace-only context; plugin-time guidance lives in this skill, not in CLAUDE.md).
- Plugin packaging notes: [plugin-packaging.md](../aming-claw/references/plugin-packaging.md).
