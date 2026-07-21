from uuid import uuid4

import pytest
from pydantic import ValidationError

from nico_agent.projects.contracts import (
    ProjectCollaborationCreate,
    ProjectMemberStateCommand,
)


def test_collaboration_create_rejects_duplicate_or_lead_member_ids() -> None:
    lead = uuid4()
    member = uuid4()
    with pytest.raises(ValidationError):
        ProjectCollaborationCreate(
            name="Duplicate team",
            goal="Ship safely",
            lead_agent_id=lead,
            member_agent_ids=[member, member],
        )
    with pytest.raises(ValidationError):
        ProjectCollaborationCreate(
            name="Lead duplicated",
            goal="Ship safely",
            lead_agent_id=lead,
            member_agent_ids=[lead],
        )


@pytest.mark.parametrize("cadence", [299, 604801])
def test_collaboration_create_bounds_supervision_cadence(cadence: int) -> None:
    with pytest.raises(ValidationError):
        ProjectCollaborationCreate(
            name="Cadence bounds",
            goal="Ship safely",
            lead_agent_id=uuid4(),
            supervision_cadence_seconds=cadence,
        )


def test_member_state_commands_require_a_reason_for_removal() -> None:
    with pytest.raises(ValidationError):
        ProjectMemberStateCommand(
            target="removed",
            expected_project_revision=1,
            expected_member_revision=1,
        )
