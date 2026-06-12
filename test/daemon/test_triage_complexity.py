"""Triage complexity score flows onto the OrchestratorTask (Phase 4).

Run:  uv run python -m unittest test.daemon.test_triage_complexity -v
"""

from __future__ import annotations

import asyncio
import time
import unittest

import pydantic

from yuyutsava.agents.triage.agent import TriageDecision
from yuyutsava.daemon.channels import ProposalDecision
from yuyutsava.daemon.triage_loop import OrchestratorTask, TriageLoop
from yuyutsava.events.bus import EventEnvelope
from yuyutsava.storage.events import Proposal


class _StubStore:
    async def put_decision(self, **kw) -> None: ...
    async def put_proposal(self, p) -> None: ...
    async def put_consent_rule(self, rule) -> None: ...


class _NullChannels:
    async def post_event(self, ev) -> None: ...


def _loop(queue: asyncio.Queue) -> TriageLoop:
    return TriageLoop(
        bus=object(), store=_StubStore(), channels=_NullChannels(),
        triage=object(), capabilities_block="", task_queue=queue,
        proposal_expiry_sec=300,
    )


def _envelope() -> EventEnvelope:
    return EventEnvelope(
        event_id="e1", topic="fs.changed", source="fs",
        ts=time.time(), severity=1, summary="new file",
        payload_ref="sqlite://event_payloads/e1", hints={},
    )


def _proposal() -> Proposal:
    return Proposal.new(
        event_id="e1", topic="fs.changed", summary="new file",
        proposed="move it", subagent="file-organizer", urgency=1,
        expiry_sec=300,
    )


class TriageDecisionComplexityTests(unittest.TestCase):
    def test_defaults_to_three(self) -> None:
        d = TriageDecision(action="drop", reason="r")
        self.assertEqual(d.complexity, 3)

    def test_bounds_enforced(self) -> None:
        for bad in (0, 6):
            with self.assertRaises(pydantic.ValidationError):
                TriageDecision(action="drop", reason="r", complexity=bad)

    def test_orchestrator_task_default(self) -> None:
        task = OrchestratorTask(
            proposal_id="p", event_id="e", topic="t", summary="s",
            instruction="i", subagent_hint="h", urgency=1,
        )
        self.assertEqual(task.complexity, 3)


class TriageLoopComplexityTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_decision_path_carries_complexity(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        await _loop(queue)._handle_user_decision(
            _envelope(), _proposal(), ProposalDecision(decision="approve"),
            complexity=5,
        )
        self.assertEqual(queue.get_nowait().complexity, 5)

    async def test_user_decision_path_defaults_complexity(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        await _loop(queue)._handle_user_decision(
            _envelope(), _proposal(), ProposalDecision(decision="approve"),
        )
        self.assertEqual(queue.get_nowait().complexity, 3)

    async def test_auto_approve_path_scores_one(self) -> None:
        # Rule-approved single-file moves are the anchored complexity-1
        # example; no LLM is in the loop to score them.
        queue: asyncio.Queue = asyncio.Queue()
        decision = TriageDecision(
            action="propose", subagent_hint="file-organizer",
            proposed_instruction="move it", reason="auto_approve rule",
            urgency=1, complexity=1,
        )
        await _loop(queue)._auto_approve_path(_envelope(), decision, rule_id="r1")
        self.assertEqual(queue.get_nowait().complexity, 1)


if __name__ == "__main__":
    unittest.main()
