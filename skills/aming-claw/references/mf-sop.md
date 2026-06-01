<!-- governance-hint {"attach_to_node": {"path": "skills/aming-claw/references/mf-sop.md", "role": "doc", "target_area_key": "agent.governance", "target_node_id": "L3.13", "target_subsystem_key": "workflow_orchestration", "target_title": "Workflow Orchestration"}} -->

# Manual-Fix Checklist

Canonical source: `docs/governance/manual-fix-sop.md`. This file is only the short session checklist.

## Before Editing

1. Confirm the V1 implementation default: ordinary V1 implementation uses
   observer-led Manual Fix, with local Codex subagents as bounded `mf_sub`
   workers when parallel help is needed. Governance chain/executor
   dev/test/qa/merge execution is not the V1 default route; reserve it for
   explicit user requests to test chain automation or for documented
   experiments.
   - Observer/judge is a no-direct-code coordinator for governed nontrivial
     implementation work; it does not directly write implementation code.
   - If a local route/precheck provider is configured, resolve it through
     Aming-owned route/precheck contracts and record provider id, version, and
     hash evidence; source-controlled Aming skills must not name private
     provider systems or provider-specific tool names.
   - Route context must be consumed by machine gates, not merely shown in a
     prompt. For `observer_led_parallel_lanes` / `mf_parallel.v1` work, record
     timeline evidence for `route_context`, `route_action_precheck`,
     `bounded_implementation_worker_dispatch`, and `mf_subagent_startup` with
     matching `route_context_hash`, `prompt_contract_id`, and
     `prompt_contract_hash`.
   - Before local implementation writes for P0, cross-module, or parallel MF
     work, run `agent.governance.precheck_service.run_precheck` with
     `kind="route.pre_mutation"` or an equivalent `route.action_precheck`
     service gate. `preflight_check` output or advisory route prose is not
     authorization; machine route identity, allowed/blocked actions, required
     lanes/evidence, caller role, and visible injection manifest evidence must
     be present. Provider unavailable, transport closed, or stale route
     evidence must block as `blocked_route_context_unavailable`.
   - Dispatch nontrivial implementation to bounded `mf_sub`/worker lanes with
     target files, tests or a recorded no-test/E2E decision, worktree/fence
     evidence, and review evidence.
   - The only direct observer mutation exception is tiny deterministic scope:
     record the explicit reason, allowed files, exact dirty-scope match
     evidence, and timeline event before mutation.
2. Ensure a backlog row exists with target files, acceptance criteria, and details.
3. Predeclare/start the MF row with an MF id.
   - In MVP, API/storage may show `mf_type=chain_rescue` for observer-hotfix or
     manual-fix work. Treat it as the internal audited MF bucket, not as a sign
     that chain execution is required.
4. Capture baselines:
   - `git status`;
   - `version_check`;
   - `preflight_check`;
   - `graph_status`;
   - `graph_operations_queue`;
   - `wf_impact` for target files.
   Resolve any `plugin_update_state` blocker reported by `preflight_check`;
   missing state or `update_available` is a warning to record.
5. Run graph-first discovery and list reused nodes/modules in the working notes or final summary.
6. For new features or user-visible behavior changes, record the E2E impact decision:
   - run or add/update the relevant E2E and record evidence;
   - for dashboard/graph/bootstrap/file-hygiene paths, update the repo-owned fixture artifact first, materialize it into an isolated temp project, then run the E2E against that generated project;
   - for orphan file flows, put the orphan doc/test/config file in the fixture artifact, verify weak evidence first appears as an `asset_binding_proposal`, then let the E2E write the source-controlled governance hint, commit the fixture change, run Update graph, and assert the binding;
   - file a follow-up backlog row when live-AI, DB-mutating, slow, or human-approval E2E is deferred;
   - write `e2e_not_applicable` with a reason for docs-only or non-runtime changes.
7. For nontrivial architecture, frontend/UI, or QA-sensitive work, resolve the
   matching source-controlled review pack before implementation or before close:
   - architecture/data continuity: preserve API, persistence, state, migration,
     retry, and acceptance traceability;
   - frontend/UI implementation: require component convention, responsive,
     state, accessibility, and screenshot evidence;
   - QA evidence gate: require focused test, fixture, contract validation, E2E
     run/defer, and close-gate evidence;
   - validate structured review output with the review pack validator before
     converting accepted findings into acceptance criteria or follow-up backlog.
