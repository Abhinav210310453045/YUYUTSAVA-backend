"""Unit tests for the SQLite artifact store.

Run:  uv run python -m unittest test.context.test_artifacts -v
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from yuyutsava.context.artifacts_unified import (
    UnifiedArtifactStore, sqlite_artifact_store,
)


class SqliteArtifactStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = sqlite_artifact_store(Path(self._tmp.name) / "state.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_put_get_roundtrip(self) -> None:
        content = "x" * 50_000
        aid = await self.store.put("thread-1", "ws_search", content)
        self.assertTrue(aid.startswith("art_"))

        sl = await self.store.get(aid, offset=0, length=10_000)
        self.assertIsNotNone(sl)
        self.assertEqual(len(sl.content), 10_000)
        self.assertEqual(sl.total_chars, 50_000)

        tail = await self.store.get(aid, offset=49_990, length=100)
        self.assertEqual(len(tail.content), 10)

    async def test_get_missing_returns_none(self) -> None:
        self.assertIsNone(await self.store.get("art_nope"))
        self.assertIsNone(await self.store.grep("art_nope", "x"))

    async def test_grep_lines_and_invalid_regex(self) -> None:
        body = "alpha\nbeta target line\ngamma\ntarget again\n"
        aid = await self.store.put("t", "tr_execute", body)

        hits = await self.store.grep(aid, r"target")
        self.assertEqual(len(hits), 2)
        self.assertTrue(hits[0].startswith("2:"))
        self.assertTrue(hits[1].startswith("4:"))

        bad = await self.store.grep(aid, r"([unclosed")
        self.assertEqual(len(bad), 1)
        self.assertIn("invalid regex", bad[0])

    async def test_delete_older_than(self) -> None:
        aid = await self.store.put("t", "ws_search", "old content")
        deleted = await self.store.delete_older_than(time.time() + 60)
        self.assertEqual(deleted, 1)
        self.assertIsNone(await self.store.get(aid))


if __name__ == "__main__":
    unittest.main()
