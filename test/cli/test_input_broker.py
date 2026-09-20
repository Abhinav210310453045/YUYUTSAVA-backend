"""One typed line, and which of two destinations it reaches.

`R-SOL-01`: this was five methods and four fields inside `ChatDashboard`, so
the rules about which line answers a question and which starts a turn could
only be exercised by building a full-screen application. They are rules about a
queue and a future, and both bugs the comments record — a superseded ask
wiping the live future, and an answer being queued behind the turn waiting for
it — are reachable here directly.
"""

from __future__ import annotations

import asyncio
import unittest

from yuyutsava.cli.render.input_broker import DEFAULT_PROMPT, InputBroker


class ALineWithNoQuestionOpen(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.broker = InputBroker()

    async def test_it_is_queued_for_the_repl(self) -> None:
        self.broker.buffer.text = "do the thing"
        self.assertEqual(self.broker.submit(), "do the thing")
        self.assertEqual(await self.broker.read_line(), "do the thing")

    async def test_submitting_clears_the_line(self) -> None:
        self.broker.buffer.text = "typed"
        self.broker.submit()
        self.assertEqual(self.broker.buffer.text, "")

    async def test_an_empty_line_still_submits(self) -> None:
        """Enter on an empty line is how a user accepts a default."""
        self.assertEqual(self.broker.submit(), "")
        self.assertEqual(await self.broker.read_line(), "")

    async def test_lines_are_read_in_order(self) -> None:
        for text in ("one", "two", "three"):
            self.broker.buffer.text = text
            self.broker.submit()
        self.assertEqual([await self.broker.read_line() for _ in range(3)], ["one", "two", "three"])

    async def test_closing_wakes_the_reader_with_nothing(self) -> None:
        self.broker.close()
        self.assertIsNone(await self.broker.read_line())


class AQuestionOutranksATurn(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repaints = 0
        self.broker = InputBroker(invalidate=self._repaint)

    def _repaint(self) -> None:
        self.repaints += 1

    async def _answer(self, text: str) -> None:
        await asyncio.sleep(0)
        self.broker.buffer.text = text
        self.broker.submit()

    async def test_the_answer_does_not_become_a_turn(self) -> None:
        """Queueing it would park the answer behind the very turn that is
        blocked waiting for it."""
        pending = asyncio.create_task(self.broker.ask())
        await self._answer("y")
        self.assertEqual(await asyncio.wait_for(pending, 2), "y")
        self.broker.close()
        self.assertIsNone(await self.broker.read_line(), "nothing was queued")

    async def test_submit_reports_that_it_answered(self) -> None:
        pending = asyncio.create_task(self.broker.ask())
        await asyncio.sleep(0)
        self.broker.buffer.text = "y"
        self.assertIsNone(self.broker.submit(), "None means it answered a question")
        await asyncio.wait_for(pending, 2)

    async def test_the_prompt_changes_and_reverts(self) -> None:
        self.assertEqual(self.broker.prompt, DEFAULT_PROMPT)
        pending = asyncio.create_task(self.broker.ask("approve/reject> "))
        await asyncio.sleep(0)
        self.assertEqual(self.broker.prompt, "approve/reject> ")
        self.assertEqual(self.broker.prompt_text(), [("class:prompt", "approve/reject> ")])
        await self._answer("n")
        await asyncio.wait_for(pending, 2)
        self.assertEqual(self.broker.prompt, DEFAULT_PROMPT)

    async def test_the_pane_is_repainted_when_the_prompt_moves(self) -> None:
        pending = asyncio.create_task(self.broker.ask("q> "))
        await asyncio.sleep(0)
        self.assertGreaterEqual(self.repaints, 1, "the new prompt has to be drawn")
        await self._answer("y")
        await asyncio.wait_for(pending, 2)

    async def test_cancelling_refuses_rather_than_hanging(self) -> None:
        """A question that cannot be answered is never consent."""
        pending = asyncio.create_task(self.broker.ask())
        await asyncio.sleep(0)
        self.broker.cancel_ask()
        self.assertIsNone(await asyncio.wait_for(pending, 2))

    async def test_a_closed_broker_refuses_immediately(self) -> None:
        broker = InputBroker(is_closed=lambda: True)
        self.assertIsNone(await asyncio.wait_for(broker.ask(), 2))
        self.assertIsNone(await asyncio.wait_for(broker.read_line(), 2))

    async def test_it_knows_whether_one_is_open(self) -> None:
        self.assertFalse(self.broker.has_pending_ask())
        pending = asyncio.create_task(self.broker.ask())
        await asyncio.sleep(0)
        self.assertTrue(self.broker.has_pending_ask())
        self.broker.cancel_ask()
        await asyncio.wait_for(pending, 2)
        self.assertFalse(self.broker.has_pending_ask())


class TwoQuestionsAtOnce(unittest.IsolatedAsyncioTestCase):
    """Parallel tool calls can both ask. The older one is not answerable any
    more, and leaving it pending would hang its tool call forever."""

    def setUp(self) -> None:
        self.broker = InputBroker()

    async def test_the_older_question_is_refused_not_left_hanging(self) -> None:
        first = asyncio.create_task(self.broker.ask("first> "))
        await asyncio.sleep(0)
        second = asyncio.create_task(self.broker.ask("second> "))
        await asyncio.sleep(0)
        self.assertIsNone(await asyncio.wait_for(first, 2), "the superseded ask is a refusal")
        self.broker.buffer.text = "y"
        self.broker.submit()
        self.assertEqual(await asyncio.wait_for(second, 2), "y")

    async def test_the_superseded_ask_does_not_wipe_the_live_one(self) -> None:
        """The superseded ask resumes *after* its replacement has installed
        itself; clearing the slot blindly there wiped the live future, and the
        next line was queued as a turn instead of answering."""
        first = asyncio.create_task(self.broker.ask("first> "))
        await asyncio.sleep(0)
        second = asyncio.create_task(self.broker.ask("second> "))
        await asyncio.sleep(0)
        # Bounded: an unresolved supersede is the failure this test is for, and
        # it must show up as a failure rather than as a hung run.
        await asyncio.wait_for(first, 2)
        await asyncio.sleep(0)
        self.assertTrue(self.broker.has_pending_ask(), "the second question is still open")
        self.assertEqual(self.broker.prompt, "second> ")
        self.broker.buffer.text = "y"
        self.assertIsNone(self.broker.submit(), "the line must answer, not queue")
        self.assertEqual(await asyncio.wait_for(second, 2), "y")


if __name__ == "__main__":
    unittest.main()
