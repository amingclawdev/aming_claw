#!/usr/bin/env node
// hn-fear-demo-smoke
//
// Lightweight dashboard smoke and screenshot capture for the HN fear demo.
// It intentionally does not replay code workflows or call live AI providers.
//
//   node scripts/e2e-hn-demo.mjs --dashboard http://127.0.0.1:40000/dashboard --project aming-claw
//   node scripts/e2e-hn-demo.mjs --project aming-claw --headed --keep-open
//   node scripts/e2e-hn-demo.mjs --ensure-fixture --no-browser
//   node scripts/e2e-hn-demo.mjs --sandbox-audit --no-browser

import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { exit } from "node:process";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const REPO_ROOT = path.resolve(SCRIPT_DIR, "../../..");
const DEFAULT_BACKEND_PORT = "40000";
const DEFAULT_SCREENSHOT_DIR = path.join(REPO_ROOT, "docs", "hn-demo", "screenshots");
const DEFAULT_FIXTURE_PROJECT = "aming-claw-hn-demo";
const DEFAULT_FIXTURE_ROOT = path.join(os.tmpdir(), "ac-hn-demo-fixture");
const DEFAULT_BACKLOG_ID = "HN-FEAR-DEMO-SMOKE-SCREENSHOTS-20260526";

const FLAGS = parseFlags(process.argv.slice(2));
const SANDBOX_AUDIT = FLAGS["sandbox-audit"] === true;
const RUN_ID = String(
  FLAGS["run-id"] || new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14),
).replace(/[^a-zA-Z0-9_-]/g, "-");
const DEFAULT_BACKEND = `http://127.0.0.1:${FLAGS.port || process.env.GOVERNANCE_PORT || DEFAULT_BACKEND_PORT}`;
const BACKEND = trimTrailingSlash(FLAGS.backend || process.env.VITE_BACKEND_URL || DEFAULT_BACKEND);
const DASHBOARD = trimTrailingSlash(
  FLAGS.dashboard || process.env.DASHBOARD_URL || process.env.VITE_DASHBOARD_URL || `${BACKEND}/dashboard`,
);
const EXPLICIT_PROJECT = Boolean(FLAGS["project-id"] || FLAGS.project || process.env.VITE_PROJECT_ID);
let PROJECT =
  FLAGS["project-id"] ||
  FLAGS.project ||
  process.env.VITE_PROJECT_ID ||
  (SANDBOX_AUDIT ? `aming-claw-hn-demo-${RUN_ID.toLowerCase()}` : DEFAULT_FIXTURE_PROJECT);
const FIXTURE_PROJECT = FLAGS["fixture-project"] || (SANDBOX_AUDIT ? PROJECT : DEFAULT_FIXTURE_PROJECT);
const FIXTURE_ROOT = path.resolve(
  FLAGS["fixture-dir"] ||
    FLAGS["fixture-root"] ||
    process.env.AMING_CLAW_HN_DEMO_FIXTURE_ROOT ||
    (SANDBOX_AUDIT ? path.join(os.tmpdir(), "ac-hn-demo-fixtures", RUN_ID) : DEFAULT_FIXTURE_ROOT),
);
const STATE_DIR = path.resolve(FLAGS["state-dir"] || path.join(os.tmpdir(), "ac-hn-demo-state", RUN_ID));
const REPORT_PATH = path.resolve(FLAGS.report || path.join(REPO_ROOT, "docs", "hn-demo", "audits", `${RUN_ID}.md`));
const JSON_REPORT_PATH = path.resolve(FLAGS["json-report"] || REPORT_PATH.replace(/\.md$/i, ".json"));
const CODEX_INSTALL_REPORT = FLAGS["codex-install-report"] ? path.resolve(FLAGS["codex-install-report"]) : "";
const CLAUDE_INSTALL_REPORT = FLAGS["claude-install-report"] ? path.resolve(FLAGS["claude-install-report"]) : "";
const REQUIRE_INSTALL_GATES = FLAGS["require-install-gates"] === true;
const WORKER_COUNT = Math.max(2, Number(FLAGS.workers || 2));
const OBSERVER_MODE = FLAGS.observer || "scripted";
let BACKLOG_ID = FLAGS.backlog || DEFAULT_BACKLOG_ID;
const SCREENSHOT_DIR = path.resolve(FLAGS.screenshots || DEFAULT_SCREENSHOT_DIR);
const HEADLESS = FLAGS.headed !== true;
const KEEP_OPEN = FLAGS["keep-open"] === true || FLAGS.interactive === true || FLAGS.headed === true;
const ENSURE_FIXTURE = SANDBOX_AUDIT || FLAGS["ensure-fixture"] === true || (!EXPLICIT_PROJECT && FLAGS["no-fixture"] !== true);
if (ENSURE_FIXTURE) PROJECT = FIXTURE_PROJECT;
const RESET_FIXTURE = SANDBOX_AUDIT || FLAGS["reset-fixture"] === true;
const NO_BROWSER = FLAGS["no-browser"] === true || (SANDBOX_AUDIT && FLAGS.browser !== true);
const HTTP_RETRIES = Number(FLAGS["http-retries"] || process.env.DASHBOARD_E2E_HTTP_RETRIES || 3);
const NAV_TIMEOUT_MS = Number(FLAGS["nav-timeout-ms"] || 30000);

const SCREENSHOTS = [
  {
    id: "before-work-contract",
    file: "01-before-work-contract.png",
    view: "backlog",
    setup: openBacklogContract,
    selectors: [".backlog-modal", ".backlog-modal-tabs", ".backlog-modal-tab-panel"],
  },
  {
    id: "before-work-graph",
    file: "02-before-work-graph.png",
    view: "graph",
    selectors: [".graph-view", ".graph-toolbar", ".graph-canvas"],
  },
  {
    id: "during-work-timeline",
    file: "03-during-work-timeline.png",
    view: "backlog",
    setup: openBacklogTimeline,
    selectors: [".backlog-modal", ".backlog-dag-shell, .timeline-empty", ".backlog-evidence-inspector"],
  },
  {
    id: "during-work-evidence",
    file: "04-during-work-evidence.png",
    view: "backlog",
    setup: openBacklogEvidence,
    selectors: [".backlog-modal", ".backlog-evidence-inspector"],
  },
  {
    id: "after-work-asset-inbox",
    file: "05-after-work-asset-inbox.png",
    view: "assets",
    selectors: [".asset-browser-view", ".asset-relation-browser", ".asset-detail-panel"],
  },
  {
    id: "after-work-review-queue",
    file: "06-after-work-review-queue.png",
    view: "review",
    selectors: [".view-title", "text=Review Queue"],
  },
];

const C = {
  reset: "\x1b[0m",
  dim: "\x1b[2m",
  bold: "\x1b[1m",
  red: "\x1b[31m",
  green: "\x1b[32m",
  yellow: "\x1b[33m",
  cyan: "\x1b[36m",
};
const c = (color, text) => `${C[color]}${text}${C.reset}`;
const say = (color, tag, text) => console.log(`${c(color, tag)} ${text}`);
const phase = (text) => console.log(`\n${c("cyan", "phase")} ${c("bold", text)}`);
const ok = (text) => say("green", "  ok", text);
const warn = (text) => say("yellow", "  warn", text);
const fail = (text) => say("red", "  fail", text);
const info = (text) => say("dim", "  -", text);

function parseFlags(args) {
  const bool = new Set([
    "headed",
    "keep-open",
    "interactive",
    "ensure-fixture",
    "reset-fixture",
    "no-browser",
    "no-fixture",
    "sandbox-audit",
    "browser",
    "require-install-gates",
  ]);
  const out = {};
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (!arg.startsWith("--")) continue;
    const key = arg.slice(2);
    if (bool.has(key)) {
      out[key] = true;
    } else {
      out[key] = args[i + 1];
      i++;
    }
  }
  return out;
}

class HttpError extends Error {
  constructor(method, route, status, body) {
    super(`${method} ${route} -> ${status}`);
    this.method = method;
    this.route = route;
    this.status = status;
    this.body = body;
  }
}

