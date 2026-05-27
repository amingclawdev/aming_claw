#!/usr/bin/env node

import { readFileSync } from "node:fs";

const REQUIRED_SKILLS = [
  "aming-claw",
  "aming-claw-launcher",
  "aming-claw-hn-demo",
  "aming-claw-hn-demo-before-work",
  "aming-claw-hn-demo-during-work",
  "aming-claw-hn-demo-after-work",
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
  "demo_fixture_result",
  "limitations",
  "auth_mode",
  "self_rating",
  "why_rating",
  "evidence_refs",
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
  console.error("Usage: node docker/hn-install-audit/validate-report.mjs <report.json>");
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
    if (!report.demo_fixture_result || report.demo_fixture_result.ok !== true) {
      errors.push("PASS report must include demo_fixture_result.ok=true");
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

const file = process.argv[2];
if (!file) usage();

const report = readReport(file);
const errors = validate(report);
if (errors.length) {
  console.error("INSTALL AUDIT REPORT INVALID");
  for (const error of errors) console.error(`- ${error}`);
  process.exit(1);
}

console.log("INSTALL AUDIT REPORT OK");
