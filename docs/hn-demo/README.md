# HN Fear Demo

This demo is framed around three ordinary fears people have when they let an
AI coding agent touch a real project:

1. **Before work:** will the agent understand the project before it edits?
2. **During work:** can parallel agent work stay isolated and reviewable?
3. **After work:** can the project remember what changed and catch drift later?

The demo is intentionally not just a set of UI screenshots. Each visible state
maps back to a governance layer: a commit-bound graph, a backlog/MF ledger,
branch/worktree fences, merge-queue evidence, asset inbox state, and reconcile.
Live AI execution is not required to understand or replay the article flow; the
HN demo skill guides deterministic setup where possible and points to the exact
dashboard surfaces to inspect.

Main article draft:
[Hope is not an engineering control for AI coding agents](article.md).

## Installed-User Prompt

After installing the Aming Claw plugin, ask Codex or Claude:

```text
Use the Aming Claw HN demo skill to run the three fear cases for this project:
before work, during work, and after work. If needed, create an isolated fixture
project, but produce the backlog rows, timeline events, graph traces, worker
fences, tests, and reconcile evidence during this run. Do not treat pre-existing
fixture data as proof. Show me the dashboard URLs for each case.
```

The skill should leave you with dashboard states that correspond to the three
case pages:

If this is your first run and governance has no registered project yet, the demo
uses an isolated local fixture instead of asking you for a `project_id`. The
fixture is created under the OS temp directory, bootstrapped as
`aming-claw-hn-demo`, and left with an active graph plus empty backlog/timeline
so the observer has to create evidence for real. Your real app is not touched.
The first-run runner is packaged with the plugin at
`frontend/dashboard/scripts/e2e-hn-demo.mjs`, so the `--no-browser` setup path
does not require a dashboard npm install.

For launch rehearsal, use the repeatable sandbox audit runner:

```bash
node frontend/dashboard/scripts/e2e-hn-demo.mjs --sandbox-audit --no-browser
```

That path creates an isolated fixture project, runs install/package smoke
checks, drives the three demo fears through real governance calls, and writes
`docs/hn-demo/audits/latest.md` plus `latest.json`. Browser capture is optional:
add `--browser --port <port> --project-id <isolated-id>` to record screenshots
against a non-conflicting dashboard session.

For full one-click install E2E, run the Docker host lanes first:

```bash
docker/hn-install-audit/run-install-audit.sh --host both
```

Those lanes build separate Codex and Claude Code containers, reuse host auth
read-only at runtime, feed the README install prompt, then feed the HN demo
prompt. This is a release gate, not a user install requirement. Their JSON
reports can be passed back into `--sandbox-audit` with `--require-install-gates`.

| Case | Page | Architecture note | Dashboard URL pattern |
| --- | --- | --- | --- |
| Before work | [Fear Before Work](cases/before-work.md) | [Before Work Architecture](architecture/before-work-architecture.md) | `http://localhost:40000/dashboard?project_id=<project_id>&view=graph` |
| During work | [Fear During Work](cases/during-work.md) | [During Work Architecture](architecture/during-work-architecture.md) | `http://localhost:40000/dashboard?project_id=<project_id>&view=backlog&backlog=<backlog_id>` |
| After work | [Fear After Work](cases/after-work.md) | [After Work Architecture](architecture/after-work-architecture.md) | `http://localhost:40000/dashboard?project_id=<project_id>&view=assets` |

Each case also has a longer dogfood writeup:

| Case | Deeper story |
| --- | --- |
| Before work | [AI proposed 5 components for my parallel system. After walking one scenario, only 3 were real.](https://dev.to/amingin_ai/ai-proposed-5-components-for-my-parallel-system-after-walking-one-scenario-only-3-were-real-12nd) |
| During work | [I told my AI to build a feature. Did it? I had no idea.](https://dev.to/amingin_ai/i-told-my-ai-to-build-a-feature-did-it-i-had-no-idea-1f1) |
| After work | [AI's tech debt is invisible - even to AI. I solved it at the architecture layer.](https://dev.to/amingin_ai/ais-tech-debt-is-invisible-even-to-ai-i-solved-it-at-the-architecture-layer-1nh1) |

Useful supporting dashboard patterns after the skill runs:

- `http://localhost:40000/dashboard?project_id=<project_id>&view=operations`
- `http://localhost:40000/dashboard?project_id=<project_id>&view=review`
- `http://localhost:40000/dashboard?project_id=<project_id>&view=projects`

## No-Install Screenshot Path

HN readers who do not install the plugin can still follow the story with the
stable screenshot slots below. Article images should be exported under
`docs/hn-demo/screenshots/` with these filenames:

| Order | Filename | What it should show |
| --- | --- | --- |
| 01 | `docs/hn-demo/screenshots/01-before-work-contract.png` | Backlog contract, target files, docs, tests, and close-gate evidence before editing. |
| 02 | `docs/hn-demo/screenshots/02-before-work-graph.png` | Project graph, selected node, graph health, and stale/current state before editing. |
| 03 | `docs/hn-demo/screenshots/03-during-work-timeline.png` | Timeline DAG with observer + worker lanes, phase checkpoints, and evidence inspector. The bundled screenshot shows three workers; two is the minimum useful parallel demo. |
| 04 | `docs/hn-demo/screenshots/04-during-work-evidence.png` | Timeline evidence inspector with event details, actor lane, phase, and status. |
| 05 | `docs/hn-demo/screenshots/05-after-work-asset-inbox.png` | Asset Inbox state for docs, tests, config, bindings, and drift. |
| 06 | `docs/hn-demo/screenshots/06-after-work-review-queue.png` | Review Queue boundary for reminders, proposals, and impact review. |

The screenshots are optional evidence for the article. The case pages remain
readable without them because they describe the fear, demo action, evidence, and
architecture layer directly.

## Architecture Map

- The before-work case is about the local graph-first control plane described in
  [Before Work Architecture](architecture/before-work-architecture.md),
  [System Architecture](../architecture.md), and the scoped work ledger
  described in [Manual Fix SOP](../governance/manual-fix-sop.md).
- The during-work case is about observer-led parallel work, contract-bound
  dispatch, timeline evidence, merge authority, and one-hop branch graph policy,
  covered by [During Work Architecture](architecture/during-work-architecture.md)
  and [Manual Fix SOP](../governance/manual-fix-sop.md).
- The after-work case is about asset review and reconcile, covered by
  [After Work Architecture](architecture/after-work-architecture.md),
  [Asset Inbox API Contract](../api/asset-inbox-contract.md),
  [Reconcile Workflow](../governance/reconcile-workflow.md), and
  [Manual Fix SOP](../governance/manual-fix-sop.md).
