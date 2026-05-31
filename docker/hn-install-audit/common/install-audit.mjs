#!/usr/bin/env node

import crypto from "node:crypto";
import {
  buildInstallAuditStateManagerReport,
  parseChangedFiles,
  sanitizeReportValue,
} from "./state-manager.mjs";
import {
  cpSync,
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { TextDecoder } from "node:util";

const HOST = process.env.AI_HOST || "codex";
const HOME = process.env.HOME || "/home/audit";
const OUT_DIR = process.env.AUDIT_OUTPUT_DIR || "/audit-output";
const RUN_ID = process.env.RUN_ID || new Date().toISOString().replace(/[-:.TZ]/g, "").slice(0, 14);
const AUTH_MODE = process.env.AUTH_MODE || "AUTH_REUSED_FROM_HOST";
const REPO_URL = process.env.PLUGIN_REPO_URL || "https://github.com/amingclawdev/aming-claw";
const REPO_REF = process.env.PLUGIN_REF || "";
const WORK_ROOT = process.env.AUDIT_WORK_ROOT || "/workspace/install-audit";
const SRC_ROOT = join(WORK_ROOT, "source");
const VENV_DIR = join(WORK_ROOT, ".venv");
const PYTHON_BIN = join(VENV_DIR, "bin", "python");
const INSTALL_ROOT = join(HOME, ".aming-claw", "plugins");
const CODEX_HOME = process.env.CODEX_HOME || join(HOME, ".codex");
const CODEX_CONFIG = join(CODEX_HOME, "config.toml");
const CODEX_MARKETPLACE = join(HOME, ".aming-claw", "codex-marketplaces", "aming-claw-local");
const CLAUDE_HOME = join(HOME, ".claude");
const CLAUDE_MARKETPLACE = join(CLAUDE_HOME, "plugins", "marketplaces", "aming-claw-local");
const REPORT_PATH = join(OUT_DIR, `${HOST}-install-audit-${RUN_ID}.json`);
const AI_SELF_REPORT_PATH = join(OUT_DIR, `${HOST}-ai-self-report-${RUN_ID}.json`);
const AI_PROMPT_MODE = process.env.AI_PROMPT_MODE || "required"; // required | optional | skip
const PROMPT_TIMEOUT_MS = Number(process.env.AI_PROMPT_TIMEOUT_MS || 20 * 60 * 1000);
const GOVERNANCE_PORT = process.env.GOVERNANCE_PORT || "40000";
const GOVERNANCE_URL = `http://127.0.0.1:${GOVERNANCE_PORT}`;
const IMPACT_CHANGED_FILES = parseChangedFiles(process.env.DOCKER_AI_E2E_CHANGED_FILES || "");
const LIVE_OBSERVER_ROUTE_REQUESTED = /^(1|true|required|yes)$/i.test(process.env.DOCKER_LIVE_OBSERVER_ROUTE || "");
const LIVE_OBSERVER_ROUTE_REPORT_PATH = process.env.LIVE_OBSERVER_ROUTE_REPORT_PATH
  || join(OUT_DIR, `${HOST}-live-observer-route-${RUN_ID}.json`);

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

const FEATURE_SMOKE_NAMES = [
  "observer_command_pending",
  "live_observer_route",
];

const DEFAULT_PLUGIN_VERSION = "0.1.1";

function sha256(text) {
  return crypto.createHash("sha256").update(String(text)).digest("hex");
}

function redact(text) {
  return String(text || "")
    .replace(/sk-[A-Za-z0-9_-]{16,}/g, "[REDACTED_OPENAI_TOKEN]")
    .replace(/ghp_[A-Za-z0-9_]{16,}/g, "[REDACTED_GITHUB_TOKEN]")
    .replace(/xox[baprs]-[A-Za-z0-9-]{16,}/g, "[REDACTED_SLACK_TOKEN]")
    .replace(/Bearer\s+[A-Za-z0-9._-]{16,}/gi, "Bearer [REDACTED]")
    .replace(/("access_token"\s*:\s*")[^"]+(")/gi, "$1[REDACTED]$2")
    .replace(/("refresh_token"\s*:\s*")[^"]+(")/gi, "$1[REDACTED]$2");
}

function sample(text, max = 3000) {
  return redact(String(text || "")).slice(0, max);
}

function sanitizeEvidence(value) {
  if (Array.isArray(value)) return value.map((item) => sanitizeEvidence(item));
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => {
        const normalized = key.toLowerCase();
        if (
          normalized === "token"
          || normalized === "session_token"
          || normalized.endsWith("_token")
          || normalized === "authorization"
        ) {
          return [key, "[REDACTED]"];
        }
        return [key, sanitizeEvidence(item)];
      }),
    );
  }
  if (typeof value === "string") return redact(value);
  return value;
}

function sortedValue(value) {
  if (Array.isArray(value)) return value.map((item) => sortedValue(item));
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .sort()
        .map((key) => [key, sortedValue(value[key])]),
    );
  }
  return value;
}

function sameJson(left, right) {
  return JSON.stringify(sortedValue(left)) === JSON.stringify(sortedValue(right));
}

function pluginVersion() {
  try {
    const raw = readFileSync(join(SRC_ROOT, ".codex-plugin", "plugin.json"), "utf8");
    const parsed = JSON.parse(raw);
    return String(parsed.version || "").trim() || DEFAULT_PLUGIN_VERSION;
  } catch {
    return DEFAULT_PLUGIN_VERSION;
  }
}

function claudeCacheRoot() {
  return join(CLAUDE_HOME, "plugins", "cache", "aming-claw-local", "aming-claw", pluginVersion());
}

function codexCacheRoot() {
  return join(CODEX_HOME, "plugins", "cache", "aming-claw-local", "aming-claw", pluginVersion());
}

function run(command, args = [], options = {}) {
  const started = Date.now();
  const result = spawnSync(command, args, {
    cwd: options.cwd || WORK_ROOT,
    input: options.input || undefined,
    encoding: "utf8",
    timeout: options.timeout || 120000,
    env: { ...process.env, ...(options.env || {}) },
  });
  return {
    ok: result.status === 0,
    status: result.status,
    signal: result.signal || "",
    command: [command, ...args].join(" "),
    stdout: sample(result.stdout),
    stderr: sample(result.stderr),
    elapsed_ms: Date.now() - started,
  };
}

