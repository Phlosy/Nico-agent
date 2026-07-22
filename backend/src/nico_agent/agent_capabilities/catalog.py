"""Server-owned capability profiles and live availability resolution."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from nico_agent.agent_capabilities.contracts import (
    CapabilityProfileRead,
    SkillCapabilityRead,
    ToolCapabilityRead,
)
from nico_agent.domain.models import AgentVersion, Skill, SkillVersion, ToolDefinition
from nico_agent.tools.builtin import (
    DatabaseReadExecutor,
    FileReadExecutor,
    FileWriteExecutor,
    HttpReadExecutor,
    PythonSandboxExecutor,
    ReportWriteExecutor,
    WebFetchExecutor,
    WebSearchExecutor,
)
from nico_agent.tools.contracts import ToolDefinitionSpec

FILE_READ = "file.read@1.0.0"
FILE_WRITE = "file.write@1.0.0"
REPORT_WRITE = "report.write@1.0.0"
PYTHON_EXECUTE = "python.execute@1.0.0"
WEB_SEARCH = "web.search@1.0.0"
WEB_FETCH = "web.fetch@1.1.0"
HTTP_READ = "http.read@1.0.0"
DATABASE_READ = "database.read@1.0.0"

_BUILTIN_SPECS = tuple(
    sorted(
        (
            FileReadExecutor.spec,
            FileWriteExecutor.spec,
            ReportWriteExecutor.spec,
            PythonSandboxExecutor.spec,
            WebSearchExecutor.spec,
            WebFetchExecutor.spec,
            HttpReadExecutor.spec,
            DatabaseReadExecutor.spec,
        ),
        key=lambda item: item.reference,
    )
)


def maintained_profiles(*, web_ready: bool) -> tuple[CapabilityProfileRead, ...]:
    return (
        CapabilityProfileRead(
            key="minimal",
            label="Minimal",
            description="No optional Tools or Skills.",
        ),
        CapabilityProfileRead(
            key="web_research",
            label="Web Research",
            description="Search and fetch public Web sources without local mutation tools.",
            recommended=web_ready,
        ),
        CapabilityProfileRead(
            key="developer",
            label="Developer",
            description="Workspace files, reports, Python, and Web when configured.",
            recommended=not web_ready,
        ),
        CapabilityProfileRead(
            key="custom",
            label="Custom",
            description="Choose each currently usable Tool and published Skill.",
        ),
    )


def profile_tool_refs(profile: str, *, web_ready: bool) -> tuple[str, ...]:
    if profile == "minimal":
        return ()
    if profile == "web_research":
        return (WEB_FETCH, WEB_SEARCH)
    if profile == "developer":
        refs = [FILE_READ, FILE_WRITE, PYTHON_EXECUTE, REPORT_WRITE]
        if web_ready:
            refs.extend((WEB_FETCH, WEB_SEARCH))
        return tuple(sorted(refs))
    raise ValueError(f"unsupported maintained capability profile: {profile}")


def web_state(settings: dict[str, Any]) -> tuple[bool, str | None]:
    configured = _mapping(settings.get("web_provider"))
    candidate_hash = configured.get("candidate_hash")
    ready = (
        configured.get("enabled") is True
        and configured.get("provider") in {"brave", "searxng"}
        and isinstance(candidate_hash, str)
        and len(candidate_hash) == 64
    )
    return ready, candidate_hash if ready else None


def tool_catalog(
    settings: dict[str, Any],
    definitions: Iterable[ToolDefinition],
    *,
    target_version: AgentVersion | None,
) -> tuple[ToolCapabilityRead, ...]:
    tenant_policy = _mapping(settings.get("tool_policy"))
    allowed_refs = _strings(tenant_policy.get("allow"))
    allowed_permissions = _strings(tenant_policy.get("permissions"))
    tool_configs = _mapping(tenant_policy.get("tools"))
    secret_refs = _mapping(tenant_policy.get("secret_refs"))
    web_ready, _candidate_hash = web_state(settings)
    database_rows = {f"{row.name}@{row.version}": row for row in definitions}
    builtin = {spec.reference: spec for spec in _BUILTIN_SPECS}
    references = sorted(set(builtin) | set(database_rows))
    plugin_blocked = bool(target_version is not None and target_version.plugin_refs)
    result: list[ToolCapabilityRead] = []
    for reference in references:
        row = database_rows.get(reference)
        spec = builtin.get(reference)
        name, version = reference.rsplit("@", 1)
        description = spec.description if spec is not None else row.description
        permission = spec.permission if spec is not None else row.permission
        risk = str(spec.risk) if spec is not None else row.risk
        reason_code = None
        reason = None
        if spec is None:
            reason_code = "RUNTIME_EXECUTOR_UNAVAILABLE"
            reason = "No built-in runtime executor is installed for this Tool definition."
        elif row is not None and row.status == "disabled":
            reason_code = "TOOL_DISABLED"
            reason = "The Tool definition is disabled."
        elif plugin_blocked:
            reason_code = "PLUGIN_PERMISSION_LAYER_UNAVAILABLE"
            reason = "Tools cannot be granted while this Agent uses Plugins."
        elif reference not in allowed_refs or permission not in allowed_permissions:
            reason_code = "TENANT_POLICY_DENIED"
            reason = "The Tenant capability ceiling does not authorize this Tool and permission."
        elif reference in {WEB_SEARCH, WEB_FETCH} and not web_ready:
            reason_code = "WEB_PROVIDER_REQUIRED"
            reason = "Verify and activate a Web Provider first."
        elif reference == HTTP_READ and not _string_list(
            _mapping(tool_configs.get(reference)).get("allowed_domains")
        ):
            reason_code = "HTTP_ENDPOINT_POLICY_REQUIRED"
            reason = "Configure at least one allowed HTTP domain in the Tenant Tool policy."
        elif reference == DATABASE_READ and not isinstance(
            _mapping(tool_configs.get(reference)).get("source"), str
        ):
            reason_code = "DATABASE_SOURCE_REQUIRED"
            reason = "Configure a read-only database source in the Tenant Tool policy."
        elif reference == DATABASE_READ and not isinstance(secret_refs.get("database_url"), str):
            reason_code = "DATABASE_SECRET_REQUIRED"
            reason = "Configure the database_url Secret reference for the read-only source."
        result.append(
            ToolCapabilityRead(
                reference=reference,
                name=name,
                version=version,
                description=description,
                permission=permission,
                risk=risk,
                usable=reason_code is None,
                reason_code=reason_code,
                reason=reason,
            )
        )
    return tuple(result)


def skill_catalog(
    settings: dict[str, Any],
    rows: Iterable[tuple[Skill, SkillVersion]],
    *,
    target_agent_id: UUID | None,
) -> tuple[SkillCapabilityRead, ...]:
    tenant_policy = _mapping(settings.get("skill_policy"))
    enabled = tenant_policy.get("enabled") is True
    allowed_ids = _uuid_strings(tenant_policy.get("allowed_skill_ids"))
    allowed_versions = _uuid_strings(tenant_policy.get("allowed_skill_version_ids"))
    allowed_scopes = _strings(tenant_policy.get("scopes"))
    result: list[SkillCapabilityRead] = []
    for skill, version in sorted(rows, key=lambda row: (row[0].name, row[1].version, row[1].id)):
        reason_code = None
        reason = None
        published = (
            skill.status == "published"
            and version.status == "published"
            and skill.current_version_id == version.id
            and version.approved_at is not None
            and version.published_at is not None
        )
        if not published:
            reason_code = "SKILL_NOT_PUBLISHED"
            reason = "Only the current approved published Skill version can be granted."
        elif not enabled or skill.id not in allowed_ids:
            reason_code = "TENANT_SKILL_POLICY_DENIED"
            reason = "The Tenant Skill policy does not authorize this Skill."
        elif allowed_versions and version.id not in allowed_versions:
            reason_code = "TENANT_SKILL_VERSION_DENIED"
            reason = "The Tenant Skill policy does not authorize this exact version."
        elif skill.scope_type not in allowed_scopes:
            reason_code = "SKILL_SCOPE_DENIED"
            reason = "The Tenant Skill policy does not authorize this Skill scope."
        elif skill.scope_type == "project":
            reason_code = "PROJECT_CONTEXT_REQUIRED"
            reason = "Project-scoped Skills require a project-aware capability flow."
        elif skill.scope_type == "agent" and skill.agent_id != target_agent_id:
            reason_code = "AGENT_SCOPE_MISMATCH"
            reason = "This Skill belongs to a different Agent."
        result.append(
            SkillCapabilityRead(
                skill_id=skill.id,
                skill_version_id=version.id,
                name=skill.name,
                version=version.version,
                description=skill.description,
                scope=skill.scope_type,
                trust="published" if published else "unavailable",
                usable=reason_code is None,
                reason_code=reason_code,
                reason=reason,
            )
        )
    return tuple(result)


def builtin_spec(reference: str) -> ToolDefinitionSpec | None:
    return next((spec for spec in _BUILTIN_SPECS if spec.reference == reference), None)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _strings(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str) and item}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _uuid_strings(value: Any) -> set[UUID]:
    result: set[UUID] = set()
    if not isinstance(value, list):
        return result
    for item in value:
        try:
            result.add(UUID(str(item)))
        except (TypeError, ValueError):
            continue
    return result
