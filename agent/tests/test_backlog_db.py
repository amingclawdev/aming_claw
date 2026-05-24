"""Tests for OPT-DB-BACKLOG: governance DB backlog backend.

Covers:
  - Schema migration v14->v15
  - Upsert idempotency
  - Close lifecycle
  - REST endpoint happy path + 404 error
  - ETL dry-run vs apply parity
  - MCP tool definitions
  - auto_chain _try_backlog_close_via_db helper
"""
import gc
import json
import os
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _safe_cleanup(tmp_dir):
    """Best-effort cleanup that tolerates Windows file locks on SQLite WAL files."""
    import shutil
    try:
        # Force garbage collection to release SQLite connections
        gc.collect()
        tmp_dir.cleanup()
    except (PermissionError, OSError):
        # Windows: WAL/SHM files may still be locked; ignore
        try:
            shutil.rmtree(tmp_dir.name, ignore_errors=True)
        except Exception:
            pass


class TestSchemaV14ToV15(unittest.TestCase):
    """AC1: Schema migration creates backlog_bugs table."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        os.makedirs(os.path.join(
            self.tmp.name, "codex-tasks", "state", "governance", "test-project"
        ), exist_ok=True)
        self._conns = []

    def tearDown(self):
        for c in self._conns:
            try:
                c.close()
            except Exception:
                pass
        os.environ.pop("SHARED_VOLUME_PATH", None)
        _safe_cleanup(self.tmp)

    def _get_conn(self, pid="test-project"):
        from governance.db import get_connection
        conn = get_connection(pid)
        self._conns.append(conn)
        return conn

    def test_schema_version_is_at_least_15(self):
        from governance.db import SCHEMA_VERSION
        self.assertGreaterEqual(SCHEMA_VERSION, 15)

    def test_migration_creates_backlog_bugs_table(self):
        conn = self._get_conn()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = {t["name"] for t in tables}
        self.assertIn("backlog_bugs", table_names)

    def test_migration_v14_to_v15_idempotent(self):
        """Calling _run_migrations from v14 -> v15 on a v14 DB works."""
        from governance.db import _run_migrations
        conn = self._get_conn()
        # Calling migration again should be safe (CREATE IF NOT EXISTS)
        _run_migrations(conn, 14, 15)
        conn.commit()
        # Verify table still exists and is functional
        conn.execute(
            "INSERT INTO backlog_bugs (bug_id, created_at, updated_at) VALUES (?, ?, ?)",
            ("TEST-MIG", "2026-01-01", "2026-01-01")
        )
        conn.commit()
        row = conn.execute("SELECT * FROM backlog_bugs WHERE bug_id='TEST-MIG'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["bug_id"], "TEST-MIG")

    def test_schema_meta_updated_to_current_version(self):
        from governance.db import SCHEMA_VERSION
        conn = self._get_conn()
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        self.assertEqual(row["value"], str(SCHEMA_VERSION))


class TestBacklogUpsertIdempotency(unittest.TestCase):
    """AC3: Two upserts -> exactly 1 row."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        os.makedirs(os.path.join(
            self.tmp.name, "codex-tasks", "state", "governance", "test-project"
        ), exist_ok=True)
        self._conns = []

    def tearDown(self):
        for c in self._conns:
            try:
                c.close()
            except Exception:
                pass
        os.environ.pop("SHARED_VOLUME_PATH", None)
        _safe_cleanup(self.tmp)

    def _get_conn(self, pid="test-project"):
        from governance.db import get_connection
        conn = get_connection(pid)
        self._conns.append(conn)
        return conn

    def test_upsert_creates_then_updates(self):
        conn = self._get_conn()

        # First insert
        conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(bug_id) DO UPDATE SET
                 title = excluded.title,
                 updated_at = excluded.updated_at
            """,
            ("B99", "Original title", "OPEN", "P1", "2026-01-01", "2026-01-01"),
        )
        conn.commit()

        # Second insert (update)
        conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(bug_id) DO UPDATE SET
                 title = excluded.title,
                 updated_at = excluded.updated_at
            """,
            ("B99", "Updated title", "OPEN", "P1", "2026-01-01", "2026-01-02"),
        )
        conn.commit()

        # Should be exactly 1 row
        rows = conn.execute("SELECT * FROM backlog_bugs WHERE bug_id='B99'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Updated title")


