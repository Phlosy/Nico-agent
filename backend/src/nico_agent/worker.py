"""Goal B worker entry point: infrastructure supervision only.

The persistent Run lease loop deliberately begins in Goal C/D. This process
proves the separate deployment unit and shared package without pretending to
execute domain work.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from nico_agent.config import Settings, get_settings
from nico_agent.health import (
    HealthServiceProtocol,
    InfrastructureResources,
    build_health_service,
)
from nico_agent.logging import configure_logging

logger = logging.getLogger(__name__)


async def supervise_once(service: HealthServiceProtocol) -> bool:
    report = await service.readiness()
    components = {name: item.status for name, item in report.components.items()}
    if report.status == "ready":
        logger.info("infrastructure readiness checked", extra={"components": components})
        return True
    logger.warning("infrastructure readiness degraded", extra={"components": components})
    return False


async def worker_main(settings: Settings | None = None) -> None:
    runtime_settings = settings or get_settings()
    configure_logging(runtime_settings.log_level)
    resources = InfrastructureResources.create(runtime_settings)
    service = build_health_service(runtime_settings, resources)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(stop_signal, stopping.set)

    logger.info(
        "infrastructure supervisor started",
        extra={"interval_seconds": runtime_settings.worker_health_interval_seconds},
    )
    try:
        while not stopping.is_set():
            await supervise_once(service)
            try:
                await asyncio.wait_for(
                    stopping.wait(), timeout=runtime_settings.worker_health_interval_seconds
                )
            except TimeoutError:
                pass
    finally:
        await resources.close()
        logger.info("infrastructure supervisor stopped")


def run() -> None:
    asyncio.run(worker_main())


if __name__ == "__main__":
    run()
