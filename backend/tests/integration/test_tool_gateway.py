from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import Settings
from nico_agent.control_plane import ControlPlaneService
from nico_agent.database import Database, TenantContext
from nico_agent.domain.models import (
    Agent,
    AgentVersion,
    AuditRecord,
    Event,
    Project,
    Run,
    RunStep,
    RuntimeSession,
    Task,
    Tenant,
    ToolCall,
    ToolDefinition,
)
from nico_agent.domain.states import ToolCallStatus
from nico_agent.runtime import MockRuntimeProvider
from nico_agent.runtime.registry import RuntimeProviderRegistry
from nico_agent.runtime.service import RuntimeExecutionService
from nico_agent.tools import (
    ToolDefinitionSpec,
    ToolExecutionResult,
    ToolGateway,
    ToolGatewayRequest,
    ToolIsolation,
    ToolRegistry,
    ToolRetryPolicy,
)
from nico_agent.tools.errors import (
    ToolAccessDenied,
    ToolExecutorFailure,
    ToolIdempotencyConflict,
    ToolLeaseLost,
)
from nico_agent.tools.secrets import EnvironmentSecretResolver

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with the Compose dependencies running",
)


def _spec(
    *,
    timeout_seconds: int = 5,
    retry_policy: ToolRetryPolicy | None = None,
) -> ToolDefinitionSpec:
    return ToolDefinitionSpec(
        name="test.echo",
        version="1.0.0",
        description="Deterministic Gateway integration executor",
        input_schema={
            "type": "object",
            "properties": {
                "mode": {"type": "string"},
                "value": {"type": "string"},
                "password": {"type": "string"},
            },
            "required": ["mode"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "value": {"type": "string"},
                "secret_echo": {"type": "string"},
                "executor_call": {"type": "integer"},
            },
            "required": ["value", "secret_echo", "executor_call"],
            "additionalProperties": False,
        },
        permission="test.execute",
        timeout_seconds=timeout_seconds,
        retry_policy=retry_policy or ToolRetryPolicy(max_attempts=1),
        isolation=ToolIsolation.IN_PROCESS,
        secret_names=frozenset({"authorization"}),
    )


@dataclass
class _Executor:
    spec: ToolDefinitionSpec
    delay_seconds: float = 0
    calls: int = 0
    implementation_hash: str = "a" * 64
    seen_contexts: list[Any] = field(default_factory=list)

    async def execute(self, context, arguments, secrets):
        self.calls += 1
        self.seen_contexts.append(context)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if arguments["mode"] == "transient" and self.calls == 1:
            raise ToolExecutorFailure("TEMPORARY", "token=must-not-persist")
        if arguments["mode"] == "explode":
            raise RuntimeError("very-secret-value")
        return ToolExecutionResult(
            output={
                "value": arguments.get("value", "ok"),
                "secret_echo": secrets["authorization"],
                "executor_call": self.calls,
            },
            usage={"executor_calls": self.calls},
        )


@dataclass
class _ProviderExecutor(_Executor):
    seen_secrets: list[dict[str, str]] = field(default_factory=list)

    def required_secret_names(self, tool_config):
        provider = tool_config.get("provider")
        if provider == "brave":
            return frozenset({"authorization"})
        if provider == "searxng":
            return frozenset()
        raise ToolAccessDenied(self.spec.name, self.spec.version, "unknown provider")

    async def execute(self, context, arguments, secrets):
        self.calls += 1
        self.seen_contexts.append(context)
        self.seen_secrets.append(secrets)
        return ToolExecutionResult(
            output={
                "value": arguments.get("value", "ok"),
                "secret_echo": secrets.get("authorization", "none"),
                "executor_call": self.calls,
            },
            usage={"executor_calls": self.calls},
        )


