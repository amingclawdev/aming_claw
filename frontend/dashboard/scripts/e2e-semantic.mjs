#!/usr/bin/env node
// dashboard-semantic-e2e
//
// End-to-end validation of the dashboard's AI enrich / feedback / AI review
// pipeline. Read-only on backend code: queues real jobs, files real feedback,
// walks the operator through review decisions, then verifies state changes.
//
//   node scripts/e2e-semantic.mjs                # interactive, dry-run by default
//   node scripts/e2e-semantic.mjs --project dashboard-e2e-demo
//   node scripts/e2e-semantic.mjs --probe        # preflight only
//   node scripts/e2e-semantic.mjs --probe-cancel # read-only cancel coverage check
//   node scripts/e2e-semantic.mjs --node L7.37   # use specific node
//   node scripts/e2e-semantic.mjs --edge L7.37,L4.13,owns_state
//   node scripts/e2e-semantic.mjs --apply        # real AI calls (dry_run=false)
//   node scripts/e2e-semantic.mjs --auto-decision keep  # noninteractive dry-run
//   node scripts/e2e-semantic.mjs --ignore-cancel  # bypass cancel-first gate
//   node scripts/e2e-semantic.mjs --project aming-claw --unsafe-aming-claw
//   node scripts/e2e-semantic.mjs --fixture-review-queue-categories  # offline fixture contract
//
// CANCEL-FIRST CONTRACT: before any POST that queues operator-visible work,
// the runner verifies each planned step has a working cancel path. If any
// step's cancel category is "missing", the run aborts with a gap report so
// the backend can add the endpoint before E2E runs against scale or
// uncancellable side-effects. Pass --ignore-cancel to override (only for
// targeted debug runs you intend to clean up by hand).
//
// On any non-2xx the runner emits a copy-pasteable bug prompt for the backend
// agent and exits 1. See skills/dashboard-semantic-e2e/references/bug-prompt.md.

import { createInterface } from "node:readline/promises";
import { stdin as input, stdout as output, exit } from "node:process";

// ---------- CLI parsing ----------

const argv = process.argv.slice(2);
const FLAGS = parseFlags(argv);
const BACKEND = FLAGS.backend || process.env.VITE_BACKEND_URL || "http://localhost:40000";
const DEFAULT_E2E_PROJECT = "dashboard-e2e-demo";
const PROJECT = FLAGS.project || process.env.VITE_PROJECT_ID || DEFAULT_E2E_PROJECT;
const APPLY = FLAGS.apply === true;
const PROBE_ONLY = FLAGS.probe === true;
const PROBE_CANCEL = FLAGS["probe-cancel"] === true;
const IGNORE_CANCEL = FLAGS["ignore-cancel"] === true;
const UNSAFE_AMING_CLAW = FLAGS["unsafe-aming-claw"] === true;
const SCOPE_RECONCILE_ONLY = FLAGS["scope-reconcile-only"] === true;
const ACTIONS_ONLY = FLAGS["actions-only"] === true;
const FIXTURE_REVIEW_QUEUE_CATEGORIES = FLAGS["fixture-review-queue-categories"] === true;
const FORCED_NODE = FLAGS.node || null;
const FORCED_EDGE = FLAGS.edge || null;
const AUTO_DECISION = normalizeDecisionFlag(FLAGS["auto-decision"] || "");
const AUTO_CONTINUE = FLAGS["auto-continue"] === true || Boolean(AUTO_DECISION);
const HTTP_RETRIES = Number(FLAGS["http-retries"] || process.env.DASHBOARD_E2E_HTTP_RETRIES || 3);

function parseFlags(args) {
  const BOOL = new Set([
    "probe",
    "apply",
    "probe-cancel",
    "ignore-cancel",
    "unsafe-aming-claw",
    "scope-reconcile-only",
    "actions-only",
    "fixture-review-queue-categories",
    "auto-continue",
  ]);
  const out = {};
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (!a.startsWith("--")) continue;
    const key = a.slice(2);
    if (BOOL.has(key)) {
      out[key] = true;
    } else {
      out[key] = args[i + 1];
      i++;
    }
  }
  return out;
}

function normalizeDecisionFlag(value) {
  const raw = String(value || "").trim().toLowerCase();
  if (!raw) return "";
  const aliases = {
    accept: "a",
    a: "a",
    reject: "r",
    r: "r",
    "file-backlog": "f",
    file_backlog: "f",
    backlog: "f",
    f: "f",
    keep: "k",
    k: "k",
    "needs-signoff": "n",
    needs_signoff: "n",
    signoff: "n",
    n: "n",
    skip: "s",
    s: "s",
  };
  const normalized = aliases[raw];
  if (!normalized) {
    throw new Error("--auto-decision must be one of accept, reject, file-backlog, keep, needs-signoff, or skip");
  }
  return normalized;
}

if (
  PROJECT === "aming-claw" &&
  !UNSAFE_AMING_CLAW &&
  !PROBE_ONLY &&
  !PROBE_CANCEL &&
  !FIXTURE_REVIEW_QUEUE_CATEGORIES
) {
  console.error(
    [
      "Refusing to mutate project_id=aming-claw from dashboard e2e.",
      "Use --project dashboard-e2e-demo after bootstrapping examples/dashboard-e2e-demo,",
      "or pass --unsafe-aming-claw for an explicit main-project debug run.",
    ].join("\n"),
  );
  exit(2);
}

// ---------- Tiny ANSI helpers ----------

const C = {
  reset: "\x1b[0m",
  dim: "\x1b[2m",
  bold: "\x1b[1m",
  red: "\x1b[31m",
  green: "\x1b[32m",
  yellow: "\x1b[33m",
  cyan: "\x1b[36m",
  magenta: "\x1b[35m",
  blue: "\x1b[34m",
};
const c = (color, s) => `${C[color]}${s}${C.reset}`;
const phase = (msg) => console.log(`\n${c("cyan", "▍ phase")} ${c("bold", msg)}`);
const ok = (msg) => console.log(`  ${c("green", "✓")} ${msg}`);
const warn = (msg) => console.log(`  ${c("yellow", "!")} ${msg}`);
const info = (msg) => console.log(`  ${c("dim", "·")} ${c("dim", msg)}`);
const fail = (msg) => console.log(`  ${c("red", "✗")} ${msg}`);

// ---------- HTTP helpers ----------

class HttpError extends Error {
  constructor(method, url, status, body, request) {
    super(`${method} ${url} → ${status}`);
    this.method = method;
    this.url = url;
    this.status = status;
    this.body = body;
    this.request = request;
  }
}

async function http(method, path, body, label) {
  const url = `${BACKEND}${path}`;
  const init = { method, headers: { Accept: "application/json" } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res;
  for (let attempt = 0; attempt <= HTTP_RETRIES; attempt++) {
    try {
      res = await fetch(url, init);
      break;
    } catch (e) {
      if (attempt >= HTTP_RETRIES) {
        throw new HttpError(method, path, 0, String(e), body);
      }
      await new Promise((resolve) => setTimeout(resolve, 250 * (attempt + 1)));
    }
  }
  const text = await res.text();
  let json = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    // leave as null; non-JSON body is captured in the bug prompt
  }
  if (!res.ok) {
    throw new HttpError(method, path, res.status, text, body);
  }
  if (label) info(`${label} ${method} ${path} → ${res.status}`);
  return json;
}

