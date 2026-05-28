"""RubyAdapter — deterministic graph adapter for Ruby/Sinatra source.

Conference-demo-grade Ruby support. The adapter is intentionally regex-based
and stdlib-only: no tree-sitter, prism, or network dependencies. It mirrors
the public surface of :class:`PythonAdapter` and
:class:`JavaScriptTypescriptAdapter` so Phase Z, the cluster grouper, and
the legacy graph generator can dispatch uniformly.

Scope of MVP best-effort parsing:
- ``module`` / ``class`` declarations (including ``class Foo < Bar``)
- instance methods (``def foo``) and class/singleton methods
  (``def self.foo``, ``def Foo.foo``)
- ``require`` and ``require_relative`` import facts
- block end-line tracking via an ``end``-counted line scan that skips strings,
  comments, here-docs, and postfix ``if/unless/while/until`` openers

Known limitations (see ``docs/ruby-demo/README.md``):
- ``find_test_pairing`` returns one conventional hint: ``spec/<rel>_spec.rb``.
  ``LanguageAdapter`` currently exposes ``Optional[str]``; callers needing
  the ``test/`` minitest variant should derive it from the spec hint.
- Heredocs, ``%w()`` literals, and metaprogrammed constants are not parsed.
- ``extract_relations`` returns ``[]`` — call/require edges are intentionally
  left to Phase Z so we do not emit speculative relations for the demo.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.governance.language_policy import DEFAULT_LANGUAGE_POLICY


_RUBY_SOURCE_EXTENSIONS = (".rb", ".rake")

# Opener detection. Anchored at start-of-line (with optional leading whitespace)
# so postfix expressions like ``return x if cond`` do not register as block
# openers. We do not include ``do`` here because ``do |x|`` blocks are very
# common as bare statement suffixes; counting them risks under-balancing
# ``end``s when source uses both ``{...}`` and ``do...end`` block forms.
_LINE_OPENER_RE = re.compile(
    r"^(?P<indent>\s*)(?P<kw>module|class|def|if|unless|while|until|for|case|begin)\b"
)
# Track only ``do``s that introduce a block on a line that isn't itself an opener
# (e.g. ``items.each do |item|``). ``do`` at end-of-line is the signal.
_DO_BLOCK_RE = re.compile(r"\bdo\b(?:\s*\|[^|]*\|)?\s*(?:#.*)?$")
_END_LINE_RE = re.compile(r"^\s*end\b")

_MODULE_RE = re.compile(r"^\s*module\s+(?P<name>[A-Z][A-Za-z0-9_:]*)")
_CLASS_RE = re.compile(r"^\s*class\s+(?P<name>[A-Z][A-Za-z0-9_:]*)")
_METHOD_RE = re.compile(
    r"""^\s*def\s+
        (?P<receiver>self|[A-Z][A-Za-z0-9_:]*)?\s*\.?\s*
        (?P<name>[A-Za-z_][A-Za-z0-9_!?=]*)
    """,
    re.VERBOSE,
)
_REQUIRE_RE = re.compile(
    r"""^\s*(?P<kind>require|require_relative)\s*
        \(?\s*['"](?P<spec>[^'"]+)['"]\s*\)?""",
    re.VERBOSE,
)

# Comment / string skipping for end-counting. We strip these per-line before
# counting openers/closers to reduce false positives from ``# end`` comments
# and from ``"end"`` literals.
_LINE_COMMENT_RE = re.compile(r"#.*$")
_DOUBLE_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
_SINGLE_STRING_RE = re.compile(r"'(?:[^'\\]|\\.)*'")
# Postfix conditionals do NOT open a block, even though the keyword matches
# the opener regex. We detect them by the keyword appearing after non-whitespace
# earlier in the same logical line.
_POSTFIX_KW_RE = re.compile(r"\S.+\b(if|unless|while|until)\b")


class RubyAdapter:
    """Ruby-specific implementation of the :class:`LanguageAdapter` Protocol."""

    # ------------------------------------------------------------------
    # Identity + classification
    # ------------------------------------------------------------------
    def supports(self, file_path: str) -> bool:
        if not file_path:
            return False
        return Path(file_path.replace("\\", "/")).suffix.lower() in _RUBY_SOURCE_EXTENSIONS

    def language(self) -> str:
        return "ruby"

    def classify_file(self, file_path: str) -> Dict[str, Any]:
        language = DEFAULT_LANGUAGE_POLICY.language_for_path(file_path)
        return {
            "file_kind": "source" if self.supports(file_path) else "",
            "language": language,
            "adapter": "ruby",
        }

    def collect_decorators(self, ast_node: Any) -> List[str]:
        # Ruby has no decorators; ``attr_accessor`` etc. are method calls
        # that we intentionally do not synthesize as decorator names.
        return []

    def find_module_root(self, file_path: str) -> str:
        if not file_path:
            return ""
        normalised = file_path.replace("\\", "/")
        return os.path.dirname(normalised)

    # ------------------------------------------------------------------
    # Test pairing
    # ------------------------------------------------------------------
    def detect_test_pairing(self, source_file: str) -> Optional[str]:
        return self.find_test_pairing(source_file)

    def find_test_pairing(self, source_file: str) -> Optional[str]:
        """Return a single conventional spec-file hint for *source_file*.

        ``LanguageAdapter`` returns ``Optional[str]`` — callers that want
        the minitest ``test/<rel>_test.rb`` variant should derive it from
        the returned spec hint by swapping ``spec/`` for ``test/`` and the
        ``_spec.rb`` suffix for ``_test.rb``.
        """
        if not source_file or not self.supports(source_file):
            return None
        rel = source_file.replace("\\", "/").lstrip("/")
        path = Path(rel)
        name = path.name
        # Already a Ruby test file — nothing to pair.
        if name.endswith("_spec.rb") or name.endswith("_test.rb"):
            return None
        stem = path.stem
        if not stem:
            return None
        parts = list(path.parts)
        # Drop a leading ``lib/`` so ``lib/foo/bar.rb`` → ``spec/foo/bar_spec.rb``.
        if parts and parts[0] == "lib":
            parts = parts[1:]
        # Replace the basename with the spec-suffixed variant.
        if parts:
            parts[-1] = f"{stem}_spec.rb"
        else:
            parts = [f"{stem}_spec.rb"]
        return "spec/" + "/".join(parts)

    # ------------------------------------------------------------------
    # Symbol + import parsing
    # ------------------------------------------------------------------
    def parse_symbols(self, file_path: str, source: str = "") -> List[Dict[str, Any]]:
        text = source or ""
        if not text:
            return []
        lines = text.splitlines()
        block_ends = _compute_block_ends(lines)

        symbols: List[Dict[str, Any]] = []
        for idx, raw_line in enumerate(lines):
            lineno = idx + 1
            stripped = _strip_strings_and_comments(raw_line)
            # Skip lines that are purely postfix conditionals — they share the
            # same leading keyword via _LINE_OPENER_RE but never open a block.
            if _is_postfix_conditional(stripped):
                continue

            mod_match = _MODULE_RE.match(stripped)
            if mod_match:
                symbols.append(_symbol(mod_match.group("name"), "module", lineno, block_ends.get(lineno, lineno)))
                continue

            cls_match = _CLASS_RE.match(stripped)
            if cls_match:
                symbols.append(_symbol(cls_match.group("name"), "class", lineno, block_ends.get(lineno, lineno)))
                continue

            method_match = _METHOD_RE.match(stripped)
            if method_match:
                receiver = (method_match.group("receiver") or "").strip()
                name = method_match.group("name")
                if receiver:
                    kind = "classmethod" if receiver == "self" else "singleton_method"
                    symbol_name = f"{receiver}.{name}"
                else:
                    kind = "method"
                    symbol_name = name
                symbols.append(_symbol(symbol_name, kind, lineno, block_ends.get(lineno, lineno)))
        return symbols

    def parse_imports(self, file_path: str, source: str = "") -> List[Dict[str, Any]]:
        text = source or ""
        if not text:
            return []
        imports: List[Dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for idx, raw_line in enumerate(text.splitlines()):
            stripped = _strip_strings_and_comments_keep_quotes(raw_line)
            match = _REQUIRE_RE.match(stripped)
            if not match:
                continue
            spec = (match.group("spec") or "").strip()
            if not spec:
                continue
            kind = match.group("kind")
            key = (kind, spec)
            if key in seen:
                continue
            seen.add(key)
            imports.append({
                "local": spec,
                "imported": spec,
                "specifier": spec,
                "kind": kind,
                "lineno": idx + 1,
            })
        return imports

    def extract_relations(
        self,
        file_path: str,
        source: str = "",
        *,
        symbols: Optional[List[Dict[str, Any]]] = None,
        imports: Optional[List[Dict[str, Any]]] = None,
    ) -> List[Dict[str, Any]]:
        # MVP: do not synthesize Ruby relation edges. Phase Z owns relation
        # construction; emitting speculative require/call edges here risks
        # polluting the demo graph.
        return []


def _symbol(name: str, kind: str, lineno: int, end_lineno: int) -> Dict[str, Any]:
    return {
        "name": name,
        "kind": kind,
        "lineno": int(lineno),
        "end_lineno": int(max(lineno, end_lineno)),
        "decorators": [],
    }


def _strip_strings_and_comments(line: str) -> str:
    """Return *line* with simple string literals and trailing comments removed."""
    s = _DOUBLE_STRING_RE.sub('""', line)
    s = _SINGLE_STRING_RE.sub("''", s)
    s = _LINE_COMMENT_RE.sub("", s)
    return s


def _strip_strings_and_comments_keep_quotes(line: str) -> str:
    """Like :func:`_strip_strings_and_comments` but preserves quoted bodies.

    Used by :func:`parse_imports` because the require specifier sits inside
    the quotes and must survive stripping.
    """
    return _LINE_COMMENT_RE.sub("", line)


def _is_postfix_conditional(stripped_line: str) -> bool:
    """True when ``if/unless/while/until`` appears after other tokens.

    Postfix forms (``do_thing if cond``) re-use opener keywords without
    opening a new block; we must not increment the end-tracking counter
    for them.
    """
    leading = stripped_line.lstrip()
    if not leading:
        return False
    # If the line *starts* with one of these keywords, it's a real opener.
    if any(leading.startswith(kw + " ") or leading == kw for kw in (
        "if", "unless", "while", "until", "module", "class", "def",
        "for", "case", "begin",
    )):
        return False
    return bool(_POSTFIX_KW_RE.search(stripped_line))


def _compute_block_ends(lines: List[str]) -> Dict[int, int]:
    """Map each opener line number to the best-effort line number of its ``end``.

    Uses a simple counter that increments on ``module``/``class``/``def``/
    other opener keywords (and trailing ``do`` blocks) and decrements on
    bare ``end`` lines. Strings and trailing comments are stripped before
    matching to avoid false positives. The mapping is best-effort: complex
    heredocs, ``%w()`` literals, and inline DSL may yield approximate ends.
    """
    stack: List[int] = []  # stack of opener line numbers (1-based)
    ends: Dict[int, int] = {}
    in_heredoc: Optional[str] = None
    in_block_comment = False

    for idx, raw in enumerate(lines):
        lineno = idx + 1

        # ``=begin`` / ``=end`` block comments — skipped wholesale.
        bare = raw.strip()
        if in_block_comment:
            if bare.startswith("=end"):
                in_block_comment = False
            continue
        if bare.startswith("=begin"):
            in_block_comment = True
            continue

        # Heredoc body — skip until terminator is encountered.
        if in_heredoc is not None:
            if raw.strip() == in_heredoc or raw.strip() == in_heredoc.lstrip("-~"):
                in_heredoc = None
            continue

        stripped = _strip_strings_and_comments(raw)
        # Detect a heredoc opener like ``<<~TEXT`` or ``<<EOF``; consume from
        # the next line until terminator. Conservative: only the simplest form.
        heredoc_match = re.search(r"<<[-~]?(?P<tag>[A-Z_][A-Z0-9_]*)", stripped)
        if heredoc_match:
            in_heredoc = heredoc_match.group("tag")

        # ``end`` first: a bare ``end`` line closes the innermost opener.
        if _END_LINE_RE.match(stripped) and not _is_postfix_conditional(stripped):
            if stack:
                opener_line = stack.pop()
                ends[opener_line] = lineno
            continue

        if _is_postfix_conditional(stripped):
            continue

        opener_match = _LINE_OPENER_RE.match(stripped)
        if opener_match:
            stack.append(lineno)
            continue

        # ``items.each do |x|`` style — opens a block without a leading keyword.
        if _DO_BLOCK_RE.search(stripped):
            stack.append(lineno)
            continue

    # Anything left open: anchor end_lineno to the file length so callers
    # get a defined value rather than a missing key.
    if stack:
        last = len(lines)
        for opener_line in stack:
            ends[opener_line] = max(opener_line, last)
    return ends


__all__ = ["RubyAdapter"]
