"""Smooth, adaptive pacing of streamed LLM text to a terminal.

The chat REPL receives assistant text as LLM token *chunks* that arrive in
irregular sizes and at irregular intervals (a big block, then a stall, then
another block). Printing each chunk the instant it arrives mirrors that
irregularity and feels jerky — "drive a yard, hard brake, accelerate, brake".

:class:`TokenSmoother` decouples the *display* rate from the *arrival* rate.
``feed()`` appends incoming text to an internal buffer without blocking; a
single background asyncio task drains that buffer to the terminal a few
characters at a time at a steady, adaptive cadence:

  * When caught up, it emits at a gentle steady pace (``base_cps``) so prose
    flows smoothly like a typewriter.
  * When the model is far ahead (large backlog), it speeds up toward
    ``max_cps`` so it never lags noticeably behind and a burst finishes fast.

The component is intentionally self-contained: it depends only on the stdlib
and a write callable, so it can be dropped in or out (e.g. disabled on a
non-TTY) without affecting any other part of the system.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Callable


class TokenSmoother:
    """Pace streamed text to a writer at a steady, adaptive per-character rate.

    Parameters
    ----------
    write:
        Callable that writes a string to the destination (e.g. a function
        wrapping ``sys.stdout.write``). The smoother flushes after each write.
    flush:
        Optional callable invoked after each write to flush the destination.
    base_cps:
        Steady characters-per-second when the smoother has caught up to the
        incoming stream. This sets the "feel" of the typewriter cadence.
    max_cps:
        Upper bound on characters-per-second used when a large backlog has
        accumulated, so the smoother can catch up without a visible dump.
    catchup_chars:
        Backlog size (in characters) at which the delay is roughly halved.
        Smaller values make the smoother more eager to catch up.
    """

    def __init__(
        self,
        write: Callable[[str], None],
        *,
        flush: Callable[[], None] | None = None,
        base_cps: float = 180.0,
        max_cps: float = 1200.0,
        catchup_chars: int = 120,
    ) -> None:
        self._write = write
        self._flush = flush
        self._base_delay = 1.0 / max(1.0, base_cps)
        self._min_delay = 1.0 / max(1.0, max_cps)
        self._catchup = max(1, catchup_chars)
        self._buf: deque[str] = deque()
        self._buflen = 0
        self._task: asyncio.Task | None = None
        self._data_ready = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()  # empty buffer == idle

    # -- producer side -----------------------------------------------------

    def feed(self, text: str) -> None:
        """Append ``text`` to the buffer and wake the drain task.

        Non-blocking. Lazily starts the background drain task on first use.
        """
        if not text:
            return
        self._buf.append(text)
        self._buflen += len(text)
        self._idle.clear()
        self._data_ready.set()
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())

    # -- consumer side -----------------------------------------------------

    async def drain(self) -> None:
        """Block until the buffer is fully written to the destination.

        Callers use this before printing non-prose output (tool calls, logs)
        so buffered text appears in the correct order, and at end of turn.
        """
        if self._buflen == 0:
            return
        await self._idle.wait()

    async def aclose(self) -> None:
        """Drain any remaining text, then stop the background task."""
        await self.drain()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # -- internals ---------------------------------------------------------

    def _delay_for(self, backlog: int) -> float:
        """Adaptive per-tick delay: larger backlog -> shorter delay."""
        delay = self._base_delay / (1.0 + backlog / self._catchup)
        if delay < self._min_delay:
            return self._min_delay
        return delay

    def _take(self, n: int) -> str:
        """Pop up to ``n`` characters from the front of the buffer."""
        out: list[str] = []
        need = n
        while need > 0 and self._buf:
            head = self._buf[0]
            if len(head) <= need:
                out.append(head)
                need -= len(head)
                self._buf.popleft()
            else:
                out.append(head[:need])
                self._buf[0] = head[need:]
                need = 0
        taken = "".join(out)
        self._buflen -= len(taken)
        return taken

    async def _run(self) -> None:
        while True:
            if self._buflen == 0:
                self._idle.set()
                self._data_ready.clear()
                await self._data_ready.wait()
                continue
            # Emit a chunk sized to the backlog: a few chars at a time when
            # caught up, more per tick when far behind, so we stay smooth
            # without thousands of tiny sleeps on a large burst.
            backlog = self._buflen
            chunk = 1 + backlog // self._catchup
            self._write(self._take(chunk))
            if self._flush is not None:
                self._flush()
            await asyncio.sleep(self._delay_for(backlog))
