#!/usr/bin/env node
import assert from "node:assert/strict";
import crypto from "node:crypto";

const SCENARIO_ID = "gated_live_ai_observer_route_demo";
const FIXED_AT = "2026-05-31T12:08:00Z";

function hasFlag(name) {
  return process.argv.slice(2).includes(name);
}

function hash(value) {
  return `sha256:${crypto.createHash("sha256").update(value).digest("hex")}`;
}

function blockedPayload() {
  return {
    schema_version: "live_observer_route_demo.v1",
    ok: false,
    scenario_id: SCENARIO_ID,
    status: "blocked",
    blocked: {
      reason_code: "live_ai_observer_route_requires_approval",
      reason: "The gated live observer route demo requires --allow-live-ai.",
      remediation: "Re-run only after operator approval with --allow-live-ai.",
    },
    live_ai: {
      approval: "missing",
      execution_mode: "blocked",
      calls_models: false,
      silent_quota_use: false,
    },
  };
}

function allowedPayload() {
  const routeContextHash = hash(`${SCENARIO_ID}:route-context:v1`);
  const promptContractHash = hash(`${SCENARIO_ID}:prompt-contract:v1`);
  const finalDriftPromptHash = hash(`${SCENARIO_ID}:final-drift-prompt:v1`);
  const stepOutputs = [
    {
      step_id: "01_route_alert_ack",
      status: "passed",
      output_summary: "Observer acknowledged the selected live observer route alert before executing route evidence.",
      evidence_ref: "route_alert_acknowledged",
    },
    {
      step_id: "02_ordered_route_steps",
      status: "passed",
      output_summary: "Observer followed the route order: alert, evidence capture, drift prompt.",
      evidence_ref: "ordered_step_outputs",
    },
    {
      step_id: "03_sanitized_live_ai_evidence",
      status: "passed",
      output_summary: "Live observer evidence is represented by an operator-approved deterministic harness with no provider stdout.",
      evidence_ref: "live_ai_sanitized",
    },
  ];
  const payload = {
    schema_version: "live_observer_route_demo.v1",
    ok: true,
    scenario_id: SCENARIO_ID,
    status: "passed",
    source: "deterministic_test_harness",
    created_at: FIXED_AT,
    route_context: {
      service_id: "observer.live_ai_route_demo",
      role: "observer",
      stage: "implementation",
      route_context_hash: routeContextHash,
      prompt_contract_hash: promptContractHash,
      raw_context_exposed: false,
    },
    live_ai: {
      approval: "operator-approved/manual",
      approval_flag: "--allow-live-ai",
      execution_mode: "deterministic_test_harness",
      provider: "manual",
      model: "manual",
      calls_models: false,
      silent_quota_use: false,
      prompt_output_policy: "redacted",
      credential_output_policy: "redacted",
    },
    observer_evidence: {
      route_alert_ack: {
        status: "acknowledged",
        caller_role: "observer",
        alert_codes: ["test_flow_live_observer_route", "live_observer_route_demo"],
        route_context_hash: routeContextHash,
        prompt_contract_hash: promptContractHash,
      },
      ordered_step_outputs: stepOutputs,
      final_drift_prompt: {
        status: "shown",
        drift_state: "possible_drift_reviewed",
        prompt_hash: finalDriftPromptHash,
        prompt_summary: "Before close, re-check route context, test evidence, live-AI approval, and asset drift state.",
        prompt_output_policy: "hash_and_summary_only",
      },
      no_raw_prompt_output: true,
    },
    timeline: [
      {
        seq: 1,
        event_type: "route_alert_ack",
        event_kind: "route_context",
        actor: "observer",
        phase: "implementation",
        status: "acknowledged",
        payload_ref: "observer_evidence.route_alert_ack",
      },
      ...stepOutputs.map((step, index) => ({
        seq: index + 2,
        event_type: "observer_step_output",
        event_kind: "implementation",
        actor: "observer",
        phase: "implementation",
        status: step.status,
        step_id: step.step_id,
        payload_ref: `observer_evidence.ordered_step_outputs.${index}`,
      })),
      {
        seq: 5,
        event_type: "final_drift_prompt",
        event_kind: "verification",
        actor: "observer",
        phase: "verification",
        status: "shown",
        payload_ref: "observer_evidence.final_drift_prompt",
      },
    ],
    checks: [
      { id: "route_alert_ack", status: "passed" },
      { id: "ordered_step_outputs", status: "passed" },
      { id: "final_drift_prompt", status: "passed" },
      { id: "live_ai_evidence_sanitized", status: "passed" },
      { id: "no_raw_prompt_output", status: "passed" },
    ],
  };

  assert.equal(payload.live_ai.calls_models, false);
  assert.equal(payload.observer_evidence.no_raw_prompt_output, true);
  assert.deepEqual(payload.timeline.map((event) => event.seq), [1, 2, 3, 4, 5]);
  return payload;
}

const payload = hasFlag("--allow-live-ai") ? allowedPayload() : blockedPayload();
process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
if (!payload.ok) process.exitCode = 2;
