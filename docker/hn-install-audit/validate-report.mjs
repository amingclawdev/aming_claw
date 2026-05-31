#!/usr/bin/env node

import { readFileSync } from "node:fs";

let stateManagerModule;
try {
  stateManagerModule = await import("./common/state-manager.mjs");
} catch {
  stateManagerModule = await import("./state-manager.mjs");
}
const {
  runStateManagerSelfTest,
  validateStateManagerReport,
} = stateManagerModule;

const REQUIRED_SKILLS = [
  "aming-claw",
  "aming-claw-launcher",
  "aming-claw-hn-challenge",
  "aming-claw-hn-demo",
  "aming-claw-hn-demo-before-work",
  "aming-claw-hn-demo-during-work",
  "aming-claw-hn-demo-after-work",
  "aming-claw-vibe-queue-demo",
  "aming-claw-drift-demo",
  "aming-claw-backlog-dupe-demo",
];

const REQUIRED_RESOURCES = [
  "aming-claw://current-context",
  "aming-claw://skill",
  "aming-claw://graph-first",
  "aming-claw://mf-sop",
];

const REQUIRED_FIELDS = [
  "host",
  "status",
  "image_digest",
  "install_prompt_sha256",
  "demo_prompt_sha256",
  "install_command",
  "plugin_root",
  "cache_path",
  "fresh_session_id",
  "skills_seen",
  "mcp_tools_seen",
  "resources_read",
  "dashboard_health",
  "ai_fixture_readiness",
  "feature_smoke_results",
  "demo_fixture_result",
  "everyday_demo_results",
  "limitations",
  "auth_mode",
  "self_rating",
  "why_rating",
  "evidence_refs",
  "state_manager",
];

const TOKEN_PATTERNS = [
  /sk-[A-Za-z0-9_-]{16,}/,
  /ghp_[A-Za-z0-9_]{16,}/,
  /xox[baprs]-[A-Za-z0-9-]{16,}/,
  /Bearer\s+[A-Za-z0-9._-]{16,}/i,
  /CLAUDE_CODE_OAUTH_TOKEN\s*=\s*\S+/,
  /"access_token"\s*:\s*"[^"]+"/i,
  /"refresh_token"\s*:\s*"[^"]+"/i,
];

function usage() {
  console.error("Usage: node docker/hn-install-audit/validate-report.mjs [--require-live-observer-route] <report.json>|--self-test");
  process.exit(2);
}

function readReport(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    console.error(`invalid JSON report: ${error.message}`);
    process.exit(2);
  }
}

function stringsIn(value, prefix = "$") {
  if (typeof value === "string") return [[prefix, value]];
  if (Array.isArray(value)) return value.flatMap((item, index) => stringsIn(item, `${prefix}[${index}]`));
  if (value && typeof value === "object") {
    return Object.entries(value).flatMap(([key, item]) => stringsIn(item, `${prefix}.${key}`));
  }
  return [];
}

function missingFrom(required, seen) {
  const set = new Set(Array.isArray(seen) ? seen.map(String) : []);
  return required.filter((item) => !set.has(item));
}

function validateLiveObserverRoute(report, { requireRequested = false } = {}) {
  const errors = [];
  const liveObserverRoute = report.live_observer_route_result || {};
  const requested = liveObserverRoute.requested === true;
  if (requireRequested && !requested) {
    errors.push("Docker live observer route proof was required but not requested");
  }
  if (!requested && !requireRequested) return errors;

  if (liveObserverRoute.ok !== true) errors.push("Docker live observer route result is not ok");
  if (liveObserverRoute.provider_backed !== true) {
    errors.push("Docker live observer route must be provider_backed=true");
  }
  if (liveObserverRoute.raw_output_stored !== false) {
    errors.push("Docker live observer route must not store raw prompt output");
  }
  if (!String(liveObserverRoute.prompt_sha256 || "").trim()) {
    errors.push("Docker live observer route requires prompt_sha256");
  }
  if (!String(liveObserverRoute.output_sha256 || "").trim()) {
    errors.push("Docker live observer route requires output_sha256");
  }
  const evidence = liveObserverRoute.evidence || {};
  if (evidence.schema_version !== "docker_live_observer_route_evidence.v1") {
    errors.push("Docker live observer route evidence schema mismatch");
  }
  if (evidence.live_ai?.provider_backed !== true || evidence.live_ai?.calls_models !== true) {
    errors.push("Docker live observer route evidence must mark live model invocation");
  }
  if (evidence.live_ai?.container_runtime !== "docker") {
    errors.push("Docker live observer route evidence must mark container runtime docker");
  }
  if (evidence.route_alert_ack?.status !== "acknowledged") {
    errors.push("Docker live observer route missing acknowledged route alert");
  }
  if (Number(evidence.ordered_step_count || 0) < 3) {
    errors.push("Docker live observer route requires at least three ordered steps");
  }
  if (evidence.final_drift_prompt?.status !== "shown") {
    errors.push("Docker live observer route missing final drift prompt");
  }
  if (evidence.no_raw_prompt_output !== true) {
    errors.push("Docker live observer route evidence must set no_raw_prompt_output=true");
  }
  const featureSmokes = Array.isArray(report.feature_smoke_results)
    ? report.feature_smoke_results
    : [];
  const liveSmoke = featureSmokes.find((item) => item?.name === "live_observer_route");
  if (!liveSmoke || liveSmoke.ok !== true) {
    errors.push("Docker live observer route feature smoke is missing or failed");
  }
  return errors;
}

