"""SQLite-backed checkpointer + TTL sweeper for the orchestrator/subagent graphs.

Why this exists
---------------
The MVP used :class:`langgraph.checkpoint.memory.MemorySaver` so a daemon
restart lost every mid-conversation thread. That was fine for development
but it means a crash during a long-running task leaves the user without
any way to see what happened. Phase 2 swaps in
:class:`langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver` backed by
``~/.yuyutsava/checkpoints.db`` so threads survive restarts.

To stop the database from growing without bound we sweep stale rows on a
schedule. The checkpoint table has no ``created_ts`` column, so we encode
the timestamp into the ``thread_id`` itself (``<role>-<unix_ts>-<uuid>``)
and parse it back at sweep time. Anything older than ``ttl_sec`` is
deleted via :meth:`AsyncSqliteSaver.adelete_thread`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import AsyncExitStack
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

logger = logging.getLogger("yuyutsava.daemon.checkpointing")


# Default: an orchestrator task's checkpoint is meaningful only for the
# duration of that task. One hour gives generous slack for very long
# subagent runs while keeping the DB bounded.
DEFAULT_TTL_SEC: int = 3600
DEFAULT_SWEEP_INTERVAL_SEC: int = 300


def thread_id(role: str) -> str:
    """Mint a thread_id whose timestamp the sweeper can parse.

    Format: ``<role>-<unix_ts>-<uuid4>``. ``role`` is a short tag like
    ``orch`` or ``triage``; the sweeper does not look at it but it makes
    rows easier to skim when debugging the DB by hand.
    """
    import uuid as _uuid  # local — uuid is cheap but avoid eager import
    return f"{role}-{int(time.time())}-{_uuid.uuid4()}"


def _parse_ts(thread_id_value: str) -> float | None:
    """Recover the unix timestamp from a thread_id minted by :func:`thread_id`.

    Returns ``None`` for thread_ids that don't fit the expected shape — the
    sweeper leaves those alone rather than guessing.
    """
    parts = thread_id_value.split("-")
    # role - <ts> - <uuid-with-4-dashes>
    if len(parts) < 3:
        return None
    try:
        return float(parts[1])
    except (ValueError, IndexError):
        return None


class CheckpointerManager:
    """Owns the AsyncSqliteSaver context and a background TTL sweeper.

    The saver is an async context manager; we keep it open for the daemon's
    lifetime via an :class:`AsyncExitStack`. Call :meth:`start` after the
    main loop is running and :meth:`stop` during shutdown.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        ttl_sec: int = DEFAULT_TTL_SEC,
        sweep_interval_sec: int = DEFAULT_SWEEP_INTERVAL_SEC,
    ) -> None:
        self._db_path = db_path
        self._ttl_sec = ttl_sec
        self._sweep_interval_sec = sweep_interval_sec
        self._stack = AsyncExitStack()
        self._saver: AsyncSqliteSaver | None = None
        self._sweeper_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> AsyncSqliteSaver:
        """Open the SQLite saver and start the sweeper. Returns the saver."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._saver = await self._stack.enter_async_context(
            AsyncSqliteSaver.from_conn_string(str(self._db_path)),
        )
        await self._saver.setup()
        logger.info("checkpointer: %s (ttl=%ss, sweep=%ss)",
                    self._db_path, self._ttl_sec, self._sweep_interval_sec)
        self._sweeper_task = asyncio.create_task(self._sweeper(), name="ckpt-sweeper")
        return self._saver

    async def stop(self) -> None:
        """Stop the sweeper and close the saver. Idempotent."""
        self._stop_event.set()
        if self._sweeper_task is not None:
            try:
                await asyncio.wait_for(self._sweeper_task, timeout=2.0)
            except asyncio.TimeoutError:
                self._sweeper_task.cancel()
                try:
                    await self._sweeper_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._sweeper_task = None
        await self._stack.aclose()
        self._saver = None

    @property
    def saver(self) -> AsyncSqliteSaver:
        if self._saver is None:
            raise RuntimeError("CheckpointerManager.start() must be called first")
        return self._saver

    # ------------------------------------------------------------------
    # Sweeper
    # ------------------------------------------------------------------

    async def _sweeper(self) -> None:
        """Periodically delete checkpoints whose thread_id is older than TTL."""
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._sweep_interval_sec,
                )
                return  # stop requested
            except asyncio.TimeoutError:
                pass
            try:
                deleted = await self._sweep_once()
                if deleted:
                    logger.info("checkpointer: swept %d stale thread(s)", deleted)
            except Exception:
                logger.exception("checkpointer: sweep iteration failed")

    async def _sweep_once(self) -> int:
        """Delete every thread whose minted timestamp is older than TTL.

        Returns the number of threads removed. Threads with unparseable
        timestamps (e.g. from old code paths or external callers) are left
        in place.
        """
        if self._saver is None:
            return 0
        cutoff = time.time() - self._ttl_sec
        stale: list[str] = []
        async with self._saver.lock, self._saver.conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints"
        ) as cur:
            async for (tid,) in cur:
                ts = _parse_ts(tid)
                if ts is not None and ts < cutoff:
                    stale.append(tid)
        for tid in stale:
            try:
                await self._saver.adelete_thread(tid)
            except Exception:
                logger.exception("checkpointer: failed to delete thread %r", tid)
        return len(stale)
