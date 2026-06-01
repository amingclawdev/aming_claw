#!/usr/bin/env node
// dashboard-projects-e2e
//
// Read-only by default. Verifies that the dashboard project console can work
// against an isolated example project without mutating the main aming-claw graph.
//
//   node scripts/e2e-projects.mjs
//   node scripts/e2e-projects.mjs --project dashboard-e2e-demo
//   node scripts/e2e-projects.mjs --apply   # bootstrap/build missing example graph

import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { exit } from "node:process";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(SCRIPT_DIR, "../../..");
const DEFAULT_PROJECT = "dashboard-e2e-demo";
const DEFAULT_PARENT = "aming-claw";
const DEFAULT_WORKSPACE = path.join(REPO_ROOT, "examples", DEFAULT_PROJECT);

const FLAGS = parseFlags(process.argv.slice(2));
const BACKEND = FLAGS.backend || process.env.VITE_BACKEND_URL || "http://localhost:40000";
const PROJECT = FLAGS.project || process.env.VITE_PROJECT_ID || DEFAULT_PROJECT;
const PARENT_PROJECT = FLAGS.parent || DEFAULT_PARENT;
const WORKSPACE = path.resolve(FLAGS.workspace || DEFAULT_WORKSPACE);
const APPLY = FLAGS.apply === true;
const SKIP_PARENT = FLAGS["skip-parent-isolation"] === true;
const ONLY = String(FLAGS.only || "").trim();
const HTTP_RETRIES = Number(FLAGS["http-retries"] || process.env.DASHBOARD_E2E_HTTP_RETRIES || 3);

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
const phase = (text) => console.log(`\n${c("cyan", "phase")} ${c("bold", text)}`);
const ok = (text) => console.log(`  ${c("green", "ok")} ${text}`);
const warn = (text) => console.log(`  ${c("yellow", "warn")} ${text}`);
const fail = (text) => console.log(`  ${c("red", "fail")} ${text}`);
const info = (text) => console.log(`  ${c("dim", text)}`);

function parseFlags(args) {
  const bool = new Set(["apply", "skip-parent-isolation"]);
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
  constructor(method, url, status, body, request) {
    super(`${method} ${url} -> ${status}`);
    this.method = method;
    this.url = url;
    this.status = status;
    this.body = body;
    this.request = request;
  }
}

