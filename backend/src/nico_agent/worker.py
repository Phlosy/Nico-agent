"""Infrastructure supervision and bounded persistent Run execution."""

from __future__ import annotations

import asyncio
import logging
import signal

from nico_agent.config import Settings, get_settings
from nico_agent.database import Database
from nico_agent.health import (
    HealthServiceProtocol,
    InfrastructureResources,
    build_health_service,
)
from nico_agent.logging import configure_logging
from nico_agent.runtime import MockRuntimeProvider, RuntimeProviderRegistry
from nico_agent.runtime.executor import RuntimeWorker

logger = logging.getLogger(__name__)


async def supervise_once(service: HealthServiceProtocol) -> bool:
    report = await service.readiness()
    components = {name: item.status for name, item in report.components.items()}
    if report.status == "ready":
        logger.info("infrastructure readiness checked", extra={"components": components})
        return True
    logger.warning("infrastructure readiness degraded", extra={"components": components})
    return False


async def execute_loop(
    worker: RuntimeWorker,
    stopping: asyncio.Event,
    *,
    poll_interval_seconds: float,
) -> None:
    while not stopping.is_set():
        claimed = await worker.execute_once()
        if claimed:
            continue
        try:
            await asyncio.wait_for(stopping.wait(), timeout=poll_interval_seconds)
        except TimeoutError:
            pass


async def worker_main(settings: Settings | None = None) -> None:
    runtime_settings = settings or get_settings()
    configure_logging(runtime_settings.log_level)
    resources = InfrastructureResources.create(runtime_settings)
    service = build_health_service(runtime_settings, resources)
    database = Database(resources.engine)
    providers = []
    if runtime_settings.environment != "production":
        providers.append(MockRuntimeProvider())
    registry = RuntimeProviderRegistry(providers)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(stop_signal, stopping.set)

    logger.info(
        "runtime worker started",
        extra={
            "health_interval_seconds": runtime_settings.worker_health_interval_seconds,
            "poll_interval_seconds": runtime_settings.worker_poll_interval_seconds,
            "concurrency": runtime_settings.worker_concurrency,
            "providers": registry.names,
        },
    )
    execution_tasks = [
        asyncio.create_task(
            execute_loop(
                RuntimeWorker(
                    database,
                    registry,
                    worker_id=f"{runtime_settings.worker_id}-{index + 1}",
                    lease_seconds=runtime_settings.worker_lease_seconds,
                    heartbeat_seconds=runtime_settings.worker_heartbeat_seconds,
                ),
                stopping,
                poll_interval_seconds=runtime_settings.worker_poll_interval_seconds,
            )
        )
        for index in range(runtime_settings.worker_concurrency)
    ]
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
        stopping.set()
        await asyncio.gather(*execution_tasks, return_exceptions=True)
        await resources.close()
        logger.info("runtime worker stopped")


def run() -> None:
    asyncio.run(worker_main())


if __name__ == "__main__":
    run()
