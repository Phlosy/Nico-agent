import pytest

from nico_agent.health import ComponentHealth, ReadinessReport


@pytest.fixture
def ready_report() -> ReadinessReport:
    return ReadinessReport.from_components(
        {
            name: ComponentHealth(status="up", latency_ms=0)
            for name in ("postgres", "redis", "minio")
        }
    )
