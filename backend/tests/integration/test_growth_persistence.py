from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings
from nico_agent.database import Database, TenantContext
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    Approval,
    Evaluation,
    GrowthSource,
    Memory,
    Project,
    Run,
    RunStep,
    Skill,
    SkillDeployment,
    SkillVersion,
    Task,
    Tenant,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with the Compose dependencies running",
)


async def _seed_terminal_scope(database: Database, label: str) -> dict:
    suffix = uuid4().hex[:10]
    async with database.admin_transaction() as session:
        tenant = Tenant(name=f"Growth {label} {suffix}", slug=f"growth-{label}-{suffix}")
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Growth Agent",
        )
        session.add_all([project, agent])
        await session.flush()
        agent_version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="growth-test",
            mandate="Produce a controlled growth source",
            content_hash="a" * 64,
        )
        session.add(agent_version)
        await session.flush()
        agent.current_version_id = agent_version.id
        agent.status = "ready"
        task = Task(
            tenant_id=tenant.id,
            project_id=project.id,
            assignee_agent_id=agent.id,
            title="Growth source",
            status="completed",
        )
        session.add(task)
        await session.flush()
        run = Run(
            tenant_id=tenant.id,
            task_id=task.id,
            agent_id=agent.id,
            agent_version_id=agent_version.id,
            attempt=1,
            status="completed",
            result={"summary": "terminal source"},
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
            output={"lesson": "verify before publishing"},
            ended_at=datetime.now(UTC),
        )
        session.add(step)
        await session.flush()
        return {
            "tenant_id": tenant.id,
            "project_id": project.id,
            "agent_id": agent.id,
            "agent_version_id": agent_version.id,
            "run_id": run.id,
            "step_id": step.id,
        }


def _memory(scope: dict, *, content_hash: str = "b" * 64) -> Memory:
    return Memory(
        tenant_id=scope["tenant_id"],
        version=1,
        memory_type="semantic",
        scope_type="project",
        project_id=scope["project_id"],
        content="A candidate must be evaluated and approved before recall.",
        confidence=0.85,
        content_hash=content_hash,
        created_by="reflection:deterministic-v1",
    )


def _skill(scope: dict) -> tuple[Skill, SkillVersion]:
    skill = Skill(
        tenant_id=scope["tenant_id"],
        name=f"verify-before-publish-{uuid4().hex[:8]}",
        description="Validate and approve a candidate before publication.",
        scope_type="project",
        project_id=scope["project_id"],
        created_by="reflection:deterministic-v1",
    )
    version = SkillVersion(
        tenant_id=scope["tenant_id"],
        skill_id=skill.id,
        version=1,
        conditions={"when": "candidate_ready"},
        preconditions=[{"kind": "terminal_source"}],
        input_schema={"type": "object"},
        steps=[{"id": "validate", "action": "evaluate"}],
        tools=[],
        output_schema={"type": "object"},
        validation={"minimum_score": 0.8},
        failure_modes=[{"code": "VALIDATION_FAILED"}],
        content_hash="c" * 64,
        created_by="reflection:deterministic-v1",
    )
    return skill, version


def _source(scope: dict, *, memory: Memory | None = None, version: SkillVersion | None = None):
    return GrowthSource(
        tenant_id=scope["tenant_id"],
        subject_type="memory" if memory is not None else "skill_version",
        memory_id=memory.id if memory is not None else None,
        skill_version_id=version.id if version is not None else None,
        run_id=scope["run_id"],
        run_step_id=scope["step_id"],
        agent_version_id=scope["agent_version_id"],
        trajectory_hash="d" * 64,
        generator_name="deterministic-reflection",
        generator_version="1.0.0",
        source_hash=uuid4().hex.ljust(64, "0"),
        snapshot={"run_status": "completed", "step_status": "completed"},
    )


