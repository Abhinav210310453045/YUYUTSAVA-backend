"""HTTP endpoints for message feedback (👍/👎 reactions in the UI).

  * ``POST /feedback``            — record/replace a rating on a message
  * ``GET  /feedback?session_id`` — list feedback (per-session, or all)

Backed by the process-singleton :class:`SqliteFeedbackStore` (no app-state
wiring needed). The reacted-to message pair is snapshotted into the row so the
record survives session deletion — feed for a future feedback agent.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from yuyutsava.storage.feedback_store import (
    RATINGS,
    MessageFeedback,
    get_default_feedback_store,
)
from yuyutsava.storage.sessions import SessionNotFound, get_default_session_store

logger = logging.getLogger("yuyutsava.daemon.web.routers.feedback")

router = APIRouter(tags=["feedback"])


class FeedbackIn(BaseModel):
    session_id: str = Field(..., description="Session the message belongs to")
    message_ref: str = Field(..., description="Client message id / turn ref being rated")
    rating: str = Field(..., description="'up' or 'down'")
    user_text: str = Field("", description="Snapshot of the prompting user turn")
    assistant_text: str = Field("", description="Snapshot of the rated assistant turn")
    note: str | None = Field(None, description="Optional free-text note (e.g. why 👎)")


class FeedbackOut(BaseModel):
    feedback_id: str
    thread_id: str
    session_id: str
    message_ref: str
    rating: str
    note: str | None
    user_text: str
    assistant_text: str
    created_ts: float

    @classmethod
    def from_record(cls, r: MessageFeedback) -> "FeedbackOut":
        return cls(
            feedback_id=r.feedback_id,
            thread_id=r.thread_id,
            session_id=r.session_id,
            message_ref=r.message_ref,
            rating=r.rating,
            note=r.note,
            user_text=r.user_text,
            assistant_text=r.assistant_text,
            created_ts=r.created_ts,
        )


@router.post("/feedback", response_model=FeedbackOut, summary="Record message feedback")
async def submit_feedback(body: FeedbackIn) -> FeedbackOut:
    if body.rating not in RATINGS:
        raise HTTPException(status_code=400, detail=f"rating must be one of {list(RATINGS)}")
    try:
        session = await get_default_session_store().get(body.session_id)
    except SessionNotFound:
        raise HTTPException(status_code=404, detail=f"no session with id {body.session_id!r}")
    # session.workspace is a Path; stringify so psycopg/sqlite can adapt it.
    ws = getattr(session, "workspace", None)
    rec = await get_default_feedback_store().upsert(
        thread_id=session.thread_id,
        session_id=body.session_id,
        message_ref=body.message_ref,
        rating=body.rating,
        user_text=body.user_text,
        assistant_text=body.assistant_text,
        workspace=str(ws) if ws is not None else None,
        note=body.note,
    )
    return FeedbackOut.from_record(rec)


@router.get("/feedback", response_model=list[FeedbackOut], summary="List message feedback")
async def list_feedback(
    session_id: str | None = Query(None, description="Filter to one session; omit for all"),
    limit: int = Query(1000, ge=1, le=5000),
) -> list[FeedbackOut]:
    store = get_default_feedback_store()
    if session_id:
        try:
            session = await get_default_session_store().get(session_id)
        except SessionNotFound:
            raise HTTPException(status_code=404, detail=f"no session with id {session_id!r}")
        records = await store.list_for_thread(session.thread_id, limit=limit)
    else:
        records = await store.list_all(limit=limit)
    return [FeedbackOut.from_record(r) for r in records]