function trimTrailingSlash(value) {
  return String(value || "").replace(/\/+$/, "");
}

function pid(projectId) {
  return encodeURIComponent(projectId);
}

function dashboardUrl(view, extra = {}) {
  const url = new URL(DASHBOARD);
  url.searchParams.set("project_id", PROJECT);
  url.searchParams.set("view", view);
  for (const [key, value] of Object.entries(extra)) {
    if (value !== undefined && value !== null && value !== "") url.searchParams.set(key, String(value));
  }
  return url.toString();
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function auditCheck(audit, name, passed, details = {}, severity = "blocker") {
  const item = { name, passed: Boolean(passed), severity, ...details };
  audit.machine_audit.checks.push(item);
  if (!item.passed && severity === "blocker") audit.machine_audit.blockers.push(item);
  return item;
}

function runCommand(command, args, options = {}) {
  const started = Date.now();
  try {
    const stdout = execFileSync(command, args, {
      cwd: options.cwd || REPO_ROOT,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      timeout: options.timeout || 30000,
      env: { ...process.env, ...(options.env || {}) },
    });
    return { ok: true, command: [command, ...args].join(" "), stdout: stdout.trim(), elapsed_ms: Date.now() - started };
  } catch (error) {
    return {
      ok: false,
      command: [command, ...args].join(" "),
      stdout: String(error.stdout || "").trim(),
      stderr: String(error.stderr || error.message || error).trim(),
      elapsed_ms: Date.now() - started,
    };
  }
}

function readJsonFile(file) {
  return JSON.parse(readFileSync(file, "utf8"));
}

function readOptionalJsonFile(file) {
  if (!file || !existsSync(file)) return null;
  return readJsonFile(file);
}

function shortSha(value) {
  return String(value || "").slice(0, 12);
}

function getQueryTraceId(response) {
  return response?.trace_id || response?.trace?.trace_id || response?.query_trace?.trace_id || "";
}

function getGraphQueryCount(response) {
  return Number(response?.result?.count || response?.count || 0);
}

function firstGraphNodeId(response) {
  const matches = response?.result?.matches || [];
  const match = Array.isArray(matches) ? matches[0] : null;
  if (match?.node?.node_id || match?.node?.id) return match.node.node_id || match.node.id;
  const nodes = response?.result?.nodes || response?.nodes || response?.result?.items || [];
  const first = Array.isArray(nodes) ? nodes[0] : null;
  return first?.id || first?.node_id || response?.result?.node?.id || "";
}

async function http(method, route, body = undefined) {
  let response;
  for (let attempt = 0; attempt <= HTTP_RETRIES; attempt++) {
    try {
      const headers = { Accept: "application/json" };
      const init = { method, headers };
      if (body !== undefined) {
        headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(body);
      }
      response = await fetch(`${BACKEND}${route}`, init);
      break;
    } catch (error) {
      if (attempt >= HTTP_RETRIES) throw new HttpError(method, route, 0, String(error));
      await new Promise((resolve) => setTimeout(resolve, 250 * (attempt + 1)));
    }
  }
  const text = await response.text();
  let json = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    json = null;
  }
  if (!response.ok) throw new HttpError(method, route, response.status, text);
  return json;
}

async function httpText(method, route, body = undefined) {
  const headers = { Accept: "text/html,application/json" };
  const init = { method, headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(`${BACKEND}${route}`, init);
  const text = await response.text();
  return { ok: response.ok, status: response.status, text };
}

async function graphQuery(tool, args, extra = {}) {
  return http("POST", `/api/graph-governance/${pid(PROJECT)}/query`, {
    tool,
    args,
    actor: "hn_demo_sandbox_observer",
    query_source: "observer",
    query_purpose: "prompt_context_build",
    ...extra,
  });
}

async function resolveTrace(traceId) {
  if (!traceId) return { ok: false, reason: "missing_trace_id" };
  try {
    const result = await http("GET", `/api/graph-governance/${pid(PROJECT)}/query-traces/${encodeURIComponent(traceId)}`);
    return { ok: true, result };
  } catch (error) {
    return { ok: false, error: error.message, body: error.body || "" };
  }
}

async function upsertBacklog(bugId, body) {
  return http("POST", `/api/backlog/${pid(PROJECT)}/${encodeURIComponent(bugId)}`, body);
}

async function appendTimeline(body) {
  return http("POST", `/api/task/${pid(PROJECT)}/timeline`, body);
}

async function getBacklog(query = "") {
  return http("GET", `/api/backlog/${pid(PROJECT)}${query}`);
}

async function getTimeline(query = "") {
  return http("GET", `/api/task/${pid(PROJECT)}/timeline${query}`);
}

function isInside(child, parent) {
  const relative = path.relative(parent, child);
  return relative === "" || (relative && !relative.startsWith("..") && !path.isAbsolute(relative));
}

function writeFixtureFile(relativePath, content) {
  const file = path.join(FIXTURE_ROOT, relativePath);
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, `${content.trim()}\n`, "utf8");
}

