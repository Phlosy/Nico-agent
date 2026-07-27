from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.api import create_app
from nico_agent.api_schemas import RunCreate, RunTransition
from nico_agent.config import Settings
from nico_agent.control_plane import ControlPlaneService
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainConflict
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    Event,
    ExternalToolProvider,
    Project,
    Run,
    RuntimeSession,
    RunToolBinding,
    Task,
    Tenant,
    ToolCall,
)
from nico_agent.domain.states import RunStatus, ToolCallStatus
from nico_agent.net.safe_http import (
    PinnedRequest,
    RawHttpResponse,
    SafeHttpClient,
    SafeHttpError,
)
from nico_agent.runtime import MockRuntimeProvider
from nico_agent.runtime.executor import RuntimeWorker
from nico_agent.runtime.registry import RuntimeProviderRegistry
from nico_agent.runtime.service import RuntimeExecutionService
from nico_agent.testing.fake_tool_provider import (
    FAKE_ECHO_INPUT_SCHEMA,
    FAKE_ECHO_OUTPUT_SCHEMA,
    FAKE_ECHO_TOOL,
    FakeToolProvider,
    FakeToolProviderMode,
    FakeToolProviderScope,
)
from nico_agent.tool_providers.client import ToolProviderClient
from nico_agent.tool_providers.contracts import (
    ExternalToolProviderCreate,
    ProviderToolContract,
    RunToolBindingCreate,
)
from nico_agent.tool_providers.errors import ToolProviderError
from nico_agent.tool_providers.executor import ExternalToolResolver
from nico_agent.tool_providers.service import ToolProviderService
from nico_agent.tools import (
    ToolDefinitionSpec,
    ToolExecutionResult,
    ToolGateway,
    ToolGatewayRequest,
    ToolGatewayResult,
    ToolIsolation,
    ToolRegistry,
    ToolRetryPolicy,
    ToolRisk,
)
from nico_agent.tools.contracts import canonical_hash
from nico_agent.tools.errors import ToolAccessDenied, ToolApprovalRequired, ToolLeaseLost
from nico_agent.tools.secrets import EnvironmentSecretResolver

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with the Compose dependencies running",
)

SECRET = "integration-tool-provider-secret-at-least-32-bytes"
CREDENTIAL_REF = "env:NICO_TOOL_SECRET_INTEGRATION_PROVIDER"


@pytest.fixture(autouse=True)
async def retire_external_provider_test_tasks() -> None:
    """Keep deliberately interrupted Provider Runs out of later integration claims."""

    yield
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        async with database.admin_transaction() as session:
            await session.execute(
                update(Task)
                .where(Task.title.like("External Provider%"))
                .values(status="completed", priority=-2_147_483_648)
            )
    finally:
        await engine.dispose()


class _SwitchableAsgiTransport:
    def __init__(self) -> None:
        self.provider: FakeToolProvider | None = None
        self.fail_after_execute_once = False

    async def request(self, request: PinnedRequest) -> RawHttpResponse:
        if self.provider is None:
            raise SafeHttpError("NETWORK_ERROR", "test Provider is not available")
        headers = dict(request.headers)
        headers["Host"] = request.hostname
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.provider.app),
            base_url=f"{request.scheme}://{request.hostname}:{request.port}",
        ) as client:
            response = await client.request(
                request.method,
                request.target,
                headers=headers,
                content=request.body,
            )
        if request.target == "/v1/tool-calls" and self.fail_after_execute_once:
            self.fail_after_execute_once = False
            raise SafeHttpError(
                "READ_TIMEOUT",
                "simulated response loss after Provider execution",
            )
        return RawHttpResponse(
            status=response.status_code,
            headers=tuple(response.headers.multi_items()),
            body=response.content,
        )


@dataclass(slots=True)
class _LocalFallbackExecutor:
    spec: ToolDefinitionSpec
    calls: int = 0
    implementation_hash: str = "f" * 64

    async def execute(self, _context, arguments, _secrets) -> ToolExecutionResult:
        self.calls += 1
        return ToolExecutionResult(output={"echo": {"local_fallback": arguments}})


@dataclass(frozen=True, slots=True)
class _Setup:
    settings: Settings
    database: Database
    context: TenantContext
    service: ToolProviderService
    client: ToolProviderClient
    transport: _SwitchableAsgiTransport
    provider: FakeToolProvider
    provider_id: UUID
    project_id: UUID
    task_id: UUID
    agent_id: UUID
    agent_version_id: UUID
    run_id: UUID
    binding_id: UUID
    binding_digest: str
    spec: ToolDefinitionSpec


def _tool_spec() -> ToolDefinitionSpec:
    return ToolDefinitionSpec(
        name=FAKE_ECHO_TOOL.name,
        version=FAKE_ECHO_TOOL.version,
        description="Deterministic external Provider echo",
        input_schema=FAKE_ECHO_INPUT_SCHEMA,
        output_schema=FAKE_ECHO_OUTPUT_SCHEMA,
        permission="test.external.echo",
        timeout_seconds=5,
        retry_policy=ToolRetryPolicy(max_attempts=1),
        isolation=ToolIsolation.IN_PROCESS,
        risk=ToolRisk.LOW,
        max_output_bytes=1_048_576,
    )


def _settings() -> Settings:
    return Settings(
        environment="test",
        tool_provider_allow_http_loopback=True,
        tool_provider_connect_timeout_seconds=1,
        tool_provider_read_timeout_seconds=5,
        _env_file=None,
    )


