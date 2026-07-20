from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from nico_agent.agent_versions import AgentVersionLifecycle
from nico_agent.api_schemas import AgentVersionCreate


def test_agent_version_hash_resolves_legacy_runtime_provider_deterministically() -> None:
    legacy = AgentVersionCreate(
        role="assistant",
        mandate="Keep the existing behavior",
        run_config={"runtime_provider": "legacy-runtime", "temperature": 0.2},
    )
    explicit = legacy.model_copy(update={"runtime_provider": "legacy-runtime"})

    legacy_hash = AgentVersionLifecycle.content_hash(
        legacy,
        runtime_provider=AgentVersionLifecycle.runtime_provider(legacy),
    )
    explicit_hash = AgentVersionLifecycle.content_hash(
        explicit,
        runtime_provider=AgentVersionLifecycle.runtime_provider(explicit),
    )

    assert AgentVersionLifecycle.runtime_provider(legacy) == "legacy-runtime"
    assert legacy_hash == explicit_hash
    assert len(legacy_hash) == len(explicit_hash) == 64


def test_provider_clone_changes_only_native_route_fields() -> None:
    endpoint_id = uuid4()
    source = SimpleNamespace(
        role="researcher",
        mandate="Preserve this mandate",
        boundaries=["no writes"],
        long_term_goal="durable goal",
        current_goal="current goal",
        execution_mode="plan_and_execute",
        model_config_json={"temperature": 0.1},
        tool_policy={"allow": ["read"]},
        memory_policy={"scope": "project"},
        skill_policy={"enabled": True},
        plugin_refs=[{"name": "example"}],
        coordination_policy={"max_children": 2},
        budgets={"tokens": 1000},
        run_config={"checkpoint": True},
    )

    command = AgentVersionLifecycle.command_from_version(
        source,
        model_endpoint_id=endpoint_id,
        model_name="model-next",
    )

    assert command.role == source.role
    assert command.mandate == source.mandate
    assert command.boundaries == source.boundaries
    assert command.tool_policy == source.tool_policy
    assert command.memory_policy == source.memory_policy
    assert command.plugin_refs == source.plugin_refs
    assert command.runtime_provider == "nico_native"
    assert command.model_endpoint_id == endpoint_id
    assert command.model_name == "model-next"
