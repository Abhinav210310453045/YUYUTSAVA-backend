"""Unit tests for the task registry (SQLite backend).

Run:  uv run python -m unittest test.daemon.test_task_registry -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yuyutsava.daemon.task_registry import (
    SqliteTaskStore,
    TaskRegistry,
    mint_task_id,
)


class TaskRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.registry = TaskRegistry(
            SqliteTaskStore(Path(self._tmp.name) / "state.db")
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_create_get_roundtrip(self) -> None:
        tid = mint_task_id()
        self.assertTrue(tid.startswith("tsk_"))
        await self.registry.create(task_id=tid, origin="api", instruction="do x")

        rec = await self.registry.get(tid)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.status, "queued")
        self.assertEqual(rec.origin, "api")
        self.assertEqual(rec.instruction, "do x")
        self.assertIsNone(rec.thread_id)
        self.assertIsNone(rec.started_ts)

    async def test_lifecycle_transitions(self) -> None:
        tid = mint_task_id()
        await self.registry.create(task_id=tid, origin="api", instruction="do x")

        await self.registry.mark_running(tid, thread_id="orch-1-abc")
        rec = await self.registry.get(tid)
        self.assertEqual(rec.status, "running")
        self.assertEqual(rec.thread_id, "orch-1-abc")
        self.assertIsNotNone(rec.started_ts)

        await self.registry.mark_done(tid, result_summary="moved 3 files")
        rec = await self.registry.get(tid)
        self.assertEqual(rec.status, "done")
        self.assertEqual(rec.result_summary, "moved 3 files")
        self.assertIsNotNone(rec.finished_ts)

    async def test_mark_failed_records_error(self) -> None:
        tid = mint_task_id()
        await self.registry.create(task_id=tid, origin="api", instruction="do x")
        await self.registry.mark_failed(tid, error="boom " * 200)
        rec = await self.registry.get(tid)
        self.assertEqual(rec.status, "failed")
        self.assertLessEqual(len(rec.error), 500)

    async def test_cancel_flow(self) -> None:
        tid = mint_task_id()
        await self.registry.create(task_id=tid, origin="api", instruction="do x")
        await self.registry.mark_running(tid, thread_id="t")

        self.assertFalse(self.registry.cancel_requested(tid))
        self.assertEqual(await self.registry.request_cancel(tid), "ok")
        self.assertTrue(self.registry.cancel_requested(tid))

        await self.registry.mark_cancelled(tid, note="cancelled by user")
        rec = await self.registry.get(tid)
        self.assertEqual(rec.status, "cancelled")
        # Flag cleared once terminal.
        self.assertFalse(self.registry.cancel_requested(tid))

    async def test_cancel_unknown_and_terminal(self) -> None:
        self.assertEqual(await self.registry.request_cancel("tsk_nope"), "not_found")

        tid = mint_task_id()
        await self.registry.create(task_id=tid, origin="api", instruction="x")
        await self.registry.mark_done(tid, result_summary="done")
        self.assertEqual(await self.registry.request_cancel(tid), "conflict")

    async def test_list_pagination_and_status_filter(self) -> None:
        ids = []
        for i in range(5):
            tid = mint_task_id()
            ids.append(tid)
            await self.registry.create(task_id=tid, origin="api", instruction=f"t{i}")
        await self.registry.mark_done(ids[0], result_summary="ok")

        # Newest-first, cursor walk in pages of 2.
        page1, cur1 = await self.registry.list(limit=2)
        self.assertEqual([r.task_id for r in page1], [ids[4], ids[3]])
        self.assertEqual(cur1, ids[3])

        page2, cur2 = await self.registry.list(limit=2, cursor=cur1)
        self.assertEqual([r.task_id for r in page2], [ids[2], ids[1]])

        page3, cur3 = await self.registry.list(limit=2, cursor=cur2)
        self.assertEqual([r.task_id for r in page3], [ids[0]])
        self.assertIsNone(cur3)

        done, _ = await self.registry.list(status="done")
        self.assertEqual([r.task_id for r in done], [ids[0]])

    async def test_update_rejects_unknown_columns(self) -> None:
        tid = mint_task_id()
        await self.registry.create(task_id=tid, origin="api", instruction="x")
        store = self.registry._store
        with self.assertRaises(ValueError):
            await store.update(tid, {"instruction; DROP TABLE tasks": "x"})


if __name__ == "__main__":
    unittest.main()
