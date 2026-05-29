# Observer Reminder Echo Demo

This demo is for a new user who wants to see Aming Claw notify an observer in
the normal local environment, then see that reminder carried into a subagent
contract as explicit evidence.

The point is the real reminder path:

1. A command is enqueued for the observer.
2. Governance emits a small hook reminder.
3. The observer displays that reminder.
4. The observer claims the command and only then sees the command payload.
5. The subagent contract writes the reminder into `received_reminder_echo`.

Docker is only a validation harness for the fixture and docs. It is not the
user entrypoint and it does not call a model.

## Files

- `docs/fixtures/observer-reminder-echo-demo.json` is the prompt and fixture
  data for the local observer run.
- `docs/demos/observer-reminder-echo.md` is this new-user walkthrough.
- `docker/observer-reminder-echo-demo.Dockerfile` checks that the fixture and
  docs keep the boundary clear.
- `agent/governance/observer_session.py` defines the reminder shape.
- `agent/governance/server.py` publishes the reminder when a command is
  enqueued.
- `agent/tests/test_observer_command_queue.py` already covers the payload
  boundary for observer command reminders.

## Run Locally

Start from a normal local Aming Claw checkout with the plugin loaded.

```bash
aming-claw launcher
aming-claw start
```

Open the dashboard if you want to watch the backlog and timeline while the demo
runs:

```text
http://localhost:40000/dashboard?project_id=aming-claw&view=backlog&backlog=AC-DEMO-OBSERVER-REMINDER-ECHO-20260529
```

In a Codex observer session, paste this prompt:

```text
Use docs/fixtures/observer-reminder-echo-demo.json. Run the real local observer
command flow. Display the hook reminder you receive exactly, then claim the
command and show that the business payload appears only after claim. Do not call
a proof script. Do not expose session_token in the final summary.
```

The observer should use the fixture steps:

1. Register an observer session with `observer_session_register`.
2. Enqueue the demo command with `observer_command_enqueue`.
3. Display the returned `hook_reminder` exactly.
4. Check that the hook reminder has no business payload fields.
5. Claim the command with `observer_command_next`.
6. Show the claimed command id, command type, and payload keys.
7. Dispatch or simulate the subagent contract with the reminder echo addendum.
8. Record timeline evidence against
   `AC-DEMO-OBSERVER-REMINDER-ECHO-20260529`.

## Expected Reminder

The observer should display this reminder after enqueue:

```json
{
  "kind": "observer_command_pending",
  "project_id": "aming-claw",
  "message": "pending observer commands exist; call observer_command_next",
  "payload_included": false,
  "next_action": {
    "tool": "observer_command_next",
    "description": "claim the next pending observer command"
  }
}
```

This reminder must not contain `payload`, `command_type`, `demo_id`,
`request_summary`, `requested_worker_lane`,
`business_payload_should_not_appear_in_hook`, or `session_token`.

After the observer claims the command, the command payload can be shown as
payload keys. The payload is deliberately visible only after authenticated
claim, not in the hook reminder.

## Subagent Contract Echo

The subagent contract should include this addendum:

```text
Before doing any other work, write the exact hook reminder you received into
received_reminder_echo. Use the same JSON shape and values. Then continue with
your owned files. Do not include session_token or the claimed business payload
inside received_reminder_echo.
```

Expected worker evidence:

```json
{
  "received_reminder_echo": {
    "kind": "observer_command_pending",
    "project_id": "aming-claw",
    "message": "pending observer commands exist; call observer_command_next",
    "payload_included": false,
    "next_action": {
      "tool": "observer_command_next",
      "description": "claim the next pending observer command"
    }
  },
  "claimed_command_summary": {
    "command_type": "analyze_requirements",
    "payload_keys": [
      "demo_id",
      "request_summary",
      "requested_worker_lane",
      "business_payload_should_not_appear_in_hook"
    ]
  }
}
```

The echo proves what reminder the worker was told the observer received. It is
not a place for session tokens or command payload contents.

## Where To See Evidence

Backlog:

```text
http://localhost:40000/dashboard?project_id=aming-claw&view=backlog&backlog=AC-DEMO-OBSERVER-REMINDER-ECHO-20260529
```

The backlog row should list the planned files and acceptance criteria for the
demo. The row is the starting point for a new user who wants to know what work
is being demonstrated.

Timeline:

```text
task_timeline_list(project_id="aming-claw", backlog_id="AC-DEMO-OBSERVER-REMINDER-ECHO-20260529")
```

The timeline should contain implementation or verification evidence with the
hook reminder, the subagent `received_reminder_echo`, and the payload-boundary
check.

Files:

```text
docs/fixtures/observer-reminder-echo-demo.json
docs/demos/observer-reminder-echo.md
docker/observer-reminder-echo-demo.Dockerfile
agent/governance/observer_session.py
agent/governance/service_registry.py
agent/governance/contract_templates/observer_reminder_echo_demo.v1.json
agent/governance/server.py
agent/tests/test_observer_command_queue.py
agent/tests/test_service_router.py
agent/tests/test_contract_template_registry.py
agent/tests/test_task_timeline.py
```

Change record:

```bash
git show --name-only --format='%H%n%s%n%aI' <commit>
git show --stat <commit>
```

After merge and graph update, the dashboard graph/assets views or graph path
lookup should show the file inventory. New docs, fixtures, and Docker assets
may first appear as unbound assets; that is expected until an accepted binding
or governance hint ties them to a graph node.

## Docker Validation

Run this only to validate the fixture and documentation boundary:

```bash
docker build -f docker/observer-reminder-echo-demo.Dockerfile .
```

The Docker build checks that:

- the fixture JSON parses;
- the fixture contains observer prompt steps, expected reminder, subagent echo,
  file-location checks, and change-record checks;
- the expected hook reminder is payload-free;
- this document says Docker is verification only.

It does not start governance, call MCP tools, call Codex or Claude, or run live
AI. The real demo is the local observer prompt flow above.

## Troubleshooting

If `/api/health` works but `/dashboard` does not, dashboard static assets may
be missing. Build the dashboard from `frontend/dashboard` or use the MCP tools
directly.

If the hook reminder includes command payload fields, treat it as a bug. The
expected reminder keys are only `kind`, `project_id`, `message`,
`payload_included`, and `next_action`.

If the file does not appear as a graph-bound node immediately after merge, check
the file inventory or asset inbox first. Source-controlled docs and fixtures may
need a graph update and accepted binding before they become trusted node
coverage.
