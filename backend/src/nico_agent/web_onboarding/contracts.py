"""Secret-free contracts for Web Provider onboarding."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nico_agent.web.contracts import SearchRequest

WebProviderKey = Literal["brave", "searxng"]


class FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class WebEndpointChoice(FrozenContract):
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2000)
    default: bool = False


class WebProviderPreset(FrozenContract):
    key: WebProviderKey
    display_name: str = Field(min_length=1, max_length=100)
    requires_secret: bool
    secret_name: str | None = Field(default=None, max_length=100)
    credential_env_prefix: str | None = Field(default=None, max_length=120)
    endpoints: tuple[WebEndpointChoice, ...]
    default_config: dict[str, object]
    documentation_url: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def validate_secret_shape(self) -> WebProviderPreset:
        if self.requires_secret != bool(self.secret_name and self.credential_env_prefix):
            raise ValueError("Web Provider Secret metadata is inconsistent")
        if not self.endpoints or sum(choice.default for choice in self.endpoints) != 1:
            raise ValueError("Web Provider must define exactly one default endpoint")
        return self


class WebProviderCatalog(FrozenContract):
    schema_version: int = 1
    catalog_revision: str = Field(min_length=1, max_length=64)
    providers: tuple[WebProviderPreset, ...]


class WebSearchPolicy(FrozenContract):
    safe_search: Literal["off", "moderate", "strict"] = "moderate"
    cache_ttl_seconds: int = Field(default=900, ge=1, le=86_400)
    rate_limit_per_minute: int = Field(default=20, ge=1, le=10_000)
    allowed_domains: tuple[str, ...] = Field(default=(), max_length=100)

    @field_validator("allowed_domains")
    @classmethod
    def validate_domains(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return SearchRequest(query="policy validation", domains=values).domains


class WebProviderCandidate(FrozenContract):
    schema_version: int = 1
    provider: WebProviderKey
    endpoint_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    credential_ref: str | None = Field(default=None, max_length=300)
    policy: WebSearchPolicy = Field(default_factory=WebSearchPolicy)
    catalog_revision: str = Field(min_length=1, max_length=64)

    @field_validator("credential_ref")
    @classmethod
    def validate_credential_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        import re

        if re.fullmatch(r"env:NICO_TOOL_SECRET_[A-Z0-9_]{1,100}", value) is None:
            raise ValueError("Web credentials must use a Nico tool Secret environment reference")
        return value


class WebProbeCreate(FrozenContract):
    candidate: WebProviderCandidate
    idempotency_key: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$",
    )


class WebProbeRead(FrozenContract):
    id: UUID
    status: str
    provider: WebProviderKey
    candidate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: dict[str, object]
    error_code: str | None
    error_detail: str | None
    verified_at: datetime | None
    activated_at: datetime | None
    revision: int


class WebActivationTarget(FrozenContract):
    project_id: UUID
    expected_tenant_revision: int = Field(ge=1)
    agent_id: UUID | None = None
    expected_agent_revision: int | None = Field(default=None, ge=1)
    starter_agent_name: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]{1,118}[a-z0-9]$",
        max_length=120,
    )
    starter_agent_display_name: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_target(self) -> WebActivationTarget:
        existing = self.agent_id is not None
        starter = self.starter_agent_name is not None
        if existing == starter:
            raise ValueError("select exactly one existing or Starter Agent")
        if existing and self.expected_agent_revision is None:
            raise ValueError("existing Agent activation requires its expected revision")
        if existing and self.starter_agent_display_name is not None:
            raise ValueError("existing Agent activation cannot include a Starter display name")
        if starter and self.expected_agent_revision is not None:
            raise ValueError("Starter Agent activation cannot include an expected revision")
        if starter and self.starter_agent_display_name is None:
            raise ValueError("Starter Agent activation requires a display name")
        return self


class WebPreviewCreate(FrozenContract):
    probe_id: UUID
    candidate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    target: WebActivationTarget


class WebActivationCreate(WebPreviewCreate):
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    maintenance_attempt_id: UUID | None = None


class WebActivationPreview(FrozenContract):
    schema_version: int = 1
    probe_id: UUID
    candidate_hash: str
    preview_hash: str
    expires_at: datetime
    changed_fields: tuple[str, ...]
    projection: dict[str, object]


class WebActivationRead(FrozenContract):
    schema_version: int = 1
    probe_id: UUID
    candidate_hash: str
    provider: WebProviderKey
    tenant_revision: int
    agent_id: UUID
    agent_revision: int
    agent_version_id: UUID
    agent_version: int
    project_id: UUID
    activated_at: datetime


class WebSetupReadiness(FrozenContract):
    schema_version: int = 1
    writes_enabled: bool
    reason: Literal["ready", "deployment_policy_disabled"]
    tenant_revision: int


class WebAuthorizedAgent(FrozenContract):
    id: UUID
    name: str
    revision: int
    current_version_id: UUID
    current_version: int


class WebProviderStatus(FrozenContract):
    schema_version: int = 1
    writes_enabled: bool
    tenant_revision: int
    configured: bool
    authorized: bool
    enabled: bool
    provider: WebProviderKey | None = None
    endpoint_key: str | None = None
    credential_ref: str | None = None
    secret_required: bool = False
    diagnosis: Literal[
        "ready",
        "unconfigured",
        "unauthorized",
        "provider_unreachable",
        "recent_probe_failed",
    ]
    latest_probe: WebProbeRead | None = None
    agents: tuple[WebAuthorizedAgent, ...] = ()


class WebConfigurationTestCreate(FrozenContract):
    idempotency_key: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$",
    )


class WebDisableTarget(FrozenContract):
    agent_id: UUID
    expected_tenant_revision: int = Field(ge=1)
    expected_agent_revision: int = Field(ge=1)


class WebDisablePreviewCreate(FrozenContract):
    target: WebDisableTarget


class WebDisableCreate(WebDisablePreviewCreate):
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class WebDisablePreview(FrozenContract):
    schema_version: int = 1
    preview_hash: str
    changed_fields: tuple[str, ...]
    projection: dict[str, object]


class WebDisableRead(FrozenContract):
    schema_version: int = 1
    provider: WebProviderKey
    tenant_revision: int
    agent_id: UUID
    agent_revision: int
    agent_version_id: UUID
    agent_version: int
    disabled_at: datetime


def canonical_candidate_hash(candidate: WebProviderCandidate) -> str:
    encoded = json.dumps(
        candidate.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
