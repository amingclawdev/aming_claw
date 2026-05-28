#!/usr/bin/env node
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { spawn, spawnSync } from "node:child_process";
import crypto from "node:crypto";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(SCRIPT_DIR, "..");
const DEFAULT_REGISTRY = join(SCRIPT_DIR, "test-scenarios.json");
const DEFAULT_STATE_DIR = join(tmpdir(), "aming-claw-test-scenario-manager", "state");
const DEFAULT_GOVERNANCE_URL = "http://127.0.0.1:40000";
const VALID_MODES = new Set(["doctor", "plan", "run", "report"]);
const RUN_TERMINAL_STATUSES = new Set(["passed", "failed", "blocked", "dry_run"]);

function nowIso() {
  return new Date().toISOString();
}

function compactTimestamp(iso) {
  return iso.replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
}

function parseArgs(argv) {
  const options = {
    json: false,
    dryRun: false,
    allowNetwork: false,
    allowBootstrap: false,
    stateDir: DEFAULT_STATE_DIR,
    cacheDir: "",
    registry: DEFAULT_REGISTRY,
    governanceUrl: DEFAULT_GOVERNANCE_URL,
    scenario: "",
    runId: "",
    timeoutMs: 0,
  };
  const positional = [];

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith("--")) {
      positional.push(arg);
      continue;
    }
    const [rawKey, inlineValue] = arg.slice(2).split(/=(.*)/s, 2);
    const key = rawKey.replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
    const boolKeys = new Set(["json", "dryRun", "allowNetwork", "allowBootstrap"]);
    if (boolKeys.has(key)) {
      options[key] = inlineValue === undefined ? true : inlineValue !== "false";
      continue;
    }
    const value = inlineValue !== undefined ? inlineValue : argv[++i];
    if (!value || value.startsWith("--")) {
      throw new Error(`--${rawKey} requires a value`);
    }
    if (key === "stateDir") options.stateDir = resolve(value);
    else if (key === "cacheDir") options.cacheDir = resolve(value);
    else if (key === "registry") options.registry = resolve(value);
    else if (key === "backend") options.governanceUrl = value.replace(/\/+$/, "");
    else if (key === "governanceUrl") options.governanceUrl = value.replace(/\/+$/, "");
    else if (key === "scenario") options.scenario = value;
    else if (key === "runId") options.runId = value;
    else if (key === "timeoutMs") options.timeoutMs = Number(value) || 0;
    else options[key] = value;
  }

  const mode = positional[0] || "doctor";
  if (!VALID_MODES.has(mode)) {
    throw new Error(`unknown mode ${mode}; expected one of ${Array.from(VALID_MODES).join(", ")}`);
  }
  if (!options.scenario && positional[1]) {
    options.scenario = positional[1];
  }
  options.stateDir = resolve(options.stateDir);
  options.cacheDir = resolve(options.cacheDir || join(options.stateDir, "workspaces"));
  options.registry = resolve(options.registry);
  return { mode, options };
}

