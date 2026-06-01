# MCP Tool Guide

Prefer MCP tools over raw SQLite or hand-rolled HTTP calls when the tool exists. Raw HTTP is acceptable as a fallback when a tool is absent from the current client.

## Runtime And Health

- `health`: governance service health.
- `version_check`: HEAD, chain version, dirty files, and runtime match.
- `runtime_status`: core governance/version state plus optional advanced
  chain/ops readiness when ServiceManager is present.
  Pass `project_id`, for example `runtime_status(project_id="aming-claw")`.
- `preflight_check`: system, version, graph, coverage, queue, and plugin update
  state baseline.

Use these at session start, after commits, and before closing a backlog row.
ServiceManager or executor offline means advanced chain/executor work is
degraded; it does not by itself mean governance, dashboard, graph, backlog, or
Review Queue is down.
If a runtime tool returns `governance_online=false`, the MCP server and skill
loaded successfully but the governance HTTP service is offline or timed out.
Tell the user to start it with `aming-claw start` or the launcher, then retry;
do not describe that state as a plugin or MCP install failure.

## AI Config And Local Runtime

Use the project AI config endpoint before queueing AI Enrich:

- HTTP fallback: `GET /api/projects/{project_id}/ai-config`.
- Check `tool_health.openai` and `tool_health.anthropic` for local CLI probe
  status, path, runtime, command, and version.
- Check `project_config.ai.routing` for project-specific role/provider/model
  routes. The `semantic` route must have both provider and model before live AI
  semantic enrichment should run.
- Check `semantic.use_ai_default` for the semantic worker default and
  `model_catalog` for valid provider/model choices.

Provider mapping:

- `openai` -> Codex CLI, command `codex`, optional override `CODEX_BIN`.
- `anthropic` -> Claude Code CLI, command `claude`, optional override
  `CLAUDE_BIN`.

Status wording must separate command detection from real AI availability:

- `detected`: local command exists and version probe worked.
- `auth unknown`: version probe does not prove login or model-call success.
- `missing`: command or configured path is absent.
- `routing missing`: project semantic provider/model is unset; block AI Enrich
  and ask the user to configure AI config.

Do not run a real Codex or Claude model call as a readiness check unless the
user explicitly asks; it may spend quota or trigger interactive login.

## Semantic Enrichment

Use the dashboard AI Enrich action or `POST /semantic/jobs` for MVP semantic
work. The flow is queue -> worker -> Review Queue -> accept/reject ->
projection. Treat `ai_complete` as "AI proposal generated", not as trusted or
approved memory.

`/semantic-enrich` is a lower-level admin/debug/rebuild endpoint. Do not use it
as the default operator path.

## Backlog

- `backlog_list`: find rows by status/priority/search. Defaults to compact
  `OPEN` rows with `limit=50` to avoid oversized MCP context. Use `offset` for
  pagination, `include_closed=true` for all statuses, `view=full` only for a
  deliberately small page, and `backlog_get` for full detail of one row.
- `backlog_get`: inspect the selected row.
- `backlog_upsert`: create/update a row before code or doc mutations.
- `backlog_close`: close with commit evidence. Protected closes must include
  either a public-safe `route_token` payload (`route_context_hash`,
  `prompt_contract_id`, `caller_role`, `allowed_action`, `scope.project_id`,
  `expires_at`, and `evidence_refs`) or an explicit `route_waiver` /
  `route_token_waiver` with manual-fix/same-worktree reason and timeline
  evidence. For `observer_led_parallel_lanes` / `mf_parallel.v1` work, this
  token/waiver does not replace close-gate route consumption evidence.
- `task_timeline_append`: append observer/agent execution evidence during MF
  work. For close-gate evidence use `event_kind=implementation`,
  `event_kind=verification`, and `event_kind=close_ready` with
  `status=accepted`/`passed`/`succeeded`. Route-parallel close also requires
  passing timeline events for `route_context`, `route_action_precheck`,
  `mf_subagent_dispatch`, and `mf_subagent_startup`, each carrying matching
  `route_context_hash`, `prompt_contract_id`, and `prompt_contract_hash`.
- `task_timeline_list`: inspect append-only timeline events by `backlog_id`,
  `task_id`, `trace_id`, `phase`, or `event_kind`.
- `mf_timeline_precheck`: run the same non-mutating MF close-gate check that
  `backlog_close` will enforce.
