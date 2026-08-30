"""``UnifiedThreadSummaryStore`` matches both twins, on both backends.

Second domain migrated onto the dialect adapter (Phase 2 step 2.5b). Same
acceptance shape as the visuals migration. It originally ran against all four
implementations; the old twins were deleted on 2026-08-08 once the unified store
matched them — except on one test, where it beat them (below).

``test_concurrent_puts_get_distinct_versions`` is why this suite matters. It was
written to confirm an assumption — that a single
``INSERT ... SELECT COALESCE(MAX(version),0)+1 ... RETURNING`` cannot interleave
with a concurrent writer — and it **disproved** it against a live server. At
READ COMMITTED the inner ``SELECT`` reads a transaction snapshot, so concurrent
writers pick the same version and one dies on ``thread_summaries_pkey``.

The old ``PgThreadSummaryStore`` had that race; the unified store retries on
duplicate-key and does not. So the twins were not merely matched here, they were
**beaten** — which is the strongest argument available for finishing the
migration on the remaining domains.

Run:  .venv/bin/python test/storage/test_summary_store_parity.py
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


class _SummaryContract:
    """Behaviour every ThreadSummaryStore implementation must satisfy."""

    async def test_put_returns_incrementing_versions(self) -> None:
        self.assertEqual(await self.store.put(self.thread, "first"), 1)
        self.assertEqual(await self.store.put(self.thread, "second"), 2)
        self.assertEqual(await self.store.put(self.thread, "third"), 3)

    async def test_latest_returns_the_newest_version(self) -> None:
        await self.store.put(self.thread, "old")
        await self.store.put(self.thread, "new")
        got = await self.store.latest(self.thread)
        self.assertIsNotNone(got)
        self.assertEqual(got.summary, "new")
        self.assertEqual(got.version, 2)
        self.assertEqual(got.thread_id, self.thread)

    async def test_latest_unknown_thread_is_none(self) -> None:
        self.assertIsNone(await self.store.latest("no-such-thread"))

    async def test_token_count_and_task_id_roundtrip(self) -> None:
        await self.store.put(self.thread, "s", token_count=1234, task_id="task-9")
        got = await self.store.latest(self.thread)
        self.assertEqual(got.token_count, 1234)
        self.assertEqual(got.task_id, "task-9")

    async def test_defaults_when_optional_fields_omitted(self) -> None:
        await self.store.put(self.thread, "s")
        got = await self.store.latest(self.thread)
        self.assertEqual(got.token_count, 0)
        self.assertIsNone(got.task_id)

    async def test_versions_are_per_thread(self) -> None:
        await self.store.put(self.thread, "a")
        await self.store.put(self.thread, "b")
        other = await self.store.put(self.thread + "-other", "x")
        self.assertEqual(other, 1, "version counter leaked across threads")

    async def test_concurrent_puts_get_distinct_versions(self) -> None:
        """No two concurrent writers may claim the same version.

        The reason both backends were moved to a single
        ``INSERT ... SELECT ... RETURNING``: a read-then-write pair can
        interleave, and the primary key is ``(thread_id, version)``.
        """
        results = await asyncio.gather(
            *(self.store.put(self.thread, f"s{i}") for i in range(5)),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        self.assertEqual(errors, [], f"concurrent puts raised: {errors}")
        self.assertEqual(
            sorted(results), [1, 2, 3, 4, 5],
            f"concurrent puts produced duplicate/skipped versions: {sorted(results)}",
        )


class _SqliteCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "state.db"
        self.thread = "thread-summary-parity"

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()


class SqliteUnified(_SummaryContract, _SqliteCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from yuyutsava.context.summary_store_unified import sqlite_summary_store

        self.store = sqlite_summary_store(self.db)


class _PgCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg.pool import PgPool

        self.thread = f"thread-summary-{os.getpid()}-{id(self)}"
        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_pg_dsn()))
        await self.pool.open()

    async def asyncTearDown(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM thread_summaries WHERE thread_id LIKE %s", (self.thread + "%",)
            )
        await self.pool.close()


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class PostgresUnified(_SummaryContract, _PgCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from yuyutsava.context.summary_store_unified import pg_summary_store

        self.store = pg_summary_store(self.pool)


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
