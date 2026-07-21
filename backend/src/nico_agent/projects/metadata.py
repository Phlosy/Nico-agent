"""Shared interpretation of Project collaboration metadata."""

from __future__ import annotations

from typing import Any

from nico_agent.domain.models import Project

MANAGED_PROJECT_METADATA_KEY = "_nico_collaboration"


def managed_project_metadata(project: Project) -> dict[str, Any]:
    value = project.metadata_json.get(MANAGED_PROJECT_METADATA_KEY, {})
    return value if isinstance(value, dict) else {}


def is_managed_project(project: Project) -> bool:
    return managed_project_metadata(project).get("managed") is True
