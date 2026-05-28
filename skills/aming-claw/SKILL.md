---
name: aming-claw
description: Use when working in the Aming Claw repo or any governance, dashboard, MCP, backlog, graph, semantic reconcile, scope/full reconcile, chain/executor advanced ops, or manual-fix/observer-hotfix task. Enforces graph-first discovery, backlog/MF tracking before mutations, MCP-first operations, Chain trailers on commits, and post-commit runtime/graph checks.
---

## REQUIRED FIRST READ

Before any response that uses this skill, in this exact order:

  ListMcpResourcesTool()
  ReadMcpResourceTool(uri="aming-claw://current-context")
  ReadMcpResourceTool(uri="aming-claw://skill")
  ReadMcpResourceTool(uri="aming-claw://graph-first")

current-context anchors project_id, governance URLs, and 3 guardrails.
skill is the operating contract (Start Sequence, Observer Operating Modes).
graph-first has copy-pasteable graph_query payload examples.

Common failures when these are skipped:
- Bootstrapping the wrong project (workspace_match auto-detected aming-claw)
- Calling task_create dev/pm (V1 default is observer-led mf_parallel.v1)
- Using Grep on the aming-claw codebase instead of graph_query
- Fabricating trace_id strings (audit ledger is server-resolvable, will fail)
- Running Execution Supervisor mode by default (Design Alignment is default)

# Aming Claw

## Capabilities

Use Aming Claw as a local graph-first governance assistant. In a fresh session,
tell the user you can help with:

- Diagnose project governance state: core runtime, version, active snapshot, graph stale/current, pending scope reconcile, operations queue, semantic queue, and open backlog. Treat ServiceManager/executor as advanced chain/ops readiness, not V1 core health.
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
The V1 implementation default is observer-led Manual Fix with local Codex
subagents as bounded `mf_sub` workers when parallel help is needed. Governance
chain/executor dev/test/qa/merge automation is advanced and experimental in V1,
not the V1 default implementation entrypoint; use it only when the user
explicitly asks to test chain automation.
For governed nontrivial implementation work, the observer/judge is a
coordinator, not an implementation author: it does not directly write
implementation code. When Judgment Brain is available, run the protocol
registry preflight `protocol_list` and topology precheck
`judgment_plan_precheck` before implementation planning. Nontrivial
implementation must be dispatched to bounded `mf_sub`/worker lanes with target
files, tests or a recorded no-test/E2E decision, branch/worktree and fence
evidence, and review evidence. The only direct observer mutation exception is
tiny deterministic scope with an explicit reason, allowed files, exact
dirty-scope match evidence, and a timeline event before mutation.
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

- Chain dev/test/qa/merge automation is experimental in MVP. Prefer
  observer-led Manual Fix for ordinary MVP implementation unless the user
  explicitly asks for chain execution.
- Function-level call graph queries are available for supported adapters through
  `function_callees`, `function_callers`, and `high_function_degree`; dashboard
  visualization is still evolving.
- Documentation is tracked as commit-bound doc asset state before it is trusted
  as node impact scope. Weak path/semantic matches remain proposal candidates
  until review or source-controlled hint evidence materializes the binding.
- Arbitrary graph editing, node moves, ownership rewrites, dependency rewrites, and automatic topology mutation are out of scope.

## MVP Graph Model

Aming Claw's MVP is primarily a governance tool for other local projects. A
target project must be registered/bootstraped before graph-native claims can
use node, function, edge, or coverage evidence.

The Aming Claw repo itself does not need an active local graph snapshot for the
plugin to be usable. When working on Aming Claw internals and no active
`project_id="aming-claw"` snapshot exists, use `aming-claw://seed-graph-summary`
as the compact packaged navigation map for core surfaces. If richer packaged
context is needed and the MCP resource is available, read
`aming-claw://self-graph-bundle/graph-structure` and
`aming-claw://self-graph-bundle/semantic-projection` as read-only orientation
for the sealed self graph. Then use bounded workspace search/file reads for
exact code. Do not claim live node-level or function-level graph evidence for
Aming Claw itself unless an active `aming-claw` graph exists.

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

