<!-- governance-hint {"attach_to_node": {"path": "docs/governance/manual-fix-sop.md", "role": "doc", "target_area_key": "agent.governance", "target_node_id": "L3.13", "target_subsystem_key": "workflow_orchestration", "target_title": "Workflow Orchestration"}} -->

# Manual Fix SOP (Standard Operating Procedure)

> Status: DRAFT v7 - trailer-priority chain anchor + graph-first/E2E impact gate
> Author: Observer
> Date: 2026-04-05
> Scope: Enforceable operating procedure for AI agents performing manual fixes

---

## 1. When Manual Fix Is Required

Manual fixes are **only** permitted in chicken-and-egg deadlock scenarios where the normal Workflow (PM->Dev->Test->QA->Merge) cannot operate:

| Scenario | Why Workflow Cannot Run | Manual Fix Scope |
|----------|------------------------|------------------|
| Dirty workspace blocks all chains | auto_chain gate rejects every dispatch | Commit accumulated code |
| Fixing auto_chain itself | The chain engine is the thing being repaired | Modify auto_chain.py |
| Fixing auto_chain itself — conn contention | conn.commit() not called before synchronous _publish_event in auto_chain.py; legacy subscribers hit 60s busy_timeout stall | Scope C / High — modify auto_chain.py commit-before-publish pattern |
| Fixing executor CLI | Dev stage requires executor to run | Modify executor code |
| Governance service won't start | No service = no API = no tasks | Fix server.py startup |

**Bootstrap is NOT a manual fix.** It has its own dedicated flow (`bootstrap_project()`) with separate preconditions and verification. Do not use this SOP for first-time initialization — use the Bootstrap Flow documented in `reconcile-flow-design.md` section 11.1.

**Principle: Manual fixes must be minimal in scope. The sole goal is to restore Workflow operation. All subsequent fixes must return to the normal Workflow chain.**

> **See also:** [reconcile-workflow.md](reconcile-workflow.md) — Reconcile replaces ad-hoc manual
> fixes with a structured, auditable process. Rollback paths in §7 and §9 reference this SOP.

---

## 2. Manual Fix Flow (6 Phases)

```
┌──────────────────────────────────────────────────────────────────┐
│                       MANUAL FIX FLOW                            │
│                                                                  │
│  Phase 0: ASSESS (read-only, no changes)                         │
│  ┌────────────────────────────────────────────────┐              │
│  │ 0.1  git status                                │              │
│  │      -> identify dirty files                   │              │
│  │ 0.2  wf_impact(changed_files)                  │              │
│  │      -> count affected nodes, verify_level,    │              │
│  │         gate_mode                              │              │
│  │ 0.3  preflight_check                           │              │
│  │      -> capture system integrity baseline      │              │
│  │ 0.4  version_check                             │              │
│  │      -> HEAD vs chain_version vs dirty state   │              │
│  └──────────────────────┬─────────────────────────┘              │
│                         v                                        │
│  Phase 1: CLASSIFY (dual-axis risk assessment)                   │
│  ┌────────────────────────────────────────────────┐              │
│  │ Axis 1 — Affected node count:                  │              │
│  │   0 nodes    -> Scope A                        │              │
│  │   1-5 nodes  -> Scope B                        │              │
│  │   6-20 nodes -> Scope C                        │              │
│  │   >20 nodes  -> Scope D                        │              │
│  │                                                │              │
│  │ Axis 2 — Change danger level:                  │              │
│  │   Low:  new files, new routes, docs, tests     │              │
│  │   Med:  modify normal business logic           │              │
│  │   High: delete/rename, modify executor /       │              │
│  │         auto_chain / governance / version gate  │              │
│  │                                                │              │
│  │ Final level = max(Axis 1, Axis 2)              │              │
│  │ (see S3 matrix for combined rules)             │              │
│  └──────────────────────┬─────────────────────────┘              │
│                         v                                        │
│  Phase 2: PRE-COMMIT VERIFY                                      │
│  ┌────────────────────────────────────────────────┐              │
│  │ 2.1  Execute checks per combined level          │              │
│  │      (see S3 matrix)                           │              │
│  │ 2.2  Record: which nodes affected, which are    │              │
│  │      false positives (with evidence per S4)    │              │
│  │ 2.3  If verify_requires dependencies exist:    │              │
│  │      confirm upstream nodes are verified        │              │
│  │ 2.4  MANDATORY RULES (cannot be skipped):      │              │
│  │      - delete/rename -> reconcile(dry_run) (R2) │              │
│  │      - Scope D -> must split or dry_run (R1)    │              │
│  │      - explicit+v4 real impact -> auto-generate │              │
│  │        verification task after commit (R3)      │              │
│  │      - new files -> check/create nodes (R6)     │              │
│  │      - create execution record (R7)             │              │
│  │      - verify doc locations (R10)               │              │
│  │      - check coverage for committed files (R9)  │              │
│  │      - check E2E impact for new features (R14)  │              │
│  └──────────────────────┬─────────────────────────┘              │
│                         v                                        │
│  Phase 3: COMMIT                                                 │
│  ┌────────────────────────────────────────────────┐              │
│  │ 3.1  git add <specific files>                  │              │
│  │      (NEVER git add -A; add files explicitly)  │              │
│  │ 3.2  git commit -m "manual fix: <reason>"      │              │
│  │      Commit message MUST include:              │              │
│  │      - "manual fix:" prefix (for audit trail)  │              │
│  │      - affected node list (real vs false pos.)  │              │
│  │      - bypass reason                           │              │
│  │ 3.3  Do NOT push yet (wait for Phase 5 pass)   │              │
│  └──────────────────────┬─────────────────────────┘              │
│                         v                                        │
│  Phase 4: POST-COMMIT VERIFY (re-run after EVERY commit, R8)     │
│  ┌────────────────────────────────────────────────┐              │
│  │ 4.1  Restart governance service                │              │
│  │      (SERVER_VERSION must read new HEAD)        │              │
│  │ 4.2  version_check -> confirm ok=true,          │              │
│  │      dirty=false                               │              │
│  │ 4.3  preflight_check -> compare against         │              │
│  │      Phase 0 baseline                          │              │
│  │      New blockers = regressions -> ABORT        │              │
│  │ 4.4  wf_impact recheck -> confirm affected      │              │
│  │      nodes unchanged                           │              │
│  │ 4.5  If gate_mode=explicit nodes truly affected:│              │
│  │      -> auto-create verification task (MANDATORY)│             │
│  │ 4.6  If this commit generated additional files  │              │
│  │      (audit record, execution record, nodes):  │              │
│  │      -> commit them, then LOOP back to 4.1 (R8)│              │
│  └──────────────────────┬─────────────────────────┘              │
│                         v                                        │
│  Phase 5: WORKFLOW RESTORE PROOF                                 │
│  ┌────────────────────────────────────────────────┐              │
│  │ 5.1  Create a minimal test task via task_create │              │
│  │ 5.2  Observe: does it enter the chain?          │              │
│  │      (status transitions: queued -> claimed)   │              │
│  │ 5.3  Observe: does auto_chain dispatch the      │              │
│  │      next stage after completion?              │              │
│  │      (check task_list for follow-up task)      │              │
│  │ 5.4  Record result: RESTORED or STILL_BROKEN    │              │
│  │      If STILL_BROKEN -> diagnose, do NOT push  │              │
│  │ 5.5  Disable observer_mode if temporarily       │              │
│  │      enabled                                   │              │
│  │ 5.6  Write structured audit record (see S6)     │              │
│  └────────────────────────────────────────────────┘              │
└──────────────────────────────────────────────────────────────────┘
```

