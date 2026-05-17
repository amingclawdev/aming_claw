"""Role permission matrix and scope checking.

State transitions are governed by an explicit state machine table using enums.
Scope checking validates that the session's scope covers the target nodes.
"""

import fnmatch

from .enums import VerifyStatus, Role
from .errors import (
    PermissionDeniedError, ForbiddenTransitionError,
    InvalidTransitionError, ScopeViolationError,
)


# --- Capability Classes ---

OPERATOR_ROLES = {Role.OBSERVER.value, Role.COORDINATOR.value}
MF_SUBAGENT_ROLES = {Role.MF_SUB.value}


def session_role(session: dict | None) -> str:
    """Return the normalized role string from an authenticated session."""
    if not isinstance(session, dict):
        return ""
    return str(session.get("role") or "").strip().lower()


def require_operator_capability(session: dict | None, action: str) -> None:
    """Allow only observer/coordinator sessions to mutate governance state."""
    role = session_role(session)
    if role not in OPERATOR_ROLES:
        raise PermissionDeniedError(
            role,
            action,
            {"detail": "Graph governance state operations are observer/coordinator only"},
        )


def require_mf_subagent_capability(session: dict | None, action: str) -> None:
    """Allow a bounded MF subagent operation or an operator override."""
    role = session_role(session)
    if role in OPERATOR_ROLES or role in MF_SUBAGENT_ROLES:
        return
    raise PermissionDeniedError(
        role,
        action,
        {"detail": "MF subagent operations require mf_sub, observer, or coordinator role"},
    )


# --- State Machine Table ---

# (from_status, to_status) -> set of allowed roles
TRANSITION_RULES: dict[tuple, set] = {
    (VerifyStatus.PENDING, VerifyStatus.TESTING):   {Role.TESTER, Role.COORDINATOR},
    (VerifyStatus.PENDING, VerifyStatus.T2_PASS):   {Role.TESTER},
    (VerifyStatus.TESTING, VerifyStatus.T2_PASS):   {Role.TESTER},
    (VerifyStatus.TESTING, VerifyStatus.FAILED):    set(Role),
    (VerifyStatus.T2_PASS, VerifyStatus.QA_PASS):   {Role.QA},
    (VerifyStatus.QA_PASS, VerifyStatus.FAILED):    set(Role),
    (VerifyStatus.T2_PASS, VerifyStatus.FAILED):    set(Role),
    (VerifyStatus.PENDING, VerifyStatus.FAILED):    set(Role),
    (VerifyStatus.FAILED,  VerifyStatus.PENDING):   {Role.DEV},
    (VerifyStatus.PENDING, VerifyStatus.WAIVED):    {Role.COORDINATOR},
    (VerifyStatus.FAILED,  VerifyStatus.WAIVED):    {Role.COORDINATOR},
    (VerifyStatus.SKIPPED, VerifyStatus.PENDING):   {Role.COORDINATOR, Role.DEV},
}

# Explicitly forbidden transitions (regardless of role)
FORBIDDEN_TRANSITIONS: set[tuple] = {
    (VerifyStatus.PENDING, VerifyStatus.QA_PASS),  # Cannot skip T2
}


def check_transition(from_status: VerifyStatus, to_status: VerifyStatus, role: Role) -> None:
    """Validate a state transition. Raises on violation.

    Args:
        from_status: Current verify status of the node.
        to_status: Target verify status.
        role: Role of the session performing the transition.

    Raises:
        ForbiddenTransitionError: If the transition is explicitly forbidden.
        InvalidTransitionError: If no rule exists for the transition.
        PermissionDeniedError: If the role is not allowed for this transition.
    """
    transition = (from_status, to_status)

    if transition in FORBIDDEN_TRANSITIONS:
        raise ForbiddenTransitionError(
            from_status.value, to_status.value,
            f"Transition {from_status.value} -> {to_status.value} is forbidden (cannot skip T2)",
        )

    allowed_roles = TRANSITION_RULES.get(transition)
    if allowed_roles is None:
        raise InvalidTransitionError(from_status.value, to_status.value)

    if role not in allowed_roles:
        allowed_names = sorted(r.value for r in allowed_roles)
        raise PermissionDeniedError(
            role.value,
            f"{from_status.value} -> {to_status.value}",
            {"allowed_roles": allowed_names},
        )


# --- Scope Checking ---

def check_scope(node_id: str, scope: list[str]) -> None:
    """Validate that a node is within the session's scope.

    Args:
        node_id: Node being operated on (e.g., "L1.5").
        scope: List of glob patterns (e.g., ["L1.*", "L2.*"]).
                Empty scope means global access.

    Raises:
        ScopeViolationError: If node is outside scope.
    """
    if not scope:
        return  # Empty scope = global access

    for pattern in scope:
        if fnmatch.fnmatch(node_id, pattern):
            return

    raise ScopeViolationError(node_id, scope)


def check_nodes_scope(node_ids: list[str], scope: list[str]) -> None:
    """Validate all nodes are within scope. Raises on first violation."""
    for node_id in node_ids:
        check_scope(node_id, scope)
