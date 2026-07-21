from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from nico_agent.coordination.contracts import BudgetGrant, DelegationIntent
from nico_agent.coordination.policy import (
    build_coordination_policy_snapshot,
    delegation_fingerprint,
    narrow_child_permissions,
)


def test_coordination_policy_is_fail_closed_and_only_restricts_parent() -> None:
    snapshot = build_coordination_policy_snapshot(
        {
            "coordination_policy": {
                "enabled": True,
                "max_depth": 5,
                "max_children": 8,
                "max_parallelism": 4,
                "allowed_agent_version_ids": ["v1", "v2"],
                "allowed_secret_refs": ["search", "market-data"],
            }
        },
        {
            "enabled": True,
            "max_depth": 3,
            "max_children": 4,
            "max_parallelism": 2,
            "allowed_agent_version_ids": ["v2", "v3"],
            "allowed_secret_refs": ["search"],
        },
    )

    assert snapshot == {
        "version": 1,
        "enabled": True,
        "max_depth": 3,
        "max_children": 4,
        "max_parallelism": 2,
        "allowed_agent_version_ids": ["v2"],
        "allowed_secret_refs": ["search"],
        "errors": [],
    }
    assert build_coordination_policy_snapshot({}, {})["enabled"] is False


def test_project_member_scope_freezes_concrete_versions_without_wildcards() -> None:
    tenant = {
        "coordination_policy": {
            "enabled": True,
            "allowed_target_scopes": ["project_members"],
            "max_depth": 3,
            "max_children": 8,
            "max_parallelism": 4,
        }
    }
    agent = {
        "enabled": True,
        "allowed_target_scopes": ["project_members"],
        "max_depth": 2,
        "max_children": 4,
        "max_parallelism": 2,
    }

    snapshot = build_coordination_policy_snapshot(
        tenant,
        agent,
        project_member_version_ids=["member-b", "member-a", "member-a"],
    )

    assert snapshot["enabled"] is True
    assert snapshot["target_scope"] == "project_members"
    assert snapshot["allowed_agent_version_ids"] == ["member-a", "member-b"]
    assert "*" not in snapshot["allowed_agent_version_ids"]
    denied = build_coordination_policy_snapshot(
        tenant,
        {**agent, "allowed_target_scopes": []},
        project_member_version_ids=["member-a"],
    )
    assert denied["enabled"] is False
    assert denied["allowed_agent_version_ids"] == []


def test_child_permissions_are_exact_intersection_and_never_contain_secret_values() -> None:
    narrowed = narrow_child_permissions(
        parent={
            "allow": ["http.read@1.0.0", "file.read@1.0.0"],
            "permissions": ["network.read", "filesystem.read"],
            "secret_refs": {
                "authorization": "env:NICO_TOOL_SECRET_HTTP_AUTH",
                "tenant_only": "env:NICO_TOOL_SECRET_TENANT_ONLY",
            },
            "model_endpoint_id": "endpoint-a",
            "model": "model-a",
        },
        child={
            "allow": ["http.read@1.0.0", "shell.exec@1.0.0"],
            "permissions": ["network.read", "process.exec"],
            "secrets": ["authorization"],
            "model_endpoint_id": "endpoint-a",
            "model": "model-a",
        },
        restrictions={
            "allow": ["http.read@1.0.0"],
            "permissions": ["network.read"],
            "secrets": ["authorization", "unknown"],
        },
    )

    assert narrowed["allow"] == ["http.read@1.0.0"]
    assert narrowed["permissions"] == ["network.read"]
    assert narrowed["secret_refs"] == {"authorization": "env:NICO_TOOL_SECRET_HTTP_AUTH"}
    assert narrowed["model_endpoint_id"] == "endpoint-a"
    assert narrowed["model"] == "model-a"
    assert "very-secret-value" not in repr(narrowed)


def test_child_cannot_change_model_endpoint_or_model() -> None:
    with pytest.raises(ValueError, match="model endpoint"):
        narrow_child_permissions(
            parent={"model_endpoint_id": "endpoint-a", "model": "model-a"},
            child={"model_endpoint_id": "endpoint-b", "model": "model-a"},
            restrictions={},
        )


def test_delegation_intent_budget_and_fingerprint_are_bounded_and_stable() -> None:
    version_id = uuid4()
    intent = DelegationIntent(
        target_agent_version_id=version_id,
        objective="Compare two independent sources",
        acceptance={"required": ["summary"]},
        context_refs=("memory:2", "memory:1"),
        budget=BudgetGrant(token_limit=500, cost_limit_microunits=2500, tool_call_limit=3),
        idempotency_key="delegation-1",
    )
    reordered = intent.model_copy(update={"context_refs": ("memory:1", "memory:2")})

    assert delegation_fingerprint(intent) == delegation_fingerprint(reordered)
    with pytest.raises(ValidationError):
        BudgetGrant(token_limit=0)
