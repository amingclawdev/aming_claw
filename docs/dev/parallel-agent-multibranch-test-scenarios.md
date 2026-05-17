# Parallel Agent Multibranch Test Scenarios

> Status: P0 design contract
> Backlog: `ARCH-PARALLEL-AGENT-TEST-SCENARIO-MATRIX`
> Scope: dry-run scenario matrix for parallel branch governance runtime

## Purpose

Parallel branch development touches task runtime state, branch/worktree identity,
merge ordering, graph snapshots, semantic projections, pending scope reconcile,
rollback, and dashboard/MCP read models. The test design has to lead the
implementation so schema and API choices remain testable.

This document is the contract for that work. Runtime implementation may start
with dry-run or pending tests, but every affected state transition must be
represented here before it is encoded in production code.

## PR Gate

Any PR that changes parallel branch runtime schema, merge queue behavior, graph
ref activation, semantic projection activation, pending scope keys, rollback
state, Chain integration, or dashboard/MCP read models must do one of these:

1. Implement or update a test mapped to the affected scenario.
2. Mark the scenario `pending` with an explicit infrastructure blocker.
3. Update this matrix when the behavior intentionally changes.

The dry-run oracle must not mutate the production git checkout or production
governance DB. Tests should use an isolated temporary repository and an
ephemeral SQLite database.

## Runtime Surfaces

| Surface | Responsibility |
| --- | --- |
| `BranchTaskRuntimeContext` | Durable branch/task state, lease/fence token, checkpoint, attempt, replay source, and recovery decision. |
| `MergeQueueRuntime` | Ordered merge queue, dependency blockers, revalidation, stale target handling, and merge result recording. |
| `BatchMergeRuntime` | Batch-level branch retention, rollback_required state, ordered replay, and cleanup authorization. |
| `GraphRefRuntime` | Active graph snapshot/projection refs for each branch, target ref, merge epoch, rollback epoch, and replay epoch. |
| `SemanticProjectionRuntime` | Projection activation and invalidation for branch snapshots, merge epochs, rollback epochs, and semantic job carry-forward. |
| `PendingScopeRuntime` | Branch/ref/batch-aware pending scope reconcile rows and materialization decisions. |
| `ManagedRefRuntime` | Same-project state for existing long-lived refs, release branches, stale target detection, archive policy, and deletion blockers. |
| `Dashboard/MCP read model` | Compact operator views for branch lanes, queue blockers, rollback timeline, graph epoch, and recovery actions. |
| `Chain adapter` | Optional Chain identity fields while MF/observer remains the first client. |

## Oracle Shape

Each scenario must define these dimensions:

| Dimension | Required content |
| --- | --- |
| `scenario_id` | Stable ID such as `PB-001`. |
| `mode` | `dry_run`, `unit`, `integration`, or `e2e`. |
| `preconditions` | Git refs, task states, leases, queue rows, graph refs, semantic projections, pending scope rows, and dashboard state before the trigger. |
| `events` | Ordered events such as restart, claim, merge attempt, branch rebase, graph activation, rollback request, or stale agent callback. |
| `expected.task_runtime` | Task state, attempt, lease owner, fence token, checkpoint, replay source, and terminal/blocked state. |
| `expected.merge_queue` | Queue position, dependency status, merge gate decision, stale/rebase flags, and operator actions. |
| `expected.git` | Branch head, target head, retained worktree, merge commit, revert/reset marker, and cleanup permission. |
| `expected.graph` | Active snapshot ref, branch snapshot ref, merge epoch, rollback epoch, replay epoch, and stale/materialized flags. |
| `expected.semantic` | Projection ID, projection status, semantic job status, event watermark, and carry-forward/rebuild decision. |
| `expected.pending_scope` | Branch/ref/batch scoped pending row state and materialization outcome. |
| `expected.dashboard_mcp` | Compact rows and action affordances visible to the operator without loading full backlog or graph payloads. |
| `blocked_by` | Infrastructure flags that prevent a real test today. Empty means the test should be implemented immediately. |

Example dry-run oracle:

```json
{
  "scenario_id": "PB-001",
  "mode": "dry_run",
  "events": ["service_restart", "observer_recovery_scan"],
  "expected": {
    "task_runtime": ["T3 lease expired", "T5 lease expired"],
    "merge_queue": ["T4 dependency_blocked by T2"],
    "git": ["retain all batch branches"],
    "graph": ["active target ref remains at T1 merge epoch"],
    "semantic": ["no projection activation for failed or unfinished branches"],
    "pending_scope": ["no cross-branch row reuse"],
    "dashboard_mcp": ["show recovery actions for T2/T3/T5"]
  },
  "blocked_by": ["I1", "I2", "I3", "I5", "I7"]
}
```

