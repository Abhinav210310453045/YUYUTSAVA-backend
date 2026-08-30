"""``UnifiedTranscriptStore`` behaves identically on SQLite and Postgres.

Phase 2 step 2.5b, playbook order 8. Written **before** the unified store, per
``docs/architecture-review/07-migration-playbook.md`` step 2.

This is the first migration to exercise every capability the dialect offers at
once, which is why it is worth doing before the larger stores:

* ``json_param``/``json_value`` — ``content`` is `jsonb` on Postgres and TEXT on
  SQLite;
* ``ts_param``/``epoch`` — ``created_ts`` is `TIMESTAMPTZ` on Postgres and a REAL
  epoch on SQLite;
* ``ensure_parent`` — Postgres carries ``transcript_messages_thread_fk``, so a
  message for an unknown thread is rejected there and accepted on SQLite;
* ``write`` — the per-message insert loop must be one transaction.

Three real divergences the twins carried, each now pinned by a test:

1. **`created_ts` source.** SQLite passed `time.time()`; Postgres let the column
   DEFAULT fire. Two clocks — the app's and the database's — for one field.
2. **`seq` allocation.** `AUTOINCREMENT` vs `BIGSERIAL`. Both monotonic, but only
   SQLite's is guaranteed gap-free; `after_seq` paging must not assume contiguity.
3. **`content` decode.** SQLite always parsed the string; Postgres parsed it only
   `if isinstance(content, str)`. Same result today, different assumptions.

Postgres cases skip when no server is reachable, so the suite is useful in
zero-config mode too.

Run:  .venv/bin/python test/storage/test_transcript_store_parity.py
"""

from __future__ import annotations

# NOTE: helpers below use the raw pool connection, which yields TUPLES.
# They read positionally on purpose — reading by name only worked while the
# dialect was leaking `dict_row` onto pooled connections (finding AT).

import os
import socket
import tempfile
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


def _msgs(*texts: str):
    """Human messages with stable ids, so dedup is testable."""
    from langchain_core.messages import HumanMessage

    return [HumanMessage(content=t, id=f"m-{t}") for t in texts]