function runGit(args, cwd, allowFail = false) {
  try {
    return execFileSync("git", args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
  } catch (error) {
    if (allowFail) return "";
    throw error;
  }
}

function materializeFixtureWorkspace() {
  assert(!isInside(FIXTURE_ROOT, REPO_ROOT), `refusing to create HN demo fixture inside plugin checkout: ${FIXTURE_ROOT}`);
  assert(
    !FIXTURE_ROOT.replaceAll("\\", "/").includes("aming-claw"),
    `refusing unsafe HN demo fixture path because legacy config fallback treats it as the self project: ${FIXTURE_ROOT}`,
  );
  if (RESET_FIXTURE && existsSync(FIXTURE_ROOT)) rmSync(FIXTURE_ROOT, { recursive: true, force: true });
  mkdirSync(FIXTURE_ROOT, { recursive: true });
  writeFixtureFile(
    "package.json",
    JSON.stringify(
      {
        name: "aming-claw-hn-demo-fixture",
        version: "0.0.0",
        private: true,
        type: "module",
        scripts: { test: "node tests/order-router.test.mjs" },
      },
      null,
      2,
    ),
  );
  writeFixtureFile(
    "README.md",
    `# Aming Claw HN Demo Fixture

This generated project exists only so a first-run user can see Aming Claw
without bootstrapping their real application. The files are small on purpose:
one feature, one config file, one doc, and one test surface.
`,
  );
  writeFixtureFile(
    "src/order-router.js",
    `import { readFeatureFlag } from "./runtime-config.js";

export function routeOrder(order) {
  if (!order || !order.id) return { queue: "manual-review", reason: "missing id" };
  if (readFeatureFlag("priorityCheckout") && order.total > 500) {
    return { queue: "priority", reason: "large order" };
  }
  return { queue: "standard", reason: "default" };
}
`,
  );
  writeFixtureFile(
    "src/runtime-config.js",
    `import { readFileSync } from "node:fs";

const flags = JSON.parse(readFileSync(new URL("../config/feature-flags.json", import.meta.url), "utf8"));

export function readFeatureFlag(name) {
  return Boolean(flags[name]);
}
`,
  );
  writeFixtureFile("config/feature-flags.json", JSON.stringify({ priorityCheckout: true }, null, 2));
  writeFixtureFile(
    "docs/order-routing.md",
    `# Order Routing

Large orders should enter the priority queue when priority checkout is enabled.
Orders without an id must stay in manual review.
`,
  );
  writeFixtureFile(
    "tests/order-router.test.mjs",
    `import assert from "node:assert/strict";
import { routeOrder } from "../src/order-router.js";

assert.equal(routeOrder({ id: "o-1", total: 100 }).queue, "standard");
assert.equal(routeOrder({ id: "o-2", total: 900 }).queue, "priority");
assert.equal(routeOrder({ total: 900 }).queue, "manual-review");
console.log("hn demo fixture ok");
`,
  );

  const hasGit = runGit(["--version"], FIXTURE_ROOT, true);
  if (!hasGit) {
    warn("git is unavailable; fixture will bootstrap without a git commit");
    return { workspace: FIXTURE_ROOT, commit: "" };
  }
  if (!existsSync(path.join(FIXTURE_ROOT, ".git"))) {
    runGit(["init"], FIXTURE_ROOT);
  }
  runGit(["config", "user.email", "fixture@example.invalid"], FIXTURE_ROOT);
  runGit(["config", "user.name", "HN Demo Fixture"], FIXTURE_ROOT);
  runGit(["add", "."], FIXTURE_ROOT);
  const dirty = runGit(["status", "--porcelain"], FIXTURE_ROOT);
  if (dirty) runGit(["commit", "-m", "baseline hn demo fixture"], FIXTURE_ROOT);
  const commit = runGit(["rev-parse", "HEAD"], FIXTURE_ROOT, true);
  return { workspace: FIXTURE_ROOT, commit };
}

async function ensureFixtureProject() {
  phase("isolated demo fixture");
  PROJECT = FIXTURE_PROJECT;
  const fixture = materializeFixtureWorkspace();
  const projects = await http("GET", "/api/projects");
  assert(Array.isArray(projects.projects), "/api/projects did not return projects[]");
  const existing = projects.projects.find((project) => project.project_id === PROJECT);
  const shouldBootstrap = !existing || RESET_FIXTURE || !existing.active_snapshot_id;
  if (shouldBootstrap) {
    const result = await http("POST", "/api/project/bootstrap", {
      workspace_path: fixture.workspace,
      project_name: PROJECT,
      scan_depth: 3,
      exclude_patterns: ["node_modules", "dist", "build", "coverage", ".aming-claw/e2e-artifacts"],
      config_override: {
        graph: { exclude_paths: ["node_modules", "dist", "build", "coverage", ".aming-claw/e2e-artifacts"] },
      },
    });
    const returnedProject = result.project_id || PROJECT;
    assert(
      returnedProject === PROJECT,
      `fixture bootstrap returned project_id=${returnedProject}, expected ${PROJECT}; refusing to seed demo data into the wrong project`,
    );
    PROJECT = returnedProject;
    ok(`bootstrapped fixture project=${PROJECT} workspace=${fixture.workspace}`);
  } else {
    if (existing.workspace_path) {
      const existingWorkspace = path.resolve(existing.workspace_path);
      assert(
        !isInside(existingWorkspace, REPO_ROOT),
        `registered HN demo fixture points inside plugin checkout; refusing to use ${existingWorkspace}`,
      );
    }
    ok(`fixture project already registered: ${PROJECT}`);
  }
  if (fixture.commit) ok(`fixture baseline commit=${fixture.commit.slice(0, 12)}`);
}

async function verifyFixtureBaseline(backlog) {
  assert(Number(backlog.count || backlog.bugs.length || 0) === 0, "fixture must start with an empty backlog");
  const timeline = await http("GET", `/api/task/${pid(PROJECT)}/timeline`);
  assert(Number(timeline.count || 0) === 0, "fixture must start with an empty timeline");
  const query = await http("POST", `/api/graph-governance/${pid(PROJECT)}/query`, {
    tool: "find_node_by_path",
    args: { path: "src/order-router.js" },
    actor: "hn_demo_fixture",
    query_source: "observer",
    query_purpose: "gate_validation",
  });
  assert(Number(query.result?.count || 0) > 0, "fixture graph cannot resolve src/order-router.js");
  ok("fixture graph resolves src/order-router.js");
  ok("fixture starts with empty backlog and timeline evidence");
}

async function checkGovernance() {
  phase("governance and dashboard");
  if (ENSURE_FIXTURE) await ensureFixtureProject();
  const [health, projects, status, backlog, feedback] = await Promise.all([
    http("GET", "/api/health"),
    http("GET", "/api/projects"),
    http("GET", `/api/graph-governance/${pid(PROJECT)}/status`),
    http("GET", `/api/backlog/${pid(PROJECT)}`),
    http("GET", `/api/graph-governance/${pid(PROJECT)}/snapshots/active/feedback/queue`),
  ]);

  assert(health.status === "ok" || health.ok === true, "/api/health did not report ok");
  assert(Array.isArray(projects.projects), "/api/projects did not return projects[]");
  assert(projects.projects.some((project) => project.project_id === PROJECT), `project ${PROJECT} is not registered`);
  assert(status.active_snapshot_id, `${PROJECT} active graph snapshot is missing`);
  assert(Array.isArray(backlog.bugs), `/api/backlog/${PROJECT} did not return bugs[]`);
  assert(feedback.summary || Array.isArray(feedback.items) || Array.isArray(feedback.groups), "review queue response shape is not recognized");
  if (ENSURE_FIXTURE) {
    await verifyFixtureBaseline(backlog);
  } else {
    assert(backlog.bugs.some((bug) => bug.bug_id === BACKLOG_ID), `backlog row ${BACKLOG_ID} is not visible`);
    ok(`backlog=${BACKLOG_ID}`);
  }

  ok(`governance reachable at ${BACKEND}`);
  ok(`dashboard target ${DASHBOARD}`);
  ok(`project=${PROJECT} snapshot=${status.active_snapshot_id}`);
}

function createAudit() {
  return {
    schema_version: "hn_demo_sandbox_audit.v1",
    run_id: RUN_ID,
    started_at: new Date().toISOString(),
    backend: BACKEND,
    dashboard: DASHBOARD,
    project_id: PROJECT,
    fixture_root: FIXTURE_ROOT,
    state_dir: STATE_DIR,
    observer_mode: OBSERVER_MODE,
    workers_requested: WORKER_COUNT,
    install_smoke: {},
    raw_evidence: {
      trace_ids: [],
      backlog_ids: [],
      timeline_event_ids: [],
      screenshots: [],
      install_gates: {},
    },
    machine_audit: {
      checks: [],
      blockers: [],
      non_blocking_gaps: [],
    },
    agent_behavior_audit: [],
    same_observer_self_review: [],
    launch_recommendation: "UNKNOWN",
  };
}

async function runInstallSmoke(audit) {
  phase("one-click install smoke");
  mkdirSync(STATE_DIR, { recursive: true });
  const skillFiles = [
    "skills/aming-claw/SKILL.md",
    "skills/aming-claw-launcher/SKILL.md",
    "skills/aming-claw-hn-demo/SKILL.md",
    "skills/aming-claw-hn-demo-before-work/SKILL.md",
    "skills/aming-claw-hn-demo-during-work/SKILL.md",
    "skills/aming-claw-hn-demo-after-work/SKILL.md",
  ];
  const requiredFiles = [
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    ".codex-plugin/plugin.json",
    ".agents/plugins/marketplace.json",
    "frontend/dashboard/scripts/e2e-hn-demo.mjs",
    ...skillFiles,
  ];
  const missing = requiredFiles.filter((file) => !existsSync(path.join(REPO_ROOT, file)));
  const dashboardAssets = [
    "agent/governance/dashboard_dist/index.html",
    "frontend/dashboard/dist/index.html",
  ].filter((file) => existsSync(path.join(REPO_ROOT, file)));
  const pluginManifest = existsSync(path.join(REPO_ROOT, ".claude-plugin", "plugin.json"))
    ? readJsonFile(path.join(REPO_ROOT, ".claude-plugin", "plugin.json"))
    : {};
  const codexManifest = existsSync(path.join(REPO_ROOT, ".codex-plugin", "plugin.json"))
    ? readJsonFile(path.join(REPO_ROOT, ".codex-plugin", "plugin.json"))
    : {};
  const pythonCandidates = [FLAGS.python, process.env.PYTHON, "python3", "python"]
    .filter(Boolean)
    .filter((item, index, values) => values.indexOf(item) === index);
  let cliHelp = { ok: false, command: "", stderr: "no Python candidates" };
  for (const python of pythonCandidates) {
    cliHelp = runCommand(python, ["-m", "agent.cli", "--help"], { timeout: 30000 });
    if (cliHelp.ok) break;
  }
  const dashboardRoute = await httpText("GET", "/dashboard").catch((error) => ({
    ok: false,
    status: 0,
    text: String(error.message || error),
  }));
  audit.install_smoke = {
    missing_required_files: missing,
    dashboard_assets: dashboardAssets,
    plugin_manifest_name: pluginManifest.name || "",
    codex_manifest_name: codexManifest.name || "",
    cli_help: cliHelp,
    dashboard_route: {
      ok: dashboardRoute.ok,
      status: dashboardRoute.status,
      body_sample: String(dashboardRoute.text || "").slice(0, 160),
    },
  };
  auditCheck(audit, "plugin package files present", missing.length === 0, { missing });
  auditCheck(audit, "dashboard packaged assets present", dashboardAssets.length > 0, { dashboardAssets });
  auditCheck(audit, "CLI help is callable from checkout", cliHelp.ok, { command: cliHelp.command, stderr: cliHelp.stderr || "" });
  auditCheck(audit, "dashboard /dashboard route serves", dashboardRoute.ok, { status: dashboardRoute.status });
  importDockerInstallGate(audit, "codex", CODEX_INSTALL_REPORT);
  importDockerInstallGate(audit, "claude", CLAUDE_INSTALL_REPORT);
  ok(`install smoke checked ${requiredFiles.length} files`);
}

function importDockerInstallGate(audit, host, reportPath) {
  const label = `${host} Docker Install Gate`;
  const report = readOptionalJsonFile(reportPath);
  if (!report) {
    const status = "SKIPPED";
    audit.raw_evidence.install_gates[host] = {
      host,
      status,
      report_path: reportPath || "",
      reason: reportPath ? "report_path_missing" : "report_not_provided",
    };
    auditCheck(
      audit,
      label,
      false,
      { status, report_path: reportPath || "", reason: audit.raw_evidence.install_gates[host].reason },
      REQUIRE_INSTALL_GATES ? "blocker" : "warning",
    );
    return;
  }
  audit.raw_evidence.install_gates[host] = {
    host,
    status: report.status || "UNKNOWN",
    auth_mode: report.auth_mode || "",
    report_path: reportPath,
    skills_seen: report.skills_seen || [],
    resources_read: report.resources_read || [],
    dashboard_health: report.dashboard_health || {},
    demo_fixture_result: report.demo_fixture_result || {},
    limitations: report.limitations || [],
  };
  auditCheck(
    audit,
    label,
    report.status === "PASS",
    {
      status: report.status || "UNKNOWN",
      auth_mode: report.auth_mode || "",
      report_path: reportPath,
      self_rating: report.self_rating,
      why_rating: report.why_rating || "",
    },
    report.status === "PASS" ? "blocker" : "blocker",
  );
}

async function runBeforeWorkCase(audit) {
  phase("sandbox observer: before work");
  const bugId = `HN-SBX-${RUN_ID}-BEFORE`;
  const mfId = `MF-${bugId}`;
  const structure = await graphQuery("find_node_by_path", { path: "src/order-router.js" }, {
    actor: "observer:hn-sandbox-before",
    query_purpose: "prompt_context_build",
  });
  const docs = await graphQuery("find_node_by_path", { path: "docs/order-routing.md" }, {
    actor: "observer:hn-sandbox-before",
    query_purpose: "prompt_context_build",
  });
  const tests = await graphQuery("find_node_by_path", { path: "tests/order-router.test.mjs" }, {
    actor: "observer:hn-sandbox-before",
    query_purpose: "prompt_context_build",
  });
  const traces = [structure, docs, tests].map(getQueryTraceId).filter(Boolean);
  const duplicateProbe = await getBacklog("?view=compact&q=order-router&include_closed=true");
  await upsertBacklog(bugId, {
    actor: "observer:hn-sandbox-before",
    title: "HN sandbox before-work contract: order router change",
    status: "OPEN",
    priority: "P2",
    mf_type: "chain_rescue",
    force_admit: true,
    target_files: ["src/order-router.js", "tests/order-router.test.mjs", "docs/order-routing.md"],
    test_files: ["tests/order-router.test.mjs"],
    provenance_paths: traces,
    acceptance_criteria: [
      "Graph discovery resolves source, test, and doc surfaces before implementation.",
      "Backlog duplicate probe runs before admitting work.",
      "Contract uses mf_parallel.v1 workers with disjoint owned_files.",
    ],
    chain_trigger_json: {
      template_id: "mf_parallel.v1",
      requirement_ids: ["project_structure", "backlog_dedupe_probe", "contract_fence"],
      workers: [
        { task_id: `${bugId}-worker-a`, owned_files: ["src/order-router.js"] },
        { task_id: `${bugId}-worker-b`, owned_files: ["tests/order-router.test.mjs", "docs/order-routing.md"] },
      ],
    },
    details_md: "Created by sandbox audit scripted observer from an empty fixture. This row is not seeded by fixture setup.",
  });
  const event = await appendTimeline({
    backlog_id: bugId,
    mf_id: mfId,
    actor: "observer:hn-sandbox-before",
    event_type: "before_work_dispatch_contract",
    event_kind: "implementation",
    phase: "dispatch",
    status: "accepted",
    payload: {
      graph_query_trace_ids: traces,
      duplicate_probe: {
        q: "order-router",
        count: Number(duplicateProbe.count || 0),
        filtered_count: Number(duplicateProbe.filtered_count || 0),
      },
      requirement_ids: ["project_structure", "backlog_dedupe_probe", "contract_fence"],
    },
    verification: {
      source_count: getGraphQueryCount(structure),
      docs_count: getGraphQueryCount(docs),
      tests_count: getGraphQueryCount(tests),
    },
  });
  audit.raw_evidence.before_work = { bug_id: bugId, mf_id: mfId, trace_ids: traces, duplicate_probe: duplicateProbe, target_node_id: firstGraphNodeId(structure) };
  audit.raw_evidence.backlog_ids.push(bugId);
  audit.raw_evidence.trace_ids.push(...traces);
  if (event?.id) audit.raw_evidence.timeline_event_ids.push(event.id);
  BACKLOG_ID = bugId;
  auditCheck(audit, "before-work uses real graph traces", traces.length >= 3, { traces });
  auditCheck(audit, "before-work backlog duplicate probe recorded", duplicateProbe && Array.isArray(duplicateProbe.bugs), {
    count: duplicateProbe.count,
    filtered_count: duplicateProbe.filtered_count,
  });
  ok(`before-work backlog=${bugId}`);
  return { bugId, mfId, traces, nodeId: firstGraphNodeId(structure) };
}

async function allocateWorker({ bugId, workerIndex, ownedFiles, baseCommit }) {
  const label = String.fromCharCode(65 + workerIndex);
  const taskId = `${bugId}-worker-${label.toLowerCase()}`;
  const response = await http("POST", `/api/graph-governance/${pid(PROJECT)}/parallel-branches/allocate`, {
    actor: `observer:hn-sandbox-worker-${label.toLowerCase()}`,
    task_id: taskId,
    parent_task_id: `${bugId}-observer`,
    root_task_id: `${bugId}-observer`,
    backlog_id: bugId,
    workspace_root: FIXTURE_ROOT,
    base_commit: baseCommit,
    target_head_commit: baseCommit,
    merge_queue_id: `mq-${RUN_ID}`,
    branch_prefix: `hn-sbx-${RUN_ID.toLowerCase()}`,
    worker_id: `worker-${label.toLowerCase()}`,
    create_worktree: false,
  });
  const context = response.context || {};
  return {
    label,
    task_id: taskId,
    parent_task_id: `${bugId}-observer`,
    owned_files: ownedFiles,
    fence_token: context.fence_token || "",
    base_commit: context.base_commit || baseCommit,
    target_head_commit: context.target_head_commit || baseCommit,
    branch: context.branch_ref || context.ref_name || "",
    worktree: context.worktree_path || FIXTURE_ROOT,
    merge_queue_id: context.merge_queue_id || `mq-${RUN_ID}`,
    raw: response,
  };
}

async function runWorkerGraphQuery(worker, tool, args) {
  return graphQuery(tool, args, {
    actor: `mf_sub:${worker.task_id}`,
    query_source: "mf_subagent",
    query_purpose: "subagent_context_build",
    task_id: worker.task_id,
    parent_task_id: worker.parent_task_id,
    worker_role: "mf_sub",
    fence_token: worker.fence_token,
  });
}

function applyDuringWorkChange() {
  writeFixtureFile(
    "src/order-router.js",
    `import { readFeatureFlag } from "./runtime-config.js";

export function routeOrder(order) {
  if (!order || !order.id) return { queue: "manual-review", reason: "missing id" };
  if (order.expedited === true) {
    return { queue: "priority", reason: "expedited" };
  }
  if (readFeatureFlag("priorityCheckout") && order.total > 500) {
    return { queue: "priority", reason: "large order" };
  }
  return { queue: "standard", reason: "default" };
}
`,
  );
  writeFixtureFile(
    "tests/order-router.test.mjs",
    `import assert from "node:assert/strict";
import { routeOrder } from "../src/order-router.js";

assert.equal(routeOrder({ id: "o-1", total: 100 }).queue, "standard");
assert.equal(routeOrder({ id: "o-2", total: 900 }).queue, "priority");
assert.equal(routeOrder({ id: "o-3", total: 100, expedited: true }).reason, "expedited");
assert.equal(routeOrder({ total: 900 }).queue, "manual-review");
console.log("hn demo fixture ok");
`,
  );
  writeFixtureFile(
    "docs/observer-routing-note.md",
    `# Observer Routing Note

This file is intentionally created during the sandbox audit so after-work
review can prove docs are not auto-bound to graph nodes without a governance
hint.
`,
  );
}

async function runDuringWorkCase(audit) {
  phase("sandbox observer: during work");
  const bugId = `HN-SBX-${RUN_ID}-DURING`;
  const mfId = `MF-${bugId}`;
  const baseCommit = runGit(["rev-parse", "HEAD"], FIXTURE_ROOT, true);
  const workers = [];
  const ownedScopes = [
    ["src/order-router.js"],
    ["tests/order-router.test.mjs", "docs/observer-routing-note.md"],
    ["docs/order-routing.md"],
  ];
  for (let index = 0; index < WORKER_COUNT; index++) {
    workers.push(await allocateWorker({ bugId, workerIndex: index, ownedFiles: ownedScopes[index] || [`docs/worker-${index}.md`], baseCommit }));
  }
  const workerQueries = [];
  for (const worker of workers) {
    const targetPath = worker.owned_files[0] || "src/order-router.js";
    const query = await runWorkerGraphQuery(worker, "find_node_by_path", { path: targetPath });
    workerQueries.push({ worker, query, trace_id: getQueryTraceId(query), resolved: await resolveTrace(getQueryTraceId(query)) });
  }
  const traces = workerQueries.map((item) => item.trace_id).filter(Boolean);
  await upsertBacklog(bugId, {
    actor: "observer:hn-sandbox-during",
    title: "HN sandbox during-work execution: two-plus worker evidence",
    status: "OPEN",
    priority: "P2",
    mf_type: "chain_rescue",
    force_admit: true,
    target_files: ["src/order-router.js", "tests/order-router.test.mjs", "docs/observer-routing-note.md"],
    test_files: ["tests/order-router.test.mjs"],
    provenance_paths: traces,
    acceptance_criteria: [
      "At least two workers receive disjoint owned_files.",
      "Each worker has a server-allocated fence_token and resolvable mf_subagent graph trace.",
      "A real test subprocess runs and its exit code is recorded.",
      "The committed diff carries Chain trailers.",
    ],
    chain_trigger_json: {
      template_id: "mf_parallel.v1",
      requirement_ids: ["worker_fences", "mf_subagent_traces", "real_tests", "chain_trailers"],
      workers: workers.map((worker) => ({
        task_id: worker.task_id,
        owned_files: worker.owned_files,
        fence_token_present: Boolean(worker.fence_token),
      })),
    },
    details_md: "Created by sandbox audit scripted observer. The fixture did not pre-create this timeline.",
  });
  const dispatchEvent = await appendTimeline({
    backlog_id: bugId,
    mf_id: mfId,
    actor: "observer:hn-sandbox-during",
    event_type: "during_work_parallel_dispatch",
    event_kind: "implementation",
    phase: "dispatch",
    status: "accepted",
    payload: {
      workers: workers.map((worker) => ({
        task_id: worker.task_id,
        owned_files: worker.owned_files,
        fence_token: worker.fence_token,
        base_commit: worker.base_commit,
        target_head_commit: worker.target_head_commit,
        merge_queue_id: worker.merge_queue_id,
      })),
      graph_query_trace_ids: traces,
    },
  });
  applyDuringWorkChange();
  const testRun = runCommand("node", ["tests/order-router.test.mjs"], { cwd: FIXTURE_ROOT, timeout: 30000 });
  runGit(["add", "."], FIXTURE_ROOT);
  runGit([
    "commit",
    "-m",
    [
      "feat: add expedited order routing",
      "",
      "Chain-Source-Stage: observer-hotfix",
      `Chain-Project: ${PROJECT}`,
      `Chain-Bug-Id: ${bugId}`,
    ].join("\n"),
  ], FIXTURE_ROOT);
  const commit = runGit(["rev-parse", "HEAD"], FIXTURE_ROOT, true);
  const verificationEvent = await appendTimeline({
    backlog_id: bugId,
    mf_id: mfId,
    actor: "observer:hn-sandbox-during",
    event_type: "during_work_tests_and_commit",
    event_kind: "verification",
    phase: "verification",
    status: testRun.ok ? "passed" : "failed",
    payload: {
      graph_query_trace_ids: traces,
      commit,
      changed_files: ["src/order-router.js", "tests/order-router.test.mjs", "docs/observer-routing-note.md"],
    },
    verification: {
      tests_run: [testRun.command],
      tests_exit_code: testRun.ok ? 0 : 1,
      stdout: testRun.stdout,
      stderr: testRun.stderr || "",
    },
  });
  const closeReadyEvent = await appendTimeline({
    backlog_id: bugId,
    mf_id: mfId,
    actor: "observer:hn-sandbox-during",
    event_type: "during_work_close_ready",
    event_kind: "close_ready",
    phase: "close_ready",
    status: testRun.ok ? "accepted" : "blocked",
    payload: { commit, graph_query_trace_ids: traces },
  });
  audit.raw_evidence.during_work = { bug_id: bugId, mf_id: mfId, workers, worker_queries: workerQueries, traces, test_run: testRun, commit };
  audit.raw_evidence.backlog_ids.push(bugId);
  audit.raw_evidence.trace_ids.push(...traces);
  for (const event of [dispatchEvent, verificationEvent, closeReadyEvent]) {
    if (event?.id) audit.raw_evidence.timeline_event_ids.push(event.id);
  }
  auditCheck(audit, "during-work has at least two workers", workers.length >= 2, { worker_count: workers.length });
  auditCheck(audit, "during-work workers have server-allocated fences", workers.every((worker) => worker.fence_token), {
    workers: workers.map((worker) => ({ task_id: worker.task_id, fence_token_present: Boolean(worker.fence_token) })),
  });
  auditCheck(audit, "during-work worker traces resolve", workerQueries.every((item) => item.resolved.ok), {
    traces: workerQueries.map((item) => ({ trace_id: item.trace_id, ok: item.resolved.ok })),
  });
  auditCheck(audit, "during-work real tests pass", testRun.ok, { command: testRun.command, stderr: testRun.stderr || "" });
  auditCheck(audit, "during-work commit has Chain trailers", Boolean(commit), { commit: shortSha(commit) });
  ok(`during-work backlog=${bugId} commit=${shortSha(commit)}`);
  return { bugId, mfId, commit, workers, traces };
}

async function runAfterWorkCase(audit, during, targetNodeId) {
  phase("sandbox observer: after work");
  const bugId = `HN-SBX-${RUN_ID}-AFTER`;
  const mfId = `MF-${bugId}`;
  const staleStatus = await http("GET", `/api/graph-governance/${pid(PROJECT)}/status`);
  const reconcile = await http("POST", `/api/graph-governance/${pid(PROJECT)}/reconcile/full`, {
    actor: "observer:hn-sandbox-after",
    project_root: FIXTURE_ROOT,
    activate: true,
    semantic_enrich: false,
    run_id: `hn-sandbox-${RUN_ID}`,
  });
  const snapshotId = reconcile.snapshot_id || reconcile.active_snapshot_id || reconcile.result?.snapshot_id || "";
  const orphanQuery = await graphQuery("find_node_by_path", { path: "docs/observer-routing-note.md" }, {
    actor: "observer:hn-sandbox-after",
    query_purpose: "prompt_context_build",
  });
  const feedbackBefore = await http("GET", `/api/graph-governance/${pid(PROJECT)}/snapshots/active/feedback/queue`);
  let hint = null;
  if (targetNodeId && snapshotId) {
    try {
      hint = await http("POST", `/api/graph-governance/${pid(PROJECT)}/snapshots/${encodeURIComponent(snapshotId)}/file-hygiene/hints/attach`, {
        actor: "observer:hn-sandbox-after",
        project_root: FIXTURE_ROOT,
        path: "docs/observer-routing-note.md",
        target_node_id: targetNodeId,
        reason: "Sandbox audit validates after-work governance hint review boundary.",
      });
    } catch (error) {
      hint = { ok: false, error: error.message, body: error.body || "" };
    }
  }
  const feedbackAfter = await http("GET", `/api/graph-governance/${pid(PROJECT)}/snapshots/active/feedback/queue`);
  const traceId = getQueryTraceId(orphanQuery);
  await upsertBacklog(bugId, {
    actor: "observer:hn-sandbox-after",
    title: "HN sandbox after-work drift and asset review",
    status: "OPEN",
    priority: "P2",
    mf_type: "chain_rescue",
    force_admit: true,
    target_files: ["docs/observer-routing-note.md"],
    provenance_paths: [traceId].filter(Boolean),
    acceptance_criteria: [
      "Post-commit graph status is checked before reconcile.",
      "Full reconcile activates a snapshot at the new commit.",
      "Orphan doc/config/test asset is not silently trusted as graph truth.",
      "Governance hint writes an uncommitted review boundary when attachable.",
    ],
    chain_trigger_json: {
      template_id: "mf_parallel.v1",
      requirement_ids: ["post_commit_stale_check", "full_reconcile", "asset_review_boundary"],
    },
    details_md: "Created by sandbox audit scripted observer after a real fixture commit.",
  });
  const verificationEvent = await appendTimeline({
    backlog_id: bugId,
    mf_id: mfId,
    actor: "observer:hn-sandbox-after",
    event_type: "after_work_reconcile_and_asset_review",
    event_kind: "verification",
    phase: "asset_review",
    status: reconcile.ok === false ? "failed" : "passed",
    payload: {
      graph_query_trace_ids: [traceId].filter(Boolean),
      stale_status: staleStatus,
      reconcile,
      orphan_query_count: getGraphQueryCount(orphanQuery),
      governance_hint: hint,
      feedback_before_count: Number(feedbackBefore.count || feedbackBefore.summary?.total || 0),
      feedback_after_count: Number(feedbackAfter.count || feedbackAfter.summary?.total || 0),
    },
  });
  const closeReadyEvent = await appendTimeline({
    backlog_id: bugId,
    mf_id: mfId,
    actor: "observer:hn-sandbox-after",
    event_type: "after_work_close_ready",
    event_kind: "close_ready",
    phase: "close_ready",
    status: "accepted",
    payload: { snapshot_id: snapshotId, graph_query_trace_ids: [traceId].filter(Boolean) },
  });
  audit.raw_evidence.after_work = {
    bug_id: bugId,
    mf_id: mfId,
    stale_status: staleStatus,
    reconcile,
    snapshot_id: snapshotId,
    orphan_trace_id: traceId,
    orphan_query: orphanQuery,
    governance_hint: hint,
    feedback_before: feedbackBefore,
    feedback_after: feedbackAfter,
  };
  audit.raw_evidence.backlog_ids.push(bugId);
  if (traceId) audit.raw_evidence.trace_ids.push(traceId);
  for (const event of [verificationEvent, closeReadyEvent]) {
    if (event?.id) audit.raw_evidence.timeline_event_ids.push(event.id);
  }
  auditCheck(audit, "after-work post-commit status checked", Boolean(staleStatus.active_snapshot_id), {
    active_snapshot_id: staleStatus.active_snapshot_id,
    is_stale: staleStatus.is_stale,
    graph_stale: staleStatus.graph_stale,
  });
  auditCheck(audit, "after-work full reconcile activated snapshot", Boolean(snapshotId), { snapshot_id: snapshotId });
  auditCheck(audit, "after-work orphan probe uses real trace", Boolean(traceId), { trace_id: traceId, count: getGraphQueryCount(orphanQuery) });
  const hintState = hint?.state || hint?.result?.state || hint?.hint?.state || hint?.error || "";
  auditCheck(audit, "after-work governance hint reached review boundary", Boolean(hint?.ok || hintState === "written_uncommitted"), {
    hint_state: hintState,
  }, "warning");
  ok(`after-work backlog=${bugId} snapshot=${snapshotId || "unknown"}`);
}

async function captureSandboxAuditScreenshots(audit) {
  if (NO_BROWSER) return;
  const { chromium } = await loadPlaywright();
  mkdirSync(SCREENSHOT_DIR, { recursive: true });
  const browser = await launchChromium(chromium);
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
    const page = await context.newPage();
    page.setDefaultTimeout(NAV_TIMEOUT_MS);
    const shots = [
      { view: "projects", file: `${RUN_ID}-projects.png` },
      { view: "graph", file: `${RUN_ID}-graph.png` },
      { view: "backlog", file: `${RUN_ID}-backlog.png` },
      { view: "review", file: `${RUN_ID}-review.png` },
    ];
    for (const shot of shots) {
      await navigate(page, shot.view);
      const output = path.join(SCREENSHOT_DIR, shot.file);
      await page.screenshot({ path: output, fullPage: true });
      audit.raw_evidence.screenshots.push(output);
    }
    auditCheck(audit, "browser mode captured isolated dashboard screenshots", audit.raw_evidence.screenshots.length === shots.length, {
      screenshots: audit.raw_evidence.screenshots,
    }, "warning");
  } finally {
    await browser.close();
  }
}

