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

## Installed-User Prompt

After installing the Aming Claw plugin, ask Codex or Claude:

```text
Use the Aming Claw HN demo skill to run the three fear cases for this project:
before work, during work, and after work. Use deterministic demo evidence where
possible, do not require live AI, and show me the dashboard URLs for each case.
```

The skill should leave you with dashboard states that correspond to the three
case pages:

| Case | Page | Dashboard URL pattern |
| --- | --- | --- |
| Before work | [Fear Before Work](cases/before-work.md) | `http://localhost:40000/dashboard?project_id=<project_id>&view=graph` |
| During work | [Fear During Work](cases/during-work.md) | `http://localhost:40000/dashboard?project_id=<project_id>&view=backlog&backlog=<backlog_id>` |
| After work | [Fear After Work](cases/after-work.md) | `http://localhost:40000/dashboard?project_id=<project_id>&view=assets` |

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
| 01 | `docs/hn-demo/screenshots/01-before-work-graph.png` | Project graph, selected node, and evidence before editing. |
| 02 | `docs/hn-demo/screenshots/02-before-work-backlog.png` | Backlog row with target files and acceptance criteria. |
| 03 | `docs/hn-demo/screenshots/03-during-work-fences.png` | Branch/worktree or MF subagent identity and fence evidence. |
| 04 | `docs/hn-demo/screenshots/04-during-work-merge-queue.png` | Review-ready work, dependency or merge-gate status, and tests. |
| 05 | `docs/hn-demo/screenshots/05-after-work-asset-inbox.png` | Asset Inbox or file hygiene rows after the change. |
| 06 | `docs/hn-demo/screenshots/06-after-work-reconcile.png` | Operations Queue or reconcile status after commit/update graph. |

The screenshots are optional evidence for the article. The case pages remain
readable without them because they describe the fear, demo action, evidence, and
architecture layer directly.

## Architecture Map

- The before-work case is about the local graph-first control plane described in
  [System Architecture](../architecture.md) and the scoped work ledger described
  in [Manual Fix SOP](../governance/manual-fix-sop.md).
- The during-work case is about bounded manual fixes plus branch-isolated
  parallel work, covered by [Manual Fix SOP](../governance/manual-fix-sop.md),
  [Parallel Agent Multibranch Runtime Design](../dev/parallel-agent-multibranch-design.md),
  and [Parallel Agent Multibranch Test Scenarios](../dev/parallel-agent-multibranch-test-scenarios.md).
- The after-work case is about asset review and reconcile, covered by
  [Asset Inbox API Contract](../api/asset-inbox-contract.md),
  [Reconcile Workflow](../governance/reconcile-workflow.md), and
  [Manual Fix SOP](../governance/manual-fix-sop.md).
