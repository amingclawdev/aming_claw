from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "test-scenario-manager.mjs"


def _run_manager(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = subprocess.run(
        ["node", str(SCRIPT), *args],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"manager exited {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def _json(result: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(result.stdout)


def _write_route_manifest(project_root: Path, routes: list[dict], manifest_path: Path | None = None) -> Path:
    if manifest_path is None:
        manifest_path = project_root / ".aming-claw" / "test-routes.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": "external-demo",
                "routes": routes,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return manifest_path


def test_doctor_json_reports_backend_blocker_without_failing_hard(tmp_path: Path) -> None:
    result = _run_manager(
        "doctor",
        "--json",
        "--state-dir",
        str(tmp_path / "state"),
        "--backend",
        "http://127.0.0.1:9",
    )
    payload = _json(result)

    assert payload["ok"] is False
    assert result.returncode == 0
    assert any(blocker["reason_code"] == "backend_unreachable" for blocker in payload["blockers"])
    assert {tool["id"] for tool in payload["tools"]} == {"node", "python", "git"}
    assert payload["registry"]["schema_version"] == 1
    assert {
        "simple_user_entry",
        "paradigm_route_context_demo",
        "dashboard_mock_ai_playwright_fixture",
        "mock_ai_docker_fixture",
        "service_router_docker_fixture",
        "service_router_ai_structured_output_fixture",
        "service_router_live_ai_environment_tester",
        "ruby_graph_sinatra",
    }.issubset(set(payload["registry"]["scenario_ids"]))
    assert payload["registry"]["scenario_count"] == 8
    assert payload["paths"]["cache_inside_repo"] is False


def test_plan_output_lists_scenarios_actions_and_fixture_metadata(tmp_path: Path) -> None:
    result = _run_manager(
        "plan",
        "--json",
        "--state-dir",
        str(tmp_path / "state"),
    )
    payload = _json(result)
    scenarios = {scenario["scenario_id"]: scenario for scenario in payload["scenarios"]}
    scenario = scenarios["simple_user_entry"]
    paradigm = scenarios["paradigm_route_context_demo"]
    dashboard_mock = scenarios["dashboard_mock_ai_playwright_fixture"]
    mock_docker = scenarios["mock_ai_docker_fixture"]
    docker = scenarios["service_router_docker_fixture"]
    ai_fixture = scenarios["service_router_ai_structured_output_fixture"]
    live_probe = scenarios["service_router_live_ai_environment_tester"]
    ruby = scenarios["ruby_graph_sinatra"]

    assert payload["selected_count"] == 8
    assert set(scenarios) == {
        "simple_user_entry",
        "paradigm_route_context_demo",
        "dashboard_mock_ai_playwright_fixture",
        "mock_ai_docker_fixture",
        "service_router_docker_fixture",
        "service_router_ai_structured_output_fixture",
        "service_router_live_ai_environment_tester",
        "ruby_graph_sinatra",
    }
    assert scenario["scenario_id"] == "simple_user_entry"
    assert scenario["target_project"] == "aming-claw"
    assert scenario["test_flow_route"]["schema_version"] == "test_flow_route.v1"
    assert scenario["test_flow_route"]["decision"] == "mixed"
    assert set(scenario["test_flow_route"]["lanes"]) == {"focused_unit", "e2e_fixture"}
    assert scenario["test_flow_route"]["prompt_alert_bundle"]["noise_policy"] == "selected_lanes_only"
    assert {
        alert["lane"] for alert in scenario["test_flow_route"]["prompt_alert_bundle"]["alerts"]
    } == {"focused_unit", "e2e_fixture"}
    commands = {command["id"]: command for command in scenario["commands"]}
    assert "simple_mode_project_inbox_flow" in commands
    assert (
        "agent/tests/test_raw_requirement.py::"
        "test_simple_mode_observer_command_flow_for_execution_and_worker_controls"
    ) in commands["simple_mode_project_inbox_flow"]["command"]
    assert commands["simple_mode_project_inbox_flow"]["env"] == {
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"
    }
    assert "observer_session_command_queue" in commands
    assert paradigm["test_flow_route"]["decision"] == "mixed"
    assert paradigm["test_flow_route"]["primary_lane"] == "fixture_only"
    assert paradigm["test_flow_route"]["lanes"] == ["focused_unit", "fixture_only"]
    assert paradigm["test_flow_route"]["model_calls"] == "forbidden"
    assert paradigm["test_flow_route"]["autorun"] is True
    assert {
        alert["code"] for alert in paradigm["test_flow_route"]["prompt_alert_bundle"]["alerts"]
    } == {"test_flow_focused_unit", "test_flow_fixture_only"}
    assert "dashboard_mock_ai_playwright_route_declared" in paradigm["test_flow_route"][
        "evidence_ids"
    ]
    assert "route_prompt_bundle_is_hashable_and_low_noise" in paradigm["test_flow_route"][
        "evidence_ids"
    ]
    paradigm_commands = {command["id"]: command for command in paradigm["commands"]}
    assert paradigm_commands["paradigm_route_context_demo"]["command"][:2] == [
        "python",
        "scripts/paradigm-route-context-demo.py",
    ]
    dashboard_alerts = dashboard_mock["test_flow_route"]["prompt_alert_bundle"]["alerts"]
    dashboard_commands = {command["id"]: command for command in dashboard_mock["commands"]}
    assert dashboard_mock["execution_policy"]["lane"] == "playwright_mock_ai"
    assert dashboard_mock["execution_policy"]["model_calls"] == "mocked"
    assert dashboard_mock["execution_policy"]["live_ai"] == "disabled"
    assert dashboard_mock["test_flow_route"]["decision"] == "playwright_mock_ai"
    assert dashboard_mock["test_flow_route"]["primary_lane"] == "playwright_mock_ai"
    assert dashboard_mock["test_flow_route"]["lanes"] == ["playwright_mock_ai"]
    assert dashboard_mock["test_flow_route"]["model_calls"] == "mocked"
    assert dashboard_mock["test_flow_route"]["autorun"] is True
    assert dashboard_alerts[0]["code"] == "test_flow_playwright_mock_ai"
    assert dashboard_alerts[0]["severity"] == "warning"
    assert "call_live_ai" in dashboard_alerts[0]["blocked_actions"]
    assert dashboard_mock["safety"]["mock_ai"] is True
    assert dashboard_mock["safety"]["calls_models"] is False
    assert dashboard_mock["fixtures"][0]["kind"] == "mock_ai_timeline"
    assert (
        "frontend/dashboard/scripts/e2e-demo-mock-ai.mjs"
        in dashboard_commands["dashboard_mock_ai_playwright_evidence"]["command"]
    )
    mock_docker_deps = {item["id"]: item for item in mock_docker["dependency_decisions"]}
    assert mock_docker["execution_policy"]["requires_flags"] == ["--allow-docker"]
    assert mock_docker["execution_policy"]["model_calls"] == "mocked"
    assert mock_docker["test_flow_route"]["decision"] == "docker_fixture"
    assert mock_docker["test_flow_route"]["primary_lane"] == "docker_fixture"
    assert mock_docker["test_flow_route"]["requires_flags"] == ["--allow-docker"]
    assert mock_docker["test_flow_route"]["model_calls"] == "mocked"
    assert mock_docker["safety"]["no_ai_credentials"] is True
    assert mock_docker["safety"]["fixed_structured_output"] is True
    assert mock_docker["fixtures"][0]["kind"] == "docker_fixture"
    assert mock_docker["fixtures"][1]["kind"] == "json_fixture"
    assert mock_docker_deps["docker_fixture"]["status"] == "planned"
    assert "docker image inspect" in " ".join(mock_docker_deps["docker_fixture"]["command"])
    mock_docker_commands = {command["id"]: command for command in mock_docker["commands"]}
    container_command = mock_docker_commands["mock_ai_docker_no_credential_structured_output"]["command"]
    assert container_command[:2] == ["sh", "-lc"]
    assert "docker run" in container_command[2]
    assert "--network none" in container_command[2]
    assert "--pull=never" in container_command[2]
    assert "--entrypoint node" in container_command[2]
    assert "runtime: \"docker\"" in container_command[2]
    docker_deps = {item["id"]: item for item in docker["dependency_decisions"]}
    assert docker["execution_policy"]["requires_flags"] == ["--allow-docker"]
    assert docker["execution_policy"]["model_calls"] == "forbidden"
    assert docker["test_flow_route"]["decision"] == "docker_fixture"
    assert docker["test_flow_route"]["primary_lane"] == "docker_fixture"
    assert docker["test_flow_route"]["requires_flags"] == ["--allow-docker"]
    assert docker["test_flow_route"]["model_calls"] == "forbidden"
    docker_alerts = docker["test_flow_route"]["prompt_alert_bundle"]["alerts"]
    assert [alert["lane"] for alert in docker_alerts] == ["docker_fixture"]
    assert docker_alerts[0]["severity"] == "block"
    assert "start_docker_without_approval" in docker_alerts[0]["blocked_actions"]
    assert docker["safety"]["uses_docker_fixture"] is True
    assert docker["safety"]["calls_models"] is False
    assert docker["fixtures"][0]["kind"] == "docker_fixture"
    assert docker["fixtures"][0]["calls_models"] is False
    assert docker_deps["docker_fixture"]["kind"] == "capability"
    assert docker_deps["docker_fixture"]["required"] is True
    assert docker_deps["docker_fixture"]["status"] == "planned"
    assert docker_deps["docker_fixture"]["command"] == [
        "docker",
        "info",
        "--format",
        "{{json .ServerVersion}}",
    ]
    ai_deps = {item["id"]: item for item in ai_fixture["dependency_decisions"]}
    assert ai_fixture["execution_policy"]["live_ai"] == "manual_auth_unknown"
    assert ai_fixture["execution_policy"]["model_calls"] == "forbidden"
    assert ai_fixture["test_flow_route"]["decision"] == "fixture_only"
    assert ai_fixture["test_flow_route"]["autorun"] is True
    assert ai_fixture["test_flow_route"]["model_calls"] == "forbidden"
    assert ai_fixture["test_flow_route"]["prompt_alert_bundle"]["alerts"][0]["lane"] == "fixture_only"
    assert ai_fixture["safety"]["auth_status"] == "unknown"
    assert ai_fixture["safety"]["calls_models"] is False
    assert ai_fixture["fixtures"][0]["kind"] == "json_fixture"
    assert ai_fixture["fixtures"][0]["calls_models"] is False
    assert ai_deps["ai_structured_output_fixture"]["required"] is True
    assert ai_deps["live_ai_runtime"]["required"] is False
    assert ai_deps["live_ai_runtime"]["status"] == "planned"
    live_deps = {item["id"]: item for item in live_probe["dependency_decisions"]}
    live_commands = {command["id"]: command for command in live_probe["commands"]}
    assert live_probe["execution_policy"]["lane"] == "live_ai_environment_probe"
    assert live_probe["execution_policy"]["requires_flags"] == ["--allow-live-ai"]
    assert live_probe["execution_policy"]["model_calls"] == "explicit_probe_only"
    assert live_probe["test_flow_route"]["decision"] == "live_ai_environment_probe"
    assert live_probe["test_flow_route"]["requires_flags"] == ["--allow-live-ai"]
    assert live_probe["test_flow_route"]["model_calls"] == "explicit_probe_only"
    assert live_probe["test_flow_route"]["prompt_alert_bundle"]["alerts"][0]["severity"] == "block"
    assert live_probe["safety"]["environment_probe"] is True
    assert live_probe["safety"]["calls_models"] is True
    assert live_probe["route_context"]["service_id"] == "service_router.live_ai_environment_tester"
    assert live_probe["fixtures"][0]["kind"] == "environment_probe"
    assert live_probe["fixtures"][0]["script"] == "scripts/live-ai-environment-probe.mjs"
    assert {item["id"] for item in live_probe["evidence_requirements"]} >= {
        "live_ai_probe_gate",
        "expected_provider_model_match",
        "cli_detected_version_path",
        "auth_or_invocation_evidence",
        "sanitized_prompt_output",
        "no_silent_quota_use",
    }
    assert live_deps["live_ai_environment_probe"]["status"] == "planned"
    assert live_deps["live_ai_environment_probe"]["required"] is True
    assert "scripts/live-ai-environment-probe.mjs" in live_deps["live_ai_environment_probe"]["command"]
    assert "--role" in live_deps["live_ai_environment_probe"]["command"]
    assert "--allow-live-ai" in live_deps["live_ai_environment_probe"]["command"]
    assert (
        "scripts/live-ai-environment-probe.mjs"
        in live_commands["service_router_live_ai_environment_probe"]["command"]
    )
    assert "--allow-live-ai" in live_commands["service_router_live_ai_environment_probe"]["command"]
    assert ruby["target_project"] == "test-scenario-ruby-sinatra"
    assert ruby["repository"]["commit"] == "5236d3459b8b9015e5ce21ddd0c6beb0db4081d4"
    assert ruby["repository"]["workspace_path"] == str(tmp_path / "state" / "workspaces" / "sinatra")
    assert ruby["validation"]["required_path"] == "lib/sinatra/base.rb"
    assert ruby["test_flow_route"]["decision"] == "external_graph_fixture"
    assert ruby["test_flow_route"]["requires_flags"] == ["--allow-network", "--allow-bootstrap"]


def test_plan_loads_external_manifest_route_with_registration_metadata(tmp_path: Path) -> None:
    project_root = tmp_path / "external-project"
    project_root.mkdir()
    manifest_path = _write_route_manifest(
        project_root,
        [
            {
                "id": "external_dashboard_route",
                "title": "External dashboard route",
                "lane": "e2e_fixture",
                "runner": "commands",
                "lifecycle": "manual_review",
                "side_effect_class": "isolated_governance_fixture",
                "commands": [
                    {
                        "id": "metadata_only",
                        "cwd": "external_project",
                        "command": ["{node}", "-e", "console.log('external metadata ok')"],
                    }
                ],
                "dependencies": [],
                "fixtures": [{"id": "external-fixture", "kind": "json_fixture"}],
                "evidence_requirements": [{"id": "registration_evidence", "kind": "route_registration"}],
            }
        ],
    )
    result = _run_manager(
        "plan",
        "--scenario",
        "external_dashboard_route",
        "--json",
        "--state-dir",
        str(tmp_path / "state"),
        "--project-root",
        str(project_root),
    )
    payload = _json(result)
    [scenario] = payload["scenarios"]
    registration = scenario["route_registration"]
    expected_hash = "sha256:" + hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    assert payload["selected_count"] == 1
    assert scenario["target_project"] == "external-demo"
    assert registration == scenario["test_flow_route"]["route_registration"]
    assert registration["source"] == "external_project_manifest"
    assert registration["project_id"] == "external-demo"
    assert registration["project_root"] == str(project_root)
    assert registration["manifest_path"] == str(manifest_path)
    assert registration["manifest_hash"] == expected_hash
    assert registration["route_id"] == "external_dashboard_route"
    assert registration["trust_level"] == "source_controlled"
    assert registration["lifecycle"] == "manual_review"
    assert registration["side_effect_class"] == "isolated_governance_fixture"
    assert scenario["test_flow_route"]["decision"] == "e2e_fixture"
    assert scenario["test_flow_route"]["primary_lane"] == "e2e_fixture"
    assert scenario["test_flow_route"]["lanes"] == ["e2e_fixture"]
    assert scenario["test_flow_route"]["prompt_alert_bundle"]["alerts"][0]["lane"] == "e2e_fixture"
    assert scenario["commands"][0]["resolved_cwd"] == str(project_root)


def test_run_executes_external_manifest_commands_in_project_root(tmp_path: Path) -> None:
    project_root = tmp_path / "external-project"
    project_root.mkdir()
    (project_root / "marker.txt").write_text("external cwd marker\n", encoding="utf-8")
    manifest_path = _write_route_manifest(
        project_root,
        [
            {
                "id": "external_cwd_route",
                "title": "External cwd route",
                "lane": "focused_unit",
                "runner": "commands",
                "lifecycle": "local",
                "side_effect_class": "local_test",
                "commands": [
                    {
                        "id": "external_project_cwd",
                        "cwd": "external_project",
                        "command": [
                            "{node}",
                            "-e",
                            "const fs=require('node:fs'); if(!fs.existsSync('marker.txt')) process.exit(7); console.log(process.cwd());",
                        ],
                    },
                    {
                        "id": "project_cwd_alias",
                        "cwd": "project",
                        "command": [
                            "{node}",
                            "-e",
                            "const fs=require('node:fs'); if(!fs.existsSync('marker.txt')) process.exit(8); console.log(process.cwd());",
                        ],
                    },
                ],
                "dependencies": [],
                "fixtures": [],
                "evidence_requirements": [{"id": "cwd_evidence", "kind": "command_summary"}],
            }
        ],
        project_root / "aming-claw-test-routes.json",
    )
    result = _run_manager(
        "run",
        "--scenario",
        "external_cwd_route",
        "--json",
        "--state-dir",
        str(tmp_path / "state"),
        "--project-root",
        str(project_root),
        "--test-route-manifest",
        str(manifest_path),
    )
    payload = _json(result)
    [report] = payload["reports"]

    assert report["status"] == "passed"
    assert report["route_registration"]["source"] == "external_project_manifest"
    assert report["route_registration"]["manifest_path"] == str(manifest_path)
    assert report["route_registration"]["project_root"] == str(project_root)
    assert report["test_flow_route"]["route_registration"] == report["route_registration"]
    assert [summary["status"] for summary in report["command_summaries"]] == ["passed", "passed"]
    assert [summary["cwd"] for summary in report["command_summaries"]] == [str(project_root), str(project_root)]
    assert all(str(project_root) in summary["stdout_tail"] for summary in report["command_summaries"])


def test_external_fixture_only_manifest_lane_controls_route_without_pytest_or_e2e(tmp_path: Path) -> None:
    project_root = tmp_path / "external-project"
    project_root.mkdir()
    _write_route_manifest(
        project_root,
        [
            {
                "id": "external_fixture_only_route",
                "title": "External fixture-only route",
                "lane": "fixture_only",
                "runner": "commands",
                "lifecycle": "stable",
                "side_effect_class": "read",
                "commands": [
                    {
                        "id": "metadata_only",
                        "cwd": "external_project",
                        "command": ["{node}", "-e", "console.log('fixture metadata ok')"],
                    }
                ],
                "dependencies": [],
                "fixtures": [{"id": "config-fixture", "kind": "json_fixture"}],
                "evidence_requirements": [{"id": "fixture_policy", "kind": "test_flow_route"}],
            }
        ],
    )
    result = _run_manager(
        "plan",
        "--scenario",
        "external_fixture_only_route",
        "--json",
        "--state-dir",
        str(tmp_path / "state"),
        "--project-root",
        str(project_root),
    )
    payload = _json(result)
    [scenario] = payload["scenarios"]
    route = scenario["test_flow_route"]

    assert route["decision"] == "fixture_only"
    assert route["primary_lane"] == "fixture_only"
    assert route["lanes"] == ["fixture_only"]
    assert route["model_calls"] == "forbidden"
    assert route["autorun"] is True
    assert route["prompt_alert_bundle"]["alerts"][0]["code"] == "test_flow_fixture_only"
    assert scenario["execution_policy"]["model_calls"] == "forbidden"


def test_service_router_fixture_dependency_gating_shape(tmp_path: Path) -> None:
    docker_result = _run_manager(
        "run",
        "--scenario",
        "service_router_docker_fixture",
        "--json",
        "--state-dir",
        str(tmp_path / "docker-state"),
        check=False,
    )
    docker_payload = _json(docker_result)
    [docker_report] = docker_payload["reports"]
    docker_deps = {item["id"]: item for item in docker_report["dependency_decisions"]}

    assert docker_result.returncode == 2
    assert docker_payload["ok"] is False
    assert docker_report["status"] == "blocked"
    assert docker_report["blocked"]["reason_code"] == "dependency_docker_fixture_blocked"
    assert docker_deps["docker_fixture"]["status"] == "blocked"
    assert "--allow-docker" in docker_deps["docker_fixture"]["reason"]
    assert docker_report["safety"]["calls_models"] is False
    assert docker_report["execution_policy"]["model_calls"] == "forbidden"
    assert docker_report["test_flow_route"]["decision"] == "docker_fixture"
    assert docker_report["test_flow_route"]["prompt_alert_bundle"]["alerts"][0]["lane"] == "docker_fixture"
    assert docker_report["fixtures"][0]["kind"] == "docker_fixture"
    assert docker_report["command_summaries"] == []

    mock_docker_result = _run_manager(
        "run",
        "--scenario",
        "mock_ai_docker_fixture",
        "--json",
        "--state-dir",
        str(tmp_path / "mock-docker-state"),
        check=False,
    )
    mock_docker_payload = _json(mock_docker_result)
    [mock_docker_report] = mock_docker_payload["reports"]
    mock_docker_deps = {item["id"]: item for item in mock_docker_report["dependency_decisions"]}

    assert mock_docker_result.returncode == 2
    assert mock_docker_payload["ok"] is False
    assert mock_docker_report["status"] == "blocked"
    assert mock_docker_report["blocked"]["reason_code"] == "dependency_docker_fixture_blocked"
    assert mock_docker_deps["docker_fixture"]["status"] == "blocked"
    assert mock_docker_report["execution_policy"]["model_calls"] == "mocked"
    assert mock_docker_report["safety"]["mock_ai"] is True
    assert mock_docker_report["safety"]["no_ai_credentials"] is True
    assert mock_docker_report["test_flow_route"]["model_calls"] == "mocked"
    assert mock_docker_report["test_flow_route"]["prompt_alert_bundle"]["alerts"][0]["lane"] == "docker_fixture"
    assert mock_docker_report["command_summaries"] == []

    ai_result = _run_manager(
        "run",
        "--scenario",
        "service_router_ai_structured_output_fixture",
        "--json",
        "--state-dir",
        str(tmp_path / "ai-state"),
    )
    ai_payload = _json(ai_result)
    [ai_report] = ai_payload["reports"]
    ai_deps = {item["id"]: item for item in ai_report["dependency_decisions"]}

    assert ai_report["status"] == "passed"
    assert ai_deps["ai_structured_output_fixture"]["status"] == "available"
    assert ai_deps["live_ai_runtime"]["status"] == "blocked"
    assert ai_deps["live_ai_runtime"]["required"] is False
    assert "--allow-live-ai" in ai_deps["live_ai_runtime"]["reason"]
    assert ai_report["safety"]["auth_status"] == "unknown"
    assert ai_report["safety"]["calls_models"] is False
    assert ai_report["execution_policy"]["live_ai"] == "manual_auth_unknown"
    assert ai_report["test_flow_route"]["decision"] == "fixture_only"
    assert ai_report["test_flow_route"]["prompt_alert_bundle"]["alerts"][0]["code"] == "test_flow_fixture_only"
    assert ai_report["fixtures"][0]["calls_models"] is False
    assert ai_report["command_summaries"][0]["status"] == "passed"

    live_result = _run_manager(
        "run",
        "--scenario",
        "service_router_live_ai_environment_tester",
        "--json",
        "--state-dir",
        str(tmp_path / "live-state"),
        check=False,
    )
    live_payload = _json(live_result)
    [live_report] = live_payload["reports"]
    live_deps = {item["id"]: item for item in live_report["dependency_decisions"]}

    assert live_result.returncode == 2
    assert live_payload["ok"] is False
    assert live_report["status"] == "blocked"
    assert live_report["blocked"]["reason_code"] == "dependency_live_ai_environment_probe_blocked"
    assert live_deps["live_ai_environment_probe"]["status"] == "blocked"
    assert "--allow-live-ai" in live_deps["live_ai_environment_probe"]["reason"]
    assert live_report["safety"]["environment_probe"] is True
    assert live_report["execution_policy"]["live_ai"] == "environment_probe"
    assert live_report["test_flow_route"]["decision"] == "live_ai_environment_probe"
    assert live_report["test_flow_route"]["prompt_alert_bundle"]["alerts"][0]["code"] == "test_flow_live_ai_probe"
    assert live_report["command_summaries"] == []


def test_required_live_ai_dependency_stays_manual_when_flag_supplied(tmp_path: Path) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scenarios": [
                    {
                        "id": "manual_live_ai_probe",
                        "title": "Manual live AI probe",
                        "target_project": "aming-claw",
                        "target_ref": "HEAD",
                        "runner": "commands",
                        "dependencies": [
                            {
                                "id": "live_ai_runtime",
                                "kind": "capability",
                                "required": True,
                            }
                        ],
                        "commands": [
                            {
                                "id": "must_not_run",
                                "cwd": "repo",
                                "command": ["{node}", "-e", "process.exit(9)"],
                            }
                        ],
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    result = _run_manager(
        "run",
        "--scenario",
        "manual_live_ai_probe",
        "--registry",
        str(registry),
        "--json",
        "--state-dir",
        str(tmp_path / "state"),
        "--allow-live-ai",
        check=False,
    )
    payload = _json(result)
    [report] = payload["reports"]
    deps = {item["id"]: item for item in report["dependency_decisions"]}

    assert result.returncode == 2
    assert report["status"] == "blocked"
    assert report["blocked"]["reason_code"] == "dependency_live_ai_runtime_blocked"
    assert "auth is still unknown" in deps["live_ai_runtime"]["reason"]
    assert report["command_summaries"] == []


def test_live_ai_environment_probe_mode_runs_fake_command_when_allowed(tmp_path: Path) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scenarios": [
                    {
                        "id": "fake_live_ai_environment_probe",
                        "title": "Fake live AI environment probe",
                        "target_project": "aming-claw",
                        "target_ref": "HEAD",
                        "runner": "commands",
                        "dependencies": [
                            {
                                "id": "node",
                                "kind": "command",
                                "command": "{node}",
                                "required": True,
                            },
                            {
                                "id": "live_ai_runtime",
                                "kind": "capability",
                                "mode": "environment_probe",
                                "required": True,
                            },
                        ],
                        "execution_policy": {
                            "lane": "live_ai_environment_probe",
                            "requires_flags": ["--allow-live-ai"],
                            "live_ai": "environment_probe",
                            "model_calls": "explicit_probe_only",
                        },
                        "safety": {
                            "environment_probe": True,
                            "live_ai": True,
                            "calls_models": True,
                            "requires_human_approval": True,
                        },
                        "route_context": {
                            "service_id": "service_router.live_ai_environment_tester",
                            "route": "tester",
                        },
                        "fixtures": [
                            {
                                "id": "fake-live-ai-probe",
                                "kind": "environment_probe",
                                "calls_models": False,
                            }
                        ],
                        "evidence_requirements": [
                            {
                                "id": "live_ai_probe_gate",
                                "kind": "dependency_decision",
                                "dependency_id": "live_ai_runtime",
                                "required_status": "allowed",
                            }
                        ],
                        "commands": [
                            {
                                "id": "fake_probe",
                                "cwd": "repo",
                                "command": [
                                    "{node}",
                                    "-e",
                                    "console.log('fake live ai environment probe ok')",
                                ],
                            }
                        ],
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    blocked_result = _run_manager(
        "run",
        "--scenario",
        "fake_live_ai_environment_probe",
        "--registry",
        str(registry),
        "--json",
        "--state-dir",
        str(tmp_path / "blocked-state"),
        check=False,
    )
    blocked_payload = _json(blocked_result)
    [blocked_report] = blocked_payload["reports"]
    blocked_deps = {item["id"]: item for item in blocked_report["dependency_decisions"]}

    assert blocked_result.returncode == 2
    assert blocked_report["status"] == "blocked"
    assert blocked_deps["live_ai_runtime"]["status"] == "blocked"
    assert "--allow-live-ai" in blocked_deps["live_ai_runtime"]["reason"]
    assert blocked_report["command_summaries"] == []

    allowed_result = _run_manager(
        "run",
        "--scenario",
        "fake_live_ai_environment_probe",
        "--registry",
        str(registry),
        "--json",
        "--state-dir",
        str(tmp_path / "allowed-state"),
        "--allow-live-ai",
    )
    allowed_payload = _json(allowed_result)
    [allowed_report] = allowed_payload["reports"]
    allowed_deps = {item["id"]: item for item in allowed_report["dependency_decisions"]}

    assert allowed_report["status"] == "passed"
    assert allowed_deps["live_ai_runtime"]["status"] == "allowed"
    assert allowed_report["command_summaries"][0]["status"] == "passed"
    assert "fake live ai environment probe ok" in allowed_report["command_summaries"][0]["stdout_tail"]


def test_docker_fixture_dependency_can_be_approved_with_command_probe(tmp_path: Path) -> None:
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scenarios": [
                    {
                        "id": "local_docker_fixture_probe",
                        "title": "Local Docker fixture probe",
                        "target_project": "aming-claw",
                        "target_ref": "HEAD",
                        "runner": "commands",
                        "dependencies": [
                            {
                                "id": "docker_fixture",
                                "kind": "capability",
                                "required": True,
                                "command": ["{node}", "--version"],
                            }
                        ],
                        "commands": [
                            {
                                "id": "metadata",
                                "cwd": "repo",
                                "command": ["{node}", "-e", "process.exit(0)"],
                            }
                        ],
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    result = _run_manager(
        "run",
        "--scenario",
        "local_docker_fixture_probe",
        "--registry",
        str(registry),
        "--json",
        "--state-dir",
        str(tmp_path / "state"),
        "--allow-docker",
    )
    payload = _json(result)
    [report] = payload["reports"]
    deps = {item["id"]: item for item in report["dependency_decisions"]}

    assert report["status"] == "passed"
    assert deps["docker_fixture"]["status"] == "allowed"
    assert deps["docker_fixture"]["command"][1] == "--version"


def test_dry_run_updates_state_and_report(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    result = _run_manager(
        "run",
        "--scenario",
        "simple_user_entry",
        "--dry-run",
        "--json",
        "--state-dir",
        str(state_dir),
    )
    payload = _json(result)
    [report] = payload["reports"]

    assert report["status"] == "dry_run"
    assert report["scenario_id"] == "simple_user_entry"
    assert report["target_commit"]
    assert all(summary["skipped"] for summary in report["command_summaries"])

    state = json.loads((state_dir / "state.json").read_text(encoding="utf8"))
    assert state["last_run_id"] == report["run_id"]
    assert state["scenarios"]["simple_user_entry"]["last_status"] == "dry_run"
    report_path = Path(state["scenarios"]["simple_user_entry"]["report_path"])
    assert report_path.exists()
    persisted = json.loads(report_path.read_text(encoding="utf8"))
    assert persisted["run_id"] == report["run_id"]
    assert persisted["artifacts"][-1]["kind"] == "report"


def test_ruby_scenario_blocked_report_shape_without_network(tmp_path: Path) -> None:
    result = _run_manager(
        "run",
        "--scenario",
        "ruby_graph_sinatra",
        "--json",
        "--state-dir",
        str(tmp_path / "state"),
        "--backend",
        "http://127.0.0.1:9",
        check=False,
    )
    payload = _json(result)
    [report] = payload["reports"]

    assert result.returncode == 2
    assert payload["ok"] is False
    assert report["status"] == "blocked"
    assert report["target_project"] == "test-scenario-ruby-sinatra"
    assert report["target_commit"] == "5236d3459b8b9015e5ce21ddd0c6beb0db4081d4"
    assert report["blocked"]["reason_code"] == "governance_unreachable"
    assert "pass --backend" in report["blocked"]["remediation"]
    assert report["dependency_decisions"]
    assert isinstance(report["command_summaries"], list)
    assert isinstance(report["http_summaries"], list)

    state = json.loads((tmp_path / "state" / "state.json").read_text(encoding="utf8"))
    scenario_state = state["scenarios"]["ruby_graph_sinatra"]
    assert scenario_state["status"] == "blocked"
    assert scenario_state["last_status"] == "blocked"
    assert scenario_state["timestamps"]["started_at"]
    assert scenario_state["timestamps"]["completed_at"]
    persisted = json.loads(
        Path(scenario_state["report_path"]).read_text(encoding="utf8")
    )
    assert persisted["blocked"]["reason_code"] == "governance_unreachable"


def test_ruby_scenario_bootstrap_forces_external_project_id(tmp_path: Path) -> None:
    workspace = tmp_path / "state" / "workspaces" / "tiny-ruby"
    workspace.mkdir(parents=True)
    (workspace / "lib").mkdir()
    (workspace / "lib" / "app.rb").write_text("module Local; class App; end; end\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=workspace, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "add", "."], cwd=workspace, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "init"],
        cwd=workspace,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, text=True).strip()

    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "scenarios": [
                    {
                        "id": "local_ruby_graph",
                        "title": "Local Ruby graph",
                        "project_id": "local-ruby-project",
                        "target_project": "local-ruby-project",
                        "runner": "ruby_graph",
                        "repository": {
                            "url": "file:///unused",
                            "ref": commit,
                            "commit": commit,
                            "workspace_name": "tiny-ruby",
                        },
                        "dependencies": [
                            {"id": "git", "kind": "command", "command": "git"},
                            {"id": "governance_bootstrap", "kind": "capability"},
                        ],
                        "validation": {
                            "required_path": "lib/app.rb",
                            "required_language": "ruby",
                            "function_index_queries": ["Local::App"],
                        },
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    bootstrap_bodies: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return None

        def _json(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/health":
                self._json(200, {"status": "ok"})
                return
            if self.path == "/api/graph-governance/local-ruby-project/status":
                self._json(200, {"ok": True, "active_snapshot_id": "snap-local-ruby"})
                return
            self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if self.path == "/api/project/bootstrap":
                bootstrap_bodies.append(body)
                project_id = body.get("config_override", {}).get("project_id", "aming-claw")
                self._json(200, {"project_id": project_id, "snapshot_id": "snap-local-ruby"})
                return
            if self.path == "/api/graph-governance/local-ruby-project/query":
                tool = body.get("tool")
                if tool == "list_layers":
                    result = {"layers": [{"layer": "L7", "count": 1}]}
                elif tool == "find_node_by_path":
                    result = {
                        "matches": [
                            {
                                "node": {
                                    "primary_files": ["lib/app.rb"],
                                    "metadata": {"language": "ruby"},
                                },
                                "primary_file": "lib/app.rb",
                            }
                        ]
                    }
                else:
                    result = {"matches": [{"function": "Local::App", "primary_file": "lib/app.rb"}]}
                self._json(200, {"ok": True, "result": result})
                return
            self._json(404, {"ok": False, "error": "not found"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = _run_manager(
            "run",
            "--scenario",
            "local_ruby_graph",
            "--registry",
            str(registry),
            "--json",
            "--state-dir",
            str(tmp_path / "state"),
            "--backend",
            f"http://127.0.0.1:{server.server_port}",
            "--allow-bootstrap",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    payload = _json(result)
    [report] = payload["reports"]
    assert report["status"] == "passed"
    assert report["target_project"] == "local-ruby-project"
    assert bootstrap_bodies[0]["config_override"]["project_id"] == "local-ruby-project"
    assert bootstrap_bodies[0]["config_override"]["language"] == "ruby"
    assert any("local-ruby-project" in item["url"] for item in report["http_summaries"])
