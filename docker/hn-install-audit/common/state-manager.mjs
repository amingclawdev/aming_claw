#!/usr/bin/env node

import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

export const STATE_MANAGER_SCHEMA_VERSION = "docker_ai_e2e_state_manager.v1";
export const LANE_STATE_SCHEMA_VERSION = "docker_ai_e2e_lane_state.v1";
export const PROVIDER_SCHEMA_VERSION = "docker_ai_e2e_provider.v1";

export const LANE_IDS = Object.freeze(["install", "update", "new-feature", "external-project"]);
export const HOST_LANES = Object.freeze(["codex", "claude", "both"]);
export const LANE_STATUSES = Object.freeze([
  "pass",
  "fail",
  "blocked",
  "skipped",
  "reused",
  "login_required",
  "not_run",
]);

export const REASONS = Object.freeze({
  explicitLane: "explicit_lane_requested",
  installImpact: "install_packaging_or_auth_bootstrap_changed",
  updateImpact: "runtime_plugin_or_governance_update_surface_changed",
  featureImpact: "feature_owned_files_changed",
  externalProjectImpact: "external_project_governance_contract_changed",
  noImpact: "no_changed_files_matched_lane_triggers",
  updateNeedsPreviousBaseline: "update_lane_requires_previous_baseline_but_container_is_current",
  updateFromPreviousBaseline: "update_lane_can_upgrade_previous_baseline_to_target",
  featureRequiresCurrentContainer: "new_feature_lane_requires_current_updated_container",
  installReusesAuthFreshRuntime: "install_lane_reuses_auth_but_reinstalls_plugin_runtime",
  authNotReady: "auth_readiness_missing_or_unknown",
  externalProviderBlocked: "external_project_provider_config_incomplete",
  observerPendingExtensionOnly: "observer_command_pending_feature_lane_extension_point_only",
});

const TOKEN_PATTERNS = [
  /sk-[A-Za-z0-9_-]{16,}/g,
  /ghp_[A-Za-z0-9_]{16,}/g,
  /xox[baprs]-[A-Za-z0-9-]{16,}/g,
  /Bearer\s+[A-Za-z0-9._-]{16,}/gi,
  /CLAUDE_CODE_OAUTH_TOKEN\s*=\s*\S+/g,
  /("access_token"\s*:\s*")[^"]+(")/gi,
  /("refresh_token"\s*:\s*")[^"]+(")/gi,
];

const SENSITIVE_KEY_PATTERN = /token|secret|credential|password|authorization|cookie/i;

const DEFAULT_IMPACT_RULES = [
  {
    lane_id: "install",
    reason: REASONS.installImpact,
    paths: [
      "docker/hn-install-audit/**",
      ".codex-plugin/**",
      ".claude-plugin/**",
      "skills/**",
      "agent/plugin_installer.py",
      "agent/cli.py",
      "pyproject.toml",
      "MANIFEST.in",
    ],
  },
  {
    lane_id: "update",
    reason: REASONS.updateImpact,
    paths: [
      "agent/governance/**",
      "agent/mcp/**",
      "scripts/**",
      ".codex-plugin/**",
      ".claude-plugin/**",
      "pyproject.toml",
      "MANIFEST.in",
    ],
  },
  {
    lane_id: "external-project",
    reason: REASONS.externalProjectImpact,
    paths: [
      ".aming-claw.yaml",
      "docs/config/aming-claw-yaml.md",
      "agent/governance/external_project_governance.py",
      "agent/governance/project_service.py",
      "agent/governance/project_profile.py",
    ],
  },
];

function asString(value, fallback = "") {
  const text = String(value ?? "").trim();
  return text || fallback;
}

function asArray(value) {
  if (Array.isArray(value)) return value;
  if (value === undefined || value === null || value === "") return [];
  return [value];
}

function safeBool(value, fallback = false) {
  return typeof value === "boolean" ? value : fallback;
}

function uniqueStrings(values) {
  return [...new Set(asArray(values).flatMap((value) => {
    if (typeof value === "string") return value.split(/[\n,]/);
    return [String(value)];
  }).map((value) => value.trim()).filter(Boolean))];
}

export function redactSecretString(value) {
  let text = String(value ?? "");
  for (const pattern of TOKEN_PATTERNS) {
    text = text.replace(pattern, (...matches) => {
      if (matches.length >= 3 && String(matches[0]).startsWith("\"")) {
        return `${matches[1]}[REDACTED]${matches[2]}`;
      }
      if (/^Bearer\s/i.test(matches[0])) return "Bearer [REDACTED]";
      if (/CLAUDE_CODE_OAUTH_TOKEN/i.test(matches[0])) return "CLAUDE_CODE_OAUTH_TOKEN=[REDACTED]";
      return "[REDACTED_TOKEN]";
    });
  }
  return text;
}

export function sanitizeReportValue(value, path = []) {
  if (typeof value === "string") {
    const key = path[path.length - 1] || "";
    if (SENSITIVE_KEY_PATTERN.test(key) && !/^auth_mode$/.test(key)) return "[REDACTED]";
    return redactSecretString(value);
  }
  if (Array.isArray(value)) return value.map((item, index) => sanitizeReportValue(item, [...path, String(index)]));
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, sanitizeReportValue(item, [...path, key])]),
    );
  }
  return value;
}