function validate(report) {
  const errors = [];
  for (const field of REQUIRED_FIELDS) {
    if (!(field in report)) errors.push(`missing required field: ${field}`);
  }
  if (!["codex", "claude"].includes(String(report.host || ""))) {
    errors.push("host must be codex or claude");
  }
  if (!["PASS", "FAIL", "SKIPPED", "LOGIN_REQUIRED"].includes(String(report.status || ""))) {
    errors.push("status must be PASS, FAIL, SKIPPED, or LOGIN_REQUIRED");
  }
  if (report.auth_mode !== "AUTH_REUSED_FROM_HOST") {
    errors.push("first Docker implementation must label auth_mode as AUTH_REUSED_FROM_HOST");
  }
  errors.push(...validateStateManagerReport(report.state_manager));

  const tokenLeaks = [];
  for (const [path, value] of stringsIn(report)) {
    for (const pattern of TOKEN_PATTERNS) {
      if (pattern.test(value)) tokenLeaks.push(`${path} matches ${pattern}`);
    }
  }
  if (tokenLeaks.length) errors.push(`token-looking values leaked: ${tokenLeaks.join("; ")}`);

  if (report.status === "PASS") {
    const missingSkills = missingFrom(REQUIRED_SKILLS, report.skills_seen);
    const missingResources = missingFrom(REQUIRED_RESOURCES, report.resources_read);
    if (missingSkills.length) errors.push(`PASS report missing skills: ${missingSkills.join(", ")}`);
    if (missingResources.length) errors.push(`PASS report missing resources: ${missingResources.join(", ")}`);
    if (!Array.isArray(report.mcp_tools_seen) || !report.mcp_tools_seen.includes("graph_query")) {
      errors.push("PASS report must include graph_query in mcp_tools_seen");
    }
    if (!report.dashboard_health || report.dashboard_health.ok !== true) {
      errors.push("PASS report must include dashboard_health.ok=true");
    }
    if (!report.ai_fixture_readiness || report.ai_fixture_readiness.ok !== true) {
      errors.push("PASS report must include ai_fixture_readiness.ok=true");
    } else if (!report.ai_fixture_readiness.isolated_governance_workspace?.started) {
      errors.push("PASS report must include started isolated_governance_workspace evidence");
    }
    if (!Array.isArray(report.feature_smoke_results) || report.feature_smoke_results.length < 1) {
      errors.push("PASS report must include feature_smoke_results");
    } else {
      const observerSmoke = report.feature_smoke_results.find((item) => item?.name === "observer_command_pending");
      if (!observerSmoke || observerSmoke.ok !== true) {
        errors.push("PASS report must include passing observer_command_pending feature smoke");
      } else {
        const checks = observerSmoke.checks || {};
        for (const check of [
          "hook_reminder_contract",
          "event_stream_received",
          "event_reminder_contract",
          "event_payload_reminder_only",
          "command_payload_preserved",
          "claim_via_token",
          "complete_via_token",
          "token_omitted_from_report",
        ]) {
          if (checks[check] !== true) errors.push(`observer_command_pending smoke failed check: ${check}`);
        }
      }
    }
    errors.push(...validateLiveObserverRoute(report));
    if (!report.demo_fixture_result || report.demo_fixture_result.ok !== true) {
      errors.push("PASS report must include demo_fixture_result.ok=true");
    }
    if (!Array.isArray(report.everyday_demo_results) || report.everyday_demo_results.length < 3) {
      errors.push("PASS report must include three everyday_demo_results");
    } else {
      for (const item of report.everyday_demo_results) {
        if (!item.fixture || item.fixture.ok !== true) errors.push(`PASS report everyday fixture failed: ${item.name || "unknown"}`);
        if (!item.audit || item.audit.ok !== true) errors.push(`PASS report everyday audit failed: ${item.name || "unknown"}`);
      }
    }
    if (!String(report.fresh_session_id || "").trim()) {
      errors.push("PASS report requires a non-empty fresh_session_id");
    }
  }

  if (report.status === "SKIPPED" && !String(report.skip_reason || "").trim()) {
    errors.push("SKIPPED report requires skip_reason");
  }
  if (report.status === "LOGIN_REQUIRED" && !String(report.skip_reason || "").trim()) {
    errors.push("LOGIN_REQUIRED report requires skip_reason");
  }

  return errors;
}

const args = process.argv.slice(2);
if (!args.length) usage();
if (args.includes("--self-test")) {
  const result = runStateManagerSelfTest();
  console.log(`INSTALL AUDIT STATE MANAGER SELF TEST OK: ${result.assertions} assertions`);
  process.exit(0);
}

const requireLiveObserverRoute = args.includes("--require-live-observer-route");
const file = args.find((arg) => !arg.startsWith("--"));
if (!file) usage();

const report = readReport(file);
const errors = validate(report);
if (requireLiveObserverRoute) {
  errors.push(...validateLiveObserverRoute(report, { requireRequested: true }));
}
if (errors.length) {
  console.error("INSTALL AUDIT REPORT INVALID");
  for (const error of errors) console.error(`- ${error}`);
  process.exit(1);
}

console.log("INSTALL AUDIT REPORT OK");
