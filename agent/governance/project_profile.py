"""Project profile discovery for reconcile bootstrap boundaries.

The profile is intentionally conservative: it discovers source, test, doc,
and excluded roots before symbol scanning so Phase Z can build a production
code graph while keeping tests/docs as downstream consumers.
"""
from __future__ import annotations

import os
import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

from agent.governance.language_policy import DEFAULT_LANGUAGE_POLICY, LanguagePolicy

SOURCE_EXTENSIONS = set(DEFAULT_LANGUAGE_POLICY.source_extensions)
PYTHON_EXTENSIONS = set(DEFAULT_LANGUAGE_POLICY.python_extensions)
TEST_DIR_NAMES = set(DEFAULT_LANGUAGE_POLICY.test_dir_names)
DOC_DIR_NAMES = set(DEFAULT_LANGUAGE_POLICY.doc_dir_names)
DEFAULT_EXCLUDE_ROOTS = set(DEFAULT_LANGUAGE_POLICY.exclude_roots)
MANIFEST_LANGUAGE_HINTS = dict(DEFAULT_LANGUAGE_POLICY.manifest_language_hints)


@dataclass(frozen=True)
class ProjectProfile:
    """Discovered source/test/doc boundaries for a project root."""

    project_root: str
    languages: List[str] = field(default_factory=list)
    source_roots: List[str] = field(default_factory=list)
    test_roots: List[str] = field(default_factory=list)
    doc_roots: List[str] = field(default_factory=list)
    exclude_roots: List[str] = field(default_factory=list)
    ignore_globs: List[str] = field(default_factory=list)
    manifest_files: List[str] = field(default_factory=list)
    language_policy: LanguagePolicy = field(
        default=DEFAULT_LANGUAGE_POLICY,
        compare=False,
        repr=False,
    )

    def normalize_relpath(self, path: str) -> str:
        return self.language_policy.normalize_relpath(self.project_root, path)

    def is_excluded_path(self, path: str) -> bool:
        rel = self.normalize_relpath(path)
        return (
            self.language_policy.is_excluded_path(rel, self.exclude_roots)
            or _matches_any_glob(rel, self.ignore_globs)
        )

    def is_doc_path(self, path: str) -> bool:
        rel = self.normalize_relpath(path)
        return self.language_policy.is_doc_path(rel, self.doc_roots)

    def is_test_path(self, path: str) -> bool:
        rel = self.normalize_relpath(path)
        return self.language_policy.is_test_path(rel, self.test_roots)

    def is_production_source_path(self, path: str) -> bool:
        rel = self.normalize_relpath(path)
        return (
            self.language_policy.is_source_path(rel)
            and not self.language_policy.is_config_path(rel)
            and not self.language_policy.is_generated_path(rel)
            and not self.is_excluded_path(rel)
            and not self.is_test_path(rel)
            and not self.is_doc_path(rel)
        )


def discover_project_profile(
    project_root: str,
    extra_exclude_roots: Optional[Iterable[str]] = None,
    extra_ignore_globs: Optional[Iterable[str]] = None,
) -> ProjectProfile:
    """Discover a minimal language/profile boundary map for *project_root*."""
    root = Path(project_root).resolve()
    manifests = _discover_manifests(root)
    languages = _discover_languages(root, manifests)
    test_roots = _discover_named_dirs(root, TEST_DIR_NAMES)
    doc_roots = _discover_named_dirs(root, DOC_DIR_NAMES)
    exclude_roots = _merge_roots(
        _discover_existing_excludes(root),
        _configured_exclude_roots(root),
        extra_exclude_roots or [],
    )
    ignore_globs = _merge_roots(_configured_ignore_globs(root), extra_ignore_globs or [])
    source_roots = _discover_source_roots(root, test_roots, doc_roots, exclude_roots)

    if not source_roots:
        source_roots = ["."]

    return ProjectProfile(
        project_root=str(root),
        languages=languages,
        source_roots=source_roots,
        test_roots=test_roots,
        doc_roots=doc_roots,
        exclude_roots=exclude_roots,
        ignore_globs=ignore_globs,
        manifest_files=manifests,
    )


def _discover_manifests(root: Path) -> List[str]:
    found = []
    for path in _iter_files(root):
        if DEFAULT_LANGUAGE_POLICY.manifest_language(path.name):
            found.append(_rel(root, path))
    return sorted(found)


