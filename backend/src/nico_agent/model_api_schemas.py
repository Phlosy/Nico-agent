"""HTTP schemas for model endpoints and native runtime facts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class FromAttributesModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ModelEndpointCreate(BaseModel):
    stable_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,118}[a-z0-9]$", max_length=120)
    display_name: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=2000)
    credential_ref: str = Field(pattern=r"^env:NICO_MODEL_SECRET_[A-Z0-9_]{1,100}$", max_length=300)
    allowed_models: list[str] = Field(default_factory=list, max_length=1000)
    capabilities: dict[str, Any] = Field(default_factory=lambda: {"streaming": True, "tools": True})
    rate_limit: dict[str, Any] = Field(default_factory=dict)
    tls_policy: dict[str, Any] = Field(default_factory=dict)


class ModelEndpointPatch(BaseModel):
    enabled: bool | None = None
    status: Literal["active", "disabled"] | None = None
    credential_ref: str | None = Field(
        default=None,
        pattern=r"^env:NICO_MODEL_SECRET_[A-Z0-9_]{1,100}$",
        max_length=300,
    )
    rate_limit: dict[str, Any] | None = None


class ModelEndpointRead(FromAttributesModel):
    id: UUID
    stable_key: str
    revision: int
    display_name: str
    protocol: str
    base_url: str
    credential_ref: str
    provider_key: str
    catalog_revision: str | None
    provider_options: dict[str, Any]
    verified_probe_id: UUID | None
    verified_at: datetime | None
    status: str
    allowed_models: list[str]
    capabilities: dict[str, Any]
    rate_limit: dict[str, Any]
    tls_policy: dict[str, Any]
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ModelCallRead(FromAttributesModel):
    id: UUID
    run_id: UUID
    context_snapshot_id: UUID
    model_endpoint_id: UUID | None
    replay_of_model_call_id: UUID | None
    call_key: str
    provider: str
    model: str
    status: str
    request_redacted: dict[str, Any]
    response_redacted: dict[str, Any] | None
    request_hash: str
    response_hash: str | None
    provider_request_id: str | None
    usage: dict[str, Any]
    usage_status: str
    cost: dict[str, Any]
    cost_status: str
    pricing_revision: str | None
    error: dict[str, Any] | None
    started_at: datetime
    ended_at: datetime | None


class ContextSnapshotRead(FromAttributesModel):
    id: UUID
    run_id: UUID
    parent_snapshot_id: UUID | None
    conversation_id: UUID | None
    conversation_turn_id: UUID | None
    schema_version: int
    version: int
    reason: str
    source_refs: list[Any]
    memory_refs: list[dict[str, Any]]
    skill_refs: list[dict[str, Any]]
    effect_metadata: dict[str, Any]
    rendered_messages: list[dict[str, Any]]
    token_estimate: int
    truncation: dict[str, Any]
    selected_turn_ids: list[str]
    conversation_summary_hash: str | None
    artifact_refs: list[dict[str, Any]]
    token_budget: int | None
    content_hash: str
    created_at: datetime


class RuntimeKnowledgeUsageRead(FromAttributesModel):
    id: UUID
    run_id: UUID
    source_type: Literal["memory", "skill_version"]
    memory_id: UUID | None
    skill_id: UUID | None
    skill_version_id: UUID | None
    source_version: int
    content_hash: str
    scope_type: str
    selection: dict[str, Any]
    status: str
    first_context_snapshot_id: UUID | None
    first_model_call_id: UUID | None
    context_count: int
    model_call_count: int
    outcome_status: str | None
    result_hash: str | None
    effect_metadata: dict[str, Any]
    first_consumed_at: datetime | None
    ended_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime
