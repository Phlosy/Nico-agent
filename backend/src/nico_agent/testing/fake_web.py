"""Deterministic Web fixtures for offline search/fetch acceptance tests."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from nico_agent.net.safe_http import PinnedRequest, RawHttpResponse

SEARCH_HOST = "fake-web"
EVIDENCE_HOST = "evidence.example"
EVIDENCE_URL = f"https://{EVIDENCE_HOST}/article"

app = FastAPI(title="Nico Offline Web Fixture", docs_url=None, redoc_url=None)


def search_payload() -> dict[str, object]:
    """Return the stable SearXNG-shaped response used by both fixture modes."""

    return {
        "query": "Nico Web Provider connectivity check",
        "results": [
            {
                "title": "Nico offline evidence",
                "url": EVIDENCE_URL,
                "content": "Deterministic current evidence for the Nico Web E2E.",
                "engine": "fixture",
            }
        ],
    }


def article_html() -> bytes:
    return (
        b"<!doctype html><html><head><title>Nico offline evidence</title></head>"
        b"<body><main><h1>Nico Web evidence</h1>"
        b"<p>The offline search and fetch chain completed successfully.</p>"
        b"</main></body></html>"
    )


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/search")
async def search() -> JSONResponse:
    return JSONResponse(search_payload())


@app.get("/article")
async def article() -> HTMLResponse:
    return HTMLResponse(article_html())


@app.get("/redirect")
async def redirect() -> RedirectResponse:
    return RedirectResponse(EVIDENCE_URL, status_code=302)


@app.get("/private-target")
async def private_target() -> RedirectResponse:
    return RedirectResponse("http://169.254.169.254/latest/meta-data", status_code=302)


@app.get("/status/{status_code}")
async def status(status_code: int) -> Response:
    if status_code not in {429, 500, 502, 503}:
        return Response(status_code=400)
    headers = {"Retry-After": "1"} if status_code == 429 else None
    return JSONResponse({"status": status_code}, status_code=status_code, headers=headers)


@app.get("/slow")
async def slow() -> HTMLResponse:
    await asyncio.sleep(0.25)
    return HTMLResponse(article_html())


class FakeWebTransport:
    """SafeHttp transport with the same behavior as the fixture ASGI app."""

    def __init__(self) -> None:
        self.requests: list[PinnedRequest] = []

    async def request(self, request: PinnedRequest) -> RawHttpResponse:
        self.requests.append(request)
        path = urlsplit(request.target).path
        if request.hostname == SEARCH_HOST and path == "/search":
            return _json_response(200, search_payload())
        if request.hostname == EVIDENCE_HOST and path == "/article":
            return RawHttpResponse(
                200,
                (("Content-Type", "text/html; charset=utf-8"),),
                article_html(),
            )
        if path == "/redirect":
            return RawHttpResponse(302, (("Location", EVIDENCE_URL),), b"")
        if path == "/private-target":
            return RawHttpResponse(
                302,
                (("Location", "http://169.254.169.254/latest/meta-data"),),
                b"",
            )
        if path.startswith("/status/"):
            try:
                status_code = int(path.rsplit("/", 1)[-1])
            except ValueError:
                status_code = 400
            headers = (("Retry-After", "1"),) if status_code == 429 else ()
            return RawHttpResponse(status_code, headers, b"{}")
        if path == "/slow":
            await asyncio.sleep(0.25)
            return RawHttpResponse(
                200,
                (("Content-Type", "text/html; charset=utf-8"),),
                article_html(),
            )
        return _json_response(404, {"detail": "not found"})


def fake_web_resolver(hostname: str, _port: int) -> list[str]:
    """Resolve fixture names while preserving private/public safety semantics."""

    if hostname == SEARCH_HOST:
        return ["127.0.0.1"]
    if hostname == EVIDENCE_HOST:
        return ["93.184.216.34"]
    if hostname == "169.254.169.254":
        return ["169.254.169.254"]
    raise OSError("fixture hostname is unknown")


def _json_response(status: int, payload: dict[str, object]) -> RawHttpResponse:
    return RawHttpResponse(
        status,
        (("Content-Type", "application/json"),),
        json.dumps(payload, separators=(",", ":")).encode(),
    )
