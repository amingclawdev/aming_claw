# syntax=docker/dockerfile:1
FROM node:22-alpine

WORKDIR /demo

COPY docs/fixtures/observer-reminder-echo-demo.json docs/fixtures/observer-reminder-echo-demo.json
COPY docs/demos/observer-reminder-echo.md docs/demos/observer-reminder-echo.md

RUN node <<'NODE'
const fs = require("fs");

const fixturePath = "docs/fixtures/observer-reminder-echo-demo.json";
const docPath = "docs/demos/observer-reminder-echo.md";
const fixture = JSON.parse(fs.readFileSync(fixturePath, "utf8"));
const doc = fs.readFileSync(docPath, "utf8");

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

for (const key of [
  "observer_prompt_steps",
  "fixture_payload",
  "expected_hook_reminder",
  "expected_subagent_received_reminder_echo",
  "expected_file_locations",
  "change_record_checks"
]) {
  assert(Object.hasOwn(fixture, key), `fixture missing ${key}`);
}

assert(Array.isArray(fixture.observer_prompt_steps), "observer_prompt_steps must be an array");
assert(fixture.observer_prompt_steps.length >= 6, "observer_prompt_steps should describe the real local flow");

const reminder = fixture.expected_hook_reminder;
assert(reminder.kind === "observer_command_pending", "unexpected reminder kind");
assert(reminder.project_id === "aming-claw", "unexpected reminder project_id");
assert(reminder.payload_included === false, "hook reminder must be payload-free");

for (const forbidden of [
  "payload",
  "command_type",
  "demo_id",
  "request_summary",
  "requested_worker_lane",
  "business_payload_should_not_appear_in_hook",
  "session_token"
]) {
  assert(!Object.hasOwn(reminder, forbidden), `hook reminder leaks ${forbidden}`);
}

const echo = fixture.expected_subagent_received_reminder_echo;
assert(echo.field_name === "received_reminder_echo", "subagent echo field name changed");
assert(JSON.stringify(echo.required_value) === JSON.stringify(reminder), "subagent echo must match hook reminder");

assert(doc.includes("Docker is only a validation harness"), "doc must state Docker validation boundary");
assert(doc.includes("It does not start governance"), "doc must state Docker does not start governance");
assert(doc.includes("received_reminder_echo"), "doc must describe subagent reminder echo");
assert(!doc.includes("sk-"), "doc must not include obvious API-key-looking text");
NODE

CMD ["node", "-e", "console.log('observer reminder echo fixture validation passed; run the real demo locally with docs/demos/observer-reminder-echo.md')"]
