"""Spillover routing: failover to the SQLite buffer + drain-and-delete reconcile.

No live Postgres needed — the *primary* is faked to raise the same runtime
errors a dead Postgres would, and a capturing fake pool stands in for the drain
target so we can assert the buffer is emptied (drain-and-delete: a row never
lives in both stores).
"""

from __future__ import annotations

import contextlib
import tempfile
import time
import unittest
from pathlib import Path

import psycopg

from yuyutsava.storage.backend import StorageSettings
from yuyutsava.storage.events import Store
from yuyutsava.storage.events.sqlite_backend import (
    SqliteEventStore,
    SqliteEventsBackend,
    SqliteProposalStore,
)
from yuyutsava.storage.models import Proposal
from yuyutsava.storage.routing.facade import RoutedStore
from yuyutsava.storage.routing.health import StorageHealth
from yuyutsava.storage.routing.reconcile import Reconciler


def _proposal(pid: str, event_id: str = "e1") -> Proposal:
    now = time.time()
    return Proposal(
        proposal_id=pid, event_id=event_id, topic="fs.write", summary="s",
        proposed="do", subagent="a", urgency=1, created_ts=now, expires_ts=now + 60,
        status="pending", session_id=None, agent_path=None,
    )


class _RaisingPrimary:
    """A Pg twin whose every write trips a Postgres runtime error."""

    async def put_event_payload(self, **kw) -> None:
        raise psycopg.OperationalError("postgres down")


class _DownPool:
    """Pool whose connections always fail — keeps the health probe degraded."""

    @contextlib.asynccontextmanager
    async def connection(self):
        raise psycopg.OperationalError("postgres down")
        yield  # pragma: no cover


class _CaptureConn:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def execute(self, sql, params=None):
        self._sink.append((sql, params))


class _CapturePool:
    """Stands in for the drain target; records every INSERT it receives."""

    def __init__(self) -> None:
        self.inserts: list = []

    @contextlib.asynccontextmanager
    async def connection(self):
        yield _CaptureConn(self.inserts)


class RoutedStoreFailoverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.backend = SqliteEventsBackend(Path(self._tmp.name) / "state.db")
        await self.backend.open()

    async def asyncTearDown(self) -> None:
        await self.backend.close()
        self._tmp.cleanup()

    async def test_pg_error_routes_write_to_buffer_and_marks_degraded(self) -> None:
        buffer = SqliteEventStore(self.backend)
        health = StorageHealth(_DownPool(), probe_interval_sec=0.05)
        routed = RoutedStore(_RaisingPrimary(), buffer, health, name="event_payloads")

        self.assertFalse(health.degraded)
        await routed.put_event_payload(
            event_id="e1", topic="fs.write", ts=time.time(), payload={"a": 1}
        )
        # Degraded flag flipped and the write was preserved in the SQLite buffer.
        self.assertTrue(health.degraded)
        rec = await buffer.get_event_payload("e1")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.payload["a"], 1)
        await health.stop()


class ReconcileDrainAndDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.backend = SqliteEventsBackend(Path(self._tmp.name) / "state.db")
        await self.backend.open()

    async def asyncTearDown(self) -> None:
        await self.backend.close()
        self._tmp.cleanup()

    async def test_drain_moves_rows_to_pg_then_deletes_from_sqlite(self) -> None:
        # Simulate rows buffered during an outage.
        events = SqliteEventStore(self.backend)
        proposals = SqliteProposalStore(self.backend)
        await events.put_event_payload(
            event_id="e1", topic="fs.write", ts=time.time(), payload={"path": "/x"}
        )
        await proposals.put(_proposal("p1"))

        pool = _CapturePool()
        moved = await Reconciler(self.backend, pool).reconcile()

        # Both rows drained.
        self.assertEqual(moved, 2)
        # Inserted into the (fake) Postgres — proof the drain wrote downstream.
        inserted_tables = " ".join(sql for sql, _ in pool.inserts)
        self.assertIn("event_payloads", inserted_tables)
        self.assertIn("proposals", inserted_tables)
        # event_payloads must drain before proposals (FK order).
        first_table = pool.inserts[0][0]
        self.assertIn("event_payloads", first_table)
        # Drain-and-delete: the SQLite buffer is now EMPTY — no duplication.
        self.assertEqual(
            await self.backend.fetchall("SELECT event_id FROM event_payloads"), []
        )
        self.assertEqual(
            await self.backend.fetchall("SELECT proposal_id FROM proposals"), []
        )

    async def test_empty_buffer_drains_nothing(self) -> None:
        moved = await Reconciler(self.backend, _CapturePool()).reconcile()
        self.assertEqual(moved, 0)


class PureSqliteModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_sqlite_backend_uses_plain_twins_not_routed(self) -> None:
        # backend=sqlite (no pool) → SQLite is the permanent primary; the
        # domain stores are plain twins, never RoutedStore, so nothing drains.
        store = Store.for_backend(StorageSettings(backend="sqlite"))
        self.assertNotIsInstance(store._events, RoutedStore)
        self.assertEqual(type(store._events).__name__, "SqliteEventStore")


if __name__ == "__main__":
    unittest.main()
