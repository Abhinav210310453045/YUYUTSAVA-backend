"""``UnifiedTaskStore`` behaves identically on SQLite and Postgres.

Phase 2 step 2.5b, playbook order 10. Written **before** the unified store.

``tasks`` is the daemon's work ledger — every submitted instruction, its status,
which model ran it and what it cost. Two properties carry real weight:

**``task_id`` is the pagination cursor.** It is ``tsk_`` + ULID, and ULIDs sort
by creation time, so ``ORDER BY task_id DESC`` is chronological and
``task_id < cursor`` is a keyset page. That only holds if both backends order
the same TEXT column the same way — a collation difference would silently
reorder the task list and skip rows while paging.

**``update`` is column-whitelisted.** ``fields`` keys are interpolated into the
SQL, so ``_check_fields`` against ``_MUTABLE_COLUMNS`` is the only thing between
a caller and injection. It is asserted here on both backends rather than trusted.

The one asymmetry: Postgres has ``tasks_thread_fk``, so a task with a
``thread_id`` needs the hub row first. SQLite has no such constraint. The dialect
absorbs it via ``ensure_parent``, and the Postgres case asserts the hub row is
actually created — including the ``origin`` attribute, which only a task insert
knows.

Run:  .venv/bin/python test/storage/test_task_store_parity.py
"""

from __future__ import annotations

# NOTE: helpers below use the raw pool connection, which yields TUPLES.
# They read positionally on purpose — reading by name only worked while the
# dialect was leaking `dict_row` onto pooled connections (finding AT).

import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlparse

from yuyutsava.storage.backend import DEFAULT_PG_DSN


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


class _TaskContract:
    """Behaviour both backends must agree on."""

    def _rec(self, task_id: str, **over):
        from yuyutsava.daemon.task_registry import TaskRecord

        base = dict(
            task_id=task_id, origin="cli", instruction="do the thing",
            status="queued", thread_id=None, complexity=None, model=None,
            created_ts=time.time(), started_ts=None, finished_ts=None,
            deferred_ms=0, result_summary=None, error=None,
        )
        base.update(over)
        return TaskRecord(**base)

    async def test_insert_then_get_roundtrips_every_field(self) -> None:
        tid = self.tid("round")
        rec = self._rec(tid, complexity=3, model="gemini-2.5-flash",
                        deferred_ms=250, result_summary="ok")
        await self.store.insert(rec)
        got = await self.store.get(tid)
        for field in ("task_id", "origin", "instruction", "status", "complexity",
                      "model", "deferred_ms", "result_summary"):
            with self.subTest(field=field):
                self.assertEqual(getattr(got, field), getattr(rec, field))

    async def test_created_ts_is_a_float(self) -> None:
        """TIMESTAMPTZ on Postgres, REAL on SQLite; callers do arithmetic on it."""
        tid = self.tid("ts")
        await self.store.insert(self._rec(tid))
        got = await self.store.get(tid)
        self.assertIsInstance(got.created_ts, float)

    async def test_missing_is_none(self) -> None:
        self.assertIsNone(await self.store.get(self.tid("ghost")))

    async def test_update_returns_true_only_when_a_row_changed(self) -> None:
        tid = self.tid("upd")
        await self.store.insert(self._rec(tid))
        self.assertTrue(await self.store.update(tid, {"status": "running"}))
        self.assertFalse(
            await self.store.update(self.tid("nope"), {"status": "running"}),
            "update reported success for a task that does not exist; the "
            "registry uses this to detect a lost task",
        )

    async def test_update_applies_the_value(self) -> None:
        tid = self.tid("apply")
        await self.store.insert(self._rec(tid))
        await self.store.update(tid, {"status": "done", "result_summary": "fine"})
        got = await self.store.get(tid)
        self.assertEqual(got.status, "done")
        self.assertEqual(got.result_summary, "fine")

    async def test_update_rejects_columns_outside_the_whitelist(self) -> None:
        """``fields`` keys reach the SQL directly — this check is the only guard."""
        tid = self.tid("inject")
        await self.store.insert(self._rec(tid))
        with self.assertRaises(ValueError):
            await self.store.update(tid, {"task_id = 'x' --": "boom"})
        with self.assertRaises(ValueError):
            await self.store.update(tid, {"instruction": "not mutable"})

    async def test_update_rejects_an_empty_field_set(self) -> None:
        with self.assertRaises(ValueError):
            await self.store.update(self.tid("empty"), {})

    async def test_null_fields_round_trip(self) -> None:
        tid = self.tid("nulls")
        await self.store.insert(self._rec(tid))
        got = await self.store.get(tid)
        for field in ("thread_id", "complexity", "model", "started_ts",
                      "finished_ts", "result_summary", "error"):
            with self.subTest(field=field):
                self.assertIsNone(getattr(got, field))

    async def test_list_filters_by_status(self) -> None:
        a, b = self.tid("s-a"), self.tid("s-b")
        await self.store.insert(self._rec(a, status="queued"))
        await self.store.insert(self._rec(b, status="done"))
        ids = {r.task_id for r in await self.store.list(status="done", limit=200)}
        self.assertIn(b, ids)
        self.assertNotIn(a, ids)

    async def test_list_is_newest_first(self) -> None:
        """``task_id`` is a ULID, so DESC on it is reverse-chronological."""
        ids = [self.tid(f"o{i}") for i in range(3)]
        for i in ids:
            await self.store.insert(self._rec(i))
        got = [r.task_id for r in await self.store.list(limit=200) if r.task_id in ids]
        self.assertEqual(got, sorted(ids, reverse=True))

    async def test_cursor_pages_strictly_older(self) -> None:
        """Keyset paging: ``task_id < cursor``, so no row is served twice.

        A collation difference between the backends would show up here as a
        duplicated or skipped row rather than an error.
        """
        ids = sorted(self.tid(f"p{i}") for i in range(3))
        for i in ids:
            await self.store.insert(self._rec(i))
        page = [r.task_id for r in await self.store.list(cursor=ids[1], limit=200)
                if r.task_id in ids]
        self.assertEqual(
            page, [ids[0]],
            "the cursor is inclusive on one backend — the boundary task would "
            "render twice while paging",
        )

    async def test_limit_is_honoured(self) -> None:
        for i in range(4):
            await self.store.insert(self._rec(self.tid(f"L{i}")))
        self.assertLessEqual(len(await self.store.list(limit=2)), 2)

    async def test_status_and_cursor_combine(self) -> None:
        ids = sorted(self.tid(f"c{i}") for i in range(3))
        for i in ids:
            await self.store.insert(self._rec(i, status="done"))
        got = [r.task_id for r in
               await self.store.list(status="done", cursor=ids[2], limit=200)
               if r.task_id in ids]
        self.assertEqual(got, [ids[1], ids[0]])


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


