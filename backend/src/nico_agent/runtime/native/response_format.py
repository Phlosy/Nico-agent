"""Capability-driven JSON response-format selection for Native Runtime calls."""

from __future__ import annotations

from typing import Any


def select_json_response_format(
    endpoint: dict[str, Any],
    requested: dict[str, Any],
) -> dict[str, Any] | None:
    """Select the strongest endpoint-supported JSON constraint.

    A JSON Schema request may be weakened to JSON Object at the provider wire
    boundary; local parsing remains authoritative for schema validation.
    """

    capabilities = endpoint.get("capabilities")
    if not isinstance(capabilities, dict):
        return None
    if requested.get("type") == "json_schema" and capabilities.get("json_schema") is True:
        return requested
    if capabilities.get("json_object") is True:
        return {"type": "json_object"}
    return None


def supports_json_response(endpoint: dict[str, Any]) -> bool:
    capabilities = endpoint.get("capabilities")
    return isinstance(capabilities, dict) and (
        capabilities.get("json_schema") is True or capabilities.get("json_object") is True
    )
