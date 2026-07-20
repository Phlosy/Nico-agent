"""Read-only PostgreSQL query tool with server and lexical safety layers."""

from __future__ import annotations

import base64
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg

from nico_agent.tools.contracts import (
    ToolDefinitionSpec,
    ToolExecutionResult,
    ToolIsolation,
    ToolRetryPolicy,
    ToolRisk,
    canonical_hash,
    canonical_json,
)
from nico_agent.tools.errors import ToolExecutorFailure

Connector = Callable[..., Awaitable[Any]]

_FORBIDDEN_WORDS = frozenset(
    {
        "alter",
        "analyze",
        "call",
        "checkpoint",
        "cluster",
        "comment",
        "commit",
        "copy",
        "create",
        "deallocate",
        "delete",
        "discard",
        "do",
        "drop",
        "execute",
        "explain",
        "grant",
        "import",
        "insert",
        "listen",
        "load",
        "lock",
        "merge",
        "notify",
        "operator",
        "prepare",
        "reassign",
        "refresh",
        "reindex",
        "release",
        "reset",
        "revoke",
        "rollback",
        "savepoint",
        "security",
        "set",
        "show",
        "start",
        "truncate",
        "update",
        "vacuum",
    }
)
_FORBIDDEN_FUNCTIONS = frozenset(
    {
        "dblink",
        "dblink_connect",
        "dblink_exec",
        "lo_export",
        "lo_import",
        "nextval",
        "pg_advisory_lock",
        "pg_advisory_xact_lock",
        "pg_cancel_backend",
        "pg_create_restore_point",
        "pg_logical_emit_message",
        "pg_read_binary_file",
        "pg_read_file",
        "pg_reload_conf",
        "pg_rotate_logfile",
        "pg_sleep",
        "pg_terminate_backend",
        "set_config",
    }
)
_WORD = re.compile(r"[a-z_][a-z0-9_$]*", re.IGNORECASE)
_FUNCTION = re.compile(r"([a-z_][a-z0-9_$]*)\s*\(", re.IGNORECASE)
_LOCKING_CLAUSE = re.compile(r"\bfor\s+(?:no\s+key\s+update|key\s+share|update|share)\b", re.I)
_QUOTED_FUNCTION = re.compile(r'"(?:[^"]|"")*"\s*\(')


