"""SSE broadcast hub + the WebChannel UserChannel.

Items pushed onto the broadcast queue are typed :class:`StreamItem` variants.
The SSE responder (``daemon/web/routers/stream.py``) serializes each item to
JSON at the wire boundary via :meth:`StreamItem.to_wire_dict`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal

from yuyutsava.daemon.channels import (
    AskPrompt,
    ChannelEvent,
    ChannelPayload,
    ProposalDecision,
    UserChannel,
)
from yuyutsava.storage.events import Proposal, Store


# ---------------------------------------------------------------------------
# Typed stream items (broadcast queue payloads → SSE wire format)
# ---------------------------------------------------------------------------


def _payload_to_data(payload: ChannelPayload) -> dict[str, Any]:
    """Drop the ``kind`` discriminator — it's promoted to the wire envelope."""
    d = dataclasses.asdict(payload)
    d.pop("kind", None)
    return d


@dataclass(frozen=True)
class StreamEventItem:
    """SSE relay of a :class:`ChannelEvent` (token/log/timeline/etc.).

    ``task_id`` / ``session_id`` mirror the ChannelEvent scoping tags so the
    SSE responder can filter (``/stream?task_id=``) and the hub can route
    items into the per-task replay ring.
    """

    payload: ChannelPayload
    task_id: str | None = None
    session_id: str | None = None
    type: Literal["event"] = "event"

    def to_wire_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "kind": self.payload.kind,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "data": _payload_to_data(self.payload),
        }


@dataclass(frozen=True)
class StreamProposalItem:
    """SSE relay of a pending :class:`Proposal` awaiting a user decision."""

    proposal: Proposal
    type: Literal["proposal"] = "proposal"

    def to_wire_dict(self) -> dict[str, Any]:
        return {"type": self.type, "proposal": dataclasses.asdict(self.proposal)}


@dataclass(frozen=True)
class StreamAskItem:
    """SSE relay of a Tier-2 :class:`AskPrompt` (tool permission etc.)."""

    ask_id: str
    title: str
    body: str
    options: list[str]
    session_id: str | None = None
    agent_path: str | None = None
    type: Literal["ask"] = "ask"

    @classmethod
    def from_ask(cls, a: AskPrompt) -> "StreamAskItem":
        return cls(
            ask_id=a.ask_id,
            title=a.title,
            body=a.body,
            options=list(a.options),
            session_id=a.session_id,
            agent_path=a.agent_path,
        )

    def to_wire_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "ask": {
                "ask_id": self.ask_id,
                "title": self.title,
                "body": self.body,
                "options": self.options,
                "session_id": self.session_id,
                "agent_path": self.agent_path,
            },
        }


@dataclass(frozen=True)
class StreamAskResolvedItem:
    """Broadcast when a pending ask is resolved (by ANY surface, or expiry) so
    every connected client (UI + CLI) clears its prompt. Carries ``session_id``
    to mirror the original ask's scoping for session-filtered streams."""

    ask_id: str
    session_id: str | None = None
    type: Literal["ask_resolved"] = "ask_resolved"

    def to_wire_dict(self) -> dict[str, Any]:
        return {"type": self.type, "ask_id": self.ask_id, "session_id": self.session_id}


@dataclass(frozen=True)
class StreamProposalResolvedItem:
    """Broadcast when a pending proposal is resolved/expired — clears the card
    on every surface regardless of where it was answered."""

    proposal_id: str
    session_id: str | None = None
    type: Literal["proposal_resolved"] = "proposal_resolved"

    def to_wire_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "proposal_id": self.proposal_id,
            "session_id": self.session_id,
        }


StreamItem = (
    StreamEventItem
    | StreamProposalItem
    | StreamAskItem
    | StreamAskResolvedItem
    | StreamProposalResolvedItem
)


# ---------------------------------------------------------------------------
# WebHub — broadcast queue + pending proposal/ask futures
# ---------------------------------------------------------------------------


# Per-task replay ring: enough to refill a mobile client that reconnects
# mid-task without persisting the firehose.
TASK_RING_SIZE = 500
# Rings are dropped oldest-task-first past this bound so an immortal daemon
# can't accumulate rings forever.
MAX_TRACKED_TASKS = 64


