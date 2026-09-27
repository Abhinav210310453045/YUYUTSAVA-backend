"""Identical memory text is stored once, with or without embeddings.

Near-duplicate detection compares vectors, so it said nothing when there were
none: on the SQLite keyword store, while the embedder was down, or against an
earlier copy stored without a vector. Identical text of the same kind now
collapses to the existing row whenever dedup is on (threshold at most 1.0), on
both backends. Different text, or the same text as a different kind, is still
its own memory; dedup off still stores every write.

Run:  .venv/bin/python -m pytest test/memory/test_exact_dedup.py
"""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

import httpx

from test.support.pg import pg_dsn, requires_pg
from yuyutsava.memory.store_unified import pg_memory_store, sqlite_memory_store
from yuyutsava.storage.backend import StorageSettings
from yuyutsava.storage.pg.pool import PgPool

DEDUP_ON = 0.97
DEDUP_OFF = 1.1


class OnSqlite(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "state.db"

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_identical_text_is_stored_once(self) -> None:
        store = sqlite_memory_store(self.db, dedup_threshold=DEDUP_ON)
        first = await store.add(kind="fact", text="the user prefers tea")
        second = await store.add(kind="fact", text="the user prefers tea")
        self.assertEqual(first, second)

    async def test_different_text_or_kind_is_its_own_memory(self) -> None:
        store = sqlite_memory_store(self.db, dedup_threshold=DEDUP_ON)
        a = await store.add(kind="fact", text="the user prefers tea")
        b = await store.add(kind="fact", text="the user prefers coffee")
        c = await store.add(kind="preference", text="the user prefers tea")
        self.assertEqual(len({a, b, c}), 3)

    async def test_dedup_off_stores_every_write(self) -> None:
        store = sqlite_memory_store(self.db, dedup_threshold=DEDUP_OFF)
        first = await store.add(kind="fact", text="same")
        second = await store.add(kind="fact", text="same")
        self.assertNotEqual(first, second)


class _Unreachable:
    async def embed(self, texts, *, mode="document"):
        raise httpx.ConnectError("refused")

    async def embed_one(self, text, *, mode="document"):
        raise httpx.ConnectError("refused")


@requires_pg
class OnPostgresWithTheEmbedderDown(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=pg_dsn()))
        await self.pool.open()
        self.tag = f"claude-exact-dedup-{uuid.uuid4().hex}"
        self.store = pg_memory_store(self.pool, _Unreachable(), dedup_threshold=DEDUP_ON)

    async def asyncTearDown(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM memories WHERE text LIKE %s", (f"{self.tag}%",))
        await self.pool.close()

    async def test_identical_text_is_stored_once_without_vectors(self) -> None:
        text = f"{self.tag}: identical text must collapse to one row"
        with self.assertLogs("yuyutsava.memory.store_unified", "WARNING"):
            first = await self.store.add(kind="task_outcome", text=text)
            second = await self.store.add(kind="task_outcome", text=text)
        self.assertEqual(first, second)
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT count(*) FROM memories WHERE text = %s", (text,))
            (count,) = await cur.fetchone()
        self.assertEqual(count, 1)

    async def test_backfill_with_the_embedder_down_is_one_line(self) -> None:
        """Every CLI start backfills vectorless rows; an unreachable embedder is
        expected there and must not print a traceback (the R-BUG-13 rule)."""
        with self.assertLogs("yuyutsava.memory.store_unified", "WARNING"):
            await self.store.add(kind="fact", text=f"{self.tag}: waits for a vector")
        with self.assertLogs("yuyutsava.retrieval.pg", "WARNING") as logs:
            fixed = await self.store.backfill_embeddings()
        self.assertEqual(fixed, 0)
        self.assertIn("embedder unreachable", logs.output[0])
        self.assertIsNone(logs.records[0].exc_info)


if __name__ == "__main__":
    unittest.main()
