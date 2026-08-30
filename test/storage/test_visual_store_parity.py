"""``UnifiedVisualStore`` behaves identically on both backends.

Phase 2 step 2.3 acceptance, now also step 2.1b (cross-backend behavioural
conformance) for this domain.

Originally this suite ran against FOUR implementations — the SqliteVisualStore
and PgVisualStore twins plus the unified store on each dialect — and all 40
assertions passed. That result is what justified deleting the twins on
2026-08-08. With them gone, the same contract now runs against the one
implementation on both live backends, which is the property that has to keep
holding.

The suite deliberately covers the **on-disk side effect**, not just rows: a
visual row holds an absolute path to a PNG, and every delete path must unlink
the file. A raw table delete would pass a rows-only test and orphan every image.

Postgres cases skip when no server is reachable, so the file runs anywhere.

Run:  .venv/bin/python test/storage/test_visual_store_parity.py
"""

from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from yuyutsava.storage.backend import DEFAULT_PG_DSN
from yuyutsava.visuals.types import RenderResult


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

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"


def _result(kind: str = "chart", title: str = "T") -> RenderResult:
    return RenderResult(
        kind=kind, title=title, mime="image/png", image_bytes=PNG, source="src",
    )


class _VisualStoreContract:
    """Behaviour every VisualStore implementation must satisfy.

    Mixed into one TestCase per implementation. ``self.store`` and
    ``self.out_dir`` are provided by the concrete case.
    """

    async def test_save_then_get_roundtrip(self) -> None:
        rec = await self.store.save(_result(title="hello"), self.thread)
        got = await self.store.get(rec.visual_id)
        self.assertIsNotNone(got)
        self.assertEqual(got.visual_id, rec.visual_id)
        self.assertEqual(got.thread_id, self.thread)
        self.assertEqual(got.kind, "chart")
        self.assertEqual(got.title, "hello")
        self.assertEqual(got.mime, "image/png")
        self.assertEqual(got.source, "src")
        self.assertIsInstance(got.created_ts, float)
        self.assertGreater(got.created_ts, 0)

    async def test_save_writes_the_image_file(self) -> None:
        rec = await self.store.save(_result(), self.thread)
        self.assertTrue(Path(rec.path).exists(), "image file was not written")
        self.assertEqual(Path(rec.path).read_bytes(), PNG)

    async def test_get_unknown_returns_none(self) -> None:
        self.assertIsNone(await self.store.get("vis_nope"))

    async def test_list_for_thread_is_newest_first(self) -> None:
        a = await self.store.save(_result(title="a"), self.thread)
        b = await self.store.save(_result(title="b"), self.thread)
        rows = await self.store.list_for_thread(self.thread)
        self.assertEqual([r.visual_id for r in rows][:2], [b.visual_id, a.visual_id])

    async def test_list_for_thread_isolates_threads(self) -> None:
        await self.store.save(_result(), self.thread)
        await self.store.save(_result(), self.thread + "-other")
        rows = await self.store.list_for_thread(self.thread)
        self.assertTrue(all(r.thread_id == self.thread for r in rows))

    async def test_list_respects_limit(self) -> None:
        for _ in range(3):
            await self.store.save(_result(), self.thread)
        self.assertEqual(len(await self.store.list_for_thread(self.thread, limit=2)), 2)

    async def test_delete_removes_row_and_file(self) -> None:
        rec = await self.store.save(_result(), self.thread)
        path = Path(rec.path)
        self.assertTrue(path.exists())

        self.assertTrue(await self.store.delete(rec.visual_id))
        self.assertIsNone(await self.store.get(rec.visual_id))
        self.assertFalse(path.exists(), "PNG orphaned on disk after delete")

    async def test_delete_unknown_is_false(self) -> None:
        self.assertFalse(await self.store.delete("vis_nope"))

    async def test_delete_for_thread_removes_rows_and_files(self) -> None:
        recs = [await self.store.save(_result(), self.thread) for _ in range(2)]
        keep = await self.store.save(_result(), self.thread + "-other")

        n = await self.store.delete_for_thread(self.thread)
        self.assertEqual(n, 2)
        self.assertEqual(await self.store.list_for_thread(self.thread), [])
        for r in recs:
            self.assertFalse(Path(r.path).exists(), "PNG orphaned after thread purge")
        self.assertIsNotNone(await self.store.get(keep.visual_id))
        self.assertTrue(Path(keep.path).exists(), "another thread's image was deleted")

    async def test_delete_for_unknown_thread_is_zero(self) -> None:
        self.assertEqual(await self.store.delete_for_thread("no-such-thread"), 0)


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


class _SqliteCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmp.name) / "out"
        self.thread = "thread-parity"
        self.db = Path(self._tmp.name) / "state.db"

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()


class SqliteUnified(_VisualStoreContract, _SqliteCase):
    """The unified store on SQLite."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from yuyutsava.visuals.store_unified import sqlite_visual_store

        # Real factory: owns its own schema, no twin kept alive as plumbing.
        self.store = _OutDirBinding(sqlite_visual_store(self.db), self.out_dir)


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


class _PgCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from yuyutsava.storage.backend import StorageSettings
        from yuyutsava.storage.pg.pool import PgPool

        self._tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self._tmp.name) / "out"
        self.thread = f"thread-parity-{os.getpid()}"
        self.pool = PgPool(StorageSettings(backend="postgres", pg_dsn=_pg_dsn()))
        await self.pool.open()

    async def asyncTearDown(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM visual_artifacts WHERE thread_id LIKE %s",
                (self.thread.split("-other")[0] + "%",),
            )
        await self.pool.close()
        self._tmp.cleanup()


@unittest.skipUnless(PG_UP, f"no Postgres reachable at {_pg_dsn()}")
class PostgresUnified(_VisualStoreContract, _PgCase):
    """The SAME code as SqliteUnified, over a different dialect."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from yuyutsava.visuals.store_unified import pg_visual_store

        self.store = _OutDirBinding(pg_visual_store(self.pool), self.out_dir)


class _OutDirBinding:
    """Pins ``out_dir`` on ``save`` so images land in the test's temp directory.

    ``save`` takes ``out_dir`` per call; every other method does not. Binding it
    here keeps the shared contract methods free of backend/plumbing detail.
    """

    def __init__(self, store, out_dir: Path) -> None:
        self._s = store
        self._out = out_dir

    async def save(self, result, thread_id, **kw):
        return await self._s.save(result, thread_id, out_dir=self._out)

    def __getattr__(self, name):
        return getattr(self._s, name)


if __name__ == "__main__":
    print(f"Postgres at {_pg_dsn()}: {'UP' if PG_UP else 'DOWN (pg cases skip)'}\n")
    unittest.main(verbosity=2)
