from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from nico_agent.tools import ToolExecutionContext
from nico_agent.tools.builtin import DatabaseReadExecutor, validate_read_query
from nico_agent.tools.errors import ToolExecutorFailure


class FakeTransaction(AbstractAsyncContextManager):
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False


class FakeAttribute:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeStatement:
    def __init__(self, columns, records) -> None:
        self.columns = columns
        self.records = records
        self.parameters = None
        self.timeout = None

    def get_attributes(self):
        return [FakeAttribute(name) for name in self.columns]

    async def fetch(self, *parameters, **kwargs):
        self.parameters = parameters
        self.timeout = kwargs["timeout"]
        return self.records


class FakeConnection:
    def __init__(self, *, columns=None, records=None, unsafe_role=False, read_only="on") -> None:
        self.statement = FakeStatement(columns or ["value"], records or [])
        self.unsafe_role = unsafe_role
        self.read_only = read_only
        self.executed: list[str] = []
        self.prepared_query = None
        self.transaction_readonly = None
        self.closed = False

    def transaction(self, *, readonly):
        self.transaction_readonly = readonly
        return FakeTransaction()

    async def execute(self, query):
        self.executed.append(query)

    async def fetchrow(self, query):
        return {
            "rolsuper": self.unsafe_role,
            "rolcreaterole": False,
            "rolcreatedb": False,
            "rolreplication": False,
            "rolbypassrls": False,
        }

    async def fetchval(self, query):
        return self.read_only

    async def prepare(self, query):
        self.prepared_query = query
        return self.statement

    async def close(self, **kwargs):
        self.closed = True


def _context(config=None) -> ToolExecutionContext:
    return ToolExecutionContext(
        tenant_id=uuid4(),
        run_id=uuid4(),
        run_step_id=uuid4(),
        actor_id="database-test",
        correlation_id=uuid4(),
        tool_config=config or {"source": "analytics"},
    )


@pytest.mark.parametrize(
    "query",
    [
        "SELECT id, name FROM public.assets WHERE id = $1",
        "WITH latest AS (SELECT max(ts) AS ts FROM prices) SELECT ts FROM latest;",
        "SELECT 'delete; -- is data' AS value",
        'SELECT "update" FROM "daily-prices"',
        "SELECT $$drop table hidden$$ AS text",
    ],
)
def test_read_query_validator_accepts_one_select_or_with(query: str) -> None:
    assert validate_read_query(query)


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("DELETE FROM prices", "DATABASE_STATEMENT_DENIED"),
        (
            "WITH gone AS (DELETE FROM prices RETURNING *) SELECT * FROM gone",
            "DATABASE_STATEMENT_DENIED",
        ),
        ("SELECT 1; SELECT 2", "DATABASE_MULTI_STATEMENT_DENIED"),
        ("SELECT 1 -- hidden", "DATABASE_COMMENT_DENIED"),
        ("SELECT /* hidden */ 1", "DATABASE_COMMENT_DENIED"),
        ("SELECT * FROM prices FOR UPDATE", "DATABASE_STATEMENT_DENIED"),
        ("SELECT pg_catalog.pg_read_file('/etc/passwd')", "DATABASE_FUNCTION_DENIED"),
        ("SELECT pg_sleep(10)", "DATABASE_FUNCTION_DENIED"),
        ('SELECT "custom_function"()', "DATABASE_FUNCTION_DENIED"),
        ("SELECT value::custom_type FROM prices", "DATABASE_FUNCTION_DENIED"),
        ("COPY prices TO STDOUT", "DATABASE_STATEMENT_DENIED"),
        ("SELECT 'unterminated", "DATABASE_QUERY_INVALID"),
    ],
)
def test_read_query_validator_rejects_writes_evasion_locks_and_dangerous_functions(
    query: str, code: str
) -> None:
    with pytest.raises(ToolExecutorFailure) as captured:
        validate_read_query(query)
    assert captured.value.code == code