def _client(transport: _SwitchableAsgiTransport) -> ToolProviderClient:
    return ToolProviderClient(
        http=SafeHttpClient(
            transport=transport,
            resolver=lambda _hostname, _port: ("127.0.0.1",),
            connect_timeout=1,
            read_timeout=5,
        ),
        secret_resolver=EnvironmentSecretResolver(
            {"NICO_TOOL_SECRET_INTEGRATION_PROVIDER": SECRET}
        ),
        max_response_bytes=1_048_576,
    )


async def _seed_setup(
    database: Database,
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    max_calls: int = 5,
    retry_attempts: int = 1,
    approval_mode: str = "never",
    provider_timeout_seconds: int = 5,
    max_total_duration_seconds: int = 30,
) -> _Setup:
    settings = _settings()
    suffix = uuid4().hex[:10]
    spec = _tool_spec()
    tenant_policy = {
        "allow": [spec.reference],
        "permissions": [spec.permission],
        "secret_refs": {},
        "tools": {spec.reference: {}},
    }
    agent_policy = {
        "allow": [spec.reference],
        "permissions": [spec.permission],
        "secrets": [],
        "tools": {spec.reference: {}},
    }
    configured_calls = (
        [
            {
                "call_id": "external-echo",
                "name": spec.name,
                "version": spec.version,
                "arguments": {"message": "runtime consumed this"},
                "idempotency_key": "external-echo-1",
            }
        ]
        if tool_calls is None
        else tool_calls
    )
    async with database.admin_transaction() as session:
        await session.execute(
            update(Task).where(Task.title == "External Provider integration").values(priority=-1)
        )
        tenant = Tenant(
            name=f"External Provider {suffix}",
            slug=f"external-provider-{suffix}",
            settings={"tool_policy": tenant_policy},
        )
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"provider-project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"provider-agent-{suffix}",
            display_name="External Provider Agent",
        )
        session.add_all([project, agent])
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="provider-test",
            mandate="Call the exact external Tool and consume its result",
            tool_policy=agent_policy,
            run_config={
                "runtime_provider": "mock",
                "mock": {
                    "steps": [],
                    "tool_calls": configured_calls,
                },
            },
            content_hash="a" * 64,
        )
        session.add(version)
        await session.flush()
        agent.current_version_id = version.id
        agent.status = "ready"
        task = Task(
            tenant_id=tenant.id,
            project_id=project.id,
            assignee_agent_id=agent.id,
            title="External Provider integration",
            input={"request": "echo through Provider"},
            status="assigned",
            priority=2_147_483_647,
        )
        session.add(task)
        await session.flush()
        tenant_id = tenant.id
        project_id = project.id
        task_id = task.id
        agent_id = agent.id
        version_id = version.id

    context = TenantContext(tenant_id, "integration:provider", uuid4())
    transport = _SwitchableAsgiTransport()
    client = _client(transport)
    service = ToolProviderService(
        database,
        settings,
        client=client,
        approval_required_risks=frozenset(),
    )
    await service.register_tool_definition(context, spec)
    registered = await service.register(
        context,
        ExternalToolProviderCreate(
            name=f"stub.{suffix}",
            project_id=project_id,
            endpoint_ref="http://stub.localhost:18080",
            credential_ref=CREDENTIAL_REF,
        ),
    )
    provider = FakeToolProvider(provider_id=str(registered.id), secret=SECRET)
    transport.provider = provider
    verified = await service.verify(context, registered.id)
    assert verified.status == "active"
    control_plane = ControlPlaneService(
        database,
        settings=settings,
        tool_provider_service=service,
    )
    run = await control_plane.create_run(
        context,
        task_id,
        RunCreate(
            timeout_seconds=30,
            tool_bindings=[
                RunToolBindingCreate.model_validate(
                    {
                        "provider_id": str(registered.id),
                        "tool": {"name": spec.name, "version": spec.version},
                        "policy": {
                            "timeout_seconds": provider_timeout_seconds,
                            "max_calls": max_calls,
                            "max_total_duration": max_total_duration_seconds,
                            "max_single_call_duration": provider_timeout_seconds,
                            "retry": {"max_attempts": retry_attempts},
                            "approval_mode": approval_mode,
                            "cancel_timeout_ms": 1_000,
                        },
                    }
                )
            ],
        ),
    )
    snapshot = run.tool_binding_snapshot["bindings"][0]
    provider.provision_scope(
        FakeToolProviderScope(
            binding_digest=snapshot["binding_digest"],
            tenant_id=tenant_id,
            project_id=project_id,
            run_id=run.id,
            task_id=task_id,
            agent_id=agent_id,
            agent_version_id=version_id,
            tool=FAKE_ECHO_TOOL,
        )
    )
    return _Setup(
        settings=settings,
        database=database,
        context=context,
        service=service,
        client=client,
        transport=transport,
        provider=provider,
        provider_id=registered.id,
        project_id=project_id,
        task_id=task_id,
        agent_id=agent_id,
        agent_version_id=version_id,
        run_id=run.id,
        binding_id=UUID(snapshot["binding_id"]),
        binding_digest=snapshot["binding_digest"],
        spec=spec,
    )


def _gateway(setup: _Setup, fallback: _LocalFallbackExecutor) -> ToolGateway:
    return ToolGateway(
        setup.database,
        ToolRegistry([fallback]),
        approval_required_risks=frozenset(),
        external_resolver=ExternalToolResolver(setup.database, setup.client),
    )


