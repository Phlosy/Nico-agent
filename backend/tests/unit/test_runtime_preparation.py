from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from nico_agent.runtime.contracts import RuntimeSessionRequest
from nico_agent.runtime.native.context import build_native_context, build_phase_context
from nico_agent.runtime.preparation import (
    RuntimePreparationService,
    build_knowledge_policy_snapshot,
    narrow_knowledge_policy,
)


def test_knowledge_policy_is_tenant_agent_intersection_and_bounded() -> None:
    shared_skill = uuid4()
    snapshot = build_knowledge_policy_snapshot(
        {
            "memory_policy": {
                "enabled": True,
                "scopes": ["tenant", "project"],
                "memory_types": ["semantic", "procedural"],
                "top_k": 8,
                "max_chars": 20_000,
                "max_tokens": 5_000,
                "minimum_similarity": 0.2,
            },
            "skill_policy": {
                "enabled": True,
                "scopes": ["tenant", "project"],
                "allowed_skill_ids": [str(shared_skill), str(uuid4())],
                "top_k": 6,
                "max_chars": 30_000,
                "max_tokens": 6_000,
            },
        },
        {
            "enabled": True,
            "scopes": ["project", "agent"],
            "memory_types": ["semantic", "episodic"],
            "top_k": 3,
            "max_chars": 9_000,
            "max_tokens": 2_500,
            "minimum_similarity": 0.5,
        },
        {
            "enabled": True,
            "scopes": ["project", "agent"],
            "allowed_skill_ids": [str(shared_skill)],
            "top_k": 2,
            "max_chars": 12_000,
            "max_tokens": 2_200,
        },
    )

    assert snapshot["memory"] == {
        "enabled": True,
        "scopes": ["project"],
        "memory_types": ["semantic"],
        "top_k": 3,
        "max_chars": 9_000,
        "max_tokens": 2_500,
        "minimum_similarity": 0.5,
    }
    assert snapshot["skill"]["allowed_skill_ids"] == [str(shared_skill)]
    assert snapshot["skill"]["top_k"] == 2
    assert snapshot["skill"]["max_tokens"] == 2_200
    assert len(snapshot["content_hash"]) == 64


def test_child_policy_can_only_reduce_parent_scope_ids_and_caps() -> None:
    allowed = uuid4()
    parent = build_knowledge_policy_snapshot(
        {
            "memory_policy": {
                "enabled": True,
                "scopes": ["tenant", "project"],
                "top_k": 10,
                "max_chars": 20_000,
            },
            "skill_policy": {
                "enabled": True,
                "scopes": ["tenant", "project"],
                "allowed_skill_ids": [str(allowed)],
                "top_k": 5,
                "max_chars": 20_000,
            },
        },
        {
            "enabled": True,
            "scopes": ["tenant", "project"],
            "top_k": 8,
            "max_chars": 12_000,
        },
        {
            "enabled": True,
            "scopes": ["tenant", "project"],
            "allowed_skill_ids": [str(allowed)],
            "top_k": 4,
            "max_chars": 12_000,
        },
    )

    narrowed = narrow_knowledge_policy(
        parent,
        {
            "enabled": True,
            "scopes": ["project", "agent"],
            "top_k": 6,
            "max_chars": 10_000,
        },
        {
            "enabled": True,
            "scopes": ["project"],
            "allowed_skill_ids": [str(allowed), str(uuid4())],
            "top_k": 3,
            "max_chars": 10_000,
        },
        {
            "memory": {"scopes": ["project"], "top_k": 2},
            "skill": {"scopes": ["project"], "allowed_skill_ids": [str(allowed)]},
        },
    )

    assert narrowed["memory"]["enabled"] is True
    assert narrowed["memory"]["scopes"] == ["project"]
    assert narrowed["memory"]["top_k"] == 2
    assert narrowed["skill"]["allowed_skill_ids"] == [str(allowed)]
    assert (
        narrow_knowledge_policy(
            parent,
            {"enabled": True, "scopes": ["agent"]},
            {"enabled": True, "scopes": ["agent"], "allowed_skill_ids": [str(allowed)]},
            {},
        )["memory"]["enabled"]
        is False
    )


