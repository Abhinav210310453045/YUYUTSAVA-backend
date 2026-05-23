"""SSE broadcast hub + the WebChannel UserChannel.

Items pushed onto the broadcast queue are typed :class:`StreamItem` variants.
The SSE responder (``daemon/web/routers/stream.py``) serializes each item to
JSON at the wire boundary via :meth:`StreamItem.to_wire_dict`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
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
    """SSE relay of a :class:`ChannelEvent` (token/log/timeline/etc.)."""

    payload: ChannelPayload
    type: Literal["event"] = "event"

    def to_wire_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "kind": self.payload.kind,
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


StreamItem = StreamEventItem | StreamProposalItem | StreamAskItem


# ---------------------------------------------------------------------------
# WebHub — broadcast queue + pending proposal/ask futures
# ---------------------------------------------------------------------------


class WebHub:
    """Holds pending proposals/asks and the SSE broadcast queue."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._subscribers: list[asyncio.Queue[StreamItem]] = []
        self._lock = asyncio.Lock()
        self.pending_proposals: dict[str, asyncio.Future[ProposalDecision]] = {}
        self.pending_asks: dict[str, asyncio.Future[str]] = {}

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
        async with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                # Drop silently for slow tabs.
                pass


class WebChannel(UserChannel):
    name = "web"

    def __init__(self, hub: WebHub) -> None:
        self._hub = hub

    async def post_event(self, ev: ChannelEvent) -> None:
        await self._hub.broadcast(StreamEventItem(payload=ev.payload))

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
            self._hub.pending_proposals.pop(p.proposal_id, None)

    async def post_ask(self, a: AskPrompt) -> str:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._hub.pending_asks[a.ask_id] = fut
        await self._hub.broadcast(StreamAskItem.from_ask(a))
        try:
            return await fut
        finally:
            self._hub.pending_asks.pop(a.ask_id, None)
