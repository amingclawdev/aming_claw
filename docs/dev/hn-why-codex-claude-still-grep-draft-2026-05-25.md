# Hope is not an engineering control for AI coding agents

Right now, when you ask an AI coding agent to ship a feature, you give it a
prompt and hope.

You hope it touched the right files. You hope it did not reimplement something
the project already had. You hope it ran the right tests. You hope it did not
break docs or config you forgot to mention. You hope the diff is actually what
you asked for.

Hope is not an engineering control.

I have been building Aming Claw around a simple idea: an AI coding agent should
not just produce a diff. It should work against a contract, leave typed evidence,
and update the project facts that the next agent will read.

The problem is not that agents use grep. Grep is fast, local, inspectable, and
honest. The problem is using grep, prompts, and chat history as the only memory
of a project.

## The three fears

The way I now think about AI coding work is split across three ordinary fears.

**Before work:** will the agent understand the project before it edits, or will
it duplicate an existing pattern and touch the wrong owner?

**During work:** can I see what the agent actually did, which files it owned,
which evidence it produced, and whether the work satisfied the contract?

**After work:** once the patch lands, do I know what changed in docs, tests,
config, generated assets, graph memory, and semantic memory before the next agent
trusts stale project state?

Those fears map to three kinds of project facts.

## Structural facts: before the edit

Structural facts describe what the project is.

Which files belong to which subsystem? Which functions call which functions?
Which docs, tests, config files, and assets are bound to a node? Which graph
snapshot was built from which commit? Is the active graph current, or is the
project asking the agent to reason from stale structure?

This matters because AI can make plausible architecture mistakes that are not
syntax errors.

One of my earlier failures was a service-pattern miss. A project already had a
standard HTTP service pattern, but the AI introduced a parallel WebSocket-style
service that looked coherent in isolation and wrong in context. The code could
compile. The problem was that the agent did not see the existing project shape
before writing.

Aming Claw treats the graph as a commit-bound projection of source, hints, config,
and accepted review events. The graph is not a mutable memory blob that the AI
edits directly. If the graph is wrong, the repair path is source-controlled
evidence or a reconcile run, not a silent database edit.

Case: [Fear Before Work](../hn-demo/cases/before-work.md)