---

## 3. Dual-Axis Risk Matrix

### Axis 1: Scope (affected node count)

| Scope | Node Count | Meaning |
|-------|-----------|---------|
| A | 0 | No governance nodes affected |
| B | 1-5 | Small, targeted impact |
| C | 6-20 | Broad impact, may include false positives |
| D | >20 | Very broad, almost certainly needs splitting |

### Axis 2: Danger (change type)

| Danger | Change Types | Examples |
|--------|-------------|----------|
| Low | New files, new routes, docs, tests | Adding reconcile.py, adding .md |
| Medium | Modify existing business logic | Changing a handler's response format |
| High | Delete/rename, modify infrastructure | Changing auto_chain.py, executor, version gate, server startup |

### Combined Rules Matrix

```
                    Danger
                    Low           Medium          High
Scope  ┌────────────────┬────────────────┬────────────────┐
  A    │ Commit directly│ Commit directly│ Run related    │
  (0)  │ No extra checks│ No extra checks│ tests first    │
       ├────────────────┼────────────────┼────────────────┤
  B    │ Run module     │ Run module     │ Run full suite │
  (1-5)│ tests          │ tests + verify │ + verify each  │
       │                │ explicit nodes │ node manually  │
       ├────────────────┼────────────────┼────────────────┤
  C    │ Run full suite │ Run full suite │ Run full suite │
  (6-20)│ + record false│ + verify each  │ + MANDATORY    │
       │ positives      │ real node      │ split or       │
       │                │                │ dry_run first  │
       ├────────────────┼────────────────┼────────────────┤
  D    │ MUST split     │ MUST split     │ MUST split     │
  (>20)│ or dry_run     │ + full verify  │ + full verify  │
       │ reconcile      │ after each     │ + observer     │
       │                │ sub-commit     │ approval       │
       └────────────────┴────────────────┴────────────────┘
```

### Mandatory Hard Rules (cannot be overridden)

These rules are **not guidelines**. Violation constitutes a governance breach:

| # | Rule | Trigger | Required Action |
|---|------|---------|-----------------|
| R1 | No direct commit at Scope D | >20 affected nodes | MUST split into sub-commits or run reconcile(dry_run=true) first |
| R2 | Dry-run before delete/rename | Any file deletion or rename in diff | MUST run reconcile_project(dry_run=true) to check for broken refs |
| R3 | Auto-generate verification task | Commit affects gate_mode=explicit + verify_level=4 node (real, not false positive) | MUST create task_create(type=test) targeting that node after commit |
| R4 | Structured audit record | Every manual fix | MUST produce a structured record per template in S6 |
| R5 | Workflow restore proof | Every manual fix | MUST demonstrate auto_chain dispatch works before pushing |
| R6 | New-file node check | Any new file in commit | MUST check if governance node exists for the new file; if not, create node in graph.json before or during commit |
| R7 | Execution record required | Every manual fix | MUST create an external execution record (docs/dev/manual-fix-current-YYYY-MM-DD[-NNN].md) at Phase 0 start, filled incrementally |
| R8 | Multi-commit restart loop | Phase 4 generates additional files (audit record, execution record, node updates) | MUST re-run Phase 4 (restart governance + version_check + preflight delta) after EVERY subsequent commit. Do NOT push until final restart confirms ok=true |
| R9 | Coverage warnings actionable | preflight_check reports unmapped files that are in the current commit | MUST either create nodes for unmapped committed files or document why they are intentionally unmapped |
| R10 | Doc location check | Any new documentation file | MUST verify file is placed in the correct directory per project convention (e.g., governance docs in docs/governance/, dev docs in docs/dev/). Misplaced docs must be moved before commit |
| R11 | chain anchor via trailer (trailer-priority) | Every manual fix commit | MUST author the MF commit with a `Chain-Source-Stage:` git trailer (e.g. `Chain-Source-Stage: observer-hotfix`) so it is recognized as the latest chain anchor. `handle_version_check` in `agent/governance/server.py` now uses `agent/governance/chain_trailer.py get_chain_state()` as the primary source of `chain_version` — the legacy `POST /api/version-update/{project_id}` DB-write path is **deprecated** in the trailer era and the API returns `{"deprecated_write_ignored": true, "source": "git_trailer"}` (the DB `project_version.chain_version` row is no longer authoritative). Without a trailer on HEAD, `get_chain_state()` walks first-parent back to the previous trailered commit and `HEAD != CHAIN_VERSION` blocks all dispatch through the version gate. Verify with `GET /api/version-check/{project_id}` returns `ok: true`, `dirty: false`, and `chain_version == HEAD`. Working reference: MF commit `0d4329d` (a trailered observer-hotfix that became the chain anchor). The deprecated `POST /api/version-sync` + `POST /api/version-update` flow MUST NOT be used as the primary mechanism — calling it is a DB-write no-op against chain state. |
| R12 | Per-project chain history cache | Every manual fix commit | After commit, the per-project chain history cache at `agent/governance/chain_history/{project_id}.json` will be updated on next governance startup or bootstrap. Manual fixes that add commits without trailers will be detected as `legacy_inferred` entries in the cache. No manual action needed — the cache updates incrementally. |
| R13 | Graph-first reuse check | Every AI-authored manual fix or implementation task | MUST inspect the active graph or graph snapshot for target files, nearby modules, existing nodes, and reusable subsystems before creating new modules or abstractions. The execution record must list reused graph nodes/modules or state that the graph was unavailable and why. |
| R14 | E2E impact gate | New feature, user-visible behavior change, dashboard operator path, graph/reconcile/bootstrap/project-config behavior, semantic job/review/cancel/backlog behavior, or any change to a feature already covered by an E2E suite | MUST record an E2E impact decision before close: run or add/update the relevant E2E and record evidence, or file a follow-up backlog row when the E2E is deferred. Live-AI, DB-mutating, or human-approval E2E may be deferred, but only with an explicit backlog row and reason. |
| R15 | Plugin update state gate | Every manual fix before commit and before close | MUST run MCP `preflight_check` and resolve any `plugin_update_state` blocker. `python -m agent.cli mf precommit-check --json-output` can be used as supplemental local evidence from the repo checkout; do not assume a stale installed `aming-claw` shell command has the same subcommands until plugin/CLI update aftercare has run. `update_available` and missing state are warnings that must be recorded, not blockers. `applied_pending_restart`, failed update state, or unsatisfied MCP/governance/ServiceManager restart obligations MUST be resolved before closing the MF row. |
| R16 | Asset binding proposal-first | Any doc/test/config binding suggestion produced by an observer, AI session, or `mf_sub` worker | Weak evidence such as path mentions, import-only references, semantic summaries, or downgraded weak test fan-in MUST be submitted as an `asset_binding_proposal` with `self_precheck`, not written as trusted graph state. Only source-controlled governance hints, accepted review decisions, direct test symbol imports, or registered config loader/rule evidence may materialize during reconcile. |
| R17 | Doc asset impact boundary | Any documentation binding or review-impact claim | Documentation files are commit-bound assets first. Reconcile MUST preserve doc hash/state in doc asset state; weak matches remain `candidate`; review impact MUST consume accepted doc bindings only. Use AI/observer proposal review or source-controlled hints before treating a doc as node-owned. |
| R18 | Observer MF timeline gate | Every observer/manual-fix backlog close | MUST append task timeline rows for `event_kind=implementation`, `event_kind=verification`, and `event_kind=close_ready` against the same `backlog_id` before calling backlog close. Governance enforces this in `handle_backlog_close`; missing rows return `mf_timeline_gate_failed`. Emergency bypass requires `bypass_timeline_gate=true` plus a non-empty `timeline_bypass_reason`, and the bypass is itself written to the task timeline. |
| R19 | Instantiated contract gate | Parallel MF work, subagent work, dashboard/API/user-visible behavior, or any task with explicit evidence requirements | Observer MUST instantiate a source-controlled contract template such as `agent/governance/contract_templates/mf_parallel.v1.json` into `chain_trigger_json.parallel_contract` before delegation. Required evidence items MUST have stable `id` values. For `mf_sub` workers, the instance MUST include task metadata `task_id`, `parent_task_id`, `worker_role=mf_sub`, and `fence_token`; graph lookups MUST use audited `query_source=mf_subagent` with the same identity context, and timeline evidence MUST record returned graph trace ids through `payload.graph_trace_ids`, `payload.graph_query_trace_ids`, `verification.graph_trace_ids`, `verification.graph_query_trace_ids`, or `verification.contract_evidence[].graph_trace_ids`. Timeline evidence MUST reference requirement ids through `payload.requirement_id(s)`, `verification.requirement_id(s)`, or `verification.contract_evidence[].requirement_id`. `handle_backlog_close` blocks MF close until every required contract evidence id is present with a passing status. |