def _discover_languages(root: Path, manifests: Iterable[str]) -> List[str]:
    langs = {
        DEFAULT_LANGUAGE_POLICY.manifest_language(name)
        for name in manifests
        if DEFAULT_LANGUAGE_POLICY.manifest_language(name)
    }
    for path in _iter_files(root):
        language = DEFAULT_LANGUAGE_POLICY.language_for_path(str(path))
        if language in {"python", "javascript", "typescript", "go", "rust", "cpp"}:
            langs.add(language)
    return sorted(langs)


def _discover_named_dirs(root: Path, names: set[str]) -> List[str]:
    found = set()
    for dirpath, dirnames, _filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not DEFAULT_LANGUAGE_POLICY.is_excluded_path(d)]
        for dirname in list(dirnames):
            if dirname.lower() in names:
                found.add(_rel(root, Path(dirpath) / dirname))
    return sorted(found)


def _discover_existing_excludes(root: Path) -> List[str]:
    found = set()
    for name in DEFAULT_EXCLUDE_ROOTS:
        if (root / name).exists():
            found.add(name)
    return sorted(found)


def _configured_exclude_roots(root: Path) -> List[str]:
    try:
        from project_config import effective_graph_exclude_roots, load_project_config  # type: ignore

        config = load_project_config(root)
    except Exception:
        return []
    return _merge_roots(effective_graph_exclude_roots(config))


def _configured_ignore_globs(root: Path) -> List[str]:
    try:
        from project_config import load_project_config  # type: ignore

        config = load_project_config(root)
    except Exception:
        return []
    values = getattr(getattr(config, "graph", None), "ignore_globs", []) or []
    out: List[str] = []
    seen: set[str] = set()
    for value in values:
        glob = str(value or "").replace("\\", "/").strip().strip("/")
        if not glob or glob in seen:
            continue
        seen.add(glob)
        out.append(glob)
    return sorted(out)


def _merge_roots(*groups: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group or []:
            rel = str(value or "").replace("\\", "/").strip().strip("/")
            if not rel or rel in seen:
                continue
            seen.add(rel)
            out.append(rel)
    return sorted(out)


def _discover_source_roots(
    root: Path,
    test_roots: List[str],
    doc_roots: List[str],
    exclude_roots: List[str],
) -> List[str]:
    roots = set()
    if _contains_root_source_file(root):
        roots.add(".")
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        rel = _rel(root, child)
        lower = child.name.lower()
        if lower in TEST_DIR_NAMES or lower in DOC_DIR_NAMES:
            continue
        if _is_under_any(rel, test_roots) or _is_under_any(rel, doc_roots):
            continue
        if DEFAULT_LANGUAGE_POLICY.is_excluded_path(child.name) or _is_under_any(rel, exclude_roots):
            continue
        if _contains_source_file(child, root, exclude_roots):
            roots.add(rel)
    return sorted(roots)


def _contains_root_source_file(root: Path) -> bool:
    for child in root.iterdir():
        if not child.is_file():
            continue
        if child.name in DEFAULT_EXCLUDE_ROOTS:
            continue
        if DEFAULT_LANGUAGE_POLICY.is_source_path(str(child)):
            return True
    return False


def _contains_source_file(path: Path, root: Path, exclude_roots: List[str] | None = None) -> bool:
    for file_path in _iter_files(path):
        rel = _rel(root, file_path)
        if _is_under_any(rel, exclude_roots or []):
            continue
        parts = [p.lower() for p in rel.split("/") if p]
        if any(part in TEST_DIR_NAMES or part in DOC_DIR_NAMES for part in parts):
            continue
        if DEFAULT_LANGUAGE_POLICY.is_source_path(str(file_path)):
            return True
    return False


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not DEFAULT_LANGUAGE_POLICY.is_excluded_path(d)]
        for fname in filenames:
            yield Path(dirpath) / fname


def _rel(root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(root)).replace("\\", "/")


def _is_under_any(rel: str, roots: Iterable[str]) -> bool:
    norm = str(rel or "").replace("\\", "/").strip("/")
    for root in roots or []:
        base = str(root or "").replace("\\", "/").strip("/")
        if not base:
            continue
        if norm == base or norm.startswith(base + "/"):
            return True
    return False


def _matches_any_glob(rel: str, globs: Iterable[str]) -> bool:
    norm = str(rel or "").replace("\\", "/").strip("/")
    for pattern in globs or []:
        glob = str(pattern or "").replace("\\", "/").strip("/")
        if glob and fnmatch.fnmatch(norm, glob):
            return True
    return False


__all__ = ["ProjectProfile", "discover_project_profile"]
