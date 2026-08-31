"""Slim lifecycle owner for the LangGraph checkpointer (SQLite or Postgres).

Why this exists
---------------
The MVP used :class:`langgraph.checkpoint.memory.MemorySaver` so a daemon
restart lost every mid-conversation thread. Phase 2 swapped in
:class:`langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver` backed by
``~/.yuyutsava/checkpoints.db``. The context-controller work added an
``AsyncPostgresSaver`` branch keyed off ``YUYUTSAVA_STORAGE_BACKEND`` so
checkpoints can live in the durable Postgres store alongside artifacts,
summaries, and memories.

Fallback contract: when Postgres is configured but unreachable, ``start()``
falls back to SQLite and records :attr:`fallback_reason` — bootstrap turns
that into a user-visible timeline event, because checkpoints written to the
fallback are invisible to Postgres. ``YUYUTSAVA_STORAGE_REQUIRE=1`` disables
the fallback and fails the boot instead.

This module owns *only* the saver's async-context lifecycle. The TTL sweep
lives in :class:`yuyutsava.storage.sweeper.UnifiedSweeper`, which borrows
the live saver.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from yuyutsava.storage.backend import StorageSettings

logger = logging.getLogger("yuyutsava.daemon.checkpointing")


class CheckpointerSaver:
    """Owns the checkpointer context for the daemon's lifetime.

    The saver is an async context manager; we keep it open via an
    :class:`AsyncExitStack`. Call :meth:`start` after the main loop is
    running and :meth:`stop` during shutdown.

    The returned saver is borrowed by :class:`UnifiedSweeper` for its
    checkpoint-TTL sweep — we do not own the sweep cadence here.
    """

    def __init__(self, db_path: Path, *, storage: StorageSettings | None = None) -> None:
        self._db_path = db_path
        self._storage = storage or StorageSettings.from_env()
        self._stack = AsyncExitStack()
        self._saver: BaseCheckpointSaver | None = None
        # Set when Postgres was configured but the daemon fell back to
        # SQLite. Bootstrap surfaces this on the user channels.
        self.fallback_reason: str | None = None

    async def start(self) -> BaseCheckpointSaver:
        """Open the configured saver. Returns the live saver for sharing."""
        if self._storage.is_postgres():
            try:
                saver = await self._start_postgres()
                logger.info("checkpointer: postgres (%s)", _redact_dsn(self._storage.pg_dsn))
                return saver
            except Exception as exc:
                if self._storage.require:
                    logger.error(
                        "checkpointer: postgres unavailable and "
                        "YUYUTSAVA_STORAGE_REQUIRE=1 — refusing to boot"
                    )
                    raise
                self.fallback_reason = (
                    f"Postgres checkpointer unavailable ({exc}); "
                    "falling back to SQLite. Checkpoints written now are "
                    "INVISIBLE to Postgres once it returns."
                )
                logger.error("checkpointer: %s", self.fallback_reason)

        saver = await self._start_sqlite()
        logger.info("checkpointer: %s", self._db_path)
        return saver

    async def _start_postgres(self) -> BaseCheckpointSaver:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        saver = await self._stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(self._storage.pg_dsn),
        )
        await saver.setup()
        self._saver = saver
        return saver

    async def _start_sqlite(self) -> BaseCheckpointSaver:
        await asyncio.to_thread(self._db_path.parent.mkdir, parents=True, exist_ok=True)
        saver = await self._stack.enter_async_context(
            AsyncSqliteSaver.from_conn_string(str(self._db_path)),
        )
        await saver.setup()
        self._saver = saver
        return saver

    async def stop(self) -> None:
        """Close the saver. Idempotent."""
        await self._stack.aclose()
        self._saver = None

    @property
    def saver(self) -> BaseCheckpointSaver:
        if self._saver is None:
            raise RuntimeError("CheckpointerSaver.start() must be called first")
        return self._saver


def _redact_dsn(dsn: str) -> str:
    """Hide the password segment of a DSN for log lines."""
    try:
        before, _, after = dsn.partition("@")
        if ":" in before and after:
            scheme_user = before.rsplit(":", 1)[0]
            return f"{scheme_user}:***@{after}"
    except Exception:
        pass
    return dsn
