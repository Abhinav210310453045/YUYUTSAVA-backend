"""The empty-turn retry, on a real agent graph through the real stream driver.

`R-BUG-24`. Every earlier test of the retry used a fake ``drive``, so none
noticed that the real one built ``HumanMessage(content=None)`` and crashed — the
retry never ran in the REPL, the app or voice. Nor would it have done anything:
a finished run has no next node, so re-entering it with no input schedules
nothing. Here a real deepagents graph, with a scripted model, is driven exactly
the way ``ConversationService._drive`` drives it.

The user's Retry (``retry_last_turn``) is the same re-run, started on request:
it is exercised here on the same graphs, after a turn that gave up and after
one that died before the model answered.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from yuyutsava.conversation.recovery import (
    NEW_CHAT,
    RETRY,
    recover_empty_turn,
    retry_last_turn,
)
from yuyutsava.conversation.repair import is_internal
from yuyutsava.conversation.service import ConversationService
from yuyutsava.core.streaming import astream_agent_iter

#: A scripted reply with this text raises instead: a provider failing mid-turn.
_BUSY = "(provider busy)"


class _Scripted(FakeMessagesListChatModel):
    """Replies in order; binding tools is a no-op."""

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        result = super()._generate(messages, stop, run_manager, **kwargs)
        if result.generations[0].message.content == _BUSY:
            raise RuntimeError("503 service unavailable")
        return result


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

    async def retry() -> tuple[str, list]:
        events: list = []
        final, _ = await retry_last_turn(agent, thread, events.append, drive)
        return final, events

    async def history() -> list:
        state = await agent.aget_state({"configurable": {"thread_id": thread}})
        return list(state.values.get("messages", []))

    return turn, history, retry


def _texts(messages: list) -> list[str]:
    return [str(m.content) for m in messages if isinstance(m, AIMessage) and m.content]


class TheRetryLadderOnARealGraph(unittest.TestCase):
    def test_an_empty_reply_is_re_run_and_answered_without_a_new_message(self) -> None:
        turn, history, _ = _conversation(
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
        turn, history, _ = _conversation(
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
        turn, history, _ = _conversation([_call()] + [AIMessage(content="")] * 3)

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

    def test_the_last_notice_offers_retry_and_a_new_chat_and_names_no_command(self) -> None:
        turn, _, _ = _conversation([_call()] + [AIMessage(content="")] * 3)
        _, events = asyncio.run(turn("what is on sunday?"))
        gave_up = events[-1].data
        self.assertEqual(gave_up["code"], "empty_turn")
        self.assertEqual([a["id"] for a in gave_up["actions"]], [RETRY.id, NEW_CHAT.id])
        self.assertNotIn("/", gave_up["text"], "the app has no slash commands")


class RetryingOnRequest(unittest.TestCase):
    """The user's Retry: the model runs again on the history, nothing is sent."""

    def test_a_turn_that_gave_up_is_answered_on_retry_and_its_closing_note_is_gone(self) -> None:
        turn, history, retry = _conversation(
            [_call()] + [AIMessage(content="")] * 3 + [AIMessage(content="Sunday: a 5k.")]
        )

        async def go():
            await turn("what is on sunday?")
            final, _ = await retry()
            return final, await history()

        final, msgs = asyncio.run(go())
        self.assertEqual(final, "Sunday: a 5k.")
        self.assertEqual(len([m for m in msgs if isinstance(m, HumanMessage)]), 1)
        self.assertFalse(
            [m for m in msgs if is_internal(getattr(m, "additional_kwargs", {}))],
            "the '[no reply]' note is removed before the model runs again",
        )
        self.assertEqual(msgs[-1].content, "Sunday: a 5k.")

    def test_a_turn_that_died_before_the_model_answered_is_retried(self) -> None:
        turn, history, retry = _conversation(
            [AIMessage(content=_BUSY), AIMessage(content="Sunday: a 5k.")]
        )

        async def go():
            with self.assertRaises(RuntimeError):
                await turn("what is on sunday?")
            final, _ = await retry()
            return final, await history()

        final, msgs = asyncio.run(go())
        self.assertEqual(final, "Sunday: a 5k.")
        self.assertEqual([type(m).__name__ for m in msgs], ["HumanMessage", "AIMessage"])

    def test_an_answered_turn_has_nothing_to_retry(self) -> None:
        turn, history, retry = _conversation(
            [AIMessage(content="Sunday: a 5k."), AIMessage(content="a second answer")]
        )

        async def go():
            await turn("what is on sunday?")
            final, events = await retry()
            return final, events, await history()

        final, events, msgs = asyncio.run(go())
        self.assertEqual(final, "")
        self.assertEqual(events[-1].data["level"], "info")
        self.assertIn("Nothing to retry", events[-1].data["text"])
        self.assertEqual(_texts(msgs), ["Sunday: a 5k."], "the model was not called again")

    def test_a_retry_that_fails_raises_its_own_error_not_an_empty_notice(self) -> None:
        turn, history, retry = _conversation(
            [AIMessage(content=_BUSY), AIMessage(content=_BUSY), AIMessage(content="ok")]
        )

        async def go():
            with self.assertRaises(RuntimeError):
                await turn("what is on sunday?")
            with self.assertRaisesRegex(RuntimeError, "503"):
                await retry()
            return await history()

        msgs = asyncio.run(go())
        self.assertEqual([type(m).__name__ for m in msgs], ["HumanMessage"], "nothing written")

    def test_empty_again_on_retry_ends_in_the_same_notice(self) -> None:
        turn, _, retry = _conversation([_call()] + [AIMessage(content="")] * 6)

        async def go():
            await turn("what is on sunday?")
            return await retry()

        final, events = asyncio.run(go())
        self.assertEqual(final, "")
        self.assertEqual(events[-1].data.get("code"), "empty_turn")


class TheServiceRetries(unittest.TestCase):
    """``run_turn(retry=True)`` — the entry point the REPL's /retry and the app's button use."""

    def test_retry_sends_nothing_new_and_answers(self) -> None:
        agent = create_deep_agent(
            model=_Scripted(
                responses=[_call()]
                + [AIMessage(content="")] * 3
                + [AIMessage(content="Sunday: a 5k.")]
            ),
            tools=[read_plan],
            checkpointer=InMemorySaver(),
        )
        convo = ConversationService(
            store=None,
            session=SimpleNamespace(id="s1", thread_id="t-svc"),
            workspace=Path("."),
            bundle=SimpleNamespace(agent=agent),
            bookkeep=False,
        )
        events: list = []

        async def go():
            first = await convo.run_turn("what is on sunday?", on_event=events.append)
            again = await convo.run_turn("ignored", on_event=events.append, retry=True)
            state = await agent.aget_state({"configurable": {"thread_id": "t-svc"}})
            return first, again, state.values["messages"]

        first, again, msgs = asyncio.run(go())
        self.assertEqual((first, again), ("", "Sunday: a 5k."))
        humans = [m.content for m in msgs if isinstance(m, HumanMessage)]
        self.assertEqual(humans, ["what is on sunday?"], "the retry's text is never sent")


if __name__ == "__main__":
    unittest.main()
