"""Tests for handle_backlog_close commit verification (OPT-BACKLOG-CH5).

AC6: At least 3 test functions covering real commit, fake commit, empty commit.
AC8: All tests use unittest.mock.patch to mock subprocess.run.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest


def _make_ctx(bug_id="BUG-001", commit="abc123", project_id="test-proj"):
    """Build a minimal RequestContext-like object for handle_backlog_close."""
    ctx = MagicMock()
    ctx.path_params = {"project_id": project_id, "bug_id": bug_id}
    ctx.get_project_id.return_value = project_id
    ctx.body = {
        "commit": commit,
        "actor": "test",
        "route_waiver": {
            "accepted": True,
            "waiver_type": "manual_fix",
            "allowed_action": "backlog_close",
            "project_id": project_id,
            "backlog_id": bug_id,
            "reason": "Unit test supplies explicit route gate waiver evidence.",
            "timeline_evidence": {"event_id": "test-route-gate"},
        },
    }
    return ctx


def _valid_route_token(action="backlog_close", bug_id="BUG-001", project_id="test-proj"):
    return {
        "route_context_hash": "sha256:test-route-context",
        "prompt_contract_id": "prompt-contract-backlog-close",
        "prompt_contract_hash": "sha256:test-prompt-contract",
        "caller_role": "observer",
        "allowed_action": action,
        "scope": {"project_id": project_id, "backlog_id": bug_id},
        "expires_at": "2999-01-01T00:00:00Z",
        "evidence_refs": ["timeline:test-route-token-backlog-close"],
    }


@pytest.fixture
def _mock_db():
    """Patch get_connection so SELECT returns a row and UPDATE/commit succeed."""
    with patch("agent.governance.server.get_connection") as mock_gc:
        conn = MagicMock()
        # SELECT returns a row (bug exists) with OPEN status for close eligibility
        conn.execute.return_value.fetchone.return_value = {"bug_id": "BUG-001", "status": "OPEN"}
        mock_gc.return_value = conn
        yield conn


@pytest.fixture
def _mock_audit():
    """Patch audit_service.record to no-op."""
    with patch("agent.governance.server.audit_service") as mock_audit:
        yield mock_audit


@patch("agent.governance.server.subprocess.run")
def test_close_with_real_commit(_mock_subprocess, _mock_db, _mock_audit):
    """AC1/AC5: When commit resolves (returncode=0), close succeeds."""
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    ctx = _make_ctx(commit="abc123")

    result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["status"] == "FIXED"
    _mock_subprocess.assert_called_once()
    call_args = _mock_subprocess.call_args
    assert "git" in call_args[0][0]
    assert "rev-parse" in call_args[0][0]
    assert "--verify" in call_args[0][0]
    assert "abc123" in call_args[0][0]


@patch("agent.governance.server.subprocess.run")
def test_backlog_close_without_route_token_or_waiver_is_blocked(_mock_subprocess, _mock_db, _mock_audit):
    """Protected backlog_close rejects callers that provide no route gate evidence."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    ctx = _make_ctx(commit="abc123")
    ctx.body.pop("route_waiver")

    with pytest.raises(GovernanceError) as exc_info:
        handle_backlog_close(ctx)

    assert exc_info.value.code == "route_token_required"
    assert exc_info.value.status == 422


@patch("agent.governance.server.subprocess.run")
def test_backlog_close_accepts_valid_route_token(_mock_subprocess, _mock_db, _mock_audit):
    """Protected backlog_close accepts public route-token evidence through the HTTP body."""
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    ctx = _make_ctx(commit="abc123")
    ctx.body.pop("route_waiver")
    ctx.body["route_token"] = _valid_route_token()

    result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["route_token_gate"]["decision"] == "route_token"
    assert result["route_token_gate"]["scope"]["backlog_id"] == "BUG-001"


