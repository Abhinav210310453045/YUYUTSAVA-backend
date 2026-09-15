"""Progressive-disclosure discovery: agents find tools and skills without a full catalog dump.

One contract — a catalog (Tier 0), bounded search (Tier 1) and lazy expansion
(Tier 2) — shared by tools
(:class:`~yuyutsava.discovery.keyword_provider.KeywordCatalogProvider`) and skills
(:class:`~yuyutsava.discovery.vector_provider.VectorStoreProvider`). An agent pays
tokens only for the entries it pulls. :class:`~yuyutsava.discovery.tool_registry.ToolRegistry`
holds an agent's tools on top of the keyword provider.

The names below are re-exported because other modules import them from here;
import anything else from the module that owns it.
"""

from __future__ import annotations

from yuyutsava.discovery.entry import CatalogEntry
from yuyutsava.discovery.keyword_provider import KeywordCatalogProvider
from yuyutsava.discovery.search_tool import make_discovery_search_tool
from yuyutsava.discovery.vector_provider import VectorStoreProvider

__all__ = [
    "CatalogEntry",
    "KeywordCatalogProvider",
    "VectorStoreProvider",
    "make_discovery_search_tool",
]
