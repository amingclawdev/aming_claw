# HN Multi-Agent Challenge Demo

This is the runnable demo for the HN article's main claim: one observer can
coordinate multiple contracted workers against the same commit-bound project
graph without the user writing orchestration code.

The challenge case is intentionally narrow:

1. Worker A and Worker B receive contracts bound to the same commit.
2. Worker A passes and its diff is accepted as candidate evidence.
3. Worker B fails or is interrupted.
4. The observer replays Worker B from the same contract lineage and frozen
   commit.
5. The replay passes with a clean, disjoint diff.
6. Accepted work lands through an ordered Git merge, then the target graph is
   reconciled once.

Live AI execution is not required to reproduce the protocol. The default path
uses scripted workers so the observer, contracts, fences, graph traces, failed
attempt, replay, test evidence, and self-evaluation are generated locally and
repeatably.

HN entry article:
[Show HN: Aming Claw - A new multi-agent coding architecture (zero
orchestration, commit-bound)](article.md).

More cases, audit trails, and the longer design story:
[Hope is not an engineering control for AI coding agents](design-story.md).

## Install

For Codex, ask for the one-shot path:

```text
One-shot install and open dashboard for Aming Claw from https://github.com/amingclawdev/aming-claw
```

For Claude Code, paste this once:

```text
Install aming-claw end-to-end from https://github.com/amingclawdev/aming-claw:
1. Run `/plugin marketplace add https://github.com/amingclawdev/aming-claw`
2. Run `/plugin install aming-claw@aming-claw-local`
3. pip install -e the marketplace clone at
   ~/.claude/plugins/marketplaces/aming-claw-local
   (Windows: %USERPROFILE%\.claude\plugins\marketplaces\aming-claw-local)
4. Start `aming-claw start` in a background terminal
5. Run `aming-claw open` to launch the dashboard
6. Remind me to reload Claude Code so the plugin's MCP tools and skills load
```

After install, reload Codex or open a new Claude Code session so the Aming Claw
skills and MCP tools are loaded before running the demo prompt below.

## Installed-User Prompt

After installing the Aming Claw plugin, ask Codex or Claude:

```text
Use the Aming Claw HN demo skill to run the multi-agent challenge demo. If
needed, create an isolated fixture project, but produce the backlog rows,
timeline events, graph traces, worker fences, tests, replay evidence, reconcile
evidence, and semantic evaluation during this run. Do not treat pre-existing
fixture data as proof. Show one passing worker, one failed or interrupted
worker, and a replay attempt that passes from the same contract evidence. Show
me the dashboard URLs and the generated audit report.
```

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
checks, drives the multi-agent challenge through real governance calls, and
writes `docs/hn-demo/audits/latest.md` plus `latest.json`. Browser capture is
optional: add `--browser --port <port> --project-id <isolated-id>` to record
screenshots against a non-conflicting dashboard session.

The challenge sandbox evidence is intentionally replay-shaped: one bounded
worker passes, another fails or is interrupted, and a replay attempt is recorded
with a new fence, new graph trace, and the same contract lineage. This is the
smallest case that makes graph-bound contracts different from ordinary
chat-based retry.

For full one-click install E2E, run the Docker host lanes first:

```bash
docker/hn-install-audit/run-install-audit.sh --host both
```

Those lanes build separate Codex and Claude Code containers, reuse host auth
read-only at runtime, feed the README install prompt, then feed the HN demo
prompt. This is a release gate, not a user install requirement. Their JSON
reports can be passed back into `--sandbox-audit` with `--require-install-gates`.

## What To Inspect

| Surface | What it proves | Dashboard URL pattern |
| --- | --- | --- |
| Commit-bound graph | The observer scopes workers from project structure, not only from chat text. | `http://localhost:40000/dashboard?project_id=<project_id>&view=graph` |
| Backlog timeline | Worker A pass, Worker B failure, replay, verification, and close-ready evidence stay separated by lane. | `http://localhost:40000/dashboard?project_id=<project_id>&view=backlog&backlog=<backlog_id>` |
| Review and reconcile | Accepted work reconciles once against the target graph before later agents treat it as truth. | `http://localhost:40000/dashboard?project_id=<project_id>&view=operations` |
| Audit report | The same observer that ran the demo writes a semantic evaluation with cited artifacts. | `docs/hn-demo/audits/latest.md` |

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

The screenshots are optional evidence for the article. The primary demo is the
multi-agent challenge above; use the longer design story link near the top if
you want the older case narrative and audit trail.

## Supporting Protocol Docs

- [System Architecture](../architecture.md)
- [Manual Fix SOP](../governance/manual-fix-sop.md)
- [Reconcile Workflow](../governance/reconcile-workflow.md)
- [Asset Inbox API Contract](../api/asset-inbox-contract.md)
