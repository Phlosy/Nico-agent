"""Durable, tenant-isolated execution of Provider onboarding probes."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update

from nico_agent.database import Database, ProviderProbeClaim, TenantContext
from nico_agent.domain.errors import DomainError
from nico_agent.domain.models import AuditRecord, ProviderProbe
from nico_agent.models import ModelDiscoveryRequest, ModelGateway, ModelMessage, ModelRequest
from nico_agent.models.errors import ModelProviderError
from nico_agent.models.http_safety import clean_external_text
from nico_agent.tools.secrets import EnvironmentSecretResolver, SecretResolver
from nico_agent.web import SearchProviderError, SearchRequest, WebProviderRegistry


class ProviderProbeWorker:
    def __init__(
        self,
        database: Database,
        model_gateway: ModelGateway,
        *,
        worker_id: str,
        lease_seconds: int = 90,
        execution_timeout_seconds: float | None = None,
        web_providers: WebProviderRegistry | None = None,
        tool_secret_resolver: SecretResolver | None = None,
    ) -> None:
        if not worker_id or len(worker_id) > 200:
            raise ValueError("worker_id must contain between 1 and 200 characters")
        if lease_seconds < 30 or lease_seconds > 3600:
            raise ValueError("lease_seconds must be between 30 and 3600")
        timeout = (
            min(60.0, lease_seconds * 2 / 3)
            if execution_timeout_seconds is None
            else execution_timeout_seconds
        )
        if timeout <= 0 or timeout >= lease_seconds:
            raise ValueError("execution timeout must be positive and shorter than the lease")
        self.database = database
        self.model_gateway = model_gateway
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.execution_timeout_seconds = timeout
        self.web_providers = web_providers
        self.tool_secret_resolver = tool_secret_resolver or EnvironmentSecretResolver()

    async def execute_once(self) -> bool:
        claim = await self.database.claim_next_provider_probe(
            self.worker_id,
            self.lease_seconds,
        )
        if claim is None:
            return False
        started = time.perf_counter()
        context = TenantContext(
            claim.tenant_id,
            f"provider-probe-worker:{self.worker_id}",
            uuid4(),
        )
        snapshot = await self._load_snapshot(context, claim)
        if snapshot is None:
            return True
        try:
            result, verified = await self._execute_bounded(snapshot)
        except Exception as exc:
            await self._finish(
                context,
                claim,
                status="failed",
                result={"latency_ms": _elapsed_ms(started)},
                error_code=_provider_error_code(exc),
                error_detail=_safe_error_detail(exc),
                verified=False,
            )
        else:
            result["latency_ms"] = _elapsed_ms(started)
            await self._finish(
                context,
                claim,
                status="succeeded",
                result=result,
                error_code=None,
                error_detail=None,
                verified=verified,
            )
        return True

    async def _execute_bounded(self, snapshot: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        try:
            async with asyncio.timeout(self.execution_timeout_seconds):
                return await self._execute(snapshot)
        except TimeoutError as exc:
            raise ModelProviderError(
                "MODEL_PROVIDER_TIMEOUT",
                "Provider probe exceeded its execution deadline.",
            ) from exc

    async def _load_snapshot(
        self,
        context: TenantContext,
        claim: ProviderProbeClaim,
    ) -> dict[str, Any] | None:
        async with self.database.tenant_transaction(context) as session:
            probe = await session.scalar(
                select(ProviderProbe).where(
                    ProviderProbe.tenant_id == claim.tenant_id,
                    ProviderProbe.id == claim.probe_id,
                    ProviderProbe.status == "running",
                    ProviderProbe.lease_token == claim.lease_token,
                )
            )
            if probe is None:
                return None
            return {
                "kind": probe.kind,
                "provider_key": probe.provider_key,
                "model": probe.model_name,
                "endpoint": {
                    "id": str(probe.id),
                    "protocol": probe.protocol,
                    "base_url": probe.base_url,
                    "credential_ref": probe.credential_ref,
                    "provider_options": dict(probe.provider_options),
                    "capabilities": {"streaming": True},
                    "allowed_models": [probe.model_name] if probe.model_name else [],
                    "tls_policy": {},
                },
            }

    async def _execute(self, snapshot: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        endpoint = snapshot["endpoint"]
        if snapshot["kind"] == "verify_web":
            return await self._execute_web(snapshot)
        if snapshot["kind"] == "discover_models":
            discovered = await self.model_gateway.discover(
                ModelDiscoveryRequest(endpoint=endpoint, limit=1000)
            )
            return {
                "models": [model.model_dump(mode="json") for model in discovered.models],
                "truncated": discovered.truncated,
                "protocol": endpoint["protocol"],
            }, False

        model = snapshot["model"]
        if not isinstance(model, str) or not model:
            raise ValueError("verification probe is missing a model ID")
        response = await self.model_gateway.complete(
            ModelRequest(
                model=model,
                messages=(
                    ModelMessage(
                        role="user",
                        content="Reply with a short acknowledgement for a connectivity check.",
                    ),
                ),
                endpoint=endpoint,
                max_output_tokens=16,
                timeout_seconds=45,
            )
        )
        reasoning_was_truncated = (
            response.finish_reason == "length"
            and response.usage.output_tokens is not None
            and response.usage.output_tokens > 0
        )
        if not response.text.strip() and not reasoning_was_truncated:
            raise ValueError("provider returned an empty completion")
        request_id = (
            clean_external_text(response.provider_request_id, 200)
            if response.provider_request_id
            else None
        )
        return {
            "response_present": True,
            "finish_reason": clean_external_text(response.finish_reason or "", 100) or None,
            "usage": response.usage.model_dump(mode="json"),
            "provider_request_id": request_id,
            "protocol": endpoint["protocol"],
        }, True

    async def _execute_web(self, snapshot: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        if self.web_providers is None:
            raise SearchProviderError(
                "WEB_SEARCH_NOT_CONFIGURED",
                "Web Provider registry is unavailable",
            )
        provider_key = str(snapshot["provider_key"])
        provider = self.web_providers.get(provider_key)
        options = snapshot["endpoint"].get("provider_options")
        if not isinstance(options, dict):
            raise ValueError("Web Provider probe options are invalid")
        policy = options.get("policy")
        if not isinstance(policy, dict):
            raise ValueError("Web Provider probe policy is invalid")
        secret = None
        if provider_key == "brave":
            secret = self.tool_secret_resolver.resolve(
                "web_search_brave_api_key",
                str(snapshot["endpoint"].get("credential_ref") or ""),
            )
        page = await provider.search(
            SearchRequest(query="Nico Web Provider connectivity check", count=1),
            secret=secret,
            config=policy,
        )
        return {
            "response_present": True,
            "provider": page.provider,
            "result_count": len(page.results),
        }, True

    async def _finish(
        self,
        context: TenantContext,
        claim: ProviderProbeClaim,
        *,
        status: str,
        result: dict[str, Any],
        error_code: str | None,
        error_detail: str | None,
        verified: bool,
    ) -> None:
        completed_at = datetime.now(UTC)
        async with self.database.tenant_transaction(context) as session:
            statement = (
                update(ProviderProbe)
                .where(
                    ProviderProbe.tenant_id == claim.tenant_id,
                    ProviderProbe.id == claim.probe_id,
                    ProviderProbe.status == "running",
                    ProviderProbe.lease_token == claim.lease_token,
                )
                .values(
                    status=status,
                    result=result,
                    error_code=error_code,
                    error_detail=error_detail,
                    completed_at=completed_at,
                    verified_at=completed_at if verified else None,
                    lease_token=None,
                    lease_expires_at=None,
                    revision=ProviderProbe.revision + 1,
                    updated_at=completed_at,
                )
                .returning(ProviderProbe.id, ProviderProbe.candidate_hash)
            )
            row = (await session.execute(statement)).one_or_none()
            if row is None:
                return
            session.add(
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action=f"provider_probe.{status}",
                    resource_type="provider_probe",
                    resource_id=row.id,
                    actor_id=context.actor_id,
                    details={
                        "candidate_hash": row.candidate_hash,
                        "error_code": error_code,
                    },
                    correlation_id=context.correlation_id,
                )
            )


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _provider_error_code(exc: Exception) -> str:
    if isinstance(exc, SearchProviderError):
        return exc.code
    code = exc.code if isinstance(exc, DomainError) else ""
    mapping = {
        "MODEL_AUTH_FAILED": "PROVIDER_AUTH_FAILED",
        "MODEL_CREDENTIAL_UNAVAILABLE": "PROVIDER_AUTH_FAILED",
        "MODEL_MODEL_UNAVAILABLE": "PROVIDER_MODEL_UNAVAILABLE",
        "MODEL_PROVIDER_RATE_LIMITED": "PROVIDER_RATE_LIMITED",
        "MODEL_RATE_LIMITED": "PROVIDER_RATE_LIMITED",
        "MODEL_PROVIDER_TIMEOUT": "PROVIDER_TIMEOUT",
        "MODEL_PROVIDER_NETWORK_ERROR": "PROVIDER_NETWORK_ERROR",
        "MODEL_PROVIDER_UNAVAILABLE": "PROVIDER_UPSTREAM_ERROR",
        "MODEL_ENDPOINT_DENIED": "PROVIDER_ENDPOINT_DENIED",
        "MODEL_PROTOCOL_ERROR": "PROVIDER_PROTOCOL_ERROR",
        "MODEL_DISCOVERY_UNAVAILABLE": "PROVIDER_DISCOVERY_UNAVAILABLE",
        "TOOL_SECRET_UNAVAILABLE": "WEB_SEARCH_SECRET_UNAVAILABLE",
    }
    return mapping.get(code, "PROVIDER_PROTOCOL_ERROR")


def _safe_error_detail(exc: Exception) -> str:
    if isinstance(exc, SearchProviderError):
        return clean_external_text(exc.message, 500) or "Web Provider probe failed"
    if isinstance(exc, DomainError):
        details = {
            "PROVIDER_AUTH_FAILED": "Provider authentication failed.",
            "PROVIDER_MODEL_UNAVAILABLE": "The selected model is unavailable.",
            "PROVIDER_RATE_LIMITED": "The Provider rate limit rejected the probe.",
            "PROVIDER_TIMEOUT": "The Provider probe timed out.",
            "PROVIDER_NETWORK_ERROR": "The Provider network request failed.",
            "PROVIDER_UPSTREAM_ERROR": "The Provider is temporarily unavailable.",
            "PROVIDER_ENDPOINT_DENIED": "The Provider endpoint was denied by policy.",
            "PROVIDER_PROTOCOL_ERROR": "The Provider returned an invalid response.",
            "PROVIDER_DISCOVERY_UNAVAILABLE": "Model discovery is unavailable.",
            "WEB_SEARCH_SECRET_UNAVAILABLE": "Web Search credential is unavailable.",
        }
        return details.get(_provider_error_code(exc), "Provider probe failed.")
    if isinstance(exc, ValueError):
        return clean_external_text(str(exc), 500) or "provider probe failed"
    return "provider probe failed"