async def _new_task(setup: _Setup, *, project_id: UUID | None = None) -> UUID:
    async with setup.database.admin_transaction() as session:
        task = Task(
            tenant_id=setup.context.tenant_id,
            project_id=project_id or setup.project_id,
            assignee_agent_id=setup.agent_id,
            title=f"External Provider validation {uuid4()}",
            input={"request": "validate binding"},
            status="assigned",
            priority=0,
        )
        session.add(task)
        await session.flush()
        return task.id


def _run_create(
    setup: _Setup,
    *,
    provider_id: UUID | None = None,
    tool_name: str | None = None,
    tool_version: str | None = None,
) -> RunCreate:
    return RunCreate.model_validate(
        {
            "timeout_seconds": 30,
            "tool_bindings": [
                {
                    "provider_id": str(provider_id or setup.provider_id),
                    "tool": {
                        "name": tool_name or setup.spec.name,
                        "version": tool_version or setup.spec.version,
                    },
                    "policy": {
                        "timeout_seconds": 5,
                        "max_calls": 2,
                        "max_total_duration": 10,
                        "max_single_call_duration": 5,
                        "retry": {"max_attempts": 1},
                        "approval_mode": "never",
                    },
                }
            ],
        }
    )


def _control(setup: _Setup) -> ControlPlaneService:
    return ControlPlaneService(
        setup.database,
        settings=setup.settings,
        tool_provider_service=setup.service,
    )


def _checkpoint(tool_name: str) -> dict[str, Any]:
    value = {
        "schema_version": 2,
        "execution_mode": "react",
        "loop_state": "waiting_for_tool",
        "pending_actions": [{"kind": "tool", "name": tool_name}],
    }
    return {**value, "checkpoint_hash": canonical_hash(value)}


