"""Public contracts for managed multi-Agent Projects."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from nico_agent.api_schemas import ProjectRead


class FromAttributesModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProjectPreflightRequest(BaseModel):
    lead_agent_id: UUID
    member_agent_ids: list[UUID] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_members(self) -> ProjectPreflightRequest:
        if len(set(self.member_agent_ids)) != len(self.member_agent_ids):
            raise ValueError("member_agent_ids must be unique")
        if self.lead_agent_id in self.member_agent_ids:
            raise ValueError("lead_agent_id must not be repeated in member_agent_ids")
        return self


class ProjectCollaborationCreate(ProjectPreflightRequest):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    goal: str = Field(min_length=1, max_length=20_000)
    acceptance: dict = Field(default_factory=dict)
    supervision_cadence_seconds: int | None = Field(default=3600, ge=300, le=604_800)


class ProjectMemberAdd(BaseModel):
    agent_id: UUID
    expected_project_revision: int = Field(ge=1)


class ProjectMemberStateCommand(BaseModel):
    target: Literal["active", "paused", "removed"]
    expected_project_revision: int = Field(ge=1)
    expected_member_revision: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_removal_reason(self) -> ProjectMemberStateCommand:
        if self.target == "removed" and not (self.reason or "").strip():
            raise ValueError("removed members require a reason")
        return self


class ProjectLeadReplace(BaseModel):
    new_lead_agent_id: UUID
    expected_project_revision: int = Field(ge=1)


class ProjectMemberRead(FromAttributesModel):
    id: UUID
    project_id: UUID
    agent_id: UUID
    role: Literal["lead", "member"]
    status: Literal["active", "paused", "removed"]
    removal_reason: str | None
    removed_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime


class ProjectSessionRead(FromAttributesModel):
    id: UUID
    project_id: UUID
    project_member_id: UUID
    agent_id: UUID
    current_conversation_id: UUID | None
    status: Literal["active", "paused", "archived"]
    revision: int
    created_at: datetime
    updated_at: datetime


class ProjectTimelineEntryRead(BaseModel):
    sequence: int
    occurred_at: datetime
    kind: Literal[
        "artifact",
        "conversation",
        "delegation",
        "plan",
        "run",
        "state",
        "task",
        "tool",
    ]
    event_type: str
    resource_type: str
    resource_id: UUID
    run_id: UUID | None
    actor_id: str
    facts: dict
    links: dict[str, str]


class ProjectTimelinePage(BaseModel):
    entries: list[ProjectTimelineEntryRead]
    next_cursor: int | None
    has_more: bool


class ProjectLeadPreflightRead(BaseModel):
    compatible: bool
    lead_agent_id: UUID
    lead_agent_version_id: UUID | None
    member_agent_version_ids: list[UUID]
    issues: list[str]


class ProjectCollaborationRead(BaseModel):
    project: ProjectRead
    members: list[ProjectMemberRead]
    sessions: list[ProjectSessionRead]
    lead_agent_id: UUID


class ProjectMemberMutationRead(BaseModel):
    project: ProjectRead
    member: ProjectMemberRead
    session: ProjectSessionRead


class ProjectLeadReplaceRead(ProjectCollaborationRead):
    pass
