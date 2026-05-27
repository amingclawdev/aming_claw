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
const PROJECT = clean(FLAGS["project-id"] || `daily-planner-lite-drift-${RUN_ID}`).toLowerCase();
const FIXTURE_ROOT = path.resolve(FLAGS["fixture-root"] || path.join(os.tmpdir(), "ac-drift-demo", RUN_ID));
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
  if (!response.ok) throw new Error(`${method} ${route} -> ${response.status}: ${text.slice(0, 500)}`);
  return text ? JSON.parse(text) : null;
}

function write(relativePath, content) {
  const file = path.join(FIXTURE_ROOT, relativePath);
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, `${content.trim()}\n`, "utf8");
}

function git(args) {
  return execFileSync("git", args, { cwd: FIXTURE_ROOT, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
}

function materializeFixture() {
  assert(!FIXTURE_ROOT.startsWith(REPO_ROOT + path.sep), `refusing to create fixture inside repo: ${FIXTURE_ROOT}`);
  if (RESET && existsSync(FIXTURE_ROOT)) rmSync(FIXTURE_ROOT, { recursive: true, force: true });
  mkdirSync(FIXTURE_ROOT, { recursive: true });
  write("package.json", JSON.stringify({ name: "daily-planner-lite-drift", version: "0.0.0", private: true, type: "module", scripts: { test: "node tests/reminders.test.mjs" } }, null, 2));
  write("README.md", "# Daily Planner Lite Drift Fixture\n\nFixture for code/docs drift around reminders.");
  write("src/reminders.js", `export function createReminder(task, options = {}) {
  return {
    taskId: task.id,
    channel: "in-app",
    enabled: Boolean(options.enabled),
    leadMinutes: Number(options.leadMinutes || 10),
  };
}

export function describeReminder(reminder) {
  if (!reminder.enabled) return "Reminder off";
  return \`In-app reminder \${reminder.leadMinutes} minutes before task\`;
}`);
  write("src/app.js", `import { createReminder, describeReminder } from "./reminders.js";

export function taskSummary(task) {
  const reminder = createReminder(task, { enabled: Boolean(task.reminder) });
  return [task.title, describeReminder(reminder)].join(" - ");
}`);
  write("docs/reminders.md", `# Reminders

Daily Planner Lite supports in-app reminders only.

Email reminders are not supported. Reminder toggles default to off, and enabled reminders appear ten minutes before the task.`);
  write("tests/reminders.test.mjs", `import assert from "node:assert/strict";
import { createReminder, describeReminder } from "../src/reminders.js";

const reminder = createReminder({ id: "task-1" }, { enabled: true });
assert.equal(reminder.channel, "in-app");
assert.match(describeReminder(reminder), /In-app reminder/);
assert.equal(describeReminder(createReminder({ id: "task-2" })).includes("off"), true);
console.log("drift fixture ok");`);
  if (!existsSync(path.join(FIXTURE_ROOT, ".git"))) git(["init"]);
  git(["config", "user.email", "fixture@example.invalid"]);
  git(["config", "user.name", "Drift Fixture"]);
  git(["add", "."]);
  if (git(["status", "--porcelain"])) git(["commit", "-m", "baseline reminders docs"]);
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
    const source = await http("POST", `/api/graph-governance/${pid(PROJECT)}/query`, { tool: "find_node_by_path", args: { path: "src/reminders.js" }, actor: "drift_fixture", query_source: "observer", query_purpose: "gate_validation" });
    const doc = await http("POST", `/api/graph-governance/${pid(PROJECT)}/query`, { tool: "find_node_by_path", args: { path: "docs/reminders.md" }, actor: "drift_fixture", query_source: "observer", query_purpose: "gate_validation" });
    assert(Number(source.result?.count || 0) > 0, "graph cannot resolve src/reminders.js");
    const backlog = await http("GET", `/api/backlog/${pid(PROJECT)}`);
    const timeline = await http("GET", `/api/task/${pid(PROJECT)}/timeline`);
    assert(Number(backlog.count || backlog.bugs?.length || 0) === 0, "drift fixture must not seed backlog rows");
    assert(Number(timeline.count || 0) === 0, "drift fixture must not seed timeline events");
    console.log(JSON.stringify({ ok: true, project_id: PROJECT, fixture_root: FIXTURE_ROOT, baseline_commit: commit, snapshot_id: status.active_snapshot_id, doc_graph_count: Number(doc.result?.count || 0), trace_ids: [source.trace_id, doc.trace_id].filter(Boolean) }, null, 2));
  } catch (error) {
    console.error(error.message);
    exit(1);
  }
}

await main();
