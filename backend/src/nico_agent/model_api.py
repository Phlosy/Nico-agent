"""Tenant-scoped model configuration and native runtime inspection API."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import AccessDenied, DomainError, ResourceNotFound
from nico_agent.domain.models import (
    AgentActionBatch,
    AgentActionRecord,
    AgentActionRepair,
    AuditRecord,
    ContextSnapshot,
    Event,
    ModelCall,
    ModelEndpoint,
    Run,
    RuntimeKnowledgeUsage,
)
from nico_agent.domain_api import get_tenant_context
from nico_agent.model_api_schemas import (
    AgentActionBatchRead,
    ContextSnapshotRead,
    ModelCallRead,
    ModelEndpointCreate,
    ModelEndpointPatch,
    ModelEndpointRead,
    RuntimeKnowledgeUsageRead,
)

router = APIRouter(prefix="/api/v1", tags=["model-runtime"])


class ModelRuntimeService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create_endpoint(
        self, context: TenantContext, command: ModelEndpointCreate
    ) -> ModelEndpoint:
        async with self.database.tenant_transaction(context) as session:
            lock_key = f"{context.tenant_id}:model-endpoint:{command.stable_key}"
            await session.execute(
                select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
            )
            latest = await session.scalar(
                select(func.max(ModelEndpoint.revision)).where(
                    ModelEndpoint.tenant_id == context.tenant_id,
                    ModelEndpoint.stable_key == command.stable_key,
                )
            )
            endpoint = ModelEndpoint(
                tenant_id=context.tenant_id,
                stable_key=command.stable_key,
                revision=(latest or 0) + 1,
                display_name=command.display_name,
                protocol="openai_compatible",
                base_url=command.base_url.rstrip("/"),
                credential_ref=command.credential_ref,
                allowed_models=command.allowed_models,
                capabilities=command.capabilities,
                rate_limit=command.rate_limit,
                tls_policy=command.tls_policy,
            )
            session.add(endpoint)
            await session.flush()
            session.add(
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action="model_endpoint.create",
                    resource_type="model_endpoint",
                    resource_id=endpoint.id,
                    actor_id=context.actor_id,
                    details={
                        "stable_key": endpoint.stable_key,
                        "revision": endpoint.revision,
                        "protocol": endpoint.protocol,
                    },
                    correlation_id=context.correlation_id,
                )
            )
            await session.flush()
            return endpoint

    async def list_endpoints(self, context: TenantContext) -> list[ModelEndpoint]:
        async with self.database.tenant_transaction(context) as session:
            return list(
                await session.scalars(
                    select(ModelEndpoint)
                    .where(ModelEndpoint.tenant_id == context.tenant_id)
                    .order_by(ModelEndpoint.stable_key, ModelEndpoint.revision)
                )
            )

    async def patch_endpoint(
        self,
        context: TenantContext,
        endpoint_id: UUID,
        command: ModelEndpointPatch,
    ) -> ModelEndpoint:
        async with self.database.tenant_transaction(context) as session:
            endpoint = await session.scalar(
                select(ModelEndpoint)
                .where(
                    ModelEndpoint.tenant_id == context.tenant_id,
                    ModelEndpoint.id == endpoint_id,
                )
                .with_for_update()
            )
            if endpoint is None:
                raise ResourceNotFound("model_endpoint", str(endpoint_id))
            if command.enabled is not None:
                endpoint.enabled = command.enabled
            if command.status is not None:
                endpoint.status = command.status
            if command.credential_ref is not None:
                endpoint.credential_ref = command.credential_ref
            if command.rate_limit is not None:
                endpoint.rate_limit = command.rate_limit
            session.add(
                AuditRecord(
                    tenant_id=context.tenant_id,
                    action="model_endpoint.update_operational",
                    resource_type="model_endpoint",
                    resource_id=endpoint.id,
                    actor_id=context.actor_id,
                    details={"enabled": endpoint.enabled, "status": endpoint.status},
                    correlation_id=context.correlation_id,
                )
            )
            await session.flush()
            return endpoint

    async def list_model_calls(self, context: TenantContext, run_id: UUID) -> list[ModelCall]:
        async with self.database.tenant_transaction(context) as session:
            await self._require_run(session, context, run_id)
            return list(
                await session.scalars(
                    select(ModelCall)
                    .where(
                        ModelCall.tenant_id == context.tenant_id,
                        ModelCall.run_id == run_id,
                    )
                    .order_by(ModelCall.created_at, ModelCall.id)
                )
            )

    async def list_agent_actions(
        self,
        context: TenantContext,
        run_id: UUID,
    ) -> list[dict]:
        async with self.database.tenant_transaction(context) as session:
            await self._require_run(session, context, run_id)
            batches = list(
                await session.scalars(
                    select(AgentActionBatch)
                    .where(
                        AgentActionBatch.tenant_id == context.tenant_id,
                        AgentActionBatch.run_id == run_id,
                    )
                    .order_by(AgentActionBatch.created_at, AgentActionBatch.id)
                )
            )
            if not batches:
                return []
            batch_ids = [batch.id for batch in batches]
            actions = list(
                await session.scalars(
                    select(AgentActionRecord)
                    .where(
                        AgentActionRecord.tenant_id == context.tenant_id,
                        AgentActionRecord.run_id == run_id,
                        AgentActionRecord.batch_id.in_(batch_ids),
                    )
                    .order_by(AgentActionRecord.batch_id, AgentActionRecord.ordinal)
                )
            )
            repairs = list(
                await session.scalars(
                    select(AgentActionRepair)
                    .where(
                        AgentActionRepair.tenant_id == context.tenant_id,
                        AgentActionRepair.run_id == run_id,
                        AgentActionRepair.result_batch_id.in_(batch_ids),
                    )
                    .order_by(
                        AgentActionRepair.result_batch_id,
                        AgentActionRepair.repair_ordinal,
                    )
                )
            )
            actions_by_batch: dict[UUID, list[dict]] = {batch.id: [] for batch in batches}
            for action in actions:
                actions_by_batch[action.batch_id].append(
                    {
                        "id": action.id,
                        "ordinal": action.ordinal,
                        "action_id": action.action_id,
                        "kind": action.kind,
                        "provider_call_id": action.provider_call_id,
                        "tool_name": action.tool_name,
                        "intent": action.intent_redacted,
                        "completion": action.completion,
                        "content_hash": action.content_hash,
                        "arguments_hash": action.arguments_hash,
                        "question_hash": action.question_hash,
                        "reason_hash": action.reason_hash,
                        "compatibility_mode": action.compatibility_mode,
                        "status": action.status,
                        "outcome_ref": action.outcome_ref,
                        "observation_ref": action.observation_ref,
                        "created_at": action.created_at,
                    }
                )
            repairs_by_batch: dict[UUID, list[AgentActionRepair]] = {
                batch.id: [] for batch in batches
            }
            for repair in repairs:
                repairs_by_batch[repair.result_batch_id].append(repair)
            return [
                {
                    "id": batch.id,
                    "run_id": batch.run_id,
                    "runtime_session_id": batch.runtime_session_id,
                    "context_snapshot_id": batch.context_snapshot_id,
                    "model_call_id": batch.model_call_id,
                    "run_step_id": batch.run_step_id,
                    "replay_of_batch_id": batch.replay_of_batch_id,
                    "schema_version": batch.schema_version,
                    "parse_revision": batch.parse_revision,
                    "batch_key": batch.batch_key,
                    "source_format": batch.source_format,
                    "response_hash": batch.response_hash,
                    "action_count": batch.action_count,
                    "dispatch_cursor": batch.dispatch_cursor,
                    "status": batch.status,
                    "actions": actions_by_batch[batch.id],
                    "repairs": repairs_by_batch[batch.id],
                    "created_at": batch.created_at,
                    "updated_at": batch.updated_at,
                }
                for batch in batches
            ]

    async def list_contexts(self, context: TenantContext, run_id: UUID) -> list[ContextSnapshot]:
        async with self.database.tenant_transaction(context) as session:
            await self._require_run(session, context, run_id)
            return list(
                await session.scalars(
                    select(ContextSnapshot)
                    .where(
                        ContextSnapshot.tenant_id == context.tenant_id,
                        ContextSnapshot.run_id == run_id,
                    )
                    .order_by(ContextSnapshot.version)
                )
            )

    async def list_knowledge_usages(
        self, context: TenantContext, run_id: UUID
    ) -> list[RuntimeKnowledgeUsage]:
        async with self.database.tenant_transaction(context) as session:
            await self._require_run(session, context, run_id)
            return list(
                await session.scalars(
                    select(RuntimeKnowledgeUsage)
                    .where(
                        RuntimeKnowledgeUsage.tenant_id == context.tenant_id,
                        RuntimeKnowledgeUsage.run_id == run_id,
                    )
                    .order_by(
                        RuntimeKnowledgeUsage.source_type,
                        RuntimeKnowledgeUsage.created_at,
                        RuntimeKnowledgeUsage.id,
                    )
                )
            )

    async def events_after(
        self, context: TenantContext, run_id: UUID, sequence: int
    ) -> tuple[list[Event], bool]:
        async with self.database.tenant_transaction(context) as session:
            run = await self._require_run(session, context, run_id)
            events = list(
                await session.scalars(
                    select(Event)
                    .where(
                        Event.tenant_id == context.tenant_id,
                        Event.run_id == run_id,
                        Event.sequence > sequence,
                    )
                    .order_by(Event.sequence)
                    .limit(500)
                )
            )
            terminal = run.status in {"completed", "failed", "cancelled", "timed_out"}
            return events, terminal

    @staticmethod
    async def _require_run(session, context: TenantContext, run_id: UUID) -> Run:
        run = await session.scalar(
            select(Run).where(Run.tenant_id == context.tenant_id, Run.id == run_id)
        )
        if run is None:
            raise ResourceNotFound("run", str(run_id))
        return run


def get_model_service(request: Request) -> ModelRuntimeService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the model runtime database is unavailable")
    return ModelRuntimeService(database)


Service = Annotated[ModelRuntimeService, Depends(get_model_service)]
Context = Annotated[TenantContext, Depends(get_tenant_context)]


def _require_endpoint_writes(request: Request) -> None:
    if not request.app.state.settings.model_endpoint_writes_enabled:
        raise AccessDenied(
            "MODEL_ENDPOINT_WRITES_DISABLED",
            "model endpoint writes are disabled by deployment policy",
        )


@router.post(
    "/model-endpoints",
    response_model=ModelEndpointRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_model_endpoint(
    request: Request,
    command: ModelEndpointCreate,
    service: Service,
    context: Context,
):
    _require_endpoint_writes(request)
    return await service.create_endpoint(context, command)


@router.get("/model-endpoints", response_model=list[ModelEndpointRead])
async def list_model_endpoints(service: Service, context: Context):
    return await service.list_endpoints(context)


@router.patch("/model-endpoints/{endpoint_id}", response_model=ModelEndpointRead)
async def patch_model_endpoint(
    request: Request,
    endpoint_id: UUID,
    command: ModelEndpointPatch,
    service: Service,
    context: Context,
):
    _require_endpoint_writes(request)
    return await service.patch_endpoint(context, endpoint_id, command)


@router.get("/runs/{run_id}/model-calls", response_model=list[ModelCallRead])
async def list_model_calls(run_id: UUID, service: Service, context: Context):
    return await service.list_model_calls(context, run_id)


@router.get("/runs/{run_id}/agent-actions", response_model=list[AgentActionBatchRead])
async def list_agent_actions(run_id: UUID, service: Service, context: Context):
    return await service.list_agent_actions(context, run_id)


@router.get("/runs/{run_id}/contexts", response_model=list[ContextSnapshotRead])
async def list_context_snapshots(run_id: UUID, service: Service, context: Context):
    return await service.list_contexts(context, run_id)


@router.get(
    "/runs/{run_id}/knowledge-usages",
    response_model=list[RuntimeKnowledgeUsageRead],
)
async def list_runtime_knowledge_usages(run_id: UUID, service: Service, context: Context):
    return await service.list_knowledge_usages(context, run_id)


@router.get("/runs/{run_id}/events/stream")
async def stream_run_events(
    run_id: UUID,
    service: Service,
    context: Context,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    try:
        cursor = max(0, int(last_event_id or "0"))
    except ValueError as exc:
        raise DomainError("INVALID_EVENT_CURSOR", "Last-Event-ID must be an integer") from exc
    initial_events, initial_terminal = await service.events_after(context, run_id, cursor)

    async def stream():
        nonlocal cursor
        events, terminal = initial_events, initial_terminal
        while True:
            for event in events:
                cursor = event.sequence
                payload = {
                    "id": str(event.id),
                    "sequence": event.sequence,
                    "type": event.event_type,
                    "payload": event.payload,
                    "created_at": event.created_at.isoformat(),
                }
                data = json.dumps(payload, default=str)
                yield f"id: {event.sequence}\nevent: {event.event_type}\ndata: {data}\n\n"
            if terminal and not events:
                return
            if not events:
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)
            events, terminal = await service.events_after(context, run_id, cursor)

    return StreamingResponse(stream(), media_type="text/event-stream")
