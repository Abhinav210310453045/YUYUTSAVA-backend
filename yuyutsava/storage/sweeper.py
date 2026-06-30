"""Unified TTL sweeper for checkpoints, on-disk blobs, and event payloads.

Replaces three separate background sweepers that used to live under
``daemon/``:

  - ``daemon/checkpointing.py:CheckpointerManager`` — sweep half (the saver
    lifecycle owner remains in daemon as a slim :class:`CheckpointerSaver`).
  - ``daemon/blob_sweeper.py:BlobSweeper`` — webcam JPEGs + matching DB rows.
  - ``daemon/events_sweeper.py:EventsSweeper`` — non-blob ``event_payloads``
    rows older than the retention window.

The three policies shared the same shape (loop on an interval, run a delete,
log if non-zero) but had independent intervals, configs, and error handling.
Folding them behind one :class:`UnifiedSweeper` means **one** place to tune
TTL policy and **one** task in the daemon's loop set.

Design choices:

- Single loop interval. Defaults to 5 min; the events sweeper used to run on
  a 1-hour cadence but the DELETE is a no-op once the table is caught up so
  the extra ticks are cheap.
- Per-blob-target TTL. Different sources can register different retention
  windows on the same loop (webcam frames = 1h, future audio clips = 30m,
  etc).
- Typed :class:`SweepReport` is returned by :meth:`sweep_once` and logged on
  every tick — a stable signal that the sweeper is alive without trying to
  parse three different log lines.

Lifecycle: implements ``async def run(self, stop_event)`` so it slots into
the daemon's main loop set alongside ``TriageLoop`` and ``OrchestratorLoop``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from yuyutsava.storage.events import Store
from yuyutsava.storage.ids import parse_thread_id_ts

logger = logging.getLogger("yuyutsava.storage.sweeper")


@dataclass(frozen=True)
class SweeperConfig:
    """Tunables shared by every sweep target.

    Per-target blob TTLs live on :class:`BlobSweepTarget` — different blob
    directories can age at different rates without affecting the global loop.
    """

    # How long a LangGraph checkpoint row sticks around before the sweeper
    # deletes it (parsed from the thread_id minted at create time).
    checkpoint_ttl_sec: int = 3600

    # Non-blob event_payloads rows past this age are deleted.
    event_ttl_sec: int = 7 * 24 * 3600

    # Offloaded tool-result artifacts past this age are deleted. Artifacts
    # are scratch (referenced from compaction summaries by id) — anything a
    # task still needs after a week should have been written to a file.
    artifact_ttl_sec: int = 7 * 24 * 3600

    # Loop interval — one tick of every target per period.
    sweep_interval_sec: int = 300


@dataclass(frozen=True)
class SweepReport:
    """One tick's deletion counters, returned by :meth:`UnifiedSweeper.sweep_once`."""

    checkpoints_deleted: int = 0
    blob_files_deleted: int = 0
    blob_rows_deleted: int = 0
    event_rows_deleted: int = 0
    artifact_rows_deleted: int = 0

    @property
    def total(self) -> int:
        return (
            self.checkpoints_deleted
            + self.blob_files_deleted
            + self.blob_rows_deleted
            + self.event_rows_deleted
            + self.artifact_rows_deleted
        )


@dataclass(frozen=True)
class BlobSweepTarget:
    """One on-disk directory + TTL pair the sweeper supervises.

    Files older than ``ttl_sec`` (by mtime) are deleted, along with the
    matching ``event_payloads`` rows whose ``blob_path`` lives under
    ``directory/``. Directories are *never* removed — only files inside.

    The deepface enrolled-faces store at ``~/.yuyutsava/deepface/`` is in a
    sibling directory entirely and is never registered here: that's user data
    with indefinite retention; blobs are scratch.
    """

    name: str           # human label, e.g. "webcam"
    directory: Path     # absolute path; files older than ttl_sec get deleted
    ttl_sec: int        # delete files whose mtime is older than (now - ttl_sec)
    glob: str = "*"     # restrict to a glob within ``directory`` (e.g. "*.jpg")


