# Proposal: Dashboard E2E Governance Ledger And Autorun Policy

**Date**: 2026-05-12
**Status**: Draft v1 for review
**Audience**: Observer, dashboard implementers, governance API implementers
**Backlog**:
- `OPT-E2E-SEMANTIC-REJECT-LIVE-PATH` (P1)
- `OPT-E2E-EVIDENCE-LEDGER-HASH-STALE` (P1)
- `OPT-E2E-SUITE-REGISTRY-AUTORUN-CONFIG` (P2)
- `OPT-E2E-IMPACT-PLANNER-DASHBOARD` (P2)

## 1. Summary

Dashboard E2E coverage should become governed evidence, not just runnable
scripts. The system should know:

1. Which E2E suite proves which feature/API/operator path.
2. Which file, node, feature, and later symbol hashes were covered when it
   passed.
3. Whether that proof is still current after a new commit and scope reconcile.
4. Which suites may run automatically and which require explicit approval.

The immediate driver is the AI semantic jobs path. Queueing, cancellation,
review accept, review reject, and semantic projection mount correctness are
core product behavior. A rejected semantic proposal must not appear as applied
content, including the previously observed bad state where a rejected proposal
looked active under `hash-unverified`.

## 2. Priority Order

### P1. Semantic Reject Live E2E

Extend the live semantic E2E path to support explicit decision modes:

```text
--semantic-decision accept | reject | both
```

The reject mode must assert:

- The feedback row is resolved as rejected.
- The linked semantic event is rejected.
- The semantic job row is rejected or terminal.
- The projection is rebuilt.
- The target node has no payload from the rejected event.
- No other node is projected from the rejected event.
- The dashboard/API state does not show rejected content as
  `hash-unverified` applied semantic content.

Default E2E remains safe: no live AI call unless explicitly requested.

### P1. E2E Evidence Ledger

Persist E2E pass/fail evidence keyed by suite, commit, snapshot, and covered
hashes. This gives the dashboard and chain a deterministic answer to:

```text
Has the E2E proof for this feature gone stale since it last passed?
```

### P2. Suite Registry And Autorun Policy

Add named suites to `.aming-claw.yaml` so project owners can configure what can
run automatically, what is only suggested, and what requires human approval.

### P2. Dashboard Impact Planner

Expose a dashboard/API view that says which suites are current, stale,
required, blocked, or recommended for the active commit/snapshot.

## 3. Existing Substrate

The current system already has the pieces needed for an MVP:

- Graph snapshots bind graph state to commit SHA.
- Governance index feature rows bind nodes to primary/test/doc/config files.
- Feature hashes already include bound file hashes.
- File inventory records per-file hashes.
- Dashboard E2E scripts already create isolated fixture projects.
- `.aming-claw.yaml` already has `testing.unit_command`,
  `testing.e2e_command`, and `testing.allowed_commands`.

The gap is that E2E scripts do not write durable pass evidence, and the graph
does not yet use that evidence to mark suites stale after changes.

## 4. Data Model

### 4.1 E2E Suite Registry

The registry is project configuration. It is read by governance APIs,
dashboard, and future runners.

```yaml
testing:
  e2e:
    auto_run: "off"       # off | suggest | safe | gated | all
    default_timeout_sec: 600
    max_parallel: 1
    require_clean_worktree: true
    evidence_retention_days: 14

    suites:
      dashboard.semantic.safe:
        command: "npm run e2e:trunk -- --reset"
        trigger:
          paths:
            - "frontend/dashboard/**"
            - "agent/governance/server.py"
            - "agent/governance/reconcile_feedback.py"
          tags:
            - semantic_jobs
            - feedback_decision
            - projection
        auto: true
        live_ai: false
        mutates_db: true
        requires_human_approval: false
        isolation_project: "dashboard-e2e-fixture"

      dashboard.semantic.live.reject:
        command: "npm run e2e:trunk -- --semantic-live --semantic-decision reject"
        trigger:
          tags:
            - semantic_jobs
            - ai_review
            - reject_decision
            - projection
        auto: false
        live_ai: true
        mutates_db: true
        requires_human_approval: true
        isolation_project: "dashboard-e2e-fixture"
```

### 4.2 E2E Evidence Row

Evidence rows can start as JSON companion state and later move into SQLite if
query volume justifies it.

