# Observer Operation Standards

## V1 Default Implementation Mode

The V1 implementation default is observer-led Manual Fix. Ordinary
implementation starts from an MF backlog row and, when parallel help is needed,
uses local Codex subagents as bounded `mf_sub` workers under an
`mf_parallel.v1` contract.

Subagents are governed by the MF backlog row, contract, file/worktree fence,
and timeline evidence. They stop at `review_ready` or `waiting_merge` with
structured evidence; they do not merge, push, release gates, activate graph
refs, close backlog, delete worktrees, or mutate merge queues.

Governance `task_create` dev/test/qa/merge and executor release are not the V1
default implementation entrypoint. Use the chain flow below only when the user
explicitly asks to test chain automation or a documented experiment requires
it. Chain trailers are MF audit anchors on commits; they do not mean
auto-chain execution is active.

## Principles

1. **Observer uses the governance control plane, not direct DB access**: All operations go through MCP tools or the REST API — do not read or write the DB directly.
2. **Look before you act at every step**: Before claiming, you must inspect first (search memory + context + queue), then decide.
3. **For explicit chain operation, only `complete` triggers progression**: Do not use `release` as a substitute for `complete`. `release` only puts the task back in the queue for the executor; `complete` triggers the next auto-chain stage.
4. **`complete` status can only be `succeeded`**: Do not use `failed` to cancel a task (it triggers a retry). If you truly need to abandon a task, use `complete(succeeded)` with a note.
5. **Observer recovery powers are limited to governance repair**: Observer may repair graph runtime state through governance APIs, but must not directly mark nodes as `testing`, `t2_pass`, or `qa_pass` outside the normal chain.

---

## Route-Token Gate For Protected Mutations

High-risk governance mutations must carry route-owned evidence. The protected
external actions are implementation-worker `task_create`, mutation-bearing
`task_complete`, merge queue writes, external merge-result recording, live merge
execution, `backlog_close`, and release-gate checks. Read-only status/backlog,
graph, and dry-run merge planning queries remain usable without a `route_token`.

A valid `route_token` must include `route_context_hash`, `prompt_contract_id`,
`caller_role`, an allowed action, matching project/backlog/task scope where
applicable, `expires_at`, and `evidence_refs`. If the observer is using an
explicit manual-fix or same-worktree exception, pass `route_waiver` with an
accepted decision, waiver type, reason, matching action/scope, and timeline
evidence. Governance records accepted route-token or waiver evidence into the
task timeline for later audit.
For route context consumption, timeline evidence must carry the same required
identity (`route_context_hash`, `prompt_contract_id`) plus public-safe
`visible_injection_manifest_hash` or `visible_injection_manifest`.
`prompt_contract_hash` may be supplied for propagation/comparison, but is not
required route identity.

## Advanced Chain Flow: Coordinator Takeover

### Prerequisites

```
observer_mode: ON
executor: scale=0 (avoid auto-claim)
queue: cleared (no orphan tasks)
```

### Step 1 — Coordinator task creation and inspect

```
[Observer action]
task_create(type="task", prompt="user request content")
→ automatically enters observer_hold

task_list() → confirm task is in observer_hold
```

**Inspect (required):**
- Search related memory: `GET /api/mem/{pid}/search?q=<keyword>`
- Check active queue: `task_list()`
- Check rule engine result: `rule_decision` in task metadata

**Output for user to review:**
- Relevant pitfalls / decisions / patterns found
- Rule engine decision (new / retry / conflict)
- Suggested next action

### Step 2 — User review

Wait for user confirmation:
- Did memory recall miss any critical information?
- Is the rule decision correct (new vs retry)?
- Does the task prompt need modification?

### Step 3 — Coordinator execution

```
[Observer action]
task_claim(worker_id="observer")
→ Execute coordinator logic:
  - Decide which task type to create (PM / direct reply / reject)
  - If creating PM: define target_files, acceptance_criteria, related_nodes
task_complete(status="succeeded", result={coordinator_output})
→ Triggers auto-chain → generates PM task (because observer_mode=ON, enters observer_hold)
```

---

## Recovery-Only Node Governance

Use these APIs only when governance state is corrupted or runtime node state is missing.

### Allowed

- `POST /api/wf/{project_id}/import-graph`
  - Observer may use this only with a non-empty `reason`
  - Purpose: restore `graph.json` and initialize runtime node state
- `POST /api/wf/{project_id}/observer-sync-node-state`
  - Observer may use this only with a non-empty `reason`
  - Purpose: rebuild `node_state` from existing `graph.json`

