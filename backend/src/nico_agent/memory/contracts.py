"""Provider-neutral immutable contracts for Memory indexing and retrieval."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TextChunk:
    index: int
    start_offset: int
    end_offset: int
    content: str
    content_hash: str


class EmbeddingProvider(Protocol):
    name: str
    version: str
    dimension: int

    def embed(self, texts: list[str]) -> list[tuple[float, ...]]: ...


@dataclass(frozen=True, slots=True)
class MemoryQueryContext:
    project_id: UUID | None = None
    agent_id: UUID | None = None
    team_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class MemorySourceReference:
    run_id: UUID
    run_step_id: UUID
    tool_call_id: UUID | None
    trajectory_hash: str
    source_hash: str


@dataclass(frozen=True, slots=True)
class MemorySearchResult:
    memory_id: UUID
    memory_key: UUID
    version: int
    memory_type: str
    scope_type: str
    content: str
    content_hash: str
    confidence: float
    similarity: float
    chunk_index: int
    chunk_content: str
    sources: tuple[MemorySourceReference, ...]


@dataclass(frozen=True, slots=True)
class MemoryIndexSummary:
    memory_id: UUID
    content_hash: str
    chunk_count: int
    embedding_provider: str
    embedding_version: str
    embedding_dimension: int
    already_indexed: bool
