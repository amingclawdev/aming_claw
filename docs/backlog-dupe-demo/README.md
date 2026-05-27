# Backlog Duplicate Demo

Backlog Duplicate is the everyday demo for a user request that sounds a lot
like work already in the backlog.

It shows that a current Claude Code or Codex session can act as the observer:
it checks for similar backlog work, explains the overlap in ordinary language,
and asks the user whether to merge, supersede, or keep the request separate.
Scripted runners are setup and CI fallback only. They can seed a fixture with
overlapping backlog rows, but they are not proof that a live AI observer made a
good product decision.

## What This Proves

- New requirements are checked against existing backlog work before creating a
  duplicate row.
- The observer asks for a user choice when overlap is real.
- Merge, supersede, and separate decisions leave visible backlog evidence.
- The dashboard can show why a requirement was not blindly added as a new item.

## Install And Run Prompt

After installing Aming Claw and reloading your Claude Code or Codex session,
send one message:

```text
Use this current Claude Code or Codex session as the observer for the Aming
Claw Backlog Duplicate demo.

/aming-claw:aming-claw-backlog-dupe-demo

Set up an isolated fixture if needed. Do not treat any scripted runner as proof
that a live AI observer made the backlog decision. Show me the similar backlog
items and ask me whether to merge, supersede, or keep the new request separate.
```

## Fixture Setup Path

Fixture setup is separate from observer behavior.

Expected fixture helper path:

```bash
node frontend/dashboard/scripts/e2e-backlog-dupe-fixture.mjs --no-browser
```

The fixture helper should only create or reset an isolated project with one or
more existing backlog rows. The observer session should still accept the new
request, run the duplicate/overlap check, ask for the user decision, update the
backlog, and summarize the result.

For CI or release smoke, a scripted runner may exercise all three decisions,
but that result should be labeled as scripted verification, not live AI proof.

## Dashboard Surfaces To Inspect

| Surface | What to look for |
| --- | --- |
| Backlog | Existing similar work, the new request, and the final user decision. |
| Timeline | Evidence that the observer asked before creating or changing work. |
| Graph | Target files or feature area used to compare the requests. |
| Review Queue | Any proposed merge/supersede/separate recommendation that needs user approval. |
| Operations Queue | Any graph or semantic work caused by the accepted decision. |

Useful URL patterns:

```text
http://localhost:40000/dashboard?project_id=<project_id>&view=backlog
http://localhost:40000/dashboard?project_id=<project_id>&view=graph
http://localhost:40000/dashboard?project_id=<project_id>&view=review
http://localhost:40000/dashboard?project_id=<project_id>&view=operations
```

## Expected Artifacts

- A demo project id, usually from an isolated fixture.
- At least one existing backlog row that overlaps the new request.
- The new user request captured in the observer conversation.
- An overlap summary in plain language.
- A user choice: merge, supersede, or separate.
- A backlog update or new row that matches the user's choice.
- Dashboard links or screenshots for backlog, timeline, graph, and review.

## Honest Limitations

- The demo does not prove that semantic similarity is perfect. It proves that
  similar work is surfaced before adding duplicate backlog.
- The user remains the decision maker when merge/supersede/separate is a
  product judgment.
- Scripted fixture setup can seed useful examples, but it cannot replace the
  current Claude Code or Codex session asking the user.
- If the graph or semantic state is stale, the observer should say so and treat
  similarity results as review evidence, not final truth.
