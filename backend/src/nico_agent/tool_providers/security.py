"""Pure endpoint and HMAC validation for Tool Provider Protocol v1."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nico_agent.tool_providers.errors import ToolProviderErrorCode, provider_error

PROTOCOL_IDENTIFIER = "nico-tool-provider-v1"
PROTOCOL_VERSION = "1"
AUTHORIZATION_SCHEME = "Nico-HMAC-SHA256"

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_NONCE = re.compile(r"^[0-9a-f]{32}$")
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,299}$")
_HTTP_HEADER_NAMES = {
    "x-nico-protocol": "protocol",
    "x-nico-provider-id": "provider_id",
    "x-nico-binding-digest": "binding_digest",
    "x-nico-request-id": "request_id",
    "x-nico-timestamp": "timestamp",
    "x-nico-nonce": "nonce",
    "x-nico-content-sha256": "content_sha256",
    "authorization": "authorization",
}


class HMACSignatureHeaders(BaseModel):
    """Ephemeral request headers. Authorization is hidden from repr by design."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: str = PROTOCOL_IDENTIFIER
    provider_id: str = Field(min_length=1, max_length=300)
    binding_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    request_id: str = Field(min_length=1, max_length=300)
    timestamp: int = Field(ge=0)
    nonce: str = Field(pattern=r"^[0-9a-f]{32}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorization: str = Field(min_length=1, max_length=100, repr=False)

    @field_validator("protocol")
    @classmethod
    def validate_protocol(cls, value: str) -> str:
        if value != PROTOCOL_IDENTIFIER:
            raise ValueError("unsupported Tool Provider protocol")
        return value

    @field_validator("provider_id", "request_id")
    @classmethod
    def validate_opaque_ids(cls, value: str) -> str:
        if not _OPAQUE_ID.fullmatch(value):
            raise ValueError("Provider request identities must be opaque safe strings")
        return value

    @field_validator("authorization")
    @classmethod
    def validate_authorization(cls, value: str) -> str:
        prefix = f"{AUTHORIZATION_SCHEME} "
        if not value.startswith(prefix) or not _HEX_DIGEST.fullmatch(value.removeprefix(prefix)):
            raise ValueError("invalid Tool Provider authorization header")
        return value

    @classmethod
    def from_http_headers(cls, headers: Mapping[str, str]) -> HMACSignatureHeaders:
        normalized = {str(key).lower(): str(value) for key, value in headers.items()}
        missing = sorted(name for name in _HTTP_HEADER_NAMES if name not in normalized)
        if missing:
            raise ValueError("missing required Tool Provider authentication headers")
        values = {
            field_name: normalized[header_name]
            for header_name, field_name in _HTTP_HEADER_NAMES.items()
        }
        return cls(**values)

    def as_http_headers(self) -> dict[str, str]:
        return {
            "X-Nico-Protocol": self.protocol,
            "X-Nico-Provider-Id": self.provider_id,
            "X-Nico-Binding-Digest": self.binding_digest,
            "X-Nico-Request-Id": self.request_id,
            "X-Nico-Timestamp": str(self.timestamp),
            "X-Nico-Nonce": self.nonce,
            "X-Nico-Content-SHA256": self.content_sha256,
            "Authorization": self.authorization,
        }


class HMACVerification(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str
    binding_digest: str
    request_id: str
    timestamp: int
    nonce: str
    content_sha256: str


def validate_provider_endpoint(
    value: str,
    *,
    allow_http: bool = False,
    allow_http_loopback: bool = False,
) -> str:
    """Normalize a credential-free Provider endpoint without resolving DNS."""

    parsed = urlsplit(value.strip())
    hostname = parsed.hostname.lower().rstrip(".") if parsed.hostname else ""
    try:
        parsed_ip = ipaddress.ip_address(hostname)
    except ValueError:
        parsed_ip = None
    is_loopback_name = (
        hostname == "localhost"
        or hostname.endswith(".localhost")
        or (parsed_ip is not None and parsed_ip.is_loopback)
    )
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and (allow_http or (allow_http_loopback and is_loopback_name))
    ):
        raise ValueError("Tool Provider endpoints must use HTTPS")
    if not hostname or parsed.username or parsed.password:
        raise ValueError("Tool Provider endpoints must be credential-free absolute URLs")
    if parsed.query or parsed.fragment:
        raise ValueError("Tool Provider endpoints cannot include query or fragment data")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Tool Provider endpoint port is invalid") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Tool Provider endpoint port is invalid")
    default_port = 443 if parsed.scheme == "https" else 80
    authority_host = (
        f"[{hostname}]" if parsed_ip is not None and parsed_ip.version == 6 else hostname
    )
    authority = authority_host if port in {None, default_port} else f"{authority_host}:{port}"
    path = parsed.path.rstrip("/")
    return urlunsplit((parsed.scheme, authority, path, "", ""))


def provider_endpoint_identity(
    endpoint_url: str,
    *,
    allow_http: bool = False,
    allow_http_loopback: bool = False,
) -> str:
    """Return the opaque identity frozen into a Run instead of persisting its URL."""

    normalized = validate_provider_endpoint(
        endpoint_url,
        allow_http=allow_http,
        allow_http_loopback=allow_http_loopback,
    )
    return f"sha256:{hashlib.sha256(normalized.encode()).hexdigest()}"