export function tokenLeakPaths(value, prefix = "$") {
  if (typeof value === "string") {
    const leaked = TOKEN_PATTERNS.some((pattern) => {
      pattern.lastIndex = 0;
      return pattern.test(value);
    });
    return leaked ? [prefix] : [];
  }
  if (Array.isArray(value)) {
    return value.flatMap((item, index) => tokenLeakPaths(item, `${prefix}[${index}]`));
  }
  if (value && typeof value === "object") {
    return Object.entries(value).flatMap(([key, item]) => tokenLeakPaths(item, `${prefix}.${key}`));
  }
  return [];
}

export function parseChangedFiles(value) {
  return uniqueStrings(value);
}

export function normalizeProviderConfig(input = {}) {
  const suiteRegistry = input.suite_registry || {};
  return sanitizeReportValue({
    schema_version: PROVIDER_SCHEMA_VERSION,
    provider_id: asString(input.provider_id, "aming-claw-self-install"),
    adapter: asString(input.adapter, "self_install"),
    project_id: asString(input.project_id, "aming-claw"),
    workspace_source: {
      type: asString(input.workspace_source?.type, "git"),
      repo_url: asString(input.workspace_source?.repo_url),
      ref: asString(input.workspace_source?.ref),
      mount_path: asString(input.workspace_source?.mount_path, "/plugin-source"),
    },
    bootstrap: {
      policy: asString(input.bootstrap?.policy, "none"),
      graph_reconcile: asString(input.bootstrap?.graph_reconcile, "skip"),
      require_clean_worktree: safeBool(input.bootstrap?.require_clean_worktree, true),
    },
    ai_routing_expectations: {
      hosts: uniqueStrings(input.ai_routing_expectations?.hosts || ["codex", "claude"]),
      semantic: input.ai_routing_expectations?.semantic || null,
    },
    fixture_data: {
      policy: asString(input.fixture_data?.policy, "ephemeral"),
      root: asString(input.fixture_data?.root),
    },
    suite_registry: {
      install: asArray(suiteRegistry.install).map(String),
      update: asArray(suiteRegistry.update).map(String),
      "new-feature": asArray(suiteRegistry["new-feature"] || suiteRegistry.new_feature || suiteRegistry.feature).map(String),
      "external-project": asArray(suiteRegistry["external-project"] || suiteRegistry.external_project).map(String),
    },
    cleanup: {
      policy: asString(input.cleanup?.policy, "ephemeral_container"),
    },
    evidence_mapping: {
      report_field: asString(input.evidence_mapping?.report_field, "state_manager"),
      project_graph_field: asString(input.evidence_mapping?.project_graph_field, "lanes.*.after.project_graph"),
    },
  });
}

