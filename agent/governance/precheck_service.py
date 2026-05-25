"""Unified local precheck gates for contract-driven MF workflow stages."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Callable


PRECHECK_RESULT_SCHEMA_VERSION = "mf_workflow_precheck_result.v1"

ALLOW = "allow"
REVIEW_REQUIRED = "review_required"
BLOCK = "block"

GATE_KINDS = (
    "mf_subagent.dispatch",
    "mf_subagent.handoff",
    "workflow.merge",
    "workflow.merge_queue_entry",
    "workflow.merge_preview",
    "workflow.live_merge",
    "workflow.reconcile_policy",
    "backlog.close",
)

PASS_STATUSES = {"accepted", "allow", "ok", "pass", "passed", "succeeded", "success"}
GIT_EVIDENCE_IGNORED_PATH_LIMIT = 50


class PrecheckServiceError(ValueError):
    """Raised when the precheck request itself cannot be evaluated."""


def run_precheck(
    kind: str,
    contract_id: str,
    stage: str,
    subject: Mapping[str, Any],
    actor: str,
) -> dict[str, Any]:
    """Run one registered local gate and return the common result contract."""

    subject_dict = _mapping(subject)
    kind_s = _text(kind)
    stage_s = _text(stage)
    contract_id_s = _text(contract_id)
    if kind_s not in _GATE_REGISTRY:
        return _result(
            kind=kind_s,
            contract_id=contract_id_s,
            stage=stage_s,
            decision=BLOCK,
            subject=subject_dict,
            evidence={
                "schema_version": PRECHECK_RESULT_SCHEMA_VERSION,
                "actor": _text(actor),
                "errors": ["unknown_gate_kind"],
                "supported_gate_kinds": list(GATE_KINDS),
            },
        )

    evidence = _GATE_REGISTRY[kind_s](
        contract_id_s,
        stage_s,
        subject_dict,
        _text(actor),
    )
    return _result(
        kind=kind_s,
        contract_id=contract_id_s,
        stage=stage_s,
        decision=_decision_from_evidence(evidence),
        subject=subject_dict,
        evidence=evidence,
    )


def _dispatch_gate(
    contract_id: str,
    stage: str,
    subject: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    errors.extend(_contract_errors(subject, contract_id, stage, "mf_subagent.dispatch"))

    worker_path = _path(subject, "worker_worktree", "worktree")
    target_path = _path(subject, "target_worktree", "main_worktree")
    worker_git = _git_evidence(worker_path)
    target_git = _git_evidence(target_path)
    owned_files = _string_list(subject.get("owned_files") or subject.get("write_scope"))
    branch_ref = _first_text(subject, "branch_ref", "branch")
    merge_queue_id = _first_text(subject, "merge_queue_id")
    base_commit = _text(subject.get("base_commit"))
    target_head_commit = _text(subject.get("target_head_commit"))
    graph_snapshot_commit = _text(
        subject.get("graph_snapshot_commit") or subject.get("active_graph_commit")
    )
    adoption_mode = _text(
        subject.get("branch_adoption_mode") or subject.get("adoption_mode")
    ).lower()

    if not owned_files:
        errors.append("missing_write_scope")
    if not branch_ref:
        errors.append("missing_branch_ref")
    if not worker_path:
        errors.append("missing_worktree_path")
    if not merge_queue_id:
        errors.append("missing_merge_queue_id")
    if not _text(subject.get("fence_token")):
        errors.append("missing_fence_token")
    if worker_git["error"]:
        errors.append("worker_git_unavailable")
    if target_git["error"]:
        errors.append("target_git_unavailable")
    if worker_git["dirty"]:
        errors.append("dirty_worker_worktree")
    if target_git["dirty"]:
        errors.append("dirty_target_main_worktree")
    if worker_git["root"] and target_git["root"] and worker_git["root"] == target_git["root"]:
        errors.append("same_worktree_non_isolated_worker")
    if base_commit and worker_git["head"] and worker_git["head"] != base_commit:
        errors.append("worker_head_mismatch")
    if target_head_commit and target_git["head"] and target_git["head"] != target_head_commit:
        errors.append("target_head_mismatch")
    if not base_commit:
        errors.append("missing_base_commit")
    if not target_head_commit:
        errors.append("missing_target_head_commit")
    if bool(subject.get("active_graph_stale")) or bool(subject.get("graph_stale")):
        errors.append("active_graph_stale_at_dispatch")
    if (
        graph_snapshot_commit
        and target_head_commit
        and graph_snapshot_commit != target_head_commit
    ):
        errors.append("graph_snapshot_target_head_mismatch")
    if adoption_mode in {"existing_branch", "adopt_existing_branch"} and not _has_pass_evidence(
        subject.get("branch_adoption_evidence")
    ):
        errors.append("missing_existing_branch_adoption_evidence")

    return {
        "schema_version": PRECHECK_RESULT_SCHEMA_VERSION,
        "actor": actor,
        "gate_kind": "mf_subagent.dispatch",
        "stage": stage,
        "errors": _dedupe(errors),
        "warnings": _dedupe(warnings),
        "worker_git": worker_git,
        "target_git": target_git,
        "owned_files": owned_files,
        "branch_ref": branch_ref,
        "worktree_path": worker_path,
        "merge_queue_id": merge_queue_id,
        "base_commit": base_commit,
        "target_head_commit": target_head_commit,
        "current_target_head": _text(target_git.get("head")),
        "graph_snapshot_commit": graph_snapshot_commit,
        "graph_current": not (
            bool(subject.get("active_graph_stale"))
            or bool(subject.get("graph_stale"))
            or (
                graph_snapshot_commit
                and target_head_commit
                and graph_snapshot_commit != target_head_commit
            )
        ),
        "branch_adoption_mode": adoption_mode,
        "fence_token_present": bool(_text(subject.get("fence_token"))),
    }


def _handoff_gate(
    contract_id: str,
    stage: str,
    subject: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    errors.extend(_contract_errors(subject, contract_id, stage, "mf_subagent.handoff"))

    worktree_path = _path(subject, "worker_worktree", "source_worktree", "worktree")
    git = _git_evidence(worktree_path)
    owned = set(_string_list(subject.get("owned_files") or subject.get("write_scope")))
    forbidden = _string_list(subject.get("forbidden_paths"))
    dirty = set(git["dirty_files"])
    ignored = set(git["ignored_files"])
    all_observed = dirty | ignored
    forbidden_hits = sorted(path for path in all_observed if _matches_any(path, forbidden))

    if git["error"]:
        errors.append("worker_git_unavailable")
    if dirty and not owned:
        errors.append("missing_write_scope")
    if dirty and not dirty.issubset(owned):
        errors.append("dirty_scope_outside_owned_files")
    if forbidden_hits:
        errors.append("forbidden_path_changes")
    if not _has_pass_evidence(subject.get("tests_evidence")):
        errors.append("missing_tests_evidence")
    if not _has_timeline_kind(subject.get("timeline_evidence"), {"implementation", "verification"}):
        errors.append("missing_timeline_evidence")

    return {
        "schema_version": PRECHECK_RESULT_SCHEMA_VERSION,
        "actor": actor,
        "gate_kind": "mf_subagent.handoff",
        "stage": stage,
        "errors": _dedupe(errors),
        "warnings": _dedupe(warnings),
        "worker_git": git,
        "owned_files": sorted(owned),
        "dirty_scope_exact_match": bool(not dirty or dirty.issubset(owned)),
        "forbidden_paths": forbidden,
        "forbidden_path_hits": forbidden_hits,
        "tests_evidence_present": _has_pass_evidence(subject.get("tests_evidence")),
        "timeline_evidence_present": _has_timeline_kind(
            subject.get("timeline_evidence"),
            {"implementation", "verification"},
        ),
    }


def _merge_gate(
    contract_id: str,
    stage: str,
    subject: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    errors.extend(_contract_errors(subject, contract_id, stage, "workflow.merge"))

    main_git = _git_evidence(_path(subject, "main_worktree", "target_worktree"))
    source_git = _git_evidence(_path(subject, "source_worktree", "worker_worktree", "worktree"))
    subject_source_commit = _text(subject.get("source_commit"))
    observed_source_head = _text(source_git.get("head"))
    source_commit = observed_source_head or subject_source_commit
    errors.extend(_token_errors(subject, current_commit=source_commit))
    missing_evidence = _missing_required_evidence(subject, include_close_ready=False)

    if main_git["error"]:
        errors.append("main_git_unavailable")
    if source_git["error"]:
        errors.append("source_git_unavailable")
    if main_git["dirty"]:
        errors.append("dirty_main_worktree")
    if source_git["dirty"]:
        errors.append("source_candidate_uncommitted")
    if not source_commit:
        errors.append("missing_source_commit")
    if (
        subject_source_commit
        and observed_source_head
        and subject_source_commit != observed_source_head
    ):
        errors.append("source_commit_head_mismatch")
    if missing_evidence:
        errors.append("contract_evidence_incomplete")
    if not _has_timeline_kind(subject.get("timeline_evidence"), {"implementation", "verification"}):
        errors.append("missing_implementation_or_verification_timeline")

    return {
        "schema_version": PRECHECK_RESULT_SCHEMA_VERSION,
        "actor": actor,
        "gate_kind": "workflow.merge",
        "stage": stage,
        "errors": _dedupe(errors),
        "warnings": _dedupe(warnings),
        "main_git": main_git,
        "source_git": source_git,
        "source_commit": source_commit,
        "subject_source_commit": subject_source_commit,
        "observed_source_head": observed_source_head,
        "missing_required_evidence": missing_evidence,
        "timeline_evidence_present": _has_timeline_kind(
            subject.get("timeline_evidence"),
            {"implementation", "verification"},
        ),
    }


def _merge_queue_entry_gate(
    contract_id: str,
    stage: str,
    subject: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    errors.extend(_contract_errors(subject, contract_id, stage, "workflow.merge_queue_entry"))

    main_git = _git_evidence(_path(subject, "main_worktree", "target_worktree"))
    source_git = _git_evidence(_path(subject, "source_worktree", "worker_worktree", "worktree"))
    subject_source_commit = _text(subject.get("source_commit"))
    observed_source_head = _text(source_git.get("head"))
    source_commit = observed_source_head or subject_source_commit
    merge_queue_id = _first_text(subject, "merge_queue_id")
    branch_ref = _first_text(subject, "branch_ref", "branch")
    expected_target_head = _text(subject.get("target_head_commit"))
    observed_target_head = _text(main_git.get("head"))
    errors.extend(_token_errors(subject, current_commit=source_commit))

    if main_git["error"]:
        errors.append("main_git_unavailable")
    if source_git["error"]:
        errors.append("source_git_unavailable")
    if main_git["dirty"]:
        errors.append("dirty_main_worktree")
    if source_git["dirty"]:
        errors.append("source_candidate_uncommitted")
    if not merge_queue_id:
        errors.append("missing_merge_queue_id")
    if not branch_ref:
        errors.append("missing_branch_ref")
    if not source_commit:
        errors.append("missing_source_commit")
    if (
        subject_source_commit
        and observed_source_head
        and subject_source_commit != observed_source_head
    ):
        errors.append("source_commit_head_mismatch")
    if expected_target_head and observed_target_head and expected_target_head != observed_target_head:
        errors.append("merge_queue_target_head_stale")
    if not _has_timeline_kind(subject.get("timeline_evidence"), {"implementation", "verification"}):
        errors.append("missing_implementation_or_verification_timeline")

    return {
        "schema_version": PRECHECK_RESULT_SCHEMA_VERSION,
        "actor": actor,
        "gate_kind": "workflow.merge_queue_entry",
        "stage": stage,
        "errors": _dedupe(errors),
        "warnings": _dedupe(warnings),
        "merge_queue_id": merge_queue_id,
        "branch_ref": branch_ref,
        "main_git": main_git,
        "source_git": source_git,
        "source_commit": source_commit,
        "subject_source_commit": subject_source_commit,
        "observed_source_head": observed_source_head,
        "target_head_commit": expected_target_head,
        "observed_target_head": observed_target_head,
        "timeline_evidence_present": _has_timeline_kind(
            subject.get("timeline_evidence"),
            {"implementation", "verification"},
        ),
    }


def _merge_preview_gate(
    contract_id: str,
    stage: str,
    subject: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    errors.extend(_contract_errors(subject, contract_id, stage, "workflow.merge_preview"))

    main_git = _git_evidence(_path(subject, "main_worktree", "target_worktree"))
    source_git = _git_evidence(_path(subject, "source_worktree", "worker_worktree", "worktree"))
    subject_source_commit = _text(subject.get("source_commit"))
    observed_source_head = _text(source_git.get("head"))
    source_commit = observed_source_head or subject_source_commit
    merge_queue_id = _first_text(subject, "merge_queue_id")
    merge_preview_id = _first_text(subject, "merge_preview_id")
    expected_target_head = _text(subject.get("target_head_commit"))
    observed_target_head = _text(main_git.get("head"))
    preview_passed = _has_pass_evidence(subject.get("merge_preview_evidence"))
    errors.extend(_token_errors(subject, current_commit=source_commit))

    if main_git["error"]:
        errors.append("main_git_unavailable")
    if source_git["error"]:
        errors.append("source_git_unavailable")
    if main_git["dirty"]:
        errors.append("dirty_main_worktree")
    if source_git["dirty"]:
        errors.append("source_candidate_uncommitted")
    if not merge_queue_id:
        errors.append("missing_merge_queue_id")
    if not merge_preview_id:
        errors.append("missing_merge_preview_id")
    if not source_commit:
        errors.append("missing_source_commit")
    if (
        subject_source_commit
        and observed_source_head
        and subject_source_commit != observed_source_head
    ):
        errors.append("source_commit_head_mismatch")
    if expected_target_head and observed_target_head and expected_target_head != observed_target_head:
        errors.append("merge_preview_target_head_stale")
    if not preview_passed:
        errors.append("missing_merge_preview_evidence")

    return {
        "schema_version": PRECHECK_RESULT_SCHEMA_VERSION,
        "actor": actor,
        "gate_kind": "workflow.merge_preview",
        "stage": stage,
        "errors": _dedupe(errors),
        "warnings": _dedupe(warnings),
        "merge_queue_id": merge_queue_id,
        "merge_preview_id": merge_preview_id,
        "main_git": main_git,
        "source_git": source_git,
        "source_commit": source_commit,
        "subject_source_commit": subject_source_commit,
        "observed_source_head": observed_source_head,
        "target_head_commit": expected_target_head,
        "observed_target_head": observed_target_head,
        "merge_preview_evidence_present": preview_passed,
    }


def _live_merge_gate(
    contract_id: str,
    stage: str,
    subject: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    errors.extend(_contract_errors(subject, contract_id, stage, "workflow.live_merge"))

    main_git = _git_evidence(_path(subject, "main_worktree", "target_worktree"))
    source_git = _git_evidence(_path(subject, "source_worktree", "worker_worktree", "worktree"))
    subject_source_commit = _text(subject.get("source_commit"))
    observed_source_head = _text(source_git.get("head"))
    source_commit = observed_source_head or subject_source_commit
    merge_commit = _text(subject.get("merge_commit"))
    target_head_before = _text(subject.get("target_head_before_merge") or subject.get("target_head_commit"))
    target_head_after = _text(subject.get("target_head_after_merge"))
    observed_target_head = _text(main_git.get("head"))
    errors.extend(_token_errors(subject, current_commit=source_commit))

    if main_git["error"]:
        errors.append("main_git_unavailable")
    if source_git["error"]:
        errors.append("source_git_unavailable")
    if main_git["dirty"]:
        errors.append("dirty_main_worktree")
    if source_git["dirty"]:
        errors.append("source_candidate_uncommitted")
    if not _first_text(subject, "merge_queue_id"):
        errors.append("missing_merge_queue_id")
    if not source_commit:
        errors.append("missing_source_commit")
    if (
        subject_source_commit
        and observed_source_head
        and subject_source_commit != observed_source_head
    ):
        errors.append("source_commit_head_mismatch")
    if not merge_commit:
        errors.append("missing_merge_commit")
    if target_head_after and merge_commit and target_head_after != merge_commit:
        errors.append("target_head_after_merge_not_merge_commit")
    if observed_target_head and merge_commit and observed_target_head != merge_commit:
        errors.append("target_head_after_merge_mismatch")
    if target_head_before and target_head_after and target_head_before == target_head_after:
        errors.append("merge_did_not_advance_target_head")

    return {
        "schema_version": PRECHECK_RESULT_SCHEMA_VERSION,
        "actor": actor,
        "gate_kind": "workflow.live_merge",
        "stage": stage,
        "errors": _dedupe(errors),
        "warnings": _dedupe(warnings),
        "merge_queue_id": _first_text(subject, "merge_queue_id"),
        "main_git": main_git,
        "source_git": source_git,
        "source_commit": source_commit,
        "subject_source_commit": subject_source_commit,
        "observed_source_head": observed_source_head,
        "target_head_before_merge": target_head_before,
        "target_head_after_merge": target_head_after,
        "observed_target_head": observed_target_head,
        "merge_commit": merge_commit,
    }


def _reconcile_policy_gate(
    contract_id: str,
    stage: str,
    subject: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    errors.extend(_contract_errors(subject, contract_id, stage, "workflow.reconcile_policy"))
    current_commit = _text(subject.get("merge_commit") or subject.get("source_commit"))
    errors.extend(_token_errors(subject, current_commit=current_commit))

    changed_files = _string_list(subject.get("changed_files"))
    if any(_is_runtime_or_operator_path(path) for path in changed_files):
        if _text(subject.get("e2e_decision")) not in {"e2e_current", "e2e_added"}:
            errors.append("runtime_api_dashboard_change_requires_e2e_or_review")
        else:
            warnings.append("runtime_api_dashboard_reconcile_requires_observer_review")
    if bool(subject.get("graph_rule_changed")) or bool(subject.get("rule_fingerprint_changed")):
        warnings.append("graph_rule_change_requires_full_reconcile_review")
    if _text(subject.get("scope_kind")) not in {"", "docs", "code_module", "tests", "config"}:
        warnings.append("unknown_reconcile_scope")

    return {
        "schema_version": PRECHECK_RESULT_SCHEMA_VERSION,
        "actor": actor,
        "gate_kind": "workflow.reconcile_policy",
        "stage": stage,
        "errors": _dedupe(errors),
        "warnings": _dedupe(warnings),
        "changed_files": changed_files,
        "scope_kind": _text(subject.get("scope_kind")),
        "e2e_decision": _text(subject.get("e2e_decision")),
        "graph_rule_changed": bool(subject.get("graph_rule_changed")),
    }


def _close_gate(
    contract_id: str,
    stage: str,
    subject: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    errors.extend(_contract_errors(subject, contract_id, stage, "backlog.close"))
    current_commit = _text(subject.get("merge_commit") or subject.get("source_commit"))
    errors.extend(_token_errors(subject, current_commit=current_commit))
    missing_evidence = _missing_required_evidence(subject, include_close_ready=True)

    if not _text(subject.get("merge_commit")):
        errors.append("missing_merge_commit")
    if not _has_timeline_kind(subject.get("timeline_evidence"), {"close_ready"}):
        errors.append("missing_close_ready_timeline")
    if not _has_timeline_kind(
        subject.get("timeline_evidence"),
        {"implementation", "verification", "close_ready"},
    ):
        errors.append("mf_timeline_precheck_incomplete")
    if missing_evidence:
        errors.append("required_evidence_ids_missing")

    return {
        "schema_version": PRECHECK_RESULT_SCHEMA_VERSION,
        "actor": actor,
        "gate_kind": "backlog.close",
        "stage": stage,
        "errors": _dedupe(errors),
        "warnings": _dedupe(warnings),
        "merge_commit": _text(subject.get("merge_commit")),
        "missing_required_evidence": missing_evidence,
        "close_ready_present": _has_timeline_kind(subject.get("timeline_evidence"), {"close_ready"}),
        "mf_timeline_precheck_compatible": _has_timeline_kind(
            subject.get("timeline_evidence"),
            {"implementation", "verification", "close_ready"},
        ),
    }


_Gate = Callable[[str, str, dict[str, Any], str], dict[str, Any]]
_GATE_REGISTRY: dict[str, _Gate] = {
    "mf_subagent.dispatch": _dispatch_gate,
    "mf_subagent.handoff": _handoff_gate,
    "workflow.merge": _merge_gate,
    "workflow.merge_queue_entry": _merge_queue_entry_gate,
    "workflow.merge_preview": _merge_preview_gate,
    "workflow.live_merge": _live_merge_gate,
    "workflow.reconcile_policy": _reconcile_policy_gate,
    "backlog.close": _close_gate,
}


def _result(
    *,
    kind: str,
    contract_id: str,
    stage: str,
    decision: str,
    subject: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    evidence_hash = _hash(evidence)
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "precheck_run_id": f"precheck-{_slug(kind)}-{evidence_hash[:12]}",
        "kind": kind,
        "contract_id": contract_id,
        "stage": stage,
        "decision": decision,
        "status": _status_for_decision(decision),
        "subject": subject,
        "evidence": evidence,
        "evidence_hash": f"sha256:{evidence_hash}",
        "created_at": created_at,
    }


def _decision_from_evidence(evidence: Mapping[str, Any]) -> str:
    if evidence.get("errors"):
        return BLOCK
    if evidence.get("warnings"):
        return REVIEW_REQUIRED
    return ALLOW


def _status_for_decision(decision: str) -> str:
    if decision == ALLOW:
        return "passed"
    if decision == REVIEW_REQUIRED:
        return "warning"
    return "failed"


def _git_evidence(path: str) -> dict[str, Any]:
    if not path:
        return {
            "path": "",
            "root": "",
            "head": "",
            "dirty": True,
            "dirty_files": [],
            "tracked_dirty_files": [],
            "untracked_files": [],
            "ignored_files": [],
            "ignored_files_omitted_count": 0,
            "ignored_truncated": False,
            "untracked_count": 0,
            "ignored_count": 0,
            "error": "missing_path",
        }
    root = _git(path, "rev-parse", "--show-toplevel")
    head = _git(path, "rev-parse", "HEAD")
    status = _git(path, "status", "--porcelain=v1", "-uall", "--ignored")
    error = ""
    if root is None or head is None or status is None:
        error = "not_a_git_worktree"
    rows = _parse_status(status or "")
    dirty_files = sorted(
        item["path"] for item in rows if item["kind"] in {"tracked", "untracked"}
    )
    tracked_dirty = sorted(item["path"] for item in rows if item["kind"] == "tracked")
    untracked = sorted(item["path"] for item in rows if item["kind"] == "untracked")
    ignored = sorted(item["path"] for item in rows if item["kind"] == "ignored")
    ignored_sample = ignored[:GIT_EVIDENCE_IGNORED_PATH_LIMIT]
    ignored_omitted = max(0, len(ignored) - len(ignored_sample))
    return {
        "path": str(Path(path).expanduser()),
        "root": _text(root),
        "head": _text(head),
        "dirty": bool(dirty_files) or bool(error),
        "dirty_files": dirty_files,
        "tracked_dirty_files": tracked_dirty,
        "untracked_files": untracked,
        "ignored_files": ignored_sample,
        "ignored_files_omitted_count": ignored_omitted,
        "ignored_truncated": ignored_omitted > 0,
        "ignored_path_limit": GIT_EVIDENCE_IGNORED_PATH_LIMIT,
        "untracked_count": len(untracked),
        "ignored_count": len(ignored),
        "error": error,
    }


def _git(path: str, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", path, *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError:
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _parse_status(output: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for raw in output.splitlines():
        if not raw:
            continue
        code = raw[:2]
        path = raw[2:].strip() if len(raw) > 2 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if code == "!!":
            kind = "ignored"
        elif code == "??":
            kind = "untracked"
        else:
            kind = "tracked"
        rows.append({"status": code, "path": path, "kind": kind})
    return rows


def _contract_errors(
    subject: Mapping[str, Any],
    contract_id: str,
    stage: str,
    kind: str,
) -> list[str]:
    contract = subject.get("contract")
    if contract is None:
        return []
    if not isinstance(contract, Mapping):
        return ["invalid_contract"]
    errors: list[str] = []
    instance_id = _text(contract.get("contract_instance_id"))
    if instance_id and contract_id and instance_id != contract_id:
        errors.append("contract_id_mismatch")
    registry = contract.get("gate_registry")
    if isinstance(registry, Mapping) and kind not in registry:
        errors.append("contract_gate_kind_missing")
    stage_rows = contract.get("stage_graph")
    if isinstance(stage_rows, Sequence) and not isinstance(stage_rows, (str, bytes)):
        found = False
        for row in stage_rows:
            if not isinstance(row, Mapping):
                continue
            if _text(row.get("stage")) == stage and _text(row.get("gate_kind")) == kind:
                found = True
                break
        if not found:
            errors.append("contract_stage_kind_mismatch")
    else:
        errors.append("contract_stage_graph_missing")
    return errors


def _token_errors(subject: Mapping[str, Any], *, current_commit: str) -> list[str]:
    token = subject.get("precheck_token") or subject.get("precheck_result")
    if not isinstance(token, Mapping):
        return ["missing_precheck_token"]
    errors: list[str] = []
    if not _text(token.get("precheck_run_id")):
        errors.append("missing_precheck_run_id")
    if _text(token.get("evidence_hash")) and not _text(token.get("evidence_hash")).startswith("sha256:"):
        errors.append("invalid_precheck_evidence_hash")
    token_subject = token.get("subject") if isinstance(token.get("subject"), Mapping) else {}
    token_commit = _first_text(
        token_subject,
        "source_commit",
        "merge_commit",
        "head_commit",
        "current_commit",
    )
    token_fence = _first_text(token_subject, "fence_token")
    subject_fence = _first_text(subject, "fence_token")
    if current_commit:
        if not token_commit:
            errors.append("missing_precheck_token_subject_commit")
        elif token_commit != current_commit:
            errors.append("precheck_token_subject_commit_mismatch")
    if subject_fence:
        if not token_fence:
            errors.append("missing_precheck_token_subject_fence")
        elif token_fence != subject_fence:
            errors.append("precheck_token_subject_fence_mismatch")
    return errors


def _missing_required_evidence(
    subject: Mapping[str, Any],
    *,
    include_close_ready: bool,
) -> list[str]:
    required = _required_evidence_ids(subject)
    if not include_close_ready:
        required = [item for item in required if item != "close_ready"]
    present = _present_evidence_ids(subject.get("contract_evidence"))
    return sorted(item for item in required if item not in present)


def _required_evidence_ids(subject: Mapping[str, Any]) -> list[str]:
    explicit = _string_list(subject.get("required_evidence_ids"))
    contract = subject.get("contract")
    if isinstance(contract, Mapping):
        for item in contract.get("evidence_requirements") or []:
            if isinstance(item, Mapping) and bool(item.get("required")):
                evidence_id = _text(item.get("id"))
                if evidence_id:
                    explicit.append(evidence_id)
    return _dedupe(explicit)


def _present_evidence_ids(value: Any) -> set[str]:
    present: set[str] = set()
    if isinstance(value, Mapping):
        iterable = value.values()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        iterable = value
    else:
        iterable = []
    for item in iterable:
        if isinstance(item, str):
            present.add(item)
            continue
        if not isinstance(item, Mapping):
            continue
        status = _text(item.get("status") or item.get("decision")).lower()
        if status and status not in PASS_STATUSES:
            continue
        evidence_id = _text(
            item.get("id") or item.get("requirement_id") or item.get("evidence_id")
        )
        if evidence_id:
            present.add(evidence_id)
    return present


def _has_pass_evidence(value: Any) -> bool:
    if isinstance(value, Mapping):
        status = _text(value.get("status") or value.get("decision")).lower()
        return bool(value.get("passed")) or status in PASS_STATUSES
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_has_pass_evidence(item) for item in value)
    return False


def _has_timeline_kind(value: Any, required_kinds: set[str]) -> bool:
    found: set[str] = set()
    items: Sequence[Any]
    if isinstance(value, Mapping):
        items = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = value
    else:
        items = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        status = _text(item.get("status") or item.get("decision")).lower()
        if status and status not in PASS_STATUSES:
            continue
        kind = _text(item.get("event_kind") or item.get("kind")).lower()
        if kind:
            found.add(kind)
    return required_kinds.issubset(found)


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _is_runtime_or_operator_path(path: str) -> bool:
    return (
        path == "agent/governance/server.py"
        or path.startswith("frontend/dashboard/")
        or path == "agent/mcp/tools.py"
        or path.startswith("shared-volume/")
    )


def _path(subject: Mapping[str, Any], *keys: str) -> str:
    return _first_text(subject, *keys)


def _first_text(subject: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        token = _text(subject.get(key))
        if token:
            return token
    return ""


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [str(item) for item in value if str(item or "").strip()]
    return []


def _text(value: Any) -> str:
    return str(value or "").strip()


def _dedupe(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _text(value)
        if item and item not in seen:
            out.append(item)
            seen.add(item)
    return out


def _hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _slug(value: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in value).strip("-")
