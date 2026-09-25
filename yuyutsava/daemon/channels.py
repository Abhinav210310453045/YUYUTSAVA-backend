"""The daemon's channel router.

:class:`ChannelRouter` fans :class:`~yuyutsava.ports.channels.ChannelEvent` out
to every registered :class:`~yuyutsava.ports.channels.UserChannel`, and routes
proposals and asks to the channels that should show them. The types it carries
live in :mod:`yuyutsava.ports.channels`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass
from typing import Any

from yuyutsava.ports.channels import (
    AskPrompt,
    ChannelEvent,
    ProposalDecision,
    UserChannel,
)
from yuyutsava.storage.events import Proposal

logger = logging.getLogger("yuyutsava.daemon.channels")


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

    TODO: when ``yuyutsava/daemon/push_channel.py`` (pync-backed
    macOS notifications) is added for ``--no-ui`` mode, this constructor
    must assert that ``PushChannel`` is not present alongside ``WebChannel``.
    The Electron renderer already shows focus-aware OS banners via
    ``notify:show`` IPC; pairing both would double-banner the user. Pick
    one channel based on ``DaemonConfig.headless`` at boot — never both.
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
            c
            for c in self.channels
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
            except Exception:  # noqa: BLE001 — bookkeeping; the ask still goes out
                logger.debug("ask registry record failed", exc_info=True)

        prefer = None
        if self.session_origin is not None:
            try:
                prefer = self.session_origin.get(a.session_id)
            except Exception:  # noqa: BLE001 — a routing hint; without it every channel is asked
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
            # that can never be answered and whose agent is gone.
            # Detached because we are unwinding a cancellation and must not
            # await here.
            if self.ask_registry is not None:
                asyncio.create_task(self.ask_registry.resolve(a.ask_id, "", status="cancelled"))
            raise
        if answer is None:
            logger.error("No channel could handle ask %s; defaulting to reject", a.ask_id)
            answer = "reject"

        if self.ask_registry is not None:
            try:
                await self.ask_registry.resolve(a.ask_id, answer)
            except Exception:  # noqa: BLE001 — bookkeeping; the answer still returns
                logger.debug("ask registry resolve failed", exc_info=True)
        return answer

    async def _race_ask(self, a: AskPrompt, channels: list[UserChannel]) -> str | None:
        """Await every channel at once; return the first real answer, or None.

        ``None`` means every candidate declined (``NotImplementedError``) or
        failed — the caller then falls back to the sequential path.
        """
        tasks = {
            asyncio.create_task(c.post_ask(a), name=f"ask:{c.name}:{a.ask_id}"): c for c in channels
        }
        answer: str | None = None
        try:
            while tasks:
                done, _ = await asyncio.wait(tasks.keys(), return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    channel = tasks.pop(t)
                    try:
                        answer = t.result()
                    except asyncio.CancelledError:
                        continue
                    except NotImplementedError:
                        continue  # this surface can't show asks
                    except Exception:  # noqa: BLE001 — one channel must not stop the rest
                        logger.warning(
                            "channel %s failed showing ask %s",
                            channel.name,
                            a.ask_id,
                            exc_info=True,
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