@patch("agent.governance.server.subprocess.run")
def test_close_with_fake_commit(_mock_subprocess, _mock_db, _mock_audit):
    """AC2: When commit doesn't resolve (returncode!=0), raise 422 commit_not_found."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=1, stderr="fatal: not a valid object")
    ctx = _make_ctx(commit="deadbeef999")

    with pytest.raises(GovernanceError) as exc_info:
        handle_backlog_close(ctx)

    assert exc_info.value.code == "commit_not_found"
    assert exc_info.value.status == 422


@patch("agent.governance.server.subprocess.run")
def test_close_with_empty_commit(_mock_subprocess, _mock_db, _mock_audit):
    """AC5: When commit is empty string, skip verification entirely."""
    from agent.governance.server import handle_backlog_close

    ctx = _make_ctx(commit="")

    result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["status"] == "FIXED"
    _mock_subprocess.assert_not_called()


@patch("agent.governance.server.subprocess.run")
def test_close_with_timeout(_mock_subprocess, _mock_db, _mock_audit):
    """AC3: When git times out, log warning and allow close."""
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)
    ctx = _make_ctx(commit="abc123")

    result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["status"] == "FIXED"


@patch("agent.governance.server.subprocess.run")
def test_close_with_git_not_found(_mock_subprocess, _mock_db, _mock_audit):
    """AC4: When git binary not found, log warning and allow close."""
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.side_effect = FileNotFoundError("git not found")
    ctx = _make_ctx(commit="abc123")

    result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["status"] == "FIXED"


@patch("agent.governance.server.subprocess.run")
def test_mf_close_without_timeline_evidence_is_blocked(_mock_subprocess, _mock_db, _mock_audit):
    """Observer/MF close requires implementation, verification, and close-ready timeline rows."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    _mock_db.execute.return_value.fetchone.return_value = {
        "bug_id": "BUG-001",
        "status": "OPEN",
        "mf_type": "observer_hotfix",
        "bypass_policy_json": "{}",
        "chain_stage": "",
    }
    ctx = _make_ctx(commit="abc123")

    with patch("agent.governance.task_timeline.list_events", return_value=[]):
        with pytest.raises(GovernanceError) as exc_info:
            handle_backlog_close(ctx)

    assert exc_info.value.code == "mf_timeline_gate_failed"
    assert exc_info.value.status == 422


