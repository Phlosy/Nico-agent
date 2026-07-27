from __future__ import annotations

import asyncio
import os
import socket
import sys
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.api import create_app
from nico_agent.config import Settings
from nico_agent.database import Database
from nico_agent.domain.models import Agent, AgentVersion, Project, Run, Task, Tenant
from nico_agent.net.safe_http import SafeHttpClient
from nico_agent.runtime import MockRuntimeProvider
from nico_agent.runtime.executor import RuntimeWorker
from nico_agent.runtime.registry import RuntimeProviderRegistry
from nico_agent.testing.fake_tool_provider import (
    FAKE_ECHO_INPUT_SCHEMA,
    FAKE_ECHO_OUTPUT_SCHEMA,
    FAKE_ECHO_TOOL,
)
from nico_agent.tool_providers.client import ToolProviderClient
from nico_agent.tool_providers.executor import ExternalToolResolver
from nico_agent.tools import (
    ToolDefinitionSpec,
    ToolGateway,
    ToolIsolation,
    ToolRegistry,
    ToolRetryPolicy,
    ToolRisk,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_E2E") != "1",
    reason="set RUN_E2E=1 with PostgreSQL running",
)

SECRET = "live-http-provider-secret-at-least-32-bytes"
PROVISION_KEY = "live-http-provision-key-at-least-32-bytes"
CREDENTIAL_ENV = "NICO_TOOL_SECRET_LIVE_HTTP_PROVIDER"
CREDENTIAL_REF = f"env:{CREDENTIAL_ENV}"
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


async def _wait_until_listening(port: int, process: asyncio.subprocess.Process) -> None:
    async with httpx.AsyncClient(trust_env=False) as client:
        for _ in range(50):
            if process.returncode is not None:
                output = await process.stdout.read() if process.stdout is not None else b""
                raise RuntimeError(
                    f"live Provider exited during startup: {output.decode(errors='replace')}"
                )
            try:
                await client.get(f"http://127.0.0.1:{port}/", timeout=0.1)
                return
            except httpx.HTTPError:
                await asyncio.sleep(0.05)
    raise TimeoutError("live Provider did not start within the bounded readiness window")


def _spec() -> ToolDefinitionSpec:
    return ToolDefinitionSpec(
        name=FAKE_ECHO_TOOL.name,
        version=FAKE_ECHO_TOOL.version,
        description="Live-process external Provider echo",
        input_schema=FAKE_ECHO_INPUT_SCHEMA,
        output_schema=FAKE_ECHO_OUTPUT_SCHEMA,
        permission="test.external.echo",
        timeout_seconds=5,
        retry_policy=ToolRetryPolicy(max_attempts=1),
        isolation=ToolIsolation.IN_PROCESS,
        risk=ToolRisk.LOW,
        max_output_bytes=1_048_576,
    )


