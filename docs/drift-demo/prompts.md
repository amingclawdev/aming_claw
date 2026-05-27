# Docs Drift Demo Prompts

Send these prompts one message at a time. Wait for the observer to answer each
step before sending the next one.

## 1. Start The Demo

```text
Use this current Claude Code or Codex session as the observer for the Docs Drift
demo. If there is no safe project ready, set up the isolated fixture first.
Show me the project id and the dashboard graph link.
```

## 2. First Change With Stale Docs

```text
Change the demo reminder feature so reminders can include optional email
notifications, but do not update `docs/reminders.md` yet. Keep the work bounded
and tell me where the stale documentation signal appears.
```

## 3. Inspect The Drift

```text
Show me the dashboard evidence that the feature changed while docs are stale.
Include the graph, asset or review state, operations queue, and backlog or
timeline link.
```

## 4. Second Doc-Fix Prompt

```text
Now update the docs so the reminder documentation matches the email-notification
behavior. Keep this as a doc-fix step and show me how the drift state changes
afterward.
```

## 5. Ask For The Evidence Summary

```text
Summarize the Docs Drift demo evidence: first code change, stale-doc signal,
doc fix, dashboard links, and any limitations.
```
