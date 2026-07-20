"""SQLAlchemy base and transaction boundaries with mandatory tenant context."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


class Base(DeclarativeBase):
    pass


@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: UUID
    actor_id: str
    correlation_id: UUID


@dataclass(frozen=True, slots=True)
class RunClaim:
    run_id: UUID
    tenant_id: UUID
    lease_token: UUID
    previous_status: str


class Database:
    def __init__(self, engine: AsyncEngine, *, runtime_role: str = "nico_runtime") -> None:
        if not _ROLE_NAME.fullmatch(runtime_role):
            raise ValueError("database runtime role contains unsupported characters")
        self.engine = engine
        self.runtime_role = runtime_role
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def tenant_transaction(self, context: TenantContext) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as session, session.begin():
            await session.execute(text(f"SET LOCAL ROLE {self.runtime_role}"))
            await session.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(context.tenant_id)},
            )
            yield session

    @asynccontextmanager
    async def admin_transaction(self) -> AsyncIterator[AsyncSession]:
        """Owner transaction reserved for development tenant bootstrap and migrations."""

        async with self.sessions() as session, session.begin():
            yield session

    async def claim_next_run(self, worker_id: str, lease_seconds: int) -> RunClaim | None:
        """Call the minimal SECURITY DEFINER queue function as the claimer role."""

        if not worker_id or len(worker_id) > 200:
            raise ValueError("worker_id must contain between 1 and 200 characters")
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE nico_worker_claimer"))
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT run_id, tenant_id, lease_token, previous_status "
                            "FROM claim_next_run(:worker_id, :lease_seconds)"
                        ),
                        {"worker_id": worker_id, "lease_seconds": lease_seconds},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            return RunClaim(
                run_id=row["run_id"],
                tenant_id=row["tenant_id"],
                lease_token=row["lease_token"],
                previous_status=row["previous_status"],
            )

    async def reconcile_coordination_waiters(self) -> int:
        """Wake DB-authoritative parents whose complete child set is already terminal."""

        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE nico_worker_claimer"))
            value = await session.scalar(text("SELECT reconcile_coordination_waiters()"))
            return int(value or 0)

    async def reconcile_expired_tool_approvals(self) -> int:
        """Expire durable approval requests and wake their suspended Runs."""

        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE nico_worker_claimer"))
            value = await session.scalar(text("SELECT reconcile_expired_tool_approvals()"))
            return int(value or 0)
