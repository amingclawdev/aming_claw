#!/usr/bin/env node

import { mkdirSync, writeFileSync } from "node:fs";
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
const PROJECT = clean(FLAGS["project-id"] || `daily-planner-lite-dupe-${RUN_ID}`).toLowerCase();
const FIXTURE_ROOT = path.resolve(FLAGS["fixture-root"] || path.join(os.tmpdir(), "ac-backlog-dupe-demo", RUN_ID));
const REPORT = path.resolve(FLAGS.report || path.join(REPO_ROOT, "docs", "backlog-dupe-demo", "audits", `${RUN_ID}.md`));
const JSON_REPORT = path.resolve(FLAGS["json-report"] || REPORT.replace(/\.md$/i, ".json"));
const SETUP_BUG_ID = "PLANNER-REMINDER-DEFAULTS";

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

function run(command, args, cwd = REPO_ROOT) {
  return execFileSync(command, args, { cwd, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }).trim();
}

function parseFixtureOutput(stdout) {
  const start = stdout.lastIndexOf("{");
  assert(start >= 0, "fixture did not print JSON");
  return JSON.parse(stdout.slice(start));
}

function runFixture() {
  return parseFixtureOutput(run("node", [
    path.join(SCRIPT_DIR, "e2e-backlog-dupe-fixture.mjs"),
    "--backend", BACKEND,
    "--project-id", PROJECT,
    "--fixture-root", FIXTURE_ROOT,
    "--run-id", RUN_ID,
    "--reset-fixture",
    "--no-browser",
  ]));
}

async function graphQuery(tool, args, actor = "backlog_dupe_audit") {
  return http("POST", `/api/graph-governance/${pid(PROJECT)}/query`, { tool, args, actor, query_source: "observer", query_purpose: "prompt_context_build" });
}

async function upsertBacklog(bugId, body) {
  return http("POST", `/api/backlog/${pid(PROJECT)}/${encodeURIComponent(bugId)}`, body);
}

async function appendTimeline(body) {
  return http("POST", `/api/task/${pid(PROJECT)}/timeline`, body);
}

function overlapScore(existing, proposal) {
  const haystack = `${existing.title || ""} ${existing.details_md || ""} ${JSON.stringify(existing.acceptance_criteria || "")} ${JSON.stringify(existing.target_files || "")}`.toLowerCase();
  const words = proposal.toLowerCase().split(/[^a-z0-9]+/).filter((word) => word.length > 3);
  const matches = words.filter((word, index, values) => values.indexOf(word) === index && haystack.includes(word));
  return { score: matches.length, matches };
}

