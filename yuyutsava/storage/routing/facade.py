"""``RoutedStore`` — a transparent spillover proxy over a twin pair.

Wraps a Postgres *primary* and a SQLite *buffer* sharing one
:class:`~yuyutsava.storage.routing.health.StorageHealth`. Any ``async`` method
on the primary is proxied: when healthy it runs on Postgres; on a Postgres
runtime error it marks the process degraded and re-runs the same call against
the SQLite buffer (so the write is never lost). While degraded, calls go
straight to the buffer until the health probe recovers and reconcile drains it.

Generic by design (``__getattr__``) so it works for every domain twin —
events, consent, interrupts, memory, skills — without per-method boilerplate.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from yuyutsava.storage.routing.errors import PG_RUNTIME_ERRORS
from yuyutsava.storage.routing.health import StorageHealth

logger = logging.getLogger("yuyutsava.storage.routing.facade")


class RoutedStore:
    def __init__(self, primary: Any, buffer: Any, health: StorageHealth, *, name: str = "") -> None:
        self._primary = primary
        self._buffer = buffer
        self._health = health
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        # Only triggered for names not found on the instance, so the private
        # fields above never recurse. Guard dunder/private lookups anyway.
        if attr.startswith("_"):
            raise AttributeError(attr)
        primary_attr = getattr(self._primary, attr)
        if not asyncio.iscoroutinefunction(primary_attr):
            # Sync attribute/method or plain value — no failover semantics.
            return primary_attr
        buffer_attr = getattr(self._buffer, attr)

        async def _wrapped(*args: Any, **kwargs: Any) -> Any:
            if self._health.degraded:
                return await buffer_attr(*args, **kwargs)
            try:
                return await primary_attr(*args, **kwargs)
            except PG_RUNTIME_ERRORS as exc:
                self._health.mark_degraded(f"{self._name}.{attr}: {exc}")
                return await buffer_attr(*args, **kwargs)

        _wrapped.__name__ = attr
        return _wrapped
