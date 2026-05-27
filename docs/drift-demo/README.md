# Docs Drift Demo

Docs Drift is the everyday demo for a feature change that leaves documentation
behind.

It shows that a current Claude Code or Codex session can act as the observer:
it notices when code changes and docs no longer match, keeps that drift visible,
and guides a second fix instead of silently treating the first change as done.
Scripted runners are setup and CI fallback only. They can create a fixture or
smoke-test dashboard behavior, but they are not proof that a live AI observer
noticed drift.

## What This Proves

- A feature change can pass implementation checks while docs are still stale.
- The stale doc state is surfaced as review, asset, operations, or backlog
  evidence instead of being buried in chat.
- A second doc-focused change can clear or reduce the drift state.
- The observer separates "feature implemented" from "project knowledge is
  current."

## Install And Run Prompt

After installing Aming Claw and reloading your Claude Code or Codex session,
send one message:

```text
Use this current Claude Code or Codex session as the observer for the Aming
Claw Docs Drift demo.

/aming-claw:aming-claw-drift-demo

Set up an isolated fixture if needed. Do not treat any scripted runner as proof
that a live AI observer noticed documentation drift. Show me the stale-doc state
after the first change and the cleared or improved state after the doc fix.
```

## Fixture Setup Path

Fixture setup is separate from observer behavior.

Expected fixture helper path:

```bash
node frontend/dashboard/scripts/e2e-drift-demo-fixture.mjs --no-browser
```

The fixture helper should only create or reset an isolated project with a small
feature and matching docs. The observer session should still run the first
feature change, inspect the stale docs signal, guide the doc fix, and summarize
the dashboard evidence.

For CI or release smoke, a scripted runner may perform the same two-step story,
but that result should be labeled as scripted verification, not live AI proof.

## Dashboard Surfaces To Inspect

| Surface | What to look for |
| --- | --- |
| Graph | The commit-bound snapshot and whether graph state is stale after changes. |
| Asset Inbox | Documentation, test, or config assets that need binding or review. |
| Review Queue | Proposed drift, stale docs, or semantic review items that need acceptance. |
| Operations Queue | Scope reconcile, semantic jobs, or graph update work after code and docs change. |
| Backlog | The feature change row and the doc-fix row or follow-up decision. |

Useful URL patterns:

```text
http://localhost:40000/dashboard?project_id=<project_id>&view=graph
http://localhost:40000/dashboard?project_id=<project_id>&view=assets
http://localhost:40000/dashboard?project_id=<project_id>&view=review
http://localhost:40000/dashboard?project_id=<project_id>&view=operations
http://localhost:40000/dashboard?project_id=<project_id>&view=backlog
```

## Expected Artifacts

- A demo project id, usually from an isolated fixture.
- One feature-change backlog row or timeline event.
- A first change where code behavior changes and docs are intentionally stale.
- A visible stale-doc or drift signal in dashboard evidence.
- A second doc-fix prompt and resulting doc update evidence.
- Dashboard links or screenshots for graph, asset/review state, operations, and
  backlog.

## Honest Limitations

- The demo is about surfacing drift, not automatically proving every doc is
  correct.
- Some doc relationships may appear as proposals until the user accepts them or
  source-controlled hints bind them.
- If the graph is stale, the observer should say that plainly and avoid
  overclaiming current graph truth.
- Scripted fixture setup can produce repeatable evidence, but live observer
  behavior must come from the current Claude Code or Codex session.
