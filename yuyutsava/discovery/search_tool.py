"""The one search tool both tools and skills are built from.

``make_discovery_search_tool(provider, name=…, noun=…)`` returns a LangChain
tool whose body is identical regardless of resource type:

  * ``select:a,b``   → exact fetch of those ids
  * ``*`` / empty    → the Tier-0 catalog (names only — never the full schemas)
  * anything else    → a bounded, ranked search

Each match is expanded through ``CatalogEntry.load_detail`` only here, so full
schemas/bodies enter the context one matched entry at a time, capped by
``max_results``.
"""

from __future__ import annotations

import logging

from langchain_core.tools import BaseTool, StructuredTool

from yuyutsava.discovery.provider import DiscoveryProvider

logger = logging.getLogger("yuyutsava.discovery.search_tool")

_DEFAULT_MAX = 5


def make_discovery_search_tool(
    provider: DiscoveryProvider,
    *,
    name: str,
    noun: str,
    examples: str = "",
) -> BaseTool:
    """Return a search tool over ``provider`` named ``name`` (noun e.g. 'tool')."""

    async def _search(query: str, max_results: int = _DEFAULT_MAX) -> str:
        q = (query or "").strip()
        if not q or q == "*":
            block = provider.catalog_block()
            if block:
                return block
            return f"no {noun} catalog available — search by keyword or select:<name>."

        result = await provider.search(q, max_results)
        if not result.entries:
            tail = f" ({result.note})" if result.note else ""
            return f"no {noun} matched {query!r}{tail}. Try a keyword or select:<name>."

        parts: list[str] = []
        for e in result.entries:
            try:
                parts.append(e.load_detail())
            except Exception:
                logger.warning("discovery: failed to expand %r", e.id, exc_info=True)
        body = "\n".join(p for p in parts if p)
        if result.note:
            body = f"{body}\n\n{result.note}" if body else result.note
        logger.debug("%s(%r) → %d matches", name, query, len(result.entries))
        return body

    description = (
        f"Search available {noun}s and load only the ones you need.\n\n"
        f"  {name}('select:NAME1,NAME2')  — load these exact {noun}s by name\n"
        f"  {name}('a few keywords')      — ranked matches (top {_DEFAULT_MAX})\n"
        f"  {name}('*')                   — list {noun} names only (cheap)\n"
        f"  max_results widens a keyword search.\n"
    )
    if examples:
        description += "\n" + examples

    return StructuredTool.from_function(
        coroutine=_search,
        name=name,
        description=description,
    )