@pytest.mark.asyncio
async def test_runtime_consumes_provider_result_and_never_falls_back_locally() -> None:
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        setup = await _seed_setup(database)
        fallback = _LocalFallbackExecutor(setup.spec)
        worker = RuntimeWorker(
            database,
            RuntimeProviderRegistry([MockRuntimeProvider()]),
            worker_id=f"external-runtime-{uuid4()}",
            lease_seconds=5,
            heartbeat_seconds=0.05,
            tool_gateway=_gateway(setup, fallback),
        )

        assert await worker.execute_once() is True

        async with database.admin_transaction() as session:
            run = await session.get(Run, setup.run_id)
            runtime = await session.scalar(
                select(RuntimeSession).where(RuntimeSession.run_id == setup.run_id)
            )
            binding = await session.get(RunToolBinding, setup.binding_id)
            call = await session.scalar(select(ToolCall).where(ToolCall.run_id == setup.run_id))
            events = list(
                await session.scalars(
                    select(Event).where(Event.run_id == setup.run_id).order_by(Event.sequence)
                )
            )

        assert run is not None and run.status == RunStatus.COMPLETED.value
        assert run.result is not None
        tool_result = run.result["tool_results"][0]
        assert tool_result["status"] == "succeeded"
        assert tool_result["output"] == {"echo": {"message": "runtime consumed this"}}
        assert runtime is not None
        assert runtime.trajectory["messages"][-2]["role"] == "tool"
        assert runtime.trajectory["messages"][-2]["content"]["output"] == tool_result["output"]
        assert binding is not None and binding.status == "completed"
        assert binding.call_count == 1
        assert call is not None and call.provider_status == "succeeded"
        assert call.provider_execution_id is not None
        assert setup.provider.count("executions") == 1
        assert fallback.calls == 0
        event_types = {event.event_type for event in events}
        assert {
            "tool.binding.resolved",
            "tool.provider.requested",
            "tool.provider.started",
            "tool.provider.completed",
            "tool.binding.finalized",
        } <= event_types
        provider_events = [
            event for event in events if event.event_type.startswith("tool.provider")
        ]
        assert all(event.payload.get("task_id") == str(setup.task_id) for event in provider_events)
        serialized = str(
            [
                run.tool_binding_snapshot,
                run.result,
                runtime.trajectory,
                [event.payload for event in events],
            ]
        )
        assert SECRET not in serialized
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approval_mode", "requires_approval"),
    [("never", False), ("inherit", False), ("always", True)],
)
async def test_binding_approval_modes_are_enforced_at_call_time(
    approval_mode: str,
    requires_approval: bool,
) -> None:
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        setup = await _seed_setup(
            database,
            tool_calls=[],
            approval_mode=approval_mode,
        )
        worker_id = f"external-approval-{approval_mode}-{uuid4()}"
        claim = await database.claim_next_run(worker_id, 5)
        assert claim is not None and claim.run_id == setup.run_id
        await RuntimeExecutionService(database).prepare_claim(
            claim,
            worker_id=worker_id,
            registry=RuntimeProviderRegistry([MockRuntimeProvider()]),
        )
        fallback = _LocalFallbackExecutor(setup.spec)
        request = ToolGatewayRequest(
            tool_name=setup.spec.name,
            tool_version=setup.spec.version,
            arguments={"approval": approval_mode},
            idempotency_key=f"approval-{approval_mode}",
            caller="runtime:mock",
            checkpoint=_checkpoint(setup.spec.name),
        )

        if requires_approval:
            with pytest.raises(ToolApprovalRequired):
                await _gateway(setup, fallback).execute(
                    claim,
                    worker_id=worker_id,
                    request=request,
                )
            assert setup.provider.count("executions") == 0
        else:
            result = await _gateway(setup, fallback).execute(
                claim,
                worker_id=worker_id,
                request=request,
            )
            assert result.status is ToolCallStatus.SUCCEEDED
            assert setup.provider.count("executions") == 1
        assert fallback.calls == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_run_creation_rejects_allowlist_version_and_unknown_provider() -> None:
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        setup = await _seed_setup(database, tool_calls=[])
        control = _control(setup)
        version_task = await _new_task(setup)
        with pytest.raises(ToolProviderError) as version_error:
            await control.create_run(
                setup.context,
                version_task,
                _run_create(setup, tool_version="1.0.1"),
            )
        assert version_error.value.code == "TOOL_BINDING_VERSION_MISMATCH"

        unknown_task = await _new_task(setup)
        with pytest.raises(ToolProviderError) as unknown_provider:
            await control.create_run(
                setup.context,
                unknown_task,
                _run_create(setup, provider_id=uuid4()),
            )
        assert unknown_provider.value.code == "TOOL_PROVIDER_NOT_FOUND"

        unlisted_spec = setup.spec.model_copy(
            update={
                "name": "nico.stub.unlisted",
                "description": "Registered but absent from AgentVersion allowlist",
            }
        )
        await setup.service.register_tool_definition(setup.context, unlisted_spec)
        allowlist_task = await _new_task(setup)
        with pytest.raises(ToolAccessDenied) as allowlist:
            await control.create_run(
                setup.context,
                allowlist_task,
                _run_create(setup, tool_name=unlisted_spec.name),
            )
        assert allowlist.value.code == "TOOL_ACCESS_DENIED"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_run_creation_rejects_disabled_and_cross_project_provider() -> None:
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        setup = await _seed_setup(database, tool_calls=[])
        async with database.admin_transaction() as session:
            other_project = Project(
                tenant_id=setup.context.tenant_id,
                name=f"other-provider-project-{uuid4()}",
            )
            session.add(other_project)
            await session.flush()
            other_project_id = other_project.id
        scoped_task = await _new_task(setup, project_id=other_project_id)
        with pytest.raises(ToolProviderError) as scope_error:
            await _control(setup).create_run(
                setup.context,
                scoped_task,
                _run_create(setup),
            )
        assert scope_error.value.code == "TOOL_PROVIDER_SCOPE_MISMATCH"

        await setup.service.disable(setup.context, setup.provider_id)
        disabled_task = await _new_task(setup)
        with pytest.raises(ToolProviderError) as disabled:
            await _control(setup).create_run(
                setup.context,
                disabled_task,
                _run_create(setup),
            )
        assert disabled.value.code == "TOOL_PROVIDER_DISABLED"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_provider_reverification_is_idempotent_and_refresh_requires_disable() -> None:
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        setup = await _seed_setup(database, tool_calls=[])
        original = await setup.service.get(setup.context, setup.provider_id)

        unchanged = await setup.service.verify(setup.context, setup.provider_id)
        assert unchanged.revision == original.revision
        assert unchanged.capability_digest == original.capability_digest

        changed_tool = ProviderToolContract(
            name=FAKE_ECHO_TOOL.name,
            version=FAKE_ECHO_TOOL.version,
            input_schema_digest=FAKE_ECHO_TOOL.input_schema_digest,
            output_schema_digest=f"sha256:{'e' * 64}",
        )
        setup.provider.supported_tools = (changed_tool,)
        with pytest.raises(DomainConflict) as active_refresh:
            await setup.service.verify(setup.context, setup.provider_id)
        assert active_refresh.value.code == "TOOL_PROVIDER_CAPABILITY_CHANGE_REQUIRES_DISABLE"

        disabled = await setup.service.disable(setup.context, setup.provider_id)
        refreshed = await setup.service.verify(setup.context, setup.provider_id)
        assert disabled.status == "disabled"
        assert refreshed.status == "active"
        assert refreshed.capability_digest != original.capability_digest
        assert refreshed.revision == disabled.revision + 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_run_binding_rechecks_provider_after_unlocked_health_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        setup = await _seed_setup(database, tool_calls=[])
        original_health = setup.service.client.health
        changed = False

        async def disable_during_health(endpoint):
            nonlocal changed
            if not changed:
                changed = True
                await setup.service.disable(setup.context, setup.provider_id)
            return await original_health(endpoint)

        monkeypatch.setattr(setup.service.client, "health", disable_during_health)
        task_id = await _new_task(setup)
        with pytest.raises(DomainConflict) as conflict:
            await _control(setup).create_run(
                setup.context,
                task_id,
                _run_create(setup),
            )
        assert conflict.value.code == "TOOL_PROVIDER_REVISION_CONFLICT"
        assert (await setup.service.get(setup.context, setup.provider_id)).status == "disabled"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_run_creation_rejects_schema_mismatch_and_expired_provider() -> None:
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        setup = await _seed_setup(database, tool_calls=[])
        mismatched = ProviderToolContract(
            name=FAKE_ECHO_TOOL.name,
            version=FAKE_ECHO_TOOL.version,
            input_schema_digest=FAKE_ECHO_TOOL.input_schema_digest,
            output_schema_digest=f"sha256:{'d' * 64}",
        )
        registered = await setup.service.register(
            setup.context,
            ExternalToolProviderCreate(
                name=f"schema-mismatch.{uuid4().hex[:8]}",
                project_id=setup.project_id,
                endpoint_ref="http://stub.localhost:18080",
                credential_ref=CREDENTIAL_REF,
            ),
        )
        mismatch_provider = FakeToolProvider(
            provider_id=str(registered.id),
            secret=SECRET,
            supported_tools=(mismatched,),
        )
        setup.transport.provider = mismatch_provider
        await setup.service.verify(setup.context, registered.id)
        schema_task = await _new_task(setup)
        with pytest.raises(ToolProviderError) as schema_error:
            await _control(setup).create_run(
                setup.context,
                schema_task,
                _run_create(setup, provider_id=registered.id),
            )
        assert schema_error.value.code == "TOOL_PROVIDER_SCHEMA_MISMATCH"

        expiring = await setup.service.register(
            setup.context,
            ExternalToolProviderCreate(
                name=f"expiring.{uuid4().hex[:8]}",
                project_id=setup.project_id,
                endpoint_ref="http://stub.localhost:18080",
                credential_ref=CREDENTIAL_REF,
                expires_at=datetime.now(UTC) + timedelta(milliseconds=500),
            ),
        )
        expiring_provider = FakeToolProvider(provider_id=str(expiring.id), secret=SECRET)
        setup.transport.provider = expiring_provider
        await setup.service.verify(setup.context, expiring.id)
        await asyncio.sleep(0.6)
        expired_task = await _new_task(setup)
        with pytest.raises(ToolProviderError) as expired:
            await _control(setup).create_run(
                setup.context,
                expired_task,
                _run_create(setup, provider_id=expiring.id),
            )
        assert expired.value.code == "TOOL_PROVIDER_EXPIRED"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_verify_persists_expiry_transition_and_event_before_error() -> None:
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        setup = await _seed_setup(database, tool_calls=[])
        expiring = await setup.service.register(
            setup.context,
            ExternalToolProviderCreate(
                name=f"verify-expiry.{uuid4().hex[:8]}",
                project_id=setup.project_id,
                endpoint_ref="http://stub.localhost:18080",
                credential_ref=CREDENTIAL_REF,
                expires_at=datetime.now(UTC) + timedelta(milliseconds=100),
            ),
        )
        setup.transport.provider = FakeToolProvider(
            provider_id=str(expiring.id),
            secret=SECRET,
        )
        await asyncio.sleep(0.15)

        with pytest.raises(ToolProviderError) as error:
            await setup.service.verify(setup.context, expiring.id)
        assert error.value.code == "TOOL_PROVIDER_EXPIRED"

        async with database.admin_transaction() as session:
            provider = await session.get(ExternalToolProvider, expiring.id)
            event = await session.scalar(
                select(Event).where(
                    Event.aggregate_id == expiring.id,
                    Event.event_type == "tool.provider.expired",
                )
            )
        assert provider is not None and provider.status == "expired"
        assert event is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_registration_recognizes_numeric_ipv4_and_ipv6_loopback() -> None:
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        setup = await _seed_setup(database, tool_calls=[])
        for label, endpoint in (
            ("ipv4", "http://127.0.0.1:18080"),
            ("ipv6", "http://[::1]:18080"),
        ):
            provider = await setup.service.register(
                setup.context,
                ExternalToolProviderCreate(
                    name=f"{label}.{uuid4().hex[:8]}",
                    project_id=setup.project_id,
                    endpoint_ref=endpoint,
                    credential_ref=CREDENTIAL_REF,
                ),
            )
            assert provider.status == "registered"
            assert provider.endpoint_ref == endpoint
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_public_api_redacts_credentials_and_enforces_tenant_scope() -> None:
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        setup = await _seed_setup(database, tool_calls=[])
        app = create_app(settings=settings, health_service=object(), database=database)
        headers = {
            "X-Tenant-ID": str(setup.context.tenant_id),
            "X-Actor-ID": setup.context.actor_id,
        }
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://nico.test",
            ) as api,
        ):
            provider_response = await api.get(
                f"/api/v1/external-tool-providers/{setup.provider_id}",
                headers=headers,
            )
            run_response = await api.get(f"/api/v1/runs/{setup.run_id}", headers=headers)
            missing_response = await api.get(
                f"/api/v1/external-tool-providers/{uuid4()}",
                headers=headers,
            )
            other_tenant_id = uuid4()
            async with database.admin_transaction() as session:
                session.add(
                    Tenant(
                        id=other_tenant_id,
                        name=f"Other tenant {other_tenant_id}",
                        slug=f"other-{other_tenant_id.hex[:12]}",
                    )
                )
            cross_tenant = await api.get(
                f"/api/v1/external-tool-providers/{setup.provider_id}",
                headers={**headers, "X-Tenant-ID": str(other_tenant_id)},
            )

        assert provider_response.status_code == 200
        assert provider_response.json()["credential_ref"] == "env:[REDACTED]"
        assert run_response.status_code == 200
        assert (
            run_response.json()["tool_binding_snapshot"]["bindings"][0]["credential_ref"]
            == "[REDACTED]"
        )
        assert SECRET not in provider_response.text
        assert SECRET not in run_response.text
        assert missing_response.status_code == 404
        assert missing_response.json()["code"] == "TOOL_PROVIDER_NOT_FOUND"
        assert cross_tenant.status_code == 404
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_code", "expected_event", "delay_seconds"),
    [
        (
            FakeToolProviderMode.SERVER_ERROR,
            "TOOL_PROVIDER_CONNECTION_ERROR",
            "tool.provider.failed",
            0,
        ),
        (
            FakeToolProviderMode.RATE_LIMIT,
            "TOOL_PROVIDER_RATE_LIMIT",
            "tool.provider.failed",
            0,
        ),
        (
            FakeToolProviderMode.MALFORMED_RESPONSE,
            "TOOL_PROVIDER_INVALID_RESPONSE",
            "tool.provider.protocol_error",
            0,
        ),
        (
            FakeToolProviderMode.TIMEOUT,
            "TOOL_PROVIDER_TIMEOUT",
            "tool.provider.failed",
            0.01,
        ),
        (
            FakeToolProviderMode.INVALID_SCHEMA,
            "TOOL_OUTPUT_INVALID",
            "tool.provider.protocol_error",
            0,
        ),
    ],
)
async def test_provider_failure_has_structured_error_and_no_local_fallback(
    mode: FakeToolProviderMode,
    expected_code: str,
    expected_event: str,
    delay_seconds: float,
) -> None:
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        setup = await _seed_setup(database)
        setup.provider.set_mode(mode, delay_seconds=delay_seconds)
        fallback = _LocalFallbackExecutor(setup.spec)
        worker = RuntimeWorker(
            database,
            RuntimeProviderRegistry([MockRuntimeProvider()]),
            worker_id=f"external-failure-{uuid4()}",
            lease_seconds=5,
            heartbeat_seconds=0.05,
            tool_gateway=_gateway(setup, fallback),
        )

        assert await worker.execute_once() is True

        async with database.admin_transaction() as session:
            run = await session.get(Run, setup.run_id)
            call = await session.scalar(select(ToolCall).where(ToolCall.run_id == setup.run_id))
            events = list(await session.scalars(select(Event).where(Event.run_id == setup.run_id)))
        assert run is not None and run.status == RunStatus.FAILED.value
        assert call is not None and call.status == ToolCallStatus.FAILED.value
        assert call.error["code"] == expected_code
        assert expected_event in {event.event_type for event in events}
        assert fallback.calls == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_restart_reuses_request_id_and_provider_idempotency_result() -> None:
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        setup = await _seed_setup(database, tool_calls=[])
        fallback = _LocalFallbackExecutor(setup.spec)
        worker_one = f"external-crash-one-{uuid4()}"
        claim_one = await database.claim_next_run(worker_one, 5)
        assert claim_one is not None and claim_one.run_id == setup.run_id
        await RuntimeExecutionService(database).prepare_claim(
            claim_one,
            worker_id=worker_one,
            registry=RuntimeProviderRegistry([MockRuntimeProvider()]),
        )
        gateway_one = _gateway(setup, fallback)
        request = ToolGatewayRequest(
            tool_name=setup.spec.name,
            tool_version=setup.spec.version,
            arguments={"message": "execute once"},
            idempotency_key="crash-window-key",
            caller="runtime:mock",
        )
        executor = await gateway_one.external_resolver.resolve(
            claim_one,
            worker_id=worker_one,
            tool_name=setup.spec.name,
            tool_version=setup.spec.version,
        )
        assert executor is not None
        prepared = await gateway_one._begin_call(
            claim_one,
            worker_id=worker_one,
            request=request,
            executor=executor,
            arguments_hash=canonical_hash(request.arguments),
        )
        assert not isinstance(prepared, ToolGatewayResult)
        assert hasattr(prepared, "context")
        first_execution = await executor.execute(
            prepared.context.model_copy(update={"attempt": 1}),
            request.arguments,
            {},
        )
        assert first_execution.output == {"echo": {"message": "execute once"}}

        async with database.admin_transaction() as session:
            completed_events = list(
                await session.scalars(
                    select(Event).where(
                        Event.run_id == setup.run_id,
                        Event.event_type == "tool.provider.completed",
                    )
                )
            )
            assert completed_events == []
            run = await session.get(Run, setup.run_id, with_for_update=True)
            assert run is not None
            run.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

        worker_two = f"external-crash-two-{uuid4()}"
        claim_two = await database.claim_next_run(worker_two, 5)
        assert claim_two is not None and claim_two.run_id == setup.run_id
        restarted = _gateway(setup, fallback)
        result = await restarted.execute(claim_two, worker_id=worker_two, request=request)

        async with database.admin_transaction() as session:
            binding = await session.get(RunToolBinding, setup.binding_id)
            call = await session.scalar(select(ToolCall).where(ToolCall.run_id == setup.run_id))
        assert result.status is ToolCallStatus.SUCCEEDED
        assert result.usage["idempotency_replayed"] is True
        assert binding is not None and binding.call_count == 1
        assert call is not None and call.provider_request_id is not None
        assert setup.provider.count("execute") == 2
        assert setup.provider.count("executions") == 1
        assert setup.provider.count("idempotency_replay") == 1
        assert fallback.calls == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_lost_response_after_provider_success_retries_without_second_side_effect() -> None:
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        setup = await _seed_setup(database, tool_calls=[], retry_attempts=2)
        setup.transport.fail_after_execute_once = True
        fallback = _LocalFallbackExecutor(setup.spec)
        worker_id = f"external-response-loss-{uuid4()}"
        claim = await database.claim_next_run(worker_id, 5)
        assert claim is not None and claim.run_id == setup.run_id
        await RuntimeExecutionService(database).prepare_claim(
            claim,
            worker_id=worker_id,
            registry=RuntimeProviderRegistry([MockRuntimeProvider()]),
        )

        result = await _gateway(setup, fallback).execute(
            claim,
            worker_id=worker_id,
            request=ToolGatewayRequest(
                tool_name=setup.spec.name,
                tool_version=setup.spec.version,
                arguments={"message": "response was lost once"},
                idempotency_key="lost-response-key",
                caller="runtime:mock",
            ),
        )

        assert result.status is ToolCallStatus.SUCCEEDED
        assert result.output == {"echo": {"message": "response was lost once"}}
        assert result.attempts == 2
        assert result.usage["idempotency_replayed"] is True
        assert setup.provider.count("execute") == 2
        assert setup.provider.count("executions") == 1
        assert setup.provider.count("idempotency_replay") == 1
        assert fallback.calls == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_run_cancel_sends_bounded_provider_cancel_and_records_acknowledgement() -> None:
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        setup = await _seed_setup(database)
        setup.provider.set_mode(FakeToolProviderMode.DELAY, delay_seconds=1)
        fallback = _LocalFallbackExecutor(setup.spec)
        worker = RuntimeWorker(
            database,
            RuntimeProviderRegistry([MockRuntimeProvider()]),
            worker_id=f"external-cancel-{uuid4()}",
            lease_seconds=5,
            heartbeat_seconds=0.05,
            tool_gateway=_gateway(setup, fallback),
        )
        executing = asyncio.create_task(worker.execute_once())
        await asyncio.wait_for(setup.provider.execution_started.wait(), timeout=2)
        async with database.admin_transaction() as session:
            run = await session.get(Run, setup.run_id)
            assert run is not None
            revision = run.revision

        control_plane = ControlPlaneService(
            database,
            settings=settings,
            tool_provider_service=setup.service,
        )
        cancelled = await control_plane.cancel_run(
            setup.context,
            setup.run_id,
            expected_revision=revision,
        )
        await asyncio.wait_for(setup.provider.cancel_received.wait(), timeout=2)
        await asyncio.wait_for(executing, timeout=3)

        async with database.admin_transaction() as session:
            run = await session.get(Run, setup.run_id)
            binding = await session.get(RunToolBinding, setup.binding_id)
            call = await session.scalar(select(ToolCall).where(ToolCall.run_id == setup.run_id))
            events = list(await session.scalars(select(Event).where(Event.run_id == setup.run_id)))
        assert cancelled.status == RunStatus.CANCELLED.value
        assert run is not None and run.status == RunStatus.CANCELLED.value
        assert binding is not None and binding.status == "revoked"
        assert binding.cancel_status == "cancelled"
        assert binding.active_provider_request_id is None
        assert call is not None and call.status == ToolCallStatus.CANCELLED.value
        assert call.provider_status == "cancelled"
        assert call.external_execution_may_continue is False
        assert setup.provider.count("cancel") >= 1
        assert "tool.provider.cancel.requested" in {event.event_type for event in events}
        assert "tool.provider.cancelled" in {event.event_type for event in events}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_timeout_with_uncertain_cancel_stops_retry_and_marks_external_execution() -> None:
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        setup = await _seed_setup(
            database,
            tool_calls=[],
            retry_attempts=2,
            provider_timeout_seconds=1,
            max_total_duration_seconds=2,
        )
        setup.provider.set_mode(FakeToolProviderMode.DELAY, delay_seconds=2)
        setup.provider.set_cancel_uncertain()
        worker_id = f"external-uncertain-timeout-{uuid4()}"
        claim = await database.claim_next_run(worker_id, 5)
        assert claim is not None and claim.run_id == setup.run_id
        await RuntimeExecutionService(database).prepare_claim(
            claim,
            worker_id=worker_id,
            registry=RuntimeProviderRegistry([MockRuntimeProvider()]),
        )

        result = await _gateway(setup, _LocalFallbackExecutor(setup.spec)).execute(
            claim,
            worker_id=worker_id,
            request=ToolGatewayRequest(
                tool_name=setup.spec.name,
                tool_version=setup.spec.version,
                arguments={"message": "uncertain cancellation"},
                idempotency_key="uncertain-timeout",
                caller="runtime:mock",
            ),
        )

        async with database.admin_transaction() as session:
            binding = await session.get(RunToolBinding, setup.binding_id)
            call = await session.scalar(select(ToolCall).where(ToolCall.run_id == setup.run_id))
            events = list(await session.scalars(select(Event).where(Event.run_id == setup.run_id)))
        event_types = {event.event_type for event in events}
        assert result.status is ToolCallStatus.TIMED_OUT
        assert result.attempts == 1
        assert binding is not None and binding.cancel_status == "unknown"
        assert call is not None and call.provider_status == "unknown"
        assert call.external_execution_may_continue is True
        assert setup.provider.count("execute") == 1
        assert setup.provider.count("cancel") == 1
        assert "tool.provider.cancel.requested" in event_types
        assert "tool.provider.failed" in event_types
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_binding_serializes_distinct_in_flight_provider_requests() -> None:
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        setup = await _seed_setup(database, tool_calls=[])
        fallback = _LocalFallbackExecutor(setup.spec)
        gateway = _gateway(setup, fallback)
        worker_id = f"external-serialized-binding-{uuid4()}"
        claim = await database.claim_next_run(worker_id, 5)
        assert claim is not None and claim.run_id == setup.run_id
        await RuntimeExecutionService(database).prepare_claim(
            claim,
            worker_id=worker_id,
            registry=RuntimeProviderRegistry([MockRuntimeProvider()]),
        )
        executor = await gateway.external_resolver.resolve(
            claim,
            worker_id=worker_id,
            tool_name=setup.spec.name,
            tool_version=setup.spec.version,
        )
        assert executor is not None
        first = ToolGatewayRequest(
            tool_name=setup.spec.name,
            tool_version=setup.spec.version,
            arguments={"index": 1},
            idempotency_key="serialized-first",
            caller="runtime:mock",
        )
        prepared = await gateway._begin_call(
            claim,
            worker_id=worker_id,
            request=first,
            executor=executor,
            arguments_hash=canonical_hash(first.arguments),
        )
        assert not isinstance(prepared, ToolGatewayResult)

        with pytest.raises(ToolProviderError) as conflict:
            await gateway.execute(
                claim,
                worker_id=worker_id,
                request=ToolGatewayRequest(
                    tool_name=setup.spec.name,
                    tool_version=setup.spec.version,
                    arguments={"index": 2},
                    idempotency_key="serialized-second",
                    caller="runtime:mock",
                ),
            )
        assert conflict.value.context.cause == "BINDING_CALL_IN_PROGRESS"
        async with database.admin_transaction() as session:
            binding = await session.get(RunToolBinding, setup.binding_id)
            calls = list(
                await session.scalars(select(ToolCall).where(ToolCall.run_id == setup.run_id))
            )
        assert binding is not None and binding.call_count == 1
        assert len(calls) == 1
        assert setup.provider.count("execute") == 0
        assert fallback.calls == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_total_duration_deadline_prevents_second_provider_attempt() -> None:
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        setup = await _seed_setup(
            database,
            tool_calls=[],
            retry_attempts=2,
            provider_timeout_seconds=1,
            max_total_duration_seconds=1,
        )
        setup.provider.set_mode(FakeToolProviderMode.DELAY, delay_seconds=2)
        worker_id = f"external-duration-budget-{uuid4()}"
        claim = await database.claim_next_run(worker_id, 5)
        assert claim is not None and claim.run_id == setup.run_id
        await RuntimeExecutionService(database).prepare_claim(
            claim,
            worker_id=worker_id,
            registry=RuntimeProviderRegistry([MockRuntimeProvider()]),
        )

        result = await _gateway(setup, _LocalFallbackExecutor(setup.spec)).execute(
            claim,
            worker_id=worker_id,
            request=ToolGatewayRequest(
                tool_name=setup.spec.name,
                tool_version=setup.spec.version,
                arguments={"message": "one duration budget"},
                idempotency_key="duration-budget",
                caller="runtime:mock",
            ),
        )

        async with database.admin_transaction() as session:
            binding = await session.get(RunToolBinding, setup.binding_id)
        assert result.status is ToolCallStatus.TIMED_OUT
        assert result.attempts == 1
        assert setup.provider.count("execute") == 1
        assert binding is not None
        assert binding.total_duration_ms <= binding.max_total_duration_ms
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_binding_budget_is_durable_and_exhaustion_is_fail_closed() -> None:
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        setup = await _seed_setup(database, tool_calls=[], max_calls=1)
        fallback = _LocalFallbackExecutor(setup.spec)
        worker_id = f"external-budget-{uuid4()}"
        claim = await database.claim_next_run(worker_id, 5)
        assert claim is not None
        await RuntimeExecutionService(database).prepare_claim(
            claim,
            worker_id=worker_id,
            registry=RuntimeProviderRegistry([MockRuntimeProvider()]),
        )
        gateway = _gateway(setup, fallback)
        first = await gateway.execute(
            claim,
            worker_id=worker_id,
            request=ToolGatewayRequest(
                tool_name=setup.spec.name,
                tool_version=setup.spec.version,
                arguments={"index": 1},
                idempotency_key="budget-first",
                caller="runtime:mock",
            ),
        )
        assert first.status is ToolCallStatus.SUCCEEDED

        with pytest.raises(ToolProviderError) as captured:
            await gateway.execute(
                claim,
                worker_id=worker_id,
                request=ToolGatewayRequest(
                    tool_name=setup.spec.name,
                    tool_version=setup.spec.version,
                    arguments={"index": 2},
                    idempotency_key="budget-second",
                    caller="runtime:mock",
                ),
            )
        assert captured.value.code == "TOOL_BINDING_BUDGET_EXCEEDED"
        async with database.admin_transaction() as session:
            binding = await session.get(RunToolBinding, setup.binding_id)
        assert binding is not None and binding.call_count == 1
        assert setup.provider.count("executions") == 1
        assert fallback.calls == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_terminal_run_and_mutated_binding_snapshot_are_rejected() -> None:
    settings = _settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        setup = await _seed_setup(database, tool_calls=[])
        worker_id = f"external-terminal-{uuid4()}"
        claim = await database.claim_next_run(worker_id, 5)
        assert claim is not None
        await RuntimeExecutionService(database).prepare_claim(
            claim,
            worker_id=worker_id,
            registry=RuntimeProviderRegistry([MockRuntimeProvider()]),
        )
        control = ControlPlaneService(
            database,
            settings=settings,
            tool_provider_service=setup.service,
        )
        async with database.admin_transaction() as session:
            run = await session.get(Run, setup.run_id)
            assert run is not None
            revision = run.revision
        await control.transition_run(
            setup.context,
            setup.run_id,
            command=RunTransition(
                target=RunStatus.COMPLETED,
                expected_revision=revision,
                result={"done": True},
            ),
        )
        with pytest.raises((ToolLeaseLost, ToolProviderError)):
            await _gateway(setup, _LocalFallbackExecutor(setup.spec)).execute(
                claim,
                worker_id=worker_id,
                request=ToolGatewayRequest(
                    tool_name=setup.spec.name,
                    tool_version=setup.spec.version,
                    arguments={"late": True},
                    idempotency_key="late-call",
                    caller="runtime:mock",
                ),
            )
        async with database.tenant_transaction(setup.context) as session:
            run = await session.get(Run, setup.run_id)
            assert run is not None
            mutated = dict(run.tool_binding_snapshot)
            mutated["bindings"] = []
            run.tool_binding_snapshot = mutated
            with pytest.raises(Exception, match="snapshot is immutable"):
                await session.flush()
    finally:
        await engine.dispose()
