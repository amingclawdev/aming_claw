"""Provider-neutral AI invocation contracts and adapters.

This module keeps prompt routing, backend selection, and audit evidence in one
place so observer/subagent runtime code can use CLI and API-key providers
without duplicating evidence policy.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


REQUEST_SCHEMA_VERSION = "ai_invocation_request.v1"
RESULT_SCHEMA_VERSION = "ai_invocation_result.v1"
DOCKER_LIVE_OBSERVER_ROUTE_SCHEMA_VERSION = "docker_live_observer_route_evidence.v1"

BACKEND_CODEX_CLI = "codex_cli"
BACKEND_CLAUDE_CLI = "claude_cli"
BACKEND_OPENAI_API = "openai_api"
BACKEND_ANTHROPIC_API = "anthropic_api"
BACKEND_FIXTURE = "fixture"
BACKEND_DOCKER_LIVE_AI = "docker_live_ai"

_SENSITIVE_KEY_RE = re.compile(r"(api[_-]?key|token|secret|password|credential)", re.I)
_SECRET_VALUE_RE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{8,})")


def sha256_text(value: str) -> str:
    return "sha256:" + hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _string(value: Any) -> str:
    return str(value or "").strip()


def _nested(mapping: Mapping[str, Any], *keys: str) -> Mapping[str, Any]:
    cur: Any = mapping
    for key in keys:
        if not isinstance(cur, Mapping):
            return {}
        cur = cur.get(key)
    return cur if isinstance(cur, Mapping) else {}


def _first_string(mapping: Mapping[str, Any], names: tuple[str, ...]) -> str:
    for name in names:
        value = _string(mapping.get(name))
        if value:
            return value
    return ""


def redact_text(value: str, *, max_chars: int = 4000) -> str:
    text = _SECRET_VALUE_RE.sub("[REDACTED]", value or "")
    return text[:max_chars]


def redact_command(command: list[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for part in command:
        if redact_next:
            redacted.append("[REDACTED]")
            redact_next = False
            continue
        lowered = part.lower()
        if _SENSITIVE_KEY_RE.search(lowered):
            redacted.append(part)
            if "=" not in part:
                redact_next = True
            continue
        redacted.append(redact_text(part, max_chars=300))
    return redacted


def safe_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    source = env or os.environ
    return {
        key: ("[REDACTED]" if _SENSITIVE_KEY_RE.search(key) else value)
        for key, value in source.items()
    }


@dataclass
class RoutePromptContract:
    route_context_hash: str = ""
    prompt_contract_id: str = ""
    prompt_contract_hash: str = ""
    route_token_ref: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "RoutePromptContract":
        data = payload if isinstance(payload, Mapping) else {}
        route_context = _nested(data, "route_context")
        route_prompt_contract = _nested(data, "route_prompt_contract")
        prompt_contract = _nested(data, "prompt_contract")
        route_token = _nested(data, "route_token")
        return cls(
            route_context_hash=(
                _first_string(data, ("route_context_hash",))
                or _first_string(route_context, ("route_context_hash",))
                or _first_string(route_prompt_contract, ("route_context_hash",))
                or _first_string(prompt_contract, ("route_context_hash",))
                or _first_string(route_token, ("route_context_hash",))
            ),
            prompt_contract_id=(
                _first_string(data, ("prompt_contract_id",))
                or _first_string(route_context, ("prompt_contract_id",))
                or _first_string(route_prompt_contract, ("prompt_contract_id", "id"))
                or _first_string(prompt_contract, ("prompt_contract_id", "id"))
                or _first_string(route_token, ("prompt_contract_id",))
            ),
            prompt_contract_hash=(
                _first_string(data, ("prompt_contract_hash",))
                or _first_string(route_context, ("prompt_contract_hash",))
                or _first_string(route_prompt_contract, ("prompt_contract_hash",))
                or _first_string(prompt_contract, ("prompt_contract_hash",))
                or _first_string(route_token, ("prompt_contract_hash",))
            ),
            route_token_ref=(
                _first_string(data, ("route_token_ref", "route_token_id", "token_id"))
                or _first_string(route_token, ("token_id", "route_token_id"))
            ),
        )

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "route_context_hash": self.route_context_hash,
            "prompt_contract_id": self.prompt_contract_id,
            "prompt_contract_hash": self.prompt_contract_hash,
            "route_token_ref": self.route_token_ref,
            "raw_context_exposed": False,
        }


@dataclass
class AIInvocationRequest:
    role: str
    provider: str
    model: str = ""
    backend_mode: str = ""
    cwd: str = ""
    prompt: str = ""
    system_prompt: str = ""
    timeout_sec: int = 120
    output_path: str = ""
    auth_mode: str = ""
    output_policy: str = "hash_and_summary_only"
    route: RoutePromptContract = field(default_factory=RoutePromptContract)
    metadata: dict[str, Any] = field(default_factory=dict)

    def resolved_backend(self) -> str:
        if self.backend_mode:
            return self.backend_mode
        provider = self.provider.lower()
        if provider == "openai":
            return BACKEND_CODEX_CLI
        if provider == "anthropic":
            return BACKEND_CLAUDE_CLI
        return BACKEND_FIXTURE

    def prompt_text(self) -> str:
        if self.system_prompt:
            return (
                "=== SYSTEM PROMPT START ===\n"
                f"{self.system_prompt}\n"
                "=== SYSTEM PROMPT END ===\n\n"
                "=== TASK PROMPT START ===\n"
                f"{self.prompt}\n"
                "=== TASK PROMPT END ===\n"
            )
        return self.prompt

    def to_evidence(self) -> dict[str, Any]:
        return {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "backend_mode": self.resolved_backend(),
            "cwd": self.cwd,
            "timeout_sec": self.timeout_sec,
            "auth_mode": self.auth_mode,
            "output_policy": self.output_policy,
            "prompt_sha256": sha256_text(self.prompt_text()),
            "route_prompt_contract": self.route.as_dict(),
            "raw_prompt_exposed": False,
        }


@dataclass
class AIInvocationResult:
    request: AIInvocationRequest
    status: str
    output_text: str = ""
    error: str = ""
    command: list[str] = field(default_factory=list)
    returncode: int = 0
    elapsed_ms: int = 0
    provider_backed: bool = False
    calls_models: bool = False
    raw_output_stored: bool = False
    auth_status: str = "unknown"
    output_path: str = ""

    @property
    def output_sha256(self) -> str:
        return sha256_text(self.output_text)

    @property
    def prompt_sha256(self) -> str:
        return sha256_text(self.request.prompt_text())

    def to_evidence(self) -> dict[str, Any]:
        route = self.request.route.as_dict()
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "request_schema_version": REQUEST_SCHEMA_VERSION,
            "status": self.status,
            "role": self.request.role,
            "provider": self.request.provider,
            "model": self.request.model,
            "backend_mode": self.request.resolved_backend(),
            "auth_mode": self.request.auth_mode,
            "auth_status": self.auth_status,
            "provider_backed": self.provider_backed,
            "calls_models": self.calls_models,
            "returncode": self.returncode,
            "elapsed_ms": self.elapsed_ms,
            "command": redact_command(self.command),
            "route_prompt_contract": route,
            "route_alert_ack": {
                "status": "acknowledged" if route.get("route_context_hash") else "not_applicable",
                "route_context_hash": route.get("route_context_hash", ""),
                "prompt_contract_id": route.get("prompt_contract_id", ""),
                "prompt_contract_hash": route.get("prompt_contract_hash", ""),
            },
            "ordered_step_outputs": [
                {"step_id": "01_invocation_contract", "status": "passed"},
                {"step_id": "02_provider_backend", "status": "passed" if self.command or self.status != "failed" else "failed"},
                {"step_id": "03_sanitized_evidence", "status": "passed"},
            ],
            "prompt_sha256": self.prompt_sha256,
            "output_sha256": self.output_sha256,
            "raw_output_stored": self.raw_output_stored,
            "no_raw_prompt_output": True,
            "error": redact_text(self.error, max_chars=1000),
            "output_path": self.output_path,
        }


def build_codex_exec_command(
    *,
    model: str = "",
    cwd: str,
    output_path: str = "",
    dangerous: bool | None = None,
    sandbox: str = "workspace-write",
    ephemeral: bool = False,
) -> list[str]:
    codex_bin = os.getenv("CODEX_BIN", "").strip()
    if not codex_bin:
        codex_bin = "codex.cmd" if os.name == "nt" else "codex"
    use_dangerous = (
        os.getenv("CODEX_DANGEROUS", "1").strip().lower() not in {"0", "false", "no"}
        if dangerous is None
        else dangerous
    )
    cmd = [codex_bin, "exec"]
    if model:
        cmd.extend(["--model", model])
    if use_dangerous:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        cmd.extend(["--sandbox", sandbox])
    cmd.append("--skip-git-repo-check")
    if ephemeral:
        cmd.append("--ephemeral")
    cmd.extend(["-C", cwd, "-o"])
    if output_path:
        cmd.append(output_path)
    return cmd


def build_claude_code_command(
    *,
    model: str = "",
    cwd: str = "",
    prompt_file: str = "",
    allowed_tools: str = "",
    max_turns: str = "",
) -> list[str]:
    claude_bin = os.getenv("CLAUDE_BIN", "claude")
    cmd = [claude_bin, "-p"]
    if prompt_file:
        cmd.extend(["--system-prompt-file", prompt_file])
    if model:
        cmd.extend(["--model", model])
    if cwd:
        cmd.extend(["--add-dir", cwd])
    if allowed_tools:
        cmd.extend(["--allowedTools", allowed_tools])
    if max_turns:
        cmd.extend(["--max-turns", max_turns])
    return cmd


def _failed_result(
    request: AIInvocationRequest,
    *,
    error: str,
    command: list[str],
    elapsed_ms: int,
    auth_status: str = "unknown",
) -> AIInvocationResult:
    return AIInvocationResult(
        request=request,
        status="failed",
        error=error,
        command=command,
        returncode=1,
        elapsed_ms=elapsed_ms,
        provider_backed=request.resolved_backend() != BACKEND_FIXTURE,
        calls_models=False,
        auth_status=auth_status,
    )


def invoke_fixture(request: AIInvocationRequest) -> AIInvocationResult:
    output = '{"ok":true,"provider":"%s","backend":"fixture"}' % (request.provider or "fixture")
    return AIInvocationResult(
        request=request,
        status="completed",
        output_text=output,
        command=["fixture", request.provider or "fixture"],
        returncode=0,
        provider_backed=False,
        calls_models=False,
        auth_status="not_required",
    )


def invoke_api(request: AIInvocationRequest) -> AIInvocationResult:
    backend = request.resolved_backend()
    provider = "openai" if backend == BACKEND_OPENAI_API else "anthropic"
    model = request.model or ("gpt-4o" if provider == "openai" else "claude-sonnet-4-6")
    command = ["api", provider, model]
    started = time.perf_counter()
    prompt = request.prompt_text()
    try:
        if provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            if not api_key:
                return _failed_result(
                    request,
                    error="OPENAI_API_KEY not set",
                    command=command,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    auth_status="missing_api_key",
                )
            import requests as _req

            resp = _req.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": int(request.metadata.get("max_tokens") or 4096),
                },
                timeout=request.timeout_sec,
            )
            if resp.status_code >= 400:
                return _failed_result(
                    request,
                    error=_api_error("OpenAI", resp),
                    command=command,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    auth_status="api_error",
                )
            output = resp.json()["choices"][0]["message"]["content"]
        else:
            api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
            if not api_key:
                return _failed_result(
                    request,
                    error="ANTHROPIC_API_KEY not set",
                    command=command,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    auth_status="missing_api_key",
                )
            import requests as _req

            resp = _req.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": int(request.metadata.get("max_tokens") or 8192),
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=request.timeout_sec,
            )
            if resp.status_code >= 400:
                return _failed_result(
                    request,
                    error=_api_error("Anthropic", resp),
                    command=command,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    auth_status="api_error",
                )
            output = resp.json()["content"][0]["text"]
        return AIInvocationResult(
            request=request,
            status="completed",
            output_text=output,
            command=command,
            returncode=0,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            provider_backed=True,
            calls_models=True,
            auth_status="api_key_env",
        )
    except Exception as exc:
        return _failed_result(
            request,
            error=str(exc),
            command=command,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            auth_status="failed",
        )


def _api_error(provider: str, resp: Any) -> str:
    try:
        body = resp.json()
        err = body.get("error", {})
        if isinstance(err, Mapping):
            message = err.get("message") or err.get("type") or ""
        else:
            message = str(err)
    except Exception:
        message = getattr(resp, "text", "")[:500]
    return f"{provider} API error (HTTP {resp.status_code}): {message or 'unknown error'}"


def invoke_cli(request: AIInvocationRequest) -> AIInvocationResult:
    backend = request.resolved_backend()
    started = time.perf_counter()
    cwd = request.cwd or os.getcwd()
    output_path = request.output_path
    temp_dir = ""
    prompt = request.prompt_text()
    try:
        if not output_path and backend == BACKEND_CODEX_CLI:
            temp_dir = tempfile.mkdtemp(prefix="aming-claw-ai-invocation-")
            output_path = str(Path(temp_dir) / "last-message.txt")
        if backend == BACKEND_CODEX_CLI:
            command = build_codex_exec_command(model=request.model, cwd=cwd, output_path=output_path)
            result = subprocess.run(
                command,
                input=prompt,
                text=True,
                cwd=cwd,
                capture_output=True,
                timeout=request.timeout_sec,
                check=False,
            )
            output_text = ""
            if output_path:
                try:
                    output_text = Path(output_path).read_text(encoding="utf-8")
                except OSError:
                    output_text = result.stdout
            return AIInvocationResult(
                request=request,
                status="completed" if result.returncode == 0 else "failed",
                output_text=output_text,
                error=result.stderr if result.returncode else "",
                command=command,
                returncode=result.returncode,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                provider_backed=True,
                calls_models=result.returncode == 0,
                auth_status="cli_auth_unknown" if result.returncode == 0 else "cli_failed",
                output_path=output_path,
            )
        command = build_claude_code_command(model=request.model, cwd=cwd)
        result = subprocess.run(
            command,
            input=prompt,
            text=True,
            cwd=cwd,
            capture_output=True,
            timeout=request.timeout_sec,
            check=False,
        )
        return AIInvocationResult(
            request=request,
            status="completed" if result.returncode == 0 else "failed",
            output_text=result.stdout,
            error=result.stderr if result.returncode else "",
            command=command,
            returncode=result.returncode,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            provider_backed=True,
            calls_models=result.returncode == 0,
            auth_status="cli_auth_unknown" if result.returncode == 0 else "cli_failed",
        )
    except Exception as exc:
        return _failed_result(
            request,
            error=str(exc),
            command=[],
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            auth_status="failed",
        )
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def invoke_ai(request: AIInvocationRequest) -> AIInvocationResult:
    backend = request.resolved_backend()
    if backend == BACKEND_FIXTURE:
        return invoke_fixture(request)
    if backend in {BACKEND_OPENAI_API, BACKEND_ANTHROPIC_API}:
        return invoke_api(request)
    if backend in {BACKEND_CODEX_CLI, BACKEND_CLAUDE_CLI}:
        return invoke_cli(request)
    if backend == BACKEND_DOCKER_LIVE_AI:
        return _failed_result(
            request,
            error="docker_live_ai backend is a governed external harness; use docker/hn-install-audit/run-install-audit.sh",
            command=["docker_live_ai"],
            elapsed_ms=0,
            auth_status="external_harness_required",
        )
    return _failed_result(
        request,
        error=f"unsupported AI invocation backend: {backend}",
        command=[],
        elapsed_ms=0,
        auth_status="unsupported_backend",
    )