class SqliteUnifiedTasks(_TaskContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.daemon.task_store_unified import sqlite_task_store

        self._tmp = tempfile.TemporaryDirectory()
        self.store = sqlite_task_store(Path(self._tmp.name) / "state.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def tid(self, name: str) -> str:
        return f"tsk_{name}"

    async def test_thread_id_needs_no_parent_row(self) -> None:
        """SQLite has no thread-hub FK, so ``ensure_parent`` must be a no-op."""
        tid = self.tid("nofk")
        await self.store.insert(self._rec(tid, thread_id="thread-never-created"))
        self.assertEqual((await self.store.get(tid)).thread_id, "thread-never-created")


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class PostgresUnifiedTasks(_TaskContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.daemon.task_store_unified import pg_task_store
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg.pool import PgPool

        self._suffix = f"{os.getpid()}-{id(self)}"
        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_pg_dsn()))
        await self.pool.open()
        self.store = pg_task_store(self.pool)

    async def asyncTearDown(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM llm_usage WHERE task_id LIKE %s", (f"tsk_%{self._suffix}",))
            await conn.execute(
                "DELETE FROM tasks WHERE task_id LIKE %s", (f"tsk_%{self._suffix}",))
            await conn.execute(
                "DELETE FROM threads WHERE thread_id LIKE %s", (f"thr-%{self._suffix}",))
        await self.pool.close()

    def tid(self, name: str) -> str:
        return f"tsk_{name}-{self._suffix}"

    async def test_parent_thread_row_is_created(self) -> None:
        """Postgres-only: ``tasks_thread_fk`` needs the hub row to exist first."""
        thread = f"thr-fk-{self._suffix}"
        await self.store.insert(self._rec(self.tid("fk"), thread_id=thread))
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM threads WHERE thread_id = %s", (thread,))
            self.assertEqual((await cur.fetchone())[0], 1)

    async def test_origin_is_recorded_on_the_hub_row(self) -> None:
        """The task insert is often what creates the hub row, and only it knows origin.

        ``Dialect.ensure_parent`` forwards ``**attrs`` for exactly this: an
        earlier version dropped them, which left every task-created thread with
        a null origin.
        """
        thread = f"thr-origin-{self._suffix}"
        await self.store.insert(
            self._rec(self.tid("origin"), thread_id=thread, origin="voice"))
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT origin FROM threads WHERE thread_id = %s", (thread,))
            row = await cur.fetchone()
        self.assertEqual(
            row[0], "voice",
            "the hub row was created without its origin — ensure_parent dropped "
            "the attribute it was given",
        )

    async def test_update_to_a_new_thread_creates_its_parent(self) -> None:
        """``mark_running`` patches ``thread_id`` after the task already exists."""
        tid = self.tid("late")
        thread = f"thr-late-{self._suffix}"
        await self.store.insert(self._rec(tid))
        self.assertTrue(
            await self.store.update(tid, {"status": "running", "thread_id": thread}))
        self.assertEqual((await self.store.get(tid)).thread_id, thread)


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
