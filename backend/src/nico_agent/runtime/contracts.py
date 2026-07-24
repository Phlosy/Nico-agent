"""Stable DTOs and protocol shared by every Agent runtime provider."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from nico_agent.artifacts.contracts import RuntimeArtifactIntent, RuntimeArtifactOutcome
from nico_agent.coordination.contracts import (
    RuntimeCoordinationIntent,
    RuntimeCoordinationOutcome,
)
from nico_agent.domain.context import ConversationContextMessage
from nico_agent.runtime.actions import (
    AgentAction as AgentAction,
)
from nico_agent.runtime.actions import (
    AgentActionBatch as AgentActionBatch,
)
from nico_agent.runtime.actions import (
    AgentActionKind as AgentActionKind,
)
from nico_agent.runtime.actions import (
    AskUserAction as AskUserAction,
)
from nico_agent.runtime.actions import (
    FinalAction as FinalAction,
)
from nico_agent.runtime.actions import (
    ToolCallAction as ToolCallAction,
)
from nico_agent.user_inputs.contracts import RuntimeUserInputIntent, RuntimeUserInputRequest


class RuntimeCapability(StrEnum):
    STREAM_EVENTS = "stream_events"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    STATUS = "status"
    TRAJECTORY = "trajectory"
    CHECKPOINT = "checkpoint"
    PLATFORM_TOOLS = "platform_tools"
    PLANNING = "planning"
    REFLECTION = "reflection"
    COORDINATION = "coordination"
    ARTIFACTS = "artifacts"
    INTERVENTIONS = "interventions"
    USER_INPUT = "user_input"


class RuntimeSessionStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    SUSPENDED = "suspended"
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
    TOOL_CALL_STARTED = "tool.call.started"
    TOOL_CALL_COMPLETED = "tool.call.completed"
    CHECKPOINT_SAVED = "checkpoint.saved"
    CONTEXT_SNAPSHOT_CREATED = "context.snapshot.created"
    MODEL_CALL_STARTED = "model.call.started"
    MODEL_OUTPUT_DELTA = "model.output.delta"
    MODEL_CALL_COMPLETED = "model.call.completed"
    MODEL_CALL_FAILED = "model.call.failed"
    ACTION_BATCH_CREATED = "action.batch.created"
    PLAN_CREATED = "plan.created"
    PLAN_STATUS_CHANGED = "plan.status.changed"
    PLAN_STEP_STARTED = "plan.step.started"
    PLAN_STEP_COMPLETED = "plan.step.completed"
    PLAN_STEP_FAILED = "plan.step.failed"
    REFLECTION_COMPLETED = "reflection.completed"
    EVALUATION_COMPLETED = "evaluation.completed"
    DELEGATION_STARTED = "delegation.started"
    DELEGATION_ACCEPTED = "delegation.accepted"
    DELEGATION_RESULT_RECEIVED = "delegation.result_received"
    ARTIFACT_STORED = "artifact.stored"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"


class RuntimeExecutionMode(StrEnum):
    DIRECT = "direct"
    REACT = "react"
    PLAN_AND_EXECUTE = "plan_and_execute"


class RuntimeDisposition(StrEnum):
    TERMINAL = "terminal"
    SUSPENDED = "suspended"


class RuntimeLoopState(StrEnum):
    """Detailed execution state projected beneath the public Run lifecycle."""

    INITIALIZING = "initializing"
    PLANNING = "planning"
    REASONING = "reasoning"
    EXECUTING = "executing"
    SEARCHING = "searching"
    FETCHING = "fetching"
    WAITING_FOR_TOOL = "waiting_for_tool"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_USER_INPUT = "waiting_for_user_input"
    OBSERVING = "observing"
    DELEGATING = "delegating"
    WAITING_FOR_SUBAGENT = "waiting_for_subagent"
    REFLECTING = "reflecting"
    FINALIZING = "finalizing"
    CANCELLING = "cancelling"
    PAUSED = "paused"
    BUDGET_EXHAUSTED = "budget_exhausted"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ContextSeed(BaseModel):
    """Immutable, already-authorized inputs used to rebuild model context."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = Field(default=1, ge=1)
    platform_instructions: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    trusted_context: tuple[dict[str, Any], ...] = ()
    untrusted_context: tuple[dict[str, Any], ...] = ()
    conversation_messages: tuple[ConversationContextMessage, ...] = ()
    memory_refs: tuple[dict[str, Any], ...] = ()
    skill_refs: tuple[dict[str, Any], ...] = ()
    effect_metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeProviderDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=100)
    protocol_version: str = "1.0"
    implementation: Literal["native", "adapter", "test"] = "adapter"
    capabilities: frozenset[RuntimeCapability]
    compatibility: dict[str, Any] = Field(default_factory=dict)


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
    execution_mode: RuntimeExecutionMode = RuntimeExecutionMode.DIRECT
    execution_manifest: dict[str, Any] = Field(default_factory=dict)
    context_seed: ContextSeed | None = None
    model_endpoint_snapshot: dict[str, Any] | None = None
    coordination_policy_snapshot: dict[str, Any] = Field(default_factory=dict)
    recovery_state: dict[str, Any] = Field(default_factory=dict)
    checkpoint: dict[str, Any] | None = None
    event_sequence: int = Field(default=0, ge=0)
    resume_session_id: str | None = Field(default=None, min_length=1, max_length=500)
    tool_session: RuntimeToolSession | None = None


class RuntimeToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    version: str
    description: str
    input_schema: dict[str, Any]

    @property
    def reference(self) -> str:
        return f"{self.name}@{self.version}"


class RuntimeToolIntent(BaseModel):
    model_config = ConfigDict(frozen=True)

    call_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=50)
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=200)
    checkpoint: dict[str, Any] | None = None


class RuntimeToolOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    call_id: str
    tool_call_id: str | None = None
    run_step_id: str | None = None
    status: Literal["succeeded", "failed", "timed_out", "cancelled"]
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    cached: bool = False


class RuntimeToolSession(BaseModel):
    model_config = ConfigDict(frozen=True)

    socket_path: str = Field(min_length=1, max_length=2000)
    token: str = Field(min_length=32, max_length=500, repr=False)
    server_command: tuple[str, ...] = Field(min_length=1)
    server_name: str = Field(default="nico", pattern=r"^[a-z][a-z0-9_-]{0,62}$")


class RuntimeActionOutcome(BaseModel):
    """Bounded authoritative result used to advance one persisted Action cursor."""

    model_config = ConfigDict(frozen=True)

    status: Literal["succeeded", "failed", "blocked", "unknown"]
    outcome_ref: str | None = Field(default=None, min_length=1, max_length=500)
    observation_ref: str | None = Field(default=None, min_length=1, max_length=500)
    value: dict[str, Any] = Field(default_factory=dict)


class RuntimeActionState(BaseModel):
    """Persisted dispatch state plus a recovered domain projection when available."""

    model_config = ConfigDict(frozen=True)

    ordinal: int = Field(ge=0)
    action_id: str = Field(min_length=64, max_length=64)
    status: Literal["pending", "dispatched", "succeeded", "failed", "blocked"]
    outcome_ref: str | None = Field(default=None, max_length=500)
    observation_ref: str | None = Field(default=None, max_length=500)
    outcome: RuntimeActionOutcome | None = None


class RuntimeActionBatchState(BaseModel):
    """Committed batch state returned by the runtime persistence authority."""

    model_config = ConfigDict(frozen=True)

    batch_key: str = Field(min_length=64, max_length=64)
    action_count: int = Field(ge=1, le=32)
    dispatch_cursor: int = Field(ge=0)
    status: Literal["pending", "dispatching", "completed", "failed"]
    actions: tuple[RuntimeActionState, ...] = Field(min_length=1, max_length=32)


@runtime_checkable
class RuntimeActionHandler(Protocol):
    async def wait_for_batch(self, batch_key: str) -> RuntimeActionBatchState: ...

    async def begin_action(
        self,
        batch_key: str,
        *,
        ordinal: int,
        action_id: str,
    ) -> RuntimeActionBatchState: ...

    async def complete_action(
        self,
        batch_key: str,
        *,
        ordinal: int,
        action_id: str,
        outcome: RuntimeActionOutcome,
    ) -> RuntimeActionBatchState: ...

    async def release_action(
        self,
        batch_key: str,
        *,
        ordinal: int,
        action_id: str,
    ) -> RuntimeActionBatchState: ...


@runtime_checkable
class RuntimeUserInputHandler(Protocol):
    async def request_user_input(
        self,
        intent: RuntimeUserInputIntent,
    ) -> RuntimeUserInputRequest: ...


@runtime_checkable
class RuntimeToolHandler(Protocol):
    async def list_tools(self) -> tuple[RuntimeToolSpec, ...]: ...

    async def execute_tool(self, intent: RuntimeToolIntent) -> RuntimeToolOutcome: ...


@runtime_checkable
class RuntimeCoordinationHandler(Protocol):
    async def coordinate(self, intent: RuntimeCoordinationIntent) -> RuntimeCoordinationOutcome: ...


@runtime_checkable
class RuntimeArtifactHandler(Protocol):
    async def store_artifact(self, intent: RuntimeArtifactIntent) -> RuntimeArtifactOutcome: ...


class RuntimeIntervention(BaseModel):
    model_config = ConfigDict(frozen=True)

    intervention_id: UUID
    content: str = Field(min_length=1, max_length=16_000)
    content_hash: str = Field(min_length=64, max_length=64)
    boundary_key: str = Field(min_length=1, max_length=200)


