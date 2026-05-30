# Test Scenario Manager V1

<!-- governance-hint {"binding":{"path":"scripts/test-scenarios.json","role":"config","target_module":"scripts.test-scenario-manager","target_title":"scripts.test-scenario-manager"}} -->

The test scenario manager is a small reusable runner for product capability
checks that need state, evidence, and repeatable reports. It is intentionally
outside the governance executor path: it runs from the checkout, writes state
under a caller-provided or temp directory, and keeps external project material
in a temp/cache workspace instead of this repository.

```bash
node scripts/test-scenario-manager.mjs doctor --json
node scripts/test-scenario-manager.mjs plan --scenario simple_user_entry --json
node scripts/test-scenario-manager.mjs run --scenario simple_user_entry --json
node scripts/test-scenario-manager.mjs report --json
```

## Contract

The CLI modes are:

- `doctor`: parse and validate the registry, show scenario ids, state/cache
  paths, and dependency plans. It does not mutate state.
- `plan`: show the selected scenario commands, dependencies, target project,
  target ref, and bootstrap decisions. It does not mutate state.
- `run`: execute the selected scenario or all scenarios. It writes a per-run
  report and updates `state.json`. `--dry-run` records the same state shape
  without executing commands or HTTP mutations.
- `report`: read `state.json` and the latest or requested report.

`--json` emits machine-readable JSON. Without it, the CLI prints a compact
human summary. `run` exits nonzero for `failed` or `blocked` reports while
still writing the report.

Useful flags:

- `--scenario <id>` selects one scenario.
- `--state-dir <path>` controls where `state.json` and `reports/*.json` are
  written. The default is under the system temp directory.
- `--cache-dir <path>` controls external project cache workspaces. The manager
  refuses to use a cache directory inside this repo.
- `--dry-run` records plans and skipped command summaries without executing.
- `--allow-network` permits external clone/fetch work for scenarios that need
  it.
- `--allow-bootstrap` permits governance bootstrap HTTP calls that mutate the
  local governance project registry and graph state.
- `--governance-url <url>` defaults to `http://127.0.0.1:40000`.
- `--project-root <path>` points at an external governed project that owns
  source-controlled test route declarations.
- `--route-manifest <path>` loads an external test route manifest. The alias
  `--test-route-manifest <path>` is accepted for governance wording.

When `--project-root` is provided without an explicit manifest path, the manager
auto-discovers the first existing source-controlled manifest at:

```text
<project-root>/.aming-claw/test-routes.json
<project-root>/.aming-claw-test-routes.json
<project-root>/aming-claw-test-routes.json
```

The external manifest schema is versioned as `schema_version: 1` and has a
top-level `project_id` plus `routes[]`. Each route materializes as a normal
scenario merged with `scripts/test-scenarios.json`; duplicate ids are rejected.
Routes use the same runner, dependency, fixture, command, and evidence shapes
as built-in scenarios, with additional registration fields:

```json
{
  "schema_version": 1,
  "project_id": "target-project",
  "routes": [
    {
      "id": "target.fixture.route",
      "title": "Target fixture route",
      "lane": "fixture_only",
      "runner": "commands",
      "lifecycle": "stable",
      "side_effect_class": "read",
      "commands": [
        {"id": "metadata", "cwd": "external_project", "command": ["{node}", "-e", "console.log('ok')"]}
      ],
      "dependencies": [],
      "fixtures": [],
      "evidence_requirements": []
    }
  ]
}
```

Every materialized external route includes `route_registration` in `plan` and
`run` reports, and the same object is nested under
`test_flow_route.route_registration`:

```json
{
  "source": "external_project_manifest",
  "project_id": "target-project",
  "project_root": "/path/to/target-project",
  "manifest_path": "/path/to/target-project/.aming-claw/test-routes.json",
  "manifest_hash": "sha256:<manifest-bytes>",
  "route_id": "target.fixture.route",
  "trust_level": "source_controlled",
  "lifecycle": "stable",
  "side_effect_class": "read"
}
```

For external manifest routes, command `cwd` values of `external_project` and
`project` resolve to `project_root`. Built-in repo scenarios keep their current
behavior: `repo` and unspecified cwd resolve to this repository, and `project`
falls back to this repository unless route registration names an external root.

## Test Flow Route

`plan` and `run` reports include `test_flow_route`. This is the route-owned
summary that tells observer, worker, and QA lanes what kind of verification is
being selected without loading a noisy role prompt.

The manager infers selected lanes from the scenario runner, dependencies,
fixtures, execution policy, safety metadata, and commands:

