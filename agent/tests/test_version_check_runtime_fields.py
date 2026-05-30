"""Tests that /api/version-check/{pid} response contains runtime version fields."""

import json
import sqlite3
from unittest import mock

from agent.governance.server import handle_version_check  # noqa: E402


def _make_mock_conn():
    """Create an in-memory SQLite connection with project_version table."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE project_version (
            project_id TEXT PRIMARY KEY,
            chain_version TEXT,
            updated_at TEXT,
            git_head TEXT,
            dirty_files TEXT,
            git_synced_at TEXT
        )
    """)
    conn.execute(
        "INSERT INTO project_version VALUES (?, ?, ?, ?, ?, ?)",
        ("test-proj", "abc1234", "2026-01-01T00:00:00Z", "abc1234", "[]", "2026-01-01T00:00:00Z"),
    )
    conn.commit()
    return conn


class _Ctx:
    body = {}
    query = {}
    def __init__(self, pid="test-proj"):
        self._pid = pid
    def get_project_id(self):
        return self._pid


def test_version_check_contains_runtime_fields_with_row():
    """version-check response with a DB row must contain runtime version keys."""
    with mock.patch("agent.governance.server.get_connection", return_value=_make_mock_conn()), \
         mock.patch("agent.governance.server._utc_now", return_value="2026-01-01T00:00:00Z"):
        result = handle_version_check(_Ctx())
    assert "gov_runtime_version" in result
    assert "sm_runtime_version" in result
    assert "runtime_match" in result
    assert "project_root" in result
    assert "target_project_version" in result
    assert "governance_runtime" in result
    assert isinstance(result["runtime_match"], bool)


def test_version_check_contains_runtime_fields_no_row():
    """version-check response without a DB row must also contain runtime version keys."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE project_version (project_id TEXT PRIMARY KEY, chain_version TEXT, updated_at TEXT, git_head TEXT, dirty_files TEXT, git_synced_at TEXT)")
    conn.commit()
    with mock.patch("agent.governance.server.get_connection", return_value=conn), \
         mock.patch("agent.governance.server._utc_now", return_value="2026-01-01T00:00:00Z"):
        result = handle_version_check(_Ctx("no-such-project"))
    assert "gov_runtime_version" in result
    assert "sm_runtime_version" in result
    assert "runtime_match" in result
    assert "target_project_version" in result
    assert "governance_runtime" in result


def test_runtime_match_with_full_chain_short_runtime():
    """runtime_match=True when gov_runtime is 7-char prefix of full chain_version."""
    full_sha = "a" * 40
    short = full_sha[:7]
    mc = _make_mock_conn()
    mc.execute("UPDATE project_version SET chain_version=?, git_head=?", (full_sha, full_sha))
    mc.commit()
    fake_resp = mock.MagicMock()
    fake_resp.read.return_value = json.dumps({"runtime_version": short}).encode()
    fake_resp.__enter__ = lambda s: s
    fake_resp.__exit__ = mock.Mock(return_value=False)
    with mock.patch("agent.governance.server.get_connection", return_value=mc), \
         mock.patch("agent.governance.server._utc_now", return_value="2026-01-01T00:00:00Z"), \
         mock.patch("agent.governance.chain_trailer.get_chain_state", side_effect=Exception("no trailer")), \
         mock.patch("agent.governance.chain_trailer.get_runtime_version", return_value=short), \
         mock.patch("urllib.request.urlopen", return_value=fake_resp):
        result = handle_version_check(_Ctx())
    assert result["runtime_match"] is True