async def _seed_gateway_run(
    database: Database,
    spec: ToolDefinitionSpec,
    *,
    worker_id: str,
    authorize: bool = True,
    priority: int = 0,
    tool_config: dict[str, Any] | None = None,
    secret_refs: dict[str, str] | None = None,
    agent_secret_names: list[str] | None = None,
):
    suffix = uuid4().hex[:10]
    tool_config = {"scope": "tenant", "max_items": 5} if tool_config is None else tool_config
    secret_refs = (
        {"authorization": "env:NICO_TOOL_SECRET_TEST_AUTH"} if secret_refs is None else secret_refs
    )
    agent_secret_names = ["authorization"] if agent_secret_names is None else agent_secret_names
    tenant_policy = {
        "allow": [spec.reference],
        "permissions": [spec.permission],
        "secret_refs": secret_refs,
        "tools": {spec.reference: tool_config},
    }
    agent_policy = {
        "allow": [spec.reference],
        "permissions": [spec.permission],
        "secrets": agent_secret_names,
        "tools": {spec.reference: tool_config},
    }
    if not authorize:
        agent_policy = {}
    async with database.admin_transaction() as session:
        tenant = Tenant(
            name=f"Gateway {suffix}",
            slug=f"gateway-{suffix}",
            settings={"tool_policy": tenant_policy},
        )
        session.add(tenant)
        await session.flush()
        project = Project(tenant_id=tenant.id, name=f"project-{suffix}")
        agent = Agent(
            tenant_id=tenant.id,
            name=f"agent-{suffix}",
            display_name="Gateway Agent",
        )
        session.add_all([project, agent])
        await session.flush()
        version = AgentVersion(
            tenant_id=tenant.id,
            agent_id=agent.id,
            version=1,
            status="published",
            role="tool-gateway-test",
            mandate="Execute a deterministic tool call",
            tool_policy=agent_policy,
            run_config={"runtime_provider": "mock"},
            content_hash="b" * 64,
        )
        session.add(version)
        await session.flush()
        agent.current_version_id = version.id
        agent.status = "ready"
        task = Task(
            tenant_id=tenant.id,
            project_id=project.id,
            assignee_agent_id=agent.id,
            title="Gateway integration",
            status="running",
            priority=priority,
        )
        session.add(task)
        await session.flush()
        run = Run(
            tenant_id=tenant.id,
            task_id=task.id,
            agent_id=agent.id,
            agent_version_id=version.id,
            attempt=1,
        )
        session.add(run)
        await session.flush()

    claim = await database.claim_next_run(worker_id, 5)
    assert claim is not None and claim.run_id == run.id
    await RuntimeExecutionService(database).prepare_claim(
        claim,
        worker_id=worker_id,
        registry=RuntimeProviderRegistry([MockRuntimeProvider()]),
    )
    return claim


def _gateway(
    database: Database,
    executor: _Executor,
    *,
    environment: dict[str, str] | None = None,
) -> ToolGateway:
    return ToolGateway(
        database,
        ToolRegistry([executor]),
        secret_resolver=EnvironmentSecretResolver(
            {"NICO_TOOL_SECRET_TEST_AUTH": "very-secret-value"}
            if environment is None
            else environment
        ),
    )


