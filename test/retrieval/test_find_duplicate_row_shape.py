"""``find_duplicate`` works whichever row shape the caller's connection yields.

A live daemon failure:

    memory: task_outcome write failed
      File ".../memory/store_unified.py", line 199, in add
        return await d.write(_do)
      File ".../retrieval/pg.py", line 143, in find_duplicate
        if row and float(row[1]) >= threshold:
    KeyError: 1

Postgres rows arrive in **two shapes** in this codebase:

* a plain pooled connection yields **tuples** — `row[1]` works, `row["score"]` does not;
* a connection inside ``Dialect.write()`` / ``Dialect.reading()`` yields
  **mappings** — the reverse.

``UnifiedMemoryStore.add`` deliberately runs the dedup probe *inside* the
insert's transaction, so a crash cannot commit a memory without its parent row
or vice versa. That means ``find_duplicate`` gets the mapping connection, and it
was reading positionally — so **every memory write with an embedding and dedup
enabled failed**, on the daemon's main path.

## Why nothing caught it

``test/storage/test_memory_store_parity.py`` has 35 assertions over this store
and never reached the line. The dedup probe is guarded by:

    if self._search is not None and self._dedup_threshold <= 1.0 and embedding:

and ``pg_memory_store`` defaults ``dedup_threshold=1.1`` — dedup **off**. Only
the daemon, which passes the configured ``0.97``, turns it on. A default that
disables the feature meant the suite exercised the store thoroughly and this
branch not at all.

Run:  .venv/bin/python test/retrieval/test_find_duplicate_row_shape.py
"""

from __future__ import annotations

import os
import socket
import unittest
from urllib.parse import urlparse

from yuyutsava.storage.backend import DEFAULT_PG_DSN


def _dsn() -> str:
    return os.environ.get("YUYUTSAVA_PG_DSN", "").strip() or DEFAULT_PG_DSN


def _pg_up() -> bool:
    u = urlparse(_dsn())
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 5432), 1.5):
            return True
    except OSError:
        return False


PG_UP = _pg_up()


class RowShapeHelper(unittest.TestCase):
    """The helper itself, with no database at all."""

    def test_reads_a_mapping_by_name(self) -> None:
        from yuyutsava.retrieval.pg import _by_name_or_position

        row = {"dup_id": "mem_1", "score": 0.99}
        self.assertEqual(_by_name_or_position(row, ("dup_id", "score")), ("mem_1", 0.99))

    def test_reads_a_tuple_by_position(self) -> None:
        from yuyutsava.retrieval.pg import _by_name_or_position

        self.assertEqual(
            _by_name_or_position(("mem_1", 0.99), ("dup_id", "score")), ("mem_1", 0.99))

    def test_a_mapping_without_those_keys_falls_back(self) -> None:
        """psycopg's dict rows are dicts; a stray shape must not hard-fail."""
        from yuyutsava.retrieval.pg import _by_name_or_position

        self.assertEqual(
            _by_name_or_position({0: "mem_1", 1: 0.99}, ("dup_id", "score")),
            ("mem_1", 0.99),
        )


@unittest.skipUnless(PG_UP, f"no Postgres at {_dsn()}")
class AgainstBothConnectionShapes(unittest.IsolatedAsyncioTestCase):
    """The regression: run the real query on both row factories."""

    async def asyncSetUp(self) -> None:
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg.pool import PgPool

        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_dsn()))
        await self.pool.open()

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    def _search(self):
        from yuyutsava.retrieval.pg import PgVectorSearch, PgVectorTable

        return PgVectorSearch(self.pool, PgVectorTable(
            table="memories", id_col="memory_id", text_col="text",
            embedding_col="embedding",
        ))

    async def test_pooled_connection_tuple_rows(self) -> None:
        """The shape a plain borrower gets."""
        vec = "[" + ",".join(["0.0"] * 768) + "]"
        async with self.pool.connection() as conn:
            row = await (await conn.execute("SELECT 1")).fetchone()
            self.assertNotIsInstance(row, dict, "pool no longer yields tuples")
            await self._search().find_duplicate(conn, vec, 0.97)  # must not raise

    async def test_dialect_write_mapping_rows(self) -> None:
        """The shape ``UnifiedMemoryStore.add`` actually hands it — the bug."""
        from yuyutsava.storage.dialect import PostgresDialect

        vec = "[" + ",".join(["0.0"] * 768) + "]"
        search = self._search()

        async def probe(conn):
            row = await (await conn.execute("SELECT 1 AS n")).fetchone()
            self.assertIsInstance(row, dict, "dialect.write no longer yields mappings")
            return await search.find_duplicate(conn, vec, 0.97)

        await PostgresDialect(self.pool).write(probe)  # must not raise


@unittest.skipUnless(PG_UP, f"no Postgres at {_dsn()}")
class TheRealWritePath(unittest.IsolatedAsyncioTestCase):
    """End to end through ``UnifiedMemoryStore.add`` with dedup **on**.

    The parity suite runs with the factory default (``1.1``), which switches
    dedup off. The daemon passes the configured ``0.97``. This uses the daemon's
    value, which is the only way to reach the failing line.
    """

    async def asyncSetUp(self) -> None:
        from yuyutsava.memory.config import MemorySettings
        from yuyutsava.memory.embedder import Embedder
        from yuyutsava.memory.store_unified import pg_memory_store
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg.pool import PgPool

        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_dsn()))
        await self.pool.open()
        settings = MemorySettings.from_env(default_enabled=True)
        self.embedder = Embedder(settings)
        self.store = pg_memory_store(
            self.pool, embedder=self.embedder,
            dedup_threshold=settings.dedup_threshold,   # 0.97 — dedup ON
        )
        self.tag = f"claude-regression-{id(self)}"

    async def asyncTearDown(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM memories WHERE text LIKE %s", (f"{self.tag}%",))
        await self.pool.close()

    async def test_an_embedded_write_does_not_raise(self) -> None:
        memory_id = await self.store.add(
            kind="task_outcome", text=f"{self.tag}: a write that carries an embedding")
        self.assertTrue(memory_id, "the write returned nothing")

    async def test_identical_text_is_deduplicated(self) -> None:
        """Proves ``find_duplicate`` actually RAN — not merely that add() survived.

        Without this, a `find_duplicate` that silently returned ``None`` on every
        call would pass the test above while the feature stayed broken.
        """
        text = f"{self.tag}: identical text must collapse to one row"
        first = await self.store.add(kind="task_outcome", text=text)
        second = await self.store.add(kind="task_outcome", text=text)
        self.assertEqual(
            first, second,
            "the second write created a new row — find_duplicate returned None, "
            "so the dedup probe is not actually running",
        )

    async def test_different_text_is_not_deduplicated(self) -> None:
        """Negative control — dedup that matched everything would also pass above."""
        a = await self.store.add(kind="task_outcome", text=f"{self.tag}: apples and oranges")
        b = await self.store.add(
            kind="task_outcome",
            text=f"{self.tag}: the quantum mechanics of superconducting niobium")
        self.assertNotEqual(a, b, "unrelated texts were deduplicated")


if __name__ == "__main__":
    print(f"Postgres at {_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