### Forbidden

- Do not use Observer to directly push nodes to `testing`
- Do not use Observer to directly push nodes to `t2_pass`
- Do not use Observer to directly push nodes to `qa_pass`
- Do not use `node-update` or `node-batch-update` as a substitute for verification
- Do not read or write governance DB directly

### Required audit posture

Every recovery action must include:

- project id
- reason
- source graph path if importing
- clear statement that the action is a governance repair, not feature acceptance

---

## Advanced Chain Flow: PM Stage Takeover

### Step 4 — PM task inspect

```
task_list() → find the newly generated PM task (observer_hold state)

[Inspect]
- Read PM task prompt (PRD spec generated by auto-chain)
- Confirm target_files coverage is correct
- Confirm acceptance_criteria is complete
- Confirm related_nodes correspond to the correct acceptance graph nodes
```

### Step 5 — User reviews PM spec

**Required fields (otherwise PM gate will block):**
```json
{
  "target_files": ["agent/xxx.py"],
  "verification": "how to verify the implementation is correct",
  "acceptance_criteria": ["criterion 1", "criterion 2"]
}
```

### Step 6 — PM execution and complete

```
task_claim(worker_id="observer")
task_complete(status="succeeded", result={
  "target_files": [...],
  "verification": "...",
  "acceptance_criteria": [...],
  "prd_scope": "brief description"
})
→ auto-chain passes PM gate → generates Dev task (observer_hold)
```

---

## Advanced Chain Flow: Dev Stage Takeover

### Step 7 — Dev task inspect

```
task_list() → find Dev task (observer_hold)

[Inspect]
- Read Dev task prompt (implementation instructions generated by auto-chain)
- Confirm target_files match PM
- Search related pitfalls: there may be historical bugs to inform in advance
```

### Step 8 — User confirms Dev start

Options:
- **A. Release to executor**: explicit chain test only; `task_release(task_id)` + `executor_scale(1)` → auto-execute
- **B. Observer manually implements inside chain**: explicit chain test only; `task_claim` → modify code yourself → `task_complete(result={changed_files:[...]})`

### Step 9 — Dev complete (when implementing manually)

```
task_complete(status="succeeded", result={
  "changed_files": ["agent/xxx.py"],
  "summary": "implementation description"
})
→ auto-chain triggers checkpoint gate → generates Test task (observer_hold)
```

**checkpoint gate checks:**
- `changed_files` must actually exist in `git diff`
- No files outside target_files are allowed

---

## Advanced Chain Flow: Test / QA / Merge Stages

### Step 10 — Test task

```
task_claim(worker_id="observer")
# Run tests
pytest agent/tests/ -v
task_complete(status="succeeded", result={
  "test_report": {
    "tool": "pytest",
    "passed": <N>,
    "failed": 0,
    "summary": "N tests passed"
  }
})
→ auto-chain T2 pass gate → generates QA task (observer_hold)
```

### Step 11 — QA task

```
task_claim(worker_id="observer")
# Review code changes, verify acceptance_criteria
task_complete(status="succeeded", result={
  "recommendation": "qa_pass",
  "criteria_results": [
    {"criterion": "...", "passed": true, "evidence": "..."}
  ]
})
→ auto-chain QA gate → generates Merge task (observer_hold)
```

### Step 12 — Merge task

```
task_claim(worker_id="observer")
# git add + git commit
git commit -m "feat: ..."
task_complete(status="succeeded", result={
  "merged": true,
  "commit_sha": "<sha>"
})
→ auto-chain triggers version-update → chain_version updated to new HEAD
```

---

## Observability — Logging at Each Step

Every step in the observer flow now has structured logging (L4.24). Check governance container logs via `docker logs <container>`.

| Step | Log Prefix | Fields Logged |
|------|-----------|---------------|
| Memory search | `memory.search:` | project, query (80 chars), top_k, results count, top-3 ref_ids |
| Memory write | `memory.write:` | project, kind, module, memory_id, content (120 chars) |
| Task create (API) | `API task.create:` | project, type, prompt (80 chars) |
| Task create (DB) | `Task created:` | task_id, project, type, status, retry_round |
| Conflict rules | `API conflict_rules:` | project, decision, reason |
| Task claim (API) | `API task.claim:` | project, worker |
| Task claim (DB) | `task.claimed:` | task_id, type, worker, attempt, fence_token |
| Task complete (API) | `API task.complete:` | project, task_id, status, result keys |
| Task complete (DB) | `task.complete:` | task_id, status, exec_status, by, result (200 chars) |
| Context memory fetch | `context.fetch_memories:` | project, query, budget, results count, tokens |
| Chain memory write | `chain_memory.write:` | project, kind, module, memory_id, content (100 chars) |
| Gate blocked | `auto_chain:` | gate name, reason, task_id |
| Coordinator gate | `coordinator.gate:` | attempt, max retries, error reason |
| Coordinator retry | `coordinator.gate:` | retry attempt number, validation error |
| Memory search (dbservice) | `DockerBackend:` | dbservice semantic results count, fallback to FTS5 if unavailable |

