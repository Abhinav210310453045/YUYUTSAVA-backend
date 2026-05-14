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
import fnmatch
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ulid import ULID

from yuyutsava.agents.orchestrator.capabilities import render_capabilities_block
from yuyutsava.agents.triage.agent import TriageAgent, TriageDecision
from yuyutsava.daemon.channels import (
    ChannelEvent, ChannelRouter, ProposalDecision,
)
from yuyutsava.events.bus import EventBus, EventEnvelope
from yuyutsava.events.store import ConsentRule, Proposal, Store
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

    def render_to_message(self) -> str:
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
        capabilities_block: str,
        task_queue: asyncio.Queue[OrchestratorTask],
        proposal_expiry_sec: int,
        max_concurrent: int = 4,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self._bus = bus
        self._store = store
        self._channels = channels
        self._triage = triage
        self._capabilities_block = capabilities_block
        self._task_queue = task_queue
        self._proposal_expiry_sec = proposal_expiry_sec
        self._sem = asyncio.Semaphore(max_concurrent)
        self._workers: set[asyncio.Task[None]] = set()
        self._skill_registry = skill_registry

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
                rule = self._match_rule(ev)
                if rule and rule["decision"] == "auto_skip":
                    await self._store.put_decision(
                        proposal_id=None, event_id=ev.event_id,
                        outcome="skipped_by_rule",
                        action_summary=f"rule:{rule['rule_id']}",
                    )
                    await self._channels.post_event(ChannelEvent(
                        kind="timeline",
                        data={"ts": ev.ts, "line": f"auto-skipped: {ev.summary}",
                              "cls": "event-decision-skipped"},
                    ))
                    return

                if rule and rule["decision"] == "auto_approve":
                    # No triage call; synthesise a proper instruction from event
                    # metadata so the subagent gets an unambiguous path, not just
                    # the raw summary string.
                    proposed_instruction = _build_fs_instruction(ev)
                    decision = TriageDecision(
                        action="propose",
                        subagent_hint=rule.get("subagent_hint") or "file-organizer",
                        proposed_instruction=proposed_instruction,
                        reason="auto_approve rule",
                        urgency=1,
                    )
                    await self._auto_approve_path(ev, decision, rule_id=rule["rule_id"])
                    return

                # 2. LLM triage — include skills index if available.
                skills_index = (
                    self._skill_registry.index_block(agent="triage")
                    if self._skill_registry else ""
                )
                decision = await self._triage.classify(
                    ev, self._capabilities_block, skills_index=skills_index
                )

                if decision.action == "drop":
                    return
                if decision.action == "log":
                    await self._store.put_decision(
                        proposal_id=None, event_id=ev.event_id,
                        outcome="logged", action_summary=decision.reason,
                    )
                    await self._channels.post_event(ChannelEvent(
                        kind="timeline",
                        data={"ts": ev.ts, "line": f"logged: {ev.summary} — {decision.reason}",
                              "cls": ""},
                    ))
                    return

                # 3. action == "propose": Tier-1 consent.
                proposal = Proposal.new(
                    event_id=ev.event_id,
                    topic=ev.topic,
                    summary=ev.summary,
                    proposed=decision.proposed_instruction or ev.summary,
                    subagent=decision.subagent_hint or "file-organizer",
                    urgency=decision.urgency,
                    expiry_sec=self._proposal_expiry_sec,
                )
                await self._store.put_proposal(proposal)

                user_decision: ProposalDecision = await self._channels.post_proposal(proposal)
                await self._handle_user_decision(ev, proposal, user_decision)
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
            subagent=decision.subagent_hint or "file-organizer",
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
            kind="timeline",
            data={"ts": ev.ts, "line": f"auto-approved: {approved.proposed}",
                  "cls": "event-decision-approved"},
        ))
        await self._task_queue.put(OrchestratorTask(
            proposal_id=approved.proposal_id, event_id=ev.event_id,
            topic=ev.topic, summary=ev.summary,
            instruction=approved.proposed, subagent_hint=approved.subagent,
            urgency=approved.urgency,
        ))

    async def _handle_user_decision(
        self, ev: EventEnvelope, proposal: Proposal, ud: ProposalDecision,
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

    # --- rule matching --------------------------------------------------

    def _match_rule(self, ev: EventEnvelope) -> dict[str, Any] | None:
        rules = self._store.list_consent_rules()
        now = time.time()
        for rule in rules:
            if rule.get("expires_ts") and rule["expires_ts"] < now:
                continue
            if not fnmatch.fnmatchcase(ev.topic, rule["topic_glob"]):
                continue
            try:
                m = json.loads(rule["match_json"])
            except Exception:
                continue
            if not self._match_predicate(ev, m):
                continue
            return rule
        return None

    @staticmethod
    def _match_predicate(ev: EventEnvelope, predicate: dict[str, Any]) -> bool:
        # Dotted hint paths: "hints.ext" => ev.hints["ext"]
        for key, expected in predicate.items():
            if key == "topic":
                if not fnmatch.fnmatchcase(ev.topic, str(expected)):
                    return False
                continue
            if key.startswith("hints."):
                hint_key = key.split(".", 1)[1]
                actual = ev.hints.get(hint_key, "")
                if not fnmatch.fnmatchcase(str(actual), str(expected)):
                    return False
                continue
            if key in ("ext", "kind"):
                actual = ev.hints.get(key, "")
                if not fnmatch.fnmatchcase(str(actual), str(expected)):
                    return False
                continue
        return True
