# Route Context And Test Routes Demo

This demo is the concrete proof case for the article claim:

> A development system routes the right context from governed project state,
> instead of asking an agent to infer authority from chat history.

It exercises two Aming Claw mechanisms together:

- `test_flow_route`: the test route selected for a scenario, including lane,
  required flags, model-call policy, and selected-lane alert bundle.
- `route.prompt_alert_bundle`: the route-owned prompt context packet with
  stable hashes and low-noise alerts.

The demo is deterministic. Its default proof does not start Docker, call live
AI, mutate the primary governance project registry, or require live providers.
Dashboard evidence is covered by a separate Playwright lane that uses a mocked
governance API and fixed mock-AI timeline input.

## Run

```bash
python scripts/paradigm-route-context-demo.py --json
```

Or run it through the scenario manager:

```bash
node scripts/test-scenario-manager.mjs run \
  --scenario paradigm_route_context_demo \
  --json
```

## What It Proves

The JSON report contains seven proof cases:

| Proof case | Expected result | Surface proved |
|---|---|---|
| `fixture_only_route_runs` | `fixture_only` passes with `model_calls: forbidden` | Contract + process |
| `dashboard_mock_ai_playwright_route_declared` | Playwright mock-AI dashboard lane is declared with `model_calls: mocked` | Process + contract |
| `docker_route_blocks_without_approval` | Docker route is blocked without `--allow-docker` | Constraint |
| `mock_ai_docker_route_blocks_without_approval` | AI-related Docker route is gated, then runs fixed mock output inside a no-network container after approval | Constraint + contract |
| `live_ai_route_blocks_without_approval` | Live-AI route is blocked without `--allow-live-ai` | Constraint |
| `external_project_registers_fixture_route` | External manifest route passes and records manifest hash | Relationship / impact + contract |
| `route_prompt_bundle_is_hashable_and_low_noise` | Prompt bundle has route/prompt hashes and no raw context leak | Contract + constraint |

The point is not that one command is impressive. The point is that the same
demo answers all five development-system questions without reading chat
history:

```text
intent      -> scenario ids and external route ids
contract    -> test_flow_route, allowed lanes, flags, evidence ids
relationship / impact -> external project root plus manifest hash
process     -> scenario manager run reports
constraint  -> selected-lane alerts and action precheck
```

## Expected Highlights

The fixture-only route should pass:

```json
{
  "id": "fixture_only_route_runs",
  "status": "passed",
  "decision": "fixture_only",
  "model_calls": "forbidden",
  "calls_models": false
}
```

The Docker and live-AI routes should fail closed without operator approval:

```json
{
  "id": "docker_route_blocks_without_approval",
  "status": "blocked",
  "requires_flags": ["--allow-docker"],
  "command_summaries": []
}
```

When the operator explicitly approves Docker, the mock-AI Docker route uses a
preloaded local image and refuses implicit pulls:

```bash
node scripts/test-scenario-manager.mjs run \
  --scenario mock_ai_docker_fixture \
  --allow-docker \
  --json
```

The structured-output proof runs inside `docker run --network none
--pull=never`, clears AI credential env vars, and emits `runtime: "docker"` and
`calls_models: false`.

The dashboard mock-AI route is browser-verifiable without provider calls:

```bash
node frontend/dashboard/scripts/e2e-demo-mock-ai.mjs
```

It starts the dashboard on a temporary dev port, mocks the governance API, and
asserts that these evidence cards are visible: `Observer alert received`,
`Expert review`, `Test route`, and `Final drift prompt`.

```json
{
  "id": "live_ai_route_blocks_without_approval",
  "status": "blocked",
  "requires_flags": ["--allow-live-ai"],
  "command_summaries": []
}
```

The external route proof should bind the target project root to a
source-controlled manifest hash:

```json
{
  "source": "external_project_manifest",
  "project_id": "paradigm-external-demo",
  "trust_level": "source_controlled"
}
```

The prompt bundle proof should expose hashes, not raw context:

```json
{
  "service_id": "route.prompt_alert_bundle",
  "route_context_hash": "sha256:...",
  "prompt_contract_hash": "sha256:...",
  "raw_context_exposed": false,
  "action_precheck_allowed": false
}
```

## Why This Demo Matters

Most agent demos show a model producing code. This one shows a development
system deciding what context and verification lane the agent is allowed to use.

That is the paradigm boundary:

```text
Memory can suggest a route.
Governed project state must authorize the route.
Route context must be visible, hashable, scoped, and cleared by evidence.
```

The demo keeps the user-facing story small: run one command and inspect the
proof cases. Underneath, it proves intent, contract, relationship / impact,
process, and constraint as separate authority layers.