export function validateProviderConfig(provider) {
  const errors = [];
  if (!provider || typeof provider !== "object") return ["provider config must be an object"];
  if (provider.schema_version !== PROVIDER_SCHEMA_VERSION) errors.push(`provider.schema_version must be ${PROVIDER_SCHEMA_VERSION}`);
  if (!String(provider.provider_id || "").trim()) errors.push("provider.provider_id is required");
  if (!String(provider.adapter || "").trim()) errors.push("provider.adapter is required");
  if (!String(provider.project_id || "").trim()) errors.push("provider.project_id is required");
  if (!provider.workspace_source || typeof provider.workspace_source !== "object") errors.push("provider.workspace_source is required");
  if (!provider.suite_registry || typeof provider.suite_registry !== "object") errors.push("provider.suite_registry is required");
  if (provider.ai_routing_expectations?.hosts?.some((host) => !HOST_LANES.includes(host))) {
    errors.push(`provider.ai_routing_expectations.hosts must use ${HOST_LANES.join(", ")}`);
  }
  return errors;
}

export function normalizeLaneState(input = {}) {
  const laneId = asString(input.lane_id, "install");
  const host = asString(input.host, "codex");
  return sanitizeReportValue({
    schema_version: LANE_STATE_SCHEMA_VERSION,
    lane_id: laneId,
    host,
    state_status: asString(input.state_status, "not_run"),
    container: {
      id: asString(input.container?.id),
      image: asString(input.container?.image),
      image_digest: asString(input.container?.image_digest, "unknown-local-build"),
      dirty: safeBool(input.container?.dirty, false),
      reset_status: asString(input.container?.reset_status, "not_required"),
    },
    auth: {
      mode: asString(input.auth?.mode, "AUTH_REUSED_FROM_HOST"),
      ready: safeBool(input.auth?.ready, false),
      evidence: asArray(input.auth?.evidence).map(String),
    },
    source: {
      repo_url: asString(input.source?.repo_url),
      ref: asString(input.source?.ref),
      commit: asString(input.source?.commit),
    },
    installed: {
      plugin_version: asString(input.installed?.plugin_version),
      runtime_version: asString(input.installed?.runtime_version),
      governance_schema: asString(input.installed?.governance_schema, "unknown"),
    },
    runtime: {
      governance_url: asString(input.runtime?.governance_url),
      governance_health: input.runtime?.governance_health || null,
    },
    project_graph: {
      project_id: asString(input.project_graph?.project_id),
      snapshot_id: asString(input.project_graph?.snapshot_id),
      snapshot_commit: asString(input.project_graph?.snapshot_commit),
      semantic_projection_id: asString(input.project_graph?.semantic_projection_id),
      graph_stale: safeBool(input.project_graph?.graph_stale, false),
      state: asString(input.project_graph?.state, "not_applicable"),
    },
    last_evidence: {
      status: asString(input.last_evidence?.status, "unknown"),
      commit: asString(input.last_evidence?.commit),
      report_path: asString(input.last_evidence?.report_path),
      validated_at: asString(input.last_evidence?.validated_at),
    },
  });
}

export function validateLaneState(state) {
  const errors = [];
  if (!state || typeof state !== "object") return ["lane state must be an object"];
  if (state.schema_version !== LANE_STATE_SCHEMA_VERSION) errors.push(`schema_version must be ${LANE_STATE_SCHEMA_VERSION}`);
  if (!LANE_IDS.includes(state.lane_id)) errors.push(`lane_id must be one of ${LANE_IDS.join(", ")}`);
  if (!HOST_LANES.includes(state.host)) errors.push(`host must be one of ${HOST_LANES.join(", ")}`);
  if (!LANE_STATUSES.includes(state.state_status)) errors.push(`state_status must be one of ${LANE_STATUSES.join(", ")}`);
  if (!state.auth || typeof state.auth !== "object") errors.push("auth state is required");
  if (!state.source || typeof state.source !== "object") errors.push("source state is required");
  return errors;
}