8. For parallel MF or subagent work, instantiate a source-controlled contract template before delegation:
   - start from `agent/governance/contract_templates/mf_parallel.v1.json`;
   - when a deterministic MF workflow worker is used, instantiate
     `agent/governance/contract_templates/mf_workflow_runtime.v1.json` and run
     each privileged stage through
     `agent.governance.precheck_service.run_precheck(kind, contract_id, stage,
     subject, actor)`;
   - run `route.pre_mutation` before local implementation writes for high-risk,
     P0, cross-module, `mf_parallel.v1`, or `observer_led_parallel_lanes`
     implementation actions;
   - use the workflow runtime stage graph
     `dispatch -> startup_gate -> implementation_wait -> handoff_gate -> merge_gate ->
     merge_queue_entry -> merge_preview -> live_merge -> reconcile ->
     close_gate -> done`, with `observer_review` for yellow-lane results and
     `blocked` for red-lane results;
   - require every precheck result to carry `precheck_run_id`, `kind`,
     `contract_id`, `stage`, `decision`, `status`, `subject`, `evidence`,
     `evidence_hash`, and `created_at`; merge/reconcile/close gates must verify
     the referenced token still matches subject commit/fence evidence;
   - write the instance to `chain_trigger_json.parallel_contract`;
   - treat subagents as local Codex workers governed by the MF backlog row,
     contract, file/worktree fence, and timeline evidence; do not use
     governance `task_create` dev/test/qa/merge as the default implementation
     entrypoint;
   - use observer-only coordination by default: the observer clarifies scope,
     checks runtime/graph/backlog state, creates the contract, may start agents
     only when the user explicitly asks or an approved contract calls for it,
     and reviews candidates; the observer does
     not implement, wait, merge, push, release gates, activate graph refs, close
     backlog, delete worktrees, or mutate merge queues unless the user
     explicitly asks or a documented governance transition requires it;
   - fill each `mf_sub` worker's runtime identity from task metadata before dispatch:
     `task_id`, `parent_task_id`, `worker_role=mf_sub`, `branch_ref`,
     `worktree_path`, `base_commit`, `target_head_commit`, `merge_queue_id`,
     and `fence_token`;
   - assign every worker a branch/worktree/file fence before dispatch, then
     require it to stay inside that fence and stop at `review_ready` or
     `waiting_merge`, never merge/push or mutate merge queues;
   - require subagent graph lookups to use audited
     `query_source=mf_subagent`, with `task_id`, `parent_task_id`,
     `worker_role`, and `fence_token` in the query context;
   - before `spawn_agent`, run and record
     `agent.governance.mf_subagent_contract.validate_mf_subagent_dispatch_gate`
     for each local `mf_sub` worker; the gate must pass with an isolated
     branch/worktree/file fence, `base_commit`, `target_head_commit`,
     `merge_queue_id`, `fence_token`, owned files, current target graph
     evidence, and dirty-scope evidence before non-blocking dispatch;
   - block dispatch when the target/main HEAD moved after contract creation or
     when the active target graph is stale. Existing branch/worktree adoption is
     allowed only as a first-class recovery path with explicit adoption
     evidence in the contract/timeline;
   - block target/main worktree dispatch by default. A same-worktree exception
     requires `same_worktree_allowed=true`, an explicit operator reason, exact
     dirty-scope evidence, and observer timeline evidence before dispatch;
   - after the local `mf_sub` worker starts and before it edits files, require
     `mf_subagent.startup` through the unified precheck service with
     `actual_git_root` or `actual_cwd`, `actual_fence_token`, branch, HEAD,
     target/main HEAD, and owned files. Block and stop the worker if actual
     runtime root is target/main, differs from the assigned worker worktree,
     carries the wrong branch/HEAD/fence token, or target/main became dirty,
     especially when dirty files overlap owned files;
   - require subagent implementation or verification timeline evidence to
     include returned graph trace ids in `payload.graph_trace_ids`,
     `payload.graph_query_trace_ids`, `verification.graph_trace_ids`, or
     `verification.graph_query_trace_ids`;
   - align self-precheck and gate expectations before dispatch: required
     evidence ids, focused test commands, E2E decision or defer row, finish-gate
     fence expectations, and the compact `self_check` evidence the subagent must
     report;
   - record the observer-configured test scenario policy before delegation:
     MF/subagent work is not universally test-first; the observer must choose
     `none`, `reuse_existing`, or `new_scenario_required`, give the reason,
     list required evidence ids, and record the E2E run/defer/not_applicable
     decision with a follow-up backlog id when deferred;
   - when the decision is `new_scenario_required`, name the fixture path and
     scenario ids in the contract and require fixture-backed tests before or
     with implementation. For `MF-WORKFLOW-PRECHECK-SERVICE-20260525`, the
     decision is `new_scenario_required`; the fixture
     `agent/tests/fixtures/mf_workflow_runtime.py` is required and must create
     isolated temporary git repositories/worktrees without mutating the live
     repo;
   - require structured worker final output with status, branch/worktree, owned
     changed files, tests run, graph query trace ids, precheck evidence,
     generated assets policy, and risks/open questions;
   - after a dispatch gate passes, stop at non-blocking dispatch unless the
     user explicitly asks the observer to wait, review, merge, close, or take
     another privileged action;
   - give every required evidence item a stable `id`;
   - require timeline evidence to reference ids through `payload.requirement_id(s)`,
     `verification.requirement_id(s)`, or `verification.contract_evidence[].requirement_id`;
   - make E2E evidence required for dashboard/API/operator-path changes unless explicitly deferred with a follow-up backlog row;
   - before accepting a merge candidate, check contract fit, diff scope,
     focused test/E2E evidence, docs/test/config impact, generated assets
     policy, graph/reconcile plan, Chain trailers, and backlog close policy;
   - when changed docs/templates are not graph-bound, record Asset Inbox
     binding or Governance Hint follow-up as needed for auditability.
