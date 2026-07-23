from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from nico_agent.runtime.contracts import (
    RuntimeCapability,
    RuntimeDisposition,
    RuntimeEventType,
    RuntimeServices,
    RuntimeSessionRequest,
    RuntimeSessionStatus,
    RuntimeToolSession,
)
from nico_agent.runtime.errors import RuntimeCapabilityUnsupported, RuntimeExecutionFailed
from nico_agent.runtime.hermes import HermesRuntimeProvider

_FAKE_HERMES = """\
import json
import os
import sys
import time

args = sys.argv[1:]
capture = os.environ.get("FAKE_HERMES_CAPTURE")
if capture:
    with open(capture, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(args) + "\\n")

if args and args[0] == "version":
    print(f"Hermes Agent v{os.environ.get('FAKE_HERMES_VERSION', '0.18.2')} (fake)")
    raise SystemExit(0)

if args[:2] == ["sessions", "export"]:
    session_id = args[args.index("--session-id") + 1]
    print(json.dumps({
        "id": session_id,
        "messages": [
            {"role": "user", "content": "exported input"},
            {"role": "assistant", "content": "exported answer"},
        ],
    }))
    raise SystemExit(0)

if args and args[0] == "chat":
    prompt = args[args.index("-q") + 1]
    if '"mode": "cancel"' in prompt:
        time.sleep(30)
    if '"mode": "fail"' in prompt:
        print("api_key=super-secret-value", file=sys.stderr)
        raise SystemExit(7)
    session_id = args[args.index("--resume") + 1] if "--resume" in args else "fake-session-001"
    print("fake Hermes answer")
    print(f"session_id: {session_id}", file=sys.stderr)
    raise SystemExit(0)

print("unsupported fake invocation", file=sys.stderr)
raise SystemExit(2)
"""


def _request(
    *,
    mode: str = "success",
    resume_session_id: str | None = None,
    event_sequence: int = 0,
    tool_session: RuntimeToolSession | None = None,
) -> RuntimeSessionRequest:
    return RuntimeSessionRequest(
        tenant_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        task_title="Hermes adapter acceptance",
        task_input={"mode": mode},
        acceptance={"done": True},
        role="researcher",
        mandate="Complete the supplied task",
        boundaries=["do not expose secrets"],
        model_config_data={"model": "fake-model", "provider": "fake-provider"},
        run_config={"hermes": {}},
        resume_session_id=resume_session_id,
        event_sequence=event_sequence,
        tool_session=tool_session,
    )


def _provider(tmp_path: Path) -> tuple[HermesRuntimeProvider, Path]:
    script = tmp_path / "fake_hermes.py"
    script.write_text(_FAKE_HERMES, encoding="utf-8")
    capture = tmp_path / "calls.jsonl"
    provider = HermesRuntimeProvider(
        (sys.executable, str(script)),
        environment={"FAKE_HERMES_CAPTURE": str(capture)},
        state_root=tmp_path / "hermes-state",
    )
    return provider, capture


@pytest.mark.asyncio
async def test_hermes_cli_success_normalizes_events_and_redacted_export(tmp_path: Path) -> None:
    provider, capture = _provider(tmp_path)
    request = _request()
    assert await provider.probe_version() == "0.18.2"
    handle = await provider.create_session(request)
    events_task = asyncio.create_task(_events(provider, handle.external_session_id))

    result = await provider.run(handle.external_session_id, request)
    events = await events_task
    trajectory = await provider.export_trajectory(handle.external_session_id)
    calls = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]

    assert result.status is RuntimeSessionStatus.COMPLETED
    assert result.output == {"message": "fake Hermes answer"}
    assert result.external_session_id == "fake-session-001"
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    output_delta = next(event for event in events if event.type is RuntimeEventType.OUTPUT_DELTA)
    assert output_delta.payload["visibility"] == "assistant"
    assert events[-1].type is RuntimeEventType.RUN_COMPLETED
    assert trajectory.external_session_id == "fake-session-001"
    assert trajectory.messages[-1]["content"] == "exported answer"
    assert calls[0] == ["version"]
    chat_call = calls[1]
    assert chat_call[0] == "chat"
    assert chat_call[chat_call.index("--model") + 1] == "fake-model"
    assert chat_call[chat_call.index("--provider") + 1] == "fake-provider"
    assert "--toolsets" not in chat_call
    export_call = calls[2]
    assert export_call[:2] == ["sessions", "export"]
    assert "--redact" in export_call


