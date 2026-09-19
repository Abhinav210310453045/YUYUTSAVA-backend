"""`WaiterMap` — an answer may arrive before the thing that waits for it.

`R-BUG-09`: a permission prompt is visible to clients before the future that
carries its answer is registered, so a fast surface can answer into thin air.
These pin the two halves of the contract: an early answer is kept and delivered,
and an answer for a prompt that has already finished is still refused.
"""

from __future__ import annotations

import asyncio
import unittest

from yuyutsava.aio import WaiterMap


class HoldingAnEarlyAnswer(unittest.IsolatedAsyncioTestCase):
    async def test_a_held_answer_is_delivered_when_the_waiter_registers(self) -> None:
        waiters: WaiterMap = WaiterMap()
        self.assertTrue(waiters.hold("ask-1", "approve"))

        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        waiters["ask-1"] = fut

        # Delivered during registration, so the waiter never blocks at all.
        self.assertTrue(fut.done())
        self.assertEqual(await fut, "approve")

    async def test_a_waiter_with_nothing_held_still_blocks(self) -> None:
        waiters: WaiterMap = WaiterMap()
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        waiters["ask-2"] = fut
        self.assertFalse(fut.done())

    async def test_holding_is_refused_once_a_waiter_has_been_and_gone(self) -> None:
        """A finished prompt must still conflict, not swallow a late answer."""
        waiters: WaiterMap = WaiterMap()
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        waiters["ask-3"] = fut
        fut.set_result("approve")
        del waiters["ask-3"]

        self.assertFalse(waiters.hold("ask-3", "reject"))
        self.assertEqual(waiters.held_ids(), ())

    async def test_a_held_answer_expires(self) -> None:
        waiters: WaiterMap = WaiterMap(ttl=-1.0)  # already stale when stored
        waiters.hold("ask-4", "approve")
        self.assertEqual(waiters.held_ids(), ())

        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        waiters["ask-4"] = fut
        self.assertFalse(fut.done())

    async def test_held_answers_are_bounded(self) -> None:
        waiters: WaiterMap = WaiterMap(max_held=2)
        for i in range(5):
            waiters.hold(f"ask-{i}", "approve")
        self.assertLessEqual(len(waiters.held_ids()), 2)

    async def test_it_is_still_a_dict(self) -> None:
        """Every existing reader treats this as a plain mapping."""
        waiters: WaiterMap = WaiterMap()
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        waiters["ask-5"] = fut
        self.assertIn("ask-5", waiters)
        self.assertIs(waiters.get("ask-5"), fut)
        self.assertEqual([k for k, _ in waiters.items()], ["ask-5"])
        self.assertIs(waiters.pop("ask-5"), fut)
        self.assertEqual(len(waiters), 0)


if __name__ == "__main__":
    unittest.main()
