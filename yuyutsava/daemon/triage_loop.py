"""
Triage loop: bus consumer → consent-rule lookup → triage LLM → Tier-1 proposal
→ user decision → OrchestratorTask enqueue.

Each event is handled in its own asyncio task so a slow human decision on
one proposal doesn't stall classification of the next event. A semaphore
caps concurrency to keep storms from blowing the LLM budget.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ulid import ULID

from yuyutsava.agents.triage.agent import TriageAgent, TriageDecision
from yuyutsava.daemon.channels import (
    ChannelEvent, ChannelRouter, ProposalDecision, TimelinePayload,
)
from yuyutsava.daemon.consent import ConsentEvaluator
from yuyutsava.events.bus import EventBus, EventEnvelope
from yuyutsava.storage.events import ConsentRule, Proposal, Store
from yuyutsava.skills.registry import SkillRegistry

logger = logging.getLogger("yuyutsava.daemon.triage_loop")


# ---------------------------------------------------------------------------
# OrchestratorTask
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrchestratorTask:
    """Approved-by-user instruction that the orchestrator should run on."""

    proposal_id: str
    event_id: str
    topic: str
    summary: str
    instruction: str        # what the orchestrator/subagent should do
    subagent_hint: str
    urgency: int
    # TaskRegistry join key (``tsk_<ULID>``). Set by TaskSubmissionService
    # for user-submitted tasks (carried through triage via event hints);
    # empty for organic events — the orchestrator loop mints one so every
    # run is visible to ``GET /tasks``.
    task_id: str = ""
    # 1-5 complexity score (Phase 4 model routing). Triage self-scores
    # organic events; direct submissions are scored at submit time.
    complexity: int = 3
    # Durable resume after a daemon reload: when set, this task is a re-run of
    # an interrupted task and the orchestrator loop reuses this persisted
    # thread_id (rather than minting a fresh one) so the graph continues from
    # its last checkpoint. Empty/None for normal first runs.
    resume_thread_id: str | None = None
    # Task kind. "normal" = an instruction to act on. "subagent_completed" = a
    # wake-up telling the master a background subagent finished, so it can plan
    # next steps and report back to the user. See ``completion``/``parent_thread_id``.
    kind: str = "normal"
    # Originating channel name ("cli"/"web"/…) for origin-aware HITL routing.
    origin: str = ""
    # For ``subagent_completed`` wake-ups: append this turn to the conversation
    # thread that launched the task (so the master keeps full context) instead of
    # minting a fresh thread. None → fresh thread (message is self-contained).
    parent_thread_id: str | None = None
    # For ``subagent_completed``: {task_id, agent_name, ok, summary}.
    completion: dict | None = None

    def render_to_message(self) -> str:
        if self.kind == "subagent_completed" and self.completion:
            c = self.completion
            ok = c.get("ok")
            verdict = "succeeded" if ok else "did NOT succeed"
            # The summary is already compacted upstream (watcher.compact_error),
            # so this stays a short line and never dumps a raw traceback here.
            decision = (
                "Tell the user, in your own words, what happened and decide whether "
                "any follow-up is needed. Do NOT start new work unless it is clearly "
                "required to finish what the user originally asked for."
            )
            if not ok:
                decision = (
                    "Decide the next step yourself: either relaunch the task with "
                    "start_async_task (if it's worth retrying), or report the failure "
                    "to the user in your own words. Use check the task logs only if "
                    "you need more detail than the summary above."
                )
            # Showable artifacts the subagent produced (via artifact_create): tell
            # the master the exact ids and to re-embed the relevant ones inline.
            artifacts = c.get("artifacts") or []
            artifacts_block = ""
            if ok and artifacts:
                artifacts_block = (
                    f"  artifacts: {', '.join(artifacts)}\n"
                    f"The subagent produced the artifact(s) above for the user. Show the "
                    f"relevant one(s) inline by calling artifact_show(<id>) for each — do "
                    f"this BEFORE writing your reply so they render in it.\n"
                )
            return (
                f"[background-task-update] A background subagent you started has "
                f"finished — this is a system notification, not a new user request.\n"
                f"  agent: {c.get('agent_name', '?')}\n"
                f"  task_id: {c.get('task_id', '?')}\n"
                f"  result: {verdict}\n"
                f"  summary: {c.get('summary', '') or '(no summary)'}\n"
                f"{artifacts_block}\n"
                f"{decision}"
            )
        return (
            f"[event] {self.topic} | event_id={self.event_id}\n"
            f"Summary: {self.summary}\n"
            f"User-approved proposal: {self.instruction}\n"
            f"Suggested subagent: {self.subagent_hint}\n"
        )


# ---------------------------------------------------------------------------
# Triage loop
# ---------------------------------------------------------------------------


def _build_fs_instruction(ev: "EventEnvelope") -> str:
    """Build a concrete move instruction for fs.changed events.

    Falls back to ev.summary for non-fs topics or when the path hint is absent.
    """
    if ev.topic != "fs.changed":
        return ev.summary
    path_str = ev.hints.get("path", "")
    if not path_str:
        return ev.summary
    filename = Path(path_str).name
    year = datetime.datetime.now().year
    home = os.path.expanduser("~")
    inbox = f"{home}/Documents/Inbox/{year}"
    return f"Move {path_str} to {inbox}/{filename}"


class TriageLoop:
    """Owns the bus subscription and concurrency budget."""

    def __init__(
        self,
        *,
        bus: EventBus,
        store: Store,
        channels: ChannelRouter,
        triage: TriageAgent,
        capabilities_block: "str | Callable[[], str]",
        task_queue: asyncio.Queue[OrchestratorTask],
        proposal_expiry_sec: int,
        max_concurrent: int = 4,
        skill_registry: SkillRegistry | None = None,
        runtime_settings: object | None = None,
    ) -> None:
        self._bus = bus
        self._store = store
        self._channels = channels
        self._triage = triage
        # Callable, not a string: the roster changes at runtime (the user can
        # switch a dedicated subagent off), and a block captured at boot would
        # keep offering an agent that is no longer there.
        self._capabilities_block = capabilities_block
        self._runtime_settings = runtime_settings
        self._task_queue = task_queue
        self._proposal_expiry_sec = proposal_expiry_sec
        self._sem = asyncio.Semaphore(max_concurrent)
        self._workers: set[asyncio.Task[None]] = set()
        self._skill_registry = skill_registry
        self._consent = ConsentEvaluator(store)

    def _capabilities(self) -> str:
        block = self._capabilities_block
        return block() if callable(block) else block

    def _hint(self, hint: str | None) -> str:
        """Resolve a triage subagent hint against the *current* roster.

        A hint naming a switched-off subagent (or the historical
        ``file-organizer`` default when that one is off) falls back to
        ``general-purpose``, which is always available — otherwise the proposal
        would be approved and then refused downstream by the gate middleware.
        """
        name = (hint or "").strip() or "file-organizer"
        if self._runtime_settings is None:
            return name
        try:
            if self._runtime_settings.subagents().is_enabled(name):
                return name
        except Exception:  # noqa: BLE001 — never break triage on a toggle read
            return name
        logger.info("triage: subagent %s is off — routing to general-purpose", name)
        return "general-purpose"

    async def run(self, stop_event: asyncio.Event) -> None:
        sub = self._bus.subscribe("**")
        try:
            async for envelope in sub:
                if stop_event.is_set():
                    break
                t = asyncio.create_task(self._handle(envelope), name="triage-handler")
                self._workers.add(t)
                t.add_done_callback(self._workers.discard)
        finally:
            # Wait for in-flight handlers, but don't hang forever.
            if self._workers:
                await asyncio.wait(self._workers, timeout=5.0)

    async def _handle(self, ev: EventEnvelope) -> None:
        async with self._sem:
            try:
                # 1. consent_rules first — auto-approve / auto-skip without LLM.
                rule = (await self._consent.evaluate(ev)).rule
                if rule and rule.decision == "auto_skip":
                    await self._store.put_decision(
                        proposal_id=None, event_id=ev.event_id,
                        outcome="skipped_by_rule",
                        action_summary=f"rule:{rule.rule_id}",
                    )
                    await self._channels.post_event(ChannelEvent(
                        payload=TimelinePayload(
                            ts=ev.ts,
                            line=f"auto-skipped: {ev.summary}",
                            cls="event-decision-skipped",
                        ),
                    ))
                    return

                if rule and rule.decision == "auto_approve":
                    # No triage call; synthesise a proper instruction from event
                    # metadata so the subagent gets an unambiguous path, not just
                    # the raw summary string.
                    proposed_instruction = _build_fs_instruction(ev)
                    decision = TriageDecision(
                        action="propose",
                        subagent_hint=self._hint("file-organizer"),
                        proposed_instruction=proposed_instruction,
                        reason="auto_approve rule",
                        urgency=1,
                        # No LLM here; a rule-approved single-file move is the
                        # prompt's anchored complexity-1 example.
                        complexity=1,
                    )
                    await self._auto_approve_path(ev, decision, rule_id=rule.rule_id)
                    return

                # 2. LLM triage — include skills index if available.
                skills_index = (
                    self._skill_registry.index_block(agent="triage")
                    if self._skill_registry else ""
                )
                decision = await self._triage.classify(
                    ev, self._capabilities(), skills_index=skills_index
                )

                if decision.action == "drop":
                    return
                if decision.action == "log":
                    await self._store.put_decision(
                        proposal_id=None, event_id=ev.event_id,
                        outcome="logged", action_summary=decision.reason,
                    )
                    await self._channels.post_event(ChannelEvent(
                        payload=TimelinePayload(
                            ts=ev.ts,
                            line=f"logged: {ev.summary} — {decision.reason}",
                        ),
                    ))
                    return

                # 3. action == "propose": Tier-1 consent.
                proposal = Proposal.new(
                    event_id=ev.event_id,
                    topic=ev.topic,
                    summary=ev.summary,
                    proposed=decision.proposed_instruction or ev.summary,
                    subagent=self._hint(decision.subagent_hint),
                    urgency=decision.urgency,
                    expiry_sec=self._proposal_expiry_sec,
                )
                await self._store.put_proposal(proposal)

                user_decision: ProposalDecision = await self._channels.post_proposal(proposal)
                await self._handle_user_decision(
                    ev, proposal, user_decision, complexity=decision.complexity,
                )
            except Exception:
                logger.exception("triage handler crashed for %s", ev.event_id)

    async def _auto_approve_path(
        self, ev: EventEnvelope, decision: TriageDecision, *, rule_id: str
    ) -> None:
        proposal = Proposal.new(
            event_id=ev.event_id,
            topic=ev.topic,
            summary=ev.summary,
            proposed=decision.proposed_instruction or ev.summary,
            subagent=self._hint(decision.subagent_hint),
            urgency=decision.urgency,
            expiry_sec=self._proposal_expiry_sec,
        )
        # Immediately mark as approved (do not block on user).
        approved = dataclasses.replace(proposal, status="approved")
        await self._store.put_proposal(approved)
        await self._store.put_decision(
            proposal_id=approved.proposal_id, event_id=ev.event_id,
            outcome="auto_approved", action_summary=f"rule:{rule_id}",
        )
        await self._channels.post_event(ChannelEvent(
            payload=TimelinePayload(
                ts=ev.ts,
                line=f"auto-approved: {approved.proposed}",
                cls="event-decision-approved",
            ),
        ))
        await self._task_queue.put(OrchestratorTask(
            proposal_id=approved.proposal_id, event_id=ev.event_id,
            topic=ev.topic, summary=ev.summary,
            instruction=approved.proposed, subagent_hint=approved.subagent,
            urgency=approved.urgency,
            task_id=ev.hints.get("task_id", ""),
            complexity=decision.complexity,
        ))

    async def _handle_user_decision(
        self, ev: EventEnvelope, proposal: Proposal, ud: ProposalDecision,
        *, complexity: int = 3,
    ) -> None:
        outcome = ud.decision

        if ud.decision in ("skip", "skip_remember"):
            await self._store.put_decision(
                proposal_id=proposal.proposal_id, event_id=ev.event_id,
                outcome="skipped",
            )
            if ud.decision == "skip_remember":
                await self._add_consent_rule_for(ev, decision_kind="auto_skip")
            return

        if ud.decision == "expired":
            await self._store.put_decision(
                proposal_id=proposal.proposal_id, event_id=ev.event_id,
                outcome="expired",
            )
            return

        # approve / approve_remember / modify → enqueue an OrchestratorTask
        instruction = (
            ud.edited_instruction.strip()
            if ud.decision == "modify" and ud.edited_instruction else proposal.proposed
        )
        if ud.decision == "approve_remember":
            await self._add_consent_rule_for(ev, decision_kind="auto_approve")

        await self._store.put_decision(
            proposal_id=proposal.proposal_id, event_id=ev.event_id,
            outcome=outcome, action_summary=instruction[:200],
        )
        await self._task_queue.put(OrchestratorTask(
            proposal_id=proposal.proposal_id, event_id=ev.event_id,
            topic=ev.topic, summary=ev.summary,
            instruction=instruction, subagent_hint=proposal.subagent,
            urgency=proposal.urgency,
            task_id=ev.hints.get("task_id", ""),
            complexity=complexity,
        ))

    async def _add_consent_rule_for(
        self, ev: EventEnvelope, *, decision_kind: str,
    ) -> None:
        # Scope by directory + ext so "remember for this PDF" means files in
        # the same directory, not every PDF on the machine.
        match: dict[str, str] = {"topic": ev.topic, "ext": ev.hints.get("ext", "")}
        parent_dir = ev.hints.get("parent", "")
        if parent_dir:
            match["hints.parent"] = parent_dir
        rule = ConsentRule(
            rule_id=str(ULID()),
            topic_glob=ev.topic,
            match_json=json.dumps(match),
            decision=decision_kind,
            created_ts=time.time(),
            expires_ts=time.time() + 7 * 86400 if decision_kind == "auto_approve" else None,
        )
        await self._store.put_consent_rule(rule)
