"""Unit tests for SqliteThreadSummaryStore versioning.

Run:  uv run python -m unittest test.context.test_summary_store -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yuyutsava.context.summary_store_unified import sqlite_summary_store


class SummaryStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = sqlite_summary_store(Path(self._tmp.name) / "state.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_versions_increment_per_thread(self) -> None:
        self.assertEqual(await self.store.put("t1", "first"), 1)
        self.assertEqual(await self.store.put("t1", "second", token_count=42), 2)
        self.assertEqual(await self.store.put("t2", "other thread"), 1)

        latest = await self.store.latest("t1")
        self.assertEqual(latest.version, 2)
        self.assertEqual(latest.summary, "second")
        self.assertEqual(latest.token_count, 42)

    async def test_latest_missing_thread(self) -> None:
        self.assertIsNone(await self.store.latest("nope"))


if __name__ == "__main__":
    unittest.main()
