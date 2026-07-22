from __future__ import annotations

import asyncio
import json
import os
from uuid import uuid4

import pytest

from nico_agent.mcp import McpGatewayHost
from nico_agent.mcp.server import NicoMcpServer
from nico_agent.runtime.contracts import (
    RuntimeToolIntent,
    RuntimeToolOutcome,
    RuntimeToolSpec,
)


class FakeToolHandler:
    def __init__(self) -> None:
        self.intents: list[RuntimeToolIntent] = []

    async def list_tools(self):
        return (
            RuntimeToolSpec(
                name="file.read",
                version="1.0.0",
                description="Read a scoped file",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            ),
        )

    async def execute_tool(self, intent):
        self.intents.append(intent)
        return RuntimeToolOutcome(
            call_id=intent.call_id,
            tool_call_id="00000000-0000-0000-0000-000000000111",
            run_step_id="00000000-0000-0000-0000-000000000112",
            status="succeeded",
            output={"path": intent.arguments["path"], "content": "hello"},
        )


class FakeWebToolHandler:
    search_tool_call_id = "00000000-0000-0000-0000-000000000211"

    def __init__(self) -> None:
        self.intents: list[RuntimeToolIntent] = []

    async def list_tools(self):
        return (
            RuntimeToolSpec(
                name="web.search",
                version="1.0.0",
                description="Search the current public Web",
                input_schema={"type": "object"},
            ),
            RuntimeToolSpec(
                name="web.fetch",
                version="1.0.0",
                description="Fetch an observed Web source",
                input_schema={"type": "object"},
            ),
        )

    async def execute_tool(self, intent):
        self.intents.append(intent)
        if intent.name == "web.search":
            return RuntimeToolOutcome(
                call_id=intent.call_id,
                tool_call_id=self.search_tool_call_id,
                run_step_id="00000000-0000-0000-0000-000000000212",
                status="succeeded",
                output={"results": [{"url": "https://docs.example/nico"}]},
            )
        assert intent.arguments["search_tool_call_id"] == self.search_tool_call_id
        return RuntimeToolOutcome(
            call_id=intent.call_id,
            tool_call_id="00000000-0000-0000-0000-000000000213",
            run_step_id="00000000-0000-0000-0000-000000000214",
            status="succeeded",
            output={"final_url": "https://docs.example/nico", "content": "Nico docs"},
        )


