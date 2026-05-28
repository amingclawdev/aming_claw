# Contract Template Registry

Status: V1 design decision and implementation notes, 2026-05-28

Backlog: UE-AUDIT-CONTRACT-MODULE-20260528

## Decision

Use a reusable source-controlled contract template registry in V1. Do not add a
database-backed mutable template service yet.

## Options Considered

One-off static UE contract:

- Lowest implementation cost.
- Easy to validate for `ue_audit.v1`.
- Does not create a reusable path for later governed contracts.
- Would push list/resolve behavior into ad hoc code or prompts.

Reusable source-controlled registry:

- Keeps templates reviewable in git.
- Supports deterministic loading by `template_id`, `task_type`, `stage`, and
  `version`.
- Gives MCP tools a stable list/resolve surface without adding runtime state.
- Fits current governance patterns where contract templates already live under
  `agent/governance/contract_templates`.

Database-backed mutable service:

- Useful later for project overrides, runtime authoring, approvals, and version
  promotion workflows.
- Adds migration, authorization, review, and rollback burden.
- Not justified for the V1 UE audit contract because templates are governance
  source of truth, not user-authored dashboard content yet.

## V1 Shape

Templates are JSON files in:

```text
agent/governance/contract_templates
```

The registry loads every `*.json` file deterministically, validates the basic
contract metadata, and sorts templates by `template_id`.

Required metadata:

- `schema_version`
- `template_id`
- `version`

Optional resolution metadata:

- `task_types`
- `stages`

The public helpers are:

- `load_contract_templates`
- `list_contract_templates`
- `get_contract_template`
- `resolve_contract_template`

Unknown template ids and malformed templates raise explicit registry errors so
callers can return structured MCP failures instead of silently falling back to
prompt text.

## MCP Surface

The MCP dispatcher resolves templates in process:

- `contract_template_list`: list templates, filtered by task type or stage.
- `contract_template_get`: fetch an exact versioned `template_id`.
- `contract_template_resolve`: resolve by template id, task type, stage, or
  version.
- `ue_audit_validate`: validate UE audit inputs and output against
  `ue_audit.v1`.

The MCP tools intentionally do not mutate templates. Template changes require a
source-controlled code review.

## Deferred Work

A mutable database service is deferred until templates need runtime authoring,
project-specific overrides, approval state, promotion workflows, or dashboard
editing. That later service should preserve source-controlled defaults and make
project overrides explicit.
