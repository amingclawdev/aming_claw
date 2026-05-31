# How Aming Claw implements right context for AI-agent development

This is the technical companion to
[Most AI coding agents are chat with tools, not development systems](./most-ai-coding-agents-are-chat-with-tools.md).

## Thesis

Aming Claw implements right context by making governed development state the
authority layer.

The public thesis says:

> Most AI agents are not development systems.

This technical companion explains the implementation chain:

```text
Relationship / impact management -> Graph
Intent management                -> Backlog
Process management               -> Timeline
Contract management              -> Contract
Constraint management            -> Gates and alerts
```

Contract-bound alerts are the routing mechanism between those layers. They turn
development state into short, stage-specific obligations.

Memory remains useful, but only as an advisory signal.

## Authority chain

The stack should be read by authority, not by convenience:

```text
Intent       tells what matters.
Contract     tells what is allowed.
Relationship tells what is connected and affected.
Process      tells what happened and where work stands.
Constraint   tells what must happen next.
Close gate    tells whether work is accepted.
Memory        suggests what may matter, but never authorizes close.
```

This is the difference between a coding assistant and an AI-agent development
system.

The loop is not:

```text
chat history -> agent guesses -> tool calls -> summary
```

The loop is:

```text
user intent enters backlog
observer creates contract
agent emits event
relationship facts are queried at the relevant version
timeline records evidence
contract routes alerts
close gate recomputes required evidence
```

## Stage context packets

The agent should not receive the whole workspace memory.

It should receive a stage context packet:

```text
stage
contract id
allowed files
required facts
required evidence
blocking alerts
non-blocking alerts
freshness boundary
close condition
```

The packet changes by stage:

- before work: intent, priority, duplicate state, confirmation need;
- during dispatch: contract, allowed scope, worker fence, required evidence;
- during implementation: relevant relationship and fact context;
- after implementation: changed artifacts, test obligations, drift,
  constraints;
- before close: process evidence, unresolved alerts, waiver or follow-up state.

Long context answers:

> How much can the agent read?

Right context answers:

> Which facts are authoritative for this stage?

## Backlog: intent management

Backlog answers:

> What does the user want, and what matters most?

It stores:

- raw user requirements;
- confirmed requirements;
- priority;
- acceptance criteria;
- duplicate or supersede decisions;
- backlog status;
- close state.

Without backlog, a coding agent can execute a local instruction, but the system
does not know whether that work belongs to the user's larger demand.

## Contract: contract management

Contract answers:

> What is this AI allowed to do now?

It stores:

- target files;
- forbidden files;
- owned files;
- worker fence token;
- acceptance criteria;
- required evidence;
- close gate;
- required services.

Without contract, a coding agent can make a plausible change, but the system
cannot tell whether that change was authorized.

## Graph: relationship / impact management

Graph answers:

> What exists, how is it connected, and what can this change affect at this
> commit?

It stores:

- graph snapshots;
- changed files;
- file inventory;
- function and relation facts;
- doc, test, and config assets;
- query traces;
- review bindings;
- reconcile status.

If the commit changes, graph facts can become stale. That is a feature. Stale
facts are safer than drifting memories because stale facts can be rejected.

## Timeline: process management

Timeline answers:

> What actually happened?

It stores:

- worker dispatch evidence;
- graph query trace ids;
- implementation events;
- test results;
- verification evidence;
- route decisions;
- close-ready evidence;
- waivers or follow-ups.

Without timeline, a system cannot distinguish "the agent says it did this" from
"the project has evidence that this happened."

## Gates and alerts: constraint management

Gates and alerts are generated. They are not hand-written context.

The input is a typed event:

- requirement_captured;
- requirement_confirmed;
- duplicate_candidate_found;
- observer_command_enqueue;
- observer_command_next;
- worker_dispatched;
- worker_finished;
- worker_failed;
- file_changed;
- test_failed;
- graph_stale;
- close_requested.

The output is a short list of required or recommended actions:

- ask user to confirm requirement;
- merge into existing backlog row;
- run dispatch gate;
- run focused tests;
- run graph query;
- run scope reconcile;
- inspect doc drift;
- append verification evidence;
- block close;
- allow close.

In other words:

> Agents emit events. Contracts route alerts. Gates decide whether progress is
> allowed.

The alert is the moment where right context becomes operational. It tells the
agent or observer what must happen next, why, and what evidence will clear it.

