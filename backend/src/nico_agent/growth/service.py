"""Idempotently persist MemoryCandidate and SkillCandidate from reflection DTOs."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainConflict
from nico_agent.domain.models import (
    AuditRecord,
    Event,
    GrowthSource,
    Memory,
    Skill,
    SkillVersion,
)
from nico_agent.growth.contracts import (
    CandidateGenerationResult,
    GrowthPolicy,
    MemoryCandidateDraft,
    MemoryCandidateReference,
    ReflectionProvider,
    ReflectionResult,
    SkillCandidateDraft,
    SkillCandidateReference,
    TrajectorySnapshot,
    canonical_hash,
)
from nico_agent.growth.reflection import DeterministicReflectionProvider
from nico_agent.growth.snapshot import TrajectorySnapshotBuilder
from nico_agent.memory.chunking import content_hash, normalize_text


class GrowthCandidateService:
    def __init__(
        self,
        database: Database,
        *,
        provider: ReflectionProvider | None = None,
        snapshot_builder: TrajectorySnapshotBuilder | None = None,
    ) -> None:
        self.database = database
        self.provider = provider or DeterministicReflectionProvider()
        self.snapshot_builder = snapshot_builder or TrajectorySnapshotBuilder(database)
        if not self.provider.name or not self.provider.version:
            raise ValueError("reflection provider requires stable name and version")
        if len(self.provider.name) > 120 or len(self.provider.version) > 80:
            raise ValueError("reflection provider identity exceeds persistence limits")
        if len(self._actor) > 200:
            raise ValueError("reflection provider identity exceeds actor persistence limits")

    async def generate(
        self,
        context: TenantContext,
        run_id: UUID,
        *,
        policy: GrowthPolicy | None = None,
    ) -> CandidateGenerationResult:
        policy = policy or GrowthPolicy()
        initial_snapshot = await self.snapshot_builder.build(context, run_id)
        reflection = await self.provider.reflect(initial_snapshot)
        reflection = self._apply_policy(reflection, policy)
        plans = self._candidate_plans(initial_snapshot, reflection, policy)

        async with self.database.tenant_transaction(context) as session:
            locked_snapshot = await self.snapshot_builder.build_in_session(
                session, context, run_id, for_update=True
            )
            if locked_snapshot.snapshot_hash != initial_snapshot.snapshot_hash:
                raise DomainConflict(
                    "TRAJECTORY_CHANGED",
                    "terminal trajectory changed while reflection was running",
                    details={"run_id": str(run_id)},
                )
            existing_sources = list(
                await session.scalars(
                    select(GrowthSource).where(
                        GrowthSource.run_id == run_id,
                        GrowthSource.generator_name == self.provider.name,
                        GrowthSource.generator_version == self.provider.version,
                        GrowthSource.trajectory_hash == initial_snapshot.snapshot_hash,
                    )
                )
            )
            matching = {
                source.source_hash: source
                for source in existing_sources
                if source.snapshot.get("policy_hash") == policy.content_hash
            }
            expected_keys = {plan["source_hash"] for plan in plans}
            if matching and set(matching) != expected_keys:
                raise DomainConflict(
                    "REFLECTION_NONDETERMINISTIC",
                    "the same provider version produced a different candidate set",
                    details={"run_id": str(run_id)},
                )
            if set(matching) == expected_keys:
                return await self._existing_result(
                    session, initial_snapshot, policy, plans, matching
                )

            memory_refs: list[MemoryCandidateReference] = []
            skill_ref: SkillCandidateReference | None = None
            for plan in plans:
                if plan["kind"] == "memory":
                    memory, source = await self._persist_memory(
                        session, context, initial_snapshot, policy, plan
                    )
                    memory_refs.append(self._memory_reference(memory, source.source_hash))
                    self._record_candidate(
                        session,
                        context,
                        initial_snapshot,
                        aggregate_type="memory",
                        aggregate_id=memory.id,
                        event_type="MemoryCandidateCreated",
                        action="memory.candidate.create",
                        payload={
                            "memory_type": memory.memory_type,
                            "scope_type": memory.scope_type,
                            "source_hash": source.source_hash,
                        },
                    )
                else:
                    skill, version, source = await self._persist_skill(
                        session, context, initial_snapshot, policy, plan
                    )
                    skill_ref = self._skill_reference(skill, version, source.source_hash)
                    self._record_candidate(
                        session,
                        context,
                        initial_snapshot,
                        aggregate_type="skill_version",
                        aggregate_id=version.id,
                        event_type="SkillCandidateCreated",
                        action="skill.candidate.create",
                        payload={
                            "skill_id": str(skill.id),
                            "scope_type": skill.scope_type,
                            "source_hash": source.source_hash,
                        },
                    )
            await session.flush()
            return CandidateGenerationResult(
                run_id=run_id,
                snapshot_hash=initial_snapshot.snapshot_hash,
                generator_name=self.provider.name,
                generator_version=self.provider.version,
                policy_hash=policy.content_hash,
                memories=tuple(memory_refs),
                skill=skill_ref,
                already_generated=False,
            )

    @staticmethod
    def _apply_policy(result: ReflectionResult, policy: GrowthPolicy) -> ReflectionResult:
        memories = tuple(
            item
            for item in result.memories
            if not (item.memory_type == "semantic" and not policy.generate_semantic)
            and not (item.memory_type == "procedural" and not policy.generate_procedural)
        )
        skill = result.skill if policy.generate_skill else None
        if not memories and skill is None:
            raise DomainConflict(
                "REFLECTION_EMPTY",
                "growth policy removed every candidate from the reflection result",
            )
        return ReflectionResult(memories=memories, skill=skill, notes=result.notes)

    def _candidate_plans(
        self,
        snapshot: TrajectorySnapshot,
        reflection: ReflectionResult,
        policy: GrowthPolicy,
    ) -> list[dict[str, Any]]:
        plans: list[dict[str, Any]] = []
        memories = sorted(reflection.memories, key=lambda item: (item.memory_type, item.content))
        for ordinal, draft in enumerate(memories):
            normalized_content = normalize_text(draft.content)
            if not normalized_content:
                raise DomainConflict(
                    "REFLECTION_CONTENT_EMPTY",
                    "reflection produced empty memory content after normalization",
                    details={"memory_type": draft.memory_type},
                )
            draft_hash = content_hash(normalized_content)
            key = canonical_hash(
                {
                    "snapshot_hash": snapshot.snapshot_hash,
                    "provider": self.provider.name,
                    "provider_version": self.provider.version,
                    "policy_hash": policy.content_hash,
                    "kind": f"memory:{draft.memory_type}",
                    "ordinal": ordinal,
                    "content_hash": draft_hash,
                }
            )
            plans.append(
                {
                    "kind": "memory",
                    "draft": draft,
                    "content": normalized_content,
                    "content_hash": draft_hash,
                    "source_hash": key,
                }
            )
        if reflection.skill is not None:
            skill_hash = self._skill_content_hash(reflection.skill)
            key = canonical_hash(
                {
                    "snapshot_hash": snapshot.snapshot_hash,
                    "provider": self.provider.name,
                    "provider_version": self.provider.version,
                    "policy_hash": policy.content_hash,
                    "kind": "skill_version",
                    "content_hash": skill_hash,
                }
            )
            plans.append(
                {
                    "kind": "skill",
                    "draft": reflection.skill,
                    "content_hash": skill_hash,
                    "source_hash": key,
                }
            )
        return plans

    async def _persist_memory(
        self,
        session: AsyncSession,
        context: TenantContext,
        snapshot: TrajectorySnapshot,
        policy: GrowthPolicy,
        plan: dict[str, Any],
    ) -> tuple[Memory, GrowthSource]:
        draft: MemoryCandidateDraft = plan["draft"]
        expires_at = None
        if draft.memory_type == "working":
            expires_at = snapshot.ended_at + timedelta(seconds=policy.working_ttl_seconds)
        project_id, agent_id = self._scope_owners(
            policy.memory_scope, snapshot.project_id, snapshot.agent_id
        )
        memory = Memory(
            tenant_id=context.tenant_id,
            version=1,
            memory_type=draft.memory_type,
            scope_type=policy.memory_scope,
            project_id=project_id,
            agent_id=agent_id,
            content=plan["content"],
            confidence=draft.confidence,
            content_hash=plan["content_hash"],
            created_by=self._actor,
            expires_at=expires_at,
        )
        session.add(memory)
        await session.flush()
        source = self._source(context, snapshot, policy, plan["source_hash"], memory=memory)
        session.add(source)
        await session.flush()
        return memory, source

    async def _persist_skill(
        self,
        session: AsyncSession,
        context: TenantContext,
        snapshot: TrajectorySnapshot,
        policy: GrowthPolicy,
        plan: dict[str, Any],
    ) -> tuple[Skill, SkillVersion, GrowthSource]:
        draft: SkillCandidateDraft = plan["draft"]
        project_id, agent_id = self._scope_owners(
            policy.skill_scope, snapshot.project_id, snapshot.agent_id
        )
        name = self._skill_name(draft.name_hint, plan["source_hash"])
        skill = Skill(
            tenant_id=context.tenant_id,
            name=name,
            description=draft.description,
            scope_type=policy.skill_scope,
            project_id=project_id,
            agent_id=agent_id,
            created_by=self._actor,
        )
        session.add(skill)
        await session.flush()
        version = SkillVersion(
            tenant_id=context.tenant_id,
            skill_id=skill.id,
            version=1,
            conditions=draft.conditions,
            preconditions=draft.preconditions,
            input_schema=draft.input_schema,
            steps=draft.steps,
            tools=draft.tools,
            output_schema=draft.output_schema,
            validation=draft.validation,
            failure_modes=draft.failure_modes,
            content_hash=plan["content_hash"],
            created_by=self._actor,
        )
        session.add(version)
        await session.flush()
        source = self._source(context, snapshot, policy, plan["source_hash"], skill_version=version)
        session.add(source)
        await session.flush()
        return skill, version, source

    def _source(
        self,
        context: TenantContext,
        snapshot: TrajectorySnapshot,
        policy: GrowthPolicy,
        source_hash: str,
        *,
        memory: Memory | None = None,
        skill_version: SkillVersion | None = None,
    ) -> GrowthSource:
        primary_step = snapshot.steps[-1]
        primary_tool = primary_step.tool_calls[-1] if primary_step.tool_calls else None
        return GrowthSource(
            tenant_id=context.tenant_id,
            subject_type="memory" if memory is not None else "skill_version",
            memory_id=memory.id if memory is not None else None,
            skill_version_id=skill_version.id if skill_version is not None else None,
            run_id=snapshot.run_id,
            run_step_id=primary_step.id,
            tool_call_id=primary_tool.id if primary_tool is not None else None,
            runtime_session_id=snapshot.runtime.id if snapshot.runtime is not None else None,
            agent_version_id=snapshot.agent_version_id,
            trajectory_hash=snapshot.snapshot_hash,
            generator_name=self.provider.name,
            generator_version=self.provider.version,
            source_hash=source_hash,
            snapshot={
                "candidate_key": source_hash,
                "policy_hash": policy.content_hash,
                "trajectory": snapshot.model_dump(mode="json"),
            },
        )

    async def _existing_result(
        self,
        session: AsyncSession,
        snapshot: TrajectorySnapshot,
        policy: GrowthPolicy,
        plans: list[dict[str, Any]],
        sources: dict[str, GrowthSource],
    ) -> CandidateGenerationResult:
        memory_refs: list[MemoryCandidateReference] = []
        skill_ref: SkillCandidateReference | None = None
        for plan in plans:
            source = sources[plan["source_hash"]]
            if plan["kind"] == "memory":
                memory = await session.get(Memory, source.memory_id)
                if memory is None:
                    raise DomainConflict("GROWTH_SOURCE_BROKEN", "memory candidate is missing")
                memory_refs.append(self._memory_reference(memory, source.source_hash))
            else:
                version = await session.get(SkillVersion, source.skill_version_id)
                if version is None:
                    raise DomainConflict(
                        "GROWTH_SOURCE_BROKEN", "skill version candidate is missing"
                    )
                skill = await session.get(Skill, version.skill_id)
                if skill is None:
                    raise DomainConflict("GROWTH_SOURCE_BROKEN", "skill candidate is missing")
                skill_ref = self._skill_reference(skill, version, source.source_hash)
        return CandidateGenerationResult(
            run_id=snapshot.run_id,
            snapshot_hash=snapshot.snapshot_hash,
            generator_name=self.provider.name,
            generator_version=self.provider.version,
            policy_hash=policy.content_hash,
            memories=tuple(memory_refs),
            skill=skill_ref,
            already_generated=True,
        )

    @property
    def _actor(self) -> str:
        return f"reflection:{self.provider.name}@{self.provider.version}"

    @staticmethod
    def _scope_owners(
        scope: str, project_id: UUID, agent_id: UUID
    ) -> tuple[UUID | None, UUID | None]:
        if scope == "project":
            return project_id, None
        if scope == "agent":
            return None, agent_id
        return None, None

    @staticmethod
    def _skill_content_hash(draft: SkillCandidateDraft) -> str:
        return canonical_hash(
            {
                "conditions": draft.conditions,
                "preconditions": draft.preconditions,
                "input_schema": draft.input_schema,
                "steps": draft.steps,
                "tools": draft.tools,
                "output_schema": draft.output_schema,
                "validation": draft.validation,
                "failure_modes": draft.failure_modes,
            }
        )

    @staticmethod
    def _skill_name(name_hint: str, source_hash: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", name_hint.casefold()).strip("-")
        slug = slug[:120].strip("-") or "learned-skill"
        return f"{slug}-{source_hash[:12]}"

    @staticmethod
    def _memory_reference(memory: Memory, source_hash: str) -> MemoryCandidateReference:
        return MemoryCandidateReference(
            id=memory.id,
            memory_key=memory.memory_key,
            memory_type=memory.memory_type,
            scope_type=memory.scope_type,
            content_hash=memory.content_hash,
            source_hash=source_hash,
        )

    @staticmethod
    def _skill_reference(
        skill: Skill, version: SkillVersion, source_hash: str
    ) -> SkillCandidateReference:
        return SkillCandidateReference(
            skill_id=skill.id,
            skill_version_id=version.id,
            name=skill.name,
            content_hash=version.content_hash,
            source_hash=source_hash,
        )

    def _record_candidate(
        self,
        session: AsyncSession,
        context: TenantContext,
        snapshot: TrajectorySnapshot,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        event_type: str,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        session.add_all(
            [
                Event(
                    tenant_id=context.tenant_id,
                    event_type=event_type,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    run_id=snapshot.run_id,
                    actor_id=self._actor,
                    payload=payload,
                    correlation_id=context.correlation_id,
                ),
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action=action,
                    resource_type=aggregate_type,
                    resource_id=aggregate_id,
                    actor_id=self._actor,
                    details=payload,
                    correlation_id=context.correlation_id,
                ),
            ]
        )
