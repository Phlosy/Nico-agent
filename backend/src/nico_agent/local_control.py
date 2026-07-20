"""Local-only, stdin-bound control surface for deployment maintenance."""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import create_async_engine

from nico_agent.config import get_settings
from nico_agent.database import Database


def _request() -> dict[str, Any]:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise ValueError("local control input must be a JSON object")
    return payload


def _uuid(payload: dict[str, Any]) -> UUID:
    value = payload.get("attempt_id")
    if not isinstance(value, str):
        raise ValueError("attempt_id is required")
    return UUID(value)


def _token(payload: dict[str, Any]) -> str:
    value = payload.get("token")
    if not isinstance(value, str) or not 32 <= len(value) <= 500:
        raise ValueError("maintenance token is invalid")
    return value


def _lease_seconds(payload: dict[str, Any]) -> int:
    value = payload.get("lease_seconds", 120)
    if not isinstance(value, int) or not 30 <= value <= 900:
        raise ValueError("lease_seconds must be between 30 and 900")
    return value


async def execute(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    engine = create_async_engine(settings.resolved_database_url)
    database = Database(engine)
    try:
        attempt_id = _uuid(payload)
        if action == "acquire":
            token = secrets.token_urlsafe(32)
            lease = await database.acquire_runtime_maintenance(
                attempt_id,
                token,
                _lease_seconds(payload),
            )
            response: dict[str, Any] = {
                "ok": lease.acquired,
                "active_run_count": lease.active_run_count,
            }
            if lease.acquired:
                response.update(
                    {
                        "attempt_id": str(attempt_id),
                        "token": token,
                        "lease_expires_at": lease.lease_expires_at.isoformat()
                        if lease.lease_expires_at
                        else None,
                    }
                )
            else:
                response.update(
                    {
                        "code": "RUNTIME_MAINTENANCE_ACTIVE_RUNS",
                        "message": "Native Runs must be terminal before Provider setup.",
                    }
                )
            return response
        if action == "renew":
            renewed = await database.renew_runtime_maintenance(
                attempt_id,
                _token(payload),
                _lease_seconds(payload),
            )
            return {
                "ok": renewed,
                "code": None if renewed else "RUNTIME_MAINTENANCE_LEASE_LOST",
            }
        if action == "release":
            released = await database.release_runtime_maintenance(attempt_id, _token(payload))
            return {"ok": released, "released": released}
        raise ValueError("local control action must be acquire, renew, or release")
    finally:
        await engine.dispose()


def run() -> None:
    action = sys.argv[1] if len(sys.argv) == 2 else ""
    try:
        response = asyncio.run(execute(action, _request()))
    except (ValueError, json.JSONDecodeError) as exc:
        response = {"ok": False, "code": "LOCAL_CONTROL_INVALID", "message": str(exc)}
    except Exception:
        response = {
            "ok": False,
            "code": "LOCAL_CONTROL_UNAVAILABLE",
            "message": "local control operation failed",
        }
    print(json.dumps(response, separators=(",", ":"), sort_keys=True))
    if not response.get("ok"):
        raise SystemExit(4)


if __name__ == "__main__":
    run()
