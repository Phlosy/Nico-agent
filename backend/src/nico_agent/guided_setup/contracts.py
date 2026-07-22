"""Secret-free contracts for the guided setup coordinator."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

SetupAreaKey = Literal["model", "web", "capabilities", "verification"]
SetupAreaState = Literal["ready", "incomplete", "blocked", "failed", "skipped"]
SetupOverallState = Literal["full", "partial", "incomplete"]


class FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SetupAreaRead(FrozenContract):
    key: SetupAreaKey
    state: SetupAreaState
    summary: str = Field(min_length=1, max_length=500)
    next_action: str | None = Field(default=None, max_length=200)
    details: dict[str, object] = Field(default_factory=dict)


class SetupTargetRead(FrozenContract):
    agent_id: UUID
    agent_name: str
    agent_revision: int = Field(ge=1)
    agent_version_id: UUID
    agent_version: int = Field(ge=1)
    model_name: str | None = None


class SetupReadinessRead(FrozenContract):
    schema_version: int = 1
    tenant_revision: int = Field(ge=1)
    overall: SetupOverallState
    areas: tuple[SetupAreaRead, ...]
    target: SetupTargetRead | None = None
    selected_profile: str | None = None
    web_provider: Literal["brave", "searxng"] | None = None
    verified_at: datetime | None = None


class SetupIntentPatch(FrozenContract):
    expected_tenant_revision: int = Field(ge=1)
    web_intent: Literal["enabled", "skipped"] | None = None
    selected_agent_id: UUID | None = None
    selected_profile: str | None = Field(default=None, min_length=1, max_length=80)
    capability_agent_version_id: UUID | None = None


class SetupIntentRead(FrozenContract):
    schema_version: int = 1
    tenant_revision: int = Field(ge=1)
    web_intent: Literal["enabled", "skipped"] | None = None
    selected_agent_id: UUID | None = None
    selected_profile: str | None = None
    capability_agent_version_id: UUID | None = None


class SetupProofCreate(FrozenContract):
    expected_tenant_revision: int = Field(ge=1)
    run_id: UUID


class SetupProofRead(FrozenContract):
    schema_version: int = 1
    state: Literal["succeeded", "failed"]
    tenant_revision: int = Field(ge=1)
    run_id: UUID
    agent_version_id: UUID | None = None
    web_candidate_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    verified_at: datetime | None = None
    failure_class: str | None = None
