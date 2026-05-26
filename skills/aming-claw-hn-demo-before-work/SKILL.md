---
name: aming-claw-hn-demo-before-work
description: HN demo case for the fear that AI does not understand project structure and duplicates work. Guides evidence collection for graph-first discovery, backlog contract, target file fence, and acceptance criteria before implementation.
---

# HN Demo: Before Work

Show how Aming Claw turns "the AI will grep blindly and duplicate work" into a
bounded, auditable start condition.

## Fear

AI does not understand the project structure, misses existing modules, and
creates duplicate work.

## Evidence To Collect

- Graph discovery: node, file, function, or neighbor evidence for the target
  area before reading broad source files.
- Backlog contract: row with title, details, target files, tests, required
  docs, and acceptance criteria.
- Target file fence: exact files or worktree boundary assigned to the worker.
- Acceptance criteria: concrete conditions the implementation must satisfy.

## Architecture Reason

- Commit-bound graph: graph evidence is tied to a known commit, not dirty local
  guesses.
- Graph-first discovery: the operator starts from structure, ownership, and
  neighbors before patching.
- Backlog contract: scope, acceptance criteria, and evidence obligations live
  in the work ledger.

## Operator Steps

1. Check governance and dashboard status. If governance is offline, instruct
   the user to run `aming-claw start`; do not start it silently.
2. Confirm the project and graph commit with `graph_status` or the dashboard.
3. Inspect the Graph view for the target area. Prefer node inspector, related
   files, functions, and neighbors over broad source search.
4. Inspect the Backlog row. Verify target files and acceptance criteria are
   present before implementation.
5. Inspect or state the fence: branch/worktree, owned files, base commit, and
   any merge queue/fence token if this is a subagent demo.
6. Capture screenshots or links for Graph, Backlog, and fence evidence.

## Evidence Summary

```text
Before-work evidence
- Fear: project structure misunderstanding and duplicate work
- Graph: <snapshot/link/screenshot/node evidence>
- Backlog contract: <bug id/link/screenshot>
- Fence: <branch/worktree/files/base commit>
- Acceptance criteria: <summary or link>
- Architecture reason: commit-bound graph + graph-first discovery + backlog contract
- Limitations: <none/offline dashboard/manual screenshot/etc>
```
