#!/usr/bin/env node

import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import { exit } from "node:process";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(SCRIPT_DIR, "../../..");
const FLAGS = parseFlags(process.argv.slice(2));
const RUN_ID = clean(FLAGS["run-id"] || new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14));
const BACKEND = trim(FLAGS.backend || process.env.VITE_BACKEND_URL || "http://127.0.0.1:40000");
const PROJECT = clean(FLAGS["project-id"] || `daily-planner-lite-vibe-${RUN_ID}`).toLowerCase();
const FIXTURE_ROOT = path.resolve(FLAGS["fixture-root"] || path.join(os.tmpdir(), "ac-vibe-queue-demo", RUN_ID));
const PREVIEW_PORT = Number(FLAGS["preview-port"] || process.env.VIBE_QUEUE_PREVIEW_PORT || 4173);
const RESET = FLAGS.reset === true || FLAGS["reset-fixture"] === true;

function parseFlags(args) {
  const bool = new Set(["no-browser", "reset", "reset-fixture"]);
  const out = {};
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (!arg.startsWith("--")) continue;
    const key = arg.slice(2);
    if (bool.has(key)) out[key] = true;
    else {
      out[key] = args[i + 1];
      i++;
    }
  }
  return out;
}

function clean(value) {
  return String(value || "run").replace(/[^a-zA-Z0-9_-]/g, "-");
}

function trim(value) {
  return String(value || "").replace(/\/+$/, "");
}

function pid(value) {
  return encodeURIComponent(value);
}

function dashboardUrl(view = "backlog") {
  return `${BACKEND}/dashboard?project_id=${pid(PROJECT)}&view=${encodeURIComponent(view)}`;
}

function previewUrl() {
  return `http://127.0.0.1:${PREVIEW_PORT}/`;
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function http(method, route, body) {
  const headers = { Accept: "application/json" };
  const init = { method, headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(`${BACKEND}${route}`, init);
  const text = await response.text();
  let json = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    json = null;
  }
  if (!response.ok) throw new Error(`${method} ${route} -> ${response.status}: ${text.slice(0, 500)}`);
  return json;
}

function write(relativePath, content) {
  const file = path.join(FIXTURE_ROOT, relativePath);
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, `${content.trim()}\n`, "utf8");
}

