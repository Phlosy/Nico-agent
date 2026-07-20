"""Tenant-scoped Provider catalog and durable probe API."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import AccessDenied, DomainError
from nico_agent.domain_api import get_tenant_context
from nico_agent.provider_onboarding.catalog import get_provider_catalog
from nico_agent.provider_onboarding.contracts import (
    ProviderActivationCreate,
    ProviderActivationPreview,
    ProviderActivationRead,
    ProviderCatalog,
    ProviderConnectionRead,
    ProviderPreviewCreate,
    ProviderProbeCreate,
    ProviderProbeRead,
    ProviderSetupReadiness,
)
from nico_agent.provider_onboarding.service import ProviderOnboardingService

router = APIRouter(prefix="/api/v1", tags=["provider-onboarding"])


def get_provider_onboarding_service(request: Request) -> ProviderOnboardingService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the Provider database is unavailable")
    return ProviderOnboardingService(database)


Service = Annotated[ProviderOnboardingService, Depends(get_provider_onboarding_service)]
Context = Annotated[TenantContext, Depends(get_tenant_context)]


@router.get("/provider-catalog", response_model=ProviderCatalog)
async def provider_catalog() -> ProviderCatalog:
    return get_provider_catalog()


@router.post(
    "/provider-probes",
    response_model=ProviderProbeRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_provider_probe(
    command: ProviderProbeCreate,
    service: Service,
    context: Context,
):
    return await service.create_probe(context, command)


@router.get("/provider-probes/{probe_id}", response_model=ProviderProbeRead)
async def get_provider_probe(
    probe_id: UUID,
    service: Service,
    context: Context,
):
    return await service.get_probe(context, probe_id)


@router.post("/provider-probes/{probe_id}/cancel", response_model=ProviderProbeRead)
async def cancel_provider_probe(
    probe_id: UUID,
    service: Service,
    context: Context,
):
    return await service.cancel_probe(context, probe_id)


def _require_provider_writes(request: Request) -> None:
    if not request.app.state.settings.model_endpoint_writes_enabled:
        raise AccessDenied(
            "MODEL_ENDPOINT_WRITES_DISABLED",
            "Provider activation is disabled by deployment policy",
        )


@router.post(
    "/provider-activation/preview",
    response_model=ProviderActivationPreview,
)
async def preview_provider_activation(
    request: Request,
    command: ProviderPreviewCreate,
    service: Service,
    context: Context,
):
    _require_provider_writes(request)
    return await service.preview_activation(context, command)


@router.post(
    "/provider-activation",
    response_model=ProviderActivationRead,
    status_code=status.HTTP_201_CREATED,
)
async def activate_provider(
    request: Request,
    command: ProviderActivationCreate,
    service: Service,
    context: Context,
):
    _require_provider_writes(request)
    return await service.activate(context, command)


@router.get("/provider-connections", response_model=list[ProviderConnectionRead])
async def list_provider_connections(service: Service, context: Context):
    return await service.list_connections(context)


@router.get("/provider-setup-readiness", response_model=ProviderSetupReadiness)
async def provider_setup_readiness(service: Service, context: Context):
    return await service.setup_readiness(context)