@pytest.mark.asyncio
async def test_mcp_protocol_lists_exact_authorized_version_and_calls_broker() -> None:
    handler = FakeToolHandler()
    host = McpGatewayHost(handler)
    session = await host.start()
    server = NicoMcpServer(session.socket_path, session.token)
    try:
        initialized = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        listed = await server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool = listed["result"]["tools"][0]
        called = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": tool["name"], "arguments": {"path": "note.txt"}},
            }
        )

        assert initialized["result"]["serverInfo"]["name"] == "nico-tool-gateway"
        assert tool["title"] == "file.read@1.0.0"
        assert tool["name"] == "nico__file_read__v1_0_0"
        assert called["result"]["isError"] is False
        assert called["result"]["structuredContent"]["content"] == "hello"
        assert called["result"]["structuredContent"]["_nico"] == {
            "tool_call_id": "00000000-0000-0000-0000-000000000111",
            "run_step_id": "00000000-0000-0000-0000-000000000112",
        }
        assert handler.intents[0].name == "file.read"
        assert handler.intents[0].version == "1.0.0"
        assert handler.intents[0].idempotency_key.startswith("mcp:")
        assert session.token not in repr(session)
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_mcp_web_search_exposes_platform_id_for_follow_up_fetch() -> None:
    handler = FakeWebToolHandler()
    host = McpGatewayHost(handler)
    session = await host.start()
    server = NicoMcpServer(session.socket_path, session.token)
    try:
        listed = await server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = {tool["title"]: tool for tool in listed["result"]["tools"]}
        search = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": "search",
                "method": "tools/call",
                "params": {
                    "name": tools["web.search@1.0.0"]["name"],
                    "arguments": {"query": "current Nico documentation"},
                },
            }
        )
        platform_id = search["result"]["structuredContent"]["_nico"]["tool_call_id"]
        fetched = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": "fetch",
                "method": "tools/call",
                "params": {
                    "name": tools["web.fetch@1.0.0"]["name"],
                    "arguments": {
                        "url": "https://docs.example/nico",
                        "search_tool_call_id": platform_id,
                    },
                },
            }
        )

        assert platform_id == handler.search_tool_call_id
        assert fetched["result"]["structuredContent"]["final_url"] == ("https://docs.example/nico")
        assert [intent.name for intent in handler.intents] == ["web.search", "web.fetch"]
        assert "_nico.tool_call_id" in tools["web.search@1.0.0"]["description"]
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_mcp_idempotency_is_stable_across_child_restart_and_bound_to_arguments() -> None:
    handler = FakeToolHandler()
    host = McpGatewayHost(handler)
    session = await host.start()
    first = NicoMcpServer(session.socket_path, session.token)
    recovered = NicoMcpServer(session.socket_path, session.token)
    try:
        list_request = {"jsonrpc": "2.0", "id": "list", "method": "tools/list"}
        first_tool = (await first.handle(list_request))["result"]["tools"][0]["name"]
        recovered_tool = (await recovered.handle(list_request))["result"]["tools"][0]["name"]
        base_call = {
            "jsonrpc": "2.0",
            "id": "logical-call-7",
            "method": "tools/call",
            "params": {"name": first_tool, "arguments": {"path": "same.txt"}},
        }
        recovered_call = {
            **base_call,
            "params": {"name": recovered_tool, "arguments": {"path": "same.txt"}},
        }
        changed_call = {
            **recovered_call,
            "params": {"name": recovered_tool, "arguments": {"path": "different.txt"}},
        }

        await first.handle(base_call)
        await recovered.handle(recovered_call)
        await recovered.handle(changed_call)

        assert handler.intents[0].idempotency_key == handler.intents[1].idempotency_key
        assert handler.intents[2].idempotency_key != handler.intents[1].idempotency_key
    finally:
        await host.close()


@pytest.mark.asyncio
async def test_real_stdio_server_initialize_list_and_call() -> None:
    handler = FakeToolHandler()
    host = McpGatewayHost(handler)
    session = await host.start()
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "NICO_MCP_SOCKET": session.socket_path,
        "NICO_MCP_TOKEN": session.token,
    }
    process = await asyncio.create_subprocess_exec(
        *session.server_command,
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None
    try:

        async def rpc(request):
            process.stdin.write((json.dumps(request) + "\n").encode())
            await process.stdin.drain()
            return json.loads(await asyncio.wait_for(process.stdout.readline(), timeout=3))

        initialized = await rpc({"jsonrpc": "2.0", "id": "init", "method": "initialize"})
        listed = await rpc({"jsonrpc": "2.0", "id": "list", "method": "tools/list"})
        tool_name = listed["result"]["tools"][0]["name"]
        called = await rpc(
            {
                "jsonrpc": "2.0",
                "id": "call",
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": {"path": "stdio.txt"}},
            }
        )

        assert initialized["result"]["capabilities"] == {"tools": {"listChanged": False}}
        assert called["result"]["structuredContent"]["path"] == "stdio.txt"
        assert handler.intents[0].arguments == {"path": "stdio.txt"}
    finally:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=3)
        await host.close()


@pytest.mark.asyncio
async def test_broker_rejects_wrong_token_without_listing_tools() -> None:
    handler = FakeToolHandler()
    host = McpGatewayHost(handler)
    session = await host.start()
    try:
        reader, writer = await asyncio.open_unix_connection(session.socket_path)
        writer.write(
            (
                json.dumps({"token": "x" * 64, "action": "list", "nonce": str(uuid4())}) + "\n"
            ).encode()
        )
        await writer.drain()
        response = json.loads(await reader.readline())
        writer.close()
        await writer.wait_closed()

        assert response == {"ok": False, "error": {"code": "MCP_UNAUTHORIZED"}}
        assert handler.intents == []
    finally:
        await host.close()
