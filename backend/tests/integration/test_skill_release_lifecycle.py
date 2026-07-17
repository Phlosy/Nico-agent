from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nico_agent.config import Settings
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainError
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    Approval,
    AuditRecord,
    Event,
    Project,
    Run,
    RunStep,
    SkillDeployment,
    SkillVersion,
    Task,
    Tenant,
    ToolCall,
    ToolDefinition,
)
from nico_agent.growth import (
    GrowthApprovalService,
    GrowthCandidateService,
    GrowthEvaluationService,
)
from nico_agent.skills import SkillLifecycleService, SkillVersionDraft, stable_rollout_bucket

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with the Compose dependencies running",
)


def _database() -> tuple[Database, AsyncEngine]:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    return Database(engine), engine


def _context(scope: dict, actor: str) -> TenantContext:
    return TenantContext(scope["tenant_id"], actor, uuid4())


async def _seed_skill(database: Database, label: str) -> tuple[dict, object]:
    suffix = uuid4().hex[:10]
    async with database.admin_transaction() as session:
        tenant = Tenant(name=f"Skill {label} {suffix}", slug=f"skill-{label}-{suffix}")
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        other_project = Project(tenant_id=tenant.id, name=f"other-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Skill Release Agent",
        )
        session.add_all([project, other_project, agent])
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="skill-release-test",
            mandate="Produce reviewed reusable procedures",
            content_hash="a" * 64,
        )
        session.add(version)
        await session.flush()
        agent.current_version_id = version.id
        agent.status = "ready"
        task = Task(
            tenant_id=tenant.id,
            project_id=project.id,
            assignee_agent_id=agent.id,
            title="Release a controlled skill",
            input={"topic": "release governance"},
            acceptance={"requires_human_review": True},
            status="completed",
        )
        session.add(task)
        await session.flush()
        run = Run(
            tenant_id=tenant.id,
            task_id=task.id,
            agent_id=agent.id,
            agent_version_id=version.id,
            attempt=1,
            status="completed",
            result={"summary": "release only independently reviewed procedures"},
            ended_at=datetime.now(UTC),
        )
        session.add(run)
        await session.flush()
        step = RunStep(
            tenant_id=tenant.id,
            run_id=run.id,
            sequence=1,
            kind="result",
            status="completed",
            output={"lesson": "validate, approve, canary, then promote"},
            ended_at=datetime.now(UTC),
        )
        session.add(step)
        await session.flush()
        tool = ToolDefinition(
            tenant_id=tenant.id,
            name="research.search",
            version="1.0.0",
            status="enabled",
            description="Search bounded public research evidence",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            permission="network.search",
            timeout_seconds=5,
            retry_policy={"max_attempts": 1},
            isolation_policy={"kind": "gateway"},
            risk="low",
            max_output_bytes=4096,
            implementation_hash="b" * 64,
            content_hash="c" * 64,
            created_by="integration-test",
        )
        session.add(tool)
        await session.flush()
        session.add(
            ToolCall(
                tenant_id=tenant.id,
                run_id=run.id,
                run_step_id=step.id,
                tool_definition_id=tool.id,
                tool_name=tool.name,
                tool_version=tool.version,
                idempotency_key=f"skill-release-{suffix}",
                arguments_hash="d" * 64,
                caller="runtime:fixture",
                arguments={"query": "release evidence"},
                status="succeeded",
                result={"summary": "bounded evidence"},
                ended_at=datetime.now(UTC),
            )
        )
        await session.flush()
        scope = {
            "tenant_id": tenant.id,
            "project_id": project.id,
            "other_project_id": other_project.id,
            "agent_id": agent.id,
            "agent_version_id": version.id,
            "task_id": task.id,
            "run_id": run.id,
            "tool_definition_id": tool.id,
        }
    generated = await GrowthCandidateService(database).generate(
        _context(scope, "reflection:test"), run.id
    )
    assert generated.skill is not None
    return scope, generated.skill


