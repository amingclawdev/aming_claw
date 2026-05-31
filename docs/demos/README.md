# Demo Map: proof cases for the AI-agent development system paradigm

Aming Claw demos are organized as proof cases, not feature tours.

Each demo answers one question:

> Which part of a development system would fail if this surface did not exist?

The short thesis is:

[Most AI coding agents are chat with tools, not development systems](../articles/most-ai-coding-agents-are-chat-with-tools.md).

The longer implementation note is:

[How Aming Claw implements right context for AI-agent development](../articles/how-aming-claw-implements-right-context.md).

## Start here

| Demo | Audience | Type | Paradigm surfaces | What it proves |
|---|---|---|---|---|
| [Vibe Queue Demo](../vibe-queue-demo/README.md) | Ordinary vibe-coding users | Everyday workflow | Intent + Process + Contract | A user can keep describing requirements while the observer records, confirms, queues, executes, and reports progress. |
| [HN Multi-Agent Challenge](../hn-demo/README.md) | Technical reviewers | Challenge / adversarial proof | Contract + Relationship / Impact + Process + Constraint | Multiple commit-bound workers can be coordinated by one observer, including failed/interrupted worker replay and audit evidence. |
| [Route Context And Test Routes](route-context-test-routes.md) | Agent-infra builders | Runnable route proof | Contract + Relationship / Impact + Process + Constraint | Route-owned context selects focused/fixture/Docker/live-AI/external lanes, emits low-noise alerts, and blocks unsafe lanes without approval. |
| [Observer Reminder Echo](observer-reminder-echo.md) | Agent-infra builders | Protocol proof | Intent + Process + Constraint | A payload-free hook reminder can route an obligation without leaking the business payload, then become timeline evidence. |

## Everyday user demos

These demos avoid audit-heavy language. They show the problems ordinary users
hit while working with AI agents.

| Demo | User pain | Surface proved | Run |
|---|---|---|---|
| [Vibe Queue Demo](../vibe-queue-demo/README.md) | "I want to keep adding requirements without waiting for one AI task to finish." | Intent management + process management + contract management | Use the `aming-claw-vibe-queue-demo` skill after install. |
| [Docs Drift Demo](../drift-demo/README.md) | "The feature changed, but the docs did not." | Relationship / impact management + constraint management | Use the `aming-claw-drift-demo` skill after install. |
| [Backlog Duplicate Demo](../backlog-dupe-demo/README.md) | "I asked for something that overlaps existing work." | Intent management + constraint management | Use the `aming-claw-backlog-dupe-demo` skill after install. |

## Technical proof demos

These demos are for readers who want to challenge the architecture.

| Demo | Failure mode | Surface proved | Evidence |
|---|---|---|---|
| [HN Multi-Agent Challenge](../hn-demo/README.md) | Chat-based multi-agent work loses scope, replay, and audit boundaries. | Contract + relationship / impact + process + constraint | Observer coordinates multiple commit-bound workers, failed/interrupted replay, graph reconcile, and audit self-review. |
| [Route Context And Test Routes](route-context-test-routes.md) | A worker receives a noisy prompt or wrong test lane, then silently starts Docker, calls live AI, or treats an external project route as local. | Contract + relationship / impact + process + constraint | `paradigm_route_context_demo` proves fixture-only pass, Docker/live-AI gated block, external manifest registration, and hashable prompt context with no raw prompt leak. |
| [Observer Reminder Echo](observer-reminder-echo.md) | A worker could claim it saw context that was never delivered. | Intent + process + constraint | Hook reminder is payload-free; observer must claim payload; worker must echo reminder as timeline evidence. |
| [Weak Test Binding / Graph Self-Repair](../config/semantic-enrichment.md) | A TypeScript node can be falsely credited with unrelated Python tests as strong coverage. | Relationship / impact + constraint | Semantic rule, fixture, review gate, and full reconcile materialize the corrected relationship. |

## Onboarding and inspection demos

These are the quick "see the system" demos in the README.

| Demo | Purpose | Link |
|---|---|---|
| Install and verify | Confirm plugin install, runtime startup, and dashboard opening. | [README demos](../../README.md#demos) |
| Bootstrap project | Build a commit-bound graph for a target project. | [README demos](../../README.md#demos) |
| Inspect graph | Navigate nodes, relations, functions, and files. | [README demos](../../README.md#demos) |
| Resolve backlog row | Show backlog evidence and completion. | [README demos](../../README.md#demos) |
| AI Enrich review | Queue, propose, accept, or reject semantic memory. | [README demos](../../README.md#demos) |
| Governance Hint | Bind an orphan doc/test/config file through source-controlled evidence. | [README demos](../../README.md#demos) |

## Older story cases

These are narrative cases behind the original three-fear framing.

| Case | Fear | Surface focus |
|---|---|---|
| [Fear Before Work](../hn-demo/cases/before-work.md) | "AI does not understand the project before editing." | Relationship / impact + contract |
| [Fear During Work](../hn-demo/cases/during-work.md) | "I cannot see what agents are doing during implementation." | Process + contract |
| [Fear After Work](../hn-demo/cases/after-work.md) | "AI changes leave stale docs, tests, and config behind." | Relationship / impact + constraint |

## How to read this map

If you are evaluating the paradigm, do not start by asking whether a single demo
looks impressive. Ask whether the demos cover the five surfaces:

```text
intent      -> what the user wants
contract    -> what the agent is allowed to do
relationship / impact -> what exists and what changes affect
process     -> what happened and where work stands
constraint  -> what must be checked before progress or close
```

If a coding-agent system cannot show runnable cases for those five surfaces, it
is probably still chat with tools.
