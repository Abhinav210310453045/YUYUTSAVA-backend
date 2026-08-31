"""The contract every discovery source implements.

Two methods, mapping to the three-tier disclosure shape:

  Tier-0  ``catalog_block()``  — the cheap, always-visible ``name: blurb`` list,
          or ``None`` when the set is too large to enumerate (the caller then
          relies on Tier-1 search instead of listing).
  Tier-1  ``search()``         — a *bounded* lookup: exact ``select:`` fetch or
          ranked matches, never the whole inventory. Returns a ``SearchResult``
          so it can attach a "N more — narrow…" note when it had to truncate.

Tier-2 (the full schema/body) is reached through each returned
``CatalogEntry.load_detail`` — it is not a provider method because expansion is
always per-entry and lazy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from yuyutsava.discovery.entry import CatalogEntry

# Marker the search tool recognises as an exact-id fetch, e.g. "select:tr_write_file,tr_ls".
SELECT_PREFIX = "select:"


@dataclass(frozen=True)
class SearchResult:
    entries: list[CatalogEntry] = field(default_factory=list)
    note: str = ""  # optional trailing note (e.g. truncation hint or "unknown id")


@runtime_checkable
class DiscoveryProvider(Protocol):
    def catalog_block(self) -> str | None:
        """Tier-0: grouped ``name: blurb`` text, or None if the set is unlisted."""
        ...

    async def search(self, query: str, max_results: int) -> SearchResult:
        """Tier-1: bounded matches for ``query`` (handles ``select:`` itself)."""
        ...