async def _approve_and_publish(
    database: Database,
    scope: dict,
    skill_id: UUID,
    version_id: UUID,
    *,
    skill_revision: int,
    version_revision: int,
):
    evaluation = await GrowthEvaluationService(database).evaluate(
        _context(scope, "evaluator:caller"), "skill_version", version_id
    )
    assert evaluation.verdict == "pass"
    approval_service = GrowthApprovalService(database)
    requested = await approval_service.request(
        _context(scope, "operator:requester"),
        "skill_version",
        version_id,
        expected_revision=version_revision,
    )
    approved = await approval_service.decide(
        _context(scope, "operator:reviewer"),
        requested.id,
        decision="approved",
        reason="Schemas, exact tools, source and failure modes passed review.",
        expected_revision=requested.revision,
    )
    assert approved.status == "approved"
    return await SkillLifecycleService(database).publish_version(
        _context(scope, "operator:publisher"),
        skill_id,
        version_id,
        expected_skill_revision=skill_revision,
        expected_version_revision=version_revision,
    )


async def _draft_from(database: Database, context: TenantContext, version_id: UUID, label: str):
    async with database.tenant_transaction(context) as session:
        version = await session.get(SkillVersion, version_id)
        assert version is not None
        return SkillVersionDraft(
            conditions={"when": f"reviewed_{label}"},
            preconditions=tuple(version.preconditions),
            input_schema=version.input_schema,
            steps=tuple(
                [
                    *version.steps,
                    {
                        "id": f"release-{label}",
                        "action": "record_release_decision",
                    },
                ]
            ),
            tools=tuple(version.tools),
            output_schema=version.output_schema,
            validation={**version.validation, "release_label": label},
            failure_modes=tuple(version.failure_modes),
        )


async def _add_run(database: Database, scope: dict, run_id: UUID, attempt: int) -> None:
    async with database.admin_transaction() as session:
        session.add(
            Run(
                id=run_id,
                tenant_id=scope["tenant_id"],
                task_id=scope["task_id"],
                agent_id=scope["agent_id"],
                agent_version_id=scope["agent_version_id"],
                attempt=attempt,
                status="completed",
                result={"summary": "canary resolution fixture"},
                ended_at=datetime.now(UTC),
            )
        )


def _run_for_bucket(deployment_id: UUID, predicate) -> UUID:
    for _ in range(10_000):
        candidate = uuid4()
        if predicate(stable_rollout_bucket(candidate, deployment_id)):
            return candidate
    raise AssertionError("could not find deterministic rollout fixture")