#### R19.1 Local `mf_sub` Dispatch Gate

Before `spawn_agent`, the observer MUST run and record
`agent.governance.mf_subagent_contract.validate_mf_subagent_dispatch_gate` for
each local `mf_sub` worker. The gate must pass before non-blocking dispatch and
must prove an isolated branch/worktree/file fence, `base_commit`,
`target_head_commit`, `fence_token`, owned files, and dirty-scope evidence.

Dispatch into the target/main worktree is blocked by default. If a
same-worktree exception is ever used, it MUST include
`same_worktree_allowed=true`, an explicit operator reason, exact dirty-scope
evidence, and observer timeline evidence before dispatch. The observer must
also run audited graph lookups with `query_source=mf_subagent` and the task
identity/fence context, record returned trace ids in implementation or
verification timeline evidence, then stop after dispatch unless the user
explicitly asks to wait, review, merge, close, or perform another privileged
action.

#### R19.2 Contract-driven MF Workflow Runtime and Unified Precheck Gates

MF workflow workers may drive the deterministic stage graph only when the
backlog row instantiates a contract from
`agent/governance/contract_templates/mf_workflow_runtime.v1.json`. The runtime
stage order is:

```text
dispatch -> implementation_wait -> handoff_gate -> merge_gate -> reconcile -> close_gate -> done
```

`observer_review` and `blocked` are explicit branch targets. Green-lane
prechecks may advance to the next stage. Yellow-lane results go to
`observer_review`. Red-lane results go to `blocked`.

The authoritative local gate is
`agent.governance.precheck_service.run_precheck(kind, contract_id, stage,
subject, actor)`. Workflow workers MUST call this service for registered gate
kinds instead of duplicating policy logic:

- `mf_subagent.dispatch`
- `mf_subagent.handoff`
- `workflow.merge`
- `workflow.reconcile_policy`
- `backlog.close`

Every precheck result MUST include `precheck_run_id`, `kind`, `contract_id`,
`stage`, `decision`, `status`, `subject`, `evidence`, `evidence_hash`, and
`created_at`. Merge, reconcile, and close gates MUST verify that the referenced
precheck token still matches the subject commit/fence evidence.

The observer owns the optional test-scenario decision before delegation:
`none`, `reuse_existing`, or `new_scenario_required`. When the observer chooses
`new_scenario_required`, the contract MUST name the fixture path and scenario
ids, and the worker must add or update fixture-backed tests before or with the
implementation. For backlog `MF-WORKFLOW-PRECHECK-SERVICE-20260525`, the
decision is `new_scenario_required`; `agent/tests/fixtures/mf_workflow_runtime.py`
is required and must create isolated temporary git repositories/worktrees
without mutating the live repo. E2E remains `e2e_not_applicable` while the
change stays in local Python service/runtime modules, contract template,
fixture tests, and SOP docs. If server, dashboard, MCP API, or operator runtime
behavior is needed, the worker must stop with `needs_revision`.

---

### R13 Graph-First Reuse Checklist

Before editing code, the agent must use the graph as the first map of the system:

1. Query or inspect the active graph for target files and nearby modules.
2. Identify existing L7 implementation nodes, L3/L2 parent areas, and relevant L4 assets/contracts.
3. Check whether an existing module already implements the needed abstraction.
4. Reuse or extend the existing module when it fits the request.
5. Only create a new module after recording why no existing node/module is a good owner.

This rule exists because the graph can reveal reusable work that text search alone may miss. If the graph says a subsystem already exists, the default action is to inspect and reuse it, not rebuild it.

---

### R14 E2E Impact Checklist

Every new feature or externally visible behavior change must answer these questions
before the backlog row is closed:

1. Does the change affect a dashboard user path or API contract?
2. Does it affect graph build, scope/full reconcile, bootstrap, project config, or
   code-search/query behavior?
3. Does it affect semantic jobs, queueing, cancellation, review accept/reject,
   backlog filing, or evidence projection?
4. Is there an existing E2E suite in `.aming-claw.yaml` whose trigger paths,
   nodes, or tags cover this change?
5. If yes, did the suite run and write current evidence, or is there a recorded
   stale/missing reason?
6. If no, should a new E2E suite or scenario be added now?

The allowed outcomes are:

- `e2e_current`: relevant E2E passed and evidence was recorded for the current
  commit/snapshot.
- `e2e_added`: a new or updated E2E scenario was committed and evidence was
  recorded.
- `e2e_deferred`: a backlog row was filed with scope, reason, and acceptance
  criteria. This is acceptable for live-AI, DB-mutating, slow, or human-approval
  suites, but it must be explicit.
- `e2e_not_applicable`: the change is docs-only or otherwise outside runtime and
  operator behavior; record the reason in the MF notes or final summary.

When the E2E impact planner exists for the project, use it as the source of truth
for `current`, `stale`, `missing`, and `blocked` suite status. Chain integration
is tracked separately in backlog rows and is not required for manual execution of
this rule. For parallel/MF work, encode the E2E decision in the instantiated
contract:

Observer-started MF/subagent work is not universally test-first. Before
delegation, the observer records `parallel_contract.test_scenario_policy` with
`mode=observer_configured`, the explicit test-scenario decision (`none`,
`reuse_existing`, or `new_scenario_required`), the reason, required evidence
ids, and the E2E run/defer/not_applicable decision. Deferred E2E must name the
follow-up backlog row before close-gate evidence can pass.

```json
{
  "parallel_contract": {
    "template_id": "mf_parallel.v1",
    "contract_instance_id": "<backlog-id>",
    "evidence_requirements": [
      {"id": "focused_tests", "required": true, "phase": "verification"},
      {
        "id": "integration_e2e",
        "required": true,
        "phase": "integration",
        "kind": "e2e",
        "command": "cd frontend/dashboard && npm run e2e:semantic -- --project <fixture> --probe"
      }
    ]
  }
}
```

