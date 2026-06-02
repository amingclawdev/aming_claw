"""Tests for task implementation timeline evidence."""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _conn(tmp_dir):
    os.environ["SHARED_VOLUME_PATH"] = tmp_dir
    os.makedirs(
        os.path.join(tmp_dir, "codex-tasks", "state", "governance", "proj"),
        exist_ok=True,
    )
    from agent.governance.db import get_connection

    return get_connection("proj")


def _ctx(query=None, *, path_params=None, body=None, method="GET"):
    from agent.governance import server

    params = {"project_id": "proj"}
    if path_params:
        params.update(path_params)
    return server.RequestContext(
        None,
        method,
        params,
        query or {},
        body or {},
        "req-test",
        "",
        "",
    )


ROUTE_IDENTITY = {
    "route_context_hash": "sha256:4920bc6ece43e5166504c5c91d8e657eb4bf7490eb85df81d668b6ea60f6a927",
    "prompt_contract_id": "rprompt-ac-service-route-context-gate-20260531",
    "prompt_contract_hash": "sha256:e96ff2d045d64d1578145c9ec1457ff0a3b220b6dfdfe35c331dff620a3e0e3a",
}


def _route_context_consumption_events():
    return [
        {
            "event_kind": "route_context",
            "phase": "dispatch",
            "status": "passed",
            "event_id": "tl-route-context",
            "payload": {
                "route_context": {
                    **ROUTE_IDENTITY,
                    "caller_role": "observer",
                    "allowed_actions": ["dispatch_worker"],
                    "blocked_actions": ["apply_patch"],
                    "required_lanes": ["bounded_implementation_worker"],
                },
                "visible_injection_manifest_hash": "sha256:test-visible-manifest",
            },
        },
        {
            "event_kind": "route_action_precheck",
            "phase": "pre_mutation",
            "status": "allowed",
            "event_id": "tl-route-action",
            "verification": {
                **ROUTE_IDENTITY,
                "allowed_action": "dispatch_worker",
                "caller_role": "observer",
            },
        },
        {
            "event_kind": "mf_subagent_dispatch",
            "phase": "dispatch",
            "status": "passed",
            "event_id": "tl-dispatch",
            "payload": {
                "mf_subagent_dispatch_gate": {
                    **ROUTE_IDENTITY,
                    "worker_id": "mf-sub-test",
                    "bounded": True,
                }
            },
        },
        {
            "event_kind": "mf_subagent_startup",
            "phase": "startup_gate",
            "status": "passed",
            "event_id": "tl-startup",
            "payload": {
                "mf_subagent_startup_gate": {
                    **ROUTE_IDENTITY,
                    "worker_id": "mf-sub-test",
                    "fence_token": "fence-test",
                }
            },
        },
    ]


def _route_context_qa_verification_event():
    return {
        "event_kind": "qa_verification",
        "phase": "verification",
        "status": "passed",
        "event_id": "tl-qa-verification",
        "verification": {
            **ROUTE_IDENTITY,
            "contract_evidence": [
                {
                    "requirement_id": "independent_verification_lane",
                    "status": "passed",
                    "reviewer_role": "qa",
                }
            ],
        },
    }


def _without_prompt_contract_hash(value):
    if isinstance(value, dict):
        return {
            key: _without_prompt_contract_hash(item)
            for key, item in value.items()
            if key != "prompt_contract_hash"
        }
    if isinstance(value, list):
        return [_without_prompt_contract_hash(item) for item in value]
    return value


def _route_token(action="task_timeline_append", bug_id="BUG-ROUTE", task_id="", project_id="proj"):
    scope = {"project_id": project_id, "backlog_id": bug_id}
    if task_id:
        scope["task_id"] = task_id
    return {
        "route_context_hash": f"sha256:test-route-context-{action}",
        "prompt_contract_id": f"prompt-contract-{action}",
        "prompt_contract_hash": f"sha256:test-prompt-contract-{action}",
        "caller_role": "observer",
        "allowed_action": action,
        "scope": scope,
        "expires_at": "2999-01-01T00:00:00Z",
        "evidence_refs": ["timeline:test-route-token"],
    }


def _route_waiver(action="task_timeline_append", bug_id="BUG-ROUTE", task_id="", project_id="proj"):
    scope = {"project_id": project_id, "backlog_id": bug_id}
    if task_id:
        scope["task_id"] = task_id
    return {
        "accepted": True,
        "waiver_type": "manual_fix",
        "route_context_hash": f"sha256:test-route-context-{action}",
        "prompt_contract_id": f"prompt-contract-{action}",
        "caller_role": "observer",
        "allowed_action": action,
        "scope": scope,
        "reason": "Unit test supplies explicit route gate waiver evidence.",
        "timeline_evidence": {"event_id": "test-route-gate"},
    }


class TestTaskTimeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def _insert_router_backlog(self, bug_id="BUG-SERVICE-ROUTER", contract=None):
        contract = contract or {
            "parallel_contract": {
                "template_id": "mf_parallel.v1",
                "contract_instance_id": bug_id,
            }
        }
        self.conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, chain_trigger_json, created_at, updated_at)
               VALUES (?, ?, 'OPEN', 'P1', ?, '2026-05-29T00:00:00Z', '2026-05-29T00:00:00Z')""",
            (bug_id, "Service router test", json.dumps(contract)),
        )
        self.conn.commit()
        return contract

    def _record_route_context_consumption(self, bug_id, *, task_id=""):
        from agent.governance import task_timeline

        for event in [
            *_route_context_consumption_events(),
            _route_context_qa_verification_event(),
        ]:
            task_timeline.record_event(
                self.conn,
                project_id="proj",
                backlog_id=bug_id,
                task_id=task_id,
                event_type=event.get("event_type") or event.get("event_kind"),
                phase=event.get("phase", ""),
                event_kind=event.get("event_kind", ""),
                status=event.get("status", ""),
                payload=event.get("payload") or {},
                verification=event.get("verification") or {},
                artifact_refs=event.get("artifact_refs") or {},
            )
        self.conn.commit()

    def _route_waiver_for_existing_identity(self, bug_id, *, task_id=""):
        waiver = _route_waiver("task_timeline_append", bug_id, task_id=task_id)
        waiver.update(ROUTE_IDENTITY)
        return waiver

    def test_concurrent_timeline_writes_use_serialized_queue(self):
        from agent.governance import task_timeline

        errors = []

        def write(i):
            try:
                task_timeline.enqueue_event(
                    "proj",
                    task_id="task-concurrent",
                    backlog_id="BUG-TL",
                    attempt_num=1,
                    event_type="ai.implementation_evidence.proposed",
                    actor=f"worker-{i}",
                    status="proposed",
                    payload={"i": i},
                    wait=True,
                )
            except Exception as exc:  # pragma: no cover - failure surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(i,)) for i in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        events = task_timeline.list_events(self.conn, "proj", task_id="task-concurrent")
        self.assertEqual(len(events), 20)
        self.assertEqual(
            {event["payload"]["i"] for event in events},
            set(range(20)),
        )

    def test_timeline_append_protected_close_evidence_requires_route_token_before_mutation(self):
        from agent.governance import server
        from agent.governance.errors import GovernanceError

        for event_kind in ("implementation", "qa_verification", "independent_verification"):
            with self.subTest(event_kind=event_kind):
                with self.assertRaises(GovernanceError) as raised:
                    server.handle_task_timeline_append(
                        _ctx(
                            body={
                                "backlog_id": "BUG-TL-PROTECTED",
                                "event_type": f"mf.{event_kind}",
                                "event_kind": event_kind,
                                "status": "accepted",
                            },
                            method="POST",
                        )
                    )

                self.assertEqual(raised.exception.code, "route_token_required")
                self.assertEqual(
                    raised.exception.details["fault_domain"],
                    "caller_missing_route_evidence",
                )
                self.assertTrue(raised.exception.details["expected_behavior"])
                self.assertTrue(raised.exception.details["do_not_file_system_bug"])
                self.assertFalse(raised.exception.details["is_system_bug"])
                self.assertIn("next_valid_actions", raised.exception.details)
                self.assertIn("system_bug_preconditions", raised.exception.details)
                count = self.conn.execute(
                    "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ?",
                    ("BUG-TL-PROTECTED",),
                ).fetchone()["c"]
                self.assertEqual(count, 0)

    def test_timeline_append_rejects_generic_waiver_without_route_identity(self):
        from agent.governance import server
        from agent.governance.errors import GovernanceError

        with self.assertRaises(GovernanceError) as raised:
            server.handle_task_timeline_append(
                _ctx(
                    body={
                        "backlog_id": "BUG-TL-BAD-WAIVER",
                        "event_type": "mf.verification",
                        "event_kind": "verification",
                        "status": "passed",
                        "route_waiver": {
                            "accepted": True,
                            "waiver_type": "manual_fix",
                            "allowed_action": "task_timeline_append",
                            "scope": {"project_id": "proj", "backlog_id": "BUG-TL-BAD-WAIVER"},
                            "reason": "Unit test supplies explicit route gate waiver evidence.",
                            "timeline_evidence": {"event_id": "test-route-gate"},
                        },
                    },
                    method="POST",
                )
            )

        self.assertEqual(raised.exception.code, "route_token_required")
        self.assertIn("route identity", str(raised.exception))
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ?",
            ("BUG-TL-BAD-WAIVER",),
        ).fetchone()["c"]
        self.assertEqual(count, 0)

    def test_timeline_append_accepts_valid_route_token(self):
        from agent.governance import server

        result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": "BUG-TL-TOKEN",
                    "event_type": "mf.close_ready",
                    "event_kind": "close_ready",
                    "status": "accepted",
                    "route_token": _route_token("task_timeline_append", "BUG-TL-TOKEN"),
                },
                method="POST",
            )
        )

        self.assertEqual(result["route_token_gate"]["decision"], "route_token")
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ?",
            ("BUG-TL-TOKEN",),
        ).fetchone()["c"]
        self.assertEqual(count, 2)

    def test_timeline_append_accepts_route_context_waiver(self):
        from agent.governance import server

        result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": "BUG-TL-WAIVER",
                    "event_type": "mf.verification",
                    "event_kind": "verification",
                    "status": "passed",
                    "route_waiver": _route_waiver("task_timeline_append", "BUG-TL-WAIVER"),
                },
                method="POST",
            )
        )

        self.assertEqual(result["route_token_gate"]["decision"], "route_waiver")
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ?",
            ("BUG-TL-WAIVER",),
        ).fetchone()["c"]
        self.assertEqual(count, 2)

    def test_mf_parallel_timeline_rejects_generic_waiver_for_protected_evidence(self):
        from agent.governance import server
        from agent.governance.errors import GovernanceError

        self._insert_router_backlog("BUG-TL-MF-PARALLEL-WAIVER")

        with self.assertRaises(GovernanceError) as raised:
            server.handle_task_timeline_append(
                _ctx(
                    body={
                        "backlog_id": "BUG-TL-MF-PARALLEL-WAIVER",
                        "event_type": "mf.implementation",
                        "event_kind": "implementation",
                        "status": "accepted",
                        "route_waiver": _route_waiver(
                            "task_timeline_append",
                            "BUG-TL-MF-PARALLEL-WAIVER",
                        ),
                    },
                    method="POST",
                )
            )

        self.assertEqual(raised.exception.code, "route_token_required")
        self.assertEqual(
            raised.exception.details["fault_domain"],
            "caller_missing_route_evidence",
        )
        self.assertTrue(raised.exception.details["waiver_evidence_only"])
        self.assertIn(
            "bounded_implementation_worker_dispatch",
            raised.exception.details["required_before_protected_evidence"],
        )
        self.assertIn(
            "mf_subagent_startup",
            raised.exception.details["required_before_protected_evidence"],
        )
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ?",
            ("BUG-TL-MF-PARALLEL-WAIVER",),
        ).fetchone()["c"]
        self.assertEqual(count, 0)

    def test_mf_parallel_waiver_evidence_does_not_satisfy_close_precheck(self):
        from agent.governance import server

        bug_id = "BUG-TL-MF-PARALLEL-WAIVER-EVIDENCE"
        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": bug_id,
        }
        self.conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, chain_trigger_json, mf_type, created_at, updated_at)
               VALUES (?, ?, 'OPEN', 'P0', ?, 'chain_rescue', '2026-05-29T00:00:00Z', '2026-05-29T00:00:00Z')""",
            (bug_id, "High-risk waiver evidence test", json.dumps(contract)),
        )
        self.conn.commit()

        waiver_result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": bug_id,
                    "event_type": "route.waiver.recorded",
                    "event_kind": "route_waiver",
                    "phase": "route_gate",
                    "status": "accepted",
                    "route_waiver": _route_waiver(
                        "task_timeline_append",
                        bug_id,
                    ),
                },
                method="POST",
            )
        )
        self.assertEqual(waiver_result["event_kind"], "route_waiver")
        self.assertIn("route_waiver", waiver_result["payload"])

        precheck = server.handle_backlog_timeline_gate(
            _ctx(
                path_params={"project_id": "proj", "bug_id": bug_id},
                query={"include_events": "true"},
            )
        )

        self.assertFalse(precheck["can_close"], precheck)
        self.assertIn(
            "bounded_implementation_worker_dispatch",
            precheck["timeline_gate"]["route_context_gate"]["missing_requirement_ids"],
        )
        self.assertIn(
            "mf_subagent_startup",
            precheck["timeline_gate"]["route_context_gate"]["missing_requirement_ids"],
        )

    def test_mf_parallel_timeline_accepts_valid_route_token_for_protected_evidence(self):
        from agent.governance import server

        self._insert_router_backlog("BUG-TL-MF-PARALLEL-TOKEN")
        result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": "BUG-TL-MF-PARALLEL-TOKEN",
                    "event_type": "mf.implementation",
                    "event_kind": "implementation",
                    "status": "accepted",
                    "route_token": _route_token(
                        "task_timeline_append",
                        "BUG-TL-MF-PARALLEL-TOKEN",
                    ),
                },
                method="POST",
            )
        )

        self.assertEqual(result["route_token_gate"]["decision"], "route_token")
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ?",
            ("BUG-TL-MF-PARALLEL-TOKEN",),
        ).fetchone()["c"]
        self.assertEqual(count, 2)

    def test_mf_parallel_timeline_accepts_matching_waiver_after_bounded_evidence(self):
        from agent.governance import server

        bug_id = "BUG-TL-MF-PARALLEL-WAIVER-AFTER-EVIDENCE"
        self._insert_router_backlog(bug_id)
        self._record_route_context_consumption(bug_id)

        result = server.handle_task_timeline_append(
            _ctx(
                body={
                    "backlog_id": bug_id,
                    "event_type": "mf.implementation",
                    "event_kind": "implementation",
                    "status": "accepted",
                    "route_waiver": self._route_waiver_for_existing_identity(bug_id),
                },
                method="POST",
            )
        )

        self.assertEqual(result["route_token_gate"]["decision"], "route_waiver")
        self.assertEqual(result["event_kind"], "implementation")
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ? AND event_kind = ?",
            (bug_id, "implementation"),
        ).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_mf_parallel_timeline_rejects_waiver_identity_mismatch_after_bounded_evidence(self):
        from agent.governance import server
        from agent.governance.errors import GovernanceError

        bug_id = "BUG-TL-MF-PARALLEL-WAIVER-MISMATCH"
        self._insert_router_backlog(bug_id)
        self._record_route_context_consumption(bug_id)
        waiver = self._route_waiver_for_existing_identity(bug_id)
        waiver["route_context_hash"] = "sha256:mismatched-route-context"

        with self.assertRaises(GovernanceError) as raised:
            server.handle_task_timeline_append(
                _ctx(
                    body={
                        "backlog_id": bug_id,
                        "event_type": "mf.implementation",
                        "event_kind": "implementation",
                        "status": "accepted",
                        "route_waiver": waiver,
                    },
                    method="POST",
                )
            )

        self.assertEqual(raised.exception.code, "route_token_required")
        self.assertEqual(
            raised.exception.details["route_identity_mismatch_fields"],
            ["route_context_hash"],
        )
        count = self.conn.execute(
            "SELECT COUNT(*) AS c FROM task_timeline_events WHERE backlog_id = ? AND event_kind = ?",
            (bug_id, "implementation"),
        ).fetchone()["c"]
        self.assertEqual(count, 0)

    def test_task_claim_and_complete_write_verified_timeline(self):
        from agent.governance import task_timeline
        from agent.governance.task_registry import claim_task, complete_task, create_task

        task = create_task(
            self.conn,
            "proj",
            "implement evidence",
            task_type="dev",
            metadata={"bug_id": "BUG-TL", "mf_id": "MF-TL", "trace_id": "tr-tl"},
        )
        self.conn.commit()
        claimed, fence = claim_task(self.conn, "proj", "worker-1", caller_pid=1234)
        self.conn.commit()
        self.assertEqual(claimed["task_id"], task["task_id"])

        result = {
            "changed_files": ["agent/example.py"],
            "implementation_evidence": [
                {
                    "file": "agent/example.py",
                    "symbols": ["do_work"],
                    "change_intent": "add observable evidence",
                }
            ],
            "self_check": {
                "ready_for_gate": True,
                "tests_run": ["pytest -q agent/tests/test_task_timeline.py"],
            },
            "_artifacts": {"output_path": "shared-volume/codex-tasks/logs/output.txt"},
        }

        with mock.patch("agent.governance.auto_chain.on_task_completed", return_value=None):
            complete_task(
                self.conn,
                task["task_id"],
                status="succeeded",
                result=result,
                project_id="proj",
                completed_by="worker-1",
                fence_token=fence,
            )
        self.conn.commit()

        events = task_timeline.list_events(self.conn, "proj", task_id=task["task_id"])
        event_types = [event["event_type"] for event in events]
        self.assertIn("task.claimed", event_types)
        self.assertIn("gate.evidence.verified", event_types)
        self.assertIn("task.completed", event_types)

        gate_event = next(event for event in events if event["event_type"] == "gate.evidence.verified")
        self.assertEqual(gate_event["status"], "passed")
        self.assertTrue(gate_event["verification"]["passed"])
        self.assertEqual(gate_event["backlog_id"], "BUG-TL")

    def test_list_events_filters_by_backlog_id_without_task_id(self):
        from agent.governance import task_timeline

        task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-a",
            backlog_id="BUG-A",
            event_type="task.started",
            actor="worker-a",
        )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-b",
            backlog_id="BUG-A",
            event_type="task.completed",
            actor="worker-b",
        )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-c",
            backlog_id="BUG-B",
            event_type="task.completed",
            actor="worker-c",
        )

        events = task_timeline.list_events(self.conn, "proj", backlog_id="BUG-A")

        self.assertEqual([event["task_id"] for event in events], ["task-a", "task-b"])
        self.assertEqual({event["backlog_id"] for event in events}, {"BUG-A"})

    def test_task_completed_timeline_event_without_route_token_records_blocked_services(self):
        from agent.governance import task_timeline

        self._insert_router_backlog()

        source = task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-router",
            backlog_id="BUG-SERVICE-ROUTER",
            event_type="task.completed",
            actor="worker",
            status="succeeded",
            payload={"task_type": "dev"},
        )
        self.conn.commit()

        routed = task_timeline.list_events(
            self.conn,
            "proj",
            parent_event_id=source["id"],
            event_kind="service_route",
        )

        self.assertGreaterEqual(len(routed), 2)
        self.assertEqual({event["parent_event_id"] for event in routed}, {source["id"]})
        self.assertTrue(all(event["correlation_id"].startswith("service-route:") for event in routed))
        self.assertIn(
            "test_governance.preview",
            {event["payload"]["service_id"] for event in routed},
        )
        self.assertIn(
            "review.recommendations",
            {event["payload"]["service_id"] for event in routed},
        )
        self.assertEqual({event["event_type"] for event in routed}, {"service.route.blocked"})
        self.assertEqual(
            {event["payload"]["status"] for event in routed},
            {"route_context_token_required"},
        )

    def test_timeline_append_preserves_top_level_route_token_for_service_router(self):
        from agent.governance import server, task_timeline

        bug_id = "BUG-SERVICE-ROUTER-TOKEN"
        task_id = "task-router-token"
        self._insert_router_backlog(bug_id=bug_id)

        result = server.handle_task_timeline_append(
            _ctx(
                method="POST",
                body={
                    "task_id": task_id,
                    "backlog_id": bug_id,
                    "event_type": "task.completed",
                    "actor": "worker",
                    "status": "succeeded",
                    "payload": {"task_type": "dev"},
                    "route_token": _route_token(
                        "service_route",
                        bug_id=bug_id,
                        task_id=task_id,
                    ),
                },
            )
        )
        self.conn.commit()

        source = task_timeline.list_events(
            self.conn,
            "proj",
            task_id=task_id,
            backlog_id=bug_id,
        )[0]
        routed = task_timeline.list_events(
            self.conn,
            "proj",
            parent_event_id=result["id"],
            event_kind="service_route",
        )

        self.assertIn("route_token", source["payload"])
        self.assertGreaterEqual(len(routed), 2)
        self.assertEqual({event["event_type"] for event in routed}, {"service.route.completed"})
        self.assertEqual({event["payload"]["decision"] for event in routed}, {"allow"})
        self.assertTrue(
            all(
                event["payload"]["route_evidence"]["route_context_hash"]
                == "sha256:test-route-context-service_route"
                for event in routed
            )
        )

    def test_observer_repair_route_evidence_records_service_route_gate_inputs(self):
        from agent.governance import server, task_timeline

        bug_id = "BUG-OBSERVER-ROUTE-EVIDENCE"
        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": bug_id,
        }
        self.conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, target_files, test_files,
                acceptance_criteria, chain_trigger_json, mf_type, bypass_policy_json,
                created_at, updated_at)
               VALUES (?, ?, 'MF_IN_PROGRESS', 'P0', ?, ?, ?, ?, 'chain_rescue', ?,
                       '2026-06-02T00:00:00Z', '2026-06-02T00:00:00Z')""",
            (
                bug_id,
                "Observer repair route evidence",
                json.dumps(["agent/governance/observer_repair_run.py"]),
                json.dumps(["agent/tests/test_observer_repair_run.py"]),
                json.dumps(["record route service evidence only"]),
                json.dumps(contract),
                json.dumps({"mf_type": "chain_rescue"}),
            ),
        )
        self.conn.commit()

        dry_run = server.handle_observer_repair_run_route_evidence(
            _ctx(
                method="POST",
                body={
                    "root_backlog_ids": [bug_id],
                    "actor": "observer-test",
                    "version_check": {"ok": True, "dirty": False, "dirty_files": []},
                },
            )
        )

        self.assertFalse(dry_run["record"])
        self.assertTrue(dry_run["recordable"])
        self.assertEqual(
            [event["event_type"] for event in dry_run["source_events"]],
            ["route.prompt_context.requested", "route.action.requested"],
        )

        result = server.handle_observer_repair_run_route_evidence(
            _ctx(
                method="POST",
                body={
                    "root_backlog_ids": [bug_id],
                    "actor": "observer-test",
                    "record": True,
                    "version_check": {"ok": True, "dirty": False, "dirty_files": []},
                },
            )
        )

        self.assertTrue(result["recorded"])
        self.assertEqual(len(result["recorded_source_event_ids"]), 2)
        self.assertEqual(
            {event["payload"]["service_id"] for event in result["recorded_service_events"]},
            {"route.prompt_alert_bundle", "route.action_precheck"},
        )
        self.assertEqual(
            {event["event_type"] for event in result["recorded_service_events"]},
            {"service.route.completed"},
        )

        replay = server.handle_observer_repair_run_route_evidence(
            _ctx(
                method="POST",
                body={
                    "root_backlog_ids": [bug_id],
                    "actor": "observer-test",
                    "record": True,
                    "version_check": {"ok": True, "dirty": False, "dirty_files": []},
                },
            )
        )

        self.assertEqual(
            replay["reused_source_event_ids"],
            result["recorded_source_event_ids"],
        )
        self.assertEqual(
            replay["recorded_source_event_ids"],
            result["recorded_source_event_ids"],
        )

        events = task_timeline.list_events(self.conn, "proj", backlog_id=bug_id, limit=100)
        route_gate = task_timeline.mf_route_context_gate_verification(
            events,
            contract=contract,
        )

        self.assertEqual(
            route_gate["present_requirement_ids"],
            ["route_context", "route_action_precheck"],
        )
        self.assertEqual(
            route_gate["missing_requirement_ids"],
            [
                "bounded_implementation_worker_dispatch",
                "mf_subagent_startup",
                "independent_verification_lane",
            ],
        )

        close_gate = task_timeline.mf_close_gate_verification(events, contract=contract)
        self.assertFalse(close_gate["passed"], close_gate)
        self.assertEqual(
            close_gate["missing_event_kinds"],
            ["close_ready", "implementation", "verification"],
        )

    def test_ai_validated_timeline_route_persists_contract_evidence(self):
        from agent.governance import task_timeline

        bug_id = "BUG-AI-ROUTE-EVIDENCE"
        route_requirement = "ai_output_validated"
        self._insert_router_backlog(
            bug_id=bug_id,
            contract={
                "parallel_contract": {
                    "contract_instance_id": bug_id,
                    "service_routes": [
                        {
                            "route_id": "service.test_governance.preview",
                            "service_id": "test_governance.preview",
                            "mode": "preview",
                            "side_effect_class": "read",
                            "requirement_ids": ["service_route_checked"],
                        }
                    ],
                    "event_routes": [
                        {
                            "route_id": "event.ai_structured_output.validated",
                            "event_kind": "ai.structured_output.validated",
                            "service_route_id": "service.test_governance.preview",
                            "required_evidence_ids": [route_requirement],
                            "enabled": True,
                        }
                    ],
                }
            },
        )

        source = task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-ai-route",
            backlog_id=bug_id,
            event_type="ai.structured_output.validated",
            actor="ai-fixture",
            status="passed",
            payload={
                "producer": "fixture",
                "validated": True,
                "route_token": _route_token(
                    "service_route",
                    bug_id=bug_id,
                    task_id="task-ai-route",
                ),
            },
        )
        self.conn.commit()

        routed = task_timeline.list_events(
            self.conn,
            "proj",
            parent_event_id=source["id"],
            event_kind="service_route",
        )

        self.assertEqual(len(routed), 1)
        payload = routed[0]["payload"]
        self.assertEqual(payload["route_id"], "event.ai_structured_output.validated")
        self.assertEqual(payload["requirement_ids"], ["service_route_checked", route_requirement])
        self.assertEqual(
            [item["requirement_id"] for item in payload["contract_evidence"]],
            ["service_route_checked", route_requirement],
        )
        self.assertEqual(
            routed[0]["verification"]["contract_evidence"],
            payload["contract_evidence"],
        )

    def test_observer_reminder_echo_timeline_route_persists_safe_echo(self):
        from agent.governance import task_timeline

        bug_id = "BUG-REMINDER-ECHO"
        self._insert_router_backlog(
            bug_id=bug_id,
            contract={
                "parallel_contract": {
                    "template_id": "observer_reminder_echo_demo.v1",
                    "contract_instance_id": bug_id,
                }
            },
        )

        source = task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-reminder-echo",
            backlog_id=bug_id,
            event_type="observer.command.notified",
            actor="observer-fixture",
            status="notified",
            payload={
                "hook_reminder": {
                    "kind": "observer_command_pending",
                    "project_id": "proj",
                    "message": "pending observer commands exist; call observer_command_next",
                    "payload_included": False,
                    "next_action": {
                        "tool": "observer_command_next",
                        "description": "claim the next pending observer command",
                        "source": "nested-business-field",
                    },
                    "raw_id": "raw-1",
                    "source": "dashboard",
                    "command_type": "analyze_requirements",
                    "command_id": "cmd-1",
                },
                "route_token": _route_token(
                    "service_route",
                    bug_id=bug_id,
                    task_id="task-reminder-echo",
                ),
            },
        )
        self.conn.commit()

        routed = task_timeline.list_events(
            self.conn,
            "proj",
            parent_event_id=source["id"],
            event_kind="service_route",
        )

        self.assertEqual(len(routed), 1)
        payload = routed[0]["payload"]
        result = payload["result"]
        received_reminder = result["received_reminder"]
        echo = result["received_reminder_echo"]
        self.assertEqual(routed[0]["event_type"], "service.route.completed")
        self.assertEqual(payload["service_id"], "observer.reminder_echo")
        self.assertEqual(payload["route_id"], "event.observer_command_notified.reminder_echo")
        self.assertEqual(
            payload["requirement_ids"],
            [
                "observer_reminder_visible",
                "payload_boundary_preserved",
                "received_reminder_echo",
            ],
        )
        self.assertEqual(
            echo,
            {
                "kind": "observer_command_pending",
                "project_id": "proj",
                "message": "pending observer commands exist; call observer_command_next",
                "payload_included": False,
                "next_action": {
                    "tool": "observer_command_next",
                    "description": "claim the next pending observer command",
                },
            },
        )
        self.assertEqual(received_reminder, echo)
        result_json = json.dumps(result, sort_keys=True)
        self.assertNotIn("raw_id", result_json)
        self.assertNotIn("source", result_json)
        self.assertNotIn("nested-business-field", result_json)
        self.assertNotIn("command_type", result_json)
        self.assertNotIn("command_id", result_json)
        self.assertTrue(result["payload_boundary"]["business_payload_excluded"])

    def test_route_timeline_event_is_idempotent_for_same_source_event(self):
        from agent.governance import service_router, task_timeline

        self._insert_router_backlog()
        source = task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-router",
            backlog_id="BUG-SERVICE-ROUTER",
            event_type="task.completed",
            actor="worker",
            status="succeeded",
        )
        self.conn.commit()

        service_router.route_timeline_event(self.conn, source)
        service_router.route_timeline_event(self.conn, source)
        self.conn.commit()

        routed = task_timeline.list_events(
            self.conn,
            "proj",
            parent_event_id=source["id"],
            event_kind="service_route",
        )
        correlations = [event["correlation_id"] for event in routed]
        self.assertEqual(len(correlations), len(set(correlations)))
        self.assertEqual(len(routed), 2)

    def test_service_route_timeline_event_does_not_recurse(self):
        from agent.governance import task_timeline

        self._insert_router_backlog()
        source = task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-router",
            backlog_id="BUG-SERVICE-ROUTER",
            event_type="service.route.completed",
            phase="service_router",
            event_kind="service_route",
            actor="service-router",
            status="allowed",
            payload={"service_router_suppress": True},
            correlation_id="service-route:test",
        )
        self.conn.commit()

        children = task_timeline.list_events(
            self.conn,
            "proj",
            parent_event_id=source["id"],
        )

        self.assertEqual(children, [])

    def test_mf_process_timeline_records_queryable_test_scenario_decision(self):
        from agent.governance import task_timeline

        verification = task_timeline.mf_test_scenario_verification({
            "test_scenario_policy": "new_scenario_required",
            "test_scenario_spec": {
                "id": "scn-mf-timeline",
                "name": "MF timeline schema scenario",
                "steps": [
                    "record the observer scenario decision",
                    "record the implementation/gate result against the same scenario",
                ],
                "expected": [
                    "timeline rows are queryable by scenario and correlation",
                    "gate evidence keeps a parent pointer to the scenario decision",
                ],
            },
            "verification_notes": ["scenario was designed before implementation"],
        })
        self.assertTrue(verification["passed"], verification)

        scenario_event = task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF",
            mf_id="MF-20260523",
            task_id="task-mf",
            attempt_num=1,
            event_type="mf.test_scenario.decision",
            phase="plan",
            event_kind="scenario_spec",
            scenario_id="scn-mf-timeline",
            correlation_id="corr-mf-1",
            severity="info",
            decision="required",
            actor="observer",
            status="accepted",
            payload={
                "test_scenario_policy": "new_scenario_required",
                "test_scenario_spec": {
                    "id": "scn-mf-timeline",
                    "steps": ["record scenario", "record gate result"],
                    "expected": ["rows can be filtered"],
                },
            },
            verification=verification,
        )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF",
            mf_id="MF-20260523",
            task_id="task-mf",
            attempt_num=1,
            event_type="gate.mf_scenario.verified",
            phase="gate",
            event_kind="gate_result",
            scenario_id="scn-mf-timeline",
            parent_event_id=scenario_event["id"],
            correlation_id="corr-mf-1",
            severity="info",
            decision="approved",
            actor="gate",
            status="passed",
            verification={"passed": True, "checks": {"scenario_executed": True}},
        )
        self.conn.commit()

        events = task_timeline.list_events(
            self.conn,
            "proj",
            backlog_id="BUG-MF",
            scenario_id="scn-mf-timeline",
            correlation_id="corr-mf-1",
        )

        self.assertEqual([event["event_kind"] for event in events], ["scenario_spec", "gate_result"])
        self.assertEqual(events[0]["phase"], "plan")
        self.assertEqual(events[0]["decision"], "required")
        self.assertEqual(events[0]["schema_version"], 2)
        self.assertEqual(events[1]["parent_event_id"], scenario_event["id"])

        gate_events = task_timeline.list_events(
            self.conn,
            "proj",
            backlog_id="BUG-MF",
            scenario_id="scn-mf-timeline",
            event_kind="gate_result",
        )
        self.assertEqual(len(gate_events), 1)
        self.assertEqual(gate_events[0]["event_type"], "gate.mf_scenario.verified")

    def test_mf_test_scenario_policy_verification(self):
        from agent.governance import task_timeline

        cases = [
            (
                "none with note",
                {"test_scenario_policy": "none", "verification_notes": ["copy-only README wording"]},
                True,
            ),
            (
                "none without note",
                {"test_scenario_policy": "none"},
                False,
            ),
            (
                "reuse existing with test command",
                {
                    "test_scenario_policy": "reuse_existing",
                    "tests_run": ["pytest -q agent/tests/test_task_timeline.py"],
                },
                True,
            ),
            (
                "reuse existing without evidence",
                {"test_scenario_policy": "reuse_existing"},
                False,
            ),
            (
                "new scenario missing spec",
                {"test_scenario_policy": "new_scenario_required", "verification_notes": ["high-risk path"]},
                False,
            ),
            (
                "new scenario with spec",
                {
                    "test_scenario_policy": "new_scenario_required",
                    "test_scenario_spec": {
                        "id": "scn-new",
                        "steps": ["seed fixture", "run MF command"],
                        "expected": ["gate sees scenario evidence"],
                    },
                },
                True,
            ),
            (
                "observer configured new scenario with deferred e2e",
                {
                    "test_scenario_policy": {
                        "mode": "observer_configured",
                        "decision": "new_scenario_required",
                        "allowed_decisions": [
                            "none",
                            "reuse_existing",
                            "new_scenario_required",
                        ],
                        "reason": "contract policy behavior needs focused coverage",
                        "required_evidence_ids": [
                            "observer_test_strategy",
                            "focused_tests",
                            "contract_gate_tests",
                            "docs_policy_update",
                            "e2e_deferred_followup",
                        ],
                        "e2e_decision": "e2e_deferred",
                        "followup_backlog_id": "E2E-OBSERVER-TEST-SCENARIO-POLICY-20260524",
                    },
                    "test_scenario_spec": {
                        "id": "scn-observer-policy",
                        "steps": ["instantiate contract", "run close gate"],
                        "expected": ["required evidence ids block close until referenced"],
                    },
                },
                True,
            ),
            (
                "observer configured deferred e2e missing followup",
                {
                    "test_scenario_policy": {
                        "mode": "observer_configured",
                        "decision": "none",
                        "allowed_decisions": [
                            "none",
                            "reuse_existing",
                            "new_scenario_required",
                        ],
                        "reason": "docs-only policy wording",
                        "required_evidence_ids": ["observer_test_strategy"],
                        "e2e_decision": "e2e_deferred",
                    },
                },
                False,
            ),
        ]
        for label, payload, expected in cases:
            with self.subTest(label=label):
                result = task_timeline.mf_test_scenario_verification(payload)
                self.assertEqual(result["passed"], expected, result)
                self.assertEqual(
                    result["effective_decision"],
                    (
                        payload["test_scenario_policy"]["decision"]
                        if isinstance(payload["test_scenario_policy"], dict)
                        else payload["test_scenario_policy"]
                    ),
                )

    def test_mf_close_gate_requires_observer_execution_evidence(self):
        from agent.governance import task_timeline

        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF-GATE",
            event_type="mf.implementation.completed",
            phase="implement",
            event_kind="implementation",
            actor="observer",
            status="passed",
            payload={"changed_files": ["agent/governance/server.py"]},
        )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF-GATE",
            event_type="mf.verification.completed",
            phase="verify",
            event_kind="verification",
            actor="observer",
            status="passed",
            verification={"tests_run": ["pytest -q agent/tests/test_task_timeline.py"]},
        )
        self.conn.commit()

        events = task_timeline.list_events(self.conn, "proj", backlog_id="BUG-MF-GATE")
        blocked = task_timeline.mf_close_gate_verification(events)

        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(blocked["missing_event_kinds"], ["close_ready"])

        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF-GATE",
            event_type="mf.close_ready.accepted",
            phase="close",
            event_kind="close_ready",
            actor="observer",
            status="accepted",
            verification={"graph_reconciled": True, "preflight_ok": True},
        )
        self.conn.commit()

        ready_events = task_timeline.list_events(self.conn, "proj", backlog_id="BUG-MF-GATE")
        ready = task_timeline.mf_close_gate_verification(ready_events)

        self.assertTrue(ready["passed"], ready)
        self.assertEqual(
            ready["present_event_kinds"],
            ["close_ready", "implementation", "verification"],
        )

    def test_mf_close_gate_requires_instantiated_contract_evidence(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-MF-CONTRACT",
            "evidence_requirements": [
                {
                    "id": "backend_tests",
                    "required": True,
                    "phase": "verification",
                    "command": "pytest -q agent/tests/test_task_timeline.py",
                },
                {
                    "id": "review_queue_category_e2e",
                    "required": True,
                    "phase": "integration",
                    "kind": "e2e",
                    "command": "cd frontend/dashboard && npm run e2e:semantic -- --project fixture --probe",
                },
            ],
        }
        base_events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {
                "event_kind": "verification",
                "phase": "verification",
                "status": "passed",
                "verification": {
                    "requirement_ids": ["backend_tests"],
                    "tests_run": ["pytest -q agent/tests/test_task_timeline.py"],
                },
            },
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]

        blocked = task_timeline.mf_close_gate_verification(base_events, contract=contract)

        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(blocked["missing_event_kinds"], [])
        self.assertEqual(
            blocked["contract_gate"]["missing_requirement_ids"],
            ["review_queue_category_e2e"],
        )

        ready = task_timeline.mf_close_gate_verification(
            [
                *base_events,
                *_route_context_consumption_events(),
                _route_context_qa_verification_event(),
                {
                    "event_kind": "verification",
                    "phase": "integration",
                    "status": "passed",
                    "verification": {
                        "contract_evidence": [
                            {
                                "requirement_id": "review_queue_category_e2e",
                                "status": "passed",
                                "command": (
                                    "cd frontend/dashboard && npm run e2e:semantic "
                                    "-- --project fixture --probe"
                                ),
                            }
                        ]
                    },
                },
            ],
            contract=contract,
        )

        self.assertTrue(ready["passed"], ready)
        self.assertTrue(ready["contract_gate"]["passed"])
        self.assertTrue(ready["route_context_gate"]["passed"])
        self.assertEqual(
            ready["contract_gate"]["present_requirement_ids"],
            ["backend_tests", "review_queue_category_e2e"],
        )

    def test_mf_parallel_close_gate_requires_route_context_consumption(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-ROUTE-CONTEXT",
        }
        base_events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]

        blocked = task_timeline.mf_close_gate_verification(base_events, contract=contract)

        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(
            blocked["route_context_gate"]["missing_requirement_ids"],
            [
                "route_context",
                "route_action_precheck",
                "bounded_implementation_worker_dispatch",
                "mf_subagent_startup",
                "independent_verification_lane",
            ],
        )
        missing_groups = blocked["missing_evidence_groups"]["groups"]
        self.assertEqual(missing_groups["timeline"]["missing"], [])
        self.assertEqual(
            missing_groups["route_service"]["missing"],
            ["route_context", "route_action_precheck"],
        )
        self.assertEqual(
            missing_groups["bounded_worker"]["missing"],
            ["bounded_implementation_worker_dispatch", "mf_subagent_startup"],
        )
        self.assertEqual(
            missing_groups["independent_verification"]["missing"],
            ["independent_verification_lane"],
        )
        reminder = blocked["route_context_reminder"]
        self.assertTrue(reminder["blocked"])
        self.assertEqual(reminder["contract_template_id"], "mf_workflow_runtime.v1")
        self.assertEqual(
            reminder["allowed_stages"],
            ["dispatch", "startup_gate", "implementation_wait", "handoff_gate"],
        )
        self.assertIn("route.prompt_alert_bundle", [
            action["command"] for action in reminder["next_actions"]
        ])

        advisory_only = task_timeline.mf_close_gate_verification(
            [
                *base_events,
                {
                    "event_kind": "route_context_advisory",
                    "status": "passed",
                    "payload": {"message": "observer should dispatch a worker"},
                },
            ],
            contract=contract,
        )
        self.assertFalse(advisory_only["passed"], advisory_only)

        ordinary_verification_only = task_timeline.mf_close_gate_verification(
            [*base_events, *_route_context_consumption_events()],
            contract=contract,
        )

        self.assertFalse(ordinary_verification_only["passed"], ordinary_verification_only)
        self.assertEqual(
            ordinary_verification_only["route_context_gate"]["missing_requirement_ids"],
            ["independent_verification_lane"],
        )

        ready = task_timeline.mf_close_gate_verification(
            [
                *base_events,
                *_route_context_consumption_events(),
                _route_context_qa_verification_event(),
            ],
            contract=contract,
        )

        self.assertTrue(ready["passed"], ready)
        self.assertEqual(
            ready["route_context_gate"]["present_requirement_ids"],
            [
                "route_context",
                "route_action_precheck",
                "bounded_implementation_worker_dispatch",
                "mf_subagent_startup",
                "independent_verification_lane",
            ],
        )

    def test_route_context_gate_accepts_visible_manifest_without_prompt_contract_hash(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-ROUTE-NO-PROMPT-HASH",
        }
        events = _without_prompt_contract_hash(
            [
                *_route_context_consumption_events(),
                _route_context_qa_verification_event(),
                {
                    "event_kind": "route_waiver",
                    "phase": "pre_mutation",
                    "status": "accepted",
                    "payload": {
                        "route_waiver": {
                            "accepted": True,
                            "route_context_hash": ROUTE_IDENTITY["route_context_hash"],
                            "prompt_contract_id": ROUTE_IDENTITY["prompt_contract_id"],
                            "allowed_action": "task_timeline_append",
                            "timeline_evidence": {"event_id": "tl-route-waiver"},
                        }
                    },
                },
            ]
        )

        result = task_timeline.mf_route_context_gate_verification(events, contract)

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["missing_requirement_ids"], [])
        self.assertEqual(
            result["route_identity"],
            {
                "route_context_hash": ROUTE_IDENTITY["route_context_hash"],
                "prompt_contract_id": ROUTE_IDENTITY["prompt_contract_id"],
            },
        )

    def test_route_context_gate_ignores_route_context_without_visible_manifest(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-ROUTE-NO-MANIFEST",
        }
        events = _without_prompt_contract_hash(_route_context_consumption_events())
        events[0]["payload"].pop("visible_injection_manifest_hash", None)

        result = task_timeline.mf_route_context_gate_verification(events, contract)

        self.assertFalse(result["passed"], result)
        self.assertIn("route_context", result["missing_requirement_ids"])
        self.assertEqual(
            result["ignored_route_events"][0]["reason"],
            "missing_visible_injection_manifest",
        )

    def test_mf_parallel_close_gate_requires_matching_qa_lane(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-ROUTE-QA-LANE",
            "route_topology_policy": {
                "selected_topology": "observer_led_parallel_lanes",
                "required_lanes": [
                    "observer_coordinator",
                    "bounded_implementation_worker",
                    "independent_verification_lane",
                    "observer_merge_close_gate",
                ],
                "independent_verification_required": True,
            },
        }
        base_events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
            *_route_context_consumption_events(),
        ]

        blocked = task_timeline.mf_close_gate_verification(base_events, contract=contract)

        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(
            blocked["route_context_gate"]["missing_requirement_ids"],
            ["independent_verification_lane"],
        )

        wrong_identity = _route_context_qa_verification_event()
        wrong_identity["verification"]["prompt_contract_hash"] = "sha256:wrong"
        mismatch = task_timeline.mf_close_gate_verification(
            [*base_events, wrong_identity],
            contract=contract,
        )
        self.assertFalse(mismatch["passed"], mismatch)
        self.assertIn(
            "route_identity_mismatch",
            mismatch["route_context_gate"]["missing_requirement_ids"],
        )
        self.assertEqual(
            mismatch["missing_evidence_groups"]["groups"]["route_identity"]["missing"],
            ["route_identity_mismatch"],
        )

        ready = task_timeline.mf_close_gate_verification(
            [*base_events, _route_context_qa_verification_event()],
            contract=contract,
        )
        self.assertTrue(ready["passed"], ready)
        self.assertEqual(
            ready["route_context_gate"]["evidence_events"][
                "independent_verification_lane"
            ][0]["event_kind"],
            "qa_verification",
        )

    def test_mf_parallel_close_gate_rejects_route_identity_mismatch(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-ROUTE-MISMATCH",
        }
        events = [
            {"event_kind": "implementation", "phase": "implementation", "status": "accepted"},
            {"event_kind": "verification", "phase": "verification", "status": "passed"},
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
            *_route_context_consumption_events(),
        ]
        events[-1]["payload"]["mf_subagent_startup_gate"]["prompt_contract_hash"] = (
            "sha256:different-prompt-contract"
        )

        blocked = task_timeline.mf_close_gate_verification(events, contract=contract)

        self.assertFalse(blocked["passed"], blocked)
        self.assertIn(
            "route_identity_mismatch",
            blocked["route_context_gate"]["missing_requirement_ids"],
        )

    def test_mf_contract_gate_uses_observer_configured_required_evidence_ids(self):
        from agent.governance import task_timeline

        contract = {
            "template_id": "mf_parallel.v1",
            "contract_instance_id": "BUG-OBSERVER-POLICY",
            "evidence_requirements": [
                {
                    "id": "e2e_deferred_followup",
                    "required": False,
                    "phase": "integration",
                    "kind": "e2e_defer",
                },
            ],
            "test_scenario_policy": {
                "mode": "observer_configured",
                "decision": "new_scenario_required",
                "allowed_decisions": [
                    "none",
                    "reuse_existing",
                    "new_scenario_required",
                ],
                "reason": "observer requires contract-backed evidence",
                "required_evidence_ids": [
                    "observer_test_strategy",
                    "implementation_evidence",
                    "focused_tests",
                    "contract_gate_tests",
                    "docs_policy_update",
                    "e2e_deferred_followup",
                ],
                "e2e_decision": "e2e_deferred",
                "followup_backlog_id": "E2E-OBSERVER-TEST-SCENARIO-POLICY-20260524",
            },
        }
        base_events = [
            {
                "event_kind": "implementation",
                "phase": "implementation",
                "status": "accepted",
                "payload": {
                    "requirement_ids": [
                        "implementation_evidence",
                        "docs_policy_update",
                    ],
                },
            },
            {
                "event_kind": "verification",
                "phase": "verification",
                "status": "passed",
                "verification": {
                    "requirement_ids": [
                        "observer_test_strategy",
                        "focused_tests",
                        "contract_gate_tests",
                    ],
                },
            },
            {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
        ]

        blocked = task_timeline.mf_close_gate_verification(base_events, contract=contract)

        self.assertFalse(blocked["passed"], blocked)
        self.assertEqual(
            blocked["contract_gate"]["missing_requirement_ids"],
            ["e2e_deferred_followup"],
        )

        ready = task_timeline.mf_close_gate_verification(
            [
                *base_events,
                *_route_context_consumption_events(),
                _route_context_qa_verification_event(),
                {
                    "event_kind": "verification",
                    "phase": "integration",
                    "status": "passed",
                    "verification": {
                        "contract_evidence": [
                            {
                                "requirement_id": "e2e_deferred_followup",
                                "status": "passed",
                                "followup_backlog_id": (
                                    "E2E-OBSERVER-TEST-SCENARIO-POLICY-20260524"
                                ),
                            }
                        ]
                    },
                },
            ],
            contract=contract,
        )

        self.assertTrue(ready["passed"], ready)
        self.assertTrue(ready["route_context_gate"]["passed"])
        self.assertEqual(
            ready["contract_gate"]["present_requirement_ids"],
            [
                "contract_gate_tests",
                "docs_policy_update",
                "e2e_deferred_followup",
                "focused_tests",
                "implementation_evidence",
                "observer_test_strategy",
            ],
        )

    def test_mf_parallel_template_exposes_optional_e2e_requirement(self):
        from agent.governance import task_timeline

        template_path = (
            Path(__file__).resolve().parents[1]
            / "governance"
            / "contract_templates"
            / "mf_parallel.v1.json"
        )
        template = json.loads(template_path.read_text(encoding="utf-8"))

        requirements = task_timeline.mf_contract_requirements(template)
        by_id = {item["id"]: item for item in requirements}
        policy = template["test_scenario_policy"]

        self.assertEqual(policy["mode"], "observer_configured")
        self.assertEqual(
            policy["allowed_decisions"],
            ["none", "reuse_existing", "new_scenario_required"],
        )
        self.assertIn("observer_test_strategy", policy["required_evidence_ids"])
        self.assertIn("focused_tests", by_id)
        self.assertIn("observer_test_strategy", by_id)
        self.assertIn("contract_gate_tests", by_id)
        self.assertIn("docs_policy_update", by_id)
        self.assertIn("e2e_deferred_followup", by_id)
        self.assertIn("integration_e2e", by_id)
        self.assertTrue(by_id["observer_test_strategy"]["required"])
        self.assertTrue(by_id["focused_tests"]["required"])
        self.assertFalse(by_id["contract_gate_tests"]["required"])
        self.assertFalse(by_id["docs_policy_update"]["required"])
        self.assertFalse(by_id["e2e_deferred_followup"]["required"])
        self.assertFalse(by_id["integration_e2e"]["required"])
        self.assertEqual(by_id["integration_e2e"]["kind"], "e2e")

    def test_db_migration_from_v41_adds_timeline_v2_columns_and_indexes(self):
        from agent.governance import db

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_meta (key, value) VALUES ('schema_version', '41');
            CREATE TABLE task_timeline_events (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id           TEXT NOT NULL,
                backlog_id           TEXT NOT NULL DEFAULT '',
                mf_id                TEXT NOT NULL DEFAULT '',
                task_id              TEXT NOT NULL DEFAULT '',
                attempt_num          INTEGER NOT NULL DEFAULT 0,
                event_type           TEXT NOT NULL,
                actor                TEXT NOT NULL DEFAULT '',
                status               TEXT NOT NULL DEFAULT '',
                payload_json         TEXT NOT NULL DEFAULT '{}',
                verification_json    TEXT NOT NULL DEFAULT '{}',
                artifact_refs_json   TEXT NOT NULL DEFAULT '{}',
                trace_id             TEXT NOT NULL DEFAULT '',
                commit_sha           TEXT NOT NULL DEFAULT '',
                created_at           TEXT NOT NULL
            );
        """)

        db._ensure_schema(conn)

        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(task_timeline_events)").fetchall()
        }
        self.assertIn("phase", columns)
        self.assertIn("event_kind", columns)
        self.assertIn("scenario_id", columns)
        self.assertIn("correlation_id", columns)
        self.assertIn("schema_version", columns)
        indexes = {
            str(row["name"])
            for row in conn.execute("PRAGMA index_list(task_timeline_events)").fetchall()
        }
        self.assertIn("idx_task_timeline_scenario", indexes)
        self.assertIn("idx_task_timeline_kind", indexes)
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()["value"]
        self.assertEqual(version, str(db.SCHEMA_VERSION))
        conn.close()

    def test_task_timeline_list_handler_filters_by_backlog_id_query(self):
        from agent.governance import server, task_timeline

        task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-a",
            backlog_id="BUG-A",
            event_type="task.started",
            actor="worker-a",
            trace_id="trace-a",
            phase="implement",
            event_kind="observation",
            scenario_id="scn-handler",
            correlation_id="corr-handler",
        )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-b",
            backlog_id="BUG-A",
            event_type="task.completed",
            actor="worker-b",
            trace_id="trace-b",
            phase="gate",
            event_kind="gate_result",
            scenario_id="scn-handler",
            correlation_id="corr-handler",
            decision="approved",
        )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            task_id="task-c",
            backlog_id="BUG-B",
            event_type="task.completed",
            actor="worker-c",
            trace_id="trace-c",
        )
        self.conn.commit()

        result = server.handle_task_timeline_list(_ctx({"backlog_id": "BUG-A"}))

        self.assertTrue(result["ok"])
        self.assertEqual(result["project_id"], "proj")
        self.assertEqual(result["task_id"], "")
        self.assertEqual(result["backlog_id"], "BUG-A")
        self.assertEqual(result["trace_id"], "")
        self.assertEqual(result["count"], 2)
        self.assertEqual(
            [event["task_id"] for event in result["events"]],
            ["task-a", "task-b"],
        )

        filtered = server.handle_task_timeline_list(
            _ctx({
                "backlog_id": "BUG-A",
                "task_id": "task-b",
                "trace_id": "trace-b",
                "phase": "gate",
                "event_kind": "gate_result",
                "scenario_id": "scn-handler",
                "correlation_id": "corr-handler",
                "decision": "approved",
                "limit": ["5"],
            })
        )
        self.assertEqual(filtered["task_id"], "task-b")
        self.assertEqual(filtered["trace_id"], "trace-b")
        self.assertEqual(filtered["phase"], "gate")
        self.assertEqual(filtered["event_kind"], "gate_result")
        self.assertEqual(filtered["scenario_id"], "scn-handler")
        self.assertEqual(filtered["correlation_id"], "corr-handler")
        self.assertEqual(filtered["decision"], "approved")
        self.assertEqual(filtered["count"], 1)
        self.assertEqual(filtered["events"][0]["task_id"], "task-b")

    def test_backlog_timeline_gate_precheck_matches_close_gate_evidence(self):
        from agent.governance import server, task_timeline

        server.handle_backlog_upsert(
            _ctx(
                path_params={"bug_id": "BUG-MF-PRECHECK"},
                body={
                    "title": "MF timeline precheck",
                    "status": "OPEN",
                    "mf_type": "observer_hotfix",
                    "force_admit": True,
                },
                method="POST",
            )
        )

        for kind in ("implementation", "verification"):
            task_timeline.record_event(
                self.conn,
                project_id="proj",
                backlog_id="BUG-MF-PRECHECK",
                event_type=f"mf.{kind}",
                phase=kind,
                event_kind=kind,
                status="accepted",
            )
        self.conn.commit()

        blocked = server.handle_backlog_timeline_gate(
            _ctx({"include_events": "true"}, path_params={"bug_id": "BUG-MF-PRECHECK"})
        )

        self.assertTrue(blocked["ok"])
        self.assertTrue(blocked["applicable"])
        self.assertFalse(blocked["can_close"])
        self.assertEqual(blocked["timeline_gate"]["missing_event_kinds"], ["close_ready"])
        self.assertEqual(blocked["event_count"], 2)
        self.assertEqual(len(blocked["events"]), 2)

        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF-PRECHECK",
            event_type="mf.close_ready",
            phase="close",
            event_kind="close_ready",
            status="accepted",
        )
        self.conn.commit()

        ready = server.handle_backlog_timeline_gate(
            _ctx(path_params={"bug_id": "BUG-MF-PRECHECK"})
        )
        self.assertTrue(ready["can_close"])
        self.assertTrue(ready["timeline_gate"]["passed"])
        self.assertEqual(
            ready["timeline_gate"]["present_event_kinds"],
            ["close_ready", "implementation", "verification"],
        )

    def test_observer_hotfix_aliases_upsert_as_mf_applicable_rows(self):
        from agent.governance import server

        for index, alias in enumerate(("observer_hotfix", "observer-hotfix"), start=1):
            bug_id = f"BUG-MF-ALIAS-{index}"
            server.handle_backlog_upsert(
                _ctx(
                    path_params={"bug_id": bug_id},
                    body={
                        "title": "MF alias precheck",
                        "status": "OPEN",
                        "mf_type": alias,
                        "force_admit": True,
                    },
                    method="POST",
                )
            )

            row = self.conn.execute(
                "SELECT mf_type, bypass_policy_json FROM backlog_bugs WHERE bug_id = ?",
                (bug_id,),
            ).fetchone()
            self.assertEqual(row["mf_type"], "chain_rescue")
            self.assertIn("chain_rescue", row["bypass_policy_json"])

            precheck = server.handle_backlog_timeline_gate(
                _ctx(path_params={"bug_id": bug_id})
            )
            self.assertTrue(precheck["applicable"], precheck)
            self.assertFalse(precheck["can_close"])
            self.assertEqual(
                precheck["timeline_gate"]["missing_event_kinds"],
                ["close_ready", "implementation", "verification"],
            )

    def test_backlog_timeline_gate_precheck_uses_instantiated_contract(self):
        from agent.governance import server, task_timeline

        server.handle_backlog_upsert(
            _ctx(
                path_params={"bug_id": "BUG-MF-CONTRACT-PRECHECK"},
                body={
                    "title": "MF contract precheck",
                    "status": "OPEN",
                    "mf_type": "observer_hotfix",
                    "force_admit": True,
                    "chain_trigger_json": {
                        "parallel_contract": {
                            "template_id": "mf_parallel.v1",
                            "contract_instance_id": "BUG-MF-CONTRACT-PRECHECK",
                            "evidence_requirements": [
                                {"id": "unit_tests", "required": True, "phase": "verification"},
                                {"id": "dashboard_e2e", "required": True, "phase": "integration", "kind": "e2e"},
                            ],
                        }
                    },
                },
                method="POST",
            )
        )

        for kind in ("implementation", "close_ready"):
            task_timeline.record_event(
                self.conn,
                project_id="proj",
                backlog_id="BUG-MF-CONTRACT-PRECHECK",
                event_type=f"mf.{kind}",
                phase=kind,
                event_kind=kind,
                status="accepted",
            )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF-CONTRACT-PRECHECK",
            event_type="mf.verification",
            phase="verification",
            event_kind="verification",
            status="passed",
            verification={"requirement_id": "unit_tests"},
        )
        self.conn.commit()

        blocked = server.handle_backlog_timeline_gate(
            _ctx(path_params={"bug_id": "BUG-MF-CONTRACT-PRECHECK"})
        )

        self.assertFalse(blocked["can_close"])
        self.assertEqual(
            blocked["timeline_gate"]["contract_gate"]["missing_requirement_ids"],
            ["dashboard_e2e"],
        )

        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF-CONTRACT-PRECHECK",
            event_type="mf.integration.e2e",
            phase="integration",
            event_kind="verification",
            status="passed",
            verification={
                "contract_evidence": [
                    {
                        "requirement_id": "dashboard_e2e",
                        "status": "passed",
                        "command": "npm run e2e:semantic -- --project fixture --probe",
                    }
                ]
            },
        )
        for event in [
            *_route_context_consumption_events(),
            _route_context_qa_verification_event(),
        ]:
            task_timeline.record_event(
                self.conn,
                project_id="proj",
                backlog_id="BUG-MF-CONTRACT-PRECHECK",
                event_type=f"mf.{event['event_kind']}",
                phase=event.get("phase", ""),
                event_kind=event.get("event_kind", ""),
                status=event.get("status", ""),
                payload=event.get("payload"),
                verification=event.get("verification"),
                artifact_refs=event.get("artifact_refs"),
            )
        self.conn.commit()

        ready = server.handle_backlog_timeline_gate(
            _ctx(path_params={"bug_id": "BUG-MF-CONTRACT-PRECHECK"})
        )
        self.assertTrue(ready["can_close"], ready)
        self.assertTrue(ready["timeline_gate"]["contract_gate"]["passed"])

    def test_backlog_close_handler_loads_instantiated_contract(self):
        from agent.governance import server, task_timeline
        from agent.governance.errors import GovernanceError

        server.handle_backlog_upsert(
            _ctx(
                path_params={"bug_id": "BUG-MF-CONTRACT-CLOSE"},
                body={
                    "title": "MF contract close",
                    "status": "OPEN",
                    "mf_type": "observer_hotfix",
                    "force_admit": True,
                    "chain_trigger_json": {
                        "parallel_contract": {
                            "template_id": "mf_parallel.v1",
                            "contract_instance_id": "BUG-MF-CONTRACT-CLOSE",
                            "evidence_requirements": [
                                {"id": "unit_tests", "required": True, "phase": "verification"},
                                {"id": "dashboard_e2e", "required": True, "phase": "integration", "kind": "e2e"},
                            ],
                        }
                    },
                },
                method="POST",
            )
        )

        for kind in ("implementation", "close_ready"):
            task_timeline.record_event(
                self.conn,
                project_id="proj",
                backlog_id="BUG-MF-CONTRACT-CLOSE",
                event_type=f"mf.{kind}",
                phase=kind,
                event_kind=kind,
                status="accepted",
            )
        task_timeline.record_event(
            self.conn,
            project_id="proj",
            backlog_id="BUG-MF-CONTRACT-CLOSE",
            event_type="mf.verification",
            phase="verification",
            event_kind="verification",
            status="passed",
            verification={"requirement_id": "unit_tests"},
        )
        self.conn.commit()

        with self.assertRaises(GovernanceError) as raised:
            server.handle_backlog_close(
                _ctx(
                    path_params={"bug_id": "BUG-MF-CONTRACT-CLOSE"},
                    body={
                        "actor": "observer",
                        "route_waiver": _route_waiver("backlog_close", "BUG-MF-CONTRACT-CLOSE"),
                    },
                    method="POST",
                )
            )

        self.assertEqual(raised.exception.code, "mf_timeline_gate_failed")
        self.assertIn("dashboard_e2e", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
