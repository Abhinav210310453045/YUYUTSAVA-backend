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
import time
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


@dataclass(frozen=True)
class SystemMetricsPayload:
    """System load reading (Phase 5 ResourceMonitor).

    Emitted at most once per ``ResourceSettings.emit_sec`` while any
    orchestrator task is running, so mobile/web clients get a live load
    view over the existing SSE stream without polling /system/metrics.
    """

    cpu_pct: float
    mem_available_mb: float
    disk_free_gb: float
    ts: float
    kind: Literal["system_metrics"] = "system_metrics"


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
    | SystemMetricsPayload
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


# Where an ask came from — and therefore which view *owns* it. Rendering is a
# pure function of (owner, where the user is): the owning view shows it inline,
# everywhere else shows a notification plus an inbox entry, and an unfocused app
# gets the overlay. A permission prompt must never appear inside a different
# running session's path, which is why every ask carries its origin.
ASK_SURFACES = ("chat", "voice", "tinker", "background", "cli")


@dataclass(frozen=True)
class AskPrompt:
    """A Tier-2 interrupt rendered for the user (tool-level permission).

    One record, whatever raised it — a chat turn, a tinker card, a background
    subagent or the CLI. The ownership fields below are what let every surface
    agree on where it belongs without any of them guessing.
    """

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
    # ---- ownership: which surface raised this, and how to get back to it ----
    surface: str = "background"      # one of ASK_SURFACES
    thread_id: str | None = None     # conversation thread, when a conversation owns it
    card_id: str | None = None       # TODO card, for tinker asks
    task_id: str | None = None       # background task id, for bg asks
    agent_label: str | None = None   # short human name ("TinkerAgent", "file-organizer")
    # ``interrupt_id`` is LangGraph's own id for this interrupt (the ``it_id``
    # collected in core/streaming.py). Needed so a resume after a daemon
    # restart maps the reply back onto the right interrupt when a turn is
    # blocked on several at once.
    interrupt_id: str | None = None
    created_ts: float = field(default_factory=time.time)

    def to_wire_dict(self) -> dict[str, Any]:
        """The full record every client surface renders from.

        ``interrupt_value`` rides along deliberately: the collapsed summary
        (title/body/options from ``interrupt_format``) is not enough to show
        the full command, every path, and the risk/zone that the expanded card
        promises — and dropping it here was exactly why clients couldn't.
        """
        return {
            "ask_id": self.ask_id,
            "title": self.title,
            "body": self.body,
            "options": list(self.options),
            "session_id": self.session_id,
            "agent_path": self.agent_path,
            "surface": self.surface,
            "thread_id": self.thread_id,
            "card_id": self.card_id,
            "task_id": self.task_id,
            "agent_label": self.agent_label,
            "interrupt_id": self.interrupt_id,
            "created_ts": self.created_ts,
            "interrupt_value": dict(self.interrupt_value or {}),
        }


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

    # Whether this channel can be shown an ask *concurrently* with the others
    # and abandoned cleanly when somebody else answers first. True for surfaces
    # that park an ``asyncio.Future`` (web, cli-remote) — those all get every
    # ask and the first answer wins. False for surfaces that block a thread on
    # stdin: :class:`TerminalChannel` reads through ``asyncio.to_thread(input)``,
    # which cannot actually be cancelled, so fanning out to it would leak a
    # blocked thread per ask and spam a daemon's stderr with prompts nobody is
    # reading. Those stay the sequential fallback for a headless daemon.
    broadcast_asks: bool = False

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
    # ``daemon.ask_registry.AskRegistry`` — persists every ask before it is
    # broadcast and marks it resolved on answer, so an ask survives a dropped
    # frame or a daemon restart. Duck-typed (``record`` / ``resolve``) and
    # optional so headless/test routers need no storage.
    ask_registry: Any | None = None

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
        """Show an ask on every surface that can hold one; first answer wins.

        Previously this picked exactly ONE channel, which is what made an ask
        invisible everywhere except wherever it happened to land — the whole
        point of the ask being a permission prompt is that you can grant it
        from wherever you are. Now every cancel-safe surface (see
        ``UserChannel.broadcast_asks``) gets it simultaneously and the losers
        are cancelled; their ``finally`` blocks broadcast ``ask_resolved``, so
        the cards clear in sync.
        """
        # Durable first: the row must exist before anyone can see the ask, so a
        # frame dropped on the wire (WebHub.broadcast drops on QueueFull) is
        # still recoverable through GET /asks.
        if self.ask_registry is not None:
            try:
                await self.ask_registry.record(a)
            except Exception:  # noqa: BLE001
                logger.debug("ask registry record failed", exc_info=True)

        prefer = None
        if self.session_origin is not None:
            try:
                prefer = self.session_origin.get(a.session_id)
            except Exception:  # noqa: BLE001
                logger.debug("session_origin.get failed", exc_info=True)
                prefer = None
        ordered = self._ordered_for_ask(prefer=prefer)
        fanout = [c for c in ordered if c.broadcast_asks]

        answer: str | None = None
        try:
            if fanout:
                answer = await self._race_ask(a, fanout)
            if answer is None:
                # No broadcast-capable surface answered (or none exists —
                # headless daemon): fall back to the historical
                # first-accepting-channel walk so a terminal-only daemon still
                # prompts.
                for c in ordered:
                    if c.broadcast_asks:
                        continue
                    try:
                        answer = await c.post_ask(a)
                        break
                    except NotImplementedError:
                        continue
        except asyncio.CancelledError:
            # The asking turn was cancelled (Stop button, barge-in) — on either
            # path. Nobody is waiting for this answer any more, so retire the
            # record: leaving it 'pending' would strand a card in the Inbox
            # that can never be answered and whose agent no longer exists.
            # Detached because we are unwinding a cancellation and must not
            # await here.
            if self.ask_registry is not None:
                asyncio.create_task(
                    self.ask_registry.resolve(a.ask_id, "", status="cancelled")
                )
            raise
        if answer is None:
            logger.error("No channel could handle ask %s; defaulting to reject", a.ask_id)
            answer = "reject"

        if self.ask_registry is not None:
            try:
                await self.ask_registry.resolve(a.ask_id, answer)
            except Exception:  # noqa: BLE001
                logger.debug("ask registry resolve failed", exc_info=True)
        return answer

    async def _race_ask(self, a: AskPrompt, channels: list[UserChannel]) -> str | None:
        """Await every channel at once; return the first real answer, or None.

        ``None`` means every candidate declined (``NotImplementedError``) or
        failed — the caller then falls back to the sequential path.
        """
        tasks = {
            asyncio.create_task(c.post_ask(a), name=f"ask:{c.name}:{a.ask_id}"): c
            for c in channels
        }
        answer: str | None = None
        try:
            while tasks:
                done, _ = await asyncio.wait(
                    tasks.keys(), return_when=asyncio.FIRST_COMPLETED
                )
                for t in done:
                    channel = tasks.pop(t)
                    try:
                        answer = t.result()
                    except asyncio.CancelledError:
                        continue
                    except NotImplementedError:
                        continue          # this surface can't show asks
                    except Exception:     # noqa: BLE001
                        logger.warning(
                            "channel %s failed showing ask %s",
                            channel.name, a.ask_id, exc_info=True,
                        )
                        continue
                    return answer
            return None
        finally:
            # Whoever lost the race is told to stop showing it. Their finally
            # blocks broadcast ask_resolved, which is what clears the card on
            # every other surface.
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.wait(tasks.keys())

    async def shutdown(self) -> None:
        for c in self.channels:
            try:
                await c.shutdown()
            except Exception:
                logger.exception("channel %s shutdown failed", c.name)
