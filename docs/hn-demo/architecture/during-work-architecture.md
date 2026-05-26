# During Work Architecture

## Fear

The fear during work is that AI execution becomes a black box. The developer can
ask for a feature, wait, and receive a confident final answer, but still not know
which files were owned, what evidence was produced, whether the work satisfied
the contract, or whether parallel agents created incompatible project state.

## Failure Mode

Parallel worktrees solve only the file-editing part of the problem. They do not
by themselves answer who has merge authority, which branch output belongs to
which contract, whether a worker is reviewing its own work, or which graph state
is canonical after multiple branches move.

Without explicit rules, AI parallelism can create multiple plausible versions of
the project in memory, not just multiple diffs in Git.

## Architecture Invariants

**Observer owns merge authority:** workers can implement, run checks, and append
evidence. They cannot merge, push, close the backlog row, activate graph refs, or
accept their own work.

**Contract owns scope:** each worker receives target files, forbidden paths,
acceptance criteria, required evidence, source head, dirty-scope expectations,
and a fence token. Output that does not match the contract is not accepted.

**Timeline owns evidence:** dispatch, implementation, verification, close-ready,
merge, and close are separate typed events. A final chat answer is not the audit
record.

**Target ref owns graph truth:** branch/worktree graph artifacts are one-hop
candidate evidence against the target commit. A branch cannot chain graph
reconcile, activate its own projection, or become canonical project memory. Only
after ordered merge does the target ref reconcile and advance the active graph.

## Observer Mode

Observer mode changes the developer's time shape. The human can stay in
requirements and review mode, file multiple contracts, tighten boundaries, and
then dispatch bounded workers in parallel. Two workers are enough to prove the
model: separate scopes, separate fences, separate traces, one review boundary.
This avoids the slow loop where the developer waits for one agent to finish
before thinking about the next task.

The observer still owns review and merge order. Parallel execution becomes a
batch of contract-bound candidate changes, not a swarm of autonomous agents with
write authority.

## What The Dashboard Shows

- backlog row or manual-fix contract;
- timeline lanes for observer and two or more worker actions;
- dispatch, implementation, verification, and close-ready checkpoints;
- evidence inspector with actor, phase, status, artifacts, and requirement ids;
- graph stale/current state after commit and reconcile.

## What The Agent Can Do

- implement within its file/worktree fence;
- run required checks and prechecks;
- append implementation and verification evidence;
- produce candidate branch output for observer review.

## What The Agent Cannot Do

- merge itself;
- close the backlog row;
- edit files outside its contract;
- reuse a stale source head or fence token;
- make branch-local graph state canonical;
- chain reconcile from a branch candidate.

## Evidence In This Repo

- Demo case: [Fear During Work](../cases/during-work.md)
- Demo entry: [HN Fear Demo](../README.md)
- Workflow reference: [Manual Fix SOP](../../governance/manual-fix-sop.md)

## Related Case Study

[I told my AI to build a feature. Did it? I had no
idea.](https://dev.to/amingin_ai/i-told-my-ai-to-build-a-feature-did-it-i-had-no-idea-1f1)

That earlier story is the task-state version of this case: a task is not done
because the agent says so; it needs status, commit evidence, and a queryable
ledger that both the human and AI can read.
