"""Teardown reaps a task without raising, and without hiding why it died."""

from __future__ import annotations

import asyncio
import unittest

from yuyutsava.aio.tasks import cancel_and_reap, reap


class Reap(unittest.IsolatedAsyncioTestCase):
    async def test_a_cancelled_task_is_reaped_silently(self) -> None:
        task = asyncio.create_task(asyncio.sleep(60))
        with self.assertNoLogs("yuyutsava.aio.tasks", level="DEBUG"):
            await cancel_and_reap(task, what="sleeper")
        self.assertTrue(task.cancelled())

    async def test_a_task_that_failed_is_logged_not_raised(self) -> None:
        async def boom() -> None:
            raise RuntimeError("it broke")

        task = asyncio.create_task(boom())
        await asyncio.sleep(0)
        with self.assertLogs("yuyutsava.aio.tasks", level="DEBUG") as logs:
            await reap(task, what="the worker")
        self.assertIn("the worker", logs.output[0])
        self.assertIn("it broke", "\n".join(logs.output))

    async def test_reap_waits_for_a_task_to_finish_on_its_own(self) -> None:
        done: list[bool] = []

        async def work() -> None:
            await asyncio.sleep(0.01)
            done.append(True)

        await reap(asyncio.create_task(work()), what="work")
        self.assertEqual(done, [True])

    async def test_none_is_nothing_to_reap(self) -> None:
        await reap(None, what="x")
        await cancel_and_reap(None, what="x")


if __name__ == "__main__":
    unittest.main()
