"""
In-process async pub/sub for events.

Sources publish ``EventEnvelope``s; consumers subscribe with a topic glob.
No external broker — single asyncio process, single machine. The bus is
the thing the triage loop and any debug consumers attach to.

Topics are dotted (``fs.changed``, ``clipboard.copied``, ``voice.wake``).
Subscriptions match via ``fnmatch.fnmatchcase`` so ``fs.*`` works.

Drop policy: each subscriber owns a bounded ``asyncio.Queue``. If the
queue is full when ``publish`` runs, the new event is **dropped for that
subscriber** and the loss is logged — slow consumers can't stall sources.
"""

from __future__ import annotations

import asyncio
import dataclasses
import fnmatch
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator

logger = logging.getLogger("yuyutsava.events.bus")


@dataclass(frozen=True)
class EventEnvelope:
    """Tiny by-value record passed to every subscriber.

    Large blobs live in the SQLite store / blobs dir, referenced by
    ``payload_ref``. Keep this struct small — it ends up in LLM prompts.
    """

    event_id: str             # ULID
    topic: str                # dotted, e.g. "fs.changed"
    source: str               # source name, e.g. "fs"
    ts: float                 # epoch seconds
    severity: int             # 0 trace, 1 info, 2 notable, 3 urgent
    summary: str              # <=120 chars; what triage and orchestrator see
    payload_ref: str          # "sqlite://event_payloads/<id>" or "file://..."
    hints: dict[str, str] = field(default_factory=dict)


@dataclass
class _Subscription:
    pattern: str
    queue: asyncio.Queue[EventEnvelope]
    dropped: int = 0


class EventBus:
    """Async pub/sub. Construct once per daemon.

    Call ``close()`` at shutdown to wake every subscriber's ``async for``
    so they exit cleanly without waiting for one more event.
    """

    def __init__(self, *, queue_size: int = 256) -> None:
        self._subs: list[_Subscription] = []
        self._lock = asyncio.Lock()
        self._queue_size = queue_size
        self._closed = False

    async def publish(self, ev: EventEnvelope) -> None:
        if self._closed:
            return
        # Snapshot under lock so a concurrent subscribe doesn't race.
        async with self._lock:
            subs = list(self._subs)
        for sub in subs:
            if not fnmatch.fnmatchcase(ev.topic, sub.pattern):
                continue
            try:
                sub.queue.put_nowait(ev)
            except asyncio.QueueFull:
                sub.dropped += 1
                if sub.dropped % 100 == 1:
                    logger.warning(
                        "EventBus: dropped %d events for pattern %r (slow consumer)",
                        sub.dropped, sub.pattern,
                    )

    async def close(self) -> None:
        """Wake all subscribers so their ``async for`` loops exit.

        Each subscriber gets a single ``None`` sentinel — the iterator
        catches it and breaks. Safe to call repeatedly.
        """
        self._closed = True
        async with self._lock:
            subs = list(self._subs)
        for sub in subs:
            try:
                sub.queue.put_nowait(None)  # type: ignore[arg-type]
            except asyncio.QueueFull:
                # Drain one and retry; close should always succeed.
                try:
                    sub.queue.get_nowait()
                except Exception:
                    pass
                try:
                    sub.queue.put_nowait(None)  # type: ignore[arg-type]
                except Exception:
                    pass

    async def subscribe(self, pattern: str = "**") -> AsyncIterator[EventEnvelope]:
        """Yield events matching ``pattern`` until the bus is closed or the
        consumer task is cancelled.
        """
        sub = _Subscription(pattern=pattern, queue=asyncio.Queue(maxsize=self._queue_size))
        async with self._lock:
            self._subs.append(sub)
        try:
            while True:
                ev = await sub.queue.get()
                if ev is None:  # close sentinel
                    return
                yield ev
        finally:
            async with self._lock:
                if sub in self._subs:
                    self._subs.remove(sub)


def make_envelope(
    *,
    topic: str,
    source: str,
    summary: str,
    payload_ref: str,
    severity: int = 1,
    hints: dict[str, str] | None = None,
    event_id: str | None = None,
    ts: float | None = None,
) -> EventEnvelope:
    """Build an envelope. ``event_id`` defaults to a fresh ULID."""
    from ulid import ULID  # local import keeps optional dep out of cold paths
    return EventEnvelope(
        event_id=event_id or str(ULID()),
        topic=topic,
        source=source,
        ts=ts if ts is not None else time.time(),
        severity=severity,
        summary=(summary or "")[:120],
        payload_ref=payload_ref,
        hints=dict(hints or {}),
    )
