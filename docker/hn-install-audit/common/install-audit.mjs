#!/usr/bin/env node

import crypto from "node:crypto";
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
const CLAUDE_CACHE = join(CLAUDE_HOME, "plugins", "cache", "aming-claw-local", "aming-claw", "0.1.0");
const REPORT_PATH = join(OUT_DIR, `${HOST}-install-audit-${RUN_ID}.json`);
const AI_SELF_REPORT_PATH = join(OUT_DIR, `${HOST}-ai-self-report-${RUN_ID}.json`);
const AI_PROMPT_MODE = process.env.AI_PROMPT_MODE || "required"; // required | optional | skip
const PROMPT_TIMEOUT_MS = Number(process.env.AI_PROMPT_TIMEOUT_MS || 20 * 60 * 1000);
const GOVERNANCE_PORT = process.env.GOVERNANCE_PORT || "40000";
const GOVERNANCE_URL = `http://127.0.0.1:${GOVERNANCE_PORT}`;

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
  rmSync(CLAUDE_MARKETPLACE, { recursive: true, force: true });
  rmSync(CLAUDE_CACHE, { recursive: true, force: true });
  ensureDir(dirname(CLAUDE_MARKETPLACE));
  ensureDir(dirname(CLAUDE_CACHE));
  cpSync(SRC_ROOT, CLAUDE_MARKETPLACE, {
    recursive: true,
    filter: (source) => !source.includes(`${SRC_ROOT}/.git`),
  });
  cpSync(SRC_ROOT, CLAUDE_CACHE, {
    recursive: true,
    filter: (source) => !source.includes(`${SRC_ROOT}/.git`),
  });
  writeJson(join(CLAUDE_HOME, "plugins", "installed_plugins.json"), {
    "aming-claw@aming-claw-local": {
      scope: "user",
      installPath: CLAUDE_CACHE,
      version: "0.1.0",
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
  if (HOST === "codex") return join(CODEX_HOME, "plugins", "cache", "aming-claw-local", "aming-claw", "0.1.0", "skills");
  return join(CLAUDE_CACHE, "skills");
}

function listSkillsSeen() {
  const root = installedSkillRoot();
  return REQUIRED_SKILLS.filter((skill) => existsSync(join(root, skill, "SKILL.md")));
}

function resourcesRead() {
  const root = HOST === "codex"
    ? join(CODEX_HOME, "plugins", "cache", "aming-claw-local", "aming-claw", "0.1.0")
    : CLAUDE_CACHE;
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
  skills,
  tools,
  resources,
}) {
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
  const loginRequired = HOST === "claude" && AI_PROMPT_MODE === "required" && (isLoginRequired(ai.install) || isLoginRequired(ai.demo));
  if (loginRequired) blockers.push("Claude AI prompt execution requires login");
  else if (AI_PROMPT_MODE === "required" && (!ai.install.ok || !ai.demo.ok)) blockers.push("AI prompt execution failed");

  const aiSkipped = AI_PROMPT_MODE === "skip";
  const status = loginRequired ? "LOGIN_REQUIRED" : blockers.length ? "FAIL" : aiSkipped ? "SKIPPED" : "PASS";
  const skipReason = aiSkipped
    ? "AI_PROMPT_MODE=skip; deterministic install checks ran, but the one-click AI install and HN demo prompts were not executed."
    : loginRequired
      ? "Claude CLI is installed but not authenticated in the mounted auth home. Run `claude /login` or `claude auth login` with the same auth home, then rerun with --claude-auth-home <dir>."
    : "";
  return {
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
    cache_path: HOST === "codex" ? join(CODEX_HOME, "plugins", "cache", "aming-claw-local", "aming-claw", "0.1.0") : CLAUDE_CACHE,
    fresh_session_id: `${HOST}-docker-${RUN_ID}`,
    skills_seen: skills,
    mcp_tools_seen: tools,
    resources_read: resources,
    dashboard_health: dashboard,
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
        : "Install lane passed because the fresh container installed the plugin, verified skills/MCP/resources, served dashboard, ran the HN demo fixture, and ran the everyday demo fixture/audit checks.",
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
  };
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
