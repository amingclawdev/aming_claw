# Fear Before Work

## Fear

The common fear before an agent starts is that it will treat the repository as a
bag of text. It may edit a file that looks relevant, miss the real owner, skip
the test surface, or invent project structure from whatever happened to fit in
the prompt.

## Demo

Run the HN demo skill and choose the before-work case. The demo should open the
project dashboard on the graph view, select the target area, and show the
backlog row before any implementation work starts.

Expected dashboard pattern:

```text
http://localhost:40000/dashboard?project_id=<project_id>&view=graph
```

Optional screenshot slots:

```text
docs/hn-demo/screenshots/01-before-work-contract.png
docs/hn-demo/screenshots/02-before-work-graph.png
```

## Evidence

The visible evidence is the project fact layer before editing:

- a commit-bound graph snapshot for the selected project;
- node/file/function/test/doc/config context where available;
- a backlog row that names the requested work, target files, and acceptance
  criteria;
- runtime and graph status that distinguish core governance readiness from
  optional chain or executor readiness.

This is skill-guided and deterministic where possible. The case does not need a
live AI model to prove the mechanism: the graph, backlog row, and dashboard
state are local governance records.

## Why this works

Aming Claw puts a project fact layer in front of the agent. The stable V1 flow
is graph-first, backlog-first, then scoped manual-fix work. The graph is tied to
a commit, so dirty workspace guesses do not become project truth. Backlog rows
record intent and acceptance criteria before mutation.

Architecture references:

- [System Architecture](../../architecture.md)
- [Manual Fix SOP](../../governance/manual-fix-sop.md)