Subagents should write only their assigned evidence. The observer writes
integration/build/E2E evidence after combining branches or worktree changes.
The close gate checks the instantiated contract, not just the generic
`verification` event.

#### R14.1 Artifact-backed E2E Flow

For dashboard, graph, bootstrap, project-management, file-hygiene, and semantic
operator paths, prefer artifact-backed fixture generation over hand-editing
generated example projects:

1. Extend the repo-owned fixture artifact first, usually
   `docs/fixtures/<fixture>/l4-*.md`, by adding or updating the
   `governance-hint` metadata and fenced `file path="..."` blocks.
2. Materialize the artifact into an isolated temporary workspace, not into
   `examples/*`, for E2E execution:
   `node scripts/materialize-fixture.mjs --root <temp-project> --artifact <artifact> --project-id <e2e-project>`.
3. Initialize/commit the generated fixture workspace before bootstrap so graph
   snapshots are commit-anchored and root repo status remains clean.
4. Add or update the E2E scenario to operate on the generated fixture: bootstrap
   or full-reconcile, make a fixture commit, run Update graph/scope reconcile,
   then assert dashboard/API state against the active snapshot.
5. For orphan file handling, the artifact must include an intentionally orphaned
   doc/test/config file. The E2E writes a governance hint, commits that fixture
   file, runs Update graph, and asserts that the file is attached to the chosen
   node.
6. If the E2E is deferred, file a backlog row with the missing artifact,
   scenario, expected assertions, and reason for deferral before closing the MF
   row.

#### R14.2 Source-controlled Governance Hint Repair

Governance hints are durable only as source-controlled evidence. Do not repair
doc/test/config bindings by editing graph DB rows directly.

When a hint contains only `target_node_id`, treat it as a repair candidate:
node ids can change after a full graph rebuild. Prefer stable target evidence
such as `target_module`, or the composite `target_area_key` +
`target_subsystem_key` + `target_title`. `target_title` alone is stable only
when it resolves to exactly one node. If a hint contains both an unambiguous
stable target and a node id, the stable target is authoritative; the node id is
legacy convenience evidence.

For hint reset/repair:

1. Audit the hint against the active snapshot.
2. Use the source-controlled hint repair path to either:
   - `stabilize` the hint by adding stable target metadata; or
   - `withdraw` the hint by removing the hint comment from the file.
3. Commit the changed file.
4. Run Update Graph/reconcile so the projected binding is added, moved, or
   withdrawn from graph state.

---

## 4. False Positive Evidence Standard

When wf_impact reports a node as affected but the change does not actually impact that node's functionality, it may be classified as a **false positive** — but only with sufficient evidence.

### Minimum Evidence Requirement

A false positive classification requires **at least 3 of the following 5 criteria** to be satisfied:

| # | Criterion | How to Check |
|---|-----------|-------------|
| E1 | Diff is additive only | `git diff --cached` shows only `+` lines, no `-` lines in the relevant file |
| E2 | Change location is outside node's functional scope | The modified lines are not in any function/handler that the node documents |
| E3 | Node has other primary files that are unchanged | Node's `primary` field lists multiple files; others are not in the dirty set |
| E4 | Related tests pass | `pytest agent/tests/test_<module>.py` exits 0 |
| E5 | Preflight baseline unchanged | `preflight_check` shows no new blockers compared to Phase 0 |
| E6 | Plugin update state clear | MCP `preflight_check` reports no `plugin_update_state` blockers; optional checkout-local CLI guard agrees |

### Required Documentation

Every false positive must be recorded with:

```
node_id:              L5.3
classification:       false_positive
evidence_satisfied:   [E1, E3, E4]    (minimum 3)
evidence_details:     "diff is +37 lines (additive only), node primary includes
                       role_service.py which is unchanged, test_server passes"
```

This documentation goes into the commit message and the structured audit record (S6).

---

## 5. Node Dependency Rules

### 5.1 verify_requires Dependency Chains

```
If an affected node has a verify_requires field:

  node_A (verify_requires: [node_B, node_C])
    -> node_B and node_C must have node_state = verified
    -> If upstream is pending -> do not proceed, or assess whether
       upstream needs verification first

How to check:
  wf_impact returns verify_requires in each affected_node entry
  If verify_requires is non-empty, check each dependency's state
```

### 5.2 gate_mode Handling

```
gate_mode=auto:
  -> System verifies automatically; auto_chain handles post-commit
  -> No additional manual action needed

gate_mode=explicit:
  -> Requires explicit verification to pass the gate
  -> If truly affected (not false positive):
     MANDATORY: create verification task after commit (Rule R3)
  -> If false positive (with evidence per S4):
     Record in audit, no verification task needed

gate_mode=skip:
  -> Node skips verification entirely, no action needed
```

### 5.3 Handling Inflated Impact Counts

```
Typical scenario: adding one new route to server.py -> wf_impact reports 15 nodes

Diagnosis:
  1. Review the actual diff: does it only add new code (no modification of existing logic)?
  2. Do the affected nodes' primary fields include other files beyond server.py?
  3. Is the change location within the functional scope of the affected node?

Resolution:
  - Truly affected nodes: verify per gate_mode rules
  - False positives: classify with evidence per S4, record in audit
```

---

## 6. Structured Audit Record

Every manual fix **must** produce a structured audit record in the following format. This record is appended to `docs/dev/bug-and-fix-backlog.md` under a new section `## Manual Fix Audit Log`.

### Template

```yaml
manual_fix_id:          MF-2026-04-05-001
timestamp:              2026-04-05T17:30:00Z
operator:               observer
trigger_scenario:       dirty_workspace_blocking_chain
                        # One of: dirty_workspace_blocking_chain,
                        #         fixing_auto_chain, fixing_executor,
                        #         governance_startup_failure

bypass_used:            skip_version_check
                        # Or: none, observer_merge, reconciliation_lane,
                        #     _DISABLE_VERSION_GATE

changed_files:
  - agent/governance/server.py (+37, modified)
  - agent/governance/reconcile.py (new)
  - agent/tests/test_reconcile.py (new)
  - docs/dev/reconcile-flow-design.md (new)
  - docs/dev/bug-and-fix-backlog.md (new)

classification:
  scope:                C (15 nodes reported)
  danger:               Low (additive only, new route + new files)
  combined_level:       C-Low

reported_impact:
  - L4.15  HTTP routing (real)
  - L5.3   Dual token model (false_positive, E1+E3+E4)
  - L5.4   Agent Lifecycle API (false_positive, E1+E3+E4)
  - L7.1   Context Assembly (false_positive, E1+E3+E4)
  # ... (list all 15)

actual_impact:
  - L4.15  (real, gate_mode=explicit, verify_level=4)

false_positive_nodes:   14
false_positive_reason:  "server.py granularity — only added 1 new route handler,
                         14 other nodes share server.py as primary but their
                         functional scope is unrelated to reconcile endpoint"

pre_commit_checks:
  - pytest test_reconcile.py: PASS (27 tests)
  - pytest agent/tests/ -x: PASS (275 tests)
  - preflight baseline: 0 blockers, 2 warnings
  - plugin update state: no blockers
e2e_impact:
  decision:              e2e_current | e2e_added | e2e_deferred | e2e_not_applicable
  suites:
    - dashboard.semantic.safe: current
  evidence:
    - e2e-20260512-abcdef
  deferred_backlog:
    - OPT-CHAIN-E2E-AUTORUN-POLICY

post_commit_checks:
  - governance restart: OK
  - version_check: ok=true, dirty=false
  - preflight delta: 0 new blockers
  - verification task created: task-XXXX for L4.15

workflow_restore_result: RESTORED
  - test task created: task-XXXX
  - status transitions observed: queued -> claimed -> succeeded
  - auto_chain dispatched next stage: YES
  - follow-up task found in task_list: YES

commit_hash:            (filled after commit)
followup_needed:
  - "wf_impact granularity issue: server.py triggers 15 nodes for any change.
     Consider splitting server.py or adding per-function node mapping."
```

