"""SearXNG JSON Search API adapter."""

from __future__ import annotations

from typing import Any

from nico_agent.net.safe_http import SafeHttpClient, SafeHttpError, SafeHttpPolicy
from nico_agent.web.contracts import SearchPage, SearchProviderError, SearchRequest
from nico_agent.web.providers._common import (
    json_object,
    page,
    protocol_error,
    query_with_domains,
    result,
    retry_after,
)

_SAFE_SEARCH = {"off": "0", "moderate": "1", "strict": "2"}


class SearxngSearchProvider:
    key = "searxng"

    def __init__(
        self,
        *,
        endpoint: str,
        http: Any | None = None,
        allow_private: bool = False,
        safe_search: str = "moderate",
        max_response_bytes: int = 524_288,
    ) -> None:
        if safe_search not in _SAFE_SEARCH:
            raise ValueError("SearXNG SafeSearch must be off, moderate or strict")
        self.http = http or SafeHttpClient()
        self.endpoint = endpoint
        self.safe_search = safe_search
        self.max_response_bytes = max_response_bytes
        self.policy = SafeHttpPolicy.exact_endpoint(
            endpoint,
            allow_http=allow_private,
            allow_loopback=allow_private,
            allow_private=allow_private,
        )

    async def search(
        self,
        request: SearchRequest,
        *,
        secret: str | None = None,
    ) -> SearchPage:
        params = {
            "q": query_with_domains(request),
            "format": "json",
            "pageno": "1",
            "safesearch": _SAFE_SEARCH[self.safe_search],
        }
        if request.language:
            params["language"] = request.language
        if request.freshness:
            params["time_range"] = request.freshness
        try:
            response = await self.http.request(
                "GET",
                self.endpoint,
                policy=self.policy,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Nico-Agent-Web-Search/1.0",
                },
                params=params,
                max_response_bytes=self.max_response_bytes,
                max_redirects=0,
            )
        except SafeHttpError as exc:
            raise SearchProviderError(
                "WEB_PROVIDER_UNAVAILABLE",
                "SearXNG request failed",
                retryable=exc.code in {"NETWORK_ERROR", "DNS_ERROR"},
            ) from exc
        self._raise_status(response)
        payload = json_object(response)
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise protocol_error()
        values = []
        for raw in raw_results:
            site_name = raw.get("engine") if isinstance(raw, dict) else None
            if site_name is not None and not isinstance(site_name, str):
                raise protocol_error()
            values.append(
                result(
                    raw,
                    title_key="title",
                    url_key="url",
                    snippet_key="content",
                    published_key="publishedDate",
                    site_name=site_name,
                )
            )
        return page(self.key, values, request)

    @staticmethod
    def _raise_status(response) -> None:
        if response.status == 200:
            return
        if response.status == 403:
            raise SearchProviderError(
                "WEB_PROVIDER_PROTOCOL_ERROR",
                "SearXNG JSON search is not enabled",
            )
        if response.status == 429:
            raise SearchProviderError(
                "WEB_PROVIDER_RATE_LIMITED",
                "SearXNG rate limit was exceeded",
                retryable=True,
                retry_after_seconds=retry_after(response),
            )
        if 500 <= response.status < 600:
            raise SearchProviderError(
                "WEB_PROVIDER_UNAVAILABLE",
                "SearXNG is unavailable",
                retryable=True,
            )
        raise protocol_error()
