import logging

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
async def test_liveness_does_not_touch_external_dependencies(
    ready_report: ReadinessReport,
) -> None:
    client, service = make_client(ready_report)

    async with client:
        response = await client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "alive"
    assert service.calls == 0
    assert response.headers["x-request-id"]


@pytest.mark.asyncio
async def test_readiness_returns_200_when_every_component_is_up(
    ready_report: ReadinessReport,
) -> None:
    client, service = make_client(ready_report)

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
async def test_invalid_request_id_is_replaced(ready_report: ReadinessReport) -> None:
    client, _ = make_client(ready_report)

    async with client:
        response = await client.get(
            "/api/v1/health/live", headers={"X-Request-ID": "bad id\nvalue"}
        )

    assert response.status_code == 200
    assert response.headers["x-request-id"] != "bad id\nvalue"


@pytest.mark.asyncio
async def test_unhandled_errors_return_a_safe_body_and_request_id(
    ready_report: ReadinessReport,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR)
    service = StubHealthService(ready_report)
    app = create_app(
        settings=Settings(environment="test", _env_file=None),
        health_service=service,
    )

    @app.get("/test/unhandled-error")
    async def fail() -> None:
        raise RuntimeError("secret internal detail")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/test/unhandled-error",
            headers={"X-Request-ID": "failed-request-1"},
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}
    assert response.headers["x-request-id"] == "failed-request-1"
    assert "secret internal detail" not in response.text
    assert "secret internal detail" not in caplog.text


@pytest.mark.asyncio
async def test_openapi_describes_health_contract(ready_report: ReadinessReport) -> None:
    client, _ = make_client(ready_report)

    async with client:
        response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert "/api/v1/health/live" in response.json()["paths"]
    assert "/api/v1/health/ready" in response.json()["paths"]


@pytest.mark.asyncio
async def test_openapi_describes_goal_c_control_plane(ready_report: ReadinessReport) -> None:
    client, _ = make_client(ready_report)

    async with client:
        response = await client.get("/openapi.json")

    paths = response.json()["paths"]
    assert {
        "/api/v1/tenants/bootstrap",
        "/api/v1/projects",
        "/api/v1/agents",
        "/api/v1/agents/{agent_id}/versions",
        "/api/v1/tasks",
        "/api/v1/tasks/{task_id}/runs",
        "/api/v1/runs/{run_id}/steps",
        "/api/v1/runs/{run_id}/events",
        "/api/v1/audit",
    } <= paths.keys()

    parameters = paths["/api/v1/agents"]["get"]["parameters"]
    assert any(
        parameter["in"] == "header" and parameter["name"] == "X-Tenant-ID"
        for parameter in parameters
    )


@pytest.mark.asyncio
async def test_development_tenant_context_is_rejected_in_production(
    ready_report: ReadinessReport,
) -> None:
    app = create_app(
        settings=Settings(environment="production", _env_file=None),
        health_service=StubHealthService(ready_report),
        database=object(),  # type: ignore[arg-type]
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/agents",
            headers={"X-Tenant-ID": "00000000-0000-0000-0000-000000000001"},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "DEVELOPMENT_TENANT_CONTEXT_DISABLED"
