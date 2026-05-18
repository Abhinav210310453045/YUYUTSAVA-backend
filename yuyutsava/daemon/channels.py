"""
User-facing communication abstraction.

The daemon talks to the user only through ``UserChannel`` implementations.
Each channel handles three flavours of message:

- ``post_event``: token streams, tool calls, status updates (broadcast to all
  enabled channels).
- ``post_proposal``: Tier-1 consent — "I want to do X, may I?". Routed to
  one channel based on originator policy; awaits a user decision.
- ``post_ask``: Tier-2 tool-level interrupt — "permission for tr_write_file".
  Same routing as proposals; reuses the existing TaskRunner interrupt
  semantics, only the transport changes.

The web UI is the canonical surface; terminal is a fallback. Voice and
push notifications plug in later as additional ``UserChannel`` impls
without changing the orchestrator or any subagent.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from yuyutsava.events.store import Proposal

logger = logging.getLogger("yuyutsava.daemon.channels")


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelEvent:
    """Token / status / log event broadcast to all channels.

    ``kind`` semantics (kept small for the SSE payload):

    - ``log``: free-text status (one short line).
    - ``token``: streaming AI text. ``data["text"]`` holds the chunk.
    - ``tool_call``: the model called a tool. ``data`` has ``name`` and ``args``.
    - ``tool_result``: a tool returned. ``data`` has ``name`` and ``preview``.
    - ``timeline``: a structured timeline row appended (event/proposal/decision).
    """

    kind: Literal["log", "token", "tool_call", "tool_result", "timeline"]
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AskPrompt:
    """A Tier-2 interrupt rendered for the user (tool-level permission)."""

    ask_id: str
    title: str
    body: str
    options: list[str]              # e.g. ["approve","reject"] or free-text if empty
    interrupt_value: dict[str, Any]  # raw langgraph interrupt for caller context
    session_id: str | None = None    # thread_id of the originating run (HITL scoping)
    agent_path: str | None = None    # e.g. "orchestrator/file_organizer#1" — who's asking


@dataclass(frozen=True)
class ProposalDecision:
    """User response to a Tier-1 ``Proposal``."""

    decision: Literal["approve", "approve_remember", "modify", "skip", "skip_remember", "expired"]
    edited_instruction: str | None = None


# ---------------------------------------------------------------------------
# UserChannel ABC
# ---------------------------------------------------------------------------


class UserChannel(ABC):
    """One way to talk to the user. Channels are free to no-op irrelevant calls."""

    name: str = "unnamed"

    @abstractmethod
    async def post_event(self, ev: ChannelEvent) -> None: ...

    @abstractmethod
    async def post_proposal(self, p: Proposal) -> ProposalDecision:
        """Show a proposal and **block** until the user responds (or it expires)."""

    @abstractmethod
    async def post_ask(self, a: AskPrompt) -> str:
        """Show a Tier-2 ask and block until the user responds. Return the response string."""

    async def shutdown(self) -> None:
        """Optional cleanup hook."""


# ---------------------------------------------------------------------------
# ChannelRouter
# ---------------------------------------------------------------------------


@dataclass
class ChannelRouter:
    """Fan-out for events; first-available routing for asks/proposals.

    TODO(phase2-§3.4): when ``yuyutsava/daemon/push_channel.py`` (pync-backed
    macOS notifications) is added for ``--no-ui`` mode, this constructor
    must assert that ``PushChannel`` is not present alongside ``WebChannel``.
    The Electron renderer already shows focus-aware OS banners via
    ``notify:show`` IPC; pairing both would double-banner the user. Pick
    one channel based on ``DaemonConfig.headless`` at boot — never both.
    See PHASE_2_PLAN §3.4 and the new-risks section.
    """

    channels: list[UserChannel] = dataclasses.field(default_factory=list)
    primary_name: str = "web"  # tried first for asks/proposals

    async def post_event(self, ev: ChannelEvent) -> None:
        await asyncio.gather(
            *(c.post_event(ev) for c in self.channels),
            return_exceptions=True,
        )

    def _ordered_for_ask(self) -> list[UserChannel]:
        primary = [c for c in self.channels if c.name == self.primary_name]
        rest = [c for c in self.channels if c.name != self.primary_name]
        return primary + rest

    async def post_proposal(self, p: Proposal) -> ProposalDecision:
        for c in self._ordered_for_ask():
            try:
                return await c.post_proposal(p)
            except NotImplementedError:
                continue
        logger.error("No channel could handle proposal %s; defaulting to skip", p.proposal_id)
        return ProposalDecision(decision="skip")

    async def post_ask(self, a: AskPrompt) -> str:
        for c in self._ordered_for_ask():
            try:
                return await c.post_ask(a)
            except NotImplementedError:
                continue
        logger.error("No channel could handle ask %s; defaulting to reject", a.ask_id)
        return "reject"

    async def shutdown(self) -> None:
        for c in self.channels:
            try:
                await c.shutdown()
            except Exception:
                logger.exception("channel %s shutdown failed", c.name)
