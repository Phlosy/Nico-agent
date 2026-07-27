"""Independent local-process fixture for the external Provider E2E test."""

from __future__ import annotations

import os
import secrets
from uuid import UUID

from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict

from nico_agent.testing.fake_tool_provider import (
    FAKE_ECHO_TOOL,
    FakeToolProvider,
    FakeToolProviderScope,
)
from nico_agent.tool_providers.contracts import ProviderToolContract


class ScopeProvision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_digest: str
    tenant_id: UUID
    project_id: UUID | None = None
    run_id: UUID
    task_id: UUID | None = None
    agent_id: UUID
    agent_version_id: UUID
    tool: ProviderToolContract = FAKE_ECHO_TOOL


def _required_environment(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required for the live Tool Provider fixture")
    return value


provider = FakeToolProvider(
    provider_id=_required_environment("NICO_TEST_TOOL_PROVIDER_ID"),
    secret=_required_environment("NICO_TEST_TOOL_PROVIDER_SECRET"),
)
_provision_key = _required_environment("NICO_TEST_TOOL_PROVIDER_PROVISION_KEY")
app = provider.app


@app.post("/__test__/scopes", include_in_schema=False)
async def provision_scope(
    command: ScopeProvision,
    x_test_provision_key: str = Header(alias="X-Test-Provision-Key"),
) -> dict[str, str]:
    if not secrets.compare_digest(x_test_provision_key, _provision_key):
        raise HTTPException(status_code=403, detail="test provisioning denied")
    provider.provision_scope(
        FakeToolProviderScope(
            binding_digest=command.binding_digest,
            tenant_id=command.tenant_id,
            project_id=command.project_id,
            run_id=command.run_id,
            task_id=command.task_id,
            agent_id=command.agent_id,
            agent_version_id=command.agent_version_id,
            tool=command.tool,
        )
    )
    return {"status": "provisioned"}


@app.get("/__test__/stats", include_in_schema=False)
async def provider_stats(
    x_test_provision_key: str = Header(alias="X-Test-Provision-Key"),
) -> dict[str, int]:
    if not secrets.compare_digest(x_test_provision_key, _provision_key):
        raise HTTPException(status_code=403, detail="test provisioning denied")
    return dict(provider.call_counts)
