from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.governance.reconcile_semantic_config import (
    DEFAULT_CONFIG_PATH,
    PROJECT_OVERRIDE_PATH,
    SemanticConfigValidationError,
    apply_project_ai_routing,
    load_semantic_enrichment_config,
)
from agent.governance import reconcile_semantic_ai
from agent.governance.reconcile_semantic_ai import build_semantic_ai_call, resolve_semantic_ai_route


def test_default_semantic_config_loads_state_only_profile():
    config = load_semantic_enrichment_config()

    assert config.analyzer == "reconcile_semantic"
    assert config.provider == "anthropic"
    assert config.model == "claude-opus-4-7"
    assert config.analyzer_role == "reconcile_semantic_analyzer"
    assert config.chain_role == "pm"
    assert config.role == "pm"
    assert config.job_profiles["node"].analyzer_role == "reconcile_node_semantic_analyzer"
    assert config.job_profiles["edge"].analyzer_role == "reconcile_edge_semantic_analyzer"
    assert config.job_profiles["global_review"].analyzer_role == "reconcile_global_semantic_reviewer"
    assert config.job_profiles["graph_structure"].analyzer_role == "reconcile_graph_structure_analyzer"
    assert config.graph_structure_ops.schema_version == "graph_structure_ops.v1"
    assert config.graph_structure_ops.analyzer_role == "reconcile_graph_structure_analyzer"
    assert config.graph_structure_ops.operations["add_edge"]["enabled"] is True
    assert config.graph_structure_ops.operations["add_edge"]["edge_allowlist"] == [
        "calls",
        "configures",
        "depends_on",
        "documents",
        "imports",
        "tests",
        "uses",
    ]
    assert config.graph_structure_ops.evidence_policy["dedupe_operations"] is True
    assert config.graph_structure_ops.evidence_policy["calls"] == {
        "require_call_evidence": True,
        "import_only_action": "downgrade",
        "downgrade_to": "imports",
    }
    assert config.graph_structure_ops.bridge_policy["calls"]["require_concrete_evidence"] is True
    assert config.graph_structure_ops.bridge_policy["calls"]["weak_evidence_action"] == "downgrade"
    assert config.graph_structure_ops.bridge_policy["calls"]["downgrade_to"] == "imports"
    assert "function_call" in config.graph_structure_ops.bridge_policy["calls"]["evidence_kinds"]
    assert config.job_profiles["retry"].analyzer_role == "reconcile_semantic_retry_reviewer"
    assert config.job_profiles["dry_run"].analyzer_role == "reconcile_semantic_dry_run"
    assert config.executables["anthropic"] == "claude"
    assert config.executables["openai"] == "codex"
    assert config.use_ai_default is False
    assert config.execution_policy.ai_input_mode == "feature"
    assert config.execution_policy.dynamic_semantic_graph_state is True
    assert config.execution_policy.worker_max_concurrency == 10
    assert config.execution_policy.worker_claim_batch_size == 10
    assert config.execution_policy.worker_lease_seconds == 600
    assert config.automation_policy.semantic_mode == "manual"
    assert config.automation_policy.feedback_review_mode == "enqueue_only"
    assert config.automation_policy.graph_apply_mode == "manual"
    assert "modify_code" not in config.permissions_can
    assert "mutate_graph_topology" in config.permissions_cannot
    payload = config.to_instruction_payload()
    assert payload["mutate_project_files"] is False
    assert payload["mutate_graph_topology"] is False
    assert payload["role"] == "reconcile_node_semantic_analyzer"
    assert payload["analyzer_role"] == "reconcile_node_semantic_analyzer"
    assert payload["chain_role"] == "pm"
    assert payload["legacy_role_alias"] == "pm"
    assert payload["job_type"] == "node"
    assert payload["execution_policy"]["ai_input_mode"] == "feature"
    assert payload["execution_policy"]["worker_max_concurrency"] == 10
    assert payload["execution_policy"]["worker_claim_batch_size"] == 10
    assert payload["automation_policy"]["feedback_review_mode"] == "enqueue_only"
    assert payload["graph_structure_ops"]["bridge_policy"]["calls"]["downgrade_to"] == "imports"
    assert payload["prompt_template"]
    structure_payload = config.to_instruction_payload("graph_structure")
    assert structure_payload["job_type"] == "graph_structure"
    assert structure_payload["role"] == "reconcile_graph_structure_analyzer"
    assert "graph_structure_ops.v1" in structure_payload["job_profile"]["prompt_template"]
    assert structure_payload["graph_structure_ops"]["bridge_policy"]["calls"]["weak_evidence_action"] == "downgrade"
    assert Path(DEFAULT_CONFIG_PATH).exists()


