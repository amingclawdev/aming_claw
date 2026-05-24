from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from agent.governance.db import _ensure_schema
from agent.governance.doc_asset_state import build_doc_asset_state
from agent.governance.asset_projection import list_asset_projection
from agent.governance.governance_index import build_governance_index, persist_governance_index
from agent.governance.reconcile_phases.phase_z_v2 import (
    build_graph_v2_from_symbols,
    build_rebase_candidate_graph,
)


PID = "doc-asset-state-test"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _tmp_project(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp())
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def _candidate(project: Path, *, run_id: str = "doc-state") -> tuple[dict, dict]:
    phase_result = build_graph_v2_from_symbols(str(project), dry_run=True)
    candidate = build_rebase_candidate_graph(
        str(project),
        phase_result,
        session_id=run_id,
        run_id=phase_result["run_id"],
    )
    return phase_result, candidate


def test_doc_path_match_is_commit_bound_candidate_not_trusted_binding() -> None:
    project = _tmp_project({
        "agent/mymod.py": "def hello():\n    return 'ok'\n",
        "docs/ref.md": "# Ref\nSee agent/mymod.py for details.\n",
    })

    phase_result, candidate = _candidate(project)
    graph_node = next(
        node for node in candidate["deps_graph"]["nodes"]
        if node["layer"] == "L7" and node["title"] == "agent.mymod"
    )
    assert graph_node["secondary"] == []

    state = build_doc_asset_state(
        project_id=PID,
        run_id=phase_result["run_id"],
        commit_sha="abc1234",
        file_inventory=phase_result["file_inventory"],
        graph_nodes=candidate["deps_graph"]["nodes"],
    )
    docs = {row["path"]: row for row in state["docs"]}
    row = docs["docs/ref.md"]

    assert state["schema_version"] == "doc_asset_state.v1"
    assert state["summary"]["by_status"]["candidate"] == 1
    assert row["commit_sha"] == "abc1234"
    assert row["run_id"] == phase_result["run_id"]
    assert row["doc_kind"] == "doc"
    assert row["sha256"]
    assert row["file_hash"] == f"sha256:{row['sha256']}"
    assert row["binding_status"] == "candidate"
    assert row["accepted_bindings"] == []
    assert row["impact_scope_policy"] == "accepted_bindings_only"
    assert row["binding_candidates"][0]["target_node_id"] == graph_node["id"]
    assert row["binding_candidates"][0]["precheck"]["ok"] is True
    assert row["binding_candidates"][0]["precheck"]["decision"] == "review_required"


def test_source_controlled_hint_promotes_doc_asset_state_to_accepted() -> None:
    hint = {
        "binding": {
            "role": "doc",
            "path": "docs/ref.md",
            "target_module": "agent.mymod",
        }
    }
    project = _tmp_project({
        "agent/mymod.py": "def hello():\n    return 'ok'\n",
        "docs/ref.md": (
            "<!-- governance-hint "
            + json.dumps(hint, sort_keys=True)
            + " -->\n# Ref\nSee agent/mymod.py for details.\n"
        ),
    })

    phase_result, candidate = _candidate(project, run_id="doc-hint")
    graph_node = next(
        node for node in candidate["deps_graph"]["nodes"]
        if node["layer"] == "L7" and node["title"] == "agent.mymod"
    )
    assert graph_node["secondary"] == ["docs/ref.md"]

    state = build_doc_asset_state(
        project_id=PID,
        run_id=phase_result["run_id"],
        commit_sha="def5678",
        file_inventory=phase_result["file_inventory"],
        graph_nodes=candidate["deps_graph"]["nodes"],
    )
    row = {item["path"]: item for item in state["docs"]}["docs/ref.md"]

    assert state["summary"]["by_status"]["accepted"] == 1
    assert row["binding_status"] == "accepted"
    assert row["binding_candidates"] == []
    assert row["accepted_bindings"] == [
        {
            "node_id": graph_node["id"],
            "title": "agent.mymod",
            "role": "doc",
            "source": "graph_node",
        }
    ]
    assert row["impact_scope_policy"] == "accepted_bindings_only"


def test_governance_index_persists_doc_asset_state_artifact(tmp_path) -> None:
    conn = _conn()
    project = _tmp_project({
        "agent/mymod.py": "def hello():\n    return 'ok'\n",
        "docs/ref.md": "# Ref\nSee agent/mymod.py for details.\n",
    })
    phase_result, candidate = _candidate(project, run_id="doc-index")

    index = build_governance_index(
        conn,
        PID,
        project,
        run_id="index-doc-state",
        commit_sha="abc1234",
        candidate_graph=candidate,
        snapshot_id="full-abc1234-doc-state",
        snapshot_kind="full",
        file_inventory=phase_result["file_inventory"],
    )
    summary = persist_governance_index(
        conn,
        PID,
        index,
        artifact_root=tmp_path / "artifacts",
    )

    artifact_path = Path(summary["artifacts"]["doc_asset_state_path"])
    assert artifact_path.exists()
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    row = {item["path"]: item for item in payload["docs"]}["docs/ref.md"]

    assert summary["doc_asset_state"]["by_status"]["candidate"] == 1
    assert summary["asset_projection_rows_persisted"] == 1
    assert summary["asset_binding_rows_persisted"] == 1
    assert payload["commit_sha"] == "abc1234"
    assert row["binding_status"] == "candidate"
    assert row["binding_candidates"][0]["target_title"] == "agent.mymod"

    projection_rows = list_asset_projection(
        conn,
        project_id=PID,
        snapshot_id="full-abc1234-doc-state",
        asset_kind="doc",
    )
    assert [item["asset_path"] for item in projection_rows] == ["docs/ref.md"]
    assert projection_rows[0]["binding_status"] == "candidate"
    assert projection_rows[0]["binding_candidates"][0]["target_title"] == "agent.mymod"
