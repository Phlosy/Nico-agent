"""Dependency probes and health response contracts."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from nico_agent.config import Settings

ProbeCallable = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class DependencyProbe:
    name: str
    check: ProbeCallable


class ComponentHealth(BaseModel):
    status: Literal["up", "down"]
    latency_ms: float = Field(ge=0)
    detail: str | None = None


class ReadinessReport(BaseModel):
    status: Literal["ready", "not_ready"]
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    components: dict[str, ComponentHealth]

    @classmethod
    def from_components(cls, components: Mapping[str, ComponentHealth]) -> ReadinessReport:
        values = dict(components)
        all_up = values and all(item.status == "up" for item in values.values())
        status = "ready" if all_up else "not_ready"
        return cls(status=status, components=values)

    @classmethod
    def ready_for_testing(cls) -> ReadinessReport:
        return cls.from_components(
            {
                name: ComponentHealth(status="up", latency_ms=0)
                for name in ("postgres", "redis", "minio")
            }
        )


class HealthServiceProtocol(Protocol):
    async def readiness(self) -> ReadinessReport: ...


class HealthService:
    """Run dependency probes concurrently with an independent timeout per probe."""

    def __init__(self, probes: list[DependencyProbe], timeout_seconds: float) -> None:
        if not probes:
            raise ValueError("at least one dependency probe is required")
        self._probes = tuple(probes)
        self._timeout_seconds = timeout_seconds

    async def readiness(self) -> ReadinessReport:
        results = await asyncio.gather(*(self._run_probe(probe) for probe in self._probes))
        return ReadinessReport.from_components(dict(results))

    async def _run_probe(self, probe: DependencyProbe) -> tuple[str, ComponentHealth]:
        started = time.perf_counter()
        try:
            await asyncio.wait_for(probe.check(), timeout=self._timeout_seconds)
        except TimeoutError:
            detail = f"TimeoutError: dependency check exceeded {self._timeout_seconds:g}s"
            result = ComponentHealth(status="down", latency_ms=_elapsed_ms(started), detail=detail)
        except Exception as exc:  # noqa: BLE001 - health endpoints must aggregate all failures
            detail = f"{type(exc).__name__}: {str(exc)[:200]}"
            result = ComponentHealth(status="down", latency_ms=_elapsed_ms(started), detail=detail)
        else:
            result = ComponentHealth(status="up", latency_ms=_elapsed_ms(started))
        return probe.name, result


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


@dataclass(slots=True)
class InfrastructureResources:
    engine: AsyncEngine
    redis: Redis
    http: httpx.AsyncClient

    @classmethod
    def create(cls, settings: Settings) -> InfrastructureResources:
        return cls(
            engine=create_async_engine(settings.database_url, pool_pre_ping=True),
            redis=Redis.from_url(settings.redis_url, decode_responses=True),
            http=httpx.AsyncClient(),
        )

    async def close(self) -> None:
        await self.http.aclose()
        await self.redis.aclose()
        await self.engine.dispose()


def build_health_service(
    settings: Settings,
    resources: InfrastructureResources,
) -> HealthService:
    async def check_postgres() -> None:
        async with resources.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def check_redis() -> None:
        if not await resources.redis.ping():
            raise RuntimeError("Redis PING returned a false response")

    async def check_minio() -> None:
        response = await resources.http.get(f"{settings.minio_url}/minio/health/ready")
        response.raise_for_status()

    return HealthService(
        probes=[
            DependencyProbe("postgres", check_postgres),
            DependencyProbe("redis", check_redis),
            DependencyProbe("minio", check_minio),
        ],
        timeout_seconds=settings.dependency_timeout_seconds,
    )
