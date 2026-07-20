from __future__ import annotations

import math

import pytest

from nico_agent.memory.chunking import DeterministicChunker, content_hash, normalize_text
from nico_agent.memory.embedding import FeatureHashEmbedding


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def test_normalization_and_content_hash_are_stable() -> None:
    first = "  Ａｇｅｎｔ\r\nlearns safely.   \r\n\r\n\r\n"
    second = "Agent\nlearns safely.\n\n"

    assert normalize_text(first) == "Agent\nlearns safely."
    assert normalize_text(first) == normalize_text(second)
    assert content_hash(first) == content_hash(second)
    assert len(content_hash(first)) == 64


@pytest.mark.parametrize("value", ["", " \n\t "])
def test_normalization_rejects_empty_content(value: str) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        normalize_text(value)


def test_chunker_is_deterministic_bounded_and_overlapping() -> None:
    text = (
        "First paragraph explains candidate isolation and approval. " * 5
        + "\n\n"
        + "Second paragraph explains tenant scoped retrieval and pgvector ranking. " * 5
        + "\n\n"
        + "Third paragraph explains immutable sources and rollback. " * 5
    )
    chunker = DeterministicChunker(max_chars=180, overlap_chars=30)

    first = chunker.chunk(text)
    second = chunker.chunk(text)

    assert first == second
    assert len(first) > 2
    assert [chunk.index for chunk in first] == list(range(len(first)))
    assert all(len(chunk.content) <= 180 for chunk in first)
    assert all(len(chunk.content_hash) == 64 for chunk in first)
    assert all(
        current.start_offset < previous.end_offset
        for previous, current in zip(first, first[1:], strict=False)
    )


def test_chunker_prefers_paragraph_boundary() -> None:
    first_paragraph = "A" * 100
    second_paragraph = "B" * 100
    chunks = DeterministicChunker(max_chars=150, overlap_chars=20).chunk(
        f"{first_paragraph}\n\n{second_paragraph}"
    )

    assert chunks[0].content == first_paragraph
    assert chunks[0].end_offset == len(first_paragraph)


def test_chunker_configuration_fails_closed() -> None:
    with pytest.raises(ValueError, match="at least 64"):
        DeterministicChunker(max_chars=63)
    with pytest.raises(ValueError, match="less than half"):
        DeterministicChunker(max_chars=100, overlap_chars=50)


def test_feature_hash_embedding_is_normalized_and_deterministic() -> None:
    embedder = FeatureHashEmbedding()
    values = embedder.embed(
        [
            "candidate approval and safe publication",
            "candidate approval and controlled publication",
            "ocean temperature and cloud formation",
        ]
    )

    assert len(values) == 3
    assert all(len(vector) == 384 for vector in values)
    assert all(math.isclose(_cosine(vector, vector), 1.0, abs_tol=1e-12) for vector in values)
    assert values == embedder.embed(
        [
            "candidate approval and safe publication",
            "candidate approval and controlled publication",
            "ocean temperature and cloud formation",
        ]
    )
    assert _cosine(values[0], values[1]) > _cosine(values[0], values[2])


def test_embedding_rejects_empty_batches_and_text() -> None:
    embedder = FeatureHashEmbedding()
    with pytest.raises(ValueError, match="at least one"):
        embedder.embed([])
    with pytest.raises(ValueError, match="must not be empty"):
        embedder.embed(["  "])
