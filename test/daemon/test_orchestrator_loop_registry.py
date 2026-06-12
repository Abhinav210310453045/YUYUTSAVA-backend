"""OrchestratorLoop ↔ TaskRegistry integration (fake graph, no LLM).

Covers the Phase-2 contract: status transitions queued→running→done|failed|
cancelled, thread_id joining, event tagging with task_id/session_id, and
the coarse between-events cancel check.

Run:  uv run python -m unittest test.daemon.test_orchestrator_loop_registry -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from yuyutsava.core.streaming import StreamEvent
from yuyutsava.daemon.channels import ChannelRouter, UserChannel
from yuyutsava.daemon.orchestrator_loop import OrchestratorLoop
from yuyutsava.daemon.task_registry import SqliteTaskStore, TaskRegistry
from yuyutsava.daemon.triage_loop import OrchestratorTask


class _RecordingChannel(UserChannel):
    name = "recording"

    def __init__(self) -> None:
        self.events: list = []

    async def post_event(self, ev) -> None:
        self.events.append(ev)

    async def post_proposal(self, p):
        raise NotImplementedError

    async def post_ask(self, a) -> str:
        raise NotImplementedError


class _RecordingStore:
    def __init__(self) -> None:
        self.decisions: list[dict] = []

    async def put_decision(self, **kw) -> None:
        self.decisions.append(kw)


def _task(task_id: str = "") -> OrchestratorTask:
    return OrchestratorTask(
        proposal_id="p1", event_id="e1", topic="user.task.submitted",
        summary="sum", instruction="do the thing", subagent_hint="general-purpose",
        urgency=2, task_id=task_id,
    )


class OrchestratorLoopRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.registry = TaskRegistry(
            SqliteTaskStore(Path(self._tmp.name) / "state.db")
        )
        self.channel = _RecordingChannel()
        self.store = _RecordingStore()
        self.loop = OrchestratorLoop(
            task_queue=None,  # _run_task is driven directly
            channels=ChannelRouter(channels=[self.channel]),
            store=self.store,
            orchestrator_model=object(),
            deps=SimpleNamespace(
                skill_registry=None, async_task_mirror=None, memory_store=None,
            ),
            orchestrator_token_budget=1000,
            task_registry=self.registry,
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _patch_stream(self, events_factory):
        """Patch graph build + streaming inside the orchestrator-loop module."""
        build_p = mock.patch(
            "yuyutsava.daemon.orchestrator_loop.build_orchestrator",
            return_value=object(),
        )
        stream_p = mock.patch(
            "yuyutsava.daemon.orchestrator_loop.astream_agent_iter",
            events_factory,
        )
        build_p.start()
        stream_p.start()
        self.addCleanup(build_p.stop)
        self.addCleanup(stream_p.stop)

    async def test_happy_path_transitions_and_tagging(self) -> None:
        task_id = self.registry.mint_task_id()
        await self.registry.create(task_id=task_id, origin="api", instruction="do the thing")

        async def fake_stream(graph, message, **kw):
            yield StreamEvent(kind="token", data={"text": "hi"})
            yield StreamEvent(kind="final", data={"text": "all done"})

        self._patch_stream(fake_stream)
        await self.loop._run_task(_task(task_id))

        rec = await self.registry.get(task_id)
        self.assertEqual(rec.status, "done")
        self.assertEqual(rec.result_summary, "all done")
        self.assertTrue(rec.thread_id.startswith("orch-"))
        self.assertIsNotNone(rec.started_ts)
        self.assertIsNotNone(rec.finished_ts)

        # Every event the run emitted is tagged for per-task SSE filtering.
        tagged = [e for e in self.channel.events if e.task_id == task_id]
        self.assertEqual(len(tagged), len(self.channel.events))
        self.assertTrue(all(e.session_id == rec.thread_id for e in tagged))

        # Decision audit row still written.
        self.assertEqual(self.store.decisions[-1]["outcome"], "orchestrator_done")

    async def test_organic_task_gets_minted_registry_row(self) -> None:
        async def fake_stream(graph, message, **kw):
            yield StreamEvent(kind="final", data={"text": "ok"})

        self._patch_stream(fake_stream)
        await self.loop._run_task(_task(task_id=""))

        rows, _ = await self.registry.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].origin, "event:user.task.submitted")
        self.assertEqual(rows[0].status, "done")

    async def test_cancel_between_stream_events(self) -> None:
        task_id = self.registry.mint_task_id()
        await self.registry.create(task_id=task_id, origin="api", instruction="x")
        registry = self.registry

        async def fake_stream(graph, message, **kw):
            yield StreamEvent(kind="token", data={"text": "1"})
            await registry.request_cancel(task_id)
            yield StreamEvent(kind="token", data={"text": "2"})
            raise AssertionError("stream should have been abandoned after cancel")

        self._patch_stream(fake_stream)
        await self.loop._run_task(_task(task_id))

        rec = await self.registry.get(task_id)
        self.assertEqual(rec.status, "cancelled")
        self.assertEqual(self.store.decisions[-1]["outcome"], "orchestrator_cancelled")

    async def test_cancel_while_still_queued_skips_run(self) -> None:
        task_id = self.registry.mint_task_id()
        await self.registry.create(task_id=task_id, origin="api", instruction="x")
        await self.registry.request_cancel(task_id)

        async def fake_stream(graph, message, **kw):
            raise AssertionError("graph must not run for a pre-cancelled task")
            yield  # pragma: no cover

        self._patch_stream(fake_stream)
        await self.loop._run_task(_task(task_id))

        rec = await self.registry.get(task_id)
        self.assertEqual(rec.status, "cancelled")

    async def test_failure_marks_failed_and_reraises(self) -> None:
        task_id = self.registry.mint_task_id()
        await self.registry.create(task_id=task_id, origin="api", instruction="x")

        async def fake_stream(graph, message, **kw):
            yield StreamEvent(kind="token", data={"text": "1"})
            raise RuntimeError("graph exploded")

        self._patch_stream(fake_stream)
        with self.assertRaises(RuntimeError):
            await self.loop._run_task(_task(task_id))

        rec = await self.registry.get(task_id)
        self.assertEqual(rec.status, "failed")
        self.assertIn("graph exploded", rec.error)


if __name__ == "__main__":
    unittest.main()
