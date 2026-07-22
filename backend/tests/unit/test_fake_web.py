from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from nico_agent.testing.fake_model import app as model_app
from nico_agent.testing.fake_web import EVIDENCE_URL
from nico_agent.testing.fake_web import app as web_app


@pytest.mark.asyncio
async def test_fake_web_exposes_search_redirect_failure_and_slow_fixtures() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=web_app), base_url="http://fixture"
    ) as client:
        search = await client.get("/search")
        article = await client.get("/article")
        redirect = await client.get("/redirect", follow_redirects=False)
        private = await client.get("/private-target", follow_redirects=False)
        limited = await client.get("/status/429")
        unavailable = await client.get("/status/503")
        slow = await client.get("/slow")

    assert search.json()["results"][0]["url"] == EVIDENCE_URL
    assert "offline search and fetch chain" in article.text
    assert redirect.headers["location"] == EVIDENCE_URL
    assert private.headers["location"] == "http://169.254.169.254/latest/meta-data"
    assert limited.status_code == 429 and limited.headers["retry-after"] == "1"
    assert unavailable.status_code == 503
    assert slow.status_code == 200


@pytest.mark.asyncio
async def test_fake_model_scripts_search_fetch_and_observed_url_citation() -> None:
    headers = {"Authorization": "Bearer goal-g-fake-token"}
    messages = [{"role": "user", "content": "find current evidence"}]
    async with AsyncClient(
        transport=ASGITransport(app=model_app), base_url="http://model"
    ) as client:
        search_response = await _complete(client, headers, messages)
        search_call = _tool_call(search_response)
        assert search_call["function"]["name"] == "web.search"

        messages.extend(
            [
                {"role": "assistant", "content": None, "tool_calls": [search_call]},
                {
                    "role": "tool",
                    "tool_call_id": search_call["id"],
                    "content": json.dumps(
                        {
                            "tool_call_id": "00000000-0000-4000-8000-000000000001",
                            "output": {"results": [{"url": EVIDENCE_URL}]},
                        }
                    ),
                },
            ]
        )
        fetch_response = await _complete(client, headers, messages)
        fetch_call = _tool_call(fetch_response)
        fetch_arguments = json.loads(fetch_call["function"]["arguments"])
        assert fetch_call["function"]["name"] == "web.fetch"
        assert fetch_arguments == {
            "url": EVIDENCE_URL,
            "search_tool_call_id": "00000000-0000-4000-8000-000000000001",
        }

        messages.extend(
            [
                {"role": "assistant", "content": None, "tool_calls": [fetch_call]},
                {
                    "role": "tool",
                    "tool_call_id": fetch_call["id"],
                    "content": json.dumps({"output": {"final_url": EVIDENCE_URL}}),
                },
            ]
        )
        final_response = await _complete(client, headers, messages)

    text = "".join(
        event["choices"][0]["delta"].get("content", "")
        for event in final_response
        if event.get("choices")
    )
    assert text == f"Verified offline Web evidence: {EVIDENCE_URL}"


async def _complete(client: AsyncClient, headers: dict, messages: list[dict]) -> list[dict]:
    response = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={
            "model": "web-e2e-fake",
            "messages": messages,
            "stream": True,
            "tools": [
                {"type": "function", "function": {"name": "web.search"}},
                {"type": "function", "function": {"name": "web.fetch"}},
            ],
        },
    )
    assert response.status_code == 200
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


def _tool_call(events: list[dict]) -> dict:
    return next(
        event["choices"][0]["delta"]["tool_calls"][0]
        for event in events
        if event.get("choices") and event["choices"][0]["delta"].get("tool_calls")
    )
