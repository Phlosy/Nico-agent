"""Minimal MCP stdio server backed by a per-Run authenticated Unix broker."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from typing import Any

from nico_agent.tools.contracts import canonical_json

_PROTOCOL_VERSION = "2025-06-18"
_SUPPORTED_PROTOCOLS = frozenset({"2024-11-05", "2025-03-26", _PROTOCOL_VERSION})
_MAX_MESSAGE_BYTES = 1_100_000
_NAME_CHARACTER = re.compile(r"[^a-zA-Z0-9_-]")


class NicoMcpServer:
    def __init__(self, socket_path: str, token: str) -> None:
        self.socket_path = socket_path
        self.token = token
        self._tools: dict[str, dict[str, Any]] = {}

    async def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            requested = request.get("params", {}).get("protocolVersion")
            return self._result(
                request_id,
                {
                    "protocolVersion": (
                        requested if requested in _SUPPORTED_PROTOCOLS else _PROTOCOL_VERSION
                    ),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "nico-tool-gateway", "version": "1.0.0"},
                },
            )
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return None
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            tools = await self._list_tools()
            return self._result(request_id, {"tools": tools})
        if method == "tools/call":
            return self._result(
                request_id, await self._call_tool(request_id, request.get("params"))
            )
        return self._error(request_id, -32601, "method not found")

    async def _list_tools(self) -> list[dict[str, Any]]:
        response = await self._broker({"action": "list"})
        if not response.get("ok") or not isinstance(response.get("tools"), list):
            raise RuntimeError("tool listing failed")
        self._tools = {}
        result = []
        for value in response["tools"]:
            if not isinstance(value, dict):
                continue
            reference = f"{value.get('name')}@{value.get('version')}"
            mcp_name = _mcp_name(str(value.get("name", "")), str(value.get("version", "")))
            self._tools[mcp_name] = {**value, "reference": reference}
            result.append(
                {
                    "name": mcp_name,
                    "title": reference,
                    "description": f"{value.get('description', '')} (Nico tool {reference})",
                    "inputSchema": value.get("input_schema", {"type": "object"}),
                }
            )
        return result

    async def _call_tool(self, request_id: Any, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _tool_error("MCP_CALL_INVALID", "tool call parameters are invalid")
        if not self._tools:
            await self._list_tools()
        tool = self._tools.get(params["name"])
        arguments = params.get("arguments", {})
        if tool is None or not isinstance(arguments, dict):
            return _tool_error("MCP_TOOL_DENIED", "tool is not available in this Run")
        response = await self._broker(
            {
                "action": "call",
                "call_id": canonical_json(request_id)[:200],
                "tool": tool["reference"],
                "arguments": arguments,
            }
        )
        if not response.get("ok"):
            error = response.get("error", {})
            return _tool_error(str(error.get("code", "TOOL_FAILED")), "tool call failed")
        outcome = response.get("outcome", {})
        if outcome.get("status") != "succeeded":
            error = outcome.get("error") or {}
            return _tool_error(
                str(error.get("code", "TOOL_FAILED")),
                str(error.get("message", "tool call failed"))[:1000],
            )
        output = outcome.get("output") or {}
        return {
            "content": [{"type": "text", "text": canonical_json(output)}],
            "structuredContent": output,
            "isError": False,
        }

    async def _broker(self, request: dict[str, Any]) -> dict[str, Any]:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self.socket_path, limit=_MAX_MESSAGE_BYTES),
            timeout=5,
        )
        request["token"] = self.token
        writer.write((canonical_json(request) + "\n").encode())
        await writer.drain()
        try:
            async with asyncio.timeout(310):
                line = await reader.readline()
        finally:
            writer.close()
            await writer.wait_closed()
        if not line or len(line) > _MAX_MESSAGE_BYTES:
            raise RuntimeError("broker response is invalid")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RuntimeError("broker response is invalid")
        return value

    @staticmethod
    def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


async def serve() -> None:
    socket_path = os.environ.pop("NICO_MCP_SOCKET", "")
    token = os.environ.pop("NICO_MCP_TOKEN", "")
    if not socket_path or len(token) < 32:
        raise SystemExit(2)
    server = NicoMcpServer(socket_path, token)
    while line := await asyncio.to_thread(sys.stdin.buffer.readline):
        if len(line) > _MAX_MESSAGE_BYTES:
            continue
        request: Any = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                continue
            response = await server.handle(request)
        except Exception:
            request_id = request.get("id") if isinstance(request, dict) else None
            response = NicoMcpServer._error(request_id, -32603, "internal error")
        if response is not None:
            sys.stdout.write(canonical_json(response) + "\n")
            sys.stdout.flush()


def _mcp_name(name: str, version: str) -> str:
    return f"nico__{_NAME_CHARACTER.sub('_', name)}__v{_NAME_CHARACTER.sub('_', version)}"


def _tool_error(code: str, message: str) -> dict[str, Any]:
    error = {"code": code[:100], "message": message[:1000]}
    return {
        "content": [{"type": "text", "text": canonical_json(error)}],
        "structuredContent": {"error": error},
        "isError": True,
    }


if __name__ == "__main__":
    asyncio.run(serve())
