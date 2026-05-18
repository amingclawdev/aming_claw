"""Generated projects for graph rule fingerprint rollback scenarios."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


RULE_FINGERPRINT_SCENARIO_ID = "RULE-FINGERPRINT-ROLLBACK-001"


@dataclass(frozen=True)
class RuleFingerprintFixtureProject:
    root: Path
    service_path: Path
    config_path: Path


@dataclass(frozen=True)
class RuleFingerprintGitFixtureProject(RuleFingerprintFixtureProject):
    head_commit: str


BASE_SERVICE_TEXT = "def run():\n    return 'ok'\n"
CONFIG_PATCH_TEXT = "graph_structure:\n  allowed_ops:\n    - add_edge\n"
HINT_SERVICE_TEXT = (
    "def run():\n"
    "    # aming-claw-hint:start id=hint-rollback op=add_edge edge=tests target=L7.1\n"
    "    # reason: generated project rollback test\n"
    "    # aming-claw-hint:end\n"
    "    return 'ok'\n"
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def create_rule_fingerprint_fixture_project(
    tmp_path: Path,
    *,
    name: str = "generated-rule-fingerprint-project",
) -> RuleFingerprintFixtureProject:
    """Create the deterministic non-git project used by fingerprint tests."""
    project = tmp_path / name
    service_path = project / "agent" / "service.py"
    config_path = project / ".aming-claw" / "reconcile" / "semantic_enrichment.yaml"
    _write(service_path, BASE_SERVICE_TEXT)
    return RuleFingerprintFixtureProject(
        root=project,
        service_path=service_path,
        config_path=config_path,
    )


def create_rule_fingerprint_git_fixture_project(
    tmp_path: Path,
    *,
    name: str = "generated-rule-rollback-project",
) -> RuleFingerprintGitFixtureProject:
    """Create the deterministic git-backed project used by stale queue tests."""
    fixture = create_rule_fingerprint_fixture_project(tmp_path, name=name)
    _git(["init"], cwd=fixture.root)
    _git(["config", "user.email", "test@example.com"], cwd=fixture.root)
    _git(["config", "user.name", "Test User"], cwd=fixture.root)
    _git(["add", "."], cwd=fixture.root)
    _git(["commit", "-m", "base"], cwd=fixture.root)
    head = _git(["rev-parse", "HEAD"], cwd=fixture.root).stdout.strip()
    return RuleFingerprintGitFixtureProject(
        root=fixture.root,
        service_path=fixture.service_path,
        config_path=fixture.config_path,
        head_commit=head,
    )


def apply_config_change(fixture: RuleFingerprintFixtureProject) -> None:
    _write(fixture.config_path, CONFIG_PATCH_TEXT)


def rollback_config_change(fixture: RuleFingerprintFixtureProject) -> None:
    if fixture.config_path.exists():
        fixture.config_path.unlink()


def apply_hint_change(fixture: RuleFingerprintFixtureProject) -> None:
    _write(fixture.service_path, HINT_SERVICE_TEXT)


def rollback_hint_change(fixture: RuleFingerprintFixtureProject) -> None:
    _write(fixture.service_path, BASE_SERVICE_TEXT)


def rule_fingerprint_mismatch_pair() -> tuple[dict, dict]:
    """Return deterministic old/current rule fingerprints for stale queue tests."""
    old_rule = {
        "fingerprint": "sha256:anchor-before-rollback",
        "components": {"algorithm": {"fingerprint": "sha256:algo-v1"}},
    }
    current_rule = {
        "fingerprint": "sha256:current-after-rollback",
        "components": {"algorithm": {"fingerprint": "sha256:algo-v2"}},
    }
    return old_rule, current_rule