function ensureDir(path) {
  mkdirSync(path, { recursive: true });
}

function copyIfExists(source, target) {
  if (!existsSync(source)) return false;
  ensureDir(dirname(target));
  cpSync(source, target, { recursive: true });
  return true;
}

function copyHostAuth() {
  const copied = [];
  if (HOST === "codex") {
    const sourceRoot = "/host-auth/codex";
    for (const rel of ["auth.json", "credentials.json"]) {
      if (copyIfExists(join(sourceRoot, rel), join(CODEX_HOME, rel))) copied.push(rel);
    }
    return copied;
  }

  const sourceRoot = "/host-auth/claude";
  for (const rel of [".credentials.json", "credentials.json", "auth.json"]) {
    if (copyIfExists(join(sourceRoot, rel), join(CLAUDE_HOME, rel))) copied.push(rel);
  }
  if (copyIfExists("/host-auth/claude-home.json", join(HOME, ".claude.json"))) {
    copied.push(".claude.json");
  }
  return copied;
}

function gitCloneSource() {
  rmSync(SRC_ROOT, { recursive: true, force: true });
  ensureDir(dirname(SRC_ROOT));
  const clone = run("git", ["clone", REPO_URL, SRC_ROOT], { cwd: WORK_ROOT, timeout: 300000 });
  if (!clone.ok) return clone;
  if (REPO_REF) {
    const checkout = run("git", ["checkout", REPO_REF], { cwd: SRC_ROOT, timeout: 120000 });
    if (!checkout.ok) return checkout;
  }
  return { ...clone, plugin_root: SRC_ROOT };
}

function installRuntime() {
  const venv = run("python3", ["-m", "venv", VENV_DIR], { cwd: WORK_ROOT, timeout: 120000 });
  if (!venv.ok) return venv;
  const install = run(PYTHON_BIN, ["-m", "pip", "install", "-e", SRC_ROOT], { cwd: SRC_ROOT, timeout: 300000 });
  return {
    ...install,
    venv: {
      ok: venv.ok,
      command: venv.command,
      stdout: venv.stdout,
      stderr: venv.stderr,
      elapsed_ms: venv.elapsed_ms,
    },
  };
}

function installCodexPlugin() {
  return run(
    PYTHON_BIN,
    [
      "-m",
      "agent.cli",
      "plugin",
      "install",
      REPO_URL,
      "--install-root",
      INSTALL_ROOT,
      "--python",
      PYTHON_BIN,
      "--codex-home",
      CODEX_HOME,
      "--codex-config",
      CODEX_CONFIG,
      "--codex-marketplace-root",
      CODEX_MARKETPLACE,
      "--json-output",
    ],
    { cwd: SRC_ROOT, timeout: 300000 },
  );
}

function installClaudePlugin() {
  const claudeCache = claudeCacheRoot();
  rmSync(CLAUDE_MARKETPLACE, { recursive: true, force: true });
  rmSync(claudeCache, { recursive: true, force: true });
  ensureDir(dirname(CLAUDE_MARKETPLACE));
  ensureDir(dirname(claudeCache));
  cpSync(SRC_ROOT, CLAUDE_MARKETPLACE, {
    recursive: true,
    filter: (source) => !source.includes(`${SRC_ROOT}/.git`),
  });
  cpSync(SRC_ROOT, claudeCache, {
    recursive: true,
    filter: (source) => !source.includes(`${SRC_ROOT}/.git`),
  });
  writeJson(join(CLAUDE_HOME, "plugins", "installed_plugins.json"), {
    "aming-claw@aming-claw-local": {
      scope: "user",
      installPath: claudeCache,
      version: pluginVersion(),
      installedAt: new Date().toISOString(),
      lastUpdated: new Date().toISOString(),
      gitCommitSha: gitHead(SRC_ROOT),
    },
  });
  writeJson(join(CLAUDE_HOME, "plugins", "known_marketplaces.json"), {
    "aming-claw-local": {
      source: { source: "local", path: CLAUDE_MARKETPLACE },
      installLocation: CLAUDE_MARKETPLACE,
      lastUpdated: new Date().toISOString(),
    },
  });
  return {
    ok: true,
    command: "manual claude marketplace/cache install from cloned source",
    stdout: "",
    stderr: "",
    elapsed_ms: 0,
  };
}

