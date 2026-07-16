import pytest
from httpx import ASGITransport, AsyncClient

from nico_agent.api import create_app
from nico_agent.config import Settings
from nico_agent.health import ComponentHealth, ReadinessReport


class StubHealthService:
    def __init__(self, report: ReadinessReport) -> None:
        self.report = report
        self.calls = 0

    async def readiness(self) -> ReadinessReport:
        self.calls += 1
        return self.report


def make_client(report: ReadinessReport) -> tuple[AsyncClient, StubHealthService]:
    service = StubHealthService(report)
    app = create_app(
        settings=Settings(environment="test", _env_file=None),
        health_service=service,
    )
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    return client, service


@pytest.mark.asyncio
async def test_liveness_does_not_touch_external_dependencies() -> None:
    client, service = make_client(ReadinessReport.ready_for_testing())

    async with client:
        response = await client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "alive"
    assert service.calls == 0
    assert response.headers["x-request-id"]


@pytest.mark.asyncio
async def test_readiness_returns_200_when_every_component_is_up() -> None:
    client, service = make_client(ReadinessReport.ready_for_testing())

    async with client:
        response = await client.get(
            "/api/v1/health/ready",
            headers={"X-Request-ID": "acceptance-request-1"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.headers["x-request-id"] == "acceptance-request-1"
    assert service.calls == 1


@pytest.mark.asyncio
async def test_readiness_returns_503_with_component_status() -> None:
    report = ReadinessReport.from_components(
        {"postgres": ComponentHealth(status="down", latency_ms=1.25, detail="unavailable")}
    )
    client, _ = make_client(report)

    async with client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["components"]["postgres"]["detail"] == "unavailable"


@pytest.mark.asyncio
async def test_invalid_request_id_is_replaced() -> None:
    client, _ = make_client(ReadinessReport.ready_for_testing())

    async with client:
        response = await client.get(
            "/api/v1/health/live", headers={"X-Request-ID": "bad id\nvalue"}
        )

    assert response.status_code == 200
    assert response.headers["x-request-id"] != "bad id\nvalue"


@pytest.mark.asyncio
async def test_openapi_describes_health_contract() -> None:
    client, _ = make_client(ReadinessReport.ready_for_testing())

    async with client:
        response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert "/api/v1/health/live" in response.json()["paths"]
    assert "/api/v1/health/ready" in response.json()["paths"]