class DatabaseReadExecutor:
    spec = ToolDefinitionSpec(
        name="database.read",
        version="1.0.0",
        description=(
            "Run one bounded parameterized query on an administrator-provided "
            "read-only PostgreSQL source"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "source": {"type": "string", "minLength": 1, "maxLength": 100},
                "query": {"type": "string", "minLength": 1, "maxLength": 100000},
                "parameters": {"type": "array", "maxItems": 100},
            },
            "required": ["source", "query"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "columns": {"type": "array", "items": {"type": "string"}},
                "rows": {"type": "array", "items": {"type": "array"}},
                "row_count": {"type": "integer", "minimum": 0},
                "truncated": {"type": "boolean"},
            },
            "required": ["source", "columns", "rows", "row_count", "truncated"],
            "additionalProperties": False,
        },
        permission="database.read",
        timeout_seconds=30,
        retry_policy=ToolRetryPolicy(
            max_attempts=2,
            backoff_seconds=0.2,
            retryable_codes=frozenset({"DATABASE_UNAVAILABLE"}),
        ),
        isolation=ToolIsolation.READ_ONLY_DATABASE,
        risk=ToolRisk.MEDIUM,
        max_output_bytes=1_100_000,
        secret_names=frozenset({"database_url"}),
    )
    implementation_hash = canonical_hash({"executor": "database.read", "revision": 1})

    def __init__(
        self,
        *,
        connector: Connector = asyncpg.connect,
        connect_timeout: float = 5.0,
        statement_timeout_ms: int = 5_000,
        max_rows: int = 500,
        max_output_bytes: int = 1_048_576,
    ) -> None:
        self.connector = connector
        self.connect_timeout = connect_timeout
        self.statement_timeout_ms = statement_timeout_ms
        self.max_rows = max_rows
        self.max_output_bytes = max_output_bytes

    async def execute(self, context, arguments, secrets):
        source = arguments["source"]
        if source != context.tool_config.get("source"):
            raise ToolExecutorFailure("DATABASE_SOURCE_DENIED", "database source is not allowed")
        database_url = secrets.get("database_url")
        if not database_url:
            raise ToolExecutorFailure(
                "DATABASE_SECRET_UNAVAILABLE", "database source credentials are unavailable"
            )
        query = validate_read_query(arguments["query"])
        parameters = arguments.get("parameters", [])
        max_rows = _restrict_int(context.tool_config.get("max_rows"), self.max_rows)
        timeout_ms = _restrict_int(
            context.tool_config.get("statement_timeout_ms"), self.statement_timeout_ms
        )
        output_limit = _restrict_int(
            context.tool_config.get("max_output_bytes"), self.max_output_bytes
        )
        connection = None
        try:
            connection = await self.connector(database_url, timeout=self.connect_timeout)
            async with connection.transaction(readonly=True):
                await connection.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
                await connection.execute(
                    f"SET LOCAL idle_in_transaction_session_timeout = {timeout_ms}"
                )
                await _verify_read_only_role(connection)
                bounded_query = (
                    f"SELECT * FROM ({query}) AS nico_bounded_query LIMIT {max_rows + 1}"
                )
                statement = await connection.prepare(bounded_query)
                attributes = statement.get_attributes()
                columns = [attribute.name for attribute in attributes]
                records = await statement.fetch(*parameters, timeout=timeout_ms / 1000)
        except ToolExecutorFailure:
            raise
        except (TimeoutError, asyncpg.QueryCanceledError) as exc:
            raise ToolExecutorFailure("DATABASE_TIMEOUT", "database read timed out") from exc
        except (OSError, asyncpg.PostgresConnectionError) as exc:
            raise ToolExecutorFailure(
                "DATABASE_UNAVAILABLE", "database source is unavailable"
            ) from exc
        except (asyncpg.PostgresError, ValueError, TypeError) as exc:
            raise ToolExecutorFailure("DATABASE_QUERY_FAILED", "database read failed") from exc
        finally:
            if connection is not None:
                try:
                    await connection.close(timeout=self.connect_timeout)
                except Exception:
                    pass

        truncated = len(records) > max_rows
        rows = [serialize_record(record, columns) for record in records[:max_rows]]
        output = {
            "source": source,
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }
        try:
            size = len(canonical_json(output).encode())
        except (TypeError, ValueError) as exc:
            raise ToolExecutorFailure(
                "DATABASE_VALUE_INVALID", "database returned an unsupported value"
            ) from exc
        if size > output_limit:
            raise ToolExecutorFailure(
                "DATABASE_OUTPUT_TOO_LARGE", "database result exceeded its byte limit"
            )
        return ToolExecutionResult(
            output=output,
            usage={"rows": len(rows), "output_bytes": size},
        )


async def _verify_read_only_role(connection: Any) -> None:
    role = await connection.fetchrow(
        """
        SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname = current_user
        """
    )
    read_only = await connection.fetchval("SHOW transaction_read_only")
    if role is None or any(bool(role[name]) for name in role.keys()) or read_only != "on":
        raise ToolExecutorFailure(
            "DATABASE_ROLE_UNSAFE", "database source role is not safely read-only"
        )


