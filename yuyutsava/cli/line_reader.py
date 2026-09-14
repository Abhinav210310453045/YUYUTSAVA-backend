"""One place the CLI reads a line from, whoever owns the terminal.

## The failure this exists for

The split-view dashboard runs a prompt_toolkit ``Application`` in full-screen
mode for the whole session. That puts the terminal into raw mode and makes the
application the sole reader of stdin, so any other code in the process calling
``input()`` — even on a worker thread — competes for those keystrokes and
loses. The measured symptom: a permission card rendered, ``approve/reject>``
appeared in the pane, and typing did nothing at all. The turn sat waiting on an
answer that could never arrive, and the only way out was killing the process.

Three prompts had that bug (the chat REPL's interrupt handler, the
background-subagent bridge, and ``yuyutsava attach``) and any new one would
have inherited it, because nothing in the type of ``input()`` says "not while a
full-screen application is running". So reading a line is a **seam** rather
than a call: whichever front owns the terminal installs its reader once, and
every prompt goes through :func:`read_line`.

Process-global and synchronous to install — the same shape as
``llm.quirks.first_chunk_retry.set_retry_listener`` and the context meter's
bus. There is only ever one owner of a terminal, so there is nothing to thread
through three constructors.

``None`` from :func:`read_line` means the line could not be read: EOF, Ctrl+C,
or a front shutting down. Every caller must treat it as a **refusal** — a
question that cannot be answered is never consent.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger("yuyutsava.cli.line_reader")

#: Takes the prompt to show, returns the line, or ``None`` if unanswerable.
Reader = Callable[[str], Awaitable[str | None]]

_reader: Reader | None = None


def set_line_reader(reader: Reader) -> None:
    """Install the reader for the front that owns the terminal."""
    global _reader
    _reader = reader


def clear_line_reader(reader: Reader | None = None) -> None:
    """Uninstall, falling back to ``input()``.

    Passing *reader* clears only if it is still the installed one, so a front
    tearing down late cannot unhook a front that has since taken over.

    Compared with ``==``, not ``is``: fronts install a **bound method**
    (``dashboard.ask``), and every attribute access builds a fresh bound-method
    object, so ``dash.ask is dash.ask`` is ``False``. Identity here silently
    never matched and the reader outlived the application that owned it.
    """
    global _reader
    if reader is None or _reader == reader:
        _reader = None


def has_line_reader() -> bool:
    """Whether a front has claimed stdin. Tests and diagnostics."""
    return _reader is not None


async def read_line(prompt: str) -> str | None:
    """One line of user input, stripped, or ``None`` if it cannot be read."""
    reader = _reader
    if reader is not None:
        try:
            line = await reader(prompt)
        except (EOFError, KeyboardInterrupt):
            return None
        except Exception:  # noqa: BLE001 — a broken front must not hang a turn
            logger.debug("installed line reader raised", exc_info=True)
            return None
        return None if line is None else line.strip()
    try:
        line = await asyncio.get_running_loop().run_in_executor(
            None, lambda: input(prompt)
        )
    except (EOFError, KeyboardInterrupt):
        return None
    return line.strip()


__all__ = [
    "Reader",
    "clear_line_reader",
    "has_line_reader",
    "read_line",
    "set_line_reader",
]
