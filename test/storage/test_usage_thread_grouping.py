"""Per-conversation cost is answerable on BOTH backends (`group_by=thread`).

Resolves the divergence recorded as finding U (Option C, chosen 2026-08-08).

The problem: ``UsagePolicy`` tags tinker turns ``task_id="tinker:<card_id>"``,
but a card is not an orchestrator task. Postgres enforces
``llm_usage_task_fk REFERENCES tasks(task_id)``, so the insert nulls the tag and
the spend lands in the anonymous ``''`` bucket. SQLite has no FK and keeps it.
Result: ``GET /usage?group_by=task`` answered "what did card 42 cost?" on SQLite
and could not on Postgres.

Option C does not change storage or the constraint. It observes that the card
identity was never lost — it is in ``thread_id`` (``todo:<card_id>``), written
identically on both backends — and exposes that column to the report.

These tests pin both halves: the new grouping works everywhere, and the old
divergence is still there (unchanged, by design) so nobody mistakes this for a
storage fix.

Run:  .venv/bin/python test/storage/test_usage_thread_grouping.py
"""

from __future__ import annotations

import os
import socket
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
        with socket.create_connection((u.hostname or "127.0.0.1", u.port or 5432), timeout=1.5):
            return True
    except OSError:
        return False


PG_UP = _pg_reachable()
CARD_THREAD = "todo:card_grouping_test"


def _tinker_row(rid: str, cost: float) -> UsageRow:
    """One tinker turn: task_id names a CARD, which is not a row in `tasks`."""
    return UsageRow(
        id=rid, ts=time.time(), thread_id=CARD_THREAD,
        task_id="tinker:card_grouping_test", role="tinker", model="claude-x",
        input_tokens=1000, output_tokens=200, est_cost_usd=cost,
    )


class _ThreadGroupingContract:
    async def test_thread_grouping_recovers_per_conversation_cost(self) -> None:
        await self.store.add(_tinker_row("usg_tg_1", 0.05))
        await self.store.add(_tinker_row("usg_tg_2", 0.07))

        rows = await self.store.aggregate(group_by="thread")
        mine = [r for r in rows if r.key == CARD_THREAD]
        self.assertEqual(len(mine), 1, f"no per-thread row for {CARD_THREAD}: {rows}")
        self.assertAlmostEqual(mine[0].est_cost_usd, 0.12, places=6)
        self.assertEqual(mine[0].calls, 2)
        self.assertEqual(mine[0].input_tokens, 2000)

    async def test_thread_grouping_is_accepted(self) -> None:
        """Rejected before Option C; the enum and validator must both allow it."""
        await self.store.aggregate(group_by="thread")  # must not raise

    async def test_unknown_grouping_still_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await self.store.aggregate(group_by="nonsense")

    async def test_other_groupings_unaffected(self) -> None:
        await self.store.add(_tinker_row("usg_tg_3", 0.05))
        by_model = await self.store.aggregate(group_by="model")
        self.assertTrue(any(r.key == "claude-x" for r in by_model))
        totals = await self.store.aggregate()
        self.assertTrue(any(r.key == "all" for r in totals))


class SqliteThreadGrouping(_ThreadGroupingContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.daemon.usage import SqliteUsageStore

        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteUsageStore(Path(self._tmp.name) / "state.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_sqlite_still_keeps_the_orphan_task_tag(self) -> None:
        """Option C changed no storage semantics — this divergence remains.

        Pinned deliberately: if a later change makes SQLite null orphans too
        (Option A), that is a decision worth failing a test over rather than
        discovering from a cost report.
        """
        await self.store.add(_tinker_row("usg_tg_4", 0.05))
        row = (await self.store.list(limit=1))[0]
        self.assertEqual(row.task_id, "tinker:card_grouping_test")


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class PostgresThreadGrouping(_ThreadGroupingContract, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.daemon.usage import PgUsageStore
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg.pool import PgPool

        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_pg_dsn()))
        await self.pool.open()
        self.store = PgUsageStore(self.pool)
        await self._clean()

    async def asyncTearDown(self) -> None:
        await self._clean()
        await self.pool.close()

    async def _clean(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM llm_usage WHERE thread_id = %s", (CARD_THREAD,)
            )

    async def test_postgres_still_nulls_the_orphan_task_tag(self) -> None:
        """The FK workaround is untouched — Option C routed around it, not through.

        This is the behaviour that made per-card cost unanswerable via
        ``group_by=task``. It stays, because ``llm_usage_task_fk`` is doing its
        job: ``task_id`` means "a real orchestrator task".
        """
        await self.store.add(_tinker_row("usg_tg_5", 0.05))
        rows = [r for r in await self.store.list(limit=50) if r.id == "usg_tg_5"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0].task_id, "",
            "Postgres kept an orphan task_id — llm_usage_task_fk may have been "
            "relaxed, which is Option B, not the Option C that was chosen",
        )


class ApiSurface(unittest.TestCase):
    """The HTTP layer must accept the new grouping, not just the store."""

    def test_endpoint_and_schema_allow_thread(self) -> None:
        import typing

        from yuyutsava.daemon.usage import GroupBy
        from yuyutsava.daemon.web.schemas.usage import UsageOut

        self.assertIn("thread", typing.get_args(GroupBy))
        schema_arg = typing.get_args(UsageOut.model_fields["group_by"].annotation)[0]
        self.assertIn(
            "thread", typing.get_args(schema_arg),
            "UsageOut still rejects group_by='thread'; the response schema was "
            "not updated alongside the store",
        )


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
