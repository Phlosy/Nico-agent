import os

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.api import create_app
from nico_agent.config import Settings

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with the Compose dependencies running",
)


@pytest.fixture
def settings() -> Settings:
    return Settings(environment="test", _env_file=None)


@pytest.mark.asyncio
async def test_migration_enables_extensions_without_goal_c_tables(settings: Settings) -> None:
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
    assert revision == "20260716_0001"
    assert domain_tables == set()


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