def test_graph_structure_ops_config_can_disable_operations_and_edges(tmp_path):
    cfg = tmp_path / "semantic-ops.yaml"
    cfg.write_text(
        "\n".join(
            [
                'version: "1.0"',
                "analyzer: reconcile_semantic",
                "prompt_template: semantic prompt",
                "graph_structure_ops:",
                "  operations:",
                "    add_edge:",
                "      enabled: true",
                "      edge_allowlist: [tests]",
                "    suppress_edge:",
                "      enabled: false",
            ]
        ),
        encoding="utf-8",
    )

    config = load_semantic_enrichment_config(config_path=cfg)

    assert config.graph_structure_ops.operations["add_edge"]["edge_allowlist"] == ["tests"]
    assert config.graph_structure_ops.operations["suppress_edge"]["enabled"] is False


def test_graph_structure_bridge_policy_can_override_weak_calls(tmp_path):
    cfg = tmp_path / "semantic-bridge-policy.yaml"
    cfg.write_text(
        "\n".join(
            [
                'version: "1.0"',
                "analyzer: reconcile_semantic",
                "prompt_template: semantic prompt",
                "graph_structure_ops:",
                "  bridge_policy:",
                "    calls:",
                "      weak_evidence_action: downgrade",
                "      downgrade_to: depends_on",
                "      evidence_kinds: [function_call]",
            ]
        ),
        encoding="utf-8",
    )

    config = load_semantic_enrichment_config(config_path=cfg)

    calls_policy = config.graph_structure_ops.bridge_policy["calls"]
    assert calls_policy["weak_evidence_action"] == "downgrade"
    assert calls_policy["downgrade_to"] == "depends_on"
    assert calls_policy["evidence_kinds"] == ["function_call"]
    structure_ops = config.to_instruction_payload("graph_structure")["graph_structure_ops"]
    assert structure_ops["bridge_policy"]["calls"]["downgrade_to"] == "depends_on"


def test_semantic_worker_execution_policy_invalid_values_fall_back(tmp_path):
    cfg = tmp_path / "semantic-worker-policy.yaml"
    cfg.write_text(
        "\n".join(
            [
                'version: "1.0"',
                "analyzer: reconcile_semantic",
                "prompt_template: semantic prompt",
                "execution_policy:",
                "  worker_max_concurrency: nope",
                "  worker_claim_batch_size: -5",
                "  worker_lease_seconds: 5",
            ]
        ),
        encoding="utf-8",
    )

    config = load_semantic_enrichment_config(config_path=cfg)

    assert config.execution_policy.worker_max_concurrency == 4
    assert config.execution_policy.worker_claim_batch_size == 4
    assert config.execution_policy.worker_lease_seconds == 600


def test_project_ai_routing_overrides_semantic_provider_and_model(monkeypatch):
    config = load_semantic_enrichment_config()
    monkeypatch.setattr(
        "agent.governance.project_service.get_project_config_metadata",
        lambda project_id: {
            "project_id": project_id,
            "ai": {
                "routing": {
                    "semantic": {"provider": "openai", "model": "gpt-5.5"}
                }
            },
        },
    )

    updated = apply_project_ai_routing(config, project_id="demo-project")

    assert updated.provider == "openai"
    assert updated.model == "gpt-5.5"
    assert "demo-project:ai.routing.semantic" in updated.override_path


