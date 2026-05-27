---
name: aming-claw-drift-demo
description: Public demo for Docs Drift: a feature changes but documentation is stale, and the drift state is surfaced before a second doc fix. Use when a user asks to run, preview, or collect evidence for docs drift with the current Claude Code or Codex observer session.
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

# Docs Drift Demo

Show that a feature can change while docs are stale, and that the stale-doc
state remains visible until a doc-fix step addresses it.

## Role

The preferred observer is the user's current Claude Code or Codex session. This
session should run the two-step story: first change the feature while leaving
docs stale, then guide the doc fix and compare the dashboard evidence.

Scripted runners are setup and CI fallback only. They may create an isolated
fixture or run a deterministic smoke, but do not claim scripted output as proof
that a live AI observer noticed documentation drift.

## Fixture Setup

Use fixture setup only when there is no safe project ready.

Expected setup helper:

```bash
node frontend/dashboard/scripts/e2e-drift-demo-fixture.mjs --no-browser
```

Fixture setup should return a project id and prepare a small feature plus docs.
It should not be described as the live observer's drift detection or product
judgment.

## Observer Behavior

1. Confirm governance, dashboard, graph, operations, review, and asset state.
2. If needed, create or select the isolated fixture project.
3. Run the first bounded feature change while intentionally leaving docs stale.
4. Show where stale docs or drift appear: graph stale state, Asset Inbox,
   Review Queue, Operations Queue, backlog, or timeline.
5. Avoid saying the project knowledge is current after only the feature change.
6. Ask for or accept the second doc-fix prompt.
7. Update the docs and compare the dashboard state after the fix.
8. End with a short evidence summary and limitations.

## Evidence Summary

```text
Docs Drift evidence
- Project: <project_id>
- First feature change: <backlog id/link or timeline event>
- Stale docs signal: <asset/review/operations/graph evidence>
- Doc fix: <backlog id/link or changed doc>
- Dashboard: <graph link>, <assets link>, <review link>, <operations link>
- Limitations: <proposal not accepted/stale graph/scripted setup/etc>
```
