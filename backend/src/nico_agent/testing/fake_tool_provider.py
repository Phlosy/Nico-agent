"""Deterministic ASGI Tool Provider used by protocol and end-to-end tests."""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request
from pydantic import ValidationError
from starlette.responses import JSONResponse, Response

from nico_agent.tool_providers.contracts import (
    ProviderCancelRequest,
    ProviderCancelResponse,
    ProviderCancelStatus,
    ProviderCapabilitiesResponse,
    ProviderFeatureSet,
    ProviderHealthResponse,
    ProviderHealthStatus,
    ProviderToolCallRequest,
    ProviderToolCallResponse,
    ProviderToolCallStatus,
    ProviderToolContract,
    ProviderToolError,
    ProviderToolIdentity,
    schema_digest,
)
from nico_agent.tool_providers.errors import ToolProviderError
from nico_agent.tool_providers.security import verify_hmac_request

FAKE_ECHO_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}
FAKE_ECHO_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"echo": {"type": "object"}},
    "required": ["echo"],
    "additionalProperties": False,
}
FAKE_ECHO_TOOL = ProviderToolContract(
    name="nico.stub.echo",
    version="1.0.0",
    input_schema_digest=schema_digest(FAKE_ECHO_INPUT_SCHEMA),
    output_schema_digest=schema_digest(FAKE_ECHO_OUTPUT_SCHEMA),
)


