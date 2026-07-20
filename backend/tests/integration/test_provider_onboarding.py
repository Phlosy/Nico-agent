from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from nico_agent.api import create_app
from nico_agent.config import Settings

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app(settings=Settings(environment="test", _env_file=None))
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as value:
            yield value


async def _headers(client: AsyncClient, label: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/tenants/bootstrap",
        json={"name": label, "slug": f"{label.lower()}-{uuid4().hex[:10]}"},
        headers={"X-Actor-ID": "provider-bootstrap"},
    )
    assert response.status_code == 201, response.text
    return {
        "X-Tenant-ID": response.json()["id"],
        "X-Actor-ID": "provider-operator",
    }


@pytest.mark.asyncio
async def test_catalog_probe_idempotency_cancellation_and_tenant_isolation(
    client: AsyncClient,
) -> None:
    catalog_response = await client.get("/api/v1/provider-catalog")
    assert catalog_response.status_code == 200
    catalog = catalog_response.json()
    openai = next(item for item in catalog["providers"] if item["key"] == "openai")
    first_headers = await _headers(client, "ProviderAPI")
    second_headers = await _headers(client, "ProviderOther")
    command = {
        "kind": "verify_completion",
        "candidate": {
            "provider_key": "openai",
            "protocol": openai["protocol"],
            "base_url": next(item["base_url"] for item in openai["locations"] if item["default"]),
            "credential_ref": "env:NICO_MODEL_SECRET_CANARY",
            "model": openai["recommended_models"][0],
            "catalog_revision": catalog["catalog_revision"],
        },
        "idempotency_key": f"provider-api-{uuid4().hex}",
    }

    created = await client.post("/api/v1/provider-probes", json=command, headers=first_headers)
    assert created.status_code == 201, created.text
    probe = created.json()
    repeated = await client.post("/api/v1/provider-probes", json=command, headers=first_headers)
    assert repeated.status_code == 201
    assert repeated.json()["id"] == probe["id"]

    hidden = await client.get(f"/api/v1/provider-probes/{probe['id']}", headers=second_headers)
    assert hidden.status_code == 404
    cancelled = await client.post(
        f"/api/v1/provider-probes/{probe['id']}/cancel",
        headers=first_headers,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert "CANARY" in cancelled.json()["credential_ref"]
    assert "sk-canary-value" not in cancelled.text

    terminal = await client.post(
        f"/api/v1/provider-probes/{probe['id']}/cancel",
        headers=first_headers,
    )
    assert terminal.status_code == 200
