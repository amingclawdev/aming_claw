"""Phase Z v2 — Symbol-level topology infrastructure + driver.

PR1: AST-based parsing, import-aware call graph construction, Tarjan SCC,
and hybrid cycle handling.
PR2: Scoring + aggregation.
PR3: Driver function, coverage lookup, diff, artifact write, CLI.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from agent.governance.language_adapters import (
    FileTreeAdapter,
    JavaScriptTypescriptAdapter,
    LanguageAdapter,
    PythonAdapter,
)
from agent.governance.language_policy import DEFAULT_LANGUAGE_POLICY

# ---------------------------------------------------------------------------
# R6: Directories to exclude from production module scanning
# ---------------------------------------------------------------------------
EXCLUDE_DIRS: frozenset[str] = frozenset(DEFAULT_LANGUAGE_POLICY.exclude_roots)

# Default production directories to scan
DEFAULT_PROD_DIRS: Tuple[str, ...] = ("agent", "scripts")
_GRAPH_LANGUAGE_ADAPTERS: Tuple[LanguageAdapter, ...] = (
    PythonAdapter(),
    JavaScriptTypescriptAdapter(),
)
_FILETREE_ADAPTER = FileTreeAdapter()
_IMPORT_RESOLUTION_SUFFIXES: Tuple[str, ...] = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class FunctionMeta:
    """Metadata for a single function/method extracted via AST."""
    module: str
    name: str
    qualified_name: str  # module::func_name
    lineno: int
    end_lineno: int
    decorators: List[str] = field(default_factory=list)
    calls: List[str] = field(default_factory=list)
    call_contexts: List[Dict[str, Any]] = field(default_factory=list)
    is_entry: bool = False


@dataclass
class ModuleInfo:
    """Parsed information for a single production module."""
    path: str
    module_name: str  # dotted module name
    import_map: Dict[str, str] = field(default_factory=dict)
    # import_map: local_name -> fully_qualified_name
    # e.g. {"get_config": "agent.config.get_config", "os": "os"}
    functions: List[FunctionMeta] = field(default_factory=list)
    source: str = ""
    language: str = ""
    source_kind: str = "symbol_ast"
    adapter_symbols: List[Dict[str, Any]] = field(default_factory=list)
    adapter_imports: List[Dict[str, Any]] = field(default_factory=list)
    adapter_relations: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class TypedRelation:
    """Language-neutral relation extracted from code, state, or artifacts."""
    source_module: str
    relation_type: str
    target: str
    target_kind: str
    evidence: str = ""
    source_file: str = ""


@dataclass
class CallEdge:
    """A resolved call edge in the call graph."""
    caller: str  # qualified_name of caller
    target: str  # qualified_name of target
    confidence: str = "strong"  # "strong" | "weak"


@dataclass
class WeakEdge:
    """An ambiguous call that could not be uniquely resolved."""
    caller: str
    target: str  # the raw call target as written in source
    candidates: List[str] = field(default_factory=list)
    reason: str = ""
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CallGraph:
    """The full call graph with resolved and ambiguous edges."""
    edges: Dict[str, List[str]] = field(default_factory=dict)
    # caller -> [target qualified names]
    weak_edges: List[WeakEdge] = field(default_factory=list)
    all_functions: Dict[str, FunctionMeta] = field(default_factory=dict)


@dataclass
class CycleDecision:
    """Result of handle_cycle() for a single SCC."""
    scc: List[str]
    action: str  # "auto_break" | "block_for_observer"
    reason: str = ""
    weak_edge: Optional[str] = None  # edge to break if auto_break


# ---------------------------------------------------------------------------
# R1: AST-based parse_production_modules
# ---------------------------------------------------------------------------

class _ImportExtractor(ast.NodeVisitor):
    """Extract import map from a module AST."""

    def __init__(self, module_name: str = "") -> None:
        self.module_name = module_name
        self.import_map: Dict[str, str] = {}

    def _resolve_from_import(self, node: ast.ImportFrom, alias_name: str) -> str:
        module = node.module or ""
        if int(getattr(node, "level", 0) or 0) <= 0:
            return f"{module}.{alias_name}" if module else alias_name
        package_parts = self.module_name.split(".")[:-1]
        levels_up = int(node.level) - 1
        if levels_up > 0:
            package_parts = package_parts[:-levels_up] if levels_up < len(package_parts) else []
        parts = [part for part in package_parts if part]
        if module:
            parts.extend(part for part in module.split(".") if part)
            parts.append(alias_name)
        else:
            parts.append(alias_name)
        return ".".join(parts)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name
            self.import_map[local] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            local = alias.asname or alias.name
            self.import_map[local] = self._resolve_from_import(node, alias.name)
        self.generic_visit(node)


class _FunctionExtractor(ast.NodeVisitor):
    """Extract function metadata from a module AST."""

    def __init__(self, module_name: str, import_map: Dict[str, str]) -> None:
        self.module_name = module_name
        self.import_map = import_map
        self.functions: List[FunctionMeta] = []
        self._current_class: Optional[str] = None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        old = self._current_class
        self._current_class = node.name
        self.generic_visit(node)
        self._current_class = old

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._extract_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._extract_function(node)

    def _extract_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        name = node.name
        if self._current_class:
            name = f"{self._current_class}.{name}"

        qualified = f"{self.module_name}::{name}"

        decorators = []
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name):
                decorators.append(dec.id)
            elif isinstance(dec, ast.Attribute):
                decorators.append(dec.attr)
            elif isinstance(dec, ast.Call):
                if isinstance(dec.func, ast.Name):
                    decorators.append(dec.func.id)
                elif isinstance(dec.func, ast.Attribute):
                    decorators.append(dec.func.attr)

        call_records = _extract_call_records(node)
        calls = [str(record.get("target") or "") for record in call_records]

        is_entry = any(d in ("app", "route", "cli", "command", "main")
                       for d in decorators) or name in ("main", "__main__")

        end_lineno = getattr(node, "end_lineno", node.lineno)

        fm = FunctionMeta(
            module=self.module_name,
            name=name,
            qualified_name=qualified,
            lineno=node.lineno,
            end_lineno=end_lineno,
            decorators=decorators,
            calls=calls,
            call_contexts=call_records,
            is_entry=is_entry,
        )
        self.functions.append(fm)
        # Don't visit nested functions as separate top-level
        self.generic_visit(node)


def _extract_calls(node: ast.AST) -> List[str]:
    """Extract all function call targets from a function body."""
    return [str(record.get("target") or "") for record in _extract_call_records(node)]


def _extract_call_records(node: ast.AST) -> List[Dict[str, Any]]:
    """Extract function call targets plus language-neutral call context."""
    records: List[Dict[str, Any]] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            target = _call_target_name(child)
            if target:
                records.append(_call_record(child, target))
    return records


def _call_record(node: ast.Call, target: str) -> Dict[str, Any]:
    func = node.func
    raw_target = target.rsplit(".", 1)[-1]
    if isinstance(func, ast.Name):
        call_syntax = "name_call"
        receiver_kind = ""
    elif isinstance(func, ast.Attribute):
        call_syntax = "attribute_call"
        receiver_kind = _call_receiver_kind(func.value)
    else:
        call_syntax = ""
        receiver_kind = ""
    return {
        "target": target,
        "raw_target": raw_target,
        "call_syntax": call_syntax,
        "receiver_kind": receiver_kind,
    }


def _call_receiver_kind(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        called = _call_target_name(node)
        if called in {"set", "list", "dict"}:
            return "builtin_collection"
        return "call_result"
    if isinstance(node, ast.Name):
        return "local_name"
    if isinstance(node, ast.Subscript):
        return "subscript"
    if isinstance(node, ast.Attribute):
        return "attribute"
    return ""


def _call_target_name(node: ast.Call) -> Optional[str]:
    """Get the string representation of a call target."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts = []
        current = func
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        parts.reverse()
        return ".".join(parts)
    return None


def _path_to_module(path: str, root: str) -> str:
    """Convert a file path to a dotted module name relative to root's parent."""
    rel = os.path.relpath(path, os.path.dirname(root) if not os.path.isdir(root) else os.path.dirname(root))
    # Actually, root is the project root containing agent/ and scripts/
    rel = os.path.relpath(path, root)
    rel = rel.replace(os.sep, "/")
    rel = DEFAULT_LANGUAGE_POLICY.strip_source_suffix(rel)
    if rel.endswith("/__init__"):
        rel = rel[:-9]
    return rel.replace("/", ".")


def _adapter_for_source_file(file_path: str) -> LanguageAdapter:
    for adapter in _GRAPH_LANGUAGE_ADAPTERS:
        if adapter.supports(file_path):
            return adapter
    return _FILETREE_ADAPTER


def _parse_python_module(fpath: str, mod_name: str, source: str) -> Optional[ModuleInfo]:
    try:
        tree = ast.parse(source, filename=fpath)
    except (SyntaxError, ValueError):
        return None

    imp_ext = _ImportExtractor(mod_name)
    imp_ext.visit(tree)

    func_ext = _FunctionExtractor(mod_name, imp_ext.import_map)
    func_ext.visit(tree)

    return ModuleInfo(
        path=fpath,
        module_name=mod_name,
        import_map=imp_ext.import_map,
        functions=func_ext.functions,
        source=source,
        language="python",
        source_kind="python_ast",
    )


def _parse_filetree_module(
    project_root: str,
    fpath: str,
    mod_name: str,
    source: str,
    adapter: LanguageAdapter,
    module_path: str,
) -> ModuleInfo:
    metadata = adapter.classify_file(fpath) if hasattr(adapter, "classify_file") else {}
    language = str(metadata.get("language") or DEFAULT_LANGUAGE_POLICY.language_for_path(fpath))
    symbols = _safe_adapter_symbols(adapter, fpath, source)
    imports = _safe_adapter_imports(adapter, fpath, source)
    relations = _safe_adapter_relations(adapter, fpath, source, symbols=symbols, imports=imports)
    return ModuleInfo(
        path=module_path,
        module_name=mod_name,
        import_map=_adapter_import_map(project_root, module_path, imports),
        functions=_function_meta_from_adapter_symbols(mod_name, symbols),
        source=source,
        language=language,
        source_kind="filetree_fallback" if isinstance(adapter, FileTreeAdapter) else "adapter_static",
        adapter_symbols=symbols,
        adapter_imports=imports,
        adapter_relations=relations,
    )


def _safe_adapter_symbols(
    adapter: LanguageAdapter,
    file_path: str,
    source: str,
) -> List[Dict[str, Any]]:
    try:
        return [
            item for item in adapter.parse_symbols(file_path, source)
            if isinstance(item, dict)
        ]
    except Exception:
        return []


def _safe_adapter_imports(
    adapter: LanguageAdapter,
    file_path: str,
    source: str,
) -> List[Dict[str, Any]]:
    try:
        return [
            item for item in adapter.parse_imports(file_path, source)
            if isinstance(item, dict)
        ]
    except Exception:
        return []


