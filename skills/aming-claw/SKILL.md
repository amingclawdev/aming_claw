---
name: aming-claw
description: Use when working in the Aming Claw repo or any governance, dashboard, MCP, ServiceManager, backlog, graph, semantic reconcile, scope/full reconcile, chain, executor, or manual-fix/observer-hotfix task. Enforces graph-first discovery, backlog/MF tracking before mutations, MCP-first operations, Chain trailers on commits, and post-commit runtime/graph checks.
---

# Aming Claw

## Capabilities

Use Aming Claw as a local graph-first governance assistant. In a fresh session,
tell the user you can help with:

- Diagnose project governance state: runtime, ServiceManager, version, active snapshot, graph stale/current, pending scope reconcile, operations queue, semantic queue, and open backlog.
- Explore graph structure: layers, subsystems, features, hierarchy, node files, function indexes, neighbors, edge evidence, fan-in/fan-out, quality flags, orphan/low-relation signals, and doc/test coverage.
- Locate code precisely: resolve file paths to nodes, search module/title/file/function metadata, inspect `function_lines`, query `function_callers` / `function_callees`, and fetch bounded file excerpts only after graph lookup.
- Rank PR opportunities: use graph evidence to identify high fan-out nodes, missing tests/docs, suspicious dependencies, semantic drift, review debt, and candidate refactor/test/doc issues.
- Generate evidence-backed backlog rows: include node ids, primary files, related functions, graph metrics, neighbors, risk, acceptance criteria, target files, and test files.
- Guide dashboard collaboration: use browser-use to inspect Projects, Graph tree, Inspector, Relations, Functions, Operations Queue, Review Queue, and Backlog as the same shared control plane the user sees.
- Onboard new users with a host-rendered launcher MVP: dashboard link, project initialization path, browser collaboration entry, graph concepts, backlog workflow, and safe startup commands.
- Run targeted semantic enrichment and review when requested: explain missing/current/hash-unverified/pending-review states, queue/cancel/retry semantics, and the difference between AI-proposed memory and user-approved memory.
- Drive advanced chain/dev/test/qa workflows only when explicitly needed; MVP work can stay local with graph, backlog, tests, and dashboard checks.

## Operating Contract

Treat the active graph as the project map and the backlog as the work ledger. Before editing code, docs, config, dashboard assets, or runtime state, establish current graph/runtime status, identify the owning nodes/modules, and record the work item.
For new features or user-visible behavior changes, treat E2E impact as part of the work ledger: run/update the relevant suite and evidence, or file an explicit follow-up backlog row when the E2E is deferred.
For dashboard/graph E2E work, update repo-owned fixture artifacts first and materialize them into isolated temporary projects; do not hand-edit generated example projects as the source of truth.

## MVP Capability Matrix

Stable MVP capabilities:

- Register/bootstrap a target project after explicit user approval.
- Build or update a commit-bound project graph.
- Query graph structure, files, functions, docs/tests, neighbors, fan-in/fan-out, and bounded file excerpts.
- Show and file backlog rows with graph evidence.
- Run targeted AI Enrich for selected nodes or edges, then review and accept/reject proposed semantic memory.
- Use Governance Hint to bind orphan doc/test/config files to existing nodes.
- Use the dashboard with browser-use as the shared visual control plane.

Limited or deferred capabilities:

- Chain dev/test/qa/merge automation is experimental in MVP. Prefer Manual Fix for ordinary MVP implementation unless the user explicitly asks for chain execution.
- Function-level call graph queries are available for supported adapters through
  `function_callees`, `function_callers`, and `high_function_degree`; dashboard
  visualization is still evolving.
- Arbitrary graph editing, node moves, ownership rewrites, dependency rewrites, and automatic topology mutation are out of scope.

## MVP Graph Model

Aming Claw's MVP is primarily a governance tool for other local projects. A
target project must be registered/bootstraped before graph-native claims can
use node, function, edge, or coverage evidence.

The Aming Claw repo itself does not need an active local graph snapshot for the
plugin to be usable. When working on Aming Claw internals and no active
`project_id="aming-claw"` snapshot exists, use `aming-claw://seed-graph-summary`
as the packaged navigation map for core surfaces, then use bounded workspace
search/file reads for exact code. Do not claim node-level or function-level
graph evidence for Aming Claw itself unless an active `aming-claw` graph exists.

A missing active graph for `aming-claw` is not an install failure. A missing
active graph for the user's target project means the project should be
registered/bootstraped before graph-backed governance is available.

For Aming Claw internals, prefer live graph queries and the active semantic
projection when available. If a new session needs a compact orientation, use
these fallback references after checking graph/runtime status:

