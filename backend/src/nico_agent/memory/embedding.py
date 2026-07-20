"""Offline deterministic feature-hashing embedding used as the reproducible baseline."""

from __future__ import annotations

import hashlib
import math
import re

from nico_agent.memory.chunking import normalize_text

_TOKENS = re.compile(r"\w+|[^\w\s]", re.UNICODE)


class FeatureHashEmbedding:
    name = "nico-feature-hashing"
    version = "1.0.0"
    dimension = 384

    def embed(self, texts: list[str]) -> list[tuple[float, ...]]:
        if not texts:
            raise ValueError("at least one text is required")
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, value: str) -> tuple[float, ...]:
        text = normalize_text(value).casefold()
        features: list[tuple[str, float]] = []
        for token in _TOKENS.findall(text):
            features.append((f"token:{token}", 2.0))
            padded = f"^{token}$"
            for width in (3, 4, 5):
                if len(padded) < width:
                    continue
                weight = 0.5 / math.sqrt(len(padded) - width + 1)
                for offset in range(len(padded) - width + 1):
                    features.append((f"char{width}:{padded[offset : offset + width]}", weight))
        vector = [0.0] * self.dimension
        for feature, weight in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            encoded = int.from_bytes(digest, "big")
            index = encoded % self.dimension
            sign = -1.0 if encoded & (1 << 63) else 1.0
            vector[index] += sign * weight
        norm = math.sqrt(sum(component * component for component in vector))
        if norm == 0:
            raise ValueError("text did not produce an embedding")
        return tuple(component / norm for component in vector)
