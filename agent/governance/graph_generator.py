"""Graph generator — auto-generate acceptance graphs from codebase structure.

Scans a workspace directory, detects language/framework, and generates
a layered AcceptanceGraph with file-to-node mapping and dependency edges.

Layer assignment:
  L0 = config/CI files
  L1 = core/shared modules
  L2 = feature modules
  L3 = entrypoints/API
  L4 = tests
"""

import ast
import fnmatch
import json
import logging
import os
import posixpath
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

# Safety limits
MAX_NODES = 50

# File classification patterns
_CONFIG_EXTENSIONS = {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".env", ".gemspec"}
_CONFIG_NAMES = {
    "pyproject.toml", "package.json", "Cargo.toml", "go.mod",
    "Makefile", "Dockerfile", ".dockerignore", ".gitignore",
    ".github", ".aming-claw.yaml", ".aming-claw.json",
    # Ruby manifests: Gemfile/Rakefile/config.ru are DSL files but classify
    # as config so they map to L0 rather than appearing as production source.
    "Gemfile", "Rakefile", "config.ru", "Gemfile.lock",
}
_CI_DIRS = {".github", ".circleci", ".gitlab-ci"}

_TEST_PATTERNS_PY = ("test_", "_test.py")
_TEST_PATTERNS_GO = ("_test.go",)
_TEST_PATTERNS_TS = (".test.ts", ".test.tsx", ".test.js", ".test.jsx", ".spec.ts", ".spec.js")
_TEST_PATTERNS_RB = ("_spec.rb", "_test.rb")

_ENTRYPOINT_NAMES = {
    "__main__.py", "main.py", "app.py", "server.py", "cli.py",
    "index.ts", "index.js", "main.go", "main.rs",
    # Ruby/Sinatra entrypoints — ``config.ru`` is the Rack rackup file,
    # ``app.rb`` is the conventional Sinatra app entrypoint.
    "config.ru", "app.rb",
}

_CORE_DIR_NAMES = {"core", "shared", "common", "lib", "utils", "pkg", "internal"}

_DEFAULT_EXCLUDE = {
    "__pycache__", ".git", "node_modules", ".venv", "venv",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".tox",
    "target", ".next", ".nuxt", "coverage", ".eggs", "*.egg-info",
    ".claude", ".worktrees", "shared-volume", "runtime",
}


def _normalize_path(p: str) -> str:
    """Normalize path to forward slashes (posix) — AC10."""
    return p.replace("\\", "/")


def _is_excluded(rel_path: str, name: str, excludes: Set[str]) -> bool:
    rel = _normalize_path(rel_path).strip("/")
    lowered_rel = rel.lower()
    lowered_name = name.lower()
    for raw in excludes:
        pattern = _normalize_path(str(raw or "")).strip("/")
        if not pattern:
            continue
        lowered_pattern = pattern.lower()
        if lowered_name == lowered_pattern:
            return True
        if lowered_rel == lowered_pattern or lowered_rel.startswith(lowered_pattern + "/"):
            return True
        if fnmatch.fnmatch(lowered_name, lowered_pattern) or fnmatch.fnmatch(lowered_rel, lowered_pattern):
            return True
    return False


def _is_test_file(filename: str) -> bool:
    """Check if a file is a test file by naming convention."""
    lower = filename.lower()
    if lower.startswith("test_") and lower.endswith(".py"):
        return True
    for pat in _TEST_PATTERNS_GO + _TEST_PATTERNS_TS + _TEST_PATTERNS_RB:
        if lower.endswith(pat):
            return True
    if lower.endswith("_test.py"):
        return True
    return False


def _is_config_file(filepath: str, filename: str) -> bool:
    """Check if a file is a config/CI file."""
    if filename in _CONFIG_NAMES:
        return True
    _, ext = os.path.splitext(filename)
    if ext in _CONFIG_EXTENSIONS:
        return True
    parts = _normalize_path(filepath).split("/")
    for part in parts:
        if part in _CI_DIRS:
            return True
    return False


def _is_entrypoint(filename: str) -> bool:
    """Check if a file is an entrypoint/API file."""
    return filename in _ENTRYPOINT_NAMES


def _is_core_module(filepath: str) -> bool:
    """Check if file is in a core/shared directory."""
    parts = _normalize_path(filepath).split("/")
    for part in parts:
        if part.lower() in _CORE_DIR_NAMES:
            return True
    return False


# ============================================================
# Public API
# ============================================================


