"""Unit tests for the inter-agent completion loop (Part A).

Covers the pieces that let the master agent learn — on its own — that a
background subagent finished:

  * ``LaunchIndex`` (task_id → launching conversation/channel) + its parser
  * ``AsyncTaskMirror`` completion surfacing (``list_recent_completed`` +
    ``render_block`` showing finished-but-unacked tasks, hidden after notify)
  * ``AsyncTaskHealthWatcher`` calling its ``completion_sink`` exactly once when a
    task reaches a terminal status, with parent/origin populated from the index.

No network, no LLM: the watcher is driven against a fake LangGraph client.

Runnable as a script (``python test/async_subagents/test_completion_loop.py``)
or via pytest.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from yuyutsava.async_subagents.launch_index import LaunchIndex, parse_async_task_id
from yuyutsava.async_subagents.mirror import AsyncTaskMirror, MirroredTask
from yuyutsava.async_subagents.watcher import AsyncTaskHealthWatcher


# ---------------------------------------------------------------------------
# LaunchIndex
# ---------------------------------------------------------------------------


class LaunchIndexTests(unittest.TestCase):
    def test_parse_task_id(self):
        self.assertEqual(
            parse_async_task_id("Launched async subagent. task_id: abc-123."),
            "abc-123",
        )
        self.assertEqual(parse_async_task_id("task_id: xyz"), "xyz")
        self.assertIsNone(parse_async_task_id("no id here"))
        self.assertIsNone(parse_async_task_id(""))
        self.assertIsNone(parse_async_task_id(None))

    def test_record_and_get(self):
        idx = LaunchIndex()
        idx.record("t1", "T-parent", "cli")
        rec = idx.get("t1")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.parent_thread_id, "T-parent")
        self.assertEqual(rec.origin, "cli")
        self.assertIsNone(idx.get("missing"))

    def test_record_ignores_blanks(self):
        idx = LaunchIndex()
        idx.record("", "T", "cli")
        idx.record("t", "", "cli")
        self.assertIsNone(idx.get(""))
        self.assertIsNone(idx.get("t"))

    def test_lru_bound(self):
        idx = LaunchIndex(max_entries=3)
        for i in range(5):
            idx.record(f"t{i}", f"T{i}")
        self.assertIsNone(idx.get("t0"))  # evicted
        self.assertIsNone(idx.get("t1"))
        self.assertIsNotNone(idx.get("t4"))


# ---------------------------------------------------------------------------
# Mirror completion surfacing
# ---------------------------------------------------------------------------


class MirrorCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def _mk(self, m: AsyncTaskMirror, tid: str, status: str = "running") -> None:
        now = time.time()
        await m.upsert(MirroredTask(
            task_id=tid, agent_name="file-organizer", graph_id="file-organizer",
            instruction="organize", status=status, started_at=now, last_update_at=now,
        ))

    async def test_render_surfaces_completion_then_hides_after_notify(self):
        m = AsyncTaskMirror()
        await self._mk(m, "task-aaaa")
        await m.set_status("task-aaaa", "success", summary="moved 8 files")
        block = m.render_block()
        self.assertIn("finished", block)
        self.assertIn("moved 8 files", block)
        # Once acknowledged, it must not be re-reported.
        await m.mark_notified("task-aaaa")
        self.assertEqual(m.render_block(), "")

    async def test_list_recent_completed_filters_notified(self):
        m = AsyncTaskMirror()
        await self._mk(m, "a")
        await self._mk(m, "b")
        await m.set_status("a", "success", summary="x")
        await m.set_status("b", "error", error="boom")
        self.assertEqual(len(m.list_recent_completed()), 2)
        await m.mark_notified("a")
        remaining = m.list_recent_completed()
        self.assertEqual([t.task_id for t in remaining], ["b"])
        self.assertEqual(len(m.list_recent_completed(unnotified_only=False)), 2)


# ---------------------------------------------------------------------------
# Watcher completion sink (fake LangGraph client)
# ---------------------------------------------------------------------------


class _FakeRuns:
    def __init__(self, parent: "_FakeClient") -> None:
        self._p = parent

    async def list(self, thread_id: str, limit: int = 1):
        return [{"run_id": "r1", "status": self._p.run_status,
                 "assistant_id": "file-organizer"}]

    async def cancel(self, thread_id: str, run_id: str):
        return None

    async def create(self, **kwargs):
        self._p.created.append(kwargs)
        return {"run_id": "r2"}


class _FakeThreads:
    def __init__(self, parent: "_FakeClient") -> None:
        self._p = parent

    async def search(self, limit: int = 50):
        return [{"thread_id": tid} for tid in self._p.thread_ids]

    async def get(self, thread_id: str):
        return {
            "thread_id": thread_id,
            "status": self._p.thread_status,
            "values": {"messages": [
                {"role": "assistant", "content": self._p.final_text},
            ]},
            "metadata": {},
        }


class _FakeClient:
    def __init__(self) -> None:
        self.threads = _FakeThreads(self)
        self.runs = _FakeRuns(self)
        self.thread_ids = ["th-1"]
        self.run_status = "running"
        self.thread_status = ""
        self.final_text = "moved 8 files: Documents/invoice.pdf, report.pdf"
        self.created: list = []


class WatcherCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def _build(self):
        mirror = AsyncTaskMirror()
        idx = LaunchIndex()
        idx.record("th-1", "T-parent", "cli")
        calls: list[tuple] = []

        async def sink(task: MirroredTask, ok: bool, summary: str) -> None:
            calls.append((task, ok, summary))

        async def ask_handler(_ask):
            return "reject"

        async def event_sink(_ev):
            return None

        w = AsyncTaskHealthWatcher(
            mirror=mirror, host_url="http://127.0.0.1:0",
            ask_handler=ask_handler, event_sink=event_sink,
            completion_sink=sink, launch_index=idx,
        )
        w._client = _FakeClient()
        return w, mirror, w._client, calls

    async def test_ingest_populates_parent_and_origin(self):
        w, mirror, client, _calls = await self._build()
        await w._discover_new_threads()
        t = mirror.get("th-1")
        self.assertIsNotNone(t)
        self.assertEqual(t.parent_thread_id, "T-parent")
        self.assertEqual(t.origin, "cli")
        self.assertEqual(t.agent_name, "file-organizer")

    async def test_completion_sink_fires_once_on_terminal(self):
        w, mirror, client, calls = await self._build()
        await w._discover_new_threads()      # ingest th-1 (running)
        await w._poll_known_tasks()          # still running → no completion
        self.assertEqual(calls, [])

        client.run_status = "success"        # task finishes
        await w._poll_known_tasks()          # terminal → sink fires
        self.assertEqual(len(calls), 1)
        task, ok, summary = calls[0]
        self.assertTrue(ok)
        self.assertIn("moved 8 files", summary)
        self.assertEqual(task.parent_thread_id, "T-parent")
        self.assertEqual(task.origin, "cli")

        # Already terminal → dropped from polling → no second call.
        await w._poll_known_tasks()
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
