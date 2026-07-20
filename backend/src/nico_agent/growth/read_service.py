"""Tenant-scoped read models for the growth HTTP surface."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import ResourceNotFound
from nico_agent.domain.models import (
    Approval,
    Evaluation,
    GrowthSource,
    Memory,
    Skill,
    SkillDeployment,
    SkillVersion,
)


class GrowthReadService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def list_memories(
        self,
        context: TenantContext,
        *,
        status: str | None = None,
        memory_type: str | None = None,
        scope_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Memory]:
        async with self.database.tenant_transaction(context) as session:
            statement = select(Memory)
            if status is not None:
                statement = statement.where(Memory.status == status)
            if memory_type is not None:
                statement = statement.where(Memory.memory_type == memory_type)
            if scope_type is not None:
                statement = statement.where(Memory.scope_type == scope_type)
            return list(
                (
                    await session.scalars(
                        statement.order_by(Memory.created_at.desc(), Memory.id.desc())
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
            )

    async def get_memory(self, context: TenantContext, memory_id: UUID) -> Memory:
        async with self.database.tenant_transaction(context) as session:
            return await self._memory(session, memory_id)

    async def list_skills(
        self,
        context: TenantContext,
        *,
        status: str | None = None,
        scope_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Skill]:
        async with self.database.tenant_transaction(context) as session:
            statement = select(Skill)
            if status is not None:
                statement = statement.where(Skill.status == status)
            if scope_type is not None:
                statement = statement.where(Skill.scope_type == scope_type)
            return list(
                (
                    await session.scalars(
                        statement.order_by(Skill.created_at.desc(), Skill.id.desc())
                        .offset(offset)
                        .limit(limit)
                    )
                ).all()
            )

    async def get_skill(self, context: TenantContext, skill_id: UUID) -> Skill:
        async with self.database.tenant_transaction(context) as session:
            return await self._skill(session, skill_id)

    async def list_skill_versions(
        self, context: TenantContext, skill_id: UUID
    ) -> list[SkillVersion]:
        async with self.database.tenant_transaction(context) as session:
            await self._skill(session, skill_id)
            return list(
                (
                    await session.scalars(
                        select(SkillVersion)
                        .where(SkillVersion.skill_id == skill_id)
                        .order_by(SkillVersion.version, SkillVersion.id)
                    )
                ).all()
            )

    async def get_skill_version(
        self, context: TenantContext, skill_id: UUID, version_id: UUID
    ) -> SkillVersion:
        async with self.database.tenant_transaction(context) as session:
            return await self._skill_version(session, skill_id, version_id)

    async def list_sources(
        self,
        context: TenantContext,
        subject_type: str,
        subject_id: UUID,
    ) -> list[GrowthSource]:
        async with self.database.tenant_transaction(context) as session:
            await self._require_subject(session, subject_type, subject_id)
            predicate = (
                GrowthSource.memory_id == subject_id
                if subject_type == "memory"
                else GrowthSource.skill_version_id == subject_id
            )
            return list(
                (
                    await session.scalars(
                        select(GrowthSource)
                        .where(GrowthSource.subject_type == subject_type, predicate)
                        .order_by(GrowthSource.created_at, GrowthSource.id)
                    )
                ).all()
            )

    async def list_evaluations(
        self,
        context: TenantContext,
        subject_type: str,
        subject_id: UUID,
    ) -> list[Evaluation]:
        async with self.database.tenant_transaction(context) as session:
            await self._require_subject(session, subject_type, subject_id)
            predicate = (
                Evaluation.memory_id == subject_id
                if subject_type == "memory"
                else Evaluation.skill_version_id == subject_id
            )
            return list(
                (
                    await session.scalars(
                        select(Evaluation)
                        .where(Evaluation.subject_type == subject_type, predicate)
                        .order_by(Evaluation.created_at.desc(), Evaluation.id.desc())
                    )
                ).all()
            )

    async def list_approvals(
        self,
        context: TenantContext,
        subject_type: str,
        subject_id: UUID,
    ) -> list[Approval]:
        async with self.database.tenant_transaction(context) as session:
            await self._require_subject(session, subject_type, subject_id)
            predicate = (
                Approval.memory_id == subject_id
                if subject_type == "memory"
                else Approval.skill_version_id == subject_id
            )
            return list(
                (
                    await session.scalars(
                        select(Approval)
                        .where(Approval.subject_type == subject_type, predicate)
                        .order_by(Approval.created_at.desc(), Approval.id.desc())
                    )
                ).all()
            )

    async def get_approval(self, context: TenantContext, approval_id: UUID) -> Approval:
        async with self.database.tenant_transaction(context) as session:
            approval = await session.scalar(select(Approval).where(Approval.id == approval_id))
            if approval is None:
                raise ResourceNotFound("approval", str(approval_id))
            return approval

    async def list_deployments(
        self,
        context: TenantContext,
        skill_id: UUID,
        *,
        status: str | None = None,
    ) -> list[SkillDeployment]:
        async with self.database.tenant_transaction(context) as session:
            await self._skill(session, skill_id)
            statement = select(SkillDeployment).where(SkillDeployment.skill_id == skill_id)
            if status is not None:
                statement = statement.where(SkillDeployment.status == status)
            return list(
                (
                    await session.scalars(
                        statement.order_by(
                            SkillDeployment.created_at.desc(), SkillDeployment.id.desc()
                        )
                    )
                ).all()
            )

    @staticmethod
    async def _memory(session: AsyncSession, memory_id: UUID) -> Memory:
        memory = await session.scalar(select(Memory).where(Memory.id == memory_id))
        if memory is None:
            raise ResourceNotFound("memory", str(memory_id))
        return memory

    @staticmethod
    async def _skill(session: AsyncSession, skill_id: UUID) -> Skill:
        skill = await session.scalar(select(Skill).where(Skill.id == skill_id))
        if skill is None:
            raise ResourceNotFound("skill", str(skill_id))
        return skill

    @staticmethod
    async def _skill_version(
        session: AsyncSession, skill_id: UUID, version_id: UUID
    ) -> SkillVersion:
        version = await session.scalar(
            select(SkillVersion).where(
                SkillVersion.id == version_id,
                SkillVersion.skill_id == skill_id,
            )
        )
        if version is None:
            raise ResourceNotFound("skill_version", str(version_id))
        return version

    @staticmethod
    async def _require_subject(session: AsyncSession, subject_type: str, subject_id: UUID) -> None:
        if subject_type == "memory":
            await GrowthReadService._memory(session, subject_id)
            return
        version = await session.scalar(select(SkillVersion).where(SkillVersion.id == subject_id))
        if version is None:
            raise ResourceNotFound("skill_version", str(subject_id))
