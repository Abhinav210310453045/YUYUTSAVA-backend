"""Where a typed line goes: to an outstanding question, or to the REPL.

A full-screen application holds stdin in raw mode for its whole lifetime, so a
blocking ``input()`` anywhere else in the process can never see a keystroke.
The permission prompt found this the hard way: its card rendered, its
``approve/reject>`` appeared in the transcript, and typing did nothing. Every
question a full-screen front asks has to be read through the application's own
buffer — which is this.

So one line has two possible destinations, and the choice is not the
application's to make while drawing: an outstanding question takes the line
first, because queueing the answer would park it behind the very turn that is
blocked waiting for it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.history import FileHistory, InMemoryHistory

DEFAULT_PROMPT = "> "


class InputBroker:
    """The input line, the turn queue, and the question that outranks it."""

    def __init__(
        self,
        *,
        history_path: Path | None = None,
        completer: Any | None = None,
        invalidate: Callable[[], None] = lambda: None,
        is_closed: Callable[[], bool] = lambda: False,
    ) -> None:
        history = FileHistory(str(history_path)) if history_path is not None else InMemoryHistory()
        #: The prompt_toolkit buffer the layout draws and focuses.
        self.buffer = Buffer(
            history=history,
            completer=completer,
            complete_while_typing=True,
            multiline=False,
        )
        self.prompt = DEFAULT_PROMPT
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        #: A question the next submitted line answers *instead* of starting a
        #: turn.
        self._ask: asyncio.Future[str | None] | None = None
        self._invalidate = invalidate
        self._is_closed = is_closed

    # -- the line -----------------------------------------------------------

    def submit(self) -> str | None:
        """Take the current line.

        Returns the text when it was queued as a turn, and ``None`` when it
        answered an outstanding question — so a caller can react to the first
        without having to know how the second is resolved.
        """
        text = self.buffer.text
        self.buffer.reset(append_to_history=bool(text.strip()))
        ask = self._ask
        if ask is not None and not ask.done():
            ask.set_result(text)
            return None
        self._queue.put_nowait(text)
        return text

    def clear_line(self) -> None:
        self.buffer.reset()

    def close(self) -> None:
        """No more lines are coming — wake whoever is reading, with nothing."""
        self._queue.put_nowait(None)

    async def read_line(self) -> str | None:
        """Next submitted line, or ``None`` when the user asked to quit."""
        if self._is_closed():
            return None
        return await self._queue.get()

    # -- the question -------------------------------------------------------

    def has_pending_ask(self) -> bool:
        return self._ask is not None and not self._ask.done()

    async def ask(self, prompt: str = "approve/reject> ") -> str | None:
        """Read one line as the answer to a question, not as a new turn.

        Returns the line, or ``None`` if the user cancelled (Ctrl+C) or quit —
        which callers must treat as a refusal, never as consent.
        """
        if self._is_closed():
            return None
        loop = asyncio.get_running_loop()
        mine: asyncio.Future[str | None] = loop.create_future()
        previous, self._ask = self._ask, mine
        if previous is not None and not previous.done():
            # Parallel asks: the older one is not answerable any more, and
            # leaving it pending would hang its tool call forever.
            previous.set_result(None)
        self.prompt = prompt
        self._invalidate()
        try:
            return await mine
        finally:
            # Only clear the slot if it is still ours. A superseded ask resumes
            # *after* its replacement has installed itself, and clearing blindly
            # here wipes the live future — so the next submitted line is queued
            # as a new turn instead of answering the question.
            if self._ask is mine:
                self._ask = None
                self.prompt = DEFAULT_PROMPT
                self._invalidate()

    def cancel_ask(self) -> None:
        """Refuse any outstanding question. Used by Ctrl+C and by quit."""
        ask = self._ask
        if ask is not None and not ask.done():
            ask.set_result(None)

    def prompt_text(self) -> list[tuple[str, str]]:
        return [("class:prompt", self.prompt)]


__all__ = ["DEFAULT_PROMPT", "InputBroker"]
