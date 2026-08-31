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
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from yuyutsava.storage.events import Store
from yuyutsava.storage.events.roles import EventPayloadSweeper
from yuyutsava.storage.ids import parse_thread_id_ts
from yuyutsava.storage.paths import blobs_dir

logger = logging.getLogger("yuyutsava.storage.sweeper")

# Gregorian epoch (1582-10-15) to Unix epoch, in seconds — the offset UUIDv6
# timestamps are counted from, in 100-nanosecond ticks.
_GREGORIAN_OFFSET_SEC = 12_219_292_800


def _checkpoint_id_ts(checkpoint_id: str) -> float | None:
    """Unix seconds encoded in a LangGraph ``checkpoint_id``, or None.

    Checkpoint ids are UUIDv6: the 60-bit timestamp is split across the top 64
    bits as 48 high bits, the 4-bit version, then 12 low bits. Anything that
    isn't a v6 uuid (a hand-written id, a future format) yields None so the
    caller can fall back rather than treat it as epoch-zero and delete it.
    """
    try:
        value = uuid.UUID(checkpoint_id)
    except (AttributeError, TypeError, ValueError):
        return None
    if value.version != 6:
        return None
    ticks = (value.int >> 80) << 12 | (value.int >> 64) & 0xFFF
    return ticks / 1e7 - _GREGORIAN_OFFSET_SEC


def _last_write_ts(thread_id: str, latest_checkpoint_id: str | None) -> float | None:
    """When *thread_id* last wrote a checkpoint, in Unix seconds, or None.

    Prefers the newest checkpoint's own timestamp (real activity) and falls
    back to the thread id's minted timestamp (creation) only when the id can't
    be decoded. None means "unknown" — the sweeper leaves those alone.
    """
    if latest_checkpoint_id:
        ts = _checkpoint_id_ts(latest_checkpoint_id)
        if ts is not None:
            return ts
    return parse_thread_id_ts(thread_id)


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

    # TODO-board workspaces are durable user data with NO TTL — the sweep only
    # removes blobs/todoboard/<dir> dirs whose card row is gone. The grace
    # window keeps a freshly-created dir alive across the exchange's
    # mkdir-before-row-insert ordering.
    todo_orphan_grace_sec: int = 3600


