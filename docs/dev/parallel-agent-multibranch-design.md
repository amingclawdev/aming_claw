# Parallel Agent Multibranch Runtime Design

> Status: P0 design contract
> Backlog: `ARCH-PARALLEL-AGENT-MULTIBRANCH-DESIGN-DOC`
> Parent: `ARCH-PARALLEL-AGENT-MULTIBRANCH-EXECUTION`
> Test oracle: `docs/dev/parallel-agent-multibranch-test-scenarios.md`

## Purpose

Aming Claw needs parallel branch execution without losing the serial quality
properties of Chain and governance. The runtime must let multiple agents work
in isolated branches or worktrees while merges remain ordered, auditable,
rollbackable, and graph-aware.

The governing test contract is
`docs/dev/parallel-agent-multibranch-test-scenarios.md`. Runtime schema and API
work must either implement the mapped scenario tests or update the scenario
matrix with explicit pending infrastructure flags.

## Non-Goals

- Do not increase executor parallelism before one branch can safely allocate,
  checkpoint, queue, merge, reconcile, and clean up.
- Do not encode MF-only assumptions. MF/observer is the first client, but Chain
  must be able to use the same runtime later.
- Do not treat branch-local graph or semantic evidence as active target-ref
  truth; merge acceptance only permits target-ref scope reconcile to materialize
  a target snapshot.
- Do not chain graph reconcile from a branch graph candidate. Branch graph
  state must remain a one-hop delta from the selected target commit graph.
- Do not rely on a shared dirty checkout for concurrent work.

## Scenario Coverage

| Scenario | Runtime pressure |
| --- | --- |
| PB-001 | Restart recovery for five mixed-state tasks. |
| PB-002 | Merge dependency ordering. |
| PB-003 | Target branch moves and downstream stale handling. |
| PB-004 | Batch rollback and replay after wrong merge order. |
| PB-005 | DB rollback for graph, semantic, jobs, and pending scope rows. |
| PB-006 | Governance Hint add/change/remove rollback. |
| PB-007 | Chain compatibility without requiring Chain in MVP. |
| PB-008 | Stale agent resurrection and fence enforcement. |
| PB-009 | Cleanup retention while batch is unresolved. |
| PB-010 | Compact dashboard/MCP read model. |
| PB-011 | Branch graph artifact isolation before merge. |
| PB-012 | Multi-project and batch isolation. |
| PB-013 | Existing long-lived ref governance under one project identity. |
| PB-014 | Managed ref bootstrap/import for existing branched repositories. |
| PB-015 | MF subagent backend contract for branch-isolated worker execution. |

## Runtime Surfaces

| Surface | Owns | Does not own |
| --- | --- | --- |
| `BranchTaskRuntimeContext` | Branch/task state, lease/fence token, checkpoint, attempt, replay source, current agent, and recovery decision. | Merge ordering or target-ref mutation. |
| `MergeQueueRuntime` | Dependency ordering, merge preview, merge gate state, target branch freshness, conflict state, and merge result. | Long-lived agent execution. |
| `BatchMergeRuntime` | Batch grouping, retained branches/worktrees, rollback_required state, rollback target, replay order, and cleanup authorization. | Per-task implementation details. |
| `GraphRefRuntime` | Active target graph refs, branch candidate graph refs, merge epoch, rollback epoch, and replay epoch. | Semantic payload trust decisions. |
| `SemanticProjectionRuntime` | Projection activation, semantic job carry-forward, stale/cancelled state, and epoch-bound rebuild decisions. | Structural graph snapshot creation. |
| `PendingScopeRuntime` | Branch/ref/batch/epoch-aware scope reconcile rows and materialization decisions. | Merge queue policy. |
| `ManagedRefRuntime` | Same-project governance state for existing long-lived refs, release branches, and large feature branches. | Treating ordinary refs as separate projects or merging graph truth across refs. |
| `MF subagent contract` | Backend-neutral branch worker input/result schema, fence identity, checkpoint evidence, and merge-queue readiness. | Merge, push, graph activation, release gates, task creation, or worktree cleanup. |
| `Dashboard/MCP read model` | Bounded operator views, blockers, recovery actions, queue status, graph epoch, rollback timeline. | Full backlog or graph expansion by default. |
| `Chain adapter` | Optional Chain identity fields and event mapping. | A separate parallel runtime. |

