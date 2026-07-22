"""Secret-free contracts for Agent capability review and publication."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

CapabilityProfileKey = Literal["minimal", "web_research", "developer", "custom"]
CapabilityRisk = Literal["low", "medium", "high"]


class FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CapabilityProfileRead(FrozenContract):
    key: CapabilityProfileKey
    label: str
    description: str
    recommended: bool = False


class ToolCapabilityRead(FrozenContract):
    reference: str
    name: str
    version: str
    description: str
    permission: str
    risk: CapabilityRisk
    usable: bool
    reason_code: str | None = None
    reason: str | None = None


class SkillCapabilityRead(FrozenContract):
    skill_id: UUID
    skill_version_id: UUID
    name: str
    version: int
    description: str
    scope: Literal["tenant", "project", "agent"]
    trust: Literal["published", "unavailable"]
    usable: bool
    reason_code: str | None = None
    reason: str | None = None


class CapabilityCatalogRead(FrozenContract):
    schema_version: int = 1
    tenant_revision: int = Field(ge=1)
    agent_id: UUID | None = None
    agent_revision: int | None = None
    web_ready: bool
    web_candidate_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    profiles: tuple[CapabilityProfileRead, ...]
    tools: tuple[ToolCapabilityRead, ...]
    skills: tuple[SkillCapabilityRead, ...]


class CapabilityTarget(FrozenContract):
    agent_id: UUID | None = None
    expected_agent_revision: int | None = Field(default=None, ge=1)
    starter_agent_name: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]{1,118}[a-z0-9]$",
        max_length=120,
    )
    starter_agent_display_name: str | None = Field(default=None, min_length=1, max_length=200)
    source_agent_version_id: UUID | None = None

    @model_validator(mode="after")
    def validate_target(self) -> CapabilityTarget:
        existing = self.agent_id is not None
        starter = self.starter_agent_name is not None
        if existing == starter:
            raise ValueError("select exactly one existing or Starter Agent")
        if existing:
            if self.expected_agent_revision is None:
                raise ValueError("existing Agent publication requires its expected revision")
            if (
                self.starter_agent_display_name is not None
                or self.source_agent_version_id is not None
            ):
                raise ValueError("existing Agent publication cannot include Starter fields")
        else:
            if self.expected_agent_revision is not None:
                raise ValueError("Starter Agent publication cannot include an Agent revision")
            if self.starter_agent_display_name is None or self.source_agent_version_id is None:
                raise ValueError(
                    "Starter Agent publication requires display name and source version"
                )
        return self


class CapabilitySelection(FrozenContract):
    profile: CapabilityProfileKey
    tool_refs: tuple[str, ...] = Field(default=(), max_length=200)
    skill_version_ids: tuple[UUID, ...] = Field(default=(), max_length=200)

    @model_validator(mode="after")
    def validate_selection(self) -> CapabilitySelection:
        if self.profile != "custom" and (self.tool_refs or self.skill_version_ids):
            raise ValueError("maintained profiles do not accept custom capability IDs")
        if len(set(self.tool_refs)) != len(self.tool_refs):
            raise ValueError("Tool selections must be unique")
        if len(set(self.skill_version_ids)) != len(self.skill_version_ids):
            raise ValueError("Skill selections must be unique")
        return self


class CapabilityPreviewCreate(FrozenContract):
    expected_tenant_revision: int = Field(ge=1)
    target: CapabilityTarget
    selection: CapabilitySelection


class CapabilityActivationCreate(CapabilityPreviewCreate):
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_risks: tuple[Literal["medium", "high"], ...] = ()


class CapabilityDiffRead(FrozenContract):
    tools_added: tuple[str, ...] = ()
    tools_removed: tuple[str, ...] = ()
    skills_added: tuple[UUID, ...] = ()
    skills_removed: tuple[UUID, ...] = ()
    model_route_changed: bool = False


class CapabilityPreviewRead(FrozenContract):
    schema_version: int = 1
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: datetime
    target_agent_id: UUID
    target_agent_name: str
    proposed_agent_version_id: UUID
    proposed_agent_version: int = Field(ge=1)
    profile: CapabilityProfileKey
    tool_refs: tuple[str, ...]
    skill_version_ids: tuple[UUID, ...]
    risks: tuple[Literal["medium", "high"], ...]
    diff: CapabilityDiffRead
    projection: dict[str, object]


class CapabilityActivationRead(FrozenContract):
    schema_version: int = 1
    tenant_revision: int = Field(ge=1)
    agent_id: UUID
    agent_name: str
    agent_revision: int = Field(ge=1)
    agent_version_id: UUID
    agent_version: int = Field(ge=1)
    profile: CapabilityProfileKey
    tool_refs: tuple[str, ...]
    skill_version_ids: tuple[UUID, ...]
    published_at: datetime
