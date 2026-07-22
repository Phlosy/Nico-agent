from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from nico_agent.config import Settings
from nico_agent.domain.models import ProviderProbe
from nico_agent.guided_setup.service import (
    GuidedSetupService,
    _ledger_matches,
    _version_web_authorized,
    merge_guided_setup_ledger,
    parse_guided_setup_ledger,
)


def _service(*, model_writes: bool = True, web_writes: bool = True) -> GuidedSetupService:
    return GuidedSetupService(
        None,  # type: ignore[arg-type]
        Settings(
            model_endpoint_writes_enabled=model_writes,
            web_provider_writes_enabled=web_writes,
            _env_file=None,
        ),
    )


def test_malformed_ledger_is_treated_as_fresh() -> None:
    assert parse_guided_setup_ledger({"guided_setup": "bad"}) == {"schema_version": 1}
    assert parse_guided_setup_ledger({"guided_setup": {"schema_version": 999}}) == {
        "schema_version": 1
    }


def test_ledger_merge_preserves_unrelated_settings_and_normalizes_ids() -> None:
    agent_id = uuid4()
    settings = {"tool_policy": {"allow": ["file.read@1.0.0"]}, "custom": {"keep": True}}

    merged = merge_guided_setup_ledger(
        settings,
        web_intent="skipped",
        selected_agent_id=agent_id,
        selected_profile="developer",
    )

    assert merged["tool_policy"] == settings["tool_policy"]
    assert merged["custom"] == {"keep": True}
    assert merged["guided_setup"] == {
        "schema_version": 1,
        "web_intent": "skipped",
        "selected_agent_id": str(agent_id),
        "selected_profile": "developer",
    }
    assert _ledger_matches(
        merged["guided_setup"],
        {"web_intent": "skipped", "selected_agent_id": agent_id},
    )
    assert not _ledger_matches(
        merged["guided_setup"],
        {"web_intent": "enabled"},
    )


def test_web_skip_is_explicit_but_verified_configuration_wins() -> None:
    service = _service()
    skipped = service._web_area(
        {"web_intent": "skipped"},
        {},
        None,
    )
    probe = ProviderProbe(
        tenant_id=uuid4(),
        kind="verify_web",
        status="succeeded",
        provider_key="searxng",
        protocol="web_search",
        base_url="http://fake-web/search",
        credential_ref=None,
        model_name=None,
        catalog_revision="test",
        candidate_hash="a" * 64,
        idempotency_key="test",
    )
    ready = service._web_area(
        {"web_intent": "skipped"},
        {
            "enabled": True,
            "provider": "searxng",
            "candidate_hash": "a" * 64,
        },
        probe,
    )

    assert skipped.state == "skipped"
    assert ready.state == "ready"


def test_deployment_policy_blocks_missing_provider_configuration() -> None:
    service = _service(model_writes=False, web_writes=False)

    assert service._model_area(False, None).state == "blocked"
    assert service._web_area({}, {}, None).state == "blocked"


def test_proof_trace_requires_bounded_search_fetch_and_observed_citation() -> None:
    search_id = uuid4()
    now = datetime.now(UTC)
    run = SimpleNamespace(result={"content": "Evidence: https://example.test/article"})
    search = SimpleNamespace(
        id=search_id,
        tool_name="web.search",
        status="succeeded",
        created_at=now,
        arguments={"query": "Nico"},
        result={"results": [{"url": "https://example.test/article"}]},
    )
    fetch = SimpleNamespace(
        id=uuid4(),
        tool_name="web.fetch",
        status="succeeded",
        created_at=now + timedelta(seconds=1),
        arguments={
            "url": "https://example.test/article",
            "search_tool_call_id": str(search_id),
        },
        result={"final_url": "https://example.test/article"},
    )
    runtime = SimpleNamespace(
        checkpoint={
            "execution_mode": "setup_proof",
            "loop_state": "completed",
            "usage": {"model_calls": 0, "tool_calls": 2},
        },
        tool_policy_snapshot={
            "allow": ["web.search@1.0.0", "web.fetch@1.1.0"],
            "tools": {
                "web.search@1.0.0": {"candidate_hash": "a" * 64},
                "web.fetch@1.1.0": {"candidate_hash": "a" * 64},
            },
        },
    )
    assert (
        GuidedSetupService._proof_trace_failure(
            run,
            (search, fetch),
            runtime,
            candidate_hash="a" * 64,
        )
        is None
    )
    run.result = {"content": "Evidence without URL"}
    assert (
        GuidedSetupService._proof_trace_failure(
            run,
            (search, fetch),
            runtime,
            candidate_hash="a" * 64,
        )
        == "citation_missing"
    )


