# Asset Inbox API Contract

Backlog: `DOGFOOD-ASSET-INBOX-CONTRACT-MOCK-FIRST-20260523`

## Graph Owners

The graph owner check for this slice found the backend and frontend surfaces
that should own the first implementation pass:

- Backend document/file state: `L7.43` (`agent.governance.doc_asset_state`)
- Backend governance index: `L7.57` (`agent.governance.governance_index`)
- Frontend type contract: `L7.221` (`frontend.dashboard.src.types`)
- Frontend API client: `L7.214` (`frontend.dashboard.src.lib.api`)
- Frontend UI entry points for the next pass: `L7.222`
  (`frontend.dashboard.src.views.BacklogView`) and `L7.203`
  (`frontend.dashboard.src.App`)

`wf_impact` is not available for this repo snapshot because the legacy workflow
graph is missing. Treat the active graph node lookup above plus targeted tests
as the current implementation boundary.

## Purpose

Asset Inbox is a read model for file and graph hygiene. It is not a backlog
table and should not become the default container for every orphan file.

The UI needs to separate:

- source files that have no graph owner,
- docs/tests/config files that are unbound,
- weak binding candidates proposed by AI/rules,
- accepted bindings that are already graph state,
- ignored/generated/archive assets,
- stale mapped source whose hash no longer matches the active projection.

Backlog rows are created only from selected actionable assets. Accepted
bindings, not weak candidates, are the only asset state allowed into review
impact scope.

## Source vs Derived State

Source inputs:

- committed source files,
- committed governance hints/config,
- accepted graph/semantic events,
- deterministic file inventory and document asset state artifacts.

Derived state:

- Asset Inbox rows,
- candidate binding proposals,
- dashboard grouping/filtering,
- backlog creation payloads generated from an operator selection.

AI proposals are not trusted graph state. A weak doc/test/config match can be
shown, rejected, waived, or converted into source-controlled evidence such as a
governance hint. Direct graph DB edits are out of scope.

## Mock Fixture

The shared fixture is:

```text
docs/fixtures/asset-inbox-contract-mock.json
```

It covers every V1 status:

- `source_orphan`
- `doc_unbound`
- `doc_candidate`
- `accepted`
- `test_candidate`
- `config_pending_decision`
- `ignored`
- `archive`
- `stale`

The backend precheck helper is:

```text
agent/governance/asset_inbox_contract.py
```

The first contract test is:

```text
agent/tests/test_asset_inbox_contract.py
```

## Read API Shape

Live read endpoint:

```http
GET /api/graph-governance/{project_id}/snapshots/{snapshot_id}/asset-inbox
GET /api/graph-governance/{project_id}/snapshots/active/asset-inbox
```

Response contract:

```json
{
  "schema_version": "asset_inbox.v1",
  "ok": true,
  "project_id": "asset-inbox-fixture",
  "snapshot_id": "scope-fixture-asset-inbox",
  "commit_sha": "abc123",
  "impact_scope_policy": "accepted_bindings_only",
  "backlog_policy": {
    "default_container": false,
    "create_from_selected_assets_only": true
  },
  "summary": {
    "total": 9,
    "by_status": {
      "source_orphan": 1,
      "doc_candidate": 1
    }
  },
  "items": [],
  "batch_actions": []
}
```

The frontend has typed API client methods for this surface. Mutation endpoints
for the batch actions are intentionally separate and are not implied by the read
endpoint.

## Batch Actions

The mock defines these action contracts:

- `queue_asset_binding_proposals`: ask AI/rules to propose doc/test/config
  bindings. This creates reviewable proposals, not graph state.
- `queue_semantic_enrich`: queue semantic work for selected source/stale assets.
- `reject_or_waive_candidates`: resolve weak candidates without source mutation.
- `create_backlog_from_selection`: create a backlog row only from selected
  actionable assets.
- `write_governance_hint`: write source-controlled binding evidence for a
  reviewed doc/test/config asset. This mutates source and requires review.

## Parallel Split

Backend worker contract:

- read file inventory, doc asset state, accepted graph bindings, semantic stale
  state, and weak proposal state;
- materialize `AssetInboxResponse`;
- expose the snapshot read endpoint;
- run `agent/tests/test_asset_inbox_contract.py` plus endpoint tests.

Frontend worker contract:

- consume `AssetInboxResponse` from `frontend/dashboard/src/types.ts`;
- use `docs/fixtures/asset-inbox-contract-mock.json` for mock-first component
  tests;
- render Asset Inbox as a separate surface from Backlog;
- keep batch actions disabled or mocked until live backend endpoints exist.

Gate contract:

- no backlog row is created without explicit selected assets;
- weak candidates never count as accepted impact scope;
- `write_governance_hint` is presented as source mutation and review-required;
- fixture, backend validator, and frontend types stay schema-compatible.
