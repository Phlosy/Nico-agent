from __future__ import annotations

import math
import time

import pytest

from nico_agent.net.safe_http import SafeHttpClient
from nico_agent.testing.fake_web import FakeWebTransport, fake_web_resolver
from nico_agent.web.contracts import SearchRequest
from nico_agent.web.extraction import extract_web_content
from nico_agent.web.providers.searxng import SearxngSearchProvider


@pytest.mark.asyncio
async def test_search_adapter_platform_processing_p95_is_below_100_ms() -> None:
    provider = SearxngSearchProvider(
        endpoint="http://fake-web:8120/search",
        http=SafeHttpClient(transport=FakeWebTransport(), resolver=fake_web_resolver),
        allow_private=True,
    )
    request = SearchRequest(query="bounded benchmark", count=1)
    await provider.search(request)
    samples = []
    for _ in range(30):
        started = time.perf_counter()
        page = await provider.search(request)
        samples.append((time.perf_counter() - started) * 1000)
        assert len(page.results) == 1
    assert _p95(samples) < 100


def test_maximum_html_extraction_platform_processing_p95_is_below_250_ms() -> None:
    body = (
        "<html><head><title>Benchmark</title></head><body><main><p>"
        + "Bounded deterministic evidence. " * 23_000
        + "</p></main></body></html>"
    ).encode()
    assert 700_000 < len(body) <= 768_000
    extract_web_content(body, "text/html; charset=utf-8", mode="markdown", max_chars=20_000)
    samples = []
    for _ in range(8):
        started = time.perf_counter()
        result = extract_web_content(
            body,
            "text/html; charset=utf-8",
            mode="markdown",
            max_chars=20_000,
        )
        samples.append((time.perf_counter() - started) * 1000)
        assert result.truncated is True and len(result.content) == 20_000
    assert _p95(samples) < 250


def _p95(samples: list[float]) -> float:
    return sorted(samples)[math.ceil(len(samples) * 0.95) - 1]