## Canonical Identity

Every durable runtime row or event that can survive restart must carry enough
identity to prevent cross-project, cross-branch, cross-batch, and stale-attempt
mutation.

Required identity fields:

| Field | Meaning |
| --- | --- |
| `project_id` | Governance project identity. |
| `batch_id` | Optional batch that groups related branches and merge/rollback lifecycle. |
| `backlog_id` | Backlog row that requested the work. |
| `task_id` | Governance task row when present. |
| `chain_id` | Optional Chain run identity. |
| `root_task_id` | Optional Chain root task identity. |
| `stage_task_id` | Optional Chain stage task identity. |
| `stage_type` | Optional Chain stage, such as `dev`, `test`, `qa`, or `merge`. |
| `agent_id` | Logical agent identity. |
| `worker_id` | Executor or external worker identity. |
| `attempt` | Monotonic attempt number for replay and stale callback rejection. |
| `lease_id` | Current claim lease. |
| `fence_token` | Token required for mutating callbacks. |
| `branch_ref` | Full branch ref, for example `refs/heads/codex/PB001-T3-task-runtime`. |
| `ref_name` | Target ref being merged into, normally `main`. |
| `worktree_id` | Durable worktree identity. |
| `worktree_path` | Local path, stored as runtime state and not as portable project identity. |
| `base_commit` | Target commit used when branch work began. |
| `head_commit` | Current branch head. |
| `target_head_commit` | Current target ref head at validation/merge time. |
| `snapshot_id` | Target structural graph snapshot, or a bounded branch candidate evidence pointer. |
| `projection_id` | Target semantic projection, or a branch candidate/proposal pointer that is not trusted graph truth. |
| `merge_queue_id` | Queue row identity. |
| `merge_preview_id` | Git/graph/test preview identity for a merge attempt. |
| `rollback_epoch` | Epoch used to isolate rollback and replay state. |
| `replay_epoch` | Epoch used when replaying retained branch heads. |

Rows may omit optional Chain fields only when Chain is not the client. The
schema must still reserve them.

## Event Envelope

Durable events should use this shape:

```json
{
  "event_id": "evt-...",
  "event_type": "branch_task.checkpointed",
  "project_id": "aming-claw",
  "batch_id": "batch-parallel-001",
  "backlog_id": "ARCH-...",
  "task_id": "task-...",
  "chain_id": "",
  "stage_task_id": "",
  "branch_ref": "refs/heads/codex/PB001-T3-task-runtime",
  "worktree_id": "wt-...",
  "attempt": 2,
  "lease_id": "lease-...",
  "fence_token": "fence-...",
  "base_commit": "B0",
  "head_commit": "B3",
  "snapshot_id": "snapshot-branch-B3",
  "projection_id": "semproj-branch-B3",
  "merge_queue_id": "mq-...",
  "rollback_epoch": "",
  "replay_epoch": "",
  "payload": {},
  "created_at": "2026-05-16T00:00:00Z",
  "actor": "agent-or-observer"
}
```

State rebuild after restart must derive from durable rows plus events, not from
in-memory worker state.

## BranchTaskRuntimeContext

State machine:

```text
allocated
  -> running
  -> checkpointed
  -> scope_ready
  -> review_ready
  -> merge_queued
  -> merged
  -> batch_retained
  -> cleaned
```

Exceptional states:

