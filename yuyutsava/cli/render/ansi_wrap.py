"""Wrap ANSI-coloured text to a column width, without breaking the colours.

The chat dashboard's left pane must never draw past its own edge: one long
line and the transcript is painted straight across the context column beside
it. Rich wraps its own output to the pane width, but it is not the only writer
— ``warnings.warn`` and every ``logging`` record arrive as one unbounded line,
and a ``print`` can be any length at all. So the *view* wraps, at render time,
and the buffer stores logical lines. That also means a terminal that gets
narrower re-wraps correctly instead of bleeding.

Two things make this more than ``textwrap``:

* **escape sequences have no width.** Measuring ``len()`` on coloured text
  overestimates wildly and wraps far too early.
* **colour spans the break.** A line split in the middle of a coloured run has
  to re-open that run on the continuation, or the rest of the line loses its
  colour — and the pane must be reset at each break so nothing leaks into the
  next row.

East-Asian wide characters count as two columns and combining marks as zero,
because a pane is measured in cells, not codepoints.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

#: CSI / OSC escape sequences. Only SGR (``…m``) affects later text, but every
#: sequence is zero-width and must be carried through the wrap untouched.
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;:?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")

#: Turns every attribute off. Emitted at a wrap break so a colour cannot run
#: into the pane's neighbour.
_RESET = "\x1b[0m"

_SGR_RESET = ("\x1b[0m", "\x1b[m")


@lru_cache(maxsize=4096)
def char_width(ch: str) -> int:
    """Display cells for one character: 0 for combining, 2 for wide, else 1.

    Cached because this is the innermost loop of the whole display: three
    ``unicodedata`` lookups ran for every character of every line on every
    wrap, and a wrap happens per frame and per scroll event. The argument is
    one character, so the cache is bounded by the alphabet actually in use.
    """
    if unicodedata.combining(ch):
        return 0
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def visible_width(text: str) -> int:
    """Display width of *text*, ignoring escape sequences."""
    return sum(char_width(c) for c in _ANSI_RE.sub("", text))


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _tokens(line: str) -> list[tuple[bool, str]]:
    """Split into ``(is_escape, text)`` runs, in order."""
    out: list[tuple[bool, str]] = []
    pos = 0
    for m in _ANSI_RE.finditer(line):
        if m.start() > pos:
            out.append((False, line[pos : m.start()]))
        out.append((True, m.group()))
        pos = m.end()
    if pos < len(line):
        out.append((False, line[pos:]))
    return out


#: Cells a continuation row is indented by. Everything Rich writes already
#: fits the pane, so the lines that actually reach this wrapper are the ones
#: nothing formatted — a ``print``, a log record, a warning — and those are
#: exactly the ones whose second row, flush against column zero, reads as a
#: new statement rather than as the rest of the one above it.
HANG = 4


@lru_cache(maxsize=8192)
def row_count(line: str, width: int, hang: int = 0) -> int:
    """How many display rows *line* takes at *width*.

    Split from :func:`wrap_ansi` because the scroll arithmetic only ever
    needed the number, and computing it by wrapping the whole scrollback was
    what made the wheel stutter: every tick re-wrapped all five thousand
    buffered lines, characters and escape sequences and all.
    """
    if width <= 0 or visible_width(line) <= width:
        return 1
    return len(wrap_ansi(line, width, hang=hang))


def _hang_for(hang: int, width: int) -> int:
    """The continuation indent this width can afford, which may be none.

    An indent is dropped unless the pane is at least four times as wide: in a
    narrow column those cells are the difference between a sentence and a
    stack of fragments, and the reader needs the text more than the alignment.
    """
    return hang if 0 < hang * 4 <= width else 0


class _Row:
    """One row under construction, and the break it would rather take.

    A state machine rather than a run of locals in :func:`wrap_ansi`: the
    escape bookkeeping, the indent and the pending word all have to move
    together at a break, and keeping them apart is how a break re-opened the
    colour but lost the word, or indented a row and then measured it as if it
    had not been.
    """

    def __init__(self, width: int, hang: int) -> None:
        self.width = width
        self.hang = hang
        self.rows: list[str] = []
        self._active: list[str] = []  # SGR sequences still in effect
        self._parts: list[str] = []  # pieces of the row so far
        self._col = 0
        #: Where the last space sits in ``_parts``, and the column it ended at.
        #: A break there keeps a word whole; ``None`` means there is nowhere
        #: better than the hard edge.
        self._space: tuple[int, int] | None = None

    # -- escapes ------------------------------------------------------------

    def escape(self, chunk: str) -> None:
        self._parts.append(chunk)
        if chunk in _SGR_RESET:
            self._active.clear()
        elif chunk.endswith("m"):
            self._active.append(chunk)

    # -- text ---------------------------------------------------------------

    def char(self, ch: str, cells: int) -> None:
        if self._col + cells > self.width and self._col > self.hang:
            self.wrap()
        if ch == " ":
            self._space = (len(self._parts), self._col + cells)
        self._parts.append(ch)
        self._col += cells

    def ellipsis(self) -> None:
        """A glyph wider than the whole pane becomes one visible elision.

        Emitting it whole would overflow by a cell, which is the bleed this
        module exists to prevent, and dropping it would lose a character
        silently.
        """
        if self._col + 1 > self.width and self._col > 0:
            self.wrap()
        self._parts.append("…")
        self._col += 1

    # -- breaking -----------------------------------------------------------

    def wrap(self) -> None:
        """Close this row and start the next, carrying any part-word over."""
        carry: list[str] = []
        if self._breakable():
            at, _end = self._space
            carry = self._parts[at + 1 :]
            del self._parts[at + 1 :]
        self._emit("".join(self._parts))
        self._parts = [*self._active, " " * self.hang, *carry]
        self._col = self.hang + sum(char_width(c) for c in "".join(carry) if c not in _SGR_RESET)
        self._space = None

    def _breakable(self) -> bool:
        """Whether the last space is a better break than the hard edge.

        Only when it leaves most of the row used: a space near the start means
        the word that follows is longer than the pane, and moving it down only
        to break it there wastes a row and still breaks the word.
        """
        if self._space is None:
            return False
        _at, end = self._space
        return end * 2 >= self.width

    def _emit(self, text: str) -> None:
        self.rows.append(text + _RESET if self._active else text)

    def finish(self) -> list[str]:
        tail = "".join(self._parts)
        # A trailing row of nothing but re-opened escapes carries no text.
        if strip_ansi(tail).strip():
            self._emit(tail)
        elif not self.rows:
            self.rows.append(tail)
        return self.rows


def wrap_ansi(line: str, width: int, *, hang: int = 0) -> list[str]:
    """Wrap one logical line to *width* cells, preserving colour across breaks.

    Returns at least one element (``[""]`` for an empty line) so a blank line
    in the transcript stays a blank row.

    Breaks at a space when one is far enough along the row to be worth taking,
    and at the hard edge otherwise — a pane is not the place to keep a
    4,000-character base64 blob on one row, and a token longer than the pane
    has to break somewhere.

    A character wider than the entire pane becomes ``…``. Emitting it whole
    would overflow by a cell, and the no-overflow guarantee has to hold at
    every width or it is not a guarantee.

    *hang* indents every row after the first, so a wrapped line reads as one
    statement instead of starting again at column zero; :func:`_hang_for`
    decides whether the pane can afford it.
    """
    if width <= 0 or visible_width(line) <= width:
        return [line]

    row = _Row(width, _hang_for(hang, width))
    for is_escape, chunk in _tokens(line):
        if is_escape:
            row.escape(chunk)
            continue
        for ch in chunk:
            cells = char_width(ch)
            if cells > width:
                row.ellipsis()
            else:
                row.char(ch, cells)
    return row.finish()


def wrap_lines(lines: list[str], width: int, *, hang: int = 0) -> list[str]:
    """``wrap_ansi`` over a list, flattened."""
    out: list[str] = []
    for line in lines:
        out.extend(wrap_ansi(line, width, hang=hang))
    return out


__all__ = [
    "HANG",
    "char_width",
    "row_count",
    "strip_ansi",
    "visible_width",
    "wrap_ansi",
    "wrap_lines",
]
