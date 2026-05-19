from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import yaml

from agent.governance import graph_events
from agent.governance import graph_snapshot_store as store
from agent.governance import server
from agent.governance.db import _ensure_schema
from agent.governance.graph_enrich_config_ops import SCHEMA_VERSION
from agent.governance.graph_proposal_index import aggregate_graph_enrich_config_proposals
from agent.governance.graph_proposal_review import (
    apply_graph_enrich_config_observer_override,
)
from agent.governance.reconcile_semantic_config import PROJECT_OVERRIDE_PATH


PID = "graph-proposal-review-test"
SNAPSHOT_ID = "proposal-review-snapshot"
FIXTURE = Path(__file__).parent / "fixtures" / "graph_enrich_config_dogfood_proposals.json"


class _NoCloseConn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


def _conn(tmp_path) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    store.ensure_schema(c)
    graph_events.ensure_schema(c)
    store.create_graph_snapshot(
        c,
        PID,
        snapshot_id=SNAPSHOT_ID,
        commit_sha="proposal-review-commit",
        snapshot_kind="scope",
        graph_json={"deps_graph": {"nodes": [], "edges": []}},
        status=store.SNAPSHOT_STATUS_ACTIVE,
        created_by="test",
    )
    return c


def _dogfood_cluster() -> dict:
    events = json.loads(FIXTURE.read_text())["events"]
    clusters = aggregate_graph_enrich_config_proposals(events)
    return next(
        cluster
        for cluster in clusters
        if cluster["issue_family"] == "test_import_fanin_direct_symbol_gate"
    )


def _observer_override_payload() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {"analyzer_role": "reconcile_graph_enrich_config_analyzer"},
        "operations": [
            {
                "op": "tighten_rule",
                "rule_id": "tests.test_import_fanin.require_direct_symbol_import",
                "edge": "tests",
                "source_evidence": "test_import_fanin",
                "action": "require_direct_symbol_import",
                "downgrade_to": "weak_tests",
                "confidence": 0.93,
                "when": {
                    "all": [
                        {
                            "predicate": "source_evidence_is",
                            "value": "test_import_fanin",
                        }
                    ]
                },
                "evidence": {
                    "reason": (
                        "Observer chose none of the raw AI candidates and authored "
                        "a project-level rule that preserves weak test evidence."
                    ),
                },
            }
        ],
        "self_check": {
            "valid": True,
            "checked_rules": [
                "schema_version",
                "semantic_bridge_normalized",
                "op_supported",
                "required_fields_present",
                "edge_supported_or_canonical_alias",
                "source_evidence_present",
                "action_present",
                "predicate_guard_weak_call_requires_call_syntax_or_receiver",
                "predicate_guard_string_literal_requires_raw_target",
                "config_patch_previewed",
                "observer_approval_required",
            ],
            "known_risks": [],
        },
    }


def _ctx(body: dict):
    return server.RequestContext(
        None,
        "POST",
        {"project_id": PID, "snapshot_id": SNAPSHOT_ID},
        {},
        body,
        "req-graph-proposal-review-test",
        "",
        "",
    )


