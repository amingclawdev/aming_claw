"""Tests for governance pre-flight self-check system."""

import json
import os
import sqlite3
import unittest
from datetime import datetime, timezone, timedelta

# Ensure agent/ is on sys.path
import sys
_agent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _agent_dir not in sys.path:
    sys.path.insert(0, _agent_dir)

from governance.preflight import (
    check_system, check_version, check_graph,
    check_coverage, check_queue, check_plugin_update_state, run_preflight,
)


def _create_test_db():
    """Create an in-memory DB with governance schema for testing."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE node_state (
        project_id TEXT NOT NULL, node_id TEXT NOT NULL,
        verify_status TEXT NOT NULL DEFAULT 'pending',
        build_status TEXT NOT NULL DEFAULT 'impl:missing',
        evidence_json TEXT, updated_by TEXT,
        updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (project_id, node_id))""")
    conn.execute("""CREATE TABLE node_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL, node_id TEXT NOT NULL,
        from_status TEXT, to_status TEXT NOT NULL,
        role TEXT NOT NULL, evidence_json TEXT,
        session_id TEXT, ts TEXT NOT NULL, version INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'created', type TEXT NOT NULL DEFAULT 'task',
        prompt TEXT, related_nodes TEXT, assigned_to TEXT,
        created_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        started_at TEXT, completed_at TEXT, result_json TEXT,
        error_message TEXT, attempt_count INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        priority INTEGER NOT NULL DEFAULT 0, metadata_json TEXT,
        retry_round INTEGER NOT NULL DEFAULT 0, parent_task_id TEXT,
        trace_id TEXT, chain_id TEXT)""")
    conn.execute("""CREATE TABLE task_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL, attempt INTEGER NOT NULL,
        worker_id TEXT, started_at TEXT, completed_at TEXT,
        status TEXT, result_json TEXT)""")
    conn.execute("""CREATE TABLE project_version (
        project_id TEXT PRIMARY KEY, chain_version TEXT NOT NULL,
        updated_at TEXT NOT NULL, updated_by TEXT,
        git_head TEXT, dirty_files TEXT, git_synced_at TEXT)""")
    conn.execute("""CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY, project_id TEXT,
        role TEXT, created_at TEXT)""")
    conn.execute("""CREATE TABLE schema_meta (
        key TEXT PRIMARY KEY, value TEXT)""")
    conn.commit()
    return conn


class TestCheckSystem(unittest.TestCase):
    def test_pass_with_all_tables(self):
        conn = _create_test_db()
        result = check_system(conn)
        self.assertEqual(result["status"], "pass")

    def test_fail_with_missing_table(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE node_state (id TEXT)")
        result = check_system(conn)
        self.assertEqual(result["status"], "fail")
        self.assertIn("missing_tables", result["details"])


class TestCheckVersion(unittest.TestCase):
    def setUp(self):
        self.conn = _create_test_db()
        self.pid = "test-proj"

    def test_pass_when_synced(self):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO project_version VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.pid, "abc123", now, "test", "abc123", "[]", now))
        self.conn.commit()
        result = check_version(self.conn, self.pid, prefer_trailer=False)
        self.assertEqual(result["status"], "pass")

    def test_fail_on_version_mismatch(self):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO project_version VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.pid, "abc123", now, "test", "def456", "[]", now))
        self.conn.commit()
        result = check_version(self.conn, self.pid, prefer_trailer=False)
        self.assertEqual(result["status"], "fail")
        self.assertIn("version_mismatch", result["details"])

    def test_warn_on_stale_sync(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        self.conn.execute(
            "INSERT INTO project_version VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.pid, "abc123", old, "test", "abc123", "[]", old))
        self.conn.commit()
        result = check_version(self.conn, self.pid, prefer_trailer=False)
        self.assertEqual(result["status"], "warn")
        self.assertIn("sync_stale_seconds", result["details"])

    def test_fail_no_row(self):
        result = check_version(self.conn, self.pid, prefer_trailer=False)
        self.assertEqual(result["status"], "fail")

    def test_trailer_source_overrides_stale_db_chain_version(self):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO project_version VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.pid, "old1234", now, "test", "abc1234", "[]", now))
        self.conn.commit()

        import governance.preflight as preflight

        old_chain_state = preflight._chain_state_from_git
        old_git_head = preflight._git_head_short
        try:
            preflight._chain_state_from_git = lambda: {
                "chain_sha": "abc1234",
                "version": "abc1234",
                "dirty_files": [],
                "source": "trailer",
            }
            preflight._git_head_short = lambda: "abc1234"
            result = check_version(self.conn, self.pid)
        finally:
            preflight._chain_state_from_git = old_chain_state
            preflight._git_head_short = old_git_head

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["details"]["source"], "trailer")
        self.assertEqual(result["details"]["legacy_chain_version"], "old1234")


