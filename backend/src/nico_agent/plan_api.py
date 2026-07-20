"""Tenant-scoped, read-only Plan and runtime evaluation history API."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from nico_agent.api_schemas import PlanRead, PlanStepRead, RuntimeEvaluationRead
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainError, ResourceNotFound
from nico_agent.domain.models import Plan, PlanStep, Run, RuntimeEvaluation
from nico_agent.domain_api import get_tenant_context

router = APIRouter(prefix="/api/v1", tags=["planning"])


class PlanQueryService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def list_plans(self, context: TenantContext, run_id: UUID) -> list[Plan]:
        async with self.database.tenant_transaction(context) as session:
            await self._require_run(session, context, run_id)
            return list(
                await session.scalars(
                    select(Plan)
                    .where(Plan.tenant_id == context.tenant_id, Plan.run_id == run_id)
                    .order_by(Plan.revision)
                )
            )

    async def get_plan(self, context: TenantContext, run_id: UUID, plan_id: UUID) -> Plan:
        async with self.database.tenant_transaction(context) as session:
            await self._require_run(session, context, run_id)
            plan = await session.scalar(
                select(Plan).where(
                    Plan.tenant_id == context.tenant_id,
                    Plan.run_id == run_id,
                    Plan.id == plan_id,
                )
            )
            if plan is None:
                raise ResourceNotFound("plan", str(plan_id))
            return plan

    async def list_steps(
        self, context: TenantContext, run_id: UUID, plan_id: UUID
    ) -> list[PlanStep]:
        await self.get_plan(context, run_id, plan_id)
        async with self.database.tenant_transaction(context) as session:
            return list(
                await session.scalars(
                    select(PlanStep)
                    .where(
                        PlanStep.tenant_id == context.tenant_id,
                        PlanStep.run_id == run_id,
                        PlanStep.plan_id == plan_id,
                    )
                    .order_by(PlanStep.position)
                )
            )

    async def list_evaluations(
        self, context: TenantContext, run_id: UUID
    ) -> list[RuntimeEvaluation]:
        async with self.database.tenant_transaction(context) as session:
            await self._require_run(session, context, run_id)
            return list(
                await session.scalars(
                    select(RuntimeEvaluation)
                    .where(
                        RuntimeEvaluation.tenant_id == context.tenant_id,
                        RuntimeEvaluation.run_id == run_id,
                    )
                    .order_by(RuntimeEvaluation.sequence)
                )
            )

    @staticmethod
    async def _require_run(session, context: TenantContext, run_id: UUID) -> Run:
        run = await session.scalar(
            select(Run).where(Run.tenant_id == context.tenant_id, Run.id == run_id)
        )
        if run is None:
            raise ResourceNotFound("run", str(run_id))
        return run


def get_plan_service(request: Request) -> PlanQueryService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the planning database is unavailable")
    return PlanQueryService(database)


Service = Annotated[PlanQueryService, Depends(get_plan_service)]
Context = Annotated[TenantContext, Depends(get_tenant_context)]


@router.get("/runs/{run_id}/plans", response_model=list[PlanRead])
async def list_run_plans(run_id: UUID, service: Service, context: Context):
    return await service.list_plans(context, run_id)


@router.get("/runs/{run_id}/plans/{plan_id}", response_model=PlanRead)
async def get_run_plan(run_id: UUID, plan_id: UUID, service: Service, context: Context):
    return await service.get_plan(context, run_id, plan_id)


@router.get("/runs/{run_id}/plans/{plan_id}/steps", response_model=list[PlanStepRead])
async def list_plan_steps(run_id: UUID, plan_id: UUID, service: Service, context: Context):
    return await service.list_steps(context, run_id, plan_id)


@router.get(
    "/runs/{run_id}/runtime-evaluations",
    response_model=list[RuntimeEvaluationRead],
)
async def list_runtime_evaluations(run_id: UUID, service: Service, context: Context):
    return await service.list_evaluations(context, run_id)
