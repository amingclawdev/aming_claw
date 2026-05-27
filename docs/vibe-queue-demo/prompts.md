# Vibe Queue Demo Prompts

Send these prompts one message at a time. Wait for the observer to answer each
step before sending the next one.

## 1. Start The Demo

```text
Use this current Claude Code or Codex session as the observer for the Vibe Queue
demo. If there is no safe project ready, set up the isolated fixture first.
Show Daily Planner Lite as the target project first. Use Codex's in-app browser
for the Aming Claw dashboard, and tell me to open the planner preview in my
normal browser. Show me:

- Open Aming Claw Dashboard — Use in Codex
- Open Daily Planner Preview — Open in external browser
- Project id
- Fixture root
- Dashboard backlog, timeline, and prompt queue links

Do not imply Codex can keep both pages visible or controllable at once.
```

## 2. Give The First Request

```text
Requirement 1: I want to mark one task as Today Focus and show it at the top
of the planner. Please clarify the scope, create one backlog item after I
confirm, and tell me what files or areas the worker would be allowed to touch.
```

## 3. Confirm The First Request

```text
Confirmed. Write the Today Focus backlog row and stop. Do not implement yet.
```

## 4. Give The Second Request

```text
Requirement 2: each task can have a reminder toggle, but reminders should be
off by default. Please clarify it, check whether it overlaps the first request,
then create a second backlog row after I confirm.
```

## 5. Confirm The Second Request

```text
Confirmed. Write the reminder toggle backlog row and stop. Do not implement
yet.
```

## 6. Ask The Observer To Start Work

```text
Start implementation for the two confirmed planner requirements. Keep this
session as the observer. Dispatch compatible work in parallel where safe, but
land commits serially. Show me the timeline or backlog event that says work has
started.
```

## 7. Add A Mid-Implementation Requirement

```text
While that is in progress, I also want a quick capture input so I can add a
task without choosing a time. Please decide whether that should be folded into
active work, added as a follow-up, or kept separate.
```

## 8. Ask For The Decision

```text
Show me the observer decision for the quick capture request, the reason for
that decision, and where it appears in the dashboard.
```

## 9. Ask For The Evidence Summary

```text
Summarize the Vibe Queue demo evidence: both original requests, the mid-run
requirement, observer decision, worker state, serial commits, dashboard links,
and any limitations.
```
