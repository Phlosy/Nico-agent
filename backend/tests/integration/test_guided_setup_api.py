from __future__ import annotations

import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from nico_agent.api import create_app
from nico_agent.config import Settings
from nico_agent.local_defaults import local_tenant_settings

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


async def _bootstrap(client: AsyncClient, label: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/tenants/bootstrap",
        json={
            "name": label,
            "slug": f"guided-{label.lower()}-{uuid4().hex[:8]}",
            "settings": {
                **local_tenant_settings(),
                "operator_secret": f"private-{label}",
            },
        },
        headers={"X-Actor-ID": "guided-bootstrap"},
    )
    assert response.status_code == 201, response.text
    return {
        "X-Tenant-ID": response.json()["id"],
        "X-Actor-ID": "guided-operator",
    }


@pytest.mark.asyncio
async def test_setup_readiness_intent_is_redacted_isolated_and_idempotent() -> None:
    app = create_app(
        settings=Settings(
            environment="test",
            model_endpoint_writes_enabled=True,
            web_provider_writes_enabled=True,
            _env_file=None,
        )
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            first_headers = await _bootstrap(client, "First")
            second_headers = await _bootstrap(client, "Second")

            fresh = await client.get("/api/v1/setup/readiness", headers=first_headers)
            assert fresh.status_code == 200, fresh.text
            fresh_payload = fresh.json()
            assert fresh_payload["overall"] == "incomplete"
            assert [area["key"] for area in fresh_payload["areas"]] == [
                "model",
                "web",
                "capabilities",
                "verification",
            ]
            assert "private-First" not in fresh.text

            command = {
                "expected_tenant_revision": fresh_payload["tenant_revision"],
                "web_intent": "skipped",
            }
            updated = await client.patch(
                "/api/v1/setup/intent",
                headers=first_headers,
                json=command,
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["web_intent"] == "skipped"

            repeated = await client.patch(
                "/api/v1/setup/intent",
                headers=first_headers,
                json=command,
            )
            assert repeated.status_code == 200, repeated.text
            assert repeated.json()["tenant_revision"] == updated.json()["tenant_revision"]

            first = await client.get("/api/v1/setup/readiness", headers=first_headers)
            second = await client.get("/api/v1/setup/readiness", headers=second_headers)
            assert (
                next(area for area in first.json()["areas"] if area["key"] == "web")["state"]
                == "skipped"
            )
            assert (
                next(area for area in second.json()["areas"] if area["key"] == "web")["state"]
                == "incomplete"
            )

            conflict = await client.patch(
                "/api/v1/setup/intent",
                headers=first_headers,
                json={**command, "web_intent": "enabled"},
            )
            assert conflict.status_code == 409
            assert conflict.json()["code"] == "REVISION_CONFLICT"
            assert "private-First" not in conflict.text
