from __future__ import annotations

import pytest

from nico_agent.runtime.native.aggregation import (
    AggregationStrategy,
    ChildResult,
    aggregate_child_results,
)


def _results() -> tuple[ChildResult, ...]:
    return (
        ChildResult(
            delegation_id="d1",
            child_run_id="r1",
            status="completed",
            result={"summary": "accepted"},
            artifact_refs=("artifact:a1",),
        ),
        ChildResult(
            delegation_id="d2",
            child_run_id="r2",
            status="failed",
            error={"code": "CHILD_FAILED"},
        ),
    )


@pytest.mark.asyncio
async def test_fail_fast_and_best_effort_are_deterministic() -> None:
    failed = await aggregate_child_results(_results(), strategy=AggregationStrategy.FAIL_FAST)
    partial = await aggregate_child_results(_results(), strategy=AggregationStrategy.BEST_EFFORT)

    assert failed.status == "failed"
    assert partial.status == "completed"
    assert partial.result["results"][0]["artifact_refs"] == ["artifact:a1"]
    assert partial.result["failures"][0]["error"]["code"] == "CHILD_FAILED"


@pytest.mark.asyncio
async def test_model_judge_cannot_exceed_reserved_budget() -> None:
    async def judge(results, budget):
        assert len(results) == 2 and budget == 20
        return {"winner": "r1"}, 10

    outcome = await aggregate_child_results(
        _results(),
        strategy=AggregationStrategy.MODEL_JUDGE,
        judge=judge,
        judge_token_budget=20,
    )
    assert outcome.result == {"winner": "r1"}
    assert outcome.judge_tokens_used == 10

    async def overspend(_results, budget):
        return {}, budget + 1

    with pytest.raises(ValueError, match="exceeded"):
        await aggregate_child_results(
            _results(),
            strategy=AggregationStrategy.MODEL_JUDGE,
            judge=overspend,
            judge_token_budget=20,
        )
