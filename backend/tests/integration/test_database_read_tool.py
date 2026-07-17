from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest

from nico_agent.config import Settings
from nico_agent.tools import ToolExecutionContext
from nico_agent.tools.builtin import DatabaseReadExecutor

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 with the Compose dependencies running",
)


@pytest.mark.asyncio
async def test_real_postgres_readonly_role_query_is_bounded_and_closed() -> None:
    settings = Settings()
    suffix = uuid4().hex[:12]
    role = f"nico_tool_read_{suffix}"
    table = f"nico_tool_data_{suffix}"
    password = f"read_{suffix}"
    admin = await asyncpg.connect(
        host=settings.database_host,
        port=settings.database_port,
        database=settings.database_name,
        user=settings.database_user,
        password=settings.database_password,
    )
    try:
        await admin.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'")
        await admin.execute(f'CREATE TABLE public."{table}" (id integer, value text)')
        await admin.execute(
            f"INSERT INTO public.\"{table}\" VALUES (1, 'one'), (2, 'two'), (3, 'three')"
        )
        await admin.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
        await admin.execute(f'GRANT SELECT ON public."{table}" TO "{role}"')

        database_url = (
            f"postgresql://{role}:{password}@{settings.database_host}:"
            f"{settings.database_port}/{settings.database_name}"
        )
        executor = DatabaseReadExecutor(max_rows=2)
        context = ToolExecutionContext(
            tenant_id=uuid4(),
            run_id=uuid4(),
            run_step_id=uuid4(),
            actor_id="database-integration-test",
            correlation_id=uuid4(),
            tool_config={"source": "integration"},
        )
        result = await executor.execute(
            context,
            {
                "source": "integration",
                "query": f'SELECT id, value FROM public."{table}" WHERE id >= $1 ORDER BY id',
                "parameters": [1],
            },
            {"database_url": database_url},
        )

        assert result.output == {
            "source": "integration",
            "columns": ["id", "value"],
            "rows": [[1, "one"], [2, "two"]],
            "row_count": 2,
            "truncated": True,
        }
    finally:
        await admin.execute(f'DROP TABLE IF EXISTS public."{table}"')
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = $1",
            role,
        )
        await admin.execute(f'DROP OWNED BY "{role}"')
        await admin.execute(f'DROP ROLE IF EXISTS "{role}"')
        await admin.close()