## Contract-bound alert example

An alert is not a notification.

It is a routed obligation generated from current development state.

It has:

- source event;
- contract id;
- affected files or graph nodes;
- required service;
- blocking or non-blocking severity;
- evidence needed to clear it.

The Observer Reminder Echo demo uses this payload-free reminder:

```json
{
  "kind": "observer_command_pending",
  "project_id": "aming-claw",
  "message": "pending observer commands exist; call observer_command_next",
  "payload_included": false,
  "next_action": {
    "tool": "observer_command_next",
    "description": "claim the next pending observer command"
  }
}
```

That packet is small on purpose. It says what must happen next, and it also
says what is not included.

The reminder must not include command payload fields or `session_token`. The
observer sees the business payload only after `observer_command_next` claims the
command.

The subagent contract then requires the worker to echo the exact reminder:

```json
{
  "received_reminder_echo": {
    "kind": "observer_command_pending",
    "project_id": "aming-claw",
    "message": "pending observer commands exist; call observer_command_next",
    "payload_included": false,
    "next_action": {
      "tool": "observer_command_next",
      "description": "claim the next pending observer command"
    }
  }
}
```

The observer does not need to inspect a memory dump to know what to do next.
The governed workflow emits the obligation, preserves the payload boundary, and
turns the reminder into auditable contract evidence.

## Minimal service router shape

```text
event
  -> load backlog row
  -> load contract template
  -> load current graph snapshot
  -> load changed files
  -> load existing timeline evidence
  -> evaluate route rules
  -> return service plan
```

The return value is not a paragraph. It is structured work:

```json
{
  "contract_id": "AC-DEMO-OBSERVER-REMINDER-ECHO-20260529",
  "event": "observer_command_enqueue",
  "blocking": [
    {
      "service": "observer_command_next",
      "reason": "pending observer command must be claimed before payload is visible",
      "required_evidence": "claimed_command_summary"
    }
  ],
  "non_blocking": [
    {
      "service": "subagent_contract_echo",
      "reason": "worker should prove the reminder it received",
      "required_evidence": "received_reminder_echo"
    }
  ]
}
```

This becomes the agent's next prompt, the dashboard's next action, and the close
gate's checklist.

The important constraint: the same route must be recomputable at close time. If
the final changed-file set differs from the worker's claimed scope, the gate
must recompute tests, reconcile needs, doc drift, and waiver or follow-up
requirements from the final state.

## Test-route and route-context proof

The route context system is easiest to inspect through a runnable test-route
demo:

```bash
python scripts/paradigm-route-context-demo.py --json
```

The same proof is registered as a scenario:

```bash
node scripts/test-scenario-manager.mjs run \
  --scenario paradigm_route_context_demo \
  --json
```

This demo does not ask an agent to infer test policy from a prompt. It asks the
route layer to return a structured `test_flow_route` and a low-noise
`prompt_alert_bundle`.

It proves seven route decisions:

| Route proof | Expected result | Why it matters |
|---|---|---|
| `fixture_only_route_runs` | passes with `model_calls: forbidden` | deterministic fixture work should not call a model |
| `dashboard_mock_ai_playwright_route_declared` | declares the Playwright mock-AI dashboard lane | browser evidence can use fixed mock AI inputs without provider calls |
| `docker_route_blocks_without_approval` | blocks without `--allow-docker` | container boundaries require explicit operator approval |
| `mock_ai_docker_route_blocks_without_approval` | blocks AI-related Docker validation without approval | Docker AI checks stay gated, then run fixed structured mock output inside a no-network container after approval |
| `live_ai_route_blocks_without_approval` | blocks without `--allow-live-ai` | live provider checks must not spend quota silently |
| `external_project_registers_fixture_route` | records external project root and manifest hash | external project routes are source-controlled target evidence |
| `route_prompt_bundle_is_hashable_and_low_noise` | exposes route/prompt hashes and no raw context | context injection is visible, scoped, and auditable |

This is the paradigm in a single command:

```text
Intent                -> scenario ids and external route ids
Contract              -> test_flow_route lanes, flags, and evidence ids
Relationship / impact -> external project root and manifest hash
Process               -> scenario manager run reports
Constraint            -> selected-lane alerts and action precheck
```

