"""Shared network safety primitives."""

from nico_agent.net.safe_http import (
    HttpTransport,
    PinnedRequest,
    RawHttpResponse,
    ResolvedHttpTarget,
    SafeHttpClient,
    SafeHttpError,
    SafeHttpPolicy,
    SafeHttpResult,
    SocketHttpTransport,
    canonicalize_http_url,
    domain_allowed,
    resolve_http_target,
)

__all__ = [
    "HttpTransport",
    "PinnedRequest",
    "RawHttpResponse",
    "ResolvedHttpTarget",
    "SafeHttpClient",
    "SafeHttpError",
    "SafeHttpPolicy",
    "SafeHttpResult",
    "SocketHttpTransport",
    "canonicalize_http_url",
    "domain_allowed",
    "resolve_http_target",
]
