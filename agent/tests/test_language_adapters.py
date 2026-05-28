"""Tests for the LanguageAdapter contract surface (cluster 84bbe649).

Locks down the contract published by ``agent/governance/language_adapters/``
in isolation from ``cluster_grouper`` so the surface remains testable when
the grouper is rewritten or replaced. Covers AC1-AC5 + adapter null-behavior
and the import-safe / stateless invariant from ``base.py``.

These tests are *additive* — they intentionally duplicate a few assertions
already in ``test_cluster_grouper.py`` because that file exercises adapters
indirectly via the grouper's internals; this file is the contract anchor.
"""
from __future__ import annotations

import ast
import os
import sys
from typing import get_type_hints

import pytest


# Ensure repo root is on sys.path so ``agent.*`` imports work when pytest
# is invoked from the project directory.
_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from agent.governance.language_adapters import (  # noqa: E402
    FileTreeAdapter,
    JavaScriptTypescriptAdapter,
    LanguageAdapter,
    PythonAdapter,
    RubyAdapter,
)


# ---------------------------------------------------------------------------
# AC1 — Protocol surface: LanguageAdapter exposes legacy and graph hooks
# ---------------------------------------------------------------------------

def test_ac1_protocol_surface_has_graph_construction_methods():
    """LanguageAdapter Protocol must declare legacy and graph-construction methods."""
    required = (
        "supports",
        "language",
        "classify_file",
        "collect_decorators",
        "find_module_root",
        "detect_test_pairing",
        "find_test_pairing",
        "parse_symbols",
        "parse_imports",
        "extract_relations",
    )
    for name in required:
        assert hasattr(LanguageAdapter, name), f"LanguageAdapter missing {name!r}"

    # And all in-tree implementations satisfy the runtime-checkable Protocol.
    assert isinstance(PythonAdapter(), LanguageAdapter)
    assert isinstance(JavaScriptTypescriptAdapter(), LanguageAdapter)
    assert isinstance(RubyAdapter(), LanguageAdapter)
    assert isinstance(FileTreeAdapter(), LanguageAdapter)


# ---------------------------------------------------------------------------
# AC2 — Package re-exports the three public symbols
# ---------------------------------------------------------------------------

def test_ac2_package_reexports_public_symbols():
    """LanguageAdapter, PythonAdapter, FileTreeAdapter importable from package root."""
    import agent.governance.language_adapters as pkg

    for sym in (
        "LanguageAdapter",
        "PythonAdapter",
        "JavaScriptTypescriptAdapter",
        "RubyAdapter",
        "FileTreeAdapter",
    ):
        assert hasattr(pkg, sym), f"language_adapters package missing {sym!r}"
        assert sym in getattr(pkg, "__all__", ()), f"{sym!r} not declared in __all__"


# ---------------------------------------------------------------------------
# AC3 — supports() dispatch contract
# ---------------------------------------------------------------------------

def test_ac3_supports_dispatch_contract():
    """PythonAdapter.supports gates on .py extension; FileTreeAdapter accepts anything."""
    py = PythonAdapter()
    ft = FileTreeAdapter()

    # PythonAdapter — .py-only.
    assert py.supports("foo.py") is True
    assert py.supports("foo.unknown") is False
    assert py.supports("foo.go") is False
    assert py.supports("") is False

    # FileTreeAdapter — language-agnostic conservative fallback.
    assert ft.supports("anything.go") is True
    assert ft.supports("foo.py") is True
    assert ft.supports("README") is True


# ---------------------------------------------------------------------------
# AC4 — collect_decorators surface contract
# ---------------------------------------------------------------------------

def test_ac4_collect_decorators_surface():
    """PythonAdapter pulls decorator names from AST; FileTreeAdapter returns []."""
    py = PythonAdapter()
    ft = FileTreeAdapter()

    src = (
        "@route\n"
        "def handler():\n"
        "    pass\n"
    )
    module = ast.parse(src)
    func = module.body[0]
    assert isinstance(func, ast.FunctionDef)
    assert py.collect_decorators(func) == ["route"]

    # @app.route — attribute chain
    src2 = "@app.route('/x')\ndef h():\n    pass\n"
    func2 = ast.parse(src2).body[0]
    assert py.collect_decorators(func2) == ["app.route"]

    # FileTreeAdapter knows nothing about decorators — always [].
    assert ft.collect_decorators(func) == []
    assert ft.collect_decorators(None) == []
    assert ft.collect_decorators("not-an-ast-node") == []


# ---------------------------------------------------------------------------
# AC5 — detect_test_pairing fallback behavior
# ---------------------------------------------------------------------------