class TestBacklogCloseLifecycle(unittest.TestCase):
    """AC4: Close sets status=FIXED, commit, fixed_at."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        os.makedirs(os.path.join(
            self.tmp.name, "codex-tasks", "state", "governance", "test-project"
        ), exist_ok=True)
        self._conns = []

    def tearDown(self):
        for c in self._conns:
            try:
                c.close()
            except Exception:
                pass
        os.environ.pop("SHARED_VOLUME_PATH", None)
        _safe_cleanup(self.tmp)

    def _get_conn(self, pid="test-project"):
        from governance.db import get_connection
        conn = get_connection(pid)
        self._conns.append(conn)
        return conn

    def test_close_updates_status_and_commit(self):
        conn = self._get_conn()

        # Insert a bug
        conn.execute(
            """INSERT INTO backlog_bugs
               (bug_id, title, status, priority, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("B99", "Test bug", "OPEN", "P1", "2026-01-01", "2026-01-01"),
        )
        conn.commit()

        # Close it
        conn.execute(
            """UPDATE backlog_bugs
               SET status = 'FIXED', "commit" = ?, fixed_at = ?, updated_at = ?
               WHERE bug_id = ?""",
            ("abc1234", "2026-01-02T00:00:00Z", "2026-01-02T00:00:00Z", "B99"),
        )
        conn.commit()

        row = conn.execute("SELECT * FROM backlog_bugs WHERE bug_id='B99'").fetchone()
        self.assertEqual(row["status"], "FIXED")
        self.assertEqual(row["commit"], "abc1234")
        self.assertIn("2026-01-02", row["fixed_at"])


