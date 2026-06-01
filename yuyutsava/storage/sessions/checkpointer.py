"""Async checkpointer factory.

Currently produces ``AsyncSqliteSaver``; an ``AsyncPostgresSaver`` branch
keyed off ``SessionsSettings.backend`` is the planned extension point and
will not require touching callers.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from yuyutsava.storage.sessions.config import SessionsSettings


@asynccontextmanager
async def build_checkpointer(settings: SessionsSettings):
    """Yield an open, ``setup()``-completed ``BaseCheckpointSaver``.

    Always use as an ``async with`` — the underlying connection is owned by
    the context, not the caller. Closes cleanly on exit even after exceptions.
    """
    if settings.backend != "sqlite":
        raise NotImplementedError(
            f"sessions backend {settings.backend!r} is not implemented yet; "
            "only 'sqlite' is supported. Add a branch here when you wire Postgres."
        )

    await asyncio.to_thread(
        settings.db_path.parent.mkdir, parents=True, exist_ok=True
    )
    async with AsyncSqliteSaver.from_conn_string(str(settings.db_path)) as saver:
        await saver.setup()
        yield saver


__all__ = ["build_checkpointer", "BaseCheckpointSaver"]
