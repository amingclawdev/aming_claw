# Fear During Work

## Fear

The fear during work is that multiple agents will collide in the same checkout,
overwrite each other, or produce changes that cannot be reviewed in a sensible
order. Even a good single-agent patch becomes risky if the system cannot say
which branch, worktree, fence token, tests, and merge gate it belongs to.

## Demo

If you install the aming-claw Claude Code plugin and run the HN demo skill, the
during-work case opens your local dashboard on the backlog item view and shows a
manual-fix or subagent work item with bounded ownership, timeline lanes, and
evidence details: dispatch, implementation, verification, close-ready state,
actors, target files, and any inferred or blocked checkpoints.

If you don't want to install anything yet, the "What you would see" section
below describes the flow without setup.

Expected dashboard pattern (opens locally after the skill runs):

```text
http://localhost:40000/dashboard?project_id=<project_id>&view=backlog&backlog=<backlog_id>
```

## What you would see

The backlog modal shows a timeline DAG for one work item, grouped by worker
lanes and phase columns. Timeline cards expose dispatch, implementation, and
review-ready checkpoints, while the evidence inspector shows actor, phase,
status, commit, task id, and attempt metadata for the selected node. The point is
not that the agent wrote a confident final answer; the point is that its work is
attached to lane structure, phase transitions, and evidence records.

![During-work timeline](../screenshots/03-during-work-timeline.png)

*The timeline makes worker lanes, phases, and blocked checkpoints visible for
review.*

![During-work evidence inspector](../screenshots/04-during-work-evidence.png)

*Selecting a timeline node shows the evidence fields behind the visible card.*

This answers the during-work fear by making execution reviewable while agents
are still working, before a merge or close decision can hide the path they took.

## Evidence

*The visible evidence is what a human reviewer sees on the dashboard while
agents are executing. The agents are the operators -- they implement, verify, and
post evidence to the timeline. The human reads the lane structure and intervenes
at gate boundaries.*

The visible evidence is not "the agent said it was careful." It is durable
coordination state:

- a manual-fix backlog row with target files and acceptance criteria;
- timeline lanes that separate observer and worker actions;
- dispatch, implementation, verification, and close-ready checkpoints;
- evidence inspector details that show actor, phase, status, and artifacts.

The demo can use deterministic fixtures or dry-run evidence. It does not require
live AI execution to show the isolation and gate model.

## Why this works

Manual Fix keeps implementation bounded when the V1 chain is not the right tool
for the job. The parallel multibranch design extends that discipline to
multiple workers: branch-local evidence is candidate evidence, target graph truth
changes only after ordered merge and target reconcile, and stale fences are
rejected instead of trusted.

The important boundary is that the worker does not accept its own work. Dispatch,
implementation, verification, merge readiness, and backlog close are separate
state transitions. The contract, source head, dirty scope, and evidence timeline
make those transitions reviewable.

Observer mode also protects the developer's thinking time. The human can keep
discussing requirements and review boundaries while the agent turns them into
contracts and dispatches bounded workers in parallel.

The graph boundary is one-hop: branch/worktree graph artifacts are candidate
evidence against the target commit, not canonical project memory. Only the
target ref graph advances after ordered merge and target reconcile.

## A real instance

`HN-BACKLOG-TIMELINE-LANE-READABILITY-20260526` was filed when the backlog
timeline needed to make observer and subagent execution readable instead of
showing raw IDs as the primary lane names. It required human-readable lane
grouping, kept raw audit ids available in the evidence inspector, and landed
with the HN demo evidence work in commit
`dcb0f1f350218e224222af890ef6e1c1c6300f1d`. Parallel agent work is not
reviewable unless the lanes and evidence survive the UI.

Related dogfood story:

[I told my AI to build a feature. Did it? I had no
idea.](https://dev.to/amingin_ai/i-told-my-ai-to-build-a-feature-did-it-i-had-no-idea-1f1)

That post solved an upstream piece of this fear: **markdown is dead text, the
backlog database is live state**. A task isn't done because the agent says so --
it's done when the state machine transitions to `done(commit hash)` with the
commit automatically bound. That binding is what makes during-work evidence
reviewable, and what this case extends into multi-agent territory with worktree
fences and observer gates.

Architecture references:

- [During Work Architecture](../architecture/during-work-architecture.md)
- [Manual Fix SOP](../../governance/manual-fix-sop.md)
