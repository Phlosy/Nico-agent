from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings
from nico_agent.database import Database
from nico_agent.domain.models import Agent, AgentVersion, Project, Run, Task, Tenant

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with the Compose dependencies running",
)


async def seed_pending_run(
    database: Database,
    *,
    priority: int = 0,
    run_config: dict | None = None,
) -> Run:
    suffix = uuid4().hex[:10]
    async with database.admin_transaction() as session:
        tenant = Tenant(name=f"Lease {suffix}", slug=f"lease-{suffix}")
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Lease Agent",
        )
        session.add_all([project, agent])
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="worker-test",
            mandate="Prove lease behavior",
            run_config=run_config or {},
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
            title="Lease acceptance",
            status="running",
            priority=priority,
        )
        session.add(task)
        await session.flush()
        run = Run(
            tenant_id=tenant.id,
            task_id=task.id,
            agent_id=agent.id,
            agent_version_id=version.id,
            attempt=1,
        )
        session.add(run)
        await session.flush()
        run_id = run.id
        tenant_id = tenant.id

    async with database.admin_transaction() as session:
        return (
            (
                await session.execute(
                    text("SELECT * FROM runs WHERE id = :run_id AND tenant_id = :tenant_id"),
                    {"run_id": run_id, "tenant_id": tenant_id},
                )
            )
            .mappings()
            .one()
        )


@pytest.mark.asyncio
async def test_claimer_role_is_minimal_and_runtime_session_has_force_rls() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    try:
        async with engine.connect() as connection:
            role = (
                await connection.execute(
                    text(
                        "SELECT rolsuper, rolbypassrls FROM pg_roles "
                        "WHERE rolname = 'nico_worker_claimer'"
                    )
                )
            ).one()
            protected = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = 'runtime_sessions'"
                    )
                )
            ).one()
            function_security = (
                await connection.execute(
                    text(
                        "SELECT prosecdef, proconfig FROM pg_proc WHERE proname = 'claim_next_run'"
                    )
                )
            ).one()
        assert role == (False, False)
        assert protected == (True, True)
        assert function_security[0] is True
        assert "search_path=pg_catalog, public" in function_security[1]

        with pytest.raises(ProgrammingError):
            async with engine.begin() as connection:
                await connection.execute(text("SET LOCAL ROLE nico_worker_claimer"))
                await connection.execute(text("SELECT * FROM runs"))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_claim_is_unique_and_expired_lease_is_reclaimed() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        seeded = await seed_pending_run(database, priority=2_000_000_000)
        claims = await asyncio.gather(
            database.claim_next_run("worker-a", 30),
            database.claim_next_run("worker-b", 30),
        )
        target_claims = [
            claim for claim in claims if claim is not None and claim.run_id == seeded["id"]
        ]
        winner = target_claims[0]

        assert len(target_claims) == 1
        assert winner.run_id == seeded["id"]
        assert winner.tenant_id == seeded["tenant_id"]
        assert winner.previous_status == "pending"

        async with database.admin_transaction() as session:
            await session.execute(
                text(
                    "UPDATE runs SET lease_expires_at = :expired "
                    "WHERE id = :run_id AND tenant_id = :tenant_id"
                ),
                {
                    "expired": datetime.now(UTC) - timedelta(seconds=1),
                    "run_id": winner.run_id,
                    "tenant_id": winner.tenant_id,
                },
            )

        reclaimed = await database.claim_next_run("worker-c", 30)
        assert reclaimed is not None
        assert reclaimed.run_id == winner.run_id
        assert reclaimed.lease_token != winner.lease_token
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_claim_orders_higher_priority_first() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        low = await seed_pending_run(database, priority=-10)
        high = await seed_pending_run(database, priority=50)

        claimed = await database.claim_next_run("priority-worker", 30)

        assert claimed is not None
        assert claimed.run_id == high["id"]
        assert claimed.run_id != low["id"]
    finally:
        await engine.dispose()
