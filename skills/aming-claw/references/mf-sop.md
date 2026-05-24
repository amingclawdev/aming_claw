<!-- governance-hint {"attach_to_node": {"path": "skills/aming-claw/references/mf-sop.md", "role": "doc", "target_node_id": "L7.159"}} -->

# Manual-Fix Checklist

Canonical source: `docs/governance/manual-fix-sop.md`. This file is only the short session checklist.

## Before Editing

1. Confirm chain/MF route is justified. Routine feature work should use the normal chain when possible.
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
7. For parallel MF or subagent work, instantiate a source-controlled contract template before delegation:
   - start from `agent/governance/contract_templates/mf_parallel.v1.json`;
   - write the instance to `chain_trigger_json.parallel_contract`;
   - give every required evidence item a stable `id`;
   - require timeline evidence to reference ids through `payload.requirement_id(s)`,
     `verification.requirement_id(s)`, or `verification.contract_evidence[].requirement_id`;
   - make E2E evidence required for dashboard/API/operator-path changes unless explicitly deferred with a follow-up backlog row.
8. If an AI session or `mf_sub` worker proposes doc/test/config binding changes, require the local asset-binding precheck first:
   - run `agent.governance.asset_binding_proposals.precheck_asset_binding_proposal` against the draft proposal;
   - include compact `self_precheck` evidence with the submitted proposal;
   - do not request direct graph materialization from weak evidence.
9. Treat documentation as a commit-bound asset before impact scope:
   - weak doc path matches stay as doc asset state `candidate` rows;
   - only accepted bindings from review decisions, source-controlled hints, or durable rules count as node-owned docs;
   - when changing doc binding behavior, verify `doc-asset-state.json` shows path/hash/status/proposal evidence.
10. For observer/MF work, append timeline evidence as work proceeds:
   - `task_timeline_append` with `event_kind=implementation` after scoped code,
     docs, config, or fixture changes are made;
   - `task_timeline_append` with `event_kind=verification` after focused tests,
     review checks, or documented no-test decisions;
   - `task_timeline_append` with `event_kind=close_ready` after commit,
     redeploy/reconcile/version checks are complete;
   - run `mf_timeline_precheck` before `backlog_close`.

## Commit

Stage explicit files only. Use Chain trailers:

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