function emitBugPrompt(err, phaseN, expected, ctaName, componentName, handlerHint) {
  const reqJson = err.request === undefined ? "(no body)" : JSON.stringify(err.request, null, 2);
  const escaped = err.request === undefined ? "" : JSON.stringify(err.request).replace(/'/g, "'\\''");
  const bodyExcerpt = (err.body || "").slice(0, 500);
  console.log("");
  console.log(c("red", "=== BACKEND BUG PROMPT ==="));
  console.log(`
## Dashboard E2E found a backend bug

**Detected during**: \`dashboard-semantic-e2e\` skill, phase ${phaseN}.

**Endpoint**: \`${err.method} ${err.url}\`
**Observed status**: \`${err.status}\`
**Observed body** (first 500 chars):
\`\`\`
${bodyExcerpt}
\`\`\`

**Request payload**:
\`\`\`json
${reqJson}
\`\`\`

**Curl reproducer**:
\`\`\`bash
curl -s -X ${err.method} \\
  -H 'Content-Type: application/json' \\
  -d '${escaped}' \\
  '${BACKEND}${err.url}'
\`\`\`

**Expected**: ${expected}.

**Frontend impact**: the dashboard's \`${ctaName}\` button (in \`frontend/dashboard/src/components/${componentName}.tsx\`) cannot complete its flow.

**Out of scope for this skill**: validates dashboard behaviour only. Backend handler ${handlerHint} is yours to own. Please:
1. Confirm the contract in \`frontend/dashboard/src/lib/api.ts\` + the prototype's \`openSemJobModal\` / \`submitFeedback\` flow.
2. Add a regression test under \`agent/tests/\` that POSTs the failing payload.
3. Verify after the fix with \`node frontend/dashboard/scripts/e2e-semantic.mjs --probe\`.
`);
  console.log(c("red", "=== END BUG PROMPT ==="));
}

// ---------- Cancel capability table ----------
//
// Probed against localhost:40000 on 2026-05-11 (post MF-2026-05-10-011).
// Re-run --probe-cancel to refresh.
//
// Dashboard-cancellable paths:
//   node_semantic    -> POST /semantic/jobs/{node_id}/cancel    (per-row)
//   edge_semantic    -> POST /semantic/jobs/{edge_id}/cancel    (accepts pipe `|`,
//                       arrow `src->dst:type`, or event id)
//   feedback_review  -> POST /feedback/cancel                   (soft, returns
//                       feedback_cancel_contract: "keep_status_observation")
//   scope_reconcile  -> intentionally NOT cancellable. POST /reconcile/scope/cancel
//                       returns 410 scope_reconcile_cancel_disabled because waiving
//                       pending-scope rows poisoned future HEAD reconciles.
// Bulk:
//   POST /semantic/jobs/cancel-all  (AND-combined filters; returns
//   cancelled_count + skipped_terminal + cancelled_ops)
// Session id:
//   POST /semantic/jobs returns queued_ops[] AND cancel route accepts session
//   job_id (cancels every row from that request).

const CANCEL_CAPS = {
  node_semantic: {
    kind: "per-row",
    cancelPath: (sid, id) =>
      `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(sid)}/semantic/jobs/${encodeURIComponent(id)}/cancel`,
    idShape: "node_id (NOT POST response's session job_id)",
  },
  edge_semantic: {
    kind: "per-row",
    cancelPath: (sid, id) =>
      `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(sid)}/semantic/jobs/${encodeURIComponent(id)}/cancel`,
    idShape: "edge id — pipe `src|dst|type`, arrow `src->dst:type`, or event id all accepted",
  },
  scope_reconcile: {
    kind: "missing",
    cancelPath: () => `/api/graph-governance/${PROJECT}/reconcile/scope/cancel`,
    reason: "disabled by MF-2026-05-10-011; recover by materializing graph or starting a fresh reconcile",
  },
  feedback_review: {
    kind: "soft",
    cancelPath: (sid) =>
      `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(sid)}/feedback/cancel`,
    cancelAction: "soft via feedback_cancel_contract=keep_status_observation",
  },
  bulk_semantic: {
    kind: "per-row",
    cancelPath: (sid) =>
      `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(sid)}/semantic/jobs/cancel-all`,
    idShape: "POST body with optional AND filters {operation_type, target_scope, before_ts, status}",
  },
  feedback_submit: {
    kind: "soft",
    cancelPath: (sid) =>
      `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(sid)}/feedback/decision`,
    cancelAction: "keep_status_observation",
  },
};

// What the runner will plan to submit. Each step declares a cancelKey into CANCEL_CAPS.
// Phase 3 builds this plan up-front; phaseCancelGate checks each entry and aborts if
// any returns kind === "missing" (unless --ignore-cancel).
function buildSubmitPlan({ chosenNode, chosenEdge }) {
  const plan = [];
  if (chosenNode) {
    plan.push({
      step: "enrich.node",
      cancelKey: "node_semantic",
      desc: `enrich(node ${chosenNode.node_id})`,
      scope: "selected_node",
    });
  }
  if (chosenEdge) {
    plan.push({
      step: "enrich.edge",
      cancelKey: "edge_semantic",
      desc: `enrich(edge ${chosenEdge.src}|${chosenEdge.dst}|${chosenEdge.edge_type || chosenEdge.type})`,
      scope: "selected_edge",
    });
  }
  plan.push({
    step: "review.global",
    cancelKey: "bulk_semantic",
    desc: "global review (writes feedback_review rows at snapshot scale; per-row soft-withdraw is not viable)",
    scope: "snapshot",
  });
  if (chosenNode) {
    plan.push({
      step: "feedback.node",
      cancelKey: "feedback_submit",
      desc: `feedback(node ${chosenNode.node_id})`,
      scope: "node",
    });
  }
  if (chosenEdge) {
    plan.push({
      step: "feedback.edge",
      cancelKey: "feedback_submit",
      desc: `feedback(edge)`,
      scope: "edge",
    });
  }
  return plan;
}

// Read-only probe: try every documented cancel endpoint and report what the
// backend currently exposes. Does NOT depend on the snapshot having queued work.
async function probeCancelEndpoints(snapshotId) {
  const probes = [];
  const bulkPaths = [
    `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs/cancel-all`,
    `/api/graph-governance/${PROJECT}/operations/cancel-all`,
    `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/operations/cancel-all`,
    `/api/graph-governance/${PROJECT}/reconcile/scope/cancel`,
    `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/feedback/cancel`,
  ];
  for (const p of bulkPaths) {
    let status = 0;
    try {
      const res = await fetch(`${BACKEND}${p}`, { method: "POST" });
      status = res.status;
    } catch {
      status = -1;
    }
    probes.push({ path: p, status, expectsExist: false });
  }
  // Per-row probes need a real id from the queue
  const ops = await http("GET", `/api/graph-governance/${PROJECT}/operations/queue`);
  const types = {};
  for (const o of ops.operations || []) {
    if (!types[o.operation_type]) types[o.operation_type] = o;
  }
  const samples = [];
  for (const [t, op] of Object.entries(types)) {
    samples.push({
      op_type: t,
      target_id: op.target_id,
      operation_id: op.operation_id,
      status: op.status,
      supported_actions: op.supported_actions || [],
    });
  }
  return { bulkProbes: probes, sampleOps: samples };
}

// ---------- Phases ----------

async function phasePreflight() {
  phase("preflight (read-only)");
  const probes = [
    { path: "/api/health", check: (j) => j?.status === "ok" && j.version, name: "/health" },
    { path: `/api/graph-governance/${PROJECT}/status`, check: (j) => j?.active_snapshot_id, name: "/status" },
    { path: `/api/graph-governance/${PROJECT}/snapshots/active/summary`, check: (j) => (j?.health?.semantic_health?.feature_count ?? j?.counts?.features ?? 0) > 0, name: "/summary" },
    { path: `/api/graph-governance/${PROJECT}/operations/queue`, check: (j) => typeof j?.count === "number", name: "/operations/queue" },
    { path: `/api/graph-governance/${PROJECT}/snapshots/active/semantic/projection`, check: (j) => j?.ok === true, name: "/projection" },
  ];
  let lastSummary = null;
  let lastStatus = null;
  for (const p of probes) {
    try {
      const j = await http("GET", p.path);
      if (!p.check(j)) {
        const err = new HttpError("GET", p.path, 200, JSON.stringify(j).slice(0, 500), undefined);
        throw err;
      }
      ok(`${p.name}`);
      if (p.name === "/summary") lastSummary = j;
      if (p.name === "/status") lastStatus = j;
      if (p.name === "/projection" && (j.status === "missing" || !j.projection?.node_semantics)) {
        warn("projection has no node_semantics yet (snapshot warming up); target picker will fall back to /nodes");
      }
    } catch (e) {
      fail(`${p.name}: ${e.message}`);
      emitBugPrompt(e, 1, "200 + the documented response shape", "Refresh", "App", "in `agent/governance/server.py`");
      throw e;
    }
  }
  const sid = lastStatus.active_snapshot_id;
  // /feedback/queue + /nodes + /edges need a snapshot id.
  const sidProbes = [
    { path: `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(sid)}/feedback/queue?require_current_semantic=true`, check: (j) => j?.summary, name: "/feedback/queue" },
    { path: `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(sid)}/nodes?include_semantic=true&limit=1000`, check: (j) => Array.isArray(j?.nodes), name: "/nodes" },
    { path: `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(sid)}/edges?limit=4000`, check: (j) => Array.isArray(j?.edges), name: "/edges" },
  ];
  for (const p of sidProbes) {
    try {
      const j = await http("GET", p.path);
      if (!p.check(j)) throw new HttpError("GET", p.path, 200, JSON.stringify(j).slice(0, 500), undefined);
      ok(`${p.name}`);
    } catch (e) {
      fail(`${p.name}: ${e.message}`);
      emitBugPrompt(e, 1, "200 + the documented response shape", "Refresh", "App", "in `agent/governance/server.py`");
      throw e;
    }
  }
  // Banner check: backend owns project-scoped stale detection. Comparing
  // service version with snapshot commit is a false positive for nested demo
  // projects whose workspace did not change under the newer repo HEAD.
  const runtime = (await http("GET", "/api/health")).version;
  const snap = lastStatus.graph_snapshot_commit;
  const graphStale = lastStatus?.current_state?.graph_stale;
  const bannerActive =
    typeof graphStale?.is_stale === "boolean"
      ? graphStale.is_stale
      : snap && runtime && !snap.startsWith(runtime) && !runtime.startsWith(snap.slice(0, runtime.length));
  if (bannerActive) {
    const head = graphStale?.head_commit || runtime;
    warn(`graph snapshot behind HEAD — head ${head.slice(0, 7)} vs snapshot ${snap.slice(0, 7)} (dashboard banner active)`);
  }
  return {
    snapshotId: sid,
    summary: lastSummary,
    status: lastStatus,
  };
}

async function phasePickTargets(snapshotId, projection, nodes, edges) {
  phase("pick targets");
  // Node
  let chosenNode = null;
  if (FORCED_NODE) {
    chosenNode = nodes.find((n) => n.node_id === FORCED_NODE);
    if (!chosenNode) throw new Error(`forced node not found: ${FORCED_NODE}`);
    ok(`node (forced) = ${chosenNode.node_id} ${chosenNode.title}`);
  } else {
    // Prefer L7 stale via projection validity; warming-up snapshots return null
    const projSem = (projection?.projection?.node_semantics) || {};
    for (const [nid, entry] of Object.entries(projSem)) {
      if (!nid.startsWith("L7.")) continue;
      const v = entry?.validity || {};
      const status = String(v.status || "").toLowerCase();
      if (status.includes("stale") || v.valid === false) {
        chosenNode = nodes.find((n) => n.node_id === nid && !isPackageMarker(n));
        if (chosenNode) break;
      }
    }
    if (!chosenNode) {
      chosenNode = nodes.find((n) => n.layer === "L7" && !isPackageMarker(n));
      warn("no stale L7 found; using first governable L7 — exercises the 'already current' path");
    }
    ok(`node = ${chosenNode.node_id} ${chosenNode.title}`);
  }
  // Edge
  let chosenEdge = null;
  if (FORCED_EDGE) {
    const [src, dst, type] = FORCED_EDGE.split(",");
    chosenEdge = edges.find((e) => e.src === src && e.dst === dst && (e.edge_type || e.type) === type);
    if (!chosenEdge) throw new Error(`forced edge not found: ${FORCED_EDGE}`);
    ok(`edge (forced) = ${chosenEdge.src} ${chosenEdge.edge_type} ${chosenEdge.dst}`);
  } else {
    const PREFERRED = ["depends_on", "owns_state", "reads_state", "writes_state"];
    for (const t of PREFERRED) {
      chosenEdge = edges.find((e) => (e.edge_type || e.type) === t);
      if (chosenEdge) break;
    }
    if (!chosenEdge) {
      chosenEdge = edges.find((e) => (e.edge_type || e.type) !== "contains");
    }
    if (!chosenEdge) {
      warn("no typed edge in snapshot — edge enrich/feedback rows will be skipped");
    } else {
      ok(`edge = ${chosenEdge.src} ${chosenEdge.edge_type} ${chosenEdge.dst}`);
    }
  }
  return { chosenNode, chosenEdge };
}

function isPackageMarker(n) {
  if (!n) return false;
  const meta = n.metadata || {};
  return Boolean(
    n.exclude_as_feature ||
      meta.exclude_as_feature ||
      meta.feature_metadata?.exclude_as_feature ||
      (meta.module && /\.__init__$/.test(meta.module)) ||
      (n.title && n.title.endsWith(".__init__")),
  );
}

// Verifies the current scope_reconcile contract without mutating pending-scope
// rows. MF-2026-05-10-011 deliberately disabled cancel because the old soft
// cancel wrote `waived` state that could poison future HEAD reconciles.
function syntheticReconcileCommit() {
  // Stable per-day so re-running the test the same day reuses the same row;
  // a new day yields a fresh commit so cancel can be exercised end-to-end.
  const day = new Date().toISOString().slice(0, 10).replace(/-/g, "");
  return ("e2e" + day + "0".repeat(40)).slice(0, 40);
}

async function phaseScopeReconcileClick(status) {
  phase("scope reconcile cancel-disabled contract");
  const realHead = status?.current_state?.graph_stale?.head_commit;
  const snap = status?.graph_snapshot_commit;
  const synth = syntheticReconcileCommit();
  info(`real HEAD=${(realHead || "").slice(0, 7)} · graph=${(snap || "").slice(0, 7)} · synthetic op=${synth.slice(0, 16)}…`);

  // 1. Current operations rows must not advertise cancel for scope_reconcile.
  const ops1 = await http("GET", `/api/graph-governance/${PROJECT}/operations/queue`);
  const badRow = (ops1.operations || []).find(
    (o) => o.operation_type === "scope_reconcile" && (o.supported_actions || []).includes("cancel"),
  );
  if (badRow) {
    fail(`scope_reconcile row incorrectly advertises cancel: ${JSON.stringify(badRow)}`);
    return { skipped: false, ok: false };
  }
  ok("operations/queue omits cancel for scope_reconcile rows");

  // 2. Direct POST remains available as a clear disabled contract: 410.
  try {
    const url = `${BACKEND}/api/graph-governance/${PROJECT}/reconcile/scope/cancel`;
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ operation_id: `scope-reconcile:${synth}`, actor: "dashboard_e2e", reason: "e2e_contract_probe" }),
    });
    if (res.status !== 410) {
      fail(`scope_reconcile cancel returned ${res.status}; expected 410 disabled`);
      return { skipped: false, ok: false };
    }
    ok("POST /reconcile/scope/cancel returns 410 disabled");
  } catch (e) {
    fail(`disabled-cancel POST probe failed: ${e.message}`);
    return { skipped: false, ok: false };
  }
  return { skipped: false, ok: true };
}