def test_project_ai_routing_can_override_semantic_worker_policy(monkeypatch):
    config = load_semantic_enrichment_config()
    monkeypatch.setattr(
        "agent.governance.project_service.get_project_config_metadata",
        lambda project_id: {
            "project_id": project_id,
            "ai": {
                "routing": {
                    "semantic": {
                        "worker": {
                            "worker_max_concurrency": 7,
                            "claim_batch_size": 8,
                            "claim_lease_seconds": 901,
                        }
                    }
                }
            },
        },
    )

    updated = apply_project_ai_routing(config, project_id="demo-project")

    assert updated.execution_policy.worker_max_concurrency == 7
    assert updated.execution_policy.worker_claim_batch_size == 8
    assert updated.execution_policy.worker_lease_seconds == 901
    assert "demo-project:ai.routing.semantic" in updated.override_path


def test_legacy_role_field_maps_to_chain_role_not_analyzer(tmp_path):
    cfg = tmp_path / "legacy-semantic.yaml"
    cfg.write_text(
        "\n".join(
            [
                'version: "1.0"',
                "analyzer: reconcile_semantic",
                "role: qa",
                "prompt_template: legacy route config",
            ]
        ),
        encoding="utf-8",
    )

    config = load_semantic_enrichment_config(config_path=cfg)

    assert config.analyzer_role == "reconcile_semantic_analyzer"
    assert config.chain_role == "qa"
    assert config.role == "qa"
    payload = config.to_instruction_payload()
    assert payload["role"] == "reconcile_node_semantic_analyzer"
    assert payload["chain_role"] == "qa"
    assert payload["legacy_role_alias"] == "qa"
    graph_structure = config.to_instruction_payload("graph_structure")
    assert "graph_structure_ops.v1" in graph_structure["job_profile"]["prompt_template"]
    assert "payload.output_contract" in graph_structure["job_profile"]["prompt_template"]


def test_job_profile_aliases_support_per_job_overrides(tmp_path):
    cfg = tmp_path / "profiles.yaml"
    cfg.write_text(
        "\n".join(
            [
                'version: "1.0"',
                "analyzer: reconcile_semantic",
                "analyzer_role: reconcile_semantic_analyzer",
                "chain_role: pm",
                "provider: anthropic",
                "model: claude-opus-4-7",
                "job_profiles:",
                "  features:",
                "    analyzer_role: custom_node_analyzer",
                "    prompt_template: Node-only prompt",
                "  edges:",
                "    analyzer_role: custom_edge_analyzer",
                "    model: gpt-edge",
                "    provider: openai",
                "  global:",
                "    analyzer_role: custom_global_reviewer",
                "prompt_template: base prompt",
            ]
        ),
        encoding="utf-8",
    )

    config = load_semantic_enrichment_config(config_path=cfg)

    assert config.job_profile("reconcile_semantic_feature").analyzer_role == "custom_node_analyzer"
    assert config.job_profile("edge_semantic_requested").analyzer_role == "custom_edge_analyzer"
    assert config.job_profile("edge").provider == "openai"
    assert config.job_profile("edge").model == "gpt-edge"
    assert config.job_profile("reconcile_global_semantic_review").analyzer_role == "custom_global_reviewer"
    assert config.to_instruction_payload("edge")["job_profile"]["model"] == "gpt-edge"


def test_project_override_merges_with_default(tmp_path):
    project = tmp_path / "project"
    override_path = project / PROJECT_OVERRIDE_PATH
    override_path.parent.mkdir(parents=True)
    override_path.write_text(
        "\n".join(
            [
                'model: "gpt-test-semantic"',
                "use_ai_default: true",
                "input_policy:",
                "  max_excerpt_chars: 77",
                "execution_policy:",
                "  ai_input_mode: batch",
                "automation_policy:",
                "  semantic_mode: auto",
                "  feedback_review_mode: auto",
                "  graph_apply_mode: manual",
                "  review_workers: 2",
                "prompt_template: |-",
                "  Custom project semantic analyzer prompt.",
            ]
        ),
        encoding="utf-8",
    )

    config = load_semantic_enrichment_config(project_root=project)

    assert config.model == "gpt-test-semantic"
    assert config.use_ai_default is True
    assert config.input_policy.max_excerpt_chars == 77
    assert config.execution_policy.ai_input_mode == "batch"
    assert config.execution_policy.worker_max_concurrency == 10
    assert config.execution_policy.worker_claim_batch_size == 10
    assert config.automation_policy.semantic_mode == "auto"
    assert config.automation_policy.feedback_review_mode == "auto"
    assert config.automation_policy.review_workers == 2
    assert config.prompt_template == "Custom project semantic analyzer prompt."
    assert config.executables["anthropic"] == "claude"
    assert "read_graph_snapshot" in config.permissions_can
    assert config.override_path == str(override_path)