**Gate validation rules (G1-G7):** The coordinator gate validates all actions before execution. Only `reply_only` and `create_pm_task` are allowed (G1). PM tasks must include a prompt of at least 50 characters (G2). Unknown action types are rejected (G3). Legacy format `create_task` is forced to PM type (G4). Context updates are validated for expected keys (G5). Actions referencing non-existent tasks are rejected (G6). Rate limits apply to PM task creation (G7).

**Tip:** To tail governance logs during observer session:
```bash
docker logs -f <governance-container> 2>&1 | grep -E "memory\.|task\.|API |chain_memory|conflict_rules|context\."
```

---

## Common Errors and Handling

| Error | Cause | Fix |
|-------|-------|-----|
| PM gate blocked: PRD missing fields | result missing verification/acceptance_criteria | Re-claim + complete with full fields |
| checkpoint gate: files not in git diff | changed_files listed files have no actual changes | Commit first, then complete |
| T2 pass gate: test_report not dict | result.test_report is a string | Change to `{"tool":..,"passed":N,"failed":0}` dict |
| auto-chain generates retry task | gate blocked triggers retry | Claim the retry task too + complete with correct format |
| executor claims ahead of you | executor is fast when observer_mode=OFF | Use observer_mode=ON or executor_scale(0) |

---

## Backlog storage (DB is authoritative)

The project backlog lives **exclusively** in the `backlog_bugs` governance DB table (schema v15+). The markdown file `docs/dev/bug-and-fix-backlog.md` is a **read-only** human-readable projection; direct edits to it are not permitted and are not read by auto-chain / cron / coordinator.

> **Why DB-first**: every commit to the markdown file bumped HEAD, which could silently kill an in-flight auto-chain stage (B47 root cause). Storing backlog in the DB decouples metadata from git.

See [`../dev/backlog-governance.md`](../dev/backlog-governance.md) for the full governance contract (who writes, who reads, how md is regenerated).

### MCP Tools

| Tool | Description |
|------|-------------|
| `backlog_list` | List backlog bugs for a project, optionally filtered by status/priority |
| `backlog_get` | Get details of a single backlog bug by ID |
| `backlog_upsert` | Create or update a backlog bug entry (idempotent ON CONFLICT) |
| `backlog_close` | Close a backlog bug (set status=FIXED with commit hash) |

### REST Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/backlog/{pid}` | List bugs (?status=&priority=) |
| GET | `/api/backlog/{pid}/{bug_id}` | Single bug detail (404 if missing) |
| POST | `/api/backlog/{pid}/{bug_id}` | Upsert bug |
| POST | `/api/backlog/{pid}/{bug_id}/close` | Close bug (sets status=FIXED, records commit) |

### Writing entries — required form

All backlog writes go through the API. Example (MF entry):

```bash
curl -X POST "http://localhost:40000/api/backlog/aming-claw/MF-2026-04-21-001" \
  -H "Content-Type: application/json" \
  -d '{"title":"...","status":"FIXED","priority":"P3","commit":"<short-hash>",
       "target_files":["..."],"test_files":["..."],"actor":"observer-manual"}'
```

**Do not** append entries to `bug-and-fix-backlog.md` directly — they will not be seen by downstream consumers and will bump HEAD on commit.

### ETL (one-off re-sync from md)

If legacy md edits exist (e.g. unmerged branch, historical import), re-sync with:

```bash
python scripts/etl-backlog-md-to-db.py --dry-run   # preview
python scripts/etl-backlog-md-to-db.py --apply     # idempotent
```

The initial backfill was completed 2026-04-21 (70 entries ETL'd).

---

## Prohibited Actions

- Directly `sqlite3.connect()` governance.db — WAL lock
- `task_complete(status="failed")` to cancel a task — triggers retry
- `node_update` bulk-writing status as a substitute for actual verification
- `version-update` with `updated_by="init"` after bootstrap
- `docker cp` code changes without going through the governance chain
