"""Database-authoritative eligibility checks for Web fetch targets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select

from nico_agent.database import Database, TenantContext
from nico_agent.domain.models import ToolCall
from nico_agent.domain.states import ToolCallStatus
from nico_agent.net.safe_http import (
    CanonicalHttpUrl,
    SafeHttpError,
    canonicalize_http_url,
    domain_allowed,
)
from nico_agent.tools.contracts import ToolExecutionContext


class WebSourceDenied(Exception):
    def __init__(self, message: str = "Web fetch source is not authorized") -> None:
        super().__init__(message)
        self.code = "WEB_FETCH_SOURCE_DENIED"
        self.message = message


@dataclass(frozen=True, slots=True)
class AuthorizedWebSource:
    target: CanonicalHttpUrl
    basis: str
    search_tool_call_id: UUID | None = None


class WebSourceAuthorizer:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def authorize(
        self,
        context: ToolExecutionContext,
        url: str,
        *,
        search_tool_call_id: str | UUID | None,
        allowed_domains: tuple[str, ...],
    ) -> AuthorizedWebSource:
        target = _canonical_target(url)
        if search_tool_call_id is None:
            if not domain_allowed(target.hostname, allowed_domains):
                raise WebSourceDenied()
            return AuthorizedWebSource(target=target, basis="allowlist")

        try:
            call_id = UUID(str(search_tool_call_id))
        except (TypeError, ValueError) as exc:
            raise WebSourceDenied() from exc
        tenant_context = TenantContext(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            correlation_id=context.correlation_id,
        )
        async with self.database.tenant_transaction(tenant_context) as session:
            call = await session.scalar(
                select(ToolCall).where(
                    ToolCall.tenant_id == context.tenant_id,
                    ToolCall.id == call_id,
                )
            )
        if not _eligible_search_call(call, context):
            raise WebSourceDenied()
        if target.url not in _result_urls(call.result):
            raise WebSourceDenied()
        return AuthorizedWebSource(
            target=target,
            basis="search",
            search_tool_call_id=call_id,
        )


def configured_allowed_domains(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 100:
        raise WebSourceDenied("Web fetch domain policy is invalid")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or "://" in item:
            raise WebSourceDenied("Web fetch domain policy is invalid")
        wildcard = item.startswith("*.")
        hostname = (item[2:] if wildcard else item).rstrip(".").lower()
        if not hostname or any(character in hostname for character in "*/:@?#[]"):
            raise WebSourceDenied("Web fetch domain policy is invalid")
        try:
            canonical = canonicalize_http_url(f"https://{hostname}/").hostname
        except SafeHttpError as exc:
            raise WebSourceDenied("Web fetch domain policy is invalid") from exc
        normalized = f"*.{canonical}" if wildcard else canonical
        if normalized not in result:
            result.append(normalized)
    return tuple(result)


def _canonical_target(url: str) -> CanonicalHttpUrl:
    try:
        return canonicalize_http_url(url)
    except SafeHttpError as exc:
        raise WebSourceDenied() from exc


def _eligible_search_call(
    call: ToolCall | None,
    context: ToolExecutionContext,
) -> bool:
    return bool(
        call is not None
        and call.tenant_id == context.tenant_id
        and call.run_id == context.run_id
        and call.tool_name == "web.search"
        and call.tool_version == "1.0.0"
        and call.status == ToolCallStatus.SUCCEEDED.value
        and isinstance(call.result, dict)
    )


def _result_urls(result: dict[str, Any] | None) -> frozenset[str]:
    if not isinstance(result, dict):
        return frozenset()
    values: set[str] = set()
    raw_results = result.get("results")
    if not isinstance(raw_results, list):
        return frozenset()
    for raw in raw_results[:10]:
        if not isinstance(raw, dict) or not isinstance(raw.get("url"), str):
            continue
        try:
            values.add(canonicalize_http_url(raw["url"]).url)
        except SafeHttpError:
            continue
    return frozenset(values)