def test_observer_override_select_none_writes_config_with_audit(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    conn = _conn(tmp_path)
    project = tmp_path / "generated-project"
    project.mkdir()
    cluster = _dogfood_cluster()

    try:
        result = apply_graph_enrich_config_observer_override(
            conn,
            PID,
            SNAPSHOT_ID,
            cluster=cluster,
            raw_output=json.dumps(_observer_override_payload()),
            mode="accept",
            project_root=project,
            actor="observer_user",
            rationale="AI candidates were repetitive; observer authored the canonical config rule.",
        )

        assert result["ok"] is True
        assert result["accepted"] is True
        assert result["review"]["review_action"] == "observer_override"
        assert result["review"]["selected_event_id"] == ""
        assert result["review"]["cluster_id"] == cluster["cluster_id"]
        assert result["review"]["rejected_event_ids"] == cluster["support_event_ids"]
        assert result["audit"]["operation_hash"].startswith("sha256:")

        config = yaml.safe_load((project / PROJECT_OVERRIDE_PATH).read_text())
        rule = config["graph_enrich_config_ops"]["rules"][
            "tests.test_import_fanin.require_direct_symbol_import"
        ]
        assert rule["op"] == "tighten_rule"
        assert rule["downgrade_to"] == "weak_tests"
        assert rule["when"]["all"][0]["predicate"] == "source_evidence_is"

        rows = graph_events.list_events(
            conn,
            PID,
            SNAPSHOT_ID,
            event_types=["graph_enrich_config_requested", "graph_enrich_config_completed"],
            limit=10,
        )
        requested = next(row for row in rows if row["event_type"] == "graph_enrich_config_requested")
        completed = next(row for row in rows if row["event_type"] == "graph_enrich_config_completed")

        assert requested["status"] == graph_events.EVENT_STATUS_MATERIALIZED
        assert requested["payload"]["review"]["review_action"] == "observer_override"
        assert requested["payload"]["review"]["selected_event_id"] == ""
        assert requested["payload"]["review"]["rejected_event_ids"] == cluster["support_event_ids"]
        assert completed["source_event_id"] == requested["event_id"]
        assert completed["payload"]["result"]["gate"]["accepted_count"] == 1
        assert completed["evidence"]["source"] == "observer_graph_enrich_config_override"
        assert completed["evidence"]["actor"] == "observer_user"
        assert completed["evidence"]["cluster_id"] == cluster["cluster_id"]
        assert completed["evidence"]["review_action"] == "observer_override"
        assert completed["evidence"]["precheck"]["status"] == "passed"
        assert completed["evidence"]["operation_hash"] == result["audit"]["operation_hash"]
    finally:
        conn.close()


def test_observer_override_api_accepts_cluster_payload_and_audits(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    conn = _conn(tmp_path)
    project = tmp_path / "generated-project"
    project.mkdir()
    cluster = _dogfood_cluster()
    monkeypatch.setattr(server, "get_connection", lambda _project_id: _NoCloseConn(conn))
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )

    try:
        status, result = server.handle_graph_governance_snapshot_graph_enrich_config_observer_override(
            _ctx(
                {
                    "mode": "accept",
                    "project_root": str(project),
                    "cluster": cluster,
                    "ai_output": json.dumps(_observer_override_payload()),
                    "actor": "observer_user",
                    "rationale": "Override all raw candidates with the canonical project-level rule.",
                }
            )
        )

        assert status == 200
        assert result["ok"] is True
        assert result["review"]["cluster_id"] == cluster["cluster_id"]
        assert result["audit"]["request_event_id"]
        assert result["audit"]["result_event_id"]
        assert (project / PROJECT_OVERRIDE_PATH).exists()
    finally:
        conn.close()


def test_observer_override_dry_run_keeps_config_unwritten_but_audited(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    conn = _conn(tmp_path)
    project = tmp_path / "generated-project"
    project.mkdir()
    cluster = _dogfood_cluster()

    try:
        result = apply_graph_enrich_config_observer_override(
            conn,
            PID,
            SNAPSHOT_ID,
            cluster=cluster,
            raw_output=json.dumps(_observer_override_payload()),
            mode="dry_run",
            project_root=project,
            actor="observer_user",
            rationale="Preview observer-authored override before writing config.",
        )

        assert result["ok"] is True
        assert result["accepted"] is False
        assert result["mutated"] is False
        assert result["dry_run"] is True
        assert not (project / PROJECT_OVERRIDE_PATH).exists()

        rows = graph_events.list_events(
            conn,
            PID,
            SNAPSHOT_ID,
            event_types=["graph_enrich_config_requested", "graph_enrich_config_completed"],
            limit=10,
        )
        requested = next(row for row in rows if row["event_type"] == "graph_enrich_config_requested")
        completed = next(row for row in rows if row["event_type"] == "graph_enrich_config_completed")
        assert requested["status"] == graph_events.EVENT_STATUS_MATERIALIZED
        assert completed["payload"]["result"]["accepted"] is False
        assert completed["evidence"]["mode"] == "dry_run"
    finally:
        conn.close()