// Comprehensive E2E covering every Action entry the dashboard exposes:
//   1. single node enrich            → POST /semantic/jobs (target_scope=node)
//   2. single edge enrich            → POST /semantic/jobs (target_scope=edge)
//   3. bulk node enrich (preset)     → POST /semantic/jobs (target=nodes, scope=missing)
//   3.5 cancel-all node_semantic     → POST /semantic/jobs/cancel-all when rows were queued
//   4. bulk edge enrich (preset)     → POST /semantic/jobs (target=edges, scope=missing)
//   4.5 cancel-all edge_semantic     → POST /semantic/jobs/cancel-all when rows were queued
//   5. per-node feedback             → POST /feedback
//   6. file backlog 2-step           → POST /events + POST /events/{id}/file-backlog
//
// Each writer step pairs with a verification + a cancel/withdraw so the test
// leaves the queue clean. Uses dry_run=true unless --apply. This matters for
// edge semantic rows because a local worker can claim them immediately, and
// running rows are deliberately not cancellable.
async function phaseActionsOnly(snapshotId, projection, nodesRes, edgesRes) {
  phase("actions-only · exercise every dashboard action with cancel paired");

  const { chosenNode, chosenEdge } = await phasePickTargets(
    snapshotId,
    projection,
    nodesRes.nodes,
    edgesRes.edges,
  );
  if (!chosenNode) throw new Error("no node available — projection or /nodes empty");
  if (!chosenEdge) warn("no typed edge available — edge phases will be skipped");

  // All enrich phases honor --apply / dry_run. Real queue writes are useful for
  // targeted debug, but the default Mac E2E lane must not require live AI or
  // leave uncancellable running rows behind.
  const dry = !APPLY;
  const results = [];
  const log = (step, ok, detail) => results.push({ step, ok, detail });

  // 1. single node enrich
  {
    const payload = {
      job_type: "semantic_enrichment",
      target_scope: "node",
      target_ids: [chosenNode.node_id],
      options: {
        target: "nodes",
        include_nodes: true,
        include_edges: false,
        scope: "selected_node",
        mode: "retry",
        dry_run: dry,
        skip_current: true,
        retry_stale_failed: true,
        include_package_markers: false,
      },
      created_by: "dashboard_e2e",
    };
    try {
      const r = await http(
        "POST",
        `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs`,
        payload,
      );
      ok(`[1] single-node enrich · job_id=${r.job_id} · queued=${r.queued_count} · ops=${(r.queued_ops || []).length}`);
      log("single_node_enrich", true, `job=${r.job_id} queued=${r.queued_count}`);
      // Cancel the session immediately to leave the queue clean
      if (r.job_id && r.queued_count > 0) {
        const c = await http(
          "POST",
          `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs/${encodeURIComponent(r.job_id)}/cancel`,
          { actor: "dashboard_e2e" },
        );
        ok(`    cancel(session) · cancelled_count=${c.cancelled_count ?? "?"}`);
      }
    } catch (e) {
      fail(`[1] single-node enrich failed: ${e.message}`);
      log("single_node_enrich", false, e.message);
    }
  }

  // 2. single edge enrich
  if (chosenEdge) {
    const edgeKey = `${chosenEdge.src}|${chosenEdge.dst}|${chosenEdge.edge_type || chosenEdge.type}`;
    const payload = {
      job_type: "semantic_enrichment",
      target_scope: "edge",
      target_ids: [edgeKey],
      options: {
        target: "edges",
        include_nodes: false,
        include_edges: true,
        scope: "selected_node",
        mode: "semanticize",
        dry_run: dry,
        skip_current: true,
        include_package_markers: false,
      },
      created_by: "dashboard_e2e",
    };
    try {
      const r = await http(
        "POST",
        `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs`,
        payload,
      );
      ok(`[2] single-edge enrich · job_id=${r.job_id} · queued=${r.queued_count}`);
      log("single_edge_enrich", true, `job=${r.job_id} queued=${r.queued_count}`);
      if (r.job_id && r.queued_count > 0) {
        const c = await http(
          "POST",
          `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs/${encodeURIComponent(r.job_id)}/cancel`,
          { actor: "dashboard_e2e" },
        );
        ok(`    cancel(session) · cancelled_count=${c.cancelled_count ?? "?"}`);
      }
    } catch (e) {
      fail(`[2] single-edge enrich failed: ${e.message}`);
      log("single_edge_enrich", false, e.message);
    }
  } else {
    info("[2] skipped (no edge)");
  }

  // 3. bulk node enrich (preset: retry_stale → use scope=stale when applying)
  let bulkNodeJobId = null;
  let bulkNodeQueued = 0;
  {
    const payload = {
      job_type: "semantic_enrichment",
      target_scope: "snapshot",
      target_ids: [],
      options: {
        target: "nodes",
        include_nodes: true,
        include_edges: false,
        scope: "stale",
        mode: "retry",
        dry_run: dry,
        skip_current: true,
        retry_stale_failed: true,
        include_package_markers: false,
      },
      created_by: "dashboard_e2e",
    };
    try {
      const r = await http(
        "POST",
        `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs`,
        payload,
      );
      bulkNodeJobId = r.job_id;
      bulkNodeQueued = Number(r.queued_count || 0);
      ok(`[3] bulk-node enrich (preset=retry_stale) · job_id=${r.job_id} · queued=${r.queued_count}`);
      log("bulk_node_enrich", true, `queued=${r.queued_count}${dry ? " dry_run" : ""}`);
    } catch (e) {
      fail(`[3] bulk-node enrich failed: ${e.message}`);
      log("bulk_node_enrich", false, e.message);
    }
  }

  // 3.5 cancel-all node_semantic
  if (bulkNodeJobId && bulkNodeQueued > 0) {
    try {
      const c = await http(
        "POST",
        `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs/cancel-all`,
        { operation_type: "node_semantic", status: "queued", actor: "dashboard_e2e" },
      );
      ok(`[3.5] cancel-all node_semantic · cancelled=${c.cancelled_count} · skipped_terminal=${c.skipped_terminal} · matched=${c.matched_count}`);
      log("cancel_all_node", true, `cancelled=${c.cancelled_count}`);
    } catch (e) {
      fail(`[3.5] cancel-all node_semantic failed: ${e.message}`);
      log("cancel_all_node", false, e.message);
    }
  } else {
    ok("[3.5] cancel-all node_semantic skipped · no queued rows");
    log("cancel_all_node", true, "skipped");
  }

  // 4. bulk edge enrich (preset: missing_edges)
  // Bulk edge enrichment uses target_scope=edge + options.all_eligible=true,
  // not target_scope=snapshot. The snapshot path is the node-semantic queue
  // and silently routes target=edges as node work.
  let bulkEdgeJobId = null;
  let bulkEdgeQueued = 0;
  {
    const payload = {
      job_type: "semantic_enrichment",
      target_scope: "edge",
      target_ids: [],
      options: {
        target: "edges",
        include_nodes: false,
        include_edges: true,
        scope: "missing",
        mode: "semanticize",
        dry_run: dry,
        skip_current: true,
        all_eligible: true,
        include_contains: false,
        limit: 1000,
        include_package_markers: false,
      },
      created_by: "dashboard_e2e",
    };
    try {
      const r = await http(
        "POST",
        `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs`,
        payload,
      );
      bulkEdgeJobId = r.job_id;
      bulkEdgeQueued = Number(r.queued_count || 0);
      ok(`[4] bulk-edge enrich (preset=missing_edges) · job_id=${r.job_id} · queued=${r.queued_count}`);
      log("bulk_edge_enrich", true, `queued=${r.queued_count}${dry ? " dry_run" : ""}`);
    } catch (e) {
      fail(`[4] bulk-edge enrich failed: ${e.message}`);
      log("bulk_edge_enrich", false, e.message);
    }
  }

  // 4.5 cancel-all edge_semantic
  if (bulkEdgeJobId && bulkEdgeQueued > 0) {
    try {
      const c = await http(
        "POST",
        `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs/cancel-all`,
        { operation_type: "edge_semantic", status: "queued", actor: "dashboard_e2e" },
      );
      ok(`[4.5] cancel-all edge_semantic · cancelled=${c.cancelled_count} · skipped_terminal=${c.skipped_terminal} · matched=${c.matched_count}`);
      log("cancel_all_edge", true, `cancelled=${c.cancelled_count}`);
    } catch (e) {
      fail(`[4.5] cancel-all edge_semantic failed: ${e.message}`);
      log("cancel_all_edge", false, e.message);
    }
  } else {
    ok("[4.5] cancel-all edge_semantic skipped · no queued rows");
    log("cancel_all_edge", true, "skipped");
  }

  // 5. per-node feedback (soft-withdraw via /feedback/cancel)
  let fbId = null;
  {
    const payload = {
      feedback_kind: "status_observation",
      summary: `[E2E actions-only] status observation for ${chosenNode.node_id} (${chosenNode.title})`,
      source_node_ids: [chosenNode.node_id],
      target_id: chosenNode.node_id,
      target_type: "node",
      priority: "P3",
      paths: chosenNode.primary_files ?? [],
      reason: "dashboard.e2e.actions_only",
      create_graph_event: false,
      actor: "dashboard_e2e",
      source_round: "user",
    };
    try {
      const r = await http(
        "POST",
        `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/feedback`,
        payload,
      );
      fbId = r.feedback?.feedback_id ?? r.items?.[0]?.feedback_id ?? null;
      ok(`[5] feedback submit · feedback_id=${fbId ?? "(none)"}`);
      log("feedback_submit", !!fbId, fbId ?? "no feedback_id");
    } catch (e) {
      fail(`[5] feedback submit failed: ${e.message}`);
      log("feedback_submit", false, e.message);
    }
    if (fbId) {
      try {
        const c = await http(
          "POST",
          `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/feedback/cancel`,
          { feedback_ids: [fbId], actor: "dashboard_e2e" },
        );
        ok(`    feedback/cancel · cancelled=${c.cancelled_count} · contract=${c.feedback_cancel_contract}`);
      } catch (e) {
        warn(`    feedback/cancel failed: ${e.message}`);
      }
    }
  }

  // 5.5 MF-2026-05-10-011 cancel contract: scope_reconcile cancel must return 410
  try {
    const url = `${BACKEND}/api/graph-governance/${PROJECT}/reconcile/scope/cancel`;
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ commit_sha: "e2e-test-commit" }),
    });
    if (res.status === 410) {
      ok(`[5.5a] scope_reconcile cancel · 410 disabled (MF-2026-05-10-011)`);
      log("scope_reconcile_disabled", true, "410");
    } else {
      fail(`[5.5a] scope_reconcile cancel returned ${res.status}; expected 410`);
      log("scope_reconcile_disabled", false, `status=${res.status}`);
    }
  } catch (e) {
    log("scope_reconcile_disabled", false, e.message);
  }

  // 5.6 clear-terminal happy path (drains any cancelled rows we just produced)
  try {
    const ct = await http(
      "POST",
      `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs/clear-terminal`,
      { actor: "dashboard_e2e" },
    );
    ok(`[5.6] clear-terminal · deleted_nodes=${ct.deleted_count} · edge_audit=${ct.edge_audit_matched}`);
    log("clear_terminal", ct.ok === true, `deleted=${ct.deleted_count}`);
  } catch (e) {
    fail(`[5.6] clear-terminal failed: ${e.message}`);
    log("clear_terminal", false, e.message);
  }

  // 5.7 queue cleanliness. The operations queue may legitimately include
  // not_queued suggestions, but actions-only must not leave live semantic jobs.
  try {
    const openOps = await openSemanticOperations();
    if (openOps.length > 0) {
      const detail = openOps.map((op) => `${op.operation_type}:${op.target_id}:${op.status}`).join(", ");
      fail(`[5.7] open semantic operations remain: ${detail}`);
      log("semantic_queue_clean", false, detail);
    } else {
      ok("[5.7] semantic operations queue clean");
      log("semantic_queue_clean", true, "no open semantic ops");
    }
  } catch (e) {
    fail(`[5.7] semantic queue cleanliness check failed: ${e.message}`);
    log("semantic_queue_clean", false, e.message);
  }

  // 6. file backlog 2-step
  {
    try {
      const ev = await http(
        "POST",
        `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/events`,
        {
          event_kind: "proposed_event",
          event_type: "backlog_candidate_requested",
          target_type: "node",
          target_id: chosenNode.node_id,
          source: "dashboard_e2e",
          user_text: `[E2E] backlog dry-run for ${chosenNode.node_id}`,
          payload: { backlog_draft: { title: "e2e-backlog-test", task_type: "dev" } },
          precondition: { snapshot_id: snapshotId },
          status: "proposed",
        },
      );
      const eventId = ev.event?.event_id ?? ev.event_id;
      ok(`[6a] proposed_event · event_id=${eventId}`);
      if (!eventId) throw new Error("no event_id returned");
      const f = await http(
        "POST",
        `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/events/${encodeURIComponent(eventId)}/file-backlog`,
        {
          backlog: {
            title: "e2e-backlog-test",
            task_type: "dev",
            priority: "P3",
            target_files: [],
            affected_graph_nodes: [chosenNode.node_id],
            graph_gate_mode: "advisory",
            branch_mode: "batch_branch",
            acceptance_criteria: ["Smoke test only — auto-generated by e2e-semantic --actions-only"],
            prompt: "E2E backlog drill, do not work on this task.",
          },
          start_chain: false,
          actor: "dashboard_e2e",
        },
      );
      const bugId = f.bug_id ?? f.event?.backlog_bug_id ?? f.backlog_task_id ?? f.task_id;
      ok(`[6b] file-backlog · bug_id=${bugId ?? "(none)"}`);
      log("file_backlog", !!bugId, bugId ?? "no bug_id");
    } catch (e) {
      fail(`[6] file backlog failed: ${e.message}`);
      log("file_backlog", false, e.message);
    }
  }

  // Summary
  console.log("");
  console.log(c("bold", "  ── actions-only summary ────────────────────────────────"));
  for (const r of results) {
    const tag = r.ok ? c("green", "OK  ") : c("red", "FAIL");
    console.log(`  ${tag}  ${r.step.padEnd(22)}  ${r.detail || ""}`);
  }
  const allOk = results.every((r) => r.ok);
  return { ok: allOk, results };
}

