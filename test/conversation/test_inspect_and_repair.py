"""Reading a thread's shape before using it, and whose turn it is to finish.

`R-DRY-05`: `ConversationService` wrote the read-state/diagnose/repair sequence
twice — once before every turn and once after a turn came back empty. One helper,
`recovery.inspect_and_repair`, now serves both.

The parameter that sequence threads is `close_turn`, and it decides something a
user can see. `unclosed_turn` means the history ends on a tool result, so the
model never got to say what it concluded. Repairing that *closes* the turn by
writing a conclusion on the model's behalf — right before a fresh message, and
wrong when resuming an approval, because there the model is about to finish
that very turn itself. `close_turn=resume_value is None` is what encodes it.

Nothing covered the resume case, which is exactly the branch the helper made
shared: a mistake there would silently put words in the assistant's mouth on
every approved permission.
"""

from __future__ import annotations

import asyncio
import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from yuyutsava.conversation.recovery import inspect_and_repair


def _turn_left_open() -> list:
    """A history whose only problem is that the model never concluded.

    The tool ran and answered; the assistant then said nothing.
    """
    return [
        HumanMessage(content="read the file"),
        AIMessage(
            content="",
            tool_calls=[{"id": "tc-1", "name": "tr_read_file", "args": {"path": "/tmp/a"}}],
        ),
        ToolMessage(content="the contents", tool_call_id="tc-1", name="tr_read_file"),
    ]


class _Agent:
    def __init__(self, messages: list) -> None:
        self._messages = messages
        self.updates: list = []

    async def aget_state(self, _config):
        return type("S", (), {"values": {"messages": self._messages}})()

    async def aupdate_state(self, _config, update):
        self.updates.append(update)


def _inspect(messages: list, *, close_turn: bool):
    agent = _Agent(messages)
    found, repaired = asyncio.run(
        inspect_and_repair(agent, "t1", close_turn=close_turn, what="test")
    )
    return found, repaired, agent


class WhoseTurnItIsToFinish(unittest.TestCase):
    def test_before_a_new_message_the_open_turn_is_closed(self) -> None:
        """A fresh message must not be sent into a history the model would not
        continue from, so the abandoned turn is concluded first."""
        found, repaired, agent = _inspect(_turn_left_open(), close_turn=True)
        self.assertTrue(found.unclosed_turn)
        self.assertEqual(repaired, 1)
        self.assertEqual(len(agent.updates), 1)

    def test_when_resuming_the_model_finishes_its_own_turn(self) -> None:
        """The paused turn is about to continue. Writing its conclusion here
        would put words in the assistant's mouth on every approved
        permission."""
        found, repaired, agent = _inspect(_turn_left_open(), close_turn=False)
        self.assertTrue(found.unclosed_turn, "the finding is still reported")
        self.assertEqual(repaired, 0, "but nothing is written")
        self.assertEqual(agent.updates, [])


class WhatItReportsBack(unittest.TestCase):
    def test_a_clean_history_needs_nothing(self) -> None:
        found, repaired, agent = _inspect([HumanMessage(content="hi")], close_turn=True)
        self.assertFalse(found.any())
        self.assertEqual(repaired, 0)
        self.assertEqual(agent.updates, [])

    def test_an_unreadable_checkpoint_is_not_nothing_wrong(self) -> None:
        """The two are different answers and lead to different notices, which
        is why findings and count come back together."""

        class Unreadable:
            async def aget_state(self, _config):
                raise RuntimeError("checkpoint unreachable")

        found, repaired = asyncio.run(inspect_and_repair(Unreadable(), "t1", what="test"))
        self.assertIsNone(found, "None means we could not look")
        self.assertEqual(repaired, 0)

    def test_a_failure_does_not_raise_into_the_turn(self) -> None:
        """Both callers reach this with the user waiting."""

        class Exploding:
            async def aget_state(self, _config):
                raise KeyboardInterrupt("even this")

        with self.assertRaises(KeyboardInterrupt):
            asyncio.run(inspect_and_repair(Exploding(), "t1", what="test"))


if __name__ == "__main__":
    unittest.main()
