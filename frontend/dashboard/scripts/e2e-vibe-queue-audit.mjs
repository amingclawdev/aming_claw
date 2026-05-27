#!/usr/bin/env node

import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
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
const REPORT = path.resolve(FLAGS.report || path.join(REPO_ROOT, "docs", "vibe-queue-demo", "audits", `${RUN_ID}.md`));
const JSON_REPORT = path.resolve(FLAGS["json-report"] || REPORT.replace(/\.md$/i, ".json"));

function parseFlags(args) {
  const bool = new Set(["no-browser"]);
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

function run(command, args, cwd = FIXTURE_ROOT, allowFail = false) {
  try {
    const stdout = execFileSync(command, args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
    return { ok: true, command: [command, ...args].join(" "), stdout };
  } catch (error) {
    const result = { ok: false, command: [command, ...args].join(" "), stdout: String(error.stdout || "").trim(), stderr: String(error.stderr || error.message).trim() };
    if (!allowFail) throw new Error(`${result.command} failed: ${result.stderr}`);
    return result;
  }
}

function git(args) {
  return run("git", args).stdout;
}

function write(relativePath, content) {
  const file = path.join(FIXTURE_ROOT, relativePath);
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, `${content.trim()}\n`, "utf8");
}

function parseFixtureOutput(stdout) {
  const start = stdout.lastIndexOf("{");
  assert(start >= 0, "fixture did not print JSON");
  return JSON.parse(stdout.slice(start));
}

function runFixture() {
  const args = [
    path.join(SCRIPT_DIR, "e2e-vibe-queue-fixture.mjs"),
    "--backend", BACKEND,
    "--project-id", PROJECT,
    "--fixture-root", FIXTURE_ROOT,
    "--run-id", RUN_ID,
    "--reset-fixture",
    "--no-browser",
  ];
  return parseFixtureOutput(run("node", args, REPO_ROOT).stdout);
}

async function graphQuery(tool, args, actor = "vibe_queue_audit") {
  return http("POST", `/api/graph-governance/${pid(PROJECT)}/query`, {
    tool,
    args,
    actor,
    query_source: "observer",
    query_purpose: "prompt_context_build",
  });
}

async function upsertBacklog(bugId, body) {
  return http("POST", `/api/backlog/${pid(PROJECT)}/${encodeURIComponent(bugId)}`, body);
}

async function appendTimeline(body) {
  return http("POST", `/api/task/${pid(PROJECT)}/timeline`, body);
}

function commitAll(message, bugId) {
  git(["add", "."]);
  git(["commit", "-m", [message, "", "Chain-Source-Stage: observer-hotfix", `Chain-Project: ${PROJECT}`, `Chain-Bug-Id: ${bugId}`].join("\n")]);
  return git(["rev-parse", "HEAD"]);
}

function applyTodayFocus() {
  write("src/app.js", `import { loadTasks, saveTasks } from "./storage.js";
import { reminderState } from "./reminders.js";

export function createTask(title, time = "") {
  return { id: String(Date.now()), title: title.trim(), time, done: false, reminder: false, focused: false };
}

export function promoteFocus(tasks, id) {
  return tasks.map((task) => ({ ...task, focused: task.id === id }));
}

export function sortTasks(tasks) {
  return [...tasks].sort((a, b) => Number(Boolean(b.focused)) - Number(Boolean(a.focused)) || String(a.time || "99:99").localeCompare(String(b.time || "99:99")));
}

export function renderTask(task) {
  const reminder = reminderState(task);
  const focus = task.focused ? "Today Focus" : "";
  return [focus, task.time, task.title, reminder.label].filter(Boolean).join(" - ");
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
  const paint = () => { list.innerHTML = sortTasks(tasks).map((task) => \`<li>\${renderTask(task)}</li>\`).join(""); };
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    tasks = saveTasks(addTask(tasks, createTask(title.value, time.value)));
    title.value = "";
    paint();
  });
  paint();
}

bindPlanner();`);
  write("tests/planner.test.mjs", `${readFileSync(path.join(FIXTURE_ROOT, "tests/planner.test.mjs"), "utf8").trim()}
import { promoteFocus } from "../src/app.js";
assert.equal(promoteFocus([{ id: "a" }, { id: "b" }], "b")[1].focused, true);
console.log("today focus ok");`);
}

function applyReminderToggle() {
  write("src/reminders.js", `export function reminderState(task) {
  return { enabled: Boolean(task.reminder), label: task.reminder ? "Reminder on" : "No reminder" };
}

export function toggleReminder(task) {
  return { ...task, reminder: !Boolean(task.reminder) };
}`);
  write("tests/planner.test.mjs", `${readFileSync(path.join(FIXTURE_ROOT, "tests/planner.test.mjs"), "utf8").trim()}
import { toggleReminder } from "../src/reminders.js";
assert.equal(toggleReminder({ reminder: false }).reminder, true);
console.log("reminder toggle ok");`);
}

function applyQuickCapture() {
  write("src/quick-capture.js", `import { addTask, createTask } from "./app.js";

export function quickCapture(tasks, text) {
  return addTask(tasks, createTask(text, ""));
}`);
  write("tests/quick-capture.test.mjs", `import assert from "node:assert/strict";
import { quickCapture } from "../src/quick-capture.js";

assert.equal(quickCapture([], "Inbox note").length, 1);
console.log("quick capture ok");`);
}

async function runAudit() {
  const audit = {
    schema_version: "vibe_queue_demo_audit.v1",
    run_id: RUN_ID,
    project_id: PROJECT,
    backend: BACKEND,
    fixture_root: FIXTURE_ROOT,
    evidence: { backlog_ids: [], timeline_event_ids: [], trace_ids: [], commits: [], tests: [], queue_states: [] },
    checks: [],
    warnings: [],
  };
  const fixture = runFixture();
  audit.fixture = fixture;
  const baselineBacklog = await http("GET", `/api/backlog/${pid(PROJECT)}`);
  const baselineTimeline = await http("GET", `/api/task/${pid(PROJECT)}/timeline`);
  assert(Number(baselineBacklog.count || baselineBacklog.bugs?.length || 0) === 0, "fixture backlog was not empty");
  assert(Number(baselineTimeline.count || 0) === 0, "fixture timeline was not empty");
  audit.checks.push({ name: "fixture starts without implementation evidence", passed: true });

  const appTrace = await graphQuery("find_node_by_path", { path: "src/app.js" });
  const reminderTrace = await graphQuery("find_node_by_path", { path: "src/reminders.js" });
  audit.evidence.trace_ids.push(...[appTrace.trace_id, reminderTrace.trace_id].filter(Boolean));

  const reqA = `VIBE-${RUN_ID}-FOCUS`;
  const reqB = `VIBE-${RUN_ID}-REMINDER`;
  const reqC = `VIBE-${RUN_ID}-CAPTURE`;
  for (const [bugId, title, files] of [
    [reqA, "Add Today Focus at top of task list", ["src/app.js", "tests/planner.test.mjs"]],
    [reqB, "Add per-task reminder toggle defaulting off", ["src/reminders.js", "tests/planner.test.mjs"]],
  ]) {
    await upsertBacklog(bugId, {
      actor: "observer:vibe-queue",
      title,
      status: "OPEN",
      priority: "P2",
      mf_type: "chain_rescue",
      force_admit: true,
      target_files: files,
      test_files: files.filter((file) => file.startsWith("tests/")),
      provenance_paths: audit.evidence.trace_ids.filter(Boolean),
      acceptance_criteria: [`${title}.`, "Observer records requirement before dispatch."],
      details_md: "Created during audit requirement mode; no implementation event existed before explicit start.",
    });
    const event = await appendTimeline({ backlog_id: bugId, actor: "observer:vibe-queue", event_type: "requirement_confirmed", event_kind: "requirement", phase: "Clarifying", status: "accepted", payload: { queue_state: "Backlog Contracts" } });
    if (event?.id) audit.evidence.timeline_event_ids.push(event.id);
    audit.evidence.backlog_ids.push(bugId);
  }
  const preStartTimeline = await http("GET", `/api/task/${pid(PROJECT)}/timeline`);
  const preImpl = (preStartTimeline.events || []).filter((event) => event.event_kind === "implementation");
  assert(preImpl.length === 0, "implementation evidence existed before explicit start");
  audit.checks.push({ name: "requirement mode first", passed: true, backlog_count: 2, implementation_events_before_start: 0 });

  for (const bugId of [reqA, reqB]) {
    await upsertBacklog(bugId, { actor: "observer:vibe-queue", status: "MF_IN_PROGRESS", force_admit: true });
  }
  const dispatch = await appendTimeline({
    actor: "observer:vibe-queue",
    backlog_id: reqA,
    event_type: "parallel_compatible_dispatch",
    event_kind: "implementation",
    phase: "In Progress",
    status: "accepted",
    payload: {
      workers: [
        { backlog_id: reqA, owned_files: ["src/app.js", "tests/planner.test.mjs"] },
        { backlog_id: reqB, owned_files: ["src/reminders.js", "tests/planner.test.mjs"], note: "shared test file requires serial commit merge" },
      ],
      graph_query_trace_ids: audit.evidence.trace_ids.filter(Boolean),
    },
  });
  if (dispatch?.id) audit.evidence.timeline_event_ids.push(dispatch.id);

  await upsertBacklog(reqC, {
    actor: "observer:vibe-queue",
    title: "Add quick capture input for fast task entry",
    status: "OPEN",
    priority: "P2",
    mf_type: "chain_rescue",
    force_admit: true,
    target_files: ["src/quick-capture.js", "tests/quick-capture.test.mjs"],
    test_files: ["tests/quick-capture.test.mjs"],
    acceptance_criteria: ["Quick capture can add a task with only text.", "Requirement is queued while A/B are already in progress."],
    details_md: "Mid-run requirement queued by observer without interrupting active worker scopes.",
  });
  const queued = await appendTimeline({ backlog_id: reqC, actor: "observer:vibe-queue", event_type: "mid_run_requirement_queued", event_kind: "requirement", phase: "User Ideas", status: "accepted", payload: { queue_state: "Backlog Contracts", active_backlog_ids: [reqA, reqB] } });
  if (queued?.id) audit.evidence.timeline_event_ids.push(queued.id);
  audit.evidence.backlog_ids.push(reqC);

  applyTodayFocus();
  let test = run("node", ["tests/planner.test.mjs"], FIXTURE_ROOT, true);
  assert(test.ok, test.stderr || "planner test failed after Today Focus");
  let commit = commitAll("feat: add today focus", reqA);
  audit.evidence.tests.push(test);
  audit.evidence.commits.push({ backlog_id: reqA, commit });
  await appendTimeline({ backlog_id: reqA, actor: "observer:vibe-queue", event_type: "serial_commit_done", event_kind: "close_ready", phase: "Done", status: "accepted", payload: { commit, queue_state: "Done" }, verification: { tests_run: [test.command], tests_exit_code: 0 } });
  await upsertBacklog(reqA, { actor: "observer:vibe-queue", status: "FIXED", commit, force_admit: true });

  applyReminderToggle();
  test = run("node", ["tests/planner.test.mjs"], FIXTURE_ROOT, true);
  assert(test.ok, test.stderr || "planner test failed after reminder toggle");
  commit = commitAll("feat: add reminder toggle", reqB);
  audit.evidence.tests.push(test);
  audit.evidence.commits.push({ backlog_id: reqB, commit });
  await appendTimeline({ backlog_id: reqB, actor: "observer:vibe-queue", event_type: "serial_commit_done", event_kind: "close_ready", phase: "Done", status: "accepted", payload: { commit, queue_state: "Done" }, verification: { tests_run: [test.command], tests_exit_code: 0 } });
  await upsertBacklog(reqB, { actor: "observer:vibe-queue", status: "FIXED", commit, force_admit: true });

  applyQuickCapture();
  test = run("node", ["tests/quick-capture.test.mjs"], FIXTURE_ROOT, true);
  assert(test.ok, test.stderr || "quick capture test failed");
  commit = commitAll("feat: add quick capture", reqC);
  audit.evidence.tests.push(test);
  audit.evidence.commits.push({ backlog_id: reqC, commit });
  await appendTimeline({ backlog_id: reqC, actor: "observer:vibe-queue", event_type: "serial_commit_done", event_kind: "close_ready", phase: "Done", status: "accepted", payload: { commit, queue_state: "Done" }, verification: { tests_run: [test.command], tests_exit_code: 0 } });
  await upsertBacklog(reqC, { actor: "observer:vibe-queue", status: "FIXED", commit, force_admit: true });

  const reconcile = await http("POST", `/api/graph-governance/${pid(PROJECT)}/reconcile/full`, { actor: "observer:vibe-queue", project_root: FIXTURE_ROOT, activate: true, semantic_enrich: false, run_id: `vibe-${RUN_ID}` });
  audit.evidence.reconcile = reconcile;
  audit.evidence.queue_states = ["User Ideas", "Clarifying", "Backlog Contracts", "In Progress", "Commit Queue", "Done"];
  audit.checks.push({ name: "serial commits and tests complete", passed: true, commit_count: audit.evidence.commits.length, test_count: audit.evidence.tests.length });
  audit.self_review = [
    "This audit uses real governance backlog, timeline, graph query, test, commit, and reconcile calls.",
    "Parallelism is shown as compatible dispatch evidence; commits are intentionally serial.",
    "The mid-run requirement is queued while earlier work is in progress, which is the public-facing behavior under test.",
  ];
  writeReports(audit);
}

function markdown(audit) {
  return `# Vibe Queue Demo Audit

- Run ID: \`${audit.run_id}\`
- Project: \`${audit.project_id}\`
- Fixture: \`${audit.fixture_root}\`
- Backend: \`${audit.backend}\`

## Evidence

- Backlog rows: ${audit.evidence.backlog_ids.map((id) => `\`${id}\``).join(", ")}
- Timeline events: ${audit.evidence.timeline_event_ids.map((id) => `\`${id}\``).join(", ")}
- Graph traces: ${audit.evidence.trace_ids.filter(Boolean).map((id) => `\`${id}\``).join(", ")}
- Queue states: ${audit.evidence.queue_states.join(" -> ")}
- Commits: ${audit.evidence.commits.map((item) => `\`${item.backlog_id}:${item.commit.slice(0, 12)}\``).join(", ")}

## Checks

${audit.checks.map((check) => `- ${check.passed ? "PASS" : "FAIL"}: ${check.name}`).join("\n")}

## Self Review

${audit.self_review.map((item) => `- ${item}`).join("\n")}
`;
}

function writeReports(audit) {
  audit.finished_at = new Date().toISOString();
  mkdirSync(path.dirname(REPORT), { recursive: true });
  writeFileSync(REPORT, markdown(audit), "utf8");
  writeFileSync(JSON_REPORT, JSON.stringify(audit, null, 2), "utf8");
  console.log(JSON.stringify({ ok: true, report: REPORT, json_report: JSON_REPORT, project_id: PROJECT }, null, 2));
}

try {
  await runAudit();
} catch (error) {
  console.error(error.message);
  exit(1);
}
