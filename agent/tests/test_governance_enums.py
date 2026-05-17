"""Tests for governance enums."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from governance.enums import (
    VerifyLevel, VerifyStatus, BuildStatus, Role,
    SessionStatus, GateMode, GatePolicy, EvidenceType, MemoryKind,
    STATUS_ORDER, status_satisfies,
)


class TestVerifyLevel(unittest.TestCase):
    def test_integer_comparison(self):
        self.assertLess(VerifyLevel.L1, VerifyLevel.L3)
        self.assertGreater(VerifyLevel.L5, VerifyLevel.L2)
        self.assertEqual(VerifyLevel.L4, 4)

    def test_no_string_comparison_needed(self):
        # This was a bug in v1: "L3" > "L10" is True in string comparison
        self.assertGreater(VerifyLevel.L5, VerifyLevel.L1)


class TestVerifyStatus(unittest.TestCase):
    def test_from_str_standard(self):
        self.assertEqual(VerifyStatus.from_str("pending"), VerifyStatus.PENDING)
        self.assertEqual(VerifyStatus.from_str("t2_pass"), VerifyStatus.T2_PASS)
        self.assertEqual(VerifyStatus.from_str("qa_pass"), VerifyStatus.QA_PASS)

    def test_from_str_legacy(self):
        self.assertEqual(VerifyStatus.from_str("T2-pass"), VerifyStatus.T2_PASS)
        self.assertEqual(VerifyStatus.from_str("pass"), VerifyStatus.QA_PASS)
        self.assertEqual(VerifyStatus.from_str("fail"), VerifyStatus.FAILED)
        self.assertEqual(VerifyStatus.from_str("verify:pass"), VerifyStatus.QA_PASS)
        self.assertEqual(VerifyStatus.from_str("verify:T2-pass"), VerifyStatus.T2_PASS)

    def test_from_str_invalid(self):
        with self.assertRaises(ValueError):
            VerifyStatus.from_str("not_a_status")

    def test_status_satisfies(self):
        self.assertTrue(status_satisfies(VerifyStatus.QA_PASS, VerifyStatus.T2_PASS))
        self.assertTrue(status_satisfies(VerifyStatus.T2_PASS, VerifyStatus.T2_PASS))
        self.assertFalse(status_satisfies(VerifyStatus.PENDING, VerifyStatus.T2_PASS))
        self.assertFalse(status_satisfies(VerifyStatus.FAILED, VerifyStatus.PENDING))
        self.assertTrue(status_satisfies(VerifyStatus.WAIVED, VerifyStatus.QA_PASS))


class TestBuildStatus(unittest.TestCase):
    def test_from_str(self):
        self.assertEqual(BuildStatus.from_str("impl:done"), BuildStatus.DONE)
        self.assertEqual(BuildStatus.from_str("[impl:partial]"), BuildStatus.PARTIAL)

    def test_from_str_invalid(self):
        with self.assertRaises(ValueError):
            BuildStatus.from_str("xyz")


class TestRole(unittest.TestCase):
    def test_from_str(self):
        self.assertEqual(Role.from_str("dev"), Role.DEV)
        self.assertEqual(Role.from_str("TESTER"), Role.TESTER)
        self.assertEqual(Role.from_str("mf_sub"), Role.MF_SUB)
        self.assertEqual(Role.from_str("MF_SUB"), Role.MF_SUB)
        self.assertEqual(Role.from_str("coordinator"), Role.COORDINATOR)

    def test_from_str_invalid(self):
        with self.assertRaises(ValueError):
            Role.from_str("admin")


if __name__ == "__main__":
    unittest.main()
