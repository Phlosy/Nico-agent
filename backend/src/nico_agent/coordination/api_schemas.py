"""HTTP schemas for generic dynamic coordination primitives."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from nico_agent.coordination.contracts import (
    AgentMessageStatus,
    AgentMessageType,
    AgentMessageVisibility,
    DelegationStatus,
)


class FromAttributesModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DelegationRead(FromAttributesModel):
    id: UUID
    parent_run_id: UUID
    parent_run_step_id: UUID | None
    child_task_id: UUID
    child_run_id: UUID
    target_agent_id: UUID
    target_agent_version_id: UUID
    objective: str
    acceptance: dict[str, Any]
    context_refs: list[str]
    execution_mode: str
    budget_grant: dict[str, Any]
    policy_snapshot: dict[str, Any]
    permission_snapshot: dict[str, Any]
    status: DelegationStatus
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    started_at: datetime | None
    ended_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime


class AgentMessageRead(FromAttributesModel):
    id: UUID
    delegation_id: UUID
    sender_run_id: UUID
    receiver_run_id: UUID
    message_type: AgentMessageType
    content: dict[str, Any]
    refs: list[str]
    visibility: AgentMessageVisibility
    status: AgentMessageStatus
    correlation_id: UUID
    delivered_at: datetime | None
    acknowledged_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RetryRequestCreate(BaseModel):
    reason: str = Field(min_length=1, max_length=8_000)
    idempotency_key: str = Field(min_length=1, max_length=200)
