"""Stable errors emitted by runtime providers and orchestration."""

from __future__ import annotations

from nico_agent.domain.errors import DomainError
from nico_agent.runtime.contracts import RuntimeCapability


class RuntimeProviderError(DomainError):
    pass


class RuntimeSessionNotFound(RuntimeProviderError):
    def __init__(self, external_session_id: str) -> None:
        super().__init__(
            "RUNTIME_SESSION_NOT_FOUND",
            "runtime session was not found",
            details={"external_session_id": external_session_id},
        )


class RuntimeCapabilityUnsupported(RuntimeProviderError):
    def __init__(self, provider: str, capability: RuntimeCapability) -> None:
        super().__init__(
            "RUNTIME_CAPABILITY_UNSUPPORTED",
            f"runtime provider {provider} does not support {capability.value}",
            details={"provider": provider, "capability": capability.value},
        )


class RuntimeExecutionFailed(RuntimeProviderError):
    def __init__(self, provider: str, code: str, message: str) -> None:
        super().__init__(
            "RUNTIME_EXECUTION_FAILED",
            message,
            details={"provider": provider, "runtime_code": code},
        )
