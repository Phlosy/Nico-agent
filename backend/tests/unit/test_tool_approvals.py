from __future__ import annotations

import pytest
from pydantic import ValidationError

from nico_agent.domain.errors import InvalidStateTransition
from nico_agent.domain.states import (
    TOOL_APPROVAL_TRANSITIONS,
    ToolApprovalStatus,
    transition_state,
)
from nico_agent.tool_approvals.contracts import ToolApprovalDecision


def test_tool_approval_has_one_way_terminal_transitions() -> None:
    assert (
        transition_state(
            "tool_approval_request",
            ToolApprovalStatus.REQUESTED,
            ToolApprovalStatus.APPROVED,
            TOOL_APPROVAL_TRANSITIONS,
        )
        is ToolApprovalStatus.APPROVED
    )
    with pytest.raises(InvalidStateTransition):
        transition_state(
            "tool_approval_request",
            ToolApprovalStatus.APPROVED,
            ToolApprovalStatus.REJECTED,
            TOOL_APPROVAL_TRANSITIONS,
        )


def test_tool_approval_decision_requires_valid_scope_shape() -> None:
    approved = ToolApprovalDecision(
        expected_revision=1,
        decision="approve",
        allowed_scope="run",
    )
    assert approved.allowed_scope == "run"

    with pytest.raises(ValidationError):
        ToolApprovalDecision(expected_revision=1, decision="approve")
    with pytest.raises(ValidationError):
        ToolApprovalDecision(
            expected_revision=1,
            decision="reject",
            allowed_scope="once",
        )
