"""Tenant-safe durable Agent question, answer, and wake semantics."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select

from nico_agent.database import Database, RunClaim, TenantContext
from nico_agent.domain.errors import DomainConflict, ResourceNotFound
from nico_agent.domain.models import (
    AgentActionBatch,
    AgentActionRecord,
    AuditRecord,
    Event,
    Run,
    RuntimeSession,
    UserInputRequest,
)
from nico_agent.domain.states import (
    AgentActionBatchStatus,
    AgentActionStatus,
    RunStatus,
    UserInputRequestStatus,
    require_revision,
)
from nico_agent.runtime.contracts import RuntimeLoopState
from nico_agent.runtime.errors import RuntimeLeaseLost
from nico_agent.runtime.lifecycle import RunLifecycleAuthority
from nico_agent.user_inputs.contracts import (
    RuntimeUserInputIntent,
    RuntimeUserInputRequest,
    UserInputAnswer,
    UserInputRequestRead,
    validate_user_input_answer,
)

_TERMINAL_RUNS = {
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
    RunStatus.TIMED_OUT.value,
}


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class UserInputService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def request(
        self,
        claim: RunClaim,
        *,
        worker_id: str,
        intent: RuntimeUserInputIntent,
    ) -> RuntimeUserInputRequest:
        context = TenantContext(claim.tenant_id, f"worker:{worker_id}", uuid4())
        async with self.database.tenant_transaction(context) as session:
            run = await session.scalar(
                select(Run)
                .where(Run.tenant_id == claim.tenant_id, Run.id == claim.run_id)
                .with_for_update()
            )
            if not self._owns(run, claim, worker_id):
                raise RuntimeLeaseLost(str(claim.run_id))
            if run is None or RunStatus(run.status) not in {
                RunStatus.RUNNING,
                RunStatus.WAITING_FOR_USER_INPUT,
            }:
                raise DomainConflict(
                    "USER_INPUT_RUN_NOT_ACTIVE",
                    "user input can be requested only by an active Run",
                )
            runtime_session = await session.scalar(
                select(RuntimeSession)
                .where(
                    RuntimeSession.tenant_id == run.tenant_id,
                    RuntimeSession.run_id == run.id,
                )
                .with_for_update()
            )
            if runtime_session is None:
                raise ResourceNotFound("runtime_session", str(run.id))
            batch = await session.scalar(
                select(AgentActionBatch)
                .where(
                    AgentActionBatch.tenant_id == run.tenant_id,
                    AgentActionBatch.run_id == run.id,
                    AgentActionBatch.batch_key == intent.batch_key,
                )
                .with_for_update()
            )
            if batch is None:
                raise ResourceNotFound("agent_action_batch", intent.batch_key)
            action = await session.scalar(
                select(AgentActionRecord)
                .where(
                    AgentActionRecord.tenant_id == run.tenant_id,
                    AgentActionRecord.run_id == run.id,
                    AgentActionRecord.batch_id == batch.id,
                    AgentActionRecord.ordinal == intent.ordinal,
                    AgentActionRecord.action_id == intent.action_id,
                )
                .with_for_update()
            )
            if action is None:
                raise ResourceNotFound("agent_action", intent.action_id)
            if (
                action.kind != "ask_user"
                or action.status != AgentActionStatus.DISPATCHED.value
                or batch.dispatch_cursor != intent.ordinal
            ):
                raise DomainConflict(
                    "USER_INPUT_ACTION_INVALID",
                    "user input request does not match the active dispatched AskUserAction",
                )

            request_hash = _canonical_hash(
                {
                    "run_id": str(run.id),
                    "batch_key": intent.batch_key,
                    "action_id": intent.action_id,
                    "ordinal": intent.ordinal,
                    "question": intent.question,
                    "reason": intent.reason,
                    "input_schema": intent.input_schema,
                    "idempotency_key": intent.idempotency_key,
                }
            )
            existing = await session.scalar(
                select(UserInputRequest)
                .where(
                    UserInputRequest.tenant_id == run.tenant_id,
                    UserInputRequest.run_id == run.id,
                    UserInputRequest.agent_action_id == action.id,
                )
                .with_for_update()
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise DomainConflict(
                        "USER_INPUT_IDEMPOTENCY_CONFLICT",
                        "AskUserAction already has a different durable request",
                    )
                return self._runtime_view(existing)
            if RunStatus(run.status) is not RunStatus.RUNNING:
                raise DomainConflict(
                    "USER_INPUT_REQUEST_MISSING",
                    "waiting Run has no matching durable user input request",
                )

            now = datetime.now(UTC)
            wake_key = _canonical_hash(
                {"run_id": str(run.id), "agent_action_id": str(action.id), "kind": "user_input"}
            )
            request = UserInputRequest(
                tenant_id=run.tenant_id,
                run_id=run.id,
                runtime_session_id=runtime_session.id,
                action_batch_id=batch.id,
                agent_action_id=action.id,
                question=intent.question,
                reason=intent.reason,
                input_schema=intent.input_schema,
                redacted_projection={
                    "question": intent.question,
                    "reason": intent.reason,
                    "input_schema": intent.input_schema,
                },
                request_hash=request_hash,
                wake_key=wake_key,
                expires_at=now + timedelta(seconds=intent.ttl_seconds),
            )
            session.add(request)
            await session.flush()
            self._persist_checkpoint(run, runtime_session, intent.checkpoint)
            await RunLifecycleAuthority.transition(
                session,
                context,
                run,
                target=RunStatus.WAITING_FOR_USER_INPUT,
                reason="user_input_requested",
                metadata={
                    "request_id": str(request.id),
                    "agent_action_id": str(action.id),
                    "wake_key": wake_key,
                },
                loop_state=RuntimeLoopState.WAITING_FOR_USER_INPUT,
                lease_owner=worker_id,
                lease_token=claim.lease_token,
                event_type="RunWaitingForUserInput",
                action="user_inputs.request",
                clear_lease=False,
            )
            runtime_session.loop_state = RuntimeLoopState.WAITING_FOR_USER_INPUT.value
            runtime_session.provider_state = {
                **runtime_session.provider_state,
                "user_input_request": {
                    "request_id": str(request.id),
                    "revision": request.revision,
                    "wake_key": wake_key,
                },
            }
            runtime_session.revision += 1
            self._record(
                session,
                context,
                request,
                event_type="UserInputRequested",
                action="user_input.request",
            )
            await session.flush()
            return self._runtime_view(request)

    async def get(
        self,
        context: TenantContext,
        request_id: UUID,
    ) -> UserInputRequestRead:
        async with self.database.tenant_transaction(context) as session:
            run_id = await session.scalar(
                select(UserInputRequest.run_id).where(
                    UserInputRequest.tenant_id == context.tenant_id,
                    UserInputRequest.id == request_id,
                )
            )
            if run_id is None:
                raise ResourceNotFound("user_input_request", str(request_id))
            run = await session.scalar(
                select(Run)
                .where(Run.tenant_id == context.tenant_id, Run.id == run_id)
                .with_for_update()
            )
            request = await self._request(session, context, request_id)
            await self._expire_if_due(session, context, run, request)
            await session.flush()
            return self._public_view(request)

    async def list(
        self,
        context: TenantContext,
        *,
        run_id: UUID | None = None,
        status: UserInputRequestStatus | None = None,
        limit: int = 100,
    ) -> list[UserInputRequestRead]:
        async with self.database.tenant_transaction(context) as session:
            statement = select(UserInputRequest.id).where(
                UserInputRequest.tenant_id == context.tenant_id
            )
            if run_id is not None:
                statement = statement.where(UserInputRequest.run_id == run_id)
            request_ids = list(
                await session.scalars(
                    statement.order_by(
                        UserInputRequest.created_at.desc(),
                        UserInputRequest.id.desc(),
                    ).limit(limit)
                )
            )

        result: list[UserInputRequestRead] = []
        for request_id in request_ids:
            request = await self.get(context, request_id)
            if status is None or request.status == status.value:
                result.append(request)
        return result

    async def answer(
        self,
        context: TenantContext,
        request_id: UUID,
        command: UserInputAnswer,
        *,
        idempotency_key: str,
    ) -> UserInputRequestRead:
        answer_hash = _canonical_hash(command.answer)
        async with self.database.tenant_transaction(context) as session:
            run_id = await session.scalar(
                select(UserInputRequest.run_id).where(
                    UserInputRequest.tenant_id == context.tenant_id,
                    UserInputRequest.id == request_id,
                )
            )
            if run_id is None:
                raise ResourceNotFound("user_input_request", str(request_id))
            run = await session.scalar(
                select(Run)
                .where(Run.tenant_id == context.tenant_id, Run.id == run_id)
                .with_for_update()
            )
            if run is None:
                raise ResourceNotFound("run", str(run_id))
            request = await self._request(session, context, request_id)
            current = UserInputRequestStatus(request.status)
            if current is not UserInputRequestStatus.REQUESTED:
                if (
                    current is UserInputRequestStatus.ANSWERED
                    and request.answer_hash == answer_hash
                    and request.answer_idempotency_key == idempotency_key
                ):
                    return self._public_view(request)
                raise DomainConflict(
                    "USER_INPUT_ALREADY_RESOLVED",
                    "user input request already has a different terminal resolution",
                    details={"status": request.status},
                )
            if run.status in _TERMINAL_RUNS:
                await self._resolve_without_answer(
                    session,
                    context,
                    run,
                    request,
                    status=UserInputRequestStatus.CANCELLED,
                    actor=context.actor_id,
                    idempotency_key=f"run-terminal:{run.id}",
                )
                await session.flush()
                return self._public_view(request)
            if request.expires_at <= datetime.now(UTC):
                await self._resolve_without_answer(
                    session,
                    context,
                    run,
                    request,
                    status=UserInputRequestStatus.EXPIRED,
                    actor="system:user-input-expiry",
                    idempotency_key=f"expiry:{request.id}",
                )
                await session.flush()
                return self._public_view(request)

            require_revision(
                "user_input_request",
                expected=command.expected_revision,
                actual=request.revision,
            )
            validate_user_input_answer(request.input_schema, command.answer)
            batch, action = await self._action_boundary(session, run, request)
            if (
                action.status != AgentActionStatus.DISPATCHED.value
                or batch.dispatch_cursor != action.ordinal
            ):
                raise DomainConflict(
                    "USER_INPUT_ACTION_CONFLICT",
                    "AskUserAction is no longer the unresolved Action",
                )

            now = datetime.now(UTC)
            request.status = UserInputRequestStatus.ANSWERED.value
            request.answer_payload = command.answer
            request.answer_hash = answer_hash
            request.answer_ref = f"user_input:{request.id}:answer"
            request.answer_idempotency_key = idempotency_key
            request.answered_by = context.actor_id
            request.answered_at = now
            request.resolved_at = now
            request.revision += 1
            action.status = AgentActionStatus.SUCCEEDED.value
            action.outcome_ref = f"user_input:{request.id}"
            action.observation_ref = request.answer_ref
            action.completed_at = now
            action.revision += 1
            batch.dispatch_cursor = action.ordinal + 1
            batch.status = (
                AgentActionBatchStatus.COMPLETED.value
                if batch.dispatch_cursor == batch.action_count
                else AgentActionBatchStatus.DISPATCHING.value
            )
            if batch.status == AgentActionBatchStatus.COMPLETED.value:
                batch.completed_at = now
            batch.revision += 1

            if RunStatus(run.status) is RunStatus.WAITING_FOR_USER_INPUT:
                await RunLifecycleAuthority.transition(
                    session,
                    context,
                    run,
                    target=RunStatus.RUNNING,
                    reason="user_input_answered",
                    metadata={
                        "request_id": str(request.id),
                        "agent_action_id": str(action.id),
                        "answer_hash": answer_hash,
                    },
                    loop_state=RuntimeLoopState.OBSERVING,
                    event_type="RunWoken",
                    action="user_inputs.answer",
                )
            runtime_session = await session.scalar(
                select(RuntimeSession)
                .where(
                    RuntimeSession.tenant_id == run.tenant_id,
                    RuntimeSession.run_id == run.id,
                )
                .with_for_update()
            )
            if runtime_session is not None:
                runtime_session.status = "paused"
                runtime_session.provider_state = {
                    **runtime_session.provider_state,
                    "user_input_answer": {
                        "request_id": str(request.id),
                        "answer_hash": answer_hash,
                        "revision": request.revision,
                    },
                }
                runtime_session.revision += 1
            self._record(
                session,
                context,
                request,
                event_type="UserInputAnswered",
                action="user_input.answer",
            )
            await session.flush()
            return self._public_view(request)

    async def _expire_if_due(
        self,
        session,
        context: TenantContext,
        run: Run,
        request: UserInputRequest,
    ) -> None:
        if (
            request.status == UserInputRequestStatus.REQUESTED.value
            and request.expires_at <= datetime.now(UTC)
        ):
            await self._resolve_without_answer(
                session,
                context,
                run,
                request,
                status=UserInputRequestStatus.EXPIRED,
                actor="system:user-input-expiry",
                idempotency_key=f"expiry:{request.id}",
            )

    async def _resolve_without_answer(
        self,
        session,
        context: TenantContext,
        run: Run,
        request: UserInputRequest,
        *,
        status: UserInputRequestStatus,
        actor: str,
        idempotency_key: str,
    ) -> None:
        batch, action = await self._action_boundary(session, run, request)
        now = datetime.now(UTC)
        request.status = status.value
        request.answer_idempotency_key = idempotency_key
        request.answered_by = actor
        request.resolved_at = now
        request.revision += 1
        if action.status == AgentActionStatus.DISPATCHED.value:
            action.status = AgentActionStatus.BLOCKED.value
            action.outcome_ref = f"user_input:{request.id}"
            action.observation_ref = f"user_input:{request.id}:{status.value}"
            action.completed_at = now
            action.revision += 1
        if batch.status not in {
            AgentActionBatchStatus.COMPLETED.value,
            AgentActionBatchStatus.FAILED.value,
        }:
            batch.status = AgentActionBatchStatus.FAILED.value
            batch.completed_at = now
            batch.revision += 1
        if RunStatus(run.status) is RunStatus.WAITING_FOR_USER_INPUT:
            await RunLifecycleAuthority.transition(
                session,
                TenantContext(context.tenant_id, actor, context.correlation_id),
                run,
                target=RunStatus.RUNNING,
                reason=f"user_input_{status.value}",
                metadata={"request_id": str(request.id), "status": status.value},
                loop_state=RuntimeLoopState.OBSERVING,
                event_type="RunWoken",
                action=f"user_inputs.{status.value}",
            )
        self._record(
            session,
            TenantContext(context.tenant_id, actor, context.correlation_id),
            request,
            event_type={
                UserInputRequestStatus.EXPIRED: "UserInputExpired",
                UserInputRequestStatus.CANCELLED: "UserInputCancelled",
            }[status],
            action=f"user_input.{status.value}",
        )

    @staticmethod
    async def _request(session, context: TenantContext, request_id: UUID) -> UserInputRequest:
        request = await session.scalar(
            select(UserInputRequest)
            .where(
                UserInputRequest.tenant_id == context.tenant_id,
                UserInputRequest.id == request_id,
            )
            .with_for_update()
        )
        if request is None:
            raise ResourceNotFound("user_input_request", str(request_id))
        return request

    @staticmethod
    async def _action_boundary(
        session,
        run: Run,
        request: UserInputRequest,
    ) -> tuple[AgentActionBatch, AgentActionRecord]:
        batch = await session.scalar(
            select(AgentActionBatch)
            .where(
                AgentActionBatch.tenant_id == run.tenant_id,
                AgentActionBatch.run_id == run.id,
                AgentActionBatch.id == request.action_batch_id,
            )
            .with_for_update()
        )
        action = await session.scalar(
            select(AgentActionRecord)
            .where(
                AgentActionRecord.tenant_id == run.tenant_id,
                AgentActionRecord.run_id == run.id,
                AgentActionRecord.id == request.agent_action_id,
            )
            .with_for_update()
        )
        if batch is None or action is None:
            raise DomainConflict(
                "USER_INPUT_ACTION_MISSING",
                "user input request lost its AgentAction boundary",
            )
        return batch, action

    @staticmethod
    def _persist_checkpoint(
        run: Run,
        runtime_session: RuntimeSession,
        checkpoint: dict[str, Any],
    ) -> None:
        schema_version = checkpoint.get("schema_version")
        if not isinstance(schema_version, int) or schema_version < 1:
            raise ValueError("user input requires a versioned pre-action checkpoint")
        expected_hash = checkpoint.get("checkpoint_hash")
        if expected_hash is not None:
            hash_payload = dict(checkpoint)
            hash_payload.pop("checkpoint_hash", None)
            if not isinstance(expected_hash, str) or _canonical_hash(hash_payload) != expected_hash:
                raise ValueError("user input checkpoint failed its integrity check")
        runtime_session.checkpoint = checkpoint
        runtime_session.checkpoint_schema_version = schema_version
        runtime_session.checkpoint_revision += 1
        runtime_session.checkpoint_hash = expected_hash
        runtime_session.loop_state = RuntimeLoopState.WAITING_FOR_USER_INPUT.value
        runtime_session.revision += 1
        run.checkpoint = checkpoint
        run.checkpoint_schema_version = schema_version
        run.checkpoint_revision += 1
        run.checkpoint_hash = expected_hash
        run.revision += 1

    @staticmethod
    def _owns(run: Run | None, claim: RunClaim, worker_id: str) -> bool:
        return (
            run is not None
            and run.lease_owner == worker_id
            and run.lease_token == claim.lease_token
        )

    @staticmethod
    def _runtime_view(request: UserInputRequest) -> RuntimeUserInputRequest:
        return RuntimeUserInputRequest(
            id=request.id,
            status=request.status,
            revision=request.revision,
            wake_key=request.wake_key,
            expires_at=request.expires_at,
        )

    @staticmethod
    def _public_view(request: UserInputRequest) -> UserInputRequestRead:
        return UserInputRequestRead(
            id=request.id,
            run_id=request.run_id,
            agent_action_id=request.agent_action_id,
            question=request.question,
            reason=request.reason,
            input_schema=request.input_schema,
            status=request.status,
            answer_hash=request.answer_hash,
            answer_ref=request.answer_ref,
            expires_at=request.expires_at,
            answered_at=request.answered_at,
            revision=request.revision,
            created_at=request.created_at,
            updated_at=request.updated_at,
        )

    @staticmethod
    def _record(
        session,
        context: TenantContext,
        request: UserInputRequest,
        *,
        event_type: str,
        action: str,
    ) -> None:
        payload = {
            "request_id": str(request.id),
            "agent_action_id": str(request.agent_action_id),
            "status": request.status,
            "request_hash": request.request_hash,
            "answer_hash": request.answer_hash,
            "revision": request.revision,
            "expires_at": request.expires_at.isoformat(),
        }
        session.add(
            Event(
                tenant_id=request.tenant_id,
                event_type=event_type,
                aggregate_type="user_input_request",
                aggregate_id=request.id,
                run_id=request.run_id,
                actor_id=context.actor_id,
                payload=payload,
                correlation_id=context.correlation_id,
            )
        )
        session.add(
            AuditRecord(
                tenant_id=request.tenant_id,
                action=action,
                resource_type="user_input_request",
                resource_id=request.id,
                actor_id=context.actor_id,
                details=payload,
                correlation_id=context.correlation_id,
            )
        )


class RunUserInputHandler:
    """Lease-bound provider adapter for one durable AskUserAction."""

    def __init__(
        self,
        service: UserInputService,
        claim: RunClaim,
        *,
        worker_id: str,
    ) -> None:
        self.service = service
        self.claim = claim
        self.worker_id = worker_id

    async def request_user_input(
        self,
        intent: RuntimeUserInputIntent,
    ) -> RuntimeUserInputRequest:
        return await self.service.request(
            self.claim,
            worker_id=self.worker_id,
            intent=intent,
        )
