from __future__ import annotations

import json
import sqlite3

import pytest

from agent.governance import graph_events
from agent.governance import graph_query_trace
from agent.governance import graph_snapshot_store as store
from agent.governance import reconcile_semantic_enrichment as semantic_enrichment
from agent.governance.db import _ensure_schema


PID = "graph-query-trace-test"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("agent.governance.db._governance_root", lambda: tmp_path / "state")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    _ensure_schema(c)
    graph_events.ensure_schema(c)
    store.ensure_schema(c)
    graph_query_trace.ensure_schema(c)
    yield c
    c.close()


def _seed_snapshot(conn, tmp_path):
    project_root = tmp_path / "workspace"
    (project_root / "agent" / "governance").mkdir(parents=True)
    (project_root / "agent" / "tests").mkdir(parents=True)
    (project_root / "docs").mkdir(parents=True)
    (project_root / "agent" / "governance" / "server.py").write_text("def serve():\n    return 'ok'\n", encoding="utf-8")
    (project_root / "agent" / "tests" / "test_server.py").write_text("def test_serve():\n    assert True\n", encoding="utf-8")
    (project_root / "docs" / "architecture.md").write_text(
        "Batch job substrate connects reconcile, scope reconcile, and chain branch execution.\n",
        encoding="utf-8",
    )
    graph = {
        "deps_graph": {
            "nodes": [
                {
                    "id": "L3.1",
                    "layer": "L3",
                    "title": "Runtime",
                    "kind": "subsystem",
                    "metadata": {"kind": "subsystem"},
                },
                {
                    "id": "L7.1",
                    "layer": "L7",
                    "title": "Governance Server",
                    "kind": "service_runtime",
                    "primary": ["agent/governance/server.py"],
                    "secondary": ["docs/architecture.md"],
                    "test": ["agent/tests/test_server.py"],
                    "metadata": {
                        "hierarchy_parent": "L3.1",
                        "function_count": 3,
                        "functions": [
                            "agent.governance.server::serve",
                            "agent.governance.server::Server.start",
                        ],
                        "function_lines": {
                            "serve": [1, 2],
                            "Server.start": [10, 12],
                        },
                        "function_calls": [
                            {
                                "caller": "agent.governance.server::serve",
                                "caller_short": "serve",
                                "caller_module": "agent.governance.server",
                                "caller_file": "agent/governance/server.py",
                                "caller_line": [1, 2],
                                "callee": "agent.governance.helper::helper",
                                "callee_short": "helper",
                                "callee_module": "agent.governance.helper",
                                "callee_file": "agent/governance/helper.py",
                                "callee_line": [1, 2],
                                "confidence": "strong",
                                "resolution": "resolved",
                            }
                        ],
                        "function_call_count": 1,
                        "function_called_by_count": 0,
                        "config_files": [".aming-claw.yaml"],
                    },
                },
                {
                    "id": "L7.2",
                    "layer": "L7",
                    "title": "Untested Helper",
                    "kind": "implementation",
                    "primary": ["agent/governance/helper.py"],
                    "metadata": {
                        "hierarchy_parent": "L3.1",
                        "function_count": 55,
                        "module": "agent.governance.helper",
                        "functions": ["agent.governance.helper::helper"],
                        "function_lines": {"helper": [1, 2]},
                        "function_called_by": [
                            {
                                "caller": "agent.governance.server::serve",
                                "caller_short": "serve",
                                "caller_module": "agent.governance.server",
                                "caller_file": "agent/governance/server.py",
                                "caller_line": [1, 2],
                                "callee": "agent.governance.helper::helper",
                                "callee_short": "helper",
                                "callee_module": "agent.governance.helper",
                                "callee_file": "agent/governance/helper.py",
                                "callee_line": [1, 2],
                                "confidence": "strong",
                                "resolution": "resolved",
                            }
                        ],
                        "function_call_count": 0,
                        "function_called_by_count": 1,
                    },
                },
            ],
            "edges": [
                {"source": "L3.1", "target": "L7.1", "type": "contains"},
                {"source": "L7.1", "target": "L7.2", "type": "depends_on"},
            ],
        }
    }
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-query-test",
        commit_sha="abc1234",
        snapshot_kind="full",
        graph_json=graph,
        file_inventory=[
            {"path": "docs/architecture.md", "file_kind": "doc", "graph_status": "attached"},
            {"path": "agent/governance/server.py", "file_kind": "source", "graph_status": "mapped"},
        ],
    )
    store.index_graph_snapshot(
        conn,
        PID,
        snapshot["snapshot_id"],
        nodes=graph["deps_graph"]["nodes"],
        edges=store.graph_payload_edges(graph),
    )
    store.activate_graph_snapshot(conn, PID, snapshot["snapshot_id"])
    conn.commit()
    return snapshot["snapshot_id"], project_root


