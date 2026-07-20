from __future__ import annotations

import pytest
from pydantic import ValidationError

from nico_agent.domain.models import ModelEndpoint, ProviderProbe
from nico_agent.domain.states import ProviderProbeKind, ProviderProbeStatus
from nico_agent.provider_onboarding.catalog import get_provider_catalog
from nico_agent.provider_onboarding.contracts import (
    CandidateConfiguration,
    ProviderCatalog,
    ProviderPreset,
    canonical_candidate_hash,
)

EXPECTED_PROVIDER_KEYS = {
    "openai",
    "anthropic",
    "google-gemini",
    "openrouter",
    "xai",
    "deepseek",
    "alibaba-bailian",
    "moonshot-kimi",
    "zhipu-glm",
    "minimax",
}


def test_catalog_contains_the_versioned_safe_provider_baseline() -> None:
    catalog = get_provider_catalog()

    assert catalog.schema_version == 1
    assert catalog.catalog_revision == "2026-07-20"
    assert {provider.key for provider in catalog.providers} == EXPECTED_PROVIDER_KEYS
    assert {provider.protocol for provider in catalog.providers} == {
        "openai_compatible",
        "anthropic_messages",
        "google_gemini",
    }
    for provider in catalog.providers:
        assert provider.documentation_url.startswith("https://")
        assert provider.recommended_models
        assert provider.authentication == "api_key"
        assert all(location.base_url.startswith("https://") for location in provider.locations)
        assert "secret" not in provider.model_dump_json().lower()


def test_catalog_rejects_duplicate_keys_and_unsafe_locations() -> None:
    provider = get_provider_catalog().providers[0]
    with pytest.raises(ValueError, match="duplicate provider key"):
        ProviderCatalog(
            schema_version=1,
            catalog_revision="test",
            providers=[provider, provider],
        )

    payload = provider.model_dump()
    payload["locations"] = [{"key": "default", "label": "Default", "base_url": "http://api"}]
    with pytest.raises(ValidationError):
        ProviderPreset.model_validate(payload)


def test_candidate_hash_is_canonical_and_semantic() -> None:
    first = CandidateConfiguration(
        provider_key="openai",
        protocol="openai_compatible",
        base_url="https://api.openai.com/v1/",
        credential_ref="env:NICO_MODEL_SECRET_OPENAI",
        model="gpt-5.6-terra",
        provider_options={"region": "global", "workspace_id": None},
        catalog_revision="2026-07-20",
    )
    reordered = CandidateConfiguration(
        provider_key="openai",
        protocol="openai_compatible",
        base_url="https://api.openai.com/v1",
        credential_ref="env:NICO_MODEL_SECRET_OPENAI",
        model="gpt-5.6-terra",
        provider_options={"workspace_id": None, "region": "global"},
        catalog_revision="2026-07-20",
    )

    assert canonical_candidate_hash(first) == canonical_candidate_hash(reordered)
    assert len(canonical_candidate_hash(first)) == 64
    assert canonical_candidate_hash(first) != canonical_candidate_hash(
        reordered.model_copy(update={"model": "gpt-5.6-sol"})
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_key", "../openai"),
        ("base_url", "http://api.openai.com/v1"),
        ("credential_ref", "plain-text-key"),
        ("model", "bad\nmodel"),
    ],
)
def test_candidate_rejects_unsafe_metadata(field: str, value: str) -> None:
    payload = {
        "provider_key": "openai",
        "protocol": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        "credential_ref": "env:NICO_MODEL_SECRET_OPENAI",
        "model": "gpt-5.6-terra",
        "provider_options": {},
        "catalog_revision": "2026-07-20",
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        CandidateConfiguration.model_validate(payload)


def test_provider_persistence_metadata_carries_provenance_and_probe_states() -> None:
    endpoint_columns = set(ModelEndpoint.__table__.columns.keys())
    probe_columns = set(ProviderProbe.__table__.columns.keys())

    assert {
        "provider_key",
        "catalog_revision",
        "provider_options",
        "verified_probe_id",
        "verified_at",
    } <= endpoint_columns
    assert {
        "kind",
        "status",
        "candidate_hash",
        "credential_ref",
        "lease_expires_at",
        "result",
        "error_code",
        "activated_at",
    } <= probe_columns
    assert set(ProviderProbeKind) == {"discover_models", "verify_completion"}
    assert set(ProviderProbeStatus) == {
        "pending",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "activated",
    }

    protocol_constraint = next(
        constraint
        for constraint in ModelEndpoint.__table__.constraints
        if constraint.name == "ck_model_endpoints_protocol"
    )
    assert "anthropic_messages" in str(protocol_constraint.sqltext)
    assert "google_gemini" in str(protocol_constraint.sqltext)
