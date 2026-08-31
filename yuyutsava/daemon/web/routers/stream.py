"""SSE stream of channel events + proposal/ask broadcasts.

Optional ``?task_id=`` / ``?session_id=`` query params scope the stream to
one orchestrator run (mobile task-detail view). Filtering happens here at
the responder — the hub fans out everything to every subscriber unchanged.
A ``?token=`` param may also be present (EventSource cannot set an
Authorization header); it's consumed by the auth middleware, never here.
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from yuyutsava.daemon.web.deps import get_hub

router = APIRouter(tags=["stream"])


def item_matches(item, task_id: str | None, session_id: str | None) -> bool:
    """True when *item* passes the requested scope filters.

    ``task_id`` matches only items tagged with that task (event items);
    proposals/asks carry no task tag and are excluded — clients that need
    them filter by ``session_id`` (the run's thread_id) instead, which asks
    and proposals do carry.
    """
    if task_id is not None and getattr(item, "task_id", None) != task_id:
        return False
    if session_id is not None:
        sid = getattr(item, "session_id", None)
        if sid is None:
            # StreamProposalItem nests the session under .proposal.
            sid = getattr(getattr(item, "proposal", None), "session_id", None)
        if sid != session_id:
            return False
    return True


@router.get("/stream", summary="SSE stream of channel events", include_in_schema=False)
async def stream(
    request: Request,
    hub=Depends(get_hub),
    task_id: str | None = None,
    session_id: str | None = None,
) -> EventSourceResponse:
    async def gen():
        yield {"event": "hello", "data": json.dumps({"ts": time.time()})}
        async for item in hub.subscribe():
            if await request.is_disconnected():
                return
            if not item_matches(item, task_id, session_id):
                continue
            wire = item.to_wire_dict()
            yield {"event": wire["type"], "data": json.dumps(wire, default=str)}

    return EventSourceResponse(gen())