@patch("agent.governance.server.subprocess.run")
def test_mf_like_policy_alias_close_cannot_skip_timeline_gate(_mock_subprocess, _mock_db, _mock_audit):
    """MF-like observer-hotfix policy rows must not close as ordinary backlog rows."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    _mock_db.execute.return_value.fetchone.return_value = {
        "bug_id": "BUG-001",
        "status": "OPEN",
        "mf_type": "",
        "bypass_policy_json": '{"mf_type": "observer-hotfix"}',
        "chain_stage": "",
    }
    ctx = _make_ctx(commit="abc123")

    with patch("agent.governance.task_timeline.list_events", return_value=[]):
        with pytest.raises(GovernanceError) as exc_info:
            handle_backlog_close(ctx)

    assert exc_info.value.code == "mf_timeline_gate_failed"
    assert exc_info.value.status == 422


@patch("agent.governance.server.subprocess.run")
def test_mf_close_with_required_timeline_evidence_passes(_mock_subprocess, _mock_db, _mock_audit):
    """Required observer/MF timeline evidence is returned in the close response."""
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    _mock_db.execute.return_value.fetchone.return_value = {
        "bug_id": "BUG-001",
        "status": "OPEN",
        "mf_type": "observer_hotfix",
        "bypass_policy_json": "{}",
        "chain_stage": "",
    }
    events = [
        {"event_kind": "implementation", "phase": "implement", "status": "passed"},
        {"event_kind": "verification", "phase": "verify", "status": "passed"},
        {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
    ]
    ctx = _make_ctx(commit="abc123")

    with patch("agent.governance.task_timeline.list_events", return_value=events):
        result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["timeline_gate"]["passed"] is True
    assert result["timeline_gate"]["present_event_kinds"] == [
        "close_ready",
        "implementation",
        "verification",
    ]


@patch("agent.governance.server.subprocess.run")
def test_mf_close_instantiated_contract_missing_e2e_is_blocked(_mock_subprocess, _mock_db, _mock_audit):
    """Instantiated MF contracts can require specific timeline evidence before close."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    _mock_db.execute.return_value.fetchone.return_value = {
        "bug_id": "BUG-001",
        "status": "OPEN",
        "mf_type": "observer_hotfix",
        "bypass_policy_json": "{}",
        "chain_stage": "",
        "chain_trigger_json": {
            "parallel_contract": {
                "template_id": "mf_parallel.v1",
                "contract_instance_id": "BUG-001",
                "evidence_requirements": [
                    {"id": "unit_tests", "required": True, "phase": "verification"},
                    {"id": "dashboard_e2e", "required": True, "phase": "integration", "kind": "e2e"},
                ],
            }
        },
    }
    events = [
        {"event_kind": "implementation", "phase": "implementation", "status": "passed"},
        {
            "event_kind": "verification",
            "phase": "verification",
            "status": "passed",
            "verification": {"requirement_id": "unit_tests"},
        },
        {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
    ]
    ctx = _make_ctx(commit="abc123")

    with patch("agent.governance.task_timeline.list_events", return_value=events):
        with pytest.raises(GovernanceError) as exc_info:
            handle_backlog_close(ctx)

    assert exc_info.value.code == "mf_timeline_gate_failed"
    assert "dashboard_e2e" in str(exc_info.value)


@patch("agent.governance.server.subprocess.run")
def test_mf_close_instantiated_contract_evidence_passes(_mock_subprocess, _mock_db, _mock_audit):
    """Contract requirement evidence is returned in the close response."""
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    _mock_db.execute.return_value.fetchone.return_value = {
        "bug_id": "BUG-001",
        "status": "OPEN",
        "mf_type": "observer_hotfix",
        "bypass_policy_json": "{}",
        "chain_stage": "",
        "chain_trigger_json": {
            "parallel_contract": {
                "template_id": "mf_parallel.v1",
                "contract_instance_id": "BUG-001",
                "evidence_requirements": [
                    {"id": "unit_tests", "required": True, "phase": "verification"},
                    {"id": "dashboard_e2e", "required": True, "phase": "integration", "kind": "e2e"},
                ],
            }
        },
    }
    events = [
        {"event_kind": "implementation", "phase": "implementation", "status": "passed"},
        {
            "event_kind": "verification",
            "phase": "verification",
            "status": "passed",
            "verification": {"requirement_id": "unit_tests"},
        },
        {
            "event_kind": "verification",
            "phase": "integration",
            "status": "passed",
            "verification": {
                "contract_evidence": [
                    {"requirement_id": "dashboard_e2e", "status": "passed", "command": "npm run e2e"}
                ]
            },
        },
        {"event_kind": "close_ready", "phase": "close", "status": "accepted"},
    ]
    ctx = _make_ctx(commit="abc123")

    with patch("agent.governance.task_timeline.list_events", return_value=events):
        result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["timeline_gate"]["contract_gate"]["passed"] is True
    assert result["timeline_gate"]["contract_gate"]["present_requirement_ids"] == [
        "dashboard_e2e",
        "unit_tests",
    ]


@patch("agent.governance.server.subprocess.run")
def test_mf_close_timeline_gate_explicit_bypass_requires_reason(_mock_subprocess, _mock_db, _mock_audit):
    """Emergency bypass is explicit and records timeline evidence."""
    from agent.governance.errors import GovernanceError
    from agent.governance.server import handle_backlog_close

    _mock_subprocess.return_value = MagicMock(returncode=0)
    _mock_db.execute.return_value.fetchone.return_value = {
        "bug_id": "BUG-001",
        "status": "OPEN",
        "mf_type": "system_recovery",
        "bypass_policy_json": "{}",
        "chain_stage": "",
    }
    ctx = _make_ctx(commit="abc123")
    ctx.body["bypass_timeline_gate"] = True

    with pytest.raises(GovernanceError) as exc_info:
        handle_backlog_close(ctx)

    assert exc_info.value.code == "mf_timeline_bypass_reason_required"

    ctx.body["timeline_bypass_reason"] = "system recovery bootstrap could not write normal timeline"
    with patch("agent.governance.task_timeline.record_event") as record_event:
        result = handle_backlog_close(ctx)

    assert result["ok"] is True
    assert result["timeline_gate"]["status"] == "bypassed"
    assert record_event.call_count == 2
