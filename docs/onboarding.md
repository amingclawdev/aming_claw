# Aming Claw V1 Onboarding Guide

This guide describes the V1 path for using Aming Claw to govern another local
project. V1 is local-first: start governance, open the dashboard, explicitly
register a clean target project, build the graph, then use dashboard/MCP tools
to inspect, file backlog, and plan PR work.

## 1. Start Aming Claw

Install from the repository or plugin flow in [README.md](../README.md), then
verify the install before starting governance:

```bash
aming-claw plugin doctor
```

The doctor is read-only. It reports missing CLI, marketplace, manifest, or MCP
config issues; fix anything that reads `fail` before continuing.

Then start governance in a separate terminal:

```bash
aming-claw start
```

Open:

```text
http://localhost:40000/dashboard
```

The root path `/` is not the dashboard and may return `404`.

## 2. Register A Target Project

Project registration is explicit. Do not silently register a workspace just
because `/api/projects` is empty.

Use the dashboard Projects page first:

1. Choose or paste the target workspace path.
2. Review the exclude-path field. Confirm which generated, vendored, nested, or
   tool-owned directories should be excluded before graph build.
3. Give it a project name/id.
4. Click Bootstrap or Build graph.
5. Watch progress in Projects/Operations Queue.

Bootstrap mutates Aming Claw governance state: it writes project registry/DB
rows, scans the workspace, and creates a commit-bound graph snapshot. It uses
governance on port `40000`, not ServiceManager on `40101`.

API fallback:

```http
POST http://127.0.0.1:40000/api/project/bootstrap
```

Use common excludes for generated folders:

```text
node_modules, dist, build, .expo, .next, coverage
```

Also check for project-specific names that are not safe defaults, such as
`node`, `vendor`, generated SDK/client folders, local model downloads, embedded
example repositories, fixture clones, scratch worktrees, and large build/cache
roots. The dashboard bootstrap form requires this review before it submits. If
they should not become governed L4/L7 nodes, add them before first bootstrap:

```yaml
graph:
  exclude_paths:
    - "node"
    - "vendor"
    - "generated"
  ignore_globs:
    - "**/*.generated.*"
  nested_projects:
    mode: "exclude"
    roots:
      - "examples/fixture-app"
```

If the target workspace is a dirty git repo, commit/stash first. Dirty
worktree rejection is intentional because graph snapshots are commit-bound.

## 3. Project Config In V1

V1 stores most user project metadata in the Aming Claw project registry. It
should not default to creating or mutating `.aming-claw.yaml` in the target
project root unless the user explicitly chooses a source-controlled config.

For source-controlled projects that want a config file, see
[config/aming-claw-yaml.md](config/aming-claw-yaml.md). The key V1 sections are:

- `project_id` and `language`.
- `graph.exclude_paths` / `graph.ignore_globs`.
- `testing.e2e` suite metadata.
- `ai.routing`, especially the `semantic` provider/model.

## 4. First Useful Actions

After graph build:

1. Open Graph and inspect L1/L2/L3/L4/L7 hierarchy.
2. Select candidate nodes and review Files, Relations, Functions, and Problems.
3. Use AI or MCP graph queries for `function_index`, fan-in/fan-out, docs/tests,
   and bounded source excerpts.
4. File backlog rows with node ids, target files, tests, risk, and acceptance
   criteria.
5. For implementation, use Manual Fix in V1 unless the user explicitly asks to
   test experimental chain automation. The MF checklist
   (predeclare backlog → graph-first discovery → focused tests → Chain trailer
   commit → Update Graph → backlog close) lives in
   [skills/aming-claw/references/mf-sop.md](../skills/aming-claw/references/mf-sop.md).

## 5. AI Enrich

Configure the project's `semantic` provider/model in AI config before live
semantic jobs. OpenAI routes use Codex CLI (`codex`); Anthropic routes use
Claude Code CLI (`claude`). Version detection does not prove authentication.

> Cost expectation: AI Enrich subprocess-spawns the local CLI per node/edge,
> so token use scales linearly with the selection size. On a fresh project of a
> few hundred nodes, start with 5–10 nodes you actually care about, watch the
> Review Queue, and only run repository-wide enrichment after the routing and
> prompt template are tuned.

Recommended flow:

1. Select a node or edge.
2. Run AI Enrich.
3. Watch Operations Queue.
4. Review the proposed semantic memory.
5. Accept, reject, or retry in Review Queue.

`ai_complete` means a proposal exists. It is not trusted project memory until
reviewed and accepted.

## 6. Governance Hint

Governance Hint is the V1-safe graph correction path for orphan doc/test/config
files that already appear in snapshot file inventory.

It writes a source-controlled hint into the file, then requires:

1. Commit the hint.
2. Update Graph/reconcile.
3. Confirm the file is attached to the expected node.

It does not create nodes, rewrite ownership, move hierarchy edges, or edit
dependency/function-call relations.

## 7. What Is Not The V1 Default

- Auto-chain PM -> Dev -> Test -> QA -> Merge is experimental in V1.
- ServiceManager/executor degraded is not a dashboard/graph failure.
- Workflow acceptance graph tools (`wf_*`) are separate from the snapshot graph
  and require import before use.
- Telegram, Redis, dbservice, and full production deployment are advanced
  surfaces, not required for the local plugin MVP.
