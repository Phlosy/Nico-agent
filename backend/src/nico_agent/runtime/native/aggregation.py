"""Deterministic child-result aggregation policies with an optional bounded judge."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AggregationStrategy(StrEnum):
    FAIL_FAST = "fail_fast"
    BEST_EFFORT = "best_effort"
    MODEL_JUDGE = "model_judge"


class ChildResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    delegation_id: str
    child_run_id: str
    status: str
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    artifact_refs: tuple[str, ...] = ()


class AggregationOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    strategy: AggregationStrategy
    accepted: tuple[ChildResult, ...]
    rejected: tuple[ChildResult, ...]
    result: dict[str, Any]
    judge_tokens_used: int = Field(default=0, ge=0)


Judge = Callable[[tuple[ChildResult, ...], int], Awaitable[tuple[dict[str, Any], int]]]


async def aggregate_child_results(
    results: tuple[ChildResult, ...],
    *,
    strategy: AggregationStrategy,
    judge: Judge | None = None,
    judge_token_budget: int = 0,
) -> AggregationOutcome:
    accepted = tuple(item for item in results if item.status == "completed")
    rejected = tuple(item for item in results if item.status != "completed")
    if strategy is AggregationStrategy.FAIL_FAST and rejected:
        return AggregationOutcome(
            status="failed",
            strategy=strategy,
            accepted=accepted,
            rejected=rejected,
            result={"code": "CHILD_RESULT_REJECTED"},
        )
    if strategy is AggregationStrategy.MODEL_JUDGE:
        if judge is None or judge_token_budget < 1:
            return AggregationOutcome(
                status="failed",
                strategy=strategy,
                accepted=accepted,
                rejected=rejected,
                result={"code": "MODEL_JUDGE_UNAVAILABLE"},
            )
        judged, used = await judge(results, judge_token_budget)
        if used < 0 or used > judge_token_budget:
            raise ValueError("model judge exceeded its reserved token budget")
        return AggregationOutcome(
            status="completed",
            strategy=strategy,
            accepted=accepted,
            rejected=rejected,
            result=judged,
            judge_tokens_used=used,
        )
    return AggregationOutcome(
        status="completed",
        strategy=strategy,
        accepted=accepted,
        rejected=rejected,
        result={
            "results": [item.model_dump(mode="json") for item in accepted],
            "failures": [item.model_dump(mode="json") for item in rejected],
        },
    )
