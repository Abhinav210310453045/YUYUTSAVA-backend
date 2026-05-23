"""Slim lifecycle owner for the LangGraph SQLite-backed checkpointer.

Why this exists
---------------
The MVP used :class:`langgraph.checkpoint.memory.MemorySaver` so a daemon
restart lost every mid-conversation thread. That was fine for development
but it means a crash during a long-running task leaves the user without
any way to see what happened. Phase 2 swapped in
:class:`langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver` backed by
``~/.yuyutsava/checkpoints.db`` so threads survive restarts.

This module now owns *only* the saver's async-context lifecycle. The TTL
sweep half of the original ``CheckpointerManager`` moved to
:class:`yuyutsava.storage.sweeper.UnifiedSweeper`, which sweeps stale
threads alongside blob files and old event_payloads rows behind a single
config and loop. We keep the lifecycle owner here because opening
:class:`AsyncSqliteSaver` requires :class:`AsyncExitStack` and the
daemon's main entry point is the natural place to hold that stack.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

logger = logging.getLogger("yuyutsava.daemon.checkpointing")


class CheckpointerSaver:
    """Owns the AsyncSqliteSaver context for the daemon's lifetime.

    The saver is an async context manager; we keep it open via an
    :class:`AsyncExitStack`. Call :meth:`start` after the main loop is
    running and :meth:`stop` during shutdown.

    The returned saver is borrowed by :class:`UnifiedSweeper` for its
    checkpoint-TTL sweep — we do not own the sweep cadence here anymore.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._stack = AsyncExitStack()
        self._saver: AsyncSqliteSaver | None = None

    async def start(self) -> AsyncSqliteSaver:
        """Open the SQLite saver. Returns the live saver for sharing."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._saver = await self._stack.enter_async_context(
            AsyncSqliteSaver.from_conn_string(str(self._db_path)),
        )
        await self._saver.setup()
        logger.info("checkpointer: %s", self._db_path)
        return self._saver

    async def stop(self) -> None:
        """Close the saver. Idempotent."""
        await self._stack.aclose()
        self._saver = None

    @property
    def saver(self) -> AsyncSqliteSaver:
        if self._saver is None:
            raise RuntimeError("CheckpointerSaver.start() must be called first")
        return self._saver
