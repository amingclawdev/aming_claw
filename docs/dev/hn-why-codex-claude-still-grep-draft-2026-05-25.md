# Why do Codex and Claude still grep?

Every serious coding agent eventually reaches for grep.

That is not a failure. Grep is fast, local, inspectable, and honest. If the
question is "where does this symbol appear?" or "which file contains this
string?", grep is still one of the best tools we have.

The problem is that grep is not project memory.

It can show matching text, but it does not know which file owns a feature, which
work item changed it, which reviewer accepted a semantic claim, which generated
asset is merely noise, or which verification evidence should block a merge. It
does not remember the difference between "this doc mentions a module" and "this
doc is accepted as the module's governance record." It does not know whether an
agent is reading a current commit-bound model of the project or just sampling
whatever the working tree happens to contain.

That gap is where many AI coding sessions become repetitive. The model asks the
filesystem the same broad questions again and again because the durable fact
layer is missing or too weak to trust.

## The three fact layers we are dogfooding

In Aming Claw we have been trying to separate three kinds of project facts.

**Structural facts** are facts about the project shape. Which files belong to
which subsystem? Which functions call which functions? Which docs, tests, and
config files are bound to a node, and which are just unowned assets? Which graph
snapshot was built from which commit?

Structural facts should not be vibes from an embedding search. They should be
commit-bound, queryable, and repairable. If the graph is wrong, the repair path
should be source-controlled hints, config, rules, accepted review events, or a
new reconcile run, not a silent database edit.

**Work facts** are facts about intent, scope, and responsibility. What backlog
row authorized the change? Which branch and worktree is this agent allowed to
touch? Which paths are forbidden because another agent or the observer owns
them? What acceptance criteria are being tested?

These facts matter more once you have multiple agents. Without work facts, an
assistant can be locally correct and globally destructive: it can "clean up"
dirty files that belong to someone else, edit a sibling draft, or merge a branch
before the observer has reviewed the evidence.

**Execution facts** are facts about what actually happened. Which graph queries
were run? Which trace ids came back? Which tests passed? Was an ignored doc file
force-added or still invisible to normal git status? Which runtime version was
serving the dashboard when the work was verified?

These facts are boring in the right way. They make an agent's claims auditable
after the context window is gone.

## Dogfood case: Asset Inbox relation browser

One concrete example is the Asset Inbox relation browser work.

The naive version of this feature is a search page for orphan files. That is
useful, but it is not enough. A repo has source files, unbound docs, generated
artifacts, stale mappings, weak candidate matches, accepted bindings, and
ignored archives. Treating all of that as one pile produces bad governance:
agents start filing work from noise, or they treat a weak AI proposal as if it
were trusted graph state.

The fact-layer version separates source state from derived state:

- committed files, hints, config, accepted events, and deterministic inventory
  are source inputs;
- Asset Inbox rows, candidate binding proposals, dashboard grouping, and backlog
  payloads are derived outputs;
- weak matches can be shown to an operator, but accepted bindings are the only
  ones that enter review impact scope.

Grep can find "Asset Inbox." It cannot tell you whether a doc is a candidate,
accepted binding, stale projection, generated artifact, or ignored asset unless
that state has been modeled somewhere else.

## Dogfood case: observer-only merge workflow

The second example is the observer-only merge workflow we use for parallel
Manual Fix work.

In that workflow, implementation agents are deliberately boring. A worker gets a
backlog id, a branch, a worktree, a fence token, an owned file list, forbidden
paths, required checks, and a stop condition. It does not merge. It does not
push. It does not close the backlog row. It does not activate graph refs or
mutate merge queues. It stops at review_ready with evidence.

That sounds bureaucratic until you have two agents writing related launch docs
at the same time while the observer has unrelated dirty edits in governance
files. In that situation, grep can tell an agent that a phrase exists in a
forbidden file. It cannot tell the agent that the file is out of contract and
must not be touched. The work fact has to come from the backlog and the parallel
contract, and the execution fact has to come from timeline evidence and focused
checks.

## What this changes for coding agents

The interesting question is not whether agents should stop using grep. They
should keep using grep.

The question is what grep should be surrounded by.

A better local coding loop looks like this:

1. Ask the project graph for the relevant structure.
2. Ask the backlog or contract for the permitted work boundary.
3. Use grep and file reads for exact local evidence.
4. Make the scoped change.
5. Record execution facts: traces, changed files, tests, ignored-path status,
   and any deferred review.

That loop does not require magic. It requires refusing to let the model's
temporary context be the only memory of the project.

## Boundaries

The boundaries are important because the claim is easy to overstate.

This is not a claim that OpenAI, Anthropic, or any other lab cannot build these
layers. They can. Some parts may already exist inside proprietary products or
enterprise workflows. The point is narrower: the default local coding-agent
experience still often falls back to repeated grep because durable,
project-specific facts are not always present, trusted, or exposed to the agent
as first-class tools.

This is also not a claim that graphs solve everything. A bad graph is worse than
no graph if agents treat it as authority. The graph has to be commit-bound,
inspectable, and repairable. AI-generated semantics have to go through review
before they become trusted project memory. Docs and tests have to remain assets
until their binding is accepted.

And grep remains part of the system. Exact text search is still the fastest way
to verify many local claims. The failure mode is using grep as a substitute for
ownership, work scope, and execution history.

## Challenge

The challenge is to make the extra memory earn its keep.

The Hacker News skeptical version of this is fair:

- Is the graph fresh, or is it stale ceremony?
- Can a developer inspect and repair the facts, or did we just create another
  opaque index?
- Does the workflow catch real multi-agent failures, or does it only produce
  audit logs?
- Can the system degrade gracefully to grep and local files when governance is
  unavailable?
- Are we measuring whether this reduces repeated context gathering, merge
  conflicts, and unsupported claims?

Those are the right questions. A project-memory layer earns trust only when it
helps on messy real repos, under dirty worktrees, ignored files, parallel
branches, and skeptical review.

My current answer is: grep is still necessary. It is just not enough. The next
useful layer for coding agents is not another bigger prompt. It is durable
project facts: structural facts about the code, work facts about the contract,
and execution facts about what actually happened.
