"""FastAPI routes for the core control plane."""

from __future__ import annotations

from typing import Annotated
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from nico_agent.api_schemas import (
    AgentClone,
    AgentCreate,
    AgentPatch,
    AgentRead,
    AgentRollback,
    AgentVersionCreate,
    AgentVersionRead,
    AuditRead,
    EventRead,
    ProjectCreate,
    ProjectPatch,
    ProjectRead,
    RevisionCommand,
    RunCreate,
    RunRead,
    RunStepCreate,
    RunStepRead,
    RunStepTransition,
    RuntimeSessionRead,
    RunTransition,
    TaskCreate,
    TaskRead,
    TaskTransition,
    TenantCreate,
    TenantRead,
    TenantSettingsPatch,
    ToolCallRead,
    ToolDefinitionRead,
)
from nico_agent.config import Settings
from nico_agent.control_plane import ControlPlaneService
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import AccessDenied, DomainError
from nico_agent.runtime.contracts import RuntimeTrajectory

router = APIRouter(prefix="/api/v1", tags=["control-plane"])


def _correlation_id(request: Request) -> UUID:
    value = getattr(request.state, "request_id", "request")
    try:
        return UUID(value)
    except ValueError:
        return uuid5(NAMESPACE_URL, value)


def _allow_development_context(settings: Settings) -> None:
    if settings.environment == "production":
        raise AccessDenied(
            "DEVELOPMENT_TENANT_CONTEXT_DISABLED",
            "X-Tenant-ID is disabled in production; configure authenticated tenant claims",
        )


def get_service(request: Request) -> ControlPlaneService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the control-plane database is unavailable")
    return ControlPlaneService(database)


def get_tenant_context(
    request: Request,
    tenant_id: Annotated[UUID, Header(alias="X-Tenant-ID")],
    actor_id: Annotated[str, Header(alias="X-Actor-ID", min_length=1, max_length=200)] = (
        "development-user"
    ),
) -> TenantContext:
    _allow_development_context(request.app.state.settings)
    return TenantContext(
        tenant_id=tenant_id,
        actor_id=actor_id,
        correlation_id=_correlation_id(request),
    )


Service = Annotated[ControlPlaneService, Depends(get_service)]
Context = Annotated[TenantContext, Depends(get_tenant_context)]


@router.post(
    "/tenants/bootstrap",
    response_model=TenantRead,
    status_code=status.HTTP_201_CREATED,
    summary="Bootstrap a tenant in development or test only",
)
async def bootstrap_tenant(
    request: Request,
    command: TenantCreate,
    service: Service,
    actor_id: Annotated[str, Header(alias="X-Actor-ID", min_length=1, max_length=200)] = (
        "development-bootstrap"
    ),
):
    _allow_development_context(request.app.state.settings)
    return await service.bootstrap_tenant(
        command,
        actor_id=actor_id,
        correlation_id=_correlation_id(request),
    )


@router.patch("/tenant/settings", response_model=TenantRead)
async def update_tenant_settings(
    command: TenantSettingsPatch,
    service: Service,
    context: Context,
):
    return await service.update_tenant_settings(context, command)


@router.post("/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
async def create_project(command: ProjectCreate, service: Service, context: Context):
    return await service.create_project(context, command)


@router.get("/projects", response_model=list[ProjectRead])
async def list_projects(service: Service, context: Context):
    return await service.list_projects(context)


@router.get("/projects/{project_id}", response_model=ProjectRead)
async def get_project(project_id: UUID, service: Service, context: Context):
    return await service.get_project(context, project_id)


@router.patch("/projects/{project_id}", response_model=ProjectRead)
async def update_project(
    project_id: UUID, command: ProjectPatch, service: Service, context: Context
):
    return await service.update_project(context, project_id, command)


@router.post("/projects/{project_id}/archive", response_model=ProjectRead)
async def archive_project(
    project_id: UUID, command: RevisionCommand, service: Service, context: Context
):
    return await service.archive_project(
        context, project_id, expected_revision=command.expected_revision
    )


@router.post("/agents", response_model=AgentRead, status_code=status.HTTP_201_CREATED)
async def create_agent(command: AgentCreate, service: Service, context: Context):
    return await service.create_agent(context, command)


@router.get("/agents", response_model=list[AgentRead])
async def list_agents(service: Service, context: Context):
    return await service.list_agents(context)


@router.get("/agents/{agent_id}", response_model=AgentRead)
async def get_agent(agent_id: UUID, service: Service, context: Context):
    return await service.get_agent(context, agent_id)


@router.patch("/agents/{agent_id}", response_model=AgentRead)
async def update_agent(agent_id: UUID, command: AgentPatch, service: Service, context: Context):
    return await service.update_agent(context, agent_id, command)


@router.post("/agents/{agent_id}/clone", response_model=AgentRead, status_code=201)
async def clone_agent(agent_id: UUID, command: AgentClone, service: Service, context: Context):
    return await service.clone_agent(context, agent_id, command)


@router.post("/agents/{agent_id}/archive", response_model=AgentRead)
async def archive_agent(
    agent_id: UUID, command: RevisionCommand, service: Service, context: Context
):
    return await service.archive_agent(
        context, agent_id, expected_revision=command.expected_revision
    )


@router.post("/agents/{agent_id}/restore", response_model=AgentRead)
async def restore_agent(
    agent_id: UUID, command: RevisionCommand, service: Service, context: Context
):
    return await service.restore_agent(
        context, agent_id, expected_revision=command.expected_revision
    )


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: UUID,
    service: Service,
    context: Context,
    expected_revision: Annotated[int, Query(ge=1)],
) -> Response:
    await service.delete_agent(context, agent_id, expected_revision=expected_revision)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/agents/{agent_id}/versions",
    response_model=AgentVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_version(
    agent_id: UUID, command: AgentVersionCreate, service: Service, context: Context
):
    return await service.create_agent_version(context, agent_id, command)