@runtime_checkable
class RuntimeInterventionHandler(Protocol):
    async def freeze(self, boundary_key: str) -> tuple[RuntimeIntervention, ...]: ...


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    """Narrow capabilities exposed to a provider; never contains persistence sessions."""

    tool_handler: RuntimeToolHandler | None = None
    coordination_handler: RuntimeCoordinationHandler | None = None
    artifact_handler: RuntimeArtifactHandler | None = None
    intervention_handler: RuntimeInterventionHandler | None = None
    action_handler: RuntimeActionHandler | None = None
    user_input_handler: RuntimeUserInputHandler | None = None


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


class RuntimeOutcome(BaseModel):
    """Protocol v2 result, including durable suspension without terminating the Run."""

    model_config = ConfigDict(frozen=True)

    disposition: RuntimeDisposition
    status: RuntimeSessionStatus
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    checkpoint: dict[str, Any] | None = None
    wake_condition: dict[str, Any] | None = None
    external_session_id: str | None = Field(default=None, min_length=1, max_length=500)

    @classmethod
    def terminal(
        cls,
        *,
        status: RuntimeSessionStatus,
        output: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
        checkpoint: dict[str, Any] | None = None,
        external_session_id: str | None = None,
    ) -> RuntimeOutcome:
        if status is RuntimeSessionStatus.SUSPENDED:
            raise ValueError("terminal outcome cannot use suspended status")
        return cls(
            disposition=RuntimeDisposition.TERMINAL,
            status=status,
            output=output,
            error=error,
            usage=usage or {},
            checkpoint=checkpoint,
            external_session_id=external_session_id,
        )

    @classmethod
    def suspended(
        cls,
        *,
        checkpoint: dict[str, Any],
        wake_condition: dict[str, Any],
        usage: dict[str, Any] | None = None,
        external_session_id: str | None = None,
    ) -> RuntimeOutcome:
        return cls(
            disposition=RuntimeDisposition.SUSPENDED,
            status=RuntimeSessionStatus.SUSPENDED,
            usage=usage or {},
            checkpoint=checkpoint,
            wake_condition=wake_condition,
            external_session_id=external_session_id,
        )

    def to_result(self) -> RuntimeResult:
        if self.disposition is not RuntimeDisposition.TERMINAL:
            raise ValueError("only a terminal runtime outcome can become a RuntimeResult")
        return RuntimeResult(
            status=self.status,
            output=self.output,
            error=self.error,
            usage=self.usage,
            checkpoint=self.checkpoint,
            external_session_id=self.external_session_id,
        )


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
        self,
        external_session_id: str,
        request: RuntimeSessionRequest,
        tool_handler: RuntimeToolHandler | None = None,
    ) -> RuntimeResult: ...

    async def pause(self, external_session_id: str) -> RuntimeSessionHandle: ...

    async def resume(self, external_session_id: str) -> RuntimeSessionHandle: ...

    async def cancel(self, external_session_id: str) -> RuntimeSessionHandle: ...

    async def get_status(self, external_session_id: str) -> RuntimeSessionHandle: ...

    def stream_events(
        self, external_session_id: str, *, after_sequence: int = 0
    ) -> AsyncIterator[RuntimeEvent]: ...

    async def export_trajectory(self, external_session_id: str) -> RuntimeTrajectory: ...


@runtime_checkable
class AgentRuntimeProviderV2(Protocol):
    """Protocol v2 provider; v1 providers are supported by ``execute_provider``."""

    @property
    def descriptor(self) -> RuntimeProviderDescriptor: ...

    async def create_session(self, request: RuntimeSessionRequest) -> RuntimeSessionHandle: ...

    async def execute(
        self,
        external_session_id: str,
        request: RuntimeSessionRequest,
        services: RuntimeServices,
    ) -> RuntimeOutcome: ...

    async def pause(self, external_session_id: str) -> RuntimeSessionHandle: ...

    async def resume(self, external_session_id: str) -> RuntimeSessionHandle: ...

    async def cancel(self, external_session_id: str) -> RuntimeSessionHandle: ...

    async def get_status(self, external_session_id: str) -> RuntimeSessionHandle: ...

    def stream_events(
        self, external_session_id: str, *, after_sequence: int = 0
    ) -> AsyncIterator[RuntimeEvent]: ...

    async def export_trajectory(self, external_session_id: str) -> RuntimeTrajectory: ...


async def execute_provider(
    provider: AgentRuntimeProvider | AgentRuntimeProviderV2,
    external_session_id: str,
    request: RuntimeSessionRequest,
    services: RuntimeServices,
) -> RuntimeOutcome:
    """Execute v2 natively or adapt one v1 terminal result without changing its behavior."""

    execute = getattr(provider, "execute", None)
    if execute is not None:
        return await execute(external_session_id, request, services)
    result = await provider.run(external_session_id, request, services.tool_handler)  # type: ignore[union-attr]
    return RuntimeOutcome.terminal(
        status=result.status,
        output=result.output,
        error=result.error,
        usage=result.usage,
        checkpoint=result.checkpoint,
        external_session_id=result.external_session_id,
    )
