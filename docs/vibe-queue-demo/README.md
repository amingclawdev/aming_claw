# Vibe Queue Demo

Vibe Queue is the everyday demo for a user who keeps talking while agents are
already working.

It shows that a current Claude Code or Codex session can act as the observer:
it keeps the backlog, worker lanes, new requirements, and dashboard evidence in
one place while work continues in bounded steps. Scripted runners are setup and
CI fallback only. They can create a fixture or smoke-test the protocol, but
they are not proof that a live AI observer handled the conversation.

The primary product under demo is **Daily Planner Lite**, not the Aming Claw
repo. Start by showing the planner fixture and preview URL, then keep the
governance dashboard visible as the control plane.

## What This Proves

- You can add a new requirement mid-implementation without losing the original
  work context.
- The observer can decide whether the new request belongs in the active work,
  a follow-up backlog row, or a separate item.
- Dashboard evidence shows the queue, timeline, graph state, and operations
  state instead of relying on a long chat transcript.
- Worker output remains bounded by target files and acceptance criteria.

## Two-Window Demo Setup

Use Codex's in-app browser for the Aming Claw dashboard. Open the planner
preview in your normal browser so you can watch the product update while Codex
keeps the governance cockpit visible.

Do not imply Codex can keep both pages visible or controllable at once. Use the
two links like this:

| Link | Where to open | Purpose |
| --- | --- | --- |
| Open Aming Claw Dashboard | Use in Codex | Backlog, timeline, prompt queue, graph, operations, and review state. |
| Open Daily Planner Preview | Open in external browser | The product page the user watches while requirements are queued and implemented. |

The fixture helper prints both links. If the planner preview server is not
already running, start it with the printed command, for example:

```bash
python3 -m http.server 4173 --directory <fixture_root>
```

Prefer the localhost planner preview URL over `file://` so browser-use and
normal browsers see the same app.

## Install And Run Prompt

After installing Aming Claw and reloading your Claude Code or Codex session,
send one message:

```text
Use this current Claude Code or Codex session as the observer for the Aming
Claw Vibe Queue demo.

/aming-claw:aming-claw-vibe-queue-demo

Set up an isolated fixture if needed. Do not treat any scripted runner as proof
that a live AI observer handled the queue. Show Daily Planner Lite as the target
project first. Use Codex's in-app browser for the Aming Claw dashboard, and
tell me to open the planner preview in my normal browser. Show me the backlog
item, dashboard links, preview link, and the point where I add a new
requirement while work is in progress.
```

## Fixture Setup Path

Fixture setup is separate from observer behavior.

Expected fixture helper path:

```bash
node frontend/dashboard/scripts/e2e-vibe-queue-fixture.mjs --no-browser
```

The fixture helper should only create or reset an isolated project and return a
`project_id`, dashboard link, planner preview URL, and preview-server command.
The observer session should still create or inspect backlog rows, record
timeline evidence, handle the mid-run requirement, and summarize what happened.

For CI or release smoke, a scripted runner may drive the fixture end to end, but
that result should be labeled as scripted verification, not live AI proof.

## Dashboard Surfaces To Inspect

| Surface | What to look for |
| --- | --- |
| Backlog | The original request, the new requirement, and the observer decision. |
| Timeline | Separate events for initial setup, worker activity, user interruption, and follow-up handling. |
| Graph | The project snapshot used when the observer scoped the work. |
| Operations Queue | Any graph update, reconcile, semantic, or review work that becomes pending. |
| Review Queue | Any proposed follow-up, stale evidence, or human decision that should not be silently accepted. |

Useful URL patterns:

```text
Open Aming Claw Dashboard (Use in Codex):
http://localhost:40000/dashboard?project_id=<project_id>&view=backlog

Open Daily Planner Preview (Open in external browser):
http://127.0.0.1:<preview_port>/

Keep these dashboard links available while the external preview is open:
http://localhost:40000/dashboard?project_id=<project_id>&view=backlog  (prompt queue/backlog)
http://localhost:40000/dashboard?project_id=<project_id>&view=timeline
http://localhost:40000/dashboard?project_id=<project_id>&view=graph
http://localhost:40000/dashboard?project_id=<project_id>&view=operations
http://localhost:40000/dashboard?project_id=<project_id>&view=review
```

## Expected Artifacts

- A demo project id, usually from an isolated fixture.
- A two-window setup summary with:
  - Open Aming Claw Dashboard — Use in Codex.
  - Open Daily Planner Preview — Open in external browser.
- One original backlog row for the first request.
- A second confirmed backlog row before implementation starts.
- One recorded mid-implementation requirement.
- A visible observer decision: merge into current work, add a follow-up, or
  keep separate.
- Timeline evidence for the user interruption and the observer response.
- Dashboard links or screenshots for backlog, timeline, graph, and operations.

## Honest Limitations

- The demo does not prove that every worker finished correctly. It proves that
  the observer keeps changing requirements visible and auditable.
- If the graph is stale, the dashboard should say so. The observer should not
  pretend stale graph state is current.
- Scripted fixture setup is useful, but it is not the same thing as the current
  Claude Code or Codex session observing live work.
- Advanced chain/executor automation may be unavailable. The demo should still
  work as observer-led backlog and timeline evidence.