## Infrastructure Flags

| Flag | Meaning |
| --- | --- |
| I0 | Documentation-only guard is available. |
| I1 | Durable `BranchTaskRuntimeContext` store exists. |
| I2 | Durable `MergeQueueRuntime` store exists. |
| I3 | Branch/ref-aware graph snapshot refs exist. |
| I4 | Semantic projection activation and rollback epochs exist. |
| I5 | Branch/ref/batch-aware `pending_scope_reconcile` keys exist. |
| I6 | Governance Hint add/remove/change deltas are invertible. |
| I7 | Dashboard/MCP compact read model exists. |
| I8 | Chain adapter identity fields exist without requiring Chain execution. |
| I9 | Batch branch/worktree retention and cleanup policy exists. |
| I10 | Managed ref runtime store, decision oracle, and governance API exist for existing long-lived branches. |
| I11 | MF subagent backend contract and role profile exist. |

## Fixture Topology

The canonical fixture is a five-task batch against a temporary target repo.
Generated git-backed target repos are created through
`agent/tests/fixtures/parallel_project.py` so branch/worktree and merge-preview
tests share the same isolated project shape instead of relying on the Aming
Claw source checkout.

| Fixture item | Value |
| --- | --- |
| Project | `fixture-parallel-project` |
| Base target | `main@B0` |
| Batch | `batch-parallel-001` |
| Target branch | `main` |
| Worktree root | Temporary directory created by the test harness |
| DB | Ephemeral SQLite database |
| Graph baseline | `snapshot-main-B0` with `projection-main-B0` |
| Branch naming | `codex/PB001-T<task>-<slug>` |

Task fixture:

| Task | Branch | Dependency | Runtime state at restart fixture | Intent |
| --- | --- | --- | --- | --- |
| T1 | `codex/PB001-T1-scope-reconcile` | none | `merged` | Foundation scope reconcile and graph baseline update. |
| T2 | `codex/PB001-T2-branch-graph-refs` | T1 | `merge_failed` | Branch/ref graph snapshot support. |
| T3 | `codex/PB001-T3-task-runtime` | T1 | `running` with expired lease | Branch task runtime and replay support. |
| T4 | `codex/PB001-T4-dashboard-read-model` | T2 | `queued_for_merge` | Operator read model; complete but blocked by T2. |
| T5 | `codex/PB001-T5-chain-adapter` | T3 | `running` with expired lease | Chain compatibility hook. |

## Scenario Matrix

