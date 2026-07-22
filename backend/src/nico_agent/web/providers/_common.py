"""Shared strict normalization for Web search Provider responses."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from nico_agent.net.safe_http import RawHttpResponse
from nico_agent.web.contracts import (
    SearchPage,
    SearchProviderError,
    SearchRequest,
    SearchResult,
)
from nico_agent.web.normalization import clean_external_text


def headers(response: RawHttpResponse) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in response.headers:
        result.setdefault(name.lower(), value.strip())
    return result


def json_object(response: RawHttpResponse) -> dict[str, Any]:
    media_type = headers(response).get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json" and not media_type.endswith("+json"):
        raise protocol_error()
    try:
        payload = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise protocol_error() from exc
    if not isinstance(payload, dict):
        raise protocol_error()
    return payload


def result(
    raw: Any,
    *,
    title_key: str,
    url_key: str,
    snippet_key: str,
    published_key: str | None = None,
    site_name: str | None = None,
    secrets: Mapping[str, str] | None = None,
) -> SearchResult:
    if not isinstance(raw, dict):
        raise protocol_error()
    title = raw.get(title_key)
    url = raw.get(url_key)
    snippet = raw.get(snippet_key, "")
    if not isinstance(title, str) or not isinstance(url, str) or not isinstance(snippet, str):
        raise protocol_error()
    published = raw.get(published_key) if published_key else None
    if published is not None and not isinstance(published, str):
        raise protocol_error()
    try:
        return SearchResult(
            title=clean_external_text(title, 500, secrets=secrets),
            url=url,
            snippet=clean_external_text(snippet, 2000, secrets=secrets),
            published_at=(
                clean_external_text(published, 100, secrets=secrets) if published else None
            ),
            site_name=(clean_external_text(site_name, 200, secrets=secrets) if site_name else None),
        )
    except ValidationError as exc:
        raise protocol_error() from exc


def page(provider: str, values: list[SearchResult], request: SearchRequest) -> SearchPage:
    deduplicated: list[SearchResult] = []
    seen_urls: set[str] = set()
    for item in values:
        if item.url in seen_urls:
            continue
        seen_urls.add(item.url)
        deduplicated.append(item)
        if len(deduplicated) == request.count:
            break
    return SearchPage(provider=provider, results=tuple(deduplicated))


def query_with_domains(request: SearchRequest) -> str:
    if not request.domains:
        return request.query
    filters = " OR ".join(f"site:{domain}" for domain in request.domains)
    return f"{request.query} ({filters})"


def retry_after(response: RawHttpResponse) -> int | None:
    value = headers(response).get("retry-after")
    if value is None or not value.isdigit():
        return None
    return min(int(value), 3600)


def protocol_error() -> SearchProviderError:
    return SearchProviderError(
        "WEB_PROVIDER_PROTOCOL_ERROR",
        "Web search Provider returned an invalid response",
    )
