from __future__ import annotations

import pytest

from nico_agent.config import Settings
from nico_agent.web_onboarding.catalog import get_web_provider_catalog
from nico_agent.web_onboarding.contracts import WebProviderCandidate, WebSearchPolicy
from nico_agent.web_onboarding.service import WebOnboardingService


def test_catalog_is_static_secret_free_and_uses_deployment_searxng_endpoint() -> None:
    settings = Settings(
        environment="test",
        web_searxng_endpoint="http://searxng:8080/search",
        web_searxng_allow_private=True,
        _env_file=None,
    )

    catalog = get_web_provider_catalog(settings)
    providers = {provider.key: provider for provider in catalog.providers}

    assert tuple(providers) == ("brave", "searxng")
    assert providers["brave"].requires_secret is True
    assert providers["brave"].secret_name == "web_search_brave_api_key"
    assert providers["searxng"].requires_secret is False
    assert providers["searxng"].endpoints[0].url == "http://searxng:8080/search"
    assert "brave-secret" not in catalog.model_dump_json().lower()


def test_candidate_requires_brave_secret_and_rejects_searxng_secret() -> None:
    settings = Settings(environment="test", _env_file=None)
    service = WebOnboardingService(None, settings)  # type: ignore[arg-type]
    revision = get_web_provider_catalog(settings).catalog_revision

    with pytest.raises(ValueError, match="tool Secret"):
        WebProviderCandidate(
            provider="brave",
            endpoint_key="managed",
            credential_ref="env:NICO_MODEL_SECRET_WRONG_SCOPE",
            catalog_revision=revision,
        )
    with pytest.raises(Exception, match="requires a credential"):
        service._validate_candidate(
            WebProviderCandidate(
                provider="brave",
                endpoint_key="managed",
                catalog_revision=revision,
            )
        )
    with pytest.raises(Exception, match="does not accept"):
        service._validate_candidate(
            WebProviderCandidate(
                provider="searxng",
                endpoint_key="configured",
                credential_ref="env:NICO_TOOL_SECRET_SEARXNG_NOT_ALLOWED",
                catalog_revision=revision,
            )
        )


def test_policy_canonicalizes_allowed_domains() -> None:
    policy = WebSearchPolicy(
        allowed_domains=("Docs.Example.com.", "docs.example.com")
    )

    assert policy.allowed_domains == ("docs.example.com",)
