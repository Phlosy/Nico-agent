from __future__ import annotations

import asyncio
import json

from nico_agent.evals.conversation_continuity import (
    ContinuityObservation,
    evaluate_observations,
    load_continuity_suite,
    run_external_suite,
    run_hermetic_suite,
)


def _passing_observations():
    suite = load_continuity_suite()
    observations = []
    for case in suite.cases:
        status = {
            "final": "completed",
            "ask_user": "suspended",
            "failure": "failed",
        }[case.expected.action]
        observations.append(
            ContinuityObservation(
                case_id=case.id,
                provider="synthetic",
                policy_revision=suite.policy_revision,
                status=status,
                action=case.expected.action,
                waiting=case.expected.waiting,
                actual_intent=case.expected.intent,
                model_calls=case.expected.max_model_calls,
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                usage_status="exact",
                latency_ms=10,
                tool_effect_count=0,
                assumption_stated=case.expected.assumption == "required",
                answer_requirements_met=True,
                source_separation_met=True,
                error_code=case.expected.error_code,
            )
        )
    return suite, observations


def test_fixture_contains_each_acceptance_example_once() -> None:
    suite = load_continuity_suite()

    assert len(suite.cases) == 11
    assert [case.acceptance_example for case in suite.cases] == [
        f"AE{index}" for index in range(1, 12)
    ]
    assert {case.category for case in suite.cases} >= {
        "reported_timestamp",
        "low_risk_omission",
        "pronoun_continuation",
        "minor_typo",
        "genuine_ambiguity",
        "ambiguous_high_risk_delete",
        "medium_confidence_low_risk",
        "correction_rejection",
        "infinite_loop_guard",
        "source_separation",
        "provider_portability",
    }


def test_hermetic_report_compares_baseline_and_redacts_case_content() -> None:
    report = asyncio.run(run_hermetic_suite(load_continuity_suite()))
    rendered = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)

    assert report.verification_status == "verified"
    assert report.metrics.pass_rate.rate == 1
    assert report.baseline_metrics is not None
    assert report.baseline_metrics.pass_rate.rate == 7 / 11
    assert report.baseline_metrics.unnecessary_clarification_rate.rate == 3 / 8
    assert report.metrics.unnecessary_clarification_rate.rate == 0
    assert report.metrics.paired_delta_denominator == 11
    assert "你平台是怎么提供de" not in rendered
    assert "把刚才那个删了" not in rendered


def test_missing_partial_timeout_and_failures_remain_in_denominators() -> None:
    suite, observations = _passing_observations()
    observations[0] = observations[0].model_copy(
        update={
            "input_tokens": 10,
            "output_tokens": None,
            "total_tokens": None,
            "usage_status": "partial",
        }
    )
    observations[1] = observations[1].model_copy(
        update={
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "usage_status": "missing",
        }
    )
    observations[2] = observations[2].model_copy(
        update={"status": "timed_out", "action": "failure", "error_code": "EVALUATION_TIMEOUT"}
    )
    observations[3] = observations[3].model_copy(update={"actual_intent": "wrong intent"})
    high_risk_index = next(
        index for index, case in enumerate(suite.cases) if case.expected.risk == "high"
    )
    observations[high_risk_index] = observations[high_risk_index].model_copy(
        update={"tool_effect_count": 1}
    )
    observations[-1] = observations[-1].model_copy(
        update={
            "status": "skipped",
            "action": "unknown",
            "skip_reason": "credential unavailable",
        }
    )

    report = evaluate_observations(
        suite,
        observations,
        provider="synthetic",
        credential_status="available",
    )

    assert report.metrics.pass_rate.denominator == 10
    assert report.metrics.timeout_count == 1
    assert report.metrics.skipped_count == 1
    assert report.metrics.token_partial_count == 1
    assert report.metrics.token_missing_count == 1
    assert report.metrics.wrong_intent_rate.model_dump() == {
        "numerator": 1,
        "denominator": 10,
        "rate": 0.1,
    }
    assert report.metrics.unsafe_high_risk_action_rate.rate == 1


def test_missing_external_credentials_are_explicitly_unverified(monkeypatch) -> None:
    for name in (
        "NICO_CONTINUITY_EVAL_BASE_URL",
        "NICO_CONTINUITY_EVAL_MODEL",
        "NICO_CONTINUITY_EVAL_CREDENTIAL_REF",
    ):
        monkeypatch.delenv(name, raising=False)
    report = asyncio.run(
        run_external_suite(
            load_continuity_suite(),
            base_url=None,
            model=None,
            credential_ref=None,
        )
    )

    assert report.credential_status == "unavailable"
    assert report.verification_status == "unverified"
    assert report.metrics.skipped_count == 11
    assert report.metrics.pass_rate.denominator == 0
    assert all(case.status == "skipped" for case in report.cases)
    assert all(case.skip_reason for case in report.cases)