```json
{
  "suite_id": "dashboard.semantic.live.reject",
  "project_id": "aming-claw",
  "ref_name": "main",
  "commit_sha": "5d78bf745208fdbf080ac0e2d6c2fdd6f502185a",
  "snapshot_id": "scope-5d78bf7-6847",
  "command": "npm run e2e:trunk -- --semantic-live --semantic-decision reject",
  "status": "passed",
  "passed_at": "2026-05-12T00:00:00Z",
  "duration_ms": 120000,
  "artifact_path": ".aming-claw/e2e-artifacts/run-123/report.json",
  "covered_files": [
    {
      "path": "frontend/dashboard/src/App.tsx",
      "file_hash": "sha256:..."
    }
  ],
  "covered_nodes": [
    {
      "node_id": "L7.175",
      "feature_hash": "sha256:..."
    }
  ],
  "covered_symbols": [],
  "safety": {
    "live_ai": true,
    "mutates_db": true,
    "isolation_project": "dashboard-e2e-fixture"
  }
}
```

### 4.3 Status Values

```text
current   Last passing evidence still matches covered hashes.
stale     Covered file, feature, or symbol hash changed.
missing   No evidence exists for a required suite.
failed    Latest run failed.
blocked   Suite cannot run because services, config, budget, approval, or
          isolation prerequisites are missing.
skipped   Suite was intentionally skipped with an explicit reason.
```

## 5. Staleness Rules

MVP uses file and feature hashes:

1. Load the active snapshot's file inventory and feature index.
2. For each evidence row, compare `covered_files[].file_hash`.
3. Compare `covered_nodes[].feature_hash`.
4. If any covered hash differs, mark the evidence `stale`.
5. If the covered node no longer exists, mark `stale` with reason
   `covered_node_missing`.
6. If a suite trigger matches changed files but no evidence exists, mark
   `missing`.

Later V2 can add symbol/function hashes:

- `function_lines` already gives stable symbol locations.
- A future symbol index can store `symbol_hash` or `function_body_hash`.
- Symbol hashes should refine, not replace, file/feature staleness because E2E
  validates cross-file contracts.

## 6. Impact Planner

Inputs:

- Active project id and selected ref.
- Active graph snapshot and commit.
- Scope reconcile file delta.
- Suite registry.
- E2E evidence ledger.
- Governance index feature/file hashes.

Output:

```json
{
  "project_id": "aming-claw",
  "snapshot_id": "scope-5d78bf7-6847",
  "commit_sha": "5d78bf745208fdbf080ac0e2d6c2fdd6f502185a",
  "suites": [
    {
      "suite_id": "dashboard.semantic.live.reject",
      "status": "stale",
      "priority": "required",
      "reasons": [
        {
          "kind": "changed_file",
          "path": "frontend/dashboard/src/App.tsx"
        },
        {
          "kind": "covered_feature_hash_changed",
          "node_id": "L7.175"
        }
      ],
      "command": "npm run e2e:trunk -- --semantic-live --semantic-decision reject",
      "live_ai": true,
      "requires_human_approval": true,
      "can_autorun": false
    }
  ]
}
```

## 7. Autorun Policy

`testing.e2e.auto_run` controls behavior:

| Mode | Behavior |
|---|---|
| `off` | Never run automatically. Only compute status. |
| `suggest` | Show required/recommended suites in dashboard. |
| `safe` | Auto-run only suites with `live_ai=false`, isolated project, and no human approval requirement. |
| `gated` | Auto-run safe suites; queue approval prompts for live/costly/mutating suites. |
| `all` | Run any matching suite allowed by config. Intended only for sandbox/CI. |

Hard safety rules:

- `live_ai=true` never autoruns unless policy is `gated` with approval or
  `all`.
- Suites without `isolation_project` do not autorun.
- Dirty worktree blocks autorun when `require_clean_worktree=true`.
- Runner must clear/cancel semantic jobs it creates unless the suite explicitly
  declares durable side effects.
- Failed E2E may file or suggest backlog, but backlog creation is controlled by
  a separate policy.

## 7.1 Scenario Manager Fixture Lanes

`scripts/test-scenarios.json` declares service-router fixture lanes and the
scenario manager surfaces the selected test-flow route in `plan` and `run`
reports as `test_flow_route`.

- `service_router_docker_fixture` is a deterministic Docker fixture lane. It is
  blocked by default and requires `--allow-docker`; after approval the manager
  probes Docker readiness before any command evidence is trusted.
- `service_router_ai_structured_output_fixture` is a deterministic
  structured-output lane. It uses fixture JSON only, declares model calls
  forbidden, and records live AI as manual/auth-unknown rather than invoking a
  provider.

The scenario manager preserves lane metadata in `plan` and `run` reports.
Live-AI validation remains outside this deterministic runner until project AI
config, local CLI auth, and operator approval are verified.

