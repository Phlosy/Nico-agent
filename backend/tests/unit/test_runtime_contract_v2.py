from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from nico_agent.runtime.contracts import (
    ContextSeed,
    RuntimeDisposition,
    RuntimeExecutionMode,
    RuntimeOutcome,
    RuntimeServices,
    RuntimeSessionRequest,
    RuntimeSessionStatus,
    execute_provider,
)
from nico_agent.runtime.hermes import HermesRuntimeProvider
from nico_agent.runtime.mock import MockRuntimeProvider
from nico_agent.runtime.service import (
    LEGACY_PROVIDER_RESOLVER_REMOVAL_VERSION,
    RuntimeExecutionService,
    resolve_runtime_provider,
    resolve_runtime_provider_name,
)


def _request() -> RuntimeSessionRequest:
    return RuntimeSessionRequest(
        tenant_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        task_title="Native contract",
        task_input={"question": "hello"},
        acceptance={"type": "object"},
        role="assistant",
        mandate="Answer safely",
        execution_mode=RuntimeExecutionMode.DIRECT,
        context_seed=ContextSeed(
            source_refs=("task:input",),
            untrusted_context=({"source": "task:input", "content": "hello"},),
        ),
    )


def test_v2_request_freezes_native_execution_inputs() -> None:
    request = _request()

    assert request.execution_mode is RuntimeExecutionMode.DIRECT
    assert request.context_seed is not None
    assert request.context_seed.source_refs == ("task:input",)
    assert request.execution_manifest == {}
    assert request.model_endpoint_snapshot is None


def test_runtime_outcome_distinguishes_terminal_and_suspended() -> None:
    terminal = RuntimeOutcome.terminal(
        status=RuntimeSessionStatus.COMPLETED,
        output={"answer": "ok"},
    )
    suspended = RuntimeOutcome.suspended(
        checkpoint={"schema_version": 1, "cursor": "child-results"},
        wake_condition={"type": "child_runs_terminal", "run_ids": [str(uuid4())]},
    )

    assert terminal.disposition is RuntimeDisposition.TERMINAL
    assert terminal.to_result().status is RuntimeSessionStatus.COMPLETED
    assert suspended.disposition is RuntimeDisposition.SUSPENDED
    assert suspended.status is RuntimeSessionStatus.SUSPENDED
    with pytest.raises(ValueError, match="terminal"):
        suspended.to_result()


@pytest.mark.asyncio
async def test_v1_provider_is_wrapped_as_terminal_v2_outcome() -> None:
    provider = MockRuntimeProvider()
    request = _request().model_copy(update={"run_config": {"mock": {"output": {"ok": True}}}})
    handle = await provider.create_session(request)

    outcome = await execute_provider(
        provider,
        handle.external_session_id,
        request,
        RuntimeServices(),
    )

    assert outcome.disposition is RuntimeDisposition.TERMINAL
    assert outcome.status is RuntimeSessionStatus.COMPLETED
    assert outcome.output == {"ok": True}


@pytest.mark.parametrize(
    ("runtime_provider", "run_config", "model_config", "expected"),
    [
        ("nico_native", {}, {}, "nico_native"),
        (None, {"runtime_provider": "hermes"}, {}, "hermes"),
        (None, {}, {"runtime_provider": "hermes"}, "hermes"),
        (None, {}, {}, "mock"),
    ],
)
def test_runtime_provider_resolution_preserves_legacy_rows(
    runtime_provider: str | None,
    run_config: dict[str, str],
    model_config: dict[str, str],
    expected: str,
) -> None:
    assert (
        resolve_runtime_provider_name(
            runtime_provider=runtime_provider,
            run_config=run_config,
            model_config=model_config,
        )
        == expected
    )


def test_legacy_provider_resolution_is_explicit_and_deprecated() -> None:
    explicit = resolve_runtime_provider(runtime_provider="hermes", run_config={}, model_config={})
    legacy = resolve_runtime_provider(
        runtime_provider=None,
        run_config={"runtime_provider": "hermes"},
        model_config={},
    )
    default = resolve_runtime_provider(runtime_provider=None, run_config={}, model_config={})

    assert explicit.telemetry == {
        "provider": "hermes",
        "source": "agent_version",
        "legacy": False,
        "removal_version": None,
    }
    assert legacy.source == "legacy_run_config" and legacy.legacy is True
    assert legacy.telemetry["removal_version"] == LEGACY_PROVIDER_RESOLVER_REMOVAL_VERSION
    assert default.name == "mock" and default.source == "legacy_default_mock"


def test_persisted_hermes_v1_session_is_compatible_without_rewriting_history() -> None:
    persisted = SimpleNamespace(
        provider_name="hermes",
        provider_version="0.18.2",
        protocol_version="1.0",
    )
    unsupported = SimpleNamespace(
        provider_name="hermes",
        provider_version="0.17.0",
        protocol_version="1.0",
    )

    assert RuntimeExecutionService._descriptor_can_resume(
        persisted, HermesRuntimeProvider.descriptor
    )
    assert not RuntimeExecutionService._descriptor_can_resume(
        unsupported, HermesRuntimeProvider.descriptor
    )
    assert persisted.protocol_version == "1.0"
