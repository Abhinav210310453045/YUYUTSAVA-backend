"""Every persisted table has a declared lifecycle — no silent survivors.

Phase 2 step 2.4. Session deletion used to be driven by hand-maintained table
lists inside ``purge.py``; adding a domain meant editing a module you were not
working in, and forgetting was silent. Two domains had already slipped through
(``message_feedback``, ``pending_asks``), both holding user-visible text.

The lists are now derived from :mod:`yuyutsava.storage.domains`. These tests
keep the registry honest:

  * the derived lists still match what purge actually needs;
  * every session-scoped domain is cleaned up *somehow*, and the registry says how;
  * against a **live** Postgres, no real table is missing from the registry.

The live check is the important one — it is the only thing that can catch a
table created by a migration that nobody declared.

Run:  .venv/bin/python test/storage/test_domain_registry.py
"""

from __future__ import annotations

import inspect
import os
import socket
import unittest
from urllib.parse import urlparse

from yuyutsava.storage.backend import DEFAULT_PG_DSN
from yuyutsava.storage.domains import (
    BY_TABLE,
    DOMAINS,
    Backend,
    PurgeMode,
    purge_tables,
    session_scoped_tables,
    unaccounted,
)


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


class RegistryShape(unittest.TestCase):
    def test_no_duplicate_tables(self) -> None:
        names = [d.table for d in DOMAINS]
        dupes = sorted({n for n in names if names.count(n) > 1})
        self.assertEqual(dupes, [], f"a table is declared twice: {dupes}")

    def test_session_scoped_domains_declare_a_scope_key(self) -> None:
        for d in DOMAINS:
            if d.purge in (PurgeMode.ROW_DELETE, PurgeMode.STORE_METHOD):
                with self.subTest(table=d.table):
                    self.assertIsNotNone(
                        d.scope_key,
                        f"{d.table} is purged per session but declares no scope_key",
                    )

    def test_keep_domains_are_justified(self) -> None:
        """Surviving session deletion is a decision, so it needs a stated reason."""
        for d in DOMAINS:
            if d.purge is PurgeMode.KEEP and d.session_scoped:
                with self.subTest(table=d.table):
                    self.assertTrue(
                        d.note,
                        f"{d.table} is session-scoped but KEEP, with no note "
                        f"explaining why it should survive session deletion",
                    )

    def test_external_domains_name_their_owner(self) -> None:
        for d in DOMAINS:
            if d.purge is PurgeMode.EXTERNAL:
                with self.subTest(table=d.table):
                    self.assertTrue(
                        d.note,
                        f"{d.table} is EXTERNAL but does not say what deletes it",
                    )


class PurgeDerivation(unittest.TestCase):
    """The derived lists must still be what purge actually uses."""

    def test_purge_uses_the_registry(self) -> None:
        from yuyutsava.storage import purge

        self.assertEqual(tuple(purge._STATE_TABLES), purge_tables(Backend.SQLITE))
        self.assertEqual(tuple(purge._PG_CHILD_TABLES), purge_tables(Backend.POSTGRES))

    def test_derivation_did_not_change_behaviour(self) -> None:
        """Pins the pre-registry lists so the swap stays behaviour-preserving."""
        self.assertEqual(
            set(purge_tables(Backend.SQLITE)),
            {("transcript_messages", "thread_id"), ("artifacts", "thread_id"),
             ("thread_summaries", "thread_id"), ("voice_messages", "thread_id"),
             ("tasks", "thread_id"), ("llm_usage", "thread_id"),
             ("proposals", "session_id"), ("decisions", "session_id")},
        )
        self.assertEqual(
            set(purge_tables(Backend.POSTGRES)),
            {("llm_usage", "thread_id"), ("tasks", "thread_id"),
             ("transcript_chunks", "thread_id"), ("transcript_messages", "thread_id"),
             ("artifact_chunks", "thread_id"), ("artifacts", "thread_id"),
             ("thread_summaries", "thread_id"), ("voice_messages", "thread_id"),
             ("interrupts", "thread_id"), ("proposals", "session_id"),
             ("decisions", "session_id")},
        )

    def test_every_store_method_domain_is_called_by_purge(self) -> None:
        """A ``STORE_METHOD`` domain is only cleaned up if purge actually calls it.

        This is the assertion that would have caught both historical bugs:
        declaring the domain is not enough, ``purge_session`` has to invoke its
        store.
        """
        from yuyutsava.storage import purge

        src = inspect.getsource(purge.purge_session)
        for d in DOMAINS:
            if d.purge is not PurgeMode.STORE_METHOD:
                continue
            with self.subTest(table=d.table):
                self.assertIn(
                    d.table, src,
                    f"{d.table} is declared STORE_METHOD but purge_session never "
                    f"records it. Its rows survive session deletion.\n"
                    f"Registry note: {d.note}",
                )


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class LiveSchemaCoverage(unittest.IsolatedAsyncioTestCase):
    """The registry vs. the tables that actually exist.

    Static lists cannot catch a table a migration created but nobody declared.
    Introspection can.
    """

    async def _live_tables(self) -> frozenset[str]:
        import psycopg

        async with await psycopg.AsyncConnection.connect(_pg_dsn(), connect_timeout=5) as conn:
            cur = await conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE'"
            )
            return frozenset(r[0] for r in await cur.fetchall())

    async def test_no_live_table_is_undeclared(self) -> None:
        missing = unaccounted(await self._live_tables())
        self.assertEqual(
            sorted(missing), [],
            f"Live Postgres has tables the domain registry does not describe: "
            f"{sorted(missing)}\n"
            f"Declare each in yuyutsava/storage/domains.py with how it is cleaned "
            f"up. An undeclared table is how message_feedback and pending_asks "
            f"came to survive session deletion.",
        )

    async def test_every_scoped_live_table_is_accounted_for(self) -> None:
        """Any live table with thread_id/session_id must have a lifecycle."""
        import psycopg

        async with await psycopg.AsyncConnection.connect(_pg_dsn(), connect_timeout=5) as conn:
            cur = await conn.execute(
                "SELECT DISTINCT table_name FROM information_schema.columns "
                "WHERE table_schema='public' AND column_name IN ('thread_id','session_id')"
            )
            scoped = frozenset(r[0] for r in await cur.fetchall())

        declared_scoped = session_scoped_tables(Backend.POSTGRES)
        gaps = sorted(
            t for t in scoped
            if t not in declared_scoped
            and not (t in BY_TABLE and BY_TABLE[t].purge is PurgeMode.KEEP)
        )
        self.assertEqual(
            gaps, [],
            f"Live tables carry session data but the registry does not treat them "
            f"as session-scoped: {gaps}. Each either needs a scope_key + purge "
            f"mode, or an explicit KEEP with a reason.",
        )


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (live checks skip)'}\n")
    unittest.main(verbosity=2)