async function openSemanticOperations() {
  const ops = await http("GET", `/api/graph-governance/${PROJECT}/operations/queue`);
  const openStatuses = new Set(["queued", "ai_pending", "pending_ai", "running", "claimed", "ai_reviewing"]);
  return (ops.operations || []).filter((op) => {
    const type = String(op.operation_type || "");
    if (type !== "node_semantic" && type !== "edge_semantic") return false;
    const status = String(op.status || "").toLowerCase().replace(/-/g, "_");
    return openStatuses.has(status);
  });
}

function phaseCancelGate(plan) {
  phase("cancel-first gate");
  const blocking = [];
  for (const step of plan) {
    const cap = CANCEL_CAPS[step.cancelKey];
    if (!cap) {
      blocking.push({ step, reason: `unknown cancel key: ${step.cancelKey}` });
      continue;
    }
    if (cap.kind === "per-row") {
      ok(`${step.desc}: cancel via per-row POST (${cap.idShape})`);
    } else if (cap.kind === "soft") {
      ok(`${step.desc}: soft-cancel via decision=${cap.cancelAction}`);
    } else {
      fail(`${step.desc}: NO CANCEL — ${cap.reason}`);
      blocking.push({ step, reason: cap.reason });
    }
  }
  if (blocking.length === 0) {
    ok("all planned steps have a working cancel path");
    return;
  }
  if (IGNORE_CANCEL) {
    warn(`--ignore-cancel set; proceeding despite ${blocking.length} uncancellable step(s)`);
    return;
  }
  console.log("");
  console.log(c("red", "=== CANCEL GAP — RUN ABORTED ==="));
  console.log(`
The following planned steps have no working cancel endpoint. Running them
without a cancel path means the operator cannot abort if the queued work
explodes (the last full-scope enrich queued 187 individual ops with no bulk
cancel). Forward the gap report to the backend agent and re-run when filled.

`);
  for (const b of blocking) {
    console.log(`  ${c("red", "✗")} ${b.step.desc}`);
    console.log(`      reason: ${b.reason}`);
  }
  console.log("");
  console.log(c("dim", "Override with --ignore-cancel only for narrow debug runs."));
  console.log(c("dim", "See skills/dashboard-semantic-e2e/references/bug-prompt.md for the gap template."));
  console.log(c("red", "=== END ==="));
  throw new Error(`cancel gate: ${blocking.length} step(s) without cancel`);
}

