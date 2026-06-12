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


# ---------------------------------------------------------------------------
# Async (background) subagent payloads
# ---------------------------------------------------------------------------
# Emitted by ``yuyutsava.async_subagents.watcher.AsyncTaskHealthWatcher`` as
# background tasks make progress. Rendered by the Electron renderer's
# Background Tasks panel and used by the CLI Mode-1 bridge to print inline
# status banners between user turns.


@dataclass(frozen=True)
class AsyncTaskStartedPayload:
    """A new background subagent task was launched."""

    task_id: str
    agent_name: str
    instruction_preview: str
    ts: float
    kind: Literal["async_task_started"] = "async_task_started"


@dataclass(frozen=True)
class AsyncTaskProgressPayload:
    """A status change or log line for a known background task.

    ``kind_hint`` distinguishes "status_change" (e.g. running->awaiting_user)
    from "log" (free-form progress text). Free text avoids a second Literal
    explosion; the consumer can format accordingly.
    """

    task_id: str
    agent_name: str
    kind_hint: str
    text: str
    ts: float
    kind: Literal["async_task_progress"] = "async_task_progress"


@dataclass(frozen=True)
class AsyncTaskAwaitingUserPayload:
    """The background graph hit ``interrupt()`` and is waiting for a user reply."""

    task_id: str
    agent_name: str
    ask_id: str
    title: str
    ts: float
    kind: Literal["async_task_awaiting_user"] = "async_task_awaiting_user"


@dataclass(frozen=True)
class AsyncTaskCompletedPayload:
    """The background task reached a terminal status."""

    task_id: str
    agent_name: str
    ok: bool
    summary: str
    duration_sec: float
    ts: float
    kind: Literal["async_task_completed"] = "async_task_completed"


ChannelPayload = (
    LogPayload
    | TokenPayload
    | ToolCallPayload
    | ToolResultPayload
    | TimelinePayload
    | HttpLogPayload
    | AsyncTaskStartedPayload
    | AsyncTaskProgressPayload
    | AsyncTaskAwaitingUserPayload
    | AsyncTaskCompletedPayload
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

    ``task_id`` / ``session_id`` scope the event to one orchestrator run:
    the orchestrator loop tags everything it emits so the SSE stream can be
    filtered per task (``/stream?task_id=``) and the WebHub can keep a
    per-task replay ring. ``None`` for unscoped events (boot notices, HTTP
    logs, source chatter).
    """

    payload: ChannelPayload
    task_id: str | None = None
    session_id: str | None = None

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
    """Fan-out for events; origin-aware then first-available for asks/proposals.

    Routing for ``post_ask`` / ``post_proposal``:
      1. If ``session_origin`` is set and the ask carries a ``session_id`` that
         maps to a connected channel, try that channel first. This lets a
         CLI-issued task get its HITL prompt back in the same CLI session
         even when the Electron renderer is also live.
      2. Otherwise fall back to ``primary_name``-first (default: ``web``).
      3. Channels that raise ``NotImplementedError`` are skipped.

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
    # Optional ``SessionOriginMap`` — see yuyutsava.async_subagents.session_origin.
    # Typed as ``Any`` here to avoid the daemon-side channels module importing
    # async_subagents (which pulls langgraph_api). The duck-typed contract is
    # ``.get(session_id) -> channel_name | None``.
    session_origin: Any | None = None

    def register(self, channel: UserChannel) -> bool:
        """Add ``channel`` to the fan-out. Idempotent by ``channel.name``.

        Returns ``True`` when newly added, ``False`` when a channel with
        that name is already registered (the existing instance wins —
        callers that need the live instance should :meth:`find` it).
        """
        if self.find(channel.name) is not None:
            return False
        self.channels.append(channel)
        return True

    def unregister(self, name: str) -> UserChannel | None:
        """Remove and return the channel named ``name`` (None if absent).

        The caller owns any further teardown (``await channel.shutdown()``
        or a plugin's ``stop()``) — the router only stops fanning out to it.
        """
        ch = self.find(name)
        if ch is not None:
            self.channels.remove(ch)
        return ch

    def find(self, name: str) -> UserChannel | None:
        for c in self.channels:
            if c.name == name:
                return c
        return None

    async def post_event(self, ev: ChannelEvent) -> None:
        await asyncio.gather(
            *(c.post_event(ev) for c in self.channels),
            return_exceptions=True,
        )

    def _ordered_for_ask(self, *, prefer: str | None = None) -> list[UserChannel]:
        # 1. Origin channel (if connected) — placed first when supplied.
        origin = [c for c in self.channels if prefer and c.name == prefer]
        # 2. Primary channel (skip if it was the origin).
        primary = [
            c for c in self.channels
            if c.name == self.primary_name and not (prefer and c.name == prefer)
        ]
        # 3. Everything else.
        rest = [c for c in self.channels if c not in origin and c not in primary]
        return origin + primary + rest

    async def post_proposal(self, p: Proposal) -> ProposalDecision:
        for c in self._ordered_for_ask():
            try:
                return await c.post_proposal(p)
            except NotImplementedError:
                continue
        logger.error("No channel could handle proposal %s; defaulting to skip", p.proposal_id)
        return ProposalDecision(decision="skip")

    async def post_ask(self, a: AskPrompt) -> str:
        prefer = None
        if self.session_origin is not None:
            try:
                prefer = self.session_origin.get(a.session_id)
            except Exception:  # noqa: BLE001
                logger.debug("session_origin.get failed", exc_info=True)
                prefer = None
        for c in self._ordered_for_ask(prefer=prefer):
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
