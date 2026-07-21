from __future__ import annotations

import pytest

from nico_agent.models import ModelResponse, ModelUsage
from nico_agent.provider_onboarding.worker import ProviderProbeWorker


class CompletionGateway:
    def __init__(self, response: ModelResponse) -> None:
        self.response = response

    async def complete(self, _request):
        return self.response


def _snapshot() -> dict[str, object]:
    return {
        "kind": "verify_completion",
        "model": "deepseek-v4-pro",
        "endpoint": {
            "protocol": "openai_compatible",
            "base_url": "https://api.deepseek.com",
            "credential_ref": "env:NICO_MODEL_SECRET_TEST",
            "provider_options": {},
            "capabilities": {"streaming": True},
            "allowed_models": ["deepseek-v4-pro"],
        },
    }


@pytest.mark.asyncio
async def test_reasoning_tokens_truncated_before_final_text_prove_connectivity() -> None:
    response = ModelResponse(
        text="",
        finish_reason="length",
        usage=ModelUsage(
            input_tokens=12,
            output_tokens=16,
            total_tokens=28,
            status="exact",
        ),
        provider_request_id="deepseek-request",
    )
    worker = ProviderProbeWorker(None, CompletionGateway(response), worker_id="probe-test")

    result, verified = await worker._execute(_snapshot())

    assert verified is True
    assert result["response_present"] is True
    assert result["finish_reason"] == "length"
    assert result["usage"]["output_tokens"] == 16


@pytest.mark.asyncio
async def test_truly_empty_completion_does_not_prove_connectivity() -> None:
    response = ModelResponse(
        text="",
        finish_reason="stop",
        usage=ModelUsage(output_tokens=0, total_tokens=0, status="exact"),
    )
    worker = ProviderProbeWorker(None, CompletionGateway(response), worker_id="probe-test")

    with pytest.raises(ValueError, match="empty completion"):
        await worker._execute(_snapshot())
