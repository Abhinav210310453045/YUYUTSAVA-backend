"""Unit tests for TaskSubmissionService (direct + via-triage paths).

Run:  uv run python -m unittest test.daemon.test_task_submission -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from yuyutsava.daemon.task_registry import SqliteTaskStore, TaskRegistry
from yuyutsava.daemon.task_submission import SUBMITTED_TOPIC, TaskSubmissionService


class _RecordingStore:
    """Store stand-in that records the audit-trail writes."""

    def __init__(self) -> None:
        self.event_payloads: list[dict] = []
        self.proposals: list[object] = []
        self.decisions: list[dict] = []

    async def put_event_payload(self, **kw) -> None:
        self.event_payloads.append(kw)

    async def put_proposal(self, p) -> None:
        self.proposals.append(p)

    async def put_decision(self, **kw) -> None:
        self.decisions.append(kw)


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list[object] = []

    async def publish(self, ev) -> None:
        self.published.append(ev)


class TaskSubmissionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.registry = TaskRegistry(
            SqliteTaskStore(Path(self._tmp.name) / "state.db")
        )
        self.queue: asyncio.Queue = asyncio.Queue()
        self.store = _RecordingStore()
        self.bus = _RecordingBus()
        self.svc = TaskSubmissionService(
            registry=self.registry,
            task_queue=self.queue,
            store=self.store,
            bus=self.bus,
            proposal_expiry_sec=300,
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_direct_enqueues_and_registers(self) -> None:
        task_id = await self.svc.submit_direct("organize my downloads", origin="api")
        self.assertTrue(task_id.startswith("tsk_"))

        # Registry row queued.
        rec = await self.registry.get(task_id)
        self.assertEqual(rec.status, "queued")
        self.assertEqual(rec.origin, "api")

        # OrchestratorTask on the queue, joined by task_id.
        self.assertEqual(self.queue.qsize(), 1)
        task = self.queue.get_nowait()
        self.assertEqual(task.task_id, task_id)
        self.assertEqual(task.instruction, "organize my downloads")
        self.assertEqual(task.topic, SUBMITTED_TOPIC)

        # Audit trail: approved proposal + user_submitted decision + payload.
        self.assertEqual(len(self.store.proposals), 1)
        self.assertEqual(self.store.proposals[0].status, "approved")
        self.assertEqual(self.store.decisions[0]["outcome"], "user_submitted")
        self.assertEqual(self.store.event_payloads[0]["topic"], SUBMITTED_TOPIC)
        # Nothing went near triage.
        self.assertEqual(self.bus.published, [])

    async def test_direct_session_hint_lands_on_proposal(self) -> None:
        await self.svc.submit_direct("x", origin="cli", session_hint="cli-1-abc")
        self.assertEqual(self.store.proposals[0].session_id, "cli-1-abc")

    async def test_via_triage_publishes_event(self) -> None:
        task_id = await self.svc.submit_via_triage("review my inbox", origin="telegram")

        # Registry row exists (queued until/unless approved).
        rec = await self.registry.get(task_id)
        self.assertEqual(rec.status, "queued")
        self.assertEqual(rec.origin, "telegram")

        # Nothing enqueued directly — triage owns the consent path.
        self.assertEqual(self.queue.qsize(), 0)

        # Envelope published with the registry join key in hints.
        self.assertEqual(len(self.bus.published), 1)
        ev = self.bus.published[0]
        self.assertEqual(ev.topic, SUBMITTED_TOPIC)
        self.assertEqual(ev.source, "telegram")
        self.assertEqual(ev.hints["task_id"], task_id)
        self.assertEqual(ev.summary, "review my inbox")

        # Full instruction persisted for the audit trail.
        self.assertEqual(
            self.store.event_payloads[0]["payload"]["instruction"],
            "review my inbox",
        )

    async def test_empty_instruction_rejected(self) -> None:
        with self.assertRaises(ValueError):
            await self.svc.submit_direct("   ")
        with self.assertRaises(ValueError):
            await self.svc.submit_via_triage("")


if __name__ == "__main__":
    unittest.main()
