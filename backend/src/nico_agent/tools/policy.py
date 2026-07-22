"""Fail-closed composition of tenant and immutable AgentVersion tool policy."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from nico_agent.tools.contracts import ToolDefinitionSpec
from nico_agent.tools.errors import ToolAccessDenied, ToolError, ToolSecretUnavailable


@dataclass(frozen=True, slots=True)
class ToolAuthorization:
    tool_config: dict[str, Any]
    secret_refs: dict[str, str]


def build_tool_policy_snapshot(
    tenant_settings: dict[str, Any],
    agent_policy: dict[str, Any],
    *,
    plugin_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Freeze the restrictive policy intersection without resolving any Secret."""

    tenant_policy = _mapping(tenant_settings.get("tool_policy"))
    agent_policy = _mapping(agent_policy)
    tenant_allow = _string_set(tenant_policy.get("allow"))
    agent_allow = _string_set(agent_policy.get("allow"))
    tenant_permissions = _string_set(tenant_policy.get("permissions"))
    agent_permissions = _string_set(agent_policy.get("permissions"))
    allow = tenant_allow & agent_allow
    permissions = tenant_permissions & agent_permissions
    errors: list[str] = []

    if any("*" in item for item in tenant_allow | agent_allow):
        allow = set()
        errors.append("wildcard tool grants are unsupported")
    if any("*" in item for item in tenant_permissions | agent_permissions):
        permissions = set()
        errors.append("wildcard permission grants are unsupported")
    if plugin_refs:
        allow = set()
        permissions = set()
        errors.append("plugin permission layer is not implemented")

    tenant_secret_refs = {
        str(name): ref
        for name, ref in _mapping(tenant_policy.get("secret_refs")).items()
        if isinstance(name, str) and isinstance(ref, str)
    }
    agent_secret_names = _string_set(agent_policy.get("secrets"))
    secret_refs = {
        name: ref for name, ref in tenant_secret_refs.items() if name in agent_secret_names
    }

    tenant_configs = _mapping(tenant_policy.get("tools"))
    agent_configs = _mapping(agent_policy.get("tools"))
    tool_configs: dict[str, Any] = {}
    for reference in sorted(allow):
        tenant_config = _mapping(tenant_configs.get(reference))
        if reference in agent_configs:
            agent_config = _mapping(agent_configs.get(reference))
            tool_configs[reference] = _restrict_value(tenant_config, agent_config)
        else:
            tool_configs[reference] = tenant_config

    return {
        "version": 1,
        "allow": sorted(allow),
        "permissions": sorted(permissions),
        "secret_refs": dict(sorted(secret_refs.items())),
        "tools": tool_configs,
        "errors": errors,
    }


def authorize_tool(
    snapshot: dict[str, Any],
    spec: ToolDefinitionSpec,
    *,
    secret_name_selector: Callable[[dict[str, Any]], frozenset[str]] | None = None,
) -> ToolAuthorization:
    reference = spec.reference
    allow = _string_set(snapshot.get("allow"))
    permissions = _string_set(snapshot.get("permissions"))
    if snapshot.get("version") != 1:
        raise ToolAccessDenied(spec.name, spec.version, "tool policy snapshot is unavailable")
    if reference not in allow:
        raise ToolAccessDenied(spec.name, spec.version, "tool version is not allowlisted")
    if spec.permission not in permissions:
        raise ToolAccessDenied(spec.name, spec.version, "required permission is not granted")

    configs = _mapping(snapshot.get("tools"))
    tool_config = _mapping(configs.get(reference))
    required_secret_names = spec.secret_names
    if secret_name_selector is not None:
        try:
            required_secret_names = secret_name_selector(deepcopy(tool_config))
        except ToolError:
            raise
        except Exception as exc:
            raise ToolAccessDenied(
                spec.name,
                spec.version,
                "tool Secret requirements are invalid for the frozen configuration",
            ) from exc
        if not isinstance(required_secret_names, frozenset) or any(
            not isinstance(name, str) or not name for name in required_secret_names
        ):
            raise ToolAccessDenied(
                spec.name,
                spec.version,
                "tool Secret requirements are invalid for the frozen configuration",
            )
        if not required_secret_names <= spec.secret_names:
            raise ToolAccessDenied(
                spec.name,
                spec.version,
                "tool Secret requirements exceed the ToolDefinition declaration",
            )

    all_secret_refs = _mapping(snapshot.get("secret_refs"))
    secret_refs: dict[str, str] = {}
    for name in sorted(required_secret_names):
        reference_value = all_secret_refs.get(name)
        if not isinstance(reference_value, str) or not reference_value:
            raise ToolSecretUnavailable(name)
        secret_refs[name] = reference_value

    return ToolAuthorization(
        tool_config=tool_config,
        secret_refs=secret_refs,
    )


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(key, str)}


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str) and item}


def _restrict_value(tenant_value: Any, agent_value: Any) -> Any:
    """Apply an optional Agent restriction without allowing it to widen the tenant value."""

    if isinstance(tenant_value, dict) and isinstance(agent_value, dict):
        return {
            key: _restrict_value(tenant_value[key], agent_value[key])
            for key in tenant_value.keys() & agent_value.keys()
        }
    if isinstance(tenant_value, list) and isinstance(agent_value, list):
        agent_items = set(_hashable_items(agent_value))
        return [item for item in tenant_value if _hashable(item) in agent_items]
    if isinstance(tenant_value, bool) and isinstance(agent_value, bool):
        return tenant_value and agent_value
    if (
        isinstance(tenant_value, (int, float))
        and not isinstance(tenant_value, bool)
        and isinstance(agent_value, (int, float))
        and not isinstance(agent_value, bool)
    ):
        return min(tenant_value, agent_value)
    if tenant_value == agent_value:
        return tenant_value
    return None


def _hashable_items(values: list[Any]) -> list[Any]:
    return [_hashable(value) for value in values]


def _hashable(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((str(key), _hashable(item)) for key, item in value.items()))
    if isinstance(value, list):
        return tuple(_hashable(item) for item in value)
    return value
