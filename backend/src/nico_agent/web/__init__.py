"""Provider-neutral Web capability primitives."""

from nico_agent.web.contracts import (
    SearchPage,
    SearchProvider,
    SearchProviderError,
    SearchRequest,
    SearchResult,
)
from nico_agent.web.normalization import ExternalText, bounded_external_text, clean_external_text
from nico_agent.web.registry import WebProviderRegistry

__all__ = [
    "ExternalText",
    "SearchPage",
    "SearchProvider",
    "SearchProviderError",
    "SearchRequest",
    "SearchResult",
    "WebProviderRegistry",
    "bounded_external_text",
    "clean_external_text",
]
