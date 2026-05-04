"""
Source registry: discover source classes, manage their lifecycle, restart on
failure with exponential backoff.

Also hosts the **pre-LLM rule layer**: per-source ignore patterns, content
hash dedup, severity floor. Events that fail these rules never reach triage,
which keeps both latency and LLM cost bounded.

Sources are registered via the ``DEFAULT_SOURCES`` list at import time. Add
a new source by importing its class and appending. The registry is
intentionally not plugin-discovered (entry-point discovery is overkill for
the in-tree set we ship).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from yuyutsava.core.config import EventsConfig, SourceConfig
from yuyutsava.events.bus import EventBus
from yuyutsava.events.source import EventSource, SourceContext
from yuyutsava.events.store import Store

logger = logging.getLogger("yuyutsava.events.registry")


# Map of source name → factory that builds a fresh instance.
# Each entry is added by the source module via ``register_source``.
_SOURCE_FACTORIES: dict[str, Callable[[], EventSource]] = {}


def register_source(name: str, factory: Callable[[], EventSource]) -> None:
    """Register a source factory by name. Called by source modules at import time."""
    _SOURCE_FACTORIES[name] = factory


def _import_builtin_sources() -> None:
    """Import bundled sources so their ``register_source`` calls run."""
    # Local imports avoid a circular dep at package load time.
    from yuyutsava.events.sources import fs as _fs  # noqa: F401


class SourceRegistry:
    """Owns a SourceContext per active source and supervises their lifecycle."""

    def __init__(self, bus: EventBus, store: Store, config: EventsConfig) -> None:
        _import_builtin_sources()
        self._bus = bus
        self._store = store
        self._config = config
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._sources: dict[str, EventSource] = {}
        self._cancelled: dict[str, asyncio.Event] = {}

    async def start_all(self) -> None:
        for name, src_cfg in self._config.sources.items():
            if not src_cfg.enabled:
                logger.info("source %s disabled in config; skipping", name)
                continue
            factory = _SOURCE_FACTORIES.get(name)
            if factory is None:
                logger.warning("source %s in config but no factory registered", name)
                continue
            await self._start_one(name, src_cfg, factory)

    async def _start_one(
        self,
        name: str,
        src_cfg: SourceConfig,
        factory: Callable[[], EventSource],
    ) -> None:
        source = factory()
        self._sources[name] = source
        cancelled = asyncio.Event()
        self._cancelled[name] = cancelled
        ctx = SourceContext(
            name=name,
            bus=self._bus,
            store=self._store,
            params=src_cfg.params,
            cancelled=cancelled,
        )
        self._tasks[name] = asyncio.create_task(
            self._run_with_backoff(name, source, ctx),
            name=f"source-{name}",
        )

    async def _run_with_backoff(
        self, name: str, source: EventSource, ctx: SourceContext
    ) -> None:
        delay = 1.0
        failures = 0
        while not ctx.cancelled.is_set():
            try:
                logger.info("starting source %s", name)
                await source.start(ctx)
                # If start() returns cleanly without cancellation, treat as
                # graceful end and stop respawning.
                if not ctx.cancelled.is_set():
                    logger.info("source %s exited cleanly; not respawning", name)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                logger.exception("source %s crashed (attempt %d)", name, failures)
                if failures >= 5:
                    logger.error(
                        "source %s quarantined after %d failures; "
                        "fix config or restart daemon", name, failures,
                    )
                    return
                await asyncio.sleep(min(delay, 60.0))
                delay = min(delay * 2, 60.0)

    async def stop_all(self) -> None:
        for name, source in list(self._sources.items()):
            self._cancelled[name].set()
            try:
                await asyncio.wait_for(source.stop(), timeout=2.0)
            except Exception:
                logger.exception("source %s stop() failed", name)
        for name, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
