"""Generated git fixtures for parallel branch scenario tests."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ParallelFixtureProject:
    root: Path
    main_head: str


@dataclass(frozen=True)
class MergePreviewFixtureProject(ParallelFixtureProject):
    clean_branch: str
    conflict_branch: str


def git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def create_parallel_fixture_project(tmp_path: Path, *, name: str = "parallel-project") -> ParallelFixtureProject:
    """Create a deterministic git-backed project for branch/worktree tests."""
    repo = tmp_path / name
    repo.mkdir()
    git(["init"], cwd=repo)
    git(["checkout", "-b", "main"], cwd=repo)
    git(["config", "user.email", "test@example.com"], cwd=repo)
    git(["config", "user.name", "Test User"], cwd=repo)

    (repo / "README.md").write_text("# Parallel Fixture Project\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "service.py").write_text(
        "def service_entry():\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_service.py").write_text(
        "from src.service import service_entry\n\n"
        "def test_service_entry():\n"
        "    assert service_entry() == 'ok'\n",
        encoding="utf-8",
    )
    git(["add", "."], cwd=repo)
    git(["commit", "-m", "base fixture"], cwd=repo)
    main_head = git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    return ParallelFixtureProject(root=repo, main_head=main_head)


def create_merge_preview_fixture_project(tmp_path: Path) -> MergePreviewFixtureProject:
    """Create a git project with clean and conflicting feature branches."""
    fixture = create_parallel_fixture_project(tmp_path, name="merge-preview-project")
    repo = fixture.root

    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    git(["add", "shared.txt"], cwd=repo)
    git(["commit", "-m", "shared base"], cwd=repo)

    git(["checkout", "-b", "feature-clean"], cwd=repo)
    (repo / "clean.txt").write_text("clean\n", encoding="utf-8")
    git(["add", "clean.txt"], cwd=repo)
    git(["commit", "-m", "clean branch"], cwd=repo)

    git(["checkout", "main"], cwd=repo)
    git(["checkout", "-b", "feature-conflict"], cwd=repo)
    (repo / "shared.txt").write_text("branch\n", encoding="utf-8")
    git(["commit", "-am", "conflict branch"], cwd=repo)

    git(["checkout", "main"], cwd=repo)
    (repo / "shared.txt").write_text("main\n", encoding="utf-8")
    git(["commit", "-am", "main change"], cwd=repo)
    main_head = git(["rev-parse", "HEAD"], cwd=repo).stdout.strip()
    return MergePreviewFixtureProject(
        root=repo,
        main_head=main_head,
        clean_branch="feature-clean",
        conflict_branch="feature-conflict",
    )
