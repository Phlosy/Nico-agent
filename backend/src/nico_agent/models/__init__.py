"""Provider-neutral model access for Nico's native runtime."""

from nico_agent.models.contracts import (
    DiscoveredModel,
    ModelDiscoveryRequest,
    ModelDiscoveryResult,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelToolCall,
    ModelToolDefinition,
    ModelUsage,
)
from nico_agent.models.gateway import ModelGateway
from nico_agent.models.registry import ModelProviderRegistry

__all__ = [
    "DiscoveredModel",
    "ModelDiscoveryRequest",
    "ModelDiscoveryResult",
    "ModelGateway",
    "ModelMessage",
    "ModelProviderRegistry",
    "ModelRequest",
    "ModelResponse",
    "ModelStreamEvent",
    "ModelStreamEventType",
    "ModelToolCall",
    "ModelToolDefinition",
    "ModelUsage",
]
