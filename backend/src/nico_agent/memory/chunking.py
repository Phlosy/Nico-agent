"""Versioned deterministic Unicode normalization and paragraph-aware chunking."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from nico_agent.memory.contracts import TextChunk

_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")


def normalize_text(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("memory content must be a string")
    normalized = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n"))
    normalized = _EXCESS_BLANK_LINES.sub("\n\n", normalized).strip()
    if not normalized:
        raise ValueError("memory content must not be empty")
    return normalized


def content_hash(value: str) -> str:
    return hashlib.sha256(normalize_text(value).encode("utf-8")).hexdigest()


class DeterministicChunker:
    name = "nico-paragraph-window"
    version = "1.0.0"

    def __init__(self, *, max_chars: int = 800, overlap_chars: int = 120) -> None:
        if max_chars < 64:
            raise ValueError("max_chars must be at least 64")
        if overlap_chars < 0 or overlap_chars >= max_chars // 2:
            raise ValueError("overlap_chars must be non-negative and less than half max_chars")
        self.max_chars = max_chars
        self.overlap_chars = overlap_chars

    def chunk(self, value: str) -> tuple[TextChunk, ...]:
        text = normalize_text(value)
        chunks: list[TextChunk] = []
        start = 0
        while start < len(text):
            upper = min(start + self.max_chars, len(text))
            end = self._preferred_end(text, start, upper)
            content_start = start
            while content_start < end and text[content_start].isspace():
                content_start += 1
            content_end = end
            while content_end > content_start and text[content_end - 1].isspace():
                content_end -= 1
            if content_end <= content_start:
                break
            chunk_text = text[content_start:content_end]
            chunks.append(
                TextChunk(
                    index=len(chunks),
                    start_offset=content_start,
                    end_offset=content_end,
                    content=chunk_text,
                    content_hash=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                )
            )
            if end >= len(text):
                break
            next_start = max(end - self.overlap_chars, start + 1)
            while next_start < len(text) and text[next_start].isspace():
                next_start += 1
            start = next_start
        if not chunks:
            raise ValueError("memory content did not produce a chunk")
        return tuple(chunks)

    def _preferred_end(self, text: str, start: int, upper: int) -> int:
        if upper >= len(text):
            return len(text)
        minimum = start + self.max_chars // 2
        paragraph = text.rfind("\n\n", minimum, upper)
        if paragraph >= minimum:
            return paragraph
        whitespace = max(
            text.rfind(" ", minimum, upper),
            text.rfind("\n", minimum, upper),
            text.rfind("\t", minimum, upper),
        )
        return whitespace if whitespace >= minimum else upper
