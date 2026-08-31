"""Shared protocol for long-lived daemon loops.

The daemon schedules several concurrent loops — :class:`TriageLoop`,
:class:`OrchestratorLoop`, :class:`UnifiedSweeper` — all with the same shape:
``async def run(stop_event: asyncio.Event) -> None``. Capturing that contract
in one ``@runtime_checkable`` Protocol gives the type checker a hook without
forcing the existing classes into an inheritance chain. ``isinstance(loop,
Loop)`` works because the Protocol is runtime-checkable.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable


@runtime_checkable
class Loop(Protocol):
    """Long-lived async loop scheduled by the daemon.

    Implementations run until ``stop_event`` is set (or the agent yields).
    They MUST NOT swallow ``CancelledError`` — graceful teardown is driven
    by the event, not by cancellation, but the lifecycle owner reserves the
    right to cancel as a fallback.
    """

    async def run(self, stop_event: asyncio.Event) -> None: ...
