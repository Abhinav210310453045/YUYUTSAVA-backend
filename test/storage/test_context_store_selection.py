"""One backend decision for the context stores, shared by all three stacks.

Phase 3 step 3.5, closing the last open item of `F-S04`.

The CLI stack, the tinker bundle and the daemon each wrote out the same
selection by hand::

    if pg_pool is not None:
        artifact_store = pg_artifact_store(pg_pool, embedder=..., ...)
        summary_store  = pg_summary_store(pg_pool)
        transcript_store = pg_transcript_store(pg_pool)
    else:
        artifact_store = sqlite_artifact_store(state_db_path())
        ...

plus a *separate* two-condition guard for the transcript index
(``pg_pool is not None and embedder is not None``). Three copies of one
decision, and a fourth caller getting either guard wrong would silently lose
recall rather than fail — the exact shape of finding Y.

`StoreFactory.context_stores()` owns it now. This suite pins that:

* the selection agrees with the backend on both sides;
* the transcript index needs **both** a pool and an embedder;
* no stack assembler hand-rolls the branch again.

Run:  .venv/bin/python test/storage/test_context_store_selection.py
"""

from __future__ import annotations

import ast
import inspect
import os
import pathlib
import socket
import unittest
from urllib.parse import urlparse

from yuyutsava.storage.backend import DEFAULT_PG_DSN, StorageSettings
from yuyutsava.storage.factory import ContextStores, StoreFactory


def _pg_dsn() -> str:
    return os.environ.get("YUYUTSAVA_PG_DSN", "").strip() or DEFAULT_PG_DSN


def _pg_reachable() -> bool:
    u = urlparse(_pg_dsn())
    try:
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 5432), timeout=1.5):
            return True
    except OSError:
        return False


PG_UP = _pg_reachable()


class SqliteSelection(unittest.TestCase):
    def setUp(self) -> None:
        self.f = StoreFactory(StorageSettings(backend="sqlite"))

    def test_returns_all_four_slots(self) -> None:
        cs = self.f.context_stores()
        self.assertIsInstance(cs, ContextStores)
        for slot in ("artifacts", "summaries", "transcripts"):
            with self.subTest(slot=slot):
                self.assertIsNotNone(getattr(cs, slot))

    def test_no_transcript_index_without_pgvector(self) -> None:
        self.assertIsNone(
            self.f.context_stores().transcript_index,
            "a transcript index was built without pgvector — the recall "
            "middleware would try to embed against a table that is not there",
        )

    def test_stores_are_the_unified_ones(self) -> None:
        cs = self.f.context_stores()
        for slot in ("artifacts", "summaries", "transcripts"):
            with self.subTest(slot=slot):
                self.assertTrue(
                    type(getattr(cs, slot)).__name__.startswith("Unified"),
                    f"{slot} is not a unified store — a twin came back",
                )


