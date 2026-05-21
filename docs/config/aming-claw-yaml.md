# .aming-claw.yaml Schema Reference

`.aming-claw.yaml` is the project metadata and runtime routing contract used by
bootstrap, graph reconcile, dashboard project management, testing, and AI role
routing. Keep it at the workspace root.

Do not put graph adaptation rules in this file. Language/framework-specific
review-context rules live in the companion
`.aming-claw/reconcile/semantic_enrichment.yaml` config documented in
[semantic-enrichment.md](semantic-enrichment.md).

## Required Fields

```yaml
project_id: my-project
language: python
```

- `project_id`: kebab-case governance project id.
- `language`: primary language hint. Mixed-language projects are still scanned
  by the language adapters.

## Testing

```yaml
testing:
  unit_command: "python -m pytest"
  e2e_command: "npm run e2e"
  allowed_commands:
    - executable: "python"
      args_prefixes: ["-m pytest", "-m unittest"]
  e2e:
    auto_run: false
    default_timeout_sec: 900
    max_parallel: 1
    require_clean_worktree: true
    evidence_retention_days: 14
    suites:
      dashboard.semantic.safe:
        label: "Dashboard semantic trunk safe path"
        command: "node frontend/dashboard/scripts/e2e-trunk.mjs --reset --skip-dashboard"
        auto_run: false
        live_ai: false
        mutates_db: true
        requires_human_approval: false
        isolation_project: "dashboard-e2e-fixture"
        timeout_sec: 900
        trigger:
          paths:
            - "agent/governance/**"
            - "frontend/dashboard/**"
          tags: ["dashboard", "semantic"]
      dashboard.static.production:
        label: "Dashboard production static route"
        command: "node frontend/dashboard/scripts/e2e-trunk.mjs --probe --static-route --build-dashboard --dashboard http://localhost:40000/dashboard"
        auto_run: false
        live_ai: false
        mutates_db: false
        requires_human_approval: false
        isolation_project: "aming-claw"
        timeout_sec: 300
        trigger:
          paths:
            - "agent/governance/server.py"
            - "frontend/dashboard/**"
          tags: ["dashboard", "static-route", "production"]
```

`allowed_commands` is the command safety allowlist used by bootstrap and
project registration checks.

`testing.e2e` is the dashboard-visible suite registry. It is deliberately
separate from the legacy single `e2e_command`: each suite declares its command,
whether it may auto-run, whether it uses live AI or mutates governance DB state,
and the path/node/tag triggers that make previous evidence stale or relevant.
Live AI suites should keep `auto_run: false` and
`requires_human_approval: true`.
Production static-route suites can build `frontend/dashboard/dist` and probe
`/dashboard` on the governance origin; they should remain non-mutating and safe
to run before a manual smoke test.

E2E evidence and impact use:

```http
GET  /api/projects/{project_id}/e2e/config
POST /api/graph-governance/{project_id}/snapshots/{snapshot_id}/e2e/evidence
GET  /api/graph-governance/{project_id}/snapshots/{snapshot_id}/e2e/impact
```

The evidence endpoint records the suite result with covered file hashes and L7
feature hashes. The impact endpoint compares that ledger to a later graph
snapshot so the dashboard can show `current`, `stale`, `missing`, or `failed`
without guessing from filenames alone.

## Graph Governance

```yaml
governance:
  enabled: true
  test_tool_label: "pytest"
  exclude_roots:
    - "examples"

graph:
  exclude_paths:
    - "docs/dev"
    - ".worktrees"
  ignore_globs:
    - "**/node_modules/**"
    - "**/dist/**"
  nested_projects:
    mode: "exclude"
    roots:
      - "examples/dashboard-e2e-demo"
```

`governance.exclude_roots` is the legacy path-prefix list. `graph.exclude_paths`
is the v2 graph-scanner path-prefix list. They are merged into
`effective_exclude_roots` along with `graph.nested_projects.roots` when
`nested_projects.mode` is `exclude`.

