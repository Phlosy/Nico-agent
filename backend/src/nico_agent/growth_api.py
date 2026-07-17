"""FastAPI routes for Goal F controlled Memory and Skill growth."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainError, ResourceNotFound
from nico_agent.domain_api import get_tenant_context
from nico_agent.growth.api_service import GrowthApiService
from nico_agent.growth.approval import ApprovalReference
from nico_agent.growth.contracts import CandidateGenerationResult, GrowthPolicy
from nico_agent.growth.evaluation import EvaluationReference
from nico_agent.growth_api_schemas import (
    ApprovalCancelCommand,
    ApprovalDecisionCommand,
    ApprovalRead,
    ApprovalRequestCommand,
    EvaluationRead,
    GrowthCandidatesCommand,
    GrowthPublishCommand,
    GrowthSourceRead,
    GrowthTransitionCommand,
    MemoryRead,
    MemoryRevisionCommand,
    MemorySearchCommand,
    MemorySearchRead,
    MemoryStatusValue,
    MemoryTypeValue,
    ScopeValue,
    SkillCanaryCommand,
    SkillDeploymentEntityRead,
    SkillPublishCommand,
    SkillRead,
    SkillRevisionCommand,
    SkillStatusValue,
    SkillStopCommand,
    SkillSwitchCommand,
    SkillVersionEntityRead,
)
from nico_agent.memory.contracts import MemoryQueryContext
from nico_agent.memory.lifecycle import MemoryLifecycleResult
from nico_agent.skills.contracts import (
    SkillDeploymentReference,
    SkillResolution,
    SkillVersionComparison,
    SkillVersionReference,
)

router = APIRouter(prefix="/api/v1", tags=["growth"])


def get_growth_service(request: Request) -> GrowthApiService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the growth database is unavailable")
    return GrowthApiService(database)


Service = Annotated[GrowthApiService, Depends(get_growth_service)]
Context = Annotated[TenantContext, Depends(get_tenant_context)]


@router.post(
    "/runs/{run_id}/growth-candidates",
    response_model=CandidateGenerationResult,
    status_code=status.HTTP_200_OK,
    summary="Generate controlled candidates from a terminal Run",
)
async def generate_growth_candidates(
    run_id: UUID,
    command: GrowthCandidatesCommand,
    service: Service,
    context: Context,
):
    return await service.candidates.generate(
        context,
        run_id,
        policy=GrowthPolicy(**command.model_dump()),
    )


@router.get("/memories", response_model=list[MemoryRead])
async def list_memories(
    service: Service,
    context: Context,
    memory_status: Annotated[MemoryStatusValue | None, Query(alias="status")] = None,
    memory_type: MemoryTypeValue | None = None,
    scope_type: ScopeValue | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
):
    return await service.read.list_memories(
        context,
        status=memory_status,
        memory_type=memory_type,
        scope_type=scope_type,
        limit=limit,
        offset=offset,
    )


@router.post("/memories/search", response_model=list[MemorySearchRead])
async def search_memories(
    command: MemorySearchCommand,
    service: Service,
    context: Context,
) -> list[MemorySearchRead]:
    results = await service.retrieval.search(
        context,
        MemoryQueryContext(project_id=command.project_id, agent_id=command.agent_id),
        command.query,
        limit=command.limit,
        minimum_similarity=command.minimum_similarity,
    )
    return [MemorySearchRead.model_validate(item) for item in results]


@router.get("/memories/{memory_id}", response_model=MemoryRead)
async def get_memory(memory_id: UUID, service: Service, context: Context):
    return await service.read.get_memory(context, memory_id)


@router.get("/memories/{memory_id}/sources", response_model=list[GrowthSourceRead])
async def list_memory_sources(memory_id: UUID, service: Service, context: Context):
    return await service.read.list_sources(context, "memory", memory_id)


@router.get("/memories/{memory_id}/evaluations", response_model=list[EvaluationRead])
async def list_memory_evaluations(memory_id: UUID, service: Service, context: Context):
    return await service.read.list_evaluations(context, "memory", memory_id)


@router.post("/memories/{memory_id}/evaluations", response_model=EvaluationReference)
async def evaluate_memory(memory_id: UUID, service: Service, context: Context):
    return await service.evaluations.evaluate(context, "memory", memory_id)


@router.get("/memories/{memory_id}/approvals", response_model=list[ApprovalRead])
async def list_memory_approvals(memory_id: UUID, service: Service, context: Context):
    return await service.read.list_approvals(context, "memory", memory_id)


@router.post("/memories/{memory_id}/approvals", response_model=ApprovalReference)
async def request_memory_approval(
    memory_id: UUID,
    command: ApprovalRequestCommand,
    service: Service,
    context: Context,
):
    return await service.approvals.request(
        context,
        "memory",
        memory_id,
        expected_revision=command.expected_revision,
        expires_in_seconds=command.expires_in_seconds,
    )


@router.post("/memories/{memory_id}/publish", response_model=MemoryLifecycleResult)
async def publish_memory(
    memory_id: UUID,
    command: GrowthPublishCommand,
    service: Service,
    context: Context,
):
    return await service.memories.publish(
        context,
        memory_id,
        expected_revision=command.expected_revision,
    )


@router.post(
    "/memories/{memory_id}/revisions",
    response_model=MemoryLifecycleResult,
    status_code=status.HTTP_201_CREATED,
)
async def revise_memory(
    memory_id: UUID,
    command: MemoryRevisionCommand,
    service: Service,
    context: Context,
):
    return await service.memories.revise(
        context,
        memory_id,
        content=command.content,
        reason=command.reason,
        expected_revision=command.expected_revision,
        confidence=command.confidence,
        expires_at=command.expires_at,
    )


@router.post("/memories/{memory_id}/invalidate", response_model=MemoryLifecycleResult)
async def invalidate_memory(
    memory_id: UUID,
    command: GrowthTransitionCommand,
    service: Service,
    context: Context,
):
    return await service.memories.invalidate(
        context,
        memory_id,
        reason=command.reason,
        expected_revision=command.expected_revision,
    )


@router.post("/memories/{memory_id}/expire", response_model=MemoryLifecycleResult)
async def expire_memory(
    memory_id: UUID,
    command: GrowthTransitionCommand,
    service: Service,
    context: Context,
):
    return await service.memories.expire(
        context,
        memory_id,
        reason=command.reason,
        expected_revision=command.expected_revision,
    )


@router.delete("/memories/{memory_id}", response_model=MemoryLifecycleResult)
async def delete_memory(
    memory_id: UUID,
    service: Service,
    context: Context,
    expected_revision: Annotated[int, Query(ge=1)],
    reason: Annotated[
        str,
        Query(min_length=1, max_length=10_000, pattern=r".*\S.*"),
    ],
):
    return await service.memories.delete(
        context,
        memory_id,
        reason=reason,
        expected_revision=expected_revision,
    )


@router.get("/growth-approvals/{approval_id}", response_model=ApprovalRead)
async def get_growth_approval(approval_id: UUID, service: Service, context: Context):
    return await service.read.get_approval(context, approval_id)


@router.post("/growth-approvals/{approval_id}/decision", response_model=ApprovalReference)
async def decide_growth_approval(
    approval_id: UUID,
    command: ApprovalDecisionCommand,
    service: Service,
    context: Context,
):
    return await service.approvals.decide(
        context,
        approval_id,
        decision=command.decision,
        reason=command.reason,
        expected_revision=command.expected_revision,
    )


@router.post("/growth-approvals/{approval_id}/cancel", response_model=ApprovalReference)
async def cancel_growth_approval(
    approval_id: UUID,
    command: ApprovalCancelCommand,
    service: Service,
    context: Context,
):
    return await service.approvals.cancel(
        context,
        approval_id,
        reason=command.reason,
        expected_revision=command.expected_revision,
    )


@router.get("/skills", response_model=list[SkillRead])
async def list_skills(
    service: Service,
    context: Context,
    skill_status: Annotated[SkillStatusValue | None, Query(alias="status")] = None,
    scope_type: ScopeValue | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
):
    return await service.read.list_skills(
        context,
        status=skill_status,
        scope_type=scope_type,
        limit=limit,
        offset=offset,
    )


@router.get("/skills/{skill_id}", response_model=SkillRead)
async def get_skill(skill_id: UUID, service: Service, context: Context):
    return await service.read.get_skill(context, skill_id)


@router.get("/skills/{skill_id}/versions", response_model=list[SkillVersionEntityRead])
async def list_skill_versions(skill_id: UUID, service: Service, context: Context):
    return await service.read.list_skill_versions(context, skill_id)


@router.get("/skills/{skill_id}/versions/compare", response_model=SkillVersionComparison)
async def compare_skill_versions(
    skill_id: UUID,
    service: Service,
    context: Context,
    from_version_id: UUID,
    to_version_id: UUID,
):
    return await service.skills.compare(
        context,
        skill_id,
        from_version_id,
        to_version_id,
    )


@router.get("/skills/{skill_id}/versions/{version_id}", response_model=SkillVersionEntityRead)
async def get_skill_version(
    skill_id: UUID,
    version_id: UUID,
    service: Service,
    context: Context,
):
    return await service.read.get_skill_version(context, skill_id, version_id)


@router.get(
    "/skills/{skill_id}/versions/{version_id}/sources",
    response_model=list[GrowthSourceRead],
)
async def list_skill_version_sources(
    skill_id: UUID,
    version_id: UUID,
    service: Service,
    context: Context,
):
    await service.read.get_skill_version(context, skill_id, version_id)
    return await service.read.list_sources(context, "skill_version", version_id)


@router.get(
    "/skills/{skill_id}/versions/{version_id}/evaluations",
    response_model=list[EvaluationRead],
)
async def list_skill_version_evaluations(
    skill_id: UUID,
    version_id: UUID,
    service: Service,
    context: Context,
):
    await service.read.get_skill_version(context, skill_id, version_id)
    return await service.read.list_evaluations(context, "skill_version", version_id)


@router.post(
    "/skills/{skill_id}/versions/{version_id}/evaluations",
    response_model=EvaluationReference,
)
async def evaluate_skill_version(
    skill_id: UUID,
    version_id: UUID,
    service: Service,
    context: Context,
):
    await service.read.get_skill_version(context, skill_id, version_id)
    return await service.evaluations.evaluate(context, "skill_version", version_id)


@router.get(
    "/skills/{skill_id}/versions/{version_id}/approvals",
    response_model=list[ApprovalRead],
)
async def list_skill_version_approvals(
    skill_id: UUID,
    version_id: UUID,
    service: Service,
    context: Context,
):
    await service.read.get_skill_version(context, skill_id, version_id)
    return await service.read.list_approvals(context, "skill_version", version_id)


@router.post(
    "/skills/{skill_id}/versions/{version_id}/approvals",
    response_model=ApprovalReference,
)
async def request_skill_version_approval(
    skill_id: UUID,
    version_id: UUID,
    command: ApprovalRequestCommand,
    service: Service,
    context: Context,
):
    await service.read.get_skill_version(context, skill_id, version_id)
    return await service.approvals.request(
        context,
        "skill_version",
        version_id,
        expected_revision=command.expected_revision,
        expires_in_seconds=command.expires_in_seconds,
    )


@router.post(
    "/skills/{skill_id}/versions/{base_version_id}/revisions",
    response_model=SkillVersionReference,
    status_code=status.HTTP_201_CREATED,
)
async def revise_skill_version(
    skill_id: UUID,
    base_version_id: UUID,
    command: SkillRevisionCommand,
    service: Service,
    context: Context,
):
    return await service.skills.revise(
        context,
        skill_id,
        base_version_id,
        draft=command.draft,
        reason=command.reason,
        expected_skill_revision=command.expected_skill_revision,
        expected_version_revision=command.expected_version_revision,
    )


@router.post(
    "/skills/{skill_id}/versions/{version_id}/publish",
    response_model=SkillVersionReference,
)
async def publish_skill_version(
    skill_id: UUID,
    version_id: UUID,
    command: SkillPublishCommand,
    service: Service,
    context: Context,
):
    return await service.skills.publish_version(
        context,
        skill_id,
        version_id,
        expected_skill_revision=command.expected_skill_revision,
        expected_version_revision=command.expected_version_revision,
    )


@router.get("/skills/{skill_id}/deployments", response_model=list[SkillDeploymentEntityRead])
async def list_skill_deployments(
    skill_id: UUID,
    service: Service,
    context: Context,
    deployment_status: Annotated[Literal["active", "retired"] | None, Query(alias="status")] = None,
):
    return await service.read.list_deployments(
        context,
        skill_id,
        status=deployment_status,
    )


@router.post("/skills/{skill_id}/deployments", response_model=SkillDeploymentReference)
async def deploy_skill_canary(
    skill_id: UUID,
    command: SkillCanaryCommand,
    service: Service,
    context: Context,
):
    return await service.skills.deploy_canary(
        context,
        skill_id,
        command.skill_version_id,
        scope_type=command.scope_type,
        scope_id=command.scope_id,
        rollout_percentage=command.rollout_percentage,
        expected_skill_revision=command.expected_skill_revision,
    )


@router.post(
    "/skills/{skill_id}/deployments/{deployment_id}/retire",
    response_model=SkillDeploymentReference,
)
async def retire_skill_canary(
    skill_id: UUID,
    deployment_id: UUID,
    command: GrowthTransitionCommand,
    service: Service,
    context: Context,
):
    deployments = await service.read.list_deployments(context, skill_id)
    if deployment_id not in {item.id for item in deployments}:
        raise ResourceNotFound("skill_deployment", str(deployment_id))
    return await service.skills.retire_canary(
        context,
        deployment_id,
        reason=command.reason,
        expected_revision=command.expected_revision,
    )


@router.get("/skills/{skill_id}/resolve", response_model=SkillResolution)
async def resolve_skill(
    skill_id: UUID,
    service: Service,
    context: Context,
    run_id: UUID,
):
    return await service.skills.resolve(context, skill_id, run_id)


@router.post("/skills/{skill_id}/promote", response_model=SkillVersionReference)
async def promote_skill_version(
    skill_id: UUID,
    command: SkillSwitchCommand,
    service: Service,
    context: Context,
):
    return await service.skills.promote(
        context,
        skill_id,
        command.skill_version_id,
        reason=command.reason,
        expected_skill_revision=command.expected_skill_revision,
    )


@router.post("/skills/{skill_id}/rollback", response_model=SkillVersionReference)
async def rollback_skill_version(
    skill_id: UUID,
    command: SkillSwitchCommand,
    service: Service,
    context: Context,
):
    return await service.skills.rollback(
        context,
        skill_id,
        command.skill_version_id,
        reason=command.reason,
        expected_skill_revision=command.expected_skill_revision,
    )


@router.post("/skills/{skill_id}/deprecate", response_model=SkillVersionReference)
async def deprecate_skill(
    skill_id: UUID,
    command: SkillStopCommand,
    service: Service,
    context: Context,
):
    return await service.skills.deprecate(
        context,
        skill_id,
        reason=command.reason,
        expected_skill_revision=command.expected_skill_revision,
    )


@router.post("/skills/{skill_id}/disable", response_model=SkillVersionReference)
async def disable_skill(
    skill_id: UUID,
    command: SkillStopCommand,
    service: Service,
    context: Context,
):
    return await service.skills.disable(
        context,
        skill_id,
        reason=command.reason,
        expected_skill_revision=command.expected_skill_revision,
    )
