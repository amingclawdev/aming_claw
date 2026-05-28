"""Tests for RubyAdapter — regex-based Ruby/Sinatra graph adapter.

Scope is intentionally narrow: lock in the public surface the rest of the
governance pipeline (Phase Z, cluster grouper, asset binding proposals)
consumes — ``supports``, ``classify_file``, ``parse_symbols``,
``parse_imports``, and ``find_test_pairing``. Heavier scenarios that depend
on heredocs / metaprogramming are documented as known limitations in
``docs/ruby-demo/README.md`` and are intentionally *not* asserted here.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from agent.governance.language_adapters import LanguageAdapter, RubyAdapter  # noqa: E402


# ---------------------------------------------------------------------------
# supports / classify_file / Protocol conformance
# ---------------------------------------------------------------------------

def test_ruby_adapter_satisfies_protocol():
    assert isinstance(RubyAdapter(), LanguageAdapter)


def test_supports_only_ruby_extensions():
    rb = RubyAdapter()
    assert rb.supports("app.rb") is True
    assert rb.supports("lib/tasks/db.rake") is True
    # Case-insensitive on suffix.
    assert rb.supports("Lib/Foo.RB") is True
    # Windows path style still resolves the suffix.
    assert rb.supports("lib\\foo.rb") is True
    # Non-Ruby suffixes and empty input.
    assert rb.supports("app.py") is False
    assert rb.supports("Gemfile") is False
    assert rb.supports("") is False


def test_classify_file_reports_ruby_metadata():
    rb = RubyAdapter()
    assert rb.classify_file("lib/foo.rb") == {
        "file_kind": "source",
        "language": "ruby",
        "adapter": "ruby",
    }
    # Non-Ruby paths still surface the language policy's view, but the
    # adapter must not pretend the file is Ruby source.
    classified = rb.classify_file("README.md")
    assert classified["file_kind"] == ""
    assert classified["adapter"] == "ruby"


def test_language_key_is_stable():
    assert RubyAdapter().language() == "ruby"


# ---------------------------------------------------------------------------
# parse_symbols — module / class / def / self.method
# ---------------------------------------------------------------------------

_SAMPLE_RUBY = """\
require 'sinatra'
require_relative './lib/store'

module Demo
  VERSION = '0.1.0'

  class Service
    def initialize(store)
      @store = store
    end

    def call(req)
      @store.fetch(req)
    end

    def self.build
      new(Store.new)
    end
  end

  class Store
    def Store.connect
      :ok
    end
  end

  def standalone_helper
    42
  end