function runReviewQueueCategoryFixtureContract() {
  phase("review queue category fixture contract");
  const fixture = buildReviewQueueCategoryFixture();
  const expectedCategories = [
    "semantic",
    "graph_structure",
    "doc_binding",
    "test_binding",
    "config_binding",
    "status_observation",
    "backlog",
    "other",
  ];

  assertFixture(
    expectedCategories.every((category) => fixture.summary.by_category_visible_groups[category] === 7),
    "fixture_contract: backend visible category counts are intentionally non-heuristic",
  );

  const tabs = buildReviewQueueCategoryTabsFromMetadata(fixture);
  const tabById = new Map(tabs.map((tab) => [tab.id, tab]));
  assertFixture(tabById.get("ALL")?.visibleGroups === 8, "category_ui_test_or_e2e: all tab uses summary visible_group_count");
  for (const category of expectedCategories) {
    const tab = tabById.get(category);
    assertFixture(Boolean(tab), `category_ui_test_or_e2e: ${category} tab exists`);
    assertFixture(tab.visibleGroups === 7, `category_ui_test_or_e2e: ${category} visible count comes from backend metadata`);
    assertFixture(tab.allItems === 11, `category_ui_test_or_e2e: ${category} item count comes from backend metadata`);
    assertFixture(
      tab.label === fixture.action_catalog.category_labels[category],
      `category_ui_test_or_e2e: ${category} label comes from backend catalog metadata`,
    );
  }
  assertFixture(
    tabs.map((tab) => tab.id).join(",") === `ALL,${expectedCategories.join(",")}`,
    "category_ui_test_or_e2e: tab order follows backend category_order",
  );

  const heuristicCounts = Object.fromEntries(
    expectedCategories.map((category) => [
      category,
      fixture.groups.filter((group) => group.category === category).length,
    ]),
  );
  assertFixture(
    expectedCategories.every((category) => heuristicCounts[category] === 1),
    "category_ui_test_or_e2e: fixture would fail if category tabs used frontend group-count heuristics",
  );

  const actionResults = [];
  for (const category of expectedCategories) {
    const filtered = filterReviewQueueGroupsByCategory(fixture.groups, category);
    assertFixture(filtered.length === 1, `category_ui_test_or_e2e: ${category} filter shows only matching group`);
    const group = filtered[0];
    const originalFeedbackIds = [...group.feedback_ids];
    const originalQueueId = group.queue_id;
    for (const action of ["accept", "retry", "reject", "file_backlog"]) {
      const payload = buildReviewQueueActionPayload(group, action);
      assertFixture(
        payload.group_id === originalQueueId,
        `actions_preserve_original_ids: ${action} preserves ${category} group id`,
      );
      assertFixture(
        JSON.stringify(payload.feedback_ids) === JSON.stringify(originalFeedbackIds),
        `actions_preserve_original_ids: ${action} preserves ${category} feedback ids`,
      );
      actionResults.push(`${category}:${action}:${payload.group_id}:${payload.feedback_ids.join("+")}`);
    }
  }

  ok("fixture_contract: offline backend metadata fixture covers semantic/graph/binding/status/backlog/other categories");
  ok("category_ui_test_or_e2e: tabs/counts/labels/order are driven by backend metadata");
  ok("actions_preserve_original_ids: filtered accept/retry/reject/file-backlog payloads keep original group and feedback ids");
  info(`action payload samples = ${actionResults.slice(0, 4).join(" | ")} ...`);
  console.log("");
  console.log(c("green", "REVIEW-QUEUE-CATEGORY-FIXTURE OK"));
}

