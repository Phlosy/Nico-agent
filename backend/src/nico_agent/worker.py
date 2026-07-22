"""Infrastructure supervision and bounded persistent Run execution."""

from __future__ import annotations

import asyncio
import logging
import shlex
import signal

import httpx
from anyio import Path

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
from nico_agent.models.providers import (
    AnthropicMessagesProvider,
    GoogleGeminiProvider,
    OpenAICompatibleProvider,
)
from nico_agent.net.doh import DOH_ENDPOINTS, DnsOverHttpsResolver
from nico_agent.net.safe_http import SafeHttpClient
from nico_agent.projects.worker import ProjectSupervisionWorker
from nico_agent.provider_onboarding.worker import ProviderProbeWorker
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
    WebFetchExecutor,
    WebSearchExecutor,
    WorkspaceManager,
)
from nico_agent.web.cache import RedisWebFetchCache, RedisWebSearchCache
from nico_agent.web.providers import BraveSearchProvider, SearxngSearchProvider
from nico_agent.web.rate_limit import RedisWebRateLimiter
from nico_agent.web.registry import WebProviderRegistry
from nico_agent.web.source_authorization import WebSourceAuthorizer

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


async def health_loop(
    service: HealthServiceProtocol,
    stopping: asyncio.Event,
    *,
    interval_seconds: float,
) -> None:
    while not stopping.is_set():
        await supervise_once(service)
        try:
            await asyncio.wait_for(stopping.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


async def supervise_worker_tasks(
    tasks: list[asyncio.Task[None]],
    stopping: asyncio.Event,
) -> None:
    stop_waiter = asyncio.create_task(stopping.wait(), name="worker-stop-waiter")
    try:
        done, _ = await asyncio.wait(
            [stop_waiter, *tasks],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_waiter in done:
            return
        failed = next(task for task in done if task is not stop_waiter)
        if failed.cancelled():
            raise RuntimeError(f"worker task {failed.get_name()} was cancelled unexpectedly")
        error = failed.exception()
        if error is not None:
            raise RuntimeError(f"worker task {failed.get_name()} failed") from error
        raise RuntimeError(f"worker task {failed.get_name()} exited unexpectedly")
    finally:
        stop_waiter.cancel()
        await asyncio.gather(stop_waiter, return_exceptions=True)


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
    model_provider_options = {
        "client": model_http,
        "connect_timeout": runtime_settings.model_connect_timeout_seconds,
        "read_timeout": runtime_settings.model_read_timeout_seconds,
        "max_response_bytes": runtime_settings.model_max_response_bytes,
        "allow_http_loopback": runtime_settings.model_allow_http_loopback,
        "trusted_private_hosts": runtime_settings.model_trusted_private_hosts,
        "allow_http_trusted_hosts": runtime_settings.model_allow_http_trusted_hosts,
    }
    model_providers = [
        OpenAICompatibleProvider(**model_provider_options),
        AnthropicMessagesProvider(**model_provider_options),
        GoogleGeminiProvider(**model_provider_options),
    ]
    model_gateway = ModelGateway(
        ModelProviderRegistry(model_providers),
        rate_limiter=RedisModelRateLimiter(resources.redis),
        max_attempts=runtime_settings.model_max_attempts,
        retry_base_seconds=runtime_settings.model_retry_base_seconds,
    )
    provider_probe_gateway = ModelGateway(
        ModelProviderRegistry(model_providers),
        rate_limiter=RedisModelRateLimiter(resources.redis),
        max_attempts=1,
        retry_base_seconds=0,
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
    web_http = SafeHttpClient(
        connect_timeout=runtime_settings.web_connect_timeout_seconds,
        read_timeout=runtime_settings.web_read_timeout_seconds,
    )
    doh_bootstrap_http = SafeHttpClient(
        connect_timeout=runtime_settings.web_connect_timeout_seconds,
        read_timeout=runtime_settings.web_read_timeout_seconds,
    )
    resolver_http = {
        key: SafeHttpClient(
            resolver=DnsOverHttpsResolver(
                endpoint,
                http=doh_bootstrap_http,
                allow_private_bootstrap=True,
            ),
            connect_timeout=runtime_settings.web_connect_timeout_seconds,
            read_timeout=runtime_settings.web_read_timeout_seconds,
        )
        for key, endpoint in DOH_ENDPOINTS.items()
    }
    web_providers = WebProviderRegistry(
        [
            BraveSearchProvider(
                http=web_http,
                max_response_bytes=runtime_settings.web_search_max_response_bytes,
            ),
            SearxngSearchProvider(
                http=web_http,
                endpoint=runtime_settings.web_searxng_endpoint,
                allow_private=runtime_settings.web_searxng_allow_private,
                max_response_bytes=runtime_settings.web_search_max_response_bytes,
            ),
        ]
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
            WebSearchExecutor(
                web_providers,
                cache=RedisWebSearchCache(resources.redis),
                rate_limiter=RedisWebRateLimiter(resources.redis),
                environment=runtime_settings.environment,
            ),
            WebFetchExecutor(
                WebSourceAuthorizer(database),
                http=web_http,
                resolver_http=resolver_http,
                cache=RedisWebFetchCache(resources.redis),
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
            "provider_probe_concurrency": runtime_settings.provider_probe_concurrency,
            "project_supervision_concurrency": (runtime_settings.project_supervision_concurrency),
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
            ),
            name=f"run-executor-{index + 1}",
        )
        for index in range(runtime_settings.worker_concurrency)
    ]
    probe_tasks = [
        asyncio.create_task(
            execute_loop(
                ProviderProbeWorker(
                    database,
                    provider_probe_gateway,
                    worker_id=(f"{runtime_settings.worker_id}-provider-probe-{index + 1}"),
                    lease_seconds=runtime_settings.provider_probe_lease_seconds,
                    web_providers=web_providers,
                ),
                stopping,
                poll_interval_seconds=(runtime_settings.provider_probe_poll_interval_seconds),
            ),
            name=f"provider-probe-executor-{index + 1}",
        )
        for index in range(runtime_settings.provider_probe_concurrency)
    ]
    supervision_tasks = [
        asyncio.create_task(
            execute_loop(
                ProjectSupervisionWorker(
                    database,
                    worker_id=(f"{runtime_settings.worker_id}-project-supervision-{index + 1}"),
                    lease_seconds=runtime_settings.project_supervision_lease_seconds,
                ),
                stopping,
                poll_interval_seconds=(runtime_settings.project_supervision_poll_interval_seconds),
            ),
            name=f"project-supervision-executor-{index + 1}",
        )
        for index in range(runtime_settings.project_supervision_concurrency)
    ]
    health_task = asyncio.create_task(
        health_loop(
            service,
            stopping,
            interval_seconds=runtime_settings.worker_health_interval_seconds,
        ),
        name="infrastructure-health",
    )
    supervised_tasks = [*execution_tasks, *probe_tasks, *supervision_tasks, health_task]
    health_marker = Path(runtime_settings.worker_health_marker)
    try:
        await health_marker.write_text(f"{runtime_settings.worker_id}\n", encoding="utf-8")
        await supervise_worker_tasks(supervised_tasks, stopping)
    finally:
        await health_marker.unlink(missing_ok=True)
        stopping.set()
        await asyncio.gather(*supervised_tasks, return_exceptions=True)
        await model_http.aclose()
        await resources.close()
        logger.info("runtime worker stopped")


def run() -> None:
    asyncio.run(worker_main())


if __name__ == "__main__":
    run()
