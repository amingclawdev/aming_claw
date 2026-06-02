"""Observer runtime launcher contracts.

The observer launcher is intentionally thin: it converts route/backlog context
into a provider-neutral AI invocation request. ServiceManager or future manager
HTTP endpoints can call the same functions without depending on click.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

try:
    from ai_invocation import (
        AIInvocationRequest,
        AIInvocationResult,
        RoutePromptContract,
        invoke_ai,
    )
    from governance.mf_subagent_contract import (
        MfSubagentContractError,
        validate_mf_subagent_dispatch_gate,
    )
except ImportError:  # pragma: no cover - package import path
    from agent.ai_invocation import (
        BACKEND_CLAUDE_CLI,
        BACKEND_CODEX_CLI,
        BACKEND_DOCKER_LIVE_AI,
        AIInvocationRequest,
        AIInvocationResult,
        RoutePromptContract,
        invoke_ai,
    )
    from agent.governance.mf_subagent_contract import (
        MfSubagentContractError,
        validate_mf_subagent_dispatch_gate,
    )
else:  # pragma: no cover - direct module import path
    from ai_invocation import BACKEND_CLAUDE_CLI, BACKEND_CODEX_CLI, BACKEND_DOCKER_LIVE_AI


OBSERVER_RUN_SCHEMA_VERSION = "observer_run.v1"
ONE_HOP_EXECUTION_GATE_SCHEMA_VERSION = "observer_one_hop_execution_gate.v1"
ONE_HOP_REQUIRED_BACKENDS = {
    BACKEND_CODEX_CLI,
    BACKEND_CLAUDE_CLI,
    BACKEND_DOCKER_LIVE_AI,
}


@dataclass
class ObserverRunRequest:
    project_id: str
    backlog_id: str
    route: RoutePromptContract
    provider: str = "openai"
    model: str = ""
    backend_mode: str = "codex_cli"
    workspace: str = ""
    prompt: str = ""
    timeout_sec: int = 120
    dispatch_gate: Mapping[str, Any] = field(default_factory=dict)
    main_worktree: str = ""

    @classmethod
    def from_route_token(
        cls,
        *,
        project_id: str,
        backlog_id: str,
        route_token: Mapping[str, Any],
        provider: str = "openai",
        model: str = "",
        backend_mode: str = "codex_cli",
        workspace: str = "",
        prompt: str = "",
        timeout_sec: int = 120,
    ) -> "ObserverRunRequest":
        return cls(
            project_id=project_id,
            backlog_id=backlog_id,
            route=RoutePromptContract.from_mapping({"route_token": route_token}),
            provider=provider,
            model=model,
            backend_mode=backend_mode,
            workspace=workspace,
            prompt=prompt,
            timeout_sec=timeout_sec,
        )


def validate_observer_run_request(request: ObserverRunRequest) -> list[str]:
    missing: list[str] = []
    if not request.project_id:
        missing.append("project_id")
    if not request.backlog_id:
        missing.append("backlog_id")
    if not request.route.route_context_hash:
        missing.append("route_context_hash")
    if not request.route.prompt_contract_id:
        missing.append("prompt_contract_id")
    if not request.provider:
        missing.append("provider")
    if not request.backend_mode:
        missing.append("backend_mode")
    return missing


def _normalize_path(path: str) -> str:
    token = str(path or "").strip()
    if not token:
        return ""
    return str(Path(token).expanduser().resolve())


def _execution_gate_required(request: ObserverRunRequest) -> bool:
    return request.backend_mode in ONE_HOP_REQUIRED_BACKENDS


def validate_one_hop_execution_gate(request: ObserverRunRequest) -> dict[str, Any]:
    """Validate that a live observer/worker run is fenced to one hop.

    The lower-level MF gate already knows how to prove the isolated branch,
    worktree, fence token, merge queue, route context, and dirty-scope evidence.
    This observer gate adds the launcher-specific check that the invocation cwd
    matches the gated worktree.
    """

    if not _execution_gate_required(request):
        return {
            "schema_version": ONE_HOP_EXECUTION_GATE_SCHEMA_VERSION,
            "required": False,
            "allowed": True,
            "reason": "backend_does_not_launch_code_mutating_cli",
        }

    if not request.dispatch_gate:
        return {
            "schema_version": ONE_HOP_EXECUTION_GATE_SCHEMA_VERSION,
            "required": True,
            "allowed": False,
            "missing": ["dispatch_gate"],
            "error": "live observer execution requires one-hop dispatch gate evidence",
        }

    try:
        gate = validate_mf_subagent_dispatch_gate(
            request.dispatch_gate,
            target_worktree_path=request.main_worktree,
            main_worktree_path=request.main_worktree,
        )
    except MfSubagentContractError as exc:
        return {
            "schema_version": ONE_HOP_EXECUTION_GATE_SCHEMA_VERSION,
            "required": True,
            "allowed": False,
            "missing": [],
            "error": str(exc),
        }

    workspace = _normalize_path(request.workspace or str(Path.cwd()))
    gated_worktree = _normalize_path(str(gate.get("worktree") or ""))
    if workspace != gated_worktree:
        return {
            "schema_version": ONE_HOP_EXECUTION_GATE_SCHEMA_VERSION,
            "required": True,
            "allowed": False,
            "missing": [],
            "error": "observer workspace must match gated one-hop worktree",
            "workspace": workspace,
            "gated_worktree": gated_worktree,
            "dispatch_gate": gate,
        }

    return {
        "schema_version": ONE_HOP_EXECUTION_GATE_SCHEMA_VERSION,
        "required": True,
        "allowed": True,
        "dispatch_gate": gate,
    }


def build_observer_prompt(request: ObserverRunRequest) -> str:
    if request.prompt:
        return request.prompt
    return (
        "You are the Aming Claw observer for a route-owned manual-fix run.\n"
        f"Project: {request.project_id}\n"
        f"Backlog: {request.backlog_id}\n"
        f"Route context hash: {request.route.route_context_hash}\n"
        f"Prompt contract id: {request.route.prompt_contract_id}\n\n"
        "Required order: acknowledge route context, query graph before file edits, "
        "execute only in the gated one-hop worktree, dispatch only bounded mf_sub "
        "workers through dispatch gates, record timeline "
        "evidence, and stop before merge/close unless verification gates pass. "
        "Do not expose raw private Judgment Brain context."
    )


def build_observer_invocation_request(request: ObserverRunRequest) -> AIInvocationRequest:
    workspace = request.workspace or str(Path.cwd())
    return AIInvocationRequest(
        role="observer",
        provider=request.provider,
        model=request.model,
        backend_mode=request.backend_mode,
        cwd=workspace,
        prompt=build_observer_prompt(request),
        timeout_sec=request.timeout_sec,
        auth_mode="cli_auth" if request.backend_mode.endswith("_cli") else "api_key_env",
        route=request.route,
        metadata={
            "project_id": request.project_id,
            "backlog_id": request.backlog_id,
            "observer_launcher": True,
        },
    )


def run_observer(request: ObserverRunRequest, *, execute: bool = False) -> dict[str, Any]:
    missing = validate_observer_run_request(request)
    invocation_request = build_observer_invocation_request(request)
    if missing:
        return {
            "ok": False,
            "schema_version": OBSERVER_RUN_SCHEMA_VERSION,
            "status": "rejected",
            "missing": missing,
            "execute": execute,
            "invocation_request": invocation_request.to_evidence(),
        }

    if execute:
        execution_gate = validate_one_hop_execution_gate(request)
        if not execution_gate.get("allowed"):
            return {
                "ok": False,
                "schema_version": OBSERVER_RUN_SCHEMA_VERSION,
                "status": "rejected",
                "project_id": request.project_id,
                "backlog_id": request.backlog_id,
                "execute": execute,
                "missing": execution_gate.get("missing") or [],
                "one_hop_execution_gate": execution_gate,
                "invocation_request": invocation_request.to_evidence(),
            }
        result = invoke_ai(invocation_request)
    else:
        execution_gate = {
            "schema_version": ONE_HOP_EXECUTION_GATE_SCHEMA_VERSION,
            "required": _execution_gate_required(request),
            "allowed": True,
            "status": "deferred_until_execute",
        }
        result = AIInvocationResult(
            request=invocation_request,
            status="planned",
            command=[request.backend_mode, "dry-run"],
            returncode=0,
            provider_backed=request.backend_mode != "fixture",
            calls_models=False,
            auth_status="not_invoked",
        )
    evidence = result.to_evidence()
    return {
        "ok": result.status in {"planned", "completed"},
        "schema_version": OBSERVER_RUN_SCHEMA_VERSION,
        "status": result.status,
        "project_id": request.project_id,
        "backlog_id": request.backlog_id,
        "execute": execute,
        "one_hop_execution_gate": execution_gate,
        "invocation": evidence,
    }