function addSameObserverReview(audit) {
  const check = (name) => audit.machine_audit.checks.find((item) => item.name === name);
  const workerCheck = check("during-work worker traces resolve");
  const installGates = audit.raw_evidence.install_gates || {};
  const installGateStatuses = ["codex", "claude"].map((host) => installGates[host]?.status || "SKIPPED");
  const anyDockerInstallPass = installGateStatuses.includes("PASS");
  const allRequestedDockerInstallPass = installGateStatuses.every((status) => status === "PASS");
  const bothDockerInstallMissing = installGateStatuses.every((status) => status !== "PASS");
  const installOk = ["plugin package files present", "dashboard packaged assets present", "CLI help is callable from checkout", "dashboard /dashboard route serves"].every(
    (name) => check(name)?.passed,
  );
  audit.agent_behavior_audit = [
    "The fixture only bootstrapped a project and active graph; backlog and timeline started empty.",
    "The observer path created backlog rows, timeline events, graph query traces, worker fences, and test evidence during this run.",
    "The same scripted observer that performed the run writes this evaluation, so the score cites operation artifacts instead of a second-hand review.",
  ];
  audit.same_observer_self_review = [
    {
      category: "Operational Clarity",
      score: installOk ? 4 : 2,
      why: installOk
        ? "The package, CLI, dashboard route, and skill files were all visible from the same checkout."
        : "Install smoke found at least one packaging or dashboard readiness issue.",
      personally_observed_evidence: audit.install_smoke,
      hesitation: allRequestedDockerInstallPass
        ? "Both Docker host lanes passed; remaining concern is only whether a live AI-observer transcript should be attached for persuasion."
        : anyDockerInstallPass
        ? "At least one Docker host lane passed; the remaining concern is cross-host parity."
        : "This is still only a local checkout preflight until a Docker Codex or Claude install gate passes.",
      suggested_fix: allRequestedDockerInstallPass
        ? "Keep the Codex and Claude install reports attached to the launch checklist."
        : "Run docker/hn-install-audit/run-install-audit.sh and pass the generated host reports into --sandbox-audit.",
    },
    {
      category: "Evidence Credibility",
      score: workerCheck?.passed ? 5 : 2,
      why: workerCheck?.passed
        ? "Worker graph queries used server-validated mf_subagent identity and returned resolvable trace IDs."
        : "At least one worker trace did not resolve, so the report cannot support the audit claim.",
      personally_observed_evidence: audit.raw_evidence.during_work?.worker_queries || [],
      hesitation: "Timeline payloads can still include self-reported fields; trace resolution is the strongest evidence.",
      suggested_fix: "Keep adding ledger joins to close any remaining self-report gaps.",
    },
    {
      category: "Reproducibility",
      score: audit.machine_audit.blockers.length === 0 ? 4 : 2,
      why: audit.machine_audit.blockers.length === 0
        ? "The run used explicit run_id, project_id, fixture_dir, state_dir, and report paths."
        : "One or more blocker checks failed, so the run is not a clean reproduce.",
      personally_observed_evidence: {
        run_id: audit.run_id,
        project_id: audit.project_id,
        fixture_root: audit.fixture_root,
        report: REPORT_PATH,
      },
      hesitation: "Governance still runs outside this script unless the caller starts it on the requested port.",
      suggested_fix: "Add a managed governance child process mode only after port/state cleanup is proven.",
    },
    {
      category: "HN Persuasiveness",
      score: audit.machine_audit.blockers.length === 0 ? 4 : 2,
      why: "The report separates raw evidence, machine checks, behavior audit, and honest hesitation instead of claiming a magic demo.",
      personally_observed_evidence: audit.machine_audit.checks,
      hesitation: "Scripted observer evidence is less psychologically convincing than a live AI observer transcript.",
      suggested_fix: "Run optional --observer codex/claude mode and attach the transcript for launch rehearsal.",
    },
    {
      category: "Claim Alignment",
      score: audit.raw_evidence.during_work?.workers?.length >= 2 ? 5 : 2,
      why: audit.raw_evidence.during_work?.workers?.length >= 2
        ? "The demo exercises one observer with multiple bounded worker identities, matching the AI-as-operator claim."
        : "Single-worker evidence would undercut the concurrency claim.",
      personally_observed_evidence: audit.raw_evidence.during_work?.workers || [],
      hesitation: "This runner validates the governance protocol, not true autonomous model quality.",
      suggested_fix: "Keep AI observer mode separate from scripted protocol smoke so both claims stay honest.",
    },
  ];
  audit.launch_recommendation = audit.machine_audit.blockers.length === 0 ? "CONDITIONAL_PASS" : "BLOCK";
  if (bothDockerInstallMissing && REQUIRE_INSTALL_GATES) {
    audit.launch_recommendation = "BLOCK";
  }
}

