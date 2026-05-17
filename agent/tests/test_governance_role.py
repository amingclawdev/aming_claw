"""Tests for governance role service."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from governance.db import get_connection, close_connection
from governance import role_service
from governance.errors import AuthError, TokenExpiredError, TokenInvalidError, DuplicateRoleError
from governance.redis_client import reset_redis


class TestRoleService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SHARED_VOLUME_PATH"] = self.tmp.name
        reset_redis()

        self.project_id = "test-project"
        self.conn = get_connection(self.project_id)

    def tearDown(self):
        close_connection(self.conn)
        os.environ.pop("SHARED_VOLUME_PATH", None)
        self.tmp.cleanup()

    def test_register_dev(self):
        result = role_service.register(
            self.conn, "dev-001", self.project_id, "dev", scope=["L1.*"],
        )
        self.conn.commit()
        self.assertIn("session_id", result)
        self.assertIn("token", result)
        self.assertEqual(result["role"], "dev")
        self.assertTrue(result["token"].startswith("gov-"))

    def test_register_coordinator(self):
        result = role_service.register(
            self.conn, "coord-001", self.project_id, "coordinator",
        )
        self.conn.commit()
        self.assertEqual(result["role"], "coordinator")

    def test_register_mf_sub(self):
        result = role_service.register(
            self.conn, "mf-sub-001", self.project_id, "mf_sub", scope=["L7.*"],
        )
        self.conn.commit()
        self.assertEqual(result["role"], "mf_sub")
        self.assertTrue(result["token"].startswith("gov-"))

    def test_authenticate_valid_token(self):
        result = role_service.register(
            self.conn, "tester-001", self.project_id, "tester",
        )
        self.conn.commit()

        session = role_service.authenticate(self.conn, result["token"])
        self.assertEqual(session["role"], "tester")
        self.assertEqual(session["principal_id"], "tester-001")

    def test_authenticate_invalid_token(self):
        with self.assertRaises(TokenInvalidError):
            role_service.authenticate(self.conn, "gov-invalid-token")

    def test_authenticate_no_token(self):
        with self.assertRaises(AuthError):
            role_service.authenticate(self.conn, "")

    def test_duplicate_role_different_role(self):
        role_service.register(self.conn, "agent-001", self.project_id, "dev")
        self.conn.commit()
        with self.assertRaises(DuplicateRoleError):
            role_service.register(self.conn, "agent-001", self.project_id, "tester")

    def test_re_register_same_role_refreshes(self):
        r1 = role_service.register(self.conn, "agent-001", self.project_id, "dev")
        self.conn.commit()
        r2 = role_service.register(self.conn, "agent-001", self.project_id, "dev")
        self.conn.commit()
        self.assertEqual(r1["session_id"], r2["session_id"])
        self.assertTrue(r2.get("refreshed"))
        # New token should be different
        self.assertNotEqual(r1["token"], r2["token"])

    def test_heartbeat(self):
        result = role_service.register(self.conn, "agent-001", self.project_id, "dev")
        self.conn.commit()
        hb = role_service.heartbeat(self.conn, result["session_id"])
        self.assertEqual(hb["status"], "active")
        self.assertIn("server_time", hb)

    def test_deregister(self):
        result = role_service.register(self.conn, "agent-001", self.project_id, "dev")
        self.conn.commit()
        dr = role_service.deregister(self.conn, result["session_id"])
        self.conn.commit()
        self.assertEqual(dr["status"], "deregistered")

        # Token should no longer work
        with self.assertRaises((TokenExpiredError, TokenInvalidError)):
            role_service.authenticate(self.conn, result["token"])

    def test_list_sessions(self):
        role_service.register(self.conn, "dev-001", self.project_id, "dev")
        role_service.register(self.conn, "tester-001", self.project_id, "tester")
        self.conn.commit()
        sessions = role_service.list_sessions(self.conn, self.project_id)
        self.assertEqual(len(sessions), 2)

    def test_get_active_roles(self):
        role_service.register(self.conn, "dev-001", self.project_id, "dev")
        role_service.register(self.conn, "tester-001", self.project_id, "tester")
        self.conn.commit()
        roles = role_service.get_active_roles(self.conn, self.project_id)
        self.assertIn("dev", roles)
        self.assertIn("tester", roles)

    def test_check_role_available(self):
        self.assertFalse(role_service.check_role_available(self.conn, self.project_id, "tester"))
        role_service.register(self.conn, "tester-001", self.project_id, "tester")
        self.conn.commit()
        self.assertTrue(role_service.check_role_available(self.conn, self.project_id, "tester"))


if __name__ == "__main__":
    unittest.main()
