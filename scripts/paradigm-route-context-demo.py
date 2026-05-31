#!/usr/bin/env python3
"""Runnable proof case for route context and test-flow routing.

The default proof intentionally avoids Docker, live AI, browser E2E, and primary
governance mutations. It proves that the route layer can:

- select fixture-only, Playwright mock-AI, Docker-gated, live-AI-gated, and
  external manifest lanes;
- keep Docker/live-AI lanes blocked without explicit operator flags while
  keeping dashboard AI validation on fixed mock inputs;
- expose compact prompt alert context with stable hashes; and
- keep raw prompt/private context out of the worker-facing bundle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
MANAGER = REPO_ROOT / "scripts" / "test-scenario-manager.mjs"


def _run(command: list[str], *, cwd: Path = REPO_ROOT, expected_codes: set[int] | None = None) -> subprocess.CompletedProcess[str]:
    expected = expected_codes or {0}
    env = os.environ.copy()
    env.setdefault("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode not in expected:
        raise RuntimeError(
            "command failed with unexpected exit code "
            f"{result.returncode}: {' '.join(command)}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def _manager_json(args: list[str], *, state_dir: Path, expected_codes: set[int] | None = None) -> dict[str, Any]:
    result = _run(
        ["node", str(MANAGER), *args, "--json", "--state-dir", str(state_dir)],
        expected_codes=expected_codes,
    )
    return json.loads(result.stdout)


def _first_scenario(payload: dict[str, Any]) -> dict[str, Any]:
    scenarios = payload.get("scenarios") or []
    if not scenarios:
        raise AssertionError("manager plan returned no scenarios")
    return scenarios[0]


def _first_report(payload: dict[str, Any]) -> dict[str, Any]:
    reports = payload.get("reports") or []
    if not reports:
        raise AssertionError("manager run returned no reports")
    return reports[0]


def _write_external_manifest(project_root: Path) -> Path:
    manifest = {
        "schema_version": 1,
        "project_id": "paradigm-external-demo",
        "routes": [
            {
                "id": "paradigm_external_fixture_only",
                "title": "Paradigm external fixture-only route",
                "lane": "fixture_only",
                "runner": "commands",
                "lifecycle": "stable",
                "side_effect_class": "read",
                "commands": [
                    {
                        "id": "external_fixture_metadata",
                        "cwd": "external_project",
                        "command": [
                            "{node}",
                            "-e",
                            (
                                "const assert = require('node:assert'); "
                                "assert(process.cwd().endsWith('external-route-project')); "
                                "console.log(JSON.stringify({route:'external_fixture_only', cwd:process.cwd()}));"
                            ),
                        ],
                    }
                ],
                "dependencies": [],
                "fixtures": [
                    {
                        "id": "external-route-config",
                        "kind": "json_fixture",
                        "calls_models": False,
                    }
                ],
                "evidence_requirements": [
                    {
                        "id": "external_route_registration",
                        "kind": "route_registration",
                    }
                ],
            }
        ],
    }
    manifest_path = project_root / ".aming-claw" / "test-routes.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def _contract(service_routes: list[dict[str, Any]], event_routes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "contract_instance_id": "paradigm-route-context-contract",
        "service_routes": service_routes,
        "event_routes": event_routes,
    }


def _service_route(service_id: str, mode: str, side_effect_class: str) -> dict[str, Any]:
    return {
        "route_id": f"service.{service_id}",
        "service_id": service_id,
        "mode": mode,
        "side_effect_class": side_effect_class,
        "idempotency_key_policy": {
            "fields": [
                "event_id",
                "event_kind",
                "stage",
                "task_id",
                "backlog_id",
                "route_id",
                "service_id",
            ]
        },
    }


def _route_context_bundle_proof() -> dict[str, Any]:
    sys.path.insert(0, str(REPO_ROOT))
    from agent.governance.service_router import route_event

    prompt_contract = _contract(
        service_routes=[
            _service_route("route.prompt_alert_bundle", "preview", "read"),
        ],
        event_routes=[
            {
                "route_id": "event.route_prompt_context.preview",
                "event_kind": "route.prompt_context.requested",
                "service_route_id": "service.route.prompt_alert_bundle",
                "enabled": True,
            }
        ],
    )
    action_contract = _contract(
        service_routes=[
            _service_route("route.action_precheck", "gate", "gate"),
        ],
        event_routes=[
            {
                "route_id": "event.route_action.pre_mutation",
                "event_kind": "route.action.requested",
                "service_route_id": "service.route.action_precheck",
                "enabled": True,
            }
        ],
    )

    prompt_payload: dict[str, Any] = {
        "priority": "P1",
        "route_id": "paradigm-route-context",
        "stage": "dispatch",
        "caller_role": "implementation_worker",
        "content": {
            "summary": "Prove route-owned context and low-noise alerts.",
            "raw_prompt": "raw prompt must stay hidden",
        },
        "target_files": [
            "scripts/test-scenario-manager.mjs",
            "scripts/test-scenarios.json",
            "docs/demos/route-context-test-routes.md",
        ],
        "test_files": [
            "agent/tests/test_test_scenario_manager.py",
            "agent/tests/test_service_router.py",
        ],
        "acceptance_criteria": [
            "route context hashes are visible",
            "raw prompt text is not exposed",
        ],
        "evidence_required": [
            "focused_tests",
            "route_context_hash",
            "prompt_contract_hash",
        ],
        "prompt_contract": {
            "prompt_contract_id": "rprompt-paradigm-route-context-demo",
            "target_files": [
                "scripts/test-scenario-manager.mjs",
                "scripts/test-scenarios.json",
                "docs/demos/route-context-test-routes.md",
            ],
            "test_files": [
                "agent/tests/test_test_scenario_manager.py",
                "agent/tests/test_service_router.py",
            ],
            "acceptance_criteria": [
                "route context hashes are visible",
                "raw prompt text is not exposed",
            ],
            "evidence_required": [
                "focused_tests",
                "route_context_hash",
                "prompt_contract_hash",
            ],
            "raw_prompt": "worker prompt body must stay hidden",
        },
        "route_alerts": [
            {
                "code": "test_flow_route_demo",
                "severity": "info",
                "message": "Demo must stay deterministic and avoid live AI.",
            }
        ],
        "visible_injection_manifest": {
            "allowed_injections": [
                {
                    "kind": "route_doc",
                    "id": "route-context-test-routes.v1",
                    "path": "docs/demos/route-context-test-routes.md",
                    "sha256": "sha256:demo-doc",
                    "content": "doc body must not be copied into the bundle",
                }
            ]
        },
        "hidden_context": "private observer context must stay hidden",
    }

    prompt_result = route_event(
        {
            "event_id": "evt-paradigm-route-context",
            "event_kind": "route.prompt_context.requested",
            "stage": "dispatch",
            "payload": prompt_payload,
        },
        prompt_contract,
    )

    route = prompt_result["routes"][0]
    bundle = route["result"]["route_prompt_bundle"]
    bundle_json = json.dumps(bundle, sort_keys=True)
    forbidden_fragments = [
        "raw prompt must stay hidden",
        "worker prompt body must stay hidden",
        "private observer context must stay hidden",
        "doc body must not be copied",
    ]
    leaked = [fragment for fragment in forbidden_fragments if fragment in bundle_json]
    if leaked:
        raise AssertionError(f"route prompt bundle leaked hidden context: {leaked}")

    observer_prompt_result = route_event(
        {
            "event_id": "evt-paradigm-route-context-observer",
            "event_kind": "route.prompt_context.requested",
            "stage": "dispatch",
            "payload": {**prompt_payload, "caller_role": "observer"},
        },
        prompt_contract,
    )
    observer_bundle = observer_prompt_result["routes"][0]["result"]["route_prompt_bundle"]
    action_result = route_event(
        {
            "event_id": "evt-paradigm-route-action",
            "event_kind": "route.action.requested",
            "payload": {
                "caller_role": "observer",
                "action": "apply_patch",
                "route_prompt_bundle": observer_bundle,
                "version_check": {"status": "passed", "dirty": False, "dirty_files": []},
                "graph_status": {"current_state": {"graph_stale": {"is_stale": False}}},
            },
        },
        action_contract,
    )
    action_gate = action_result["routes"][0]["result"]["route_action_gate"]

    return {
        "ok": True,
        "service_id": route["service_id"],
        "selected_topology": bundle["selected_topology"],
        "recommended_topology": bundle["recommended_topology"],
        "route_context_hash": bundle["route_context_hash"],
        "prompt_contract_hash": bundle["prompt_contract_hash"],
        "prompt_contract_id": bundle["prompt_contract"]["prompt_contract_id"],
        "alert_codes": [alert["code"] for alert in bundle["alerts"]],
        "raw_context_exposed": False,
        "action_precheck_allowed": bool(action_gate["allowed"]),
        "action_precheck_status": action_gate["status"],
        "action_precheck_reason_present": bool(action_gate["reason"]),
    }


def build_report(state_dir: Path) -> dict[str, Any]:
    state_dir.mkdir(parents=True, exist_ok=True)

    fixture_plan = _first_scenario(
        _manager_json(
            ["plan", "--scenario", "service_router_ai_structured_output_fixture"],
            state_dir=state_dir / "fixture-plan",
        )
    )
    fixture_report = _first_report(
        _manager_json(
            ["run", "--scenario", "service_router_ai_structured_output_fixture"],
            state_dir=state_dir / "fixture-run",
        )
    )
    dashboard_plan = _first_scenario(
        _manager_json(
            ["plan", "--scenario", "dashboard_mock_ai_playwright_fixture"],
            state_dir=state_dir / "dashboard-plan",
        )
    )
    docker_report = _first_report(
        _manager_json(
            ["run", "--scenario", "service_router_docker_fixture"],
            state_dir=state_dir / "docker-run",
            expected_codes={2},
        )
    )
    mock_docker_report = _first_report(
        _manager_json(
            ["run", "--scenario", "mock_ai_docker_fixture"],
            state_dir=state_dir / "mock-ai-docker-run",
            expected_codes={2},
        )
    )
    live_report = _first_report(
        _manager_json(
            ["run", "--scenario", "service_router_live_ai_environment_tester"],
            state_dir=state_dir / "live-run",
            expected_codes={2},
        )
    )
    observer_route_blocked = _first_report(
        _manager_json(
            ["run", "--scenario", "gated_live_ai_observer_route_demo"],
            state_dir=state_dir / "live-observer-blocked-run",
            expected_codes={2},
        )
    )
    observer_route_allowed = _first_report(
        _manager_json(
            [
                "run",
                "--scenario",
                "gated_live_ai_observer_route_demo",
                "--allow-live-ai",
            ],
            state_dir=state_dir / "live-observer-allowed-run",
        )
    )
    observer_route_evidence = observer_route_allowed["structured_outputs"][0]["evidence"]

    external_project = state_dir / "external-route-project"
    external_project.mkdir(parents=True, exist_ok=True)
    manifest_path = _write_external_manifest(external_project)
    external_report = _first_report(
        _manager_json(
            [
                "run",
                "--scenario",
                "paradigm_external_fixture_only",
                "--project-root",
                str(external_project),
            ],
            state_dir=state_dir / "external-run",
        )
    )
    manifest_hash = "sha256:" + hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    route_bundle = _route_context_bundle_proof()

    proof_cases = [
        {
            "id": "fixture_only_route_runs",
            "scenario_id": fixture_report["scenario_id"],
            "status": fixture_report["status"],
            "decision": fixture_report["test_flow_route"]["decision"],
            "primary_lane": fixture_report["test_flow_route"]["primary_lane"],
            "model_calls": fixture_report["test_flow_route"]["model_calls"],
            "alert_codes": [
                alert["code"]
                for alert in fixture_report["test_flow_route"]["prompt_alert_bundle"]["alerts"]
            ],
            "calls_models": fixture_report["safety"]["calls_models"],
            "command_statuses": [item["status"] for item in fixture_report["command_summaries"]],
        },
        {
            "id": "dashboard_mock_ai_playwright_route_declared",
            "scenario_id": dashboard_plan["scenario_id"],
            "status": "planned",
            "decision": dashboard_plan["test_flow_route"]["decision"],
            "primary_lane": dashboard_plan["test_flow_route"]["primary_lane"],
            "model_calls": dashboard_plan["test_flow_route"]["model_calls"],
            "live_ai": dashboard_plan["test_flow_route"]["live_ai"],
            "alert_codes": [
                alert["code"]
                for alert in dashboard_plan["test_flow_route"]["prompt_alert_bundle"]["alerts"]
            ],
            "command_ids": [item["id"] for item in dashboard_plan["commands"]],
            "fixture_kinds": [item["kind"] for item in dashboard_plan["fixtures"]],
        },
        {
            "id": "docker_route_blocks_without_approval",
            "scenario_id": docker_report["scenario_id"],
            "status": docker_report["status"],
            "decision": docker_report["test_flow_route"]["decision"],
            "requires_flags": docker_report["test_flow_route"]["requires_flags"],
            "blocked_reason_code": docker_report["blocked"]["reason_code"],
            "alert_codes": [
                alert["code"]
                for alert in docker_report["test_flow_route"]["prompt_alert_bundle"]["alerts"]
            ],
            "command_summaries": docker_report["command_summaries"],
        },
        {
            "id": "mock_ai_docker_route_blocks_without_approval",
            "scenario_id": mock_docker_report["scenario_id"],
            "status": mock_docker_report["status"],
            "decision": mock_docker_report["test_flow_route"]["decision"],
            "model_calls": mock_docker_report["test_flow_route"]["model_calls"],
            "requires_flags": mock_docker_report["test_flow_route"]["requires_flags"],
            "blocked_reason_code": mock_docker_report["blocked"]["reason_code"],
            "alert_codes": [
                alert["code"]
                for alert in mock_docker_report["test_flow_route"]["prompt_alert_bundle"]["alerts"]
            ],
            "command_summaries": mock_docker_report["command_summaries"],
        },
        {
            "id": "live_ai_route_blocks_without_approval",
            "scenario_id": live_report["scenario_id"],
            "status": live_report["status"],
            "decision": live_report["test_flow_route"]["decision"],
            "requires_flags": live_report["test_flow_route"]["requires_flags"],
            "blocked_reason_code": live_report["blocked"]["reason_code"],
            "alert_codes": [
                alert["code"]
                for alert in live_report["test_flow_route"]["prompt_alert_bundle"]["alerts"]
            ],
            "command_summaries": live_report["command_summaries"],
        },
        {
            "id": "live_observer_route_blocks_without_approval",
            "scenario_id": observer_route_blocked["scenario_id"],
            "status": observer_route_blocked["status"],
            "decision": observer_route_blocked["test_flow_route"]["decision"],
            "requires_flags": observer_route_blocked["test_flow_route"]["requires_flags"],
            "blocked_reason_code": observer_route_blocked["blocked"]["reason_code"],
            "alert_codes": [
                alert["code"]
                for alert in observer_route_blocked["test_flow_route"]["prompt_alert_bundle"]["alerts"]
            ],
            "command_summaries": observer_route_blocked["command_summaries"],
        },
        {
            "id": "live_observer_route_runs_approved_deterministic_timeline",
            "scenario_id": observer_route_allowed["scenario_id"],
            "status": observer_route_allowed["status"],
            "decision": observer_route_allowed["test_flow_route"]["decision"],
            "model_calls": observer_route_allowed["test_flow_route"]["model_calls"],
            "live_ai": observer_route_allowed["test_flow_route"]["live_ai"],
            "structured_output_schema": observer_route_evidence["schema_version"],
            "source": observer_route_evidence["source"],
            "calls_models": observer_route_evidence["live_ai"]["calls_models"],
            "observer_ack_status": observer_route_evidence["observer_evidence"]["route_alert_ack"]["status"],
            "step_ids": [
                step["step_id"]
                for step in observer_route_evidence["observer_evidence"]["ordered_step_outputs"]
            ],
            "timeline_event_types": [
                event["event_type"]
                for event in observer_route_evidence["timeline"]
            ],
            "final_drift_prompt_status": observer_route_evidence["observer_evidence"]["final_drift_prompt"]["status"],
            "raw_prompt_output_stored": not observer_route_evidence["observer_evidence"]["no_raw_prompt_output"],
        },
        {
            "id": "external_project_registers_fixture_route",
            "scenario_id": external_report["scenario_id"],
            "status": external_report["status"],
            "decision": external_report["test_flow_route"]["decision"],
            "route_registration": {
                key: external_report["route_registration"][key]
                for key in (
                    "source",
                    "project_id",
                    "manifest_hash",
                    "route_id",
                    "trust_level",
                    "side_effect_class",
                )
            },
            "expected_manifest_hash": manifest_hash,
            "command_statuses": [item["status"] for item in external_report["command_summaries"]],
        },
        {
            "id": "route_prompt_bundle_is_hashable_and_low_noise",
            **route_bundle,
        },
    ]

    ok = (
        fixture_plan["test_flow_route"]["decision"] == "fixture_only"
        and fixture_report["status"] == "passed"
        and fixture_report["safety"]["calls_models"] is False
        and dashboard_plan["test_flow_route"]["decision"] == "playwright_mock_ai"
        and dashboard_plan["test_flow_route"]["model_calls"] == "mocked"
        and docker_report["status"] == "blocked"
        and "--allow-docker" in docker_report["test_flow_route"]["requires_flags"]
        and mock_docker_report["status"] == "blocked"
        and mock_docker_report["test_flow_route"]["model_calls"] == "mocked"
        and "--allow-docker" in mock_docker_report["test_flow_route"]["requires_flags"]
        and live_report["status"] == "blocked"
        and "--allow-live-ai" in live_report["test_flow_route"]["requires_flags"]
        and observer_route_blocked["status"] == "blocked"
        and observer_route_blocked["test_flow_route"]["decision"] == "live_observer_route_demo"
        and observer_route_allowed["status"] == "passed"
        and observer_route_evidence["source"] == "deterministic_test_harness"
        and observer_route_evidence["live_ai"]["calls_models"] is False
        and observer_route_evidence["observer_evidence"]["route_alert_ack"]["status"] == "acknowledged"
        and observer_route_evidence["observer_evidence"]["final_drift_prompt"]["status"] == "shown"
        and observer_route_evidence["observer_evidence"]["no_raw_prompt_output"] is True
        and external_report["status"] == "passed"
        and external_report["route_registration"]["manifest_hash"] == manifest_hash
        and route_bundle["raw_context_exposed"] is False
        and route_bundle["action_precheck_allowed"] is False
    )

    return {
        "schema_version": "aming_claw.paradigm_route_context_demo.v1",
        "ok": bool(ok),
        "summary": (
            "Route-owned context selects the right test lane, emits only lane-specific "
            "alerts, blocks unsafe Docker/live-AI/observer routes without approval, and exposes "
            "hashable prompt context without raw prompt leakage. Dashboard validation uses "
            "Playwright with mock AI inputs; AI-related Docker validation stays gated, while "
            "the live observer route demo records deterministic timeline-shaped observer evidence."
        ),
        "surfaces": {
            "intent": "scenario ids and external manifest route ids state the test intent",
            "contract": "test_flow_route and route_prompt_bundle define allowed lanes, files, flags, and evidence",
            "relationship_impact": "external manifest registration binds a target project root and manifest hash",
            "process": "test-scenario-manager run reports record passed/blocked command evidence",
            "constraint": "prompt_alert_bundle blocks Docker/live-AI/observer routes without explicit flags and action_precheck blocks observer mutation",
        },
        "proof_cases": proof_cases,
        "state_dir": str(state_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the route context/test-route paradigm demo.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    parser.add_argument("--state-dir", default="", help="Directory for demo state and manager reports.")
    args = parser.parse_args()

    if args.state_dir:
        state_dir = Path(args.state_dir).resolve()
        report = build_report(state_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="aming-claw-paradigm-demo-") as tmp:
            report = build_report(Path(tmp))

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["summary"])
        for case in report["proof_cases"]:
            print(f"- {case['id']}: {case.get('status', 'ok')} {case.get('decision', '')}".rstrip())
        print(f"ok: {str(report['ok']).lower()}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