function buildReviewQueueCategoryTabsFromMetadata(feedback) {
  const groups = feedback.groups ?? [];
  const summary = feedback.summary;
  const visibleByCategory = summary.by_category_visible_groups;
  const allItemsByCategory = summary.by_category_all_items ?? {};
  const ids =
    visibleByCategory && Object.keys(visibleByCategory).length > 0
      ? Object.keys(visibleByCategory)
      : stableUnique(groups.map((group) => group.category || "review"));
  return [
    {
      id: "ALL",
      label: "All",
      visibleGroups: summary.visible_group_count ?? groups.length,
      allItems: summary.visible_item_count ?? groups.reduce((total, group) => total + (group.item_count || 0), 0),
    },
    ...ids
      .filter(Boolean)
      .sort((a, b) => reviewQueueCategoryOrder(a, feedback.action_catalog) - reviewQueueCategoryOrder(b, feedback.action_catalog))
      .map((id) => ({
        id,
        label: feedback.action_catalog?.category_labels?.[id] || titleizeFixture(id),
        visibleGroups: visibleByCategory?.[id] ?? groups.filter((group) => group.category === id).length,
        allItems: allItemsByCategory[id] ?? groups
          .filter((group) => group.category === id)
          .reduce((total, group) => total + (group.item_count || group.feedback_ids.length || 0), 0),
      })),
  ];
}

function filterReviewQueueGroupsByCategory(groups, category) {
  return groups.filter((group) => group.category === category);
}

function buildReviewQueueActionPayload(group, action) {
  const base = {
    group_id: group.queue_id,
    feedback_ids: [...group.feedback_ids],
    representative_feedback_id: group.representative_feedback_id,
    target_id: group.target_id,
  };
  if (action === "accept") {
    return { ...base, action: "accept_semantic_enrichment" };
  }
  if (action === "retry") {
    return { ...base, action: "retry_semantic_enrichment", rationale: "[fixture] retry keeps ids" };
  }
  if (action === "reject") {
    return { ...base, action: "reject_false_positive" };
  }
  if (action === "file_backlog") {
    return {
      ...base,
      action: "file_backlog",
      backlog: {
        title: `[Fixture] Backlog from ${group.representative_feedback_id}`,
        affected_graph_nodes: [group.target_id],
      },
    };
  }
  throw new Error(`unknown fixture action: ${action}`);
}

function reviewQueueCategoryOrder(category, actionCatalog) {
  const order = actionCatalog?.category_order ?? [];
  const index = order.indexOf(category);
  return index === -1 ? order.length : index;
}

function buildReviewQueueCategoryFixture() {
  const categories = [
    "semantic",
    "graph_structure",
    "doc_binding",
    "test_binding",
    "config_binding",
    "status_observation",
    "backlog",
    "other",
  ];
  return {
    ok: true,
    project_id: "fixture-review-queue",
    snapshot_id: "fixture-snapshot",
    group_count: categories.length,
    count: categories.length,
    action_catalog: {
      category_order: categories,
      category_labels: Object.fromEntries(categories.map((category) => [category, `Backend ${titleizeFixture(category)}`])),
    },
    summary: {
      raw_count: 88,
      visible_group_count: categories.length,
      visible_item_count: 88,
      hidden_status_observation_count: 0,
      hidden_resolved_count: 0,
      hidden_claimed_count: 0,
      hidden_semantic_pending_count: 0,
      require_current_semantic: false,
      by_kind: {},
      by_status: {},
      by_lane_all_items: {},
      by_lane_visible_groups: {},
      by_category_visible_groups: Object.fromEntries(categories.map((category) => [category, 7])),
      by_category_all_items: Object.fromEntries(categories.map((category) => [category, 11])),
    },
    groups: categories.map((category, index) => ({
      queue_id: `group-${category}`,
      group_by: "target",
      lane: `lane-${category}`,
      category,
      category_label: `Backend ${titleizeFixture(category)}`,
      priority: "P2",
      target_type: "node",
      target_id: `L7.${index + 1}`,
      representative_feedback_id: `fb-${category}-representative`,
      representative_issue: `Fixture issue for ${category}`,
      feedback_ids: [`fb-${category}-a`, `fb-${category}-b`],
      item_count: 1,
      semantic_review_gate: { ready: true, reason: "fixture-current" },
    })),
  };
}

function assertFixture(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
  ok(message);
}

function stableUnique(values) {
  return Array.from(new Set(values));
}

function titleizeFixture(value) {
  return String(value)
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

async function phaseSubmit(snapshotId, node, edge, projection) {
  phase(`submit (${APPLY ? c("red", "APPLY mode — real AI calls") : "dry-run"})`);
  const transcript = [];
  const dry = !APPLY;

  // Choose mode based on node status (projection may be warming up → null)
  const nodeProj = projection?.projection?.node_semantics?.[node.node_id];
  const nodeStatus = String(nodeProj?.validity?.status || "").toLowerCase();
  const nodeMode = nodeStatus.includes("stale") || nodeProj?.validity?.valid === false ? "retry" : "semanticize";

  // 1. Enrich node
  const enrichNodePayload = {
    job_type: "semantic_enrichment",
    target_scope: "node",
    target_ids: [node.node_id],
    options: {
      target: "nodes",
      include_nodes: true,
      include_edges: false,
      scope: "selected_node",
      mode: nodeMode,
      dry_run: dry,
      skip_current: true,
      retry_stale_failed: nodeMode === "retry",
      include_package_markers: false,
    },
    created_by: "dashboard_e2e",
  };
  try {
    const r = await http("POST", `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs`, enrichNodePayload);
    transcript.push({ step: "enrich.node", id: r.job_id, status: r.status, queued: r.queued_count });
    ok(`enrich(node, ${nodeMode}) → job_id=${r.job_id} · queued=${r.queued_count}`);
  } catch (e) {
    fail(`enrich(node) failed: ${e.message}`);
    emitBugPrompt(e, 3, "202 with job_id non-empty", "⚡ AI enrich", "ActionControlPanel", "at `agent/governance/server.py:6435` (handle_graph_governance_snapshot_semantic_jobs_create)");
    throw e;
  }

  // 2. Enrich edge (if available)
  if (edge) {
    const edgeKey = `${edge.src}|${edge.dst}|${edge.edge_type || edge.type}`;
    const enrichEdgePayload = {
      job_type: "semantic_enrichment",
      target_scope: "edge",
      target_ids: [edgeKey],
      options: {
        target: "edges",
        include_nodes: false,
        include_edges: true,
        scope: "selected_node",
        mode: "semanticize",
        dry_run: dry,
        skip_current: true,
        include_package_markers: false,
      },
      created_by: "dashboard_e2e",
    };
    try {
      const r = await http("POST", `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs`, enrichEdgePayload);
      transcript.push({ step: "enrich.edge", id: r.job_id, status: r.status, queued: r.queued_count });
      ok(`enrich(edge) → job_id=${r.job_id} · queued=${r.queued_count}`);
    } catch (e) {
      warn(`enrich(edge) failed: ${e.message} — continuing`);
      emitBugPrompt(e, 3, "202 with job_id non-empty for edge scope", "⚡ AI enrich edge", "ActionControlPanel", "at `agent/governance/server.py:6435` (edge branch in `_semantic_jobs_target_scope`)");
    }
  }

  // 3. Global review
  const reviewPayload = {
    job_type: "global_review",
    target_scope: "snapshot",
    target_ids: [],
    options: {
      target: "both",
      include_nodes: true,
      include_edges: true,
      scope: "full",
      mode: "review",
      dry_run: dry,
      skip_current: false,
      include_package_markers: false,
    },
    created_by: "dashboard_e2e",
  };
  try {
    const r = await http("POST", `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/semantic/jobs`, reviewPayload);
    transcript.push({ step: "review.global", id: r.job_id, status: r.status, queued: r.queued_count });
    ok(`review(global) → job_id=${r.job_id} · queued=${r.queued_count}`);
  } catch (e) {
    warn(`review(global) failed: ${e.message} — continuing`);
    emitBugPrompt(e, 3, "202 with job_id for global_review job_type", "Run global semantic review", "ActionPanel", "at `agent/governance/server.py:6435`");
  }

  // 4. Feedback on node (graph_correction → auto graph event)
  const feedbackNodePayload = {
    feedback_kind: "graph_correction",
    summary: `[E2E] Auto-generated graph correction probe for ${node.node_id} (${node.title}).`,
    source_node_ids: [node.node_id],
    target_id: node.node_id,
    target_type: "node",
    priority: "P3",
    paths: node.primary_files ?? [],
    reason: "dashboard.e2e",
    create_graph_event: true,
    actor: "dashboard_e2e",
    source_round: "user",
  };
  try {
    const r = await http("POST", `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/feedback`, feedbackNodePayload);
    const id = r.feedback?.feedback_id ?? r.items?.[0]?.feedback_id;
    const eventId = r.event?.event_id;
    transcript.push({ step: "feedback.node", id, kind: "graph_correction", target: node.node_id, event_id: eventId });
    ok(`feedback(node graph_correction) → feedback_id=${id ?? c("red", "(none)")}${eventId ? ` event=${eventId}` : ""}`);
    if (!id) warnFeedbackShape(feedbackNodePayload, r);
  } catch (e) {
    fail(`feedback(node) failed: ${e.message}`);
    emitBugPrompt(e, 3, "200 with items[0].feedback_id non-empty", "Submit feedback", "ActionControlPanel", "at `agent/governance/server.py:4206`");
    throw e;
  }

  // 5. Feedback on edge
  if (edge) {
    const edgeKey = `${edge.src}|${edge.dst}|${edge.edge_type || edge.type}`;
    const feedbackEdgePayload = {
      feedback_kind: "project_improvement",
      summary: `[E2E] Project improvement note on ${edgeKey}: confirm typed evidence is still valid.`,
      source_node_ids: [edge.src, edge.dst],
      target_id: edgeKey,
      target_type: "edge",
      priority: "P3",
      paths: [],
      reason: "dashboard.e2e",
      create_graph_event: false,
      actor: "dashboard_e2e",
      source_round: "user",
    };
    try {
      const r = await http("POST", `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/feedback`, feedbackEdgePayload);
      const id = r.feedback?.feedback_id ?? r.items?.[0]?.feedback_id;
      transcript.push({ step: "feedback.edge", id, kind: "project_improvement", target: edgeKey });
      ok(`feedback(edge project_improvement) → feedback_id=${id ?? c("red", "(none)")}`);
      if (!id) warnFeedbackShape(feedbackEdgePayload, r);
    } catch (e) {
      warn(`feedback(edge) failed: ${e.message} — continuing`);
      emitBugPrompt(e, 3, "200 with items[0].feedback_id non-empty for edge target", "Submit feedback (edge)", "ActionControlPanel", "at `agent/governance/server.py:4206` (edge target branch)");
    }
  }
  return transcript;
}

async function phasePauseForOperator(rl, snapshotId, transcript) {
  phase("pause for operator review");
  console.log(`
  Dashboard:           ${c("cyan", "http://localhost:5173")}
  Active snapshot:     ${c("dim", snapshotId)}
  Created job ids:     ${transcript.filter((t) => t.step.startsWith("enrich") || t.step.startsWith("review")).map((t) => t.id).join(", ") || c("dim", "—")}
  Created feedback ids: ${transcript.filter((t) => t.step.startsWith("feedback")).map((t) => t.id).join(", ") || c("dim", "—")}

  Open the dashboard, switch to ${c("bold", "Graph")} or ${c("bold", "Operations Queue")} tab, and verify the rows above are visible.
  Then click into the ${c("bold", "Action Panel")} (Header → ⚖ Action) → ${c("bold", "Review & approve")} to see the new feedback rows.
`);
  if (AUTO_CONTINUE) {
    ok(`auto-continue enabled${AUTO_DECISION ? ` (decision=${AUTO_DECISION})` : ""}`);
    return;
  }
  while (true) {
    const ans = (await rl.question(c("yellow", "  Type 'g' to continue, 'q' to abort: "))).trim().toLowerCase();
    if (ans === "g") return;
    if (ans === "q") throw new Error("operator aborted");
  }
}

async function phaseDecisions(rl, snapshotId, transcript) {
  phase("decisions");
  // Poll the queue for up to 8s in case the backend's classifier is async.
  let queue = null;
  for (let i = 0; i < 8; i++) {
    queue = await http("GET", `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/feedback/queue?require_current_semantic=false`);
    const fbIds = transcript.filter((t) => t.step.startsWith("feedback")).map((t) => t.id);
    const found = (queue.groups || []).filter((g) => fbIds.includes(g.feedback_id) || fbIds.some((id) => g.id === id));
    if (found.length === fbIds.length) break;
    info(`waiting for feedback rows to appear in queue (${i + 1}/8)…`);
    await new Promise((r) => setTimeout(r, 1000));
  }
  const fbItems = transcript.filter((t) => t.step.startsWith("feedback") && t.id);
  if (fbItems.length === 0) {
    warn("no feedback rows captured; skipping decisions phase");
    return [];
  }

  const decisions = [];
  for (let i = 0; i < fbItems.length; i++) {
    const fb = fbItems[i];
    console.log(`
  ${c("bold", `[${i + 1}/${fbItems.length}]`)} feedback_id: ${c("magenta", fb.id)}
    kind:    ${fb.kind}
    target:  ${fb.target}
`);
    const choice = AUTO_DECISION || (
      await rl.question(c("yellow", "    [a]ccept / [r]eject / [f]ile-backlog / [k]eep / [n]eeds-signoff / [s]kip / [q]abort: "))
    ).trim().toLowerCase();
    if (AUTO_DECISION) info(`auto-decision=${AUTO_DECISION}`);

    if (choice === "q") throw new Error("operator aborted");
    if (choice === "s") {
      decisions.push({ feedback_id: fb.id, action: "skip", result: null });
      continue;
    }

    let action;
    let endpoint;
    let payload;
    if (choice === "a") {
      action = fb.kind === "graph_correction" ? "accept_graph_correction" : "accept_project_improvement";
      endpoint = `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/feedback/decision`;
      payload = { feedback_ids: [fb.id], action, actor: "dashboard_e2e", create_patch: action === "accept_graph_correction" };
    } else if (choice === "r") {
      action = "reject_false_positive";
      endpoint = `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/feedback/decision`;
      payload = { feedback_ids: [fb.id], action, actor: "dashboard_e2e", rationale: "[E2E] explicit reject" };
    } else if (choice === "k") {
      action = "keep_status_observation";
      endpoint = `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/feedback/decision`;
      payload = { feedback_ids: [fb.id], action, actor: "dashboard_e2e" };
    } else if (choice === "n") {
      action = "needs_human_signoff";
      endpoint = `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/feedback/decision`;
      payload = { feedback_ids: [fb.id], action, actor: "dashboard_e2e" };
    } else if (choice === "f") {
      action = "file_backlog";
      endpoint = `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/feedback/file-backlog`;
      payload = {
        feedback_ids: [fb.id],
        backlog: {
          title: `[E2E] Backlog from ${fb.id}`,
          task_type: "dev",
          priority: "P3",
          target_files: [],
          affected_graph_nodes: [String(fb.target).split("|")[0]],
          graph_gate_mode: "advisory",
          branch_mode: "batch_branch",
          acceptance_criteria: [],
          prompt: "Auto-generated by dashboard-semantic-e2e for backlog filing path validation.",
        },
        start_chain: false,
        actor: "dashboard_e2e",
      };
    } else {
      warn(`unknown choice '${choice}', skipping`);
      decisions.push({ feedback_id: fb.id, action: "skip", result: null });
      continue;
    }

    try {
      const r = await http("POST", endpoint, payload);
      decisions.push({ feedback_id: fb.id, action, result: r });
      ok(`${action} → ${summariseDecisionResp(action, r)}`);
    } catch (e) {
      fail(`${action} failed: ${e.message}`);
      emitBugPrompt(
        e,
        5,
        action === "file_backlog" ? "200 with backlog_task_id" : "200 with items[0].reviewer_decision set",
        action === "file_backlog" ? "File backlog" : "Submit feedback / Review decision",
        action === "file_backlog" ? "ActionPanel" : "InspectorDrawer",
        action === "file_backlog"
          ? "at `agent/governance/server.py:5040`"
          : "at `agent/governance/server.py:4698` (handle_graph_governance_snapshot_feedback_decision)",
      );
      decisions.push({ feedback_id: fb.id, action, error: e.message });
    }
  }
  return decisions;
}

function warnFeedbackShape(req, res) {
  // Soft bug — POST succeeded but the response did not surface a feedback_id
  // in either the documented `feedback.feedback_id` or legacy `items[].feedback_id`
  // location. Without it the decisions phase can't act on this row. Emit a
  // bug prompt but keep the run going.
  warn("backend response is missing feedback_id — decisions phase will skip this row");
  console.log("");
  console.log(c("yellow", "=== BACKEND BUG PROMPT (soft, run continues) ==="));
  console.log(`
## Dashboard E2E found a backend bug

**Detected during**: \`dashboard-semantic-e2e\` skill, phase 3.

**Endpoint**: \`POST /api/graph-governance/{pid}/snapshots/{sid}/feedback\`
**Observed status**: \`2xx (call accepted)\`
**Issue**: response did not include a \`feedback_id\` at \`feedback.feedback_id\` (documented) nor \`items[0].feedback_id\` (legacy). Without it the dashboard's toast shows \`feedback_id=—\` and the review queue cannot reference the row.

**Request payload**:
\`\`\`json
${JSON.stringify(req, null, 2)}
\`\`\`

**Observed response (truncated)**:
\`\`\`json
${JSON.stringify(res, null, 2).slice(0, 1500)}
\`\`\`

**Expected**: response carries \`feedback.feedback_id\` (string, e.g. \`rf-6bbe10c2ff\`).

**Frontend impact**: toast and Drawer Feedback tab display \`feedback_id=—\`; review queue can't decide on the row. Source: \`frontend/dashboard/src/components/ActionControlPanel.tsx\` reads \`res.feedback?.feedback_id ?? res.items?.[0]?.feedback_id\`.

Please confirm \`agent/governance/server.py:4206\` (handle_graph_governance_snapshot_feedback_submit) returns the \`feedback\` object every time, and add a regression test asserting the field is non-empty.
`);
  console.log(c("yellow", "=== END BUG PROMPT ==="));
}

function summariseDecisionResp(action, r) {
  if (action === "file_backlog") {
    return `task_id=${r?.backlog_task_id ?? r?.task_id ?? "(none)"}`;
  }
  const item = r?.items?.[0];
  return `reviewer_decision=${item?.reviewer_decision ?? "(none)"}`;
}

async function phaseVerify(snapshotId, transcript, decisions) {
  phase("verify");
  // Re-fetch queue + ops; assert moves where we acted
  const queue = await http("GET", `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(snapshotId)}/feedback/queue?require_current_semantic=false`);
  const ops = await http("GET", `/api/graph-governance/${PROJECT}/operations/queue`);
  ok(`queue.summary.raw_count = ${queue.summary?.raw_count ?? "(unknown)"}`);
  ok(`ops.count = ${ops.count}`);

  console.log("");
  console.log(c("bold", "  ── Summary ──────────────────────────────────────────────────────────────"));
  for (const t of transcript) {
    console.log(`  ${c("dim", t.step.padEnd(15))} ${t.id ?? c("dim", "—")}  ${c("dim", t.target ?? "")}`);
  }
  for (const d of decisions) {
    const tag = d.error ? c("red", "ERROR") : d.action === "skip" ? c("dim", "skip") : c("green", "ok");
    console.log(`  ${c("dim", "decision".padEnd(15))} ${d.feedback_id}  ${d.action}  ${tag}`);
  }
  console.log(c("bold", "  ─────────────────────────────────────────────────────────────────────────"));
  const allOk = decisions.every((d) => !d.error);
  console.log("");
  console.log(allOk ? c("green", "ACCEPTANCE OK") : c("red", "ACCEPTANCE FAIL"));
  return allOk;
}

// ---------- Main ----------

async function main() {
  console.log(c("bold", "dashboard-semantic-e2e"));
  console.log(c("dim", `  backend = ${BACKEND}  project = ${PROJECT}  apply=${APPLY}`));

  if (FIXTURE_REVIEW_QUEUE_CATEGORIES) {
    runReviewQueueCategoryFixtureContract();
    return;
  }

  const pre = await phasePreflight();
  if (PROBE_ONLY) {
    console.log("");
    console.log(c("green", "PROBE OK — preflight passed, no writes issued."));
    return;
  }

  const sid = pre.snapshotId;

  if (ACTIONS_ONLY) {
    const [projection, nodesRes, edgesRes] = await Promise.all([
      http("GET", `/api/graph-governance/${PROJECT}/snapshots/active/semantic/projection`),
      http("GET", `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(pre.snapshotId)}/nodes?include_semantic=true&limit=1000`),
      http("GET", `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(pre.snapshotId)}/edges?limit=4000`),
    ]);
    const r = await phaseActionsOnly(pre.snapshotId, projection, nodesRes, edgesRes);
    console.log("");
    console.log(r.ok ? c("green", "ACTIONS-ONLY OK") : c("red", "ACTIONS-ONLY FAIL"));
    if (!r.ok) exit(1);
    return;
  }

  if (SCOPE_RECONCILE_ONLY) {
    const r = await phaseScopeReconcileClick(pre.status);
    console.log("");
    if (r.skipped) {
      console.log(c("yellow", "SCOPE-RECONCILE-ONLY skipped — graph is not stale; queue an MF or commit on main first."));
      return;
    }
    console.log(r.ok ? c("green", "SCOPE-RECONCILE-ONLY OK") : c("red", "SCOPE-RECONCILE-ONLY FAIL"));
    if (!r.ok) exit(1);
    return;
  }

  if (PROBE_CANCEL) {
    phase("probe cancel coverage (read-only)");
    const { bulkProbes, sampleOps } = await probeCancelEndpoints(sid);
    console.log("");
    console.log(c("bold", "  Bulk / collection cancel endpoints:"));
    for (const p of bulkProbes) {
      const tag = p.status >= 200 && p.status < 300 ? c("green", "EXISTS") :
                  p.status === 404 ? c("red", "404 missing") :
                  p.status === 405 ? c("yellow", "405 wrong method") :
                  p.status === -1 ? c("red", "no response") :
                  c("yellow", `${p.status}`);
      console.log(`    ${tag.padEnd(24)} ${p.path}`);
    }
    console.log("");
    console.log(c("bold", "  Per-row cancel by op_type (from supported_actions, then verified):"));
    for (const s of sampleOps) {
      const declared = (s.supported_actions || []).includes("cancel");
      const cap = CANCEL_CAPS[s.op_type];
      const verified = cap?.kind === "per-row";
      const disabled = cap?.kind === "missing";
      const suggestionOnly = s.status === "not_queued" && !declared;
      let tag;
      if (declared && verified) tag = c("green", "declared+verified");
      else if (declared && !verified) tag = c("red", "DECLARED BUT BROKEN");
      else if (disabled) tag = c("dim", "disabled");
      else if (suggestionOnly) tag = c("dim", "not queued");
      else if (!declared && verified) tag = c("yellow", "available when queued");
      else tag = c("dim", "absent");
      console.log(`    ${tag.padEnd(28)}  ${s.op_type.padEnd(18)}  example: target_id=${s.target_id}  status=${s.status}  actions=[${(s.supported_actions || []).join(",")}]`);
    }
    console.log("");
    console.log(c("bold", "  Static capability table (CANCEL_CAPS):"));
    for (const [k, cap] of Object.entries(CANCEL_CAPS)) {
      const tag = cap.kind === "per-row" ? c("green", "per-row") :
                  cap.kind === "soft" ? c("yellow", "soft") :
                  c("red", "missing");
      console.log(`    ${tag.padEnd(20)} ${k}${cap.reason ? "  — " + cap.reason : ""}`);
    }
    console.log("");
    const anyMissing = sampleOps.some((s) => {
      const cap = CANCEL_CAPS[s.op_type];
      if (cap?.kind === "missing") return false;
      if (s.status === "not_queued") return false;
      return !(s.supported_actions || []).includes("cancel") && s.op_type !== "feedback_review";
    });
    if (anyMissing) {
      console.log(c("yellow", "PROBE-CANCEL: some op types lack cancel; full E2E will gate."));
    } else {
      console.log(c("green", "PROBE-CANCEL OK"));
    }
    return;
  }

  const [projection, nodesRes, edgesRes] = await Promise.all([
    http("GET", `/api/graph-governance/${PROJECT}/snapshots/active/semantic/projection`),
    http("GET", `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(sid)}/nodes?include_semantic=true&limit=1000`),
    http("GET", `/api/graph-governance/${PROJECT}/snapshots/${encodeURIComponent(sid)}/edges?limit=4000`),
  ]);
  const { chosenNode, chosenEdge } = await phasePickTargets(sid, projection, nodesRes.nodes, edgesRes.edges);

  const plan = buildSubmitPlan({ chosenNode, chosenEdge });
  phaseCancelGate(plan);

  const transcript = await phaseSubmit(sid, chosenNode, chosenEdge, projection);

  const rl = createInterface({ input, output });
  let allOk = false;
  try {
    await phasePauseForOperator(rl, sid, transcript);
    const decisions = await phaseDecisions(rl, sid, transcript);
    allOk = await phaseVerify(sid, transcript, decisions);
  } finally {
    rl.close();
  }
  exit(allOk ? 0 : 1);
}

main().catch((err) => {
  if (err instanceof HttpError) {
    // bug prompt was already emitted; just exit
    exit(1);
  }
  console.error(c("red", `FATAL: ${err.message}`));
  if (err.stack) console.error(c("dim", err.stack));
  exit(2);
});
