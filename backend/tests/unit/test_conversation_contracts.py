from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from nico_agent.conversations.contracts import (
    ConversationCreate,
    ConversationPatch,
    ConversationTurnCreate,
    ConversationTurnRetry,
)
from nico_agent.domain.states import ConversationStatus, ConversationTurnStatus


def test_conversation_contracts_are_bounded_and_generate_idempotency_keys() -> None:
    command = ConversationCreate(project_id=uuid4(), agent_id=uuid4())
    turn = ConversationTurnCreate(user_input="hello")

    assert command.title == "New conversation"
    assert command.idempotency_key
    assert turn.idempotency_key
    assert ConversationStatus.ACTIVE.value == "active"
    assert ConversationTurnStatus.WAITING_FOR_APPROVAL.value == "waiting_for_approval"


def test_conversation_patch_requires_a_real_change() -> None:
    with pytest.raises(ValidationError):
        ConversationPatch(expected_revision=1)


def test_turn_rejects_empty_and_unbounded_input() -> None:
    with pytest.raises(ValidationError):
        ConversationTurnCreate(user_input="")
    with pytest.raises(ValidationError):
        ConversationTurnCreate(user_input="x" * 1_000_001)


def test_turn_retry_requires_a_run_identity_and_revision() -> None:
    command = ConversationTurnRetry(expected_run_id=uuid4(), expected_run_revision=3)

    assert command.expected_run_revision == 3
    with pytest.raises(ValidationError):
        ConversationTurnRetry(expected_run_id=uuid4(), expected_run_revision=0)
