# Docker AI E2E State Manager

This document defines the reusable state contract for Docker-backed AI E2E
checks. The first implementation wires the install audit harness into the
shared state/report shape, while update, new-feature, and external-project
lanes remain dependency-aware extension points.

## Goals

- Reuse authenticated container/home state without leaking auth material.
- Distinguish install, update, new-feature, and external-project governance
  lanes.
- Record before/after state so a later run can decide whether to rerun, reuse,
  reset, or block.
- Keep Aming Claw self-install as the first provider without hard-coding the
  contract to this repository.
- Produce sanitized JSON evidence that can be validated without Docker.

## Lane State Machine

Every lane has a normalized state:

```text
unknown -> planned -> dependency_ready -> running -> pass
                                      -> fail
                                      -> blocked
                                      -> skipped
                                      -> reused
```

A lane cannot claim `pass` from model text alone. Deterministic command
evidence and validator checks must support the result.

## Lanes

Install proves a fresh plugin/runtime install while reusing read-only host auth
when available. It records the source checkout/ref, plugin/runtime state,
dashboard health, command evidence, and report validation.

Update proves upgrade from a previous known-good baseline to the target commit.
If the container is already at the target commit, it must reset to a configured
previous baseline before running the update lane.

New-feature validates feature-specific behavior on top of a current container.
If the container commit is behind target, the lane blocks with
`new_feature_lane_requires_current_updated_container` and asks for the update
lane first. The observer command pending reminder/callback belongs here.

External-project reuses the same state model for projects governed by Aming
Claw. Providers must supply project id, workspace source, target ref/commit,
bootstrap policy, graph/reconcile policy, AI routing expectations, fixture data
policy, suite registry, cleanup/reset policy, and evidence mapping.

## Durable State

The `state_manager` report section uses
`docker_ai_e2e_state_manager.v1`. Each lane stores:

- container id, image name, image digest, dirty/reset status;
- host lane: `codex`, `claude`, or `both`;
- auth mode/readiness and redacted auth evidence labels;
- source repository, ref, starting commit, target commit, resulting commit;
- installed plugin/runtime/governance schema state;
- governance URL and health probe result;
- target project graph state when applicable;
- last passing evidence status, commit, report path, and validation time.

The contract separates CLI detection from real auth proof. A version check can
show that `codex` or `claude` exists, but it never proves a usable
authenticated session.

## Dependency Rules

- New-feature depends on an updated container at the target commit.
- Update depends on a previous known-good baseline when the container is
  already current.
- Install reuses authenticated host state while reinstalling plugin/runtime
  state from scratch.
- External-project requires explicit provider configuration before bootstrap or
  graph-backed assertions.

## Impact Planning

The planner receives changed files and requested lanes. It marks each lane as
selected or skipped with deterministic reasons.

Default triggers:

- install: Docker audit harness, plugin packaging, skill/plugin manifests, and
  installer/runtime packaging files;
- update: governance/runtime/MCP/scripts/package surfaces;
- new-feature: registered feature triggers, including observer command pending;
- external-project: `.aming-claw.yaml`, project governance/profile/config
  contract files.

When no changed file matches a lane and no explicit request selects it, the
lane records `no_changed_files_matched_lane_triggers`.

## Provider Contract

Providers normalize external state into the same lane/report shape:

```json
{
  "schema_version": "docker_ai_e2e_provider.v1",
  "provider_id": "aming-claw-self-install",
  "adapter": "self_install",
  "project_id": "aming-claw",
  "workspace_source": {
    "type": "git",
    "repo_url": "https://github.com/amingclawdev/aming-claw",
    "ref": "main",
    "mount_path": "/plugin-source"
  },
  "bootstrap": {
    "policy": "none",
    "graph_reconcile": "skip",
    "require_clean_worktree": true
  },
  "ai_routing_expectations": {
    "hosts": ["codex", "claude"],
    "semantic": null
  },
  "fixture_data": {
    "policy": "ephemeral",
    "root": ""
  },
  "suite_registry": {
    "install": ["docker-hn-install-audit"],
    "update": [],
    "new-feature": ["observer_command_pending"],
    "external-project": []
  },
  "cleanup": {
    "policy": "ephemeral_container"
  },
  "evidence_mapping": {
    "report_field": "state_manager",
    "project_graph_field": "lanes.*.after.project_graph"
  }
}
```

Future external-project adapters can implement source checkout, project
bootstrap, graph/reconcile, AI routing checks, fixture generation, suite
selection, cleanup/reset, and evidence mapping without changing lane semantics.

## Evidence And No-Token Policy

- Auth files are mounted read-only and reported only as redacted labels.
- Token-looking values are rejected by the validator.
- Report sanitization redacts token, secret, credential, password,
  authorization, and cookie fields.
- Reports may contain command stdout/stderr samples only after sanitization.
- No lane may report full Docker success unless that lane actually ran.

## First Implementation

The first code implementation adds
`docker/hn-install-audit/common/state-manager.mjs` and wires it into the
existing install audit report. It provides lane state normalization, provider
config validation, dependency planning, impact planning, report sanitization,
token leak detection, and validator self-test coverage.

The install audit runner accepts `--changed-files` and passes those paths to
the impact planner. Full update, rollback, and external-project execution are
not implemented in this first merge.
