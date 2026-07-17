"""Tenant-safe Memory indexing and pgvector cosine retrieval application services."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from nico_agent.database import Database, TenantContext
from nico_agent.domain.errors import AccessDenied, DomainConflict, ResourceNotFound
from nico_agent.domain.models import Agent, GrowthSource, Memory, MemoryChunk, Project
from nico_agent.memory.chunking import DeterministicChunker, content_hash
from nico_agent.memory.contracts import (
    EmbeddingProvider,
    MemoryIndexSummary,
    MemoryQueryContext,
    MemorySearchResult,
    MemorySourceReference,
)
from nico_agent.memory.embedding import FeatureHashEmbedding


class MemoryIndexService:
    def __init__(
        self,
        database: Database,
        *,
        chunker: DeterministicChunker | None = None,
        embedder: EmbeddingProvider | None = None,
    ) -> None:
        self.database = database
        self.chunker = chunker or DeterministicChunker()
        self.embedder = embedder or FeatureHashEmbedding()
        if self.embedder.dimension != 384:
            raise ValueError("the current memory_chunks index requires 384 dimensions")

    async def index_memory(self, context: TenantContext, memory_id: UUID) -> MemoryIndexSummary:
        async with self.database.tenant_transaction(context) as session:
            memory = await session.scalar(select(Memory).where(Memory.id == memory_id))
            if memory is None:
                raise ResourceNotFound("memory", str(memory_id))
            return await self.index_memory_in_session(session, memory)

    async def index_memory_in_session(
        self, session: AsyncSession, memory: Memory
    ) -> MemoryIndexSummary:
        """Create deterministic chunks inside an existing publication transaction."""

        if memory.status != "active":
            raise DomainConflict(
                "MEMORY_NOT_ACTIVE",
                "only active memory can be indexed",
                details={"memory_id": str(memory.id), "status": memory.status},
            )
        if memory.expires_at is not None and memory.expires_at <= datetime.now(UTC):
            raise DomainConflict(
                "MEMORY_EXPIRED",
                "expired memory cannot be indexed",
                details={"memory_id": str(memory.id)},
            )
        canonical_hash = content_hash(memory.content)
        if canonical_hash != memory.content_hash:
            raise DomainConflict(
                "MEMORY_CONTENT_HASH_MISMATCH",
                "memory content does not match its immutable hash",
                details={"memory_id": str(memory.id)},
            )

        chunks = self.chunker.chunk(memory.content)
        vectors = self.embedder.embed([chunk.content for chunk in chunks])
        existing = (
            await session.scalars(
                select(MemoryChunk)
                .where(
                    MemoryChunk.memory_id == memory.id,
                    MemoryChunk.embedding_provider == self.embedder.name,
                    MemoryChunk.embedding_version == self.embedder.version,
                    MemoryChunk.chunker_version == self.chunker.version,
                )
                .order_by(MemoryChunk.chunk_index)
            )
        ).all()
        if existing:
            expected = [(chunk.index, chunk.content_hash) for chunk in chunks]
            actual = [(chunk.chunk_index, chunk.content_hash) for chunk in existing]
            if actual != expected:
                raise DomainConflict(
                    "MEMORY_INDEX_CONFLICT",
                    "existing immutable index does not match deterministic chunks",
                    details={"memory_id": str(memory.id)},
                )
            return self._summary(memory, len(existing), already_indexed=True)

        session.add_all(
            [
                MemoryChunk(
                    tenant_id=memory.tenant_id,
                    memory_id=memory.id,
                    chunk_index=chunk.index,
                    start_offset=chunk.start_offset,
                    end_offset=chunk.end_offset,
                    content=chunk.content,
                    content_hash=chunk.content_hash,
                    memory_content_hash=memory.content_hash,
                    chunker_name=self.chunker.name,
                    chunker_version=self.chunker.version,
                    embedding_provider=self.embedder.name,
                    embedding_version=self.embedder.version,
                    embedding_dimension=self.embedder.dimension,
                    embedding=list(vector),
                )
                for chunk, vector in zip(chunks, vectors, strict=True)
            ]
        )
        await session.flush()
        return self._summary(memory, len(chunks), already_indexed=False)

    def _summary(
        self, memory: Memory, chunk_count: int, *, already_indexed: bool
    ) -> MemoryIndexSummary:
        return MemoryIndexSummary(
            memory_id=memory.id,
            content_hash=memory.content_hash,
            chunk_count=chunk_count,
            embedding_provider=self.embedder.name,
            embedding_version=self.embedder.version,
            embedding_dimension=self.embedder.dimension,
            already_indexed=already_indexed,
        )


class MemoryRetriever:
    def __init__(
        self,
        database: Database,
        *,
        embedder: EmbeddingProvider | None = None,
        chunker_version: str = DeterministicChunker.version,
    ) -> None:
        self.database = database
        self.embedder = embedder or FeatureHashEmbedding()
        self.chunker_version = chunker_version
        if self.embedder.dimension != 384:
            raise ValueError("the current memory_chunks index requires 384 dimensions")

    async def search(
        self,
        context: TenantContext,
        query_context: MemoryQueryContext,
        query: str,
        *,
        limit: int = 10,
        minimum_similarity: float = 0.0,
    ) -> tuple[MemorySearchResult, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if not 0.0 <= minimum_similarity <= 1.0:
            raise ValueError("minimum_similarity must be between 0 and 1")
        if query_context.team_id is not None:
            raise AccessDenied(
                "MEMORY_TEAM_SCOPE_UNAVAILABLE",
                "team memory scope is unavailable until Goal G establishes membership",
            )
        query_vector = list(self.embedder.embed([query])[0])

        async with self.database.tenant_transaction(context) as session:
            await self._validate_context(session, query_context)
            scope_predicates = [Memory.scope_type == "tenant"]
            if query_context.project_id is not None:
                scope_predicates.append(
                    and_(
                        Memory.scope_type == "project",
                        Memory.project_id == query_context.project_id,
                    )
                )
            if query_context.agent_id is not None:
                scope_predicates.append(
                    and_(
                        Memory.scope_type == "agent",
                        Memory.agent_id == query_context.agent_id,
                    )
                )

            distance = MemoryChunk.embedding.cosine_distance(query_vector)
            ranked = (
                select(
                    Memory.id.label("memory_id"),
                    Memory.memory_key,
                    Memory.version,
                    Memory.memory_type,
                    Memory.scope_type,
                    Memory.content.label("memory_content"),
                    Memory.confidence,
                    MemoryChunk.chunk_index,
                    MemoryChunk.content.label("chunk_content"),
                    distance.label("distance"),
                    func.row_number()
                    .over(
                        partition_by=Memory.id,
                        order_by=(distance, MemoryChunk.chunk_index),
                    )
                    .label("chunk_rank"),
                )
                .join(MemoryChunk, MemoryChunk.memory_id == Memory.id)
                .where(
                    Memory.status == "active",
                    or_(Memory.expires_at.is_(None), Memory.expires_at > func.now()),
                    or_(*scope_predicates),
                    MemoryChunk.embedding_provider == self.embedder.name,
                    MemoryChunk.embedding_version == self.embedder.version,
                    MemoryChunk.embedding_dimension == self.embedder.dimension,
                    MemoryChunk.chunker_version == self.chunker_version,
                    distance <= 1.0 - minimum_similarity,
                )
                .subquery()
            )
            rows = (
                (
                    await session.execute(
                        select(ranked)
                        .where(ranked.c.chunk_rank == 1)
                        .order_by(ranked.c.distance, ranked.c.memory_id)
                        .limit(limit)
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                return ()

            memory_ids = [row["memory_id"] for row in rows]
            source_rows = (
                await session.scalars(
                    select(GrowthSource)
                    .where(GrowthSource.memory_id.in_(memory_ids))
                    .order_by(GrowthSource.memory_id, GrowthSource.created_at, GrowthSource.id)
                )
            ).all()
            sources_by_memory: dict[UUID, list[MemorySourceReference]] = defaultdict(list)
            for source in source_rows:
                if source.memory_id is None:
                    continue
                sources_by_memory[source.memory_id].append(
                    MemorySourceReference(
                        run_id=source.run_id,
                        run_step_id=source.run_step_id,
                        tool_call_id=source.tool_call_id,
                        trajectory_hash=source.trajectory_hash,
                        source_hash=source.source_hash,
                    )
                )
            return tuple(
                MemorySearchResult(
                    memory_id=row["memory_id"],
                    memory_key=row["memory_key"],
                    version=row["version"],
                    memory_type=row["memory_type"],
                    scope_type=row["scope_type"],
                    content=row["memory_content"],
                    confidence=float(row["confidence"]),
                    similarity=max(0.0, min(1.0, 1.0 - float(row["distance"]))),
                    chunk_index=row["chunk_index"],
                    chunk_content=row["chunk_content"],
                    sources=tuple(sources_by_memory[row["memory_id"]]),
                )
                for row in rows
            )

    async def _validate_context(self, session, query_context: MemoryQueryContext) -> None:
        if query_context.project_id is not None:
            project_id = await session.scalar(
                select(Project.id).where(Project.id == query_context.project_id)
            )
            if project_id is None:
                raise ResourceNotFound("project", str(query_context.project_id))
        if query_context.agent_id is not None:
            agent_id = await session.scalar(
                select(Agent.id).where(Agent.id == query_context.agent_id)
            )
            if agent_id is None:
                raise ResourceNotFound("agent", str(query_context.agent_id))
