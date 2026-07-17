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
