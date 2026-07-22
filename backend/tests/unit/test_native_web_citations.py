from __future__ import annotations

import hashlib
import json

from nico_agent.runtime.contracts import RuntimeToolOutcome
from nico_agent.runtime.native.checkpoint import (
    load_plan_checkpoint,
    load_react_checkpoint,
    make_plan_checkpoint,
    make_react_checkpoint,
)
from nico_agent.runtime.native.context import (
    merge_observed_web_urls,
    output_has_observed_web_citation,
)


def test_web_observations_collect_only_successful_marked_canonical_urls() -> None:
    search = RuntimeToolOutcome(
        call_id="search",
        status="succeeded",
        output={
            "results": [
                {"url": "HTTPS://Example.com:443/docs"},
                {"url": "not-a-url"},
            ],
            "external_content": {"untrusted": True, "source": "web_search"},
        },
    )
    fetch = RuntimeToolOutcome(
        call_id="fetch",
        status="succeeded",
        output={
            "url": "https://example.com/docs",
            "final_url": "https://example.com/article",
            "external_content": {"untrusted": True, "source": "web_fetch"},
        },
    )
    unmarked = RuntimeToolOutcome(
        call_id="other",
        status="succeeded",
        output={"results": [{"url": "https://attacker.example"}]},
    )

    observed = merge_observed_web_urls((), search)
    observed = merge_observed_web_urls(observed, fetch)
    observed = merge_observed_web_urls(observed, unmarked)

    assert observed == (
        "https://example.com/docs",
        "https://example.com/article",
    )


def test_citation_requires_an_exact_observed_url_not_a_prefix() -> None:
    observed = ("https://example.com/source",)

    assert output_has_observed_web_citation(
        {"content": "See https://example.com/source."}, observed
    )
    assert not output_has_observed_web_citation(
        {"content": "See https://example.com/source-pretend."}, observed
    )
    assert not output_has_observed_web_citation(
        {"content": "No link was included."}, observed
    )


def test_old_native_checkpoint_hashes_remain_loadable() -> None:
    manifest = {
        "schema_version": 1,
        "runtime_provider": "nico_native",
        "execution_mode": "react",
    }
    react = make_react_checkpoint(
        manifest=manifest,
        loop_state="reasoning",
        iteration=1,
        context_version=0,
    ).model_dump(mode="json")
    plan = make_plan_checkpoint(
        manifest=manifest,
        loop_state="planning",
    ).model_dump(mode="json")
    for payload in (react, plan):
        payload.pop("observed_web_urls")
        payload.pop("citation_repair_attempted")
        payload.pop("citation_provisional_output")
        payload["checkpoint_hash"] = _hash_without_checkpoint(payload)

    loaded_react = load_react_checkpoint(react, manifest=manifest)
    loaded_plan = load_plan_checkpoint(plan, manifest=manifest)

    assert loaded_react.observed_web_urls == ()
    assert loaded_plan.observed_web_urls == ()
    assert loaded_react.citation_repair_attempted is False
    assert loaded_plan.citation_repair_attempted is False


def _hash_without_checkpoint(payload: dict) -> str:
    value = {key: item for key, item in payload.items() if key != "checkpoint_hash"}
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
