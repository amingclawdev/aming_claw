---
name: aming-claw-backlog-dupe-demo
description: Public demo for Backlog Duplicate: a new requirement overlaps existing backlog work, and the observer asks whether to merge, supersede, or keep it separate. Use when a user asks to run, preview, or collect duplicate backlog evidence with the current Claude Code or Codex observer session.
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

# Backlog Duplicate Demo

Show that a new request is checked against similar backlog work before the
observer creates another item.

## Role

The preferred observer is the user's current Claude Code or Codex session. This
session should accept the new requirement, check for similar backlog work, ask
the user to choose merge, supersede, or separate, and then record the decision.

Scripted runners are setup and CI fallback only. They may create an isolated
fixture or run deterministic smoke for all three choices, but do not claim
scripted output as proof that a live AI observer made the backlog decision.

## Fixture Setup

Use fixture setup only when there is no safe project ready.

Expected setup helper:

```bash
node frontend/dashboard/scripts/e2e-backlog-dupe-fixture.mjs --no-browser
```

Fixture setup should return a project id and seed one or more similar backlog
rows. It should not be described as the live observer's overlap analysis or
user decision.

## Observer Behavior

1. Confirm governance, dashboard, graph, backlog, and review state.
2. If needed, create or select the isolated fixture project.
3. Capture the user's new requirement in ordinary language.
4. Search backlog and, when useful, graph context for related work.
5. Explain the overlap plainly.
6. Ask the user to choose one path:
   - merge: add the new detail to the existing backlog row;
   - supersede: mark the older direction as replaced and preserve the reason;
   - separate: create a separate row and explain why it is not a duplicate.
7. Apply only the choice the user made.
8. Show dashboard links for backlog, timeline, graph, operations, and review
   state.
9. End with a short evidence summary and limitations.

## Evidence Summary

```text
Backlog Duplicate evidence
- Project: <project_id>
- New request: <summary>
- Similar backlog: <bug ids/links>
- User choice: <merge/supersede/separate>
- Final backlog state: <updated or new bug id/link>
- Dashboard: <backlog link>, <graph link>, <review link>, <operations link>
- Limitations: <stale graph/weak similarity/scripted setup/etc>
```