### Purpose

This structured record enables:

| Use Case | How |
|----------|-----|
| Frequency analysis | Count manual fixes per trigger_scenario |
| Deadlock hotspot detection | Which modules most often enter deadlock |
| Bypass risk tracking | Which bypass_used types are most common |
| False positive pattern mining | Which nodes are most often false positives -> improve wf_impact |
| Workflow health monitoring | Track workflow_restore_result over time |

---

### 6.1 Canonical MF Commit-Message Template (Trailer-Priority)

Every manual fix commit MUST carry the four chain trailers below so that
`agent/governance/chain_trailer.py get_chain_state()` recognizes the commit as
the latest chain anchor.
Without these trailers the version gate will fall back to the previous trailered
commit and `HEAD != CHAIN_VERSION` will block all subsequent dispatch.

```
manual fix: <one-line summary of what changed and why>

<optional body: affected node list, false-positive evidence, rollback plan>

Chain-Source-Task: <originating task id, e.g. task-1777670504-0d2aed>
Chain-Source-Stage: observer-hotfix
Chain-Parent: <40-char hash of previous chain anchor commit>
Chain-Bug-Id: <bug id from backlog, e.g. OPT-BACKLOG-MF-SOP-R11-STALE-DB-VERSION-UPDATE-DEPRECATED>
```

Notes:

- `Chain-Source-Stage` value SHOULD be `observer-hotfix` for observer-driven manual
  fixes; other recognized values include `manual-fix` and `reconcile-hotfix`.
- `Chain-Parent` MUST be the full 40-char commit hash of the previous trailered
  chain anchor (find via `git log --grep='Chain-Source-Stage:' -n 1 --pretty=%H`).
- `Chain-Source-Task` and `Chain-Bug-Id` are required for cross-link audit; if no
  task/bug exists yet, create one first and reference its id here.
- A working example of a correctly trailered MF commit is `0d4329d`.

---

## 7. Reconcile vs Manual Fix: Responsibility Split

Manual fix and reconcile are **complementary but distinct**. They must not be confused or mixed:

```
┌─────────────────────────┐      ┌─────────────────────────┐
│      MANUAL FIX          │      │       RECONCILE          │
│                          │      │                          │
│ Responsibility:          │      │ Responsibility:          │
│  Freeze code state       │      │  Fix graph / node refs   │
│  (git commit)            │      │  Fix waive lifecycle     │
│                          │      │  Sync DB state           │
│ Operates on:             │      │ Operates on:             │
│  Working tree + git      │      │  Graph.json + governance │
│                          │      │  DB + node_state         │
│ Precondition:            │      │ Precondition:            │
│  Workflow deadlocked     │      │  Code state is frozen    │
│                          │      │  (committed)             │
│ Output:                  │      │ Output:                  │
│  Clean HEAD, dirty=false │      │  Consistent graph + DB   │
│  Workflow unblocked      │      │  ImpactAnalyzer works    │
└────────────┬────────────┘      └────────────┬────────────┘
             │                                 │
             │          CORRECT ORDER          │
             │                                 │
             v                                 v
        1. Manual Fix               2. Reconcile (if needed)
        (commit first)              (fix refs against committed code)

WRONG ORDER:
  reconcile -> commit -> graph drifts again (reconcile was based on stale code)

WRONG USAGE:
  Using reconcile as "fix everything" button (it only fixes graph/DB, not dirty workspace)
  Using manual fix to update graph refs (that is reconcile's job)
```

---

## 8. Common Pitfalls

### Pitfall 1: Forgetting to restart governance after commit

```
Symptom:  version_check shows ok=false; HEAD changed but SERVER_VERSION still old
Cause:    SERVER_VERSION is captured once at process startup, never auto-refreshed
Fix:      Restart governance service
Prevent:  Phase 4 step 1 is always "restart governance"
```

### Pitfall 2: git add -A accidentally staging sensitive files

```
Symptom:  .env, credentials, .claude/worktrees committed to repo
Cause:    git add -A stages everything without discrimination
Prevent:  Always add files explicitly by name; run git diff --cached before commit
```

### Pitfall 3: Manual fix introduces new dirty files

```
Symptom:  version_check still shows dirty=true after commit
Cause:    During the fix process, other files were modified (tests, docs)
Prevent:  Phase 0 records baseline dirty_files; Phase 4 compares
          If new dirty files appeared -> either commit them too, or git checkout to revert
```

### Pitfall 4: auto_chain reports dispatched:true but creates no task

```
Symptom:  task_complete returns {auto_chain: {dispatched: true}} but task_list is empty
Cause:    auto_chain dispatch silently blocked by version gate (B1/B6)
Diagnose: Check version_check.dirty — if still dirty files, gate is still blocking
Prevent:  Ensure ALL files are committed before relying on auto_chain
```

### Pitfall 5: Forgetting to audit version gate bypass

```
Symptom:  Tasks with skip_version_check mixed into normal chain, no audit trail
Cause:    skip_version_check has no access control or logging (B2)
Prevent:  Every bypass MUST be recorded in the structured audit record (S6)
```

### Pitfall 6: Wrong order — reconcile before commit

```
Wrong:  reconcile (fix graph refs) -> commit (introduce new code)
        -> reconcile results overwritten, graph drifts again
Right:  commit (freeze code state) -> reconcile (fix refs against latest code)
See:    S7 for the full responsibility split
```

### Pitfall 7: Forgetting to create governance nodes for new files (Rule R6)

```
Symptom:  New files committed but not mapped in graph.json. Coverage check shows
          unmapped files. wf_impact returns 0 nodes for the new file, making
          future changes invisible to governance.
Cause:    SOP v2 had no explicit step to check node existence for new files.
Prevent:  Rule R6 — before commit, check if each new file has a governance node.
          If not, add node to graph.json and include graph.json in the commit.
Discovered: MF-2026-04-05-001 — reconcile.py, test_reconcile.py, 3x .md all
            committed without nodes. Had to retroactively add L25.3 and L9.12.
```

### Pitfall 8: No execution record created during fix (Rule R7)

```
Symptom:  Manual fix completed but no external record documenting the step-by-step
          execution. Only the audit YAML in bug-and-fix-backlog.md exists, which
          lacks execution detail (decisions, false positive reasoning, timing).
Cause:    SOP v2 only required a structured audit record (R4) but not a full
          execution record showing each phase's inputs and outputs.
Prevent:  Rule R7 — create docs/dev/manual-fix-current-YYYY-MM-DD[-NNN].md at
          Phase 0 start. Fill incrementally as each phase completes.
Discovered: MF-2026-04-05-001 — execution record was created retroactively after
            all phases completed, losing real-time decision context.
```

### Pitfall 9: Multi-commit cycle without restart (Rule R8)

