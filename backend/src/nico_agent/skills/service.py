"""Controlled SkillVersion publication, canary resolution, promotion and rollback."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import and_, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import AccessDenied, DomainConflict, ResourceNotFound
from nico_agent.domain.models import (
    Agent,
    AuditRecord,
    Event,
    GrowthSource,
    Project,
    Run,
    Skill,
    SkillDeployment,
    SkillVersion,
    Task,
)
from nico_agent.domain.states import require_revision
from nico_agent.growth.approval import GrowthApprovalService
from nico_agent.growth.contracts import canonical_hash, skill_content_hash
from nico_agent.growth.evaluation import ValidationSubjectBuilder
from nico_agent.skills.contracts import (
    SkillDeploymentReference,
    SkillResolution,
    SkillVersionComparison,
    SkillVersionDraft,
    SkillVersionReference,
    compare_skill_versions,
    stable_rollout_bucket,
    version_payload,
)


class SkillLifecycleService:
    revision_generator_name = "manual-skill-revision"
    revision_generator_version = "1.0.0"

    def __init__(self, database: Database) -> None:
        self.database = database

    async def compare(
        self,
        context: TenantContext,
        skill_id: UUID,
        from_version_id: UUID,
        to_version_id: UUID,
    ) -> SkillVersionComparison:
        async with self.database.tenant_transaction(context) as session:
            await self._get_skill(session, skill_id)
            source = await self._get_version(session, skill_id, from_version_id)
            target = await self._get_version(session, skill_id, to_version_id)
            return compare_skill_versions(
                skill_id=skill_id,
                from_version_id=source.id,
                from_version=source.version,
                from_content_hash=source.content_hash,
                from_payload=version_payload(source),
                to_version_id=target.id,
                to_version=target.version,
                to_content_hash=target.content_hash,
                to_payload=version_payload(target),
            )

    async def revise(
        self,
        context: TenantContext,
        skill_id: UUID,
        base_version_id: UUID,
        *,
        draft: SkillVersionDraft,
        reason: str,
        expected_skill_revision: int,
        expected_version_revision: int,
    ) -> SkillVersionReference:
        reason = self._reason(reason)
        new_hash = skill_content_hash(draft)
        async with self.database.tenant_transaction(context) as session:
            skill = await self._lock_skill(session, skill_id)
            require_revision("skill", expected=expected_skill_revision, actual=skill.revision)
            if skill.status == "disabled":
                raise DomainConflict(
                    "SKILL_DISABLED",
                    "a disabled skill cannot receive a new version",
                    details={"skill_id": str(skill_id)},
                )
            base = await self._lock_version(session, skill_id, base_version_id)
            require_revision(
                "skill_version", expected=expected_version_revision, actual=base.revision
            )
            latest = await session.scalar(
                select(SkillVersion)
                .where(SkillVersion.skill_id == skill.id)
                .order_by(SkillVersion.version.desc(), SkillVersion.id.desc())
                .limit(1)
                .with_for_update()
            )
            if latest is None or latest.id != base.id:
                raise DomainConflict(
                    "SKILL_VERSION_REVISION_STALE",
                    "only the latest skill version can be revised",
                    details={"skill_version_id": str(base_version_id)},
                )
            if new_hash == base.content_hash:
                raise DomainConflict(
                    "SKILL_VERSION_UNCHANGED",
                    "a skill revision must change its canonical content",
                )
            source = await session.scalar(
                select(GrowthSource)
                .where(GrowthSource.skill_version_id == base.id)
                .order_by(GrowthSource.created_at, GrowthSource.id)
            )
            if source is None:
                raise DomainConflict(
                    "SKILL_SOURCE_MISSING",
                    "a skill revision requires an immutable source",
                )
            revised = SkillVersion(
                tenant_id=context.tenant_id,
                skill_id=skill.id,
                version=base.version + 1,
                status="draft",
                conditions=draft.conditions,
                preconditions=list(draft.preconditions),
                input_schema=draft.input_schema,
                steps=list(draft.steps),
                tools=list(draft.tools),
                output_schema=draft.output_schema,
                validation=draft.validation,
                failure_modes=list(draft.failure_modes),
                content_hash=new_hash,
                created_by=context.actor_id,
            )
            session.add(revised)
            await session.flush()
            source_hash = canonical_hash(
                {
                    "tenant_id": context.tenant_id,
                    "skill_id": skill.id,
                    "version": revised.version,
                    "content_hash": revised.content_hash,
                    "parent_source_hash": source.source_hash,
                    "reason": reason,
                    "generator": self.revision_generator_name,
                    "generator_version": self.revision_generator_version,
                }
            )
            session.add(
                GrowthSource(
                    tenant_id=context.tenant_id,
                    subject_type="skill_version",
                    skill_version_id=revised.id,
                    run_id=source.run_id,
                    run_step_id=source.run_step_id,
                    tool_call_id=source.tool_call_id,
                    runtime_session_id=source.runtime_session_id,
                    agent_version_id=source.agent_version_id,
                    trajectory_hash=source.trajectory_hash,
                    generator_name=self.revision_generator_name,
                    generator_version=self.revision_generator_version,
                    source_hash=source_hash,
                    snapshot={
                        "candidate_key": source_hash,
                        "policy_hash": source.snapshot.get("policy_hash"),
                        "trajectory": source.snapshot.get("trajectory"),
                        "revision": {
                            "parent_skill_version_id": str(base.id),
                            "parent_source_hash": source.source_hash,
                            "reason": reason,
                            "actor": context.actor_id,
                        },
                    },
                )
            )
            skill.revision += 1
            self._record_skill(
                session,
                context,
                skill,
                event_type="SkillVersionDraftCreated",
                action="skill.version.revise",
                details={
                    "skill_version_id": str(revised.id),
                    "version": revised.version,
                    "base_version_id": str(base.id),
                    "content_hash": revised.content_hash,
                    "reason": reason,
                },
            )
            await session.flush()
            return self._version_reference(skill, revised)

    async def publish_version(
        self,
        context: TenantContext,
        skill_id: UUID,
        skill_version_id: UUID,
        *,
        expected_skill_revision: int,
        expected_version_revision: int,
    ) -> SkillVersionReference:
        now = datetime.now(UTC)
        async with self.database.tenant_transaction(context) as session:
            skill = await self._lock_skill(session, skill_id)
            version = await self._lock_version(session, skill_id, skill_version_id)
            if version.status == "published":
                return self._version_reference(skill, version, already_in_state=True)
            require_revision("skill", expected=expected_skill_revision, actual=skill.revision)
            require_revision(
                "skill_version", expected=expected_version_revision, actual=version.revision
            )
            if skill.status == "disabled":
                raise DomainConflict("SKILL_DISABLED", "a disabled skill cannot be published")
            if version.status not in {"draft", "testing"}:
                raise DomainConflict(
                    "SKILL_VERSION_NOT_PUBLISHABLE",
                    "only a draft or testing skill version can be published",
                    details={"skill_version_id": str(version.id), "status": version.status},
                )
            latest = await session.scalar(
                select(SkillVersion)
                .where(SkillVersion.skill_id == skill.id)
                .order_by(SkillVersion.version.desc(), SkillVersion.id.desc())
                .limit(1)
                .with_for_update()
            )
            if latest is None or latest.id != version.id:
                raise DomainConflict(
                    "SKILL_VERSION_NOT_LATEST",
                    "only the latest draft skill version can be published",
                    details={"skill_version_id": str(version.id)},
                )
            if skill_content_hash(version) != version.content_hash:
                raise DomainConflict(
                    "SKILL_CONTENT_HASH_MISMATCH",
                    "skill version content does not match its immutable hash",
                )
            source = await session.scalar(
                select(GrowthSource)
                .where(GrowthSource.skill_version_id == version.id)
                .order_by(GrowthSource.created_at, GrowthSource.id)
            )
            if source is None:
                raise DomainConflict(
                    "SKILL_SOURCE_MISSING",
                    "skill publication requires an immutable growth source",
                )
            evaluation = await GrowthApprovalService.latest_evaluation(
                session, "skill_version", version.id, version.content_hash
            )
            if evaluation is None or not (
                evaluation.status == "completed" and evaluation.verdict == "pass"
            ):
                raise DomainConflict(
                    "GROWTH_EVALUATION_NOT_PASSED",
                    "latest evaluation must pass before skill publication",
                )
            current_subject = await ValidationSubjectBuilder(self.database).build_in_session(
                session, "skill_version", version.id
            )
            if evaluation.details.get("subject_snapshot_hash") != current_subject.snapshot_hash:
                raise DomainConflict(
                    "GROWTH_EVALUATION_STALE",
                    "skill validation evidence no longer matches the current release context",
                    details={"skill_version_id": str(version.id)},
                )
            approval = await GrowthApprovalService.valid_approval(
                session,
                "skill_version",
                version.id,
                version.content_hash,
                now=now,
            )
            if approval is None or approval.reviewer is None:
                raise DomainConflict(
                    "GROWTH_APPROVAL_REQUIRED",
                    "skill publication requires an unexpired human approval",
                )
            if approval.requester == approval.reviewer:
                raise DomainConflict(
                    "GROWTH_APPROVAL_INVALID",
                    "self-reviewed approval cannot publish a skill version",
                )

            if version.status == "draft":
                version.status = "testing"
                version.evaluated_at = evaluation.ended_at or now
                version.revision += 1
                await session.flush()
            if skill.current_version_id is None and skill.status == "candidate":
                skill.status = "testing"
                skill.revision += 1
                await session.flush()

            version.status = "published"
            version.evaluated_at = version.evaluated_at or evaluation.ended_at or now
            version.approved_at = approval.decided_at or now
            version.published_at = now
            version.revision += 1
            await session.flush()

            initial_publication = skill.current_version_id is None
            if initial_publication:
                if skill.status == "testing":
                    skill.status = "approved"
                    skill.revision += 1
                    await session.flush()
                if skill.status != "approved":
                    raise DomainConflict(
                        "SKILL_NOT_PUBLISHABLE",
                        "initial skill publication requires an approved skill identity",
                        details={"skill_id": str(skill.id), "status": skill.status},
                    )
                skill.status = "published"
                skill.current_version_id = version.id
                skill.revision += 1
            elif skill.status not in {"published", "deprecated"}:
                raise DomainConflict(
                    "SKILL_NOT_VERSIONABLE",
                    "only a published or deprecated skill can publish another version",
                    details={"skill_id": str(skill.id), "status": skill.status},
                )

            self._record_skill(
                session,
                context,
                skill,
                event_type="SkillPublished" if initial_publication else "SkillVersionPublished",
                action="skill.version.publish",
                details={
                    "skill_version_id": str(version.id),
                    "version": version.version,
                    "content_hash": version.content_hash,
                    "evaluation_id": str(evaluation.id),
                    "approval_id": str(approval.id),
                    "source_id": str(source.id),
                    "initial_publication": initial_publication,
                },
            )
            await session.flush()
            return self._version_reference(skill, version)

    async def deploy_canary(
        self,
        context: TenantContext,
        skill_id: UUID,
        skill_version_id: UUID,
        *,
        scope_type: Literal["project", "agent"],
        scope_id: UUID,
        rollout_percentage: int,
        expected_skill_revision: int,
    ) -> SkillDeploymentReference:
        if not 1 <= rollout_percentage <= 99:
            raise ValueError("canary rollout must be between 1 and 99 percent")
        async with self.database.tenant_transaction(context) as session:
            skill = await self._lock_skill(session, skill_id)
            if skill.status != "published" or skill.current_version_id is None:
                raise DomainConflict(
                    "SKILL_NOT_PUBLISHED",
                    "canary deployment requires a published stable skill",
                )
            version = await self._get_version(session, skill_id, skill_version_id)
            if version.status != "published":
                raise DomainConflict(
                    "SKILL_VERSION_NOT_PUBLISHED",
                    "canary deployment requires a published skill version",
                )
            if version.id == skill.current_version_id:
                raise DomainConflict(
                    "SKILL_CANARY_IS_STABLE",
                    "the stable skill version cannot be deployed as a canary",
                )
            await self._validate_deployment_scope(session, skill, scope_type, scope_id)
            predicate = (
                SkillDeployment.project_id == scope_id
                if scope_type == "project"
                else SkillDeployment.agent_id == scope_id
            )
            existing = await session.scalar(
                select(SkillDeployment)
                .where(
                    SkillDeployment.skill_id == skill.id,
                    SkillDeployment.scope_type == scope_type,
                    predicate,
                    SkillDeployment.status == "active",
                )
                .with_for_update()
            )
            if existing is not None:
                if (
                    existing.skill_version_id == version.id
                    and existing.rollout_percentage == rollout_percentage
                ):
                    return self._deployment_reference(
                        existing,
                        skill_revision=skill.revision,
                        already_in_state=True,
                    )
                raise DomainConflict(
                    "SKILL_DEPLOYMENT_OVERLAP",
                    "an active canary already exists for this skill and scope",
                    details={"deployment_id": str(existing.id)},
                )
            require_revision("skill", expected=expected_skill_revision, actual=skill.revision)
            deployment = SkillDeployment(
                tenant_id=context.tenant_id,
                skill_id=skill.id,
                skill_version_id=version.id,
                scope_type=scope_type,
                project_id=scope_id if scope_type == "project" else None,
                agent_id=scope_id if scope_type == "agent" else None,
                rollout_percentage=rollout_percentage,
                status="active",
                created_by=context.actor_id,
            )
            session.add(deployment)
            skill.revision += 1
            await session.flush()
            self._record_deployment(
                session,
                context,
                deployment,
                event_type="SkillCanaryDeployed",
                action="skill.canary.deploy",
                reason=None,
            )
            return self._deployment_reference(
                deployment,
                skill_revision=skill.revision,
            )

    async def retire_canary(
        self,
        context: TenantContext,
        deployment_id: UUID,
        *,
        reason: str,
        expected_revision: int,
    ) -> SkillDeploymentReference:
        reason = self._reason(reason)
        async with self.database.tenant_transaction(context) as session:
            seed = await session.scalar(
                select(SkillDeployment).where(SkillDeployment.id == deployment_id)
            )
            if seed is None:
                raise ResourceNotFound("skill_deployment", str(deployment_id))
            skill = await self._lock_skill(session, seed.skill_id)
            deployment = await session.scalar(
                select(SkillDeployment).where(SkillDeployment.id == deployment_id).with_for_update()
            )
            if deployment is None:
                raise ResourceNotFound("skill_deployment", str(deployment_id))
            if deployment.status == "retired":
                return self._deployment_reference(
                    deployment,
                    skill_revision=skill.revision,
                    already_in_state=True,
                )
            require_revision(
                "skill_deployment", expected=expected_revision, actual=deployment.revision
            )
            self._retire_deployment(session, context, deployment, reason)
            skill.revision += 1
            await session.flush()
            return self._deployment_reference(
                deployment,
                skill_revision=skill.revision,
            )

    async def promote(
        self,
        context: TenantContext,
        skill_id: UUID,
        skill_version_id: UUID,
        *,
        reason: str,
        expected_skill_revision: int,
    ) -> SkillVersionReference:
        return await self._switch_stable(
            context,
            skill_id,
            skill_version_id,
            reason=reason,
            expected_skill_revision=expected_skill_revision,
            action="promote",
        )

    async def rollback(
        self,
        context: TenantContext,
        skill_id: UUID,
        skill_version_id: UUID,
        *,
        reason: str,
        expected_skill_revision: int,
    ) -> SkillVersionReference:
        return await self._switch_stable(
            context,
            skill_id,
            skill_version_id,
            reason=reason,
            expected_skill_revision=expected_skill_revision,
            action="rollback",
        )

    async def deprecate(
        self,
        context: TenantContext,
        skill_id: UUID,
        *,
        reason: str,
        expected_skill_revision: int,
    ) -> SkillVersionReference:
        return await self._stop_skill(
            context,
            skill_id,
            target="deprecated",
            reason=reason,
            expected_skill_revision=expected_skill_revision,
        )

    async def disable(
        self,
        context: TenantContext,
        skill_id: UUID,
        *,
        reason: str,
        expected_skill_revision: int,
    ) -> SkillVersionReference:
        return await self._stop_skill(
            context,
            skill_id,
            target="disabled",
            reason=reason,
            expected_skill_revision=expected_skill_revision,
        )

    async def resolve(
        self,
        context: TenantContext,
        skill_id: UUID,
        run_id: UUID,
    ) -> SkillResolution:
        async with self.database.tenant_transaction(context) as session:
            return await self.resolve_in_session(session, skill_id, run_id)

    async def resolve_in_session(
        self,
        session: AsyncSession,
        skill_id: UUID,
        run_id: UUID,
    ) -> SkillResolution:
        skill = await self._get_skill(session, skill_id)
        if skill.status != "published" or skill.current_version_id is None:
            raise DomainConflict(
                "SKILL_NOT_RESOLVABLE",
                "only a published skill can be resolved for execution",
                details={"skill_id": str(skill_id), "status": skill.status},
            )
        row = (
            await session.execute(
                select(Run, Task).join(Task, Task.id == Run.task_id).where(Run.id == run_id)
            )
        ).one_or_none()
        if row is None:
            raise ResourceNotFound("run", str(run_id))
        run, task = row
        self._authorize_run_scope(skill, task.project_id, run.agent_id)
        return await self._resolve_for_run(session, skill, run, task)

    async def resolve_available_in_session(
        self,
        session: AsyncSession,
        run: Run,
        task: Task,
        *,
        allowed_skill_ids: frozenset[UUID],
        allowed_scope_types: frozenset[str],
        limit: int,
    ) -> tuple[tuple[Skill, SkillVersion, SkillResolution], ...]:
        """Resolve an explicit, scope-bounded published Skill allowlist for one Run."""

        if not 1 <= limit <= 100:
            raise ValueError("Skill runtime limit must be between 1 and 100")
        if not allowed_scope_types <= {"tenant", "project", "agent"}:
            raise ValueError("Skill runtime scopes contain unsupported values")
        if not allowed_skill_ids or not allowed_scope_types:
            return ()
        skills = list(
            await session.scalars(
                select(Skill)
                .where(
                    Skill.id.in_(allowed_skill_ids),
                    Skill.status == "published",
                    Skill.current_version_id.is_not(None),
                    Skill.scope_type.in_(allowed_scope_types),
                )
                .order_by(
                    case(
                        (Skill.scope_type == "agent", 0),
                        (Skill.scope_type == "project", 1),
                        else_=2,
                    ),
                    Skill.name,
                    Skill.id,
                )
            )
        )
        resolved: list[tuple[Skill, SkillVersion, SkillResolution]] = []
        for skill in skills:
            if not self._run_scope_matches(skill, task.project_id, run.agent_id):
                continue
            resolution = await self._resolve_for_run(session, skill, run, task)
            version = await self._get_version(session, skill.id, resolution.skill_version_id)
            resolved.append((skill, version, resolution))
            if len(resolved) >= limit:
                break
        return tuple(resolved)

    async def _resolve_for_run(
        self,
        session: AsyncSession,
        skill: Skill,
        run: Run,
        task: Task,
    ) -> SkillResolution:
        if skill.current_version_id is None:
            raise DomainConflict("SKILL_STABLE_VERSION_MISSING", "skill has no stable version")
        deployment = await session.scalar(
            select(SkillDeployment)
            .where(
                SkillDeployment.skill_id == skill.id,
                SkillDeployment.status == "active",
                or_(
                    and_(
                        SkillDeployment.scope_type == "agent",
                        SkillDeployment.agent_id == run.agent_id,
                    ),
                    and_(
                        SkillDeployment.scope_type == "project",
                        SkillDeployment.project_id == task.project_id,
                    ),
                ),
            )
            .order_by(
                case((SkillDeployment.scope_type == "agent", 0), else_=1),
                SkillDeployment.id,
            )
        )
        selected_id = skill.current_version_id
        selection: Literal["stable", "canary"] = "stable"
        bucket: int | None = None
        if deployment is not None:
            bucket = stable_rollout_bucket(run.id, deployment.id)
            if bucket < deployment.rollout_percentage:
                selected_id = deployment.skill_version_id
                selection = "canary"
        version = await self._get_version(session, skill.id, selected_id)
        if version.status != "published":
            raise DomainConflict(
                "SKILL_POINTER_INVALID",
                "resolved skill pointer does not reference a published version",
            )
        return SkillResolution(
            skill_id=skill.id,
            skill_version_id=version.id,
            version=version.version,
            run_id=run.id,
            selection=selection,
            deployment_id=deployment.id if deployment is not None else None,
            rollout_percentage=deployment.rollout_percentage if deployment is not None else None,
            bucket=bucket,
        )

    async def _switch_stable(
        self,
        context: TenantContext,
        skill_id: UUID,
        skill_version_id: UUID,
        *,
        reason: str,
        expected_skill_revision: int,
        action: Literal["promote", "rollback"],
    ) -> SkillVersionReference:
        reason = self._reason(reason)
        async with self.database.tenant_transaction(context) as session:
            skill = await self._lock_skill(session, skill_id)
            if skill.status == "disabled":
                raise DomainConflict("SKILL_DISABLED", "a disabled skill cannot change pointer")
            target = await self._get_version(session, skill_id, skill_version_id)
            if target.status != "published":
                raise DomainConflict(
                    "SKILL_VERSION_NOT_PUBLISHED",
                    "stable pointer can only select a published skill version",
                )
            active_deployments = await self._active_deployments(session, skill.id)
            if (
                skill.status == "published"
                and skill.current_version_id == target.id
                and not active_deployments
            ):
                return self._version_reference(skill, target, already_in_state=True)
            require_revision("skill", expected=expected_skill_revision, actual=skill.revision)
            if skill.current_version_id is None:
                raise DomainConflict(
                    "SKILL_STABLE_VERSION_MISSING",
                    "skill has no stable version to promote or roll back",
                )
            current = await self._get_version(session, skill.id, skill.current_version_id)
            if action == "promote" and target.version <= current.version:
                raise DomainConflict(
                    "SKILL_PROMOTION_NOT_FORWARD",
                    "promotion target must be newer than the stable version",
                )
            restoring_deprecated_current = (
                action == "rollback" and skill.status == "deprecated" and target.id == current.id
            )
            if (
                action == "rollback"
                and target.version >= current.version
                and not restoring_deprecated_current
            ):
                raise DomainConflict(
                    "SKILL_ROLLBACK_NOT_HISTORICAL",
                    "rollback target must be an older published version",
                )
            for deployment in active_deployments:
                self._retire_deployment(
                    session,
                    context,
                    deployment,
                    f"Retired during stable {action}: {reason}",
                )
            if active_deployments:
                await session.flush()
            if skill.status == "deprecated":
                skill.status = "published"
            elif skill.status != "published":
                raise DomainConflict(
                    "SKILL_NOT_SWITCHABLE",
                    "stable pointer requires a published or deprecated skill",
                )
            previous_id = skill.current_version_id
            skill.current_version_id = target.id
            skill.revision += 1
            self._record_skill(
                session,
                context,
                skill,
                event_type=(
                    "SkillVersionPromoted" if action == "promote" else "SkillVersionRolledBack"
                ),
                action=f"skill.version.{action}",
                details={
                    "previous_version_id": str(previous_id),
                    "skill_version_id": str(target.id),
                    "version": target.version,
                    "retired_deployments": len(active_deployments),
                    "reason": reason,
                },
            )
            await session.flush()
            return self._version_reference(skill, target)

    async def _stop_skill(
        self,
        context: TenantContext,
        skill_id: UUID,
        *,
        target: Literal["deprecated", "disabled"],
        reason: str,
        expected_skill_revision: int,
    ) -> SkillVersionReference:
        reason = self._reason(reason)
        async with self.database.tenant_transaction(context) as session:
            skill = await self._lock_skill(session, skill_id)
            if skill.status == target:
                version = await self._current_or_latest(session, skill)
                return self._version_reference(skill, version, already_in_state=True)
            require_revision("skill", expected=expected_skill_revision, actual=skill.revision)
            if target == "deprecated" and skill.status != "published":
                raise DomainConflict(
                    "INVALID_STATE_TRANSITION",
                    f"skill cannot transition from {skill.status} to deprecated",
                )
            if target == "disabled" and skill.status not in {
                "candidate",
                "testing",
                "approved",
                "published",
                "deprecated",
            }:
                raise DomainConflict(
                    "INVALID_STATE_TRANSITION",
                    f"skill cannot transition from {skill.status} to disabled",
                )
            deployments = await self._active_deployments(session, skill.id)
            for deployment in deployments:
                self._retire_deployment(
                    session,
                    context,
                    deployment,
                    f"Retired because skill became {target}: {reason}",
                )
            if deployments:
                await session.flush()
            skill.status = target
            skill.revision += 1
            version = await self._current_or_latest(session, skill)
            self._record_skill(
                session,
                context,
                skill,
                event_type="SkillDeprecated" if target == "deprecated" else "SkillDisabled",
                action=f"skill.{target}",
                details={
                    "skill_version_id": str(version.id),
                    "retired_deployments": len(deployments),
                    "reason": reason,
                },
            )
            await session.flush()
            return self._version_reference(skill, version)

    @staticmethod
    async def _get_skill(session: AsyncSession, skill_id: UUID) -> Skill:
        skill = await session.scalar(select(Skill).where(Skill.id == skill_id))
        if skill is None:
            raise ResourceNotFound("skill", str(skill_id))
        return skill

    @staticmethod
    async def _lock_skill(session: AsyncSession, skill_id: UUID) -> Skill:
        skill = await session.scalar(select(Skill).where(Skill.id == skill_id).with_for_update())
        if skill is None:
            raise ResourceNotFound("skill", str(skill_id))
        return skill

    @staticmethod
    async def _get_version(
        session: AsyncSession, skill_id: UUID, skill_version_id: UUID
    ) -> SkillVersion:
        version = await session.scalar(
            select(SkillVersion).where(
                SkillVersion.id == skill_version_id,
                SkillVersion.skill_id == skill_id,
            )
        )
        if version is None:
            raise ResourceNotFound("skill_version", str(skill_version_id))
        return version

    @staticmethod
    async def _lock_version(
        session: AsyncSession, skill_id: UUID, skill_version_id: UUID
    ) -> SkillVersion:
        version = await session.scalar(
            select(SkillVersion)
            .where(
                SkillVersion.id == skill_version_id,
                SkillVersion.skill_id == skill_id,
            )
            .with_for_update()
        )
        if version is None:
            raise ResourceNotFound("skill_version", str(skill_version_id))
        return version

    @staticmethod
    async def _active_deployments(session: AsyncSession, skill_id: UUID) -> list[SkillDeployment]:
        return list(
            (
                await session.scalars(
                    select(SkillDeployment)
                    .where(
                        SkillDeployment.skill_id == skill_id,
                        SkillDeployment.status == "active",
                    )
                    .order_by(SkillDeployment.id)
                    .with_for_update()
                )
            ).all()
        )

    @staticmethod
    async def _current_or_latest(session: AsyncSession, skill: Skill) -> SkillVersion:
        if skill.current_version_id is not None:
            return await SkillLifecycleService._get_version(
                session, skill.id, skill.current_version_id
            )
        version = await session.scalar(
            select(SkillVersion)
            .where(SkillVersion.skill_id == skill.id)
            .order_by(SkillVersion.version.desc(), SkillVersion.id.desc())
            .limit(1)
        )
        if version is None:
            raise DomainConflict("SKILL_VERSION_MISSING", "skill has no version")
        return version

    @staticmethod
    async def _validate_deployment_scope(
        session: AsyncSession,
        skill: Skill,
        scope_type: Literal["project", "agent"],
        scope_id: UUID,
    ) -> None:
        model = Project if scope_type == "project" else Agent
        owner = await session.scalar(select(model).where(model.id == scope_id))
        if owner is None:
            raise ResourceNotFound(scope_type, str(scope_id))
        allowed = (
            skill.scope_type == "tenant"
            or (
                skill.scope_type == "project"
                and scope_type == "project"
                and skill.project_id == scope_id
            )
            or (
                skill.scope_type == "agent" and scope_type == "agent" and skill.agent_id == scope_id
            )
        )
        if not allowed:
            raise AccessDenied(
                "SKILL_DEPLOYMENT_SCOPE_FORBIDDEN",
                "canary deployment cannot widen the owning skill scope",
            )

    @staticmethod
    def _authorize_run_scope(skill: Skill, project_id: UUID | None, agent_id: UUID) -> None:
        if not SkillLifecycleService._run_scope_matches(skill, project_id, agent_id):
            raise AccessDenied(
                "SKILL_SCOPE_FORBIDDEN",
                "run context is outside the published skill scope",
            )

    @staticmethod
    def _run_scope_matches(skill: Skill, project_id: UUID | None, agent_id: UUID) -> bool:
        return (
            skill.scope_type == "tenant"
            or (skill.scope_type == "project" and skill.project_id == project_id)
            or (skill.scope_type == "agent" and skill.agent_id == agent_id)
        )

    @staticmethod
    def _retire_deployment(
        session: AsyncSession,
        context: TenantContext,
        deployment: SkillDeployment,
        reason: str,
    ) -> None:
        deployment.status = "retired"
        deployment.retired_by = context.actor_id
        deployment.retired_at = datetime.now(UTC)
        deployment.revision += 1
        SkillLifecycleService._record_deployment(
            session,
            context,
            deployment,
            event_type="SkillCanaryRetired",
            action="skill.canary.retire",
            reason=reason,
        )

    @staticmethod
    def _reason(value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 10_000:
            raise ValueError("skill reason must contain between 1 and 10000 characters")
        return normalized

    @staticmethod
    def _version_reference(
        skill: Skill,
        version: SkillVersion,
        *,
        already_in_state: bool = False,
    ) -> SkillVersionReference:
        return SkillVersionReference(
            skill_id=skill.id,
            skill_version_id=version.id,
            version=version.version,
            skill_status=skill.status,
            version_status=version.status,
            content_hash=version.content_hash,
            skill_revision=skill.revision,
            version_revision=version.revision,
            current_version_id=skill.current_version_id,
            already_in_state=already_in_state,
        )

    @staticmethod
    def _deployment_reference(
        deployment: SkillDeployment,
        *,
        skill_revision: int,
        already_in_state: bool = False,
    ) -> SkillDeploymentReference:
        return SkillDeploymentReference(
            id=deployment.id,
            skill_id=deployment.skill_id,
            skill_version_id=deployment.skill_version_id,
            scope_type=deployment.scope_type,
            project_id=deployment.project_id,
            agent_id=deployment.agent_id,
            rollout_percentage=deployment.rollout_percentage,
            status=deployment.status,
            revision=deployment.revision,
            skill_revision=skill_revision,
            already_in_state=already_in_state,
        )

    @staticmethod
    def _record_skill(
        session: AsyncSession,
        context: TenantContext,
        skill: Skill,
        *,
        event_type: str,
        action: str,
        details: dict,
    ) -> None:
        payload = {
            "skill_id": str(skill.id),
            "skill_status": skill.status,
            "current_version_id": (
                str(skill.current_version_id) if skill.current_version_id else None
            ),
            **details,
        }
        session.add_all(
            [
                Event(
                    tenant_id=context.tenant_id,
                    event_type=event_type,
                    aggregate_type="skill",
                    aggregate_id=skill.id,
                    actor_id=context.actor_id,
                    payload=payload,
                    correlation_id=context.correlation_id,
                ),
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action=action,
                    resource_type="skill",
                    resource_id=skill.id,
                    actor_id=context.actor_id,
                    details=payload,
                    correlation_id=context.correlation_id,
                ),
            ]
        )

    @staticmethod
    def _record_deployment(
        session: AsyncSession,
        context: TenantContext,
        deployment: SkillDeployment,
        *,
        event_type: str,
        action: str,
        reason: str | None,
    ) -> None:
        payload = {
            "deployment_id": str(deployment.id),
            "skill_id": str(deployment.skill_id),
            "skill_version_id": str(deployment.skill_version_id),
            "scope_type": deployment.scope_type,
            "scope_id": str(deployment.project_id or deployment.agent_id),
            "rollout_percentage": deployment.rollout_percentage,
            "status": deployment.status,
            "reason": reason,
        }
        session.add_all(
            [
                Event(
                    tenant_id=context.tenant_id,
                    event_type=event_type,
                    aggregate_type="skill_deployment",
                    aggregate_id=deployment.id,
                    actor_id=context.actor_id,
                    payload=payload,
                    correlation_id=context.correlation_id,
                ),
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action=action,
                    resource_type="skill_deployment",
                    resource_id=deployment.id,
                    actor_id=context.actor_id,
                    details=payload,
                    correlation_id=context.correlation_id,
                ),
            ]
        )
