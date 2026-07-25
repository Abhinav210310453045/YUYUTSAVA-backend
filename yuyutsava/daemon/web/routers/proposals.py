"""Proposal and ask decision endpoints.

Thin HTTP veneer over :class:`DecisionService` (Phase 3 extraction) — the
same service instance backs the channel-plugin ``InboundSink``, so a
decision arriving over HTTP or from a Telegram button takes one code path.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from yuyutsava.daemon.web.deps import get_decision_service
from yuyutsava.daemon.web.exceptions import ConflictError
from yuyutsava.daemon.web.schemas.proposal import (
    AskRespondIn, OkOut, ProposalRespondIn,
)
from yuyutsava.daemon.web.services.decision_service import DecisionConflictError

router = APIRouter(tags=["decisions"])


@router.get(
    "/asks",
    summary="Every ask still awaiting an answer (the Inbox / hydration source)",
)
async def list_asks(
    request: Request,
    status: str = Query("pending", pattern="^(pending)$"),
) -> dict[str, Any]:
    """Rediscovery for asks, which never expire and must never be lost.

    Two real holes close here. ``WebHub.broadcast`` drops silently on
    ``QueueFull``, and asks carry no ``task_id`` so the per-task replay ring
    can't refill them — a missed SSE frame used to mean the ask was gone with
    the agent still blocked on it. And an ask raised before a daemon restart
    has no live broadcast at all. Hydrating from here on connect makes both
    self-healing: the record is written before the ask is ever shown.
    """
    registry = getattr(request.app.state, "ask_registry", None)
    if registry is None:
        return {"asks": [], "count": 0}
    asks = registry.pending()
    return {"asks": asks, "count": len(asks)}


@router.post(
    "/proposal/{proposal_id}/respond",
    response_model=OkOut,
    summary="Submit user decision for a pending Tier-1 proposal",
)
async def respond_proposal(
    proposal_id: str,
    body: ProposalRespondIn,
    decisions=Depends(get_decision_service),
) -> OkOut:
    try:
        outcome = await decisions.respond_proposal(
            proposal_id, body.decision,
            edited_instruction=body.edited_instruction,
        )
    except DecisionConflictError as exc:
        raise ConflictError(str(exc)) from exc
    return OkOut(ok=outcome.ok, note=outcome.note)


@router.post(
    "/ask/{ask_id}/respond",
    response_model=OkOut,
    summary="Reply to a Tier-2 ask prompt",
)
async def respond_ask(
    ask_id: str,
    body: AskRespondIn,
    decisions=Depends(get_decision_service),
) -> OkOut:
    try:
        outcome = await decisions.respond_ask(ask_id, body.response)
    except DecisionConflictError as exc:
        raise ConflictError(str(exc)) from exc
    return OkOut(ok=outcome.ok, note=outcome.note)
