from __future__ import annotations

from uuid import uuid4

from nico_agent.runtime.contracts import RuntimeSessionRequest
from nico_agent.runtime.native.context import (
    build_citation_repair_context,
    build_clarification_repair_context,
    build_native_context,
    build_phase_context,
)
from nico_agent.runtime.native.prompts import NATIVE_ACTION_POLICY, NATIVE_CONTINUITY_POLICY


def _request() -> RuntimeSessionRequest:
    return RuntimeSessionRequest(
        tenant_id=uuid4(),
        run_id=uuid4(),
        task_id=uuid4(),
        agent_id=uuid4(),
        agent_version_id=uuid4(),
        task_title="Continue the conversation",
        task_input={"message": "incomplete follow-up"},
        role="assistant",
        mandate="Help accurately",
        boundaries=["Do not widen authority"],
    )


def test_every_native_context_contains_the_same_continuity_policy_once() -> None:
    request = _request()
    contexts = {
        "direct": build_native_context(request, mode="direct"),
        "react": build_native_context(request, mode="react"),
        "planner": build_phase_context(
            request,
            phase="planning",
            instruction="Plan the task.",
            payload={},
            version=1,
        ),
        "plan_step": build_phase_context(
            request,
            phase="plan_step",
            instruction="Execute the step.",
            payload={},
            version=1,
        ),
        "reflection": build_phase_context(
            request,
            phase="reflection",
            instruction="Reflect on the failed step.",
            payload={},
            version=1,
        ),
        "correction": build_citation_repair_context(
            request,
            provisional_output={"content": "draft"},
            observed_urls=("https://docs.example/source",),
            version=1,
        ),
        "clarification_correction": build_clarification_repair_context(
            request,
            observation={
                "type": "clarification_policy_observation",
                "interpreted_intent": "Continue the conversation",
            },
            version=1,
        ),
    }

    for label, context in contexts.items():
        assert context.messages[0].role == "system", label
        rendered = "\n".join(message.content or "" for message in context.messages)
        assert rendered.count(NATIVE_CONTINUITY_POLICY) == 1, label
        assert rendered.count(NATIVE_ACTION_POLICY) == 1, label
        assert NATIVE_CONTINUITY_POLICY in (context.messages[0].content or ""), label
        assert NATIVE_ACTION_POLICY in (context.messages[0].content or ""), label


def test_policy_distinguishes_incomplete_form_from_ambiguous_or_risky_intent() -> None:
    policy = NATIVE_CONTINUITY_POLICY

    assert "typos, mixed-language wording, truncation" in policy
    assert "omitted subjects or objects" in policy
    assert "context-dependent references" in policy
    assert "Incomplete syntax alone does not make the intent ambiguous" in policy
    assert "one dominant, low-risk, and reversible interpretation" in policy
    assert "answer directly" in policy
    assert "briefly state the assumption" in policy
    assert "long list of low-probability alternatives" in policy
    assert "similarly plausible" in policy
    assert "indispensable information is missing" in policy
    assert "safe useful portion" in policy
    assert "one blocking question" in policy
    assert "destructive, write, payment, funds, permission, security" in policy
    assert "externally visible send" in policy
    assert "explicit confirmation" in policy


def test_policy_does_not_embed_case_answers_or_widen_runtime_authority() -> None:
    policy = NATIVE_CONTINUITY_POLICY

    assert "Runtime and Tool Gateway" in policy
    assert "does not grant or widen authorization" in policy
    assert "ask_user" not in policy
    assert all(
        fixture not in policy
        for fixture in (
            "你平台是怎么提供de",
            "第二种呢",
            "那个更适合 Mac",
            "kubernetes 怎么重启 depoly",
            "SQLite",
            "Docker",
            "containerd",
        )
    )


def test_action_policy_reserves_live_ask_user_for_active_gate_contract() -> None:
    assert "`final`" in NATIVE_ACTION_POLICY
    assert "Legacy plain-text final output remains accepted" in NATIVE_ACTION_POLICY
    assert "Emit `ask_user` only when it appears in the active response contract" in (
        NATIVE_ACTION_POLICY
    )
    assert "Dominant low-risk interpretations must be answered directly" in (NATIVE_ACTION_POLICY)
