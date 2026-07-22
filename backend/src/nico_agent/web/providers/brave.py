"""Brave Web Search API adapter."""

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

_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_SAFE_SEARCH = frozenset({"off", "moderate", "strict"})


class BraveSearchProvider:
    key = "brave"

    def __init__(
        self,
        *,
        http: Any | None = None,
        safe_search: str = "moderate",
        max_response_bytes: int = 524_288,
    ) -> None:
        if safe_search not in _SAFE_SEARCH:
            raise ValueError("Brave SafeSearch must be off, moderate or strict")
        self.http = http or SafeHttpClient()
        self.safe_search = safe_search
        self.max_response_bytes = max_response_bytes
        self.policy = SafeHttpPolicy.exact_endpoint(_ENDPOINT)

    async def search(
        self,
        request: SearchRequest,
        *,
        secret: str | None = None,
    ) -> SearchPage:
        if not isinstance(secret, str) or not secret:
            raise SearchProviderError(
                "WEB_SEARCH_SECRET_UNAVAILABLE",
                "Brave Web Search credential is unavailable",
            )
        params = {
            "q": query_with_domains(request),
            "count": str(request.count),
            "safesearch": self.safe_search,
        }
        if request.language:
            params["search_lang"] = request.language
        if request.country:
            params["country"] = request.country
        if request.freshness:
            params["freshness"] = request.freshness
        try:
            response = await self.http.request(
                "GET",
                _ENDPOINT,
                policy=self.policy,
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": secret,
                    "User-Agent": "Nico-Agent-Web-Search/1.0",
                },
                params=params,
                max_response_bytes=self.max_response_bytes,
                max_redirects=0,
            )
        except SafeHttpError as exc:
            raise SearchProviderError(
                "WEB_PROVIDER_UNAVAILABLE",
                "Brave Web Search request failed",
                retryable=exc.code in {"NETWORK_ERROR", "DNS_ERROR"},
            ) from exc
        self._raise_status(response)
        payload = json_object(response)
        web = payload.get("web")
        raw_results = web.get("results") if isinstance(web, dict) else None
        if not isinstance(raw_results, list):
            raise protocol_error()
        values = []
        for raw in raw_results:
            profile = raw.get("profile") if isinstance(raw, dict) else None
            site_name = profile.get("long_name") if isinstance(profile, dict) else None
            if site_name is not None and not isinstance(site_name, str):
                raise protocol_error()
            values.append(
                result(
                    raw,
                    title_key="title",
                    url_key="url",
                    snippet_key="description",
                    published_key="page_age",
                    site_name=site_name,
                    secrets={"brave_api_key": secret},
                )
            )
        return page(self.key, values, request)

    @staticmethod
    def _raise_status(response) -> None:
        if response.status == 200:
            return
        if response.status in {401, 403}:
            raise SearchProviderError(
                "WEB_PROVIDER_AUTH_FAILED",
                "Brave Web Search authentication failed",
            )
        if response.status == 429:
            raise SearchProviderError(
                "WEB_PROVIDER_RATE_LIMITED",
                "Brave Web Search rate limit was exceeded",
                retryable=True,
                retry_after_seconds=retry_after(response),
            )
        if 500 <= response.status < 600:
            raise SearchProviderError(
                "WEB_PROVIDER_UNAVAILABLE",
                "Brave Web Search is unavailable",
                retryable=True,
            )
        raise protocol_error()