- `skills/aming-claw/references/architecture-map.md`
- `skills/aming-claw/references/semantic-control-loop.md`
- `skills/aming-claw/references/graph-repair-and-reconcile.md`

## Commit-Bound Graph

Graph snapshots are commit-bound. They represent the selected ref/HEAD commit,
not dirty worktree state. Full reconcile, scope reconcile, bootstrap graph
builds, and Update Graph should use a clean worktree.

If the worktree is dirty, do not call that a graph bug. Tell the user:

```text
Graph snapshots are commit-bound. Commit/stash unrelated local changes before reconcile, or use an isolated clean worktree for candidate-only inspection.
```

## Branch Graph Policy

Parallel branch/worktree graph state is one-hop candidate evidence from a
target commit graph. The target ref's active graph snapshot and semantic
projection are the only graph truth.

Do not chain graph reconcile from a branch candidate. Do not activate or
persist branch-local graph candidates as target graph truth. During branch
development, compare the current branch HEAD against the selected target commit
graph and replace or prune stale branch candidates instead of creating a
branch-local graph history. If the target ref moves, rebase or recompute the
branch delta against the new target graph before merge gating.

After an ordered merge lands on the target ref, run target-ref scope reconcile
and activate the target snapshot/projection. Branch refs, worktree ids,
snapshot ids, and projection ids may be recorded only as bounded candidate
evidence, merge-gate inputs, rollback/replay provenance, or audit pointers.

For existing projects with long-lived release, maintenance, or large feature
branches, do not register each ordinary branch as a separate project by default.
Keep one project identity and use a managed ref context for each governed ref.
Managed refs may carry ref-local snapshot/projection pointers, but merges still
update code first and then reconcile the target ref. After merge, archive the
source ref context; do not delete the project as a substitute for ref cleanup.
When importing an already-branched project, run managed-ref bootstrap dry-run
first, review the target/agent/managed/ignored/unmanaged/blocked classification,
then apply only the accepted managed refs. Refreshing an existing managed ref
marks it stale and requires recomputation against target graph truth.

## Current Workspace Not Registered

If governance is running but `GET /api/projects` does not include the active
workspace, do not silently bootstrap it. Project bootstrap mutates governance
state: it writes the project registry/DB, scans the workspace, and creates graph
snapshot state.

Ask the user before registering unless they explicitly requested
initialization, registration, or bootstrap. Use governance, not ServiceManager:

- Correct API: `POST http://127.0.0.1:40000/api/project/bootstrap`.
- Do not use `http://127.0.0.1:40101/` for project bootstrap; port `40101` is
  the ServiceManager sidecar.

For an explicit "initialize this project" request, infer `project_id` from the
folder name, `workspace_path` from the current workspace root, language from
project files when obvious, and common excludes such as `node_modules`, `dist`,
`build`, `.expo`, `.next`, and `coverage`. Before bootstrapping, inspect the
target root or ask the user to confirm the dashboard exclude-path field:
project-specific generated, vendored, nested, or tool-owned directories such as
`node`, `vendor`, generated clients, fixture clones, scratch worktrees, or
downloaded assets should be added before graph build. Source-controlled
projects can keep the same rule in `graph.exclude_paths`, `graph.ignore_globs`,
or `graph.nested_projects`. When bootstrapping from an AI session instead of
the dashboard form, surface this as an explicit visible reminder before calling
the bootstrap API/CLI, and include the reviewed exclude list in the bootstrap
request. After bootstrap, open:
`http://127.0.0.1:40000/dashboard?project_id=<project_id>&view=projects`.

## Semantic Enrichment MVP

Recommended path: dashboard AI Enrich or `POST /semantic/jobs` -> semantic
worker -> Review Queue -> accept/reject -> semantic projection. The
`/semantic-enrich` endpoint is a lower-level admin/debug/rebuild path, not the
default MVP workflow.

`ai_complete` means AI generated a proposal or worker output; it does not mean
trusted memory. Accepted semantics become trusted only after the operator
approves the Review Queue item and projection materializes it.

Before queueing live AI work, check `/api/projects/{project_id}/ai-config`.
Project-level `ai.routing.semantic` wins over the global semantic config. If
the project semantic provider/model is missing, AI Enrich is blocked until AI
config is saved for that project.

For any AI workflow that emits structured data consumed by governance code,
require an AI self-precheck step before final output. The prompt or payload
must expose the applicable local precheck script/API and instruct the model to
run it against the draft JSON, repair model-correctable contract errors once,
and include precheck evidence in the final result. The system-side gate remains
authoritative: AI self-precheck cannot bypass parsing, validation, Review
Queue, or observer approval.

