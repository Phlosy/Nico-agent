"""Public registry API for run-scoped external Tool Providers."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from nico_agent.api_schemas import ToolDefinitionRead
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainError
from nico_agent.domain_api import get_tenant_context
from nico_agent.tool_providers.contracts import (
    ExternalToolProviderCreate,
    ExternalToolProviderRead,
)
from nico_agent.tool_providers.service import ToolProviderService
from nico_agent.tools.contracts import ToolDefinitionSpec

router = APIRouter(prefix="/api/v1", tags=["external-tool-providers"])


def get_tool_provider_service(request: Request) -> ToolProviderService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the Tool Provider database is unavailable")
    return ToolProviderService(
        database,
        request.app.state.settings,
        approval_required_risks=frozenset(request.app.state.settings.tool_approval_required_risks),
    )


Service = Annotated[ToolProviderService, Depends(get_tool_provider_service)]
Context = Annotated[TenantContext, Depends(get_tenant_context)]


@router.post(
    "/external-tool-providers",
    response_model=ExternalToolProviderRead,
    status_code=status.HTTP_201_CREATED,
)
async def register_external_tool_provider(
    command: ExternalToolProviderCreate,
    service: Service,
    context: Context,
):
    return await service.register(context, command)


@router.get(
    "/external-tool-providers/{provider_id}",
    response_model=ExternalToolProviderRead,
)
async def get_external_tool_provider(
    provider_id: UUID,
    service: Service,
    context: Context,
):
    return await service.get(context, provider_id)


@router.post(
    "/external-tool-providers/{provider_id}/verify",
    response_model=ExternalToolProviderRead,
)
async def verify_external_tool_provider(
    provider_id: UUID,
    service: Service,
    context: Context,
):
    return await service.verify(context, provider_id)


@router.post(
    "/external-tool-providers/{provider_id}/disable",
    response_model=ExternalToolProviderRead,
)
async def disable_external_tool_provider(
    provider_id: UUID,
    service: Service,
    context: Context,
):
    return await service.disable(context, provider_id)


@router.post(
    "/external-tool-providers/{provider_id}/revoke",
    response_model=ExternalToolProviderRead,
)
async def revoke_external_tool_provider(
    provider_id: UUID,
    service: Service,
    context: Context,
):
    return await service.revoke(context, provider_id)


@router.post(
    "/tool-definitions",
    response_model=ToolDefinitionRead,
    status_code=status.HTTP_201_CREATED,
)
async def register_external_tool_definition(
    command: ToolDefinitionSpec,
    service: Service,
    context: Context,
):
    return await service.register_tool_definition(context, command)