@pytest.mark.asyncio
async def test_public_http_provider_run_worker_and_trajectory_e2e(monkeypatch) -> None:
    monkeypatch.setenv(CREDENTIAL_ENV, SECRET)
    settings = Settings(
        environment="test",
        tool_provider_allow_http_loopback=True,
        tool_provider_connect_timeout_seconds=1,
        tool_provider_read_timeout_seconds=5,
        _env_file=None,
    )
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    process: asyncio.subprocess.Process | None = None
    try:
        spec = _spec()
        suffix = uuid4().hex[:10]
        tenant_policy = {
            "allow": [spec.reference],
            "permissions": [spec.permission],
            "secret_refs": {},
            "tools": {spec.reference: {}},
        }
        async with database.admin_transaction() as session:
            await session.execute(
                update(Task).where(Task.title == "External Provider live E2E").values(priority=-1)
            )
            tenant = Tenant(
                name=f"Live Provider {suffix}",
                slug=f"live-provider-{suffix}",
                settings={"tool_policy": tenant_policy},
            )
            session.add(tenant)
            await session.flush()
            project = Project(tenant_id=tenant.id, name=f"live-project-{suffix}")
            agent = Agent(
                tenant_id=tenant.id,
                name=f"live-agent-{suffix}",
                display_name="Live Provider Agent",
            )
            session.add_all([project, agent])
            await session.flush()
            version = AgentVersion(
                tenant_id=tenant.id,
                agent_id=agent.id,
                version=1,
                status="published",
                role="provider-live-e2e",
                mandate="Call the live external Tool Provider",
                tool_policy={
                    "allow": [spec.reference],
                    "permissions": [spec.permission],
                    "secrets": [],
                    "tools": {spec.reference: {}},
                },
                run_config={
                    "runtime_provider": "mock",
                    "mock": {
                        "steps": [],
                        "tool_calls": [
                            {
                                "call_id": "live-http-call",
                                "name": spec.name,
                                "version": spec.version,
                                "arguments": {"transport": "real-http"},
                                "idempotency_key": "live-http-call-1",
                            }
                        ],
                    },
                },
                content_hash="e" * 64,
            )
            session.add(version)
            await session.flush()
            agent.current_version_id = version.id
            agent.status = "ready"
            task = Task(
                tenant_id=tenant.id,
                project_id=project.id,
                assignee_agent_id=agent.id,
                title="External Provider live E2E",
                input={"request": "execute over a real local HTTP socket"},
                status="assigned",
                priority=2_147_483_647,
            )
            session.add(task)
            await session.flush()
            tenant_id = tenant.id
            project_id = project.id
            agent_id = agent.id
            version_id = version.id
            task_id = task.id

        app = create_app(settings=settings, health_service=object(), database=database)
        headers = {
            "X-Tenant-ID": str(tenant_id),
            "X-Actor-ID": "e2e:external-provider",
        }
        port = _free_port()
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://nico.test",
            ) as api,
        ):
            definition = await api.post(
                "/api/v1/tool-definitions",
                headers=headers,
                json=spec.model_dump(mode="json"),
            )
            assert definition.status_code == 201, definition.text
            registered = await api.post(
                "/api/v1/external-tool-providers",
                headers=headers,
                json={
                    "name": f"live.stub.{suffix}",
                    "project_id": str(project_id),
                    "endpoint_ref": f"http://localhost:{port}",
                    "credential_ref": CREDENTIAL_REF,
                },
            )
            assert registered.status_code == 201, registered.text
            provider_id = registered.json()["id"]

            environment = {
                **os.environ,
                "NICO_TEST_TOOL_PROVIDER_ID": provider_id,
                "NICO_TEST_TOOL_PROVIDER_SECRET": SECRET,
                "NICO_TEST_TOOL_PROVIDER_PROVISION_KEY": PROVISION_KEY,
            }
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "uvicorn",
                "nico_agent.testing.live_tool_provider:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
                cwd=BACKEND_DIR,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            await _wait_until_listening(port, process)

            verified = await api.post(
                f"/api/v1/external-tool-providers/{provider_id}/verify",
                headers=headers,
            )
            assert verified.status_code == 200, verified.text
            assert verified.json()["status"] == "active"
            created = await api.post(
                f"/api/v1/tasks/{task_id}/runs",
                headers=headers,
                json={
                    "timeout_seconds": 30,
                    "tool_bindings": [
                        {
                            "provider_id": provider_id,
                            "tool": {"name": spec.name, "version": spec.version},
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
                },
            )
            assert created.status_code == 201, created.text
            created_run = created.json()
            snapshot = created_run["tool_binding_snapshot"]["bindings"][0]
            assert snapshot["credential_ref"] == "[REDACTED]"

            async with httpx.AsyncClient(trust_env=False) as local_provider:
                provisioned = await local_provider.post(
                    f"http://127.0.0.1:{port}/__test__/scopes",
                    headers={"X-Test-Provision-Key": PROVISION_KEY},
                    json={
                        "binding_digest": snapshot["binding_digest"],
                        "tenant_id": str(tenant_id),
                        "project_id": str(project_id),
                        "run_id": created_run["id"],
                        "task_id": str(task_id),
                        "agent_id": str(agent_id),
                        "agent_version_id": str(version_id),
                        "tool": FAKE_ECHO_TOOL.model_dump(mode="json"),
                    },
                )
                assert provisioned.status_code == 200, provisioned.text

            tool_client = ToolProviderClient(
                http=SafeHttpClient(connect_timeout=1, read_timeout=5),
                max_response_bytes=1_048_576,
            )
            worker = RuntimeWorker(
                database,
                RuntimeProviderRegistry([MockRuntimeProvider()]),
                worker_id=f"live-provider-worker-{suffix}",
                lease_seconds=5,
                heartbeat_seconds=0.05,
                tool_gateway=ToolGateway(
                    database,
                    ToolRegistry([]),
                    approval_required_risks=frozenset(),
                    external_resolver=ExternalToolResolver(database, tool_client),
                ),
            )
            assert await worker.execute_once() is True

            completed = await api.get(f"/api/v1/runs/{created_run['id']}", headers=headers)
            trajectory = await api.get(
                f"/api/v1/runs/{created_run['id']}/trajectory",
                headers=headers,
            )
            events = await api.get(
                f"/api/v1/runs/{created_run['id']}/events",
                headers=headers,
            )
            async with httpx.AsyncClient(trust_env=False) as local_provider:
                stats = await local_provider.get(
                    f"http://127.0.0.1:{port}/__test__/stats",
                    headers={"X-Test-Provision-Key": PROVISION_KEY},
                )

        assert completed.status_code == 200
        output = completed.json()["result"]["tool_results"][0]
        assert output["status"] == "succeeded"
        assert output["output"] == {"echo": {"transport": "real-http"}}
        assert trajectory.status_code == 200
        assert any(
            message["role"] == "tool" and message["content"]["output"] == output["output"]
            for message in trajectory.json()["messages"]
        )
        event_types = {event["event_type"] for event in events.json()}
        assert {
            "tool.binding.resolved",
            "tool.provider.requested",
            "tool.provider.started",
            "tool.provider.completed",
        } <= event_types
        assert stats.status_code == 200
        assert stats.json()["executions"] == 1
        assert process.pid != os.getpid()
        assert SECRET not in completed.text
        assert SECRET not in trajectory.text
        async with database.admin_transaction() as session:
            persisted = await session.scalar(select(Run).where(Run.id == created_run["id"]))
        assert persisted is not None and persisted.status == "completed"
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                process.kill()
                await process.wait()
        await engine.dispose()
