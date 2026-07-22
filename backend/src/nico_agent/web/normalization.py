"""Normalization for untrusted text received from external Web systems."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

from nico_agent.tools.secrets import redact_value


@dataclass(frozen=True, slots=True)
class ExternalText:
    text: str
    truncated: bool

    @property
    def metadata(self) -> dict[str, bool]:
        return {"external_content": True}


def clean_external_text(
    value: str,
    maximum: int | None = None,
    *,
    secrets: Mapping[str, str] | None = None,
) -> str:
    if maximum is not None and maximum < 1:
        raise ValueError("maximum must be positive")
    redacted = str(redact_value(value, secrets))
    normalized = unicodedata.normalize("NFC", redacted).replace("\r\n", "\n").replace("\r", "\n")
    cleaned = "".join(
        character
        for character in normalized
        if character in {"\n", "\t"} or not unicodedata.category(character).startswith("C")
    ).strip()
    return cleaned if maximum is None else cleaned[:maximum]


def bounded_external_text(
    value: str,
    *,
    maximum: int,
    secrets: Mapping[str, str] | None = None,
) -> ExternalText:
    cleaned = clean_external_text(value, secrets=secrets)
    return ExternalText(text=cleaned[:maximum], truncated=len(cleaned) > maximum)
