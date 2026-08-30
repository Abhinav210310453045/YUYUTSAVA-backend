"""``UnifiedFeedbackStore`` behaves identically on SQLite and Postgres.

Phase 2 step 2.5b, playbook order 11. Written **before** the unified store.

``message_feedback`` holds `user_text` and `assistant_text` **verbatim** — the
actual conversation — which is why session deletion had to learn to purge it
(finding 3c). Two divergences the twins carried, both asserted here:

**The returned record did not match the stored row.** ``upsert`` builds a
``MessageFeedback`` with ``created_ts=time.time()`` and returns it. The SQLite
twin wrote that value; the Postgres twin **omitted the column** and let
``DEFAULT now()`` fire — the database server's clock. So on Postgres the caller
received a timestamp that was not the one persisted. Same class as finding AE,
but observable in a single call rather than only via the sweeper.

**Re-rating was DELETE-then-INSERT.** Correct only because both twins wrapped it
in a transaction — and the Postgres one did not, until Phase 2 fixed it (one of
the six pre-existing bugs). A single ``ON CONFLICT`` upsert removes that
dependence entirely.

Note what this suite can and cannot show: with both statements inside one
transaction, a single connection cannot observe the gap between them, so no test
here fails if the store reverts to DELETE-then-INSERT. The upsert is justified
as removing a correctness dependence on the caller, not by a red test.

Run:  .venv/bin/python test/storage/test_feedback_store_parity.py
"""

from __future__ import annotations

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


class _FeedbackContract:
    """Behaviour both backends must agree on."""

    async def _add(self, thread: str, ref: str = "m1", **over):
        kw = dict(
            thread_id=thread, session_id=thread, message_ref=ref, rating="up",
            user_text="what the user asked", assistant_text="what the agent said",
        )
        kw.update(over)
        return await self.store.upsert(**kw)

    async def test_roundtrip_preserves_every_field(self) -> None:
        tid = self.tid("round")
        rec = await self._add(tid, workspace="/tmp/ws", note="helpful")
        got = (await self.store.list_for_thread(tid))[0]
        for field in ("feedback_id", "thread_id", "session_id", "workspace",
                      "message_ref", "rating", "note", "user_text", "assistant_text"):
            with self.subTest(field=field):
                self.assertEqual(getattr(got, field), getattr(rec, field))

    async def test_returned_record_matches_what_was_stored(self) -> None:
        """The Postgres twin returned an app timestamp and stored a server one.

        ``upsert`` hands back a ``MessageFeedback`` the caller may render or log.
        If the persisted row carries a different ``created_ts``, the two disagree
        forever and nothing ever raises.
        """
        tid = self.tid("echo")
        rec = await self._add(tid)
        stored = (await self.store.list_for_thread(tid))[0]
        self.assertAlmostEqual(
            stored.created_ts, rec.created_ts, places=3,
            msg="the stored timestamp is not the one upsert returned — the row "
                "was written with the database's clock instead of the caller's",
        )

    async def test_created_ts_is_an_epoch_float(self) -> None:
        tid = self.tid("ts")
        before = time.time()
        await self._add(tid)
        got = (await self.store.list_for_thread(tid))[0]
        self.assertIsInstance(got.created_ts, float)
        self.assertGreaterEqual(got.created_ts, before - 5)

    async def test_optional_fields_may_be_null(self) -> None:
        tid = self.tid("nulls")
        await self._add(tid)
        got = (await self.store.list_for_thread(tid))[0]
        self.assertIsNone(got.workspace)
        self.assertIsNone(got.note)

    async def test_rerating_replaces_rather_than_duplicates(self) -> None:
        """The unique index is (thread_id, message_ref) — one verdict per message."""
        tid = self.tid("rerate")
        await self._add(tid, rating="up")
        await self._add(tid, rating="down", note="changed my mind")
        rows = await self.store.list_for_thread(tid)
        self.assertEqual(
            len(rows), 1,
            "re-rating created a second row; the message would show two "
            "conflicting verdicts",
        )
        self.assertEqual(rows[0].rating, "down")
        self.assertEqual(rows[0].note, "changed my mind")

    async def test_rerating_mints_a_new_id_and_timestamp(self) -> None:
        """Deliberate: a re-rating is a new judgement, not an edit of the old one."""
        tid = self.tid("newid")
        first = await self._add(tid, rating="up")
        await asyncio_sleep()
        second = await self._add(tid, rating="down")
        self.assertNotEqual(first.feedback_id, second.feedback_id)
        stored = (await self.store.list_for_thread(tid))[0]
        self.assertEqual(stored.feedback_id, second.feedback_id)
        self.assertGreater(stored.created_ts, first.created_ts)

    async def test_rerating_leaves_exactly_one_row_every_time(self) -> None:
        """One row after each upsert, across repeated re-ratings.

        Deliberately NOT called "no delete-then-insert window": that window is
        **not observable from this test**, because both statements ran inside
        one transaction and a single connection can never see between them.
        Reverting the store to DELETE-then-INSERT keeps this suite green — which
        is why the single-statement upsert is justified as removing a dependence
        on the caller's transaction, not as fixing a bug this proves.
        """
        tid = self.tid("window")
        await self._add(tid, rating="up")
        for rating in ("down", "up", "down"):
            await self._add(tid, rating=rating)
            self.assertEqual(len(await self.store.list_for_thread(tid)), 1)

    async def test_different_messages_coexist(self) -> None:
        tid = self.tid("multi")
        await self._add(tid, ref="m1")
        await self._add(tid, ref="m2")
        self.assertEqual(len(await self.store.list_for_thread(tid)), 2)

    async def test_invalid_rating_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await self._add(self.tid("bad"), rating="meh")

    async def test_list_is_newest_first(self) -> None:
        tid = self.tid("order")
        for ref in ("m1", "m2", "m3"):
            await self._add(tid, ref=ref)
            await asyncio_sleep()
        rows = await self.store.list_for_thread(tid)
        self.assertEqual([r.created_ts for r in rows],
                         sorted((r.created_ts for r in rows), reverse=True))

    async def test_limit_is_honoured(self) -> None:
        tid = self.tid("limit")
        for ref in ("m1", "m2", "m3"):
            await self._add(tid, ref=ref)
        self.assertEqual(len(await self.store.list_for_thread(tid, limit=2)), 2)

    async def test_threads_are_isolated(self) -> None:
        a, b = self.tid("iso-a"), self.tid("iso-b")
        await self._add(a)
        await self._add(b)
        rows = await self.store.list_for_thread(a)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].thread_id, a)

    async def test_list_all_spans_threads(self) -> None:
        a, b = self.tid("all-a"), self.tid("all-b")
        await self._add(a)
        await self._add(b)
        ids = {r.thread_id for r in await self.store.list_all(limit=500)}
        self.assertIn(a, ids)
        self.assertIn(b, ids)

    async def test_delete_for_thread_removes_the_conversation_text(self) -> None:
        """Session deletion depends on this — the row holds verbatim messages."""
        tid = self.tid("del")
        await self._add(tid, ref="m1")
        await self._add(tid, ref="m2")
        self.assertEqual(await self.store.delete_for_thread(tid), 2)
        self.assertEqual(await self.store.list_for_thread(tid), [])

    async def test_delete_spares_other_threads(self) -> None:
        a, b = self.tid("dsp-a"), self.tid("dsp-b")
        await self._add(a)
        await self._add(b)
        await self.store.delete_for_thread(a)
        self.assertEqual(len(await self.store.list_for_thread(b)), 1)

    async def test_delete_of_an_unknown_thread_is_zero(self) -> None:
        self.assertEqual(await self.store.delete_for_thread(self.tid("ghost")), 0)


async def asyncio_sleep() -> None:
    """A tick, so ``time.time()`` advances between two upserts."""
    import asyncio

    await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


class SqliteUnifiedFeedback(_FeedbackContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.storage.feedback_store_unified import sqlite_feedback_store

        self._tmp = tempfile.TemporaryDirectory()
        self.store = sqlite_feedback_store(Path(self._tmp.name) / "state.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def tid(self, name: str) -> str:
        return f"thread-{name}"


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class PostgresUnifiedFeedback(_FeedbackContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.feedback_store_unified import pg_feedback_store
        from yuyutsava.storage.pg.pool import PgPool

        self._suffix = f"{os.getpid()}-{id(self)}"
        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_pg_dsn()))
        await self.pool.open()
        self.store = pg_feedback_store(self.pool)

    async def asyncTearDown(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM message_feedback WHERE thread_id LIKE %s",
                (f"thread-%{self._suffix}",),
            )
        await self.pool.close()

    def tid(self, name: str) -> str:
        return f"thread-{name}-{self._suffix}"

    async def test_feedback_has_no_thread_fk(self) -> None:
        """Deliberate: feedback outlives its thread until purge_session removes it.

        Asserted rather than assumed — an FK added here would make every upsert
        for a thread with no hub row fail, and feedback is written from surfaces
        that never create one.
        """
        tid = self.tid("nofk")
        await self._add(tid)
        self.assertEqual(len(await self.store.list_for_thread(tid)), 1)


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
