"""HTTP and application contracts for durable ToolCall decisions."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolApprovalDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    expected_revision: int = Field(ge=1)
    decision: Literal["approve", "reject"]
    allowed_scope: Literal["once", "run"] | None = None
    reason: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def validate_scope(self) -> ToolApprovalDecision:
        if self.decision == "approve" and self.allowed_scope is None:
            raise ValueError("approved tool calls require once or run scope")
        if self.decision == "reject" and self.allowed_scope is not None:
            raise ValueError("rejected tool calls cannot grant a scope")
        return self


class ToolApprovalRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    run_id: UUID
    run_step_id: UUID
    tool_call_id: UUID
    tool_definition_id: UUID
    tool_name: str
    tool_version: str
    risk_level: Literal["medium", "high"]
    status: Literal["requested", "approved", "rejected", "expired", "cancelled"]
    allowed_scope: Literal["none", "once", "run"] | None
    requester: str
    arguments: dict[str, Any]
    arguments_hash: str
    decided_by: str | None
    decision: dict[str, Any] | None
    expires_at: datetime
    decided_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime
