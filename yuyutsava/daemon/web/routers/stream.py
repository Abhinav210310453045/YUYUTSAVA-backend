"""SSE stream of channel events + proposal/ask broadcasts."""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, Request
from sse_starlette.sse import EventSourceResponse

from yuyutsava.daemon.web.deps import get_hub

router = APIRouter(tags=["stream"])


@router.get("/stream", summary="SSE stream of channel events", include_in_schema=False)
async def stream(request: Request, hub=Depends(get_hub)) -> EventSourceResponse:
    async def gen():
        yield {"event": "hello", "data": json.dumps({"ts": time.time()})}
        async for item in hub.subscribe():
            if await request.is_disconnected():
                return
            wire = item.to_wire_dict()
            yield {"event": wire["type"], "data": json.dumps(wire, default=str)}

    return EventSourceResponse(gen())
