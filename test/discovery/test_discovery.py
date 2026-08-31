"""Unit tests for the shared progressive-discovery layer.

Run:  uv run python -m unittest test.discovery.test_discovery -v

Covers the contract both tools and skills sit on: the Tier-0 catalog, ``select:``
exact fetch, keyword ranking, ``max_results`` capping with a truncation note,
the bare-``*``→catalog-only rule (the old wildcard schema dump must stay gone),
and the VectorStoreProvider used by skills.
"""

from __future__ import annotations

import unittest

from yuyutsava.discovery import (
    CatalogEntry,
    KeywordCatalogProvider,
    VectorStoreProvider,
    make_discovery_search_tool,
)
from yuyutsava.retrieval.hit import Hit


def _entry(name: str, group: str, blurb: str) -> CatalogEntry:
    return CatalogEntry(
        id=name,
        group=group,
        blurb=blurb,
        match_text=f"{name} {blurb}",
        load_detail=lambda name=name: f"SCHEMA<{name}>",
    )


def _provider() -> KeywordCatalogProvider:
    return KeywordCatalogProvider([
        _entry("tr_write_file", "tr", "write a file to disk"),
        _entry("tr_read_file", "tr", "read a file from disk"),
        _entry("tr_grep", "tr", "search file contents"),
        _entry("ws_tavily_search", "ws", "search the web"),
        _entry("ws_exa_search", "ws", "search the web with exa"),
        _entry("db_query", "db", "run a read-only sql query"),
    ])


class KeywordProviderTests(unittest.IsolatedAsyncioTestCase):
    def test_catalog_groups_by_namespace(self) -> None:
        block = _provider().catalog_block()
        assert block is not None
        # grouped headers + names, no schemas
        self.assertIn("tr_*:", block)
        self.assertIn("  - tr_write_file: write a file to disk", block)
        self.assertIn("ws_*:", block)
        self.assertNotIn("SCHEMA<", block)

    def test_empty_catalog_is_none(self) -> None:
        self.assertIsNone(KeywordCatalogProvider([]).catalog_block())

    async def test_select_exact(self) -> None:
        r = await _provider().search("select:tr_write_file,ws_exa_search", 5)
        self.assertEqual([e.id for e in r.entries], ["tr_write_file", "ws_exa_search"])
        self.assertEqual(r.note, "")

    async def test_select_reports_unknown(self) -> None:
        r = await _provider().search("select:tr_write_file,nope", 5)
        self.assertEqual([e.id for e in r.entries], ["tr_write_file"])
        self.assertIn("nope", r.note)

    async def test_keyword_ranks_by_overlap(self) -> None:
        r = await _provider().search("search the web", 5)
        ids = [e.id for e in r.entries]
        # both web-search tools must rank above unrelated ones; file/db excluded
        self.assertIn("ws_tavily_search", ids[:2])
        self.assertIn("ws_exa_search", ids[:2])
        self.assertNotIn("db_query", ids)

    async def test_keyword_caps_at_max_results_with_note(self) -> None:
        r = await _provider().search("file", 2)  # 'file' is in 3 tr_* match_texts
        self.assertEqual(len(r.entries), 2)
        self.assertIn("more match", r.note)

    async def test_glob_matches_and_caps(self) -> None:
        r = await _provider().search("tr_*", 2)
        self.assertEqual(len(r.entries), 2)
        self.assertTrue(all(e.id.startswith("tr_") for e in r.entries))
        self.assertIn("more match", r.note)


class SearchToolTests(unittest.IsolatedAsyncioTestCase):
    def _tool(self):
        return make_discovery_search_tool(_provider(), name="tool_search", noun="tool")

    async def test_star_returns_catalog_not_schemas(self) -> None:
        out = await self._tool().ainvoke({"query": "*"})
        self.assertIn("tr_write_file: write a file", out)
        self.assertNotIn("SCHEMA<", out)

    async def test_empty_returns_catalog(self) -> None:
        out = await self._tool().ainvoke({"query": "   "})
        self.assertIn("tr_*:", out)

    async def test_select_expands_full_detail(self) -> None:
        out = await self._tool().ainvoke({"query": "select:tr_grep"})
        self.assertIn("SCHEMA<tr_grep>", out)

    async def test_no_match_message(self) -> None:
        out = await self._tool().ainvoke({"query": "quantum teleportation"})
        self.assertIn("no tool matched", out)

    async def test_tool_is_named(self) -> None:
        self.assertEqual(self._tool().name, "tool_search")


class _FakeSkillStore:
    def __init__(self, hits: list[Hit]) -> None:
        self._hits = hits
        self.last_k: int | None = None

    async def search(self, query, k=5, **kwargs):
        self.last_k = k
        return self._hits[:k]


class VectorProviderTests(unittest.IsolatedAsyncioTestCase):
    def _provider(self):
        hits = [
            Hit("pdf-to-archive", "convert a pdf then archive it", 0.9, {"scope": "personal"}),
            Hit("resize-images", "batch resize images", 0.7, {"scope": "bundled"}),
        ]
        bodies = {"pdf-to-archive": "FULL BODY pdf", "resize-images": "FULL BODY resize"}
        return VectorStoreProvider(_FakeSkillStore(hits), loader=bodies.__getitem__)

    def test_catalog_block_is_none(self) -> None:
        # large/growing set: discovered via search, not enumerated
        self.assertIsNone(self._provider().catalog_block())

    async def test_search_returns_metadata_not_body(self) -> None:
        r = await self._provider().search("pdf", 5)
        rendered = "\n".join(e.load_detail() for e in r.entries)
        self.assertIn("pdf-to-archive: convert a pdf", rendered)
        self.assertNotIn("FULL BODY", rendered)  # body stays behind sk_read_skill

    async def test_select_returns_full_body(self) -> None:
        r = await self._provider().search("select:pdf-to-archive", 5)
        self.assertEqual(r.entries[0].load_detail(), "FULL BODY pdf")


if __name__ == "__main__":
    unittest.main()