Related deeper story:
[AI proposed 5 components for my parallel system. After walking one scenario, only 3 were real.](https://dev.to/amingin_ai/ai-proposed-5-components-for-my-parallel-system-after-walking-one-scenario-only-3-were-real-12nd)

## Work facts: during the edit

Work facts describe what was promised.

What backlog row authorized the change? Which target files are in scope? Which
paths are forbidden? Which branch, worktree, fence token, source head, and
precheck belong to this worker? Which acceptance criteria are required before a
human can close the task?

Without work facts, an agent can be locally correct and globally destructive. It
can edit a sibling draft, clean up someone else's dirty file, or merge a branch
because its own final answer sounds confident.

In the Aming Claw V1 flow, a worker does not accept its own work. It can
implement, run checks, and append evidence. It cannot merge, close the backlog
row, or make branch-local graph state canonical. Observer review and machine
prechecks are separate state transitions.

This is the part I think of as contract-driven execution. The contract names the
work, target files, acceptance criteria, required evidence, and review boundary.
The execution timeline records dispatch, implementation, verification, and
close-ready events. The interesting part is not that a timeline exists. The
interesting part is that "I implemented it", "it passed verification", "it is
ready to merge", and "the backlog is closed" are different facts.

Case: [Fear During Work](../hn-demo/cases/during-work.md)

Related deeper story:
[I told my AI to build a feature. Did it? I had no idea.](https://dev.to/amingin_ai/i-told-my-ai-to-build-a-feature-did-it-i-had-no-idea-1f1)

## Execution and project-memory facts: after the edit

Execution facts describe what actually happened.

Which graph queries ran? Which trace ids came back? Which tests passed? Which
commit landed? Which runtime version served the dashboard when verification ran?
Which docs, tests, config files, and generated assets changed, and are they
trusted project memory or just candidate evidence?

This is where a lot of AI work rots quietly. A diff can be correct while the
project's memory is not. A doc can mention a feature without being a trusted
governance record for that feature. A path match can be useful evidence without
being strong enough to enter review impact scope. A smoke test can pass while a
reader-facing case page still points at old screenshots.

That is why the after-work case separates source records from derived views:
committed files, source-controlled hints, config, accepted bindings, review
decisions, and timeline events are durable inputs; Asset Inbox rows, graph
snapshots, semantic projections, candidate bindings, and operations-queue state
are derived views.

A changed doc first becomes a commit-bound asset with status and provenance. It
becomes graph impact scope only after a reviewed binding, not because an AI or a
path heuristic guessed it belonged there.

Case: [Fear After Work](../hn-demo/cases/after-work.md)

Related deeper story:
[AI's tech debt is invisible - even to AI. I solved it at the architecture layer.](https://dev.to/amingin_ai/ais-tech-debt-is-invisible-even-to-ai-i-solved-it-at-the-architecture-layer-1nh1)

## A small real audit trail

This article draft caught one of its own boring failures during launch prep.

The HN demo browser smoke was passing, but the reader-facing docs still pointed
at old screenshot filenames. That is exactly the kind of drift that usually
survives because no source file is "broken."

We filed it as `HN-FEAR-DEMO-SCREENSHOT-INDEX-20260526`, patched the demo README
and case pages, reran the HN browser smoke, committed the fix, reconciled the
graph, and only then closed the backlog row. The source-visible part is the
audit commit:
[3ae68da8834cf24404c4d9672b2adaf02c19443e](https://github.com/amingclawdev/aming-claw/commit/3ae68da8834cf24404c4d9672b2adaf02c19443e).

The follow-up commit that made the audit link visible from the article draft is:
[70243f2dffe96c3a1bc5a9d6ed602ae6d236a60d](https://github.com/amingclawdev/aming-claw/commit/70243f2dffe96c3a1bc5a9d6ed602ae6d236a60d).

GitHub shows the source diff. The backlog row, timeline events, close gate, and
graph snapshot are local governance records unless you run the demo yourself.
That boundary matters: public source history is not the same thing as the local
audit trail that produced and verified it.

## What this changes for coding agents

The point is not to make agents stop using grep. They should keep using grep.

The question is what grep should be surrounded by.

A better local coding loop looks like this:

1. Ask the project graph for current structure and ownership.
2. Ask the backlog or contract for permitted scope.
3. Use grep and file reads for exact local evidence.
4. Make the scoped change.
5. Record execution facts: traces, changed files, tests, ignored-path status,
   runtime state, and any deferred review.
6. Reconcile source-derived project memory before the next agent trusts it.

That loop does not require magic. It requires refusing to let the model's
temporary context be the only memory of the project.

## Boundaries

The claim is easy to overstate, so here are the boundaries.

This is not a claim that OpenAI, Anthropic, or any other lab cannot build these
layers. They can. Some parts may already exist inside proprietary products or
enterprise workflows.

This is also not a claim that graphs solve everything. A bad graph is worse than
no graph if agents treat it as authority. The graph has to be commit-bound,
inspectable, and repairable. AI-generated semantics have to go through review
before they become trusted project memory. Docs, tests, and config files have to
remain assets until their binding is accepted.

And grep remains part of the system. Exact text search is still the fastest way
to verify many local claims. The failure mode is using grep as a substitute for
ownership, work scope, and execution history.

## Links for readers

Demo entry point:
[HN Fear Demo](../hn-demo/README.md)

The three case pages:

- [Before work: project understanding and contract](../hn-demo/cases/before-work.md)
- [During work: timeline, evidence, and merge boundary](../hn-demo/cases/during-work.md)
- [After work: asset review, drift, and reconcile](../hn-demo/cases/after-work.md)

Deeper background stories:

- [Before work: AI proposed 5 components for my parallel system. After walking one scenario, only 3 were real.](https://dev.to/amingin_ai/ai-proposed-5-components-for-my-parallel-system-after-walking-one-scenario-only-3-were-real-12nd)
- [During work: I told my AI to build a feature. Did it? I had no idea.](https://dev.to/amingin_ai/i-told-my-ai-to-build-a-feature-did-it-i-had-no-idea-1f1)
- [After work: AI's tech debt is invisible - even to AI. I solved it at the architecture layer.](https://dev.to/amingin_ai/ais-tech-debt-is-invisible-even-to-ai-i-solved-it-at-the-architecture-layer-1nh1)

Public audit commits:

- [Real audit fix: align HN demo screenshot index](https://github.com/amingclawdev/aming-claw/commit/3ae68da8834cf24404c4d9672b2adaf02c19443e)
- [Article audit link: add real HN audit trail](https://github.com/amingclawdev/aming-claw/commit/70243f2dffe96c3a1bc5a9d6ed602ae6d236a60d)

Suggested HN comment:

```text
I wrote this around three concrete fears I kept hitting with AI coding agents:

Before work: will it understand the project or invent a plausible wrong design?
https://github.com/amingclawdev/aming-claw/blob/main/docs/hn-demo/cases/before-work.md

During work: can I reconstruct what the agent actually did and what evidence it produced?
https://github.com/amingclawdev/aming-claw/blob/main/docs/hn-demo/cases/during-work.md

After work: do docs, tests, config, and graph memory drift after the patch lands?
https://github.com/amingclawdev/aming-claw/blob/main/docs/hn-demo/cases/after-work.md

The small audit commit mentioned in the article is here:
https://github.com/amingclawdev/aming-claw/commit/3ae68da8834cf24404c4d9672b2adaf02c19443e
```