```
Symptom:  First commit updates code. Audit record or execution record generates a
          second commit. SERVER_VERSION still matches first commit, not second.
          Telegram gate notification fires: "SERVER_VERSION mismatch".
Cause:    SOP v2 Phase 4 says "restart governance" but doesn't account for the
          fact that Phase 5 (audit record) may create ANOTHER commit, requiring
          ANOTHER restart.
Prevent:  Rule R8 — after every commit (including audit/record commits), re-run
          Phase 4 (restart + version_check + preflight delta). Only push after
          the final restart confirms ok=true.
Discovered: MF-2026-04-05-001 — second commit (4394f36, audit record) triggered
            Telegram gate alert because governance was not restarted.
```

### Pitfall 10: Coverage warnings ignored for committed files (Rule R9)

```
Symptom:  preflight_check shows unmapped files as warnings, but operator ignores
          them because they are "pre-existing". Some of those unmapped files were
          just committed in this fix.
Cause:    SOP v2 compares preflight baseline for new blockers but doesn't require
          action on warnings related to the current commit's files.
Prevent:  Rule R9 — if any unmapped file in the coverage warning list is part of
          the current commit, it must get a node or be documented as intentionally
          unmapped.
```

### Pitfall 12: conn-contention — synchronous _publish_event while write-tx open (commit-before-publish)

```
Symptom:  auto_chain.py on_task_completed triggers _publish_event (e.g.
          pm.prd.published) while the main SQLite connection still has an
          open write transaction. Legacy EventBus subscribers that open
          their own write connections hit the 60s busy_timeout and stall
          the entire chain.
Cause:    _publish_event is called synchronously BEFORE conn.commit().
          SQLite WAL allows concurrent readers but only one writer; the
          subscriber's write attempt blocks until busy_timeout expires.
Pattern:  commit-before-publish — always call conn.commit() BEFORE any
          synchronous _publish_event call in auto_chain.py to release the
          write lock before subscribers attempt their own writes.
Fix:      MF-2026-04-24-001 (PM path, commits e745691+c6f05be+f740cbb)
          MF-2026-04-24-002 (dev path, commit bf3b497)
          Both applied the same commit-before-publish reordering in
          auto_chain.py on_task_completed.
Prevent:  Any new _publish_event call site in auto_chain.py must ensure
          conn.commit() is called first. Code review checklist item.
```

### Pitfall 11: Documentation placed in wrong directory (Rule R10)

```
Symptom:  Governance SOP placed in docs/dev/ instead of docs/governance/. Node
          L9.12 created with wrong path. Must be moved + node updated.
Cause:    No directory convention check in the SOP.
Prevent:  Rule R10 — verify doc location against project conventions before commit.
Discovered: MF-2026-04-05-001 — manual-fix-sop.md initially created in docs/dev/,
            later moved to docs/governance/.
```

---

## 9. Worked Example

### Example: Current State (2026-04-05)

```
Dirty files:
  M  agent/governance/server.py       (staged, +37 lines reconcile endpoint)
  ?? agent/governance/reconcile.py     (untracked, new file)
  ?? agent/tests/test_reconcile.py     (untracked, new file)
  ?? docs/dev/reconcile-flow-design.md (untracked, new file)
  ?? docs/dev/bug-and-fix-backlog.md   (untracked, new file)

Phase 0: ASSESS
  $ version_check -> ok=false, dirty=true, dirty_files=["server.py"]
  $ wf_impact(server.py) -> 15 nodes, 13 explicit, 2 auto
  $ wf_impact(reconcile.py) -> 0 nodes (new file, not in any node)
  $ wf_impact(test_reconcile.py) -> 0 nodes
  $ wf_impact(*.md) -> 0 nodes
  $ preflight_check -> baseline: 0 blockers, 2 warnings

Phase 1: CLASSIFY (dual-axis)
  Axis 1 (Scope): 15 reported nodes -> Scope C
  Axis 2 (Danger): all changes are additive (new route, new files) -> Low
  Combined: C-Low -> "Run full suite + record false positives"

  Impact analysis:
  - server.py: only adds 1 new route handler, does not modify existing routes
  - All 15 nodes triggered because their primary field includes server.py
  - Real impact: L4.15 (HTTP routing) — new route is within its scope
  - False positive: remaining 14 — evidence: E1 (additive only) + E3 (other
    primary files unchanged) + E4 (tests pass) = 3/5 criteria met

Phase 2: PRE-COMMIT VERIFY
  [x] Mandatory rule check:
      - R1 (Scope D): N/A, we are Scope C
      - R2 (delete/rename): N/A, no deletions or renames
      - R3 (explicit+v4 real): L4.15 is real + explicit + v4 -> MUST create
        verification task after commit
      - R4 (audit record): will produce after commit
      - R5 (workflow restore proof): will execute in Phase 5
  [x] pytest agent/tests/test_reconcile.py -> 27 tests PASS
  [x] pytest agent/tests/ -x -> 275 tests PASS
  [x] L4.15 verify_requires: [] (no upstream dependencies)
  [x] False positive evidence documented for 14 nodes (E1+E3+E4)

Phase 3: COMMIT
  $ git add agent/governance/server.py
  $ git add agent/governance/reconcile.py
  $ git add agent/tests/test_reconcile.py
  $ git add docs/dev/reconcile-flow-design.md
  $ git add docs/dev/bug-and-fix-backlog.md
  $ git diff --cached   (verify staged files are correct)
  $ git commit -m "manual fix: add reconcile feature (endpoint + core + tests + docs)

  Affected nodes: L4.15 (real), L5.3-L10.8 (false positive, E1+E3+E4)
  Bypass reason: dirty workspace blocking all auto_chain dispatch (B1)
  Files: server.py (+37), reconcile.py (new), test_reconcile.py (new), 2x .md (new)"

Phase 4: POST-COMMIT VERIFY
  $ restart governance
  $ version_check -> ok=true, dirty=false
  $ preflight_check -> compare: 0 blockers (same as baseline), no regression
  $ python -m agent.cli mf precommit-check --json-output -> plugin_update_state has no blockers
  $ wf_impact(server.py) -> confirm 15 nodes (unchanged)
  $ task_create type=test for L4.15 verification (Rule R3)
  $ POST /api/task/<project>/timeline event_kind=implementation with changed files and implementation evidence
  $ POST /api/task/<project>/timeline event_kind=verification with test/preflight/reconcile evidence
  $ POST /api/task/<project>/timeline event_kind=close_ready after graph is current and close evidence is complete

Phase 5: WORKFLOW RESTORE PROOF
  $ task_create type=test "verify auto_chain dispatch works"
  $ Observe: queued -> claimed -> succeeded (state transitions confirmed)
  $ Observe: auto_chain dispatched next stage (follow-up task exists in task_list)
  $ Record: workflow_restore_result = RESTORED
  $ observer_mode(false)
  $ Write structured audit record to bug-and-fix-backlog.md
```

---

## 10. Decision Tree (Quick Reference)