```text
running -> lease_expired -> reclaimable -> running
running -> failed -> replay_pending -> running
running -> abandoned
checkpointed -> base_stale -> replay_pending
review_ready -> dependency_blocked
merge_queued -> merge_blocked
merge_queued -> rollback_required
```

Rules:

- Only the current `fence_token` may checkpoint, complete, abandon, or request
  merge for an active attempt.
- A stale callback from an old agent must be recorded and ignored.
- If the target branch moves, a branch that was validated against the old
  target enters `base_stale` or `stale_after_dependency_merge`.
- Replay starts from the retained branch head plus the last durable checkpoint.
- Crash recovery scans expired leases and classifies tasks as `reclaimable`,
  `dependency_blocked`, `merge_failed`, or `rollback_required`.

## Branch Graph Policy

Parallel branch graph state is intentionally one-hop:

```text
target active graph @ base_commit
  -> branch delta/candidate evidence @ branch_head
```

The runtime must not create this shape:

```text
branch graph candidate
  -> next branch graph candidate
    -> next branch graph candidate
```

Rules:

- The active target graph/projection is the only graph truth.
- Branch/worktree graph artifacts are candidate evidence for merge gate,
  rollback, replay, and dashboard/MCP audit.
- Branch candidates are recomputed from the selected target commit graph and
  current branch head; stale candidates are replaced or pruned.
- If the target ref moves, the branch candidate becomes stale until rebased or
  recomputed against the new target graph.
- Only after an ordered merge lands on the target ref may target scope
  reconcile activate a new target graph snapshot/projection.

This keeps parallel development as target graph plus branch delta, not multiple
branch-local graph universes.

## Existing Long-Lived Branches

Existing projects may already have release branches, maintenance branches, or
large feature branches with many rounds of work. These are not modeled as
separate projects by default. Project identity remains the repository/workspace;
the long-lived branch gets a managed ref context under the same project.

```text
project_id = repo/workspace identity
  -> target ref context: refs/heads/main
  -> managed ref context: refs/heads/release/1.x
  -> managed ref context: refs/heads/feature/large-refactor
```

Rules:

- Import existing branches through managed-ref bootstrap dry-run first. The
  dry-run classifies refs as target, short-lived agent, managed, ignored,
  unmanaged, or blocked before anything is written.
- Applying a managed-ref bootstrap creates or refreshes `managed_ref_contexts`
  only. It must not create new project identities and must not activate a
  branch-local graph snapshot as target graph truth.
- Short-lived agent branches use one-hop candidate evidence.
- Existing long-lived refs may have ref-local current graph/projection pointers,
  but those pointers are scoped to the ref context and are not target graph
  truth.
- Merge from a managed ref to a target ref is code-first: freeze source/target
  heads and merge base, run preview/tests/gate, update the target ref, then run
  target-ref scope reconcile and activate the target snapshot.
- Do not merge structural graph truth across refs. Recompute target graph truth
  from the target HEAD after the code merge.
- After merge, mark the source ref context `merged` then `archived`; do not
  delete the project just because a source branch merged.
- Project deletion must be blocked while unresolved managed refs are still
  imported, tracked, stale, validating, merge-ready, merging, merged but not
  archived, or rollback-required.

The minimum durable state for a managed ref is source ref, target ref, merge
base, source head, target head, source snapshot/projection pointers, merge
preview, merge queue id, rollback epoch, archive policy, status, and evidence.

Managed-ref bootstrap records source/target heads, merge base, ahead/behind
counts, classification reason, and operator evidence. Refreshing an existing
managed ref marks the context stale and clears branch-local graph/preview
pointers so the next step must recompute against current target graph truth.

## MergeQueueRuntime

Dependency types:

| Type | Meaning |
| --- | --- |
| `hard_depends_on` | Upstream must merge successfully first. |
| `serializes_after` | Upstream should merge first to keep graph/runtime order deterministic. |
| `conflicts_with` | Branches touch mutually exclusive surfaces and require operator ordering. |
| `same_node_or_file_conflict` | Graph or file overlap detected. |
| `requires_graph_epoch` | Branch requires a specific graph/projection epoch before merge. |

Implemented runtime slice: `decide_merge_queue` carries compact typed blocker
evidence for all dependency types above. Failed, abandoned, stale,
rebase-required, running, validating, or merge-blocked upstream items move the
downstream item to `dependency_blocked`; unrelated branches remain mergeable
when they do not share blockers.

Implemented durability slice: `parallel_branch_merge_queue_items` persists
`MergeQueueItem` rows with typed dependencies, target refs, validation heads,
merge preview ids, and graph/projection ids. `decide_persisted_merge_queue`
replays PB-002/PB-003 queue decisions from SQLite after restart without
performing git merge side effects.

Implemented gate slice: `decide_merge_gate` and the governance merge-gate API
compose queue readiness with required evidence checks before any live merge.
The default plan is dry-run and cannot mutate the target ref. A future executor
slice may use the same plan with explicit operator approval, but dependencies,
stale target heads, rollback-required batches, and missing or failed evidence
must block target mutation.

Implemented result slice: the merge-result API records an externally performed
merge outcome into the durable queue and branch context. It accepts `merged` or
`merge_failed`, stores merge commit or failure evidence, updates target-head
evidence, and enforces the branch fence token when the context has one. It does
not run git merge or activate graph refs by itself.

Implemented preview slice: the merge-preview API produces `git_conflict_check`
evidence with read-only git commands. It resolves target and branch commits,
verifies the expected target head, runs `git merge-tree --write-tree`, and
returns clean/conflict/stale/error evidence without checkout, merge, commit,
reset, or ref mutation.

Implemented execution slice: the merge-execute API defaults to dry-run and
requires explicit `allow_target_ref_mutation=true` before it can checkout the
target branch and merge. It generates merge preview evidence, evaluates the
merge gate, checks for a clean worktree, performs the merge with Chain trailers,
and records `merged` or `merge_failed` back into durable queue/context state.

Queue states:

```text
waiting_dependency
dependency_blocked
queued_for_merge
validating
stale_after_dependency_merge
rebase_required
merge_blocked
merge_ready
merging
merged
merge_failed
abandoned
```

Merge gate inputs:

| Input | Required decision |
| --- | --- |
| Dependency status | All hard dependencies merged or explicitly waived. |
| Git conflict check | Merge preview must be clean or explicitly blocked. |
| Dirty worktree check | Candidate worktree must not contain unrelated dirty files. |
| Test evidence | Required tests from backlog or Chain stage pass. |
| Graph currentness | Branch one-hop candidate evidence is current for branch head and target base graph. |
| Scope reconcile | Scope result exists for the target-ref merge path or branch candidate fallback is explicitly labeled. |
| Semantic projection | Target projection is current, or branch semantic proposal state is stale/deferred candidate evidence. |
| Backlog acceptance | Acceptance criteria are satisfied or blocked with reason. |
| Batch rollback state | Batch must not be `rollback_required`. |
| Chain state | If Chain is client, stage state must permit merge. |

After any upstream merge, downstream queued branches must be revalidated. A
branch that was previously clean can become `stale_after_dependency_merge`.

## BatchMergeRuntime

Batch states:

```text
open
merge_in_progress
rollback_required
rollback_in_progress
replay_pending
replay_in_progress
accepted
abandoned
cleaned
```

Rules:

- Branches and worktrees remain retained until the batch is `accepted`,
  `abandoned`, or explicitly archived with rollback evidence.
- Cleanup is blocked while any branch is `merge_failed`,
  `dependency_blocked`, `rollback_required`, or `replay_pending`.
- A severe merge-order error moves the batch to `rollback_required`.
- Rollback records `rollback_epoch`, target rollback commit, abandoned merge
  epoch, affected graph refs, affected semantic projections, and replay order.