function globToRegExp(glob) {
  const escaped = String(glob)
    .replace(/[.+^${}()|[\]\\]/g, "\\$&")
    .replace(/\*\*/g, "__DOUBLE_STAR__")
    .replace(/\*/g, "[^/]*")
    .replace(/__DOUBLE_STAR__/g, ".*");
  return new RegExp(`^${escaped}$`);
}

function matchesAny(path, patterns) {
  return asArray(patterns).some((pattern) => {
    const text = String(pattern);
    if (text.endsWith("/**")) return path === text.slice(0, -3) || path.startsWith(text.slice(0, -2));
    return globToRegExp(text).test(path);
  });
}

export function planImpact({
  changedFiles = [],
  requestedLanes = [],
  featureLanes = [],
  rules = DEFAULT_IMPACT_RULES,
} = {}) {
  const files = uniqueStrings(changedFiles);
  const selections = new Map();

  for (const laneId of uniqueStrings(requestedLanes)) {
    if (!LANE_IDS.includes(laneId)) continue;
    selections.set(laneId, {
      lane_id: laneId,
      selected: true,
      reason: REASONS.explicitLane,
      changed_files: [],
    });
  }

  for (const rule of rules) {
    const matched = files.filter((file) => matchesAny(file, rule.paths));
    if (!matched.length) continue;
    const current = selections.get(rule.lane_id);
    selections.set(rule.lane_id, {
      lane_id: rule.lane_id,
      selected: true,
      reason: current?.reason || rule.reason,
      changed_files: uniqueStrings([...(current?.changed_files || []), ...matched]),
    });
  }

  for (const lane of featureLanes) {
    const matched = files.filter((file) => matchesAny(file, lane.trigger?.paths || []));
    if (!matched.length) continue;
    selections.set("new-feature", {
      lane_id: "new-feature",
      selected: true,
      feature_id: asString(lane.feature_id, asString(lane.id)),
      reason: lane.extension_point_only ? REASONS.observerPendingExtensionOnly : REASONS.featureImpact,
      changed_files: uniqueStrings(matched),
      extension_point_only: Boolean(lane.extension_point_only),
      child_backlog_id: asString(lane.child_backlog_id),
    });
  }

  return {
    schema_version: "docker_ai_e2e_impact_plan.v1",
    changed_files: files,
    lanes: LANE_IDS.map((laneId) => selections.get(laneId) || {
      lane_id: laneId,
      selected: false,
      reason: REASONS.noImpact,
      changed_files: [],
    }),
  };
}

export function planLaneDependency({
  lane_id: laneId,
  before_state: beforeState,
  target_commit: targetCommit,
  previous_baseline_commit: previousBaselineCommit = "",
} = {}) {
  const state = normalizeLaneState(beforeState || { lane_id: laneId });
  const target = asString(targetCommit);
  const current = asString(state.source?.commit);
  const previous = asString(previousBaselineCommit);
  const actions = [];
  let decision = "run";
  let reason = "";

  if (laneId === "install") {
    actions.push("reuse_auth_volume_read_only", "reset_plugin_cache_and_runtime");
    reason = REASONS.installReusesAuthFreshRuntime;
    if (!state.auth.ready) {
      decision = "blocked";
      actions.push("provide_authenticated_host_home");
      reason = REASONS.authNotReady;
    }
  } else if (laneId === "update") {
    if (target && current === target && !previous) {
      decision = "blocked";
      actions.push("select_previous_known_good_baseline");
      reason = REASONS.updateNeedsPreviousBaseline;
    } else {
      if (target && current === target) actions.push("reset_container_to_previous_baseline");
      actions.push("upgrade_runtime_and_plugin_to_target");
      reason = REASONS.updateFromPreviousBaseline;
    }
  } else if (laneId === "new-feature") {
    if (target && current !== target) {
      decision = "blocked";
      actions.push("run_update_lane_to_target_first");
      reason = REASONS.featureRequiresCurrentContainer;
    } else {
      actions.push("run_feature_smoke_on_current_container");
      reason = REASONS.featureImpact;
    }
  } else if (laneId === "external-project") {
    if (!state.project_graph?.project_id) {
      decision = "blocked";
      actions.push("provide_external_project_provider_config");
      reason = REASONS.externalProviderBlocked;
    } else {
      actions.push("bootstrap_or_refresh_target_project", "run_project_graph_reconcile_policy");
      reason = REASONS.externalProjectImpact;
    }
  }

  return {
    schema_version: "docker_ai_e2e_lane_dependency.v1",
    lane_id: laneId,
    decision,
    reason,
    actions,
    starting_commit: current,
    target_commit: target,
    previous_baseline_commit: previous,
  };
}