function git(args, allowFail = false) {
  try {
    return execFileSync("git", args, { cwd: FIXTURE_ROOT, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
  } catch (error) {
    if (allowFail) return "";
    throw error;
  }
}

function materializeFixture() {
  assert(!FIXTURE_ROOT.startsWith(REPO_ROOT + path.sep), `refusing to create fixture inside repo: ${FIXTURE_ROOT}`);
  if (RESET && existsSync(FIXTURE_ROOT)) rmSync(FIXTURE_ROOT, { recursive: true, force: true });
  mkdirSync(FIXTURE_ROOT, { recursive: true });
  write("package.json", JSON.stringify({ name: "daily-planner-lite", version: "0.0.0", private: true, type: "module", scripts: { test: "node tests/planner.test.mjs" } }, null, 2));
  write("README.md", "# Daily Planner Lite\n\nSmall static planner fixture for Aming Claw Vibe Queue demo.");
  write("index.html", `<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Daily Planner Lite</title><link rel="stylesheet" href="./styles.css"></head>
<body>
  <main>
    <h1>Daily Planner Lite</h1>
    <form id="task-form"><input id="task-title" placeholder="Task"><input id="task-time" type="time"><button>Add</button></form>
    <ul id="task-list"></ul>
  </main>
  <script type="module" src="./src/app.js"></script>
</body>
</html>`);
  write("styles.css", "body { font-family: system-ui, sans-serif; margin: 2rem; } li { margin: 0.5rem 0; } .done { text-decoration: line-through; }");
  write("src/storage.js", `const KEY = "daily-planner-lite.tasks";

export function loadTasks(storage = globalThis.localStorage) {
  if (!storage) return [];
  return JSON.parse(storage.getItem(KEY) || "[]");
}

export function saveTasks(tasks, storage = globalThis.localStorage) {
  if (storage) storage.setItem(KEY, JSON.stringify(tasks));
  return tasks;
}`);
  write("src/reminders.js", `export function reminderState(task) {
  return { enabled: Boolean(task.reminder), label: task.reminder ? "Reminder on" : "No reminder" };
}`);
  write("src/app.js", `import { loadTasks, saveTasks } from "./storage.js";
import { reminderState } from "./reminders.js";

export function createTask(title, time = "") {
  return { id: String(Date.now()), title: title.trim(), time, done: false, reminder: false };
}

export function sortTasks(tasks) {
  return [...tasks].sort((a, b) => String(a.time || "99:99").localeCompare(String(b.time || "99:99")));
}

export function renderTask(task) {
  const reminder = reminderState(task);
  return [task.time, task.title, reminder.label].filter(Boolean).join(" - ");
}

export function addTask(tasks, task) {
  if (!task.title) return tasks;
  return sortTasks([...tasks, task]);
}

export function toggleDone(tasks, id) {
  return tasks.map((task) => task.id === id ? { ...task, done: !task.done } : task);
}

export function bindPlanner(documentRef = globalThis.document) {
  const form = documentRef?.querySelector("#task-form");
  const title = documentRef?.querySelector("#task-title");
  const time = documentRef?.querySelector("#task-time");
  const list = documentRef?.querySelector("#task-list");
  if (!form || !title || !time || !list) return;
  let tasks = loadTasks();
  const paint = () => { list.innerHTML = tasks.map((task) => \`<li>\${renderTask(task)}</li>\`).join(""); };
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    tasks = saveTasks(addTask(tasks, createTask(title.value, time.value)));
    title.value = "";
    paint();
  });
  paint();
}

bindPlanner();`);
  write("tests/planner.test.mjs", `import assert from "node:assert/strict";
import { addTask, createTask, renderTask, sortTasks } from "../src/app.js";
import { reminderState } from "../src/reminders.js";

const tasks = sortTasks([{ title: "Lunch", time: "12:00" }, { title: "Plan", time: "09:00" }]);
assert.equal(tasks[0].title, "Plan");
assert.equal(addTask([], createTask("Write notes", "10:00")).length, 1);
assert.equal(reminderState({ reminder: false }).label, "No reminder");
assert.match(renderTask({ title: "Plan", time: "09:00", reminder: false }), /Plan/);
console.log("daily planner fixture ok");`);
  if (!existsSync(path.join(FIXTURE_ROOT, ".git"))) git(["init"]);
  git(["config", "user.email", "fixture@example.invalid"]);
  git(["config", "user.name", "Daily Planner Fixture"]);
  git(["add", "."]);
  if (git(["status", "--porcelain"])) git(["commit", "-m", "baseline daily planner lite"]);
  return git(["rev-parse", "HEAD"]);
}

async function main() {
  try {
    const commit = materializeFixture();
    const bootstrap = await http("POST", "/api/project/bootstrap", {
      workspace_path: FIXTURE_ROOT,
      project_name: PROJECT,
      scan_depth: 4,
      exclude_patterns: ["node_modules", "dist", "build", "coverage", ".aming-claw/e2e-artifacts"],
      config_override: { project_id: PROJECT, graph: { exclude_paths: ["node_modules", "dist", "build", "coverage", ".aming-claw/e2e-artifacts"] } },
    });
    assert((bootstrap.project_id || PROJECT) === PROJECT, `bootstrap returned wrong project ${bootstrap.project_id}`);
    const status = await http("GET", `/api/graph-governance/${pid(PROJECT)}/status`);
    assert(status.active_snapshot_id, "active graph snapshot missing");
    const query = await http("POST", `/api/graph-governance/${pid(PROJECT)}/query`, {
      tool: "find_node_by_path",
      args: { path: "src/app.js" },
      actor: "vibe_queue_fixture",
      query_source: "observer",
      query_purpose: "gate_validation",
    });
    assert(Number(query.result?.count || 0) > 0, "graph cannot resolve src/app.js");
    const backlog = await http("GET", `/api/backlog/${pid(PROJECT)}`);
    const timeline = await http("GET", `/api/task/${pid(PROJECT)}/timeline`);
    assert(Number(backlog.count || backlog.bugs?.length || 0) === 0, "vibe fixture must not seed backlog rows");
    assert(Number(timeline.count || 0) === 0, "vibe fixture must not seed timeline events");
    console.log(JSON.stringify({
      ok: true,
      project_id: PROJECT,
      fixture_root: FIXTURE_ROOT,
      baseline_commit: commit,
      snapshot_id: status.active_snapshot_id,
      trace_id: query.trace_id || "",
      two_window_setup: {
        default: "Use Codex's in-app browser for the Aming Claw dashboard. Open the planner preview in your normal browser.",
        codex_page: "Open Aming Claw Dashboard",
        external_page: "Open Daily Planner Preview",
      },
      dashboard_url: dashboardUrl("backlog"),
      dashboard_links: {
        backlog: dashboardUrl("backlog"),
        timeline: dashboardUrl("timeline"),
        prompt_queue: dashboardUrl("backlog"),
        graph: dashboardUrl("graph"),
        operations: dashboardUrl("operations"),
        review: dashboardUrl("review"),
      },
      planner_preview_url: previewUrl(),
      planner_preview_command: `python3 -m http.server ${PREVIEW_PORT} --directory ${FIXTURE_ROOT}`,
    }, null, 2));
  } catch (error) {
    console.error(error.message);
    exit(1);
  }
}

await main();
