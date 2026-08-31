from __future__ import annotations

import time

from fastapi import APIRouter

from yuyutsava.daemon.web.schemas.health import HealthOut

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut, summary="Liveness probe")
async def health() -> HealthOut:
    return HealthOut(status="ok", ts=time.time())
