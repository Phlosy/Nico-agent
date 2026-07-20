"""Provider-neutral runtime contracts and built-in adapters."""

from nico_agent.runtime.contracts import (
    AgentRuntimeProvider,
    AgentRuntimeProviderV2,
    ContextSeed,
    RuntimeCapability,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeExecutionMode,
    RuntimeOutcome,
    RuntimeProviderDescriptor,
    RuntimeResult,
    RuntimeServices,
    RuntimeSessionHandle,
    RuntimeSessionRequest,
    RuntimeSessionStatus,
    RuntimeToolHandler,
    RuntimeToolIntent,
    RuntimeToolOutcome,
    RuntimeToolSession,
    RuntimeToolSpec,
    RuntimeTrajectory,
)
from nico_agent.runtime.hermes import HermesRuntimeProvider
from nico_agent.runtime.mock import MockRuntimeProvider
from nico_agent.runtime.native import NicoNativeRuntimeProvider
from nico_agent.runtime.registry import RuntimeProviderRegistry

__all__ = [
    "AgentRuntimeProvider",
    "AgentRuntimeProviderV2",
    "ContextSeed",
    "MockRuntimeProvider",
    "HermesRuntimeProvider",
    "NicoNativeRuntimeProvider",
    "RuntimeCapability",
    "RuntimeEvent",
    "RuntimeEventType",
    "RuntimeExecutionMode",
    "RuntimeOutcome",
    "RuntimeProviderDescriptor",
    "RuntimeProviderRegistry",
    "RuntimeResult",
    "RuntimeSessionHandle",
    "RuntimeSessionRequest",
    "RuntimeSessionStatus",
    "RuntimeTrajectory",
    "RuntimeServices",
    "RuntimeToolHandler",
    "RuntimeToolIntent",
    "RuntimeToolOutcome",
    "RuntimeToolSession",
    "RuntimeToolSpec",
]
