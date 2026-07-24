"""Tenant-scoped REST resources for durable Agent questions."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request

from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainConflict, DomainError
from nico_agent.domain.states import UserInputRequestStatus
from nico_agent.domain_api import get_tenant_context
from nico_agent.user_inputs.contracts import UserInputAnswer, UserInputRequestRead
from nico_agent.user_inputs.service import UserInputService

router = APIRouter(prefix="/api/v1", tags=["user-input"])


def get_service(request: Request) -> UserInputService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the user input database is unavailable")
    return UserInputService(database)


Service = Annotated[UserInputService, Depends(get_service)]
Context = Annotated[TenantContext, Depends(get_tenant_context)]


@router.get("/user-input-requests", response_model=list[UserInputRequestRead])
async def list_user_input_requests(
    service: Service,
    context: Context,
    run_id: UUID | None = None,
    status: UserInputRequestStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    return await service.list(context, run_id=run_id, status=status, limit=limit)


@router.get("/user-input-requests/{request_id}", response_model=UserInputRequestRead)
async def get_user_input_request(
    request_id: UUID,
    service: Service,
    context: Context,
):
    return await service.get(context, request_id)


@router.post(
    "/user-input-requests/{request_id}/answer",
    response_model=UserInputRequestRead,
)
async def answer_user_input_request(
    request_id: UUID,
    command: UserInputAnswer,
    service: Service,
    context: Context,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
):
    try:
        result = await service.answer(
            context,
            request_id,
            command,
            idempotency_key=idempotency_key,
        )
    except ValueError as exc:
        raise DomainError(
            "USER_INPUT_ANSWER_INVALID",
            "user input answer does not match the request schema",
        ) from exc
    if result.status != UserInputRequestStatus.ANSWERED.value:
        raise DomainConflict(
            f"USER_INPUT_{result.status.upper()}",
            f"user input request is {result.status}",
            details={"status": result.status, "revision": result.revision},
        )
    return result