For semantic dogfood findings that suggest graph structure is wrong, classify
the fix path before editing graph-building code:

- `config_patch`: existing semantic enrichment config plus registered
  predicates/actions can express the correction.
- `registered_action_needed`: the config model is right, but a reusable
  predicate/action/enricher must be added and registered before config can use
  it.
- `adapter_evidence_gap`: the bottom algorithm is sound, but a language or
  file-role adapter must extract more evidence.
- `core_algorithm_gap`: the generic graph algorithm cannot express the
  relation correctly even with config, registration, or adapter evidence.

Direct core algorithm edits require a written note explaining why the
config/registration/adapter path cannot express the issue. If graph rule inputs
or their fingerprint change, do not use scope reconcile as the recovery path:
run full reconcile, then let semantic state carry forward only entries whose
feature, file, source-function, and test-function hashes still match.

## Governance Hint MVP

Governance Hint is the MVP-safe graph-structure correction path for orphan
doc/test/config files that already appear in the snapshot file inventory with
`scan_status=orphan`.

It writes source-controlled evidence into the file, returns
`written_uncommitted`, and does not mutate the graph DB directly. The user or
agent must commit the hint file and then run Update Graph/reconcile before the
binding appears in the graph. Repeating the same path + target node + role is
idempotent.

Use Governance Hint when an orphan doc/test/config file clearly belongs to an
existing node. Do not create nodes, edit the DB, move ownership, change primary
files, rewrite hierarchy/dependency edges, or invent function-call relations
through this flow. If the API says `file inventory row not found`, run Update
Graph first so the file enters snapshot inventory, then retry.

When summarizing work, explicitly report whether a hint is still uncommitted or
not yet materialized into the graph.

## Manual Fix SOP

Use the manual-fix SOP for observer-hotfix, chain rescue, and other bypass
work where normal chain execution is not the right path. The canonical SOP is
`docs/governance/manual-fix-sop.md`; the compact session checklist is
`aming-claw://mf-sop`.

During MVP, Chain is not the default path for routine implementation. Use MF
for ordinary MVP fixes/features when needed, but do not treat MF as a bypass of
governance: backlog, graph discovery, tests, explicit commit files, Chain
trailers, post-commit scope reconcile, and backlog close still apply. This is a
temporary MVP mode; when Chain is stable, return to Chain-first development.
Some API responses may normalize observer-hotfix/manual-fix work to the
internal `chain_rescue` MF type. During MVP, treat that value as the audited MF
bucket, not as a requirement that ordinary implementation must use chain.

For parallel branch work, Codex subagents may be used as the MVP worker backend
only through the `mf_sub` contract. An `mf_sub` worker may patch code, run
tests, inspect diffs, checkpoint, and report blockers inside its assigned
worktree. It must not merge, push, activate graph refs, release gates, create
tasks, delete worktrees, or modify merge queues; those remain observer,
governance, or merge-queue operations. Natural-language cwd instructions are
not enough: the worker result must pass the finish gate, which validates the
current fence and, when the assigned worktree exists, recomputes the
`base_commit..HEAD` changed-file set from that worktree before checkpoint or
merge-queue entry.
Use role/capability boundaries for parallel workers: `mf_sub` sessions may use
the finish gate and task-scoped audited graph queries with
`query_source=mf_subagent`, `parent_task_id`/`task_id`, and `fence_token`.
Observer/coordinator remains required for merge queue writes, merge execution,
graph reconcile/activation, backlog close, ServiceManager/governance restarts,
worktree cleanup, and other privileged state changes. Do not tell a subagent to
identify as observer to get graph access.

Before editing:

1. Confirm the MF route is justified.
2. Ensure the backlog row exists with target files, acceptance criteria, and
   details.
3. Predeclare/start the MF row when the MCP/HTTP surface is available.
4. Capture baselines: `git status`, `version_check`, `preflight_check`,
   `graph_status`, and `graph_operations_queue`. Use `wf_impact` only when the
   project has an imported workflow acceptance graph; if `wf_*` returns
   `needs_import_graph=true`, record that precondition instead of treating
   `total_nodes=0` as healthy.
   Treat `preflight_check.checks.plugin_update_state` blockers as MF blockers;
   `update_available` or missing plugin state is a warning to record.
5. Run graph-first discovery and record the owning nodes or why the file is not
   graph-mapped.
6. Record the E2E decision: run it, defer it with a follow-up backlog row, or
   mark it `e2e_not_applicable` with a reason.

