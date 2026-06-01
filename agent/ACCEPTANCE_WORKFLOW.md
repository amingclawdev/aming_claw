<!-- governance-hint {"attach_to_node": {"path": "agent/ACCEPTANCE_WORKFLOW.md", "role": "doc", "target_module": "agent.workspace_queue", "target_node_id": "L7.198", "target_title": "agent.workspace_queue"}} -->

# Task Iteration and Acceptance Workflow

## Status Transitions
1. `pending`: Task created, awaiting execution
2. `processing`: In progress
3. `pending_acceptance`: Execution complete, awaiting user acceptance
4. `accepted`: User acceptance passed
5. `archive`: Only `accepted` tasks may be archived
6. `rejected`: User acceptance rejected; task remains in results area for further iteration

## Acceptance Gates
- After task execution, acceptance document and test cases must be written
- Archiving is prohibited until `/accept <task_id|alias>` is executed
- After `/reject <task_id|alias> <reason>`, the task remains queryable and can be fixed and re-submitted for acceptance

## Query Commands
- `/status`: View active tasks with acceptance markers
- `/status <task_id|alias>`: View single task status, acceptance marker, next command, acceptance doc path
- `/accept <task_id|alias>`: Accept and archive
- `/reject <task_id|alias> <reason>`: Reject without archiving

## Task Completion Artifacts
- `shared-volume/codex-tasks/results/<task_id>.json`
- `shared-volume/codex-tasks/logs/<task_id>.run.json`
- `shared-volume/codex-tasks/acceptance/<task_id>.acceptance.md`
- `shared-volume/codex-tasks/acceptance/<task_id>.cases.json`
