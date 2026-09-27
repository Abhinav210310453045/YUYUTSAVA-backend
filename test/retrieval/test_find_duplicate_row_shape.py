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

import hashlib
import unittest
import uuid

from test.support.pg import PG_UP, pg_dsn, requires_pg


class RowShapeHelper(unittest.TestCase):
    """The helper itself, with no database at all."""

    def test_reads_a_mapping_by_name(self) -> None:
        from yuyutsava.retrieval.pg import _by_name_or_position

        row = {"dup_id": "mem_1", "score": 0.99}
        self.assertEqual(_by_name_or_position(row, ("dup_id", "score")), ("mem_1", 0.99))

    def test_reads_a_tuple_by_position(self) -> None:
        from yuyutsava.retrieval.pg import _by_name_or_position

        self.assertEqual(
            _by_name_or_position(("mem_1", 0.99), ("dup_id", "score")), ("mem_1", 0.99)
        )

    def test_a_mapping_without_those_keys_falls_back(self) -> None:
        """psycopg's dict rows are dicts; a stray shape must not hard-fail."""
        from yuyutsava.retrieval.pg import _by_name_or_position

        self.assertEqual(
            _by_name_or_position({0: "mem_1", 1: 0.99}, ("dup_id", "score")),
            ("mem_1", 0.99),
        )


@requires_pg
class AgainstBothConnectionShapes(unittest.IsolatedAsyncioTestCase):
    """The regression: run the real query on both row factories."""

    async def asyncSetUp(self) -> None:
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg.pool import PgPool

        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=pg_dsn()))
        await self.pool.open()

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    def _search(self):
        from yuyutsava.retrieval.pg import PgVectorSearch, PgVectorTable

        return PgVectorSearch(
            self.pool,
            PgVectorTable(
                table="memories",
                id_col="memory_id",
                text_col="text",
                embedding_col="embedding",
            ),
        )

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


class _OneHotEmbedder:
    """Deterministic vectors: one-hot at a position chosen by the text's hash.

    Identical text → identical vector (cosine 1); different text → orthogonal
    (cosine 0, barring a 1-in-768 collision). This used the real embedder, so
    the dedup test passed only while a local embedding server answered —
    without one the write stores no vector, the probe is skipped by design, and
    identical text makes two rows (R-TST-14).
    """

    async def embed(self, texts: list[str], *, mode: str = "document") -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def embed_one(self, text: str, *, mode: str = "document") -> list[float]:
        return self._vec(text)

    @staticmethod
    def _vec(text: str) -> list[float]:
        from yuyutsava.memory.config import EMBEDDING_DIM

        hot = int(hashlib.sha256(text.encode()).hexdigest(), 16) % EMBEDDING_DIM
        return [1.0 if i == hot else 0.0 for i in range(EMBEDDING_DIM)]


@requires_pg
class TheRealWritePath(unittest.IsolatedAsyncioTestCase):
    """End to end through ``UnifiedMemoryStore.add`` with dedup **on**.

    The parity suite runs with the factory default (``1.1``), which switches
    dedup off. The daemon passes the configured ``0.97``. This uses the daemon's
    value, which is the only way to reach the failing line — with a
    deterministic embedder, so the outcome depends on the code, not on whether
    an embedding server is running.
    """

    async def asyncSetUp(self) -> None:
        from yuyutsava.memory.config import MemorySettings
        from yuyutsava.memory.store_unified import pg_memory_store
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg.pool import PgPool

        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=pg_dsn()))
        await self.pool.open()
        settings = MemorySettings.from_env(default_enabled=True)
        self.store = pg_memory_store(
            self.pool,
            embedder=_OneHotEmbedder(),
            dedup_threshold=settings.dedup_threshold,  # 0.97 — dedup ON
        )
        # Unique per run: id(self) can repeat across runs and match a row an
        # earlier, crashed run left behind.
        self.tag = f"claude-regression-{uuid.uuid4().hex}"

    async def asyncTearDown(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM memories WHERE text LIKE %s", (f"{self.tag}%",))
        await self.pool.close()

    async def test_an_embedded_write_does_not_raise(self) -> None:
        memory_id = await self.store.add(
            kind="task_outcome", text=f"{self.tag}: a write that carries an embedding"
        )
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
            first,
            second,
            "the second write created a new row — find_duplicate returned None, "
            "so the dedup probe is not actually running",
        )

    async def test_different_text_is_not_deduplicated(self) -> None:
        """Negative control — dedup that matched everything would also pass above."""
        a = await self.store.add(kind="task_outcome", text=f"{self.tag}: apples and oranges")
        b = await self.store.add(
            kind="task_outcome",
            text=f"{self.tag}: the quantum mechanics of superconducting niobium",
        )
        self.assertNotEqual(a, b, "unrelated texts were deduplicated")


if __name__ == "__main__":
    print(f"Postgres at {pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