class TranscriptIndexNeedsBoth(unittest.TestCase):
    """Pool **and** embedder. Either alone must yield ``None``, not a half-built index."""

    def test_pool_without_embedder(self) -> None:
        f = StoreFactory(StorageSettings(backend="postgres"), pg_pool=object())
        self.assertIsNone(f.transcript_index())

    def test_embedder_without_pool(self) -> None:
        f = StoreFactory(StorageSettings(backend="postgres"), embedder=object())
        self.assertIsNone(f.transcript_index())

    def test_neither(self) -> None:
        self.assertIsNone(StoreFactory(StorageSettings(backend="sqlite")).transcript_index())


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class PostgresSelection(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.memory.config import MemorySettings
        from yuyutsava.memory.embedder import Embedder
        from yuyutsava.storage.pg.pool import PgPool

        self.settings = StorageSettings(backend="postgres", pg_dsn=_pg_dsn())
        self.pool = PgPool(self.settings)
        await self.pool.open()
        self.embedder = Embedder(MemorySettings.from_env(default_enabled=True))

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    async def test_selects_the_postgres_stores(self) -> None:
        cs = StoreFactory(
            self.settings, pg_pool=self.pool, embedder=self.embedder
        ).context_stores()
        self.assertTrue(
            cs.artifacts.supports_recall,
            "Postgres + embedder gave an artifact store without recall",
        )
        self.assertIsNotNone(
            cs.transcript_index,
            "no transcript index with a live pool AND embedder — resumed "
            "threads would stop recalling their swept turns",
        )

    async def test_semantic_recall_flag_is_honoured(self) -> None:
        cs = StoreFactory(
            self.settings, pg_pool=self.pool, embedder=self.embedder
        ).context_stores(semantic_recall=False)
        self.assertFalse(
            cs.artifacts.supports_recall,
            "semantic_recall=False was ignored; every put would spawn an "
            "indexing task the operator switched off",
        )

    async def test_a_dead_pool_falls_back_to_sqlite(self) -> None:
        """``is_postgres`` is ``pg_pool is not None`` — the CLI's exact condition.

        The CLI opens its own pool and passes ``None`` when that fails, so the
        factory must treat a missing pool as SQLite even though settings say
        Postgres. Substituting the factory for the hand-written branch is only
        behaviour-preserving because of this.
        """
        cs = StoreFactory(self.settings, pg_pool=None).context_stores()
        self.assertIsNone(cs.transcript_index)
        self.assertFalse(cs.artifacts.supports_recall)


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class DialectLeavesPooledConnectionsAlone(unittest.IsolatedAsyncioTestCase):
    """A dialect read/write must not change the connection for the next borrower.

    Regression test for a live daemon failure. ``PostgresDialect`` set
    ``conn.row_factory = dict_row`` and never restored it. A pooled connection
    goes back to the pool afterwards, so every later borrower got mappings too —
    and ``PgPrefsBackend.get``, which reads ``row[0]``, started failing with
    ``KeyError: 0`` at a call site that never touched the dialect::

        W yuyutsava.prefs.runtime: runtime settings: load failed; using defaults
          File ".../storage/events/pg_stores.py", line 90, in get
            return row[0]
        KeyError: 0

    **Why every existing test missed it:** each opens its own pool and usually
    drives one kind of consumer, so the contaminated connection was never handed
    to a positional reader. The daemon shares one long-lived pool between the
    unified stores and the prefs backend, so it failed on the first boot after
    the change. Sharing is the condition, and only this test creates it.
    """

    async def asyncSetUp(self) -> None:
        from yuyutsava.storage.pg.pool import PgPool

        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_pg_dsn()))
        await self.pool.open()

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    async def _positional_read_works(self) -> bool:
        async with self.pool.connection() as conn:
            row = await (await conn.execute("SELECT 1")).fetchone()
            try:
                return row[0] == 1
            except (KeyError, TypeError):
                return False

    async def test_baseline_is_positional(self) -> None:
        """Negative control: the pool must hand out tuples to begin with.

        If it did not, the checks below would pass for the wrong reason.
        """
        self.assertTrue(
            await self._positional_read_works(),
            "the pool is not yielding tuples by default, so this suite proves "
            "nothing about contamination",
        )

    async def test_read_does_not_contaminate_the_pool(self) -> None:
        from yuyutsava.storage.dialect import PostgresDialect

        async with PostgresDialect(self.pool).reading() as conn:
            await (await conn.execute("SELECT 1 AS n")).fetchone()
        self.assertTrue(
            await self._positional_read_works(),
            "a dialect READ left row_factory=dict_row on the pooled connection; "
            "the next borrower reading row[0] gets KeyError: 0",
        )

    async def test_write_does_not_contaminate_the_pool(self) -> None:
        from yuyutsava.storage.dialect import PostgresDialect

        async def _w(conn):
            await conn.execute("SELECT 1")

        await PostgresDialect(self.pool).write(_w)
        self.assertTrue(
            await self._positional_read_works(),
            "a dialect WRITE left row_factory=dict_row on the pooled connection",
        )

    async def test_restored_even_when_the_body_raises(self) -> None:
        """A failed write must not leak the factory either."""
        from yuyutsava.storage.dialect import PostgresDialect

        async def _boom(conn):
            await conn.execute("SELECT 1")
            raise RuntimeError("deliberate")

        with self.assertRaises(RuntimeError):
            await PostgresDialect(self.pool).write(_boom)
        self.assertTrue(
            await self._positional_read_works(),
            "row_factory leaked out of a FAILED write — the restore is not in "
            "a finally block",
        )

    async def test_the_real_victim_still_works(self) -> None:
        """End-to-end: the exact call the daemon logged as failing."""
        from yuyutsava.storage.dialect import PostgresDialect
        from yuyutsava.storage.events.pg_stores import PgPrefsBackend

        async with PostgresDialect(self.pool).reading() as conn:
            await (await conn.execute("SELECT 1 AS n")).fetchone()
        prefs = PgPrefsBackend(self.pool)
        await prefs.put("dialect_leak_probe", {"ok": True})
        self.assertEqual(await prefs.get("dialect_leak_probe", None), {"ok": True})
        await prefs.delete("dialect_leak_probe")


class NoStackAssemblerHandRollsTheBranch(unittest.TestCase):
    """The three call sites must go through the factory, not rebuild the branch."""

    _ASSEMBLERS = (
        ("yuyutsava/cli/agent_stack.py", "build_agent_stack"),
        ("yuyutsava/agents/tinker/agent.py", "build_tinker_stack"),
        ("yuyutsava/daemon/bootstrap.py", "build_daemon"),
    )

    def _source(self, relpath: str, func: str) -> str:
        root = pathlib.Path(__file__).resolve().parents[2]
        tree = ast.parse((root / relpath).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == func):
                return ast.get_source_segment(
                    (root / relpath).read_text(encoding="utf-8"), node) or ""
        raise AssertionError(f"{func} not found in {relpath}")

    def test_none_construct_the_stores_directly(self) -> None:
        for relpath, func in self._ASSEMBLERS:
            src = self._source(relpath, func)
            for ctor in ("pg_artifact_store(", "sqlite_artifact_store(",
                         "pg_summary_store(", "sqlite_summary_store(",
                         "pg_transcript_store(", "sqlite_transcript_store("):
                with self.subTest(assembler=func, ctor=ctor):
                    self.assertNotIn(
                        ctor, src,
                        f"{func} constructs {ctor} itself. That is the branch "
                        f"StoreFactory.context_stores() exists to own — a "
                        f"fourth copy is how the three drifted in the first "
                        f"place.",
                    )

    def test_none_hand_roll_the_transcript_index_guard(self) -> None:
        for relpath, func in self._ASSEMBLERS:
            src = self._source(relpath, func)
            with self.subTest(assembler=func):
                self.assertNotIn(
                    "PgTranscriptIndex(", src,
                    f"{func} builds PgTranscriptIndex directly; the "
                    f"pool-AND-embedder guard belongs in one place",
                )

    def test_the_assemblers_were_actually_found(self) -> None:
        """Negative control — the checks above are vacuous on an empty source."""
        for relpath, func in self._ASSEMBLERS:
            with self.subTest(assembler=func):
                self.assertGreater(len(self._source(relpath, func)), 500)


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