end
"""


def test_parse_symbols_emits_module_class_method_and_self_method():
    rb = RubyAdapter()
    symbols = rb.parse_symbols("lib/demo.rb", _SAMPLE_RUBY)

    by_name = {(s["name"], s["kind"]) for s in symbols}
    assert ("Demo", "module") in by_name
    assert ("Service", "class") in by_name
    assert ("Store", "class") in by_name
    # Instance methods land as "method".
    assert ("initialize", "method") in by_name
    assert ("call", "method") in by_name
    assert ("standalone_helper", "method") in by_name
    # ``def self.build`` → classmethod, qualified as ``self.build``.
    assert ("self.build", "classmethod") in by_name
    # ``def Store.connect`` → singleton_method, qualified as ``Store.connect``.
    assert ("Store.connect", "singleton_method") in by_name


def test_parse_symbols_records_block_ends_for_modules_and_classes():
    rb = RubyAdapter()
    symbols = rb.parse_symbols("lib/demo.rb", _SAMPLE_RUBY)
    by_kind_name = {(s["kind"], s["name"]): s for s in symbols}

    module_sym = by_kind_name[("module", "Demo")]
    class_sym = by_kind_name[("class", "Service")]
    # end_lineno must be strictly greater than the opener line for these
    # multi-line blocks; the exact value is best-effort (see adapter docstring).
    assert module_sym["end_lineno"] > module_sym["lineno"]
    assert class_sym["end_lineno"] > class_sym["lineno"]
    # Decorators are always empty for Ruby.
    assert module_sym["decorators"] == []


def test_parse_symbols_empty_source_returns_empty_list():
    assert RubyAdapter().parse_symbols("lib/demo.rb", "") == []


def test_parse_symbols_skips_postfix_if_unless():
    """``return x if cond`` must not register as an opener / symbol."""
    rb = RubyAdapter()
    source = "def guard\n  return 0 if true\n  return 1 unless false\nend\n"
    symbols = rb.parse_symbols("lib/demo.rb", source)
    names = [(s["name"], s["kind"]) for s in symbols]
    assert ("guard", "method") in names
    # No phantom symbols for the postfix conditionals.
    assert all(n[0] == "guard" for n in names)


# ---------------------------------------------------------------------------
# parse_imports — require / require_relative
# ---------------------------------------------------------------------------

def test_parse_imports_handles_require_and_require_relative():
    rb = RubyAdapter()
    source = (
        "require 'sinatra'\n"
        "require \"json\"\n"
        "require_relative './lib/store'\n"
        "require_relative \"../shared/helpers\"\n"
    )
    imports = rb.parse_imports("app.rb", source)

    by_kind_spec = {(imp["kind"], imp["specifier"]) for imp in imports}
    assert ("require", "sinatra") in by_kind_spec
    assert ("require", "json") in by_kind_spec
    assert ("require_relative", "./lib/store") in by_kind_spec
    assert ("require_relative", "../shared/helpers") in by_kind_spec

    # Each import fact must carry the canonical fields consumed by Phase Z.
    for imp in imports:
        assert imp["local"] == imp["imported"] == imp["specifier"]
        assert imp["kind"] in {"require", "require_relative"}
        assert imp["lineno"] > 0


def test_parse_imports_dedupes_identical_requires():
    rb = RubyAdapter()
    source = "require 'sinatra'\nrequire 'sinatra'\n"
    imports = rb.parse_imports("app.rb", source)
    assert len(imports) == 1
    assert imports[0]["specifier"] == "sinatra"


def test_parse_imports_ignores_commented_requires():
    rb = RubyAdapter()
    source = "# require 'not-loaded'\nrequire 'real'\n"
    imports = rb.parse_imports("app.rb", source)
    specs = [imp["specifier"] for imp in imports]
    assert specs == ["real"]


def test_parse_imports_empty_source_returns_empty_list():
    assert RubyAdapter().parse_imports("app.rb", "") == []


# ---------------------------------------------------------------------------
# find_test_pairing — conventional ``spec/<rel>_spec.rb`` hint
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "source,expected",
    [
        ("lib/foo.rb", "spec/foo_spec.rb"),
        ("lib/sub/bar.rb", "spec/sub/bar_spec.rb"),
        ("app.rb", "spec/app_spec.rb"),
        # Already-test files should not pair to themselves.
        ("spec/foo_spec.rb", None),
        ("test/foo_test.rb", None),
        # Non-Ruby files yield nothing.
        ("app.py", None),
        ("", None),
    ],
)
def test_find_test_pairing(source, expected):
    assert RubyAdapter().find_test_pairing(source) == expected


def test_detect_test_pairing_matches_find_test_pairing():
    rb = RubyAdapter()
    assert rb.detect_test_pairing("lib/foo.rb") == rb.find_test_pairing("lib/foo.rb")
    assert rb.detect_test_pairing("spec/foo_spec.rb") is None


# ---------------------------------------------------------------------------
# extract_relations / collect_decorators — null-behavior anchors
# ---------------------------------------------------------------------------

def test_extract_relations_is_empty_for_mvp():
    """Phase Z owns Ruby relation edges; adapter must not synthesize any."""
    rb = RubyAdapter()
    assert rb.extract_relations("app.rb", "require 'sinatra'\nclass Foo\nend\n") == []


def test_collect_decorators_returns_empty():
    rb = RubyAdapter()
    assert rb.collect_decorators(None) == []
    assert rb.collect_decorators("attr_accessor :name") == []