@pytest.mark.asyncio
async def test_database_executor_uses_readonly_transaction_bounds_and_parameters() -> None:
    connection = FakeConnection(
        columns=["id", "amount", "at"],
        records=[
            {"id": uuid4(), "amount": Decimal("12.3400"), "at": datetime(2026, 1, 2, tzinfo=UTC)},
            {"id": uuid4(), "amount": Decimal("9.1"), "at": datetime(2026, 1, 3, tzinfo=UTC)},
            {"id": uuid4(), "amount": Decimal("8.2"), "at": datetime(2026, 1, 4, tzinfo=UTC)},
        ],
    )
    seen: dict = {}

    async def connect(database_url, **kwargs):
        seen.update(database_url=database_url, timeout=kwargs["timeout"])
        return connection

    executor = DatabaseReadExecutor(
        connector=connect,
        max_rows=10,
        statement_timeout_ms=5_000,
    )
    result = await executor.execute(
        _context({"source": "analytics", "max_rows": 2, "statement_timeout_ms": 1200}),
        {
            "source": "analytics",
            "query": "SELECT id, amount, at FROM ledger WHERE amount > $1;",
            "parameters": [5],
        },
        {"database_url": "postgresql://readonly:secret@database/analytics"},
    )

    assert seen["database_url"].startswith("postgresql://readonly:")
    assert connection.transaction_readonly is True
    assert connection.executed == [
        "SET LOCAL statement_timeout = 1200",
        "SET LOCAL idle_in_transaction_session_timeout = 1200",
    ]
    assert connection.prepared_query.endswith("LIMIT 3")
    assert connection.statement.parameters == (5,)
    assert result.output["row_count"] == 2
    assert result.output["truncated"] is True
    assert result.output["rows"][0][1] == "12.3400"
    assert result.output["rows"][0][2] == "2026-01-02T00:00:00+00:00"
    assert connection.closed is True


@pytest.mark.asyncio
async def test_source_secret_role_and_transaction_state_fail_closed() -> None:
    executor = DatabaseReadExecutor(connector=lambda *args, **kwargs: None)
    with pytest.raises(ToolExecutorFailure) as source:
        await executor.execute(
            _context(),
            {"source": "other", "query": "SELECT 1"},
            {"database_url": "secret"},
        )
    assert source.value.code == "DATABASE_SOURCE_DENIED"

    with pytest.raises(ToolExecutorFailure) as secret:
        await executor.execute(_context(), {"source": "analytics", "query": "SELECT 1"}, {})
    assert secret.value.code == "DATABASE_SECRET_UNAVAILABLE"

    for connection in (FakeConnection(unsafe_role=True), FakeConnection(read_only="off")):

        async def connect(database_url, selected=connection, **kwargs):
            return selected

        unsafe_executor = DatabaseReadExecutor(connector=connect)
        with pytest.raises(ToolExecutorFailure) as unsafe:
            await unsafe_executor.execute(
                _context(),
                {"source": "analytics", "query": "SELECT 1"},
                {"database_url": "must-not-leak"},
            )
        assert unsafe.value.code == "DATABASE_ROLE_UNSAFE"
        assert "must-not-leak" not in unsafe.value.message
        assert connection.closed is True


@pytest.mark.asyncio
async def test_database_output_limit_and_unsupported_value_are_stable() -> None:
    for record, output_limit, expected in (
        ({"value": "x" * 200}, 50, "DATABASE_OUTPUT_TOO_LARGE"),
        ({"value": object()}, 1000, "DATABASE_VALUE_INVALID"),
    ):
        connection = FakeConnection(records=[record])

        async def connect(database_url, selected=connection, **kwargs):
            return selected

        executor = DatabaseReadExecutor(connector=connect, max_output_bytes=output_limit)
        with pytest.raises(ToolExecutorFailure) as captured:
            await executor.execute(
                _context(),
                {"source": "analytics", "query": "SELECT value FROM data"},
                {"database_url": "postgresql://hidden"},
            )
        assert captured.value.code == expected


@pytest.mark.asyncio
async def test_connection_failure_is_retryable_and_does_not_leak_dsn() -> None:
    async def connect(database_url, **kwargs):
        raise OSError(f"cannot connect to {database_url}")

    executor = DatabaseReadExecutor(connector=connect)
    with pytest.raises(ToolExecutorFailure) as captured:
        await executor.execute(
            _context(),
            {"source": "analytics", "query": "SELECT 1"},
            {"database_url": "postgresql://user:very-secret@hidden/database"},
        )

    assert captured.value.code == "DATABASE_UNAVAILABLE"
    assert "very-secret" not in captured.value.message
