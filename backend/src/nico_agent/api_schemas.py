"""Versioned HTTP schemas for the Goal C core control plane."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from nico_agent.domain.states import (
    AgentStatus,
    AgentVersionStatus,
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
    model_config_data: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias="model_config",
        serialization_alias="model_config",
    )
    tool_policy: dict[str, Any] = Field(default_factory=dict)
    memory_policy: dict[str, Any] = Field(default_factory=dict)
    skill_policy: dict[str, Any] = Field(default_factory=dict)
    plugin_refs: list[dict[str, Any]] = Field(default_factory=list)
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
    model_config_data: dict[str, Any] = Field(
        validation_alias="model_config_json", serialization_alias="model_config"
    )
    tool_policy: dict[str, Any]
    memory_policy: dict[str, Any]
    skill_policy: dict[str, Any]
    plugin_refs: list[dict[str, Any]]
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
    cost: dict[str, Any]
    result: dict[str, Any] | None
    error: dict[str, Any] | None
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


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
