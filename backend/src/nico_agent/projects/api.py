"""HTTP routes for managed Project collaboration."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status

from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainError
from nico_agent.domain_api import get_tenant_context
from nico_agent.projects.contracts import (
    ProjectCollaborationCreate,
    ProjectCollaborationRead,
    ProjectLeadPreflightRead,
    ProjectLeadReplace,
    ProjectLeadReplaceRead,
    ProjectMemberAdd,
    ProjectMemberMutationRead,
    ProjectMemberRead,
    ProjectMemberStateCommand,
    ProjectPreflightRequest,
    ProjectSessionRead,
)
from nico_agent.projects.service import ProjectCollaborationService

router = APIRouter(prefix="/api/v1/projects", tags=["project-collaboration"])


def get_service(request: Request) -> ProjectCollaborationService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the Project database is unavailable")
    return ProjectCollaborationService(database)


Service = Annotated[ProjectCollaborationService, Depends(get_service)]
Context = Annotated[TenantContext, Depends(get_tenant_context)]
IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=200),
]


@router.post("/collaboration/preflight", response_model=ProjectLeadPreflightRead)
async def preflight_project(
    command: ProjectPreflightRequest,
    service: Service,
    context: Context,
):
    return await service.preflight(context, command)


@router.post(
    "/collaboration",
    response_model=ProjectCollaborationRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_project(
    command: ProjectCollaborationCreate,
    service: Service,
    context: Context,
    idempotency_key: IdempotencyKey,
):
    return await service.create(context, command, idempotency_key=idempotency_key)


@router.get("/{project_id}/members", response_model=list[ProjectMemberRead])
async def list_members(project_id: UUID, service: Service, context: Context):
    return await service.list_members(context, project_id)


@router.post(
    "/{project_id}/members",
    response_model=ProjectMemberMutationRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    project_id: UUID,
    command: ProjectMemberAdd,
    service: Service,
    context: Context,
    idempotency_key: IdempotencyKey,
):
    return await service.add_member(
        context,
        project_id,
        command,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/{project_id}/members/{agent_id}/state",
    response_model=ProjectMemberMutationRead,
)
async def set_member_state(
    project_id: UUID,
    agent_id: UUID,
    command: ProjectMemberStateCommand,
    service: Service,
    context: Context,
    idempotency_key: IdempotencyKey,
):
    return await service.set_member_state(
        context,
        project_id,
        agent_id,
        command,
        idempotency_key=idempotency_key,
    )


@router.post("/{project_id}/lead", response_model=ProjectLeadReplaceRead)
async def replace_lead(
    project_id: UUID,
    command: ProjectLeadReplace,
    service: Service,
    context: Context,
    idempotency_key: IdempotencyKey,
):
    return await service.replace_lead(
        context,
        project_id,
        command,
        idempotency_key=idempotency_key,
    )


@router.get("/{project_id}/sessions", response_model=list[ProjectSessionRead])
async def list_sessions(project_id: UUID, service: Service, context: Context):
    return await service.list_sessions(context, project_id)


@router.get("/{project_id}/sessions/{session_id}", response_model=ProjectSessionRead)
async def get_session(
    project_id: UUID,
    session_id: UUID,
    service: Service,
    context: Context,
):
    return await service.get_session(context, project_id, session_id)
