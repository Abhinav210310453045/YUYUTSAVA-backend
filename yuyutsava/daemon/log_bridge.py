"""Forward the daemon's own log records to the UI's Logs panel.

## Why

The panel called "Logs" contained one kind of line: `GET /tasks → 200 (3ms)`.
HTTP access traffic from the UI's own polling, rendered at 10px, with no level,
no subsystem and no filter. Everything the daemon actually says about itself —
model calls, provider retries, compactions, offloads, storage failover, MCP
loads, ~80 caught-and-logged exceptions — went to stderr behind a
non-propagating handler, visible only to whoever launched the process from a
terminal. And the log-level dropdown in the titlebar changed *that* stream, so
switching it to DEBUG appeared to do nothing at all.

## How

A ``logging.Handler`` on the ``yuyutsava`` logger puts records on a bounded
queue; an async task drains it and broadcasts each as an
:class:`~yuyutsava.daemon.channels.AppLogPayload` over the existing SSE hub.

Three properties this has to have:

* **it cannot block a caller.** ``Handler.emit`` runs on whatever thread
  logged, including threads with no event loop, so it only ever appends to a
  deque. All I/O happens on the drain task.
* **it cannot feed itself.** Broadcasting can log — a full subscriber queue, a
  serialization error — and that log would be broadcast, which logs. Records
  from this module and from the transport it uses are dropped on sight.
* **it drops rather than grows.** A debug-level flood must cost bounded memory
  and never stall the daemon; overflow is counted and reported once per batch
  so the gap is visible instead of silent.

The level gate is the logger's own, so the existing ``PUT /logs/level``
endpoint and its titlebar dropdown now control what reaches the panel — which
is what a user already expects them to do.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from typing import Any

logger = logging.getLogger("yuyutsava.daemon.log_bridge")

#: Records held while the drain task catches up. A debug flood costs this much
#: memory and no more.
MAX_QUEUE = 1_000

#: How long the drain task sleeps when the queue is empty.
_IDLE_SEC = 0.25

#: Loggers whose records are never forwarded, because forwarding them could
#: produce more of them. This module plus the transport it broadcasts through.
_MUTED = (
    "yuyutsava.daemon.log_bridge",
    "yuyutsava.daemon.channels",
    "yuyutsava.daemon.web.services.stream_service",
)


class _QueueHandler(logging.Handler):
    """Appends formatted records to a bounded deque. Never blocks, never raises."""

    def __init__(self, sink: deque) -> None:
        super().__init__()
        self._sink = sink
        self.dropped = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_MUTED):
            return
        try:
            message = record.getMessage()
            if record.exc_info:
                # One line: the panel is a list, not a traceback viewer. The
                # full traceback is still on stderr for whoever needs it.
                exc = record.exc_info[1]
                message = f"{message} — {type(exc).__name__}: {exc}"
            if len(self._sink) >= MAX_QUEUE:
                self.dropped += 1
                return
            self._sink.append((
                record.levelname, record.name, message,
                record.created,
            ))
        except Exception:  # noqa: BLE001 — logging must never raise at a caller
            self.dropped += 1


class LogBridge:
    """Installs the handler and drains it onto the SSE hub."""

    def __init__(self, hub: Any, *, logger_name: str = "yuyutsava") -> None:
        self._hub = hub
        self._logger_name = logger_name
        self._queue: deque = deque()
        self._handler = _QueueHandler(self._queue)
        self._task: asyncio.Task | None = None
        self._installed_on: logging.Logger | None = None

    def install(self) -> None:
        """Attach the handler and start draining. Idempotent."""
        if self._installed_on is not None:
            return
        target = logging.getLogger(self._logger_name)
        # No level of its own: the logger's level decides, so PUT /logs/level
        # controls the panel exactly as a user expects it to.
        target.addHandler(self._handler)
        self._installed_on = target
        self._task = asyncio.create_task(self._drain(), name="yuyutsava-log-bridge")

    async def aclose(self) -> None:
        if self._installed_on is not None:
            with contextlib.suppress(Exception):
                self._installed_on.removeHandler(self._handler)
            self._installed_on = None
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _drain(self) -> None:
        from yuyutsava.daemon.channels import AppLogPayload
        from yuyutsava.daemon.web.services.stream_service import StreamEventItem

        while True:
            try:
                if not self._queue:
                    await asyncio.sleep(_IDLE_SEC)
                    continue
                # Batch whatever has accumulated; one await per record would
                # make a flood slower than the flood itself.
                batch = []
                while self._queue and len(batch) < 200:
                    batch.append(self._queue.popleft())
                dropped, self._handler.dropped = self._handler.dropped, 0
                if dropped:
                    batch.append((
                        "WARNING", "yuyutsava.daemon.log_bridge",
                        f"{dropped} log line(s) dropped — the UI could not keep up",
                        time.time(),
                    ))
                for level, name, message, created in batch:
                    with contextlib.suppress(Exception):
                        await self._hub.broadcast(StreamEventItem(
                            payload=AppLogPayload(
                                level=level, logger=name, message=message,
                                ts=created,
                            ),
                        ))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the bridge outlives its own bugs
                await asyncio.sleep(1.0)


__all__ = ["MAX_QUEUE", "LogBridge"]