HN demo first-run is a narrow exception because it uses a generated isolated
fixture, not the user's active workspace. When a user asks to try, preview, or
run the HN demo and no project is registered, run
`node frontend/dashboard/scripts/e2e-hn-demo.mjs --ensure-fixture --no-browser`
from the Aming Claw plugin checkout. Use the returned
`project_id="aming-claw-hn-demo"` for demo dashboard links. The runner is
included in the plugin payload, so the first-run `--no-browser` path does not
require a dashboard npm install. Do not scan the plugin checkout or bootstrap
the current app just to obtain a project id.

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
request. If the target root contains Aming Claw plugin/runtime artifacts such
as `.mcp.json` with `--project aming-claw`, `.codex-plugin/`,
`.claude-plugin/`, `.agents/plugins/`, or `shared-volume/codex-tasks/`, stop
and ask the user to clean them up or choose the real project root before graph
build. After bootstrap, open:
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

Do not treat weak documentation matches as impact scope. Reconcile records them
in doc asset state with path/hash/status/proposal evidence; only accepted doc
bindings should be used when claiming node-owned documentation.

When summarizing work, explicitly report whether a hint is still uncommitted or
not yet materialized into the graph.

## Manual Fix SOP

Use the manual-fix SOP for ordinary V1 implementation, observer-hotfix, chain
rescue, and other bounded work that should stay under observer control. The
canonical SOP is `docs/governance/manual-fix-sop.md`; the compact session
checklist is `aming-claw://mf-sop`.

## Context Registry

Before starting observer-heavy work or dispatching bounded workers, resolve the
role-scoped context packs for the active project:

```text
context_pack_resolve(project_id="<project_id>", role="observer", mode="<mode>")
```

The registry is DB-first with source-controlled fallback docs. It is the place
for local product principles, expert review rules, task context, and private
observer-only judgment that should not be shipped in public skills.

Context boundaries are part of the contract:

- Observer sessions may receive `private_founder` packs when the local operator
  imported them into the governance DB.
- `mf_sub`, chain dev/test/qa/merge, and other worker roles must receive only
  packs explicitly allowed for that role.
- Do not paste private observer context into worker prompts. Translate it into
  scoped contracts, acceptance criteria, target files, and review gates.
- Resolution evidence should record pack ids, versions, and hashes; do not log
  private pack bodies as timeline evidence.

If no local pack exists, fall back to
`skills/aming-claw/references/observer-context-safe.md` for observer-safe
expertise routing. Private strategy can be imported locally with
`context_pack_seed_private_file`; its body must not be committed to git.

During MVP, Chain is not the default path for routine implementation. The V1
implementation default is observer-led Manual Fix: backlog row, graph
discovery, local Codex `mf_sub` workers when useful, focused tests, explicit
commit files, Chain trailers, post-commit scope reconcile, and backlog close.
Chain trailers are MF audit anchors and do not mean auto-chain execution is
active. Future releases may revise the default, but V1 sessions should not
enter governance task_create dev/test/qa/merge or executor release flows unless
the user explicitly asks to test chain automation.
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
Before `spawn_agent`, the observer must run and record the local dispatch gate
`agent.governance.mf_subagent_contract.validate_mf_subagent_dispatch_gate`.
The gate must prove an isolated branch/worktree/file fence, `base_commit`,
`target_head_commit`, `merge_queue_id`, `fence_token`, owned files, current
target graph evidence, and dirty-scope evidence. Existing branch adoption is
allowed only with explicit adoption evidence; target/main worktree dispatch and
active-graph-stale dispatch are blocked by default.
Dispatch into the target/main worktree is blocked by default. A same-worktree
exception requires `same_worktree_allowed=true`, an explicit operator reason,
exact dirty-scope evidence, and observer timeline evidence before dispatch.
Use role/capability boundaries for parallel workers: `mf_sub` sessions may use
the finish gate and task-scoped audited graph queries with
`query_source=mf_subagent`, `parent_task_id`/`task_id`, and `fence_token`.
Observer/coordinator remains required for merge queue writes, merge execution,
graph reconcile/activation, backlog close, ServiceManager/governance restarts,
worktree cleanup, and other privileged state changes. Do not tell a subagent to
identify as observer to get graph access.
For deterministic MF workflow workers, use the privileged-stage graph
`dispatch -> implementation_wait -> handoff_gate -> merge_gate ->
merge_queue_entry -> merge_preview -> live_merge -> reconcile -> close_gate ->
done`; every stage after handoff must carry compact precheck evidence and a
token whose subject still matches the fenced commit/fence state.

