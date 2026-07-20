"""Fail-closed coordination policy composition and permission narrowing."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from nico_agent.coordination.contracts import DelegationIntent


def build_coordination_policy_snapshot(
    tenant_settings: dict[str, Any],
    agent_policy: dict[str, Any],
) -> dict[str, Any]:
    """Freeze the exact tenant/Agent intersection used for one RuntimeSession."""

    tenant = _mapping(tenant_settings.get("coordination_policy"))
    agent = _mapping(agent_policy)
    errors: list[str] = []
    enabled = tenant.get("enabled") is True and agent.get("enabled") is True
    tenant_versions = _string_set(tenant.get("allowed_agent_version_ids"))
    agent_versions = _string_set(agent.get("allowed_agent_version_ids"))
    tenant_secrets = _string_set(tenant.get("allowed_secret_refs"))
    agent_secrets = _string_set(agent.get("allowed_secret_refs"))
    if any("*" in item for item in tenant_versions | agent_versions):
        enabled = False
        errors.append("wildcard AgentVersion grants are unsupported")
    if any("*" in item for item in tenant_secrets | agent_secrets):
        enabled = False
        errors.append("wildcard secret grants are unsupported")

    return {
        "version": 1,
        "enabled": enabled,
        "max_depth": _restrict_positive_int(tenant.get("max_depth"), agent.get("max_depth")),
        "max_children": _restrict_positive_int(
            tenant.get("max_children"), agent.get("max_children")
        ),
        "max_parallelism": _restrict_positive_int(
            tenant.get("max_parallelism"), agent.get("max_parallelism")
        ),
        "allowed_agent_version_ids": sorted(tenant_versions & agent_versions),
        "allowed_secret_refs": sorted(tenant_secrets & agent_secrets),
        "errors": errors,
    }


def narrow_child_permissions(
    *,
    parent: dict[str, Any],
    child: dict[str, Any],
    restrictions: dict[str, Any],
) -> dict[str, Any]:
    """Create Parent ∩ Child ∩ Delegation permissions without resolving secrets."""

    parent_endpoint = _optional_string(parent.get("model_endpoint_id"))
    child_endpoint = _optional_string(child.get("model_endpoint_id")) or parent_endpoint
    if parent_endpoint != child_endpoint:
        raise ValueError("child model endpoint cannot differ from the parent snapshot")
    parent_model = _optional_string(parent.get("model"))
    child_model = _optional_string(child.get("model")) or parent_model
    if parent_model != child_model:
        raise ValueError("child model cannot differ from the parent snapshot")

    allow = (
        _string_set(parent.get("allow"))
        & _string_set(child.get("allow"))
        & _restriction_set(restrictions, "allow", parent, child)
    )
    permissions = (
        _string_set(parent.get("permissions"))
        & _string_set(child.get("permissions"))
        & _restriction_set(restrictions, "permissions", parent, child)
    )
    parent_refs = {
        name: reference
        for name, reference in _mapping(parent.get("secret_refs")).items()
        if isinstance(reference, str) and reference
    }
    secret_names = (
        set(parent_refs)
        & _string_set(child.get("secrets"))
        & _restriction_set(restrictions, "secrets", parent, child, default=set(parent_refs))
    )
    return {
        "version": 1,
        "allow": sorted(allow),
        "permissions": sorted(permissions),
        "secret_refs": {name: parent_refs[name] for name in sorted(secret_names)},
        "model_endpoint_id": parent_endpoint,
        "model": parent_model,
    }


def narrow_coordination_policy(parent: dict[str, Any], child: dict[str, Any]) -> dict[str, Any]:
    """Prevent a Child Run from regaining coordination authority its parent lacked."""

    errors = [
        *[item for item in parent.get("errors", []) if isinstance(item, str)],
        *[item for item in child.get("errors", []) if isinstance(item, str)],
    ]
    return {
        "version": 1,
        "enabled": parent.get("enabled") is True and child.get("enabled") is True,
        "max_depth": _restrict_positive_int(parent.get("max_depth"), child.get("max_depth")),
        "max_children": _restrict_positive_int(
            parent.get("max_children"), child.get("max_children")
        ),
        "max_parallelism": _restrict_positive_int(
            parent.get("max_parallelism"), child.get("max_parallelism")
        ),
        "allowed_agent_version_ids": sorted(
            _string_set(parent.get("allowed_agent_version_ids"))
            & _string_set(child.get("allowed_agent_version_ids"))
        ),
        "allowed_secret_refs": sorted(
            _string_set(parent.get("allowed_secret_refs"))
            & _string_set(child.get("allowed_secret_refs"))
        ),
        "errors": errors,
    }


def delegation_fingerprint(intent: DelegationIntent) -> str:
    """Return a stable duplicate/loop guard independent of idempotency and ref ordering."""

    payload = {
        "target_agent_version_id": str(intent.target_agent_version_id),
        "objective": intent.objective.strip(),
        "acceptance": intent.acceptance,
        "context_refs": sorted(set(intent.context_refs)),
        "permission_restrictions": intent.permission_restrictions,
        "execution_mode": intent.execution_mode,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {item for item in value if isinstance(item, str) and item}


def _restrict_positive_int(tenant: Any, agent: Any) -> int:
    values = [
        item
        for item in (tenant, agent)
        if isinstance(item, int) and not isinstance(item, bool) and item > 0
    ]
    return min(values) if len(values) == 2 else 0


def _restriction_set(
    restrictions: dict[str, Any],
    key: str,
    parent: dict[str, Any],
    child: dict[str, Any],
    *,
    default: set[str] | None = None,
) -> set[str]:
    if key in restrictions:
        return _string_set(restrictions.get(key))
    if default is not None:
        return default
    return _string_set(parent.get(key)) | _string_set(child.get(key))


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
