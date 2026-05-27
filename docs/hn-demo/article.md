# Show HN: Aming Claw - A new multi-agent coding architecture (zero orchestration, commit-bound)

If you've used LangGraph supervisor, AutoGen GroupChat, or CrewAI for coding
work, you've written the orchestration. Aming Claw asks for zero.

The observer is your current Claude Code or Codex session, not a new daemon.

The observer holds the project's commit-bound code graph. It decides which
worker gets which files using two signals together: the requirement itself
(LLM-side) and the code graph's structural boundaries: dependency, module, and
function scope.

Each worker runs under its own contract: scoped files, fence token, trace
ledger, close gate. The full worker path runs each worker in an isolated git
worktree against a frozen commit hash. The HN demo starts from your current
Claude Code or Codex session as observer; scripted workers are a zero-setup
fallback that uses the same contracts, fences, and replay logic.

The shared object is not the chat. It is not the workflow state. It is the
project graph.

```text
                  observer
                     |
           commit-bound project graph
                     |
        +------------+------------+
        |                         |
   Worker A contract         Worker B contract
   scope A, fence A          scope B, fence B
        |                         |
      pass                 fail / interrupted
        |                         |
 candidate diff A          replay B against X
        |                         |
        +------------+------------+
                     |
              ordered Git merge
                     |
          target graph reconcile once
```

The case I want you to challenge:

1. Worker A and Worker B both receive contracts bound to commit hash X.
2. Worker A passes; its diff is accepted as candidate evidence.
3. Worker B fails mid-execution.
4. The observer replays Worker B against commit hash X. Worker B sees the
   original code, not Worker A's in-progress changes.
5. The replay passes, producing a clean diff against X — Worker B's contract
   scope and Worker A's contract scope are disjoint by design, so B's replay
   never touches files A already accepted.
6. Both accepted diffs land through an ordered Git merge.
7. The target project graph is reconciled once after the accepted change lands.
8. The backlog row closes only after the timeline and contract gates pass.

Worker A and Worker B can both be Claude, both Codex, scripted local workers, or
any compatible agent process. The coordination model is the same regardless of
runtime.

The installed-user demo starts with your current Claude Code or Codex session as
observer. Scripted workers are available for zero-setup reproducibility and CI,
so you do not need two AI subscriptions to challenge the protocol. Live worker
mode plugs in whichever AI runtime you have.

What is not new: supervisors, handoffs, traces, shared workflow state,
checkpoint replay, parallel branches. LangGraph has strong primitives for
supervisors, state graphs, checkpointing, replay, and durable workflows.

The narrow claim: I have not found another open-source, plug-and-play
coding-agent framework where:

1. the user writes zero orchestration code;
2. the observer decides scope from the project graph itself, not just the
   prompt;
3. workers run under commit-bound contracts with fenced files and trace ledgers;
4. replay is tied to the original contract and frozen commit instead of chat
   memory;
5. accepted work reconciles once against the target project graph before the
   next agent treats it as truth.

If you know one -- research prototypes, commercial products, open-source
projects -- please send it to me. I'd genuinely like to know what to compare
against.

Repo: https://github.com/amingclawdev/aming-claw

How to run the demo: [HN multi-agent challenge demo](README.md)

More cases, audit trails, and the design story: [Hope is not an engineering
control for AI coding agents](design-story.md)