- `backlog_export`: export backlog rows as portable JSON for transfer or backup.
- `backlog_import`: import portable backlog JSON with `skip`, `overwrite`, or
  `fail` conflict handling and optional dry-run.

For MF work, use the backlog row as the single source of scope, target files, acceptance, and commit evidence.
Use the timeline tools as the execution ledger: write implementation evidence
when code/docs/config change, verification evidence after tests or review, and
close-ready evidence after commit/redeploy/reconcile checks. Run
`mf_timeline_precheck` before `backlog_close` so a session can repair missing
evidence before the authoritative gate rejects the close.
During MVP, some observer-hotfix/manual-fix flows are stored as
`mf_type=chain_rescue`. Treat that as the internal audited MF bucket, not as a
requirement that ordinary implementation must run through chain automation.

**HTTP fallback when MCP tools are unavailable in the current client/session.**
Current Aming Claw plugin sessions should expose the `backlog_*` MCP tools; if
the client did not hot-load MCP yet, reload/open a new session or use
governance HTTP routes directly:

- `GET  /api/backlog/{project_id}` — list (returns `{bugs: [...], count}`).
  Legacy no-query calls return all full rows. Optimized query params:
  `view=compact|full`, `limit` (max 200), `offset`, `q`, `status`,
  `priority`, `include_closed=false`. Optimized responses include
  `total_count`, `filtered_count`, `has_more`, `next_offset`, and `summary`.
- `GET  /api/backlog/{project_id}/{bug_id}` — fetch one row.
- `POST /api/backlog/{project_id}/{bug_id}` — upsert. Body fields: `title`,
  `status` (`OPEN`/`FIXED`/`CLOSED`/...), `priority` (`P0..P3`),
  `mf_type`, `target_files` (semicolon-joined), `test_files`,
  `acceptance_criteria` (semicolon-joined sentences), `commit`,
  `fixed_at`, `details_md`. Pass `"force_admit": true` to skip the AI
  triage duplicate-check gate when filing a known/intentional row.
- `POST /api/backlog/{project_id}/{bug_id}/predeclare-mf` — pre-declare MF
  intent before the commit.
- `POST /api/backlog/{project_id}/{bug_id}/start-mf` — mark MF in progress.
- `POST /api/backlog/{project_id}/{bug_id}/close` — close with commit
  evidence after the MF lands.
- `POST /api/task/{project_id}/timeline` — append task/MF execution evidence.
- `GET  /api/task/{project_id}/timeline` — list timeline events. Query filters:
  `task_id`, `backlog_id`, `trace_id`, `phase`, `event_kind`, `scenario_id`,
  `correlation_id`, `severity`, `decision`, `parent_event_id`, `limit`.
- `GET  /api/backlog/{project_id}/{bug_id}/timeline-gate` — read-only MF
  close-gate precheck. Optional query: `include_events=true`, `limit`.
- `GET  /api/backlog/{project_id}/portable/export` — export portable JSON.
  Optional query: `status`, `priority`, `bug_id` (comma-separated).
- `POST /api/backlog/{project_id}/portable/import` — import portable JSON.
  Body: `payload`, `on_conflict` (`skip`/`overwrite`/`fail`), `dry_run`,
  `actor`.

