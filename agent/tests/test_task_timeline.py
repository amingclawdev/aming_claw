"""Tests for task implementation timeline evidence."""

import os
import sys
import tempfile
import threading
import unittest
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


class TestTaskTimeline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _conn(self.tmp.name)

    def tearDown(self):
        self.conn.close()
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

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


if __name__ == "__main__":
    unittest.main()
