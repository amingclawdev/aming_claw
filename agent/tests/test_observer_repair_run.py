from __future__ import annotations

import json

from agent.governance import observer_repair_run


def _rows():
    return [
        {
            "bug_id": "AC-TIMELINE-APPEND-ROUTE-WAIVER-SCHEMA-20260602",
            "title": "task_timeline_append protected gate advertises route_waiver recovery but MCP entrypoint does not consume it",
            "status": "OPEN",
            "priority": "P2",
            "details_md": "MCP schema does not expose route_waiver and route_token recovery path.",
            "chain_trigger_json": "{}",
        },
        {
            "bug_id": "AC-GRAPH-PENDING-SCOPE-QUEUE-TIMEOUT-20260602",
            "title": "graph_pending_scope_queue times out for external content-sys target commit",
            "status": "OPEN",
            "priority": "P2",
            "details_md": "pending scope queue timeout leaves graph stale after target commit.",
            "chain_trigger_json": "{}",
        },
        {
            "bug_id": "CONTENT-SYS-DOCKER-CONTEXT-FIXTURE-20260601",
            "title": "Docker context fixture close blocked by route and timeline evidence",
            "status": "OPEN",
            "priority": "P1",
            "details_md": "missing implementation verification close_ready independent_verification and route_identity_mismatch.",
            "chain_trigger_json": json.dumps(
                {
                    "depends_on": [
                        "AC-TIMELINE-APPEND-ROUTE-WAIVER-SCHEMA-20260602",
                        "AC-GRAPH-PENDING-SCOPE-QUEUE-TIMEOUT-20260602",
                    ]
                }
            ),
        },
    ]


def test_repair_run_plan_is_deterministic_and_judge_independent():
    kwargs = {
        "project_id": "aming-claw",
        "root_backlog_ids": [row["bug_id"] for row in _rows()],
        "backlog_rows": _rows(),
        "blockers": ["route_token_required from protected backlog_upsert"],
        "actor": "observer-test",
    }

    first = observer_repair_run.build_repair_run_plan(**kwargs)
    second = observer_repair_run.build_repair_run_plan(**kwargs)

    assert first["repair_run_id"] == second["repair_run_id"]
    assert first["route_context"]["route_context_hash"] == second["route_context"]["route_context_hash"]
    assert first["runtime_independent_of_judgment_brain"] is True
    assert first["route_context"]["judgment_brain_required"] is False
    assert first["route_context"]["authorizes_protected_write"] is False
    assert first["protected_write_policy"]["diagnostic_events_count_as_close_evidence"] is False


def test_repair_run_groups_lanes_and_orders_dependencies():
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=[row["bug_id"] for row in _rows()],
        backlog_rows=_rows(),
    )

    lane_ids = [lane["lane_id"] for lane in plan["lane_dispatches"]]
    assert "runtime_schema" in lane_ids
    assert "graph_reconcile" in lane_ids
    assert "route_context" in lane_ids
    assert "independent_verification" in lane_ids
    assert "close_gate" in lane_ids

    edges = {
        (edge["from"], edge["to"], edge["reason"])
        for edge in plan["backlog_dependency_dag"]["edges"]
    }
    assert (
        "AC-TIMELINE-APPEND-ROUTE-WAIVER-SCHEMA-20260602",
        "CONTENT-SYS-DOCKER-CONTEXT-FIXTURE-20260601",
        "declared_dependency",
    ) in edges
    assert (
        "AC-GRAPH-PENDING-SCOPE-QUEUE-TIMEOUT-20260602",
        "CONTENT-SYS-DOCKER-CONTEXT-FIXTURE-20260601",
        "declared_dependency",
    ) in edges
    assert any(edge[2] == "schema_before_protected_write" for edge in edges)


def test_repair_run_classifies_gate_failures_into_next_legal_actions():
    plan = observer_repair_run.build_repair_run_plan(
        project_id="content-sys",
        root_backlog_ids=[],
        blockers=[
            {
                "error": "route_token_required",
                "message": "route_token is required for protected governance action backlog_close",
            },
            "graph_pending_scope_queue timed out while graph stale",
            "route_identity_mismatch after stale hand-written route context",
        ],
    )

    assert "return_to_route_context_and_request_valid_route_token" in plan["next_legal_actions"]
    assert "replace_queue_wait_with_bounded_reconcile_fallback" in plan["next_legal_actions"]
    assert "supersede_or_reset_stale_route_identity_before_retry" in plan["next_legal_actions"]
    assert plan["checkpoints"][0]["checkpoint_id"] == "diagnosed"
    assert plan["checkpoints"][0]["status"] == "passed"


def test_repair_run_includes_service_generated_route_preview():
    plan = observer_repair_run.build_repair_run_plan(
        project_id="aming-claw",
        root_backlog_ids=[row["bug_id"] for row in _rows()],
        backlog_rows=_rows(),
        graph_status={"current_state": {"graph_stale": {"is_stale": False}}},
        version_check={"ok": True, "status": "passed", "dirty": False, "dirty_files": []},
    )

    preview = plan["route_service_preview"]
    bundle = preview["prompt_bundle"]
    identity = preview["service_generated_route_identity"]
    prechecks = {item["precheck_id"]: item for item in preview["action_prechecks"]}
    bundle_json = json.dumps(bundle, sort_keys=True)

    assert preview["available"] is True
    assert preview["template_id"] == "mf_workflow_runtime.v1"
    assert preview["counts_as_close_evidence"] is False
    assert preview["authorizes_protected_write"] is False
    assert identity["route_context_hash"] == bundle["route_context_hash"]
    assert identity["prompt_contract_hash"] == bundle["prompt_contract_hash"]
    assert identity["prompt_contract_id"] == bundle["prompt_contract"]["prompt_contract_id"]
    assert identity["route_context_hash"].startswith("sha256:")
    assert identity["route_context_hash"] != plan["route_context"]["route_context_hash"]
    assert "raw_prompt" not in bundle_json
    assert prechecks["observer_dispatch_bounded_worker"]["result"]["decision"] == "allow"
    assert prechecks["implementation_worker_apply_patch"]["result"]["decision"] == "block"
    assert "bounded dispatch/startup evidence" in (
        prechecks["implementation_worker_apply_patch"]["route_action_gate"]["reason"]
    )
