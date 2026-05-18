# Aming Claw Codex Setup

Use this project-level Codex setup when working in this repository or an
external project governed by Aming Claw.

## Runtime Model

- Codex does not auto-start Aming Claw services.
- Start governance explicitly with `aming-claw start` or `aming-governance-host`.
- `aming-claw start` starts governance only. It does not prove the current
  Codex session loaded the plugin/skills/MCP, that dashboard assets exist, or
  that ServiceManager/executor are online.
- `aming-claw start` exits quickly when governance is already healthy; otherwise
  it runs as a foreground service and should stay in a separate terminal.
- After plugin install/update, reload Codex or open a new session.
- Keep `.mcp.json` project-local and relocatable; do not put credentials or
  absolute user-machine paths in it.
- Use MCP tools for graph, backlog, runtime, and ServiceManager checks before
  editing governance or dashboard code.

## Local Startup

```bash
aming-claw launcher
aming-claw start
```

Then open:

```text
http://localhost:40000/dashboard
```

The root path `/` is not the dashboard and may return `404`. If `/api/health`
is OK but `/dashboard` returns `503`, dashboard static assets are missing. No
build is needed when `agent/governance/dashboard_dist/index.html` or
`frontend/dashboard/dist/index.html` exists; raw checkouts missing both can run
`cd frontend/dashboard && npm install && npm run build`.

## Codex Contract

1. Load the project `.mcp.json`.
2. Call `runtime_status(project_id="<id>")`, `graph_status`, and
   `graph_operations_queue` before implementation work.
3. File or update a backlog row before mutating code, docs, config, dashboard
   assets, or runtime state.
4. For manual fixes, follow `skills/aming-claw/references/mf-sop.md`.
5. For dashboard or graph behavior, evaluate E2E impact and run or file the
   relevant E2E evidence.

## MVP Boundaries

- Chain automation is limited in MVP. Use MF for ordinary implementation when
  needed, but keep backlog-first, graph-first, tests, Chain trailers, commit,
  scope reconcile, and backlog-close discipline.
- Target projects must be explicitly registered/bootstraped before graph-backed
  claims are available. Do not silently bootstrap an unregistered workspace.
- Bootstrap uses governance on port `40000`, not ServiceManager on `40101`, and
  requires a clean git worktree when the target is a git repo.
- Aming Claw internals may use `aming-claw://seed-graph-summary` as the MVP
  navigation fallback when no active self graph exists.
- AI Enrich is available through project AI routing and Review Queue. `ai_complete`
  means a proposal exists, not trusted memory.
- Governance Hint is available only for binding orphan doc/test/config files in
  snapshot inventory to existing nodes; commit the hint and Update Graph before
  expecting graph materialization.

## Local AI Runtime Readiness

- Check `GET /api/projects/{project_id}/ai-config` before live AI work. Read
  `tool_health`, `model_catalog`, `project_config.ai.routing`, and
  `semantic.use_ai_default`.
- `openai` routes use Codex CLI (`codex`, override with `CODEX_BIN`);
  `anthropic` routes use Claude Code CLI (`claude`, override with `CLAUDE_BIN`).
- Version detection means only `auth unknown`; do not treat `codex --version`
  or `claude --version` as proof that a real AI task can run.
- If semantic provider/model routing is missing, AI Enrich is blocked until the
  project is configured in AI config.
- If ServiceManager/executor are offline, chain/executor is degraded even when
  local AI CLIs are detected.

## Structured AI Output

- Any AI path that produces machine-consumed structured data must expose and
  require a local precheck script/API for the draft output before final output.
- The AI-facing prompt must tell the model to run that precheck on its own
  output, repair model-correctable schema/contract errors once, and include
  precheck evidence in the final structured result.
- The local governance gate remains authoritative. AI self-precheck reduces
  malformed outputs and retries, but it must never bypass server-side parsing,
  validation, review, or observer approval.

Global Codex settings are intentionally out of scope for v1. Keep this
project-level setup transparent and reversible.
