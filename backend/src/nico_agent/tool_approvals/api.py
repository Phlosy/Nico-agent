"""REST resources for durable sensitive ToolCall approval."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request

from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainError
from nico_agent.domain.states import ToolApprovalStatus
from nico_agent.domain_api import get_tenant_context
from nico_agent.tool_approvals.contracts import ToolApprovalDecision, ToolApprovalRead
from nico_agent.tool_approvals.service import ToolApprovalService

router = APIRouter(prefix="/api/v1", tags=["tool-approvals"])


def get_service(request: Request) -> ToolApprovalService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the approval database is unavailable")
    return ToolApprovalService(database)


Service = Annotated[ToolApprovalService, Depends(get_service)]
Context = Annotated[TenantContext, Depends(get_tenant_context)]


@router.get("/tool-approval-requests", response_model=list[ToolApprovalRead])
async def list_tool_approval_requests(
    service: Service,
    context: Context,
    run_id: UUID | None = None,
    status: ToolApprovalStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    return await service.list(context, run_id=run_id, status=status, limit=limit)


@router.get("/tool-approval-requests/{approval_id}", response_model=ToolApprovalRead)
async def get_tool_approval_request(
    approval_id: UUID,
    service: Service,
    context: Context,
):
    return await service.get(context, approval_id)


@router.post(
    "/tool-approval-requests/{approval_id}/decision",
    response_model=ToolApprovalRead,
)
async def decide_tool_approval_request(
    approval_id: UUID,
    command: ToolApprovalDecision,
    service: Service,
    context: Context,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
):
    return await service.decide(
        context,
        approval_id,
        command,
        idempotency_key=idempotency_key,
    )