Commit explicit files only, and use Chain trailers for true MF commits:

```text
Chain-Source-Stage: observer-hotfix
Chain-Project: aming-claw
Chain-Bug-Id: <backlog-id>
```

After commit, re-check version/runtime and graph state, reconcile if the graph
is stale, then close the backlog row with commit and verification evidence.

## Start Sequence

1. Confirm the workspace root and project id, normally `aming-claw`.
2. Check runtime health with MCP/HTTP: `health`, `version_check`, and `runtime_status(project_id="<project_id>")` when available. If the live runtime is older than the documented skill contract or lacks `graph_query(tool=query_schema)`, stop and ask for reload/redeploy/update before relying on new graph tools.
3. Check graph state: `graph_status` and `graph_operations_queue`.
4. If governance is offline or this is a fresh install, read `aming-claw://seed-graph-summary` for packaged MVP structure before asking the user to start services.
5. For AI or semantic work, check local AI runtime readiness through the project AI config before queueing jobs.
6. Call `graph_query` with `tool=query_schema` to discover the live query contract.
7. Run graph-first discovery before implementation. Prefer `find_node_by_path`, `search_structure`, `list_features`, `function_index`, `function_callers`, `function_callees`, `high_function_degree`, `degree_summary`, `high_degree_nodes`, `get_neighbors`, and `search_semantic` before broad filesystem scans. Start with compact graph queries; use `search_semantic`, `get_node(include_semantic=true)`, or `get_neighbors(include_edge_semantic=true)` for semantic payloads. See [graph-first.md](references/graph-first.md).
8. Read or create the backlog row before any mutation. For MF/observer-hotfix work, predeclare/start the MF row first.
9. Inspect files only after graph discovery identifies likely owners and reusable modules.

## Local AI Runtime Readiness

Before claiming AI Enrich, semantic review, or chain/executor readiness, inspect
the selected project's AI config:

- HTTP fallback: `GET /api/projects/{project_id}/ai-config`.
- Read `tool_health.openai`, `tool_health.anthropic`,
  `project_config.ai.routing`, `semantic.use_ai_default`, and `model_catalog`.
- `openai` uses the local Codex CLI command `codex`; `CODEX_BIN` may override
  the executable path.
- `anthropic` uses the local Claude Code CLI command `claude`; `CLAUDE_BIN`
  may override the executable path.

Report these states separately:

- `CLI detected`: the command exists and a version probe succeeded.
- `auth unknown`: version probing does not prove login or real model execution.
- `missing`: the CLI is absent or the configured path is wrong.
- `routing missing`: the project has no semantic provider/model; AI Enrich must
  be blocked and the user should configure AI config first.
- `ServiceManager/executor unavailable`: automatic chain/executor work is
  degraded even when the local CLIs are present.

Use a compact status shape when helping the user:

```text
Codex CLI: detected at <path>, version <version>, auth unknown.
Claude CLI: detected at <path>, version <version>, auth unknown.
Semantic route: <provider/model or unset>.
AI Enrich: ready / blocked because <reason>.
Chain executor: ready / degraded because <reason>.
```

Do not treat `codex --version` or `claude --version` as proof that a real AI
task can run. Real model calls or login checks must be explicit user-approved
actions so the session does not spend quota or trigger an interactive login by
surprise.

Dashboard behavior is the contract: if the semantic provider/model is missing,
surface "AI enrich blocked: configure this project's semantic provider/model in
AI config first." For MVP project registration flows, AI config writes should
go through the Aming Claw project registry instead of defaulting to a target
project root file.

## Fresh Session Launcher

On the first Aming Claw skill load in a fresh session, show a short
host-rendered launcher block before deep work. This is the MVP for onboarding
buttons until the dashboard/plugin frontend owns native controls.

First read `aming-claw://current-context` when available. If that resource is
missing or governance is offline, continue safely with the offline launcher
state instead of auto-starting services.

The launcher should be status-aware:

- If governance is online, include the dashboard link, project id, runtime
  version, graph stale state, operations queue count, and open backlog count
  when known.
- If the active project differs from the plugin default project, call that out
  before recommending graph or backlog actions.
- If governance is offline or current context is missing, show the explicit
  startup flow:

  ```text
  aming-claw launcher
  aming-claw start
  ```

- If no active graph exists for the selected project, make "Initialize Project"
  the primary next action.
- If graph is stale, make "Review Impact / Reconcile Graph" the primary next
  action.
- If graph is current, keep the primary actions to three: "Check Current
  Project Status", "Find PR Opportunities", and "Explain Graph Concepts".

Host-owned MVP behavior:

