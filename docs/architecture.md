# Aming Claw - System Architecture

> **Canonical V1 architecture document.** This document describes the stable
> Aming Claw control plane first, then lists chain/executor/gateway surfaces as
> advanced or experimental.
> Last updated: 2026-05-21 | V1 graph review control-plane alignment

## 1. Overview

Aming Claw V1 is a local graph-first governance system for AI-assisted code
review and implementation. Its stable path is:

1. Register or bootstrap a target project.
2. Build a commit-bound graph of files, functions, tests, docs, config, and
   relations.
3. Inspect that graph through the dashboard or MCP graph tools.
4. File or update backlog rows with graph evidence.
5. Run AI Enrich as typed proposals, not trusted state.
6. Accept or reject proposals through Review Queue gates.
7. Use Manual Fix for scoped implementation work.
8. Commit source changes, then run Update Graph/reconcile so later review uses
   the new graph truth.

The broader repository still contains an automated multi-role chain
(PM -> Dev -> Test -> QA -> Merge), executor workers, gateway hooks, and legacy
memory services. Those are advanced or experimental in V1. The V1 proof is the
graph/backlog/review/reconcile loop.

## 2. Design Principles

- **Project fact layer before model opinion.** AI sessions should query a
  project graph instead of re-guessing ownership, impact, tests, docs, and
  function locations from each diff.
- **Source before projection.** Committed code, source-controlled
  hints/config/rules, backlog rows, and accepted semantic events are durable source
  records. Graph snapshots, semantic projections, review queues, and materialized
  bindings are derived views.
- **Commit-bound graph state.** Every graph snapshot is pinned to a git commit.
  Dirty worktree state is not graph truth. Reconcile materializes a new graph
  after source changes are committed.
- **AI proposes; governance decides.** AI output that affects project memory or
  graph behavior becomes a typed proposal with precheck evidence. Server-side
  parsing, policy gates, Review Queue decisions, and reconcile remain
  authoritative.
- **Source-controlled graph repair.** Graph defects are fixed through reviewed
  source-controlled hints/config/rules plus reconcile, not direct graph database
  edits.
- **Local-first operation.** Governance DB, backlog, graph snapshots, review
  queues, plugin update state, and dashboard state live on the user's machine.
  AI features use local `claude` or `codex` CLI routing when configured.
- **Graceful degradation.** Chain/executor, ServiceManager, gateway, Redis,
  dbservice, and AI provider availability are separate runtime states. Graph
  query, backlog, dashboard, and Manual Fix can remain useful when advanced
  automation is degraded.

## 3. State Contract

The graph is not trusted because an AI said so. It is trusted as a replayable
projection from source records.

| State | Role | Examples |
|-------|------|----------|
| Source | Durable input | committed code, source-controlled hints/config/rules, backlog rows, accepted semantic events |
| Proposal | Untrusted workflow input | AI semantic payloads, graph/config suggestions, backlog suggestions |
| Derived projection | Materialized view | graph snapshots, semantic projections, file/test/doc/config bindings, review queue overlays |
| Runtime status | Operational evidence | health checks, version checks, operations queue rows, plugin update state |

SQLite is the local persistence substrate for governance state. It is not a
license to mutate graph truth directly. For graph structure, the authoritative
path is source -> commit -> reconcile -> query.

## 4. V1 Control Plane

```
User / Observer Session
    |                       \
    | dashboard              \ MCP tools
    v                         v
Governance Service (:40000) <------ Local AI CLI (optional)
    |
    +-- Project Registry / AI Config
    +-- Backlog Ledger
    +-- Commit-Bound Graph Snapshots
    +-- Semantic Jobs + Review Queue
    +-- Operations Queue
    +-- Source-Controlled Hint APIs
    +-- Reconcile / Update Graph
```

The dashboard and MCP tools expose the same control plane. The human can inspect
project state visually while an AI observer session performs graph queries,
files backlog rows, runs focused tests, and reports evidence.

## 5. Core Components

### 5.1 Governance Service (`:40000`)

The governance service is the central local control plane. In stable V1 it
provides:

