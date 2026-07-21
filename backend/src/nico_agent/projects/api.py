"""HTTP routes for managed Project collaboration."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status

from nico_agent.api_schemas import ProjectRead
from nico_agent.conversations.contracts import (
    ConversationRead,
    ConversationTurnAccepted,
    ConversationTurnCreate,
)
from nico_agent.conversations.service import ConversationService
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainError
from nico_agent.domain_api import get_tenant_context
from nico_agent.projects.contracts import (
    ProjectChangeCreate,
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
    ProjectSupervisionCadenceCommand,
    ProjectSupervisionCycleRead,
    ProjectTimelinePage,
    RunInterventionCreate,
    RunInterventionRead,
    RunInterventionWithdraw,
)
from nico_agent.projects.interventions import ProjectInterventionService
from nico_agent.projects.orchestration import ProjectOrchestrationService
from nico_agent.projects.read_service import ProjectSessionReadService
from nico_agent.projects.service import ProjectCollaborationService

router = APIRouter(prefix="/api/v1/projects", tags=["project-collaboration"])


def get_service(request: Request) -> ProjectCollaborationService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the Project database is unavailable")
    return ProjectCollaborationService(database)


def get_read_service(request: Request) -> ProjectSessionReadService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the Project database is unavailable")
    return ProjectSessionReadService(database)


def get_orchestration_service(request: Request) -> ProjectOrchestrationService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the Project database is unavailable")
    return ProjectOrchestrationService(database)


def get_intervention_service(request: Request) -> ProjectInterventionService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the Project database is unavailable")
    return ProjectInterventionService(database)


Service = Annotated[ProjectCollaborationService, Depends(get_service)]
ReadService = Annotated[ProjectSessionReadService, Depends(get_read_service)]
OrchestrationService = Annotated[ProjectOrchestrationService, Depends(get_orchestration_service)]
InterventionService = Annotated[ProjectInterventionService, Depends(get_intervention_service)]
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


@router.post(
    "/{project_id}/sessions/{session_id}/open",
    response_model=ConversationRead,
)
async def open_session_conversation(
    project_id: UUID,
    session_id: UUID,
    service: Service,
    context: Context,
):
    return await service.resolve_session_conversation(context, project_id, session_id)


@router.post(
    "/{project_id}/sessions/{session_id}/messages",
    response_model=ConversationTurnAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_session_message(
    project_id: UUID,
    session_id: UUID,
    command: ConversationTurnCreate,
    service: Service,
    context: Context,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ] = None,
):
    if idempotency_key is not None:
        command = command.model_copy(update={"idempotency_key": idempotency_key})
    conversation = await service.resolve_session_conversation(
        context,
        project_id,
        session_id,
    )
    return await ConversationService(service.database).create_turn(
        context,
        conversation.id,
        command,
    )


@router.get(
    "/{project_id}/sessions/{session_id}/timeline",
    response_model=ProjectTimelinePage,
)
async def get_session_timeline(
    project_id: UUID,
    session_id: UUID,
    service: ReadService,
    context: Context,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    return await service.timeline(
        context,
        project_id,
        session_id,
        after_sequence=after_sequence,
        limit=limit,
    )


@router.post(
    "/{project_id}/supervision/sync",
    response_model=ProjectSupervisionCycleRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_project_sync(
    project_id: UUID,
    service: OrchestrationService,
    context: Context,
    idempotency_key: IdempotencyKey,
):
    return await service.create_manual(context, project_id, idempotency_key=idempotency_key)


@router.patch("/{project_id}/supervision/cadence", response_model=ProjectRead)
async def update_project_cadence(
    project_id: UUID,
    command: ProjectSupervisionCadenceCommand,
    service: OrchestrationService,
    context: Context,
):
    return await service.update_cadence(
        context,
        project_id,
        cadence_seconds=command.cadence_seconds,
        expected_revision=command.expected_project_revision,
        reason=command.reason,
    )


@router.get(
    "/{project_id}/supervision/cycles",
    response_model=list[ProjectSupervisionCycleRead],
)
async def list_project_supervision_cycles(
    project_id: UUID,
    service: OrchestrationService,
    context: Context,
):
    return await service.list_cycles(context, project_id)


@router.get(
    "/{project_id}/supervision/cycles/{cycle_id}",
    response_model=ProjectSupervisionCycleRead,
)
async def get_project_supervision_cycle(
    project_id: UUID,
    cycle_id: UUID,
    service: OrchestrationService,
    context: Context,
):
    return await service.get_cycle(context, project_id, cycle_id)


@router.post(
    "/{project_id}/sessions/{session_id}/runs/{run_id}/interventions",
    response_model=RunInterventionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_run_intervention(
    project_id: UUID,
    session_id: UUID,
    run_id: UUID,
    command: RunInterventionCreate,
    service: InterventionService,
    context: Context,
    idempotency_key: IdempotencyKey,
):
    return await service.create_local_guidance(
        context,
        project_id,
        session_id,
        run_id,
        command,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/{project_id}/sessions/{session_id}/runs/{run_id}/interventions",
    response_model=list[RunInterventionRead],
)
async def list_run_interventions(
    project_id: UUID,
    session_id: UUID,
    run_id: UUID,
    service: InterventionService,
    context: Context,
):
    return await service.list_interventions(context, project_id, session_id, run_id)


@router.post(
    "/{project_id}/sessions/{session_id}/runs/{run_id}/interventions/{intervention_id}/withdraw",
    response_model=RunInterventionRead,
)
async def withdraw_run_intervention(
    project_id: UUID,
    session_id: UUID,
    run_id: UUID,
    intervention_id: UUID,
    command: RunInterventionWithdraw,
    service: InterventionService,
    context: Context,
):
    return await service.withdraw(
        context,
        project_id,
        session_id,
        run_id,
        intervention_id,
        command,
    )


@router.post(
    "/{project_id}/sessions/{session_id}/project-changes",
    response_model=ConversationTurnAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def escalate_project_change(
    project_id: UUID,
    session_id: UUID,
    command: ProjectChangeCreate,
    service: InterventionService,
    context: Context,
    idempotency_key: IdempotencyKey,
):
    return await service.escalate_project_change(
        context,
        project_id,
        session_id,
        command,
        idempotency_key=idempotency_key,
    )
