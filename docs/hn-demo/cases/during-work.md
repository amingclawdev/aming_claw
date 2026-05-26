# Fear During Work

## Fear

The fear during work is that multiple agents will collide in the same checkout,
overwrite each other, or produce changes that cannot be reviewed in a sensible
order. Even a good single-agent patch becomes risky if the system cannot say
which branch, worktree, fence token, tests, and merge gate it belongs to.

## Demo

Run the HN demo skill and choose the during-work case. The demo should show a
manual-fix or subagent work item with bounded ownership, timeline lanes, and
evidence details: dispatch, implementation, verification, close-ready state,
actors, target files, and any inferred or blocked checkpoints.

Expected dashboard pattern:

```text
http://localhost:40000/dashboard?project_id=<project_id>&view=backlog&backlog=<backlog_id>
```

Optional screenshot slots:

```text
docs/hn-demo/screenshots/03-during-work-timeline.png
docs/hn-demo/screenshots/04-during-work-evidence.png
```

## Evidence

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

Related dogfood story:

[I told my AI to build a feature. Did it? I had no
idea.](https://dev.to/amingin_ai/i-told-my-ai-to-build-a-feature-did-it-i-had-no-idea-1f1)

That post is the earlier backlog/state-machine case behind this fear: a task is
not done because the agent says so; it needs status, commit evidence, and a
queryable ledger that both the human and AI can read.

Architecture references:

- [Manual Fix SOP](../../governance/manual-fix-sop.md)
- [Parallel Agent Multibranch Runtime Design](../../dev/parallel-agent-multibranch-design.md)
- [Parallel Agent Multibranch Test Scenarios](../../dev/parallel-agent-multibranch-test-scenarios.md)
