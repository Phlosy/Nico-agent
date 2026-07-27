"""Tenant-scoped read and command API for dynamic Agent coordination."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from nico_agent.api_schemas import RevisionCommand, RunRead
from nico_agent.coordination.api_schemas import (
    AgentMessageRead,
    DelegationRead,
    RetryRequestCreate,
)
from nico_agent.coordination.service import CoordinationService
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainError
from nico_agent.domain_api import get_tenant_context

router = APIRouter(prefix="/api/v1", tags=["coordination"])


def get_service(request: Request) -> CoordinationService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the coordination database is unavailable")
    return CoordinationService(database, settings=request.app.state.settings)


Service = Annotated[CoordinationService, Depends(get_service)]
Context = Annotated[TenantContext, Depends(get_tenant_context)]


@router.get("/runs/{run_id}/delegations", response_model=list[DelegationRead])
async def list_delegations(run_id: UUID, service: Service, context: Context):
    return await service.list_delegations(context, run_id)


@router.get("/runs/{run_id}/children", response_model=list[RunRead])
async def list_children(run_id: UUID, service: Service, context: Context):
    return await service.list_children(context, run_id)


@router.get("/runs/{run_id}/messages", response_model=list[AgentMessageRead])
async def list_messages(run_id: UUID, service: Service, context: Context):
    return await service.list_messages(context, run_id)


@router.post("/runs/{run_id}/tree-cancel", response_model=RunRead)
async def cancel_tree(
    run_id: UUID,
    command: RevisionCommand,
    service: Service,
    context: Context,
):
    return await service.cancel_tree(
        context,
        run_id,
        expected_revision=command.expected_revision,
    )


@router.post(
    "/delegations/{delegation_id}/retry-request",
    response_model=AgentMessageRead,
    status_code=201,
)
async def request_retry(
    delegation_id: UUID,
    command: RetryRequestCreate,
    service: Service,
    context: Context,
):
    return await service.request_retry(
        context,
        delegation_id,
        reason=command.reason,
        idempotency_key=command.idempotency_key,
    )
