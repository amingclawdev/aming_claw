---
name: aming-claw-hn-demo-during-work
description: HN demo case for the fear that AI and subagents are black boxes during implementation. Guides evidence collection for timeline DAG, observer and subagent lanes, dispatch gate, evidence inspector, isolated worktrees, and append-only contract evidence.
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

# HN Demo: During Work

Show how Aming Claw makes subagent work observable while it is happening.

## Fear

AI and subagents are black boxes: they run somewhere, change something, and the
operator cannot tell whether scope, identity, or evidence is trustworthy.

## Evidence To Collect

- Timeline DAG: ordered events for dispatch, implementation, verification, and
  handoff or review-ready state.
- Observer/subagent lanes: which actor made each decision or produced each
  artifact.
- Dispatch gate: evidence that branch, worktree, base commit, target commit,
  merge queue id, fence token, and owned files were checked before handoff.
- Evidence inspector: links or screenshots showing contract evidence,
  precheck ids, trace ids, or test results.

## Architecture Reason

- `mf_sub` dispatch gate blocks unsafe worker startup before implementation.
- Isolated worktrees and file fences keep parallel changes bounded.
- Append-only timeline records what happened without rewriting history.
- Contract evidence turns handoff claims into checkable requirements.

## Synthetic Data Setup (only if data does not exist)

If task_timeline_list / backlog_list returns empty for the demo project, you are
CREATING demo data, not reading existing data. Mandatory rules:

1. DO NOT call task_create with type=pm/dev/test/qa/merge. That is the chain
   path. V1 default is observer-led mf_parallel.v1.

2. Write parallel_contract into backlog.chain_trigger_json via backlog_upsert.
   workers[] is an array; for parallel work include multiple workers with
   DISJOINT owned_files. For the HN during-work case, create at least two
   worker entries; a single-worker timeline does not demonstrate parallel
   observability. The bundled screenshot currently shows three worker lanes,
   which is acceptable but not required.

3. Tie every task_timeline_append to the same mf_id (MF-<BACKLOG-ID>).
   Per-worker events use the worker's task_id; observer events can use
   parent_task_id.

4. Before the first mf_sub graph_query, create or verify server-side worker
   runtime identity through governance
   `/api/graph-governance/<pid>/parallel-branches/allocate` using the worker's
   task_id, parent_task_id, fence_token, base_commit, target_head_commit, and
   merge_queue_id. Local `aming-claw mf dispatch-gate` validates dispatch
   evidence but does not by itself register the fence for graph-query auth.

5. For each mf_sub graph_query: query_source="mf_subagent" + the worker's
   task_id, parent_task_id, worker_role="mf_sub", fence_token as top-level
   params.

6. Capture the returned trace_id and write into payload.graph_query_trace_ids
   in the timeline event. NEVER fabricate trace_id strings -- anyone can GET
   /api/graph-governance/<pid>/query-traces/<trace_id> to verify.

7. mf_type=chain_rescue in mf_timeline_precheck output is the MVP MF storage
   bucket label, not an error. See aming-claw://mf-sop.

## Role and Mode

This subskill inherits the observer-mode operator role, mode boundaries, and
acceptance criteria from the umbrella `aming-claw-hn-demo` skill loaded by the
Skill tool.
Do not invent an `aming-claw://skill-hn-demo` MCP resource.

This subskill covers only During Work operator steps for timeline DAG, lanes,
dispatch/startup gate evidence, evidence inspector details, and implementation
observability. Use Design Alignment by default; enter Execution Supervisor only
when the umbrella mode gate is explicitly satisfied.

## Operator Steps

1. Check governance and dashboard status. If governance is offline, instruct
   the user to run `aming-claw start`; do not start it silently.
2. Open the Backlog row and timeline for the demo work item.
3. Find observer and `mf_sub` lane events. Identify dispatch, startup,
   implementation, verification, and handoff or review-ready checkpoints.
4. Inspect dispatch-gate evidence: assigned worktree, branch, base commit,
   target head, merge queue id, fence token, and owned files.
5. Open evidence details for prechecks, graph query trace ids, tests, and
   screenshots where available.
6. Capture screenshots or links for the timeline DAG, lanes, gate evidence,
   and evidence inspector.

## Evidence Summary

```text
During-work evidence
- Fear: subagents are black boxes
- Timeline DAG: <link/screenshot/event ids>
- Lanes: <observer lane/subagent lane evidence>
- Dispatch gate: <precheck id or gate evidence>
- Evidence inspector: <link/screenshot/trace ids/tests>
- Architecture reason: mf_sub gate + isolated worktrees + append-only timeline + contract evidence
- Limitations: <none/offline dashboard/manual screenshot/etc>
```
