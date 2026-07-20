"""Immutable provider-neutral model DTOs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class ModelCapability(StrEnum):
    STREAMING = "streaming"
    TOOLS = "tools"
    STRUCTURED_OUTPUT = "structured_output"


class ModelStreamEventType(StrEnum):
    RESPONSE_STARTED = "response.started"
    TEXT_DELTA = "text.delta"
    TOOL_CALL_DELTA = "tool_call.delta"
    USAGE = "usage"
    RESPONSE_COMPLETED = "response.completed"


class ModelMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    name: str | None = Field(default=None, max_length=120)
    tool_call_id: str | None = Field(default=None, max_length=300)
    tool_calls: tuple[dict[str, Any], ...] = ()


class ModelToolDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=4000)
    input_schema: dict[str, Any]


class ModelRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    model: str = Field(min_length=1, max_length=200)
    messages: tuple[ModelMessage, ...] = Field(min_length=1)
    endpoint: dict[str, Any]
    tools: tuple[ModelToolDefinition, ...] = ()
    response_format: dict[str, Any] | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=1)
    timeout_seconds: float | None = Field(default=None, gt=0, le=3600)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    status: Literal["missing", "partial", "exact"] = "missing"


class ModelToolCall(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=300)
    name: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any]


class ModelStreamEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: ModelStreamEventType
    text_delta: str | None = None
    tool_index: int | None = Field(default=None, ge=0)
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments_delta: str | None = None
    finish_reason: str | None = None
    usage: ModelUsage | None = None
    provider_request_id: str | None = None


class ModelResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = ""
    tool_calls: tuple[ModelToolCall, ...] = ()
    finish_reason: str | None = None
    usage: ModelUsage = Field(default_factory=ModelUsage)
    provider_request_id: str | None = None


class DiscoveredModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=200)
    display_name: str | None = Field(default=None, max_length=200)
    capabilities: tuple[ModelCapability, ...] = ()


class ModelDiscoveryRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    endpoint: dict[str, Any]
    limit: int = Field(default=1000, ge=1, le=1000)


class ModelDiscoveryResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    models: tuple[DiscoveredModel, ...]
    truncated: bool = False


@runtime_checkable
class ModelProvider(Protocol):
    @property
    def name(self) -> str: ...

    def describe_capabilities(self) -> frozenset[ModelCapability]: ...

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]: ...


@runtime_checkable
class ModelDiscoveryProvider(Protocol):
    def discover(self, request: ModelDiscoveryRequest) -> Any: ...