- Replay uses retained branch heads through the merge queue. It does not replay
  shell history.
- The first executable slice is the PB-004/PB-009 dry-run oracle in
  `agent/tests/test_batch_merge_rollback.py`: it produces rollback/replay
  decisions, retained branch/worktree evidence, compact dashboard rows, and
  replay `MergeQueueItem` entries without mutating git or the production DB.
- The first durability slice is the SQLite-backed batch/item store in
  `parallel_branch_batch_runtimes` and `parallel_branch_batch_items`. It
  persists retained branch/worktree evidence, abandoned merge commits, rollback
  graph/projection refs, replay epochs, and cleanup status so
  `decide_persisted_batch_rollback_replay` can replay PB-004/PB-009 decisions
  after restart without mutating git.

## Graph And Semantic Ref Rules

Target-ref facts:

- Active graph snapshot refs.
- Active semantic projection refs.
- Merge epochs.
- Rollback epochs.
- Replay epochs after acceptance.

Branch-local candidate facts:

- Branch graph artifact.
- Branch semantic projection.
- Scope reconcile result for branch head.
- Merge preview graph result.

Branch-local facts must not become active target-ref facts until the merge
queue accepts the branch and target ref is updated.

DB rollback requirements:

| Table area | Required rollback behavior |
| --- | --- |
| `graph_snapshot_refs` | Preserve ref activation history and point active target ref to rollback-compatible snapshot. |
| `graph_semantic_projections` | Deactivate or supersede abandoned epoch projections. |
| `graph_semantic_nodes` / `graph_semantic_edges` | Treat abandoned epoch rows as stale or inactive, not current. |
| `graph_semantic_jobs` | Cancel, requeue, or rebuild jobs by epoch and branch/ref. |
| `pending_scope_reconcile` | Key rows by project, branch/ref, batch, and epoch before parallel rollout. |
| `graph_events` | Record merge, rollback, abandoned epoch, replay, and hint inverse events. |
| Governance Hint state | Hint add/change/remove must be invertible under incremental reconcile. |

## PendingScopeRuntime

Current pending scope rows are keyed too narrowly for parallel branch work. The
parallel runtime requires keys that include:

```text
project_id
branch_ref or ref_name
commit_sha
batch_id
merge_queue_id
rollback_epoch
replay_epoch
```

Until those keys exist, true parallel rollout must treat scope materialization
as pending infrastructure. Full rebuild fallback is allowed only when labeled
as a fallback in test evidence and dashboard/MCP state.

## Dashboard And MCP Read Model

Default reads must stay compact. The operator should see:

- Batch lanes and branch lanes.
- Task state, owner, attempt, lease status, and stale-fence warnings.
- Dependency blockers and queue position.
- Base/head commits and target head at validation.
- Graph snapshot/projection state.
- Scope reconcile state.
- Merge gate blockers.
- Rollback epoch, retained branches, and replay order.
- Available actions: reclaim, rebase, revalidate, queue merge, block merge,
  abandon, rollback batch, replay batch, and cleanup when allowed.

Detailed payloads should be fetched by ID.

Implemented runtime slice: `build_parallel_branch_read_model` composes branch
runtime contexts, recovery decisions, merge queue decisions, and batch rollback
plans into a bounded payload for PB-010. It exposes branch lanes, compact queue
blockers, rollback epochs, graph epochs, action affordances, total counts, and
truncation flags without expanding backlog rows, graph nodes, or semantic
payloads. MCP and dashboard routes can consume this as the stable read-model
shape when their durable stores are wired.

Implemented API slice: `build_parallel_branch_read_model_from_db` composes the
same payload from durable branch context, merge queue, and batch runtime rows.
The governance endpoint `GET /api/graph-governance/{project_id}/parallel-branches`
exposes this compact read model for dashboard/MCP clients without expanding
full backlog rows or graph payloads.

## Chain Compatibility

