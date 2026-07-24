from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine
from test_user_input_runtime import _request_and_suspend

from nico_agent.api import create_app
from nico_agent.config import Settings
from nico_agent.database import Database

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with PostgreSQL running",
)


async def test_user_input_api_is_tenant_safe_versioned_idempotent_and_redacted() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        claim, _batch, request, _checkpoint = await _request_and_suspend(
            database,
            worker_id=f"user-input-api-{uuid4().hex[:8]}",
            schema={"type": "string", "enum": ["file", "database", "run"]},
        )
        app = create_app(settings=settings, health_service=object(), database=database)
        headers = {
            "X-Tenant-ID": str(claim.tenant_id),
            "X-Actor-ID": "user-input-api-test",
        }
        answer = {
            "expected_revision": request.revision,
            "answer": "database",
        }
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://user-input.test",
        ) as client:
            listed = await client.get(
                "/api/v1/user-input-requests",
                params={"run_id": str(claim.run_id), "status": "requested"},
                headers=headers,
            )
            assert listed.status_code == 200
            assert [item["id"] for item in listed.json()] == [str(request.id)]
            assert "answer" not in listed.json()[0]
            assert "answer_payload" not in listed.json()[0]

            isolated = await client.get(
                f"/api/v1/user-input-requests/{request.id}",
                headers={**headers, "X-Tenant-ID": str(uuid4())},
            )
            assert isolated.status_code == 404

            isolated_answer = await client.post(
                f"/api/v1/user-input-requests/{request.id}/answer",
                headers={
                    **headers,
                    "X-Tenant-ID": str(uuid4()),
                    "Idempotency-Key": "cross-tenant",
                },
                json=answer,
            )
            assert isolated_answer.status_code == 404

            malformed = await client.post(
                f"/api/v1/user-input-requests/{request.id}/answer",
                headers={**headers, "Idempotency-Key": "malformed"},
                json={**answer, "answer": "unknown"},
            )
            assert malformed.status_code == 400
            assert malformed.json()["code"] == "USER_INPUT_ANSWER_INVALID"
            assert "unknown" not in malformed.text

            stale = await client.post(
                f"/api/v1/user-input-requests/{request.id}/answer",
                headers={**headers, "Idempotency-Key": "stale"},
                json={**answer, "expected_revision": request.revision + 1},
            )
            assert stale.status_code == 409
            assert stale.json()["code"] == "REVISION_CONFLICT"

            answered = await client.post(
                f"/api/v1/user-input-requests/{request.id}/answer",
                headers={**headers, "Idempotency-Key": "answer-once"},
                json=answer,
            )
            assert answered.status_code == 200
            assert answered.json()["status"] == "answered"
            assert "answer" not in answered.json()
            assert "answer_payload" not in answered.json()

            replay = await client.post(
                f"/api/v1/user-input-requests/{request.id}/answer",
                headers={**headers, "Idempotency-Key": "answer-once"},
                json=answer,
            )
            assert replay.status_code == 200
            assert replay.json() == answered.json()

            conflict = await client.post(
                f"/api/v1/user-input-requests/{request.id}/answer",
                headers={**headers, "Idempotency-Key": "different-answer"},
                json={**answer, "answer": "file"},
            )
            assert conflict.status_code == 409
            assert conflict.json()["code"] == "USER_INPUT_ALREADY_RESOLVED"
            assert "file" not in conflict.text
    finally:
        await engine.dispose()


async def test_expired_user_input_answer_returns_stable_conflict_after_persisting_expiry() -> None:
    settings = Settings(environment="test", _env_file=None)
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        claim, _batch, request, _checkpoint = await _request_and_suspend(
            database,
            worker_id=f"user-input-expired-api-{uuid4().hex[:8]}",
            schema={"type": "string"},
            ttl_seconds=1,
        )
        await asyncio.sleep(1.05)

        app = create_app(settings=settings, health_service=object(), database=database)
        headers = {
            "X-Tenant-ID": str(claim.tenant_id),
            "X-Actor-ID": "user-input-expiry-test",
            "Idempotency-Key": "late-answer",
        }
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://user-input.test",
        ) as client:
            response = await client.post(
                f"/api/v1/user-input-requests/{request.id}/answer",
                headers=headers,
                json={"expected_revision": request.revision, "answer": "too late"},
            )
            assert response.status_code == 409
            assert response.json()["code"] == "USER_INPUT_EXPIRED"
            assert "too late" not in response.text

            persisted_view = await client.get(
                f"/api/v1/user-input-requests/{request.id}",
                headers=headers,
            )
            assert persisted_view.status_code == 200
            assert persisted_view.json()["status"] == "expired"
    finally:
        await engine.dispose()
