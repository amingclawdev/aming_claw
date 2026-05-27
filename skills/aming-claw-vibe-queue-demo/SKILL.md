---
name: aming-claw-vibe-queue-demo
description: Public demo for Vibe Queue: keep talking while agents work. Use when a user asks to run, preview, or collect evidence for a mid-implementation requirement being handled by the current Claude Code or Codex observer session.
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

# Vibe Queue Demo

Show that a user can keep talking while work is in progress and the observer
keeps the queue understandable.

## Role

The preferred observer is the user's current Claude Code or Codex session. This
session should check runtime state, create or inspect backlog rows, record the
mid-implementation requirement, and explain the decision.

Scripted runners are setup and CI fallback only. They may create an isolated
fixture or run a deterministic smoke, but do not claim scripted output as proof
that a live AI observer handled the conversation.

## Fixture Setup

Use fixture setup only when there is no safe project ready.

Expected setup helper:

```bash
node frontend/dashboard/scripts/e2e-vibe-queue-fixture.mjs --no-browser
```

Fixture setup should return a project id and leave the real user project alone.
It should not be described as the observer's live reasoning, backlog decision,
or AI proof.

## Two-Window Demo Setup

The primary demo subject is the target project: **Daily Planner Lite**. Show the
project id, fixture root, and planner preview before explaining Aming Claw repo
internals.

Use Codex's in-app browser for the Aming Claw dashboard. Open the planner
preview in the user's normal browser so they can watch the product update while
Codex keeps the governance cockpit visible.

Do not imply Codex can keep both pages visible or controllable at once. Present
the two links this way:

- **Open Aming Claw Dashboard** — Use in Codex:
  `http://localhost:40000/dashboard?project_id=<project_id>&view=backlog`
- **Open Daily Planner Preview** — Open in external browser:
  `http://127.0.0.1:<preview_port>/`

If a local preview server is not already running, tell the user to run the
fixture output's preview command, normally:

```bash
python3 -m http.server <preview_port> --directory <fixture_root>
```

Keep dashboard links visible for backlog, timeline, prompt queue, graph,
operations, and review state even when the planner preview is opened outside
Codex.

## Observer Behavior

1. Confirm governance, dashboard, graph, and backlog state.
2. If needed, create or select the isolated fixture project.
3. Show the target project first: Daily Planner Lite, its fixture root, the
   planner preview URL, and the dashboard backlog link.
4. Clarify the first requirement, then write one backlog row only after the
   user confirms. For the default demo, this is Today Focus at the top of the
   planner.
5. Clarify the second requirement, then write a second backlog row only after
   the user confirms. For the default demo, this is per-task reminder toggle,
   default off.
6. Do not dispatch implementation until the user explicitly says to start.
7. Start only bounded work that has target files and acceptance criteria.
8. When the user adds a new requirement during implementation, pause and
   classify it. For the default demo, this is quick capture input:
   - fold into current work if it is small and within the same acceptance
     boundary;
   - create a follow-up if it is related but expands scope;
   - keep separate if it changes the product direction or target area.
9. Record the decision in backlog or timeline evidence.
10. Show dashboard links for backlog, timeline, prompt queue, graph,
   operations, and review state.
11. End with a short evidence summary and limitations.

## Evidence Summary

```text
Vibe Queue evidence
- Project: <project_id>
- Open Aming Claw Dashboard (Use in Codex): <dashboard backlog/timeline link>
- Open Daily Planner Preview (Open in external browser): <localhost preview link>
- Prompt queue: <dashboard prompt queue/backlog link>
- Initial requests: <Today Focus backlog id/link>, <reminder toggle backlog id/link>
- Mid-run requirement: <summary>
- Observer decision: <folded/follow-up/separate + reason>
- Worker state: <queued/running/done/blocked/scripted fallback>
- Commit order: <serial commits or pending commit queue>
- Dashboard: <backlog link>, <graph link>, <operations link>, <review link>
- Limitations: <stale graph/offline dashboard/scripted setup/etc>
```
