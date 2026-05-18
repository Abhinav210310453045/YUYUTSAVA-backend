"""Retention sweeper for non-blob ``event_payloads`` rows.

Without this, every event that flows through the bus is kept forever in
``state.db``. Webcam/audio blob rows have their own sweeper
(:mod:`yuyutsava.daemon.blob_sweeper`) tied to on-disk file deletion;
everything else (token events, tool-call/result, timeline, http_log, …)
would grow unbounded.

Default retention is 7 days. Configure via the constructor.
"""

from __future__ import annotations

import asyncio
import logging
import time

from yuyutsava.events.store import Store

logger = logging.getLogger("yuyutsava.daemon.events_sweeper")


DEFAULT_RETENTION_SEC: int = 7 * 24 * 3600
DEFAULT_SWEEP_INTERVAL_SEC: int = 3600  # 1 hour


class EventsSweeper:
    """Periodically prune ``event_payloads`` rows older than the retention window."""

    def __init__(
        self,
        store: Store,
        *,
        retention_sec: int = DEFAULT_RETENTION_SEC,
        sweep_interval_sec: int = DEFAULT_SWEEP_INTERVAL_SEC,
    ) -> None:
        self._store = store
        self._retention_sec = retention_sec
        self._sweep_interval_sec = sweep_interval_sec
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        logger.info(
            "events sweeper: retention=%ss, sweep every %ss",
            self._retention_sec, self._sweep_interval_sec,
        )
        self._task = asyncio.create_task(self._loop(), name="events-sweeper")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._sweep_interval_sec,
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                cutoff = time.time() - self._retention_sec
                removed = self._store.delete_event_payloads_older_than(cutoff)
                if removed:
                    logger.info("events sweeper: removed %d row(s)", removed)
            except Exception:
                logger.exception("events sweeper: sweep iteration failed")