@pytest.mark.asyncio
async def test_gateway_success_is_redacted_audited_and_idempotent() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    spec = _spec()
    executor = _Executor(spec)
    worker_id = f"gateway-success-{uuid4()}"
    try:
        claim = await _seed_gateway_run(database, spec, worker_id=worker_id)
        gateway = _gateway(database, executor)
        request = ToolGatewayRequest(
            tool_name=spec.name,
            tool_version=spec.version,
            arguments={"mode": "success", "value": "hello", "password": "literal"},
            idempotency_key="same-call",
            caller="runtime:mock",
        )

        authorized = await gateway.list_authorized(claim, worker_id=worker_id)
        assert [item.reference for item in authorized] == [spec.reference]

        first = await gateway.execute(claim, worker_id=worker_id, request=request)
        second = await gateway.execute(claim, worker_id=worker_id, request=request)

        assert first.status is ToolCallStatus.SUCCEEDED
        assert first.output == {
            "value": "hello",
            "secret_echo": "[REDACTED]",
            "executor_call": 1,
        }
        assert second.cached is True
        assert second.tool_call_id == first.tool_call_id
        assert executor.calls == 1
        assert executor.seen_contexts[0].tool_config == {
            "scope": "tenant",
            "max_items": 5,
        }

        with pytest.raises(ToolIdempotencyConflict):
            await gateway.execute(
                claim,
                worker_id=worker_id,
                request=request.model_copy(
                    update={"arguments": {"mode": "success", "value": "different"}}
                ),
            )

        async with database.admin_transaction() as session:
            call = await session.get(ToolCall, first.tool_call_id)
            step = await session.get(RunStep, first.run_step_id)
            runtime_session = await session.scalar(
                select(RuntimeSession).where(RuntimeSession.run_id == claim.run_id)
            )
            definition_count = await session.scalar(
                select(func.count())
                .select_from(ToolDefinition)
                .where(ToolDefinition.tenant_id == claim.tenant_id)
            )
            event_count = await session.scalar(
                select(func.count()).select_from(Event).where(Event.run_id == claim.run_id)
            )
            audit_count = await session.scalar(
                select(func.count())
                .select_from(AuditRecord)
                .where(AuditRecord.resource_id == first.tool_call_id)
            )
        assert call is not None and call.arguments["password"] == "[REDACTED]"
        assert call.result == first.output
        assert step is not None and step.sequence == 1
        assert step.step_key == "tool:same-call"
        assert step.step_type == "tool"
        assert runtime_session is not None
        assert "very-secret-value" not in str(runtime_session.tool_policy_snapshot)
        assert runtime_session.tool_policy_snapshot["secret_refs"] == {
            "authorization": "env:NICO_TOOL_SECRET_TEST_AUTH"
        }
        assert definition_count == 1
        assert event_count >= 3
        assert audit_count == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_gateway_uses_provider_selected_secret_subset_for_list_and_execute() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    spec = _spec()
    executor = _ProviderExecutor(spec)
    worker_id = f"gateway-conditional-secret-{uuid4()}"
    try:
        claim = await _seed_gateway_run(
            database,
            spec,
            worker_id=worker_id,
            tool_config={"provider": "searxng"},
            secret_refs={},
            agent_secret_names=[],
        )
        gateway = _gateway(database, executor, environment={})

        authorized = await gateway.list_authorized(claim, worker_id=worker_id)
        result = await gateway.execute(
            claim,
            worker_id=worker_id,
            request=ToolGatewayRequest(
                tool_name=spec.name,
                tool_version=spec.version,
                arguments={"mode": "success"},
                idempotency_key="secretless-provider",
                caller="runtime:mock",
            ),
        )

        assert [item.reference for item in authorized] == [spec.reference]
        assert result.status is ToolCallStatus.SUCCEEDED
        assert result.output is not None and result.output["secret_echo"] == "[REDACTED]"
        assert executor.seen_secrets == [{}]
        assert executor.seen_contexts[0].tool_config == {"provider": "searxng"}
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_gateway_list_and_execute_fail_when_selected_secret_value_is_missing() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    spec = _spec()
    executor = _ProviderExecutor(spec)
    worker_id = f"gateway-missing-selected-secret-{uuid4()}"
    try:
        claim = await _seed_gateway_run(
            database,
            spec,
            worker_id=worker_id,
            tool_config={"provider": "brave"},
        )
        gateway = _gateway(database, executor, environment={})

        assert await gateway.list_authorized(claim, worker_id=worker_id) == ()
        result = await gateway.execute(
            claim,
            worker_id=worker_id,
            request=ToolGatewayRequest(
                tool_name=spec.name,
                tool_version=spec.version,
                arguments={"mode": "success"},
                idempotency_key="missing-selected-secret",
                caller="runtime:mock",
            ),
        )

        assert result.status is ToolCallStatus.FAILED
        assert result.error is not None
        assert result.error["code"] == "TOOL_SECRET_UNAVAILABLE"
        assert executor.calls == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_gateway_denial_and_invalid_input_are_terminal_without_execution() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    spec = _spec()
    executor = _Executor(spec)
    try:
        denied_worker = f"gateway-denied-{uuid4()}"
        denied_claim = await _seed_gateway_run(
            database, spec, worker_id=denied_worker, authorize=False
        )
        denied = await _gateway(database, executor).execute(
            denied_claim,
            worker_id=denied_worker,
            request=ToolGatewayRequest(
                tool_name=spec.name,
                tool_version=spec.version,
                arguments={"mode": "success"},
                idempotency_key="denied",
                caller="runtime:mock",
            ),
        )
        assert denied.status is ToolCallStatus.FAILED
        assert denied.error is not None and denied.error["code"] == "TOOL_ACCESS_DENIED"

        invalid_worker = f"gateway-invalid-{uuid4()}"
        invalid_claim = await _seed_gateway_run(database, spec, worker_id=invalid_worker)
        invalid = await _gateway(database, executor).execute(
            invalid_claim,
            worker_id=invalid_worker,
            request=ToolGatewayRequest(
                tool_name=spec.name,
                tool_version=spec.version,
                arguments={"mode": 123},
                idempotency_key="invalid",
                caller="runtime:mock",
            ),
        )
        assert invalid.status is ToolCallStatus.FAILED
        assert invalid.error is not None and invalid.error["code"] == "TOOL_INPUT_INVALID"
        assert executor.calls == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_gateway_retries_declared_errors_and_sanitizes_unexpected_failures() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    spec = _spec(
        retry_policy=ToolRetryPolicy(
            max_attempts=2,
            retryable_codes=frozenset({"TEMPORARY"}),
        )
    )
    try:
        retry_executor = _Executor(spec)
        retry_worker = f"gateway-retry-{uuid4()}"
        retry_claim = await _seed_gateway_run(database, spec, worker_id=retry_worker)
        retried = await _gateway(database, retry_executor).execute(
            retry_claim,
            worker_id=retry_worker,
            request=ToolGatewayRequest(
                tool_name=spec.name,
                tool_version=spec.version,
                arguments={"mode": "transient"},
                idempotency_key="retry",
                caller="runtime:mock",
            ),
        )
        assert retried.status is ToolCallStatus.SUCCEEDED
        assert retried.attempts == 2
        assert retry_executor.calls == 2

        exploding_executor = _Executor(spec)
        explode_worker = f"gateway-explode-{uuid4()}"
        explode_claim = await _seed_gateway_run(database, spec, worker_id=explode_worker)
        exploded = await _gateway(database, exploding_executor).execute(
            explode_claim,
            worker_id=explode_worker,
            request=ToolGatewayRequest(
                tool_name=spec.name,
                tool_version=spec.version,
                arguments={"mode": "explode"},
                idempotency_key="explode",
                caller="runtime:mock",
            ),
        )
        assert exploded.status is ToolCallStatus.FAILED
        assert exploded.error == {
            "code": "TOOL_EXECUTOR_FAILED",
            "message": "tool executor raised an unexpected error",
        }
        assert "very-secret-value" not in str(exploded)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_gateway_timeout_and_task_cancellation_reach_terminal_records() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    spec = _spec(timeout_seconds=1)
    try:
        timeout_executor = _Executor(spec, delay_seconds=1.2)
        timeout_worker = f"gateway-timeout-{uuid4()}"
        timeout_claim = await _seed_gateway_run(database, spec, worker_id=timeout_worker)
        timed_out = await _gateway(database, timeout_executor).execute(
            timeout_claim,
            worker_id=timeout_worker,
            request=ToolGatewayRequest(
                tool_name=spec.name,
                tool_version=spec.version,
                arguments={"mode": "success"},
                idempotency_key="timeout",
                caller="runtime:mock",
            ),
        )
        assert timed_out.status is ToolCallStatus.TIMED_OUT
        assert timed_out.error is not None and timed_out.error["code"] == "TOOL_TIMEOUT"

        cancel_executor = _Executor(spec, delay_seconds=2)
        cancel_worker = f"gateway-cancel-{uuid4()}"
        cancel_claim = await _seed_gateway_run(database, spec, worker_id=cancel_worker)
        request = ToolGatewayRequest(
            tool_name=spec.name,
            tool_version=spec.version,
            arguments={"mode": "success"},
            idempotency_key="cancel",
            caller="runtime:mock",
        )
        execution = asyncio.create_task(
            _gateway(database, cancel_executor).execute(
                cancel_claim, worker_id=cancel_worker, request=request
            )
        )
        await asyncio.sleep(0.05)
        execution.cancel()
        with pytest.raises(asyncio.CancelledError):
            await execution

        async with database.admin_transaction() as session:
            cancelled = await session.scalar(
                select(ToolCall).where(
                    ToolCall.run_id == cancel_claim.run_id,
                    ToolCall.idempotency_key == "cancel",
                )
            )
        assert cancelled is not None and cancelled.status == "cancelled"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_authoritative_run_cancel_rejects_late_tool_result() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    spec = _spec()
    worker_id = f"gateway-authoritative-cancel-{uuid4()}"
    try:
        claim = await _seed_gateway_run(database, spec, worker_id=worker_id)
        request = ToolGatewayRequest(
            tool_name=spec.name,
            tool_version=spec.version,
            arguments={"mode": "success"},
            idempotency_key="authoritative-cancel",
            caller="runtime:mock",
        )
        execution = asyncio.create_task(
            _gateway(database, _Executor(spec, delay_seconds=0.2)).execute(
                claim,
                worker_id=worker_id,
                request=request,
            )
        )
        for _ in range(50):
            async with database.admin_transaction() as session:
                row = (
                    await session.execute(
                        select(Run.status, Run.revision).where(Run.id == claim.run_id)
                    )
                ).one()
            if row.status == "waiting_for_tool":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("tool call did not enter waiting_for_tool")

        await ControlPlaneService(database).cancel_run(
            TenantContext(claim.tenant_id, "cancel-test", uuid4()),
            claim.run_id,
            expected_revision=row.revision,
        )
        with pytest.raises(ToolLeaseLost):
            await execution

        async with database.admin_transaction() as session:
            call = await session.scalar(
                select(ToolCall).where(
                    ToolCall.run_id == claim.run_id,
                    ToolCall.idempotency_key == "authoritative-cancel",
                )
            )
            run = await session.get(Run, claim.run_id)
        assert run is not None and run.status == "cancelled"
        assert call is not None and call.status == "cancelled"
        assert call.result is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_tool_call_is_taken_over_and_late_result_is_rejected() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    spec = _spec()
    old_worker = f"gateway-old-{uuid4()}"
    request = ToolGatewayRequest(
        tool_name=spec.name,
        tool_version=spec.version,
        arguments={"mode": "success", "value": "fresh"},
        idempotency_key="recover",
        caller="runtime:mock",
    )
    try:
        old_claim = await _seed_gateway_run(
            database,
            spec,
            worker_id=old_worker,
            priority=10_000,
        )
        old_execution = asyncio.create_task(
            _gateway(database, _Executor(spec, delay_seconds=0.2)).execute(
                old_claim,
                worker_id=old_worker,
                request=request,
            )
        )
        for _ in range(50):
            async with database.admin_transaction() as session:
                status = await session.scalar(select(Run.status).where(Run.id == old_claim.run_id))
            if status == "waiting_for_tool":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("tool call did not enter waiting_for_tool")

        async with database.admin_transaction() as session:
            await session.execute(
                text("UPDATE runs SET lease_expires_at = :expired WHERE id = :run_id"),
                {
                    "expired": datetime.now(UTC) - timedelta(seconds=1),
                    "run_id": old_claim.run_id,
                },
            )
        with pytest.raises(ToolLeaseLost):
            await old_execution

        new_worker = f"gateway-new-{uuid4()}"
        new_claim = await database.claim_next_run(new_worker, 5)
        assert new_claim is not None and new_claim.run_id == old_claim.run_id
        recovered_executor = _Executor(spec)
        recovered = await _gateway(database, recovered_executor).execute(
            new_claim,
            worker_id=new_worker,
            request=request,
        )
        assert recovered.status is ToolCallStatus.SUCCEEDED
        assert recovered.output is not None and recovered.output["value"] == "fresh"
        assert recovered_executor.calls == 1

        async with database.admin_transaction() as session:
            call = await session.scalar(
                select(ToolCall).where(
                    ToolCall.run_id == old_claim.run_id,
                    ToolCall.idempotency_key == "recover",
                )
            )
        assert call is not None and call.execution_lease_token == new_claim.lease_token
        assert call.status == "succeeded"
    finally:
        await engine.dispose()