function reportStatusToLaneStatus(status) {
  const normalized = String(status || "").toUpperCase();
  if (normalized === "PASS") return "pass";
  if (normalized === "SKIPPED") return "skipped";
  if (normalized === "LOGIN_REQUIRED") return "login_required";
  if (normalized === "BLOCKED") return "blocked";
  if (normalized === "REUSED") return "reused";
  return "fail";
}

export function buildStateManagerReport({
  host,
  status,
  provider,
  impact_plan: impactPlan,
  before_state: beforeState,
  after_state: afterState,
  target_commit: targetCommit,
  previous_baseline_commit: previousBaselineCommit = "",
  command_evidence: commandEvidence = [],
  feature_smoke_results: featureSmokeResults = [],
  generated_at: generatedAt = new Date().toISOString(),
} = {}) {
  const laneId = "install";
  const before = normalizeLaneState({ ...(beforeState || {}), lane_id: laneId, host });
  const after = normalizeLaneState({
    ...(afterState || {}),
    lane_id: laneId,
    host,
    state_status: reportStatusToLaneStatus(status),
  });
  const dependencyPlan = planLaneDependency({
    lane_id: laneId,
    before_state: before,
    target_commit: targetCommit,
    previous_baseline_commit: previousBaselineCommit,
  });

  return sanitizeReportValue({
    schema_version: STATE_MANAGER_SCHEMA_VERSION,
    generated_at: generatedAt,
    host,
    provider: normalizeProviderConfig(provider || {}),
    target_commit: asString(targetCommit),
    lanes: {
      install: {
        status: after.state_status,
        before,
        after,
        dependency_plan: dependencyPlan,
      },
    },
    impact_plan: impactPlan || planImpact({ requestedLanes: ["install"] }),
    command_evidence: asArray(commandEvidence),
    feature_smoke_results: asArray(featureSmokeResults),
    evidence_policy: {
      tokens: "redacted",
      auth_files: "redacted_labels_only",
      real_ai_auth: "not_inferred_from_version_checks",
    },
  });
}

