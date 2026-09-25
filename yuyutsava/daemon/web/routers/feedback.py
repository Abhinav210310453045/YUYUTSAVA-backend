"""HTTP endpoints for message feedback (👍/👎 reactions in the UI).

  * ``POST /feedback``            — record/replace a rating on a message
  * ``GET  /feedback?session_id`` — list feedback (per-session, or all)

Backed by the daemon's feedback store (``app.state.feedback_store``). The
reacted-to message pair is snapshotted into the row — feed for a future
feedback agent — which is why deleting a session deletes its feedback too.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from yuyutsava.daemon.web.deps import get_feedback_store, get_session_store
from yuyutsava.daemon.web.schemas.feedback import FeedbackIn, FeedbackOut
from yuyutsava.storage.feedback_store import RATINGS
from yuyutsava.storage.sessions import SessionNotFound

logger = logging.getLogger("yuyutsava.daemon.web.routers.feedback")

router = APIRouter(tags=["feedback"])


@router.post("/feedback", response_model=FeedbackOut, summary="Record message feedback")
async def submit_feedback(
    body: FeedbackIn,
    sessions=Depends(get_session_store),
    feedback=Depends(get_feedback_store),
) -> FeedbackOut:
    if body.rating not in RATINGS:
        raise HTTPException(status_code=400, detail=f"rating must be one of {list(RATINGS)}")
    try:
        session = await sessions.get(body.session_id)
    except SessionNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"no session with id {body.session_id!r}"
        ) from exc
    # session.workspace is a Path; stringify so psycopg/sqlite can adapt it.
    ws = getattr(session, "workspace", None)
    rec = await feedback.upsert(
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
    sessions=Depends(get_session_store),
    store=Depends(get_feedback_store),
) -> list[FeedbackOut]:
    if session_id:
        try:
            session = await sessions.get(session_id)
        except SessionNotFound as exc:
            raise HTTPException(
                status_code=404, detail=f"no session with id {session_id!r}"
            ) from exc
        records = await store.list_for_thread(session.thread_id, limit=limit)
    else:
        records = await store.list_all(limit=limit)
    return [FeedbackOut.from_record(r) for r in records]