@router.get("/agents/{agent_id}/versions", response_model=list[AgentVersionRead])
async def list_agent_versions(agent_id: UUID, service: Service, context: Context):
    return await service.list_agent_versions(context, agent_id)


@router.post("/agents/{agent_id}/versions/{version_id}/publish", response_model=AgentRead)
async def publish_agent_version(
    agent_id: UUID,
    version_id: UUID,
    command: RevisionCommand,
    service: Service,
    context: Context,
):
    return await service.publish_agent_version(
        context,
        agent_id,
        version_id,
        expected_revision=command.expected_revision,
    )


@router.post("/agents/{agent_id}/rollback", response_model=AgentRead)
async def rollback_agent_version(
    agent_id: UUID, command: AgentRollback, service: Service, context: Context
):
    return await service.rollback_agent_version(
        context,
        agent_id,
        command.version_id,
        expected_revision=command.expected_revision,
    )


@router.post("/tasks", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(command: TaskCreate, service: Service, context: Context):
    return await service.create_task(context, command)


@router.get("/tasks/{task_id}", response_model=TaskRead)
async def get_task(task_id: UUID, service: Service, context: Context):
    return await service.get_task(context, task_id)


@router.post("/tasks/{task_id}/transition", response_model=TaskRead)
async def transition_task(
    task_id: UUID, command: TaskTransition, service: Service, context: Context
):
    return await service.transition_task(context, task_id, command)


@router.post("/tasks/{task_id}/runs", response_model=RunRead, status_code=201)
async def create_run(task_id: UUID, command: RunCreate, service: Service, context: Context):
    return await service.create_run(context, task_id, command)


@router.get("/runs/{run_id}", response_model=RunRead)
async def get_run(run_id: UUID, service: Service, context: Context):
    return await service.get_run(context, run_id)


@router.get("/runs/{run_id}/runtime", response_model=RuntimeSessionRead)
async def get_runtime_session(run_id: UUID, service: Service, context: Context):
    return await service.get_runtime_session(context, run_id)


@router.get("/runs/{run_id}/trajectory", response_model=RuntimeTrajectory)
async def get_runtime_trajectory(run_id: UUID, service: Service, context: Context):
    return await service.get_runtime_trajectory(context, run_id)


@router.post("/runs/{run_id}/transition", response_model=RunRead)
async def transition_run(run_id: UUID, command: RunTransition, service: Service, context: Context):
    return await service.transition_run(context, run_id, command)


@router.post("/runs/{run_id}/cancel", response_model=RunRead)
async def cancel_run(run_id: UUID, command: RevisionCommand, service: Service, context: Context):
    return await service.cancel_run(context, run_id, expected_revision=command.expected_revision)


@router.post("/runs/{run_id}/retry", response_model=RunRead, status_code=201)
async def retry_run(run_id: UUID, command: RunCreate, service: Service, context: Context):
    return await service.retry_run(context, run_id, command)


@router.post("/runs/{run_id}/steps", response_model=RunStepRead, status_code=201)
async def create_run_step(run_id: UUID, command: RunStepCreate, service: Service, context: Context):
    return await service.create_run_step(context, run_id, command)


@router.post("/runs/{run_id}/steps/{step_id}/transition", response_model=RunStepRead)
async def transition_run_step(
    run_id: UUID,
    step_id: UUID,
    command: RunStepTransition,
    service: Service,
    context: Context,
):
    return await service.transition_run_step(context, run_id, step_id, command)


@router.get("/runs/{run_id}/events", response_model=list[EventRead])
async def list_run_events(run_id: UUID, service: Service, context: Context):
    return await service.list_run_events(context, run_id)


@router.get("/runs/{run_id}/steps", response_model=list[RunStepRead])
async def list_run_steps(run_id: UUID, service: Service, context: Context):
    return await service.list_run_steps(context, run_id)


@router.get("/tool-definitions", response_model=list[ToolDefinitionRead])
async def list_tool_definitions(service: Service, context: Context):
    return await service.list_tool_definitions(context)


@router.get("/runs/{run_id}/tool-calls", response_model=list[ToolCallRead])
async def list_run_tool_calls(run_id: UUID, service: Service, context: Context):
    return await service.list_run_tool_calls(context, run_id)


@router.get("/audit", response_model=list[AuditRead])
async def list_audit(
    service: Service,
    context: Context,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    return await service.list_audit(context, limit=limit)
