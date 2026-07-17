from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings
from nico_agent.database import Database, TenantContext
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    Project,
    Run,
    RunStep,
    Task,
    Tenant,
    ToolCall,
    ToolDefinition,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with the Compose dependencies running",
)


async def _seed_tool_scope(database: Database, label: str) -> dict:
    suffix = uuid4().hex[:10]
    async with database.admin_transaction() as session:
        tenant = Tenant(name=f"Tool {label} {suffix}", slug=f"tool-{label}-{suffix}")
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Tool Agent",
        )
        session.add_all([project, agent])
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="tool-test",
            mandate="Exercise the tool persistence contract",
            tool_policy={
                "allow": ["file.read@1.0.0"],
                "permissions": ["filesystem.read"],
            },
            content_hash="b" * 64,
        )
        session.add(version)
        await session.flush()
        agent.current_version_id = version.id
        agent.status = "ready"
        task = Task(
            tenant_id=tenant.id,
            project_id=project.id,
            assignee_agent_id=agent.id,
            title="Tool persistence",
            status="running",
        )
        session.add(task)
        await session.flush()
        run = Run(
            tenant_id=tenant.id,
            task_id=task.id,
            agent_id=agent.id,
            agent_version_id=version.id,
            attempt=1,
            status="running",
        )
        session.add(run)
        await session.flush()
        step = RunStep(
            tenant_id=tenant.id,
            run_id=run.id,
            sequence=1,
            kind="tool",
            status="completed",
        )
        definition = ToolDefinition(
            tenant_id=tenant.id,
            name="file.read",
            version="1.0.0",
            status="enabled",
            description="Read one scoped file",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            permission="filesystem.read",
            timeout_seconds=5,
            retry_policy={"max_attempts": 1},
            isolation_policy={"kind": "workspace"},
            risk="low",
            max_output_bytes=1024,
            implementation_hash="c" * 64,
            content_hash="d" * 64,
            created_by="integration-test",
        )
        session.add_all([step, definition])
        await session.flush()
        call = ToolCall(
            tenant_id=tenant.id,
            run_id=run.id,
            run_step_id=step.id,
            tool_definition_id=definition.id,
            tool_name=definition.name,
            tool_version=definition.version,
            idempotency_key="call-1",
            arguments_hash="e" * 64,
            caller="runtime:mock",
            arguments={"path": "input.txt"},
            status="succeeded",
            result={"content": "ok"},
        )
        session.add(call)
        await session.flush()
        return {
            "tenant_id": tenant.id,
            "run_id": run.id,
            "step_id": step.id,
            "definition_id": definition.id,
            "call_id": call.id,
        }


@pytest.mark.asyncio
async def test_tool_definitions_and_calls_are_force_rls_isolated() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        first = await _seed_tool_scope(database, "first")
        second = await _seed_tool_scope(database, "second")

        for own, hidden in ((first, second), (second, first)):
            context = TenantContext(own["tenant_id"], "rls-test", uuid4())
            async with database.tenant_transaction(context) as session:
                definition_ids = set((await session.scalars(select(ToolDefinition.id))).all())
                call_ids = set((await session.scalars(select(ToolCall.id))).all())
            assert own["definition_id"] in definition_ids
            assert own["call_id"] in call_ids
            assert hidden["definition_id"] not in definition_ids
            assert hidden["call_id"] not in call_ids
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_enabled_definition_and_terminal_call_are_database_immutable() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        seeded = await _seed_tool_scope(database, "immutable")

        with pytest.raises(DBAPIError):
            async with database.admin_transaction() as session:
                await session.execute(
                    text("UPDATE tool_definitions SET description = 'mutated' WHERE id = :id"),
                    {"id": seeded["definition_id"]},
                )
        with pytest.raises(DBAPIError):
            async with database.admin_transaction() as session:
                await session.execute(
                    text("UPDATE tool_calls SET result = CAST(:result AS jsonb) WHERE id = :id"),
                    {"id": seeded["call_id"], "result": '{"changed":true}'},
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_tool_call_scope_and_idempotency_are_database_enforced() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        first = await _seed_tool_scope(database, "scope-first")
        second = await _seed_tool_scope(database, "scope-second")

        with pytest.raises(IntegrityError):
            async with database.admin_transaction() as session:
                session.add(
                    ToolCall(
                        tenant_id=first["tenant_id"],
                        run_id=first["run_id"],
                        run_step_id=second["step_id"],
                        tool_definition_id=first["definition_id"],
                        tool_name="file.read",
                        tool_version="1.0.0",
                        idempotency_key="cross-tenant",
                        arguments_hash="f" * 64,
                        caller="runtime:mock",
                        arguments={},
                    )
                )
                await session.flush()

        with pytest.raises(IntegrityError):
            async with database.admin_transaction() as session:
                existing = await session.scalar(
                    select(ToolCall).where(ToolCall.id == first["call_id"])
                )
                assert existing is not None
                session.add(
                    ToolCall(
                        tenant_id=first["tenant_id"],
                        run_id=first["run_id"],
                        run_step_id=first["step_id"],
                        tool_definition_id=first["definition_id"],
                        tool_name="file.read",
                        tool_version="1.0.0",
                        idempotency_key="call-1",
                        arguments_hash="f" * 64,
                        caller="runtime:mock",
                        arguments={},
                    )
                )
                await session.flush()

        context = TenantContext(first["tenant_id"], "count-test", uuid4())
        async with database.tenant_transaction(context) as session:
            count = await session.scalar(select(func.count()).select_from(ToolCall))
        assert count == 1
    finally:
        await engine.dispose()
