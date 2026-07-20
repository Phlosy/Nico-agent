from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError

from nico_agent.runtime.contracts import (
    AgentRuntimeProvider,
    RuntimeEventType,
    RuntimeSessionRequest,
    RuntimeSessionStatus,
)
from nico_agent.runtime.errors import RuntimeSessionNotFound
from nico_agent.runtime.mock import MockRuntimeProvider


def request(*, behavior: dict | None = None) -> RuntimeSessionRequest:
    return RuntimeSessionRequest(
        tenant_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        task_title="Contract acceptance",
        task_input={"value": 42},
        acceptance={"result": "ok"},
        role="tester",
        mandate="Execute deterministically",
        boundaries=["No external writes"],
        run_config={"mock": behavior or {}},
    )


@pytest.mark.asyncio
async def test_mock_satisfies_provider_protocol_and_completes() -> None:
    provider = MockRuntimeProvider()
    runtime_request = request(behavior={"output": {"result": "ok"}})

    assert isinstance(provider, AgentRuntimeProvider)
    handle = await provider.create_session(runtime_request)
    result = await provider.run(handle.external_session_id, runtime_request)

    assert handle.status is RuntimeSessionStatus.CREATED
    assert result.status is RuntimeSessionStatus.COMPLETED
    assert result.output == {"result": "ok"}
    assert result.checkpoint == {"completed_steps": 2}


@pytest.mark.asyncio
async def test_event_stream_is_ordered_and_terminal() -> None:
    provider = MockRuntimeProvider()
    runtime_request = request()
    handle = await provider.create_session(runtime_request)
    run_task = asyncio.create_task(provider.run(handle.external_session_id, runtime_request))

    events = [event async for event in provider.stream_events(handle.external_session_id)]
    result = await run_task

    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[0].type is RuntimeEventType.SESSION_CREATED
    assert events[-1].type is RuntimeEventType.RUN_COMPLETED
    assert result.status is RuntimeSessionStatus.COMPLETED


@pytest.mark.asyncio
async def test_event_stream_can_resume_after_sequence() -> None:
    provider = MockRuntimeProvider()
    runtime_request = request()
    handle = await provider.create_session(runtime_request)
    await provider.run(handle.external_session_id, runtime_request)

    events = [
        event
        async for event in provider.stream_events(handle.external_session_id, after_sequence=2)
    ]

    assert events and events[0].sequence == 3


@pytest.mark.asyncio
async def test_mock_failure_is_a_result_with_trajectory() -> None:
    provider = MockRuntimeProvider()
    runtime_request = request(behavior={"fail": True, "error_code": "EXPECTED", "steps": ["only"]})
    handle = await provider.create_session(runtime_request)

    result = await provider.run(handle.external_session_id, runtime_request)
    trajectory = await provider.export_trajectory(handle.external_session_id)

    assert result.status is RuntimeSessionStatus.FAILED
    assert result.error == {"code": "EXPECTED", "message": "mock runtime failed"}
    assert trajectory.status is RuntimeSessionStatus.FAILED
    assert trajectory.events[-1].type is RuntimeEventType.RUN_FAILED


@pytest.mark.asyncio
async def test_running_mock_can_pause_and_resume() -> None:
    provider = MockRuntimeProvider()
    runtime_request = request(behavior={"delay_seconds": 0.05, "steps": ["one", "two"]})
    handle = await provider.create_session(runtime_request)
    run_task = asyncio.create_task(provider.run(handle.external_session_id, runtime_request))
    await asyncio.sleep(0.01)

    paused = await provider.pause(handle.external_session_id)
    assert paused.status is RuntimeSessionStatus.PAUSED
    await asyncio.sleep(0.06)
    assert not run_task.done()

    resumed = await provider.resume(handle.external_session_id)
    assert resumed.status is RuntimeSessionStatus.RUNNING
    assert (await run_task).status is RuntimeSessionStatus.COMPLETED


@pytest.mark.asyncio
async def test_running_mock_can_cancel() -> None:
    provider = MockRuntimeProvider()
    runtime_request = request(behavior={"delay_seconds": 1, "steps": ["slow"]})
    handle = await provider.create_session(runtime_request)
    run_task = asyncio.create_task(provider.run(handle.external_session_id, runtime_request))
    await asyncio.sleep(0.01)

    await provider.cancel(handle.external_session_id)
    result = await asyncio.wait_for(run_task, timeout=0.2)

    assert result.status is RuntimeSessionStatus.CANCELLED
    assert (await provider.get_status(handle.external_session_id)).status is (
        RuntimeSessionStatus.CANCELLED
    )


@pytest.mark.asyncio
async def test_cancel_before_run_is_terminal() -> None:
    provider = MockRuntimeProvider()
    runtime_request = request()
    handle = await provider.create_session(runtime_request)
    await provider.cancel(handle.external_session_id)

    result = await provider.run(handle.external_session_id, runtime_request)

    assert result.status is RuntimeSessionStatus.CANCELLED


@pytest.mark.asyncio
async def test_session_request_is_immutable() -> None:
    runtime_request = request()

    with pytest.raises(ValidationError):
        runtime_request.task_title = "changed"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_unknown_runtime_session_is_stable_error() -> None:
    provider = MockRuntimeProvider()

    with pytest.raises(RuntimeSessionNotFound) as captured:
        await provider.get_status("missing")

    assert captured.value.code == "RUNTIME_SESSION_NOT_FOUND"
