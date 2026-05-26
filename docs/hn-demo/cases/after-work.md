# Fear After Work

## Fear

The fear after work is that the agent leaves behind a patch and the project
forgets what happened. Docs may be unbound, tests may be stale, generated files
may look like source, and the next agent may reason from old graph memory.

## Demo

Run the HN demo skill and choose the after-work case. The demo should show the
post-change hygiene path: Asset Inbox or file state for changed docs/tests/config
assets, Operations Queue or reconcile status, and any Review Queue item that
must be accepted before weak evidence becomes trusted memory.

Expected dashboard pattern:

```text
http://localhost:40000/dashboard?project_id=<project_id>&view=assets
```

Supporting pattern:

```text
http://localhost:40000/dashboard?project_id=<project_id>&view=operations
```

Optional screenshot slots:

```text
docs/hn-demo/screenshots/05-after-work-asset-inbox.png
docs/hn-demo/screenshots/06-after-work-reconcile.png
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

Architecture references:

- [Asset Inbox API Contract](../../api/asset-inbox-contract.md)
- [Reconcile Workflow](../../governance/reconcile-workflow.md)
- [Manual Fix SOP](../../governance/manual-fix-sop.md)
