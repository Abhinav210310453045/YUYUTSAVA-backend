"""Cache-token accounting survives a round trip, on both backends.

Cache reads are the dominant cost lever on a long conversation — one real
session ran ~93 % cache-read on its last call — and ``llm_usage`` recorded only
input/output, so a cheap session and an expensive one were indistinguishable in
the ledger. These pin the parts that are easy to get subtly wrong:

* both columns round-trip through ``add``/``list`` and sum through
  ``aggregate`` on SQLite and Postgres alike;
* they are subsets of ``input_tokens``, never additions to it, so nothing may
  quietly inflate the input total by adding them;
* an existing v1 table is upgraded **in place** and its rows keep 0 rather than
  being lost or rebuilt;
* ``list(thread_id=…)`` filters server-side, which is what let the CLI stop
  reading a fixed 2,000-row window and filtering in Python.

Postgres coverage is skipped (not failed) when no server is reachable, matching
test_usage_thread_grouping.py.

Run:  .venv/bin/python test/storage/test_usage_cache_tokens.py
"""

from __future__ import annotations

import os
import socket
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlparse

from yuyutsava.daemon.usage import UsageRow
from yuyutsava.storage.backend import DEFAULT_PG_DSN


def _pg_dsn() -> str:
    return os.environ.get("YUYUTSAVA_PG_DSN", "").strip() or DEFAULT_PG_DSN


def _pg_reachable() -> bool:
    u = urlparse(_pg_dsn())
    try:
        with socket.create_connection(
            (u.hostname or "127.0.0.1", u.port or 5432), timeout=1.5
        ):
            return True
    except OSError:
        return False


PG_UP = _pg_reachable()
THREAD = "cache_tokens_test_thread"


def _row(rid: str, *, cached: int = 0, created: int = 0, thread: str = THREAD,
         tin: int = 1_000) -> UsageRow:
    return UsageRow(
        id=rid, ts=time.time(), thread_id=thread, task_id="", role="cli",
        model="claude-cache-x", input_tokens=tin, output_tokens=100,
        est_cost_usd=0.01, cache_read_tokens=cached, cache_creation_tokens=created,
    )


class _CacheTokenContract:
    async def test_both_counts_round_trip(self) -> None:
        await self.store.add(_row("usg_ct_1", cached=930, created=12))
        got = [r for r in await self.store.list(limit=50) if r.id == "usg_ct_1"]
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].cache_read_tokens, 930)
        self.assertEqual(got[0].cache_creation_tokens, 12)

    async def test_a_row_with_no_cache_detail_reads_back_as_zero(self) -> None:
        await self.store.add(_row("usg_ct_2"))
        got = [r for r in await self.store.list(limit=50) if r.id == "usg_ct_2"][0]
        self.assertEqual(got.cache_read_tokens, 0)
        self.assertEqual(got.cache_creation_tokens, 0)

    async def test_they_are_subsets_of_input_not_additions(self) -> None:
        # If a cache read were ever added to input_tokens, a 93 %-cached call
        # would report nearly double the tokens it was charged for.
        await self.store.add(_row("usg_ct_3", cached=900, tin=1_000))
        got = [r for r in await self.store.list(limit=50) if r.id == "usg_ct_3"][0]
        self.assertEqual(got.input_tokens, 1_000)
        self.assertLessEqual(got.cache_read_tokens, got.input_tokens)

    async def test_aggregate_sums_the_cache_columns(self) -> None:
        await self.store.add(_row("usg_ct_4", cached=100, created=5))
        await self.store.add(_row("usg_ct_5", cached=250, created=7))
        rows = await self.store.aggregate(group_by="thread")
        mine = [r for r in rows if r.key == THREAD]
        self.assertEqual(len(mine), 1, f"no per-thread row: {rows}")
        self.assertEqual(mine[0].cache_read_tokens, 350)
        self.assertEqual(mine[0].cache_creation_tokens, 12)

    async def test_aggregate_still_answers_the_old_questions(self) -> None:
        await self.store.add(_row("usg_ct_6", cached=1))
        by_model = await self.store.aggregate(group_by="model")
        self.assertTrue(any(r.key == "claude-cache-x" for r in by_model))
        self.assertTrue(any(r.key == "all" for r in await self.store.aggregate()))

    async def test_list_filters_by_thread_server_side(self) -> None:
        await self.store.add(_row("usg_ct_7", thread=THREAD))
        await self.store.add(_row("usg_ct_8", thread="some_other_thread"))
        ids = {r.id for r in await self.store.list(thread_id=THREAD, limit=50)}
        self.assertIn("usg_ct_7", ids)
        self.assertNotIn("usg_ct_8", ids)

    async def test_the_thread_and_time_filters_compose(self) -> None:
        now = time.time()
        old = _row("usg_ct_9")
        object.__setattr__(old, "ts", now - 3_600)
        await self.store.add(old)
        await self.store.add(_row("usg_ct_10"))
        ids = {
            r.id
            for r in await self.store.list(thread_id=THREAD, since=now - 60, limit=50)
        }
        self.assertIn("usg_ct_10", ids)
        self.assertNotIn("usg_ct_9", ids)