def _safe_adapter_relations(
    adapter: LanguageAdapter,
    file_path: str,
    source: str,
    *,
    symbols: List[Dict[str, Any]],
    imports: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    try:
        return [
            item for item in adapter.extract_relations(
                file_path,
                source,
                symbols=symbols,
                imports=imports,
            )
            if isinstance(item, dict)
        ]
    except Exception:
        return []


def _function_meta_from_adapter_symbols(
    module_name: str,
    symbols: List[Dict[str, Any]],
) -> List[FunctionMeta]:
    functions: List[FunctionMeta] = []
    for symbol in symbols or []:
        name = str(symbol.get("name") or "").strip()
        if not name:
            continue
        lineno = int(symbol.get("lineno") or 1)
        end_lineno = int(symbol.get("end_lineno") or lineno)
        decorators = [
            str(item)
            for item in (symbol.get("decorators") if isinstance(symbol.get("decorators"), list) else [])
            if str(item)
        ]
        calls = [
            str(item)
            for item in (symbol.get("calls") if isinstance(symbol.get("calls"), list) else [])
            if str(item)
        ]
        functions.append(FunctionMeta(
            module=module_name,
            name=name,
            qualified_name=f"{module_name}::{name}",
            lineno=lineno,
            end_lineno=end_lineno,
            decorators=decorators,
            calls=calls,
            is_entry=name in {"main", "App", "handler"},
        ))
    return functions


def _function_line_index(functions: List[FunctionMeta]) -> Dict[str, List[int]]:
    line_index: Dict[str, List[int]] = {}
    for func in functions:
        short_name = str(func.qualified_name or func.name).rsplit("::", 1)[-1]
        if not short_name:
            continue
        start = int(func.lineno or 0)
        if start <= 0:
            continue
        end = int(func.end_lineno or start)
        if end <= 0:
            end = start
        line_index[short_name] = [start, end]
    return line_index


def _adapter_import_map(
    project_root: str,
    source_file: str,
    imports: List[Dict[str, Any]],
) -> Dict[str, str]:
    import_map: Dict[str, str] = {}
    for row in imports or []:
        specifier = str(row.get("specifier") or row.get("imported") or "").strip()
        if not specifier:
            continue
        imported = _resolve_adapter_import_row(project_root, source_file, row, specifier)
        local = str(row.get("local") or specifier).strip() or specifier
        import_map[local] = imported
    return import_map


def _resolve_adapter_import_row(
    project_root: str,
    source_file: str,
    row: Dict[str, Any],
    specifier: str,
) -> str:
    level = int(row.get("level") or 0)
    if level <= 0:
        return _resolve_source_import(project_root, source_file, specifier)
    source_module = _path_to_module(source_file, project_root)
    package_parts = source_module.split(".")[:-1]
    levels_up = level - 1
    if levels_up > 0:
        package_parts = package_parts[:-levels_up] if levels_up < len(package_parts) else []
    module = str(row.get("module") or "").strip(".")
    name = str(row.get("name") or "").strip(".")
    parts = [part for part in package_parts if part]
    if module:
        parts.extend(part for part in module.split(".") if part)
        if name:
            parts.append(name)
    elif name:
        parts.append(name)
    return ".".join(parts) or specifier


def _resolve_source_import(project_root: str, source_file: str, specifier: str) -> str:
    specifier = str(specifier or "").replace("\\", "/").strip()
    if not specifier.startswith("."):
        return specifier
    root = Path(project_root)
    source_path = Path(source_file.replace("\\", "/"))
    if not source_path.is_absolute():
        source_path = root / source_path
    target = (source_path.parent / specifier).resolve()
    candidates: List[Path] = []
    if target.suffix.lower() in DEFAULT_LANGUAGE_POLICY.source_extensions:
        candidates.append(target)
    else:
        candidates.extend(target.with_suffix(suffix) for suffix in _IMPORT_RESOLUTION_SUFFIXES)
        candidates.extend(target / f"index{suffix}" for suffix in _IMPORT_RESOLUTION_SUFFIXES)
    for candidate in candidates:
        if candidate.is_file():
            return _path_to_module(str(candidate), project_root)
    try:
        rel = str(target.relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        rel = str(target).replace("\\", "/")
    return DEFAULT_LANGUAGE_POLICY.strip_source_suffix(rel).replace("/", ".")


def parse_production_modules(
    project_root: str,
    prod_dirs: Optional[Tuple[str, ...]] = None,
    profile: Optional[Any] = None,
) -> Dict[str, ModuleInfo]:
    """Walk prod_dirs under project_root and dispatch source files to adapters.

    Returns dict keyed by dotted module name -> ModuleInfo.
    Skips excluded/test/doc directories so DFS operates on production code.
    """
    if profile is None:
        from agent.governance.project_profile import discover_project_profile
        profile = discover_project_profile(project_root)
    if prod_dirs is None:
        prod_dirs = tuple(getattr(profile, "source_roots", None) or DEFAULT_PROD_DIRS)

    modules: Dict[str, ModuleInfo] = {}

    for prod_dir in prod_dirs:
        base = project_root if prod_dir in ("", ".") else os.path.join(project_root, prod_dir)
        if not os.path.isdir(base):
            continue

        for dirpath, dirnames, filenames in os.walk(base):
            # Filter excluded dirs IN-PLACE so os.walk skips them
            kept_dirs = []
            for dirname in dirnames:
                rel_dir = os.path.relpath(os.path.join(dirpath, dirname), project_root)
                if dirname.lower() in EXCLUDE_DIRS:
                    continue
                if profile.is_excluded_path(rel_dir) or profile.is_test_path(rel_dir) or profile.is_doc_path(rel_dir):
                    continue
                kept_dirs.append(dirname)
            dirnames[:] = kept_dirs

            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                rel_file = os.path.relpath(fpath, project_root)
                if not profile.is_production_source_path(rel_file):
                    continue
                try:
                    source = _read_file(fpath)
                except (UnicodeDecodeError, OSError):
                    continue

                mod_name = _path_to_module(fpath, project_root)
                adapter = _adapter_for_source_file(fpath)
                if isinstance(adapter, PythonAdapter):
                    parsed = _parse_python_module(fpath, mod_name, source)
                    if parsed is None:
                        continue
                    modules[mod_name] = parsed
                else:
                    modules[mod_name] = _parse_filetree_module(
                        project_root,
                        fpath,
                        mod_name,
                        source,
                        adapter,
                        rel_file.replace(os.sep, "/"),
                    )

    return modules


def parse_production_module_file(
    project_root: str,
    rel_path: str,
    profile: Optional[Any] = None,
) -> Optional[ModuleInfo]:
    """Parse one production source file through the same adapter path as Phase Z.

    This is intentionally a narrow public helper for scope-reconcile deltas. It
    does not build call graphs, clusters, or relations for the project; callers
    must decide whether the resulting module-level facts are sufficient for a
    safe incremental update or whether to fall back to the full graph builder.
    """
    if profile is None:
        from agent.governance.project_profile import discover_project_profile
        profile = discover_project_profile(project_root)
    root = Path(project_root).resolve()
    rel = str(rel_path or "").replace("\\", "/").strip("/")
    if (
        not rel
        or profile.is_excluded_path(rel)
        or profile.is_test_path(rel)
        or profile.is_doc_path(rel)
        or not profile.is_production_source_path(rel)
    ):
        return None
    fpath = root / rel
    if not fpath.is_file():
        return None
    try:
        source = _read_file(str(fpath))
    except (UnicodeDecodeError, OSError):
        return None

    mod_name = _path_to_module(str(fpath), project_root)
    adapter = _adapter_for_source_file(str(fpath))
    if isinstance(adapter, PythonAdapter):
        return _parse_python_module(str(fpath), mod_name, source)
    return _parse_filetree_module(
        project_root,
        str(fpath),
        mod_name,
        source,
        adapter,
        rel,
    )


def _read_file(path: str) -> str:
    """Read file with fallback encoding."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except UnicodeDecodeError:
        with open(path, "r", encoding="latin-1") as f:
            return f.read()


# ---------------------------------------------------------------------------
# R2: Import-aware call graph construction
# ---------------------------------------------------------------------------

def build_call_graph(modules: Dict[str, ModuleInfo]) -> CallGraph:
    """Build a call graph with import-aware resolution.

    For each call in each function:
    1. Check if the call target is a local function in the same module
    2. Check if the call target resolves via the module's import map
    3. If ambiguous (multiple candidates), record as weak_edge
    4. Never use naive last-segment matching
    """
    graph = CallGraph()

    # Build lookup: qualified_name -> FunctionMeta
    all_funcs: Dict[str, FunctionMeta] = {}
    # Also build: short_name -> [qualified_names] for resolution
    name_to_qualified: Dict[str, List[str]] = {}
    # module_name -> {func_short_name -> qualified_name}
    module_local_funcs: Dict[str, Dict[str, str]] = {}
    function_languages: Dict[str, str] = {}

    for mod_name, mod_info in modules.items():
        module_local_funcs[mod_name] = {}
        for func in mod_info.functions:
            all_funcs[func.qualified_name] = func
            function_languages[func.qualified_name] = str(mod_info.language or "")
            module_local_funcs[mod_name][func.name] = func.qualified_name

            short = func.name
            if short not in name_to_qualified:
                name_to_qualified[short] = []
            name_to_qualified[short].append(func.qualified_name)

    graph.all_functions = all_funcs

    for mod_name, mod_info in modules.items():
        local_funcs = module_local_funcs.get(mod_name, {})

        for func in mod_info.functions:
            caller = func.qualified_name
            if caller not in graph.edges:
                graph.edges[caller] = []

            for call_index, call_target in enumerate(func.calls):
                resolved = _resolve_call(
                    call_target=call_target,
                    caller_module=mod_name,
                    import_map=mod_info.import_map,
                    local_funcs=local_funcs,
                    all_funcs=all_funcs,
                    name_to_qualified=name_to_qualified,
                    module_local_funcs=module_local_funcs,
                    caller_language=str(mod_info.language or ""),
                    function_languages=function_languages,
                )

                if resolved is None:
                    # External / builtin — skip
                    continue
                elif isinstance(resolved, str):
                    # Uniquely resolved
                    graph.edges[caller].append(resolved)
                elif isinstance(resolved, list):
                    # Ambiguous — weak edge
                    context = {}
                    if call_index < len(func.call_contexts or []):
                        context = dict(func.call_contexts[call_index] or {})
                    graph.weak_edges.append(WeakEdge(
                        caller=caller,
                        target=call_target,
                        candidates=resolved,
                        reason=f"ambiguous: {len(resolved)} candidates for '{call_target}'",
                        context=context,
                    ))

    return graph


def _resolve_call(
    call_target: str,
    caller_module: str,
    import_map: Dict[str, str],
    local_funcs: Dict[str, str],
    all_funcs: Dict[str, FunctionMeta],
    name_to_qualified: Dict[str, List[str]],
    module_local_funcs: Dict[str, Dict[str, str]],
    caller_language: str = "",
    function_languages: Optional[Dict[str, str]] = None,
) -> Optional[str | List[str]]:
    """Resolve a call target to a qualified function name.

    Returns:
        None — external/builtin, not in our codebase
        str — uniquely resolved qualified name
        list — ambiguous, multiple candidates (weak edge)
    """
    # 1. Check local scope first (same module)
    if call_target in local_funcs:
        return local_funcs[call_target]

    # 2. Check import map
    # Handle dotted calls like "config.get_value" -> check if "config" is imported
    parts = call_target.split(".")
    first_part = parts[0]

    if first_part in import_map:
        fqn_base = import_map[first_part]
        if len(parts) > 1:
            # e.g. call is "config.get_value", import_map["config"] = "agent.config"
            # resolved target = "agent.config.get_value"
            fqn_target = fqn_base + "." + ".".join(parts[1:])
        else:
            # Direct imported name, e.g. "get_config" -> "agent.config.get_config"
            fqn_target = fqn_base

        # Try to find this in all_funcs
        # The qualified name format is "module::func_name"
        # So we need to find module::func from the fqn
        resolved = _find_function_by_fqn(fqn_target, all_funcs, module_local_funcs)
        if resolved is not None:
            return resolved

    # 3. JS/TS has common lexical closures and hook setters whose short names
    # are intentionally local. Without import evidence, do not cross module
    # boundaries by short name for these languages.
    if (
        "." not in call_target
        and call_target not in import_map
        and _requires_import_for_cross_module_short_name(caller_language)
    ):
        return None

    # 4. Conservative fallback for simple names: only consider same top-level
    # namespace and same language. This avoids cross-package/cross-language false
    # edges such as Python logger.info resolving to a frontend helper named info.
    if "." not in call_target and call_target not in import_map:
        candidates = name_to_qualified.get(call_target, [])
        # Filter out self (don't count calls within the same function)
        candidates = [c for c in candidates if not c.startswith(f"{caller_module}::")]
        caller_root = _top_level_namespace(caller_module)
        candidates = [
            c for c in candidates
            if _top_level_namespace(c.split("::", 1)[0]) == caller_root
        ]
        if caller_language and function_languages:
            candidates = [
                c for c in candidates
                if not function_languages.get(c) or function_languages.get(c) == caller_language
            ]
        if len(candidates) == 1:
            return candidates[0]
        elif len(candidates) > 1:
            return candidates  # Ambiguous

    # Not resolved — external or builtin
    return None


def _requires_import_for_cross_module_short_name(language: str) -> bool:
    return str(language or "").lower() in {
        "javascript",
        "typescript",
        "javascript_typescript",
    }


def _top_level_namespace(module_name: str) -> str:
    return str(module_name or "").split(".", 1)[0]


def _find_function_by_fqn(
    fqn: str,
    all_funcs: Dict[str, FunctionMeta],
    module_local_funcs: Dict[str, Dict[str, str]],
) -> Optional[str]:
    """Find a function by its fully qualified name.

    The fqn might be like "agent.config.get_config" — we need to find
    a module "agent.config" with function "get_config".
    """
    # Try splitting at each dot from right to left
    parts = fqn.split(".")
    for i in range(len(parts) - 1, 0, -1):
        mod = ".".join(parts[:i])
        func_name = ".".join(parts[i:])
        if mod in module_local_funcs:
            local = module_local_funcs[mod]
            if func_name in local:
                return local[func_name]
    return None


# ---------------------------------------------------------------------------
# R3: Tarjan SCC algorithm
# ---------------------------------------------------------------------------

def tarjan_scc(graph: Dict[str, List[str]]) -> List[List[str]]:
    """Standard Tarjan's SCC algorithm.

    Returns ALL SCCs including singletons.
    Each SCC is a list of node names.
    """
    index_counter = [0]
    stack: List[str] = []
    on_stack: Set[str] = set()
    index: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    result: List[List[str]] = []

    # Ensure all targets are in the graph as keys (even if they have no outgoing edges)
    all_nodes: Set[str] = set(graph.keys())
    for targets in graph.values():
        for t in targets:
            all_nodes.add(t)

    def strongconnect(v: str) -> None:
        index[v] = index_counter[0]
        lowlink[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)

        for w in graph.get(v, []):
            if w not in index:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], index[w])

        if lowlink[v] == index[v]:
            scc: List[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            result.append(scc)

    for v in sorted(all_nodes):  # sorted for determinism
        if v not in index:
            strongconnect(v)

    return result


# ---------------------------------------------------------------------------
# R4 + R5: Cycle handling
# ---------------------------------------------------------------------------

def handle_cycle(
    scc: List[str],
    all_functions: Dict[str, FunctionMeta],
    graph_edges: Dict[str, List[str]],
) -> CycleDecision:
    """Decide how to handle a cycle (SCC with size >= 2).

    auto_break for false-positive shapes:
    - All members are __init__, test_, or decorator-only functions
    - Same-module size-2 cycles

    block_for_observer for:
    - Cross-module cycles
    - Size >= 3 cycles (unless all false-positive)
    """
    if len(scc) < 2:
        return CycleDecision(
            scc=scc,
            action="auto_break",
            reason="singleton — no cycle",
        )

    # Check if all members are likely false positives
    all_fp = all(
        _is_likely_false_positive_cycle_member(node, all_functions)
        for node in scc
    )

    # Check if same module
    modules_in_scc = set()
    for node in scc:
        mod = node.split("::")[0] if "::" in node else ""
        modules_in_scc.add(mod)
    same_module = len(modules_in_scc) == 1

    cross_module = not same_module

    # Decision logic
    if all_fp:
        weak = _pick_weakest_edge_in_cycle(scc, all_functions, graph_edges)
        return CycleDecision(
            scc=scc,
            action="auto_break",
            reason="all members are likely false positives (init/test/decorator)",
            weak_edge=weak,
        )

    if same_module and len(scc) == 2:
        weak = _pick_weakest_edge_in_cycle(scc, all_functions, graph_edges)
        return CycleDecision(
            scc=scc,
            action="auto_break",
            reason="same-module size-2 cycle",
            weak_edge=weak,
        )

    if cross_module:
        return CycleDecision(
            scc=scc,
            action="block_for_observer",
            reason="cross-module cycle requires observer review",
        )

    if len(scc) >= 3:
        return CycleDecision(
            scc=scc,
            action="block_for_observer",
            reason=f"size-{len(scc)} cycle requires observer review",
        )

    # Fallback: block
    return CycleDecision(
        scc=scc,
        action="block_for_observer",
        reason="unhandled cycle shape",
    )


def _is_likely_false_positive_cycle_member(
    node: str,
    all_functions: Dict[str, FunctionMeta],
) -> bool:
    """Check if a node is likely a false-positive cycle member.

    False-positive indicators:
    - __init__ methods
    - test_ prefixed functions
    - Functions with only decorator-related calls
    """
    func = all_functions.get(node)
    if func is None:
        return False

    short_name = func.name
    # __init__ methods
    if short_name.endswith("__init__") or short_name == "__init__":
        return True
    # test functions
    if short_name.startswith("test_") or ".test_" in short_name:
        return True
    # Decorator-only functions (e.g., property, staticmethod)
    decorator_only_names = {"property", "staticmethod", "classmethod", "abstractmethod"}
    if func.decorators and all(d in decorator_only_names for d in func.decorators):
        return True

    return False


def _pick_weakest_edge_in_cycle(
    scc: List[str],
    all_functions: Dict[str, FunctionMeta],
    graph_edges: Dict[str, List[str]],
) -> Optional[str]:
    """Pick the weakest edge in a cycle to break.

    Confidence ranking (weakest first):
    1. function-internal import (call inside function body to imported name)
    2. top-level import (call to top-level imported name)
    3. direct module reference (call via module.func pattern)

    Returns "caller -> target" string for the weakest edge, or None.
    """
    scc_set = set(scc)
    cycle_edges: List[Tuple[str, str, int]] = []  # (caller, target, strength)

    for node in scc:
        for target in graph_edges.get(node, []):
            if target in scc_set:
                strength = _edge_strength(node, target, all_functions)
                cycle_edges.append((node, target, strength))

    if not cycle_edges:
        return None

    # Pick weakest (lowest strength)
    cycle_edges.sort(key=lambda x: x[2])
    weakest = cycle_edges[0]
    return f"{weakest[0]} -> {weakest[1]}"


def _edge_strength(
    caller: str,
    target: str,
    all_functions: Dict[str, FunctionMeta],
) -> int:
    """Rate the strength of a call edge.

    Lower = weaker = better candidate for breaking.
    1 = function-internal import (weakest)
    2 = top-level import
    3 = direct module reference (strongest)
    """
    caller_func = all_functions.get(caller)
    target_func = all_functions.get(target)

    if caller_func is None or target_func is None:
        return 2  # default mid-strength

    caller_mod = caller_func.module
    target_mod = target_func.module

    # Same module = direct reference (strongest)
    if caller_mod == target_mod:
        return 3

    # Check if the target's short name appears in caller's calls
    # as a dotted reference (module.func pattern) — mid strength
    target_short = target_func.name
    for call in caller_func.calls:
        if "." in call and call.endswith(target_short):
            return 2  # top-level import

    # Otherwise assume function-internal import (weakest)
    return 1


# ---------------------------------------------------------------------------
# PR2: Scoring + Aggregation
# ---------------------------------------------------------------------------

def score_function_layer(
    func_qname: str,
    scc_index: Dict[str, int],
    graph_edges: Dict[str, List[str]],
    all_functions: Dict[str, FunctionMeta],
) -> int:
    """Score a function's layer based on SCC topology order and call depth.

    Lower layer = closer to leaf (no outgoing calls).
    Returns an integer layer score >= 0.
    """
    base = scc_index.get(func_qname, 0)
    outgoing = graph_edges.get(func_qname, [])
    if not outgoing:
        return base
    max_target = max(scc_index.get(t, 0) for t in outgoing)
    return max(base, max_target + 1)


def aggregate_functions_into_nodes(
    modules: Dict[str, ModuleInfo],
    layer_scores: Dict[str, int],
) -> List[Dict[str, Any]]:
    """Aggregate per-function layer scores into per-module node dicts.

    Returns a list of node dicts with keys:
      node_id, primary_file, module, layer, functions, function_count
    """
    nodes: List[Dict[str, Any]] = []
    for mod_name, mod_info in modules.items():
        func_layers = [layer_scores.get(f.qualified_name, 0) for f in mod_info.functions]
        agg_layer = max(func_layers) if func_layers else 0
        nodes.append({
            "node_id": mod_name,
            "primary_file": mod_info.path,
            "module": mod_name,
            "layer": agg_layer,
            "functions": [f.qualified_name for f in mod_info.functions],
            "function_lines": _function_line_index(mod_info.functions),
            "function_count": len(mod_info.functions),
            "language": mod_info.language,
            "source_kind": mod_info.source_kind,
        })
    return nodes


# ---------------------------------------------------------------------------
# PR3 R2: Coverage lookup
# ---------------------------------------------------------------------------

def find_test_coverage(
    project_root: str,
    primary_file: str,
    profile: Optional[Any] = None,
) -> Dict[str, Any]:
    """Find test files and coverage for a given primary source file.

    Returns dict with test_files (list of paths) and covered_lines (int).
    """
    test_files: List[str] = []
    covered_lines = 0

    stem = os.path.splitext(os.path.basename(primary_file))[0].lower()
    primary_ext = os.path.splitext(primary_file)[1].lower()
    rel = os.path.relpath(primary_file, project_root) if os.path.isabs(primary_file) else primary_file
    rel_normalized = rel.replace(os.sep, "/").strip("/")
    while rel_normalized.startswith("./"):
        rel_normalized = rel_normalized[2:]
    rel_without_ext = os.path.splitext(rel_normalized)[0]
    module_token = rel_without_ext.replace("/", ".")
    basename = os.path.basename(primary_file)
    compact_stem = re.sub(r"[^a-z0-9]+", "", stem)
    if profile is None:
        from agent.governance.project_profile import discover_project_profile
        profile = discover_project_profile(project_root)

    def _is_test_like(rel: str, fname: str) -> bool:
        return profile.is_test_path(rel)

    def _matches_primary(fname: str) -> bool:
        lower = fname.lower()
        if primary_ext in DEFAULT_LANGUAGE_POLICY.python_extensions:
            if (
                lower == f"test_{stem}.py"
                or lower.startswith(f"test_{stem}_")
                or lower == f"{stem}_test.py"
            ):
                return True
            file_stem = os.path.splitext(lower)[0]
            compact_file_stem = re.sub(r"[^a-z0-9]+", "", file_stem)
            return bool(
                compact_stem
                and (
                    compact_file_stem.startswith(f"test{compact_stem}")
                    or compact_file_stem.endswith(f"{compact_stem}test")
                )
            )
        suffixes = {primary_ext}
        if primary_ext in {".jsx", ".tsx"}:
            suffixes.add(primary_ext[-3:])
        return any(
            lower in {
                f"{stem}.test{suffix}",
                f"{stem}.spec{suffix}",
                f"test_{stem}{suffix}",
                f"{stem}_test{suffix}",
            }
            or lower.startswith(f"test_{stem}_")
            for suffix in suffixes
            if suffix
        )

    def _content_matches_primary(content: str) -> bool:
        lowered = content.lower()
        tokens = [
            rel_normalized.lower(),
            rel_without_ext.lower(),
            basename.lower(),
            module_token.lower(),
            module_token.replace("_", "").lower(),
            f"from {module_token.lower()} import",
            f"import {module_token.lower()}",
        ]
        if stem and len(stem) >= 8:
            tokens.append(stem)
        return any(token and token in lowered for token in tokens)

    for dirpath, dirnames, filenames in os.walk(project_root):
        kept_dirs = []
        for dirname in dirnames:
            rel_dir = _repo_relpath(project_root, os.path.join(dirpath, dirname))
            if profile.is_excluded_path(rel_dir) or profile.is_doc_path(rel_dir):
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for fname in filenames:
            if not DEFAULT_LANGUAGE_POLICY.is_source_path(fname):
                continue
            fpath = os.path.join(dirpath, fname)
            rel = _repo_relpath(project_root, fpath)
            if profile.is_excluded_path(rel) or profile.is_doc_path(rel):
                continue
            if not _is_test_like(rel, fname):
                continue
            name_match = _matches_primary(fname)
            content_match = False
            content = ""
            test_files.append(fpath)
            try:
                content = _read_file(fpath)
                content_match = _content_matches_primary(content)
            except OSError:
                content = ""
            if not name_match and not content_match:
                test_files.pop()
                continue
            covered_lines += content.count("\n")

    return {"test_files": test_files, "covered_lines": covered_lines}


def find_doc_coverage(
    project_root: str,
    primary_file: str,
    profile: Optional[Any] = None,
) -> Dict[str, Any]:
    """Find doc files referencing a given primary source file.

    Returns dict with doc_files (list of paths) and covered_lines (int).
    """
    doc_files: List[str] = []
    ignored_doc_files: List[str] = []
    covered_lines = 0

    rel = os.path.relpath(primary_file, project_root) if os.path.isabs(primary_file) else primary_file
    rel_normalized = rel.replace(os.sep, "/").strip("/")
    module_token = os.path.splitext(rel_normalized)[0].replace("/", ".")
    basename = os.path.basename(primary_file)
    if profile is None:
        from agent.governance.project_profile import discover_project_profile
        profile = discover_project_profile(project_root)
    skip_rel_prefixes = {
        "docs/dev/scratch/",
        "docs/dev/observer/logs/",
    }

    for dirpath, dirnames, filenames in os.walk(project_root):
        kept_dirs = []
        for dirname in dirnames:
            rel_dir = _repo_relpath(project_root, os.path.join(dirpath, dirname))
            if profile.is_excluded_path(rel_dir) or profile.is_test_path(rel_dir):
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for fname in filenames:
            if not fname.lower().endswith((".md", ".rst", ".txt", ".adoc")):
                continue
            fpath = os.path.join(dirpath, fname)
            doc_rel = _repo_relpath(project_root, fpath)
            if profile.is_excluded_path(doc_rel) or profile.is_test_path(doc_rel):
                ignored_doc_files.append(fpath)
                continue
            if any(doc_rel.startswith(prefix) for prefix in skip_rel_prefixes):
                ignored_doc_files.append(fpath)
                continue
            if _is_git_ignored_path(project_root, doc_rel):
                ignored_doc_files.append(fpath)
                continue
            try:
                content = _read_file(fpath)
                if rel_normalized in content or basename in content or module_token in content:
                    doc_files.append(fpath)
                    covered_lines += content.count("\n")
            except OSError:
                pass

    return {
        "doc_files": doc_files,
        "covered_lines": covered_lines,
        "ignored_doc_files": ignored_doc_files,
    }


_GIT_IGNORED_CACHE: Dict[Tuple[str, str], bool] = {}
_GIT_REPO_CACHE: Dict[str, bool] = {}


def _is_git_repo(project_root: str) -> bool:
    root = os.path.abspath(project_root)
    cached = _GIT_REPO_CACHE.get(root)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(
            ["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        is_repo = proc.returncode == 0 and proc.stdout.strip().lower() == "true"
    except (OSError, subprocess.SubprocessError):
        is_repo = False
    _GIT_REPO_CACHE[root] = is_repo
    return is_repo


def _is_git_ignored_path(project_root: str, rel_path: str) -> bool:
    """Return True when a path will be absent from git-created chain worktrees."""
    normalized = str(rel_path or "").replace("\\", "/").strip("/")
    if not normalized or not _is_git_repo(project_root):
        return False
    key = (os.path.abspath(project_root), normalized)
    cached = _GIT_IGNORED_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(
            ["git", "-C", os.path.abspath(project_root), "check-ignore", "-q", "--", normalized],
            capture_output=True,
            text=True,
            timeout=5,
        )
        ignored = proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ignored = False
    _GIT_IGNORED_CACHE[key] = ignored
    return ignored


# ---------------------------------------------------------------------------
# PR3 R3: Dry-run artifact
# ---------------------------------------------------------------------------

def write_dry_run_artifact(
    project_root: str,
    nodes: List[Dict[str, Any]],
    diff_report: Dict[str, Any],
    scratch_dir: Optional[str] = None,
    feature_clusters: Optional[List[Dict[str, Any]]] = None,
    file_inventory: Optional[List[Dict[str, Any]]] = None,
    file_inventory_summary: Optional[Dict[str, Any]] = None,
    typed_relations: Optional[List[Dict[str, Any]]] = None,
    architecture_graph: Optional[Dict[str, Any]] = None,
    module_dependency_edges: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Write docs/dev/scratch/graph-v2-{date}.json with diff-vs-current report.

    Returns the path to the written file.
    """
    if scratch_dir is None:
        scratch_dir = os.path.join(project_root, "docs", "dev", "scratch")
    os.makedirs(scratch_dir, exist_ok=True)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = os.path.join(scratch_dir, f"graph-v2-{date_str}.json")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "node_count": len(nodes),
        "nodes": nodes,
        "diff_report": diff_report,
        "feature_clusters": feature_clusters or [],
        "file_inventory": file_inventory or [],
        "file_inventory_summary": file_inventory_summary or {},
        "typed_relations": typed_relations or [],
        "architecture_graph": architecture_graph or {},
        "module_dependency_edges": module_dependency_edges or [],
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    return out_path


# ---------------------------------------------------------------------------
# Reconcile feature-cluster synthesis
# ---------------------------------------------------------------------------

def _repo_relpath(project_root: str, path: str) -> str:
    raw = str(path or "")
    try:
        if os.path.isabs(raw):
            raw = os.path.relpath(raw, project_root)
    except ValueError:
        pass
    rel = raw.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.strip("/")


def _module_token_from_relpath(rel_path: str) -> str:
    rel = str(rel_path or "").replace("\\", "/").strip("/")
    if not rel:
        return ""
    token = DEFAULT_LANGUAGE_POLICY.strip_source_suffix(rel)
    if token.endswith("/__init__"):
        token = token[:-9]
    return token.replace("/", ".").strip(".")


def _source_root_aliases(rel_path: str, profile: Any) -> Set[str]:
    rel = str(rel_path or "").replace("\\", "/").strip("/")
    aliases: Set[str] = set()
    for raw_root in getattr(profile, "source_roots", []) or []:
        root = str(raw_root or "").replace("\\", "/").strip("/")
        if root in {"", "."}:
            continue
        prefix = f"{root}/"
        if rel.startswith(prefix):
            alias = _module_token_from_relpath(rel[len(prefix):])
            if alias:
                aliases.add(alias)
    return aliases


def _module_aliases_for_source(
    project_root: str,
    module: ModuleInfo,
    profile: Any,
) -> Set[str]:
    rel = _repo_relpath(project_root, module.path)
    aliases = {
        str(module.module_name or "").strip("."),
        _module_token_from_relpath(rel),
    }
    aliases.update(_source_root_aliases(rel, profile))
    return {alias for alias in aliases if alias}


def _production_module_alias_index(
    project_root: str,
    modules: Dict[str, ModuleInfo],
    profile: Any,
) -> Dict[str, Set[str]]:
    alias_to_modules: Dict[str, Set[str]] = {}
    for module_name, module in sorted((modules or {}).items()):
        for alias in _module_aliases_for_source(project_root, module, profile):
            alias_to_modules.setdefault(alias, set()).add(module_name)
    return alias_to_modules


def _resolve_import_token_to_unique_module(
    token: str,
    alias_to_modules: Dict[str, Set[str]],
) -> str:
    normalized = str(token or "").replace("\\", "/").strip().strip(".")
    if not normalized:
        return ""
    normalized = DEFAULT_LANGUAGE_POLICY.strip_source_suffix(normalized).replace("/", ".").strip(".")
    while normalized:
        module_names = alias_to_modules.get(normalized)
        if module_names:
            if len(module_names) == 1:
                return next(iter(module_names))
            return ""
        if "." not in normalized:
            return ""
        normalized = normalized.rsplit(".", 1)[0]
    return ""


def _iter_test_source_files(project_root: str, profile: Any) -> List[Tuple[str, str]]:
    files: List[Tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(project_root):
        kept_dirs = []
        for dirname in dirnames:
            rel_dir = _repo_relpath(project_root, os.path.join(dirpath, dirname))
            if profile.is_excluded_path(rel_dir) or profile.is_doc_path(rel_dir):
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for fname in filenames:
            if not DEFAULT_LANGUAGE_POLICY.is_source_path(fname):
                continue
            fpath = os.path.join(dirpath, fname)
            rel = _repo_relpath(project_root, fpath)
            if profile.is_excluded_path(rel) or profile.is_doc_path(rel):
                continue
            if profile.is_test_path(rel):
                files.append((rel, fpath))
    return files


def _test_import_tokens(project_root: str, test_file: str, source: str) -> Set[str]:
    adapter = _adapter_for_source_file(test_file)
    imports = _safe_adapter_imports(adapter, test_file, source)
    tokens: Set[str] = set()
    for row in imports:
        imported = str(row.get("imported") or "").strip()
        specifier = str(row.get("specifier") or "").strip()
        if imported:
            tokens.add(_resolve_adapter_import_row(project_root, test_file, row, imported))
        if specifier:
            tokens.add(specifier)
            tokens.add(_resolve_source_import(project_root, test_file, specifier))
    return {token for token in tokens if token}


def _pytest_fixture_names(source: str, file_path: str = "<test>") -> Set[str]:
    try:
        tree = ast.parse(source or "", filename=file_path)
    except (SyntaxError, ValueError):
        return set()
    names: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in getattr(node, "decorator_list", []) or []:
            dec_name = _decorator_name(decorator)
            if dec_name in {"pytest.fixture", "fixture"} or dec_name.endswith(".fixture"):
                names.add(node.name)
                break
    return names


def _decorator_name(dec: Any) -> str:
    if isinstance(dec, ast.Name):
        return dec.id
    if isinstance(dec, ast.Attribute):
        parent = _decorator_name(dec.value)
        return f"{parent}.{dec.attr}" if parent else dec.attr
    if isinstance(dec, ast.Call):
        return _decorator_name(dec.func)
    return ""


def _pytest_test_fixture_args(source: str, file_path: str = "<test>") -> Set[str]:
    try:
        tree = ast.parse(source or "", filename=file_path)
    except (SyntaxError, ValueError):
        return set()
    names: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        for arg in getattr(node.args, "args", []) or []:
            if arg.arg not in {"self", "cls"}:
                names.add(arg.arg)
    return names


def _pytest_fixture_fanin_index(
    project_root: str,
    profile: Any,
    alias_to_modules: Dict[str, Set[str]],
) -> Dict[str, List[Dict[str, Any]]]:
    fixtures: Dict[str, List[Dict[str, Any]]] = {}
    for rel, fpath in _iter_test_source_files(project_root, profile):
        if os.path.basename(rel) != "conftest.py":
            continue
        try:
            source = _read_file(fpath)
        except (UnicodeDecodeError, OSError):
            continue
        fixture_names = _pytest_fixture_names(source, fpath)
        if not fixture_names:
            continue
        module_hits: Dict[str, Set[str]] = {}
        for token in _test_import_tokens(project_root, fpath, source):
            module_name = _resolve_import_token_to_unique_module(token, alias_to_modules)
            if module_name:
                module_hits.setdefault(module_name, set()).add(token)
        if not module_hits:
            continue
        for fixture_name in sorted(fixture_names):
            for module_name, tokens in sorted(module_hits.items()):
                fixtures.setdefault(fixture_name, []).append({
                    "module_name": module_name,
                    "fixture": fixture_name,
                    "fixture_path": fpath,
                    "fixture_rel_path": rel,
                    "imports": sorted(tokens),
                })
    return fixtures


def build_test_consumer_fanin_index(
    project_root: str,
    modules: Dict[str, ModuleInfo],
    profile: Optional[Any] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Map production modules to tests that import them as downstream consumers."""
    if profile is None:
        from agent.governance.project_profile import discover_project_profile
        profile = discover_project_profile(project_root)
    alias_to_modules = _production_module_alias_index(project_root, modules, profile)
    if not alias_to_modules:
        return {}
    fixture_index = _pytest_fixture_fanin_index(project_root, profile, alias_to_modules)

    index: Dict[str, List[Dict[str, Any]]] = {}
    for rel, fpath in _iter_test_source_files(project_root, profile):
        try:
            source = _read_file(fpath)
        except (UnicodeDecodeError, OSError):
            continue
        module_hits: Dict[str, Set[str]] = {}
        for token in _test_import_tokens(project_root, fpath, source):
            module_name = _resolve_import_token_to_unique_module(token, alias_to_modules)
            if module_name:
                module_hits.setdefault(module_name, set()).add(token)
        for module_name, tokens in sorted(module_hits.items()):
            index.setdefault(module_name, []).append({
                "path": fpath,
                "rel_path": rel,
                "evidence": "test_import_fanin",
                "imports": sorted(tokens),
                "covered_lines": source.count("\n"),
            })
        for fixture_name in sorted(_pytest_test_fixture_args(source, fpath)):
            for fixture_hit in fixture_index.get(fixture_name) or []:
                module_name = str(fixture_hit.get("module_name") or "")
                if not module_name:
                    continue
                imports = list(fixture_hit.get("imports") or [])
                imports.append(f"pytest_fixture:{fixture_name}")
                index.setdefault(module_name, []).append({
                    "path": fpath,
                    "rel_path": rel,
                    "evidence": "pytest_fixture_consumer_fanin",
                    "imports": sorted(set(imports)),
                    "covered_lines": source.count("\n"),
                    "fixture": fixture_name,
                    "fixture_path": fixture_hit.get("fixture_rel_path") or fixture_hit.get("fixture_path"),
                })
    for entries in index.values():
        entries.sort(key=lambda item: str(item.get("rel_path") or item.get("path") or ""))
    return index


def _merge_test_consumer_fanin(
    project_root: str,
    coverage: Dict[str, Any],
    entries: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if not entries:
        return coverage
    merged = dict(coverage or {})
    files = list(merged.get("test_files") or [])
    seen = {_repo_relpath(project_root, path) for path in files}
    covered_lines = int(merged.get("covered_lines") or 0)
    fan_in_evidence = list(merged.get("fan_in_evidence") or [])
    for entry in entries:
        path = str(entry.get("path") or "")
        rel = _repo_relpath(project_root, entry.get("rel_path") or path)
        if path and rel not in seen:
            files.append(path)
            seen.add(rel)
            covered_lines += int(entry.get("covered_lines") or 0)
        fan_in_evidence.append({
            "path": rel,
            "evidence": entry.get("evidence") or "test_import_fanin",
            "imports": list(entry.get("imports") or []),
        })
    merged["test_files"] = files
    merged["covered_lines"] = covered_lines
    merged["fan_in_evidence"] = fan_in_evidence
    return merged


def _attach_test_consumer_fanin_to_nodes(
    project_root: str,
    nodes: List[Dict[str, Any]],
    fanin_index: Dict[str, List[Dict[str, Any]]],
) -> None:
    if not fanin_index:
        return
    for node in nodes:
        module_name = str(node.get("module") or node.get("node_id") or "")
        entries = fanin_index.get(module_name) or []
        if entries:
            node["test_coverage"] = _merge_test_consumer_fanin(
                project_root,
                node.get("test_coverage") or {"test_files": [], "covered_lines": 0},
                entries,
            )


def _module_from_qname(qname: str) -> str:
    return qname.split("::", 1)[0] if "::" in qname else ""


def _package_key(path: str) -> str:
    """Return a generic file-tree bucket key for bounded batch coalescing."""
    normal = str(path or "").replace("\\", "/").strip("/")
    if not normal:
        return ""
    parent = os.path.dirname(normal)
    return parent or normal


def _cluster_fingerprint(entries: List[str], primary_files: List[str]) -> str:
    payload = "|".join(sorted(entries)) + "||" + "|".join(sorted(primary_files))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _cluster_feature_hash(cluster_payload: Dict[str, Any]) -> str:
    payload = {
        "entries": sorted(cluster_payload.get("entries") or []),
        "primary_files": sorted(cluster_payload.get("primary_files") or []),
        "secondary_files": sorted(cluster_payload.get("secondary_files") or []),
        "functions": sorted(cluster_payload.get("functions") or []),
        "modules": sorted(cluster_payload.get("modules") or []),
        "synthesis": cluster_payload.get("synthesis") or {},
    }
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _cluster_file_cap() -> int:
    try:
        from agent.governance.reconcile_config import RECONCILE_FEATURE_CLUSTER_FILE_CAP
        return max(1, int(RECONCILE_FEATURE_CLUSTER_FILE_CAP))
    except Exception:
        return 6


# ---------------------------------------------------------------------------
# Architecture profile bootstrap: typed relation extraction
# ---------------------------------------------------------------------------

_SQL_CREATE_RE = re.compile(
    r"\bCREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+([A-Za-z_][\w]*)",
    re.IGNORECASE,
)
_SQL_INSERT_RE = re.compile(r"\bINSERT(?:\s+OR\s+\w+)?\s+INTO\s+([A-Za-z_][\w]*)", re.IGNORECASE)
_SQL_UPDATE_RE = re.compile(r"\bUPDATE\s+([A-Za-z_][\w]*)", re.IGNORECASE)
_SQL_DELETE_RE = re.compile(r"\bDELETE\s+FROM\s+([A-Za-z_][\w]*)", re.IGNORECASE)
_SQL_READ_RE = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w]*)", re.IGNORECASE)
_EVENT_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)+$", re.IGNORECASE)
_ARTIFACT_SUFFIXES = (
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".db",
    ".sqlite", ".sqlite3", ".lock", ".tar.gz", ".md",
)
_STATE_NAME_TOKENS = (
    "db", "database", "state", "store", "repository", "repo", "model",
    "schema", "migration", "queue", "memory", "session", "checkpoint",
    "registry",
)
_ORCHESTRATION_NAME_TOKENS = (
    "chain", "orchestrator", "workflow", "worker", "scheduler", "cron",
    "queue", "bridge", "server", "gateway", "runner", "executor", "manager",
    "deploy", "dispatch",
)
_CONTRACT_NAME_TOKENS = (
    "api", "route", "schema", "contract", "interface", "adapter", "plugin",
    "profile", "validator", "output_schemas",
)
_CONFIG_NAME_TOKENS = ("config", "settings", "permission", "role")
_VALIDATION_NAME_TOKENS = (
    "validator", "validation", "preflight", "check", "policy", "rules",
    "gate", "gatekeeper", "permission",
)
_GRAPH_TOOL_NAME_TOKENS = (
    "graph", "impact", "symbol", "topology", "cluster", "node",
    "dependency", "mapping",
)
_DOC_TOOL_NAME_TOKENS = ("doc", "docs", "documentation", "readme")
_AUDIT_NAME_TOKENS = ("audit", "evidence", "observability", "trace")
_GATEWAY_NAME_TOKENS = ("api", "server", "gateway", "mcp", "route", "http")


def _string_constants(source: str) -> List[str]:
    try:
        tree = ast.parse(source or "")
    except (SyntaxError, ValueError):
        return []
    out: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.strip()
            if value:
                out.append(value)
    return out


def _sql_string_constants(source: str) -> List[str]:
    try:
        tree = ast.parse(source or "")
    except (SyntaxError, ValueError):
        return []
    out: List[str] = []

    def add_if_sql(value: Any) -> None:
        if not isinstance(value, str):
            return
        if re.search(
            r"\b(CREATE\s+TABLE|SELECT|INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)\b",
            value,
            re.IGNORECASE,
        ):
            out.append(value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_name = ""
            if isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
            elif isinstance(node.func, ast.Name):
                func_name = node.func.id
            if func_name in {"execute", "executemany", "executescript"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant):
                    add_if_sql(first.value)
        elif isinstance(node, ast.Assign):
            target_names = []
            for target in node.targets:
                if isinstance(target, ast.Name):
                    target_names.append(target.id.lower())
            if any(("schema" in name or "sql" in name) for name in target_names):
                if isinstance(node.value, ast.Constant):
                    add_if_sql(node.value.value)
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and ("schema" in target.id.lower() or "sql" in target.id.lower()):
                if isinstance(node.value, ast.Constant):
                    add_if_sql(node.value.value)
    return out


def _route_relations(module: ModuleInfo, project_root: str) -> List[TypedRelation]:
    try:
        tree = ast.parse(module.source or "", filename=module.path)
    except (SyntaxError, ValueError):
        return []
    out: List[TypedRelation] = []
    rel_file = _repo_relpath(project_root, module.path)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            func = dec.func if isinstance(dec, ast.Call) else dec
            name = _call_target_name(ast.Call(func=func, args=[], keywords=[])) if isinstance(func, ast.Attribute) else ""
            if isinstance(func, ast.Name):
                name = func.id
            if not name:
                continue
            name_lower = name.lower()
            if not any(token in name_lower for token in ("route", "get", "post", "put", "delete", "patch", "command")):
                continue
            args = dec.args if isinstance(dec, ast.Call) else []
            path_arg = ""
            method_arg = name.upper() if name_lower in {"get", "post", "put", "delete", "patch"} else ""
            if args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
                if name_lower == "route" and len(args) >= 2:
                    method_arg = str(args[0].value).upper()
                    if isinstance(args[1], ast.Constant) and isinstance(args[1].value, str):
                        path_arg = str(args[1].value)
                else:
                    path_arg = str(args[0].value)
            target = " ".join(part for part in (method_arg, path_arg) if part).strip() or name
            out.append(TypedRelation(
                source_module=module.module_name,
                source_file=rel_file,
                relation_type="http_route",
                target=target,
                target_kind="interface",
                evidence=f"{node.name}@{name}",
            ))
    return out


def _looks_like_artifact(value: str) -> bool:
    if not value or len(value) > 180:
        return False
    if any(ch.isspace() for ch in value):
        return False
    if value.startswith("*"):
        return False
    lower = value.lower()
    if lower in set(_ARTIFACT_SUFFIXES):
        return False
    if any(lower.endswith(suffix) for suffix in _ARTIFACT_SUFFIXES):
        return True
    if "/" in value or "\\" in value:
        return any(suffix in lower for suffix in _ARTIFACT_SUFFIXES)
    return False


def _governed_artifact_literal(project_root: str, value: str, profile: Optional[Any] = None) -> bool:
    rel = DEFAULT_LANGUAGE_POLICY.normalize_relpath(project_root, value)
    if not rel:
        return False
    if profile is not None and profile.is_excluded_path(rel):
        return False
    if DEFAULT_LANGUAGE_POLICY.is_excluded_path(rel):
        return False
    if _is_git_ignored_path(project_root, rel):
        return False
    return True


def _artifact_relation_type(source: str) -> str:
    lowered = (source or "").lower()
    write_tokens = ("write_text", "write_bytes", "json.dump", "open(", "'w'", '"w"', "os.replace", "shutil.copy")
    if any(token in lowered for token in write_tokens):
        return "writes_artifact"
    return "reads_artifact"


def _module_name_tokens(module_name: str) -> Set[str]:
    raw = module_name.replace("-", "_").replace("/", ".")
    parts = []
    for chunk in raw.split("."):
        parts.extend(p for p in chunk.split("_") if p)
    return {p.lower() for p in parts if p}


def extract_typed_relations(
    project_root: str,
    modules: Dict[str, ModuleInfo],
    *,
    graph_enrich_config_rules: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Extract typed state/workflow/contract/artifact edges from modules.

    This is intentionally deterministic.  AI can later review ambiguous labels,
    but the scanner first records evidence that a generic PM can audit.
    """
    sql_by_module: Dict[str, str] = {}
    created_tables: Set[str] = set()
    try:
        from agent.governance.project_profile import discover_project_profile
        profile = discover_project_profile(project_root)
    except Exception:
        profile = None
    for module in modules.values():
        blob = "\n".join(_sql_string_constants(module.source or ""))
        sql_by_module[module.module_name] = blob
        created_tables.update(_SQL_CREATE_RE.findall(blob))
    created_tables.add("sqlite_master")

    relations: List[TypedRelation] = []
    for module in modules.values():
        source = module.source or ""
        rel_file = _repo_relpath(project_root, module.path)
        constants = _string_constants(source)
        sql_blob = sql_by_module.get(module.module_name, "")

        for table in sorted(set(_SQL_CREATE_RE.findall(sql_blob))):
            relations.append(TypedRelation(module.module_name, "owns_state", table, "db_table",
                                           "CREATE TABLE", rel_file))
        for table in sorted(set(_SQL_INSERT_RE.findall(sql_blob) + _SQL_UPDATE_RE.findall(sql_blob) + _SQL_DELETE_RE.findall(sql_blob))):
            if table not in created_tables:
                continue
            relations.append(TypedRelation(module.module_name, "writes_state", table, "db_table",
                                           "SQL write", rel_file))
        for table in sorted(set(_SQL_READ_RE.findall(sql_blob))):
            if table not in created_tables:
                continue
            relations.append(TypedRelation(module.module_name, "reads_state", table, "db_table",
                                           "SQL read", rel_file))

        for value in sorted(set(constants)):
            if _looks_like_artifact(value) and _governed_artifact_literal(project_root, value, profile):
                relations.append(TypedRelation(
                    module.module_name,
                    _artifact_relation_type(source),
                    value.replace("\\", "/"),
                    "artifact",
                    "string literal",
                    rel_file,
                ))
                continue
            if _EVENT_TOKEN_RE.match(value) and not value.startswith(("http.", "https.")):
                if re.match(r"^L\d+\.\d+$", value):
                    continue
                lower = source.lower()
                if "chain_events" in lower or "event_type" in lower or "emit" in lower:
                    rel_type = "emits_event" if ("insert into chain_events" in lower or "persist_event" in lower or "emit" in lower) else "consumes_event"
                    relation = TypedRelation(module.module_name, rel_type, value, "event",
                                             "event literal", rel_file)
                    relation = _apply_graph_enrich_config_rule_to_typed_relation(
                        relation,
                        module=module,
                        rules=graph_enrich_config_rules,
                    )
                    if relation is not None:
                        relations.append(relation)

        lowered = source.lower()
        if "/api/task" in lowered or "create_task" in lowered or "task_create" in lowered:
            relations.append(TypedRelation(module.module_name, "creates_task", "governance_task",
                                           "task", "task creation", rel_file))
        if "operation_type" in lowered or "cluster_fingerprint" in lowered:
            relations.append(TypedRelation(module.module_name, "uses_task_metadata",
                                           "task_metadata", "task_metadata",
                                           "metadata contract", rel_file))
        relations.extend(_route_relations(module, project_root))
        for adapter_rel in module.adapter_relations or []:
            relation_type = str(adapter_rel.get("relation_type") or "")
            target = str(adapter_rel.get("target") or "")
            target_kind = str(adapter_rel.get("target_kind") or "")
            if not relation_type or not target or not target_kind:
                continue
            relations.append(TypedRelation(
                module.module_name,
                relation_type,
                target,
                target_kind,
                str(adapter_rel.get("evidence") or "adapter relation"),
                rel_file,
            ))

    return [r.__dict__ for r in _dedupe_typed_relations(relations)]


def _apply_graph_enrich_config_rule_to_typed_relation(
    relation: TypedRelation,
    *,
    module: ModuleInfo,
    rules: Optional[Dict[str, Any]] = None,
) -> Optional[TypedRelation]:
    decision = _graph_enrich_config_rule_decision_for_typed_relation(
        relation,
        module=module,
        rules=rules,
    )
    if _graph_enrich_config_rule_suppresses(decision):
        return None
    downgrade_to = str(decision.get("downgrade_to") or "")
    if decision.get("matched") and downgrade_to and downgrade_to != relation.relation_type:
        return TypedRelation(
            source_module=relation.source_module,
            relation_type=downgrade_to,
            target=relation.target,
            target_kind=relation.target_kind,
            evidence=relation.evidence,
            source_file=relation.source_file,
        )
    return relation


def _graph_enrich_config_rule_decision_for_typed_relation(
    relation: TypedRelation,
    *,
    module: ModuleInfo,
    rules: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not rules:
        return {"matched": False, "rule_id": "", "action": "", "downgrade_to": "", "errors": []}
    source_evidence = _typed_relation_source_evidence(relation)
    if not source_evidence:
        return {"matched": False, "rule_id": "", "action": "", "downgrade_to": "", "errors": []}
    context = {
        "edge": relation.relation_type,
        "source_evidence": source_evidence,
        "language": str((module.language if module else "") or ""),
        "source_path": str(relation.source_file or (module.path if module else "") or ""),
        "raw_target": str(relation.target or ""),
        "target_kind": str(relation.target_kind or ""),
    }
    try:
        from agent.governance.graph_enrich_config_ops import evaluate_graph_enrich_config_rules

        return evaluate_graph_enrich_config_rules(rules, context)
    except Exception:
        return {"matched": False, "rule_id": "", "action": "", "downgrade_to": "", "errors": ["rule_eval_failed"]}


def _typed_relation_source_evidence(relation: TypedRelation) -> str:
    evidence = str(relation.evidence or "").strip().lower().replace(" ", "_")
    if evidence in {"event_literal", "string_literal"}:
        return "string_literal"
    return evidence


def _dedupe_typed_relations(relations: List[TypedRelation]) -> List[TypedRelation]:
    seen: Set[Tuple[str, str, str, str]] = set()
    out: List[TypedRelation] = []
    for rel in relations:
        key = (rel.source_module, rel.relation_type, rel.target_kind, rel.target)
        if key in seen:
            continue
        seen.add(key)
        out.append(rel)
    out.sort(key=lambda r: (r.source_module, r.relation_type, r.target_kind, r.target))
    return out


def _relations_by_module(typed_relations: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for rel in typed_relations or []:
        out.setdefault(str(rel.get("source_module") or ""), []).append(rel)
    return out


def _config_relation_owner(path: str, known_modules: Set[str]) -> Tuple[str, str, str]:
    """Return deterministic config ownership as (module, relation, evidence)."""
    rel = str(path or "").replace("\\", "/").strip("/")
    lower = rel.lower()
    name = lower.rsplit("/", 1)[-1]

    def has(module_name: str) -> bool:
        return module_name in known_modules

    if lower.startswith("config/roles/") and lower.endswith((".yaml", ".yml")):
        if has("agent.governance.role_config"):
            role = Path(rel).stem
            return (
                "agent.governance.role_config",
                "configures_role",
                f"role config role={role}",
            )
    if lower in {
        "config/reconcile/semantic_enrichment.yaml",
        "config/reconcile/semantic_enrichment.yml",
    }:
        if has("agent.governance.reconcile_semantic_config"):
            return (
                "agent.governance.reconcile_semantic_config",
                "configures_analyzer",
                "reconcile semantic analyzer config",
            )
    if name in {"pipeline_config.yaml", "pipeline_config.yml", "pipeline_config.json", "pipeline_config.yaml.example"}:
        if has("agent.pipeline_config"):
            return (
                "agent.pipeline_config",
                "configures_model_routing",
                "pipeline provider/model routing config",
            )
    if name in {"agent_config.json", ".aming-claw.yaml", ".aming-claw.yml"}:
        for module_name in ("agent.project_config", "agent.governance.external_project_governance"):
            if has(module_name):
                return (
                    module_name,
                    "configures_runtime",
                    "project runtime governance config",
                )
    if name == ".mcp.json" and has("agent.governance.mcp_server"):
        return (
            "agent.governance.mcp_server",
            "configures_runtime",
            "MCP server config",
        )
    return "", "", ""


def materialize_config_file_relations(
    project_root: str,
    nodes: List[Dict[str, Any]],
    file_inventory: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Attach deterministic config files to owning modules and typed relations.

    Config is a first-class governance input like docs/tests: it should not
    drive DFS traversal, but graph readers need to know which runtime behavior
    a config file controls and which code loads it.
    """
    known_modules = {
        str(node.get("module") or node.get("node_id") or "")
        for node in nodes or []
        if str(node.get("module") or node.get("node_id") or "")
    }
    nodes_by_module = {
        str(node.get("module") or node.get("node_id") or ""): node
        for node in nodes or []
        if str(node.get("module") or node.get("node_id") or "")
    }
    relations: List[Dict[str, Any]] = []
    for row in file_inventory or []:
        path = _repo_relpath(project_root, str(row.get("path") or ""))
        if not path:
            continue
        kind = str(row.get("file_kind") or "")
        owner, relation_type, evidence = _config_relation_owner(path, known_modules)
        if not owner and kind != "config":
            continue
        if not owner:
            continue
        node = nodes_by_module.get(owner)
        if node is not None:
            config_files = node.setdefault("config_files", [])
            if path not in config_files:
                config_files.append(path)
                config_files.sort()
        row["scan_status"] = "config_attached"
        row["graph_status"] = "attached"
        row["decision"] = "attach_to_node"
        row["candidate_node_id"] = owner
        row["attached_to"] = owner
        row["mapped_node_ids"] = [owner]
        row["reason"] = "deterministically attached as config governance input"
        relations.append(TypedRelation(
            source_module=owner,
            relation_type=relation_type,
            target=path,
            target_kind="config",
            evidence=evidence,
            source_file=path,
        ).__dict__)
    return relations


def build_module_dependency_edges(
    modules: Dict[str, ModuleInfo],
    call_graph: CallGraph,
) -> List[Dict[str, Any]]:
    """Collapse DFS/import facts into module-level dependency edges.

    Edge direction follows deps_graph semantics: dependency -> dependent.
    If module A calls/imports module B, B is a prerequisite for A, so the
    emitted edge is B -> A.  This keeps peer edges usable by impact analysis.
    """
    known_modules = set(modules)
    seen: Set[Tuple[str, str, str, str]] = set()
    out: List[Dict[str, Any]] = []

    def add_edge(source_module: str, target_module: str, relation_type: str, evidence: str) -> None:
        if not source_module or not target_module or source_module == target_module:
            return
        if source_module not in known_modules or target_module not in known_modules:
            return
        key = (source_module, target_module, relation_type, evidence)
        if key in seen:
            return
        seen.add(key)
        out.append({
            "source_module": source_module,
            "target_module": target_module,
            "relation_type": relation_type,
            "direction": "dependency_to_dependent",
            "evidence": evidence,
        })

    for caller, targets in sorted((call_graph.edges or {}).items()):
        caller_module = _module_from_qname(caller)
        for target in sorted(set(targets or [])):
            target_module = _module_from_qname(target)
            add_edge(
                target_module,
                caller_module,
                "calls_module",
                f"{caller} calls {target}",
            )

    for module_name, module in sorted(modules.items()):
        for alias, imported in sorted((module.import_map or {}).items()):
            imported_module = imported
            while imported_module and imported_module not in known_modules and "." in imported_module:
                imported_module = imported_module.rsplit(".", 1)[0]
            add_edge(
                imported_module,
                module_name,
                "imports_module",
                f"{module_name} imports {alias} -> {imported}",
            )

    out.sort(key=lambda item: (
        item["source_module"],
        item["target_module"],
        item["relation_type"],
        item["evidence"],
    ))
    return out


def _function_short_name(qname: str) -> str:
    return str(qname or "").rsplit("::", 1)[-1]


def _function_line_range(func: FunctionMeta | None) -> List[int]:
    if not func:
        return [0, 0]
    start = int(func.lineno or 0)
    end = int(func.end_lineno or start or 0)
    return [start, end]


def build_function_call_facts(
    modules: Dict[str, ModuleInfo],
    call_graph: CallGraph,
    *,
    graph_enrich_config_rules: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build per-module function call facts for graph metadata/query tools.

    Module-level `depends_on` remains the structural graph edge. These facts are
    an additive detail layer for AI inspection, function jump, and local impact
    reasoning.
    """
    facts: Dict[str, Dict[str, Any]] = {
        module_name: {"calls": [], "called_by": [], "weak_calls": []}
        for module_name in modules
    }
    module_paths = {module_name: module.path for module_name, module in modules.items()}

    def append_unique(bucket: List[Dict[str, Any]], item: Dict[str, Any]) -> None:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if all(json.dumps(existing, sort_keys=True, ensure_ascii=False) != key for existing in bucket):
            bucket.append(item)

    for caller, targets in sorted((call_graph.edges or {}).items()):
        caller_func = call_graph.all_functions.get(caller)
        caller_module = _module_from_qname(caller)
        if caller_module not in facts:
            continue
        for target in sorted(set(targets or [])):
            target_func = call_graph.all_functions.get(target)
            target_module = _module_from_qname(target)
            if target_module not in facts:
                continue
            item = {
                "caller": caller,
                "caller_short": _function_short_name(caller),
                "caller_module": caller_module,
                "caller_file": module_paths.get(caller_module, ""),
                "caller_line": _function_line_range(caller_func),
                "callee": target,
                "callee_short": _function_short_name(target),
                "callee_module": target_module,
                "callee_file": module_paths.get(target_module, ""),
                "callee_line": _function_line_range(target_func),
                "confidence": "strong",
                "resolution": "resolved",
            }
            append_unique(facts[caller_module]["calls"], item)
            append_unique(facts[target_module]["called_by"], item)

    for weak in sorted(call_graph.weak_edges or [], key=lambda item: (item.caller, item.target)):
        caller_module = _module_from_qname(weak.caller)
        if caller_module not in facts:
            continue
        caller_func = call_graph.all_functions.get(weak.caller)
        rule_decision = _graph_enrich_config_rule_decision_for_weak_call(
            modules,
            weak,
            rules=graph_enrich_config_rules,
        )
        if _graph_enrich_config_rule_suppresses(rule_decision):
            continue
        item = {
            "caller": weak.caller,
            "caller_short": _function_short_name(weak.caller),
            "caller_module": caller_module,
            "caller_file": module_paths.get(caller_module, ""),
            "caller_line": _function_line_range(caller_func),
            "raw_target": str((weak.context or {}).get("raw_target") or weak.target),
            "candidates": list(weak.candidates or []),
            "confidence": "weak",
            "resolution": "ambiguous",
            "reason": weak.reason,
        }
        if isinstance(weak.context, dict):
            for key in ("call_syntax", "receiver_kind"):
                if weak.context.get(key):
                    item[key] = weak.context[key]
        if rule_decision.get("matched"):
            item["graph_enrich_config_rule"] = {
                "rule_id": rule_decision.get("rule_id", ""),
                "action": rule_decision.get("action", ""),
                "downgrade_to": rule_decision.get("downgrade_to", ""),
                "matched_predicates": rule_decision.get("matched_predicates", []),
            }
        append_unique(facts[caller_module]["weak_calls"], item)

    for bucket in facts.values():
        bucket["calls"].sort(key=lambda item: (item.get("caller", ""), item.get("callee", "")))
        bucket["called_by"].sort(key=lambda item: (item.get("callee", ""), item.get("caller", "")))
        bucket["weak_calls"].sort(key=lambda item: (item.get("caller", ""), item.get("raw_target", "")))
    return facts


def _load_graph_enrich_config_rules(project_root: str | Path) -> Dict[str, Any]:
    try:
        from agent.governance.reconcile_semantic_config import load_semantic_enrichment_config

        config = load_semantic_enrichment_config(project_root=project_root)
        return dict(config.graph_enrich_config_ops.rules or {})
    except Exception:
        return {}


def _graph_enrich_config_rule_decision_for_weak_call(
    modules: Dict[str, ModuleInfo],
    weak: WeakEdge,
    *,
    rules: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not rules:
        return {"matched": False, "rule_id": "", "action": "", "downgrade_to": "", "errors": []}
    caller_module = _module_from_qname(weak.caller)
    module = modules.get(caller_module)
    context = {
        "edge": "calls",
        "source_evidence": "weak_call_resolver_ambiguous_short_name",
        "language": str((module.language if module else "") or ""),
        "source_path": str((module.path if module else "") or ""),
        "raw_target": str((weak.context or {}).get("raw_target") or weak.target),
    }
    if isinstance(weak.context, dict):
        for key in ("call_syntax", "receiver_kind"):
            if weak.context.get(key):
                context[key] = weak.context[key]
    try:
        from agent.governance.graph_enrich_config_ops import evaluate_graph_enrich_config_rules

        return evaluate_graph_enrich_config_rules(rules, context)
    except Exception:
        return {"matched": False, "rule_id": "", "action": "", "downgrade_to": "", "errors": ["rule_eval_failed"]}


def _graph_enrich_config_rule_suppresses(decision: Dict[str, Any]) -> bool:
    if not decision.get("matched"):
        return False
    action = str(decision.get("action") or "")
    downgrade_to = str(decision.get("downgrade_to") or "")
    return action in {"drop", "ignore", "reject"} or downgrade_to in {"drop", "ignore"}


def enrich_nodes_with_function_call_facts(
    nodes: List[Dict[str, Any]],
    function_call_facts: Dict[str, Dict[str, Any]],
) -> None:
    for node in nodes:
        module_name = str(node.get("module") or node.get("node_id") or "")
        facts = function_call_facts.get(module_name) or {}
        calls = list(facts.get("calls") or [])
        called_by = list(facts.get("called_by") or [])
        weak_calls = list(facts.get("weak_calls") or [])
        node["function_calls"] = calls
        node["function_called_by"] = called_by
        node["function_weak_calls"] = weak_calls
        node["function_call_count"] = len(calls)
        node["function_called_by_count"] = len(called_by)
        node["function_weak_call_count"] = len(weak_calls)


def _score_architecture_signals(module_name: str, rels: List[Dict[str, Any]]) -> Dict[str, Any]:
    tokens = _module_name_tokens(module_name)
    rel_types = {str(rel.get("relation_type") or "") for rel in rels}
    target_kinds = {str(rel.get("target_kind") or "") for rel in rels}
    persistence = 0.0
    orchestration = 0.0
    domain_contract = 0.0

    if rel_types & {"owns_state"}:
        persistence += 3.0
    if rel_types & {"reads_state", "writes_state"}:
        persistence += 1.5
    if rel_types & {"reads_artifact", "writes_artifact"}:
        persistence += 0.75
    if tokens & set(_STATE_NAME_TOKENS):
        persistence += 1.0

    if rel_types & {"creates_task", "emits_event", "consumes_event", "http_route"}:
        orchestration += 2.0
    if tokens & set(_ORCHESTRATION_NAME_TOKENS):
        orchestration += 1.0

    if rel_types & {"http_route", "uses_task_metadata"}:
        domain_contract += 1.5
    if rel_types & {"configures_role", "configures_analyzer", "configures_model_routing", "configures_runtime"}:
        domain_contract += 1.25
    if tokens & set(_CONTRACT_NAME_TOKENS):
        domain_contract += 1.0

    roles = []
    if rel_types & {"owns_state"} or persistence >= 3.0:
        roles.append("state")
    elif persistence >= 1.0:
        roles.append("state_consumer")
    if orchestration >= 2.0:
        roles.append("orchestration")
    if domain_contract >= 1.5:
        roles.append("domain_contract")
    elif domain_contract >= 1.0 and (tokens & set(_CONTRACT_NAME_TOKENS)):
        roles.append("domain_contract")
    if rel_types & {"http_route"} or tokens & set(_GATEWAY_NAME_TOKENS):
        roles.append("gateway_entry")
    if tokens & set(_VALIDATION_NAME_TOKENS):
        roles.append("validation")
    if tokens & set(_GRAPH_TOOL_NAME_TOKENS):
        roles.append("graph_tooling")
    if tokens & set(_DOC_TOOL_NAME_TOKENS):
        roles.append("documentation")
    if tokens & set(_AUDIT_NAME_TOKENS):
        roles.append("audit_evidence")
    if target_kinds & {"task", "task_metadata", "event"} and "orchestration" not in roles:
        roles.append("orchestration")
    if rel_types & {"configures_role", "configures_analyzer", "configures_model_routing", "configures_runtime"} or tokens & set(_CONFIG_NAME_TOKENS):
        roles.append("configuration")
    if not roles:
        roles.append("implementation")

    return {
        "persistence_weight": round(persistence, 2),
        "orchestration_weight": round(orchestration, 2),
        "domain_contract_weight": round(domain_contract, 2),
        "roles": list(dict.fromkeys(roles)),
        "relation_counts": {
            rel_type: sum(1 for rel in rels if rel.get("relation_type") == rel_type)
            for rel_type in sorted(rel_types)
        },
    }


def enrich_nodes_with_architecture_signals(
    nodes: List[Dict[str, Any]],
    typed_relations: List[Dict[str, Any]],
) -> None:
    by_module = _relations_by_module(typed_relations)
    for node in nodes:
        module_name = str(node.get("module") or node.get("node_id") or "")
        rels = by_module.get(module_name, [])
        node["architecture_signals"] = _score_architecture_signals(module_name, rels)
        node["typed_relations"] = rels


def append_filetree_fallback_source_nodes(
    project_root: str,
    nodes: List[Dict[str, Any]],
    profile: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Add source files not covered by the symbol parser as file-tree nodes."""
    try:
        from agent.governance.reconcile_file_inventory import build_file_inventory
    except Exception:
        return []
    existing = {
        _repo_relpath(project_root, str(node.get("primary_file") or ""))
        for node in nodes
        if node.get("primary_file")
    }
    try:
        inventory = build_file_inventory(
            project_root=project_root,
            run_id="filetree-fallback",
            nodes=nodes,
            feature_clusters=[],
        )
    except Exception:
        return []

    added: List[Dict[str, Any]] = []
    for row in inventory:
        if row.get("file_kind") != "source":
            continue
        rel = str(row.get("path") or "")
        if not rel or rel in existing:
            continue
        module = DEFAULT_LANGUAGE_POLICY.strip_source_suffix(rel).replace("/", ".").replace("\\", ".")
        node = {
            "node_id": module,
            "primary_file": rel,
            "module": module,
            "layer": 0,
            "functions": [],
            "function_lines": {},
            "function_count": 0,
            "test_coverage": find_test_coverage(project_root, rel, profile=profile),
            "doc_coverage": find_doc_coverage(project_root, rel, profile=profile),
            "source_kind": "filetree_fallback",
            "language": row.get("language") or "",
        }
        nodes.append(node)
        added.append(node)
        existing.add(rel)
    return added


def _fallback_feature_clusters(fallback_nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not fallback_nodes:
        return []
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for node in fallback_nodes:
        primary = str(node.get("primary_file") or "")
        buckets.setdefault(_package_key(primary), []).append(node)

    clusters: List[Dict[str, Any]] = []
    cap = _cluster_file_cap()
    for package_key, bucket_nodes in sorted(buckets.items()):
        current: List[Dict[str, Any]] = []
        for node in sorted(bucket_nodes, key=lambda n: str(n.get("primary_file") or "")):
            if current and len(current) >= cap:
                clusters.append(_fallback_cluster_from_nodes(package_key, current))
                current = []
            current.append(node)
        if current:
            clusters.append(_fallback_cluster_from_nodes(package_key, current))
    clusters.sort(key=lambda c: c["cluster_fingerprint"])
    return clusters


def _fallback_cluster_from_nodes(package_key: str, nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    primary_files = sorted({str(node.get("primary_file") or "") for node in nodes if node.get("primary_file")})
    entries = [f"filetree::{path}" for path in primary_files]
    cluster = {
        "cluster_fingerprint": _cluster_fingerprint(entries, primary_files),
        "entries": entries,
        "primary_files": primary_files,
        "secondary_files": sorted({
            path
            for node in nodes
            for path in ((node.get("test_coverage") or {}).get("test_files") or [])
        }),
        "functions": [],
        "modules": sorted({str(node.get("module") or "") for node in nodes if node.get("module")}),
        "decorators": [],
        "synthesis": {
            "strategy": "filetree_fallback_source",
            "package_key": package_key,
            "root_count": len(entries),
            "cycle_root_count": 0,
            "function_count": 0,
            "module_count": len(nodes),
            "file_cap": _cluster_file_cap(),
        },
    }
    cluster["feature_hash"] = _cluster_feature_hash(cluster)
    return cluster


def _area_key(module_name: str) -> str:
    parts = [p for p in module_name.split(".") if p]
    if not parts:
        return "root"
    if len(parts) >= 2 and parts[0] == "agent":
        if parts[1] in {"governance", "mcp", "telegram_gateway"}:
            return ".".join(parts[:2])
        return "agent.core"
    return parts[0]


def _area_title(area: str) -> str:
    return area.replace("_", " ").replace(".", " / ").title()


def _subsystem_key(module_name: str, signals: Dict[str, Any]) -> Tuple[str, str]:
    lower = module_name.lower()
    roles = set(signals.get("roles") or [])
    if "backlog" in lower:
        return "backlog_state_management", "Backlog State Management"
    if "memory" in lower:
        return "memory_system", "Memory System"
    if "reconcile" in lower:
        return "reconcile_graph_rebase", "Reconcile Graph Rebase"
    if "auto_chain" in lower or "chain_" in lower or lower.endswith(".chain_context"):
        return "standard_chain_runtime", "Standard Chain Runtime"
    if "project_profile" in lower or lower.endswith(".profile"):
        return "project_profile_boundaries", "Project Profile & Boundaries"
    if any(token in lower for token in ("language_adapter", "symbol_", ".symbol", "cluster_processor", "cluster_grouper")):
        return "symbol_language_analysis", "Symbol & Language Analysis"
    if any(token in lower for token in ("server", "gateway", ".mcp", "telegram")) or "gateway_entry" in roles:
        return "governance_api_gateway", "Governance API Gateway"
    if any(token in lower for token in ("service_manager", "deploy", "cron")):
        return "service_deployment", "Service Deployment"
    if "validation" in roles or any(token in lower for token in ("validator", "preflight", "policy", "permission", "gate")):
        return "validation_policy", "Validation & Policy"
    if "graph_tooling" in roles or any(token in lower for token in ("graph", "impact_analyzer")):
        return "graph_impact_tooling", "Graph & Impact Tooling"
    if "documentation" in roles or "doc_generator" in lower or "doc_policy" in lower:
        return "documentation_tooling", "Documentation Tooling"
    if "audit_evidence" in roles or any(token in lower for token in ("audit", "evidence", "observability")):
        return "audit_evidence", "Audit & Evidence"
    if any(token in lower for token in ("db", "state_service", "session", "baseline", "project_service")):
        return "governance_state_store", "Governance State Store"
    if "orchestration" in roles:
        return "workflow_orchestration", "Workflow Orchestration"
    if "state" in roles:
        return "persistent_state", "Persistent State"
    if "state_consumer" in roles:
        return "state_consumers", "State Consumers"
    if "domain_contract" in roles:
        return "domain_contracts", "Domain Contracts"
    area = _area_key(module_name)
    return f"{area.replace('.', '_')}_implementation", f"{_area_title(area)} Implementation"


def build_architecture_graph(
    nodes: List[Dict[str, Any]],
    typed_relations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build a generic hierarchy from module nodes and typed relations."""
    area_ids: Dict[str, str] = {}
    subsystem_ids: Dict[Tuple[str, str], str] = {}
    asset_ids: Dict[str, str] = {}
    asset_group_ids: Dict[str, str] = {}
    module_ids: Dict[str, str] = {}
    graph_nodes: List[Dict[str, Any]] = [{
        "id": "L1.1",
        "layer": "L1",
        "kind": "system",
        "title": "Project Runtime",
        "children": [],
    }]
    links: List[Dict[str, Any]] = []

    def add_link(source: str, target: str, relation_type: str, evidence: str = "") -> None:
        item = {"source": source, "target": target, "type": relation_type}
        if evidence:
            item["evidence"] = evidence
        if item not in links:
            links.append(item)

    def ensure_asset_group(target_kind: str) -> str:
        if "__project_assets" not in area_ids:
            area_id = f"L2.{len(area_ids) + 1}"
            area_ids["__project_assets"] = area_id
            graph_nodes.append({
                "id": area_id,
                "layer": "L2",
                "kind": "area",
                "title": "Project Assets",
                "area_key": "__project_assets",
                "children": [],
            })
            add_link("L1.1", area_id, "contains")
        group_map = {
            "db_table": ("state_assets", "State Assets"),
            "artifact": ("artifact_assets", "Artifact Assets"),
            "config": ("config_assets", "Config Assets"),
            "event": ("contract_assets", "Contract Assets"),
            "interface": ("interface_contracts", "Interface Contracts"),
            "task": ("contract_assets", "Contract Assets"),
            "task_metadata": ("contract_assets", "Contract Assets"),
        }
        group_key, title = group_map.get(target_kind, ("misc_assets", "Misc Assets"))
        if group_key not in asset_group_ids:
            group_id = f"L3.{len(subsystem_ids) + len(asset_group_ids) + 1}"
            asset_group_ids[group_key] = group_id
            graph_nodes.append({
                "id": group_id,
                "layer": "L3",
                "kind": "subsystem",
                "title": title,
                "area_key": "__project_assets",
                "subsystem_key": group_key,
                "roles": [],
                "children": [],
            })
            add_link(area_ids["__project_assets"], group_id, "contains")
        return asset_group_ids[group_key]

    def asset_identity(target_kind: str, target: str) -> Tuple[str, str, bool]:
        lower = target.lower()
        if target_kind == "db_table":
            return f"db_table:{target}", target, False
        if target_kind == "artifact":
            basename = lower.rsplit("/", 1)[-1]
            important = (
                basename.startswith("graph")
                or basename in {"governance.db", "context_store.db", "manager_signal.json", "manager_status.json"}
                or lower.endswith((".db", ".sqlite", ".sqlite3"))
            )
            if important:
                return f"artifact:{target}", target, False
            return "artifact:__artifact_assets", "Other Artifact Files", True
        if target_kind == "config":
            return f"config:{target}", target, False
        if target_kind == "event":
            return "event:__event_contracts", "Event Contracts", True
        if target_kind == "interface":
            if "/api/" in lower:
                return f"interface:{target}", target, False
            return "interface:__interface_contracts", "Interface Contracts", True
        if target_kind == "task":
            return "task:__task_contracts", "Task Contracts", True
        if target_kind == "task_metadata":
            return "task_metadata:__task_metadata_contracts", "Task Metadata Contracts", True
        return f"{target_kind}:{target}", target, False

    sorted_nodes = sorted(nodes, key=lambda n: str(n.get("module") or n.get("node_id") or ""))
    for node in sorted_nodes:
        module_name = str(node.get("module") or node.get("node_id") or "")
        if not module_name:
            continue
        area = _area_key(module_name)
        if area not in area_ids:
            area_id = f"L2.{len(area_ids) + 1}"
            area_ids[area] = area_id
            graph_nodes.append({
                "id": area_id,
                "layer": "L2",
                "kind": "area",
                "title": _area_title(area),
                "area_key": area,
                "children": [],
            })
            add_link("L1.1", area_id, "contains")

        signals = node.get("architecture_signals") or {}
        subsystem_key, subsystem_title = _subsystem_key(module_name, signals)
        subsystem_tuple = (area, subsystem_key)
        if subsystem_tuple not in subsystem_ids:
            subsystem_id = f"L3.{len(subsystem_ids) + 1}"
            subsystem_ids[subsystem_tuple] = subsystem_id
            graph_nodes.append({
                "id": subsystem_id,
                "layer": "L3",
                "kind": "subsystem",
                "title": subsystem_title,
                "area_key": area,
                "subsystem_key": subsystem_key,
                "roles": [],
                "children": [],
            })
            add_link(area_ids[area], subsystem_id, "contains")

        module_id = str(node.get("node_id") or module_name)
        module_ids[module_name] = module_id
        add_link(subsystem_ids[subsystem_tuple], module_id, "contains")

    for rel in typed_relations or []:
        module_name = str(rel.get("source_module") or "")
        module_id = module_ids.get(module_name)
        if not module_id:
            continue
        target_kind = str(rel.get("target_kind") or "")
        target = str(rel.get("target") or "")
        relation_type = str(rel.get("relation_type") or "")
        if target_kind in {"db_table", "artifact", "config", "event", "task", "task_metadata", "interface"} and target:
            asset_key, asset_title, aggregate_asset = asset_identity(target_kind, target)
            if asset_key not in asset_ids:
                asset_id = f"L4.{len(asset_ids) + 1}"
                asset_ids[asset_key] = asset_id
                graph_nodes.append({
                    "id": asset_id,
                    "layer": "L4",
                    "kind": target_kind,
                    "title": asset_title,
                    "asset_key": asset_key,
                    "aggregate_asset": aggregate_asset,
                    "children": [],
                })
                add_link(ensure_asset_group(target_kind), asset_id, "contains")
            evidence_parts = [str(rel.get("evidence") or "")]
            if asset_title != target:
                evidence_parts.append(target)
            add_link(module_id, asset_ids[asset_key], relation_type,
                     " | ".join(part for part in evidence_parts if part))

    for item in graph_nodes:
        item["children"] = sorted({
            link["target"] for link in links
            if link["source"] == item["id"] and link["type"] == "contains"
        })

    return {
        "nodes": graph_nodes,
        "links": sorted(links, key=lambda x: (x["source"], x["target"], x["type"])),
        "module_count": len(module_ids),
        "area_count": len(area_ids),
        "subsystem_count": sum(1 for n in graph_nodes if n.get("kind") == "subsystem"),
        "typed_relation_count": len(typed_relations or []),
    }


def synthesize_feature_clusters(
    *,
    project_root: str,
    modules: Dict[str, ModuleInfo],
    call_graph: CallGraph,
    sccs: List[List[str]],
    nodes: Optional[List[Dict[str, Any]]] = None,
    file_cap: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Build reconcile FeatureCluster candidates from SCC roots.

    The synthesis path is intentionally source-only: tests/docs are attached
    as secondary coverage after DFS, but they never become traversal roots.
    """
    if not modules or not call_graph.all_functions:
        return []

    cap = max(1, int(file_cap or _cluster_file_cap()))
    component_nodes: Dict[int, Set[str]] = {
        idx: {fn for fn in scc if fn in call_graph.all_functions}
        for idx, scc in enumerate(sccs)
    }
    component_nodes = {idx: members for idx, members in component_nodes.items() if members}
    if not component_nodes:
        return []

    component_by_function: Dict[str, int] = {
        fn: idx
        for idx, members in component_nodes.items()
        for fn in members
    }
    dag: Dict[int, Set[int]] = {idx: set() for idx in component_nodes}
    indegree: Dict[int, int] = {idx: 0 for idx in component_nodes}
    for caller, targets in call_graph.edges.items():
        caller_component = component_by_function.get(caller)
        if caller_component is None:
            continue
        for target in targets:
            target_component = component_by_function.get(target)
            if target_component is None or target_component == caller_component:
                continue
            if target_component not in dag[caller_component]:
                dag[caller_component].add(target_component)
                indegree[target_component] += 1

    root_components = sorted(idx for idx, count in indegree.items() if count == 0)
    if not root_components:
        root_components = sorted(component_nodes)

    module_functions: Dict[str, Set[str]] = {}
    for module_name, module_info in modules.items():
        module_functions[module_name] = {
            func.qualified_name for func in module_info.functions
        }

    node_by_module = {
        str(node.get("module") or node.get("node_id") or ""): node
        for node in (nodes or [])
    }

    reach_cache: Dict[int, Set[int]] = {}

    def reachable_components(start: int) -> Set[int]:
        if start in reach_cache:
            return set(reach_cache[start])
        seen: Set[int] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(sorted(dag.get(current, set()) - seen, reverse=True))
        reach_cache[start] = set(seen)
        return seen

    def files_for_modules(module_names: Set[str]) -> Tuple[List[str], List[str]]:
        primary_files: Set[str] = set()
        secondary_files: Set[str] = set()
        for module_name in sorted(module_names):
            node = node_by_module.get(module_name)
            if node:
                pf = _repo_relpath(project_root, str(node.get("primary_file") or ""))
                if pf:
                    primary_files.add(pf)
                for test_file in (node.get("test_coverage") or {}).get("test_files", []):
                    rel = _repo_relpath(project_root, str(test_file))
                    if rel:
                        secondary_files.add(rel)
                for doc_file in (node.get("doc_coverage") or {}).get("doc_files", []):
                    rel = _repo_relpath(project_root, str(doc_file))
                    if rel:
                        secondary_files.add(rel)
                continue
            module_info = modules.get(module_name)
            if module_info:
                rel = _repo_relpath(project_root, module_info.path)
                if rel:
                    primary_files.add(rel)
        return sorted(primary_files), sorted(secondary_files)

    branches: List[Dict[str, Any]] = []
    for root_component in root_components:
        root_functions = sorted(component_nodes[root_component])
        if not root_functions:
            continue
        entry_qname = root_functions[0]
        entry_module = _module_from_qname(entry_qname)
        reached_components = reachable_components(root_component)
        reached_functions: Set[str] = set()
        for component in reached_components:
            reached_functions.update(component_nodes.get(component, set()))

        # Keep a root module's local helpers with its root branch. This avoids
        # fragmenting plain, undecorated modules into one cluster per helper.
        reached_functions.update(module_functions.get(entry_module, set()))

        reached_modules = {
            module for module in (_module_from_qname(fn) for fn in reached_functions) if module
        }
        primary_files, secondary_files = files_for_modules(reached_modules)
        if not primary_files:
            continue
        entry_file = _repo_relpath(project_root, modules.get(entry_module, ModuleInfo("", "")).path)
        package_key = _package_key(entry_file or primary_files[0])
        decorators = sorted({
            dec
            for fn in reached_functions
            for dec in (call_graph.all_functions.get(fn).decorators if call_graph.all_functions.get(fn) else [])
        })
        branches.append({
            "entry_qname": entry_qname,
            "root_functions": root_functions,
            "root_component_size": len(root_functions),
            "is_cycle_root": len(root_functions) > 1,
            "package_key": package_key,
            "primary_files": primary_files,
            "secondary_files": secondary_files,
            "functions": sorted(reached_functions),
            "modules": sorted(reached_modules),
            "decorators": decorators,
        })

    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for branch in branches:
        buckets.setdefault(branch["package_key"], []).append(branch)

    clusters: List[Dict[str, Any]] = []

    def flush_chunk(package_key: str, chunk: List[Dict[str, Any]]) -> None:
        if not chunk:
            return
        entries = sorted({branch["entry_qname"] for branch in chunk if branch.get("entry_qname")})
        primary_files = sorted({pf for branch in chunk for pf in branch.get("primary_files", [])})
        secondary_files = sorted({sf for branch in chunk for sf in branch.get("secondary_files", [])})
        functions = sorted({fn for branch in chunk for fn in branch.get("functions", [])})
        modules_in_cluster = sorted({mod for branch in chunk for mod in branch.get("modules", [])})
        decorators = sorted({dec for branch in chunk for dec in branch.get("decorators", [])})
        cluster = {
            "cluster_fingerprint": _cluster_fingerprint(entries, primary_files),
            "entries": entries,
            "primary_files": primary_files,
            "secondary_files": secondary_files,
            "functions": functions,
            "modules": modules_in_cluster,
            "decorators": decorators,
            "synthesis": {
                "strategy": "scc_indegree_root_dfs_filetree_coalesce",
                "package_key": package_key,
                "root_count": len(entries),
                "cycle_root_count": sum(1 for branch in chunk if branch.get("is_cycle_root")),
                "function_count": len(functions),
                "module_count": len(modules_in_cluster),
                "file_cap": cap,
            },
        }
        cluster["feature_hash"] = _cluster_feature_hash(cluster)
        clusters.append(cluster)

    for package_key, package_branches in sorted(buckets.items()):
        current: List[Dict[str, Any]] = []
        current_files: Set[str] = set()
        for branch in sorted(package_branches, key=lambda b: (
            b.get("primary_files", [""])[0] if b.get("primary_files") else "",
            b.get("entry_qname", ""),
        )):
            branch_files = set(branch.get("primary_files", []))
            next_files = current_files | branch_files
            if current and len(next_files) > cap:
                flush_chunk(package_key, current)
                current = []
                current_files = set()
            current.append(branch)
            current_files.update(branch_files)
        flush_chunk(package_key, current)

    clusters.sort(key=lambda c: c["cluster_fingerprint"])
    return clusters


# ---------------------------------------------------------------------------
# PR3 R4: Diff against existing graph
# ---------------------------------------------------------------------------

def _default_existing_graph_path(project_root: str) -> Optional[str]:
    """Return the best-effort current governance graph path for *project_root*."""
    explicit = os.environ.get("PHASE_Z_EXISTING_GRAPH_PATH")
    if explicit and os.path.isfile(explicit):
        return explicit

    project_id = (
        os.environ.get("AMING_PROJECT_ID")
        or os.environ.get("PROJECT_ID")
        or "aming-claw"
    )
    candidates = [
        os.path.join(project_root, "agent", "governance", "graph.json"),
        os.path.join(
            project_root,
            "shared-volume",
            "codex-tasks",
            "state",
            "governance",
            project_id,
            "graph.json",
        ),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    governance_root = os.path.join(
        project_root, "shared-volume", "codex-tasks", "state", "governance"
    )
    if os.path.isdir(governance_root):
        found = []
        for name in sorted(os.listdir(governance_root)):
            candidate = os.path.join(governance_root, name, "graph.json")
            if os.path.isfile(candidate):
                found.append(candidate)
        if len(found) == 1:
            return found[0]
    return None


def _normalize_graph_path(project_root: str, path: Any) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    try:
        if os.path.isabs(raw):
            raw = os.path.relpath(raw, project_root)
    except ValueError:
        pass
    normalized = raw.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def _extract_graph_nodes(payload: Any) -> List[Dict[str, Any]]:
    """Extract node dictionaries from supported graph.json shapes."""
    if not isinstance(payload, dict):
        if isinstance(payload, list):
            return [n for n in payload if isinstance(n, dict)]
        return []

    nodes = payload.get("nodes")
    if isinstance(nodes, list):
        return [n for n in nodes if isinstance(n, dict)]
    if isinstance(nodes, dict):
        return [n for n in nodes.values() if isinstance(n, dict)]

    deps_graph = payload.get("deps_graph")
    if isinstance(deps_graph, dict):
        deps_nodes = deps_graph.get("nodes")
        if isinstance(deps_nodes, list):
            return [n for n in deps_nodes if isinstance(n, dict)]
        if isinstance(deps_nodes, dict):
            return [n for n in deps_nodes.values() if isinstance(n, dict)]
    return []


def _node_id(node: Dict[str, Any]) -> str:
    return str(node.get("node_id") or node.get("id") or "")


def _node_layer(node: Dict[str, Any]) -> Any:
    return node.get("layer")


def _node_primary_files(project_root: str, node: Dict[str, Any]) -> List[str]:
    raw = (
        node.get("primary_file")
        or node.get("primary")
        or node.get("primary_files")
        or []
    )
    if isinstance(raw, str):
        raw_values = [raw]
    elif isinstance(raw, list):
        raw_values = raw
    else:
        raw_values = []
    return sorted({
        normalized
        for normalized in (_normalize_graph_path(project_root, p) for p in raw_values)
        if normalized
    })


def _index_primary_files(
    project_root: str,
    nodes: List[Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[str]]]:
    by_primary: Dict[str, Dict[str, Any]] = {}
    owners: Dict[str, List[str]] = {}
    for node in nodes:
        nid = _node_id(node)
        for primary in _node_primary_files(project_root, node):
            owners.setdefault(primary, []).append(nid)
            by_primary.setdefault(primary, node)
    return by_primary, owners


def diff_against_existing_graph(
    project_root: str,
    new_nodes: List[Dict[str, Any]],
    graph_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare new derived nodes vs existing graph.json.

    Returns ID-based drift plus primary-file drift.  The latter is the useful
    calibration signal when rebasing an old Lx graph into symbol-derived module
    nodes whose IDs are intentionally different.
    """
    existing_graph_path = graph_path or _default_existing_graph_path(project_root)

    old_nodes_by_id: Dict[str, Any] = {}
    if existing_graph_path and os.path.isfile(existing_graph_path):
        try:
            with open(existing_graph_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for n in _extract_graph_nodes(data):
                nid = _node_id(n)
                if nid:
                    old_nodes_by_id[nid] = n
        except (json.JSONDecodeError, OSError):
            pass

    new_ids = {_node_id(n) for n in new_nodes if _node_id(n)}
    old_ids = set(old_nodes_by_id.keys())

    only_in_new = sorted(new_ids - old_ids)
    only_in_old = sorted(old_ids - new_ids)

    layer_changes: List[Dict[str, Any]] = []
    new_by_id = {_node_id(n): n for n in new_nodes if _node_id(n)}
    for nid in new_ids & old_ids:
        old_layer = _node_layer(old_nodes_by_id[nid])
        new_layer = _node_layer(new_by_id[nid])
        if old_layer is not None and new_layer is not None and old_layer != new_layer:
            layer_changes.append({
                "node_id": nid,
                "old_layer": old_layer,
                "new_layer": new_layer,
            })

    old_nodes = list(old_nodes_by_id.values())
    old_by_primary, old_primary_owners = _index_primary_files(project_root, old_nodes)
    new_by_primary, new_primary_owners = _index_primary_files(project_root, new_nodes)
    old_primaries = set(old_by_primary)
    new_primaries = set(new_by_primary)

    layer_changes_by_primary: List[Dict[str, Any]] = []
    for primary in sorted(old_primaries & new_primaries):
        old_node = old_by_primary[primary]
        new_node = new_by_primary[primary]
        old_layer = _node_layer(old_node)
        new_layer = _node_layer(new_node)
        if old_layer is not None and new_layer is not None and old_layer != new_layer:
            layer_changes_by_primary.append({
                "primary_file": primary,
                "old_node_id": _node_id(old_node),
                "new_node_id": _node_id(new_node),
                "old_layer": old_layer,
                "new_layer": new_layer,
            })

    duplicate_old = {
        primary: sorted([owner for owner in owners if owner])
        for primary, owners in old_primary_owners.items()
        if len([owner for owner in owners if owner]) > 1
    }
    duplicate_new = {
        primary: sorted([owner for owner in owners if owner])
        for primary, owners in new_primary_owners.items()
        if len([owner for owner in owners if owner]) > 1
    }

    return {
        "graph_path": existing_graph_path or "",
        "old_node_count": len(old_nodes_by_id),
        "new_node_count": len(new_nodes),
        "only_in_new": only_in_new,
        "only_in_old": only_in_old,
        "layer_changes": layer_changes,
        "primary_file_diff": {
            "matched": len(old_primaries & new_primaries),
            "only_in_new": sorted(new_primaries - old_primaries),
            "only_in_old": sorted(old_primaries - new_primaries),
            "layer_changes": layer_changes_by_primary,
            "duplicates_in_old": duplicate_old,
            "duplicates_in_new": duplicate_new,
        },
    }


# ---------------------------------------------------------------------------
# PR3 R5: Write graph.v2.json
# ---------------------------------------------------------------------------

def write_graph_v2_json(
    project_root: str,
    nodes: List[Dict[str, Any]],
) -> str:
    """Write agent/governance/graph.v2.json (NOT graph.json).

    Returns the path to the written file.
    """
    out_path = os.path.join(project_root, "agent", "governance", "graph.v2.json")
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "version": "v2",
        "node_count": len(nodes),
        "nodes": nodes,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return out_path


def build_rebase_candidate_graph(
    project_root: str,
    phase_result: Dict[str, Any],
    *,
    session_id: str = "",
    run_id: str = "",
) -> Dict[str, Any]:
    """Materialize a reviewable deps_graph from Phase Z v2 architecture data.

    The output intentionally remains a candidate artifact: callers may write it
    beside governance state for observer review, but this function never touches
    the canonical graph.json.
    """
    arch = phase_result.get("architecture_graph") or {}
    arch_nodes = list(arch.get("nodes") or [])
    arch_links = list(arch.get("links") or [])
    module_nodes = sorted(
        [n for n in (phase_result.get("nodes") or []) if isinstance(n, dict)],
        key=lambda n: str(n.get("module") or n.get("node_id") or ""),
    )

    out_nodes: List[Dict[str, Any]] = []
    id_map: Dict[str, str] = {}
    seen_ids: Set[str] = set()

    for item in arch_nodes:
        node_id = str(item.get("id") or "")
        if not node_id or node_id in seen_ids:
            continue
        seen_ids.add(node_id)
        out_nodes.append({
            "id": node_id,
            "title": str(item.get("title") or node_id),
            "layer": str(item.get("layer") or node_id.split(".", 1)[0]),
            "primary": [],
            "secondary": [],
            "test": [],
            "config": [],
            "artifacts": [],
            "_deps": [],
            "verify_level": 1,
            "gate_mode": "auto",
            "test_coverage": "none",
            "metadata": {
                "kind": item.get("kind") or "",
                "area_key": item.get("area_key") or "",
                "subsystem_key": item.get("subsystem_key") or "",
                "asset_key": item.get("asset_key") or "",
                "aggregate_asset": bool(item.get("aggregate_asset")),
                "children": item.get("children") or [],
            },
            "version": f"rebase:{session_id}" if session_id else "rebase:candidate",
        })

    for idx, node in enumerate(module_nodes, start=1):
        module_name = str(node.get("module") or node.get("node_id") or "")
        node_id = f"L7.{idx}"
        if module_name:
            id_map[module_name] = node_id
        raw_primary = node.get("primary_file") or ""
        primary = [_repo_relpath(project_root, raw_primary)] if raw_primary else []
        test_files = [
            _repo_relpath(project_root, f)
            for f in (node.get("test_coverage") or {}).get("test_files", [])
            if f
        ]
        doc_files = [
            _repo_relpath(project_root, f)
            for f in (node.get("doc_coverage") or {}).get("doc_files", [])
            if f
        ]
        config_files = [
            _repo_relpath(project_root, f)
            for f in (node.get("config_files") or [])
            if f
        ]
        out_nodes.append({
            "id": node_id,
            "title": module_name or str(node.get("node_id") or node_id),
            "layer": "L7",
            "primary": sorted({p for p in primary if p}),
            "secondary": sorted({p for p in doc_files if p}),
            "test": sorted({p for p in test_files if p}),
            "config": sorted({p for p in config_files if p}),
            "artifacts": [],
            "_deps": [],
            "verify_level": 1,
            "gate_mode": "auto",
            "test_coverage": "direct" if test_files else "none",
            "metadata": {
                "module": module_name,
                "function_count": node.get("function_count", 0),
                "functions": node.get("functions") or [],
                "function_lines": node.get("function_lines") or {},
                "function_calls": node.get("function_calls") or [],
                "function_called_by": node.get("function_called_by") or [],
                "function_weak_calls": node.get("function_weak_calls") or [],
                "function_call_count": node.get("function_call_count", 0),
                "function_called_by_count": node.get("function_called_by_count", 0),
                "function_weak_call_count": node.get("function_weak_call_count", 0),
                "config_files": sorted({p for p in config_files if p}),
                "test_consumer_fanin": (node.get("test_coverage") or {}).get("fan_in_evidence") or [],
                "architecture_signals": node.get("architecture_signals") or {},
                "typed_relations": node.get("typed_relations") or [],
            },
            "version": f"rebase:{session_id}" if session_id else "rebase:candidate",
        })

    out_ids = {str(n.get("id") or "") for n in out_nodes}
    nodes_by_id = {str(n.get("id") or ""): n for n in out_nodes}
    hierarchy_links: List[Dict[str, Any]] = []
    evidence_links: List[Dict[str, Any]] = []
    dependency_links: List[Dict[str, Any]] = []
    hierarchy_index: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    evidence_index: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    dependency_index: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    dependency_warnings: List[Dict[str, Any]] = []
    aggregate_dependency_skips: List[Dict[str, Any]] = []
    adjacency: Dict[str, Set[str]] = {node_id: set() for node_id in out_ids}

    def would_create_cycle(source: str, target: str) -> bool:
        if source == target:
            return True
        stack = [target]
        seen: Set[str] = set()
        while stack:
            current = stack.pop()
            if current == source:
                return True
            if current in seen:
                continue
            seen.add(current)
            stack.extend(sorted(adjacency.get(current, set()) - seen))
        return False

    def add_indexed_link(
        links: List[Dict[str, Any]],
        index: Dict[Tuple[str, str, str], Dict[str, Any]],
        source: str,
        target: str,
        relation_type: str,
        evidence: str = "",
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not source or not target or source not in out_ids or target not in out_ids:
            return False
        if source == target:
            return False
        key = (source, target, relation_type)
        if key in index:
            item = index[key]
            item["evidence_count"] = int(item.get("evidence_count") or 1) + 1
            if evidence:
                sample = item.setdefault("evidence_sample", [])
                if isinstance(sample, list) and evidence not in sample and len(sample) < 5:
                    sample.append(evidence)
            return True
        item = {"source": source, "target": target, "type": relation_type}
        if evidence:
            item["evidence"] = evidence
            item["evidence_sample"] = [evidence]
            item["evidence_count"] = 1
        if metadata:
            item["metadata"] = metadata
        index[key] = item
        links.append(item)
        return True

    def add_dependency_link(
        source: str,
        target: str,
        relation_type: str,
        evidence: str = "",
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not source or not target or source not in out_ids or target not in out_ids:
            return False
        if source == target:
            return False
        key = (source, target, relation_type)
        if key in dependency_index:
            return add_indexed_link(
                dependency_links,
                dependency_index,
                source,
                target,
                relation_type,
                evidence,
                metadata=metadata,
            )
        if would_create_cycle(source, target):
            dependency_warnings.append({
                "reason": "cycle_suppressed",
                "source": source,
                "target": target,
                "type": relation_type,
                "evidence": evidence,
            })
            return False
        added = add_indexed_link(
            dependency_links,
            dependency_index,
            source,
            target,
            relation_type,
            evidence,
            metadata=metadata,
        )
        if not added:
            return False
        adjacency.setdefault(source, set()).add(target)
        return True

    def node_layer(node_id: str) -> str:
        return str((nodes_by_id.get(node_id) or {}).get("layer") or "")

    def is_aggregate_asset(node_id: str) -> bool:
        node = nodes_by_id.get(node_id) or {}
        metadata = node.get("metadata") or {}
        return node.get("layer") == "L4" and bool(metadata.get("aggregate_asset"))

    def sorted_links(links: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return sorted(links, key=lambda x: (x["source"], x["target"], x["type"]))

    def dependency_direction(source: str, target: str, relation_type: str) -> Tuple[str, str]:
        producer_to_asset = {
            "owns_state",
            "writes_state",
            "writes_artifact",
            "emits_event",
        }
        asset_to_consumer = {
            "reads_state",
            "reads_artifact",
            "consumes_event",
            "uses_task_metadata",
            "http_route",
            "creates_task",
            "configures_role",
            "configures_analyzer",
            "configures_model_routing",
            "configures_runtime",
        }
        if relation_type in producer_to_asset:
            return source, target
        if relation_type in asset_to_consumer:
            return target, source
        return source, target

    for link in arch_links:
        src = str(link.get("source") or "")
        dst = str(link.get("target") or "")
        src = id_map.get(src, src)
        dst = id_map.get(dst, dst)
        relation_type = str(link.get("type") or "depends_on")
        evidence = str(link.get("evidence") or "")
        if relation_type == "contains":
            add_indexed_link(
                hierarchy_links,
                hierarchy_index,
                src,
                dst,
                relation_type,
                evidence,
                metadata={"edge_kind": "hierarchy"},
            )
            continue
        add_indexed_link(
            evidence_links,
            evidence_index,
            src,
            dst,
            relation_type,
            evidence,
            metadata={"edge_kind": "typed_evidence"},
        )
        dep_src, dep_dst = dependency_direction(src, dst, relation_type)
        aggregate_asset = src if is_aggregate_asset(src) else dst if is_aggregate_asset(dst) else ""
        if aggregate_asset:
            aggregate_dependency_skips.append({
                "reason": "aggregate_asset_not_promoted",
                "asset": aggregate_asset,
                "source": src,
                "target": dst,
                "type": relation_type,
            })
            continue
        add_dependency_link(
            dep_src,
            dep_dst,
            relation_type,
            evidence,
            metadata={
                "edge_kind": "typed_dependency",
                "evidence_source": src,
                "evidence_target": dst,
            },
        )

    for edge in phase_result.get("module_dependency_edges") or []:
        source_module = str(edge.get("source_module") or "")
        target_module = str(edge.get("target_module") or "")
        source_id = id_map.get(source_module, "")
        target_id = id_map.get(target_module, "")
        evidence = str(edge.get("evidence") or "")
        metadata = {
            "edge_kind": "module_dependency",
            "relation_type": edge.get("relation_type") or "",
        }
        add_indexed_link(
            evidence_links,
            evidence_index,
            source_id,
            target_id,
            "depends_on",
            evidence,
            metadata=metadata,
        )
        add_dependency_link(
            source_id,
            target_id,
            "depends_on",
            evidence,
            metadata=metadata,
        )

    parent: Dict[str, str] = {}
    for link in hierarchy_links:
        if link.get("type") == "contains":
            parent[str(link["target"])] = str(link["source"])

    def parent_at(node_id: str, layer: str) -> str:
        current = node_id
        seen: Set[str] = set()
        while current and current not in seen:
            seen.add(current)
            if (nodes_by_id.get(current) or {}).get("layer") == layer:
                return current
            current = parent.get(current, "")
        return ""

    asset_producers: Dict[str, Set[str]] = {}
    asset_consumers: Dict[str, Set[str]] = {}
    for link in dependency_links:
        source = str(link.get("source") or "")
        target = str(link.get("target") or "")
        source_layer = node_layer(source)
        target_layer = node_layer(target)
        if source_layer == "L7" and target_layer == "L4":
            asset_producers.setdefault(target, set()).add(source)
        elif source_layer == "L4" and target_layer == "L7":
            asset_consumers.setdefault(source, set()).add(target)

    for asset_id, producers in sorted(asset_producers.items()):
        if is_aggregate_asset(asset_id):
            aggregate_dependency_skips.append({
                "reason": "aggregate_shared_asset_not_promoted",
                "asset": asset_id,
                "producer_count": len(producers),
                "consumer_count": len(asset_consumers.get(asset_id, set())),
            })
            continue
        consumers = asset_consumers.get(asset_id, set())
        for producer in sorted(producers):
            for consumer in sorted(consumers):
                add_dependency_link(
                    producer,
                    consumer,
                    "depends_on",
                    f"shared asset {asset_id}",
                    metadata={"edge_kind": "shared_asset_dependency", "asset": asset_id},
                )

    base_dependency_edges = [
        link for link in list(dependency_links)
        if str(link.get("source") or "").startswith("L7.")
        and str(link.get("target") or "").startswith("L7.")
    ]
    for link in base_dependency_edges:
        source_l7 = str(link["source"])
        target_l7 = str(link["target"])
        source_l3 = parent_at(source_l7, "L3")
        target_l3 = parent_at(target_l7, "L3")
        source_l2 = parent_at(source_l7, "L2")
        target_l2 = parent_at(target_l7, "L2")
        if source_l3 and target_l3 and source_l3 != target_l3:
            add_dependency_link(
                source_l3,
                target_l3,
                "depends_on",
                f"aggregated from {source_l7}->{target_l7}",
                metadata={"edge_kind": "aggregated_l3_dependency"},
            )
        if source_l2 and target_l2 and source_l2 != target_l2:
            add_dependency_link(
                source_l2,
                target_l2,
                "depends_on",
                f"aggregated from {source_l7}->{target_l7}",
                metadata={"edge_kind": "aggregated_l2_dependency"},
            )

    deps_by_child: Dict[str, List[str]] = {}
    for link in dependency_links:
        deps_by_child.setdefault(str(link["target"]), []).append(str(link["source"]))
    for node in out_nodes:
        node["_deps"] = sorted(set(deps_by_child.get(str(node.get("id")), [])))
        parent_id = parent.get(str(node.get("id") or ""))
        if parent_id:
            metadata = node.setdefault("metadata", {})
            if isinstance(metadata, dict):
                metadata["hierarchy_parent"] = parent_id

    hierarchy_edge_type_counts = _count_values([str(link.get("type") or "") for link in hierarchy_links])
    evidence_edge_type_counts = _count_values([str(link.get("type") or "") for link in evidence_links])
    dependency_edge_type_counts = _count_values([str(link.get("type") or "") for link in dependency_links])
    same_layer_dependency_count = sum(
        1 for link in dependency_links
        if str(link.get("source") or "").split(".", 1)[0] == str(link.get("target") or "").split(".", 1)[0]
    )
    cross_layer_dependency_count = sum(
        1 for link in dependency_links
        if str(link.get("source") or "").split(".", 1)[0] != str(link.get("target") or "").split(".", 1)[0]
    )
    try:
        from agent.governance.governance_hints import apply_binding_hints_to_graph_nodes
        governance_hint_bindings = apply_binding_hints_to_graph_nodes(project_root, out_nodes)
    except Exception as exc:
        governance_hint_bindings = {
            "hint_count": 0,
            "applied_count": 0,
            "skipped_count": 0,
            "error": str(exc),
        }

    graph_payload = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id or phase_result.get("run_id", ""),
        "session_id": session_id,
        "hierarchy_graph": {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": out_nodes,
            "links": sorted_links(hierarchy_links),
        },
        "evidence_graph": {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": out_nodes,
            "links": sorted_links(evidence_links),
        },
        "deps_graph": {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": out_nodes,
            "links": sorted_links(dependency_links),
        },
        "gates_graph": {
            "directed": True,
            "multigraph": False,
            "graph": {},
            "nodes": [],
            "links": [],
        },
        "architecture_summary": {
            "node_count": len(out_nodes),
            "link_count": len(hierarchy_links) + len(evidence_links) + len(dependency_links),
            "hierarchy_link_count": len(hierarchy_links),
            "evidence_link_count": len(evidence_links),
            "dependency_link_count": len(dependency_links),
            "module_node_count": len(module_nodes),
            "typed_relation_count": len(phase_result.get("typed_relations") or []),
            "module_dependency_count": len(phase_result.get("module_dependency_edges") or []),
            "area_count": arch.get("area_count", 0),
            "subsystem_count": arch.get("subsystem_count", 0),
            "hierarchy_edge_type_counts": hierarchy_edge_type_counts,
            "evidence_edge_type_counts": evidence_edge_type_counts,
            "dependency_edge_type_counts": dependency_edge_type_counts,
            "edge_type_counts": dependency_edge_type_counts,
            "same_layer_dependency_count": same_layer_dependency_count,
            "cross_layer_dependency_count": cross_layer_dependency_count,
            "aggregate_dependency_skipped_count": len(aggregate_dependency_skips),
            "aggregate_dependency_skipped_sample": aggregate_dependency_skips[:25],
            "cycle_suppressed_dependency_count": len(dependency_warnings),
            "dependency_warning_count": len(dependency_warnings),
            "dependency_warning_sample": dependency_warnings[:25],
            "governance_hint_bindings": governance_hint_bindings,
        },
    }
    return graph_payload


def _count_values(values: List[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for value in values:
        key = str(value or "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def validate_dependency_patches(
    candidate: Dict[str, Any],
    patches: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Validate PM/Dev proposed dependency corrections before QA apply.

    The validator is deliberately conservative.  AI may propose graph repairs,
    but every patch must identify concrete nodes, carry evidence, respect edge
    direction rules, avoid aggregate L4 buckets, and keep deps_graph acyclic.
    """
    nodes = [
        node for node in (candidate.get("deps_graph") or {}).get("nodes", [])
        if isinstance(node, dict)
    ]
    nodes_by_id = {str(node.get("id") or ""): node for node in nodes}
    deps_links = [
        link for link in (candidate.get("deps_graph") or {}).get("links", [])
        if isinstance(link, dict)
    ]
    working_links = [dict(link) for link in deps_links]
    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    for idx, raw_patch in enumerate(patches or []):
        patch = dict(raw_patch or {})
        patch_id = str(patch.get("patch_id") or f"patch-{idx + 1}")
        errors = _dependency_patch_errors(patch, nodes_by_id, working_links)
        normalized = _normalize_dependency_patch(patch, patch_id)
        if errors:
            rejected.append({
                "patch_id": patch_id,
                "errors": errors,
                "patch": normalized,
            })
            continue
        accepted.append(normalized)
        _apply_patch_to_links(working_links, normalized)

    return {
        "ok": not rejected,
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "accepted": accepted,
        "rejected": rejected,
    }


def apply_dependency_patches(
    candidate: Dict[str, Any],
    patches: List[Dict[str, Any]],
    *,
    qa_actor: str = "",
) -> Dict[str, Any]:
    """Return a candidate copy with QA-validated dependency patches applied."""
    validation = validate_dependency_patches(candidate, patches)
    if not validation.get("ok"):
        return {
            "ok": False,
            "candidate": candidate,
            "validation": validation,
        }

    updated = copy.deepcopy(candidate)
    deps_graph = updated.setdefault("deps_graph", {})
    links = [
        dict(link) for link in deps_graph.get("links", [])
        if isinstance(link, dict)
    ]
    for patch in validation.get("accepted") or []:
        _apply_patch_to_links(links, patch, qa_actor=qa_actor)
    deps_graph["links"] = sorted(links, key=lambda x: (
        str(x.get("source") or ""),
        str(x.get("target") or ""),
        str(x.get("type") or ""),
    ))
    summary = updated.setdefault("architecture_summary", {})
    summary["dependency_patch_review"] = {
        "qa_actor": qa_actor,
        "accepted_count": validation.get("accepted_count", 0),
        "rejected_count": validation.get("rejected_count", 0),
        "patch_ids": [patch.get("patch_id") for patch in validation.get("accepted") or []],
    }
    summary["dependency_link_count"] = len(deps_graph.get("links") or [])
    summary["dependency_edge_type_counts"] = _count_values([
        str(link.get("type") or "") for link in deps_graph.get("links") or []
    ])
    summary["edge_type_counts"] = summary["dependency_edge_type_counts"]
    _refresh_node_deps(updated)
    return {
        "ok": True,
        "candidate": updated,
        "validation": validation,
    }


def _normalize_dependency_patch(patch: Dict[str, Any], patch_id: str) -> Dict[str, Any]:
    evidence = patch.get("evidence") or []
    if isinstance(evidence, str):
        evidence_items = [evidence]
    elif isinstance(evidence, list):
        evidence_items = [str(item) for item in evidence if str(item or "").strip()]
    else:
        evidence_items = []
    return {
        "patch_id": patch_id,
        "op": str(patch.get("op") or patch.get("type") or "add_dependency"),
        "source": str(patch.get("source") or patch.get("from") or ""),
        "target": str(patch.get("target") or patch.get("to") or ""),
        "edge_type": str(patch.get("edge_type") or patch.get("relation_type") or "depends_on"),
        "old_edge_type": str(patch.get("old_edge_type") or ""),
        "reason": str(patch.get("reason") or ""),
        "evidence": evidence_items,
        "confidence": str(patch.get("confidence") or ""),
    }


def _dependency_patch_errors(
    patch: Dict[str, Any],
    nodes_by_id: Dict[str, Dict[str, Any]],
    current_links: List[Dict[str, Any]],
) -> List[str]:
    normalized = _normalize_dependency_patch(patch, str(patch.get("patch_id") or "patch"))
    op = normalized["op"]
    source = normalized["source"]
    target = normalized["target"]
    edge_type = normalized["edge_type"]
    old_type = normalized["old_edge_type"]
    errors: List[str] = []

    if op not in {"add_dependency", "remove_dependency", "reclassify_edge"}:
        errors.append("invalid_op")
    if not source or source not in nodes_by_id:
        errors.append("source_missing")
    if not target or target not in nodes_by_id:
        errors.append("target_missing")
    if source == target:
        errors.append("self_dependency")
    if not normalized["reason"] or not normalized["evidence"]:
        errors.append("missing_reason_or_evidence")
    if edge_type == "contains":
        errors.append("contains_not_patchable")
    if source in nodes_by_id and target in nodes_by_id:
        if _patch_uses_aggregate_asset(nodes_by_id[source], nodes_by_id[target]):
            errors.append("aggregate_asset_not_allowed")
        if not _dependency_patch_direction_ok(nodes_by_id[source], nodes_by_id[target], edge_type):
            errors.append("invalid_dependency_direction")

    existing = _find_link(current_links, source, target, old_type or edge_type)
    if op in {"remove_dependency", "reclassify_edge"} and existing is None:
        errors.append("edge_not_found")
    if op == "reclassify_edge" and not old_type:
        errors.append("old_edge_type_required")
    if op in {"add_dependency", "reclassify_edge"} and not errors:
        trial_links = [
            link for link in current_links
            if not (op == "reclassify_edge"
                    and str(link.get("source") or "") == source
                    and str(link.get("target") or "") == target
                    and str(link.get("type") or "") == old_type)
        ]
        if _find_link(trial_links, source, target, edge_type) is None:
            trial_links.append({"source": source, "target": target, "type": edge_type})
        if _links_have_cycle(nodes_by_id.keys(), trial_links):
            errors.append("cycle_introduced")
    return errors


def _patch_uses_aggregate_asset(source_node: Dict[str, Any], target_node: Dict[str, Any]) -> bool:
    for node in (source_node, target_node):
        if node.get("layer") == "L4" and bool((node.get("metadata") or {}).get("aggregate_asset")):
            return True
    return False


def _dependency_patch_direction_ok(
    source_node: Dict[str, Any],
    target_node: Dict[str, Any],
    edge_type: str,
) -> bool:
    source_layer = str(source_node.get("layer") or "")
    target_layer = str(target_node.get("layer") or "")
    if edge_type == "depends_on":
        return source_layer == target_layer or {source_layer, target_layer} <= {"L4", "L7"}
    if edge_type in {
        "reads_state",
        "reads_artifact",
        "consumes_event",
        "uses_task_metadata",
        "http_route",
        "creates_task",
        "configures_role",
        "configures_analyzer",
        "configures_model_routing",
        "configures_runtime",
    }:
        return source_layer == "L4" and target_layer == "L7"
    if edge_type in {"owns_state", "writes_state", "writes_artifact", "emits_event"}:
        return source_layer == "L7" and target_layer == "L4"
    return False


def _find_link(
    links: List[Dict[str, Any]],
    source: str,
    target: str,
    edge_type: str,
) -> Optional[Dict[str, Any]]:
    for link in links:
        if (
            str(link.get("source") or "") == source
            and str(link.get("target") or "") == target
            and str(link.get("type") or "") == edge_type
        ):
            return link
    return None


def _apply_patch_to_links(
    links: List[Dict[str, Any]],
    patch: Dict[str, Any],
    *,
    qa_actor: str = "",
) -> None:
    op = patch["op"]
    source = patch["source"]
    target = patch["target"]
    edge_type = patch["edge_type"]
    old_type = patch.get("old_edge_type") or edge_type
    if op in {"remove_dependency", "reclassify_edge"}:
        links[:] = [
            link for link in links
            if not (
                str(link.get("source") or "") == source
                and str(link.get("target") or "") == target
                and str(link.get("type") or "") == old_type
            )
        ]
    if op in {"add_dependency", "reclassify_edge"} and _find_link(links, source, target, edge_type) is None:
        links.append({
            "source": source,
            "target": target,
            "type": edge_type,
            "evidence": "; ".join(patch.get("evidence") or []),
            "metadata": {
                "edge_kind": "qa_dependency_patch",
                "patch_id": patch.get("patch_id") or "",
                "reason": patch.get("reason") or "",
                "confidence": patch.get("confidence") or "",
                "qa_actor": qa_actor,
            },
        })


def _links_have_cycle(node_ids: Any, links: List[Dict[str, Any]]) -> bool:
    adjacency: Dict[str, Set[str]] = {str(node_id): set() for node_id in node_ids}
    for link in links:
        source = str(link.get("source") or "")
        target = str(link.get("target") or "")
        if not source or not target:
            continue
        adjacency.setdefault(source, set()).add(target)
        adjacency.setdefault(target, set())
    visiting: Set[str] = set()
    visited: Set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        for target in adjacency.get(node_id, set()):
            if visit(target):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(visit(node_id) for node_id in list(adjacency))


def _refresh_node_deps(candidate: Dict[str, Any]) -> None:
    deps_graph = candidate.get("deps_graph") or {}
    deps_by_child: Dict[str, Set[str]] = {}
    for link in deps_graph.get("links") or []:
        source = str(link.get("source") or "")
        target = str(link.get("target") or "")
        if source and target:
            deps_by_child.setdefault(target, set()).add(source)
    for node in deps_graph.get("nodes") or []:
        node["_deps"] = sorted(deps_by_child.get(str(node.get("id") or ""), set()))


def build_candidate_coverage_ledger(
    project_root: str,
    phase_result: Dict[str, Any],
    candidate: Dict[str, Any],
) -> Dict[str, Any]:
    """Build an auditable file/relation coverage ledger for PM review.

    The ledger is intentionally evidence-first.  Rule scanners do not have to
    prove semantic completeness; they only have to surface low-confidence or
    uncovered regions so the chain can audit and repair candidate graph gaps.
    """
    nodes = [
        node for node in candidate.get("deps_graph", {}).get("nodes", [])
        if isinstance(node, dict)
    ]
    primary_owners: Dict[str, List[str]] = {}
    primary_modules: Dict[str, List[str]] = {}
    config_owners: Dict[str, List[str]] = {}
    config_modules: Dict[str, List[str]] = {}
    module_signals: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        node_id = str(node.get("id") or "")
        metadata = node.get("metadata") or {}
        module_name = str(metadata.get("module") or "")
        if module_name:
            module_signals[module_name] = metadata.get("architecture_signals") or {}
        for primary in node.get("primary") or []:
            path = _repo_relpath(project_root, str(primary or ""))
            if not path:
                continue
            primary_owners.setdefault(path, []).append(node_id)
            if module_name:
                primary_modules.setdefault(path, []).append(module_name)
        for config_path in node.get("config") or []:
            path = _repo_relpath(project_root, str(config_path or ""))
            if not path:
                continue
            config_owners.setdefault(path, []).append(node_id)
            if module_name:
                config_modules.setdefault(path, []).append(module_name)

    relation_types_by_file: Dict[str, List[str]] = {}
    for rel in phase_result.get("typed_relations") or []:
        path = _repo_relpath(project_root, str(rel.get("source_file") or ""))
        if not path:
            continue
        relation_types_by_file.setdefault(path, []).append(str(rel.get("relation_type") or ""))

    rows: List[Dict[str, Any]] = []
    for row in phase_result.get("file_inventory") or []:
        path = _repo_relpath(project_root, str(row.get("path") or ""))
        if not path:
            continue
        file_kind = str(row.get("file_kind") or "unknown")
        scan_status = str(row.get("scan_status") or "")
        graph_nodes = sorted(set(primary_owners.get(path, []) + config_owners.get(path, [])))
        modules = sorted(set(primary_modules.get(path, []) + config_modules.get(path, [])))
        relation_counts = _count_values(relation_types_by_file.get(path, []))
        relation_total = sum(relation_counts.values())
        roles = sorted({
            role
            for module in modules
            for role in (module_signals.get(module, {}).get("roles") or [])
            if role
        })
        audit_reasons: List[str] = []
        recommended_action = "none"

        if file_kind == "source":
            if graph_nodes:
                coverage_status = "source_covered_by_candidate"
                if relation_total == 0:
                    audit_reasons.append("source_has_no_typed_relations")
                if roles == ["implementation"] or not roles:
                    audit_reasons.append("source_has_only_implementation_profile")
            else:
                coverage_status = "source_missing_candidate_node"
                audit_reasons.append("source_not_in_candidate_graph")
        elif file_kind in {"test", "doc"}:
            if scan_status == "secondary_attached" or row.get("attached_to"):
                coverage_status = f"{file_kind}_consumer_attached"
            else:
                coverage_status = f"{file_kind}_consumer_orphan_audit"
                audit_reasons.append(f"{file_kind}_not_attached_to_candidate")
        elif file_kind == "config":
            if scan_status == "config_attached" or graph_nodes:
                coverage_status = "config_attached"
            else:
                coverage_status = "config_pending_semantic_classification"
                audit_reasons.append("config_requires_ai_semantic_classification")
        elif file_kind == "type_contract":
            coverage_status = "type_contract_support"
        elif scan_status == "ignored" or str(row.get("decision") or "") == "ignore":
            coverage_status = "ignored_or_generated"
        elif str(row.get("decision") or "") == "pending" or scan_status in {"pending_decision", "orphan"}:
            coverage_status = "pending_pm_decision"
            audit_reasons.append("non_source_asset_requires_pm_decision")
        else:
            coverage_status = scan_status or "unknown"

        if audit_reasons:
            if file_kind == "source":
                recommended_action = "pm_relation_audit"
            elif file_kind in {"test", "doc"}:
                recommended_action = "pm_consumer_attachment_audit"
            elif file_kind == "config":
                recommended_action = "semantic_config_classification"
            else:
                recommended_action = "pm_file_classification"

        rows.append({
            "path": path,
            "file_kind": file_kind,
            "language": row.get("language") or "",
            "sha256": row.get("sha256") or "",
            "inventory_status": scan_status,
            "coverage_status": coverage_status,
            "graph_nodes": graph_nodes,
            "modules": modules,
            "roles": roles,
            "relation_counts": relation_counts,
            "audit_reasons": audit_reasons,
            "recommended_chain_action": recommended_action,
            "decision": row.get("decision") or "",
        })

    audit_rows = [row for row in rows if row["audit_reasons"]]
    summary = {
        "total_files": len(rows),
        "by_file_kind": _count_values([row["file_kind"] for row in rows]),
        "by_coverage_status": _count_values([row["coverage_status"] for row in rows]),
        "audit_reason_counts": _count_values([
            reason for row in rows for reason in row["audit_reasons"]
        ]),
        "pm_audit_required_count": len(audit_rows),
        "pm_audit_required_sample": [
            {
                "path": row["path"],
                "file_kind": row["file_kind"],
                "coverage_status": row["coverage_status"],
                "audit_reasons": row["audit_reasons"],
                "recommended_chain_action": row["recommended_chain_action"],
            }
            for row in audit_rows[:25]
        ],
    }
    return {
        "summary": summary,
        "rows": rows,
    }


def write_rebase_candidate_artifacts(
    project_root: str,
    phase_result: Dict[str, Any],
    *,
    out_dir: str,
    session_id: str = "",
    run_id: str = "",
) -> Dict[str, Any]:
    """Write graph.rebase.candidate.json and graph.rebase.review.json."""
    target_dir = Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    candidate = build_rebase_candidate_graph(
        project_root,
        phase_result,
        session_id=session_id,
        run_id=run_id,
    )
    candidate_path = target_dir / "graph.rebase.candidate.json"
    review_path = target_dir / "graph.rebase.review.json"
    coverage_ledger_path = target_dir / "graph.rebase.coverage-ledger.json"
    candidate_path.write_text(json.dumps(candidate, indent=2, ensure_ascii=False), encoding="utf-8")
    coverage_ledger = build_candidate_coverage_ledger(project_root, phase_result, candidate)
    coverage_ledger_path.write_text(
        json.dumps(coverage_ledger, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    nodes = candidate.get("deps_graph", {}).get("nodes", [])
    links = candidate.get("deps_graph", {}).get("links", [])
    ids = {str(n.get("id") or "") for n in nodes if isinstance(n, dict)}
    missing_links = [
        link for link in links
        if str(link.get("source") or "") not in ids or str(link.get("target") or "") not in ids
    ]
    duplicate_primary: Dict[str, List[str]] = {}
    for node in nodes:
        for primary in node.get("primary") or []:
            duplicate_primary.setdefault(primary, []).append(str(node.get("id") or ""))
    duplicate_primary = {
        path: owners for path, owners in duplicate_primary.items()
        if path and len(owners) > 1
    }
    by_layer: Dict[str, int] = {}
    by_kind: Dict[str, int] = {}
    for node in nodes:
        by_layer[str(node.get("layer") or "")] = by_layer.get(str(node.get("layer") or ""), 0) + 1
        kind = str((node.get("metadata") or {}).get("kind") or "implementation")
        by_kind[kind] = by_kind.get(kind, 0) + 1
    candidate_primaries = {
        str(primary).replace("\\", "/")
        for node in nodes
        for primary in (node.get("primary") or [])
        if primary
    }
    source_files = {
        str(row.get("path") or "")
        for row in (phase_result.get("file_inventory") or [])
        if row.get("file_kind") == "source"
    }
    source_missing = sorted(source_files - candidate_primaries)
    review = {
        "candidate_graph_path": str(candidate_path),
        "coverage_ledger_path": str(coverage_ledger_path),
        "candidate_node_count": len(nodes),
        "candidate_link_count": len(links),
        "by_layer": dict(sorted(by_layer.items())),
        "by_kind": dict(sorted(by_kind.items())),
        "duplicate_primary_files": duplicate_primary,
        "missing_link_count": len(missing_links),
        "missing_links": missing_links[:25],
        "architecture_summary": candidate.get("architecture_summary") or {},
        "source_coverage": {
            "source_file_count": len(source_files),
            "covered_source_count": len(source_files & candidate_primaries),
            "missing_source_count": len(source_missing),
            "missing_source_sample": source_missing[:25],
        },
        "coverage_ledger_summary": coverage_ledger.get("summary") or {},
        "phase_z_report_path": phase_result.get("report_path", ""),
        "run_id": run_id or phase_result.get("run_id", ""),
        "session_id": session_id,
    }
    review_path.write_text(json.dumps(review, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "candidate_graph_path": str(candidate_path),
        "review_path": str(review_path),
        "coverage_ledger_path": str(coverage_ledger_path),
        "review": review,
    }


# ---------------------------------------------------------------------------
# PR2a: DFS coloring from entry points
# ---------------------------------------------------------------------------

_ENTRY_DECORATORS_SET = frozenset(
    {"route", "app", "get", "post", "put", "delete", "patch", "cli"}
)
_MCP_HANDLER_PATTERNS = frozenset(
    {"mcp_tool", "server.tool", "server.resource", "server.prompt"}
)


def identify_entries(modules: Dict[str, ModuleInfo]) -> List[str]:
    """Detect entry-point functions from module metadata.

    Entry criteria:
    - Decorated with @route/@app/@get/@post/@put/@delete/@patch/@cli
    - MCP handler patterns (mcp_tool, server.tool, etc.)
    - __main__ guard or scripts/ path with __main__ block
    """
    entries: List[str] = []
    for _mod_name, mod_info in modules.items():
        is_script = "scripts/" in mod_info.path.replace("\\", "/")
        for func in mod_info.functions:
            if _is_entry_func(func, is_script):
                entries.append(func.qualified_name)
    return entries


def _is_entry_func(func: FunctionMeta, is_script: bool) -> bool:
    for dec in func.decorators:
        dec_lower = dec.lower()
        for pat in _ENTRY_DECORATORS_SET:
            if pat in dec_lower:
                return True
        for pat in _MCP_HANDLER_PATTERNS:
            if pat in dec_lower:
                return True
    if "__main__" in func.name or "__main__" in func.qualified_name:
        return True
    if is_script and func.is_entry:
        return True
    return False


def dfs_color_from_entries(
    edges: Dict[str, List[str]],
    entries: List[str],
    track_distance: bool = False,
) -> Tuple[Dict[str, Set[str]], Dict[str, int]]:
    """Perform DFS from each entry through strong call-graph edges.

    Args:
        edges: Strong call-graph edges (caller -> [targets]).
        entries: List of entry-point qualified names.
        track_distance: Reserved for future re-add of min_distance computation
            via in-DFS hashmap. Currently unused.

    Returns:
        (color_sets, color_count_map) where:
        - color_sets[entry_qname] = set of all reachable function qnames
        - color_count_map[fn_qname] = count of distinct entries reaching fn
    """
    color_sets: Dict[str, Set[str]] = {}
    color_count_map: Dict[str, int] = {}

    for entry in entries:
        visited: Set[str] = set()
        stack = [entry]
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            for target in edges.get(node, []):
                if target not in visited:
                    stack.append(target)
        color_sets[entry] = visited
        for fn in visited:
            color_count_map[fn] = color_count_map.get(fn, 0) + 1

    return color_sets, color_count_map


# ---------------------------------------------------------------------------
# PR3 R1/R10/R11: Driver function
# ---------------------------------------------------------------------------

CYCLE_ABORT_THRESHOLD = 30


def build_graph_v2_from_symbols(
    project_root: str,
    dry_run: bool = True,
    owner: Optional[str] = None,
    scratch_dir: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Orchestrate the full symbol-level topology pipeline.

    Calls: parse_production_modules, build_call_graph, tarjan_scc,
    score_function_layer, aggregate_functions_into_nodes,
    find_test_coverage, find_doc_coverage.

    If dry_run=True, writes to docs/dev/scratch/ and returns report_path.
    If dry_run=False, writes graph.v2.json and calls create_baseline.
    If >30 cycles detected, returns status='aborted'.
    """
    # Step 1: PR1 — parse + call graph + SCC
    from agent.governance.project_profile import discover_project_profile

    profile = discover_project_profile(project_root)
    modules = parse_production_modules(project_root, profile=profile)
    call_graph = build_call_graph(modules)
    sccs = tarjan_scc(call_graph.edges)

    # Handle cycles
    real_cycles = [scc for scc in sccs if len(scc) >= 2]

    # R10: Cycle abort threshold
    if len(real_cycles) > CYCLE_ABORT_THRESHOLD:
        return {
            "status": "aborted",
            "abort_reason": f"Too many cycles: {len(real_cycles)} exceeds threshold {CYCLE_ABORT_THRESHOLD}",
        }

    for scc in real_cycles:
        handle_cycle(scc, call_graph.all_functions, call_graph.edges)

    # Step 2a: DFS coloring from entries
    entry_qnames = identify_entries(modules)
    _color_sets, color_count_map = dfs_color_from_entries(
        call_graph.edges, entry_qnames
    )
    max_color_count = max(color_count_map.values()) if color_count_map else 0

    # Step 2: PR2 — scoring + aggregation
    # Build SCC index (topological order)
    scc_index: Dict[str, int] = {}
    for idx, scc in enumerate(sccs):
        for node in scc:
            scc_index[node] = idx

    layer_scores: Dict[str, int] = {}
    for qname in call_graph.all_functions:
        layer_scores[qname] = score_function_layer(
            qname, scc_index, call_graph.edges, call_graph.all_functions
        )

    nodes = aggregate_functions_into_nodes(modules, layer_scores)

    test_consumer_fanin = build_test_consumer_fanin_index(
        project_root,
        modules,
        profile=profile,
    )

    # Step 3: PR3 — coverage lookup
    for node in nodes:
        pf = node.get("primary_file", "")
        test_cov = find_test_coverage(project_root, pf, profile=profile)
        doc_cov = find_doc_coverage(project_root, pf, profile=profile)
        node["test_coverage"] = test_cov
        node["doc_coverage"] = doc_cov
    _attach_test_consumer_fanin_to_nodes(project_root, nodes, test_consumer_fanin)

    feature_clusters = synthesize_feature_clusters(
        project_root=project_root,
        modules=modules,
        call_graph=call_graph,
        sccs=sccs,
        nodes=nodes,
    )
    adapter_fallback_nodes = [
        node for node in nodes
        if node.get("source_kind") == "filetree_fallback"
    ]
    fallback_nodes = append_filetree_fallback_source_nodes(project_root, nodes, profile=profile)
    all_fallback_nodes = adapter_fallback_nodes + fallback_nodes
    _attach_test_consumer_fanin_to_nodes(project_root, all_fallback_nodes, test_consumer_fanin)
    if all_fallback_nodes:
        feature_clusters.extend(_fallback_feature_clusters(all_fallback_nodes))
        feature_clusters.sort(key=lambda c: c.get("cluster_fingerprint", ""))

    run_id = run_id or datetime.now(timezone.utc).strftime("phase-z-v2-%Y%m%dT%H%M%SZ")
    try:
        from agent.governance.reconcile_file_inventory import (
            build_file_inventory,
            summarize_file_inventory,
        )
        file_inventory = build_file_inventory(
            project_root=project_root,
            run_id=run_id,
            nodes=nodes,
            feature_clusters=feature_clusters,
        )
        file_inventory_summary = summarize_file_inventory(file_inventory)
    except Exception:
        file_inventory = []
        file_inventory_summary = {
            "total": 0,
            "by_kind": {},
            "by_status": {"error": 1},
            "pending_decision_count": 0,
            "pending_decision_sample": [],
        }

    graph_enrich_config_rules = _load_graph_enrich_config_rules(project_root)
    typed_relations = extract_typed_relations(
        project_root,
        modules,
        graph_enrich_config_rules=graph_enrich_config_rules,
    )
    typed_relations.extend(materialize_config_file_relations(
        project_root,
        nodes,
        file_inventory,
    ))
    try:
        file_inventory_summary = summarize_file_inventory(file_inventory)  # type: ignore[name-defined]
    except Exception:
        pass
    typed_relations = [r.__dict__ for r in _dedupe_typed_relations([
        TypedRelation(**rel) if isinstance(rel, dict) else rel
        for rel in typed_relations
    ])]
    function_call_facts = build_function_call_facts(
        modules,
        call_graph,
        graph_enrich_config_rules=graph_enrich_config_rules,
    )
    enrich_nodes_with_function_call_facts(nodes, function_call_facts)
    enrich_nodes_with_architecture_signals(nodes, typed_relations)
    architecture_graph = build_architecture_graph(nodes, typed_relations)
    module_dependency_edges = build_module_dependency_edges(modules, call_graph)

    # Step 4: Diff against existing
    diff_report = diff_against_existing_graph(project_root, nodes)

    if dry_run:
        # R3: Write dry-run artifact
        report_path = write_dry_run_artifact(
            project_root,
            nodes,
            diff_report,
            scratch_dir=scratch_dir,
            feature_clusters=feature_clusters,
            file_inventory=file_inventory,
            file_inventory_summary=file_inventory_summary,
            typed_relations=typed_relations,
            architecture_graph=architecture_graph,
            module_dependency_edges=module_dependency_edges,
        )
        return {
            "status": "ok",
            "run_id": run_id,
            "report_path": report_path,
            "node_count": len(nodes),
            "nodes": nodes,
            "feature_clusters": feature_clusters,
            "typed_relations": typed_relations,
            "function_call_facts": function_call_facts,
            "architecture_graph": architecture_graph,
            "module_dependency_edges": module_dependency_edges,
            "file_inventory": file_inventory,
            "file_inventory_summary": file_inventory_summary,
            "diff_report": diff_report,
        }
    else:
        # R5: Write graph.v2.json
        graph_path = write_graph_v2_json(project_root, nodes)

        # R11: Call create_baseline with scope_kind='symbol-bootstrap'
        try:
            from agent.governance.baseline_service import create_baseline
            from agent.governance.db import get_connection
            conn = get_connection("aming-claw")
            create_baseline(
                conn=conn,
                project_id="aming-claw",
                chain_version="",
                trigger="phase-z-v2",
                triggered_by=owner or "phase-z-v2",
                scope_kind="symbol-bootstrap",
            )
            conn.close()
        except Exception:
            pass  # Best-effort baseline creation

        return {
            "status": "ok",
            "run_id": run_id,
            "graph_path": graph_path,
            "node_count": len(nodes),
            "nodes": nodes,
            "feature_clusters": feature_clusters,
            "typed_relations": typed_relations,
            "function_call_facts": function_call_facts,
            "architecture_graph": architecture_graph,
            "module_dependency_edges": module_dependency_edges,
            "file_inventory": file_inventory,
            "file_inventory_summary": file_inventory_summary,
            "diff_report": diff_report,
        }
