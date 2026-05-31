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

Gated live observer evidence is intentionally separate from those smoke routes.
`gated_live_ai_observer_route_demo` fails closed unless `--allow-live-ai` is
present. When approved, it still runs a deterministic local harness: the output
is timeline-shaped observer evidence for route alert acknowledgement, ordered
step outputs, and final drift prompt handling. It is not a provider-call proof,
and it stores no raw prompt output.

Provider-backed observer evidence is a separate Docker live-AI lane.
`docker_live_ai_observer_route_demo` fails closed unless both `--allow-docker`
and `--allow-live-ai` are present. Its approved run goes through
`docker/hn-install-audit/run-install-audit.sh`, writes a
`live_observer_route_result`, and stores transcript hashes/evidence fields
instead of raw prompt output.

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

The JSON report contains eight proof cases:

| Proof case | Expected result | Surface proved |
|---|---|---|
| `fixture_only_route_runs` | `fixture_only` passes with `model_calls: forbidden` | Contract + process |
| `dashboard_mock_ai_playwright_route_declared` | Playwright mock-AI dashboard lane is declared with `model_calls: mocked` | Process + contract |
| `docker_route_blocks_without_approval` | Docker route is blocked without `--allow-docker` | Constraint |
| `mock_ai_docker_route_blocks_without_approval` | AI-related Docker route is gated, then runs fixed mock output inside a no-network container after approval | Constraint + contract |
| `live_ai_route_blocks_without_approval` | Live-AI route is blocked without `--allow-live-ai` | Constraint |
| `docker_live_ai_observer_route_blocks_without_approval` | Docker live-AI observer route is blocked without both Docker and live-AI approval | Constraint + process |
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

The gated live observer route is a separate scenario, not a replacement for
the mock dashboard or Docker routes:

```bash
node scripts/test-scenario-manager.mjs run \
  --scenario gated_live_ai_observer_route_demo \
  --json
```

Without approval it is blocked before commands run. With explicit approval it
executes the deterministic local harness:

```bash
node scripts/test-scenario-manager.mjs run \
  --scenario gated_live_ai_observer_route_demo \
  --allow-live-ai \
  --json
```

Expected approved evidence:

```json
{
  "source": "deterministic_test_harness",
  "live_ai": {
    "approval": "operator-approved/manual",
    "execution_mode": "deterministic_test_harness",
    "calls_models": false
  },
  "observer_evidence": {
    "route_alert_ack": {"status": "acknowledged"},
    "ordered_step_outputs": [
      {"step_id": "01_route_alert_ack", "status": "passed"},
      {"step_id": "02_ordered_route_steps", "status": "passed"},
      {"step_id": "03_sanitized_live_ai_evidence", "status": "passed"}
    ],
    "final_drift_prompt": {"status": "shown"},
    "no_raw_prompt_output": true
  }
}
```

The Docker live-AI observer route is the real provider-call route, so the
default demo proves only that it is registered and blocked without both gates:

```bash
node scripts/test-scenario-manager.mjs run \
  --scenario docker_live_ai_observer_route_demo \
  --json
```

Expected blocked evidence:

```json
{
  "id": "docker_live_ai_observer_route_blocks_without_approval",
  "status": "blocked",
  "decision": "docker_live_ai_observer_route",
  "model_calls": "provider_backed_live_ai",
  "requires_flags": ["--allow-docker", "--allow-live-ai"],
  "command_summaries": []
}
```

An approved run is intentionally explicit because it starts Docker and invokes
a real provider-backed CLI inside the install-audit container:

```bash
node scripts/test-scenario-manager.mjs run \
  --scenario docker_live_ai_observer_route_demo \
  --allow-docker \
  --allow-live-ai \
  --json
```

That route must produce `live_observer_route_result.provider_backed: true`,
acknowledge the route alert, record ordered observer steps and the final drift
prompt, and keep `raw_output_stored: false`. The scenario validates that field
set with `validate-report.mjs --require-live-observer-route`, so route proof
quality is auditable even when the broader install-audit report still records
unrelated demo-suite blockers.

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
