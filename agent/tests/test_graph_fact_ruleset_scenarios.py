"""Scenario-first tests for the graph fact/ruleset pipeline.

These tests intentionally describe the next graph-construction contract before
the implementation exists.  They use generated projects so future fact/ruleset
work is constrained by realistic source, test, and config consumers without
mutating the Aming Claw repository graph.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.governance.reconcile_phases.phase_z_v2 import (
    build_graph_v2_from_symbols,
    build_rebase_candidate_graph,
)
from agent.governance.state_reconcile import _incremental_metadata_scope_eligibility


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _candidate_for_generated_project(project: Path) -> dict:
    graph = build_graph_v2_from_symbols(str(project), dry_run=True)
    assert graph["status"] == "ok"
    return build_rebase_candidate_graph(
        str(project),
        graph,
        session_id="graph-fact-ruleset-scenario",
        run_id=graph["run_id"],
    )


def _l7_nodes_by_title(candidate: dict) -> dict[str, dict]:
    return {
        str(node.get("title") or ""): node
        for node in ((candidate.get("deps_graph") or {}).get("nodes") or [])
        if node.get("layer") == "L7"
    }


def _edge_titles(candidate: dict) -> set[tuple[str, str, str]]:
    nodes = {
        str(node.get("id") or ""): str(node.get("title") or "")
        for node in ((candidate.get("deps_graph") or {}).get("nodes") or [])
    }
    out: set[tuple[str, str, str]] = set()
    for edge in ((candidate.get("deps_graph") or {}).get("links") or []):
        source = nodes.get(str(edge.get("source") or ""))
        target = nodes.get(str(edge.get("target") or ""))
        edge_type = str(edge.get("type") or "")
        if source and target:
            out.add((source, target, edge_type))
    return out


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Python relative imports are not yet normalized into standard import "
        "facts before module dependency edge generation."
    ),
)
def test_generated_project_relative_import_fact_drives_dependency_edge(tmp_path: Path) -> None:
    project = tmp_path / "generated-relative-import-project"
    _write(project / "agent" / "__init__.py", "")
    _write(project / "agent" / "governance" / "__init__.py", "")
    _write(
        project / "agent" / "governance" / "graph_snapshot_store.py",
        "def ensure_schema():\n"
        "    return True\n",
    )
    _write(
        project / "agent" / "governance" / "graph_events.py",
        "from . import graph_snapshot_store as store\n\n"
        "def create_event():\n"
        "    return store.ensure_schema()\n",
    )

    candidate = _candidate_for_generated_project(project)

    assert (
        "agent.governance.graph_snapshot_store",
        "agent.governance.graph_events",
        "depends_on",
    ) in _edge_titles(candidate)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "pytest fixture usage is not yet represented as consumer-side facts "
        "that fan in through conftest imports to the tested module."
    ),
)
def test_generated_project_pytest_fixture_consumer_fanin_attaches_test_to_subject(tmp_path: Path) -> None:
    project = tmp_path / "generated-pytest-fixture-fanin-project"
    _write(project / "agent" / "__init__.py", "")
    _write(
        project / "agent" / "service.py",
        "def service_entry():\n"
        "    return 'ok'\n",
    )
    _write(
        project / "agent" / "tests" / "conftest.py",
        "import pytest\n"
        "from agent.service import service_entry\n\n"
        "@pytest.fixture\n"
        "def service_value():\n"
        "    return service_entry()\n",
    )
    _write(
        project / "agent" / "tests" / "test_service_contract.py",
        "def test_service_contract(service_value):\n"
        "    assert service_value == 'ok'\n",
    )

    candidate = _candidate_for_generated_project(project)
    service = _l7_nodes_by_title(candidate)["agent.service"]
    fanin = (service.get("metadata") or {}).get("test_consumer_fanin") or []

    assert "agent/tests/test_service_contract.py" in set(service.get("test") or [])
    assert any(
        entry.get("path") == "agent/tests/test_service_contract.py"
        and entry.get("evidence") == "pytest_fixture_consumer_fanin"
        for entry in fanin
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Ruleset/config changes are currently metadata-only eligible; rule-aware "
        "reconcile must classify them as interpretation changes instead."
    ),
)
def test_ruleset_change_is_not_metadata_only_scope_incremental() -> None:
    scope_delta = {
        "impacted_files": ["aming_claw/graph_rules/python.yml"],
        "hash_changed_files": ["aming_claw/graph_rules/python.yml"],
        "added_files": [],
        "removed_files": [],
        "status_changed_files": [],
    }
    old_rows = [
        {
            "path": "aming_claw/graph_rules/python.yml",
            "file_kind": "config",
            "attachment_role": "secondary",
        }
    ]
    new_rows = [
        {
            "path": "aming_claw/graph_rules/python.yml",
            "file_kind": "config",
            "attachment_role": "secondary",
            "ruleset_scope": "graph_fact_interpretation",
        }
    ]

    eligibility = _incremental_metadata_scope_eligibility(
        scope_delta,
        project_root=Path("."),
        active_graph_json={"deps_graph": {"nodes": []}},
        old_rows=old_rows,
        new_rows=new_rows,
    )

    assert eligibility == {
        "supported": False,
        "reason": "ruleset_change_requires_rule_aware_reconcile",
        "rule_aware": True,
        "ruleset_paths": ["aming_claw/graph_rules/python.yml"],
    }