class TestCheckGraph(unittest.TestCase):
    def setUp(self):
        self.conn = _create_test_db()
        self.pid = "test-proj"

    def test_pass_no_pending(self):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO node_state VALUES (?, ?, 'qa_pass', 'unknown', NULL, NULL, ?, 1)",
            (self.pid, "L1.1", now))
        self.conn.commit()
        result = check_graph(self.conn, self.pid)
        self.assertEqual(result["status"], "pass")

    def test_warn_orphan_pending(self):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO node_state VALUES (?, ?, 'pending', 'unknown', NULL, NULL, ?, 1)",
            (self.pid, "L3.1", now))
        self.conn.commit()
        result = check_graph(self.conn, self.pid)
        self.assertEqual(result["status"], "warn")
        self.assertIn("L3.1", result["details"]["orphan_pending"])

    def test_pass_pending_with_active_task(self):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO node_state VALUES (?, ?, 'pending', 'unknown', NULL, NULL, ?, 1)",
            (self.pid, "L3.1", now))
        meta = json.dumps({"related_nodes": ["L3.1"]})
        self.conn.execute(
            "INSERT INTO tasks (task_id, project_id, status, type, created_at, updated_at, metadata_json) "
            "VALUES (?, ?, 'queued', 'dev', ?, ?, ?)",
            ("t1", self.pid, now, now, meta))
        self.conn.commit()
        result = check_graph(self.conn, self.pid)
        self.assertEqual(result["status"], "pass")


class TestCheckCoverage(unittest.TestCase):
    def test_returns_result(self):
        result = check_coverage()
        self.assertIn(result["status"], ("pass", "warn"))
        if result["status"] == "warn":
            self.assertIn("unmapped_files", result["details"])


class TestCheckQueue(unittest.TestCase):
    def setUp(self):
        self.conn = _create_test_db()
        self.pid = "test-proj"

    def test_pass_empty_queue(self):
        result = check_queue(self.conn, self.pid)
        self.assertEqual(result["status"], "pass")

    def test_warn_stuck_task(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        self.conn.execute(
            "INSERT INTO tasks (task_id, project_id, status, type, created_at, updated_at) "
            "VALUES (?, ?, 'claimed', 'dev', ?, ?)",
            ("stuck-1", self.pid, old, old))
        self.conn.commit()
        result = check_queue(self.conn, self.pid)
        self.assertEqual(result["status"], "warn")
        self.assertEqual(len(result["details"]["stuck_tasks"]), 1)

    def test_pass_fresh_claimed(self):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO tasks (task_id, project_id, status, type, created_at, updated_at) "
            "VALUES (?, ?, 'claimed', 'dev', ?, ?)",
            ("fresh-1", self.pid, now, now))
        self.conn.commit()
        result = check_queue(self.conn, self.pid)
        self.assertEqual(result["status"], "pass")


class TestCheckPluginUpdateState(unittest.TestCase):
    def test_warn_when_state_missing(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            result = check_plugin_update_state(str(Path(td) / "missing.json"))

        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["details"]["update_status"], "unknown")

    def test_fail_when_restart_pending(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "plugin_id": "aming-claw@aming-claw-local",
                "update_status": "applied_pending_restart",
                "restart_required": {
                    "governance": {"required": True, "reason": "governance changed"}
                },
            }), encoding="utf-8")

            result = check_plugin_update_state(str(path))

        self.assertEqual(result["status"], "fail")
        self.assertIn("governance", result["details"]["blockers"][0])


class TestRunPreflight(unittest.TestCase):
    def setUp(self):
        self.conn = _create_test_db()
        self.pid = "test-proj"
        import governance.preflight as preflight

        self._preflight_module = preflight
        self._old_check_plugin_update_state = preflight.check_plugin_update_state
        preflight.check_plugin_update_state = lambda state_path=None: {
            "status": "pass",
            "details": {
                "state_path": "test-plugin-state.json",
                "state_exists": True,
                "update_status": "current",
                "blockers": [],
                "warnings": [],
            },
        }
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO project_version VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.pid, "abc123", now, "test", "abc123", "[]", now))
        self.conn.commit()

    def tearDown(self):
        self._preflight_module.check_plugin_update_state = self._old_check_plugin_update_state

    def test_full_report_structure(self):
        report = run_preflight(self.conn, self.pid)
        self.assertIn("ok", report)
        self.assertIn("checks", report)
        self.assertIn("blockers", report)
        self.assertIn("warnings", report)
        self.assertIn("auto_fixed", report)
        for category in ("system", "version", "graph", "coverage", "queue", "plugin_update_state"):
            self.assertIn(category, report["checks"])

    def test_ok_true_when_all_pass(self):
        report = run_preflight(self.conn, self.pid)
        # system and version should pass; coverage may warn (that's ok)
        self.assertTrue(report["ok"])

    def test_auto_fix_orphan_node(self):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO node_state VALUES (?, ?, 'pending', 'unknown', NULL, NULL, ?, 1)",
            (self.pid, "L99.1", now))
        self.conn.commit()

        report = run_preflight(self.conn, self.pid, auto_fix=True)
        self.assertTrue(len(report["auto_fixed"]) > 0)
        self.assertIn("waived", report["auto_fixed"][0])

        # Verify node is now waived
        row = self.conn.execute(
            "SELECT verify_status FROM node_state WHERE node_id='L99.1'"
        ).fetchone()
        self.assertEqual(row[0], "waived")

    def test_auto_fix_stuck_task(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat()
        self.conn.execute(
            "INSERT INTO tasks (task_id, project_id, status, type, created_at, updated_at) "
            "VALUES (?, ?, 'claimed', 'dev', ?, ?)",
            ("stuck-1", self.pid, old, old))
        self.conn.commit()

        report = run_preflight(self.conn, self.pid, auto_fix=True)
        self.assertTrue(len(report["auto_fixed"]) > 0)

        row = self.conn.execute(
            "SELECT status FROM tasks WHERE task_id='stuck-1'"
        ).fetchone()
        self.assertEqual(row[0], "failed")


if __name__ == "__main__":
    unittest.main()
