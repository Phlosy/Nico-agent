"""SQLAlchemy base and transaction boundaries with mandatory tenant context."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
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


@dataclass(frozen=True, slots=True)
class ProviderProbeClaim:
    probe_id: UUID
    tenant_id: UUID
    lease_token: str
    previous_status: str


@dataclass(frozen=True, slots=True)
class ProjectSupervisionClaim:
    cycle_id: UUID
    tenant_id: UUID
    lease_token: UUID
    previous_status: str


@dataclass(frozen=True, slots=True)
class RuntimeMaintenanceLease:
    acquired: bool
    active_run_count: int
    lease_expires_at: datetime | None


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

    async def reconcile_user_input_requests(self) -> int:
        """Resolve expired/answered user-input waits and repair missed wakes."""

        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE nico_worker_claimer"))
            value = await session.scalar(text("SELECT reconcile_user_input_requests()"))
            return int(value or 0)

    async def claim_next_provider_probe(
        self, worker_id: str, lease_seconds: int
    ) -> ProviderProbeClaim | None:
        """Claim one pending or expired Provider probe across tenants."""

        if not worker_id or len(worker_id) > 200:
            raise ValueError("worker_id must contain between 1 and 200 characters")
        if lease_seconds < 30 or lease_seconds > 3600:
            raise ValueError("probe lease_seconds must be between 30 and 3600")
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE nico_worker_claimer"))
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT probe_id, tenant_id, lease_token, previous_status "
                            "FROM claim_next_provider_probe(:worker_id, :lease_seconds)"
                        ),
                        {"worker_id": worker_id, "lease_seconds": lease_seconds},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            return ProviderProbeClaim(
                probe_id=row["probe_id"],
                tenant_id=row["tenant_id"],
                lease_token=row["lease_token"],
                previous_status=row["previous_status"],
            )

    async def claim_next_project_supervision(
        self, worker_id: str, lease_seconds: int
    ) -> ProjectSupervisionClaim | None:
        """Enqueue due slots and claim one supervision cycle across tenants."""

        if not worker_id or len(worker_id) > 200:
            raise ValueError("worker_id must contain between 1 and 200 characters")
        if lease_seconds < 30 or lease_seconds > 3600:
            raise ValueError("supervision lease_seconds must be between 30 and 3600")
        async with self.sessions() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE nico_worker_claimer"))
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT cycle_id, tenant_id, lease_token, previous_status "
                            "FROM claim_next_project_supervision(:worker_id, :lease_seconds)"
                        ),
                        {"worker_id": worker_id, "lease_seconds": lease_seconds},
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            return ProjectSupervisionClaim(
                cycle_id=row["cycle_id"],
                tenant_id=row["tenant_id"],
                lease_token=row["lease_token"],
                previous_status=row["previous_status"],
            )

    async def acquire_runtime_maintenance(
        self,
        attempt_id: UUID,
        token: str,
        lease_seconds: int,
    ) -> RuntimeMaintenanceLease:
        async with self.admin_transaction() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT active_run_count, lease_expires_at "
                            "FROM acquire_runtime_maintenance(:attempt_id, :token, :lease_seconds)"
                        ),
                        {
                            "attempt_id": attempt_id,
                            "token": token,
                            "lease_seconds": lease_seconds,
                        },
                    )
                )
                .mappings()
                .one()
            )
            count = int(row["active_run_count"])
            return RuntimeMaintenanceLease(
                acquired=count == 0,
                active_run_count=count,
                lease_expires_at=row["lease_expires_at"],
            )

    async def renew_runtime_maintenance(
        self,
        attempt_id: UUID,
        token: str,
        lease_seconds: int,
    ) -> bool:
        async with self.admin_transaction() as session:
            value = await session.scalar(
                text("SELECT renew_runtime_maintenance(:attempt_id, :token, :lease_seconds)"),
                {
                    "attempt_id": attempt_id,
                    "token": token,
                    "lease_seconds": lease_seconds,
                },
            )
            return bool(value)

    async def release_runtime_maintenance(self, attempt_id: UUID, token: str) -> bool:
        async with self.admin_transaction() as session:
            value = await session.scalar(
                text("SELECT release_runtime_maintenance(:attempt_id, :token)"),
                {"attempt_id": attempt_id, "token": token},
            )
            return bool(value)
