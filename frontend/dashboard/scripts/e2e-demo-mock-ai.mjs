#!/usr/bin/env node

import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import path from "node:path";
import { exit } from "node:process";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const DASHBOARD_DIR = path.resolve(SCRIPT_DIR, "..");
const REPO_ROOT = path.resolve(SCRIPT_DIR, "../../..");
const FLAGS = parseFlags(process.argv.slice(2));
const PROJECT = clean(FLAGS["project-id"] || "aming-claw-dashboard-mock-ai");
const BUG_ID = clean(FLAGS.backlog || "AC-DEMO-AI-MOCK-DOCKER-PLAYWRIGHT-20260531");
const PORT = Number(FLAGS.port || (5300 + Math.floor(Math.random() * 1000)));
const HEADLESS = FLAGS.headed !== true;
const KEEP_OPEN = FLAGS["keep-open"] === true || FLAGS.headed === true;
const BASE = `http://127.0.0.1:${PORT}`;
const SNAPSHOT_ID = "snap-dashboard-mock-ai";
const COMMIT = "mock-ai-fixture-head";
const RAW_PROMPT_SENTINEL = "RAW_PROMPT_SHOULD_NOT_RENDER";

function parseFlags(args) {
  const bool = new Set(["headed", "keep-open"]);
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
  return String(value || "demo").replace(/[^a-zA-Z0-9_.:-]/g, "-");
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function pid(value) {
  return encodeURIComponent(value);
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForHttp(url, timeoutMs = 30000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const response = await fetch(url);
      if (response.ok || response.status === 404) return;
    } catch {
      // Vite is still starting.
    }
    await delay(250);
  }
  throw new Error(`dashboard dev server did not become ready at ${url}`);
}