class UnifiedSweeper:
    """Coordinates checkpoint + blob + event TTL sweeps behind one loop.

    The LangGraph saver lifecycle (open async ctx, close on shutdown) is owned
    by :class:`yuyutsava.daemon.checkpointing.CheckpointerSaver` — this class
    only borrows the live saver to sweep its checkpoint rows.
    """

    def __init__(
        self,
        *,
        store: Store,
        checkpoint_saver: BaseCheckpointSaver,
        blob_targets: list[BlobSweepTarget] | None = None,
        config: SweeperConfig | None = None,
        artifact_store: object | None = None,  # context.artifacts.ArtifactStore
    ) -> None:
        self._store = store
        self._saver = checkpoint_saver
        self._blob_targets = list(blob_targets or [])
        self._config = config or SweeperConfig()
        self._artifact_store = artifact_store

        # Make sure every registered blob directory exists so the first sweep
        # doesn't log a FileNotFoundError on a fresh install.
        for t in self._blob_targets:
            t.directory.mkdir(parents=True, exist_ok=True)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Loop until ``stop_event`` is set, calling :meth:`sweep_once` each tick.

        Mirrors the ``TriageLoop`` / ``OrchestratorLoop`` shape so the daemon
        can hold a single ``list[Loop]`` and ``asyncio.create_task`` over it.
        """
        cfg = self._config
        logger.info(
            "sweeper: checkpoint_ttl=%ss, event_ttl=%ss, blob_targets=%d, every %ss",
            cfg.checkpoint_ttl_sec, cfg.event_ttl_sec, len(self._blob_targets),
            cfg.sweep_interval_sec,
        )
        for t in self._blob_targets:
            logger.info("  blob: %s @ %s (ttl=%ss, glob=%s)",
                        t.name, t.directory, t.ttl_sec, t.glob)

        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=cfg.sweep_interval_sec,
                )
                return  # stop requested
            except asyncio.TimeoutError:
                pass
            try:
                report = await self.sweep_once()
                # Log every tick so daemon health is visible; counters are 0
                # when there's nothing to delete.
                logger.info(
                    "sweeper: checkpoints=%d blob_files=%d blob_rows=%d event_rows=%d",
                    report.checkpoints_deleted, report.blob_files_deleted,
                    report.blob_rows_deleted, report.event_rows_deleted,
                )
            except Exception:
                logger.exception("sweeper: tick failed")

    async def sweep_once(self) -> SweepReport:
        """Run every sweep once and return a typed counter report."""
        checkpoints_deleted = await self._sweep_checkpoints()
        blob_files_deleted, blob_rows_deleted = await self._sweep_blobs()
        event_rows_deleted = await self._sweep_events()
        artifact_rows_deleted = await self._sweep_artifacts()
        return SweepReport(
            checkpoints_deleted=checkpoints_deleted,
            blob_files_deleted=blob_files_deleted,
            blob_rows_deleted=blob_rows_deleted,
            event_rows_deleted=event_rows_deleted,
            artifact_rows_deleted=artifact_rows_deleted,
        )

    # ------------------------------------------------------------------
    # Sweep targets
    # ------------------------------------------------------------------

    async def _sweep_checkpoints(self) -> int:
        """Delete every LangGraph thread whose minted timestamp is older than TTL.

        Threads with unparseable timestamps (e.g. external callers) are left
        in place — the format ``<role>-<unix_ts>-<uuid>`` is load-bearing here
        and ``parse_thread_id_ts`` returns None for non-conforming ids.

        Enumeration dispatches on saver type (SQLite vs Postgres); deletion
        goes through the shared ``adelete_thread`` API either way.
        """
        cutoff = time.time() - self._config.checkpoint_ttl_sec
        try:
            thread_ids = await self._enumerate_thread_ids()
        except Exception:
            logger.exception("sweeper: failed to enumerate stale checkpoints")
            return 0
        stale = [
            tid for tid in thread_ids
            if (ts := parse_thread_id_ts(tid)) is not None and ts < cutoff
        ]
        deleted = 0
        for tid in stale:
            try:
                await self._saver.adelete_thread(tid)
                deleted += 1
            except Exception:
                logger.exception("sweeper: failed to delete thread %r", tid)
        return deleted

    async def _enumerate_thread_ids(self) -> list[str]:
        """List distinct checkpoint thread_ids for the active saver backend."""
        if isinstance(self._saver, AsyncSqliteSaver):
            out: list[str] = []
            async with self._saver.lock, self._saver.conn.execute(
                "SELECT DISTINCT thread_id FROM checkpoints"
            ) as cur:
                async for (tid,) in cur:
                    out.append(tid)
            return out

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        if isinstance(self._saver, AsyncPostgresSaver):
            # _cursor() is the saver's own pool-vs-connection abstraction;
            # there is no public query surface, and the table name is part
            # of the package's stable migration contract.
            async with self._saver._cursor() as cur:  # noqa: SLF001
                await cur.execute("SELECT DISTINCT thread_id FROM checkpoints")
                rows = await cur.fetchall()
            return [r["thread_id"] if isinstance(r, dict) else r[0] for r in rows]

        logger.warning(
            "sweeper: unknown checkpointer type %s — skipping checkpoint sweep",
            type(self._saver).__name__,
        )
        return []

    async def _sweep_blobs(self) -> tuple[int, int]:
        """Run one pass over every registered blob target. Returns (files, rows)."""
        if not self._blob_targets:
            return 0, 0
        loop = asyncio.get_running_loop()
        total_files = 0
        total_rows = 0
        for target in self._blob_targets:
            # File I/O off the event loop — directories with thousands of
            # entries can block for tens of ms otherwise.
            removed_files = await loop.run_in_executor(
                None, self._sweep_target_files, target,
            )
            removed_rows = await self._store.delete_event_payloads_with_blob_prefix(
                str(target.directory) + "/",
                time.time() - target.ttl_sec,
            )
            total_files += removed_files
            total_rows += removed_rows
        return total_files, total_rows

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
                logger.debug("sweeper: unlink %s failed", path, exc_info=True)
        return removed

    async def _sweep_artifacts(self) -> int:
        """Delete offloaded tool-result artifacts older than their TTL."""
        if self._artifact_store is None:
            return 0
        cutoff = time.time() - self._config.artifact_ttl_sec
        try:
            return await self._artifact_store.delete_older_than(cutoff)  # type: ignore[attr-defined]
        except Exception:
            logger.exception("sweeper: artifact sweep failed")
            return 0

    async def _sweep_events(self) -> int:
        """Delete non-blob ``event_payloads`` rows older than the retention window.

        Blob-backed rows (``blob_path IS NOT NULL``) are skipped — those are
        owned by :meth:`_sweep_blobs`, which ties their deletion to on-disk
        file removal.
        """
        cutoff = time.time() - self._config.event_ttl_sec
        return await self._store.delete_event_payloads_older_than(cutoff)
