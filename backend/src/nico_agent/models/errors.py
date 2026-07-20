"""Stable, non-sensitive Model Gateway errors."""

from nico_agent.domain.errors import DomainError


class ModelError(DomainError):
    pass


class ModelProviderNotFound(ModelError):
    def __init__(self, name: str) -> None:
        super().__init__("MODEL_PROVIDER_NOT_FOUND", f"model provider {name} is not registered")


class ModelCapabilityMismatch(ModelError):
    def __init__(self, capability: str) -> None:
        super().__init__(
            "MODEL_CAPABILITY_MISMATCH",
            f"selected model endpoint does not provide required capability {capability}",
        )


class ModelEndpointDenied(ModelError):
    def __init__(self, message: str = "model endpoint is denied by deployment policy") -> None:
        super().__init__("MODEL_ENDPOINT_DENIED", message)


class ModelCredentialUnavailable(ModelError):
    def __init__(self) -> None:
        super().__init__("MODEL_CREDENTIAL_UNAVAILABLE", "model credential is unavailable")


class ModelProviderError(ModelError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(code, message, details={"retryable": retryable})
        self.retryable = retryable


class ModelProtocolError(ModelError):
    def __init__(self, message: str) -> None:
        super().__init__("MODEL_PROTOCOL_ERROR", message)


class ModelDiscoveryUnavailable(ModelError):
    def __init__(self, message: str = "model discovery is unavailable for this provider") -> None:
        super().__init__("MODEL_DISCOVERY_UNAVAILABLE", message)


class ModelRateLimited(ModelError):
    def __init__(self) -> None:
        super().__init__("MODEL_RATE_LIMITED", "model request exceeded its distributed rate limit")


class ModelRateLimitUnavailable(ModelError):
    def __init__(self) -> None:
        super().__init__(
            "MODEL_RATE_LIMIT_UNAVAILABLE",
            "hard model rate limiting is temporarily unavailable",
        )