def test_semantic_ai_route_can_be_enabled_by_env(monkeypatch):
    config = load_semantic_enrichment_config()
    monkeypatch.setenv("RECONCILE_SEMANTIC_AI_PROVIDER", "openai")
    monkeypatch.setenv("RECONCILE_SEMANTIC_AI_MODEL", "gpt-test-semantic")

    route = resolve_semantic_ai_route(config)

    assert route["provider"] == "openai"
    assert route["model"] == "gpt-test-semantic"
    assert route["source"] == "env"


def test_semantic_ai_claude_call_resolves_path_binary(monkeypatch, tmp_path):
    config = load_semantic_enrichment_config()
    config.executables["anthropic"] = r"C:\Users\tester\.local\bin\claude.exe"
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.setattr(reconcile_semantic_ai.shutil, "which", lambda name: None)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout='{"feature_name":"Governance Trace","semantic_summary":"Trace is auditable."}',
            stderr="",
        )

    monkeypatch.setattr(reconcile_semantic_ai.subprocess, "run", fake_run)
    ai_call = build_semantic_ai_call(
        semantic_config=config,
        project_id="aming-claw",
        snapshot_id="full-test",
        project_root=tmp_path,
    )

    result = ai_call("reconcile_semantic_feature", {"feature": {"node_id": "L7.1"}})

    assert calls
    assert calls[0][0].endswith("claude.exe")
    assert "--model" in calls[0]
    assert "claude-opus-4-7" in calls[0]
    assert result["feature_name"] == "Governance Trace"
    assert result["_ai_route"]["executable_source"] == "config"


def test_semantic_ai_claude_call_extracts_wrapped_result(monkeypatch, tmp_path):
    config = load_semantic_enrichment_config()
    config.executables["anthropic"] = "claude-test"
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"type":"result","is_error":false,'
                '"result":"{\\"feature_name\\":\\"Trace\\",'
                '\\"semantic_summary\\":\\"Wrapped JSON is parsed.\\"}"}'
            ),
            stderr="",
        )

    monkeypatch.setattr(reconcile_semantic_ai.subprocess, "run", fake_run)
    ai_call = build_semantic_ai_call(
        semantic_config=config,
        project_id="aming-claw",
        snapshot_id="full-test",
        project_root=tmp_path,
    )

    result = ai_call("reconcile_semantic_feature", {"feature": {"node_id": "L7.1"}})

    assert calls
    assert result["feature_name"] == "Trace"
    assert result["semantic_summary"] == "Wrapped JSON is parsed."
    assert result["_ai_cli_result"]["type"] == "result"


def test_semantic_ai_normalizes_opus_dot_alias(monkeypatch):
    config = load_semantic_enrichment_config()
    config.model = "claude-opus-4.7"
    monkeypatch.delenv("RECONCILE_SEMANTIC_AI_PROVIDER", raising=False)
    monkeypatch.delenv("RECONCILE_SEMANTIC_AI_MODEL", raising=False)

    route = resolve_semantic_ai_route(config)

    assert route["provider"] == "anthropic"
    assert route["model"] == "claude-opus-4-7"


