"""``StoreFactory`` builds what ``build_daemon`` used to build inline.

Phase 2 step 2.6 (findings ``F-S04``, ``F-S07``). The composition root used to
re-decide "Postgres or SQLite" thirteen times, and separately wrapped three of
those stores in ``RoutedStore`` with nothing recording why. Both are now one
resolved backend plus a declared per-domain failover policy.

These tests pin the *equivalence*: for each backend, the factory must hand back
the same store types the inline branches did, and spillover must apply to
exactly the three domains it applied to before — no more (which would change
outage behaviour) and no fewer (which would lose writes).

Run:  .venv/bin/python test/storage/test_store_factory.py
"""

from __future__ import annotations

import os
import socket
import tempfile
import unittest
from urllib.parse import urlparse

from yuyutsava.storage.backend import DEFAULT_PG_DSN, StorageSettings
from yuyutsava.storage.domains import DOMAINS, Failover
from yuyutsava.storage.factory import StoreFactory


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


class _Settings:
    """Minimal stand-ins for the settings objects the factory reads."""

    class Memory:
        def __init__(self, enabled=True):
            self.enabled = enabled
            self.min_score = 0.0
            self.dedup_threshold = 0.9
            self.embed_model = "fake-embed"


class SqliteBackend(unittest.TestCase):
    """No pool -> every store is the SQLite implementation, no failover."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["YUYUTSAVA_STATE_DIR"] = self._tmp.name
        self.f = StoreFactory(StorageSettings(backend="sqlite"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_backend_resolves_to_sqlite(self) -> None:
        self.assertFalse(self.f.is_postgres)

    def test_every_store_is_a_sqlite_implementation(self) -> None:
        cases = {
            "artifacts": self.f.artifacts(),
            "summaries": self.f.summaries(),
            "transcripts": self.f.transcripts(),
            "voice": self.f.voice(),
            "tasks": self.f.tasks(),
            "usage": self.f.usage(),
            "visuals": self.f.visuals(),
            "feedback": self.f.feedback(),
            "todos": self.f.todos(),
            "memory": self.f.memory(_Settings.Memory()),
            "skills": self.f.skills(_Settings.Memory()),
        }
        for name, store in cases.items():
            with self.subTest(store=name):
                cls = type(store).__name__
                self.assertNotIn(
                    "Pg", cls, f"{name} returned a Postgres store on the SQLite backend",
                )
                self.assertNotEqual(
                    cls, "RoutedStore",
                    f"{name} was wrapped for failover on SQLite — there is nothing "
                    f"to fail over from, and the wrapper would proxy to a "
                    f"Postgres store that does not exist",
                )

    def test_memory_disabled_returns_none(self) -> None:
        self.assertIsNone(self.f.memory(_Settings.Memory(enabled=False)))


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class PostgresBackend(unittest.TestCase):
    """With a pool -> Postgres stores, and spillover exactly where declared."""

    @classmethod
    def setUpClass(cls) -> None:
        from yuyutsava.storage.pg.pool import PgPool
        from yuyutsava.storage.routing.health import StorageHealth

        cls._tmp = tempfile.TemporaryDirectory()
        os.environ["YUYUTSAVA_STATE_DIR"] = cls._tmp.name
        cls.settings = StorageSettings(backend="postgres", pg_dsn=_pg_dsn())
        # NOT opened. The factory only needs a pool *reference* to choose store
        # types — the stores hold it and connect lazily. Opening it here would
        # bind an event loop this synchronous TestCase then discards, producing
        # "Event loop is closed" noise from the pool's background worker.
        cls.pool = PgPool(cls.settings)
        cls.health = StorageHealth(cls.pool)
        cls.f = StoreFactory(
            cls.settings, pg_pool=cls.pool, health=cls.health, embedder=None,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_backend_resolves_to_postgres(self) -> None:
        self.assertTrue(self.f.is_postgres)

    def test_non_spillover_stores_are_bare_postgres(self) -> None:
        """RAISE domains must NOT be wrapped — the error has to reach the caller."""
        for name, store in {
            "artifacts": self.f.artifacts(),
            "summaries": self.f.summaries(),
            "transcripts": self.f.transcripts(),
            "voice": self.f.voice(),
            "tasks": self.f.tasks(),
            "usage": self.f.usage(),
        }.items():
            with self.subTest(store=name):
                self.assertNotEqual(
                    type(store).__name__, "RoutedStore",
                    f"{name} gained spillover failover it did not have before; "
                    f"an outage would now buffer writes instead of raising",
                )

    def test_spillover_stores_are_routed(self) -> None:
        """The three REST-path domains keep the failover they had."""
        for name, store in {
            "visuals": self.f.visuals(),
            "feedback": self.f.feedback(),
            "todos": self.f.todos(),
        }.items():
            with self.subTest(store=name):
                self.assertEqual(
                    type(store).__name__, "RoutedStore",
                    f"{name} lost its spillover buffer — a Postgres blip would "
                    f"now lose the write outright",
                )

    def test_spillover_set_matches_the_registry(self) -> None:
        """The wiring and the declaration cannot drift apart."""
        declared = {d.table for d in DOMAINS if d.failover is Failover.SPILLOVER}
        self.assertEqual(
            declared, {"visual_artifacts", "message_feedback", "todo_cards"},
            "the set of spillover domains changed; that alters outage behaviour "
            "and should be a deliberate, reviewed decision",
        )

    def test_missing_health_downgrades_to_raise(self) -> None:
        """Spillover without a health probe is not spillover — it must not pretend.

        ``RoutedStore`` needs ``StorageHealth`` to mark the process degraded and
        to trigger reconciliation. Wrapping without one would buffer writes that
        nothing ever drains.
        """
        f = StoreFactory(self.settings, pg_pool=self.pool, health=None)
        self.assertNotEqual(type(f.visuals()).__name__, "RoutedStore")


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
