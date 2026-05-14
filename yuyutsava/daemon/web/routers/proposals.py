"""Proposal and ask decision endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from yuyutsava.daemon.channels import ProposalDecision
from yuyutsava.daemon.web.deps import get_hub
from yuyutsava.daemon.web.exceptions import ConflictError
from yuyutsava.daemon.web.schemas.proposal import (
    AskRespondIn, OkOut, ProposalRespondIn,
)

router = APIRouter(tags=["decisions"])


@router.post(
    "/proposal/{proposal_id}/respond",
    response_model=OkOut,
    summary="Submit user decision for a pending Tier-1 proposal",
)
async def respond_proposal(
    proposal_id: str,
    body: ProposalRespondIn,
    hub=Depends(get_hub),
) -> OkOut:
    target_status = (
        "approved" if body.decision in ("approve", "approve_remember")
        else "modified" if body.decision == "modify"
        else "skipped"
    )
    flipped = hub.store.try_set_proposal_status(
        proposal_id, from_status="pending", to_status=target_status,
    )
    if not flipped:
        raise ConflictError("proposal expired or already resolved")

    fut = hub.pending_proposals.get(proposal_id)
    if fut is None or fut.done():
        return OkOut(ok=True, note="no listener (already resolved)")

    edited = body.edited_instruction if body.decision == "modify" else None
    fut.set_result(ProposalDecision(decision=body.decision, edited_instruction=edited))
    return OkOut(ok=True)


@router.post(
    "/ask/{ask_id}/respond",
    response_model=OkOut,
    summary="Reply to a Tier-2 ask prompt",
)
async def respond_ask(ask_id: str, body: AskRespondIn, hub=Depends(get_hub)) -> OkOut:
    response = body.response.strip() or "reject"
    fut = hub.pending_asks.get(ask_id)
    if fut is None or fut.done():
        raise ConflictError("ask expired or already answered")
    fut.set_result(response)
    return OkOut(ok=True)