@pytest.mark.asyncio
async def test_hermes_executes_protocol_v2_with_honest_capabilities(tmp_path: Path) -> None:
    provider, _ = _provider(tmp_path)
    request = _request()
    handle = await provider.create_session(request)

    outcome = await provider.execute(
        handle.external_session_id,
        request,
        RuntimeServices(),
    )

    assert outcome.disposition is RuntimeDisposition.TERMINAL
    assert outcome.status is RuntimeSessionStatus.COMPLETED
    assert provider.descriptor.protocol_version == "2.0"
    assert provider.descriptor.implementation == "adapter"
    assert RuntimeCapability.PLANNING not in provider.descriptor.capabilities
    assert RuntimeCapability.COORDINATION not in provider.descriptor.capabilities


@pytest.mark.asyncio
async def test_hermes_uses_per_run_home_and_only_nico_mcp_toolset(tmp_path: Path) -> None:
    provider, capture = _provider(tmp_path)
    token = "mcp-session-token-that-is-never-an-argument"
    request = _request(
        tool_session=RuntimeToolSession(
            socket_path="/tmp/nico-test.sock",
            token=token,
            server_command=(sys.executable, "-m", "nico_agent.mcp.server"),
        )
    )
    handle = await provider.create_session(request)
    session = provider._sessions[handle.external_session_id]
    assert session.hermes_home is not None
    config_path = session.hermes_home / "config.yaml"
    config = json.loads(config_path.read_text(encoding="utf-8"))

    assert config_path.stat().st_mode & 0o777 == 0o600
    assert config["platform_toolsets"]["cli"] == ["nico"]
    assert list(config["mcp_servers"]) == ["nico"]
    assert config["mcp_servers"]["nico"]["env"]["NICO_MCP_TOKEN"] == token
    assert {"terminal", "web", "browser"}.issubset(config["agent"]["disabled_toolsets"])

    result = await provider.run(handle.external_session_id, request)
    await provider.export_trajectory(handle.external_session_id)
    calls = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]
    chat_call = calls[1]
    assert result.status is RuntimeSessionStatus.COMPLETED
    assert chat_call[chat_call.index("--toolsets") + 1] == "nico"
    assert token not in " ".join(chat_call)
    assert not config_path.exists()


@pytest.mark.asyncio
async def test_hermes_rejects_non_nico_toolset_before_creating_run_home(
    tmp_path: Path,
) -> None:
    provider, _ = _provider(tmp_path)
    request = _request(
        tool_session=RuntimeToolSession(
            socket_path="/tmp/rogue.sock",
            token="x" * 32,
            server_command=(sys.executable, "-m", "rogue.server"),
            server_name="rogue",
        )
    )

    with pytest.raises(RuntimeExecutionFailed) as error:
        await provider.create_session(request)

    assert error.value.details["runtime_code"] == "HERMES_TOOLSET_DENIED"
    assert not (tmp_path / "hermes-state" / str(request.tenant_id)).exists()


@pytest.mark.asyncio
async def test_hermes_resume_uses_persisted_session_and_continues_sequence(tmp_path: Path) -> None:
    provider, capture = _provider(tmp_path)
    request = _request(resume_session_id="existing-session", event_sequence=5)
    handle = await provider.create_session(request)
    events_task = asyncio.create_task(_events(provider, handle.external_session_id))

    result = await provider.run(handle.external_session_id, request)
    events = await events_task
    calls = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]
    call = calls[1]

    assert result.status is RuntimeSessionStatus.COMPLETED
    assert result.external_session_id == "existing-session"
    assert events[0].sequence == 6
    assert any(event.type is RuntimeEventType.RUN_RESUMED for event in events)
    assert call[call.index("--resume") + 1] == "existing-session"