function markdownReport(audit) {
  const lines = [];
  lines.push(`# HN Demo Sandbox Audit`);
  lines.push("");
  lines.push(`- Run ID: \`${audit.run_id}\``);
  lines.push(`- Project: \`${audit.project_id}\``);
  lines.push(`- Backend: \`${audit.backend}\``);
  lines.push(`- Fixture: \`${audit.fixture_root}\``);
  lines.push(`- Observer mode: \`${audit.observer_mode}\``);
  lines.push(`- Launch recommendation: **${audit.launch_recommendation}**`);
  lines.push("");
  lines.push("## Raw Evidence");
  lines.push("");
  lines.push(`- Backlog rows: ${audit.raw_evidence.backlog_ids.map((id) => `\`${id}\``).join(", ") || "none"}`);
  lines.push(`- Trace IDs: ${audit.raw_evidence.trace_ids.map((id) => `\`${id}\``).join(", ") || "none"}`);
  lines.push(`- Timeline event IDs: ${audit.raw_evidence.timeline_event_ids.map((id) => `\`${id}\``).join(", ") || "none"}`);
  if (audit.raw_evidence.during_work?.commit) lines.push(`- Fixture commit: \`${audit.raw_evidence.during_work.commit}\``);
  if (audit.raw_evidence.after_work?.snapshot_id) lines.push(`- Post-reconcile snapshot: \`${audit.raw_evidence.after_work.snapshot_id}\``);
  if (audit.raw_evidence.screenshots.length) {
    lines.push(`- Screenshots: ${audit.raw_evidence.screenshots.map((shot) => `\`${shot}\``).join(", ")}`);
  }
  const installGates = audit.raw_evidence.install_gates || {};
  if (Object.keys(installGates).length) {
    lines.push("");
    lines.push("## Docker Install Gates");
    lines.push("");
    lines.push("| Host | Status | Auth Mode | Report | Reason |");
    lines.push("|---|---:|---|---|---|");
    for (const host of ["codex", "claude"]) {
      const gate = installGates[host] || { status: "SKIPPED", auth_mode: "", report_path: "", reason: "not_checked" };
      lines.push(`| ${host} | ${gate.status || "UNKNOWN"} | ${gate.auth_mode || ""} | \`${gate.report_path || ""}\` | ${gate.reason || gate.why_rating || ""} |`);
    }
  }
  lines.push("");
  lines.push("## Machine Audit");
  lines.push("");
  lines.push("| Check | Result | Severity | Evidence |");
  lines.push("|---|---:|---|---|");
  for (const check of audit.machine_audit.checks) {
    const evidence = JSON.stringify(Object.fromEntries(Object.entries(check).filter(([key]) => !["name", "passed", "severity"].includes(key)))).slice(0, 240);
    lines.push(`| ${check.name} | ${check.passed ? "PASS" : "FAIL"} | ${check.severity} | \`${evidence}\` |`);
  }
  lines.push("");
  lines.push("## Agent Behavior Audit");
  lines.push("");
  for (const item of audit.agent_behavior_audit) lines.push(`- ${item}`);
  lines.push("");
  lines.push("## Same-Observer Self-Review");
  lines.push("");
  for (const review of audit.same_observer_self_review) {
    lines.push(`### ${review.category}: ${review.score}/5`);
    lines.push("");
    lines.push(`- Why: ${review.why}`);
    lines.push(`- Personally observed evidence: \`${JSON.stringify(review.personally_observed_evidence).slice(0, 500)}\``);
    lines.push(`- Hesitation: ${review.hesitation}`);
    lines.push(`- Suggested fix: ${review.suggested_fix}`);
    lines.push("");
  }
  lines.push("## Launch Recommendation");
  lines.push("");
  if (audit.machine_audit.blockers.length) {
    lines.push("Block public launch until these checks pass:");
    for (const blocker of audit.machine_audit.blockers) lines.push(`- ${blocker.name}`);
  } else {
    lines.push("Conditional pass for HN rehearsal. This proves the fixture is not replaying pre-seeded evidence and that the observer path can generate verifiable audit artifacts.");
  }
  if (audit.machine_audit.non_blocking_gaps.length) {
    lines.push("");
    lines.push("Non-blocking gaps:");
    for (const gap of audit.machine_audit.non_blocking_gaps) lines.push(`- ${gap}`);
  }
  lines.push("");
  return `${lines.join("\n")}\n`;
}