def test_proof_rejects_unrelated_tool_and_candidate_drift() -> None:
    now = datetime.now(UTC)
    run = SimpleNamespace(result={})
    unrelated = SimpleNamespace(
        id=uuid4(),
        tool_name="file.read",
        status="succeeded",
        created_at=now,
        arguments={},
        result={},
    )
    runtime = SimpleNamespace(
        checkpoint={
            "execution_mode": "setup_proof",
            "loop_state": "completed",
            "usage": {"model_calls": 0, "tool_calls": 2},
        },
        tool_policy_snapshot={
            "allow": ["web.search@1.0.0", "web.fetch@1.1.0"],
            "tools": {
                "web.search@1.0.0": {"candidate_hash": "b" * 64},
                "web.fetch@1.1.0": {"candidate_hash": "b" * 64},
            },
        },
    )
    assert (
        GuidedSetupService._proof_trace_failure(
            run,
            (unrelated,),
            runtime,
            candidate_hash="a" * 64,
        )
        == "web_candidate_mismatch"
    )
    runtime.tool_policy_snapshot["tools"]["web.search@1.0.0"]["candidate_hash"] = "a" * 64
    runtime.tool_policy_snapshot["tools"]["web.fetch@1.1.0"]["candidate_hash"] = "a" * 64
    assert (
        GuidedSetupService._proof_trace_failure(
            run,
            (unrelated,),
            runtime,
            candidate_hash="a" * 64,
        )
        == "unexpected_tool_call"
    )


def test_proof_rejects_a_model_driven_trace_without_platform_orchestration() -> None:
    runtime = SimpleNamespace(
        checkpoint={
            "execution_mode": "react",
            "loop_state": "completed",
            "usage": {"total_tokens": 100},
        },
        tool_policy_snapshot={},
    )

    assert (
        GuidedSetupService._proof_trace_failure(
            SimpleNamespace(result={}),
            (),
            runtime,
            candidate_hash="a" * 64,
        )
        == "platform_orchestration_missing"
    )


def test_proof_rejects_duplicate_platform_tool_calls() -> None:
    now = datetime.now(UTC)
    search_id = uuid4()
    search = SimpleNamespace(
        id=search_id,
        tool_name="web.search",
        status="succeeded",
        created_at=now,
        arguments={"query": "Nico"},
        result={"results": [{"url": "https://example.test/article"}]},
    )
    runtime = SimpleNamespace(
        checkpoint={
            "execution_mode": "setup_proof",
            "loop_state": "completed",
            "usage": {"model_calls": 0, "tool_calls": 2},
        },
        tool_policy_snapshot={
            "allow": ["web.search@1.0.0", "web.fetch@1.1.0"],
            "tools": {
                "web.search@1.0.0": {"candidate_hash": "a" * 64},
                "web.fetch@1.1.0": {"candidate_hash": "a" * 64},
            },
        },
    )

    assert (
        GuidedSetupService._proof_trace_failure(
            SimpleNamespace(result={}),
            (search, search),
            runtime,
            candidate_hash="a" * 64,
        )
        == "unexpected_tool_call"
    )


def test_web_authorization_is_bound_to_both_tools_and_current_candidate() -> None:
    version = SimpleNamespace(
        tool_policy={
            "allow": ["web.search@1.0.0", "web.fetch@1.1.0"],
            "tools": {
                "web.search@1.0.0": {"candidate_hash": "a" * 64},
                "web.fetch@1.1.0": {"candidate_hash": "a" * 64},
            },
        }
    )

    assert _version_web_authorized(version, "a" * 64)
    assert not _version_web_authorized(version, "b" * 64)
    version.tool_policy["allow"].remove("web.fetch@1.1.0")
    assert not _version_web_authorized(version, "a" * 64)
