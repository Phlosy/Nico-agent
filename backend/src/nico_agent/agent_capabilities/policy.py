"""Pure compilation and preview helpers for capability proposals."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any
from uuid import UUID

from nico_agent.agent_capabilities.catalog import builtin_spec
from nico_agent.agent_capabilities.contracts import (
    CapabilityDiffRead,
    SkillCapabilityRead,
    ToolCapabilityRead,
)


def compile_tool_policy(
    tenant_settings: dict[str, Any],
    selected_refs: tuple[str, ...],
    catalog: tuple[ToolCapabilityRead, ...],
) -> dict[str, Any]:
    items = {item.reference: item for item in catalog}
    _require_usable("Tool", selected_refs, items)
    tenant_policy = _mapping(tenant_settings.get("tool_policy"))
    tenant_configs = _mapping(tenant_policy.get("tools"))
    tenant_secret_refs = _mapping(tenant_policy.get("secret_refs"))
    permissions = sorted({items[reference].permission for reference in selected_refs})
    secrets: set[str] = set()
    for reference in selected_refs:
        spec = builtin_spec(reference)
        if spec is not None:
            secrets.update(name for name in spec.secret_names if name in tenant_secret_refs)
    return {
        "allow": sorted(selected_refs),
        "permissions": permissions,
        "tools": {
            reference: deepcopy(_mapping(tenant_configs.get(reference)))
            for reference in sorted(selected_refs)
        },
        "secrets": sorted(secrets),
    }


def compile_skill_policy(
    tenant_settings: dict[str, Any],
    selected_versions: tuple[UUID, ...],
    catalog: tuple[SkillCapabilityRead, ...],
    *,
    base_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    items = {item.skill_version_id: item for item in catalog}
    _require_usable("Skill", selected_versions, items)
    selected = [items[value] for value in selected_versions]
    tenant_policy = _mapping(tenant_settings.get("skill_policy"))
    base = _mapping(base_policy)
    return {
        "enabled": bool(selected),
        "pin_versions": True,
        "scopes": sorted({item.scope for item in selected}),
        "allowed_skill_ids": sorted({str(item.skill_id) for item in selected}),
        "allowed_skill_version_ids": sorted(str(item.skill_version_id) for item in selected),
        "top_k": _bounded_limit(base, tenant_policy, "top_k", 5, 100),
        "max_chars": _bounded_limit(base, tenant_policy, "max_chars", 16_000, 256_000),
        "max_tokens": _bounded_limit(base, tenant_policy, "max_tokens", 4_000, 64_000),
    }


def elevated_risks(
    selected_refs: tuple[str, ...],
    catalog: tuple[ToolCapabilityRead, ...],
) -> tuple[str, ...]:
    items = {item.reference: item for item in catalog}
    return tuple(
        sorted(
            {items[reference].risk for reference in selected_refs if items[reference].risk != "low"}
        )
    )


def capability_diff(
    current_tool_policy: dict[str, Any],
    current_skill_policy: dict[str, Any],
    proposed_tool_policy: dict[str, Any],
    proposed_skill_policy: dict[str, Any],
) -> CapabilityDiffRead:
    current_tools = _strings(current_tool_policy.get("allow"))
    proposed_tools = _strings(proposed_tool_policy.get("allow"))
    current_skills = _uuids(current_skill_policy.get("allowed_skill_version_ids"))
    proposed_skills = _uuids(proposed_skill_policy.get("allowed_skill_version_ids"))
    return CapabilityDiffRead(
        tools_added=tuple(sorted(proposed_tools - current_tools)),
        tools_removed=tuple(sorted(current_tools - proposed_tools)),
        skills_added=tuple(sorted(proposed_skills - current_skills, key=str)),
        skills_removed=tuple(sorted(current_skills - proposed_skills, key=str)),
        model_route_changed=False,
    )


def preview_hash(projection: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            {"schema_version": 1, "projection": projection},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _require_usable(kind: str, values: tuple[Any, ...], items: dict[Any, Any]) -> None:
    missing = [str(value) for value in values if value not in items]
    unavailable = [
        {"id": str(value), "reason_code": items[value].reason_code}
        for value in values
        if value in items and not items[value].usable
    ]
    if missing or unavailable:
        from nico_agent.domain.errors import DomainConflict

        raise DomainConflict(
            "CAPABILITY_UNAVAILABLE",
            f"one or more selected {kind} capabilities are unavailable",
            details={"missing": sorted(missing), "unavailable": unavailable},
        )


def _bounded_limit(
    base: dict[str, Any],
    tenant: dict[str, Any],
    key: str,
    default: int,
    maximum: int,
) -> int:
    values = [
        value
        for value in (base.get(key), tenant.get(key))
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    ]
    return min([*values, default, maximum])


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _strings(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str) and item}


def _uuids(value: Any) -> set[UUID]:
    result: set[UUID] = set()
    if not isinstance(value, list):
        return result
    for item in value:
        try:
            result.add(UUID(str(item)))
        except (TypeError, ValueError):
            continue
    return result
