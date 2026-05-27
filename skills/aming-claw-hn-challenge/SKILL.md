---
name: aming-claw-hn-challenge
description: >-
  Public HN challenge entrypoint for Aming Claw. Use when a user asks to run,
  preview, present, or collect evidence for the multi-agent challenge: one
  observer coordinates multiple commit-bound workers, records a failed or
  interrupted worker, replays it from the same contract lineage, reconciles the
  target graph, and writes an audit self-review.
---

## REQUIRED FIRST READ

Before any response that uses this skill, in this exact order:

  ListMcpResourcesTool()
  ReadMcpResourceTool(uri="aming-claw://current-context")
  ReadMcpResourceTool(uri="aming-claw://skill")
  ReadMcpResourceTool(uri="aming-claw://graph-first")

current-context anchors project_id, governance URLs, and guardrails.
skill is the operating contract.
graph-first has copy-pasteable graph_query payload examples.

Common failures when these are skipped:
- Bootstrapping the wrong project.
- Calling task_create dev/pm instead of observer-led mf_parallel.v1.
- Using Grep on the aming-claw codebase instead of graph_query.
- Fabricating trace_id strings.
- Falling back to the older before/during/after case walkthrough instead of
  the HN challenge.

# Aming Claw HN Challenge

This is the public challenge entrypoint. The legacy
`aming-claw-hn-demo` skill remains as a compatibility alias, but this skill is
the one HN readers should see in the skill menu.

Run the demo as one observer coordinating multiple contracted workers against
the same commit-bound project graph. Do not ask the user to write orchestration
code.

## Challenge Shape

The run must show:

1. Worker A and Worker B receive contracts bound to the same commit.
2. Worker A passes and its diff is accepted as candidate evidence.
3. Worker B fails or is interrupted.
4. The observer replays Worker B from the same contract lineage and frozen
   commit.
5. The replay passes with a clean, disjoint diff.
6. Accepted work lands through an ordered Git merge.
7. The target graph reconciles once after accepted work lands.
8. The audit report explains why the same observer trusts or hesitates on the
   result.

Worker runtime is generic: Claude, Codex, scripted workers, or any compatible
local process can produce the evidence. For installed users, the default
observer is the current Claude Code or Codex session reading this skill.
Scripted workers are allowed as bounded worker runtimes or machine-verification
fallbacks so users do not need two AI subscriptions.

## Live AI Observer Prompt

For the public HN demo, the observer is the current AI session reading this
skill. Do not claim that
`node frontend/dashboard/scripts/e2e-hn-demo.mjs --sandbox-audit --observer claude`
or `--observer codex` launches that AI runtime. In the runner, `--observer` is
only a report label unless a separate install-audit container invokes the AI
CLI and produces its own transcript/report.

Use this prompt contract for a live AI observer run:

```text
I am the live AI observer for the Aming Claw HN challenge. I will not treat a
scripted runner label as proof that Claude or Codex executed the observer role.

Steps:
1. Read the required Aming Claw MCP resources.
2. Verify governance, dashboard, graph, backlog, and operations status.
3. Create or bootstrap an isolated fixture only if no safe project is selected.
4. Prove the fixture starts with an active graph and empty backlog/timeline.
5. Create the mf_parallel.v1 backlog contract myself.
6. Allocate or verify two disjoint worker contexts and fence tokens.
7. Run real graph_query calls and record returned trace_ids.
8. Record Worker A pass, Worker B failed/interrupted, and Worker B replay pass.
9. Run real tests and capture exit code/output.
10. Reconcile the target graph once after accepted work lands.
11. Write the audit report myself, including why I trust or hesitate on the
    result.

Allowed helper: use e2e-hn-demo.mjs for fixture setup, deterministic protocol
smoke, dashboard screenshots, or final machine verification. Do not use its
--observer flag as evidence that a live AI observer ran. The current session
must create or verify the backlog contract, timeline events, graph traces,
worker fences, test evidence, replay evidence, reconcile evidence, and audit
evaluation.
```

## Operator Flow

1. Baseline runtime:
   - Check runtime_status, graph_status, and graph_operations_queue when MCP is
     available.
   - If governance is offline, tell the user to run `aming-claw start` in a
     separate terminal. Do not silently start services.
   - If no target project exists, use the isolated fixture path instead of
     asking the user to invent a project id.

2. Preferred installed-user run:

   - Treat this current Claude Code or Codex session as the observer.
   - If no safe project is selected, use the fixture helper only to create an
     isolated project with an active graph and empty backlog/timeline:

     ```bash
     node frontend/dashboard/scripts/e2e-hn-demo.mjs --ensure-fixture --no-browser
     ```

   - Then this session must create or verify the contract, worker fences,
     graph traces, timeline events, tests, replay, reconcile, and audit report.
   - Use `--sandbox-audit --no-browser` only when the user explicitly asks for
     a release/CI machine-verification run, or after the live observer run as a
     cross-check.

3. Evidence requirements:
   - At least two worker contexts with disjoint `owned_files`.
   - Per-worker fence tokens allocated or verified before any `mf_subagent`
     graph query.
   - Real graph_query trace ids. Never fabricate `graph_query_trace_ids`.
   - Timeline events for dispatch, passing worker, failed/interrupted worker,
     replay attempt, verification, reconcile, and close-ready status.
   - Tests and reconcile evidence captured from real subprocess/API output.
   - Same-observer audit report with a rating and the reason for that rating.

4. Dashboard surfaces to show:
   - Graph: `/dashboard?project_id=<id>&view=graph`
   - Backlog timeline:
     `/dashboard?project_id=<id>&view=backlog&backlog=<backlog_id>`
   - Operations queue: `/dashboard?project_id=<id>&view=operations`
   - Review queue: `/dashboard?project_id=<id>&view=review`

5. Optional background only:
   - Use `aming-claw-hn-demo-before-work`,
     `aming-claw-hn-demo-during-work`, and
     `aming-claw-hn-demo-after-work` only if the user explicitly asks for the
     older case-story walkthrough.

## Evidence Summary

```text
HN challenge evidence
- Runtime: governance=<ok/offline>, dashboard=<ok/missing>, MCP=<ok/missing>, project_id=<id>
- Graph: snapshot=<id>, graph=<link/screenshot>, result=<claim>
- Workers: A=<pass evidence>, B=<failed/interrupted evidence>, replay=<pass evidence>
- Timeline: backlog=<link/screenshot>, lanes=<link/screenshot>, trace_ids=<resolvable ids>
- Merge/reconcile: commit=<sha>, operations=<link/screenshot>, result=<claim>
- Audit report: path=<latest.md/report>, same_observer_score=<score>, hesitation=<reason>
- Limitations: <offline services, missing install gates, manual screenshots, no live AI provider needed>
```