@pytest.mark.asyncio
async def test_hermes_process_group_cancel_and_pause_capability(tmp_path: Path) -> None:
    provider, _ = _provider(tmp_path)
    request = _request(mode="cancel")
    handle = await provider.create_session(request)
    session = provider._sessions[handle.external_session_id]
    assert session.hermes_home is not None
    config_path = session.hermes_home / "config.yaml"
    run_task = asyncio.create_task(provider.run(handle.external_session_id, request))
    await asyncio.sleep(0.1)

    cancelled = await provider.cancel(handle.external_session_id)
    result = await asyncio.wait_for(run_task, timeout=2)

    assert cancelled.status is RuntimeSessionStatus.CANCELLED
    assert result.status is RuntimeSessionStatus.CANCELLED
    assert not config_path.exists()
    assert RuntimeCapability.PAUSE not in provider.descriptor.capabilities
    with pytest.raises(RuntimeCapabilityUnsupported):
        await provider.pause(handle.external_session_id)


@pytest.mark.asyncio
async def test_hermes_failure_is_stable_and_redacts_cli_secrets(tmp_path: Path) -> None:
    provider, _ = _provider(tmp_path)
    request = _request(mode="fail")
    handle = await provider.create_session(request)

    result = await provider.run(handle.external_session_id, request)

    assert result.status is RuntimeSessionStatus.FAILED
    assert result.error is not None
    assert result.error["code"] == "HERMES_CLI_FAILED"
    assert "super-secret-value" not in result.error["message"]
    assert "[REDACTED]" in result.error["message"]


@pytest.mark.asyncio
async def test_missing_hermes_executable_returns_stable_error() -> None:
    provider = HermesRuntimeProvider(("/definitely/missing/hermes",))
    request = _request()

    with pytest.raises(RuntimeExecutionFailed) as captured:
        await provider.create_session(request)

    assert captured.value.details["runtime_code"] == "HERMES_NOT_INSTALLED"


@pytest.mark.asyncio
async def test_incompatible_hermes_version_fails_closed(tmp_path: Path) -> None:
    script = tmp_path / "fake_hermes.py"
    script.write_text(_FAKE_HERMES, encoding="utf-8")
    provider = HermesRuntimeProvider(
        (sys.executable, str(script)), environment={"FAKE_HERMES_VERSION": "0.19.0"}
    )

    with pytest.raises(RuntimeExecutionFailed) as captured:
        await provider.create_session(_request())

    assert captured.value.details["runtime_code"] == "HERMES_VERSION_UNSUPPORTED"


async def _events(provider: HermesRuntimeProvider, external_id: str):
    return [event async for event in provider.stream_events(external_id)]


def test_local_hermes_reference_matches_pinned_cli_boundary() -> None:
    default_reference = Path(__file__).resolve().parents[4] / "hermes-agent"
    reference = Path(os.getenv("HERMES_REFERENCE_DIR", default_reference))
    if not reference.is_dir():
        pytest.skip("local Hermes reference checkout is unavailable")

    package = (reference / "hermes_cli" / "__init__.py").read_text(encoding="utf-8")
    parser = (reference / "hermes_cli" / "_parser.py").read_text(encoding="utf-8")
    main = (reference / "hermes_cli" / "main.py").read_text(encoding="utf-8")
    cli = (reference / "cli.py").read_text(encoding="utf-8")
    version = re.search(r'__version__\s*=\s*["\']([^"\']+)', package)

    assert version is not None and version.group(1) == "0.18.2"
    for flag in ("--query", "--quiet", "--model", "--provider", "--toolsets", "--resume"):
        assert flag in parser
    assert 'print(f"\\nsession_id: {cli.session_id}", file=sys.stderr)' in cli
    assert '"--session-id"' in main
    assert 'choices=["jsonl", "md", "qmd", "html", "trace"]' in main