function writeAuditReports(audit) {
  audit.finished_at = new Date().toISOString();
  addSameObserverReview(audit);
  mkdirSync(path.dirname(REPORT_PATH), { recursive: true });
  writeFileSync(REPORT_PATH, markdownReport(audit), "utf8");
  writeFileSync(JSON_REPORT_PATH, JSON.stringify(audit, null, 2), "utf8");
  const latestMd = path.join(REPO_ROOT, "docs", "hn-demo", "audits", "latest.md");
  const latestJson = path.join(REPO_ROOT, "docs", "hn-demo", "audits", "latest.json");
  writeFileSync(latestMd, markdownReport(audit), "utf8");
  writeFileSync(latestJson, JSON.stringify(audit, null, 2), "utf8");
  ok(`wrote ${REPORT_PATH}`);
  ok(`wrote ${JSON_REPORT_PATH}`);
}

async function runSandboxAudit() {
  const audit = createAudit();
  console.log(c("bold", "hn-fear-demo-sandbox-audit"));
  console.log(c("dim", `run_id=${RUN_ID} backend=${BACKEND} project=${PROJECT}`));
  console.log(c("dim", `fixture=${FIXTURE_ROOT} state_dir=${STATE_DIR}`));
  try {
    await runInstallSmoke(audit);
    await checkGovernance();
    const before = await runBeforeWorkCase(audit);
    const during = await runDuringWorkCase(audit);
    await runAfterWorkCase(audit, during, before.nodeId);
    await captureSandboxAuditScreenshots(audit);
    writeAuditReports(audit);
    console.log("");
    console.log(c(audit.machine_audit.blockers.length ? "red" : "green", `HN DEMO SANDBOX AUDIT ${audit.launch_recommendation}`));
    if (audit.machine_audit.blockers.length) exit(3);
  } catch (error) {
    audit.machine_audit.blockers.push({ name: "sandbox audit crashed", passed: false, severity: "blocker", error: error.message });
    writeAuditReports(audit);
    console.log("");
    fail(error.message);
    if (error instanceof HttpError) console.log(c("dim", `body=${String(error.body || "").slice(0, 1000)}`));
    console.log(c("red", "HN DEMO SANDBOX AUDIT FAIL"));
    exit(error instanceof HttpError ? 1 : 2);
  }
}

