"""
Filesystem event source.

Watches one or more roots via ``watchdog`` and emits a coalesced ``fs.changed``
event after each burst settles. Coalescing matters because editors typically
emit several events per save (created, modified, attribute_changed) and
because tools like ``npm install`` emit thousands.

Config (params from ``events_config.json``):

    {
      "roots": ["~/Downloads"],
      "ignore": ["*.tmp", ".DS_Store", "*.crdownload", "*.part"],
      "coalesce_window_ms": 2000
    }

The watchdog ``Observer`` runs on a background thread; events cross into
asyncio via ``loop.call_soon_threadsafe``. Getting that bridge wrong drops
events silently — so all bookkeeping happens on the asyncio side.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from pathlib import Path
from typing import Any, Callable

from watchdog.events import (
    DirCreatedEvent, DirDeletedEvent, DirModifiedEvent, DirMovedEvent,
    FileCreatedEvent, FileDeletedEvent, FileModifiedEvent, FileMovedEvent,
    FileSystemEvent, FileSystemEventHandler,
)
from watchdog.observers import Observer

from yuyutsava.events.registry import register_source
from yuyutsava.events.source import EventSource, SourceContext

logger = logging.getLogger("yuyutsava.events.sources.fs")


_DEFAULT_IGNORE = ("*.tmp", ".DS_Store", "*.crdownload", "*.part", "*~")


class _CoalescingHandler(FileSystemEventHandler):
    """Watchdog callback running on Observer thread; trampolines into asyncio."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        callback: Callable[[FileSystemEvent], None],
    ) -> None:
        self._loop = loop
        self._callback = callback

    def on_any_event(self, event: FileSystemEvent) -> None:
        try:
            self._loop.call_soon_threadsafe(self._callback, event)
        except RuntimeError:
            # Loop closed during shutdown.
            pass


