"""HTTP API for durable Conversation and Turn resources."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from nico_agent.api_schemas import RevisionCommand
from nico_agent.artifacts.minio import MinioArtifactStore
from nico_agent.conversations.attachments import ConversationAttachmentService
from nico_agent.conversations.contracts import (
    ConversationAttachmentRead,
    ConversationCompact,
    ConversationCompactAccepted,
    ConversationCreate,
    ConversationPatch,
    ConversationQueueRead,
    ConversationQueueResume,
    ConversationRead,
    ConversationTurnAccepted,
    ConversationTurnCreate,
    ConversationTurnRead,
    ConversationTurnRetry,
)
from nico_agent.conversations.service import ConversationService
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainError
from nico_agent.domain.states import ConversationStatus
from nico_agent.domain_api import get_tenant_context

router = APIRouter(prefix="/api/v1", tags=["conversations"])


def get_service(request: Request) -> ConversationService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the conversation database is unavailable")
    return ConversationService(
        database,
        approval_locked_risks=frozenset(request.app.state.settings.tool_approval_locked_risks),
    )


Service = Annotated[ConversationService, Depends(get_service)]
Context = Annotated[TenantContext, Depends(get_tenant_context)]


def get_attachment_service(request: Request) -> ConversationAttachmentService:
    database: Database | None = request.app.state.database
    if database is None:
        raise DomainError("DATABASE_UNAVAILABLE", "the conversation database is unavailable")
    settings = request.app.state.settings
    return ConversationAttachmentService(
        database,
        MinioArtifactStore(
            settings.minio_url,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            bucket=settings.minio_bucket,
        ),
        max_bytes=settings.artifact_max_bytes,
        ttl_seconds=settings.conversation_attachment_ttl_seconds,
        excerpt_chars=settings.conversation_attachment_excerpt_chars,
        max_count=settings.conversation_attachment_max_count,
        max_total_bytes=settings.conversation_attachment_max_total_bytes,
    )


AttachmentService = Annotated[ConversationAttachmentService, Depends(get_attachment_service)]


@router.post(
    "/conversations",
    response_model=ConversationRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    command: ConversationCreate,
    service: Service,
    context: Context,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ] = None,
):
    if idempotency_key is not None:
        command = command.model_copy(update={"idempotency_key": idempotency_key})
    return await service.create(context, command)


@router.get("/conversations", response_model=list[ConversationRead])
async def list_conversations(
    service: Service,
    context: Context,
    project_id: UUID | None = None,
    agent_id: UUID | None = None,
    status_filter: Annotated[ConversationStatus | None, Query(alias="status")] = None,
    mode: Literal["personal", "project"] | None = None,
    before: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    return await service.list(
        context,
        project_id=project_id,
        agent_id=agent_id,
        status=status_filter,
        mode=mode,
        before=before,
        limit=limit,
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationRead)
async def get_conversation(conversation_id: UUID, service: Service, context: Context):
    return await service.get(context, conversation_id)


@router.patch("/conversations/{conversation_id}", response_model=ConversationRead)
async def patch_conversation(
    conversation_id: UUID,
    command: ConversationPatch,
    service: Service,
    context: Context,
):
    return await service.patch(context, conversation_id, command)


@router.get(
    "/conversations/{conversation_id}/turns",
    response_model=list[ConversationTurnRead],
)
async def list_conversation_turns(
    conversation_id: UUID,
    service: Service,
    context: Context,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
):
    return await service.list_turns(
        context,
        conversation_id,
        after_sequence=after_sequence,
        limit=limit,
    )


@router.get(
    "/conversations/{conversation_id}/queue",
    response_model=ConversationQueueRead,
)
async def get_conversation_queue(
    conversation_id: UUID,
    service: Service,
    context: Context,
):
    return await service.get_queue(context, conversation_id)


@router.post(
    "/conversations/{conversation_id}/queue/resume",
    response_model=ConversationQueueRead,
)
async def resume_conversation_queue(
    conversation_id: UUID,
    command: ConversationQueueResume,
    service: Service,
    context: Context,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ] = None,
):
    if idempotency_key is not None:
        command = command.model_copy(update={"idempotency_key": idempotency_key})
    return await service.resume_queue(context, conversation_id, command)


@router.post(
    "/conversations/{conversation_id}/turns",
    response_model=ConversationTurnAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_conversation_turn(
    conversation_id: UUID,
    command: ConversationTurnCreate,
    service: Service,
    context: Context,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ] = None,
):
    if idempotency_key is not None:
        command = command.model_copy(update={"idempotency_key": idempotency_key})
    return await service.create_turn(context, conversation_id, command)


@router.post(
    "/conversations/{conversation_id}/compact",
    response_model=ConversationCompactAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def compact_conversation(
    conversation_id: UUID,
    command: ConversationCompact,
    service: Service,
    context: Context,
    idempotency_key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ] = None,
):
    if idempotency_key is not None:
        command = command.model_copy(update={"idempotency_key": idempotency_key})
    return await service.compact(context, conversation_id, command)


@router.post(
    "/conversations/{conversation_id}/attachments",
    response_model=ConversationAttachmentRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_conversation_attachment(
    conversation_id: UUID,
    request: Request,
    service: AttachmentService,
    context: Context,
    name: Annotated[str, Query(min_length=1, max_length=300)],
    content_type: Annotated[str, Query(min_length=1, max_length=200)],
    idempotency_key: Annotated[str, Query(min_length=1, max_length=200)],
):
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > service.max_bytes:
            raise DomainError("ATTACHMENT_TOO_LARGE", "attachment exceeds the configured limit")
        chunks.append(chunk)
    return await service.upload(
        context,
        conversation_id,
        name=name,
        content_type=content_type,
        idempotency_key=idempotency_key,
        data=b"".join(chunks),
    )


@router.get(
    "/conversations/{conversation_id}/attachments",
    response_model=list[ConversationAttachmentRead],
)
async def list_conversation_attachments(
    conversation_id: UUID,
    service: AttachmentService,
    context: Context,
):
    return await service.list(context, conversation_id)


@router.delete(
    "/conversations/{conversation_id}/attachments/{attachment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_conversation_attachment(
    conversation_id: UUID,
    attachment_id: UUID,
    service: AttachmentService,
    context: Context,
) -> Response:
    await service.delete(context, conversation_id, attachment_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/conversation-turns/{turn_id}", response_model=ConversationTurnRead)
async def get_conversation_turn(turn_id: UUID, service: Service, context: Context):
    return await service.get_turn(context, turn_id)


@router.post(
    "/conversation-turns/{turn_id}/cancel",
    response_model=ConversationTurnRead,
)
async def cancel_conversation_turn(
    turn_id: UUID,
    command: RevisionCommand,
    service: Service,
    context: Context,
):
    return await service.cancel_turn(
        context,
        turn_id,
        expected_run_revision=command.expected_revision,
    )


@router.post(
    "/conversation-turns/{turn_id}/retry",
    response_model=ConversationTurnAccepted,
    status_code=status.HTTP_201_CREATED,
)
async def retry_conversation_turn(
    turn_id: UUID,
    command: ConversationTurnRetry,
    service: Service,
    context: Context,
):
    return await service.retry_turn(context, turn_id, command)
