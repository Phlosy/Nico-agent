"""Versioned, secret-free contracts shared by Provider onboarding surfaces."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ProtocolFamily = Literal["openai_compatible", "anthropic_messages", "google_gemini"]
DiscoveryStrategy = Literal["openai_models", "anthropic_models", "gemini_models", "curated"]

_SAFE_KEY = re.compile(r"^[a-z][a-z0-9_-]{1,118}[a-z0-9]$")
_CREDENTIAL_REF = re.compile(
    r"^(?:env:NICO_MODEL_SECRET_[A-Z0-9_]{1,100}|secret:[A-Za-z0-9._:/-]{1,240})$"
)
_BIDI_CONTROLS = {
    "\u061c",
    "\u200e",
    "\u200f",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
}


def _safe_text(value: str, *, maximum: int, field_name: str) -> str:
    normalized = unicodedata.normalize("NFC", value.strip())
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} must contain 1-{maximum} characters")
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise ValueError(f"{field_name} contains control characters")
    if any(character in _BIDI_CONTROLS for character in normalized):
        raise ValueError(f"{field_name} contains directional controls")
    return normalized


def normalize_https_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("service locations must be credential-free HTTPS URLs")
    if parsed.query or parsed.fragment:
        raise ValueError("service locations cannot include query or fragment data")
    hostname = parsed.hostname.lower().rstrip(".")
    port = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
    path = parsed.path.rstrip("/")
    return urlunsplit(("https", f"{hostname}{port}", path, "", ""))


class FrozenContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProviderLocation(FrozenContract):
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$", max_length=64)
    label: str = Field(min_length=1, max_length=100)
    base_url: str = Field(max_length=2000)
    default: bool = False

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        return _safe_text(value, maximum=100, field_name="location label")

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        return normalize_https_url(value)


class ProviderOption(FrozenContract):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$", max_length=64)
    label: str = Field(min_length=1, max_length=100)
    required: bool = False
    choices: tuple[str, ...] = ()

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        return _safe_text(value, maximum=100, field_name="option label")


class ProviderPreset(FrozenContract):
    key: str = Field(max_length=120)
    display_name: str = Field(min_length=1, max_length=200)
    protocol: ProtocolFamily
    locations: tuple[ProviderLocation, ...]
    authentication: Literal["api_key"] = "api_key"
    discovery: DiscoveryStrategy
    recommended_models: tuple[str, ...]
    capabilities: dict[str, bool] = Field(default_factory=dict)
    options: tuple[ProviderOption, ...] = ()
    documentation_url: str = Field(max_length=2000)

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not _SAFE_KEY.fullmatch(value):
            raise ValueError("invalid provider key")
        return value

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        return _safe_text(value, maximum=200, field_name="display name")

    @field_validator("recommended_models")
    @classmethod
    def validate_models(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("at least one recommended model is required")
        normalized = tuple(
            _safe_text(value, maximum=200, field_name="model ID") for value in values
        )
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate recommended model")
        return normalized

    @field_validator("documentation_url")
    @classmethod
    def validate_documentation_url(cls, value: str) -> str:
        return normalize_https_url(value)

    @model_validator(mode="after")
    def validate_locations(self) -> ProviderPreset:
        if not self.locations:
            raise ValueError("at least one service location is required")
        keys = [location.key for location in self.locations]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate service location key")
        defaults = [location for location in self.locations if location.default]
        if len(self.locations) > 1 and len(defaults) != 1:
            raise ValueError("multi-region providers require exactly one default location")
        return self


class ProviderCatalog(FrozenContract):
    schema_version: int = Field(ge=1)
    catalog_revision: str = Field(min_length=1, max_length=64)
    providers: tuple[ProviderPreset, ...]

    @model_validator(mode="after")
    def validate_provider_keys(self) -> ProviderCatalog:
        keys = [provider.key for provider in self.providers]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate provider key")
        return self


class CandidateConfiguration(FrozenContract):
    schema_version: int = Field(default=1, ge=1)
    provider_key: str = Field(max_length=120)
    protocol: ProtocolFamily
    base_url: str = Field(max_length=2000)
    credential_ref: str = Field(max_length=300)
    model: str | None = Field(default=None, max_length=200)
    provider_options: dict[str, Any] = Field(default_factory=dict)
    catalog_revision: str = Field(min_length=1, max_length=64)

    @field_validator("provider_key")
    @classmethod
    def validate_provider_key(cls, value: str) -> str:
        if not _SAFE_KEY.fullmatch(value):
            raise ValueError("invalid provider key")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_candidate_url(cls, value: str) -> str:
        return normalize_https_url(value)

    @field_validator("credential_ref")
    @classmethod
    def validate_credential_ref(cls, value: str) -> str:
        if not _CREDENTIAL_REF.fullmatch(value):
            raise ValueError("credential must be an env or Nico Secret reference")
        return value

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _safe_text(value, maximum=200, field_name="model ID")


class ProviderProbeCreate(FrozenContract):
    kind: Literal["discover_models", "verify_completion"]
    candidate: CandidateConfiguration
    idempotency_key: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$",
    )

    @model_validator(mode="after")
    def validate_kind_shape(self) -> ProviderProbeCreate:
        if self.kind == "verify_completion" and self.candidate.model is None:
            raise ValueError("verification requires a model ID")
        if self.kind == "discover_models" and self.candidate.model is not None:
            raise ValueError("discovery candidate must not select a model")
        return self


class ProviderProbeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: UUID
    kind: str
    status: str
    provider_key: str
    protocol: str
    base_url: str
    credential_ref: str
    provider_options: dict[str, Any]
    model_name: str | None
    catalog_revision: str
    candidate_hash: str
    result: dict[str, Any]
    error_code: str | None
    error_detail: str | None
    attempt: int
    worker_id: str | None
    lease_expires_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    verified_at: datetime | None
    activated_at: datetime | None
    revision: int
    created_at: datetime
    updated_at: datetime


class ProviderActivationTarget(FrozenContract):
    project_id: UUID
    agent_id: UUID | None = None
    expected_agent_revision: int | None = Field(default=None, ge=1)
    starter_agent_name: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]{1,118}[a-z0-9]$",
        max_length=120,
    )
    starter_agent_display_name: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_target(self) -> ProviderActivationTarget:
        existing = self.agent_id is not None
        starter = self.starter_agent_name is not None
        if existing == starter:
            raise ValueError("select exactly one existing or Starter Agent")
        if existing:
            if self.expected_agent_revision is None:
                raise ValueError("existing Agent activation requires its expected revision")
            if self.starter_agent_display_name is not None:
                raise ValueError("existing Agent activation cannot include a Starter display name")
        else:
            if self.expected_agent_revision is not None:
                raise ValueError("Starter Agent activation cannot include an expected revision")
            if self.starter_agent_display_name is None:
                raise ValueError("Starter Agent activation requires a display name")
        return self


class ProviderPreviewCreate(FrozenContract):
    probe_id: UUID
    candidate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    target: ProviderActivationTarget


class ProviderActivationCreate(ProviderPreviewCreate):
    preview_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    maintenance_attempt_id: UUID | None = None


class ProviderActivationPreview(FrozenContract):
    schema_version: int = 1
    probe_id: UUID
    candidate_hash: str
    preview_hash: str
    expires_at: datetime
    changed_fields: tuple[str, ...]
    projection: dict[str, Any]


class ProviderActivationRead(FrozenContract):
    schema_version: int = 1
    probe_id: UUID
    candidate_hash: str
    endpoint_id: UUID
    endpoint_revision: int
    endpoint_reused: bool
    agent_id: UUID
    agent_revision: int
    agent_version_id: UUID
    agent_version: int
    project_id: UUID
    activated_at: datetime


def canonical_candidate_bytes(candidate: CandidateConfiguration) -> bytes:
    payload = candidate.model_dump(mode="json", exclude_none=False)
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_candidate_hash(candidate: CandidateConfiguration) -> str:
    return hashlib.sha256(canonical_candidate_bytes(candidate)).hexdigest()
