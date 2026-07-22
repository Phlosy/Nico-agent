"""Tenant-scoped Agent capability API."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from nico_agent.agent_capabilities.contracts import (
    CapabilityActivationCreate,
    CapabilityActivationRead,
    CapabilityCatalogRead,
    CapabilityPreviewCreate,
    CapabilityPreviewRead,
)
from nico_agent.agent_capabilities.service import AgentCapabilityService
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainError
from nico_agent.domain_api import get_tenant_context

router = APIRouter(prefix="/api/v1/agent-capabilities", tags=["agent-capabilities"])


def get_agent_capability_service(request: Request) -> AgentCapabilityService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the capability database is unavailable")
    return AgentCapabilityService(database)


Service = Annotated[AgentCapabilityService, Depends(get_agent_capability_service)]
Context = Annotated[TenantContext, Depends(get_tenant_context)]


@router.get("/catalog", response_model=CapabilityCatalogRead)
async def capability_catalog(
    service: Service,
    context: Context,
    agent_id: Annotated[UUID | None, Query()] = None,
):
    return await service.catalog(context, agent_id=agent_id)


@router.post("/preview", response_model=CapabilityPreviewRead)
async def preview_capabilities(
    command: CapabilityPreviewCreate,
    service: Service,
    context: Context,
):
    return await service.preview(context, command)


@router.post(
    "/activate",
    response_model=CapabilityActivationRead,
    status_code=status.HTTP_201_CREATED,
)
async def activate_capabilities(
    command: CapabilityActivationCreate,
    service: Service,
    context: Context,
):
    return await service.activate(context, command)