9. If an AI session or `mf_sub` worker proposes doc/test/config binding changes, require the local asset-binding precheck first:
   - run `agent.governance.asset_binding_proposals.precheck_asset_binding_proposal` against the draft proposal;
   - include compact `self_precheck` evidence with the submitted proposal;
   - do not request direct graph materialization from weak evidence.
10. Treat documentation as a commit-bound asset before impact scope:
   - weak doc path matches stay as doc asset state `candidate` rows;
   - only accepted bindings from review decisions, source-controlled hints, or durable rules count as node-owned docs;
   - when changing doc binding behavior, verify `doc-asset-state.json` shows path/hash/status/proposal evidence.
   - governance hints should prefer stable target evidence such as
     `target_module`, or `target_area_key` + `target_subsystem_key` +
     `target_title`; title-only hints are repair candidates when the title is
     ambiguous. Reset/repair hints by editing the source hint, committing it,
     and running Update Graph/reconcile.
11. Keep asset binding and drift on separate audit lines:
   - binding relationships are source-controlled append-only commands,
     normally governance-hint bind/unbind events, then reconcile materializes
     graph secondary/test/config fields, file inventory effective state,
     asset projection, and binding events;
   - file/hash/drift/impact state is observed DB evidence written by reconcile,
     gate, or workflow worker from git diff plus accepted bindings;
   - changed bound assets covered by contract/gate may be recorded as
     `not_drifted` with gate evidence;
   - unchanged bound assets impacted by related source/config changes become
     `suspected`/`impact_pending` until observer, user, or AI-assisted review
     resolves them;
   - do not directly hand-write trusted accepted binding rows into DB as a
     substitute for source-controlled binding evidence.
12. For observer/MF work, append timeline evidence as work proceeds:
   - `task_timeline_append` with `event_kind=implementation` after scoped code,
     docs, config, or fixture changes are made;
   - `task_timeline_append` with `event_kind=verification` after focused tests,
     review checks, or documented no-test decisions;
   - `task_timeline_append` with `event_kind=close_ready` after commit,
     redeploy/reconcile/version checks are complete;
   - run `mf_timeline_precheck` before `backlog_close`; for route-parallel work,
     the gate also checks route-context consumption and bounded worker
     dispatch/startup evidence tied to the same route identity.
   - when using MCP `backlog_close`, pass the route-token gate evidence:
     either `route_token` with route context / prompt contract / scope /
     expiry / evidence refs, or an explicit `route_waiver` with reason and
     timeline evidence.

## Commit

Stage explicit files only. Use Chain trailers as MF audit anchors. Chain
trailers do not mean auto-chain execution is active:

```text
Chain-Source-Stage: observer-hotfix
Chain-Project: aming-claw
Chain-Bug-Id: <backlog-id>
```

Use `[observer-hotfix]` or `manual fix:` in the subject when this is a true MF bypass.

## After Commit

1. Restart/redeploy changed runtime services when needed.
2. Run `version_check`; require `ok=true`, `dirty=false`, and runtime matching HEAD for runtime changes.
3. Run MCP `preflight_check`; require no `plugin_update_state` blockers. As
   supplemental local evidence from the repo checkout, run
   `python -m agent.cli mf precommit-check --json-output`. Do not assume a
   stale installed `aming-claw` shell command has the same subcommands until
   plugin/CLI update aftercare has run.
4. Check graph status. If HEAD is ahead of the active graph, run direct Update graph/scope reconcile before telling a dashboard user the graph is current. Explicit pending-scope queueing is legacy/debug only.
5. Rebuild or refresh semantic projection when dashboard semantic state changed.
6. Confirm the E2E impact decision is current, deferred with a backlog row, or explicitly not applicable.
7. Append `close_ready` timeline evidence and run `mf_timeline_precheck`.
8. Close the backlog row with the commit hash and verification evidence.
