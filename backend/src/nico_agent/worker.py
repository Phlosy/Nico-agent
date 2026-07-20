"""Infrastructure supervision and bounded persistent Run execution."""

from __future__ import annotations

import asyncio
import logging
import shlex
import signal

import httpx

from nico_agent.artifacts.minio import MinioArtifactStore
from nico_agent.artifacts.service import ArtifactService
from nico_agent.config import Settings, get_settings
from nico_agent.database import Database
from nico_agent.health import (
    HealthServiceProtocol,
    InfrastructureResources,
    build_health_service,
)
from nico_agent.logging import configure_logging
from nico_agent.models import ModelGateway, ModelProviderRegistry
from nico_agent.models.gateway import RedisModelRateLimiter
from nico_agent.models.providers import OpenAICompatibleProvider
from nico_agent.runtime import (
    HermesRuntimeProvider,
    MockRuntimeProvider,
    NicoNativeRuntimeProvider,
    RuntimeProviderRegistry,
)
from nico_agent.runtime.executor import RuntimeWorker
from nico_agent.tools import ToolGateway, ToolRegistry
from nico_agent.tools.builtin import (
    DatabaseReadExecutor,
    FileReadExecutor,
    FileWriteExecutor,
    HttpReadExecutor,
    PythonSandboxExecutor,
    ReportWriteExecutor,
    SandboxRunnerClient,
    WorkspaceManager,
)

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


def build_runtime_registry(
    settings: Settings,
    model_gateway: ModelGateway,
) -> RuntimeProviderRegistry:
    providers = [
        NicoNativeRuntimeProvider(
            model_gateway,
            post_tool_delay_seconds=settings.native_post_tool_delay_seconds,
        )
    ]
    if settings.environment != "production":
        providers.append(MockRuntimeProvider())
    if settings.hermes_enabled:
        providers.append(
            HermesRuntimeProvider(
                tuple(shlex.split(settings.hermes_command)),
                cwd=settings.hermes_cwd,
                state_root=settings.hermes_state_root,
            )
        )
    return RuntimeProviderRegistry(providers)


async def worker_main(settings: Settings | None = None) -> None:
    runtime_settings = settings or get_settings()
    configure_logging(runtime_settings.log_level)
    resources = InfrastructureResources.create(runtime_settings)
    service = build_health_service(runtime_settings, resources)
    database = Database(resources.engine)
    model_http = httpx.AsyncClient(follow_redirects=False, trust_env=False)
    model_provider = OpenAICompatibleProvider(
        client=model_http,
        connect_timeout=runtime_settings.model_connect_timeout_seconds,
        read_timeout=runtime_settings.model_read_timeout_seconds,
        max_response_bytes=runtime_settings.model_max_response_bytes,
        allow_http_loopback=runtime_settings.model_allow_http_loopback,
        trusted_private_hosts=runtime_settings.model_trusted_private_hosts,
        allow_http_trusted_hosts=runtime_settings.model_allow_http_trusted_hosts,
    )
    model_gateway = ModelGateway(
        ModelProviderRegistry([model_provider]),
        rate_limiter=RedisModelRateLimiter(resources.redis),
        max_attempts=runtime_settings.model_max_attempts,
        retry_base_seconds=runtime_settings.model_retry_base_seconds,
    )
    registry = build_runtime_registry(runtime_settings, model_gateway)
    workspace = WorkspaceManager(
        runtime_settings.workspace_root,
        max_file_bytes=runtime_settings.workspace_max_file_bytes,
        max_total_bytes=runtime_settings.workspace_max_total_bytes,
    )
    sandbox_client = SandboxRunnerClient(
        runtime_settings.sandbox_runner_url,
        runtime_settings.sandbox_runner_token,
    )
    tool_registry = ToolRegistry(
        [
            FileReadExecutor(workspace),
            FileWriteExecutor(workspace),
            ReportWriteExecutor(workspace),
            HttpReadExecutor(
                max_response_bytes=runtime_settings.http_max_response_bytes,
                connect_timeout=runtime_settings.http_connect_timeout_seconds,
                read_timeout=runtime_settings.http_read_timeout_seconds,
                max_redirects=runtime_settings.http_max_redirects,
                allow_http_loopback=runtime_settings.http_allow_loopback,
            ),
            DatabaseReadExecutor(
                connect_timeout=runtime_settings.database_tool_connect_timeout_seconds,
                statement_timeout_ms=runtime_settings.database_tool_statement_timeout_ms,
                max_rows=runtime_settings.database_tool_max_rows,
                max_output_bytes=runtime_settings.database_tool_max_output_bytes,
            ),
            PythonSandboxExecutor(
                sandbox_client,
                wall_time_seconds=runtime_settings.sandbox_wall_time_seconds,
                memory_bytes=runtime_settings.sandbox_memory_bytes,
                nano_cpus=runtime_settings.sandbox_nano_cpus,
                pids_limit=runtime_settings.sandbox_pids_limit,
                output_bytes=runtime_settings.sandbox_output_bytes,
            ),
        ]
    )
    tool_gateway = ToolGateway(
        database,
        tool_registry,
        approval_required_risks=frozenset(runtime_settings.tool_approval_required_risks),
        approval_ttl_seconds=runtime_settings.tool_approval_ttl_seconds,
    )
    artifact_service = ArtifactService(
        database,
        MinioArtifactStore(
            runtime_settings.minio_url,
            access_key=runtime_settings.minio_access_key,
            secret_key=runtime_settings.minio_secret_key,
            bucket=runtime_settings.minio_bucket,
        ),
        max_bytes=runtime_settings.artifact_max_bytes,
    )
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
                    tool_gateway=tool_gateway,
                    artifact_service=artifact_service,
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
        await model_http.aclose()
        await resources.close()
        logger.info("runtime worker stopped")


def run() -> None:
    asyncio.run(worker_main())


if __name__ == "__main__":
    run()
