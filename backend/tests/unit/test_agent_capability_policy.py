from __future__ import annotations

from uuid import uuid4

from nico_agent.agent_capabilities.catalog import profile_tool_refs, tool_catalog
from nico_agent.agent_capabilities.contracts import SkillCapabilityRead
from nico_agent.agent_capabilities.policy import (
    capability_diff,
    compile_skill_policy,
    compile_tool_policy,
    elevated_risks,
    preview_hash,
)
from nico_agent.local_defaults import local_tenant_settings


def test_minimal_compiles_to_empty_optional_policies() -> None:
    settings = local_tenant_settings()
    tools = tool_catalog(settings, (), target_version=None)
    tool_policy = compile_tool_policy(settings, (), tools)
    skill_policy = compile_skill_policy(settings, (), ())
    assert tool_policy == {
        "allow": [],
        "permissions": [],
        "tools": {},
        "secrets": [],
    }
    assert skill_policy["enabled"] is False
    assert skill_policy["allowed_skill_version_ids"] == []


def test_developer_compilation_is_exact_and_aggregates_risk_once() -> None:
    settings = local_tenant_settings()
    tools = tool_catalog(settings, (), target_version=None)
    selected = profile_tool_refs("developer", web_ready=False)
    policy = compile_tool_policy(settings, selected, tools)
    assert policy["allow"] == sorted(selected)
    assert elevated_risks(selected, tools) == ("high", "medium")
    assert "database.read@1.0.0" not in policy["allow"]


def test_skill_policy_uses_exact_version_ids_and_diff_reports_them() -> None:
    skill_id = uuid4()
    version_id = uuid4()
    item = SkillCapabilityRead(
        skill_id=skill_id,
        skill_version_id=version_id,
        name="test-skill",
        version=2,
        description="A published test Skill",
        scope="tenant",
        trust="published",
        usable=True,
    )
    settings = {
        "skill_policy": {
            "enabled": True,
            "allowed_skill_ids": [str(skill_id)],
            "allowed_skill_version_ids": [str(version_id)],
            "scopes": ["tenant"],
        }
    }
    proposed = compile_skill_policy(settings, (version_id,), (item,))
    assert proposed["allowed_skill_ids"] == [str(skill_id)]
    assert proposed["allowed_skill_version_ids"] == [str(version_id)]
    diff = capability_diff({}, {}, {}, proposed)
    assert diff.skills_added == (version_id,)


def test_preview_hash_is_canonical_and_sensitive_to_selection() -> None:
    left = {"selection": {"tools": ["b", "a"]}, "tenant_revision": 1}
    reordered = {"tenant_revision": 1, "selection": {"tools": ["b", "a"]}}
    changed = {"tenant_revision": 1, "selection": {"tools": ["a"]}}
    assert preview_hash(left) == preview_hash(reordered)
    assert preview_hash(left) != preview_hash(changed)
