"""Explicit enumerations for the governance service.

All status/level/role comparisons use these enums — never raw string comparison.
"""

from enum import IntEnum, Enum


class VerifyLevel(IntEnum):
    """Verification depth — integer comparison, no string ordering dependency."""
    L1 = 1  # Code exists
    L2 = 2  # API callable
    L3 = 3  # UI visible
    L4 = 4  # End-to-end
    L5 = 5  # Real third-party


class VerifyStatus(Enum):
    """Verification status — explicit state machine."""
    PENDING = "pending"
    TESTING = "testing"      # Currently running tests (intermediate state)
    T2_PASS = "t2_pass"      # T1+T2 unit/API tests passed
    QA_PASS = "qa_pass"      # E2E passed (formerly "pass")
    FAILED  = "failed"
    WAIVED  = "waived"       # Manually exempted by coordinator
    SKIPPED = "skipped"      # Skipped due to unsatisfied gate
    ROLLED_BACK = "rolled_back"  # Soft-deleted via node-soft-delete (PR-C)

    @classmethod
    def from_str(cls, s: str) -> "VerifyStatus":
        """Parse from string, with legacy format support."""
        mapping = {
            "pending": cls.PENDING,
            "testing": cls.TESTING,
            "t2_pass": cls.T2_PASS,
            "T2-pass": cls.T2_PASS,    # legacy
            "verify:T2-pass": cls.T2_PASS,
            "qa_pass": cls.QA_PASS,
            "pass": cls.QA_PASS,        # legacy
            "verify:pass": cls.QA_PASS,
            "failed": cls.FAILED,
            "fail": cls.FAILED,         # legacy
            "verify:fail": cls.FAILED,
            "waived": cls.WAIVED,
            "skipped": cls.SKIPPED,
            "verify:skipped": cls.SKIPPED,
            "verify:pending": cls.PENDING,
            "rolled_back": cls.ROLLED_BACK,
        }
        normalized = s.strip().lower() if s else "pending"
        if normalized in mapping:
            return mapping[normalized]
        for key, val in mapping.items():
            if key.lower() == normalized:
                return val
        raise ValueError(f"Unknown verify status: {s!r}")


# Ordered status for gate comparison (higher = further along)
STATUS_ORDER = {
    VerifyStatus.PENDING: 0,
    VerifyStatus.TESTING: 1,
    VerifyStatus.T2_PASS: 2,
    VerifyStatus.QA_PASS: 3,
    VerifyStatus.WAIVED:  3,  # waived is equivalent to qa_pass
    VerifyStatus.FAILED: -1,  # failed is always below minimum
    VerifyStatus.SKIPPED: -2,
    VerifyStatus.ROLLED_BACK: -3,  # soft-deleted, never blocks gates
}


def status_satisfies(current: VerifyStatus, minimum: VerifyStatus) -> bool:
    """Check if current status meets or exceeds the minimum required status."""
    return STATUS_ORDER.get(current, -1) >= STATUS_ORDER.get(minimum, 0)


class BuildStatus(Enum):
    DONE    = "impl:done"
    PARTIAL = "impl:partial"
    MISSING = "impl:missing"

    @classmethod
    def from_str(cls, s: str) -> "BuildStatus":
        mapping = {
            "impl:done": cls.DONE,
            "impl:partial": cls.PARTIAL,
            "impl:missing": cls.MISSING,
        }
        normalized = s.strip().lower() if s else "impl:missing"
        if normalized in mapping:
            return mapping[normalized]
        # Try bracket format: [impl:done]
        stripped = normalized.strip("[]")
        if stripped in mapping:
            return mapping[stripped]
        raise ValueError(f"Unknown build status: {s!r}")


class Role(Enum):
    PM          = "pm"
    DEV         = "dev"
    TESTER      = "tester"
    QA          = "qa"
    GATEKEEPER  = "gatekeeper"
    MF_SUB      = "mf_sub"
    OBSERVER    = "observer"
    COORDINATOR = "coordinator"

    @classmethod
    def from_str(cls, s: str) -> "Role":
        mapping = {r.value: r for r in cls}
        normalized = s.strip().lower() if s else ""
        if normalized in mapping:
            return mapping[normalized]
        # Also try enum name
        try:
            return cls[s.strip().upper()]
        except (KeyError, AttributeError):
            raise ValueError(f"Unknown role: {s!r}")


class SessionStatus(Enum):
    ACTIVE       = "active"
    EXPIRED      = "expired"      # Past TTL (non-coordinator) or manually expired
    DEREGISTERED = "deregistered"  # Explicitly unregistered by coordinator


class GateMode(Enum):
    AUTO        = "auto"       # Gates auto-derived from deps with verify >= L3
    EXPLICIT    = "explicit"   # Gates manually specified
    CONDITIONAL = "conditional"  # Gates with conditions
    SKIP        = "skip"       # Bypass QA/gatekeeper stages entirely
    MANUAL      = "manual"     # Require manual intervention for gates


class GatePolicy(Enum):
    DEFAULT      = "default"       # Must satisfy min_status
    RELEASE_ONLY = "release_only"  # Only checked during release
    WAIVABLE     = "waivable"      # Can be waived by coordinator


class EvidenceType(Enum):
    TEST_REPORT       = "test_report"
    E2E_REPORT        = "e2e_report"
    ERROR_LOG         = "error_log"
    COMMIT_REF        = "commit_ref"
    MANUAL_REVIEW     = "manual_review"
    BACKFILL_EVIDENCE = "backfill_evidence"


class MemoryKind(Enum):
    DECISION    = "decision"
    PITFALL     = "pitfall"
    WORKAROUND  = "workaround"
    INVARIANT   = "invariant"
    OWNERSHIP   = "ownership"
    PATTERN     = "pattern"
    API         = "api"
    STUB        = "stub"
