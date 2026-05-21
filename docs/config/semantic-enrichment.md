# Semantic Enrichment And Graph Rule Config

This document describes the source-controlled config layer that lets Aming Claw
adapt graph review context for different languages, frameworks, and repository
conventions.

## Location

Project override:

```text
.aming-claw/reconcile/semantic_enrichment.yaml
```

Default bundled config:

```text
config/reconcile/semantic_enrichment.yaml
```

The loader deep-merges the project override on top of the bundled default. For
isolated tests, `RECONCILE_SEMANTIC_CONFIG` can point at an alternate base
config before the project override is applied.

## Why This Exists

The graph builder should stay language-neutral where possible. It extracts
files, functions, imports, calls, string evidence, tests, docs, config files,
and relations. Some repositories still need local rules because frameworks and
languages encode intent differently.

Examples:

- JavaScript and TypeScript tests often prove coverage through direct symbol
  imports.
- Python tests can mention paths or import broad modules without directly
  testing a TypeScript symbol.
- Event names, schema ids, CLI executable names, and config literals can look
  like graph edges unless a rule constrains them.
- Framework-specific calls may need registered predicates/actions before YAML
  can express the rule safely.

This config is part of the graph self-repair loop: suspicious review context
becomes a typed proposal, the proposal is reviewed, and the durable fix becomes
source-controlled config or an upstream registered function/predicate.

## Source Vs Derived State

The semantic enrichment config is source. It is committed with the project and
included in the graph rule fingerprint. Graph snapshots, semantic projections,
operations queue rows, and review queue overlays are derived from source and
reconcile output.

Do not treat an AI `graph_enrich_config` proposal as trusted config. It must
pass precheck, server validation, and observer/human review before it is written
or copied into source.

## Top-Level Shape

```yaml
execution_policy:
  worker_max_concurrency: 4
  worker_claim_batch_size: 4
  chunk_large_nodes: true
  chunk_context_mode: function_index

job_profiles:
  node:
    analyzer_role: reconcile_node_semantic_analyzer
  edge:
    analyzer_role: reconcile_edge_semantic_analyzer
  graph_structure:
    analyzer_role: reconcile_graph_structure_analyzer
  graph_enrich_config:
    analyzer_role: reconcile_graph_enrich_config_analyzer

graph_enrich_config_ops:
  rules: {}

graph_structure_ops:
  evidence_policy:
    dedupe_operations: true
```

Most projects only need `graph_enrich_config_ops.rules` and occasionally a
small `execution_policy` override for large-node semantic work.

## Graph Enrich Config Rules

`graph_enrich_config_ops.rules` refines how extracted evidence becomes graph
review context. Rules are evaluated against evidence contexts during reconcile.
They can reject, downgrade, tighten, or require stronger evidence before a
relation is treated as review truth.

Example: require a direct symbol import before a test-import fan-in becomes
strong test coverage.

```yaml
graph_enrich_config_ops:
  rules:
    tests.test_import_fanin.require_direct_symbol_import:
      op: tighten_rule
      edge: tests
      source_evidence: test_import_fanin
      action: require_direct_symbol_import
      downgrade_to: weak_tests
      reason: Require direct test symbol import and preserve weak test evidence.
      when:
        all:
          - predicate: source_evidence_is
            value: test_import_fanin
```

With this rule, a real TypeScript test that directly imports the target symbol
can remain strong coverage, while a Python file that merely mentions a path can
stay weak fan-in evidence or be ignored.

## Supported Rule Fields

| Field | Meaning |
|-------|---------|
| `op` | Rule operation such as `add_rule`, `tighten_rule`, `update_rule`, `downgrade_rule`, `promote_rule`, or `review_rule`. |
| `edge` | Relation family being constrained, such as `tests`, `calls`, `emits_event`, `consumes_event`, `imports`, or config-related edges. |
| `source_evidence` | Evidence type being matched, such as `test_import_fanin`, `string_literal`, `function_calls`, `import_only`, or weak-call resolver evidence. |
| `action` | Resulting action, such as `reject`, `drop`, `ignore`, `downgrade`, `promote`, or `require_direct_symbol_import`. |
| `downgrade_to` | Lower-confidence target such as `weak_tests`, `ignore`, or another supported relation target. |
| `when` | Predicate block. `when.all` is conjunction; every predicate must match. |
| `reason` | Human-readable rationale. Prefer linking the dogfood or review case that caused the rule. |

Rule operations are intentionally gated. A proposal that would weaken an
existing rule, add an underconstrained predicate, or bypass review is rejected
by the graph-enrich config gate.

## Supported Predicates

Current built-in predicates:

| Predicate | Context checked |
|-----------|-----------------|
| `source_evidence_is` | normalized evidence source |
| `raw_target_in` | raw extracted target token |
| `language_is` | source language |
| `call_syntax_is` | call syntax, normalized across aliases |
| `receiver_kind_in` | receiver classification for call evidence |

Predicate guardrails matter. For example, a weak-call resolver rule that only
matches `raw_target_in: add` is too broad unless it also constrains call syntax
or receiver kind. String-literal event rules should usually constrain specific
raw targets so broad literals do not suppress real events.

## Config Is Not Generated Code

Some findings cannot be expressed safely in YAML. The graph-enrich config gate
recognizes upstream proposal operations such as:

- `register_adapter`
- `register_function`
- `register_enrich_function`
- `register_predicate`
- `register_rule_predicate`

Those proposals are not written into the target project as generated code. They
should become upstream Aming Claw work: implement the adapter/predicate/action,
add tests, then expose the new behavior to project-local config.

Use YAML config when existing predicates/actions can express the project
behavior. Use registered code when a new language or framework feature requires
new deterministic evidence or a new safe predicate.

## Graph Structure Ops

`graph_structure_ops` is the companion gate for source-controlled graph
structure repairs. It covers reviewed operations such as:

- `add_edge`
- `suppress_edge`
- `move_file`

Accepted graph structure operations are materialized as source hint blocks and
take effect after commit plus Update Graph. Deleting the hint withdraws the
projected graph effect during reconcile.

## Review Flow

1. AI semantic review or observer graph inspection finds suspicious context.
2. The AI emits a structured proposal with schema
   `graph_enrich_config_ops.v1` or `graph_structure_ops.v1`.
3. The proposal includes self-precheck evidence.
4. Governance parses, normalizes, and validates the proposal.
5. Existing rules and predicate guardrails are checked.
6. Observer/human review decides whether the proposed change is safe.
7. Source-controlled config or hints are updated.
8. Commit, then Update Graph/reconcile.

This flow is what makes graph repair durable and replayable. The proposal is a
candidate; the committed config/hint plus reconcile output is the trusted path.

## Minimal Project Override

```yaml
graph_enrich_config_ops:
  rules:
    tests.test_import_fanin.require_direct_symbol_import:
      op: tighten_rule
      edge: tests
      source_evidence: test_import_fanin
      action: require_direct_symbol_import
      downgrade_to: weak_tests
      reason: Direct test coverage requires a real symbol import in this repo.
      when:
        all:
          - predicate: source_evidence_is
            value: test_import_fanin
```

Commit this file and run Update Graph before expecting graph queries, review
scope, or dashboard coverage to reflect the rule.
