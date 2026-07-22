"""Trusted Web search Provider adapters."""

from nico_agent.web.providers.brave import BraveSearchProvider
from nico_agent.web.providers.searxng import SearxngSearchProvider

__all__ = ["BraveSearchProvider", "SearxngSearchProvider"]
