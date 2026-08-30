"""Tasks endpoints: submit (direct/triage), list, detail, cancel, replay.

Run:  uv run python -m unittest test.web.test_tasks_api -v
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from yuyutsava.daemon.channels import ChannelEvent, LogPayload
from yuyutsava.daemon.task_registry import TaskRegistry
from yuyutsava.daemon.task_store_unified import sqlite_task_store
from yuyutsava.daemon.task_submission import SUBMITTED_TOPIC, TaskSubmissionService
from yuyutsava.daemon.web.app import create_app
from yuyutsava.daemon.web.services.stream_service import WebChannel, WebHub


class _RecordingStore:
    async def put_event_payload(self, **kw) -> None: ...
    async def put_proposal(self, p) -> None: ...
    async def put_decision(self, **kw) -> None: ...


class _RecordingBus:
    def __init__(self) -> None:
        self.published: list = []

    async def publish(self, ev) -> None:
        self.published.append(ev)


class TasksApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.registry = TaskRegistry(
            sqlite_task_store(Path(self._tmp.name) / "state.db")
        )
        self.queue: asyncio.Queue = asyncio.Queue()
        self.bus = _RecordingBus()
        self.hub = WebHub(store=_RecordingStore())
        submission = TaskSubmissionService(
            registry=self.registry, task_queue=self.queue,
            store=_RecordingStore(), bus=self.bus, proposal_expiry_sec=300,
        )
        app = create_app(
            self.hub, host="127.0.0.1",
            task_registry=self.registry, task_submission=submission,
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self._tmp.cleanup()

    async def test_submit_direct_then_get(self) -> None:
        r = await self.client.post(
            "/tasks", json={"instruction": "summarize ~/Downloads"},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        task_id = body["task_id"]
        self.assertTrue(task_id.startswith("tsk_"))
        self.assertEqual(body["mode"], "direct")
        self.assertEqual(self.queue.qsize(), 1)

        r = await self.client.get(f"/tasks/{task_id}")
        self.assertEqual(r.status_code, 200)
        detail = r.json()
        self.assertEqual(detail["status"], "queued")
        self.assertEqual(detail["instruction"], "summarize ~/Downloads")

    async def test_submit_triage_publishes(self) -> None:
        r = await self.client.post(
            "/tasks", json={"instruction": "review inbox", "mode": "triage"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.queue.qsize(), 0)
        self.assertEqual(len(self.bus.published), 1)
        self.assertEqual(self.bus.published[0].topic, SUBMITTED_TOPIC)

    async def test_list_and_status_filter(self) -> None:
        for i in range(3):
            await self.client.post("/tasks", json={"instruction": f"t{i}"})
        r = await self.client.get("/tasks", params={"limit": 2})
        body = r.json()
        self.assertEqual(len(body["tasks"]), 2)
        self.assertIsNotNone(body["next_cursor"])

        r = await self.client.get(
            "/tasks", params={"limit": 2, "cursor": body["next_cursor"]},
        )
        self.assertEqual(len(r.json()["tasks"]), 1)

        r = await self.client.get("/tasks", params={"status": "done"})
        self.assertEqual(r.json()["tasks"], [])

    async def test_cancel_paths(self) -> None:
        r = await self.client.post("/tasks", json={"instruction": "x"})
        task_id = r.json()["task_id"]

        r = await self.client.post(f"/tasks/{task_id}/cancel")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.registry.cancel_requested(task_id))

        r = await self.client.post("/tasks/tsk_missing/cancel")
        self.assertEqual(r.status_code, 404)

        await self.registry.mark_done(task_id, result_summary="done")
        r = await self.client.post(f"/tasks/{task_id}/cancel")
        self.assertEqual(r.status_code, 409)

    async def test_events_replay_from_ring(self) -> None:
        r = await self.client.post("/tasks", json={"instruction": "x"})
        task_id = r.json()["task_id"]

        # Simulate the orchestrator emitting tagged events through the
        # WebChannel (the same path production uses).
        channel = WebChannel(self.hub)
        for i in range(3):
            await channel.post_event(ChannelEvent(
                payload=LogPayload(text=f"line {i}"),
                task_id=task_id, session_id="orch-1-abc",
            ))
        # Untagged event must not leak into the ring.
        await channel.post_event(ChannelEvent(payload=LogPayload(text="noise")))

        r = await self.client.get(f"/tasks/{task_id}/events")
        self.assertEqual(r.status_code, 200)
        events = r.json()["events"]
        self.assertEqual(len(events), 3)
        self.assertEqual(events[0]["data"]["text"], "line 0")
        self.assertEqual(events[0]["task_id"], task_id)
        self.assertEqual(events[0]["session_id"], "orch-1-abc")

        r = await self.client.get("/tasks/tsk_missing/events")
        self.assertEqual(r.status_code, 404)

    async def test_direct_submission_runs_end_to_end(self) -> None:
        """POST /tasks → orchestrator consumes the queue → GET shows done."""
        from types import SimpleNamespace
        from unittest import mock

        from yuyutsava.core.streaming import StreamEvent
        from yuyutsava.daemon.channels import ChannelRouter
        from yuyutsava.daemon.orchestrator_loop import OrchestratorLoop

        r = await self.client.post("/tasks", json={"instruction": "do it"})
        task_id = r.json()["task_id"]

        loop = OrchestratorLoop(
            task_queue=self.queue,
            channels=ChannelRouter(channels=[WebChannel(self.hub)]),
            store=_RecordingStore(),
            orchestrator_model=object(),
            deps=SimpleNamespace(
                skill_registry=None, async_task_mirror=None, memory_store=None,
            ),
            orchestrator_token_budget=1000,
            task_registry=self.registry,
        )

        async def fake_stream(graph, message, **kw):
            yield StreamEvent(kind="log", data={"text": "working"})
            yield StreamEvent(kind="final", data={"text": "did it"})

        with mock.patch(
            "yuyutsava.daemon.orchestrator_loop.build_orchestrator",
            return_value=object(),
        ), mock.patch(
            "yuyutsava.daemon.orchestrator_loop.astream_agent_iter", fake_stream,
        ):
            await loop._run_task(self.queue.get_nowait())

        r = await self.client.get(f"/tasks/{task_id}")
        detail = r.json()
        self.assertEqual(detail["status"], "done")
        self.assertEqual(detail["result_summary"], "did it")
        self.assertIsNotNone(detail["thread_id"])

        # The run's events are replayable for late joiners.
        r = await self.client.get(f"/tasks/{task_id}/events")
        kinds = [e["kind"] for e in r.json()["events"]]
        self.assertIn("log", kinds)
        self.assertIn("timeline", kinds)

    async def test_validation(self) -> None:
        r = await self.client.post("/tasks", json={"instruction": ""})
        self.assertEqual(r.status_code, 422)
        r = await self.client.post(
            "/tasks", json={"instruction": "x", "mode": "bogus"},
        )
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