async function runAudit() {
  const audit = {
    schema_version: "backlog_dupe_demo_audit.v1",
    run_id: RUN_ID,
    project_id: PROJECT,
    backend: BACKEND,
    fixture_root: FIXTURE_ROOT,
    evidence: { backlog_ids: [], timeline_event_ids: [], trace_ids: [] },
    checks: [],
    warnings: [],
  };
  audit.fixture = runFixture();
  const before = await http("GET", `/api/backlog/${pid(PROJECT)}?include_closed=true`);
  const setupRows = (before.bugs || []).filter((bug) => bug.bug_id === SETUP_BUG_ID);
  assert(setupRows.length === 1, "fixture did not seed exactly one setup backlog row");
  const setup = setupRows[0];
  audit.evidence.backlog_ids.push(SETUP_BUG_ID);
  const sourceTrace = await graphQuery("find_node_by_path", { path: "src/reminders.js" });
  const storageTrace = await graphQuery("find_node_by_path", { path: "src/storage.js" });
  audit.evidence.trace_ids.push(...[sourceTrace.trace_id, storageTrace.trace_id].filter(Boolean));

  const proposal = "Add a reminder toggle for tasks, default off, and persist the reminder state.";
  const search = await http("GET", `/api/backlog/${pid(PROJECT)}?include_closed=true&q=${encodeURIComponent("reminder toggle default off")}`);
  const candidates = (search.bugs || []).length ? search.bugs : (before.bugs || []);
  const scored = candidates.map((bug) => ({ bug_id: bug.bug_id, title: bug.title, status: bug.status, target_files: bug.target_files, acceptance_criteria: bug.acceptance_criteria, overlap: overlapScore(bug, proposal) }));
  const best = scored.find((item) => item.bug_id === SETUP_BUG_ID) || scored[0];
  assert(best && best.overlap.score > 0, "overlap probe did not find setup backlog row");
  const overlapEvent = await appendTimeline({
    backlog_id: SETUP_BUG_ID,
    actor: "observer:backlog-dupe",
    event_type: "overlap_detected_before_new_row",
    event_kind: "requirement",
    phase: "Clarifying",
    status: "accepted",
    payload: {
      user_proposal: proposal,
      existing_backlog_id: SETUP_BUG_ID,
      overlap: best,
      backlog_search_returned_count: (search.bugs || []).length,
      safe_choices: ["merge into existing row", "supersede existing row", "create separate row with explicit difference"],
      graph_query_trace_ids: audit.evidence.trace_ids.filter(Boolean),
    },
  });
  if (overlapEvent?.id) audit.evidence.timeline_event_ids.push(overlapEvent.id);

  const userChoice = "merge into existing row";
  await upsertBacklog(SETUP_BUG_ID, {
    actor: "observer:backlog-dupe",
    title: setup.title,
    status: setup.status || "OPEN",
    priority: setup.priority || "P2",
    mf_type: "chain_rescue",
    force_admit: true,
    target_files: ["src/reminders.js", "src/storage.js", "tests/reminders.test.mjs"],
    test_files: ["tests/reminders.test.mjs"],
    provenance_paths: audit.evidence.trace_ids.filter(Boolean),
    acceptance_criteria: [
      "Reminder toggle exists for each task.",
      "New tasks default reminder toggle to off.",
      "Reminder state persists with task storage.",
      "Merged user wording: Add a reminder toggle for tasks, default off, and persist the reminder state.",
    ],
    details_md: `${setup.details_md || ""}\n\nAUDIT MERGE DECISION: User chose to merge the similar proposal into this existing setup row. No duplicate backlog row should be created.`,
  });
  const mergeEvent = await appendTimeline({
    backlog_id: SETUP_BUG_ID,
    actor: "observer:backlog-dupe",
    event_type: "user_choice_merge_existing_row",
    event_kind: "requirement",
    phase: "Backlog Contracts",
    status: "accepted",
    payload: { user_choice: userChoice, duplicate_row_created: false, existing_backlog_id: SETUP_BUG_ID },
  });
  if (mergeEvent?.id) audit.evidence.timeline_event_ids.push(mergeEvent.id);

  const after = await http("GET", `/api/backlog/${pid(PROJECT)}?include_closed=true&q=${encodeURIComponent("reminder")}`);
  const reminderRows = (after.bugs || []).filter((bug) => JSON.stringify(bug).toLowerCase().includes("reminder"));
  const duplicateRows = reminderRows.filter((bug) => bug.bug_id !== SETUP_BUG_ID && JSON.stringify(bug).toLowerCase().includes("toggle"));
  assert(duplicateRows.length === 0, `duplicate reminder toggle rows were created: ${duplicateRows.map((bug) => bug.bug_id).join(", ")}`);

  audit.evidence.overlap = best;
  audit.evidence.user_choice = userChoice;
  audit.evidence.before_count = before.bugs?.length || 0;
  audit.evidence.after_count = after.bugs?.length || 0;
  audit.evidence.duplicate_rows = duplicateRows;
  audit.checks.push({ name: "exactly one setup backlog row existed", passed: true });
  audit.checks.push({ name: "overlap surfaced before creating a new row", passed: true, overlap_score: best.overlap.score, matches: best.overlap.matches });
  audit.checks.push({ name: "merge choice created no duplicate row", passed: duplicateRows.length === 0 });
  audit.self_review = [
    "This audit stays in requirement intake mode; it does not dispatch implementation.",
    "The duplicate warning cites backlog id, status, target files, acceptance criteria, and matching terms.",
    "The user choice is simulated as merge, and the existing row is updated instead of creating a second row.",
  ];
  writeReports(audit);
}

function markdown(audit) {
  return `# Backlog Duplicate Demo Audit

- Run ID: \`${audit.run_id}\`
- Project: \`${audit.project_id}\`
- Fixture: \`${audit.fixture_root}\`
- Backend: \`${audit.backend}\`

## Evidence

- Existing setup row: \`${SETUP_BUG_ID}\`
- Timeline events: ${audit.evidence.timeline_event_ids.map((id) => `\`${id}\``).join(", ")}
- Graph traces: ${audit.evidence.trace_ids.filter(Boolean).map((id) => `\`${id}\``).join(", ")}
- User choice: \`${audit.evidence.user_choice}\`
- Duplicate rows created: ${audit.evidence.duplicate_rows.length}

## Overlap

- Existing id: \`${audit.evidence.overlap.bug_id}\`
- Title: ${audit.evidence.overlap.title}
- Status: ${audit.evidence.overlap.status}
- Matched terms: ${audit.evidence.overlap.overlap.matches.join(", ")}
- Target files: \`${JSON.stringify(audit.evidence.overlap.target_files)}\`

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