async def _approve_subject(session, *, tenant_id, memory=None, version=None) -> None:
    content_hash = memory.content_hash if memory is not None else version.content_hash
    subject_type = "memory" if memory is not None else "skill_version"
    evaluation = Evaluation(
        tenant_id=tenant_id,
        subject_type=subject_type,
        memory_id=memory.id if memory is not None else None,
        skill_version_id=version.id if version is not None else None,
        evaluator_name="deterministic-validator",
        evaluator_version="1.0.0",
        content_hash=content_hash,
        created_by="validator:test",
        started_at=datetime.now(UTC),
    )
    approval = Approval(
        tenant_id=tenant_id,
        subject_type=subject_type,
        memory_id=memory.id if memory is not None else None,
        skill_version_id=version.id if version is not None else None,
        content_hash=content_hash,
        requester="reflection:test",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session.add_all([evaluation, approval])
    await session.flush()
    evaluation.status = "completed"
    evaluation.score = 0.95
    evaluation.verdict = "pass"
    evaluation.details = {"rules": ["source", "shape", "hash"]}
    evaluation.ended_at = datetime.now(UTC)
    approval.status = "approved"
    approval.reviewer = "operator:test"
    approval.reason = "Deterministic validation passed."
    approval.decided_at = datetime.now(UTC)
    await session.flush()


@pytest.mark.asyncio
async def test_growth_tables_are_force_rls_isolated() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        first = await _seed_terminal_scope(database, "rls-first")
        second = await _seed_terminal_scope(database, "rls-second")
        created: list[Memory] = []
        for scope in (first, second):
            async with database.admin_transaction() as session:
                memory = _memory(scope)
                session.add(memory)
                await session.flush()
                session.add(_source(scope, memory=memory))
                created.append(memory)

        for own, hidden in ((first, second), (second, first)):
            context = TenantContext(own["tenant_id"], "rls-test", uuid4())
            async with database.tenant_transaction(context) as session:
                memory_ids = set((await session.scalars(select(Memory.id))).all())
                source_ids = set((await session.scalars(select(GrowthSource.memory_id))).all())
            own_memory = next(item for item in created if item.tenant_id == own["tenant_id"])
            hidden_memory = next(item for item in created if item.tenant_id == hidden["tenant_id"])
            assert own_memory.id in memory_ids
            assert own_memory.id in source_ids
            assert hidden_memory.id not in memory_ids
            assert hidden_memory.id not in source_ids
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_scope_and_terminal_source_are_enforced_by_database() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        first = await _seed_terminal_scope(database, "scope-first")
        second = await _seed_terminal_scope(database, "scope-second")

        with pytest.raises(IntegrityError):
            async with database.admin_transaction() as session:
                memory = _memory(first)
                memory.project_id = second["project_id"]
                session.add(memory)
                await session.flush()

        with pytest.raises(IntegrityError):
            async with database.admin_transaction() as session:
                session.add(
                    Memory(
                        tenant_id=first["tenant_id"],
                        version=1,
                        memory_type="episodic",
                        scope_type="team",
                        content="Team scope is unavailable before Goal G.",
                        confidence=0.5,
                        content_hash="e" * 64,
                        created_by="test",
                    )
                )
                await session.flush()

        async with database.admin_transaction() as session:
            memory = _memory(first)
            session.add(memory)
            await session.flush()
            await session.execute(
                text("UPDATE runs SET status = 'running' WHERE id = :id"),
                {"id": first["run_id"]},
            )
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    session.add(_source(first, memory=memory))
                    await session.flush()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_publication_requires_matching_evaluation_and_approval() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        scope = await _seed_terminal_scope(database, "publication-gate")
        async with database.admin_transaction() as session:
            memory = _memory(scope)
            session.add(memory)
            await session.flush()
            session.add(_source(scope, memory=memory))
            await session.flush()
            memory_id = memory.id

        with pytest.raises(DBAPIError):
            async with database.admin_transaction() as session:
                memory = await session.get(Memory, memory_id)
                memory.status = "active"
                memory.approved_at = datetime.now(UTC)
                await session.flush()

        with pytest.raises(DBAPIError):
            async with database.admin_transaction() as session:
                session.add(
                    Evaluation(
                        tenant_id=scope["tenant_id"],
                        subject_type="memory",
                        memory_id=memory_id,
                        evaluator_name="validator",
                        evaluator_version="1",
                        content_hash="f" * 64,
                        created_by="test",
                    )
                )
                await session.flush()

        with pytest.raises(DBAPIError):
            async with database.admin_transaction() as session:
                direct = _memory(scope)
                direct.status = "active"
                direct.approved_at = datetime.now(UTC)
                session.add(direct)
                await session.flush()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_valid_memory_and_skill_growth_chain_and_canary() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        scope = await _seed_terminal_scope(database, "valid-chain")
        async with database.admin_transaction() as session:
            memory = _memory(scope)
            skill, version = _skill(scope)
            session.add_all([memory, skill])
            await session.flush()
            version.skill_id = skill.id
            session.add(version)
            await session.flush()
            session.add_all([_source(scope, memory=memory), _source(scope, version=version)])
            await session.flush()

            skill.status = "testing"
            version.status = "testing"
            await session.flush()
            await _approve_subject(session, tenant_id=scope["tenant_id"], memory=memory)
            await _approve_subject(session, tenant_id=scope["tenant_id"], version=version)

            memory.status = "active"
            memory.approved_at = datetime.now(UTC)
            version.status = "published"
            version.evaluated_at = datetime.now(UTC)
            version.approved_at = datetime.now(UTC)
            version.published_at = datetime.now(UTC)
            await session.flush()
            skill.status = "approved"
            await session.flush()
            skill.status = "published"
            skill.current_version_id = version.id
            await session.flush()

            deployment = SkillDeployment(
                tenant_id=scope["tenant_id"],
                skill_id=skill.id,
                skill_version_id=version.id,
                scope_type="project",
                project_id=scope["project_id"],
                rollout_percentage=10,
                created_by="operator:test",
            )
            session.add(deployment)
            await session.flush()

            assert memory.status == "active"
            assert skill.current_version_id == version.id
            assert deployment.status == "active"

        with pytest.raises(DBAPIError):
            async with database.admin_transaction() as session:
                await session.execute(
                    text("UPDATE memories SET content = 'mutated' WHERE id = :id"),
                    {"id": memory.id},
                )
        with pytest.raises(DBAPIError):
            async with database.admin_transaction() as session:
                await session.execute(
                    text("DELETE FROM growth_sources WHERE memory_id = :id"),
                    {"id": memory.id},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_canary_overlap_and_terminal_records_are_immutable() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        scope = await _seed_terminal_scope(database, "canary-immutable")
        async with database.admin_transaction() as session:
            memory = _memory(scope)
            session.add(memory)
            await session.flush()
            session.add(_source(scope, memory=memory))
            await session.flush()
            await _approve_subject(session, tenant_id=scope["tenant_id"], memory=memory)
            evaluation = await session.scalar(
                select(Evaluation).where(Evaluation.memory_id == memory.id)
            )
            assert evaluation is not None

        with pytest.raises(DBAPIError):
            async with database.admin_transaction() as session:
                await session.execute(
                    text("UPDATE evaluations SET score = 0.1 WHERE id = :id"),
                    {"id": evaluation.id},
                )

        async with database.admin_transaction() as session:
            skill, version = _skill(scope)
            session.add(skill)
            await session.flush()
            version.skill_id = skill.id
            session.add(version)
            await session.flush()
            session.add(_source(scope, version=version))
            skill.status = "testing"
            version.status = "testing"
            await session.flush()
            await _approve_subject(session, tenant_id=scope["tenant_id"], version=version)
            version.status = "published"
            version.approved_at = version.published_at = datetime.now(UTC)
            await session.flush()
            first = SkillDeployment(
                tenant_id=scope["tenant_id"],
                skill_id=skill.id,
                skill_version_id=version.id,
                scope_type="project",
                project_id=scope["project_id"],
                rollout_percentage=20,
                created_by="operator:test",
            )
            session.add(first)
            await session.flush()

        with pytest.raises(IntegrityError):
            async with database.admin_transaction() as session:
                session.add(
                    SkillDeployment(
                        tenant_id=scope["tenant_id"],
                        skill_id=skill.id,
                        skill_version_id=version.id,
                        scope_type="project",
                        project_id=scope["project_id"],
                        rollout_percentage=30,
                        created_by="operator:test",
                    )
                )
                await session.flush()

        async with database.admin_transaction() as session:
            deployment = await session.get(SkillDeployment, first.id)
            deployment.status = "retired"
            deployment.retired_by = "operator:test"
            deployment.retired_at = datetime.now(UTC)
            await session.flush()

        with pytest.raises(DBAPIError):
            async with database.admin_transaction() as session:
                await session.execute(
                    text("UPDATE skill_deployments SET rollout_percentage = 40 WHERE id = :id"),
                    {"id": first.id},
                )
    finally:
        await engine.dispose()
