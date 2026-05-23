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
from typing import Any, Literal, Mapping

from yuyutsava.storage.events import Proposal

logger = logging.getLogger("yuyutsava.daemon.channels")


# ---------------------------------------------------------------------------
# ChannelEvent payload variants
# ---------------------------------------------------------------------------
#
# Each payload variant is a frozen dataclass with its own ``kind`` literal
# discriminator. Consumers can match on the payload type (``match`` or
# ``isinstance``) instead of dict-fishing on free-form data.


@dataclass(frozen=True)
class LogPayload:
    """Free-text status message (one short line)."""

    text: str = ""
    kind: Literal["log"] = "log"


@dataclass(frozen=True)
class TokenPayload:
    """Streaming AI text chunk."""

    text: str = ""
    kind: Literal["token"] = "token"


@dataclass(frozen=True)
class ToolCallPayload:
    """The model called a tool."""

    name: str
    args: Mapping[str, Any] = field(default_factory=dict)
    kind: Literal["tool_call"] = "tool_call"


@dataclass(frozen=True)
class ToolResultPayload:
    """A tool returned (preview is already truncated)."""

    name: str
    preview: str = ""
    kind: Literal["tool_result"] = "tool_result"


@dataclass(frozen=True)
class TimelinePayload:
    """Structured timeline row (event/proposal/decision)."""

    line: str = ""
    cls: str = ""
    ts: float | None = None
    kind: Literal["timeline"] = "timeline"


@dataclass(frozen=True)
class HttpLogPayload:
    """HTTP access log entry (produced by the web middleware)."""

    method: str
    path: str
    status: int
    duration_ms: int
    ts: float
    kind: Literal["http_log"] = "http_log"


ChannelPayload = (
    LogPayload
    | TokenPayload
    | ToolCallPayload
    | ToolResultPayload
    | TimelinePayload
    | HttpLogPayload
)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelEvent:
    """Token / status / log event broadcast to all channels.

    The ``payload`` is a typed variant (see :data:`ChannelPayload`); each
    variant carries its own ``kind`` discriminator so consumers can pattern
    match on the payload type.
    """

    payload: ChannelPayload

    @property
    def kind(self) -> str:
        return self.payload.kind


@dataclass(frozen=True)
class AskPrompt:
    """A Tier-2 interrupt rendered for the user (tool-level permission)."""

    ask_id: str
    title: str
    body: str
    options: list[str]              # e.g. ["approve","reject"] or free-text if empty
    # ``interrupt_value`` is a raw passthrough from LangGraph's ``interrupt()``
    # — its shape is one of the variants documented in
    # :mod:`yuyutsava.models.interrupts` (plus the loose ``orchestrator_ask``
    # form the orchestrator builds inline). We treat it as opaque context
    # carried with the prompt; consumers parse it back into a typed model
    # when they need to. Typed as a read-only ``Mapping`` to discourage
    # mutation through this field.
    interrupt_value: Mapping[str, Any]
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