class _TranscriptContract:
    """Behaviour both backends must agree on."""

    async def test_put_then_list_roundtrips(self) -> None:
        tid = self.tid("round")
        n = await self.store.put_messages(tid, _msgs("hello", "world"))
        self.assertEqual(n, 2)
        rows = await self.store.list_messages(tid)
        self.assertEqual([r.type for r in rows], ["human", "human"])
        self.assertEqual(len(rows), 2)

    async def test_content_is_decoded_not_a_string(self) -> None:
        """``content`` is jsonb on PG and TEXT on SQLite; callers index into it."""
        tid = self.tid("decode")
        await self.store.put_messages(tid, _msgs("hi"))
        row = (await self.store.list_messages(tid))[0]
        self.assertIsInstance(
            row.content, dict,
            "content came back as a string. The web router does "
            "content.get('data', {}) on this — it would silently render nothing.",
        )
        self.assertIn("data", row.content)

    async def test_put_is_idempotent_on_message_id(self) -> None:
        """Re-persisting a checkpoint must not duplicate the conversation."""
        tid = self.tid("dedup")
        await self.store.put_messages(tid, _msgs("a", "b"))
        again = await self.store.put_messages(tid, _msgs("a", "b", "c"))
        self.assertEqual(
            again, 1,
            "put_messages re-inserted messages it had already stored; a resumed "
            "thread would show every turn twice",
        )
        self.assertEqual(len(await self.store.list_messages(tid)), 3)

    async def test_empty_input_is_a_no_op(self) -> None:
        self.assertEqual(await self.store.put_messages(self.tid("empty"), []), 0)

    async def test_seq_is_ascending(self) -> None:
        tid = self.tid("order")
        await self.store.put_messages(tid, _msgs("1", "2", "3"))
        seqs = [r.seq for r in await self.store.list_messages(tid)]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), 3)

    async def test_after_seq_pages_without_assuming_contiguity(self) -> None:
        """``AUTOINCREMENT`` and ``BIGSERIAL`` may both leave gaps.

        Paging must use "greater than the last seq I saw", never "last + 1".
        """
        tid = self.tid("page")
        await self.store.put_messages(tid, _msgs("1", "2", "3"))
        rows = await self.store.list_messages(tid)
        rest = await self.store.list_messages(tid, after_seq=rows[0].seq)
        self.assertEqual(len(rest), 2)
        self.assertNotIn(rows[0].seq, [r.seq for r in rest])

    async def test_limit_is_honoured(self) -> None:
        tid = self.tid("limit")
        await self.store.put_messages(tid, _msgs("1", "2", "3"))
        self.assertEqual(len(await self.store.list_messages(tid, limit=2)), 2)

    async def test_threads_are_isolated(self) -> None:
        a, b = self.tid("iso-a"), self.tid("iso-b")
        await self.store.put_messages(a, _msgs("a1"))
        await self.store.put_messages(b, _msgs("b1"))
        rows = await self.store.list_messages(a)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].thread_id, a)

    async def test_created_ts_is_an_epoch_float(self) -> None:
        """One clock, one type. Postgres stores TIMESTAMPTZ, SQLite a REAL.

        The twins also disagreed on *who* set it: SQLite passed ``time.time()``,
        Postgres let the column DEFAULT fire. The unified store writes it
        explicitly on both, so a row's timestamp comes from the application on
        either backend and the TTL sweep compares like with like.
        """
        import time as _t

        before = _t.time()
        tid = self.tid("ts")
        await self.store.put_messages(tid, _msgs("t"))
        row = (await self.store.list_messages(tid))[0]
        self.assertIsInstance(row.created_ts, float)
        self.assertGreaterEqual(row.created_ts, before - 5)
        self.assertLessEqual(row.created_ts, _t.time() + 5)

    async def test_delete_older_than_respects_the_cutoff(self) -> None:
        import time as _t

        tid = self.tid("ttl")
        await self.store.put_messages(tid, _msgs("keep"))
        await self.store.delete_older_than(_t.time() - 3600)
        self.assertEqual(
            len(await self.store.list_messages(tid)), 1,
            "the TTL sweep deleted a message newer than the cutoff",
        )
        await self.store.delete_older_than(_t.time() + 3600)
        self.assertEqual(len(await self.store.list_messages(tid)), 0)

    async def test_unknown_thread_lists_empty(self) -> None:
        self.assertEqual(await self.store.list_messages(self.tid("ghost")), [])

    async def test_task_id_is_optional(self) -> None:
        tid = self.tid("task")
        self.assertEqual(await self.store.put_messages(tid, _msgs("x"), task_id=None), 1)
        self.assertEqual(
            await self.store.put_messages(tid, _msgs("y"), task_id="task-123"), 1)


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


class SqliteUnifiedTranscript(_TranscriptContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.context.transcript_store_unified import sqlite_transcript_store

        self._tmp = tempfile.TemporaryDirectory()
        self.store = sqlite_transcript_store(Path(self._tmp.name) / "state.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def tid(self, name: str) -> str:
        return f"thread-{name}"


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class PostgresUnifiedTranscript(_TranscriptContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.context.transcript_store_unified import pg_transcript_store
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg.pool import PgPool

        self._suffix = f"{os.getpid()}-{id(self)}"
        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_pg_dsn()))
        await self.pool.open()
        self.store = pg_transcript_store(self.pool)
        self._threads: list[str] = []

    async def asyncTearDown(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM transcript_messages WHERE thread_id LIKE %s",
                (f"thread-%{self._suffix}",),
            )
            await conn.execute(
                "DELETE FROM threads WHERE thread_id LIKE %s",
                (f"thread-%{self._suffix}",),
            )
        await self.pool.close()

    def tid(self, name: str) -> str:
        return f"thread-{name}-{self._suffix}"

    async def test_parent_thread_row_is_created(self) -> None:
        """Postgres-only: ``transcript_messages_thread_fk`` needs the hub row.

        SQLite has no such constraint, so this path exists on one backend only
        and is exactly the kind of asymmetry the dialect is meant to absorb —
        ``ensure_parent`` is a no-op on SQLite.
        """
        tid = self.tid("fk")
        await self.store.put_messages(tid, _msgs("needs a parent"))
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM threads WHERE thread_id = %s", (tid,))
            row = await cur.fetchone()
        self.assertEqual(
            row[0], 1,
            "the threads hub row was not created, so the insert can only have "
            "succeeded because the FK is missing — or it did not succeed",
        )


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