```
Code needs manual commit?
  |
  +-- Can use Workflow? --YES--> Do NOT manually fix. Use PM->Dev->Test->QA->Merge
  |
  +-- Is this a bootstrap? --YES--> Use Bootstrap Flow, not this SOP
  |
  +-- Cannot use Workflow (deadlock / infrastructure failure)
       |
       +-- Phase 0: git status + wf_impact + preflight + version_check
       |
       +-- Classify: Scope (A/B/C/D) x Danger (Low/Med/High)
       |
       +-- Any delete/rename in diff?
       |    +-- YES -> MANDATORY: reconcile(dry_run=true) first (Rule R2)
       |
       +-- Scope D (>20 nodes)?
       |    +-- YES -> MANDATORY: split commit (Rule R1)
       |
       +-- Run checks per matrix level
       |
       +-- Any new files in commit?
       |    +-- YES -> check/create governance nodes (Rule R6)
       |    +-- YES -> verify doc location conventions (Rule R10)
       |
       +-- Create execution record (Rule R7)
       |
       +-- Check unmapped files in coverage that are in this commit (Rule R9)
       |
       +-- Commit with "manual fix:" prefix + node impact + evidence
       |
       +-- Post-commit: restart + version_check + preflight + delta check
       |
       +-- Real impact on explicit+v4 node?
       |    +-- YES -> MANDATORY: create verification task (Rule R3)
       |
       +-- Additional files generated (audit, execution record, nodes)?
       |    +-- YES -> commit them, LOOP back to restart (Rule R8)
       |
       +-- Workflow restore proof: create test task, observe full chain
       |    +-- RESTORED -> push allowed
       |    +-- STILL_BROKEN -> diagnose, do NOT push
       |
       +-- Write structured audit record (Rule R4)
```

---

## 11. Relationship to Other Flows

```
Manual Fix SOP (this document)
  |
  +-- Precondition: Workflow deadlocked (not bootstrap, not normal dev)
  |
  +-- Tools used:
  |   +-- version_check (MCP)
  |   +-- wf_impact (MCP)
  |   +-- preflight_check (MCP)
  |   +-- git (CLI)
  |   +-- reconcile (optional, for post-commit graph repair)
  |
  +-- After completion: Workflow resumes normal operation
  |   +-- All subsequent fixes return to Workflow
  |
  +-- Audit: structured record in bug-and-fix-backlog.md
```

| Flow | When to Use | Relationship |
|------|-------------|--------------|
| Workflow (PM->Dev->...->Merge) | Normal development | Manual fix goal is to restore this |
| Reconcile | Graph node references drifted | Run AFTER manual commit if graph needs repair (see S7) |
| Bootstrap | First-time initialization | Separate flow, do not use this SOP |
| This SOP | Workflow deadlock only | Minimal-scope fix, return to Workflow ASAP |

---

## 12. Metadata Propagation (G9)

When performing a manual fix that spans multiple phases or creates follow-up tasks, the following task metadata fields **must** be preserved and carried forward. Losing any of these fields breaks the auto-chain's ability to track lineage and enforce governance.

### Required Metadata Fields

| Field | Purpose | Propagation Rule |
|-------|---------|-----------------|
| `target_files` | Files the task is allowed to modify; used by checkpoint gate to verify scope | Copy verbatim from the originating PM task into every subsequent stage (dev, test, QA, merge). Never narrow or expand without a new PM decision |
| `acceptance_criteria` | Grep/script-based checks that the QA stage evaluates | Copy verbatim from PM result. Do not paraphrase — QA agents parse these literally |
| `chain_context` | Event-sourced runtime context linking all stages in a chain (see Phase 8) | Serialized JSON blob; pass as-is via `chain_context` field in task metadata. Never manually edit — auto_chain appends stage events automatically |
| `ref_id` | Entity reference ID linking the task to a governance graph node or memory entity | Preserve across retries and manual re-creations. If a manual fix replaces a failed task, reuse the original `ref_id` so lineage remains connected |
| `_branch` | Git branch name for the worktree where changes live | Required by merge stage. If manually creating a merge task, extract from the dev task's result metadata |
| `_worktree` | Filesystem path to the isolated worktree | Required by merge stage for cherry-pick/merge operations. Must point to a valid worktree; verify with `git worktree list` before propagating |

### Propagation Checklist (per phase)

1. **Phase 0 (Assess):** Record the originating task's metadata snapshot — capture `target_files`, `acceptance_criteria`, `chain_context`, and `ref_id` before making any changes
2. **Phase 3 (Commit):** Include metadata field names in the commit message body for audit traceability
3. **Phase 4 (Post-commit):** When creating follow-up verification tasks (Rule R3), copy `target_files` and `acceptance_criteria` from the original PM task
4. **Phase 5 (Workflow Restore):** When creating test tasks to prove workflow restoration, include `chain_context` so the test task links back to the manual fix chain

### Anti-Patterns

| Anti-Pattern | Consequence | Prevention |
|-------------|-------------|------------|
| Omitting `target_files` from dev task | Checkpoint gate cannot verify scope → gate_blocked | Always copy from PM result |
| Manually editing `chain_context` JSON | Corrupts event-sourced history → chain context archive unusable | Let auto_chain manage; only read, never write |
| Creating merge task without `_branch`/`_worktree` | Merge stage cannot locate changes → merge fails with "no worktree" error (D6 fix) | Extract from dev task result; verify worktree exists |
| Reusing `ref_id` from an unrelated task | Governance graph links unrelated changes → false impact analysis | Only reuse `ref_id` from the same logical chain |

---

## 13. Codex Review Adoption Log

| # | Suggestion | Adopted | Detail |
|---|-----------|:-------:|--------|
| 1 | Add hard rules instead of soft guidance | **Full** | 5 mandatory rules (R1-R5) in S3, cannot be overridden |
| 2 | Dual-axis classification (scope + danger) | **Full** | Combined matrix in S3, replaces single-axis node count |
| 3 | False positive evidence standard | **Full** | 5 criteria (E1-E5), minimum 3 required, per S4 |
| 4 | Workflow restore proof in post-commit | **Full** | Phase 5 requires observed state transitions + follow-up task existence |
| 5 | Structured audit template | **Full** | YAML template in S6 with 15 fields for analytics |
| 6 | Clearer reconcile relationship | **Full** | S7 responsibility split diagram + correct order + wrong usage examples |
| 7 | Bootstrap boundary clarification | **Full** | Option A adopted: bootstrap removed from manual fix scenarios, explicit exclusion in S1 |

---

## 14. v3 Dogfooding Findings (MF-2026-04-05-001)

Executing MF-2026-04-05-001 using SOP v2 revealed 5 gaps. Each gap is now covered by a new mandatory rule:

| Gap Found | What Happened | New Rule |
|-----------|---------------|----------|
| No new-file node check | reconcile.py + 3x .md committed without governance nodes. Had to retroactively add L25.3 and L9.12 to graph.json | R6 |
| No execution record requirement | Audit record (R4) lacked step-by-step execution detail. Execution record created retroactively, losing real-time context | R7 |
| No multi-commit restart loop | Second commit (audit record) triggered Telegram gate alert because governance wasn't restarted again | R8 |
| Coverage warnings not actionable | preflight reported 49 unmapped files as "pre-existing warning", but some were just committed | R9 |
| No doc location verification | SOP initially placed in docs/dev/ instead of docs/governance/. Node path was wrong | R10 |

These findings validate the dogfooding approach: using the SOP to update the SOP exposed real procedural gaps that would have remained hidden in a workflow-driven update.

---

## 15. Registered Manual Fixes

Precedent manual fixes registered for audit trail and pattern reference.

| MF ID | Date | Scenario | Classification | Pattern | Commits | Description |
|-------|------|----------|----------------|---------|---------|-------------|
| MF-2026-04-24-001 | 2026-04-24 | fixing_auto_chain_itself — conn contention | Scope C / High | commit-before-publish | e745691+c6f05be+f740cbb | PM-path conn-contention fix in auto_chain.py: reordered conn.commit() before synchronous _publish_event calls to prevent legacy subscriber 60s busy_timeout stall |
| MF-2026-04-24-002 | 2026-04-24 | fixing_auto_chain_itself — conn contention | Scope C / High | commit-before-publish | bf3b497 | Dev-path conn-contention fix in auto_chain.py: same commit-before-publish reordering applied to the dev task completion path |

