"""
``EventSource`` ABC + ``SourceContext``.

A source is anything that produces events for the bus: filesystem watcher,
clipboard polling, webcam frame capture, voice wake-word detector, hotkey
listener, …

Sources never touch the bus or the SQLite store directly. They get a
``SourceContext`` injected at start time and call ``ctx.emit(...)``.

This decouples sources from persistence and makes them testable in
isolation: a unit test passes a fake ``ctx`` that captures emits.
"""

from __future__ import annotations

from yuyutsava.storage.events.roles import EventPayloadWriter

import asyncio
import dataclasses
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from yuyutsava.events.bus import EventBus, make_envelope
from yuyutsava.storage.events import Store

logger = logging.getLogger("yuyutsava.events.source")


@dataclass
class SourceContext:
    """Runtime context handed to a source on ``start()``.

    Sources call ``await ctx.emit(...)`` to publish — the context handles
    persistence + bus publish atomically. They watch ``ctx.cancelled`` and
    exit cleanly when it's set.
    """

    name: str
    bus: EventBus
    # Narrowed from the whole events Store (Phase 2 step 2.7): a source
    # publishes payloads and does nothing else with it. Every EventSource
    # subclass receives this context, so the wide type advertised ~30 methods
    # to code that needs one.
    store: EventPayloadWriter
    params: dict[str, Any]
    cancelled: asyncio.Event

    async def emit(
        self,
        *,
        topic: str,
        summary: str,
        payload: dict[str, Any],
        severity: int = 1,
        hints: dict[str, str] | None = None,
        blob_path: str | None = None,
    ) -> str:
        """Persist payload, publish a small envelope. Returns event_id."""
        ev = make_envelope(
            topic=topic,
            source=self.name,
            summary=summary,
            payload_ref=(f"file://{blob_path}" if blob_path
                         else f"sqlite://event_payloads/<{topic}>"),
            severity=severity,
            hints=hints,
        )
        # Patch payload_ref now that we have event_id (for the sqlite case).
        if not blob_path:
            ev = dataclasses.replace(
                ev, payload_ref=f"sqlite://event_payloads/{ev.event_id}"
            )
        await self.store.put_event_payload(
            event_id=ev.event_id,
            topic=topic,
            ts=ev.ts,
            payload=payload,
            blob_path=blob_path,
        )
        await self.bus.publish(ev)
        return ev.event_id


class EventSource(ABC):
    """Abstract base for all event sources.

    Subclasses implement ``start(ctx)`` and ``stop()``. ``start`` should
    run forever (or until ``ctx.cancelled.is_set()``); the registry
    handles task lifecycle and backoff.
    """

    name: str = "unnamed"
    topics: tuple[str, ...] = ()

    @abstractmethod
    async def start(self, ctx: SourceContext) -> None:
        ...

    @abstractmethod
    async def stop(self) -> None:
        ...
