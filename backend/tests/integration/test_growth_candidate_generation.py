from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nico_agent.config import Settings
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainError
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    Approval,
    AuditRecord,
    Evaluation,
    Event,
    GrowthSource,
    Memory,
    Project,
    Run,
    RunStep,
    RuntimeSession,
    Skill,
    SkillVersion,
    Task,
    Tenant,
    ToolCall,
    ToolDefinition,
)
from nico_agent.growth import GrowthCandidateService, GrowthPolicy
from nico_agent.memory.service import MemoryIndexService

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with the Compose dependencies running",
)


async def _seed_scope(
    database: Database,
    label: str,
    *,
    run_status: str = "completed",
    step_status: str = "completed",
) -> dict:
    suffix = uuid4().hex[:10]
    ended_at = datetime.now(UTC) if run_status in _terminal_runs() else None
    async with database.admin_transaction() as session:
        tenant = Tenant(name=f"Candidate {label} {suffix}", slug=f"candidate-{label}-{suffix}")
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Candidate Agent",
        )
        session.add_all([project, agent])
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="growth-test",
            mandate="Produce reviewable growth candidates; token=mandate-secret",
            model_config_json={"model": "fixture", "api_key": "model-secret"},
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
            title="Verify before publishing",
            input={"topic": "growth", "password": "task-secret"},
            acceptance={"minimum_score": 0.8},
            status=_task_status(run_status),
        )
        session.add(task)
        await session.flush()
        run = Run(
            tenant_id=tenant.id,
            task_id=task.id,
            agent_id=agent.id,
            agent_version_id=version.id,
            attempt=1,
            status=run_status,
            result={"summary": "terminal source"} if run_status == "completed" else None,
            error=(
                {"code": "SOURCE_FAILED", "message": "execution failed"}
                if run_status == "failed"
                else None
            ),
            ended_at=ended_at,
        )
        session.add(run)
        await session.flush()
        step = RunStep(
            tenant_id=tenant.id,
            run_id=run.id,
            sequence=1,
            kind="result",
            status=step_status,
            output=({"lesson": "verify before publishing"} if step_status == "completed" else None),
            error={"code": "STEP_FAILED"} if step_status == "failed" else None,
            ended_at=datetime.now(UTC) if step_status in _terminal_steps() else None,
        )
        session.add(step)
        await session.flush()
        return {
            "tenant_id": tenant.id,
            "project_id": project.id,
            "agent_id": agent.id,
            "agent_version_id": version.id,
            "run_id": run.id,
            "step_id": step.id,
            "ended_at": ended_at,
        }


def _terminal_runs() -> set[str]:
    return {"completed", "failed", "cancelled", "timed_out"}


def _terminal_steps() -> set[str]:
    return {"completed", "failed", "cancelled"}


def _task_status(run_status: str) -> str:
    if run_status in {"completed", "failed", "cancelled"}:
        return run_status
    return "running"


def _context(scope: dict, actor: str = "growth-test") -> TenantContext:
    return TenantContext(scope["tenant_id"], actor, uuid4())


def _database() -> tuple[Database, AsyncEngine]:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    return Database(engine), engine


