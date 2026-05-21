# Configuration Reference

Configuration in Aming Claw V1 has two different jobs:

- **Project metadata and runtime routing** tell governance how to register,
  scan, test, and route AI work for a project.
- **Graph/semantic adaptation rules** teach reconcile how to handle language
  and framework-specific evidence without editing the graph database directly.

Keep those layers separate. `.aming-claw.yaml` is the project runtime contract.
`.aming-claw/reconcile/semantic_enrichment.yaml` is the source-controlled rules
override for graph review context and semantic enrichment.

## V1 Configuration Files

| File | Role |
|------|------|
| [aming-claw-yaml.md](aming-claw-yaml.md) | `.aming-claw.yaml` project metadata: project id, language hint, excludes, testing, E2E suites, AI routing. |
| [semantic-enrichment.md](semantic-enrichment.md) | `.aming-claw/reconcile/semantic_enrichment.yaml` graph/semantic rule override used to adapt review context for different languages and frameworks. |
| [mcp-json.md](mcp-json.md) | `.mcp.json` MCP server configuration for editor/plugin sessions. |
| [role-permissions.md](role-permissions.md) | Advanced chain role permission schema and YAML migration plan. |

## Config As Graph Adaptation

Aming Claw does not try to solve every language and framework behavior by
hardcoding one global graph algorithm. The bottom graph builder extracts
deterministic evidence, and project-local config can refine how that evidence
is interpreted.

Typical examples:

- a TypeScript test must directly import the tested symbol before it becomes
  strong test coverage
- a Python path string should stay weak evidence instead of becoming a direct
  test binding
- schema-version string literals should not become runtime event edges
- framework-specific call syntax may need a registered predicate or upstream
  adapter before a YAML rule can express it safely

The safe path is:

1. AI semantic review or an observer finds suspicious graph context.
2. The AI emits a typed proposal, such as `graph_enrich_config_ops.v1`.
3. Governance prechecks and gates the proposal.
4. Human/observer review decides whether the rule is valid.
5. The accepted fix becomes source-controlled config or an upstream registered
   predicate/action.
6. Reconcile rebuilds the graph projection from committed source and rules.

AI proposals are never trusted state by themselves. Config rules are source
inputs; graph snapshots and semantic projections are derived.
