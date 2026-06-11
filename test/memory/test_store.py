"""Unit tests for the SQLite keyword-fallback memory store.

Run:  uv run python -m unittest test.memory.test_store -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yuyutsava.memory.store import SqliteMemoryStore


class SqliteMemoryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteMemoryStore(Path(self._tmp.name) / "state.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_add_and_keyword_search(self) -> None:
        await self.store.add(kind="fact", text="User prefers PDFs sorted into ~/Documents/papers")
        await self.store.add(kind="task_outcome", text="Organized downloads folder by file type")
        await self.store.add(kind="preference", text="Reply in a concise tone")

        hits = await self.store.search("organize the downloads folder")
        self.assertTrue(hits)
        self.assertEqual(hits[0].kind, "task_outcome")

    async def test_kind_filter(self) -> None:
        await self.store.add(kind="fact", text="project yuyutsava uses postgres")
        await self.store.add(kind="summary", text="postgres migration completed in session 3")

        hits = await self.store.search("postgres", kinds=("summary",))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].kind, "summary")

    async def test_invalid_kind_coerced_to_fact(self) -> None:
        mid = await self.store.add(kind="weird", text="something")
        self.assertTrue(mid.startswith("mem_"))
        hits = await self.store.search("something")
        self.assertEqual(hits[0].kind, "fact")

    async def test_no_match_returns_empty(self) -> None:
        self.assertEqual(await self.store.search("zzz unfindable"), [])


if __name__ == "__main__":
    unittest.main()
