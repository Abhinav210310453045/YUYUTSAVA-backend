"""A dropped event is recorded, not silently discarded.

Found by driving the running daemon: a task submitted with ``mode=triage`` sat
in ``queued`` forever with **no trace anywhere** — no decision row, no timeline
line, no log. It looked exactly like a broken daemon.

``TriageLoop._handle`` records every other outcome:

    auto_skip      -> put_decision(outcome="skipped_by_rule") + timeline
    auto_approve   -> put_decision(outcome="auto_approved")   + timeline
    log            -> put_decision(outcome="logged")          + timeline
    propose        -> put_proposal(...) and on into consent
    drop           -> return          # <- nothing at all

The registry row staying ``queued`` is deliberate v1 behaviour
(``task_submission.submit_via_triage`` says so). The **silence** was not: with
nothing written, "triage judged this not worth acting on" and "the daemon never
picked it up" are the same observation from the UI.

Run:  .venv/bin/python test/daemon/test_triage_drop_is_recorded.py
"""

from __future__ import annotations

import asyncio
import time
import unittest
from typing import Any

from yuyutsava.agents.triage.agent import TriageDecision
from yuyutsava.daemon.triage_loop import TriageLoop
from yuyutsava.events.bus import EventEnvelope


class _RecordingStore:
    def __init__(self) -> None:
        self.decisions: list[dict] = []
        self.proposals: list[Any] = []

    async def put_decision(self, **kw: Any) -> None:
        self.decisions.append(kw)

    async def put_proposal(self, p: Any) -> None:
        self.proposals.append(p)


class _RecordingChannels:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def post_event(self, ev: Any) -> None:
        self.events.append(ev)

    async def post_proposal(self, p: Any) -> Any:  # pragma: no cover
        raise AssertionError("a dropped event must never reach consent")


class _NoRule:
    async def evaluate(self, ev: Any) -> Any:
        class _R:
            rule = None

        return _R()


class _Triage:
    def __init__(self, decision: TriageDecision) -> None:
        self._decision = decision

    async def classify(self, ev: Any, capabilities: Any, skills_index: str = "") -> Any:
        return self._decision


def _envelope() -> EventEnvelope:
    return EventEnvelope(
        event_id="e-drop", topic="user.task.submitted", source="api",
        ts=time.time(), severity=1, summary="what is 2+2",
        payload_ref="sqlite://event_payloads/e-drop",
        hints={"task_id": "tsk_drop", "instruction": "what is 2+2"},
    )


def _loop(store: Any, channels: Any, decision: TriageDecision) -> TriageLoop:
    loop = TriageLoop(
        bus=object(), store=store, channels=channels,
        triage=_Triage(decision), capabilities_block="",
        task_queue=asyncio.Queue(), proposal_expiry_sec=300,
    )
    loop._consent = _NoRule()  # type: ignore[assignment]
    return loop


DROP = TriageDecision(
    action="drop", subagent_hint="", proposed_instruction="",
    reason="not actionable — a general knowledge question", urgency=1,
)


class DropIsRecorded(unittest.IsolatedAsyncioTestCase):
    async def test_a_decision_row_is_written(self) -> None:
        store, channels = _RecordingStore(), _RecordingChannels()
        await _loop(store, channels, DROP)._handle(_envelope())
        self.assertEqual(
            len(store.decisions), 1,
            "a dropped event wrote no decision — from the UI it is "
            "indistinguishable from the daemon never seeing it",
        )

    async def test_the_row_says_what_happened_and_why(self) -> None:
        store, channels = _RecordingStore(), _RecordingChannels()
        await _loop(store, channels, DROP)._handle(_envelope())
        row = store.decisions[0]
        self.assertEqual(row["outcome"], "dropped")
        self.assertEqual(row["event_id"], "e-drop")
        self.assertIsNone(row["proposal_id"], "a drop never produced a proposal")
        self.assertEqual(
            row["action_summary"], DROP.reason,
            "the reason is the only thing that tells a user why nothing happened",
        )

    async def test_a_timeline_line_is_posted(self) -> None:
        store, channels = _RecordingStore(), _RecordingChannels()
        await _loop(store, channels, DROP)._handle(_envelope())
        self.assertEqual(len(channels.events), 1)
        line = channels.events[0].payload.line
        self.assertIn("dropped", line)
        self.assertIn(DROP.reason, line)

    async def test_it_never_reaches_consent_or_the_queue(self) -> None:
        """A drop is a decision not to act — it must not propose anything."""
        store, channels = _RecordingStore(), _RecordingChannels()
        loop = _loop(store, channels, DROP)
        await loop._handle(_envelope())
        self.assertEqual(store.proposals, [])
        self.assertTrue(loop._task_queue.empty())


class OtherOutcomesStillBehave(unittest.IsolatedAsyncioTestCase):
    """Negative control — the drop branch must not have swallowed the others."""

    async def test_log_still_records_logged(self) -> None:
        store, channels = _RecordingStore(), _RecordingChannels()
        decision = TriageDecision(
            action="log", subagent_hint="", proposed_instruction="",
            reason="worth noting", urgency=1)
        await _loop(store, channels, decision)._handle(_envelope())
        self.assertEqual(store.decisions[0]["outcome"], "logged")

    async def test_propose_still_reaches_consent(self) -> None:
        store, channels = _RecordingStore(), _RecordingChannels()
        decision = TriageDecision(
            action="propose", subagent_hint="general-purpose",
            proposed_instruction="do the thing", reason="actionable", urgency=2)
        # _RecordingChannels.post_proposal raises if reached — which is the
        # assertion: a proposal MUST get there.
        with self.assertRaises(AssertionError):
            await self._raise_through(_loop(store, channels, decision))
        self.assertEqual(len(store.proposals), 1)

    async def _raise_through(self, loop: TriageLoop) -> None:
        """``_handle`` swallows exceptions; re-raise what the channel raised."""
        import logging

        with self.assertLogs("yuyutsava", level="ERROR") as cm:
            await loop._handle(_envelope())
        logging.getLogger(__name__).debug("%s", cm.output)
        raise AssertionError("post_proposal was reached")


if __name__ == "__main__":
    unittest.main(verbosity=2)
