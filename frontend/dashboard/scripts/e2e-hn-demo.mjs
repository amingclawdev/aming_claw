#!/usr/bin/env node
// hn-fear-demo-smoke
//
// Lightweight dashboard smoke and screenshot capture for the HN fear demo.
// It intentionally does not replay code workflows or call live AI providers.
//
//   node scripts/e2e-hn-demo.mjs --dashboard http://127.0.0.1:40000/dashboard --project aming-claw
//   node scripts/e2e-hn-demo.mjs --project aming-claw --headed --keep-open
//   node scripts/e2e-hn-demo.mjs --ensure-fixture --no-browser

import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { exit } from "node:process";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const REPO_ROOT = path.resolve(SCRIPT_DIR, "../../..");
const DEFAULT_BACKEND = "http://127.0.0.1:40000";
const DEFAULT_SCREENSHOT_DIR = path.join(REPO_ROOT, "docs", "hn-demo", "screenshots");
const DEFAULT_FIXTURE_PROJECT = "aming-claw-hn-demo";
const DEFAULT_FIXTURE_ROOT = path.join(os.tmpdir(), "ac-hn-demo-fixture");
const DEFAULT_BACKLOG_ID = "HN-FEAR-DEMO-SMOKE-SCREENSHOTS-20260526";

const FLAGS = parseFlags(process.argv.slice(2));
const BACKEND = trimTrailingSlash(FLAGS.backend || process.env.VITE_BACKEND_URL || DEFAULT_BACKEND);
const DASHBOARD = trimTrailingSlash(
  FLAGS.dashboard || process.env.DASHBOARD_URL || process.env.VITE_DASHBOARD_URL || `${BACKEND}/dashboard`,
);
const EXPLICIT_PROJECT = Boolean(FLAGS.project || process.env.VITE_PROJECT_ID);
let PROJECT = FLAGS.project || process.env.VITE_PROJECT_ID || DEFAULT_FIXTURE_PROJECT;
const FIXTURE_PROJECT = FLAGS["fixture-project"] || DEFAULT_FIXTURE_PROJECT;
const FIXTURE_ROOT = path.resolve(FLAGS["fixture-root"] || process.env.AMING_CLAW_HN_DEMO_FIXTURE_ROOT || DEFAULT_FIXTURE_ROOT);
const BACKLOG_ID = FLAGS.backlog || DEFAULT_BACKLOG_ID;
const SCREENSHOT_DIR = path.resolve(FLAGS.screenshots || DEFAULT_SCREENSHOT_DIR);
const HEADLESS = FLAGS.headed !== true;
const KEEP_OPEN = FLAGS["keep-open"] === true || FLAGS.interactive === true || FLAGS.headed === true;
const ENSURE_FIXTURE = FLAGS["ensure-fixture"] === true || (!EXPLICIT_PROJECT && FLAGS["no-fixture"] !== true);
if (ENSURE_FIXTURE) PROJECT = FIXTURE_PROJECT;
const RESET_FIXTURE = FLAGS["reset-fixture"] === true;
const NO_BROWSER = FLAGS["no-browser"] === true;
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
  const bool = new Set(["headed", "keep-open", "interactive", "ensure-fixture", "reset-fixture", "no-browser", "no-fixture"]);
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
