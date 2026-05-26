---
name: aming-claw-hn-demo-after-work
description: HN demo case for the fear that code changes leave docs, tests, and config stale after implementation. Guides evidence collection for Asset Inbox, binding state, Baseline and Possible drift, Review Queue, impact scope, and review boundaries.
---

# HN Demo: After Work

Show how Aming Claw keeps docs, tests, and config from becoming invisible
collateral damage after code changes.

## Fear

Code changes land, but related docs, tests, or config are stale, orphaned, or
accepted as true without review.

## Evidence To Collect

- Asset Inbox: changed, orphaned, candidate, or bound doc/test/config assets.
- Binding state: accepted, candidate, orphan, unbound, or source-controlled
  hint state.
- Baseline/Possible drift: whether related assets are known clean, suspected,
  or pending impact review.
- Review Queue: proposal review boundary for AI or weak-evidence changes.

## Architecture Reason

- Asset inventory records docs/tests/config as commit-bound project assets.
- Binding projection separates candidate relationships from trusted graph
  ownership.
- Impact scope flags related assets that may need review after source changes.
- Drift status distinguishes baseline, suspected, possible, and resolved
  states.
- Review boundary keeps weak AI or path evidence out of graph truth until
  accepted.

## Operator Steps

1. Check governance and dashboard status. If governance is offline, instruct
   the user to run `aming-claw start`; do not start it silently.
2. Open Asset Inbox for the project and active graph snapshot.
3. Filter or inspect docs, tests, and config related to the demo change.
4. Identify binding state and drift status. Do not treat candidate or weak path
   matches as trusted graph ownership.
5. Open Review Queue for pending binding, unbind, semantic, or impact review
   proposals.
6. Capture screenshots or links for Asset Inbox, binding details, drift status,
   and Review Queue.

## Evidence Summary

```text
After-work evidence
- Fear: docs/tests/config become stale after code changes
- Asset Inbox: <link/screenshot/asset ids>
- Binding state: <accepted/candidate/orphan/unbound/hint evidence>
- Drift: <baseline/possible/suspected/impact_pending/resolved evidence>
- Review Queue: <link/screenshot/proposal ids>
- Architecture reason: asset inventory + binding projection + impact scope + drift status + review boundary
- Limitations: <none/offline dashboard/manual screenshot/etc>
```
