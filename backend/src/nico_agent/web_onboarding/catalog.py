"""Product-owned Web Provider catalog."""

from __future__ import annotations

from nico_agent.config import Settings
from nico_agent.web_onboarding.contracts import (
    WebDnsResolverChoice,
    WebEndpointChoice,
    WebProviderCatalog,
    WebProviderPreset,
)

CATALOG_REVISION = "2026-07-22.2"


def get_web_provider_catalog(settings: Settings) -> WebProviderCatalog:
    defaults = {
        "dns_resolver": "system",
        "safe_search": "moderate",
        "cache_ttl_seconds": settings.web_search_cache_ttl_seconds,
        "rate_limit_per_minute": 20,
        "allowed_domains": [],
    }
    return WebProviderCatalog(
        catalog_revision=CATALOG_REVISION,
        dns_resolvers=(
            WebDnsResolverChoice(
                key="system",
                label="System DNS",
                description=(
                    "Use the operating-system resolver; choose this when DNS returns real IPs."
                ),
            ),
            WebDnsResolverChoice(
                key="cloudflare",
                label="Cloudflare DNS-over-HTTPS",
                description=(
                    "Resolve real public IPs through encrypted DNS; compatible with Fake-IP."
                ),
                recommended=True,
            ),
            WebDnsResolverChoice(
                key="google",
                label="Google Public DNS-over-HTTPS",
                description="Resolve real public IPs through Google Public DNS.",
            ),
        ),
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
