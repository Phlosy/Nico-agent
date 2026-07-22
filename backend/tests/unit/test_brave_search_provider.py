from __future__ import annotations

import json

import pytest

from nico_agent.net.safe_http import RawHttpResponse
from nico_agent.web.contracts import SearchProviderError, SearchRequest
from nico_agent.web.providers.brave import BraveSearchProvider


class FakeHttp:
    def __init__(self, response: RawHttpResponse) -> None:
        self.response = response
        self.requests: list[dict] = []

    async def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.response


def _response(payload, *, status: int = 200, headers=()) -> RawHttpResponse:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return RawHttpResponse(
        status=status,
        headers=(("Content-Type", "application/json"), *headers),
        body=body,
    )


@pytest.mark.asyncio
async def test_brave_maps_request_and_normalizes_deduplicated_results() -> None:
    http = FakeHttp(
        _response(
            {
                "web": {
                    "results": [
                        {
                            "title": "Nico\u0000 docs",
                            "url": "https://Docs.Example:443/current",
                            "description": "Current release notes",
                            "page_age": "2026-07-20",
                            "profile": {"long_name": "Docs"},
                        },
                        {
                            "title": "duplicate",
                            "url": "https://docs.example/current",
                            "description": "duplicate",
                        },
                    ]
                }
            }
        )
    )
    provider = BraveSearchProvider(http=http, safe_search="strict")
    page = await provider.search(
        SearchRequest(
            query="nico current",
            count=5,
            language="en",
            country="US",
            freshness="week",
            domains=("docs.example",),
        ),
        secret="brave-secret",
    )

    request = http.requests[0]
    assert request["method"] == "GET"
    assert request["headers"]["X-Subscription-Token"] == "brave-secret"
    assert request["params"] == {
        "q": "nico current (site:docs.example)",
        "count": "5",
        "search_lang": "en",
        "country": "US",
        "freshness": "week",
        "safesearch": "strict",
    }
    assert page.provider == "brave"
    assert len(page.results) == 1
    assert page.results[0].title == "Nico docs"
    assert page.results[0].url == "https://docs.example/current"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "code", "retryable"),
    [
        (_response({}, status=401), "WEB_PROVIDER_AUTH_FAILED", False),
        (
            _response({}, status=429, headers=(("Retry-After", "7"),)),
            "WEB_PROVIDER_RATE_LIMITED",
            True,
        ),
        (_response({}, status=503), "WEB_PROVIDER_UNAVAILABLE", True),
        (_response(b"not-json"), "WEB_PROVIDER_PROTOCOL_ERROR", False),
        (_response({"unexpected": []}), "WEB_PROVIDER_PROTOCOL_ERROR", False),
    ],
)
async def test_brave_maps_failures_without_exposing_raw_body_or_secret(
    response, code, retryable
) -> None:
    provider = BraveSearchProvider(http=FakeHttp(response))

    with pytest.raises(SearchProviderError) as captured:
        await provider.search(SearchRequest(query="private query"), secret="brave-secret")

    assert captured.value.code == code
    assert captured.value.retryable is retryable
    if response.status == 429:
        assert captured.value.retry_after_seconds == 7
    assert "private query" not in captured.value.message
    assert "brave-secret" not in captured.value.message
    assert "not-json" not in captured.value.message
