from __future__ import annotations

import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from nico_agent.api import create_app
from nico_agent.config import Settings

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


async def _bootstrap(client: AsyncClient, label: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/tenants/bootstrap",
        json={"name": label, "slug": f"{label.lower()}-{uuid4().hex[:10]}"},
        headers={"X-Actor-ID": "web-bootstrap"},
    )
    assert response.status_code == 201, response.text
    return {
        "X-Tenant-ID": response.json()["id"],
        "X-Actor-ID": "web-operator",
    }


@pytest.mark.asyncio
async def test_web_api_exposes_policy_preflight_before_accepting_probe() -> None:
    app = create_app(
        settings=Settings(
            environment="test",
            web_provider_writes_enabled=False,
            _env_file=None,
        )
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            headers = await _bootstrap(client, "WebPolicy")
            catalog_response = await client.get("/api/v1/web/catalog")
            assert catalog_response.status_code == 200
            catalog = catalog_response.json()
            readiness = await client.get("/api/v1/web/setup-readiness", headers=headers)
            assert readiness.status_code == 200
            assert readiness.json()["writes_enabled"] is False
            assert readiness.json()["reason"] == "deployment_policy_disabled"

            denied = await client.post(
                "/api/v1/web/provider-probes",
                headers=headers,
                json={
                    "candidate": {
                        "provider": "brave",
                        "endpoint_key": "managed",
                        "credential_ref": "env:NICO_TOOL_SECRET_WEB_SEARCH_BRAVE_TEST",
                        "catalog_revision": catalog["catalog_revision"],
                    },
                    "idempotency_key": f"web-denied-{uuid4().hex}",
                },
            )

            assert denied.status_code == 403
            assert denied.json()["code"] == "WEB_PROVIDER_WRITES_DISABLED"
            assert "BRAVE_TEST" not in denied.text


@pytest.mark.asyncio
async def test_web_api_creates_secret_free_durable_probe() -> None:
    app = create_app(
        settings=Settings(
            environment="test",
            web_provider_writes_enabled=True,
            _env_file=None,
        )
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            headers = await _bootstrap(client, "WebProbe")
            catalog = (await client.get("/api/v1/web/catalog")).json()
            created = await client.post(
                "/api/v1/web/provider-probes",
                headers=headers,
                json={
                    "candidate": {
                        "provider": "searxng",
                        "endpoint_key": "configured",
                        "catalog_revision": catalog["catalog_revision"],
                    },
                    "idempotency_key": f"web-probe-{uuid4().hex}",
                },
            )

            assert created.status_code == 201, created.text
            assert created.json()["provider"] == "searxng"
            assert created.json()["status"] == "pending"
            assert "credential_ref" not in created.text
            read = await client.get(
                f"/api/v1/web/provider-probes/{created.json()['id']}",
                headers=headers,
            )
            assert read.status_code == 200
            assert read.json()["candidate_hash"] == created.json()["candidate_hash"]