def _seed_edge_projection(conn, snapshot_id):
    projection = {
        "node_semantics": {},
        "edge_semantics": {
            "L7.1->L7.2:depends_on": {
                "edge_id": "L7.1->L7.2:depends_on",
                "edge": {
                    "src": "L7.1",
                    "dst": "L7.2",
                    "type": "depends_on",
                    "edge_type": "depends_on",
                },
                "semantic": {
                    "semantic_label": "server_helper_cache_dependency",
                    "relation_purpose": "Governance server dispatch invokes the helper cache.",
                    "risk": {"level": "medium", "reason": "helper cache is a hidden dependency"},
                },
                "validity": {"status": "edge_semantic_current", "valid": True},
                "source_event": {"event_id": "ge-edge-1", "event_type": "edge_semantic_enriched"},
            }
        },
    }
    conn.execute(
        """
        INSERT INTO graph_semantic_projections
          (project_id, snapshot_id, projection_id, base_commit, branch_ref,
           projection_rule_version, event_watermark, status, projection_json,
           health_json, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            PID,
            snapshot_id,
            "semproj-test",
            "abc1234",
            "main",
            "test",
            1,
            "current",
            json.dumps(projection),
            json.dumps({"edge_semantic_current_count": 1}),
            "test",
            "2026-05-13T00:00:00Z",
            "2026-05-13T00:00:00Z",
        ),
    )
    conn.commit()


def _seed_node_semantic(conn, snapshot_id):
    semantic_enrichment._ensure_semantic_state_schema(conn)
    conn.execute(
        """
        INSERT INTO graph_semantic_nodes
          (project_id, snapshot_id, node_id, status, feature_hash,
           file_hashes_json, semantic_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            PID,
            snapshot_id,
            "L7.1",
            "ai_complete",
            "fh-server",
            "{}",
            json.dumps(
                {
                    "feature_name": "Governance Server",
                    "domain_label": "runtime.governance",
                    "intent": "Serve graph governance APIs.",
                    "semantic_summary": "Runtime API for graph governance.",
                    "quality_flags": ["reviewed"],
                }
            ),
            "2026-05-14T00:00:00Z",
        ),
    )
    conn.commit()


def test_trace_records_queries_and_budget_usage(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)
    trace = graph_query_trace.start_trace(
        conn,
        PID,
        snapshot_id,
        actor="ai-reviewer",
        query_source="ai_global_review",
        query_purpose="global_architecture_review",
        run_id="global-review-001",
        budget={"max_queries": 5},
    )["trace"]

    result = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        trace_id=trace["trace_id"],
        tool="get_node",
        args={"node_id": "L7.1", "include_feedback": True},
        project_root=project_root,
    )

    assert result["ok"] is True
    assert result["result"]["node"]["title"] == "Governance Server"
    assert result["args_hash"].startswith("sha256:")
    assert result["result_hash"].startswith("sha256:")

    stored = graph_query_trace.get_trace(conn, PID, trace["trace_id"])["trace"]
    assert stored["usage"]["query_count"] == 1
    assert stored["event_count"] == 1
    assert stored["events"][0]["tool"] == "get_node"
    assert stored["status"] == "running"
    assert stored["artifact_path"]


def test_query_tools_reuse_graph_files_and_search_docs(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)
    result = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="dashboard",
        query_source="dashboard",
        query_purpose="inspect_node",
        tool="search_docs",
        args={"query": "Batch job substrate", "limit": 5},
        project_root=project_root,
    )

    assert result["ok"] is True
    assert result["result"]["match_count"] == 1
    assert result["result"]["matches"][0]["path"] == "docs/architecture.md"

    trace = graph_query_trace.get_trace(conn, PID, result["trace_id"])["trace"]
    assert trace["query_source"] == "dashboard"
    assert trace["query_purpose"] == "inspect_node"
    assert trace["status"] == "complete"
    assert trace["usage"]["file_excerpt_chars"] > 0


