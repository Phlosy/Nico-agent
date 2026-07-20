"""Authenticated Unix-socket broker between MCP stdio children and the Worker."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any

from nico_agent.runtime.contracts import (
    RuntimeToolHandler,
    RuntimeToolIntent,
    RuntimeToolSession,
)
from nico_agent.tools.contracts import canonical_json
from nico_agent.tools.errors import ToolError

_MAX_MESSAGE_BYTES = 1_100_000


class McpGatewayHost:
    def __init__(self, handler: RuntimeToolHandler) -> None:
        self.handler = handler
        self._temporary = tempfile.TemporaryDirectory(prefix="nico-mcp-")
        self._directory = Path(self._temporary.name)
        self._socket_path = self._directory / "gateway.sock"
        self._token = secrets.token_urlsafe(48)
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.Task[Any]] = set()

    async def start(self) -> RuntimeToolSession:
        if self._server is not None:
            raise RuntimeError("MCP Gateway host is already started")
        self._server = await asyncio.start_unix_server(
            self._accept,
            path=self._socket_path,
            limit=_MAX_MESSAGE_BYTES,
        )
        os.chmod(self._socket_path, 0o600)
        return RuntimeToolSession(
            socket_path=str(self._socket_path),
            token=self._token,
            server_command=(sys.executable, "-m", "nico_agent.mcp.server"),
        )

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for task in tuple(self._clients):
            task.cancel()
        if self._clients:
            await asyncio.gather(*self._clients, return_exceptions=True)
        self._temporary.cleanup()

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self._clients.add(task)
        task.add_done_callback(self._clients.discard)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        response: dict[str, Any]
        try:
            async with asyncio.timeout(5):
                line = await reader.readline()
            if not line or len(line) > _MAX_MESSAGE_BYTES:
                raise ValueError("invalid broker request")
            import json

            request = json.loads(line)
            if not isinstance(request, dict) or not secrets.compare_digest(
                str(request.get("token", "")), self._token
            ):
                response = {"ok": False, "error": {"code": "MCP_UNAUTHORIZED"}}
            else:
                response = await self._dispatch(request)
        except (TimeoutError, ValueError, UnicodeDecodeError):
            response = {"ok": False, "error": {"code": "MCP_REQUEST_INVALID"}}
        except ToolError as exc:
            response = {
                "ok": False,
                "error": {"code": exc.code, "message": exc.message[:1000]},
            }
        except Exception:
            response = {"ok": False, "error": {"code": "MCP_GATEWAY_FAILED"}}
        encoded = (canonical_json(response) + "\n").encode()
        if len(encoded) > _MAX_MESSAGE_BYTES:
            encoded = b'{"error":{"code":"MCP_RESPONSE_TOO_LARGE"},"ok":false}\n'
        writer.write(encoded)
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        if action == "list":
            tools = await self.handler.list_tools()
            return {
                "ok": True,
                "tools": [tool.model_dump(mode="json") for tool in tools],
            }
        if action == "call":
            call_id = str(request.get("call_id", ""))[:200]
            reference = request.get("tool")
            arguments = request.get("arguments", {})
            if (
                not call_id
                or not isinstance(reference, str)
                or "@" not in reference
                or not isinstance(arguments, dict)
            ):
                raise ValueError("invalid tool call")
            name, version = reference.rsplit("@", 1)
            idempotency_material = canonical_json(
                {"call_id": call_id, "tool": reference, "arguments": arguments}
            )
            idempotency_key = "mcp:" + hashlib.sha256(idempotency_material.encode()).hexdigest()
            outcome = await self.handler.execute_tool(
                RuntimeToolIntent(
                    call_id=call_id,
                    name=name,
                    version=version,
                    arguments=arguments,
                    idempotency_key=idempotency_key,
                )
            )
            return {"ok": True, "outcome": outcome.model_dump(mode="json")}
        raise ValueError("unknown broker action")
