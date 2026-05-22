# Aming Claw Architecture Map

Use this as a compact fallback when an active graph or semantic projection is
not available. When MCP is available, query the graph first and treat this file
as a navigation aid, not as live evidence.

## Source Of Truth

Committed code, source-controlled hints/config/rules, accepted semantic events,
and reconcile output are source inputs. The graph and semantic projection are
derived state. AI proposals are workflow input until a gate accepts them.

Do not repair graph state by editing the DB directly. Repair source, add a
source-controlled hint/config/rule, or accept a reviewed correction path, then
run the appropriate reconcile flow.

## Core Areas

- Runtime and MCP control plane: `agent/governance/server.py` (`L7.142`) hosts
  the HTTP API, review queue, graph-governance endpoints, semantic decision
  endpoints, and dashboard data surfaces.
- Project registration and bootstrap: `agent/governance/project_service.py`
  (`L7.97`) owns project registration, bootstrap orchestration, and full graph
  creation for newly governed projects.
- Graph construction and reconcile: `agent/governance/reconcile_phases/phase_z_v2.py`
  (`L7.123`) builds snapshot-native graph structure, function indexes, file
  inventory, and graph evidence used by review.
- Semantic memory: `agent/governance/reconcile_semantic_enrichment.py`
  (`L7.128`) handles node/edge semantic events, review queue payloads,
  projections, and carry-forward across reconcile.

## Review Control Plane

The MVP proof is the review control plane:

1. Build or update a commit-bound graph.
2. Queue semantic enrichment for selected nodes or edges.
3. Require structured AI output with self-precheck evidence.
4. Record audited graph queries made by the AI session.
5. Send proposals to Review Queue.
6. Accept/reject semantic memory separately from graph mutations.
7. Repair durable graph defects through hints/config/rules/reconcile.

## Reconcile Choice

Use scope/incremental reconcile for ordinary committed code changes when the
graph rule fingerprint is stable. Use full reconcile when graph-building code,
semantic enrichment config, source hints, or other graph rule inputs change, or
when `graph_status` says `recommended_action=run_full_reconcile`.

## Bootstrap Excludes

Before bootstrapping a project, confirm generated, vendored, nested, or
tool-owned directories. Common defaults are `node_modules`, `dist`, `build`,
`.expo`, `.next`, and `coverage`; project-specific paths such as `node`,
`vendor`, generated clients, fixture clones, scratch worktrees, and downloaded
assets must be reviewed before the first graph build.
