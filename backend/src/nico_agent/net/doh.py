"""Deployment-owned DNS-over-HTTPS resolver for public Web targets."""

from __future__ import annotations

import ipaddress
import json
from typing import Any

from nico_agent.net.safe_http import (
    SafeHttpClient,
    SafeHttpError,
    SafeHttpPolicy,
    canonicalize_http_url,
    normalize_response_headers,
)

DOH_ENDPOINTS = {
    "cloudflare": "https://cloudflare-dns.com/dns-query",
    "google": "https://dns.google/resolve",
}


class DnsOverHttpsResolver:
    """Resolve target names through one exact HTTPS endpoint.

    The endpoint may bootstrap through a deployment-approved private/Fake-IP
    address, but returned target addresses are still validated by the caller's
    SafeHttpPolicy before any target connection is attempted.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        http: SafeHttpClient | None = None,
        allow_private_bootstrap: bool = False,
    ) -> None:
        parsed = canonicalize_http_url(endpoint)
        if parsed.scheme != "https" or parsed.query:
            raise ValueError("DNS-over-HTTPS endpoint must be an HTTPS URL without a query")
        self.endpoint = parsed.url
        self.http = http or SafeHttpClient()
        self.policy = SafeHttpPolicy.exact_endpoint(
            self.endpoint,
            allow_private=allow_private_bootstrap,
        )

    async def __call__(self, hostname: str, _port: int) -> tuple[str, ...]:
        addresses: list[str] = []
        for record_name, record_type, version in (("A", 1, 4), ("AAAA", 28, 6)):
            response = await self.http.request(
                "GET",
                self.endpoint,
                policy=self.policy,
                headers={"Accept": "application/dns-json"},
                params={"name": hostname, "type": record_name},
                max_response_bytes=65_536,
            )
            if response.status != 200:
                raise SafeHttpError("DNS_ERROR", "DNS-over-HTTPS request failed")
            content_type = normalize_response_headers(response.headers).get("content-type", "")
            if content_type.split(";", 1)[0].strip().lower() not in {
                "application/dns-json",
                "application/json",
            }:
                raise SafeHttpError("DNS_ERROR", "DNS-over-HTTPS response type is invalid")
            payload = _json_object(response.body)
            if payload.get("Status") != 0:
                raise SafeHttpError("DNS_ERROR", "DNS-over-HTTPS resolution failed")
            answers = payload.get("Answer", [])
            if not isinstance(answers, list):
                raise SafeHttpError("DNS_ERROR", "DNS-over-HTTPS response is invalid")
            for answer in answers:
                if not isinstance(answer, dict) or answer.get("type") != record_type:
                    continue
                value = answer.get("data")
                try:
                    address = ipaddress.ip_address(value)
                except ValueError as exc:
                    raise SafeHttpError(
                        "DNS_ERROR", "DNS-over-HTTPS returned an invalid address"
                    ) from exc
                if address.version != version:
                    raise SafeHttpError(
                        "DNS_ERROR", "DNS-over-HTTPS returned an invalid address family"
                    )
                addresses.append(str(address))
        result = tuple(dict.fromkeys(addresses))
        if not result:
            raise SafeHttpError("DNS_ERROR", "DNS-over-HTTPS returned no address")
        return result


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SafeHttpError("DNS_ERROR", "DNS-over-HTTPS response is invalid") from exc
    if not isinstance(payload, dict):
        raise SafeHttpError("DNS_ERROR", "DNS-over-HTTPS response is invalid")
    return payload