@pytest.mark.asyncio
async def test_skill_release_canary_promotion_deprecation_rollback_and_disable() -> None:
    database, engine = _database()
    try:
        scope, candidate = await _seed_skill(database, "lifecycle")
        service = SkillLifecycleService(database)
        first = await _approve_and_publish(
            database,
            scope,
            candidate.skill_id,
            candidate.skill_version_id,
            skill_revision=1,
            version_revision=1,
        )
        assert first.skill_status == "published"
        assert first.version_status == "published"
        assert first.current_version_id == first.skill_version_id
        stable = await service.resolve(
            _context(scope, "runtime:resolver"), first.skill_id, scope["run_id"]
        )
        assert stable.selection == "stable" and stable.version == 1

        second_draft = await _draft_from(
            database,
            _context(scope, "operator:reader"),
            first.skill_version_id,
            "v2",
        )
        second = await service.revise(
            _context(scope, "operator:editor"),
            first.skill_id,
            first.skill_version_id,
            draft=second_draft,
            reason="Add an explicit release-decision step.",
            expected_skill_revision=first.skill_revision,
            expected_version_revision=first.version_revision,
        )
        comparison = await service.compare(
            _context(scope, "operator:reader"),
            first.skill_id,
            first.skill_version_id,
            second.skill_version_id,
        )
        assert comparison.direction == "upgrade"
        assert comparison.changed_fields == ("conditions", "steps", "validation")
        assert second.version_status == "draft"

        second = await _approve_and_publish(
            database,
            scope,
            second.skill_id,
            second.skill_version_id,
            skill_revision=second.skill_revision,
            version_revision=second.version_revision,
        )
        assert second.current_version_id == first.skill_version_id

        deployments = await asyncio.gather(
            service.deploy_canary(
                _context(scope, "operator:deployer-a"),
                second.skill_id,
                second.skill_version_id,
                scope_type="project",
                scope_id=scope["project_id"],
                rollout_percentage=50,
                expected_skill_revision=second.skill_revision,
            ),
            service.deploy_canary(
                _context(scope, "operator:deployer-b"),
                second.skill_id,
                second.skill_version_id,
                scope_type="project",
                scope_id=scope["project_id"],
                rollout_percentage=50,
                expected_skill_revision=second.skill_revision,
            ),
        )
        assert deployments[0].id == deployments[1].id
        assert sum(item.already_in_state for item in deployments) == 1
        deployment = max(deployments, key=lambda item: item.skill_revision)

        hit_id = _run_for_bucket(deployment.id, lambda bucket: bucket < 50)
        miss_id = _run_for_bucket(deployment.id, lambda bucket: bucket >= 50)
        await _add_run(database, scope, hit_id, 2)
        await _add_run(database, scope, miss_id, 3)
        hit = await service.resolve(_context(scope, "runtime:resolver"), first.skill_id, hit_id)
        miss = await service.resolve(_context(scope, "runtime:resolver"), first.skill_id, miss_id)
        assert hit.selection == "canary" and hit.skill_version_id == second.skill_version_id
        assert hit.bucket is not None and hit.bucket < 50
        assert miss.selection == "stable" and miss.skill_version_id == first.skill_version_id
        assert miss.bucket is not None and miss.bucket >= 50

        promoted = await service.promote(
            _context(scope, "operator:publisher"),
            second.skill_id,
            second.skill_version_id,
            reason="Canary evidence met the promotion threshold.",
            expected_skill_revision=deployment.skill_revision,
        )
        assert promoted.current_version_id == second.skill_version_id
        after_promotion = await service.resolve(
            _context(scope, "runtime:resolver"), first.skill_id, miss_id
        )
        assert after_promotion.selection == "stable"
        assert after_promotion.skill_version_id == second.skill_version_id

        third_draft = await _draft_from(
            database,
            _context(scope, "operator:reader"),
            second.skill_version_id,
            "v3",
        )
        third = await service.revise(
            _context(scope, "operator:editor"),
            second.skill_id,
            second.skill_version_id,
            draft=third_draft,
            reason="Create a third immutable release candidate.",
            expected_skill_revision=promoted.skill_revision,
            expected_version_revision=second.version_revision,
        )
        third = await _approve_and_publish(
            database,
            scope,
            third.skill_id,
            third.skill_version_id,
            skill_revision=third.skill_revision,
            version_revision=third.version_revision,
        )
        third = await service.promote(
            _context(scope, "operator:publisher"),
            third.skill_id,
            third.skill_version_id,
            reason="Promote the newer reviewed version.",
            expected_skill_revision=third.skill_revision,
        )
        deprecated = await service.deprecate(
            _context(scope, "operator:publisher"),
            third.skill_id,
            reason="Pause new execution while investigating regression evidence.",
            expected_skill_revision=third.skill_revision,
        )
        with pytest.raises(DomainError) as not_resolvable:
            await service.resolve(
                _context(scope, "runtime:resolver"), third.skill_id, scope["run_id"]
            )
        assert not_resolvable.value.code == "SKILL_NOT_RESOLVABLE"

        rolled_back = await service.rollback(
            _context(scope, "operator:publisher"),
            third.skill_id,
            first.skill_version_id,
            reason="Restore the original independently reviewed version.",
            expected_skill_revision=deprecated.skill_revision,
        )
        assert rolled_back.skill_status == "published"
        assert rolled_back.current_version_id == first.skill_version_id
        disabled = await service.disable(
            _context(scope, "operator:publisher"),
            third.skill_id,
            reason="Disable execution after the controlled rollback test.",
            expected_skill_revision=rolled_back.skill_revision,
        )
        assert disabled.skill_status == "disabled"
        with pytest.raises(DomainError) as disabled_resolution:
            await service.resolve(
                _context(scope, "runtime:resolver"), third.skill_id, scope["run_id"]
            )
        assert disabled_resolution.value.code == "SKILL_NOT_RESOLVABLE"

        async with database.tenant_transaction(_context(scope, "auditor")) as session:
            active_deployments = await session.scalar(
                select(func.count())
                .select_from(SkillDeployment)
                .where(
                    SkillDeployment.skill_id == third.skill_id,
                    SkillDeployment.status == "active",
                )
            )
            events = await session.scalar(
                select(func.count()).select_from(Event).where(Event.aggregate_id == third.skill_id)
            )
            audits = await session.scalar(
                select(func.count())
                .select_from(AuditRecord)
                .where(AuditRecord.resource_id == third.skill_id)
            )
        assert active_deployments == 0
        assert events and events >= 8
        assert audits and audits >= 8
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_skill_release_scope_revision_and_tenant_boundaries_fail_closed() -> None:
    database, engine = _database()
    try:
        scope, candidate = await _seed_skill(database, "boundaries")
        service = SkillLifecycleService(database)
        first = await _approve_and_publish(
            database,
            scope,
            candidate.skill_id,
            candidate.skill_version_id,
            skill_revision=1,
            version_revision=1,
        )
        draft = await _draft_from(
            database,
            _context(scope, "operator:reader"),
            first.skill_version_id,
            "boundary",
        )
        second = await service.revise(
            _context(scope, "operator:editor"),
            first.skill_id,
            first.skill_version_id,
            draft=draft,
            reason="Create a version for boundary checks.",
            expected_skill_revision=first.skill_revision,
            expected_version_revision=first.version_revision,
        )
        second = await _approve_and_publish(
            database,
            scope,
            second.skill_id,
            second.skill_version_id,
            skill_revision=second.skill_revision,
            version_revision=second.version_revision,
        )
        with pytest.raises(DomainError) as scope_denied:
            await service.deploy_canary(
                _context(scope, "operator:deployer"),
                second.skill_id,
                second.skill_version_id,
                scope_type="project",
                scope_id=scope["other_project_id"],
                rollout_percentage=10,
                expected_skill_revision=second.skill_revision,
            )
        assert scope_denied.value.code == "SKILL_DEPLOYMENT_SCOPE_FORBIDDEN"

        with pytest.raises(DomainError) as stale:
            await service.revise(
                _context(scope, "operator:editor"),
                first.skill_id,
                first.skill_version_id,
                draft=draft,
                reason="Attempt a stale version branch.",
                expected_skill_revision=second.skill_revision,
                expected_version_revision=first.version_revision,
            )
        assert stale.value.code == "SKILL_VERSION_REVISION_STALE"

        deprecated = await service.deprecate(
            _context(scope, "operator:publisher"),
            first.skill_id,
            reason="Exercise recovery when only the current stable pointer is selected.",
            expected_skill_revision=second.skill_revision,
        )
        restored = await service.rollback(
            _context(scope, "operator:publisher"),
            first.skill_id,
            first.skill_version_id,
            reason="Restore the same historical stable version after deprecation.",
            expected_skill_revision=deprecated.skill_revision,
        )
        assert restored.skill_status == "published"
        assert restored.current_version_id == first.skill_version_id

        other_scope, _ = await _seed_skill(database, "other-tenant")
        with pytest.raises(DomainError) as hidden:
            await service.compare(
                _context(other_scope, "operator:reader"),
                first.skill_id,
                first.skill_version_id,
                second.skill_version_id,
            )
        assert hidden.value.code == "RESOURCE_NOT_FOUND"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_skill_publication_rejects_stale_tool_validation_snapshot() -> None:
    database, engine = _database()
    try:
        scope, candidate = await _seed_skill(database, "stale-tool")
        evaluation = await GrowthEvaluationService(database).evaluate(
            _context(scope, "evaluator:caller"),
            "skill_version",
            candidate.skill_version_id,
        )
        assert evaluation.verdict == "pass"
        approvals = GrowthApprovalService(database)
        requested = await approvals.request(
            _context(scope, "operator:requester"),
            "skill_version",
            candidate.skill_version_id,
            expected_revision=1,
        )
        await approvals.decide(
            _context(scope, "operator:reviewer"),
            requested.id,
            decision="approved",
            reason="The exact enabled tool passed independent review.",
            expected_revision=requested.revision,
        )

        async with database.admin_transaction() as session:
            tool = await session.get(ToolDefinition, scope["tool_definition_id"])
            assert tool is not None
            tool.status = "disabled"
            tool.revision += 1
            await session.flush()

        with pytest.raises(DomainError) as stale:
            await SkillLifecycleService(database).publish_version(
                _context(scope, "operator:publisher"),
                candidate.skill_id,
                candidate.skill_version_id,
                expected_skill_revision=1,
                expected_version_revision=1,
            )
        assert stale.value.code == "GROWTH_EVALUATION_STALE"

        async with database.tenant_transaction(_context(scope, "operator:reader")) as session:
            skill = await session.get(SkillVersion, candidate.skill_version_id)
        assert skill is not None and skill.status == "draft"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_database_rejects_self_review_publish_and_stopping_with_active_canary() -> None:
    database, engine = _database()
    try:
        scope, candidate = await _seed_skill(database, "database-guards")
        await GrowthEvaluationService(database).evaluate(
            _context(scope, "evaluator:caller"),
            "skill_version",
            candidate.skill_version_id,
        )
        async with database.admin_transaction() as session:
            approval = Approval(
                tenant_id=scope["tenant_id"],
                subject_type="skill_version",
                skill_version_id=candidate.skill_version_id,
                action="publish",
                content_hash=candidate.content_hash,
                status="requested",
                requester="operator:self",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            session.add(approval)
            await session.flush()
            approval.status = "approved"
            approval.reviewer = "operator:self"
            approval.reason = "Invalid self review."
            approval.decided_at = datetime.now(UTC)
            await session.flush()

        with pytest.raises(DBAPIError):
            async with database.admin_transaction() as session:
                version = await session.get(SkillVersion, candidate.skill_version_id)
                assert version is not None
                version.status = "testing"
                await session.flush()
                version.status = "published"
                version.approved_at = version.published_at = datetime.now(UTC)
                await session.flush()

        release_scope, release_candidate = await _seed_skill(database, "database-stop-guard")
        published = await _approve_and_publish(
            database,
            release_scope,
            release_candidate.skill_id,
            release_candidate.skill_version_id,
            skill_revision=1,
            version_revision=1,
        )
        draft = await _draft_from(
            database,
            _context(release_scope, "operator:reader"),
            published.skill_version_id,
            "guard-canary",
        )
        canary_version = await SkillLifecycleService(database).revise(
            _context(release_scope, "operator:editor"),
            published.skill_id,
            published.skill_version_id,
            draft=draft,
            reason="Create a database guard canary.",
            expected_skill_revision=published.skill_revision,
            expected_version_revision=published.version_revision,
        )
        canary_version = await _approve_and_publish(
            database,
            release_scope,
            canary_version.skill_id,
            canary_version.skill_version_id,
            skill_revision=canary_version.skill_revision,
            version_revision=canary_version.version_revision,
        )
        deployment = await SkillLifecycleService(database).deploy_canary(
            _context(release_scope, "operator:deployer"),
            canary_version.skill_id,
            canary_version.skill_version_id,
            scope_type="project",
            scope_id=release_scope["project_id"],
            rollout_percentage=25,
            expected_skill_revision=canary_version.skill_revision,
        )
        assert deployment.status == "active"

        with pytest.raises(DBAPIError):
            async with database.admin_transaction() as session:
                await session.execute(
                    text("UPDATE skills SET current_version_id = :version_id WHERE id = :skill_id"),
                    {
                        "skill_id": published.skill_id,
                        "version_id": canary_version.skill_version_id,
                    },
                )
        with pytest.raises(DBAPIError):
            async with database.admin_transaction() as session:
                await session.execute(
                    text("UPDATE skills SET status = 'disabled' WHERE id = :skill_id"),
                    {"skill_id": published.skill_id},
                )
        with pytest.raises(DBAPIError):
            async with database.admin_transaction() as session:
                session.add(
                    SkillDeployment(
                        tenant_id=release_scope["tenant_id"],
                        skill_id=published.skill_id,
                        skill_version_id=canary_version.skill_version_id,
                        scope_type="project",
                        project_id=release_scope["other_project_id"],
                        rollout_percentage=10,
                        created_by="operator:direct-sql",
                    )
                )
                await session.flush()
    finally:
        await engine.dispose()