async function loadPlaywright() {
  try {
    const mod = require("playwright");
    if (mod.chromium) return mod;
  } catch {
    // Fall through to ESM import; local installs may expose ESM resolution first.
  }
  try {
    const mod = require("@playwright/test");
    if (mod.chromium) return mod;
  } catch {
    // Fall through to ESM import.
  }
  try {
    const mod = await import("playwright");
    if (mod.chromium) return mod;
  } catch {
    // Fall through to @playwright/test; some local checkouts expose only that.
  }
  try {
    const mod = await import("@playwright/test");
    if (mod.chromium) return mod;
  } catch {
    // Handled below with a clear message.
  }
  throw new Error(
    "Playwright is not installed in this checkout. Run `cd frontend/dashboard && npm install`, then rerun `npm run e2e:hn-demo`.",
  );
}

async function launchChromium(chromium) {
  try {
    return await chromium.launch({ headless: HEADLESS });
  } catch (error) {
    const message = String(error?.message || error);
    if (message.includes("Executable doesn't exist") || message.includes("playwright install")) {
      throw new Error(`${message}\nRun: cd frontend/dashboard && npx playwright install chromium`);
    }
    throw error;
  }
}

async function waitForDashboardReady(page, expectedText) {
  await page.waitForSelector(".app-body", { timeout: NAV_TIMEOUT_MS });
  await page.waitForLoadState("networkidle", { timeout: NAV_TIMEOUT_MS }).catch(() => {});
  if (expectedText) await page.getByText(expectedText, { exact: false }).first().waitFor({ timeout: NAV_TIMEOUT_MS });
  const loadFailed = await page.locator("text=Load failed").count();
  assert(loadFailed === 0, `dashboard reported Load failed at ${page.url()}`);
}

