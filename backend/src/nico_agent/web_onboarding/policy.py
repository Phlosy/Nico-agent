"""Pure Web onboarding policy projection helpers."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any
from uuid import UUID

from nico_agent.web_onboarding.contracts import WebProviderCandidate

SEARCH_REF = "web.search@1.0.0"
FETCH_REF = "web.fetch@1.1.0"
LEGACY_FETCH_REFS = frozenset({"web.fetch@1.0.0"})
WEB_REFS = frozenset({SEARCH_REF, FETCH_REF, *LEGACY_FETCH_REFS})
SEARCH_PERMISSION = "network.web.search"
FETCH_PERMISSION = "network.web.fetch"
BRAVE_SECRET = "web_search_brave_api_key"


def merge_tenant_policy(value: Any, candidate: WebProviderCandidate) -> dict[str, Any]:
    policy = deepcopy(value) if isinstance(value, dict) else {}
    policy["allow"] = sorted(
        (set(strings(policy.get("allow"))) - LEGACY_FETCH_REFS) | {SEARCH_REF, FETCH_REF}
    )
    policy["permissions"] = sorted(
        set(strings(policy.get("permissions"))) | {SEARCH_PERMISSION, FETCH_PERMISSION}
    )
    tools = deepcopy(policy.get("tools")) if isinstance(policy.get("tools"), dict) else {}
    for reference in LEGACY_FETCH_REFS:
        tools.pop(reference, None)
    search_config, fetch_config = tool_configs(candidate)
    tools[SEARCH_REF] = search_config
    tools[FETCH_REF] = fetch_config
    policy["tools"] = tools
    secret_refs = (
        deepcopy(policy.get("secret_refs")) if isinstance(policy.get("secret_refs"), dict) else {}
    )
    if candidate.credential_ref is not None:
        secret_refs[BRAVE_SECRET] = candidate.credential_ref
    else:
        secret_refs.pop(BRAVE_SECRET, None)
    policy["secret_refs"] = secret_refs
    return policy


def merge_agent_policy(value: Any, candidate: WebProviderCandidate) -> dict[str, Any]:
    policy = deepcopy(value) if isinstance(value, dict) else {}
    policy["allow"] = sorted(
        (set(strings(policy.get("allow"))) - LEGACY_FETCH_REFS) | {SEARCH_REF, FETCH_REF}
    )
    policy["permissions"] = sorted(
        set(strings(policy.get("permissions"))) | {SEARCH_PERMISSION, FETCH_PERMISSION}
    )
    tools = deepcopy(policy.get("tools")) if isinstance(policy.get("tools"), dict) else {}
    for reference in LEGACY_FETCH_REFS:
        tools.pop(reference, None)
    search_config, fetch_config = tool_configs(candidate)
    tools[SEARCH_REF] = search_config
    tools[FETCH_REF] = fetch_config
    policy["tools"] = tools
    secrets = set(strings(policy.get("secrets")))
    if candidate.credential_ref is not None:
        secrets.add(BRAVE_SECRET)
    else:
        secrets.discard(BRAVE_SECRET)
    policy["secrets"] = sorted(secrets)
    return policy


def tool_configs(candidate: WebProviderCandidate) -> tuple[dict[str, Any], dict[str, Any]]:
    policy = candidate.policy
    return (
        {
            "provider": candidate.provider,
            "safe_search": policy.safe_search,
            "cache_ttl_seconds": policy.cache_ttl_seconds,
            "rate_limit_per_minute": policy.rate_limit_per_minute,
        },
        {
            "allowed_domains": list(policy.allowed_domains),
            "cache_ttl_seconds": policy.cache_ttl_seconds,
            "dns_resolver": policy.dns_resolver,
        },
    )


def remove_tenant_web_policy(value: Any) -> dict[str, Any]:
    policy = deepcopy(value) if isinstance(value, dict) else {}
    policy["allow"] = [item for item in strings(policy.get("allow")) if item not in WEB_REFS]
    policy["permissions"] = [
        item
        for item in strings(policy.get("permissions"))
        if item not in {SEARCH_PERMISSION, FETCH_PERMISSION}
    ]
    tools = deepcopy(policy.get("tools")) if isinstance(policy.get("tools"), dict) else {}
    for reference in WEB_REFS:
        tools.pop(reference, None)
    policy["tools"] = tools
    secret_refs = (
        deepcopy(policy.get("secret_refs")) if isinstance(policy.get("secret_refs"), dict) else {}
    )
    secret_refs.pop(BRAVE_SECRET, None)
    policy["secret_refs"] = secret_refs
    return policy


def remove_agent_web_policy(value: Any) -> dict[str, Any]:
    policy = deepcopy(value) if isinstance(value, dict) else {}
    policy["allow"] = [item for item in strings(policy.get("allow")) if item not in WEB_REFS]
    policy["permissions"] = [
        item
        for item in strings(policy.get("permissions"))
        if item not in {SEARCH_PERMISSION, FETCH_PERMISSION}
    ]
    tools = deepcopy(policy.get("tools")) if isinstance(policy.get("tools"), dict) else {}
    for reference in WEB_REFS:
        tools.pop(reference, None)
    policy["tools"] = tools
    policy["secrets"] = [item for item in strings(policy.get("secrets")) if item != BRAVE_SECRET]
    return policy


def strings(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def preview_hash(probe_id: UUID, candidate_hash: str, projection: dict[str, Any]) -> str:
    encoded = json.dumps(
        {
            "schema_version": 1,
            "probe_id": str(probe_id),
            "candidate_hash": candidate_hash,
            "projection": projection,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def disable_preview_hash(projection: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"schema_version": 1, "projection": projection},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
