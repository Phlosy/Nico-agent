"""Tenant-scoped guided setup API."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainError
from nico_agent.domain_api import get_tenant_context
from nico_agent.guided_setup.contracts import (
    SetupIntentPatch,
    SetupIntentRead,
    SetupProofCreate,
    SetupProofRead,
    SetupReadinessRead,
)
from nico_agent.guided_setup.service import GuidedSetupService

router = APIRouter(prefix="/api/v1/setup", tags=["guided-setup"])


def get_guided_setup_service(request: Request) -> GuidedSetupService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the setup database is unavailable")
    return GuidedSetupService(database, request.app.state.settings)


Service = Annotated[GuidedSetupService, Depends(get_guided_setup_service)]
Context = Annotated[TenantContext, Depends(get_tenant_context)]


@router.get("/readiness", response_model=SetupReadinessRead)
async def setup_readiness(service: Service, context: Context):
    return await service.readiness(context)


@router.patch("/intent", response_model=SetupIntentRead)
async def update_setup_intent(
    command: SetupIntentPatch,
    service: Service,
    context: Context,
):
    return await service.update_intent(context, command)


@router.post("/proof", response_model=SetupProofRead)
async def validate_setup_proof(
    command: SetupProofCreate,
    service: Service,
    context: Context,
):
    return await service.validate_proof(context, command)
