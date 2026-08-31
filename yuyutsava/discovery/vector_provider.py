"""Discovery provider backed by a semantic ``VectorStore`` (skills, future RAG).

Used where the set is large/growing and best *not* listed in full — so
``catalog_block`` returns ``None`` and discovery happens through ranked search.
``search`` delegates to the store (semantic, with the store's own keyword
fallback) and maps each ``Hit`` to a ``CatalogEntry`` whose ``load_detail``
fetches the full body lazily via ``loader``.

This is the same three-tier contract the in-memory ``KeywordCatalogProvider``
implements, so both feed the one ``make_discovery_search_tool`` factory.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from yuyutsava.discovery.entry import CatalogEntry
from yuyutsava.discovery.provider import SELECT_PREFIX, SearchResult

logger = logging.getLogger("yuyutsava.discovery.vector_provider")


class VectorStoreProvider:
    """Adapts a store with ``async search(query, k, **kwargs) -> list[Hit]``."""

    def __init__(
        self,
        store: object,
        loader: Callable[[str], str],
        *,
        group: str = "skill",
        search_kwargs: dict | None = None,
    ) -> None:
        self._store = store
        self._loader = loader
        self._group = group
        self._search_kwargs = search_kwargs or {}

    def catalog_block(self) -> str | None:
        # Set is large/growing — discover via search(), don't enumerate.
        return None

    async def search(self, query: str, max_results: int) -> SearchResult:
        q = query.strip()
        if q.lower().startswith(SELECT_PREFIX):
            return self._select(q[len(SELECT_PREFIX):])
        try:
            hits = await self._store.search(q, k=max_results, **self._search_kwargs)
        except Exception:
            logger.warning("discovery: vector search failed", exc_info=True)
            return SearchResult()
        return SearchResult(entries=[self._hit_to_entry(h) for h in hits])

    def _select(self, raw: str) -> SearchResult:
        names = [n.strip() for n in raw.split(",") if n.strip()]
        return SearchResult(
            entries=[
                CatalogEntry(
                    id=n,
                    group=self._group,
                    blurb="",
                    match_text=n,
                    load_detail=lambda n=n: self._loader(n),
                )
                for n in names
            ]
        )

    def _hit_to_entry(self, hit) -> CatalogEntry:
        # Ranked matches return metadata only (Tier-1) — the agent then loads
        # the full body via the domain's read tool (e.g. sk_read_skill) or an
        # explicit select:. This keeps a fuzzy search from dumping whole bodies.
        name = hit.id
        blurb = hit.text
        return CatalogEntry(
            id=name,
            group=str(hit.payload.get("scope", self._group)),
            blurb=blurb,
            match_text=f"{name} {blurb}",
            load_detail=lambda n=name, b=blurb: f"- {n}: {b}",
        )
