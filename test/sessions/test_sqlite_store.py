"""Unit tests for SqliteSessionStore.

Run:  uv run python -m unittest test.sessions.test_sqlite_store -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from yuyutsava.sessions.sqlite_store import SqliteSessionStore, mint_thread_id
from yuyutsava.sessions.store import SessionNotFound


class SqliteSessionStoreTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "sessions.db"
        self.store = SqliteSessionStore(self.db, busy_timeout_ms=2000)
        self.ws = Path(self._tmp.name) / "ws"
        self.ws.mkdir()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_create_then_get_roundtrip(self) -> None:
        s = await self.store.create(workspace=self.ws, task="hello world")
        self.assertEqual(s.status, "running")
        self.assertEqual(s.message_count, 0)
        self.assertEqual(s.task_preview, "hello world")
        self.assertEqual(s.workspace, self.ws.resolve())
        loaded = await self.store.get(s.id)
        self.assertEqual(loaded.id, s.id)
        self.assertEqual(loaded.thread_id, s.thread_id)

    async def test_get_missing_raises_session_not_found(self) -> None:
        with self.assertRaises(SessionNotFound):
            await self.store.get("does-not-exist")

    async def test_touch_increments_message_count(self) -> None:
        s = await self.store.create(workspace=self.ws, task="t")
        await self.store.touch(s.id, message_delta=3)
        await self.store.touch(s.id, message_delta=2)
        loaded = await self.store.get(s.id)
        self.assertEqual(loaded.message_count, 5)
        self.assertGreaterEqual(loaded.updated_at, s.updated_at)

    async def test_touch_sets_memory_files_when_provided(self) -> None:
        s = await self.store.create(workspace=self.ws, task="t")
        await self.store.touch(s.id, message_delta=1, memory_files_count=7)
        loaded = await self.store.get(s.id)
        self.assertEqual(loaded.memory_files_count, 7)
        self.assertEqual(loaded.message_count, 1)

    async def test_update_status_validates(self) -> None:
        s = await self.store.create(workspace=self.ws, task="t")
        await self.store.update_status(s.id, "done")
        loaded = await self.store.get(s.id)
        self.assertEqual(loaded.status, "done")
        with self.assertRaises(ValueError):
            await self.store.update_status(s.id, "nope")

    async def test_list_orders_by_updated_at_desc_and_filters_workspace(self) -> None:
        other_ws = Path(self._tmp.name) / "other"
        other_ws.mkdir()

        a = await self.store.create(workspace=self.ws, task="A")
        b = await self.store.create(workspace=other_ws, task="B")
        c = await self.store.create(workspace=self.ws, task="C")
        # bump A so it becomes the most recent in self.ws
        await asyncio.sleep(0.01)
        await self.store.touch(a.id, message_delta=1)

        all_rows = await self.store.list()
        ids = [r.id for r in all_rows]
        self.assertIn(a.id, ids)
        self.assertIn(b.id, ids)
        self.assertIn(c.id, ids)

        ws_rows = await self.store.list(workspace=self.ws)
        self.assertEqual([r.id for r in ws_rows][:2], [a.id, c.id])
        for row in ws_rows:
            self.assertEqual(row.workspace, self.ws.resolve())

    async def test_delete_removes_row(self) -> None:
        s = await self.store.create(workspace=self.ws, task="t")
        await self.store.delete(s.id)
        with self.assertRaises(SessionNotFound):
            await self.store.get(s.id)

    async def test_concurrent_touches_do_not_lose_writes(self) -> None:
        s = await self.store.create(workspace=self.ws, task="t")
        # Fire 20 concurrent touches; the per-process lock + retry should
        # serialize them without dropping any. Total must equal sum of deltas.
        await asyncio.gather(*(self.store.touch(s.id, message_delta=1) for _ in range(20)))
        loaded = await self.store.get(s.id)
        self.assertEqual(loaded.message_count, 20)

    async def test_mint_thread_id_format(self) -> None:
        tid = mint_thread_id("cli")
        self.assertTrue(tid.startswith("cli-"))
        # role-<ts>-<uuid> → at least 3 dash-separated chunks (uuid has dashes too)
        self.assertGreaterEqual(tid.count("-"), 5)


if __name__ == "__main__":
    unittest.main()