def validate_read_query(query: str) -> str:
    if not isinstance(query, str) or not query.strip() or len(query.encode()) > 100_000:
        raise ToolExecutorFailure("DATABASE_QUERY_INVALID", "database query is invalid")
    code, semicolons = _scan_sql(query)
    stripped = code.strip()
    if semicolons:
        if len(semicolons) != 1 or query[semicolons[0] + 1 :].strip():
            raise ToolExecutorFailure(
                "DATABASE_MULTI_STATEMENT_DENIED", "database query must contain one statement"
            )
        stripped = code[: semicolons[0]].strip()
    words = [match.group(0).lower() for match in _WORD.finditer(stripped)]
    if not words or words[0] not in {"select", "with"}:
        raise ToolExecutorFailure(
            "DATABASE_STATEMENT_DENIED", "database query must be SELECT or WITH"
        )
    if _FORBIDDEN_WORDS.intersection(words) or _LOCKING_CLAUSE.search(stripped):
        raise ToolExecutorFailure(
            "DATABASE_STATEMENT_DENIED", "database query contains a denied operation"
        )
    if "::" in stripped or _QUOTED_FUNCTION.search(stripped):
        raise ToolExecutorFailure(
            "DATABASE_FUNCTION_DENIED", "database query contains an unsafe invocation"
        )
    functions = {match.group(1).lower() for match in _FUNCTION.finditer(stripped)}
    if functions & _FORBIDDEN_FUNCTIONS:
        raise ToolExecutorFailure(
            "DATABASE_FUNCTION_DENIED", "database query contains a denied function"
        )
    return query[: semicolons[0]].strip() if semicolons else query.strip()


def _scan_sql(query: str) -> tuple[str, list[int]]:
    output = list(query)
    semicolons: list[int] = []
    index = 0
    length = len(query)
    while index < length:
        character = query[index]
        if character == "'":
            index = _blank_quoted(query, output, index, "'")
            continue
        if character == '"':
            index = _blank_quoted_identifier(query, output, index)
            continue
        if character == "$" and (delimiter := _dollar_delimiter(query, index)):
            end = query.find(delimiter, index + len(delimiter))
            if end < 0:
                raise ToolExecutorFailure(
                    "DATABASE_QUERY_INVALID", "database query has an unterminated string"
                )
            for position in range(index, end + len(delimiter)):
                output[position] = " "
            index = end + len(delimiter)
            continue
        if query.startswith("--", index) or query.startswith("/*", index):
            raise ToolExecutorFailure(
                "DATABASE_COMMENT_DENIED", "database query comments are not allowed"
            )
        if character == ";":
            semicolons.append(index)
        index += 1
    return "".join(output), semicolons


def _blank_quoted(query: str, output: list[str], start: int, quote: str) -> int:
    output[start] = " "
    index = start + 1
    while index < len(query):
        output[index] = " "
        if query[index] == quote:
            if index + 1 < len(query) and query[index + 1] == quote:
                output[index + 1] = " "
                index += 2
                continue
            return index + 1
        index += 1
    raise ToolExecutorFailure("DATABASE_QUERY_INVALID", "database query has an unterminated string")


def _blank_quoted_identifier(query: str, output: list[str], start: int) -> int:
    output[start] = '"'
    index = start + 1
    while index < len(query):
        if query[index] == '"':
            if index + 1 < len(query) and query[index + 1] == '"':
                output[index] = " "
                output[index + 1] = " "
                index += 2
                continue
            output[index] = '"'
            return index + 1
        output[index] = " "
        index += 1
    raise ToolExecutorFailure(
        "DATABASE_QUERY_INVALID", "database query has an unterminated identifier"
    )


def _dollar_delimiter(query: str, start: int) -> str | None:
    match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", query[start:])
    return match.group(0) if match else None


def serialize_record(record: Any, columns: list[str]) -> list[Any]:
    if isinstance(record, Mapping):
        values = [record[column] for column in columns]
    else:
        values = list(record)
    return [_json_value(value) for value in values]


def _json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 10:
        raise ToolExecutorFailure("DATABASE_VALUE_INVALID", "database value nesting is too deep")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ToolExecutorFailure(
                "DATABASE_VALUE_INVALID", "database returned a non-finite number"
            )
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return [_json_value(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _json_value(item, depth=depth + 1) for key, item in value.items()}
    raise ToolExecutorFailure("DATABASE_VALUE_INVALID", "database returned an unsupported value")


def _restrict_int(value: Any, platform_limit: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return min(value, platform_limit)
    return platform_limit