- **Dashboard** served at `/dashboard`
- **Project registry** for workspaces, graph state, and AI routing
- **Graph governance** for snapshots, graph query, file inventory, function
  indexes, function hashes, relation evidence, and Update Graph
- **Semantic projection** for accepted node/edge semantic memory
- **Operations Queue** for reconcile, semantic jobs, graph-enrich/config
  proposals, and review work
- **Review Queue** for human accept/reject of AI-proposed memory or follow-ups
- **Backlog** as the durable work ledger
- **Source-controlled hint APIs** for file binding and graph repair
- **Runtime status** combining health, version, graph, and ServiceManager state

Key implementation areas:

- `agent/governance/server.py` - governance HTTP API
- `agent/governance/reconcile.py` and reconcile phases - graph build/update
- `agent/governance/state_reconcile.py` - state-only reconcile and projection
- `agent/governance/graph_hint_projection.py` - materialize graph structure hints
- `agent/governance/graph_structure_hints.py` - scan/write source graph hints
- `agent/governance/governance_hints.py` - doc/test/config binding hints
- `agent/governance/graph_structure_ops.py` - proposal/dry-run/apply pipeline

### 5.2 Dashboard

The dashboard is the shared visual panel for humans and AI-assisted sessions.
It should be treated as an inspection and review surface, not a separate source
of truth.

Stable V1 surfaces:

- **Projects** - register/select projects and inspect graph bootstrap state
- **Graph** - browse hierarchy, nodes, files, relations, tests, docs, config,
  and function indexes
- **Inspector** - inspect node metadata, hashes, evidence, and bindings
- **Operations Queue** - monitor graph builds, semantic jobs, reconcile work,
  and graph/config proposals
- **Review Queue** - accept/reject AI-proposed semantic memory and follow-ups
- **Backlog** - inspect requirements, defects, PR opportunities, MF rows, target
  files, and verification notes
- **AI Config** - configure local provider/model routing

### 5.3 MCP Server

The MCP server gives Codex/Claude sessions tool access to the same governance
state the dashboard shows.

Important stable tools:

- Runtime: `health`, `runtime_status`, `version_check`, `preflight_check`
- Graph: `graph_status`, `graph_operations_queue`, `graph_query`,
  `graph_pending_scope_queue`
- Backlog: `backlog_list`, `backlog_get`, `backlog_upsert`, `backlog_close`
- Manual operation support: `task_*`, `observer_mode`, `wf_*`, `node_update`
  where applicable
- Host/service status: `manager_health`, `governance_redeploy`,
  `executor_status`, `executor_scale`

MCP does not own long-lived governance or executor lifecycle. It exposes tools
and delegates service lifecycle to the host process or ServiceManager.

Key files: `agent/mcp/server.py`, `agent/mcp/tools.py`

### 5.4 Backlog

Backlog rows are the durable work ledger. They record intent, scope, target
files, acceptance criteria, risk, verification notes, and close evidence.

For V1, ordinary implementation work should be backlog-first and graph-first:
file/update the backlog row, query the graph, make a scoped change, run focused
tests, then report evidence before commit/reconcile/close.

### 5.5 Review Queue And AI Enrich

AI Enrich generates semantic summaries, intent, risks, and sometimes follow-up
suggestions. `ai_complete` means a proposal exists. It does not mean the
project memory is trusted.

Trusted semantic memory requires:

1. AI output in a machine-consumed shape.
2. Local precheck evidence when the output is structured.
3. Server-side parsing and policy checks.
4. Review Queue accept/reject.
5. Semantic projection materialization.

MCP graph queries used by AI semantic review produce trace ids. This makes
subagent/observer context gathering visible and auditable instead of hidden
inside chat history.

## 6. Graph And Reconcile

### 6.1 Commit-Bound Graph

The graph snapshot is a commit-bound project fact layer. It indexes:

- project hierarchy nodes
- primary, secondary, test, doc, and config files
- file hashes and feature hashes
- function line ranges and function hashes
- test function line ranges and hashes
- dependency/relation evidence
- file inventory and orphan state
- semantic current/stale/missing status

Function-level hashes allow governance to detect changed functions, stale
semantic payloads, and review impact more precisely than file-level checks.

