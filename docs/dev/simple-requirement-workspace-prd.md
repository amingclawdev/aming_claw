# Simple Requirement Workspace PRD

Status: V1 implementation contract, 2026-05-28

Backlog: OBSERVER-COMMAND-QUEUE-SESSION-TOKEN-V1-20260528

## Product Goal

Simple Mode gives ordinary users one workspace for turning a rough request into
tracked work without exposing graph, backlog, or worker internals by default.
The dashboard records user intent, governance stores command payloads durably,
and a registered observer AI session pulls commands through an authenticated
queue.

## Three Tabs

Simple Mode has three top-level tabs:

1. Before development
2. In progress
3. Completed

Before development contains raw requirement capture, AI analysis results, user
confirmation, and the execution queue. In progress contains worker cards and
pause, continue, and cancel controls. Completed contains finished requirement
cards with commit evidence and an optional View audit drill-down.

Engineer Mode remains available for graph, backlog, Review Queue, Operations
Queue, and detailed audit work. Simple Mode is the default ordinary-user view,
not a replacement for existing governance surfaces.

## Observer Session Registration

An AI observer session must register before claiming observer-only commands.
Registration writes an `observer_sessions` row and returns:

- `observer_session_id`
- `session_token`
- `heartbeat_interval_sec`

The token is returned only at registration time. Governance stores only
`token_hash`, never the raw token.

Session fields:

- `session_id`
- `project_id`
- `observer_kind`
- `session_label`
- `pid`
- `cwd`
- `capabilities_json`
- `token_hash`
- `status`
- `registered_at`
- `last_seen_at`
- `closed_at`
- `revoked_at`

Computed status is derived from `status` and `last_seen_at`:

- `active`: recently heartbeated
- `idle`: heartbeat is late but not stale
- `stale`: heartbeat is too old for privileged command actions
- `closed`: session closed itself
- `revoked`: session token is no longer usable

Heartbeat is allowed for stale sessions with a valid token so a session can
recover. Command claim, complete, and fail are blocked for stale, closed, or
revoked sessions.

## Session Token and Capability Model

Sensitive observer actions validate all of these conditions:

- `project_id` matches the registered session
- `session_id` exists
- `session_token` hashes to the stored `token_hash`
- session is active enough for the action
- session capabilities allow the action
- for command actions, capabilities allow the command type

The server must not trust `actor="observer"` or any self-reported role as
authorization.

V1 capabilities are stored as JSON with:

- `actions`: e.g. `observer_command_claim`, `observer_command_complete`
- `command_types`: e.g. `analyze_requirements`, `pause_worker`

`*` may be used by trusted local sessions when all observer command actions or
command types are allowed.

## Observer Command Queue

Dashboard clicks write durable rows to `observer_command_queue`.

Command fields:

- `command_id`
- `project_id`
- `command_type`
- `payload_json`
- `status`
- `target_session_id`
- `claimed_by_session_id`
- `created_by`
- `created_at`
- `notified_at`
- `claimed_at`
- `completed_at`
- `result_json`
- `error`

V1 command types:

- `analyze_requirements`
- `confirm_requirement`
- `move_to_execution_queue`
- `pause_worker`
- `continue_worker`
- `cancel_worker`

V1 statuses:

- `queued`: stored and waiting for an observer
- `notified`: reminder was sent, payload still lives in governance DB
- `claimed`: a registered observer session owns it
- `running`: reserved for a future explicit running transition
- `completed`: observer wrote a result
- `failed`: observer wrote an error
- `cancelled`: command was cancelled before completion

Claiming is idempotent for the same session. A claimed command cannot be claimed
by another session. Complete and fail require the same `claimed_by_session_id`.

For `analyze_requirements`, V1 completion may project the raw requirement into
`needs_confirmation` with the AI interpretation and proposed backlog mapping
stored as command result and operator-facing note. Backlog creation still
requires explicit confirmation.

## Hook Reminder Semantics

Hooks and MCP notifications are reminder-only.

Allowed reminder payload:

```json
{
  "kind": "observer_command_pending",
  "project_id": "aming-claw",
  "message": "pending observer commands exist; call observer_command_next",
  "payload_included": false
}
```

The hook must not carry the business payload. The observer obtains payload by
calling `observer_command_next` or `observer_command_claim` with a valid
session token.

## API and MCP Contract

Governance API exposes:

- `POST /api/projects/{project_id}/observer-sessions/register`
- `GET /api/projects/{project_id}/observer-sessions`
- `POST /api/projects/{project_id}/observer-sessions/{session_id}/heartbeat`
- `POST /api/projects/{project_id}/observer-sessions/{session_id}/close`
- `POST /api/projects/{project_id}/observer-sessions/{session_id}/revoke`
- `POST /api/projects/{project_id}/observer-commands`
- `GET /api/projects/{project_id}/observer-commands`
- `POST /api/projects/{project_id}/observer-commands/next`
- `POST /api/projects/{project_id}/observer-commands/claim`
- `POST /api/projects/{project_id}/observer-commands/{command_id}/complete`
- `POST /api/projects/{project_id}/observer-commands/{command_id}/fail`

MCP exposes matching tools for registration, heartbeat, command list, next or
claim, complete, and fail. MCP may also expose enqueue for local smoke tests
and dashboard parity, but dashboard remains the primary producer.

## Non-Goals

- No direct chat injection into arbitrary Claude or Codex windows.
- No business payload in hook or notification messages.
- No removal of Engineer Mode.
- No automatic backlog row creation from raw capture.
- No trusted memory claim from AI output before governance validation and
  explicit operator confirmation.