def test_one_shot_query_finishes_failed_trace_on_error(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)
    result = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="inspect_node",
        tool="not_a_real_tool",
        project_root=project_root,
    )

    assert result["ok"] is False
    trace = graph_query_trace.get_trace(conn, PID, result["trace_id"])["trace"]
    assert trace["status"] == "failed"
    assert trace["event_count"] == 1
    assert trace["events"][0]["tool"] == "not_a_real_tool"


def test_graph_native_discovery_queries_cover_paths_functions_and_degrees(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)

    path_result = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="find_node_by_path",
        args={"path": "agent/governance/server.py"},
        project_root=project_root,
    )
    assert path_result["ok"] is True
    assert path_result["result"]["matches"][0]["node"]["node_id"] == "L7.1"
    assert path_result["result"]["matches"][0]["matched_files"][0]["role"] == "primary"

    subtree_result = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="find_node_by_path",
        args={"path": "agent/governance", "directory": True},
        project_root=project_root,
    )
    assert subtree_result["ok"] is True
    assert subtree_result["result"]["match"] == "directory"
    assert [match["node"]["node_id"] for match in subtree_result["result"]["matches"]] == [
        "L7.1",
        "L7.2",
    ]
    assert (
        subtree_result["result"]["matches"][0]["matched_files"][0]["path"]
        == "agent/governance/server.py"
    )

    structure = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="search_structure",
        args={"query": "Server.start"},
        project_root=project_root,
    )
    assert structure["result"]["matches"][0]["node"]["node_id"] == "L7.1"

    functions = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="function_index",
        args={"query": "serve"},
        project_root=project_root,
    )
    assert functions["result"]["matches"][0]["short_name"] == "serve"
    assert functions["result"]["matches"][0]["line_start"] == 1

    callees = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="function_callees",
        args={"query": "serve"},
        project_root=project_root,
    )
    assert callees["result"]["matches"][0]["callee_short"] == "helper"
    assert callees["result"]["matches"][0]["callee_node"]["node_id"] == "L7.2"

    callers = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="function_callers",
        args={"query": "helper"},
        project_root=project_root,
    )
    assert callers["result"]["matches"][0]["caller_short"] == "serve"

    high_fn = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="high_function_degree",
        args={"metric": "fan_out"},
        project_root=project_root,
    )
    assert high_fn["result"]["functions"][0]["short_name"] == "serve"
    assert high_fn["result"]["functions"][0]["fan_out"] == 1

    degree = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="degree_summary",
        args={"node_id": "L7.1"},
        project_root=project_root,
    )
    assert degree["result"]["fan_in"] == 1
    assert degree["result"]["fan_out"] == 1
    assert degree["result"]["by_type"]["depends_on"]["out"] == 1

    neighbors = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="get_neighbors",
        args={"node_id": "L7.1", "compact": True},
        project_root=project_root,
    )
    contract = neighbors["result"]["graph_contract"]
    depends_on = contract["deps_graph"]["depends_on"]
    assert depends_on["direction"] == "dependency_to_dependent"
    assert "B -> A" in depends_on["interpretation"]

    high_degree = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="high_degree_nodes",
        args={"metric": "fan_out", "edge_types": ["depends_on"]},
        project_root=project_root,
    )
    assert high_degree["result"]["nodes"][0]["node"]["node_id"] == "L7.1"


def test_list_features_defaults_to_compact_budget_safe_payload(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)

    result = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="list_features",
        args={},
        project_root=project_root,
    )

    assert result["ok"] is True
    payload = result["result"]
    assert payload["compact"] is True
    assert payload["include_semantic"] is False
    assert payload["count"] == 2
    first = payload["features"][0]
    assert "semantic" not in first
    assert "function_calls" not in first["metadata"]


def test_list_features_can_opt_into_compact_semantic_overlay(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)
    _seed_node_semantic(conn, snapshot_id)

    result = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="list_features",
        args={"compact": True, "include_semantic": True, "limit": 1},
        project_root=project_root,
    )

    assert result["ok"] is True
    payload = result["result"]
    assert payload["compact"] is True
    assert payload["include_semantic"] is True
    assert payload["features"][0]["semantic"]["feature_name"] == "Governance Server"
    assert payload["features"][0]["semantic"]["status"] == "ai_complete"
    assert "semantic_summary" not in payload["features"][0]["semantic"]


