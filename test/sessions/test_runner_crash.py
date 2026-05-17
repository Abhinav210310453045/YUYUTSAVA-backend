"""Crash + resume tests for the session runner.

Verifies the load-bearing claim of the architecture: even an ungraceful exit
(no ``finally:``, simulated kill mid-stream) leaves a row in the store that
:func:`yuyutsava.sessions.runner.run_session` can pick up via ``--resume``.

Run:  uv run python -m unittest test.sessions.test_runner_crash -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yuyutsava.sessions.sqlite_store import SqliteSessionStore


class _FakeAgent:
    """Stand-in for a CompiledStateGraph that records the thread_id given.

    We never actually invoke this in these tests — the runner is tested via
    the store directly for the crash-recovery behavior. ``run_session`` would
    pass the agent to ``astream_agent``, which needs a real LLM, so we test
    the bookkeeping invariants on the store instead.
    """


class CrashRecoveryTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "sessions.db"
        self.store = SqliteSessionStore(self.db, busy_timeout_ms=2000)
        self.ws = Path(self._tmp.name) / "ws"
        self.ws.mkdir()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_row_exists_before_first_step(self) -> None:
        # The runner creates the row BEFORE the first LLM call. If a SIGKILL
        # hits between create and the first stream tick, the row is still
        # there with status='running' — fully recoverable.
        s = await self.store.create(workspace=self.ws, task="indexing repo")
        loaded = await self.store.get(s.id)
        self.assertEqual(loaded.status, "running")
        self.assertEqual(loaded.message_count, 0)
        self.assertEqual(loaded.task_preview, "indexing repo")

    async def test_simulated_crash_leaves_row_recoverable(self) -> None:
        # Simulate the runner's behavior on an abnormal exit:
        #   1. create row
        #   2. a few ticks land
        #   3. no graceful update_status call (process killed)
        s = await self.store.create(workspace=self.ws, task="long task")
        for _ in range(5):
            await self.store.touch(s.id, message_delta=1)
        # No update_status("crashed") — that's exactly what a hard kill misses.
        # A new process opens the store and finds the row:
        store2 = SqliteSessionStore(self.db, busy_timeout_ms=2000)
        loaded = await store2.get(s.id)
        self.assertEqual(loaded.status, "running")  # stuck-running is the tell
        self.assertEqual(loaded.message_count, 5)

    async def test_continue_picks_most_recent_for_workspace(self) -> None:
        import asyncio as _aio

        older = await self.store.create(workspace=self.ws, task="old")
        await _aio.sleep(0.01)
        newer = await self.store.create(workspace=self.ws, task="new")
        rows = await self.store.list(workspace=self.ws, limit=1)
        self.assertEqual(rows[0].id, newer.id)
        self.assertNotEqual(rows[0].id, older.id)

    async def test_resume_marks_row_running_again_after_crashed(self) -> None:
        s = await self.store.create(workspace=self.ws, task="t")
        await self.store.update_status(s.id, "crashed")
        # Runner-style: explicit resume bumps the row back to running.
        await self.store.update_status(s.id, "running")
        loaded = await self.store.get(s.id)
        self.assertEqual(loaded.status, "running")


if __name__ == "__main__":
    unittest.main()