function readJsonFile(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

function loadRegistry(registryPath) {
  const raw = readJsonFile(registryPath);
  if (raw.schema_version !== 1) {
    throw new Error(`unsupported registry schema_version ${raw.schema_version}`);
  }
  if (!Array.isArray(raw.scenarios)) {
    throw new Error("registry.scenarios must be an array");
  }
  const seen = new Set();
  const scenarios = raw.scenarios.map((scenario, index) => normalizeScenario(scenario, index));
  for (const scenario of scenarios) {
    if (seen.has(scenario.id)) throw new Error(`duplicate scenario id ${scenario.id}`);
    seen.add(scenario.id);
  }
  return {
    schema_version: raw.schema_version,
    registry_path: registryPath,
    scenario_count: scenarios.length,
    scenarios,
    byId: new Map(scenarios.map((scenario) => [scenario.id, scenario])),
  };
}

function normalizeScenario(scenario, index) {
  if (!scenario || typeof scenario !== "object") {
    throw new Error(`scenario at index ${index} must be an object`);
  }
  if (!scenario.id || typeof scenario.id !== "string") {
    throw new Error(`scenario at index ${index} requires a string id`);
  }
  const runner = String(scenario.runner || "");
  if (!runner) throw new Error(`scenario ${scenario.id} requires runner`);
  if (!["commands", "ruby_graph"].includes(runner)) {
    throw new Error(`scenario ${scenario.id} has unsupported runner ${runner}`);
  }
  if (runner === "commands") {
    if (!Array.isArray(scenario.commands) || !scenario.commands.length) {
      throw new Error(`commands scenario ${scenario.id} requires commands`);
    }
    for (const command of scenario.commands) {
      if (!command.id || !Array.isArray(command.command) || !command.command.length) {
        throw new Error(`scenario ${scenario.id} has invalid command entry`);
      }
    }
  }
  if (runner === "ruby_graph") {
    const commit = scenario.repository?.commit || scenario.repository?.expected_commit || scenario.repository?.ref || "";
    if (!scenario.repository?.url || !commit || !scenario.repository?.workspace_name) {
      throw new Error(`ruby_graph scenario ${scenario.id} requires repository url, commit, and workspace_name`);
    }
    const validation = scenario.validation || scenario.graph_expectations || {};
    if (!validation.required_path) {
      throw new Error(`ruby_graph scenario ${scenario.id} requires validation.required_path`);
    }
  }
  const repository = scenario.repository
    ? {
        ...scenario.repository,
        commit: scenario.repository.commit || scenario.repository.expected_commit || scenario.repository.ref || "",
        ref: scenario.repository.ref || scenario.repository.commit || scenario.repository.expected_commit || "HEAD",
      }
    : undefined;
  const validation = scenario.validation || scenario.graph_expectations || undefined;
  return {
    title: scenario.title || scenario.id,
    description: scenario.description || "",
    project_id: scenario.project_id || scenario.target_project || "",
    target_project: scenario.target_project || scenario.project_id || "",
    target_ref: scenario.target_ref || repository?.commit || repository?.ref || "HEAD",
    dependencies: Array.isArray(scenario.dependencies) ? scenario.dependencies : [],
    commands: Array.isArray(scenario.commands) ? scenario.commands : [],
    artifacts: Array.isArray(scenario.artifacts) ? scenario.artifacts : [],
    ...scenario,
    repository,
    validation,
  };
}

function selectScenarios(registry, scenarioId) {
  if (!scenarioId) return registry.scenarios;
  const scenario = registry.byId.get(scenarioId);
  if (!scenario) {
    throw new Error(`unknown scenario ${scenarioId}`);
  }
  return [scenario];
}

function expandToken(value) {
  if (value === "{python}") return process.env.PYTHON || process.env.PYTHON_BIN || "python";
  if (value === "{node}") return process.execPath;
  return value;
}

function expandCommand(command) {
  return command.map((part) => expandToken(String(part)));
}

function resolveCwd(cwd, externalWorkspace = "") {
  if (!cwd || cwd === "repo") return REPO_ROOT;
  if (cwd === "external_workspace") return externalWorkspace || REPO_ROOT;
  return isAbsolute(cwd) ? cwd : resolve(REPO_ROOT, cwd);
}

function commandAvailable(binary) {
  const resolved = expandToken(String(binary || ""));
  if (!resolved) return false;
  if (resolved.includes("/") || resolved.includes("\\")) return existsSync(resolved);
  const result = spawnSync(resolved, ["--version"], {
    encoding: "utf8",
    timeout: 3000,
    stdio: "pipe",
  });
  return result.error?.code !== "ENOENT";
}

function sanitizeText(value) {
  return String(value || "")
    .replace(/\b(Bearer\s+)[A-Za-z0-9._~+/=-]+/gi, "$1[REDACTED]")
    .replace(/([?&](?:token|key|secret|password|api_key|session)[^=]*=)[^&\s]+/gi, "$1[REDACTED]")
    .replace(/\b(token|secret|password|api[_-]?key|session[_-]?token)(["':=\s]+)[^\s"',}]+/gi, "$1$2[REDACTED]");
}

function sanitizeArg(value) {
  const text = sanitizeText(value);
  if (/^--?(token|secret|password|api[-_]?key|session[-_]?token)(=|$)/i.test(text)) {
    const [key] = text.split("=", 1);
    return `${key}=[REDACTED]`;
  }
  return text;
}

function sanitizeCommand(command) {
  return command.map((part) => sanitizeArg(String(part)));
}

function sanitizeEnv(env) {
  if (!env || typeof env !== "object" || Array.isArray(env)) return {};
  return Object.fromEntries(
    Object.entries(env).map(([key, value]) => [String(key), sanitizeText(String(value))]),
  );
}

function sanitizeUrl(url) {
  try {
    const parsed = new URL(url);
    parsed.search = "";
    return parsed.toString();
  } catch {
    return sanitizeText(url);
  }
}

function normalizeProjectId(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function tailText(text, maxChars = 12000) {
  const sanitized = sanitizeText(text);
  if (sanitized.length <= maxChars) return sanitized;
  return sanitized.slice(sanitized.length - maxChars);
}

function isInside(parent, child) {
  const rel = relative(resolve(parent), resolve(child));
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

function assertExternalCacheDir(cacheDir) {
  if (isInside(REPO_ROOT, cacheDir)) {
    throw new Error(`refusing to use cache dir inside repo: ${cacheDir}`);
  }
}

function gitOutput(cwd, args) {
  const result = spawnSync("git", args, { cwd, encoding: "utf8", timeout: 10000, stdio: "pipe" });
  if (result.status !== 0) return "";
  return result.stdout.trim();
}

function commandVersion(binary) {
  const resolved = expandToken(String(binary || ""));
  if (!resolved) return "";
  if (resolved === process.execPath) return process.version;
  const result = spawnSync(resolved, ["--version"], {
    encoding: "utf8",
    timeout: 3000,
    stdio: "pipe",
  });
  if (result.error || result.status !== 0) return "";
  return sanitizeText(`${result.stdout || result.stderr}`.trim().split(/\r?\n/, 1)[0] || "");
}

function buildDependencyDecisions(scenario, options, { planning = false } = {}) {
  const decisions = [];
  for (const dependency of scenario.dependencies) {
    const decision = {
      id: dependency.id || "",
      kind: dependency.kind || "",
      required: dependency.required ?? true,
      status: "not_checked",
      remediation: dependency.remediation || "",
    };
    if (planning || options.dryRun) {
      decision.status = options.dryRun ? "skipped_by_dry_run" : "planned";
    } else if (dependency.kind === "command") {
      const available = commandAvailable(dependency.command);
      decision.status = available ? "available" : "blocked";
      decision.command = sanitizeArg(expandToken(dependency.command || ""));
      if (!available && !decision.remediation) {
        decision.remediation = `Install ${dependency.command} or adjust the scenario registry.`;
      }
    } else if (dependency.id === "network") {
      decision.status = options.allowNetwork ? "allowed" : "blocked";
      if (!options.allowNetwork) decision.reason = "network operations require --allow-network";
    } else if (dependency.id === "governance_bootstrap") {
      decision.status = options.allowBootstrap ? "allowed" : "blocked";
      if (!options.allowBootstrap) decision.reason = "bootstrap mutates governance state and requires --allow-bootstrap";
    } else {
      decision.status = "planned";
    }
    decisions.push(decision);
  }
  return decisions;
}

function planScenario(scenario, options) {
  const plan = {
    scenario_id: scenario.id,
    title: scenario.title,
    target_project: scenario.target_project || "",
    target_ref: scenario.target_ref || "",
    runner: scenario.runner,
    dependency_decisions: buildDependencyDecisions(scenario, options, { planning: true }),
    artifacts: scenario.artifacts || [],
  };
  if (scenario.runner === "commands") {
    plan.commands = scenario.commands.map((command) => ({
      id: command.id,
      cwd: command.cwd || "repo",
      command: sanitizeCommand(expandCommand(command.command)),
      env: sanitizeEnv(command.env),
      timeout_ms: command.timeout_ms || 0,
    }));
  }
  if (scenario.runner === "ruby_graph") {
    const workspace = join(options.cacheDir, scenario.repository.workspace_name);
    plan.repository = {
      url: scenario.repository.url,
      ref: scenario.repository.ref,
      commit: scenario.repository.commit || "",
      workspace_path: workspace,
    };
    plan.bootstrap = {
      governance_url: options.governanceUrl,
      requires_allow_network_when_missing: true,
      requires_allow_bootstrap: true,
      project_name: scenario.bootstrap?.project_name || scenario.target_project || scenario.id,
      scan_depth: scenario.bootstrap?.scan_depth || 3,
    };
    plan.validation = scenario.validation;
  }
  return plan;
}

function runIdFor(scenarioId, startedAt) {
  const seed = `${scenarioId}:${startedAt}:${process.pid}`;
  const suffix = crypto.createHash("sha1").update(seed).digest("hex").slice(0, 8);
  return `${compactTimestamp(startedAt)}-${scenarioId}-${suffix}`;
}

function baseReport(scenario, options) {
  const startedAt = nowIso();
  return {
    schema_version: 1,
    run_id: runIdFor(scenario.id, startedAt),
    scenario_id: scenario.id,
    scenario_title: scenario.title,
    target_project: scenario.target_project || "",
    target_ref: scenario.target_ref || "",
    target_commit: "",
    status: "running",
    started_at: startedAt,
    completed_at: "",
    duration_ms: 0,
    dependency_decisions: [],
    artifacts: [],
    command_summaries: [],
    http_summaries: [],
    checks: [],
    blocked: null,
    report_path: "",
    options: {
      dry_run: Boolean(options.dryRun),
      allow_network: Boolean(options.allowNetwork),
      allow_bootstrap: Boolean(options.allowBootstrap),
      governance_url: options.governanceUrl,
      backend_url: options.governanceUrl,
      state_dir: options.stateDir,
      cache_dir: options.cacheDir,
    },
  };
}

function finishReport(report, status) {
  report.status = status;
  report.completed_at = nowIso();
  report.duration_ms = new Date(report.completed_at).getTime() - new Date(report.started_at).getTime();
  return report;
}

function blockedReport(report, reasonCode, reason, remediation, extra = {}) {
  report.blocked = {
    reason_code: reasonCode,
    reason: sanitizeText(reason),
    remediation: sanitizeText(remediation),
  };
  if (extra.checks) report.checks.push(...extra.checks);
  return finishReport(report, "blocked");
}

function statePath(stateDir) {
  return join(stateDir, "state.json");
}

function reportsDir(stateDir) {
  return join(stateDir, "reports");
}

function readState(stateDir) {
  const path = statePath(stateDir);
  if (!existsSync(path)) {
    return {
      schema_version: 1,
      created_at: nowIso(),
      updated_at: "",
      last_run_id: "",
      scenarios: {},
      runs: [],
    };
  }
  return readJsonFile(path);
}

function writeRunState(options, report) {
  mkdirSync(reportsDir(options.stateDir), { recursive: true });
  const reportPath = join(reportsDir(options.stateDir), `${report.run_id}.json`);
  report.report_path = reportPath;
  if (!report.artifacts.some((artifact) => artifact.kind === "report" && artifact.path === reportPath)) {
    report.artifacts.push({ kind: "report", path: reportPath });
  }
  writeFileSync(reportPath, JSON.stringify(report, null, 2) + "\n", "utf8");

  const state = readState(options.stateDir);
  state.updated_at = report.completed_at;
  state.last_run_id = report.run_id;
  state.scenarios[report.scenario_id] = {
    scenario_id: report.scenario_id,
    target_project: report.target_project,
    target_ref: report.target_ref,
    target_commit: report.target_commit,
    status: report.status,
    last_status: report.status,
    last_run_id: report.run_id,
    started_at: report.started_at,
    completed_at: report.completed_at,
    last_started_at: report.started_at,
    last_completed_at: report.completed_at,
    timestamps: {
      started_at: report.started_at,
      completed_at: report.completed_at,
    },
    dependency_decisions: report.dependency_decisions,
    artifacts: report.artifacts,
    report_path: reportPath,
  };
  state.runs = Array.isArray(state.runs) ? state.runs : [];
  state.runs.push({
    run_id: report.run_id,
    scenario_id: report.scenario_id,
    status: report.status,
    started_at: report.started_at,
    completed_at: report.completed_at,
    report_path: reportPath,
  });
  writeFileSync(statePath(options.stateDir), JSON.stringify(state, null, 2) + "\n", "utf8");
  return { state, report };
}

async function runCommand(commandSpec, options, report, externalWorkspace = "") {
  const command = expandCommand(commandSpec.command);
  const cwd = resolveCwd(commandSpec.cwd || "repo", externalWorkspace);
  const summary = {
    id: commandSpec.id,
    cwd,
    command: sanitizeCommand(command),
    timeout_ms: options.timeoutMs || commandSpec.timeout_ms || 120000,
    status: "running",
    skipped: false,
    exit_code: null,
    signal: "",
    duration_ms: 0,
    env: sanitizeEnv(commandSpec.env),
    stdout_tail: "",
    stderr_tail: "",
  };
  if (options.dryRun) {
    summary.status = "skipped";
    summary.skipped = true;
    report.command_summaries.push(summary);
    return summary;
  }

  const started = Date.now();
  await new Promise((resolvePromise) => {
    let stdout = "";
    let stderr = "";
    const child = spawn(command[0], command.slice(1), {
      cwd,
      stdio: ["ignore", "pipe", "pipe"],
      env: { ...process.env, ...(commandSpec.env || {}) },
    });
    const timeout = setTimeout(() => {
      summary.status = "timed_out";
      child.kill("SIGTERM");
    }, summary.timeout_ms);
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString("utf8");
      if (stdout.length > 80000) stdout = stdout.slice(stdout.length - 80000);
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString("utf8");
      if (stderr.length > 80000) stderr = stderr.slice(stderr.length - 80000);
    });
    child.on("error", (error) => {
      clearTimeout(timeout);
      summary.status = "failed";
      summary.stderr_tail = sanitizeText(error.message);
      summary.duration_ms = Date.now() - started;
      resolvePromise();
    });
    child.on("close", (code, signal) => {
      clearTimeout(timeout);
      summary.exit_code = code;
      summary.signal = signal || "";
      if (summary.status !== "timed_out") summary.status = code === 0 ? "passed" : "failed";
      summary.duration_ms = Date.now() - started;
      summary.stdout_tail = tailText(stdout);
      summary.stderr_tail = tailText(stderr);
      resolvePromise();
    });
  });
  report.command_summaries.push(summary);
  return summary;
}

async function fetchJson(method, url, body, report, timeoutMs = 30000) {
  const summary = {
    method,
    url: sanitizeUrl(url),
    status_code: null,
    ok: false,
    duration_ms: 0,
    response_keys: [],
    error: "",
  };
  const started = Date.now();
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, {
      method,
      headers: body === undefined ? {} : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    const text = await response.text();
    let json = null;
    try {
      json = text ? JSON.parse(text) : null;
    } catch {
      json = null;
    }
    summary.status_code = response.status;
    summary.ok = response.ok;
    summary.response_keys = json && typeof json === "object" ? Object.keys(json).slice(0, 20) : [];
    if (!response.ok) {
      summary.error = sanitizeText(json?.error || text || response.statusText);
    }
    return { ok: response.ok, status: response.status, json, text, summary };
  } catch (error) {
    summary.error = sanitizeText(error.message || String(error));
    return { ok: false, status: 0, json: null, text: "", error: summary.error, summary };
  } finally {
    clearTimeout(timeout);
    summary.duration_ms = Date.now() - started;
    report.http_summaries.push(summary);
  }
}

async function probeBackendHealth(options, timeoutMs = 1000) {
  const holder = { http_summaries: [] };
  const health = await fetchJson("GET", `${options.governanceUrl}/api/health`, undefined, holder, timeoutMs);
  const summary = holder.http_summaries[0] || {
    method: "GET",
    url: sanitizeUrl(`${options.governanceUrl}/api/health`),
    status_code: 0,
    ok: false,
    duration_ms: 0,
    response_keys: [],
    error: "unknown backend probe failure",
  };
  return {
    ok: Boolean(health.ok),
    reachable: Boolean(health.status),
    url: sanitizeUrl(`${options.governanceUrl}/api/health`),
    status_code: health.status || 0,
    response_keys: summary.response_keys || [],
    error: summary.error || "",
    summary,
  };
}

async function runCommandsScenario(scenario, options) {
  const report = baseReport(scenario, options);
  report.target_commit = gitOutput(REPO_ROOT, ["rev-parse", scenario.target_ref || "HEAD"]);
  report.dependency_decisions = buildDependencyDecisions(scenario, options);
  if (!options.dryRun) {
    const blocked = report.dependency_decisions.find((decision) => decision.status === "blocked" && decision.required !== false);
    if (blocked) {
      return blockedReport(
        report,
        `dependency_${blocked.id || "unknown"}_blocked`,
        blocked.reason || `Required dependency ${blocked.id} is unavailable.`,
        blocked.remediation || "Install the dependency and re-run the scenario.",
      );
    }
  }
  for (const command of scenario.commands) {
    const result = await runCommand(command, options, report);
    if (!options.dryRun && result.status !== "passed") {
      return finishReport(report, "failed");
    }
  }
  return finishReport(report, options.dryRun ? "dry_run" : "passed");
}

async function ensureSinatraWorkspace(scenario, options, report) {
  assertExternalCacheDir(options.cacheDir);
  mkdirSync(options.cacheDir, { recursive: true });
  const repo = scenario.repository;
  const workspace = join(options.cacheDir, repo.workspace_name);
  const expected = repo.commit || repo.expected_commit || repo.ref || "";
  if (!existsSync(workspace)) {
    if (!options.allowNetwork) {
      return {
        ok: false,
        blocked: blockedReport(
          report,
          "network_clone_not_allowed",
          `Pinned Sinatra workspace is missing at ${workspace}.`,
          "Re-run with --allow-network or pre-populate that cache path with the pinned Sinatra ref.",
        ),
      };
    }
    mkdirSync(workspace, { recursive: true });
    const init = await runCommand(
      {
        id: "init_sinatra_workspace",
        cwd: workspace,
        command: ["git", "init"],
        timeout_ms: 30000,
      },
      options,
      report,
    );
    const remote = init.status === "passed"
      ? await runCommand(
          {
            id: "add_sinatra_remote",
            cwd: workspace,
            command: ["git", "remote", "add", "origin", repo.url],
            timeout_ms: 30000,
          },
          options,
          report,
        )
      : init;
    const fetch = remote.status === "passed"
      ? await runCommand(
          {
            id: "fetch_sinatra_commit",
            cwd: workspace,
            command: ["git", "fetch", "--depth", "1", "origin", expected],
            timeout_ms: 180000,
          },
          options,
          report,
        )
      : remote;
    const checkout = fetch.status === "passed"
      ? await runCommand(
          {
            id: "checkout_sinatra_commit",
            cwd: workspace,
            command: ["git", "checkout", "--detach", expected],
            timeout_ms: 30000,
          },
          options,
          report,
        )
      : fetch;
    if (checkout.status !== "passed") {
      return {
        ok: false,
        blocked: blockedReport(
          report,
          "sinatra_clone_failed",
          `git fetch/checkout failed for ${repo.url} at ${expected}.`,
          "Check network access and the pinned Sinatra commit, then re-run with --allow-network.",
        ),
      };
    }
  } else if (options.allowNetwork) {
    const current = gitOutput(workspace, ["rev-parse", "HEAD"]);
    if (expected && current !== expected) {
      const fetch = await runCommand(
        {
          id: "fetch_sinatra_commit",
          cwd: workspace,
          command: ["git", "fetch", "--depth", "1", "origin", expected],
          timeout_ms: 180000,
        },
        options,
        report,
      );
      if (fetch.status === "passed") {
        await runCommand(
          {
            id: "checkout_sinatra_commit",
            cwd: workspace,
            command: ["git", "checkout", "--detach", expected],
            timeout_ms: 30000,
          },
          options,
          report,
        );
      }
    }
  }
  if (!existsSync(join(workspace, ".git"))) {
    return {
      ok: false,
      blocked: blockedReport(
        report,
        "cache_workspace_not_git",
        `Cache workspace is not a git checkout: ${workspace}.`,
        "Remove or replace that cache directory with a git checkout of the pinned Sinatra ref.",
      ),
    };
  }
  const dirty = gitOutput(workspace, ["status", "--porcelain"]);
  if (dirty) {
    return {
      ok: false,
      blocked: blockedReport(
        report,
        "cache_workspace_dirty",
        `Cached Sinatra workspace has local changes at ${workspace}.`,
        "Use a clean cache checkout or choose a different --cache-dir; the manager will not reset cached workspaces.",
      ),
    };
  }
  const commit = gitOutput(workspace, ["rev-parse", "HEAD"]);
  if (expected && commit !== expected) {
    return {
      ok: false,
      blocked: blockedReport(
        report,
        "cache_workspace_wrong_commit",
        `Cached Sinatra workspace is at ${commit || "unknown"}, expected ${expected}.`,
        "Use a cache checkout at the pinned ref or remove the stale cache and re-run with --allow-network.",
      ),
    };
  }
  return { ok: true, workspace, commit };
}

function layerNodeCount(layerResponse) {
  const layers = layerResponse?.result?.layers;
  if (!Array.isArray(layers)) return 0;
  return layers.reduce((total, layer) => total + (Number(layer.count) || 0), 0);
}

function responseMatches(response) {
  return Array.isArray(response?.result?.matches) ? response.result.matches : [];
}

function evidenceContainsRuby(value) {
  const serialized = JSON.stringify(value || {}).toLowerCase();
  return serialized.includes('"language":"ruby"') || serialized.includes('"language": "ruby"');
}

function anyPrimaryRubyFile(matches) {
  return matches.some((match) => {
    const files = [
      ...(Array.isArray(match?.node?.primary_files) ? match.node.primary_files : []),
      match?.primary_file,
      match?.path,
    ].filter(Boolean);
    return files.some((file) => String(file).endsWith(".rb"));
  });
}

async function runRubyGraphScenario(scenario, options) {
  const report = baseReport(scenario, options);
  report.target_project = scenario.project_id || scenario.target_project || scenario.id;
  report.target_commit = scenario.repository?.commit || scenario.repository?.expected_commit || "";
  report.dependency_decisions = buildDependencyDecisions(scenario, options);
  const gitDecision = report.dependency_decisions.find((decision) => decision.id === "git");
  if (!options.dryRun && gitDecision?.status === "blocked") {
    return blockedReport(
      report,
      "dependency_git_blocked",
      "git is unavailable.",
      gitDecision.remediation || "Install git and re-run the scenario.",
    );
  }
  if (options.dryRun) {
    return finishReport(report, "dry_run");
  }

  const backend = await probeBackendHealth(options, 1000);
  report.http_summaries.push(backend.summary);
  if (!backend.ok) {
    return blockedReport(
      report,
      "governance_unreachable",
      `Governance did not respond at ${options.governanceUrl}.`,
      "Start governance with `aming-claw start` or pass --backend to a healthy governance URL.",
    );
  }

  if (!options.allowBootstrap) {
    return blockedReport(
      report,
      "governance_bootstrap_not_allowed",
      "Governance bootstrap mutates the project registry and graph state.",
      "Re-run with --allow-bootstrap after confirming governance state may be updated.",
    );
  }

  const prepared = await ensureSinatraWorkspace(scenario, options, report);
  if (!prepared.ok) return prepared.blocked;
  report.target_commit = prepared.commit;
  const requestedProjectId = normalizeProjectId(
    scenario.project_id || scenario.bootstrap?.project_name || scenario.target_project || scenario.id,
  );
  report.target_project = requestedProjectId;
  report.artifacts.push({ kind: "workspace", path: prepared.workspace });

  const configOverride = {
    ...(scenario.bootstrap?.config_override || {}),
    project_id: requestedProjectId,
    language: scenario.validation?.required_language || "ruby",
  };
  const bootstrapBody = {
    workspace_path: prepared.workspace,
    project_id: requestedProjectId,
    project_name: requestedProjectId,
    scan_depth: scenario.bootstrap?.scan_depth || 8,
    config_override: configOverride,
  };
  const bootstrap = await fetchJson("POST", `${options.governanceUrl}/api/project/bootstrap`, bootstrapBody, report, 240000);
  if (!bootstrap.ok || !bootstrap.json) {
    return blockedReport(
      report,
      "bootstrap_failed",
      bootstrap.summary?.error || bootstrap.error || "Sinatra bootstrap failed.",
      "Check the governance bootstrap response, ensure the cached workspace is clean, and re-run.",
    );
  }
  const projectId = normalizeProjectId(bootstrap.json.project_id || bootstrapBody.project_name || report.target_project);
  if (projectId !== requestedProjectId) {
    return blockedReport(
      report,
      "bootstrap_project_id_mismatch",
      `Bootstrap returned project_id ${projectId || "unknown"} instead of ${requestedProjectId}.`,
      "Do not trust this graph evidence. Fix the bootstrap project_id override before re-running.",
    );
  }
  report.target_project = projectId;

  const status = await fetchJson("GET", `${options.governanceUrl}/api/graph-governance/${encodeURIComponent(projectId)}/status`, undefined, report, 60000);
  if (!status.ok || !status.json?.active_snapshot_id) {
    return blockedReport(
      report,
      "graph_status_unavailable",
      "Bootstrapped Sinatra project has no active graph status response.",
      "Open the dashboard project entry or re-run bootstrap after governance is healthy.",
    );
  }
  report.checks.push({ id: "active_snapshot", status: "passed", value: status.json.active_snapshot_id });

  const queryBase = `${options.governanceUrl}/api/graph-governance/${encodeURIComponent(projectId)}/query`;
  const queryBody = (tool, args) => ({
    tool,
    args,
    query_source: "observer",
    query_purpose: "prompt_context_build",
  });
  const layers = await fetchJson("POST", queryBase, queryBody("list_layers", {}), report, 60000);
  const nodeCount = layerNodeCount(layers.json);
  if (!layers.ok || nodeCount <= 0) {
    return blockedReport(
      report,
      "graph_node_count_zero",
      "Sinatra graph query returned no materialized graph nodes.",
      "Re-run bootstrap with governance logs visible and inspect the graph build failure.",
    );
  }
  report.checks.push({ id: "nonzero_nodes", status: "passed", value: nodeCount });

  const requiredPath = scenario.validation.required_path;
  const pathQuery = await fetchJson("POST", queryBase, queryBody("find_node_by_path", { path: requiredPath }), report, 60000);
  const pathMatches = responseMatches(pathQuery.json);
  if (!pathQuery.ok || !pathMatches.length) {
    return blockedReport(
      report,
      "required_ruby_file_not_resolved",
      `${requiredPath} was not resolvable in the Sinatra graph.`,
      "Inspect the graph snapshot files and Ruby adapter classification, then re-run bootstrap.",
    );
  }
  const rubyEvidence = evidenceContainsRuby(pathMatches) || anyPrimaryRubyFile(pathMatches);
  if (!rubyEvidence) {
    return blockedReport(
      report,
      "required_language_not_ruby",
      `${requiredPath} resolved, but the graph response did not show Ruby language evidence.`,
      "Inspect Ruby language adapter metadata for the Sinatra snapshot.",
    );
  }
  report.checks.push({ id: "required_path_resolved", status: "passed", value: requiredPath });
  report.checks.push({ id: "language_ruby", status: "passed", value: "ruby" });

  const queries = Array.isArray(scenario.validation.function_index_queries)
    ? scenario.validation.function_index_queries
    : ["Sinatra::Base"];
  const functionMatches = [];
  for (const query of queries) {
    const response = await fetchJson("POST", queryBase, queryBody("function_index", { query, limit: 20 }), report, 60000);
    if (response.ok) {
      functionMatches.push(...responseMatches(response.json));
    }
  }
  const rubyFunctionMatches = functionMatches.filter((match) => anyPrimaryRubyFile([match]) || String(match.function || "").includes("::"));
  if (!rubyFunctionMatches.length) {
    return blockedReport(
      report,
      "ruby_function_index_empty",
      "function_index did not return Ruby symbols or methods for Sinatra.",
      "Inspect Ruby symbol extraction and graph function_lines for the bootstrapped Sinatra snapshot.",
    );
  }
  report.checks.push({
    id: "ruby_function_index",
    status: "passed",
    value: rubyFunctionMatches.slice(0, 10).map((match) => match.function || match.short_name || match.primary_file),
  });

  return finishReport(report, "passed");
}

async function runScenario(scenario, options) {
  if (scenario.runner === "commands") return runCommandsScenario(scenario, options);
  if (scenario.runner === "ruby_graph") return runRubyGraphScenario(scenario, options);
  throw new Error(`unsupported runner ${scenario.runner}`);
}

async function doctor(registry, options) {
  const scenarioPlans = registry.scenarios.map((scenario) => planScenario(scenario, options));
  const tools = [
    {
      id: "node",
      command: process.execPath,
      available: true,
      version: process.version,
      required: true,
    },
    {
      id: "python",
      command: sanitizeArg(expandToken("{python}")),
      available: commandAvailable("{python}"),
      version: commandVersion("{python}"),
      required: true,
    },
    {
      id: "git",
      command: "git",
      available: commandAvailable("git"),
      version: commandVersion("git"),
      required: true,
    },
  ];
  const backend = await probeBackendHealth(options, 1000);
  const blockers = [];
  for (const tool of tools) {
    if (tool.required && !tool.available) {
      blockers.push({
        reason_code: `${tool.id}_unavailable`,
        remediation: `Install ${tool.id} or configure the matching binary before running scenarios.`,
      });
    }
  }
  if (!backend.ok) {
    blockers.push({
      reason_code: "backend_unreachable",
      remediation: "Start governance with `aming-claw start` or pass --backend to a healthy governance URL.",
      detail: backend.error || `HTTP ${backend.status_code}`,
    });
  }
  return {
    ok: blockers.length === 0,
    mode: "doctor",
    blockers,
    tools,
    backend,
    registry: {
      path: registry.registry_path,
      schema_version: registry.schema_version,
      scenario_count: registry.scenario_count,
      scenario_ids: registry.scenarios.map((scenario) => scenario.id),
    },
    paths: {
      repo_root: REPO_ROOT,
      state_dir: options.stateDir,
      cache_dir: options.cacheDir,
      cache_inside_repo: isInside(REPO_ROOT, options.cacheDir),
    },
    scenarios: scenarioPlans,
  };
}

function plan(registry, options) {
  const selected = selectScenarios(registry, options.scenario);
  return {
    ok: true,
    mode: "plan",
    selected_count: selected.length,
    scenarios: selected.map((scenario) => planScenario(scenario, options)),
  };
}

function readReport(options) {
  const state = readState(options.stateDir);
  const runId = options.runId || state.last_run_id || "";
  let report = null;
  if (runId) {
    const path = join(reportsDir(options.stateDir), `${runId}.json`);
    if (existsSync(path)) report = readJsonFile(path);
  }
  if (!report && options.scenario && state.scenarios?.[options.scenario]?.report_path) {
    const path = state.scenarios[options.scenario].report_path;
    if (existsSync(path)) report = readJsonFile(path);
  }
  return {
    ok: true,
    mode: "report",
    state_path: statePath(options.stateDir),
    state,
    report,
  };
}

function printHuman(result) {
  const lines = [];
  lines.push(`${result.mode || "result"}: ${result.ok === false ? "failed" : "ok"}`);
  if (result.mode === "doctor") {
    lines.push(`registry: ${result.registry.path}`);
    lines.push(`scenarios: ${result.registry.scenario_ids.join(", ")}`);
    lines.push(`state: ${result.paths.state_dir}`);
    lines.push(`cache: ${result.paths.cache_dir}`);
  } else if (result.mode === "plan") {
    for (const scenario of result.scenarios) {
      lines.push(`${scenario.scenario_id}: ${scenario.runner} (${scenario.target_project || "no project"})`);
    }
  } else if (result.mode === "run") {
    for (const report of result.reports) {
      lines.push(`${report.scenario_id}: ${report.status} report=${report.report_path}`);
      if (report.blocked) lines.push(`blocked: ${report.blocked.reason_code} - ${report.blocked.remediation}`);
    }
  } else if (result.mode === "report") {
    lines.push(`state: ${result.state_path}`);
    if (result.report) lines.push(`last report: ${result.report.run_id} ${result.report.status}`);
  } else {
    lines.push(JSON.stringify(result, null, 2));
  }
  process.stdout.write(`${lines.join("\n")}\n`);
}

async function main() {
  const { mode, options } = parseArgs(process.argv.slice(2));
  const registry = loadRegistry(options.registry);
  let result;
  if (mode === "doctor") {
    result = await doctor(registry, options);
  } else if (mode === "plan") {
    result = plan(registry, options);
  } else if (mode === "report") {
    result = readReport(options);
  } else {
    const selected = selectScenarios(registry, options.scenario);
    const reports = [];
    for (const scenario of selected) {
      const report = await runScenario(scenario, options);
      writeRunState(options, report);
      reports.push(report);
    }
    const statuses = reports.map((report) => report.status);
    result = {
      ok: statuses.every((status) => status === "passed" || status === "dry_run"),
      mode: "run",
      reports,
    };
    if (statuses.some((status) => RUN_TERMINAL_STATUSES.has(status) && !["passed", "dry_run"].includes(status))) {
      process.exitCode = 2;
    }
  }

  if (options.json) {
    process.stdout.write(JSON.stringify(result, null, 2) + "\n");
  } else {
    printHuman(result);
  }
}

main().catch((error) => {
  const payload = {
    ok: false,
    error: sanitizeText(error.message || String(error)),
  };
  if (process.argv.includes("--json")) {
    process.stderr.write(JSON.stringify(payload, null, 2) + "\n");
  } else {
    process.stderr.write(`error: ${payload.error}\n`);
  }
  process.exitCode = 1;
});
