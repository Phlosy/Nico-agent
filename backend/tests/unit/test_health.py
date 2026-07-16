import asyncio

import pytest

from nico_agent.health import DependencyProbe, HealthService


@pytest.mark.asyncio
async def test_readiness_reports_all_dependencies_up() -> None:
    calls: list[str] = []

    async def healthy(name: str) -> None:
        calls.append(name)

    service = HealthService(
        probes=[
            DependencyProbe("postgres", lambda: healthy("postgres")),
            DependencyProbe("redis", lambda: healthy("redis")),
            DependencyProbe("minio", lambda: healthy("minio")),
        ],
        timeout_seconds=0.1,
    )

    report = await service.readiness()

    assert report.status == "ready"
    assert set(calls) == {"postgres", "redis", "minio"}
    assert {name: check.status for name, check in report.components.items()} == {
        "postgres": "up",
        "redis": "up",
        "minio": "up",
    }


@pytest.mark.asyncio
async def test_readiness_is_degraded_with_a_safe_failure_category() -> None:
    async def healthy() -> None:
        return None

    async def broken() -> None:
        raise ConnectionError("connection refused")

    service = HealthService(
        probes=[DependencyProbe("postgres", healthy), DependencyProbe("redis", broken)],
        timeout_seconds=0.1,
    )

    report = await service.readiness()

    assert report.status == "not_ready"
    assert report.components["postgres"].status == "up"
    assert report.components["redis"].status == "down"
    assert report.components["redis"].detail == "ConnectionError: dependency check failed"


@pytest.mark.asyncio
async def test_readiness_times_out_each_dependency() -> None:
    async def too_slow() -> None:
        await asyncio.sleep(0.1)

    service = HealthService(
        probes=[DependencyProbe("minio", too_slow)],
        timeout_seconds=0.01,
    )

    report = await service.readiness()

    assert report.status == "not_ready"
    assert report.components["minio"].status == "down"
    assert report.components["minio"].detail == "TimeoutError: dependency check exceeded 0.01s"


@pytest.mark.asyncio
async def test_readiness_redacts_credentials_from_failure_details() -> None:
    async def leaks_credentials() -> None:
        raise ConnectionError("postgresql://nico:top-secret@postgres/nico password=another-secret")

    service = HealthService(
        probes=[DependencyProbe("postgres", leaks_credentials)],
        timeout_seconds=0.1,
    )

    report = await service.readiness()

    detail = report.components["postgres"].detail
    assert detail == "ConnectionError: dependency check failed"
    assert "top-secret" not in detail
    assert "another-secret" not in detail
