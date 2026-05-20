"""Service-side AI caller for reconcile semantic enrichment."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import subprocess
import time
from pathlib import Path
from typing import Any

from .reconcile_semantic_config import SemanticAnalyzerConfig


def _normalize_provider(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value in {"codex", "openai", "gpt"}:
        return "openai"
    if value in {"claude", "anthropic", "opus"}:
        return "anthropic"
    return value


def _infer_provider(model: str, provider: str = "") -> str:
    p = _normalize_provider(provider)
    if p in {"openai", "anthropic"}:
        return p
    m = (model or "").strip().lower()
    if m.startswith(("gpt-", "o1", "o3", "o4", "gpt-5")):
        return "openai"
    if m.startswith("claude"):
        return "anthropic"
    return ""


def _normalize_model_id(provider: str, model: str) -> str:
    value = (model or "").strip()
    if not value:
        return value
    if _normalize_provider(provider) == "anthropic":
        key = re.sub(r"[\s_.]+", "-", value.lower())
        if key in {
            "opus4-7",
            "opus-4-7",
            "claude-opus4-7",
            "claude-opus-4-7",
        }:
            return "claude-opus-4-7"
    return value


def _resolve_cli_binary(
    env_var: str,
    configured: str,
    candidates: list[str],
    fallback: str,
) -> tuple[str, str]:
    configured = (configured or "").strip()
    if configured:
        return configured, "config"
    configured = os.getenv(env_var, "").strip()
    if configured:
        return configured, "env"
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved, "path"
    return fallback, "fallback"


def _extract_json_dict(text: str) -> dict[str, Any] | None:
    if not text or not text.strip():
        return None
    raw = text.strip()
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return None
        inner_result = parsed.get("result")
        if isinstance(inner_result, str) and inner_result.strip():
            inner = _extract_json_dict(inner_result)
            if inner:
                inner["_ai_cli_result"] = {
                    key: value for key, value in parsed.items()
                    if key != "result"
                }
                return inner
        return parsed
    except json.JSONDecodeError:
        pass
    blocks = re.findall(r"```(?:json)?\s*\n(.*?)\n```", raw, re.DOTALL)
    for block in reversed(blocks):
        try:
            parsed = json.loads(block.strip())
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    for end in range(len(raw) - 1, -1, -1):
        if raw[end] != "}":
            continue
        depth = 0
        for start in range(end, -1, -1):
            if raw[start] == "}":
                depth += 1
            elif raw[start] == "{":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(raw[start:end + 1])
                    except json.JSONDecodeError:
                        break
                    return parsed if isinstance(parsed, dict) else None
        break
    return None


def _semantic_error_message(parsed: dict[str, Any], config: SemanticAnalyzerConfig) -> str:
    """Return an error message when the model returned an error-only JSON object."""
    error = parsed.get("error") or parsed.get("message")
    if not error and parsed.get("is_error") is True:
        error = parsed.get("result") or "semantic AI returned error result"
    if not error:
        return ""
    required = config.output_schema.get("required") if isinstance(config.output_schema, dict) else []
    semantic_keys = {
        "feature_name",
        "semantic_summary",
        "intent",
        "domain_label",
        *(str(item) for item in (required or []) if str(item)),
    }
    has_semantic_payload = any(parsed.get(key) not in (None, "", [], {}) for key in semantic_keys)
    return "" if has_semantic_payload else str(error)


def resolve_semantic_ai_route(config: SemanticAnalyzerConfig) -> dict[str, str]:
    """Resolve provider/model for semantic AI from config/env/pipeline."""
    chain_role = config.chain_role or config.role or "pm"
    base_meta = {
        "analyzer_role": config.analyzer_role,
        "chain_role": chain_role,
    }
    env_provider = os.getenv("RECONCILE_SEMANTIC_AI_PROVIDER", "").strip()
    env_model = os.getenv("RECONCILE_SEMANTIC_AI_MODEL", "").strip()
    if env_provider or env_model:
        model = env_model or config.model
        provider = _infer_provider(model, env_provider or config.provider)
        model = _normalize_model_id(provider, model)
        return {"provider": provider, "model": model, "source": "env", **base_meta}

    provider = _normalize_provider(config.provider)
    model = _normalize_model_id(provider, (config.model or "").strip())
    if provider in {"", "injected", "none", "off"}:
        return {"provider": "", "model": model, "source": "disabled", **base_meta}
    if provider in {"pipeline", "role"}:
        agent_dir = Path(__file__).resolve().parents[1]
        if str(agent_dir) not in sys.path:
            sys.path.insert(0, str(agent_dir))
        try:
            from pipeline_config import get_effective_pipeline_config, resolve_role_config  # type: ignore
        except Exception:
            get_effective_pipeline_config = None  # type: ignore
            resolve_role_config = None  # type: ignore
        if get_effective_pipeline_config and resolve_role_config:
            try:
                pipeline = get_effective_pipeline_config()
                resolved = resolve_role_config(chain_role, pipeline)
                model = model or (resolved.get("model") or "")
                provider = _infer_provider(model, resolved.get("provider") or "")
                model = _normalize_model_id(provider, model)
            except Exception:
                provider = ""
        if not model:
            try:
                from config import get_claude_model, get_model_provider
                model = (get_claude_model() or "").strip()
                provider = _infer_provider(model, get_model_provider())
                model = _normalize_model_id(provider, model)
            except Exception:
                pass
        return {"provider": provider, "model": model, "source": "pipeline", **base_meta}
    return {"provider": _infer_provider(model, provider), "model": model, "source": "config", **base_meta}


def build_semantic_ai_call(
    *,
    semantic_config: SemanticAnalyzerConfig,
    project_id: str,
    snapshot_id: str,
    project_root: str | Path | None = None,
) -> Any:
    """Return an ai_call(stage, payload) callable, or None when not configured."""
    route = resolve_semantic_ai_route(semantic_config)
    provider = route.get("provider", "")
    model = route.get("model", "")
    if provider not in {"openai", "anthropic"}:
        return None

    root = Path(project_root or os.getenv("CODEX_WORKSPACE", os.getcwd())).resolve()
    log_dir = root / "shared-volume" / "codex-tasks" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    def git_changed() -> set[str]:
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except Exception:
            return set()
        changed = set()
        for line in (proc.stdout or "").splitlines():
            if len(line) > 3:
                changed.add(line[3:].strip().replace("\\", "/"))
        return changed

    def call(stage: str, payload: dict[str, Any]) -> dict[str, Any]:
        stage_profile = semantic_config.job_profile(stage)
        stage_route = dict(route)
        if stage_profile.provider or stage_profile.model:
            stage_model = stage_profile.model or model
            stage_provider = _infer_provider(stage_model, stage_profile.provider or provider)
            stage_route.update(
                {
                    "provider": stage_provider,
                    "model": _normalize_model_id(stage_provider, stage_model),
                    "source": f"{route.get('source', 'config')}+job_profile",
                }
            )
        stage_route["analyzer_role"] = stage_profile.analyzer_role or semantic_config.analyzer_role
        stage_route["chain_role"] = semantic_config.chain_role or semantic_config.role or "pm"
        stage_job_type = semantic_config.to_instruction_payload(stage).get("job_type", "node")
        stage_route["job_type"] = stage_job_type
        call_provider = stage_route.get("provider", "")
        call_model = stage_route.get("model", "")
        if call_provider not in {"openai", "anthropic"}:
            raise RuntimeError(f"semantic AI provider is not configured for stage {stage!r}")
        batch_hint = ""
        if isinstance(payload.get("features"), list):
            batch_hint = (
                "The payload is a batch. Return exactly one JSON object with a "
                "'features' array. Include one object for every input "
                "feature.node_id, and include node_id on each output object. "
            )
        prompt_template = stage_profile.prompt_template or semantic_config.prompt_template
        if stage_job_type == "graph_structure":
            output_instruction = (
                "Return exactly one JSON object matching payload.output_contract. "
                "The top-level schema_version must be graph_structure_ops.v1. "
                "Only use supported operations listed in the supplied payload. "
            )
        elif stage_job_type == "graph_enrich_config":
            output_instruction = (
                "Return exactly one JSON object matching payload.output_contract. "
                "The top-level schema_version must be graph_enrich_config_ops.v1. "
                "Only use supported operations listed in the supplied payload. "
            )
        else:
            self_check_instruction = (
                "Before final output, self-precheck the JSON contract. Include "
                "self_check with required=true, valid=true or false, status, "
                "checked_rules, checked_rules_count, repair_attempts, "
                "max_repair_attempts, and known_risks. Required rules are "
                "required_fields_present, source_payload_only, no_project_mutation, "
                "review_feedback_accounted_for, and graph_suggestions_contract_checked. "
                "For batch mode, include self_check on every features item. "
            )
            output_instruction = (
                "Return exactly one JSON object matching the requested semantic fields. "
                f"{batch_hint}{self_check_instruction}"
            )
        graph_context_instruction = (
            "When graph_query_audit or graph_query_context is present in the payload, "
            "treat it as the authoritative audited graph evidence for this task. "
            "When semantic_evidence is present, cite and reason from its evidence_items "
            "instead of assuming facts from filenames or unaudited context. "
            "Do not invent graph facts beyond the supplied payload.\n"
        )
        prompt = (
            f"{prompt_template}\n\n"
            f"{output_instruction}"
            f"{graph_context_instruction}"
            "Do not modify files, create tasks, or inspect project files outside the supplied payload.\n\n"
            "Payload:\n"
            f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
        )
        node_id = str(payload.get("feature", {}).get("node_id") or "feature")
        attempt_tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{snapshot_id}-{node_id}-{stage}")
        timeout_sec = int(os.getenv("RECONCILE_SEMANTIC_AI_TIMEOUT_SEC", os.getenv("CODEX_TIMEOUT_SEC", "900")))
        before = git_changed()
        t0 = time.perf_counter()
        if call_provider == "openai":
            codex_bin, executable_source = _resolve_cli_binary(
                "CODEX_BIN",
                semantic_config.executables.get("openai", ""),
                ["codex", "codex.cmd", "codex.ps1"] if os.name == "nt" else ["codex"],
                "codex",
            )
            output_last = log_dir / f"reconcile-semantic-{attempt_tag}.last_message.txt"
            prompt_file = log_dir / f"reconcile-semantic-{attempt_tag}.prompt.md"
            prompt_file.write_text(prompt, encoding="utf-8")
            cmd = [
                codex_bin,
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
                "-C",
                str(root),
                "-o",
                str(output_last),
            ]
            if call_model:
                cmd.extend(["--model", call_model])
            cmd.append("-")
            proc = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=timeout_sec,
                check=False,
            )
            raw = output_last.read_text(encoding="utf-8") if output_last.exists() else (proc.stdout or "")
            stderr = proc.stderr or ""
        else:
            claude_bin, executable_source = _resolve_cli_binary(
                "CLAUDE_BIN",
                semantic_config.executables.get("anthropic", ""),
                ["claude", "claude.exe", "claude.cmd"] if os.name == "nt" else ["claude"],
                "claude",
            )
            cmd = [claude_bin, "-p", "--output-format", "json"]
            if call_model:
                cmd.extend(["--model", call_model])
            if os.getenv("CLAUDE_DANGEROUS", "1").strip().lower() not in {"0", "false", "no"}:
                cmd.append("--dangerously-skip-permissions")
            env = {
                k: v for k, v in os.environ.items()
                if k not in {
                    "CLAUDECODE",
                    "CLAUDE_CODE_ENTRYPOINT",
                    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST",
                    "CLAUDE_CODE_EXECPATH",
                    "CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH",
                    "CLAUDE_CODE_EMIT_TOOL_USE_SUMMARIES",
                    "CLAUDE_CODE_ENABLE_ASK_USER_QUESTION_TOOL",
                    "CLAUDE_CODE_OAUTH_TOKEN",
                    "ANTHROPIC_API_KEY",
                }
            }
            proc = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=timeout_sec,
                check=False,
                cwd=str(root),
                env=env,
            )
            raw = proc.stdout or ""
            stderr = proc.stderr or ""
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        after = git_changed()
        new_changes = sorted(after - before)
        if new_changes:
            raise RuntimeError(
                "semantic AI attempted project mutation: "
                + ", ".join(new_changes)
            )
        if int(proc.returncode or 0) != 0:
            raise RuntimeError(str(stderr or raw or "semantic AI failed"))
        parsed = _extract_json_dict(raw)
        if not parsed:
            raise RuntimeError("semantic AI returned no JSON object")
        error_message = _semantic_error_message(parsed, semantic_config)
        if error_message:
            raise RuntimeError(f"semantic AI returned error response: {error_message}")
        parsed["_ai_route"] = {
            **stage_route,
            "executable": str(cmd[0]) if cmd else "",
            "executable_source": executable_source,
        }
        parsed["_ai_elapsed_ms"] = elapsed_ms
        return parsed

    return call


__all__ = [
    "build_semantic_ai_call",
    "resolve_semantic_ai_route",
]
