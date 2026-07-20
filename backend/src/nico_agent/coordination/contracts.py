"""Stable, persistence-free contracts for dynamic multi-Agent coordination."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DelegationStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


TERMINAL_DELEGATION_STATUSES = {
    DelegationStatus.COMPLETED,
    DelegationStatus.FAILED,
    DelegationStatus.CANCELLED,
    DelegationStatus.REJECTED,
}


class AgentMessageType(StrEnum):
    TASK_ASSIGNMENT = "task_assignment"
    PROGRESS = "progress"
    QUESTION = "question"
    ANSWER = "answer"
    RESULT = "result"
    CRITIQUE = "critique"
    RETRY_REQUEST = "retry_request"
    CANCEL = "cancel"
    SYSTEM_NOTICE = "system_notice"


class AgentMessageStatus(StrEnum):
    QUEUED = "queued"
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"


class AgentMessageVisibility(StrEnum):
    SENDER_RECEIVER = "sender_receiver"
    DELEGATION_TREE = "delegation_tree"
    PARENT_ONLY = "parent_only"


class BudgetGrant(BaseModel):
    """Resources reserved by a parent before a Child Run is created."""

    model_config = ConfigDict(frozen=True)

    token_limit: int = Field(ge=1)
    cost_limit_microunits: int = Field(default=0, ge=0)
    tool_call_limit: int = Field(default=0, ge=0)
    wall_time_seconds: int | None = Field(default=None, ge=1, le=604_800)


class DelegationIntent(BaseModel):
    """Provider-safe request to create one child Task and Run atomically."""

    model_config = ConfigDict(frozen=True)

    target_agent_version_id: UUID
    objective: str = Field(min_length=1, max_length=20_000)
    acceptance: dict[str, Any] = Field(default_factory=dict)
    context_refs: tuple[str, ...] = Field(default=(), max_length=256)
    budget: BudgetGrant
    permission_restrictions: dict[str, Any] = Field(default_factory=dict)
    execution_mode: Literal["serial", "parallel"] = "parallel"
    idempotency_key: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_context_refs(self) -> DelegationIntent:
        if any(not value or len(value) > 2_000 for value in self.context_refs):
            raise ValueError("context_refs must contain non-empty bounded references")
        return self


class RuntimeCoordinationIntent(BaseModel):
    """Narrow command accepted by the runtime coordination handler."""

    model_config = ConfigDict(frozen=True)

    action: Literal["delegate", "send_message", "acknowledge", "retry"]
    delegation: DelegationIntent | None = None
    delegation_id: UUID | None = None
    message_type: AgentMessageType | None = None
    content: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class RuntimeCoordinationOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: str
    status: Literal["accepted", "rejected", "delivered", "acknowledged"]
    delegation_id: UUID | None = None
    child_task_id: UUID | None = None
    child_run_id: UUID | None = None
    message_id: UUID | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class DelegationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    delegation_id: UUID
    child_task_id: UUID
    child_run_id: UUID
    status: DelegationStatus
    idempotent_replay: bool = False
