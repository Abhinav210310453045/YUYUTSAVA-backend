"""Durable task resume after a config hot-reload restart.

Covers:
  * ``resume_interrupted_tasks`` re-enqueues ``running`` rows (carrying their
    persisted thread_id) and ``queued`` rows (fresh) left by a prior instance.
  * ``OrchestratorLoop`` reuses the persisted thread_id and passes ``resume=True``
    down to streaming instead of minting a new thread.
  * ``astream_agent_iter(resume=True)`` continues from a checkpoint (``input=None``)
    when state exists, and falls back to a fresh run when it does not.

Run:  uv run python -m unittest test.daemon.test_resume -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from yuyutsava.core.streaming import StreamEvent, astream_agent_iter
from yuyutsava.daemon.channels import ChannelRouter, UserChannel
from yuyutsava.daemon.orchestrator_loop import OrchestratorLoop, resume_interrupted_tasks
from yuyutsava.daemon.task_registry import TaskRegistry
from yuyutsava.daemon.task_store_unified import sqlite_task_store
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


class ResumeOnStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.registry = TaskRegistry(
            sqlite_task_store(Path(self._tmp.name) / "state.db")
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_reenqueues_running_but_never_queued(self) -> None:
        """``queued`` is a consent boundary — see test_resume_consent_boundary.py.

        This case used to assert the opposite (both statuses resumed). That
        behaviour executed Tier-1 proposals the user had declined by timeout:
        a ``queued`` row carries no ``proposal_id``, so at boot a *direct*
        submission (approved, enqueued in the same breath) is indistinguishable
        from a *triage* submission still waiting on — or refused by — the user.
        Observed live: two expired triage proposals were resurrected and run.
        """
        # A task that was mid-flight (running, has a checkpoint thread_id)…
        running_id = self.registry.mint_task_id()
        await self.registry.create(task_id=running_id, origin="api", instruction="resume me")
        await self.registry.mark_running(running_id, thread_id="orch-abc")
        # …and one that never started — never authorised, so never resumed.
        queued_id = self.registry.mint_task_id()
        await self.registry.create(task_id=queued_id, origin="api", instruction="queued one")

        queue: asyncio.Queue[OrchestratorTask] = asyncio.Queue()
        n = await resume_interrupted_tasks(self.registry, queue)
        self.assertEqual(n, 1, "only the running task may resume")

        items = []
        while not queue.empty():
            items.append(queue.get_nowait())
        by_id = {t.task_id: t for t in items}

        self.assertEqual(by_id[running_id].resume_thread_id, "orch-abc")
        self.assertEqual(by_id[running_id].instruction, "resume me")
        self.assertNotIn(
            queued_id, by_id,
            "a never-authorised queued task was re-enqueued — a restart is "
            "bypassing Tier-1 consent",
        )

    async def test_ignores_terminal_tasks(self) -> None:
        done_id = self.registry.mint_task_id()
        await self.registry.create(task_id=done_id, origin="api", instruction="finished")
        await self.registry.mark_running(done_id, thread_id="orch-done")
        await self.registry.mark_done(done_id, result_summary="ok")

        queue: asyncio.Queue[OrchestratorTask] = asyncio.Queue()
        n = await resume_interrupted_tasks(self.registry, queue)
        self.assertEqual(n, 0)
        self.assertTrue(queue.empty())

    async def test_no_registry_is_noop(self) -> None:
        queue: asyncio.Queue[OrchestratorTask] = asyncio.Queue()
        self.assertEqual(await resume_interrupted_tasks(None, queue), 0)


class OrchestratorLoopResumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.registry = TaskRegistry(
            sqlite_task_store(Path(self._tmp.name) / "state.db")
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

    async def test_resume_reuses_thread_id_and_passes_resume_flag(self) -> None:
        task_id = self.registry.mint_task_id()
        await self.registry.create(task_id=task_id, origin="api", instruction="x")
        await self.registry.mark_running(task_id, thread_id="orch-prev")  # prior run

        captured: dict = {}

        async def fake_stream(graph, message, **kw):
            captured["resume"] = kw.get("resume")
            captured["thread_id"] = kw.get("thread_id")
            yield StreamEvent(kind="final", data={"text": "resumed done"})

        self._patch_stream(fake_stream)
        await self.loop._run_task(OrchestratorTask(
            proposal_id="", event_id="", topic="resume", summary="s",
            instruction="x", subagent_hint="general-purpose", urgency=2,
            task_id=task_id, resume_thread_id="orch-prev",
        ))

        self.assertIs(captured["resume"], True)
        self.assertEqual(captured["thread_id"], "orch-prev")
        rec = await self.registry.get(task_id)
        self.assertEqual(rec.status, "done")
        self.assertEqual(rec.thread_id, "orch-prev")

    async def test_normal_task_mints_fresh_thread_and_no_resume(self) -> None:
        task_id = self.registry.mint_task_id()
        await self.registry.create(task_id=task_id, origin="api", instruction="x")

        captured: dict = {}

        async def fake_stream(graph, message, **kw):
            captured["resume"] = kw.get("resume")
            captured["thread_id"] = kw.get("thread_id")
            yield StreamEvent(kind="final", data={"text": "done"})

        self._patch_stream(fake_stream)
        await self.loop._run_task(OrchestratorTask(
            proposal_id="p", event_id="e", topic="t", summary="s",
            instruction="x", subagent_hint="general-purpose", urgency=2,
            task_id=task_id,
        ))

        self.assertIs(captured["resume"], False)
        self.assertTrue(captured["thread_id"].startswith("orch-"))
        self.assertNotEqual(captured["thread_id"], "orch-prev")


class _FakeSnap:
    def __init__(self, values):
        self.values = values


class _FakeAgent:
    """Minimal CompiledStateGraph stand-in for astream_agent_iter."""

    def __init__(self, state_values):
        self._state = state_values
        self.astream_inputs: list = []

    async def aget_state(self, cfg):
        return _FakeSnap(self._state)

    async def astream(self, inp, config=None, stream_mode=None):
        self.astream_inputs.append(inp)
        # No work to stream: the iterator completes immediately so the wrapper
        # emits its terminal "final" event.
        if False:  # pragma: no cover — marks this an async generator
            yield


class StreamingResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_continues_from_checkpoint(self) -> None:
        agent = _FakeAgent({"messages": [object()]})
        events = [
            ev async for ev in astream_agent_iter(
                agent, "the task", thread_id="t1", resume=True,
            )
        ]
        # Resumed runs continue from the checkpoint → no fresh HumanMessage.
        self.assertEqual(agent.astream_inputs, [None])
        self.assertEqual(events[-1].kind, "final")

    async def test_resume_without_state_falls_back_to_fresh(self) -> None:
        agent = _FakeAgent({})  # no messages → nothing to resume
        events = [
            ev async for ev in astream_agent_iter(
                agent, "the task", thread_id="t1", resume=True,
            )
        ]
        self.assertEqual(len(agent.astream_inputs), 1)
        self.assertIsInstance(agent.astream_inputs[0], dict)
        self.assertIn("messages", agent.astream_inputs[0])
        self.assertEqual(events[-1].kind, "final")


if __name__ == "__main__":
    unittest.main()