### 6.2 Reconcile

Reconcile materializes graph state from committed source and governed inputs.

Full reconcile is heavier than grep because it builds the reusable project fact
layer. Steady-state work should prefer commit-bound incremental/scope reconcile
when the rule fingerprint and graph inputs permit it. Incremental reconcile
updates changed files/functions, affected edges, semantic carry-forward/stale
state, and review queues.

If graph rule inputs or source-controlled graph hints change in a way that
alters the rule fingerprint, full reconcile may be required.

### 6.3 Semantic Projection

Semantic memory is event/projection based:

- accepted semantic events are source records
- semantic projections are derived views for a specific graph snapshot
- stale/current decisions use graph commit, feature hash, file hash, function
  hash, and test-function hash evidence

This avoids fragile in-place state mutation. New accepted events or new graph
snapshots produce new projections.

## 7. Source-Controlled Graph Repair

V1 graph repair is constrained and replayable. It is not arbitrary graph DB
editing.

Supported source-controlled repairs:

| Repair | Meaning |
|--------|---------|
| file binding | attach orphan doc/test/config files to existing nodes |
| `add_edge` | add a reviewed graph relation |
| `suppress_edge` | suppress an incorrect inferred relation |
| `move_file` | move file ownership/binding to a different node |

Repair contract:

1. AI or observer identifies a graph defect.
2. AI may submit a typed proposal; proposal state is not trusted graph state.
3. Observer/human reviews graph evidence.
4. A source-controlled hint/config/rule is added or updated.
5. The change is committed.
6. Reconcile projects the repair into the graph.
7. Removing the hint withdraws the projected effect on the next reconcile.

This is the self-repair pattern: defects are not patched directly in the graph
database; they become durable, reviewable source inputs that future reconciles
can replay.

## 8. Manual Fix Workflow

Manual Fix is the stable V1 implementation workflow. It is observer-led: the
user can ask an Aming Claw-enabled AI session to perform the workflow rather
than manually running every command.

Core sequence:

1. Check runtime, graph status, operations queue, and backlog.
2. File or update the backlog row.
3. Query graph evidence before editing.
4. Scope the change to target files.
5. Run focused tests and relevant prechecks.
6. Report changed files, test evidence, and remaining warnings.
7. Commit only after user approval when requested.
8. Run Update Graph/reconcile after commit.
9. Close backlog with commit and verification evidence.

Manual Fix commits should carry Chain trailers when committed under governance:

```text
Chain-Source-Stage: observer-hotfix
Chain-Project: <project_id>
Chain-Bug-Id: <backlog_id>
```

The trailer is still used by version checks and runtime status, even though
auto-chain is not the default V1 implementation path.

## 9. V1 Data Flow

```
User asks for governed review or fix
    |
    +--> Dashboard inspection
    |
    +--> AI observer session via MCP
            |
            +--> runtime_status / graph_status / operations queue
            +--> graph_query for nodes, files, functions, tests, docs, impact
            +--> backlog row for intent/scope/evidence
            +--> optional AI Enrich typed proposal
            +--> Review Queue accept/reject
            +--> scoped Manual Fix
            +--> focused tests/prechecks
            +--> commit source changes
            +--> Update Graph / reconcile
            +--> backlog close evidence
```

## 10. Runtime Boundaries

Keep these states separate:

- plugin/skill/MCP assets are installed in the current editor session
- governance is running on port `40000`
- dashboard static assets are present
- the active workspace is registered/bootstrapped
- the active graph snapshot is current for the target commit
- ServiceManager is healthy
- executor/chain automation is available
- local AI CLIs are detected and project AI routing is configured
- self graph bundle compatibility is current for the installed runtime

`aming-claw start` starts governance. It does not prove plugin reload,
dashboard asset availability, ServiceManager/executor health, or AI CLI auth.

Graph snapshots are commit-bound. When the worktree is dirty, graph query
results describe the active snapshot commit, not uncommitted files.

## 11. API Surface

### REST API

The governance service owns the stable local HTTP API at `:40000`.

Stable V1 categories:

