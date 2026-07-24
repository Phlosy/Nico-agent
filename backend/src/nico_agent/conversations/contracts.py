"""Public request and response contracts for durable conversations."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nico_agent.domain.context import (
    ConversationContextMessage as ConversationContextMessage,
)
from nico_agent.domain.states import (
    ConversationApprovalMode,
    ConversationQueuePauseReason,
    ConversationQueueState,
    ConversationStatus,
    ConversationTurnStatus,
    RunStatus,
)

DEFAULT_CONVERSATION_BUDGETS = {
    "max_iterations": 12,
    "max_tool_calls": 8,
}


class FromAttributesModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ConversationCreate(BaseModel):
    mode: Literal["personal", "project"] = "project"
    project_id: UUID | None = None
    agent_id: UUID
    agent_version_id: UUID | None = None
    title: str = Field(default="New conversation", min_length=1, max_length=300)
    idempotency_key: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_mode_target(self) -> ConversationCreate:
        if self.mode == "project" and self.project_id is None:
            raise ValueError("project mode requires project_id")
        if self.mode == "personal" and self.project_id is not None:
            raise ValueError("personal mode does not accept project_id")
        return self


class ConversationPatch(BaseModel):
    expected_revision: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    status: ConversationStatus | None = None
    approval_mode: ConversationApprovalMode | None = None

    @model_validator(mode="after")
    def require_change(self) -> ConversationPatch:
        if self.title is None and self.status is None and self.approval_mode is None:
            raise ValueError("at least one conversation field must be changed")
        return self


class ConversationRead(FromAttributesModel):
    id: UUID
    mode: Literal["personal", "project"]
    project_id: UUID
    agent_id: UUID
    agent_version_id: UUID
    title: str
    status: ConversationStatus
    summary: str | None
    summary_through_sequence: int
    summary_input_hash: str | None
    summary_model_call_id: UUID | None
    last_turn_id: UUID | None
    approval_mode: ConversationApprovalMode
    queue_state: ConversationQueueState
    queue_pause_reason: ConversationQueuePauseReason | None
    queue_pause_turn_id: UUID | None
    queue_paused_at: datetime | None
    created_by: str
    revision: int
    created_at: datetime
    updated_at: datetime


class ConversationTurnCreate(BaseModel):
    user_input: str = Field(min_length=1, max_length=1_000_000)
    idempotency_key: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=200)
    max_steps: int = Field(default=64, ge=1, le=10_000)
    token_budget: int | None = Field(default=None, ge=1)
    timeout_seconds: int | None = Field(default=None, ge=1, le=604_800)
    budgets: dict = Field(default_factory=lambda: dict(DEFAULT_CONVERSATION_BUDGETS))


class ConversationCompact(BaseModel):
    idempotency_key: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=200)
    token_budget: int | None = Field(default=None, ge=1)
    timeout_seconds: int | None = Field(default=None, ge=1, le=604_800)


class ConversationCompactAccepted(BaseModel):
    conversation_id: UUID
    task_id: UUID
    run_id: UUID
    through_sequence: int
    input_hash: str
    status: RunStatus
    replayed: bool = False


class ConversationAttachmentRead(FromAttributesModel):
    id: UUID
    conversation_id: UUID
    name: str
    content_type: str
    sha256: str
    size_bytes: int
    status: str
    uploaded_by: str
    expires_at: datetime
    consumed_by_turn_id: UUID | None
    artifact_id: UUID | None
    idempotency_key: str
    revision: int
    created_at: datetime
    updated_at: datetime


class ConversationTurnRead(FromAttributesModel):
    id: UUID
    conversation_id: UUID
    sequence: int
    user_input: str
    task_id: UUID
    run_id: UUID
    status: ConversationTurnStatus
    run_status: RunStatus
    run_revision: int
    assistant_output: dict | None
    artifact_refs: list[dict]
    usage: dict
    error: dict | None
    revision: int
    created_at: datetime
    updated_at: datetime


class ConversationTurnRetry(BaseModel):
    expected_run_id: UUID
    expected_run_revision: int = Field(ge=1)
    max_steps: int | None = Field(default=None, ge=1, le=10_000)
    token_budget: int | None = Field(default=None, ge=1)
    timeout_seconds: int | None = Field(default=None, ge=1, le=604_800)
    budgets: dict | None = None


class ConversationQueueResume(BaseModel):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(default_factory=lambda: str(uuid4()), min_length=1, max_length=200)


class ConversationQueueRead(BaseModel):
    conversation_id: UUID
    revision: int
    state: ConversationQueueState
    pause_reason: ConversationQueuePauseReason | None
    pause_turn_id: UUID | None
    paused_at: datetime | None
    head_turn: ConversationTurnRead | None
    active_turn: ConversationTurnRead | None
    pause_turn: ConversationTurnRead | None
    queued_turns: list[ConversationTurnRead]
    queued_count: int = Field(ge=0)
    capacity: int = Field(default=20, ge=1)


class ConversationTurnAccepted(ConversationTurnRead):
    """202 response proving Turn, Task, and Run were committed together."""
