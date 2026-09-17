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

from yuyutsava.daemon.web.schemas.feedback import FeedbackIn, FeedbackOut
from yuyutsava.storage.feedback_store import (
    RATINGS,
    get_default_feedback_store,
)
from yuyutsava.storage.sessions import SessionNotFound, get_default_session_store

logger = logging.getLogger("yuyutsava.daemon.web.routers.feedback")

router = APIRouter(tags=["feedback"])


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
