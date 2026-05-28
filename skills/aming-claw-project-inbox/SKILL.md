---
name: aming-claw-project-inbox
description: Use when a user wants to capture raw requirements, keep talking without dispatching work, review a batch of captured requirements, or promote confirmed requirements into Aming Claw backlog rows from the Project Inbox.
---

## REQUIRED FIRST READ

Before any response that uses this skill, in this exact order:

  ListMcpResourcesTool()
  ReadMcpResourceTool(uri="aming-claw://current-context")
  ReadMcpResourceTool(uri="aming-claw://skill")

current-context anchors project_id, governance URL, dashboard URL, and safe
first actions. The root skill defines Observer Operating Modes and backlog/MF
rules.

# Project Inbox

Project Inbox is the lightweight intake mode for ordinary product work.

It separates two jobs that agents often blur:

1. **Capture Mode**: store the user's raw words exactly as requirements.
2. **Confirmation Mode**: after the user says the intake round is done,
   refine the captured items into backlog rows.

## Default Dashboard

Open the project homepage at:

```text
http://localhost:40000/dashboard?project_id=<project_id>&view=inbox
```

This page shows raw requirements first, then backlog lanes:

- Raw Inbox
- Needs Confirmation
- Ready Backlog
- In Progress
- Review Needed
- Done

## Capture Mode

Use Capture Mode when the user is still thinking, adding ideas quickly, or
says things like:

- "先记录"
- "需求模式"
- "我先连续说几个需求"
- "先别拆，先存下来"
- "keep capturing"

In Capture Mode, lower reasoning deliberately:

- Do not run graph discovery.
- Do not decompose the requirement.
- Do not create or update backlog rows.
- Do not dispatch workers.
- Do not summarize away user wording.

Only store the user's raw requirement text through governance:

```bash
curl -sS -X POST \
  "http://127.0.0.1:40000/api/projects/<project_id>/raw-requirements" \
  -H "Content-Type: application/json" \
  -d '{
    "raw_text": "<exact user text>",
    "source": "chat",
    "actor": "observer",
    "session_id": "<optional session id>"
  }'
```

Then reply with the captured id and a short acknowledgement. Keep the user in
flow.

## Confirmation Mode

Use Confirmation Mode when the user says the intake round is ready to refine,
for example:

- "确认这一轮需求"
- "整理成 backlog"
- "开始拆分"
- "把刚才的需求细化"
- "进入确认模式"

In Confirmation Mode:

1. List raw requirements:

   ```bash
   curl -sS \
     "http://127.0.0.1:40000/api/projects/<project_id>/raw-requirements?status=raw_inbox"
   ```

2. Group duplicates and overlaps before filing backlog rows. If a similar
   backlog row already exists, ask whether to merge, replace, or keep separate.

3. Ask only the missing questions needed to produce actionable backlog rows:
   target user, desired behavior, acceptance criteria, and obvious boundaries.

4. For each confirmed item, use the normal Aming Claw backlog path:
   `backlog_upsert` with target files and acceptance criteria when known.

5. Mark each raw requirement as promoted only after a real backlog row exists:

   ```bash
   curl -sS -X POST \
     "http://127.0.0.1:40000/api/projects/<project_id>/raw-requirements/<raw_id>/status" \
     -H "Content-Type: application/json" \
     -d '{
       "status": "promoted",
       "promoted_bug_id": "<backlog bug id>"
     }'
   ```

If the user rejects or shelves an item, mark it `dismissed` with a note.

## Guardrails

- Capture Mode is intentionally not graph-first. It is intake, not design.
- Confirmation Mode returns to the normal Aming Claw contract: graph/backlog
  checks before implementation.
- Never claim work has started because a raw requirement was captured.
- Never mark a raw requirement `promoted` without a real backlog id.
- If governance is offline, ask the user to run `aming-claw start`; do not
  silently start services.
