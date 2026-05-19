"""Configuration loader for state-only reconcile semantic enrichment."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config" / "reconcile" / "semantic_enrichment.yaml"
PROJECT_OVERRIDE_PATH = Path(".aming-claw") / "reconcile" / "semantic_enrichment.yaml"

_REQUIRED_FIELDS = {"version", "analyzer", "prompt_template"}
_FORBIDDEN_ALLOWED = {
    "modify_code",
    "modify_docs",
    "modify_tests",
    "mutate_graph_topology",
    "run_command",
    "execute_script",
    "create_chain_task",
    "finalize_snapshot",
}
DEFAULT_SEMANTIC_WORKER_MAX_CONCURRENCY = 4
DEFAULT_SEMANTIC_WORKER_CLAIM_BATCH_SIZE = 4
DEFAULT_SEMANTIC_WORKER_LEASE_SECONDS = 600
_GRAPH_STRUCTURE_EDGE_ALLOWLIST = {
    "calls",
    "configures",
    "depends_on",
    "documents",
    "imports",
    "tests",
    "uses",
}
_DEFAULT_GRAPH_STRUCTURE_BRIDGE_CALL_EVIDENCE_KINDS = [
    "call",
    "calls",
    "call_reference",
    "direct_call",
    "function_call",
    "function_calls",
    "resolved_call",
    "resolved_function_call",
    "runtime_call",
    "strong_call",
]

GRAPH_STRUCTURE_PROMPT_TEMPLATE = """You are the reconcile graph structure analyzer.

Your job is to propose graph structure operations that can be validated by the
graph_structure_ops.v1 gate and later materialized as source hint blocks. You
do not modify files, database rows, or graph topology.

Return exactly one JSON object with schema_version "graph_structure_ops.v1".
Only emit operations listed in payload.output_contract.supported_operations.
Do not emit operations missing from the supplied contract, direct DB patches,
or graph topology mutations outside the gate contract."""

GRAPH_ENRICH_CONFIG_PROMPT_TEMPLATE = """You are the reconcile graph enrich config analyzer.

Your job is to propose configuration-rule operations that can be validated by
the graph_enrich_config_ops.v1 gate and later written to the project semantic
enrichment override config. You do not modify files, database rows, or graph
topology.