| ID | Family | Trigger | Expected decision | Blocked by | Future tests |
| --- | --- | --- | --- | --- | --- |
| PB-001 | Machine restart recovery | Service restarts with T1 merged, T2 merge_failed, T4 queued_for_merge, T3/T5 unfinished. | Keep T1 merged; keep T2 merge_failed for observer action; mark T4 dependency_blocked; expire T3/T5 leases; retain all batch branches; activate no unfinished graph/semantic projections. | Runtime store, recovery API, and checkpoint fence are implemented; live executor replay remains gated. | `test_parallel_branch_runtime.py`, `test_merge_queue_runtime.py`, `test_graph_governance_api.py` |
| PB-002 | Merge dependency ordering | Downstream branch requests merge before upstream foundation branch. | Reject merge with `waiting_dependency` or `dependency_blocked`; no target branch mutation; no graph ref activation. | Implemented with pure decision oracle, durable SQLite queue replay, fenced governance enqueue API, dry-run merge-gate planning, and read-only merge preview evidence; live merge execution remains gated. | `test_merge_queue_runtime.py`, `test_graph_governance_api.py` |
| PB-003 | Target branch moves | Upstream branch merges after downstream branch was validated. | Mark downstream `stale_after_dependency_merge`; require rebase/sync, scope reconcile, semantic projection check, and merge preview. | Implemented with pure decision oracle, durable SQLite queue replay, dry-run merge-gate planning, and stale target-head merge preview evidence; scope/materialization integration remains separate. | `test_merge_queue_runtime.py`, `test_graph_rollback_epoch.py` |
| PB-004 | Wrong merge order rollback/replay | Batch detects severe integration issue after an invalid merge order. | Enter `rollback_required`; retain all batch branches/worktrees; rollback target ref and graph ref; replay retained branch heads in corrected queue order. | Implemented with pure decision oracle, durable SQLite batch replay, and governance rollback-planning API; live rollback execution remains gated. | `test_batch_merge_rollback.py`, `test_graph_governance_api.py` |
| PB-005 | DB rollback consistency | Code rollback is requested after graph/semantic/pending rows changed. | Move graph snapshot refs and semantic projection refs to rollback epoch; mark superseded branch projections inactive; requeue or abandon semantic jobs by epoch; scope rows are isolated by branch/ref/batch. | Graph/projection/pending-scope read model implemented; abandoned merge semantic jobs are exposed and cancellable by rollback epoch. | `test_graph_rollback_epoch.py` |
| PB-006 | Governance Hint rollback | A hint is added, changed, then removed across branch commits. | Incremental reconcile emits add/change/remove deltas; removed hint cannot leave stale graph binding; rollback restores prior binding state. | Implemented as invertible delta oracle; reconcile integration remains pending. | `test_governance_hint_rollback.py` |
| PB-007 | Chain compatibility | A Chain stage records branch runtime identity while MF remains the active client. | Accept optional `chain_id`, `root_task_id`, `stage_task_id`, `stage_type`, and `retry_round`; runtime semantics do not require Chain execution. | Implemented for runtime identity and no-execution Chain payload adapter; full Chain parallel execution remains future work. | `test_parallel_branch_runtime.py`, `test_chain_parallel_branch_adapter.py` |
| PB-008 | Old agent resurrection | A stale agent callback tries to complete or merge after lease recovery by another agent. | Reject stale fence token; record ignored callback; do not change task state, queue state, branch head, graph ref, or semantic projection. | Implemented for checkpoint/recovery API and merge-result recording; live merge execution remains gated. | `test_parallel_branch_runtime.py`, `test_graph_governance_api.py` |
| PB-009 | Cleanup retention | Batch has unresolved merge_failed or rollback_required state. | Block branch/worktree cleanup; cleanup allowed only after batch accepted, abandoned, or explicitly archived with rollback evidence. | Implemented with pure decision oracle and durable SQLite batch replay; live cleanup execution remains gated. | `test_batch_merge_rollback.py` |
| PB-010 | Dashboard/MCP compact read model | Operator opens dashboard or calls MCP after mixed parallel state. | Return bounded payload: branch lanes, task states, dependency blockers, rollback epoch, graph epoch, and action affordances without full backlog/graph expansion. | Implemented as pure-state read model and read-only governance API over durable stores; dashboard UI wiring remains future work. | `agent/tests/test_parallel_branch_read_model.py`, `agent/tests/test_graph_governance_api.py`, `frontend/dashboard/scripts/e2e-parallel-branches.mjs` |
| PB-011 | Branch graph artifact isolation | Branch-local graph artifacts are produced before merge. | Store branch artifacts as one-hop candidate evidence from the target graph; do not chain branch candidates or mutate active target graph refs until target scope reconcile after merge. | Implemented for graph ref/projection candidate read model and one-hop artifact policy. | `test_graph_rollback_epoch.py`, `test_batch_jobs.py` |
| PB-012 | Multi-project and batch isolation | Two projects or batches reuse task IDs and branch slugs. | Runtime keys include project, batch, branch/ref, and attempt identity; no task, queue, pending scope, graph, or semantic row crosses boundaries. | Implemented for branch context and merge queue scope; graph/semantic isolation still depends on I3/I4. | `test_parallel_branch_runtime.py`, `test_merge_queue_runtime.py` |
| PB-013 | Existing long-lived ref governance | A project import discovers release and feature branches that already have many commits. | Keep one project identity; create managed ref contexts; detect target movement as stale; merge code through target reconcile; archive source ref context instead of deleting a project. | Implemented as SQLite managed-ref runtime, decision oracle, and governance API; dashboard wiring remains pending. | `test_managed_ref_runtime.py`, `test_graph_governance_api.py` |
| PB-014 | Managed ref bootstrap/import | Operator imports an already-branched repository. | Dry-run classifies target, short-lived agent, managed, ignored, unmanaged, and blocked refs; apply only creates/refreshes managed ref contexts in the same project and never activates branch-local graph truth. | Implemented for supplied refs and git branch discovery through governance API. | `test_managed_ref_runtime.py`, `test_graph_governance_api.py` |
| PB-015 | MF subagent backend contract | Observer delegates one branch implementation to a Codex subagent while executor remains offline. | Build an `mf_sub` payload from `BranchTaskRuntimeContext`; require backlog, worktree, branch, target head, attempt, and fence identity; normalize only checkpoint/test/change evidence into merge-queue readiness; reject stale fences and merge/push/graph activation attempts. | Implemented as backend-neutral contract helpers, finish-gate checkpoint validation, and merge-queue checkpoint enforcement; live subagent spawning remains operator-driven. | `test_mf_subagent_contract.py`, `test_graph_governance_api.py`, `test_role_config.py` |

