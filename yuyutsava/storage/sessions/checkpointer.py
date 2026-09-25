"""Async checkpointer factory.

Produces ``AsyncSqliteSaver`` (zero-config default) or ``AsyncPostgresSaver``
when the storage backend is Postgres (``YUYUTSAVA_STORAGE_BACKEND=postgres``,
see :mod:`yuyutsava.storage.backend`). Callers are unchanged either way —
both savers implement :class:`BaseCheckpointSaver`.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

import psycopg
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from yuyutsava.storage.backend import StorageSettings
from yuyutsava.storage.pg.pool import PostgresUnavailable
from yuyutsava.storage.sessions.config import SessionsSettings


@asynccontextmanager
async def build_checkpointer(
    settings: SessionsSettings,
    storage: StorageSettings | None = None,
):
    """Yield an open, ``setup()``-completed ``BaseCheckpointSaver``.

    Always use as an ``async with`` — the underlying connection is owned by
    the context, not the caller. Closes cleanly on exit even after exceptions.

    ``storage`` defaults to :meth:`StorageSettings.from_env`; pass it
    explicitly in tests to avoid env coupling. Raises
    :class:`~yuyutsava.storage.pg.pool.PostgresUnavailable` when Postgres is
    selected but cannot be reached.
    """
    storage = storage or StorageSettings.from_env()

    if storage.is_postgres() or settings.backend == "postgres":
        async with AsyncExitStack() as stack:
            # Only the connect is translated; an error in the body is the body's.
            try:
                saver = await stack.enter_async_context(
                    AsyncPostgresSaver.from_conn_string(storage.pg_dsn)
                )
            except psycopg.OperationalError as exc:
                raise PostgresUnavailable(storage.pg_dsn, exc) from exc
            await saver.setup()
            yield saver
            return

    if settings.backend != "sqlite":
        raise NotImplementedError(
            f"sessions backend {settings.backend!r} is not implemented; use 'sqlite' or 'postgres'."
        )

    await asyncio.to_thread(settings.db_path.parent.mkdir, parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(settings.db_path)) as saver:
        await saver.setup()
        yield saver


__all__ = ["build_checkpointer", "BaseCheckpointSaver"]
