"""SSE scope filters + WebHub per-task ring bounds.

Run:  uv run python -m unittest test.web.test_stream_filter -v
"""

from __future__ import annotations

import unittest

from yuyutsava.daemon.channels import LogPayload
from yuyutsava.daemon.web.routers.stream import item_matches
from yuyutsava.daemon.web.services.stream_service import (
    MAX_TRACKED_TASKS,
    TASK_RING_SIZE,
    StreamAskItem,
    StreamEventItem,
    StreamProposalItem,
    WebHub,
)
from yuyutsava.storage.events import Proposal


def _event(task_id=None, session_id=None) -> StreamEventItem:
    return StreamEventItem(
        payload=LogPayload(text="x"), task_id=task_id, session_id=session_id,
    )


def _proposal(session_id=None) -> StreamProposalItem:
    return StreamProposalItem(proposal=Proposal.new(
        event_id="e1", topic="t", summary="s", proposed="p",
        subagent="general-purpose", urgency=1, expiry_sec=60,
        session_id=session_id,
    ))


class ItemMatchesTests(unittest.TestCase):
    def test_no_filters_passes_everything(self) -> None:
        self.assertTrue(item_matches(_event(), None, None))
        self.assertTrue(item_matches(_proposal(), None, None))

    def test_task_id_filter(self) -> None:
        self.assertTrue(item_matches(_event(task_id="tsk_1"), "tsk_1", None))
        self.assertFalse(item_matches(_event(task_id="tsk_2"), "tsk_1", None))
        self.assertFalse(item_matches(_event(), "tsk_1", None))
        # Proposals carry no task tag — excluded under a task_id filter.
        self.assertFalse(item_matches(_proposal(), "tsk_1", None))

    def test_session_id_filter(self) -> None:
        self.assertTrue(item_matches(
            _event(session_id="orch-1"), None, "orch-1"))
        self.assertFalse(item_matches(
            _event(session_id="orch-2"), None, "orch-1"))
        # Proposals match via their nested proposal.session_id.
        self.assertTrue(item_matches(_proposal("orch-1"), None, "orch-1"))
        self.assertFalse(item_matches(_proposal("orch-2"), None, "orch-1"))
        # Asks carry session_id directly.
        ask = StreamAskItem(
            ask_id="a1", title="t", body="b", options=[], session_id="orch-1",
        )
        self.assertTrue(item_matches(ask, None, "orch-1"))


class TaskRingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.hub = WebHub(store=object())

    async def test_ring_is_bounded_per_task(self) -> None:
        for i in range(TASK_RING_SIZE + 50):
            await self.hub.broadcast(_event(task_id="tsk_1"))
        self.assertEqual(len(self.hub.task_events("tsk_1")), TASK_RING_SIZE)

    async def test_untagged_items_not_ringed(self) -> None:
        await self.hub.broadcast(_event())
        self.assertEqual(self.hub.task_events("tsk_1"), [])

    async def test_oldest_task_evicted_past_cap(self) -> None:
        for i in range(MAX_TRACKED_TASKS + 1):
            await self.hub.broadcast(_event(task_id=f"tsk_{i:03d}"))
        self.assertEqual(self.hub.task_events("tsk_000"), [])
        self.assertEqual(len(self.hub.task_events(f"tsk_{MAX_TRACKED_TASKS:03d}")), 1)


if __name__ == "__main__":
    unittest.main()