class WebHub:
    """Holds pending proposals/asks, the SSE broadcast queue, and per-task
    replay rings (last :data:`TASK_RING_SIZE` items per task)."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._subscribers: list[asyncio.Queue[StreamItem]] = []
        self._lock = asyncio.Lock()
        self.pending_proposals: dict[str, asyncio.Future[ProposalDecision]] = {}
        self.pending_asks: dict[str, asyncio.Future[str]] = {}
        self._task_rings: "OrderedDict[str, deque[StreamItem]]" = OrderedDict()

    async def subscribe(self) -> AsyncIterator[StreamItem]:
        q: asyncio.Queue[StreamItem] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers.append(q)
        try:
            while True:
                item = await q.get()
                yield item
        finally:
            async with self._lock:
                if q in self._subscribers:
                    self._subscribers.remove(q)

    async def broadcast(self, item: StreamItem) -> None:
        task_id = getattr(item, "task_id", None)
        if task_id:
            ring = self._task_rings.get(task_id)
            if ring is None:
                ring = deque(maxlen=TASK_RING_SIZE)
                self._task_rings[task_id] = ring
                while len(self._task_rings) > MAX_TRACKED_TASKS:
                    self._task_rings.popitem(last=False)
            ring.append(item)
        async with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                # Drop silently for slow tabs.
                pass

    def task_events(self, task_id: str) -> list[StreamItem]:
        """Replay buffer for one task (oldest first); ``[]`` when unknown.

        Served by ``GET /tasks/{id}/events`` so a client that reconnects
        mid-task can fill the gap before resuming the live stream.
        """
        return list(self._task_rings.get(task_id, ()))

    async def resolve_ask(self, ask_id: str, session_id: str | None) -> None:
        """Drop the pending ask future and tell every surface it's resolved.

        Shared by ``WebChannel`` and ``CliRemoteChannel`` so an ask answered on
        any surface clears the CLI prompt **and** the UI AskCard in sync — this
        is what makes "answer from anywhere" actually stay consistent.
        """
        self.pending_asks.pop(ask_id, None)
        await self.broadcast(StreamAskResolvedItem(
            ask_id=ask_id, session_id=session_id,
        ))

    async def resolve_proposal(
        self, proposal_id: str, session_id: str | None
    ) -> None:
        """Drop the pending proposal future and broadcast its resolution.

        Counterpart to :meth:`resolve_ask` for Tier-1 proposals.
        """
        self.pending_proposals.pop(proposal_id, None)
        await self.broadcast(StreamProposalResolvedItem(
            proposal_id=proposal_id, session_id=session_id,
        ))


class WebChannel(UserChannel):
    name = "web"

    def __init__(self, hub: WebHub) -> None:
        self._hub = hub

    async def post_event(self, ev: ChannelEvent) -> None:
        await self._hub.broadcast(StreamEventItem(
            payload=ev.payload, task_id=ev.task_id, session_id=ev.session_id,
        ))

    async def post_proposal(self, p: Proposal) -> ProposalDecision:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ProposalDecision] = loop.create_future()
        self._hub.pending_proposals[p.proposal_id] = fut
        await self._hub.broadcast(StreamProposalItem(proposal=p))
        timeout = max(1.0, p.expires_ts - time.time())
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return ProposalDecision(decision="expired")
        finally:
            # Tell every surface the proposal is no longer pending (answered
            # here or elsewhere, or expired) so cards/prompts clear in sync.
            await self._hub.resolve_proposal(p.proposal_id, p.session_id)

    async def post_ask(self, a: AskPrompt) -> str:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._hub.pending_asks[a.ask_id] = fut
        await self._hub.broadcast(StreamAskItem.from_ask(a))
        try:
            return await fut
        finally:
            # Tell every surface the ask is resolved (answered here or on another
            # surface) so the CLI prompt and the UI AskCard both clear — this is
            # what makes "answer from anywhere" stay in sync.
            await self._hub.resolve_ask(a.ask_id, a.session_id)
