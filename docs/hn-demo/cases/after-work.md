# Fear After Work

## Fear

The fear after work is that the agent leaves behind a patch and the project
forgets what happened. Docs may be unbound, tests may be stale, generated files
may look like source, and the next agent may reason from old graph memory.

## Demo

Run the HN demo skill and choose the after-work case. The demo should show the
post-change hygiene path: Asset Inbox or file state for changed docs/tests/config
assets and Review Queue boundaries for reminders, proposals, and impact review
before weak evidence becomes trusted memory.

Expected dashboard pattern:

```text
http://localhost:40000/dashboard?project_id=<project_id>&view=assets
```

Supporting pattern:

```text
http://localhost:40000/dashboard?project_id=<project_id>&view=operations
http://localhost:40000/dashboard?project_id=<project_id>&view=review
```

Optional screenshot slots:

```text
docs/hn-demo/screenshots/05-after-work-asset-inbox.png
docs/hn-demo/screenshots/06-after-work-review-queue.png
```

## Evidence

The visible evidence is the post-work audit layer:

- Asset Inbox separates unbound docs/tests/config, weak candidates, accepted
  bindings, ignored assets, archives, and stale mapped files;
- weak AI or rule matches are proposals, not trusted graph state;
- reconcile updates commit-bound graph and semantic projections after source is
  committed;
- Manual Fix evidence records implementation and verification before close.

This case can run without live AI. AI proposals are optional; deterministic
asset state, reconcile status, and review gates are enough to show the control
plane.

## Why this works

Aming Claw separates source records from derived projections. Accepted bindings,
source-controlled hints, committed files, and review decisions are durable
inputs; Asset Inbox rows, graph snapshots, semantic projections, and operations
queue state are derived views. That separation lets the dashboard explain what
is trusted, what is only a candidate, and what must be reconciled before the
next agent treats it as project memory.

A changed doc first becomes a commit-bound asset with status and provenance. It
becomes graph impact scope only after a reviewed binding, not because an AI or
path heuristic guessed it belonged there.

Related dogfood story:

[AI's tech debt is invisible - even to AI. I solved it at the architecture
layer.](https://dev.to/amingin_ai/ais-tech-debt-is-invisible-even-to-ai-i-solved-it-at-the-architecture-layer-1nh1)

That post is the earlier graph-memory case behind this fear: after an AI change
lands, the project graph must be marked stale and reconciled before the next
agent treats old structure as truth. This case extends that idea to docs, tests,
config, weak bindings, and review impact scope.

Architecture references:

- [Asset Inbox API Contract](../../api/asset-inbox-contract.md)
- [Reconcile Workflow](../../governance/reconcile-workflow.md)
- [Manual Fix SOP](../../governance/manual-fix-sop.md)