def test_ac5_detect_test_pairing_fallback():
    """foo.py → tests/test_foo.py; existing test files → None; FileTreeAdapter → None."""
    py = PythonAdapter()
    ft = FileTreeAdapter()

    assert py.detect_test_pairing("agent/foo.py") == "tests/test_foo.py"
    # Source already a test → no further pairing.
    assert py.detect_test_pairing("test_foo.py") is None
    assert py.detect_test_pairing("agent/tests/test_foo.py") is None
    # Non-Python source → None.
    assert py.detect_test_pairing("foo.go") is None
    assert py.detect_test_pairing("") is None

    # FileTreeAdapter — null behavior unconditionally.
    assert ft.detect_test_pairing("foo.py") is None
    assert ft.detect_test_pairing("anything.go") is None
    assert ft.detect_test_pairing("") is None


def test_graph_hooks_return_policy_metadata_and_safe_defaults():
    py = PythonAdapter()
    ft = FileTreeAdapter()

    assert py.language() == "python"
    assert py.classify_file("agent/service.py") == {
        "file_kind": "source",
        "language": "python",
        "adapter": "python",
    }
    assert py.find_test_pairing("agent/service.py") == "tests/test_service.py"
    symbols = py.parse_symbols(
        "agent/service.py",
        "import os\nfrom agent.db import save\n\nclass Service:\n    pass\n\ndef run():\n    save()\n",
    )
    assert {"name": "Service", "kind": "class", "lineno": 4, "end_lineno": 5, "decorators": []} in symbols
    assert any(symbol["name"] == "run" and symbol["kind"] == "function" for symbol in symbols)
    imports = py.parse_imports("agent/service.py", "import os\nfrom agent.db import save\n")
    assert {"local": "os", "imported": "os", "kind": "import"} in imports
    assert {
        "local": "save",
        "imported": "agent.db.save",
        "kind": "from_import",
        "module": "agent.db",
        "name": "save",
        "level": 0,
    } in imports
    assert py.extract_relations("agent/service.py", "def run():\n    pass\n") == []

    assert ft.language() == ""
    assert ft.classify_file("web/src/App.tsx") == {
        "file_kind": "source",
        "language": "typescript",
        "adapter": "filetree",
    }
    assert ft.find_test_pairing("web/src/App.tsx") is None
    assert ft.parse_symbols("web/src/App.tsx", "export function App() {}") == []
    assert ft.parse_imports("web/src/App.tsx", "import React from 'react'") == []
    assert ft.extract_relations("web/src/App.tsx", "") == []


def test_python_adapter_preserves_relative_import_facts():
    py = PythonAdapter()

    imports = py.parse_imports(
        "agent/governance/graph_events.py",
        "from . import graph_snapshot_store as store\n"
        "from .graph_snapshot_store import ensure_schema\n"
        "from ..shared import helpers\n",
    )

    assert {
        "local": "store",
        "imported": "graph_snapshot_store",
        "kind": "from_import",
        "module": "",
        "name": "graph_snapshot_store",
        "level": 1,
    } in imports
    assert {
        "local": "ensure_schema",
        "imported": "graph_snapshot_store.ensure_schema",
        "kind": "from_import",
        "module": "graph_snapshot_store",
        "name": "ensure_schema",
        "level": 1,
    } in imports
    assert {
        "local": "helpers",
        "imported": "shared.helpers",
        "kind": "from_import",
        "module": "shared",
        "name": "helpers",
        "level": 2,
    } in imports


# ---------------------------------------------------------------------------
# Additional coverage — find_module_root package-boundary walk + invariants
# ---------------------------------------------------------------------------

def test_find_module_root_walks_package_boundary(tmp_path):
    """PythonAdapter walks up to the first non-__init__ package boundary."""
    # Layout:
    #   tmp/
    #     pkg/
    #       __init__.py
    #       sub/
    #         __init__.py
    #         leaf.py
    pkg = tmp_path / "pkg"
    sub = pkg / "sub"
    sub.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (sub / "__init__.py").write_text("")
    leaf = sub / "leaf.py"
    leaf.write_text("x = 1\n")

    py = PythonAdapter()
    root = py.find_module_root(str(leaf))
    # Must climb past sub/__init__ and pkg/__init__ to land on pkg
    # (the package root — its parent has no __init__.py).
    assert os.path.normpath(root) == os.path.normpath(str(pkg))

    # FileTreeAdapter degenerates to dirname.
    ft = FileTreeAdapter()
    assert os.path.normpath(ft.find_module_root(str(leaf))) == os.path.normpath(str(sub))


def test_adapters_are_stateless_and_import_safe():
    """base.py invariant: adapters MUST be import-safe and stateless.

    Two independent instances with identical inputs must yield identical outputs,
    and constructing an adapter must not require any I/O / arguments.
    """
    a = PythonAdapter()
    b = PythonAdapter()
    assert a.supports("x.py") == b.supports("x.py") is True
    assert a.detect_test_pairing("agent/foo.py") == b.detect_test_pairing("agent/foo.py")

    fa = FileTreeAdapter()
    fb = FileTreeAdapter()
    assert fa.supports("anything.go") == fb.supports("anything.go") is True
    assert fa.collect_decorators(None) == fb.collect_decorators(None) == []