function startDashboard() {
  const child = spawn(
    "npm",
    ["run", "dev", "--", "--host", "127.0.0.1", "--port", String(PORT), "--strictPort"],
    {
      cwd: DASHBOARD_DIR,
      detached: true,
      env: {
        ...process.env,
        VITE_BACKEND_URL: "http://127.0.0.1:9",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => {
    stdout += chunk.toString("utf8");
    if (stdout.length > 12000) stdout = stdout.slice(stdout.length - 12000);
  });
  child.stderr.on("data", (chunk) => {
    stderr += chunk.toString("utf8");
    if (stderr.length > 12000) stderr = stderr.slice(stderr.length - 12000);
  });
  return {
    child,
    logs: () => ({ stdout, stderr }),
    stop: () => {
      if (!child.pid) return;
      try {
        process.kill(-child.pid, "SIGTERM");
      } catch {
        child.kill("SIGTERM");
      }
    },
  };
}

function healthResponse() {
  return {
    status: "ok",
    service: "governance-mock",
    port: 0,
    version: "mock-ai-fixture",
    pid: process.pid,
    request_id: "mock-health",
  };
}

function summaryHealth() {
  return {
    project_health_score: 1,
    raw_project_health_score: 1,
    file_hygiene_score: 1,
    artifact_binding_score: 1,
    governance_observability_score: 1,
    doc_coverage_ratio: 1,
    test_coverage_ratio: 1,
    semantic_coverage_ratio: 1,
    structure_health_score: 1,
    semantic_health_score: 1,
    project_insight_health_score: 1,
    semantic_health: {
      score: 1,
      feature_count: 0,
      semantic_current_count: 0,
      semantic_missing_count: 0,
      semantic_stale_count: 0,
      semantic_unverified_hash_count: 0,
      semantic_current_ratio: 1,
      edge_semantic_eligible_count: 0,
      edge_semantic_current_count: 0,
      edge_semantic_requested_count: 0,
      edge_semantic_missing_count: 0,
    },
    structure_health: { feature_count: 0, governed_feature_count: 0 },
  };
}

function backlogBug() {
  return {
    bug_id: BUG_ID,
    title: "Add mock-AI Docker and Playwright validation for the route-governed demo",
    status: "OPEN",
    priority: "P1",
    target_files: [
      "scripts/test-scenarios.json",
      "frontend/dashboard/src/views/BacklogView.tsx",
      "frontend/dashboard/scripts/e2e-demo-mock-ai.mjs",
    ],
    test_files: [
      "frontend/dashboard/scripts/e2e-demo-mock-ai.mjs",
      "agent/tests/test_test_scenario_manager.py",
    ],
    acceptance_criteria: [
      "Observer alert acknowledgement is visible in dashboard evidence.",
      "Expert review evidence is visible.",
      "Test route evidence is visible.",
      "Final drift prompt is visible.",
      "Docker demo visualization evidence is visible.",
      "Live AI calls stay disabled.",
    ],
    details_md: "Mock dashboard row for route-context demo validation.",
    provenance_paths: ["mock-ai-playwright-route"],
    created_at: "2026-05-31T12:00:00Z",
    updated_at: "2026-05-31T12:00:00Z",
  };
}

function mockDemoVisualizationEvidence() {
  return {
    artifact_id: "content_sys_demo_visualization",
    artifact_refs: [
      {
        id: "content_context_fixture_summary",
        path: "artifacts/content-context-fixture-summary.json",
        status: "passed",
        digest_status: "deferred_until_summary_reference_is_final",
      },
      {
        id: "content_sys_route_plan",
        path: "artifacts/content-sys-route-plan.json",
        status: "passed",
        digest: "sha256:mock-route-plan",
      },
      {
        id: "content_sys_bootstrap",
        path: "artifacts/content-sys-bootstrap.json",
        status: "passed",
        digest: "sha256:mock-bootstrap",
      },
      {
        id: "content_sys_preflight",
        path: "artifacts/content-sys-preflight.json",
        status: "passed",
        digest: "sha256:mock-preflight",
      },
      {
        id: "aming_claw_health",
        path: "artifacts/aming-claw-health.json",
        status: "passed",
        digest: "sha256:mock-health",
      },
      {
        id: "content_sys_demo_visualization",
        path: "artifacts/content-sys-demo-visualization.json",
        status: "generated",
        digest_status: "recorded_in_summary_artifact_ref",
      },
    ],
    fixture_id: "content_sys.docker_context_fixture",
    frontend_display_contract: {
      artifact_path: "artifacts/content-sys-demo-visualization.json",
      panel_ids: [
        "content_sys_demo_status_cards",
        "content_sys_demo_timeline",
        "content_sys_demo_artifacts",
        "content_sys_demo_privacy_boundary",
      ],
      render_outside_docker: true,
      route_id: "content_sys_docker_context_fixture",
      screen_id: "content_sys_demo_visualization",
      schema_version: "content_sys.demo_visualization_evidence.v1",
    },
    privacy_boundary: {
      host_home_mounted: false,
      host_only_context_required: false,
      host_paths_emitted: false,
      host_provider_env: false,
      model_calls: "forbidden",
      provider_runtime: "disabled",
      raw_prompt_emitted: false,
      real_media_sources: "omitted",
    },
    public_summary: "Public Docker fixture evidence for content-sys demo visualization is ready for frontend rendering.",
    route_identity: {
      prompt_contract_id: "content_sys.docker_context_fixture.v1",
      route_context_hash: "sha256:mock-content-sys-route-context",
      route_id: "content_sys_docker_context_fixture",
      visible_injection_manifest_hash: "sha256:mock-content-sys-visible-manifest",
    },
    route_refs: {
      prompt_contract_id: "content_sys.docker_context_fixture.v1",
      route_context_hash: "sha256:mock-content-sys-route-context",
      route_id: "content_sys_docker_context_fixture",
      visible_injection_manifest_hash: "sha256:mock-content-sys-visible-manifest",
    },
    scenario_id: "content_sys_docker_context_fixture",
    schema_version: "content_sys.demo_visualization_evidence.v1",
    status_cards: [
      { id: "container_governance", sequence: 1, status: "passed" },
      { id: "governed_project", sequence: 2, status: "passed" },
      { id: "bootstrap", sequence: 3, status: "passed" },
      { id: "preflight", sequence: 4, status: "passed" },
      { id: "tests", sequence: 5, status: "passed" },
      { id: "route_plan", sequence: 6, status: "passed" },
      { id: "privacy_boundary", sequence: 7, status: "passed" },
      { id: "self_graph_required", sequence: 8, status: "passed" },
    ],
    timeline_events: [
      { id: "checkout", sequence: 1, status: "passed" },
      { id: "governance", sequence: 2, status: "passed" },
      { id: "bootstrap", sequence: 3, status: "passed" },
      { id: "preflight", sequence: 4, status: "passed" },
      { id: "tests", sequence: 5, status: "passed" },
      { id: "route_plan", sequence: 6, status: "passed" },
      { id: "summary", sequence: 7, status: "passed" },
    ],
  };
}

function mockTimelineEvent() {
  return {
    event_id: "mock-ai-observer-route-alert",
    event_type: "route_context_alert",
    event_kind: "route_context",
    actor: "observer",
    phase: "implementation",
    status: "acknowledged",
    payload: {
      lane: "observer",
      requirement_ids: [
        "observer_alert_ack_visible",
        "expert_review_visible",
        "test_route_visible",
        "final_drift_prompt_visible",
        "demo_visualization_evidence",
      ],
      observer_alert_acknowledgement: {
        received: true,
        stage: "implementation",
        caller_role: "observer",
        route_context_hash: "sha256:mock-route-context",
        prompt_contract_hash: "sha256:mock-prompt-contract",
        alert_codes: ["route_context_loaded", "test_flow_playwright_mock_ai"],
        raw_context_exposed: false,
      },
      expert_review: {
        reviewer: "architecture-review-agent",
        verdict: "approved_with_tests",
        evidence: "Reviewed route context evidence cards, deterministic mock AI input, and the selected test route.",
      },
      test_flow_route: {
        decision: "playwright_mock_ai",
        primary_lane: "playwright_mock_ai",
        lanes: ["playwright_mock_ai"],
        model_calls: "mocked",
        live_ai: "disabled",
        requires_flags: [],
      },
      final_drift_prompt: {
        status: "shown",
        drift_state: "possible_drift_reviewed",
        message: "Before close, re-check route context, test evidence, and asset drift state.",
      },
      demo_visualization_evidence: mockDemoVisualizationEvidence(),
    },
    verification: {
      passed: true,
      mock_ai_provider: "fixture",
      model_calls: "mocked",
    },
    created_at: "2026-05-31T12:02:00Z",
  };
}

function mockApiResponse(url) {
  const pathName = url.pathname;
  const graphPrefix = `/api/graph-governance/${pid(PROJECT)}`;
  const backlogPrefix = `/api/backlog/${pid(PROJECT)}`;
  const bug = backlogBug();
  const timelineEvent = mockTimelineEvent();

  if (pathName === "/api/health") return healthResponse();
  if (pathName === "/api/projects") {
    return {
      ok: true,
      projects: [{ project_id: PROJECT, name: "Dashboard Mock AI", status: "graph-active", initialized: true }],
    };
  }
  if (pathName === `/api/projects/${pid(PROJECT)}/ai-config`) {
    return {
      project_id: PROJECT,
      workspace_path: REPO_ROOT,
      role_routing: { observer: { provider: "mock", model: "mock-ai-fixture", source: "playwright_fixture" } },
      tool_health: { mock: { provider: "mock", status: "ready", auth_status: "not_required" } },
      model_catalog: { providers: { mock: { label: "Mock", runtime: "fixture" } }, models: { mock: ["mock-ai-fixture"] } },
      semantic: { provider: "mock", model: "mock-ai-fixture", use_ai_default: false },
    };
  }
  if (pathName === `${graphPrefix}/status`) {
    return {
      ok: true,
      project_id: PROJECT,
      active_snapshot_id: SNAPSHOT_ID,
      graph_snapshot_commit: COMMIT,
      materialized_graph_baseline_commit: COMMIT,
      scan_baseline_commit: COMMIT,
      scan_baseline_id: 1,
      pending_scope_reconcile_count: 0,
      pending_scope_reconcile: [],
      current_state: { snapshot_id: SNAPSHOT_ID, graph_stale: { is_stale: false, active_graph_commit: COMMIT, head_commit: COMMIT } },
    };
  }
  if (pathName === `${graphPrefix}/snapshots/active/summary`) {
    return {
      ok: true,
      project_id: PROJECT,
      snapshot_id: SNAPSHOT_ID,
      commit_sha: COMMIT,
      snapshot_kind: "mock",
      snapshot_status: "active",
      created_at: "2026-05-31T12:00:00Z",
      graph_sha256: "sha256:graph",
      inventory_sha256: "sha256:inventory",
      drift_sha256: "sha256:drift",
      counts: {
        nodes: 0,
        nodes_by_layer: {},
        edges: 0,
        edges_by_type: {},
        features: 0,
        files: 0,
        orphan_files: 0,
        pending_decision_files: 0,
        cleanup_candidates: 0,
        ai_review_feedback: 0,
      },
      health: summaryHealth(),
    };
  }
  if (pathName === `${graphPrefix}/snapshots/active/semantic/projection`) {
    return { ok: true, project_id: PROJECT, snapshot_id: SNAPSHOT_ID, projection: null };
  }
  if (pathName === `${graphPrefix}/snapshots/${SNAPSHOT_ID}/nodes`) {
    return { ok: true, project_id: PROJECT, snapshot_id: SNAPSHOT_ID, nodes: [], count: 0 };
  }
  if (pathName === `${graphPrefix}/snapshots/${SNAPSHOT_ID}/edges`) {
    return { ok: true, project_id: PROJECT, snapshot_id: SNAPSHOT_ID, edges: [], count: 0 };
  }
  if (pathName === `${graphPrefix}/snapshots/${SNAPSHOT_ID}/feedback/queue`) {
    return { ok: true, project_id: PROJECT, snapshot_id: SNAPSHOT_ID, group_count: 0, count: 0, groups: [], summary: {} };
  }
  if (pathName === `${graphPrefix}/snapshots/${SNAPSHOT_ID}/asset-inbox`) {
    return {
      schema_version: "asset_inbox.v1",
      ok: true,
      project_id: PROJECT,
      snapshot_id: SNAPSHOT_ID,
      commit_sha: COMMIT,
      impact_scope_policy: "accepted_bindings_only",
      backlog_policy: { default_container: false, create_from_selected_assets_only: true },
      summary: { total: 0, by_status: {}, operator_review_count: 0 },
      items: [],
      batch_actions: [],
    };
  }
  if (pathName === `${graphPrefix}/asset-impact/reminders`) {
    return {
      ok: true,
      project_id: PROJECT,
      status: "pending",
      asset_kind: "",
      reminders: [],
      count: 0,
      summary: { total: 0, pending: 0, by_kind: {}, by_asset_kind: {}, by_status: {} },
    };
  }
  if (pathName === `${graphPrefix}/operations/queue`) {
    return {
      ok: true,
      project_id: PROJECT,
      snapshot_id: SNAPSHOT_ID,
      active_snapshot_id: SNAPSHOT_ID,
      count: 0,
      operations: [],
      summary: { by_type: {}, by_status: {}, pending_scope_reconcile_count: 0 },
    };
  }
  if (pathName === `${backlogPrefix}/${encodeURIComponent(BUG_ID)}/timeline-gate`) {
    return {
      bug_id: BUG_ID,
      applicable: true,
      can_close: false,
      timeline_gate: {
        schema_version: "mf_close_timeline_gate.v1",
        passed: false,
        status: "blocked",
        required_event_kinds: ["route_context"],
        present_event_kinds: ["route_context"],
        missing_event_kinds: [],
        event_count: 1,
        contract_gate: {
          passed: true,
          status: "passed",
          required_requirement_ids: [
            "observer_alert_ack_visible",
            "expert_review_visible",
            "test_route_visible",
            "final_drift_prompt_visible",
            "demo_visualization_evidence",
          ],
          present_requirement_ids: [
            "observer_alert_ack_visible",
            "expert_review_visible",
            "test_route_visible",
            "final_drift_prompt_visible",
            "demo_visualization_evidence",
          ],
          missing_requirement_ids: [],
        },
      },
      event_count: 1,
      events: [timelineEvent],
    };
  }
  if (pathName === `${backlogPrefix}/${encodeURIComponent(BUG_ID)}`) return bug;
  if (pathName === backlogPrefix) {
    return {
      bugs: [bug],
      count: 1,
      total_count: 1,
      filtered_count: 1,
      view: "compact",
      summary: { total: 1, open: 1, fixed: 0, urgent_open: 1, by_status: { OPEN: 1 }, by_priority: { P1: 1 } },
    };
  }
  return null;
}

async function fulfillApi(route) {
  const url = new URL(route.request().url());
  const response = mockApiResponse(url);
  if (!response) {
    await route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ ok: false, error: `unmocked route ${url.pathname}` }),
    });
    return;
  }
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(response),
  });
}