The default proof does not run browser E2E or Docker. Those heavier lanes are
registered separately: `dashboard_mock_ai_playwright_fixture` runs Playwright
against a mocked governance API, while `mock_ai_docker_fixture` remains blocked
until `--allow-docker`. The point is that the system chooses those routes only
when the selected lane requires them, and the corresponding alert is short,
visible, and stage-specific.

## Observer Reminder Echo proof

The current demo is small, but it proves the whole chain.

The user-facing promise is simple:

> Show a new user that Aming Claw can notify an observer, keep business payload
> out of the hook reminder, and carry that reminder into subagent contract
> evidence.

The governed route is:

1. `observer_command_enqueue` records that work is waiting for the observer.
2. Governance emits a payload-free hook reminder.
3. The observer displays that reminder exactly.
4. The observer calls `observer_command_next` and only then sees payload keys.
5. The subagent contract requires `received_reminder_echo`.
6. Timeline evidence records the reminder, echo, and payload-boundary check.
7. The backlog row `AC-DEMO-OBSERVER-REMINDER-ECHO-20260529` anchors the demo
   intent and acceptance criteria.

Mapped to the general surfaces:

```text
Relationship / impact -> demo files, fixture, Docker validation, governance code,
                         tests, and graph/asset binding status
Intent                -> backlog AC-DEMO-OBSERVER-REMINDER-ECHO-20260529
Process               -> enqueue -> reminder -> claim -> subagent echo
                         -> timeline evidence
Contract              -> subagent addendum requiring received_reminder_echo
Constraint            -> payload-free hook, no session_token, no business
                         payload before claim
```

The product experience can still feel simple:

```text
You have a pending observer command.
Call observer_command_next.
```

But underneath, the system is governed by backlog, contract, graph/asset state,
timeline, and evidence gates.

## Why this beats a memory dump

A memory dump says:

> There may be pending observer commands.

A contract-bound alert says:

> A pending observer command exists. The hook reminder includes no business
> payload and no session token. Call `observer_command_next`, then prove the
> worker received the exact reminder through `received_reminder_echo`.

The second is operational.

It can be audited. It can be retried. It can be shown to the user. It can fail
closed.

## Where personal judgment belongs

Personal judgment is still valuable. In Aming Claw, it belongs below the
authoritative layers.

A private operator memory store can store:

- recurring product instincts;
- architecture preferences;
- launch heuristics;
- expert routing rules;
- examples of good decisions.

Those can improve observer decisions. They should not silently override
development facts.

The clean separation is:

```text
private judgment -> proposes
intent management -> captures demand
contract management -> authorizes scope
relationship / impact management -> verifies system facts
process management -> records evidence
constraint management -> routes next obligations
close gate -> accepts or rejects completion
```

This lets a founder's judgment compound without turning into invisible
development memory.

## Known gaps and honest boundaries

This design is still early.

The hardest parts are:

- making alert routing strict without making the system feel heavy;
- keeping the ordinary-user surface simple;
- preventing fake evidence from passing gates;
- making concurrent workers safe without forcing users to understand git;
- deciding when memory should influence routing versus only annotate it;
- making source-backed expert judgment useful without turning it into hidden
  authority.

But the core direction feels stable:

> Build a governed development workflow, not a bigger prompt.

The agent should not carry the whole development system in its head. The
workflow should raise the right alert at the right time, with the evidence
needed to clear it.

## Terminology stack

Use these terms at different layers:

```text
Public hook:
  Most AI agents are not development systems.
  Chat plus tools is not an AI-agent development system.
  AI agents need a governed development workflow.
  Stop treating chat history as development state.

Paradigm:
  right context implemented through governed development state
  relationship / intent / process / contract / constraint management
  stage-specific context packets for coding agents

Implementation chain:
  Relationship -> Intent -> Process -> Contract -> Constraint -> Close

Aming Claw implementation:
  Graph -> Backlog -> Timeline -> Contract -> Gates/Alerts -> Close

Mechanism:
  contract-bound alerts
  workflow alerts

Evidence primitives:
  graph
  backlog
  timeline
  contract
  gates
  evidence-gated close

Boundary:
  memory suggests; governed development state authorizes
```

Avoid leading with `contract-bound alerts` for non-expert audiences. Use
`workflow alerts` first, then define why the alert is contract-bound.

Avoid making "memory is bad" the public claim. The defensible position is:

> Memory is useful, but it is not the authority layer.
