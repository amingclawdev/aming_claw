#!/usr/bin/env node

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
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
const REPORT = path.resolve(FLAGS.report || path.join(REPO_ROOT, "docs", "drift-demo", "audits", `${RUN_ID}.md`));
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
  return parseFixtureOutput(run("node", [
    path.join(SCRIPT_DIR, "e2e-drift-demo-fixture.mjs"),
    "--backend", BACKEND,
    "--project-id", PROJECT,
    "--fixture-root", FIXTURE_ROOT,
    "--run-id", RUN_ID,
    "--reset-fixture",
    "--no-browser",
  ], REPO_ROOT).stdout);
}

async function graphQuery(tool, args, actor = "drift_demo_audit") {
  return http("POST", `/api/graph-governance/${pid(PROJECT)}/query`, { tool, args, actor, query_source: "observer", query_purpose: "prompt_context_build" });
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

function changeReminderBehaviorWithoutDocs() {
  write("src/reminders.js", `export function createReminder(task, options = {}) {
  const channel = options.email ? "email" : "in-app";
  return {
    taskId: task.id,
    channel,
    enabled: Boolean(options.enabled),
    leadMinutes: Number(options.leadMinutes || 10),
    email: options.email || "",
  };
}

export function describeReminder(reminder) {
  if (!reminder.enabled) return "Reminder off";
  if (reminder.channel === "email") return \`Email reminder to \${reminder.email}\`;
  return \`In-app reminder \${reminder.leadMinutes} minutes before task\`;
}`);
  write("tests/reminders.test.mjs", `import assert from "node:assert/strict";
import { createReminder, describeReminder } from "../src/reminders.js";

const reminder = createReminder({ id: "task-1" }, { enabled: true });
assert.equal(reminder.channel, "in-app");
const email = createReminder({ id: "task-2" }, { enabled: true, email: "me@example.invalid" });
assert.equal(email.channel, "email");
assert.match(describeReminder(email), /Email reminder/);
console.log("drift fixture ok");`);
}

function updateDocsForEmailReminder() {
  write("docs/reminders.md", `# Reminders

Daily Planner Lite supports in-app reminders and optional email reminders.

Reminder toggles default to off. Enabled in-app reminders appear ten minutes before the task. Email reminders can send to a configured email address when one is supplied.`);
}

function driftSignalFrom(status, ops, feedback) {
  const signals = [];
  const stale = status.current_state?.graph_stale || status.graph_stale || {};
  if (stale.is_stale) signals.push({ source: "graph_stale", status: "impact_pending", detail: stale });
  const drift = status.current_state?.semantic_drift || status.semantic_drift || {};
  if (drift.has_drift || Number(drift.node_stale || 0) > 0) signals.push({ source: "semantic_drift", status: "suspected", detail: drift });
  for (const op of ops.operations || []) {
    const text = JSON.stringify(op).toLowerCase();
    if (text.includes("drift") || text.includes("stale") || text.includes("impact")) signals.push({ source: "operations_queue", status: op.status || "suspected", detail: op });
  }
  const feedbackText = JSON.stringify(feedback || {}).toLowerCase();
  if (feedbackText.includes("docs/reminders.md") || feedbackText.includes("drift")) signals.push({ source: "feedback_queue", status: "suspected", detail: feedback });
  return signals;
}

async function runAudit() {
  const audit = {
    schema_version: "drift_demo_audit.v1",
    run_id: RUN_ID,
    project_id: PROJECT,
    backend: BACKEND,
    fixture_root: FIXTURE_ROOT,
    evidence: { backlog_ids: [], timeline_event_ids: [], trace_ids: [], commits: [], tests: [] },
    checks: [],
    warnings: [],
  };
  audit.fixture = runFixture();
  const bugId = `DRIFT-${RUN_ID}-REMINDERS`;
  const sourceTrace = await graphQuery("find_node_by_path", { path: "src/reminders.js" });
  const docTrace = await graphQuery("find_node_by_path", { path: "docs/reminders.md" });
  audit.evidence.trace_ids.push(...[sourceTrace.trace_id, docTrace.trace_id].filter(Boolean));
  await upsertBacklog(bugId, {
    actor: "observer:drift-demo",
    title: "Add email reminder behavior and detect stale docs",
    status: "MF_IN_PROGRESS",
    priority: "P2",
    mf_type: "chain_rescue",
    force_admit: true,
    target_files: ["src/reminders.js", "tests/reminders.test.mjs", "docs/reminders.md"],
    test_files: ["tests/reminders.test.mjs"],
    provenance_paths: audit.evidence.trace_ids.filter(Boolean),
    acceptance_criteria: ["Email reminder behavior is implemented.", "Docs drift is checked before docs are updated.", "Docs are updated and reconciled after the drift check."],
  });
  audit.evidence.backlog_ids.push(bugId);
  const startEvent = await appendTimeline({ backlog_id: bugId, actor: "observer:drift-demo", event_type: "implementation_started", event_kind: "implementation", phase: "implementation", status: "accepted", payload: { graph_query_trace_ids: audit.evidence.trace_ids.filter(Boolean) } });
  if (startEvent?.id) audit.evidence.timeline_event_ids.push(startEvent.id);

  const beforeDoc = readFileSync(path.join(FIXTURE_ROOT, "docs/reminders.md"), "utf8");
  changeReminderBehaviorWithoutDocs();
  let test = run("node", ["tests/reminders.test.mjs"], FIXTURE_ROOT, true);
  assert(test.ok, test.stderr || "reminder tests failed after behavior change");
  let commit = commitAll("feat: add email reminder behavior", bugId);
  audit.evidence.tests.push(test);
  audit.evidence.commits.push({ kind: "code_without_docs", commit });
  const statusBeforeReconcile = await http("GET", `/api/graph-governance/${pid(PROJECT)}/status`);
  const opsBefore = await http("GET", `/api/graph-governance/${pid(PROJECT)}/operations/queue`);
  const feedbackBefore = await http("GET", `/api/graph-governance/${pid(PROJECT)}/snapshots/active/feedback/queue`);
  const mismatch = {
    changed_source: "src/reminders.js",
    unchanged_doc: "docs/reminders.md",
    doc_still_says_email_not_supported: beforeDoc.includes("Email reminders are not supported"),
    source_now_mentions_email: readFileSync(path.join(FIXTURE_ROOT, "src/reminders.js"), "utf8").includes("email"),
  };
  let driftSignals = driftSignalFrom(statusBeforeReconcile, opsBefore, feedbackBefore);
  const driftEvent = await appendTimeline({ backlog_id: bugId, actor: "observer:drift-demo", event_type: "docs_drift_probe_before_doc_fix", event_kind: "verification", phase: "drift_probe", status: "accepted", payload: { mismatch, drift_signals: driftSignals }, verification: { tests_run: [test.command], tests_exit_code: 0 } });
  if (driftEvent?.id) audit.evidence.timeline_event_ids.push(driftEvent.id);
  if (!driftSignals.length) {
    audit.warnings.push("Governance API did not expose a clear docs/reminders.md drift row before reconcile; report records source/doc mismatch and recommended action instead.");
  }

  const firstReconcile = await http("POST", `/api/graph-governance/${pid(PROJECT)}/reconcile/full`, { actor: "observer:drift-demo", project_root: FIXTURE_ROOT, activate: true, semantic_enrich: false, run_id: `drift-code-${RUN_ID}` });
  const statusAfterCodeReconcile = await http("GET", `/api/graph-governance/${pid(PROJECT)}/status`);
  const opsAfterCodeReconcile = await http("GET", `/api/graph-governance/${pid(PROJECT)}/operations/queue`);
  driftSignals = driftSignals.concat(driftSignalFrom(statusAfterCodeReconcile, opsAfterCodeReconcile, {}));
  if (!driftSignals.length) audit.warnings.push("Post-code reconcile still did not expose a dashboard/API drift row; limitation is non-blocking for this audit.");

  updateDocsForEmailReminder();
  test = run("node", ["tests/reminders.test.mjs"], FIXTURE_ROOT, true);
  assert(test.ok, test.stderr || "reminder tests failed after doc fix");
  commit = commitAll("docs: update reminder behavior", bugId);
  audit.evidence.tests.push(test);
  audit.evidence.commits.push({ kind: "doc_fix", commit });
  const secondReconcile = await http("POST", `/api/graph-governance/${pid(PROJECT)}/reconcile/full`, { actor: "observer:drift-demo", project_root: FIXTURE_ROOT, activate: true, semantic_enrich: false, run_id: `drift-docs-${RUN_ID}` });
  const finalStatus = await http("GET", `/api/graph-governance/${pid(PROJECT)}/status`);
  const closeEvent = await appendTimeline({ backlog_id: bugId, actor: "observer:drift-demo", event_type: "docs_drift_resolved_or_limited", event_kind: "close_ready", phase: "close_ready", status: "accepted", payload: { code_reconcile: firstReconcile, docs_reconcile: secondReconcile, final_status: finalStatus, warnings: audit.warnings } });
  if (closeEvent?.id) audit.evidence.timeline_event_ids.push(closeEvent.id);
  await upsertBacklog(bugId, { actor: "observer:drift-demo", status: "FIXED", commit, force_admit: true });

  audit.evidence.mismatch = mismatch;
  audit.evidence.drift_signals = driftSignals;
  audit.evidence.reconciles = [firstReconcile, secondReconcile];
  audit.checks.push({ name: "changed source while doc still described old behavior", passed: mismatch.source_now_mentions_email && mismatch.doc_still_says_email_not_supported });
  audit.checks.push({ name: "docs updated after drift probe", passed: readFileSync(path.join(FIXTURE_ROOT, "docs/reminders.md"), "utf8").includes("optional email reminders") });
  audit.self_review = [
    "This audit demonstrates the after-work mismatch with a real source change and unchanged doc.",
    "A missing clear drift row is reported as an honest product gap, not hidden as a pass.",
    "The resolution path updates docs, commits, and reconciles again.",
  ];
  writeReports(audit);
}

function markdown(audit) {
  return `# Docs Drift Demo Audit

- Run ID: \`${audit.run_id}\`
- Project: \`${audit.project_id}\`
- Fixture: \`${audit.fixture_root}\`
- Backend: \`${audit.backend}\`

## Evidence

- Backlog rows: ${audit.evidence.backlog_ids.map((id) => `\`${id}\``).join(", ")}
- Timeline events: ${audit.evidence.timeline_event_ids.map((id) => `\`${id}\``).join(", ")}
- Graph traces: ${audit.evidence.trace_ids.filter(Boolean).map((id) => `\`${id}\``).join(", ")}
- Commits: ${audit.evidence.commits.map((item) => `\`${item.kind}:${item.commit.slice(0, 12)}\``).join(", ")}
- Drift signals: ${audit.evidence.drift_signals.length ? audit.evidence.drift_signals.map((item) => `\`${item.source}:${item.status}\``).join(", ") : "none exposed by API"}

## Warnings

${audit.warnings.length ? audit.warnings.map((item) => `- ${item}`).join("\n") : "- None"}

## Checks

${audit.checks.map((check) => `- ${check.passed ? "PASS" : "FAIL"}: ${check.name}`).join("\n")}

## Recommended Action

When no clear dashboard drift row is exposed, show changed source \`src/reminders.js\`, impacted doc \`docs/reminders.md\`, and ask the user to review/update the doc before treating it as current.
`;
}

function writeReports(audit) {
  audit.finished_at = new Date().toISOString();
  mkdirSync(path.dirname(REPORT), { recursive: true });
  writeFileSync(REPORT, markdown(audit), "utf8");
  writeFileSync(JSON_REPORT, JSON.stringify(audit, null, 2), "utf8");
  console.log(JSON.stringify({ ok: true, report: REPORT, json_report: JSON_REPORT, project_id: PROJECT, warnings: audit.warnings }, null, 2));
}

try {
  await runAudit();
} catch (error) {
  console.error(error.message);
  exit(1);
}
