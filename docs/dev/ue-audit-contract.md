# UE Audit Contract

Status: V1 source-controlled governance contract, 2026-05-28

Backlog: UE-AUDIT-CONTRACT-MODULE-20260528

## Purpose

`ue_audit.v1` makes user-experience review a repeatable governance contract.
It is for user-facing flows where the product risk is that the interface hides
what the user asked for, what state the request is in, what action is next, or
why something failed.

The first built-in archetype is the ordinary non-technical vibe-coding builder:
a user who describes product behavior in plain language and primarily needs to
see captured requests, AI interpretation, progress state, failures, and
completion evidence.

## Required Inputs

The fixed V1 contract requires these inputs before an audit can be considered
valid:

- `target_user_type`
- `skill_level`
- `jtbd`
- `product_surface`
- `flow_scenario`
- `task_stage`
- `artifact_refs`
- `constraints`
- `non_goals`
- `success_criteria`

`artifact_refs` should point to the PRD, design, screenshot, route, prototype,
or implementation evidence being reviewed. The validator checks shape only; it
does not call live AI and does not fetch external artifacts.

## Bundled Expert

Governance includes an explicit bundled UE expert profile:

- Source id: `aming_claw.bundled_ue_expert.v1`
- Source type: `bundled_governance_template`
- Source record: `agent.governance.ue_audit_contract:BUNDLED_UE_EXPERT_PROFILE`

This means UE review does not depend on installing an external skill. External
skills can be layered later, but V1 always records the source of the expert
profile used by the contract.

## Dimensions

Every audit should evaluate:

- hierarchy
- object visibility
- status visibility
- next action clarity
- terminology
- error, empty, and loading states
- feedback and progress
- accessibility basics
- mobile and desktop fit
- developer jargon leakage

For Simple Mode and ordinary user flows, the reviewer should bias toward
plain-language state visibility over internal worker, graph, or audit detail.

## Machine Output

Audit output is machine-readable. The top-level gate decision must be one of:

- `pass`
- `pass_with_followups`
- `block`

Each finding must include:

- `severity`
- `screen_flow`
- `user_impact`
- `evidence_refs`
- `recommendation`
- `acceptance_impact`

Severity values are `info`, `minor`, `major`, and `critical`. Blocking means a
user-facing acceptance gate should not close until the finding is resolved or a
product owner explicitly records a waiver.

## Flow Hooks

Recommended V1 hooks:

- PRD/design review: optional but recommended when the target user, JTBD,
  terminology, or success criteria are unclear.
- Pre-frontend implementation: required for user-facing flows before UI code is
  generated or changed.
- Post-implementation screenshot smoke: required when screenshots or equivalent
  render artifacts exist.
- Pre-close gate: required before closing user-facing work with UE impact.

## Non-Goals

The UE audit contract does not replace product owner judgment, does not
auto-approve UI changes solely from AI review, and does not block
non-user-facing backend work by default.

## Local APIs

The in-process governance helpers are:

- `agent.governance.ue_audit_contract.contract_definition`
- `agent.governance.ue_audit_contract.validate_audit_inputs`
- `agent.governance.ue_audit_contract.validate_audit_output`
- `agent.governance.ue_audit_contract.validate_ue_audit_payload`

The MCP surface exposes:

- `contract_template_list`
- `contract_template_get`
- `contract_template_resolve`
- `ue_audit_validate`
