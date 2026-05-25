"""Backend-neutral contract for MF subagent branch workers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent.governance.asset_binding_proposals import (
    PRECHECK_SCHEMA_VERSION as ASSET_BINDING_PRECHECK_SCHEMA_VERSION,
    PROPOSAL_SCHEMA_VERSION as ASSET_BINDING_PROPOSAL_SCHEMA_VERSION,
)
from agent.governance.parallel_branch_runtime import BranchTaskRuntimeContext


MF_SUB_ROLE = "mf_sub"
INPUT_SCHEMA_VERSION = "mf_subagent_input.v1"
RESULT_SCHEMA_VERSION = "mf_subagent_result.v1"
FINISH_GATE_SCHEMA_VERSION = "mf_subagent_finish_gate.v1"
DISPATCH_GATE_SCHEMA_VERSION = "mf_subagent_dispatch_gate.v1"
FINISH_GATE_REPLAY_SOURCE = "mf_sub_finish_gate"
BACKEND_CONTRACT = "parallel_branch_worker.v1"
DISPATCH_DEFAULT = "non_blocking_after_gate"
WORKTREE_POLICY_MODE = "isolated_worktree_required"

MF_SUB_ALLOWED_CAPABILITIES = (
    "modify_code",
    "run_tests",
    "git_diff",
    "checkpoint_branch_task",
    "report_blocker",
)
MF_SUB_FORBIDDEN_ACTIONS = (
    "merge",
    "push",
    "activate_graph",
    "release_gate",
    "create_task",
    "delete_worktree",
    "modify_merge_queue",
)
MF_SUB_REQUIRED_OUTPUT = (
    "status",
    "changed_files",
    "test_results",
    "checkpoint_id",
    "fence_token",
)

_REQUIRED_CONTEXT_FIELDS = (
    "project_id",
    "task_id",
    "backlog_id",
    "branch_ref",
    "worktree_path",
    "base_commit",
    "target_head_commit",
    "merge_queue_id",
    "fence_token",
)
_READY_STATUSES = {"completed", "succeeded", "ready_for_merge"}
_PASS_STATUSES = {"pass", "passed", "ok", "succeeded", "success", "clean"}
_FORBIDDEN_RESULT_FLAGS = {
    "merge_commit": "merge",
    "push_performed": "push",
    "graph_activated": "activate_graph",
    "release_gate_passed": "release_gate",
    "task_created": "create_task",
    "worktree_deleted": "delete_worktree",
}
_FINISH_IDENTITY_FIELDS = (
    "project_id",
    "task_id",
    "backlog_id",
    "branch_ref",
    "worktree_path",
    "base_commit",
    "target_head_commit",
    "merge_queue_id",
)
_DISPATCH_REQUIRED_FIELDS = (
    "branch",
    "worktree",
    "base_commit",
    "target_head_commit",
    "merge_queue_id",
    "fence_token",
)


class MfSubagentContractError(ValueError):
    """Raised when an MF subagent payload violates the worker contract."""


def _string(value: Any) -> str:
    return str(value or "").strip()


def _string_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        raise MfSubagentContractError(f"{field_name} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise MfSubagentContractError(f"{field_name} must be a list of strings")
        if item:
            result.append(item)
    return result


def _mapping(value: Any, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MfSubagentContractError(f"{field_name} must be a mapping")
    return dict(value)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _normalize_worktree_path(path: str) -> str:
    token = _string(path)
    if not token:
        return ""
    return str(Path(token).expanduser().resolve())


def _nested_mapping(payload: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return dict(value) if isinstance(value, Mapping) else {}


def _dispatch_string(
    payload: Mapping[str, Any],
    *,
    names: Sequence[str],
    nested_keys: Sequence[tuple[str, Sequence[str]]] = (),
) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, Mapping):
            for nested_name in (
                "branch_ref",
                "ref_name",
                "name",
                "path",
                "worktree_path",
            ):
                token = _string(value.get(nested_name))
                if token:
                    return token
        token = _string(value)
        if token:
            return token
    for parent_key, child_names in nested_keys:
        nested = _nested_mapping(payload, parent_key)
        for child_name in child_names:
            token = _string(nested.get(child_name))
            if token:
                return token
    return ""


def _dirty_scope_evidence(value: Any) -> dict[str, Any]:
    check = _mapping(value, field_name="dirty_scope_check")
    status = _string(check.get("status") or check.get("result")).lower()
    passed = _bool(check.get("passed")) or status in _PASS_STATUSES
    exact_match = _bool(
        check.get("dirty_scope_exact_match")
        or check.get("exact_match")
        or check.get("owned_scope_only")
    )
    evidence_fields = [
        "changed_files",
        "dirty_files",
        "owned_files",
        "checked_paths",
        "allowed_dirty_files",
    ]
    has_file_evidence = any(key in check for key in evidence_fields)
    if not passed or not has_file_evidence:
        raise MfSubagentContractError(
            "dirty_scope_check must include passing dirty-scope evidence"
        )
    return {
        "status": status or "passed",
        "passed": True,
        "dirty_scope_exact_match": exact_match,
        "changed_file_count": len(
            _string_list(check.get("changed_files"), field_name="changed_files")
        ),
        "dirty_file_count": len(
            _string_list(check.get("dirty_files"), field_name="dirty_files")
        ),
    }


def _override_reason(payload: Mapping[str, Any], override: Mapping[str, Any]) -> str:
    for key in (
        "same_worktree_reason",
        "operator_reason",
        "explicit_operator_reason",
        "reason",
    ):
        token = _string(payload.get(key))
        if token:
            return token
    for key in ("operator_reason", "explicit_operator_reason", "reason"):
        token = _string(override.get(key))
        if token:
            return token
    return ""


def _timeline_evidence_present(
    payload: Mapping[str, Any],
    override: Mapping[str, Any],
) -> bool:
    if _bool(payload.get("timeline_event_recorded")) or _bool(
        override.get("timeline_event_recorded")
    ):
        return True
    for key in (
        "timeline_evidence",
        "observer_timeline_event",
        "dispatch_timeline_evidence",
    ):
        value = payload.get(key) if key in payload else override.get(key)
        if isinstance(value, Mapping) and (
            _string(value.get("event_id"))
            or _string(value.get("event_type"))
            or _string(value.get("recorded_at"))
        ):
            return True
    return False


def validate_mf_subagent_dispatch_gate(
    payload: Mapping[str, Any],
    *,
    target_worktree_path: str = "",
    main_worktree_path: str = "",
) -> dict[str, Any]:
    """Validate local MF subagent dispatch evidence before handoff.

    The gate is intentionally local and backend-neutral: observers and AI
    self-checks can run it before spawning a bounded `mf_sub` worker.
    """

    if not isinstance(payload, Mapping):
        raise MfSubagentContractError("MF subagent dispatch payload must be a mapping")
    payload = dict(payload)
    branch = _dispatch_string(
        payload,
        names=("branch", "branch_ref", "ref_name"),
        nested_keys=(("branch_context", ("branch_ref", "ref_name")),),
    )
    worktree = _dispatch_string(
        payload,
        names=("worktree", "worktree_path"),
        nested_keys=(("branch", ("worktree_path", "path")),),
    )
    base_commit = _dispatch_string(
        payload,
        names=("base_commit",),
        nested_keys=(("branch", ("base_commit",)),),
    )
    target_head_commit = _dispatch_string(
        payload,
        names=("target_head_commit",),
        nested_keys=(("branch", ("target_head_commit", "head_commit")),),
    )
    merge_queue_id = _dispatch_string(
        payload,
        names=("merge_queue_id",),
        nested_keys=(
            ("branch_context", ("merge_queue_id",)),
            ("graph_identity", ("merge_queue_id",)),
            ("branch", ("merge_queue_id",)),
        ),
    )
    fence_token = _dispatch_string(payload, names=("fence_token",))
    values = {
        "branch": branch,
        "worktree": worktree,
        "base_commit": base_commit,
        "target_head_commit": target_head_commit,
        "merge_queue_id": merge_queue_id,
        "fence_token": fence_token,
    }
    missing = [field for field in _DISPATCH_REQUIRED_FIELDS if not values[field]]
    if missing:
        raise MfSubagentContractError(
            "MF subagent dispatch missing required fields: " + ", ".join(missing)
        )

    owned_files = _string_list(
        payload.get("owned_files") or payload.get("write_scope"),
        field_name="owned_files",
    )
    if not owned_files:
        raise MfSubagentContractError("MF subagent dispatch missing owned_files fence")
    dirty_scope = _dirty_scope_evidence(payload.get("dirty_scope_check"))

    policy = _nested_mapping(payload, "worktree_policy")
    override = _nested_mapping(payload, "same_worktree_override")
    if not override:
        override = _nested_mapping(payload, "override_policy")
    same_worktree_allowed = _bool(
        payload.get("same_worktree_allowed")
        or policy.get("same_worktree_allowed")
        or override.get("same_worktree_allowed")
    )
    normalized_worktree = _normalize_worktree_path(worktree)
    target_paths = [
        _normalize_worktree_path(path)
        for path in (
            target_worktree_path,
            main_worktree_path,
            _string(payload.get("target_worktree_path")),
            _string(payload.get("main_worktree_path")),
            _string(policy.get("target_worktree_path")),
            _string(policy.get("main_worktree_path")),
        )
        if _string(path)
    ]
    target_role = _string(
        payload.get("worktree_role") or policy.get("worktree_role")
    ).lower()
    same_worktree = normalized_worktree in target_paths or target_role in {
        "target",
        "main",
    }
    if same_worktree and not same_worktree_allowed:
        raise MfSubagentContractError(
            "same-worktree dispatch is blocked by default for local mf_sub workers"
        )

    override_used = same_worktree and same_worktree_allowed
    override_reason = ""
    if override_used:
        override_reason = _override_reason(payload, override)
        if not override_reason:
            raise MfSubagentContractError(
                "same-worktree dispatch override requires explicit operator reason"
            )
        if not dirty_scope["dirty_scope_exact_match"]:
            raise MfSubagentContractError(
                "same-worktree dispatch override requires dirty_scope_exact_match evidence"
            )
        if not _timeline_evidence_present(payload, override):
            raise MfSubagentContractError(
                "same-worktree dispatch override requires observer timeline evidence"
            )

    return {
        "schema_version": DISPATCH_GATE_SCHEMA_VERSION,
        "role": MF_SUB_ROLE,
        "dispatch_default": DISPATCH_DEFAULT,
        "worktree_policy": WORKTREE_POLICY_MODE,
        "branch": branch,
        "worktree": worktree,
        "base_commit": base_commit,
        "target_head_commit": target_head_commit,
        "merge_queue_id": merge_queue_id,
        "fence_token": fence_token,
        "owned_files": owned_files,
        "isolated_worktree": not same_worktree,
        "same_worktree_allowed": same_worktree_allowed,
        "override": {
            "used": override_used,
            "reason": override_reason,
            "timeline_evidence_recorded": override_used,
        },
        "dirty_scope_check": dirty_scope,
    }


def _require_context(context: BranchTaskRuntimeContext) -> None:
    missing = [field for field in _REQUIRED_CONTEXT_FIELDS if not getattr(context, field)]
    if missing:
        raise MfSubagentContractError(
            f"MF subagent context missing required fields: {', '.join(missing)}"
        )


def build_mf_subagent_input(
    context: BranchTaskRuntimeContext,
    *,
    prompt: str,
    acceptance_criteria: Sequence[str] | None = None,
    target_files: Sequence[str] | None = None,
    test_commands: Sequence[str] | None = None,
    operator_notes: str = "",
    backend: str = "codex_subagent",
) -> dict[str, Any]:
    """Build the stable input payload for a branch-isolated MF subagent."""

    _require_context(context)
    return {
        "schema_version": INPUT_SCHEMA_VERSION,
        "role": MF_SUB_ROLE,
        "backend": backend,
        "backend_contract": BACKEND_CONTRACT,
        "project_id": context.project_id,
        "task_id": context.task_id,
        "batch_id": context.batch_id,
        "backlog_id": context.backlog_id,
        "branch": {
            "branch_ref": context.branch_ref,
            "ref_name": context.ref_name,
            "worktree_id": context.worktree_id,
            "worktree_path": context.worktree_path,
            "base_commit": context.base_commit,
            "head_commit": context.head_commit,
            "target_head_commit": context.target_head_commit,
        },
        "runtime_identity": {
            "agent_id": context.agent_id,
            "worker_id": context.worker_id,
            "attempt": context.attempt,
            "lease_id": context.lease_id,
            "fence_token": context.fence_token,
            "checkpoint_id": context.checkpoint_id,
            "depends_on": list(context.depends_on),
        },
        "chain_identity": {
            "chain_id": context.chain_id,
            "root_task_id": context.root_task_id,
            "stage_task_id": context.stage_task_id,
            "stage_type": context.stage_type,
            "retry_round": context.retry_round,
        },
        "graph_identity": {
            "snapshot_id": context.snapshot_id,
            "projection_id": context.projection_id,
            "merge_queue_id": context.merge_queue_id,
            "merge_preview_id": context.merge_preview_id,
            "rollback_epoch": context.rollback_epoch,
            "replay_epoch": context.replay_epoch,
        },
        "work": {
            "prompt": prompt,
            "acceptance_criteria": _string_list(
                acceptance_criteria, field_name="acceptance_criteria"
            ),
            "target_files": _string_list(target_files, field_name="target_files"),
            "test_commands": _string_list(test_commands, field_name="test_commands"),
            "operator_notes": operator_notes,
        },
        "capabilities": {
            "can": list(MF_SUB_ALLOWED_CAPABILITIES),
            "cannot": list(MF_SUB_FORBIDDEN_ACTIONS),
        },
        "prechecks": {
            "asset_binding_proposal": {
                "proposal_schema_version": ASSET_BINDING_PROPOSAL_SCHEMA_VERSION,
                "precheck_schema_version": ASSET_BINDING_PRECHECK_SCHEMA_VERSION,
                "local_function": (
                    "agent.governance.asset_binding_proposals."
                    "precheck_asset_binding_proposal"
                ),
                "gate_rule": (
                    "Run the same precheck on any doc/test/config binding proposal "
                    "before submitting it; include the compact self_precheck object "
                    "with the proposal so the server gate can verify the hash."
                ),
            },
        },
        "required_output": list(MF_SUB_REQUIRED_OUTPUT),
    }


def normalize_mf_subagent_result(
    payload: Mapping[str, Any],
    *,
    expected_fence_token: str,
) -> dict[str, Any]:
    """Validate and normalize a branch worker result before queueing merge review."""

    if not expected_fence_token:
        raise MfSubagentContractError("expected_fence_token is required")
    missing = [field for field in MF_SUB_REQUIRED_OUTPUT if field not in payload]
    if missing:
        raise MfSubagentContractError(
            f"MF subagent result missing required fields: {', '.join(missing)}"
        )

    fence_token = str(payload.get("fence_token") or "")
    if fence_token != expected_fence_token:
        raise MfSubagentContractError("MF subagent result fence token is stale")

    actions = {action.lower() for action in _string_list(payload.get("actions"), field_name="actions")}
    for field_name, action in _FORBIDDEN_RESULT_FLAGS.items():
        if payload.get(field_name):
            actions.add(action)
    forbidden = sorted(actions.intersection(MF_SUB_FORBIDDEN_ACTIONS))
    if forbidden:
        raise MfSubagentContractError(
            f"MF subagent result attempted forbidden actions: {', '.join(forbidden)}"
        )

    status = str(payload.get("status") or "")
    changed_files = _string_list(payload.get("changed_files"), field_name="changed_files")
    new_files = _string_list(payload.get("new_files"), field_name="new_files")
    blockers = _string_list(payload.get("blockers"), field_name="blockers")
    test_results = _mapping(payload.get("test_results"), field_name="test_results")
    test_status = str(test_results.get("status") or "").lower()
    tests_passed = bool(test_results.get("passed")) or test_status in _PASS_STATUSES
    merge_queue_ready = status in _READY_STATUSES and tests_passed and not blockers

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "role": MF_SUB_ROLE,
        "status": status,
        "changed_files": changed_files,
        "new_files": new_files,
        "test_results": test_results,
        "checkpoint_id": str(payload.get("checkpoint_id") or ""),
        "fence_token": fence_token,
        "merge_queue_ready": merge_queue_ready,
        "blockers": blockers,
        "summary": str(payload.get("summary") or ""),
        "evidence": _mapping(payload.get("evidence"), field_name="evidence"),
    }


def validate_mf_subagent_finish_gate(
    payload: Mapping[str, Any],
    *,
    context: BranchTaskRuntimeContext,
) -> dict[str, Any]:
    """Validate a subagent finish claim against durable branch runtime facts.

    The subagent payload is a claim. This function only returns evidence that
    matches the current runtime context and is ready to become a checkpoint.
    """

    _require_context(context)
    normalized = normalize_mf_subagent_result(
        payload,
        expected_fence_token=context.fence_token,
    )
    if not normalized["merge_queue_ready"]:
        raise MfSubagentContractError("MF subagent finish gate is not merge-queue ready")

    identity_mismatches: list[str] = []
    for field in _FINISH_IDENTITY_FIELDS:
        claimed = str(payload.get(field) or "")
        expected = str(getattr(context, field) or "")
        if claimed and expected and claimed != expected:
            identity_mismatches.append(field)
    if identity_mismatches:
        raise MfSubagentContractError(
            "MF subagent finish gate identity mismatch: "
            + ", ".join(sorted(identity_mismatches))
        )

    claimed_head = str(payload.get("head_commit") or payload.get("branch_head") or "")
    checkpoint_id = str(normalized.get("checkpoint_id") or "").strip()
    if not checkpoint_id:
        raise MfSubagentContractError("checkpoint_id is required")

    return {
        "schema_version": FINISH_GATE_SCHEMA_VERSION,
        "role": MF_SUB_ROLE,
        "project_id": context.project_id,
        "task_id": context.task_id,
        "backlog_id": context.backlog_id,
        "branch_ref": context.branch_ref,
        "worktree_path": context.worktree_path,
        "base_commit": context.base_commit,
        "target_head_commit": context.target_head_commit,
        "merge_queue_id": context.merge_queue_id,
        "head_commit": claimed_head or context.head_commit,
        "checkpoint_id": checkpoint_id,
        "fence_token": context.fence_token,
        "replay_source": FINISH_GATE_REPLAY_SOURCE,
        "changed_files": normalized["changed_files"],
        "new_files": normalized["new_files"],
        "test_results": normalized["test_results"],
        "blockers": normalized["blockers"],
        "summary": normalized["summary"],
        "evidence": normalized["evidence"],
        "merge_queue_ready": True,
    }