export function buildInstallAuditStateManagerReport({
  host,
  status,
  authMode = "AUTH_REUSED_FROM_HOST",
  authCopied = [],
  repoUrl = "",
  repoRef = "",
  workRoot = "",
  imageDigest = "unknown-local-build",
  governanceUrl = "",
  dashboard = null,
  sourceCommit = "",
  pluginVersion = "",
  reportPath = "",
  changedFiles = [],
  commandEvidence = [],
  featureSmokeResults = [],
  generatedAt,
} = {}) {
  const featureSmokeNames = asArray(featureSmokeResults)
    .map((item) => String(item?.name || item?.feature_id || "").trim())
    .filter(Boolean);
  const newFeatureSuites = uniqueStrings([
    "observer_command_pending",
    ...(featureSmokeNames.includes("live_observer_route") ? ["live_observer_route"] : []),
  ]);
  const provider = normalizeProviderConfig({
    provider_id: "aming-claw-self-install",
    adapter: "self_install",
    project_id: "aming-claw",
    workspace_source: {
      type: String(repoUrl).startsWith("file:") ? "mounted_repo" : "git",
      repo_url: repoUrl,
      ref: repoRef,
      mount_path: "/plugin-source",
    },
    bootstrap: {
      policy: "self_install_governance_start",
      graph_reconcile: "skip",
      require_clean_worktree: false,
    },
    ai_routing_expectations: { hosts: [host] },
    fixture_data: { policy: "ephemeral_docker_workspace", root: workRoot },
    suite_registry: {
      install: ["docker-hn-install-audit"],
      update: [],
      "new-feature": newFeatureSuites,
      "external-project": [],
    },
  });
  const authEvidence = asArray(authCopied).map((item) => `[redacted:${item}]`);
  const observerFeature = {
    id: "observer_command_pending",
    extension_point_only: !featureSmokeResults.length,
    child_backlog_id: "OBSERVER-COMMAND-PENDING-REMINDER-CALLBACK-20260528",
    trigger: {
      paths: [
        "agent/governance/event_bus.py",
        "agent/governance/server.py",
        "agent/mcp/events.py",
        "agent/tests/test_observer_command_queue.py",
        "agent/tests/test_mcp_events.py",
      ],
    },
  };
  const liveObserverFeature = {
    id: "live_observer_route",
    extension_point_only: !featureSmokeNames.includes("live_observer_route"),
    child_backlog_id: "AC-DEMO-DOCKER-LIVE-OBSERVER-ROUTE-20260531",
    trigger: {
      paths: [
        "scripts/live-ai-observer-route-demo.mjs",
        "scripts/test-scenarios.json",
        "scripts/test-scenario-manager.mjs",
        "docker/hn-install-audit/common/install-audit.mjs",
        "docker/hn-install-audit/validate-report.mjs",
      ],
    },
  };

  return buildStateManagerReport({
    host,
    status,
    provider,
    impact_plan: planImpact({
      changedFiles,
      requestedLanes: ["install"],
      featureLanes: [observerFeature, liveObserverFeature],
    }),
    before_state: normalizeLaneState({
      lane_id: "install",
      host,
      auth: { mode: authMode, ready: authEvidence.length > 0, evidence: authEvidence },
      container: {
        image: `aming-claw-install-audit-${host}`,
        image_digest: imageDigest,
        reset_status: "plugin_runtime_fresh_required",
      },
      source: { repo_url: repoUrl, ref: repoRef, commit: "" },
      project_graph: {
        project_id: "aming-claw",
        state: "self_install_provider_no_target_graph_claim",
      },
    }),
    after_state: normalizeLaneState({
      lane_id: "install",
      host,
      auth: { mode: authMode, ready: authEvidence.length > 0, evidence: authEvidence },
      container: {
        image: `aming-claw-install-audit-${host}`,
        image_digest: imageDigest,
        reset_status: "completed_or_failed_with_report",
      },
      source: { repo_url: repoUrl, ref: repoRef, commit: sourceCommit },
      installed: {
        plugin_version: pluginVersion,
        runtime_version: sourceCommit,
        governance_schema: "installed_runtime",
      },
      runtime: { governance_url: governanceUrl, governance_health: dashboard },
      project_graph: {
        project_id: "aming-claw",
        state: "self_install_provider_no_target_graph_claim",
      },
      last_evidence: { status, commit: sourceCommit, report_path: reportPath },
    }),
    target_commit: sourceCommit || repoRef || "unknown",
    command_evidence: commandEvidence,
    feature_smoke_results: featureSmokeResults.length ? featureSmokeResults : [
      {
        feature_id: "observer_command_pending",
        status: "not_implemented_here",
        child_backlog_id: "OBSERVER-COMMAND-PENDING-REMINDER-CALLBACK-20260528",
      },
    ],
    generated_at: generatedAt,
  });
}

