from __future__ import annotations

import json

import pytest

from nico_agent.net.safe_http import RawHttpResponse
from nico_agent.web.contracts import SearchProviderError, SearchRequest
from nico_agent.web.providers.searxng import SearxngSearchProvider


class FakeHttp:
    def __init__(self, response: RawHttpResponse) -> None:
        self.response = response
        self.requests: list[dict] = []

    async def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.response


def _response(payload, *, status: int = 200, content_type: str = "application/json"):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return RawHttpResponse(status, (("Content-Type", content_type),), body)


@pytest.mark.asyncio
async def test_searxng_uses_json_contract_without_a_secret() -> None:
    http = FakeHttp(
        _response(
            {
                "results": [
                    {
                        "title": "Nico docs",
                        "url": "https://docs.example/nico",
                        "content": "Current documentation",
                        "publishedDate": "2026-07-20",
                        "engine": "duckduckgo",
                    }
                ]
            }
        )
    )
    provider = SearxngSearchProvider(
        http=http,
        endpoint="http://localhost:8080/search",
        allow_private=True,
        safe_search="moderate",
    )

    page = await provider.search(
        SearchRequest(
            query="nico",
            language="zh-CN",
            freshness="month",
            domains=("docs.example",),
        )
    )

    assert http.requests[0]["params"] == {
        "q": "nico (site:docs.example)",
        "format": "json",
        "language": "zh-CN",
        "pageno": "1",
        "time_range": "month",
        "safesearch": "1",
    }
    assert page.provider == "searxng"
    assert page.results[0].site_name == "duckduckgo"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "code"),
    [
        (_response({}, status=403), "WEB_PROVIDER_PROTOCOL_ERROR"),
        (
            _response(b"<html>disabled</html>", content_type="text/html"),
            "WEB_PROVIDER_PROTOCOL_ERROR",
        ),
        (_response({"answers": []}), "WEB_PROVIDER_PROTOCOL_ERROR"),
    ],
)
async def test_searxng_rejects_disabled_or_non_json_responses(response, code) -> None:
    provider = SearxngSearchProvider(
        http=FakeHttp(response),
        endpoint="https://search.example/search",
    )

    with pytest.raises(SearchProviderError) as captured:
        await provider.search(SearchRequest(query="nico"))

    assert captured.value.code == code
    assert "disabled" not in captured.value.message
