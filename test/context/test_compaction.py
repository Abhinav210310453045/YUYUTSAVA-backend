"""Unit tests for YuyutsavaCompactionMiddleware.

Covers the correctness requirements called out in the master plan:
- trigger math (no compaction under threshold),
- pinned task message survives every cycle,
- an AIMessage with (parallel) tool_calls is never separated from its
  ToolMessages by the cut,
- summaries persist to the ThreadSummaryStore,
- empty-history resume re-injects the latest persisted summary,
- after repeated cycles the model input still contains the session intent.

Run:  uv run python -m unittest test.context.test_compaction -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)

from yuyutsava.context.compaction import YuyutsavaCompactionMiddleware
from yuyutsava.context.config import ContextSettings
from yuyutsava.context.summary_store_unified import sqlite_summary_store

FAKE_SUMMARY = (
    "## SESSION INTENT\nOrganize the downloads folder.\n"
    "## DECISIONS MADE\nGroup by file type.\n"
    "## WORK COMPLETED\nMoved 12 PDFs.\n"
    "## ARTIFACTS\nart_TEST123\n"
    "## CURRENT STATE / NEXT STEP\nMove images next.\n"
    "## OPEN QUESTIONS\nNone\n"
)

TASK = HumanMessage(content="[event] fs.changed | organize my downloads folder")


def _mw(
    settings: ContextSettings,
    summary_store=None,
    memory_sink=None,
) -> YuyutsavaCompactionMiddleware:
    model = FakeListChatModel(responses=[FAKE_SUMMARY] * 10)
    return YuyutsavaCompactionMiddleware(
        model=model,
        settings=settings,
        summary_store=summary_store,
        memory_sink=memory_sink,
        role="test",
    )


def _turn(i: int, chars: int = 400) -> list:
    """One AI tool-call turn + its ToolMessage + a closing AI message."""
    return [
        AIMessage(
            content="",
            tool_calls=[{"name": "tr_read_file", "args": {}, "id": f"tc-{i}"}],
        ),
        ToolMessage(content="x" * chars, tool_call_id=f"tc-{i}", name="tr_read_file"),
        AIMessage(content=f"observed result {i} " + "y" * 50),
    ]


def _assert_tool_pairs_intact(testcase: unittest.TestCase, messages: list) -> None:
    """Every ToolMessage in the rewritten state must have its AIMessage."""
    ai_call_ids = {
        tc["id"]
        for m in messages
        if isinstance(m, AIMessage)
        for tc in (m.tool_calls or [])
    }
    for m in messages:
        if isinstance(m, ToolMessage):
            testcase.assertIn(
                m.tool_call_id, ai_call_ids,
                f"orphaned ToolMessage {m.tool_call_id} after compaction",
            )


class CompactionTriggerTests(unittest.IsolatedAsyncioTestCase):
    def test_trigger_math(self) -> None:
        s = ContextSettings(max_input_tokens=1000, compact_fraction=0.7)
        self.assertEqual(s.compact_trigger_tokens, 700)

    async def test_no_compaction_under_threshold(self) -> None:
        mw = _mw(ContextSettings(max_input_tokens=100_000, keep_messages=4))
        state = {"messages": [TASK, *(_turn(1))]}
        self.assertIsNone(await mw.abefore_model(state, None))

    async def test_compaction_pins_task_and_keeps_tail(self) -> None:
        mw = _mw(ContextSettings(max_input_tokens=600, compact_fraction=0.5, keep_messages=4))
        messages = [TASK]
        for i in range(8):
            messages.extend(_turn(i))
        state = {"messages": messages}

        update = await mw.abefore_model(state, None)
        self.assertIsNotNone(update)
        new = update["messages"]

        self.assertIsInstance(new[0], RemoveMessage)
        rebuilt = new[1:]
        # Pinned task message survives verbatim at the head.
        self.assertIs(rebuilt[0], TASK)
        # A summary message exists and carries the structured sections.
        summaries = [
            m for m in rebuilt
            if isinstance(m, HumanMessage) and "## SESSION INTENT" in str(m.content)
        ]
        self.assertEqual(len(summaries), 1)
        # The rewritten history is strictly smaller than the original.
        self.assertLess(len(rebuilt), len(messages))
        _assert_tool_pairs_intact(self, rebuilt)

    async def test_parallel_tool_calls_never_split(self) -> None:
        mw = _mw(ContextSettings(max_input_tokens=600, compact_fraction=0.5, keep_messages=3))
        messages = [TASK]
        for i in range(4):
            messages.extend(_turn(i))
        # Parallel tool-call turn positioned so a naive cut lands inside it.
        messages.append(AIMessage(
            content="",
            tool_calls=[
                {"name": "ws_a", "args": {}, "id": "par-1"},
                {"name": "ws_b", "args": {}, "id": "par-2"},
            ],
        ))
        messages.append(ToolMessage(content="a" * 300, tool_call_id="par-1", name="ws_a"))
        messages.append(ToolMessage(content="b" * 300, tool_call_id="par-2", name="ws_b"))
        messages.append(AIMessage(content="done with parallel work"))

        update = await mw.abefore_model({"messages": messages}, None)
        self.assertIsNotNone(update)
        _assert_tool_pairs_intact(self, update["messages"][1:])


class CompactionPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.summary_store = sqlite_summary_store(Path(self._tmp.name) / "state.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_summary_persisted_with_sections(self) -> None:
        mw = _mw(
            ContextSettings(max_input_tokens=600, compact_fraction=0.5, keep_messages=4),
            summary_store=self.summary_store,
        )
        messages = [TASK]
        for i in range(8):
            messages.extend(_turn(i))

        with patch(
            "yuyutsava.context.compaction._current_thread_id", return_value="t-persist"
        ):
            update = await mw.abefore_model({"messages": messages}, None)

        self.assertIsNotNone(update)
        row = await self.summary_store.latest("t-persist")
        self.assertIsNotNone(row)
        self.assertEqual(row.version, 1)
        for section in (
            "## SESSION INTENT", "## DECISIONS MADE", "## WORK COMPLETED",
            "## CURRENT STATE / NEXT STEP", "## OPEN QUESTIONS",
        ):
            self.assertIn(section, row.summary)

    async def test_summary_embedded_into_memory_sink(self) -> None:
        captured: list[dict] = []

        class _Sink:
            async def add(self, **kwargs):
                captured.append(kwargs)
                return "mem_x"

        mw = _mw(
            ContextSettings(max_input_tokens=600, compact_fraction=0.5, keep_messages=4),
            summary_store=self.summary_store,
            memory_sink=_Sink(),
        )
        messages = [TASK]
        for i in range(8):
            messages.extend(_turn(i))
        with patch(
            "yuyutsava.context.compaction._current_thread_id", return_value="t-mem"
        ):
            await mw.abefore_model({"messages": messages}, None)

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["kind"], "summary")
        self.assertIn("## SESSION INTENT", captured[0]["text"])

    async def test_resume_injects_latest_summary(self) -> None:
        await self.summary_store.put("t-resume", FAKE_SUMMARY)
        await self.summary_store.put("t-resume", FAKE_SUMMARY + "v2 marker")
        mw = _mw(ContextSettings(), summary_store=self.summary_store)

        with patch(
            "yuyutsava.context.compaction._current_thread_id", return_value="t-resume"
        ):
            update = await mw.abefore_agent({"messages": [TASK]}, None)

        self.assertIsNotNone(update)
        injected = update["messages"][0]
        self.assertIsInstance(injected, SystemMessage)
        self.assertIn("v2 marker", injected.content)

    async def test_resume_noop_with_history_or_no_summary(self) -> None:
        mw = _mw(ContextSettings(), summary_store=self.summary_store)
        with patch(
            "yuyutsava.context.compaction._current_thread_id", return_value="t-fresh"
        ):
            # Fresh thread, no stored summary.
            self.assertIsNone(await mw.abefore_agent({"messages": [TASK]}, None))
        await self.summary_store.put("t-live", FAKE_SUMMARY)
        with patch(
            "yuyutsava.context.compaction._current_thread_id", return_value="t-live"
        ):
            # Live history present — no injection.
            state = {"messages": [TASK, AIMessage(content="working")]}
            self.assertIsNone(await mw.abefore_agent(state, None))


class ThreeCycleContinuityTests(unittest.IsolatedAsyncioTestCase):
    """The headline requirement: after 3 compaction cycles the model input
    still contains the original task and a structured summary."""

    async def test_three_cycles_keep_session_intent(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        store = sqlite_summary_store(Path(self._tmp.name) / "state.db")
        mw = _mw(
            ContextSettings(max_input_tokens=600, compact_fraction=0.5, keep_messages=4),
            summary_store=store,
        )

        messages: list = [TASK]
        with patch(
            "yuyutsava.context.compaction._current_thread_id", return_value="t-3cycles"
        ):
            turn = 0
            for _cycle in range(3):
                # Grow the conversation until compaction fires.
                update = None
                while update is None:
                    messages.extend(_turn(turn))
                    turn += 1
                    update = await mw.abefore_model({"messages": messages}, None)
                messages = update["messages"][1:]  # drop RemoveMessage marker

                # After every cycle the model would still see the task + intent.
                self.assertIs(messages[0], TASK)
                self.assertTrue(
                    any("## SESSION INTENT" in str(m.content) for m in messages),
                    "summary with session intent missing after compaction",
                )
                _assert_tool_pairs_intact(self, messages)

        row = await store.latest("t-3cycles")
        self.assertEqual(row.version, 3)


if __name__ == "__main__":
    unittest.main()
