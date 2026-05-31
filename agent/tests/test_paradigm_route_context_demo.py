from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "paradigm-route-context-demo.py"


def _run_demo(tmp_path: Path) -> dict:
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = subprocess.run(
        [
            "python",
            str(SCRIPT),
            "--json",
            "--state-dir",
            str(tmp_path / "state"),
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"demo exited {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return json.loads(result.stdout)


def _cases(payload: dict) -> dict[str, dict]:
    return {case["id"]: case for case in payload["proof_cases"]}


def test_paradigm_route_context_demo_proves_test_flow_lanes(tmp_path: Path) -> None:
    payload = _run_demo(tmp_path)
    cases = _cases(payload)

    assert payload["schema_version"] == "aming_claw.paradigm_route_context_demo.v1"
    assert payload["ok"] is True
    assert set(payload["surfaces"]) == {
        "intent",
        "contract",
        "relationship_impact",
        "process",
        "constraint",
    }
    assert cases["fixture_only_route_runs"]["status"] == "passed"
    assert cases["fixture_only_route_runs"]["decision"] == "fixture_only"
    assert cases["fixture_only_route_runs"]["model_calls"] == "forbidden"
    assert cases["fixture_only_route_runs"]["calls_models"] is False

    assert cases["dashboard_mock_ai_playwright_route_declared"]["status"] == "planned"
    assert cases["dashboard_mock_ai_playwright_route_declared"]["decision"] == "playwright_mock_ai"
    assert cases["dashboard_mock_ai_playwright_route_declared"]["model_calls"] == "mocked"
    assert cases["dashboard_mock_ai_playwright_route_declared"]["live_ai"] == "disabled"
    assert cases["dashboard_mock_ai_playwright_route_declared"]["alert_codes"] == [
        "test_flow_playwright_mock_ai"
    ]
    assert cases["dashboard_mock_ai_playwright_route_declared"]["fixture_kinds"] == [
        "mock_ai_timeline"
    ]

    assert cases["docker_route_blocks_without_approval"]["status"] == "blocked"
    assert cases["docker_route_blocks_without_approval"]["decision"] == "docker_fixture"
    assert "--allow-docker" in cases["docker_route_blocks_without_approval"]["requires_flags"]
    assert cases["docker_route_blocks_without_approval"]["command_summaries"] == []

    assert cases["mock_ai_docker_route_blocks_without_approval"]["status"] == "blocked"
    assert cases["mock_ai_docker_route_blocks_without_approval"]["decision"] == "docker_fixture"
    assert cases["mock_ai_docker_route_blocks_without_approval"]["model_calls"] == "mocked"
    assert "--allow-docker" in cases["mock_ai_docker_route_blocks_without_approval"]["requires_flags"]
    assert cases["mock_ai_docker_route_blocks_without_approval"]["command_summaries"] == []

    assert cases["live_ai_route_blocks_without_approval"]["status"] == "blocked"
    assert cases["live_ai_route_blocks_without_approval"]["decision"] == "live_ai_environment_probe"
    assert "--allow-live-ai" in cases["live_ai_route_blocks_without_approval"]["requires_flags"]
    assert cases["live_ai_route_blocks_without_approval"]["command_summaries"] == []

    assert cases["live_observer_route_blocks_without_approval"]["status"] == "blocked"
    assert (
        cases["live_observer_route_blocks_without_approval"]["decision"]
        == "live_observer_route_demo"
    )
    assert (
        "--allow-live-ai"
        in cases["live_observer_route_blocks_without_approval"]["requires_flags"]
    )
    assert cases["live_observer_route_blocks_without_approval"]["alert_codes"] == [
        "test_flow_live_observer_route"
    ]
    assert cases["live_observer_route_blocks_without_approval"]["command_summaries"] == []

    observer = cases["live_observer_route_runs_approved_deterministic_timeline"]
    assert observer["status"] == "passed"
    assert observer["decision"] == "live_observer_route_demo"
    assert observer["model_calls"] == "deterministic_test_harness"
    assert observer["live_ai"] == "operator_approved_manual"
    assert observer["structured_output_schema"] == "live_observer_route_demo.v1"
    assert observer["source"] == "deterministic_test_harness"
    assert observer["calls_models"] is False
    assert observer["observer_ack_status"] == "acknowledged"
    assert observer["step_ids"] == [
        "01_route_alert_ack",
        "02_ordered_route_steps",
        "03_sanitized_live_ai_evidence",
    ]
    assert observer["timeline_event_types"] == [
        "route_alert_ack",
        "observer_step_output",
        "observer_step_output",
        "observer_step_output",
        "final_drift_prompt",
    ]
    assert observer["final_drift_prompt_status"] == "shown"
    assert observer["raw_prompt_output_stored"] is False


def test_paradigm_route_context_demo_proves_external_registration_and_prompt_bundle(
    tmp_path: Path,
) -> None:
    payload = _run_demo(tmp_path)
    cases = _cases(payload)

    external = cases["external_project_registers_fixture_route"]
    assert external["status"] == "passed"
    assert external["decision"] == "fixture_only"
    assert external["route_registration"]["source"] == "external_project_manifest"
    assert external["route_registration"]["project_id"] == "paradigm-external-demo"
    assert external["route_registration"]["manifest_hash"] == external["expected_manifest_hash"]
    assert external["route_registration"]["trust_level"] == "source_controlled"

    bundle = cases["route_prompt_bundle_is_hashable_and_low_noise"]
    assert bundle["ok"] is True
    assert bundle["service_id"] == "route.prompt_alert_bundle"
    assert bundle["selected_topology"] == "observer_led_parallel_lanes"
    assert bundle["recommended_topology"] == "mf_parallel.v1"
    assert bundle["route_context_hash"].startswith("sha256:")
    assert bundle["prompt_contract_hash"].startswith("sha256:")
    assert bundle["prompt_contract_id"] == "rprompt-paradigm-route-context-demo"
    assert bundle["alert_codes"] == [
        "independent_verification_required",
        "test_flow_route_demo",
    ]
    assert bundle["raw_context_exposed"] is False
    assert bundle["action_precheck_allowed"] is False
    assert bundle["action_precheck_status"] == "route_action_policy_blocked"
    assert bundle["action_precheck_reason_present"] is True
