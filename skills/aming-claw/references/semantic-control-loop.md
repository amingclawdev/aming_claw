# Semantic Control Loop

Semantic enrichment is a governed memory loop, not an AI write path.

## Contract

`ai_complete` means an AI provider produced output. It does not mean the output
is trusted memory. Accepted semantic memory exists only after Review Queue
approval and projection materialization.

The normal path is:

1. Select graph nodes or edges.
2. Queue semantic enrichment.
3. The AI session reads graph evidence through audited graph queries.
4. The AI emits structured output and runs the required local self-precheck.
5. Governance parses and validates the output.
6. Review Queue exposes the proposal to an observer.
7. The observer accepts semantic memory, rejects it, or routes durable repair
   through config/rules/hints/reconcile.

## Proposal Shape

Use small, typed payloads that can become workflow input:

```json
{
  "kind": "graph_enrich_config",
  "target": "tests edge",
  "evidence": ["cross-language path mention", "no direct TypeScript symbol import"],
  "operation": "downgrade_to_weak_tests",
  "precheck": "passed"
}
```

This shape is illustrative, not a full schema. The important property is that
the model output is machine-checkable and gated.

## Observer Decisions

Accept semantic enrichment when the payload describes memory or review context
and does not ask to mutate graph structure.

Reject or route separately when the payload proposes graph topology, config
changes, or rule changes. Durable graph repair belongs in source-controlled
hints/config/rules plus reconcile, not in semantic memory.

## Dogfood Note

During the May 2026 self-graph dogfood run, node semantic jobs completed as AI
outputs first, then Review Queue decisions materialized accepted semantic
memory. A server node proposal (`L7.142`) was accepted as semantic memory after
inspection because it carried no `graph_structure_ops`, no graph-enrich config
proposal, no suggested edges, and its self-check passed.
