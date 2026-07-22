"""Nico native direct and recoverable ReAct execution loops."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from nico_agent.artifacts.contracts import RuntimeArtifactIntent
from nico_agent.coordination.contracts import DelegationIntent, RuntimeCoordinationIntent
from nico_agent.models.contracts import (
    ModelRequest,
    ModelResponse,
    ModelStreamEventType,
    ModelToolCall,
    ModelToolDefinition,
    ModelUsage,
)
from nico_agent.models.errors import ModelError, ModelProtocolError
from nico_agent.models.gateway import ModelGateway
from nico_agent.runtime.contracts import (
    RuntimeEventType,
    RuntimeExecutionMode,
    RuntimeInterventionHandler,
    RuntimeOutcome,
    RuntimeServices,
    RuntimeSessionRequest,
    RuntimeSessionStatus,
    RuntimeToolIntent,
    RuntimeToolOutcome,
    RuntimeToolSpec,
)
from nico_agent.runtime.native.checkpoint import (
    PlanCheckpoint,
    ReactCheckpoint,
    direct_checkpoint,
    load_plan_checkpoint,
    load_react_checkpoint,
    make_plan_checkpoint,
    make_react_checkpoint,
)
from nico_agent.runtime.native.completion import (
    completion_judge_response_format,
    evaluate_completion,
    parse_completion_judge,
)
from nico_agent.runtime.native.context import (
    build_citation_repair_context,
    build_direct_context,
    build_native_context,
    build_phase_context,
    inject_interventions,
    merge_observed_web_urls,
    output_has_observed_web_citation,
)
from nico_agent.runtime.native.planner import (
    PlanDraft,
    ordered_steps,
    parse_plan,
    plan_content_hash,
    planner_response_format,
)
from nico_agent.runtime.native.reflection import (
    ReflectionDecision,
    parse_reflection,
    reflection_response_format,
)
from nico_agent.tools.errors import ToolApprovalRequired

Emit = Callable[[RuntimeEventType, str | None, dict[str, Any]], Awaitable[None]]
_intervention_handler: ContextVar[RuntimeInterventionHandler | None] = ContextVar(
    "native_intervention_handler", default=None
)


@dataclass(frozen=True, slots=True)
class _ModelRound:
    response: ModelResponse
    usage_payload: dict[str, Any]
    context_hash: str
    context_version: int
    call_key: str


class NativeAgentLoop:
    def __init__(self, gateway: ModelGateway, *, post_tool_delay_seconds: float = 0) -> None:
        self.gateway = gateway
        self.post_tool_delay_seconds = post_tool_delay_seconds

    async def execute(
        self,
        request: RuntimeSessionRequest,
        *,
        services: RuntimeServices,
        emit: Emit,
        cancelled: Callable[[], bool],
    ) -> RuntimeOutcome:
        handler = (
            services.intervention_handler
            if request.execution_mode
            in {RuntimeExecutionMode.REACT, RuntimeExecutionMode.PLAN_AND_EXECUTE}
            else None
        )
        token = _intervention_handler.set(handler)
        try:
            if request.execution_mode is RuntimeExecutionMode.DIRECT:
                return await self._execute_direct(request, emit=emit, cancelled=cancelled)
            if request.execution_mode is RuntimeExecutionMode.REACT:
                return await self._execute_react(
                    request,
                    services=services,
                    emit=emit,
                    cancelled=cancelled,
                )
            if request.execution_mode is RuntimeExecutionMode.PLAN_AND_EXECUTE:
                return await self._execute_plan_and_execute(
                    request,
                    services=services,
                    emit=emit,
                    cancelled=cancelled,
                )
            return await self._fail(
                emit,
                "EXECUTION_MODE_NOT_IMPLEMENTED",
                "this runtime build does not yet support the selected execution mode",
            )
        finally:
            _intervention_handler.reset(token)

    async def _execute_plan_and_execute(
        self,
        request: RuntimeSessionRequest,
        *,
        services: RuntimeServices,
        emit: Emit,
        cancelled: Callable[[], bool],
    ) -> RuntimeOutcome:
        endpoint, model, failure = await self._requirements(request, emit)
        if failure is not None:
            return failure
        assert endpoint is not None and model is not None
        if cancelled():
            return await self._cancelled(emit)
        try:
            checkpoint = load_plan_checkpoint(
                request.checkpoint,
                manifest=request.execution_manifest,
            )
        except ValueError:
            return await self._fail(
                emit,
                "CHECKPOINT_INVALID",
                "the native plan checkpoint is corrupt or incompatible",
            )
        if checkpoint.loop_state in {"completed", "failed", "cancelled"}:
            return await self._fail(
                emit,
                "CHECKPOINT_TERMINAL",
                "a terminal native plan checkpoint cannot be resumed",
            )

        checkpoint, recovered_plan = _reconcile_plan_recovery(request, checkpoint)

        max_plan_steps = _budget(request, "max_plan_steps", 16, minimum=1, maximum=64)
        max_reflections = _budget(request, "max_reflections", 3, minimum=0, maximum=32)
        max_replans = _budget(request, "max_replans", 2, minimum=0, maximum=16)
        token_limit = _budget(request, "token_limit", 0, minimum=0, maximum=10**9)
        max_step_iterations = _budget(request, "max_step_iterations", 4, minimum=1, maximum=32)
        max_tool_calls = _budget(request, "max_tool_calls", 32, minimum=0, maximum=1024)
        tools: tuple[RuntimeToolSpec, ...] = ()
        if services.tool_handler is not None:
            tools = await services.tool_handler.list_tools()
        try:
            tool_versions = _exact_tool_versions(tools)
        except ValueError as exc:
            return await self._fail(emit, "TOOL_VERSION_AMBIGUOUS", str(exc))
        definitions = tuple(
            ModelToolDefinition(
                name=spec.name,
                description=f"{spec.description} Exact platform version: {spec.version}.",
                input_schema=spec.input_schema,
            )
            for spec in tools
        )
        plan = recovered_plan or _restore_plan(request, checkpoint)
        recovery_instruction: str | None = checkpoint.recovery_instruction
        replan_count = max(0, checkpoint.plan_revision - 1)

        if (
            checkpoint.loop_state == "finalizing"
            and checkpoint.citation_repair_attempted
            and checkpoint.citation_provisional_output is not None
        ):
            return await self._finish_plan_citation_repair(
                request,
                checkpoint,
                endpoint=endpoint,
                model=model,
                token_limit=token_limit,
                emit=emit,
                cancelled=cancelled,
            )

        if checkpoint.loop_state == "reflecting":
            if checkpoint.recovery_decision == "fail":
                return await self._fail(
                    emit,
                    "REFLECTION_REJECTED_RECOVERY",
                    recovery_instruction or "Reflection rejected recovery",
                    checkpoint=checkpoint.model_dump(mode="json"),
                    usage=checkpoint.usage,
                )
            if checkpoint.recovery_decision == "replan":
                await emit(
                    RuntimeEventType.PLAN_STATUS_CHANGED,
                    None,
                    {"revision": checkpoint.plan_revision, "status": "superseded"},
                )
                checkpoint = make_plan_checkpoint(
                    manifest=request.execution_manifest,
                    loop_state="planning",
                    plan_revision=checkpoint.plan_revision,
                    recovery_instruction=recovery_instruction,
                    context_version=checkpoint.context_version,
                    reflection_count=checkpoint.reflection_count,
                    **_web_checkpoint_state(checkpoint),
                    usage=checkpoint.usage,
                )
                plan = None
            elif checkpoint.recovery_decision == "retry":
                checkpoint = make_plan_checkpoint(
                    manifest=request.execution_manifest,
                    loop_state="executing",
                    plan_revision=checkpoint.plan_revision,
                    plan_content_hash=checkpoint.plan_content_hash,
                    current_step_key=checkpoint.current_step_key,
                    completed_step_keys=checkpoint.completed_step_keys,
                    latest_output=checkpoint.latest_output,
                    recovery_instruction=recovery_instruction,
                    context_version=checkpoint.context_version,
                    reflection_count=checkpoint.reflection_count,
                    **_web_checkpoint_state(checkpoint),
                    usage=checkpoint.usage,
                )

        while True:
            if cancelled():
                return await self._cancelled(emit, checkpoint=checkpoint.model_dump(mode="json"))
            if token_limit and int(checkpoint.usage.get("total_tokens") or 0) >= token_limit:
                return await self._fail(
                    emit,
                    "TOKEN_BUDGET_EXHAUSTED",
                    "plan_and_execute exhausted its token budget",
                    checkpoint=checkpoint.model_dump(mode="json"),
                    usage=checkpoint.usage,
                )
            if plan is None:
                planned = await self._create_plan_revision(
                    request,
                    endpoint=endpoint,
                    model=model,
                    checkpoint=checkpoint,
                    max_plan_steps=max_plan_steps,
                    recovery_instruction=recovery_instruction,
                    emit=emit,
                )
                if isinstance(planned, RuntimeOutcome):
                    return planned
                plan, checkpoint = planned
                await emit(
                    RuntimeEventType.CHECKPOINT_SAVED,
                    None,
                    checkpoint.model_dump(mode="json"),
                )

            restart_with_new_plan = False
            for step in ordered_steps(plan):
                if step.key in checkpoint.completed_step_keys:
                    continue
                result = await self._execute_plan_step(
                    request,
                    endpoint=endpoint,
                    model=model,
                    plan=plan,
                    checkpoint=checkpoint,
                    step_key=step.key,
                    recovery_instruction=recovery_instruction,
                    services=services,
                    cancelled=cancelled,
                    tools=definitions,
                    tool_versions=tool_versions,
                    max_step_iterations=max_step_iterations,
                    max_tool_calls=max_tool_calls,
                    emit=emit,
                )
                if isinstance(result, RuntimeOutcome):
                    return result
                output, evaluation, checkpoint = result
                if evaluation.passed:
                    recovery_instruction = None
                    continue

                reflected = await self._reflect(
                    request,
                    endpoint=endpoint,
                    model=model,
                    checkpoint=checkpoint,
                    plan=plan,
                    failed_step_key=step.key,
                    failed_output=output,
                    failed_checks=[check.model_dump(mode="json") for check in evaluation.checks],
                    max_reflections=max_reflections,
                    emit=emit,
                )
                if isinstance(reflected, RuntimeOutcome):
                    return reflected
                decision, checkpoint = reflected
                recovery_instruction = decision.recovery_instruction or decision.reason
                if decision.decision == "fail":
                    await emit(
                        RuntimeEventType.PLAN_STATUS_CHANGED,
                        None,
                        {"revision": checkpoint.plan_revision, "status": "failed"},
                    )
                    return await self._fail(
                        emit,
                        "REFLECTION_REJECTED_RECOVERY",
                        decision.reason,
                        checkpoint=checkpoint.model_dump(mode="json"),
                        usage=checkpoint.usage,
                    )
                if decision.decision == "replan":
                    if replan_count >= max_replans:
                        return await self._fail(
                            emit,
                            "REPLAN_BUDGET_EXHAUSTED",
                            "plan_and_execute exhausted its replan budget",
                            checkpoint=checkpoint.model_dump(mode="json"),
                            usage=checkpoint.usage,
                        )
                    replan_count += 1
                    await emit(
                        RuntimeEventType.PLAN_STATUS_CHANGED,
                        None,
                        {"revision": checkpoint.plan_revision, "status": "superseded"},
                    )
                    checkpoint = make_plan_checkpoint(
                        manifest=request.execution_manifest,
                        loop_state="planning",
                        plan_revision=checkpoint.plan_revision,
                        recovery_instruction=recovery_instruction,
                        context_version=checkpoint.context_version,
                        reflection_count=checkpoint.reflection_count,
                        **_web_checkpoint_state(checkpoint),
                        usage=checkpoint.usage,
                    )
                    await emit(
                        RuntimeEventType.CHECKPOINT_SAVED,
                        None,
                        checkpoint.model_dump(mode="json"),
                    )
                    plan = None
                    restart_with_new_plan = True
                    break
                # Retry only the rejected step. The reflection count makes the call key unique.
                checkpoint = make_plan_checkpoint(
                    manifest=request.execution_manifest,
                    loop_state="executing",
                    plan_revision=checkpoint.plan_revision,
                    plan_content_hash=checkpoint.plan_content_hash,
                    current_step_key=step.key,
                    completed_step_keys=checkpoint.completed_step_keys,
                    latest_output=checkpoint.latest_output,
                    recovery_instruction=recovery_instruction,
                    context_version=checkpoint.context_version,
                    reflection_count=checkpoint.reflection_count,
                    **_web_checkpoint_state(checkpoint),
                    usage=checkpoint.usage,
                )
                restart_with_new_plan = True
                break
            if restart_with_new_plan:
                continue

            completion = evaluate_completion(
                checkpoint.latest_output or {},
                acceptance=request.acceptance,
                evidence_refs=tuple(
                    f"plan:{checkpoint.plan_revision}:{key}"
                    for key in checkpoint.completed_step_keys
                ),
            )
            await emit(
                RuntimeEventType.EVALUATION_COMPLETED,
                None,
                {
                    "evaluation_type": "completion",
                    "method": "deterministic",
                    "verdict": "passed" if completion.passed else "failed",
                    "plan_revision": checkpoint.plan_revision,
                    "input_hash": checkpoint.plan_content_hash,
                    "output_hash": completion.output_hash,
                    "evidence_refs": list(completion.evidence_refs),
                    "result": completion.model_dump(mode="json"),
                },
            )
            completion_passed = completion.passed
            completion_failure_checks = [
                check.model_dump(mode="json") for check in completion.checks
            ]
            if completion_passed and request.run_config.get("completion_model_judge") is True:
                judged = await self._judge_completion(
                    request,
                    endpoint=endpoint,
                    model=model,
                    checkpoint=checkpoint,
                    completion=completion.model_dump(mode="json"),
                    emit=emit,
                )
                if isinstance(judged, RuntimeOutcome):
                    return judged
                judge_passed, judge_reason, checkpoint = judged
                completion_passed = judge_passed
                completion_failure_checks = [
                    {
                        "code": "MODEL_JUDGE_REJECTED",
                        "passed": judge_passed,
                        "details": {"reason": judge_reason},
                    }
                ]
            if not completion_passed:
                final_step_key = checkpoint.completed_step_keys[-1]
                reflected = await self._reflect(
                    request,
                    endpoint=endpoint,
                    model=model,
                    checkpoint=checkpoint,
                    plan=plan,
                    failed_step_key=final_step_key,
                    failed_output=checkpoint.latest_output or {},
                    failed_checks=completion_failure_checks,
                    max_reflections=max_reflections,
                    emit=emit,
                )
                if isinstance(reflected, RuntimeOutcome):
                    return reflected
                decision, checkpoint = reflected
                recovery_instruction = decision.recovery_instruction or decision.reason
                if decision.decision == "fail":
                    await emit(
                        RuntimeEventType.PLAN_STATUS_CHANGED,
                        None,
                        {"revision": checkpoint.plan_revision, "status": "failed"},
                    )
                    return await self._fail(
                        emit,
                        "COMPLETION_REJECTED",
                        decision.reason,
                        checkpoint=checkpoint.model_dump(mode="json"),
                        usage=checkpoint.usage,
                    )
                # A completed Plan revision is not edited in place. Both retry and
                # replan therefore create a corrective revision.
                if replan_count >= max_replans:
                    return await self._fail(
                        emit,
                        "REPLAN_BUDGET_EXHAUSTED",
                        "completion correction exhausted its replan budget",
                        checkpoint=checkpoint.model_dump(mode="json"),
                        usage=checkpoint.usage,
                    )
                replan_count += 1
                await emit(
                    RuntimeEventType.PLAN_STATUS_CHANGED,
                    None,
                    {"revision": checkpoint.plan_revision, "status": "superseded"},
                )
                checkpoint = make_plan_checkpoint(
                    manifest=request.execution_manifest,
                    loop_state="planning",
                    plan_revision=checkpoint.plan_revision,
                    recovery_instruction=recovery_instruction,
                    context_version=checkpoint.context_version,
                    reflection_count=checkpoint.reflection_count,
                    **_web_checkpoint_state(checkpoint),
                    usage=checkpoint.usage,
                )
                await emit(
                    RuntimeEventType.CHECKPOINT_SAVED,
                    None,
                    checkpoint.model_dump(mode="json"),
                )
                plan = None
                continue

            output = {
                "result": checkpoint.latest_output or {},
                "execution_summary": {
                    "plan_revision": checkpoint.plan_revision,
                    "completed_steps": list(checkpoint.completed_step_keys),
                    "reflections": checkpoint.reflection_count,
                },
            }
            if checkpoint.observed_web_urls:
                citation_present = output_has_observed_web_citation(
                    output,
                    checkpoint.observed_web_urls,
                )
                can_repair = not citation_present and (
                    not token_limit or int(checkpoint.usage.get("total_tokens") or 0) < token_limit
                )
                await _emit_citation_evaluation(
                    emit,
                    passed=citation_present,
                    phase="initial",
                    observed_count=len(checkpoint.observed_web_urls),
                    repair_attempted=False,
                    reason=(
                        "present"
                        if citation_present
                        else ("repair_scheduled" if can_repair else "budget_exhausted")
                    ),
                )
                if can_repair:
                    checkpoint = make_plan_checkpoint(
                        manifest=request.execution_manifest,
                        loop_state="finalizing",
                        plan_revision=checkpoint.plan_revision,
                        plan_content_hash=checkpoint.plan_content_hash,
                        completed_step_keys=checkpoint.completed_step_keys,
                        latest_output=checkpoint.latest_output,
                        context_version=checkpoint.context_version,
                        reflection_count=checkpoint.reflection_count,
                        **_web_checkpoint_state(
                            checkpoint,
                            citation_repair_attempted=True,
                            citation_provisional_output=output,
                        ),
                        usage=checkpoint.usage,
                    )
                    await emit(
                        RuntimeEventType.CHECKPOINT_SAVED,
                        None,
                        checkpoint.model_dump(mode="json"),
                    )
                    return await self._finish_plan_citation_repair(
                        request,
                        checkpoint,
                        endpoint=endpoint,
                        model=model,
                        token_limit=token_limit,
                        emit=emit,
                        cancelled=cancelled,
                    )
                if not citation_present:
                    output = _with_citation_diagnostic(
                        output,
                        observed_count=len(checkpoint.observed_web_urls),
                        repair_attempted=False,
                        reason="budget_exhausted",
                    )
            checkpoint = make_plan_checkpoint(
                manifest=request.execution_manifest,
                loop_state="completed",
                plan_revision=checkpoint.plan_revision,
                plan_content_hash=checkpoint.plan_content_hash,
                completed_step_keys=checkpoint.completed_step_keys,
                latest_output=checkpoint.latest_output,
                context_version=checkpoint.context_version,
                reflection_count=checkpoint.reflection_count,
                **_web_checkpoint_state(checkpoint),
                usage=checkpoint.usage,
            )
            await emit(
                RuntimeEventType.PLAN_STATUS_CHANGED,
                None,
                {"revision": checkpoint.plan_revision, "status": "completed"},
            )
            await emit(
                RuntimeEventType.CHECKPOINT_SAVED,
                None,
                checkpoint.model_dump(mode="json"),
            )
            await emit(RuntimeEventType.RUN_COMPLETED, None, {"output": output})
            return RuntimeOutcome.terminal(
                status=RuntimeSessionStatus.COMPLETED,
                output=output,
                usage=checkpoint.usage,
                checkpoint=checkpoint.model_dump(mode="json"),
            )

    async def _judge_completion(
        self,
        request: RuntimeSessionRequest,
        *,
        endpoint: dict[str, Any],
        model: str,
        checkpoint: PlanCheckpoint,
        completion: dict[str, Any],
        emit: Emit,
    ) -> tuple[bool, str, PlanCheckpoint] | RuntimeOutcome:
        call_key = _next_named_call_key(
            f"completion-judge:{checkpoint.plan_revision}:{checkpoint.reflection_count + 1}",
            request.recovery_state,
        )
        context = build_phase_context(
            request,
            phase="completion_judge",
            instruction=(
                "Independently judge whether the result satisfies task acceptance. "
                "Return only a strict passed or failed decision."
            ),
            payload={
                "result": checkpoint.latest_output,
                "deterministic_evaluation": completion,
            },
            version=checkpoint.context_version + 1,
            source_refs=(f"plan:{checkpoint.plan_revision}",),
        )
        try:
            round_result = await self._model_round(
                request,
                endpoint=endpoint,
                model=model,
                context=context,
                call_key=call_key,
                tools=(),
                response_format=completion_judge_response_format(),
                context_reason="completion_judge",
                emit=emit,
            )
            decision = parse_completion_judge(round_result.response.text)
        except ModelError as exc:
            return await self._model_failure(
                emit, exc, checkpoint=checkpoint.model_dump(mode="json")
            )
        except ValueError:
            return await self._fail(
                emit,
                "COMPLETION_JUDGE_OUTPUT_INVALID",
                "completion judge returned an invalid decision",
                checkpoint=checkpoint.model_dump(mode="json"),
                usage=checkpoint.usage,
            )
        usage = _merge_usage(checkpoint.usage, round_result.usage_payload)
        next_checkpoint = make_plan_checkpoint(
            manifest=request.execution_manifest,
            loop_state="finalizing",
            plan_revision=checkpoint.plan_revision,
            plan_content_hash=checkpoint.plan_content_hash,
            completed_step_keys=checkpoint.completed_step_keys,
            latest_output=checkpoint.latest_output,
            context_version=context.version,
            reflection_count=checkpoint.reflection_count,
            **_web_checkpoint_state(checkpoint),
            usage=usage,
        )
        await emit(
            RuntimeEventType.EVALUATION_COMPLETED,
            None,
            {
                "evaluation_type": "completion",
                "method": "model",
                "verdict": decision.verdict,
                "plan_revision": checkpoint.plan_revision,
                "model_call_key": call_key,
                "input_hash": checkpoint.plan_content_hash,
                "output_hash": _hash_json(checkpoint.latest_output or {}),
                "evidence_refs": [f"model-call:{call_key}"],
                "result": decision.model_dump(mode="json"),
            },
        )
        await emit(
            RuntimeEventType.CHECKPOINT_SAVED,
            None,
            next_checkpoint.model_dump(mode="json"),
        )
        return decision.verdict == "passed", decision.reason, next_checkpoint

    async def _create_plan_revision(
        self,
        request: RuntimeSessionRequest,
        *,
        endpoint: dict[str, Any],
        model: str,
        checkpoint: PlanCheckpoint,
        max_plan_steps: int,
        recovery_instruction: str | None,
        emit: Emit,
    ) -> tuple[PlanDraft, PlanCheckpoint] | RuntimeOutcome:
        revision = checkpoint.plan_revision + 1
        context = build_phase_context(
            request,
            phase="planning",
            instruction=(
                "Return a bounded executable Plan as strict JSON. Dependencies must form a DAG."
            ),
            payload={
                "revision": revision,
                "recovery_instruction": recovery_instruction,
                "max_steps": max_plan_steps,
            },
            version=checkpoint.context_version + 1,
        )
        call_key = _next_named_call_key(f"planner:{revision}", request.recovery_state)
        try:
            round_result = await self._model_round(
                request,
                endpoint=endpoint,
                model=model,
                context=context,
                call_key=call_key,
                tools=(),
                response_format=planner_response_format(),
                context_reason="planning",
                emit=emit,
            )
            plan = parse_plan(round_result.response.text, max_steps=max_plan_steps)
        except ModelError as exc:
            return await self._model_failure(
                emit, exc, checkpoint=checkpoint.model_dump(mode="json")
            )
        except ValueError:
            return await self._fail(
                emit,
                "PLANNER_OUTPUT_INVALID",
                "planner returned an invalid or over-budget Plan",
                checkpoint=checkpoint.model_dump(mode="json"),
                usage=checkpoint.usage,
            )
        usage = _merge_usage(checkpoint.usage, round_result.usage_payload)
        content_hash = plan_content_hash(plan)
        await emit(
            RuntimeEventType.PLAN_CREATED,
            None,
            {
                "revision": revision,
                "reason": "initial" if revision == 1 else "replan",
                "supersedes_revision": revision - 1 if revision > 1 else None,
                "objective": plan.objective,
                "steps": [step.model_dump(mode="json") for step in plan.steps],
                "content_hash": content_hash,
                "model_call_key": call_key,
            },
        )
        next_checkpoint = make_plan_checkpoint(
            manifest=request.execution_manifest,
            loop_state="executing",
            plan_revision=revision,
            plan_content_hash=content_hash,
            context_version=context.version,
            reflection_count=checkpoint.reflection_count,
            **_web_checkpoint_state(checkpoint),
            usage=usage,
        )
        return plan, next_checkpoint

    async def _execute_plan_step(
        self,
        request: RuntimeSessionRequest,
        *,
        endpoint: dict[str, Any],
        model: str,
        plan: PlanDraft,
        checkpoint: PlanCheckpoint,
        step_key: str,
        recovery_instruction: str | None,
        services: RuntimeServices,
        cancelled: Callable[[], bool],
        tools: tuple[ModelToolDefinition, ...],
        tool_versions: dict[str, str],
        max_step_iterations: int,
        max_tool_calls: int,
        emit: Emit,
    ) -> tuple[dict[str, Any], Any, PlanCheckpoint] | RuntimeOutcome:
        step = next(item for item in plan.steps if item.key == step_key)
        stored_state = checkpoint.step_state or {}
        if (
            stored_state.get("plan_revision") == checkpoint.plan_revision
            and stored_state.get("step_key") == step.key
        ):
            state = dict(stored_state)
        else:
            state = {
                "plan_revision": checkpoint.plan_revision,
                "step_key": step.key,
                "attempt": checkpoint.reflection_count + 1,
                "round": 0,
                "history": [],
                "pending_actions": [],
            }
        attempt = int(state["attempt"])
        await emit(
            RuntimeEventType.PLAN_STEP_STARTED,
            None,
            {
                "plan_revision": checkpoint.plan_revision,
                "step_key": step.key,
                "attempt": attempt,
            },
        )
        while True:
            if cancelled():
                return await self._cancelled(emit, checkpoint=checkpoint.model_dump(mode="json"))
            pending = state.get("pending_actions", [])
            if pending:
                handler = services.tool_handler
                if handler is None:
                    return await self._fail(
                        emit,
                        "TOOL_HANDLER_REQUIRED",
                        "a Plan step requested a tool but no Tool Gateway is available",
                        checkpoint=checkpoint.model_dump(mode="json"),
                        usage=checkpoint.usage,
                    )
                consumed = int(checkpoint.usage.get("tool_calls") or 0)
                if consumed + len(pending) > max_tool_calls:
                    return await self._fail(
                        emit,
                        "TOOL_BUDGET_EXHAUSTED",
                        "plan_and_execute exhausted its tool-call budget",
                        checkpoint=checkpoint.model_dump(mode="json"),
                        usage=checkpoint.usage,
                    )
                history = tuple(state.get("history", []))
                observed_web_urls = checkpoint.observed_web_urls
                for action in sorted(pending, key=lambda item: str(item["call_id"])):
                    await emit(
                        RuntimeEventType.TOOL_CALL_STARTED,
                        None,
                        {
                            "call_id": action["call_id"],
                            "tool": f"{action['name']}@{action['version']}",
                            "idempotency_key": action["idempotency_key"],
                            "plan_revision": checkpoint.plan_revision,
                            "plan_step_key": step.key,
                            "attempt": attempt,
                        },
                    )
                    try:
                        outcome = await handler.execute_tool(
                            RuntimeToolIntent(
                                call_id=str(action["call_id"]),
                                name=str(action["name"]),
                                version=str(action["version"]),
                                arguments=dict(action["arguments"]),
                                idempotency_key=str(action["idempotency_key"]),
                                checkpoint=checkpoint.model_dump(mode="json"),
                            )
                        )
                    except ToolApprovalRequired as exc:
                        return RuntimeOutcome.suspended(
                            checkpoint=checkpoint.model_dump(mode="json"),
                            wake_condition={
                                "type": "tool_approval",
                                **exc.details,
                            },
                            usage=checkpoint.usage,
                        )
                    except Exception as exc:
                        return await self._fail(
                            emit,
                            getattr(exc, "code", "TOOL_HANDLER_ERROR"),
                            getattr(
                                exc,
                                "message",
                                "the Tool Gateway rejected the Plan step tool call",
                            ),
                            checkpoint=checkpoint.model_dump(mode="json"),
                            usage=checkpoint.usage,
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
                            "plan_revision": checkpoint.plan_revision,
                            "plan_step_key": step.key,
                            "attempt": attempt,
                        },
                    )
                    history += (_tool_history(outcome),)
                    observed_web_urls = merge_observed_web_urls(observed_web_urls, outcome)
                usage = dict(checkpoint.usage)
                usage["tool_calls"] = consumed + len(pending)
                state["history"] = list(history)
                state["pending_actions"] = []
                checkpoint = make_plan_checkpoint(
                    manifest=request.execution_manifest,
                    loop_state="executing",
                    plan_revision=checkpoint.plan_revision,
                    plan_content_hash=checkpoint.plan_content_hash,
                    current_step_key=step.key,
                    completed_step_keys=checkpoint.completed_step_keys,
                    latest_output=checkpoint.latest_output,
                    recovery_instruction=recovery_instruction,
                    context_version=checkpoint.context_version,
                    reflection_count=checkpoint.reflection_count,
                    **_web_checkpoint_state(
                        checkpoint,
                        observed_web_urls=observed_web_urls,
                    ),
                    usage=usage,
                    step_state=state,
                )
                await emit(
                    RuntimeEventType.CHECKPOINT_SAVED,
                    None,
                    checkpoint.model_dump(mode="json"),
                )
                continue

            round_number = int(state.get("round") or 0) + 1
            if round_number > max_step_iterations:
                return await self._fail(
                    emit,
                    "PLAN_STEP_ITERATIONS_EXHAUSTED",
                    "Plan step exhausted its model iteration budget",
                    checkpoint=checkpoint.model_dump(mode="json"),
                    usage=checkpoint.usage,
                )
            call_key = _next_named_call_key(
                (
                    f"plan:{checkpoint.plan_revision}:step:{step.key}:"
                    f"attempt:{attempt}:round:{round_number}"
                ),
                request.recovery_state,
            )
            context = build_phase_context(
                request,
                phase="plan_step",
                instruction=(
                    "Execute this Plan step with authorized tools when needed. "
                    "After observing tool results, return strict JSON with one object field "
                    "named output."
                ),
                payload={
                    "plan": plan.model_dump(mode="json"),
                    "step": step.model_dump(mode="json"),
                    "completed_steps": list(checkpoint.completed_step_keys),
                    "previous_output": checkpoint.latest_output,
                    "recovery_instruction": recovery_instruction,
                },
                version=checkpoint.context_version + 1,
                source_refs=(f"plan:{checkpoint.plan_revision}", f"plan-step:{step.key}"),
                history=tuple(state.get("history", [])),
            )
            try:
                round_result = await self._model_round(
                    request,
                    endpoint=endpoint,
                    model=model,
                    context=context,
                    call_key=call_key,
                    tools=tools,
                    response_format=_step_response_format(),
                    context_reason="plan_step",
                    emit=emit,
                )
            except ModelError as exc:
                return await self._model_failure(
                    emit, exc, checkpoint=checkpoint.model_dump(mode="json")
                )
            usage = _merge_usage(checkpoint.usage, round_result.usage_payload)
            response = round_result.response
            if response.tool_calls:
                if services.tool_handler is None:
                    return await self._fail(
                        emit,
                        "TOOL_HANDLER_REQUIRED",
                        "a Plan step requested a tool but no Tool Gateway is available",
                        checkpoint=checkpoint.model_dump(mode="json"),
                        usage=usage,
                    )
                try:
                    actions = _plan_pending_actions(
                        request,
                        checkpoint.plan_revision,
                        step.key,
                        attempt,
                        response,
                        tool_versions,
                    )
                except ValueError as exc:
                    return await self._fail(
                        emit,
                        "TOOL_NOT_AUTHORIZED",
                        str(exc),
                        checkpoint=checkpoint.model_dump(mode="json"),
                        usage=usage,
                    )
                if int(usage.get("tool_calls") or 0) + len(actions) > max_tool_calls:
                    return await self._fail(
                        emit,
                        "TOOL_BUDGET_EXHAUSTED",
                        "plan_and_execute exhausted its tool-call budget",
                        checkpoint=checkpoint.model_dump(mode="json"),
                        usage=usage,
                    )
                history = tuple(state.get("history", [])) + (_assistant_history(response),)
                state.update(
                    {
                        "round": round_number,
                        "history": list(history),
                        "pending_actions": list(actions),
                    }
                )
                checkpoint = make_plan_checkpoint(
                    manifest=request.execution_manifest,
                    loop_state="executing",
                    plan_revision=checkpoint.plan_revision,
                    plan_content_hash=checkpoint.plan_content_hash,
                    current_step_key=step.key,
                    completed_step_keys=checkpoint.completed_step_keys,
                    latest_output=checkpoint.latest_output,
                    recovery_instruction=recovery_instruction,
                    context_version=context.version,
                    reflection_count=checkpoint.reflection_count,
                    **_web_checkpoint_state(checkpoint),
                    usage=usage,
                    step_state=state,
                )
                await emit(
                    RuntimeEventType.CHECKPOINT_SAVED,
                    None,
                    checkpoint.model_dump(mode="json"),
                )
                continue
            try:
                output = _parse_step_output(response.text)
            except ValueError:
                output = {}
            break

        evaluation = evaluate_completion(
            output,
            acceptance=step.acceptance,
            evidence_refs=(f"model-call:{call_key}",),
        )
        await emit(
            RuntimeEventType.EVALUATION_COMPLETED,
            None,
            {
                "evaluation_type": "step_validation",
                "method": "deterministic",
                "verdict": "passed" if evaluation.passed else "failed",
                "plan_revision": checkpoint.plan_revision,
                "plan_step_key": step.key,
                "input_hash": checkpoint.plan_content_hash,
                "output_hash": evaluation.output_hash,
                "evidence_refs": list(evaluation.evidence_refs),
                "result": evaluation.model_dump(mode="json"),
            },
        )
        if evaluation.passed:
            completed = checkpoint.completed_step_keys + (step.key,)
            next_checkpoint = make_plan_checkpoint(
                manifest=request.execution_manifest,
                loop_state="executing",
                plan_revision=checkpoint.plan_revision,
                plan_content_hash=checkpoint.plan_content_hash,
                completed_step_keys=completed,
                latest_output=output,
                context_version=context.version,
                reflection_count=checkpoint.reflection_count,
                **_web_checkpoint_state(checkpoint),
                usage=usage,
                step_state=None,
            )
            await emit(
                RuntimeEventType.PLAN_STEP_COMPLETED,
                None,
                {
                    "plan_revision": checkpoint.plan_revision,
                    "step_key": step.key,
                    "attempt": attempt,
                    "model_call_key": call_key,
                    "output": output,
                    "output_hash": evaluation.output_hash,
                    "evidence_refs": list(evaluation.evidence_refs),
                },
            )
            await emit(
                RuntimeEventType.CHECKPOINT_SAVED,
                None,
                next_checkpoint.model_dump(mode="json"),
            )
            return output, evaluation, next_checkpoint

        next_checkpoint = make_plan_checkpoint(
            manifest=request.execution_manifest,
            loop_state="reflecting",
            plan_revision=checkpoint.plan_revision,
            plan_content_hash=checkpoint.plan_content_hash,
            current_step_key=step.key,
            completed_step_keys=checkpoint.completed_step_keys,
            latest_output=output,
            context_version=context.version,
            reflection_count=checkpoint.reflection_count,
            **_web_checkpoint_state(checkpoint),
            usage=usage,
            step_state=None,
        )
        await emit(
            RuntimeEventType.PLAN_STEP_FAILED,
            None,
            {
                "plan_revision": checkpoint.plan_revision,
                "step_key": step.key,
                "attempt": attempt,
                "model_call_key": call_key,
                "output": output,
                "output_hash": evaluation.output_hash,
                "error": {"code": "STEP_VALIDATION_FAILED"},
            },
        )
        return output, evaluation, next_checkpoint

    async def _reflect(
        self,
        request: RuntimeSessionRequest,
        *,
        endpoint: dict[str, Any],
        model: str,
        checkpoint: PlanCheckpoint,
        plan: PlanDraft,
        failed_step_key: str,
        failed_output: dict[str, Any],
        failed_checks: list[dict[str, Any]],
        max_reflections: int,
        emit: Emit,
    ) -> tuple[ReflectionDecision, PlanCheckpoint] | RuntimeOutcome:
        if checkpoint.reflection_count >= max_reflections:
            return await self._fail(
                emit,
                "REFLECTION_BUDGET_EXHAUSTED",
                "plan_and_execute exhausted its Reflection budget",
                checkpoint=checkpoint.model_dump(mode="json"),
                usage=checkpoint.usage,
            )
        number = checkpoint.reflection_count + 1
        call_key = _next_named_call_key(f"reflection:{number}", request.recovery_state)
        context = build_phase_context(
            request,
            phase="reflection",
            instruction="Choose exactly one recovery decision: retry, replan, or fail.",
            payload={
                "plan": plan.model_dump(mode="json"),
                "failed_step_key": failed_step_key,
                "failed_output": failed_output,
                "failed_checks": failed_checks,
            },
            version=checkpoint.context_version + 1,
            source_refs=(f"plan:{checkpoint.plan_revision}:{failed_step_key}",),
        )
        try:
            round_result = await self._model_round(
                request,
                endpoint=endpoint,
                model=model,
                context=context,
                call_key=call_key,
                tools=(),
                response_format=reflection_response_format(),
                context_reason="reflection",
                emit=emit,
            )
            decision = parse_reflection(round_result.response.text)
        except ModelError as exc:
            return await self._model_failure(
                emit, exc, checkpoint=checkpoint.model_dump(mode="json")
            )
        except ValueError:
            return await self._fail(
                emit,
                "REFLECTION_OUTPUT_INVALID",
                "reflection returned an invalid recovery decision",
                checkpoint=checkpoint.model_dump(mode="json"),
                usage=checkpoint.usage,
            )
        usage = _merge_usage(checkpoint.usage, round_result.usage_payload)
        next_checkpoint = make_plan_checkpoint(
            manifest=request.execution_manifest,
            loop_state="reflecting",
            plan_revision=checkpoint.plan_revision,
            plan_content_hash=checkpoint.plan_content_hash,
            current_step_key=failed_step_key,
            completed_step_keys=checkpoint.completed_step_keys,
            latest_output=checkpoint.latest_output,
            recovery_instruction=decision.recovery_instruction or decision.reason,
            recovery_decision=decision.decision,
            context_version=context.version,
            reflection_count=number,
            **_web_checkpoint_state(checkpoint),
            usage=usage,
        )
        await emit(
            RuntimeEventType.REFLECTION_COMPLETED,
            None,
            {
                "plan_revision": checkpoint.plan_revision,
                "plan_step_key": failed_step_key,
                "reflection_number": number,
                "decision": decision.decision,
                "reason": decision.reason,
                "recovery_instruction": decision.recovery_instruction,
                "model_call_key": call_key,
            },
        )
        await emit(
            RuntimeEventType.EVALUATION_COMPLETED,
            None,
            {
                "evaluation_type": "reflection",
                "method": "model",
                "verdict": decision.decision,
                "plan_revision": checkpoint.plan_revision,
                "plan_step_key": failed_step_key,
                "model_call_key": call_key,
                "input_hash": checkpoint.plan_content_hash,
                "output_hash": _hash_json(decision.model_dump(mode="json")),
                "evidence_refs": [f"model-call:{call_key}"],
                "result": decision.model_dump(mode="json"),
            },
        )
        await emit(
            RuntimeEventType.CHECKPOINT_SAVED,
            None,
            next_checkpoint.model_dump(mode="json"),
        )
        return decision, next_checkpoint

    async def _citation_repair_round(
        self,
        request: RuntimeSessionRequest,
        *,
        endpoint: dict[str, Any],
        model: str,
        provisional_output: dict[str, Any],
        observed_urls: tuple[str, ...],
        context_version: int,
        mode: str,
        emit: Emit,
    ) -> _ModelRound | None:
        call_key = _next_named_call_key(
            f"citation-repair:{mode}",
            request.recovery_state,
        )
        context = build_citation_repair_context(
            request,
            provisional_output=provisional_output,
            observed_urls=observed_urls,
            version=context_version,
        )
        await emit(
            RuntimeEventType.STEP_STARTED,
            None,
            {
                "step_key": f"citation-repair:{mode}",
                "step_type": "reasoning",
                "name": "web.citation_repair",
            },
        )
        try:
            result = await self._model_round(
                request,
                endpoint=endpoint,
                model=model,
                context=context,
                call_key=call_key,
                tools=(),
                emit=emit,
            )
        except ModelError:
            await emit(
                RuntimeEventType.STEP_COMPLETED,
                None,
                {
                    "step_key": f"citation-repair:{mode}",
                    "step_type": "reasoning",
                    "name": "web.citation_repair",
                    "model_call_key": call_key,
                    "decision": "model_error",
                },
            )
            return None
        response = result.response
        await emit(
            RuntimeEventType.STEP_COMPLETED,
            None,
            {
                "step_key": f"citation-repair:{mode}",
                "step_type": "reasoning",
                "name": "web.citation_repair",
                "model_call_key": call_key,
                "context_version": context.version,
                "decision": (
                    "tool_calls"
                    if response.tool_calls
                    else ("final" if response.text.strip() else "empty")
                ),
            },
        )
        return result

    async def _finish_react_citation_repair(
        self,
        request: RuntimeSessionRequest,
        checkpoint: ReactCheckpoint,
        *,
        endpoint: dict[str, Any],
        model: str,
        token_limit: int,
        emit: Emit,
        cancelled: Callable[[], bool],
    ) -> RuntimeOutcome:
        provisional = checkpoint.citation_provisional_output or {}
        if cancelled():
            return await self._cancelled(emit, checkpoint=checkpoint.model_dump(mode="json"))
        repair = None
        reason = "repair_failed"
        if not token_limit or int(checkpoint.usage.get("total_tokens") or 0) < token_limit:
            repair = await self._citation_repair_round(
                request,
                endpoint=endpoint,
                model=model,
                provisional_output=provisional,
                observed_urls=checkpoint.observed_web_urls,
                context_version=checkpoint.context_version + 1,
                mode="react",
                emit=emit,
            )
        else:
            reason = "token_budget"
        usage = checkpoint.usage
        output = provisional
        context_version = checkpoint.context_version
        context_hash = checkpoint.context_hash
        call_key = checkpoint.last_model_call_key
        if repair is not None:
            usage = _merge_usage(checkpoint.usage, repair.usage_payload)
            context_version = repair.context_version
            context_hash = repair.context_hash
            call_key = repair.call_key
            candidate = _repaired_output("react", provisional, repair.response)
            if candidate is not None and output_has_observed_web_citation(
                candidate,
                checkpoint.observed_web_urls,
            ):
                output = candidate
                reason = "repaired"
        passed = reason == "repaired"
        if not passed:
            output = _with_citation_diagnostic(
                provisional,
                observed_count=len(checkpoint.observed_web_urls),
                repair_attempted=repair is not None,
                reason=reason,
            )
        await _emit_citation_evaluation(
            emit,
            passed=passed,
            phase="repair",
            observed_count=len(checkpoint.observed_web_urls),
            repair_attempted=repair is not None,
            reason=reason,
        )
        completed = make_react_checkpoint(
            manifest=request.execution_manifest,
            loop_state="completed",
            iteration=checkpoint.iteration,
            context_version=context_version,
            context_hash=context_hash,
            last_model_call_key=call_key,
            history=checkpoint.history,
            completed_action_keys=checkpoint.completed_action_keys,
            tool_calls_consumed=checkpoint.tool_calls_consumed,
            coordination_calls_consumed=checkpoint.coordination_calls_consumed,
            consumed_message_ids=checkpoint.consumed_message_ids,
            **_web_checkpoint_state(checkpoint),
            usage=usage,
        )
        await emit(RuntimeEventType.CHECKPOINT_SAVED, None, completed.model_dump(mode="json"))
        await emit(RuntimeEventType.RUN_COMPLETED, None, {"output": output})
        return RuntimeOutcome.terminal(
            status=RuntimeSessionStatus.COMPLETED,
            output=output,
            usage=usage,
            checkpoint=completed.model_dump(mode="json"),
        )

    async def _finish_plan_citation_repair(
        self,
        request: RuntimeSessionRequest,
        checkpoint: PlanCheckpoint,
        *,
        endpoint: dict[str, Any],
        model: str,
        token_limit: int,
        emit: Emit,
        cancelled: Callable[[], bool],
    ) -> RuntimeOutcome:
        provisional = checkpoint.citation_provisional_output or {}
        if cancelled():
            return await self._cancelled(emit, checkpoint=checkpoint.model_dump(mode="json"))
        repair = None
        reason = "repair_failed"
        if not token_limit or int(checkpoint.usage.get("total_tokens") or 0) < token_limit:
            repair = await self._citation_repair_round(
                request,
                endpoint=endpoint,
                model=model,
                provisional_output=provisional,
                observed_urls=checkpoint.observed_web_urls,
                context_version=checkpoint.context_version + 1,
                mode="plan",
                emit=emit,
            )
        else:
            reason = "token_budget"
        usage = checkpoint.usage
        output = provisional
        context_version = checkpoint.context_version
        if repair is not None:
            usage = _merge_usage(checkpoint.usage, repair.usage_payload)
            context_version = repair.context_version
            candidate = _repaired_output("plan", provisional, repair.response)
            if candidate is not None and output_has_observed_web_citation(
                candidate,
                checkpoint.observed_web_urls,
            ):
                output = candidate
                reason = "repaired"
        passed = reason == "repaired"
        if not passed:
            output = _with_citation_diagnostic(
                provisional,
                observed_count=len(checkpoint.observed_web_urls),
                repair_attempted=repair is not None,
                reason=reason,
            )
        await _emit_citation_evaluation(
            emit,
            passed=passed,
            phase="repair",
            observed_count=len(checkpoint.observed_web_urls),
            repair_attempted=repair is not None,
            reason=reason,
        )
        latest_output = output.get("result")
        if not isinstance(latest_output, dict):
            latest_output = checkpoint.latest_output
        completed = make_plan_checkpoint(
            manifest=request.execution_manifest,
            loop_state="completed",
            plan_revision=checkpoint.plan_revision,
            plan_content_hash=checkpoint.plan_content_hash,
            completed_step_keys=checkpoint.completed_step_keys,
            latest_output=latest_output,
            context_version=context_version,
            reflection_count=checkpoint.reflection_count,
            **_web_checkpoint_state(checkpoint),
            usage=usage,
        )
        await emit(
            RuntimeEventType.PLAN_STATUS_CHANGED,
            None,
            {"revision": completed.plan_revision, "status": "completed"},
        )
        await emit(RuntimeEventType.CHECKPOINT_SAVED, None, completed.model_dump(mode="json"))
        await emit(RuntimeEventType.RUN_COMPLETED, None, {"output": output})
        return RuntimeOutcome.terminal(
            status=RuntimeSessionStatus.COMPLETED,
            output=output,
            usage=usage,
            checkpoint=completed.model_dump(mode="json"),
        )

    async def _execute_react(
        self,
        request: RuntimeSessionRequest,
        *,
        services: RuntimeServices,
        emit: Emit,
        cancelled: Callable[[], bool],
    ) -> RuntimeOutcome:
        endpoint, model, failure = await self._requirements(request, emit)
        if failure is not None:
            return failure
        assert endpoint is not None and model is not None
        if (
            services.tool_handler is None
            and services.coordination_handler is None
            and services.artifact_handler is None
        ):
            return await self._fail(
                emit,
                "TOOL_HANDLER_REQUIRED",
                "react mode requires a platform Tool or Coordination handler",
            )
        if cancelled():
            return await self._cancelled(emit)
        try:
            checkpoint = load_react_checkpoint(
                request.checkpoint,
                manifest=request.execution_manifest,
            )
        except ValueError:
            return await self._fail(
                emit,
                "CHECKPOINT_INVALID",
                "the native react checkpoint is corrupt or incompatible",
            )
        if checkpoint.loop_state in {"completed", "failed", "cancelled"}:
            return await self._fail(
                emit,
                "CHECKPOINT_TERMINAL",
                "a terminal native checkpoint cannot be resumed",
            )

        tools: tuple[RuntimeToolSpec, ...] = ()
        if services.tool_handler is not None:
            tools = await services.tool_handler.list_tools()
        try:
            tool_versions = _exact_tool_versions(tools)
        except ValueError as exc:
            return await self._fail(emit, "TOOL_VERSION_AMBIGUOUS", str(exc))
        definitions: tuple[ModelToolDefinition, ...] = tuple(
            ModelToolDefinition(
                name=spec.name,
                description=f"{spec.description} Exact platform version: {spec.version}.",
                input_schema=spec.input_schema,
            )
            for spec in tools
        )
        coordination_enabled = (
            services.coordination_handler is not None
            and request.coordination_policy_snapshot.get("version") == 1
            and request.coordination_policy_snapshot.get("enabled") is True
        )
        if coordination_enabled:
            if "delegate_agent" in tool_versions:
                return await self._fail(
                    emit,
                    "TOOL_NAME_RESERVED",
                    "delegate_agent is reserved for native coordination",
                )
            definitions = (*definitions, _coordination_definition())
        artifact_enabled = services.artifact_handler is not None
        if artifact_enabled:
            if "store_artifact" in tool_versions:
                return await self._fail(
                    emit,
                    "TOOL_NAME_RESERVED",
                    "store_artifact is reserved for native Artifact storage",
                )
            definitions = (*definitions, _artifact_definition())
        max_iterations = _budget(request, "max_iterations", 8, minimum=1, maximum=64)
        max_tool_calls = _budget(request, "max_tool_calls", 32, minimum=0, maximum=1024)
        token_limit = _budget(request, "token_limit", 0, minimum=0, maximum=10**9)

        if checkpoint.loop_state == "waiting_for_subagent":
            resumed = await self._resume_coordination(request, checkpoint, emit=emit)
            if isinstance(resumed, RuntimeOutcome):
                return resumed
            checkpoint = resumed

        while True:
            if cancelled():
                return await self._cancelled(emit, checkpoint=checkpoint.model_dump(mode="json"))
            if (
                checkpoint.citation_repair_attempted
                and checkpoint.citation_provisional_output is not None
            ):
                return await self._finish_react_citation_repair(
                    request,
                    checkpoint,
                    endpoint=endpoint,
                    model=model,
                    token_limit=token_limit,
                    emit=emit,
                    cancelled=cancelled,
                )
            if checkpoint.pending_actions:
                action_result = await self._execute_pending_actions(
                    request,
                    checkpoint,
                    services=services,
                    emit=emit,
                    cancelled=cancelled,
                    max_tool_calls=max_tool_calls,
                )
                if isinstance(action_result, RuntimeOutcome):
                    return action_result
                checkpoint = action_result
                if checkpoint.iteration > max_iterations:
                    return await self._fail(
                        emit,
                        "MAX_ITERATIONS_EXCEEDED",
                        "react mode exhausted its model iteration budget",
                        checkpoint=checkpoint.model_dump(mode="json"),
                    )
                continue

            if checkpoint.iteration > max_iterations:
                return await self._fail(
                    emit,
                    "MAX_ITERATIONS_EXCEEDED",
                    "react mode exhausted its model iteration budget",
                    checkpoint=checkpoint.model_dump(mode="json"),
                )
            if token_limit and int(checkpoint.usage.get("total_tokens") or 0) >= token_limit:
                return await self._fail(
                    emit,
                    "TOKEN_BUDGET_EXHAUSTED",
                    "react mode exhausted its token budget",
                    checkpoint=checkpoint.model_dump(mode="json"),
                )

            context_version = checkpoint.context_version + 1
            context = build_native_context(
                request,
                mode="react",
                history=checkpoint.history,
                version=context_version,
            )
            call_key = _next_call_key(checkpoint.iteration, request.recovery_state)
            step_key = f"reasoning:{checkpoint.iteration}"
            await emit(
                RuntimeEventType.STEP_STARTED,
                None,
                {
                    "step_key": step_key,
                    "step_type": "reasoning",
                    "iteration": checkpoint.iteration,
                    "name": "react.reasoning",
                },
            )
            try:
                round_result = await self._model_round(
                    request,
                    endpoint=endpoint,
                    model=model,
                    context=context,
                    call_key=call_key,
                    tools=definitions,
                    emit=emit,
                )
            except ModelError as exc:
                return await self._model_failure(
                    emit,
                    exc,
                    checkpoint=checkpoint.model_dump(mode="json"),
                )
            usage = _merge_usage(checkpoint.usage, round_result.usage_payload)
            response = round_result.response
            await emit(
                RuntimeEventType.STEP_COMPLETED,
                None,
                {
                    "step_key": step_key,
                    "step_type": "reasoning",
                    "iteration": checkpoint.iteration,
                    "name": "react.reasoning",
                    "model_call_key": call_key,
                    "context_version": context_version,
                    "decision": "tool_calls" if response.tool_calls else "final",
                },
            )

            if response.tool_calls:
                try:
                    actions = _pending_actions(
                        request,
                        checkpoint.iteration,
                        response,
                        tool_versions,
                        coordination_enabled=coordination_enabled,
                        artifact_enabled=artifact_enabled,
                    )
                except ValueError as exc:
                    return await self._fail(
                        emit,
                        "TOOL_NOT_AUTHORIZED",
                        str(exc),
                        checkpoint=checkpoint.model_dump(mode="json"),
                    )
                assistant = _assistant_history(response)
                checkpoint = make_react_checkpoint(
                    manifest=request.execution_manifest,
                    loop_state="waiting_for_tool",
                    iteration=checkpoint.iteration,
                    context_version=context_version,
                    context_hash=context.content_hash,
                    last_model_call_key=call_key,
                    history=checkpoint.history + (assistant,),
                    pending_actions=actions,
                    completed_action_keys=checkpoint.completed_action_keys,
                    tool_calls_consumed=checkpoint.tool_calls_consumed,
                    coordination_calls_consumed=checkpoint.coordination_calls_consumed,
                    waiting_delegations=checkpoint.waiting_delegations,
                    consumed_message_ids=checkpoint.consumed_message_ids,
                    **_web_checkpoint_state(checkpoint),
                    usage=usage,
                )
                await emit(
                    RuntimeEventType.CHECKPOINT_SAVED,
                    None,
                    checkpoint.model_dump(mode="json"),
                )
                continue

            if not response.text.strip():
                return await self._fail(
                    emit,
                    "EMPTY_MODEL_OUTPUT",
                    "model returned neither a final result nor a tool call",
                    checkpoint=checkpoint.model_dump(mode="json"),
                )
            output = {"content": response.text}
            if checkpoint.observed_web_urls:
                citation_present = output_has_observed_web_citation(
                    output,
                    checkpoint.observed_web_urls,
                )
                can_repair = (
                    not citation_present
                    and checkpoint.iteration < max_iterations
                    and (not token_limit or int(usage.get("total_tokens") or 0) < token_limit)
                )
                await _emit_citation_evaluation(
                    emit,
                    passed=citation_present,
                    phase="initial",
                    observed_count=len(checkpoint.observed_web_urls),
                    repair_attempted=False,
                    reason=(
                        "present"
                        if citation_present
                        else ("repair_scheduled" if can_repair else "budget_exhausted")
                    ),
                )
                if can_repair:
                    checkpoint = make_react_checkpoint(
                        manifest=request.execution_manifest,
                        loop_state="reasoning",
                        iteration=checkpoint.iteration + 1,
                        context_version=context_version,
                        context_hash=context.content_hash,
                        last_model_call_key=call_key,
                        history=checkpoint.history,
                        completed_action_keys=checkpoint.completed_action_keys,
                        tool_calls_consumed=checkpoint.tool_calls_consumed,
                        coordination_calls_consumed=checkpoint.coordination_calls_consumed,
                        consumed_message_ids=checkpoint.consumed_message_ids,
                        **_web_checkpoint_state(
                            checkpoint,
                            citation_repair_attempted=True,
                            citation_provisional_output=output,
                        ),
                        usage=usage,
                    )
                    await emit(
                        RuntimeEventType.CHECKPOINT_SAVED,
                        None,
                        checkpoint.model_dump(mode="json"),
                    )
                    continue
                if not citation_present:
                    output = _with_citation_diagnostic(
                        output,
                        observed_count=len(checkpoint.observed_web_urls),
                        repair_attempted=False,
                        reason="budget_exhausted",
                    )
            checkpoint = make_react_checkpoint(
                manifest=request.execution_manifest,
                loop_state="completed",
                iteration=checkpoint.iteration,
                context_version=context_version,
                context_hash=context.content_hash,
                last_model_call_key=call_key,
                history=checkpoint.history,
                completed_action_keys=checkpoint.completed_action_keys,
                tool_calls_consumed=checkpoint.tool_calls_consumed,
                coordination_calls_consumed=checkpoint.coordination_calls_consumed,
                consumed_message_ids=checkpoint.consumed_message_ids,
                **_web_checkpoint_state(checkpoint),
                usage=usage,
            )
            await emit(
                RuntimeEventType.CHECKPOINT_SAVED,
                None,
                checkpoint.model_dump(mode="json"),
            )
            await emit(RuntimeEventType.RUN_COMPLETED, None, {"output": output})
            return RuntimeOutcome.terminal(
                status=RuntimeSessionStatus.COMPLETED,
                output=output,
                usage=usage,
                checkpoint=checkpoint.model_dump(mode="json"),
            )

    async def _execute_pending_actions(
        self,
        request: RuntimeSessionRequest,
        checkpoint: ReactCheckpoint,
        *,
        services: RuntimeServices,
        emit: Emit,
        cancelled: Callable[[], bool],
        max_tool_calls: int,
    ) -> ReactCheckpoint | RuntimeOutcome:
        tool_action_count = sum(
            1 for action in checkpoint.pending_actions if action.get("kind") == "tool"
        )
        if checkpoint.tool_calls_consumed + tool_action_count > max_tool_calls:
            return await self._fail(
                emit,
                "TOOL_BUDGET_EXHAUSTED",
                "react mode exhausted its tool-call budget",
                checkpoint=checkpoint.model_dump(mode="json"),
            )
        history = checkpoint.history
        observed_web_urls = checkpoint.observed_web_urls
        completed = list(checkpoint.completed_action_keys)
        waiting_delegations: list[dict[str, Any]] = []
        for action in sorted(checkpoint.pending_actions, key=lambda item: str(item["call_id"])):
            if cancelled():
                return await self._cancelled(emit, checkpoint=checkpoint.model_dump(mode="json"))
            if action.get("kind") == "coordination":
                coordination_handler = services.coordination_handler
                if coordination_handler is None:
                    return await self._fail(
                        emit,
                        "COORDINATION_HANDLER_REQUIRED",
                        "native coordination handler is unavailable",
                        checkpoint=checkpoint.model_dump(mode="json"),
                    )
                try:
                    delegation = DelegationIntent.model_validate(
                        {
                            **dict(action["arguments"]),
                            "idempotency_key": str(action["idempotency_key"]),
                        }
                    )
                    await emit(
                        RuntimeEventType.DELEGATION_STARTED,
                        None,
                        {
                            "call_id": action["call_id"],
                            "target_agent_version_id": str(delegation.target_agent_version_id),
                            "iteration": checkpoint.iteration,
                        },
                    )
                    coordination_outcome = await coordination_handler.coordinate(
                        RuntimeCoordinationIntent(action="delegate", delegation=delegation)
                    )
                except (ValidationError, ValueError) as exc:
                    return await self._fail(
                        emit,
                        "COORDINATION_INTENT_INVALID",
                        str(exc),
                        checkpoint=checkpoint.model_dump(mode="json"),
                    )
                except Exception as exc:
                    return await self._fail(
                        emit,
                        getattr(exc, "code", "COORDINATION_HANDLER_ERROR"),
                        getattr(exc, "message", "the Coordination Service rejected delegation"),
                        checkpoint=checkpoint.model_dump(mode="json"),
                    )
                if (
                    coordination_outcome.status != "accepted"
                    or coordination_outcome.child_run_id is None
                    or coordination_outcome.delegation_id is None
                ):
                    return await self._fail(
                        emit,
                        "DELEGATION_REJECTED",
                        "the Coordination Service did not accept the child Run",
                        checkpoint=checkpoint.model_dump(mode="json"),
                    )
                waiting_delegations.append(
                    {
                        "call_id": str(action["call_id"]),
                        "idempotency_key": str(action["idempotency_key"]),
                        "delegation_id": str(coordination_outcome.delegation_id),
                        "child_run_id": str(coordination_outcome.child_run_id),
                    }
                )
                await emit(
                    RuntimeEventType.DELEGATION_ACCEPTED,
                    None,
                    waiting_delegations[-1],
                )
            elif action.get("kind") == "artifact":
                artifact_handler = services.artifact_handler
                if artifact_handler is None:
                    return await self._fail(
                        emit,
                        "ARTIFACT_HANDLER_REQUIRED",
                        "native Artifact handler is unavailable",
                        checkpoint=checkpoint.model_dump(mode="json"),
                    )
                try:
                    artifact_intent = RuntimeArtifactIntent.model_validate(
                        {
                            **dict(action["arguments"]),
                            "idempotency_key": str(action["idempotency_key"]),
                        }
                    )
                    artifact_outcome = await artifact_handler.store_artifact(artifact_intent)
                except (ValidationError, ValueError) as exc:
                    return await self._fail(
                        emit,
                        "ARTIFACT_INTENT_INVALID",
                        str(exc),
                        checkpoint=checkpoint.model_dump(mode="json"),
                    )
                except Exception as exc:
                    return await self._fail(
                        emit,
                        getattr(exc, "code", "ARTIFACT_HANDLER_ERROR"),
                        getattr(exc, "message", "Artifact storage rejected the content"),
                        checkpoint=checkpoint.model_dump(mode="json"),
                    )
                history += (_artifact_history(str(action["call_id"]), artifact_outcome),)
                await emit(
                    RuntimeEventType.ARTIFACT_STORED,
                    None,
                    {
                        "artifact_id": str(artifact_outcome.artifact_id),
                        "sha256": artifact_outcome.sha256,
                        "size_bytes": artifact_outcome.size_bytes,
                    },
                )
            else:
                handler = services.tool_handler
                if handler is None:
                    return await self._fail(
                        emit,
                        "TOOL_HANDLER_REQUIRED",
                        "the Tool Gateway handler is unavailable",
                        checkpoint=checkpoint.model_dump(mode="json"),
                    )
                await emit(
                    RuntimeEventType.TOOL_CALL_STARTED,
                    None,
                    {
                        "call_id": action["call_id"],
                        "tool": f"{action['name']}@{action['version']}",
                        "idempotency_key": action["idempotency_key"],
                        "iteration": checkpoint.iteration,
                    },
                )
                intent = RuntimeToolIntent(
                    call_id=str(action["call_id"]),
                    name=str(action["name"]),
                    version=str(action["version"]),
                    arguments=dict(action["arguments"]),
                    idempotency_key=str(action["idempotency_key"]),
                    checkpoint=checkpoint.model_dump(mode="json"),
                )
                try:
                    outcome = await handler.execute_tool(intent)
                except ToolApprovalRequired as exc:
                    return RuntimeOutcome.suspended(
                        checkpoint=checkpoint.model_dump(mode="json"),
                        wake_condition={
                            "type": "tool_approval",
                            **exc.details,
                        },
                        usage=checkpoint.usage,
                    )
                except Exception as exc:
                    return await self._fail(
                        emit,
                        getattr(exc, "code", "TOOL_HANDLER_ERROR"),
                        getattr(exc, "message", "the Tool Gateway rejected the tool call"),
                        checkpoint=checkpoint.model_dump(mode="json"),
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
                        "iteration": checkpoint.iteration,
                    },
                )
                history += (_tool_history(outcome),)
                observed_web_urls = merge_observed_web_urls(observed_web_urls, outcome)
            completed.append(str(action["idempotency_key"]))

        if waiting_delegations:
            waiting = make_react_checkpoint(
                manifest=request.execution_manifest,
                loop_state="waiting_for_subagent",
                iteration=checkpoint.iteration,
                context_version=checkpoint.context_version,
                context_hash=checkpoint.context_hash,
                last_model_call_key=checkpoint.last_model_call_key,
                history=history,
                completed_action_keys=tuple(completed),
                tool_calls_consumed=checkpoint.tool_calls_consumed + tool_action_count,
                coordination_calls_consumed=(
                    checkpoint.coordination_calls_consumed + len(waiting_delegations)
                ),
                waiting_delegations=tuple(waiting_delegations),
                consumed_message_ids=checkpoint.consumed_message_ids,
                **_web_checkpoint_state(
                    checkpoint,
                    observed_web_urls=observed_web_urls,
                ),
                usage=checkpoint.usage,
            )
            await emit(
                RuntimeEventType.CHECKPOINT_SAVED,
                None,
                waiting.model_dump(mode="json"),
            )
            return RuntimeOutcome.suspended(
                checkpoint=waiting.model_dump(mode="json"),
                wake_condition={
                    "type": "all_child_runs_terminal",
                    "child_run_ids": [item["child_run_id"] for item in waiting_delegations],
                },
                usage=checkpoint.usage,
            )

        if self.post_tool_delay_seconds:
            await asyncio.sleep(self.post_tool_delay_seconds)
        observed = make_react_checkpoint(
            manifest=request.execution_manifest,
            loop_state="observing",
            iteration=checkpoint.iteration + 1,
            context_version=checkpoint.context_version,
            context_hash=checkpoint.context_hash,
            last_model_call_key=checkpoint.last_model_call_key,
            history=history,
            completed_action_keys=tuple(completed),
            tool_calls_consumed=checkpoint.tool_calls_consumed + tool_action_count,
            coordination_calls_consumed=checkpoint.coordination_calls_consumed,
            consumed_message_ids=checkpoint.consumed_message_ids,
            **_web_checkpoint_state(
                checkpoint,
                observed_web_urls=observed_web_urls,
            ),
            usage=checkpoint.usage,
        )
        await emit(
            RuntimeEventType.CHECKPOINT_SAVED,
            None,
            observed.model_dump(mode="json"),
        )
        return observed

    async def _resume_coordination(
        self,
        request: RuntimeSessionRequest,
        checkpoint: ReactCheckpoint,
        *,
        emit: Emit,
    ) -> ReactCheckpoint | RuntimeOutcome:
        messages = request.recovery_state.get("coordination_messages", [])
        by_child = {
            str(message.get("child_run_id")): message
            for message in messages
            if isinstance(message, dict)
            and message.get("message_id") not in checkpoint.consumed_message_ids
        }
        if any(item.get("child_run_id") not in by_child for item in checkpoint.waiting_delegations):
            return RuntimeOutcome.suspended(
                checkpoint=checkpoint.model_dump(mode="json"),
                wake_condition={
                    "type": "all_child_runs_terminal",
                    "child_run_ids": [
                        item["child_run_id"] for item in checkpoint.waiting_delegations
                    ],
                },
                usage=checkpoint.usage,
            )
        history = checkpoint.history
        consumed = list(checkpoint.consumed_message_ids)
        for waiting in checkpoint.waiting_delegations:
            message = by_child[str(waiting["child_run_id"])]
            if message.get("status") not in {"completed", "failed", "cancelled", "rejected"}:
                return await self._fail(
                    emit,
                    "COORDINATION_MESSAGE_INVALID",
                    "child result message is not terminal",
                    checkpoint=checkpoint.model_dump(mode="json"),
                )
            history += (_coordination_history(waiting, message),)
            consumed.append(str(message["message_id"]))
            await emit(
                RuntimeEventType.DELEGATION_RESULT_RECEIVED,
                None,
                {
                    "delegation_id": waiting["delegation_id"],
                    "child_run_id": waiting["child_run_id"],
                    "status": message["status"],
                },
            )
        observed = make_react_checkpoint(
            manifest=request.execution_manifest,
            loop_state="observing",
            iteration=checkpoint.iteration + 1,
            context_version=checkpoint.context_version,
            context_hash=checkpoint.context_hash,
            last_model_call_key=checkpoint.last_model_call_key,
            history=history,
            completed_action_keys=checkpoint.completed_action_keys,
            tool_calls_consumed=checkpoint.tool_calls_consumed,
            coordination_calls_consumed=checkpoint.coordination_calls_consumed,
            consumed_message_ids=tuple(consumed),
            **_web_checkpoint_state(checkpoint),
            usage=checkpoint.usage,
        )
        await emit(
            RuntimeEventType.CHECKPOINT_SAVED,
            None,
            observed.model_dump(mode="json"),
        )
        return observed

    async def _model_round(
        self,
        request: RuntimeSessionRequest,
        *,
        endpoint: dict[str, Any],
        model: str,
        context: Any,
        call_key: str,
        tools: tuple[ModelToolDefinition, ...],
        emit: Emit,
        response_format: dict[str, Any] | None = None,
        context_reason: str | None = None,
    ) -> _ModelRound:
        handler = _intervention_handler.get()
        if handler is not None:
            boundary_key = _replay_base(call_key) or call_key
            context = inject_interventions(
                context,
                await handler.freeze(boundary_key[:200]),
            )
        rendered = [
            message.model_dump(mode="json", exclude_none=True) for message in context.messages
        ]
        await emit(
            RuntimeEventType.CONTEXT_SNAPSHOT_CREATED,
            None,
            {
                "schema_version": context.schema_version,
                "version": context.version,
                "reason": context_reason
                or ("initial" if context.version == 1 else "tool_observation"),
                "source_refs": list(context.source_refs),
                "memory_refs": list(context.memory_refs),
                "skill_refs": list(context.skill_refs),
                "effect_metadata": context.effect_metadata,
                "rendered_messages": rendered,
                "token_estimate": context.token_estimate,
                "truncation": context.truncation,
                "content_hash": context.content_hash,
            },
        )
        model_request = ModelRequest(
            model=model,
            messages=context.messages,
            endpoint=endpoint,
            tools=tools,
            response_format=response_format or request.model_config_data.get("response_format"),
            temperature=request.model_config_data.get("temperature"),
            max_output_tokens=request.model_config_data.get("max_output_tokens"),
            timeout_seconds=request.model_config_data.get("timeout_seconds"),
            metadata={"run_id": str(request.run_id), "call_key": call_key},
        )
        request_redacted = {
            "model": model,
            "messages": rendered,
            "tools": [tool.model_dump(mode="json") for tool in tools],
            "response_format": model_request.response_format,
            "temperature": model_request.temperature,
            "max_output_tokens": model_request.max_output_tokens,
        }
        await emit(
            RuntimeEventType.MODEL_CALL_STARTED,
            None,
            {
                "call_key": call_key,
                "replay_of_call_key": _replay_base(call_key),
                "model_endpoint_id": endpoint.get("id"),
                "provider": endpoint.get("protocol", "openai_compatible"),
                "model": model,
                "request_redacted": request_redacted,
                "request_hash": _hash_json(request_redacted),
            },
        )
        text_parts: list[str] = []
        delta_buffer = ""
        last_delta_emit = time.monotonic()
        tool_parts: dict[int, dict[str, str]] = {}
        finish_reason = None
        usage = ModelUsage()
        request_id = None

        async def flush_delta(*, force: bool = True) -> None:
            nonlocal delta_buffer, last_delta_emit
            if not delta_buffer:
                return
            if not force and len(delta_buffer) < 512:
                return
            delta = delta_buffer if force else delta_buffer[:512]
            delta_buffer = "" if force else delta_buffer[512:]
            last_delta_emit = time.monotonic()
            await emit(
                RuntimeEventType.MODEL_OUTPUT_DELTA,
                delta,
                {"call_key": call_key, "delta": delta},
            )

        try:
            async for event in self.gateway.stream(model_request):
                if event.text_delta:
                    text_parts.append(event.text_delta)
                    delta_buffer += event.text_delta
                    while len(delta_buffer) >= 512:
                        await flush_delta(force=False)
                    if time.monotonic() - last_delta_emit >= 0.1:
                        await flush_delta()
                if event.type is ModelStreamEventType.TOOL_CALL_DELTA:
                    index = event.tool_index or 0
                    part = tool_parts.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if event.tool_call_id:
                        part["id"] = event.tool_call_id
                    if event.tool_name:
                        part["name"] = event.tool_name
                    if event.tool_arguments_delta:
                        part["arguments"] += event.tool_arguments_delta
                if event.finish_reason is not None:
                    finish_reason = event.finish_reason
                if event.usage is not None:
                    usage = event.usage
                if event.provider_request_id is not None:
                    request_id = event.provider_request_id
            await flush_delta()
            tool_calls = tuple(
                _tool_call(index, value) for index, value in sorted(tool_parts.items())
            )
        except ModelError as exc:
            await flush_delta()
            await emit(
                RuntimeEventType.MODEL_CALL_FAILED,
                None,
                {
                    "call_key": call_key,
                    "status": "failed",
                    "error": {"code": exc.code, "message": exc.message},
                    "usage": _usage(usage),
                    "usage_status": usage.status,
                    "cost_status": "unknown",
                },
            )
            raise
        response = ModelResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            provider_request_id=request_id,
        )
        response_redacted = {
            "content": response.text,
            "finish_reason": finish_reason,
            "tool_calls": [call.model_dump(mode="json") for call in tool_calls],
        }
        cost, cost_status = _cost(endpoint, usage)
        await emit(
            RuntimeEventType.MODEL_CALL_COMPLETED,
            None,
            {
                "call_key": call_key,
                "response_redacted": response_redacted,
                "response_hash": _hash_json(response_redacted),
                "provider_request_id": request_id,
                "usage": _usage(usage),
                "usage_status": usage.status,
                "cost": cost,
                "cost_status": cost_status,
                "pricing_revision": endpoint.get("pricing_revision"),
            },
        )
        usage_payload = {**_usage(usage), "cost": cost, "cost_status": cost_status}
        return _ModelRound(response, usage_payload, context.content_hash, context.version, call_key)

    async def _execute_direct(
        self,
        request: RuntimeSessionRequest,
        *,
        emit: Emit,
        cancelled: Callable[[], bool],
    ) -> RuntimeOutcome:
        endpoint, model, failure = await self._requirements(request, emit)
        if failure is not None:
            return failure
        assert endpoint is not None and model is not None
        if cancelled():
            return await self._cancelled(emit)
        context = build_direct_context(request)
        step_key = "reasoning:1"
        await emit(
            RuntimeEventType.STEP_STARTED,
            None,
            {
                "step_key": step_key,
                "step_type": "reasoning",
                "iteration": 1,
                "name": "direct.reasoning",
            },
        )
        try:
            round_result = await self._model_round(
                request,
                endpoint=endpoint,
                model=model,
                context=context,
                call_key="model:1",
                tools=(),
                emit=emit,
            )
        except ModelError as exc:
            return await self._model_failure(emit, exc)
        response = round_result.response
        await emit(
            RuntimeEventType.STEP_COMPLETED,
            None,
            {
                "step_key": step_key,
                "step_type": "reasoning",
                "iteration": 1,
                "name": "direct.reasoning",
                "model_call_key": "model:1",
                "context_version": context.version,
                "decision": "tool_calls" if response.tool_calls else "final",
            },
        )
        checkpoint = direct_checkpoint(
            context_hash=context.content_hash,
            call_key="model:1",
            completed=not response.tool_calls,
            usage=round_result.usage_payload,
        )
        await emit(RuntimeEventType.CHECKPOINT_SAVED, None, checkpoint)
        if response.tool_calls or response.finish_reason == "tool_calls":
            return await self._fail(
                emit,
                "MODE_CAPABILITY_VIOLATION",
                "direct mode cannot execute tool or delegation actions",
                checkpoint=checkpoint,
                usage=round_result.usage_payload,
            )
        if not response.text.strip():
            return await self._fail(
                emit,
                "EMPTY_MODEL_OUTPUT",
                "model returned no task result",
                checkpoint=checkpoint,
                usage=round_result.usage_payload,
            )
        output = {"content": response.text}
        await emit(RuntimeEventType.RUN_COMPLETED, None, {"output": output})
        return RuntimeOutcome.terminal(
            status=RuntimeSessionStatus.COMPLETED,
            output=output,
            usage=round_result.usage_payload,
            checkpoint=checkpoint,
        )

    @staticmethod
    async def _requirements(
        request: RuntimeSessionRequest, emit: Emit
    ) -> tuple[dict[str, Any] | None, str | None, RuntimeOutcome | None]:
        endpoint = request.model_endpoint_snapshot
        if endpoint is None:
            return (
                None,
                None,
                await NativeAgentLoop._fail(
                    emit,
                    "MODEL_ENDPOINT_REQUIRED",
                    "nico_native requires a configured model endpoint revision",
                ),
            )
        model = endpoint.get("model") or request.model_config_data.get("model")
        if not isinstance(model, str) or not model:
            return (
                endpoint,
                None,
                await NativeAgentLoop._fail(
                    emit,
                    "MODEL_REQUIRED",
                    "nico_native requires a model name",
                ),
            )
        return endpoint, model, None

    @staticmethod
    async def _model_failure(
        emit: Emit,
        exc: ModelError,
        *,
        checkpoint: dict[str, Any] | None = None,
    ) -> RuntimeOutcome:
        error = {"code": exc.code, "message": exc.message}
        await emit(RuntimeEventType.RUN_FAILED, None, {"error": error})
        return RuntimeOutcome.terminal(
            status=RuntimeSessionStatus.FAILED,
            error=error,
            checkpoint=checkpoint,
        )

    @staticmethod
    async def _fail(
        emit: Emit,
        code: str,
        message: str,
        *,
        checkpoint: dict[str, Any] | None = None,
        usage: dict[str, Any] | None = None,
    ) -> RuntimeOutcome:
        error = {"code": code, "message": message}
        await emit(RuntimeEventType.RUN_FAILED, None, {"error": error})
        return RuntimeOutcome.terminal(
            status=RuntimeSessionStatus.FAILED,
            error=error,
            checkpoint=checkpoint,
            usage=usage,
        )

    @staticmethod
    async def _cancelled(emit: Emit, *, checkpoint: dict[str, Any] | None = None) -> RuntimeOutcome:
        error = {"code": "CANCELLED", "message": "runtime execution was cancelled"}
        await emit(RuntimeEventType.RUN_CANCELLED, None, {"error": error})
        return RuntimeOutcome.terminal(
            status=RuntimeSessionStatus.CANCELLED,
            error=error,
            checkpoint=checkpoint,
        )


# Compatibility name retained for callers from Goal G.
NativeDirectLoop = NativeAgentLoop


def _exact_tool_versions(tools: tuple[RuntimeToolSpec, ...]) -> dict[str, str]:
    versions: dict[str, str] = {}
    for tool in tools:
        if tool.name in versions and versions[tool.name] != tool.version:
            raise ValueError(f"multiple authorized versions exist for tool {tool.name}")
        versions[tool.name] = tool.version
    return versions


def _pending_actions(
    request: RuntimeSessionRequest,
    iteration: int,
    response: ModelResponse,
    versions: dict[str, str],
    *,
    coordination_enabled: bool = False,
    artifact_enabled: bool = False,
) -> tuple[dict[str, Any], ...]:
    actions = []
    for call in response.tool_calls:
        if call.name == "delegate_agent":
            if not coordination_enabled:
                raise ValueError("model requested native coordination while it is disabled")
            actions.append(
                {
                    "kind": "coordination",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "idempotency_key": f"react:{request.run_id}:{iteration}:{call.id}",
                }
            )
            continue
        if call.name == "store_artifact":
            if not artifact_enabled:
                raise ValueError("model requested Artifact storage while it is disabled")
            actions.append(
                {
                    "kind": "artifact",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "idempotency_key": f"react:{request.run_id}:{iteration}:{call.id}",
                }
            )
            continue
        version = versions.get(call.name)
        if version is None:
            raise ValueError(f"model requested an unauthorized tool: {call.name}")
        actions.append(
            {
                "kind": "tool",
                "call_id": call.id,
                "name": call.name,
                "version": version,
                "arguments": call.arguments,
                "idempotency_key": f"react:{request.run_id}:{iteration}:{call.id}",
            }
        )
    return tuple(actions)


def _coordination_definition() -> ModelToolDefinition:
    return ModelToolDefinition(
        name="delegate_agent",
        description=(
            "Create a bounded Child Agent Run for an independent subtask. Multiple calls in one "
            "response execute in parallel; the parent resumes only after all are terminal."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["target_agent_version_id", "objective", "budget"],
            "properties": {
                "target_agent_version_id": {"type": "string", "format": "uuid"},
                "objective": {"type": "string", "minLength": 1, "maxLength": 20000},
                "acceptance": {"type": "object"},
                "context_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 256,
                },
                "budget": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["token_limit"],
                    "properties": {
                        "token_limit": {"type": "integer", "minimum": 1},
                        "cost_limit_microunits": {"type": "integer", "minimum": 0},
                        "tool_call_limit": {"type": "integer", "minimum": 0},
                        "wall_time_seconds": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 604800,
                        },
                    },
                },
                "permission_restrictions": {"type": "object"},
                "execution_mode": {"type": "string", "enum": ["serial", "parallel"]},
            },
        },
    )


def _artifact_definition() -> ModelToolDefinition:
    return ModelToolDefinition(
        name="store_artifact",
        description=(
            "Store a bounded result Artifact in private content-addressed storage. Child Runs "
            "may explicitly share it with their direct parent."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "content_text"],
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 300},
                "content_type": {"type": "string", "maxLength": 200},
                "content_text": {"type": "string"},
                "artifact_type": {"type": "string", "maxLength": 64},
                "metadata": {"type": "object"},
                "share_with_parent": {"type": "boolean"},
            },
        },
    )


def _plan_pending_actions(
    request: RuntimeSessionRequest,
    revision: int,
    step_key: str,
    attempt: int,
    response: ModelResponse,
    versions: dict[str, str],
) -> tuple[dict[str, Any], ...]:
    actions = []
    for call in response.tool_calls:
        version = versions.get(call.name)
        if version is None:
            raise ValueError(f"model requested an unauthorized tool: {call.name}")
        actions.append(
            {
                "call_id": call.id,
                "name": call.name,
                "version": version,
                "arguments": call.arguments,
                "idempotency_key": (
                    f"plan:{request.run_id}:{revision}:{step_key}:{attempt}:{call.id}"
                ),
            }
        )
    return tuple(actions)


def _assistant_history(response: ModelResponse) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": response.text or None,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                },
            }
            for call in response.tool_calls
        ],
    }


def _tool_history(outcome: RuntimeToolOutcome) -> dict[str, Any]:
    payload = {
        "status": outcome.status,
        "output": outcome.output,
        "error": outcome.error,
        "tool_call_id": outcome.tool_call_id,
        "run_step_id": outcome.run_step_id,
        "cached": outcome.cached,
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    original_chars = len(serialized)
    if original_chars > 16_000:
        serialized = serialized[:16_000] + "…[TRUNCATED]"
    return {
        "role": "tool",
        "tool_call_id": outcome.call_id,
        "content": serialized,
        "source_ref": (
            f"tool_call:{outcome.tool_call_id}"
            if outcome.tool_call_id
            else f"tool_action:{outcome.call_id}"
        ),
        "truncation": {"original_chars": original_chars} if original_chars > 16_000 else {},
    }


def _coordination_history(waiting: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "delegation_id": waiting["delegation_id"],
        "child_run_id": waiting["child_run_id"],
        "status": message["status"],
        "result": message.get("result"),
        "error": message.get("error"),
        "artifact_refs": message.get("artifact_refs", []),
    }
    return {
        "role": "tool",
        "name": "delegate_agent",
        "tool_call_id": waiting["call_id"],
        "content": json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str),
    }


def _artifact_history(call_id: str, outcome: Any) -> dict[str, Any]:
    payload = outcome.model_dump(mode="json")
    return {
        "role": "tool",
        "name": "store_artifact",
        "tool_call_id": call_id,
        "content": json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str),
    }


def _tool_call(index: int, value: dict[str, str]) -> ModelToolCall:
    if not value["id"] or not value["name"]:
        raise ModelProtocolError(f"tool call {index} is missing id or function name")
    try:
        arguments = json.loads(value["arguments"] or "{}")
    except json.JSONDecodeError as exc:
        raise ModelProtocolError(f"tool call {index} arguments are not valid JSON") from exc
    if not isinstance(arguments, dict):
        raise ModelProtocolError(f"tool call {index} arguments must be a JSON object")
    return ModelToolCall(id=value["id"], name=value["name"], arguments=arguments)


def _next_call_key(iteration: int, recovery_state: dict[str, Any]) -> str:
    base = f"model:{iteration}"
    existing = {
        str(value) for value in recovery_state.get("model_call_keys", []) if isinstance(value, str)
    }
    if base not in existing:
        return base
    replay = 1
    while f"{base}:replay:{replay}" in existing:
        replay += 1
    return f"{base}:replay:{replay}"


def _next_named_call_key(base: str, recovery_state: dict[str, Any]) -> str:
    existing = {
        str(value) for value in recovery_state.get("model_call_keys", []) if isinstance(value, str)
    }
    if base not in existing:
        return base
    replay = 1
    while f"{base}:replay:{replay}" in existing:
        replay += 1
    return f"{base}:replay:{replay}"


def _web_checkpoint_state(
    checkpoint: ReactCheckpoint | PlanCheckpoint,
    **updates: Any,
) -> dict[str, Any]:
    state = {
        "observed_web_urls": checkpoint.observed_web_urls,
        "citation_repair_attempted": checkpoint.citation_repair_attempted,
        "citation_provisional_output": checkpoint.citation_provisional_output,
    }
    state.update(updates)
    return state


def _repaired_output(
    mode: str,
    provisional: dict[str, Any],
    response: ModelResponse,
) -> dict[str, Any] | None:
    if response.tool_calls or not response.text.strip():
        return None
    if mode == "react":
        return {"content": response.text.strip()}
    try:
        result = _parse_step_output(response.text)
    except ValueError:
        result = {}
    if not result:
        previous = provisional.get("result")
        result = dict(previous) if isinstance(previous, dict) else {}
        if isinstance(result.get("content"), str):
            result["content"] = response.text.strip()
        else:
            result["citation_note"] = response.text.strip()
    return {**provisional, "result": result}


def _with_citation_diagnostic(
    output: dict[str, Any],
    *,
    observed_count: int,
    repair_attempted: bool,
    reason: str,
) -> dict[str, Any]:
    existing = output.get("diagnostics")
    diagnostics = list(existing) if isinstance(existing, list) else []
    diagnostics.append(
        {
            "code": "WEB_CITATION_MISSING",
            "observed_source_count": observed_count,
            "repair_attempted": repair_attempted,
            "reason": reason,
        }
    )
    return {**output, "diagnostics": diagnostics}


async def _emit_citation_evaluation(
    emit: Emit,
    *,
    passed: bool,
    phase: str,
    observed_count: int,
    repair_attempted: bool,
    reason: str,
) -> None:
    await emit(
        RuntimeEventType.STEP_COMPLETED,
        None,
        {
            "step_key": f"citation-evaluation:{phase}",
            "step_type": "observation",
            "name": "web.citation_validation",
            "method": "deterministic",
            "verdict": "passed" if passed else "failed",
            "output": {
                "code": None if passed else "WEB_CITATION_MISSING",
                "phase": phase,
                "observed_source_count": observed_count,
                "repair_attempted": repair_attempted,
                "reason": reason,
            },
        },
    )


def _restore_plan(request: RuntimeSessionRequest, checkpoint: PlanCheckpoint) -> PlanDraft | None:
    if checkpoint.plan_revision == 0 or checkpoint.loop_state == "planning":
        return None
    planning = request.recovery_state.get("planning")
    if not isinstance(planning, dict):
        return None
    active = planning.get("active_plan")
    if not isinstance(active, dict) or active.get("revision") != checkpoint.plan_revision:
        return None
    try:
        plan = parse_plan(
            {"objective": active.get("objective"), "steps": active.get("steps")},
            max_steps=64,
        )
    except ValueError:
        return None
    if checkpoint.plan_content_hash and plan_content_hash(plan) != checkpoint.plan_content_hash:
        return None
    return plan


def _reconcile_plan_recovery(
    request: RuntimeSessionRequest, checkpoint: PlanCheckpoint
) -> tuple[PlanCheckpoint, PlanDraft | None]:
    """Advance a compact checkpoint from facts committed just before a crash."""
    planning = request.recovery_state.get("planning")
    if not isinstance(planning, dict):
        return checkpoint, None
    active = planning.get("active_plan")
    if not isinstance(active, dict):
        return checkpoint, None
    revision = active.get("revision")
    status = active.get("status")
    if type(revision) is not int or revision < checkpoint.plan_revision:
        return checkpoint, None
    if status not in {"active", "completed"}:
        return checkpoint, None
    try:
        plan = parse_plan(
            {"objective": active.get("objective"), "steps": active.get("steps")},
            max_steps=64,
        )
    except ValueError:
        return checkpoint, None
    content_hash = plan_content_hash(plan)
    if active.get("content_hash") != content_hash:
        return checkpoint, None

    raw_completed = active.get("completed_step_keys", [])
    completed = tuple(
        key
        for key in raw_completed
        if isinstance(key, str) and any(step.key == key for step in plan.steps)
    )
    reflection_count = active.get("reflection_count", 0)
    if type(reflection_count) is not int:
        reflection_count = 0
    facts_ahead = (
        revision > checkpoint.plan_revision
        or not set(completed).issubset(set(checkpoint.completed_step_keys))
        or reflection_count > checkpoint.reflection_count
    )
    if not facts_ahead:
        return checkpoint, plan
    latest_reflection = active.get("latest_reflection")
    recovery_decision = None
    recovery_instruction = checkpoint.recovery_instruction
    loop_state = "executing"
    current_step_key = checkpoint.current_step_key
    if (
        reflection_count > checkpoint.reflection_count
        and isinstance(latest_reflection, dict)
        and latest_reflection.get("decision") in {"retry", "replan", "fail"}
    ):
        recovery_decision = str(latest_reflection["decision"])
        recovery_instruction = str(
            latest_reflection.get("recovery_instruction")
            or latest_reflection.get("reason")
            or "Resume from persisted Reflection"
        )
        loop_state = "reflecting"

    recovered_usage = request.recovery_state.get("usage")
    usage = recovered_usage if isinstance(recovered_usage, dict) else checkpoint.usage
    context_version = request.recovery_state.get("last_context_version", 0)
    if type(context_version) is not int:
        context_version = 0
    reconciled = make_plan_checkpoint(
        manifest=request.execution_manifest,
        loop_state=loop_state,
        plan_revision=revision,
        plan_content_hash=content_hash,
        current_step_key=current_step_key,
        completed_step_keys=completed,
        latest_output=(
            active.get("latest_output")
            if isinstance(active.get("latest_output"), dict)
            else checkpoint.latest_output
        ),
        recovery_instruction=recovery_instruction,
        recovery_decision=recovery_decision,
        context_version=max(checkpoint.context_version, context_version),
        reflection_count=max(checkpoint.reflection_count, reflection_count),
        **_web_checkpoint_state(checkpoint),
        usage=usage,
    )
    return reconciled, plan


def _step_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "nico_plan_step_output",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["output"],
                "properties": {"output": {"type": "object"}},
            },
        },
    }


def _parse_step_output(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("step output is not valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"output"}:
        raise ValueError("step output must contain exactly one output field")
    output = payload["output"]
    if not isinstance(output, dict):
        raise ValueError("step output must be a JSON object")
    return output


def _replay_base(call_key: str) -> str | None:
    marker = ":replay:"
    return call_key.split(marker, 1)[0] if marker in call_key else None


def _budget(
    request: RuntimeSessionRequest,
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = request.budgets.get(name, default)
    if type(value) is not int:
        return default
    return min(maximum, max(minimum, value))


def _merge_usage(current: dict[str, Any], addition: dict[str, Any]) -> dict[str, Any]:
    result = dict(current)
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        left = current.get(key)
        right = addition.get(key)
        if isinstance(left, int) or isinstance(right, int):
            result[key] = (left if isinstance(left, int) else 0) + (
                right if isinstance(right, int) else 0
            )
    result["model_calls"] = int(current.get("model_calls") or 0) + 1
    result["cost_status"] = (
        "exact"
        if current.get("cost_status") in {None, "exact"} and addition.get("cost_status") == "exact"
        else "unknown"
    )
    return result


def _usage(usage: ModelUsage) -> dict[str, Any]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
    }


def _cost(endpoint: dict[str, Any], usage: ModelUsage) -> tuple[dict[str, Any], str]:
    pricing = endpoint.get("pricing")
    if not isinstance(pricing, dict):
        return {}, "unknown"
    input_rate = pricing.get("input_per_million")
    output_rate = pricing.get("output_per_million")
    if not isinstance(input_rate, (int, float)) or not isinstance(output_rate, (int, float)):
        return {}, "unknown"
    if usage.input_tokens is None or usage.output_tokens is None:
        return {}, "unknown"
    amount = (usage.input_tokens * input_rate + usage.output_tokens * output_rate) / 1_000_000
    return {"amount": round(amount, 12), "currency": pricing.get("currency", "USD")}, "exact"


def _hash_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()
