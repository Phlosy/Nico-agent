from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from nico_agent.agent_capabilities.catalog import (
    DATABASE_READ,
    HTTP_READ,
    PYTHON_EXECUTE,
    WEB_FETCH,
    WEB_SEARCH,
    maintained_profiles,
    profile_tool_refs,
    skill_catalog,
    tool_catalog,
)
from nico_agent.local_defaults import local_tenant_settings


def test_profiles_are_stable_and_web_research_is_recommended_when_ready() -> None:
    profiles = maintained_profiles(web_ready=True)
    assert [item.key for item in profiles] == [
        "minimal",
        "web_research",
        "developer",
        "custom",
    ]
    assert next(item for item in profiles if item.recommended).key == "web_research"
    assert profile_tool_refs("minimal", web_ready=True) == ()
    assert profile_tool_refs("web_research", web_ready=True) == (WEB_FETCH, WEB_SEARCH)
    assert HTTP_READ not in profile_tool_refs("developer", web_ready=True)
    assert DATABASE_READ not in profile_tool_refs("developer", web_ready=True)
    assert PYTHON_EXECUTE in profile_tool_refs("developer", web_ready=False)


def test_local_ceiling_makes_only_self_contained_tools_usable() -> None:
    settings = local_tenant_settings()
    catalog = tool_catalog(settings, (), target_version=None)
    by_ref = {item.reference: item for item in catalog}
    assert {ref for ref, item in by_ref.items() if item.usable} == {
        "file.read@1.0.0",
        "file.write@1.0.0",
        "report.write@1.0.0",
        PYTHON_EXECUTE,
    }
    assert by_ref[WEB_SEARCH].reason_code == "TENANT_POLICY_DENIED"
    assert by_ref[HTTP_READ].reason_code == "TENANT_POLICY_DENIED"
    assert by_ref[DATABASE_READ].reason_code == "TENANT_POLICY_DENIED"


def test_endpoint_tools_explain_their_specific_missing_dependencies() -> None:
    settings = local_tenant_settings()
    policy = settings["tool_policy"]
    policy["allow"].extend([HTTP_READ, DATABASE_READ])
    policy["permissions"].extend(["network.http.read", "database.read"])
    policy["tools"].update({HTTP_READ: {}, DATABASE_READ: {}})
    by_ref = {item.reference: item for item in tool_catalog(settings, (), target_version=None)}
    assert by_ref[HTTP_READ].reason_code == "HTTP_ENDPOINT_POLICY_REQUIRED"
    assert by_ref[DATABASE_READ].reason_code == "DATABASE_SOURCE_REQUIRED"


def test_web_and_plugins_are_resolved_live() -> None:
    settings = local_tenant_settings()
    policy = settings["tool_policy"]
    policy["allow"].extend([WEB_SEARCH, WEB_FETCH])
    policy["permissions"].extend(["network.web.search", "network.web.fetch"])
    policy["tools"].update({WEB_SEARCH: {}, WEB_FETCH: {}})
    not_ready = {item.reference: item for item in tool_catalog(settings, (), target_version=None)}
    assert not_ready[WEB_SEARCH].reason_code == "WEB_PROVIDER_REQUIRED"
    settings["web_provider"] = {
        "enabled": True,
        "provider": "searxng",
        "candidate_hash": "a" * 64,
    }
    ready = {item.reference: item for item in tool_catalog(settings, (), target_version=None)}
    assert ready[WEB_SEARCH].usable is True
    plugin_version = SimpleNamespace(plugin_refs=[{"name": "example"}])
    blocked = {
        item.reference: item for item in tool_catalog(settings, (), target_version=plugin_version)
    }
    assert blocked[WEB_SEARCH].reason_code == "PLUGIN_PERMISSION_LAYER_UNAVAILABLE"


def test_skill_catalog_pins_only_current_tenant_authorized_published_version() -> None:
    skill_id = uuid4()
    version_id = uuid4()
    now = datetime.now(UTC)
    skill = SimpleNamespace(
        id=skill_id,
        name="research-summary",
        description="Summarize researched sources",
        scope_type="tenant",
        agent_id=None,
        status="published",
        current_version_id=version_id,
    )
    version = SimpleNamespace(
        id=version_id,
        version=3,
        status="published",
        approved_at=now,
        published_at=now,
    )
    settings = {
        "skill_policy": {
            "enabled": True,
            "allowed_skill_ids": [str(skill_id)],
            "allowed_skill_version_ids": [str(version_id)],
            "scopes": ["tenant"],
        }
    }
    item = skill_catalog(settings, ((skill, version),), target_agent_id=None)[0]
    assert item.usable is True
    assert item.skill_version_id == version_id
    denied = skill_catalog(
        {"skill_policy": {"enabled": False}},
        ((skill, version),),
        target_agent_id=None,
    )[0]
    assert denied.reason_code == "TENANT_SKILL_POLICY_DENIED"
