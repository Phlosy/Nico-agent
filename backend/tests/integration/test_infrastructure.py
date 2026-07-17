import os
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.api import create_app
from nico_agent.config import Settings
from nico_agent.database import Database, TenantContext
from nico_agent.domain.models import Project, Task, Tenant

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with the Compose dependencies running",
)


@pytest.fixture
def settings() -> Settings:
    return Settings(environment="test", _env_file=None)


@pytest.mark.asyncio
async def test_migration_enables_extensions_and_core_schema(settings: Settings) -> None:
    engine = create_async_engine(settings.resolved_database_url)
    try:
        async with engine.connect() as connection:
            extensions = set(
                (
                    await connection.execute(
                        text(
                            "SELECT extname FROM pg_extension "
                            "WHERE extname IN ('pgcrypto', 'vector')"
                        )
                    )
                ).scalars()
            )
            revision = (
                await connection.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            domain_tables = set(
                (
                    await connection.execute(
                        text(
                            "SELECT tablename FROM pg_tables "
                            "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                        )
                    )
                ).scalars()
            )
    finally:
        await engine.dispose()

    assert extensions == {"pgcrypto", "vector"}
    assert revision == "20260717_0008"
    assert domain_tables == {
        "tenants",
        "projects",
        "agents",
        "agent_versions",
        "tasks",
        "runs",
        "run_steps",
        "events",
        "audit_records",
        "runtime_sessions",
        "tool_definitions",
        "tool_calls",
        "memories",
        "skills",
        "skill_versions",
        "growth_sources",
        "evaluations",
        "approvals",
        "skill_deployments",
        "memory_chunks",
    }


@pytest.mark.asyncio
async def test_runtime_role_and_force_rls_cover_every_core_table(settings: Settings) -> None:
    engine = create_async_engine(settings.resolved_database_url)
    expected_tables = {
        "tenants",
        "projects",
        "agents",
        "agent_versions",
        "tasks",
        "runs",
        "run_steps",
        "events",
        "audit_records",
        "runtime_sessions",
        "tool_definitions",
        "tool_calls",
        "memories",
        "skills",
        "skill_versions",
        "growth_sources",
        "evaluations",
        "approvals",
        "skill_deployments",
        "memory_chunks",
    }
    try:
        async with engine.connect() as connection:
            role = (
                await connection.execute(
                    text(
                        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'nico_runtime'"
                    )
                )
            ).one()
            protected = set(
                (
                    await connection.execute(
                        text(
                            "SELECT relname FROM pg_class "
                            "WHERE relname = ANY(:tables) "
                            "AND relrowsecurity AND relforcerowsecurity"
                        ),
                        {"tables": sorted(expected_tables)},
                    )
                ).scalars()
            )
    finally:
        await engine.dispose()

    assert role == (False, False)
    assert protected == expected_tables


@pytest.mark.asyncio
async def test_rls_isolates_tenants_and_composite_keys_reject_cross_tenant_links(
    settings: Settings,
) -> None:
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    tenant_a = Tenant(name="Tenant A", slug=f"tenant-a-{uuid4()}")
    tenant_b = Tenant(name="Tenant B", slug=f"tenant-b-{uuid4()}")
    try:
        async with database.admin_transaction() as session:
            session.add_all([tenant_a, tenant_b])
            await session.flush()

        context_a = TenantContext(tenant_a.id, "integration-a", uuid4())
        context_b = TenantContext(tenant_b.id, "integration-b", uuid4())
        async with database.tenant_transaction(context_a) as session:
            project_a = Project(tenant_id=tenant_a.id, name="private-a")
            session.add(project_a)
            await session.flush()

        async with database.tenant_transaction(context_b) as session:
            visible_projects = (await session.scalars(select(Project))).all()
        assert visible_projects == []

        with pytest.raises(IntegrityError):
            async with database.tenant_transaction(context_b) as session:
                session.add(
                    Task(
                        tenant_id=tenant_b.id,
                        project_id=project_a.id,
                        title="cross-tenant reference",
                    )
                )
                await session.flush()

        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL ROLE nico_runtime"))
            count = (await connection.execute(text("SELECT count(*) FROM projects"))).scalar_one()
        assert count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_redis_round_trip(settings: Settings) -> None:
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await client.set("nico:goal-b:integration", "ready", ex=30)
        assert await client.get("nico:goal-b:integration") == "ready"
    finally:
        await client.delete("nico:goal-b:integration")
        await client.aclose()


@pytest.mark.asyncio
async def test_minio_readiness_endpoint(settings: Settings) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{settings.minio_url}/minio/health/ready")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_api_readiness_uses_real_dependencies(settings: Settings) -> None:
    app = create_app(settings=settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert set(response.json()["components"]) == {"postgres", "redis", "minio"}
