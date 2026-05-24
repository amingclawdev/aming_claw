# Governance Documentation

This directory contains the deeper governance references behind the V1 local
plugin experience. For first-time users, start with [README.md](../../README.md)
and [onboarding.md](../onboarding.md); this directory is for operators and
agents who need the rules behind backlog, Manual Fix, graph governance, and the
experimental chain.

## V1 Governance Model

V1 is graph-first and local-first:

- The dashboard and MCP graph tools are the primary control plane.
- Target projects must be explicitly registered/bootstrapped before graph-backed
  claims are available.
- Graph snapshots are commit-bound and should be built from a clean worktree.
- Backlog rows are the canonical work ledger; do not file work by editing
  `docs/dev` scratch files.
- Manual Fix is the normal V1 implementation path while chain automation is
  experimental. MF still requires backlog-first, graph-first, focused tests,
  Chain trailers, post-commit Update Graph, and backlog close evidence.
- Observer-only collaboration is the default for parallel MF work. The observer
  clarifies scope, checks graph/backlog/runtime state, writes the backlog row
  and `mf_parallel.v1` contract, starts agents only when the user explicitly
  asks or an approved contract calls for it, and reviews merge candidates.
  Implementation agents are assigned bounded branches/worktrees/files and stop
  at `review_ready` or `waiting_merge` with structured evidence; they do not
  merge, push, release gates, activate graph refs, close backlog, delete
  worktrees, or mutate merge queues.
- AI Enrich creates proposals that require Review Queue approval before they
  become trusted semantic memory.
- Source-controlled hints/config/rules are the durable repair inputs for graph
  defects. Reconcile materializes the graph projection; direct DB graph edits
  are not the trusted repair path.

Observer merge review checks contract fit, diff scope, focused test and E2E
evidence, docs/test/config impact, generated assets policy, graph/reconcile
plan, Chain trailers, and backlog close policy. The observer does not wait,
merge, or push by default; those actions require an explicit user request or a
documented governance transition. If changed docs/templates are not graph-bound,
record an Asset Inbox binding or Governance Hint follow-up before claiming
audit-grade node coverage.

## V1 Entry Points

| Need | Start Here |
| --- | --- |
| Install and run locally | [README.md](../../README.md) |
| Register a target project | [onboarding.md](../onboarding.md) |
| Configure project YAML and AI routing | [config/aming-claw-yaml.md](../config/aming-claw-yaml.md) |
| Adapt graph rules for a language/framework | [semantic enrichment config](../config/semantic-enrichment.md) |
| Use MCP tools and graph queries | [skills MCP guide](../../skills/aming-claw/references/mcp-tools.md) |
| Follow Manual Fix in V1 | [skills MF checklist](../../skills/aming-claw/references/mf-sop.md) |
| Package Codex/Claude plugins | [plugin packaging notes](../../skills/aming-claw/references/plugin-packaging.md) |

## Specifications

| File | V1 Status | Description |
| --- | --- | --- |
| [manual-fix-sop.md](manual-fix-sop.md) | Active (R11 stale — pre-2026-05-01 trailer migration) | Canonical MF history and constraints. Use the compact skill checklist for day-to-day V1 work; R11's `/api/version-update` description is deprecated under trailer-priority. |
| [version-control.md](version-control.md) | Active | Version gate and Chain trailer lifecycle. |
| [memory.md](memory.md) | Active | SQLite/FTS memory backend and semantic search notes. |
| [acceptance-graph.md](acceptance-graph.md) | Advanced | Older workflow acceptance graph; distinct from the snapshot graph used by dashboard. |
| [auto-chain.md](auto-chain.md) | Experimental in V1 | PM -> Dev -> Test -> QA -> Merge automation. Not the default V1 implementation path. |
| [gates.md](gates.md) | Experimental in V1 | Gate definitions for chain automation. |
| [conflict-rules.md](conflict-rules.md) | Advanced | Task conflict rules. |
| [audit-process.md](audit-process.md) | Advanced | Chain full-process audit procedure. |
| [reconcile-workflow.md](reconcile-workflow.md) | Advanced | Reconcile design for graph/workflow repair. |
| [feature-index.md](feature-index.md) | Generated/reference | Reconcile feature index; may lag the active dashboard snapshot. |

## Historical Material

Files under `docs/dev/` are handoffs, proposals, scratch notes, and historical
audit records. They are useful for maintainers, but they are not the V1 user
entry point and should not be treated as the canonical backlog.
