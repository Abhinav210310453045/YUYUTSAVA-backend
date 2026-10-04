"""After a switch, compaction, the meter and usage follow the model the turn runs on.

An agent is built with one model; its conversation may run a turn on another
(``llm/switch``), which hands each hook an ``active`` lookup. Without it a switch
from a 1M-token model to a 200k one would compact at 700k — after the overflow —
and the ledger would bill the wrong model.
"""

from __future__ import annotations

import asyncio
import unittest

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from yuyutsava.context.compaction import YuyutsavaCompactionMiddleware
from yuyutsava.context.config import ContextSettings
from yuyutsava.context.meter import (
    ContextMeterPolicy,
    bus,
    reset_sizing_caches,
    reset_thread_state,
)
from yuyutsava.llm.handle import ModelHandle
from yuyutsava.policy.builtin.usage import UsagePolicy
from yuyutsava.policy.types import ModelCall, Turn, Usage


def _handle(name: str, window: int, provider: str = "groq") -> ModelHandle:
    return ModelHandle(
        model=FakeListChatModel(responses=["x"]),
        name=name,
        provider=provider,
        context_window=window,
    )


SMALL = _handle("llama-3.1-8b-instant", 600)


class TheBudget(unittest.TestCase):
    def test_follows_the_window_in_use(self) -> None:
        settings = ContextSettings(max_input_tokens=1_000_000, compact_fraction=0.7)
        self.assertEqual(settings.trigger_for(200_000), 140_000)
        self.assertEqual(settings.trigger_for(None), 700_000)

    def test_a_hand_set_budget_holds_through_a_switch(self) -> None:
        settings = ContextSettings(max_input_tokens=50_000, fixed_window=True)
        self.assertEqual(settings.budget_for(1_000_000), 50_000)


def _history() -> list:
    messages: list = [HumanMessage("organize my downloads")]
    for i in range(8):
        messages += [
            AIMessage("", tool_calls=[{"name": "tr_ls", "args": {}, "id": f"c{i}"}]),
            ToolMessage("x" * 400, tool_call_id=f"c{i}"),
            AIMessage(f"done {i}"),
        ]
    return messages


def _compaction(settings: ContextSettings, active=None) -> YuyutsavaCompactionMiddleware:
    return YuyutsavaCompactionMiddleware(
        model=FakeListChatModel(responses=["## SESSION INTENT\nsummary"] * 4),
        settings=settings,
        role="test",
        active=active,
    )


class Compaction(unittest.TestCase):
    def test_compacts_under_the_smaller_window_it_switched_to(self) -> None:
        settings = ContextSettings(max_input_tokens=100_000, compact_fraction=0.5, keep_messages=4)
        state = {"messages": _history()}
        self.assertIsNone(asyncio.run(_compaction(settings).abefore_model(state, None)))
        switched = _compaction(settings, active=lambda _thread: SMALL)
        self.assertIsNotNone(asyncio.run(switched.abefore_model(state, None)))

    def test_a_hand_set_budget_is_not_overridden(self) -> None:
        settings = ContextSettings(
            max_input_tokens=100_000, fixed_window=True, compact_fraction=0.5, keep_messages=4
        )
        switched = _compaction(settings, active=lambda _thread: SMALL)
        self.assertIsNone(asyncio.run(switched.abefore_model({"messages": _history()}, None)))


class Meter(unittest.TestCase):
    def setUp(self) -> None:
        self.seen: list = []
        bus().subscribe("", self.seen.append)

    def tearDown(self) -> None:
        bus().clear()
        reset_thread_state()
        reset_sizing_caches()

    def _publish(self, active) -> object:
        policy = ContextMeterPolicy(
            settings=ContextSettings(max_input_tokens=1_000_000),
            role="cli",
            model_name="gemini-3.5-flash",
            active=active,
        )
        asyncio.run(policy.before_model(Turn(messages=(SystemMessage("s"),), thread_id="t")))
        asyncio.run(policy.revise_model_call(ModelCall(system_texts=("prompt",))))
        return self.seen[-1]

    def test_reports_the_model_in_use_and_its_window(self) -> None:
        snapshot = self._publish(lambda _thread: _handle("claude-haiku-4-5", 200_000))
        self.assertEqual(snapshot.model, "claude-haiku-4-5")
        self.assertEqual(snapshot.max_input_tokens, 200_000)
        self.assertEqual(snapshot.compact_trigger_tokens, 140_000)

    def test_without_a_switch_reports_the_built_model(self) -> None:
        snapshot = self._publish(lambda _thread: None)
        self.assertEqual(snapshot.model, "gemini-3.5-flash")
        self.assertEqual(snapshot.max_input_tokens, 1_000_000)


class _Rows:
    def __init__(self) -> None:
        self.rows: list = []

    async def add(self, row) -> None:
        self.rows.append(row)


class UsageRows(unittest.TestCase):
    def _row(self, active):
        store = _Rows()
        policy = UsagePolicy(
            store, model_name="gemini-3.5-flash", provider="vertex", prices={}, active=active
        )
        turn = Turn(thread_id="t", usage=Usage(input_tokens=10, output_tokens=2))
        asyncio.run(policy.after_model(turn))
        return store.rows[0]

    def test_records_the_model_the_turn_ran_on_and_its_provider(self) -> None:
        row = self._row(lambda _thread: SMALL)
        self.assertEqual((row.model, row.provider), ("llama-3.1-8b-instant", "groq"))

    def test_without_a_switch_records_the_built_model(self) -> None:
        row = self._row(lambda _thread: None)
        self.assertEqual((row.model, row.provider), ("gemini-3.5-flash", "vertex"))


if __name__ == "__main__":
    unittest.main()