@dataclass(frozen=True)
class SweepReport:
    """One tick's deletion counters, returned by :meth:`UnifiedSweeper.sweep_once`."""

    checkpoints_deleted: int = 0
    blob_files_deleted: int = 0
    blob_rows_deleted: int = 0
    event_rows_deleted: int = 0
    artifact_rows_deleted: int = 0
    todo_orphan_dirs_deleted: int = 0

    @property
    def total(self) -> int:
        return (
            self.checkpoints_deleted
            + self.blob_files_deleted
            + self.blob_rows_deleted
            + self.event_rows_deleted
            + self.artifact_rows_deleted
            + self.todo_orphan_dirs_deleted
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
        store: EventPayloadSweeper,
        checkpoint_saver: BaseCheckpointSaver,
        blob_targets: list[BlobSweepTarget] | None = None,
        config: SweeperConfig | None = None,
        artifact_store: object | None = None,  # context.artifacts.ArtifactStore
        todo_exchange: object | None = None,  # todoboard.exchange.TodoExchange
        storage_health: object | None = None,  # storage.routing.health.StorageHealth
    ) -> None:
        self._store = store
        self._saver = checkpoint_saver
        self._blob_targets = list(blob_targets or [])
        self._config = config or SweeperConfig()
        self._artifact_store = artifact_store
        self._todo_exchange = todo_exchange
        self._storage_health = storage_health

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
                    "sweeper: checkpoints=%d blob_files=%d blob_rows=%d event_rows=%d todo_orphans=%d",
                    report.checkpoints_deleted, report.blob_files_deleted,
                    report.blob_rows_deleted, report.event_rows_deleted,
                    report.todo_orphan_dirs_deleted,
                )
            except Exception:
                logger.exception("sweeper: tick failed")

    async def sweep_once(self) -> SweepReport:
        """Run every sweep once and return a typed counter report."""
        checkpoints_deleted = await self._sweep_checkpoints()
        blob_files_deleted, blob_rows_deleted = await self._sweep_blobs()
        event_rows_deleted = await self._sweep_events()
        artifact_rows_deleted = await self._sweep_artifacts()
        todo_orphan_dirs_deleted = await self._sweep_todo_orphans()
        return SweepReport(
            checkpoints_deleted=checkpoints_deleted,
            blob_files_deleted=blob_files_deleted,
            blob_rows_deleted=blob_rows_deleted,
            event_rows_deleted=event_rows_deleted,
            artifact_rows_deleted=artifact_rows_deleted,
            todo_orphan_dirs_deleted=todo_orphan_dirs_deleted,
        )

    # ------------------------------------------------------------------
    # Sweep targets
    # ------------------------------------------------------------------

    async def _sweep_checkpoints(self) -> int:
        """Delete every LangGraph thread whose LAST WRITE is older than TTL.

        Staleness is measured from the newest checkpoint the thread wrote, not
        from when its id was minted: a session the user is still typing into is
        not stale no matter how long ago it was opened. Keying off creation
        time deleted live conversations mid-session — every sweep tick wiped
        the checkpoint of any chat older than the TTL, so each turn restarted
        from an empty history (observed on a ~4h CLI session: input tokens
        reset to the system-prompt baseline on every turn).

        LangGraph mints ``checkpoint_id`` as a UUIDv6, whose embedded
        timestamp *is* the write time, so last-activity needs no extra table
        or dependency. Threads whose id cannot be decoded fall back to the
        minted ``<role>-<unix_ts>-<uuid>`` thread-id timestamp, and threads
        with neither are left in place (external callers own their retention).

        Enumeration dispatches on saver type (SQLite vs Postgres); deletion
        goes through the shared ``adelete_thread`` API either way.
        """
        cutoff = time.time() - self._config.checkpoint_ttl_sec
        try:
            threads = await self._enumerate_threads()
        except Exception:
            logger.exception("sweeper: failed to enumerate stale checkpoints")
            return 0
        stale = [
            tid for tid, latest_id in threads
            if (ts := _last_write_ts(tid, latest_id)) is not None and ts < cutoff
        ]
        deleted = 0
        for tid in stale:
            try:
                await self._saver.adelete_thread(tid)
                deleted += 1
            except Exception:
                logger.exception("sweeper: failed to delete thread %r", tid)
        return deleted

    async def _enumerate_threads(self) -> list[tuple[str, str]]:
        """List ``(thread_id, newest checkpoint_id)`` for the active backend.

        ``max(checkpoint_id)`` is the thread's most recent write across every
        checkpoint namespace: UUIDv6 sorts lexicographically by time, which is
        exactly why LangGraph uses it for checkpoint ids.
        """
        query = (
            "SELECT thread_id, max(checkpoint_id) AS latest "
            "FROM checkpoints GROUP BY thread_id"
        )
        if isinstance(self._saver, AsyncSqliteSaver):
            out: list[tuple[str, str]] = []
            async with self._saver.lock, self._saver.conn.execute(query) as cur:
                async for tid, latest in cur:
                    out.append((tid, latest))
            return out

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        if isinstance(self._saver, AsyncPostgresSaver):
            # _cursor() is the saver's own pool-vs-connection abstraction;
            # there is no public query surface, and the table name is part
            # of the package's stable migration contract.
            async with self._saver._cursor() as cur:  # noqa: SLF001
                await cur.execute(query)
                rows = await cur.fetchall()
            return [
                (r["thread_id"], r["latest"]) if isinstance(r, dict) else (r[0], r[1])
                for r in rows
            ]

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

    async def _sweep_todo_orphans(self) -> int:
        """Remove ``blobs/todoboard/<dir>`` workspaces whose card row is gone.

        No TTL — card workspaces are durable user data; only true orphans go
        (a delete_card whose rmtree failed, or a crash between the exchange's
        mkdir and its row insert — the grace window covers the latter). Card
        ids come from the exchange, never raw SQL. Two hard safety rules:
        skip entirely while storage is degraded (the SQLite buffer's card list
        is partial and would misclassify live workspaces), and skip when the
        id listing fails — deleting on incomplete knowledge is never OK.
        """
        if self._todo_exchange is None:
            return 0
        if self._storage_health is not None and getattr(self._storage_health, "degraded", False):
            return 0
        root = blobs_dir() / "todoboard"
        if not root.is_dir():
            return 0
        try:
            live = set(await self._todo_exchange.list_card_ids())  # type: ignore[attr-defined]
        except Exception:
            logger.exception("sweeper: could not list TODO card ids — skipping orphan sweep")
            return 0
        cutoff = time.time() - self._config.todo_orphan_grace_sec

        def _remove_orphans() -> int:
            removed = 0
            for entry in root.iterdir():
                if not entry.is_dir() or entry.name in live:
                    continue
                try:
                    if entry.stat().st_mtime >= cutoff:
                        continue  # inside the grace window
                    shutil.rmtree(entry)
                    removed += 1
                    logger.info("sweeper: removed orphan TODO workspace %s", entry)
                except OSError:
                    logger.debug("sweeper: rmtree %s failed", entry, exc_info=True)
            return removed

        return await asyncio.get_running_loop().run_in_executor(None, _remove_orphans)
