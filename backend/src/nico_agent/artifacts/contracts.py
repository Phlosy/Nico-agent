"""Provider-safe contracts for bounded Artifact storage."""

from __future__ import annotations

import base64
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RuntimeArtifactIntent(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=300)
    content_type: str = Field(default="text/plain", min_length=1, max_length=200)
    content_text: str | None = Field(default=None, max_length=10_485_760)
    content_base64: str | None = Field(default=None, max_length=13981016)
    artifact_type: str = Field(default="file", min_length=1, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)
    share_with_parent: bool = True
    idempotency_key: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def exactly_one_content(self) -> RuntimeArtifactIntent:
        if (self.content_text is None) == (self.content_base64 is None):
            raise ValueError("exactly one of content_text or content_base64 is required")
        return self

    def content_bytes(self) -> bytes:
        if self.content_text is not None:
            return self.content_text.encode()
        try:
            return base64.b64decode(self.content_base64 or "", validate=True)
        except ValueError as exc:
            raise ValueError("content_base64 is invalid") from exc


class RuntimeArtifactOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_id: UUID
    status: str
    name: str
    content_type: str
    sha256: str
    size_bytes: int = Field(ge=0)
    shared_with_run_ids: tuple[UUID, ...] = ()