@pytest.mark.asyncio
async def test_generation_is_concurrent_idempotent_inactive_and_audited() -> None:
    database, engine = _database()
    try:
        scope = await _seed_scope(database, "idempotent")
        context = _context(scope)
        service = GrowthCandidateService(database)

        first, second = await asyncio.gather(
            service.generate(context, scope["run_id"]),
            service.generate(context, scope["run_id"]),
        )
        generated = first if not first.already_generated else second
        repeated = second if not first.already_generated else first

        assert generated.already_generated is False
        assert repeated.already_generated is True
        assert {item.id for item in generated.memories} == {item.id for item in repeated.memories}
        assert {item.memory_type for item in generated.memories} == {
            "episodic",
            "semantic",
            "procedural",
        }
        assert generated.skill is not None
        assert repeated.skill is not None
        assert generated.skill.skill_version_id == repeated.skill.skill_version_id

        async with database.tenant_transaction(context) as session:
            memories = list(await session.scalars(select(Memory)))
            skills = list(await session.scalars(select(Skill)))
            versions = list(await session.scalars(select(SkillVersion)))
            sources = list(
                await session.scalars(
                    select(GrowthSource).where(GrowthSource.run_id == scope["run_id"])
                )
            )
            events = list(
                await session.scalars(select(Event).where(Event.run_id == scope["run_id"]))
            )
            resource_ids = {memory.id for memory in memories} | {version.id for version in versions}
            audits = list(
                await session.scalars(
                    select(AuditRecord).where(AuditRecord.resource_id.in_(resource_ids))
                )
            )
            evaluation_count = await session.scalar(select(func.count(Evaluation.id)))
            approval_count = await session.scalar(select(func.count(Approval.id)))

        assert len(memories) == 3
        assert all(item.status == "candidate" for item in memories)
        assert all(item.scope_type == "project" for item in memories)
        assert len(skills) == 1 and skills[0].status == "candidate"
        assert len(versions) == 1 and versions[0].status == "draft"
        assert len(sources) == 4
        assert {source.source_hash for source in sources} == {
            item.source_hash for item in generated.memories
        } | {generated.skill.source_hash}
        assert {event.event_type for event in events} == {
            "MemoryCandidateCreated",
            "SkillCandidateCreated",
        }
        assert len(events) == 4
        assert {audit.action for audit in audits} == {
            "memory.candidate.create",
            "skill.candidate.create",
        }
        assert len(audits) == 4
        assert evaluation_count == 0
        assert approval_count == 0

        with pytest.raises(DomainError) as inactive:
            await MemoryIndexService(database).index_memory(context, memories[0].id)
        assert inactive.value.code == "MEMORY_NOT_ACTIVE"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_snapshot_redacts_secrets_and_preserves_tool_runtime_provenance() -> None:
    database, engine = _database()
    try:
        scope = await _seed_scope(database, "provenance")
        async with database.admin_transaction() as session:
            definition = ToolDefinition(
                tenant_id=scope["tenant_id"],
                name="research.search",
                version="3.2.1",
                status="enabled",
                description="Search a bounded research source",
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
            session.add(definition)
            await session.flush()
            call = ToolCall(
                tenant_id=scope["tenant_id"],
                run_id=scope["run_id"],
                run_step_id=scope["step_id"],
                tool_definition_id=definition.id,
                tool_name=definition.name,
                tool_version=definition.version,
                idempotency_key="growth-provenance-call",
                arguments_hash="d" * 64,
                caller="runtime:fixture",
                arguments={
                    "api_key": "tool-argument-secret",
                    "query": "authorization=Bearer-deadbeef",
                },
                status="succeeded",
                result={"token": "tool-result-secret", "value": "safe evidence"},
                usage={"requests": 1},
                ended_at=datetime.now(UTC),
            )
            runtime = RuntimeSession(
                tenant_id=scope["tenant_id"],
                run_id=scope["run_id"],
                provider_name="hermes-adapter",
                provider_version="0.9.0",
                protocol_version="1.0",
                status="completed",
                usage={"input_tokens": 12},
                trajectory={"messages": [{"password": "runtime-secret", "content": "safe"}]},
                ended_at=datetime.now(UTC),
            )
            session.add_all([call, runtime])
            await session.flush()
            definition_id = definition.id
            call_id = call.id
            runtime_id = runtime.id

        context = _context(scope)
        result = await GrowthCandidateService(database).generate(context, scope["run_id"])
        assert result.skill is not None

        async with database.tenant_transaction(context) as session:
            sources = list(
                await session.scalars(
                    select(GrowthSource).where(GrowthSource.run_id == scope["run_id"])
                )
            )
            version = await session.get(SkillVersion, result.skill.skill_version_id)

        assert version is not None
        assert version.tools == [
            {
                "name": "research.search",
                "version": "3.2.1",
                "tool_definition_id": str(definition_id),
            }
        ]
        assert all(source.tool_call_id == call_id for source in sources)
        assert all(source.runtime_session_id == runtime_id for source in sources)
        trajectory = sources[0].snapshot["trajectory"]
        assert trajectory["runtime"]["provider"] == "hermes-adapter"
        assert trajectory["runtime"]["provider_version"] == "0.9.0"
        assert trajectory["steps"][0]["tool_calls"][0]["version"] == "3.2.1"
        serialized = json.dumps([source.snapshot for source in sources], sort_keys=True)
        assert "[REDACTED]" in serialized
        for secret in (
            "model-secret",
            "mandate-secret",
            "task-secret",
            "tool-argument-secret",
            "deadbeef",
            "tool-result-secret",
            "runtime-secret",
        ):
            assert secret not in serialized
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_run_produces_expiring_working_memory_and_no_skill() -> None:
    database, engine = _database()
    try:
        scope = await _seed_scope(
            database,
            "failed",
            run_status="failed",
            step_status="failed",
        )
        context = _context(scope)
        policy = GrowthPolicy(working_ttl_seconds=600)

        result = await GrowthCandidateService(database).generate(
            context, scope["run_id"], policy=policy
        )

        assert result.skill is None
        assert {item.memory_type for item in result.memories} == {"episodic", "working"}
        async with database.tenant_transaction(context) as session:
            memories = list(await session.scalars(select(Memory)))
            skill_count = await session.scalar(select(func.count(Skill.id)))
        working = next(item for item in memories if item.memory_type == "working")
        assert working.expires_at == scope["ended_at"] + timedelta(seconds=600)
        assert skill_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_policy_owns_scope_selection_not_the_reflection_provider() -> None:
    database, engine = _database()
    try:
        scope = await _seed_scope(database, "policy-scope")
        context = _context(scope)
        policy = GrowthPolicy(memory_scope="agent", skill_scope="tenant")

        result = await GrowthCandidateService(database).generate(
            context, scope["run_id"], policy=policy
        )
        assert result.skill is not None

        async with database.tenant_transaction(context) as session:
            memories = list(await session.scalars(select(Memory)))
            skill = await session.get(Skill, result.skill.skill_id)

        assert all(item.scope_type == "agent" for item in memories)
        assert all(item.agent_id == scope["agent_id"] for item in memories)
        assert all(item.project_id is None for item in memories)
        assert skill is not None
        assert skill.scope_type == "tenant"
        assert skill.project_id is None
        assert skill.agent_id is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_status", "step_status", "expected_code"),
    [
        ("running", "running", "RUN_NOT_TERMINAL"),
        ("completed", "running", "TRAJECTORY_INCOMPLETE"),
    ],
)
async def test_generation_rejects_nonterminal_or_incomplete_trajectory(
    run_status: str, step_status: str, expected_code: str
) -> None:
    database, engine = _database()
    try:
        scope = await _seed_scope(
            database,
            f"guard-{run_status}-{step_status}",
            run_status=run_status,
            step_status=step_status,
        )
        context = _context(scope)

        with pytest.raises(DomainError) as error:
            await GrowthCandidateService(database).generate(context, scope["run_id"])
        assert error.value.code == expected_code

        async with database.tenant_transaction(context) as session:
            assert await session.scalar(select(func.count(Memory.id))) == 0
            assert await session.scalar(select(func.count(Skill.id))) == 0
            assert await session.scalar(select(func.count(GrowthSource.id))) == 0

        if run_status == "running":
            async with database.admin_transaction() as session:
                run = await session.get(Run, scope["run_id"])
                assert run is not None
                run.status = "cancelled"
                run.ended_at = datetime.now(UTC)
    finally:
        await engine.dispose()
