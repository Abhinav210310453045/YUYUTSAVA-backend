"""Proposal and ask decision endpoints.

Thin HTTP veneer over :class:`DecisionService` (Phase 3 extraction) — the
same service instance backs the channel-plugin ``InboundSink``, so a
decision arriving over HTTP or from a Telegram button takes one code path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from yuyutsava.daemon.web.deps import get_decision_service
from yuyutsava.daemon.web.exceptions import ConflictError
from yuyutsava.daemon.web.schemas.proposal import (
    AskRespondIn, OkOut, ProposalRespondIn,
)
from yuyutsava.daemon.web.services.decision_service import DecisionConflictError

router = APIRouter(tags=["decisions"])


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