def test_get_file_excerpt_accepts_start_line_end_line_aliases(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)
    source = project_root / "agent" / "governance" / "server.py"
    source.write_text(
        "\n".join(f"line {idx}" for idx in range(1, 121)) + "\n",
        encoding="utf-8",
    )

    result = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="get_file_excerpt",
        args={"path": "agent/governance/server.py", "start_line": 92, "end_line": 95},
        project_root=project_root,
    )

    assert result["ok"] is True
    excerpt = result["result"]
    assert excerpt["line_start"] == 92
    assert excerpt["line_end"] == 95
    assert "line 92" in excerpt["excerpt"]
    assert "line 1\n" not in excerpt["excerpt"]


def test_function_call_query_reports_truncation_instead_of_scanning_forever(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)

    result = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="function_callees",
        args={"query": "serve", "max_scan": 1, "limit": 10},
        project_root=project_root,
    )

    assert result["ok"] is True
    assert result["result"]["truncated"] is False
    assert result["result"]["scanned_facts"] == 1

    row = conn.execute(
        "SELECT metadata_json FROM graph_nodes_index WHERE project_id=? AND snapshot_id=? AND node_id=?",
        (PID, snapshot_id, "L7.1"),
    ).fetchone()
    metadata = json.loads(row["metadata_json"])
    metadata["function_calls"].append({**metadata["function_calls"][0], "caller_short": "serve_again"})
    conn.execute(
        "UPDATE graph_nodes_index SET metadata_json=? WHERE project_id=? AND snapshot_id=? AND node_id=?",
        (json.dumps(metadata), PID, snapshot_id, "L7.1"),
    )
    conn.commit()

    truncated = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="function_callees",
        args={"query": "no-such-function", "max_scan": 1, "limit": 10},
        project_root=project_root,
    )

    assert truncated["ok"] is True
    assert truncated["result"]["truncated"] is True
    assert truncated["result"]["truncation_reason"] == "max_scan"


def test_graph_native_queries_search_edge_projection_and_neighbor_semantics(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)
    _seed_edge_projection(conn, snapshot_id)

    semantic = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="search_semantic",
        args={"query": "helper cache", "scope": "edges"},
        project_root=project_root,
    )
    assert semantic["result"]["matches"][0]["result_type"] == "edge"
    assert semantic["result"]["matches"][0]["edge_id"] == "L7.1->L7.2:depends_on"
    assert semantic["result"]["matches"][0]["validity"]["status"] == "edge_semantic_current"

    neighbors = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="get_neighbors",
        args={"node_id": "L7.1", "direction": "out", "include_edge_semantic": True},
        project_root=project_root,
    )
    edge = neighbors["result"]["edges"][0]
    assert edge["edge_semantic"]["semantic"]["semantic_label"] == "server_helper_cache_dependency"


def test_query_schema_exposes_tools_and_enums(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)
    result = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="prompt_context_build",
        tool="query_schema",
        project_root=project_root,
    )

    assert result["ok"] is True
    assert "find_node_by_path" in result["result"]["tool_names"]
    find_by_path = result["result"]["tools"]["find_node_by_path"]
    assert "directory" in find_by_path["optional_args"]
    assert "subtree" in find_by_path["args"]["match"]["enum"]
    assert find_by_path["examples"][0]["args"] == {
        "path": "frontend/dashboard/src",
        "directory": True,
        "limit": 25,
    }
    assert "observer" in result["result"]["query_sources"]
    assert "prompt_context_build" in result["result"]["query_purposes"]
    assert result["result"]["tools"]["high_function_degree"]["args"]["metric"]["enum"] == [
        "fan_in",
        "fan_out",
        "total",
    ]
    list_features = result["result"]["tools"]["list_features"]
    assert list_features["args"]["compact"]["default"] is True
    assert "include_semantic" in list_features["optional_args"]
    assert result["result"]["tools"]["get_node"]["optional_args"][:2] == [
        "compact",
        "include_semantic",
    ]
    assert result["result"]["tools"]["get_neighbors"]["args"]["direction"]["enum"] == [
        "in",
        "out",
        "both",
    ]
    assert result["result"]["tools"]["search_semantic"]["args"]["scope"]["enum"] == [
        "all",
        "nodes",
        "edges",
    ]
    assert "timeout_ms" in result["result"]["tools"]["function_callees"]["optional_args"]


