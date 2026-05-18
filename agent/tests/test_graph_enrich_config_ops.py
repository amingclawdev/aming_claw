from __future__ import annotations

import json
import sqlite3

import pytest

from agent.governance import server
from agent.governance.db import _ensure_schema
from agent.governance.graph_enrich_config_ops import (
    SCHEMA_VERSION,
    run_graph_enrich_config_ai_output_pipeline,
)
from agent.governance.reconcile_semantic_config import (
    PROJECT_OVERRIDE_PATH,
    load_semantic_enrichment_config,
)


PID = "graph-enrich-config-ops-test"


class _NoCloseConn:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    monkeypatch.setattr(server, "get_connection", lambda _project_id: _NoCloseConn(c))
    monkeypatch.setattr(
        server,
        "_require_graph_governance_operator",
        lambda _ctx, _conn, _action: {"role": "observer"},
    )
    yield c
    c.close()


def _ctx(body: dict):
    return server.RequestContext(
        None,
        "POST",
        {"project_id": PID},
        {},
        body,
        "req-graph-enrich-config-ops-test",
        "",
        "",
    )


def _payload() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "analyzer_role": "reconcile_graph_enrich_config_analyzer",
        },
        "operations": [
            {
                "op": "upsert_edge_evidence_policy",
                "rule_id": "calls-import-only-downgrade",
                "edge": "calls",
                "source_evidence": "import_only",
                "action": "downgrade",
                "downgrade_to": "imports",
                "confidence": 0.94,
                "evidence": {
                    "reason": "import-only type annotations should not create calls edges",
                },
            }
        ],
        "self_check": {
            "valid": True,
            "checked_rules": ["edge_supported", "action_supported", "config_path"],
            "known_risks": [],
        },
    }


def test_graph_enrich_config_ops_dry_run_is_non_mutating_generated_project(tmp_path):
    project = tmp_path / "generated-project"
    project.mkdir()

    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=json.dumps(_payload()),
        mode="dry_run",
        project_root=project,
    )

    assert result["ok"] is True
    assert result["mutated"] is False
    assert result["accepted"] is False
    assert result["preview"]["config_path"] == str(project / PROJECT_OVERRIDE_PATH)
    assert result["preview"]["graph_structure_ops"]["evidence_policy"]["calls"] == {
        "import_only_action": "downgrade",
        "downgrade_to": "imports",
        "require_call_evidence": True,
    }
    assert not (project / PROJECT_OVERRIDE_PATH).exists()


def test_graph_enrich_config_ops_accept_writes_project_override_and_loader_reads_it(
    tmp_path,
):
    project = tmp_path / "generated-project"
    project.mkdir()

    result = run_graph_enrich_config_ai_output_pipeline(
        raw_output=json.dumps(_payload()),
        mode="accept",
        project_root=project,
    )

    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["mutated"] is True
    assert result["requires_commit"] is True
    override_path = project / PROJECT_OVERRIDE_PATH
    assert override_path.exists()
    assert "import_only_action: downgrade" in override_path.read_text(encoding="utf-8")

    config = load_semantic_enrichment_config(project_root=project)
    assert config.graph_structure_ops.evidence_policy["calls"] == {
        "import_only_action": "downgrade",
        "downgrade_to": "imports",
        "require_call_evidence": True,
    }


def test_graph_enrich_config_ops_api_accepts_ai_output_and_writes_project_override(
    conn,
    tmp_path,
):
    project = tmp_path / "generated-project"
    project.mkdir()

    status, result = server.handle_graph_governance_graph_enrich_config_ops_ai_output(
        _ctx(
            {
                "mode": "accept",
                "project_root": str(project),
                "ai_output": json.dumps(_payload()),
            }
        )
    )

    assert status == 200
    assert result["ok"] is True
    assert result["dry_run"] is False
    assert result["mutated"] is True
    assert result["project_root"] == str(project.resolve())
    assert (project / PROJECT_OVERRIDE_PATH).exists()