async function http(method, route, body) {
  const init = { method, headers: { Accept: "application/json" } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  for (let attempt = 0; attempt <= HTTP_RETRIES; attempt++) {
    try {
      response = await fetch(`${BACKEND}${route}`, init);
      break;
    } catch (error) {
      if (attempt >= HTTP_RETRIES) {
        throw new HttpError(method, route, 0, String(error), body);
      }
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
  if (!response.ok) throw new HttpError(method, route, response.status, text, body);
  return json;
}

function pid(projectId) {
  return encodeURIComponent(projectId);
}

function snapshotPath(projectId, snapshotId, suffix) {
  return `/api/graph-governance/${pid(projectId)}/snapshots/${encodeURIComponent(snapshotId)}${suffix}`;
}

function activePath(projectId, suffix) {
  return `/api/graph-governance/${pid(projectId)}/snapshots/active${suffix}`;
}

function shortCommit(commit) {
  if (!commit) return "-";
  return commit.length > 10 ? commit.slice(0, 7) : commit;
}

function allNodePaths(node) {
  return [
    ...(node.primary_files || []),
    ...(node.secondary_files || []),
    ...(node.test_files || []),
    ...(node.metadata?.config_files || []),
  ].map((item) => String(item).replaceAll("\\", "/"));
}

function relativeWorkspace() {
  return path.relative(REPO_ROOT, WORKSPACE).replaceAll("\\", "/");
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function extractFunctionBlock(source, name) {
  const fnIndex = source.indexOf(`function ${name}`);
  assert(fnIndex >= 0, `${name} function is missing`);
  const paramsStart = source.indexOf("(", fnIndex);
  assert(paramsStart >= 0, `${name} function parameters are missing`);
  let paramsDepth = 0;
  let paramsEnd = -1;
  for (let index = paramsStart; index < source.length; index++) {
    const char = source[index];
    if (char === "(") paramsDepth++;
    if (char === ")") {
      paramsDepth--;
      if (paramsDepth === 0) {
        paramsEnd = index;
        break;
      }
    }
  }
  assert(paramsEnd >= 0, `${name} function parameters are unterminated`);
  const start = source.indexOf("{", paramsEnd);
  assert(start >= 0, `${name} function body is missing`);
  let depth = 0;
  for (let index = start; index < source.length; index++) {
    const char = source[index];
    if (char === "{") depth++;
    if (char === "}") {
      depth--;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`${name} function body is unterminated`);
}

function visibleJsxText(source) {
  const fragments = [];
  for (const match of source.matchAll(/>\s*([^<>{}][^<>{}]*)\s*</g)) {
    const text = match[1].replace(/\s+/g, " ").trim();
    if (text) fragments.push(text);
  }
  for (const match of source.matchAll(/\b(?:placeholder|aria-label)=["']([^"']+)["']/g)) {
    const text = match[1].replace(/\s+/g, " ").trim();
    if (text) fragments.push(text);
  }
  return fragments.join("\n");
}

function assertNoOperatorTerms(label, text) {
  const normalized = text.toLowerCase();
  const forbidden = [
    "worker controls",
    "execution queue",
    "audit",
    "command count",
    "command counts",
    "backlog",
    "commit",
  ];
  const found = forbidden.filter((term) => normalized.includes(term));
  assert(
    found.length === 0,
    `${label} default visible copy uses operator term(s): ${found.join(", ")}`,
  );
}

function verifySimpleModeRequestFirstDesktopContract() {
  phase("simple mode request-first desktop contract");
  const viewSource = readFileSync(
    path.join(REPO_ROOT, "frontend/dashboard/src/views/ProjectInboxView.tsx"),
    "utf8",
  );
  const cssSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/styles.css"), "utf8");
  const rootBlock = extractFunctionBlock(viewSource, "ProjectInboxView");
  const overviewBlock = extractFunctionBlock(viewSource, "RequestFirstOverview");
  const beforeBlock = extractFunctionBlock(viewSource, "BeforeDevelopmentTab");
  const rawCardBlock = extractFunctionBlock(viewSource, "RawRequirementCard");

  const requestFirstIndex = rootBlock.indexOf("<RequestFirstOverview");
  const observerIndex = rootBlock.indexOf("project-inbox-observer");
  const statsIndex = rootBlock.indexOf("project-inbox-stats");
  const tabsIndex = rootBlock.indexOf("simple-mode-tabs");
  assert(requestFirstIndex >= 0, "Simple Mode should render the request-first overview");
  assert(observerIndex >= 0, "Simple Mode observer/status strip is missing");
  assert(statsIndex >= 0, "Simple Mode request summary counters are missing");
  assert(tabsIndex >= 0, "Simple Mode tabs are missing");
  assert(
    requestFirstIndex < observerIndex && requestFirstIndex < statsIndex && requestFirstIndex < tabsIndex,
    "Desktop Simple Mode must render request cards before observer telemetry, counters, and tabs",
  );

  const cardsIndex = overviewBlock.indexOf("simple-request-card");
  const originalTextIndex = overviewBlock.indexOf("You asked");
  const nextLineIndex = overviewBlock.indexOf("simple-request-next");
  assert(cardsIndex >= 0, "Request-first overview should render request cards");
  assert(originalTextIndex >= 0, "Request cards must label the original user request");
  assert(nextLineIndex >= 0, "Request cards must show a next-action or waiting line");

  const captureIndex = beforeBlock.indexOf("project-inbox-capture");
  assert(captureIndex >= 0, "Before-development panel should keep a request capture affordance");

  const desktopCssIndex = cssSource.indexOf(".simple-shell .project-inbox-view");
  const requestFirstCssIndex = cssSource.indexOf(".simple-request-first");
  assert(desktopCssIndex >= 0, "Simple Mode desktop shell CSS is missing");
  assert(requestFirstCssIndex >= 0, "Request-first card CSS is missing");

  const defaultVisibleCopy = [
    visibleJsxText(rootBlock),
    visibleJsxText(overviewBlock),
    visibleJsxText(beforeBlock),
    visibleJsxText(rawCardBlock),
  ].join("\n");
  assertNoOperatorTerms("Simple Mode desktop first viewport", defaultVisibleCopy);
  ok("request cards are source-ordered before telemetry, counters, and tabs for desktop");
  ok("default first-viewport copy avoids operator terms");
}

function verifyOrdinaryUserEntryContract() {
  phase("temporary engineer-homepage ordinary user entry desktop contract");
  const appSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/App.tsx"), "utf8");
  const consoleSource = readFileSync(
    path.join(REPO_ROOT, "frontend/dashboard/src/views/ProjectConsoleView.tsx"),
    "utf8",
  );
  const treeSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/components/TreePanel.tsx"), "utf8");
  const cssSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/styles.css"), "utf8");
  const readLocationStart = appSource.indexOf("function readDashboardLocation");
  const readLocationEnd = appSource.indexOf("function writeStoredProjectId", readLocationStart);
  assert(readLocationStart >= 0 && readLocationEnd > readLocationStart, "readDashboardLocation source block is missing");
  const readLocationBlock = appSource.slice(readLocationStart, readLocationEnd);
  const consoleBlock = extractFunctionBlock(consoleSource, "ProjectConsoleView");
  const rowBlock = extractFunctionBlock(consoleSource, "ProjectRow");

  assert(appSource.includes('DASHBOARD_MODE_PARAM = "mode"'), "App should define a simple mode query parameter");
  assert(appSource.includes('DASHBOARD_SIMPLE_PARAM = "simple"'), "App should define a simple boolean query parameter");
  assert(
    /return\s*\{\s*projectId:\s*DEFAULT_PROJECT_ID,\s*view:\s*["']overview["']/.test(readLocationBlock),
    "Server-side/default dashboard state should prefer the temporary Engineer overview",
  );
  assert(
    readLocationBlock.includes("if (viewParam)") && readLocationBlock.includes("view = normalizeViewName(viewParam)"),
    "Explicit dashboard view params should be preserved",
  );
  assert(
    readLocationBlock.includes("shouldOpenSimpleMode(params)") && readLocationBlock.includes('view = "inbox"'),
    "Legacy Simple Mode query params should still land in the inbox",
  );
  assert(
    readLocationBlock.includes('view = "overview"'),
    "Dashboard URLs without an explicit view should land in the temporary Engineer overview",
  );
  assert(
    appSource.includes("function shouldOpenSimpleMode") &&
      appSource.includes('modeParam === "simple"') &&
      appSource.includes('simpleParam === "1"'),
    "Simple Mode should remain explicitly available via mode=simple or simple=1",
  );
  assert(!readLocationBlock.includes("|| true"), "Temporary homepage routing must not rely on an always-true condition");
  assert(
    !/projectParam\s*\?\s*["']inbox["']\s*:\s*["']projects["']/.test(readLocationBlock),
    "Project-scoped URLs must not be a hidden Simple Mode entry",
  );
  assert(
    appSource.includes("url.searchParams.delete(DASHBOARD_MODE_PARAM)") &&
      appSource.includes("url.searchParams.delete(DASHBOARD_SIMPLE_PARAM)"),
    "Canonical dashboard navigation should strip legacy simple entry params after routing",
  );
  assert(
    appSource.includes('data-testid="simple-shell-engineer-panel"'),
    "Simple Mode should expose a stable engineer-panel escape action",
  );
  assert(
    appSource.includes('writeDashboardLocation(currentProjectId, "overview", "push")'),
    "Engineer-panel escape should enter the dashboard overview without changing projects",
  );
  assert(appSource.includes('setView("overview")'), "Engineer-panel escape should update current view state");
  assert(
    appSource.includes("Engineer panel"),
    "Engineer-panel escape should be visible as a secondary action in Simple Mode",
  );
  assert(treeSource.includes('label="Project Inbox"'), "Engineer sidebar should keep a route back to Simple Mode");

  const entryIndex = consoleBlock.indexOf("ordinary-entry-panel");
  const statsIndex = consoleBlock.indexOf("project-console-score-grid");
  const tableIndex = consoleBlock.indexOf("Project Registry");
  assert(entryIndex >= 0, "Projects page should render an ordinary-user entry panel");
  assert(statsIndex >= 0, "Projects page summary counters are missing");
  assert(tableIndex >= 0, "Projects page registry table is missing");
  assert(
    entryIndex < statsIndex && entryIndex < tableIndex,
    "Ordinary-user entry should appear before project counters and the registry table",
  );
  assert(consoleBlock.includes('data-testid="ordinary-user-open-requests"'), "Entry panel primary action needs a stable test id");
  assert(consoleBlock.includes("Start from a request"), "Entry panel should start from a user's request");
  assert(consoleBlock.includes("Open requests"), "Entry panel should expose Open requests");
  assert(
    consoleBlock.includes("onOpenProject(simpleEntryProject.project_id)"),
    "Entry panel should open the selected request workspace",
  );
  assert(rowBlock.includes("Open requests"), "Project rows should expose Open requests");
  assert(
    rowBlock.includes("Open the request workspace for this project"),
    "Project row request action should be distinct from engineer actions",
  );

  assert(cssSource.includes(".ordinary-entry-panel"), "Ordinary-user entry panel CSS is missing");
  assert(cssSource.includes(".ordinary-entry-primary"), "Ordinary-user entry primary action CSS is missing");

  const panelStart = consoleSource.indexOf('<section className="ordinary-entry-panel"');
  const panelEnd = panelStart >= 0 ? consoleSource.indexOf("</section>", panelStart) : -1;
  assert(panelStart >= 0 && panelEnd > panelStart, "Ordinary-user entry panel source block is missing");
  const entryCopy = visibleJsxText(consoleSource.slice(panelStart, panelEnd));
  const forbidden = ["graph", "backlog", "worker", "execution queue", "audit", "commit", "snapshot"];
  const found = forbidden.filter((term) => entryCopy.toLowerCase().includes(term));
  assert(found.length === 0, `Ordinary-user entry copy uses operator term(s): ${found.join(", ")}`);
  ok("/dashboard and project-scoped dashboard URLs default to the temporary Engineer overview");
  ok("explicit Simple Mode query params still enter the inbox");
  ok("Simple Mode exposes a secondary engineer-panel escape");
  ok("Projects still exposes an ordinary-user request entry when users enter the engineer panel");
  ok("ordinary-user entry copy avoids operator terms");
}

async function ensureProjectRegistered() {
  phase("project registry");
  const projects = await http("GET", "/api/projects");
  assert(Array.isArray(projects.projects), "/api/projects did not return projects[]");
  const project = projects.projects.find((row) => row.project_id === PROJECT);
  if (!project) {
    if (!APPLY) {
      throw new Error(
        `Project ${PROJECT} is not registered. Re-run with --apply to bootstrap ${WORKSPACE}.`,
      );
    }
    return bootstrapProject();
  }
  ok(`${PROJECT} registered`);
  info(`workspace=${project.workspace_path || "(empty)"} snapshot=${project.active_snapshot_id || "-"}`);
  return project;
}

async function bootstrapProject() {
  phase("bootstrap project (--apply)");
  assert(existsSync(WORKSPACE), `workspace does not exist: ${WORKSPACE}`);
  const result = await http("POST", "/api/project/bootstrap", {
    workspace_path: WORKSPACE,
    project_name: PROJECT,
    scan_depth: 3,
  });
  ok(`bootstrapped ${result.project_id || PROJECT} snapshot=${result.snapshot_id || "-"}`);
  return {
    project_id: result.project_id || PROJECT,
    workspace_path: WORKSPACE,
    active_snapshot_id: result.snapshot_id,
  };
}

async function verifyProjectConfig() {
  phase("project config");
  const [config, aiConfig, refs] = await Promise.all([
    http("GET", `/api/projects/${pid(PROJECT)}/config`),
    http("GET", `/api/projects/${pid(PROJECT)}/ai-config`),
    http("GET", `/api/projects/${pid(PROJECT)}/git-refs`),
  ]);
  assert(config.project_id === PROJECT, `config project_id mismatch: ${config.project_id}`);
  const language = String(config.language || "").toLowerCase();
  assert(
    language.includes("type") || language === "mixed",
    `expected typescript or mixed project config, got ${config.language || "(empty)"}`,
  );
  const excludes = [
    ...(config.graph?.exclude_paths || []),
    ...(config.graph?.ignore_globs || []),
    ...(config.graph?.effective_exclude_roots || []),
  ].join(" ");
  assert(excludes.includes("node_modules"), "project config should exclude node_modules");
  assert(aiConfig.project_id === PROJECT, "ai-config project_id mismatch");
  ok(`config loaded language=${config.language}`);
  ok(`ai semantic route=${aiConfig.semantic?.provider || "-"} / ${aiConfig.semantic?.model || "-"}`);
  ok(`git refs loaded repo=${Boolean(refs.is_git_repo)} ref=${refs.selected_ref || refs.current_branch || "-"}`);
  return { config, aiConfig, refs };
}

async function verifyGraphRuntime(project) {
  phase("graph runtime");
  let status = null;
  try {
    status = await http("GET", `/api/graph-governance/${pid(PROJECT)}/status`);
  } catch (error) {
    if (!APPLY) throw error;
    warn(`status missing (${error.message}); bootstrapping project again`);
    await bootstrapProject();
    status = await http("GET", `/api/graph-governance/${pid(PROJECT)}/status`);
  }
  if (!status.active_snapshot_id && APPLY) {
    await buildFullGraph();
    status = await http("GET", `/api/graph-governance/${pid(PROJECT)}/status`);
  }
  assert(status.active_snapshot_id, "active_snapshot_id is missing");
  const summary = await http("GET", activePath(PROJECT, "/summary"));
  const ops = await http("GET", `/api/graph-governance/${pid(PROJECT)}/operations/queue`);
  const nodes = await http("GET", snapshotPath(PROJECT, status.active_snapshot_id, "/nodes?include_semantic=true&limit=1000"));
  const edges = await http("GET", snapshotPath(PROJECT, status.active_snapshot_id, "/edges?limit=4000"));

  assert((summary.counts?.features || summary.health?.semantic_health?.feature_count || 0) > 0, "summary has no features");
  verifySummaryHealthTaxonomy(summary);
  assert(Array.isArray(nodes.nodes) && nodes.nodes.length > 0, "nodes[] is empty");
  assert(Array.isArray(edges.edges), "edges[] missing");

  const rel = relativeWorkspace();
  const inspectable = nodes.nodes.find((node) => {
    const paths = allNodePaths(node);
    const isL7 = node.layer === "L7";
    const hasPrimary = (node.primary_files || []).length > 0;
    const hasFunctions = Number(node.metadata?.function_count || 0) > 0;
    const staysRelativeToExample =
      paths.every((item) => !item.startsWith("..")) &&
      paths.every((item) => !item.includes(`${rel}/`));
    return isL7 && hasPrimary && hasFunctions && staysRelativeToExample;
  });
  assert(inspectable, "no inspectable L7 node with functions found in example graph");

  const stale = status.current_state?.graph_stale;
  ok(`snapshot=${status.active_snapshot_id} commit=${shortCommit(status.graph_snapshot_commit)}`);
  ok(`counts nodes=${summary.counts?.nodes ?? nodes.count} features=${summary.counts?.features ?? "-"}`);
  ok(`inspectable node=${inspectable.node_id} ${inspectable.title}`);
  ok(`operations queue count=${ops.count}`);
  if (stale?.is_stale) {
    warn(`example graph stale: ${shortCommit(stale.active_graph_commit)} -> ${shortCommit(stale.head_commit)}`);
  } else {
    ok("example graph is current for its workspace");
  }
  return { status, summary, ops, nodes, edges, inspectable, project };
}

function verifySummaryHealthTaxonomy(summary) {
  const h = summary.health || {};
  const project = Number(h.project_health_score);
  const structure = Number(h.structure_health_score);
  const semantic = Number(h.semantic_health_score);
  assert(Number.isFinite(project), "summary health project_health_score is missing");
  if (Number.isFinite(structure) && Number.isFinite(semantic) && Math.abs(structure - semantic) > 0.01) {
    assert(
      Math.abs(project - structure) <= 0.01,
      `project_health_score should prefer structure health (${structure}) over semantic health (${semantic}); got ${project}`,
    );
  }
  ok(`project health taxonomy project=${project} structure=${Number.isFinite(structure) ? structure : "-"} semantic=${Number.isFinite(semantic) ? semantic : "-"}`);
}

async function buildFullGraph() {
  phase("build graph (--apply)");
  const result = await http("POST", `/api/graph-governance/${pid(PROJECT)}/reconcile/full`, {
    run_id: `dashboard-projects-e2e-full-${Date.now()}`,
    actor: "dashboard_e2e",
    activate: true,
    semantic_enrich: true,
    semantic_use_ai: false,
    enqueue_stale: false,
    semantic_skip_completed: true,
    notes_extra: { source: "dashboard_projects_e2e", action: "build_graph" },
  });
  ok(`full reconcile snapshot=${result.snapshot_id || result.activation?.snapshot_id || "-"}`);
  return result;
}

async function verifyParentIsolation() {
  if (SKIP_PARENT) {
    warn("parent isolation skipped");
    return;
  }
  phase("parent graph isolation");
  const rootConfigPath = path.join(REPO_ROOT, ".aming-claw.yaml");
  assert(existsSync(rootConfigPath), `root .aming-claw.yaml missing: ${rootConfigPath}`);
  const rootConfig = readFileSync(rootConfigPath, "utf8");
  assert(rootConfig.includes("examples"), "root .aming-claw.yaml should exclude examples");
  ok("root config excludes examples");

  const parentStatus = await http("GET", `/api/graph-governance/${pid(PARENT_PROJECT)}/status`);
  assert(parentStatus.active_snapshot_id, `${PARENT_PROJECT} active snapshot missing`);
  const parentNodes = await http(
    "GET",
    snapshotPath(PARENT_PROJECT, parentStatus.active_snapshot_id, "/nodes?include_semantic=false&limit=3000"),
  );
  const rel = relativeWorkspace();
  const hits = (parentNodes.nodes || []).filter((node) =>
    allNodePaths(node).some((item) => item.includes(rel)),
  );
  assert(hits.length === 0, `${PARENT_PROJECT} graph contains ${hits.length} ${PROJECT} path(s)`);
  ok(`${PARENT_PROJECT} active graph has 0 ${PROJECT} nodes`);
}

async function verifyProjectSwitchContract(runtime) {
  phase("project switch contract");
  const parentStatus = await http("GET", `/api/graph-governance/${pid(PARENT_PROJECT)}/status`);
  assert(parentStatus.active_snapshot_id, `${PARENT_PROJECT} active snapshot missing`);
  assert(parentStatus.project_id === PARENT_PROJECT, "parent status project_id mismatch");
  assert(runtime.status.project_id === PROJECT, "target status project_id mismatch");
  assert(parentStatus.active_snapshot_id !== runtime.status.active_snapshot_id, "project snapshots should be distinct");
  ok(`${PARENT_PROJECT} snapshot=${parentStatus.active_snapshot_id}`);
  ok(`${PROJECT} snapshot=${runtime.status.active_snapshot_id}`);
}

function verifyProjectImportUiContract() {
  phase("project import UI contract");
  const viewSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/views/ProjectConsoleView.tsx"), "utf8");
  const apiSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/lib/api.ts"), "utf8");
  const serverSource = readFileSync(path.join(REPO_ROOT, "agent/governance/server.py"), "utf8");
  assert(viewSource.includes('data-testid="project-import-directory"'), "Projects page import directory button is missing");
  assert(viewSource.includes("handleChooseDirectory"), "Projects page does not wire a directory picker handler");
  assert(viewSource.includes("Directory picker unavailable. Paste the path manually."), "Projects page should gracefully fall back to manual path entry");
  assert(viewSource.includes("AbortController"), "Projects page directory picker must client-timeout instead of hanging");
  assert(viewSource.includes("actionState?.key === \"bootstrap\""), "Bootstrap button should remain usable while directory picker is trying");
  assert(viewSource.includes('data-testid="project-import-exclude-paths"'), "Projects bootstrap form should expose exclude path review");
  assert(viewSource.includes('data-testid="project-import-exclude-confirm"'), "Projects bootstrap form should require exclude path confirmation");
  assert(viewSource.includes("parseBootstrapExcludePaths"), "Projects bootstrap should normalize exclude path input before submit");
  assert(viewSource.includes("config_override: { graph: { exclude_paths: excludePaths } }"), "Projects bootstrap should send reviewed excludes into project config");
  assert(apiSource.includes("exclude_patterns?: string[]"), "dashboard API client missing bootstrap exclude pattern contract");
  assert(apiSource.includes("/api/local/choose-directory"), "dashboard API client missing directory picker endpoint");
  assert(apiSource.includes("timeout_seconds?: number"), "dashboard API client missing directory picker timeout contract");
  assert(serverSource.includes("_open_local_directory_picker_windows"), "backend missing Windows directory picker fallback");
  assert(serverSource.includes("directory picker timed out; paste the path manually"), "backend picker fallback should timeout into manual entry");
  ok("Projects page exposes import directory picker contract");
}

function verifyProjectProgressContract() {
  phase("project bootstrap progress contract");
  const viewSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/views/ProjectConsoleView.tsx"), "utf8");
  const apiSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/lib/api.ts"), "utf8");
  const serviceSource = readFileSync(path.join(REPO_ROOT, "agent/governance/project_service.py"), "utf8");
  assert(apiSource.includes("bootstrap_progress?: ProjectOperationProgress"), "project registry type should expose bootstrap_progress");
  assert(serviceSource.includes("update_project_operation_progress"), "backend should persist project operation progress");
  assert(serviceSource.includes('"full_reconcile"'), "bootstrap progress should expose the full_reconcile phase");
  assert(viewSource.includes("project-console-progress"), "Projects page should display a visible long-operation progress strip");
  assert(viewSource.includes("polling registry status"), "Projects page should poll registry status while long graph operations run");
  assert(viewSource.includes("elapsedLabel"), "Projects page should display elapsed time for long operations");
  ok("Projects page exposes pollable bootstrap/build graph progress");
}

function verifyHeaderV1Contract() {
  phase("header v1 contract");
  const appSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/App.tsx"), "utf8");
  const headerSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/components/Header.tsx"), "utf8");
  assert(!appSource.includes("onOpenReview={() => setActionPanelOpen(true)}"), "Header should not expose global Action launcher in v1");
  assert(!headerSource.includes(">Action<"), "Header component should not render the global Action launcher in v1");
  assert(!headerSource.includes("onOpenReview"), "Header component should not accept the global Action launcher prop in v1");
  ok("global Action launcher is hidden for v1");
}

function verifySummaryHealthSourceContract() {
  phase("summary health taxonomy contract");
  const storeSource = readFileSync(path.join(REPO_ROOT, "agent/governance/graph_snapshot_store.py"), "utf8");
  const scoreBlock = storeSource.indexOf("project_score = (");
  const structureFallback = storeSource.indexOf('else structure.get("score")', scoreBlock);
  const semanticFallback = storeSource.indexOf('else semantic.get("score")', scoreBlock);
  assert(scoreBlock >= 0, "summary health project_score block is missing");
  assert(structureFallback > scoreBlock, "project health should include structure fallback");
  assert(semanticFallback > structureFallback, "project health should prefer structure before semantic fallback");
  ok("project_health_score falls back legacy -> structure -> semantic");
}

function verifyProjectDisplayNameContract() {
  phase("project display-name contract");
  const headerSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/components/Header.tsx"), "utf8");
  const viewSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/views/ProjectConsoleView.tsx"), "utf8");
  const projectServiceSource = readFileSync(path.join(REPO_ROOT, "agent/governance/project_service.py"), "utf8");
  assert(headerSource.includes("activeProjectLabel"), "Header should derive active project display name");
  assert(headerSource.includes("`${project.name.trim()} · ${project.project_id}`"), "Project selector should show display name plus project_id when they differ");
  assert(viewSource.includes("currentProjectLabel"), "Projects view subtitle should prefer display name for current project");
  assert(viewSource.includes("projectLabelFor(projects, currentProjectId)"), "Projects view should derive current label from registry");
  assert(projectServiceSource.includes('"name",'), "Project metadata update allow-list should include name");
  assert(projectServiceSource.includes('projects["projects"][pid]["name"] = project_name.strip()'), "Bootstrap should update display name for existing project imports");
  ok("display names are primary, project_id stays visible as technical id");
}

function verifyProjectScopedFetchContract() {
  phase("project-scoped fetch contract");
  const appSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/App.tsx"), "utf8");
  const apiSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/lib/api.ts"), "utf8");
  assert(appSource.includes('const DASHBOARD_PROJECT_ID_PARAM = "project_id"'), "Dashboard should expose canonical project_id URL parameter");
  assert(appSource.includes('const DASHBOARD_LEGACY_PROJECT_PARAM = "project"'), "Dashboard should retain legacy project URL fallback");
  assert(appSource.includes("projectIdParam?.trim() ? projectIdParam : legacyProjectParam"), "Dashboard should prefer explicit project_id over legacy project param");
  assert(appSource.includes("url.searchParams.set(DASHBOARD_PROJECT_ID_PARAM"), "Dashboard should write canonical project_id URLs");
  assert(appSource.includes("url.searchParams.delete(DASHBOARD_LEGACY_PROJECT_PARAM)"), "Dashboard should remove stale legacy project URL param");
  assert(appSource.includes("const requestProjectId = currentProjectId"), "fetchAll should capture the active project_id for one request cycle");
  assert(appSource.includes("setAiConfig(null)"), "Project switch should clear stale AI config before the next project load");
  for (const token of ["statusFor(requestProjectId", "activeSummaryFor(requestProjectId", "activeProjectionFor(requestProjectId", "operationsQueueFor(requestProjectId", "backlogFor(requestProjectId", "nodesFor(requestProjectId", "edgesFor(requestProjectId", "feedbackQueueFor(requestProjectId"]) {
    assert(appSource.includes(token), `App fetchAll should use explicit project API: ${token}`);
  }
  for (const fn of ["activeProjectionFor(projectId", "nodesFor(projectId", "edgesFor(projectId", "feedbackQueueFor(projectId"]) {
    assert(apiSource.includes(fn), `API client missing explicit project method ${fn}`);
  }
  ok("dashboard data fetches are explicit per active project");
}

function verifyProjectContextFallbackContract() {
  phase("project context fallback contract");
  const appSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/App.tsx"), "utf8");
  const viewSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/views/ProjectConsoleView.tsx"), "utf8");
  const mcpSource = readFileSync(path.join(REPO_ROOT, "agent/mcp/server.py"), "utf8");
  const seedSource = readFileSync(path.join(REPO_ROOT, "agent/mcp/resources/seed-graph-summary.json"), "utf8");
  assert(appSource.includes("DASHBOARD_WORKSPACE_PARAM"), "Dashboard URL should accept workspace prefill for bootstrap fallback");
  assert(appSource.includes("shouldFallbackToProjects"), "Dashboard should fallback to Projects for missing/unbuilt graphs");
  assert(appSource.includes("Open Projects to bootstrap or build graph"), "Unknown project should guide the operator to bootstrap");
  assert(appSource.includes("Graph is not ready for ${requestProjectId}"), "Missing graph should guide the operator to build graph");
  assert(viewSource.includes("initialWorkspacePath"), "Projects bootstrap form should accept URL/workspace prefill");
  assert(mcpSource.includes("default_project_id"), "MCP current-context should expose the configured default project id");
  assert(mcpSource.includes("workspace_project_id"), "MCP current-context should expose workspace-resolved project id");
  assert(mcpSource.includes("dashboard_project_id"), "MCP current-context should expose dashboard/resource-selected project id");
  assert(mcpSource.includes("active_project_id"), "MCP current-context should expose the resolved active project id");
  assert(mcpSource.includes("_project_id_from_workspace_registry"), "MCP current-context should resolve project from registered workspace paths");
  assert(seedSource.includes("aming-claw://project/<id>/context"), "Seed guidance should direct visible dashboard projects to project-scoped context");
  ok("dashboard and MCP distinguish default, workspace, dashboard, and active project context");
}

function verifyProjectGraphActionsGuideContract() {
  phase("project graph actions guide contract");
  const viewSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/views/ProjectConsoleView.tsx"), "utf8");
  assert(viewSource.includes("project-console-guide"), "Projects page should show concise graph action guidance");
  assert(viewSource.includes("<strong>Build graph</strong>"), "Projects guide should explain Build graph");
  assert(viewSource.includes("<strong>Update graph</strong>"), "Projects guide should explain Update graph");
  assert(viewSource.includes("semantic_use_ai: false"), "Project graph actions must not make live AI calls by default");
  assert(viewSource.includes("title=\"Run full graph reconcile without AI enrichment\""), "Build graph action should have explicit tooltip");
  assert(viewSource.includes("title=\"Run scope reconcile without AI enrichment\""), "Update graph action should have explicit tooltip");
  ok("Projects page exposes clear build/update graph actions");
}

function verifyAiConfigProjectScopeContract() {
  phase("AI config project-scope contract");
  const appSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/App.tsx"), "utf8");
  const actionSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/components/ActionControlPanel.tsx"), "utf8");
  const serverSource = readFileSync(path.join(REPO_ROOT, "agent/governance/server.py"), "utf8");
  assert(appSource.includes("Project scope"), "AI config modal should show project scope");
  assert(appSource.includes("semanticAiReadiness"), "App should compute semantic AI readiness before batch enrich");
  assert(appSource.includes("project_config?.ai?.routing?.semantic"), "AI readiness should require explicit project semantic routing");
  assert(appSource.includes("aiConfig={aiConfig}"), "Action modal should receive active project AI config");
  assert(actionSource.includes("AI enrich blocked: configure this project's semantic provider/model"), "Action modal should block unconfigured live AI");
  assert(actionSource.includes("tool.status !== \"detected\""), "Action modal should block missing local CLI tools");
  assert(serverSource.includes("update_project_ai_routing_metadata"), "AI config save should write Aming-claw project registry metadata");
  assert(!serverSource.includes("update_project_ai_routing(root, routing, project_id=project_id)"), "AI config save must not create/update the governed project's local config by default");
  assert(serverSource.includes("tool_health"), "Backend ai-config response should expose tool health");
  assert(serverSource.includes("AI_MODEL_CATALOG"), "Backend ai-config response should expose model catalog");
  ok("AI config shows local CLI requirements and blocks unconfigured live enrich");
}

function verifyQueueLaneCopyContract() {
  phase("semantic queue lane copy contract");
  const opsSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/views/OperationsQueueView.tsx"), "utf8");
  assert(opsSource.includes("semantic lanes"), "Operations Queue KPI should describe semantic lanes, not executor workers");
  assert(opsSource.includes("governance semantic lanes"), "Running section should explain governance-owned semantic lanes");
  ok("queue wording matches parallel semantic worker behavior");
}

function verifyTreeLayerFilterContract() {
  phase("tree layer filter contract");
  const treeSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/components/TreePanel.tsx"), "utf8");
  assert(treeSource.includes('type LayerFilter = Layer | "ALL"'), "Tree layer filter should be single-select, not a toggle set");
  assert(treeSource.includes("setLayerFilter(l)"), "Layer chip click should select only the clicked layer");
  assert(treeSource.includes("layerFilter === l"), "Layer chip active state should match the selected layer");
  assert(treeSource.includes("LAYER_LABELS"), "Layer chips should carry semantic labels/tooltips");
  assert(treeSource.includes("layer mode intentionally returns only that semantic layer"), "Layer filtering should not mix ancestor layers into the result");
  ok("tree layer chips are semantic single-select filters");
}

function verifyVisualCollaborationPanelsContract() {
  phase("visual collaboration panels contract");
  const appSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/App.tsx"), "utf8");
  const treeSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/components/TreePanel.tsx"), "utf8");
  const graphSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/views/GraphView.tsx"), "utf8");
  const focusSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/components/FocusCard.tsx"), "utf8");
  const cssSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/styles.css"), "utf8");
  assert(appSource.includes("DASHBOARD_SIDEBAR_COLLAPSED_STORAGE_KEY"), "App should persist the left sidebar collapsed preference");
  assert(appSource.includes("collapsed={sidebarCollapsed}"), "App should pass collapse state to TreePanel");
  assert(treeSource.includes("aria-label={collapsed ? \"Expand navigation\" : \"Collapse navigation\"}"), "TreePanel collapse button needs an accessible label");
  assert(treeSource.includes("sidebar${collapsed ? \" collapsed\" : \"\"}"), "TreePanel should apply collapsed sidebar class");
  assert(graphSource.includes("FOCUS_CARD_MINIMIZED_STORAGE_KEY"), "GraphView should persist FocusCard minimized preference");
  assert(graphSource.includes("minimized={focusCardMinimized}"), "GraphView should pass minimized state to FocusCard");
  assert(focusSource.includes("MinimizedFocusCard"), "FocusCard should expose a compact minimized rendering");
  assert(focusSource.includes("aria-label=\"Restore focus card\""), "Minimized FocusCard should have an accessible restore affordance");
  assert(cssSource.includes(".sidebar.collapsed"), "CSS should reserve a compact collapsed sidebar rail");
  assert(cssSource.includes(".focus-card.focus-card-minimized"), "CSS should style the minimized FocusCard");
  ok("graph workspace panels can collapse/minimize for shared-screen collaboration");
}

function verifyEditorJumpWorkspaceContract() {
  phase("editor jump workspace contract");
  const appSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/App.tsx"), "utf8");
  const editorSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/lib/editor.ts"), "utf8");
  const fileLinkSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/components/FileLink.tsx"), "utf8");
  const inspectorSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/components/InspectorDrawer.tsx"), "utf8");
  assert(appSource.includes("activeWorkspaceRoot"), "App does not derive the active project workspace root");
  assert(appSource.includes("workspaceRoot={activeWorkspaceRoot}"), "App does not pass workspace root into the inspector");
  assert(editorSource.includes("rootOverride"), "editorUrl does not accept a workspace root override");
  assert(fileLinkSource.includes("workspaceRoot?: string"), "FileLink cannot receive an active project workspace root");
  assert(inspectorSource.includes("workspaceRoot={workspaceRoot}"), "Inspector does not propagate workspace root to file/function links");
  ok("editor jump resolves through active project workspace contract");
}

function verifyAssetRelationGraphOpsContract() {
  phase("asset relation graph operation contract");
  const appSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/App.tsx"), "utf8");
  const treeSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/components/TreePanel.tsx"), "utf8");
  const assetSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/views/AssetInboxView.tsx"), "utf8");
  const cssSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/styles.css"), "utf8");
  assert(
    /<AssetInboxView[\s\S]*onSelectNode=\{handleSelectNodeFromAsset\}[\s\S]*workspaceRoot=\{activeWorkspaceRoot\}/.test(appSource),
    "App should pass asset-aware graph navigation and active workspace root into AssetInboxView",
  );
  assert(assetSource.includes('import FileLink from "../components/FileLink"'), "Asset inbox should reuse FileLink");
  assert(
    assetSource.includes("<FileLink path={props.item.path} workspaceRoot={props.workspaceRoot} />"),
    "Asset path should use FileLink with the active workspace root",
  );
  assert(assetSource.includes("function TargetNodeButton"), "Asset relation targets should have a graph-jump target button");
  assert(assetSource.includes('className="target-link asset-target-link"'), "Target node jump should reuse target-link visual language");
  assert(treeSource.includes("Asset tree"), "Asset Inbox should render ASSET TREE in the sidebar tree slot");
  assert(treeSource.includes("ASSET_STATUS_FILTERS"), "Asset tree should expose All/Health/Candidate/Drift/Orphan status filters");
  assert(treeSource.includes("buildAssetTree"), "Asset tree should build group-scoped asset leaves");
  assert(treeSource.includes("assetLeafForItem"), "Asset tree should show asset file leaves under groups");
  assert(!assetSource.includes('className="asset-selector-panel"'), "Asset Inbox main view should not duplicate the bulky selector panel");
  assert(assetSource.includes("function AssetInspector"), "Asset inspector should expose overview/relations/candidates/actions surfaces");
  assert(assetSource.includes("ASSET_INSPECTOR_TABS"), "Asset inspector tabs should be explicit and stable");
  assert(assetSource.includes("function RelationPanel"), "Trusted bindings should render in the Relations tab");
  assert(assetSource.includes("function CandidateRelationGroup"), "Candidate bindings should render in the Candidates tab");
  assert(assetSource.includes("function AssetActionFlow"), "Actions tab should explain binding and drift flows");
  assert(assetSource.includes("Queue Observer review"), "Actions tab should steer drift changes through Observer review");
  assert(assetSource.includes("function RemoveBindingDialog"), "Remove binding should require confirmation and reason capture");
  assert(assetSource.includes("It enters Review Queue"), "Remove confirmation should warn about review/commit/apply gating");
  assert(assetSource.includes("Selected relation operation result"), "Selected relation action result should be visible from graph surface");
  assert(assetSource.includes("primaryRelationAction"), "Asset relation graph should choose add/remove actions by relation status");
  assert(assetSource.includes("Queue add for review"), "Candidate relation should expose review-gated add");
  assert(assetSource.includes("Propose remove"), "Accepted/impact/stale relation should expose Propose remove");
  assert(
    assetSource.includes("HN-ASSET-REMOVE-BINDING-RUNTIME-DRIFT-20260525"),
    "Remove-binding runtime drift should surface the linked follow-up backlog id",
  );
  assert(!assetSource.includes("editorUrl("), "AssetInboxView should not create a new editor URI helper");
  assert(cssSource.includes(".asset-action-flow-block"), "CSS should style the action flow surface");
  assert(cssSource.includes(".asset-selected-relation-op"), "CSS should keep selected relation operation result readable");
  ok("asset relation inspector exposes navigation, FileLink, review-gated operations, and known drift follow-up");
}

function verifyBacklogEvidenceContract() {
  phase("backlog evidence contract");
  const apiSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/lib/api.ts"), "utf8");
  const viewSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/views/BacklogView.tsx"), "utf8");
  const typeSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/types.ts"), "utf8");
  const cssSource = readFileSync(path.join(REPO_ROOT, "frontend/dashboard/src/styles.css"), "utf8");
  const serverSource = readFileSync(path.join(REPO_ROOT, "agent/governance/server.py"), "utf8");
  assert(apiSource.includes("backlogTimelineGateFor"), "Backlog API client should fetch per-row timeline gate evidence");
  assert(apiSource.includes("/timeline-gate?"), "Backlog API client should call the timeline-gate endpoint");
  assert(viewSource.includes("ContractGatePanel"), "Backlog detail should render a dedicated Contract & Gate tab panel");
  assert(viewSource.includes("NoGateNotice"), "Backlog gate UI should render explicit no-gate/not-applicable state");
  assert(viewSource.includes("buildTimelineLanes"), "Backlog row expansion should group execution events into one-hop lanes");
  assert(viewSource.includes("One-hop agent lanes"), "Backlog lane grid should be accessible as one-hop agent lanes");
  assert(viewSource.includes("BacklogDetailModal"), "Backlog rows should open a detail modal");
  assert(viewSource.includes("BACKLOG_URL_PARAM"), "Backlog detail modal should be URL addressable");
  assert(viewSource.includes("buildTimelineDag"), "Backlog detail should derive a timeline DAG");
  assert(viewSource.includes("buildTimelineLaneContext"), "Backlog timeline should derive readable actor-family lane context");
  assert(viewSource.includes("Subagents / Workers ·"), "Parallel worker DAG sublanes should stay under the Subagents / Workers family");
  assert(viewSource.includes("workers parallel"), "Backlog timeline should make parallel worker execution visible");
  assert(viewSource.includes("rawLaneKeyForEvent"), "Backlog timeline should keep raw lane ids inspectable without using them as labels");
  assert(viewSource.includes("ImplementationStepGrid"), "Timeline tab should group implementation evidence into audit steps");
  assert(viewSource.includes("ArtifactPills"), "Timeline and inspector should surface concrete artifacts");
  assert(viewSource.includes("EvidenceInspector"), "Backlog DAG nodes should open an evidence inspector");
  assert(viewSource.includes("RouteEvidenceCards"), "Evidence inspector should render route-context evidence cards");
  assert(viewSource.includes("Observer alert received"), "Evidence inspector should surface observer alert acknowledgement");
  assert(viewSource.includes("Expert review"), "Evidence inspector should surface expert review evidence");
  assert(viewSource.includes("Test route"), "Evidence inspector should surface selected test-route evidence");
  assert(viewSource.includes("Final drift prompt"), "Evidence inspector should surface the final drift prompt");
  assert(viewSource.includes("CONTENT_SYS_DEMO_VISUALIZATION_SCHEMA"), "Evidence inspector should recognize content-sys demo visualization schema");
  assert(viewSource.includes("contentSysDemoVisualizationEvidence"), "Evidence inspector should extract content-sys visualization payloads from timeline evidence");
  assert(viewSource.includes("Content-sys demo visualization"), "Evidence inspector should surface content-sys visualization evidence");
  assert(viewSource.includes("Docker demo status"), "Evidence inspector should surface Docker fixture status cards");
  assert(viewSource.includes("Docker demo timeline"), "Evidence inspector should surface Docker fixture timeline events");
  assert(viewSource.includes("Docker artifact refs"), "Evidence inspector should surface Docker artifact refs");
  assert(viewSource.includes("Privacy boundary"), "Evidence inspector should surface privacy boundary truth");
  assert(viewSource.includes("Frontend display contract"), "Evidence inspector should surface frontend display contract");
  assert(viewSource.includes("Inspect raw timeline payloads"), "Raw timeline payloads should remain inspectable without being the primary evidence surface");
  assert(viewSource.includes("relatedIdsFromBug"), "Backlog detail should discover related backlog ids");
  assert(viewSource.includes("BACKLOG_PARALLEL_TIMELINE_FIXTURE_EVENTS"), "Backlog detail should include a deterministic parallel-lane fixture");
  assert(viewSource.includes("contract_missing_visualization"), "Backlog fixture should model missing contract evidence");
  assert(viewSource.includes("no_false_evidence_gate"), "Backlog fixture should assert missing evidence is never rendered as passed");
  assert(viewSource.includes("missing.has(requirement.id)"), "Missing contract requirements must render as missing, not passed");
  assert(viewSource.includes("coarse/inferred"), "Coarse or inferred timeline/contract evidence should be visibly labeled");
  assert(viewSource.includes("contract {contract.template_id"), "Backlog compact rows should scan contract metadata");
  assert(apiSource.includes("backlogBugFor"), "Backlog API client should fetch full row detail for the modal");
  assert(typeSource.includes("BacklogTimelineGateResponse"), "Dashboard types should model timeline gate response");
  assert(typeSource.includes("BacklogContractSummary"), "Dashboard types should model compact backlog contract summary");
  assert(typeSource.includes("chain_trigger_json"), "Dashboard types should expose full backlog contract JSON for related-id discovery");
  assert(cssSource.includes(".backlog-gate-grid"), "Backlog gate UI should have stable layout CSS");
  assert(cssSource.includes(".backlog-lane-grid"), "Backlog lane UI should have stable layout CSS");
  assert(cssSource.includes(".backlog-modal-tabs"), "Backlog modal tabs should have stable responsive CSS");
  assert(cssSource.includes(".backlog-contract-requirement"), "Contract evidence mapping should have stable CSS");
  assert(cssSource.includes(".backlog-no-gate-state"), "No-gate state should have explicit CSS");
  assert(cssSource.includes(".backlog-modal"), "Backlog modal should have stable layout CSS");
  assert(cssSource.includes(".backlog-dag-node.status-missing"), "Backlog DAG should visibly distinguish missing evidence");
  assert(cssSource.includes(".backlog-evidence-inspector"), "Backlog evidence inspector should have stable layout CSS");
  assert(cssSource.includes(".backlog-route-evidence-cards"), "Route-context evidence cards should have stable layout CSS");
  assert(cssSource.includes(".backlog-inspector-raw"), "Raw inspector payloads should have stable disclosure CSS");
  assert(serverSource.includes("contract_summary"), "Compact backlog API should expose contract summary metadata");
  ok("backlog evidence row exposes timeline gate, contract, modal DAG, and inspector");
}

async function main() {
  console.log(c("bold", "dashboard-projects-e2e"));
  console.log(c("dim", `backend=${BACKEND} project=${PROJECT} workspace=${WORKSPACE} apply=${APPLY}`));

  try {
    if (ONLY) {
      if (ONLY === "simple-mode-request-first-desktop") {
        verifySimpleModeRequestFirstDesktopContract();
      } else if (
        ONLY === "ordinary-user-entry-desktop" ||
        ONLY === "engineer-homepage-entry-desktop" ||
        ONLY === "simple-first-entry-desktop" ||
        ONLY === "simple-first-entry-engineer-escape-desktop"
      ) {
        verifyOrdinaryUserEntryContract();
      } else {
        throw new Error(`unknown --only target: ${ONLY}`);
      }
      console.log("");
      console.log(c("green", "ACCEPTANCE OK"));
      return;
    }
    await http("GET", "/api/health");
    verifySimpleModeRequestFirstDesktopContract();
    verifyOrdinaryUserEntryContract();
    verifyProjectImportUiContract();
    verifyProjectProgressContract();
    verifyHeaderV1Contract();
    verifySummaryHealthSourceContract();
    verifyProjectDisplayNameContract();
    verifyProjectScopedFetchContract();
    verifyProjectContextFallbackContract();
    verifyProjectGraphActionsGuideContract();
    verifyAiConfigProjectScopeContract();
    verifyQueueLaneCopyContract();
    verifyTreeLayerFilterContract();
    verifyVisualCollaborationPanelsContract();
    verifyEditorJumpWorkspaceContract();
    verifyAssetRelationGraphOpsContract();
    verifyBacklogEvidenceContract();
    const project = await ensureProjectRegistered();
    await verifyProjectConfig();
    const runtime = await verifyGraphRuntime(project);
    await verifyParentIsolation();
    await verifyProjectSwitchContract(runtime);
    console.log("");
    console.log(c("green", "ACCEPTANCE OK"));
  } catch (error) {
    console.log("");
    fail(error.message);
    if (error instanceof HttpError) {
      console.log(c("dim", `body=${String(error.body || "").slice(0, 1000)}`));
      if (!APPLY) {
        console.log(c("yellow", "This script is read-only by default. Use --apply only for isolated example bootstrap/build."));
      }
    }
    console.log(c("red", "ACCEPTANCE FAIL"));
    exit(error instanceof HttpError ? 1 : 2);
  }
}

main();
