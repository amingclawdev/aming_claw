# HN Demo Audits

This directory stores repeatable launch-rehearsal reports for the HN demo.

Run from the Aming Claw plugin checkout while governance is already running:

```bash
node frontend/dashboard/scripts/e2e-hn-demo.mjs --sandbox-audit --no-browser
```

The sandbox audit uses a run-specific fixture project and writes:

- `latest.md` - human-readable report with raw evidence, machine audit,
  same-observer self-review, and launch recommendation.
- `latest.json` - machine-readable evidence bundle.
- `<run-id>.md` / `<run-id>.json` - immutable run artifacts when the default
  report path is used.

The fixture setup must stay empty: it creates only a demo project, baseline git
commit, project bootstrap, and active graph. Backlog rows, timeline events,
contracts, worker fences, trace ids, tests, reconcile evidence, and review
judgment must be produced by the observer path during the audit.

For full install E2E, run the Docker host lanes first. This is a release gate,
not a user-facing install requirement; Docker exists here to remove local
plugin/auth/cache pollution from the test.

```bash
docker/hn-install-audit/run-install-audit.sh --host both
```

If the Claude login was captured in a dedicated auth home, make it explicit:

```bash
docker/hn-install-audit/run-install-audit.sh \
  --host claude \
  --claude-auth-home ~/.aming-claw/docker-auth/claude-home
```

Then pass the generated reports into the sandbox audit when you want the launch
gate to include one-click install evidence:

```bash
node frontend/dashboard/scripts/e2e-hn-demo.mjs \
  --sandbox-audit \
  --no-browser \
  --require-install-gates \
  --codex-install-report docs/hn-demo/audits/install-<run-id>/codex-install-audit-<run-id>.json \
  --claude-install-report docs/hn-demo/audits/install-<run-id>/claude-install-audit-<run-id>.json
```

Without those reports, local package checks are only preflight evidence. They
must not be treated as Codex or Claude one-click install PASS.
