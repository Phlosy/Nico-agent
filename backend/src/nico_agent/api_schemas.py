"""Versioned HTTP schemas for the core control plane."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from nico_agent.domain.states import (
    AgentStatus,
    AgentVersionStatus,
    ConversationApprovalMode,
    ProjectStatus,
    RunStatus,
    RunStepStatus,
    TaskStatus,
)


class FromAttributesModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,98}[a-z0-9]$", max_length=100)
    settings: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)


class TenantRead(FromAttributesModel):
    id: UUID
    name: str
    slug: str
    status: str
    settings: dict[str, Any]
    limits: dict[str, Any]
    revision: int
    created_at: datetime


class TenantSettingsPatch(BaseModel):
    expected_revision: int = Field(ge=1)
    settings: dict[str, Any]


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectPatch(BaseModel):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    metadata: dict[str, Any] | None = None


class ProjectRead(FromAttributesModel):
    id: UUID
    name: str
    kind: Literal["shared", "personal"]
    owner_actor_id: str | None
    supervision_cadence_seconds: int | None
    next_supervision_at: datetime | None
    description: str | None
    metadata: dict[str, Any] = Field(validation_alias="metadata_json")
    status: ProjectStatus
    revision: int
    created_at: datetime
    updated_at: datetime


class AgentCreate(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,118}[a-z0-9]$", max_length=120)
    display_name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class AgentPatch(BaseModel):
    expected_revision: int = Field(ge=1)
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    default_approval_mode: ConversationApprovalMode | None = None
    show_response_metrics: bool | None = None


class AgentClone(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,118}[a-z0-9]$", max_length=120)
    display_name: str = Field(min_length=1, max_length=200)


class RevisionCommand(BaseModel):
    expected_revision: int = Field(ge=1)


class AgentRollback(RevisionCommand):
    version_id: UUID


class AgentRead(FromAttributesModel):
    id: UUID
    name: str
    display_name: str
    description: str | None
    status: AgentStatus
    default_approval_mode: ConversationApprovalMode
    show_response_metrics: bool
    current_version_id: UUID | None
    revision: int
    created_at: datetime
    updated_at: datetime


class AgentVersionCreate(BaseModel):
    role: str = Field(min_length=1, max_length=120)
    mandate: str = Field(min_length=1)
    boundaries: list[str] = Field(default_factory=list)
    long_term_goal: str | None = None
    current_goal: str | None = None
    # ``None`` distinguishes an omitted field from an explicit provider.  The
    # control plane resolves omitted values to either the legacy run_config
    # selection or the native default before persisting the immutable version.
    runtime_provider: str | None = Field(default=None, min_length=1, max_length=100)
    execution_mode: Literal["direct", "react", "plan_and_execute"] = "direct"
    model_endpoint_id: UUID | None = None
    model_name: str | None = Field(default=None, min_length=1, max_length=200)
    model_config_data: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias="model_config",
        serialization_alias="model_config",
    )
    tool_policy: dict[str, Any] = Field(default_factory=dict)
    memory_policy: dict[str, Any] = Field(default_factory=dict)
    skill_policy: dict[str, Any] = Field(default_factory=dict)
    plugin_refs: list[dict[str, Any]] = Field(default_factory=list)
    coordination_policy: dict[str, Any] = Field(default_factory=dict)
    budgets: dict[str, Any] = Field(default_factory=dict)
    run_config: dict[str, Any] = Field(default_factory=dict)


class AgentVersionRead(FromAttributesModel):
    id: UUID
    agent_id: UUID
    version: int
    status: AgentVersionStatus
    role: str
    mandate: str
    boundaries: list[str]
    long_term_goal: str | None
    current_goal: str | None
    runtime_provider: str | None
    execution_mode: str | None
    model_endpoint_id: UUID | None
    model_name: str | None
    model_config_data: dict[str, Any] = Field(
        validation_alias="model_config_json", serialization_alias="model_config"
    )
    tool_policy: dict[str, Any]
    memory_policy: dict[str, Any]
    skill_policy: dict[str, Any]
    plugin_refs: list[dict[str, Any]]
    coordination_policy: dict[str, Any]
    budgets: dict[str, Any]
    run_config: dict[str, Any]
    content_hash: str
    created_at: datetime


class TaskCreate(BaseModel):
    project_id: UUID
    title: str = Field(min_length=1, max_length=300)
    input: dict[str, Any] = Field(default_factory=dict)
    acceptance: dict[str, Any] = Field(default_factory=dict)
    assignee_agent_id: UUID | None = None
    parent_task_id: UUID | None = None
    priority: int = Field(default=0, ge=-100, le=100)


class TaskTransition(BaseModel):
    target: TaskStatus
    expected_revision: int = Field(ge=1)
    assignee_agent_id: UUID | None = None


class TaskRead(FromAttributesModel):
    id: UUID
    project_id: UUID
    title: str
    input: dict[str, Any]
    acceptance: dict[str, Any]
    assignee_agent_id: UUID | None
    parent_task_id: UUID | None
    project_session_id: UUID | None
    status: TaskStatus
    priority: int
    revision: int
    created_at: datetime
    updated_at: datetime


class RunCreate(BaseModel):
    max_steps: int = Field(default=64, ge=1, le=10_000)
    token_budget: int | None = Field(default=None, ge=1)
    timeout_seconds: int | None = Field(default=None, ge=1, le=604_800)
    budgets: dict[str, Any] = Field(default_factory=dict)


class RunTransition(BaseModel):
    target: RunStatus
    expected_revision: int = Field(ge=1)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class RunRead(FromAttributesModel):
    id: UUID
    task_id: UUID
    agent_id: UUID
    agent_version_id: UUID
    retry_of_run_id: UUID | None
    attempt: int
    status: RunStatus
    max_steps: int
    token_budget: int | None
    timeout_seconds: int | None
    budgets: dict[str, Any]
    checkpoint: dict[str, Any] | None
    checkpoint_schema_version: int
    checkpoint_revision: int
    checkpoint_hash: str | None
    cost: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    started_at: datetime | None
    ended_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime


class RuntimeSessionRead(FromAttributesModel):
    id: UUID
    run_id: UUID
    provider_name: str
    provider_version: str
    protocol_version: str
    provider_resolution_source: str | None
    legacy_resolver_used: bool | None
    provider_compatibility: dict[str, Any]
    external_session_id: str | None
    status: str
    capabilities: list[str]
    provider_state: dict[str, Any]
    execution_mode: str
    loop_state: str
    execution_manifest: dict[str, Any]
    model_endpoint_snapshot: dict[str, Any] | None
    coordination_policy_snapshot: dict[str, Any]
    knowledge_policy_snapshot: dict[str, Any]
    current_context_snapshot_id: UUID | None
    last_model_call_id: UUID | None
    checkpoint: dict[str, Any] | None
    checkpoint_schema_version: int
    checkpoint_revision: int
    checkpoint_hash: str | None
    usage: dict[str, Any]
    last_event_sequence: int
    started_at: datetime | None
    ended_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime


class RunStepCreate(BaseModel):
    sequence: int = Field(ge=1)
    kind: str = Field(min_length=1, max_length=100)
    input: dict[str, Any] = Field(default_factory=dict)


class RunStepTransition(BaseModel):
    target: RunStepStatus
    expected_revision: int = Field(ge=1)
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class RunStepRead(FromAttributesModel):
    id: UUID
    run_id: UUID
    sequence: int
    step_key: str | None
    step_type: str | None
    iteration: int | None
    parent_step_id: UUID | None
    context_snapshot_id: UUID | None
    model_call_id: UUID | None
    kind: str
    status: RunStepStatus
    input: dict[str, Any]
    output: dict[str, Any] | None
    error: dict[str, Any] | None
    started_at: datetime | None
    ended_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime


class EventRead(FromAttributesModel):
    id: UUID
    sequence: int
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    run_id: UUID | None
    actor_id: str
    payload: dict[str, Any]
    correlation_id: UUID
    causation_id: UUID | None
    created_at: datetime


class AuditRead(FromAttributesModel):
    id: UUID
    sequence: int
    action: str
    resource_type: str
    resource_id: UUID
    actor_id: str
    details: dict[str, Any]
    correlation_id: UUID
    created_at: datetime


class ToolDefinitionRead(FromAttributesModel):
    id: UUID
    name: str
    version: str
    status: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    permission: str
    timeout_seconds: int
    retry_policy: dict[str, Any]
    isolation_policy: dict[str, Any]
    risk: str
    max_output_bytes: int
    content_hash: str
    revision: int
    created_at: datetime


class ToolCallRead(FromAttributesModel):
    id: UUID
    run_id: UUID
    run_step_id: UUID
    tool_definition_id: UUID
    tool_name: str
    tool_version: str
    idempotency_key: str
    caller: str
    arguments: dict[str, Any]
    status: str
    attempts: list[dict[str, Any]]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    usage: dict[str, Any]
    started_at: datetime | None
    ended_at: datetime | None
    revision: int
    created_at: datetime


class PlanRead(FromAttributesModel):
    id: UUID
    run_id: UUID
    runtime_session_id: UUID
    revision: int
    status: str
    reason: str
    objective: str
    supersedes_plan_id: UUID | None
    created_by_model_call_id: UUID
    content_hash: str
    created_at: datetime
    updated_at: datetime


class PlanStepRead(FromAttributesModel):
    id: UUID
    run_id: UUID
    plan_id: UUID
    step_key: str
    position: int
    title: str
    instruction: str
    acceptance: dict[str, Any]
    dependencies: list[str]
    status: str
    attempt: int
    run_step_id: UUID | None
    output: dict[str, Any] | None
    output_hash: str | None
    evidence_refs: list[str]
    error: dict[str, Any] | None
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RuntimeEvaluationRead(FromAttributesModel):
    id: UUID
    run_id: UUID
    plan_id: UUID | None
    plan_step_id: UUID | None
    model_call_id: UUID | None
    sequence: int
    evaluation_type: str
    method: str
    status: str
    verdict: str
    input_hash: str
    output_hash: str | None
    evidence_refs: list[str]
    result: dict[str, Any]
    error: dict[str, Any] | None
    created_at: datetime


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
