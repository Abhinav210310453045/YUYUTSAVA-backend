"""Single implementation of proposal/ask response resolution.

Extracted from ``routers/proposals.py`` (Phase 3) so the HTTP endpoints and
the channel-plugin :class:`~yuyutsava.channels.plugin.InboundSink` resolve
user decisions through ONE code path: flip the proposal's persisted status,
then wake whichever channel is blocked awaiting the decision.

Channels that block on a decision (``WebChannel.post_proposal``, a Telegram
plugin's inline keyboard, …) each park an ``asyncio.Future`` keyed by
proposal/ask id in their own pending map. The service holds references to
every such map (registered via :meth:`add_waiters`) and resolves the future
wherever it lives — the responder doesn't need to know which surface is
showing the prompt.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import MutableMapping

from yuyutsava.daemon.channels import ProposalDecision

logger = logging.getLogger("yuyutsava.daemon.web.services.decision_service")

# Decisions a responder may submit. "expired" is reserved for the channel
# that owns the timeout — never accepted from a responder.
RESPONDABLE_DECISIONS = frozenset(
    {"approve", "approve_remember", "modify", "skip", "skip_remember"}
)


class DecisionConflictError(Exception):
    """The proposal/ask is gone: expired, already answered, or unknown."""


@dataclass(frozen=True)
class RespondOutcome:
    """Result of a respond call that did not conflict."""

    ok: bool = True
    note: str | None = None


def _target_status(decision: str) -> str:
    if decision in ("approve", "approve_remember"):
        return "approved"
    if decision == "modify":
        return "modified"
    return "skipped"


class DecisionService:
    """Resolves Tier-1 proposal and Tier-2 ask responses.

    ``store`` is the events :class:`~yuyutsava.storage.events.Store` (or any
    object with ``try_set_proposal_status``); waiter maps are registered by
    the surfaces that create blocking futures (WebHub at boot, channel
    plugins via the InboundSink).
    """

    def __init__(self, store: object) -> None:
        self._store = store
        self._proposal_maps: list[
            MutableMapping[str, "asyncio.Future[ProposalDecision]"]
        ] = []
        self._ask_maps: list[MutableMapping[str, "asyncio.Future[str]"]] = []

    def add_waiters(
        self,
        *,
        proposals: MutableMapping[str, "asyncio.Future[ProposalDecision]"],
        asks: MutableMapping[str, "asyncio.Future[str]"],
    ) -> None:
        """Register a surface's pending-future maps (held by reference)."""
        if not any(m is proposals for m in self._proposal_maps):
            self._proposal_maps.append(proposals)
        if not any(m is asks for m in self._ask_maps):
            self._ask_maps.append(asks)

    @staticmethod
    def _find(maps: list, key: str):
        for m in maps:
            fut = m.get(key)
            if fut is not None:
                return fut
        return None

    # ------------------------------------------------------------------ #
    # Respond paths                                                       #
    # ------------------------------------------------------------------ #

    async def respond_proposal(
        self,
        proposal_id: str,
        decision: str,
        *,
        edited_instruction: str | None = None,
    ) -> RespondOutcome:
        """Persist the decision and wake the channel awaiting it.

        Raises :class:`DecisionConflictError` when the proposal is no longer
        pending, ``ValueError`` for an unknown decision string.
        """
        if decision not in RESPONDABLE_DECISIONS:
            raise ValueError(f"invalid decision {decision!r}")
        flipped = self._store.try_set_proposal_status(
            proposal_id, from_status="pending", to_status=_target_status(decision),
        )
        if not flipped:
            raise DecisionConflictError("proposal expired or already resolved")

        fut = self._find(self._proposal_maps, proposal_id)
        if fut is None or fut.done():
            return RespondOutcome(ok=True, note="no listener (already resolved)")
        edited = edited_instruction if decision == "modify" else None
        fut.set_result(
            ProposalDecision(decision=decision, edited_instruction=edited)
        )
        return RespondOutcome(ok=True)

    async def respond_ask(self, ask_id: str, response: str) -> RespondOutcome:
        """Resolve a Tier-2 ask. Empty responses default to ``"reject"``."""
        response = response.strip() or "reject"
        fut = self._find(self._ask_maps, ask_id)
        if fut is None or fut.done():
            raise DecisionConflictError("ask expired or already answered")
        fut.set_result(response)
        return RespondOutcome(ok=True)

    # ------------------------------------------------------------------ #
    # Introspection (InboundSink.list_pending)                            #
    # ------------------------------------------------------------------ #

    def pending_ids(self) -> tuple[list[str], list[str]]:
        """(proposal_ids, ask_ids) currently awaiting a decision, any surface."""
        proposals = [
            pid for m in self._proposal_maps
            for pid, fut in m.items() if not fut.done()
        ]
        asks = [
            aid for m in self._ask_maps
            for aid, fut in m.items() if not fut.done()
        ]
        return proposals, asks
