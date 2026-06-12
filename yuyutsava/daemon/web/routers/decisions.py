from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, Query

from yuyutsava.daemon.web.deps import get_hub

router = APIRouter(tags=["decisions"])


@router.get("/decisions", summary="Recent decision log (for the timeline)")
async def list_decisions(
    limit: int = Query(50, ge=1, le=500),
    cursor: float | None = Query(
        None, description="ts of the last row of the previous page (keyset pagination)"
    ),
    hub=Depends(get_hub),
) -> list[dict[str, Any]]:
    return [asdict(d) for d in hub.store.list_decisions(limit=limit, cursor=cursor)]