class FsSource(EventSource):
    """Filesystem source. Coalesces bursts and applies ignore globs."""

    name = "fs"
    topics = ("fs.changed",)

    def __init__(self) -> None:
        self._observer: Observer | None = None  # type: ignore[type-arg]
        self._pending: dict[Path, dict[str, Any]] = {}
        self._flush_task: asyncio.Task[None] | None = None

    async def start(self, ctx: SourceContext) -> None:
        roots_raw = ctx.params.get("roots") or []
        if not roots_raw:
            logger.warning("fs source: no roots configured; nothing to watch")
            await ctx.cancelled.wait()
            return

        roots = [Path(str(r)).expanduser().resolve() for r in roots_raw]
        ignore = tuple(ctx.params.get("ignore") or _DEFAULT_IGNORE)
        coalesce_ms = int(ctx.params.get("coalesce_window_ms", 2000))
        # heartbeat_sec: idle sleep injected between event bursts. 0 = disabled.
        heartbeat_sec = int(ctx.params.get("heartbeat_sec", 30))

        loop = asyncio.get_running_loop()
        observer = Observer()
        self._observer = observer

        def on_raw(ev: FileSystemEvent) -> None:
            # Runs on the asyncio loop. Filter then schedule a flush.
            try:
                self._on_raw_event(ev, ignore, coalesce_ms, ctx)
            except Exception:
                logger.exception("fs source: handler raised")

        handler = _CoalescingHandler(loop, on_raw)
        for root in roots:
            if not root.exists():
                logger.warning("fs source: watch root %s does not exist; creating", root)
                root.mkdir(parents=True, exist_ok=True)
            observer.schedule(handler, str(root), recursive=True)
            logger.info("[fs] watching %s (ignore=%s)", root, list(ignore))
        observer.start()
        # Surface a UI-visible signal that the watcher is live. The bus topic
        # is informational; downstream consumers (triage) ignore source.* by
        # default — it's there for the activity tab / health checks.
        try:
            await ctx.emit(
                topic="source.started",
                summary=f"fs watching {', '.join(str(r) for r in roots)}",
                payload={"roots": [str(r) for r in roots], "ignore": list(ignore)},
                severity=0,
                hints={"source": "fs"},
            )
        except Exception:
            logger.debug("fs source: source.started emit failed", exc_info=True)

        try:
            if heartbeat_sec <= 0:
                await ctx.cancelled.wait()
            else:
                # Poll in heartbeat_sec intervals so the daemon sleeps between
                # bursts instead of spinning on the event loop.
                while not ctx.cancelled.is_set():
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(ctx.cancelled.wait()),
                            timeout=float(heartbeat_sec),
                        )
                    except asyncio.TimeoutError:
                        # Normal heartbeat tick — log only at DEBUG level to avoid noise.
                        logger.debug("fs source: heartbeat tick (%ds)", heartbeat_sec)
        finally:
            self._observer = None
            try:
                observer.stop()
                observer.join(timeout=2.0)
            except Exception:
                pass

    def _on_raw_event(
        self,
        ev: FileSystemEvent,
        ignore: tuple[str, ...],
        coalesce_ms: int,
        ctx: SourceContext,
    ) -> None:
        # Skip directory churn — we care about files in MVP.
        if isinstance(ev, (DirCreatedEvent, DirDeletedEvent, DirModifiedEvent, DirMovedEvent)):
            return

        path_str = ev.dest_path if isinstance(ev, FileMovedEvent) and ev.dest_path else ev.src_path
        if not path_str:
            return
        path = Path(path_str)
        name = path.name
        if any(fnmatch.fnmatchcase(name, pat) for pat in ignore):
            return

        kind = self._kind(ev)
        prev = self._pending.get(path)
        if prev is None:
            self._pending[path] = {"first_kind": kind, "last_kind": kind, "count": 1}
        else:
            prev["last_kind"] = kind
            prev["count"] += 1

        # Schedule one flush per burst window.
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(
                self._flush_after(coalesce_ms, ctx),
                name="fs-flush",
            )

    @staticmethod
    def _kind(ev: FileSystemEvent) -> str:
        if isinstance(ev, FileCreatedEvent):
            return "created"
        if isinstance(ev, FileDeletedEvent):
            return "deleted"
        if isinstance(ev, FileMovedEvent):
            return "moved"
        if isinstance(ev, FileModifiedEvent):
            return "modified"
        return "changed"

    async def _flush_after(self, coalesce_ms: int, ctx: SourceContext) -> None:
        await asyncio.sleep(coalesce_ms / 1000.0)
        # Snapshot and clear FIRST so a fresh task can be scheduled for any
        # events that arrive while we're emitting. Without this, an event
        # landing between the snapshot and `_flush_task.done()` becoming True
        # would be stranded in _pending until the next event arrived.
        if not self._pending:
            self._flush_task = None
            return
        batch, self._pending = self._pending, {}
        try:
            for path, info in batch.items():
                await self._emit_one(path, info, ctx)
        finally:
            self._flush_task = None
            # If events arrived during emit, kick another flush window.
            if self._pending:
                self._flush_task = asyncio.create_task(
                    self._flush_after(coalesce_ms, ctx),
                    name="fs-flush",
                )

    async def _emit_one(
        self, path: Path, info: dict[str, Any], ctx: SourceContext
    ) -> None:
        # If the path doesn't exist now (was a create-then-delete), skip.
        kind = info["last_kind"]
        if kind != "deleted" and not path.exists():
            return

        try:
            stat = path.stat() if path.exists() else None
        except OSError:
            stat = None

        size = stat.st_size if stat else None
        ext = path.suffix.lower().lstrip(".")
        summary = f"{kind} {path.name}" + (f" ({_human_size(size)})" if size is not None else "")

        payload = {
            "path": str(path),
            "name": path.name,
            "ext": ext,
            "kind": kind,
            "first_kind": info["first_kind"],
            "burst_count": info["count"],
            "size": size,
            "parent": str(path.parent),
        }
        hints = {
            "path": str(path),
            "ext": ext,
            "kind": kind,
            "parent": str(path.parent),
        }
        await ctx.emit(
            topic="fs.changed",
            summary=summary,
            payload=payload,
            severity=1,
            hints=hints,
        )

    async def stop(self) -> None:
        if self._observer is not None:
            try:
                self._observer.stop()
            except Exception:
                pass


def _human_size(n: int | None) -> str:
    if n is None:
        return "?"
    units = ("B", "KB", "MB", "GB", "TB")
    f = float(n)
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.0f}{units[i]}" if i == 0 else f"{f:.1f}{units[i]}"


# Register on import so SourceRegistry can find us.
register_source("fs", FsSource)
