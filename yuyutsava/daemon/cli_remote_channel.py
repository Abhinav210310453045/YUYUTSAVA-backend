"""``CliRemoteChannel`` — UserChannel impl for a CLI attached to the daemon.

Mode 2 of the CLI HITL routing matrix: the CLI process is separate from the
daemon and attaches over HTTP. It subscribes to the existing ``/stream`` SSE
endpoint for incoming events/asks and POSTs replies to the existing
``/ask/{ask_id}/respond`` endpoint. The only daemon-side addition is this
``UserChannel`` registered with the daemon's ``ChannelRouter`` while the CLI
is attached — which gives ``SessionOriginMap`` something to target when a
CLI-issued task asks a question.

Shares the ``WebHub`` broadcast queue + ``pending_asks`` dict so the CLI sees
the same stream the Electron renderer would. Origin-aware routing in
``ChannelRouter._ordered_for_ask`` ensures the CLI is *preferred* when the
ask's ``session_id`` was originated from a CLI submission.
"""

from __future__ import annotations

import asyncio
import logging
import time

from yuyutsava.daemon.channels import (
    AskPrompt,
    ChannelEvent,
    ProposalDecision,
    UserChannel,
)
from yuyutsava.daemon.web.services.stream_service import (
    StreamAskItem,
    StreamEventItem,
    StreamProposalItem,
    WebHub,
)
from yuyutsava.storage.events import Proposal

logger = logging.getLogger("yuyutsava.daemon.cli_remote_channel")


class CliRemoteChannel(UserChannel):
    """A UserChannel that delegates to the shared WebHub for transport.

    Multiple CliRemoteChannels can coexist (e.g. one per attached CLI session)
    but for v1 we register a single shared instance. The shared instance keeps
    routing simple: an ask is broadcast once; whoever responds first wins.
    """

    # Parks an asyncio.Future on the hub, so it is safe to race against the
    # other surfaces: an ask reaches the attached CLI *and* the UI, and the
    # loser is cancelled (its finally still broadcasts ask_resolved).
    broadcast_asks = True

    def __init__(self, hub: WebHub, *, name: str = "cli-remote") -> None:
        self.name = name
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
            # Mirror WebChannel: broadcast the resolution so the UI card clears
            # even when this CLI-owned proposal is answered from another surface.
            await self._hub.resolve_proposal(p.proposal_id, p.session_id)

    async def post_ask(self, a: AskPrompt) -> str:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._hub.pending_asks[a.ask_id] = fut
        await self._hub.broadcast(StreamAskItem.from_ask(a))
        try:
            return await fut
        finally:
            # Mirror WebChannel: broadcast ``ask_resolved`` so both the CLI
            # prompt and the UI AskCard clear regardless of where the answer
            # came from. Without this, a CLI-originated background-task ask
            # (which this channel owns via origin-preferred routing) resumes the
            # agent but leaves stale prompts on every surface.
            await self._hub.resolve_ask(a.ask_id, a.session_id)
