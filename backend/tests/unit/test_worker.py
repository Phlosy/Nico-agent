import logging

import pytest

from nico_agent.health import ComponentHealth, ReadinessReport
from nico_agent.worker import supervise_once


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
