"""The words and colours a background task's progress is reported in.

Two surfaces report it and they render different things: `attach` reads a
daemon's whole turn stream off the wire, tokens and tool calls included, while
the REPL's bridge shows only the four typed background-task payloads. So they
are not one renderer (`R-DRY-05`) — but they were each spelling the tags and
picking the colours themselves, and a user watching one surface and then the
other has to be told the same task reached the same state in the same words.

Each returns the tag already coloured and closed. What a caller puts after it
is its own business: the bridge has a task id and an elapsed time, the wire
does not always.
"""

from __future__ import annotations

from yuyutsava.ansi import CYAN, DIM, GREEN, RED, RESET, YELLOW


def started() -> str:
    """A background task has been accepted and is running."""
    return f"{CYAN}[bg started]{RESET}"


def progress() -> str:
    """A step inside a running task. Dim: it is context, not news."""
    return f"{DIM}[bg progress]{RESET}"


def awaiting_user() -> str:
    """The task has stopped and cannot continue without an answer."""
    return f"{YELLOW}[bg awaiting user]{RESET}"


def done(*, ok: bool) -> str:
    """The task finished. Green or red, because this is the line a user scans
    a scrollback for."""
    colour = GREEN if ok else RED
    return f"{colour}[bg done {'OK' if ok else 'FAIL'}]{RESET}"


__all__ = ["awaiting_user", "done", "progress", "started"]