## Observer Operating Modes

Use an explicit observer mode so requirement design does not accidentally turn
into long-running execution, and execution supervision does not stop at a
planning artifact.

### Design Alignment Mode

Design Alignment Mode is the default when the user is discussing requirements,
asking for prioritization, evaluating tradeoffs, designing test scenarios,
filing a backlog row, creating a contract, or saying to start a subagent and
continue the conversation. Typical trigger phrases include "先调研", "设计
contract", "提进 backlog", "启动 subagent 后停止", "我们继续对齐需求", and
"不用等待完成".

In this mode the observer may:

- check runtime, graph, backlog, and worktree state;
- run graph-first discovery and impact analysis;
- design the test-scenario policy and contract;
- upsert backlog rows and append planning/dispatch timeline evidence;
- dispatch a bounded `mf_sub` worker only when the user explicitly asks or an
  approved contract calls for it.

Design Alignment Mode is dispatch-and-stop. After a subagent handoff, do not
wait for completion, implement locally, merge, close backlog rows, push, or
release gates unless the user explicitly switches to execution supervision or
the governance contract already documents that transition.

### Execution Supervisor Mode

Execution Supervisor Mode is used only after explicit user intent to execute or
supervise work to completion. Typical trigger phrases include "推进实施",
"进入执行模式", "监视任务完成", "等待完成审计", "merge", "retry", "批量推进",
and "我睡了你接管".

In this mode the observer may:

- select and order a batch of backlog rows, then check priority and file-scope
  conflicts before dispatch;
- start bounded `mf_sub` workers with audited dispatch evidence;
- wait for subagent completion and review candidate diffs;
- run focused tests, browser/E2E checks, preflight checks, timeline precheck,
  contract gates, and merge gates;
- file follow-up backlog rows or retry on gaps instead of treating failed or
  missing evidence as success;
- merge or accept completed work, queue reconcile, close backlog rows, and
  record close-ready evidence when gates pass.

Execution Supervisor Mode still cannot bypass contract, precheck, timeline, or
merge gates. Stop for a human decision when priorities conflict, the worktree
is unsafe, required evidence is missing, generated assets are unexplained, or a
merge/reconcile action would overwrite unrelated user work.

Implementation agents must be assigned bounded branches/worktrees and files,
then stop at `review_ready` or `waiting_merge` with structured evidence:
branch/worktree, owned changed files, tests run, graph query trace ids,
precheck evidence, generated assets policy, and risks/open questions.

For both modes, timeline evidence must reflect what actually happened:
dispatch events for worker starts, implementation evidence for code changes,
verification evidence for tests/browser/prechecks, retry evidence for failed
attempts, merge evidence for accepted candidates, and `close_ready` only when
the backlog contract and MF close gate are satisfied.

Observer merge review must check contract fit, diff scope, focused test and E2E
evidence, docs/test/config impact, generated assets policy, graph/reconcile
plan, Chain trailers, and backlog close policy before accepting a candidate for
merge. If changed docs/templates are not graph-bound, record that Asset Inbox
binding or Governance Hint follow-up is needed before claiming audit-grade node
coverage.

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

Commit explicit files only, and use Chain trailers as MF audit anchors. Chain
trailers do not indicate that auto-chain execution was active:

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

Before claiming AI Enrich or semantic review readiness, inspect
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
- `chain/executor unavailable`: advanced chain automation is unavailable or
  degraded; this does not block V1 graph/backlog/dashboard/Review Queue work.

Use a compact status shape when helping the user:

```text
Codex CLI: detected at <path>, version <version>, auth unknown.
Claude CLI: detected at <path>, version <version>, auth unknown.
Semantic route: <provider/model or unset>.
AI Enrich: ready / blocked because <reason>.
Advanced chain/executor: ready / degraded because <reason>.
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
- Commit MF changes with Chain trailers as audit anchors, not as evidence that
  auto-chain execution was active:

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
- [observer-context-safe.md](references/observer-context-safe.md): source-controlled fallback for observer-safe expertise routing.
- [plugin-packaging.md](references/plugin-packaging.md): repo-local plugin layout and publish cautions.