def content_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def signature_payload(
    *,
    method: str,
    path_with_query: str,
    content_digest: str,
    provider_id: str,
    binding_digest: str,
    request_id: str,
    timestamp: int,
    nonce: str,
) -> bytes:
    normalized_method = _validate_method(method)
    normalized_path = _validate_path(path_with_query)
    if not _HEX_DIGEST.fullmatch(content_digest):
        raise ValueError("content digest must be a lowercase SHA-256 hex value")
    if not _OPAQUE_ID.fullmatch(provider_id) or not _OPAQUE_ID.fullmatch(request_id):
        raise ValueError("Provider request identities must be opaque safe strings")
    if not _DIGEST.fullmatch(binding_digest):
        raise ValueError("binding digest must use sha256:<lowercase hex>")
    if timestamp < 0:
        raise ValueError("timestamp cannot be negative")
    if not _NONCE.fullmatch(nonce):
        raise ValueError("nonce must be 128-bit lowercase hex")
    values = (
        normalized_method,
        normalized_path,
        content_digest,
        provider_id,
        binding_digest,
        request_id,
        str(timestamp),
        nonce,
    )
    return "\n".join(values).encode()


def sign_hmac_request(
    *,
    secret: str | bytes,
    method: str,
    path_with_query: str,
    body: bytes,
    provider_id: str,
    binding_digest: str,
    request_id: str,
    timestamp: int,
    nonce: str,
) -> HMACSignatureHeaders:
    key = _secret_bytes(secret)
    body_digest = content_sha256(body)
    payload = signature_payload(
        method=method,
        path_with_query=path_with_query,
        content_digest=body_digest,
        provider_id=provider_id,
        binding_digest=binding_digest,
        request_id=request_id,
        timestamp=timestamp,
        nonce=nonce,
    )
    signature = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return HMACSignatureHeaders(
        provider_id=provider_id,
        binding_digest=binding_digest,
        request_id=request_id,
        timestamp=timestamp,
        nonce=nonce,
        content_sha256=body_digest,
        authorization=f"{AUTHORIZATION_SCHEME} {signature}",
    )


def verify_hmac_request(
    *,
    headers: HMACSignatureHeaders | Mapping[str, str],
    secret: str | bytes,
    method: str,
    path_with_query: str,
    body: bytes,
    consume_nonce: Callable[[str, int], bool],
    now: datetime | int | None = None,
    window_seconds: int = 60,
) -> HMACVerification:
    """Verify signature/freshness/content and atomically consume the replay nonce."""

    parsed = (
        headers
        if isinstance(headers, HMACSignatureHeaders)
        else HMACSignatureHeaders.from_http_headers(headers)
    )
    if not 1 <= window_seconds <= 300:
        raise ValueError("signature acceptance window must be between 1 and 300 seconds")
    current_timestamp = _unix_timestamp(now)
    if abs(current_timestamp - parsed.timestamp) > window_seconds:
        raise provider_error(
            ToolProviderErrorCode.AUTH_ERROR,
            "Tool Provider request signature is stale",
            cause="SIGNATURE_STALE",
        )
    actual_content_digest = content_sha256(body)
    if not hmac.compare_digest(parsed.content_sha256, actual_content_digest):
        raise provider_error(
            ToolProviderErrorCode.AUTH_ERROR,
            "Tool Provider request content digest does not match",
            cause="CONTENT_DIGEST_MISMATCH",
        )
    payload = signature_payload(
        method=method,
        path_with_query=path_with_query,
        content_digest=parsed.content_sha256,
        provider_id=parsed.provider_id,
        binding_digest=parsed.binding_digest,
        request_id=parsed.request_id,
        timestamp=parsed.timestamp,
        nonce=parsed.nonce,
    )
    expected = hmac.new(_secret_bytes(secret), payload, hashlib.sha256).hexdigest()
    supplied = parsed.authorization.removeprefix(f"{AUTHORIZATION_SCHEME} ")
    if not hmac.compare_digest(expected, supplied):
        raise provider_error(
            ToolProviderErrorCode.AUTH_ERROR,
            "Tool Provider request signature is invalid",
            cause="SIGNATURE_INVALID",
        )
    if not consume_nonce(parsed.nonce, parsed.timestamp):
        raise provider_error(
            ToolProviderErrorCode.AUTH_ERROR,
            "Tool Provider request nonce was already consumed",
            cause="NONCE_REPLAY",
        )
    return HMACVerification(
        provider_id=parsed.provider_id,
        binding_digest=parsed.binding_digest,
        request_id=parsed.request_id,
        timestamp=parsed.timestamp,
        nonce=parsed.nonce,
        content_sha256=parsed.content_sha256,
    )


def verify_signed_body_identity(
    verification: HMACVerification,
    body: Mapping[str, object],
) -> None:
    """Require signed header identities to match their protocol body echoes."""

    expected = {
        "provider_id": verification.provider_id,
        "binding_digest": verification.binding_digest,
        "request_id": verification.request_id,
    }
    if any(body.get(name) != value for name, value in expected.items()):
        raise provider_error(
            ToolProviderErrorCode.AUTH_ERROR,
            "signed Tool Provider identity does not match the request body",
            cause="SIGNED_IDENTITY_MISMATCH",
        )


def _secret_bytes(value: str | bytes) -> bytes:
    encoded = value.encode() if isinstance(value, str) else value
    if len(encoded) < 32:
        raise ValueError("Tool Provider HMAC secret must contain at least 32 bytes")
    return encoded


def _validate_method(value: str) -> str:
    normalized = value.upper()
    if not normalized or any(not ("A" <= character <= "Z") for character in normalized):
        raise ValueError("HTTP method must contain ASCII letters only")
    return normalized


def _validate_path(value: str) -> str:
    if not value.startswith("/") or "\r" in value or "\n" in value or "#" in value:
        raise ValueError("signed request path must be an origin-form path without fragment")
    return value


def _unix_timestamp(value: datetime | int | None) -> int:
    if value is None:
        return int(datetime.now(UTC).timestamp())
    if isinstance(value, int):
        if value < 0:
            raise ValueError("current timestamp cannot be negative")
        return value
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("current time must be timezone-aware")
    return int(value.timestamp())