def detect_language(workspace_path: str) -> str:
    """Detect primary language from project marker files.

    Returns: 'python' | 'javascript' | 'typescript' | 'rust' | 'go' | 'ruby' | 'unknown'
    """
    ws = Path(workspace_path)

    if (ws / "pyproject.toml").exists() or (ws / "setup.py").exists():
        return "python"
    if (ws / "Cargo.toml").exists():
        return "rust"
    if (ws / "go.mod").exists():
        return "go"
    if (ws / "tsconfig.json").exists():
        return "typescript"
    if (ws / "package.json").exists():
        return "javascript"
    if (ws / "Gemfile").exists() or any(ws.glob("*.gemspec")):
        return "ruby"
    return "unknown"


def scan_codebase(
    workspace_path: str,
    scan_depth: int = 3,
    exclude_patterns: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Walk directory tree up to scan_depth, returning file metadata.

    Each entry: {"path": "relative/posix/path", "type": "source|test|config|entrypoint"}

    Args:
        workspace_path: Root directory to scan.
        scan_depth: Maximum directory depth to traverse.
        exclude_patterns: Additional directory names to exclude.
    """
    ws = Path(workspace_path).resolve()
    excludes = set(_DEFAULT_EXCLUDE)
    if exclude_patterns:
        excludes.update(exclude_patterns)

    files: List[Dict[str, Any]] = []

    def _walk(current: Path, depth: int):
        if depth > scan_depth:
            return
        try:
            entries = sorted(current.iterdir())
        except PermissionError:
            return

        for entry in entries:
            rel = _normalize_path(str(entry.relative_to(ws)))
            name = entry.name

            # Skip excluded directories/files.  Configured entries may be
            # simple names ("node_modules"), path prefixes ("examples/demo"),
            # or glob patterns ("*.egg-info").
            if _is_excluded(rel, name, excludes):
                continue

            if entry.is_dir():
                _walk(entry, depth + 1)
            elif entry.is_file():
                _, ext = os.path.splitext(name)
                # Only include source-like files
                if ext not in {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs",
                               ".rb", ".rake",
                               ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg",
                               ".md", ".sh"} and name not in {"Gemfile", "Rakefile", "config.ru"}:
                    continue

                if _is_test_file(name):
                    ftype = "test"
                elif _is_config_file(rel, name):
                    ftype = "config"
                elif _is_entrypoint(name):
                    ftype = "entrypoint"
                else:
                    ftype = "source"

                files.append({"path": rel, "type": ftype, "name": name})

    _walk(ws, 0)
    return files


def _parse_python_imports(filepath: str) -> List[str]:
    """Parse Python imports from a file using ast.parse.

    Returns list of top-level module names imported.
    """
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
    except (SyntaxError, ValueError, OSError):
        return []

    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module.split(".")[0])
    return imports


def _assign_layer(file_info: Dict[str, Any]) -> str:
    """Assign a layer (L0-L4) based on file type and path."""
    ftype = file_info["type"]
    if ftype == "config":
        return "L0"
    if ftype == "test":
        return "L4"
    if ftype == "entrypoint":
        return "L3"
    if _is_core_module(file_info["path"]):
        return "L1"
    return "L2"


def _group_files_into_nodes(
    files: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Group scanned files into node definitions.

    Groups by parent directory, with each directory becoming a node.
    Test files are attached to their corresponding source nodes.
    """
    # Group by directory
    dir_groups: Dict[str, List[Dict[str, Any]]] = {}
    for f in files:
        parent = posixpath.dirname(f["path"]) or "root"
        dir_groups.setdefault(parent, []).append(f)

    nodes = []
    node_idx = 0

    for dir_path, group_files in sorted(dir_groups.items()):
        primary = []
        test_files = []
        secondary = []
        layers = set()

        for f in group_files:
            layer = _assign_layer(f)
            layers.add(layer)
            if f["type"] == "test":
                test_files.append(f["path"])
            elif f["type"] == "config":
                secondary.append(f["path"])
            else:
                primary.append(f["path"])

        if not primary and not secondary:
            # Test-only directories — attach tests but still create node
            if test_files:
                primary = test_files
                test_files = []
                layers = {"L4"}

        if not primary and not secondary and not test_files:
            continue

        # Determine predominant layer
        if "L4" in layers and len(layers) == 1:
            layer = "L4"
        elif "L3" in layers:
            layer = "L3"
        elif "L0" in layers and not primary:
            layer = "L0"
        elif "L1" in layers:
            layer = "L1"
        else:
            layer = "L2"

        # Node ID: L{layer}.{index}
        layer_num = int(layer[1:])
        node_idx += 1
        node_id = f"{layer}.{node_idx}"

        # Title from directory
        title = dir_path.replace("/", " / ") if dir_path != "root" else "Project Root"

        nodes.append({
            "node_id": node_id,
            "title": title,
            "layer": layer,
            "primary": primary,
            "test": test_files,
            "secondary": secondary,
        })

    return nodes


def _build_dependency_edges(
    nodes: List[Dict[str, Any]],
    workspace_path: str,
    language: str,
) -> List[Tuple[str, str]]:
    """Build dependency edges between nodes.

    For Python: uses ast.parse to find import relationships.
    For other languages: uses directory-structure proximity as fallback.
    """
    # Build file -> node_id mapping
    file_to_node: Dict[str, str] = {}
    for node in nodes:
        for f in node["primary"]:
            file_to_node[f] = node["node_id"]
        for f in node.get("test", []):
            file_to_node[f] = node["node_id"]

    # Build module name -> node mapping (for Python)
    module_to_node: Dict[str, str] = {}
    if language == "python":
        for node in nodes:
            for f in node["primary"]:
                if f.endswith(".py"):
                    # Convert file path to module name
                    mod = f.replace("/", ".").replace(".py", "")
                    parts = mod.split(".")
                    for i in range(len(parts)):
                        module_to_node[".".join(parts[i:])] = node["node_id"]
                    # Also register just the filename stem
                    stem = parts[-1] if parts else ""
                    if stem and stem != "__init__":
                        module_to_node[stem] = node["node_id"]

    edges: Set[Tuple[str, str]] = set()
    ws = Path(workspace_path).resolve()

    if language == "python":
        for node in nodes:
            for f in node["primary"]:
                if not f.endswith(".py"):
                    continue
                full_path = str(ws / f.replace("/", os.sep))
                imports = _parse_python_imports(full_path)
                for imp in imports:
                    target_node = module_to_node.get(imp)
                    if target_node and target_node != node["node_id"]:
                        edges.add((target_node, node["node_id"]))
    else:
        # Directory-structure fallback: parent dirs depend on child dirs
        # Core modules (L1) are depended upon by feature modules (L2)
        l1_nodes = [n["node_id"] for n in nodes if n["layer"] == "L1"]
        for node in nodes:
            if node["layer"] in ("L2", "L3"):
                for l1 in l1_nodes:
                    edges.add((l1, node["node_id"]))

    return list(edges)


def _infer_doc_associations(
    nodes: List[Dict[str, Any]],
    workspace_path: str,
) -> List[Dict[str, Any]]:
    """Infer doc associations for graph nodes by matching docs/ files.

    Returns candidate associations flagged with inferred=True (P4/P5).
    Candidates require human confirmation before being added to the graph.

    Match strategies (by confidence):
      0.9 — exact stem match (e.g. reconcile.py ↔ reconcile.md)
      0.5 — partial stem overlap (e.g. auto_chain.py ↔ chain-design.md)
      0.3 — keyword match in doc's first 500 chars
    """
    ws = Path(workspace_path)
    docs_dir = ws / "docs"
    if not docs_dir.is_dir():
        return []

    # Collect all .md files under docs/
    doc_files: List[str] = []
    for root, _dirs, files in os.walk(str(docs_dir)):
        for fname in files:
            if fname.endswith(".md"):
                rel = _normalize_path(str(Path(root, fname).relative_to(ws)))
                doc_files.append(rel)

    candidates: List[Dict[str, Any]] = []

    for node in nodes:
        node_id = node.get("node_id", "")
        # Extract stems from primary files
        primary_stems: Set[str] = set()
        for pf in node.get("primary", []):
            stem = Path(pf).stem.lower().replace("_", "-")
            if stem and stem != "__init__":
                primary_stems.add(stem)
                # Also add without common prefixes/suffixes
                for prefix in ("test-", "test_"):
                    if stem.startswith(prefix):
                        primary_stems.add(stem[len(prefix):])

        if not primary_stems:
            continue

        for doc_path in doc_files:
            doc_stem = Path(doc_path).stem.lower().replace("_", "-")
            best_confidence = 0.0
            best_reason = ""

            # Strategy 1: exact stem match
            if doc_stem in primary_stems:
                best_confidence = 0.9
                best_reason = f"exact stem match: {doc_stem}"
            else:
                # Strategy 2: partial overlap (substring or shared word parts)
                for ps in primary_stems:
                    if ps in doc_stem or doc_stem in ps:
                        if len(min(ps, doc_stem, key=len)) >= 3:
                            best_confidence = max(best_confidence, 0.5)
                            best_reason = f"partial overlap: {ps} ~ {doc_stem}"
                    else:
                        # Word-level overlap: split on - and check shared words
                        ps_words = set(ps.split("-"))
                        doc_words = set(doc_stem.split("-"))
                        shared = ps_words & doc_words - {"", "the", "a", "an"}
                        if shared and len(shared) >= 1 and any(len(w) >= 3 for w in shared):
                            best_confidence = max(best_confidence, 0.5)
                            best_reason = f"word overlap: {shared} in {ps} ~ {doc_stem}"

                # Strategy 3: keyword match in doc content
                if best_confidence < 0.5:
                    try:
                        full = ws / doc_path.replace("/", os.sep)
                        with open(str(full), "r", encoding="utf-8", errors="ignore") as f:
                            head = f.read(500).lower()
                        for ps in primary_stems:
                            if ps.replace("-", "_") in head or ps.replace("-", " ") in head:
                                best_confidence = max(best_confidence, 0.3)
                                best_reason = f"keyword '{ps}' in first 500 chars"
                                break
                    except OSError:
                        pass

            if best_confidence > 0:
                candidates.append({
                    "node_id": node_id,
                    "doc_path": doc_path,
                    "confidence": best_confidence,
                    "reason": best_reason,
                    "inferred": True,
                })

    return candidates


def generate_graph(
    workspace_path: str,
    scan_depth: int = 3,
    exclude_patterns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Generate a layered AcceptanceGraph from codebase structure.

    Returns:
        {
            "graph": AcceptanceGraph instance,
            "node_count": int,
            "edge_count": int,
            "layers": {"L0": n, "L1": n, ...},
            "warning": optional str if >50 nodes,
            "code_doc_map": dict mapping code paths to doc paths,
        }
    """
    from .graph import AcceptanceGraph
    from .models import NodeDef

    language = detect_language(workspace_path)
    files = scan_codebase(workspace_path, scan_depth, exclude_patterns)
    raw_nodes = _group_files_into_nodes(files)

    warning = None
    if len(raw_nodes) > MAX_NODES:
        warning = f"Codebase produced {len(raw_nodes)} nodes, capped at {MAX_NODES}"
        raw_nodes = raw_nodes[:MAX_NODES]

    # Build dependency edges
    edges = _build_dependency_edges(raw_nodes, workspace_path, language)

    # Create AcceptanceGraph
    graph = AcceptanceGraph()

    # Add nodes
    for node_info in raw_nodes:
        node_def = NodeDef(
            id=node_info["node_id"],
            title=node_info["title"],
            layer=node_info["layer"],
            verify_level=int(node_info["layer"][1:]) + 1,
            primary=node_info["primary"],
            secondary=node_info.get("secondary", []),
            test=node_info.get("test", []),
        )
        attrs = node_def.to_dict()
        attrs.pop("gates", None)
        graph.G.add_node(node_def.id, **attrs)

    # Add edges
    edge_count = 0
    for src, dst in edges:
        if graph.G.has_node(src) and graph.G.has_node(dst):
            graph.G.add_edge(src, dst)
            edge_count += 1

    # Layer stats
    layers: Dict[str, int] = {}
    for node_info in raw_nodes:
        layer = node_info["layer"]
        layers[layer] = layers.get(layer, 0) + 1

    # Build code_doc_map from node structure
    code_doc_map: Dict[str, List[str]] = {}
    for node_info in raw_nodes:
        for f in node_info["primary"]:
            # Map source files to their test files as related docs
            related = list(node_info.get("test", []))
            if related:
                code_doc_map[f] = related

    # Infer doc associations (P4 candidates)
    inferred_docs = _infer_doc_associations(raw_nodes, workspace_path)

    result: Dict[str, Any] = {
        "graph": graph,
        "node_count": len(raw_nodes),
        "edge_count": edge_count,
        "layers": layers,
        "code_doc_map": code_doc_map,
        "inferred_docs": inferred_docs,
    }
    if warning:
        result["warning"] = warning

    return result


# ---------------------------------------------------------------------------
# Public alias for CR1 — language_adapters.python_adapter relies on the same
# AST-based import parser.  Kept as a one-line re-export to minimise blast
# radius (CR1 R10).
# ---------------------------------------------------------------------------
parse_python_imports = _parse_python_imports


def save_graph_atomic(graph, path: str) -> None:
    """Save graph to path atomically via temp-file-then-rename (R7)."""
    import networkx as nx

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "version": 1,
        "deps_graph": nx.node_link_data(graph.G),
        "gates_graph": nx.node_link_data(graph.gates_G),
    }

    # Write to temp file in same directory, then rename
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent), suffix=".tmp", prefix="graph_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # Atomic rename (same filesystem)
        os.replace(tmp_path, str(target))
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