The `test_flow_route.prompt_alert_bundle` is intentionally low-noise: it emits
only the alerts for lanes selected by the current scenario. Focused unit tests
and fixture-only checks produce informational alerts. E2E fixture checks warn
about isolation and retained evidence. Docker fixture, live-AI probe, and
external graph fixture lanes produce blocking alerts until their explicit
approval flags are present.

### 7.2 Live-AI Environment Probe Lane

Live-AI environment validation is a separate manual lane, not an extension of
the deterministic structured-output fixture. The fixture lane proves local
schema/router behavior with model calls forbidden. The live-AI environment lane
proves whether the operator-approved local runtime matches project AI routing.

The impact planner classifies an explicit live-AI environment suite as
`manual`, `live_ai`, and `environment-check`. It must not become silent
autorun, even when a suite entry sets `auto_run: true` or the project E2E
policy enables autorun. Actual invocation requires an explicit operator command
with `--allow-live-ai`.

Readiness evidence and invocation evidence are distinct:

- Readiness evidence may inspect project AI config, configured provider/model
  for the requested role, CLI path/version, catalog membership, and auth state.
  Version detection is only `auth unknown`; it does not prove a usable account.
- Invocation evidence exists only when the approved command includes
  `--allow-live-ai` and the probe records an actual provider call result.
  Missing invocation evidence must not be treated as a passing live-AI check.

Live-AI evidence must compare the expected route from project AI config with
the observed local state:

- expected role, provider, and model, for example `tester` -> `openai` /
  `gpt-5.4`;
- detected CLI path and version for the provider;
- model catalog membership for the expected model;
- auth or invocation result, reported separately from CLI detection.

Evidence artifacts must be sanitized before they enter the ledger, timeline, or
dashboard. Store provider, model, role, command shape, status, duration, and
redacted diagnostics; do not store raw prompts, completions, API keys, bearer
tokens, session tokens, or unredacted stderr/stdout from provider CLIs.

## 8. API Sketch

Read configuration:

```http
GET /api/projects/{project_id}/e2e/config
```

Read impact:

```http
GET /api/graph-governance/{project_id}/snapshots/{snapshot_id}/e2e/impact
```

Record evidence:

```http
POST /api/graph-governance/{project_id}/snapshots/{snapshot_id}/e2e/evidence
```

Optional future run endpoint:

```http
POST /api/graph-governance/{project_id}/snapshots/{snapshot_id}/e2e/runs
```

The run endpoint should be added only after the ledger and safety policy are
stable. Manual CLI execution plus evidence upload is enough for the first cut.

## 9. Dashboard UX

Add an "E2E Evidence" panel near project/operations state:

- Current suites: last pass, commit, age, artifact link.
- Required suites: why required, command, safety flags.
- Stale suites: changed file/node/hash reasons.
- Blocked suites: missing service/config/approval/isolation reason.
- Manual run controls for safe suites.
- Approval controls for live AI suites.

Do not bury this under generic test output. It is part of graph confidence.

## 10. Implementation Slices

### Slice 1: Reject E2E Contract

- Add decision mode to `frontend/dashboard/scripts/e2e-trunk.mjs`.
- Add reject assertions against feedback, linked event, job row, projection, and
  node payload.
- Keep default safe behavior.

### Slice 2: Evidence Writer

- Add a small evidence JSON writer to the E2E harness.
- Record covered files and nodes from the active snapshot bundle.
- Store artifacts under the fixture or project `.aming-claw/e2e-artifacts`.

### Slice 3: Governance Staleness Reader

- Add an API/helper that compares evidence hashes to the active governance
  index.
- Return current/stale/missing/failed/blocked statuses.

### Slice 4: Config Schema

- Extend `.aming-claw.yaml` docs and parser for `testing.e2e.suites`.
- Validate allowed commands and safety flags.

### Slice 5: Dashboard Impact Panel

- Render impact statuses and manual run guidance.
- Do not autorun yet; show commands and approval requirements.

## 11. Open Questions

- Should evidence live first in snapshot companion files, SQLite, or both?
  Recommendation: companion JSON first, SQLite later when dashboard filtering
  needs it.
- Should suite triggers bind to tags, nodes, paths, or all three?
  Recommendation: all three, but MVP can implement paths and node ids first.
- Should live AI E2E be part of normal local MVP acceptance?
  Recommendation: no. It should be opt-in and approval-gated.
- How should failed E2E file backlog?
  Recommendation: produce a backlog draft first; automatic filing is a later
  policy flag.
