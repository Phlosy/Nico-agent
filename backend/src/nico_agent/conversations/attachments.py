"""Private, short-lived Conversation attachment staging."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, text

from nico_agent.artifacts.minio import MinioArtifactStore
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import DomainConflict, ResourceNotFound
from nico_agent.domain.models import Conversation, ConversationAttachment

_TEXT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
)


class ConversationAttachmentService:
    def __init__(
        self,
        database: Database,
        store: MinioArtifactStore,
        *,
        max_bytes: int,
        ttl_seconds: int,
        excerpt_chars: int,
        max_count: int,
        max_total_bytes: int,
    ) -> None:
        self.database = database
        self.store = store
        self.max_bytes = max_bytes
        self.ttl_seconds = ttl_seconds
        self.excerpt_chars = excerpt_chars
        self.max_count = max_count
        self.max_total_bytes = max_total_bytes

    async def upload(
        self,
        context: TenantContext,
        conversation_id: UUID,
        *,
        name: str,
        content_type: str,
        idempotency_key: str,
        data: bytes,
    ) -> ConversationAttachment:
        if len(data) > self.max_bytes:
            raise DomainConflict("ATTACHMENT_TOO_LARGE", "attachment exceeds the configured limit")
        digest = hashlib.sha256(data).hexdigest()
        object_key = f"tenants/{context.tenant_id}/sha256/{digest[:2]}/{digest}"
        attachment_id = uuid4()
        temp_key = f"tenants/{context.tenant_id}/tmp/conversation-attachment/{attachment_id}"
        lock_key = (
            f"conversation-attachment:{context.tenant_id}:{conversation_id}:{idempotency_key}"
        )

        async with self.database.tenant_transaction(context) as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": lock_key},
            )
            conversation = await session.scalar(
                select(Conversation).where(
                    Conversation.tenant_id == context.tenant_id,
                    Conversation.id == conversation_id,
                )
            )
            if conversation is None:
                raise ResourceNotFound("conversation", str(conversation_id))
            if conversation.status != "active":
                raise DomainConflict(
                    "CONVERSATION_ARCHIVED", "cannot attach to an archived conversation"
                )
            existing = await session.scalar(
                select(ConversationAttachment).where(
                    ConversationAttachment.tenant_id == context.tenant_id,
                    ConversationAttachment.conversation_id == conversation_id,
                    ConversationAttachment.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if (existing.name, existing.content_type, existing.sha256, existing.size_bytes) != (
                    name,
                    content_type,
                    digest,
                    len(data),
                ):
                    raise DomainConflict(
                        "IDEMPOTENCY_KEY_REUSED",
                        "attachment idempotency key was reused with different input",
                    )
                return existing
            now = datetime.now(UTC)
            staged = list(
                await session.scalars(
                    select(ConversationAttachment)
                    .where(
                        ConversationAttachment.tenant_id == context.tenant_id,
                        ConversationAttachment.conversation_id == conversation_id,
                        ConversationAttachment.status == "staged",
                    )
                    .with_for_update()
                )
            )
            for item in staged:
                if item.expires_at <= now:
                    item.status = "expired"
                    item.revision += 1
            active = [item for item in staged if item.status == "staged"]
            if len(active) >= self.max_count:
                raise DomainConflict(
                    "ATTACHMENT_COUNT_EXCEEDED",
                    "conversation has reached the staged attachment count limit",
                )
            if sum(item.size_bytes for item in active) + len(data) > self.max_total_bytes:
                raise DomainConflict(
                    "ATTACHMENT_TOTAL_TOO_LARGE",
                    "staged attachments exceed the configured cumulative limit",
                )
            # Keep the advisory transaction lock through object finalization so two
            # replays cannot race into distinct staged rows.
            await self.store.put_temp(temp_key, data, content_type)
            await self.store.promote(temp_key, object_key)
            excerpt = self._excerpt(data, content_type)
            attachment = ConversationAttachment(
                id=attachment_id,
                tenant_id=context.tenant_id,
                conversation_id=conversation_id,
                name=name,
                content_type=content_type,
                object_key=object_key,
                sha256=digest,
                size_bytes=len(data),
                uploaded_by=context.actor_id,
                expires_at=now + timedelta(seconds=self.ttl_seconds),
                text_excerpt=excerpt,
                idempotency_key=idempotency_key,
            )
            session.add(attachment)
            await session.flush()
            return attachment

    async def list(
        self, context: TenantContext, conversation_id: UUID
    ) -> list[ConversationAttachment]:
        async with self.database.tenant_transaction(context) as session:
            conversation = await session.scalar(
                select(Conversation.id).where(
                    Conversation.tenant_id == context.tenant_id,
                    Conversation.id == conversation_id,
                )
            )
            if conversation is None:
                raise ResourceNotFound("conversation", str(conversation_id))
            rows = list(
                await session.scalars(
                    select(ConversationAttachment)
                    .where(
                        ConversationAttachment.tenant_id == context.tenant_id,
                        ConversationAttachment.conversation_id == conversation_id,
                        ConversationAttachment.status.in_(("staged", "consumed")),
                    )
                    .order_by(ConversationAttachment.created_at, ConversationAttachment.id)
                )
            )
            now = datetime.now(UTC)
            for item in rows:
                if item.status == "staged" and item.expires_at <= now:
                    item.status = "expired"
                    item.revision += 1
            await session.flush()
            return [item for item in rows if item.status != "expired"]

    async def delete(
        self, context: TenantContext, conversation_id: UUID, attachment_id: UUID
    ) -> None:
        async with self.database.tenant_transaction(context) as session:
            item = await session.scalar(
                select(ConversationAttachment)
                .where(
                    ConversationAttachment.tenant_id == context.tenant_id,
                    ConversationAttachment.conversation_id == conversation_id,
                    ConversationAttachment.id == attachment_id,
                )
                .with_for_update()
            )
            if item is None:
                raise ResourceNotFound("conversation_attachment", str(attachment_id))
            if item.status != "staged":
                raise DomainConflict(
                    "ATTACHMENT_NOT_STAGED", "only an unconsumed staged attachment can be deleted"
                )
            item.status = "deleted"
            item.revision += 1
            await session.flush()

    def _excerpt(self, data: bytes, content_type: str) -> str | None:
        if not any(content_type.lower().startswith(value) for value in _TEXT_TYPES):
            return None
        return data.decode("utf-8", errors="replace")[: self.excerpt_chars]