- `focused_unit`: deterministic local behavior can be checked with focused
  tests, usually pytest.
- `fixture_only`: realistic local envelopes, config, or state are needed, but
  no external runtime, Docker, or model call is allowed.
- `e2e_fixture`: dashboard/API/operator paths or governance state projection
  behavior needs an isolated E2E fixture.
- `docker_fixture`: container boundary, Docker networking, install isolation,
  or Docker-backed service routing is part of the behavior.
- `live_ai_environment_probe`: provider/model/CLI/auth readiness is being
  checked with explicit operator approval.
- `external_graph_fixture`: a pinned external repository is needed to validate
  graph adapter/bootstrap behavior.

The `prompt_alert_bundle` inside `test_flow_route` emits alerts only for the
selected lanes. Docker and live-AI lanes are blocking alerts because they need
explicit flags. Fixture-only and focused-unit lanes are informational alerts
because they are deterministic and should stay local.

External manifests may declare `lane` explicitly. That lane is treated as the
route owner decision for `test_flow_route.decision`, `primary_lane`, alerts,
default autorun, model-call policy, and side-effect class. This is intentional:
route alerts are a compact safety contract for observer/worker handoff, not a
command-name heuristic. A fixture-only external route with no pytest or E2E
command still reports `fixture_only`, `model_calls: forbidden`, and
`autorun: true`.

## Scenario Schema

The registry lives at `scripts/test-scenarios.json`:

```json
{
  "schema_version": 1,
  "scenarios": [
    {
      "id": "simple_user_entry",
      "target_project": "aming-claw",
      "target_ref": "HEAD",
      "runner": "commands",
      "dependencies": [],
      "commands": [
        {
          "id": "focused_pytest",
          "cwd": "repo",
          "command": ["{python}", "-m", "pytest", "agent/tests/..."],
          "timeout_ms": 120000
        }
      ]
    }
  ]
}
```

V1 supports two runner types:

- `commands`: run local commands from the repo, record sanitized stdout/stderr
  tails, exit status, timestamps, and duration.
- `ruby_graph`: prepare a pinned external Ruby project, optionally bootstrap it
  through governance, then validate graph status and graph-query responses.

The `{python}` token expands to `PYTHON`, `PYTHON_BIN`, or `python`. `{node}`
expands to the current Node executable.

## State Model

`state.json` contains the latest status per scenario plus a short run index:

```json
{
  "schema_version": 1,
  "last_run_id": "20260528T095300Z-simple_user_entry-abc12345",
  "scenarios": {
    "simple_user_entry": {
      "scenario_id": "simple_user_entry",
      "target_project": "aming-claw",
      "target_ref": "HEAD",
      "target_commit": "<git sha>",
      "last_status": "passed",
      "dependency_decisions": [],
      "artifacts": [],
      "report_path": ".../reports/<run_id>.json"
    }
  },
  "runs": []
}
```

Each report records:

- scenario id, title, target project, target ref, and resolved target commit;
- dependency decisions and remediation text;
- `passed`, `failed`, `blocked`, or `dry_run` status;
- started/completed timestamps and duration;
- artifacts such as report path or external workspace path;
- sanitized command summaries and sanitized HTTP summaries;
- blocker shape with `reason_code`, `reason`, and `remediation`.

The sanitizer redacts common token, password, bearer, secret, and API-key
patterns. Reports intentionally avoid environment dumps.

## Built-In Scenarios

`simple_user_entry` validates the ordinary user entry path for Simple Mode and
Project Inbox. It runs the focused raw requirement test:

```text
agent/tests/test_raw_requirement.py::test_simple_mode_observer_command_flow_for_execution_and_worker_controls
```

It also runs observer session and observer command queue tests:

```text
agent/tests/test_observer_session.py
agent/tests/test_observer_command_queue.py
```

`ruby_graph_sinatra` validates Ruby graph support against Sinatra at pinned
commit `5236d3459b8b9015e5ce21ddd0c6beb0db4081d4`. By default it is
safe: if the cache is missing, network is not allowed, governance is down, or
bootstrap is not explicitly allowed, the scenario writes a `blocked` report
with remediation rather than pretending success.

To run the full Sinatra path:

```bash
node scripts/test-scenario-manager.mjs run \
  --scenario ruby_graph_sinatra \
  --allow-network \
  --allow-bootstrap \
  --json
```

The Ruby validation checks that governance has an active snapshot, graph layer
counts are nonzero, `lib/sinatra/base.rb` resolves, Ruby language evidence is
present, and `function_index` returns Ruby symbol or method evidence.