export function validateStateManagerReport(stateManager) {
  const errors = [];
  if (!stateManager || typeof stateManager !== "object") return ["state_manager must be an object"];
  if (stateManager.schema_version !== STATE_MANAGER_SCHEMA_VERSION) {
    errors.push(`state_manager.schema_version must be ${STATE_MANAGER_SCHEMA_VERSION}`);
  }
  errors.push(...validateProviderConfig(stateManager.provider).map((error) => `state_manager.${error}`));
  if (!String(stateManager.target_commit || "").trim()) errors.push("state_manager.target_commit is required");
  const install = stateManager.lanes?.install;
  if (!install) {
    errors.push("state_manager.lanes.install is required");
  } else {
    errors.push(...validateLaneState(install.before).map((error) => `state_manager.lanes.install.before.${error}`));
    errors.push(...validateLaneState(install.after).map((error) => `state_manager.lanes.install.after.${error}`));
    if (!install.dependency_plan?.reason) errors.push("state_manager.lanes.install.dependency_plan.reason is required");
  }
  if (!Array.isArray(stateManager.impact_plan?.lanes) || !stateManager.impact_plan.lanes.length) {
    errors.push("state_manager.impact_plan.lanes is required");
  }
  const tokenLeaks = tokenLeakPaths(stateManager);
  if (tokenLeaks.length) errors.push(`state_manager token-looking values leaked: ${tokenLeaks.join(", ")}`);
  return errors;
}

export function runStateManagerSelfTest() {
  const provider = normalizeProviderConfig({
    provider_id: "self",
    adapter: "self_install",
    project_id: "aming-claw",
    ai_routing_expectations: { hosts: ["codex"] },
  });
  assert.deepEqual(validateProviderConfig(provider), []);

  const impact = planImpact({
    changedFiles: [
      "docker/hn-install-audit/run-install-audit.sh",
      "agent/mcp/events.py",
      "docs/config/aming-claw-yaml.md",
    ],
    requestedLanes: ["install"],
    featureLanes: [
      {
        id: "observer_command_pending",
        child_backlog_id: "OBSERVER-COMMAND-PENDING-REMINDER-CALLBACK-20260528",
        trigger: { paths: ["agent/mcp/events.py", "agent/governance/event_bus.py"] },
      },
    ],
  });
  assert.equal(impact.lanes.find((lane) => lane.lane_id === "install").selected, true);
  assert.equal(impact.lanes.find((lane) => lane.lane_id === "new-feature").reason, REASONS.featureImpact);
  assert.equal(impact.lanes.find((lane) => lane.lane_id === "external-project").selected, true);

  const blockedUpdate = planLaneDependency({
    lane_id: "update",
    before_state: normalizeLaneState({ lane_id: "update", host: "codex", auth: { ready: true }, source: { commit: "target" } }),
    target_commit: "target",
  });
  assert.equal(blockedUpdate.decision, "blocked");
  assert.equal(blockedUpdate.reason, REASONS.updateNeedsPreviousBaseline);

  const blockedFeature = planLaneDependency({
    lane_id: "new-feature",
    before_state: normalizeLaneState({ lane_id: "new-feature", host: "codex", auth: { ready: true }, source: { commit: "old" } }),
    target_commit: "target",
  });
  assert.equal(blockedFeature.reason, REASONS.featureRequiresCurrentContainer);

  const report = buildInstallAuditStateManagerReport({
    host: "codex",
    status: "PASS",
    authCopied: ["auth.json"],
    sourceCommit: "target",
    changedFiles: ["agent/mcp/events.py"],
    commandEvidence: [{ command: "echo sk-secretvalue1234567890", stdout: "Bearer abcdefghijklmnopqrstuvwxyz" }],
    featureSmokeResults: [{ name: "observer_command_pending", ok: true }],
  });
  assert.deepEqual(validateStateManagerReport(report), []);
  assert.equal(tokenLeakPaths(report).length, 0);
  assert.equal(report.command_evidence[0].stdout, "Bearer [REDACTED]");
  assert.equal(report.feature_smoke_results[0].name, "observer_command_pending");

  return { ok: true, assertions: 11 };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  try {
    console.log(JSON.stringify(runStateManagerSelfTest(), null, 2));
  } catch (error) {
    console.error(error?.stack || error?.message || error);
    process.exit(1);
  }
}
