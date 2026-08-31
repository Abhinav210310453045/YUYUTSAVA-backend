"""Process-level Postgres health: the ``degraded`` flag + a ``SELECT 1`` probe.

One :class:`StorageHealth` per process is shared by every
:class:`~yuyutsava.storage.routing.facade.RoutedStore`. When a routed write
hits a Postgres runtime error it calls :meth:`mark_degraded`; that flips the
shared flag (so all stores immediately route to their SQLite buffers) and
starts a background probe. The probe polls ``SELECT 1`` until Postgres answers,
clears the flag, and fires the recovery callback (reconcile).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from yuyutsava.storage.pg.pool import PgPool
from yuyutsava.storage.routing.errors import PG_RUNTIME_ERRORS

logger = logging.getLogger("yuyutsava.storage.routing.health")


class StorageHealth:
    def __init__(
        self,
        pool: PgPool,
        *,
        on_degrade: Callable[[str], None] | None = None,
        on_recover: Callable[[], Awaitable[None]] | None = None,
        probe_interval_sec: float = 5.0,
    ) -> None:
        self._pool = pool
        self._on_degrade = on_degrade
        self._on_recover = on_recover
        self._interval = probe_interval_sec
        self.degraded = False
        self._reason: str | None = None
        self._probe_task: asyncio.Task[None] | None = None

    def set_degrade(self, cb: Callable[[str], None] | None) -> None:
        """Wire the degrade notifier after construction (channels exist later)."""
        self._on_degrade = cb

    def set_recover(self, cb: Callable[[], Awaitable[None]] | None) -> None:
        """Wire the recovery callback (reconcile) after construction."""
        self._on_recover = cb

    def mark_degraded(self, reason: str) -> None:
        """Flip to degraded (idempotent) and ensure the recovery probe runs."""
        if not self.degraded:
            self.degraded = True
            self._reason = reason
            logger.warning("storage: degraded — %s", reason)
            if self._on_degrade is not None:
                try:
                    self._on_degrade(reason)
                except Exception:  # noqa: BLE001
                    logger.exception("storage: on_degrade callback failed")
        self._ensure_probe()

    def _ensure_probe(self) -> None:
        if self._probe_task is None or self._probe_task.done():
            self._probe_task = asyncio.create_task(self._probe_loop(), name="storage-health-probe")

    async def _probe_loop(self) -> None:
        while self.degraded:
            await asyncio.sleep(self._interval)
            try:
                async with self._pool.connection() as conn:
                    await conn.execute("SELECT 1")
            except PG_RUNTIME_ERRORS:
                continue  # still down — keep probing
            except Exception:  # noqa: BLE001
                logger.exception("storage: health probe error; will retry")
                continue
            # Postgres answered — recover.
            self.degraded = False
            self._reason = None
            logger.info("storage: postgres recovered")
            if self._on_recover is not None:
                try:
                    await self._on_recover()
                except Exception:  # noqa: BLE001
                    logger.exception("storage: reconcile after recovery failed")
            return

    async def stop(self) -> None:
        if self._probe_task is not None and not self._probe_task.done():
            self._probe_task.cancel()
            try:
                await self._probe_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._probe_task = None
