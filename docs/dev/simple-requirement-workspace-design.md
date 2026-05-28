# Simple Requirement Workspace Design

Status: High-fidelity written design, 2026-05-28

Backlog: OBSERVER-COMMAND-QUEUE-SESSION-TOKEN-V1-20260528

## Design Principle

Simple Mode is a requirement lifecycle, not a graph console. Ordinary users see
captured requests, AI interpretation, execution state, and completion evidence.
Graph, backlog, Review Queue, and raw audit details remain one click deeper in
Engineer Mode or View audit.

## Layout

The first screen is the workspace itself. It uses a dense operational layout:

- Left/top project selector inherited from the existing dashboard shell.
- Main tab bar with Before development, In progress, Completed.
- A compact observer status strip near the tab bar.
- Content area for requirement cards, execution queue, worker cards, and
  completed cards.

There is no landing page, no marketing hero, and no graph by default.

## Observer Status Strip

The status strip shows one of:

- Connected: at least one active or idle observer session exists.
- Waiting for observer: no connected session exists.
- Command queued: dashboard wrote a command and is waiting for claim.
- Running: a command is claimed or running.
- Completed: last command completed.
- Failed: last command failed and exposes retry or View audit.

The strip must not pretend that AI has run because a button was clicked. Button
clicks create queue rows. AI work begins only when a registered observer claims
the row.

## Tab 1: Before Development

Before development contains the full intake and confirmation path.

Primary sections:

- Large requirement input.
- Raw requirement cards.
- AI interpretation and proposed backlog mapping.
- Execution queue.

The requirement input captures the user's exact words. Submitting creates a
raw requirement row only. It does not create a backlog row, query the graph, or
dispatch a worker.

Raw requirement cards show:

- original request excerpt
- capture time
- status: unconfirmed or confirmed
- latest command state when an AI analysis command exists
- AI Analyze action
- Confirm action after analysis or manual review

AI Analyze enqueues `analyze_requirements` with payload:

```json
{
  "raw_id": "raw-...",
  "source": "project_inbox"
}
```

The dashboard then refreshes command state from governance. It does not send
the payload through a hook and does not inject a message into an open chat.

### Detail Modal

Opening a raw requirement card shows:

- Original request
- AI interpretation
- Proposed backlog mapping
- Suggested acceptance criteria
- Risk or missing context
- Command history summary
- Optional View audit link

The modal is the first place where richer detail appears. The card itself stays
scan-friendly.

### Execution Queue

Confirmed requirements may be moved to the execution queue. This enqueues
`move_to_execution_queue`. The queue card shows:

- requirement title or concise interpretation
- confirmation time
- target backlog id if one exists
- queued command status
- disabled state when no observer is connected

Backlog creation or update remains explicit and auditable.

## Tab 2: In Progress

In progress shows worker cards. Each worker card is tied to a requirement or
backlog row.

Worker card fields:

- requirement title
- current state
- assigned worker/session
- branch or worktree when available
- latest checkpoint or test signal
- pause, continue, cancel controls
- View audit

Controls enqueue commands:

- Pause: `pause_worker`
- Continue: `continue_worker`
- Cancel: `cancel_worker`

Button states:

- Enabled when observer connected and target worker is controllable.
- Queued after command enqueue.
- Running after observer claim.
- Completed after observer writes result.
- Failed when observer writes error.

The UI never assumes the worker paused or cancelled until governance state
confirms the result.

## Tab 3: Completed

Completed cards show finished requirements.

Card fields:

- final requirement title
- original request excerpt
- commit short hash
- completion time
- outcome summary
- View audit

View audit opens Engineer Mode context or a modal with timeline evidence,
backlog id, commit id, command ids, and verification evidence. It is optional
for ordinary users and available for operators.

## Command State Copy

Use compact operational labels:

- Waiting for observer
- Queued
- Running
- Completed
- Failed

Avoid hidden AI promises such as "Analyzing..." until a command is claimed.
When a command is only queued, the UI must say queued.

## Empty and Error States

Before development empty state: input remains primary, with no graph language.

Waiting for observer: show raw capture and manual confirmation, but disable
AI Analyze and worker control actions that require observer execution.

Command failure: keep the requirement card in place, show Failed, and offer
Retry and View audit.

Stale observer: show Waiting for observer unless another active or idle
session exists.

## Engineer Mode Boundary

Engineer Mode is still present and unchanged. Simple Mode links to it through
View audit, backlog ids, commit ids, and optional advanced controls. The
ordinary path should not require graph concepts to capture, confirm, queue,
pause, continue, cancel, or review completed work.