Use `graph.exclude_paths` for generated artifacts, local worktrees, nested demo
projects, and docs/dev handoff scratch space that should not become governed L4
or L7 nodes in the parent project.

### Companion Semantic Enrichment Config

The graph scanner uses `.aming-claw.yaml` for project-level scan/runtime
metadata. It loads graph/semantic adaptation rules from:

```text
.aming-claw/reconcile/semantic_enrichment.yaml
```

Use that companion config to adapt review context for project-specific language
and framework behavior. Examples include requiring direct TypeScript symbol
imports before a test becomes strong coverage, downgrading path-only Python
mentions to weak evidence, or suppressing schema-version string literals that
should not become event edges.

See [semantic-enrichment.md](semantic-enrichment.md) for the rule schema,
supported predicates, proposal gate, and upstream registered-function path.

## AI Routing

```yaml
ai:
  routing:
    pm:
      provider: "openai"
      model: "gpt-5.5"
    dev:
      provider: "openai"
      model: "gpt-5.5"
    tester:
      provider: "openai"
      model: "gpt-5.4"
    qa:
      provider: "openai"
      model: "gpt-5.5"
    semantic:
      provider: "anthropic"
      model: "claude-opus-4-7"
```

Dashboard reads this block through `GET /api/projects/{project_id}/config` and
`GET /api/projects/{project_id}/ai-config`. Operators can update only this
block through `POST /api/projects/{project_id}/ai-config` with a `routing`
object; the backend writes it back to `.aming-claw.yaml` / `.aming-claw.json`
and leaves other config sections intact. Execution still applies the existing
runtime routing stack until role launchers consume the project-level routing
directly.

AI routing decides which local provider/model handles each job. It does not
make AI output trusted state. Semantic and graph-configuration proposals still
need precheck, server validation, Review Queue approval, and reconcile.

## Dashboard Branch / Ref Selection

```http
GET  /api/projects/{project_id}/git-refs
POST /api/projects/{project_id}/git-ref
```

The ref selector is dashboard metadata for graph operations. `POST /git-ref`
validates that the requested branch/ref resolves to a commit and persists it in
the project registry as `selected_ref`; it does not run `git checkout`.
Branch-aware graph history and semantic projection rules are separate schema
work.

## Complete Example

```yaml
version: 2
project_id: dashboard-demo
name: "Dashboard Demo"
language: typescript

testing:
  unit_command: "npm test"
  e2e_command: "npm run e2e"
  e2e:
    auto_run: false
    suites:
      dashboard.semantic.safe:
        command: "npm run e2e -- --suite dashboard.semantic.safe"
        live_ai: false
        mutates_db: true
        trigger:
          paths:
            - "src/**"
            - "tests/e2e/**"

governance:
  enabled: true
  test_tool_label: "vitest"

graph:
  exclude_paths:
    - "node_modules"
    - "dist"
  nested_projects:
    mode: "exclude"
    roots: []

ai:
  routing:
    pm: { provider: "openai", model: "gpt-5.5" }
    dev: { provider: "openai", model: "gpt-5.5" }
    semantic: { provider: "anthropic", model: "claude-opus-4-7" }
```

Optional companion graph-rule override:

```yaml
# .aming-claw/reconcile/semantic_enrichment.yaml
graph_enrich_config_ops:
  rules:
    tests.test_import_fanin.require_direct_symbol_import:
      op: tighten_rule
      edge: tests
      source_evidence: test_import_fanin
      action: require_direct_symbol_import
      downgrade_to: weak_tests
      reason: Require direct test symbol import; preserve weak fan-in evidence.
      when:
        all:
          - predicate: source_evidence_is
            value: test_import_fanin
```

Commit both config files and run Update Graph before expecting dashboard or MCP
graph queries to reflect the new project contract.