**Do not "file" backlog by writing a markdown doc into `docs/dev/`** — the
canonical store is `backlog_bugs` table behind these routes. The
`docs/dev/manual-fix-current-*.md` files are session scratch notes, not the
backlog of record (and `docs/dev/` is gitignored, so they're not committed).

## Graph Governance

- `graph_status`: active snapshot, graph stale state, pending scope reconcile.
- `graph_operations_queue`: dashboard-ready operation rows and semantic queue status.
- `graph_query`: audited graph discovery. Start with `query_schema`, then use graph-native tools before filesystem scans:
  - `find_node_by_path`: resolve a file path to owning nodes. For a directory
    subtree, pass `args: {"path": "frontend/dashboard/src", "directory": true,
    "limit": 25}` to return nodes with files under that path without broad grep.
  - `search_structure`: search node id/title/kind/files/metadata/functions.
  - `function_index`: search `metadata.functions` and `metadata.function_lines`.
  - `function_callers`, `function_callees`, `high_function_degree`: inspect persisted function-level call facts when the active graph was built with function call metadata.
  - `degree_summary`: exact fan-in/fan-out and edge-type breakdown for a node.
  - `high_degree_nodes`: rank high fan-in/fan-out candidates.
  - `list_features`: budget-safe L7/L4 lists; default is `compact=true` and
    `include_semantic=false`.
  - `get_neighbors`: structural neighbors; pass `include_edge_semantic=true` for semantic edge projection payloads.
  - `search_semantic`: node semantics, node metadata, and current edge semantic projection.
  - `search_docs`, `get_node`, and `get_file_excerpt`: docs, exact node fetches, and bounded code excerpts.

Semantic access is explicit:

- Use `search_semantic` for semantic keyword search across nodes and current
  edge projection payloads.
- Use `get_node` with `include_semantic=true` for one known node. Add
  `compact=true` when a short status/intent/domain summary is enough.
- Use `get_neighbors` with `include_edge_semantic=true` for edge semantics
  around a node.
- Use `list_features` with `include_semantic=true` only for bounded lists. In
  compact mode it returns compact semantic overlays; use `compact=false` only
  for small, deliberate result sets.

All graph-query subtool parameters must be nested under the `args` object. Do
not flatten `path`, `query`, `limit`, or `node_id` at the top level.

Good:

```json
{"project_id":"aming-claw","tool":"find_node_by_path","args":{"path":"agent/governance/server.py"}}
```

Bad:

```json
{"project_id":"aming-claw","tool":"find_node_by_path","path":"agent/governance/server.py"}
```
- Direct Update graph is the preferred MVP path when HEAD and the active graph diverge:
  call governance `POST /api/graph-governance/{project_id}/reconcile/pending-scope`
  with `activate=true` and `semantic_use_ai=false`. Pass `target_commit_sha`
  when available; if omitted, governance infers the project git HEAD or returns
  an actionable `target_commit_sha_required` response with a recommended body.
  The backend creates and consumes transient pending-scope bookkeeping in one
  request, so operators should not see a stale queued `scope_reconcile` row.
- `graph_pending_scope_queue`: legacy/debug helper for explicitly queueing a
  pending scope row. Do not use it as the default dashboard/plugin Update graph
  flow.

## Governance Hint

Governance Hint is the safe MVP path for binding orphan doc/test/config files to
existing graph nodes. It writes a source-controlled `governance-hint` comment,
returns `written_uncommitted`, and requires a commit plus Update Graph/reconcile
before graph materialization.

Only use it for files already present in snapshot inventory with
`scan_status=orphan`. If the API reports `file inventory row not found`, update
the graph first so the file inventory can see the new file.

Example:

```json
{
  "project_id": "aming-claw",
  "tool": "query_schema"
}
```

```json
{
  "project_id": "aming-claw",
  "tool": "search_structure",
  "args": {"query": "language adapter", "limit": 10},
  "query_source": "observer",
  "query_purpose": "prompt_context_build"
}
```

## Workflow And Nodes

- `wf_summary`: node verification summary.
- `wf_impact`: impacted nodes for target files.
- `node_update`: update node verification status with evidence only after real verification.

The `wf_*` tools read the older acceptance/workflow graph (`graph.json`), not
the snapshot graph used by `graph_status`/`graph_query`. They require
`POST /api/wf/{project_id}/import-graph` first. If the response has
`needs_import_graph=true`, do not treat `total_nodes=0` as a healthy empty
project; either import the workflow graph or rely on snapshot graph governance
instead.

## Tasks And Observer

- `task_create`, `task_list`, `task_claim`, `task_complete`, `task_cancel`.
- `task_hold`, `task_release`, `observer_mode`.

Use observer controls for review/takeover flows. Preserve task metadata when manually completing or re-creating chain stages.

## Advanced ServiceManager And Executor

These tools are not part of the V1 dashboard/graph/backlog/Review Queue happy
path. Use them only for chain automation, host redeploy, or executor debugging.

- `manager_health`: ServiceManager sidecar status.
- `manager_start`: fixed bootstrap facade. Do not request takeover from MCP; run takeover from an external ops shell when needed.
- `governance_redeploy`: redeploy governance through ServiceManager.
- `executor_respawn`: ask ServiceManager to respawn the external executor.
- `executor_status` and `executor_scale`: only manage MCP-local workers when the MCP server was intentionally started with workers.

Normal editor/plugin MCP sessions should use `--workers 0`. V1 semantic jobs
are drained by the governance in-process semantic worker; executor workers are
for advanced chain automation.
