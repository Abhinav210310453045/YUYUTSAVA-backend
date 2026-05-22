"""SSE broadcast hub + the WebChannel UserChannel.

Moved unchanged from the legacy ``server.py``; logic is unchanged so existing
clients continue to work.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from typing import Any, AsyncIterator

from yuyutsava.daemon.channels import (
    AskPrompt, ChannelEvent, ProposalDecision, UserChannel,
)
from yuyutsava.events.store import Proposal, Store


class WebHub:
    """Holds pending proposals/asks and the SSE broadcast queue."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._lock = asyncio.Lock()
        self.pending_proposals: dict[str, asyncio.Future[ProposalDecision]] = {}
        self.pending_asks: dict[str, asyncio.Future[str]] = {}

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
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

    async def broadcast(self, item: dict[str, Any]) -> None:
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
        await self._hub.broadcast({
            "type": "event",
            "kind": ev.kind,
            "data": ev.data,
        })

    async def post_proposal(self, p: Proposal) -> ProposalDecision:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ProposalDecision] = loop.create_future()
        self._hub.pending_proposals[p.proposal_id] = fut
        await self._hub.broadcast({
            "type": "proposal",
            "proposal": dataclasses.asdict(p),
        })
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
        await self._hub.broadcast({
            "type": "ask",
            "ask": {
                "ask_id": a.ask_id,
                "title": a.title,
                "body": a.body,
                "options": a.options,
                "session_id": a.session_id,
                "agent_path": a.agent_path,
            },
        })
        try:
            return await fut
        finally:
            self._hub.pending_asks.pop(a.ask_id, None)
