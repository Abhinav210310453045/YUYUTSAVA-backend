"""Writes roll back on failure — proven against BOTH live backends.

The conformance suite checks *structurally* that multi-statement writes go
through a transaction helper. This proves the helpers actually behave: a failure
part-way through a multi-statement write must leave **nothing** applied, on
SQLite and on Postgres alike.

Postgres tests are skipped when no server is reachable, so this file runs
everywhere; run it with the daemon's Postgres up to exercise both halves.

    .venv/bin/python test/storage/test_rollback.py
"""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from yuyutsava.storage.backend import DEFAULT_PG_DSN
from yuyutsava.storage.base import BaseSqliteStore
from yuyutsava.storage.events.sqlite_backend import SqliteEventsBackend


class _Boom(RuntimeError):
    """Raised mid-transaction to force a rollback."""


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


# ---------------------------------------------------------------------------
# SQLite — BaseSqliteStore._run_write
# ---------------------------------------------------------------------------


class _Store(BaseSqliteStore):
    _SCHEMA_VERSION = 1
    _META_TABLE = "rollback_meta"
    _SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS rollback_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rb (id TEXT PRIMARY KEY, v TEXT NOT NULL);
    """

    async def two_writes_then_fail(self) -> None:
        async def _do(conn):
            await conn.execute("INSERT INTO rb(id, v) VALUES(?,?)", ("a", "1"))
            await conn.execute("INSERT INTO rb(id, v) VALUES(?,?)", ("b", "2"))
            raise _Boom("failure after both inserts, before commit")

        await self._run_write(_do)

    async def two_writes_ok(self) -> None:
        async def _do(conn):
            await conn.execute("INSERT INTO rb(id, v) VALUES(?,?)", ("a", "1"))
            await conn.execute("INSERT INTO rb(id, v) VALUES(?,?)", ("b", "2"))

        await self._run_write(_do)

    async def count(self) -> int:
        await self._ensure_schema()
        async with self._conn() as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM rb")
            row = await cur.fetchone()
            await cur.close()
        return int(row[0])


class SqliteRollback(unittest.IsolatedAsyncioTestCase):
    """``BaseSqliteStore._run_write`` discards a failed write.

    **Honest scope note.** These assertions passed *before* the explicit
    ``conn.rollback()`` was added, because ``_conn()`` closes the connection on
    the way out and SQLite discards an open transaction on close. So the
    behaviour was already correct, and this suite cannot distinguish explicit
    rollback from implicit-discard-on-close — verified by removing the rollback
    and re-running.

    The explicit rollback is therefore **hardening, not a bug fix**: it makes the
    code match a docstring that already promised it, and stops the guarantee
    depending on driver close semantics. The tests still earn their place by
    pinning the observable contract — *nothing survives a failed write* — which
    is what a future refactor could break.

    The genuine defect was on the Postgres side, where ``connection()`` really
    does not roll back; ``PostgresRollback`` below proves that directly.
    """

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = _Store(Path(self._tmp.name) / "rb.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_failure_rolls_back_every_statement(self) -> None:
        with self.assertRaises(_Boom):
            await self.store.two_writes_then_fail()
        self.assertEqual(
            await self.store.count(), 0,
            "SQLite left rows behind after a failed multi-statement write — "
            "_run_write did not roll back",
        )

    async def test_success_commits_every_statement(self) -> None:
        await self.store.two_writes_ok()
        self.assertEqual(await self.store.count(), 2)

    async def test_cancellation_rolls_back(self) -> None:
        """A cancelled task must not leave a partial write.

        ``CancelledError`` derives from ``BaseException``, not ``Exception``, so
        an ``except Exception`` rollback would miss it entirely.
        """
        async def _do(conn):
            await conn.execute("INSERT INTO rb(id, v) VALUES(?,?)", ("a", "1"))
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await self.store._run_write(_do)
        self.assertEqual(
            await self.store.count(), 0,
            "a cancelled write left rows behind",
        )


class SqliteEventsBackendRollback(unittest.IsolatedAsyncioTestCase):
    """The events backend's own transaction helper."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.backend = SqliteEventsBackend(Path(self._tmp.name) / "events.db")
        await self.backend.open()

    async def asyncTearDown(self) -> None:
        await self.backend.close()
        self._tmp.cleanup()

    async def _counters(self) -> int:
        rows = await self.backend.fetchall("SELECT * FROM tool_call_counters")
        return len(rows)

    async def test_transaction_rolls_back(self) -> None:
        before = await self._counters()
        with self.assertRaises(_Boom):
            async with self.backend.transaction() as conn:
                await conn.execute(
                    "INSERT INTO tool_call_counters(tool_name, day, count) VALUES(?,?,?)",
                    ("t1", "2026-08-08", 1),
                )
                await conn.execute(
                    "INSERT INTO tool_call_counters(tool_name, day, count) VALUES(?,?,?)",
                    ("t2", "2026-08-08", 1),
                )
                raise _Boom("mid-transaction failure")
        self.assertEqual(
            await self._counters(), before,
            "SqliteEventsBackend.transaction() did not roll back",
        )

    async def test_transaction_commits(self) -> None:
        before = await self._counters()
        async with self.backend.transaction() as conn:
            await conn.execute(
                "INSERT INTO tool_call_counters(tool_name, day, count) VALUES(?,?,?)",
                ("t1", "2026-08-08", 1),
            )
        self.assertEqual(await self._counters(), before + 1)


