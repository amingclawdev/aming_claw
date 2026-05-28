"""language_adapters — pluggable per-language analysis adapters (CR1 R4).

Re-exports the public surface used by ``reconcile_phases.cluster_grouper``
and downstream consumers.
"""
from __future__ import annotations

from .base import LanguageAdapter
from .filetree_adapter import FileTreeAdapter
from .javascript_typescript_adapter import JavaScriptTypescriptAdapter
from .python_adapter import PythonAdapter
from .ruby_adapter import RubyAdapter

__all__ = [
    "LanguageAdapter",
    "PythonAdapter",
    "JavaScriptTypescriptAdapter",
    "RubyAdapter",
    "FileTreeAdapter",
]
