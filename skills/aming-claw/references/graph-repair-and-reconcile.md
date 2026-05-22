# Graph Repair And Reconcile

The graph is a replayable projection. Repairs should change replay inputs, not
materialized graph rows.

## Repair Inputs

- Code changes: edit source, tests, docs, or config, then commit.
- Governance Hint: source-controlled metadata for binding orphan doc/test/config
  files that already exist in file inventory.
- Graph structure hint/config/rule: reviewed repair metadata or registered
  logic for language/framework-specific topology.
- Semantic events: accepted review memory, projected after Review Queue
  approval.
- Reconcile output: snapshot and projection materialized from committed inputs.

## Reconcile Modes

Scope/incremental reconcile is for normal code changes after a clean commit
when graph rule fingerprints match. It updates changed files/functions,
affected edges, semantic stale/current state, and review queues.

Full reconcile is required when the graph algorithm, semantic enrichment config,
source hints, or replay rules change. A rule-fingerprint mismatch is a full
reconcile signal even if the visible source edit was not a YAML config change.

## Operator Rules

- Check `graph_status` before choosing reconcile mode.
- Do not call a dirty worktree graph state trusted; graph snapshots are
  commit-bound.
- Do not use AI proposals as graph state.
- If a semantic proposal suggests topology repair, classify it as
  `config_patch`, `registered_action_needed`, `adapter_evidence_gap`, or
  `core_algorithm_gap` before changing graph-building code.
- After a full reconcile, verify semantic carry-forward and queue only the
  stale or missing semantic work that is in scope.