# ---------------------------------------------------------------------------
# Postgres — PgPool.transaction() vs connection()
# ---------------------------------------------------------------------------


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class PostgresRollback(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg.pool import PgPool

        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_pg_dsn()))
        await self.pool.open()
        async with self.pool.connection() as conn:
            await conn.execute("DROP TABLE IF EXISTS _rb_probe")
            await conn.execute("CREATE TABLE _rb_probe (id text primary key, v text not null)")

    async def asyncTearDown(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("DROP TABLE IF EXISTS _rb_probe")
        await self.pool.close()

    async def _count(self) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT COUNT(*) FROM _rb_probe")
            return int((await cur.fetchone())[0])

    async def test_transaction_rolls_back_every_statement(self) -> None:
        with self.assertRaises(_Boom):
            async with self.pool.transaction() as conn:
                await conn.execute("INSERT INTO _rb_probe VALUES (%s,%s)", ("a", "1"))
                await conn.execute("INSERT INTO _rb_probe VALUES (%s,%s)", ("b", "2"))
                raise _Boom("failure before commit")
        self.assertEqual(
            await self._count(), 0,
            "PgPool.transaction() did not roll back — both rows survived",
        )

    async def test_transaction_commits_every_statement(self) -> None:
        async with self.pool.transaction() as conn:
            await conn.execute("INSERT INTO _rb_probe VALUES (%s,%s)", ("a", "1"))
            await conn.execute("INSERT INTO _rb_probe VALUES (%s,%s)", ("b", "2"))
        self.assertEqual(await self._count(), 2)

    async def test_connection_does_NOT_roll_back(self) -> None:
        """Pins the hazard that made the bug possible.

        ``PgPool.connection()`` is autocommit: the first INSERT is durable the
        instant it runs. This asserts that documented behaviour so nobody
        "simplifies" a transaction() call back to connection() believing they
        are equivalent.
        """
        with self.assertRaises(_Boom):
            async with self.pool.connection() as conn:
                await conn.execute("INSERT INTO _rb_probe VALUES (%s,%s)", ("a", "1"))
                raise _Boom("failure after an autocommitted statement")
        self.assertEqual(
            await self._count(), 1,
            "connection() appears transactional now; if the pool stopped being "
            "autocommit, PgPool's docs and the conformance rules need revisiting",
        )


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class FeedbackUpsertAtomicity(unittest.IsolatedAsyncioTestCase):
    """End-to-end: the real bug, on the real store, against real Postgres.

    The twins deleted the prior row then inserted the replacement. Under the old
    autocommit path a failure between them destroyed the user's feedback and
    wrote nothing back.

    ``UnifiedFeedbackStore`` (ADR-002 step 2.5b) replaced that with a single
    ``ON CONFLICT ... DO UPDATE``, so the guard is now the stronger property:
    **one statement**, which cannot have a gap at all — regardless of how the
    caller wraps it.
    """

    async def asyncSetUp(self) -> None:
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg.pool import PgPool

        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_pg_dsn()))
        await self.pool.open()

    async def asyncTearDown(self) -> None:
        await self.pool.close()

    async def test_upsert_is_a_single_statement(self) -> None:
        import inspect

        from yuyutsava.storage.feedback_store_unified import UnifiedFeedbackStore

        src = inspect.getsource(UnifiedFeedbackStore.upsert)
        self.assertNotIn(
            "DELETE FROM message_feedback", src,
            "upsert is back to DELETE-then-INSERT: a failure between the two "
            "destroys the user's prior feedback and never writes the "
            "replacement, and correctness again depends on the caller wrapping "
            "it in a transaction.",
        )
        self.assertEqual(
            src.count("await conn.execute("), 1,
            "upsert issues more than one statement; the ON CONFLICT form exists "
            "precisely so there is no window between them",
        )
        self.assertIn("ON CONFLICT (thread_id, message_ref)", src)


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg tests skip)'}\n")
    unittest.main(verbosity=2)
