"""Bounded Tenant ceiling used only when creating a fresh local Tenant."""

from __future__ import annotations

import json

_LOCAL_TOOL_CONFIGS: dict[str, dict[str, object]] = {
    "file.read@1.0.0": {"roots": ["workspace"]},
    "file.write@1.0.0": {"roots": ["workspace"]},
    "report.write@1.0.0": {"roots": ["workspace"]},
    "python.execute@1.0.0": {"network": False},
}


def local_tenant_settings() -> dict[str, object]:
    """Return a new deterministic settings object without Web or remote-data grants."""

    return {
        "tool_policy": {
            "allow": sorted(_LOCAL_TOOL_CONFIGS),
            "permissions": [
                "code.python.execute",
                "filesystem.read",
                "filesystem.write",
                "report.write",
            ],
            "secret_refs": {},
            "tools": {key: dict(_LOCAL_TOOL_CONFIGS[key]) for key in sorted(_LOCAL_TOOL_CONFIGS)},
        },
        "skill_policy": {
            "enabled": False,
            "allowed_skill_ids": [],
            "scopes": [],
        },
    }


def main() -> None:
    print(json.dumps(local_tenant_settings(), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