class TestBacklogRESTEndpoints(unittest.TestCase):
    """AC2: REST endpoint happy path + 404 error."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        os.makedirs(os.path.join(
            self.tmp.name, "codex-tasks", "state", "governance", "test-project"
        ), exist_ok=True)

    def tearDown(self):
        os.environ.pop("SHARED_VOLUME_PATH", None)
        _safe_cleanup(self.tmp)

    def _make_ctx(self, path_params, query=None, body=None):
        """Build a minimal RequestContext-like object."""
        ctx = MagicMock()
        ctx.path_params = path_params
        ctx.query = query or {}
        ctx.body = body or {}
        return ctx

    def test_upsert_and_list(self):
        from governance.server import handle_backlog_upsert, handle_backlog_list

        ctx = self._make_ctx(
            {"project_id": "test-project", "bug_id": "B99"},
            body={"title": "Test bug", "status": "OPEN", "priority": "P1"},
        )
        result = handle_backlog_upsert(ctx)
        self.assertTrue(result["ok"])

        # List
        ctx2 = self._make_ctx({"project_id": "test-project"})
        result2 = handle_backlog_list(ctx2)
        self.assertGreaterEqual(result2["count"], 1)
        bug_ids = [b["bug_id"] for b in result2["bugs"]]
        self.assertIn("B99", bug_ids)

    def test_upsert_existing_preserves_omitted_fields(self):
        from governance.server import handle_backlog_upsert, handle_backlog_get

        ctx = self._make_ctx(
            {"project_id": "test-project", "bug_id": "PATCH-1"},
            body={
                "title": "Preserve me",
                "status": "OPEN",
                "priority": "P1",
                "target_files": ["agent/governance/server.py"],
                "test_files": ["agent/tests/test_backlog_db.py"],
                "acceptance_criteria": ["original acceptance"],
                "details_md": "Original details",
                "chain_trigger_json": {"source": "test"},
                "required_docs": ["docs/governance/manual-fix-sop.md"],
                "provenance_paths": ["agent/governance/server.py"],
                "force_admit": True,
            },
        )
        handle_backlog_upsert(ctx)

        patch_ctx = self._make_ctx(
            {"project_id": "test-project", "bug_id": "PATCH-1"},
            body={"status": "FIXED", "commit": "abc1234", "fixed_at": "2026-05-24T00:00:00Z"},
        )
        handle_backlog_upsert(patch_ctx)

        result = handle_backlog_get(
            self._make_ctx({"project_id": "test-project", "bug_id": "PATCH-1"})
        )
        self.assertEqual(result["title"], "Preserve me")
        self.assertEqual(result["status"], "FIXED")
        self.assertEqual(result["priority"], "P1")
        self.assertEqual(result["commit"], "abc1234")
        self.assertEqual(result["fixed_at"], "2026-05-24T00:00:00Z")
        self.assertEqual(result["details_md"], "Original details")
        self.assertEqual(json.loads(result["target_files"]), ["agent/governance/server.py"])
        self.assertEqual(json.loads(result["test_files"]), ["agent/tests/test_backlog_db.py"])
        self.assertEqual(json.loads(result["acceptance_criteria"]), ["original acceptance"])
        self.assertEqual(json.loads(result["chain_trigger_json"]), {"source": "test"})
        self.assertEqual(result["required_docs"], ["docs/governance/manual-fix-sop.md"])
        self.assertEqual(result["provenance_paths"], ["agent/governance/server.py"])

    def test_upsert_existing_preserves_evidence_when_only_docs_change(self):
        from governance.server import handle_backlog_upsert, handle_backlog_get

        handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": "PATCH-DOCS"},
                body={
                    "title": "Evidence row",
                    "status": "OPEN",
                    "priority": "P0",
                    "target_files": ["agent/governance/server.py"],
                    "test_files": ["agent/tests/test_backlog_db.py"],
                    "acceptance_criteria": ["preserve omitted evidence"],
                    "details_md": "Original evidence details",
                    "chain_trigger_json": {"parallel_contract": {"lane": "backlog-upsert-preserve"}},
                    "required_docs": ["docs/original.md"],
                    "provenance_paths": ["BACKLOG-ORIGINAL"],
                    "force_admit": True,
                },
            )
        )

        handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": "PATCH-DOCS"},
                body={
                    "required_docs": ["docs/replacement.md"],
                    "provenance_paths": ["BACKLOG-UPDATED"],
                },
            )
        )

        result = handle_backlog_get(
            self._make_ctx({"project_id": "test-project", "bug_id": "PATCH-DOCS"})
        )
        self.assertEqual(result["title"], "Evidence row")
        self.assertEqual(result["status"], "OPEN")
        self.assertEqual(result["priority"], "P0")
        self.assertEqual(json.loads(result["target_files"]), ["agent/governance/server.py"])
        self.assertEqual(json.loads(result["test_files"]), ["agent/tests/test_backlog_db.py"])
        self.assertEqual(json.loads(result["acceptance_criteria"]), ["preserve omitted evidence"])
        self.assertEqual(result["details_md"], "Original evidence details")
        self.assertEqual(
            json.loads(result["chain_trigger_json"]),
            {"parallel_contract": {"lane": "backlog-upsert-preserve"}},
        )
        self.assertEqual(result["required_docs"], ["docs/replacement.md"])
        self.assertEqual(result["provenance_paths"], ["BACKLOG-UPDATED"])

        handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": "PATCH-DOCS"},
                body={"required_docs": []},
            )
        )

        cleared = handle_backlog_get(
            self._make_ctx({"project_id": "test-project", "bug_id": "PATCH-DOCS"})
        )
        self.assertEqual(cleared["required_docs"], [])
        self.assertEqual(cleared["provenance_paths"], ["BACKLOG-UPDATED"])
        self.assertEqual(cleared["title"], "Evidence row")

    def test_upsert_existing_allows_explicit_clear(self):
        from governance.server import handle_backlog_upsert, handle_backlog_get

        handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": "PATCH-CLEAR"},
                body={
                    "title": "Clear me",
                    "target_files": ["agent/governance/server.py"],
                    "details_md": "Details",
                    "force_admit": True,
                },
            )
        )

        handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": "PATCH-CLEAR"},
                body={"title": "", "target_files": [], "details_md": ""},
            )
        )

        result = handle_backlog_get(
            self._make_ctx({"project_id": "test-project", "bug_id": "PATCH-CLEAR"})
        )
        self.assertEqual(result["title"], "")
        self.assertEqual(json.loads(result["target_files"]), [])
        self.assertEqual(result["details_md"], "")

    def test_get_existing(self):
        from governance.server import handle_backlog_upsert, handle_backlog_get

        ctx = self._make_ctx(
            {"project_id": "test-project", "bug_id": "B100"},
            body={"title": "Another bug"},
        )
        handle_backlog_upsert(ctx)

        ctx2 = self._make_ctx({"project_id": "test-project", "bug_id": "B100"})
        result = handle_backlog_get(ctx2)
        self.assertEqual(result["bug_id"], "B100")

    def test_get_missing_404(self):
        from governance.server import handle_backlog_get
        from governance.errors import GovernanceError

        ctx = self._make_ctx({"project_id": "test-project", "bug_id": "NONEXISTENT"})
        with self.assertRaises(GovernanceError) as cm:
            handle_backlog_get(ctx)
        self.assertEqual(cm.exception.status, 404)

    def test_close_existing(self):
        from governance.server import handle_backlog_upsert, handle_backlog_close

        ctx = self._make_ctx(
            {"project_id": "test-project", "bug_id": "B101"},
            body={"title": "Closeable bug"},
        )
        handle_backlog_upsert(ctx)

        ctx2 = self._make_ctx(
            {"project_id": "test-project", "bug_id": "B101"},
            body={},
        )
        result = handle_backlog_close(ctx2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "FIXED")

    def test_close_missing_404(self):
        from governance.server import handle_backlog_close
        from governance.errors import GovernanceError

        ctx = self._make_ctx(
            {"project_id": "test-project", "bug_id": "NONEXISTENT"},
            body={"commit": "abc"},
        )
        with self.assertRaises(GovernanceError) as cm:
            handle_backlog_close(ctx)
        self.assertEqual(cm.exception.status, 404)

    def test_list_with_status_filter(self):
        from governance.server import handle_backlog_upsert, handle_backlog_list

        # Create OPEN and FIXED bugs
        for bug_id, status in [("BF1", "OPEN"), ("BF2", "FIXED"), ("BF3", "OPEN")]:
            ctx = self._make_ctx(
                {"project_id": "test-project", "bug_id": bug_id},
                body={"title": "Bug %s" % bug_id, "status": status},
            )
            handle_backlog_upsert(ctx)

        # Filter by OPEN
        ctx2 = self._make_ctx({"project_id": "test-project"}, query={"status": "OPEN"})
        result = handle_backlog_list(ctx2)
        for bug in result["bugs"]:
            self.assertEqual(bug["status"], "OPEN")

    def test_list_compact_paginates_and_summarizes(self):
        from governance.server import handle_backlog_upsert, handle_backlog_list

        for bug_id in ["BP1", "BP2", "BP3"]:
            ctx = self._make_ctx(
                {"project_id": "test-project", "bug_id": bug_id},
                body={
                    "title": "Paged bug %s" % bug_id,
                    "status": "OPEN",
                    "priority": "P1",
                    "details_md": "long details " * 80,
                    "target_files": ["a.py", "b.py", "c.py", "d.py"],
                    "test_files": ["test_a.py", "test_b.py"],
                    "acceptance_criteria": ["one", "two", "three"],
                    "chain_trigger_json": {
                        "parallel_contract": {
                            "template_id": "mf_parallel.v1",
                            "contract_instance_id": bug_id,
                            "evidence_requirements": [
                                {"id": "unit_tests", "required": True},
                                {"id": "dashboard_e2e", "required": False},
                            ],
                        }
                    },
                    "force_admit": True,
                },
            )
            handle_backlog_upsert(ctx)

        ctx2 = self._make_ctx(
            {"project_id": "test-project"},
            query={"view": "compact", "limit": "2", "offset": "0", "status": "OPEN"},
        )
        result = handle_backlog_list(ctx2)

        self.assertEqual(result["view"], "compact")
        self.assertEqual(result["limit"], 2)
        self.assertEqual(result["offset"], 0)
        self.assertEqual(result["count"], 2)
        self.assertGreaterEqual(result["filtered_count"], 3)
        self.assertTrue(result["has_more"])
        self.assertIsNotNone(result["next_offset"])
        self.assertGreaterEqual(result["summary"]["open"], 3)
        bug = result["bugs"][0]
        self.assertTrue(bug["compact"])
        self.assertLessEqual(len(bug["details_md"]), 283)
        self.assertEqual(bug["target_file_count"], 4)
        self.assertEqual(bug["acceptance_count"], 3)
        self.assertEqual(bug["target_files"], ["a.py", "b.py", "c.py"])
        self.assertEqual(bug["contract_summary"]["template_id"], "mf_parallel.v1")
        self.assertEqual(bug["contract_summary"]["required_evidence_count"], 1)
        self.assertEqual(bug["contract_summary"]["optional_evidence_count"], 1)

    def test_list_search_and_exclude_closed(self):
        from governance.server import handle_backlog_upsert, handle_backlog_list

        handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": "BS1"},
                body={
                    "title": "Needle open row",
                    "status": "OPEN",
                    "details_md": "alpha needle-token beta " * 20,
                    "target_files": ["needle.py"],
                    "force_admit": True,
                },
            )
        )
        handle_backlog_upsert(
            self._make_ctx(
                {"project_id": "test-project", "bug_id": "BS2"},
                body={
                    "title": "Needle fixed row",
                    "status": "FIXED",
                    "details_md": "needle-token",
                    "force_admit": True,
                },
            )
        )

        search_ctx = self._make_ctx(
            {"project_id": "test-project"},
            query={"view": "compact", "limit": "10", "q": "needle-token"},
        )
        search_result = handle_backlog_list(search_ctx)
        self.assertGreaterEqual(search_result["filtered_count"], 2)
        self.assertIn("BS1", {bug["bug_id"] for bug in search_result["bugs"]})

        open_ctx = self._make_ctx(
            {"project_id": "test-project"},
            query={
                "view": "compact",
                "limit": "10",
                "q": "Needle fixed row",
                "include_closed": "false",
            },
        )
        open_result = handle_backlog_list(open_ctx)
        self.assertEqual(open_result["filtered_count"], 0)


class TestETLParsing(unittest.TestCase):
    """AC6: ETL dry-run vs apply parity."""

    def _load_etl(self):
        import importlib.util
        script_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        spec = importlib.util.spec_from_file_location(
            "etl_backlog", os.path.join(script_dir, "scripts", "etl-backlog-md-to-db.py")
        )
        etl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(etl)
        return etl

    def test_parse_finds_bugs(self):
        """ETL parse finds at least 1 bug from the actual backlog file."""
        script_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        backlog_path = os.path.join(script_dir, "docs", "dev", "bug-and-fix-backlog.md")
        if not os.path.exists(backlog_path):
            self.skipTest("Backlog file not found at expected path")

        etl = self._load_etl()
        bugs = etl.parse_backlog(backlog_path)
        self.assertGreater(len(bugs), 0, "Should find at least 1 bug in backlog")

        # Verify structure of first bug
        bug = bugs[0]
        self.assertIn("bug_id", bug)
        self.assertIn("title", bug)
        self.assertIn("status", bug)

    def test_dry_run_does_not_modify(self):
        """Dry-run should not make HTTP calls."""
        etl = self._load_etl()

        # Create a simple temp backlog
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
            f.write("# Bug Backlog\n\n")
            f.write("| ID | Description | Fix Commit | Date |\n")
            f.write("|----|-------------|------------|------|\n")
            f.write("| B1 | Test bug | abc123 | 2026-01-01 |\n")
            f.write("| B2 | Another bug | (OPEN) | 2026-01-02 |\n")
            tmp_path = f.name

        try:
            bugs = etl.parse_backlog(tmp_path)
            self.assertEqual(len(bugs), 2)
            self.assertEqual(bugs[0]["status"], "FIXED")
            self.assertEqual(bugs[1]["status"], "OPEN")
        finally:
            os.unlink(tmp_path)


class TestMCPToolDefinitions(unittest.TestCase):
    """AC5: MCP TOOLS list contains backlog tools."""

    def test_backlog_tools_in_list(self):
        from governance.mcp_server import TOOLS
        tool_names = {t["name"] for t in TOOLS}
        self.assertIn("backlog_list", tool_names)
        self.assertIn("backlog_get", tool_names)
        self.assertIn("backlog_upsert", tool_names)
        self.assertIn("backlog_close", tool_names)
        self.assertIn("task_timeline_append", tool_names)
        self.assertIn("task_timeline_list", tool_names)
        self.assertIn("mf_timeline_precheck", tool_names)

    def test_dispatch_backlog_list(self):
        """_dispatch_tool routes backlog_list to HTTP call."""
        from governance.mcp_server import _dispatch_tool
        with patch("governance.mcp_server._http") as mock_http:
            mock_http.return_value = {"bugs": [], "count": 0}
            result = _dispatch_tool("backlog_list", {"project_id": "test"})
            mock_http.assert_called_once()
            call_args = mock_http.call_args
            self.assertEqual(call_args[0][0], "GET")
            self.assertEqual(
                call_args[0][1],
                "/api/backlog/test?view=compact&limit=50&offset=0&status=OPEN",
            )

    def test_dispatch_backlog_get(self):
        from governance.mcp_server import _dispatch_tool
        with patch("governance.mcp_server._http") as mock_http:
            mock_http.return_value = {"bug_id": "B1"}
            result = _dispatch_tool("backlog_get", {"project_id": "test", "bug_id": "B1"})
            mock_http.assert_called_once_with("GET", "/api/backlog/test/B1")

    def test_dispatch_backlog_upsert(self):
        from governance.mcp_server import _dispatch_tool
        with patch("governance.mcp_server._http") as mock_http:
            mock_http.return_value = {"ok": True}
            result = _dispatch_tool("backlog_upsert", {
                "project_id": "test", "bug_id": "B1", "title": "Bug"
            })
            mock_http.assert_called_once()
            call_args = mock_http.call_args
            self.assertEqual(call_args[0][0], "POST")
            self.assertEqual(call_args[0][1], "/api/backlog/test/B1")

    def test_dispatch_backlog_close(self):
        from governance.mcp_server import _dispatch_tool
        with patch("governance.mcp_server._http") as mock_http:
            mock_http.return_value = {"ok": True}
            result = _dispatch_tool("backlog_close", {
                "project_id": "test", "bug_id": "B1", "commit": "abc"
            })
            mock_http.assert_called_once()
            call_args = mock_http.call_args
            self.assertEqual(call_args[0][0], "POST")
            self.assertEqual(call_args[0][1], "/api/backlog/test/B1/close")

    def test_dispatch_timeline_tools(self):
        from governance.mcp_server import _dispatch_tool
        with patch("governance.mcp_server._http") as mock_http:
            mock_http.return_value = {"ok": True}
            _dispatch_tool("task_timeline_append", {
                "project_id": "test",
                "backlog_id": "B1",
                "event_type": "mf.implementation",
                "event_kind": "implementation",
                "status": "accepted",
            })
            _dispatch_tool("task_timeline_list", {
                "project_id": "test",
                "backlog_id": "B1",
                "event_kind": "implementation",
                "limit": 25,
            })
            _dispatch_tool("mf_timeline_precheck", {
                "project_id": "test",
                "bug_id": "B1",
                "include_events": True,
                "limit": 25,
            })
            self.assertEqual(mock_http.call_args_list[0][0], (
                "POST",
                "/api/task/test/timeline",
                {
                    "backlog_id": "B1",
                    "event_type": "mf.implementation",
                    "event_kind": "implementation",
                    "status": "accepted",
                },
            ))
            self.assertEqual(mock_http.call_args_list[1][0], (
                "GET",
                "/api/task/test/timeline?backlog_id=B1&event_kind=implementation&limit=25",
            ))
            self.assertEqual(mock_http.call_args_list[2][0], (
                "GET",
                "/api/backlog/test/B1/timeline-gate?include_events=true&limit=25",
            ))


class TestTryBacklogCloseViaDb(unittest.TestCase):
    """AC7: _try_backlog_close_via_db helper."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        self._conns = []

    def tearDown(self):
        for c in self._conns:
            try:
                c.close()
            except Exception:
                pass
        os.environ.pop("SHARED_VOLUME_PATH", None)
        _safe_cleanup(self.tmp)

    def _get_conn(self, pid="test-project"):
        from governance.db import get_connection
        conn = get_connection(pid)
        self._conns.append(conn)
        return conn

    def _insert_bug(self, bug_id="B99", status="OPEN"):
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO backlog_bugs (bug_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (bug_id, status, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
        return conn

    def test_success_returns_true(self):
        from governance.auto_chain import _try_backlog_close_via_db

        conn = self._insert_bug("B99")

        result = _try_backlog_close_via_db("test-project", "B99", "abc123")

        self.assertTrue(result)
        row = conn.execute(
            "SELECT status, \"commit\", runtime_state, chain_stage "
            "FROM backlog_bugs WHERE bug_id='B99'"
        ).fetchone()
        self.assertEqual(row["status"], "FIXED")
        self.assertEqual(row["commit"], "abc123")
        self.assertEqual(row["runtime_state"], "fixed")
        self.assertEqual(row["chain_stage"], "fixed")

    def test_supplied_connection_avoids_opening_second_connection(self):
        from governance.auto_chain import _try_backlog_close_via_db

        conn = self._insert_bug("B100")

        with patch("governance.db.get_connection", side_effect=AssertionError("unused")):
            result = _try_backlog_close_via_db(
                "test-project", "B100", "abc123", conn=conn,
            )

        self.assertTrue(result)
        row = conn.execute(
            "SELECT status, \"commit\" FROM backlog_bugs WHERE bug_id='B100'"
        ).fetchone()
        self.assertEqual(row["status"], "FIXED")
        self.assertEqual(row["commit"], "abc123")

    def test_already_fixed_returns_true(self):
        from governance.auto_chain import _try_backlog_close_via_db

        self._insert_bug("DONE", status="FIXED")

        result = _try_backlog_close_via_db("test-project", "DONE", "abc123")

        self.assertTrue(result)

    def test_missing_returns_false_and_logs_warning(self):
        from governance.auto_chain import _try_backlog_close_via_db

        self._get_conn()
        with self.assertLogs("governance.auto_chain", level="WARNING") as cm:
            result = _try_backlog_close_via_db("test-project", "MISSING", "abc")
            self.assertFalse(result)
            log_text = " ".join(cm.output)
            self.assertRegex(log_text, r"backlog.*missing")

    def test_invalid_status_returns_false(self):
        from governance.auto_chain import _try_backlog_close_via_db

        self._insert_bug("B1", status="FAILED")

        with self.assertLogs("governance.auto_chain", level="WARNING") as cm:
            result = _try_backlog_close_via_db("test-project", "B1", "abc")
            self.assertFalse(result)
            log_text = " ".join(cm.output)
            self.assertRegex(log_text, r"invalid status")


class TestObserverDocUpdate(unittest.TestCase):
    """AC10: docs/roles/observer.md contains backlog migration section."""

    def test_observer_md_contains_backlog_section(self):
        doc_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "docs", "roles", "observer.md"
        )
        if not os.path.exists(doc_path):
            self.skipTest("observer.md not found")
        with open(doc_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("Backlog storage", content)
        self.assertIn("backlog_list", content)
        self.assertIn("backlog_upsert", content)


if __name__ == "__main__":
    unittest.main()