class SqliteCacheTokens(_CacheTokenContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.daemon.usage import SqliteUsageStore

        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteUsageStore(Path(self._tmp.name) / "state.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_a_v1_table_is_upgraded_in_place(self) -> None:
        """The columns are added to an existing table; its rows survive.

        Someone upgrading has months of usage history in state.db. Losing it —
        or rebuilding the table to add two defaulted columns — would be a far
        worse outcome than the reporting gap this closes.
        """
        from yuyutsava.daemon.usage import SqliteUsageStore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "legacy.db"

        # Exactly the v1 schema, with one row in it.
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE llm_usage_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE llm_usage (
                id            TEXT PRIMARY KEY,
                ts            REAL NOT NULL,
                thread_id     TEXT NOT NULL DEFAULT '',
                task_id       TEXT NOT NULL DEFAULT '',
                role          TEXT NOT NULL,
                model         TEXT NOT NULL,
                input_tokens  INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                est_cost_usd  REAL NOT NULL DEFAULT 0
            );
        """)
        con.execute("INSERT INTO llm_usage_meta VALUES ('schema_version', '1')")
        con.execute(
            "INSERT INTO llm_usage VALUES "
            "('usg_legacy', 1000.0, ?, '', 'cli', 'old-model', 500, 50, 0.0)",
            (THREAD,),
        )
        con.commit()
        con.close()

        store = SqliteUsageStore(path)
        rows = await store.list(limit=50)  # triggers _ensure_schema/_migrate

        legacy = [r for r in rows if r.id == "usg_legacy"]
        self.assertEqual(len(legacy), 1, "the pre-existing row was lost")
        self.assertEqual(legacy[0].input_tokens, 500)
        self.assertEqual(legacy[0].cache_read_tokens, 0)

        # And the upgraded table takes new rows with the cache counts.
        await store.add(_row("usg_ct_new", cached=42))
        got = [r for r in await store.list(limit=50) if r.id == "usg_ct_new"][0]
        self.assertEqual(got.cache_read_tokens, 42)

    async def test_the_per_thread_index_exists(self) -> None:
        await self.store.list(limit=1)  # ensure schema
        con = sqlite3.connect(self.store._db_path)
        names = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        con.close()
        self.assertIn("llm_usage_thread_ts_idx", names)


@unittest.skipUnless(PG_UP, f"no Postgres at {_pg_dsn()}")
class PgCacheTokens(_CacheTokenContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.daemon.usage import PgUsageStore
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg import migrations
        from yuyutsava.storage.pg.pool import PgPool

        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_pg_dsn()))
        await self.pool.open()
        await migrations.apply(self.pool)
        self.store = PgUsageStore(self.pool)
        await self._clean()

    async def asyncTearDown(self) -> None:
        await self._clean()
        await self.pool.close()

    async def _clean(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM llm_usage WHERE id LIKE 'usg_ct_%%'"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
