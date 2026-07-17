"""Provider-neutral runtime contracts and built-in adapters."""

from nico_agent.runtime.contracts import (
    AgentRuntimeProvider,
    RuntimeCapability,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeProviderDescriptor,
    RuntimeResult,
    RuntimeSessionHandle,
    RuntimeSessionRequest,
    RuntimeSessionStatus,
    RuntimeTrajectory,
)
from nico_agent.runtime.hermes import HermesRuntimeProvider
from nico_agent.runtime.mock import MockRuntimeProvider
from nico_agent.runtime.registry import RuntimeProviderRegistry

__all__ = [
    "AgentRuntimeProvider",
    "MockRuntimeProvider",
    "HermesRuntimeProvider",
    "RuntimeCapability",
    "RuntimeEvent",
    "RuntimeEventType",
    "RuntimeProviderDescriptor",
    "RuntimeProviderRegistry",
    "RuntimeResult",
    "RuntimeSessionHandle",
    "RuntimeSessionRequest",
    "RuntimeSessionStatus",
    "RuntimeTrajectory",
]