Chain remains serial inside one Chain run. Parallel branch runtime allows many
Chain or MF clients to exist at once.

Rules:

- MF/observer-hotfix is the first client.
- Chain identity fields are optional but reserved from the first schema slice.
- Chain stages use the same branch allocation, checkpoint, merge queue, and
  rollback APIs.
- Chain events and branch runtime events must be replayable after restart.
- No API should require Chain to be running for MF clients.
- No API should assume all future clients are MF clients.

Implemented adapter slice: `build_parallel_branch_context_from_chain_payload`
maps Chain task payload metadata into `BranchTaskRuntimeContext` without
starting Chain execution. `parallel_branch_event_payload_from_context` emits a
compact branch runtime event envelope carrying Chain identity, retry/attempt,
branch/worktree, graph/projection, merge queue, and rollback/replay fields.
Serial Chain stages with no `branch_ref` remain unchanged.

## MVP Implementation Order

1. Scenario matrix and doc guards. Done by `ARCH-PARALLEL-AGENT-TEST-SCENARIO-MATRIX`.
2. This design contract and doc guard.
3. Branch/ref graph and semantic rollback contract.
4. Branch/ref-aware graph snapshot refs and semantic projection refs.
5. Durable `BranchTaskRuntimeContext`.
6. Durable `MergeQueueRuntime`. First SQLite-backed queue item store is
   implemented by `agent/tests/test_merge_queue_runtime.py`; live merge
   execution remains gated.
7. Durable `BatchMergeRuntime`. First SQLite-backed batch/item store is
   implemented by `agent/tests/test_batch_merge_rollback.py`; live rollback
   execution remains gated.
8. Branch/ref/batch-aware `pending_scope_reconcile`.
9. MF adapter MVP for one isolated branch through merge queue. First
   side-effect-free branch allocation planner is implemented by
   `agent/tests/test_parallel_branch_runtime.py`; actual git worktree execution
   remains delegated/gated.
10. Dashboard/MCP compact read model. First pure-state read model is implemented
   by `agent/tests/test_parallel_branch_read_model.py`; first governance API
   read model is implemented by `agent/tests/test_graph_governance_api.py`.
11. Chain adapter hook tests and later Chain integration. First no-execution
    adapter is implemented by `agent/tests/test_chain_parallel_branch_adapter.py`.
12. Managed ref runtime for existing long-lived branches. First SQLite-backed
    state/decision slice is implemented by `agent/tests/test_managed_ref_runtime.py`.
13. MF subagent backend contract. First backend-neutral payload/result contract
    is implemented by `agent/tests/test_mf_subagent_contract.py`; Codex
    subagents can act as the MVP worker backend, while executor and Chain can
    later implement the same contract.

The smallest runtime slice is one backlog row in one isolated worktree:

```text
allocate branch/worktree
-> run implementation
-> finish gate
-> validated checkpoint
-> branch-local scope/graph evidence
-> queue merge
-> merge gate
-> merge to target
-> target scope reconcile
-> retain then cleanup
```

Implemented allocation slice: `plan_branch_runtime_context` derives a
side-effect-free `BranchTaskRuntimeContext` with deterministic `branch_ref`,
`worktree_id`, and `worktree_path` from task, worker, batch, and attempt
identity. It sanitizes branch/worktree names and persists through the existing
branch runtime table; actual `git worktree` creation remains delegated to the
executor/MF client.

Implemented MF adapter API slice: governance exposes
`POST /api/graph-governance/{project_id}/parallel-branches/allocate` to persist
the planned runtime context and, when explicitly requested, materialize the
isolated git worktree under `.worktrees` through the existing branch graph
artifact initializer. `POST .../checkpoint` records replay checkpoints with
fence-token enforcement, and `POST .../recover-expired` rotates expired running
contexts into `reclaimable` after restart. These APIs create the execution
front door for MF/executor clients, while live merge and rollback execution
remain gated by the merge queue and graph checks.

