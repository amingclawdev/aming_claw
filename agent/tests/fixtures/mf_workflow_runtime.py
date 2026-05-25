"""Fixture-backed MF workflow runtime scenarios.

The helpers create isolated temporary git repositories and linked worktrees.
They never inspect or mutate the live Aming Claw checkout.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Any


SCENARIOS = {
    "SCN-MF-WF-001": "contract stage graph",
    "SCN-MF-WF-002": "dispatch clean/isolation/HEAD gate",
    "SCN-MF-WF-003": "handoff dirty scope with untracked and ignored files",
    "SCN-MF-WF-004": "merge token/source commit gate",
    "SCN-MF-WF-006": "close gate evidence",
}

CONTRACT_ID = "MF-WORKFLOW-PRECHECK-SERVICE-20260525"
FENCE_TOKEN = "fence-mf-workflow-precheck-96c1289"


@dataclass(frozen=True)
class MfWorkflowFixture:
    root: Path
    main_worktree: Path
    worker_worktree: Path
    base_commit: str
    target_head_commit: str
    branch: str
    owned_files: tuple[str, ...]
    forbidden_paths: tuple[str, ...]

    def dispatch_subject(self, contract: dict[str, Any]) -> dict[str, Any]:
        return {
            "contract": {**contract, "contract_instance_id": CONTRACT_ID},
            "branch": self.branch,
            "worker_worktree": str(self.worker_worktree),
            "target_worktree": str(self.main_worktree),
            "base_commit": self.base_commit,
            "target_head_commit": self.target_head_commit,
            "fence_token": FENCE_TOKEN,
            "owned_files": list(self.owned_files),
            "forbidden_paths": list(self.forbidden_paths),
        }

    def handoff_subject(self, contract: dict[str, Any]) -> dict[str, Any]:
        return {
            "contract": {**contract, "contract_instance_id": CONTRACT_ID},
            "worker_worktree": str(self.worker_worktree),
            "fence_token": FENCE_TOKEN,
            "owned_files": list(self.owned_files),
            "forbidden_paths": list(self.forbidden_paths),
            "tests_evidence": passed_tests(),
            "timeline_evidence": implementation_verification_timeline(),
        }

    def merge_subject(
        self,
        contract: dict[str, Any],
        *,
        source_commit: str,
        precheck_token: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "contract": {**contract, "contract_instance_id": CONTRACT_ID},
            "main_worktree": str(self.main_worktree),
            "source_worktree": str(self.worker_worktree),
            "source_commit": source_commit,
            "fence_token": FENCE_TOKEN,
            "precheck_token": precheck_token,
            "contract_evidence": complete_contract_evidence(contract),
            "timeline_evidence": implementation_verification_timeline(),
            "required_evidence_ids": required_evidence_ids(contract),
        }

    def close_subject(
        self,
        contract: dict[str, Any],
        *,
        merge_commit: str,
        precheck_token: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "contract": {**contract, "contract_instance_id": CONTRACT_ID},
            "merge_commit": merge_commit,
            "fence_token": FENCE_TOKEN,
            "precheck_token": precheck_token,
            "contract_evidence": complete_contract_evidence(contract),
            "timeline_evidence": [
                *implementation_verification_timeline(),
                {"event_kind": "close_ready", "status": "passed", "event_id": "tl-close"},
            ],
            "required_evidence_ids": required_evidence_ids(contract),
        }


def create_runtime_fixture(tmp_path: Path) -> MfWorkflowFixture:
    main = tmp_path / "target"
    worker = tmp_path / "worker"
    main.mkdir()
    _git(main, "init", "-b", "main")
    _git(main, "config", "user.email", "mf@example.test")
    _git(main, "config", "user.name", "MF Test")
    _write(main / ".gitignore", "*.ignored\n")
    _write(main / "agent/governance/precheck_service.py", "BASE = 1\n")
    _write(main / "agent/governance/mf_workflow_runtime.py", "BASE = 1\n")
    _write(main / "docs/governance/manual-fix-sop.md", "# SOP\n")
    _git(main, "add", ".")
    _git(main, "commit", "-m", "initial fixture")
    base = _git(main, "rev-parse", "HEAD")
    _git(main, "worktree", "add", "-b", "mf/workflow-precheck-service-20260525", str(worker), base)
    _git(worker, "config", "user.email", "mf@example.test")
    _git(worker, "config", "user.name", "MF Test")
    return MfWorkflowFixture(
        root=tmp_path,
        main_worktree=main,
        worker_worktree=worker,
        base_commit=base,
        target_head_commit=base,
        branch="mf/workflow-precheck-service-20260525",
        owned_files=(
            "agent/governance/precheck_service.py",
            "agent/governance/mf_workflow_runtime.py",
            "docs/governance/manual-fix-sop.md",
            "agent/tests/fixtures/mf_workflow_runtime.py",
        ),
        forbidden_paths=(
            "agent/governance/server.py",
            "frontend/dashboard/**",
            "agent/mcp/tools.py",
            "shared-volume/**",
        ),
    )


def make_handoff_dirty_scope(fixture: MfWorkflowFixture) -> None:
    _write(fixture.worker_worktree / "agent/governance/precheck_service.py", "BASE = 2\n")
    _write(fixture.worker_worktree / "agent/governance/mf_workflow_runtime.py", "BASE = 2\n")
    _write(fixture.worker_worktree / "agent/tests/fixtures/mf_workflow_runtime.py", "fixture = True\n")
    _write(fixture.worker_worktree / "scratch.ignored", "ignored\n")


def make_forbidden_change(fixture: MfWorkflowFixture) -> None:
    _write(fixture.worker_worktree / "frontend/dashboard/src/App.tsx", "forbidden\n")


def commit_worker_candidate(fixture: MfWorkflowFixture, message: str = "candidate") -> str:
    _write(fixture.worker_worktree / "agent/governance/precheck_service.py", "BASE = 3\n")
    _git(fixture.worker_worktree, "add", "agent/governance/precheck_service.py")
    _git(fixture.worker_worktree, "commit", "-m", message)
    return _git(fixture.worker_worktree, "rev-parse", "HEAD")


def make_precheck_token(source_commit: str) -> dict[str, Any]:
    return {
        "precheck_run_id": "precheck-mf-subagent-handoff-fixture",
        "evidence_hash": "sha256:" + ("a" * 64),
        "subject": {
            "source_commit": source_commit,
            "fence_token": FENCE_TOKEN,
        },
    }


def passed_tests() -> dict[str, Any]:
    return {
        "status": "passed",
        "command": "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest ... -q",
    }


def implementation_verification_timeline() -> list[dict[str, Any]]:
    return [
        {"event_kind": "implementation", "status": "accepted", "event_id": "tl-impl"},
        {"event_kind": "verification", "status": "passed", "event_id": "tl-verify"},
    ]


def required_evidence_ids(contract: dict[str, Any]) -> list[str]:
    return [
        str(item["id"])
        for item in contract.get("evidence_requirements", [])
        if item.get("required")
    ]


def complete_contract_evidence(contract: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"id": evidence_id, "status": "passed"}
        for evidence_id in required_evidence_ids(contract)
    ]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()