def test_snapshot_orphan_file_filter_excludes_attached_rows(conn, tmp_path):
    graph = {"deps_graph": {"nodes": [], "edges": []}}
    snapshot = store.create_graph_snapshot(
        conn,
        PID,
        snapshot_id="full-file-filter",
        commit_sha="abc9999",
        snapshot_kind="full",
        graph_json=graph,
        file_inventory=[
            {
                "path": "docs/actionable.md",
                "file_kind": "doc",
                "scan_status": "orphan",
                "graph_status": "unmapped",
                "attached_node_ids": [],
            },
            {
                "path": "docs/already-bound.md",
                "file_kind": "doc",
                "scan_status": "orphan",
                "graph_status": "attached",
                "attached_node_ids": ["L7.1"],
                "attached_to": "L7.1",
            },
        ],
    )

    result = store.list_graph_snapshot_files(
        conn,
        PID,
        snapshot["snapshot_id"],
        scan_status="orphan",
    )

    assert result["filtered_count"] == 1
    assert result["files"][0]["path"] == "docs/actionable.md"


def test_budget_blocks_queries_after_limit(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)
    trace = graph_query_trace.start_trace(
        conn,
        PID,
        snapshot_id,
        actor="gate",
        query_source="chain_graph_gate",
        query_purpose="gate_validation",
        budget={"max_queries": 1},
    )["trace"]

    first = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        trace_id=trace["trace_id"],
        tool="list_layers",
        project_root=project_root,
    )
    second = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        trace_id=trace["trace_id"],
        tool="list_layers",
        project_root=project_root,
    )

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["error"] == "query_budget_exceeded"
    assert second["budget_key"] == "max_queries"
    stored = graph_query_trace.get_trace(conn, PID, trace["trace_id"])["trace"]
    assert stored["status"] == "budget_exceeded"


def test_get_neighbors_compact_keeps_large_neighbor_metadata_budget_safe(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)
    huge_functions = [
        {"name": f"fn_{idx}", "path": "agent/governance/helper.py", "lineno": idx}
        for idx in range(1500)
    ]
    conn.execute(
        """
        UPDATE graph_nodes_index
        SET metadata_json = ?
        WHERE project_id = ? AND snapshot_id = ? AND node_id = ?
        """,
        (
            json.dumps({
                "hierarchy_parent": "L3.1",
                "function_count": len(huge_functions),
                "functions": huge_functions,
            }),
            PID,
            snapshot_id,
            "L7.2",
        ),
    )
    conn.commit()
    trace = graph_query_trace.start_trace(
        conn,
        PID,
        snapshot_id,
        actor="semantic-worker",
        query_source="ai_semantic_review",
        query_purpose="semantic_enrichment",
        budget={"max_result_chars": 10_000},
    )["trace"]

    result = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        trace_id=trace["trace_id"],
        tool="get_neighbors",
        args={"node_id": "L7.1", "direction": "both", "limit": 10, "compact": True},
        project_root=project_root,
    )

    assert result["ok"] is True
    assert result["budget_exceeded"] is False
    assert result["usage"]["result_chars"] < 10_000
    assert result["result"]["compact"] is True
    neighbor = {
        node["node_id"]: node
        for node in result["result"]["nodes"]
    }["L7.2"]
    assert neighbor["metadata"]["function_count"] == len(huge_functions)
    assert "functions" not in neighbor["metadata"]


def test_low_health_query_uses_structural_and_feedback_signals(conn, tmp_path):
    snapshot_id, project_root = _seed_snapshot(conn, tmp_path)
    result = graph_query_trace.traced_query(
        conn,
        PID,
        snapshot_id,
        actor="observer",
        query_source="observer",
        query_purpose="health_score",
        tool="list_low_health_nodes",
        args={"limit": 10},
        project_root=project_root,
    )

    assert result["ok"] is True
    low = {item["node"]["node_id"]: item for item in result["result"]["nodes"]}
    assert "L7.2" in low
    assert "missing_test_binding" in low["L7.2"]["issues"]
    assert "high_function_count" in low["L7.2"]["issues"]
