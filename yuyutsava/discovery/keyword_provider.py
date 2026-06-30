"""In-memory discovery provider: catalog + ``select:`` + keyword/glob ranking.

Used for tools, where the set is small, fixed per session, and best listed in
full. There is no embedding step — matching is deterministic token overlap with
an fnmatch convenience for the existing ``tr_*`` style patterns.

The pathological ``tool_search('*')`` dump is gone: a bare ``*`` is handled by
the search tool as a request for the catalog, and every other path here is
capped at ``max_results`` (``select:`` excepted — an explicit id list is honoured
in full).
"""

from __future__ import annotations

import fnmatch

from yuyutsava.discovery.entry import CatalogEntry
from yuyutsava.discovery.provider import SELECT_PREFIX, SearchResult


def _tokenize(text: str) -> list[str]:
    return [t for t in "".join(c if c.isalnum() else " " for c in text.lower()).split() if t]


class KeywordCatalogProvider:
    """Holds ``CatalogEntry`` objects; serves them by select / keyword / glob."""

    def __init__(self, entries: list[CatalogEntry]) -> None:
        self._entries = list(entries)
        self._by_id = {e.id: e for e in self._entries}

    # -- Tier-0 ------------------------------------------------------------
    def catalog_block(self) -> str | None:
        if not self._entries:
            return None
        groups: dict[str, list[CatalogEntry]] = {}
        for e in self._entries:
            groups.setdefault(e.group, []).append(e)
        lines: list[str] = []
        for group in sorted(groups):
            lines.append(f"{group}_*:")
            for e in sorted(groups[group], key=lambda x: x.id):
                lines.append(f"  - {e.id}: {e.blurb}")
        return "\n".join(lines)

    # -- Tier-1 ------------------------------------------------------------
    async def search(self, query: str, max_results: int) -> SearchResult:
        q = query.strip()
        if q.lower().startswith(SELECT_PREFIX):
            return self._select(q[len(SELECT_PREFIX):])
        if "*" in q or "?" in q:
            return self._glob(q, max_results)
        return self._keyword(q, max_results)

    def _select(self, raw: str) -> SearchResult:
        names = [n.strip() for n in raw.split(",") if n.strip()]
        found: list[CatalogEntry] = []
        unknown: list[str] = []
        for n in names:
            entry = self._by_id.get(n)
            if entry is not None:
                found.append(entry)
            else:
                unknown.append(n)
        note = f"unknown: {', '.join(unknown)}" if unknown else ""
        return SearchResult(entries=found, note=note)

    def _glob(self, pattern: str, max_results: int) -> SearchResult:
        matched = [e for e in self._entries if fnmatch.fnmatchcase(e.id, pattern)]
        matched.sort(key=lambda e: e.id)
        return self._cap(matched, max_results)

    def _keyword(self, query: str, max_results: int) -> SearchResult:
        terms = _tokenize(query)
        if not terms:
            return SearchResult()
        scored: list[tuple[int, CatalogEntry]] = []
        for e in self._entries:
            hay = e.match_text.lower()
            score = sum(1 for t in terms if t in hay)
            if score:
                scored.append((score, e))
        # Highest overlap first, then stable by id for determinism.
        scored.sort(key=lambda pair: (-pair[0], pair[1].id))
        return self._cap([e for _, e in scored], max_results)

    @staticmethod
    def _cap(matched: list[CatalogEntry], max_results: int) -> SearchResult:
        if len(matched) > max_results:
            extra = len(matched) - max_results
            return SearchResult(
                entries=matched[:max_results],
                note=f"{extra} more match — narrow with a keyword or select:<name>.",
            )
        return SearchResult(entries=matched)