---

# Manual-Fix vs Chain-Spawn Decision Matrix

Per proposal §15.5b, manual-fix is reserved for B48-class meta-circular deadlocks (chain pipeline broken, governance wedge, deploy-selfkill, graph corrupted). Routine reconcile-detected gaps must go through Phase H chain-spawn. This section documents the boundary.

## Decision matrix

| # | Trigger Scenario | Route | Rationale |
|---|-----------------|-------|-----------|
| 1 | Missing API doc | **Chain-spawn** | Documentation gap does not block the chain pipeline; PM can generate a PRD and dev can write the doc through normal workflow |
| 2 | Missing unit test | **Chain-spawn** | Test gaps are routine work items; the chain is fully operational and can handle test creation |
| 3 | Phase Z found candidate node missing | **Chain-spawn** | Node gaps are detected by reconcile and can be addressed through a standard PM→Dev→Test→QA→Merge chain |
| 4 | Stale ref / wrong path | **Chain-spawn** | Reference corrections are routine maintenance; reconcile detects them and chain-spawn fixes them |
| 5 | Chain pipeline itself broken | **Manual-fix** | Meta-circular deadlock: the tool used to fix things is itself broken. Cannot chain-spawn a fix for the chain engine |
| 6 | Security/sensitive doc | **Manual-fix** | Security-sensitive changes may require immediate intervention outside normal chain timing; observer discretion applies |
| 7 | Hotfix during incident | **Manual-fix** | Active incident with pipeline down or governance wedged; waiting for full chain would extend the outage |

**Rule of thumb:** If the chain pipeline is operational, use chain-spawn. Manual-fix is only for when the pipeline itself cannot execute.

## Why manual-fix is narrow

A manual-fix bypasses the following governance mechanisms that a normal chain enforces:

1. **PM PRD review** — no structured requirements document is produced
2. **dev contract** — no target_files / acceptance_criteria scope constraint
3. **test/qa** — no automated test stage or QA verification pass
4. **gatekeeper** — no gatekeeper review of changed files against allowed scope
5. **audit trail** — reduced audit trail (only git commit prefix + optional MF execution record, vs. full chain event history)

Each bypass increases the risk of undetected regressions, scope creep, and governance drift. This is why manual-fix must remain narrow: reserved for scenarios where the chain cannot run at all.

## Manual-fix has only

Unlike a full chain (which produces PM PRD, dev contract, test results, QA pass, gatekeeper approval, and merge audit), a manual-fix provides only:

1. **Git commit prefix**: either `manual fix:` or `[observer-hotfix]` — this is the primary audit signal that identifies a commit as bypassing the normal chain
2. **Optional MF execution record file**: `docs/dev/manual-fix-current-YYYY-MM-DD[-NNN].md` — a freeform record of the manual fix steps, decisions, and outcomes (per Rule R7)

These two mechanisms are the entire governance surface for a manual-fix. Everything else (scope verification, test coverage, regression detection) depends on the operator's diligence.

## Examples (today's session — 2026-04-25)

### Example A: Chain A fix at 7cf3ca1

- **Scenario:** Chain pipeline broken (auto_chain dispatch silently dropping tasks)
- **Route:** Manual-fix (correct — pipeline itself was non-functional)
- **Commit:** 7cf3ca1 with `manual fix:` prefix
- **Justification:** The chain engine's dispatch mechanism was the component being repaired; spawning a chain to fix the chain would deadlock

### Example B: Observer-hotfixes bf564b5 + cac32c3

- **Scenario:** Observer-detected issues requiring immediate intervention
- **Route:** Manual-fix via `[observer-hotfix]` prefix
- **Commits:** bf564b5 and cac32c3
- **Justification:** Issues discovered during active incident where waiting for full chain execution would have extended the outage window

## Concrete manual-fix authorship steps

When a manual-fix is warranted (per the decision matrix above), follow these 5 steps:

1. **Verify chain-broken** — Confirm the chain pipeline cannot execute. Check: `GET /api/version-check/{pid}` returns errors, `task_create` fails, or auto_chain dispatch is non-functional. Document the evidence
2. **Make minimal change** — Apply the smallest possible code or documentation change that restores pipeline operation. Do not bundle unrelated improvements
3. **Commit with prefix** — Use `git commit -m "manual fix: <reason>"` or `git commit -m "[observer-hotfix] <reason>"`. Include affected node list and bypass reason in the commit body
4. **Write MF execution record** — Create `docs/dev/manual-fix-current-YYYY-MM-DD[-NNN].md` documenting: what broke, what was changed, why chain-spawn was not viable, and post-fix verification results
5. **Verify state** — Restart governance, run `version_check` (expect `ok=true, dirty=false`), run `preflight_check`, and confirm workflow restore per Phase 5 of this SOP

## Troubleshooting: L7 Node Drop During Merge Gate

### Symptoms
- PM proposed_nodes with `parent_layer=7` or `parent_layer="L7"` are present in `pm.prd.published` chain_event but absent from `node_state` after merge completes
- Phase B reconcile reports `pm_proposed_not_in_node_state` discrepancies for L7 nodes
- `_commit_graph_delta` log shows `skipping creates[] item with non-int parent_layer: L7`

### Root Cause
Prior to the fix, `_commit_graph_delta` attempted `int(parent_layer)` which rejected string-prefixed values like `"L7"`. PM stages typically emit `parent_layer="L7"` (string with prefix) while `_commit_graph_delta` only accepted bare integers.

### Verification Query
```sql
-- Check for L7 nodes in chain_events but missing from node_state
SELECT ce.root_task_id, ce.payload_json
FROM chain_events ce
WHERE ce.event_type = 'graph.delta.validated'
  AND ce.payload_json LIKE '%parent_layer%L7%'
  AND NOT EXISTS (
    SELECT 1 FROM node_state ns
    WHERE ns.project_id = 'aming-claw'
      AND ns.node_id LIKE 'L7.%'
  );
```

### Manual Backfill Steps
1. Identify dropped L7 nodes from the validated payload above
2. For each dropped node, insert into node_state:
   ```sql
   INSERT OR IGNORE INTO node_state (project_id, node_id, verify_status, build_status, updated_at, version)
   VALUES ('aming-claw', 'L7.<N>', 'pending', 'unknown', datetime('now'), 1);
   ```
3. Run reconcile Phase B to confirm no remaining `pm_proposed_not_in_node_state` discrepancies
4. Restart governance to clear any cached state

## Cross-references

- **Proposal §15.5b**: The decision matrix above is derived from `proposal-reconcile-comprehensive-2026-04-25.md` section 15.5b, which defines the manual-fix vs chain-spawn boundary
- **B48 precedent**: The meta-circular deadlock pattern was first documented in `project_b48_sm_sidecar_import.md` — executor silent-death caused by SM sidecar ImportError, where the fix tooling (executor) was itself the broken component. This B48 precedent established that manual-fix is appropriate when the repair target is the repair tool itself
- **Phase H chain-spawn**: Routine gaps (missing docs, missing tests, stale refs, missing nodes) detected by reconcile are routed through Phase H chain-spawn, which creates a full PM→Dev→Test→QA→Merge chain for each gap. See reconcile flow documentation for Phase H details
