"""A consent rule decides an event before triage's model is asked.

``TriageLoop._handle`` consults consent rules first: ``auto_skip`` records
``skipped_by_rule`` and posts a timeline line; ``auto_approve`` files an
approved proposal and queues the orchestrator task without asking anyone, with
a file-organizer instruction built from the event. Neither calls the model.
(The model paths — drop, log, propose — are in test_triage_drop_is_recorded.)
"""

from __future__ import annotations

import asyncio
import time
import unittest
from typing import Any

from yuyutsava.agents.triage.agent import TriageDecision
from yuyutsava.daemon.triage_loop import TriageLoop
from yuyutsava.events.bus import EventEnvelope


class _Store:
    def __init__(self) -> None:
        self.decisions: list[dict] = []
        self.proposals: list[Any] = []

    async def put_decision(self, **kw: Any) -> None:
        self.decisions.append(kw)

    async def put_proposal(self, p: Any) -> None:
        self.proposals.append(p)


class _Channels:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    async def post_event(self, ev: Any) -> None:
        self.lines.append((ev.payload.line, ev.payload.cls))

    async def post_proposal(self, p: Any) -> Any:  # pragma: no cover
        raise AssertionError("a rule-decided event must never ask the user")


class _Rule:
    def __init__(self, decision: str) -> None:
        self.rule = type("R", (), {"decision": decision, "rule_id": "r7"})()

    async def evaluate(self, ev: Any) -> Any:
        return self


class _Model:
    def __init__(self, action: str) -> None:
        self.action = action

    async def classify(self, *a: Any, **k: Any) -> Any:
        return TriageDecision(
            action=self.action, subagent_hint="", proposed_instruction="", reason="why", urgency=1
        )


class _NoRuleAtAll:
    rule = None

    async def evaluate(self, ev: Any) -> Any:
        return self


class _NoModel:
    async def classify(self, *a: Any, **k: Any) -> Any:  # pragma: no cover
        raise AssertionError("a rule-decided event must not reach the model")


def _event() -> EventEnvelope:
    return EventEnvelope(
        event_id="e1",
        topic="fs.created",
        source="fs",
        ts=time.time(),
        severity=1,
        summary="new file report.pdf",
        payload_ref="sqlite://event_payloads/e1",
        hints={"task_id": "tsk_1", "path": "/Users/me/Downloads/report.pdf"},
    )


def _run(decision: str, model: Any = None):
    store, channels, queue = _Store(), _Channels(), asyncio.Queue()
    loop = TriageLoop(
        bus=object(),
        store=store,
        channels=channels,
        triage=model or _NoModel(),
        capabilities_block="",
        task_queue=queue,
        proposal_expiry_sec=300,
    )
    loop._consent = _Rule(decision) if decision else _NoRuleAtAll()  # type: ignore[assignment]
    asyncio.run(asyncio.wait_for(loop._handle(_event()), 10))
    tasks = []
    while not queue.empty():
        tasks.append(queue.get_nowait())
    return store, channels, tasks


class AutoSkip(unittest.TestCase):
    def test_records_the_rule_and_says_so(self) -> None:
        store, channels, tasks = _run("auto_skip")
        self.assertEqual(
            store.decisions,
            [
                {
                    "proposal_id": None,
                    "event_id": "e1",
                    "outcome": "skipped_by_rule",
                    "action_summary": "rule:r7",
                }
            ],
        )
        self.assertEqual(
            channels.lines, [("auto-skipped: new file report.pdf", "event-decision-skipped")]
        )
        self.assertEqual((store.proposals, tasks), ([], []))


class AutoApprove(unittest.TestCase):
    def test_files_an_approved_proposal_and_queues_the_work(self) -> None:
        store, channels, tasks = _run("auto_approve")
        (proposal,) = store.proposals
        self.assertEqual(proposal.status, "approved")
        (decision,) = store.decisions
        self.assertEqual(
            (decision["outcome"], decision["action_summary"], decision["proposal_id"]),
            ("auto_approved", "rule:r7", proposal.proposal_id),
        )
        self.assertEqual(channels.lines[0][1], "event-decision-approved")
        (task,) = tasks
        self.assertEqual((task.task_id, task.complexity, task.urgency), ("tsk_1", 1, 1))
        self.assertEqual(task.instruction, proposal.proposed)
        self.assertTrue(task.instruction)


class ModelVerdictLines(unittest.TestCase):
    """The timeline line for each verdict nobody acts on, style included."""

    def test_drop_and_log(self) -> None:
        for action, line, cls in (
            ("drop", "dropped: new file report.pdf — why", "event-decision-skipped"),
            ("log", "logged: new file report.pdf — why", ""),
        ):
            with self.subTest(action=action):
                store, channels, _ = _run("", _Model(action))
                self.assertEqual(channels.lines, [(line, cls)])
                self.assertEqual(store.decisions[0]["action_summary"], "why")


if __name__ == "__main__":
    unittest.main()
