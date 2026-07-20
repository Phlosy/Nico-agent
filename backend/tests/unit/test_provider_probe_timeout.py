from __future__ import annotations

import asyncio

import pytest

from nico_agent.models import ModelDiscoveryResult
from nico_agent.models.errors import ModelProviderError
from nico_agent.provider_onboarding.worker import ProviderProbeWorker


class BlockingGateway:
    async def discover(self, _request):
        await asyncio.sleep(1)
        return ModelDiscoveryResult(models=())


@pytest.mark.asyncio
async def test_probe_execution_timeout_is_shorter_than_its_lease() -> None:
    worker = ProviderProbeWorker(
        None,  # type: ignore[arg-type]
        BlockingGateway(),  # type: ignore[arg-type]
        worker_id="timeout-test",
        lease_seconds=30,
        execution_timeout_seconds=0.01,
    )

    with pytest.raises(ModelProviderError) as captured:
        await worker._execute_bounded(
            {
                "kind": "discover_models",
                "model": None,
                "endpoint": {
                    "id": "probe-timeout",
                    "protocol": "openai_compatible",
                    "base_url": "https://example.invalid/v1",
                    "credential_ref": "env:NICO_MODEL_SECRET_TEST",
                    "provider_options": {},
                    "capabilities": {"streaming": True},
                    "allowed_models": [],
                    "tls_policy": {},
                },
            }
        )

    assert captured.value.code == "MODEL_PROVIDER_TIMEOUT"


def test_probe_execution_timeout_must_fit_inside_lease() -> None:
    with pytest.raises(ValueError, match="shorter than the lease"):
        ProviderProbeWorker(
            None,  # type: ignore[arg-type]
            BlockingGateway(),  # type: ignore[arg-type]
            worker_id="timeout-test",
            lease_seconds=30,
            execution_timeout_seconds=30,
        )


def test_default_probe_timeout_reserves_time_to_persist_the_result() -> None:
    worker = ProviderProbeWorker(
        None,  # type: ignore[arg-type]
        BlockingGateway(),  # type: ignore[arg-type]
        worker_id="timeout-test",
        lease_seconds=30,
    )

    assert worker.execution_timeout_seconds == 20
