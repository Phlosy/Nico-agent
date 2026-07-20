"""Versioned HTTP schemas for controlled Memory and Skill growth."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from nico_agent.api_schemas import FromAttributesModel
from nico_agent.skills.contracts import SkillVersionDraft

MemoryStatusValue = Literal["candidate", "active", "invalidated", "expired", "deleted"]
MemoryTypeValue = Literal["working", "episodic", "semantic", "procedural"]
ScopeValue = Literal["tenant", "project", "agent"]
SkillStatusValue = Literal[
    "candidate", "testing", "approved", "published", "deprecated", "disabled"
]
SkillVersionStatusValue = Literal["draft", "testing", "published", "rejected"]
Reason = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=10_000),
]
Content = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100_000),
]


class GrowthCandidatesCommand(BaseModel):
    memory_scope: ScopeValue = "project"
    skill_scope: ScopeValue = "project"
    working_ttl_seconds: int = Field(default=86_400, ge=300, le=2_592_000)
    generate_semantic: bool = True
    generate_procedural: bool = True
    generate_skill: bool = True


class MemoryRead(FromAttributesModel):
    id: UUID
    memory_key: UUID
    version: int
    memory_type: MemoryTypeValue
    scope_type: ScopeValue
    project_id: UUID | None
    agent_id: UUID | None
    status: MemoryStatusValue
    content: str
    confidence: float
    content_hash: str
    supersedes_id: UUID | None
    created_by: str
    expires_at: datetime | None
    approved_at: datetime | None
    invalidated_at: datetime | None
    deleted_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime


class GrowthSourceRead(FromAttributesModel):
    id: UUID
    subject_type: Literal["memory", "skill_version"]
    memory_id: UUID | None
    skill_version_id: UUID | None
    run_id: UUID
    run_step_id: UUID
    tool_call_id: UUID | None
    runtime_session_id: UUID | None
    agent_version_id: UUID
    trajectory_hash: str
    generator_name: str
    generator_version: str
    source_hash: str
    created_at: datetime


class EvaluationRead(FromAttributesModel):
    id: UUID
    subject_type: Literal["memory", "skill_version"]
    memory_id: UUID | None
    skill_version_id: UUID | None
    evaluator_name: str
    evaluator_version: str
    content_hash: str
    status: Literal["pending", "completed", "failed"]
    score: float | None
    verdict: Literal["pass", "fail"] | None
    details: dict[str, Any]
    evidence: list[dict[str, Any]]
    error: dict[str, Any] | None
    created_by: str
    started_at: datetime | None
    ended_at: datetime | None
    revision: int
    created_at: datetime


class ApprovalRead(FromAttributesModel):
    id: UUID
    subject_type: Literal["memory", "skill_version"]
    memory_id: UUID | None
    skill_version_id: UUID | None
    action: Literal["publish"]
    content_hash: str
    status: Literal["requested", "approved", "rejected", "cancelled", "expired"]
    requester: str
    reviewer: str | None
    reason: str | None
    expires_at: datetime | None
    decided_at: datetime | None
    revision: int
    created_at: datetime


class MemorySearchCommand(BaseModel):
    query: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000),
    ]
    project_id: UUID | None = None
    agent_id: UUID | None = None
    limit: int = Field(default=10, ge=1, le=100)
    minimum_similarity: float = Field(default=0.0, ge=0.0, le=1.0)


class MemorySearchSource(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_id: UUID
    run_step_id: UUID
    tool_call_id: UUID | None
    trajectory_hash: str
    source_hash: str


class MemorySearchRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    memory_id: UUID
    memory_key: UUID
    version: int
    memory_type: str
    scope_type: str
    content: str
    confidence: float
    similarity: float
    chunk_index: int
    chunk_content: str
    sources: tuple[MemorySearchSource, ...]


class MemoryRevisionCommand(BaseModel):
    content: Content
    reason: Reason
    expected_revision: int = Field(ge=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    expires_at: datetime | None = None


class GrowthPublishCommand(BaseModel):
    expected_revision: int = Field(ge=1)


class GrowthTransitionCommand(BaseModel):
    expected_revision: int = Field(ge=1)
    reason: Reason


class ApprovalRequestCommand(BaseModel):
    expected_revision: int = Field(ge=1)
    expires_in_seconds: int = Field(default=86_400, ge=300, le=2_592_000)


class ApprovalDecisionCommand(BaseModel):
    decision: Literal["approved", "rejected"]
    reason: Reason
    expected_revision: int = Field(ge=1)


class ApprovalCancelCommand(BaseModel):
    reason: Reason
    expected_revision: int = Field(ge=1)


class SkillRead(FromAttributesModel):
    id: UUID
    name: str
    description: str
    scope_type: ScopeValue
    project_id: UUID | None
    agent_id: UUID | None
    status: SkillStatusValue
    current_version_id: UUID | None
    success_stats: dict[str, Any]
    created_by: str
    revision: int
    created_at: datetime
    updated_at: datetime


class SkillVersionEntityRead(FromAttributesModel):
    id: UUID
    skill_id: UUID
    version: int
    status: SkillVersionStatusValue
    conditions: dict[str, Any]
    preconditions: list[dict[str, Any]]
    input_schema: dict[str, Any]
    steps: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    output_schema: dict[str, Any]
    validation: dict[str, Any]
    failure_modes: list[dict[str, Any]]
    content_hash: str
    created_by: str
    evaluated_at: datetime | None
    approved_at: datetime | None
    published_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime


class SkillRevisionCommand(BaseModel):
    draft: SkillVersionDraft
    reason: Reason
    expected_skill_revision: int = Field(ge=1)
    expected_version_revision: int = Field(ge=1)


class SkillPublishCommand(BaseModel):
    expected_skill_revision: int = Field(ge=1)
    expected_version_revision: int = Field(ge=1)


class SkillCanaryCommand(BaseModel):
    skill_version_id: UUID
    scope_type: Literal["project", "agent"]
    scope_id: UUID
    rollout_percentage: int = Field(ge=1, le=99)
    expected_skill_revision: int = Field(ge=1)


class SkillSwitchCommand(BaseModel):
    skill_version_id: UUID
    reason: Reason
    expected_skill_revision: int = Field(ge=1)


class SkillStopCommand(BaseModel):
    reason: Reason
    expected_skill_revision: int = Field(ge=1)


class SkillDeploymentEntityRead(FromAttributesModel):
    id: UUID
    skill_id: UUID
    skill_version_id: UUID
    scope_type: Literal["project", "agent"]
    project_id: UUID | None
    agent_id: UUID | None
    rollout_percentage: int
    status: Literal["active", "retired"]
    created_by: str
    retired_by: str | None
    retired_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime
