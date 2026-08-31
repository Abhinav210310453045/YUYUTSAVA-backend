"""``UnifiedInterruptsStore`` on SQLite and Postgres.

Phase 2 step 2.5b, playbook order 14. Written **before** the unified store.

``interrupts`` is the HITL audit log: every permission prompt and question the
agent put to the user, and what they answered. Its defining property is that it
is **best-effort on the write path** — ``record`` and ``resolve`` sit in front
of a live user prompt, so a store failure must degrade to a missing audit row,
never to a blocked prompt. That contract gets explicit coverage here, because it
is the kind of thing a refactor quietly turns into a raise.

Notably this domain does **not** have the two-clock ``created_at`` bug that
transcripts, feedback, memory and artifacts all carried (findings AE, AH, AI,
AJ): the column is ``DOUBLE PRECISION`` on Postgres and both twins bound
``time.time()`` explicitly. Asserted anyway, so it stays that way.

Run:  .venv/bin/python test/storage/test_interrupts_store_parity.py
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


class _InterruptsContract:
    """Behaviour both backends must agree on."""

    def _rec(self, session: str, **over):
        from yuyutsava.storage.interrupts import InterruptRecord

        base = dict(
            session_id=session, thread_id=session, invocation_mode="sync",
            payload={"tool": "tr_write_file", "args": {"path": "/tmp/x"}},
            kind="permission", agent_path="orchestrator/coder",
            requesting_agent="coder", parent_agent="orchestrator",
            operation="write", paths=["/tmp/x", "/tmp/y"], zone="workspace",
            risk_level="medium", reason="writes outside the sandbox",
            question="Allow this write?",
        )
        base.update(over)
        return InterruptRecord(**base)

    async def test_record_returns_an_id(self) -> None:
        rid = await self.store.record(self._rec(self.sid("id")))
        self.assertTrue(rid)

    async def test_roundtrip_preserves_every_field(self) -> None:
        sid = self.sid("round")
        rid = await self.store.record(self._rec(sid))
        got = (await self.store.list_for_session(sid))[0]
        self.assertEqual(got.id, rid)
        for field in ("session_id", "thread_id", "invocation_mode", "kind",
                      "agent_path", "requesting_agent", "parent_agent",
                      "operation", "zone", "risk_level", "reason", "question"):
            with self.subTest(field=field):
                self.assertEqual(getattr(got, field), getattr(self._rec(sid), field))

    async def test_payload_comes_back_as_a_dict(self) -> None:
        """jsonb on Postgres, TEXT on SQLite — callers index into it."""
        sid = self.sid("payload")
        await self.store.record(self._rec(sid))
        got = (await self.store.list_for_session(sid))[0]
        self.assertIsInstance(got.payload, dict)
        self.assertEqual(got.payload["args"]["path"], "/tmp/x")

    async def test_paths_come_back_as_a_list(self) -> None:
        sid = self.sid("paths")
        await self.store.record(self._rec(sid))
        got = (await self.store.list_for_session(sid))[0]
        self.assertEqual(got.paths, ["/tmp/x", "/tmp/y"])

    async def test_null_paths_round_trip(self) -> None:
        sid = self.sid("nopaths")
        await self.store.record(self._rec(sid, paths=None))
        self.assertIsNone((await self.store.list_for_session(sid))[0].paths)

    async def test_unserialisable_payload_does_not_lose_the_row(self) -> None:
        """``default=str`` is load-bearing — payloads carry arbitrary objects."""
        sid = self.sid("weird")
        rid = await self.store.record(self._rec(sid, payload={"p": Path("/tmp/x")}))
        self.assertTrue(rid, "an unserialisable payload cost us the audit row")
        got = (await self.store.list_for_session(sid))[0]
        self.assertEqual(got.payload["p"], "/tmp/x")

    async def test_new_rows_are_unresolved(self) -> None:
        sid = self.sid("fresh")
        await self.store.record(self._rec(sid))
        got = (await self.store.list_for_session(sid))[0]
        self.assertIsNone(got.outcome)
        self.assertIsNone(got.resolved_at)
        self.assertIsNone(got.user_response)

    async def test_created_at_is_an_epoch_float_from_the_app(self) -> None:
        """Unlike four other domains, this column never had the two-clock bug."""
        before = time.time()
        sid = self.sid("clock")
        await self.store.record(self._rec(sid))
        got = (await self.store.list_for_session(sid))[0]
        self.assertIsInstance(got.created_at, float)
        self.assertGreaterEqual(got.created_at, before - 5)
        self.assertLessEqual(got.created_at, time.time() + 5)

    async def test_resolve_records_the_answer(self) -> None:
        sid = self.sid("resolve")
        rid = await self.store.record(self._rec(sid))
        await self.store.resolve(rid, outcome="approved", user_response="yes, go ahead")
        got = (await self.store.list_for_session(sid))[0]
        self.assertEqual(got.outcome, "approved")
        self.assertEqual(got.user_response, "yes, go ahead")
        self.assertIsNotNone(got.resolved_at)

    async def test_resolve_with_no_response_is_allowed(self) -> None:
        sid = self.sid("noresp")
        rid = await self.store.record(self._rec(sid))
        await self.store.resolve(rid, outcome="denied")
        got = (await self.store.list_for_session(sid))[0]
        self.assertEqual(got.outcome, "denied")
        self.assertIsNone(got.user_response)

    async def test_resolve_of_an_empty_id_is_a_no_op(self) -> None:
        """``record`` returns "" when it failed; ``resolve`` must tolerate that."""
        await self.store.resolve("", outcome="approved")

    async def test_resolve_of_an_unknown_id_does_not_raise(self) -> None:
        await self.store.resolve("no-such-row", outcome="approved")

    async def test_mark_orphaned_flips_only_unresolved(self) -> None:
        """The resume path: a killed prompt must not stay perpetually open."""
        sid = self.sid("orphan")
        done = await self.store.record(self._rec(sid))
        await self.store.record(self._rec(sid))  # left open
        await self.store.resolve(done, outcome="approved")

        n = await self.store.mark_orphaned_for_session(sid)
        self.assertEqual(n, 1, "the already-resolved row was flipped too")

        rows = {r.id: r for r in await self.store.list_for_session(sid)}
        self.assertEqual(rows[done].outcome, "approved")
        orphan = next(r for r in rows.values() if r.id != done)
        self.assertEqual(orphan.outcome, "orphaned")
        self.assertIsNotNone(orphan.resolved_at)

    async def test_mark_orphaned_is_scoped_to_one_session(self) -> None:
        a, b = self.sid("orph-a"), self.sid("orph-b")
        await self.store.record(self._rec(a))
        await self.store.record(self._rec(b))
        await self.store.mark_orphaned_for_session(a)
        self.assertIsNone((await self.store.list_for_session(b))[0].outcome)

    async def test_mark_orphaned_with_an_empty_session_is_zero(self) -> None:
        self.assertEqual(await self.store.mark_orphaned_for_session(""), 0)

    async def test_list_for_session_is_newest_first(self) -> None:
        sid = self.sid("order")
        for _ in range(3):
            await self.store.record(self._rec(sid))
            await _tick()
        rows = await self.store.list_for_session(sid)
        self.assertEqual([r.created_at for r in rows],
                         sorted((r.created_at for r in rows), reverse=True))

    async def test_list_for_session_limit(self) -> None:
        sid = self.sid("limit")
        for _ in range(3):
            await self.store.record(self._rec(sid))
        self.assertEqual(len(await self.store.list_for_session(sid, limit=2)), 2)

    async def test_sessions_are_isolated(self) -> None:
        a, b = self.sid("iso-a"), self.sid("iso-b")
        await self.store.record(self._rec(a))
        await self.store.record(self._rec(b))
        rows = await self.store.list_for_session(a)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].session_id, a)

    async def test_list_recent_filters_by_agent_path_prefix(self) -> None:
        """The prefix is how the UI scopes the log to one agent's subtree."""
        sid = self.sid("prefix")
        await self.store.record(
            self._rec(sid, agent_path=f"orchestrator/coder-{self._mark()}"))
        await self.store.record(
            self._rec(sid, agent_path=f"tinker/writer-{self._mark()}"))
        hits = await self.store.list_recent(agent_path_prefix="orchestrator/", limit=200)
        mine = [r for r in hits if r.session_id == sid]
        self.assertEqual(len(mine), 1)
        self.assertTrue(mine[0].agent_path.startswith("orchestrator/"))

    async def test_list_recent_without_a_prefix_returns_everything(self) -> None:
        sid = self.sid("noprefix")
        await self.store.record(self._rec(sid))
        hits = await self.store.list_recent(limit=200)
        self.assertIn(sid, [r.session_id for r in hits])

    async def test_list_recent_limit(self) -> None:
        sid = self.sid("rlimit")
        for _ in range(3):
            await self.store.record(self._rec(sid))
        self.assertLessEqual(len(await self.store.list_recent(limit=2)), 2)

    async def test_unknown_session_lists_empty(self) -> None:
        self.assertEqual(await self.store.list_for_session(self.sid("ghost")), [])


async def _tick() -> None:
    import asyncio

    await asyncio.sleep(0.01)


class BestEffortWrites(unittest.IsolatedAsyncioTestCase):
    """A store failure must never block the user prompt.

    ``record`` and ``resolve`` run in front of a live permission prompt. If they
    raise, the user sees a crash instead of a question — so the contract is:
    log, return an empty id, carry on. Exercised with a store whose every write
    explodes, because a refactor turning this into a raise is silent until the
    day the database is unavailable.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _exploding_store(self):
        from yuyutsava.storage.interrupts_unified import (
            InterruptsSchema, UnifiedInterruptsStore,
        )
        from yuyutsava.storage.dialect import SqliteDialect

        class _Boom(SqliteDialect):
            async def write(self, fn):
                raise RuntimeError("db down")

        return UnifiedInterruptsStore(
            _Boom(InterruptsSchema(Path(self._tmp.name) / "state.db"))
        )

    def _rec(self):
        from yuyutsava.storage.interrupts import InterruptRecord

        return InterruptRecord(
            session_id="s", thread_id="s", invocation_mode="sync",
            payload={}, kind="permission", agent_path="a",
        )

    async def test_record_returns_empty_string_instead_of_raising(self) -> None:
        self.assertEqual(
            await self._exploding_store().record(self._rec()), "",
            "record raised on a store failure — the user's permission prompt "
            "would crash instead of being asked",
        )

    async def test_resolve_swallows_the_failure(self) -> None:
        await self._exploding_store().resolve("row-1", outcome="approved")

    async def test_mark_orphaned_returns_zero_instead_of_raising(self) -> None:
        self.assertEqual(
            await self._exploding_store().mark_orphaned_for_session("s"), 0)


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


class SqliteUnifiedInterrupts(_InterruptsContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.storage.interrupts_unified import sqlite_interrupts_store

        self._tmp = tempfile.TemporaryDirectory()
        self.store = sqlite_interrupts_store(Path(self._tmp.name) / "interrupts.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _mark(self) -> str:
        return "sq"

    def sid(self, name: str) -> str:
        return f"sess-{name}"


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class PostgresUnifiedInterrupts(_InterruptsContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.interrupts_unified import pg_interrupts_store
        from yuyutsava.storage.pg.pool import PgPool

        self._suffix = f"{os.getpid()}-{id(self)}"
        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_pg_dsn()))
        await self.pool.open()
        self.store = pg_interrupts_store(self.pool)

    async def asyncTearDown(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM interrupts WHERE session_id LIKE %s",
                (f"sess-%{self._suffix}",))
            await conn.execute(
                "DELETE FROM threads WHERE thread_id LIKE %s",
                (f"sess-%{self._suffix}",))
        await self.pool.close()

    def _mark(self) -> str:
        return self._suffix

    def sid(self, name: str) -> str:
        return f"sess-{name}-{self._suffix}"

    async def test_parent_thread_row_is_created(self) -> None:
        """``interrupts.thread_id`` FKs to threads."""
        sid = self.sid("fk")
        await self.store.record(self._rec(sid))
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM threads WHERE thread_id = %s", (sid,))
            self.assertEqual((await cur.fetchone())[0], 1)


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