Return exactly one JSON object with schema_version "graph_enrich_config_ops.v1".
Only emit operations listed in payload.output_contract.supported_operations.
Do not emit direct file patches or graph topology mutations."""


def _graph_enrich_config_ops_instruction_payload() -> dict[str, Any]:
    from .graph_enrich_config_ops import graph_enrich_config_ops_output_contract

    contract = graph_enrich_config_ops_output_contract()
    return {
        "schema_version": str(contract.get("schema_version") or "graph_enrich_config_ops.v1"),
        "output_contract": contract,
    }


class SemanticConfigError(Exception):
    """Base exception for semantic analyzer config failures."""


class SemanticConfigValidationError(SemanticConfigError):
    """Raised when semantic analyzer config is invalid."""


@dataclass
class SemanticInputPolicy:
    include_source_excerpt: bool = True
    max_excerpt_chars: int = 12000
    include_symbol_refs: bool = True
    include_doc_refs: bool = True
    include_config_refs: bool = True
    include_review_feedback: bool = True
    include_file_hashes: bool = True


@dataclass
class SemanticExecutionPolicy:
    ai_input_mode: str = "feature"
    dynamic_semantic_graph_state: bool = True
    worker_max_concurrency: int = DEFAULT_SEMANTIC_WORKER_MAX_CONCURRENCY
    worker_claim_batch_size: int = DEFAULT_SEMANTIC_WORKER_CLAIM_BATCH_SIZE
    worker_lease_seconds: int = DEFAULT_SEMANTIC_WORKER_LEASE_SECONDS


@dataclass
class SemanticAutomationPolicy:
    semantic_mode: str = "manual"
    feedback_review_mode: str = "enqueue_only"
    graph_apply_mode: str = "manual"
    review_workers: int = 1
    review_lanes: list[str] = field(default_factory=lambda: ["graph_patch_candidate", "review_required"])


@dataclass
class SemanticJobProfile:
    analyzer_role: str = ""
    provider: str = ""
    model: str = ""
    prompt_template: str = ""
    use_ai_default: bool | None = None
    max_tokens: int | None = None
    temperature: float | None = None


@dataclass
class GraphStructureOpsConfig:
    schema_version: str = "graph_structure_ops.v1"
    analyzer_role: str = "reconcile_graph_structure_analyzer"
    operations: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence_policy: dict[str, Any] = field(default_factory=dict)
    bridge_policy: dict[str, Any] = field(default_factory=dict)


@dataclass
class GraphEnrichConfigOpsConfig:
    schema_version: str = "graph_enrich_config_ops.v1"
    rules: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class SemanticAnalyzerConfig:
    version: str
    analyzer: str
    provider: str = "anthropic"
    model: str = "claude-opus-4-7"
    analyzer_role: str = "reconcile_semantic_analyzer"
    chain_role: str = "pm"
    # Deprecated alias: historically this meant the chain/pipeline role used
    # only for model routing. Keep it loaded for old callers, but do not expose
    # it as the semantic analyzer identity in prompts.
    role: str = "pm"
    executables: dict[str, str] = field(default_factory=dict)
    use_ai_default: bool = False
    temperature: float = 0.0
    max_tokens: int = 4000
    permissions_can: list[str] = field(default_factory=list)
    permissions_cannot: list[str] = field(default_factory=list)
    input_policy: SemanticInputPolicy = field(default_factory=SemanticInputPolicy)
    execution_policy: SemanticExecutionPolicy = field(default_factory=SemanticExecutionPolicy)
    automation_policy: SemanticAutomationPolicy = field(default_factory=SemanticAutomationPolicy)
    job_profiles: dict[str, SemanticJobProfile] = field(default_factory=dict)
    graph_structure_ops: GraphStructureOpsConfig = field(default_factory=GraphStructureOpsConfig)
    graph_enrich_config_ops: GraphEnrichConfigOpsConfig = field(default_factory=GraphEnrichConfigOpsConfig)
    output_schema: dict[str, Any] = field(default_factory=dict)
    prompt_template: str = ""
    source_path: str = ""
    override_path: str = ""

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        source_path: str = "",
        override_path: str = "",
    ) -> "SemanticAnalyzerConfig":
        missing = _REQUIRED_FIELDS - set(data)
        if missing:
            raise SemanticConfigValidationError(
                f"Missing required semantic config fields: {sorted(missing)}"
            )
        analyzer = str(data.get("analyzer") or "").strip()
        if not analyzer:
            raise SemanticConfigValidationError("'analyzer' cannot be empty")
        prompt_template = str(data.get("prompt_template") or "").strip()
        if not prompt_template:
            raise SemanticConfigValidationError("'prompt_template' cannot be empty")
        permissions = data.get("permissions") or {}
        if not isinstance(permissions, dict):
            raise SemanticConfigValidationError("'permissions' must be a mapping")
        can = [str(item) for item in (permissions.get("can") or []) if str(item)]
        cannot = [str(item) for item in (permissions.get("cannot") or []) if str(item)]
        forbidden = sorted(set(can) & _FORBIDDEN_ALLOWED)
        if forbidden:
            raise SemanticConfigValidationError(
                "semantic analyzer cannot allow mutation permissions: "
                + ", ".join(forbidden)
            )
        input_policy_raw = data.get("input_policy") or {}
        if not isinstance(input_policy_raw, dict):
            raise SemanticConfigValidationError("'input_policy' must be a mapping")
        try:
            max_excerpt = int(input_policy_raw.get("max_excerpt_chars", 12000))
        except (TypeError, ValueError) as exc:
            raise SemanticConfigValidationError("input_policy.max_excerpt_chars must be an integer") from exc
        if max_excerpt < 0:
            raise SemanticConfigValidationError("input_policy.max_excerpt_chars must be >= 0")
        input_policy = SemanticInputPolicy(
            include_source_excerpt=bool(input_policy_raw.get("include_source_excerpt", True)),
            max_excerpt_chars=max_excerpt,
            include_symbol_refs=bool(input_policy_raw.get("include_symbol_refs", True)),
            include_doc_refs=bool(input_policy_raw.get("include_doc_refs", True)),
            include_config_refs=bool(input_policy_raw.get("include_config_refs", True)),
            include_review_feedback=bool(input_policy_raw.get("include_review_feedback", True)),
            include_file_hashes=bool(input_policy_raw.get("include_file_hashes", True)),
        )
        execution_policy_raw = data.get("execution_policy") or {}
        if not isinstance(execution_policy_raw, dict):
            raise SemanticConfigValidationError("'execution_policy' must be a mapping")
        execution_policy = SemanticExecutionPolicy(
            ai_input_mode=_normalize_ai_input_mode(
                execution_policy_raw.get("ai_input_mode", "feature")
            ),
            dynamic_semantic_graph_state=bool(
                execution_policy_raw.get("dynamic_semantic_graph_state", True)
            ),
            worker_max_concurrency=_bounded_int(
                _first_present(
                    execution_policy_raw,
                    "worker_max_concurrency",
                    "max_concurrent_ai_calls",
                    "semantic_worker_max_workers",
                ),
                default=DEFAULT_SEMANTIC_WORKER_MAX_CONCURRENCY,
                min_value=1,
                max_value=32,
            ),
            worker_claim_batch_size=_bounded_int(
                _first_present(
                    execution_policy_raw,
                    "worker_claim_batch_size",
                    "claim_batch_size",
                    "semantic_worker_claim_batch_size",
                ),
                default=DEFAULT_SEMANTIC_WORKER_CLAIM_BATCH_SIZE,
                min_value=1,
                max_value=100,
            ),
            worker_lease_seconds=_bounded_int(
                _first_present(
                    execution_policy_raw,
                    "worker_lease_seconds",
                    "claim_lease_seconds",
                    "semantic_worker_lease_seconds",
                ),
                default=DEFAULT_SEMANTIC_WORKER_LEASE_SECONDS,
                min_value=30,
                max_value=86400,
            ),
        )
        automation_raw = data.get("automation_policy") or data.get("automation") or {}
        if not isinstance(automation_raw, dict):
            raise SemanticConfigValidationError("'automation_policy' must be a mapping")
        try:
            review_workers = int(automation_raw.get("review_workers", 1))
        except (TypeError, ValueError) as exc:
            raise SemanticConfigValidationError("automation_policy.review_workers must be an integer") from exc
        if review_workers < 0:
            raise SemanticConfigValidationError("automation_policy.review_workers must be >= 0")
        raw_review_lanes = automation_raw.get("review_lanes") or ["graph_patch_candidate", "review_required"]
        if isinstance(raw_review_lanes, str):
            raw_review_lanes = [item.strip() for item in raw_review_lanes.split(",")]
        automation_policy = SemanticAutomationPolicy(
            semantic_mode=_normalize_automation_mode(automation_raw.get("semantic_mode", "manual")),
            feedback_review_mode=_normalize_automation_mode(
                automation_raw.get("feedback_review_mode", "enqueue_only")
            ),
            graph_apply_mode=_normalize_graph_apply_mode(
                automation_raw.get("graph_apply_mode", "manual")
            ),
            review_workers=review_workers,
            review_lanes=[
                str(item).strip()
                for item in raw_review_lanes
                if str(item).strip()
            ],
        )
        try:
            max_tokens = int(data.get("max_tokens", 4000))
        except (TypeError, ValueError) as exc:
            raise SemanticConfigValidationError("'max_tokens' must be an integer") from exc
        try:
            temperature = float(data.get("temperature", 0.0))
        except (TypeError, ValueError) as exc:
            raise SemanticConfigValidationError("'temperature' must be numeric") from exc
        raw_executables = data.get("executables") or {}
        if not isinstance(raw_executables, dict):
            raise SemanticConfigValidationError("'executables' must be a mapping")
        executables = {
            str(provider).strip(): str(command).strip()
            for provider, command in raw_executables.items()
            if str(provider).strip() and str(command).strip()
        }
        if data.get("claude_bin"):
            executables["anthropic"] = str(data.get("claude_bin")).strip()
        if data.get("codex_bin"):
            executables["openai"] = str(data.get("codex_bin")).strip()
        legacy_role = str(data.get("role") or "").strip()
        analyzer_role = str(
            data.get("analyzer_role")
            or data.get("reconcile_role")
            or data.get("semantic_role")
            or data.get("role_name")
            or "reconcile_semantic_analyzer"
        ).strip()
        if not analyzer_role:
            raise SemanticConfigValidationError("'analyzer_role' cannot be empty")
        chain_role = str(
            data.get("chain_role")
            or data.get("pipeline_role")
            or legacy_role
            or "pm"
        ).strip()
        if not chain_role:
            raise SemanticConfigValidationError("'chain_role' cannot be empty")
        job_profiles = _parse_job_profiles(data.get("job_profiles") or data.get("job_profile") or {})
        graph_structure_ops = _parse_graph_structure_ops_config(data.get("graph_structure_ops"))
        graph_enrich_config_ops = _parse_graph_enrich_config_ops_config(
            data.get("graph_enrich_config_ops")
        )
        return cls(
            version=str(data.get("version") or ""),
            analyzer=analyzer,
            provider=str(data.get("provider") or "anthropic"),
            model=str(data.get("model") or ""),
            analyzer_role=analyzer_role,
            chain_role=chain_role,
            role=legacy_role or chain_role,
            executables=executables,
            use_ai_default=bool(data.get("use_ai_default", False)),
            temperature=temperature,
            max_tokens=max_tokens,
            permissions_can=can,
            permissions_cannot=cannot,
            input_policy=input_policy,
            execution_policy=execution_policy,
            automation_policy=automation_policy,
            job_profiles=job_profiles,
            graph_structure_ops=graph_structure_ops,
            graph_enrich_config_ops=graph_enrich_config_ops,
            output_schema=data.get("output_schema") if isinstance(data.get("output_schema"), dict) else {},
            prompt_template=prompt_template,
            source_path=source_path,
            override_path=override_path,
        )

    def job_profile(self, job_type: str | None) -> SemanticJobProfile:
        normalized = _normalize_semantic_job_type(job_type)
        if normalized and normalized in self.job_profiles:
            return self.job_profiles[normalized]
        return SemanticJobProfile(analyzer_role=self.analyzer_role)

    def to_instruction_payload(self, job_type: str | None = None) -> dict[str, Any]:
        profile = self.job_profile(job_type)
        analyzer_role = profile.analyzer_role or self.analyzer_role
        return {
            "mode": "state_only_semantic_enrichment",
            "analyzer": self.analyzer,
            "analyzer_role": analyzer_role,
            "chain_role": self.chain_role,
            "pipeline_role": self.chain_role,
            "legacy_role_alias": self.role,
            "job_type": _normalize_semantic_job_type(job_type) or "node",
            "job_profile": asdict(profile),
            "provider": self.provider,
            "model": self.model,
            "role": analyzer_role,
            "executables": dict(self.executables),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "mutate_project_files": False,
            "mutate_graph_topology": False,
            "return_semantic_fields_and_suggestions_only": True,
            "permissions": {
                "can": sorted(set(self.permissions_can)),
                "cannot": sorted(set(self.permissions_cannot)),
            },
            "input_policy": asdict(self.input_policy),
            "execution_policy": asdict(self.execution_policy),
            "automation_policy": asdict(self.automation_policy),
            "graph_structure_ops": asdict(self.graph_structure_ops),
            "graph_enrich_config_ops": {
                **asdict(self.graph_enrich_config_ops),
                **_graph_enrich_config_ops_instruction_payload(),
            },
            "output_schema": self.output_schema,
            "prompt_template": self.prompt_template,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "analyzer": self.analyzer,
            "provider": self.provider,
            "model": self.model,
            "executables": dict(self.executables),
            "use_ai_default": self.use_ai_default,
            "source_path": self.source_path,
            "override_path": self.override_path,
            "analyzer_role": self.analyzer_role,
            "chain_role": self.chain_role,
            "legacy_role_alias": self.role,
            "role": self.analyzer_role,
            "input_policy": asdict(self.input_policy),
            "execution_policy": asdict(self.execution_policy),
            "automation_policy": asdict(self.automation_policy),
            "job_profiles": {
                name: asdict(profile)
                for name, profile in sorted(self.job_profiles.items())
            },
            "graph_structure_ops": asdict(self.graph_structure_ops),
            "graph_enrich_config_ops": asdict(self.graph_enrich_config_ops),
        }


def _normalize_ai_input_mode(value: Any) -> str:
    mode = str(value or "feature").strip().lower().replace("-", "_")
    if mode in {"feature", "single", "single_feature", "per_feature", "dynamic_feature"}:
        return "feature"
    if mode in {"batch", "batched", "batch_features"}:
        return "batch"
    raise SemanticConfigValidationError(
        "execution_policy.ai_input_mode must be 'feature' or 'batch'"
    )


def _first_present(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in raw:
            return raw.get(key)
    return None


def _bounded_int(
    value: Any,
    *,
    default: int,
    min_value: int,
    max_value: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed < min_value:
        return default
    if parsed > max_value:
        return max_value
    return parsed


def _normalize_automation_mode(value: Any) -> str:
    mode = str(value or "manual").strip().lower().replace("-", "_")
    if mode in {"off", "disabled", "false"}:
        mode = "manual"
    if mode not in {"manual", "enqueue_only", "auto"}:
        raise SemanticConfigValidationError(
            "automation mode must be 'manual', 'enqueue_only', or 'auto'"
        )
    return mode


def _normalize_graph_apply_mode(value: Any) -> str:
    mode = str(value or "manual").strip().lower().replace("-", "_")
    if mode in {"off", "disabled", "false"}:
        mode = "manual"
    if mode not in {"manual", "auto_low_risk"}:
        raise SemanticConfigValidationError(
            "automation_policy.graph_apply_mode must be 'manual' or 'auto_low_risk'"
        )
    return mode


def _normalize_semantic_job_type(value: Any) -> str:
    mode = str(value or "node").strip().lower().replace("-", "_")
    if not mode:
        return "node"
    if mode in {"node", "nodes", "feature", "features", "l7", "semantic_feature"}:
        return "node"
    if "edge" in mode or mode in {"relation", "relations", "dependency"}:
        return "edge"
    if "global" in mode or mode in {"project_review", "health_review", "semantic_review"}:
        return "global_review"
    if "graph_structure" in mode or mode in {"structure", "structure_ops", "graph_ops"}:
        return "graph_structure"
    if "graph_enrich_config" in mode or mode in {"enrich_config", "config_ops"}:
        return "graph_enrich_config"
    if "retry" in mode or "feedback" in mode or mode in {"repair", "refine"}:
        return "retry"
    if "dry_run" in mode or "preview" in mode:
        return "dry_run"
    if "feature" in mode or mode.startswith("reconcile_semantic"):
        return "node"
    return mode


def _default_job_profiles() -> dict[str, SemanticJobProfile]:
    return {
        "node": SemanticJobProfile(analyzer_role="reconcile_node_semantic_analyzer"),
        "edge": SemanticJobProfile(analyzer_role="reconcile_edge_semantic_analyzer"),
        "global_review": SemanticJobProfile(analyzer_role="reconcile_global_semantic_reviewer"),
        "graph_structure": SemanticJobProfile(
            analyzer_role="reconcile_graph_structure_analyzer",
            prompt_template=GRAPH_STRUCTURE_PROMPT_TEMPLATE,
        ),
        "graph_enrich_config": SemanticJobProfile(
            analyzer_role="reconcile_graph_enrich_config_analyzer",
            prompt_template=GRAPH_ENRICH_CONFIG_PROMPT_TEMPLATE,
        ),
        "retry": SemanticJobProfile(analyzer_role="reconcile_semantic_retry_reviewer"),
        "dry_run": SemanticJobProfile(analyzer_role="reconcile_semantic_dry_run"),
    }


def _job_profile_from_raw(
    raw: Any,
    *,
    default: SemanticJobProfile,
) -> SemanticJobProfile:
    if isinstance(raw, str):
        role = raw.strip()
        if not role:
            raise SemanticConfigValidationError("job profile analyzer_role cannot be empty")
        return SemanticJobProfile(analyzer_role=role)
    if raw is None:
        return default
    if not isinstance(raw, dict):
        raise SemanticConfigValidationError("job profile entries must be mappings or strings")
    analyzer_role = str(
        raw.get("analyzer_role")
        or raw.get("reconcile_role")
        or raw.get("semantic_role")
        or raw.get("role_name")
        or default.analyzer_role
        or ""
    ).strip()
    if not analyzer_role:
        raise SemanticConfigValidationError("job profile analyzer_role cannot be empty")
    max_tokens = raw.get("max_tokens", default.max_tokens)
    if max_tokens is not None:
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError) as exc:
            raise SemanticConfigValidationError("job_profiles.*.max_tokens must be an integer") from exc
        if max_tokens <= 0:
            raise SemanticConfigValidationError("job_profiles.*.max_tokens must be > 0")
    temperature = raw.get("temperature", default.temperature)
    if temperature is not None:
        try:
            temperature = float(temperature)
        except (TypeError, ValueError) as exc:
            raise SemanticConfigValidationError("job_profiles.*.temperature must be numeric") from exc
    use_ai_default = raw.get("use_ai_default", default.use_ai_default)
    if use_ai_default is not None:
        use_ai_default = bool(use_ai_default)
    return SemanticJobProfile(
        analyzer_role=analyzer_role,
        provider=str(raw.get("provider") or default.provider or "").strip(),
        model=str(raw.get("model") or default.model or "").strip(),
        prompt_template=str(raw.get("prompt_template") or default.prompt_template or "").strip(),
        use_ai_default=use_ai_default,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def _parse_job_profiles(raw: Any) -> dict[str, SemanticJobProfile]:
    profiles = _default_job_profiles()
    if raw in (None, ""):
        return profiles
    if not isinstance(raw, dict):
        raise SemanticConfigValidationError("'job_profiles' must be a mapping")
    for key, value in raw.items():
        normalized = _normalize_semantic_job_type(key)
        default = profiles.get(normalized, SemanticJobProfile(analyzer_role=f"reconcile_{normalized}_semantic_analyzer"))
        profiles[normalized] = _job_profile_from_raw(value, default=default)
    return profiles


def _parse_graph_structure_ops_config(raw: Any) -> GraphStructureOpsConfig:
    from .graph_structure_ops import normalize_graph_structure_ops_contract

    if raw not in (None, "") and not isinstance(raw, dict):
        raise SemanticConfigValidationError("'graph_structure_ops' must be a mapping")
    try:
        contract = normalize_graph_structure_ops_contract(raw if isinstance(raw, dict) else None)
    except ValueError as exc:
        raise SemanticConfigValidationError(str(exc)) from exc
    return GraphStructureOpsConfig(
        schema_version=str(contract.get("schema_version") or ""),
        analyzer_role=str(contract.get("analyzer_role") or ""),
        operations={
            str(name): dict(spec)
            for name, spec in (contract.get("operations") or {}).items()
            if isinstance(spec, dict)
        },
        evidence_policy=dict(contract.get("evidence_policy") or {}),
        bridge_policy=_normalize_graph_structure_bridge_policy(
            raw.get("bridge_policy") if isinstance(raw, dict) else None,
            evidence_policy=contract.get("evidence_policy") or {},
        ),
    )


def _parse_graph_enrich_config_ops_config(raw: Any) -> GraphEnrichConfigOpsConfig:
    if raw in (None, ""):
        return GraphEnrichConfigOpsConfig()
    if not isinstance(raw, dict):
        raise SemanticConfigValidationError("'graph_enrich_config_ops' must be a mapping")
    schema_version = str(raw.get("schema_version") or "graph_enrich_config_ops.v1").strip()
    rules_raw = raw.get("rules") or {}
    if not isinstance(rules_raw, dict):
        raise SemanticConfigValidationError("'graph_enrich_config_ops.rules' must be a mapping")
    rules: dict[str, dict[str, Any]] = {}
    for rule_id, rule in rules_raw.items():
        normalized_id = str(rule_id or "").strip()
        if not normalized_id:
            continue
        if not isinstance(rule, dict):
            raise SemanticConfigValidationError(
                "graph_enrich_config_ops.rules.* must be a mapping"
            )
        rules[normalized_id] = dict(rule)
    return GraphEnrichConfigOpsConfig(schema_version=schema_version, rules=rules)


def _normalize_graph_structure_bridge_policy(
    raw: Any,
    *,
    evidence_policy: dict[str, Any],
) -> dict[str, Any]:
    if raw not in (None, "") and not isinstance(raw, dict):
        raise SemanticConfigValidationError("'graph_structure_ops.bridge_policy' must be a mapping")
    calls_evidence = (
        evidence_policy.get("calls")
        if isinstance(evidence_policy.get("calls"), dict)
        else {}
    )
    evidence_action = str(calls_evidence.get("import_only_action") or "downgrade").strip().lower()
    weak_action = {
        "allow": "keep",
        "downgrade": "downgrade",
        "reject": "skip",
    }.get(evidence_action, "downgrade")
    default_policy: dict[str, Any] = {
        "calls": {
            "require_concrete_evidence": bool(calls_evidence.get("require_call_evidence", True)),
            "weak_evidence_action": weak_action,
            "downgrade_to": str(calls_evidence.get("downgrade_to") or "imports").strip(),
            "evidence_kinds": list(_DEFAULT_GRAPH_STRUCTURE_BRIDGE_CALL_EVIDENCE_KINDS),
        }
    }
    policy = _deep_merge(default_policy, raw or {})
    calls = policy.get("calls") if isinstance(policy.get("calls"), dict) else {}
    action = str(calls.get("weak_evidence_action") or "downgrade").strip().lower()
    action = {
        "allow": "keep",
        "allowed": "keep",
        "keep": "keep",
        "downgrade": "downgrade",
        "reject": "skip",
        "skip": "skip",
    }.get(action, action)
    if action not in {"keep", "downgrade", "skip"}:
        raise SemanticConfigValidationError(
            "graph_structure_ops.bridge_policy.calls.weak_evidence_action must be keep, downgrade, or skip"
        )
    downgrade_to = str(calls.get("downgrade_to") or "imports").strip().lower()
    if downgrade_to and downgrade_to not in _GRAPH_STRUCTURE_EDGE_ALLOWLIST:
        raise SemanticConfigValidationError(
            "graph_structure_ops.bridge_policy.calls.downgrade_to must be a supported edge"
        )
    evidence_kinds = calls.get("evidence_kinds") or []
    if isinstance(evidence_kinds, str):
        evidence_kinds = [item.strip() for item in evidence_kinds.split(",")]
    if not isinstance(evidence_kinds, list):
        raise SemanticConfigValidationError(
            "graph_structure_ops.bridge_policy.calls.evidence_kinds must be a list"
        )
    calls["require_concrete_evidence"] = bool(calls.get("require_concrete_evidence", True))
    calls["weak_evidence_action"] = action
    calls["downgrade_to"] = downgrade_to
    calls["evidence_kinds"] = [
        str(item).strip().lower().replace("-", "_").replace(".", "_")
        for item in evidence_kinds
        if str(item).strip()
    ]
    policy["calls"] = dict(calls)
    return policy


def _default_config_dict() -> dict[str, Any]:
    return {
        "version": "1.0",
        "analyzer": "reconcile_semantic",
        "provider": "anthropic",
        "model": "claude-opus-4-7",
        "analyzer_role": "reconcile_semantic_analyzer",
        "chain_role": "pm",
        "executables": {
            "anthropic": "claude",
            "openai": "codex",
        },
        "use_ai_default": False,
        "temperature": 0,
        "max_tokens": 4000,
        "permissions": {
            "can": [
                "read_graph_snapshot",
                "read_governance_index",
                "read_feature_context",
                "read_review_feedback",
                "emit_semantic_index",
                "emit_review_suggestions",
            ],
            "cannot": sorted(_FORBIDDEN_ALLOWED),
        },
        "input_policy": asdict(SemanticInputPolicy()),
        "execution_policy": asdict(SemanticExecutionPolicy()),
        "automation_policy": asdict(SemanticAutomationPolicy()),
        "job_profiles": {
            name: asdict(profile)
            for name, profile in _default_job_profiles().items()
        },
        "graph_structure_ops": asdict(_parse_graph_structure_ops_config(None)),
        "graph_enrich_config_ops": asdict(GraphEnrichConfigOpsConfig()),
        "output_schema": {
            "required": [
                "feature_name",
                "semantic_summary",
                "intent",
                "domain_label",
                "doc_coverage_review",
                "test_coverage_review",
                "config_coverage_review",
                "dependency_patch_suggestions",
                "applied_feedback_ids",
                "self_check",
            ]
        },
        "prompt_template": "You are the reconcile semantic analyzer. Return structured JSON only.",
    }


def _read_yaml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SemanticConfigValidationError(f"Invalid YAML in {path}: {exc}") from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise SemanticConfigValidationError(f"YAML file {path} must contain a mapping")
    return payload


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_semantic_enrichment_config(
    *,
    project_root: str | Path | None = None,
    config_path: str | Path | None = None,
) -> SemanticAnalyzerConfig:
    """Load default semantic analyzer config with optional project override."""
    env_path = os.getenv("RECONCILE_SEMANTIC_CONFIG", "").strip()
    base_path = Path(config_path or env_path or DEFAULT_CONFIG_PATH)
    source_payload = _read_yaml(base_path)
    source_path = str(base_path) if source_payload is not None else ""
    payload = source_payload if source_payload is not None else _default_config_dict()

    override_path = ""
    if project_root:
        candidate = Path(project_root).resolve() / PROJECT_OVERRIDE_PATH
        override_payload = _read_yaml(candidate)
        if override_payload is not None:
            payload = _deep_merge(payload, override_payload)
            override_path = str(candidate)
    return SemanticAnalyzerConfig.from_dict(
        payload,
        source_path=source_path,
        override_path=override_path,
    )


def apply_project_ai_routing(
    config: SemanticAnalyzerConfig,
    *,
    project_id: str | None = None,
) -> SemanticAnalyzerConfig:
    """Apply central project AI routing to a semantic config when present."""
    project_key = str(project_id or "").strip()
    if not project_key:
        return config
    try:
        from . import project_service

        project_config = project_service.get_project_config_metadata(project_key)
    except Exception:
        return config
    ai_config = project_config.get("ai") if isinstance(project_config, dict) else {}
    routing = ai_config.get("routing") if isinstance(ai_config, dict) else {}
    route = routing.get("semantic") if isinstance(routing, dict) else {}
    if not isinstance(route, dict):
        return config
    provider = str(route.get("provider") or "").strip()
    model = str(route.get("model") or "").strip()
    if provider:
        config.provider = provider
    if model:
        config.model = model
    worker_override = _project_worker_policy_override(ai_config, route)
    if worker_override:
        policy = config.execution_policy
        policy.worker_max_concurrency = _bounded_int(
            _first_present(
                worker_override,
                "worker_max_concurrency",
                "max_concurrent_ai_calls",
                "semantic_worker_max_workers",
            ),
            default=policy.worker_max_concurrency,
            min_value=1,
            max_value=32,
        )
        policy.worker_claim_batch_size = _bounded_int(
            _first_present(
                worker_override,
                "worker_claim_batch_size",
                "claim_batch_size",
                "semantic_worker_claim_batch_size",
            ),
            default=policy.worker_claim_batch_size,
            min_value=1,
            max_value=100,
        )
        policy.worker_lease_seconds = _bounded_int(
            _first_present(
                worker_override,
                "worker_lease_seconds",
                "claim_lease_seconds",
                "semantic_worker_lease_seconds",
            ),
            default=policy.worker_lease_seconds,
            min_value=30,
            max_value=86400,
        )
    if provider or model or worker_override:
        marker = f"aming_claw_registry:{project_key}:ai.routing.semantic"
        existing = str(getattr(config, "override_path", "") or "").strip()
        config.override_path = f"{existing}; {marker}" if existing else marker
    return config


def _project_worker_policy_override(
    ai_config: dict[str, Any],
    semantic_route: dict[str, Any],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for source in (
        semantic_route,
        semantic_route.get("worker") if isinstance(semantic_route, dict) else None,
        semantic_route.get("execution_policy") if isinstance(semantic_route, dict) else None,
        ai_config.get("semantic_worker") if isinstance(ai_config, dict) else None,
    ):
        if isinstance(source, dict):
            for key in (
                "worker_max_concurrency",
                "max_concurrent_ai_calls",
                "semantic_worker_max_workers",
                "worker_claim_batch_size",
                "claim_batch_size",
                "semantic_worker_claim_batch_size",
                "worker_lease_seconds",
                "claim_lease_seconds",
                "semantic_worker_lease_seconds",
            ):
                if key in source:
                    merged[key] = source.get(key)
    return merged


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_SEMANTIC_WORKER_CLAIM_BATCH_SIZE",
    "DEFAULT_SEMANTIC_WORKER_LEASE_SECONDS",
    "DEFAULT_SEMANTIC_WORKER_MAX_CONCURRENCY",
    "PROJECT_OVERRIDE_PATH",
    "SemanticAnalyzerConfig",
    "SemanticConfigError",
    "SemanticConfigValidationError",
    "SemanticExecutionPolicy",
    "SemanticAutomationPolicy",
    "SemanticInputPolicy",
    "SemanticJobProfile",
    "apply_project_ai_routing",
    "load_semantic_enrichment_config",
]
