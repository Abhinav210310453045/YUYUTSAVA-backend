"""User-initiated task submission — the daemon's "front door" for work.

Two trust levels, per the master plan:

- ``submit_direct``: the submitter *is* the user (authenticated API, CLI,
  later the mobile app). User-initiated equals implicit Tier-1 consent, so
  an auto-approved Proposal is recorded for the audit trail and the
  ``OrchestratorTask`` goes straight onto the queue. Tier-2 tool asks still
  fire normally during the run.
- ``submit_via_triage``: lower-trust origins. The instruction is published
  on the EventBus as a ``user.task.submitted`` event and flows through the
  normal TriageLoop classify → Proposal → user-decision path. The minted
  ``task_id`` rides the event hints so the eventual ``OrchestratorTask``
  joins back to the same registry row.

Both modes mint a ``tsk_<ULID>``, persist a TaskRegistry row (``queued``),
and write an ``event_payloads`` row so the decisions audit trail joins work
exactly like organic events. A triage-mode task the user skips (or that
triage drops) stays ``queued`` forever — coarse v1, recorded in the plan.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time

from ulid import ULID

from yuyutsava.daemon.task_registry import TaskRegistry
from yuyutsava.daemon.triage_loop import OrchestratorTask
from yuyutsava.events.bus import EventBus, EventEnvelope
from yuyutsava.storage.events import Proposal, Store

logger = logging.getLogger("yuyutsava.daemon.task_submission")

SUBMITTED_TOPIC = "user.task.submitted"

# Direct submissions bypass triage, so there is no LLM picking a subagent;
# the orchestrator's own delegation decides. general-purpose is the hint
# that constrains it least.
_DIRECT_SUBAGENT_HINT = "general-purpose"
_DIRECT_URGENCY = 2  # "notable" — a human explicitly asked for this


class TaskSubmissionService:
    """Mints task ids, persists registry rows, and feeds the daemon."""

    def __init__(
        self,
        *,
        registry: TaskRegistry,
        task_queue: asyncio.Queue[OrchestratorTask],
        store: Store,
        bus: EventBus,
        proposal_expiry_sec: int = 300,
        complexity_scorer: object | None = None,  # core.model_router.ComplexityScorer
    ) -> None:
        self._registry = registry
        self._queue = task_queue
        self._store = store
        self._bus = bus
        self._proposal_expiry_sec = proposal_expiry_sec
        self._scorer = complexity_scorer

    async def submit_direct(
        self,
        instruction: str,
        *,
        origin: str = "api",
        session_hint: str | None = None,
        complexity: int | None = None,
    ) -> str:
        """Trusted submission: auto-approved proposal + immediate enqueue.

        ``session_hint`` (a thread/session id of the submitting surface) is
        stored on the proposal's ``session_id`` so origin-aware ask routing
        (``ChannelRouter.session_origin``) can prefer the submitting channel
        for Tier-2 prompts.

        ``complexity`` (Phase 4): direct submissions skip triage, so no LLM
        self-scores them. A client-supplied 1-5 override wins; otherwise one
        short light-tier scoring call runs when a scorer is wired (model
        routing enabled). The scorer never raises — any failure scores 3.
        """
        instruction = instruction.strip()
        if not instruction:
            raise ValueError("instruction must not be empty")
        if complexity is not None:
            complexity = max(1, min(int(complexity), 5))
        elif self._scorer is not None:
            complexity = await self._scorer.score(instruction)

        task_id = self._registry.mint_task_id()
        event_id = str(ULID())
        now = time.time()

        await self._store.put_event_payload(
            event_id=event_id, topic=SUBMITTED_TOPIC, ts=now,
            payload={"instruction": instruction, "origin": origin,
                     "task_id": task_id, "mode": "direct"},
        )
        proposal = Proposal.new(
            event_id=event_id,
            topic=SUBMITTED_TOPIC,
            summary=instruction[:120],
            proposed=instruction,
            subagent=_DIRECT_SUBAGENT_HINT,
            urgency=_DIRECT_URGENCY,
            expiry_sec=self._proposal_expiry_sec,
            session_id=session_hint,
        )
        approved = dataclasses.replace(proposal, status="approved")
        await self._store.put_proposal(approved)
        await self._store.put_decision(
            proposal_id=approved.proposal_id, event_id=event_id,
            outcome="user_submitted",
            action_summary=f"direct task via {origin}: {instruction[:160]}",
            session_id=session_hint,
        )

        await self._registry.create(
            task_id=task_id, origin=origin, instruction=instruction,
            session_hint=session_hint, complexity=complexity,
        )
        await self._queue.put(OrchestratorTask(
            proposal_id=approved.proposal_id, event_id=event_id,
            topic=SUBMITTED_TOPIC, summary=instruction[:120],
            instruction=instruction, subagent_hint=_DIRECT_SUBAGENT_HINT,
            urgency=_DIRECT_URGENCY, task_id=task_id,
            complexity=complexity if complexity is not None else 3,
        ))
        logger.info("task submitted (direct, %s): %s", origin, task_id)
        return task_id

    async def submit_via_triage(self, instruction: str, *, origin: str = "api") -> str:
        """Lower-trust submission: publish onto the bus, let triage decide.

        The TriageLoop consumes the event like any other source's, so the
        instruction goes through LLM classification and Tier-1 consent. The
        registry row stays ``queued`` until (and unless) the approved
        OrchestratorTask reaches the orchestrator loop.
        """
        instruction = instruction.strip()
        if not instruction:
            raise ValueError("instruction must not be empty")

        task_id = self._registry.mint_task_id()
        event_id = str(ULID())
        now = time.time()

        await self._store.put_event_payload(
            event_id=event_id, topic=SUBMITTED_TOPIC, ts=now,
            payload={"instruction": instruction, "origin": origin,
                     "task_id": task_id, "mode": "triage"},
        )
        await self._registry.create(
            task_id=task_id, origin=origin, instruction=instruction,
        )
        await self._bus.publish(EventEnvelope(
            event_id=event_id,
            topic=SUBMITTED_TOPIC,
            source=origin,
            ts=now,
            severity=1,
            summary=instruction[:120],
            payload_ref=f"sqlite://event_payloads/{event_id}",
            # hints stay small (they land in the triage prompt); the full
            # instruction is in event_payloads.
            hints={"task_id": task_id, "instruction": instruction[:500]},
        ))
        logger.info("task submitted (triage, %s): %s", origin, task_id)
        return task_id
