"""The process-wide publication point, and the two counters that ride with it.

The meter publishes a :class:`~yuyutsava.context.meter.snapshot.ContextSnapshot`
twice per model call — before, describing what is about to be sent, and after,
carrying what the provider says it charged. The CLI dashboard subscribes and
repaints; the daemon's ``/ws/converse`` handler subscribes and forwards a frame
to the app.

Deliberately **not** a ``StreamEvent`` kind: that would mean teaching
``core/streaming``, both CLI renderers, the SSE payload union and the static web
UI about a frame none of them can act on.

The compaction and offload counters live here rather than with the policy
because the controllers that do those things report them, and a conversation's
count is not one graph's.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from yuyutsava.context.meter.bounded import Bounded
from yuyutsava.context.meter.snapshot import ContextSnapshot

logger = logging.getLogger("yuyutsava.context.meter")


Subscriber = Callable[[ContextSnapshot], None]


class MeterBus:
    """Fan-out of snapshots to whoever is displaying them.

    Same shape as ``llm.quirks.first_chunk_retry.set_retry_listener``: process
    global, synchronous, and defensive. Callbacks run inside the agent's model
    step, so they must be cheap and non-blocking — repaint requests and queue
    puts, not I/O. A raising subscriber is logged and dropped from that publish,
    never propagated: a broken meter must not fail a turn.
    """

    def __init__(self) -> None:
        self._subs: dict[int, tuple[str, Subscriber]] = {}
        self._latest: Bounded = Bounded()
        self._next = 1

    def subscribe(self, thread_id: str, cb: Subscriber) -> int:
        """Listen to one thread, or to every thread when *thread_id* is ``""``."""
        token = self._next
        self._next += 1
        self._subs[token] = (thread_id, cb)
        return token

    def unsubscribe(self, token: int) -> None:
        self._subs.pop(token, None)

    def publish(self, snap: ContextSnapshot) -> None:
        if snap.thread_id:
            self._latest[snap.thread_id] = snap
        for thread_id, cb in list(self._subs.values()):
            if thread_id and thread_id != snap.thread_id:
                continue
            try:
                cb(snap)
            except Exception:  # noqa: BLE001 — see the class docstring
                logger.debug("context meter: subscriber raised", exc_info=True)

    def latest(self, thread_id: str = "") -> ContextSnapshot | None:
        """Most recent snapshot for a thread, for a client that just connected."""
        if thread_id:
            return self._latest.get(thread_id)
        return next(reversed(self._latest.values()), None) if self._latest else None

    def clear(self) -> None:
        """Drop all state. Tests only."""
        self._subs.clear()
        self._latest.clear()


_BUS = MeterBus()


def bus() -> MeterBus:
    """The process-wide meter bus."""
    return _BUS


# ----------------------------------------------------------------------
# Counters the context controllers report into
# ----------------------------------------------------------------------

_compactions: Bounded = Bounded()


_offloads: Bounded = Bounded()


def note_compaction(thread_id: str) -> None:
    """Record that history was summarized away on this thread.

    Compaction is the only step that drops messages, so "how many times has
    this happened" is the single most useful number for judging whether a
    session's window is under pressure — and it was unobservable.
    """
    if thread_id:
        _compactions[thread_id] = _compactions.get(thread_id, 0) + 1


def note_offload(thread_id: str) -> None:
    """Record that a tool result was moved out of context into an artifact."""
    if thread_id:
        _offloads[thread_id] = _offloads.get(thread_id, 0) + 1


def counters(thread_id: str) -> tuple[int, int]:
    """``(compactions, offloads)`` seen for this thread in this process."""
    return _compactions.get(thread_id, 0), _offloads.get(thread_id, 0)


def reset_counters() -> None:
    """Drop the per-thread counters. Tests only."""
    _compactions.clear()
    _offloads.clear()


__all__ = [
    "MeterBus",
    "Subscriber",
    "bus",
    "counters",
    "note_compaction",
    "note_offload",
    "reset_counters",
]
