---
name: aming-claw-hn-demo-during-work
description: HN demo case for the fear that AI and subagents are black boxes during implementation. Guides evidence collection for timeline DAG, observer and subagent lanes, dispatch gate, evidence inspector, isolated worktrees, and append-only contract evidence.
---

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
