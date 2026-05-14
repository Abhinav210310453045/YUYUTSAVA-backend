"""On-disk blob TTL sweeper.

Some event sources (currently only :mod:`yuyutsava.events.sources.webcam`)
write payload blobs — JPEG frames, recorded audio clips, etc. — to disk
because passing them through the SQLite payload column would bloat the
event DB and the bus envelope. The path travels through the bus; the
bytes stay on the filesystem under ``~/.yuyutsava/blobs/<source>/``.

Without a sweeper, a daemon that runs for hours produces tens of
thousands of stale frames. ``CheckpointerManager`` handles checkpoint
rows; ``Store.expire_proposals`` handles proposals; this class is the
on-disk equivalent for blob-bearing events.

Design:

- Per-target sweep policy (``BlobSweepTarget``). Each target names one
  directory and the TTL that applies to files inside it. Webcam frames
  default to a 1-hour TTL. Different sources can register different
  retention windows without coupling.
- Files are deleted strictly by mtime. The matching ``event_payloads``
  rows (where ``blob_path LIKE '<dir>/%'`` and ``ts < cutoff``) are
  removed in the same tick so the DB doesn't accumulate dangling refs.
- Directories are *never* deleted, only files inside them. Subdirs that
  are not registered are ignored — the deepface enrolled-faces store
  at ``~/.yuyutsava/deepface/`` is in a sibling directory entirely and
  is never visible to this sweeper. That separation is load-bearing:
  enrolled identities are user data with indefinite retention; webcam
  frames are scratch.

Lifecycle mirrors :class:`CheckpointerManager`: ``start()`` spawns the
background task, ``stop()`` signals + joins it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from yuyutsava.events.store import Store

logger = logging.getLogger("yuyutsava.daemon.blob_sweeper")


DEFAULT_SWEEP_INTERVAL_SEC: int = 300  # 5 minutes


@dataclass(frozen=True)
class BlobSweepTarget:
    """One directory + TTL pair the sweeper supervises."""

    name: str           # human label, e.g. "webcam"
    directory: Path     # absolute path; files older than ttl_sec get deleted
    ttl_sec: int        # delete files whose mtime is older than (now - ttl_sec)
    glob: str = "*"     # restrict to a glob within ``directory`` (e.g. "*.jpg")


class BlobSweeper:
    """Periodic sweeper for on-disk blob directories."""

    def __init__(
        self,
        store: Store,
        targets: list[BlobSweepTarget],
        *,
        sweep_interval_sec: int = DEFAULT_SWEEP_INTERVAL_SEC,
    ) -> None:
        self._store = store
        self._targets = targets
        self._sweep_interval_sec = sweep_interval_sec
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Spawn the sweeper task. No-op if no targets are configured."""
        if not self._targets:
            logger.info("blob sweeper: no targets registered; not starting")
            return
        # Ensure target dirs exist so the first sweep doesn't log a noisy
        # FileNotFoundError on a fresh install.
        for t in self._targets:
            t.directory.mkdir(parents=True, exist_ok=True)
        logger.info(
            "blob sweeper: %d target(s), sweep every %ss",
            len(self._targets), self._sweep_interval_sec,
        )
        for t in self._targets:
            logger.info("  - %s @ %s (ttl=%ss, glob=%s)",
                        t.name, t.directory, t.ttl_sec, t.glob)
        self._task = asyncio.create_task(self._loop(), name="blob-sweeper")

    async def stop(self) -> None:
        """Signal the sweeper to exit and join. Idempotent."""
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
                await self._sweep_once()
            except Exception:
                logger.exception("blob sweeper: sweep iteration failed")

    async def _sweep_once(self) -> None:
        """Run one pass over every registered target."""
        loop = asyncio.get_running_loop()
        for target in self._targets:
            # File I/O off the event loop — directories with thousands of
            # entries can block for tens of ms otherwise.
            removed_files = await loop.run_in_executor(
                None, self._sweep_target_files, target,
            )
            removed_rows = self._store.delete_event_payloads_with_blob_prefix(
                str(target.directory) + "/",
                time.time() - target.ttl_sec,
            )
            if removed_files or removed_rows:
                logger.info(
                    "blob sweeper: %s — removed %d file(s), %d row(s)",
                    target.name, removed_files, removed_rows,
                )

    @staticmethod
    def _sweep_target_files(target: BlobSweepTarget) -> int:
        """Delete files in *target.directory* matching glob and older than TTL."""
        if not target.directory.exists():
            return 0
        cutoff = time.time() - target.ttl_sec
        removed = 0
        for path in target.directory.glob(target.glob):
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                # File vanished mid-iteration or permission issue — ignore;
                # the next sweep will try again.
                logger.debug("blob sweeper: unlink %s failed", path, exc_info=True)
        return removed