## Immediate Test Slice

The first slice is documentation-backed and can run before runtime facilities
exist:

| Test | Purpose | Status |
| --- | --- | --- |
| `agent/tests/test_parallel_branch_test_scenarios.py` | Verifies this contract exists, includes required scenario IDs, includes every oracle dimension, preserves the five-task restart fixture, and states the PR gate. | Implement immediately |

The next slice should create dry-run data structures without production code:

| Test | Purpose | Blockers |
| --- | --- | --- |
| `agent/tests/test_parallel_branch_runtime.py` | Pure-state `BranchTaskRuntimeContext` fixture for PB-001, PB-007, PB-008, PB-012. | I1 |
| `agent/tests/test_merge_queue_runtime.py` | Pure-state merge dependency and stale target decisions for PB-001, PB-002, PB-003. | I2 |
| `agent/tests/test_batch_merge_rollback.py` | Batch retention, rollback_required, and ordered replay decisions for PB-004 and PB-009. | Implemented as dry-run oracle; real git/DB rollback still depends on I3/I4/I5. |
| `agent/tests/test_parallel_branch_read_model.py` | Compact operator payload for PB-010: branch lanes, queue blockers, rollback timeline, graph epochs, actions, counts, and truncation. | Implemented as pure-state oracle; live MCP/dashboard endpoint still depends on durable queue/batch stores. |
| `agent/tests/test_graph_rollback_epoch.py` | Graph snapshot/projection activation, rollback, pending-scope isolation, and branch artifact isolation for PB-005 and PB-011. | Implemented for graph/projection/pending-scope read model; semantic job invalidation remains pending. |
| `agent/tests/test_governance_hint_rollback.py` | Hint add/change/remove/rollback-restored inverse delta behavior for PB-006. | Implemented as pure delta oracle; incremental reconcile integration remains pending. |
| `agent/tests/test_managed_ref_runtime.py` | Managed ref import, stale target detection, merge readiness, archive policy, and project deletion guard for PB-013. | Implemented as SQLite state/decision oracle; governance API coverage lives in `test_graph_governance_api.py`; dashboard wiring remains future work. |
| `agent/tests/test_mf_subagent_contract.py` | Backend-neutral `mf_sub` payload/result contract for PB-015. | Implemented |
| `frontend/dashboard/scripts/e2e-parallel-branches.mjs` | Operator read model for PB-010. | I7 |

The MF adapter API slice is now covered by
`test_parallel_branch_allocate_route_materializes_worktree_and_updates_read_model`
and `test_parallel_branch_recover_and_checkpoint_routes_enforce_fence`. These
tests prove a governance client can allocate a deterministic branch runtime,
materialize an isolated worktree with branch-local graph artifacts, recover
expired leases, reject stale fence tokens, and see the result through the
compact read model.

The merge-queue API slice is covered by
`test_parallel_branch_merge_queue_route_enforces_fence_and_returns_decision`.
It proves a stale agent cannot enqueue a branch after fence recovery, and that
successful enqueue returns the same dependency-blocker oracle used by durable
queue replay and the compact read model.

The merge-gate planning API slice is covered by
`test_parallel_branch_merge_gate_route_returns_dry_run_plan`. It proves an
operator can ask whether one queued branch is mergeable after queue dependency,
target freshness, rollback state, and required evidence checks, while default
dry-run mode still prevents target ref mutation.

The merge-preview evidence API slice is covered by
`test_parallel_branch_merge_preview_route_builds_gate_evidence`. It proves git
conflict evidence can be produced by read-only `rev-parse`, `merge-base`, and
`merge-tree` commands, then fed directly into the merge gate plan without
mutating the target worktree or refs.

