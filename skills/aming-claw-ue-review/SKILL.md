---
name: aming-claw-ue-review
description: Use when reviewing user-facing Aming Claw flows against the bundled ue_audit.v1 contract, especially ordinary non-technical builder flows where request and state visibility matter.
---

# Aming Claw UE Review

Use this skill for user-facing product surfaces that need the V1 UE audit
contract. It is a governance review helper, not an external dependency for the
contract module.

## Contract

Use `ue_audit.v1` from:

```text
agent/governance/contract_templates/ue_audit.v1.json
```

The bundled expert source is:

```text
aming_claw.bundled_ue_expert.v1
```

If an external UE skill is unavailable, continue with the bundled governance
profile. Record the expert source in the audit output.

## Inputs

Collect:

- target user type
- skill level
- job to be done
- product surface
- flow or scenario
- task stage
- artifact refs
- constraints
- non-goals
- success criteria

For ordinary vibe-coding builders, assume the primary need is seeing their own
requests and request states unless the product owner supplies a sharper
persona.

## Review Dimensions

Check:

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

## Output

Return machine-readable findings. Each finding includes severity, screen or
flow, user impact, evidence refs, recommendation, and acceptance impact.

The gate decision must be one of:

- `pass`
- `pass_with_followups`
- `block`

Do not auto-approve a UI from AI review alone. Do not replace product owner
judgment. Do not block non-user-facing backend work by default.
