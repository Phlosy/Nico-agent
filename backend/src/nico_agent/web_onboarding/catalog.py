"""Product-owned Web Provider catalog."""

from __future__ import annotations

from nico_agent.config import Settings
from nico_agent.web_onboarding.contracts import (
    WebEndpointChoice,
    WebProviderCatalog,
    WebProviderPreset,
)

CATALOG_REVISION = "2026-07-22"


def get_web_provider_catalog(settings: Settings) -> WebProviderCatalog:
    defaults = {
        "safe_search": "moderate",
        "cache_ttl_seconds": settings.web_search_cache_ttl_seconds,
        "rate_limit_per_minute": 20,
        "allowed_domains": [],
    }
    return WebProviderCatalog(
        catalog_revision=CATALOG_REVISION,
        providers=(
            WebProviderPreset(
                key="brave",
                display_name="Brave Search API",
                requires_secret=True,
                secret_name="web_search_brave_api_key",
                credential_env_prefix="NICO_TOOL_SECRET_WEB_SEARCH_BRAVE_",
                endpoints=(
                    WebEndpointChoice(
                        key="managed",
                        label="Brave managed API",
                        url="https://api.search.brave.com/res/v1/web/search",
                        default=True,
                    ),
                ),
                default_config=defaults,
                documentation_url="https://api-dashboard.search.brave.com/app/documentation",
            ),
            WebProviderPreset(
                key="searxng",
                display_name="SearXNG",
                requires_secret=False,
                endpoints=(
                    WebEndpointChoice(
                        key="configured",
                        label="Deployment-configured SearXNG",
                        url=settings.web_searxng_endpoint,
                        default=True,
                    ),
                ),
                default_config=defaults,
                documentation_url="https://docs.searxng.org/dev/search_api.html",
            ),
        ),
    )


def provider_preset(settings: Settings, key: str) -> WebProviderPreset | None:
    return next(
        (item for item in get_web_provider_catalog(settings).providers if item.key == key),
        None,
    )