Implemented MF subagent worker contract: `mf_sub` is a dedicated branch worker
role for Codex subagents in the MVP and executor/Chain workers later. Its
input payload carries `BranchTaskRuntimeContext` identity, including backlog,
branch, worktree, attempt, lease, fence, graph/projection, merge queue, and
optional Chain fields. Its result payload can only report changed files, test
evidence, blockers, checkpoint, and merge-queue readiness. It cannot merge,
push, activate graph refs, release gates, create tasks, delete worktrees, or
modify merge queues.

Implemented MF subagent finish gate: `POST
/api/graph-governance/{project_id}/parallel-branches/finish-gate` is the only
trusted exit for `mf_sub` worker claims. The worker fills a structured result,
but governance treats it as a claim, validates it against the current branch
runtime context, fence token, identity fields, and assigned worktree HEAD when
available. When the assigned worktree exists, governance also recomputes the
actual `base_commit..HEAD` changed-file set from that worktree and rejects
subagent `changed_files` claims that do not match. Natural-language cwd
instructions are not a runtime boundary; this diff validation is required
before recording a `mf_sub_finish_gate` checkpoint. Merge queue requests with
`worker_role=mf_sub` or `require_finish_gate=true` must reference that validated
checkpoint before they can enter the durable queue.

Implemented lightweight MF subagent capability gate: `mf_sub` is now a
first-class authenticated role, but it only has bounded worker capabilities.
An `mf_sub` session may call the finish gate and audited graph query endpoints
with `query_source=mf_subagent`, `parent_task_id`/`task_id`, and `fence_token`.
Observer/coordinator authority is still required for merge queue writes,
merge execution, graph reconcile/activation, backlog close, ServiceManager or
governance restarts, worktree cleanup, and other privileged state changes. This
is an MVP capability boundary, not enterprise RBAC.

Implemented fenced merge-queue API slice:
`POST /api/graph-governance/{project_id}/parallel-branches/merge-queue` lets a
branch runtime context enter the durable merge queue without direct client DB
writes. The route enforces the current branch fence token, requires a validated
finish-gate checkpoint for `mf_sub` worker entries, writes dependency and
graph/projection evidence into `parallel_branch_merge_queue_items`, updates the
branch context status and `merge_queue_id`, and returns the replayed merge
queue decision plan. It still performs no target-branch mutation.

Implemented batch rollback planning API slice:
`POST /api/graph-governance/{project_id}/parallel-branches/batch-runtime`
persists `BatchMergeRuntime` from branch runtime contexts and operator-supplied
merge evidence, then returns the deterministic rollback/replay plan. The route
can mark a batch `rollback_required`, retain branch/worktree evidence, expose
rollback/replay epochs and cleanup blockers, and keep git ref reset/replay
execution as an explicit later step.

Only after this loop is stable should normal executor worker parallelism be
raised above the current conservative mode.

## Pending Infrastructure

| Flag | Runtime blocker |
| --- | --- |
| I1 | Durable branch task runtime store. |
| I2 | Durable merge queue runtime store. |
| I3 | Branch/ref-aware graph snapshot refs. |
| I4 | Semantic projection rollback epochs. |
| I5 | Branch/ref/batch-aware pending scope keys. |
| I6 | Invertible Governance Hint deltas. |
| I7 | Compact dashboard/MCP parallel read model. |
| I8 | Chain adapter event identity. |
| I9 | Batch branch/worktree retention and cleanup policy. |
| I10 | Managed ref dashboard wiring for existing long-lived branches. |
| I11 | MF subagent backend contract and role profile. |

## Acceptance Bar

A runtime PR is not complete unless it answers:

- Which PB scenarios changed?
- Which identity fields are persisted?
- Which stale callback or restart path was tested?
- Which graph/semantic/pending-scope ownership rule changed?
- Which dashboard/MCP compact field exposes the new state?
- Which cleanup or rollback path prevents data loss?