async function expectVisibleText(page, text) {
  const locator = page.getByText(text, { exact: false }).first();
  await locator.waitFor({ state: "visible", timeout: 20000 });
  return (await locator.textContent()) || "";
}

async function main() {
  const server = startDashboard();
  let browser = null;
  try {
    await waitForHttp(BASE);
    browser = await chromium.launch({ headless: HEADLESS });
    const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
    await page.route("**/api/**", fulfillApi);
    await page.goto(`${BASE}/?project_id=${encodeURIComponent(PROJECT)}&view=backlog&backlog=${encodeURIComponent(BUG_ID)}`, {
      waitUntil: "networkidle",
      timeout: 30000,
    });

    const requiredTexts = [
      "Observer alert received",
      "Expert review",
      "Test route",
      "Final drift prompt",
      "Content-sys demo visualization",
      "Docker demo status",
      "Docker demo timeline",
      "Docker artifact refs",
      "Privacy boundary",
      "Frontend display contract",
      "playwright_mock_ai",
      "mocked",
      "content_sys.demo_visualization_evidence.v1",
      "model_calls: forbidden",
      "Add mock-AI Docker and Playwright validation",
    ];
    const found = [];
    for (const text of requiredTexts) {
      found.push(await expectVisibleText(page, text));
    }
    const body = (await page.textContent("body")) || "";
    assert(!body.includes(RAW_PROMPT_SENTINEL), "raw prompt sentinel leaked into dashboard body");
    const unexpectedTexts = [
      "No status_cards were attached to this visualization artifact.",
      "No timeline_events were attached to this visualization artifact.",
      "Privacy boundary fields are missing from this visualization artifact.",
      "Display contract is missing an artifact path",
    ];
    for (const text of unexpectedTexts) {
      assert(!body.includes(text), `duplicate or partial visualization card rendered: ${text}`);
    }

    if (KEEP_OPEN) await page.waitForTimeout(3000);
    console.log(JSON.stringify({
      ok: true,
      message: "dashboard mock-ai route evidence ok",
      project_id: PROJECT,
      backlog_id: BUG_ID,
      required_texts: requiredTexts,
      found_count: found.length,
      live_ai: "disabled",
      model_calls: "mocked",
    }, null, 2));
  } catch (error) {
    const logs = server.logs();
    console.error(String(error?.message || error));
    if (logs.stderr.trim()) console.error(logs.stderr.trim().slice(-2000));
    exit(1);
  } finally {
    if (browser) await browser.close().catch(() => {});
    server.stop();
  }
}

await main();