The gated live merge execution slice is covered by
`test_parallel_branch_merge_execute_route_dry_run_then_live_merge`. It proves
the API defaults to dry-run with no target mutation, and only executes a merge
in a temporary repository when the caller explicitly allows target-ref mutation
and the merge gate passes.

The merge-result recording API slice is covered by
`test_parallel_branch_merge_result_route_records_with_fence`. It proves an
externally performed merge can be recorded into durable queue/context state,
and that stale fence tokens cannot rewrite the result after recovery.

The batch rollback planning API slice is covered by
`test_parallel_branch_batch_runtime_route_returns_rollback_plan`. It proves an
operator can persist batch runtime state from branch contexts, mark the batch
`rollback_required`, see retained branch/worktree evidence and cleanup blockers,
and fetch the same rollback plan through the compact read model.

The DB rollback consistency slice is covered by
`test_pb005_rollback_invalidates_open_semantic_jobs_for_abandoned_merge`. It
proves rollback epoch state surfaces semantic jobs by disposition and cancels
open jobs tied to abandoned merge snapshots without touching the rollback target
or retained branch candidate jobs.

## Observer Recovery Dry Run

For PB-001, an observer recovery scan after machine restart should compute:

| Task | Observed state | Recovery decision |
| --- | --- | --- |
| T1 | `merged` | Leave merged; target and graph refs remain anchored at T1 merge epoch. |
| T2 | `merge_failed` | Keep failed; require observer decision: fix/rebase, abandon, or rollback batch. |
| T3 | `running`, lease expired | Mark reclaimable; replay from last checkpoint on retained branch. |
| T4 | `queued_for_merge`, depends on T2 | Move to `dependency_blocked`; do not merge until T2 is resolved and T4 is revalidated. |
| T5 | `running`, lease expired, depends on T3 | Mark reclaimable but blocked by T3 recovery. |

No cleanup is allowed while T2, T3, or T5 are unresolved. No semantic projection
from T2, T3, T4, or T5 may become active on the target ref during recovery.

## Rollback Dry Run

Rollback scenarios must treat code and DB state as one recoverable unit.

Required rollback assertions:

| Area | Assertion |
| --- | --- |
| Git | Target ref is reset or reverted to the selected rollback point; retained branch heads are still available for replay. |
| Graph refs | Active target graph ref points to a snapshot/projection compatible with the rollback target. |
| Graph events | Rollback epoch records which merge epoch was abandoned and why. |
| Semantic nodes/edges/jobs | Rows tied to abandoned epochs are inactive, stale, cancelled, or rebuild-required; they are not silently treated as current. |
| Pending scope | Rows are keyed by project, branch/ref, batch, and epoch so rollback does not reuse wrong materialization state. |
| Governance Hint | Hint remove/change emits inverse graph deltas; rollback restores prior binding state. |
| Dashboard/MCP | Operator can see rollback_required, rollback epoch, retained branches, and replay order. |

## Design Notes

- Chain remains serial inside a single chain, but the branch runtime must be
  capable of running multiple Chain or MF clients in parallel.
- MF/observer is the first client. Chain identity is optional metadata until
  Chain execution is ready to use the runtime directly.
- Branch/worktree cleanup is not a background garbage-collection concern until
  batch acceptance, abandonment, or explicit archive has happened.
- Active graph and semantic projections are target-ref facts. Branch-local
  artifacts are one-hop candidate deltas from a target graph. They must be
  recomputed when the target ref moves and must not chain from prior branch
  candidates.
- Existing long-lived refs are managed under the same project identity. They
  can carry ref-local snapshot/projection pointers, but merge still updates code
  first and then reconciles the target ref; project deletion is blocked until
  unresolved managed refs are archived or abandoned.
- Existing branch import must go through managed-ref bootstrap dry-run before
  apply. Dry-run classifications are part of the operator evidence, and refresh
  clears stale graph/preview pointers rather than treating old branch graph
  state as target truth.
- MF subagents are branch workers, not merge workers. Their contract can
  report checkpoint, changed files, tests, blockers, and readiness; those
  fields are claims until the finish gate validates them and records a
  `mf_sub_finish_gate` checkpoint. Merge queue requires that checkpoint for
  `mf_sub` entries. Merge, push, graph activation, release gates, task
  creation, and worktree cleanup remain owner-controlled operations.
- Backlog and dashboard queries must stay compact by default; detailed state
  should be fetched by ID.
