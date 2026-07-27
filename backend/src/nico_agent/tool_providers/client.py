"""Authenticated, DNS-pinned HTTP client for Tool Provider Protocol v1."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlunsplit
from uuid import UUID, uuid4

from pydantic import ValidationError

from nico_agent.net.safe_http import (
    RawHttpResponse,
    SafeHttpClient,
    SafeHttpError,
    SafeHttpPolicy,
    canonicalize_http_url,
    resolve_http_target,
)
from nico_agent.tool_providers.contracts import (
    ProviderCancelRequest,
    ProviderCancelResponse,
    ProviderCapabilitiesResponse,
    ProviderHealthResponse,
    ProviderToolCallRequest,
    ProviderToolCallResponse,
    canonical_json_bytes,
)
from nico_agent.tool_providers.errors import (
    ToolProviderError,
    ToolProviderErrorCode,
    provider_error,
)
from nico_agent.tool_providers.security import sign_hmac_request
from nico_agent.tools.secrets import EnvironmentSecretResolver, SecretResolver

_UNBOUND_DIGEST = f"sha256:{'0' * 64}"


@dataclass(frozen=True, slots=True)
class ProviderEndpoint:
    provider_id: UUID
    endpoint_ref: str
    credential_ref: str
    allow_http: bool = False
    allow_loopback: bool = False
    allow_private: bool = False


class ToolProviderClient:
    """Protocol client that never follows redirects or uses proxy environment state."""

    def __init__(
        self,
        *,
        http: SafeHttpClient | None = None,
        secret_resolver: SecretResolver | None = None,
        connect_timeout: float = 5,
        read_timeout: float = 120,
        max_response_bytes: int = 1_048_576,
    ) -> None:
        self.http = http or SafeHttpClient(
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
        )
        self.secret_resolver = secret_resolver or EnvironmentSecretResolver()
        self.max_response_bytes = max_response_bytes

    async def validate_endpoint(self, endpoint: ProviderEndpoint) -> str:
        """Resolve every address now; every actual request repeats this validation."""

        policy = self._policy(endpoint)
        target = await resolve_http_target(
            endpoint.endpoint_ref,
            policy=policy,
            resolver=self.http.resolver,
        )
        return target.origin

    async def health(self, endpoint: ProviderEndpoint) -> ProviderHealthResponse:
        request_id = f"health-{uuid4()}"
        response = await self._request(
            endpoint,
            "GET",
            "/v1/health",
            request_id=request_id,
            binding_digest=_UNBOUND_DIGEST,
        )
        return self._validated(
            ProviderHealthResponse,
            response,
            endpoint=endpoint,
            request_id=request_id,
        )

    async def capabilities(
        self,
        endpoint: ProviderEndpoint,
    ) -> ProviderCapabilitiesResponse:
        request_id = f"capabilities-{uuid4()}"
        response = await self._request(
            endpoint,
            "GET",
            "/v1/capabilities",
            request_id=request_id,
            binding_digest=_UNBOUND_DIGEST,
        )
        return self._validated(
            ProviderCapabilitiesResponse,
            response,
            endpoint=endpoint,
            request_id=request_id,
        )

    async def execute(
        self,
        endpoint: ProviderEndpoint,
        request: ProviderToolCallRequest,
        *,
        max_response_bytes: int | None = None,
    ) -> ProviderToolCallResponse:
        response = await self._request(
            endpoint,
            "POST",
            "/v1/tool-calls",
            request_id=request.request_id,
            binding_digest=request.binding_digest,
            body=canonical_json_bytes(request),
            max_response_bytes=max_response_bytes,
            deadline=request.deadline,
        )
        return self._validated(
            ProviderToolCallResponse,
            response,
            endpoint=endpoint,
            request_id=request.request_id,
            run_id=request.run_id,
            tool_call_id=_uuid_or_none(request.tool_call_id),
            attempt=request.attempt,
        )

    async def cancel(
        self,
        endpoint: ProviderEndpoint,
        request: ProviderCancelRequest,
    ) -> ProviderCancelResponse:
        response = await self._request(
            endpoint,
            "POST",
            f"/v1/tool-calls/{request.request_id}/cancel",
            request_id=request.request_id,
            binding_digest=request.binding_digest,
            body=canonical_json_bytes(request),
            deadline=request.deadline,
        )
        return self._validated(
            ProviderCancelResponse,
            response,
            endpoint=endpoint,
            request_id=request.request_id,
            run_id=request.run_id,
        )

    async def _request(
        self,
        endpoint: ProviderEndpoint,
        method: str,
        suffix: str,
        *,
        request_id: str,
        binding_digest: str,
        body: bytes | None = None,
        max_response_bytes: int | None = None,
        deadline: datetime | None = None,
    ) -> RawHttpResponse:
        deadline_timeout: float | None = None
        if deadline is not None:
            deadline_timeout = (deadline - datetime.now(UTC)).total_seconds()
            if deadline_timeout <= 0:
                raise provider_error(
                    ToolProviderErrorCode.TIMEOUT,
                    "Tool Provider request deadline has elapsed",
                    provider_id=endpoint.provider_id,
                    cause="DEADLINE_ELAPSED",
                )
        secret = self.secret_resolver.resolve("tool_provider", endpoint.credential_ref)
        url, signed_path = _endpoint_url(endpoint.endpoint_ref, suffix)
        signature = sign_hmac_request(
            secret=secret,
            method=method,
            path_with_query=signed_path,
            body=body or b"",
            provider_id=str(endpoint.provider_id),
            binding_digest=binding_digest,
            request_id=request_id,
            timestamp=int(datetime.now(UTC).timestamp()),
            nonce=secrets.token_hex(16),
        )
        headers = {
            **signature.as_http_headers(),
            "Accept": "application/json",
        }
        if method == "POST":
            headers["Content-Type"] = "application/json"
        try:
            return await self.http.request(
                method,
                url,
                policy=self._policy(endpoint),
                headers=headers,
                body=body,
                max_request_bytes=1_048_576,
                max_response_bytes=max_response_bytes or self.max_response_bytes,
                max_redirects=0,
                connect_timeout=(
                    min(self.http.connect_timeout, deadline_timeout)
                    if deadline_timeout is not None
                    else None
                ),
                read_timeout=(
                    min(self.http.read_timeout, deadline_timeout)
                    if deadline_timeout is not None
                    else None
                ),
            )
        except ToolProviderError:
            raise
        except SafeHttpError as exc:
            code = (
                ToolProviderErrorCode.TIMEOUT
                if exc.code in {"TIMEOUT", "READ_TIMEOUT"}
                else ToolProviderErrorCode.CONNECTION_ERROR
            )
            raise provider_error(
                code,
                "Tool Provider request failed",
                provider_id=endpoint.provider_id,
                cause=_stable_cause(exc.code),
            ) from exc

    @staticmethod
    def _policy(endpoint: ProviderEndpoint) -> SafeHttpPolicy:
        return SafeHttpPolicy.exact_endpoint(
            endpoint.endpoint_ref,
            allow_http=endpoint.allow_http,
            allow_loopback=endpoint.allow_loopback,
            allow_private=endpoint.allow_private,
        )

    @staticmethod
    def _validated(
        model: type[Any],
        response: RawHttpResponse,
        *,
        endpoint: ProviderEndpoint,
        request_id: str,
        run_id: UUID | None = None,
        tool_call_id: UUID | None = None,
        attempt: int | None = None,
    ) -> Any:
        if response.status != 200:
            code, retryable = _http_error(response.status)
            raise provider_error(
                code,
                f"Tool Provider rejected the request with HTTP {response.status}",
                provider_id=endpoint.provider_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                attempt=attempt,
                cause=f"HTTP_{response.status}",
                retryable=retryable,
            )
        try:
            value = model.model_validate_json(response.body)
        except ValidationError as exc:
            raise provider_error(
                ToolProviderErrorCode.INVALID_RESPONSE,
                "Tool Provider returned an invalid protocol response",
                provider_id=endpoint.provider_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                attempt=attempt,
                cause="RESPONSE_VALIDATION_FAILED",
            ) from exc
        if str(value.provider_id) != str(endpoint.provider_id):
            raise provider_error(
                ToolProviderErrorCode.PROTOCOL_ERROR,
                "Tool Provider response identity does not match the registry",
                provider_id=endpoint.provider_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                attempt=attempt,
                cause="PROVIDER_ID_MISMATCH",
            )
        if hasattr(value, "request_id") and value.request_id != request_id:
            raise provider_error(
                ToolProviderErrorCode.PROTOCOL_ERROR,
                "Tool Provider response request identity does not match",
                provider_id=endpoint.provider_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                attempt=attempt,
                cause="REQUEST_ID_MISMATCH",
            )
        return value


def _endpoint_url(base_url: str, suffix: str) -> tuple[str, str]:
    parsed = canonicalize_http_url(base_url)
    base_path = parsed.path.rstrip("/")
    path = f"{base_path}/{suffix.lstrip('/')}"
    url = urlunsplit((parsed.scheme, parsed.host_header, path, "", ""))
    return url, path


def _http_error(status: int) -> tuple[ToolProviderErrorCode, bool]:
    if status == 401:
        return ToolProviderErrorCode.AUTH_ERROR, False
    if status == 403:
        return ToolProviderErrorCode.PERMISSION_DENIED, False
    if status == 404:
        return ToolProviderErrorCode.CAPABILITY_MISMATCH, False
    if status == 429:
        return ToolProviderErrorCode.RATE_LIMIT, True
    if 500 <= status < 600:
        return ToolProviderErrorCode.CONNECTION_ERROR, True
    return ToolProviderErrorCode.PROTOCOL_ERROR, False


def _stable_cause(value: str) -> str:
    normalized = "".join(character if character.isalnum() else "_" for character in value.upper())
    return (normalized or "NETWORK_ERROR")[:100]


def _uuid_or_none(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None
