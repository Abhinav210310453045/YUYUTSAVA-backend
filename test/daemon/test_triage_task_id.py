"""Triage carries the TaskRegistry join key from event hints to the queue.

Run:  uv run python -m unittest test.daemon.test_triage_task_id -v
"""

from __future__ import annotations

import asyncio
import time
import unittest

from yuyutsava.daemon.channels import ProposalDecision
from yuyutsava.daemon.triage_loop import TriageLoop
from yuyutsava.events.bus import EventEnvelope
from yuyutsava.storage.events import Proposal


class _StubStore:
    async def put_decision(self, **kw) -> None:
        pass

    async def put_proposal(self, p) -> None:
        pass

    async def put_consent_rule(self, rule) -> None:
        pass


def _loop(queue: asyncio.Queue) -> TriageLoop:
    return TriageLoop(
        bus=object(), store=_StubStore(), channels=object(), triage=object(),
        capabilities_block="", task_queue=queue, proposal_expiry_sec=300,
    )


def _envelope(hints: dict[str, str]) -> EventEnvelope:
    return EventEnvelope(
        event_id="e1", topic="user.task.submitted", source="api",
        ts=time.time(), severity=1, summary="do x",
        payload_ref="sqlite://event_payloads/e1", hints=hints,
    )


class TriageTaskIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_decision_path_carries_task_id(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        proposal = Proposal.new(
            event_id="e1", topic="user.task.submitted", summary="do x",
            proposed="do x", subagent="general-purpose", urgency=2,
            expiry_sec=300,
        )
        await _loop(queue)._handle_user_decision(
            _envelope({"task_id": "tsk_123"}), proposal,
            ProposalDecision(decision="approve"),
        )
        task = queue.get_nowait()
        self.assertEqual(task.task_id, "tsk_123")

    async def test_organic_event_leaves_task_id_empty(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        proposal = Proposal.new(
            event_id="e1", topic="fs.changed", summary="new file",
            proposed="move it", subagent="file-organizer", urgency=1,
            expiry_sec=300,
        )
        await _loop(queue)._handle_user_decision(
            _envelope({}), proposal, ProposalDecision(decision="approve"),
        )
        task = queue.get_nowait()
        self.assertEqual(task.task_id, "")


if __name__ == "__main__":
    unittest.main()
