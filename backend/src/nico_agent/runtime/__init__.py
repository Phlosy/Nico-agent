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

__all__ = [
    "AgentRuntimeProvider",
    "RuntimeCapability",
    "RuntimeEvent",
    "RuntimeEventType",
    "RuntimeProviderDescriptor",
    "RuntimeResult",
    "RuntimeSessionHandle",
    "RuntimeSessionRequest",
    "RuntimeSessionStatus",
    "RuntimeTrajectory",
]