- Render the launcher as a compact Markdown action panel with button-like
  labels and links/copyable commands. If the host app supports interactive
  choice buttons, use them; otherwise ask the user to click the link or reply
  with the action label.
- Prefer the `## Primary Next Actions` emitted by
  `aming-claw://current-context` or `aming-claw://project/<project_id>/context`;
  do not expand it into a long menu unless the user asks.
- Keep the panel short enough to fit above the fold. Do not replace normal
  task work with a long tutorial.
- Show it once per fresh session unless the user asks for help, onboarding, or
  the launcher again.
- For concept actions, explain only the selected concept first: graph, node,
  edge, snapshot, semantic enrichment, backlog, or browser collaboration.

Suggested action labels:

- Check Current Project Status
- Find PR Opportunities
- Explain Graph Concepts
- Initialize Project
- Update Graph

Do not silently start services. Browser or dashboard buttons may open URLs or
copy commands, but must not execute local shell commands.

## Visual AI Collaboration

Aming Claw dashboard is the shared cockpit for the user, AI session, and
governance system. When browser-use is available, open the dashboard to align
with what the user sees.

- Browser-use may navigate the graph tree, node inspector, Relations, Functions, Operations Queue, Review Queue, Projects, and Backlog.
- Cross-check visible dashboard state with MCP/Graph API results before drawing conclusions or recommending actions.
- Use dashboard state to explain graph health, stale/current state, semantic status, pending jobs, review proposals, and backlog/workflow state.
- Dashboard `vscode://file/...` links are for the human editor. Browser-use does not control VS Code directly.
- For AI-side code inspection, use graph `function_lines`, `get_file_excerpt`, and workspace tools.
- For governance actions, use MCP/Graph API. For code edits, use Codex workspace tools after the user has approved the work.

Recommended visual workflow:

1. Open dashboard and select the project.
2. Verify runtime, graph, operations queue, and semantic/review state.
3. Inspect candidate nodes or edges in the graph.
4. Use graph-native queries for precise node, edge, function, and file context.
5. Use bounded file excerpts or workspace reads only for the narrowed target.
6. File/update backlog before mutation, then implement and verify.

## Local Plugin Launcher

When the user asks for a local plugin entrypoint, onboarding help, or the
governance runtime is offline, offer the explicit launcher flow instead of
auto-starting services:

```text
aming-claw launcher
aming-claw start
```

The generated launcher artifact may be a host-rendered Markdown panel for MVP
or an HTML/dashboard guide in later iterations. It may include:

- Dashboard link.
- Project selector or project id.
- Runtime and graph status summary.
- Copyable startup commands.
- Onboarding actions for initialization, graph concepts, backlog, and browser
  collaboration.

It must not execute local commands from a browser button; service startup
remains an explicit MCP/CLI action.

## Mutation Rules

- Prefer MCP tools over raw DB access or ad hoc HTTP when a tool exists. See [mcp-tools.md](references/mcp-tools.md).
- Never write directly to `governance.db` for normal operations.
- Use existing graph-owned modules/adapters before creating a new abstraction.
- Keep manual fixes small and tied to one backlog row.
- Commit with Chain trailers:

```text
Chain-Source-Stage: observer-hotfix
Chain-Project: aming-claw
Chain-Bug-Id: <backlog-id>
```

## Verification

Before closing a row:

1. Run focused tests or validation for the touched surface.
2. Run `git diff --check`.
3. Commit explicit files only.
4. Restart/redeploy governance or ServiceManager when runtime code changed.
5. Re-run `version_check` and confirm runtime matches HEAD.
6. Re-run `preflight_check` when MCP is available and confirm
   `plugin_update_state` has no blockers. As supplemental local evidence from
   the repo checkout, `python -m agent.cli mf precommit-check --json-output`
   can be used. Do not assume a stale installed `aming-claw` shell command has
   the same subcommands until plugin/CLI update aftercare has run.
7. Check graph status and operations queue; if graph is stale, run direct Update graph/scope reconcile before claiming dashboard state is current. Explicit pending-scope queueing is legacy/debug only.
8. Confirm E2E impact is current, deferred with a backlog row, or explicitly not applicable.
9. Close the backlog row with commit evidence.

## References

- [graph-first.md](references/graph-first.md): graph discovery playbook and reuse rule.
- [mf-sop.md](references/mf-sop.md): short MF checklist; canonical SOP remains `docs/governance/manual-fix-sop.md`.
- [mcp-tools.md](references/mcp-tools.md): MCP tool family guide and common payloads.
- [plugin-packaging.md](references/plugin-packaging.md): repo-local plugin layout and publish cautions.