class FakeToolProviderMode(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DELAY = "delay"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    MALFORMED_RESPONSE = "malformed_response"
    INVALID_SCHEMA = "invalid_schema"
    AUTH_FAILURE = "auth_failure"


@dataclass(frozen=True, slots=True)
class FakeToolProviderScope:
    """One exact run-scoped grant provisioned into the fake Provider."""

    binding_digest: str
    tenant_id: UUID
    project_id: UUID | None
    run_id: UUID
    task_id: UUID | None
    agent_id: UUID
    agent_version_id: UUID
    tool: ProviderToolContract


Clock = Callable[[], datetime]


class FakeToolProvider:
    """Stateful, hermetic Provider with deterministic faults and call accounting."""

    def __init__(
        self,
        *,
        provider_id: str,
        secret: str | bytes,
        supported_tools: Sequence[ProviderToolContract] = (FAKE_ECHO_TOOL,),
        clock: Clock | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.secret = secret
        self.supported_tools = tuple(supported_tools)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.mode = FakeToolProviderMode.SUCCESS
        self.delay_seconds = 0.0
        self.cancel_side_effects_may_continue = False
        self.call_counts: Counter[str] = Counter()
        self.app = FastAPI(title="Nico deterministic Tool Provider", docs_url=None)
        self._scopes: dict[str, FakeToolProviderScope] = {}
        self._nonces: set[str] = set()
        self._terminal_responses: dict[
            tuple[str, str, UUID, str], tuple[str, ProviderToolCallResponse]
        ] = {}
        self._active_requests: set[str] = set()
        self._cancelled_requests: set[str] = set()
        self._cancel_responses: dict[str, ProviderCancelResponse] = {}
        self._execution_lock = asyncio.Lock()
        self.execution_started = asyncio.Event()
        self.cancel_received = asyncio.Event()
        self._mount_routes()

    def provision_scope(self, scope: FakeToolProviderScope) -> None:
        if scope.tool not in self.supported_tools:
            raise ValueError("Fake Provider scope tool is not in capabilities")
        self._scopes[scope.binding_digest] = scope

    def set_mode(
        self,
        mode: FakeToolProviderMode | str,
        *,
        delay_seconds: float = 0.0,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("Fake Provider delay cannot be negative")
        self.mode = FakeToolProviderMode(mode)
        self.delay_seconds = delay_seconds

    def count(self, operation: str) -> int:
        return self.call_counts[operation]

    def set_cancel_uncertain(self, value: bool = True) -> None:
        self.cancel_side_effects_may_continue = value

    def reset(self) -> None:
        """Reset observations and execution state while retaining provisioned scopes."""

        self.call_counts.clear()
        self._nonces.clear()
        self._terminal_responses.clear()
        self._active_requests.clear()
        self._cancelled_requests.clear()
        self._cancel_responses.clear()
        self.execution_started.clear()
        self.cancel_received.clear()
        self.mode = FakeToolProviderMode.SUCCESS
        self.delay_seconds = 0.0
        self.cancel_side_effects_may_continue = False

    def _mount_routes(self) -> None:
        self.app.add_api_route("/v1/health", self._health, methods=["GET"])
        self.app.add_api_route("/v1/capabilities", self._capabilities, methods=["GET"])
        self.app.add_api_route("/v1/tool-calls", self._execute, methods=["POST"])
        self.app.add_api_route(
            "/v1/tool-calls/{request_id}/cancel",
            self._cancel,
            methods=["POST"],
        )

    async def _health(self, request: Request) -> Response:
        self.call_counts["health"] += 1
        authentication_error = await self._authenticate(request)
        if authentication_error is not None:
            return authentication_error
        return self._json(
            ProviderHealthResponse(
                provider_id=self.provider_id,
                status=ProviderHealthStatus.HEALTHY,
                time=self._now(),
            )
        )

    async def _capabilities(self, request: Request) -> Response:
        self.call_counts["capabilities"] += 1
        authentication_error = await self._authenticate(request)
        if authentication_error is not None:
            return authentication_error
        return self._json(
            ProviderCapabilitiesResponse(
                provider_id=self.provider_id,
                supported_tools=self.supported_tools,
                features=ProviderFeatureSet(cancellation=True),
            )
        )

    async def _execute(self, request: Request) -> Response:
        self.call_counts["execute"] += 1
        body = await request.body()
        authentication_error = self._authenticate_body(request, body)
        if authentication_error is not None:
            return authentication_error
        if self.mode is FakeToolProviderMode.AUTH_FAILURE:
            return self._error(401, "auth_failure", "Provider rejected authentication")
        try:
            tool_call = ProviderToolCallRequest.model_validate_json(body)
        except ValidationError:
            return self._error(422, "invalid_request", "Tool call request is invalid")
        scope_error = self._authorize_tool_call(request, tool_call)
        if scope_error is not None:
            return scope_error
        if self.mode is FakeToolProviderMode.RATE_LIMIT:
            self.call_counts["rate_limit"] += 1
            return self._error(
                429,
                "rate_limited",
                "Fake Provider rate limit",
                headers={"Retry-After": "1"},
            )
        if self.mode is FakeToolProviderMode.SERVER_ERROR:
            self.call_counts["server_error"] += 1
            return self._error(500, "provider_unavailable", "Fake Provider is unavailable")
        if self.mode is FakeToolProviderMode.MALFORMED_RESPONSE:
            self.call_counts["malformed_response"] += 1
            return Response(b"{not-json", status_code=200, media_type="application/json")
        return await self._execute_once(tool_call)

    async def _execute_once(self, request: ProviderToolCallRequest) -> Response:
        key = (
            request.provider_id,
            request.binding_digest,
            request.run_id,
            request.idempotency_key,
        )
        digest = request.request_digest
        async with self._execution_lock:
            terminal = self._terminal_responses.get(key)
            if terminal is not None:
                stored_digest, response = terminal
                if stored_digest != digest:
                    self.call_counts["idempotency_conflict"] += 1
                    return self._error(
                        409,
                        "idempotency_conflict",
                        "Idempotency key was used for a different request",
                    )
                self.call_counts["idempotency_replay"] += 1
                return self._json(response.model_copy(update={"idempotency_replayed": True}))

            self._active_requests.add(request.request_id)
            self.call_counts["executions"] += 1
            self.execution_started.set()
            started_at = self._now()
            try:
                if self.mode in {
                    FakeToolProviderMode.DELAY,
                    FakeToolProviderMode.TIMEOUT,
                }:
                    await asyncio.sleep(self.delay_seconds)
                response = self._terminal_response(request, started_at=started_at)
                self._terminal_responses[key] = (digest, response)
                return self._json(response)
            finally:
                self._active_requests.discard(request.request_id)

    def _terminal_response(
        self,
        request: ProviderToolCallRequest,
        *,
        started_at: datetime,
    ) -> ProviderToolCallResponse:
        common: dict[str, Any] = {
            "provider_id": self.provider_id,
            "binding_digest": request.binding_digest,
            "request_id": request.request_id,
            "tool_call_id": request.tool_call_id,
            "tool": ProviderToolIdentity(name=request.tool.name, version=request.tool.version),
            "started_at": started_at,
            "finished_at": self._now(),
            "provider_execution_id": self._execution_id(request.request_id),
        }
        if request.request_id in self._cancelled_requests:
            return ProviderToolCallResponse(
                **common,
                status=ProviderToolCallStatus.CANCELLED,
                error=ProviderToolError(
                    code="PROVIDER_CANCELLED",
                    category="cancellation",
                    retryable=False,
                    message="Tool call was cancelled",
                ),
            )
        if self.mode is FakeToolProviderMode.FAILURE:
            return ProviderToolCallResponse(
                **common,
                status=ProviderToolCallStatus.FAILED,
                error=ProviderToolError(
                    code="PROVIDER_COMMAND_FAILED",
                    category="execution",
                    retryable=False,
                    message="Fake Provider command failed",
                ),
            )
        if self.mode is FakeToolProviderMode.TIMEOUT:
            return ProviderToolCallResponse(
                **common,
                status=ProviderToolCallStatus.TIMED_OUT,
                error=ProviderToolError(
                    code="PROVIDER_DEADLINE_EXCEEDED",
                    category="timeout",
                    retryable=True,
                    message="Fake Provider deadline elapsed",
                ),
            )
        if self.mode is FakeToolProviderMode.INVALID_SCHEMA:
            result = {"unexpected": ["invalid-output-schema"]}
        else:
            result = {"echo": request.arguments}
        return ProviderToolCallResponse(
            **common,
            status=ProviderToolCallStatus.SUCCEEDED,
            result=result,
        )

    async def _cancel(self, request_id: str, request: Request) -> Response:
        self.call_counts["cancel"] += 1
        self.cancel_received.set()
        body = await request.body()
        authentication_error = self._authenticate_body(request, body)
        if authentication_error is not None:
            return authentication_error
        if self.mode is FakeToolProviderMode.AUTH_FAILURE:
            return self._error(401, "auth_failure", "Provider rejected authentication")
        try:
            cancellation = ProviderCancelRequest.model_validate_json(body)
        except ValidationError:
            return self._error(422, "invalid_request", "Cancellation request is invalid")
        scope_error = self._authorize_cancellation(request, request_id, cancellation)
        if scope_error is not None:
            return scope_error
        replay = self._cancel_responses.get(request_id)
        if replay is not None:
            self.call_counts["cancel_replay"] += 1
            return self._json(replay)
        finished = any(
            response.request_id == request_id for _, response in self._terminal_responses.values()
        )
        if not finished and not self.cancel_side_effects_may_continue:
            self._cancelled_requests.add(request_id)
        response = ProviderCancelResponse(
            provider_id=self.provider_id,
            request_id=request_id,
            status=(
                ProviderCancelStatus.ALREADY_FINISHED
                if finished
                else ProviderCancelStatus.CANCELLED
            ),
            provider_execution_id=(
                self._execution_id(request_id)
                if finished or request_id in self._active_requests
                else None
            ),
            acknowledged_at=self._now(),
            side_effects_may_continue=self.cancel_side_effects_may_continue,
        )
        self._cancel_responses[request_id] = response
        return self._json(response)

    async def _authenticate(self, request: Request) -> Response | None:
        body = await request.body()
        return self._authenticate_body(request, body)

    def _authenticate_body(self, request: Request, body: bytes) -> Response | None:
        try:
            verification = verify_hmac_request(
                headers=request.headers,
                secret=self.secret,
                method=request.method,
                path_with_query=self._path_with_query(request),
                body=body,
                consume_nonce=self._consume_nonce,
                now=self._now(),
            )
        except (ValueError, ToolProviderError):
            # ToolProviderError intentionally is not echoed; auth failures stay non-sensitive.
            self.call_counts["auth_failure"] += 1
            return self._error(401, "auth_failure", "Tool Provider authentication failed")
        if verification.provider_id != self.provider_id:
            self.call_counts["auth_failure"] += 1
            return self._error(401, "auth_failure", "Tool Provider authentication failed")
        return None

    def _authorize_tool_call(
        self,
        request: Request,
        tool_call: ProviderToolCallRequest,
    ) -> Response | None:
        scope = self._scopes.get(tool_call.binding_digest)
        headers = request.headers
        if (
            scope is None
            or headers.get("x-nico-provider-id") != tool_call.provider_id
            or headers.get("x-nico-binding-digest") != tool_call.binding_digest
            or headers.get("x-nico-request-id") != tool_call.request_id
            or tool_call.provider_id != self.provider_id
            or tool_call.tenant_id != scope.tenant_id
            or tool_call.project_id != scope.project_id
            or tool_call.run_id != scope.run_id
            or tool_call.task_id != scope.task_id
            or tool_call.agent_id != scope.agent_id
            or tool_call.agent_version_id != scope.agent_version_id
            or tool_call.tool != scope.tool
            or tool_call.deadline <= self._now()
        ):
            self.call_counts["scope_failure"] += 1
            return self._error(403, "scope_mismatch", "Tool call is outside provisioned scope")
        return None

    def _authorize_cancellation(
        self,
        request: Request,
        path_request_id: str,
        cancellation: ProviderCancelRequest,
    ) -> Response | None:
        scope = self._scopes.get(cancellation.binding_digest)
        headers = request.headers
        if (
            scope is None
            or path_request_id != cancellation.request_id
            or headers.get("x-nico-provider-id") != cancellation.provider_id
            or headers.get("x-nico-binding-digest") != cancellation.binding_digest
            or headers.get("x-nico-request-id") != cancellation.request_id
            or cancellation.provider_id != self.provider_id
            or cancellation.tenant_id != scope.tenant_id
            or cancellation.project_id != scope.project_id
            or cancellation.run_id != scope.run_id
            or cancellation.deadline <= self._now()
        ):
            self.call_counts["scope_failure"] += 1
            return self._error(403, "scope_mismatch", "Cancellation is outside provisioned scope")
        return None

    def _consume_nonce(self, nonce: str, _timestamp: int) -> bool:
        if nonce in self._nonces:
            return False
        self._nonces.add(nonce)
        return True

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Fake Provider clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _execution_id(request_id: str) -> str:
        digest = hashlib.sha256(request_id.encode()).hexdigest()[:20]
        return f"fake-exec-{digest}"

    @staticmethod
    def _path_with_query(request: Request) -> str:
        query = request.url.query
        return request.url.path if not query else f"{request.url.path}?{query}"

    @staticmethod
    def _json(model: Any) -> JSONResponse:
        return JSONResponse(model.model_dump(mode="json", exclude_none=False))

    @staticmethod
    def _error(
        status: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": code, "message": message}},
            status_code=status,
            headers=headers,
        )
