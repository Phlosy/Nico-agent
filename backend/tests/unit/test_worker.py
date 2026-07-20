import asyncio
import logging

import pytest

from nico_agent.config import Settings
from nico_agent.health import ComponentHealth, ReadinessReport
from nico_agent.models import ModelGateway, ModelProviderRegistry
from nico_agent.runtime.errors import RuntimeProviderNotFound
from nico_agent.worker import build_runtime_registry, supervise_once, supervise_worker_tasks


class StubHealthService:
    def __init__(self, report: ReadinessReport) -> None:
        self.report = report

    async def readiness(self) -> ReadinessReport:
        return self.report


@pytest.mark.asyncio
async def test_supervisor_once_reports_readiness_without_claiming_work(
    caplog, ready_report: ReadinessReport
) -> None:
    caplog.set_level(logging.INFO)
    service = StubHealthService(ready_report)

    ready = await supervise_once(service)

    assert ready is True
    assert "infrastructure readiness checked" in caplog.text


@pytest.mark.asyncio
async def test_supervisor_once_reports_degraded_infrastructure(caplog) -> None:
    caplog.set_level(logging.WARNING)
    report = ReadinessReport.from_components(
        {"postgres": ComponentHealth(status="down", latency_ms=1, detail="offline")}
    )

    ready = await supervise_once(StubHealthService(report))

    assert ready is False
    assert "infrastructure readiness degraded" in caplog.text


def test_native_is_default_and_hermes_is_opt_in() -> None:
    gateway = ModelGateway(ModelProviderRegistry())

    default_registry = build_runtime_registry(Settings(environment="test", _env_file=None), gateway)
    hermes_registry = build_runtime_registry(
        Settings(environment="test", hermes_enabled=True, _env_file=None), gateway
    )

    assert default_registry.names == ("mock", "nico_native")
    assert hermes_registry.names == ("hermes", "mock", "nico_native")
    with pytest.raises(RuntimeProviderNotFound):
        default_registry.get("hermes")

    matrix = {item["name"]: item for item in hermes_registry.capability_matrix()}
    assert matrix["nico_native"]["capabilities"]["planning"] is True
    assert matrix["hermes"]["capabilities"]["planning"] is False
    assert matrix["hermes"]["capabilities"]["coordination"] is False
    assert matrix["mock"]["implementation"] == "test"


@pytest.mark.asyncio
async def test_worker_task_supervision_stops_cleanly_on_signal() -> None:
    stopping = asyncio.Event()
    task = asyncio.create_task(stopping.wait(), name="executor")
    stopping.set()

    await supervise_worker_tasks([task], stopping)
    await task


@pytest.mark.asyncio
async def test_worker_task_supervision_escalates_unexpected_exit() -> None:
    async def exit_now() -> None:
        return None

    task = asyncio.create_task(exit_now(), name="probe-executor")
    with pytest.raises(RuntimeError, match="probe-executor exited unexpectedly"):
        await supervise_worker_tasks([task], asyncio.Event())


@pytest.mark.asyncio
async def test_worker_task_supervision_preserves_failure_cause() -> None:
    async def fail_now() -> None:
        raise ValueError("canary")

    task = asyncio.create_task(fail_now(), name="run-executor")
    with pytest.raises(RuntimeError, match="run-executor failed") as captured:
        await supervise_worker_tasks([task], asyncio.Event())
    assert isinstance(captured.value.__cause__, ValueError)
