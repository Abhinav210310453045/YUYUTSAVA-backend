"""Reusable progressive-disclosure discovery layer.

A single contract — Tier-0 catalog → Tier-1 bounded search → Tier-2 lazy expand —
shared by tools (``KeywordCatalogProvider``) and skills (``VectorStoreProvider``),
and ready for any future resource type. The agent only ever pays tokens for the
entries it actually pulls, and never for a wildcard dump.
"""

from __future__ import annotations

from yuyutsava.discovery.entry import CatalogEntry
from yuyutsava.discovery.keyword_provider import KeywordCatalogProvider
from yuyutsava.discovery.provider import (
    SELECT_PREFIX,
    DiscoveryProvider,
    SearchResult,
)
from yuyutsava.discovery.search_tool import make_discovery_search_tool
from yuyutsava.discovery.vector_provider import VectorStoreProvider

__all__ = [
    "CatalogEntry",
    "DiscoveryProvider",
    "SearchResult",
    "SELECT_PREFIX",
    "KeywordCatalogProvider",
    "VectorStoreProvider",
    "make_discovery_search_tool",
]
