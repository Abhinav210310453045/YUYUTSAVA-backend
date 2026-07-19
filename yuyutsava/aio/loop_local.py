"""Per-event-loop instance cache — ``threading.local``, but keyed by the loop.

## Why this exists

The process runs more than one asyncio loop: the daemon/CLI main loop, plus the
AsyncSubagentHost's uvicorn loop in the ``async-subagent-host`` daemon thread
(``yuyutsava/async_subagents/host.py``). Several resources are *loop-affine* —
they bind to the event loop that first uses them and misbehave (or hard-crash
with "attached to a different loop") when touched from another:

* psycopg's ``AsyncConnectionPool`` (internal locks/waiters),
* ``httpx.AsyncClient`` (transport + anyio primitives),
* grpc.aio channels inside Gemini SDK clients,
* MCP ``ClientSession``s (anyio cancel scopes).

``LoopLocal`` fixes the sharable ones at their chokepoint: the *owner* object
(``PgPool``, ``Embedder``) stays a process-wide singleton, but its loop-affine
internals become one-per-loop, created lazily on first use from each loop.
Resources that cannot be duplicated (MCP sessions) marshal instead — see
``mcp/tool_adapter.py``. The full rules live in Architecture.md under
"Event-loop ownership".

Loops are held by weakref, so a dead loop releases its instance. Instances on
loops other than the caller's cannot be safely closed from the caller's loop —
teardown of those is best-effort by design (the host thread is a daemon thread
that dies with the process).
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Awaitable, Callable, Generic, TypeVar

T = TypeVar("T")


class LoopLocal(Generic[T]):
    """Lazy per-running-loop instance cache.

    Usage::

        clients = LoopLocal(lambda: httpx.AsyncClient(...))
        client = clients.get()          # instance for the current loop

        pools = LoopLocal[AsyncConnectionPool]()
        pool = await pools.aget(_open_pool_for_this_loop)   # async factory
    """

    def __init__(self, factory: Callable[[], T] | None = None) -> None:
        self._factory = factory
        self._instances: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, T]" = (
            weakref.WeakKeyDictionary()
        )
        # Per-loop creation locks for aget(); each Lock is created lazily on
        # its own loop, so it is never itself used cross-loop.
        self._alocks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
            weakref.WeakKeyDictionary()
        )

    def get(self) -> T:
        """Instance for the current running loop, built via the constructor's
        ``factory`` on first use. Must be called from a running loop."""
        if self._factory is None:
            raise TypeError("LoopLocal.get() requires a factory; use aget()")
        loop = asyncio.get_running_loop()
        try:
            return self._instances[loop]
        except KeyError:
            instance = self._factory()
            self._instances[loop] = instance
            return instance

    async def aget(self, afactory: Callable[[], Awaitable[T]]) -> T:
        """Instance for the current running loop, built via *afactory* on first
        use. A per-loop lock serialises concurrent first calls so the factory
        runs exactly once per loop."""
        loop = asyncio.get_running_loop()
        instance = self._instances.get(loop)
        if instance is not None:
            return instance
        lock = self._alocks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            self._alocks[loop] = lock
        async with lock:
            instance = self._instances.get(loop)
            if instance is None:
                instance = await afactory()
                self._instances[loop] = instance
            return instance

    def peek(self) -> T | None:
        """Instance for the current running loop, or None — never creates."""
        return self._instances.get(asyncio.get_running_loop())

    def pop_current(self) -> T | None:
        """Remove and return the current loop's instance (for close paths)."""
        return self._instances.pop(asyncio.get_running_loop(), None)

    def instances(self) -> list[T]:
        """Best-effort snapshot of all live instances (teardown/logging only —
        instances belonging to other loops must not be *used* from here)."""
        return list(self._instances.values())

    def clear(self) -> None:
        self._instances.clear()


__all__ = ["LoopLocal"]
