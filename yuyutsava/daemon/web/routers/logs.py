"""Runtime log-level control.

Lets the UI switch the daemon's logging verbosity between DEBUG/INFO/WARNING
without restarting. The chosen level is mirrored onto the ``yuyutsava`` and
``uvicorn`` logger trees and persisted in ``user_prefs`` so it survives a
daemon restart.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from yuyutsava.daemon.web.deps import get_store
from yuyutsava.prefs.store import UserPrefsStore

router = APIRouter(tags=["logs"])

_ALLOWED = ("DEBUG", "INFO", "WARNING")
_PREF_KEY = "daemon.log_level"


class LogLevelOut(BaseModel):
    level: str


class LogLevelIn(BaseModel):
    level: str = Field(..., description="One of DEBUG, INFO, WARNING")


def _current_level_name() -> str:
    return logging.getLevelName(logging.getLogger("yuyutsava").getEffectiveLevel())


@router.get("/logs/level", response_model=LogLevelOut, summary="Get current log level")
async def get_log_level() -> LogLevelOut:
    return LogLevelOut(level=_current_level_name())


@router.put("/logs/level", response_model=LogLevelOut, summary="Set log level at runtime")
async def set_log_level(body: LogLevelIn, store=Depends(get_store)) -> LogLevelOut:
    level_name = body.level.upper()
    if level_name not in _ALLOWED:
        raise HTTPException(
            status_code=400,
            detail=f"level must be one of {_ALLOWED}, got {body.level!r}",
        )
    level = getattr(logging, level_name)
    # Apply to live loggers + their handlers so existing StreamHandlers honour it.
    for name in ("yuyutsava", "uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.setLevel(level)
        for h in lg.handlers:
            h.setLevel(level)
    # Persist across restarts.
    await UserPrefsStore(store).set(_PREF_KEY, level_name)
    return LogLevelOut(level=level_name)
