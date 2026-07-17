"""Stable DTOs and protocol shared by every Agent runtime provider."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RuntimeCapability(StrEnum):
    STREAM_EVENTS = "stream_events"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    STATUS = "status"
    TRAJECTORY = "trajectory"
    CHECKPOINT = "checkpoint"


class RuntimeSessionStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUNTIME_STATUSES = {
    RuntimeSessionStatus.COMPLETED,
    RuntimeSessionStatus.FAILED,
    RuntimeSessionStatus.CANCELLED,
}


class RuntimeEventType(StrEnum):
    SESSION_CREATED = "session.created"
    RUN_STARTED = "run.started"
    RUN_PAUSED = "run.paused"
    RUN_RESUMED = "run.resumed"
    STEP_STARTED = "step.started"
    OUTPUT_DELTA = "output.delta"
    STEP_COMPLETED = "step.completed"
    CHECKPOINT_SAVED = "checkpoint.saved"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"


class RuntimeProviderDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=100)
    protocol_version: str = "1.0"
    capabilities: frozenset[RuntimeCapability]


class RuntimeSessionRequest(BaseModel):
    """Complete immutable input needed to (re)construct one provider session."""

    model_config = ConfigDict(frozen=True)

    tenant_id: UUID
    run_id: UUID
    task_id: UUID
    agent_id: UUID
    agent_version_id: UUID
    task_title: str = Field(min_length=1, max_length=300)
    task_input: dict[str, Any] = Field(default_factory=dict)
    acceptance: dict[str, Any] = Field(default_factory=dict)
    role: str = Field(min_length=1, max_length=120)
    mandate: str = Field(min_length=1)
    boundaries: list[str] = Field(default_factory=list)
    long_term_goal: str | None = None
    current_goal: str | None = None
    model_config_data: dict[str, Any] = Field(default_factory=dict)
    run_config: dict[str, Any] = Field(default_factory=dict)
    budgets: dict[str, Any] = Field(default_factory=dict)
    checkpoint: dict[str, Any] | None = None
    event_sequence: int = Field(default=0, ge=0)
    resume_session_id: str | None = Field(default=None, min_length=1, max_length=500)


class RuntimeSessionHandle(BaseModel):
    model_config = ConfigDict(frozen=True)

    external_session_id: str = Field(min_length=1, max_length=500)
    status: RuntimeSessionStatus
    capabilities: frozenset[RuntimeCapability]
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    sequence: int = Field(ge=1)
    type: RuntimeEventType
    message: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RuntimeResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: RuntimeSessionStatus
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    checkpoint: dict[str, Any] | None = None
    external_session_id: str | None = Field(default=None, min_length=1, max_length=500)


class RuntimeTrajectory(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    provider_version: str
    external_session_id: str
    status: RuntimeSessionStatus
    events: list[RuntimeEvent]
    messages: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class AgentRuntimeProvider(Protocol):
    """Async provider boundary; implementations never receive persistence objects."""

    @property
    def descriptor(self) -> RuntimeProviderDescriptor: ...

    async def create_session(self, request: RuntimeSessionRequest) -> RuntimeSessionHandle: ...

    async def run(
        self, external_session_id: str, request: RuntimeSessionRequest
    ) -> RuntimeResult: ...

    async def pause(self, external_session_id: str) -> RuntimeSessionHandle: ...

    async def resume(self, external_session_id: str) -> RuntimeSessionHandle: ...

    async def cancel(self, external_session_id: str) -> RuntimeSessionHandle: ...

    async def get_status(self, external_session_id: str) -> RuntimeSessionHandle: ...

    def stream_events(
        self, external_session_id: str, *, after_sequence: int = 0
    ) -> AsyncIterator[RuntimeEvent]: ...

    async def export_trajectory(self, external_session_id: str) -> RuntimeTrajectory: ...