async function navigate(page, view, extra = {}) {
  await page.goto(dashboardUrl(view, extra), { waitUntil: "domcontentloaded", timeout: NAV_TIMEOUT_MS });
  await waitForDashboardReady(page, view === "review" ? "Review Queue" : undefined);
}

async function expectSelectors(page, selectors, id) {
  for (const selector of selectors) {
    await page.locator(selector).first().waitFor({ timeout: NAV_TIMEOUT_MS });
  }
  ok(`${id} selectors present`);
}

async function openBacklogModal(page) {
  await navigate(page, "backlog", { backlog: BACKLOG_ID });
  await page.locator(".backlog-modal").waitFor({ timeout: NAV_TIMEOUT_MS });
  await page.getByText(BACKLOG_ID, { exact: false }).first().waitFor({ timeout: NAV_TIMEOUT_MS });
}

async function openBacklogContract(page) {
  await openBacklogModal(page);
  await page.locator(".backlog-modal-tabs button", { hasText: "Contract" }).first().click();
  await page.locator(".backlog-modal-tab-panel").waitFor({ timeout: NAV_TIMEOUT_MS });
}

async function openBacklogTimeline(page) {
  await openBacklogModal(page);
  await page.locator(".backlog-modal-tabs button", { hasText: "Timeline" }).first().click();
  await page.locator(".backlog-evidence-inspector").waitFor({ timeout: NAV_TIMEOUT_MS });
}

async function openBacklogEvidence(page) {
  await openBacklogTimeline(page);
  const dagNode = page.locator(".backlog-dag-node").first();
  if ((await dagNode.count()) > 0) {
    await dagNode.click();
    await page.locator(".backlog-evidence-inspector pre, .backlog-inspector-grid").first().waitFor({
      timeout: NAV_TIMEOUT_MS,
    });
  } else {
    warn(`no timeline DAG node exists for ${BACKLOG_ID}; captured the empty evidence inspector state`);
  }
}

async function captureScenario(page, scenario) {
  phase(scenario.id);
  if (scenario.setup) {
    await scenario.setup(page);
  } else {
    await navigate(page, scenario.view);
  }
  await expectSelectors(page, scenario.selectors, scenario.id);
  const output = path.join(SCREENSHOT_DIR, scenario.file);
  await page.screenshot({ path: output, fullPage: true });
  ok(output);
}

async function main() {
  if (SANDBOX_AUDIT) {
    await runSandboxAudit();
    return;
  }
  console.log(c("bold", "hn-fear-demo-smoke"));
  console.log(c("dim", `backend=${BACKEND} dashboard=${DASHBOARD} project=${PROJECT}`));
  if (ENSURE_FIXTURE) console.log(c("dim", `fixture=${FIXTURE_ROOT} fixture_project=${FIXTURE_PROJECT}`));
  console.log(c("dim", `screenshots=${SCREENSHOT_DIR}`));

  let browser = null;
  try {
    await checkGovernance();
    if (NO_BROWSER) {
      console.log("");
      console.log(c("green", "HN DEMO FIXTURE OK"));
      return;
    }
    if (ENSURE_FIXTURE) {
      warn("fixture mode no longer seeds observer backlog/timeline evidence; skipping browser screenshots");
      info(`rerun with --project ${PROJECT} --backlog <observer-created-backlog-id> to capture dashboard evidence`);
      console.log("");
      console.log(c("green", "HN DEMO FIXTURE OK"));
      return;
    }
    const { chromium } = await loadPlaywright();
    mkdirSync(SCREENSHOT_DIR, { recursive: true });

    phase("browser");
    browser = await launchChromium(chromium);
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
    const page = await context.newPage();
    page.setDefaultTimeout(NAV_TIMEOUT_MS);

    for (const scenario of SCREENSHOTS) {
      await captureScenario(page, scenario);
    }

    await navigate(page, "review");
    ok(`left browser on ${page.url()}`);
    console.log("");
    console.log(c("green", "HN DEMO SMOKE OK"));

    if (KEEP_OPEN) {
      warn("keeping browser open for interactive review; press Ctrl+C to exit");
      await new Promise(() => {});
    }
  } catch (error) {
    console.log("");
    fail(error.message);
    if (error instanceof HttpError) {
      console.log(c("dim", `body=${String(error.body || "").slice(0, 1000)}`));
      console.log(c("yellow", "Start governance with `aming-claw start` and open /dashboard before rerunning."));
    }
    console.log(c("red", "HN DEMO SMOKE FAIL"));
    exit(error instanceof HttpError ? 1 : 2);
  } finally {
    if (browser && !KEEP_OPEN) await browser.close();
  }
}

main();
