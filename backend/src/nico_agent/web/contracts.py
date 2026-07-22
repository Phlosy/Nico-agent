"""Provider-neutral contracts for Web search."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nico_agent.net.safe_http import SafeHttpError, canonicalize_http_url

_LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z]{2,4})?$")
_COUNTRY = re.compile(r"^[A-Za-z]{2}$")


class SearchRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str = Field(min_length=1, max_length=2000)
    count: int = Field(default=5, ge=1, le=10)
    language: str | None = None
    country: str | None = None
    freshness: Literal["day", "week", "month", "year"] | None = None
    domains: tuple[str, ...] = Field(default=(), max_length=10)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = unicodedata.normalize("NFC", value).strip()
        if not normalized or any(
            unicodedata.category(character).startswith("C") for character in normalized
        ):
            raise ValueError("query contains unsupported control characters")
        return normalized

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str | None) -> str | None:
        if value is not None and _LANGUAGE.fullmatch(value) is None:
            raise ValueError("language must be a short BCP-47-like code")
        return value

    @field_validator("country")
    @classmethod
    def validate_country(cls, value: str | None) -> str | None:
        if value is not None and _COUNTRY.fullmatch(value) is None:
            raise ValueError("country must be a two-letter code")
        return value.upper() if value is not None else None

    @field_validator("domains")
    @classmethod
    def normalize_domains(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            if not isinstance(value, str) or "*" in value or "://" in value:
                raise ValueError("domains must contain exact hostnames")
            try:
                parsed = urlsplit(f"https://{value.rstrip('.')}/")
                hostname = parsed.hostname
                if not hostname or parsed.port is not None:
                    raise ValueError
                hostname = hostname.encode("idna").decode("ascii").lower()
            except (UnicodeError, ValueError) as exc:
                raise ValueError("domains must contain valid exact hostnames") from exc
            if hostname not in normalized:
                normalized.append(hostname)
        return tuple(normalized)

    def normalized_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class SearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1, max_length=500)
    url: str = Field(min_length=1, max_length=4096)
    snippet: str = Field(max_length=2000)
    published_at: str | None = Field(default=None, max_length=100)
    site_name: str | None = Field(default=None, max_length=200)

    @field_validator("url")
    @classmethod
    def canonicalize_url(cls, value: str) -> str:
        try:
            return canonicalize_http_url(value).url
        except SafeHttpError as exc:
            raise ValueError("search result URL is invalid") from exc


class SearchPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: Literal["brave", "searxng"]
    results: tuple[SearchResult, ...] = Field(max_length=10)


class SearchProviderError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message[:500]
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds


class SearchProvider(Protocol):
    key: str

    async def search(
        self,
        request: SearchRequest,
        *,
        secret: str | None = None,
    ) -> SearchPage: ...
