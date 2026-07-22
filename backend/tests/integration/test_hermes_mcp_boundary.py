from __future__ import annotations

import asyncio
import os
import shutil
import site
from pathlib import Path
from uuid import uuid4

import pytest

from nico_agent.mcp import McpGatewayHost
from nico_agent.runtime import HermesRuntimeProvider, RuntimeSessionRequest
from nico_agent.runtime.contracts import RuntimeToolOutcome, RuntimeToolSpec

REFERENCE_ROOT = Path(__file__).resolve().parents[4]

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="set RUN_INTEGRATION=1 to run the local Hermes MCP boundary",
)


class ListingHandler:
    async def list_tools(self):
        return (
            RuntimeToolSpec(
                name="web.search",
                version="1.0.0",
                description="Search the current public Web",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            ),
            RuntimeToolSpec(
                name="web.fetch",
                version="1.0.0",
                description="Fetch an observed Web source",
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "search_tool_call_id": {"type": "string"},
                    },
                    "required": ["url"],
                },
            ),
        )

    async def execute_tool(self, intent):
        return RuntimeToolOutcome(call_id=intent.call_id, status="failed")


@pytest.mark.asyncio
async def test_real_hermes_0182_discovers_only_nico_mcp_tools(tmp_path: Path) -> None:
    reference = REFERENCE_ROOT / "hermes-agent"
    hermes = reference / "hermes"
    if not hermes.is_file():
        pytest.skip("local Hermes 0.18.2 checkout is unavailable")
    hermes_python = shutil.which("python3")
    if hermes_python is None:
        pytest.skip("Hermes host Python is unavailable")
    test_packages = site.getsitepackages()[0]

    host = McpGatewayHost(ListingHandler())
    tool_session = await host.start()
    provider = HermesRuntimeProvider(
        (hermes_python, str(hermes)),
        environment={"PYTHONPATH": test_packages},
        state_root=tmp_path / "hermes-state",
    )
    request = RuntimeSessionRequest(
        tenant_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        task_title="Hermes MCP discovery",
        role="integration-test",
        mandate="Discover only Nico tools",
        tool_session=tool_session,
    )
    try:
        assert await provider.probe_version() == "0.18.2"
        handle = await provider.create_session(request)
        session = provider._sessions[handle.external_session_id]
        environment = os.environ.copy()
        environment["HERMES_HOME"] = str(session.hermes_home)
        environment["PYTHONPATH"] = test_packages
        process = await asyncio.create_subprocess_exec(
            hermes_python,
            str(hermes),
            "mcp",
            "test",
            "nico",
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        output = (stdout + stderr).decode(errors="replace")

        assert process.returncode == 0, output
        assert "nico" in output.lower()
        assert "web.search" in output or "nico__web_search__v1_0_0" in output
        assert "web.fetch" in output or "nico__web_fetch__v1_0_0" in output
        assert tool_session.token not in output
        assert "terminal" not in output.lower()
    finally:
        for session in provider._sessions.values():
            provider._cleanup_home(session)
        await host.close()
