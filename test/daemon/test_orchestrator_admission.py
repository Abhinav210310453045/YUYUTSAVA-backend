"""OrchestratorLoop ↔ AdmissionController integration (fake graph, fake clock).

The Phase-5 contract on the loop: heavy tasks pass through admission before
mark_running (deferral shows up as `deferred_ms` on the registry row and a
timeline event), a critically full disk fails the task with a clear error,
and a cancellation issued during a long deferral is honored before the
graph ever starts.

Run:  uv run python -m unittest test.daemon.test_orchestrator_admission -v
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from yuyutsava.core.streaming import StreamEvent
from yuyutsava.daemon.channels import ChannelRouter, UserChannel
from yuyutsava.daemon.orchestrator_loop import OrchestratorLoop
from yuyutsava.daemon.resources import (
    AdmissionController,
    ResourceMonitor,
    ResourceSettings,
    ResourceSnapshot,
)
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

    def timeline_lines(self) -> list[str]:
        return [getattr(e.payload, "line", "") for e in self.events]


class _RecordingStore:
    def __init__(self) -> None:
        self.decisions: list[dict] = []

    async def put_decision(self, **kw) -> None:
        self.decisions.append(kw)


def _snap(cpu: float = 10.0, mem: float = 8000.0, disk: float = 100.0) -> ResourceSnapshot:
    return ResourceSnapshot(
        cpu_pct=cpu, mem_available_mb=mem, disk_free_gb=disk, ts=time.time(),
    )


def _task(task_id: str, complexity: int = 5) -> OrchestratorTask:
    return OrchestratorTask(
        proposal_id="p1", event_id="e1", topic="user.task.submitted",
        summary="sum", instruction="do the heavy thing",
        subagent_hint="general-purpose", urgency=2, task_id=task_id,
        complexity=complexity,
    )


class OrchestratorAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.registry = TaskRegistry(
            sqlite_task_store(Path(self._tmp.name) / "state.db")
        )
        self.channel = _RecordingChannel()
        self.store = _RecordingStore()

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _loop(self, admission: AdmissionController) -> OrchestratorLoop:
        return OrchestratorLoop(
            task_queue=None,  # _run_task is driven directly
            channels=ChannelRouter(channels=[self.channel]),
            store=self.store,
            orchestrator_model=object(),
            deps=SimpleNamespace(
                skill_registry=None, async_task_mirror=None, memory_store=None,
            ),
            orchestrator_token_budget=1000,
            task_registry=self.registry,
            admission=admission,
        )

    def _admission(
        self, *snaps: ResourceSnapshot, on_sleep=None,
    ) -> tuple[AdmissionController, ResourceMonitor]:
        """Admission over a scripted monitor; the fake sleep advances a fake
        clock and re-samples, optionally running ``on_sleep`` (e.g. cancel)."""
        script = list(snaps)
        monitor = ResourceMonitor(
            ResourceSettings(),
            sampler=lambda: script.pop(0) if len(script) > 1 else script[0],
        )
        clock = SimpleNamespace(now=0.0)

        async def fake_sleep(sec: float) -> None:
            clock.now += sec
            await monitor.sample_once()
            if on_sleep is not None:
                await on_sleep()

        admission = AdmissionController(
            monitor, monitor.settings, registry=self.registry,
            event_sink=self._loop_channels_post_event,
            sleep=fake_sleep, clock=lambda: clock.now,
        )
        return admission, monitor

    async def _loop_channels_post_event(self, ev) -> None:
        await self.channel.post_event(ev)

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

    async def test_heavy_task_deferred_then_runs_records_deferred_ms(self) -> None:
        admission, monitor = self._admission(_snap(cpu=95), _snap(cpu=20))
        await monitor.sample_once()                # ring starts loaded
        task_id = self.registry.mint_task_id()
        await self.registry.create(task_id=task_id, origin="api", instruction="x")

        async def fake_stream(graph, message, **kw):
            yield StreamEvent(kind="final", data={"text": "done after defer"})

        self._patch_stream(fake_stream)
        await self._loop(admission)._run_task(_task(task_id))

        rec = await self.registry.get(task_id)
        self.assertEqual(rec.status, "done")
        self.assertEqual(rec.deferred_ms, 2000)    # one 2s backoff sleep
        self.assertTrue(any(
            "deferred — system busy" in ln for ln in self.channel.timeline_lines()
        ))

    async def test_disk_critical_marks_task_failed(self) -> None:
        admission, monitor = self._admission(_snap(disk=1.0))
        await monitor.sample_once()
        task_id = self.registry.mint_task_id()
        await self.registry.create(task_id=task_id, origin="api", instruction="x")

        async def fake_stream(graph, message, **kw):
            raise AssertionError("graph must not run on a critically full disk")
            yield  # pragma: no cover

        self._patch_stream(fake_stream)
        with self.assertRaises(Exception):
            await self._loop(admission)._run_task(_task(task_id))

        rec = await self.registry.get(task_id)
        self.assertEqual(rec.status, "failed")
        self.assertIn("disk critically low", rec.error)

    async def test_cancel_during_deferral_never_starts_graph(self) -> None:
        task_id = self.registry.mint_task_id()
        await self.registry.create(task_id=task_id, origin="api", instruction="x")

        async def cancel_while_deferred() -> None:
            await self.registry.request_cancel(task_id)

        admission, monitor = self._admission(
            _snap(cpu=95), _snap(cpu=20), on_sleep=cancel_while_deferred,
        )
        await monitor.sample_once()

        async def fake_stream(graph, message, **kw):
            raise AssertionError("graph must not run for a task cancelled while deferred")
            yield  # pragma: no cover

        self._patch_stream(fake_stream)
        await self._loop(admission)._run_task(_task(task_id))

        rec = await self.registry.get(task_id)
        self.assertEqual(rec.status, "cancelled")
        self.assertEqual(admission.heavy_slots_in_use, 0)  # slot released

    async def test_light_task_unaffected_by_loaded_system(self) -> None:
        admission, monitor = self._admission(_snap(cpu=99))
        await monitor.sample_once()
        task_id = self.registry.mint_task_id()
        await self.registry.create(task_id=task_id, origin="api", instruction="x")

        async def fake_stream(graph, message, **kw):
            yield StreamEvent(kind="final", data={"text": "quick"})

        self._patch_stream(fake_stream)
        await self._loop(admission)._run_task(_task(task_id, complexity=1))

        rec = await self.registry.get(task_id)
        self.assertEqual(rec.status, "done")
        self.assertEqual(rec.deferred_ms, 0)


if __name__ == "__main__":
    unittest.main()
