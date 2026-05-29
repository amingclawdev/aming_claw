# Most AI coding agents are chat with tools, not development systems.

An agent says "done."

What requirement did it satisfy? Which files was it allowed to touch? Which
project facts did it rely on? What evidence proves it? Can the next agent safely
build on it?

If those answers live only in chat history, you do not have a development
system. You have a transcript.

> Most coding agents operate inside a conversation.
> A development system operates inside governed project state.

A conversation can hold unlimited context. What it cannot tell you is which
parts of that context are authoritative for the work in front of the agent right
now. That missing layer, not bigger memory, not longer context, not more agents,
is what separates a development system from chat-with-tools.

## The generative core

Without **relationship / impact** facts, the system does not know what exists,
or what a change affects.

Without **intent**, the system does not know what the user actually wants.

Without a **contract**, the system does not know what the agent is allowed to
change.

Without a **process** record, the system does not know what actually happened,
or where work stands.

Without **constraint** gates, the system does not know whether the work can
safely proceed or close.

These five are not features. They are the questions a development system must
answer without reading chat history. If your agent answers them from the
transcript, it is guessing.

## The five management surfaces

Abstract and product-neutral, adopt these even if you never use Aming Claw.

| Surface | The question it owns |
|---|---|
| Intent management | What does the user want, confirmed and prioritized? |
| Contract management | What is this agent allowed to do now? |
| Relationship / impact management | What exists, how is it connected, what does this change affect? |
| Process management | What actually happened, with evidence, and where does work stand? |
| Constraint management | What must be verified before dispatch, merge, or close? |

Read top-to-bottom by authority, not convenience:

```text
intent defines demand
contract fences scope
relationship / impact supplies the facts
process records evidence
constraints decide whether it may proceed or close
```

## Contract is not constraint

A contract is fixed when work is dispatched: allowed files, scope, required
evidence.

A constraint gate is evaluated at each transition and recomputed at close from
the final changed set, so an agent cannot claim a narrow scope and quietly
exceed it.

Constraint management should emit alerts, not prose. The agent should receive
the next required action from current project state, not scan a memory dump and
guess what matters.

## One implementation

Aming Claw is an implementation of the paradigm, not the paradigm itself.

```text
Intent management        ->  Backlog       intent layer: demand and priority
Contract management      ->  Contract      authorization layer: allowed vs forbidden
Relationship / impact    ->  Graph         fact layer, commit-bound: structure and impact
Process management       ->  Timeline      evidence layer: what was done, how far
Constraint management    ->  Gates/Alerts  release gate: what evidence clears close
```

The boundary that keeps it honest:

> Memory suggests. Governed project state authorizes.
> A development fact must bind to an artifact and expire when that artifact
> changes.
> A stale fact can be rejected; a drifting memory cannot.

## Reproducible proof

The surfaces are not a diagram. Each is backed by a runnable case.

| Failure | Surface that caught it | Evidence |
|---|---|---|
| A TypeScript node was falsely credited with unrelated Python tests as strong coverage. | Relationship / impact + constraint | The graph exposed the weak binding, the AI proposal went to review instead of mutating truth, the rule moved into the test fan-in path, a regression fixture was added, and full reconcile materialized the corrected graph. Evidence: [semantic rule](https://github.com/amingclawdev/aming-claw/blob/main/docs/config/semantic-enrichment.md), [fixture](https://github.com/amingclawdev/aming-claw/blob/main/docs/fixtures/external-governance-demo/l4-smoke-fixture.md). |
| A hook reminder carried no business payload, so a worker could have claimed context it never received. | Intent + process + constraint | The observer claims the command before the payload is visible, and the worker must echo the exact reminder as timeline evidence. Evidence: [Observer Reminder Echo Demo](https://github.com/amingclawdev/aming-claw/blob/main/docs/demos/observer-reminder-echo.md). |

Longer implementation note:
[How Aming Claw implements right context for AI-agent development](https://github.com/amingclawdev/aming-claw/blob/main/docs/articles/how-aming-claw-implements-right-context.md)

Try the demo:
[Vibe Queue Demo](https://github.com/amingclawdev/aming-claw/blob/main/docs/vibe-queue-demo/README.md)
shows the ordinary-user version: keep describing requirements while the
observer records, confirms, queues, executes, and reports progress through
backlog and timeline state.

Technical challenge:
[HN Multi-Agent Challenge Demo](https://github.com/amingclawdev/aming-claw/blob/main/docs/hn-demo/README.md)
shows the harder version: one observer coordinates multiple commit-bound
workers, handles failed/interrupted worker replay, reconciles the target graph,
and writes audit evidence.

## The challenge

> Show me relationship, intent, process, contract, and constraint.
> If your agent has none of them, why call it a development system?
