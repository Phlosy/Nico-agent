"""PostgreSQL-authoritative Artifact metadata with private MinIO object bytes."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import or_, select, text

from nico_agent.artifacts.contracts import RuntimeArtifactIntent, RuntimeArtifactOutcome
from nico_agent.artifacts.minio import MinioArtifactStore
from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import AccessDenied, DomainConflict, ResourceNotFound
from nico_agent.domain.models import (
    AgentRunRelation,
    Artifact,
    AuditRecord,
    Event,
    Run,
    SharedArtifactLink,
    Task,
)


class ArtifactService:
    def __init__(
        self,
        database: Database,
        store: MinioArtifactStore,
        *,
        max_bytes: int,
    ) -> None:
        self.database = database
        self.store = store
        self.max_bytes = max_bytes

    async def store_runtime_artifact(
        self,
        context: TenantContext,
        owner_run_id: UUID,
        intent: RuntimeArtifactIntent,
    ) -> RuntimeArtifactOutcome:
        data = intent.content_bytes()
        if len(data) > self.max_bytes:
            raise DomainConflict("ARTIFACT_TOO_LARGE", "artifact exceeds the configured size limit")
        artifact_id = uuid4()
        temp_key = f"tenants/{context.tenant_id}/tmp/{artifact_id}"
        replay_artifact_id: UUID | None = None
        async with self.database.tenant_transaction(context) as session:
            lock_key = f"artifact:{context.tenant_id}:{owner_run_id}:{intent.idempotency_key}"
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )
            existing = await session.scalar(
                select(Artifact).where(
                    Artifact.tenant_id == context.tenant_id,
                    Artifact.owner_run_id == owner_run_id,
                    Artifact.idempotency_key == intent.idempotency_key,
                )
            )
            if existing is not None:
                if existing.status == "available":
                    return await self._outcome_locked(session, context, existing)
                if existing.status == "failed":
                    raise DomainConflict(
                        "ARTIFACT_UPLOAD_FAILED",
                        "the idempotent Artifact upload previously failed",
                    )
                replay_artifact_id = existing.id
            else:
                run = await session.scalar(
                    select(Run).where(Run.tenant_id == context.tenant_id, Run.id == owner_run_id)
                )
                if run is None:
                    raise ResourceNotFound("run", str(owner_run_id))
                task = await session.scalar(
                    select(Task).where(Task.tenant_id == context.tenant_id, Task.id == run.task_id)
                )
                if task is None:
                    raise ResourceNotFound("task", str(run.task_id))
                artifact = Artifact(
                    id=artifact_id,
                    tenant_id=context.tenant_id,
                    project_id=task.project_id,
                    owner_run_id=run.id,
                    name=intent.name,
                    content_type=intent.content_type,
                    artifact_type=intent.artifact_type,
                    temp_object_key=temp_key,
                    metadata_json=intent.metadata,
                    idempotency_key=intent.idempotency_key,
                )
                session.add(artifact)
                await session.flush()

        if replay_artifact_id is not None:
            return await self._wait_for_replay(context, replay_artifact_id)

        digest = hashlib.sha256(data).hexdigest()
        final_key = f"tenants/{context.tenant_id}/sha256/{digest[:2]}/{digest}"
        try:
            await self.store.put_temp(temp_key, data, intent.content_type)
            await self.store.promote(temp_key, final_key)
            async with self.database.tenant_transaction(context) as session:
                artifact = await session.scalar(
                    select(Artifact)
                    .where(Artifact.tenant_id == context.tenant_id, Artifact.id == artifact_id)
                    .with_for_update()
                )
                if artifact is None:
                    raise ResourceNotFound("artifact", str(artifact_id))
                artifact.status = "available"
                artifact.temp_object_key = None
                artifact.object_key = final_key
                artifact.sha256 = digest
                artifact.size_bytes = len(data)
                artifact.available_at = datetime.now(UTC)
                artifact.revision += 1
                shared_ids: list[UUID] = []
                if intent.share_with_parent:
                    parent_id = await self._direct_parent(session, context, owner_run_id)
                    if parent_id is not None:
                        await self._share_locked(
                            session,
                            context,
                            artifact,
                            parent_id,
                            purpose="child_result",
                        )
                        shared_ids.append(parent_id)
                self._record(session, context, artifact, shared_ids)
                await session.flush()
                return RuntimeArtifactOutcome(
                    artifact_id=artifact.id,
                    status=artifact.status,
                    name=artifact.name,
                    content_type=artifact.content_type,
                    sha256=digest,
                    size_bytes=len(data),
                    shared_with_run_ids=tuple(shared_ids),
                )
        except Exception:
            with contextlib.suppress(Exception):
                await self.store.remove(temp_key)
            async with self.database.tenant_transaction(context) as session:
                artifact = await session.scalar(
                    select(Artifact)
                    .where(Artifact.tenant_id == context.tenant_id, Artifact.id == artifact_id)
                    .with_for_update()
                )
                if artifact is not None and artifact.status == "uploading":
                    artifact.status = "failed"
                    artifact.error = {
                        "code": "ARTIFACT_STORE_FAILED",
                        "message": "artifact bytes could not be finalized",
                    }
                    artifact.temp_object_key = None
                    artifact.revision += 1
            raise

    async def list_for_run(self, context: TenantContext, run_id: UUID) -> list[Artifact]:
        async with self.database.tenant_transaction(context) as session:
            await self._required_run(session, context, run_id)
            linked = select(SharedArtifactLink.artifact_id).where(
                SharedArtifactLink.tenant_id == context.tenant_id,
                SharedArtifactLink.grantee_run_id == run_id,
                SharedArtifactLink.status == "active",
                or_(
                    SharedArtifactLink.expires_at.is_(None),
                    SharedArtifactLink.expires_at > datetime.now(UTC),
                ),
            )
            return list(
                await session.scalars(
                    select(Artifact)
                    .where(
                        Artifact.tenant_id == context.tenant_id,
                        Artifact.status == "available",
                        or_(Artifact.owner_run_id == run_id, Artifact.id.in_(linked)),
                    )
                    .order_by(Artifact.created_at, Artifact.id)
                )
            )

    async def read_for_run(
        self, context: TenantContext, run_id: UUID, artifact_id: UUID
    ) -> tuple[Artifact, bytes]:
        async with self.database.tenant_transaction(context) as session:
            await self._required_run(session, context, run_id)
            artifact = await session.scalar(
                select(Artifact).where(
                    Artifact.tenant_id == context.tenant_id,
                    Artifact.id == artifact_id,
                    Artifact.status == "available",
                )
            )
            if artifact is None:
                raise ResourceNotFound("artifact", str(artifact_id))
            if artifact.owner_run_id != run_id:
                link = await session.scalar(
                    select(SharedArtifactLink.id).where(
                        SharedArtifactLink.tenant_id == context.tenant_id,
                        SharedArtifactLink.artifact_id == artifact.id,
                        SharedArtifactLink.grantee_run_id == run_id,
                        SharedArtifactLink.status == "active",
                        or_(
                            SharedArtifactLink.expires_at.is_(None),
                            SharedArtifactLink.expires_at > datetime.now(UTC),
                        ),
                    )
                )
                if link is None:
                    raise AccessDenied(
                        "ARTIFACT_ACCESS_DENIED",
                        "the requesting Run does not own or hold a valid Artifact link",
                    )
            object_key = artifact.object_key
        if object_key is None:
            raise DomainConflict("ARTIFACT_NOT_AVAILABLE", "artifact object is unavailable")
        data = await self.store.read(object_key)
        if hashlib.sha256(data).hexdigest() != artifact.sha256 or len(data) != artifact.size_bytes:
            raise DomainConflict(
                "ARTIFACT_INTEGRITY_FAILED",
                "artifact hash or size does not match",
            )
        return artifact, data

    @staticmethod
    async def _outcome_locked(
        session, context: TenantContext, artifact: Artifact
    ) -> RuntimeArtifactOutcome:
        shared = tuple(
            await session.scalars(
                select(SharedArtifactLink.grantee_run_id).where(
                    SharedArtifactLink.tenant_id == context.tenant_id,
                    SharedArtifactLink.artifact_id == artifact.id,
                    SharedArtifactLink.status == "active",
                )
            )
        )
        return RuntimeArtifactOutcome(
            artifact_id=artifact.id,
            status=artifact.status,
            name=artifact.name,
            content_type=artifact.content_type,
            sha256=artifact.sha256 or "",
            size_bytes=artifact.size_bytes or 0,
            shared_with_run_ids=shared,
        )

    async def _wait_for_replay(
        self, context: TenantContext, artifact_id: UUID
    ) -> RuntimeArtifactOutcome:
        for _ in range(300):
            async with self.database.tenant_transaction(context) as session:
                artifact = await session.scalar(
                    select(Artifact).where(
                        Artifact.tenant_id == context.tenant_id,
                        Artifact.id == artifact_id,
                    )
                )
                if artifact is None:
                    raise ResourceNotFound("artifact", str(artifact_id))
                if artifact.status == "available":
                    return await self._outcome_locked(session, context, artifact)
                if artifact.status == "failed":
                    raise DomainConflict(
                        "ARTIFACT_UPLOAD_FAILED",
                        "the idempotent Artifact upload failed",
                    )
            await asyncio.sleep(0.1)
        raise DomainConflict(
            "ARTIFACT_UPLOAD_INCOMPLETE",
            "the idempotent Artifact upload did not become available in time",
        )

    @staticmethod
    async def _required_run(session, context: TenantContext, run_id: UUID) -> Run:
        run = await session.scalar(
            select(Run).where(Run.tenant_id == context.tenant_id, Run.id == run_id)
        )
        if run is None:
            raise ResourceNotFound("run", str(run_id))
        return run

    @staticmethod
    async def _direct_parent(session, context: TenantContext, run_id: UUID) -> UUID | None:
        return await session.scalar(
            select(AgentRunRelation.ancestor_run_id).where(
                AgentRunRelation.tenant_id == context.tenant_id,
                AgentRunRelation.descendant_run_id == run_id,
                AgentRunRelation.is_direct.is_(True),
            )
        )

    @staticmethod
    async def _share_locked(
        session,
        context: TenantContext,
        artifact: Artifact,
        grantee_run_id: UUID,
        *,
        purpose: str,
    ) -> SharedArtifactLink:
        relation = await session.scalar(
            select(AgentRunRelation).where(
                AgentRunRelation.tenant_id == context.tenant_id,
                AgentRunRelation.ancestor_run_id == grantee_run_id,
                AgentRunRelation.descendant_run_id == artifact.owner_run_id,
                AgentRunRelation.is_direct.is_(True),
            )
        )
        if relation is None:
            raise AccessDenied(
                "ARTIFACT_SHARE_DENIED",
                "the first Artifact release only permits Child-to-direct-Parent sharing",
            )
        existing = await session.scalar(
            select(SharedArtifactLink).where(
                SharedArtifactLink.tenant_id == context.tenant_id,
                SharedArtifactLink.artifact_id == artifact.id,
                SharedArtifactLink.grantee_run_id == grantee_run_id,
                SharedArtifactLink.purpose == purpose,
            )
        )
        if existing is not None:
            return existing
        link = SharedArtifactLink(
            tenant_id=context.tenant_id,
            artifact_id=artifact.id,
            owner_run_id=artifact.owner_run_id,
            grantee_run_id=grantee_run_id,
            visibility="parent",
            purpose=purpose,
        )
        session.add(link)
        return link

    @staticmethod
    def _record(
        session,
        context: TenantContext,
        artifact: Artifact,
        shared_ids: list[UUID],
    ) -> None:
        payload = {
            "owner_run_id": str(artifact.owner_run_id),
            "sha256": artifact.sha256,
            "size_bytes": artifact.size_bytes,
            "shared_with_run_ids": [str(value) for value in shared_ids],
        }
        session.add(
            Event(
                tenant_id=context.tenant_id,
                event_type="ArtifactAvailable",
                aggregate_type="artifact",
                aggregate_id=artifact.id,
                run_id=artifact.owner_run_id,
                actor_id=context.actor_id,
                payload=payload,
                correlation_id=context.correlation_id,
            )
        )
        session.add(
            AuditRecord(
                tenant_id=context.tenant_id,
                action="artifact.store",
                resource_type="artifact",
                resource_id=artifact.id,
                actor_id=context.actor_id,
                details=payload,
                correlation_id=context.correlation_id,
            )
        )