def test_frozen_published_knowledge_is_untrusted_and_rebuildable() -> None:
    task_id = uuid4()
    version_id = uuid4()
    memory_id = uuid4()
    memory_key = uuid4()
    skill_id = uuid4()
    skill_version_id = uuid4()
    memory_hash = "a" * 64
    skill_hash = "b" * 64
    selection = {
        "schema_version": 1,
        "policy_hash": "c" * 64,
        "query_hash": "d" * 64,
        "memory": [
            {
                "memory_id": str(memory_id),
                "memory_key": str(memory_key),
                "version": 2,
                "memory_type": "semantic",
                "scope_type": "project",
                "content_hash": memory_hash,
                "content": "Use the approved volatility convention.",
                "content_truncated": False,
                "confidence": 0.9,
                "similarity": 0.8,
                "source_hashes": ["e" * 64],
            }
        ],
        "skills": [
            {
                "skill_id": str(skill_id),
                "skill_version_id": str(skill_version_id),
                "name": "approved-analysis",
                "description": "Approved analysis steps",
                "version": 3,
                "scope_type": "project",
                "content_hash": skill_hash,
                "content": '{"steps":[{"instruction":"compare"}]}',
                "content_format": "canonical_json",
                "selection": "stable",
                "deployment_id": None,
                "rollout_percentage": None,
                "bucket": None,
            }
        ],
        "memory_chars": 39,
        "skill_chars": 37,
        "legacy_recovery": False,
    }
    task = SimpleNamespace(
        id=task_id,
        title="Analyze volatility",
        input={"symbol": "TEST"},
        acceptance={"required": ["summary"]},
    )
    version = SimpleNamespace(
        id=version_id,
        role="analyst",
        mandate="Use approved knowledge",
        boundaries=["Never widen permissions"],
        long_term_goal=None,
        current_goal=None,
    )
    seed = RuntimePreparationService._context_seed(task, version, selection)
    request = RuntimeSessionRequest(
        tenant_id=uuid4(),
        run_id=uuid4(),
        task_id=task_id,
        agent_id=uuid4(),
        agent_version_id=version_id,
        task_title=task.title,
        task_input=task.input,
        acceptance=task.acceptance,
        role=version.role,
        mandate=version.mandate,
        boundaries=version.boundaries,
        context_seed=seed,
    )

    context = build_native_context(request, mode="direct")
    rendered = "\n".join(message.content or "" for message in context.messages)
    assert "published_memory" in rendered and "published_skill" in rendered
    assert "untrusted_data" in rendered
    assert context.memory_refs[0]["memory_id"] == str(memory_id)
    assert context.skill_refs[0]["skill_version_id"] == str(skill_version_id)
    assert f"memory:{memory_id}:v2:{memory_hash}" in context.source_refs
    assert context.content_hash == build_native_context(request, mode="direct").content_hash


def test_native_context_uses_frozen_run_time_as_a_fast_path_without_blocking_verification() -> None:
    request = RuntimeSessionRequest(
        tenant_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        task_title="Tell the time",
        task_input={"message": "当前北京时间"},
        role="assistant",
        mandate="Help accurately",
        execution_manifest={"run_started_at": "2026-07-23T07:07:12.365729+00:00"},
    )

    context = build_native_context(request, mode="react")
    phase_context = build_phase_context(
        request,
        phase="planning",
        instruction="Plan the task.",
        payload={},
        version=1,
    )
    system = context.messages[0].content or ""
    phase_system = phase_context.messages[0].content or ""
    instruction = context.messages[1].content or ""

    assert "2026-07-23T07:07:12.365729+00:00" in system
    assert "2026-07-23T07:07:12.365729+00:00" in phase_system
    assert "preferred fast path for ordinary current date and time questions" in system
    assert "preferred fast path for ordinary current date and time questions" in phase_system
    assert "explicitly asks for independent verification" in system
    assert "authorized tools may still be used" in system
    assert "Do not use Web search" not in system
    assert "authoritative for current date and time" not in system
    assert "Use the fewest tool calls needed" in instruction
    assert "one successful relevant tool result is normally sufficient" in instruction
    assert "Search-to-Fetch" in instruction
    assert context.content_hash == build_native_context(request, mode="react").content_hash
