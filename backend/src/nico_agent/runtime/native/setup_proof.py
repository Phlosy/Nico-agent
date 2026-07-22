"""Deterministic Search-to-Fetch verification for guided setup Runs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from nico_agent.net.safe_http import SafeHttpError, canonicalize_http_url
from nico_agent.runtime.contracts import (
    RuntimeEventType,
    RuntimeOutcome,
    RuntimeServices,
    RuntimeSessionRequest,
    RuntimeSessionStatus,
    RuntimeToolHandler,
    RuntimeToolIntent,
    RuntimeToolOutcome,
)
from nico_agent.runtime.native.checkpoint import (
    SetupProofCheckpoint,
    load_setup_proof_checkpoint,
    make_setup_proof_checkpoint,
)
from nico_agent.tools.errors import ToolApprovalRequired

Emit = Callable[[RuntimeEventType, str | None, dict[str, Any]], Awaitable[None]]

_REQUIRED_TOOL_VERSIONS = {"web.search": "1.0.0", "web.fetch": "1.1.0"}
_REQUIRED_TOOL_REFS = frozenset(
    f"{name}@{version}" for name, version in _REQUIRED_TOOL_VERSIONS.items()
)
_SEARCH_CALL_ID = "setup-proof-search"
_FETCH_CALL_ID = "setup-proof-fetch"
_SEARCH_QUERY = "current Nico Agent information"


async def execute_setup_proof(
    request: RuntimeSessionRequest,
    *,
    services: RuntimeServices,
    emit: Emit,
    cancelled: Callable[[], bool],
) -> RuntimeOutcome:
    """Execute a bounded platform-owned proof without consulting the model."""

    try:
        checkpoint = load_setup_proof_checkpoint(
            request.checkpoint,
            manifest=request.execution_manifest,
        )
    except ValueError:
        return await _failed(
            emit,
            "CHECKPOINT_INVALID",
            "the setup verification checkpoint is corrupt or incompatible",
        )
    if checkpoint.loop_state in {"completed", "failed", "cancelled"}:
        return await _failed(
            emit,
            "CHECKPOINT_TERMINAL",
            "a terminal setup verification checkpoint cannot be resumed",
            checkpoint=checkpoint,
        )
    if set(_strings(request.budgets.get("tool_allow"))) != _REQUIRED_TOOL_REFS:
        return await _failed(
            emit,
            "SETUP_PROOF_SCOPE_INVALID",
            "setup verification requires exactly Search and Fetch",
            checkpoint=checkpoint,
        )
    handler = services.tool_handler
    if handler is None:
        return await _failed(
            emit,
            "TOOL_HANDLER_REQUIRED",
            "setup verification requires the Tool Gateway",
            checkpoint=checkpoint,
        )
    try:
        authorized = await handler.list_tools()
    except Exception:
        return await _failed(
            emit,
            "SETUP_PROOF_TOOLS_UNAVAILABLE",
            "setup verification could not read authorized Tools",
            checkpoint=checkpoint,
        )
    if {tool.reference for tool in authorized} != _REQUIRED_TOOL_REFS:
        return await _failed(
            emit,
            "SETUP_PROOF_TOOLS_UNAVAILABLE",
            "setup verification requires exact Search and Fetch Tool versions",
            checkpoint=checkpoint,
        )

    while True:
        if cancelled():
            cancelled_checkpoint = _next_checkpoint(
                request,
                checkpoint,
                loop_state="cancelled",
            )
            await emit(
                RuntimeEventType.CHECKPOINT_SAVED,
                None,
                cancelled_checkpoint.model_dump(mode="json"),
            )
            error = {"code": "CANCELLED", "message": "runtime execution was cancelled"}
            await emit(RuntimeEventType.RUN_CANCELLED, None, {"error": error})
            return RuntimeOutcome.terminal(
                status=RuntimeSessionStatus.CANCELLED,
                error=error,
                checkpoint=cancelled_checkpoint.model_dump(mode="json"),
                usage=cancelled_checkpoint.usage,
            )

        if checkpoint.loop_state == "searching":
            result = await _execute_tool(
                handler,
                emit=emit,
                checkpoint=checkpoint,
                call_id=_SEARCH_CALL_ID,
                name="web.search",
                version=_REQUIRED_TOOL_VERSIONS["web.search"],
                arguments={"query": _SEARCH_QUERY, "count": 5},
                idempotency_key=f"setup-proof:{request.run_id}:search",
                step_number=1,
            )
            if isinstance(result, RuntimeOutcome):
                return result
            if result.status != "succeeded":
                return await _failed(
                    emit,
                    "SETUP_PROOF_SEARCH_FAILED",
                    "setup verification Search failed",
                    checkpoint=checkpoint,
                )
            try:
                source_url, search_tool_call_id = _search_source(result)
            except ValueError:
                return await _failed(
                    emit,
                    "SETUP_PROOF_SEARCH_EMPTY",
                    "setup verification Search returned no fetchable result",
                    checkpoint=checkpoint,
                )
            checkpoint = make_setup_proof_checkpoint(
                manifest=request.execution_manifest,
                loop_state="fetching",
                search_tool_call_id=search_tool_call_id,
                source_url=source_url,
                completed_action_keys=(*checkpoint.completed_action_keys, _SEARCH_CALL_ID),
                tool_calls_consumed=1,
                usage={"model_calls": 0, "tool_calls": 1},
            )
            await _checkpoint_and_complete_step(emit, checkpoint, step_number=1, stage="search")
            continue

        if checkpoint.loop_state == "fetching":
            if checkpoint.source_url is None or checkpoint.search_tool_call_id is None:
                return await _failed(
                    emit,
                    "CHECKPOINT_INVALID",
                    "setup verification Fetch checkpoint is incomplete",
                    checkpoint=checkpoint,
                )
            result = await _execute_tool(
                handler,
                emit=emit,
                checkpoint=checkpoint,
                call_id=_FETCH_CALL_ID,
                name="web.fetch",
                version=_REQUIRED_TOOL_VERSIONS["web.fetch"],
                arguments={
                    "url": checkpoint.source_url,
                    "search_tool_call_id": checkpoint.search_tool_call_id,
                },
                idempotency_key=f"setup-proof:{request.run_id}:fetch",
                step_number=2,
            )
            if isinstance(result, RuntimeOutcome):
                return result
            if result.status != "succeeded":
                return await _failed(
                    emit,
                    "SETUP_PROOF_FETCH_FAILED",
                    "setup verification Fetch failed",
                    checkpoint=checkpoint,
                )
            final_url = _fetched_url(result)
            if final_url is None:
                return await _failed(
                    emit,
                    "SETUP_PROOF_FETCH_INVALID",
                    "setup verification Fetch returned no valid final URL",
                    checkpoint=checkpoint,
                )
            checkpoint = make_setup_proof_checkpoint(
                manifest=request.execution_manifest,
                loop_state="finalizing",
                search_tool_call_id=checkpoint.search_tool_call_id,
                source_url=checkpoint.source_url,
                final_url=final_url,
                completed_action_keys=(*checkpoint.completed_action_keys, _FETCH_CALL_ID),
                tool_calls_consumed=2,
                usage={"model_calls": 0, "tool_calls": 2},
            )
            await _checkpoint_and_complete_step(emit, checkpoint, step_number=2, stage="fetch")
            continue

        if checkpoint.loop_state == "finalizing":
            if checkpoint.final_url is None:
                return await _failed(
                    emit,
                    "CHECKPOINT_INVALID",
                    "setup verification final checkpoint is incomplete",
                    checkpoint=checkpoint,
                )
            await _step_started(emit, step_number=3, stage="final")
            output = {
                "content": f"Online verification succeeded. Source: {checkpoint.final_url}",
                "verification": {
                    "search": "completed",
                    "fetch": "completed",
                    "source_url": checkpoint.final_url,
                },
            }
            completed = _next_checkpoint(request, checkpoint, loop_state="completed")
            await emit(
                RuntimeEventType.CHECKPOINT_SAVED,
                None,
                completed.model_dump(mode="json"),
            )
            await emit(
                RuntimeEventType.STEP_COMPLETED,
                None,
                _step_payload(step_number=3, stage="final"),
            )
            await emit(RuntimeEventType.RUN_COMPLETED, None, {"output": output})
            return RuntimeOutcome.terminal(
                status=RuntimeSessionStatus.COMPLETED,
                output=output,
                usage=completed.usage,
                checkpoint=completed.model_dump(mode="json"),
            )

        return await _failed(
            emit,
            "CHECKPOINT_INVALID",
            "setup verification checkpoint state is invalid",
            checkpoint=checkpoint,
        )


async def _execute_tool(
    handler: RuntimeToolHandler,
    *,
    emit: Emit,
    checkpoint: SetupProofCheckpoint,
    call_id: str,
    name: str,
    version: str,
    arguments: dict[str, Any],
    idempotency_key: str,
    step_number: int,
) -> RuntimeToolOutcome | RuntimeOutcome:
    stage = name.rsplit(".", 1)[-1]
    await _step_started(emit, step_number=step_number, stage=stage)
    await emit(
        RuntimeEventType.TOOL_CALL_STARTED,
        None,
        {
            "call_id": call_id,
            "tool": f"{name}@{version}",
            "idempotency_key": idempotency_key,
            "iteration": step_number,
        },
    )
    try:
        outcome = await handler.execute_tool(
            RuntimeToolIntent(
                call_id=call_id,
                name=name,
                version=version,
                arguments=arguments,
                idempotency_key=idempotency_key,
                checkpoint=checkpoint.model_dump(mode="json"),
            )
        )
    except ToolApprovalRequired as exc:
        return RuntimeOutcome.suspended(
            checkpoint=checkpoint.model_dump(mode="json"),
            wake_condition={"type": "tool_approval", **exc.details},
            usage=checkpoint.usage,
        )
    except Exception:
        return await _failed(
            emit,
            "SETUP_PROOF_TOOL_ERROR",
            "the Tool Gateway rejected setup verification",
            checkpoint=checkpoint,
        )
    await emit(
        RuntimeEventType.TOOL_CALL_COMPLETED,
        None,
        {
            "call_id": outcome.call_id,
            "tool_call_id": outcome.tool_call_id,
            "run_step_id": outcome.run_step_id,
            "status": outcome.status,
            "cached": outcome.cached,
            "iteration": step_number,
        },
    )
    return outcome


async def _checkpoint_and_complete_step(
    emit: Emit,
    checkpoint: SetupProofCheckpoint,
    *,
    step_number: int,
    stage: str,
) -> None:
    await emit(
        RuntimeEventType.CHECKPOINT_SAVED,
        None,
        checkpoint.model_dump(mode="json"),
    )
    await emit(
        RuntimeEventType.STEP_COMPLETED,
        None,
        _step_payload(step_number=step_number, stage=stage),
    )


async def _step_started(emit: Emit, *, step_number: int, stage: str) -> None:
    await emit(
        RuntimeEventType.STEP_STARTED,
        None,
        _step_payload(step_number=step_number, stage=stage),
    )


def _step_payload(*, step_number: int, stage: str) -> dict[str, Any]:
    return {
        "step_key": f"setup-proof:{stage}",
        "step_type": "aggregation" if stage == "final" else "tool",
        "iteration": step_number,
        "name": f"setup.verification.{stage}",
    }


def _search_source(outcome: RuntimeToolOutcome) -> tuple[str, str]:
    if outcome.tool_call_id is None or not isinstance(outcome.output, dict):
        raise ValueError("Search result is missing provenance")
    try:
        tool_call_id = str(UUID(outcome.tool_call_id))
    except ValueError as exc:
        raise ValueError("Search result provenance is invalid") from exc
    results = outcome.output.get("results")
    if not isinstance(results, list):
        raise ValueError("Search result list is invalid")
    for item in results:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        try:
            return canonicalize_http_url(item["url"]).url, tool_call_id
        except SafeHttpError:
            continue
    raise ValueError("Search returned no fetchable source")


def _fetched_url(outcome: RuntimeToolOutcome) -> str | None:
    if not isinstance(outcome.output, dict):
        return None
    value = outcome.output.get("final_url")
    if not isinstance(value, str):
        return None
    try:
        return canonicalize_http_url(value).url
    except SafeHttpError:
        return None


def _next_checkpoint(
    request: RuntimeSessionRequest,
    checkpoint: SetupProofCheckpoint,
    *,
    loop_state: str,
) -> SetupProofCheckpoint:
    return make_setup_proof_checkpoint(
        manifest=request.execution_manifest,
        loop_state=loop_state,
        search_tool_call_id=checkpoint.search_tool_call_id,
        source_url=checkpoint.source_url,
        final_url=checkpoint.final_url,
        completed_action_keys=checkpoint.completed_action_keys,
        tool_calls_consumed=checkpoint.tool_calls_consumed,
        usage=checkpoint.usage,
    )


async def _failed(
    emit: Emit,
    code: str,
    message: str,
    *,
    checkpoint: SetupProofCheckpoint | None = None,
) -> RuntimeOutcome:
    checkpoint_payload = checkpoint.model_dump(mode="json") if checkpoint is not None else None
    usage = checkpoint.usage if checkpoint is not None else {}
    error = {"code": code, "message": message}
    await emit(RuntimeEventType.RUN_FAILED, None, {"error": error})
    return RuntimeOutcome.terminal(
        status=RuntimeSessionStatus.FAILED,
        error=error,
        usage=usage,
        checkpoint=checkpoint_payload,
    )


def _strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []
