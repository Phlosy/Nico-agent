"""Build bounded, redacted, hash-stable DTOs from authoritative terminal records."""

from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent import __version__
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainConflict, ResourceNotFound
from nico_agent.domain.models import (
    AgentVersion,
    Run,
    RunStep,
    RuntimeKnowledgeUsage,
    RuntimeSession,
    Task,
    ToolCall,
)
from nico_agent.growth.contracts import (
    SnapshotKnowledgeUsage,
    SnapshotRuntime,
    SnapshotStep,
    SnapshotToolCall,
    TrajectorySnapshot,
    canonical_hash,
    canonical_json,
)
from nico_agent.tools.secrets import redact_value

_TERMINAL_RUNS = {"completed", "failed", "cancelled", "timed_out"}
_TERMINAL_STEPS = {"completed", "failed", "cancelled"}
_TERMINAL_TOOL_CALLS = {"succeeded", "failed", "timed_out", "cancelled"}
_TERMINAL_RUNTIME = {"completed", "failed", "cancelled"}
_MAX_SNAPSHOT_BYTES = 512_000


class TrajectorySnapshotBuilder:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def build(self, context: TenantContext, run_id: UUID) -> TrajectorySnapshot:
        async with self.database.tenant_transaction(context) as session:
            return await self.build_in_session(session, context, run_id)

    async def build_in_session(
        self,
        session: AsyncSession,
        context: TenantContext,
        run_id: UUID,
        *,
        for_update: bool = False,
    ) -> TrajectorySnapshot:
        run_statement = select(Run).where(Run.id == run_id)
        if for_update:
            run_statement = run_statement.with_for_update()
        run = await session.scalar(run_statement)
        if run is None:
            raise ResourceNotFound("run", str(run_id))
        if run.status not in _TERMINAL_RUNS:
            raise DomainConflict(
                "RUN_NOT_TERMINAL",
                "growth candidates can only be generated from a terminal run",
                details={"run_id": str(run_id), "status": run.status},
            )
        if run.ended_at is None:
            raise DomainConflict(
                "TRAJECTORY_INCOMPLETE",
                "terminal run is missing ended_at",
                details={"run_id": str(run_id)},
            )

        task = await session.scalar(select(Task).where(Task.id == run.task_id))
        version = await session.scalar(
            select(AgentVersion).where(AgentVersion.id == run.agent_version_id)
        )
        if task is None or version is None:
            raise DomainConflict(
                "TRAJECTORY_INCOMPLETE",
                "terminal run is missing its task or agent version",
                details={"run_id": str(run_id)},
            )

        steps = list(
            await session.scalars(
                select(RunStep)
                .where(RunStep.run_id == run.id)
                .order_by(RunStep.sequence, RunStep.id)
            )
        )
        if not steps or any(step.status not in _TERMINAL_STEPS for step in steps):
            raise DomainConflict(
                "TRAJECTORY_INCOMPLETE",
                "growth trajectory requires at least one terminal run step",
                details={"run_id": str(run_id)},
            )
        tool_calls = list(
            await session.scalars(
                select(ToolCall)
                .where(ToolCall.run_id == run.id)
                .order_by(ToolCall.created_at, ToolCall.id)
            )
        )
        if any(call.status not in _TERMINAL_TOOL_CALLS for call in tool_calls):
            raise DomainConflict(
                "TRAJECTORY_INCOMPLETE",
                "growth trajectory contains a non-terminal tool call",
                details={"run_id": str(run_id)},
            )
        calls_by_step: dict[UUID, list[ToolCall]] = defaultdict(list)
        for call in tool_calls:
            calls_by_step[call.run_step_id].append(call)

        runtime_session = await session.scalar(
            select(RuntimeSession).where(RuntimeSession.run_id == run.id)
        )
        runtime: SnapshotRuntime | None = None
        if runtime_session is not None:
            if (
                runtime_session.status not in _TERMINAL_RUNTIME
                or runtime_session.trajectory is None
            ):
                raise DomainConflict(
                    "TRAJECTORY_INCOMPLETE",
                    "persisted runtime session must be terminal and contain its trajectory",
                    details={"run_id": str(run_id)},
                )
            runtime = SnapshotRuntime(
                id=runtime_session.id,
                provider=runtime_session.provider_name,
                provider_version=runtime_session.provider_version,
                protocol_version=runtime_session.protocol_version,
                status=runtime_session.status,
                usage=_bounded_redact(runtime_session.usage),
                trajectory=_bounded_redact(runtime_session.trajectory),
            )

        knowledge_rows = list(
            await session.scalars(
                select(RuntimeKnowledgeUsage)
                .where(RuntimeKnowledgeUsage.run_id == run.id)
                .order_by(
                    RuntimeKnowledgeUsage.source_type,
                    RuntimeKnowledgeUsage.created_at,
                    RuntimeKnowledgeUsage.id,
                )
            )
        )
        knowledge_usages = tuple(
            SnapshotKnowledgeUsage(
                id=item.id,
                source_type=item.source_type,
                memory_id=item.memory_id,
                skill_id=item.skill_id,
                skill_version_id=item.skill_version_id,
                source_version=item.source_version,
                content_hash=item.content_hash,
                scope_type=item.scope_type,
                status=item.status,
                first_context_snapshot_id=item.first_context_snapshot_id,
                first_model_call_id=item.first_model_call_id,
                context_count=item.context_count,
                model_call_count=item.model_call_count,
                outcome_status=item.outcome_status,
                result_hash=item.result_hash,
                selection=_bounded_redact(item.selection),
                effect_metadata=_bounded_redact(item.effect_metadata),
            )
            for item in knowledge_rows
        )

        snapshot_steps = tuple(
            SnapshotStep(
                id=step.id,
                sequence=step.sequence,
                kind=step.kind,
                status=step.status,
                input=_bounded_redact(step.input),
                output=_bounded_redact(step.output),
                error=_bounded_redact(step.error),
                tool_calls=tuple(
                    SnapshotToolCall(
                        id=call.id,
                        tool_definition_id=call.tool_definition_id,
                        name=call.tool_name,
                        version=call.tool_version,
                        status=call.status,
                        arguments=_bounded_redact(call.arguments),
                        result=_bounded_redact(call.result),
                        error=_bounded_redact(call.error),
                        usage=_bounded_redact(call.usage),
                    )
                    for call in calls_by_step[step.id]
                ),
            )
            for step in steps
        )
        payload = {
            "tenant_id": context.tenant_id,
            "run_id": run.id,
            "task_id": task.id,
            "project_id": task.project_id,
            "agent_id": run.agent_id,
            "agent_version_id": version.id,
            "agent_version_content_hash": version.content_hash,
            "platform_version": __version__,
            "run_status": run.status,
            "attempt": run.attempt,
            "task_title": _bounded_redact(task.title),
            "task_input": _bounded_redact(task.input),
            "acceptance": _bounded_redact(task.acceptance),
            "role": _bounded_redact(version.role),
            "mandate": _bounded_redact(version.mandate),
            "model_config_data": _bounded_redact(version.model_config_json),
            "run_result": _bounded_redact(run.result),
            "run_error": _bounded_redact(run.error),
            "usage": _bounded_redact(run.cost),
            "ended_at": run.ended_at,
            "steps": snapshot_steps,
            "runtime": runtime,
        }
        if knowledge_usages:
            payload["knowledge_usages"] = knowledge_usages
        snapshot = TrajectorySnapshot(**payload, snapshot_hash=canonical_hash(payload))
        if len(canonical_json(snapshot).encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
            raise DomainConflict(
                "TRAJECTORY_TOO_LARGE",
                "bounded trajectory snapshot exceeds the persistence limit",
                details={"run_id": str(run_id), "max_bytes": _MAX_SNAPSHOT_BYTES},
            )
        return snapshot


def _bounded_redact(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if depth >= 8:
        return "[TRUNCATED]"
    if isinstance(value, str):
        redacted = redact_value(value)
        return redacted if len(redacted) <= 8_000 else redacted[:8_000] + "[TRUNCATED]"
    if isinstance(value, (list, tuple)):
        items = value[:200]
        result = [_bounded_redact(item, depth=depth + 1) for item in items]
        if len(value) > len(items):
            result.append("[TRUNCATED]")
        return result
    if isinstance(value, dict):
        keys = sorted(value, key=str)[:200]
        result: dict[str, Any] = {}
        for key in keys:
            string_key = str(key)
            marker = redact_value({string_key: None})[string_key]
            result[string_key] = (
                marker if marker == "[REDACTED]" else _bounded_redact(value[key], depth=depth + 1)
            )
        if len(value) > len(keys):
            result["__truncated__"] = True
        return result
    return _bounded_redact(str(value), depth=depth + 1)