| Category | Purpose |
|----------|---------|
| Health/runtime | service health, version check, current runtime summary |
| Projects | project registry, bootstrap, project AI config |
| Graph | current state, snapshots, nodes, files, dashboard bundles, query |
| Reconcile | full/scope/state reconcile and graph update |
| Semantics | semantic jobs, projections, feedback/review queue |
| Backlog | local work ledger CRUD and close evidence |
| File hygiene/hints | orphan binding and source-controlled graph repair flows |

Advanced/experimental categories:

| Category | V1 status |
|----------|-----------|
| Tasks / auto-chain | advanced, not default V1 implementation |
| Workflow acceptance graph | available for imported workflow graphs |
| Memory API | legacy/advanced local memory surface |
| Redeploy endpoints | advanced ServiceManager/chain deployment support |

### MCP Tools

Core governance operations are available as MCP tools for AI sessions:
runtime checks, graph queries, backlog operations, operations queue inspection,
preflight, optional task/chain controls, and host/service status.

## 12. Advanced And Experimental Surfaces

### 12.1 Auto-Chain Pipeline

The auto-chain is the long-term workflow automation path:

```text
PM -> Dev -> Test -> QA -> Gatekeeper -> Merge
```

In V1 it is experimental. Ordinary fixes/features should use Manual Fix unless
the user explicitly asks to exercise chain automation.

Chain stages and gates remain useful as the future serial quality path:

| Gate | Stage | Checks |
|------|-------|--------|
| PM Gate | PM -> Dev | PRD has target files, verification, acceptance criteria |
| Checkpoint Gate | Dev -> Test | changed files exist and stay within scope |
| T2 Pass Gate | Test -> QA | structured test report passes |
| QA Pass Gate | QA -> Gatekeeper | QA recommendation and criteria pass |
| Version Gate | Any stage | git HEAD matches governed chain anchor |

Key files:

- `agent/governance/auto_chain.py`
- `agent/governance/executor_worker.py`
- `agent/executor_worker.py`

### 12.2 Executor And ServiceManager

Executor workers claim queued tasks and execute role-specific prompts. In V1,
executor/chain availability is not required for dashboard, graph query, backlog,
Review Queue, or Manual Fix.

ServiceManager supervises host-side services and can support redeploy flows. A
degraded ServiceManager should be reported separately from governance health.

### 12.3 Symmetric Redeploy

The symmetric redeploy contract lets Governance and ServiceManager request each
other's restart without self-restarting.

This is advanced in V1 and mainly relevant to chain/deploy automation. The
local plugin/dashboard graph MVP does not require it.

### 12.4 Telegram Gateway, Redis, And dbservice

These are optional or legacy/advanced surfaces:

- **Telegram Gateway** can route message ingress/egress for remote task flows.
- **Redis** can provide pub/sub and cache support for auxiliary flows.
- **dbservice/mem0** can provide semantic memory service behavior.

None of these are required for the stable V1 dashboard/MCP/backlog/graph review
control plane.

### 12.5 Workflow Acceptance Graph And Backfill

Workflow graph tools (`wf_summary`, `wf_impact`, `node_update`) apply when a
project imports or maintains an acceptance workflow graph. If no workflow graph
is imported, these tools should report that precondition rather than implying
coverage is healthy.

Backfill evidence exists for retroactively attaching verification evidence to
nodes promoted by older or bypass paths. It is useful for governance forensics,
but it is not the main V1 review loop.

### 12.6 Auto-Infer

The auto-infer path was designed for chain Dev -> QA transitions and acceptance
graph deltas. V1 Manual Fix uses explicit source changes plus Update Graph
instead.

## 13. Deployment Topology

Minimum viable local topology:

- Governance service on host port `40000`
- Dashboard assets available under `/dashboard`
- MCP server loaded by the editor/plugin session
- Local project registered/bootstrapped before graph-backed claims

Optional topology:

- ServiceManager for host service supervision
- Executor worker for chain automation
- Telegram gateway for remote ingress/egress
- Redis for auxiliary pub/sub/cache flows
- dbservice for advanced semantic memory surfaces

All stable V1 governance operations run locally on the host. Docker is optional
for auxiliary services and is not required for the dashboard/MCP/backlog/graph
control plane.
