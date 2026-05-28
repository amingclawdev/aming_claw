from __future__ import annotations

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
        "ruby_graph_sinatra",
    }.issubset(set(payload["registry"]["scenario_ids"]))
    assert payload["paths"]["cache_inside_repo"] is False


def test_plan_output_lists_both_scenarios_and_actions(tmp_path: Path) -> None:
    result = _run_manager(
        "plan",
        "--json",
        "--state-dir",
        str(tmp_path / "state"),
    )
    payload = _json(result)
    scenarios = {scenario["scenario_id"]: scenario for scenario in payload["scenarios"]}
    scenario = scenarios["simple_user_entry"]
    ruby = scenarios["ruby_graph_sinatra"]

    assert set(scenarios) == {"simple_user_entry", "ruby_graph_sinatra"}
    assert scenario["scenario_id"] == "simple_user_entry"
    assert scenario["target_project"] == "aming-claw"
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
    assert ruby["target_project"] == "test-scenario-ruby-sinatra"
    assert ruby["repository"]["commit"] == "5236d3459b8b9015e5ce21ddd0c6beb0db4081d4"
    assert ruby["repository"]["workspace_path"] == str(tmp_path / "state" / "workspaces" / "sinatra")
    assert ruby["validation"]["required_path"] == "lib/sinatra/base.rb"


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
