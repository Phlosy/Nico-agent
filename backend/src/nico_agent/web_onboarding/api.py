"""Tenant-scoped Web Provider onboarding API."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import AccessDenied, DomainError
from nico_agent.domain_api import get_tenant_context
from nico_agent.web_onboarding.catalog import get_web_provider_catalog
from nico_agent.web_onboarding.contracts import (
    WebActivationCreate,
    WebActivationPreview,
    WebActivationRead,
    WebConfigurationTestCreate,
    WebDisableCreate,
    WebDisablePreview,
    WebDisablePreviewCreate,
    WebDisableRead,
    WebPreviewCreate,
    WebProbeCreate,
    WebProbeRead,
    WebProviderCatalog,
    WebProviderStatus,
    WebSetupReadiness,
)
from nico_agent.web_onboarding.service import WebOnboardingService

router = APIRouter(prefix="/api/v1/web", tags=["web-onboarding"])


def get_web_onboarding_service(request: Request) -> WebOnboardingService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the Web Provider database is unavailable")
    return WebOnboardingService(database, request.app.state.settings)


Service = Annotated[WebOnboardingService, Depends(get_web_onboarding_service)]
Context = Annotated[TenantContext, Depends(get_tenant_context)]


def _require_web_writes(request: Request) -> None:
    if not request.app.state.settings.web_provider_writes_enabled:
        raise AccessDenied(
            "WEB_PROVIDER_WRITES_DISABLED",
            "Web Provider activation is disabled by deployment policy",
        )


@router.get("/catalog", response_model=WebProviderCatalog)
async def web_provider_catalog(request: Request) -> WebProviderCatalog:
    return get_web_provider_catalog(request.app.state.settings)


@router.get("/setup-readiness", response_model=WebSetupReadiness)
async def web_setup_readiness(service: Service, context: Context):
    return await service.setup_readiness(context)


@router.get("/status", response_model=WebProviderStatus)
async def web_provider_status(service: Service, context: Context):
    return await service.status(context)


@router.post(
    "/test",
    response_model=WebProbeRead,
    status_code=status.HTTP_201_CREATED,
)
async def test_web_provider(
    request: Request,
    command: WebConfigurationTestCreate,
    service: Service,
    context: Context,
):
    _require_web_writes(request)
    return await service.test_configuration(context, command)


@router.post(
    "/provider-probes",
    response_model=WebProbeRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_web_provider_probe(
    request: Request,
    command: WebProbeCreate,
    service: Service,
    context: Context,
):
    _require_web_writes(request)
    return await service.create_probe(context, command)


@router.get("/provider-probes/{probe_id}", response_model=WebProbeRead)
async def get_web_provider_probe(
    probe_id: UUID,
    service: Service,
    context: Context,
):
    return await service.get_probe(context, probe_id)


@router.post("/activation/preview", response_model=WebActivationPreview)
async def preview_web_activation(
    request: Request,
    command: WebPreviewCreate,
    service: Service,
    context: Context,
):
    _require_web_writes(request)
    return await service.preview_activation(context, command)


@router.post(
    "/activation",
    response_model=WebActivationRead,
    status_code=status.HTTP_201_CREATED,
)
async def activate_web_provider(
    request: Request,
    command: WebActivationCreate,
    service: Service,
    context: Context,
):
    _require_web_writes(request)
    return await service.activate(context, command)


@router.post("/disable/preview", response_model=WebDisablePreview)
async def preview_web_disable(
    request: Request,
    command: WebDisablePreviewCreate,
    service: Service,
    context: Context,
):
    _require_web_writes(request)
    return await service.preview_disable(context, command)


@router.post("/disable", response_model=WebDisableRead)
async def disable_web_provider(
    request: Request,
    command: WebDisableCreate,
    service: Service,
    context: Context,
):
    _require_web_writes(request)
    return await service.disable(context, command)
