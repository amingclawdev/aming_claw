# Ruby Graph Demo

This demo validates Aming Claw's Ruby graph support against a real Ruby
library. The first recommended target is Sinatra because it is recognizable,
medium sized, and uses Ruby DSL patterns without the full weight of Rails.

## What This Adds

The Ruby support in this branch is deterministic and dependency-free. It does
not use tree-sitter or Prism. The adapter extracts stable facts that are useful
for graph navigation:

- Ruby source detection for `.rb` and `.rake`
- Ruby project detection from `Gemfile`, `Rakefile`, `config.ru`, and
  `*.gemspec`
- `module`, `class`, instance method, and `self.method` symbol extraction
- `require` and `require_relative` import facts
- Conventional spec pairing hints such as `lib/foo/bar.rb` ->
  `spec/foo/bar_spec.rb`

## Validate With Sinatra

```bash
cd /tmp
git clone --depth=1 https://github.com/sinatra/sinatra.git
aming-claw bootstrap --path /tmp/sinatra --name sinatra-ruby-demo
```

Then verify from an Aming Claw-enabled Claude Code or Codex session:

```text
graph_status(project_id="sinatra-ruby-demo")
graph_query(project_id="sinatra-ruby-demo", tool="find_node_by_path",
            args={"path":"lib/sinatra/base.rb"})
graph_query(project_id="sinatra-ruby-demo", tool="function_index",
            args={"query":"Sinatra Base route"})
```

Expected result: the graph should identify Ruby as a project language, resolve
`lib/sinatra/base.rb`, and expose Ruby module/class/method symbols in the
function index.

Local smoke result from this branch against Sinatra:

- Language detection: `ruby`
- Scanned files at depth 4: 157 total, 129 Ruby files
- File roles: 83 source, 57 test, 16 config, 1 entrypoint
- `lib/sinatra/base.rb`: 216 symbols, 16 import facts
- Test pairing hint: `spec/sinatra/base_spec.rb`

## Known Limits

This is conference-demo-grade Ruby support, not a complete Ruby parser.

- Metaprogrammed methods are not expanded.
- DSL declarations are only visible when they look like ordinary method calls
  or symbols.
- Heredocs and advanced literal forms are skipped conservatively.
- `find_test_pairing` returns one spec-style hint because the current
  `LanguageAdapter` protocol returns `Optional[str]`, not multiple candidates.

Those limits are intentional for the first slice: the graph should be useful
and reproducible without introducing a new parser dependency the night before a
Ruby audience sees it.
