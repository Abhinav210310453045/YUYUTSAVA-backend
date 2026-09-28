"""The empty-turn retry, on a real agent graph through the real stream driver.

`R-BUG-24`. Every earlier test of the retry used a fake ``drive``, so none
noticed that the real one built ``HumanMessage(content=None)`` and crashed — the
retry never ran in the REPL, the app or voice. Nor would it have done anything:
a finished run has no next node, so re-entering it with no input schedules
nothing. Here a real deepagents graph, with a scripted model, is driven exactly
the way ``ConversationService._drive`` drives it.
"""

from __future__ import annotations

import asyncio
import unittest

from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from yuyutsava.conversation.recovery import recover_empty_turn
from yuyutsava.conversation.repair import is_internal
from yuyutsava.core.streaming import astream_agent_iter


class _Scripted(FakeMessagesListChatModel):
    """Replies in order; binding tools is a no-op."""

    def bind_tools(self, tools, **kwargs):
        return self


@tool
def read_plan(day: str) -> str:
    """Read the plan for a day."""
    return f"{day}: run 5k, then stretch"


def _call() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": "c1", "name": "read_plan", "args": {"day": "sun"}, "type": "tool_call"}],
    )


def _conversation(replies: list[AIMessage]):
    agent = create_deep_agent(
        model=_Scripted(responses=replies), tools=[read_plan], checkpointer=InMemorySaver()
    )
    thread = "t-real"

    async def drive(task, resume):
        final, steps = "", 0
        async for ev in astream_agent_iter(agent, task, thread_id=thread, resume_value=resume):
            if ev.kind == "final":
                final = ev.data.get("text", "") or final
            else:
                steps += 1
        return final, steps

    async def turn(text: str) -> tuple[str, list]:
        events: list = []
        final, _ = await drive(text, None)
        if not final:
            final, _ = await recover_empty_turn(agent, thread, events.append, drive)
        return final, events

    async def history() -> list:
        state = await agent.aget_state({"configurable": {"thread_id": thread}})
        return list(state.values.get("messages", []))

    return turn, history


def _texts(messages: list) -> list[str]:
    return [str(m.content) for m in messages if isinstance(m, AIMessage) and m.content]


class TheRetryLadderOnARealGraph(unittest.TestCase):
    def test_an_empty_reply_is_re_run_and_answered_without_a_new_message(self) -> None:
        turn, history = _conversation(
            [_call(), AIMessage(content=""), AIMessage(content="Sunday: a 5k.")]
        )

        async def go():
            final, events = await turn("what is on sunday?")
            return final, events, await history()

        final, events, msgs = asyncio.run(go())
        self.assertEqual(final, "Sunday: a 5k.")
        self.assertEqual([e.data["level"] for e in events], ["warning"])
        humans = [m for m in msgs if isinstance(m, HumanMessage)]
        self.assertEqual(len(humans), 1, "the re-run adds no message")
        self.assertFalse(
            [m for m in msgs if isinstance(m, AIMessage) and not m.content and not m.tool_calls],
            "the empty reply is gone",
        )

    def test_a_second_empty_reply_gets_a_hidden_nudge(self) -> None:
        turn, history = _conversation(
            [
                _call(),
                AIMessage(content=""),
                AIMessage(content=""),
                AIMessage(content="Sunday: a 5k."),
            ]
        )

        async def go():
            final, _ = await turn("what is on sunday?")
            return final, await history()

        final, msgs = asyncio.run(go())
        self.assertEqual(final, "Sunday: a 5k.")
        nudges = [
            m for m in msgs if isinstance(m, HumanMessage) and is_internal(m.additional_kwargs)
        ]
        self.assertEqual(len(nudges), 1)

    def test_empty_every_time_closes_the_turn_honestly_and_says_so(self) -> None:
        turn, history = _conversation([_call()] + [AIMessage(content="")] * 3)

        async def go():
            final, events = await turn("what is on sunday?")
            return final, events, await history()

        final, events, msgs = asyncio.run(go())
        self.assertEqual(final, "")
        self.assertEqual(events[-1].data["level"], "error")
        closer = msgs[-1]
        self.assertIsInstance(closer, AIMessage)
        self.assertTrue(is_internal(closer.additional_kwargs))
        self.assertIn("[no reply]", closer.content)
        self.assertNotIn("user interrupted", " ".join(_texts(msgs)), "no false history")


if __name__ == "__main__":
    unittest.main()