def test_semantic_ai_openai_call_streams_prompt_on_stdin(monkeypatch, tmp_path):
    config = load_semantic_enrichment_config()
    monkeypatch.setenv("RECONCILE_SEMANTIC_AI_PROVIDER", "openai")
    monkeypatch.setenv("RECONCILE_SEMANTIC_AI_MODEL", "gpt-test-semantic")
    monkeypatch.setenv("CODEX_BIN", "codex-test")
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        calls.append({"cmd": cmd, "input": kwargs.get("input")})
        output_path = Path(cmd[cmd.index("-o") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            '{"feature_name":"Governance Trace","semantic_summary":"Trace is auditable."}',
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_semantic_ai.subprocess, "run", fake_run)
    ai_call = build_semantic_ai_call(
        semantic_config=config,
        project_id="aming-claw",
        snapshot_id="full-test",
        project_root=tmp_path,
    )

    result = ai_call("reconcile_semantic_feature", {"feature": {"node_id": "L7.1"}})

    assert calls
    assert calls[0]["cmd"][-1] == "-"
    assert "Payload:" in calls[0]["input"]
    assert result["feature_name"] == "Governance Trace"
    assert result["_ai_route"]["provider"] == "openai"


def test_graph_structure_ai_prompt_uses_structure_contract(monkeypatch, tmp_path):
    config = load_semantic_enrichment_config()
    monkeypatch.setenv("RECONCILE_SEMANTIC_AI_PROVIDER", "openai")
    monkeypatch.setenv("RECONCILE_SEMANTIC_AI_MODEL", "gpt-test-semantic")
    monkeypatch.setenv("CODEX_BIN", "codex-test")
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        calls.append({"cmd": cmd, "input": kwargs.get("input")})
        output_path = Path(cmd[cmd.index("-o") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            (
                '{"schema_version":"graph_structure_ops.v1",'
                '"source":{"snapshot_id":"scope-test","base_commit":"abc",'
                '"analyzer_role":"reconcile_graph_structure_analyzer"},'
                '"operations":[],"self_check":{"valid":true,'
                '"checked_rules":["schema_version"],"known_risks":[]}}'
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_semantic_ai.subprocess, "run", fake_run)
    ai_call = build_semantic_ai_call(
        semantic_config=config,
        project_id="aming-claw",
        snapshot_id="scope-test",
        project_root=tmp_path,
    )

    result = ai_call(
        "graph_structure",
        {
            "snapshot_id": "scope-test",
            "output_contract": {
                "schema_version": "graph_structure_ops.v1",
                "supported_operations": ["move_file", "add_edge", "suppress_edge"],
            },
        },
    )

    assert result["schema_version"] == "graph_structure_ops.v1"
    assert calls
    prompt = calls[0]["input"]
    assert "graph_structure_ops.v1" in prompt
    assert "payload.output_contract" in prompt
    assert "move_file" in prompt
    assert "add_edge" in prompt
    assert "suppress_edge" in prompt
    assert "requested semantic fields" not in prompt


def test_semantic_ai_rejects_error_only_json(monkeypatch, tmp_path):
    config = load_semantic_enrichment_config()
    monkeypatch.setenv("RECONCILE_SEMANTIC_AI_PROVIDER", "openai")
    monkeypatch.setenv("RECONCILE_SEMANTIC_AI_MODEL", "gpt-test-semantic")
    monkeypatch.setenv("CODEX_BIN", "codex-test")

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "git":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        output_path = Path(cmd[cmd.index("-o") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text('{"error":"Cannot read supplied payload."}', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(reconcile_semantic_ai.subprocess, "run", fake_run)
    ai_call = build_semantic_ai_call(
        semantic_config=config,
        project_id="aming-claw",
        snapshot_id="full-test",
        project_root=tmp_path,
    )

    with pytest.raises(RuntimeError, match="error response"):
        ai_call("reconcile_semantic_feature", {"feature": {"node_id": "L7.1"}})


def test_semantic_config_rejects_mutation_permissions(tmp_path):
    cfg = tmp_path / "semantic.yaml"
    cfg.write_text(
        "\n".join(
            [
                'version: "1.0"',
                "analyzer: reconcile_semantic",
                "permissions:",
                "  can:",
                "    - modify_code",
                "prompt_template: unsafe",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(SemanticConfigValidationError, match="mutation permissions"):
        load_semantic_enrichment_config(config_path=cfg)