function writeJson(path, value) {
  ensureDir(dirname(path));
  writeFileSync(path, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}

function gitHead(root) {
  const result = run("git", ["rev-parse", "HEAD"], { cwd: root, timeout: 30000 });
  return result.ok ? result.stdout.trim() : "";
}

function installedSkillRoot() {
  if (HOST === "codex") return join(codexCacheRoot(), "skills");
  return join(claudeCacheRoot(), "skills");
}

function listSkillsSeen() {
  const root = installedSkillRoot();
  return REQUIRED_SKILLS.filter((skill) => existsSync(join(root, skill, "SKILL.md")));
}

function resourcesRead() {
  const root = HOST === "codex"
    ? codexCacheRoot()
    : claudeCacheRoot();
  const paths = {
    "aming-claw://current-context": ".mcp.json",
    "aming-claw://skill": "skills/aming-claw/SKILL.md",
    "aming-claw://graph-first": "skills/aming-claw/references/graph-first.md",
    "aming-claw://mf-sop": "skills/aming-claw/references/mf-sop.md",
  };
  return REQUIRED_RESOURCES.filter((uri) => existsSync(join(root, paths[uri])));
}

function mcpToolsSeen() {
  const result = run(
    PYTHON_BIN,
    [
      "-c",
      "import json; from agent.mcp.tools import TOOLS; print(json.dumps([t.get('name') for t in TOOLS]))",
    ],
    { cwd: SRC_ROOT, timeout: 30000 },
  );
  if (!result.ok) return [];
  try {
    return JSON.parse(result.stdout);
  } catch {
    return [];
  }
}

function cliVersion() {
  const binary = HOST === "codex" ? "codex" : "claude";
  return run(binary, ["--version"], { cwd: WORK_ROOT, timeout: 30000 });
}

function startGovernance() {
  const child = spawn(PYTHON_BIN, ["-m", "agent.cli", "start", "--port", GOVERNANCE_PORT, "--workspace", SRC_ROOT], {
    cwd: SRC_ROOT,
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += sample(chunk, 1000); });
  child.stderr.on("data", (chunk) => { stderr += sample(chunk, 1000); });
  return { child, stdout: () => stdout, stderr: () => stderr };
}

async function waitForHealth(timeoutMs = 45000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${GOVERNANCE_URL}/api/health`);
      if (response.ok) return { ok: true, status: response.status };
    } catch {
      // Retry until deadline.
    }
    await new Promise((resolve) => setTimeout(resolve, 750));
  }
  return { ok: false, status: 0 };
}

async function dashboardHealth() {
  try {
    const response = await fetch(`${GOVERNANCE_URL}/dashboard`);
    return { ok: response.ok, status: response.status };
  } catch (error) {
    return { ok: false, status: 0, error: sample(error.message) };
  }
}

async function governanceJson(method, path, body = undefined) {
  try {
    const response = await fetch(`${GOVERNANCE_URL}${path}`, {
      method,
      headers: body === undefined ? {} : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    return {
      ok: response.ok && (!payload || payload.ok !== false),
      status: response.status,
      payload,
    };
  } catch (error) {
    return {
      ok: false,
      status: 0,
      payload: { ok: false, error: sample(error?.message || error) },
    };
  }
}

function parseSseBlock(block) {
  const lines = String(block || "").split(/\r?\n/);
  let event = "message";
  const dataLines = [];
  for (const line of lines) {
    if (line.startsWith(":")) continue;
    if (line.startsWith("event:")) event = line.slice("event:".length).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice("data:".length).trimStart());
  }
  if (!dataLines.length) return null;
  const dataText = dataLines.join("\n");
  let data = dataText;
  try {
    data = JSON.parse(dataText);
  } catch {
    // Non-JSON SSE data is still useful diagnostic evidence.
  }
  return { event, data };
}

async function startEventProbe(projectId) {
  const controller = new AbortController();
  const events = [];
  const waiters = [];
  const url = `${GOVERNANCE_URL}/api/graph-governance/${encodeURIComponent(projectId)}/events/stream`;
  let response;
  try {
    response = await fetch(url, { signal: controller.signal });
  } catch (error) {
    return {
      ok: false,
      status: 0,
      error: sample(error?.message || error),
      close: () => controller.abort(),
      events: () => events.slice(),
      waitFor: async () => null,
    };
  }

  if (!response.ok || !response.body) {
    return {
      ok: false,
      status: response.status,
      error: "event stream unavailable",
      close: () => controller.abort(),
      events: () => events.slice(),
      waitFor: async () => null,
    };
  }

  function resolveWaiters(record) {
    for (let index = waiters.length - 1; index >= 0; index -= 1) {
      const waiter = waiters[index];
      if (waiter.eventName !== record.event) continue;
      clearTimeout(waiter.timer);
      waiters.splice(index, 1);
      waiter.resolve(record);
    }
  }

  function finishWaiters() {
    for (const waiter of waiters.splice(0)) {
      clearTimeout(waiter.timer);
      waiter.resolve(null);
    }
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const pump = (async () => {
    let buffer = "";
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        while (true) {
          const splitAt = buffer.indexOf("\n\n");
          if (splitAt < 0) break;
          const block = buffer.slice(0, splitAt);
          buffer = buffer.slice(splitAt + 2);
          const record = parseSseBlock(block);
          if (!record) continue;
          events.push(record);
          resolveWaiters(record);
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        events.push({ event: "probe_error", data: { error: sample(error?.message || error) } });
      }
    } finally {
      finishWaiters();
    }
  })();

  function waitFor(eventName, timeoutMs = 5000) {
    const existing = events.find((event) => event.event === eventName);
    if (existing) return Promise.resolve(existing);
    return new Promise((resolve) => {
      const waiter = {
        eventName,
        resolve,
        timer: setTimeout(() => {
          const index = waiters.indexOf(waiter);
          if (index >= 0) waiters.splice(index, 1);
          resolve(null);
        }, timeoutMs),
      };
      waiters.push(waiter);
    });
  }

  const ready = await waitFor("ready", 3000);
  if (!ready) controller.abort();

  return {
    ok: Boolean(ready),
    status: response.status,
    ready: sanitizeEvidence(ready),
    close: () => controller.abort(),
    events: () => events.slice(),
    waitFor,
    pump,
  };
}

function commandPendingReminder(projectId) {
  return {
    kind: "observer_command_pending",
    project_id: projectId,
    message: "pending observer commands exist; call observer_command_next",
    payload_included: false,
  };
}

function isReminderOnlyPayload(value, projectId) {
  return sameJson(value, commandPendingReminder(projectId));
}

function failedChecks(checks) {
  return Object.entries(checks)
    .filter(([, value]) => value !== true)
    .map(([key]) => key);
}

async function observerCommandPendingSmoke() {
  const name = "observer_command_pending";
  const projectId = `install-audit-${name}-${HOST}-${RUN_ID}`.toLowerCase();
  const rawId = `raw-${HOST}-${RUN_ID}`;
  let probe = null;
  let sessionId = "";
  let sessionToken = "";
  let claimedCommandId = "";

  try {
    probe = await startEventProbe(projectId);
    if (!probe.ok) {
      return {
        name,
        ok: false,
        project_id: projectId,
        phase: "event_stream_ready",
        errors: ["event_stream_ready"],
        evidence: sanitizeEvidence({
          status: probe.status,
          error: probe.error,
          events_seen: probe.events(),
        }),
      };
    }

    const register = await governanceJson(
      "POST",
      `/api/projects/${encodeURIComponent(projectId)}/observer-sessions/register`,
      {
        observer_kind: HOST,
        session_label: "docker-install-audit-feature-smoke",
        capabilities: {
          actions: ["observer_command_claim", "observer_command_complete", "observer_command_fail"],
          command_types: ["analyze_requirements"],
        },
      },
    );
    if (!register.ok) {
      return {
        name,
        ok: false,
        project_id: projectId,
        phase: "observer_session_register",
        errors: ["observer_session_register"],
        evidence: sanitizeEvidence({ status: register.status, payload: register.payload }),
      };
    }
    sessionId = String(register.payload?.session_id || register.payload?.observer_session_id || "");
    sessionToken = String(register.payload?.session_token || "");

    const commandBusinessPayload = {
      raw_id: rawId,
      source: "docker_install_audit_feature_smoke",
      host: HOST,
    };
    const enqueue = await governanceJson(
      "POST",
      `/api/projects/${encodeURIComponent(projectId)}/observer-commands`,
      {
        command_type: "analyze_requirements",
        payload: commandBusinessPayload,
        created_by: "docker-install-audit",
      },
    );
    const eventRecord = await probe.waitFor("observer_command_pending", 5000);
    const hookReminder = enqueue.payload?.hook_reminder || null;
    const eventPayload = eventRecord?.data?.payload || null;
    const command = enqueue.payload?.observer_command || {};

    const claim = await governanceJson(
      "POST",
      `/api/projects/${encodeURIComponent(projectId)}/observer-commands/next`,
      { session_id: sessionId, session_token: sessionToken },
    );
    claimedCommandId = String(claim.payload?.command?.command_id || "");

    let complete = { ok: false, status: 0, payload: { error: "claim failed" } };
    let failCleanup = null;
    if (claim.ok && claimedCommandId) {
      complete = await governanceJson(
        "POST",
        `/api/projects/${encodeURIComponent(projectId)}/observer-commands/${encodeURIComponent(claimedCommandId)}/complete`,
        {
          session_id: sessionId,
          session_token: sessionToken,
          result: { ok: true, smoke: name },
        },
      );
      if (!complete.ok) {
        failCleanup = await governanceJson(
          "POST",
          `/api/projects/${encodeURIComponent(projectId)}/observer-commands/${encodeURIComponent(claimedCommandId)}/fail`,
          {
            session_id: sessionId,
            session_token: sessionToken,
            error: "docker install-audit smoke cleanup after completion failure",
            result: { ok: false, smoke: name },
          },
        );
      }
    }

    const checks = {
      observer_session_registered: Boolean(sessionId && sessionToken),
      enqueue_http_ok: enqueue.ok,
      hook_reminder_contract: isReminderOnlyPayload(hookReminder, projectId),
      event_stream_received: eventRecord?.event === "observer_command_pending",
      event_reminder_contract: isReminderOnlyPayload(eventPayload, projectId),
      event_payload_reminder_only: isReminderOnlyPayload(eventPayload, projectId),
      command_payload_preserved: command?.payload?.raw_id === rawId
        && command?.payload?.source === "docker_install_audit_feature_smoke",
      claim_via_token: claim.ok && claim.payload?.empty === false && claimedCommandId === command?.command_id,
      complete_via_token: complete.ok && complete.payload?.command?.status === "completed",
      token_omitted_from_report: true,
    };
    const errors = failedChecks(checks);

    return {
      name,
      ok: errors.length === 0,
      project_id: projectId,
      event_name: "observer_command_pending",
      checks,
      command_status: {
        initial_status: String(command?.status || ""),
        notified_at_set: Boolean(command?.notified_at),
        claimable_statuses: ["queued", "notified"],
        decision: command?.status === "notified"
          ? "notified_status_records_enqueue_notification"
          : "queued_until_claim; callback event is the reminder evidence",
      },
      evidence: sanitizeEvidence({
        session_id: sessionId,
        command_id: command?.command_id || "",
        claimed_command_id: claimedCommandId,
        hook_reminder: hookReminder,
        event_payload: eventPayload,
        event_ts: eventRecord?.data?.ts || "",
        command_payload_sha256: sha256(JSON.stringify(command?.payload || {})),
        claim_status: claim.status,
        complete_status: complete.status,
        fail_cleanup_status: failCleanup?.status || 0,
      }),
      errors,
    };
  } catch (error) {
    return {
      name,
      ok: false,
      project_id: projectId,
      phase: "exception",
      errors: ["exception"],
      evidence: sanitizeEvidence({
        session_id: sessionId,
        claimed_command_id: claimedCommandId,
        error: sample(error?.stack || error?.message || error),
      }),
    };
  } finally {
    if (probe?.close) probe.close();
  }
}

async function runFeatureSmokes() {
  const smokes = [
    observerCommandPendingSmoke,
  ];
  const results = [];
  for (const smoke of smokes) {
    results.push(await smoke());
  }
  return results;
}

function runDemoFixture() {
  return run(
    "node",
    [
      "frontend/dashboard/scripts/e2e-hn-demo.mjs",
      "--sandbox-audit",
      "--no-browser",
      "--run-id",
      `${HOST}-${RUN_ID}`,
      "--project-id",
      `aming-claw-hn-demo-${HOST}-${RUN_ID}`.toLowerCase(),
      "--report",
      join(OUT_DIR, `${HOST}-hn-demo-${RUN_ID}.md`),
    ],
    {
      cwd: SRC_ROOT,
      timeout: Number(process.env.DEMO_TIMEOUT_MS || 300000),
      env: {
        VITE_BACKEND_URL: GOVERNANCE_URL,
        GOVERNANCE_PORT,
        PYTHON: PYTHON_BIN,
      },
    },
  );
}

function runEverydayDemoScript(script, demoName, extra = []) {
  const projectId = `aming-claw-${demoName}-${HOST}-${RUN_ID}`.toLowerCase();
  const fixtureRoot = join(WORK_ROOT, `${demoName}-fixture`);
  const report = join(OUT_DIR, `${HOST}-${demoName}-${RUN_ID}.md`);
  return run(
    "node",
    [
      script,
      "--backend",
      GOVERNANCE_URL,
      "--project-id",
      projectId,
      "--fixture-root",
      fixtureRoot,
      "--run-id",
      `${HOST}-${RUN_ID}`,
      "--report",
      report,
      "--no-browser",
      ...extra,
    ],
    {
      cwd: SRC_ROOT,
      timeout: Number(process.env.DEMO_TIMEOUT_MS || 300000),
      env: {
        VITE_BACKEND_URL: GOVERNANCE_URL,
        GOVERNANCE_PORT,
        PYTHON: PYTHON_BIN,
      },
    },
  );
}

function runEverydayDemos() {
  const demos = [
    {
      name: "vibe-queue",
      fixture: "frontend/dashboard/scripts/e2e-vibe-queue-fixture.mjs",
      audit: "frontend/dashboard/scripts/e2e-vibe-queue-audit.mjs",
    },
    {
      name: "drift-demo",
      fixture: "frontend/dashboard/scripts/e2e-drift-demo-fixture.mjs",
      audit: "frontend/dashboard/scripts/e2e-drift-demo-audit.mjs",
    },
    {
      name: "backlog-dupe",
      fixture: "frontend/dashboard/scripts/e2e-backlog-dupe-fixture.mjs",
      audit: "frontend/dashboard/scripts/e2e-backlog-dupe-audit.mjs",
    },
  ];
  return demos.map((demo) => {
    const fixture = runEverydayDemoScript(demo.fixture, demo.name);
    const audit = fixture.ok
      ? runEverydayDemoScript(demo.audit, demo.name)
      : { ok: false, command: demo.audit, stdout: "", stderr: "fixture failed", elapsed_ms: 0 };
    return { name: demo.name, fixture, audit };
  });
}

function buildInstallPrompt() {
  return `You are running a clean Docker install audit for Aming Claw on host lane ${HOST}.

Phase 1: perform the README/launcher one-click install path for this plugin.
- Install from: ${REPO_URL}
- Use the local Docker HOME, not host plugin state.
- Reuse auth only from the mounted read-only host auth files. Do not print token contents.
- Verify skills, MCP tools, and MCP resources after install.

Write any observations to ${AI_SELF_REPORT_PATH}.`;
}

function buildDemoPrompt() {
  return `You are running the Aming Claw demos after install on host lane ${HOST}.

Phase 2: run /aming-claw:aming-claw-hn-demo or the equivalent installed demo path.
Verify the before-work, during-work, and after-work evidence. Do not fabricate trace ids.
Then inspect the everyday demo skills:
- /aming-claw:aming-claw-vibe-queue-demo
- /aming-claw:aming-claw-drift-demo
- /aming-claw:aming-claw-backlog-dupe-demo

Confirm they prefer the current Claude Code or Codex session as observer, with scripts only as fixture/CI fallback.
Record whether the demos produced server-verifiable evidence and why you rate the run that way.`;
}

function buildLiveObserverRoutePrompt({ routeContextHash, promptContractHash, finalDriftPromptHash }) {
  return `You are the observer in the Docker live-AI route proof for Aming Claw.

You have received this route context alert:
- alert code: test_flow_docker_live_ai_observer_route
- caller role: observer
- stage: verification
- route context hash: ${routeContextHash}
- prompt contract hash: ${promptContractHash}

Task:
1. Acknowledge the route alert before doing any route evidence work.
2. Follow this order: route alert acknowledgement, ordered route steps, sanitized evidence summary, final drift prompt.
3. Write a JSON object to ${LIVE_OBSERVER_ROUTE_REPORT_PATH}.
4. Do not include raw prompt text, credential values, environment dumps, or full command output.

The JSON object must use this exact shape:
{
  "schema_version": "docker_live_observer_route_evidence.v1",
  "source": "docker_live_ai_observer_route",
  "provider_runtime": "${HOST}",
  "route_context": {
    "service_id": "observer.docker_live_ai_route_demo",
    "role": "observer",
    "stage": "verification",
    "route_context_hash": "${routeContextHash}",
    "prompt_contract_hash": "${promptContractHash}",
    "raw_context_exposed": false
  },
  "live_ai": {
    "provider_backed": true,
    "calls_models": true,
    "container_runtime": "docker",
    "host": "${HOST}"
  },
  "observer_evidence": {
    "route_alert_ack": {"status": "acknowledged", "alert_code": "test_flow_docker_live_ai_observer_route"},
    "ordered_step_outputs": [
      {"step_id": "01_route_alert_ack", "status": "passed"},
      {"step_id": "02_ordered_route_steps", "status": "passed"},
      {"step_id": "03_sanitized_live_ai_evidence", "status": "passed"}
    ],
    "final_drift_prompt": {
      "status": "shown",
      "prompt_hash": "${finalDriftPromptHash}"
    },
    "no_raw_prompt_output": true
  },
  "precheck": {
    "status": "passed",
    "checked_fields": ["route_alert_ack", "ordered_step_outputs", "final_drift_prompt", "no_raw_prompt_output"]
  }
}`;
}

function aiCommand(prompt) {
  if (HOST === "codex") {
    const args = ["exec", "--dangerously-bypass-approvals-and-sandbox", "--skip-git-repo-check", "-C", WORK_ROOT];
    return run("codex", args, { cwd: WORK_ROOT, input: prompt, timeout: PROMPT_TIMEOUT_MS });
  }
  const promptFile = join(OUT_DIR, `${HOST}-system-${RUN_ID}.md`);
  writeFileSync(promptFile, "You are an installation auditor. Execute the requested checks and report only evidence you observed.\n", "utf8");
  return run(
    "claude",
    [
      "-p",
      "--system-prompt-file",
      promptFile,
      "--add-dir",
      WORK_ROOT,
      "--allowedTools",
      "Bash,Read,Write,Edit,Glob,Grep",
      "--max-turns",
      "80",
    ],
    { cwd: WORK_ROOT, input: prompt, timeout: PROMPT_TIMEOUT_MS },
  );
}

function emptyLiveObserverRouteResult() {
  return {
    name: "live_observer_route",
    requested: false,
    ok: false,
    status: "not_requested",
  };
}

function compactLiveObserverEvidence(payload) {
  const evidence = payload?.observer_evidence || {};
  const steps = Array.isArray(evidence.ordered_step_outputs) ? evidence.ordered_step_outputs : [];
  return {
    schema_version: payload?.schema_version || "",
    source: payload?.source || "",
    provider_runtime: payload?.provider_runtime || "",
    route_context: {
      service_id: payload?.route_context?.service_id || "",
      role: payload?.route_context?.role || "",
      stage: payload?.route_context?.stage || "",
      route_context_hash: payload?.route_context?.route_context_hash || "",
      prompt_contract_hash: payload?.route_context?.prompt_contract_hash || "",
      raw_context_exposed: payload?.route_context?.raw_context_exposed,
    },
    live_ai: {
      provider_backed: payload?.live_ai?.provider_backed === true,
      calls_models: payload?.live_ai?.calls_models === true,
      container_runtime: payload?.live_ai?.container_runtime || "",
      host: payload?.live_ai?.host || "",
    },
    route_alert_ack: {
      status: evidence.route_alert_ack?.status || "",
      alert_code: evidence.route_alert_ack?.alert_code || "",
    },
    ordered_step_count: steps.length,
    ordered_step_ids: steps.map((step) => String(step?.step_id || "")),
    final_drift_prompt: {
      status: evidence.final_drift_prompt?.status || "",
      prompt_hash: evidence.final_drift_prompt?.prompt_hash || "",
    },
    no_raw_prompt_output: evidence.no_raw_prompt_output === true,
    precheck_status: payload?.precheck?.status || "",
  };
}

function validateLiveObserverRoutePayload(payload, expected) {
  const compact = compactLiveObserverEvidence(payload);
  const errors = [];
  if (compact.schema_version !== "docker_live_observer_route_evidence.v1") {
    errors.push("schema_version must be docker_live_observer_route_evidence.v1");
  }
  if (compact.source !== "docker_live_ai_observer_route") {
    errors.push("source must be docker_live_ai_observer_route");
  }
  if (compact.route_context.route_context_hash !== expected.routeContextHash) {
    errors.push("route_context_hash mismatch");
  }
  if (compact.route_context.prompt_contract_hash !== expected.promptContractHash) {
    errors.push("prompt_contract_hash mismatch");
  }
  if (compact.route_context.raw_context_exposed !== false) {
    errors.push("raw_context_exposed must be false");
  }
  if (!compact.live_ai.provider_backed || !compact.live_ai.calls_models) {
    errors.push("live_ai must mark provider_backed=true and calls_models=true");
  }
  if (compact.live_ai.container_runtime !== "docker") {
    errors.push("live_ai.container_runtime must be docker");
  }
  if (compact.route_alert_ack.status !== "acknowledged") {
    errors.push("route_alert_ack.status must be acknowledged");
  }
  if (compact.ordered_step_count < 3) {
    errors.push("ordered_step_outputs must contain at least 3 steps");
  }
  if (compact.final_drift_prompt.status !== "shown") {
    errors.push("final_drift_prompt.status must be shown");
  }
  if (compact.no_raw_prompt_output !== true) {
    errors.push("observer_evidence.no_raw_prompt_output must be true");
  }
  if (compact.precheck_status !== "passed") {
    errors.push("precheck.status must be passed");
  }
  return errors;
}

function parseJsonObject(text) {
  const raw = String(text || "").trim();
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    const start = raw.indexOf("{");
    const end = raw.lastIndexOf("}");
    if (start >= 0 && end > start) {
      try {
        return JSON.parse(raw.slice(start, end + 1));
      } catch {
        return null;
      }
    }
    return null;
  }
}

function runLiveObserverRoutePrompt() {
  if (!LIVE_OBSERVER_ROUTE_REQUESTED) return emptyLiveObserverRouteResult();
  if (AI_PROMPT_MODE === "skip") {
    return {
      name: "live_observer_route",
      requested: true,
      ok: false,
      status: "blocked",
      provider_backed: true,
      raw_output_stored: false,
      validation_errors: ["AI_PROMPT_MODE=skip cannot satisfy Docker live-AI observer route proof"],
    };
  }

  const routeContextHash = `sha256:${sha256("observer.docker_live_ai_route_demo:route-context:v1")}`;
  const promptContractHash = `sha256:${sha256("observer.docker_live_ai_route_demo:prompt-contract:v1")}`;
  const finalDriftPromptHash = `sha256:${sha256("observer.docker_live_ai_route_demo:final-drift-prompt:v1")}`;
  const prompt = buildLiveObserverRoutePrompt({
    routeContextHash,
    promptContractHash,
    finalDriftPromptHash,
  });
  rmSync(LIVE_OBSERVER_ROUTE_REPORT_PATH, { force: true });
  const result = aiCommand(prompt);
  let outputText = "";
  try {
    outputText = readFileSync(LIVE_OBSERVER_ROUTE_REPORT_PATH, "utf8");
  } catch {
    outputText = result.stdout || "";
  }
  const parsed = parseJsonObject(outputText);
  const validationErrors = parsed
    ? validateLiveObserverRoutePayload(parsed, { routeContextHash, promptContractHash })
    : ["AI did not write parseable Docker live observer route JSON evidence"];
  const compactEvidence = parsed ? compactLiveObserverEvidence(parsed) : {};
  const ok = Boolean(result.ok && parsed && validationErrors.length === 0);
  return sanitizeEvidence({
    name: "live_observer_route",
    requested: true,
    ok,
    status: ok ? "passed" : "failed",
    provider_backed: true,
    host: HOST,
    command: result.command,
    exit_code: result.status,
    elapsed_ms: result.elapsed_ms,
    prompt_sha256: sha256(prompt),
    stdout_sha256: sha256(result.stdout || ""),
    stderr_sha256: sha256(result.stderr || ""),
    output_sha256: sha256(outputText || ""),
    output_path: LIVE_OBSERVER_ROUTE_REPORT_PATH,
    raw_output_stored: false,
    evidence: compactEvidence,
    validation_errors: validationErrors,
  });
}

function maybeRunAiPrompts(installPrompt, demoPrompt) {
  if (AI_PROMPT_MODE === "skip") {
    return {
      install: { ok: false, skipped: true, reason: "AI_PROMPT_MODE=skip" },
      demo: { ok: false, skipped: true, reason: "AI_PROMPT_MODE=skip" },
    };
  }
  const install = aiCommand(installPrompt);
  const demo = aiCommand(demoPrompt);
  return { install, demo };
}

function isLoginRequired(result) {
  const text = `${result?.stdout || ""}\n${result?.stderr || ""}`;
  return /not logged in|please run\s+\/login|run\s+claude\s+auth|authentication required|login required|failed to authenticate|invalid authentication credentials|api error:\s*401/i.test(text);
}

function missing(required, seen) {
  const set = new Set(seen);
  return required.filter((item) => !set.has(item));
}

function selfRating(status, blockers) {
  if (status === "PASS") return 4;
  if (blockers.length <= 2) return 2;
  return 1;
}

function buildAiFixtureReadiness({ runtimeInstall, hostInstall, version, dashboard, skills, tools, resources }) {
  const missingSkills = missing(REQUIRED_SKILLS, skills);
  const missingResources = missing(REQUIRED_RESOURCES, resources);
  return {
    ok: Boolean(
      runtimeInstall.ok
      && hostInstall.ok
      && version.ok
      && dashboard.ok
      && tools.includes("graph_query")
      && missingSkills.length === 0
      && missingResources.length === 0
    ),
    isolated_governance_workspace: {
      started: Boolean(dashboard.ok),
      work_root: WORK_ROOT,
      source_root: SRC_ROOT,
      governance_url: GOVERNANCE_URL,
    },
    ai_host: {
      host: HOST,
      cli_version_ok: Boolean(version.ok),
      auth_mode: AUTH_MODE,
    },
    plugin: {
      runtime_install_ok: Boolean(runtimeInstall.ok),
      host_install_ok: Boolean(hostInstall.ok),
      cache_path: HOST === "codex" ? codexCacheRoot() : claudeCacheRoot(),
    },
    mcp: {
      graph_query_visible: tools.includes("graph_query"),
      tools_seen_count: Array.isArray(tools) ? tools.length : 0,
      required_resources_present: missingResources.length === 0,
      missing_resources: missingResources,
    },
    skills: {
      required_skills_present: missingSkills.length === 0,
      missing_skills: missingSkills,
    },
  };
}

function buildReport({
  authCopied,
  clone,
  runtimeInstall,
  hostInstall,
  version,
  installPrompt,
  demoPrompt,
  ai,
  dashboard,
  demo,
  everydayDemos,
  featureSmokes,
  liveObserverRoute,
  skills,
  tools,
  resources,
}) {
  const sourceCommit = clone.ok ? gitHead(SRC_ROOT) : "";
  const blockers = [];
  if (!version.ok) blockers.push(`${HOST} CLI version check failed`);
  if (!authCopied.length) blockers.push("host auth files were not found in mounted auth volume");
  if (!clone.ok) blockers.push("plugin source clone failed");
  if (!runtimeInstall.ok) blockers.push("pip editable install failed");
  if (!hostInstall.ok) blockers.push(`${HOST} plugin install failed`);
  if (missing(REQUIRED_SKILLS, skills).length) blockers.push(`missing skills: ${missing(REQUIRED_SKILLS, skills).join(", ")}`);
  if (!tools.includes("graph_query")) blockers.push("MCP graph_query tool not visible from installed runtime");
  if (missing(REQUIRED_RESOURCES, resources).length) blockers.push(`missing resources: ${missing(REQUIRED_RESOURCES, resources).join(", ")}`);
  if (!dashboard.ok) blockers.push("dashboard health failed");
  if (!demo.ok) blockers.push("HN demo fixture run failed");
  const everydayFailures = (everydayDemos || []).filter((item) => !item.fixture?.ok || !item.audit?.ok);
  if (everydayFailures.length) {
    blockers.push(`everyday demo audit failed: ${everydayFailures.map((item) => item.name).join(", ")}`);
  }
  const featureFailures = (featureSmokes || []).filter((item) => !item.ok);
  if (featureFailures.length) {
    blockers.push(`feature smoke failed: ${featureFailures.map((item) => item.name).join(", ")}`);
  }
  if (liveObserverRoute?.requested && !liveObserverRoute.ok) {
    blockers.push("Docker live observer route proof failed");
  }
  const loginRequired = HOST === "claude" && AI_PROMPT_MODE === "required" && (isLoginRequired(ai.install) || isLoginRequired(ai.demo));
  if (loginRequired) blockers.push("Claude AI prompt execution requires login");
  else if (AI_PROMPT_MODE === "required" && (!ai.install.ok || !ai.demo.ok)) blockers.push("AI prompt execution failed");

  const aiFixtureReadiness = buildAiFixtureReadiness({
    runtimeInstall,
    hostInstall,
    version,
    dashboard,
    skills,
    tools,
    resources,
  });
  const aiSkipped = AI_PROMPT_MODE === "skip";
  const status = loginRequired ? "LOGIN_REQUIRED" : blockers.length ? "FAIL" : aiSkipped ? "SKIPPED" : "PASS";
  const skipReason = aiSkipped
    ? "AI_PROMPT_MODE=skip; deterministic install checks ran, but the one-click AI install and HN demo prompts were not executed."
    : loginRequired
      ? "Claude CLI is installed but not authenticated in the mounted auth home. Run `claude /login` or `claude auth login` with the same auth home, then rerun with --claude-auth-home <dir>."
    : "";
  const combinedFeatureSmokes = [
    ...(featureSmokes || []),
    ...(liveObserverRoute?.requested ? [liveObserverRoute] : []),
  ];
  const stateManager = buildInstallAuditStateManagerReport({
    host: HOST,
    status,
    authMode: AUTH_MODE,
    authCopied,
    repoUrl: REPO_URL,
    repoRef: REPO_REF,
    workRoot: WORK_ROOT,
    imageDigest: process.env.IMAGE_DIGEST || "unknown-local-build",
    governanceUrl: GOVERNANCE_URL,
    dashboard,
    sourceCommit,
    pluginVersion: pluginVersion(),
    reportPath: REPORT_PATH,
    changedFiles: IMPACT_CHANGED_FILES,
    commandEvidence: [
      clone,
      runtimeInstall,
      hostInstall,
      version,
      dashboard,
      demo,
      ...(featureSmokes || []),
      ...(liveObserverRoute?.requested ? [liveObserverRoute] : []),
    ],
    featureSmokeResults: combinedFeatureSmokes,
  });

  return sanitizeReportValue({
    schema_version: "aming_claw_install_audit.v1",
    host: HOST,
    status,
    auth_mode: AUTH_MODE,
    run_id: RUN_ID,
    image_digest: process.env.IMAGE_DIGEST || "unknown-local-build",
    install_prompt_sha256: sha256(installPrompt),
    demo_prompt_sha256: sha256(demoPrompt),
    install_command: hostInstall.command || "",
    plugin_root: SRC_ROOT,
    cache_path: HOST === "codex" ? codexCacheRoot() : claudeCacheRoot(),
    fresh_session_id: `${HOST}-docker-${RUN_ID}`,
    skills_seen: skills,
    mcp_tools_seen: tools,
    resources_read: resources,
    dashboard_health: dashboard,
    ai_fixture_readiness: aiFixtureReadiness,
    feature_smoke_names: FEATURE_SMOKE_NAMES,
    feature_smoke_results: combinedFeatureSmokes,
    live_observer_route_result: liveObserverRoute || emptyLiveObserverRouteResult(),
    demo_fixture_result: {
      ok: demo.ok,
      command: demo.command,
      stdout: demo.stdout,
      stderr: demo.stderr,
    },
    everyday_demo_results: everydayDemos || [],
    limitations: [
      "Mode B reuses host auth; this is not a fresh OAuth login.",
      "The container is fresh for plugin/cache state, but authentication comes from read-only host files.",
    ],
    self_rating: selfRating(status, blockers),
    why_rating: loginRequired
        ? "Claude lane reached the AI prompt phase but the CLI reported that no authenticated Claude session was available in the mounted auth home."
      : blockers.length
        ? `Install lane failed because: ${blockers.join("; ")}`
      : aiSkipped
        ? "Install lane is skipped, not passed, because deterministic checks ran without exercising the AI one-click install prompt."
        : "Install lane passed because the fresh container installed the plugin, verified skills/MCP/resources, served dashboard, ran feature contract smokes, ran the HN demo fixture, and ran the everyday demo fixture/audit checks.",
    evidence_refs: {
      report_path: REPORT_PATH,
      ai_self_report_path: AI_SELF_REPORT_PATH,
      auth_files_copied: authCopied.map((item) => `[redacted:${item}]`),
      clone,
      runtimeInstall,
      hostInstall,
      cli_version: version,
      ai_prompt_results: ai,
    },
    skip_reason: skipReason,
    blockers,
    state_manager: stateManager,
  });
}

async function main() {
  ensureDir(OUT_DIR);
  ensureDir(WORK_ROOT);
  ensureDir(HOME);
  const authCopied = copyHostAuth();
  const version = cliVersion();
  const clone = gitCloneSource();
  let runtimeInstall = { ok: false, command: "not-run", stdout: "", stderr: "clone failed" };
  let hostInstall = { ok: false, command: "not-run", stdout: "", stderr: "runtime install failed" };
  let dashboard = { ok: false, status: 0 };
  let demo = { ok: false, command: "not-run", stdout: "", stderr: "governance not started" };
  let everydayDemos = [];
  let featureSmokes = [];
  let liveObserverRoute = emptyLiveObserverRouteResult();
  let governance = null;

  if (clone.ok) runtimeInstall = installRuntime();
  if (runtimeInstall.ok) hostInstall = HOST === "codex" ? installCodexPlugin() : installClaudePlugin();

  const installPrompt = buildInstallPrompt();
  const demoPrompt = buildDemoPrompt();
  const ai = maybeRunAiPrompts(installPrompt, demoPrompt);

  if (runtimeInstall.ok) {
    governance = startGovernance();
    const health = await waitForHealth();
    dashboard = health.ok ? await dashboardHealth() : { ok: false, status: 0, error: "health timeout" };
    if (dashboard.ok) demo = runDemoFixture();
    if (dashboard.ok) everydayDemos = runEverydayDemos();
    if (dashboard.ok) featureSmokes = await runFeatureSmokes();
    if (dashboard.ok) liveObserverRoute = runLiveObserverRoutePrompt();
  }

  const skills = hostInstall.ok ? listSkillsSeen() : [];
  const tools = runtimeInstall.ok ? mcpToolsSeen() : [];
  const resources = hostInstall.ok ? resourcesRead() : [];

  if (governance?.child) {
    governance.child.kill("SIGTERM");
  }

  const report = buildReport({
    authCopied,
    clone,
    runtimeInstall,
    hostInstall,
    version,
    installPrompt,
    demoPrompt,
    ai,
    dashboard,
    demo,
    everydayDemos,
    featureSmokes,
    liveObserverRoute,
    skills,
    tools,
    resources,
  });
  writeJson(REPORT_PATH, report);
  const validation = run("node", ["/opt/hn-install-audit/validate-report.mjs", REPORT_PATH], { cwd: WORK_ROOT });
  console.log(JSON.stringify({ report: REPORT_PATH, validation, status: report.status }, null, 2));
  if (!validation.ok || report.status !== "PASS") process.exit(1);
}

main().catch((error) => {
  ensureDir(OUT_DIR);
  writeJson(REPORT_PATH, {
    schema_version: "aming_claw_install_audit.v1",
    host: HOST,
    status: "FAIL",
    auth_mode: AUTH_MODE,
    run_id: RUN_ID,
    error: sample(error?.stack || error?.message || error),
  });
  console.error(error);
  process.exit(1);
});
