"""Unit tests for SqliteTranscriptStore + TranscriptRecorderMiddleware.

Run:  uv run python -m unittest test.context.test_transcript_store -v
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from yuyutsava.context import transcript_middleware as tmw
from yuyutsava.context.transcript_middleware import TranscriptRecorderMiddleware
from yuyutsava.context.transcript_store import SqliteTranscriptStore


class TranscriptStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteTranscriptStore(Path(self._tmp.name) / "state.db")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_put_and_list_ordered(self) -> None:
        msgs = [
            HumanMessage(content="hi", id="m1"),
            AIMessage(content="hello", id="m2"),
            ToolMessage(content="result", tool_call_id="c1", id="m3"),
        ]
        inserted = await self.store.put_messages("t1", msgs)
        self.assertEqual(inserted, 3)

        rows = await self.store.list_messages("t1")
        self.assertEqual([r.message_id for r in rows], ["m1", "m2", "m3"])
        self.assertEqual([r.type for r in rows], ["human", "ai", "tool"])
        # seq is monotonic in insertion order
        self.assertEqual([r.seq for r in rows], sorted(r.seq for r in rows))
        # content round-trips as the langchain typed record
        self.assertEqual(rows[0].content["data"]["content"], "hi")

    async def test_dedup_on_message_id(self) -> None:
        msgs = [HumanMessage(content="hi", id="m1"), AIMessage(content="yo", id="m2")]
        self.assertEqual(await self.store.put_messages("t1", msgs), 2)
        # Re-putting the same ids (plus one new) inserts only the new one.
        again = msgs + [AIMessage(content="more", id="m3")]
        self.assertEqual(await self.store.put_messages("t1", again), 1)
        rows = await self.store.list_messages("t1")
        self.assertEqual(len(rows), 3)

    async def test_messages_without_id_skipped(self) -> None:
        msgs = [HumanMessage(content="no id"), AIMessage(content="kept", id="m2")]
        self.assertEqual(await self.store.put_messages("t1", msgs), 1)
        rows = await self.store.list_messages("t1")
        self.assertEqual([r.message_id for r in rows], ["m2"])

    async def test_threads_isolated_and_after_seq(self) -> None:
        await self.store.put_messages("t1", [HumanMessage(content="a", id="a1")])
        await self.store.put_messages("t2", [HumanMessage(content="b", id="b1")])
        t1 = await self.store.list_messages("t1")
        self.assertEqual([r.message_id for r in t1], ["a1"])
        # after_seq paginates
        self.assertEqual(await self.store.list_messages("t1", after_seq=t1[0].seq), [])

    async def test_delete_older_than(self) -> None:
        await self.store.put_messages("t1", [HumanMessage(content="old", id="o1")])
        # Nothing deleted with a cutoff in the past.
        self.assertEqual(await self.store.delete_older_than(time.time() - 3600), 0)
        # Everything deleted with a cutoff in the future.
        self.assertEqual(await self.store.delete_older_than(time.time() + 3600), 1)
        self.assertEqual(await self.store.list_messages("t1"), [])


class TranscriptRecorderMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteTranscriptStore(Path(self._tmp.name) / "state.db")
        self.mw = TranscriptRecorderMiddleware(self.store)
        self._orig_tid = tmw._current_thread_id
        tmw._current_thread_id = lambda: "tA"  # type: ignore[assignment]

    async def asyncTearDown(self) -> None:
        tmw._current_thread_id = self._orig_tid  # type: ignore[assignment]
        self._tmp.cleanup()

    async def test_records_state_messages_and_dedups_across_hooks(self) -> None:
        state = {"messages": [HumanMessage(content="hi", id="m1")]}
        await self.mw.abefore_model(state, None)
        # AI message appended, then the after-model hook fires.
        state["messages"].append(AIMessage(content="hello", id="m2"))
        await self.mw.aafter_model(state, None)
        # Re-recording the same state (e.g. aafter_agent) writes nothing new.
        await self.mw.aafter_agent(state, None)

        rows = await self.store.list_messages("tA")
        self.assertEqual([r.message_id for r in rows], ["m1", "m2"])

    async def test_no_thread_id_is_noop(self) -> None:
        tmw._current_thread_id = lambda: ""  # type: ignore[assignment]
        await self.mw.abefore_model({"messages": [HumanMessage(content="x", id="z1")]}, None)
        self.assertEqual(await self.store.list_messages("tA"), [])


if __name__ == "__main__":
    unittest.main()
