"""A waiter registry that tolerates an answer arriving before its waiter.

A permission prompt becomes visible to clients *before* the future that will
carry its answer exists: the ask is emitted to the open surfaces, then a durable
record is written, and only then does the fan-out task register its future. A
surface that answers inside that window answers into thin air — and the agent,
still blocked on a future nobody will now resolve, hangs forever (`R-BUG-09`).

Closing the window by reordering would mean reshaping the channel protocol, and
it would still be a race — a slow registry write could always lose to a fast
click. So an early answer is *held* here instead of rejected, and delivered the
instant the waiter registers.

An answer is only held for an id that has never had a waiter. Once one has
registered, its absence means the prompt is genuinely finished, and the caller
still gets its conflict.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any

# An ask outlives any reasonable registration delay but must not outlive the
# process's memory: a held answer that no waiter ever claims is dropped.
DEFAULT_TTL_SECONDS = 300.0
DEFAULT_MAX_HELD = 64
# Enough ids to tell "never registered" from "already finished" across a busy
# session, without growing without bound.
DEFAULT_MAX_SEEN = 512


class WaiterMap(dict):
    """``{id: Future}`` that can be handed a result before the future exists.

    Subclasses :class:`dict` so every existing reader — ``.get``, ``.pop``,
    ``in``, iteration — keeps working untouched. The only added behaviour is on
    ``__setitem__``, which delivers a held result to the arriving waiter.
    """

    def __init__(
        self,
        *,
        ttl: float = DEFAULT_TTL_SECONDS,
        max_held: int = DEFAULT_MAX_HELD,
        max_seen: int = DEFAULT_MAX_SEEN,
    ) -> None:
        super().__init__()
        self._ttl = ttl
        self._max_held = max_held
        self._max_seen = max_seen
        self._held: dict[str, tuple[Any, float]] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()

    def hold(self, key: str, result: Any) -> bool:
        """Keep ``result`` for a waiter that has not registered yet.

        Returns ``False`` — and keeps nothing — when a waiter for ``key`` has
        already been and gone, because that is a genuinely resolved prompt
        rather than an early answer.
        """
        if key in self._seen:
            return False
        self._expire()
        while len(self._held) >= self._max_held:
            oldest = min(self._held, key=lambda k: self._held[k][1])
            self._held.pop(oldest, None)
        self._held[key] = (result, time.monotonic())
        return True

    def held_ids(self) -> tuple[str, ...]:
        """Ids with an answer waiting for their waiter (for diagnostics)."""
        self._expire()
        return tuple(self._held)

    def _expire(self) -> None:
        now = time.monotonic()
        for key in [k for k, (_, ts) in self._held.items() if now - ts > self._ttl]:
            self._held.pop(key, None)

    def _remember(self, key: str) -> None:
        self._seen[key] = None
        self._seen.move_to_end(key)
        while len(self._seen) > self._max_seen:
            self._seen.popitem(last=False)

    def __setitem__(self, key: str, fut: Any) -> None:
        super().__setitem__(key, fut)
        self._remember(key)
        self._expire()
        held = self._held.pop(key, None)
        if held is None:
            return
        # The waiter is created on the loop that is about to await it, and this
        # runs inside that same registration, so resolving here is on-loop.
        if isinstance(fut, asyncio.Future) and not fut.done():
            fut.set_result(held[0])
