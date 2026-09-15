"""The contract between the daemon and the user: events, asks and channels.

The daemon talks to the user only through :class:`UserChannel` implementations.
Each channel handles three flavours of message:

- ``post_event``: token streams, tool calls, status updates (broadcast to all
  enabled channels).
- ``post_proposal``: Tier-1 consent — "I want to do X, may I?". Routed to
  one channel based on originator policy; awaits a user decision.
- ``post_ask``: Tier-2 tool-level interrupt — "permission for tr_write_file".
  Same routing as proposals; reuses the existing TaskRunner interrupt
  semantics, only the transport changes.

The types live here, in ``ports``, so that agents, background subagents, channel
plugins and the CLI can build and read them without importing the daemon. The
router that fans them out is :class:`yuyutsava.daemon.channels.ChannelRouter`;
code that only needs to ask the user depends on :class:`AskRouter`.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol

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
class AppLogPayload:
    """One Python log record from the daemon, structured.

    The Logs panel used to contain *only* HTTP access lines — method, path,
    status, milliseconds — because ``http_log`` was the single kind routed to
    it. Meanwhile the daemon makes ~500 ``logger.*`` calls across ~150 loggers
    (model calls, retries, compactions, offloads, storage failover, MCP loads,
    caught exceptions) and every one of them went to stderr, where no user can
    see it. This is the kind that carries them.

    Structured rather than a formatted string so the panel can colour by
    level, show which subsystem is talking, and filter — none of which is
    possible once it has been flattened into prose.
    """

    level: str  # "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL"
    logger: str  # e.g. "yuyutsava.context.compaction"
    message: str
    ts: float
    kind: Literal["app_log"] = "app_log"


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
    | AppLogPayload
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
    options: list[str]  # e.g. ["approve","reject"] or free-text if empty
    # ``interrupt_value`` is a raw passthrough from LangGraph's ``interrupt()``
    # — its shape is one of the variants documented in
    # :mod:`yuyutsava.models.interrupts` (plus the loose ``orchestrator_ask``
    # form the orchestrator builds inline). We treat it as opaque context
    # carried with the prompt; consumers parse it back into a typed model
    # when they need to. Typed as a read-only ``Mapping`` to discourage
    # mutation through this field.
    interrupt_value: Mapping[str, Any]
    session_id: str | None = None  # thread_id of the originating run (HITL scoping)
    agent_path: str | None = None  # e.g. "orchestrator/file_organizer#1" — who's asking
    # ---- ownership: which surface raised this, and how to get back to it ----
    surface: str = "background"  # one of ASK_SURFACES
    thread_id: str | None = None  # conversation thread, when a conversation owns it
    card_id: str | None = None  # TODO card, for tinker asks
    task_id: str | None = None  # background task id, for bg asks
    agent_label: str | None = None  # short human name ("TinkerAgent", "file-organizer")
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
    async def post_proposal(self, p: Any) -> ProposalDecision:
        """Show a proposal and **block** until the user responds (or it expires).

        ``p`` is a :class:`yuyutsava.storage.events.Proposal`; it is typed ``Any``
        here because ``ports`` imports nothing from the codebase.
        """

    @abstractmethod
    async def post_ask(self, a: AskPrompt) -> str:
        """Show a Tier-2 ask and block until the user responds. Return the response string."""

    async def shutdown(self) -> None:
        """Optional cleanup hook."""


class AskRouter(Protocol):
    """What an agent needs from the channel router: post an ask, wait for the answer."""

    async def post_ask(self, a: AskPrompt) -> str: ...
